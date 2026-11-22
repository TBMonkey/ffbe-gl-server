#!/usr/bin/env python3
"""ffbe_craft.py — the CRAFT family (SW-128): five request classes over the SHIPPED recipe tables.

The five classes are `CraftAdd` (`QkN1Sp64`), `CraftStart` (`Gr9zxXk5`), `CraftCancel` (`79xDN1Mw`),
`CraftEnd` (`WIuvh09n`) and `CraftExe` (`PKDhIN34`). Everything that decides what a craft costs, how long
it takes, what it makes and what it drops is a shipped row of `F_RECIPE_MST` (`MpNE6gB5` = RecipeId,
`gak9Gb3N` = category 20/21/22, `M5VLKe2F` = duration seconds, `HS7V6Ww1` = material triples,
`7UR4J2SE` = gil, `6uIYE15X`/`Qy5EvcK1` = product and count, `MHNbK26X` = byproduct `rate,kind:id:count,…`)
and `F_RECIPE_BOOK_MST` (`staFu8U1` = book id, `8Qt4Ccew` = the recipe ids it unlocks, `7UR4J2SE` = its
gil price). NOTHING here invents a recipe.

WHAT IS AUTHORED, AND IT IS LABELLED AT EACH USE
  * The four legacy timer slots per category (`CraftAdd` unlocks all four): a map-script unlock in the
    retail game, granted offline because the retired script that granted it cannot run.
  * The instant-completion RATE of one lapis per started three minutes. Its CAP is shipped --
    `CRAFT_TIME_REDUCTION_COST_LIMIT` = 5000 in `F_DEFINE_MST`; the rate is not recoverable.
  * `CRAFT_ALL_BOOKS=1` seeds every `F_RECIPE_BOOK_MST` id as owned. Default OFF: books are BOUGHT
    (each carries its own gil price) and a save starts owning none, so the default answer for an
    unowned book is a REFUSAL WITH A LOG, never a silent success.

REPLY SHAPES, from our own maps rather than from the fork. `F_CRAFT`'s list is `UserCraftInfoResponse`:
tag `gy84FjCA` is the WHOLE slot list and `yeXn0tk1` the single updated slot, and BOTH `f5De4ZpL` /
`qpUQ7cE8` (StartTime `+0x38` / EndTime `+0x40`) are `serving: required` -- a slot row without them
under-serves the class. `OpeCraftResponse` (`0eDvu2N3`) has exactly one token, `MHNbK26X`, also required:
it is the byproduct CSV of an execution, not a status.
"""
from __future__ import annotations

import math
import os
import time

import player_state as ps
from ffbe_state import State

CRAFT_GROUP = "0eDvu2N3"
SLOT_LIST_TAG = "gy84FjCA"
SLOT_ONE_TAG = "yeXn0tk1"
RESULTS_TAG = "0T5wP98d"
BYPRODUCT_TAG = "MHNbK26X"

REPLY_ADD = "QkN1Sp64"
REPLY_START = "Gr9zxXk5"
REPLY_CANCEL = "79xDN1Mw"
REPLY_END = "WIuvh09n"
REPLY_EXE = "PKDhIN34"
CRAFT_REPLIES = frozenset({REPLY_ADD, REPLY_START, REPLY_CANCEL, REPLY_END, REPLY_EXE})

CATEGORIES = ("20", "21", "22")
SLOTS_PER_CATEGORY = 4
CURRENCY_KINDS = (30, 50, 51)          # gil / lapis / friend point; `_grant_items` owns the split
AUTHORED_LAPIS_PER_3MIN = 1
ALL_BOOKS = os.environ.get("CRAFT_ALL_BOOKS", "0") != "0"
DATE_FMT = "%Y-%m-%d %H:%M:%S"


# ------------------------------------------------------------------------------------------------ tables
def _table(fname: str, key: str) -> dict:
    return ps._load_ndjson(fname, key)


def _define(name: str, default: int) -> int:
    row = _table("F_DEFINE_MST.ndjson", "AK8Nn7ta").get(name) or {}
    try:
        return int(row.get("YjIUDJ97"))
    except (TypeError, ValueError):
        return default


