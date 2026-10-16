#!/usr/bin/env python3
"""ffbe_battle.py — the BATTLE axis: drops, the mission-end reply, challenge payment, the switch grant, and the MST
version table. What it owns in one line: everything that happens *because a battle was fought*.

It is the deepest module in the split -- `ffbe_player` borrows two helpers from here (`_lv_half`,
`_switch_container`) -- so the dependency is deliberately ONE WAY. It is the module to read first when
asking "why did the result screen show that": `roll_drops` and `apply_result_drops` are the pair the
`LAST_DROPS` fix touched, and the `LAST_DROPS`/`xF9Sr1a6` channel between them is D1 and D6 of the
dependency table in `ffbe_pipeline`.

Extracted 2026-09-26 from `capture_server.py` (docs/SERVER_REFACTOR_PLAN.md, Phase 3, last seam).
"""
from __future__ import annotations

import copy, json, os, random, re

from ffbe_config import *          # noqa: F401,F403 -- constants; the table has no behaviour
import ffbe_state as st           # the REBOUND runtime state; see ffbe_state's docstring
from ffbe_state import State, _note
from player_state import (credit_exp, mission_open_switch, story_beast_for_mission, story_beast_stats,
                          switch_position, unit_stats)


__all__ = [
    "CONDITION_CHECKS",
    "apply_battle_bgm",
    "apply_result_drops",
    "apply_result_item_columns",
    "challenge_table",
    "credit_mission_lapis",
    "credit_mission_rewards",
    "grant_cleared_mission",
    "mission_end_echo",
    "mission_id_from",
    "mission_lv_info",
    "mission_result_from",
    "party_size",
    "per_unit_exp",
    "record_pushed_switches",
    "roll_drops",
    "serve_challenge_clear",
    "serve_challenge_clear_all",
    "serve_version_table",
    "version_table",
]

_CHALLENGE_INFO_LOGGED: set = set()   # reply ids already announced by serve_challenge_clear_all
def roll_drops(spec, mid: str | None = None, is_battle_bundle: bool = False):
    """Turn each served drop list into a ROLL of itself, so drops vary per battle.

    A `y35YvQjZ` row's `9BetM2Ds` is `cat:item:count:rate,...` -- a comma list, FOUR fields per item. The
    client reads four fields with no length check (`GameUtils::getMissionDropItemInfo` 0x19f7c28 reads
    fields 0-3 unconditionally) and then DISCARDS the rate, so the client never rolls: the server is the
    only thing that can decide what dropped. Serving the list verbatim is why every battle yielded the
    same items, and why they did not reflect what was earned.

    Items failing their rate roll are dropped, EXCEPT that one is always kept: an empty `9BetM2Ds` splits
    to a single empty item, and a 0-field item is the same unguarded over-read shape as the 3-field list
    this server used to serve (the SW-23 fault class). A battle yielding literally nothing is also not
    retail behaviour, so guaranteeing one is both safer and closer.

    Returns a COPY: `State.replies` holds the candidate table and must not be mutated, or the roll would
    stick for the rest of the session.
    """
    if not isinstance(spec, dict):
        return spec
    if not ROLL_DROPS:
        return spec            # reference state: serve the payload's own candidate list verbatim
    rows = spec.get(DROP_TAG_ROWS)
    if not isinstance(rows, list):
        # This mission's bundle carries no drop rows at all, so it OWNS its slot and claims it empty --
        # a battle that rolled nothing must not inherit the previous battle's drops.
        if is_battle_bundle and mid is not None:
            st.LAST_DROPS.pop(str(mid), None)
        return spec
    spec = dict(spec)
    out = []
    for row in rows:
        if not isinstance(row, dict) or DROP_TOKEN not in row:
            out.append(row)
            continue
        # ROLL THE PAYLOAD'S OWN CANDIDATE LIST, not a global table. Each MonsterParts row carries its own
        # `dropInfo` (per-monster pool, e.g. Steel Bat's differs from Dire Rat's), and the DB is the oracle
        # for it -- so the pool belongs in the served `9BetM2Ds`, and the server only rolls it. `DROP_TABLE`
        # remains as the fallback for a row that has none.
        src = row.get(DROP_TOKEN) or ",".join(DROP_TABLE.split(","))
        items = [it for it in src.split(",") if it]
        rolled = []
        for it in items:
            f = it.split(":")
            rate = int(f[3]) if len(f) > 3 and f[3].isdigit() else 100
            if random.randint(1, 100) <= rate:
                rolled.append(it)
        if not rolled and items:
            rolled = [items[-1]]
        row = dict(row)
        # SERVE 4 FIELDS -- the parser reads fields [0..3] with NO bounds check (`getMissionDropItemInfo`
        # 0x19f7c28: `add x0,x8,#0x48` then StrToInt, BEFORE the count>=5 guard at 0x19f7cfc), so a
        # 3-field item over-reads the `vector<std::string>` allocation and the neighbour's bytes become a
        # std::string whose +0x10 is dereferenced (the SW-23 class). The 4th field (rate) is read and
        # DISCARDED downstream -- harmless, and REQUIRED for the read to stay in bounds. This is the same
        # direction the 2026-09-19 note reaches (battle_authoring_data.md S5.3: "comma-separated elements,
        # `:`-separated ints, >=4 fields"). Do NOT strip it. The result-screen mirror strips separately
        # (addItem is arity-tolerant), so this stays the WAVE-format shape only.
        row[DROP_TOKEN] = ",".join(rolled)
        out.append(row)
    # THE EVENT CURRENCY (APP-35). A story event pays an event item, and the AMOUNT cannot be derived: GL's
    # drop tables are not shipped (the server resolved drops live) and the JP oracle ships no item table
    # beside its drop ids, so EVENT_CURRENCY is an authored choice. Additive by construction -- it appends
    # to the payload's own candidate list and never removes from it, so no existing mission's drops change;
    # the missions it applies to are the ones whose id is under an EVENT_GATE_AREAS area.
    if EVENT_CURRENCY and mid and out and str(mid).startswith(tuple(EVENT_GATE_AREAS)):
        extra = ",".join(p + ":100" for p in EVENT_CURRENCY.split(",") if p)
        if extra:
            first = dict(out[0])
            first[DROP_TOKEN] = ((first.get(DROP_TOKEN) + "," + extra)
                                 if first.get(DROP_TOKEN) else extra)
            out[0] = first
    spec[DROP_TAG_ROWS] = out
    # AGGREGATE, do not de-duplicate. The client folds every phase's drops into ONE list and reports it
    # WITH COUNTS -- its own MissionEnd request carried `20:202000400:2`. The old `dict.fromkeys(...)`
    # collapsed a twice-dropped item into a single row, so the battle showed two and the result screen
    # listed one. Sum the counts per `cat:id`, preserving first-seen order.
    counts: dict[tuple, int] = {}
    order: list[tuple] = []
    for r in out:
        for it in r.get(DROP_TOKEN, "").split(","):
            if not it:
                continue
            f = it.split(":")
            key = (f[0], f[1])
            if key not in counts:
                order.append(key)
            counts[key] = counts.get(key, 0) + (int(f[2]) if len(f) > 2 and f[2].isdigit() else 1)
    if is_battle_bundle and mid is not None:
        st.LAST_DROPS[str(mid)] = [f"{c}:{i}:{counts[(c, i)]}" for c, i in order]
    return spec
