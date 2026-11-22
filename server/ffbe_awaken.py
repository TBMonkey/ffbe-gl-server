#!/usr/bin/env python3
"""ffbe_awaken.py — the unit class-up write path: normal awakening, NV, NV+ and EX.

WHY THIS EXISTS (APP-80, 2026-11-11). `UnitClassUpRequest`, `UnitNvClassUpRequest`,
`UnitNvpClassUpRequest` and `UnitExClassUpRequest` were all unserved: the reply was an empty/default body,
so the client's awaken scene either no-op'd or reported a connection error while the state stayed at the
old master. The tables are shipped and the client's request groups are recovered:

    normal   zf49XKg8  group sWc7EH69  og2GHy49 = the owned unit's instance id
    NV       Pf5C8xjJ  group 6U5w20zk  + KCk8u0am (the materials), Knra4I19 (discount campaign)
    NV+      V8GtMQ2q  group 6U5w20zk  same shape
    EX       k6NI9Tm1  group 14vWb7kX  same shape; the group is the F_NV_EX_CLASS_UP_MST group

THE CLIENT RENEWS THE INSTANCE. `UnitEnhanceClassUpConnectScene` removes the source id before opening the
destination, so the server must call `PlayerState.renew_unit_instance`: the roster row keeps its progress
and gets a NEW `instance_id`, and the saved decks move with it. Reusing the id deletes the awakened unit
(the Rust fork's docstring records that failure).

THE DATA SPLIT THAT MATTERS (docs/notes/unit_levels_and_awakening.md). Every `F_UNIT_CLASS_UP_MST` row
touches a FORM row (`I3SKJ4q0=0`), never a Brave-Shift BASE (`I3=1`). Normal sources are 1-6★; NV is
7★ no-shift -> a self form; NV+ is self form -> self form. The table is the authority; do not infer a
target from the unit's name or from the base row.

COSTS AND REWARDS. The client sends the material tuple for NV/NV+/EX; normal does not (the master is the
only source). We validate the sent tuple against the master, pre-check every item and the gil, then spend
once. EX's `dX6cor8j` rewards go through the ONE `PlayerState._grant_items` path, so a kind-27 card lands
in `vision_cards` and a kind-10 unit in the roster rather than an invisible bag row. The 9-field EX trust
reward has a target descriptor we do not model yet; it is logged, not fabricated.
"""
from __future__ import annotations

import json
from collections import Counter

import player_state as ps
from ffbe_battle import _lv_half
from ffbe_state import State

CLASS_UP_REQUEST = "zf49XKg8"
NV_CLASS_UP_REQUEST = "Pf5C8xjJ"
NVPLUS_CLASS_UP_REQUEST = "V8GtMQ2q"
EX_CLASS_UP_REQUEST = "k6NI9Tm1"

CLASS_UP_GROUP = "sWc7EH69"
NV_CLASS_UP_GROUP = "6U5w20zk"
EX_CLASS_UP_GROUP = "14vWb7kX"

_CLASS_UP = None
_NV_EX = None


def _class_up_map():
    global _CLASS_UP
    if _CLASS_UP is None:
        _CLASS_UP = {str(r.get("3HriTp6B")): r
                     for r in ps._load_ndjson("F_UNIT_CLASS_UP_MST.ndjson").get("__rows__", [])}
    return _CLASS_UP


def _nv_ex_map():
    global _NV_EX
    if _NV_EX is None:
        _NV_EX = {(str(r.get("JPW8Vs40")), str(r.get("f8vk4JrD"))): r
                  for r in ps._load_ndjson("F_NV_EX_CLASS_UP_MST.ndjson").get("__rows__", [])}
    return _NV_EX


def _request_json(plaintext):
    if not plaintext:
        return None
    try:
        body = json.JSONDecoder().raw_decode(plaintext)[0]
    except Exception:  # noqa: BLE001 - a bad body must never break the reply
        return None
    return body if isinstance(body, dict) else None


def _node(body, group):
    rows = body.get(group) if isinstance(body, dict) else None
    return rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None


def _cost_rows(text):
    """`20:id:count,...` -> [(kind, id, count)], or ValueError on a malformed tuple."""
    out = []
    for entry in str(text or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) != 3:
            raise ValueError("malformed cost tuple %r" % (entry,))
        kind, iid, count = parts
        if not (kind.isdigit() and iid.isdigit() and count.isdigit()):
            raise ValueError("non-numeric cost tuple %r" % (entry,))
        out.append((int(kind), str(iid), int(count)))
    return out


