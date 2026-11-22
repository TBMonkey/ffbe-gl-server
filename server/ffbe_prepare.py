#!/usr/bin/env python3
"""ffbe_prepare.py — build the whole data root from the user's TWO inputs, inside the engine.

WHY THIS EXISTS
---------------
A user has two things: **the client APK** and **their device's app-data dump**. Everything else the server
reads is derived from them, and until 2026-10-24 we made the user assemble that from a checkout with two
repo scripts and an `openssl` binary (`docs/SIMPLER_DISTRIBUTABLE.md`, APP-50/APP-51/APP-57). This module is
the one command that replaces that: it extracts `libgame.so` from the APK, decodes the MST and localized-text
tables, generates the CA + leaf, points the engine at the user's client tree and writes `prepare.json`.

THE CONTRACT (what it promises, and what it refuses)
----------------------------------------------------
* **Inputs only.** `--apk` and `--client-data`. `--client-data` may be the app-data root
  (`<...>/com.square_enix.android_googleplay.FFBEWW`) or that root's `files/` directory — both are accepted
  because both are what a user actually has.
* **No repo, no openssl, no user-run Python.** The decoders are loaded from wherever they ship (the bundle
  puts them beside the engine; a checkout keeps them under `scripts/`) and they use `cryptography` when
  available. `harness/` is never referenced: that is our dev tooling, not the user's.
* **Idempotent and disposable.** Re-running rewrites the decoded trees; deleting the data root and running
  again reproduces it.
* **Fails with a sentence, never a traceback**, and names the command to run.

`prepare.json` is the record the app's doctor reads: the two input paths with their sha256, the decoded
counts, and the engine's own version — so "is this prepared?" is a file check, not a filesystem taxonomy
quiz (`data_paths.py`'s nine directories stay an implementation detail).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.dirname(HERE)                 # APK_emulator_work/
LEGACY = os.path.dirname(WORK)               # the checkout root (or the bundle's parent)
PACKAGE = "com.square_enix.android_googleplay.FFBEWW"
LANG = "en"


class PrepareError(Exception):
    """A user-facing failure: the message is the whole story, and it names the fix."""


# --- locating the shipped pieces ----------------------------------------------------------------
def _first(paths):
    for p in paths:
        if p and os.path.exists(p):
            return p
    return ""


def find_decoder(name: str) -> str:
    """`mst_decode.py` / `text_decode.py`, wherever this build ships them.

    The bundle puts them beside the frozen engine; a checkout keeps them in `scripts/`. Both are tried, in
    that order, so the same code path serves a developer and a user.
    """
    return _first([
        os.path.join(HERE, f"{name}.py"),                       # bundled beside the engine
        os.path.join(WORK, "scripts", f"{name}.py"),            # checkout
        os.path.join(LEGACY, "scripts", f"{name}.py"),           # checkout, other data root
    ])


def find_keys_manifest() -> str:
    return _first([
        os.path.join(HERE, "localized_text_keys.json"),          # bundled beside the engine
        os.path.join(LEGACY, "docs", "analysis", "localized_text_keys.json"),
        os.path.join(WORK, "docs", "analysis", "localized_text_keys.json"),
    ])


def _load_module(name: str, path: str):
    spec = importlib.util.spec_from_file_location(f"_ffbe_{name}", path)
    if spec is None or spec.loader is None:
        raise PrepareError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- resolving the inputs -----------------------------------------------------------------------
def resolve_client_tree(client_data: str) -> dict:
    """Accept the app-data root or its `files/` dir; return the paths the engine consumes."""
    root = os.path.abspath(client_data)
    if not os.path.isdir(root):
        raise PrepareError(f"--client-data is not a directory: {root}")
    # `<root>/files` (the app-data root) or `<root>` itself (already the files tree)
    files = os.path.join(root, "files") if os.path.isdir(os.path.join(root, "files")) else root
    mst = os.path.join(files, "mst")
    text = os.path.join(files, "localized_texts", LANG)
    prefs = os.path.join(os.path.dirname(files), "shared_prefs", "gamedata.xml")
    if not os.path.isdir(mst):
        raise PrepareError(f"no `mst/` under {files} — point --client-data at the app-data root "
                           f"(`.../{PACKAGE}`) or its `files/` directory")
    if not os.path.isdir(text):
        raise PrepareError(f"no `localized_texts/{LANG}/` under {files} — is the client language set to "
                           f"English? (pass a different tree, or the tables will not decode)")
    return {"root": root, "files": files, "mst": mst, "text": text,
            "gamedata": prefs if os.path.isfile(prefs) else ""}


def extract_libgame(apk: str, dest: str) -> str:
    """The APK's own `libgame.so` — the MST tokens + keys + cipher live in its `.rodata`."""
    if not os.path.isfile(apk):
        raise PrepareError(f"--apk is not a file: {apk}")
    try:
        with zipfile.ZipFile(apk) as z:
            names = [n for n in z.namelist() if n.startswith("lib/") and n.endswith("/libgame.so")]
            if not names:
                raise PrepareError(f"no `lib/*/libgame.so` in {os.path.basename(apk)} — is this the FFBEWW APK?")
            # prefer arm64, else whatever exists (the .rodata manifest is the same in both)
            names.sort(key=lambda n: (0 if "arm64" in n else 1, n))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with z.open(names[0]) as src, open(dest, "wb") as out:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
    except zipfile.BadZipFile:
        raise PrepareError(f"{os.path.basename(apk)} is not a readable APK/zip")
    return dest


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --- the steps ----------------------------------------------------------------------------------
def check_apk(apk: str) -> None:
    """Fail before anything is written if the APK is not the one we need."""
    if not os.path.isfile(apk):
        raise PrepareError(f"--apk is not a file: {apk}")
    try:
        with zipfile.ZipFile(apk) as z:
            if not any(n.startswith("lib/") and n.endswith("/libgame.so") for n in z.namelist()):
                raise PrepareError(f"no `lib/*/libgame.so` in {os.path.basename(apk)} — is this the FFBEWW APK?")
    except zipfile.BadZipFile:
        raise PrepareError(f"{os.path.basename(apk)} is not a readable APK/zip")