def apply_result_drops(spec, mid: str | None = None):
    """Put THIS battle's roll into the result screen's item channels.

    The result screen merges four channels (`ResultSummaryScene::setLayoutItem` reads `getItemList` and then a
    virtual accessor), and they are what the operator actually sees -- which is why a battle that dropped
    rolled items still showed the same fixed set: `xF9Sr1a6` is the DROPS channel (`addItem(20,0,csv)`,
    kind 0) and we served a hardcoded list there. It now carries the same roll as `9BetM2Ds`.

    NEVER WRITES AN EMPTY VALUE. An empty item list splits to a single empty item, and a 0-field item is the
    unguarded over-read shape this project has already been burned by (SW-23) -- the same reason `roll_drops`
    refuses to emit an empty `9BetM2Ds`. So if there is no roll to apply, the payload's own value is left
    alone. A runtime fill must never make a field emptier than the payload did.

    (An earlier version of this also BLANKED the kind-5/kind-6 channels on the argument that battle-found
    content we do not model should not be a constant. That was wrong twice: it treated "unmodelled" as
    "should be empty", and empty is a SHAPE this parser reads unguarded. It produced a black screen after
    the battle with no result scene entered at all.)
    """
    if not isinstance(spec, dict):
        return spec
    if not MIRROR_DROPS:
        return spec            # control: serve the payload verbatim (last known-good). The format for this
                               # channel is NOT established -- see the comment on MIRROR_DROPS.
    rows = spec.get(RESULT_TAG)
    if not isinstance(rows, list):
        return spec
    # THIS mission's own roll, or nothing. The old unkeyed `if not LAST_DROPS` check was true whenever
    # ANY battle had ever rolled in the session, so an unrelated battle's drops could be mirrored here.
    rolled_for_this = st.LAST_DROPS.get(str(mid)) if mid is not None else None
    if not rolled_for_this:
        return spec              # this battle never rolled: keep the payload's own fallback, untouched
    # ARITY: the two channels are parsed by DIFFERENT code with DIFFERENT field counts.
    #   `9BetM2Ds` (WaveBattleInfo) -> GameUtils::getMissionDropItemInfo 0x19f7c28: FOUR fields, and the
    #     4th (the rate) is read then discarded -- which is why the roll carries it.
    #   `xF9Sr1a6` (MissionResultResponse) -> addItem(20, 0, csv): the arity is set by its own 3-field
    #     sibling `Z7G0yxXe` = addItem(20, 5, csv) = "20:290050200:1", i.e. cat:item:count.
    # Mirroring the wave format verbatim therefore handed addItem a 4-field item. That is the same class of
    # defect as SW-23 (a value whose shape disagrees with the reader), so the rate is stripped here.
    # NOTE the arity is inferred from a working sibling, not read from addItem's parser -- if this still
    # faults, read that parser next rather than guessing again.
    mirror = [":".join(it.split(":")[:3]) for it in rolled_for_this]
    spec = dict(spec)
    out = []
    for row in rows:
        if not isinstance(row, dict):
            out.append(row)
            continue
        row = dict(row)
        row[RESULT_DROP_TOKEN] = ",".join(mirror)
        out.append(row)
    spec[RESULT_TAG] = out
    return spec
def apply_battle_bgm(spec):
    """SW-105 A/B: drop `MissionPhaseMst.YS9kfCX7` (the battle BGM) when `BATTLE_BGM=0`.

    See the `BATTLE_BGM` block for the mechanism and why this is the one-variable test. Like the other
    strip transforms it copies each row before popping, so a shared payload object is never mutated, and it
    notes what it did (a silent strip and a strip that never ran look identical in the client).
    """
    if BATTLE_BGM or not isinstance(spec, dict):
        return spec
    rows = spec.get(PHASE_TAG)
    if not isinstance(rows, list):
        return spec
    out, stripped = [], 0
    for row in rows:
        if not isinstance(row, dict):
            out.append(row)
            continue
        row = dict(row)
        if row.pop(PHASE_BGM_TOKEN, None) is not None:
            stripped += 1
        out.append(row)
    spec = dict(spec)
    spec[PHASE_TAG] = out
    _note(f"battle-bgm: BATTLE_BGM=0 -- dropped {PHASE_BGM_TOKEN} from {stripped} of {len(rows)} "
          f"{PHASE_TAG} row(s)")
    return spec
def apply_result_item_columns(spec):
    """SW-93(b) A/B: strip the `09HRWXDf` addItem columns when `RESULT_ITEM_COLUMNS=0`.

    `mission_end_progression.md` S9.5: S9.3 called these tokens "inert for this screen", but REMOVING them
    made the result screen *briefer* -- a section that had been present (a listing of possible drops) went
    away -- while their item icons never rendered. So they are only PARTIALLY load-bearing, and the render
    half was never measured under a controlled A/B. This switch exists for that one comparison: serve the
    mission-end reply once with the columns and once without, then look at `ResultSummaryScene`.

    The default (`RESULT_ITEM_COLUMNS=1`) leaves the payload byte-for-byte. The strip runs LAST in the
    pipeline because `mission_end_echo` and `credit_mission_rewards` both (re)write `xF9Sr1a6` -- an
    earlier strip would be undone. Rows are COPIED before popping, like `apply_result_drops`, so a shared
    payload object is not mutated field-by-field.
    """
    if RESULT_ITEM_COLUMNS or not isinstance(spec, dict):
        return spec
    rows = spec.get(RESULT_TAG)
    if not isinstance(rows, list):
        return spec
    out, stripped = [], 0
    for row in rows:
        if not isinstance(row, dict):
            out.append(row)
            continue
        row = dict(row)
        for tok in RESULT_ITEM_TOKENS:
            if row.pop(tok, None) is not None:
                stripped += 1
        out.append(row)
    spec = dict(spec)           # shallow copy: never mutate a shared payload object (the payload must
    spec[RESULT_TAG] = out      # always remain safe to serve verbatim -- see `apply_result_drops`)
    _note(f"result-items: RESULT_ITEM_COLUMNS=0 -- stripped {stripped} item column(s) from "
          f"{len(rows)} {RESULT_TAG} row(s)")
    return spec
def mission_id_from(plaintext: str | None) -> str | None:
    """The mission id a request names, so a battle bundle can be selected per mission.

    The request body is the decrypted JSON (trailing manual-pad byte stripped). Any group row carrying
    `6uIYE15X` (the area/mission id on `DungeonResourceLoadMstListRequest`) or `qo3PECw6` (the mission
    id on `MissionEndRequest`) is enough; we take the first found. Returns None when unparseable, so
    the caller falls back to the plain requestID key.
    """
    if not plaintext:
        return None
    try:
        # DO NOT strip a fixed pad byte. The client's manual PKCS#7 pad leaves the pad VALUE as the
        # last byte (0x01..0x10 varies with length), so `rstrip("\x01")` only ever matched one case and
        # silently no-opped the rest. raw_decode parses the leading JSON value and ignores trailing
        # bytes, which is correct regardless of the pad.
        obj = json.JSONDecoder().raw_decode(plaintext)[0]
    except Exception as exc:  # noqa: BLE001
        _note(f"mission_id_from: request body did not parse as JSON ({exc!r}); "
              f"falling back to the plain replyID key (per-mission dispatch is OFF for this request)")
        return None
    if not isinstance(obj, dict):
        return None
    for grp in obj.values():
        if not isinstance(grp, list):
            continue
        for row in grp:
            if isinstance(row, dict):
                for tok in ("6uIYE15X", "qo3PECw6"):
                    v = row.get(tok)
                    if v:
                        return str(v)
    return None
