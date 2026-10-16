#!/usr/bin/env python3
"""ffbe_player.py — the PLAYER axis: roster management (favourite / storage / sell / fusion), the mailbox, the login
calendar, the switch container, the HOME badges and the diamond balance.

It borrows `_lv_half` and `_switch_container` from `ffbe_battle`, which is the only cross-module import in
the split and is one-way on purpose: the switch container is *about* a cleared mission, and the level string
is *about* the party that just fought, so both belong to the battle axis even though the reply that carries
them is the user-state one.

Extracted 2026-09-26 from `capture_server.py` (docs/SERVER_REFACTOR_PLAN.md, Phase 3, last seam).
"""
from __future__ import annotations

import copy, hashlib, json, os, time

from ffbe_config import *          # noqa: F401,F403 -- constants; the table has no behaviour
import ffbe_state as st           # the REBOUND runtime state; see ffbe_state's docstring
from ffbe_state import State, _note
from player_state import STORAGE_CURRENCY, unit_stats, BAG_ITEM_TYPE, item_mst
# The MST loader, for the Survival FAMILY resolver. Imported by name because it is the one loader that
# preserves FILE ORDER, and "first row" is exactly what `getFirstMissionSubType` means -- re-reading the
# ndjson here would risk a second, differently-ordered copy of the same table.
from player_state import _load_ndjson as _load_mst_rows  # noqa: PLC0415
from data_paths import MST_DIR, TEXT_DIR   # the data-root contract; see data_paths.py

# The only cross-module import in the split, and one-way on purpose: the switch container is
# about a cleared mission and the level string is about the party that just fought, so both
# belong to the battle axis even though the reply carrying them is the user-state one.
from ffbe_battle import _lv_half, _switch_container


__all__ = [
    "apply_carry_edit",
    "apply_party_edit",
    "apply_progress",
    "catchup_bonus",
    "claim_daily_quest",
    "claim_panel_quest",
    "claim_sp_challenge",
    "credit_emblem_objectives",
    "deliver_catchup_rewards",
    "deliver_emblem_tier_mail",
    "deliver_login_mail",
    "echo_client_group",
    "login_calendar",
    "panel_quest_tables",
    "serve_beast_board",
    "serve_beast_mix",
    "serve_challenge_clear_all",
    "serve_daily_quest",
    "serve_diamonds",
    "serve_emblem_event",
    "serve_favourite_info",
    "serve_home_badges",
    "serve_login_bonus",
    "serve_mail",
    "serve_panel_quest",
    "serve_sp_challenge",
    "serve_survival",
    "serve_switches",
    "serve_unit_favourite",
    "serve_unit_mix",
    "serve_unit_sell",
    "serve_unit_storage",
    "sp_challenge_info_rows",
    "sp_challenge_picked",
    "sp_challenge_tables",
    "sp_schedule_category",
    "sp_schedule_id",
]

_FAV_INFO_LOGGED: set = set()
def serve_diamonds(spec):
    """Attach the top-bar lapis balance wherever the reply refreshes user state.

    The client re-logins after a battle (post `MissionEndRequest` it sends GameSetting ->
    GetBackgroundDownloadInfo -> Initialize -> GetUserInfo), so attaching the group to the user-state
    reply lands the award when the player returns home.
    """
    if not isinstance(spec, dict) or DIAMOND_TAG in spec:
        return spec
    if not any(g in spec for g in USER_STATE_GROUPS):
        return spec
    out = copy.deepcopy(spec)
    out[DIAMOND_TAG] = [{DIAMOND_FREE: str(State.player.currencies["lapis_free"]),
                         DIAMOND_PAID: str(State.player.currencies["lapis_paid"])}]
    return out
def apply_party_edit(plaintext: str | None) -> None:
    """Record the client's deck edit (`PartyDeckEditRequest` -> `5Eb0Rig6`) in the ONE player state.

    WHY THIS EXISTS. The party groups are a PROJECTION of the state (`apply_to_reply` -> `_deck_rows`), so a
    deck edit the server never records is not merely "unsaved": the next user-state reply serves the OLD
    deck straight back at the player, and the battle is built from the state too, so a unit that was added
    fights nowhere. Reported live 2026-09-22 -- Dark Fina (`100000315`, a gacha pull) could be placed on the
    deck screen but never appeared in battle, because the state's Party 0 still held only Rain and Lasswell
    while the request that added her was answered with the unchanged deck.

    The request carries the SAME row shape the reply serves (`_deck_rows`): `r21y0YzS` = deck id,
    `G4L0YIB2` = name, `2A6fYiEC` = `member_type:disp_order:instance_id` triples. Two rules keep it honest:
    only instance ids IN THE ROSTER are accepted (a deck naming an unknown id would field a unit that does
    not exist), and deck ids are MATCHED, never created (the client can only edit decks it was served).
    The request's `CN92arJK.r21y0YzS` is the active deck id; it is accepted only when it names a deck
    present in the request. Beast-party rows (`49rQB3fP`) are applied as five-slot links as well.
    """
    if not plaintext or State.player is None:
        return
    if State.player.mode == "frozen":
        _note("apply_party_edit: frozen state policy -- deck edit not applied")
        return
    try:
        req = json.JSONDecoder().raw_decode(plaintext)[0]
    except Exception:  # noqa: BLE001 - a body that will not parse must never break the reply
        _note("apply_party_edit: request body did not parse as JSON; deck edit dropped")
        return
    rows = req.get(PARTY_DECK_GROUP) if isinstance(req, dict) else None
    if not isinstance(rows, list):
        return
    state = State.player.data
    known = {str(u.get("instance_id")) for u in state.get("roster") or []}
    decks = {str(d.get("id")): d for d in (state.get("party") or {}).get("decks") or []}
    changed: list[str] = []
    dropped: list[str] = []
    rejected_decks: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        deck = decks.get(str(row.get("r21y0YzS")))
        if deck is None:
            continue
        members = []
        row_unknown: list[str] = []
        malformed = False
        seen_ids: set[str] = set()
        seen_orders: set[int] = set()
        for tri in str(row.get("2A6fYiEC") or "").split(","):
            if not tri:
                continue
            parts = [x.strip() for x in tri.split(":")]
            if len(parts) != 3 or not all(parts):
                malformed = True
                break
            mtype, order, iid = parts
            if not (mtype.isdigit() and order.isdigit()):
                malformed = True
                break
            if int(mtype) < 0 or int(mtype) > 1 or int(order) < 0 or int(order) >= 5:
                malformed = True
                break
            order_i = int(order)
            if order_i in seen_orders or iid in seen_ids:
                malformed = True
                break
            seen_orders.add(order_i)
            seen_ids.add(iid)
            if iid not in known:
                row_unknown.append(iid)
                continue
            members.append({"member_type": int(mtype), "disp_order": order_i, "instance_id": iid})
        # Never commit a PARTIAL deck: if one selected instance is not owned, applying the remaining
        # members would turn a client-side selection error into silent server data loss. Reject the whole
        # row and let the next full user-state projection put the last coherent deck back on screen.
        if malformed or row_unknown or not members or len(members) > 5:
            dropped.extend(row_unknown)
            rejected_decks.append(str(row.get("r21y0YzS")))
            continue
        touched = False
        if str(deck.get("id")) == str((state.get("party") or {}).get("active_deck")):
            before = ",".join(f"{m['member_type']}:{m['disp_order']}:{m['instance_id']}"
                              for m in (deck.get("members") or []))
            after = ",".join(f"{m['member_type']}:{m['disp_order']}:{m['instance_id']}"
                             for m in members)
            print(f"party: edit active deck {deck.get('id')} before=[{before}] after=[{after}] "
                  f"changed={str(members != (deck.get('members') or [])).lower()}", flush=True)
        if members != deck.get("members"):
            deck["members"] = members
            touched = True
        # The row also carries the deck's NAME (`G4L0YIB2`, the same token `_deck_rows` serves), so a rename
        # must persist too or the next projection clobbers it back (SW-37's second half).
        new_name = row.get("G4L0YIB2")
        if isinstance(new_name, str) and new_name and new_name != deck.get("name"):
            deck["name"] = new_name
            touched = True
        if touched and str(deck["id"]) not in changed:
            changed.append(str(deck["id"]))
    # `CN92arJK` is the request's active-party setting row; its `r21y0YzS` is the active deck id. Only
    # accept an id the request also names in the served deck rows, so a malformed settings row cannot
    # point at a deck the client does not have.
    active_id = None
    settings = req.get("CN92arJK") if isinstance(req, dict) else None
    if isinstance(settings, list) and settings and isinstance(settings[0], dict):
        candidate = str(settings[0].get("r21y0YzS") or "")
        if candidate in decks:
            active_id = candidate
    if active_id is not None and active_id != str((state.get("party") or {}).get("active_deck")):
        State.player.set_active_deck(active_id)
        if active_id not in changed:
            changed.append(f"active={active_id}")

    # Beast-party rows ride the same request. The state owns the five slots, but only an owned esper (or
    # zero) may be linked. This keeps the `49rQB3fP` response projection faithful to the edit.
    beast_rows = req.get("49rQB3fP") if isinstance(req, dict) else None
    if isinstance(beast_rows, list):
        owned_beasts = {str(b.get("esper_id")) for b in state.get("beasts") or []}
        for brow in beast_rows:
            if not isinstance(brow, dict) or str(brow.get("r21y0YzS")) not in decks:
                continue
            parsed: dict[int, str] = {}
            bad = False
            for pair in str(brow.get("XZ4Kh7Ic") or "").split(","):
                bits = pair.split(":")
                if len(bits) != 2 or not bits[0].isdigit() or not bits[1].isdigit():
                    bad = True
                    break
                slot, esper = int(bits[0]), bits[1]
                if slot >= 5 or (esper != "0" and esper not in owned_beasts):
                    bad = True
                    break
                parsed[slot] = esper
            if bad or set(parsed) != set(range(5)):
                _note(f"apply_party_edit: beast deck {brow.get('r21y0YzS')} malformed/unowned; ignored")
                continue
            for slot, esper in parsed.items():
                State.player.link_beast(str(brow.get("r21y0YzS")), slot, int(esper))
            State.player.dirty = True
    if rejected_decks:
        reason = f"unknown unit id(s) {','.join(dropped)}" if dropped else "malformed/duplicate slot data"
        _note(f"apply_party_edit: rejected deck row(s) {','.join(rejected_decks)} ({reason}); "
              "no partial deck was applied")
    if changed:
        State.player.dirty = True
        print(f"party: edit applied to {','.join(changed)}", flush=True)
def _request_json(plaintext: str | None):
    """Parse the request plaintext as JSON, or None (logging nothing -- callers note their own drops)."""
    if not plaintext:
        return None
    try:
        req = json.JSONDecoder().raw_decode(plaintext)[0]
    except Exception:  # noqa: BLE001
        return None
    return req if isinstance(req, dict) else None
def _ids_field(node: dict, key: str) -> list:
    """Split a comma-separated id CSV field; absent/empty -> []. The client's own addParam DROPS empty
    strings, so a node that toggled only favourites ON carries no mGuM1Z9e key at all."""
    raw = node.get(key)
    if not isinstance(raw, str) or not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]
def serve_unit_favourite(spec, plaintext: str | None):
    """UnitFavoriteRequest (`tBDi10Ay`): record the favourite delta, reply with the SAME lists.

    Wire facts (unit_management.md): the request group `R8U3uD1i` carries `9Eb7m1NR` (ids to set) and
    `mGuM1Z9e` (ids to clear). The reply is the client's ONLY applier -- GameResponseParser::parseTag
    calls UserFavoriteInfoList::applyFavorite (0x190f154) when it parses this tag, and nothing updates
    the list optimistically -- so an empty/missing reply would make every heart toggle silently fail to
    stick across a scene change. The reply therefore ECHOES the request's non-empty lists under the same
    tag (readParam appends them to shared()+0x28 / +0x40; applyFavorite then sets each id's flag).
    Idempotent if the scene also applied locally; sufficient if it did not."""
    req = _request_json(plaintext)
    rows = req.get(FAV_GROUP) if req else None
    if not isinstance(rows, list) or not rows:
        return spec
    node = rows[0] if isinstance(rows[0], dict) else {}
    set_ids = _ids_field(node, FAV_SET)
    clear_ids = _ids_field(node, FAV_CLEAR)
    if not set_ids and not clear_ids:
        return spec
    if State.player is None:
        return spec
    on, off = State.player.set_favourites(set_ids, clear_ids)
    unknown = [i for i in set_ids + clear_ids if i not in on and i not in off]
    if unknown:
        _note(f"unit-favourite: id(s) not in the roster, skipped: {','.join(unknown)}")
    if on or off:
        print(f"unit: favourite set={len(on)} clear={len(off)}", flush=True)
    out = dict(spec) if isinstance(spec, dict) else {}
    echo = {}
    if on:
        echo[FAV_SET] = ",".join(on)
    if off:
        echo[FAV_CLEAR] = ",".join(off)
    out[FAV_GROUP] = [echo]
    return out
def serve_unit_storage(spec, plaintext: str | None):
    """UnitStorageUpdateRequest (`5ik69cuw`): set storage_state on the named roster rows.

    The request group `JMxPV87y` carries `km9gKXJ5` (int state; 1 = into the warehouse -- the client then
    unequips the unit ITSELF, WareHouseUtils::storageUpdateConnectedProcess 0x1ad46f4) and `ng9xPNt8` (ids).
    The reply class reads NOTHING (readParam is literally `mov w0,#1; ret`, 0x1942698) and the client
    applies the move locally -- so the reply is a bare group, and the server's job is the state: the
    roster row's `QHFU1k43` comes back marked on the next user-state refresh, which is what the
    warehouse tab's `isWareHouseUnit` (`storageState == 1`) filter reads."""
    req = _request_json(plaintext)
    rows = req.get(STORAGE_GROUP) if req else None
    if not isinstance(rows, list) or not rows:
        return spec
    node = rows[0] if isinstance(rows[0], dict) else {}
    ids = _ids_field(node, STORAGE_IDS)
    raw_state = node.get(STORAGE_STATE)
    try:
        state = int(str(raw_state))
    except (TypeError, ValueError):
        _note(f"unit-storage: bad state {raw_state!r}; request dropped")
        return spec
    if not ids or State.player is None:
        return spec
    moved = State.player.set_storage(ids, state)
    unknown = [i for i in ids if i not in moved]
    if unknown:
        _note(f"unit-storage: id(s) not in the roster, skipped: {','.join(unknown)}")
    if moved:
        print(f"unit: storage state {state} -> {len(moved)} unit(s)", flush=True)
    out = dict(spec) if isinstance(spec, dict) else {}
    out[STORAGE_GROUP] = []      # readParam reads nothing; an empty node list parses fine (o5sRL2Sa precedent)
    return out
def _beast_csv(raw) -> list:
    """A piece/item CSV from a beast request: the client's `addParam(string)` DROPS an empty value, so an
    absent key and an empty string are both "none"."""
    if not isinstance(raw, str) or not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]


def _beast_item_entries(raw) -> list:
    """`20:<itemId>:<count>,…` — kind 20 (the bag) then id then count, one element per DISTINCT item
    (`BeastMixScene::backScene` `0x2072e00` appends `IntToString(20)`, `':'`, id, `':'`, count after
    de-duplicating the accumulated bait vector). A non-kind-20 element is skipped, never guessed at."""
    out = []
    for part in _beast_csv(raw):
        bits = part.split(":")
        if len(bits) != 3 or bits[0] != str(BAG_ITEM_TYPE):
            continue
        try:
            out.append((int(bits[1]), int(bits[2])))
        except ValueError:
            continue
    return out


def serve_beast_board(spec, plaintext: str | None):
    """BeastBoardPieceOpenRequest (`0gk3Tfbz`): record the board's learned / unlocked pieces, then ack.

    The group `M7t6Iksb` carries `Iwfx42Wo` (EsperId) plus two piece-id CSVs: `2fim9g1S` (`PieceInfo` —
    the pieces just LEARNED; `BoardCommonScene::initEffect` `0x205f188` pushes the piece being learned,
    and `BeastUtil::learningPiece` `0x1a56468` spent its CP at the tap) and `0bMAwC1t` (`UnlockPieceInfo`
    — the pieces whose UNLOCK anime ended; `BeastBoardScene::unlockAnimeEndCallback` `0x205e710` pushes
    them, and that path spent item `107037768` — `BeastBoardScene::updateEvent` `0x205be20`). The client
    sends this on its way off the board (`backScene` `0x205c9d4` / `onTouchFooter` `0x205cda0`).

    Nothing reads the reply (no factory entry for the tag), so the ack is an empty object whenever the
    group is PRESENT (the client reads no group from it; an absent group means this is not our request and
    the spec is left untouched). The JOB is the state, which the next user-state refresh projects into
    `gP9TW2Bf`/`1S8P2u9f`."""
    req = _request_json(plaintext)
    rows = req.get(BEAST_BOARD_GROUP) if req else None
    if rows is None:
        return spec
    out = dict(spec) if isinstance(spec, dict) else {}
    node = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
    if not node:
        _note("beast-board: the group carried no usable node; answering an empty ack")
        return out
    if State.player is None:
        return out
    raw_id = node.get(BEAST_ID_TOKEN)
    try:
        esper_id = int(str(raw_id))
    except (TypeError, ValueError):
        _note(f"beast-board: unusable EsperId {raw_id!r}; request dropped")
        return out
    learned, refused = State.player.learn_beast_pieces(esper_id, _beast_csv(node.get(BEAST_BOARD_LEARNED)))
    opened, refused_u = State.player.unlock_beast_pieces(esper_id, _beast_csv(node.get(BEAST_BOARD_UNLOCKED)))
    for why in refused + refused_u:
        _note(f"beast-board: refused {why}")
    if learned or opened:
        print(f"beast: board esper {esper_id} learned=[{','.join(learned)}] "
              f"opened=[{','.join(opened)}]", flush=True)
    else:
        # The empty branch is the one that needs a line (a handler that did nothing must say what it
        # considered, name what was carried) — idiom 47.
        print(f"beast: board esper {esper_id} applied nothing "
              f"(learned={len(_beast_csv(node.get(BEAST_BOARD_LEARNED)))} "
              f"unlocked={len(_beast_csv(node.get(BEAST_BOARD_UNLOCKED)))} carried)", flush=True)
    return out


def serve_beast_mix(spec, plaintext: str | None):
    """BeastMixRequest (`C8X1KUpV`): apply the magicite, then ack.

    The group `Simv74We` carries `Iwfx42Wo` (EsperId) and `MgAVu3X6` (`ItemInfo`, `20:<itemId>:<count>,…`).
    The rule is the client's own two functions, read in the binary: `BeastUtil::giveBeastBait` `0x1a557cc`
    (per item: `round(exp*rate/100)*count` onto the RESIDUAL exp `+0xc0` while below max level, and
    `round(cp*rate/100)*count` onto CP `+0xe0`) and the `beastLvUp` loop `0x1a55b2c` (while
    `exp >= need(level+1)`: `exp -= need`, `level++`, exp zeroed AT max level, `cp += getAddCp(newLevel)`).
    The magicite is kind 20 and is consumed (`GameUtils::subItemNum`), so the bag is part of the state.

    Nothing reads the reply, so `{}` is enough — sent rather than left empty so the server log says
    "served" rather than `reply:EMPTY` for a request that did real work. An ABSENT group means this is not
    our request and the spec is left untouched; a present-but-malformed one still gets the `{}` ack."""
    req = _request_json(plaintext)
    rows = req.get(BEAST_MIX_GROUP) if req else None
    if rows is None:
        return spec
    out = dict(spec) if isinstance(spec, dict) else {}
    node = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
    if not node:
        _note("beast-mix: the group carried no usable node; answering an empty ack")
        return out
    if State.player is None:
        return out
    raw_id = node.get(BEAST_ID_TOKEN)
    try:
        esper_id = int(str(raw_id))
    except (TypeError, ValueError):
        _note(f"beast-mix: unusable EsperId {raw_id!r}; request dropped")
        return out
    entries = _beast_item_entries(node.get(BEAST_MIX_ITEMS))
    if not entries:
        print(f"beast: mix esper {esper_id} carried no materials", flush=True)
        return out
    result = State.player.mix_beast(esper_id, entries)
    for why in result["refused"]:
        _note(f"beast-mix: refused {why}")
    print(f"beast: mix esper {esper_id} exp+{result['exp']} cp+{result['cp']} -> level {result['level']} "
          f"(+{result['levels']}) magicite={result['consumed']}", flush=True)
    return out


def serve_unit_sell(spec, plaintext: str | None):
    """UnitSellRequest (`9itzg1jc`): remove the sold roster rows, credit the client's own gil.

    The request group `Ray54PNQ` carries `FBjo93gK` (the total sell gil the CLIENT computed via
    GameUtils::getSellGil) and `S5Ib7En1` (the ids). The client is optimistic: execSellUnit (0x26d64b4)
    calls GameUtils::removeUserUnit and accumulates the gil BEFORE the reply, and the reply class
    (UnitSellResponse, readParam `return 1`) reads nothing. So the reply is a bare group and the server's
    job is state consistency: drop the rows (pruning any stale deck membership), credit the client's
    number -- the balance the client will show is its own computation, and the next user-state refresh
    must agree with it."""
    req = _request_json(plaintext)
    rows = req.get(SELL_GROUP) if req else None
    if not isinstance(rows, list) or not rows:
        return spec
    node = rows[0] if isinstance(rows[0], dict) else {}
    ids = _ids_field(node, SELL_IDS)
    if not ids or State.player is None:
        return spec
    try:
        gil = int(str(node.get(SELL_GIL) or 0))
    except (TypeError, ValueError):
        gil = 0
    sold, unknown = State.player.sell_units(ids, gil)
    if unknown:
        _note(f"unit-sell: id(s) not in the roster, skipped: {','.join(unknown)}")
    if sold:
        print(f"unit: sold {len(sold)} unit(s) for {gil} gil (roster now "
              f"{len(State.player.data['roster'])})", flush=True)
    out = dict(spec) if isinstance(spec, dict) else {}
    out[SELL_GROUP] = []
    return out
def serve_unit_mix(spec, plaintext: str | None):
    """UnitMixRequest (`UiSC9y8R`): fuse the materials into the base and serve `UnitMixResponse`.

    WHY THIS EXISTS: without it the request got an EMPTY body and the client showed "Connection error (api
    call)" (observed live 2026-09-24) -- unlike `UnitExchangeRequest`, which tolerates an empty reply, this
    one does not. The request group is the reply tag `f08du6EW` carrying `og2GHy49` (base instance id) and
    `WbGYcN17` (materials, CSV).

    Reply tokens (`UnitMixResponse::readParam` `0x196f874`, all 10 mapped in the field map):
      `og2GHy49` / `WbGYcN17` echo what was fused, `B6H34Mea` = exp gained, `UDMIFB72` = LB exp gained,
      `1dT7ha9H` = success type, `7UR4J2SE` = gil, `Qic38SuC` = the LevelUp channel, `E90oBqct` = the
      SP-affinity-reward display flag (optional). `9mu4boy7` is inert (the map's own note).
    The EXP is the client's OWN formula (`GameUtils::getMixExp`, see `player_state.unit_mix_exp`) so the
    number we serve agrees with the preview the player just looked at.

    Two values are AUTHORED, labelled, and to be checked on screen:
      * `7UR4J2SE` (gil) = 0 -- there is NO unit-mix gil function in the binary (only
        `VisionCardUtils::getMixGil`), so unit fusion is treated as free.
      * `1dT7ha9H` (success type) = 1 -- no reader of `getSuccessType` could be found in the unit path, so
        the value is a placeholder rather than a recovered enum.
    """
    req = _request_json(plaintext)
    rows = req.get(MIX_GROUP) if req else None
    if not isinstance(rows, list) or not rows:
        return spec
    node = rows[0] if isinstance(rows[0], dict) else {}
    base_id = str(node.get("og2GHy49") or "").strip()
    materials = []
    for x in str(node.get("WbGYcN17") or "").split(","):
        if x.strip():
            materials.append(x.strip())
    if not base_id or not materials or State.player is None:
        return spec
    res = State.player.mix_units(base_id, materials)
    out = dict(spec) if isinstance(spec, dict) else {}
    if not res:
        _note(f"unit-mix: refused (base {base_id} or materials not in the roster, or frozen)")
        return out
    base = State.player.roster_row(base_id)
    lb_level = int(base.get("lb_level", 0) or 0)
    lb_exp = int(base.get("lb_exp", 0) or 0)
    (b_lv, b_ex), (a_lv, a_ex) = res["before"], res["after"]
    before = _lv_half(b_lv, b_ex, unit_stats(base["master_id"], b_lv), lb_level, lb_exp)
    after = _lv_half(a_lv, a_ex, unit_stats(base["master_id"], a_lv), lb_level, lb_exp)
    out[MIX_GROUP] = [{"og2GHy49": base_id,
                       "WbGYcN17": ",".join(res["materials"]),
                       "B6H34Mea": str(res["gain"]),          # Exp
                       "UDMIFB72": str(res["lb_gain"]),       # LbExp
                       "1dT7ha9H": "1",                        # success type (authored; see docstring)
                       "7UR4J2SE": "0",                        # gil (authored; no unit-mix gil exists)
                       "Qic38SuC": f"{base_id}@{before}_{after}",
                       "E90oBqct": "0"}]
    # ...and let the client relearn the roster (materials gone, base levelled) from the same reply. The
    # FULL player groups, not `currency_groups()`: fusion changes the ROSTER, and the roster group
    # (`B71MekS8`) is what makes the consumed materials disappear client-side.
    out.update(State.player.groups())
    lb_note = f" +{res['lb_gain']} lb" if res["lb_gain"] else ""
    print(f"unit: MIX {len(res['removed'])} material(s) into {base_id} -> +{res['gain']} exp{lb_note}; "
          f"level {b_lv} -> {a_lv}, roster {len(State.player.data['roster'])}", flush=True)
    return out
def serve_favourite_info(spec, reply_id: str | None = None):
    """Attach the cumulative favourite list (`UserUnitFavoriteResponse`, tag `F2pYJGt5`) to user-state replies.

    Same mechanism as serve_challenge_clear_all, same allow-list: UserFavoriteInfoList::shared() is
    rebuilt EMPTY at every client start, the favourite flag is not a field of the unit rows, and nothing
    else re-feeds the list -- so without this push a restarted client loses every heart. The tag maps to
    the NO-ARG ctor (`UserUnitFavoriteResponse()`, factory 0x1916ae4), whose readParam appends the ids of
    token `og2GHy49` to shared()+0x28 (the set list) -- re-pushing the full list is idempotent."""
    if not FAVOURITE_INFO_ALL or not isinstance(spec, dict) or FAV_INFO_TAG in spec:
        return spec
    if State.player is None or not any(g in spec for g in USER_STATE_GROUPS):
        return spec
    ids = State.player.favourite_ids()
    if not ids:
        return spec
    out = copy.deepcopy(spec)
    out[FAV_INFO_TAG] = [{"og2GHy49": ",".join(ids)}]
    if reply_id and reply_id not in _FAV_INFO_LOGGED:
        _FAV_INFO_LOGGED.add(reply_id)
        print(f"favourite-info: attached F2pYJGt5 ({len(ids)} id(s)) to {reply_id}", flush=True)
    return out
