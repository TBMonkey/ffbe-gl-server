#!/usr/bin/env python3
"""ffbe_equip.py — the `UnitEquipRequest` (`pB3st6Tg`) WRITE path: equipment, materia and the vision card.

WHY THIS EXISTS (SW-130 slice A, 2026-11-05). `UnitEquipRequest` was answer-only: the reply groups
(`TJ9eL80N` = `UnitEquipResponse`, `LhVz6aD2` = user state) were projected from the roster, but nothing read
the request, so an equipment edit was discarded the moment the next user-state reply served the old row back.
The vision card is worse than a lost edit -- it is the only place the VC slot can be written at all:
there is NO `VisionCardEquipRequest` in the 222-class map (only `LibraryVisionCardEntryRequest`), so equipping
a card rides this request like equipment does, and the slot unlock item is spent here.

THE WIRE FORMAT IS THE CLIENT'S OWN, from a live capture (`out/requests/20260921T182132_0025.json`, group
`Er92Kdhm`):

    "93Y6evuT": "1790032802751:1-302000100@2-0@3-404000200@4-406000100@5-409000100@6-0"
    "80NUYFMJ": "1790032802751:1-0@2-0"
    "nQ1iAGf3": "1790032802751:0-0"

i.e. every value is `<instanceId>:<slot>-<id>@<slot>-<id>@...` and the instance prefix is repeated on all
three. `UnitEquipRequest::createBody` (`0x19a3fc0`) adds the group; `addPartyEquipChangeInfo` (`0x19a4078`)
writes exactly these six tokens (`93Y6evuT`, `80NUYFMJ`, `nQ1iAGf3`, `2c8nqLrM`, `xBi3L6yR`, `r21y0YzS`).
The response side stores the value VERBATIM: `UserUnitEquipInfo::setVisionCardInfo` (`0x1da01f4`) is a
`std::string::assign` into `+0x58` and nothing else, and `UserUnitEquipInfoList::getEquipUserVisionCardId`
(`0x1da4598`) hands that same string back. So the slot's value is ours to choose, and we serve
`"0-<card key>"` -- the same `slot-id` tail the client itself writes.

THE MASTER CARD IS THE SLOT UNLOCK. `F_ITEM_MST` `240000100` = マスターカード, sold at
`F_MEDAL_EXCHANGE_MST` row `226702` for one `235037300` (？？？のクリスタル). There is no item-use request
class in v9.0.0, so the only wire that can carry the spend is this one: a unit that has never been unlocked,
given a card, consumes ONE `240000100` and is marked `master_card`. The mark is served as `T9Apq5fS` --
`UserUnitInfo+0x320`, which is exactly what `UnitInfoBase::getMasterCardFlg` (`0x1dd33dc`) returns
(`ldrb w8,[x19,#0x320]`, with the Neo-Vision shift dance around it).

A NOTE ON A RECORDED OFFSET. SW-130's 2026-11-04 entry said the flag was `UnitInfoBase+0x132`. The
disassembly of the symbol it named says `+0x320`; `+0x132` appears nowhere in the function. The recorded
offset was wrong, and `T9Apq5fS` was already in the field map at `+0x320`, so the "the flag has no token"
blocker was an artefact of the offset error, not a missing token.

COHERENCE RULES (each one is a refusal, never a partial write):
  * the instance must be on the roster, and all three strings must name the SAME instance;
  * an equipment / materia id must be OWNED -- currently equipped somewhere, or held in
    `inventory.equipment` / `inventory.materia` (the same "everything served is owned" invariant
    `_equip_rows` documents, and an unowned id draws a blank frame);
  * a vision card must be owned, and not already equipped on ANOTHER unit (one card, one unit);
  * a first-time VC equip needs the Master Card item; re-equipping an already-unlocked unit does not.
Refusing the whole row is deliberate: applying the affordable half of a selection error would turn a client
mistake into silent server data loss (the `apply_party_edit` rule, same reason).
"""
from __future__ import annotations

import json

import player_state as ps
from ffbe_state import State
from ffbe_vision import MASTER_CARD_ITEM_ID      # one home for the item id (the family module)

UNIT_EQUIP_REQUEST = "pB3st6Tg"