def _lv_half(level, exp, stats, lb_level, lb_exp) -> str:
    """One 15-field `Qic38SuC` half — `Lv:LimitExLvNum:Exp:HP:MP:ATK:DEF:MAG:SPR:f9:LbLv:LbExp:a12:0:a14`.

    Format read end to end in `docs/notes/result_scene_selection.md` §7 (`UserUnitLvInfo::init`): 15 colon
    fields are read UNGUARDED, and fields 3..8 are `splitInt(...,"-")` each pushing exactly four ints, so
    every stat group needs at least two dash-separated numbers.

    `a[12]` CARRIES THE EXP, and that is a correction. The note's field table says the client's own emitter
    writes literal `"0"` there (idx 30), and an earlier revision therefore left it `0` -- but the emitter is
    the REQUEST side, and `ResultUnitExpScene::updateEvent`'s show-gate is `+0x6f0 > 0` where
    `+0x6f0 <- setExp <- getPartyTotalExp` = the sum of `getAfterParam(30)`, i.e. the AFTER half's `a[12]`.
    With it `0` the unit-EXP screen never appears at all, which is exactly what the operator saw on
    2026-09-22 while the roster held the EXP. SW-25 had already observed a non-zero value making the screen
    appear and driving the level it displays. **A request-side literal is not evidence about a reply-side
    reader** (RE_VERIFICATION_IDIOMS). One run settles the remaining question -- whether the screen counts
    up from the BEFORE half's value to the AFTER half's.
    """
    fields = [str(level), "0", str(exp)]
    fields += [f"{int(s)}-0-0-0" for s in stats]          # HP, MP, ATK, DEF, MAG, SPR
    fields += ["0", str(lb_level), str(lb_exp), str(exp), "0", "0"]   # f9, LbLv, LbExp, a12=EXP, a13, a14
    assert len(fields) == 15, len(fields)
    return ":".join(fields)
def mission_lv_info(player, exp_gain: int) -> str:
    """The `Qic38SuC` value: one row per party member, `KEY@BEFORE_AFTER`.

    WHY THE KEY IS THE WHOLE POINT: `UserUnitLvInfo::init` stores the part before `@` as the LOOKUP KEY, and
    `getUnitLvInfo(key)` compares it against `UserUnitInfo::getUserUnitID()` — i.e. the unit's **instance id**,
    the same `og2GHy49` the roster serves. The old hand-authored literal keyed these rows `1000000001/2`;
    the SW-62 migration moved the units to epoch-ms instance ids, the lookup then missed SILENTLY, and the
    result read as "no experience rewarded" (idioms #26/#27). BEFORE is the unit as it stands now, AFTER the
    same with `exp_gain` credited.
    """
    rows = []
    for iid in player.party_instance_ids():
        u = player.roster_row(iid)
        if u is None:
            continue
        lv, ex = int(u.get("level", 1)), int(u.get("exp", 0))
        lbl, lbe = int(u.get("lb_level", 0) or 0), int(u.get("lb_exp", 0) or 0)
        # Forward walk, not a cumulative re-derivation: `exp` is a residual within the level and the
        # client's `getLv()` is a bare field load, so the only way to know the post-battle level is to
        # credit the exp the way the client reads the table. See `player_state.credit_exp`.
        new_lv, new_ex = credit_exp(u["master_id"], lv, ex, int(exp_gain))
        before = _lv_half(lv, ex, unit_stats(u["master_id"], lv), lbl, lbe)
        after = _lv_half(new_lv, new_ex, unit_stats(u["master_id"], new_lv), lbl, lbe)
        rows.append(f"{iid}@{before}_{after}")
    return ",".join(rows)
def per_unit_exp(total_exp) -> tuple[int, int, int]:
    """`S4U09svH` is a PARTY TOTAL; each member's share is `total // n`. Returns `(per_unit, n, remainder)`.

    WHY THE SPLIT: the client reports one number for the whole battle — it sums the enemies' exp — and the
    game's own rule (operator's community research, 2026-09-26) is that the total is **divided among the
    group's members**. Crediting the full total to each member over-paid by the party size: 197,094 sent
    for one chamber, 5 members, 39,418 owed each, 197,094 granted each.

    **The divisor counts EVERY member, including one already at `max_level`** — that is the reported rule,
    and it also makes the share derivable from the state alone (no cap lookups, so no chance of the reply
    and the roster disagreeing about who was in the party).

    Integer division FLOORS, so up to `n-1` exp is lost per battle. That is stated rather than hidden, and
    the remainder is returned so the caller can log it; distributing it would be an invention.

    ONE definition, called from BOTH the reply echo (`kcb0tsM6`) and the roster credit, so the number the
    client is shown and the number we store cannot diverge.
    """
    n = len(State.player.party_instance_ids()) or 1
    per = int(total_exp) // n
    return per, n, int(total_exp) - per * n
def mission_end_echo(spec, plaintext: str | None):
    """Make the mission-end reply carry the values the CLIENT computed and reported.

    `MissionEndRequest` sends its own `09HRWXDf` group: `S4U09svH` (unit EXP earned), `Wdi3MAs2`
    (gil earned) and `xF9Sr1a6` (the drops it saw). `MissionResultResponse`'s ctor CLEARS the
    client's result state, so the reply MUST carry those numbers back -- and the client's own
    computation is the authoritative, derivable source (it sums the enemies' exp: 1110101's
    first-visit 3x Dire Rat = 12). Hardcoding them is what read `EXP +1` where the archive shows
    `+12`. This overrides `kcb0tsM6`/`16LQuC5r`/`xF9Sr1a6` and rewrites `Qic38SuC`'s `f12` to match.
    """
    if not isinstance(spec, dict) or not plaintext:
        return spec
    try:
        # See mission_id_from: the trailing manual-pad byte is variable, so raw_decode, not rstrip.
        req = json.JSONDecoder().raw_decode(plaintext)[0]
    except Exception as exc:  # noqa: BLE001
        _note(f"mission_end_echo: request body did not parse as JSON ({exc!r}); reply left "
              f"UNMODIFIED, so the client's own EXP/gil/drops are NOT carried back")
        return spec
    rows = req.get("09HRWXDf")
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return spec
    src = rows[0]
    exp, gil, drops = src.get("S4U09svH"), src.get("Wdi3MAs2"), src.get("xF9Sr1a6")
    if exp is None and gil is None and not drops:
        return spec
    per_unit = per_unit_exp(exp)[0] if exp is not None else None
    out = copy.deepcopy(spec)
    for row in out.get("09HRWXDf", []):
        if not isinstance(row, dict):
            continue
        if per_unit is not None:
            # The PER-UNIT share, not the party total: `kcb0tsM6` is the "actual unit experience" the
            # result screen shows, and it must agree with `Qic38SuC`'s per-unit a[12] below.
            row["kcb0tsM6"] = str(per_unit)
        if gil is not None:
            row["16LQuC5r"] = str(gil)
        if drops:
            row["xF9Sr1a6"] = drops
        # NOTE: `Qic38SuC` is NOT set here any more. It must carry the party's REAL instance ids and their
        # real before/after params, so it is built from the player state in `credit_mission_rewards` (once
        # the exp has been credited). See mission_lv_info.
    return out
