#!/usr/bin/env python3
"""ffbe_vision.py — the VISION CARD family (SW-130): favourite, mix, and master-card release.

`VisionCardDisplay` (the HOME ornament) already lives in `ffbe_player.apply_vision_card_display`; this
module is the rest of the family. The shipped surface, measured 2026-11-04 (see `docs/dev/` plan note):

  * `F_VISION_CARD_MST`, 339 rows — `5giCMUd2` id, `aU3o2D4t` max level (1/2/10), `0W7g6YXS` SERIES,
    `uk6Wh8mw` exp-pattern group, `N85uJT1d` the card's MIX EXP VALUE (e.g. 400);
  * `F_VISION_CARD_EXP_PATTERN_MST`, 23 rows — `(series, level, exp)`;
  * `F_DEFINE_MST` `DIFF_VISION_CARD_SERIES_MIX_EXP_RATE` = **200** (per-mille): fodder of the SAME series
    is worth full exp, a DIFFERENT series **20%**.

THIS MODULE STARTS WITH THE FAVOURITE, which needs no economy at all.
"""
from __future__ import annotations

import player_state as ps
from ffbe_state import State

# --- favourite ----------------------------------------------------------------------------------------
FAVORITE_REPLY = "VTbX14dD"        # VisionCardFavoriteRequest
FAVORITE_GROUP = "4I71fGzq"        # OpeVisionCardFavoriteResponse
FAVORITE_ADD = "E0zptv3J"          # comma CSV of user-vision-card keys to add
FAVORITE_DEL = "N4ikBXn9"          # comma CSV of user-vision-card keys to delete
FAVORITE_TOKEN = "8zE6w9RP"        # UserVisionCardInfo vtable +0x20 = setFavoriteFlg(bool)

# --- mix ----------------------------------------------------------------------------------------------
MIX_REPLY = "5J14x3Qz"             # VisionCardMixRequest
MIX_GROUP = "oh5yce1m"             # VisionCardMixResponse
MIX_BASE = "gXb32xzQ"              # get/setBaseUserVisionCardId   -> setUserCardId
MIX_FODDER = "iUeA3z71"            # get/setElemUserVisonCardIdCsv -> setElemCardInfo  (their typo)
MIX_GIL = "7UR4J2SE"               # setUseGil(int)
MIX_EXP = "B6H34Mea"               # setGetExp(int)
MIX_SUCCESS = "1dT7ha9H"           # setSuccessType(int)
MIX_BOOK = "Wpro0aE7"              # setRecipeBookInfo(string)

# --- master-card release ----------------------------------------------------------------------------
RELEASE_REPLY = "23fbcXt4"        # MasterCardReleaseRequest
RELEASE_GROUP = "w7J3XBtq"        # the request body group (createBody 0x199978c)
RELEASE_UNIT = "og2GHy49"         # the unit INSTANCE id (the same key the roster/equip rows use)

# `F_DEFINE_MST` REMOVE_MASTERCARD_LAPIS = "500" ("マスターカード解除（ラピス）"). SHIPPED DATA, read out of
# /home/thetb/projects/ffbe/mst_decoded/F_DEFINE_MST.ndjson -- a transcription with its source, like the
# recovery rates above it, because no helper reads define rows at request time.
REMOVE_MASTERCARD_LAPIS = 500
MASTER_CARD_ITEM_ID = "240000100"     # F_ITEM_MST マスターカード: the slot unlock this op gives back

VISION_REPLIES = frozenset({FAVORITE_REPLY, MIX_REPLY, RELEASE_REPLY})


def _rows() -> list:
    return State.player.data.setdefault("vision_cards", [])


def _key(row: dict) -> str:
    """The client's own row key: `user_vc_id`, falling back to the master id (the same rule
    `player_state.vision_card_rows` applies when it builds `gXb32xzQ`)."""
    return str(row.get("user_vc_id") or row.get("vc_id") or "")


