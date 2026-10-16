#!/usr/bin/env python3
"""crypto_backend_check.py — prove the two AES backends produce identical bytes.

**Why.** 2026-10-16 `server/ffbe_crypto.py` started preferring the `cryptography` library (which the release
bundle ships) over the `openssl` CLI, because a Windows machine has no openssl and a process spawn per
request is the wrong shape for a product. That swap is only safe if the library reproduces the CLI **byte for
byte** -- including the wire's double-pad, where a one-block difference makes every reply undecodable. The
backend is chosen at import, so the two are compared in two child processes, not in one.

It also covers `aes_ecb_decrypt`, the mode the MST/text decoders use -- the same backend question, one
implementation.

**What it does not prove.** That `cryptography`'s AES is correct in the abstract (that is their test suite);
only that it agrees with the CLI on these vectors. And when `cryptography` is not importable by the chosen
interpreter the pair is NOT compared: that is a loud SKIP by default, a FAIL under `--require-lib` (used by
the bundle build, where the library must be present).

Usage:
    build/venv/bin/python harness/crypto_backend_check.py        # the interpreter that has cryptography
    python3 harness/crypto_backend_check.py --require-lib        # fail if it cannot compare
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.dirname(HERE)
SERVER = os.path.join(WORK, "server")

_CHILD = r'''
import base64, hashlib, json, os, sys
sys.path.insert(0, os.environ["FFBE_SERVER_DIR"])
import ffbe_crypto as c

KEY_A = bytes.fromhex("00112233445566778899aabbccddeeff")
KEY_B = bytes.fromhex("ffeeddccbbaa99887766554433221100")
PLAINS = ["", "A", "x" * 15, "y" * 16, "z" * 17, "{\"a\":1,\"b\":[1,2,3]}" * 40]
ECB_IN = hashlib.sha256(b"ecb-input").digest()  # 32 bytes, deterministic
out = []
for key, pt in ((KEY_A, PLAINS[0]), (KEY_A, PLAINS[1]), (KEY_B, PLAINS[2]), (KEY_A, PLAINS[3]),
                (KEY_A, PLAINS[4]), (KEY_B, PLAINS[5])):
    ct = c.cbc_encrypt(key, pt.encode())
    ct_dp = c.cbc_encrypt(key, pt.encode(), double_pad=True)
    out.append({
        "len": len(pt), "ct": ct, "ct_dp": ct_dp,
        "back": c.cbc_decrypt(key, ct).decode(),
        "back_dp": c.cbc_decrypt(key, ct_dp).decode(),
        "ecb": c.aes_ecb_decrypt(KEY_A, ECB_IN).hex(),
    })
print(json.dumps({"backend": c.backend(), "vectors": out}))
'''


def _run(backend: str, python: str) -> tuple[dict | None, str]:
    env = dict(os.environ, FFBE_CRYPTO=backend, FFBE_SERVER_DIR=SERVER)
    p = subprocess.run([python, "-c", _CHILD], capture_output=True, text=True, env=env)
    if p.returncode != 0:
        tail = (p.stderr.strip().splitlines() or ["(no stderr)"])[-1]
        return None, tail
    return json.loads(p.stdout), ""


def main() -> int:
    ap = argparse.ArgumentParser(description="compare the openssl-CLI and cryptography AES backends")
    ap.add_argument("--python", default=sys.executable, help="interpreter to test with (default: this one)")
    ap.add_argument("--require-lib", action="store_true",
                    help="FAIL instead of SKIP when cryptography is not importable")
    args = ap.parse_args()

    cli, err = _run("openssl", args.python)
    if cli is None:
        print(f"FAIL — the openssl backend could not run: {err}")
        return 1
    lib, err = _run("cryptography", args.python)
    if lib is None:
        if args.require_lib:
            print(f"FAIL — cryptography is not importable by {args.python}: {err}")
            return 1
        print(f"SKIP — cryptography not importable by {args.python} ({err})")
        print("       the two backends were NOT compared; install cryptography or use the build venv")
        return 0

    if cli["backend"] != "openssl" or lib["backend"] != "cryptography":
        print(f"FAIL — backend selection wrong: openssl child says {cli['backend']!r}, "
              f"cryptography child says {lib['backend']!r}")
        return 1

    bad = 0
    for i, (a, b) in enumerate(zip(cli["vectors"], lib["vectors"])):
        for field in ("ct", "ct_dp", "back", "back_dp", "ecb"):
            if a[field] != b[field]:
                bad += 1
                print(f"FAIL — vector {i} (len {a['len']}) field {field} differs:\n"
                      f"       openssl     : {a[field][:80]}\n       cryptography: {b[field][:80]}")
    for name, data in (("openssl", cli), ("cryptography", lib)):
        for i, v in enumerate(data["vectors"]):
            pt_len = v["len"]
            if v["back"] != "" and len(v["back"]) != pt_len:
                print(f"FAIL — {name} round-trip length wrong on vector {i}: {len(v['back'])} != {pt_len}")
                bad += 1
    if bad:
        return 1
    print(f"PASS — openssl CLI and cryptography agree on {len(cli['vectors'])} CBC vectors "
          f"(single- and double-pad) + ECB, round-trips exact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
