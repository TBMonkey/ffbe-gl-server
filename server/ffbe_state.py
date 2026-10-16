#!/usr/bin/env python3
"""ffbe_state.py — the things that CHANGE while the server runs.

Extracted 2026-09-26 from `capture_server.py` (docs/SERVER_REFACTOR_PLAN.md, Phase 3). It is the module
that makes the rest of the split possible, and it carries a trap worth stating before anything reads it.

**THE TRAP: these are rebound, so `from ffbe_state import X` would be a stale snapshot.** `main()`
reassigns `STATE_FILE`, `STATE_MODE`, `SEASON` and the rest at startup, and `reload_season_if_changed`
reassigns `SEASON`/`SEASON_MTIME`/`GACHA_CAMPAIGN` on every hot reload. A reader that did
`from ffbe_state import SEASON` would capture the value at import and then keep serving the OLD one
forever, with no error anywhere -- the season would simply appear not to hot-reload, which is the exact
bug the reload exists to prevent. **Every reader outside this module must use attribute access
(`st.SEASON`), never a name import.** That is why the readers left in `capture_server` are written `st.X`,
and it is the one place in this split where the text is NOT a pure move. The golden is what proves it
stayed correct.

This module also owns the two cross-transform channels the audit found: `LAST_DROPS` (the per-mission drop
roll, keyed by mission since 2026-09-26) and `NOTES` -- a transform that cannot parse its input must not
no-op silently, or a default is indistinguishable from a real result (idiom C24). Both are process-wide, so
neither belongs to any one transform.

`State` is the one name here that is never rebound, so `from ffbe_state import State` is safe, and that is
what the transform modules will do.
"""
from __future__ import annotations

import json
import os
import time

import data_paths   # the data-root contract (`FFBE_DATA` / `--data`); see its docstring

from ffbe_config import *          # noqa: F401,F403 -- the constants this module reads and rebinds
import re
import season as season_mod
from player_state import (PlayerState, story_unlock_bits, STORY_UNLOCK_SETS,
                          map_ext_unservable_ignores, switch_position,
                          fixed_beast_presets, BEAST_BOARD_ALL,
                          emblem_mst, emblem_mst_ids,
                          emblem_campaign, emblem_weekly_mst,
                          EMBLEM_WEEKLY_MILESTONE, EMBLEM_WEEKLY_COUNT,
                          EMBLEM_CREST_TYPE, EMBLEM_CREST_ITEM)

# The authored SEASON selector (`server/seasons.json`, `docs/notes/server_seasons.md`). A season is a
# SELECTION over levers that already exist: it sets the gacha campaign, the HOME `merqbE18` badge row, and
# (in memory only) switch bits. `SEASON=<key>` / `--season <key>` picks one; `off`/unset leaves every lever
# at its own default. `SEASON` is assigned in main(); functions read the module global.
SEASON = None

# The roll made for a battle, KEYED BY MISSION ID. `y35YvQjZ` is served at mission LOAD and `09HRWXDf`
# at mission END, so the roll has to survive between two replies or the result screen would disagree with
# the battle. Keyed because the two replies are DIFFERENT REQUESTS and nothing else binds them: measured
# on the served table, 204 entries carry the wave rows, 2 carry the result rows, and 0 carry both -- so an
# unkeyed single slot meant a mission-end could mirror a DIFFERENT battle's drops, i.e. the same request
# answered differently depending on run order, which is exactly what a recorded run must never do.
# Only `roll_drops` writes, and only when the reply is that mission's own per-mission bundle.
LAST_DROPS: dict[str, list] = {}


# A reply transform that cannot parse its input MUST NOT no-op silently: a default is indistinguishable
# from a real result, and two conclusions were drawn from exactly those defaults before it was caught
# (`docs/analysis/RE_VERIFICATION_IDIOMS.md` C24 -- `rstrip("\x01")` failed on 81.0% of recorded
# requests). Transforms record it here and `_handle` prints it as a `note:` line, like `decrypt_error`.
NOTES: list[str] = []


def _note(msg: str) -> None:
    NOTES.append(msg)