def _csv(value) -> list:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def favourite(spec: dict, plaintext) -> dict:
    """`VisionCardFavoriteRequest` (`VTbX14dD`) -- the star on a vision card.

    THE FLAG IS `8zE6w9RP`, and here it is unambiguous: our field map spells it
    `UserVisionCardInfo vtable slot 0x20 = setFavoriteFlg(bool)`. (It also appears in the FRIEND request
    body, which is why the friend slice recorded it as ambiguous — the same token on two classes,
    idiom #26. This class settles it for cards.)

    The reply is `OpeVisionCardFavoriteResponse` with the SAME two CSVs, which the client applies to its
    own `UserVisionCardInfoList` — so this reply deliberately does NOT carry the card list (that group is
    `4hPYneL3`, non-destructive, and the projection adds it when the state owns cards).

    We echo only the keys we actually CHANGED: a key that is not owned cannot be favourited, and echoing
    it would tell the client a mutation happened that did not (idiom #42 — report what was credited).
    """
    from ffbe_player import _request_json
    body = _request_json(plaintext) or {}
    rows = body.get(FAVORITE_GROUP)
    node = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
    out = dict(spec) if isinstance(spec, dict) else {}
    if not node:
        print("vision-card: favourite carried no %s row; nothing changed" % (FAVORITE_GROUP,), flush=True)
        return out
    wanted_add = _csv(node.get(FAVORITE_ADD))
    wanted_del = _csv(node.get(FAVORITE_DEL))
    owned = {_key(vc): vc for vc in _rows()}
    applied_add = [k for k in wanted_add if k in owned]
    applied_del = [k for k in wanted_del if k in owned]
    unknown = [k for k in wanted_add + wanted_del if k not in owned]
    if unknown:
        print("vision-card: favourite ignored %d unowned key(s): %s"
              % (len(unknown), ", ".join(unknown[:6])), flush=True)
    if (applied_add or applied_del) and State.player._mutate():
        for key in applied_add:
            owned[key]["favorite"] = True
        for key in applied_del:
            owned[key]["favorite"] = False
        State.player.dirty = True
    print("vision-card: favourite +%s -%s (owned %d)"
          % (",".join(applied_add) or "-", ",".join(applied_del) or "-", len(owned)), flush=True)
    out[FAVORITE_GROUP] = [{FAVORITE_ADD: ",".join(applied_add), FAVORITE_DEL: ",".join(applied_del)}]
    return out


def _exp_pattern(group) -> dict:
    """`F_VISION_CARD_EXP_PATTERN_MST`: (group, level) -> the exp the card needs to BE that level.

    The join is by the card's own pattern group: `F_VISION_CARD_MST.uk6Wh8mw` ({1,100,1002}) is the
    pattern table's key column `0W7g6YXS` (which also carries a 110 the card table never uses). This is
    the SHIPPED ladder -- `VisionCardUtils::getNeedCardExp(int, int)` (0x1a1e608) is its client side.
    """
    out = {}
    for row in ps._load_ndjson("F_VISION_CARD_EXP_PATTERN_MST.ndjson").get("__rows__", []):
        if str(row.get("0W7g6YXS")) == str(group):
            try:
                out[int(row["7wV3QZ80"])] = int(row["93fnYUJG"])
            except (KeyError, TypeError, ValueError):
                continue
    return out


