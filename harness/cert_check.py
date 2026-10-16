#!/usr/bin/env python3
"""cert_check.py — generate the lab certs in-process and verify shape, chain and the Android filename.

**Why.** The product path cannot shell out to `openssl` (Windows has none), so `server/ffbe_certs.py`
generates the CA and leaf with `cryptography`. Three properties have to be proven, because each fails
differently:

  * **the chain** -- a bad signature, CA flag or keyUsage leaves the guest rejecting the leaf at connect
    time;
  * **the SAN** -- the default HostnameVerifier needs the exact host, and a missing SAN looks like "the
    server is up but the client cannot reach it";
  * **`subject_hash_old`** -- it is the FILENAME the CA must have in /system/etc/security/cacerts/; a wrong
    hash installs an anchor the guest never consults, and nothing else in the stack would say so.

The chain and SAN are checked with `cryptography` itself, so they run anywhere. The hash is compared against
`openssl x509 -subject_hash_old` when an openssl binary is present; when it is not, the check prints exactly
what it could not prove instead of passing silently.

Usage: python3 harness/cert_check.py [--out DIR] [--keep]
"""
from __future__ import annotations

import argparse
import os
import shutil
import ssl
import subprocess
import sys
import tempfile

WORK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(WORK, "server"))


def main() -> int:
    ap = argparse.ArgumentParser(description="generate + verify the lab certs")
    ap.add_argument("--out", default=None, help="output dir (default: a temp dir, removed unless --keep)")
    ap.add_argument("--keep", action="store_true", help="keep the temp dir and print where it is")
    args = ap.parse_args()

    try:
        import ffbe_certs
    except ImportError as e:
        print(f"FAIL — cannot import server/ffbe_certs.py ({e}); it needs the `cryptography` package")
        return 1

    tmp = args.out or tempfile.mkdtemp(prefix="ffbe-cert-check-")
    problems: list[str] = []
    try:
        info = ffbe_certs.generate_certs(tmp, force=True)

        # 1. Python's own ssl must load the pair -- that is literally what the server does.
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(info["leaf_pem"], info["leaf_key"])
            print(f"OK   python ssl loads {os.path.basename(info['leaf_pem'])} + key")
        except Exception as e:  # noqa: BLE001
            problems.append(f"python ssl rejected the pair: {e}")

        # 2. chain, SAN and the CA flags, in-process (no openssl needed).
        from cryptography import x509

        ca = x509.load_pem_x509_certificate(open(info["ca_pem"], "rb").read())
        leaf = x509.load_pem_x509_certificate(open(info["leaf_pem"], "rb").read())
        if hasattr(leaf, "verify_directly_issued_by"):
            leaf.verify_directly_issued_by(ca)
            print("OK   leaf verifies directly against the CA")
        else:
            # A gate that cannot verify the chain must FAIL, not pass with a note (audit F5, 2026-10-16).
            problems.append("cryptography is too old for verify_directly_issued_by; chain NOT verified")
        # cryptography changed get_values_for_type(DNSName) from objects to str across versions; accept both.
        san_ext = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = [getattr(n, "value", n) for n in san_ext.get_values_for_type(x509.DNSName)]
        if info["host"] not in names:
            problems.append(f"SAN missing {info['host']}: {names}")
        else:
            print(f"OK   SAN contains {info['host']} ({len(names)} names)")
        if leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
            problems.append("leaf carries CA:TRUE")
        if not ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
            problems.append("root lacks CA:TRUE")

        # 3. the Android cacerts filename, against openssl when the binary exists.
        ours = ffbe_certs.subject_hash_old(ca)
        if shutil.which("openssl"):
            ref = subprocess.run(
                ["openssl", "x509", "-in", info["ca_pem"], "-noout", "-subject_hash_old"],
                capture_output=True, text=True).stdout.strip()
            if ref != ours:
                problems.append(f"subject_hash_old mismatch: ours {ours}, openssl {ref}")
            else:
                print(f"OK   subject_hash_old {ours} matches openssl ({ours}.0 is the cacerts name)")
            v = subprocess.run(
                ["openssl", "verify", "-CAfile", info["ca_pem"], info["leaf_pem"]],
                capture_output=True, text=True)
            if "OK" not in v.stdout:
                problems.append(f"openssl verify: {v.stdout.strip() or v.stderr.strip()}")
            else:
                print("OK   openssl verify -CAfile passes")
        else:
            print("NOTE openssl not on PATH: the hash was NOT compared against the CLI and `openssl verify` "
                  "did not run; the in-process chain/SAN checks above are all that is proven here")

        if problems:
            for p in problems:
                print(f"FAIL — {p}")
            return 1
        print(f"PASS — certs generated and verified in {tmp}")
        return 0
    finally:
        if not args.out and not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
        elif not args.out:
            print(f"kept {tmp}")


if __name__ == "__main__":
    raise SystemExit(main())
