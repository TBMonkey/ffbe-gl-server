#!/usr/bin/env python3
"""Decode the localized-text `.dat` tables (the English set, 145 files).

Sibling of `scripts/mst_decode.py`. The two differ in exactly the two things a reader needs to compare:

| | MST master data (`mst_decode.py`) | localized text (this script) |
|---|---|---|
| key source | **field 3** of the table's entry in the `libgame.so` `.rodata` manifest | `docs/analysis/localized_text_keys.json` (token → table + key) |
| framing | **per line** — one line = one row | **whole file** |
| cipher | probed per file: **157 CBC / 62 ECB** | **ECB** (CBC yields garbage) |

Record shapes actually present (all measured 2026-09-13 — there is **no universal key prefix**):

| shape | example | where |
|---|---|---|
| `MST_<NAME>_<id>^<field>^…` | `MST_UNIT_NAME_100000102^Rain^Rain` | most tables |
| `<TABLE>_<id>^<field>^…` (no `MST_`) | `FIELD_EFFECT_EXPLAIN_SHORT_73^Fill LB gauge` | `F_TEXT_FIELD_EFFECT_EXPLAIN_*` |
| `<NAME>^` (bare list) | `F_TEXT_ABILITY_NAME^` | `F_TEXT_ANALYTICS_LOCALIZE` |
| JSON object per line | `{"P_BATTLE_SCRIPT_INFO":"1001",…}` | `F_TEXT_BATTLE_SCRIPT` — **not text at all** |
| CSV with a header row | `item_type^item_id^use^…` then data | the `8e6PGb3p` UNKNOWN |

So the rule is simply: **every non-empty line is a record; the first `^`-delimited field is the key.**
(The delimiter is `^`, *not* the `|` that `LOCALIZED_TEXT_FORMAT.md` states, and it is not "1 JSON table".)

Output: `text_decoded/<TABLE>.ndjson`:
  * text record  → `{"key": "<first field>", "v": ["<field>", …]}`
  * JSON record  → `{"key": "", "j": {…}}`
plus `_TEXT_REPORT.tsv`.

Requires `/usr/bin/openssl`. No Python crypto dependency.

Usage:
    python3 scripts/text_decode.py [--verify] [--lang en]
"""

import argparse
import base64
import glob
import json
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APK = os.path.join(REPO, "com.square_enix.android_googleplay.FFBEWW")
KEYS_JSON = os.path.join(REPO, "docs", "analysis", "localized_text_keys.json")
OPENSSL = "/usr/bin/openssl"


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


def aes128_ecb_decrypt(ct: bytes, key8: str) -> bytes:
    """AES-128-ECB decrypt with the 8-char token as the key, right-padded with NULs."""
    key16 = key8.encode("ascii").ljust(16, b"\x00")
    return _aes_ecb_raw(key16, ct)


def pkcs5_unpad(b: bytes) -> bytes:
    if not b:
        return b
    pad = b[-1]
    if 1 <= pad <= 16 and b[-pad:] == bytes([pad]) * pad:
        return b[:-pad]
    return b