def _triples(text) -> list:
    """`kind:id:count,…` -> [{'kind','id','count'}]. The shipped shape, not a guess: a two-field entry is a
    currency amount (`30:10000`), so its `id` is 0 and the SECOND field is the count (idiom #45)."""
    out = []
    for part in str(text or "").split(","):
        bits = part.split(":")
        if not part.strip():
            continue
        if len(bits) == 2:
            out.append({"kind": int(bits[0]), "id": 0, "count": int(bits[1])})
        elif len(bits) >= 3:
            out.append({"kind": int(bits[0]), "id": int(bits[1]), "count": int(bits[2])})
    return out


def _reward_csv(rows) -> str:
    return ",".join("%d:%d:%d" % (r["kind"], r["id"], r["count"]) for r in rows)


# ------------------------------------------------------------------------------------------------- state
def _craft() -> dict:
    """The per-save craft block. One dictionary, under `data['craft']`."""
    return State.player.data.setdefault("craft", {})


def _slots() -> dict:
    return _craft().setdefault("slots", {})


def _slot_key(category, number) -> str:
    return "%s:%s" % (category, number)


def _owned_books() -> set:
    if ALL_BOOKS:
        return set(_table("F_RECIPE_BOOK_MST.ndjson", "staFu8U1"))
    return {str(b) for b in (_craft().get("books") or {})}


