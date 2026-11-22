#!/usr/bin/env python3
"""ffbe_gift.py — the friend-gift family (SW-128): the catalogue, the history, and `GiftUpdateRequest`.

WHAT IS SHIPPED AND WHAT IS NOT. There is **no `F_GIFT_MST`** — not in our 220 decoded masters, not in the
fork's 225 — so the gift catalogue is a SERVICE-side table that the server must send as a response group
(`GiftMstResponse`, tag `PmR4z1S7`). What IS shipped:

  * the eight elemental crysts — `F_ITEM_MST` 201000100..201000800 (火/氷/雷/水/土/風/光/闇の結晶,
    sub_type 89..96), which is why this module DERIVES them from the item table instead of listing ids;
  * the reward kinds: 30 = gil, 51 = friend point, 20 = item (`COST_KINDS`, instruction-confirmed from
    `GameUtils::getItemNum`);
  * `F_DEFINE_MST` `INITIAL_GIFT_ID` = 10, which pins the FIRST row's id: the client's own default
    preference is gift 10, so a server that starts its ids anywhere else starts from a mismatch.

AUTHORED, and labelled here because nothing can recover it: the id→reward ASSIGNMENT beyond id 10 (10 gil
count, 20 friend points, then one cryst each) and the one-per-friend-per-UTC-day send rule.

BOTH GROUPS ARE DESTRUCTIVE. `harness/request_api_map.json` `carrier_rules` shows `onReadStarted` ->
`cocos2d::__Array::removeAllObjects()` for `PmR4z1S7` AND `h5XGny0N`, so each one CLEARS the client's list
before the parse rebuilds it: a partial list deletes the rest (idiom #31), and the reply must carry the FULL
list every time. For the same reason they ride only the surface that rebuilds them — the boot/user-state
replies and the gift request itself (`CARRY_REPLIES`) — and never an arbitrary action ack.
"""
from __future__ import annotations

import os
import time

import player_state as ps
from ffbe_state import State

GIFT_MST_TAG = "PmR4z1S7"          # GiftMstResponse      (destructive: full list every time)
GIFT_INFO_TAG = "h5XGny0N"         # UserGiftInfoResponse (destructive: full list every time)
REPLY_GIFT_UPDATE = "9KN5rcwj"     # GiftUpdateRequest
REQUEST_GROUP = "WSH38iMI"         # the request's own group
RECIPIENTS_KEY = "2L0dxwuN"        # "friendId:giftId,…"
SELECTED_KEY = "9wXIbKq2"

# The replies that rebuild the two lists. Boot first (`75fYdNxq` = Initialize/GameSetting, `X07iYtp5` =
# GetUserInfo, `6H1R9WID` = switch info), then the friends area, then the gift request itself -- the same
# surface the JP reference server restores its feature state on.
CARRY_REPLIES = frozenset({"75fYdNxq", "X07iYtp5", "6H1R9WID", "u7Id4bMg", REPLY_GIFT_UPDATE})

GIFT_WINDOW = (1577836800, 4102444800)      # time_t, the same window our event gate serves
CRYST_FIRST, CRYST_LAST = 201000100, 201000800
GIFT_GIL = {"id": 10, "kind": 30, "count": 20}          # AUTHORED count; INITIAL_GIFT_ID pins id 10
GIFT_FP = {"id": 20, "kind": 51, "count": 10}           # AUTHORED count
DATE_FMT = "%Y-%m-%d"


def _player():
    return State.player


def _gift() -> dict:
    """The per-save gift block: send history, per-friend last-send day, receipts."""
    block = _player().data.setdefault("gift", {})
    block.setdefault("history", {})
    block.setdefault("sent", {})
    return block


def _cryst_ids() -> list:
    """The eight crysts, DERIVED from the shipped item table (never a hand-typed list)."""
    ids, want = [], set(range(CRYST_FIRST, CRYST_LAST + 1, 100))
    for row in ps._load_ndjson("F_ITEM_MST.ndjson", "tL6G9egd").values():
        try:
            item_id = int(row.get("tL6G9egd"))
        except (TypeError, ValueError):
            continue
        if item_id in want:
            ids.append(item_id)
    return sorted(ids)


def catalogue() -> list:
    """The ten gift rows, ids 10..100. Eight are shipped items; the two currency rows are authored."""
    start, end = GIFT_WINDOW
    def row(gift_id, kind, target, count):
        return {"Utx8P1Qo": str(gift_id),          # GiftID -- the row key
                "YWF08vZS": str(kind),             # ItemType
                "6uIYE15X": str(target),           # TargetID
                "Qy5EvcK1": str(count),            # Num
                "8DZfvju4": "1",                   # GiftType
                "VjJQ51uG": str(start),            # StartDate time_t   REQUIRED (GiftMst::isEnable)
                "m8ivD4NX": str(end)}              # EndDate   time_t   REQUIRED
    rows = [row(GIFT_GIL["id"], GIFT_GIL["kind"], 0, GIFT_GIL["count"]),
            row(GIFT_FP["id"], GIFT_FP["kind"], 0, GIFT_FP["count"])]
    for index, item_id in enumerate(_cryst_ids(), start=3):
        rows.append(row(index * 10, 20, item_id, 1))
    return rows


def _reward_text(row: dict) -> str:
    """`kind:count` for a currency, `kind:id:count` for an item -- the shipped two shapes (idiom #45)."""
    kind = int(row["YWF08vZS"])
    if kind in ps.STORAGE_CURRENCY:
        return "%d:%d" % (kind, int(row["Qy5EvcK1"]))
    return "%d:%d:%d" % (kind, int(row["6uIYE15X"]), int(row["Qy5EvcK1"]))