def mix(spec: dict, plaintext) -> dict:
    """`VisionCardMixRequest` (`5J14x3Qz`) -- feed fodder cards into a base card.

    THE CLIENT REPORTS THE ECONOMY AND WE APPLY IT, deliberately, and this is the one place in the family
    where that is the right shape: the request carries `setUseGil` and `setGetExp` -- the client's OWN
    computed totals (see the request's exported accessors, `VisionCardMixRequest::getUseGil` /
    `getGetExp`) -- and `harness/request_api_map.json` records exactly that. WHAT IS NOT VERIFIED is the
    column behind the per-card mix exp (`VisionCardMst::setAdjustMixExp` is inlined, so its token did not
    resolve in this pass) and the gil formula, which no shipped table obviously carries. So the numbers
    are ACCEPTED AND LOGGED rather than recomputed, and every INVARIANT is checked against shipped data
    or our own state:

      * the base card must be owned and be a legal base (`VisionCardUtils::isEnableMixBase`, 0x1a1e13c);
      * every fodder must be owned, must not be the base, and must be unique;
      * the gil must be affordable, and is spent;
      * the resulting level is walked on the SHIPPED exp ladder and capped at the card's own max level
        (`F_VISION_CARD_MST.aU3o2D4t`), so a client-reported exp cannot push a card past its ceiling.

    The fodder rows are REMOVED -- that is the mechanic. A mix that removes nothing would be a free
    level-up on every retry, so the removal is the guard.
    """
    from ffbe_player import _request_json
    body = _request_json(plaintext) or {}
    rows = body.get(MIX_GROUP)
    node = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
    out = dict(spec) if isinstance(spec, dict) else {}
    if not node:
        print("vision-card mix: request carried no %s row; nothing changed" % (MIX_GROUP,), flush=True)
        return out
    player = State.player
    base_key = str(node.get(MIX_BASE) or "")
    fodder_keys = [k.strip() for k in str(node.get(MIX_FODDER) or "").split(",") if k.strip()]
    try:
        gil = int(node.get(MIX_GIL) or 0)
        exp = int(node.get(MIX_EXP) or 0)
    except (TypeError, ValueError):
        print("vision-card mix: REFUSED -- non-numeric gil/exp in the request", flush=True)
        return out
    owned = {_key(vc): vc for vc in _rows()}
    base = owned.get(base_key)
    if base is None:
        print("vision-card mix: REFUSED -- base card %r is not owned" % (base_key,), flush=True)
        return out
    if base_key in fodder_keys or len(set(fodder_keys)) != len(fodder_keys):
        print("vision-card mix: REFUSED -- the base card or a duplicate sits in the fodder list", flush=True)
        return out
    missing = [k for k in fodder_keys if k not in owned]
    if missing:
        print("vision-card mix: REFUSED -- fodder not owned: %s" % (", ".join(missing[:6])), flush=True)
        return out
    if int(player.currencies.get("gil", 0)) < gil:
        print("vision-card mix: REFUSED -- %d gil needed, %d held"
              % (gil, int(player.currencies.get("gil", 0))), flush=True)
        return out
    master = ps.vision_card_mst(base.get("vc_id")) or {}
    try:
        max_level = max(1, int(master.get("aU3o2D4t") or 1))
    except (TypeError, ValueError):
        max_level = 1
    pattern = _exp_pattern(master.get("uk6Wh8mw"))
    level = max(1, int(base.get("level") or 1))
    total = int(base.get("total_exp") or 0) + exp
    rest = int(base.get("exp") or 0) + exp
    levelled = 0
    while level < max_level:
        need = pattern.get(level + 1)
        if not need or rest < need:
            break
        rest -= need
        level += 1
        levelled += 1
    if not player._mutate():
        return out
    if gil:
        player.spend_currency("gil", gil)
    for key in fodder_keys:
        _rows().remove(owned[key])
    base.update(level=level, exp=rest, total_exp=total)
    player.dirty = True
    print("vision-card mix: base %r +%d exp, %d gil, %d fodder -> level %d/%d%s (client-reported economy)"
          % (base_key, exp, gil, len(fodder_keys), level, max_level,
             ", +%d level(s)" % levelled if levelled else ""), flush=True)
    out[MIX_GROUP] = [{MIX_BASE: base_key, MIX_FODDER: ",".join(fodder_keys),
                       MIX_GIL: str(gil), MIX_EXP: str(exp), MIX_SUCCESS: "1", MIX_BOOK: ""}]
    return out


