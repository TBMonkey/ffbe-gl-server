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

import json
import os
import time

import yaml

from player_state import (STAT_TOKENS, TEAM_NEED_EXP, UNIT_KEY, beast_stats, limit_burst, unit_stats,
                          vision_card_mst)

HERE = os.path.dirname(os.path.abspath(__file__))
FRIENDS_FILE = os.environ.get("FRIENDS_FILE") or os.path.join(HERE, "friends.yaml")

REINFORCE_REPLY = "AJhnI37s"    # GetReinforcementInfoRequest
FRIENDLIST_REPLY = "u7Id4bMg"   # FriendListRequest
# The four friend-page actions the recorded corpus never answered (SW-128). Every one of them is a
# REQUEST id, so the step has to parse the body; the reply class is identified by the TAG map, not by a
# guess: 2vuhy0Ex and f6yGgCj2 are both FriendUnitInfoResponse (the same class 56gJBnVh already serves),
# and 2K05IxtX is FriendSearchResultResponse, whose token list our field map could NOT locate - so that
# one is answered with an empty list rather than with a row shape we would be inventing.
FRIEND_DETAIL_REPLY = "7kG0JAvE"    # FriendDetailRequest -> 2vuhy0Ex
FRIEND_SEARCH_REPLY = "3siZRSU4"    # FriendSearchRequest -> 2K05IxtX
FRIEND_SUGGEST_REPLY = "iAs67PhJ"   # FriendSuggestRequest -> f6yGgCj2
FRIEND_FAVORITE_REPLY = "1oE3Fwn4"  # FriendFavoriteRequest
# The four remaining friend actions. All four answer `I53AVzSo` (OpeFriendInfoResponse) - our request map
# says every one of the six sends the same single friend group and the reply class is the same shape.
FRIEND_AGREE_REPLY = "kx13SLUY"     # FriendAgreeRequest
FRIEND_DELETE_REPLY = "a2d6omAy"    # FriendDeleteRequest
FRIEND_REFUSE_REPLY = "1nbWRV9w"    # FriendRefuseRequest
FRIEND_REQUEST_REPLY = "j0A5vQd8"   # FriendRequest
DETAIL_TAG = "2vuhy0Ex"
SEARCH_TAG = "2K05IxtX"
SUGGEST_TAG = "f6yGgCj2"
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
            "lb_level": max(int(unit.get("lb_level") or 0), 1 if limit_burst(master_id) else 0),
            "lb_exp": int(unit.get("lb_exp") or 0),
            "vision_card": str(unit.get("vision_card") or ""),
            "esper": int(unit.get("esper") or 0),
            "esper_rarity": int(unit.get("esper_rarity") or 3),
            "esper_level": int(unit.get("esper_level") or 60),
            "abilities": abilities,
            "equipment": equipment,
        },
    }


def _suppressed_ids() -> set:
    """Friend ids this SAVE has removed (see `local_friend`'s DELETE branch).

    Keyed by friend id, value truthy. The roster YAML is the shipped truth and a request must never edit
    it, so "deleted" is per-save state; a new save starts with the whole roster again.
    """
    player = _player_state()
    if player is None:
        return set()
    removed = player.data.get("exvius_friends", {}).get("removed", {})
    if not isinstance(removed, dict):
        return set()
    return {str(k) for k, v in removed.items() if v}


def _without_removed(friends: list) -> list:
    """The roster minus this save's removals. Cheap enough to run per call: the roster is a handful."""
    gone = _suppressed_ids()
    if not gone:
        return friends
    return [f for f in friends if str(f["friend_id"]) not in gone]


def load_friends(path: str | None = None) -> list:
    """friends.yaml, re-read whenever its mtime changes. A missing file means no friends, not an error.

    REMOVED COMPANIONS ARE FILTERED HERE, on every call, and NOT in the cache: the roster file is the
    shipped truth and a request must never edit it, so a `FriendDeleteRequest` is per-SAVE state
    (`exivius_friends.removed`). Filtering in `load_friends` means one place decides what the roster is,
    and both consumers (the support list and the friends area) honour a delete without a second list.
    """
    p = path or FRIENDS_FILE
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        _cache.update(mtime=None, friends=[])
        return []
    if _cache["mtime"] == mtime:
        return _without_removed(_cache["friends"])
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
    return _without_removed(friends)


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