def echo_client_group(spec, plaintext, group: str):
    """Echo the CLIENT's OWN `group` back in the reply instead of a hardcoded copy (SW-36).

    `GetReinforcementInfoRequest` and `FriendListRequest` each carry an `I53AVzSo` group the client maintains
    itself, and it sends several distinct states -- measured over `out/requests/`: `{I40XmJV5=2,i6PMuUn1=1,
    Y58ENcB4=1}` 64x, `{0,0,1}` 25x, `{I40XmJV5=1}` 18x (one token only), `{1,0,2}` 1x. Our replies `AJhnI37s`
    and `ZSq2y7EX` hardcoded `{2,1,1}`, so every non-`{2,1,1}` send was answered with a state it did not send
    (SW-36). The token meanings are unnamed, but echoing the client's own value cannot be wrong about the
    client's own state. A reply that does not carry the group is left byte-for-byte alone.
    """
    if not isinstance(spec, dict) or group not in spec or not plaintext:
        return spec
    try:
        req = json.JSONDecoder().raw_decode(plaintext)[0]
    except Exception:  # noqa: BLE001 - an unparseable body must not break the reply
        return spec
    rows = req.get(group) if isinstance(req, dict) else None
    if not isinstance(rows, list) or not rows:
        return spec
    out = dict(spec)
    out[group] = rows
    return out
def serve_switches(spec):
    """Rewrite every TRACKED switch type's container in this reply from the one tracked state.

    Two things this fixes at once. (a) The container replaces the whole type, so two replies carrying
    different containers do not merge -- the later one wipes the earlier -- which is why one container per
    type is derived here rather than taken from whichever payload happens to be serving. (b) It carries what
    the client told us it had granted (`record_pushed_switches`), instead of erasing it.

    A tracked type the payload does NOT carry gets a **synthesised row** appended to the reply's switch
    group. This is what makes the client's "seen this hint" flags stick: they are switch type 10, and no
    payload row ever declared type 10, so before this they were recorded (`record_pushed_switches`) and
    then never served -- every session re-popped every first-visit hint. A reply with no switch group is
    left exactly as it is (the flags ride the login reply, which carries `8J1R5PXG`).
    """
    if not isinstance(spec, dict):
        return spec
    # Type 101 keeps the historical baseline indices; see SWITCH_BASE_INDICES.
    if SWITCH_TYPE_MISSION not in State.player.switches:
        State.player.switches[SWITCH_TYPE_MISSION] = set(SWITCH_BASE_INDICES)
    wanted = {}
    for stype in list(State.player.switches):
        c = _switch_container(spec, stype)
        if c is not None:
            wanted[stype] = c
    # Only groups that ALREADY carry switch rows may be rewritten or receive a synthesised type.
    switch_groups = [key for key, rows in spec.items()
                     if isinstance(rows, list)
                     and any(isinstance(r, dict) and SWITCH_TYPE_FIELD in r for r in rows)]
    if not switch_groups or not wanted:
        return spec
    present = {str(r.get(SWITCH_TYPE_FIELD)) for key in switch_groups for r in spec[key]
               if isinstance(r, dict) and SWITCH_TYPE_FIELD in r}
    missing = sorted(t for t in wanted if t not in present)
    needs_rewrite = any(str(row.get(SWITCH_BITS_FIELD)) != wanted[str(row.get(SWITCH_TYPE_FIELD))]
                        for key in switch_groups for row in spec[key]
                        if isinstance(row, dict) and str(row.get(SWITCH_TYPE_FIELD)) in wanted)
    if not missing and not needs_rewrite:
        return spec
    out = copy.deepcopy(spec)
    for key in switch_groups:
        for row in out.get(key, []):
            if isinstance(row, dict) and str(row.get(SWITCH_TYPE_FIELD)) in wanted:
                row[SWITCH_BITS_FIELD] = wanted[str(row.get(SWITCH_TYPE_FIELD))]
        for stype in missing:
            out.setdefault(key, []).append({SWITCH_TYPE_FIELD: stype, SWITCH_BITS_FIELD: wanted[stype]})
    return out
def serve_home_badges(spec):
    """Rewrite the `merqbE18` `EventBadgeInfo` value in any reply that carries it with the SEASON's badge set.

    The HOME tiles' base art and event-announce strips read from `EventBadgeInfo` (`home_map_button.md`); the
    payload's own value is the no-season default (the single Map row that made the tile appear). A season
    with no badges, or no season at all, leaves the payload untouched. Returns a COPY when it changes
    anything.
    """
    badges = st.SEASON.badge_string if st.SEASON is not None else ""
    if not isinstance(spec, dict) or not badges:
        return spec
    targets = [row for rows in spec.values() if isinstance(rows, list) for row in rows
               if isinstance(row, dict) and "merqbE18" in row]
    if not targets or all(str(row["merqbE18"]) == badges for row in targets):
        return spec
    out = copy.deepcopy(spec)
    for rows in out.values():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and "merqbE18" in row:
                row["merqbE18"] = badges
    return out
def _mail_receipt_ids(raw) -> list:
    """`Qe02W7rX` is `mailId:2,mailId:2,…` (docs/notes/mailbox.md §4): take the id before each `:`."""
    ids = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if part:
            ids.append(part.split(":", 1)[0])
    return ids
def _request_obj(plaintext):
    """The decrypted request body as a dict. `plaintext` is JSON **text** with a trailing manual-pad byte, so
    `raw_decode` is used rather than a fixed pad `rstrip` (see `mission_id_from` for why that was wrong)."""
    if isinstance(plaintext, dict):
        return plaintext
    if not plaintext:
        return {}
    try:
        obj = json.JSONDecoder().raw_decode(plaintext)[0]
        return obj if isinstance(obj, dict) else {}
    except Exception:  # noqa: BLE001
        return {}
_LOGIN_CALENDAR: dict = {}
def login_calendar() -> dict:
    """`{"YYYY-Mon": {"banner": ..., "days": {"1": {...}}}}`, loaded once."""
    if not _LOGIN_CALENDAR:
        path = os.path.join(HERE, "login_calendar.json")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                _LOGIN_CALENDAR.update(json.load(fh).get("months") or {})
    return _LOGIN_CALENDAR
def _login_bonus_month() -> tuple[str, str]:
    """The month to serve, and why: the SEASON's own month when we hold a table for it, else `LOGIN_MONTH`.

    A season is a selection, and its key already names a month (`2019-07` -> `2019-Jul`), so the calendar
    follows the season with nothing restated. The community table only covers 2017-Jan..2022-Jan, so a season
    outside that window (2016-*, 2022-Sep) falls back to the default rather than serving nothing.
    """
    wanted = getattr(st.SEASON, "login_month", "") if st.SEASON is not None else ""
    if wanted and wanted in login_calendar():
        return wanted, "season"
    if wanted:
        return LOGIN_MONTH, f"season {st.SEASON.key!r} selects {wanted}, no table for it"
    return LOGIN_MONTH, "default"
def serve_login_bonus(spec, reply_id):
    """Answer `LoginBonusRequest` (`vw9RP3i4`) with the selected month's 28 days, from the player's counter.

    Two groups, and the second one is the point: `f5bA4wFx` (`DailyLoginBonusInfoResponse`) carries one row per
    layout cell `1..28`, and `DM63p52N` (`DailyLoginBonusBannerResponse`) carries the ONE filename the scene
    downloads for the banner. Without that group the banner slot stays EMPTY, because the layout row
    `banner_dailylogin` is only a position/size definition (`LayoutCache::getSize/getX/getY`) -- no sprite is
    ever created from it (`DailyLoginScene::setDailyLoginLayout+0x290`).

    `6tSP4s8J` (state) is the only per-day signal the scene reads: 1 = this day is reached/claimed, 0 = not
    yet. The walk in `DailyLoginBonusInfo::onReadFinish` STOPS at the first 0, so `activeDay` becomes the
    player's count -- which is what dims the earlier cells and lights the current one. Every cell still
    renders (the scene builds one per ITEM, not per non-zero state), so all 28 days stay on screen.
    """
    if reply_id != "vw9RP3i4" or State.player is None or not isinstance(spec, dict):
        return spec
    month, why = _login_bonus_month()
    entry = login_calendar().get(month) or {}
    days = entry.get("days") or {}
    if not days:
        _note(f"login bonus: no calendar for {month!r} in login_calendar.json -- reply left as loaded")
        return spec
    reached = State.player.advance_login_bonus(month, time.strftime("%Y-%m-%d"), 28)
    month_no = str(_MONTH_NO.get(month.split("-")[-1], 0))
    rows = []
    for day in range(1, 29):
        item = days.get(str(day))
        if not item:
            continue
        rows.append({
            "LcWVnqNz": month_no,                    # loginMonth: only drives the month-change popup
            "ifYhsWM8": str(day),                    # loginDay: the LAYOUT KEY (`item_frame_off_small_<day>`)
            "tL6G9egd": str(item["id"]),            # itemID
            "YWF08vZS": str(item["type"]),          # itemType (storage enum)
            "5GNDIwPX": str(item["count"]),         # itemQuantity
            "6tSP4s8J": "1" if day <= reached else "0",
        })
    spec = dict(spec)
    spec["f5bA4wFx"] = rows
    spec["DM63p52N"] = [{"lO28ROQ0": LOGIN_BANNER}]
    _note(f"login bonus: {month} ({why}) day {reached}/28, {len(rows)} rows + banner {LOGIN_BANNER}"
          + ("" if len(rows) == 28 else f" -- {28 - len(rows)} day(s) have no resolvable reward"))
    return spec
def serve_mail(spec, reply_id, plaintext):
    """The mailbox is server-authoritative.

    * `MailListRequest` (`KQHpi0D7`) -> `qja26wHb` rows **and** `t8TPs6o2` counters. Both are required: the page
      size comes from `2BW7YLwg`, so rows alone render **nothing** (`MailTopScene::setMailList`).
    * `MailReceiptRequest` (`XK7efER9`) -> claim (`gSEN0jb7 == 1`) or delete (`== 3`) each id in `Qe02W7rX`,
      then serve the counters. The client clears its list and re-requests, so the mutation must be real here.

    See `docs/notes/mailbox.md`.
    """
    if State.player is None:
        return spec
    if reply_id == "KQHpi0D7":
        out = dict(spec) if isinstance(spec, dict) else {}
        out["qja26wHb"] = State.player.mail_rows()
        out["t8TPs6o2"] = State.player.mail_counters()
        return out
    if reply_id == "XK7efER9":
        row = {}
        for group in _request_obj(plaintext).get("rStH7od8") or []:
            if isinstance(group, dict):
                row.update(group)
        rtype = str(row.get("gSEN0jb7", "1"))
        ids = _mail_receipt_ids(row.get("Qe02W7rX"))
        if rtype == "3":
            if ids:
                for mid in ids:
                    State.player.delete_mail(mid)
            else:
                # The delete request names NO ids: `MailTopScene::getDeleteMailRequest` (0x224e808) only sets
                # `receiptType=3`, and the button is enabled by the RECEIVED count -- so "delete" is bulk:
                # drop every claimed (receive_type == 2) mail.
                State.player.delete_received_mail()
        else:
            for mid in ids:
                State.player.receive_mail(mid)
        out = dict(spec) if isinstance(spec, dict) else {}
        out["t8TPs6o2"] = State.player.mail_counters()
        # A claim changes a BALANCE, so the reply must carry it: without a player group here the client keeps
        # the stale lapis/gil/NRG it last saw (the mail screen shows no balance of its own).
        out.update(State.player.currency_groups())
        return out
    return spec
def deliver_login_mail(spec, reply_id):
    """The daily-login award lands in the in-box.

    When we answer `LoginBonusRequest` (`vw9RP3i4`), deliver the calendar's **active day** reward as ONE mail.
    The client recognises a daily-login mail by TITLE+MESSAGE, not a type (`isDailyLoginReward()` `0x1d80058`),
    so those two are fixed by the binary. Idempotent per day through a **NUMERIC** mail id
    (`900000000000 + day`): `ynP26rjD` is a u64 (`toNumber<long long>`), so a non-numeric id parses to `0`
    client-side and its receipt can never match (measured 2026-09-22). We do not model a per-day claim set yet -
    that is the honest limit, recorded in docs/notes/mailbox.md.
    """
    if reply_id != "vw9RP3i4" or State.player is None or not isinstance(spec, dict):
        return spec
    items = spec.get("f5bA4wFx")
    if not isinstance(items, list):
        return spec
    active = None
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            state = int(it.get("6tSP4s8J", 0))
            day = int(it.get("ifYhsWM8", 0))
        except (TypeError, ValueError):
            continue
        if day and state not in (0, 2):
            active = (day, it)
    if active is None:
        return spec
    day, it = active
    try:
        reward = [(int(it.get("YWF08vZS", 0)), int(it.get("tL6G9egd", 0)), int(it.get("5GNDIwPX", 1)))]
    except (TypeError, ValueError):
        return spec
    State.player.deliver_mail("SG_DAILY_LOGIN_TITLE", reward, mail_id=str(900000000000 + day),
                              message=str(day), daily_login=True)
    return spec

def deliver_anniversary_ticket(spec, reply_id):
    """One 8th-anniversary ticket per calendar day, delivered as an ORDINARY mailbox entry.

    The original game sent these to the inbox rather than granting them into the bag
    (`docs/notes/home_screen_payloads.md` §11.14), which is what made an over-full bag harmless: nothing is
    granted until the mail is received. The award is therefore **idempotent per day**, keyed on a NUMERIC
    mail id — `ynP26rjD` is a u64 (`toNumber<long long>`), so a non-numeric id parses to `0` client-side and
    its receipt can never match (measured 2026-09-22, the same reason `deliver_login_mail` uses
    `900000000000 + day`).

    **It must NOT look like a daily-login mail.** The client identifies those by TITLE and MESSAGE
    (`isDailyLoginReward()` `0x1d80058`): the title must be exactly `SG_DAILY_LOGIN_TITLE` and the message a
    decimal integer. This is an ordinary mail with a real title and prose, so the inbox renders it as a
    normal attachment and the client does not try to read a day number out of it.

    `attach_type` defaults to 1 whenever there are attachments, and that default is load-bearing:
    `MailDetailScene::setLayoutMessage` reads the field DIRECTLY (`ldr w8,[mail+0x68]; cmp #1; b.ne skip`) with
    no accessor, so a mail without it shows neither the reward list nor the Receive button.
    """
    if (not ANNIVERSARY_TICKET or reply_id not in ANNIVERSARY_TICKET_CARRIER.split(",")
            or State.player is None or not isinstance(spec, dict)):
        return spec
    day = time.strftime("%Y%m%d")
    if not day.isdigit():
        return spec
    # The per-day marker is checked and set BEFORE the mail exists, and it lives in the state rather than in
    # the mailbox. That is what stops the loop the player found: the inbox request is both the carrier AND
    # the delete path, so a marker living in the mail is erased by the player's own delete and the next
    # inbox request re-issues the ticket -- measured live as receive -> delete -> receive.
    if not State.player.mark_anniversary_issued(day):
        return spec
    State.player.deliver_mail(
        ANNIVERSARY_TICKET_TITLE,
        [(ANNIVERSARY_TICKET_TYPE, ANNIVERSARY_TICKET_ITEM, ANNIVERSARY_TICKET_COUNT)],
        mail_id=str(910000000000 + int(day)),
        message="A 10+1 summon ticket for the 8th anniversary. Spend it on the 8th Anniversary "
                "10+1 Ticket Summon.",
        icon_type=2, attach_type=1)
    return spec


# --- the CATCH-UP schedules: the shipped ladders, replayed on OUR clock, selected by the SEASON ---------
# Home: docs/notes/catchup_bonus_family.md. The daily calendar above is ONE schedule; the family has three
# more, and all of them are SHIPPED content selected by id rather than authored (the generator is
# harness/gen_catchup_bonus.py, the table is server/catchup_bonus.json):
#   * the cumulative total-login ladder (29 tiers at total-login days 2..1150) -- a catch-up schedule in the
#     literal sense: it is what the game gives a player for having logged in a lot;
#   * one of 412 special-login campaigns, the seasonal/event login calendar (a season picks the id);
#   * the AUTHORED fresh-account welcome grant (SW-99), the only invented numbers, labelled at their home.

_CATCHUP: dict = {}
def catchup_bonus() -> dict:
    """`server/catchup_bonus.json`, loaded once (the same lazy pattern as `login_calendar`)."""
    if not _CATCHUP:
        path = os.path.join(HERE, "catchup_bonus.json")
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    _CATCHUP.update(json.load(fh))
            except (OSError, ValueError) as exc:  # noqa: BLE001
                _note(f"catchup: {path} unreadable ({exc!r}) -- no catch-up schedule served")
    return _CATCHUP


def _catchup_plan() -> dict:
    """What the active season asks for: `{"ladder": bool, "campaign": str, "days": int, "welcome": {}}`.

    Everything comes from the season facet (`server/seasons.json`), so a season SELECTS and this function
    only resolves it against the shipped catalogue. A campaign id that does not exist is REPORTED and
    dropped, never replaced with an invented one -- the season rule is that a season names ids that resolve.
    """
    season = getattr(st, "SEASON", None)
    if season is None or not season.catchup_on:
        return {}
    book = catchup_bonus()
    cid = str(season.sp_campaign_id or "")
    campaign = ((book.get("sp_campaigns") or {}).get("campaigns") or {}).get(cid) or {}
    if cid and not campaign:
        _note(f"catchup: season {season.key!r} names campaign {cid!r}, which is not in catchup_bonus.json "
              f"-- ignored (a season selects ids, it does not create them)")
        cid = ""
    return {"ladder": bool(season.login_bonus.get("total_ladder")) and bool(book.get("total_ladder")),
            "campaign": cid,
            "days": int(campaign.get("days") or 0),
            "rewards": campaign.get("rewards") or {},
            "welcome": dict(season.welcome)}


def _ymd_now() -> tuple[str, str]:
    """`startYMD`/`endYMD` for the active campaign's window: today .. today+1yr, as `YYYYMMDD` ints.

    The window is SERVER-authoritative -- the shipped SP table carries no date columns (only the reward
    rows), so a campaign's period is a fact about the season we serve, not about the data we hold. The
    scene reads both ends only for its period label (`TotalSpecialLoginBonusScene::setRewardInfoLayout`
    0x2310234/0x2310244, then `CommonUtils::splitYMD`), so an evergreen window is honest: it says
    "this campaign is running now", which is exactly what a synthetic season means.
    """
    now = time.gmtime()
    end = time.gmtime(time.time() + 365 * 86400)
    return (f"{now.tm_year:04d}{now.tm_mon:02d}{now.tm_mday:02d}",
            f"{end.tm_year:04d}{end.tm_mon:02d}{end.tm_mday:02d}")


def _parse_reward(text: str) -> list:
    """One shipped reward string -> the `(kind, id, count)` triples the mailbox speaks.

    `server/catchup_bonus.json` already holds the parsed rows per day; this is the belt-and-braces path for
    a string read straight out of the file, and it uses the SAME conventions the generator does (gil's
    amount in `id`; a currency row with no `count` takes it from `id`) so a row cannot mean two things.
    """
    out = []
    for part in str(text or "").split(","):
        f = part.strip().split(":")
        if not f or not f[0].isdigit():
            continue
        kind = int(f[0])
        iid = int(f[1]) if len(f) > 1 and f[1].isdigit() else 0
        count = int(f[2]) if len(f) > 2 and f[2].isdigit() else 0
        if not count and kind in (0, 30, 50, 51):
            count = iid
        if count > 0:
            out.append((kind, iid, count))
    return out


def _info_day_list(today: str, span: int = CATCHUP_INFO_DAYS) -> str:
    """The `In9jMbU7` `uJK9ac2V` list: `;`-separated `YYYYMMDD` dates, the last of which is `today`.

    `UserLoginBonusInfoList::checkLoginBonusUpdated` 0x1d95d2c splits this on `;` and returns true iff one
    part EQUALS the device's today (with 0 substituted for "yesterday"), so the list must CONTAIN today or the
    total/weekly pages refuse with `LOGIN_BONUS_RETURN_HOME` (catchup note §7, and its item 4 names this as
    the most likely first-run failure). It walks BACKWARD from today, so the newest date is last, and an
    unparseable date yields an empty list rather than a list the gate will reject.
    """
    try:
        day = time.strptime(str(today), "%Y-%m-%d")
    except (ValueError, TypeError):
        return ""
    out = []
    for back in range(max(1, int(span)) - 1, -1, -1):
        # formatted by hand, not through time.strftime: the harness pins that name to a frozen value in
        # tests, and a date list that repeats one date under a pinned clock is a false pass, not a pass.
        t = time.gmtime(time.mktime(day) - back * 86400)
        out.append(f"{t.tm_year:04d}{t.tm_mon:02d}{t.tm_mday:02d}")
    return CATCHUP_DAY_SEP_INFO.join(out)


def _ladder_days_passed(plan: dict, player) -> list:
    """Every shipped ladder tier the player has been PAID through -- the CUMULATIVE list, not today's.

    `cksL9bW8` describes the player's completed total-login tiers, so it has to be the whole run: tier
    thresholds with `days <= total_login_days` and `bit <= ladder_bit` (the bit is the pay-once key, so this
    is empty on a day when nothing was due and identical on the days that were).

    It reads the LIVE counters, not the `advance_catchup` snapshot: the snapshot is taken before the day's
    payments, so using it reported the previous day -- a login that paid six tiers served an empty list.
    """
    if not plan.get("ladder") or player is None:
        return []
    cu = player.catchup()
    total = int(cu.get("total_login_days", 0) or 0)
    paid_bit = int(cu.get("ladder_bit", 0) or 0)
    out = []
    for tier in (catchup_bonus().get("total_ladder") or {}).get("tiers") or []:
        try:
            days, bit = int(tier.get("days", 0) or 0), int(tier.get("bit", 0) or 0)
        except (TypeError, ValueError):
            continue
        if 0 < days <= total and bit <= paid_bit:
            out.append(days)
    return out


def _quest_index() -> dict:
    """`{questId: {family, schedule, cond, param, max}}` from the two shipped quest tables, loaded once.

    Read from `mst_decoded/` at SERVE time (the same precedent as the panel-quest rows): the condition code
    and the ProgressMax are SHIPPED, so a code is never re-typed in the server and cannot drift from the data.
    `cond` is the code part of `Pzn5h0Ga` (`11:*` -> `11`) and `param` is the REST of it, which is
    load-bearing for the one code that is parameterised: code `15` is the mission clear and its parameter
    names the mission (`15:8991106`) or is `*` for "any quest" (227 of the 855 code-15 SP-challenge rows ship
    the wildcard). `max` is `oz5Vt6AR` -- which in the MST is **`getProgressMax`** (+0x38) and in the REPLY
    class is ProgressValue, one token with two meanings (`RE_VERIFICATION_IDIOMS` #26). Every Rookie Panel
    Quest and Rookie Challenge row measures max = 1.
    """
    if _QUEST_INDEX:
        return _QUEST_INDEX
    mst = MST_DIR
    for family, fn in (("spchallenge", "F_SP_CHALLENGE_MST.ndjson"),
                       ("panelquest", "F_PANEL_QUEST_MST.ndjson")):
        path = os.path.join(mst, fn)
        if not os.path.isfile(path):
            _note(f"progress: {path} is missing -- no {family} quests can be scored")
            continue
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                qid = str(row.get("V36ygRYs", ""))
                if not qid:
                    continue
                spec = str(row.get("Pzn5h0Ga", ""))
                cond, _, param = spec.partition(":")
                try:
                    mx = int(row.get("oz5Vt6AR", 0) or 0)
                except (TypeError, ValueError):
                    mx = 0
                _QUEST_INDEX[qid] = {"family": family, "schedule": str(row.get("zF6QIY2r", "")),
                                     "cond": cond, "param": param.strip(), "max": mx}
    return _QUEST_INDEX


_QUEST_INDEX: dict = {}


def _progress_key(family: str, schedule: str, quest_id: str) -> str:
    return f"{family}/{schedule}/{quest_id}"


def _quest_matches(action: str, meta: dict, cleared_mission) -> bool:
    """Does this reported `action` satisfy THIS quest? The parameter-aware half of the join.

    Most condition codes are a bare code with no parameter, and the code alone decides. Code `15` (the
    mission clear) is the exception that matters: `15:8991106` names one mission and `15:*` means any
    quest, so a clear of `1110100` must score `15:*` and `15:1110100` and NOT `15:8991106`. Getting this
    wrong in the permissive direction is the unrecoverable kind -- it pays a quest the player never did.

    **A parameter can also be a LIST.** Three shipped quests put fourteen mission ids in it -- `500201`,
    `500301`, `500401`, spec `15:8210107@8210108@...@8210120`, ProgressMax 8, shipped name *"Clear the
    'Once-a-Day Special Quest' 8 times!"* -- so the comparison is MEMBERSHIP on `@`, not equality against
    the whole 110-character tail. (Audit 2026-09-27: equality left those three pinned at 0 on **every**
    possible clear, so `Ag8bisM0` could never be 1 for them. `@` is a list separator in another column of
    the same table as well: `B2FW5Jq = "9001:60@6@24"`.) Membership is the fail-closed direction too -- a
    clear of mission X credits an any-of list, never an unrelated quest.

    Fail closed on the id: with no `cleared_mission` (a request that reports the action but carries no id)
    a NAMED quest does not match, while a bare-code or wildcard quest still does -- those two ask only
    "was a quest cleared", which the arrival of the request itself already asserts. So an unparseable body
    can under-credit a named quest and can never over-credit one.
    """
    param = str(meta.get("param") or "")
    if not param or param == PROGRESS_PARAM_ANY:
        return True
    if not cleared_mission:
        return False
    return str(cleared_mission) in {p.strip() for p in param.split(PROGRESS_PARAM_LIST_SEP) if p.strip()}


def _daily_quests_for_action(action, player) -> list:
    """Today's daily quests the `action` scores: `[(counter key, quest id, target)]`.

    The join is by the `action` field `harness/gen_daily_quests.py` parsed out of each row's SHIPPED detail
    sentence, so adding a daily quest is data in `server/daily_quests.json` and not a new branch here. An
    action of None (a request that is not a mapped action) matches nothing, and an unknown action name
    matches nothing rather than everything -- a typo in `PROGRESS_ACTIONS` must fail CLOSED, because a typo
    that matched everything would pay every daily quest on any request at all.
    """
    if not action or player is None:
        return []
    day = player.daily_quest_day()
    if not day:
        return []
    out = []
    for qid in player.daily_quest_ids():
        quest = daily_quest_by_id().get(str(qid))
        if not quest or str(quest.get("action") or "") != str(action):
            continue
        out.append((player.daily_progress_key(qid, day), str(qid), int(quest.get("target") or 0)))
    return out


def _satisfied_quests(action: str, cleared_mission=None) -> list:
    """Every quest the shipped tables say `action` satisfies, as counter keys, with their max.

    The join is by CONDITION CODE from the shipped tables, so adding a family or a code is data, not code.
    A quest already at its max is returned too (the bump is capped, so it is a no-op) -- that keeps the
    caller dumb and the cap authoritative.

    `cleared_mission` is the one piece of CONTEXT a parameterised code needs: `mission_clear` scores the
    `15:<that id>` rows and the `15:*` rows and nothing else (see `_quest_matches`).
    """
    codes = set(PROGRESS_ACTIONS.get(action, ()))
    out = []
    for qid, meta in _quest_index().items():
        if meta["cond"] not in codes:
            continue
        if not _quest_matches(action, meta, cleared_mission):
            continue
        out.append((_progress_key(meta["family"], meta["schedule"], qid), qid, meta))
    return out