# `UnitEquipRequest` group and its tokens (client `UnitEquipRequest::createBody` 0x19a3fc0 /
# `addPartyEquipChangeInfo` 0x19a4078; confirmed verbatim in out/requests/20260921T182132_0025.json).
EQUIP_GROUP = "Er92Kdhm"
TOK_EQUIP = "93Y6evuT"          # `<instance>:1-id@2-id@...@6-id`  (slots 1..6, "0" = empty)
TOK_MATERIA = "80NUYFMJ"        # `<instance>:1-id@2-id@...`       (one entry per ability slot)
TOK_VISION_CARD = "nQ1iAGf3"    # `<instance>:0-<card key>`        (slot 0, "0" = none)
TOK_SKIN = "2c8nqLrM"           # `<instance>:0`; the skin render id -- not modelled, ignored
TOK_DECK = "r21y0YzS"
TOK_PARTY_TYPE = "xBi3L6yR"

# `F_ITEM_MST` 240000100 = マスターカード: the vision-card slot unlock, sold by `F_MEDAL_EXCHANGE_MST`
# row 226702 (`7iJpH5zZ` = `20:240000100:1:100`, currency 235037300, price 1). One per unlocked unit. The id
# is imported from `ffbe_vision` so the whole family has ONE home for it.

# The roster field the equip reply projects as `nQ1iAGf3` (`player_state._equip_rows`) and the flag token
# `UserUnitInfo+0x320` (`UnitInfoBase::getMasterCardFlg` 0x1dd33dc), served with the roster row.
VISION_SLOT_FIELD = "vision_card"
MASTER_CARD_FIELD = "master_card"


def _request_json(plaintext):
    """The request body as a dict, or None. Local (not imported from ffbe_player) so this module has no
    dependency on the reply-side module -- it is a write step, and ffbe_vision imports ffbe_player lazily
    for the same reason."""
    if not plaintext:
        return None
    try:
        body = json.JSONDecoder().raw_decode(plaintext)[0]
    except Exception:  # noqa: BLE001 - a body that will not parse must never break the reply
        return None
    return body if isinstance(body, dict) else None


def _split_instance(value):
    """`"<instance>:<rest>"` -> (instance, rest), or ("", "") when there is no numeric instance prefix.

    The prefix is REQUIRED: a row without it cannot be attributed to a unit, and guessing the unit from
    the value's tail would let one unit's edit land on another.
    """
    head, sep, tail = str(value or "").partition(":")
    if not sep or not head.isdigit():
        return "", ""
    return head, tail


def _parse_slots(rest):
    """`"1-id@2-id@..."` -> [(slot, id)], or None when malformed.

    An empty id ("2-") is malformed rather than "none": the client writes `2-0`, and treating "" as unset
    would silently accept a truncated value.
    """
    out = []
    for part in str(rest or "").split("@"):
        if not part:
            continue
        slot, sep, value = part.partition("-")
        if not sep or not slot.isdigit() or not value.strip():
            return None
        out.append((int(slot), value.strip()))
    if len({slot for slot, _ in out}) != len(out):
        return None                     # a duplicate slot is not a list, it is two answers for one slot
    return out


def _parse_card(rest):
    """`"0-<card key>"` -> the key (`""` for `"0"`), or None when malformed."""
    slot, sep, value = str(rest or "").partition("-")
    if not sep or not slot.isdigit():
        return None
    value = value.strip()
    return "" if value in ("0", "") else value


def _owned_equipment(player) -> set:
    """Equipment ids the account owns: equipped somewhere, or held in `inventory.equipment`.

    Both halves are needed. The starter kit lives on the ROSTER ROWS (not in the inventory list), and
    `_equip_rows`' own docstring records why: an equipped id that is not owned either is invisible
    (`UserEquipItemInfoListBase::getItemInfo` returns null -> a blank frame).
    """
    ids = set()
    for unit in player.data.get("roster") or []:
        for value in (unit.get("equipment") or {}).values():
            value = str(value)
            if value not in ("", "0"):
                ids.add(value)
    for item in (player.data.get("inventory") or {}).get("equipment") or []:
        if str(item.get("item_id")) and int(item.get("count", 0) or 0) > 0:
            ids.add(str(item["item_id"]))
    return ids


def _owned_materia(player) -> set:
    """Materia ids the account owns: equipped somewhere, or held in `inventory.materia`."""
    ids = set()
    for unit in player.data.get("roster") or []:
        for value in unit.get("materia") or []:
            value = str(value)
            if value not in ("", "0"):
                ids.add(value)
    for item in (player.data.get("inventory") or {}).get("materia") or []:
        if str(item.get("item_id")) and int(item.get("count", 0) or 0) > 0:
            ids.add(str(item["item_id"]))
    return ids


