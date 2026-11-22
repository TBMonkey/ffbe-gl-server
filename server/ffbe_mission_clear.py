#!/usr/bin/env python3
"""ffbe_mission_clear.py — the mission-clear economy: first-clear rewards, the quest's own clear Gil, and
the exploration collection points.

Three shipped facts hang off the SAME pair of requests (the battle-bundle LOAD and the `MissionEnd`), so
they share one loaded-attempt record and one module:

1. **First-clear rewards** — `F_MISSION_MST.1x4bG2J8`, `kind:id:count:rate[:unit fields]`, **5,036 of
   17,690** missions (2,585 Lapis / 2,056 items / 296 units / 172 important items / 59 equipment / 24
   recipe books / 9 materia / 2 vision cards). The client CANNOT pay or report it: `MissionMst` never parses
   the column. The result screen DOES have the section — `MissionResultResponse.p1K5NecV` is stored at
   `MissionResultInfoBase+0x118` (`harness/response_field_map.json`, `mission_end_progression.md` §S4.2) and
   `MissionResultScene::setupFirstClearBonus` (0x237f8d8) splits it on ',' and ':' and lists each row. So
   the server pays and reports. Once per mission per save: `data["first_clear"]` = `{"baseline", "paid"}`,
   the baseline seeded from the save's already-cleared set the first time this runs, so a save that predates
   the feature is not retro-paid. (The collaborators' `EXVIUS_FIRST_CLEAR_BACKPAY` mail is an operator
   policy, not a recovered rule — not ported.)

2. **The quest's own clear Gil** — `F_MISSION_MST.7UR4J2SE`. The phone only sums the Gil its MONSTERS
   carry (`MissionEndRequest.Wdi3MAs2`), and `MissionMst` has no Gil getter, so a quest's clear Gil can only
   come from the server. Measured over our table: min 0, median **150**, p99 200,000, max **1,000,000**;
   3,953 missions pay ≥ 1,000 (the Chamber of Riches family). `mission_end_echo` carries the client's
   monster sum; this adds the mission's own amount and credits it, once per loaded attempt.

3. **Exploration collection points** — `HarvestDetailInfoResponse`, group `vM21k0do`: `N3hB0CwE` point id,
   `9GIvAQC2` order, `1Fa7rL5R` `kind:id:count`. A map event of type 14 (採取) is drawn and touchable only
   while its id is in `HarvestDetailInfoList`, and that list is filled from this group alone
   (`MapEventData::isEventEnable` 0x1fc5b5c). The exploration's LOAD reply gets one row per point with that
   run's reward; touching one pays it on the map and reports it on the result request's kind-6 channels —
   `EWmQy52k`/`XMgu7qE0`/`1Z2TD5ai`/`7rA9WGQe` = types 20/21/22/23, pinned in
   `mission_end_progression.md` §S4.2 from `toJsonGroup`'s `(cat,kind)` immediates. We pay what was
   reported up to what was served, once per loaded attempt, and echo it on the result.

The shared record is `data["battle_attempt"]` — written when a mission is LOADED (the `jnw49dUq` carrier:
a battle answers `jnw49dUq@<mission>`, an exploration answers the BARE `jnw49dUq` because it has no
`F_MISSION_PHASE_MST` rows — its fights are map fields), read at `MissionEnd`. `sequence` increments per
load of that mission, so a retried `MissionEnd` (same attempt) cannot double-pay while a new run can.
`frozen` writes nothing.

**Data provenance:** the collection-point CONTENT (`content/collection_points.json`) is `[Community]` — the
FFBE wiki's Global lists via the Exvius Revival fork (`docs/notes/exvius_revival_desktop.md`), because the
point ids are map-script data neither our tree nor the JP oracle decodes yet. The MECHANISM and the reward
gates are `[FFBEWW]` (the field map's own token table). Treat the pools as a hint to verify against a run.

Settings: `FIRST_CLEAR=0`, `MISSION_CLEAR_GIL=0`, `COLLECTION_POINTS=0` (each independent).
"""
from __future__ import annotations

import copy
import json
import os
import random

from ffbe_battle import mission_result_from
from ffbe_config import BATTLE_CLEAR_TOKEN, CHALLENGE_GROUP, RESULT_TAG
from ffbe_state import State, _note