def _action_context(action: str, plaintext: str | None):
    """The one fact a PARAMETERISED condition code needs from the request body. `None` if unavailable.

    `mission_clear` is the only such action today: its code `15` is scored against the mission the client
    just cleared, which is `jQsE54Iz.qo3PECw6` on the `MissionEndRequest` body. Anything else returns None
    and therefore matches the bare-code rows only -- the fail-closed direction, so an unparseable body can
    never pay a quest.

    `raw_decode`, never a fixed pad byte (RE_VERIFICATION_IDIOMS #24): the client's manual PKCS#7 pad
    leaves the pad VALUE as the last byte, so a fixed strip is a coin flip.
    """
    if not plaintext or action != "mission_clear":
        return None
    try:
        obj = json.JSONDecoder().raw_decode(plaintext)[0]
    except Exception as exc:  # noqa: BLE001 - any parse failure is the same fact: no id
        _note(f"progress: {action} body did not parse ({exc!r}) -- only unconditional quests can score")
        return None
    if not isinstance(obj, dict):
        return None
    for row in (obj.get(PROGRESS_MISSION_GROUP) or []):
        if isinstance(row, dict):
            val = row.get(PROGRESS_MISSION_TOKEN)
            if val:
                return str(val)
    return None


def apply_progress(spec, reply_id, plaintext=None):
    """The PROGRESS step: score the action the client just reported, then project every quest's progress.

    Two halves, and the order matters:
      1. **Score** -- if this reply IS the request for a mapped action (`PROGRESS_REQUESTS`), bump every
         quest that action satisfies, clamped to the shipped ProgressMax. Phase 1 maps `equip` only, because
         that is the one the operator can validate with a single tap.
      2. **Project** -- rewrite the progress fields in the panel-quest rows of THIS reply (`ELPwhZj0`, built by
         `serve_panel_quest` in the same chain). Without this the counters exist and the client is never
         told, which is exactly the state the screen was in before. The SP-challenge rows are NOT projected
         here: `sp_challenge_info_rows` already writes those three columns from the same counters, so a
         second writer of them was a divergence waiting for an edit (see the note at the removed arm).

    The projection reads the LIVE store, not a snapshot, for the reason recorded in
    `catchup_bonus_family.md` §12.3b: a transform that both writes and projects has to read twice.
    """
    if State.player is None:
        return spec
    player = State.player
    action = next((a for a, rids in PROGRESS_REQUESTS.items() if reply_id in rids), None)
    if action:
        # The CONTEXT a parameterised code needs. `mission_clear` is the only such action today, and its
        # context is the mission the client just cleared -- read from the SAME body with the same
        # `raw_decode` discipline as everywhere else (never a fixed pad byte). A request that reports the
        # action but carries no id scores only the bare-code / wildcard rows, which is the fail-closed
        # direction: a missing id under-credits and is named in the log, while a wrong one would pay.
        ctx = _action_context(action, plaintext) if action == "mission_clear" else None
        hit = _satisfied_quests(action, ctx)
        moved = []
        for key, qid, meta in hit:
            before = player.progress_value(key)
            after = player.bump_progress(key, 1, cap=meta["max"] or None,
                                         what=f"{action} -> {qid} ({meta['family']}/{meta['schedule']})")
            if after != before:
                moved.append(f"{qid}={after}/{meta['max']}")
        if moved:
            # Only the rows that MOVED are named. A mission clear satisfies every `15:*` "clear a quest N
            # times" row in the corpus -- 240 of them -- so printing the whole hit list would put a 20 kB
            # line in the log on every single battle, and the line that matters ("this quest ticked") would
            # be invisible inside it. The count is always printed, so nothing is hidden by the cap.
            shown = moved[:8]
            _note(f"progress: {action} moved {len(moved)} of {len(hit)} quest counter(s): "
                  + ", ".join(shown) + (f" … (+{len(moved) - len(shown)} more)" if len(moved) > len(shown) else ""))
        elif action == "mission_clear":
            _note(f"progress: mission_clear reported mission {ctx or '(none)'} and moved no quest counter "
                  f"({len(hit)} row(s) matched a condition spec but are already at their target, or no "
                  f"quest's spec names this mission)")
    # The DAILY QUEST's half of the same fact. Its join key is the ACTION, not a condition code: the row has
    # no `Pzn5h0Ga` column at all, and what the player must do is written in the row's shipped TEXT
    # ("Send gifts to 5 friends"). One action therefore scores both families from one store -- the reason
    # `quest_progress` is a single table and not one per feature.
    dq_hit = _daily_quests_for_action(action, player)
    for key, qid, target in dq_hit:
        player.bump_progress(key, 1, cap=target or None, what=f"{action} -> daily quest {qid}")
    if dq_hit:
        _note("progress: " + str(action) + " -> daily quest(s) "
              + ", ".join(f"{qid}={player.progress_value(k)}/{t}" for k, qid, t in dq_hit))
    # The SP-challenge arm that USED to be here is GONE, deliberately (audit 2026-09-27). It was a second
    # writer of `oz5Vt6AR`/`Ag8bisM0`/`QCLs6SV8` for the same three columns `sp_challenge_info_rows`
    # already derives -- from the identical `player_progress()` call -- and because it agreed exactly,
    # DELETING the whole arm was byte-invisible: 82/82 identical. Two writers of one state column that can
    # never disagree today is two that can disagree the day one is edited, which is the whole reason
    # `harness/state_writer_audit.py` exists. The builder is now the single writer, for the screen's reply
    # AND for the claim's refreshed set; only the PANEL arm is still projected here, because
    # `serve_panel_quest` is the older path and collapsing it is not free.
    #
    # project: panel-quest progress rows (built by serve_panel_quest in the same reply chain)
    rows = spec.get(PANEL_QUEST_INFO_TAG) if isinstance(spec, dict) else None
    if isinstance(rows, list) and rows:
        spec = _as_spec(spec)
        for r in rows:
            if not isinstance(r, dict):
                continue
            qid = str(r.get(PQ_QUEST_ID, ""))
            meta = _quest_index().get(qid)
            if not meta:
                continue
            val = player.progress_value(_progress_key(meta["family"], meta["schedule"], qid))
            mx = meta["max"]
            r[PQ_PROGRESS] = str(val)
            r[PQ_CLEAR] = "1" if (mx and val >= mx) else "0"
            r[PQ_REWARD_FLAG] = "1" if player.progress_value(
                _progress_key("claimed", meta["schedule"], qid)) else "0"
    # The DAILY QUEST rows are projected by `serve_daily_quest` itself, which is their SINGLE writer: it builds
    # the row and sets `DQ_CURRENT` from the same counters in the same place. This step used to write
    # `DQ_CURRENT` a second time, which is harmless while the two formulas agree and a silent divergence the
    # moment one of them changes -- the `ZLuw4DLa` duplication that cost a run, in a second column. Found by
    # `harness/state_writer_audit.py`, which exists because the field map indexes the client's READERS of a
    # token and cannot see how many places WE write one. The daily-quest family therefore contributes nothing
    # here, and that is the intended state: one writer per state column.
    return spec


def _as_spec(spec):
    """The spec as a MUTABLE dict, creating it when the dispatcher handed us `None`.

    A request with no `responses.json` entry arrives as `spec = None`, and a transform that ADDS groups must
    create the dict. This has now bitten TWICE in this family -- `serve_panel_quest` and
    `claim_panel_quest`, both `TypeError: 'NoneType' object is not iterable` at `dict(spec)`, both of which
    look from the client exactly like the connection error they replace. And an OFFLINE test that hands a
    step `{}` instead of `None` cannot find it: the golden replay can, because it drives the real chain.
    """
    return dict(spec) if isinstance(spec, dict) else {}


def claim_panel_quest(spec, reply_id, plaintext=None):
    """Answer `sgPanelQuestRewardRequest` (`6uue8aKx`) -- the CLAIM, which is what draws the crossed swords.

    Distinct from progress and distinct from clearing: `Ag8bisM0` is *is-cleared* and `QCLs6SV8` is
    *is-received*. Tapping a panel sends this request; with an empty body the client pauses (the operator
    hit exactly that). So this step:

      1. reads the claim triples from the request-side group `QxrSeO56` (scheduleId / groupId / questId);
      2. **verifies the quest is CLEARED** (our counter at the shipped ProgressMax) and refuses otherwise --
         paying an unearned claim would be the one unrecoverable kind of wrong here;
      3. pays the quest's own shipped reward (`dX6cor8j`) through the same `_grant_items` split every other
         grant uses, and records `claimed/<family>/<schedule>/<quest>` capped at 1, which makes a repeated
         tap a no-op instead of a second payout;
      4. answers with the refreshed quest-info rows so the cell's `QCLs6SV8` is 1 on the wire immediately.

    The claim is NOT recorded as progress: they are different states and conflating them is what would let a
    player "claim" by clearing, or clear by claiming.
    """
    if reply_id != PANEL_QUEST_CLAIM_REQUEST or State.player is None:
        return spec
    spec = _as_spec(spec)
    player = State.player
    claims = _request_obj(plaintext).get(PANEL_QUEST_CLAIM_GROUP) if plaintext else None
    if not isinstance(claims, list) or not claims:
        _note("panel quest claim: no " + PANEL_QUEST_CLAIM_GROUP + " triples in the request -- nothing claimed")
        return spec
    index = _quest_index()
    out, paid, refused = [], [], []
    for c in claims:
        if not isinstance(c, dict):
            continue
        qid = str(c.get(PQ_QUEST_ID, ""))
        meta = index.get(qid)
        if not meta:
            refused.append(f"{qid}: not a shipped quest")
            continue
        key = _progress_key(meta["family"], meta["schedule"], qid)
        done = player.progress_value(key)
        if not (meta["max"] and done >= meta["max"]):
            refused.append(f"{qid}: not cleared ({done}/{meta['max']})")
            continue
        ckey = _progress_key("claimed", meta["schedule"], qid)
        row = {"fK3W9qYA": str(c.get("fK3W9qYA", meta["schedule"])),
               "zF6QIY2r": str(c.get("zF6QIY2r", meta["schedule"])),
               "V36ygRYs": qid, "oz5Vt6AR": str(done), "Ag8bisM0": "1", "QCLs6SV8": "1"}
        if player.progress_value(ckey):
            out.append(row)
            continue                                    # already claimed: echo, do not pay again
        reward = _quest_reward(qid)
        credited = player._grant_items(reward)
        if len(credited) != len(reward):
            # Never report a payment that did not happen: the count is the CREDITED rows, not the parsed
            # ones, so a routing or shape error shows up as a shortfall instead of a silent success.
            refused.append(f"{qid}: only {len(credited)}/{len(reward)} reward row(s) creditable")
            continue
        player.bump_progress(ckey, 1, cap=1, what=f"claim {qid} ({meta['family']}/{meta['schedule']})")
        paid.append(f"{qid}: " + ", ".join(f"{r['kind']}:{r['id']}x{r['count']}" for r in credited))
        out.append(row)
    # The FULL row set, not just the claimed rows: `onReadStarted` clears the client's shared list on
    # arrival and no wire token can prevent it, so a one-row reply un-clears the rest of the schedule on
    # screen (audit 2026-09-27). And a seeded user-state group, so `apply_to_reply` runs and the reward's
    # count is fresh in THIS reply rather than on the next carrier -- `6uue8aKx` has no `responses.json`
    # entry, so the spec arrives as None.
    picked = panel_quest_picked()
    if picked:
        spec[PANEL_QUEST_INFO_TAG] = panel_quest_info_rows(picked)
    elif out:
        spec[PANEL_QUEST_INFO_TAG] = out
    spec.setdefault("LhVz6aD2", [{}])
    _note("panel quest claim: " + (f"paid {len(paid)} ({'; '.join(paid)})" if paid else "nothing paid")
          + (f"; REFUSED {len(refused)} ({'; '.join(refused)})" if refused else "")
          + f"; {len(spec.get(PANEL_QUEST_INFO_TAG) or [])} refreshed row(s) returned"
          + (f" ({len(out)} just claimed)" if out else ""))
    return spec


def _quest_reward(quest_id: str) -> list:
    """The quest's shipped reward rows (`dX6cor8j`), parsed with the shared grammar. `[]` if unknown."""
    for fn in ("F_PANEL_QUEST_MST.ndjson", "F_SP_CHALLENGE_MST.ndjson"):
        path = os.path.join(MST_DIR, fn)
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    row = json.loads(line)
                    if str(row.get("V36ygRYs", "")) == str(quest_id):
                        # DICTS, not the tuples `_parse_reward` returns: `_grant_items` skips anything
                        # without a "kind" key, and a tuple row is skipped SILENTLY -- which is how the
                        # first claim test reported "paid" while the reward went nowhere.
                        return [{"kind": k, "id": i, "count": c} for k, i, c in _parse_reward(row.get("dX6cor8j", ""))]
        except (OSError, ValueError):
            continue
    return []


# --- DAILY QUEST -------------------------------------------------------------------------------
# The one quest family whose content is not shipped: the rows ride a REPLY (`Hqdgx83H`), there is no
# `F_DAILY_QUEST_MST` in the GL corpus or the JP/Memorial set, and `DailyQuestMstList` has no
# `createParser` -- so unlike the panel quest there is no CDN table to force, and nothing to inherit. The
# ids, names and details come from `server/daily_quests.json` (generated from the three shipped
# localized-text tables), the SELECTION is the season's, and the progress is the shared `quest_progress`
# store. The full derivation and every address are in `docs/notes/daily_quests.md`.

def daily_quest_catalogue() -> dict:
    """`server/daily_quests.json`, loaded once. `{}` if it is missing -- and SAYS so.

    A missing catalogue must be loud rather than quietly serving an empty list, because an empty daily quest
    and a missing daily quest look identical on screen (idiom 37's "the framework draws, the content is
    blank"). Regenerate with `harness/gen_daily_quests.py`.
    """
    global _DQ_CATALOGUE
    if _DQ_CATALOGUE is not None:
        return _DQ_CATALOGUE
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "daily_quests.json")
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        _DQ_CATALOGUE = doc if isinstance(doc, dict) else {}
    except (OSError, ValueError) as exc:  # noqa: BLE001
        _note(f"daily quest: {path} unreadable ({exc!r}) -- run harness/gen_daily_quests.py")
        _DQ_CATALOGUE = {}
    if _DQ_CATALOGUE and not _DQ_CATALOGUE.get("quests"):
        _note("daily quest: the catalogue has no `quests` -- nothing can be served")
    return _DQ_CATALOGUE


_DQ_CATALOGUE: dict | None = None


def daily_quest_by_id() -> dict:
    """`{id: row}` from the catalogue. Built on demand (the catalogue itself is cached)."""
    return {str(q.get("id")): q for q in (daily_quest_catalogue().get("quests") or [])
            if isinstance(q, dict) and q.get("id")}


def _daily_quest_plan() -> dict:
    """`{pools, count}` -- the season's facet, or the floor when no season is loaded.

    The floor matters: the daily quest is reachable by DEFAULT (a mode of the SP-challenge list screen), so
    a default run must serve one. `season.daily_quests` already returns the floor when a season says nothing;
    this is the `State.SEASON is None` half of the same contract.
    """
    # `st.SEASON` is the live binding (the same one `serve_panel_quest` and the badge row read), NOT
    # `st.State.SEASON` -- two spellings exist in this file and only one of them is ever assigned, which is
    # the kind of pair that makes a facet silently read as "no season". `panel_quest.md`'s reproduction
    # snippet is the reference: `st.SEASON = sm.load('2019-07')`.
    season = getattr(st, "SEASON", None)
    if season is not None:
        try:
            return dict(season.daily_quests)
        except AttributeError:
            pass
    return {"pools": list(DAILY_QUEST_DEFAULT_POOLS), "count": int(DAILY_QUEST_DEFAULT_COUNT),
            "defaulted": True}


def _daily_quest_pick(plan: dict, day: str) -> tuple:
    """`(ids, note)` -- today's set, chosen DETERMINISTICALLY from (day, pools).

    Deterministic rather than random for three reasons that are all about not being wrong quietly: a client
    that re-asks mid-day gets the same list; an ephemeral-state test run gets the same list as the live one;
    and "why did the list change" is answerable from the day alone. The roll itself is recorded in the state
    (`daily_quest.resets`), so the frozen set and the computed set can be compared.

    A pool the catalogue does not have is REPORTED and dropped, never replaced -- the facet selects, it does
    not invent.
    """
    cat = daily_quest_catalogue()
    rows = daily_quest_by_id()
    # A PINNED set bypasses the pick entirely (`DAILY_QUEST_SET`), for a validation run or an A/B. It is
    # filtered through the same catalogue, so a pinned id that does not exist is reported rather than served
    # as a row with no name -- the raw-key failure of idiom 40 would otherwise be reachable from the knob.
    pinned = [p.strip() for p in str(DAILY_QUEST_SET or "").replace(" ", ",").split(",") if p.strip()]
    if pinned:
        good = [p for p in pinned if p in rows]
        dropped = [p for p in pinned if p not in rows]
        return good, (f"DAILY_QUEST_SET pin: {', '.join(good) or '(none)'}"
                      + (f"; DROPPED unknown {', '.join(dropped)}" if dropped else ""))
    want = [str(p) for p in (plan.get("pools") or [])]
    have = set(cat.get("pools") or {})
    unknown = [p for p in want if p not in have]
    pool_rows = [rows[qid] for qid, q in rows.items() if str(q.get("pool")) in want]
    pool_rows.sort(key=lambda r: str(r.get("id")))
    note = ""
    if unknown:
        note = f"unknown pool(s) {', '.join(unknown)} DROPPED"
    if not pool_rows:
        return [], (note + "; " if note else "") + f"no catalogue row in pool(s) {', '.join(want) or '(none)'}"
    try:
        count = max(1, int(plan.get("count") or DAILY_QUEST_DEFAULT_COUNT))
    except (TypeError, ValueError):
        count = int(DAILY_QUEST_DEFAULT_COUNT)
    count = min(count, len(pool_rows))
    # A stable order, then a stable offset into it: `day` and `pools` are the only inputs, so the list is a
    # pure function of them. `hashlib` rather than `hash()` because PYTHONHASHSEED randomises `hash()` per
    # process -- which would make the list differ between the server and the test that asserts it.
    #
    # ROUND-ROBIN ACROSS POOLS, not a slice of the id-sorted list. The shipped ids make the reason obvious:
    # `6000101/6000102/6000103` are Gifter I/II/III, one action at three targets, so a plain slice of three
    # would serve "send 5 gifts, send 10 gifts, send 15 gifts" -- three rows of ONE quest wearing three
    # names. A daily set is three DIFFERENT things to do, so each pool contributes one row before any pool
    # contributes a second.
    by_pool: dict = {}
    for row in pool_rows:
        by_pool.setdefault(str(row.get("pool")), []).append(row)
    for rows_of_pool in by_pool.values():
        rows_of_pool.sort(key=lambda r: str(r.get("id")))
    order = [p for p in want if p in by_pool] + [p for p in by_pool if p not in want]
    seed = int(hashlib.sha256(f"{day}|{'/'.join(want)}".encode()).hexdigest()[:8], 16)
    # The day's OFFSET (`seed`) picks the starting TIER inside each pool, so consecutive days differ; with
    # a fixed offset every day would serve tier 0 of every pool, which is a set that never changes.
    chosen: list = []
    depth_i = 0
    while len(chosen) < count and depth_i < 8:
        for p in order:
            if len(chosen) >= count:
                break
            rows_of_pool = by_pool[p]
            if len(rows_of_pool) > depth_i:
                chosen.append(rows_of_pool[(depth_i + seed) % len(rows_of_pool)])
        depth_i += 1
    return [str(c.get("id")) for c in chosen if isinstance(c, dict)], note


def _daily_status_for(player, quest: dict, day: str) -> int:
    """The `ZLuw4DLa` value for one row: incomplete / complete-unclaimed / claimed.

    **MEASURED, from the client's own two readers, which agree** (`ffbe_config`'s block has the
    disassembly): `getUnclaimedRewardCount` 0x1c74dc0 counts rows whose `getStatus()` is exactly 1, and
    `getDailyQuest` 0x1c74c60 skips rows whose `getStatus()` is exactly 2. So 1 is "done, reward not yet
    taken" and 2 is "received, gone from the list" -- which is the same three-state split the panel quest
    has (`panel_quest.md` §4i: a claim is not a clear and not progress), on one column instead of three.

    `DAILY_QUEST_STATUS_MODE = "zero"` serves 0 for everything: honest, and shows no state at all.

    **`DAILY_QUEST_FORCE_COMPLETE` is honoured HERE, not only in the row builder.** The first version forced it
    in the projection alone, and claim-all then selected candidates from the STATE (where the unearned rows
    are at 0) while the page showed all three as claimable -- so the button did nothing and the handler logged
    **nothing at all**. A display switch that makes the page promise what the server will not honour is not a
    display switch; it is an incoherence, and it cost a run.
    """
    if str(DAILY_QUEST_STATUS_MODE).lower() == "zero":
        return DQ_STATUS_INCOMPLETE
    # ORDER MATTERS, and a run caught it. The CLAIM is a fact about the account; the force-complete knob is a
    # display convenience standing in for "not earned yet". So the claim is tested FIRST -- with the force
    # checked first, every row read `1` (complete, UNCLAIMED) even after it had been claimed, and the two
    # consequences the operator saw were both downstream of that one ordering: the list kept showing three
    # claimable rows ("still lit up like it's not claimed yet"), and the client kept counting unclaimed
    # rewards, so the completion popup re-fired on every home visit. A display switch must never outrank a
    # recorded fact.
    if player.progress_value(player.daily_claim_key(str(quest.get("id")), day)):
        return DQ_STATUS_CLAIMED
    if _daily_rows_force_complete():
        return DQ_STATUS_COMPLETE
    target = int(quest.get("target") or 0)
    cur = player.progress_value(player.daily_progress_key(str(quest.get("id")), day))
    if target > 0 and cur >= target:
        return DQ_STATUS_COMPLETE
    return DQ_STATUS_INCOMPLETE


def _daily_quest_rows(player, day: str) -> list:
    """The `Hqdgx83H` rows for today's set, in the 9 tokens `readParam` 0x1944fd8 dispatches.

    `Name` and `Details` are sent even though their setters are empty (`0x1945520` / `0x1945640` are a bare
    `ret`): sending them costs nothing, documents the row, and means a future build whose setters are NOT
    empty still draws. The label the client actually uses is built from the ID either way -- and a run
    confirmed which one: the list row draws the **DETAIL** ("Summon a unit"), never the NAME ("Summoner").

    `DAILY_QUEST_FORCE_COMPLETE=1` overrides the row's COUNT for display only (see `ffbe_config`); it writes
    nothing, so `claim_daily_quest` still refuses these rows -- the refusal reads the state on purpose.

    **The STATUS is never decided here.** The first version had its own `if force: status = COMPLETE` branch,
    which meant the force outranked the CLAIM in the rows even after `_daily_status_for` was reordered to put
    the claim first: the two places disagreed, the rows kept saying "unclaimed" after a real claim, and the
    client duly kept counting unclaimed rewards and re-firing the completion popup. One decision, one place.
    """
    force = _daily_rows_force_complete()
    out = []
    for qid in player.daily_quest_ids():
        quest = daily_quest_by_id().get(str(qid))
        if not quest:
            continue
        target = int(quest.get("target") or 1)
        cur = target if force else player.progress_value(player.daily_progress_key(qid, day))
        out.append({
            DQ_ID: str(qid),
            DQ_TYPE: str(int(quest.get("type") or 1)),
            DQ_STATUS: str(_daily_status_for(player, quest, day)),
            DQ_CURRENT: str(min(cur, target)),
            DQ_TARGET: str(target),
            DQ_LOCATION: str(int(DAILY_QUEST_LOCATION)),
            DQ_NAME: str(quest.get("name") or ""),
            DQ_DETAILS: str(quest.get("detail") or ""),
            DQ_REWARD: str(quest.get("reward_info") or ""),
        })
    return out


def _daily_remaining_seconds() -> int:
    """Seconds to the next day boundary -- the reset the server owns, in the SAME clock as everything else.

    `DailyQuestInfo::getRemainingTimeInSecs` 0x1d970e4 and the `L4vx6m3T` arm of `readParam`, which stamps
    `lastCheckedTime` from the value, so this is the server's clock and not the client's.

    TWO decisions, both forced by what is already true here rather than chosen fresh:
      * **the boundary is the next LOCAL midnight**, because that is the day the `DAILY_QUEST_DAY_FORMAT`
        key and the login calendar (`advance_login_bonus`) already roll on. **A run corrected this from UTC**:
        the first daily-quest screen showed `Refresh in: 23:42:51` at 19:19 local — a countdown to a midnight
        five hours away from the one that rolls the key. Self-consistent arithmetic, visibly wrong to the
        person looking at it. A second day boundary inside one server is a bug whichever one you pick; pick
        the one the rest of the server already uses.
      * **the arithmetic needs only `time()` and `localtime()`** -- seconds elapsed today, subtracted from a
        day -- because `harness/server_regression.py`'s `FrozenClock` exposes exactly those (plus `gmtime`
        and `strftime`) and is installed over every `ffbe_*` module. Reaching for an entry point it lacks is
        a loud `AttributeError` there, which is the instrument working: a golden that silently read the real
        date once already cost this project a confusing diff, and this function hit that wall twice (first
        `localtime`, then `mktime`). It also sidesteps a real hazard -- `mktime(midnight) + 86400` is wrong
        twice a year on a DST boundary, and "86400 minus the seconds elapsed today" is not.
    """
    now = time.localtime(time.time())
    elapsed = now.tm_hour * 3600 + now.tm_min * 60 + now.tm_sec
    return max(0, 86400 - elapsed)


def _daily_complete_today() -> bool:
    """`DAILY_QUEST_COMPLETE_TODAY` as a bool. A STATE INJECTION; see `ffbe_config` for why it is separate."""
    return str(DAILY_QUEST_COMPLETE_TODAY or "").strip().lower() in ("1", "true", "yes", "on")


def _daily_inject_complete(player, day: str) -> list:
    """Drive every row's real counter to its target. Returns the rows that actually moved.

    A state write, so it is the knob that makes the earned path -- claim, claim-all, payment, the `Status`
    transition, the popup -- executable at all while two of the three default-pool rows have no request to
    score them (SW-122). Capped at each row's own `target` and idempotent, and every move is logged as an
    INJECTION so a counter that advanced without the player doing anything is never mistakable for a fact.
    """
    if not _daily_complete_today():
        return []
    moved = []
    for qid in player.daily_quest_ids():
        quest = daily_quest_by_id().get(str(qid))
        if not quest:
            continue
        target = int(quest.get("target") or 0)
        if target <= 0:
            continue
        key = player.daily_progress_key(qid, day)
        before = player.progress_value(key)
        after = player.bump_progress(key, target, cap=target,
                                    what=f"DAILY_QUEST_COMPLETE_TODAY injection -> {qid}")
        if after != before:
            moved.append(f"{qid} {before}->{after}/{target}")
    if moved:
        _note(f"daily quest: DAILY_QUEST_COMPLETE_TODAY INJECTION -- counters moved without the player doing "
              f"anything ({'; '.join(moved)}). This is a STATE WRITE, not a display switch.")
    return moved


def _daily_rows_force_complete() -> bool:
    """`DAILY_QUEST_FORCE_COMPLETE` as a bool. A DISPLAY A/B; see `ffbe_config` for why it exists."""
    return str(DAILY_QUEST_FORCE_COMPLETE or "").strip().lower() in ("1", "true", "yes", "on")