def release(spec: dict, plaintext) -> dict:
    """`MasterCardReleaseRequest` (`23fbcXt4`) -- take the Master Card back off a unit's slot.

    THE WIRE: one row `{w7J3XBtq: [{og2GHy49: <unit instance id>}]}`. `MasterCardReleaseRequest::createBody`
    (`0x199978c`) is `addGroup("w7J3XBtq")` + one node + `og2GHy49`, and the class ends there: there is no
    response class in the factory and no group to parse, so the reply's real job is the STATE -- the roster
    row's flag (`T9Apq5fS` -> `UserUnitInfo+0x320`) and the refunded item in the bag.

    THE ECONOMY is `F_DEFINE_MST` `REMOVE_MASTERCARD_LAPIS` = **500** lapis, and the refund is the item
    itself (`F_ITEM_MST` 240000100): the Master Card IS the unlock, so releasing the slot must hand it back
    or the 500 lapis would buy nothing. Both are charged/credited in one commit, and the requests are
    validated as a set first (no partial refund if the account cannot pay for all of them).

    REFUSALS, each with a reason a reader can act on:
      * the unit is not on the roster;
      * the unit has no Master Card (`master_card` false) -- nothing to release;
      * a vision card is still EQUIPPED on the unit. The client's own flow unties it first --
        `UnitEquipUtil::removeVisionCardFromAllPartyType` (`0x1ab1ac0`) exists precisely to send that
        `UnitEquipRequest` -- so releasing a loaded slot would silently destroy the card;
      * the account cannot pay 500 lapis per release.
    """
    from ffbe_player import _request_json
    body = _request_json(plaintext) or {}
    rows = body.get(RELEASE_GROUP)
    out = dict(spec) if isinstance(spec, dict) else {}
    if not isinstance(rows, list) or not rows:
        print("vision-card release: request carried no %s row; nothing changed" % RELEASE_GROUP, flush=True)
        return out
    player = State.player
    if player.mode == "frozen":
        print("vision-card release: frozen state policy -- nothing changed", flush=True)
        return out
    units = {str(u.get("instance_id")): u for u in player.data.get("roster") or []}
    targets: list = []
    for row in rows:
        instance = str((row or {}).get(RELEASE_UNIT) or "") if isinstance(row, dict) else ""
        unit = units.get(instance)
        if unit is None:
            print("vision-card release: refused -- unit %r is not in the roster" % (instance,), flush=True)
            continue
        if not unit.get("master_card"):
            print("vision-card release: refused -- unit %s has no Master Card" % instance, flush=True)
            continue
        if str(unit.get("vision_card") or ""):
            print("vision-card release: refused -- unit %s still has vision card %r equipped (remove it first)"
                  % (instance, unit.get("vision_card")), flush=True)
            continue
        targets.append(instance)
    if not targets:
        print("vision-card release: nothing releasable", flush=True)
        return out
    total = REMOVE_MASTERCARD_LAPIS * len(targets)
    free = int(player.currencies.get("lapis_free", 0) or 0)
    paid = int(player.currencies.get("lapis_paid", 0) or 0)
    if free + paid < total:
        print("vision-card release: REFUSED -- %d lapis needed for %d release(s), %d held"
              % (total, len(targets), free + paid), flush=True)
        return out
    if not player._mutate():
        return out
    for instance in targets:
        player.spend_lapis(REMOVE_MASTERCARD_LAPIS)
        player.grant_item(ps.BAG_ITEM_TYPE, MASTER_CARD_ITEM_ID, 1)
        units[instance]["master_card"] = False
    player.dirty = True
    print("vision-card release: %s -> %d Master Card item(s) refunded, %d lapis spent"
          % (", ".join(targets), len(targets), total), flush=True)
    # The release has NO reply class, so the groups that carry its effects are served explicitly: the roster
    # row (the flag this op cleared) and the bag + lapis (the refund and the price). Each is the COMPLETE
    # list the projection would serve, because both containers clear before they rebuild.
    groups = player.groups()
    for tag in ("B71MekS8", "0dNXQa3R", "J3pAG0I5"):
        if tag in groups:
            out[tag] = groups[tag]
    return out


def serve_vision(spec, reply_id, plaintext):
    """The pipeline step. No-op for any reply outside the vision-card family."""
    if reply_id not in VISION_REPLIES or State.player is None:
        return spec
    if reply_id == FAVORITE_REPLY:
        return favourite(spec, plaintext)
    if reply_id == MIX_REPLY:
        return mix(spec, plaintext)
    if reply_id == RELEASE_REPLY:
        return release(spec, plaintext)
    return spec