MISSION_TABLE = "F_MISSION_MST.ndjson"
MISSION_KEY = "qo3PECw6"
FIRST_CLEAR_COLUMN = "1x4bG2J8"
CLEAR_GIL_COLUMN = "7UR4J2SE"
EXPLORATION_COLUMN = "Y4VoF8yu"       # 2 = exploration (473 missions); 1 = everything else

FIRST_CLEAR_TOKEN = "p1K5NecV"        # `MissionResultInfoBase+0x118`, setupFirstClearBonus (0x237f8d8)
GIL_TOKEN = "16LQuC5r"                # actual bonus gil, addGil(v, 0) (the client's own `Wdi3MAs2` rides it)

COLLECTION_GROUP = "vM21k0do"         # HarvestDetailInfoResponse
COLLECTION_ID = "N3hB0CwE"            # HarvestId
COLLECTION_ORDER = "9GIvAQC2"         # HarvestNum (the map ignores it; '1' is what the fork serves)
COLLECTION_REWARDS = "1Fa7rL5R"       # HarvestItem: `kind:id:count,...`
COLLECTION_CHANNELS = {20: "EWmQy52k", 21: "XMgu7qE0", 22: "1Z2TD5ai", 23: "7rA9WGQe"}

FIRST_CLEAR_KEY = "first_clear"
CLEAR_GIL_KEY = "mission_clear_gil"
ATTEMPT_KEY = "battle_attempt"

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTION_FILE = os.path.join(HERE, "content", "collection_points.json")

_COLLECTION: dict | None = None
_PHASES: dict | None = None
_PHASES_TRIED = False


# ---------------------------------------------------------------------------------------------------
# Settings, read per call so a golden/A-B run can flip one without an edit.
# ---------------------------------------------------------------------------------------------------
def _on(name: str) -> bool:
    return str(os.environ.get(name, "1")).strip().lower() not in ("0", "off", "false", "no")


def first_clear_enabled() -> bool:
    return _on("FIRST_CLEAR")


def clear_gil_enabled() -> bool:
    return _on("MISSION_CLEAR_GIL")


def collection_enabled() -> bool:
    return _on("COLLECTION_POINTS")


# ---------------------------------------------------------------------------------------------------
# The mission row and its grammar.
# ---------------------------------------------------------------------------------------------------
def mission_row(mid) -> dict:
    from player_state import _load_ndjson
    return _load_ndjson(MISSION_TABLE, MISSION_KEY).get(str(mid)) or {}


def _int(value) -> int:
    try:
        return int(str(value or "0").strip())
    except (TypeError, ValueError):
        return 0


def reward_rows(text) -> list[dict]:
    """`kind:id:count[:rate:…]` comma list -> rows. `rate` is kept; malformed entries are dropped.

    A unit reward carries extra fields after the rate (e.g. `10:904000105:1:100:1:0:100000006`); the
    grant path only needs kind/id/count, and the extras are the fork's Trust-Moogle metadata, which our
    `_grant_items` does not model.
    """
    rows: list[dict] = []
    for entry in str(text or "").split(","):
        f = entry.strip().split(":")
        if len(f) < 3 or not all(x.isdigit() for x in f[:3]):
            continue
        kind, iid, count = (int(f[0]), int(f[1]), int(f[2]))
        rate = int(f[3]) if len(f) > 3 and f[3].isdigit() else 100
        if count <= 0 or rate <= 0:
            continue
        rows.append({"kind": kind, "id": iid, "count": count, "rate": rate})
    return rows


def first_clear_rows(mid) -> list[dict]:
    return reward_rows(mission_row(mid).get(FIRST_CLEAR_COLUMN))


def clear_gil(mid) -> int:
    return max(0, _int(mission_row(mid).get(CLEAR_GIL_COLUMN)))


def is_exploration(mid) -> bool:
    return str(mission_row(mid).get(EXPLORATION_COLUMN) or "") == "2"


def _phase_counts() -> dict | None:
    """mission -> phase count, from the tracked battle index. None when the index is unavailable.

    The index (`server/content/battle/F_MISSION_PHASE_MST.tsv`) is the JP oracle's superset (21,427
    missions vs our 17,690); a mission absent from it has no battle to load, which is what makes the
    cutscene/story beats `cleared`-less at MissionEnd.
    """
    global _PHASES, _PHASES_TRIED
    if _PHASES_TRIED:
        return _PHASES
    _PHASES_TRIED = True
    try:
        from battle_content import _default_index_source
        rows = _default_index_source().load("F_MISSION_PHASE_MST")
    except (SystemExit, OSError) as exc:  # noqa: BLE001 - a missing index must not break the reply
        print(f"mission-clear: battle index unavailable ({exc}); a battle mission needs its clear token",
              flush=True)
        _PHASES = None
        return None
    counts: dict[str, int] = {}
    for row in rows:
        mid = str(row.get("qo3PECw6") or "")
        counts[mid] = counts.get(mid, 0) + 1
    _PHASES = counts
    return _PHASES