def _card_keys(player) -> dict:
    """user-vision-card key -> the state row (the same key rule `ffbe_vision._key` and
    `player_state.vision_card_rows` apply: `user_vc_id`, falling back to the master id)."""
    out = {}
    for card in player.data.get("vision_cards") or []:
        key = str(card.get("user_vc_id") or card.get("vc_id") or "")
        if key:
            out[key] = card
    return out


def _equipped_card_owner(player, key, instance_id) -> str:
    """The OTHER unit already carrying `key`, or "" when free / carried by this unit."""
    for unit in player.data.get("roster") or []:
        if str(unit.get("instance_id")) == str(instance_id):
            continue
        if str(unit.get(VISION_SLOT_FIELD) or "") == str(key):
            return str(unit.get("instance_id"))
    return ""


def _apply_row(player, row, owned_equip, owned_materia, cards) -> str:
    """Apply one `Er92Kdhm` row; return a short status for the log ("applied" / "refused: ...").

    Everything is validated BEFORE anything is written, and the caller only commits when this returns
    "applied" -- so a refusal leaves the roster exactly as it was.
    """
    rests = {}
    instance = ""
    for token in (TOK_EQUIP, TOK_MATERIA, TOK_VISION_CARD):
        row_instance, rest = _split_instance(row.get(token))
        if not row_instance:
            return "refused: %s names no instance" % token
        if instance and row_instance != instance:
            return "refused: the row's strings name different instances"
        instance = row_instance
        rests[token] = rest
    if not rests:
        return "refused: row carries none of the equip tokens"

    unit = next((u for u in player.data.get("roster") or []
                 if str(u.get("instance_id")) == instance), None)
    if unit is None:
        return "refused: instance %s is not in the roster" % instance

    equip = _parse_slots(rests[TOK_EQUIP])
    materia = _parse_slots(rests[TOK_MATERIA])
    card = _parse_card(rests[TOK_VISION_CARD])
    if equip is None or materia is None or card is None:
        return "refused: malformed slot list"

    stray_equip = [i for _, i in equip if i != "0" and i not in owned_equip]
    if stray_equip:
        return "refused: equipment not owned: %s" % ", ".join(sorted(set(stray_equip))[:4])
    if any(slot < 1 or slot > 6 for slot, _ in equip):
        return "refused: equipment slot outside 1..6"
    stray_materia = [i for _, i in materia if i != "0" and i not in owned_materia]
    if stray_materia:
        return "refused: materia not owned: %s" % ", ".join(sorted(set(stray_materia))[:4])
    if card and card not in cards:
        return "refused: vision card %s is not owned" % card
    if card:
        owner = _equipped_card_owner(player, card, instance)
        if owner:
            return "refused: vision card %s is equipped on unit %s" % (card, owner)

    unlocked = bool(unit.get(MASTER_CARD_FIELD))
    spend_master_card = bool(card) and not unlocked
    if spend_master_card and player.item_count(ps.BAG_ITEM_TYPE, MASTER_CARD_ITEM_ID) < 1:
        return ("refused: no Master Card item (%s) to unlock this unit's slot"
                % MASTER_CARD_ITEM_ID)

    if not player._mutate():
        return "refused: state is frozen"

    if spend_master_card:
        player.spend_item(ps.BAG_ITEM_TYPE, MASTER_CARD_ITEM_ID, 1)
        unit[MASTER_CARD_FIELD] = True
    unit["equipment"] = {str(slot): value for slot, value in equip if value != "0"}
    # Preserve the SLOT INDEX the client named: `_equip_rows` renumbers from 1, so packing the list densely
    # would move a materia into a different slot than the one the player edited.
    slots = {slot: value for slot, value in materia}
    unit["materia"] = [slots.get(i, "0") for i in range(1, (max(slots) if slots else 0) + 1)]
    unit[VISION_SLOT_FIELD] = card
    # The counters the reply carries are derived from the roster (`_equip_item_rows`), so marking dirty is
    # the whole commit; `save_state` runs after the projection in the pipeline.
    player.dirty = True
    return "applied" + (" (+1 Master Card spent)" if spend_master_card else "")