def parse_records(text: str):
    """Yield (key, payload, is_json) for every non-empty line. See the module docstring for shapes."""
    for line in text.split("\n"):
        line = line.rstrip("\r")
        if not line:
            continue
        if line[0] in "{[":
            try:
                yield "", json.loads(line), True
                continue
            except ValueError:
                pass
        parts = line.split("^")
        yield parts[0], parts[1:], False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--lang", default="en", help="subdir of files/localized_texts (default: en)")
    ap.add_argument("--out", default=os.path.join(REPO, "text_decoded"))
    ap.add_argument("--verify", action="store_true", help="re-check the delivered tree")
    # The three overrides below exist because a user does NOT have a checkout: until the engine owns the
    # decoders (APP-50) this is the only way the script is runnable from a release bundle. Defaults are the
    # checkout layout, byte-for-byte the old behaviour. Homes: docs/SIMPLER_DISTRIBUTABLE.md S0/S1.
    ap.add_argument("--keys", default=KEYS_JSON, help="the localized_text_keys.json manifest")
    ap.add_argument("--client-data", default=APK,
                    help="the client app-data dir (the one holding files/localized_texts)")
    ap.add_argument("--openssl", default=OPENSSL, help="path to the openssl binary")
    args = ap.parse_args(argv)

    globals()["OPENSSL"] = args.openssl
    with open(args.keys, encoding="utf-8") as fh:
        keys = json.load(fh)

    src = os.path.join(args.client_data, "files", "localized_texts", args.lang)
    if not os.path.isdir(src):
        # `--client-data` may also be the `files/` directory itself (prepare accepts both, and a user who
        # has unzipped a device dump often has exactly that path). Same language dir, one level up.
        alt = os.path.join(args.client_data, "localized_texts", args.lang)
        if os.path.isdir(alt):
            src = alt
    files = sorted(glob.glob(os.path.join(src, "*.dat")))
    if not files:
        print(f"no .dat under {src}", file=sys.stderr)
        return 2

    os.makedirs(args.out, exist_ok=True)
    rows, bad_total, json_total, unknown_names = [], 0, 0, []
    bad_files = []
    for path in files:
        fn = os.path.basename(path)
        m = re.match(r"Ver(\d+)_([A-Za-z0-9]+)\.dat$", fn)
        if not m:
            print(f"SKIP  {fn}  (name is not Ver<N>_<token>.dat)")
            continue
        ver, token = m.group(1), m.group(2)
        info = keys.get(token)
        if not info:
            print(f"SKIP  {fn}  (token {token} not in the key map)")
            continue
        table = info.get("table") or f"UNKNOWN_{token}"
        if table == "UNKNOWN":
            table = f"UNKNOWN_{token}"
            unknown_names.append(token)

        raw = open(path, "rb").read()
        # One unreadable table must not abort the whole run: a device dump can carry a patched, truncated
        # or non-base64 file, and the remaining tables are still usable. Name it and move on; the summary
        # and the exit code decide whether enough decoded.
        try:
            text = pkcs5_unpad(
                aes128_ecb_decrypt(base64.b64decode(raw), info["key"])
            ).decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001 - a per-file failure is data, not a crash
            print(f"BAD     {table:44s} {fn:22s} skipped ({type(exc).__name__}: {exc})")
            bad_files.append((fn, f"{type(exc).__name__}: {exc}"))
            continue

        recs, bad, jsons = [], 0, 0
        for key, payload, is_json in parse_records(text):
            if is_json:
                jsons += 1
            elif not key:
                bad += 1
                continue
            recs.append((key, payload, is_json))
        bad_total += bad
        json_total += jsons

        outp = os.path.join(args.out, table + ".ndjson")
        with open(outp, "w", encoding="utf-8") as fh:
            for key, payload, is_json in recs:
                obj = {"key": key, "j": payload} if is_json else {"key": key, "v": payload}
                fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
        rows.append((fn, token, table, ver, len(recs), bad, jsons))
        status = "OK" if bad == 0 else "PARTIAL"
        print(f"{status:7s} {table:44s} {fn:22s} records={len(recs):7d} json={jsons:5d} bad={bad}")

    report = os.path.join(args.out, "_TEXT_REPORT.tsv")
    with open(report, "w", encoding="utf-8") as fh:
        fh.write("file\ttoken\ttable\tversion\trecords\tbad\tjson\n")
        for r in rows:
            fh.write("\t".join(str(x) for x in r) + "\n")

    produced = {r[2] + ".ndjson" for r in rows}
    for stale in os.listdir(args.out):
        if stale.endswith(".ndjson") and stale not in produced:
            os.remove(os.path.join(args.out, stale))

    print(f"\nfiles decoded    : {len(rows)}/{len(files)}")
    if bad_files:
        print(f"files skipped    : {len(bad_files)}")
        for fn, why in bad_files[:10]:
            print(f"  {fn}: {why}")
        if len(bad_files) > 10:
            print(f"  ... and {len(bad_files) - 10} more")
    print(f"total records    : {sum(r[4] for r in rows):,}")
    print(f"  of which JSON  : {json_total:,}")
    print(f"empty-key lines  : {bad_total}")
    print(f"unnamed tables   : {len(unknown_names)} {unknown_names}")
    print(f"report           : {report}")

    if args.verify:
        print("\n--- expected vs decoded ---")
        fails = empty_keys = 0
        for p in sorted(glob.glob(os.path.join(args.out, "*.ndjson"))):
            with open(p, encoding="utf-8") as fh:
                for ln in fh:
                    if not ln.strip():
                        continue
                    o = json.loads(ln)
                    if "j" not in o and not o.get("key"):
                        empty_keys += 1
        print(f"{'records with an empty key and no JSON':44s} violations={empty_keys}")
        print("verify: " + ("PASS" if empty_keys == 0 else "FAIL"))
    if not rows:
        print("no text table could be decoded -- check the client tree and the key manifest", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