def clear_succeeded(mid, plaintext) -> bool:
    """Did this MissionEnd report a SUCCESSFUL clear? Deny when the log is absent and a battle was loaded.

    The three shapes, measured: an exploration or a zero-phase beat sends no battle log (only the result
    body), a battle sends `2urzfX6d` with `69ieJGhD` (61/61 of the recorded battle corpus), and a defeat
    carries the group but no clear rows. The safe error on an unknown shape is to deny.
    """
    ctx = mission_result_from(plaintext)
    counts = _phase_counts()
    waves = None if counts is None else counts.get(str(mid), 0)
    if is_exploration(mid) or waves == 0:
        return ctx["cleared"] is not False
    if not ctx["group_present"]:
        _note(f"mission-clear: {mid} sent no battle log and is not a zero-wave mission -- "
              f"clear NOT assumed, nothing paid")
        return False
    return ctx["cleared"] is True


# ---------------------------------------------------------------------------------------------------
# The loaded-attempt record.
# ---------------------------------------------------------------------------------------------------
def attempt(player) -> dict:
    value = player.data.get(ATTEMPT_KEY)
    return value if isinstance(value, dict) else {}


def _attempt_mark(player, mid) -> list:
    """`[mission, sequence]` when this MissionEnd belongs to the loaded attempt, else `[mission, None]`."""
    rec = attempt(player)
    same = str(rec.get("mid")) == str(mid)
    return [str(mid), rec.get("sequence") if same else None]


def record_battle_attempt(spec, mid, reply_id):
    """Pipeline step on a mission LOAD (`jnw49dUq`, the battle-bundle carrier): bump the attempt, roll points.

    Gated on the REPLY ID, not on `is_battle_bundle`: a battle gets its own `<reply_id>@<mission>` key, but
    an EXPLORATION has no `F_MISSION_PHASE_MST` rows (its fights are map fields, not mission phases), so it
    is served the BARE `jnw49dUq` with the mission id from the request — measured on 1110104, which the
    index refuses to derive. On an exploration the step also rolls the run's collection points and puts
    `vM21k0do` on the reply (empty when the exploration has no recorded points — the group is replace-on-read
    on the client, and maps reuse point ids).
    """
    from battle_content import BATTLE_BUNDLE_REPLY_ID
    player = State.player
    if (not mid or reply_id != BATTLE_BUNDLE_REPLY_ID or not isinstance(spec, dict) or RESULT_TAG in spec
            or player is None or player.mode == "frozen"):
        return spec
    mid = str(mid)
    if not player._mutate():
        return spec
    prev = attempt(player)
    sequence = (int(prev.get("sequence") or 0) + 1) if str(prev.get("mid")) == mid else 1
    rec = {"mid": mid, "sequence": sequence}
    player.data[ATTEMPT_KEY] = rec
    if not collection_enabled() or not is_exploration(mid):
        return spec
    rolled = roll_collection(mid)
    rec["collection"] = rolled
    print(f"collection points: {mid} attempt {sequence}: {len(rolled)} point(s) "
          + " ".join(f"{p}={r}" for p, r in rolled.items()), flush=True)
    out = dict(spec)
    out[COLLECTION_GROUP] = [{COLLECTION_ID: pid, COLLECTION_ORDER: "1", COLLECTION_REWARDS: reward}
                             for pid, reward in rolled.items()]
    return out


# ---------------------------------------------------------------------------------------------------
# 1. First-clear rewards.
# ---------------------------------------------------------------------------------------------------
def _ledger(player) -> dict:
    led = player.data.get(FIRST_CLEAR_KEY)
    if isinstance(led, dict):
        led.setdefault("baseline", [])
        led.setdefault("paid", [])
        return led
    led = {"baseline": [str(m) for m in player.data.get("progress", {}).get("cleared_missions", [])],
           "paid": []}
    if player._mutate():
        player.data[FIRST_CLEAR_KEY] = led
    return led


