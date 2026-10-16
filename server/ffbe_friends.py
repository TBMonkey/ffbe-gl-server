"""ffbe_friends.py - the hand-curated friend roster: friends.yaml -> the two friend replies.

WHY. The party's 6th slot (the client's display word is "Companion") is filled from the friend
list and the reinforcement list, and both are served EMPTY today - so every battle offers the
client's own "Depart without companion" (APP-13). The operator wants to hand-curate a few friends
and their units WITHOUT editing code, so this module reads friends.yaml on every request: save the
file, and the very next request sees it, no restart, no client relaunch (hot reload).

SHAPE. A support-list row is a UNIT row. It carries the same token set player_state._unit_rows
emits for the player's own roster, plus the friend-only tokens m3Wghr1j (FriendId), 9qh17ZUf
(the friend's user name, which in THIS class means the row's own name) and n69quiCJ (emblem).
The unit's six stats are DERIVED from the shipped tables by player_state.unit_stats, so the YAML
carries no stat numbers that could drift.

WHERE THE TOKENS COME FROM: docs/notes/companion_support_unit.md (the class reads, the 18
required tokens of ReinforcementResponse, and the 11 of them the shipped F_GUEST_UNIT_MST row
already proves). Offline check without the client:
    python3 harness/reply_payload_replay.py --dump AJhnI37s
    python3 harness/reply_payload_replay.py --dump u7Id4bMg
"""
from __future__ import annotations

import os
import time

import yaml

from player_state import (STAT_TOKENS, TEAM_NEED_EXP, UNIT_KEY, beast_stats, unit_stats,
                          vision_card_mst)

HERE = os.path.dirname(os.path.abspath(__file__))
FRIENDS_FILE = os.environ.get("FRIENDS_FILE") or os.path.join(HERE, "friends.yaml")

REINFORCE_REPLY = "AJhnI37s"    # GetReinforcementInfoRequest
FRIENDLIST_REPLY = "u7Id4bMg"   # FriendListRequest
REINFORCE_TAG = "9Tp2fiyB"      # ReinforcementResponse, the NON-clearing tag (pzf5se6V empties it)
FRIENDINFO_TAG = "I53AVzSo"     # OpeFriendInfoResponse
FRIENDLIST_TAG = "56gJBnVh"     # FriendUnitInfoResponse
# ReinforcementVisionCardInfoResponse: the list the support screen resolves a companion's vision card
# through. Its tags are `2MKHk9cg` (factory flag 1 - NON-clearing, the one to use) and `9iW4xAFL`
# (flag 0 - clears everything first). Read 2026-10-15; the class's required tokens are the friend
# identity (m3Wghr1j FriendId, og2GHy49 UserUnitId, Y58ENcB4 tab) plus the card (5giCMUd2 id, gXb32xzQ
# row key) - every one of those is already computed for the support row.
VCINFO_TAG = "2MKHk9cg"
VCINFO_KEY_TOKEN = "gXb32xzQ"
VCINFO_LEVEL_TOKEN = "7wV3QZ80"
# F_VISION_CARD_MST's max-level column; the LEVEL must be >= 1 or the client indexes the grow row at
# level-1 = -1 and dereferences the null it gets back (the 2026-10-06 ornament lesson, same code path).
VC_MAX_LV_TOKEN = "aU3o2D4t"

# OpeFriendInfoResponse's two serving:required tokens. 0NVFk2fD is DelFriendInfo (comma-separated
# friend ids that were removed - none), H4zD2IQ5 is Wait, an int SupportSelectScene::updateEvent
# reads; -1 is authored to mean "no wait" so the client does not draw a refresh timer over a list
# we answer immediately.
WAIT_NO_WAIT = "-1"
DEL_FRIEND_INFO = ""

# FriendUnitInfoResponse reads only four OPTIONAL display tokens, so a friend-entry row is mostly
# identity. K3BV17gk (InfoType) is authored as 1; the two dates are display strings in the same
# format the other time fields use; X6jT6zrQ (FacebookUserId) is empty - nothing here has one.
FRIEND_LIST_INFO_TYPE = "1"
FRIEND_LIST_DATE_FMT = "%Y-%m-%d %H:%M:%S"