def _send_row(entry_id: str, now: int, sender: dict, friend_id: str,
              recipient: dict, reward: str) -> dict:
    """One `UserGiftInfo` row. `SendDate` is EPOCH SECONDS, not a formatted date -- the class parses it as a
    number, so a `%Y-%m-%d` string here would read as 0."""
    return {"2HqjK1F7": entry_id,                      # GiftSendId
            "f5LHNT4v": str(now),                      # SendDate (epoch s)
            "m94IYp6a": sender.get("friend_id", ""),   # FriendIdFrom
            "9WsTg26F": str(sender.get("master_id", 0)),   # UnitIdFrom
            "AJGfV35v": sender.get("handle_name", ""),  # HandleNameFrom
            "QSf4NDq3": str(sender.get("rank", 1)),     # TeamLvFrom
            "wjX34YxG": str(sender.get("unit_level", 1)),  # UnitLvFrom
            "bUfq8BJ3": str(friend_id),                 # FriendIdTo
            "Igd28KXq": "0",                            # ReceiveState
            "jsvoa0I2": reward,                         # GiftInfo
            "n69quiCJ": str(recipient.get("player_emblem_id", 0)),   # PlayerEmblemId  REQUIRED
            "vs48LZrt": "0"}                            # PlayerFrameType


def _self() -> dict:
    """The player's own identity as a sender row."""
    player = _player()
    prof = player.data.get("profile") or {}
    party = (player.data.get("party") or {}).get("decks") or {}
    return {"friend_id": str(player.data.get("identity", {}).get("user_id", "")),
            "handle_name": str(player.data.get("identity", {}).get("handle_name", "")),
            "player_emblem_id": prof.get("player_emblem_id", 0),
            "rank": (player.data.get("rank") or {}).get("level", 1),
            "master_id": 0, "unit_level": 1}


def gift_update(spec: dict, plaintext) -> dict:
    """`GiftUpdateRequest` (`9KN5rcwj`): record a send to each local companion, then serve the FULL history.

    The recipients must be companions from `friends.yaml` -- there is nobody else to give to offline -- and a
    second send to the same friend on the same UTC day is DROPPED, not refused: a retried request has to
    return the same history rather than either failing or paying twice.
    """
    from ffbe_friends import load_friends
    from ffbe_player import _request_json
    body = _request_json(plaintext) or {}
    rows = body.get(REQUEST_GROUP)
    node = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
    if not node:
        print("gift: update carried no %s row; nothing changed" % (REQUEST_GROUP,), flush=True)
    friends = {str(f["friend_id"]): f for f in load_friends()}
    by_id = {str(r["Utx8P1Qo"]): r for r in catalogue()}
    profile = _player().data.setdefault("profile", {})
    selected = str(node.get("Utx8P1Qo") or profile.get("gift_id") or GIFT_GIL["id"])
    if selected not in by_id:
        print("gift: REFUSED unknown gift id %r (catalogue has %s); preference unchanged"
              % (selected, sorted(by_id)), flush=True)
        # Still send both lists: they are DESTRUCTIVE, so the request that rebuilds them must always be
        # able to deliver the full set -- a refusal that withholds the list leaves the client's copy stale
        # with nothing to explain it (idiom #44).
        return _lists(spec)
    if _player()._mutate():
        profile["gift_id"] = int(selected)
        _player().dirty = True
    gift = _gift()
    now = int(time.time())
    today = time.strftime(DATE_FMT, time.gmtime(now))
    reward = _reward_text(by_id[selected])
    sent_now = 0
    for entry in str(node.get(RECIPIENTS_KEY) or "").split(","):
        if not entry.strip():
            continue
        friend_id, _, gift_id = entry.partition(":")
        if friend_id not in friends:
            print("gift: skipped %r -- not an offline companion" % (friend_id,), flush=True)
            continue
        if gift_id not in by_id:
            print("gift: skipped %r -- unknown gift %r" % (friend_id, gift_id), flush=True)
            continue
        if gift["sent"].get(friend_id) == today:
            print("gift: %r already received one today; not sent again" % (friend_id,), flush=True)
            continue
        entry_id = "%s%s" % (now, len(gift["history"]))
        gift["history"][entry_id] = _send_row(entry_id, now, _self(), friend_id, profile,
                                              _reward_text(by_id[gift_id]))
        gift["sent"][friend_id] = today
        sent_now += 1
        print("gift: sent %s to %r" % (_reward_text(by_id[gift_id]), friend_id), flush=True)
    if sent_now and _player()._mutate():
        _player().bump_progress("TOTAL_GIFT_PRESENT", sent_now, what="friend gift sent")
        _player().dirty = True
    print("gift: update selected=%r sent_now=%d history=%d (one per friend per UTC day, AUTHORED rule)"
          % (selected, sent_now, len(gift["history"])), flush=True)
    return _lists(spec)


def _lists(spec) -> dict:
    """Both destructive groups, whole. Every reply that carries them carries ALL of them."""
    out = dict(spec) if isinstance(spec, dict) else {}
    out[GIFT_MST_TAG] = catalogue()
    out[GIFT_INFO_TAG] = list(_gift()["history"].values())
    return out


def serve_gift(spec, reply_id, plaintext):
    """The pipeline step. The gift request itself, plus the boot/friends surface that carries the lists.

    BOTH groups are destructive (`onReadStarted` -> `removeAllObjects`), so each reply that carries them
    carries the WHOLE list, and they ride only `CARRY_REPLIES` -- never an arbitrary action ack, where a
    rebuilt list would wipe a pending edit (idiom #31).
    """
    if reply_id not in CARRY_REPLIES or _player() is None:
        return spec
    if reply_id == REPLY_GIFT_UPDATE:
        return gift_update(spec, plaintext)
    return _lists(spec)