def _cost_key(rows):
    return Counter((kind, iid) for kind, iid, _ in rows)


def _gil(row):
    try:
        return int(row.get("7UR4J2SE") or 0)
    except (TypeError, ValueError):
        return 0


def _refuse(what):
    print(f"unit awaken: refused -- {what}", flush=True)
    return False


def _precheck(player, materials, gil):
    totals = Counter()
    for kind, iid, count in materials:
        totals[(kind, iid)] += count
    for (kind, iid), count in totals.items():
        if player.item_count(kind, iid) < count:
            return _refuse(f"missing material {kind}:{iid} x{count}")
    if int(player.currencies.get("gil", 0) or 0) < gil:
        return _refuse(f"insufficient gil ({gil})")
    return True


def _spend(player, materials, gil):
    for kind, iid, count in materials:
        if not player.spend_item(kind, iid, count):
            return False
    return player.spend_currency("gil", gil)


def _relabel_lb(unit, target_id, target):
    lb = ps.limit_burst(target_id)
    if lb:
        cap = int(target.get("ENwn42DV") or 1)
        unit["lb_level"] = max(1, min(int(unit.get("lb_level") or 1), cap))
    else:
        unit["lb_level"] = 0


def _awaken(spec, body, unit, kind, group):
    master = ps.unit_mst(unit["master_id"]) or {}
    step = _class_up_map().get(str(unit["master_id"]))
    if step is None:
        return _refuse("no F_UNIT_CLASS_UP_MST row for %s" % unit["master_id"]) or spec
    target_id = str(step.get("t74crqAL") or "")
    target = ps.unit_mst(target_id)
    if target is None:
        return _refuse("target %s is not in F_UNIT_MST" % target_id) or spec

    source_rarity = int(master.get("9fW0TePj") or 0)
    target_rarity = int(target.get("9fW0TePj") or 0)
    source_jpw = str(master.get("JPW8Vs40") or "0")
    target_jpw = str(target.get("JPW8Vs40") or "0")
    if kind == "normal":
        if not (1 <= source_rarity <= 6 and target_rarity == source_rarity + 1):
            return _refuse("not a normal rarity step (%s -> %s)" % (source_rarity, target_rarity)) or spec
        if source_jpw not in ("", "0") or target_jpw not in ("", "0"):
            return _refuse("normal awakening touches a Neo-Vision row") or spec
    elif kind == "nv":
        if source_rarity != 7 or source_jpw not in ("", "0"):
            return _refuse("NV source is not a 7* no-shift row") or spec
        if target_rarity != 7 or target_jpw != target_id:
            return _refuse("NV target is not a self form") or spec
        if int(unit.get("level") or 1) < ps.max_level(unit["master_id"]):
            return _refuse("NV source is not at its level cap") or spec
    elif kind == "nvp":
        if source_rarity != 7 or source_jpw != str(unit["master_id"]):
            return _refuse("NV+ source is not a self form") or spec
        if target_rarity != 7 or target_jpw != target_id:
            return _refuse("NV+ target is not a self form") or spec
    else:
        return _refuse("unknown awakening kind") or spec

    node = _node(body, group) or {}
    discount = str(node.get("Knra4I19", "0") or "0")
    if discount not in ("", "0"):
        return _refuse("a class-up discount campaign is not active") or spec
    try:
        materials = _cost_rows(step.get("KCk8u0am"))
    except ValueError as exc:
        return _refuse(str(exc)) or spec
    if node.get("KCk8u0am") is not None:
        try:
            sent = _cost_rows(node.get("KCk8u0am"))
        except ValueError as exc:
            return _refuse(str(exc)) or spec
        if _cost_key(sent) != _cost_key(materials):
            return _refuse("sent awakening materials do not match the master") or spec
    gil = _gil(step)
    if not _precheck(State.player, materials, gil):
        return spec
    if State.player.mode == "frozen":
        return _refuse("state is frozen") or spec
    if not _spend(State.player, materials, gil):
        return _refuse("the material/gil debit was refused") or spec

    old_id = str(unit["instance_id"])
    unit["master_id"] = target_id
    _relabel_lb(unit, target_id, target)
    if kind == "nv":
        unit["nv_ex_level"] = 0
    renewed = State.player.renew_unit_instance(old_id)
    if renewed is None:
        print(f"unit awaken: CRITICAL -- {old_id} was paid for but could not be renewed", flush=True)
        return spec
    new_id = str(renewed["instance_id"])
    out = dict(spec) if isinstance(spec, dict) else {}
    out[group] = [{"og2GHy49": old_id, "5u7VYNEX": new_id,
                   "KCk8u0am": str(step.get("KCk8u0am") or ""),
                   "WbGYcN17": "", "02CI3bpE": "", "p9Xj4Anc": "0"}]
    # `B71MekS8` is the projection trigger: `apply_to_reply` sees a player group and rebuilds the roster
    # from the state this step just changed. The class-up group itself is consumed by the awaken scene.
    out.setdefault("B71MekS8", [])
    print(f"unit awaken: {old_id} {master.get('3HriTp6B')} -> {target_id} "
          f"({kind}), new instance {new_id}", flush=True)
    return out