# THE FRIENDS-AREA ROWS (2026-10-15): a row here is the SAME CLASS FAMILY as the support row, which is
# measured, not assumed. `FriendUnitInfo` derives from `ReinforcementInfo` : `UnitInfoBase`; its
# response class defines only `readParamUnique` (the four display keys) and the INHERITED
# `ReinforcementResponse::readParam` still runs, so the row must carry unit identity. Serving only the
# four display keys left the row with no `3HriTp6B`, so the layout's `getUnitMst([row+0x4c])` returned
# nil and read the nil row at +0x12a (`ldrsh w19,[x22,#0x12a]`, si_addr 0x12a, captured 2026-10-15).
# The row is therefore built from `_reinforce_row` plus those keys. `FRIENDS_FRIENDLIST=0` turns the
# rows back off (the empty list is safe by measurement: the layout skips on a zero row count).
FRIEND_LIST_ENABLED = os.environ.get("FRIENDS_FRIENDLIST", "1") != "0"

_cache: dict = {"mtime": None, "friends": []}


def _fail(msg: str):
    raise ValueError("friends.yaml: " + msg)


def _entry(index: int, raw: dict) -> dict:
    """One YAML entry -> a validated dict. Loud: a malformed friend is an error, never a silent skip."""
    if not isinstance(raw, dict):
        _fail("entry #%d is not a mapping" % (index + 1))
    name = str(raw.get("display_name") or "").strip()
    if not name:
        _fail("entry #%d has no display_name" % (index + 1))
    try:
        friend_id = int(raw.get("friend_id"))
    except (TypeError, ValueError):
        _fail("entry %r has no numeric friend_id" % name)
    unit = raw.get("unit") or {}
    if not isinstance(unit, dict):
        _fail("entry %r: unit: must be a mapping" % name)
    try:
        master_id = int(unit.get("master_id"))
    except (TypeError, ValueError):
        _fail("entry %r has no numeric unit.master_id" % name)
    level = int(unit.get("level") or 1)
    if level < 1:
        _fail("entry %r: unit.level must be >= 1" % name)
    stats = unit_stats(master_id, level)
    if not any(stats):
        _fail("entry %r: unit.master_id %s is not a shipped unit (unit_stats returned all zeros)"
              % (name, master_id))
    abilities = [str(x) for x in (unit.get("abilities") or [])][:4]
    equipment = [str(x) for x in (unit.get("equipment") or [])][:6]
    return {
        "display_name": name,
        "friend_id": friend_id,
        "emblem_id": int(raw.get("emblem_id") or 0),
        "rank": int(raw.get("rank") or 1),
        "instance_id": str(raw.get("unit_instance_id") or (friend_id * 10 + 1)),
        "unit": {
            "master_id": master_id,
            "level": level,
            "lb_level": int(unit.get("lb_level") or 0),
            "lb_exp": int(unit.get("lb_exp") or 0),
            "vision_card": str(unit.get("vision_card") or ""),
            "esper": int(unit.get("esper") or 0),
            "esper_rarity": int(unit.get("esper_rarity") or 3),
            "esper_level": int(unit.get("esper_level") or 60),
            "abilities": abilities,
            "equipment": equipment,
        },
    }