def credit_mission_rewards(plaintext: str | None, spec=None):
    """Make the battle's gil, loot and RANK POINTS real in the player state (schema §2.3).

    Until now the MissionEnd reply was only ECHOED back by `mission_end_echo`, so the result screen showed
    the gil, items and rank points while nothing recorded them -- all three were display-only
    (`player_state_schema.md` §4, SW-63). This credits what the client reported:

      * `Wdi3MAs2` = gil earned      -> `add_currency("gil", …)` (served as `7UR4J2SE` next refresh)
      * `xF9Sr1a6` = `cat:item:count[,…]`  (the 3-field `addItem(20,0,csv)` shape) -> `grant_item`
      * **`wuUakq71` (from the REPLY spec, not the request) = rank points for THIS battle** ->
        `add_rank_exp` (served as `B6H34Mea` on `3oU9Ktb7` next refresh). The reply grants the delta; the
        client applies it locally. Crediting it is what lets rank actually advance -- without it the next
        user-state reply resets the client's rank progress to the state's 0, so no rank is ever reached.

    ONE MissionEnd request per battle end, so one credit. A **retried** request WOULD credit twice: no
    per-battle nonce reaches the wire (`jQsE54Iz` carries only the mission id, and a repeat clear is
    legitimate, so the mission id cannot be the key). Recorded rather than silently assumed. `frozen`
    refuses and `ephemeral` keeps the credit run-local, like every other op.
    Returns the spec to carry forward. It is a TRANSFORM like every other in the chain, and it has to be
    one: it writes `Qic38SuC` and `GfQmi4h5` into the `09HRWXDf` rows, and a shallow `dict(spec)` from an
    earlier transform does not protect the rows inside those lists. `mission_end_echo` returns the SHARED
    `State.replies` entry uncopied on all five of its early-return paths, so without this copy a rank-up
    on a request that echoed nothing would write a battle-specific bonus into the loaded payload and it
    would stick for the rest of the session.
    """
    if not plaintext:
        return spec
    try:
        req = json.JSONDecoder().raw_decode(plaintext)[0]
    except Exception as exc:  # noqa: BLE001 - a body that will not parse must never break the reply
        _note(f"credit_mission_rewards: request body did not parse ({exc!r}); rewards NOT credited")
        return spec
    rows = req.get("09HRWXDf")
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return spec
    src = rows[0]
    if isinstance(spec, dict):
        spec = copy.deepcopy(spec)   # every write below lands on a private copy, never the shared payload
    paid: list[str] = []          # only what ACTUALLY changed -- `frozen` refuses and must log nothing
    lvl_before = int(State.player.data["rank"].get("level", 1))
    gil = src.get("Wdi3MAs2")
    if gil is not None and str(gil).isdigit() and int(gil) > 0:
        before = int(State.player.currencies.get("gil", 0))
        if State.player.add_currency("gil", int(gil)) != before:
            paid.append(f"{int(gil)}gil")
    for entry in str(src.get("xF9Sr1a6") or "").split(","):
        f = entry.split(":")
        if len(f) < 3 or not f[0].isdigit() or not f[1].isdigit() or not f[2].isdigit():
            continue
        before = State.player.item_count(int(f[0]), f[1])
        if State.player.grant_item(int(f[0]), f[1], int(f[2])) != before:
            paid.append(f"{f[0]}:{f[1]}x{f[2]}")
    # Rank points come from the REPLY (the request never carries them): `wuUakq71` is the delta granted
    # for this battle. Credit the total into `rank.exp` so the next `3oU9Ktb7` agrees with the client.
    if isinstance(spec, dict):
        rrows = spec.get("09HRWXDf")
        if isinstance(rrows, list) and rrows and isinstance(rrows[0], dict):
            pts = rrows[0].get("wuUakq71")
            if pts is not None and str(pts).isdigit() and int(pts) > 0:
                exp_before = int(State.player.data["rank"].get("exp", 0))
                if int(State.player.add_rank_exp(int(pts)).get("exp", 0)) != exp_before:
                    paid.append(f"{int(pts)}rankpt")
    # UNIT EXP -> the roster, and `Qic38SuC` built from it (docs/notes/result_scene_selection.md §7).
    # Build the string BEFORE crediting: its BEFORE half is the current state, its AFTER half the gain.
    uexp = src.get("S4U09svH")
    if uexp is not None and str(uexp).isdigit() and int(uexp) > 0 and isinstance(spec, dict):
        gain, n_members, remainder = per_unit_exp(uexp)
        # The party's TOTAL is logged alongside the share, so a run's exp can be re-derived from the log
        # alone -- and so an over-credit (the pre-2026-09-26 bug) is visible in the transcript rather
        # than only in the roster's levels.
        print(f"unit-exp: party total {int(uexp)} over {n_members} member(s) -> {gain} each"
              + (f" (remainder {remainder}, floored)" if remainder else ""), flush=True)
        lv_info = mission_lv_info(State.player, gain) if gain > 0 else ""
        for iid in State.player.party_instance_ids():
            u = State.player.add_unit_exp(iid, gain)
            if u is not None and int(u.get("level", 1)) > 1:
                paid.append(f"unit{u['master_id']}->Lv{u['level']}")
        for r in spec.get("09HRWXDf", []):
            if isinstance(r, dict) and lv_info:
                r["Qic38SuC"] = lv_info
        # The runtime's own confirmation line, so "served" and "not served" cannot be confused. The
        # note's open question (result_scene_selection.md §9) is whether ResultUnitExpScene is REACHED or
        # reached-and-empty, and the reply half of that question is exactly this string going out.
        if lv_info:
            ids = State.player.party_instance_ids()
            print(f"unit-exp: served Qic38SuC for {len(ids)} unit(s), +{gain} each: {lv_info[:110]}",
                  flush=True)
    if paid:
        rank = State.player.data["rank"]
        items = State.player.data.get("inventory", {}).get("items", [])
        print(f"rewards: credited {' '.join(paid)} -> gil={State.player.currencies.get('gil', 0)} "
              f"bag={sum(int(i.get('count', 0)) for i in items)} item(s) over {len(items)} kind(s) "
              f"rank={rank.get('level')}({rank.get('exp')}pt)", flush=True)
        if int(rank.get("level", 1)) > lvl_before:
            # THE RANK-UP BONUS -- the one thing that was missing from a rank-up, and the display reads it
            # from THIS reply. See `RANKUP_BONUS_LAPIS` for why the token is proven and the number authored.
            # Credit it as well as serve it: an award the balance does not show is a lie the next user-state
            # reply tells, which is the same rule the challenge awards follow.
            gained = int(rank.get("level", 1)) - lvl_before
            bonus = RANKUP_BONUS_LAPIS * gained
            if bonus > 0:
                if isinstance(spec, dict):
                    for r in spec.get("09HRWXDf", []):
                        if isinstance(r, dict):
                            r["GfQmi4h5"] = str(bonus)
                State.player.add_currency("lapis_free", bonus)
                paid.append(f"{bonus}rankup-lapis")
            print(f"rewards: RANK UP {lvl_before} -> {rank.get('level')} "
                  f"(bonus {bonus} lapis served as GfQmi4h5)", flush=True)
    return spec