def _daily_team_lv_string(player) -> str:
    """`tzCNMJ87` in the ONE grammar `DailyQuestInfo::parseTeamLvInfo` accepts, or `""` if no rank-up.

    The parser is `0x1d977ec` and it is strict, which is what makes the format recovered rather than guessed:
    ```
    split(value, ",")         -> cmp x8, #0x30   ; 0x30 = 48 = 3 std::string  => EXACTLY 3 fields
      splitInt(field0, ":")   -> cmp x8, #0xc    ; 0x0c = 12 = 3 int         => EXACTLY 3 ints
      splitInt(field1, ":")   -> cmp x8, #0xc                               => EXACTLY 3 ints
    field0 -> [+0x48, +0x40, +0x50]      field1 -> [+0x4c, +0x44, +0x54]
    ```
    and the six offsets are the six getters, read one instruction each:
    `+0x48 getBeforeRank` · `+0x40 getBeforeXP` · `+0x50 getBeforeActionMax` ·
    `+0x4c getCurrentRank` · `+0x44 getCurrentXP` · `+0x54 getCurrentActionMax`.

    ⇒ **`<beforeRank>:<beforeXP>:<beforeActionMax>,<currentRank>:<currentXP>:<currentActionMax>`**, the
    before group first. Any other shape is dropped without a word (each `cmp` branches away), so a
    plausible-but-wrong string renders an EMPTY popup rather than an error — the §F20 failure mode, and the
    reason the width checks are quoted here instead of summarised as "three ints".

    `""` when no rank-up has been recorded, and the caller then omits the token: a fabricated "1 -> 1" would
    draw a level-up that never happened, which is the one thing this value must not do.
    """
    up = (player.data.get("rank") or {}).get("last_up")
    if not isinstance(up, dict) or not up:
        # No real rank-up recorded. `DAILY_QUEST_TEAM_LV=1` fills it for a DISPLAY run, derived from the
        # current rank, and says so -- because omitting the token (the honest default) leaves the popup's
        # awards list empty, and an empty list and a wrong list look identical on screen.
        if str(DAILY_QUEST_TEAM_LV or "").strip().lower() in ("1", "true", "yes", "on"):
            rank = player.data.get("rank") or {}
            lv = int(rank.get("level") or 1)
            if lv > 1:
                try:
                    from player_state import need_exp
                    prev_need = int(need_exp(lv) or 0)
                except Exception:                       # noqa: BLE001 - a display fallback must never break a reply
                    prev_need = 0
                return (f"{lv - 1}:{prev_need}:{int(rank.get('nrg') or 0)},"
                        f"{lv}:{int(rank.get('exp') or 0)}:{int(rank.get('nrg') or 0)}")
        return ""
    try:
        return (f"{int(up['before_level'])}:{int(up['before_exp'])}:{int(up['before_nrg'])},"
                f"{int(up['to_level'])}:{int(up['to_exp'])}:{int(up['to_nrg'])}")
    except (KeyError, TypeError, ValueError):
        return ""


def serve_daily_quest(spec, reply_id):
    """The DAILY QUEST projection, and the two groups go on DIFFERENT carriers on purpose.

    `Hqdgx83H` (the rows) rides `DAILY_QUEST_CARRIERS` -- the quest screen's five requests -- because it is
    DESTRUCTIVE: `DailyQuestMstResponse::onReadStarted` `0x1945378` is `ldr w8,[x0,#0x24]; cbz ->
    DailyQuestMstList::shared()->removeAllObjects`, so putting it on any other reply would EMPTY the list.

    `sxe5C48P` (the screen's own state) rides `DAILY_QUEST_INFO_CARRIERS`, which is that set **plus the home
    refresh** (`Daud71Hn`) -- and that is measured, not tidier: `DailyQuestInfoResponse::onReadStarted`
    `0x1944ef4` is a **bare `ret`** (the function at `0x1944ef8` is the destructor), so it clears nothing and
    is safe anywhere; and the completion popup is pushed by **`HomeScene::updateEvent` `0x1e4d5e4`**, behind
    a `FunctionMstList::isValid("FUNCTION_COMEBACK_QUEST")` check. A run showed the consequence directly: with
    the flag forced, the popup appeared **from HOME** while the quest screen showed nothing at all. Off the
    quest screen the info group is sent only when the popup is being FORCED, because otherwise it is a
    `DailyQuestInfo` write with nothing new to say.

    Read-only apart from the day roll, and it must run BEFORE `apply_progress`: that step is what writes
    `DQ_CURRENT`, so a projection running after would be the last writer.
    """
    if State.player is None or reply_id not in DAILY_QUEST_INFO_CARRIERS:
        return spec
    if not daily_quest_catalogue():
        return spec
    player = State.player
    on_rows = reply_id in DAILY_QUEST_CARRIERS
    forced = str(DAILY_QUEST_SHOW_COMPLETE or "").strip().lower() in ("1", "true", "yes", "on")
    # ALWAYS send the info group on an info carrier -- do NOT gate it on the force. The first version returned
    # early unless the popup was being forced, on the reasoning that there was "nothing new to say". That was
    # wrong in a way only a stateful client exposes: `DailyQuestInfo` is a PERSISTED singleton
    # (`saveDailyQuestFlagButtonUI` / `saveDailyQuestFlagReportUI`), so a `9vmYua2D = 1` the client received
    # once stays set across restarts -- and withholding the token afterwards means we can never CLEAR it. The
    # forced A/B had already put the flag on, so the alert would have kept firing forever with the server
    # insisting (correctly) that the day's quests were claimed. A knob must be able to be undone: the honest
    # derived value is cheap (4 short keys) and the class clears nothing (`onReadStarted` is a bare `ret`).
    plan, day, note, rolled = _daily_quest_plan(), "", "", False
    if on_rows:
        day = time.strftime(DAILY_QUEST_DAY_FORMAT)
        ids, note = _daily_quest_pick(plan, day)
        # The pin is the ONE thing that may re-roll a frozen set -- and only when it actually CHANGES it.
        # The first version forced on every carrier request and re-rolled an identical set three times in one
        # run: the content was stable, but `resets` became meaningless and the real roll was buried in audit
        # noise, which is an instrument reporting a fiction.
        day, ids, rolled = player.roll_daily_quests(day, ids, plan.get("pools") or [],
                                                    force=bool(str(DAILY_QUEST_SET or "").strip()))
        _daily_inject_complete(player, day)
    rows = _daily_quest_rows(player, day or player.daily_quest_day())
    if on_rows and not rows:
        _note(f"daily quest: nothing to serve (pools={plan.get('pools')}) {note}".rstrip())
        return spec
    spec = _as_spec(spec)
    if rows:
        spec[DAILY_QUEST_TAG] = rows
    # The screen's own state. The CURRENT quest is the first row that is not yet complete, because the claim
    # reads it back off the scene (`SpDailyQuestObject::onButtonTouched` 0x253f6f4 asks the scene for its
    # daily-quest id and puts it in the request), so it has to be a row that is actually claimable.
    current = next((r[DQ_ID] for r in rows if int(r.get(DQ_STATUS) or 0) == DQ_STATUS_INCOMPLETE), "")
    if not current and rows:
        current = rows[0][DQ_ID]
    # `done` is "every row is complete AND UNCLAIMED" -- an exact test, not `>= DQ_STATUS_COMPLETE`. A claimed
    # row is `2`, so `>=` kept the flag at 1 forever and the popup re-fired on every home refresh, which is
    # what the operator saw. The popup means "you finished the day's quests, here is the reward"; once the
    # reward is taken it must stop. This is wrong with NO knobs involved too: a real player who claims
    # everything would get the message forever.
    done = bool(rows) and all(int(r.get(DQ_STATUS) or 0) == DQ_STATUS_COMPLETE for r in rows)
    if forced:
        done = True
        _note("daily quest: DQI_SHOW_COMPLETE FORCED by DAILY_QUEST_SHOW_COMPLETE -- the completion popup is "
              "not earned here, it is an A/B.")
    info = {
        DQI_REMAINING: str(_daily_remaining_seconds()),
        # The level-up lapis, at the SAME authored amount the mission-result rank-up already pays
        # (`RANKUP_BONUS_LAPIS`, live-proven 2026-09-22). The popup DISPLAYS a reward that was already
        # credited -- it does not credit it again -- so the two numbers have to agree or the popup would
        # advertise a bonus the balance never received. Zero while no level-up is involved.
        DQI_LEVEL_UP_DIA: str(int(DQI_RANKUP_LAPIS) if (done and _daily_team_lv_string(player)) else 0),
        DQI_SHOW_COMPLETE: "1" if done else "0",
    }
    # The CURRENT quest id is sent ONLY when we also sent the rows. Off the quest screen there is no row to
    # name, and an empty `nYD7LvSG` would be `setDailyQuestId(0)` -- i.e. the home refresh would CLEAR the
    # client's current quest. Omitting the key is the honest form (and `JsonNode::addParam` 0x1f11bd8 drops
    # empty strings anyway, so this is belt and braces).
    if current:
        info[DQ_ID] = current
    # `tzCNMJ87` -> parseTeamLvInfo. OMITTED when no rank-up has been recorded: the parser is strict, so a
    # wrong shape renders an EMPTY popup rather than an error, and a fabricated "1 -> 1" would draw a level-up
    # that never happened. Before this token existed the popup's awards list came up empty -- which is what the
    # operator's run reported, and the reason this is the one flag that was missing.
    team_lv = _daily_team_lv_string(player)
    if team_lv:
        info[DQI_TEAM_LV] = team_lv
        if not (player.data.get("rank") or {}).get("last_up"):
            _note("daily quest: tzCNMJ87 is a DAILY_QUEST_TEAM_LV DISPLAY A/B derived from the current rank "
                  f"({team_lv}) -- no real rank-up is recorded, so this is NOT a fact about the account")
    spec[DAILY_QUEST_INFO_TAG] = [info]
    _note(f"daily quest: {len(rows)} row(s) for {day or player.daily_quest_day() or '-'} "
          f"pools={'/'.join(plan.get('pools') or [])} rolled={rolled} current={current or '-'} "
          f"all_done={done} teamLv={team_lv or 'ABSENT (no rank-up recorded)'}"
          + (" FORCED" if forced else "") + (f" [{note}]" if note else ""))
    # The one thing that must never be silent: a square the player cannot tick. `PROGRESS_REQUESTS` is the
    # whole set of facts we can attribute, so a row whose action is not in it can only move by hand -- and a
    # daily quest that sits at 0/5 forever looks exactly like a broken one.
    if rows:
        catalogue = daily_quest_by_id()
        stranded = sorted({str(catalogue[r[DQ_ID]].get("action")) for r in rows
                           if str(r[DQ_ID]) in catalogue
                           and str(catalogue[r[DQ_ID]].get("action")) not in PROGRESS_REQUESTS})
        if stranded:
            _note(f"daily quest: {len(stranded)} of {len(rows)} row(s) name an action no request carries yet "
                  f"({', '.join(stranded)}) -- they will sit at 0 until one is located (SW-122)")
    return spec


def claim_daily_quest(spec, reply_id, plaintext=None):
    """The CLAIM: `DailyQuestClaimRewardRequest` `Zy8fYJ5e` (one) and `DCmya9WD` (all).

    The body is measured, not assumed -- `createBody` 0x1999ef4 emits `{"vj87UqEd": [{"nYD7LvSG": "<id>"}]}`
    and emits the group EMPTY when the id is 0, so an empty list is a legitimate wire state and not a parse
    failure. `nYD7LvSG` is the same token the row and the info response use, which is why one constant serves
    all three.

    Three rules, all from the panel claim's lessons (`panel_quest.md` §4i):
      * **verify before paying** -- a quest that is not at its target is REFUSED, never paid;
      * **a claim is not a clear and not progress** -- it records `claimed/daily/<day>/<id>` capped at 1, which
        is what makes a second tap a no-op across restarts;
      * **report what was CREDITED, not what was parsed** -- `_grant_items` skips a row it cannot route, and a
        log line that says "paid" for a reward that went nowhere is worse than no line at all.
    """
    if State.player is None or reply_id not in (DAILY_QUEST_CLAIM, DAILY_QUEST_CLAIM_ALL):
        return spec
    player = State.player
    rows = daily_quest_by_id()
    day = player.daily_quest_day() or time.strftime(DAILY_QUEST_DAY_FORMAT)
    ids = []
    if reply_id == DAILY_QUEST_CLAIM_ALL:
        # Claim-all: every row this reply calls COMPLETE. It must read the SAME source the rows were rendered
        # from, or the page promises claims the selection cannot see -- the defect a run found, where the rows
        # were display-forced to complete and claim-all found nothing and said nothing.
        ids = [qid for qid in player.daily_quest_ids()
               if _daily_status_for(player, rows.get(qid) or {"id": qid}, day) == DQ_STATUS_COMPLETE]
        if not ids:
            # ALWAYS report an empty claim-all. The first version logged only when something was paid, refused
            # or echoed, so a claim-all that legitimately found nothing -- and a claim-all that found nothing
            # because it was looking in the wrong place -- produced the SAME silence, and the client's
            # "nothing happened" could not be told from a healthy no-op. An arrived request that produces no
            # log line is the failure mode this project keeps paying for.
            _note(f"daily quest claim-all: NOTHING claimable -- {len(player.daily_quest_ids())} row(s) served, "
                  f"none reads complete (day={day or '-'}"
                  + (", DAILY_QUEST_FORCE_COMPLETE is ON so the page shows claim buttons it cannot honour"
                     if _daily_rows_force_complete() else "") + ")")
            return spec
    else:
        req = _request_obj(plaintext).get(DAILY_QUEST_CLAIM_GROUP) if plaintext else None
        for row in (req if isinstance(req, list) else []):
            if isinstance(row, dict) and str(row.get(DQ_ID) or ""):
                ids.append(str(row[DQ_ID]))
        if not ids:
            _note(f"daily quest claim: no {DQ_ID} in {DAILY_QUEST_CLAIM_GROUP} -- nothing claimed")
            return spec
    paid, refused, echoed = [], [], []
    for qid in ids:
        quest = rows.get(qid)
        if not quest:
            refused.append(f"{qid}: not a servable id")
            continue
        if qid not in player.daily_quest_ids():
            refused.append(f"{qid}: not in today's set")
            continue
        target = int(quest.get("target") or 0)
        cur = player.progress_value(player.daily_progress_key(qid, day))
        if not (target and cur >= target):
            refused.append(f"{qid}: not complete ({cur}/{target})")
            continue
        ckey = player.daily_claim_key(qid, day)
        if player.progress_value(ckey):
            echoed.append(qid)                    # already paid: echo the state, pay nothing
            continue
        reward = [{"kind": k, "id": i, "count": c} for k, i, c in _parse_reward(quest.get("reward_info", ""))]
        credited = player._grant_items(reward) if reward else []
        if reward and len(credited) != len(reward):
            refused.append(f"{qid}: only {len(credited)}/{len(reward)} reward row(s) creditable")
            continue
        player.bump_progress(ckey, 1, cap=1, what=f"daily quest claim {qid} ({day})")
        paid.append(f"{qid}: " + (", ".join(f"{r['kind']}:{r['id']}x{r['count']}" for r in credited) or "no reward row"))
    if paid or echoed or refused:
        _note("daily quest claim: " + (f"paid {len(paid)} ({'; '.join(paid)})" if paid else "nothing paid")
              + (f"; already claimed {len(echoed)}" if echoed else "")
              + (f"; REFUSED {len(refused)} ({'; '.join(refused)})" if refused else ""))
    # REFRESH the rows in THIS reply. `serve_daily_quest` ran before the claim (it must: it is the builder
    # and `apply_progress` writes the counts), so without this the screen the player is looking at after the
    # tap still shows the pre-claim state until the client's next poll -- which for a claim is exactly the
    # moment the player is watching. The panel claim does the same thing with its own group
    # (`panel_quest.md` §4i), so this is the sibling's shape and not a new idea.
    if paid or echoed:
        fresh = _daily_quest_rows(player, day)
        if fresh:
            spec = _as_spec(spec)
            spec[DAILY_QUEST_TAG] = fresh
    return spec


def _welcome_items(credited: list) -> list:
    """The credited welcome currencies -> `(kind, id, count)` rows, so the mail speaks one grammar.

    The kind comes from `STORAGE_CURRENCY`'s inverse (the ONE currency table, `currency_model.md` §1), and
    the `id` follows the shipped schedule's convention: gil carries the amount in `id` as well as `count`
    (`30:10000`, `daily_login_bonus.md` §7.4), lapis uses `1`, everything else `0`. Only `count` is read on
    the grant path (`PlayerState.receive_mail`); `id` is there so the row is a valid schedule entry.
    """
    rows = []
    for w in credited or []:
        cur = str(w.get("currency"))
        kind = next((k for k, c in STORAGE_CURRENCY.items() if c == cur), None)
        if kind is None:
            continue
        amount = int(w.get("amount", 0) or 0)
        rows.append((kind, amount if kind == 30 else (1 if kind == 50 else 0), amount))
    return rows


def deliver_catchup_rewards(spec, reply_id):
    """Advance the catch-up schedules, pay what became due, announce it, and serve the progress groups.

    Runs on the login-bonus request (`CATCHUP_REQUESTS`) and on any user-state refresh, because the counters
    are OURS: the client keeps no count for the cumulative ladder or for a special campaign (it reads the
    day/state out of the reply, as it does for the daily calendar -- `daily_login_bonus.md` §7.3), so the
    server advances them once per calendar day and pays exactly the tier/day that fell due.

    Three grants, three mechanisms, all one-shot and all idempotent:
      * `welcome`  -- the AUTHORED fresh-account currencies, on the first login (SW-99);
      * `ladder`   -- every shipped tier the player's total-login days have passed, keyed by the tier's own
        `QLfe23bu` bit, so a gap pays every tier it crossed;
      * `campaign` -- the selected campaign's day, keyed by the day counter.

    Each is posted as ONE mail in the DAILY-LOGIN convention (title `SG_DAILY_LOGIN_TITLE`, message = the
    day), and **the mail is the award**: nothing is credited when it is delivered, only when the client's
    Receive button claims it (`receive_mail` -> `_grant_items`), exactly as the daily calendar's award and
    the anniversary ticket work (`deliver_login_mail`, `deliver_anniversary_ticket`). Mail id bases are
    per-mechanism so a day's three grants cannot collide.
    """
    if State.player is None or not isinstance(spec, dict):
        return spec
    plan = _catchup_plan()
    if not plan:
        return spec
    if reply_id in CATCHUP_BLOCKED_REQUESTS:
        # RoutineHomeUpdateRequest's reply clears the SP list on arrival (ffbe_config's note on
        # CATCHUP_BLOCKED_REQUESTS) -- carrying `n5W9Et2I` there would wipe the campaign every home cycle.
        return spec
    is_login_reply = reply_id in CATCHUP_REQUESTS
    is_user_state = CATCHUP_USER_STATE and any(g in spec for g in USER_STATE_GROUPS)
    if not (is_login_reply or is_user_state):
        return spec

    today = time.strftime("%Y-%m-%d")
    player = State.player
    granted: list = []

    welcome = player.grant_catchup_welcome(plan.get("welcome") or {})
    if welcome:
        granted.append(("welcome", 0, _welcome_items(welcome)))

    due = player.advance_catchup(today, plan.get("campaign") or "", plan.get("days") or 0)
    if due.get("advanced"):
        if plan.get("ladder"):
            for tier in player.pay_catchup_ladder((catchup_bonus().get("total_ladder") or {}).get("tiers") or []):
                granted.append(("ladder", int(tier.get("days") or 0),
                                [(e.get("kind"), e.get("id"), e.get("count")) for e in tier.get("items") or []]))
        cid, day = due.get("campaign_id", ""), int(due.get("campaign_day") or 0)
        if cid and 0 < day <= int(plan.get("days") or 0):
            rows = _parse_reward((plan.get("rewards") or {}).get(str(day), ""))
            if rows:
                player.pay_catchup_day(cid, day, [{"kind": k, "id": i, "count": c} for k, i, c in rows])
                granted.append(("sp", day, rows))

    for kind, day, items in granted:
        base = CATCHUP_MAIL_ID_BASE.get(kind)
        if base is None or not items:
            continue
        player.deliver_mail("SG_DAILY_LOGIN_TITLE", [(int(k), int(i), int(c)) for k, i, c in items],
                            mail_id=str(base + int(day or 0)), message=str(int(day or 0)), daily_login=True)

    # The progress groups. Only on a login-bonus reply or a user-state refresh -- the two places the
    # family's scenes read (HOME's `checkLoginBonusScene` and the login-bonus screens); an unrelated reply
    # would only carry state the client has no reader for.
    if is_login_reply or is_user_state:
        spec = dict(spec)
        if plan.get("ladder"):
            # `Bbc6pF9Q` is the LADDER'S AXIS only: the 29 tiers are the shipped
            # `F_LOGIN_BONUS_TOTAL_REWARD_MST`, which the scene reads itself. `juA0Z4m7` (the required switch)
            # is deliberately NOT sent -- a non-empty switch predicate is evaluated and a wrong one is
            # treated as invalid (`UserLoginBonusTotalInfo::isValid` 0x1d95988), so omitting it is the safe
            # reading of "no condition".
            spec[CATCHUP_TOTAL_TAG] = [{CATCHUP_LOGIN_DAYS: str(due.get("total_login_days", 0))}]
        if plan.get("campaign"):
            days = sorted(int(d) for d in (plan.get("rewards") or {}))
            start, end = _ymd_now()
            spec[CATCHUP_SP_MST_TAG] = [{CATCHUP_SP_ID: str(plan["campaign"]),
                                        CATCHUP_START_YMD: start, CATCHUP_END_YMD: end,
                                        CATCHUP_SP_TODAY: "0",          # 0 = the FULL calendar, not "today"
                                        CATCHUP_SP_TARGET_TYPE: "0",
                                        CATCHUP_SP_BANNER: "", CATCHUP_SP_BG: ""}]
            spec[CATCHUP_SP_TAG] = [{CATCHUP_SP_ID: str(plan["campaign"]),
                                     CATCHUP_DAY_LIST: CATCHUP_DAY_SEP_SP.join(str(d) for d in days),
                                     CATCHUP_START_YMD: start, CATCHUP_END_YMD: end}]
            spec[CATCHUP_INFO_TAG] = [{CATCHUP_BONUS_ID: str(plan["campaign"]),
                                       CATCHUP_DAY_LIST: _info_day_list(today)}]
        # `68Vn2FrK` is the CONSECUTIVE-day count the new-player calendar renders, and it is fed by the
        # shipped `F_LOGIN_BONUS_MST` row whose window (20210830-20301231) contains today; `fHSZ74Qm` = 0 is
        # the client's own "use the device clock" sentinel (`getCurrentLoginBonusMst` 0x1c6adbc), so the
        # 15-day shipped ladder draws with its REAL shipped rewards and nothing is authored.
        # `cksL9bW8` is the crossed-ladder-days list. Its exact grammar is NOT recovered -- the sibling
        # `uJK9ac2V` day list is comma-separated and this is read the same way by
        # `LoginBonusUtil::getNextLoginBonusSceneId` 0x1abd6a8, so a comma list of the tier thresholds the
        # player has passed is the best-evidence encoding. INFERRED, and it only decides WHICH scene HOME
        # prefers, so a wrong guess costs a different screen rather than a broken one.
        spec[CATCHUP_OPE_TAG] = [{CATCHUP_SERIAL: str(int(due.get("serial_days", 0) or 0)),
                                  CATCHUP_DAY_INFO: CATCHUP_DAY_SEP_SP.join(
                                      str(d) for d in _ladder_days_passed(plan, player)),
                                  CATCHUP_PROCESS_DAY: "0"}]

    if granted or due.get("advanced"):
        _note("catchup: " + "; ".join(
            f"{kind} day {day}: " + ", ".join(f"{k}:{i}x{c}" for k, i, c in items)
            for kind, day, items in granted)
            + (f" | total_login_days={due.get('total_login_days')}" if due.get("advanced") else "")
            + f" | campaign {plan.get('campaign') or '-'} day {due.get('campaign_day')}")
    return spec
# --- PANEL QUEST (SW-121) ------------------------------------------------------------------------------
# The screen sends one request and reads three server-side groups; the ROWS and the NAMES are shipped MST the
# client must fetch (see ffbe_config's block). Selection comes from the season facet; content from the tables.
_PQ: dict = {}
def panel_quest_tables() -> dict:
    """`{"groups": {gid: row}, "quests": {gid: [row…]}}` from the two shipped MST tables, loaded once.

    Read from `mst_decoded/` at SERVE time rather than copied into a generated `server/*.json`, which is the
    point: the screen's rows are `[FFBEWW]` data, so there is no second copy to drift and no generator to
    re-run. (The generated-table pattern earns its keep when we must RESHAPE data -- community names resolved to
    shipped ids, 412 campaigns made browsable -- which is not the case here; `player_state` reads `mst_decoded`
    the same way for the unit tables.)
    """
    if _PQ:
        return _PQ
    mst = MST_DIR
    groups: dict = {}
    quests: dict = {}
    for fn, tok_gid, tok_qid, into in (
            ("F_PANEL_QUEST_GROUP_MST.ndjson", "zF6QIY2r", None, groups),
            ("F_PANEL_QUEST_MST.ndjson", "zF6QIY2r", "V36ygRYs", quests)):
        path = os.path.join(mst, fn)
        if not os.path.isfile(path):
            _note(f"panel quest: {path} is missing -- the screen will have no rows")
            continue
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                gid = str(row.get(tok_gid, ""))
                if not gid:
                    continue
                if tok_qid is None:                       # the group table: one row per group id
                    groups[gid] = row
                else:                                     # the quest table: many rows per group id
                    quests.setdefault(gid, []).append(row)
    for rows in quests.values():
        rows.sort(key=_disp_order_key)
    _PQ["groups"], _PQ["quests"] = groups, quests
    return _PQ


def panel_quest_picked() -> list:
    """The selected `[(groupId, groupRow)]`, season facet over `PANEL_QUEST_DEFAULT_GROUPS`.

    ONE selection, two callers -- the screen's opener and the CLAIM, which must send the same full row set
    for the same reason the SP-challenge claim does (see `panel_quest_info_rows`). A season OVERRIDES the
    default; it does not enable it, because the icon is on by construction and a visible, tappable screen
    that only works under a season is a broken default.
    """
    if State.player is None:
        return []
    season = getattr(st, "SEASON", None)
    wanted = (season.panel_quest_groups if season is not None else []) or list(PANEL_QUEST_DEFAULT_GROUPS)
    groups = (panel_quest_tables().get("groups") or {})
    picked = []
    for gid in wanted:
        row = groups.get(str(gid))
        if row is None:
            _note(f"panel quest: group {gid} is not in F_PANEL_QUEST_GROUP_MST -- ignored "
                  f"(a season selects ids, it does not create them)")
            continue
        picked.append((str(gid), row))
    return picked