def prepare(apk: str, client_data: str, data_root: str) -> dict:
    # VALIDATE FIRST, CREATE SECOND: a wrong path used to leave an empty data root behind, which then looked
    # like a half-prepared install to the doctor (found 2026-10-24 by the bundle smoke writing `never/`).
    tree = resolve_client_tree(client_data)
    check_apk(apk)
    data_root = os.path.abspath(data_root or os.environ.get("FFBE_DATA") or "ffbe-data")
    os.makedirs(data_root, exist_ok=True)
    print(f"prepare: data root {data_root}")
    print(f"          client tree {tree['files']}")

    libgame = extract_libgame(apk, os.path.join(data_root, "libgame.so"))
    print(f"          libgame.so  {libgame} ({os.path.getsize(libgame)} B)")

    # 1. MST tables — the engine's own decoders, located wherever this build ships them.
    mst_py = find_decoder("mst_decode")
    if not mst_py:
        raise PrepareError("the MST decoder is missing from this build (`mst_decode.py` beside the engine "
                           "or under `scripts/`) — this is a packaging bug, not something you can fix")
    out_mst = os.path.join(data_root, "mst_decoded")
    print(f"          decoding MST tables from {tree['mst']} ...")
    rc = _load_module("mst_decode", mst_py).main(
        ["--libgame", libgame, "--mst-dir", tree["mst"], "--out", out_mst])
    if rc:
        raise PrepareError(f"MST decode failed (exit {rc}) — see the lines above")

    # 2. Localized text tables.
    txt_py = find_decoder("text_decode")
    keys = find_keys_manifest()
    if not (txt_py and keys):
        raise PrepareError("the text decoder or its key manifest is missing from this build "
                           "(`text_decode.py` + `localized_text_keys.json`) — a packaging bug")
    out_txt = os.path.join(data_root, "text_decoded")
    print("          decoding localized text ...")
    rc = _load_module("text_decode", txt_py).main(
        ["--keys", keys, "--client-data", tree["root"], "--lang", LANG, "--out", out_txt])
    if rc:
        raise PrepareError(f"text decode failed (exit {rc}) — see the lines above")

    # 3. TLS: the CA + leaf the client must trust. Generated in-process (no openssl, no shell script).
    cert_dir = os.path.join(data_root, "certs")
    try:
        import ffbe_certs  # noqa: PLC0415 — same package, imported late so `--print-paths` stays cheap
        ffbe_certs.generate_certs(cert_dir)
        print(f"          certs       {cert_dir}")
    except Exception as exc:                      # noqa: BLE001 - a cert failure must not lose the decode
        print(f"          certs       SKIPPED ({exc})", file=sys.stderr)

    # 4. Point the engine at the user's own client tree (one symlink, never a copy of several GB).
    link = os.path.join(data_root, "client")
    wired = False
    try:
        if os.path.islink(link) or os.path.isfile(link):
            os.remove(link)
        elif os.path.isdir(link):
            raise OSError("a real directory is in the way")
        os.symlink(tree["root"], link, target_is_directory=True)
        wired = True
        print(f"          client      {link} -> {tree['root']}")
    except OSError as exc:
        print(f"          client      NOT linked ({exc}); pass these on the command line:\n"
              f"                      --cdn-root {tree['files']}\n"
              f"                      --version-table-xml {tree['gamedata'] or '<dump>/shared_prefs/gamedata.xml'}",
              file=sys.stderr)

    # 5. The record the doctor reads.
    counts = {k: len([f for f in os.listdir(os.path.join(data_root, k)) if f.endswith(".ndjson")])
              for k in ("mst_decoded", "text_decoded")}
    manifest = {
        "_comment": "written by `ffbe-server prepare`; the doctor reads this instead of guessing",
        "engine_version": _engine_version(),
        "inputs": {
            "apk": {"path": os.path.abspath(apk), "sha256": _sha256(apk)},
            "client_data": {"path": tree["root"], "files": tree["files"],
                            "gamedata_xml": tree["gamedata"],
                            "gamedata_sha256": _sha256(tree["gamedata"]) if tree["gamedata"] else ""},
        },
        "decoded": counts,
        "libgame_sha256": _sha256(libgame),
        "client_linked": wired,
        "lang": LANG,
    }
    with open(os.path.join(data_root, "prepare.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, sort_keys=True)
        fh.write("\n")
    print(f"          record      {os.path.join(data_root, 'prepare.json')}")
    print(f"\nprepared: {counts['mst_decoded']} MST tables, {counts['text_decoded']} text tables")
    print(f"next:     ffbe-server-app start --data {data_root}")
    return manifest


def _engine_version() -> str:
    try:
        import data_paths  # noqa: PLC0415
        return data_paths.ENGINE_VERSION
    except Exception:                              # noqa: BLE001
        return "unknown"


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="ffbe-server prepare", description=__doc__.splitlines()[0])
    ap.add_argument("--apk", required=True, help="the FFBEWW APK (its libgame.so carries the table keys)")
    ap.add_argument("--client-data", required=True,
                    help="the app-data root of your client install, or its `files/` directory")
    ap.add_argument("--data", default=os.environ.get("FFBE_DATA") or "ffbe-data",
                    help="where to build the data root (default: $FFBE_DATA or ./ffbe-data)")
    a = ap.parse_args(argv)
    try:
        prepare(a.apk, a.client_data, a.data)
    except PrepareError as exc:
        print(f"prepare: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