def apply_unit_equip(spec, reply_id, plaintext):
    """Pipeline step: record the client's edit for every `Er92Kdhm` row, then return the spec unchanged.

    The reply groups are the PROJECTION's job (`apply_to_reply` -> `_unit_rows` / `_equip_rows`), which is
    why this step writes state and adds nothing: the reply the client is waiting for carries the full
    user-state block, and a second, request-shaped group here would be a competing source of truth.
    """
    if str(reply_id) != UNIT_EQUIP_REQUEST or State.player is None:
        return spec
    body = _request_json(plaintext)
    rows = body.get(EQUIP_GROUP) if isinstance(body, dict) else None
    out = dict(spec) if isinstance(spec, dict) else {}
    if not isinstance(rows, list) or not rows:
        print("unit equip: request carried no %s rows; nothing changed" % EQUIP_GROUP, flush=True)
        return out
    player = State.player
    if player.mode == "frozen":
        print("unit equip: frozen state policy -- edit not applied", flush=True)
        return out
    owned_equip = _owned_equipment(player)
    owned_materia = _owned_materia(player)
    cards = _card_keys(player)
    applied = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        instance, _ = _split_instance(row.get(TOK_EQUIP) or row.get(TOK_VISION_CARD))
        party = row.get(TOK_PARTY_TYPE)
        if party is not None and not isinstance(party, str):
            # The reported NV+ request sends this field as a JSON number. We do not validate it (the
            # instance is the identity), but log the shape so a driven run can confirm it.
            print("unit equip: non-string party type %r (%s) -- accepted"
                  % (party, type(party).__name__), flush=True)
        status = _apply_row(player, row, owned_equip, owned_materia, cards)
        if status == "applied" or status.startswith("applied "):
            applied.append(instance)
        print("unit equip: %s -> %s" % (instance or "?", status), flush=True)
    print("unit equip: %d of %d row(s) applied" % (len(applied), len(rows)), flush=True)
    return out