def _friend_request_node(plaintext):
    """The client's own row for a friend action, or `{}` — and never silently.

    `ffbe_gacha._request_body` returns `{}` on an unparseable body, which is right for a transform that
    only reads a hint and wrong here: a friend action with no target must be visible in the log, because
    "nothing happened" and "we could not read the request" are byte-identical on the wire (idiom #47).
    """
    if not plaintext:
        print("friends: action request carried no body")
        return {}
    try:
        body = json.JSONDecoder().raw_decode(plaintext)[0]
    except Exception as exc:  # noqa: BLE001 - report, never default silently
        print("friends: action body did not parse (%r) -- reply left unchanged" % (exc,))
        return {}
    rows = body.get(FRIENDINFO_TAG) if isinstance(body, dict) else None
    row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
    if not row:
        print("friends: action carried no %s row" % FRIENDINFO_TAG)
    return row


def _player_state():
    """The live player state, or None — imported lazily so this module stays importable on its own."""
    from ffbe_state import State
    return State.player


def _friend_bucket(name):
    """One per-save dictionary under `data['exvius_friends']`, created on first use."""
    player = _player_state()
    if player is None:
        return None
    return player.data.setdefault("exvius_friends", {}).setdefault(name, {})


def favourite_map() -> dict:
    """friend id -> the client's own `8zE6w9RP` value, for the rows this reply renders.

    `8zE6w9RP` DOES ride the friend request group (`0b6e9cwr`, `8zE6w9RP`, `m3Wghr1j` in
    `harness/request_api_map.json`'s `body_keys` for all six friend actions), so the flag is a
    FRIEND field as well as the vision card's `setFavoriteFlg` — the same token on two classes
    (idiom #26). What is still unproven is whether the CLIENT reads it back off a friend row; we echo
    the value it sent, which is type-safe by construction, and a live run settles the rest.
    """
    saved = (_player_state().data.get("exvius_friends", {}).get("favorites", {})
             if _player_state() is not None else {})
    return saved if isinstance(saved, dict) else {}