_CHALLENGE_TABLE: dict | None = None
def challenge_table() -> dict:
    """mission id -> {challenge id: (lapis amount, condition spec, display order)} from the SHIPPED table.

    `dX6cor8j` (`ChallengeMst::setRewardInfo`; community lexicon `ChallengeReward`) is a comma list of
    `kind:id:count`, `V36ygRYs` is `ChallengeId`, and `Pzn5h0Ga` (`ChallengeMst::setCondInfo`) is the
    condition spec whose FIRST field is the condition TYPE. Amounts are the game's own numbers, never
    authored by us. For 1110101 the four rows are `50:1:20` + 3x `50:1:10` -- the 20+10+10+10 the
    challenge screen shows.

    EVERY spec-bearing row is included, LAPIS OR NOT: **24,138 of the 42,758 shipped rows carry no
    lapis** (measured), and a mission's "complete" icon derives from the CLEARED set, not the paid set
    (`docs/notes/mission_flag_predicate.md` §7). `amount` is 0 for a no-lapis row; a row with no spec is
    skipped, because it can never be witnessed as met.

    ALSO carries `Z0EN6jSh` (the challenge's ORDER within the mission). NOTE: the clear-info list
    (`qdhWF7z9`) holds the 8-digit `V36ygRYs` **ids**, not these orders -- resolved statically in SW-31(c);
    an earlier "orders" reading was wrong and is not what `serve_challenge_clear` serves.
    """
    global _CHALLENGE_TABLE
    if _CHALLENGE_TABLE is not None:
        return _CHALLENGE_TABLE
    out: dict = {}
    try:
        with open(CHALLENGE_MST, encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                mid, cid = row.get("qo3PECw6"), row.get("V36ygRYs")
                rew, spec = row.get("dX6cor8j") or "", row.get("Pzn5h0Ga") or ""
                if not mid or not cid:
                    continue
                try:
                    order = int(row.get("Z0EN6jSh") or -1)
                except ValueError:
                    order = -1
                amount = 0
                for part in rew.split(","):
                    f = part.split(":")
                    if len(f) >= 3 and f[0] == LAPIS_REWARD_KIND:
                        try:
                            amount += int(f[2])
                        except ValueError:
                            pass
                if not spec:
                    continue   # no condition -> can never be witnessed as met
                out.setdefault(str(mid), {})[str(cid)] = (amount, spec, order)
    except FileNotFoundError:
        _note(f"challenge_table: {CHALLENGE_MST} not found -- no challenge can be recorded")
    _CHALLENGE_TABLE = out
    return out
def party_size() -> int:
    """Units in the active party, counted from the ONE player state -- the server owns the party.

    The party groups (`8PEB5o7G` / `5Eb0Rig6`) are a PROJECTION of `State.player` (`apply_to_reply`), not
    payload literals: reading them back out of `State.replies` returned 0 on EVERY run once SW-62 removed
    the literals, which silently dropped every `34:` (party-size) challenge. (`r21y0YzS` is also a
    transposition trap -- lowercase `y`, then uppercase `Y` -- that already cost one silent zero.) Each
    miss REPORTS, so a typo cannot hide behind a plausible-looking zero.
    """
    p = State.player
    if p is None:
        _note("party_size: no player state loaded -- party-size challenges cannot be evaluated")
        return 0
    party = p.data.get("party") or {}
    active = str(party.get("active_deck"))
    for deck in party.get("decks") or []:
        if str(deck.get("id")) == active:
            return len([m for m in (deck.get("members") or [])
                        if str(m.get("instance_id") or "").strip()])
    _note(f"party_size: no deck row carries the active deck id {active!r} -- cannot be evaluated")
    return 0
def mission_result_from(plaintext: str | None) -> dict:
    """The client's own mission-end report, as the context the condition evaluators read."""
    ctx = {"cleared": None, "dead_count": None, "continue_count": None, "party_size": party_size(),
           "group_present": False, "used_item": None, "used_magic": None, "used_lb": None,
           "turns": None}
    if not plaintext:
        return ctx
    try:
        req = json.JSONDecoder().raw_decode(plaintext)[0]
    except Exception:  # noqa: BLE001
        _note("mission_result_from: request body did not parse as JSON; no condition data available")
        return ctx
    if not isinstance(req, dict):
        return ctx

    def first_row(group):
        rows = req.get(group)
        return rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}

    meta = first_row(CHALLENGE_GROUP)
    # The group must be PRESENT before "absent" can mean anything -- a request that sent no battle log
    # would otherwise pass every "pass iff absent" check. Group present 61/61 in the recorded corpus.
    ctx["group_present"] = bool(meta)
    if BATTLE_CLEAR_TOKEN in meta:
        rows = [r for r in str(meta[BATTLE_CLEAR_TOKEN]).split(",") if r.strip()]
        ctx["cleared"] = bool(rows)
        turns = []
        for row in rows:
            f = row.split(":")
            if len(f) >= 3:
                try:
                    turns.append(int(f[2]))
                except ValueError:
                    pass
        if len(turns) == len(rows):
            # Sum, not max: the aggregation (per wave vs whole mission) is not pinned, and for a lapis
            # credit the SAFE error is to deny. See docs/notes/challenge_conditions.md.
            ctx["turns"] = sum(turns)
    if "Z1p0j9uF" in meta:
        try:
            ctx["dead_count"] = int(meta["Z1p0j9uF"])
        except (TypeError, ValueError):
            pass
    if ctx["group_present"]:
        ctx["used_item"] = USE_ITEM_TOKEN in meta
        ctx["used_magic"] = USE_MAGIC_TOKEN in meta
        ctx["used_lb"] = USE_LB_TOKEN in meta
    tail = first_row("L4i4418y")
    if "6q35TL07" in tail:
        try:
            ctx["continue_count"] = int(tail["6q35TL07"])
        except (TypeError, ValueError):
            pass
    return ctx
def _cond_clear(ctx, _param):
    return ctx["cleared"]
def _cond_no_ally_ko(ctx, _param):
    return None if ctx["dead_count"] is None else ctx["dead_count"] == 0
def _cond_no_continue(ctx, _param):
    return None if ctx["continue_count"] is None else ctx["continue_count"] == 0
def _cond_party(ctx, param):
    try:
        want = int(param) + 1
    except (TypeError, ValueError):
        want = 1
    return None if not ctx["party_size"] else ctx["party_size"] >= want
def _cond_no_items(ctx, _param):
    """type 1: no item used. `None` (cannot judge) unless the group is present AND the absence of the
    token has been witnessed on a run that really used an item -- "absent => zero" is mechanically true,
    but an unwitnessed token name that happens to be absent would silently OVER-credit."""
    if not ctx["group_present"] or ctx["used_item"] is None:
        return None
    return None if not ABSENCE_WITNESSED else not ctx["used_item"]
def _cond_no_magic(ctx, _param):
    """type 6: no magic used. Same guard as _cond_no_items."""
    if not ctx["group_present"] or ctx["used_magic"] is None:
        return None
    return None if not ABSENCE_WITNESSED else not ctx["used_magic"]
def _cond_used_lb(ctx, _param):
    """type 16: use a limit burst. "present => used", so a wrong token name can only UNDER-credit --
    no witness needed, unlike the two above."""
    if not ctx["group_present"] or ctx["used_lb"] is None:
        return None
    return bool(ctx["used_lb"])
def _cond_turns_within(ctx, param):
    """types 73 (and the same shape in 75/76/77): clear within `param` turns.

    Type 73's parameter IS the turn threshold (`73:5` 180 rows, `73:10` 179, `73:3` 84), and the turn
    count is the third field of each `69ieJGhD` row -- `BattleClearLog::getTurnNum()` (vtable slot +0x40),
    fed from `BattleState::playerTurnCnt` (+0x108). The AGGREGATION is not pinned (per wave vs whole
    mission), so this sums: for a credit, the safe error is to deny.
    """
    try:
        limit = int(param)
    except (TypeError, ValueError):
        return None
    if ctx["turns"] is None:
        return None
    return ctx["turns"] <= limit