def _parse_ex_rewards(text):
    """The `dX6cor8j` reward tuple, with the client's 3/4/5/9-field forms.

    The 9-field kind-10 row carries a unit trust target descriptor; the state has no trust model yet, so
    the row is granted as its unit and the extra fields are logged rather than invented.
    """
    rows = []
    for entry in str(text or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        fields = entry.split(":")
        if len(fields) not in (3, 4, 5, 9):
            raise ValueError("invalid EX reward tuple %r" % (entry,))
        if len(fields) >= 4 and fields[3] != "100":
            raise ValueError("unsupported EX reward probability %r" % (entry,))
        kind, iid, count = int(fields[0]), int(fields[1]), int(fields[2])
        row = {"kind": kind, "id": iid, "count": count}
        if len(fields) >= 5:
            row["level"] = int(fields[4] or 1)
        if len(fields) == 9:
            row["trust_metadata"] = fields[5:]
        rows.append(row)
    return rows


def _awaken_ex(spec, body, unit):
    node = _node(body, EX_CLASS_UP_GROUP) or {}
    master = ps.unit_mst(unit["master_id"]) or {}
    group_id = str(master.get("JPW8Vs40") or "0")
    if group_id in ("", "0"):
        return _refuse("unit has no Neo-Vision EX group") or spec
    level = int(unit.get("nv_ex_level") or 0) + 1
    if level > 3:
        return _refuse("unit is already EX+3") or spec
    step = _nv_ex_map().get((group_id, str(level)))
    if step is None:
        return _refuse("no F_NV_EX_CLASS_UP_MST row for %s EX+%s" % (group_id, level)) or spec
    discount = str(node.get("Knra4I19", "0") or "0")
    if discount not in ("", "0"):
        return _refuse("an EX discount campaign is not active") or spec
    try:
        materials = _cost_rows(step.get("KCk8u0am"))
        if node.get("KCk8u0am") is not None and _cost_key(_cost_rows(node.get("KCk8u0am"))) != _cost_key(materials):
            return _refuse("sent EX materials do not match the master") or spec
        rewards = _parse_ex_rewards(step.get("dX6cor8j"))
    except ValueError as exc:
        return _refuse(str(exc)) or spec
    gil = _gil(step)
    if not _precheck(State.player, materials, gil):
        return spec
    if State.player.mode == "frozen":
        return _refuse("state is frozen") or spec
    before = _lv_half(int(unit.get("level", 1)), int(unit.get("exp", 0)),
                      ps.unit_stats(unit["master_id"], int(unit.get("level", 1))),
                      int(unit.get("lb_level", 0) or 0), int(unit.get("lb_exp", 0) or 0))
    if not _spend(State.player, materials, gil):
        return _refuse("the material/gil debit was refused") or spec
    unit["nv_ex_level"] = level
    State.player.dirty = True
    if rewards:
        projected = State.player._grant_items(rewards)
        if len(projected) != len(rewards):
            print(f"unit awaken: EX+{level} granted {len(projected)}/{len(rewards)} reward row(s); "
                  f"the unprojected rows are logged above", flush=True)
        for row in rewards:
            if row.get("trust_metadata"):
                print(f"unit awaken: EX+{level} kind-{row['kind']} trust metadata not modelled: "
                      f"{row.get('trust_metadata')}", flush=True)
    after = _lv_half(int(unit.get("level", 1)), int(unit.get("exp", 0)),
                     ps.unit_stats(unit["master_id"], int(unit.get("level", 1))),
                     int(unit.get("lb_level", 0) or 0), int(unit.get("lb_exp", 0) or 0))
    out = dict(spec) if isinstance(spec, dict) else {}
    iid = str(unit["instance_id"])
    out[EX_CLASS_UP_GROUP] = [{"og2GHy49": iid, "KCk8u0am": str(step.get("KCk8u0am") or ""),
                               "Knra4I19": "0", "Qic38SuC": f"{iid}@{before}_{after}"}]
    out.setdefault("B71MekS8", [])
    print(f"unit awaken: {iid} {unit['master_id']} -> EX+{level}", flush=True)
    return out


def apply_unit_awakening(spec, reply_id, plaintext):
    """Pipeline step: apply a class-up request, or leave the spec untouched for every other request."""
    request_id = str(reply_id)
    if request_id not in (CLASS_UP_REQUEST, NV_CLASS_UP_REQUEST, NVPLUS_CLASS_UP_REQUEST,
                          EX_CLASS_UP_REQUEST) or State.player is None:
        return spec
    body = _request_json(plaintext)
    if body is None:
        print(f"unit awaken: {request_id} carried no parseable body", flush=True)
        return spec
    if request_id == EX_CLASS_UP_REQUEST:
        node = _node(body, EX_CLASS_UP_GROUP) or {}
        unit = State.player.roster_row(str(node.get("og2GHy49", "")))
        if unit is None:
            return _refuse("unit %s is not in the roster" % node.get("og2GHy49")) or spec
        return _awaken_ex(spec, body, unit)
    group = CLASS_UP_GROUP if request_id == CLASS_UP_REQUEST else NV_CLASS_UP_GROUP
    node = _node(body, group) or {}
    unit = State.player.roster_row(str(node.get("og2GHy49", "")))
    if unit is None:
        return _refuse("unit %s is not in the roster" % node.get("og2GHy49")) or spec
    kind = {CLASS_UP_REQUEST: "normal", NV_CLASS_UP_REQUEST: "nv",
            NVPLUS_CLASS_UP_REQUEST: "nvp"}[request_id]
    return _awaken(spec, body, unit, kind, group)


# --- selftest ---------------------------------------------------------------------------------------
def _selftest() -> int:
    """The four write paths plus the two refusals that protect the economy.

    This is an OFFLINE check of the state and the reply group, not of the client scene. It uses the
    shipped 6* -> 7* row (`100000316` -> `100000317`), the 7* -> NV row (`100000317` -> `100000327`), a
    shipped NV -> NV+ row (`100043107` -> `100043127`) and the EX+1 row for group `100000327`.
    """
    failed = []

    def case(name, ok, want, got):
        print(f"awaken {'ok  ' if ok else 'FAIL'} {name} (want {want}, got {got})")
        if not ok:
            failed.append(name)

    def fresh(master, level):
        player = ps.PlayerState.load("/nonexistent", slot="awakenselftest")
        State.player = player
        unit = player.grant_unit(master)
        player.set_unit_level(unit["instance_id"], level)
        return player, unit

    def fund(player, step):
        for kind, iid, count in _cost_rows(step.get("KCk8u0am")):
            player.grant_item(kind, iid, count)
        gil = _gil(step)
        player.add_currency("gil", gil)
        return gil

    def body(group, iid, step, include_cost=True):
        node = {"og2GHy49": iid, "Knra4I19": "0"}
        if include_cost:
            node["KCk8u0am"] = step.get("KCk8u0am") or ""
        return json.dumps({group: [node]})

    # 1. renewal moves the saved deck reference, not just the roster row.
    player, unit = fresh("100000316", 100)
    old = unit["instance_id"]
    deck = player.data["party"]["decks"][0]
    if deck.get("members"):
        deck["members"][0]["instance_id"] = old
    renewed = player.renew_unit_instance(old)
    new = renewed["instance_id"] if renewed else None
    case("renewal changes the id", new != old and player.roster_row(old) is None, "a new id", new)
    case("renewal moves the deck reference",
         player.data["party"]["decks"][0]["members"][0]["instance_id"] == new, new,
         player.data["party"]["decks"][0]["members"][0].get("instance_id"))

    # 2. normal 6* -> 7*.
    player, unit = fresh("100000316", 100)
    step = _class_up_map()["100000316"]
    gil = fund(player, step)
    old = unit["instance_id"]
    out = apply_unit_awakening({}, CLASS_UP_REQUEST, body(CLASS_UP_GROUP, old, step, include_cost=False))
    case("normal master", unit["master_id"] == "100000317", "100000317", unit["master_id"])
    case("normal source gone", player.roster_row(old) is None, "None", player.roster_row(old))
    case("normal reply destination", out.get(CLASS_UP_GROUP, [{}])[0].get("5u7VYNEX") == unit["instance_id"],
         unit["instance_id"], out.get(CLASS_UP_GROUP, [{}])[0].get("5u7VYNEX"))
    case("normal materials spent", all(player.item_count(k, i) == 0 for k, i, _ in _cost_rows(step["KCk8u0am"])),
         "0", player.data["inventory"].get("items"))
    case("normal gil spent", player.currencies.get("gil", 0) == 0, 0, player.currencies.get("gil"))

    # 3. 7* -> NV.
    player, unit = fresh("100000317", 120)
    step = _class_up_map()["100000317"]
    fund(player, step)
    old = unit["instance_id"]
    out = apply_unit_awakening({}, NV_CLASS_UP_REQUEST, body(NV_CLASS_UP_GROUP, old, step))
    case("NV master", unit["master_id"] == "100000327", "100000327", unit["master_id"])
    case("NV resets EX", unit["nv_ex_level"] == 0, 0, unit["nv_ex_level"])
    case("NV source gone", player.roster_row(old) is None, "None", player.roster_row(old))

    # 4. NV -> NV+.
    player, unit = fresh("100043107", 130)
    step = _class_up_map()["100043107"]
    fund(player, step)
    old = unit["instance_id"]
    apply_unit_awakening({}, NVPLUS_CLASS_UP_REQUEST, body(NV_CLASS_UP_GROUP, old, step))
    case("NV+ master", unit["master_id"] == "100043127", "100043127", unit["master_id"])
    case("NV+ source gone", player.roster_row(old) is None, "None", player.roster_row(old))

    # 5. EX+1: same instance, cost spent, reply carries the level channel.
    player, unit = fresh("100000327", 130)
    step = _nv_ex_map()[("100000327", "1")]
    fund(player, step)
    iid = unit["instance_id"]
    out = apply_unit_awakening({}, EX_CLASS_UP_REQUEST, body(EX_CLASS_UP_GROUP, iid, step))
    case("EX level", unit["nv_ex_level"] == 1, 1, unit["nv_ex_level"])
    case("EX keeps the instance", str(unit["instance_id"]) == str(iid), iid, unit["instance_id"])
    case("EX reply channel", out.get(EX_CLASS_UP_GROUP, [{}])[0].get("Qic38SuC", "").startswith(f"{iid}@"),
         f"{iid}@...", out.get(EX_CLASS_UP_GROUP, [{}])[0].get("Qic38SuC"))

    # 5b. EX+2, whose shipped reward is a kind-27 vision card: it must land in `vision_cards`.
    step2 = _nv_ex_map()[("100000327", "2")]
    fund(player, step2)
    apply_unit_awakening({}, EX_CLASS_UP_REQUEST, body(EX_CLASS_UP_GROUP, iid, step2))
    case("EX+2 level", unit["nv_ex_level"] == 2, 2, unit["nv_ex_level"])
    case("EX+2 card reward", any(str(c.get("vc_id")) == "900020101"
                                 for c in player.data.get("vision_cards", [])),
         "900020101", player.data.get("vision_cards"))

    # 5c. EX+3 is the cap and still the same instance.
    step3 = _nv_ex_map()[("100000327", "3")]
    fund(player, step3)
    apply_unit_awakening({}, EX_CLASS_UP_REQUEST, body(EX_CLASS_UP_GROUP, iid, step3))
    case("EX+3 level", unit["nv_ex_level"] == 3, 3, unit["nv_ex_level"])

    # 6. a wrong material tuple refuses with no spend and no master change.
    player, unit = fresh("100000317", 120)
    step = _class_up_map()["100000317"]
    fund(player, step)
    old_master = unit["master_id"]
    bad = json.dumps({NV_CLASS_UP_GROUP: [{"og2GHy49": unit["instance_id"],
                                           "KCk8u0am": "20:1:1", "Knra4I19": "0"}]})
    apply_unit_awakening({}, NV_CLASS_UP_REQUEST, bad)
    case("wrong materials change nothing", unit["master_id"] == old_master
         and player.item_count(20, 250000120) == 25, "master unchanged and cost intact",
         (unit["master_id"], player.item_count(20, 250000120)))

    return 1 if failed else 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    raise SystemExit("usage: ffbe_awaken.py --selftest")