# --- condition adjudication -------------------------------------------------------------------------
# The first field of `Pzn5h0Ga` is the condition TYPE. There is no type table in the binary, but the
# shipped table carries every row's OWN description (`G4L0YIB2`), so the mapping is measurable. Counts
# over all 42757 rows that carry a spec:
#   68  クエストをクリア / "Complete the quest"            81% of 9065
#   38  コンティニューせずにクリア / "No continues"         92% of 7726
#   33  誰も戦闘不能になることなくクリア / "no ally KO'd"    88% of 6840
#   34  パーティ人数N人以上 / "party >= N"    threshold = param + 1  (34:0 -> >=1, 34:1 -> >=2, ...)
#    1  "No items" (86% of 5603) · 16 "Use a limit burst" (85% of 849) · 6 "No magic" (96% of 569) --
#       NOT implemented. The client tracks them (`addUseItem`, `addUseLimitBurstCnt`) but the request
#       token carrying each is not identified, and guessing is how this got wrong the first time.
# A type with no evaluator returns None: NOT credited, and reported by name so the gap is visible.
# The derivation, the counts and the commands live in `docs/notes/challenge_conditions.md`.
#
# The inputs come from the CLIENT's own mission-end report (MissionEnd request) and from data we serve:
#   2urzfX6d[0].Z1p0j9uF = DeadCount        (lexicon `DeadCount`)
#   2urzfX6d[0].69ieJGhD = BattleClear      (lexicon `BattleClear`)
#   L4i4418y[0].6q35TL07 = continue count   (`MissionEndRequest::createContinueCountTag()` @0x19a8398)
#   party size                              = counted from the ONE player state (we own the party)


# --- persistence ----------------------------------------------------------------------------------
# The server now holds real state -- which challenges it has paid for, which switches are granted, the lapis
# balance -- and this is where it lives. Deliberately ONE file, under `out/` (already gitignored), because we
# are testing: `python3 server/capture_server.py --wipe` must be the entire reset, `STATE=0` must run
# memoryless, and neither may need anything else. A test run must never be unable to get to a known state.
STATE_FILE = data_paths.STATE_FILE
# The mechanism knobs (docs/notes/player_state_schema.md §1). Set by main(); STATE_FILE is the resolved path
# of the loaded slot (`--state <name>` -> out/state/<name>.json; the default remains out/state.json).
STATE_SLOT = os.environ.get("STATE_SLOT", "default")
STATE_MODE = os.environ.get("STATE_MODE", "live")
STATE_OVERLAY: list = []
STATE_SETS: list = []


class State:
    by_reqid: dict = {}   # Header z5hB3P01 (requestID) -> class entry (unambiguous)
    by_token: dict = {}   # URL token -> [class entries] (10 URLs are shared by 2-3 classes)
    replies: dict = {}    # requestID (or URL token, or "_default") -> decrypted reply body
    omit_header: bool = False  # A/B switch for the reply envelope (see OMIT_HEADER env)
    echo_cipher: bool = False  # A/B: reply with the request's own ciphertext (ECHO_CIPHER env)
    close_conn: bool = False   # A/B: send Connection: close and close the socket (CLOSE_CONN env)
    tls12: bool = False        # A/B: pin TLS 1.2 (TLS12 env)
    envelope: str = "obj"      # A/B: reply envelope shape (ENVELOPE env)
    reply_key: str = "encodekey"  # A/B: which token encrypts the reply (REPLY_KEY env)
    reply_iv: str = "literal"  # A/B: which IV encrypts the reply (REPLY_IV env: literal|zero)
    double_pad: bool = True    # the client double-pads (manual PKCS7 + Java PKCS5Padding); PAD=single to A/B
    cdn_root: str = ""         # where CDN/resource fetches are served from (CDN_ROOT env / --cdn-root)
    cdn_overlay: str = ""      # checked BEFORE cdn_root; holds our authored/patched copies (CDN_OVERLAY env)
    replies_path: str = ""     # the file the bodies came from (recorded for provenance)
    replies_mtime: float = 0.0  # its mtime when last loaded -- see reload_replies_if_changed
    # Where the per-request evidence record is written. `OUT_REQUESTS` exists because this directory is
    # BOTH the run's evidence and the regression gate's INPUT (`harness/server_regression.py` replays it),
    # so a test that starts a real server here silently adds synthetic cases to a corpus that is supposed
    # to be the client's own traffic. It bit twice on 2026-09-26 before the knob existed. Point it at a
    # scratch dir for any test that is not a real run.
    outdir: str = data_paths.REQUESTS_DIR
    requests: int = 0
    # The player. ONE place holds the player (docs/notes/player_state_schema.md); the served groups and the
    # NRG cap are PROJECTIONS of it, never independent literals. Built by load_state().
    player: "PlayerState | None" = None
    # Set by anything that mutates the player, so `_handle` knows to persist it (see save_state).
    state_dirty: bool = False