CONDITION_CHECKS = {
    "1": _cond_no_items,      # no items used          (guarded, see _cond_no_items)
    "6": _cond_no_magic,      # no magic used          (guarded)
    "16": _cond_used_lb,      # use a limit burst
    "33": _cond_no_ally_ko,   # nobody KO'd
    "34": _cond_party,        # party >= param + 1
    "38": _cond_no_continue,  # no continues
    "68": _cond_clear,        # complete the quest
    "73": _cond_turns_within,  # clear within `param` turns
}
def credit_mission_lapis(spec, mid: str | None, plaintext: str | None = None):
    """Record the challenges this mission CLEARED, and pay the first clear's lapis.

    Two jobs on one pass, deliberately NOT the same set:
      * `progress.cleared_challenges` -- EVERY spec-bearing challenge we can WITNESS as met, lapis or not.
        This is the cumulative set `serve_challenge_clear` serves as `qdhWF7z9`, and the client derives a
        mission's "complete" icon from it (`GateMissionScene` -> `UserChallengeInfoList::isClear` for every
        displayed challenge -- `docs/notes/mission_flag_predicate.md` §7).
      * `progress.paid_challenges` -- the FIRST-CLEAR payment gate, lapis-bearing challenges only.

    `LAPIS_AWARD` gates the currency, never the cleared record: a no-lapis challenge must still mark. Only
    conditions we can actually EVALUATE are witnessed (see CONDITION_CHECKS); an unevaluated one is named
    in a note instead of being recorded, so a shortfall is visible rather than silent.
    """
    if not isinstance(spec, dict) or RESULT_TAG not in spec or not mid:
        return spec
    table = challenge_table().get(str(mid), {})
    # Read with `.get`, NOT `setdefault`: a bare setdefault writes an empty set into the state on every
    # mission-end, which bypasses `_mutate` and so mutates even under `frozen` -- the one policy that
    # promises no mutation, and whose transcript is the evidence record for the A/B it is running.
    cleared = State.player.cleared_challenges.get(str(mid), set())
    paid = State.player.paid_challenges.get(str(mid), set())
    cleared_before = len(cleared)
    ctx = mission_result_from(plaintext)
    fresh, unmet, unknown = {}, [], []
    for cid, (amount, cond, _order) in sorted(table.items()):
        # Skip anything already settled. A LAPIS challenge that was cleared while the award was off is NOT
        # settled -- it has not been paid -- so re-evaluate it and pay it now.
        if cid in cleared and (not amount or cid in paid):
            continue
        ctype, _, param = cond.partition(":")
        check = CONDITION_CHECKS.get(ctype)
        if check is None:
            unknown.append(f"{cid}(type {ctype})")
            continue
        verdict = check(ctx, param)
        if verdict is True:
            if amount and LAPIS_AWARD:
                fresh[cid] = amount          # recorded by `credit_challenge` below (cleared + paid)
            else:
                State.player.mark_challenge_cleared(str(mid), cid)
        elif verdict is False:
            unmet.append(f"{cid}(type {ctype})")
        else:
            unknown.append(f"{cid}(type {ctype})")
    if unknown:
        _note(f"mission {mid}: condition NOT evaluated, so NOT recorded: {', '.join(unknown)}")
    if unmet:
        print(f"    note: mission {mid}: challenge NOT met: {', '.join(unmet)}", flush=True)
    cleared_no_reward = len(cleared) - cleared_before
    if not fresh:
        print(f"    award: +0 lapis (mission {mid}) -- {len(table)} challenge(s): "
              f"{len(cleared)} cleared ({cleared_no_reward} with no reward), "
              f"{len(unmet)} not met, {len(unknown)} unevaluated "
              f"-> free={State.player.currencies['lapis_free']}", flush=True)
        return spec
    # A lapis challenge records CLEARED and PAID together through the op.
    free_before = int(State.player.currencies.get("lapis_free", 0))
    for cid in fresh:
        State.player.credit_challenge(str(mid), cid)
    amount = sum(fresh.values())
    State.player.add_currency("lapis_free", amount)
    State.state_dirty = True
    # Say what actually HAPPENED. `frozen` refuses the op and the balance does not move, so the old
    # "+N lapis -> free=X" line claimed an award the player never received -- a transcript that lies
    # in precisely the run whose log is the evidence.
    moved = int(State.player.currencies.get("lapis_free", 0)) - free_before
    outcome = "" if moved == amount else f"  ** REFUSED (state mode={State.player.mode}, balance unmoved) **"
    print(f"    award: +{amount} lapis (mission {mid}, cleared {len(fresh)} "
          f"[{','.join(sorted(fresh))}], {cleared_no_reward} more with no reward, kind {LAPIS_REWARD_KIND}) -> "
          f"free={State.player.currencies['lapis_free']} paid={State.player.currencies['lapis_paid']}"
          f"{outcome}", flush=True)
    return spec
def serve_challenge_clear(spec, mid: str | None, prior: set | None = None):
    """Tell the client which of this mission's challenges are cleared -- and which were cleared by THIS battle.

    `TQ5AJd2K` = `UserChallengeInfoResponse` (`readParam` `0x1924928`), three tokens:

      `qo3PECw6` -> MissionId            `+0x24`
      `qdhWF7z9` -> ClearInfo            `+0x28`  the cumulative cleared set for this mission
      `wf5Toh4k` -> ProvisionalClearInfo `+0x40`  `setProvisionalClearInfo` `0x1d9d2ec`

    CONVENTION = the challenge **IDS** (`V36ygRYs`), NOT the `Z0EN6jSh` order and NOT 1-based. `isClear(int)`
    (`0x1d9d06c`, via `UserChallengeInfoList::isClear(ii)` `0x1d9ecc8`) is called with `ChallengeMst+0x24`, which
    `ChallengeMstList::parseObject` (`0x1d0d95c`) fills from `V36ygRYs` -- so an ORDER list can never match.
    (`challenge_table` still carries `Z0EN6jSh` as its 3rd element; it is NOT what these lists hold. An earlier
    revision of this function served orders on that mistaken reading -- see the matrix below.)

    WHAT DRIVES THE STAR ANIMATION: `MissionResultChallengeScene::setLayout` (`0x2374a84`) calls
    `UserChallengeInfoList::isClear(lastMissionId, challengeId)` at `0x2374e6c` and `cinc`s a state of
    **2 = just cleared** / **3 = already achieved** / else 0 into `ChallengeAchieveObj+0xc0`; `updateEvent`
    (`0x2373e28`, `0x2373f98`) fires `startAchievedEffect("mission_result_challenge_star_on")` **only for
    state 2**. So the animation needs ClearInfo TRUE for this mission while the client's PRE-battle snapshot
    (`isAlreadyAchievedChallenge` `0x1e17758`, taken in `MissionEndScene::initConnect` BEFORE `accessPhp`) said
    false. The cumulative set gives exactly that, because the snapshot is taken while it is still empty.
    `wf5Toh4k` is NOT on this path (its readers are `MissionChallengeScene::setMissionChallengeLayout`,
    `RmMissionResultChallengeScene::getChallengeState`, `RmDungeonMapManager::setChallengeLayout`) -- served
    because it is correct, not because it animates.

    THE ROW'S MISSION ID MATTERS: the lookup is keyed on `UserState+0x140` (lastMissionId), so a row keyed to a
    different mission can never match. The payload carries one authored row (mission 1110101); we rewrite
    `qo3PECw6` to `mid`, so every mission's own end gets a row for that mission.

    EMPIRICAL MATRIX (one fresh run per row, one variable at a time -- do not collapse these):
      (1) ClearInfo = the four 8-digit IDs (this convention) -> still no animation observed, BUT see below
      (2) ClearInfo = "" (pre-battle) + Provisional = "0,1,2,3" -> no animation, and (2) also served the wrong
          convention. (2) is now known to be doubly wrong.
    Lapis was credited correctly in both, so the award was never in question. Static analysis (not a run) is
    what resolved the convention; the row-vs-mission mismatch is the remaining suspect for (1).
    """
    if not CLEAR_INFO_FROM_STATE or not isinstance(spec, dict) or mid is None:
        return spec
    rows = spec.get("TQ5AJd2K")
    if not isinstance(rows, list) or not rows:
        return spec
    now = set(State.player.cleared_challenges.get(str(mid), set()))
    fresh = now - set(prior or set())
    out = copy.deepcopy(spec)
    for row in out["TQ5AJd2K"]:
        if isinstance(row, dict):
            row["qo3PECw6"] = str(mid)                 # the result scene keys on lastMissionId
            row["qdhWF7z9"] = ",".join(sorted(now))    # cumulative -> state 2 on a first clear
            row["wf5Toh4k"] = ",".join(sorted(fresh))  # provisional (not this path)
    return out
