#!/usr/bin/env python3
"""build_endpoints.py — derive the server's routing table from the contract's §3 appendix.

One fact, one home: `docs/analysis/FFBE_SERVER_CONTRACT.md` §3 owns the
Request-class → requestID → encodeKey → URL map. This script does NOT copy it into the
repo; it parses that table on demand and writes a machine-readable view to the gitignored
out/ dir (`APK_emulator_work/out/endpoints.json`), which the capture server reads.

Usage:
    python3 harness/build_endpoints.py [-o out/endpoints.json]

Verification: prints the row count; the contract states 222 classes / 211 distinct URLs.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict

def _find_contract() -> str:
    """The contract's home, found by walking up from this script.

    A fixed `../..` assumed the harness sits two levels under the git root -- true in the workspace, false in
    a standalone export, which is where it broke (2026-10-16, the first export). The first ancestor holding
    the file wins; `FFBE_CONTRACT` overrides the search.
    """
    if os.environ.get("FFBE_CONTRACT"):
        return os.path.abspath(os.environ["FFBE_CONTRACT"])
    cur = os.path.dirname(os.path.abspath(__file__))
    while True:
        cand = os.path.join(cur, "docs", "analysis", "FFBE_SERVER_CONTRACT.md")
        if os.path.isfile(cand):
            return cand
        parent = os.path.dirname(cur)
        if parent == cur:
            return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "docs", "analysis", "FFBE_SERVER_CONTRACT.md")
        cur = parent


CONTRACT = _find_contract()
DEFAULT_OUT = os.path.join(os.path.dirname(__file__), "..", "out", "endpoints.json")

ROW = re.compile(
    r"^\|\s*`([A-Za-z0-9_]+)`\s*\|\s*`([A-Za-z0-9]{8})`\s*\|\s*`([A-Za-z0-9]{8})`\s*\|"
    r"\s*`(/actionSymbol/[A-Za-z0-9]+\.php)`\s*\|"
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    if not os.path.isfile(CONTRACT):
        print(f"contract not found at {CONTRACT} (set FFBE_CONTRACT, or run from a checkout)",
              file=sys.stderr)
        return 2
    text = open(CONTRACT, encoding="utf-8").read()
    by_class: dict[str, dict] = {}
    by_url: dict[str, list[str]] = defaultdict(list)
    for line in text.splitlines():
        m = ROW.match(line)
        if not m:
            continue
        cls, req_id, enc_key, url = m.groups()
        token = url.rsplit("/", 1)[1][: -len(".php")]
        by_class[cls] = {"request_id": req_id, "encode_key": enc_key, "url": url, "url_token": token}
        by_url[token].append(cls)

    rows = len(by_class)
    distinct = len(by_url)
    print(f"parsed {rows} request classes -> {distinct} distinct URLs")
    if rows != 222 or distinct != 211:
        print(f"WARNING: contract states 222 classes / 211 URLs; got {rows} / {distinct}", file=sys.stderr)

    doc = {
        "source": "docs/analysis/FFBE_SERVER_CONTRACT.md §3 (generated; do not hand-edit)",
        "classes": rows,
        "distinct_urls": distinct,
        "by_class": by_class,
        # url token -> classes; >1 means the URL is shared and the body group decides dispatch
        "by_url_token": {t: cls for t, cls in sorted(by_url.items())},
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    shared = {t: c for t, c in by_url.items() if len(c) > 1}
    print(f"shared URLs: {len(shared)} {sorted(shared)}")
    print(f"wrote {os.path.relpath(args.out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