def load_endpoints(path: str | None) -> tuple[dict, dict]:
    if not path or not os.path.exists(path):
        return {}, {}
    with open(path, encoding="utf-8") as fh:
        by_class = json.load(fh).get("by_class", {})
    by_reqid = {e["request_id"]: {**e, "class": c} for c, e in by_class.items()}
    by_token: dict[str, list] = {}
    for cls, e in by_class.items():
        by_token.setdefault(e["url_token"], []).append({**e, "class": cls})
    return by_reqid, by_token


def endpoint_for(path: str, request_id: str | None) -> tuple[dict | None, str]:
    """Select the request class. The Header requestID is exact; a shared URL is not."""
    if request_id and request_id in State.by_reqid:
        return State.by_reqid[request_id], "reqid"
    token = path.rsplit("/", 1)[-1].removesuffix(".php")
    hits = State.by_token.get(token, [])
    if len(hits) == 1:
        return hits[0], "url"
    if len(hits) > 1:
        return hits[0], f"url-shared({len(hits)}: {[h['class'] for h in hits]})"
    return None, "unknown"


def reload_replies_if_changed() -> None:
    """Re-read the replies file when it changes on disk.

    `State.replies` is loaded ONCE at startup, so editing `responses.json` while the server is up used to be
    invisible: the client kept being served the OLD payload and it read as "my edit did nothing". That is
    exactly what happened on 2026-09-19 -- the `GachaExeRequest` reply was added at 21:15:36 and the client
    re-tested at 21:16:51 against a server started at 21:04:40, so the reply was still empty and the stall was
    blamed on the payload rather than on staleness. One `stat` per request. A file that is mid-edit or invalid
    **keeps the previously loaded payload** and says so, rather than killing the server.
    """
    path = State.replies_path
    if not path:
        return
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return
    if mtime == State.replies_mtime:
        return
    try:
        with open(path, encoding="utf-8") as fh:
            new = {k: v for k, v in json.load(fh).items() if not k.startswith("_")}
    except Exception as exc:  # noqa: BLE001
        print(f"replies: {os.path.relpath(path, WORK)} is not loadable ({exc!r}); KEEPING the "
              f"{len(State.replies)} entries loaded earlier", flush=True)
        State.replies_mtime = mtime   # don't re-attempt the same bad revision on every request
        return
    State.replies = new
    State.replies_mtime = mtime
    print(f"replies: RELOADED {len(new)} keyed entries from {os.path.relpath(path, WORK)}", flush=True)


# The season selector, kept so a hot reload re-resolves the SAME choice. `SEASON_SELECTOR` is the
# `--season`/`SEASON` string (empty = use the file's `default`); `SEASON_MTIME` is `seasons.json`'s mtime at
# the last load, so the first request does not re-load it for nothing.
SEASON_SELECTOR = ""
SEASON_MTIME = 0.0
# When THIS process imported the server package. Paired with `warn_if_source_is_newer` below.
SOURCE_START = time.time()


def warn_if_source_is_newer() -> None:
    """Print a loud line when a `server/*.py` file is NEWER than this process's start.

    **This is the third time this trap has cost a session** (2026-09-22's login calendar, and again
    2026-09-26 twice: a forced table and the whole PANEL QUEST screen, both of which "did nothing" because
    the running process predated the edit). The shape is always the same and it is always silent: JSON data
    hot-reloads (`reload_replies_if_changed`, `reload_season_if_changed`), **code does not** -- Python imported
    the modules at startup, so a new transform is simply not in the pipeline, the request is answered with the
    payload as loaded, and the log shows a perfectly normal `reply:none` or an unserved feature.

    So the check runs where the reloads run (once per request, one `stat` each) and prints only when the
    answer CHANGES, which bounds it to a line or two per process. The remedy is in the message because
    "restart the server" is the whole fix and nobody should have to infer it.
    """
    global _SOURCE_WARNED
    try:
        pkg = os.path.dirname(os.path.abspath(__file__))
        newest, name = max((os.path.getmtime(os.path.join(pkg, f)), f)
                           for f in os.listdir(pkg) if f.endswith(".py"))
    except (OSError, ValueError):
        return
    if newest <= SOURCE_START:
        if _SOURCE_WARNED:
            _SOURCE_WARNED = None                       # arm it again, so a later edit warns too
        return
    if _SOURCE_WARNED == name:
        return
    _SOURCE_WARNED = name
    print(f"STALE SOURCE  {name} was modified at "
          f"{time.strftime('%H:%M:%S', time.localtime(newest))}, AFTER this server started at "
          f"{time.strftime('%H:%M:%S', time.localtime(SOURCE_START))} -- the running process does NOT have "
          f"that change. JSON hot-reloads, CODE does not. Restart the server (or re-run "
          f"`harness/ffbe_run.sh`) or this feature will silently do nothing.", flush=True)