def serve_challenge_clear_all(spec, reply_id: str | None = None):
    """Attach `UserChallengeInfoResponse` (`TQ5AJd2K`) carrying EVERY mission's cleared set to a user-state reply.

    WHY IT EXISTS. `serve_challenge_clear` can only fill a group the payload ALREADY has, and in the recorded
    corpus exactly ONE reply carries `TQ5AJd2K` -- `MissionEndRequest`. The mission LIST's "complete" state
    (`GateMissionScene::setLayoutMissionList`, `mission_flag_predicate.md` §7) evaluates
    `UserChallengeInfoList::isClear(missionId, challengeId)` for every DISPLAYED challenge, so the client can
    only draw the mini stars / `quest_complete.png` for a mission it finished THIS session. A restart (crash,
    `--fresh`, a cleared app dir) came back with an empty list, and the client never re-asks: the missions read
    **cleared but never complete**, reported live 2026-09-22 with the Switches side already correct.

    Both halves are server-authoritative, but they reach the client by different routes: `progress.switches`
    rides the switch container (`serve_switches`), which the client DOES receive on `GetBackgroundDownloadInfo`
    / `UpdateSwitchInfo` / `MissionEnd`. The challenge set had no such route at all.

    The mechanism is `serve_diamonds`'s: the group rides the replies that already refresh user state, so it
    lands at login and on the post-battle re-login chain (GameSetting -> GetBackgroundDownloadInfo ->
    Initialize -> GetUserInfo). It skips any reply that already carries the group -- that is the mission-end
    reply, whose `wf5Toh4k` (provisional = "cleared by THIS battle") drives the star animation and must not be
    clobbered. It is ALSO restricted to `CHALLENGE_INFO_ALLOW`; see the guard in the body for why.

    `qdhWF7z9` is the cumulative set as `V36ygRYs` challenge IDS (see `serve_challenge_clear` for why the ids
    and not the `Z0EN6jSh` orders). A mission carrying no challenge rows in the shipped `F_CHALLENGE_MST`
    (1110100 is one) gets no row: with nothing displayed, `w25` can never be true and it can only ever be
    `quest_clear`, which is correct and not a gap.
    """
    if not CHALLENGE_INFO_ALL or not isinstance(spec, dict) or "TQ5AJd2K" in spec:
        return spec
    # NARROW BY REPLY ID, and this is not tidiness -- it is the fix to a real fault. The first version rode
    # EVERY reply carrying a `USER_STATE_GROUPS` member, which put the group on `GetReinforcementInfoRequest`
    # and `FriendListRequest` in the result flow, and the client died ~30 s later (2026-09-22, rank-up). That
    # is a candidate, not a proven cause: the same window is also the first rank-up the server has ever
    # driven. Narrowing removes it from the battle/result path while keeping the display data on the
    # state-refresh chain, so the retry tests ONE variable.
    if not CHALLENGE_INFO_ANY and (not reply_id or reply_id not in CHALLENGE_INFO_ALLOW):
        return spec
    if not any(g in spec for g in USER_STATE_GROUPS):
        return spec
    rows = [{"qo3PECw6": str(mid), "qdhWF7z9": ",".join(sorted(ids)), "wf5Toh4k": ""}
            for mid, ids in sorted(State.player.cleared_challenges.items()) if ids]
    if not rows:
        return spec
    out = copy.deepcopy(spec)
    out["TQ5AJd2K"] = rows
    # The runtime's own confirmation line, ONCE per reply type: a silent attach and a failed attach look
    # identical in the client, and this is what made SW-87's fix checkable rather than merely observed.
    if reply_id and reply_id not in _CHALLENGE_INFO_LOGGED:
        _CHALLENGE_INFO_LOGGED.add(reply_id)
        print(f"challenge-info: attached TQ5AJd2K ({len(rows)} row(s)) to {reply_id}", flush=True)
    return out
def _set_switch_bit(container: str, index: int) -> str:
    """Set `index` in a switch container: 4 bits per hex character, MSB first inside each character."""
    k, rem = divmod(index, 4)
    chars = list(container)
    while len(chars) <= k:
        chars.append("0")
    chars[k] = "0123456789abcdef"[int(chars[k], 16) | (8 >> rem)]
    return "".join(chars)
def _switch_container(spec, stype: str) -> str | None:
    """The container to serve for ONE switch type, from tracked state, at the width that reply already uses.

    Returns None when we track nothing for `stype`, which is what keeps an unknown type (the huge `9000`
    record, say) byte-identical -- only types we actually model are ever rewritten.
    """
    # DISTINGUISH "type not tracked at all" (None) from "tracked but EMPTY" (serve an all-zero container).
    # `if not tracked` conflated the two: with `SWITCH_BASE_INDICES=` the tracked set is empty, so this
    # returned None, and `serve_switches` then LEFT the payload's static type-101 container in place — which
    # still has bit 117 (10001100, mission 1110100's clear flag) set. The client then believed the opening
    # was already cleared and dropped straight to the world map. `SWITCH_BASE_INDICES=0` masked it because
    # a one-element set is truthy. Found 2026-09-20.
    if stype not in State.player.switches:
        return None
    tracked = State.player.switches[stype]
    width = SWITCH_CONTAINER_MIN
    for rows in spec.values() if isinstance(spec, dict) else ():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and str(row.get(SWITCH_TYPE_FIELD)) == stype:
                width = max(width, len(str(row.get(SWITCH_BITS_FIELD) or "")))
    container = "0" * width
    for i in tracked:
        container = _set_switch_bit(container, i)
    return container
def grant_cleared_mission(spec, mid: str | None):
    """Record that `mid` was cleared (and grant its own switch) -- MISSION-END reply only.

    Only the MISSION-END reply may grant (gated on `RESULT_TAG`): the request that merely *names* a mission
    (e.g. `DungeonResourceLoadMstListRequest`) must not mark it cleared. The mission -> switch mapping and
    the recording itself are the `clear_mission` OP (`player_state.py`, schema §2.3) -- one home for that
    fact; this wrapper owns only the reply gate and the log. The container rewrite is `serve_switches`.
    """
    if not isinstance(spec, dict) or not mid or RESULT_TAG not in spec:
        return spec
    mid = str(mid)
    # --- the story's ESPER, when this beat is an esper beat. `player_state.story_beast_for_mission` derives it
    # from the mission's own switch list (`820<EE>100`, 19 shipped beats whose location names agree with the
    # esper they grant), so this is the GAME's rule rather than an authored one. Ownership is the SERVER's to
    # state -- the client only renders `UserBeastInfoResponse` -- and `owns_beast` guards the re-clear case,
    # because `grant_beast` is idempotent by id but rewrites rarity/level. Placed BEFORE the switch branch's
    # early return so an unrelated switch outcome can never suppress the esper.
    beast = story_beast_for_mission(mid)
    if beast is not None and not State.player.owns_beast(beast):
        rarity, level = story_beast_stats(beast)
        State.player.grant_beast(beast, rarity=rarity, level=level)
        State.state_dirty = True
        print(f"    beast: mission {mid} cleared -> granted esper {beast} at R{rarity} L{level}", flush=True)
    had = set(State.player.switches.get(SWITCH_TYPE_MISSION, set()))
    sw = State.player.clear_mission(mid)
    if sw is None:
        if mission_open_switch(mid):
            _note(f"grant_cleared_mission: mission {mid} granted no type-{SWITCH_TYPE_MISSION} switch "
                  f"(dIPkNn61={mission_open_switch(mid)!r}; the members' individual reasons are printed by "
                  f"clear_mission)")
        return spec
    # A mission's `dIPkNn61` is a COMMA LIST, so diff the type's set instead of mapping the returned ids:
    # the log then names every bit this clear actually placed, and nothing claims a member it skipped.
    new = sorted(set(State.player.switches.get(SWITCH_TYPE_MISSION, set())) - had)
    if new:
        State.state_dirty = True
        print(f"    switch: mission {mid} cleared -> granting {sw} (type {SWITCH_TYPE_MISSION} "
              f"index {new})", flush=True)
    return spec
