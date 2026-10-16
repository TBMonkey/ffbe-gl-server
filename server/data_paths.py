#!/usr/bin/env python3
"""data_paths.py — the ONE place the server resolves where its data lives (the data-root contract).

The server must be able to run from an arbitrary data directory, outside the ffbe checkout. `FFBE_DATA`
(or `--data DIR`, which wins over the env) relocates the whole layout:

    <root>/mst_decoded/                       decoded MST ndjson
    <root>/text_decoded/                      localized text ndjson
    <root>/client/files/                      the CDN root served to the client
    <root>/client/shared_prefs/gamedata.xml   the MST version registry
    <root>/cpk_extract/                       extracted art (banner / area / gacha by basename)
    <root>/overlay/                           the CDN overlay (patched MSTs + fetched packs)
    <root>/certs/                             leaf.pem / leaf.key
    <root>/state/                             state.json, per-slot <name>.json, requests/

With NO root set every path below is the exact one the server used before this contract existed, so an
unset knob is behaviour-preserving. Per key the precedence is: explicit env override (the existing ones
keep their exact semantics; `MST_DIR`/`TEXT_DIR`/`ART_ROOT` are new) > root-relative > checkout-relative
legacy default. This module only READS the environment at import time; it never mutates or probes the
filesystem. Import order matters: `capture_server` pre-parses `--data`/`--maxroster` and exports
`FFBE_DATA`/`SERVE_NV_EX` BEFORE the first server module import, because the modules bind these paths at
their own import time.
"""
from __future__ import annotations

import os

# --- version -----------------------------------------------------------------------------------------
# The engine's own version, and the number of the data-path/CLI contract this file defines. The app
# (server_app/) reads both from --print-paths and refuses to guess: a missing 'contract' line means the
# engine predates this file; a smaller number than the app expects is an app/engine mismatch worth a
# warning, not a crash. Bump ENGINE_VERSION with any served-behaviour change, CONTRACT_VERSION only
# when the layout or the CLI below changes shape.
ENGINE_VERSION = "0.1.4"
CONTRACT_VERSION = 1

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.dirname(HERE)                 # APK_emulator_work/
LEGACY = os.path.dirname(WORK)               # the ffbe worktree / checkout

PACKAGE = "com.square_enix.android_googleplay.FFBEWW"

DATA_ROOT = os.path.abspath(os.environ["FFBE_DATA"]) if os.environ.get("FFBE_DATA") else ""


def _env(name: str) -> str | None:
    """The env override, or None when unset/empty (empty is treated as unset)."""
    return os.environ.get(name) or None


def _rooted(*parts: str) -> str:
    return os.path.join(DATA_ROOT, *parts)


def _new(env_name: str, *root_parts: str, legacy: str) -> str:
    """A path with a NEW env override: `ENV > <root>/... > legacy`."""
    raw = _env(env_name)
    if raw:
        return os.path.abspath(raw)
    return _rooted(*root_parts) if DATA_ROOT else legacy


def _existing(env_name: str, *root_parts: str, legacy: str) -> str:
    """A path with an EXISTING env override: the env value is used verbatim, its old semantics."""
    raw = _env(env_name)
    if raw:
        return raw
    return _rooted(*root_parts) if DATA_ROOT else legacy


# --- decoded data -----------------------------------------------------------------------------------
MST_DIR = _new("MST_DIR", "mst_decoded", legacy=os.path.join(LEGACY, "mst_decoded"))
TEXT_DIR = _new("TEXT_DIR", "text_decoded", legacy=os.path.join(LEGACY, "text_decoded"))
ART_ROOT = _new("ART_ROOT", "cpk_extract", legacy=os.path.join(LEGACY, "cpk_extract"))

# --- the client's own tree --------------------------------------------------------------------------
# GAME_FILES is the APK dump's `files/` (where the shipped MST/resource tree lives); it is NOT the
# CDN_ROOT override -- the marquee cache etc. read the game tree even when the serving root is redirected.
GAME_FILES = _rooted("client", "files") if DATA_ROOT else os.path.join(LEGACY, PACKAGE, "files")
CDN_ROOT = _existing("CDN_ROOT", "client", "files", legacy=GAME_FILES)
CDN_OVERLAY = _existing("CDN_OVERLAY", "overlay", legacy=os.path.join(WORK, "out", "cdn_overlay"))
VERSION_TABLE_XML = os.path.normpath(_existing(
    "VERSION_TABLE_XML", "client", "shared_prefs", "gamedata.xml",
    legacy=os.path.join(LEGACY, PACKAGE, "shared_prefs", "gamedata.xml")))

# --- state ------------------------------------------------------------------------------------------
STATE_DIR = _rooted("state") if DATA_ROOT else os.path.join(WORK, "out", "state")
DEFAULT_STATE_FILE = _rooted("state", "state.json") if DATA_ROOT else os.path.join(WORK, "out", "state.json")
STATE_FILE = _env("STATE_FILE") or DEFAULT_STATE_FILE
REQUESTS_DIR = _env("OUT_REQUESTS") or (_rooted("state", "requests") if DATA_ROOT
                                        else os.path.join(WORK, "out", "requests"))

# --- the generated dispatch table --------------------------------------------------------------------
# Generated by harness/build_endpoints.py from the contract, not a data-root corpus: it belongs to the
# ENGINE. In a checkout it lives in out/; a frozen bundle ships it beside the frozen modules
# (server_app/build_dist.sh adds it), so prefer whichever exists.
ENDPOINTS_FILE = _env("ENDPOINTS_FILE") or (
    os.path.join(WORK, "out", "endpoints.json")
    if os.path.isfile(os.path.join(WORK, "out", "endpoints.json"))
    else os.path.join(HERE, "endpoints.json"))

# --- certificates -----------------------------------------------------------------------------------
CERT_DIR = _rooted("certs") if DATA_ROOT else os.path.join(WORK, "harness", "certs")

# --- optional corpora the server reads when present -------------------------------------------------
# The community gacha art (harness/fetch_gacha_community.py; gitignored).
COMMUNITY_ART_ROOT = _rooted("gacha_community", "art") if DATA_ROOT \
    else os.path.join(LEGACY, "gacha_community", "art")
# The JP/Memorial battle oracle. OPTIONAL at serve time: the battle index ships in `server/content/battle/`.
ORACLE_DIR = _rooted("jp_battle_oracle", "decoded") if DATA_ROOT \
    else os.path.join(LEGACY, "jp_battle_oracle", "decoded")


__all__ = [
    "ENGINE_VERSION",
    "ENDPOINTS_FILE",
    "CONTRACT_VERSION",
    "DATA_ROOT",
    "MST_DIR",
    "TEXT_DIR",
    "ART_ROOT",
    "GAME_FILES",
    "CDN_ROOT",
    "CDN_OVERLAY",
    "VERSION_TABLE_XML",
    "STATE_DIR",
    "DEFAULT_STATE_FILE",
    "STATE_FILE",
    "REQUESTS_DIR",
    "CERT_DIR",
    "COMMUNITY_ART_ROOT",
    "ORACLE_DIR",
]
