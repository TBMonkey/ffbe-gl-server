#!/usr/bin/env python3
"""wire_probe.py — one real encrypted request/response against a running server.

**Why this exists.** The regression golden and the audit replay replies *in-process* (no wire cipher), and
the bundle smoke used to check only that the engine starts and reports its crypto backend. That left the one
thing a release bundle actually swaps -- the AES path -- verified only by hand: the 2026-10-16 audit found
that a request-time crypto regression would pass the build (finding F1). This is that hand probe, committed.

It builds a real request envelope for a known class, encrypts it the way the client does (manual pad + the
library's own pad = `double_pad`), POSTs over TLS verified against the lab CA, then decrypts the reply and
asserts it is a non-empty JSON object.

**What it proves:** the full serving path answers -- endpoint dispatch, request decrypt, reply selection,
reply encrypt -- on whichever engine is listening, and (with `PATH` stripped) that no external `openssl`
binary was needed to do it.
**What it does not prove:** reply CONTENT (the goldens own that), or that the client is happy (a run does).

Usage:
    python3 harness/wire_probe.py --port 27400
    python3 harness/wire_probe.py --port 27400 --class GameSettingRequest --ca harness/certs/ca.pem

Exit 0 = HTTP 200 and a decrypted non-empty JSON reply; 1 = anything else, with the reason on stderr.
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.request

WORK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(WORK, "server"))

import ffbe_crypto as c  # noqa: E402

URL_PREFIX = "/lapisProd/app/php/gme/actionSymbol"


def _unpad_twice(raw: bytes) -> bytes:
    """A reply carries a manual PKCS7 pad inside a further full block (ffbe_crypto.cbc_encrypt, double_pad);
    `cbc_decrypt` strips the outer one, this strips the inner."""
    pad = raw[-1] if raw else 0
    if 1 <= pad <= 16 and raw[-pad:] == bytes([pad]) * pad:
        return raw[:-pad]
    return raw


def probe(host: str, port: int, ca: str, endpoints: str, cls: str, timeout: float) -> int:
    table = json.load(open(endpoints, encoding="utf-8"))["by_class"]
    if cls not in table:
        print(f"FAIL — {cls} is not in {endpoints}", file=sys.stderr)
        return 1
    ep = table[cls]
    key = ep["encode_key"]
    plain = json.dumps({"LhVz6aD2": [{"9Tbns0eI": "100000001", "9qh17ZUf": "Rain"}]}).encode()
    envelope = json.dumps({
        "TEAYk6R1": {"z5hB3P01": ep["request_id"], "ytHoz4E2": str(int(time.time() * 1000)),
                     "9K0Pzcpd": "1166"},
        "t7n6cVWf": {"qrVcDe48": c.cbc_encrypt(c.key16(key), plain, double_pad=True)},
    }).encode()
    url = f"https://{host}:{port}{URL_PREFIX}/{ep['url_token']}.php"
    ctx = ssl.create_default_context(cafile=ca)
    req = urllib.request.Request(url, data=envelope, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:
        raw, status = r.read(), r.status
    if status != 200:
        print(f"FAIL — HTTP {status} for {cls}", file=sys.stderr)
        return 1
    inner = json.loads(raw)["t7n6cVWf"]
    inner = inner[0] if isinstance(inner, list) else inner
    reply = json.loads(_unpad_twice(c.cbc_decrypt(c.key16(key), inner["qrVcDe48"])).decode())
    if not isinstance(reply, dict) or not reply:
        print(f"FAIL — {cls} reply is not a non-empty object", file=sys.stderr)
        return 1
    print(f"PASS — {cls} HTTP {status}, envelope {len(raw)} B, reply {len(reply)} keys, "
          f"crypto backend {c.backend()}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="one encrypted request/response against a running server")
    ap.add_argument("--host", default="localhost", help="host name the cert must carry (default localhost)")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--ca", default=os.path.join(WORK, "harness", "certs", "ca.pem"))
    ap.add_argument("--endpoints", default=os.path.join(WORK, "out", "endpoints.json"))
    ap.add_argument("--class", dest="cls", default="GameSettingRequest")
    ap.add_argument("--timeout", type=float, default=10.0)
    a = ap.parse_args()
    try:
        return probe(a.host, a.port, a.ca, a.endpoints, a.cls, a.timeout)
    except Exception as e:  # noqa: BLE001 - the reason is the product here
        print(f"FAIL — {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