def record_pushed_switches(plaintext: str | None) -> None:
    """Accumulate the switches the CLIENT has granted to ITSELF, so our container stops erasing them.

    `UpdateSwitchInfoRequest` carries `0jqUZuT1[].dIPkNn61`; the lexicon names that field **GrantedSwitchId**
    (its sibling `juA0Z4m7` is RequiredSwitchId), so this is the client *reporting what it has earned* --
    `tutorial_skip_lever.md` §2.2 shows it pushing `TUTORIAL_END_SWITCH_ID` itself. There are **207** such
    requests in the recorded corpus: `10000152` 94x, `10000111` 54x, `10000140,10000152,90001838` 23x,
    `10000140,90001838` 14x, then `10000230`, `10000132`, `10002002`, `80011002`, ...

    This mattered more than "we did not grant them": the reply container **replaces the whole switch TYPE**
    (`UserSwitchInfo::setObject`), so a static container did not merely fail to add a reported switch -- it
    **wiped** it on the next sync. `update_switch_info_response.md` already established the group is
    one-directional (echoing it back is a no-op), which leaves the container as the only way to honour it.
    """
    if not plaintext:
        return
    try:
        req = json.JSONDecoder().raw_decode(plaintext)[0]
    except Exception:  # noqa: BLE001
        _note("record_pushed_switches: request body did not parse as JSON; client-granted switches dropped")
        return
    rows = req.get(PUSHED_SWITCH_GROUP) if isinstance(req, dict) else None
    if not isinstance(rows, list):
        return
    for row in rows:
        if not isinstance(row, dict):
            continue
        for raw in str(row.get(PUSHED_SWITCH_FIELD) or "").split(","):
            sid = raw.strip()
            if not sid:
                continue
            pos = switch_position(sid)
            if not pos:
                _note(f"record_pushed_switches: {sid} has no F_SWITCH_MST row -- cannot place its bit")
                continue
            stype, idx = pos
            got = State.player.switches.setdefault(stype, set())
            if idx not in got:
                got.add(idx)
                State.state_dirty = True
                print(f"    switch: client granted {sid} -> recorded (type {stype} index {idx})", flush=True)
VERSION_TABLE: list = [None, {}]
def version_table() -> dict:
    """`F_XXX` -> version, from the SHIPPED client's own persisted table.

    The prefs key is `keyF_XXX`; the response token `a4hXTIm0` is that name WITHOUT the `key` prefix
    (the client sends `F_APP_VERSION_AND` where the prefs hold `keyF_APP_VERSION_AND`).
    """
    try:
        mtime = os.path.getmtime(VERSION_TABLE_XML)
    except OSError as exc:  # noqa: BLE001
        _note(f"version table: cannot read {VERSION_TABLE_XML} ({exc}) -- serving the reply's own rows")
        return {}
    if VERSION_TABLE[0] != mtime:
        with open(VERSION_TABLE_XML, encoding="utf-8") as fh:
            text = fh.read()
        rows = {}
        for name, ver in re.findall(r'<string name="key(F_[A-Za-z0-9_]+)">(-?\d+)</string>', text):
            if int(ver) >= 1:  # the client DROPS a row with version < 1 (mst_version_source.md SS3)
                rows[name] = ver
        VERSION_TABLE[0], VERSION_TABLE[1] = mtime, rows
    return VERSION_TABLE[1]
def serve_version_table(spec):
    """Answer `c1qYg84Q` with the WHOLE per-table table, not only the handful we hand-authored.

    Rows already in the payload WIN, because they are deliberate: `F_DEFINE_MST=227` is what makes the
    client fetch our patched `Ver227_zg5P1AxM.dat` (the shipped prefs say 224). Everything else comes
    from the shipped `gamedata.xml`. An unregistered name is inert -- `VersionInfo::setVersion` only
    updates entries the client already registered -- so an over-long table is safe.

    `FORCE_TABLE_VERSIONS` (`server/ffbe_config.py`) is applied LAST and only to the tables it names,
    because it is a fix rather than a preference. A table the client BELIEVES it has but does not is the
    worst kind of missing data: the reply advertises the shipped version, the client never fetches, and a
    `--fresh` run whose asset restore skipped the file runs with the table absent and no error anywhere. Two
    live instances: the main-text table (every plain `SG_*`/`BTN_*`/`Pop_Up_*` UI key -> the title screen
    rendered raw keys) and the four login-bonus MSTs (every calendar in that family reads its reward ROWS
    from one of them, so the calendar drew and its award display was EMPTY).
    The mechanism is one line: the client resolves a table by NAME (token + key from `getMstFileName()`/
    `getMstFileKey()`), and `Ver<N>` only names the file it fetches, so advertising a version the client
    cannot have makes it ask us and `_cdn_fetch`'s same-token fallback returns the shipped bytes. The tables
    all ship at the versions the registry already advertises, so this needs no crypto. `F_DEFINE_MST=227` is
    the same mechanism, already proven live. Homes: `docs/notes/title_screen_loc_keys.md` and
    `docs/notes/catchup_bonus_family.md`.
    """
    if not isinstance(spec, dict) or not isinstance(spec.get("c1qYg84Q"), list):
        return spec
    authored = [r for r in spec["c1qYg84Q"] if isinstance(r, dict)]
    have = {r.get("a4hXTIm0") for r in authored}
    added = [{"a4hXTIm0": n, "wM9AfX6I": v}
             for n, v in sorted(version_table().items()) if n not in have]
    rows = authored + added
    forced = _forced_table_versions()
    if forced:
        rows = [dict(r, wM9AfX6I=forced[r["a4hXTIm0"]]) if r.get("a4hXTIm0") in forced else r
                for r in rows]
        missing = sorted(set(forced) - {r.get("a4hXTIm0") for r in rows})
        if missing:
            # An unregistered name is inert, so this is a warning rather than a failure -- but it would also
            # be silent, and a silent no-op here looks exactly like the bug being fixed.
            _note(f"version table: FORCE_TABLE_VERSIONS names {missing}, which the client has NOT registered "
                  f"(not in {VERSION_TABLE_XML}) -- the row is inert")
    spec = dict(spec)  # never rewrite the loaded payload -- else the note below drifts on the 2nd request
    spec["c1qYg84Q"] = rows
    _note(f"version table: {len(authored)} authored + {len(added)} from {VERSION_TABLE_XML} "
          f"= {len(rows)} rows" + (f"; forced {forced}" if forced else ""))
    return spec


def _forced_table_versions() -> dict:
    """`FORCE_TABLE_VERSIONS` -> `{name: version}`. Malformed entries are skipped, and `off` disables it."""
    out: dict = {}
    for part in str(FORCE_TABLE_VERSIONS or "").split(","):
        part = part.strip()
        if not part or part.lower() in ("off", "none", "0"):
            continue
        name, _, ver = part.partition("=")
        name, ver = name.strip(), ver.strip()
        if name and ver.isdigit() and int(ver) >= 1:
            out[name] = ver
        elif name:
            _note(f"version table: FORCE_TABLE_VERSIONS entry {part!r} is not NAME=<int> -- ignored")
    return out