_SOURCE_WARNED = None


def reload_season_if_changed() -> None:
    """Re-read `server/seasons.json` when it changes, the way `responses.json` is re-read.

    The season is resolved at startup, so editing a season's badges (or the file's `default`) while the
    server is up used to be invisible -- the same trap `reload_replies_if_changed` exists to close. One
    `stat` per request. An unreadable file keeps the currently active season. The gacha facet follows an
    explicit `GACHA_CAMPAIGN` env, which still wins; forced switch bits are re-merged additively (they are
    never persisted, so this cannot leak into a checkpoint).
    """
    global SEASON, SEASON_MTIME, GACHA_CAMPAIGN
    try:
        mtime = os.path.getmtime(season_mod.SEASONS_FILE)
    except OSError:
        return
    if mtime == SEASON_MTIME:
        return
    new = season_mod.load(SEASON_SELECTOR or None)
    # ASSIGN THE SEASON BEFORE THE MTIME. The other order let a thread arriving in the gap read the NEW
    # mtime (so it would not reload) against the OLD season, and serve one season's badges with another's
    # campaign. `getmtime` is the reload trigger, so it must be published last, after the data it guards.
    SEASON = new
    SEASON_MTIME = mtime
    if not os.environ.get("GACHA_CAMPAIGN"):
        # Derived, not owned: when the reloaded season names no campaign, the previous campaign must be
        # dropped, or a season edited on disk keeps serving the campaign it used to have.
        GACHA_CAMPAIGN = (new.gacha_campaign if new is not None else "") or ""
    if new is not None and State.player is not None:
        # Same rule as startup: the reloaded season's bits are volatile -- served, never persisted.
        State.player.add_volatile_switches(new.switch_sets())
    print(f"season: RELOADED {os.path.basename(season_mod.SEASONS_FILE)} -> "
          f"{new.summary() if new else 'none -- every lever at its own default'}", flush=True)