def local_friend(spec, reply_id, plaintext):
    """Answer the friend family from the local roster (SW-128).

    Our roster is a YAML file, so there is no online population to search, suggest from, or send an
    invitation to: the truthful answer there is an EMPTY list, not a fabricated user. Only DETAIL has a
    real row to serve, and it is the row the friends area already renders — the tag map puts `2vuhy0Ex`
    and `56gJBnVh` on the SAME class (FriendUnitInfoResponse), so the shape is shared, and the requested
    InfoType is carried through because the page's tab selector reads it.

    AGREE / REFUSE / REQUEST all answer `OpeFriendInfoResponse` (`I53AVzSo`) with no deletions, because
    nothing can be pending in an offline save: the roster is a file and no one can send to it. That is
    the same info row `serve_friends` already emits for the support list.

    DELETE is the one action with a real effect, and it is BOTH halves: the reply names the id in
    `0NVFk2fD`, which `OpeFriendInfoResponse::onReadFinished` (`0x193d0e8`) splits and removes from the
    client's own `FriendUnitInfoList` (`0x1328180`, unresolved ids skipped —
    `docs/notes/home_screen_payloads.md`), AND the id is recorded under `exivius_friends.removed` so the
    next list reply does not re-add it. Only the second half survives a restart: the roster is shipped
    YAML and we must not edit it from a request, so "deleted" is per-save state.
    """
    out = dict(spec) if isinstance(spec, dict) else {}
    row = _friend_request_node(plaintext)
    target = str(row.get("m3Wghr1j") or "")           # FriendId
    if reply_id == FRIEND_FAVORITE_REPLY:
        flag = row.get("8zE6w9RP")
        player = _player_state()
        if player is not None and target and str(flag) in ("0", "1"):
            favorites = _friend_bucket("favorites")
            if player._mutate() and favorites is not None:
                favorites[target] = str(flag)
                player.dirty = True
        print("friends: favorite target=%r 8zE6w9RP=%r -> %s"
              % (target, flag,
                 "recorded; echoed on the next row" if str(flag) in ("0", "1") else
                 "NOT recorded (flag outside 0/1) -- the client's value is not a bool here"))
        return out
    if reply_id == FRIEND_SEARCH_REPLY:
        out[SEARCH_TAG] = []
        print("friends: search for %r -> empty (no online population; FriendSearchResultResponse "
              "has no located tokens in our field map)" % (target,))
        return out
    if reply_id == FRIEND_SUGGEST_REPLY:
        out[SUGGEST_TAG] = []
        print("friends: suggest for %r -> empty (no online senders)" % (target,))
        return out
    if reply_id in (FRIEND_AGREE_REPLY, FRIEND_REFUSE_REPLY, FRIEND_REQUEST_REPLY):
        out[FRIENDINFO_TAG] = [{"0NVFk2fD": DEL_FRIEND_INFO, "H4zD2IQ5": WAIT_NO_WAIT}]
        act = {FRIEND_AGREE_REPLY: "agree", FRIEND_REFUSE_REPLY: "refuse",
               FRIEND_REQUEST_REPLY: "request"}[reply_id]
        print("friends: %s %r -> no-op (offline save: no pending requests exist to accept, refuse or "
              "send; the roster is friends.yaml)" % (act, target))
        return out
    if reply_id == FRIEND_DELETE_REPLY:
        player = _player_state()
        known = {str(f["friend_id"]) for f in load_friends()}
        removed = _friend_bucket("removed")
        if player is not None and target and removed is not None:
            if player._mutate():
                removed[target] = True
                favorites = _friend_bucket("favorites")
                if favorites is not None:
                    favorites.pop(target, None)
                player.dirty = True
        # Name it in the reply either way: onReadFinished removes it from the CLIENT's list, and an
        # unknown id is skipped there, so a stale id is harmless while omitting a known one is not.
        out[FRIENDINFO_TAG] = [{"0NVFk2fD": target, "H4zD2IQ5": WAIT_NO_WAIT}]
        print("friends: delete %r -> client list removes it (%s); suppression recorded=%s"
              % (target, "in friends.yaml" if target in known else "NOT in friends.yaml",
                 removed is not None))
        return out
    # FRIEND_DETAIL_REPLY: the one action with a real answer.
    favs = favourite_map()
    known = {str(f["friend_id"]): f for f in load_friends()}
    if target in known:
        category = str(row.get("K3BV17gk") or FRIEND_LIST_INFO_TYPE)   # InfoType
        if category not in ("1", "2", "3", "4", "5"):
            print("friends: detail category %r is outside the client's 1..5 set; serving %s"
                  % (category, FRIEND_LIST_INFO_TYPE))
            category = FRIEND_LIST_INFO_TYPE
        detail = _friend_list_row(known[target], time.strftime(FRIEND_LIST_DATE_FMT))
        detail["K3BV17gk"] = category
        if target in favs:
            detail["8zE6w9RP"] = str(favs[target])
        out[DETAIL_TAG] = [detail]
        print("friends: detail %r category=%s -> 1 row" % (target, category))

    else:
        out[DETAIL_TAG] = []
        print("friends: detail %r -> empty (not in friends.yaml)" % (target,))
    return out


def serve_friends(spec, reply_id=None, plaintext=None):
    """The pipeline step: fill the two friend replies from friends.yaml (no-op for any other reply).

    GATED on the reply id, the apply_carry_edit precedent: when that step was ungated the golden
    replay caught it growing nine unrelated replies. With no friends.yaml and no rows, both replies
    keep exactly the shape they had (an empty list), so this step cannot regress the empty case.

    The four friend ACTIONS (SW-128) dispatch first, because each answers a request rather than filling
    a list: an unknown target must still get its empty list back instead of falling through to the
    no-friends early return.
    """
    if reply_id in (FRIEND_DETAIL_REPLY, FRIEND_SEARCH_REPLY, FRIEND_SUGGEST_REPLY,
                    FRIEND_FAVORITE_REPLY, FRIEND_AGREE_REPLY, FRIEND_DELETE_REPLY,
                    FRIEND_REFUSE_REPLY, FRIEND_REQUEST_REPLY):
        return local_friend(spec, reply_id, plaintext)
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
        rows = [_friend_list_row(f, now) for f in friends]
        favs = favourite_map()
        for row in rows:
            fid = str(row.get("m3Wghr1j"))
            if fid in favs:
                row["8zE6w9RP"] = str(favs[fid])     # echo the client's own flag; see favourite_map
        spec[FRIENDLIST_TAG] = rows
    return spec