def first_clear_due(player, mid) -> bool:
    led = _ledger(player)
    mid = str(mid)
    return mid not in led["paid"] and mid not in led["baseline"]


def _with_result_token(spec, token, value):
    out = copy.deepcopy(spec)
    for row in out.get(RESULT_TAG, []):
        if isinstance(row, dict):
            row[token] = value
    return out


def pay_first_clear(spec, mid, plaintext):
    """Pipeline step: pay `F_MISSION_MST.1x4bG2J8` once per mission and list it on the result."""
    player = State.player
    if (not first_clear_enabled() or player is None or player.mode == "frozen" or not mid
            or not isinstance(spec, dict) or RESULT_TAG not in spec):
        return spec
    mid = str(mid)
    rows = first_clear_rows(mid)
    if not rows:
        return spec
    if not first_clear_due(player, mid) or not clear_succeeded(mid, plaintext):
        return spec
    payable = [r for r in rows if player._can_credit(r)]
    for row in rows:
        if row not in payable:
            print(f"first-clear: mission {mid}: {row['kind']}:{row['id']}x{row['count']} NOT paid -- "
                  f"no reply group projects this kind", flush=True)
    if not payable:
        return spec
    before, dirty_before = copy.deepcopy(player.data), player.dirty
    try:
        granted = player._grant_items([{"kind": r["kind"], "id": r["id"], "count": r["count"]}
                                       for r in payable])
        if not granted:
            return spec
        led = _ledger(player)
        led["paid"].append(mid)
        if player.mode != "frozen":
            player.dirty = True
            State.state_dirty = True
    except Exception:  # noqa: BLE001 - a failed grant must leave the save as it was
        player.data, player.dirty = before, dirty_before
        raise
    text = ",".join(f"{r['kind']}:{r['id']}:{r['count']}" for r in granted)
    print(f"first-clear: mission {mid} paid {text}", flush=True)
    return _with_result_token(spec, FIRST_CLEAR_TOKEN, text)


# ---------------------------------------------------------------------------------------------------
# 2. The quest's own clear Gil.
# ---------------------------------------------------------------------------------------------------
def credit_clear_gil(spec, mid, plaintext):
    """Pipeline step: credit `F_MISSION_MST.7UR4J2SE` on a successful clear, once per loaded attempt."""
    player = State.player
    if (not clear_gil_enabled() or player is None or player.mode == "frozen" or not mid
            or not isinstance(spec, dict) or RESULT_TAG not in spec):
        return spec
    mid = str(mid)
    gil = clear_gil(mid)
    if gil <= 0:
        return spec
    mark = _attempt_mark(player, mid)
    led = player.data.get(CLEAR_GIL_KEY)
    if isinstance(led, dict) and led.get("last") == mark:
        return spec
    if not clear_succeeded(mid, plaintext):
        return spec
    before = int(player.currencies.get("gil", 0))
    if player.add_currency("gil", gil) == before:
        return spec
    if player.mode != "frozen":
        player.data[CLEAR_GIL_KEY] = {"last": mark}
        player.dirty = True
        State.state_dirty = True
    print(f"mission-gil: mission {mid} paid {gil} clear gil (attempt {mark[1]}) -> gil="
          f"{player.currencies.get('gil')}", flush=True)
    out = copy.deepcopy(spec)
    for row in out.get(RESULT_TAG, []):
        if isinstance(row, dict):
            shown = _int(row.get(GIL_TOKEN))
            row[GIL_TOKEN] = str(shown + gil)
    return out


# ---------------------------------------------------------------------------------------------------
# 3. Exploration collection points.
# ---------------------------------------------------------------------------------------------------
def collection_doc() -> dict:
    global _COLLECTION
    if _COLLECTION is None:
        try:
            with open(COLLECTION_FILE, encoding="utf-8") as fh:
                _COLLECTION = json.load(fh)
        except (OSError, ValueError) as exc:  # noqa: BLE001 - no content must not break a reply
            print(f"collection points: {COLLECTION_FILE} not loadable ({exc!r}); none served", flush=True)
            _COLLECTION = {}
    return _COLLECTION


def roll_collection(mid, rng=random) -> dict:
    """point id -> `kind:id:count` for one run of this exploration (the pool's odds, min..max count)."""
    doc = collection_doc()
    out: dict[str, str] = {}
    for pid, key in doc.get("missions", {}).get(str(mid), []):
        items = (doc.get("pools", {}).get(key) or {}).get("items") or []
        if not items:
            continue
        kind, iid, _odds, lo, hi = rng.choices(items, weights=[i[2] for i in items])[0]
        out[str(pid)] = f"{kind}:{iid}:{rng.randint(int(lo), int(hi))}"
    return out