def panel_quest_info_rows(picked) -> list:
    """The COMPLETE `ELPwhZj0` row set for the selection, projected from the live progress store.

    **Complete, for the same reason as `sp_challenge_info_rows`.**
    `sgPanelQuestUserQuestInfoResponse::onReadStarted` (`0x19594ec`) is byte-for-byte the same shape as the
    SP one: `if (response+0x24 == 0) UserQuestInfoList::shared()->removeAllObjects()`, and no wire token
    sets `+0x24`. So a claim reply carrying only the claimed row un-clears every other row of the schedule on
    screen (audit 2026-09-27). `serve_panel_quest` used to build these as literal `0`s and let
    `apply_progress` project them; this builder reads the store directly so the claim can call it, and
    `apply_progress`'s projection over the result is idempotent.
    """
    quests = (panel_quest_tables().get("quests") or {})
    out = []
    for gid, grow in picked:
        sched = str(grow.get(PQ_SCHEDULE_ID, ""))
        for q in (quests.get(gid) or []):
            qid = str(q.get(PQ_QUEST_ID, ""))
            meta = _quest_index().get(qid) or {}
            mx = int(meta.get("max") or 0)
            done = player_progress(qid, meta)
            out.append({PQ_SCHEDULE_ID: sched, PQ_GROUP_ID: gid, PQ_QUEST_ID: qid,
                        PQ_PROGRESS: str(done), PQ_CLEAR: "1" if (mx and done >= mx) else "0",
                        PQ_REWARD_FLAG: "1" if State.player.progress_value(
                            _progress_key("claimed", gid, qid)) else "0"})
    return out


def serve_panel_quest(spec, reply_id):
    """Answer `sgPanelQuestEntryRequest` (`GVyjSq16`) — the PANEL QUEST screen's only request.

    An empty reply is the connection error the operator hit, and the three groups below are the whole
    substance: the shipped tables carry the quest rows, the group rows and the names, so what is left is which
    groups are live, that they are live **at all**, and the player's progress through them.

    * `nnR59m0N` — one schedule row per selected (schedule, group), with an AUTHORED evergreen window (no
      shipped table dates a panel-quest schedule) whose string FORMAT is inferred (`medium`, see the config note);
    * `UvwpPAtp` — one status row per schedule. **Not optional:** `getActivePanelQuests` 0x1cbbc08 skips any
      schedule whose status vector is empty (`cmp w8,#0; b.le`), so a reply with the other two groups and no
      status rows serves successfully and still shows an empty screen;
    * `ELPwhZj0` — one row per shipped quest in the selected groups, progress 0 / uncleared / unclaimed. The
      conditions are the shipped `Pzn5h0Ga` values and are evaluated by the client, so nothing is authored here.

    `jksIZE9U` (the extra-reward claim flags) is deliberately NOT sent: its `xm7c08IS` RewardType is a string
    match key against a vocabulary nothing in the corpus names, and an empty claim-flag list is the honest
    "nothing claimed" state. Recorded rather than guessed.
    """
    if reply_id != PANEL_QUEST_REQUEST or State.player is None:
        return spec
    # `spec` is None here, and that is NORMAL, not a bug: this request has no `responses.json` entry, so the
    # dispatcher never built a payload for it and `reply_pipeline` is handed None. A transform that ADDS groups
    # must therefore create the dict rather than require one -- and the request/API map says so in advance:
    # this row's served verdict reads "served by the TRANSFORM LAYER only (no responses.json spec)". Reading
    # that as a description instead of a requirement is what made the first version a silent no-op: it guarded
    # `isinstance(spec, dict)`, returned the None untouched, and the client got the same empty body as before.
    spec = _as_spec(spec)
    picked = panel_quest_picked()
    if not picked:
        return spec
    tables = panel_quest_tables()
    groups, quests = tables.get("groups") or {}, tables.get("quests") or {}
    now = time.gmtime()
    end = time.gmtime(time.time() + PANEL_QUEST_WINDOW_DAYS * 86400)
    fmt = lambda t: time.strftime(PANEL_QUEST_WINDOW_FORMAT, t)   # noqa: E731 - a local alias, used twice

    schedules, statuses, infos = [], [], []
    for gid, grow in picked:
        if gid not in PANEL_QUEST_NAMED_GROUPS:
            # Not fatal and not blocked: the rows are real, only their NAMES would be raw keys, because
            # `F_TEXT_PANEL_QUEST_NAME` names `MST_PANEL_QUEST_NAME_<row id>` and this group's rows are not in
            # it. Say so loudly -- a screen of `MST_PANEL_QUEST_NAME_10011` looks like a data bug and is a
            # selection mistake.
            _note(f"panel quest: group {gid} has NO shipped name rows (F_TEXT_PANEL_QUEST_NAME covers "
                  f"{len(PANEL_QUEST_NAMED_GROUPS)} groups) -- its quest titles will render as raw keys")
        sched = str(grow.get(PQ_SCHEDULE_ID, ""))
        schedules.append({PQ_SCHEDULE_ID: sched, PQ_GROUP_ID: gid, PQ_PERSISTENT: "1",
                          PQ_DISP_ORDER: str(grow.get(PQ_DISP_ORDER, "") or "1"),
                          PQ_START: fmt(now), PQ_END: fmt(end), PQ_LIMIT: fmt(end)})
        # One status row per GROUP. The list is keyed by GROUP id: `SpChallengeStatusResponse::readParam`
        # stores with `setObject(info, [this+0x2c])` (0x1954838) and `ChallengeData::init` looks it up with
        # the schedule's `+0x2c` -- the same field, so one-per-group is what makes the lookup land.
        if not any(s[PQ_SCHEDULE_ID] == sched for s in statuses):
            statuses.append({PQ_SCHEDULE_ID: sched, PQ_REWARD_COUNT: "0", PQ_IS_NEW: "0"})
        for q in (quests.get(gid) or []):
            infos.append({PQ_SCHEDULE_ID: sched, PQ_GROUP_ID: gid,
                          PQ_QUEST_ID: str(q.get(PQ_QUEST_ID, "")),
                          PQ_PROGRESS: "0", PQ_CLEAR: "0", PQ_REWARD_FLAG: "0"})
    out = dict(spec)
    out[PANEL_QUEST_STATUS_TAG] = statuses
    out[PANEL_QUEST_SCHEDULE_TAG] = schedules
    out[PANEL_QUEST_INFO_TAG] = infos
    _note(f"panel quest: groups {[g for g, _ in picked]} -> {len(schedules)} schedule(s), "
          f"{len(statuses)} status row(s), {len(infos)} quest row(s)")
    return out


# --- SP CHALLENGE (the Rookie / Advanced / Expert / Parameter quest schedules) -----------------------
# The THIRD quest family and the one the two permanent schedules live in. Same shape as the panel quest --
# one request, a schedule master we author, rows the shipped `F_SP_CHALLENGE_MST` supplies -- with two
# differences that are the whole of what was wrong before: the SCHEDULE ID is recovered from the shipped
# name key rather than assumed equal to the group id (see `SP_NAME_KEY_*` in ffbe_config), and the progress
# is read from the live store so the claim state can be served.
_SPC: dict = {}