def load_friends(path: str | None = None) -> list:
    """friends.yaml, re-read whenever its mtime changes. A missing file means no friends, not an error."""
    p = path or FRIENDS_FILE
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        _cache.update(mtime=None, friends=[])
        return []
    if _cache["mtime"] == mtime:
        return _cache["friends"]
    with open(p, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    if not isinstance(doc, dict):
        _fail("the file must be a mapping with a friends: list")
    rows = doc.get("friends") or []
    if not isinstance(rows, list):
        _fail("friends: must be a list")
    friends = [_entry(i, r) for i, r in enumerate(rows)]
    seen = set()
    for f in friends:
        if f["friend_id"] in seen:
            _fail("friend_id %d appears twice" % f["friend_id"])
        seen.add(f["friend_id"])
    _cache.update(mtime=mtime, friends=friends)
    return friends


def _reinforce_row(f: dict) -> dict:
    """One friend -> one ReinforcementResponse row (the support/companion list).

    TOKENS ARE THIS CLASS'S OWN, which is not the roster's set: the level is `10fNKueF` here
    (measured: the handler atoi's it and calls the row setter at vtable slot +0x260), NOT the
    roster's `7wV3QZ80`, and a row built from the roster set crashed the support-select layout
    with si_addr 0xe0 on 2026-10-13 (a null member at +0xe0). Tokens whose value has no shipped
    source are OMITTED rather than filled: readParam only writes tokens that are PRESENT, so an
    absent token leaves the client's own default, which is strictly safer than a guessed value.
    """
    u = f["unit"]
    row = {
        # friend-only tokens
        "m3Wghr1j": str(f["friend_id"]),        # FriendId
        "9qh17ZUf": f["display_name"],          # the row's user name
        "n69quiCJ": str(f["emblem_id"]),        # player emblem (badge) id
        # unit tokens, this class's names
        "og2GHy49": f["instance_id"],           # UserUnitId
        UNIT_KEY: str(u["master_id"]),          # 3HriTp6B, UnitId
        "10fNKueF": str(u["level"]),           # Level (NOT the roster's 7wV3QZ80)
        TEAM_NEED_EXP: "0",                     # Exp, display only
        "X9ABM7En": "0",                        # TotalExp, display only
        "a71oxzCH": str(u["lb_level"]),         # UnitLbLvl
        "EXf5G3Mk": str(u["lb_exp"]),           # UnitLb
        "80NUYFMJ": "@".join("%d-%s" % (i + 1, m) for i, m in enumerate(u["abilities"])) or "@".join(
            "%d-0" % (i + 1) for i in range(4)),
        "93Y6evuT": "@".join("%d-%s" % (i + 1, e) for i, e in enumerate(u["equipment"])) or "@".join(
            "%d-0" % (i + 1) for i in range(6)),
        Y58ENcB4_KEY: "1",                      # the selected friend tab, echoed as sent
        "L8d0fqpT": str(f["rank"]),      # TeamLv -- what the client shows as the friend Rank
        "9Tbns0eI": str(f["friend_id"]),        # commits the object (the row's owning user)
        # DELIBERATELY OMITTED unless the YAML sets them: 5giCMUd2/nQ1iAGf3 (vision card),
        # XZ4Kh7Ic (esper deck), umTJJ97w (hidden skill). Absent = the client's own default, which
        # is the row that served a clean companion; a value here is an experiment with the arm on.
    }
    if u["esper"]:
        # THIS CLASS'S OWN GRAMMAR, measured at handler 0x1975344: a comma CSV of NINE ints -
        # id,rarity,lv,hp,mp,atk,def,int,mnd - which the ctor reads with NO size check (a short or
        # colon-formatted string heap-over-reads and yields garbage id/rarity; that is what crashed the
        # list on 2026-10-15). So the six stats are DERIVED from the shipped F_BEAST_STATUS_MST at the
        # level asked for, exactly as the operator's own beast row is built.
        st = beast_stats(u["esper"], u["esper_rarity"], u["esper_level"])
        if st:
            vals = [u["esper"], u["esper_rarity"], u["esper_level"]] + list(st)
            row["XZ4Kh7Ic"] = ",".join(str(v) for v in vals)
    if u["vision_card"]:
        # THE KEY CONTRACT (read 2026-10-15): this string is split on "-" and only the text AFTER the
        # first dash is kept, so it must be "<ignored>-<key>" with <key> equal to the row's gXb32xzQ in
        # VCINFO_TAG. A bare id stores an empty string and resolves nothing - which is why the earlier
        # attempt drew no card at all.
        row["5giCMUd2"] = u["vision_card"]     # the vision-card id (int, StrToInt on the wire)
        row["nQ1iAGf3"] = "0-" + _vc_key(f, u["vision_card"])
    for tok, val in zip(STAT_TOKENS, unit_stats(u["master_id"], u["level"])):
        row[tok] = "%d-0-0-0-0" % val
    return row


Y58ENcB4_KEY = "Y58ENcB4"


def _vc_key(f: dict, vc_id) -> str:
    """The one string that ties a support row to a VC-info row. Any unique value works as long as both
    sides agree - the friend id keeps it unique across friends without colliding with the player's own
    card keys (which are user_vc_ids)."""
    return "%s-%s" % (f["friend_id"], vc_id)


def _vc_info_row(f: dict, vc_id) -> dict:
    """One friend -> one ReinforcementVisionCardInfoResponse row (the card the companion shows).

    Level: the card's own max level from F_VISION_CARD_MST, never 0 - see VC_MAX_LV_TOKEN above.
    """
    mst = vision_card_mst(vc_id) or {}
    level = int(mst.get(VC_MAX_LV_TOKEN) or 1)
    return {
        "5giCMUd2": str(vc_id),                 # the vision-card id
        VCINFO_KEY_TOKEN: _vc_key(f, vc_id),    # the row key the support row points at
        "m3Wghr1j": str(f["friend_id"]),        # FriendId
        "og2GHy49": f["instance_id"],           # UserUnitId
        Y58ENcB4_KEY: "1",                      # the friend tab, echoed
        VCINFO_LEVEL_TOKEN: str(max(1, level)), # Level (>= 1, mandatory)
        "B6H34Mea": "0",                        # Exp
        "X9ABM7En": "0",                        # TotalExp
    }


def _friend_list_row(f: dict, now: str) -> dict:
    """One friend -> one FriendUnitInfoResponse row (the friends area).

    The support row IS the base: same reader (see the FRIEND_LIST_ENABLED note), so the token set that
    already renders in battle is exactly what this screen needs, and the four display keys are
    overlaid. `w1JG7i9j` stays unserved on purpose - it is the only writer of the row's +0x4d8, and 0
    keeps the reader's refine branch off.
    """
    row = _reinforce_row(f)
    row.update({        "K3BV17gk": FRIEND_LIST_INFO_TYPE,      # InfoType
        "4p8h7i9g": now,                        # StateChangeDate
        "G8mtXbf6": now,                        # LeaderUpdateDate
        "X6jT6zrQ": "",                         # FacebookUserId
    })
    return row


def serve_friends(spec, reply_id=None):
    """The pipeline step: fill the two friend replies from friends.yaml (no-op for any other reply).

    GATED on the reply id, the apply_carry_edit precedent: when that step was ungated the golden
    replay caught it growing nine unrelated replies. With no friends.yaml and no rows, both replies
    keep exactly the shape they had (an empty list), so this step cannot regress the empty case.
    """
    friends = load_friends()
    if not friends:
        return spec
    if reply_id == REINFORCE_REPLY and isinstance(spec, dict):
        spec[REINFORCE_TAG] = [_reinforce_row(f) for f in friends]
        # The card list the support screen resolves through. Its own tag, NON-clearing (see VCINFO_TAG).
        cards = [_vc_info_row(f, f["unit"]["vision_card"]) for f in friends if f["unit"]["vision_card"]]
        if cards:
            spec[VCINFO_TAG] = cards
        # pzf5se6V is the CLEARING variant - deliberately left alone (it empties the container).
        group = spec.get(FRIENDINFO_TAG)
        if isinstance(group, list):
            group.insert(0, {"0NVFk2fD": DEL_FRIEND_INFO, "H4zD2IQ5": WAIT_NO_WAIT})
        else:
            spec[FRIENDINFO_TAG] = [{"0NVFk2fD": DEL_FRIEND_INFO, "H4zD2IQ5": WAIT_NO_WAIT}]
    elif reply_id == FRIENDLIST_REPLY and isinstance(spec, dict) and FRIEND_LIST_ENABLED:
        now = time.strftime(FRIEND_LIST_DATE_FMT)
        spec[FRIENDLIST_TAG] = [_friend_list_row(f, now) for f in friends]
    return spec