def _reported(plaintext) -> dict:
    """`(kind, id)` -> count the MissionEnd request reports as collected on its kind-6 channels."""
    if not plaintext:
        return {}
    try:
        req = json.JSONDecoder().raw_decode(plaintext)[0]
    except (TypeError, ValueError):
        return {}
    rows = req.get(RESULT_TAG) if isinstance(req, dict) else None
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return {}
    out: dict[tuple, int] = {}
    for kind, channel in COLLECTION_CHANNELS.items():
        for entry in str(rows[0].get(channel) or "").split(","):
            f = entry.split(":")
            if len(f) >= 3 and all(x.isdigit() for x in f[:3]) and int(f[0]) == kind and int(f[2]) > 0:
                out[(kind, int(f[1]))] = out.get((kind, int(f[1])), 0) + int(f[2])
    return out


def pay_collection_points(spec, mid, plaintext):
    """Pipeline step: pay the points this clear reported, up to what was served, once per attempt."""
    player = State.player
    if (not collection_enabled() or player is None or player.mode == "frozen" or not mid
            or not isinstance(spec, dict) or RESULT_TAG not in spec):
        return spec
    mid = str(mid)
    rec = attempt(player)
    rolled = rec.get("collection")
    if str(rec.get("mid")) != mid or not isinstance(rolled, dict) or not rolled:
        return spec
    paid = rec.get("collection_paid")
    if paid is None:
        if not clear_succeeded(mid, plaintext):
            return spec
        served: dict[tuple, int] = {}
        for reward in rolled.values():
            kind, iid, count = (int(x) for x in reward.split(":"))
            served[(kind, iid)] = served.get((kind, iid), 0) + count
        reported = _reported(plaintext)
        grants = [{"kind": k, "id": i, "count": min(n, served[(k, i)])}
                  for (k, i), n in sorted(reported.items()) if (k, i) in served]
        extra = {key: n for key, n in reported.items() if key not in served or n > served[key]}
        if extra:
            _note(f"collection points: {mid} reported more than was served, not paid: {extra}")
        before, dirty_before = copy.deepcopy(player.data), player.dirty
        try:
            if grants and player._grant_items(grants) != grants:
                raise RuntimeError("collection point grant did not credit every row")
        except Exception:  # noqa: BLE001 - all-or-nothing
            player.data, player.dirty = before, dirty_before
            raise
        paid = [f"{g['kind']}:{g['id']}:{g['count']}" for g in grants]
        player.data[ATTEMPT_KEY]["collection_paid"] = paid
        player.dirty = True
        State.state_dirty = True
        if paid:
            print(f"collection points: {mid} paid " + ",".join(paid), flush=True)
    if not paid:
        return spec
    out = copy.deepcopy(spec)
    for row in out.get(RESULT_TAG, []):
        if not isinstance(row, dict):
            continue
        for kind, channel in COLLECTION_CHANNELS.items():
            entries = [e for e in paid if _int(e.split(":")[0]) == kind]
            if entries:
                row[channel] = ",".join(entries)
    return out