def sp_challenge_tables() -> dict:
    """`{"groups": {gid: [row…]}, "names": set(key), "schedule_names": {gid: key}}` from the SHIPPED data.

    `F_SP_CHALLENGE_MST` for the rows (1468 in 135 groups) and `text_decoded/F_TEXT_SPCHALLENGE.ndjson` for
    the names, read at SERVE time for the same reason the panel quest's are: the content is `[FFBEWW]` data,
    so there is no second copy to drift and no generator to re-run.

    The name sets exist to make the two name rules CHECKABLE rather than assumed:
      * a schedule's name is `MST_SPCHALLENGE_SCHEDULE_NAME_<scheduleId>` (the client builds the key), and
        the id is recovered as `group*10+1` when that key ships -- 83 of the 135 groups have one, including
        all four permanent ones, and the 32 that do not are event schedules on other id conventions;
      * a challenge's name is `MST_SPCHALLENGE_NAME_<challengeId>`, which the client reads off its own MST
        row, so a served challenge outside this set renders a raw key.

    `titles` holds the resolved English text of every key, so the serve log can name a schedule the way the
    screen will ("Advanced Quests") instead of printing the key it is keyed on.
    """
    if _SPC:
        return _SPC
    mst = os.path.join(MST_DIR, "F_SP_CHALLENGE_MST.ndjson")
    groups: dict = {}
    if not os.path.isfile(mst):
        _note(f"sp challenge: {mst} is missing -- the screen will have no rows")
    else:
        with open(mst, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                gid = str(row.get(SP_GROUP_ID, ""))
                if gid:
                    groups.setdefault(gid, []).append(row)
    for rows in groups.values():
        rows.sort(key=_disp_order_key)
    names: set = set()
    values: dict = {}
    path = os.path.join(TEXT_DIR, "F_TEXT_SPCHALLENGE.ndjson")
    if not os.path.isfile(path):
        _note(f"sp challenge: {path} is missing -- no name key can be verified, so the schedule and "
              f"challenge names are expected to render as raw keys")
    else:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                key = row.get("key")
                if not isinstance(key, str):
                    continue
                names.add(key)
                val = row.get("v")
                if isinstance(val, list) and val and isinstance(val[0], str):
                    values[key] = val[0]
    # The SCHEDULE id is LOOKED UP, not synthesised -- audit 2026-09-27. The client builds the name key from
    # `schedule+0x24`, and the shipped text table is the only record of what that number is: 115 keys, of
    # which **91** resolve onto a group we ship -- 83 as `group*10 + 1` and **8 as `group*10 + 0`**
    # (5010->50100, 5247->52470, 5300->53000, 5301->53010, 5332->53320, 5344->53440, 8000->80000,
    # 990001->9900010). So the `*10+1` rule is right for 83 and WRONG for 8, and a synthesiser would build a
    # key that does not exist for those eight -- an empty title, with the log silent. We therefore read the id
    # off the key that exists, which is RECOVERED for all 91 and no longer depends on the rule being
    # universal. A group with no key at all keeps the synthesised id and is reported, because the client's
    # `getName` will then build a key no row carries -- the same failure, named.
    sched_names, sched_ids = {}, {}
    for gid in groups:
        try:
            g = int(gid)
        except (TypeError, ValueError):
            continue
        for suffix in (g * SP_NAME_KEY_MULT + SP_NAME_KEY_ADD, g * SP_NAME_KEY):
            key = SP_NAME_KEY_PREFIX + str(suffix)
            if key in names:
                sched_names[gid], sched_ids[gid] = key, str(suffix)
                break
        else:
            sched_ids[gid] = str(g * SP_NAME_KEY_MULT + SP_NAME_KEY_ADD)
    _SPC["groups"], _SPC["names"], _SPC["schedule_names"] = groups, names, sched_names
    _SPC["schedule_ids"], _SPC["titles"] = sched_ids, values
    return _SPC


def _disp_order_key(row: dict):
    """Sort a quest row by its DISPLAY ORDER **numerically**, with the id as a tie-break.

    `Z0EN6jSh` is a string in the table and the orders are 10, 20, ... 180, so a plain string sort puts
    `110` before `20` -- which is exactly the order the Advanced Quests came out in before this key
    existed (`110101, 110110..110118, 110102..110109`). A display order that is not in display order is a
    quiet defect: every row is present and every name resolves, so nothing looks broken.
    """
    try:
        order = int(str(row.get("Z0EN6jSh", "")).strip())
    except (TypeError, ValueError):
        order = 1 << 30
    return (order, str(row.get(SP_CHALLENGE_ID, "")))


def sp_schedule_id(group_id) -> str:
    """The SCHEDULE id for a group -- READ from the shipped name key, not synthesised.

    `SpChallengeScheduleMst::getName` (0x1c7a510) builds `MST_SPCHALLENGE_SCHEDULE_NAME_` + this number, so
    the shipped text table is the only record of what the number must be, and it is not one rule: 83 of the
    135 groups key their name at `group*10 + 1` and **8 at `group*10 + 0`** (audit 2026-09-27). So
    `sp_challenge_tables` resolves the id by looking for a key that exists, and this reads that answer. The
    `group*10 + 1` fallback is only for a group with NO name row at all, which the serve path reports.

    One derivation, four callers (the serve path, the claim path's echoed row, the viewer, the selftest) --
    because two copies of a derivation is the collapse candidate the state-writer audit keeps pointing at.
    """
    gid = str(group_id)
    table = sp_challenge_tables()
    return str(table.get("schedule_ids", {}).get(gid)
               or int(gid) * SP_NAME_KEY_MULT + SP_NAME_KEY_ADD)


def sp_schedule_category(disp_type: str, reset_type: str, persistent: str) -> int:
    """`SpChallengeScheduleMst::getCategory` (0x1cb5044) in Python -- the tab a schedule lands in.

    Read at the address: `dispType == 3` wins, else a non-zero `resetType` means daily (category 0), else
    `persistent == 1` is category 1 and anything else is category 2. `SpChallengeTopScene` then walks the
    4-entry table at 0x2fccc54 -- {0, 1, 3, 2} -- and `getMstWithCategory` keeps the schedules of the tab's
    category, so this is the difference between a schedule being reachable and being invisible.
    """
    if str(disp_type) == "3":
        return 3
    if str(reset_type) not in ("", "0"):
        return 0
    return 1 if str(persistent) == "1" else 2


def sp_challenge_picked() -> list:
    """The selected `[(groupId, [shipped rows…]), …]`, season facet over `SP_CHALLENGE_DEFAULT_GROUPS`.

    ONE selection, two callers -- the screen's opener and the CLAIM, which must send the same full row set
    (audit 2026-09-27). An unknown group id is reported and dropped, never replaced: a facet that invents
    content is how "the screen shows nothing" becomes unexplainable.
    """
    if State.player is None:
        return []
    season = getattr(st, "SEASON", None)
    wanted = (season.sp_challenge_groups if season is not None else []) or list(SP_CHALLENGE_DEFAULT_GROUPS)
    groups = sp_challenge_tables()["groups"]
    picked = []
    for gid in wanted:
        rows = groups.get(str(gid))
        if not rows:
            _note(f"sp challenge: group {gid} is not in F_SP_CHALLENGE_MST -- ignored "
                  f"(a season selects ids, it does not create them)")
            continue
        picked.append((str(gid), rows))
    return picked


def sp_challenge_info_rows(picked) -> tuple:
    """The COMPLETE `3KLU4mSD` row set for the selection, plus the claimable count per group.

    **Complete is the load-bearing word.** `UserSpChallengeInfoResponse::onReadStarted` (0x1923d4c) does
    `UserSpChallengeInfoList::shared()->removeAllObjects()` unless the response's own `+0x24` is non-zero --
    and no wire token sets it, so the group always clears. The display list is built from the SHIPPED MST rows
    (`SpChallengeMstList::getDispListFromGroupId` 0x1cb6244 walks the MST and calls
    `getInfoFromChallengeId` per row), so a missing row does not hide a quest -- it drops that quest into the
    NOT-CLEARED bucket, because a null user row is what "not clear" looks like. So a reply carrying one row
    does not truncate the list, but it silently un-clears the other 48 (audit 2026-09-27). The claim therefore
    re-sends the whole set, and the claimed row is already in it with `QCLs6SV8 = 1`.
    """
    infos, claimable = [], {}
    names = sp_challenge_tables()["names"]
    for gid, rows in picked:
        n = 0
        sched_id = sp_schedule_id(gid)
        # A per-ROW name check, not a per-group one. The nameless rows are NOT all in 9001: of the 142
        # challenge rows with no `MST_SPCHALLENGE_NAME_<id>` key, **100 are in 9001 and 42 are spread over ten
        # other groups** (3018-3024, 5003, 5004, 5006, 5007, 99063) -- audit 2026-09-27. A rule keyed on 9001
        # would mislabel those 42 the moment a season selected one of their groups.
        nameless = [str(q.get(SP_CHALLENGE_ID, "")) for q in rows
                    if f"MST_SPCHALLENGE_NAME_{q.get(SP_CHALLENGE_ID)}" not in names]
        if nameless:
            _note(f"sp challenge: group {gid} has {len(nameless)} row(s) with NO shipped quest name "
                  f"(first {nameless[0]}) -- those titles will render as raw keys")
        for q in rows:
            qid = str(q.get(SP_CHALLENGE_ID, ""))
            if not qid:
                continue
            meta = _quest_index().get(qid) or {}
            mx = int(meta.get("max") or 0)
            done = player_progress(qid, meta)
            claimed = State.player.progress_value(_progress_key("claimed", gid, qid))
            if done and mx and done >= mx and not claimed:
                n += 1
            infos.append({SP_SCHEDULE_ID: sched_id, SP_GROUP_ID: gid, SP_CHALLENGE_ID: qid,
                          SP_PROGRESS: str(done), SP_CLEAR: "1" if (mx and done >= mx) else "0",
                          SP_REWARD_FLAG: "1" if claimed else "0"})
        claimable[gid] = n
    return infos, claimable


def serve_sp_challenge(spec, reply_id):
    """Answer `SpChallengeEntryRequest` (`MTf2j9aK`) -- the SP-challenge list, both scenes.

    Four groups, and each earns its place:
      * `NI12Hvxy` -- one schedule row per selected group. The master is not shipped, so its flags and dates
        are AUTHORED (an evergreen window, `dispType` 0 / `resetType` 0 / `persistent` 1 => category 1), and
        the SCHEDULE ID is the one recovered fact: `group*10+1`, which is what makes the client's
        `getName` build a key the shipped text table actually carries. A group with no such key is served
        WITH a warning, never quietly as a nameless row.
      * `WS1g0ECn` -- one status row per schedule, carrying the real `rewardCount` (the HOME icon badge
        reads it through `sgHomeIconUtils::setSpChallengeNotif`) and `isNew`.
      * `3KLU4mSD` -- one row per shipped challenge, with progress / cleared / received read from the live
        store. This group is DESTRUCTIVE on arrival (`UserSpChallengeInfoResponse::onReadStarted` clears the
        list), so it is only ever served on this family`'s own request, never broadcast onto a general
        refresh -- the rule that keeps a stale progress list from wiping the screen.
      * `cT7S0ikb` -- the scrolling ticker: two MST text KEYS per row (schedule name, challenge name), both
        shipped. The PAIRING is authored (one row per served schedule, with that schedule's first challenge
        in display order); an empty ticker list is legal -- `getNextTickerLog` falls back to an empty string
        (the literal at 0x2f50a80 is the empty string) -- so this is completeness, not a requirement.

    The UserInfo group rides the same reply (`MTf2j9aK` is a collided wire id shared with `GrandMissionEntry`)
    and is left exactly as the payload had it.
    """
    if reply_id != SP_CHALLENGE_REQUEST or State.player is None:
        return spec
    spec = _as_spec(spec)
    picked = sp_challenge_picked()
    if not picked:
        return spec
    tables = sp_challenge_tables()
    names, sched_names = tables["names"], tables["schedule_names"]
    disp_type, reset_type, persistent = "0", "0", "1"
    cat = sp_schedule_category(disp_type, reset_type, persistent)
    now = time.gmtime()
    end = time.gmtime(time.time() + SP_SCHEDULE_WINDOW_DAYS * 86400)
    fmt = lambda t: time.strftime(SP_SCHEDULE_WINDOW_FORMAT, t)   # noqa: E731 - a local alias, used twice

    infos, claimable = sp_challenge_info_rows(picked)
    schedules, statuses, tickers = [], [], []
    for i, (gid, rows) in enumerate(picked):
        sched_id = sp_schedule_id(gid)
        key = sched_names.get(gid)
        if key is None:
            _note(f"sp challenge: group {gid} has NO shipped schedule name at "
                  f"{SP_NAME_KEY_PREFIX}{sched_id} -- its title will render as a raw key (the client's "
                  f"getName builds that key from the schedule id, so the id is what is wrong, not the text)")
        schedules.append({SP_SCHEDULE_ID: sched_id, SP_GROUP_ID: gid, SP_DISP_TYPE: disp_type,
                          SP_RESET_TYPE: reset_type, SP_PERSISTENT: persistent, SP_SWITCH_INFO: "",
                          SP_DISP_ORDER: str(i), SP_START: fmt(now), SP_END: fmt(end),
                          SP_REWARD_LIMIT: fmt(end)})
        statuses.append({SP_SCHEDULE_ID: sched_id, SP_GROUP_ID: gid,
                         SP_REWARD_COUNT: str(claimable.get(gid, 0)), SP_IS_NEW: "0"})
        if key and names:
            first = str(rows[0].get(SP_CHALLENGE_ID, ""))
            ckey = f"MST_SPCHALLENGE_NAME_{first}"
            if ckey in names:
                tickers.append({SP_TICKER_SCHEDULE_NAME: key, SP_TICKER_CHALLENGE_NAME: ckey})
    out = dict(spec)
    out[SP_CHALLENGE_STATUS_TAG] = statuses
    out[SP_CHALLENGE_SCHEDULE_TAG] = schedules
    out[SP_CHALLENGE_INFO_TAG] = infos
    if tickers:
        out[SP_CHALLENGE_TICKER_TAG] = tickers
    _note(f"sp challenge: groups "
          + ", ".join(f"{g}='{tables['titles'].get(sched_names.get(g, ''), '?')}'"
                      f" [{len(rows)} quest(s)]" for g, rows in picked)
          + f" (tab category {cat}) -> {len(schedules)} schedule(s) {[s[SP_SCHEDULE_ID] for s in schedules]}, "
          f"{len(statuses)} status row(s) (claimable {[s[SP_REWARD_COUNT] for s in statuses]}), "
          f"{len(infos)} challenge row(s), {len(tickers)} ticker row(s)")
    return out


# --- SURVIVAL (the trial gauntlet; the mode's only possible entry is a reply) -----------------------

def _survival_area_facts() -> list:
    """`[(area_id, dungeon_id, first_mission_id, that mission's gQR24kST)]` -- resolved from the SHIPPED tables.

    THE FRAMEWORK IS THE POINT. Naming an area is enough; its dungeon and its first mission are facts the
    corpus already holds, so they are READ rather than typed. "First" means FILE ORDER, which is the order
    `getFirstMissionSubType` sees (its two list accessors are the first element of each vector).

    `gQR24kST` is reported, not obeyed: it is the CLIENT's routing input, and for a family whose areas are
    mostly not subType 13 it is usually 0. Serving a gate row for such an area is still meaningful -- the
    tile exists and the server has named it -- but the tile will take the DEFAULT branch of
    `GateScene::touchEnded`, not the survival one. That is a property of the area, not of our serving, and
    it is the honest limit of this feature.
    """
    try:
        mrows = _load_mst_rows("F_MISSION_MST.ndjson")["__rows__"]      # file order
        drows = _load_mst_rows("F_DUNGEON_MST.ndjson")["__rows__"]
    except (OSError, KeyError, ValueError) as exc:
        _note(f"survival: cannot read the MST corpus ({exc}); no area resolved")
        return []
    out = []
    seen = set()
    for area in SURVIVAL_AREAS:
        if area in seen:            # naming an area twice must not serve it twice: the lists we write are
            continue                # replace-on-read, so a duplicate row is pure noise
        seen.add(area)
        dungeons = [str(r.get("U9iHsau3")) for r in drows
                    if str(r.get("uv60hgDW")) == str(area) and r.get("U9iHsau3")]
        if not dungeons:
            _note(f"survival: area {area} has NO dungeon row in F_DUNGEON_MST -- nothing served for it")
            continue
        dungeon = dungeons[0]
        missions = [r for r in mrows if str(r.get("U9iHsau3")) == dungeon]
        if not missions:
            _note(f"survival: area {area} dungeon {dungeon} has no mission row -- skipped")
            continue
        first = missions[0]
        out.append((str(area), dungeon, str(first.get("qo3PECw6")), str(first.get("gQR24kST"))))
    return out

def _event_area_facts() -> list:
    """[(area, first_dungeon, first_mission)] per EVENT_GATE_AREAS entry, from the SHIPPED tables.

    The framework is the point, exactly as in _survival_area_facts: naming an area is enough, and the
    dungeon and the mission are READ, never typed -- so a new event is one env word. "First" is FILE ORDER,
    which is the order the client's own list accessors see.
    """
    try:
        drows = _load_mst_rows("F_DUNGEON_MST.ndjson")["__rows__"]
        mrows = _load_mst_rows("F_MISSION_MST.ndjson")["__rows__"]
    except (OSError, KeyError, ValueError) as exc:
        _note(f"event gate: cannot read the MST corpus ({exc}); no area resolved")
        return []
    out, seen = [], set()
    for area in EVENT_GATE_AREAS:
        if area in seen:
            continue
        seen.add(area)
        dungeon = next((str(r.get("U9iHsau3")) for r in drows
                        if str(r.get("uv60hgDW")) == str(area) and r.get("U9iHsau3")), "")
        if not dungeon:
            _note(f"event gate: area {area} has no F_DUNGEON_MST row -- NOT served "
                  f"(a tile with no dungeon takes no tap)")
            continue
        mission = next((str(r.get("qo3PECw6")) for r in mrows
                        if str(r.get("U9iHsau3")) == dungeon and r.get("qo3PECw6")), "")
        if not mission:
            _note(f"event gate: dungeon {dungeon} has no F_MISSION_MST row -- NOT served")
            continue
        out.append((str(area), dungeon, mission))
    return out


def serve_event_gate(spec, reply_id):
    """0xkoNP8U -> one gate row per EVENT_GATE_AREAS area, APPENDED to the carrier's own rows.

    SpDungeonCondMstResponse is what makes an area's tile exist at all (GateScene::setListLayout, and
    AreaMapManager::drawDungeon asks the same list per tile) -- the survival family's own lesson, and
    docs/notes/chamber_battle_authoring.md section 6 names this as the step a battle-authoring run does
    NOT cover. DERIVED: the area id is the env word, and its dungeon + first mission are read from the
    shipped tables. AUTHORED: the window, the tab (qnUhs32P, 1-BASED, 1 = Event), the 1/2 selector, and
    the art (reused from a shipped row so the client can resolve it -- see the config's collision note).

    APPENDED, never replaced: the list is replace-on-read, so this rides only the carriers that already
    serve it (EVENT_GATE_CARRIERS), and an area already present in the carrier's rows is left alone.
    """
    if State.player is None or reply_id not in EVENT_GATE_CARRIERS:
        return spec
    facts = _event_area_facts()
    if not facts:
        return spec
    spec = _as_spec(spec)
    rows = [r for r in (spec.get(EVENT_GATE_TAG) or []) if isinstance(r, dict)]
    have = {str(r.get("uv60hgDW")) for r in rows}
    added = 0
    for area, dungeon, mission in facts:
        if area in have:
            continue
        rows.append({
            "uv60hgDW": area,             # SubRegionId -- THE key the tile lookups match
            "U9iHsau3": dungeon,          # the dungeon the tile opens
            "qo3PECw6": mission,          # its first mission
            "qnUhs32P": EVENT_GATE_TAB,   # 1-BASED tab: 1 = Event
            "Z0EN6jSh": EVENT_GATE_KIND,  # +0x100 -- the 1/2 selector getAreaIdWithBannerTouch reads
            "VjJQ51uG": EVENT_GATE_WINDOW_START,   # AcceptedDate
            "m8ivD4NX": EVENT_GATE_WINDOW_END,     # CompletedDate
            "NTXoA6e2": EVENT_GATE_WINDOW_START,
            "V48um7Jw": EVENT_GATE_WINDOW_END,
            "7Gvw6J82": EVENT_GATE_WINDOW_END,
            "4yT7FUqj": EVENT_GATE_ART,   # the tile art (authored; see the config's collision note)
            # The shipped rows' remaining fields, with their own values, so the row's SHAPE matches what
            # the client has already parsed: the neutral zeros/empties where no reader was located.
            "0iWgxc1X": "0", "45wVefcS": "", "5mouF8P1": "", "B2FW5Qjq": "",
            "C8Qf5bs6": "0", "ET8L6iDw": "0", "M1p3ehVU": "", "MrxFQ63W": "",
            "QCLs6SV8": "0", "mTHe6pI0": "", "oY0vAuE9": "0", "vnhT2EM5": "",
            "xm7c08IS": "0", "z0ft1PK8": "0", "pAwU2R6W": "0", "td06MKEX": "0",
            "BGFw7qi4": "", "2oL4dNFy": "", "uwHx3jk0": "", "XY5ZfS0w": "", "0dh7yRIN": "",
        })
        added += 1
    if added:
        spec[EVENT_GATE_TAG] = rows
    return spec


def serve_survival(spec, reply_id):
    """Serve the Survival event row (`NsC8t4Qr`) -- the ONLY thing that can make its tiles exist.

    `SurvivalUtil::isPlayingSurvivalMission(areaId)` (`0x1a6ee20`) is
    `SurvivalEventInfoList::getObjectWithAreaId(areaId) != nullptr`; that list's only writer is
    `SurvivalEventInfoResponse::readParam` (`0x192ab4c`) on tag `NsC8t4Qr`; and `GateScene::setListLayout`,
    `SealAreaListScene::setLayoutList` and `AreaMapManager::drawDungeon` each ask that predicate per tile.
    So no shipped table can make the mode appear -- this reply is the gate. Home:
    `docs/notes/survival_mode_identification.md` (addresses, the subtype-13 census, and why
    `Fight for Survival` is a name collision).

    DERIVED: `uv60hgDW` (the area id the predicate keys on) and the mission/dungeon pair, from the shipped
    tables' survival rows (`gQR24kST == 13`); and the event pair `(pvS5A4kE, L9IAY8iW) = (11002, 1)`, which
    our own `ofp9t72b` SeasonEventScheduleMst reply already serves, so the client's event schedule resolves.
    AUTHORED (the schedule master is not shipped): the evergreen window, `L84eYzxv`/`fSxqkF10` (a `0` is a
    legal "no ladder" / "no banner" -- both come back empty, not broken), the disp order, and the gate
    row's neutral reward/condition fields, whose shape is copied from the published gate rows.

    All three served lists are DESTRUCTIVE (`onReadStarted` clears them), so this step always writes the FULL
    set it owns -- one row today. The gate row is APPENDED to whatever `0xkoNP8U` rows the carrier already
    serves, and it rides `SURVIVAL_COND_CARRIERS` only: a carrier that replaced a longer list with ours
    would delete the tiles another carrier served. The break-through list (`in6Fpa2y`) rides
    `SURVIVAL_BREAK_CARRIERS` for the same reason -- it clears on arrival. It is NOT the gate the mode
    needs: `getAreaIdWithBannerTouch`'s FALL-THROUGH path already returns the area id with an empty break
    list, so `Z0EN6jSh = 1` alone is sufficient (note SS10.4b). It is served because a played account has
    such a list, and because its lookup is a two-hop swapped-pair match that is easy to get wrong
    (SS10.4a) -- a row that silently never resolves looks identical on the wire to one that does.
    """
    if not SURVIVAL_ON or State.player is None or reply_id not in SURVIVAL_CARRIERS:
        return spec
    facts = _survival_area_facts()
    if not facts:
        return spec
    spec = _as_spec(spec)
    start, end = SURVIVAL_WINDOW_START, SURVIVAL_WINDOW_END
    events = []
    for area, dungeon, mission, sub_type in facts:
        event = {
            "pvS5A4kE": SURVIVAL_EVENT,     # EventId -- paired with L9IAY8iW by the breakthrough lookups
            "L9IAY8iW": SURVIVAL_PAIR,      # the paired id (a schedule row id in our own `ofp9t72b`)
            "L84eYzxv": SURVIVAL_SUMALLY,   # SumallyId -> SumallyRewardMstList (F_SUMALLY_REWARD_MST)
            "uv60hgDW": area,               # SubRegionId -- THE key `isPlayingSurvivalMission` matches
            "fSxqkF10": SURVIVAL_BANNER,    # RewardBannerId -> BannerMstList
            "VjJQ51uG": start,              # AcceptedDate (same token = startDate in Gacha/Notice)
            "m8ivD4NX": end,                # CompletedDate
            "Z0EN6jSh": SURVIVAL_EVENT_KIND,   # +0x100 -- getAreaIdWithBannerTouch's 1/2 SELECTOR, not an order
            "pAwU2R6W": "0",                # +0x34, no reader located
            "td06MKEX": "0",                # NoticeId (+0xfc), no reader located
            "4yT7FUqj": "",                 # +0xe0 string (banner-art/InternalDesc overload)
            "BGFw7qi4": "",                 # +0x68 \
            "2oL4dNFy": "",                 # +0x80  |
            "uwHx3jk0": "",                 # +0x98  } the five strings with no located reader -- empty so
            "XY5ZfS0w": "",                 # +0xb0  | there is never a raw key for the client to draw
            "0dh7yRIN": "",                 # +0xc8 /
        }
        events.append(event)
    user = {"pvS5A4kE": SURVIVAL_EVENT, "52ibRBqw": "0", "7SPHKY1j": "0", "UbQ26oH4": "0"}
    # The break-through row is reached THROUGH the area, not keyed by it, and the two classes store the
    # shared pair in SWAPPED offsets. `UserBreakThroughProgressInfoList::getObjectWithAreaId` (0x1db8e68):
    #   0x1db8e84  SurvivalEventInfoList::getObjectWithAreaId(area)   <- the event row, by `uv60hgDW`
    #   0x1db8e8c  ldp w8, w9, [x0, #0x24]                           <- the EVENT row's (+0x24,+0x28)
    #   0x1db8ec8  cmp [row+0x28], w8    ; 0x1db8ed4  cmp [row+0x24], w9
    # So this row's `L9IAY8iW` (@+0x24) must equal the EVENT row's `L9IAY8iW` (@+0x28) = SURVIVAL_PAIR --
    # NOT the area. Sourcing it from the area instead is silently inert: the reply is served, the client's
    # list clears and refills, and `getObjectWithAreaId` still returns null, so the row is never found.
    # `IRca7hx9` (@+0x2c) is the FALLBACK path's `w21` gate (> 0) and `hN57axnr` (@+0x40) the early-return
    # gate (!= 0); they only matter once the pair above matches. DESTRUCTIVE (`removeAllObjects` on
    # arrival) -> `SURVIVAL_BREAK_CARRIERS` only.
    brk = {
        "L9IAY8iW": SURVIVAL_PAIR,     # +0x24 -- must equal the EVENT row's +0x28 (the swapped pair)
        "hN57axnr": SURVIVAL_BREAK_GATE,  # +0x40 -- non-zero opens the early return, once the pair matches
        "IRca7hx9": SURVIVAL_BREAK_PROGRESS,  # +0x2c -- the fallback path's `w21` gate (> 0)
        "pvS5A4kE": SURVIVAL_EVENT,      # +0x28 -- must equal the EVENT row's +0x24
        "M43f8qUV": "0", "CMg9aYh2": "0", "ncDi5Hu4": "0", "eGy7QdD1": "0", "Tkeh4Ly5": "0",
        "8QfZ6NIK": "", "m3Wghr1j": "0",
        # +0x64 is CLAMPED before it is stored, so the wire value is not the stored one
        # (0x192b510-0x192b534): `csel w8,w0,5,lt` then `csinc w8,w8,wzr,gt` then `sub #1`, i.e.
        # v < 2 -> -1, 2..4 -> v, v >= 5 -> 5. "0" would store -1, outside the field's own range, so
        # send the minimum in-range value. (Found by the audit round 2026-09-30.)
        "Y58ENcB4": "2",
    }
    parts = set(SURVIVAL_PARTS)
    unknown = parts - {"event", "user", "gate", "break", "state"}
    if unknown:
        _note(f"survival: SURVIVAL_PARTS has unknown part(s) {sorted(unknown)} -- served parts are "
              f"{sorted(parts - unknown)}, the rest are OFF")
        parts -= unknown
    out = dict(spec)
    if "event" in parts:
        out[SURVIVAL_TAG] = events
    if "user" in parts:
        out[SURVIVAL_USER_TAG] = [user]
    brk_on = "break" in parts and reply_id in SURVIVAL_BREAK_CARRIERS
    if brk_on:
        out[SURVIVAL_BREAK_TAG] = [brk]
    # The party state, the one reply APP-8 was actually waiting on. DESTRUCTIVE and UN-GATED, so it is
    # restricted harder than the event list: every arrival clears the client's list unconditionally.
    if "state" in parts and reply_id in SURVIVAL_STATE_CARRIERS:
        out[SURVIVAL_STATE_TAG] = [{SURVIVAL_STATE_TOKEN: _survival_state_block()}]
    gate = "gate" in parts and reply_id in SURVIVAL_COND_CARRIERS
    if gate:
        kept = [r for r in (out.get(SURVIVAL_COND_TAG) or []) if isinstance(r, dict)]
        for area, dungeon, mission, sub_type in facts:
            # The dungeon and mission are the RESOLVED ones for this area, not a global constant: a
            # family has one dungeon per tier and they differ per area, and the only honest source for
            # "this area's first dungeon" is the corpus the client itself reads.
            kept = [r for r in kept if str(r.get("uv60hgDW")) != area]
            kept.append({
                "uv60hgDW": area, "qnUhs32P": SURVIVAL_EVENT_TYPE,
                "U9iHsau3": SURVIVAL_DUNGEON or dungeon, "qo3PECw6": SURVIVAL_MISSION or mission,
                "4yT7FUqj": SURVIVAL_BANNER_ART_BY_AREA.get(area) or SURVIVAL_BANNER_ART,
                "VjJQ51uG": start, "m8ivD4NX": end, "NTXoA6e2": start, "7Gvw6J82": end, "V48um7Jw": end,
                "0iWgxc1X": "0", "B2FW5Qjq": "", "xm7c08IS": "0", "QCLs6SV8": "0", "ET8L6iDw": "0",
                "45wVefcS": "", "MrxFQ63W": "", "5mouF8P1": "", "M1p3ehVU": "", "vnhT2EM5": "",
                "C8Qf5bs6": "0", "z0ft1PK8": "0", "mTHe6pI0": "", "oY0vAuE9": "0",
            })
        out[SURVIVAL_COND_TAG] = kept
    _note(f"survival: {len(facts)} area(s) resolved from the MST corpus -> parts {sorted(parts)}: "
          + ", ".join(f"{a}(dgn {d}, mission {m}, gQR24kST={s})" for a, d, m, s in facts)
          + f"; event {len(events)} row(s) (event {SURVIVAL_EVENT}, pair {SURVIVAL_PAIR}, kind "
            f"{SURVIVAL_EVENT_KIND}, sumally {SURVIVAL_SUMALLY or 'none'}, "
            f"banner {SURVIVAL_BANNER or 'none'})"
          + (f" + break-through row (gate {SURVIVAL_BREAK_GATE}, progress {SURVIVAL_BREAK_PROGRESS})"
             if brk_on else "")
          + (f" + gate row(s) (tab {SURVIVAL_EVENT_TYPE}), {len(out.get(SURVIVAL_COND_TAG) or [])} "
             f"total in the reply" if gate else ""))
    return out


def _survival_state_block() -> str:
    """The `2w9Iyeaq` party-state block: `<ver>@<gauge>(@<pos>|<hp>|<mp>|<lb>|||||)*`.

    GRAMMAR, decoded from the client by reading the packer and the parser (which mirror each other) --
    `SurvivalBattleUnitStateList::createSendInfoEv` 0x1db69c0 packs, `SurvivalBattleUnitStateResponse::
    readParam` 0x192a84c -> `setUnitStateInfo` 0x1db7694 parses. Separators are NAMED in .dynsym, not
    guessed: `SEND_DATA_SEPARATOR_BLOCK` = `@`, `SEND_DATA_SEPARATOR_VALUE` = `|`, and the bad-state list
    splits on `,`. NINE unit fields (EIGHT pipes).

    THE THREE REQUIREMENTS, all of which fail silently:
      * the value must not be empty (`strlen`==0 -> readParam stores nothing at all);
      * it needs at least TWO `@`-blocks (`cmp w8,#1 ; b.lt`), so an empty party is `1@0` and still
        valid -- one block would leave the summon gauge at its default;
      * **`<pos>` must equal the unit's live `BattleUnit::getPartyPosition()`.** readParam files each
        state in a dictionary keyed on `parts[0]`, and `SurvivalPlayerParty::entry()` (0x1ae50cc) looks
        it up by `getPartyPosition()` and `cbz`-skips on a miss. So `pos` is the SLOT INDEX, which is
        what `party_instance_ids()` enumerates in -- and a wrong value restores nothing, silently.

    The four CSV fields are served EMPTY: the parser assigns them opaquely, and empty is what the client
    itself emits for a unit with no limit bullets / skill intervals / brave shift / buffs. `StrToInt` is
    called UNGUARDED on fields 0-3, so those are plain decimals.
    """
    parts = [str(SURVIVAL_STATE_VERSION), str(SURVIVAL_SUMMON_GAUGE)]
    for pos, iid in enumerate(State.player.party_instance_ids()):
        row = State.player.roster_row(iid) or {}
        stats = unit_stats(row.get("master_id"), int(row.get("level") or 1))
        # pos | hp | mp | lb | badStates | limitBulletCsv | skillIntervalCsv | braveShiftCsv | buffCsv
        parts.append("|".join([str(pos), str(stats[0] or 0), str(stats[1] or 0),
                               str(row.get("lb_level") or 0), "", "", "", "", ""]))
    return "@".join(parts)


def player_progress(quest_id: str, meta: dict) -> int:
    """The stored progress for a shipped quest row, 0 when the row is not ours to score.

    One reader for every caller (the serve path, the claim path and the projection), because a second reader
    is a second formula and the `ZLuw4DLa` duplication in this family already cost a run.
    """
    if not meta:
        return 0
    return State.player.progress_value(_progress_key(meta["family"], meta["schedule"], quest_id))


def claim_sp_challenge(spec, reply_id, plaintext=None):
    """Answer `SpChallengeRewardGetRequest` (`2G7ZVs4A`) -- the SP-challenge CLAIM.

    Same three rules as the panel-quest claim, because they are the three states: `SP_CLEAR` is *is-cleared*,
    `SP_REWARD_FLAG` is *is-received*, and a claim is neither. So: read the claim triples, REFUSE anything
    that is not cleared, pay the quest's own shipped `dX6cor8j`, record `claimed/<group>/<quest>` capped at 1
    (so a second tap is a no-op across restarts), and answer with the FULL refreshed row set plus a
    user-state group -- see the two notes at the assignment below for why "just the claimed row" is wrong.

    **Which field of the triple is the challenge id** is read from the client, not guessed.
    `SpChallengeRewardGetRequest::createBody` (0x199e10c) writes `fK3W9qYA`, `zF6QIY2r`, `V36ygRYs` from the
    three ints `SpChallengeListObject::onButtonTouched` (0x253e308) passed to `addChallengeInfo`, and those
    are `mst->getChallengeId()`, `mst->getRank()` and `mst->getChallengeId()` again -- so the key NAMES are
    positional reuse and `fK3W9qYA` carries a CHALLENGE id here, not a schedule id. With 0 captures of this
    request the safe implementation is structural rather than positional: accept the triple, take whichever
    field is a SHIPPED challenge id, and refuse anything else. An unrecognised triple is reported, never
    guessed at (RE_VERIFICATION_IDIOMS #26).
    """
    if reply_id != SP_CHALLENGE_CLAIM_REQUEST or State.player is None:
        return spec
    spec = _as_spec(spec)
    player = State.player
    nodes = _request_obj(plaintext).get(SP_CHALLENGE_CLAIM_GROUP) if plaintext else None
    if not isinstance(nodes, list) or not nodes:
        _note(f"sp challenge claim: no {SP_CHALLENGE_CLAIM_GROUP} triples in the request -- nothing claimed")
        return spec
    index = _quest_index()
    out, paid, refused = [], [], []
    for node in nodes:
        if not isinstance(node, dict):
            refused.append("a claim node that is not an object")
            continue
        vals = {str(v) for v in node.values() if str(v or "").strip()}
        known = [q for q in vals if q in index]
        if len(known) != 1:
            refused.append(f"{sorted(vals)}: not exactly one shipped quest id in the triple")
            continue
        qid = known[0]
        meta = index[qid]
        gid = meta["schedule"]
        done = player_progress(qid, meta)
        if not (meta["max"] and done >= meta["max"]):
            refused.append(f"{qid}: not cleared ({done}/{meta['max']})")
            continue
        sched_id = sp_schedule_id(gid)
        ckey = _progress_key("claimed", gid, qid)
        if player.progress_value(ckey):
            refused.append(f"{qid}: already claimed -- not paid again")
            out.append({SP_SCHEDULE_ID: sched_id, SP_GROUP_ID: gid, SP_CHALLENGE_ID: qid,
                        SP_PROGRESS: str(done), SP_CLEAR: "1", SP_REWARD_FLAG: "1"})
            continue
        reward = _quest_reward(qid)
        credited = player._grant_items(reward)
        if not reward or len(credited) != len(reward):
            # Never report a payment that did not happen: the count is the CREDITED rows, not the parsed
            # ones, so a routing or shape error shows up as a shortfall instead of a silent success.
            refused.append(f"{qid}: {len(credited)}/{len(reward)} reward row(s) creditable")
            continue
        player.bump_progress(ckey, 1, cap=1, what=f"sp claim {qid} ({gid})")
        paid.append(f"{qid}: " + ", ".join(f"{r['kind']}:{r['id']}x{r['count']}" for r in credited))
        out.append({SP_SCHEDULE_ID: sched_id, SP_GROUP_ID: gid, SP_CHALLENGE_ID: qid,
                    SP_PROGRESS: str(done), SP_CLEAR: "1", SP_REWARD_FLAG: "1"})
    # TWO things this reply must carry, and neither is "the claimed row alone".
    #
    # (1) the FULL `3KLU4mSD` row set. The group clears the client's shared `UserSpChallengeInfoList` on
    # arrival (`onReadStarted` 0x1923d4c -> `removeAllObjects`; the one flag that would skip it, `+0x24`, is
    # set by no wire token), and the display list is MST-driven with a per-row lookup
    # (`SpChallengeMstList::getDispListFromGroupId` 0x1cb6244 walks the shipped rows and calls
    # `getInfoFromChallengeId` on each) -- so a missing row is not a HIDDEN quest, it is a quest in the
    # NOT-CLEARED bucket, because a null user row is what "not clear" looks like. A one-row reply therefore
    # silently un-cleared the other 48 (audit 2026-09-27). The claimed row is already in the full set with
    # `QCLs6SV8 = 1`, so this is a rebuild, not a merge.
    claimed_ids = {str(c.get(SP_CHALLENGE_ID, "")) for c in (out or [])}
    picked = sp_challenge_picked()
    if picked:
        infos, _ = sp_challenge_info_rows(picked)
        spec[SP_CHALLENGE_INFO_TAG] = infos
    elif out:
        spec[SP_CHALLENGE_INFO_TAG] = out
    # (2) a USER-STATE group, so `apply_to_reply` runs and the reward's count is fresh in the SAME reply.
    # `2G7ZVs4A` has no `responses.json` entry, so the spec arrives as None; without this the claim's reply
    # carried no `Md0N5abE` and a ticket reward only appeared on the NEXT carrier (audit 2026-09-27). The
    # panel claim has the identical gap.
    spec.setdefault("LhVz6aD2", [{}])
    served = spec.get(SP_CHALLENGE_INFO_TAG) or []
    _note("sp challenge claim: " + (f"paid {len(paid)} ({'; '.join(paid)})" if paid else "nothing paid")
          + (f"; REFUSED {len(refused)} ({'; '.join(refused)})" if refused else "")
          + f"; {len(served)} refreshed row(s) returned"
          + (f" ({len(claimed_ids)} just claimed)" if claimed_ids else ""))
    return spec

# --- the EMBLEM family (status HUD's `header_emblem_icon` -> PlayerEmblemSelectScene) -----------------
# WHY THIS EXISTS. `PlayerEmblemEntryRequest` (`Z7J9H6TK`) had NO reply, and an empty body is not a neutral
# answer: `GameScene::checkResponseMessage` (0x1e7cfc8) returns false iff `MessageState::exist()` is false,
# so the scene raised "A connection error has occurred." and its OK button re-fired the request -- measured
# live 2026-09-30 (reply `body_plain_len: 0`, from_key None). The rule that generalises: **a reply must
# produce at least one dispatchable group**, whatever the request.
#
# The LIST is not ours to serve: `EmblemMstResponse` exists as a class but has no factory tag, so the 522
# emblems come from the shipped `F_EMBLEM_ITEM_MST` alone (measured on screen: "1 / 522"). Only the player's
# OWN state is a reply, and it is two different facts -- see `player_state`'s emblem block.


def serve_emblem_info(spec, reply_id):
    """`Z7J9H6TK` -> the `3Ys8WPS6` owned-emblems group, as a projection of player state.

    A projection, not a literal, for the same reason the party groups are: the OWNED set is what
    `UserPlayerEmblemInfoList::hasEmblem` tests (`PlayerEmblemSelectListObject::setLayout` 0x231ae64), so a
    hand-written list would go stale the moment a trophy granted an emblem. An account that owns nothing
    still gets the group -- an absent group is the connection error, and `updateEmblemList` returns early
    on `count < 1` rather than faulting, so the empty list is a legitimate "you own none".
    """
    if reply_id != EMBLEM_ENTRY_REQUEST or State.player is None:
        return spec
    if isinstance(spec, dict) and spec.get(EMBLEM_GROUP):
        return spec                            # an explicit payload wins; do not overwrite a curated body
    out = dict(spec) if isinstance(spec, dict) else {}
    out[EMBLEM_GROUP] = State.player.emblem_rows()
    return out


def apply_emblem_setting(plaintext, spec):
    """`19YwqU2T` -> record the DISPLAYED emblem, and answer with the refreshed owned set.

    The request carries `3Ys8WPS6` = `{"9Tbns0eI": <user id>, "0w6XzofV": <emblem id>}` -- the SAME group
    the reply uses, so this is one tag read in both directions. Two rules keep it honest, and both are
    refusals rather than silent writes:
      * the user id must be ours (`apply_party_edit`'s rule; a mismatched body is not this player);
      * the emblem must be OWNED, or the `0` "no emblem" sentinel -- `set_player_emblem` refuses anything
        else, so no request can put a crest on the status HUD that no `F_EMBLEM_ITEM_MST` row backs.
    The reply carries `3oU9Ktb7` as a PLACEHOLDER on purpose: it is one of `USER_STATE_GROUPS`, so
    `apply_to_reply` replaces it with the real projection, which is how `n69quiCJ` reaches the header
    (`checkConnectResult` ends in `UIHeader::updateEmblem()`).
    """
    if State.player is None or not plaintext:
        return spec
    try:
        req = json.JSONDecoder().raw_decode(plaintext)[0]
    except Exception:  # noqa: BLE001 - a body that will not parse must never break the reply
        _note("apply_emblem_setting: request body did not parse as JSON; setting dropped")
        return spec
    rows = req.get(EMBLEM_GROUP) if isinstance(req, dict) else None
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return spec
    row = rows[0]

    def _answer():
        """The well-formed answer, whether or not the write was accepted.

        A REFUSAL must not answer with nothing. An empty body is the shape this whole note exists for:
        `MessageState::exist()` false -> `checkResponseMessage` false -> "A connection error has occurred."
        So a refused set (an unowned emblem, a foreign user id) re-sends the truth -- the owned list and
        the team row -- and the client simply re-renders the crest it already had. The write is still
        refused; only the *shape* of the refusal changes. This was caught by the regression golden, which
        pinned `19YwqU2T` at `len 0` on a fresh state: the first version returned the untouched spec on a
        refusal, i.e. exactly the connection error, reachable by any request naming an emblem nobody owns.
        """
        out = dict(spec) if isinstance(spec, dict) else {}
        out[EMBLEM_GROUP] = State.player.emblem_rows()
        out[TEAM_INFO_TAG] = []                # placeholder -> filled by apply_to_reply's projection
        return out

    want_user = State.player.data["identity"]["user_id"]
    if str(row.get(EMBLEM_USER_ID, want_user)) != str(want_user):
        _note(f"apply_emblem_setting: REFUSED -- body names user {row.get(EMBLEM_USER_ID)!r}, "
              f"state is {want_user!r}")
        return _answer()
    if EMBLEM_ID not in row:
        return spec
    if not State.player.set_player_emblem(row[EMBLEM_ID]):
        _note(f"apply_emblem_setting: REFUSED emblem {row[EMBLEM_ID]!r} -- not owned "
              f"(owned: {State.player.owned_emblem_ids() or 'none'})")
        return _answer()
    return _answer()



# --- the EMBLEM EVENT (`sgEmblemEventScene`): the crest campaign the status-HUD crest opens -------------
# The shipped tables and the model live in `player_state` (`emblem_weekly_mst` / `emblem_monthly_mst` /
# `emblem_objectives`); what is here is the WIRE: the three groups the screen reads, the objective credit a
# mission end feeds, and the tier mail. One home for the evidence: docs/notes/emblem_event.md.


def _emblem_window(now: float) -> tuple:
    """The campaign window, as the two `convertToTime` strings the client brackets NOW with.

    Authored: no shipped table dates an emblem campaign, so the window is the current UTC calendar month.
    What matters is that it CONTAINS now -- the client's own constraint is `StartDate <= now <= EndDate` --
    and the FORMAT is not a guess: the class's two readers both `bl CommonUtils::convertToTime`
    (0x1946bd0 / 0x1946be8), the same call the proven panel-quest schedule feeds
    `PANEL_QUEST_WINDOW_FORMAT`.
    """
    import calendar
    t = time.gmtime(now)
    start = calendar.timegm((t.tm_year, t.tm_mon, 1, 0, 0, 0, 0, 0, 0))
    ny, nm = (t.tm_year + 1, 1) if t.tm_mon == 12 else (t.tm_year, t.tm_mon + 1)
    end = calendar.timegm((ny, nm, 1, 0, 0, 0, 0, 0, 0)) - 1
    return (time.strftime(PANEL_QUEST_WINDOW_FORMAT, time.gmtime(start)),
            time.strftime(PANEL_QUEST_WINDOW_FORMAT, time.gmtime(end)))


def deliver_emblem_tier_mail() -> list:
    """Mail every earned-but-unpaid crest tier. Idempotent per count, so a caller needs no guard.

    The mailbox is state, so any reply may seed it -- `serve_emblem_event` does, which is how the crest-0
    opening gift arrives the first time the screen is opened. One mail per crest count, `icon_type` 2 (the
    treasure box, per `deliver_mail`'s own token note). The id is NUMERIC on purpose: `ynP26rjD` parses
    client-side as a u64 and a non-numeric id can never match a receipt (the daily-login mail's measured
    lesson, 2026-09-22). Returns the ids it added.
    """
    if State.player is None:
        return []
    added = []
    for tier in State.player.emblem_pending_tiers():
        items = []
        for spec in list(tier["free"]) + list(tier["pass"]):
            items.extend(_parse_reward(spec))
        if items:
            mid = str(EMBLEM_MAIL_ID_BASE + int(tier["crests"]))
            State.player.deliver_mail(EMBLEM_TIER_MAIL_TITLE, items, mail_id=mid, icon_type=2)
            added.append(mid)
        elif tier["free"] or tier["pass"]:
            # A tier whose specs no mail row can express is marked PAID rather than re-examined forever --
            # but it is said out loud, because silently dropping a reward is the failure this note prevents.
            _note(f"emblem event: tier {tier['crests']} has reward spec(s) the mailbox cannot express "
                  f"({(tier['free'] + tier['pass'])!r}); nothing mailed for it")
        State.player.mark_emblem_tiers_mailed([tier["crests"]])
    return added


def serve_emblem_event(spec, reply_id):
    """`wMWvnYDR` -> the LIVE campaign: three groups in one body.

    The client's response factory dispatches by TAG (`harness/response_tag_map.json` maps each to its
    class), so ONE reply serves the monthly campaign row, the per-milestone progress and the milestone
    windows -- which is why the capture corpus shows nothing sending those three classes separately: this
    request sends all of them. An explicit group already in the payload wins, like `serve_emblem_info`'s
    rule, so a curated body can still override one arm.

    It also DELIVERS mail (the crest-0 opening tier): the mailbox is state, and this is the request the
    campaign's own screen sends, so opening the screen is what seeds it -- the same shape as
    `deliver_login_mail`, which delivers on the daily-login request.
    """
    if reply_id != EMBLEM_EVENT_REQUEST or State.player is None:
        return spec
    out = _as_spec(spec)
    objectives = State.player.emblem_objectives()
    start, end = _emblem_window(time.time())
    window = {EMBLEM_START_DATE: start, EMBLEM_END_DATE: end}
    deliver_emblem_tier_mail()
    if not out.get(EMBLEM_MONTHLY_GROUP):
        out[EMBLEM_MONTHLY_GROUP] = [{
            EMBLEM_CAMPAIGN_TOKEN: State.player.emblem_event()["campaign"],
            EMBLEM_TOKEN_COUNT: str(State.player.emblem_tokens()),
            # Authored, and named as such: no shipped table carries a bundle id, a banner image or a
            # display order for an emblem campaign (the two tables hold milestones and rewards only).
            EMBLEM_BUNDLE_ID: "0",
            EMBLEM_PASS_STATUS: "1" if (os.environ.get("EMBLEM_PASS") or "").strip() not in ("", "0") else "0",
            EMBLEM_BANNER_IMAGE: "",
            EMBLEM_PERSISTENCE: "0",
            EMBLEM_DISPLAY_ORDER: "1",
            **window}]
    if not out.get(EMBLEM_WEEKLY_GROUP):
        out[EMBLEM_WEEKLY_GROUP] = [{EMBLEM_MILESTONE_TOKEN: o["id"],
                                     EMBLEM_WEEKLY_PROGRESS: str(o["progress"]),
                                     EMBLEM_WEEKLY_CLEARED: "1" if o["cleared"] else "0"}
                                    for o in objectives]
    if not out.get(EMBLEM_MILESTONE_GROUP):
        out[EMBLEM_MILESTONE_GROUP] = [{EMBLEM_MILESTONE_TOKEN: o["id"], **window} for o in objectives]
    return out


def credit_emblem_objectives(plaintext, spec):
    """A `MissionEndRequest` -> the campaign's objectives (a state write; the pipeline step calls this).

    **A TRANSFORM like every other step, so it returns the spec** -- and this is not a formality: the
    pipeline assigns `ctx.spec = step.call(ctx)`, so the FIRST version of this function, which returned
    `None` on its early paths, blanked **97 of the 109** golden cases. The replay caught it in one run,
    which is the whole argument for `harness/server_regression.py`: a step that forgets its return value
    is indistinguishable from a correct one until a replay compares bytes.

    The mission id rides `jQsE54Iz.qo3PECw6` -- `PROGRESS_MISSION_GROUP`/`TOKEN`, the same pair the mission
    machinery already reads. A newly cleared objective grants a crest, and a crest can cross a tier, so the
    delivery happens here rather than waiting for a screen the player may never open.
    """
    if State.player is None or not plaintext:
        return spec
    try:
        req = json.JSONDecoder().raw_decode(plaintext)[0]
    except Exception:  # noqa: BLE001 - a body that will not parse must never break the reply
        _note("credit_emblem_objectives: request body did not parse; objectives NOT credited")
        return spec
    rows = req.get(PROGRESS_MISSION_GROUP) if isinstance(req, dict) else None
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return spec
    mid = str(rows[0].get(PROGRESS_MISSION_TOKEN) or "")
    if mid and State.player.credit_emblem_objective(mid):
        deliver_emblem_tier_mail()
    return spec


def _carry_reply(spec):
    """The well-formed answer for the item-set edit, whether or not the write was accepted.

    A refusal must never answer with NOTHING: an empty body means `MessageState::exist()` false →
    `checkResponseMessage` false → *"A connection error has occurred."* (the lesson `apply_emblem_setting`
    records). So a body that will not parse still gets this answer — the echo of the list we hold.
    """
    out = dict(spec) if isinstance(spec, dict) else {}
    out[CARRY_ITEM_GROUP] = State.player.carry_item_rows()
    return out


def apply_carry_edit(plaintext, spec, reply_id=None):
    """`UM7hA0Zd` (`ItemCarryEditRequest`) → record the player's ITEM SET and echo the refreshed list.

    **Gated on the requestID, and that gate is load-bearing**: this group is CLEAR-AND-REBUILD on the client
    (`onReadStarted`), so emitting it on any other reply would wipe the item set on every request. The golden
    gate caught exactly that on this step's first run — fired unconditionally, it grew NINE unrelated replies
    by 32 bytes each (the empty `nE7pV2wr` row). A transform that writes one group too many is
    indistinguishable from a correct one until a replay compares bytes, which is what
    `harness/server_regression.py` is for.

    The body's `nE7pV2wr` group is the client's own serialisation of `UserCarryItemInfoList` —
    `dispOrder:itemId:itemNum` per element, comma-joined — the SAME tag and grammar the reply uses, so this
    is one tag read in both directions. The sender scenes read NOTHING back (audited 2026-10-07: every one of
    the eight senders only constructs the request), so the reply exists to be a well-formed ack and to
    refresh the group the Item Set screen draws.

    **The echo is COMPLETE by construction** (`carry_item_rows`), which is the one thing that matters here.

    Deliberately NOT a `LhVz6aD2` (user-state) reply. That would drag the 12-group projection into a reply
    whose consumer reads nothing — and `apply_to_reply`'s own docstring records what an unexpected
    clear-and-rebuild group does to pending client state (`UnitEquipRequest` / `5Eb0Rig6`).
    """
    if str(reply_id) != CARRY_EDIT_REQUEST or State.player is None or not plaintext:
        return spec
    try:
        req = json.JSONDecoder().raw_decode(plaintext)[0]
    except Exception:  # noqa: BLE001 - a body that will not parse must never break the reply
        _note("apply_carry_edit: request body did not parse as JSON; item set left unchanged")
        return _carry_reply(spec)
    rows = req.get(CARRY_ITEM_GROUP) if isinstance(req, dict) else None
    entries = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        csv = row.get(CARRY_ITEM_CSV)
        if not isinstance(csv, str):
            continue
        for element in csv.split(","):
            parts = element.strip().split(":")
            if len(parts) != 3:
                continue                       # the client's own guard: exactly 3 ints, else the element is skipped
            try:
                entries.append({"disp_order": int(parts[0]), "item_id": int(parts[1]),
                                "count": int(parts[2])})
            except ValueError:
                continue
    stored = State.player.set_carry_items(entries)
    # Coherence, logged rather than enforced: an id with no `F_ITEM_MST` row draws nameless, and a count the
    # bag cannot cover is a slot the client could not have filled legitimately. Both mean the client sent
    # something our state does not back -- worth a line in the log, not worth breaking the screen over.
    for row in stored:
        if item_mst(row["item_id"]) is None:
            _note(f"apply_carry_edit: slot {row['disp_order']} names item {row['item_id']}, "
                  f"which is not an F_ITEM_MST id")
        elif State.player.item_count(BAG_ITEM_TYPE, row["item_id"]) < row["count"]:
            _note(f"apply_carry_edit: slot {row['disp_order']} carries {row['count']} x {row['item_id']}, "
                  f"but the bag holds {State.player.item_count(BAG_ITEM_TYPE, row['item_id'])}")
    joined = ", ".join(f"{r['disp_order']}:{r['item_id']}x{r['count']}" for r in stored) or "empty"
    _note(f"apply_carry_edit: item set now {len(stored)} slot(s): {joined}")
    return _carry_reply(spec)


def _state_snapshot() -> dict:
    """The in-memory v2 player state. The FILE is written by `PlayerState.save` (player_state.py); this
    accessor exists so a caller can read the persisted shape without reaching through `State.player`."""
    return State.player.data


# --- the selftest ------------------------------------------------------------------------------------
# The one behaviour here that can pay a player for something they did not do is the quest JOIN, so it gets
# its own gate. Every case is a real shipped row, and the permissive direction is the one under test: a join
# that matched a `15:<missionId>` spec against ANY clear would look exactly like a working feature on the
# screen and would be wrong in the only direction that costs something.
def _selftest() -> int:
    import sys as _sys

    import player_state as ps2

    rc = 0
    index = _quest_index()
    checks = []

    def want(label, got, expect):
        nonlocal rc
        ok = got == expect
        checks.append((label, ok, got, expect))
        rc += 0 if ok else 1

    # the index really does carry the parameter, and the three shapes are distinguished
    want("110101's spec is parsed into code+param",
         (index["110101"]["cond"], index["110101"]["param"]), ("15", "8991106"))
    want("a wildcard spec keeps '*' as the param", index["301910"]["param"], "*")
    want("a bare-code spec has no param", index["100101"]["cond"], "11")

    # the parameter-aware join, on the real rows
    gil = index["110101"]
    other = index["110102"]
    wild = index["301910"]
    bare = index["100101"]
    # the ANY-OF list: three shipped quests put fourteen mission ids in the parameter, so equality against
    # the whole tail left them pinned at 0 on EVERY clear. Found by the 2026-09-27 audit, which is also why
    # these cases exist -- the gate had no case that could have caught it.
    lst = index["500201"]
    want("the multi-mission spec keeps its whole list", lst["param"].count("@"), 13)
    want("the first mission of the list matches", _quest_matches("mission_clear", lst, "8210107"), True)
    want("the LAST mission of the list matches (a prefix bug would miss it)",
         _quest_matches("mission_clear", lst, "8210120"), True)
    want("a mission OUTSIDE the list does not match", _quest_matches("mission_clear", lst, "8210106"), False)
    want("the whole tail as one string does not match",
         _quest_matches("mission_clear", lst, "8210107@8210108@8210109@8210110@8210111@8210112@8210113"
                                            "@8210114@8210115@8210116@8210117@8210118@8210119@8210120"), False)
    want("the named mission matches its own clear", _quest_matches("mission_clear", gil, "8991106"), True)
    want("a DIFFERENT mission does not match the named spec",
         _quest_matches("mission_clear", gil, "1110100"), False)
    want("no id at all does not match the named spec (fail closed)",
         _quest_matches("mission_clear", gil, None), False)
    want("the wildcard matches any clear", _quest_matches("mission_clear", wild, "1110100"), True)
    want("the wildcard matches even with no id", _quest_matches("mission_clear", wild, None), True)
    want("a bare-code spec matches unconditionally", _quest_matches("equip", bare, None), True)
    want("a named spec still refuses a no-id clear for another code's shape",
         _quest_matches("mission_clear", other, "8993903"), True)

    # and the end-to-end shape: a real mission end moves exactly the rows it should
    State.player = ps2.PlayerState.load("/nonexistent", slot="selftest-quest")
    body = json.dumps({"jQsE54Iz": [{"qo3PECw6": "8991106"}]})
    st.NOTES.clear()
    apply_progress(None, "x5Unqg2d", body)
    p = State.player
    want("a clear of 8991106 ticks 110101", p.progress_value("spchallenge/1101/110101"), 1)
    want("...and 110102 (15:8993903) stays at zero", p.progress_value("spchallenge/1101/110102"), 0)
    want("...and a wildcard quest advances by one", p.progress_value("spchallenge/3019/301910"), 1)
    st.NOTES.clear()
    apply_progress(None, "x5Unqg2d", json.dumps({"jQsE54Iz": [{"qo3PECw6": "8993903"}]}))
    want("a clear of 8993903 ticks 110102", p.progress_value("spchallenge/1101/110102"), 1)
    want("...and advances the wildcard a second time", p.progress_value("spchallenge/3019/301910"), 2)
    st.NOTES.clear()
    apply_progress(None, "x5Unqg2d", json.dumps({"jQsE54Iz": [{"qo3PECw6": "8991106"}]}))
    want("a REPEAT clear of a one-shot does not move it", p.progress_value("spchallenge/1101/110101"), 1)
    st.NOTES.clear()
    apply_progress(None, "x5Unqg2d", "{not json")
    want("an unparseable body moves no NAMED quest (the wildcards still credit, which is correct -- the "
         "request itself asserts that a quest ended)",
         p.progress_value("spchallenge/1101/110101"), 1)
    # the TFA twin. The server SERVES two mission-end requests and both bodies carry `jQsE54Iz.qo3PECw6`;
    # keying the action on one of them granted the clear's UNLOCK and paid no quest progress, silently
    # (audit 2026-09-27). Both must score, and a body the other already scored must not score twice.
    tfa = ps2.PlayerState.load("/nonexistent", slot="selftest-tfa")
    State.player = tfa
    tfa_body = json.dumps({"jQsE54Iz": [{"qo3PECw6": "8991106"}]})
    st.NOTES.clear()
    apply_progress(None, "JiglLZx6", tfa_body)
    want("a TFA mission end scores the same as a normal one",
         tfa.progress_value("spchallenge/1101/110101"), 1)
    want("...and it names the quests it moved", any("mission_clear moved" in n for n in st.NOTES), True)
    st.NOTES.clear()
    apply_progress(None, "x5Unqg2d", tfa_body)
    want("a second end of the same mission is a repeat, not a re-score",
         tfa.progress_value("spchallenge/1101/110101"), 1)
    State.player = p

    # THE CLAIM'S THREE RULES, driven through the real chain (audit 2026-09-27: `2G7ZVs4A` has 0 captures,
    # so no replay gate executes this code -- deleting the "must be cleared" rule left the golden, the
    # sequence replay, the selftest and the state-writer audit ALL green while the chain paid an uncleared
    # quest. These three cases are the gate that now fails if it happens again.)
    def _claim(qid, rank=1):
        return json.dumps({"Dtw7GU1j": [{"fK3W9qYA": qid, "zF6QIY2r": str(rank), "V36ygRYs": qid}]})

    cp = ps2.PlayerState.load("/nonexistent", slot="selftest-claim")
    State.player = cp
    before_items = json.dumps(cp.data.get("inventory", {}), sort_keys=True)
    before_roster = len(cp.data["roster"])
    st.NOTES.clear()
    sp_out = claim_sp_challenge(None, SP_CHALLENGE_CLAIM_REQUEST, _claim("110101"))
    sp_note = " ".join(st.NOTES)
    want("an UNCLEARED quest is refused: nothing claimed",
         cp.progress_value("claimed/1101/110101"), 0)
    want("...and it says so in the log", "not cleared" in sp_note, True)
    want("...and pays nothing", json.dumps(cp.data.get("inventory", {}), sort_keys=True) == before_items
         and len(cp.data["roster"]) == before_roster, True)
    st.NOTES.clear()
    cp.bump_progress("spchallenge/1101/110101", 1, cap=1)
    sp_out = claim_sp_challenge(None, SP_CHALLENGE_CLAIM_REQUEST, _claim("110101"))
    sp_note = " ".join(st.NOTES)
    want("a CLEARED quest is paid", cp.progress_value("claimed/1101/110101"), 1)
    want("...and the log names the reward", "paid 1 (110101: 23:50x1)" in sp_note, True)
    want("...and the reply's own row says received",
         [r.get(SP_REWARD_FLAG) for r in (sp_out.get(SP_CHALLENGE_INFO_TAG) or [])
          if r.get(SP_CHALLENGE_ID) == "110101"], ["1"])
    want("...and the reply carries the FULL row set, not just the claimed one (the group clears the "
         "client's list on arrival)", len(sp_out.get(SP_CHALLENGE_INFO_TAG) or []) > 1, True)
    st.NOTES.clear()
    claim_sp_challenge(None, SP_CHALLENGE_CLAIM_REQUEST, _claim("110101"))
    want("a second claim pays nothing", cp.progress_value("claimed/1101/110101"), 1)
    want("...and says so", "already claimed" in " ".join(st.NOTES), True)
    # a reward whose kind no reply group can express must be REFUSED, not logged as paid -- this is what
    # made `paid 1 (100102: 10:900020301x10)` a lie before (kind 10 is a UNIT and went to an invisible bag
    # row; the client's readers are the roster and the type-20/23 bags).
    st.NOTES.clear()
    mp = ps2.PlayerState.load("/nonexistent", slot="selftest-grant")
    State.player = mp
    mp.bump_progress("spchallenge/1001/9000001", 1, cap=1)      # pays 60:1000000125 (a recipe book)
    # the grant-level refusal is a `print` (player_state has no NOTES channel), so capture stdout for it
    import contextlib as _cx
    import io as _io
    _buf = _io.StringIO()
    with _cx.redirect_stdout(_buf):
        claim_sp_challenge(None, SP_CHALLENGE_CLAIM_REQUEST, _claim("9000001", 2))
    want("a reward no reply group projects is refused, not paid",
         mp.progress_value("claimed/1001/9000001"), 0)
    want("...and the grant logs WHY, naming the kind",
         "no reply group projects this kind" in _buf.getvalue(), True)
    want("...and the claim's own note records the shortfall",
         "0/1 reward row(s) creditable" in " ".join(st.NOTES), True)
    mp.bump_progress("spchallenge/1001/9000000", 1, cap=1)      # pays 10:213000105 (a UNIT)
    n_roster = len(mp.data["roster"])
    st.NOTES.clear()
    claim_sp_challenge(None, SP_CHALLENGE_CLAIM_REQUEST, _claim("9000000"))
    want("a UNIT reward lands in the ROSTER, which is what the client reads",
         len(mp.data["roster"]) - n_roster, 1)
    want("...and it is not banked in the bag as an invisible item_type 10",
         [i for i in mp.data["inventory"].get("items", []) if str(i.get("item_id")) == "213000105"], [])
    State.player = p

    # the schedule-name rule: the id the client turns into a text key must be one the table carries
    tables = sp_challenge_tables()
    for gid in ("1001", "1101"):
        sched_id = sp_schedule_id(gid)
        want(f"group {gid}'s schedule id {sched_id} has a shipped name key",
             SP_NAME_KEY_PREFIX + sched_id in tables["names"], True)
    want("group 9001 (the nameless hub) is reported as having no schedule name key",
         tables["schedule_names"].get("9001"), None)
    # ...and the served rows are all NAMED, which is the check that caught the display-order defect's twin
    unnamed = [q for rows in tables["groups"].values() for q in rows
               if gid_ok(tables, q) and f"MST_SPCHALLENGE_NAME_{q.get(SP_CHALLENGE_ID)}" not in tables["names"]]
    want("every served challenge row has a shipped name row", unnamed, [])

    # the category, read at the address, decides the tab
    want("authored flags -> category 1", sp_schedule_category("0", "0", "1"), 1)
    want("a daily schedule -> category 0", sp_schedule_category("0", "1", "1"), 0)
    want("dispType 3 wins over everything", sp_schedule_category("3", "0", "2"), 3)
    want("persistent != 1 -> category 2", sp_schedule_category("0", "0", "0"), 2)

    # --- SURVIVAL: the step must be OFF by default, a no-op off its carriers, and when ON it must keep
    # every gate row it did not author (that list is replace-on-read, so a partial list is a deletion).
    g = globals()
    was_on = SURVIVAL_ON
    was_area = SURVIVAL_AREA
    was_parts = SURVIVAL_PARTS
    was_kind = g["SURVIVAL_EVENT_KIND"]
    was_gate = g["SURVIVAL_BREAK_GATE"]
    was_brk_carriers = g["SURVIVAL_BREAK_CARRIERS"]
    was_state_carriers = g["SURVIVAL_STATE_CARRIERS"]
    was_areas = g["SURVIVAL_AREAS"]
    # PIN the area list for this block rather than inheriting the shipped default. The default is the
    # family (see ffbe_config), and six cases here assert single-area specifics -- so leaving them to
    # inherit it means every change to what we serve by default breaks tests that never meant to care.
    g["SURVIVAL_AREAS"] = ("1006",)
    base = {"LhVz6aD2": [{"6Nf5risL": "0"}], SURVIVAL_COND_TAG: [{"uv60hgDW": "226"}, {"uv60hgDW": "899"}]}
    try:
        g["SURVIVAL_ON"] = False
        want("survival: OFF leaves a carrier's spec object untouched",
             serve_survival(base, "lEHBdOEf") is base, True)
        g["SURVIVAL_ON"] = True
        want("survival: a non-carrier reply is untouched", serve_survival(base, "x5Unqg2d") is base, True)
        out = serve_survival(base, "lEHBdOEf")
        want("survival: the event list lands on a carrier", len(out.get(SURVIVAL_TAG) or []), 1)
        want("survival: every token the class reads is served", len((out.get(SURVIVAL_TAG) or [{}])[0]), 16)
        want("survival: SubRegionId is the area the predicate keys on",
             (out.get(SURVIVAL_TAG) or [{}])[0].get("uv60hgDW"), SURVIVAL_AREA)
        want("survival: the user-info row rides the same reply", len(out.get(SURVIVAL_USER_TAG) or []), 1)
        want("survival: ...with its own four tokens", len((out.get(SURVIVAL_USER_TAG) or [{}])[0]), 4)
        want("survival: the two existing gate rows survive",
             [r["uv60hgDW"] for r in out.get(SURVIVAL_COND_TAG) or []], ["226", "899", SURVIVAL_AREA])
        again = serve_survival(out, "lEHBdOEf")
        want("survival: re-serving replaces its own gate row instead of duplicating it",
             len(again.get(SURVIVAL_COND_TAG) or []), 3)
        fresh = serve_survival(None, "MTf2j9aK")
        want("survival: a None spec still gets a dict (idiom 41)",
             isinstance(fresh, dict) and SURVIVAL_TAG in fresh, True)
        want("survival: ...and a carrier that has no gate list does NOT get one",
             SURVIVAL_COND_TAG in fresh, False)
        entry = serve_survival(None, "cH9DnW03")
        want("survival: the entry scene's own request gets the lists (it has no responses.json spec)",
             len((entry.get(SURVIVAL_TAG) or [])) if isinstance(entry, dict) else 0, 1)
        want("survival: ...and no gate list on the entry reply either",
             isinstance(entry, dict) and SURVIVAL_COND_TAG in entry, False)
        # --- the SECOND gate on the mode's entry (APP-6, 2026-09-30) -----------------------------
        # `getAreaIdWithBannerTouch` (0x1a6fa00) returns the area on an early `mov w0,w20 ; ret`
        # (0x1a6fb04) reached only when a `UserBreakThroughProgressInfo` row exists for that area with
        # `hN57axnr` (+0x40) non-zero. These four cases are the whole claim, and each can fail.
        b = serve_survival(None, "6H1R9WID")
        brow = (b.get(SURVIVAL_BREAK_TAG) or [{}])[0]
        want("survival: the break-through list lands on its own carrier",
             len(b.get(SURVIVAL_BREAK_TAG) or []), 1)
        want("survival: the break row's +0x24 carries the EVENT row's pair, not the area",
             brow.get("L9IAY8iW"), SURVIVAL_PAIR)
        want("survival: ...with the +0x40 gate non-zero, which is what opens the early return",
             str(brow.get("hN57axnr")) not in ("", "0", "None"), True)
        want("survival: ...and the +0x2c progress gate positive",
             int(brow.get("IRca7hx9") or 0) > 0, True)
        want("survival: the event row's +0x100 is the 1/2 selector, so it must not be 0",
             (b.get(SURVIVAL_TAG) or [{}])[0].get("Z0EN6jSh"), SURVIVAL_EVENT_KIND)
        # The gate that should have caught the inert row (2026-09-30): `getObjectWithAreaId` matches the
        # break row's (+0x28,+0x24) against the EVENT row's (+0x24,+0x28) -- a SWAPPED pair, reached
        # through the area. Sourcing `L9IAY8iW` from the area instead of from the event row leaves the
        # row permanently unfindable, and no single-row assertion can see that: both rows look right.
        want("survival: the break row's +0x28 equals the EVENT row's +0x24 (swapped pair)",
             brow.get("pvS5A4kE"), (b.get(SURVIVAL_TAG) or [{}])[0].get("pvS5A4kE"))
        want("survival: the break row's +0x24 equals the EVENT row's +0x28 -- NOT the area",
             brow.get("L9IAY8iW"), (b.get(SURVIVAL_TAG) or [{}])[0].get("L9IAY8iW"))
        want("survival: ...so the two-key lookup can actually resolve the row",
             (brow.get("L9IAY8iW"), brow.get("pvS5A4kE"))
             == ((b.get(SURVIVAL_TAG) or [{}])[0].get("L9IAY8iW"),
                 (b.get(SURVIVAL_TAG) or [{}])[0].get("pvS5A4kE")), True)
        want("survival: every token the break-through class reads is served (no raw key on screen)",
             len(brow), 12)
        # --- the 2w9Iyeaq party-state block (APP-8) ------------------------------------------------
        # The grammar was decoded from the client; these assert the three properties the parser needs
        # and that every one of them fails SILENTLY when wrong, so a regression is invisible in a run.
        s = serve_survival(None, "6H1R9WID")
        sblock = (s.get(SURVIVAL_STATE_TAG) or [{}])[0].get(SURVIVAL_STATE_TOKEN)
        want("survival: the party-state block rides its own carrier", isinstance(sblock, str), True)
        want("survival: ...with exactly one token, as the class reads exactly one",
             len((s.get(SURVIVAL_STATE_TAG) or [{}])[0]), 1)
        want("survival: the block is NEVER empty (readParam stores nothing for an empty value)",
             bool(sblock), True)
        # A gate that CRASHES reads as pass to anything that only counts FAIL lines, and a truncated
        # block is the one thing this block can get wrong -- so every access below is bounds-guarded
        # and reports a mismatch instead of raising. (Found 2026-09-30: a header-only block raised
        # IndexError here, and `grep -c '^quest FAIL'` reported 0 failures.)
        fields = lambda s: s.split("|")                      # noqa: E731 - local, selftest only
        blocks = sblock.split("@") if isinstance(sblock, str) else []
        parts_of = lambda b: fields(b) if b else []            # noqa: E731
        p0 = parts_of(blocks[0] if len(blocks) > 0 else None)
        p1 = parts_of(blocks[1] if len(blocks) > 1 else None)
        units = blocks[2:] if len(blocks) > 2 else []
        uf = [fields(u) for u in units]
        want("survival: the block is NEVER empty (readParam stores nothing for an empty value)",
             bool(sblock), True)
        want("survival: ...and has at least two @-blocks, or only the version is read",
             len(blocks) >= 2, True)
        want("survival: block 0 is the format version", p0[:1], [SURVIVAL_STATE_VERSION])
        want("survival: block 1 is the summon gauge", p1[:1], [SURVIVAL_SUMMON_GAUGE])
        want("survival: one unit block per party member", len(units), len(State.player.party_instance_ids()))
        want("survival: every unit block has NINE fields (the grammar's 8 pipes)",
             sorted({len(f) for f in uf}) or [-1], [9] if uf else [9])
        want("survival: <pos> is the SLOT INDEX, which is the dictionary key entry() looks up",
             [f[0] if f else None for f in uf], [str(i) for i in range(len(uf))])
        want("survival: hp/mp are the six-stat table's first two entries (HP/MP order, plain decimals)",
             all(len(f) >= 3 and f[1].isdigit() and f[2].isdigit() for f in uf), True)
        want("survival: the four CSV fields are empty, which is what the client emits for a bare unit",
             sorted({c for f in uf for c in f[5:]}) or [""], [""])
        g["SURVIVAL_STATE_CARRIERS"] = ()
        want("survival: the UN-GATED destructive state list rides ONLY its carriers",
             SURVIVAL_STATE_TAG in serve_survival(None, "6H1R9WID"), False)
        g["SURVIVAL_STATE_CARRIERS"] = was_state_carriers
        g["SURVIVAL_PARTS"] = ("event", "user", "gate", "break")
        want("survival: PARTS without 'state' serves no state block",
             SURVIVAL_STATE_TAG in serve_survival(None, "6H1R9WID"), False)
        g["SURVIVAL_PARTS"] = ("event", "user", "gate", "break", "state")
        g["SURVIVAL_BREAK_CARRIERS"] = ()
        off = serve_survival(None, "6H1R9WID")
        want("survival: the destructive break list rides ONLY its carriers",
             SURVIVAL_BREAK_TAG in off, False)
        g["SURVIVAL_BREAK_CARRIERS"] = was_brk_carriers
        g["SURVIVAL_PARTS"] = ("event", "user", "gate", "break")
        g["SURVIVAL_BREAK_GATE"] = "0"
        zero = serve_survival(None, "6H1R9WID")
        want("survival: a zero +0x40 reproduces the pre-fix failure (the function would return 0)",
             (zero.get(SURVIVAL_BREAK_TAG) or [{}])[0].get("hN57axnr"), "0")
        g["SURVIVAL_BREAK_GATE"] = was_gate
        g["SURVIVAL_EVENT_KIND"] = "0"
        want("survival: a +0x100 of 0 is what made the mode unreachable (the loop skips both branches)",
             (serve_survival(None, "6H1R9WID").get(SURVIVAL_TAG) or [{}])[0].get("Z0EN6jSh"), "0")
        g["SURVIVAL_EVENT_KIND"] = was_kind
        # the BISECT knob: the reference needs the event row (the predicate) AND a gate row (the tile), so
        # either alone must be servable without the other.
        g["SURVIVAL_PARTS"] = ("event",)
        ev_only = serve_survival(base, "lEHBdOEf")
        want("survival: PARTS=event serves the event row only",
             (SURVIVAL_TAG in ev_only, SURVIVAL_USER_TAG in ev_only), (True, False))
        want("survival: ...and adds no gate row", len(ev_only.get(SURVIVAL_COND_TAG) or []), 2)
        g["SURVIVAL_PARTS"] = ("gate",)
        gate_only = serve_survival(base, "lEHBdOEf")
        want("survival: PARTS=gate adds the row and no lists",
             (SURVIVAL_TAG in gate_only, len(gate_only.get(SURVIVAL_COND_TAG) or [])), (False, 3))
        # The knob that decides WHICH AREAS is `SURVIVAL_AREAS` (a family), not `SURVIVAL_AREA` (the
        # single-area default it is derived from at import). Driving the wrong one is exactly the drift
        # this feature exists to remove, so the cases name the effective knob.
        # PARTS is restored first because the bisect section above left it at ("gate",) -- and a case that
        # silently inherits the previous case's knobs is how a family feature looks broken when it is fine.
        g["SURVIVAL_PARTS"] = ("event", "user", "gate", "break", "state")
        g["SURVIVAL_AREAS"] = ("nope",)
        want("survival: an area with no dungeon row serves nothing (and is reported)",
             serve_survival({"a": 1}, "lEHBdOEf"), {"a": 1})
        g["SURVIVAL_AREAS"] = ("0",)
        want("survival: area 0 serves nothing", serve_survival({"a": 1}, "lEHBdOEf"), {"a": 1})
        g["SURVIVAL_AREAS"] = ("3122", "3123")
        fam = serve_survival({"a": 1}, "lEHBdOEf")
        want("survival: a FAMILY of areas yields one event row per area",
             len(fam.get(SURVIVAL_TAG) or []), 2)
        want("survival: ...and one gate row per area, each with ITS OWN resolved dungeon",
             [(r["uv60hgDW"], r["U9iHsau3"]) for r in fam.get(SURVIVAL_COND_TAG) or []],
             [("3122", "312201"), ("3123", "312301")])
        want("survival: ...each with its own first mission, read from the corpus not typed",
             [r["qo3PECw6"] for r in fam.get(SURVIVAL_COND_TAG) or []], ["31220101", "31230101"])
        g["SURVIVAL_AREAS"] = ("3122", "3122")
        dup = serve_survival({"a": 1}, "lEHBdOEf")
        want("survival: a repeated area is served ONCE, not twice",
             len(dup.get(SURVIVAL_TAG) or []), 1)
        g["SURVIVAL_AREAS"] = was_areas
        # THE ART COLLISION GATE. One wrong string here produced THREE reported symptoms at once: a
        # duplicated banner, a family that looked like a different event, and -- because the tiles could
        # not be told apart -- a tap that opened the WRONG battle. So the invariant is direct: our art
        # must not be any SHIPPED row's art. 2026-09-30: it was spd_bn_list_00003.png, byte-identical to
        # area 226's.
        # Read the SHIPPED art from the payload file, not `State.replies`: the survival carriers'
        # `0xkoNP8U` rows are not in the resolved replies (they resolve by `from_key`), so the first
        # version compared against an EMPTY set and could never fire -- the exact mutation it exists to
        # catch passed straight through.
        shipped_art = set()
        _payload = os.path.join(os.path.dirname(os.path.abspath(__file__)), "responses.json")
        try:
            with open(_payload, encoding="utf-8") as _fh:
                for _spec in json.load(_fh).values():
                    if isinstance(_spec, dict):
                        for _row in (_spec.get(SURVIVAL_COND_TAG) or []):
                            if isinstance(_row, dict) and _row.get("4yT7FUqj"):
                                shipped_art.add(_row["4yT7FUqj"])
        except (OSError, ValueError):
            pass
        want("survival: the shipped-art set is NON-EMPTY (a gate over an empty set can never fail)",
             bool(shipped_art), True)
        g["SURVIVAL_AREAS"] = ("3122", "3123")
        art_rows = [r for r in (serve_survival({"a": 1}, "lEHBdOEf").get(SURVIVAL_COND_TAG) or [])
                    if isinstance(r, dict)]
        served_art = {str(r.get("uv60hgDW")): str(r.get("4yT7FUqj") or "") for r in art_rows}
        # A case that RAISES reads as zero failures, which is how this block hid a bug once already,
        # so every access here is bounds-guarded and every assertion reads the SERVED row.
        want("survival: the family serves one gate row per area (the art cases read THESE rows)",
             len(art_rows), 2)
        # ASSERT ON WHAT IS SERVED, not on the constant. The first version compared `SURVIVAL_BANNER_ART`
        # itself, and a mutation that hard-coded the filename into the row sailed straight past both cases
        # -- caught only by an unrelated per-area case. A gate has to read the artifact.
        want("survival: the SERVED banner art exists in the shipped area pack (else the tile draws nothing)",
             bool(served_art) and all(a and os.path.isfile(os.path.join(
                 AREA_ART_ROOTS[0], "Ver461_spdungeon1", a))
                 for a in served_art.values()), True)
        want("survival: no SERVED area wears a shipped row's art (the duplicate-banner bug)",
             sorted(set(served_art.values()) & shipped_art), [])
        art = served_art
        want("survival: every area in the family gets the same art by default",
             len(set(art.values())), 1)
        g["SURVIVAL_BANNER_ART_BY_AREA"] = {"3123": "spd_20200511_bn_list_bossrush_02.png"}
        per = {str(r.get("uv60hgDW")): str(r.get("4yT7FUqj") or "") for r in
               (serve_survival({"a": 1}, "lEHBdOEf").get(SURVIVAL_COND_TAG) or [])
               if isinstance(r, dict)}
        want("survival: ...and a per-area override wins, so tiers can differ",
             (per.get("3122"), per.get("3123")),
             (SURVIVAL_BANNER_ART, "spd_20200511_bn_list_bossrush_02.png"))
        g["SURVIVAL_BANNER_ART_BY_AREA"] = {}
    finally:
        g["SURVIVAL_ON"] = was_on
        g["SURVIVAL_AREA"] = was_area
        g["SURVIVAL_PARTS"] = was_parts

    # --- the EMBLEM family: the connection-error fix, and the two refusals ---------------------------
    # The regression golden pins the BYTES; these pin the RULES, because the two ways this can silently
    # go wrong are a served literal (a stale owned list) and a write that should have been refused.
    was_player = State.player
    try:
        ep = ps2.PlayerState.load("/nonexistent", slot="selftest-emblem")
        State.player = ep
        # The 2026-09-30 failure: no group at all -> `MessageState::exist()` false -> "A connection error".
        # An EMPTY owned list is the fresh-account truth and is NOT the bug, so the group must be present
        # either way -- that is the whole difference between the two cases below.
        want("emblem: the entry reply carries the group even with NOTHING owned (the connection-error fix)",
             EMBLEM_GROUP in (serve_emblem_info(None, EMBLEM_ENTRY_REQUEST) or {}), True)
        want("emblem: ...and an empty owned list serves no rows, rather than an absent group",
             (serve_emblem_info(None, EMBLEM_ENTRY_REQUEST) or {}).get(EMBLEM_GROUP), [])
        want("emblem: another request is left alone (the step is scoped to the entry)",
             serve_emblem_info({"LhVz6aD2": []}, "wMWvnYDR"), {"LhVz6aD2": []})
        want("emblem: grant refuses an id that is not in the shipped table",
             ep.grant_emblem(999999), False)
        want("emblem: grant refuses the 0 sentinel -- it is 'none', never an owned emblem",
             ep.grant_emblem(0), False)
        want("emblem: ...so a refused grant leaves the owned set empty",
             ep.owned_emblem_ids(), [])
        want("emblem: grant takes a REAL row of F_EMBLEM_ITEM_MST (10201 = All Rounder Bronze)",
             ep.grant_emblem(10201), True)
        want("emblem: the entry reply is a PROJECTION of the state, not a literal",
             (serve_emblem_info(None, EMBLEM_ENTRY_REQUEST) or {}).get(EMBLEM_GROUP),
             [{EMBLEM_ID: "10201"}])

        def _emblem_body(eid, user=None):
            return json.dumps({EMBLEM_GROUP: [{EMBLEM_USER_ID: user or ep.data["identity"]["user_id"],
                                               EMBLEM_ID: str(eid)}]})

        # A set of an UNOWNED emblem must not move the header; that is the case that would put a crest on
        # the status HUD with no catalogue row behind it. And it must still ANSWER -- an empty body is the
        # connection error, so a silent refusal is the same defect wearing a different hat.
        ref = apply_emblem_setting(_emblem_body(10301), None)
        want("emblem: setting an UNOWNED emblem is REFUSED (no crest with no row behind it)",
             ep.data["profile"]["player_emblem_id"], 0)
        want("emblem: ...and the refusal still answers with a group, so it is not a connection error",
             EMBLEM_GROUP in (ref or {}), True)
        ref = apply_emblem_setting(_emblem_body(10201, user="999999"), None)
        want("emblem: a body naming ANOTHER user is refused",
             ep.data["profile"]["player_emblem_id"], 0)
        want("emblem: ...and answers too", EMBLEM_GROUP in (ref or {}), True)
        out = apply_emblem_setting(_emblem_body(10201), None)
        want("emblem: setting an OWNED emblem moves the DISPLAYED id",
             ep.data["profile"]["player_emblem_id"], 10201)
        want("emblem: ...and it is the token the header reads that changes",
             ep._team_row()["n69quiCJ"], "10201")
        want("emblem: ...and the reply carries the owned set plus the team-info PLACEHOLDER for the "
             "projection to fill", (EMBLEM_GROUP in out, TEAM_INFO_TAG in out), (True, True))
        want("emblem: the sentinel is always settable, so the crest can be taken back off",
             (apply_emblem_setting(_emblem_body(0), None) or {}).get(EMBLEM_GROUP) is not None
             and ep.data["profile"]["player_emblem_id"], 0)
        # A body that will not parse must never break the reply -- the same rule every other reader follows.
        want("emblem: a malformed body leaves the reply alone rather than raising",
             (apply_emblem_setting("not json", {"LhVz6aD2": []}),
              apply_emblem_setting(json.dumps({EMBLEM_GROUP: []}), {"LhVz6aD2": []})),
             ({"LhVz6aD2": []}, {"LhVz6aD2": []}))

        # --- the EMBLEM EVENT: the shipped campaign tables, the crest, and the tier mail -----------------
        # The tables ARE the feature: the client reads the SCHEDULE from them and the account's progress from
        # us, so a model that mis-joins them draws a board that is off by a row -- and nothing else would say so.
        want("emblem event: the live campaign has the 22 reward rows and 12 objectives the community table has",
             (len(ps2.emblem_monthly_mst()), len(ps2.emblem_weekly_mst())), (22, 12))
        want("emblem event: ...keyed by the community table's crest counts 0/1/5/7/8/9/10/11/12",
             sorted({r[ps2.EMBLEM_MONTHLY_CRESTS] for r in ps2.emblem_monthly_mst()}, key=int),
             ["0", "1", "5", "7", "8", "9", "10", "11", "12"])
        want("emblem event: every objective's milestone id is also a reward row's (the join holds)",
             [o["id"] for o in State.player.emblem_objectives()
              if o["id"] not in {r[ps2.EMBLEM_MONTHLY_MILESTONE] for r in ps2.emblem_monthly_mst()}], [])
        ev_out = serve_emblem_event(None, EMBLEM_EVENT_REQUEST)
        want("emblem event: the event reply carries all THREE groups the screen reads",
             all(g in (ev_out or {}) for g in (EMBLEM_MONTHLY_GROUP, EMBLEM_WEEKLY_GROUP,
                                               EMBLEM_MILESTONE_GROUP)), True)
        want("emblem event: ...announcing the live campaign",
             ((ev_out or {}).get(EMBLEM_MONTHLY_GROUP) or [{}])[0].get(EMBLEM_CAMPAIGN_TOKEN), "9000001")
        want("emblem event: ...one weekly row per shipped objective, and the same ids in the windows",
             (len((ev_out or {}).get(EMBLEM_WEEKLY_GROUP) or []),
              [w.get(EMBLEM_MILESTONE_TOKEN) for w in (ev_out or {}).get(EMBLEM_MILESTONE_GROUP) or []]),
             (12, [o["id"] for o in State.player.emblem_objectives()]))
        want("emblem event: the milestone windows bracket NOW -- the client's own gate",
             all(w[EMBLEM_START_DATE] <= time.strftime(PANEL_QUEST_WINDOW_FORMAT) <= w[EMBLEM_END_DATE]
                 for w in (ev_out or {}).get(EMBLEM_MILESTONE_GROUP) or [{}]), True)
        want("emblem event: another request is left alone",
             serve_emblem_event({"LhVz6aD2": []}, EMBLEM_ENTRY_REQUEST), {"LhVz6aD2": []})
        # ...and the credit path. A mission end names a MISSION and nothing else, so a mission-shaped
        # objective is the only shape that can be honoured; that is the tested contract, not a claim about
        # the point-shaped rows (they stay uncleared -- see the note's Open section).
        evep = ps2.PlayerState.load("/nonexistent", slot="selftest-emblem-event")
        State.player = evep
        clear_rows = [o for o in evep.emblem_objectives() if o["type"] == "2"]
        want("emblem event: the campaign has clear-shaped objectives, and their target is a MISSION id",
             [(len(clear_rows) > 0), (clear_rows[0]["target"].isdigit() if clear_rows else False)], [True, True])
        target = clear_rows[0]["target"]
        want("emblem event: clearing that mission clears the objective and earns ONE crest",
             (evep.credit_emblem_objective(target), evep.emblem_tokens()), ([clear_rows[0]["id"]], 1))
        want("emblem event: ...and the crest is the shipped IMPORTANT item 20 in the type-23 bag",
             evep.item_count(ps2.EMBLEM_CREST_TYPE, ps2.EMBLEM_CREST_ITEM), 1)
        want("emblem event: clearing the same mission again changes nothing",
             (evep.credit_emblem_objective(target), evep.emblem_tokens()), ([], 1))
        mails = deliver_emblem_tier_mail()
        want("emblem event: the opening tier (crest 0) and the earned one (crest 1) are mailed, once each",
             (len(mails), len(evep.mailbox)), (2, 2))
        want("emblem event: ...and a second delivery mails nothing (the idempotence)",
             deliver_emblem_tier_mail(), [])
        want("emblem event: the tier-1 mail is the shipped reward set (Lapis 100+100, VIP Coin 100)",
             sorted((i["item_type"], i["item_id"], i["count"]) for i in evep.mailbox[-1]["items"]),
             [(23, 110003, 100), (50, 1, 100), (50, 1, 100)])
        want("emblem event: the mail is an ORDINARY mail, not a daily-login one (a day number here would "
             "make the client hunt for a calendar day)",
             (bool(evep.mailbox[-1].get("daily_login")),
              evep.mailbox[-1].get("title") == "SG_DAILY_LOGIN_TITLE", bool(evep.mailbox[-1].get("attach_type"))),
             (False, False, True))
    finally:
        State.player = was_player

    # --- the EVENT GATE + the event currency (APP-35): the Fundamental Forces slice -------------------
    # The gate row is the ONE thing that makes an area's tile exist, so the two properties that matter are
    # that it is DERIVED (an area word is the only input) and that it is APPENDED -- the list clears on
    # arrival, so replacing it would delete another event's tiles.
    was_player = State.player
    try:
        import player_state as _ps4
        from ffbe_battle import roll_drops as _roll
        State.player = _ps4.PlayerState(_ps4.default_state("100000001", "Rain", now_ms=1789000000000),
                                        mode="live")
        facts = _event_area_facts()
        want("event gate: naming an area resolves its dungeon and first mission from the shipped tables",
             facts[0], ("970", "97001", "9700101"))
        want("event gate: ...for EVERY named area -- a new event is one word, not a table edit",
             [(a, d, m) for a, d, m in facts if a == "888"], [("888", "88801", "8880100")])
        carrier = {EVENT_GATE_TAG: [{"uv60hgDW": "226", "U9iHsau3": "21104", "qo3PECw6": "2110401"}]}
        out = serve_event_gate(carrier, EVENT_GATE_CARRIERS[0])
        rows = (out or {}).get(EVENT_GATE_TAG) or []
        # The carrier's own row FIRST and untouched, then exactly one row per named area. The count is
        # derived from the facts list rather than typed, so adding an event to the lever cannot stale this.
        want("event gate: the rows are APPENDED, one per named area, the carrier's own tile untouched",
             (rows[0].get("uv60hgDW") if rows else None, len(rows)), ("226", 1 + len(facts)))
        ev = next((r for r in rows if r.get("uv60hgDW") == "888"), {})
        want("event gate: ...carrying the derived area/dungeon/mission and the 1-BASED Event tab",
             (ev.get("U9iHsau3"), ev.get("qo3PECw6"), ev.get("qnUhs32P")),
             ("88801", "8880100", "1"))
        want("event gate: a request that does not carry the list is left alone",
             serve_event_gate(carrier, "RoutineHomeUpdateRequest"), carrier)
        # The currency: additive by construction, and only under an event area -- so it cannot change an
        # existing mission's drops.
        ev_rolled = (_roll({DROP_TAG_ROWS: [{DROP_TOKEN: "20:101000100:1:100"}]}, mid="9700101",
                           is_battle_bundle=True) or {}).get(DROP_TAG_ROWS) or [{}]
        norm_rolled = (_roll({DROP_TAG_ROWS: [{DROP_TOKEN: "20:101000100:1:100"}]}, mid="1110101",
                             is_battle_bundle=True) or {}).get(DROP_TAG_ROWS) or [{}]
        want("event currency: the event area's battle carries the authored item",
             "20:310001240:5:100" in (ev_rolled[0].get(DROP_TOKEN) or ""), True)
        want("event currency: a non-event mission is untouched",
             "310001240" in (norm_rolled[0].get(DROP_TOKEN) or ""), False)
    finally:
        State.player = was_player

    # --- the 8th-anniversary ticket BY MAIL (SS11.16) -----------------------------------------------
    # The path is three steps and each has a distinct way of silently doing nothing: the mail is only
    # delivered on its carrier, only ONCE per day, and only grants when CLAIMED. A case that checks the
    # first and misses the other two passes with a feature that appears broken.
    was_player = State.player
    try:
        import player_state as _ps
        State.player = _ps.PlayerState(_ps.default_state("100000001", "Rain", now_ms=1789000000000),
                                       mode="live")

        def _reset_ticket():
            """Clear the mailbox, the per-day marker AND the bag.

            All three, because a case that inherits the previous case's leftovers is how a family feature
            looks broken when it is fine -- the two bag cases below failed exactly this way until the item was
            cleared too (an earlier case CLAIMED a ticket, so the bag already held one).
            """
            State.player.mailbox.clear()
            State.player.data["anniversary"] = {"last_issued": "", "issued_total": 0}
            try:
                State.player.spend_item(ANNIVERSARY_TICKET_TYPE, ANNIVERSARY_TICKET_ITEM,
                                        ANNIVERSARY_TICKET_COUNT)
            except Exception:  # noqa: BLE001 - the row may simply not exist yet
                pass

        _reset_ticket()
        n0 = len(State.player.mailbox)
        deliver_anniversary_ticket({"c1qYg84Q": []}, ANNIVERSARY_TICKET_CARRIER.split(",")[0])
        delivered = State.player.mailbox[n0:]
        want("anniversary: one ticket mail is delivered on its carrier",
             (len(delivered), delivered[0]["items"] if delivered else None),
             (1, [{"item_type": ANNIVERSARY_TICKET_TYPE, "item_id": ANNIVERSARY_TICKET_ITEM,
                   "count": ANNIVERSARY_TICKET_COUNT}]))
        m = delivered[0] if delivered else {}
        want("anniversary: it is an ORDINARY mail, not a daily-login one -- the client reads those by TITLE "
             "and MESSAGE (isDailyLoginReward 0x1d80058) and would look for a day number here",
             (bool(m.get("daily_login")), m.get("title") == "SG_DAILY_LOGIN_TITLE", bool(m.get("attach_type"))),
             (False, False, True))
        deliver_anniversary_ticket({"c1qYg84Q": []}, ANNIVERSARY_TICKET_CARRIER.split(",")[0])
        deliver_anniversary_ticket({}, "PBSP9qn5")
        want("anniversary: idempotent within a day, and a NON-carrier reply delivers nothing",
             len(State.player.mailbox) - n0, 1)

        # THE POLLED CARRIER: what removes the need for a reset, on an EMPTY mailbox (a case that cannot
        # fail is not a case -- the first version of this ran after the others had delivered and passed with
        # the carrier struck out of the config).
        _reset_ticket()
        deliver_anniversary_ticket({"QB47tpMF": []}, "Daud71Hn")
        want("anniversary: the 30-minute RoutineHomeUpdate carrier delivers the mail on its own (an "
             "already-running client needs NO reset)",
             (len(State.player.mailbox),
              State.player.mailbox[0]["items"][0]["item_id"] if State.player.mailbox else None),
             (1, ANNIVERSARY_TICKET_ITEM))
        _reset_ticket()
        for c in ANNIVERSARY_TICKET_CARRIER.split(","):
            deliver_anniversary_ticket({}, c)
        want("anniversary: EVERY carrier together still yields ONE mail a day (the daily marker, not the "
             "carrier set, is what makes it idempotent)",
             len(State.player.mailbox), 1)

        # THE LOOP THE OPERATOR FOUND (2026-10-01): receive, delete, and the ticket is BACK. The inbox
        # request is the carrier AND the delete path, so a marker living in the mail was erased by the
        # delete and the next inbox request re-issued it. The marker is state, so a delete cannot re-arm it.
        _reset_ticket()
        deliver_anniversary_ticket({"QB47tpMF": []}, "KQHpi0D7")
        claimed = State.player.receive_mail(State.player.mailbox[0]["mail_id"])
        State.player.delete_received_mail()
        deliver_anniversary_ticket({"QB47tpMF": []}, "KQHpi0D7")
        deliver_anniversary_ticket({"QB47gpMF": []}, "KQHpi0D7")
        want("anniversary: a DELETED ticket is not re-issued by the very inbox refresh that deleted it -- "
             "the receive/delete/receive loop the operator reported",
             (len(claimed), len(State.player.mailbox)), (1, 0))

        _reset_ticket()
        deliver_anniversary_ticket({"c1qYg84Q": []}, ANNIVERSARY_TICKET_CARRIER.split(",")[0])
        want("anniversary: nothing is in the bag until the mail is RECEIVED (that is the whole point of "
             "posting it -- an over-full bag was harmless)",
             any(r.get("tL6G9egd") == str(ANNIVERSARY_TICKET_ITEM) for r in State.player.item_rows()), False)
        m = State.player.mailbox[0]
        granted = State.player.receive_mail(m["mail_id"])
        want("anniversary: claiming grants the ticket exactly once",
             (len(granted), State.player.receive_mail(m["mail_id"])), (1, []))
        want("anniversary: and the BAG now projects it -- a grant nothing projects did not happen "
             "(the 2026-09-27 rule)",
             [r for r in State.player.item_rows() if r.get("tL6G9egd") == str(ANNIVERSARY_TICKET_ITEM)],
             [{"tL6G9egd": str(ANNIVERSARY_TICKET_ITEM), "Qy5EvcK1": "1"}])
    finally:
        State.player = was_player

    for label, ok, got, expect in checks:
        print(f"quest {'ok  ' if ok else 'FAIL'} {label}" + ("" if ok else f"  (got {got!r}, want {expect!r})"))
    if rc:
        print(f"SELFTEST FAIL: {rc} quest-join case(s)")
    return rc


def gid_ok(tables, row) -> bool:
    """Is this row in a group we serve? Keeps the name check scoped to what the server actually serves."""
    return str(row.get(SP_GROUP_ID, "")) in set(SP_CHALLENGE_DEFAULT_GROUPS)


if __name__ == "__main__":
    import sys as _sys
    if "--selftest" in _sys.argv:
        raise SystemExit(_selftest())
    if "--serve-sp-challenge" in _sys.argv:
        # The read-only "what would the screen get" view: the groups `serve_sp_challenge` builds for a
        # FRESH state, printed. A viewer, not a second writer -- it calls the real function, so it cannot
        # disagree with what a run serves.
        import player_state as _ps
        State.player = _ps.PlayerState.load("/nonexistent", slot="inspect")
        out = serve_sp_challenge(json.load(open(os.path.join(WORK, "server", "responses.json"),
                                                encoding="utf-8")).get(SP_CHALLENGE_REQUEST, {}),
                                 SP_CHALLENGE_REQUEST)
        for tag in (SP_CHALLENGE_SCHEDULE_TAG, SP_CHALLENGE_STATUS_TAG, SP_CHALLENGE_TICKER_TAG,
                    SP_CHALLENGE_INFO_TAG):
            rows = out.get(tag) or []
            print(f"{tag}: {len(rows)} row(s)")
            for r in rows:
                print("   " + json.dumps(r, ensure_ascii=False))
        raise SystemExit(0)
    print("ffbe_player is a library module; the server entry point is server/capture_server.py")
    raise SystemExit(0)