# ------------------------------------------------------------------------------------------------- slots
def _charge(slot: dict) -> int:
    """Lapis to finish NOW: the AUTHORED rate, the SHIPPED cap."""
    remaining = max(0, int(math.ceil(float(slot.get("end", 0) or 0) - time.time())))
    cap = _define("CRAFT_TIME_REDUCTION_COST_LIMIT", 5000)
    return min(cap, ((remaining + 179) // 180) * AUTHORED_LAPIS_PER_3MIN)


def _date(value) -> str:
    return time.strftime(DATE_FMT, time.gmtime(int(value))) if value else ""


def _slot_row(node: dict, slot: dict) -> dict:
    """One `UserCraftInfo` row. StartTime/EndTime are REQUIRED (`serving: required`) so they are always
    emitted, empty when the slot is idle -- an empty string is still a served token."""
    return {
        "gak9Gb3N": str(node.get("gak9Gb3N", "")),        # CraftId (category)
        "6ukvMSg9": str(node.get("6ukvMSg9", "")),        # No (slot number)
        "1J7WnqxL": str(slot.get("join_reason", 0)),      # JoinReason
        "MpNE6gB5": str(slot.get("recipe", 0)),           # RecipeId, 0 = idle
        "f5De4ZpL": _date(slot.get("start", 0)),          # StartTime  (+0x38)  REQUIRED
        "qpUQ7cE8": _date(slot.get("end", 0)),            # EndTime    (+0x40)  REQUIRED
        "nRHg3K2h": str(_charge(slot) if slot.get("recipe") else 0),   # DiaCharge
        "MN1g2DBs": str(max(0, int(math.ceil(float(slot.get("end", 0) or 0) - time.time())))),
    }


def _node(plaintext) -> dict:
    from ffbe_player import _request_json
    body = _request_json(plaintext)
    if body is None:
        print("craft: request body did not parse; nothing changed", flush=True)
        return {}
    rows = body.get(CRAFT_GROUP)
    return rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}


# ---------------------------------------------------------------------------------------------- payment
def _recipe(node: dict) -> dict | None:
    """The shipped recipe row for this request, or None (logged). Book ownership is checked."""
    player = State.player
    try:
        rid = str(int(node.get("MpNE6gB5")))
        bid = str(int(node.get("staFu8U1")))
    except (TypeError, ValueError):
        print("craft: request has no numeric RecipeId/RecipeBookId; nothing changed", flush=True)
        return None
    row = _table("F_RECIPE_MST.ndjson", "MpNE6gB5").get(rid)
    book = _table("F_RECIPE_BOOK_MST.ndjson", "staFu8U1").get(bid)
    if row is None:
        print("craft: recipe %r is not in F_RECIPE_MST; nothing changed" % (rid,), flush=True)
        return None
    if book is None or rid not in str(book.get("8Qt4Ccew") or "").split(","):
        print("craft: recipe %r is not unlocked by book %r; nothing changed" % (rid, bid), flush=True)
        return None
    owned = _owned_books()
    if bid not in owned:
        print("craft: book %r is NOT owned (%d owned%s); refused -- books are bought, see CRAFT_ALL_BOOKS"
              % (bid, len(owned), "" if not ALL_BOOKS else ", CRAFT_ALL_BOOKS=1"), flush=True)
        return None
    return row


def _pay(row: dict, count: int) -> bool:
    """Spend the recipe's materials and gil. False (already logged) if the player cannot afford it."""
    player = State.player
    materials = [{"kind": t["kind"], "id": t["id"], "count": t["count"] * count}
                 for t in _triples(row.get("HS7V6Ww1"))]
    missing = [m for m in materials
               if m["kind"] not in CURRENCY_KINDS and player.item_count(m["kind"], m["id"]) < m["count"]]
    gil = int(row.get("7UR4J2SE") or 0) * count
    if missing or int(player.currencies.get("gil", 0)) < gil:
        print("craft: REFUSED recipe %r x%d -- short on %s"
              % (row.get("MpNE6gB5"), count,
                 ", ".join("%d:%d x%d" % (m["kind"], m["id"], m["count"]) for m in missing) or
                 "gil (%d needed, %d held)" % (gil, int(player.currencies.get("gil", 0)))), flush=True)
        return False
    if not player._mutate():
        return False
    for m in materials:
        if m["kind"] in CURRENCY_KINDS:
            player.spend_currency(ps.STORAGE_CURRENCY.get(m["kind"], "gil"), m["count"])
        else:
            player.spend_item(m["kind"], m["id"], m["count"])
    if gil:
        player.spend_currency("gil", gil)
    return True


def _grant(rows: list) -> list:
    """Credit reward rows and return ONLY what the client can see (`_grant_items`' own contract)."""
    player = State.player
    bad = [r for r in rows if not player._can_credit(r)]
    if bad:
        print("craft: NOT credited -- no projection for %s"
              % ", ".join("%d:%d x%d" % (r["kind"], r["id"], r["count"]) for r in bad), flush=True)
        return []
    return player._grant_items(rows)


# ---------------------------------------------------------------------------------------------- actions
def craft_add(spec, plaintext):
    out = _spec(spec)
    node = _node(plaintext)
    wanted = [c for c in str(node.get("gak9Gb3N") or "").split(",") if c]
    unknown = [c for c in wanted if c not in CATEGORIES]
    if unknown:
        print("craft: add refused -- unknown categor(y/ies) %s (F_RECIPE_MST has %s)"
              % (unknown, list(CATEGORIES)), flush=True)
        return out
    slots = _slots()
    created = 0
    if State.player is not None and State.player._mutate():
        for category in wanted:
            for number in range(1, SLOTS_PER_CATEGORY + 1):
                key = _slot_key(category, number)
                if key not in slots:
                    slots[key] = {"recipe": 0, "book": 0, "start": 0, "end": 0, "join_reason": 0}
                    created += 1
        State.player.dirty = True
    print("craft: add %s -> %d slot(s) created, %d total (AUTHORED offline unlock)"
          % (wanted, created, len(slots)), flush=True)
    out[SLOT_LIST_TAG] = [_slot_row(_key_node(k), s) for k, s in sorted(slots.items())]
    return out


def _key_node(key: str) -> dict:
    category, _, number = key.partition(":")
    return {"gak9Gb3N": category, "6ukvMSg9": number}


def _slot(node: dict):
    """The addressed slot (category:number), creating it only if the category is one of the shipped three."""
    try:
        key = _slot_key(str(int(node.get("gak9Gb3N"))), str(int(node.get("6ukvMSg9"))))
    except (TypeError, ValueError):
        print("craft: request has no numeric CraftId/No; no slot addressed", flush=True)
        return None, None
    return key, _slots().get(key)


def craft_start(spec, plaintext):
    out = _spec(spec)
    node = _node(plaintext)
    key, slot = _slot(node)
    if slot is None:
        print("craft: start refused -- slot %r does not exist (CraftAdd unlocks slots)" % (key,), flush=True)
        return out
    if slot.get("recipe"):
        print("craft: start refused -- slot %r is busy with recipe %r" % (key, slot["recipe"]), flush=True)
        return out
    row = _recipe(node)
    if row is None:
        return out
    if str(node.get("gak9Gb3N")) != str(row.get("gak9Gb3N")):
        print("craft: start refused -- category %r does not match the recipe's %r"
              % (node.get("gak9Gb3N"), row.get("gak9Gb3N")), flush=True)
        return out
    if not _pay(row, 1):
        return out
    now = int(time.time())
    slot.update(recipe=str(row["MpNE6gB5"]), book=str(node.get("staFu8U1")),
                start=now, end=now + int(row.get("M5VLKe2F") or 0))
    print("craft: started recipe %r on %r, %s s (ends %s)"
          % (row["MpNE6gB5"], key, row.get("M5VLKe2F"), _date(slot["end"])), flush=True)
    out[SLOT_ONE_TAG] = [_slot_row(_key_node(key), slot)]
    return out


def craft_cancel(spec, plaintext):
    out = _spec(spec)
    node = _node(plaintext)
    key, slot = _slot(node)
    if slot is None or not slot.get("recipe"):
        print("craft: cancel -- slot %r has no active craft; nothing to cancel" % (key,), flush=True)
        return out
    if str(slot["recipe"]) != str(node.get("MpNE6gB5")):
        print("craft: cancel refused -- slot %r holds recipe %r, request names %r"
              % (key, slot["recipe"], node.get("MpNE6gB5")), flush=True)
        return out
    if str(node.get("I5o3stxJ", 0)) not in ("0", "False", "false"):
        print("craft: cancel refused -- cancellation cannot be paid with lapis", flush=True)
        return out
    if State.player is not None and State.player._mutate():
        slot.update(recipe=0, book=0, start=0, end=0)
        State.player.dirty = True
    # AUTHORED, and stated: the retail cancel refunds nothing and yields nothing.
    print("craft: cancelled recipe %r on %r (no refund, no output)" % (node.get("MpNE6gB5"), key), flush=True)
    out[SLOT_ONE_TAG] = [_slot_row(_key_node(key), slot)]
    return out


def craft_end(spec, plaintext):
    out = _spec(spec)
    node = _node(plaintext)
    key, slot = _slot(node)
    if slot is None or not slot.get("recipe"):
        print("craft: end -- slot %r has no active craft; nothing to finish" % (key,), flush=True)
        return out
    if str(slot["recipe"]) != str(node.get("MpNE6gB5")):
        print("craft: end refused -- slot %r holds recipe %r, request names %r"
              % (key, slot["recipe"], node.get("MpNE6gB5")), flush=True)
        return out
    charge = _charge(slot)
    if charge:
        if str(node.get("I5o3stxJ", 0)) not in ("1", "True", "true"):
            print("craft: end refused -- %s s remaining and lapis completion was not accepted"
                  % (slot["end"] - int(time.time())), flush=True)
            return out
        if not State.player.spend_lapis(charge):
            print("craft: end refused -- %d lapis needed to finish early, not affordable" % charge, flush=True)
            return out
        print("craft: end -- paid %d lapis to finish early" % charge, flush=True)
    row = _table("F_RECIPE_MST.ndjson", "MpNE6gB5").get(str(slot["recipe"])) or {}
    reward = {"kind": int(row.get("gak9Gb3N") or 20),
              "id": int(row.get("6uIYE15X") or 0),
              "count": int(row.get("Qy5EvcK1") or 1)}
    bonus = _byproducts(row, 1)
    credited = _grant([reward] + bonus)
    if State.player is not None and State.player._mutate():
        slot.update(recipe=0, book=0, start=0, end=0)
        State.player.dirty = True
    ps_craft_progress(row, 1)
    print("craft: finished recipe %r on %r -> %s%s"
          % (row.get("MpNE6gB5"), key, "%d:%d x%d" % (reward["kind"], reward["id"], reward["count"]),
             "" if credited else " (NOT credited -- no projection)"), flush=True)
    out[SLOT_ONE_TAG] = [_slot_row(_key_node(key), slot)]
    return out


def ps_craft_progress(row: dict, count: int) -> None:
    """The shipped progress counters (`TOTAL_CRAFT_*` are defines, so they are read, not typed)."""
    key = {"20": "TOTAL_CRAFT_ITEM", "21": "TOTAL_CRAFT_EQUIP",
           "22": "TOTAL_CRAFT_ABILITY"}.get(str(row.get("gak9Gb3N")))
    if key and State.player is not None:
        State.player.bump_progress(key, count, what="completed crafting")


def _byproducts(row: dict, count: int) -> list:
    """`MHNbK26X` = `rate,kind:id:count,…`; empty/`0`/`0,` means none. Rolled here, because drops are
    server-authoritative (the same rule `roll_drops` follows for battles)."""
    import random
    text = str(row.get("MHNbK26X") or "").strip()
    if text in ("", "0", "0,"):
        return []
    rate_text, sep, items = text.partition(",")
    try:
        rate = int(rate_text)
    except ValueError:
        print("craft: byproduct rate %r is not numeric; treated as none" % (rate_text,), flush=True)
        return []
    if not sep:
        print("craft: byproduct %r has no item list; treated as none" % (text,), flush=True)
        return []
    rows = _triples(items)
    hits = sum(1 for _ in range(count) if rate >= 100 or random.randrange(100) < rate)
    return [dict(r, count=r["count"] * hits) for r in rows] if hits else []


def craft_exe(spec, plaintext):
    """`CraftExe` -- the batch craft. `harness/request_api_map.json` records the reply as ABSENT FROM THE
    FACTORY under `ayrzi0H4`, so the two groups below are what the client's OWN result screen reads (the
    result rows it hands back, and `OpeCraftResponse`'s required byproduct slot)."""
    out = _spec(spec)
    from ffbe_player import _request_json
    body = _request_json(plaintext) or {}
    nodes = body.get("ayrzi0H4")
    if not isinstance(nodes, list) or not nodes or not all(isinstance(n, dict) for n in nodes):
        print("craft: exe refused -- no craft batch in the request", flush=True)
        return out
    preview = []
    for n in nodes:
        try:
            count = int(n.get("3EhUu8Xm") or 1)
        except (TypeError, ValueError):
            count = 1
        row = _recipe(n)
        if row is None:
            return out
        preview.append((n, row, max(1, count)))
    # Validate EVERYTHING before spending anything: a batch that fails halfway would keep the materials.
    for n, row, count in preview:
        materials = [{"kind": t["kind"], "id": t["id"], "count": t["count"] * count}
                     for t in _triples(row.get("HS7V6Ww1"))]
        gil = int(row.get("7UR4J2SE") or 0) * count
        short = [m for m in materials if m["kind"] not in CURRENCY_KINDS
                 and State.player.item_count(m["kind"], m["id"]) < m["count"]]
        if short or int(State.player.currencies.get("gil", 0)) < gil:
            print("craft: exe refused -- the batch is unaffordable at recipe %r x%d"
                  % (row.get("MpNE6gB5"), count), flush=True)
            return out
    results, bonus_all = [], []
    for n, row, count in preview:
        if not _pay(row, count):
            print("craft: exe aborted at recipe %r x%d after earlier items were paid -- see the log"
                  % (row.get("MpNE6gB5"), count), flush=True)
            return out
        reward = {"kind": int(row.get("gak9Gb3N") or 20),
                  "id": int(row.get("6uIYE15X") or 0),
                  "count": int(row.get("Qy5EvcK1") or 1) * count}
        bonus = _byproducts(row, count)
        credited = _grant([reward] + bonus)
        bonus_all.extend(bonus)
        ps_craft_progress(row, count)
        results.append({"staFu8U1": str(n.get("staFu8U1", "")), "MpNE6gB5": str(row["MpNE6gB5"]),
                        "jsvoa0I2": _reward_csv([reward]),
                        BYPRODUCT_TAG: _reward_csv(bonus), "nM63Zvtp": ""})
        print("craft: exe recipe %r x%d -> %s%s"
              % (row["MpNE6gB5"], count, _reward_csv([reward]),
                 "" if credited else " (NOT credited -- no projection)"), flush=True)
    _craft()["last_results"] = results
    out[RESULTS_TAG] = results
    out[CRAFT_GROUP] = [{BYPRODUCT_TAG: _reward_csv(bonus_all)}]
    return out


def _spec(spec) -> dict:
    """A mutable spec. A craft request has no `responses.json` entry, so the dispatcher hands us None and an
    adding transform must create the dict (idiom #41)."""
    return dict(spec) if isinstance(spec, dict) else {}


def serve_craft(spec, reply_id, plaintext):
    """The pipeline step: no-op for any reply that is not one of the five craft requests."""
    if reply_id not in CRAFT_REPLIES:
        return spec
    handler = {REPLY_ADD: craft_add, REPLY_START: craft_start, REPLY_CANCEL: craft_cancel,
               REPLY_END: craft_end, REPLY_EXE: craft_exe}[reply_id]
    return handler(spec, plaintext)