# --- selftest ---------------------------------------------------------------------------------------
def _selftest() -> int:
    """The RELEASE matrix (SW-130 slice B) and the round trip with the equip path.

    The round trip is the case worth having: equip spends the Master Card item and sets the flag; release
    must give the item back, clear the flag and charge exactly 500 lapis -- one loop, both modules, no
    hand-written state in between. A refusal that still changes something is the failure these pin.
    """
    import json as _json
    import ffbe_equip as equip
    from ffbe_state import State as _State
    failed = []

    def fresh(card="", flag=True, lapis=500):
        player = ps.PlayerState.load("/nonexistent", slot="visionselftest")
        _State.player = player
        player.grant_unit("100000315", instance_id="1000000315")
        unit = next(u for u in player.data["roster"] if str(u["instance_id"]) == "1000000315")
        unit["master_card"] = flag
        unit["vision_card"] = card
        player.currencies["lapis_free"] = lapis
        player.currencies["lapis_paid"] = 0
        return player, unit

    def body(instance="1000000315"):
        return _json.dumps({RELEASE_GROUP: [{RELEASE_UNIT: instance}]})

    def case(name, ok, want, got):
        print(f"release {'ok  ' if ok else 'FAIL'} {name} (want {want}, got {got})")
        if not ok:
            failed.append(name)

    player, unit = fresh()
    out = serve_vision({}, RELEASE_REPLY, body())
    case("one release refunds the item and charges the shipped 500 lapis",
         player.item_count(ps.BAG_ITEM_TYPE, MASTER_CARD_ITEM_ID) == 1
         and player.currencies["lapis_free"] == 0 and unit["master_card"] is False,
         "item 1, lapis 0, flag False",
         "item %d, lapis %d, flag %r" % (player.item_count(ps.BAG_ITEM_TYPE, MASTER_CARD_ITEM_ID),
                                         player.currencies["lapis_free"], unit["master_card"]))
    case("the reply carries the groups that show the release",
         all(tag in out for tag in ("B71MekS8", "0dNXQa3R", "J3pAG0I5")), "roster+bag+lapis",
         sorted(k for k in out))

    player, unit = fresh(flag=False)
    serve_vision({}, RELEASE_REPLY, body())
    case("a unit with no Master Card is refused", player.item_count(ps.BAG_ITEM_TYPE, MASTER_CARD_ITEM_ID) == 0
         and player.currencies["lapis_free"] == 500, "item 0, lapis 500",
         "item %d, lapis %d" % (player.item_count(ps.BAG_ITEM_TYPE, MASTER_CARD_ITEM_ID),
                                player.currencies["lapis_free"]))

    player, unit = fresh(card="100000101")
    serve_vision({}, RELEASE_REPLY, body())
    case("a loaded slot is refused (the client unties the card first)",
         unit["master_card"] is True and player.item_count(ps.BAG_ITEM_TYPE, MASTER_CARD_ITEM_ID) == 0,
         "flag True, item 0", "flag %r, item %d" % (unit["master_card"],
                                                    player.item_count(ps.BAG_ITEM_TYPE, MASTER_CARD_ITEM_ID)))

    player, unit = fresh(lapis=499)
    serve_vision({}, RELEASE_REPLY, body())
    case("499 lapis cannot buy a 500-lapis release",
         unit["master_card"] is True and player.item_count(ps.BAG_ITEM_TYPE, MASTER_CARD_ITEM_ID) == 0
         and player.currencies["lapis_free"] == 499, "flag True, item 0, lapis 499",
         "flag %r, item %d, lapis %d" % (unit["master_card"],
                                         player.item_count(ps.BAG_ITEM_TYPE, MASTER_CARD_ITEM_ID),
                                         player.currencies["lapis_free"]))

    # THE ROUND TRIP: buy nothing, grant the item, equip it, release it, get it back.
    player, unit = fresh(flag=False)
    player.grant_item(ps.BAG_ITEM_TYPE, MASTER_CARD_ITEM_ID, 1)
    player.data["vision_cards"].append({"vc_id": "100000101", "user_vc_id": "100000101", "level": 1})
    equip_body = _json.dumps({equip.EQUIP_GROUP: [{
        equip.TOK_EQUIP: "1000000315:1-0@2-0@3-0@4-0@5-0@6-0",
        equip.TOK_MATERIA: "1000000315:1-0",
        equip.TOK_VISION_CARD: "1000000315:0-100000101",
        equip.TOK_SKIN: "1000000315:0", equip.TOK_PARTY_TYPE: "0", equip.TOK_DECK: "0"}]})
    equip.apply_unit_equip({}, equip.UNIT_EQUIP_REQUEST, equip_body)
    equipped = (unit["vision_card"] == "100000101" and unit["master_card"] is True
                and player.item_count(ps.BAG_ITEM_TYPE, MASTER_CARD_ITEM_ID) == 0)
    # ...the release refuses while the card is on, so untie it the way the client does (an equip request).
    equip.apply_unit_equip({}, equip.UNIT_EQUIP_REQUEST,
                           equip_body.replace("0-100000101", "0-0"))
    serve_vision({}, RELEASE_REPLY, body())
    case("equip -> untie -> release returns the item and the 500 lapis",
         equipped and unit["vision_card"] == "" and unit["master_card"] is False
         and player.item_count(ps.BAG_ITEM_TYPE, MASTER_CARD_ITEM_ID) == 1
         and player.currencies["lapis_free"] == 0,
         "equipped True, slot '', flag False, item 1, lapis 0",
         "equipped %r, slot %r, flag %r, item %d, lapis %d"
         % (equipped, unit["vision_card"], unit["master_card"],
            player.item_count(ps.BAG_ITEM_TYPE, MASTER_CARD_ITEM_ID), player.currencies["lapis_free"]))
    return 1 if failed else 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    print("ffbe_vision is a library module; the server entry point is server/capture_server.py")
    raise SystemExit(0)