def load_state() -> None:
    """Build the ONE player state from `STATE_FILE`. A v1 file is migrated; anything wrong starts FRESH and
    says so -- a test run must never be blocked by a bad file it can simply delete.

    The mechanism knobs come from main(): slot, mode (`live`/`ephemeral`/`frozen`) and the overlay.
    """
    if not STATE_PERSIST:
        print("state: persistence OFF (STATE=0) -- the slot is not written back", flush=True)
    State.player = PlayerState.load(STATE_FILE, slot=STATE_SLOT, mode=STATE_MODE,
                                    overlay=STATE_OVERLAY, sets=STATE_SETS)
    p = State.player
    # The lapis presets (`--preset max`) seed the balance as a RUN INJECTION, not a save edit: routed
    # through the overlay so the ordinary autosave reverts it and a `--set`/preset can never bake into a
    # slot (schema §1.3). `--state-save` is the one way to keep it.
    for env_key, cur in (("LAPIS_FREE", "lapis_free"), ("LAPIS_PAID", "lapis_paid")):
        if os.environ.get(env_key) is not None:
            p.apply_set(f"currencies.{cur}", os.environ[env_key])
    # A run hook: seed SHIPPED summon tickets (`F_TICKET_MST` group 10010 = important items 160/170/180) so a
    # restored state that predates ticket support can display/pull the ticket-only banner (SW-67).
    if os.environ.get("GACHA_TICKETS"):
        n = int(os.environ["GACHA_TICKETS"])
        n_rows = p.grant_ticket_group(10010, n)
        print(f"tickets: granted {n} each of {n_rows} group-10010 ticket(s)", flush=True)
    # The switch baseline is a run variable; seed it only for types the state does not already track.
    if SWITCH_TYPE_MISSION not in p.switches:
        p.switches[SWITCH_TYPE_MISSION] = set(SWITCH_BASE_INDICES)
        p.dirty = True
    # The authored STORY-UNLOCK sets (`STORY_UNLOCK=vortex,espers`): the switch bits retail grants
    # through story beats we skip. A PROGRESSION grant (`grant_switch` marks the state dirty), so in
    # `live` mode it sticks across runs -- delete the bits or start `--fresh` to revert. Unknown names
    # are reported, never guessed (player_state.story_unlock_bits).
    unlock_env = (os.environ.get("STORY_UNLOCK") or "").strip()
    if unlock_env:
        names = [n for n in re.split(r"[,\s]+", unlock_env) if n]
        unknown = [n for n in names if n.lower() not in STORY_UNLOCK_SETS]
        bits = story_unlock_bits(names)
        for stype, idxs in bits.items():
            for idx in sorted(idxs):
                p.grant_switch(stype, idx)
        granted = ", ".join(f"type {t} idx {sorted(i)}" for t, i in sorted(bits.items()))
        print(f"unlock: STORY_UNLOCK={','.join(names)} -> {granted}"
              + (f" (UNKNOWN: {','.join(unknown)})" if unknown else ""), flush=True)
    # Owned ESPERS (BEAST). `BEAST_SEED=siren` grants Siren at the **Story-Digest strength** (R3/L60, full
    # board: `F_FIXED_BEAST_MST` row 1) -- a TEST lever, not what the story gives; `=all` is that same shipped
    # preset for all 18 espers. The story itself grants at the `F_BEAST_STATUS_MST` MINIMUM (rarity 1, level 1,
    # empty board) -- see `story_beast_stats`. `grant_beast` validates (id, rarity) against
    # `F_BEAST_STATUS_MST`, so a bad seed fails loudly. The first esper is linked to the active party's
    # beast deck. See docs/notes/beast_esper_surface.md.
    seed = (os.environ.get("BEAST_SEED") or "").strip().lower()
    if seed:
        if seed == "all":
            presets = fixed_beast_presets()
        elif seed == "siren":
            presets = [{"esper_id": "1", "rarity": 3, "level": 60, "board": BEAST_BOARD_ALL}]
        else:
            presets = []
            print(f"beasts: BEAST_SEED={seed!r} unknown (known: siren, all) -- nothing granted", flush=True)
        active = p.data["party"]["active_deck"]
        for i, pr in enumerate(presets):
            p.grant_beast(pr["esper_id"], pr["rarity"], pr["level"], pr.get("board"),
                          link_deck=active if i == 0 else None)
        if presets:
            print(f"beasts: BEAST_SEED={seed} -> granted {len(presets)} esper(s) "
                  f"({', '.join(str(pr['esper_id']) for pr in presets[:3])}"
                  f"{' …' if len(presets) > 3 else ''}); linked {presets[0]['esper_id']} "
                  f"to deck {active}", flush=True)
    # Owned EMBLEMS. Emblems are EARNED from trophies and the grant is server-side, so a fresh account owns
    # none -- which is why the select screen lists all 522 as "Unacquired" by default and only "No emblem"
    # is settable. `EMBLEM_SEED=<ids>` grants; ids are validated against the shipped `F_EMBLEM_ITEM_MST`, so
    # a typo fails loudly instead of putting a crest on the header that no row backs. `EMBLEM_SEED=first`
    # grants the lowest-id emblem (10201, the All-Rounder Bronze) for exercising the *Set* path.
    eseed = (os.environ.get("EMBLEM_SEED") or "").strip()
    if eseed:
        if eseed.lower() == "first":
            ids = emblem_mst_ids()[:1]
        else:
            ids = []
            for part in re.split(r"[,\s]+", eseed):
                if not part:
                    continue
                if emblem_mst(part) is None:
                    print(f"emblems: EMBLEM_SEED id {part!r} is not in F_EMBLEM_ITEM_MST -- NOT granted",
                          flush=True)
                else:
                    ids.append(int(part))
        granted_ids = [i for i in ids if p.grant_emblem(i)]
        print(f"emblems: EMBLEM_SEED={eseed} -> granted {len(granted_ids)} emblem(s)"
              + (f" ({', '.join(str(i) for i in granted_ids)})" if granted_ids else " (nothing new)"),
              flush=True)
    # The EMBLEM EVENT's crest count (docs/notes/emblem_event.md). A crest is EARNED by meeting one of the
    # live campaign's weekly objectives, and most of those name event/season content our synthetic server
    # does not open -- so a fresh account has no reachable way to a tier either. `EMBLEM_CRESTS=<n>` marks
    # the first n objectives of the live campaign CLEARED (table order, N is clamped to the shipped count)
    # and grants the one crest item per objective that meeting one would: the count DERIVES from the cleared
    # set, so a seeded count can never disagree with the board the client draws. It is an authored lever and
    # is named as one -- never a claim that the objectives were met.
    cseed = (os.environ.get("EMBLEM_CRESTS") or "").strip()
    if cseed.isdigit() and int(cseed) > 0:
        campaign = emblem_campaign()
        rows = emblem_weekly_mst(campaign)[: int(cseed)]
        cleared = []
        for row in rows:
            mid = str(row.get(EMBLEM_WEEKLY_MILESTONE, ""))
            if not mid:
                continue
            p.emblem_event()["milestones"][mid] = {"progress": int(row.get(EMBLEM_WEEKLY_COUNT, 1) or 1),
                                                     "cleared": True}
            p.grant_item(EMBLEM_CREST_TYPE, EMBLEM_CREST_ITEM, 1)
            cleared.append(mid)
        p.dirty = True
        print(f"emblem event: EMBLEM_CRESTS={cseed} -> cleared {len(cleared)} objective(s) of campaign "
              f"{campaign} (crest count {p.emblem_tokens()}); the tier mail is delivered when the screen "
              f"is next opened, or by the next mission end it credits", flush=True)
    # SW-110: suppress the map/event resources we CANNOT serve, so the client does not 404 on them. A MapExt
    # row is emitted iff `eval(downloadSwitchId)` and NOT `eval(ignoreSwitchId)`; granting the row's
    # `ignoreSwitchId` makes the client skip it (`docs/notes/mapextresource_gate.md`). Derived from the table
    # × the served set — never hand-listed. `SUPPRESS_MISSING=0` disables it for a faithful-behaviour run.
    if os.environ.get("SUPPRESS_MISSING", "1") == "1":
        def _served_cdn(rel):
            for base in (State.cdn_overlay, State.cdn_root):
                if base and os.path.isfile(os.path.join(base, rel)):
                    return True
            return False
        ignores = map_ext_unservable_ignores(_served_cdn)
        granted_n = 0
        for sw in ignores:
            pos = switch_position(sw)
            if pos:
                p.grant_switch(pos[0], pos[1])
                granted_n += 1
        print(f"suppress: {len(ignores)} unservable MapExt row(s) -> {granted_n} ignore-switch bit(s) granted"
              f" (SUPPRESS_MISSING=0 to disable)", flush=True)
    # Persist immediately on load in `live` mode: a v1 file migrates to v2 HERE, and the freshly generated
    # roster instance ids must be STABLE from now on -- regenerating them on every restart would make the
    # client's roster/party (which hold the ids it was previously served) disagree with the server's.
    # Without this, a migrated account lived only in memory until some request happened to dirty it, and
    # `--state-save` would copy a v1 file with no roster. (`ephemeral`/`frozen` never write.)
    if STATE_PERSIST and p.mode == "live":
        p.save(STATE_FILE)
    # Name any injection explicitly: the schema (§1.3) requires a fiddled run never to be mistaken for a
    # clean one. An overlay is in memory only, so this line is its record.
    inj = list(getattr(p, "_overlay_labels", []) or [])
    print(f"state: {p.mode} slot={STATE_SLOT!r} from {os.path.relpath(STATE_FILE, WORK)} -- "
          f"rank {p.data['rank']['level']}, {len(p.data['roster'])} unit(s), "
          f"{len(p.paid_challenges)} mission(s) paid, "
          f"{sum(len(v) for v in p.switches.values())} switch bit(s), "
          f"lapis free={p.currencies['lapis_free']} paid={p.currencies['lapis_paid']}"
          + (f" [injections: {', '.join(inj)}]" if inj else ""), flush=True)


def save_state() -> None:
    """Persist the player if anything changed. `PlayerState.save` writes to a temp file and renames, so a
    crash mid-write cannot leave a half-file; a write failure is reported and never breaks the reply."""
    if not STATE_PERSIST:
        State.state_dirty = False
        return
    if State.player is not None and (State.player.dirty or State.state_dirty):
        State.player.save(STATE_FILE)
    State.state_dirty = False

__all__ = ["State", "LAST_DROPS", "NOTES", "_note", "SEASON", "SEASON_SELECTOR", "SEASON_MTIME",
           "GACHA_CAMPAIGN", "STATE_FILE", "STATE_SLOT", "STATE_MODE", "STATE_OVERLAY", "STATE_SETS",
           "STATE_PERSIST", "load_endpoints", "endpoint_for", "reload_replies_if_changed",
           "reload_season_if_changed", "load_state", "save_state", "warn_if_source_is_newer"]