# ---------------------------------------------------------------------------------------------------
# Selftest — the economy and the gates, against a fresh synthetic save.
# ---------------------------------------------------------------------------------------------------
def _selftest() -> int:
    from player_state import PlayerState

    failed: list[str] = []
    challenge = json.dumps({CHALLENGE_GROUP: [{BATTLE_CLEAR_TOKEN: "1:100:3"}]})
    clear_spec = {RESULT_TAG: [{"16LQuC5r": "0"}]}

    def fresh():
        player = PlayerState.load("/nonexistent", slot="mission_clear_selftest")
        State.player = player
        return player

    def case(name, ok, want, got):
        print(f"mission-clear {'ok  ' if ok else 'FAIL'} {name} (want {want}, got {got})")
        if not ok:
            failed.append(name)

    # --- the shipped rows parse as the table says
    rows = first_clear_rows("1110103")
    case("1110103 first-clear parses to 100 lapis", rows and rows[0]["kind"] == 50 and rows[0]["count"] == 100,
         "kind 50 x100", rows)
    case("1110103 clear gil is 12", clear_gil("1110103") == 12, 12, clear_gil("1110103"))
    case("1110104 is an exploration", is_exploration("1110104"), True, is_exploration("1110104"))

    # --- first clear: pays once, lists the row, and a second clear pays nothing
    p = fresh()
    lapis = int(p.currencies.get("lapis_free", 0))
    out = pay_first_clear(dict(clear_spec), "1110103", challenge)
    case("first clear pays 100 lapis", int(p.currencies.get("lapis_free", 0)) - lapis == 100,
         100, int(p.currencies.get("lapis_free", 0)) - lapis)
    case("first clear lists p1K5NecV", out[RESULT_TAG][0].get(FIRST_CLEAR_TOKEN) == "50:0:100",
         "50:0:100", out[RESULT_TAG][0].get(FIRST_CLEAR_TOKEN))
    again = pay_first_clear(dict(clear_spec), "1110103", challenge)
    case("a repeat clear pays nothing and lists nothing",
         int(p.currencies.get("lapis_free", 0)) - lapis == 100
         and FIRST_CLEAR_TOKEN not in again[RESULT_TAG][0], "100 lapis once",
         int(p.currencies.get("lapis_free", 0)) - lapis)

    # --- a pre-feature clear is baseline, not paid
    p = fresh()
    p.data["progress"]["cleared_missions"].append("1110101")
    p.data.pop(FIRST_CLEAR_KEY, None)
    lapis = int(p.currencies.get("lapis_free", 0))
    pay_first_clear(dict(clear_spec), "1110101", challenge)
    case("a clear in the baseline is not retro-paid", int(p.currencies.get("lapis_free", 0)) == lapis,
         lapis, p.currencies.get("lapis_free"))

    # --- clear gil: paid and marked on this attempt, not on a retry
    p = fresh()
    gil = int(p.currencies.get("gil", 0))
    record_battle_attempt({"an6TB4WS": []}, "1110103", "jnw49dUq")
    out = credit_clear_gil(dict(clear_spec), "1110103", challenge)
    case("clear gil is credited", int(p.currencies.get("gil", 0)) - gil == 12, 12,
         int(p.currencies.get("gil", 0)) - gil)
    case("clear gil is on the result", out[RESULT_TAG][0].get(GIL_TOKEN) == "12", "12",
         out[RESULT_TAG][0].get(GIL_TOKEN))
    retry = credit_clear_gil(dict(clear_spec), "1110103", challenge)
    case("a retried MissionEnd does not pay again",
         int(p.currencies.get("gil", 0)) - gil == 12 and retry[RESULT_TAG][0].get(GIL_TOKEN) == "0",
         "12 once, token stays 0", int(p.currencies.get("gil", 0)) - gil)

    # --- collection points: the load reply carries the rolled points, the end pays what was reported
    p = fresh()
    p.data.pop(ATTEMPT_KEY, None)
    load = record_battle_attempt({"an6TB4WS": []}, "1110104", "jnw49dUq")
    group = load.get(COLLECTION_GROUP) or []
    case("an exploration's load carries its points", len(group) == 8, 8, len(group))
    pid, reward = group[0][COLLECTION_ID], group[0][COLLECTION_REWARDS]
    kind, iid, _count = reward.split(":")
    channel = COLLECTION_CHANNELS[int(kind)]
    report = json.dumps({RESULT_TAG: [{channel: f"{kind}:{iid}:1"}]})
    out = pay_collection_points(dict(clear_spec), "1110104", report)
    case("a collected point is paid and echoed",
         out[RESULT_TAG][0].get(channel) == f"{kind}:{iid}:1" and p.item_count(int(kind), iid) >= 1,
         f"{kind}:{iid}:1", out[RESULT_TAG][0].get(channel))
    retry = pay_collection_points(dict(clear_spec), "1110104", report)
    case("a retried collection report is not paid twice",
         p.item_count(int(kind), iid) == 1, 1, p.item_count(int(kind), iid))

    # --- a battle with no log is not assumed cleared
    p = fresh()
    lapis = int(p.currencies.get("lapis_free", 0))
    pay_first_clear(dict(clear_spec), "2111201", "{}")
    case("a battle MissionEnd with no clear log pays nothing",
         int(p.currencies.get("lapis_free", 0)) == lapis, lapis, p.currencies.get("lapis_free"))
    return 1 if failed else 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    print("ffbe_mission_clear is a library module; the server entry point is server/capture_server.py")
    raise SystemExit(0)