# --- selftest ---------------------------------------------------------------------------------------
def _selftest() -> int:
    """The refusal matrix, plus the one path that must actually write.

    Each case is a rule from the module docstring. The two that matter most are the ECONOMY guard (a first
    VC equip without the Master Card item must be refused) and NO-PARTIAL-WRITE (an unowned id in one slot
    must leave the whole row untouched) -- both are silent data loss if they regress, which is why they get
    a gate rather than a comment. `server_audit.py` runs this.
    """
    import json as _json
    failed = []

    def fresh():
        player = ps.PlayerState.load("/nonexistent", slot="equipselftest")
        State.player = player
        player.grant_unit("100000315", instance_id="1000000315")
        player.grant_unit("100000316", instance_id="1000000316")
        player.grant_equipment("302000100", 2)
        player.data.setdefault("inventory", {}).setdefault("materia", []).append(
            {"item_id": 400000100, "count": 2})
        player.data.setdefault("vision_cards", []).append(
            {"vc_id": "100000101", "user_vc_id": "100000101", "level": 1})
        return player

    def body(instance, equip="1-0@2-0@3-0@4-0@5-0@6-0", materia="1-0", card="0-0", party_type="0"):
        return _json.dumps({EQUIP_GROUP: [{
            TOK_EQUIP: "%s:%s" % (instance, equip),
            TOK_MATERIA: "%s:%s" % (instance, materia),
            TOK_VISION_CARD: "%s:%s" % (instance, card),
            TOK_SKIN: "%s:0" % instance, TOK_PARTY_TYPE: party_type, TOK_DECK: "0"}]})

    def unit(player, iid="1000000315"):
        return next(u for u in player.data["roster"] if str(u.get("instance_id")) == iid)

    def case(name, ok, want, got):
        print(f"equip {'ok  ' if ok else 'FAIL'} {name} (want {want}, got {got})")
        if not ok:
            failed.append(name)

    p = fresh()
    apply_unit_equip({}, UNIT_EQUIP_REQUEST,
                     body("1000000315", equip="1-302000100@2-0@3-0@4-0@5-0@6-0", materia="1-400000100"))
    case("equipment recorded", unit(p)["equipment"] == {"1": "302000100"}, "{'1': '302000100'}",
         unit(p)["equipment"])
    case("materia recorded", [x for x in unit(p)["materia"] if x != "0"] == ["400000100"],
         "['400000100']", unit(p)["materia"])
    p = fresh()
    apply_unit_equip({}, UNIT_EQUIP_REQUEST,
                     body("1000000315", equip="1-302000100@2-0@3-0@4-0@5-0@6-0", party_type=0))
    case("a NUMERIC party type (the reported NV+ shape) still applies",
         unit(p)["equipment"] == {"1": "302000100"}, "{'1': '302000100'}", unit(p)["equipment"])

    p = fresh()
    apply_unit_equip({}, UNIT_EQUIP_REQUEST,
                     body("1000000315", equip="1-302000100@2-999999999@3-0@4-0@5-0@6-0"))
    case("an unowned equipment id refuses the WHOLE row", unit(p)["equipment"] == {}, "{}",
         unit(p)["equipment"])

    apply_unit_equip({}, UNIT_EQUIP_REQUEST, body("9999999999", equip="1-302000100@2-0@3-0@4-0@5-0@6-0"))
    case("an instance off the roster is refused", unit(p)["equipment"] == {}, "{}", unit(p)["equipment"])

    p = fresh()
    apply_unit_equip({}, UNIT_EQUIP_REQUEST, body("1000000315", card="0-100000101"))
    case("a first VC equip without the Master Card is refused", unit(p).get("vision_card") == "", "''",
         unit(p).get("vision_card"))

    p.grant_item(ps.BAG_ITEM_TYPE, MASTER_CARD_ITEM_ID, 1)
    apply_unit_equip({}, UNIT_EQUIP_REQUEST, body("1000000315", card="0-100000999"))
    case("an unowned VC is refused", unit(p).get("vision_card") == "", "''", unit(p).get("vision_card"))

    apply_unit_equip({}, UNIT_EQUIP_REQUEST, body("1000000315", card="0-100000101"))
    rows = {r["og2GHy49"]: r for r in p._equip_rows()}
    urows = {r["og2GHy49"]: r for r in p._unit_rows()}
    case("the Master Card is spent exactly once", p.item_count(ps.BAG_ITEM_TYPE, MASTER_CARD_ITEM_ID) == 0,
         "0", p.item_count(ps.BAG_ITEM_TYPE, MASTER_CARD_ITEM_ID))
    case("the slot field is set", unit(p).get("vision_card") == "100000101", "'100000101'",
         unit(p).get("vision_card"))
    case("the equip row projects 0-<key>", rows["1000000315"].get("nQ1iAGf3") == "0-100000101",
         "'0-100000101'", rows["1000000315"].get("nQ1iAGf3"))
    case("the roster row projects T9Apq5fS", urows["1000000315"].get("T9Apq5fS") == "1", "'1'",
         urows["1000000315"].get("T9Apq5fS"))
    case("a unit with no card keeps the empty slot form", rows["1000000316"].get("nQ1iAGf3") == "", "''",
         rows["1000000316"].get("nQ1iAGf3"))

    apply_unit_equip({}, UNIT_EQUIP_REQUEST, body("1000000316", card="0-100000101"))
    case("one card cannot sit on two units", unit(p, "1000000316").get("vision_card") == "", "''",
         unit(p, "1000000316").get("vision_card"))

    p.grant_item(ps.BAG_ITEM_TYPE, MASTER_CARD_ITEM_ID, 1)
    apply_unit_equip({}, UNIT_EQUIP_REQUEST, body("1000000315", card="0-0"))
    apply_unit_equip({}, UNIT_EQUIP_REQUEST, body("1000000315", card="0-100000101"))
    case("re-equipping an unlocked unit spends nothing", p.item_count(ps.BAG_ITEM_TYPE, MASTER_CARD_ITEM_ID) == 1
         and unit(p).get("vision_card") == "100000101", "item 1, card set",
         "item %d, card %r" % (p.item_count(ps.BAG_ITEM_TYPE, MASTER_CARD_ITEM_ID),
                               unit(p).get("vision_card")))

    before = unit(p).get("vision_card")
    mixed = _json.loads(body("1000000315", card="0-0"))
    mixed[EQUIP_GROUP][0][TOK_MATERIA] = "1000000316:1-0"
    apply_unit_equip({}, UNIT_EQUIP_REQUEST, _json.dumps(mixed))
    case("strings naming different instances are refused", unit(p).get("vision_card") == before,
         repr(before), unit(p).get("vision_card"))

    out = apply_unit_equip({"X": 1}, "x5Unqg2d", body("1000000315", card="0-100000101"))
    case("a foreign request is untouched", out == {"X": 1}, "{'X': 1}", out)
    return 1 if failed else 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    print("ffbe_equip is a library module; the server entry point is server/capture_server.py")
    raise SystemExit(0)
