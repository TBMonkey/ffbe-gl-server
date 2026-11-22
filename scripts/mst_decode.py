#!/usr/bin/env python3
"""Decode the FFBEWW MST `.dat` tables (`<apk>/files/mst/Ver<N>_<token>.dat`).

Reversed 2026-09-13 from the original Android client v9.0.0 (WW), `libgame.so`
(BuildID d810bec04d71db5834ceb6f8b172b3b99368ec1a), cocos2d-x 3.x.

Cipher (proven by decrypting every one of the 219 tables, see
`docs/notes/mst_decode_and_hash_census.md`)
--------------------------------------------------------------------------
File layout: the file is ASCII base64 **split across lines**; each line is an
independently base64-encoded ciphertext (its own `=` padding) and decodes to a
whole number of 16-byte AES blocks.  As the record count equals the line count
(e.g. `Ver523_24Eka6wL.dat` has exactly 17,690 lines = 17,690 F_MISSION_MST
rows), **one line == one MST record**.

Per-line:  base64 -> AES-128 decrypt -> strip padding -> UTF-8 JSON object.

AES key:   the third NUL-terminated 8-character field of the table's entry in
           the `libgame.so` `.rodata` table manifest, right-padded with 8 NUL
           bytes to 16.  (For F_TEXT_* tables the same rule holds; there the
           second field is the *file* basename and the third the key.  For MST
           tables the second field is also the file basename -- so the key is
           never the filename token, which is why "filename token + 8 NULs"
           yields garbage.)

Mode:      two writer generations coexist.  Both use the same key.
             ECB  -- plaintext is PKCS5-padded to a block boundary.
             CBC  -- same key, IV = b'dZMjkk8gFDzKHlsx'
                     (15-byte ASCII string, 16 bytes in `.rodata`), the
                     PKCS5 padding block is *not* the last block: PKCS5 pad to
                     a block boundary, then one extra full 0x10 block.
           Because the padding tail differs, the decoder does not rely on the
           padding bytes at all: it truncates each record at its final `}`
           (padding bytes are 0x01..0x10, never `}`) and validates with
           `json.loads`.

Decryption is done by the `openssl` CLI in one bulk `-aes-128-ecb -nopad`
call per file; the CBC interpretation is then a pure-XOR post-pass over the
decrypted stream (`CBC decrypt == ECB decrypt, then XOR with the previous
ciphertext block`), so no per-line process spawning and no Python AES.

Usage
-----
    python3 scripts/mst_decode.py                 # decode all 219 tables
    python3 scripts/mst_decode.py --only B9K8ULHc # one token (debug)
    python3 scripts/mst_decode.py --verify        # decode + row-count check

Output: `mst_decoded/<F_TABLE_NAME>.ndjson` (one JSON object per line) plus
`mst_decoded/_DECODE_REPORT.tsv`.  Requires `/usr/bin/openssl`; needs a
`.gitignore` entry for `mst_decoded/`.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
from typing import Dict, Iterable, List, Optional, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIBGAME = os.path.join(REPO_ROOT, "libgame.so")
MST_DIR = os.path.join(
    REPO_ROOT,
    "com.square_enix.android_googleplay.FFBEWW",
    "files",
    "mst",
)
OUT_DIR = os.path.join(REPO_ROOT, "mst_decoded")

# `.rodata` of this build: VADDR == file offset (see `readelf -S libgame.so`,
# .rodata @ 0x2f22e40).  The table manifest sits roughly 0x2f46000-0x2f58000.
RODATA_START = 0x2F22E40
RODATA_SCAN_END = 0x3100000

CBC_IV = b"dZMjkk8gFDzKHlsx"  # 16-byte ASCII string, single hit in .rodata

TABLE_RE = re.compile(rb"^F_[A-Z0-9_]+$")
TOKEN_RE = re.compile(rb"^[A-Za-z0-9]{8}$")
DAT_RE = re.compile(r"^Ver(\d+)_([A-Za-z0-9]{8})\.dat$")

# Documented row counts used by --verify (docs/analysis/FFBE_UNIT_DATA_MODEL.md
# and docs/archive/crossref_aenigmatic_remaining.md).
EXPECTED_ROWS = {
    "F_UNIT_MST": 3311,
    "F_MISSION_MST": 17690,
    "F_ITEM_MST": 4729,
    "F_MONSTER_SKILL_SET_MST": 5187,
    "F_LIMITBURST_MST": 3584,
    "F_RECIPE_MST": 2051,
}


# --------------------------------------------------------------------------
# manifest
# --------------------------------------------------------------------------
def _rodata_strings(lib: bytes) -> List[Tuple[int, bytes]]:
    """Null-terminated printable-ASCII runs of `.rodata`, in offset order."""
    out = []
    run_re = re.compile(rb"[ -~]{1,400}")
    for m in run_re.finditer(lib, RODATA_START):
        if m.start() > RODATA_SCAN_END:
            break
        out.append((m.start(), m.group()))
    return out


def extract_manifest(lib_path: str = LIBGAME) -> Tuple[Dict[str, Tuple[str, str]], Dict[str, str]]:
    """Return (by_basename, by_table).

    `by_basename[token] = (aes_key_field, table_name)`.  A manifest entry is a
    contiguous run `TABLE\\0<8char basename>\\0<8char key>\\0` in `.rodata`;
    the manifest is emitted in declaration order, so those three allocations
    are normally adjacent.
    """
    with open(lib_path, "rb") as fh:
        lib = fh.read(RODATA_SCAN_END + 0x1000)

    strings = _rodata_strings(lib)
    at = {off: s for off, s in strings}
    order = sorted(at)

    by_basename: Dict[str, Tuple[str, str]] = {}
    by_table: Dict[str, str] = {}
    for i, off in enumerate(order):
        name = at[off]
        if i + 2 >= len(order):
            continue
        o2, o3 = order[i + 1], order[i + 2]
        s2, s3 = at[o2], at[o3]
        if not (TABLE_RE.match(name) and TOKEN_RE.match(s2) and TOKEN_RE.match(s3)):
            continue
        if off + len(name) + 1 != o2 or o2 + len(s2) + 1 != o3:
            continue  # not a contiguous triple in the pool
        by_basename[s2.decode()] = (s3.decode(), name.decode())
        by_table[name.decode()] = s2.decode()

    # Fallback for the handful of tables whose three strings are not adjacent
    # in the pool: the key field is the next 8-char token after the basename.
    for i, off in enumerate(order):
        tok = at[off]
        if not TOKEN_RE.match(tok):
            continue
        if tok.decode() in by_basename:
            continue
        for j in range(i + 1, min(i + 4, len(order))):
            s = at[order[j]]
            if TOKEN_RE.match(s):
                by_basename.setdefault(tok.decode(), (s.decode(), ""))
                break

    # Name resolution for the five tables whose name string is NOT adjacent to the
    # (basename, key) pair, so the contiguous-triple scan above cannot see it. The
    # manifest emits `basename | key | ClassName`, and the class name is the tell;
    # each of the five also has a matching preference key in gamedata.xml (three
    # under `keyF_*_MST`, two under `keyF_*_MST_LOCALIZE` -- which is why a
    # `keyF_*_MST`-suffixed filter misses exactly two).
    # Resolved 2026-09-13; byte-level evidence in
    # docs/notes/mst_decode_and_hash_census.md and the session log.
    NAME_BY_TOKEN = {
        "fFDtEMbG": "F_HOME_SCREEN_ICON_MST",              # | fFDtEMbG | 3HHaJzws | home_icon_ | HomeIconMstList
        "BX0pRc8A": "F_MATERIA_LIMIT_MST",                 # | BX0pRc8A | CVERk9KN | MateriaLimitMstList
        "i5QEJpua": "F_LOCALIZED_TEXT_MST",                # | i5QEJpua | R8PYPnJ9 | sgSplitLocalizedTextMstList
        "YIgCpDKW": "F_RESOURCE_MAP_VERSION_MST_LOCALIZE",  # | YIgCpDKW | geHlpoos | ResourceMapVersionMstLocalizeList
        "A1L1GlaQ": "F_RESOURCE_VERSION_MST_LOCALIZE",      # | A1L1GlaQ | JchlFPWK | ResourceVersionMstLocalizeList
    }
    for _tok, _name in NAME_BY_TOKEN.items():
        if _tok in by_basename and not by_basename[_tok][1]:
            by_basename[_tok] = (by_basename[_tok][0], _name)
            by_table[_name] = _tok

    return by_basename, by_table


def candidate_keys(lib_path: str = LIBGAME) -> List[str]:
    """Every distinct 8-char token in `.rodata` (brute-force fallback set)."""
    with open(lib_path, "rb") as fh:
        lib = fh.read(RODATA_SCAN_END + 0x1000)
    seen = set()
    out = []
    for _off, s in _rodata_strings(lib):
        if len(s) == 8 and TOKEN_RE.match(s):
            d = s.decode()
            if d not in seen:
                seen.add(d)
                out.append(d)
    return out


# --------------------------------------------------------------------------
# crypto
# --------------------------------------------------------------------------
def _aes_ecb_raw(key16: bytes, ct: bytes) -> bytes:
    """AES-128-ECB, no padding — `cryptography` when present, `openssl` only as the last resort.

    WHY THIS ORDER (2026-10-24, APP-50/APP-57): the release bundle ships `cryptography` (it is
    `server/ffbe_crypto.py`'s preferred backend) but does NOT ship an `openssl` binary, and a Windows user
    has none at `/usr/bin/openssl` at all. Hardcoding the CLI made this decoder dead on the very machines
    the bundle targets. The CLI stays as a fallback so a bare checkout with no `cryptography` still works.
    """
    if len(ct) % 16:
        raise ValueError("ciphertext not 16-byte aligned")
    if not ct:
        return b""
    if len(key16) != 16:
        raise ValueError("key must be 16 bytes")
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        op = Cipher(algorithms.AES(key16), modes.ECB()).decryptor()
        return op.update(ct) + op.finalize()
    except ImportError:
        pass
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = os.path.join(tmp, "ct.bin"), os.path.join(tmp, "pt.bin")
        with open(src, "wb") as fh:
            fh.write(ct)
        proc = subprocess.run([os.environ.get("FFBE_OPENSSL", "openssl"), "enc", "-d", "-aes-128-ecb",
                               "-nopad", "-K", key16.hex(), "-in", src, "-out", dst], capture_output=True)
        if proc.returncode != 0:
            raise RuntimeError("neither `cryptography` nor openssl could decrypt: "
                               + proc.stderr.decode(errors="replace"))
        with open(dst, "rb") as fh:
            return fh.read()


def aes128_ecb_decrypt_stream_keyed(ct: bytes, key16: bytes) -> bytes:
    """Bulk AES-128-ECB decrypt of the concatenated per-line ciphertexts."""
    return _aes_ecb_raw(key16, ct)


def _json_record(buf: bytes) -> Optional[bytes]:
    """Return the `{...}` record in `buf`, or None.

    The last byte of a record is always `}` and padding bytes are 0x01..0x10,
    so the record's terminator is the last `}` in the buffer.  The record does
    *not* necessarily start at the last `{`: several tables carry literal `{`
    inside string values (e.g. F_SP_CHALLENGE_MST, F_RB_SS_REWARD_MST), so walk
    the `{` positions in order and take the first one that parses.

    A `{}` found by chance inside wrongly-decrypted bytes is rejected: a record
    must parse to a *non-empty* JSON object, otherwise a brute-forced wrong key
    can look like a hit.
    """
    end = buf.rfind(b"}")
    if end < 0:
        return None
    pos = buf.find(b"{")
    while 0 <= pos <= end:
        rec = buf[pos:end + 1]
        try:
            obj = json.loads(rec.decode("utf-8"))
            if isinstance(obj, dict) and obj:
                return rec
        except Exception:
            pass
        pos = buf.find(b"{", pos + 1)
    return None


# --------------------------------------------------------------------------
# per-file decode
# --------------------------------------------------------------------------
def read_lines(path: str) -> List[bytes]:
    """Every line's independently base64-decoded ciphertext."""
    out = []
    with open(path, "rb") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                ct = base64.b64decode(raw, validate=True)
            except Exception:
                ct = base64.b64decode(raw + b"=" * (-len(raw) % 4))
            out.append(ct)
    return out


def decrypt_line(dstream: bytes, base_off: int, ct_line: bytes, mode: str) -> bytes:
    """Plaintext of one line, given the bulk-decrypted stream."""
    n = len(ct_line) // 16
    d = dstream[base_off:base_off + len(ct_line)]
    if mode == "ECB":
        return d
    prev = CBC_IV
    out = bytearray()
    for j in range(n):
        blk = ct_line[j * 16:(j + 1) * 16]
        out += bytes(a ^ b for a, b in zip(d[j * 16:(j + 1) * 16], prev))
        prev = blk
    return bytes(out)


def pick_mode(dstream: bytes, ct_lines: List[bytes], probe: int = 3) -> Optional[str]:
    off = 0
    for i, ct in enumerate(ct_lines[:probe]):
        for mode in ("ECB", "CBC"):
            if _json_record(decrypt_line(dstream, off, ct, mode)) is not None:
                return mode
        off += len(ct)
    return None


def decode_file(path: str, key: bytes) -> Tuple[List[bytes], Dict[str, int]]:
    """Return (records, stats) for one .dat file."""
    ct_lines = read_lines(path)
    concat = b"".join(ct_lines)
    if len(concat) % 16:
        raise ValueError(f"{os.path.basename(path)}: ciphertext not block aligned")
    dstream = aes128_ecb_decrypt_stream_keyed(concat, key)
    mode = pick_mode(dstream, ct_lines)
    stats = {"lines": len(ct_lines), "mode": mode or "?", "fallback": 0, "bad": 0}
    if mode is None:
        stats["bad"] = len(ct_lines)
        return [], stats

    records = []
    off = 0
    for ct in ct_lines:
        rec = _json_record(decrypt_line(dstream, off, ct, mode))
        if rec is None:  # per-line mode flip (defensive)
            other = "CBC" if mode == "ECB" else "ECB"
            rec = _json_record(decrypt_line(dstream, off, ct, other))
            if rec is not None:
                stats["fallback"] += 1
        if rec is None:
            stats["bad"] += 1
        else:
            records.append(rec)
        off += len(ct)
    return records, stats


def brute_key(path: str, cands: Iterable[str]) -> Optional[bytes]:
    """Find the AES key of a file whose manifest entry was not adjacent."""
    ct_lines = read_lines(path)
    concat = b"".join(ct_lines[:2])
    for tok in cands:
        key = tok.encode() + b"\x00" * 8
        try:
            d = aes128_ecb_decrypt_stream_keyed(concat, key)
        except Exception:
            continue
        off = 0
        for ct in ct_lines[:2]:
            for mode in ("ECB", "CBC"):
                if _json_record(decrypt_line(d, off, ct, mode)) is not None:
                    return key
            off += len(ct)
    return None


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--libgame", default=LIBGAME)
    ap.add_argument("--mst-dir", default=MST_DIR)
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--only", default=None, help="decode just this 8-char token")
    ap.add_argument("--verify", action="store_true", help="check documented row counts")
    args = ap.parse_args(argv)

    by_basename, by_table = extract_manifest(args.libgame)
    names = sorted(f for f in os.listdir(args.mst_dir) if f.endswith(".dat"))
    if args.only:
        names = [f for f in names if args.only in f]
    os.makedirs(args.out, exist_ok=True)

    rows = []
    unresolved_tables = []
    for fn in names:
        m = DAT_RE.match(fn)
        if not m:
            continue
        ver, token = int(m.group(1)), m.group(2)
        entry = by_basename.get(token)
        if entry is None:
            key = brute_key(os.path.join(args.mst_dir, fn), candidate_keys(args.libgame))
            if key is None:
                unresolved_tables.append(fn)
                rows.append((fn, token, "", "?", 0, 0, 0, 0, 0, "NO_KEY"))
                print(f"  !! no key: {fn}", file=sys.stderr)
                continue
            table = f"UNKNOWN_{token}"
        else:
            field3, table = entry
            key = field3.encode() + b"\x00" * 8
            if not table:
                table = f"UNKNOWN_{token}"

        records, stats = decode_file(os.path.join(args.mst_dir, fn), key)
        outp = os.path.join(args.out, table + ".ndjson")
        with open(outp, "wb") as fh:
            for r in records:
                fh.write(r + b"\n")
        status = "OK" if stats["bad"] == 0 else "PARTIAL"
        rows.append((fn, token, table, stats["mode"], ver,
                     stats["lines"], len(records), stats["fallback"], stats["bad"], status))
        print(f"{status:7s} {table:40s} {fn:24s} mode={stats['mode']:3s} "
              f"lines={stats['lines']:6d} records={len(records):6d} "
              f"fallback={stats['fallback']} bad={stats['bad']}")

    report = os.path.join(args.out, "_DECODE_REPORT.tsv")
    with open(report, "w") as fh:
        fh.write("file\ttoken\ttable\tmode\tversion\tlines\trecords\tmode_fallback\tbad\tstatus\n")
        for r in rows:
            fh.write("\t".join(str(x) for x in r) + "\n")

    # Regenerate, don't accumulate: drop any .ndjson from a previous run that this
    # run did not produce (this is how the `UNKNOWN_<token>` names disappear once a
    # table's name is resolved).
    produced = {r[2] + ".ndjson" for r in rows}
    for stale in os.listdir(args.out):
        if stale.endswith(".ndjson") and stale not in produced:
            os.remove(os.path.join(args.out, stale))

    print(f"\nfiles decoded      : {sum(1 for r in rows if r[9] == 'OK')}/{len(rows)}")
    print(f"tables with no key : {len(unresolved_tables)} {unresolved_tables}")
    print(f"total records      : {sum(r[6] for r in rows)}")
    print(f"report             : {report}")

    if args.verify:
        got = {r[2]: r[6] for r in rows}
        print("\n--- expected vs decoded ---")
        bad = 0
        for t, want in EXPECTED_ROWS.items():
            have = got.get(t)
            flag = "PASS" if have == want else "FAIL"
            if have != want:
                bad += 1
            print(f"{flag}  {t:26s} expected={want:6d} decoded={have}")
        print("verify:", "PASS" if bad == 0 else f"FAIL ({bad})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
