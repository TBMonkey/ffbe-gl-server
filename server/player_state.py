#!/usr/bin/env python3
"""player_state.py — the ONE place that holds the player.

Implements `docs/notes/player_state_schema.md`: the canonical player state, the mutation ops, and the
derivation of the served reply groups. `capture_server.py` owns transport (decrypt, dispatch, envelope);
this module owns the player. Replies are a PROJECTION of this state (§2.1/§2.3 of the schema), so a value
that can be derived (the NRG cap from `F_TEAM_LV_MST`, unit stats from `F_UNIT_MST`) is never stored.

Mechanism (schema §1) — three knobs, all decided at construction:
  * mode `live`      — mutations apply and are written back to the slot
  * mode `ephemeral` — mutations apply in memory only; the file on disk is untouched
  * mode `frozen`    — mutations are refused; every reply is computed from the loaded base
  * an OVERLAY (dotted-path `--set` / a JSON file) is applied to the in-memory base before serving and is
    reverted by the ordinary `live` autosave; only an explicit `--state-save` (force=True) keeps it.

Not here: the client's `shared_prefs` half of a checkpoint (schema §1.5 — the harness snapshots it).
"""
from __future__ import annotations

import calendar
import copy
import json
import os
import random
import sys
import tempfile
import time

import data_paths   # the data-root contract (`FFBE_DATA` / `--data`); see its docstring

# With no data root these are exactly the checkout-relative paths this module always used.
MST_DIR = data_paths.MST_DIR
TEXT_DIR = data_paths.TEXT_DIR   # localized text tables (the unit NAMES live here)

SCHEMA_VERSION = 2
MODES = ("live", "ephemeral", "frozen")

# The six `UserUnitInfo` stat lists, in wire order (HP/MP/ATK/DEF/MAG/SPR). In `F_UNIT_MST` each is a
# comma-separated tuple: slot 1 = Lv-1, slot 2 = maxLv (verified), intermediate levels need
# `F_UNIT_GROW_MST` (not wired yet -- see the schema §6).
STAT_TOKENS = ("em5hx4FX", "L0MX7edB", "o7Ynu1XP", "6tyb58Kc", "Y9H6TWnv", "sa8Ewx3H")

# `F_UNIT_MST` key column and the two rank-table columns.
UNIT_KEY = "3HriTp6B"
LB_KEY = "PR2HIc71"        # F_LIMITBURST_MST's unit-id column
EXP_PATTERN_KEY = "0W7g6YXS"   # F_UNIT_MST: the unit's F_UNIT_EXP_PATTERN_MST pattern id
EXP_LEVEL = "7wV3QZ80"
EXP_NEED = "93fnYUJG"
# F_UNIT_GROW_MST, keyed (maxLv, growthType, level) -> per-level percentage. NB `aU3o2D4t` and `uk6Wh8mw`
# are `F_UNIT_MST`'s maxLv/growthType tokens but are the two KEY columns here -- same token, different
# table, so read the consumer (docs/analysis/RE_VERIFICATION_IDIOMS.md #26).
GROWTH_MAXLV, GROWTH_TYPE, GROWTH_LEVEL, GROWTH_RATE = "aU3o2D4t", "uk6Wh8mw", "7wV3QZ80", "UjKF93ok"
_GROWTH_TABLE: dict | None = None
TEAM_LEVEL_KEY = "7wV3QZ80"
TEAM_NRG_CAP = "B6kyCQ9M"       # the rank's NRG cap (also the runtime field token)
TEAM_NEED_EXP = "B6H34Mea"      # the exp threshold to advance from this rank

# ---------------------------------------------------------------------------------------------------
# THE ORB FAMILIES -- the four point/max/restTime triples on `UserTeamInfoResponse` (APP-34). Model home:
# docs/notes/home_screen_payloads.md §9.2 (which home tile reads which family) and
# docs/notes/unit_party_payload_trees.md §9 (the token map). The STATE stores `point` + `anchor_utc`
# only; the max and the countdown are DERIVED (`orb_view`), exactly as the NRG cap is derived from the
# rank table (`nrg_cap`).
#
# Recovery RATES are SHIPPED DATA -- `F_DEFINE_MST`, columns `AK8Nn7ta` = key / `YjIUDJ97` = value
# (decoded at the workspace's read-only /home/thetb/projects/ffbe/mst_decoded/F_DEFINE_MST.ndjson):
RECOVER_AMOUNT_COLOSSEUM_ORB = 10    # F_DEFINE_MST "コロシアムオーブ回復量"
RECOVER_TIME_COLOSSEUM_ORB = 3600    # F_DEFINE_MST "コロシアムオーブ回復時間"
RECOVER_TIME_EVENT_ORB = 3600        # F_DEFINE_MST "イベントオーブ回復時間"
RECOVER_TIME_ARMS_ORB = 3600         # F_DEFINE_MST "アームズオーブ回復時間"
RECOVER_TIME_TFA_ORB = 3600          # F_DEFINE_MST "TFA Orb recover speed"
RECOVER_TIME_ACTION = 300            # F_DEFINE_MST "行動力回復時間" -- the NRG regen rate (below)
#
# The other three families ship NO RECOVER_AMOUNT_* row, so their one-per-tick amount is AUTHORED (the
# classic one-orb-per-recovery behaviour) and named so it cannot read as recovered.
AUTHORED_ORB_AMOUNT_EVENT = 1
AUTHORED_ORB_AMOUNT_ARMS = 1
AUTHORED_ORB_AMOUNT_TFA = 1
#
# EVERY cap is AUTHORED -- the gacha-rates rule: an authored value must never read as recovered. No
# shipped table ties a max to `getClsmOrbPointMax()` or to the other three orb objects; the lone
# `F_RB_DEFINE_MST` row's `XfWEsM97` = 50 is a SCORING parameter (MaxDamage/DamageRate/TotalPoint*), not
# an orb accessor, so the arena's cap is authored too -- but 10 is constrained by OBSERVATION, and that
# is stronger than a shipped row: `RbRankingScene::setLayout` draws one button per orb up to this cap,
# looking up `rb_ranking_btn_orb<N>`, and the client's own layout ships only `rb_ranking_btn_orb1..orb10`
# (cpk_extract/apk/layout/layout/rankingbattle/layout_rb_ranking_btn.csv). ANY cap above 10 therefore asks
# for a button that does not exist -- the lookup returns null and the next call dereferences it: SIGSEGV,
# si_addr 0, in GLThread, reproduced three times and captured live 2026-10-12 (APP-37;
# docs/notes/rb_ranking_scene_crash.md §7). The 50 this replaces was authored with no shipped source, and
# the captured loop bound held exactly 50. The event families keep the same 10 -- our reserve size, now
# equal to the arena's rather than larger than it. Runtime knob for the A/B: ORB_CAP_COLOSSEUM.
AUTHORED_ORB_CAP_COLOSSEUM = int(os.environ.get("ORB_CAP_COLOSSEUM", "10"))
AUTHORED_ORB_CAP_EVENT = 10
AUTHORED_ORB_CAP_ARMS = 10
AUTHORED_ORB_CAP_TFA = 10

ORB_FAMILIES = {
    # state family   wire tokens (point, max, restTime)               amount / period / cap
    "clsm":       {"tokens": ("xDm19iGS", "zo76TkaG", "T1J8Avne"),
                   "recover_amount": RECOVER_AMOUNT_COLOSSEUM_ORB,
                   "recover_time": RECOVER_TIME_COLOSSEUM_ORB,
                   "cap": AUTHORED_ORB_CAP_COLOSSEUM},
    "event":      {"tokens": ("Ddsg1be4", "Le8Z5cS0", "zR0pYjb2"),
                   "recover_amount": AUTHORED_ORB_AMOUNT_EVENT,
                   "recover_time": RECOVER_TIME_EVENT_ORB,
                   "cap": AUTHORED_ORB_CAP_EVENT},
    "equip_grow": {"tokens": ("9vBE36PW", "n3G5ItVX", "UIL6Ft1T"),
                   "recover_amount": AUTHORED_ORB_AMOUNT_ARMS,
                   "recover_time": RECOVER_TIME_ARMS_ORB,
                   "cap": AUTHORED_ORB_CAP_ARMS},
    "tfa":        {"tokens": ("rq9wWrt0", "K3NhECDq", "gEyJKQBa"),
                   "recover_amount": AUTHORED_ORB_AMOUNT_TFA,
                   "recover_time": RECOVER_TIME_TFA_ORB,
                   "cap": AUTHORED_ORB_CAP_TFA},
}

# ---------------------------------------------------------------------------------------------------
# THE ORB RECHARGE ON THE SHOP USE REQUEST (APP-34, 2026-10-11). The Arena's "recover orbs" confirm,
# observed live at 20:18:32-0500 as reply id `73SD2aMR` / url token `w76ThDMm`, sent:
#     {"L5Nj3cKE":[{"gdR2tJI9":"21"}],"LhVz6aD2":[...]}
# and was answered EMPTY ("reply EMPTY for 73SD2aMR (ShopUseRequest) -- the client got an empty body"),
# which the client shows as the connection error; the operator retried at 20:22:38 and 20:22:41 with the
# same shopId.
#
# THE REQUEST, READ FROM THE BINARY (all vaddrs are libgame.so v9.0.0; .text VMA == file offset):
#   * `ShopUseRequest::createBody` 0x198be54 -> `addGroup("L5Nj3cKE")` -> one node ->
#     `addParam<int>("gdR2tJI9", [this+0x30])`, then two string params (`Qy5EvcK1` from +0x38,
#     `Vt2I4RjX` from +0x50; the accessor symbols are `getPossession`/`getAddInfo`).
#   * `ShopUseRequest::getShopId` 0x198befc = `ldr w0,[x0,#0x30]`, `setShopId` 0x198bf04 =
#     `str w1,[x0,#0x30]` -> **`gdR2tJI9` is the SHOP ID (int)**, and the live body's only field is 21.
#   * WHO SENDS 21: `ClsmVsCpuStartScene::confirmAnswerYes` 0x208b8f4 constructs a `ShopUseRequest`,
#     calls the vtable slot `[vptr+0x80]` with `w1=0x15` (21) -- the reloc at 0x36506e8 is
#     `ShopUseRequest::setShopId` -- and the very next statement names the same action
#     "Recover Colosseum Orb" while reading `DefineMst::getIntValue("CONTINUE_COST")` for its price.
#     So 21 IS the arena orb recharge at the instruction level, not an inference from the id.
#
# THE SHOP TABLE (`F_SHOP_MST`, keyed by `gdR2tJI9` -- the request's own token, one tag both
# directions): row 21 = `コロシアムオーブ回復` (Colosseum orb recovery), with
#   * `8f7mxN3j` = **`ShopMst::getUseDia`** -- the LAPIS price. Proof: `ShopMstResponse::readParam`
#     0x1985efc is `atoi` -> `str w0,[x22,#0x28]`, and `ShopMst::getUseDia` 0x1d27fb4 is
#     `ldr w0,[x0,#0x28]` (`setUseDia` 0x1d27fbc is the pair). Row 21's value is 100.
#   * `xHZj7Va5` = `ShopMst::getParam` (+0x30): row 21 has "0"; the ITEM-cost restores (9000 Arena
#     Restore, 9001 Raid Restore, 9005 TFA Restore, 24/25, 50) carry `23:<itemId>` here AND useDia 0,
#     which is why a zero useDia is refused by name rather than treated as free.
# The price is therefore SHIPPED DATA, and the client's own confirm dialog agrees: `CONTINUE_COST` in
# `F_DEFINE_MST` ("コンティニューコスト") is also 100. NOTHING in the price/rate/cap of this feature
# is authored.
#
# SCOPE. The shopId -> state-family map below is OURS (the row names the orb, the state has four family
# keys), and the REPLY SHAPE is INFERRED: no `ShopUseResponse` class exists (`request_api_map.json`:
# "No ShopUseResponse class exists"), the 33 sender scenes' handlers are `checkConnectResult`-only, and
# the operator has not retried against this build. The reply carries the team row (the orb tokens) so
# `apply_to_reply`'s full projection has a `USER_STATE_GROUPS` member to fire on; that projection then
# supplies the lapis row beside it. A retry that still errors names the next token, and that decides
# the real shape.
SHOP_MST_FILE = "F_SHOP_MST.ndjson"
SHOP_ID_TOKEN = "gdR2tJI9"        # shopId (ShopMst+0x24), the SAME token the request body uses
SHOP_NAME_TOKEN = "G4L0YIB2"      # the row's display name
SHOP_USE_DIA_TOKEN = "8f7mxN3j"   # ShopMst::getUseDia -- the LAPIS price
SHOP_PARAM_TOKEN = "xHZj7Va5"     # ShopMst::getParam -- "0" or "23:<itemId>"
SHOP_USE_REQUEST = "73SD2aMR"     # ShopUseRequest (url token w76ThDMm)

# shopId -> the state orb family the shipped row refills. The ROW names the orb; our four family keys
# are the internal ones, so the join is ours:
#   21 = コロシアムオーブ回復 -> clsm      | 22 = イベントオーブ回復 -> event
#   23 = 武具強化イベントオーブ回復 -> equip_grow | 9004 = "TFA orb restore" -> tfa
# Rows 24/25/9000/9001/9005 are ITEM-cost restores (useDia 0) and 20 is the NRG refill; neither is an
# orb-family lapis spend, so both are refused by name rather than silently ignored.
SHOP_ORB_FAMILY = {"21": "clsm", "22": "event", "23": "equip_grow", "9004": "tfa"}


def shop_mst(shop_id) -> dict | None:
    """One `F_SHOP_MST` row, keyed by its own `gdR2tJI9` (the request body's token)."""
    return _load_ndjson(SHOP_MST_FILE, SHOP_ID_TOKEN).get(str(shop_id))


# ---------------------------------------------------------------------------------------------------
# The BEAST (= Esper, 幻獣) tables. One home: docs/notes/beast_esper_surface.md. `F_BEAST_STATUS_MST` is
# keyed (EsperId, Rarity); its six `min,max` pairs use the SAME column tokens as `F_UNIT_MST`'s stat
# vectors (HP/MP/ATK/DEF/MAG/SPR) and feed the wire `Monster*` fields; the level -> value curve is
# `F_BEAST_GROW_MST` (maxLv, growthType, level) -> percent of the min..max range.
BEAST_ID = "Iwfx42Wo"
BEAST_RARE = "9fW0TePj"
BEAST_MAXLV = "aU3o2D4t"
BEAST_GROWTH_TYPE = "uk6Wh8mw"
BEAST_EXP_PATTERN = "0W7g6YXS"
BEAST_CP_KEY = "5hqFc4ey"
BEAST_CP_ADD = "hbm8t3uK"
# The wire tokens for the six esper stats, in the same HP/MP/ATK/DEF/MAG/SPR order as STAT_TOKENS.
BEAST_WIRE_STAT_TOKENS = ("fh31sk7B", "PC97pWQj", "9NBV1XCS", "Pn5w9dfh", "84FJM3PD", "41fbhtj2")
# The F_FIXED_BEAST_MST board mask (the shipped Story-Digest preset — NOT a fresh esper, and not literally
# every piece: under the client's nibble-array encoding it sets bits 1..60).
BEAST_BOARD_ALL = "7ffffffffffffff8"
# A freshly obtained esper has NO board pieces. It must be the explicit string "0": `_beast_piece_rows` writes
# `str(b.get("board") or BEAST_BOARD_ALL)`, so an EMPTY string silently reads as ALL-unlocked (2026-10-11,
# when the tower-top Siren arrived with her whole board open).
BEAST_BOARD_NONE = "0"
BEAST_DECK_SLOTS = 5             # UserBeastDeckInfo::getMaxUnitNum()

# The BOARD (piece) progression — `F_BEAST_BOARD_PIECE_MST` (APP-26). `wY0Wj9Bt` = the piece row id (the
# request's CSV carries these), `xFsTMd83` = the MASK BIT index into `UserBeastPieceInfo`, `0A1BkNWb` =
# the CP cost (this table's own token — the beast's CP shares the token at another offset, idiom A5),
# `9fW0TePj` = the required rarity, `XHEZ47ke` = `unlockCost` (the LOCK pieces' item count).
BEAST_PIECE_ID = "wY0Wj9Bt"
BEAST_PIECE_BIT = "xFsTMd83"
BEAST_PIECE_CP_COST = "0A1BkNWb"
BEAST_PIECE_RARE = "9fW0TePj"
BEAST_PIECE_UNLOCK_COUNT = "XHEZ47ke"
# What a lock-piece open spends (`BeastBoardScene::updateEvent` `0x205be20`):
# `GameUtils::subItemNum(0x14, 0x0660C448, pieceRow.unlockCost)` — kind 20, this item, N copies — then the
# unlock anime. `0x0660C448` = **107005000**, which HAS an `F_ITEM_MST` row (`励輝晶`), and the confirm
# window checks the count with `GameUtils::getItemNum(0x14, 107005000, …)` (`0x2058f40`) before the tap — so
# the 48 unlock-cost pieces ARE reachable: `--grant-item 107005000 --grant-count N` seeds the cost.
# CORRECTED 2026-10-11 by an adversarial audit: this constant was first written `"107037768"`, the decimal
# of the WRONG hex (`0x06614448`, formed nowhere in the binary); every "no row in the decoded tables" claim
# in that version was an artefact of the bad conversion, not a fact about the game.
BEAST_UNLOCK_ITEM_ID = "107005000"
# The magicite (`F_ITEM_EXT_BEAST_MST`): per item, the exp and CP ONE unit grants, plus `Mnm4UZT9`, a
# `beastId:percent` rate list (default 100%). `BeastUtil::giveBeastBait` `0x1a557cc` applies
# `gain = round(value * rate/100) * count` — per unit, then multiplied.
BEAST_ITEM_ID = "tL6G9egd"
BEAST_ITEM_EXP = "B6H34Mea"
BEAST_ITEM_CP = "0A1BkNWb"
BEAST_ITEM_RATE = "Mnm4UZT9"

# `F_MISSION_MST` -> `F_SWITCH_MST`: a mission's own cleared flag is a switch id (`dIPkNn61`), and its
# (type, bit index) live in the switch table. `clear_mission` uses these to grant it (schema §2.3).
MISSION_KEY = "qo3PECw6"
MISSION_OPEN_SWITCH = "dIPkNn61"
SWITCH_KEY = "8vAYn3IX"
SWITCH_TYPE = "5opcUT9t"
SWITCH_INDEX = "QLfe23bu"
MISSION_SWITCH_TYPE = "101"      # the switch type mission flags live in (SWITCH_TYPE_MISSION, capture_server)

# ---------------------------------------------------------------------------------------------------
# The authored STORY-UNLOCK sets (`STORY_UNLOCK=vortex,espers`). A synthetic server CHOOSES a world
# state; these are the switch bits retail grants through story beats we deliberately skip (the beats
# exist in `F_STORY_SUB_MST`/`F_MAP_EXT_RESOURCE_MST` -- see docs/notes/pack_coverage.md §6).
#
#   vortex  switch `10005001` (type 100 index 8) gates `F_FUNCTION_MST FUNCTION_REEL_DIMENSION`, the
#           HOME `HOME_MAINMENU_DIMENSIONS_ICON` (Vortex) tile, and `F_DUNGEON_MST` dungeon 20001 (異界);
#           `10004001` (idx 6) gates the 異界 picture-story (`F_PICTURE_STORY_MST` 8000). NEITHER bit is
#           granted by a shipped table -- `F_STORY_SUB_MST` has no 9001 row, and the 異界 town's
#           `F_MAP_EXT_RESOURCE_MST` rows are a resource download/ignore gate, not a grant
#           (docs/notes/vortex_esper_unlock.md §1 + docs/notes/mapextresource_gate.md). Both are
#           server-authoritative, so granting them is the authored choice.
#   espers  switch `10007400` (type 101 index 14) gates `FUNCTION_REEL_BEAST` (幻獣 / BEAST). Retail
#           grants it at セイレーンの塔 (`F_STORY_SUB_MST` id 104). Owning an esper is a SEPARATE serve
#           (the `UserBeastInfoResponse` projection) -- the switch only opens the screen.
#   chamber switch `18900300` (type 101 index 287, read out of `F_SWITCH_MST`) is the THIRD tier of the
#           Chamber of the Fallen's own chain, and it is what the first Advanced Quest needs: quest `110101`
#           "Clear Attack of Gilgamesh in the Chamber of the Fallen!" is condition `15:8991106`, and mission
#           `8991106`'s required switch is `18900300`, which retail grants by clearing `8991103`. All three
#           predecessors' battle bundles ship (8991101/2/3/5/6), so the honest route is three fights; this set
#           exists so a validation run can reach the fight without them. **It opens a door and completes
#           nothing** -- no quest counter moves, because a counter is only ever bumped by a real
#           `MissionEndRequest` (idiom 43: a fabricated value must never outrank a recorded fact, and the
#           claim reads the STATE, never a switch).
#
# Values are switch INDICES within a type; the id -> (type, index) mapping is `F_SWITCH_MST` (reproduce a
# reverse map with `mst_decoded/F_SWITCH_MST.ndjson`, columns SWITCH_KEY/SWITCH_TYPE/SWITCH_INDEX).
STORY_UNLOCK_SETS = {
    "vortex": {"100": (6, 8)},   # 10004001 + 10005001
    "espers": {"101": (14,)},    # 10007400
    "chamber": {"101": (287,)},  # 18900300 -- the Chamber of the Fallen's third tier
}


def story_unlock_bits(names) -> dict:
    """`['vortex', ...]` -> `{type: {indices}}`, skipping unknown names (the caller reports them)."""
    out: dict[str, set] = {}
    for name in names:
        for stype, bits in STORY_UNLOCK_SETS.get(str(name).strip().lower(), {}).items():
            out.setdefault(str(stype), set()).update(bits)
    return out


def map_ext_unservable_ignores(is_served) -> list:
    """SW-110: the `ignoreSwitchId` of every `F_MAP_EXT_RESOURCE_MST` row whose `Y4PER0gB` resources we
    cannot serve.

    The client emits a MapExt row iff `eval(downloadSwitchId)` and NOT `eval(ignoreSwitchId)`, and a skipped
    row is **not downloaded** (`docs/notes/mapextresource_gate.md`). So granting the row's `ECcDwp14` switch
    makes the client drop ids it would otherwise 404 on — the town still loads, its missing NPC sub-scenes are
    simply never requested. `is_served(rel)` is a callback that resolves the CDN path against the overlay and
    the CDN root. `''` (ungated) and `'0'` (always-skip) carry no switch to grant, so those rows are left
    alone (they are the unsuppressible set the gate note warns about).
    """
    res, ver = {}, {}
    for row in _load_ndjson("F_RESOURCE_MAP_MST.ndjson").get("__rows__", []):
        res[str(row.get("Nnb20keZ"))] = (str(row.get("3LHdc6Qa") or ""), str(row.get("pJ56sAEo") or ""))
    for row in _load_ndjson("F_RESOURCE_MAP_VERSION_MST_LOCALIZE.ndjson").get("__rows__", []):
        ver[str(row.get("Nnb20keZ"))] = str(row.get("OzT5lSz3") or "")
    out: list = []
    for row in _load_ndjson("F_MAP_EXT_RESOURCE_MST.ndjson").get("__rows__", []):
        sw = str(row.get("ECcDwp14") or "")
        if not sw or sw == "0":
            continue
        # Only the サブ (sub) switch types are safe to grant: type 200/201 gate map/event sub-content.
        # Type 101 is メイン_S (main story) — granting one would advance the story (the sequence break we
        # refuse), so a row gated by a main-story switch is left alone even if its resource is unserved.
        pos = switch_position(sw)
        if not pos or pos[0] not in ("200", "201"):
            continue
        for rid in [x for x in str(row.get("Y4PER0gB") or "").split(",") if x]:
            sub, fn = res.get(rid, ("", ""))
            if not sub or not fn:
                out.append(sw)
                break
            name = f"Ver{ver.get(rid)}_{fn}" if ver.get(rid) else fn
            if not is_served(f"common_lang/sd/{sub}/{name}"):
                out.append(sw)
                break
    return sorted(set(out))

# ---------------------------------------------------------------------------------------------------
# Shipped-table access (lazy, cached). These tables are the DERIVATION source; never written here.
# ---------------------------------------------------------------------------------------------------
_TABLES: dict = {}


def _load_ndjson(fname: str, key_token: str | None = None) -> dict:
    # Cache key includes key_token: the SAME file loaded unkeyed (`{"__rows__": …}`) and keyed
    # (`{id: row}`) are different shapes, and caching by filename alone let the first shape shadow the
    # second -- `unit_mst()` returned None for every id after `unit_roots_by_rarity()` had loaded the file.
    cache_key = (fname, key_token)
    if cache_key in _TABLES:
        return _TABLES[cache_key]
    path = os.path.join(MST_DIR, fname)
    rows: dict = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if key_token:
                rows[str(row.get(key_token, ""))] = row
            else:
                rows.setdefault("__rows__", []).append(row)
    _TABLES[cache_key] = rows
    return rows


def unit_mst(master_id) -> dict | None:
    return _load_ndjson("F_UNIT_MST.ndjson", UNIT_KEY).get(str(master_id))


# F_VISION_CARD_MST's key token is the same `5giCMUd2` the vision-card responses use for the master id.
VISION_CARD_KEY = "5giCMUd2"


def vision_card_mst(vc_id) -> dict | None:
    return _load_ndjson("F_VISION_CARD_MST.ndjson", VISION_CARD_KEY).get(str(vc_id))


# F_ITEM_MST — the bag catalogue. Key token `tL6G9egd` is the SAME itemId token the bag reply carries
# (`UserItemInfoResponse` `0dNXQa3R`, docs/notes/item_state_tokens.md); the row also holds the name
# (`G4L0YIB2`) and the icon/sort fields the client draws from. 4729 rows; e.g. 101000100 = ポーション
# (Potion), 101000200 = ハイポーション. A bag row whose id is NOT in this table renders nameless and
# iconless, so every grant validates against it first (the `--grant-item` CLI).
ITEM_KEY = "tL6G9egd"


def item_mst(item_id) -> dict | None:
    return _load_ndjson("F_ITEM_MST.ndjson", ITEM_KEY).get(str(item_id))


# The EMBLEM catalogue. `F_EMBLEM_ITEM_MST`, 522 rows, id token `0w6XzofV` (the SAME token the
# `UserPlayerEmblemInfo` reply carries -- the MST row's key IS the emblem id), name `G4L0YIB2`,
# description `T7aEx3Mj`, sort `Z0EN6jSh`, trophy item `0HUPxDf1`. Ids run 10201..90190.
# `EmblemMstList::getMstName` (0x1c61464) returns this table's name; the CDN file is
# `Ver158_SuM2XA05.dat` under config key `UyE6HP3h`. `EmblemMstResponse` exists as a class but has NO
# factory tag, so the list cannot arrive by reply -- it is MST-driven, which is why the select screen
# renders 522 rows off the shipped table alone. See docs/notes/player_emblem_select.md.
EMBLEM_MST_FILE = "F_EMBLEM_ITEM_MST.ndjson"
EMBLEM_ID_TOKEN = "0w6XzofV"


def emblem_mst(emblem_id) -> dict | None:
    return _load_ndjson(EMBLEM_MST_FILE, EMBLEM_ID_TOKEN).get(str(emblem_id))


def emblem_mst_ids() -> list:
    """Every shipped emblem id, in table order (10201 .. 90190)."""
    return [int(i) for i in _load_ndjson(EMBLEM_MST_FILE, EMBLEM_ID_TOKEN)]


# The EMBLEM EVENT campaign tables. TWO shipped tables, joined by MILESTONE ID (`sVhVVRu8` =
# `AAwKyVw6`):
#   * F_EMBLEM_WEEKLY_MST  (561 rows / 48 campaigns) -- the OBJECTIVES: what earns a crest, per milestone;
#   * F_EMBLEM_MONTHLY_MST (392 rows / 48 campaigns) -- the TIER REWARDS: what a crest count pays out, in
#     two columns (free / passholder).
# Confirmed against the community schedule 2026-10-12: group 9000001 IS the live "Monthly Rewards" table,
# row for row, and seven other shipped groups match its older variant -- so the monthly rotation is real.
# One home for the evidence: docs/notes/emblem_event.md.
EMBLEM_WEEKLY_MST_FILE = "F_EMBLEM_WEEKLY_MST.ndjson"
EMBLEM_MONTHLY_MST_FILE = "F_EMBLEM_MONTHLY_MST.ndjson"
EMBLEM_WEEKLY_CAMPAIGN = "7isPgqH6"          # campaign (group) id -- the same id space as the monthly table
EMBLEM_WEEKLY_MILESTONE = "sVhVVRu8"         # milestone id: the join key (and the wire's milestone token)
EMBLEM_WEEKLY_TYPE = "7f307af5"              # 1 = a point target, 2 = a clear target
EMBLEM_WEEKLY_TARGET = "a67a5279"            # what to do: a MISSION id (type 2) or a banner/season id (type 1)
EMBLEM_WEEKLY_COUNT = "52c7815c"             # the target's count: 1 for a clear, points otherwise
EMBLEM_MONTHLY_CAMPAIGN = "a75e27e0"         # campaign (group) id
EMBLEM_MONTHLY_MILESTONE = "AAwKyVw6"        # the SAME milestone id as the weekly table's
EMBLEM_MONTHLY_CRESTS = "508f00a5"           # the crest count this row's rewards are paid AT
EMBLEM_MONTHLY_FREE = "b4501f90"             # the free-track reward spec
EMBLEM_MONTHLY_PASS = "5079e3f7"             # the passholder-track reward spec
# A crest is an IMPORTANT item: `F_IMPORTANT_ITEM_MST` id 20 (`クレスト` / "Crest"; the GL name is in the
# datamine, `ffbe_data/gl/important.json`), carried in the type-23 bag `important_item_rows` serves.
EMBLEM_CREST_TYPE = 23
EMBLEM_CREST_ITEM = 20
# The LIVE campaign. Group 9000001 is the row-for-row match to the community's live table (2026-10-12);
# seven other shipped groups match its older variant, so the rotation is real and which one is live is a
# CHOICE, like every other server-authoritative value. `EMBLEM_CAMPAIGN=<group>` picks another.
EMBLEM_DEFAULT_CAMPAIGN = "9000001"


def emblem_campaign() -> str:
    """The live campaign's group id: the authored lever, else the default."""
    return (os.environ.get("EMBLEM_CAMPAIGN") or "").strip() or EMBLEM_DEFAULT_CAMPAIGN


def emblem_weekly_mst(campaign=None) -> list:
    """The campaign's OBJECTIVE rows (`F_EMBLEM_WEEKLY_MST`), in table order."""
    want = str(campaign or emblem_campaign())
    return [r for r in _load_ndjson(EMBLEM_WEEKLY_MST_FILE).get("__rows__", [])
            if str(r.get(EMBLEM_WEEKLY_CAMPAIGN, "")) == want]


def emblem_monthly_mst(campaign=None) -> list:
    """The campaign's TIER-REWARD rows (`F_EMBLEM_MONTHLY_MST`), in table order."""
    want = str(campaign or emblem_campaign())
    return [r for r in _load_ndjson(EMBLEM_MONTHLY_MST_FILE).get("__rows__", [])
            if str(r.get(EMBLEM_MONTHLY_CAMPAIGN, "")) == want]


def limit_burst(master_id) -> dict | None:
    """The unit's `F_LIMITBURST_MST` row, keyed the way the CLIENT keys it: by the unit's STATIC LB id.

    That id is `F_UNIT_MST` `PR2HIc71` -- the same token as this table's key, i.e. `UnitMst::limitBurstID`
    at `+0xd0`, which `BattleCharacter::setLimitBurstID` passes to `LimitBurstLvMstList::getObject`
    (`docs/notes/limit_burst_path.md` §1b/§2). It is **not always the unit's own id**: an NV/EX row points
    at its BASE row's LB (`401016737` -> `401016717`, `401016627` -> `401016607`). Keying by `master_id`
    returned None for every such row, so `_unit()` served `lb_level = 0` and the LB activation crashed in
    `BattleCharacter::reserveLimitBurstAction` -- a `std::string` copy from NULL+0x28, captured live
    2026-10-10 (24 rows in the max roster; the availability check is level-independent, so the button was
    still offered). Falls back to the unit id when the row has no/0 column.
    """
    row = unit_mst(str(master_id)) or {}
    lb_id = str(row.get(LB_KEY) or "")
    if lb_id in ("", "0"):
        lb_id = str(master_id)
    return _load_ndjson("F_LIMITBURST_MST.ndjson", LB_KEY).get(lb_id)


def exp_pattern(master_id) -> dict:
    """`{level: cumulative exp threshold}` for a unit's exp pattern (`F_UNIT_EXP_PATTERN_MST`)."""
    pid = str((unit_mst(master_id) or {}).get(EXP_PATTERN_KEY, ""))
    out: dict = {}
    for row in _load_ndjson("F_UNIT_EXP_PATTERN_MST.ndjson").get("__rows__", []):
        if str(row.get(EXP_PATTERN_KEY, "")) == pid:
            try:
                out[int(row.get(EXP_LEVEL, 0))] = int(row.get(EXP_NEED, 0))
            except (TypeError, ValueError):
                continue
    return out


def beast_status(esper_id, rarity) -> dict | None:
    """`F_BEAST_STATUS_MST` row for (EsperId, Rarity) — the stat envelope, maxLv, growth type, exp/cp keys."""
    key = (str(esper_id), str(rarity))
    for row in _load_ndjson("F_BEAST_STATUS_MST.ndjson").get("__rows__", []):
        if (str(row.get(BEAST_ID)), str(row.get(BEAST_RARE))) == key:
            return row
    return None


def beast_growth_rate(max_lv, growth_type, level) -> int:
    """`F_BEAST_GROW_MST` (maxLv, growthType, level) -> percent of the min..max range; -1 when absent.

    The shipped `maxLv=60` rows are `0,2,3,…,60` (`grow% = level`), so L60 gets 60% — that is what the
    client's own `BeastUtil::setBeastToLv` computes from the same table (beast_esper_surface.md §2).
    """
    key = (str(max_lv), str(growth_type), str(level))
    for row in _load_ndjson("F_BEAST_GROW_MST.ndjson").get("__rows__", []):
        if (str(row.get(GROWTH_MAXLV)), str(row.get(GROWTH_TYPE)), str(row.get(GROWTH_LEVEL))) == key:
            try:
                return int(row.get(GROWTH_RATE, -1))
            except (TypeError, ValueError):
                return -1
    return -1


def beast_total_exp(pattern_id, level) -> int:
    """Sum of `F_BEAST_EXP_PATTERN_MST` per-level costs for levels 2..level (the `X9ABM7En` value)."""
    pid = str(pattern_id)
    costs: dict[int, int] = {}
    for row in _load_ndjson("F_BEAST_EXP_PATTERN_MST.ndjson").get("__rows__", []):
        if str(row.get(BEAST_EXP_PATTERN, "")) == pid:
            try:
                costs[int(row.get(EXP_LEVEL, 0))] = int(row.get(EXP_NEED, 0))
            except (TypeError, ValueError):
                continue
    return sum(costs[lv] for lv in range(2, int(level) + 1) if lv in costs)


def beast_cp(cp_key, level) -> int:
    """Sum of `BeastCpMst::getAddCp` for levels 2..level: `F_BEAST_CP_MST.hbm8t3uK`, element [level-1]."""
    for row in _load_ndjson("F_BEAST_CP_MST.ndjson").get("__rows__", []):
        if str(row.get(BEAST_CP_KEY, "")) == str(cp_key):
            vals = str(row.get(BEAST_CP_ADD, "")).split(",")
            total = 0
            for lv in range(2, int(level) + 1):
                if lv - 1 < len(vals) and vals[lv - 1].strip():
                    try:
                        total += int(vals[lv - 1])
                    except ValueError:
                        pass
            return total
    return 0


def board_piece_rows() -> dict:
    """`F_BEAST_BOARD_PIECE_MST` keyed by the piece row id (`wY0Wj9Bt`) — the ids the board request's CSV
    carries. Cached by the loader (`_load_ndjson`'s cache key includes the token)."""
    return _load_ndjson("F_BEAST_BOARD_PIECE_MST.ndjson", BEAST_PIECE_ID)


def item_ext_beast(item_id) -> dict | None:
    """The magicite row for an item id (`F_ITEM_EXT_BEAST_MST`), or None — an item with no row grants
    nothing (`BeastUtil::returnNeedRateWithExpAndCP` `0x1a558d0` returns 100/0/0 when the lookup misses)."""
    return _load_ndjson("F_ITEM_EXT_BEAST_MST.ndjson", BEAST_ITEM_ID).get(str(item_id))


def beast_exp_need(pattern_id, level) -> int:
    """`F_BEAST_EXP_PATTERN_MST` cost of ONE level (`getObjectWithPatternIdAndLv`), 0 when absent — the
    value `BeastUtil::beastLvUp` `0x1a55b2c` subtracts from the residual exp."""
    pid = str(pattern_id)
    for row in _load_ndjson("F_BEAST_EXP_PATTERN_MST.ndjson").get("__rows__", []):
        if str(row.get(BEAST_EXP_PATTERN, "")) == pid and str(row.get(EXP_LEVEL, "")) == str(level):
            try:
                return int(row.get(EXP_NEED, 0))
            except (TypeError, ValueError):
                return 0
    return 0


def beast_cp_add(cp_key, level) -> int:
    """`BeastCpMst::getAddCp(level)` (`0x1c49bf0`): `hbm8t3uK` element `[level-1]` — what `beastLvUp`
    ADDS to `UserBeastInfo+0xe0` when the beast REACHES `level`."""
    for row in _load_ndjson("F_BEAST_CP_MST.ndjson").get("__rows__", []):
        if str(row.get(BEAST_CP_KEY, "")) == str(cp_key):
            vals = str(row.get(BEAST_CP_ADD, "")).split(",")
            idx = int(level) - 1
            if 0 <= idx < len(vals) and vals[idx].strip():
                try:
                    return int(vals[idx])
                except ValueError:
                    return 0
    return 0


def beast_item_rate(raw, esper_id) -> int:
    """The `Mnm4UZT9` list's rate for one esper, 100 when it has no entry (`BeastUtil` `0x1a559a0` scans
    the comma-split parts and takes the `beastId:percent` whose id matches)."""
    for part in str(raw or "").split(","):
        key, sep, val = part.partition(":")
        if not sep or key.strip() != str(esper_id):
            continue
        try:
            return int(val)
        except ValueError:
            return 100
    return 100


def beast_gain(value, rate) -> int:
    """`giveBeastBait`'s per-unit arithmetic: `frinta(value * rate/100.0)` — round to NEAREST, ties AWAY
    from zero — then `fcvtzs`. Python's `round()` is banker's rounding, so it is NOT this."""
    return int(float(value) * int(rate) / 100.0 + 0.5)


def board_bit(mask, bit) -> bool:
    """Test one bit of a `UserBeastPieceInfo` mask, in the CLIENT's own encoding.

    The wire string is a nibble array, NOT a hex number: character index `bit>>2` counted **from the
    LEFT**, and inside that character the bit is **`3 - (bit & 3)`** — i.e. position 0 is the nibble's
    MSB. Evidence, both directions:
      * reader `0x1a8a638`: `idx = bit>>2` (`0x1a8a66c`), `char = data[idx]` (`0x1a8a698`, `data` = the
        string's begin, short strings at object+1) → `strtol(char,16)`, build the 4-char scratch with
        `v&8`→index 0 … `v&1`→index 3 (`0x1a8a6dc`), then read `scratch[bit & 3]` (`0x1a8a6f8`);
      * setter `0x1a8a360`: same index/nibble arithmetic, and it GROWS the string by APPENDING '0' at the
        end (`0x1a8a438` end++, `0x1a8a440` `strh '0'`), so higher bits live further RIGHT.
    A bit beyond the string's length reads 0 (the reader returns '0', `0x1a8a680`), which is why the
    explicit `"0"` is a valid empty board. **Corrected 2026-10-11 by an adversarial audit:** the first
    version treated the string as a big-endian hex NUMBER (`len-1-(bit>>2)`, bit `bit&3`) — a plausible
    reading of the same bytes that reverses both axes, and a live tap would have set the wrong piece.
    A negative `bit` is a caller bug and reads False (the client's own negative handling wraps)."""
    s = str(mask or "")
    try:
        b = int(bit)
        idx = b >> 2
    except (TypeError, ValueError):
        return False
    if idx < 0 or idx >= len(s):
        return False
    try:
        return bool(int(s[idx], 16) & (1 << (3 - (b & 3))))
    except ValueError:
        return False


def board_set_bit(mask, bit) -> str:
    """Set one bit of a `UserBeastPieceInfo` mask, preserving the client's layout.

    Same encoding as `board_bit`: character `bit>>2` from the left, nibble bit `3 - (bit & 3)`. The string
    is EXTENDED with `'0'` on the RIGHT when the bit is past its end, exactly as the client's setter does
    (`0x1a8a434`), and it is never truncated — a leading `'0'` is a real nibble here, unlike in a number
    (so `"0004"` + bit 0 stays 4 characters, not 3). A 73-bit board therefore needs 19 digits."""
    b = int(bit)
    idx = b >> 2
    if idx < 0:
        raise ValueError(f"board_set_bit: negative bit {bit!r}")
    chars = list(str(mask or ""))
    if idx >= len(chars):
        chars.extend("0" * (idx + 1 - len(chars)))
    try:
        val = int(chars[idx], 16)
    except ValueError:
        val = 0
    chars[idx] = "0123456789abcdef"[val | (1 << (3 - (b & 3)))]
    return "".join(chars)


def beast_stats(esper_id, rarity, level) -> list:
    """The six wire stats at `level`: floor(min + (max-min)*grow%/100), level clamped to the status row."""
    row = beast_status(esper_id, rarity)
    if row is None:
        return [0] * 6
    try:
        lv = min(int(level), int(row.get(BEAST_MAXLV, level)))
    except (TypeError, ValueError):
        lv = int(level)
    pct = max(0, beast_growth_rate(row.get(BEAST_MAXLV), row.get(BEAST_GROWTH_TYPE), lv))
    out = []
    for tok in STAT_TOKENS:
        try:
            lo, hi = (int(x) for x in str(row.get(tok, "0,0")).split(",")[:2])
        except (TypeError, ValueError):
            lo = hi = 0
        out.append(int(lo + (hi - lo) * pct / 100))
    return out


def fixed_beast_presets() -> list:
    """The shipped `F_FIXED_BEAST_MST` Story-Digest preset (18 espers at R3/L60, all board pieces)."""
    out = []
    for row in _load_ndjson("F_FIXED_BEAST_MST.ndjson").get("__rows__", []):
        try:
            out.append({"esper_id": str(row.get(BEAST_ID)), "rarity": int(row.get(BEAST_RARE, 3)),
                        "level": int(row.get(GROWTH_LEVEL, 60)),
                        "board": str(row.get("4HtPJZth") or BEAST_BOARD_ALL)})
        except (TypeError, ValueError):
            continue
    return out


def max_level(master_id) -> int:
    """The unit's MAX LEVEL — `F_UNIT_MST` `aU3o2D4t` (the same column `unit_stats` passes to
    `growth_rate` as its curve key). Read in the binary at `UnitInfoBase::getMaxLv` `0x1dcbf34`, which
    ends `ldrb w0,[x19,#0x85]` = `UnitMst::getMaxLv` (`+0x85`, `setMaxLv` at `0x1cf93f0`) — the column
    right beside the rarity byte at `+0x84`.

    This is the PER-RARITY cap in the shipped data (15/30/40/60/80/100/120/130, and **1** for the
    Tough/Magic Pots, which cannot level at all). It is a single field on `UnitMst`, not a per-rarity
    table: `getMaxLv` only swaps the row when the unit has Neo-Vision shift state (`UnitMstList::
    getNeoVisionShiftUnit`, gated on `UnitInfoBase+0x340`), which nothing we serve sets.

    WHY IT MATTERS: a unit's `F_UNIT_EXP_PATTERN_MST` pattern is shared across its series' rarities —
    our 3★ fodder `203000303` has `aU3o2D4t = 40` but an **80-row** pattern — so an uncapped walk
    produces level 42/40 (observed live 2026-09-24 after a fusion). 0/absent = no cap (unknown unit).
    """
    try:
        return int((unit_mst(master_id) or {}).get(GROWTH_MAXLV, 0) or 0)
    except (TypeError, ValueError):
        return 0


def exp_cumulative(pat: dict, level: int) -> int:
    """The TOTAL exp to reach `level` = the sum of the per-level costs for 2..level.

    Needed because `F_UNIT_EXP_PATTERN_MST.93fnYUJG` is the cost of REACHING each level and RESETS --
    it is not a running total. Level 1 costs nothing, so the sum starts at 2.
    """
    return sum(int(pat[l]) for l in sorted(pat) if 1 < l <= level)


def max_level_exp(master_id) -> int | None:
    """The TOTAL exp at which progress PINS (the sum of the per-level costs up to `max_level`), or None
    when the max level is unknown or its pattern carries no row at that level.

    WAS `exp_pattern(master_id).get(mx)` — i.e. one level's step, read as if it were the total. Same
    misreading as `level_for_exp` below; both were fixed together on 2026-09-26.
    """
    mx = max_level(master_id)
    if mx <= 1:
        return 0 if mx == 1 else None
    pat = exp_pattern(master_id)
    if mx not in pat:
        return None
    return exp_cumulative(pat, mx)


def need_unit_exp(master_id, level) -> int:
    """`needExp(level)` for a unit -- the cost of the step FROM `level` TO `level+1`.

    This is the client's own lookup, recovered 2026-09-26 from `GameUtils::getNeedUnitExp` @`0x19deab4`:
    `ldrb [UnitMst+0x85]` (maxLv guard, returns -1 at the cap) -> `ldrb [UnitMst+0xe6]` (the exp pattern
    id, PER-UNIT) -> `getObjectWithPatternID(patternId, level)` -> `needExp` @`+0x2c`. It is read FORWARD
    only: the table is never inverted anywhere in the binary.

    Returns 0 when the pattern carries no row for that level, which is the safe direction -- a step of
    zero would loop forever in `credit_exp`, so callers cap on `max_level` too.

    NAMED `need_unit_exp`, not `need_exp`, because `need_exp(level)` below is the RANK table
    (`F_TEAM_LV_MST`) and has been here longer. Worth noting: the rank one was ALWAYS residual -- its
    docstring records `GameUtils::getRestUserExp` returning `need_exp(level+1) - exp` -- so the unit exp
    was the lone holdout still on the cumulative reading. Two halves of the same screen, two models.
    """
    return int(exp_pattern(master_id).get(int(level), 0) or 0)


def credit_exp(master_id, level: int, exp: int, amount: int):
    """Credit `amount` exp to a unit at (`level`, residual `exp`). Returns `(level, exp)`.

    **WHY THIS EXISTS, and why it replaces the old model.** The client does not derive a level from a
    running total: `UnitInfoBase::getLv()` @`0x1967d40` is `ldr w0,[x0,#0x50]; ret`, a bare field load, so
    the level is whatever the server sent, and `getRestUnitExp = needExp(level+1) - exp` makes `exp` a
    RESIDUAL WITHIN THE CURRENT LEVEL, not a cumulative sum. The previous `level_for_exp` assumed the
    opposite, so every unit we served produced a NEGATIVE "exp to next level" in the client
    (`needExp(21)=4415` against a served `exp=35709` -> -31294), and a battle's credit moved units several
    levels when the client showed one. That was measured live on 2026-09-26.

    So the credit walks FORWARD, mirroring how the client reads the table: fill the current level's step,
    and on crossing it, level up and continue with what is left. `total_exp` is NOT touched here -- it is a
    lifetime counter, and only `add_unit_exp` maintains it.
    """
    level, exp, amount = int(level), int(exp), int(amount)
    cap = max_level(master_id)
    if amount <= 0:
        return level, exp
    if cap > 0 and level >= cap:
        return level, exp                       # already at the cap: the counter pins (see add_unit_exp)
    while amount > 0:
        step = need_unit_exp(master_id, level + 1)
        if step <= 0:
            break                               # no row for the next level: do not spin
        room = step - exp
        if cap > 0 and level + 1 > cap:
            break
        if amount < room:
            exp += amount
            amount = 0
        else:
            amount -= room
            level += 1
            exp = 0
    return level, exp


def level_for_exp(master_id, total_exp) -> int:
    """The highest level whose CUMULATIVE SUM of per-level costs `total_exp` covers, CAPPED at `max_level`.

    **`93fnYUJG` is a PER-LEVEL STEP, not a running total.** Proved from our own decoded MST, not from a
    wiki: `sum(1..80)` for pattern 25 is **1,422,072** exactly, and `need@80 = 35,661` is the 79->80 step
    on its own. The previous version compared `total_exp >= pat[lv]` -- a running total against a single
    step -- which made every level reachable almost at once and served **~4x the correct level**: a unit
    holding 35,661 exp was shown at **80** when it had earned **20** (measured 2026-09-26 on the campaign
    save, after an operator run; the two units at "level 1" jumped to "level 80" in one chamber).

    The cap is still not cosmetic: the pattern table is per unit SERIES, so it carries rows above the
    current rarity's cap (see `max_level`), and walking into them served level 42/40.
    """
    pat = exp_pattern(master_id)
    cap = max_level(master_id)
    total = int(total_exp)
    level, run = 1, 0
    for lv in sorted(pat):
        if lv <= 1:
            continue
        if cap > 0 and lv > cap:
            break
        step = int(pat[lv])
        if run + step > total:
            break
        run += step
        level = lv
    return level


def mission_open_switch(mission_id) -> str | None:
    """`F_MISSION_MST.dIPkNn61`: the switch a mission OPENS when it is cleared.

    `"0"`/empty is the table's sentinel for "opens nothing", which is a real answer, not a missing one.
    """
    row = _load_ndjson("F_MISSION_MST.ndjson", MISSION_KEY).get(str(mission_id))
    if not row:
        return None
    sw = row.get(MISSION_OPEN_SWITCH)
    return str(sw) if sw is not None and str(sw) not in ("", "0") else None


def switch_position(switch_id) -> tuple[str, int] | None:
    """`F_SWITCH_MST` `8vAYn3IX` -> (`5opcUT9t` type, `QLfe23bu` bit index), or None if unknown."""
    row = _load_ndjson("F_SWITCH_MST.ndjson", SWITCH_KEY).get(str(switch_id))
    if not row:
        return None
    stype, idx = row.get(SWITCH_TYPE), row.get(SWITCH_INDEX)
    if stype is None or idx is None:
        return None
    try:
        return str(stype), int(idx)
    except (TypeError, ValueError):
        return None


STORY_BEAST_STATS: dict[str, tuple[int, int]] = {}   # no special cases: every story grant is FRESH
STORY_BEAST_DEFAULT = (1, 1)


def story_beast_stats(esper_id) -> tuple[int, int]:
    """The rarity/level a STORY-granted esper arrives with: the FRESH row — rarity 1, level 1.

    `F_BEAST_STATUS_MST` gives esper 1 (セイレーン) max level **30 at rarity 1**, 40 at 2, 60 at 3, and the fresh
    level-1 row derives exp 0 / CP 0 / the rarity-1 MINIMUM stats from the same tables (`beast_total_exp`,
    `beast_cp`, `beast_stats`) — so nothing here is authored. The R3/L60/all-board values are the
    **Story-Digest preset** (`F_FIXED_BEAST_MST`, the `BEAST_SEED` lever), a DIFFERENT path: 2026-10-11 the
    tower-top grant read that preset as "the authentic first-esper grant" and Siren arrived fully levelled with
    her whole board open. `STORY_BEAST_STATS` is the override map for a future non-fresh story grant; it is
    empty on purpose."""
    return STORY_BEAST_STATS.get(str(int(esper_id)), STORY_BEAST_DEFAULT)


def story_beast_for_mission(mission_id) -> int | None:
    """The ESPER a story beat grants, DERIVED from the beat's own switch list -- never hand-listed.

    `F_MISSION_MST.dIPkNn61` names every switch a clear grants, and 19 shipped missions carry one of the form
    `820<EE>100`: the middle two digits are the esper id. The derivation is self-checking, which is why it can
    be trusted as a rule rather than as 19 coincidences -- the location names agree with the esper they grant
    in every case: `1110404` セイレーンの塔・最上階 -> `82001100` -> 1 セイレーン · `1115005` 炎獣の座 ->
    `82002100` -> 2 イフリート · `1125204` 氷魔の寝所 -> `82003100` -> 3 シヴァ · `11215105` 月狼の巣 ->
    `82016100` -> 16 フェンリル · `11425105` 不死鳥の住処 -> `82014100` -> 14 フェニックス.

    The esper id is validated against `F_BEAST_MST` (rarity 1 must be a real row), so a switch that merely
    looks like the pattern is refused rather than guessed. Returns None when the beat grants no esper -- which
    is the honest answer for a clear that is not an esper beat.
    """
    sw = mission_open_switch(mission_id)
    if not sw:
        return None
    for raw in str(sw).split(","):
        sid = raw.strip()
        if len(sid) != 8 or not sid.startswith("820") or not sid.endswith("100"):
            continue
        ee = sid[3:5]
        if not ee.isdigit():
            continue
        if beast_status(str(int(ee)), 1) is not None:
            return int(ee)
    return None


_UNIT_ROOTS: dict | None = None
_UNIT_TOP_ROWS: dict = {}
_NV_SHIFT_TARGETS: set | None = None


_UNIT_NAMES: set | None = None
_POOL_DROP_NOTE = [False]


def named_unit_ids() -> set:
    """The unit ids that have a shipped `MST_UNIT_NAME_<id>` row (from `text_decoded/F_TEXT_UNITS_NAME`).

    WHY THE POOL MUST BE FILTERED BY THIS. The result-presentation screens resolve each awarded unit's NAME
    through that key. A unit with no row yields a null the **"units obtained"** screen dereferences -- the
    client SIGSEGVs *after* an 11-draw while the 1-draw is fine, which is exactly SW-78. It was captured with
    `harness/catch_first_fault.gdb`: at the ORIGINAL fault the guest held the ASCII string "MST_UNIT".
    `F_UNIT_MST` ships 3311 ids but only 3089 are named, and `unit_roots_by_rarity()` -- what every tier
    expands from -- included **94** of the 222 nameless ones (5* 33, 4* 18, 7* 32, 3* 9, 1* 2).
    Cached for the process; an empty set (unreadable table) means DO NOT FILTER."""
    global _UNIT_NAMES
    if _UNIT_NAMES is None:
        ids: set = set()
        path = os.path.join(TEXT_DIR, "F_TEXT_UNITS_NAME.ndjson")
        try:
            prefix = "MST_UNIT_NAME_"
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    key = str(json.loads(line).get("key", ""))
                    if key.startswith(prefix):
                        ids.add(key[len(prefix):])
        except (OSError, ValueError) as exc:  # noqa: BLE001 - an unreadable text table must not empty the pool
            print(f"    pool: {path} unreadable ({exc!r}) -- NOT filtering the pool to named units", flush=True)
        _UNIT_NAMES = ids
    return _UNIT_NAMES


def _only_named(rows: list) -> list:
    """Drop pool rows whose unit has no shipped name row (see `named_unit_ids`). No-op if that set is empty."""
    names = named_unit_ids()
    if not names:
        return rows
    kept = [e for e in rows if str(e.get("unit_id")) in names]
    dropped = len(rows) - len(kept)
    if dropped and not _POOL_DROP_NOTE[0]:
        _POOL_DROP_NOTE[0] = True
        print(f"    pool: dropped {dropped} unit(s) with no MST_UNIT_NAME_ row (of {len(rows)}) -- "
              f"a nameless award crashes the 11-draw result screen (docs/QUEUE.md SW-78)", flush=True)
    return kept


def unit_roots_by_rarity() -> dict:
    """The BASE-rarity unit ids, keyed by rarity string: `F_UNIT_MST` rows whose master id is its own
    series root (`3HriTp6B == woghJa61`). These are what a tier-based pool expands to -- DERIVED from the
    shipped catalogue, never stored (one fact, one home). Cached for the process."""
    global _UNIT_ROOTS
    if _UNIT_ROOTS is None:
        out: dict = {}
        for r in _load_ndjson("F_UNIT_MST.ndjson").get("__rows__", []):
            if str(r.get(UNIT_KEY)) != str(r.get("woghJa61")):
                continue
            out.setdefault(str(r.get("9fW0TePj")), []).append(str(r.get(UNIT_KEY)))
        _UNIT_ROOTS = {k: sorted(v) for k, v in out.items()}
    return _UNIT_ROOTS


def nv_shift_targets() -> set:
    """Ids another `F_UNIT_MST` row's `JPW8Vs40` points at (self-references excluded) -- the shift FORMS.

    `JPW8Vs40` is the Neo-Vision shift id (`UnitMstList::getNeoVisionShiftUnit`, read at `0x1dcbf68`;
    `docs/notes/unit_stat_source.md` §5): the row CARRYING the id is the owned unit (MST flag `I3SKJ4q0=1`),
    the row it references is the form it becomes while shifted. Retail never puts a referenced row in a
    roster, so the max-roster sweep must not either -- granting both sides is what showed one character twice
    (operator, 2026-10-10). A self-referencing column (`JPW8Vs40 == own id`) means "no shift", not a pairing,
    and is excluded. Cached for the process.
    """
    global _NV_SHIFT_TARGETS
    if _NV_SHIFT_TARGETS is None:
        out: set = set()
        for r in _load_ndjson("F_UNIT_MST.ndjson").get("__rows__", []):
            mid = str(r.get(UNIT_KEY))
            tgt = str(r.get("JPW8Vs40") or "")
            if tgt and tgt not in ("0", mid):
                out.add(tgt)
        _NV_SHIFT_TARGETS = out
    return _NV_SHIFT_TARGETS


def has_brave_shift(master_id) -> bool:
    """True when the unit's own `F_UNIT_MST` row carries a REAL `JPW8Vs40` -- i.e. it owns a Brave-Shift form.

    This marks an NV base (the row that can shift), and is the set `--nv-ex` seeds: EX awakening is the NV
    units' upgrade path. A self-referencing column means "no shift", not a pairing.
    """
    r = unit_mst(str(master_id)) or {}
    tgt = str(r.get("JPW8Vs40") or "")
    return bool(tgt) and tgt not in ("0", str(master_id))


def top_named_unit_rows(rarity=None) -> list:
    """One `F_UNIT_MST` row per unit SERIES: the family's highest tier that has a shipped name row.

    The family key is `woghJa61` -- the series root `unit_roots_by_rarity` groups by -- so every rarity
    tier of one unit collapses to a single pick, made on `(maxLv, rarity, id)`. Nameless tiers are skipped:
    a granted unit with no `MST_UNIT_NAME_<id>` row SIGSEGVs the award screen (SW-78, `named_unit_ids`), and
    94 of the 1573 series have no named row at all. Rows that another row's Neo-Vision shift id points at are
    skipped too (`nv_shift_targets`): they are the Brave-Shift FORMS, which retail never puts in a roster --
    granting both sides is what showed `Blade Breaker Esther` twice (operator, 2026-10-10). What remains is
    the base side, whose tops measure as 7*/Lv120, NV/Lv130, 6*/Lv100, 5*, …, plus the 30 Lv1 fodder series
    (pots, gil snappers, gigantuars, trust moogles). Same-name groups still occur where two DIFFERENT
    generations of a character are each ownable in their own right (e.g. the story `Dark Fina` root vs the
    NV `100000337`); those are kept on purpose. Sorted by unit id, so the seed is deterministic.

    `rarity`, when given, restricts the pick to each family's row AT that rarity (`9fW0TePj`) instead of its
    top tier -- families without such a row are skipped. This is how a low-rarity roster is built: each
    rarity tier carries its OWN `F_LIMITBURST_MST` row, so 3* Exdeath's Limit Burst is Black Hole while the
    5*/6* tiers' is Grand Cross (measured 2026-10-10, operator's 3*-LB ask). Cached per rarity.
    """
    want = None if rarity in (None, "", 0, "0") else str(rarity)
    if want not in _UNIT_TOP_ROWS:
        names = named_unit_ids()
        if not names:
            # `named_unit_ids`' contract for an unreadable table is DO NOT FILTER -- but say so loudly:
            # this seed would then grant rows SW-78 proved can crash an award screen.
            print("    all-units: unit-name table unreadable -- the pool is NOT filtered to named units",
                  flush=True)
        targets = nv_shift_targets()
        fam: dict = {}
        for r in _load_ndjson("F_UNIT_MST.ndjson").get("__rows__", []):
            mid = str(r.get(UNIT_KEY))
            if names and mid not in names:
                continue
            if mid in targets:
                continue
            if want is not None and str(r.get("9fW0TePj")) != want:
                continue
            key = (int(r.get(GROWTH_MAXLV) or 0), int(r.get("9fW0TePj") or 0), int(mid or 0))
            root = str(r.get("woghJa61"))
            if root not in fam or key > fam[root][0]:
                fam[root] = (key, r)
        _UNIT_TOP_ROWS[want] = [r for _, r in sorted(fam.values(), key=lambda kv: kv[0][2])]
    return _UNIT_TOP_ROWS[want]


def ticket_group_items(group_id) -> list:
    """`(item_type, item_id, rarity)` for every shipped `F_TICKET_MST` row whose group (`zF6QIY2r`) matches.
    A ticket-only banner joins the shipped ticket table by `GachaDetailMst.ticketGroupId`
    (gacha_banner_types.md §6c)."""
    out = []
    for r in _load_ndjson("F_TICKET_MST.ndjson").get("__rows__", []):
        if str(r.get("zF6QIY2r")) == str(group_id):
            out.append((int(r.get("89EvGKHx", 23)), int(r.get("tL6G9egd", 0)), str(r.get("9fW0TePj"))))
    return out


def ticket_by_id(ticket_id) -> tuple | None:
    """`(item_type, item_id, rarity)` for the shipped `F_TICKET_MST` row whose **ticket id** (`zJ1A6HXm`)
    matches. The client sends this ticket id in `GachaExeRequest.zJ1A6HXm` (observed 12 = item 180, the
    5-star EX ticket, for the Golbez banner)."""
    for r in _load_ndjson("F_TICKET_MST.ndjson").get("__rows__", []):
        if str(r.get("zJ1A6HXm")) == str(ticket_id):
            return (int(r.get("89EvGKHx", 23)), int(r.get("tL6G9egd", 0)), str(r.get("9fW0TePj")))
    return None


def expand_pool(banner: dict) -> list:
    """A banner's pool as EXPLICIT `{unit_id, rarity, rate}` rows.

    `pool` is either already a list (returned as-is) or `{"tiers": [...]}` where each tier is
    `{rarity, rate, featured?: [ids], featured_share?: 0..1, units?: [ids]}`. A tier without `units`
    expands to every base-rarity root of that rarity; `featured` units take `featured_share` of the tier
    (split equally) and the non-featured take the remainder. This keeps `gacha_authored.json` small and the
    pool real -- the unit catalogue is shipped data."""
    pool = banner.get("pool")
    if isinstance(pool, list):
        return _only_named(pool)
    roots = unit_roots_by_rarity()
    out: list = []
    for tier in (pool or {}).get("tiers") or []:
        rate = max(0.0, float(tier.get("rate", 0)))
        if rate <= 0:
            continue
        units = [str(u) for u in (tier.get("units") or roots.get(str(tier.get("rarity")), []))]
        if not units:
            continue
        featured = [u for u in (str(x) for x in (tier.get("featured") or [])) if u in units]
        if featured:
            share = min(1.0, max(0.0, float(tier.get("featured_share", 0.0))))
            f_rate = rate * share / len(featured)
            others = [u for u in units if u not in featured]
            o_rate = rate * (1.0 - share) / len(others) if others else 0.0
            for u in units:
                out.append({"unit_id": u, "rarity": tier.get("rarity"),
                            "rate": f_rate if u in featured else o_rate})
        else:
            per = rate / len(units)
            for u in units:
                out.append({"unit_id": u, "rarity": tier.get("rarity"), "rate": per})
    return _only_named(out)


def team_level_mst(level) -> dict | None:
    return _load_ndjson("F_TEAM_LV_MST.ndjson", TEAM_LEVEL_KEY).get(str(level))


def nrg_cap(level) -> int:
    """The NRG cap for a rank: `F_TEAM_LV_MST`'s `B6kyCQ9M` column (`lv+40` <=100, then `140+(lv-100)//2`)."""
    row = team_level_mst(level)
    if row and row.get(TEAM_NRG_CAP):
        return int(row[TEAM_NRG_CAP])
    return 40 + int(level)


def need_exp(level) -> int:
    """`F_TEAM_LV_MST[level].B6H34Mea` -- the exp a player must ACCUMULATE WITHIN rank `level-1` to be
    promoted to `level` (row 1 = 0, a filler). The client is the authority for which row to read: the rank
    bar is `exp / getNeedUserExp(level+1)` (`UIHeader::setExpBarWidth` `0x209a904`, `add w0,w8,#1` at
    `0x209a920`), and `GameUtils::getRestUserExp` (`0x19e8de0`) returns `need_exp(level+1) - exp` as "exp
    remaining to rank up". So the threshold to advance FROM rank L is `need_exp(L+1)` (schema §2.4)."""
    row = team_level_mst(level)
    return int(row[TEAM_NEED_EXP]) if row and row.get(TEAM_NEED_EXP) else 0


# `F_GACHA_EFFECT_PATTERN_MST` is keyed `zoU5J3Kq` and carries a rarity column `9fW0TePj` and a block list
# `wpoLx6h8`. It is ONE-TO-MANY per rarity (16 rows at rarity 5), so the choice is AUTHORED: the
# lowest-numbered row of the granted rarity. (For rarity 5 that is id 9, the pattern the operator confirmed
# renders a rainbow -- schema §3.8.) The client does NOT validate the pattern against the unit.
def effect_pattern_for(rarity, gacha_id=None) -> str | None:
    """`zoU5J3Kq` for a draw. `F_GACHA_EFFECT_PATTERN_MST` is keyed by **(gachaId `X1IuZnj2`, rarity
    `9fW0TePj`)** -- not rarity alone -- so prefer a row for THIS gacha id and fall back to the lowest id of
    the rarity. The client does NOT validate the pattern (schema §3.8); an ABSENT one is the popup."""
    rows = _load_ndjson("F_GACHA_EFFECT_PATTERN_MST.ndjson").get("__rows__", [])

    def ids_for(pred) -> list:
        return sorted(int(r["zoU5J3Kq"]) for r in rows
                      if str(r.get("9fW0TePj")) == str(rarity)
                      and str(r.get("zoU5J3Kq", "")).isdigit() and pred(r))

    if gacha_id is not None:
        exact = ids_for(lambda r: str(r.get("X1IuZnj2")) == str(gacha_id))
        if exact:
            return str(exact[0])
    allids = ids_for(lambda r: True)
    return str(allids[0]) if allids else None


def growth_table() -> dict:
    """`{(maxLv, growthType, level): percent}` from `F_UNIT_GROW_MST` (cached)."""
    global _GROWTH_TABLE
    if _GROWTH_TABLE is None:
        _GROWTH_TABLE = {}
        for r in _load_ndjson("F_UNIT_GROW_MST.ndjson").get("__rows__", []):
            try:
                _GROWTH_TABLE[(str(r.get(GROWTH_MAXLV)), str(r.get(GROWTH_TYPE)),
                               str(r.get(GROWTH_LEVEL)))] = int(r.get(GROWTH_RATE, 0))
            except (TypeError, ValueError):
                continue
    return _GROWTH_TABLE


def growth_rate(max_lv, growth_type, level) -> int:
    """The growth percentage at `level` -- `UnitGrowMst::getGrowthRate(maxLv, growthType, level)`."""
    return growth_table().get((str(max_lv), str(growth_type), str(level)), 0)


def unit_stats(master_id, level) -> list:
    """The six stats at `level`, or zeros for an unknown id (never invent a unit).

    `stat(L) = min + (max - min) * growthPct(L) / 100`, read at `GameUtils::getUnitParamFromLv`
    `0x19f0518`-`0x19f0544` (`ldrh` the uint16 max, `sub` the accumulated min, `mul` the pct, signed /100,
    `add` the min back) with `growthPct = UnitGrowMst::getGrowthRate`. `F_UNIT_MST`'s stat token is the
    4-int tuple `min,max,?,?` (slot 1 = Lv-1, slot 2 = maxLv), which is why `pct(1)=0` yields `min` and
    `pct(maxLv)=100` yields `max`. Full evidence: `docs/notes/unit_stat_source.md`.
    """
    row = unit_mst(master_id) or {}
    max_lv = row.get(GROWTH_MAXLV, 0)
    growth_type = row.get(GROWTH_TYPE, 0)
    out = []
    for tok in STAT_TOKENS:
        vals = [int(x) for x in str(row.get(tok, "0")).split(",") if x.strip() not in ("",)]
        lo = vals[0] if vals else 0
        hi = vals[1] if len(vals) > 1 else lo
        pct = growth_rate(max_lv, growth_type, level)
        out.append(lo + (hi - lo) * pct // 100)
    return out


# ---------------------------------------------------------------------------------------------------
# The default (fresh-account) state. The starter roster and its epoch-ms instance ids are the recovered
# new-player shape (`true_new_player_state.md` §2-§3): Rain `100000102` + Lasswell `100000202`,
# consecutive epoch-ms ids, active deck 0, deck-0 order Lasswell first.
#
# STARTER_EQUIP is **AUTHORED, NOT recovered** (corrected 2026-09-23, SW-57). The item IDS are shipped
# `F_EQUIP_ITEM_MST` rows, but the SELECTION is copied from the **Story Digest** preset
# (`F_STATIC_UNIT_EQUIP_MST` rows 100001/100002, matched to `F_STATIC_UNIT_MST` ids `800000102`/`800000202`)
# -- a curated story-replay party, a DIFFERENT pair of units from our `100000102`/`100000202`. A genuine
# new account's kit is **server-authoritative and in NO shipped table** (verified 2026-09-23: no `F_*` table
# pairs 100000102/100000202 with gear; `F_FIXED_UNIT_MST` is the NPC fixed-party system; the real-session
# log `files/reports/20241026.log` carries no bodies -- `battle_authoring_data.md` §7.3). Kept because the
# operator observed the bare units dying in the tutorial; if a real capture ever shows the true kit,
# re-source it. Detail: `unit_party_payload_trees.md` §7b.
#
# SCOPE (2026-09-24): the kit belongs to the STARTER ROSTER ONLY, and `_unit` no longer hands it to every
# unit it creates. A unit obtained later (a gacha pull, a mail grant) starts BARE -- the retail behaviour,
# and what the operator expects -- while a starter unit gets it explicitly via `_starter_unit`. Giving it
# to every unit made a pulled unit claim the same four items as Rain/Lasswell, which the client's party
# duplicate check then stripped (EQUIP_REMOVE_CONFIRM); see `docs/notes/unit_equip_display.md` §3.
# ---------------------------------------------------------------------------------------------------
STARTER_EQUIP = {"1": "302000100", "2": "0", "3": "404000200", "4": "406000100",
                 "5": "409000100", "6": "0"}

#: The two units a fresh account starts with (`true_new_player_state.md` §2-§3). Only these carry
#: STARTER_EQUIP, and `_normalise` strips the kit from any OTHER row that still has it (the bug's fallout).
STARTER_ROSTER = ("100000102", "100000202")


MIX_RARITY_BONUS = {1: 100, 2: 200, 3: 500, 4: 1000, 5: 1500, 6: 3000, 7: 5000}


def unit_mix_exp(u: dict) -> int:
    """The EXP a material unit contributes to a fusion — `GameUtils::getMixExp` `0x19f1684`.

    The client's formula, read end to end (2026-09-24):
        (isExpUpUnit ? totalExp : totalExp / 2.5) + UnitMst[+0xdc] + rarityBonus[UnitMst[+0x84]]
    `UnitMst+0xdc` is `N85uJT1d` (`setAdjustMixExp`), `+0x84` is `9fW0TePj` (rarity, the jump-table
    index), and the bonus table is 7 float constants in .text (100/200/500/1000/1500/3000/5000). The
    term that scales with the material's own progress is `UnitInfoBase::getTotalExp` (vtable `+0x278`).

    `isExpUpUnit` (`0x1cf8848`) = `useType` (`2odVXyT8`, UnitMst+0x138) == 1 **and** the unit's
    subcategory list (UnitMst+0x1c0) contains 9. The F_UNIT_MST token that fills that list was NOT
    identified (the exp-up pots' rows carry no such CSV), so the divisor is applied to every unit whose
    `useType` is not 1. **That cannot bite our data**: the divisor only scales the material's OWN
    accumulated exp, and every material we grant is level 1 with `total_exp == 0`.
    """
    mst = unit_mst(u.get("master_id")) or {}
    total = float(u.get("total_exp", 0) or 0)
    exp_up = str(mst.get("2odVXyT8", "") or "") == "1"
    try:
        rarity = int(mst.get("9fW0TePj", 0) or 0)
    except (TypeError, ValueError):
        rarity = 0
    try:
        adjust = int(mst.get("N85uJT1d", 0) or 0)
    except (TypeError, ValueError):
        adjust = 0
    return int(total if exp_up else total / 2.5) + adjust + MIX_RARITY_BONUS.get(rarity, 0)


def unit_mix_lb_exp(u: dict) -> int:
    """The LIMIT-BURST exp a material contributes — `UnitStatusUtil::getMixLimitBurstExp` (`0x1abbb78`
    -> the array overload `0x1abbbac`).

    The loop adds, for every material whose `isLbUpUnit()` (`0x1cf8884` = `UnitMst+0xe0` > 0, i.e.
    `1Rf2VDoI` = adjustMixLbExp): `adjustMixLbExp + unit.getLimitBurstExp()`. When the material's LB
    level is >= 2 it ALSO consults `LimitBurstLvMstList` with `(UnitMst+0xd0 = PR2HIc71, lbLv)` pairs —
    that extra term is NOT implemented here (residual: SW-106). It is unreachable today because every
    unit we serve has `lb_level == 1`, which is exactly the branch the client skips.
    """
    mst = unit_mst(u.get("master_id")) or {}
    try:
        adjust = int(mst.get("1Rf2VDoI", 0) or 0)
    except (TypeError, ValueError):
        adjust = 0
    if adjust <= 0:
        return 0
    return adjust + int(u.get("lb_exp", 0) or 0)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _parse_utc(value) -> float | None:
    """`YYYY-MM-DDTHH:MM:SSZ` (what `_now_iso` writes) as a UTC epoch; None when absent/unparseable.

    `time.mktime(time.strptime(...))` -- the pairing `nrg_rest` uses -- reads the string in the HOST's
    timezone, so a file written under one TZ and served under another is off by the offset. `timegm`
    reads the `Z` as the UTC it claims to be. (NRG's countdown is not served yet, so its parser is left
    alone; the orbs are the first consumer that needs the value on a sub-hour timescale.)
    """
    if not value:
        return None
    try:
        return float(calendar.timegm(time.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError, OverflowError):
        return None


def _unit(master_id: str, instance_id) -> dict:
    """A roster row. A unit starts BARE: no equipment, no materia (retail -- and see the STARTER_EQUIP
    note above for why handing everyone the starter kit is a bug the client actively undoes)."""
    return {"instance_id": str(instance_id), "master_id": str(master_id), "level": 1, "exp": 0,
            "total_exp": 0, "lb_exp": 0, "nv_ex_level": 0, "guest": 0,
            "storage_state": 0, "favourite": 0, "granted_rarity": None, "first_get_unit_id": None,
            # lb_level must be >= 1 for any unit that HAS an LB: `LimitBurstLvMstList::getObject(lbId, 0)`
            # is null and `BattleCharacter::reserveLimitBurstAction` dereferences it UNGUARDED, so serving 0
            # crashes the client the moment the LB is activated (docs/notes/limit_burst_path.md).
            "lb_level": 1 if limit_burst(master_id) else 0,
            "equipment": {}, "materia": ["0", "0", "0", "0"], "vision_card": "",
            "affinity_points": ["", ""], "affinity_info": ["", ""]}


def _starter_unit(master_id: str, instance_id) -> dict:
    """A STARTER-ROSTER row: `_unit` plus the authored Story-Digest kit (`STARTER_EQUIP`)."""
    u = _unit(master_id, instance_id)
    u["equipment"] = dict(STARTER_EQUIP)
    return u


def _mail_time() -> str:
    """A mail timestamp in the format the client parses: `%Y-%m-%d %H:%M:%S` UTC, or `""`.
    `CommonUtils::convertToTime` `0x1e35058` is `strptime(s,"%Y-%m-%d %H:%M:%S") + timegm` (docs/notes/mailbox.md §1)."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _next_day(prev: str, today: str) -> bool:
    """True when `today` is the calendar day immediately after `prev` (both `YYYY-MM-DD`).

    Only the consecutive-day counter needs this, and it needs it to be right in BOTH directions: a same-day
    call never reaches here, a gap of one day continues the streak, and anything else (a multi-day gap, a
    backwards clock, a malformed date) restarts it at 1 rather than continuing a streak it cannot verify.
    """
    try:
        p = time.strptime(str(prev), "%Y-%m-%d")
        t = time.strptime(str(today), "%Y-%m-%d")
    except (ValueError, TypeError):
        return False
    return time.mktime(t) - time.mktime(p) == 86400


def _mail_item_string(items) -> str:
    """Serialize mailbox items to the mail wire form `type:id:count[,…]`
    (`MailSceneCommon::getItemParamFromTargetInfo` `0x2245e24`). Grammar per `docs/notes/mailbox.md` §2:
    split on `,` then `:`. Type 30/9990 (gil) takes the amount in the FIRST slot after the type
    (`30:<amount>`); every other known type takes `type:id:count`. An unrecognised type pushes a ZERO item
    client-side, so it is dropped here rather than sent.
    """
    known = (10, 15, 20, 21, 22, 23, 26, 27, 28, 50, 60, 9992)
    out = []
    for it in items:
        t = int(it.get("item_type", 0))
        if t in (30, 9990):
            out.append(f"{t}:{int(it.get('count', 0))}")
        elif t in known:
            out.append(f"{t}:{int(it.get('item_id', 0))}:{int(it.get('count', 0))}")
    return ",".join(out)


def default_state(user_id: str = "100000001", handle_name: str = "Rain", now_ms: int | None = None) -> dict:
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    rain = _starter_unit("100000102", now)
    lasswell = _starter_unit("100000202", now + 1)
    members = [{"member_type": 1, "disp_order": 0, "instance_id": lasswell["instance_id"]},
               {"member_type": 0, "disp_order": 1, "instance_id": rain["instance_id"]}]
    # Public party names are 1-based; the wire/internal deck ids remain 0..9 because the client
    # defines ten slots and the Units hub walks all ten without a null check.
    decks = [{"id": i, "name": f"Party {i + 1}", "members": [dict(m) for m in members]} for i in range(10)]
    return {
        "version": SCHEMA_VERSION,
        "meta": {"slot": "default", "created_utc": _now_iso(), "client_build": "ver.9.0.0",
                 "overlay": [], "note": ""},
        "identity": {"user_id": str(user_id), "handle_name": handle_name},
        "rank": {"level": 1, "exp": 0, "nrg": nrg_cap(1), "nrg_regen_anchor_utc": _now_iso(),
                 # `last_up` is the DAILY QUEST completion popup's only source of a before/after team level
                 # (`tzCNMJ87` -> `parseTeamLvInfo`). Written by `add_rank_exp` when a level is actually
                 # gained, read by `serve_daily_quest`, and absent until then -- which is the honest state,
                 # not a zero that would render as a fake "1 -> 1" level-up.
                 "last_up": None},
        "currencies": {"lapis_free": 0, "lapis_paid": 0, "gil": 0, "friend_point": 0},
        "profile": {"serial_login_count": 0, "player_emblem_id": 0, "message": "",
                    "decorate_home_vc_id": "", "gift_id": 0,
                    # OWNED emblems (`UserPlayerEmblemInfoList`), ids from the shipped
                    # `F_EMBLEM_ITEM_MST` (`0w6XzofV`). Empty = a fresh account, which is the truth:
                    # emblems come from trophies and the server owns that grant. `EMBLEM_SEED` grants.
                    # NOT the same field as `player_emblem_id` -- that is the one DISPLAYED next to the
                    # name (`UserTeamInfo` `n69quiCJ`), this is what the player has earned.
                    "owned_emblems": []},
        # THE EMBLEM EVENT (the crest campaign the status-HUD crest opens; docs/notes/emblem_event.md).
        # Server-authoritative, like the schedule itself: `milestones` is this account's progress through
        # the live campaign's weekly objectives (milestone id -> progress/cleared), `mailed` the crest
        # counts already paid out (idempotence that survives a retried mission end). The campaign id is
        # filled in lazily by the accessor so an authored state file needs no such key.
        "emblem_event": {"campaign": "", "milestones": {}, "mailed": []},
        "counts": {"unit": 2, "storage": 0, "friend": 0, "equip_item": 0, "item": 0,
                   "materia": 0, "material_item": 0},
        # The four ORB families (APP-34), FULL and anchorless: `orb_view` reads a missing anchor as
        # at-cap, so a fresh account starts full on all four and the Arena tile stops showing our old
        # 0/0. `point` is the settled value; `anchor_utc` is the receiveTime it was taken at.
        "orbs": {fam: {"point": spec["cap"], "anchor_utc": None}
                 for fam, spec in ORB_FAMILIES.items()},
        "roster": [rain, lasswell],
        # OWNED vision cards (`UserVisionCardInfoResponse`). Empty is the honest fresh-account value: a
        # starter account owns none, and the HOME ornament group is gated on this list (APP-15). Seeded by
        # `--grant-vc`, never hand-edited.
        "vision_cards": [],
        "party": {"active_deck": 0, "decks": decks},
        # NOTE: a fresh state carries 10 of the shipped 5-star ticket (F_TICKET_MST item type 23 / id 180,
        # group 10010) so the ticket-only banner (id 75) is pullable in the FOCUSED SET. This is TEST state,
        # not a recovered new-player inventory; the display reply (UserItemInfoResponse) is still SW-67.
        "inventory": {"items": [{"item_type": 23, "item_id": 180, "count": 10}],
                      "carry_items": [], "equipment": []},
        # The mailbox (retail delivers many rewards as mail). An entry is a `UserMailInfo`; `items` holds the
        # attached rewards as (item_type, item_id, count) rows. Pending protect/date fields stay "" until a
        # mail's own settlement semantics are pinned. See docs/notes/mailbox.md. Begin empty.
        "mailbox": [],
        # The daily-login calendar's own counter (SW-86). The client derives `activeDay` from the REPLY's
        # per-day `state` (`DailyLoginBonusInfo::onReadFinish`, 0x1df670c) and keeps no count of its own, so
        # "which day am I on" is ours. `day` = how many calendar days of the current month have been logged
        # (0 = none yet, so the FIRST login of a month is day 1); `last_advance` is the date (YYYY-MM-DD) the
        # counter last moved, so two logins in one day do not advance it twice. See
        # docs/notes/daily_login_bonus.md.
        "login_bonus": {"month": "", "day": 0, "last_advance": ""},
        # The CATCH-UP schedules (`docs/notes/catchup_bonus_family.md`). Three counters, all server-side,
        # because the client keeps no count of its own for any of them (the daily calendar's `state` is read
        # out of the reply, `daily_login_bonus.md` §7.3, and the cumulative ladder's `LoginDays` likewise).
        #   `total_login_days` -- cumulative calendar days logged in. This is the AXIS of the shipped
        #     `F_LOGIN_BONUS_TOTAL_REWARD_MST` ladder (tiers at 2, 3, 5, 7, 14, 30 ... 1150 days).
        #   `ladder_bit`      -- the highest tier's `QLfe23bu` bit already granted, so a tier can be paid
        #     exactly once even though several tiers can fall due on the same login.
        #   `campaign_id`/`campaign_day` -- which special-login campaign is being replayed and how many of
        #     its days have been paid. Re-selecting a campaign RESETS the day to 0 (a new calendar).
        #   `welcome_done`    -- the one-shot fresh-account grant (SW-99), fired on the first login.
        "catchup": {"total_login_days": 0, "last_advance": "", "serial_days": 0,
                    "ladder_bit": 0, "ladder_tier": 0,
                    "campaign_id": "", "campaign_day": 0, "welcome_done": False, "granted": []},
        # The 8th-anniversary ticket, issued BY MAIL once per calendar day. The marker lives HERE and not
        # in the mailbox ON PURPOSE: the inbox request is a carrier (so the mail appears when the player asks
        # for it) and the inbox is ALSO the delete path, so a marker that lived in the mail itself would be
        # erased by the player's own delete and the next inbox request would hand the ticket straight back --
        # measured on a run as a receive/delete/receive loop. A daily award is issued once, and a player who
        # deletes it does not get it again until tomorrow.
        "anniversary": {"last_issued": "", "issued_total": 0},
        "progress": {"switches": {}, "cleared_missions": [], "cleared_challenges": {}, "paid_challenges": {}},
        # The owned ESPERS (BEAST, 幻獣). `esper_id`/`rarity`/`level` key the `F_BEAST_STATUS_MST` stat row;
        # `board` is the `UserBeastPieceInfo` bitmask; `beast_decks` maps a party-deck id (0..9, the SAME
        # number as `UserPartyDeckInfoResponse.r21y0YzS`) to its 5 `slot -> esper_id` members. Served as the
        # `gP9TW2Bf` / `1S8P2u9f` / `49rQB3fP` groups (docs/notes/beast_esper_surface.md).
        "beasts": [], "beast_decks": {},
        "gacha": {"last_result": None, "last_pull": None, "user_banners": {},
                  "step": {}, "select": {}, "panel": {}, "box": {},
                  "guaranteed_slots": []},
        "records": {}, "notices": {},
        # The QUEST-progress store (2026-09-26) -- ONE table of "things the player did", keyed
        # `<family>/<schedule>/<quest>`, NOT a counter per feature: the same fact satisfies more than one
        # quest (SP-challenge 100101 "Equip a unit with a weapon!" and panel-quest 1000101 are the SAME
        # condition; "cleared Earth Shrine" is a fact the mission clear flag already knows).
        # `scopes` holds the reset discipline per schedule, because the shipped model has BOTH kinds and
        # the class says which: `sgPanelQuestScheduleMst::getIsPersistent()` (+0x40) is "whether the
        # schedule persists outside its period", and the six login-bonus asset families name the rest
        # (today/month, milestone/never, WLB/weekly, CLB/per-episode).
        # A NEW PERIOD IS A NEW KEY, never a wipe -- clearing would lose that the player did it at all.
        "quest_progress": {"counters": {}, "scopes": {}, "audit": []},
        # The DAILY QUEST's own block -- and it is small on purpose, because the PROGRESS lives in
        # `quest_progress` like every other quest (one table of "things the player did", so one summon
        # satisfies a daily quest AND a panel quest). What is here is only what cannot be derived:
        #   * `day`   -- the day the served set belongs to. A new day is a new KEY (`dailyquest/<day>/<id>`),
        #                 never a wipe, so the history of what the player did and when survives.
        #   * `set`   -- today's quest ids, FROZEN for the day. A server that re-rolled per request would
        #                 change the list under the player's finger; the roll happens once, here.
        #   * `resets`-- how many day-rolls this account has seen, so "the set changed" is auditable.
        # The selection itself (which pools, how many) is the SEASON's, not the state's -- `season.py`'s
        # `events.daily_quests`, defaulting to `ffbe_config.DAILY_QUEST_DEFAULT_*`.
        "daily_quest": {"day": "", "set": [], "pools": [], "resets": 0},
    }


# ---------------------------------------------------------------------------------------------------
# Loading / persistence
# ---------------------------------------------------------------------------------------------------
_SETS = (("progress", "switches"), ("progress", "cleared_challenges"), ("progress", "paid_challenges"))


def _refresh_derived_counts(data: dict) -> dict[str, int]:
    """Keep the stored unit/storage totals equal to the roster at every mutation boundary."""
    counts = data.setdefault("counts", {})
    derived = {
        "unit": len(data.get("roster") or []),
        "storage": sum(1 for u in data.get("roster") or []
                       if int(u.get("storage_state", 0) or 0) != 0),
    }
    counts.update(derived)
    return derived


def _normalise(data: dict) -> dict:
    """In-memory the three progress maps are dict[str, set]; on disk they are sorted lists.

    Also repairs a roster row's `lb_level` when it is 0 for a unit that owns a Limit Burst: level 0 is not
    a valid row (`F_LIMITBURST_LV_MST` starts at 1) and the activation path dereferences the null lookup
    unguarded, so a state written before 2026-09-22 would still crash the client.
    """
    # REPAIR (2026-09-26): a state written while the quest store shared the `progress` key has the QUEST
    # shape there and no switches, which crashes every reply on `data["progress"]["switches"]`. Move it to
    # `quest_progress` and restore the switches block. No such file was ever persisted (the bug lived in
    # `default_state` and the tests ran ephemeral), so this is belt-and-braces -- but the crash mode is a
    # hard failure on every request, so the guard earns its place.
    pr = data.get("progress")
    stripped: list = []
    if isinstance(pr, dict) and "switches" not in pr and ("counters" in pr or "scopes" in pr):
        data.setdefault("quest_progress", pr)
        data["progress"] = {"switches": {}, "cleared_missions": [],
                            "cleared_challenges": {}, "paid_challenges": {}}
        stripped.append("progress->quest_progress")
    elif isinstance(pr, dict) and "switches" not in pr:
        data["progress"] = {"switches": {}, "cleared_missions": [],
                            "cleared_challenges": {}, "paid_challenges": {}}
        stripped.append("progress:switches")

    for parent, key in _SETS:
        raw = (data.get(parent) or {}).get(key) or {}
        data[parent][key] = {str(k): set(v) for k, v in raw.items()}
    # The DAILY QUEST block (2026-09-26): a state written before it existed has none, and every read of it
    # must tolerate that rather than KeyError on a reply -- the same lesson as the `quest_progress` rename.
    dq = data.get("daily_quest")
    if not isinstance(dq, dict):
        dq = {"day": "", "set": [], "pools": [], "resets": 0}
        data["daily_quest"] = dq
    if not isinstance(dq.get("set"), list):
        dq["set"] = []
    if not isinstance(dq.get("pools"), list):
        dq["pools"] = []
    exp_ranged: list = []      # exp clamped into the client's residual range (see the roster loop)
    over_max: list = []
    renamed_parties: list = []
    for deck in (data.get("party") or {}).get("decks") or []:
        try:
            deck_id = int(deck.get("id"))
        except (TypeError, ValueError):
            continue
        # Migrate only the exact labels authored by the old default. A user-chosen party name is
        # authoritative and must not be rewritten.
        if deck.get("name") == f"Party {deck_id}":
            deck["name"] = f"Party {deck_id + 1}"
            renamed_parties.append(f"{deck_id}->{deck_id + 1}")
    for u in data.get("roster") or []:
        if int(u.get("lb_level") or 0) < 1 and limit_burst(u.get("master_id")):
            u["lb_level"] = 1
        # OVER-MAX LEVEL REPAIR (2026-09-24). Until this date nothing capped a level at the unit's
        # `F_UNIT_MST aU3o2D4t`, and the exp pattern is per-SERIES (80 rows for a 3★ unit capped at 40),
        # so a fusion credited the base straight past its cap -- the operator saw "42/40" live. Clamp the
        # level, pin the exp/total_exp at the max-level threshold, and say so (a silent clamp and a clamp
        # that never ran look identical).
        mx = max_level(u.get("master_id"))
        if mx > 0 and int(u.get("level") or 1) > mx:
            old = int(u.get("level") or 1)
            u["level"] = mx
            cap = max_level_exp(u.get("master_id"))
            if cap is not None:
                u["exp"] = min(int(u.get("exp", 0) or 0), int(cap))
                u["total_exp"] = min(int(u.get("total_exp", 0) or 0), int(cap))
            over_max.append(f"{u.get('master_id')}:{old}->{mx}")
        # LEVEL RE-DERIVATION (2026-09-26). `exp` is a RUNNING TOTAL and `level` is DERIVED from it
        # (`add_unit_exp` only ever sets the level by calling `level_for_exp`), so when the two disagree
        # the exp is authoritative and the level is a stale cache. This is the same class of repair as the
        # `counts` re-derivation below -- a derived value cached in the file -- and its absence had a
        # concrete cost: a checkpoint captured before the per-rank exp fix (2026-09-25) could never be
        # loaded again, because `state_coherence_issues` reported the disagreement and `load` REFUSES an
        # incoherent state. That silently killed every pre-fix rollback slot, and the refusal is correct
        # behaviour -- the gap was that nothing could repair the state first.
        # Placed AFTER the max-level clamp on purpose: the clamp pins `exp` at the cap, and re-deriving
        # from a pinned exp must agree with it, so the two repairs compose instead of fighting.
        # RESIDUAL-IN-RANGE REPAIR (2026-09-26, second pass). The first pass re-derived the LEVEL from a
        # cumulative exp, which was itself built on the false premise that `exp` is a running total -- so it
        # moved 80 -> 20 without reaching the truth. The client maintains a different invariant, and this is
        # it: `getRestUnitExp = needExp(level+1) - exp` must be a sane "exp to next level", i.e.
        # `0 <= exp < needExp(level+1)`. Every unit saved before this correction violated it (e.g.
        # needExp(21)=4415 against a stored exp of 35709 -> a rest-exp of -31294 in the client), so the
        # clamp is what actually repairs a slot. The LEVEL IS LEFT ALONE: `getLv()` is a bare field load,
        # so the level is the server's to state and there is nothing to re-derive it from.
        # TWO CASES, and they are different repairs, so they are tried in order.
        #
        # (a) LEGACY CUMULATIVE. A state written before 2026-09-26 stored `exp` as a running total -- that
        #     is why the campaign slot holds 35661, which is *literally* the 79->80 step of pattern 30. A
        #     level that disagrees with such a total can only be re-read through the cumulative mapping, so
        #     `level_for_exp` is used HERE -- and only here. It is NOT the client's rule (the client stores
        #     the level and reads exp forward); it is the migration tool for data written under the old
        #     assumption, which is a genuinely different job and worth saying so in the code.
        # (b) PLAIN OUT-OF-RANGE. Otherwise the residual simply is not a valid residual, so clamp it.
        # THE GATE (2026-10-10): a valid residual is not a legacy total, whatever `level_for_exp` says about
        # it. Without this an `exp == 0` row at any level is read as "cumulative 0" = level 1 and FLATTENED
        # -- which is what happened to every row the max-roster seed set (1461 rows, `exp` a valid residual
        # at the cap), and to any normally played row whose residual is 0. The legacy states this migration
        # exists for carry exp OUTSIDE the residual range (that is the whole 2026-09-26 finding), so the
        # range test separates them exactly.
        mid = u.get("master_id"); mid_exp = int(u.get("exp", 0) or 0)
        lv = int(u.get("level", 1))
        step = need_unit_exp(mid, lv + 1)
        residual_ok = step <= 0 or (0 <= mid_exp < step)
        legacy = level_for_exp(mid, mid_exp)
        if not residual_ok and legacy != lv and 0 <= mid_exp:
            u["level"] = legacy
            u["exp"] = 0                     # the total is consumed by the level; the residual starts fresh
            exp_ranged.append(f"{mid}: Lv{lv}->{legacy}, exp {mid_exp}->0 (legacy cumulative total)")
        elif step > 0 and not (0 <= mid_exp < step):
            was = mid_exp
            mid_exp = 0 if mid_exp < 0 else step - 1
            u["exp"] = mid_exp
            exp_ranged.append(f"{mid}@Lv{lv}: {was}->{mid_exp} (step {step})")
        if str(u.get("master_id")) not in STARTER_ROSTER and (u.get("equipment") or {}) == STARTER_EQUIP:
            u["equipment"] = {}
            stripped.append(str(u.get("master_id")))
        u.setdefault("equipment", {})
        u.setdefault("favourite", 0)
    # The unit/storage totals are DERIVED, but older v2 files still carry the original zero-valued
    # `counts` literals after grants/sells. `_team_row` already overrides them on the wire; repair the
    # canonical copy too, or an audit sees a state whose stored counts disagree with its own roster.
    counts = data.setdefault("counts", {})
    derived_counts = {
        "unit": len(data.get("roster") or []),
        "storage": sum(1 for u in data.get("roster") or [] if int(u.get("storage_state") or 0) != 0),
    }
    stale_counts = {k: (counts.get(k), v) for k, v in derived_counts.items()
                    if int(counts.get(k, 0) or 0) != v}
    if stale_counts:
        counts.update(derived_counts)
        print("state: repaired derived unit/storage counts: "
              + ", ".join(f"{k} {old}->{new}" for k, (old, new) in stale_counts.items()), flush=True)
    if exp_ranged:
        # Loud, like every other repair here: a silent repair and a repair that never ran look identical.
        print(f"state: clamped {len(exp_ranged)} unit exp(s) into the client's residual range: "
              f"{'; '.join(exp_ranged)}", flush=True)
    if over_max:
        print(f"state: clamped {len(over_max)} unit(s) above their max level: {','.join(over_max)}",
              flush=True)
    if stripped:
        # Loud, like every other state-level correction (`limit_burst_path.md`'s lb_level repair and
        # `save_state`'s writers): a silent repair and a repair that never ran look identical otherwise.
        print(f"state: stripped the starter equip kit from {len(stripped)} non-starter unit(s): "
              f"{','.join(stripped)}", flush=True)
    if renamed_parties:
        print(f"state: renamed generated party labels to 1-based: {', '.join(renamed_parties)}", flush=True)
    return data


def state_coherence_issues(data: dict) -> list[str]:
    """Return invariant violations in a raw v2 player state.

    This is deliberately a pure, non-repairing audit. `_normalise` repairs known migrations; this
    function answers the separate question "is this saved state internally coherent?" and is used by
    `harness/audit_player_state.py` plus checkpoint verification. The focus is the unit-adherence chain:
    every deck and beast reference must resolve to an owned object, every projection must preserve
    instance identity, and stored totals must agree with the rows they summarize.
    """
    issues: list[str] = []
    if not isinstance(data, dict):
        return ["state is not an object"]
    if int(data.get("version", 0) or 0) != SCHEMA_VERSION:
        issues.append(f"version is {data.get('version')!r}, expected {SCHEMA_VERSION}")
    roster = data.get("roster")
    if not isinstance(roster, list):
        return issues + ["roster is not a list"]
    by_iid: dict[str, dict] = {}
    for pos, unit in enumerate(roster):
        if not isinstance(unit, dict):
            issues.append(f"roster[{pos}] is not an object")
            continue
        iid = str(unit.get("instance_id") or "")
        mid = str(unit.get("master_id") or "")
        if not iid:
            issues.append(f"roster[{pos}] has no instance_id")
        elif not iid.isdigit():
            issues.append(f"roster[{pos}] has non-numeric instance_id {iid!r}")
        elif iid in by_iid:
            issues.append(f"duplicate roster instance_id {iid}")
        else:
            by_iid[iid] = unit
        if not mid or unit_mst(mid) is None:
            issues.append(f"roster instance {iid or pos} names unknown F_UNIT_MST id {mid!r}")
            continue
        try:
            level = int(unit.get("level", 0) or 0)
            exp = int(unit.get("exp", 0) or 0)
            total_exp = int(unit.get("total_exp", 0) or 0)
        except (TypeError, ValueError):
            issues.append(f"roster instance {iid} has non-integer level/exp")
            continue
        cap = max_level(mid)
        if level < 1 or (cap and level > cap):
            issues.append(f"roster instance {iid} level {level} outside 1..{cap or '?'}")
        if exp < 0 or total_exp < 0:
            issues.append(f"roster instance {iid} has negative exp/total_exp")
        if total_exp < exp:
            issues.append(f"roster instance {iid} total_exp {total_exp} is below current exp {exp}")
        # The client's invariant, not our old cumulative one: `exp` is a RESIDUAL WITHIN THE LEVEL, so the
        # test is the RANGE, not agreement with a derived level. Replaced 2026-09-26 -- the old test
        # (`level != level_for_exp(exp)`) encoded the false premise that a cumulative exp determines the
        # level, and reported every healthy row as incoherent while missing the real defect: an out-of-range
        # residual, which is what the client actually reads.
        step = need_unit_exp(mid, level + 1)
        if step > 0 and not (0 <= exp < step):
            issues.append(f"roster instance {iid} exp {exp} is outside the residual range "
                          f"0..{step - 1} for level {level} (needExp({level + 1})={step})")
        if level > (max_level(mid) or 0) > 0:
            issues.append(f"roster instance {iid} level {level} exceeds max {max_level(mid)}")
        if limit_burst(mid) is not None and int(unit.get("lb_level", 0) or 0) < 1:
            issues.append(f"roster instance {iid} owns a Limit Burst but has lb_level 0")
        first_get = str(unit.get("first_get_unit_id") or "")
        if first_get and unit_mst(first_get) is None:
            issues.append(f"roster instance {iid} names unknown first_get_unit_id {first_get}")
        equipment = unit.get("equipment") or {}
        if not isinstance(equipment, dict):
            issues.append(f"roster instance {iid} equipment is not an object")
        else:
            for slot, item_id in equipment.items():
                if str(slot) not in {"1", "2", "3", "4", "5", "6"}:
                    issues.append(f"roster instance {iid} has invalid equipment slot {slot!r}")
                if str(item_id) not in ("", "0") and _load_ndjson(
                        "F_EQUIP_ITEM_MST.ndjson", "J1YX9kmM").get(str(item_id)) is None:
                    issues.append(f"roster instance {iid} equips unknown item {item_id}")

    party = data.get("party") or {}
    decks = party.get("decks")
    if not isinstance(decks, list):
        issues.append("party.decks is not a list")
        decks = []
    deck_ids: set[str] = set()
    for pos, deck in enumerate(decks):
        if not isinstance(deck, dict):
            issues.append(f"party.decks[{pos}] is not an object")
            continue
        did = str(deck.get("id", ""))
        if not did or did in deck_ids:
            issues.append(f"duplicate/empty party deck id {did!r}")
        deck_ids.add(did)
        members = deck.get("members") or []
        if not isinstance(members, list):
            issues.append(f"party deck {did} members is not a list")
            continue
        if len(members) > 5:
            issues.append(f"party deck {did} has {len(members)} members (max 5)")
        orders: set[int] = set()
        seen: set[str] = set()
        for member in members:
            if not isinstance(member, dict):
                issues.append(f"party deck {did} has a non-object member")
                continue
            iid = str(member.get("instance_id") or "")
            try:
                order = int(member.get("disp_order", -1))
            except (TypeError, ValueError):
                order = -1
            if iid not in by_iid:
                issues.append(f"party deck {did} references non-roster instance {iid!r}")
            try:
                member_type = int(member.get("member_type", -1))
            except (TypeError, ValueError):
                member_type = -1
            if member_type not in (0, 1):
                issues.append(f"party deck {did} member {iid} has invalid member_type "
                              f"{member.get('member_type')!r}")
            if iid in seen:
                issues.append(f"party deck {did} repeats instance {iid}")
            seen.add(iid)
            if order < 0 or order >= 5 or order in orders:
                issues.append(f"party deck {did} has invalid/duplicate disp_order {order}")
            orders.add(order)
    active = str(party.get("active_deck", ""))
    if active not in deck_ids:
        issues.append(f"active deck {active!r} has no deck row")
    active_row = next((d for d in decks if isinstance(d, dict) and str(d.get("id")) == active), {})
    for member in active_row.get("members") or []:
        iid = str(member.get("instance_id") or "")
        if iid in by_iid and int(by_iid[iid].get("storage_state", 0) or 0) != 0:
            issues.append(f"active deck {active} contains stored unit {iid}")

    counts = data.get("counts") or {}
    expected_counts = {
        "unit": len(roster),
        "storage": sum(1 for u in roster if isinstance(u, dict)
                       and int(u.get("storage_state", 0) or 0) != 0),
    }
    for key, expected in expected_counts.items():
        if int(counts.get(key, 0) or 0) != expected:
            issues.append(f"counts.{key} is {counts.get(key)!r}, derived value is {expected}")

    beast_rows = data.get("beasts") or []
    beast_ids: set[str] = set()
    for pos, beast in enumerate(beast_rows):
        if not isinstance(beast, dict):
            issues.append(f"beasts[{pos}] is not an object")
            continue
        eid, rarity = str(beast.get("esper_id", "")), str(beast.get("rarity", ""))
        if eid in beast_ids:
            issues.append(f"duplicate beast/esper id {eid}")
        beast_ids.add(eid)
        if beast_status(eid, rarity) is None:
            issues.append(f"beast {eid!r} has unknown status/rarity {rarity!r}")
    for did, slots in (data.get("beast_decks") or {}).items():
        if str(did) not in deck_ids:
            issues.append(f"beast deck references missing party deck {did!r}")
        if not isinstance(slots, list) or len(slots) != BEAST_DECK_SLOTS:
            issues.append(f"beast deck {did} does not carry exactly {BEAST_DECK_SLOTS} slots")
            continue
        for slot, eid in enumerate(slots):
            if str(eid) not in ("", "0") and str(eid) not in beast_ids:
                issues.append(f"beast deck {did} slot {slot} references unowned esper {eid!r}")

    # The wire projection is the final adherence boundary. Build it from a throwaway frozen wrapper; any
    # KeyError/TypeError here is itself a coherence failure rather than a reason to hide the bad state.
    try:
        groups = PlayerState(copy.deepcopy(data), mode="frozen").groups()
        unit_ids = [str(r.get("og2GHy49")) for r in groups.get("B71MekS8", [])]
        if unit_ids != list(by_iid):
            issues.append("UserUnitInfo projection does not preserve roster instance order/identity")
        projected_decks = groups.get("5Eb0Rig6", [])
        if [str(r.get("r21y0YzS")) for r in projected_decks] != [str(d.get("id")) for d in decks]:
            issues.append("UserPartyDeckInfo projection does not preserve deck ids/order")
        for row in projected_decks:
            for triple in str(row.get("2A6fYiEC") or "").split(","):
                parts = triple.split(":")
                if len(parts) == 3 and parts[2] not in by_iid:
                    issues.append(f"projected deck {row.get('r21y0YzS')} references {parts[2]!r}")
    except Exception as exc:  # noqa: BLE001 - the audit reports malformed projections, never crashes
        issues.append(f"player reply projection failed: {exc!r}")
    return issues


def _deep_fill(base: dict, patch: dict) -> dict:
    """`patch` wins; anything it omits keeps the default (so a partial/older v2 file still loads)."""
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_fill(base[k], v)
        else:
            base[k] = v
    return base


def _upgrade(raw: dict) -> dict:
    """v1 `{diamonds, cleared, granted}` -> v2, and fill any missing v2 domain from the default."""
    if not isinstance(raw, dict):
        return default_state()
    base = default_state()
    if int(raw.get("version", 1)) >= SCHEMA_VERSION:
        _deep_fill(base, raw)
        return _normalise(base)
    dia = raw.get("diamonds") or {}
    base["currencies"]["lapis_free"] = int(dia.get("free", 0))
    base["currencies"]["lapis_paid"] = int(dia.get("paid", 0))
    granted = {str(k): set(v) for k, v in (raw.get("granted") or {}).items()}
    cleared = {str(k): set(v) for k, v in (raw.get("cleared") or {}).items()}
    base["progress"]["switches"] = granted
    base["progress"]["cleared_challenges"] = dict(cleared)
    base["progress"]["paid_challenges"] = {k: set(v) for k, v in cleared.items()}
    return _normalise(base)


def _serialise(data: dict) -> dict:
    """A JSON-safe copy: sets become sorted lists so the file is diffable (schema §2.5 spirit)."""
    out = copy.deepcopy(data)
    for parent, key in _SETS:
        out[parent][key] = {k: sorted(v) for k, v in data[parent][key].items()}
    return out


def _coerce(value):
    """`--set k=v` values arrive as strings; make the obvious ones numeric/bool, else keep the string."""
    if not isinstance(value, str):
        return value
    low = value.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none", ""):
        return None if low != "" else ""
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


#: sentinel for "this dotted path did not exist before the overlay created it".
_MISSING = object()


def _path_get(data, parts):
    cur = data
    for p in parts:
        if not isinstance(cur, dict) or p not in cur:
            return _MISSING
        cur = cur[p]
    return cur


def _path_set(data, parts, value) -> None:
    cur = data
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _path_delete(data, parts) -> None:
    cur = data
    for p in parts[:-1]:
        if not isinstance(cur, dict) or p not in cur:
            return
        cur = cur[p]
    if isinstance(cur, dict):
        cur.pop(parts[-1], None)


def _serve_unit_history() -> bool:
    """Whether a unit row carries `Z06cK4Qi` (`first_get_unit_id`) -- the AX unit-ability arms' lever.

    OFF by default, so the served row is byte-for-byte what it was. When ON the value comes from the STATE's own
    `first_get_unit_id` (what `grant_unit` records), never from a literal -- a token whose value we invent would
    measure our invention rather than the client.

    NOT `granted_rarity`: this row is the only place a first-get id belongs, and the unit's STARS come from
    `F_UNIT_MST.9fW0TePj` of the unit's own master id (field map: `UnitId` +0x4c), which is a shared table row.
    See `docs/notes/unit_skill_vector_capture.md` for why that asymmetry decides which arms are stageable.
    """
    try:
        import ffbe_config
        return bool(getattr(ffbe_config, "SERVE_UNIT_HISTORY", False))
    except Exception:  # noqa: BLE001 -- a missing config must not break the state module
        return False


def _serve_nv_ex() -> bool:
    """Whether a unit row carries `f8vk4JrD` (`nv_ex_level`, `UserUnitInfoResponse` token #21).

    OFF by default, so the served row is byte-for-byte what it was (the golden gate replays it). When ON the
    value comes from the STATE's own `nv_ex_level` (seeded by `--grant-unit all --nv-ex N`), never a literal.
    `readParam` maps the token through `atoi` to the unit's `setNvExLv(int)` at `+0x550`
    (`unit_party_payload_trees.md` §2 token 21) -- the field the client's EX/Brave-Shift paths read.
    """
    try:
        import ffbe_config
        return bool(getattr(ffbe_config, "SERVE_NV_EX", False))
    except Exception:  # noqa: BLE001 -- a missing config must not break the state module
        return False


class PlayerState:
    def __init__(self, data: dict, mode: str = "live"):
        if mode not in MODES:
            raise ValueError(f"unknown state mode {mode!r} (use one of {MODES})")
        self.data = data
        self.mode = mode
        self.dirty = False
        # An overlay is a TEST knob (schema §1.3): applied in memory, and the ordinary autosave must NOT
        # bake it into a slot. `_overlay_undo` remembers each touched path's pre-overlay value so `save()`
        # can write the canonical base back out; `force` (`--state-save`) is the one way to KEEP an
        # injection. `_overlay_labels` is this run's record, for the log and the served/saved label.
        self._overlay_undo: dict = {}
        # Switch bits granted by a SEASON: served to every reader, subtracted on every save (see
        # add_volatile_switches). Never serialised, even under force -- a checkpoint must read the same
        # whether the season is on or off.
        self._volatile_switches: set = set()
        self._overlay_labels: list = []
        # Labels already baked into the LOADED file (a slot captured with `--state-save`). Kept so a
        # captured injection stays labelled and is never mistaken for a clean base.
        self._base_overlay_labels: list = list(dict.fromkeys(
            (self.data.get("meta") or {}).get("overlay") or []))

    # --- lifecycle ---------------------------------------------------------------------------------
    @classmethod
    def load(cls, path: str, slot: str = "default", mode: str = "live",
             overlay: list | None = None, sets: list | None = None) -> "PlayerState":
        data = None
        if path and os.path.exists(path):
            raw = None
            try:
                with open(path, encoding="utf-8") as fh:
                    raw = json.load(fh)
            except OSError as exc:
                # A missing/locked file is external bad luck: a fresh account is a defensible run.
                print(f"state: {path} unreadable ({exc!r}) -- starting a FRESH account", flush=True)
            except json.JSONDecodeError as exc:
                print(f"state: {path} is not valid JSON ({exc!r}) -- starting a FRESH account", flush=True)
            if raw is not None:
                # NOT in the try above, deliberately: a crash in _upgrade is a bug or a shape we do not
                # understand, and falling back to a fresh account would silently discard the player's
                # save on the next autosave. Let it be loud.
                data = _upgrade(raw)
        if data is None:
            data = _normalise(default_state())
        data.setdefault("meta", {})["slot"] = slot
        ps = cls(data, mode=mode)
        if overlay:
            ps.apply_overlay(overlay)
        for path_, value in (sets or []):
            ps.apply_set(path_, value)
        issues = state_coherence_issues(ps.data)
        if issues:
            for issue in issues:
                print(f"state: INCOHERENT {path or '<fresh>'}: {issue}", file=sys.stderr, flush=True)
            raise ValueError(f"player state has {len(issues)} coherence issue(s); refusing to serve it")
        return ps

    def save(self, path: str, force: bool = False) -> bool:
        """Persist unless the policy forbids it. `force` is for an explicit `--state-save` (a snapshot of
        the loaded base is wanted even under `ephemeral`/`frozen`). Returns True if a write happened.

        An ordinary save (`force=False`) writes the CANONICAL base: any path touched by this run's overlay
        is reverted to its pre-overlay value, so a `--set` test can never leak into a slot (schema §1.3)."""
        if self.mode != "live" and not force:
            return False
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(_serialise(self._persist_view(force=force)), fh, indent=1)
            os.replace(tmp, path)
            self.dirty = False
            return True
        except Exception as exc:  # noqa: BLE001 - a write failure must not break the reply in flight
            print(f"state: could NOT write {path} ({exc!r})", flush=True)
            return False

    def _persist_view(self, force: bool = False) -> dict:
        """The data a save writes. Without `force`, every overlay-touched path is reverted to its
        pre-overlay value (and a path the overlay invented is removed); `meta.overlay` carries only the
        labels genuinely baked into the result -- the loaded base's, plus this run's only when `force`."""
        out = copy.deepcopy(self.data)
        if not force:
            for dotted, original in self._overlay_undo.items():
                parts = dotted.split(".")
                if original is _MISSING:
                    _path_delete(out, parts)
                else:
                    _path_set(out, parts, original)
        if self._volatile_switches:
            buckets = out.get("progress", {}).get("switches", {})
            for stype, index in self._volatile_switches:
                bucket = buckets.get(stype)
                if not bucket:
                    continue
                bucket.discard(index)
                if not bucket:
                    del buckets[stype]
        out.setdefault("meta", {})["overlay"] = (
            self._base_overlay_labels + (self._overlay_labels if force else []))
        return out

    # --- overlay / --set ---------------------------------------------------------------------------
    def apply_set(self, dotted: str, value) -> None:
        """Apply one dotted-path override IN MEMORY (schema §1.3). The pre-overlay value is remembered the
        first time the path is touched this run, so the ordinary autosave writes the base back; only an
        explicit `--state-save` keeps it. Also records the injection so the run is visibly labelled."""
        parts = dotted.split(".")
        if dotted not in self._overlay_undo:
            prev = _path_get(self.data, parts)
            self._overlay_undo[dotted] = _MISSING if prev is _MISSING else copy.deepcopy(prev)
        _path_set(self.data, parts, _coerce(value))
        self._overlay_labels.append(f"{dotted}={value}")

    def apply_overlay(self, items: list) -> None:
        """`items` may be `"path=value"` strings or `(path, value)` pairs; a dict is a flat map."""
        if isinstance(items, dict):
            items = list(items.items())
        for it in items:
            if isinstance(it, str):
                k, _, v = it.partition("=")
                self.apply_set(k, v)
            else:
                self.apply_set(it[0], it[1])

    # --- accessors used by the server --------------------------------------------------------------
    @property
    def switches(self) -> dict:
        return self.data["progress"]["switches"]

    @property
    def cleared_challenges(self) -> dict:
        return self.data["progress"]["cleared_challenges"]

    @property
    def paid_challenges(self) -> dict:
        return self.data["progress"]["paid_challenges"]

    @property
    def currencies(self) -> dict:
        return self.data["currencies"]

    def roster_row(self, instance_id) -> dict | None:
        for u in self.data["roster"]:
            if str(u["instance_id"]) == str(instance_id):
                return u
        return None

    # --- ops (schema §2.3) -------------------------------------------------------------------------
    def _mutate(self) -> bool:
        """A mutation is applied unless the policy is `frozen`. Marks dirty for the `live` writer."""
        if self.mode == "frozen":
            return False
        self.dirty = True
        return True

    def grant_unit(self, master_id: str, granted_rarity=None, first_get_unit_id=None,
                   instance_id=None) -> dict | None:
        if unit_mst(master_id) is None:
            raise ValueError(f"grant_unit: {master_id!r} is not an F_UNIT_MST id")
        if not self._mutate():
            return None
        existing = {int(u["instance_id"]) for u in self.data["roster"]
                    if str(u["instance_id"]).isdigit()}
        if instance_id is None:                       # epoch-ms, unique, consecutive (schema §3.3)
            instance_id = int(time.time() * 1000)
            while int(instance_id) in existing:
                instance_id += 1
        else:
            try:
                numeric_instance = int(instance_id)
            except (TypeError, ValueError):
                raise ValueError(f"grant_unit: instance_id must be numeric, got {instance_id!r}") from None
            if numeric_instance in existing:
                raise ValueError(f"grant_unit: instance_id {numeric_instance} already exists")
            instance_id = numeric_instance
        unit = _unit(master_id, instance_id)
        unit["granted_rarity"] = granted_rarity
        unit["first_get_unit_id"] = first_get_unit_id
        self.data["roster"].append(unit)
        _refresh_derived_counts(self.data)
        return unit

    def grant_beast(self, esper_id, rarity=1, level=1, board=None, link_deck=None, cp=None) -> dict | None:
        """Grant an owned ESPER (BEAST). Idempotent by EsperId; `link_deck` also puts it in slot 0 of that
        party deck's beast party. `rarity`/`level` must name a real `F_BEAST_STATUS_MST` row (no guessing).

        `board=None` means FRESH (`BEAST_BOARD_NONE`). The Story-Digest preset and the `BEAST_SEED` lever pass
        `BEAST_BOARD_ALL` explicitly, so the default is the safe one: an unexpected caller gets a fresh esper,
        not a fully trained one. `cp=None` means the CP EARNED by that level (`beast_cp`) — the progression
        stores CP explicitly from here on, because learning pieces SPENDS it (`UserBeastInfo+0xe0` is
        available CP, and `BeastUtil::learningPiece` decrements it). NOTE a re-grant of an already-owned
        esper rewrites `cp` the same way it rewrites rarity/level, so spent CP is reset: the story path
        guards with `owns_beast`, the `BEAST_SEED` lever deliberately does not."""
        eid = str(int(esper_id))
        status = beast_status(eid, rarity)
        if status is None:
            raise ValueError(f"grant_beast: esper {eid!r} rarity {rarity!r} is not an F_BEAST_STATUS_MST row")
        if not self._mutate():
            return None
        earned = beast_cp(status.get(BEAST_CP_KEY, ""), level)
        row = next((b for b in self.data.setdefault("beasts", []) if str(b.get("esper_id")) == eid), None)
        if row is None:
            row = {"esper_id": eid, "rarity": int(rarity), "level": int(level), "exp": 0,
                   "reset_count": 0, "board": str(board or BEAST_BOARD_NONE),
                   "cp": int(cp) if cp is not None else earned}
            self.data["beasts"].append(row)
        else:
            row.update(rarity=int(rarity), level=int(level),
                       cp=int(cp) if cp is not None else earned)
            if board is not None:
                row["board"] = str(board)
        if link_deck is not None:
            self.link_beast(link_deck, 0, eid)
        return row

    def link_beast(self, deck_id, slot, esper_id) -> None:
        """Set one slot of a party deck's beast party (`XZ4Kh7Ic` = `<slot>:<esperId>,…`, 0-based, 5 slots)."""
        if not self._mutate():
            return
        decks = self.data.setdefault("beast_decks", {})
        slots = list(decks.get(str(deck_id)) or [0] * BEAST_DECK_SLOTS)
        slots = [int(x or 0) for x in (slots + [0] * BEAST_DECK_SLOTS)[:BEAST_DECK_SLOTS]]
        slots[int(slot)] = int(esper_id or 0)
        decks[str(deck_id)] = slots

    # --- the BEAST progression: board pieces + the magicite mix (APP-26) ------------------>
    # Every RULE here was read in `libgame.so` and is the client's own, so the two models agree at the
    # next refresh: `BeastUtil::learningPiece` `0x1a56468` (learn), `BeastBoardScene::updateEvent`
    # `0x205be20` (unlock), `BeastUtil::giveBeastBait` `0x1a557cc` + `beastLvUp` `0x1a55b2c` (mix).
    def _beast_cp_available(self, entry: dict, cp_key) -> int:
        """`UserBeastInfo+0xe0` = CP AVAILABLE (earned minus spent on pieces). A row without the key falls
        back to the earned total (`beast_cp`) — that is the pre-APP-26 projection's value, so an old saved
        state serves the same number it did before; it is a migration, not a second rule."""
        if "cp" in entry:
            try:
                return int(entry.get("cp") or 0)
            except (TypeError, ValueError):
                return 0
        return beast_cp(cp_key, entry.get("level", 1))

    def beast_entry(self, esper_id) -> dict | None:
        """The state's row for an owned esper (`None` when the account does not own it)."""
        try:
            eid = str(int(esper_id))
        except (TypeError, ValueError):
            return None
        return next((b for b in self.data.get("beasts") or [] if str(b.get("esper_id")) == eid), None)

    def learn_beast_pieces(self, esper_id, piece_ids) -> tuple[list, list]:
        """Apply `BeastUtil::learningPiece` (`0x1a56468`) per piece id, in order: refuse when the piece's
        rarity is above the beast's, when the CP is below the cost, or when the bit is already set;
        otherwise `cp -= cost` and set the mask bit. Returns `(learned_ids, refusal_reasons)`.

        The client runs this at the TAP, so a piece list arriving here has already been paid for there;
        applying the same arithmetic is what keeps the two copies equal. Later refusals are normal in one
        case only: a re-sent list (a retry) whose bits are already set."""
        entry = self.beast_entry(esper_id)
        if entry is None or not self._mutate():
            return [], [f"esper {esper_id!r} is not owned"]
        status = beast_status(entry.get("esper_id"), entry.get("rarity")) or {}
        cp = self._beast_cp_available(entry, status.get(BEAST_CP_KEY, ""))
        board = str(entry.get("board") or BEAST_BOARD_NONE)
        learned: list = []
        refused: list = []
        for pid in piece_ids:
            row = board_piece_rows().get(str(pid))
            if row is None:
                refused.append(f"piece {pid}: no F_BEAST_BOARD_PIECE_MST row")
                continue
            try:
                bit = int(row.get(BEAST_PIECE_BIT))
                cost = int(row.get(BEAST_PIECE_CP_COST, 0) or 0)
                rare = int(row.get(BEAST_PIECE_RARE, 0) or 0)
            except (TypeError, ValueError):
                refused.append(f"piece {pid}: unusable row")
                continue
            if rare > int(entry.get("rarity", 0) or 0):
                refused.append(f"piece {pid}: requires rarity {rare}, beast is {entry.get('rarity')}")
                continue
            if board_bit(board, bit):
                refused.append(f"piece {pid}: bit {bit} already set")
                continue
            if cp < cost:
                refused.append(f"piece {pid}: costs {cost} CP, {cp} available")
                continue
            cp -= cost
            board = board_set_bit(board, bit)
            learned.append(str(pid))
        if learned:
            entry["cp"] = cp
            entry["board"] = board
            self.dirty = True
        return learned, refused

    def unlock_beast_pieces(self, esper_id, piece_ids) -> tuple[list, list]:
        """Apply the lock-piece open: spend `unlockCost` of `BEAST_UNLOCK_ITEM_ID` (kind 20) per piece and
        set its bit. Returns `(opened_ids, refusal_reasons)`. NO CP is charged — this path does not call
        `BeastUtil::learningPiece`; the client pays with the item (`BeastBoardScene::updateEvent`
        `0x205be20`) and only then plays the anime whose end reports the piece here."""
        entry = self.beast_entry(esper_id)
        if entry is None or not self._mutate():
            return [], [f"esper {esper_id!r} is not owned"]
        board = str(entry.get("board") or BEAST_BOARD_NONE)
        opened: list = []
        refused: list = []
        for pid in piece_ids:
            row = board_piece_rows().get(str(pid))
            if row is None:
                refused.append(f"piece {pid}: no F_BEAST_BOARD_PIECE_MST row")
                continue
            try:
                bit = int(row.get(BEAST_PIECE_BIT))
                count = int(row.get(BEAST_PIECE_UNLOCK_COUNT, 0) or 0)
            except (TypeError, ValueError):
                refused.append(f"piece {pid}: unusable row")
                continue
            if board_bit(board, bit):
                refused.append(f"piece {pid}: bit {bit} already set")
                continue
            if not self.spend_item(BAG_ITEM_TYPE, BEAST_UNLOCK_ITEM_ID, count):
                # The client's confirm window checks the count first (`GameUtils::getItemNum` `0x2058f40`), so
                # a request should not arrive without the item -- but `GameUtils::subItemNum` (`0x19e74c0`) is
                # itself a silent no-op when the bag lacks it (its kind switch just returns) and
                # `updateEvent` never tests a result, so IF one arrives the client unlocked anyway. Refusing
                # here would leave OUR board closed while the client's is open -- the silent-revert failure --
                # so we agree and SAY the cost went uncovered (name what was actually credited).
                refused.append(f"piece {pid}: opened WITHOUT charging {count} of item "
                               f"{BEAST_UNLOCK_ITEM_ID} (not held)")
            board = board_set_bit(board, bit)
            opened.append(str(pid))
        if opened:
            entry["board"] = board
            self.dirty = True
        return opened, refused

    def mix_beast(self, esper_id, entries) -> dict:
        """Feed magicite to an esper: `BeastUtil::giveBeastBait` (`0x1a557cc`) then the `beastLvUp` loop
        (`0x1a55b2c`), per item IN THE ORDER GIVEN, because the level-up runs between the client's taps.

        Per item (`ext` = `F_ITEM_EXT_BEAST_MST`; a miss grants 0/0):
            rate    = `beast_item_rate(ext.Mnm4UZT9, esper)`        (100 when absent)
            exp    += `beast_gain(ext.B6H34Mea, rate) * count`      ONLY while level < maxLv
            cp     += `beast_gain(ext.0A1BkNWb, rate) * count`
            then, while `exp >= need(level+1)`: `exp -= need`, `level++`, exp := 0 at maxLv,
            `cp += getAddCp(level)`.
        The magicite is consumed (kind 20). Returns a summary for the caller's one log line.
        """
        out = {"exp": 0, "cp": 0, "levels": 0, "level": 1, "consumed": 0, "refused": []}
        entry = self.beast_entry(esper_id)
        if entry is None or not self._mutate():
            out["refused"].append(f"esper {esper_id!r} is not owned")
            return out
        status = beast_status(entry.get("esper_id"), entry.get("rarity")) or {}
        pattern = status.get(BEAST_EXP_PATTERN, "")
        cp_key = status.get(BEAST_CP_KEY, "")
        try:
            max_lv = int(status.get(BEAST_MAXLV, 1) or 1)
        except (TypeError, ValueError):
            max_lv = 1
        level = int(entry.get("level", 1) or 1)
        exp = int(entry.get("exp", 0) or 0)
        cp = self._beast_cp_available(entry, cp_key)
        expected = str(entry.get("esper_id"))
        for item_id, count in entries:
            count = int(count)
            if count <= 0:
                continue
            ext = item_ext_beast(item_id)
            if ext:
                rate = beast_item_rate(ext.get(BEAST_ITEM_RATE), expected)
                gain_exp = beast_gain(ext.get(BEAST_ITEM_EXP, 0) or 0, rate)
                gain_cp = beast_gain(ext.get(BEAST_ITEM_CP, 0) or 0, rate)
            else:
                gain_exp = gain_cp = 0
            if not self.spend_item(BAG_ITEM_TYPE, item_id, count):
                out["refused"].append(f"item {item_id} x{count} (not held)")
                continue
            if level < max_lv:
                exp += gain_exp * count
                out["exp"] += gain_exp * count
            cp += gain_cp * count
            out["cp"] += gain_cp * count
            out["consumed"] += count
            while level < max_lv:
                need = beast_exp_need(pattern, level + 1)
                if exp < need:
                    break
                exp -= need
                level += 1
                out["levels"] += 1
                if level >= max_lv:
                    exp = 0
                cp += beast_cp_add(cp_key, level)
        entry["level"], entry["exp"], entry["cp"] = level, exp, cp
        out["level"] = level
        self.dirty = True
        return out

    # --- unit management: favourite / storage / sell (SW-106) ---------------------------------------
    # Wire facts (read in the binary 2026-09-24, one home: docs/notes/unit_management.md):
    #   favourite  UnitFavoriteRequest tBDi10Ay, group R8U3uD1i, 9Eb7m1NR=set ids / mGuM1Z9e=clear ids;
    #              the REPLY is the only applier (GameResponseParser::parseTag 0x190f154 -> applyFavorite),
    #              so the reply must echo the request's lists.
    #   storage    UnitStorageUpdateRequest 5ik69cuw, group JMxPV87y, km9gKXJ5=int state (1 = INTO
    #              warehouse -- the client then unequips the unit itself, WareHouseUtils
    #              0x1ad46f4), ng9xPNt8=ids; reply is readParam{return 1}.
    #   sell       UnitSellRequest 9itzg1jc, group Ray54PNQ, FBjo93gK=client-computed total gil,
    #              S5Ib7En1=ids; the client removes units and shows the gil itself (execSellUnit
    #              0x26d64b4 -> GameUtils::removeUserUnit), the reply (UnitSellResponse) reads nothing.
    def set_favourites(self, set_ids, clear_ids) -> tuple[list, list]:
        """Apply a favourite delta to the roster rows; returns (applied_set, applied_clear), roster-known ids only.
        The flag lives on the row (`favourite`), not a separate list, so it round-trips through save/load."""
        if not self._mutate():
            return [], []
        roster = {str(u["instance_id"]): u for u in self.data["roster"]}
        on = [str(i) for i in set_ids if str(i) in roster]
        off = [str(i) for i in clear_ids if str(i) in roster]
        for i in on:
            roster[i]["favourite"] = 1
        for i in off:
            roster[i]["favourite"] = 0
        return on, off

    def favourite_ids(self) -> list:
        """The roster's favourited instance ids, in roster order (the push group's CSV)."""
        return [str(u["instance_id"]) for u in self.data["roster"] if u.get("favourite")]

    def set_storage(self, ids, state) -> list:
        """Set `storage_state` on the named roster rows. `state` is the client's own int
        (UnitStorageUpdateRequest ctor arg, sent as km9gKXJ5): 1 = into the warehouse. Returns the ids applied."""
        if not self._mutate():
            return []
        roster = {str(u["instance_id"]): u for u in self.data["roster"]}
        moved = [str(i) for i in ids if str(i) in roster]
        for i in moved:
            roster[i]["storage_state"] = int(state)
        if moved:
            _refresh_derived_counts(self.data)
        return moved

    def sell_units(self, ids, gil) -> tuple[list, list]:
        """Remove the named roster rows and credit the sell gil. Returns (sold_ids, unknown_ids).

        The client removes the units and shows the gil ITSELF before the reply arrives (execSellUnit is
        optimistic), so the server's job is to keep STATE consistent with what the client already did:
        drop the rows, prune any deck membership (defensive -- the sell UI does not offer deck members,
        but a stale deck member that names a sold id would field a unit that does not exist), and credit
        the gil the CLIENT reported (its own number wins -- the mission_end_echo principle).
        Deck pruning is mirrored client-side? NO -- the client's decks are not touched by removeUserUnit;
        but a sold unit cannot be in a deck (the sell list excludes active party members), so this is a
        guard, not a behaviour."""
        if not self._mutate():
            return [], []
        wanted = {str(i) for i in ids}
        known = {str(u["instance_id"]) for u in self.data["roster"]}
        sold = sorted(i for i in wanted if i in known)
        unknown = sorted(wanted - known)
        if sold:
            self.drop_units(sold)
            self.currencies["gil"] = int(self.currencies.get("gil", 0)) + int(gil)
        return sold, unknown

    def mix_units(self, base_id, material_ids) -> dict | None:
        """Fuse `material_ids` into `base_id` (SW-106 item 5). Returns the result dict, or None.

        The materials are CONSUMED (removed from the roster) and the base gains the summed exp
        (`unit_mix_exp`) and LB exp (`unit_mix_lb_exp`). Returns the before/after level+exp so the caller
        can serve the `Qic38SuC` row the level-up display reads. Unknown or repeated material ids are
        dropped, and the base can never be its own material.
        """
        base = self.roster_row(base_id)
        if base is None or not self._mutate():
            return None
        seen, materials = set(), []
        for iid in material_ids:
            iid = str(iid)
            if iid == str(base_id) or iid in seen:
                continue
            u = self.roster_row(iid)
            if u is None:
                continue
            seen.add(iid)
            materials.append(u)
        if not materials:
            return None
        gain = sum(unit_mix_exp(u) for u in materials)
        lb_gain = sum(unit_mix_lb_exp(u) for u in materials)
        before_level, before_exp = int(base.get("level", 1)), int(base.get("exp", 0))
        removed = self.drop_units([u["instance_id"] for u in materials])
        self.add_unit_exp(base["instance_id"], gain)
        if lb_gain:
            base["lb_exp"] = int(base.get("lb_exp", 0) or 0) + lb_gain
        self.dirty = True
        return {"base_id": str(base["instance_id"]),
                "materials": [str(u["instance_id"]) for u in materials],
                "removed": removed, "gain": gain, "lb_gain": lb_gain,
                "before": (before_level, before_exp),
                "after": (int(base.get("level", 1)), int(base.get("exp", 0)))}

    def drop_units(self, ids) -> list:
        """Remove roster rows by instance id and prune any deck membership that named them.

        Shared by `sell_units` and `mix_units`. The deck prune is DEFENSIVE, not a behaviour: the client
        refuses to sell (`GameUtils::canSellUnit` checks `UserPartyDeckList::isJoinedAnyPartyType`) or to
        use a party member as material, so a stale deck member naming a consumed id should never occur —
        but one would field a unit that no longer exists.
        """
        wanted = {str(i) for i in ids}
        known = {str(u["instance_id"]) for u in self.data["roster"]}
        removed = sorted(i for i in wanted if i in known)
        if not removed:
            return []
        self.data["roster"] = [u for u in self.data["roster"]
                               if str(u["instance_id"]) not in wanted]
        for deck in (self.data.get("party") or {}).get("decks") or []:
            members = deck.get("members") or []
            kept = [m for m in members if str(m.get("instance_id")) not in wanted]
            if len(kept) != len(members):
                deck["members"] = kept
        _refresh_derived_counts(self.data)
        self.dirty = True
        return removed

    def add_rank_exp(self, amount: int) -> dict:
        if not self._mutate():
            return self.data["rank"]
        rank = self.data["rank"]
        rank["exp"] += int(amount)
        # `exp` is the progress WITHIN the current rank, NOT a cumulative total: the client draws the rank
        # bar as `exp / getNeedUserExp(level+1)` (`UIHeader::setExpBarWidth` `0x209a904`) and computes "exp
        # remaining" as `need_exp(level+1) - exp` (`GameUtils::getRestUserExp` `0x19e8de0`). The threshold
        # to advance from rank L is therefore the table row `level+1` (0/20/40/70/110/160/220...), and a
        # promotion SUBTRACTS it, leaving the overshoot as the new rank's progress. Operator-confirmed on
        # screen 2026-09-23 (the counter legitimately restarts after a rank-up). Bounded by the table: once
        # row `level+1` is absent `need_exp` is 0 and the loop stops.
        before, gained = rank["level"], []
        before_exp, before_nrg = int(rank["exp"]), int(rank["nrg"])
        while need_exp(rank["level"] + 1) > 0 and rank["exp"] >= need_exp(rank["level"] + 1):
            rank["exp"] -= need_exp(rank["level"] + 1)
            rank["level"] += 1
            gained.append(rank["level"])
        # A rank-up OVERLOADS energy. It is NOT a refill to the cap: the full allotment is ADDED to what you
        # already hold, so the bar goes over maximum. The client's Rank Up help screen says "your energy will
        # be fully restored" on page 1 and explains the overspill on page 2 -- and page 2 is a SHIPPED IMAGE:
        # `files/en/sd/tuto/Ver92_tuto.cpk` -> `tuto3000_2.png` (extracted at
        # `cpk_extract/en/tuto/Ver92_tuto/tuto3000_2.png`) says it outright, "Lapis increases energy by its
        # maximum ... If you still have energy left before recovering, your total energy will temporarily
        # exceed your max energy". The operator's worked example
        # (2026-09-22) is 45 energy at max 50 -> **96/51** after the rank-up, i.e. 45 + the NEW cap (51), not
        # 45 + 50 and not a clamp back to 51. Adding the cap of each rank gained reproduces that exactly for
        # a one-level jump and generalises to a multi-level one. `B6kyCQ9M` is served as the CURRENT energy
        # and `m0bD2zwU` as the cap, so 96/51 is literally what the client renders, and nothing clamps it
        # (`setActionPointMax` is a bare store; over-cap simply stops regen, since `updateActionTime` only
        # generates while actionPoint < actionPointMax).
        for lv in gained:
            rank["nrg"] += nrg_cap(lv)
        if gained:
            # RECORD the rank-up, because the DAILY QUEST's completion popup is a TEAM-LEVEL-UP popup and it
            # reads the before/after through `tzCNMJ87` -> `DailyQuestInfo::parseTeamLvInfo` (`0x1d977ec`,
            # fully decoded: 3 comma fields of 3 colon ints, before-group then current-group). Those six
            # numbers exist only at the moment of the rank-up -- afterwards the "before" is unrecoverable --
            # which is the same shape as the claim/progress/clear split. One small record, overwritten by the
            # next rank-up, read only by the popup projection.
            rank["last_up"] = {"before_level": int(before), "before_exp": before_exp,
                               "before_nrg": before_nrg, "to_level": int(rank["level"]),
                               "to_exp": int(rank["exp"]), "to_nrg": int(rank["nrg"]),
                               "at": _mail_time()}
        return rank

    def add_currency(self, kind: str, amount: int) -> int:
        if not self._mutate():
            return self.currencies.get(kind, 0)
        self.currencies[kind] = self.currencies.get(kind, 0) + int(amount)
        return self.currencies[kind]

    def spend_currency(self, kind: str, amount: int) -> bool:
        """Deduct `amount` of `kind` iff the balance covers it; returns whether it was spent. A gacha cost
        must not drive the balance negative, and `frozen` refuses like every other op (schema §1.2)."""
        amount = int(amount)
        if amount <= 0:
            return True
        if self.mode == "frozen":
            return False
        if self.currencies.get(kind, 0) < amount:
            return False
        self.currencies[kind] = self.currencies[kind] - amount
        self.dirty = True
        return True

    def spend_lapis(self, amount: int) -> bool:
        """Spend `amount` lapis, the FREE pool first and then the PAID one; False if the TOTAL is short.

        Both pools are lapis as the client counts them: `GameUtils::getItemNum(50)` is `UserDiamondInfo`'s
        total (free + paid) -- the enum table in `docs/notes/currency_model.md` §2 -- and the top bar shows
        the two pools but spends the sum. `spend_currency` stays as it is, because an authored gacha cost
        kind names one pool (`COST_KINDS["lapis"]` -> `lapis_free`); this is the op for the shop's useDia,
        which is a price in lapis, not a pool. Same refusal rules: negative balance impossible, `frozen`
        refuses.
        """
        amount = int(amount)
        if amount <= 0:
            return True
        if self.mode == "frozen":
            return False
        free = int(self.currencies.get("lapis_free", 0) or 0)
        paid = int(self.currencies.get("lapis_paid", 0) or 0)
        if free + paid < amount:
            return False
        take_free = min(free, amount)
        self.currencies["lapis_free"] = free - take_free
        self.currencies["lapis_paid"] = paid - (amount - take_free)
        self.dirty = True
        return True

    def _items(self) -> list:
        return self.data.setdefault("inventory", {}).setdefault("items", [])

    def item_count(self, item_type, item_id) -> int:
        for it in self._items():
            if str(it.get("item_type")) == str(item_type) and str(it.get("item_id")) == str(item_id):
                return int(it.get("count", 0))
        return 0

    def grant_item(self, item_type, item_id, count: int) -> int:
        if not self._mutate():
            return self.item_count(item_type, item_id)
        for it in self._items():
            if str(it.get("item_type")) == str(item_type) and str(it.get("item_id")) == str(item_id):
                it["count"] = int(it.get("count", 0)) + int(count)
                return it["count"]
        self._items().append({"item_type": int(item_type), "item_id": int(item_id), "count": int(count)})
        return int(count)

    def spend_item(self, item_type, item_id, count: int) -> bool:
        """Deduct `count` of `(item_type, item_id)` iff held; the ticket banner's cost. Same refusal rules
        as `spend_currency` (no negative balance, `frozen` refuses)."""
        if int(count) <= 0:
            return True
        if self.mode == "frozen":
            return False
        for it in self._items():
            if str(it.get("item_type")) == str(item_type) and str(it.get("item_id")) == str(item_id):
                if int(it.get("count", 0)) < int(count):
                    return False
                it["count"] = int(it["count"]) - int(count)
                self.dirty = True
                return True
        return False

    def important_item_rows(self) -> list:
        """`UserImportantItemInfoResponse` (`Md0N5abE`): the player's IMPORTANT items (type 23). Summon
        tickets live here (`F_TICKET_MST.89EvGKHx = 23`, ids keyed in `F_IMPORTANT_ITEM_MST` by the same
        `2dfixko3`). `readParam` `0x1920070` reads exactly two tokens (`2dfixko3` = itemId, `Qy5EvcK1` =
        count) and `onReadStarted` `0x192004c` clears the list once per reply, so this is the FULL set.
        This is what `GachaDetailMst::getHaveTickets` sums for the held-ticket count (SW-67)."""
        return [{"2dfixko3": str(it.get("item_id")), "Qy5EvcK1": str(it.get("count", 0))}
                for it in self._items() if str(it.get("item_type")) == "23"]

    def item_rows(self) -> list:
        """`UserItemInfoResponse` (`0dNXQa3R`, mode 0 = clear-then-add): the regular-item BAG.

        The reply carries only `(itemId, itemNum)` because `UserItemInfo::getItemType()` is the compile-time
        constant 20 (`0x1d8cae8` = `mov w0,#0x14; ret`), so ONE reply IS the type-20 bag; type 23 (important
        items / tickets) is a different class and a different singleton (`important_item_rows` -> `Md0N5abE`).
        Token map + list semantics: `docs/notes/item_state_tokens.md` §1 (`tL6G9egd` = itemId,
        `Qy5EvcK1` = itemNum). Zero-count rows are omitted: mode 1 reads `0` as a delete and a zero row draws
        nothing, and `spend_item` can leave a zero row behind.
        """
        return [{"tL6G9egd": str(it.get("item_id")), "Qy5EvcK1": str(it.get("count", 0))}
                for it in self._items()
                if str(it.get("item_type")) == str(BAG_ITEM_TYPE) and int(it.get("count", 0)) > 0]

    def carry_item_rows(self) -> list:
        """`UserCarryItemInfoResponse` (`nE7pV2wr`): the player's ITEM SET — the battle carry list.

        ONE row, whose `jsvoa0I2` is the class's own CSV: `dispOrder:itemId:itemNum` per element, elements
        joined with `,` — exactly what `UserCarryItemInfoList::toJsonGroup` serialises and what
        `ItemCarryEditRequest::createBody` sends (`docs/notes/item_state_tokens.md` §2).

        **Complete or not at all.** `onReadStarted` CLEARS `UserCarryItemInfoList` and the parse rebuilds it
        element by element, silently skipping an element that is not exactly three `:`-separated ints — so a
        partial echo deletes the rest (audited 2026-10-07). An empty set is therefore the empty string, the
        same shape the client itself sends when nothing is carried.
        """
        rows = self._carry_items()
        csv = ",".join(f"{int(r['disp_order'])}:{int(r['item_id'])}:{int(r['count'])}"
                       for r in sorted(rows, key=lambda r: int(r["disp_order"])))
        return [{"jsvoa0I2": csv}]

    def _carry_items(self) -> list:
        return self.data.setdefault("inventory", {}).setdefault("carry_items", [])

    def set_carry_items(self, entries) -> list:
        """Replace the item set with `entries` and return the stored rows.

        The CLIENT is the authority for its own slots — it serialises the list it displays — so this records
        what it sent rather than inventing a policy. Rows that do not carry three ints are dropped (the same
        guard the client's parser applies). Coherence checks (an id absent from the shipped `F_ITEM_MST`, a
        count the bag cannot cover) are logged by the caller, not enforced here: refusing would only make the
        screen disagree with the state, which is the failure mode this whole path exists to avoid.
        """
        if not self._mutate():
            return self._carry_items()
        clean = []
        for e in entries or []:
            try:
                clean.append({"disp_order": int(e["disp_order"]), "item_id": int(e["item_id"]),
                              "count": int(e["count"])})
            except (KeyError, TypeError, ValueError):
                continue
        self._carry_items()[:] = clean
        return clean

    def equip_item_rows(self) -> list:
        """`UserEquipItemInfoResponse` (`MBPCGT84`, mode 0 = clear-then-add): the OWNED equipment.

        WHY THIS IS REQUIRED FOR THE EQUIP VIEW TO RENDER: `UnitDetailScene::setEquipItemList` takes the
        slot **ids** from `UserUnitEquipInfoList` (`UnitEquipResponse`, `TJ9eL80N`) but each slot's item
        **object** (name/icon/stats) from the OWNED `UserEquipItemInfoList` via
        `UserEquipItemInfoListBase::getItemInfo(id)`, which returns null for an id that is not owned -- and a
        null object draws a blank frame (`docs/notes/unit_equip_display.md`). So an equipped id that we do
        not also own is INVISIBLE. Coherence rule: everything the roster equips is owned.

        THE COUNT MUST COVER THE WEARERS (corrected 2026-09-24). The client re-derives this per edit:
        `UnitEquipUtil::checkEquipCountAddUnitForParty` (`0x1ab4268`) computes, for each equipment id,
        `owned - equippedUnits - partyMerged`, and when that is NEGATIVE it strips the excess and shows
        EQUIP_REMOVE_CONFIRM ("the same equipment is equipped on another unit in the party, so the
        following equipment has been removed"). `owned` is `UnitEquipUtil::getEquipItemNum` ->
        `UserEquipItemInfoList::shared()` -> `getItemNum(id)` -- i.e. THIS list (item type 21's storage,
        `currency_model.md` §2). Serving one copy per distinct id while two party units shared the kit made
        the check strip a copy on every party edit (reported live 2026-09-24). So the count is the number
        of roster units equipping that id (never below what an explicit inventory row says).

        Token map (`readParam` `0x19244c4`, 2 tokens): `J1YX9kmM` = itemId, `Qy5EvcK1` = itemNum (>= 1; a
        zero would be read as a delete).
        """
        counts: dict[str, int] = {}
        order: list[str] = []
        for u in self.data["roster"]:
            for iid in (u.get("equipment") or {}).values():
                iid = str(iid)
                if iid and iid != "0":
                    if iid not in counts:
                        order.append(iid)
                        counts[iid] = 0
                    counts[iid] += 1
        for it in self.data.get("inventory", {}).get("equipment", []):
            iid = str(it.get("item_id", ""))
            if iid and int(it.get("count", 0) or 0) > 0:
                if iid not in counts:
                    order.append(iid)
                    counts[iid] = 0
                counts[iid] = max(counts[iid], int(it.get("count", 0) or 0))
        return [{"J1YX9kmM": iid, "Qy5EvcK1": str(max(1, counts[iid]))} for iid in order]

    def currency_groups(self) -> dict:
        """The currency-bearing reply groups (lapis, team = gil/friend/NRG, important items). Sent alongside
        a pull's RESULT so the client's balances and held-ticket count refresh without waiting for a separate
        user-state request (the gacha reply carries no `LhVz6aD2`, so `apply_to_reply` will not add them)."""
        g = self.groups()
        return {t: g[t] for t in ("J3pAG0I5", "3oU9Ktb7", "Md0N5abE") if t in g}

    def grant_ticket_group(self, group_id, count: int) -> int:
        """Test/dev hook: grant `count` of every shipped ticket in `F_TICKET_MST` group `group_id` (the
        server-side half of a held-ticket display test). Returns how many ticket rows were granted."""
        granted = 0
        for item_type, item_id, _rarity in ticket_group_items(group_id):
            self.grant_item(item_type, item_id, int(count))
            granted += 1
        return granted

    # --- mailbox (docs/notes/mailbox.md) -----------------------------------------------------------
    @property
    def mailbox(self) -> list:
        return self.data.setdefault("mailbox", [])

    # --- the daily-login calendar counter (SW-86) ---------------------------------------------------
    def advance_login_bonus(self, month: str, today: str, days_in_month: int = 28) -> int:
        """Move the login counter for `month` to today and return the day to serve.

        The counter is ours, not the client's: the reply's per-day `state` is the only thing the scene reads
        (`DailyLoginBonusInfo::onReadFinish`), so the server owns "which day am I on". Semantics:

        * a new `month` resets to 0 -- so the FIRST login of a month lands on **day 1**, whenever in the
          calendar month that login happens (logged in on the 10th -> day 1, not day 10);
        * `today` differing from `last_advance` advances by one, so two logins in one day do not double-count;
        * capped at `days_in_month` (the calendar layout's last cell), which then just stays there.
        """
        lb = self.data.setdefault("login_bonus", {"month": "", "day": 0, "last_advance": ""})
        if str(lb.get("month", "")) != str(month):
            lb["month"], lb["day"], lb["last_advance"] = str(month), 0, ""
        if str(lb.get("last_advance", "")) != str(today):
            if self._mutate():
                lb["day"] = min(int(lb.get("day", 0)) + 1, int(days_in_month))
            lb["last_advance"] = str(today)
        return int(lb.get("day", 0))

    def _progress(self) -> dict:
        """The progress store, created on demand. Shape and rationale in `default_state`'s comment."""
        # NAMED `quest_progress`, NOT `progress`: `progress` has been the switches + cleared-mission block
        # since the v2 schema, and adding a second `"progress"` key to the `default_state` literal silently
        # dropped the first (duplicate key, last wins) -- which the golden replay caught as a
        # `KeyError: 'switches'` on every reply. Two different meanings of "progress" in one file is exactly
        # what the naming has to prevent.
        pr = self.data.get("quest_progress")
        if not isinstance(pr, dict):
            pr = {"counters": {}, "scopes": {}, "audit": []}
            self.data["quest_progress"] = pr
        for k, v in (("counters", {}), ("scopes", {}), ("audit", [])):
            if not isinstance(pr.get(k), type(v)):
                pr[k] = v
        return pr

    def progress_value(self, key: str) -> int:
        """The counter for `key`, 0 if never recorded. Read-only -- it must not create the block."""
        pr = self.data.get("quest_progress")
        if not isinstance(pr, dict):
            return 0
        try:
            return int((pr.get("counters") or {}).get(str(key), 0) or 0)
        except (TypeError, ValueError):
            return 0

    def bump_progress(self, key: str, delta: int = 1, cap: int | None = None, what: str = "") -> int:
        """Add `delta` to one counter, clamped to `cap`, and append an audit row. Returns the new value.

        Idempotence is deliberately NOT handled here: a CUMULATIVE fact ("equipped a weapon 3 times") has to
        be able to grow, so every CALLER must only call when the action really happened. The audit tail is
        what makes that checkable after the fact, and it is capped so a `weekly` scope cannot grow the state
        file without bound. `frozen` refuses, like every other mutation.
        """
        pr = self._progress()
        try:
            cur = int(pr["counters"].get(str(key), 0) or 0)
        except (TypeError, ValueError):
            cur = 0
        new = max(0, cur + int(delta))
        if cap is not None and cap > 0:
            new = min(new, int(cap))
        if new == cur:
            return cur
        if not self._mutate():
            return cur
        pr = self._progress()
        pr["counters"][str(key)] = new
        pr["audit"].append({"when": _mail_time(), "key": str(key), "delta": int(delta), "value": new,
                            "what": str(what)})
        if len(pr["audit"]) > 400:               # bounded: a weekly scope would grow the state file forever
            del pr["audit"][:-400]
        return new

    # --- the DAILY QUEST ---------------------------------------------------------------------------
    # Two operations, both on the shared `quest_progress` counters, and both deliberately NOT resets:
    # `roll_daily_quests` changes the day's SET, and the progress of a previous day stays readable under its
    # own key. That is `panel_quest.md` §4f's rule ("a new period is a NEW KEY, not a wipe") applied to the
    # family that resets every day, and it is what lets the audit answer "when did this fire".

    def daily_quest_block(self) -> dict:
        """The `daily_quest` block, created on demand (a state from before 2026-09-26 has none)."""
        dq = self.data.get("daily_quest")
        if not isinstance(dq, dict):
            dq = {"day": "", "set": [], "pools": [], "resets": 0}
            self.data["daily_quest"] = dq
        for k, v in (("day", ""), ("set", []), ("pools", []), ("resets", 0)):
            if not isinstance(dq.get(k), type(v)):
                dq[k] = v
        return dq

    def roll_daily_quests(self, today: str, quest_ids: list, pools: list, force: bool = False) -> tuple:
        """Freeze today's set. Returns `(day, set, rolled)`; `rolled` is True only when it changed.

        The set is FROZEN for the day on purpose, and the rule is one line: **a stored set for `today` is
        authoritative**. A server that re-rolled per request would change the list under the player's finger,
        and a claim in flight would name a quest that is no longer on screen. So the ids are STORED, not
        recomputed -- a season change mid-day does not reshuffle the list, and a restart reads back the same
        list. `quest_ids` is used only on a day boundary (or when there is no set at all), and the roll is
        counted in `resets` so "the list changed" is auditable from the state alone.

        `force=True` is the ONE exception and it is deliberate: `DAILY_QUEST_SET` is an explicit operator pin,
        and a pin that could not take effect until the next day would be a knob that lies. The caller passes
        `force` only when the pin is set, and the audit row records it, so "the list changed" and "the list was
        re-pinned" are distinguishable after the fact.
        """
        dq = self.daily_quest_block()
        stored = [str(q) for q in (dq.get("set") or [])]
        same_day = str(dq.get("day") or "") == str(today)
        if same_day and stored and (not force or stored == [str(q) for q in quest_ids]):
            # A pin that already matches the stored set has nothing to do. The first version forced on EVERY
            # carrier request and re-rolled an identical set three times in one run: the content was stable,
            # but `resets` became meaningless and the real roll was buried in audit noise -- an instrument
            # reporting a fiction, which is the failure this project keeps paying for.
            return str(today), stored, False
        if not self._mutate():
            return str(dq.get("day") or today), [str(q) for q in (dq.get("set") or [])], False
        dq = self.daily_quest_block()
        dq["day"] = str(today)
        dq["set"] = [str(q) for q in quest_ids]
        dq["pools"] = [str(p) for p in pools]
        try:
            dq["resets"] = int(dq.get("resets", 0) or 0) + 1
        except (TypeError, ValueError):
            dq["resets"] = 1
        self._progress()["audit"].append({"when": _mail_time(), "key": f"dailyquest/roll/{today}",
                                          "delta": len(dq["set"]), "value": len(dq["set"]),
                                          "what": f"daily quest set{' (RE-PINNED)' if force else ''}: "
                                                  f"{', '.join(dq['set']) or '(empty)'}"})
        return str(today), [str(q) for q in dq["set"]], True

    def daily_quest_ids(self) -> list:
        """Today's set, as strings. Read-only -- a projection must not create the block."""
        dq = self.data.get("daily_quest")
        if not isinstance(dq, dict):
            return []
        return [str(q) for q in (dq.get("set") or [])]

    def daily_quest_day(self) -> str:
        dq = self.data.get("daily_quest")
        return str(dq.get("day") or "") if isinstance(dq, dict) else ""

    def daily_progress_key(self, quest_id: str, day: str = "") -> str:
        """The counter key for one quest on one day: `dailyquest/<day>/<questId>`.

        The day is part of the key, which is the whole reset discipline: yesterday's "sent 5 gifts" is still
        there under yesterday's key and cannot be spent on today's quest.
        """
        return f"dailyquest/{day or self.daily_quest_day()}/{quest_id}"

    def daily_claim_key(self, quest_id: str, day: str = "") -> str:
        """`claimed/daily/<day>/<questId>` -- the same `claimed/...` namespace the panel claim uses, so
        "already paid" is one lookup for both families."""
        return f"claimed/daily/{day or self.daily_quest_day()}/{quest_id}"

    def declare_scope(self, key: str, period: str = "once", persistent: bool = True) -> dict:
        """Record a schedule's reset discipline ONCE; an existing scope is left alone, never re-armed.

        A new period is a new key (the caller includes the period in it), so this only ever records the
        first sighting -- which is what `IsPersistent` needs to decide whether the reward carries over.
        """
        pr = self._progress()
        if str(key) in pr["scopes"]:
            return pr["scopes"][str(key)]
        if not self._mutate():
            return {}
        pr = self._progress()
        pr["scopes"][str(key)] = {"period": str(period), "persistent": bool(persistent),
                                 "first_seen": _mail_time()}
        return pr["scopes"][str(key)]

    def _anniversary(self) -> dict:
        """The per-day ticket marker, created on demand (a state written before this feature has no block)."""
        an = self.data.get("anniversary")
        if not isinstance(an, dict):
            an = {"last_issued": "", "issued_total": 0}
            self.data["anniversary"] = an
        return an

    def anniversary_issued_today(self, today: str) -> bool:
        """Has today's ticket already been issued? The marker outlives the MAIL, so a delete cannot re-arm it."""
        return str(self._anniversary().get("last_issued", "") or "") == str(today)

    def mark_anniversary_issued(self, today: str) -> bool:
        """Record the issue. Returns False when today was already recorded (so the caller sends nothing)."""
        if not self._mutate():
            return False
        an = self._anniversary()
        if str(an.get("last_issued", "") or "") == str(today):
            return False
        an["last_issued"] = str(today)
        an["issued_total"] = int(an.get("issued_total", 0) or 0) + 1
        return True

    def _catchup(self) -> dict:
        """The catch-up counters, created on demand (a state written before 2026-09-26 has no block)."""
        cu = self.data.get("catchup")
        if not isinstance(cu, dict):
            cu = self._new_catchup()
            self.data["catchup"] = cu
        return cu

    def catchup(self) -> dict:
        """The live catch-up counters, READ ONLY -- for a projection that must not create the block.

        `advance_catchup` returns a SNAPSHOT taken before the day's payments, so anything that projects the
        player's progress afterwards (the `cksL9bW8` tier list) has to read the state again or it reports the
        previous day. That was a real off-by-one-day bug: a day that paid six tiers served an empty list.
        """
        cu = self.data.get("catchup")
        return cu if isinstance(cu, dict) else self._new_catchup()

    @staticmethod
    def _new_catchup() -> dict:
        return {"total_login_days": 0, "last_advance": "", "serial_days": 0, "ladder_bit": 0,
                "ladder_tier": 0, "campaign_id": "", "campaign_day": 0, "welcome_done": False,
                "granted": []}

    def grant_catchup_welcome(self, amounts: dict) -> list:
        """The AUTHORED fresh-account grant (SW-99), fired once. Returns the rows to POST, [] if done.

        Nothing is credited here: the award IS the mail the caller delivers, and the first claim through
        `receive_mail` is what moves the currencies (the same shape as the daily calendar's award and the
        anniversary ticket -- `deliver_login_mail`, `deliver_anniversary_ticket`).

        Retail's real opening gift was server-granted and survives in no artefact we hold
        (`docs/notes/true_new_player_state.md` §4), so `amounts` is authored data and is labelled as such at
        its one home (`server/seasons.json` `default_login_bonus.welcome`). It is recorded on the FIRST login
        rather than at state creation so that it rides the same request as everything else the account is
        given, and so `--state-mode frozen` refuses it like every other mutation.
        """
        if not amounts or self._catchup().get("welcome_done"):
            return []
        if not self._mutate():
            return []
        cu = self._catchup()
        award = []
        for cur, amount in amounts.items():
            try:
                n = int(amount)
            except (TypeError, ValueError):
                continue
            if n <= 0 or cur not in self.currencies:
                continue                              # unknown currency: dropped, never coerced
            award.append({"currency": cur, "amount": n})
        cu["welcome_done"] = True
        cu.setdefault("granted", []).append(
            {"when": _mail_time(), "what": "welcome", "detail": ",".join(f"{c['currency']}={c['amount']}"
                                                                       for c in award)})
        return award

    def advance_catchup(self, today: str, campaign_id: str = "", campaign_days: int = 0) -> dict:
        """Move the catch-up counters to `today` and report what became due. One login per calendar day.

        Four schedules advance together, all server-side:

        * `total_login_days` -- the axis of the shipped cumulative ladder. The FIRST login of an account is
          day 1 (the ladder's first threshold is 2, so nothing is due yet -- the same "a new month starts at
          day 1" rule the daily calendar uses, `advance_login_bonus`). It counts LOGINS, not elapsed days: a
          calendar gap still advances it by one;
        * `serial_days` -- **consecutive** logins, which is what the new-player calendar counts. It goes back
          to 1 when the previous advance was not the previous calendar day, so a missed day restarts the
          15-day shipped ladder the way a consecutive-day counter does (served as `68Vn2FrK`
          SerialLoginCnt, read by `NewPlayerLoginBonusScene::isNewPlayerLoginBonusValid` 0x2306d7c);
        * `ladder_bit` -- every tier whose `days` the player has now passed is paid ONCE, keyed by the tier's
          own `QLfe23bu` bit, so a long gap pays every tier it crossed rather than only the last;
        * `campaign_day` -- the selected special-login campaign's day counter, reset to 0 when the season
          selects a DIFFERENT campaign (a new calendar starts at its day 1).

        A repeated call on the same date is a no-op and returns nothing due, so this is safe to call from
        every reply that carries the login-bonus groups.
        """
        cu = self._catchup()
        due: dict = {"total_login_days": int(cu.get("total_login_days", 0) or 0),
                     "serial_days": int(cu.get("serial_days", 0) or 0),
                     "ladder_tier": int(cu.get("ladder_tier", 0) or 0),
                     "ladder_bit": int(cu.get("ladder_bit", 0) or 0),
                     "campaign_id": str(cu.get("campaign_id", "") or ""),
                     "campaign_day": int(cu.get("campaign_day", 0) or 0), "advanced": False}
        if str(cu.get("campaign_id", "") or "") != str(campaign_id or ""):
            if not self._mutate():
                return due
            cu["campaign_id"] = str(campaign_id or "")
            cu["campaign_day"] = 0
            due["campaign_id"], due["campaign_day"] = cu["campaign_id"], 0
        if str(cu.get("last_advance", "")) == str(today):
            return due
        if not self._mutate():
            return due
        cu["last_advance"] = str(today)
        cu["total_login_days"] = int(cu.get("total_login_days", 0) or 0) + 1
        prev = str(cu.get("prev_advance", "") or "")
        cu["serial_days"] = int(cu.get("serial_days", 0) or 0) + 1 if _next_day(prev, today) else 1
        cu["prev_advance"] = str(today)
        if campaign_id:
            cu["campaign_day"] = min(int(cu.get("campaign_day", 0) or 0) + 1, max(1, int(campaign_days or 0)))
        due.update({"total_login_days": int(cu["total_login_days"]), "serial_days": int(cu["serial_days"]),
                    "campaign_day": int(cu.get("campaign_day", 0) or 0), "advanced": True})
        return due

    def pay_catchup_ladder(self, tiers: list) -> list:
        """Mark every shipped ladder tier the player has passed and not yet been paid; return those tiers.

        `tiers` is `server/catchup_bonus.json`'s `total_ladder.tiers` (shipped rows, ascending by `days`).
        The idempotence key is the tier's own `QLfe23bu` bit, so this is once-per-tier even if the tiers
        arrive out of order or several fall due at once. The caller posts each returned tier to the in-box;
        NOTHING is credited here -- the mail is the award (see `deliver_catchup_rewards`).
        """
        cu = self._catchup()
        days = int(cu.get("total_login_days", 0) or 0)
        paid_bit = int(cu.get("ladder_bit", 0) or 0)
        out = []
        for tier in sorted(tiers or [], key=lambda t: int(t.get("days", 0) or 0)):
            try:
                tdays, bit = int(tier.get("days", 0) or 0), int(tier.get("bit", 0) or 0)
            except (TypeError, ValueError):
                continue
            if tdays <= 0 or tdays > days or (bit and bit <= paid_bit):
                continue
            out.append(tier)
        if not out or not self._mutate():
            return []
        top = max(out, key=lambda t: int(t.get("days", 0) or 0))
        cu["ladder_bit"] = max(paid_bit, int(top.get("bit", 0) or 0))
        cu["ladder_tier"] = int(top.get("index", 0) or 0)
        for tier in out:
            # NOT credited here: each tier is posted to the in-box by the caller and credited when the
            # client's Receive button claims it (receive_mail -> _grant_items).
            cu.setdefault("granted", []).append(
                {"when": _mail_time(), "what": "ladder",
                 "detail": f"tier {tier.get('index')} @ total day {tier.get('days')}: {tier.get('label', '')}"})
        return out

    def pay_catchup_day(self, campaign_id: str, day: int, items: list) -> list:
        """Pay one day of the selected special-login campaign. `items` are the shipped row's entries.

        The counter (`campaign_day`) was already advanced by `advance_catchup`, so this only MARKS the due
        day and returns nothing; the caller posts the rows to the in-box and the claim credits them. A day
        beyond the campaign's length is refused by the caller, not here.
        """
        if not campaign_id or day <= 0 or not items or not self._mutate():
            return []
        cu = self._catchup()
        # NOT credited here: the caller posts this day to the in-box and the claim credits it. The audit
        # entry names the shipped rows so a paid day stays readable without a grant.
        cu.setdefault("granted", []).append(
            {"when": _mail_time(), "what": f"sp:{campaign_id}",
             "detail": f"day {day}: " + ",".join(f"{r.get('kind')}:{r.get('id')}x{r.get('count')}"
                                                for r in items if isinstance(r, dict))})
        return []

    def _grant_items(self, items) -> list:
        """Credit reward entries (`kind:id:count`) into the right place and return ONLY what was projected.

        The SAME split `receive_mail` uses: a currency kind (30 gil / 50 lapis / 51 friend point) is credited
        to the currency field, everything else goes to the bag via `grant_item`. One place, so a ladder tier
        and a mail attachment can never disagree about what a `30:10000` row means.

        **A returned row means the CLIENT CAN SEE IT** (audit 2026-09-27). That is the whole contract, and
        it was not true before: `_grant_items` banked *any* kind into `inventory.items` and returned it, so a
        caller's "did every row credit?" check was a tautology and a reward no reply group projects was logged
        as `paid`. Concretely, `10:<unitId>:<n>` -- the reward of **10 of the 49 quest rows this server
        serves**, including the two Advanced-Quest element-chain milestones -- was credited as an
        `item_type` 10 bag row that `item_rows` (type 20 only), `important_item_rows` (23 only) and
        `equip_item_rows` (equipment) never emit, and the roster never grew. Quest `9000000`'s shipped name
        is *"Get Lightning! Clear Lanzelt Ruins - Interior!"* and it pays `10:213000105:1:100:1` -- so the
        quest that promises you Lightning was paying you nothing at all.
        """
        rows = []
        for it in items or []:
            if not isinstance(it, dict) or "kind" not in it:
                continue                     # a TUPLE row is silently skipped -- see the claim path, which
            kind, iid, count = int(it["kind"]), int(it.get("id", 0) or 0), int(it.get("count", 0) or 0)
            if count <= 0:
                continue
            cur = STORAGE_CURRENCY.get(kind)
            if cur:
                self.add_currency(cur, count)
            elif str(kind) == "21":
                # EQUIP goes to `inventory.equipment`, NOT the generic bag: `equip_item_rows` (the
                # `MBPCGT84` / `UserEquipItemInfoResponse` group the client's Equipment screen reads) is
                # built from the roster's equipped ids plus THIS list, so an equip reward dropped into
                # `items` is owned-but-invisible -- exactly the blank-frame failure
                # `docs/notes/unit_equip_display.md` describes. Found by the panel-quest claim: quest
                # 1000101 pays `21:403046900:1` and it was landing in the wrong bucket.
                self.grant_equipment(iid, count)
            elif str(kind) == "10":
                # A UNIT. It belongs in the ROSTER, which is what `UserUnitInfoResponse` (`B71MekS8`) is
                # built from; nothing reads an `item_type` 10 row out of the bag. `granted_rarity` is left
                # None, which is what the starter roster carries and what the live client already reads.
                made = self._grant_reward_units(iid, count)
                if not made:
                    continue                 # unknown id, or `frozen` refused: NOT credited, NOT reported
                count = made
            elif str(kind) in GRANT_BAG_PROJECTED:
                self.grant_item(kind, iid, count)
            else:
                # NO PROJECTION for this kind -- e.g. 60 is a RECIPE BOOK (`F_RECIPE_BOOK_MST`
                # `staFu8U1`, e.g. `1000000125` = "Onimaru +", paid by quest `9000001`), and 22/27 are
                # materia/vision-card kinds with no served reader. Bank it in the bag and the player owns
                # something no screen can show; report it as paid and the log lies. So: do not credit, do
                # not report, and say so -- the caller's shortfall check then refuses the whole claim, which
                # is the honest outcome (an unclaimable reward, loudly) rather than a silent one.
                print(f"grant: REFUSED kind {kind} id {iid} x{count} -- no reply group projects this kind, "
                      f"so crediting it would be invisible to the client", flush=True)
                continue
            rows.append({"kind": kind, "id": iid, "count": count})
        return rows

    def _grant_reward_units(self, master_id, count: int) -> int:
        """A `10:<unitId>:<n>` reward into the ROSTER. Returns how many were actually granted (0 = refused).

        Fail closed on an id with no `F_UNIT_MST` row -- `grant_unit` raises on one, and a claim must never
        turn a bad reward row into a 500. No count cap: the parameter is SHIPPED data, not request data
        (`_quest_reward` reads `dX6cor8j` out of the table), and the measured maximum over both quest tables
        is **35** copies (`F_LOGIN_BONUS_SP_REWARD_MST` peaks at 30), so a cap would be an invention.
        """
        if unit_mst(master_id) is None:
            print(f"grant: REFUSED unit reward {master_id!r} -- not an F_UNIT_MST id, and a unit is never "
                  f"guessed", flush=True)
            return 0
        made = 0
        for _ in range(int(count)):
            if self.grant_unit(master_id) is not None:
                made += 1
        if made and made != int(count):
            print(f"grant: unit reward {master_id} granted {made}/{count} -- the state refused the rest",
                  flush=True)
        return made

    def grant_equipment(self, equip_id, count: int = 1) -> int:
        """Own `count` copies of an equipment id in `inventory.equipment` (the client's owned-equip list)."""
        if not self._mutate():
            return 0
        bag = self.data.setdefault("inventory", {}).setdefault("equipment", [])
        for it in bag:
            if str(it.get("item_id")) == str(equip_id):
                it["count"] = int(it.get("count", 0) or 0) + int(count)
                return it["count"]
        bag.append({"item_id": int(equip_id), "count": int(count)})
        return int(count)

    def next_mail_id(self) -> int:
        """The next free mail id (a mail id is what `MailReceiptRequest` names)."""
        top = 0
        for m in self.mailbox:
            try:
                top = max(top, int(m.get("mail_id", 0)))
            except (TypeError, ValueError):
                continue
        return top + 1

    def deliver_mail(self, title: str, items, *, mail_id=None, message: str = "",
                     icon_type: int = 0, attach_type: int | None = None, daily_login: bool = False,
                     send_date: str | None = None, protect_date: str = "") -> dict | None:
        """Append a mailbox entry carrying `items`, each `(item_type, item_id, count)`.

        Idempotent per `mail_id` (re-delivering the same id returns the existing entry), so a reply can call
        it without its own guard. `items` are authored by the caller, not validated here; `receive_mail`
        grants them through the normal item path. `icon_type` is the wire `9m5yIkCK` (0 mail / 1 attention /
        2 treasurebox / 3 invitation+gold). A **daily-login** mail is identified by the client via TITLE and
        MESSAGE, not a type: the title must be exactly `SG_DAILY_LOGIN_TITLE` and the message a decimal
        integer (`isDailyLoginReward()` `0x1d80058`; docs/notes/mailbox.md §3). `frozen` refuses.
        """
        if not self._mutate():
            return None
        mid = str(mail_id if mail_id is not None else self.next_mail_id())
        for m in self.mailbox:
            if str(m.get("mail_id")) == mid:
                return m
        entry = {
            "mail_id": mid, "title": str(title), "message": str(message),
            "icon_type": int(icon_type),
            # AttachType must be 1 for the detail to render the reward items and the Receive button: a direct
            # field load `ldr w8,[mail+0x68]; cmp #1; b.ne skip` in `MailDetailScene::setLayoutMessage`
            # (0x2242c8c-0x2242c94). It is read WITHOUT its accessor, which is why a `bl` sweep missed it.
            "attach_type": int(attach_type) if attach_type is not None else (1 if items else 0),
            "receive_type": 0, "daily_login": bool(daily_login),
            "send_date": send_date or _mail_time(), "open_date": "", "receive_date": "",
            "protect_date": str(protect_date),
            "items": [{"item_type": int(t), "item_id": int(i), "count": int(c)} for t, i, c in items],
        }
        self.mailbox.append(entry)
        return entry

    def receive_mail(self, mail_id) -> list:
        """Mark a mail received and grant its attachments through the ONE credit path. Returns the rows.

        The award IS the mail: nothing is credited when a mail is delivered, only when the client's Receive
        button names it. Every attachment goes through `_grant_items`, so a mailed unit (`10:`) lands in the
        roster and a mailed equipment (`21:`) in the owned-equipment list -- the split this method used to
        hand-roll turned both into bag rows no reply group projects (APP-45).

        **All-or-nothing**: rows are validated BEFORE anything is credited, because a partial grant whose
        claim is then refused would credit twice on the retry. Idempotent: an already-received mail grants
        nothing again; unknown id -> `[]`.
        """
        if self.mode == "frozen":
            return []
        mid = str(mail_id)
        for m in self.mailbox:
            if str(m.get("mail_id")) != mid:
                continue
            if int(m.get("receive_type", 0) or 0) == 2:
                return []
            rows = [{"kind": int(it.get("item_type", 0) or 0), "id": int(it.get("item_id", 0) or 0),
                     "count": int(it.get("count", 0) or 0)} for it in m.get("items") or []]
            bad = [r for r in rows if not self._can_credit(r)]
            if bad:
                print("mail %s: NOT received -- no projection for %s, so nothing was credited"
                      % (mid, ", ".join(f"{r['kind']}:{r['id']}x{r['count']}" for r in bad)), flush=True)
                return []
            granted = self._grant_items(rows)
            m["receive_type"] = 2
            m["receive_date"] = _mail_time()
            self.dirty = True
            return granted
        return []

    def _can_credit(self, row: dict) -> bool:
        """Whether `_grant_items` can project this row -- the pre-check `receive_mail` needs so a claim is
        all-or-nothing. Mirrors `_grant_items`' routing (currency / 21 equipment / 10 unit / bag kinds);
        keep the two in step when a kind is added."""
        kind, iid, count = (int(row.get("kind", 0) or 0), int(row.get("id", 0) or 0),
                            int(row.get("count", 0) or 0))
        if count <= 0:
            return False
        if kind in STORAGE_CURRENCY or str(kind) == "21":
            return True
        if str(kind) == "10":
            return unit_mst(iid) is not None
        return str(kind) in GRANT_BAG_PROJECTED

    def delete_mail(self, mail_id) -> bool:
        """Drop a mail from the mailbox (the `receiptType == 3` path). True iff it existed. `frozen` refuses."""
        mid = str(mail_id)
        if self.mode == "frozen" or not any(str(m.get("mail_id")) == mid for m in self.mailbox):
            return False
        self.data["mailbox"] = [m for m in self.mailbox if str(m.get("mail_id")) != mid]
        self.dirty = True
        return True

    def delete_received_mail(self) -> int:
        """Drop every **claimed** mail (`receive_type == 2`) and return how many were removed.

        The mail screen's bulk delete: its request names no ids (`MailTopScene::getDeleteMailRequest` 0x224e808
        only sets `receiptType = 3`), and the button is gated on the received count. `frozen` refuses.
        """
        if self.mode == "frozen":
            return 0
        keep = [m for m in self.mailbox if int(m.get("receive_type", 0) or 0) != 2]
        removed = len(self.mailbox) - len(keep)
        if removed:
            self.data["mailbox"] = keep
            self.dirty = True
        return removed

    def mail_rows(self) -> list:
        """The `UserMailInfoResponse` (`qja26wHb`) rows for `MailListRequest`. Token map: `docs/notes/mailbox.md` §1."""
        rows = []
        for m in self.mailbox:
            rows.append({
                "ynP26rjD": str(m.get("mail_id", "")),         # MailId (u64)
                "9m5yIkCK": str(m.get("icon_type", 0)),         # IconType (NOT "MailType")
                "fbcj5Aq9": str(m.get("title", "")),           # Title
                "Wk1v6upb": str(m.get("message", "")),         # Message
                "u7Z1DYz4": str(m.get("attach_type", 0)),       # AttachType (no reader)
                "7iJpH5zZ": _mail_item_string(m.get("items") or []),  # TargetInfo = the reward list
                "14Y7jihw": str(m.get("receive_type", 0)),      # ReceiveType (0/1/2)
                "f5LHNT4v": str(m.get("send_date", "")),        # SendDate
                "k0qX5yQd": str(m.get("open_date", "")),        # OpenDate
                "0Sba4mZ9": str(m.get("receive_date", "")),     # ReceiveDate
                "eGv8Da2s": str(m.get("protect_date", "")),     # ProtectDate
            })
        return rows

    def mail_counters(self) -> list:
        """`OpeMailListResponse` (`t8TPs6o2`) counters. `2BW7YLwg` (total) sizes the mail page -- without
        it `MailTopScene::setMailList` builds **zero rows** (docs/notes/mailbox.md §1.2). `sC84jvZH` is the
        HOME mail badge."""
        total = len(self.mailbox)
        unread = sum(1 for m in self.mailbox if int(m.get("receive_type", 0) or 0) == 0)
        received = sum(1 for m in self.mailbox if int(m.get("receive_type", 0) or 0) == 2)
        protect = sum(1 for m in self.mailbox if str(m.get("protect_date") or ""))
        return [{"2BW7YLwg": str(total), "mzR39KXj": str(received),
                 "TnZ3J4v8": str(protect), "sC84jvZH": str(unread)}]

    def grant_switch(self, stype, index: int) -> None:
        if not self._mutate():
            return
        self.switches.setdefault(str(stype), set()).add(int(index))

    def add_volatile_switches(self, mapping) -> int:
        """Merge switch bits that must be SERVED but never PERSISTED, and return how many were added.

        A season opens switches the client needs for the session; they have to live in `switches` or no
        reader would see them, but they must not survive into a checkpoint (a later `--season off` run
        would keep them). Recording exactly the bits THIS call added lets `_persist_view` subtract them
        from every save. A bit already present in the saved state is the player's and is left alone.
        """
        added = 0
        for stype, bits in (mapping or {}).items():
            key = str(stype)
            bucket = self.switches.setdefault(key, set())
            for index in bits or ():
                i = int(index)
                if i not in bucket:
                    bucket.add(i)
                    self._volatile_switches.add((key, i))
                    added += 1
        return added

    def owns_beast(self, esper_id) -> bool:
        """Whether the account already owns this esper -- the guard a story grant needs, because
        `grant_beast` is idempotent by id but still rewrites rarity/level, which would reset an esper the
        player has since raised every time they re-clear the beat."""
        eid = str(int(esper_id))
        return any(str(b.get("esper_id")) == eid for b in self.data.get("beasts", []))

    def unlink_beast(self, esper_id) -> int:
        """Clear every beast-deck slot holding this esper. Returns how many slots were cleared.

        A removed esper MUST leave the decks too: the deck rows are projected to the client as
        `<slot>:<esperId>,…`, and a slot naming an esper the account does not own is exactly the
        'selection naming a non-roster instance' incoherence `state_coherence_issues` exists to catch.
        """
        if not self._mutate():
            return 0
        eid = int(esper_id)
        cleared = 0
        for did, slots in (self.data.get("beast_decks") or {}).items():
            fixed = [0 if int(s or 0) == eid else int(s or 0) for s in slots]
            if fixed != [int(s or 0) for s in slots]:
                cleared += sum(1 for s, f in zip(slots, fixed) if int(s or 0) == eid)
                self.data["beast_decks"][did] = fixed
        return cleared

    def remove_beast(self, esper_id) -> bool:
        """Ungrant an owned esper, deck slots included. Returns whether a row was removed.

        The inverse of `grant_beast`, for a state reversal (2026-10-07: the operator removed the Siren a
        `BEAST_SEED` shortcut had granted, so that clearing セイレーンの塔・最上階 could be SEEN to grant her).
        """
        if not self._mutate():
            return False
        eid = str(int(esper_id))
        before = len(self.data.get("beasts", []))
        self.data["beasts"] = [b for b in self.data.get("beasts", []) if str(b.get("esper_id")) != eid]
        self.unlink_beast(int(eid))
        return len(self.data["beasts"]) != before

    def clear_mission(self, mission_id) -> str | None:
        """Record a clear AND grant the mission's own switch(es) (schema §2.3): the flag lives in
        `F_MISSION_MST.dIPkNn61` and its bit is mapped by `F_SWITCH_MST`. **That field is a COMMA LIST** —
        ！75 of the 17690 shipped missions name more than one switch, and every one of the 75 names at least
        one type-101 member: `1110404` (セイレーンの塔・最上階) is `10007400,82001100` and `1110703`
        (グランシェルト城内・玉座前) is `10010300,10010004`. Reading the whole string as one id made
        `switch_position` miss on a field no table has a row for, so **every one of those missions granted
        nothing** — including the beat that opens the Esper screen. Found 2026-10-07, before a run.

        Grants every member of type `MISSION_SWITCH_TYPE` and prints the members this op does not track
        (a type-820/200/201/720/9002/103 bit is a separate decision — 57 of the 75 pairs name a type-820
        switch, which nothing serves yet). Returns the granted ids, or None when nothing was granted.
        `frozen` refuses before recording anything, like every op.
        """
        if not self._mutate():
            return None
        mid = str(mission_id)
        if mid not in self.data["progress"]["cleared_missions"]:
            self.data["progress"]["cleared_missions"].append(mid)
        sw = mission_open_switch(mid)
        if not sw:
            return None
        granted: list[str] = []
        for raw in str(sw).split(","):
            sid = raw.strip()
            if not sid:
                continue
            pos = switch_position(sid)
            if pos is None:
                print(f"    switch: mission {mid} opens {sid}, which has no F_SWITCH_MST row — not granted",
                      flush=True)
                continue
            if pos[0] != MISSION_SWITCH_TYPE:
                print(f"    switch: mission {mid} opens {sid} (type {pos[0]} index {pos[1]}) — type not "
                      f"tracked by this op, not granted", flush=True)
                continue
            self.grant_switch(pos[0], pos[1])
            granted.append(sid)
        return ",".join(granted) if granted else None

    def mark_challenge_cleared(self, mission_id, challenge_id) -> bool:
        """Record a challenge in the CUMULATIVE cleared set only -- the set the client derives a
        mission's "complete" icon from (`serve_challenge_clear` -> `qdhWF7z9`). Used for a met challenge
        that carries NO lapis reward, so "cleared" does not depend on there being something to pay.
        Returns whether it was newly added."""
        if not self._mutate():
            return False
        mid, cid = str(mission_id), str(challenge_id)
        got = self.cleared_challenges.setdefault(mid, set())
        if cid in got:
            return False
        got.add(cid)
        return True

    def credit_challenge(self, mission_id, challenge_id) -> bool:
        """First-clear only. Records the id in BOTH the cumulative set and the paid set; returns whether
        this call was the one that paid (i.e. first clear)."""
        if not self._mutate():
            return False
        mid, cid = str(mission_id), str(challenge_id)
        self.cleared_challenges.setdefault(mid, set()).add(cid)
        if cid in self.paid_challenges.get(mid, set()):
            return False
        self.paid_challenges.setdefault(mid, set()).add(cid)
        return True

    def set_active_deck(self, deck_id) -> None:
        if self._mutate():
            self.data["party"]["active_deck"] = int(deck_id)

    def add_unit_exp(self, instance_id, amount: int) -> dict | None:
        """Credit unit EXP and advance the level by walking `needExp` FORWARD.

        **The model, recovered 2026-09-26 and corrected here.** `93fnYUJG` is a PER-LEVEL STEP and the
        client reads the table forward only (`GameUtils::getNeedUnitExp` @`0x19deab4`). `UnitInfoBase::getLv()`
        @`0x1967d40` is a bare field load, so **the level is the server's to state**; and
        `getRestUnitExp = needExp(level+1) - exp` makes **`exp` a residual WITHIN the current level**.

        This docstring asserted the opposite for a week -- that `exp` was a running total and the level the
        point the accumulated steps reached -- and that is what made `level_for_exp` (still present, now
        unused for the wire) and the whole `exp`-clamping logic wrong. The live symptom was a NEGATIVE
        "exp to next level" for every unit, measured at `needExp(21)=4415` against a served `exp=35709`.
        """
        u = self.roster_row(instance_id)
        if u is None or not self._mutate():
            return u
        amount = int(amount)
        # `exp` is a RESIDUAL WITHIN THE CURRENT LEVEL, not a running total -- recovered from
        # `getRestUnitExp = needExp(level+1) - exp` and `getLv()` being a bare field load (2026-09-26).
        # The old line added to a cumulative, which is what produced out-of-range residuals and units
        # that jumped levels in the client's own arithmetic. `credit_exp` walks forward instead.
        lv, ex = credit_exp(u["master_id"], int(u.get("level", 1)), int(u.get("exp", 0) or 0), amount)
        u["level"], u["exp"] = lv, ex
        u["total_exp"] = int(u.get("total_exp", 0)) + amount
        # AT MAX LEVEL THE COUNTER PINS. Credit past the cap is discarded, exactly as the level is capped
        # (`max_level` / `level_for_exp`): an uncapped credit is what produced level 42/40 live
        # 2026-09-24. Both counters are pinned, `total_exp` included because `GameUtils::getMixExp` reads
        # it as the unit's fusion value -- letting it run would inflate a maxed unit's worth on every
        # later fusion. (The conservative direction; if retail is ever observed accumulating past the
        # cap, only this line changes.)
        cap = max_level_exp(u["master_id"])
        if cap is not None:
            # Only `total_exp` pins now. `exp` is a residual and is already bounded by the level walk;
            # clamping it against a CUMULATIVE total was the same unit error in a second place.
            u["total_exp"] = min(u["total_exp"], int(cap))
        return u

    def max_unit(self, instance_id) -> dict | None:
        """Drive one roster row to its master's cap: `level = max_level`, residual `exp = 0`,
        `total_exp` pinned at `max_level_exp`, `lb_level` at least 1 when the unit owns an LB.

        The state-side half of the max-roster seed (`capture_server.py --grant-unit all`). It is also what
        makes a LEVEL-gated ability list light up: `UnitSkillUtil::setupUserUnitSkillIdList` (`0x1a5c488`)
        drops `F_UNIT_SERIES_LV_ACQUIRE_MST` rows above the unit's level or rarity
        (docs/notes/unit_ability_list.md §1), so a capped row at its top rarity learns everything the
        shipped tables teach it. exp=0 is inside every residual range, so the invariant `validate_state`
        checks holds at the cap for free. A unit whose cap is unknown (0/absent) is left alone rather
        than guessed at.
        """
        u = self.roster_row(instance_id)
        if u is None or not self._mutate():
            return u
        mid = u["master_id"]
        cap = max_level(mid)
        if cap <= 0:
            return u
        u["level"] = cap
        u["exp"] = 0
        total = max_level_exp(mid)
        if total is not None:
            u["total_exp"] = int(total)
        if limit_burst(mid) is not None:
            u["lb_level"] = max(int(u.get("lb_level") or 0), 1)
        return u

    def set_unit_level(self, instance_id, level) -> dict | None:
        """Pin a roster row at `level`: residual `exp = 0`, `total_exp` = the cumulative exp to that level
        (the lifetime counter stays coherent), `lb_level` ≥ 1 where the unit owns an LB. Clamped to the
        master's cap; a unit with no derivable cap is left alone.

        The general form of `max_unit`, used by `--grant-unit all --unit-level N` to build the LOW roster
        (every unit owned, all at level 1) for the screens and tests that need a low-level unit — the same
        base-side sweep, `top_named_unit_rows()`, so the Brave-Shift forms stay out.
        """
        u = self.roster_row(instance_id)
        if u is None or not self._mutate():
            return u
        mid = u["master_id"]
        cap = max_level(mid)
        if cap <= 0:
            return u
        lv = max(1, min(int(level), cap))
        u["level"] = lv
        u["exp"] = 0
        u["total_exp"] = int(exp_cumulative(exp_pattern(mid), lv))
        if limit_burst(mid) is not None:
            u["lb_level"] = max(int(u.get("lb_level") or 0), 1)
        return u

    def party_instance_ids(self) -> list:
        """The active deck's member instance ids, in slot order (the units a battle rewards)."""
        decks = self.data.get("party", {}).get("decks") or []
        active = str(self.data.get("party", {}).get("active_deck", 0))
        deck = next((d for d in decks if str(d.get("id")) == active), None) or (decks[0] if decks else None)
        if not deck:
            return []
        return [str(m.get("instance_id")) for m in (deck.get("members") or []) if m.get("instance_id")]

    def tutorial_gacha(self, master_id=None, granted_rarity=None) -> dict:
        """The forced opening summon: ONE authored award, **granted to the roster**, idempotent.

        The tutorial request is a bare shell (no banner), so the award is authored (`gacha_authored.json`);
        the effect pattern follows the GRANTED rarity, never the master row's base (schema §3.8, SW-61).
        Called again it returns the stored result rather than granting a second copy -- repeat-pull gacha
        (a real banner) is G1+ work.
        """
        g = self.data.setdefault("gacha", {})
        if g.get("last_result"):
            return g["last_result"]
        if master_id is None:
            master_id = "100000315"
        if granted_rarity is None:
            granted_rarity = int((unit_mst(master_id) or {}).get("9fW0TePj", 1) or 1)
        granted = self.grant_unit(master_id, granted_rarity=int(granted_rarity))
        result = {"unit_id": str(master_id), "granted_rarity": int(granted_rarity),
                  "pattern_id": effect_pattern_for(granted_rarity),
                  "instance_id": (granted or {}).get("instance_id"),
                  "level": int((granted or {}).get("level") or 1)}
        if granted is not None:
            self.data["gacha"]["last_result"] = result
        return result

    def banner_state(self, banner_id) -> dict:
        """The player's per-banner gacha state (`UserGachaInfo`): executions this term vs total. The term
        window itself is the authored banner's, not stored here."""
        return self.data.setdefault("gacha", {}).setdefault("user_banners", {}).setdefault(
            str(banner_id), {"term_exe_count": 0, "total_exe_count": 0})

    def gacha_step(self, banner) -> int:
        """The current 0-based step index for a step-up banner. SERVER state (`gacha_banner_types.md` §7):
        `UserGachaStepInfo` is `{gachaId, stepNum}` and the request carries no step, so this is ours."""
        if not banner.get("steps"):
            return 0
        return int(self.data.setdefault("gacha", {}).setdefault("step", {}).get(str(banner["id"]), 0))

    def roll_gacha(self, banner: dict, repeats: int = 1, ticket_id=None, offer: dict | None = None) -> dict | None:
        """Execute an AUTHORED banner `repeats` times, GRANT the drawn units, and record the result.

        The pool/rates/costs are authored (`gacha_authored.json`); this op owns the consequences:
          * the pool is EXPANDED from tiers to explicit rows (`expand_pool`);
          * the cost is charged per EXECUTION (`draws` units per execution -- a 10+1 is `draws=11`, one
            cost, NOT cost x11); a `ticket_group_id` banner spends the shipped `F_TICKET_MST` ticket the
            client named (`ticket_id`, from `GachaExeRequest.zJ1A6HXm`) or the rarest it holds;
          * `guarantee` forces at least one draw of a rarity (or one specific unit);
          * a step-up banner uses its CURRENT step's cost/draws/guarantee/pool and ADVANCES the step
            (wrapping after the last one).
        Returns None when the pool is empty or the cost is unaffordable; `frozen` refuses.
        """
        if self.mode == "frozen":
            return None
        steps = banner.get("steps") or []
        step_index = self.gacha_step(banner)
        step = steps[step_index] if steps else {}
        pool = expand_pool(banner)
        if not pool:
            return None
        repeats = max(1, int(repeats))
        cost = (offer or {}).get("cost") if offer else None
        if not cost:
            cost = step.get("cost", banner.get("cost")) or {}
        if isinstance(cost, list):      # the multi-entry costInfo: pick the OFFER this pull asked for
            cost = next((c for c in cost if (repeats > 1) == (int(c.get("draws", 1)) > 1)),
                        cost[0] if cost else {})
        # `repeats` is NOT a multiplier. The client derives it from the TAPPED ROW's `exeCount`
        # (`GachaConfirmScene::initialize` -> `GachaDetailMst::getExeCount()` -> `UserState.repeat` ->
        # `GachaExeRequest.2RtVPS3b`), i.e. it describes the transaction the row already encodes -- it is not
        # a count the player picks. So both the CHARGE and the UNIT COUNT come from the offer, never from
        # `repeats`: a 10+1 row (`exeCount=11`) is ONE charge of its price for 11 units, and the retail
        # 11-summons row (`exeCount=1` + `draws=11`) is one charge of 5000 for 11 units. Multiplying by
        # `repeats` charged 55000 / drew 121 for a 10+1. `docs/notes/gacha_multi_button.md` SS15.3.
        draws = int(cost.get("draws") or 0 if isinstance(cost, dict) else 0) \
            or int((offer or {}).get("draws") or 0) \
            or max(1, int(step.get("draws", banner.get("draws", 1))))
        guarantee = step.get("guarantee") or banner.get("guarantee") or {}
        # --- cost -------------------------------------------------------------------------------------
        kind = str(cost.get("kind", "free"))
        amount = int(cost.get("amount", 0) or 0)          # charged ONCE per transaction (see above)
        spent_ticket = None
        ticket_rar = None
        # THE PURCHASE METHOD IS RESOLVED FIRST. The client names the ticket it is paying with in
        # `GachaExeRequest.zJ1A6HXm` when the player taps the ticket button (0/absent for a lapis pull), and a
        # banner may legitimately carry BOTH a currency cost AND a ticket group -- 9100203 does (cost lapis 500,
        # `ticket_group_id` 10010, and the held ticket 12 IS a member of that group). Charging the currency
        # first REFUSED a pull the player had already paid for with a held ticket: measured 2026-09-23, banner
        # 9100203 + ticket 12 with 10 held -> lapis 500 > 340 -> "refused (cost/affordability)", no award, so
        # nothing ever accumulated. The `elif` below (no ticket named) stays for a ticket-ONLY banner.
        named = ticket_by_id(ticket_id) if ticket_id not in (None, "", 0, "0") else None
        if named is not None:
            group = banner.get("ticket_group_id")
            if group and named not in ticket_group_items(group):
                print(f"    gacha refused: ticket {ticket_id} does not belong to banner {banner.get('id')} "
                      f"group {group!r}", flush=True)
                return None
            item_type, item_id, _rar = named
            if not self.spend_item(item_type, item_id, 1):
                print(f"    gacha refused: ticket {ticket_id} (item {item_type}:{item_id}) is not held",
                      flush=True)
                return None
            spent_ticket = str(ticket_id)
            ticket_rar = _rar
        elif amount > 0:
            if kind not in COST_KINDS:
                print(f"    gacha refused: unknown cost kind {kind!r} (known: {sorted(COST_KINDS)})", flush=True)
                return None
            currency = cost_currency(kind)
            if currency is not None:
                if not self.spend_currency(currency, amount):
                    return None
            else:
                num = cost_numeric_kind(kind)
                item_id = cost.get("item_id")
                if num in ITEM_STORAGE_KINDS and item_id not in (None, "", 0):
                    if not self.spend_item(num, int(item_id), amount):
                        return None
                else:
                    print(f"    gacha refused: cost kind {kind!r} = numeric {num} has no state currency and no "
                          f"usable item_id={item_id!r} -- see docs/notes/currency_model.md", flush=True)
                    return None
        elif banner.get("ticket_group_id"):
            named = ticket_by_id(ticket_id) if ticket_id is not None else None
            rows = [named] if named else sorted(ticket_group_items(banner["ticket_group_id"]),
                                                key=lambda t: t[1], reverse=True)
            for item_type, item_id, _rarity in rows:
                if self.spend_item(item_type, item_id, 1):   # one ticket per transaction, not per repeat
                    spent_ticket = ticket_id
                    ticket_rar = _rarity
                    break
            else:
                return None
        # A TICKET'S OWN RARITY IS A FLOOR. `F_TICKET_MST.9fW0TePj` ("Rarity") is the base rarity the ticket
        # guarantees: group 10010 is a 3/4/5 ladder (items 160/170/180, colour BLUE/YELLOW/RED) and ticket 12 is
        # the 5-star EX ticket. Without this a 5-star ticket could award a 3-star base -- the one thing such a
        # ticket rules out (operator, 2026-09-23). The existing guarantee machinery below then forces at least
        # one draw of that rarity, which for a 1-draw ticket is exactly one.
        if spent_ticket and ticket_rar not in (None, "", "0"):
            want = int(ticket_rar)
            have = guarantee.get("rarity")
            if have is None or int(have) < want:
                guarantee = dict(guarantee, rarity=want)
        # --- draws ------------------------------------------------------------------------------------
        rates = [max(0.0, float(e.get("rate", 0))) for e in pool]
        total = sum(rates) or 1.0

        def _pick() -> dict:
            r = random.random() * total
            acc = 0.0
            for entry, w in zip(pool, rates):
                acc += w
                if r <= acc:
                    return entry
            return pool[-1]

        awards = [_pick() for _ in range(draws)]
        g_unit = guarantee.get("unit_id")
        g_rarity = guarantee.get("rarity")
        if g_unit is not None and not any(str(a["unit_id"]) == str(g_unit) for a in awards):
            awards[0] = {"unit_id": str(g_unit),
                         "rarity": (unit_mst(g_unit) or {}).get("9fW0TePj")}
        elif g_unit is None and g_rarity is not None and \
                not any(str(a.get("rarity")) == str(g_rarity) for a in awards):
            cand = [e for e in pool if str(e.get("rarity")) == str(g_rarity)] or pool
            idx = min(range(len(awards)), key=lambda i: int(awards[i].get("rarity") or 0))
            awards[idx] = random.choice(cand)
        granted = []
        for a in awards:
            unit = self.grant_unit(str(a["unit_id"]), granted_rarity=a.get("rarity"))
            granted.append({"unit_id": str(a["unit_id"]), "granted_rarity": a.get("rarity"),
                            "instance_id": (unit or {}).get("instance_id"),
                            "level": int((unit or {}).get("level") or 1)})
        # --- step advance + counters ------------------------------------------------------------------
        if steps:
            nxt = step_index + 1
            self.data["gacha"]["step"][str(banner["id"])] = 0 if nxt >= len(steps) else nxt
        st = self.banner_state(banner["id"])
        st["term_exe_count"] += repeats
        st["total_exe_count"] += repeats
        top = max((int(a["granted_rarity"] or 1) for a in granted), default=1)
        result = {"banner_id": str(banner["id"]), "detail_id": str(banner.get("detail_id", "")),
                  "step": step_index, "count": draws, "repeats": repeats, "ticket_id": spent_ticket,
                  "awards": granted, "pattern_id": effect_pattern_for(top, banner.get("id"))}
        self.data["gacha"]["last_pull"] = result
        return result

    def gacha_step_rows(self, banners: list) -> list:
        """`UserGachaStepInfoResponse` (7k0FbQ8B): `{gachaId, stepNum}` for every STEP-UP banner. The client
        clears the list on the first param of each reply, so one reply replaces all step state
        (gacha_banner_types.md §7)."""
        rows = []
        for b in banners or []:
            if not b.get("steps"):
                continue
            rows.append({"X1IuZnj2": str(b["id"]), "upv38FtD": str(self.gacha_step(b))})
        return rows

    def gacha_user_rows(self, banners: list) -> list:
        """`UserGachaInfoResponse` (80nGg32w): one row per authored banner = the player's view of it."""
        rows = []
        for b in banners or []:
            st = self.banner_state(b["id"])
            window = (b.get("schedule") or {}).get("window") or {}
            rows.append({"X1IuZnj2": str(b["id"]), "nqzG3b2v": str(b.get("detail_id", "")),
                         "Lqenh7d2": str(window.get("from", "")),
                         "u7dM3XwK": str(window.get("to", "")),
                         "08MuyfqD": str(st["term_exe_count"]),
                         "L4QmV0Nz": str(st["total_exe_count"])})
        return rows

    # --- derived views (schema §2.4) ---------------------------------------------------------------
    def nrg_rest(self) -> int:
        """The `5sEbN01y` countdown, from the regen anchor. The RATE is shipped (`RECOVER_TIME_ACTION`)."""
        anchor = self.data["rank"].get("nrg_regen_anchor_utc")
        try:
            started = time.mktime(time.strptime(anchor, "%Y-%m-%dT%H:%M:%SZ"))
        except Exception:  # noqa: BLE001
            return 0
        elapsed = max(0, int(time.time() - started))
        return max(0, REGEN_SECONDS - (elapsed % REGEN_SECONDS)) if REGEN_SECONDS else 0

    def orb_view(self, family: str, now: float | None = None) -> tuple:
        """One orb family's current `(point, max, restTime)` at projection time (APP-34).

        The state stores `point` + `anchor_utc` -- the client's `UserPointBase` receiveTime (+0x18, the
        moment the stored point was taken). Elapsed WHOLE ticks x the family's recovery amount regenerate
        up to the cap; `restTime` is the seconds to the NEXT tick, 0 at cap.

        A state with NO anchor has no recovery history: a fresh account, or a save written before the orb
        model existed (whose stale `{point:0, max:0, timer:0}` default is what served the Arena's 0/0). It
        reads as FULL -- orbs at cap, timer 0. That is deliberate: falling back to the stored point would
        turn a legacy 0 into "spent everything", which it never was.

        Projection only -- the settled point is NOT written back, so two reads a second apart agree and
        the anchor stays the anchor. A future spend/replenish op re-anchors (`point` + `_now_iso()`).
        """
        spec = ORB_FAMILIES[family]
        row = (self.data.get("orbs") or {}).get(family) or {}
        cap, period = int(spec["cap"]), max(1, int(spec["recover_time"]))
        try:
            point = max(0, int(row.get("point") or 0))
        except (TypeError, ValueError):
            point = 0
        started = _parse_utc(row.get("anchor_utc"))
        if started is None:
            return cap, cap, 0
        elapsed = max(0, int((time.time() if now is None else now) - started))
        point = min(cap, point + (elapsed // period) * int(spec["recover_amount"]))
        rest = 0 if point >= cap else period - (elapsed % period)
        return point, cap, rest

    def recharge_orb_with_shop(self, shop_id) -> dict | None:
        """A `ShopUseRequest` shopId -> fill one orb family to its cap for the SHIPPED lapis price.

        The WRITE half of the orb model (`orb_view` is the read half): any recovery the family has already
        banked is settled by the projection itself, and the bar is then set to the cap with a fresh anchor,
        so the settled point and the countdown stay consistent (a recharge fills the bar; the timer at cap
        is 0). The price is the row's `useDia` (see the module-level SHOP block -- row 21 = 100, shipped).

        A refusal changes NOTHING and says why: an unknown shop, a non-orb row, an item-cost row long
        (`useDia` 0), a frozen state, or a balance below the price. Loud on purpose -- `roll_gacha`'s
        over-spend rule: a silent refusal is indistinguishable from a successful one until a run.
        """
        row = shop_mst(shop_id)
        if row is None:
            print(f"    orb recharge refused: shop {shop_id!r} has no F_SHOP_MST row", flush=True)
            return None
        family = SHOP_ORB_FAMILY.get(str(shop_id))
        if family is None:
            print(f"    orb recharge refused: shop {shop_id!r} ({row.get(SHOP_NAME_TOKEN)!r}) is not an "
                  f"orb-restore row (known: {sorted(SHOP_ORB_FAMILY)})", flush=True)
            return None
        try:
            price = int(row.get(SHOP_USE_DIA_TOKEN, 0) or 0)
        except (TypeError, ValueError):
            price = 0
        if price <= 0:
            print(f"    orb recharge refused: shop {shop_id!r} is not a lapis purchase "
                  f"({SHOP_USE_DIA_TOKEN}={row.get(SHOP_USE_DIA_TOKEN)!r}, "
                  f"{SHOP_PARAM_TOKEN}={row.get(SHOP_PARAM_TOKEN)!r})", flush=True)
            return None
        if self.mode == "frozen":
            print(f"    orb recharge refused: state is frozen (shop {shop_id!r}, {price} lapis)", flush=True)
            return None
        held = int(self.currencies.get("lapis_free", 0) or 0) + int(self.currencies.get("lapis_paid", 0) or 0)
        if not self.spend_lapis(price):
            print(f"    orb recharge refused: shop {shop_id!r} costs {price} lapis > held {held}", flush=True)
            return None
        spec = ORB_FAMILIES[family]
        self.data.setdefault("orbs", {})[family] = {"point": int(spec["cap"]), "anchor_utc": _now_iso()}
        self.dirty = True
        print(f"    orb recharge: shop {shop_id} ({row.get(SHOP_NAME_TOKEN)}) -> {family} at cap "
              f"{spec['cap']}, spent {price} lapis", flush=True)
        return {"shop_id": str(shop_id), "family": family, "point": int(spec["cap"]), "price": price,
                "name": str(row.get(SHOP_NAME_TOKEN) or "")}

    def apply_shop_use(self, spec, reply_id=None, plaintext=None):
        """`SHOP_USE_REQUEST` (`ShopUseRequest`) -> the orb recharge, plus the reply that shows it.

        GATED on the reply id so it cannot leak onto another reply (`apply_carry_edit`'s precedent; when
        that step was ungated the golden replay caught it growing nine unrelated replies). The body's only
        meaningful field is the shopId in the `L5Nj3cKE` row; a body that cannot be parsed or names no shop
        is still ANSWERED -- never dropped -- because an empty body is exactly the connection error this
        feature exists to remove (`apply_emblem_setting`'s refusal lesson: a refusal re-sends the truth,
        and the truthful orb/lapis values re-render the screen the player already had).

        The reply deliberately carries ONE group, the team row, as a PLACEHOLDER: its tag `3oU9Ktb7` is a
        `USER_STATE_GROUPS` member, so the later `apply_to_reply` step replaces it (and adds the diamond
        row beside it) with the state-derived projection -- that projection is how the refilled
        `xDm19iGS` and the reduced lapis reach the client. MINIMAL is the point: this request carries no
        response class of its own, so the requirement is only that the reply is non-empty and that the
        groups the client's singleton applies carry the new state.
        """
        if str(reply_id) != SHOP_USE_REQUEST:
            return spec
        shop_id = None
        if plaintext:
            try:
                req = json.JSONDecoder().raw_decode(plaintext)[0]
            except Exception:  # noqa: BLE001 - a body that will not parse must not break the reply
                req = None
            rows = req.get("L5Nj3cKE") if isinstance(req, dict) else None
            if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                shop_id = rows[0].get(SHOP_ID_TOKEN)
        if shop_id is None:
            print("    orb recharge: request body named no shop (L5Nj3cKE/gdR2tJI9); no state change",
                  flush=True)
        else:
            self.recharge_orb_with_shop(shop_id)
        out = dict(spec) if isinstance(spec, dict) else {}
        out["3oU9Ktb7"] = [self._team_row()]   # placeholder -> apply_to_reply projects orbs + lapis
        return out

    def vision_card_rows(self) -> list:
        """`UserVisionCardInfoResponse` (`4hPYneL3`): the player's OWNED vision cards.

        WHY THIS EXISTS (APP-15): the HOME ornament vision card is resolved through the OWNED list, not the
        shipped MST alone. `VisionCardUtils::getHomeOrnamentVisionCardInfo` (`0x1a1eda4`) reads
        `UserTeamInfo+0xb8` — the `decorateHomeVcId` string `_team_row` already serves — and returns null
        unless `UserVisionCardInfoList::exist(<that key>)`; only then does
        `VisionCardUtils::getHomeOrnamentVisionCardExplainMst` (`0x1a1ee58`) look the explain row up, by the
        **int** at `UserVisionCardInfo+0x24`. With no row, `HomeScene` takes `setNoneOrnamentVisionCard` and
        the `♪` BGM title plate + note button are never built (`docs/notes/home_ornament_vc_bgm.md`).

        TAG CHOICE: the class has two tags in `harness/request_api_map.json`'s `carrier_rules` — `2Xi0wuGA`
        is factory flag 0 and **destructive** (`onReadStarted` `0x1943e30` = `removeAllObjects()`), while
        `4hPYneL3` is flag 1 and **not** destructive. So the row is added, never preceded by a clear.

        Token map (8 tokens, 3 required in practice): `gXb32xzQ` = the row KEY string
        (`UserVisionCardInfo+0x28`), `5giCMUd2` = the master VC id (`+0x24`), and **`7wV3QZ80` = Level** —
        see the comment in the loop for why a missing level is a crash, not a gap. Exp/TotalExp are optional
        and only emitted when the state row carries them.
        """
        rows = []
        for vc in self.data.get("vision_cards") or []:
            vc_id = str(vc.get("vc_id", "") or "").strip()
            if not vc_id:
                continue
            row = {"gXb32xzQ": str(vc.get("user_vc_id") or vc_id), VISION_CARD_KEY: vc_id}
            # LEVEL IS MANDATORY (2026-10-06, live first-fault capture). A row without it leaves the client's
            # `UserVisionCardInfo` level at 0, so `calcUserVcsStatus` -> `getVisionCardParamFromLv` ->
            # `VisionCardGrowMst::getGrowthRate(type, group, 0)` indexes the grow row at `level-1 = -1`, gets
            # no object, and libgame's NOT-FOUND path logs and then dereferences the null row anyway:
            # `f9400268 ldr x8,[x19]` at libgame `1c7110c` with `x19 = 0` -> SIGSEGV on the GL thread ->
            # libndk cannot process signal 11 -> the reporter aborts -> the coredump/ANR "closed out".
            row["7wV3QZ80"] = str(vc.get("level") or 1)
            for tok, field in (("B6H34Mea", "exp"), ("X9ABM7En", "total_exp")):
                if vc.get(field) not in (None, ""):
                    row[tok] = str(vc[field])
            rows.append(row)
        return rows

    # --- served reply groups (the projection) ------------------------------------------------------
    def groups(self) -> dict:
        """The player-state reply groups, keyed by the client's response tag."""
        out = {
            "B71MekS8": self._unit_rows(),     # UserUnitInfoResponse (FULL list -- clears first)
            "TJ9eL80N": self._equip_rows(),    # UnitEquipResponse (clears first)
            "5Eb0Rig6": self._deck_rows(),     # UserPartyDeckInfoResponse, party type 1
            "gP9TW2Bf": self._beast_rows(),        # UserBeastInfoResponse (FULL list -- clears first)
            "1S8P2u9f": self._beast_piece_rows(),  # UserBeastPieceInfoResponse (FULL list)
            "49rQB3fP": self._beast_deck_rows(),   # UserBeastDeckInfoResponse, deck-list type 1
            "8PEB5o7G": [{"r21y0YzS": str(self.data["party"]["active_deck"])}],  # UserActualInfo
            "3oU9Ktb7": [self._team_row()],    # UserTeamInfoResponse
            "J3pAG0I5": [{"T7sah9rc": str(self.currencies["lapis_free"]),
                          "cb3WmiD0": str(self.currencies["lapis_paid"])}],     # UserDiamondInfo
            "Md0N5abE": self.important_item_rows(),   # UserImportantItemInfoResponse (clears first)
            "0dNXQa3R": self.item_rows(),             # UserItemInfoResponse mode 0 (clears first): the bag
            "MBPCGT84": self.equip_item_rows(),       # UserEquipItemInfoResponse mode 0: OWNED equipment
        }
        # ONLY when the state actually owns vision cards. An empty list would still register the group in
        # every user-state reply, changing replies that have nothing to do with vision cards — the golden
        # replay (`harness/server_regression.py`) is the check that this stays true while the feature is
        # unused, which is what lets APP-15 land without perturbing a run.
        vcs = self.vision_card_rows()
        if vcs:
            out["4hPYneL3"] = vcs            # UserVisionCardInfoResponse, non-destructive mode
        return out

    def apply_to_reply(self, spec, reply_id=None):
        """Replace the player groups in a reply with their state-derived form.

        Fires when the reply is a user-state refresh (`LhVz6aD2` present) OR already carries one of the
        player groups -- i.e. exactly the replies that used to hold hand-written player literals. A reply
        with none is left byte-for-byte alone.

        `reply_id` is an action boundary, not just metadata. `UnitEquipRequest` (`pB3st6Tg`) updates
        `UserUnitEquipInfoList`; its broad projection must NOT carry `5Eb0Rig6`, because the client treats
        `UserPartyDeckInfoResponse::onReadStarted` as clear-and-rebuild for `UserPartyDeckList`. Injecting
        the old deck there destroys a pending party-editor replacement before the following
        `PartyDeckEditRequest` is serialized. The actual party edit and full login/user-state refreshes
        still receive the current `5Eb0Rig6` projection.
        """
        if not isinstance(spec, dict):
            return spec
        groups = self.groups()
        if "LhVz6aD2" not in spec and not any(tag in spec for tag in groups):
            return spec
        out = dict(spec)                      # never mutate the loaded payload (State.replies is shared)
        for tag, rows in groups.items():
            out[tag] = rows
        if str(reply_id) == "pB3st6Tg":
            out.pop("5Eb0Rig6", None)
        return out

    def _unit_rows(self) -> list:
        user_id = self.data["identity"]["user_id"]
        rows = []
        for u in self.data["roster"]:
            stats = unit_stats(u["master_id"], u["level"])
            row = {"og2GHy49": str(u["instance_id"]), UNIT_KEY: str(u["master_id"]),
                   TEAM_LEVEL_KEY: str(u["level"]), TEAM_NEED_EXP: str(u["exp"]),
                   "X9ABM7En": str(u["total_exp"])}
            for tok, val in zip(STAT_TOKENS, stats):
                row[tok] = f"{val}-0-0-0-0"
            row["a71oxzCH"] = str(u["lb_level"])
            row["EXf5G3Mk"] = str(u["lb_exp"])
            # storageState (UnitInfoBase+0x2a4): the warehouse tab filters on `isWareHouseUnit` =
            # `storageState == 1` (0x1dd35a0), so a unit moved in must come back marked or it vanishes
            # from the warehouse list at the next user-state refresh. SW-106.
            row["QHFU1k43"] = str(u.get("storage_state", 0))
            # AX arm: the first-get locator, sourced from the STATE (see _serve_unit_history). Must precede
            # `9Tbns0eI`, the last key that commits the object (schema §3.3).
            if _serve_unit_history() and u.get("first_get_unit_id") is not None:
                row["Z06cK4Qi"] = str(u["first_get_unit_id"])
            # NV EX level (token #21): served only under SERVE_NV_EX, from the state, and only when the unit
            # HAS one -- an absent token leaves the client's default (0), so a unit without EX keeps the exact
            # row it had before. Like every other key it must precede the commit tail.
            if _serve_nv_ex() and int(u.get("nv_ex_level") or 0) > 0:
                row["f8vk4JrD"] = str(u["nv_ex_level"])
            row["9Tbns0eI"] = str(user_id)          # the last key commits the object (schema §3.3)
            rows.append(row)
        return rows

    def _equip_rows(self) -> list:
        rows = []
        for u in self.data["roster"]:
            equip = u.get("equipment") or {}
            slots = "@".join(f"{slot}-{equip.get(str(slot), equip.get(slot, '0'))}"
                             for slot in range(1, 7))
            materia = u.get("materia") or []
            mat = "@".join(f"{i + 1}-{m}" for i, m in enumerate(materia))
            rows.append({"og2GHy49": str(u["instance_id"]),
                         "r21y0YzS": str(self.data["party"]["active_deck"]),
                         "93Y6evuT": slots, "80NUYFMJ": mat,
                         "nQ1iAGf3": u.get("vision_card", "")})
        return rows

    def _deck_rows(self) -> list:
        rows = []
        for deck in self.data["party"]["decks"]:
            members = ",".join(f"{m['member_type']}:{m['disp_order']}:{m['instance_id']}"
                               for m in deck["members"])
            shift = ",".join(f"{m['instance_id']}:0" for m in deck["members"])
            rows.append({"r21y0YzS": str(deck["id"]), "G4L0YIB2": deck["name"],
                         "2A6fYiEC": members, "w1JG7i9j": shift})
        return rows

    def _beast_rows(self) -> list:
        """`UserBeastInfoResponse` (FULL list, tag `gP9TW2Bf`): one row per owned esper.

        The 13 tokens and the stat formula are `beast_esper_surface.md` §2/§3: the six `Monster*` values are
        the `F_BEAST_STATUS_MST` min..max envelope at the level's growth percent, `X9ABM7En` the summed
        `F_BEAST_EXP_PATTERN_MST` cost, `0A1BkNWb` the summed `F_BEAST_CP_MST` add-CP.
        """
        rows = []
        for b in self.data.get("beasts") or []:
            row = beast_status(b.get("esper_id"), b.get("rarity")) or {}
            level = int(b.get("level", 1))
            out = {"Iwfx42Wo": str(b.get("esper_id")), BEAST_RARE: str(b.get("rarity", 3)),
                   GROWTH_LEVEL: str(level), "B6H34Mea": str(b.get("exp", 0)),
                   "X9ABM7En": str(beast_total_exp(row.get(BEAST_EXP_PATTERN, ""), level))}
            for tok, val in zip(BEAST_WIRE_STAT_TOKENS,
                                beast_stats(b.get("esper_id"), b.get("rarity"), level)):
                out[tok] = str(val)
            out["0A1BkNWb"] = str(b.get("cp", beast_cp(row.get(BEAST_CP_KEY, ""), level)))
            out["HXc84WT2"] = str(b.get("reset_count", 0))
            rows.append(out)
        return rows

    def _beast_piece_rows(self) -> list:
        """`UserBeastPieceInfoResponse` (tag `1S8P2u9f`) — the board bitmask per owned esper."""
        return [{"Iwfx42Wo": str(b.get("esper_id")),
                 "E8WRi1bg": str(b.get("board") or BEAST_BOARD_ALL),
                 "0bMAwC1t": str(b.get("board_unlocked", ""))}
                for b in self.data.get("beasts") or []]

    def _beast_deck_rows(self) -> list:
        """`UserBeastDeckInfoResponse` for deck-list type 1 (tag `49rQB3fP`): one row per party deck.

        `XZ4Kh7Ic` grammar = `<slot>:<esperId>,…`, all 5 slots, 0-based (beast_esper_surface.md §4).
        """
        decks = self.data.get("beast_decks") or {}
        rows = []
        for deck in self.data["party"]["decks"]:
            did = str(deck["id"])
            slots = list(decks.get(did) or [0] * BEAST_DECK_SLOTS)
            slots = [int(x or 0) for x in (slots + [0] * BEAST_DECK_SLOTS)[:BEAST_DECK_SLOTS]]
            rows.append({"r21y0YzS": did,
                         "XZ4Kh7Ic": ",".join(f"{i}:{slots[i]}" for i in range(BEAST_DECK_SLOTS))})
        return rows

    # --- the EMBLEM family (status HUD's `header_emblem_icon` -> PlayerEmblemSelectScene) --------------
    # Two independent facts, two independent fields, and conflating them is the trap:
    #   * `owned_emblems`   -> `UserPlayerEmblemInfoList`, the `3Ys8WPS6` group, token `0w6XzofV`.
    #     `PlayerEmblemSelectListObject::setLayout` (0x231accc) calls `hasEmblem(id)` on it once per
    #     `F_EMBLEM_ITEM_MST` row to decide the dimmed "Unacquired" icon -- so this is EARNED, and a fresh
    #     account owns nothing.
    #   * `player_emblem_id` -> `UserTeamInfo` `n69quiCJ`, the ONE displayed beside the name.
    # `0` is the "no emblem" sentinel in both: `EmblemMst::isNoneSettingEmblemData` (0x1c615e0) is
    # `ldr w8,[x0,#0x24]; cmp w8,#0`, and the shipped table's ids start at 10201, so no MST row is 0.
    EMBLEM_NONE_ID = 0

    def owned_emblem_ids(self) -> list:
        return [int(i) for i in (self.data.get("profile", {}).get("owned_emblems") or [])]

    def emblem_rows(self) -> list:
        """The `3Ys8WPS6` projection: one `UserPlayerEmblemInfo` per OWNED emblem.

        Emptiness is not a crash risk: `PlayerEmblemSelectScene::updateEmblemList` reads the LAYER's array
        and returns early when `count < 1` (0x231c994), so an owned list of zero draws an empty list.
        """
        return [{"0w6XzofV": str(i)} for i in self.owned_emblem_ids()]

    def grant_emblem(self, emblem_id) -> bool:
        """Add an emblem to the owned set, validating the id against the shipped table. True if added."""
        try:
            eid = int(emblem_id)
        except (TypeError, ValueError):
            return False
        if eid == self.EMBLEM_NONE_ID or emblem_mst(eid) is None:
            return False                      # 0 is the sentinel, never "owned"; a bad id is not an emblem
        owned = self.owned_emblem_ids()
        if eid in owned:
            return False
        self.data.setdefault("profile", {})["owned_emblems"] = owned + [eid]
        self.dirty = True
        return True

    def set_player_emblem(self, emblem_id) -> bool:
        """Record the DISPLAYED emblem. Refuses an id the player does not own, or an unknown one.

        Same discipline as `apply_party_edit`: only accept what we could have served, so a crafted request
        cannot put a crest on the header that no `F_EMBLEM_ITEM_MST` row backs (the header would then draw
        an atlas frame that does not exist).
        """
        try:
            eid = int(emblem_id)
        except (TypeError, ValueError):
            return False
        if eid != self.EMBLEM_NONE_ID and eid not in self.owned_emblem_ids():
            return False
        prof = self.data.setdefault("profile", {})
        if prof.get("player_emblem_id", 0) == eid:
            return True
        prof["player_emblem_id"] = eid
        self.dirty = True
        return True

    # --- the EMBLEM EVENT: the campaign the status-HUD crest opens (docs/notes/emblem_event.md) --------
    def emblem_event(self) -> dict:
        """The campaign block, initialised on first touch -- an authored state file carries no such key."""
        ev = self.data.setdefault("emblem_event", {})
        if not ev.get("campaign"):
            ev["campaign"] = emblem_campaign()
        ev.setdefault("milestones", {})
        ev.setdefault("mailed", [])
        return ev

    def emblem_tokens(self) -> int:
        """Crests earned in the live campaign: ONE PER CLEARED WEEKLY MILESTONE.

        The community schedule fixes the arithmetic: "Hero's Rewards" offers "up to 21 crests" and lists
        exactly 21 objectives, and the monthly table's reward rows are keyed by exactly those counts
        (0/1/5/7/8/9/10/11/12). A crest is therefore not a currency the client banks on its own -- it is
        the count of objectives met, which is why the state stores MILESTONES and derives the count.
        """
        return sum(1 for m in self.emblem_event()["milestones"].values() if m.get("cleared"))

    def emblem_objectives(self) -> list:
        """The live campaign's objectives, each merged with THIS account's progress. Table order."""
        ev = self.emblem_event()
        out = []
        for row in emblem_weekly_mst(ev["campaign"]):
            mid = str(row.get(EMBLEM_WEEKLY_MILESTONE, ""))
            st = ev["milestones"].get(mid) or {}
            out.append({"id": mid,
                        "type": str(row.get(EMBLEM_WEEKLY_TYPE, "")),
                        "target": str(row.get(EMBLEM_WEEKLY_TARGET, "")),
                        "count": str(row.get(EMBLEM_WEEKLY_COUNT, "")),
                        "progress": int(st.get("progress", 0) or 0),
                        "cleared": bool(st.get("cleared"))})
        return out

    def credit_emblem_objective(self, mission_id) -> list:
        """A CLEARED MISSION -> every objective that names it is met. Returns the milestones cleared.

        Objectives come in two shapes (`7f307af5`): type 2 names a MISSION to clear, type 1 names a
        banner/season event to accumulate points in. What the client tells us at a battle's end is the
        mission id and nothing else, so only mission-shaped targets can be honoured here; the point-shaped
        rows stay uncleared, and that limit is recorded in the note rather than papered over with a guess.
        Each newly cleared milestone grants ONE crest into the type-23 bag (`EMBLEM_CREST_ITEM`), so the
        count is visible in the item list as well as in this counter.
        """
        ev = self.emblem_event()
        mid = str(mission_id or "")
        if not mid or not self._mutate():
            return []
        cleared = []
        for row in self.emblem_objectives():
            if row["cleared"] or row["target"] != mid or row["type"] not in ("1", "2"):
                continue
            ev["milestones"][row["id"]] = {"progress": int(row["count"] or 1), "cleared": True}
            self.grant_item(EMBLEM_CREST_TYPE, EMBLEM_CREST_ITEM, 1)
            cleared.append(row["id"])
        if cleared:
            self.dirty = True
        return cleared

    def emblem_pending_tiers(self) -> list:
        """Earned-but-unpaid reward rows, GROUPED BY CREST COUNT (one tier = one mail).

        Grouped by `508f00a5` because several rows pay out at the same count (three rows pay Tier 1), and
        grouped so the delivery matches the community description of the feature ("reaching a new
        Adventurer Tier ... automatically sent to your mailbox"). `EMBLEM_PASS=1` adds the passholder
        column -- no shipped table records who owns a Crest Pass, so that is an authored lever -- and the
        crest-0 row is the campaign's opening gift, pending the moment the campaign is touched.
        """
        ev = self.emblem_event()
        paid = {str(x) for x in ev.get("mailed") or []}
        tokens = self.emblem_tokens()
        want_pass = (os.environ.get("EMBLEM_PASS") or "").strip() not in ("", "0")
        tiers: dict = {}
        for row in emblem_monthly_mst(ev["campaign"]):
            crests = str(row.get(EMBLEM_MONTHLY_CRESTS, ""))
            if not crests.isdigit() or int(crests) > tokens or crests in paid:
                continue
            slot = tiers.setdefault(crests, {"crests": crests, "free": [], "pass": []})
            if row.get(EMBLEM_MONTHLY_FREE):
                slot["free"].append(str(row[EMBLEM_MONTHLY_FREE]))
            if want_pass and row.get(EMBLEM_MONTHLY_PASS):
                slot["pass"].append(str(row[EMBLEM_MONTHLY_PASS]))
        return [tiers[k] for k in sorted(tiers, key=int)]

    def mark_emblem_tiers_mailed(self, crests) -> None:
        """Record tiers as paid. Idempotent, and the reason a retried mission end cannot double-mail."""
        paid = self.emblem_event()["mailed"]
        known = {str(x) for x in paid}
        for c in crests:
            if str(c) not in known:
                paid.append(str(c))
                known.add(str(c))
        self.dirty = True

    def _team_row(self) -> dict:
        rank = self.data["rank"]
        prof = self.data.get("profile", {})
        counts = self.data.get("counts", {})
        # unit / storage counts are DERIVED from the roster, never stored: grant_unit/sell_units append
        # and remove rows without touching `counts`, so a stored literal goes stale the first pull (and
        # the warehouse tab's count `NULFd7v3` is exactly the count the client shows on the unit screen).
        counts = {**counts,
                  "unit": len(self.data.get("roster") or []),
                  "storage": sum(1 for u in self.data.get("roster") or [] if u.get("storage_state"))}
        row = {
            "7wV3QZ80": str(rank["level"]), TEAM_NEED_EXP: str(rank["exp"]),
            "B6kyCQ9M": str(rank["nrg"]), "m0bD2zwU": str(nrg_cap(rank["level"])),
            "7UR4J2SE": str(self.currencies.get("gil", 0)),
            "0XUs3Tv6": str(self.currencies.get("friend_point", 0)),
            "i6qUd7bG": str(prof.get("serial_login_count", 0)),
            "n69quiCJ": str(prof.get("player_emblem_id", 0)),
            "Wk1v6upb": str(prof.get("message", "")),
            "gXb32xzQ": str(prof.get("decorate_home_vc_id", "")),
            "T1Lm7WdD": str(counts.get("unit", 0)),
            "NULFd7v3": str(counts.get("storage", 0)),
            "tsT4vP7w": str(counts.get("friend", 0)),
            "Xu7L1dNM": str(counts.get("equip_item", 0)),
            "4BI0WKjQ": str(counts.get("item", 0)),
            "92LoiU76": str(counts.get("materia", 0)),
            "9w3Pi6XJ": str(counts.get("material_item", 0)),
        }
        # The four ORB families (APP-34): point / max / restTime each, settled against that family's
        # recovery clock. The client maps these by token, so appending is safe; each family's three are
        # emitted together in readParam's order (point, max, restTime).
        for fam, spec in ORB_FAMILIES.items():
            point, cap, rest = self.orb_view(fam)
            row[spec["tokens"][0]] = str(point)
            row[spec["tokens"][1]] = str(cap)
            row[spec["tokens"][2]] = str(rest)
        # `5sEbN01y` (the NRG-regen countdown) is deliberately NOT sent yet: its handler calls
        # NotificationManager::setLocalNotificationFlg(true) (schema §3.2). Its RATE is shipped data
        # (`RECOVER_TIME_ACTION`); add the token with SW-49's countdown wiring.
        return row


# The NRG regen rate, in seconds per point: SHIPPED DATA -- `F_DEFINE_MST`'s RECOVER_TIME_ACTION = 300
# ("行動力回復時間"; decoded at /home/thetb/projects/ffbe/mst_decoded/F_DEFINE_MST.ndjson). The env knob
# stays for A/B runs, but the default is the table's value.
REGEN_SECONDS = int(os.environ.get("NRG_REGEN_SECONDS", str(RECOVER_TIME_ACTION)))

# THE one cost-kind table (docs/notes/currency_model.md). Keyed by an authored banner's semantic cost `kind`;
# each value is `(numeric_kind, state_currency_spent)`. `numeric_kind` is the client's `costInfo[0]` -- a
# `GameUtils::getItemNum` type (50 lapis / 51 friend point are instruction-confirmed; `friend_point` is exactly
# `GachaDetailMst::isFriendGacha`'s test). `None` = that kind is not a state currency spent here (an item/
# ticket cost is charged via `ticket_group_id`, not this table). Exvius point (52) is modelled as a KIND only;
# no shipped cost/reward entry spends it (currency_model.md, G2). ONE table, both call sites read it.
COST_KINDS = {
    "free":         (0,  None),
    "unit":         (10, None),
    "item":         (20, None),          # item cost -> inventory items (ticket banners use `ticket_group_id`)
    "equip":        (21, None),
    "materia":      (22, None),
    "important":    (23, None),          # important item (ticket) as a COST-ENTRY kind
    "vision_card":  (27, None),
    "gil":          (30, "gil"),
    "lapis":        (50, "lapis_free"),
    "friend_point": (51, "friend_point"),
    "exvius_point": (52, None),
}

# Storage/type kind -> the currency field it credits. Derived from `COST_KINDS` so the kind numbers have one
# home (`docs/notes/currency_model.md` §2: 50 lapis, 30 gil, 51 friend point). A mail reward uses these kinds.
STORAGE_CURRENCY = {kind: cur for kind, cur in COST_KINDS.values() if cur}


# The `getItemNum` types backed by `inventory.items`. A cost of one of these kinds names an item in
# `cost.item_id` and spends it (`20:<itemId>:<n>:<draws>` is the dominant shipped cost/reward kind). See
# docs/notes/currency_model.md.
ITEM_STORAGE_KINDS = frozenset({20, 21, 22, 23, 27})

# The regular-item bag's type. `UserItemInfo::getItemType()` is `mov w0,#0x14; ret` (0x1d8cae8) -- a
# compile-time constant, NOT a wire field -- so `UserItemInfoResponse` serves exactly the type-20 rows
# (`docs/notes/item_state_tokens.md` §1.1). Type 23 (important items / tickets) is `Md0N5abE`.
BAG_ITEM_TYPE = 20

# The bag kinds `_grant_items` can actually PROJECT: 20 is `BAG_ITEM_TYPE` (`0dNXQa3R`), 23 is the
# important-item group (`Md0N5abE`). Every OTHER kind is refused rather than banked, because a row the
# client cannot read is not a reward -- see `_grant_items`. 21 (equipment) and 10 (a unit, the roster) and
# the currency kinds are handled by their own branches there.
GRANT_BAG_PROJECTED = frozenset({"20", "23"})


def cost_numeric_kind(kind: str) -> int:
    """The client's numeric cost kind (`GachaDetailMst.costInfo[0]`). RAISES `KeyError` on an unknown kind."""
    return COST_KINDS[str(kind)][0]


def cost_currency(kind: str) -> str | None:
    """The state currency an authored cost `kind` spends, or `None` when that kind is not spent from state.
    RAISES `KeyError` on an unknown kind -- a lookup miss must be loud, never a silent default
    (`docs/analysis/RE_VERIFICATION_IDIOMS.md` #25)."""
    return COST_KINDS[str(kind)][1]


def _selftest() -> int:
    # --- the exp CURVE, pinned. `93fnYUJG` is a PER-LEVEL STEP, not a running total; reading it as a
    # threshold served ~4x the level (2026-09-26). These fixtures are the two independent proofs plus the
    # three levels the campaign save was actually wrong by, so the reading cannot regress silently.
    # REWRITTEN 2026-09-26. These fixtures used to assert the CUMULATIVE model -- "this exp yields this
    # level" -- which is the model a live run falsified: `getLv()` is a bare field load, so the client never
    # derives a level, and `getRestUnitExp = needExp(level+1) - exp` makes `exp` a residual. Asserting the
    # old model is what let SW-91 read as DONE, so the cases below pin the model's actual INVARIANTS
    # instead: the residual stays in range, a credit that cannot fill the step does not level, and one that
    # can does. The cumulative figures are kept as the counter-example, because "35709 is out of range for
    # level 20" is precisely the defect that reached a live client.
    _cases = [
        # (master_id, pattern, level, exp, in_range?, why)
        ("100000315", "30", 20, 35709, False,
         "the served value: needExp(21)=4415, so the client's getRestUnitExp was -31294"),
        ("100000315", "30", 20, 4414, True, "one below the step -- the largest in-range residual"),
        ("100000315", "30", 20, 4415, False, "exactly the step: that is level 21, not level 20"),
        ("100000315", "30", 20, 0, True, "a fresh level"),
        ("100000102", "10", 11, 871, True, "needExp(12)=872, so 871 is in range"),
    ]
    curve_fail = 0
    for mid, want_pat, lv, exp, in_range, why in _cases:
        step = need_unit_exp(mid, lv + 1)
        got_range = bool(step > 0 and 0 <= exp < step)
        got_pat = str((unit_mst(mid) or {}).get(EXP_PATTERN_KEY, ""))
        ok = (got_range == in_range) and got_pat == want_pat
        curve_fail += 0 if ok else 1
        print(f"curve {'ok  ' if ok else 'FAIL'} {mid} pat={got_pat} Lv{lv} exp={exp} "
              f"in_range={got_range} (want {in_range})  {why}")
    # The forward walk itself, on the real pattern -- the two directions that matter.
    for lv, ex, amt, want, why in ((20, 0, 84, (20, 84), "a battle's 84 cannot fill a 4415 step"),
                                   (20, 4400, 84, (21, 69), "84 over a 4415 step levels up exactly once"),
                                   (20, 0, 10 ** 6, None, "a huge credit walks many levels and stops")):
        got = credit_exp("100000315", lv, ex, amt)
        ok = want is None or got == want
        curve_fail += 0 if ok else 1
        print(f"walk  {'ok  ' if ok else 'FAIL'} Lv{lv} res{ex} +{amt} -> {got}"
              + (f" (want {want})" if want else "") + f"  {why}")
    for mid, want_pat, exp, want_lv, why in []:
        got_pat = str((unit_mst(mid) or {}).get(EXP_PATTERN_KEY, ""))
        got_lv = level_for_exp(mid, exp)
        ok = got_lv == want_lv and got_pat == want_pat
        curve_fail += 0 if ok else 1
        print(f"curve {'ok  ' if ok else 'FAIL'} {mid} pat={got_pat} exp={exp} -> lv {got_lv} "
              f"(want {want_lv})  {why}")
    # The digit-level proofs, straight from the MST. TWO figures, from TWO different patterns, both given
    # by the operator from community data and both independently confirmed here:
    #   pattern 25   sum(1..80)   = 1,422,072  -- the TOTAL to take a unit from 1 to 80
    #   pattern 20/30/99 step 79->80 = 35,661  -- and the value our capped units were left holding
    # (My first version of this test wrongly expected 35,661 on pattern 25, whose step is 44,576. The
    #  figures are per-pattern; the claim "it is a per-level step, not a running total" holds for all.)
    _rows = _load_ndjson("F_UNIT_EXP_PATTERN_MST.ndjson").get("__rows__", [])
    _pats: dict = {}
    for _r in _rows:
        _pats.setdefault(str(_r.get(EXP_PATTERN_KEY, "")), {})[int(_r[EXP_LEVEL])] = int(_r[EXP_NEED])
    for pid, want_sum, want_step, label in (("25", 1422072, None, "sum(1..80)"),
                                            ("20", None, 35661, "step 79->80"),
                                            ("30", None, 35661, "step 79->80"),
                                            ("99", None, 35661, "step 79->80")):
        _p = _pats.get(pid, {})
        got = exp_cumulative(_p, 80) if want_sum is not None else _p.get(80)
        ok = got == (want_sum if want_sum is not None else want_step)
        curve_fail += 0 if ok else 1
        print(f"curve {'ok  ' if ok else 'FAIL'} pattern {pid:>2} {label}={got} "
              f"(want {want_sum if want_sum is not None else want_step})")

    ps = PlayerState.load("/nonexistent", slot="selftest")
    print(f"mode={ps.mode} roster={[u['master_id'] for u in ps.data['roster']]}")
    print(f"nrg_cap(1)={nrg_cap(1)}  nrg_cap(50)={nrg_cap(50)}  need_exp(1)={need_exp(1)}")
    print(json.dumps(ps.groups(), indent=1))
    print("add_rank_exp(20) ->", ps.add_rank_exp(20))
    print("grant_unit(100000315, granted_rarity=8) ->",
          (ps.grant_unit("100000315", granted_rarity=8) or {}).get("master_id"))
    print("roster after grant:", [u["master_id"] for u in ps.data["roster"]])

    # Regression guard: an overlay is a test knob and must not persist (schema §1.3).
    rc = 0

    # --- the CATCH-UP counters (docs/notes/catchup_bonus_family.md) -----------------------------------
    # The invariants, not the numbers: one login per calendar day, a tier paid exactly once, a JUMP paying
    # every tier it crossed, a re-selected campaign starting a new calendar, and the one-shot welcome.
    # Each of these is a way the grant could double-pay or skip, and each must be able to FAIL.
    cu = PlayerState.load("/nonexistent", slot="selftest-catchup")
    tiers = [{"index": i, "days": d, "bit": i, "items": [{"kind": 30, "id": 100, "count": 100}]}
             for i, d in enumerate((2, 3, 5, 7, 14), start=1)]
    virgin = cu.advance_catchup("2026-01-01", "1033", 20)               # a brand-new account's first login
    first = cu.advance_catchup("2026-01-02", "1033", 20)
    again = cu.advance_catchup("2026-01-02", "1033", 20)               # the same date must not count twice
    paid1 = cu.pay_catchup_ladder(tiers)
    paid2 = cu.pay_catchup_ladder(tiers)                              # ...nor re-pay a tier
    cu.advance_catchup("2026-01-03", "1033", 20)
    jump = cu.advance_catchup("2026-01-09", "1033", 20)               # a 7-day calendar gap is still ONE login
    paid_jump = cu.pay_catchup_ladder(tiers)
    # The ladder's axis is LOGINS, not elapsed days, so a calendar gap advances it by one. A JUMP in the
    # count happens when a save is restored with a higher total (an operator's --set, an old slot), and then
    # every tier below the new total is owed at once -- so that is the case worth pinning.
    cu.data["catchup"]["total_login_days"] = 9
    paid_seeded = cu.pay_catchup_ladder(tiers)
    switched = cu.advance_catchup("2026-01-10", "1409", 28)           # a new campaign is a new calendar
    welcome1 = cu.grant_catchup_welcome({"gil": 1000, "lapis_free": 5, "not_a_currency": 9})
    welcome2 = cu.grant_catchup_welcome({"gil": 1000})
    cu.advance_catchup("2026-01-11", "1409", 28)
    cu_day = cu.pay_catchup_day("1409", 1, [{"kind": 20, "id": 555, "count": 2}])
    cur = cu.currencies
    bag = [i for i in cu.data["inventory"]["items"] if i.get("item_id") == 555]
    checks = [
        ("advances once per date", first["total_login_days"] == 2 and first["campaign_day"] == 2),
        ("same date does not advance", (not again["advanced"]) and again["total_login_days"] == 2),
        ("the FIRST login is day 1 of both", virgin["total_login_days"] == 1 and virgin["campaign_day"] == 1),
        ("tier 1 paid on total day 2", [t["days"] for t in paid1] == [2]),
        ("a paid tier is not re-paid", paid2 == []),
        ("a calendar gap is still one login", jump["total_login_days"] == 4 and
         [t["days"] for t in paid_jump] == [3]),
        ("a restored higher total pays every tier below it",
         [t["days"] for t in paid_seeded] == [5, 7] and cu.pay_catchup_ladder(tiers) == []),
        ("a new campaign restarts the day",
         switched["campaign_id"] == "1409" and switched["campaign_day"] == 1),
        ("welcome pays once", len(welcome1) == 2 and welcome2 == []),
        ("welcome drops an unknown currency", "not_a_currency" not in {w["currency"] for w in welcome1}),
        ("welcome credited the currency field", cur["gil"] >= 1000 and cur["lapis_free"] >= 5),
        ("a campaign day grants into the bag", len(cu_day) == 1 and bag and bag[0]["count"] == 2),
    ]
    for label, ok in checks:
        print(f"catchup {'ok  ' if ok else 'FAIL'} {label}")

    # --- the PROGRESS store: the invariants, not the numbers -------------------------------------------
    pr = PlayerState.load("/nonexistent", slot="selftest-progress")
    k = "spchallenge/1001/100101"
    fresh = pr.progress_value(k)              # BEFORE any bump -- reading it after is the bug this
    first = pr.bump_progress(k, 1, cap=1, what="equip a weapon")   # test just had (2026-09-26)
    capped = pr.bump_progress(k, 1, cap=1, what="equip a weapon")      # one-shot: the cap holds
    growing = pr.bump_progress("spchallenge/1001/15:999", 1)          # no cap -> it must grow
    unknown = pr.progress_value("never/seen")
    scope = pr.declare_scope("spchallenge/1001", period="week")
    scope2 = pr.declare_scope("spchallenge/1001", period="week")       # re-declaring must not re-arm
    frozen = PlayerState.load("/nonexistent", slot="selftest-frozen", mode="frozen")
    refused = frozen.bump_progress(k, 1, cap=1)
    pchecks = [
        ("a fresh store is empty", fresh == 0 and unknown == 0),
        ("one bump scores it", first == 1),
        ("a one-shot stays at its max", capped == 1),
        ("an uncapped counter grows", growing == 1 and pr.bump_progress("spchallenge/1001/15:999", 1) == 2),
        ("a declared scope is recorded once", bool(scope) and scope == scope2),
        ("frozen refuses", refused == 0 and not frozen.data.get("quest_progress", {}).get("counters")),
        ("every bump leaves an audit row", len(pr.data["quest_progress"]["audit"]) == 3),
    ]

    # --- the DAILY QUEST's day roll (docs/notes/daily_quests.md) --------------------------------------
    # Four ways the roll could go wrong, and each must be able to FAIL:
    #   * re-rolling within one day (the list would change under the player's finger, and a claim in flight
    #     would name a quest that is gone);
    #   * NOT rolling across a day boundary (the set would be frozen forever);
    #   * losing yesterday's progress when the day rolls -- a new period is a NEW KEY, never a wipe;
    #   * letting a claim pay twice, or pay for a quest that is not in today's set.
    dq = PlayerState.load("/nonexistent", slot="selftest-daily")
    d1, ids1, rolled1 = dq.roll_daily_quests("2026-01-15", ["6000101", "6000201"], ["gifter", "explorer"])
    _, ids_same_day, rolled2 = dq.roll_daily_quests("2026-01-15", ["6000301"], ["crafter"])  # ignored
    frozen_ok = rolled2 is False and ids_same_day == ["6000101", "6000201"]
    dq.bump_progress(dq.daily_progress_key("6000101", "2026-01-15"), 5, cap=5)
    dq.bump_progress(dq.daily_claim_key("6000101", "2026-01-15"), 1, cap=1)
    d2, ids2, rolled3 = dq.roll_daily_quests("2026-01-16", ["6000102"], ["gifter"])  # a new day: rolls
    dqchecks = [
        ("the first roll of a day happens", rolled1 and ids1 == ["6000101", "6000201"]),
        ("the set is FROZEN for the day", frozen_ok),
        ("a new day IS a new key, not a wipe",
         rolled3 and ids2 == ["6000102"] and dq.progress_value("dailyquest/2026-01-15/6000101") == 5
         and dq.progress_value("claimed/daily/2026-01-15/6000101") == 1
         and dq.progress_value("dailyquest/2026-01-16/6000102") == 0),
        ("yesterday's counter cannot be spent on today's quest",
         dq.progress_value(dq.daily_progress_key("6000102", "2026-01-16")) == 0),
        ("a claim is idempotent", dq.bump_progress(dq.daily_claim_key("6000101", "2026-01-15"), 1, cap=1) == 1),
        ("the reset count is auditable", dq.daily_quest_block()["resets"] == 2),
    ]
    for label, ok in dqchecks:
        print(f"daily-q {'ok  ' if ok else 'FAIL'} {label}")
        if not ok:
            rc = 1

    for label, ok in pchecks:
        print(f"progress {'ok  ' if ok else 'FAIL'} {label}")
        if not ok:
            rc = 1

        if not ok:
            rc = 1                        # accumulates: a later `ok` must not clear an earlier FAIL
    with tempfile.TemporaryDirectory() as td:
        f = os.path.join(td, "s.json")
        PlayerState.load(f, slot="t").save(f)                       # materialise the base (rank 1)
        t = PlayerState.load(f, overlay=["rank.level=80", "orbs.event.point=5"])
        applied = t.data["rank"]["level"] == 80 and t.data["orbs"]["event"]["point"] == 5
        t.save(f)                                                   # ordinary autosave
        disk = json.load(open(f))
        ok_auto = (disk["rank"]["level"] == 1
                   and disk["orbs"]["event"] == {"point": AUTHORED_ORB_CAP_EVENT, "anchor_utc": None}
                   and disk["meta"]["overlay"] == [])
        k = PlayerState.load(f, overlay=["rank.level=80"])
        k.save(f, force=True)                                       # --state-save keeps the injection
        disk2 = json.load(open(f))
        ok_keep = disk2["rank"]["level"] == 80 and disk2["meta"]["overlay"] == ["rank.level=80"]
        print(f"overlay: applied-in-memory={applied}  autosave-writes-base={ok_auto}  "
              f"state-save-keeps={ok_keep}")
        if not (applied and ok_auto and ok_keep):
            print("SELFTEST FAIL: overlay persistence contract broken")
            rc = 1

        # Coherence audit: the fresh state passes; a deck naming a non-roster instance fails loudly.
        # This is the guard that keeps UI-only selections from being mistaken for server corruption.
        fresh = PlayerState.load("/nonexistent", slot="selftest")
        clean = not state_coherence_issues(fresh.data)
        broken = copy.deepcopy(fresh.data)
        broken["party"]["decks"][0]["members"][0]["instance_id"] = "9999999999999"
        caught = any("non-roster instance" in issue for issue in state_coherence_issues(broken))
        fresh.grant_unit("100000315", granted_rarity=8, instance_id="1000000315")
        post_grant_clean = not state_coherence_issues(fresh.data)
        equip_reply = fresh.apply_to_reply({"LhVz6aD2": [{}]}, reply_id="pB3st6Tg")
        party_reply = fresh.apply_to_reply({"LhVz6aD2": [{}]}, reply_id="TS5Dx9aZ")
        action_projection = ("5Eb0Rig6" not in equip_reply and
                             "5Eb0Rig6" in party_reply)
        print(f"coherence: clean-state={clean} post-grant={post_grant_clean} "
              f"orphan-deck-member-detected={caught} action-projection={action_projection}")
        if not (clean and post_grant_clean and caught and action_projection):
            print("SELFTEST FAIL: player-state coherence audit broken")
            rc = 1

    # --- a mission's open-switch field is a COMMA LIST (2026-10-07). 75 of the 17690 shipped missions
    # name two switches and EVERY one of them names a type-101 member, so reading the field as one id made
    # all 75 grant nothing. The two on the campaign path are the fixtures: 1110404 (セイレーンの塔・最上階)
    # opens `10007400,82001100` — the first is the Esper screen's own gate — and 1110703
    # (グランシェルト城内・玉座前) opens two type-101 bits. 820 is deliberately NOT granted: this op tracks
    # one type, and a type-820 bit would need its container served, which is a separate decision.
    mstate = PlayerState.load("/nonexistent", slot="selftest")
    got_1110404 = mstate.clear_mission("1110404")
    got_1110703 = mstate.clear_mission("1110703")
    got_1110403 = mstate.clear_mission("1110403")     # single-id mission: must not regress
    got_1110303 = mstate.clear_mission("1110303")     # the Dalnakya -> Siren's Tower link
    got_noop = mstate.clear_mission("2110101")        # dIPkNn61 is empty: records the clear, grants nothing
    got_200only = mstate.clear_mission("1135307")     # the ONLY grant is type 200: a no-op, never a guess
    s101 = mstate.switches.get("101", set())
    switch_cases = [
        ("1110404 grants 10007400 (101,14) and skips the type-820 member",
         got_1110404 == "10007400" and 14 in s101 and "820" not in mstate.switches),
        ("1110703 grants BOTH members (101,26)+(101,55)",
         got_1110703 == "10010300,10010004" and 26 in s101 and 55 in s101),
        ("1110403 single id still granted (101,13)", got_1110403 == "10007300" and 13 in s101),
        ("1110303 grants the tower's gate 10006300 (101,9)", got_1110303 == "10006300" and 9 in s101),
        ("a mission opening nothing is None, but its clear is recorded",
         got_noop is None and "2110101" in mstate.data["progress"]["cleared_missions"]),
        ("a type-200-only grant is a no-op (no bit, no guess)", got_200only is None and "200" not in mstate.switches),
    ]
    switch_fail = 0
    for why, ok in switch_cases:
        switch_fail += 0 if ok else 1
        print(f"switches {'ok  ' if ok else 'FAIL'} {why}")
    if switch_fail:
        print(f"SELFTEST FAIL: mission open-switch grant broken ({switch_fail} case(s))")
        rc = 1

    # --- the ESPER a story beat grants, derived from the beat's own switch list (`820<EE>100`). The count is
    # asserted because it is the claim: 19 shipped missions carry one, and the semantic agreement across all
    # 19 (location name vs esper name) is what makes this a rule. The two failures are the interesting ones:
    # a beat that grants no esper must answer None rather than guess.
    esper_cases = [
        ("1110404 -> 1 (セイレーンの塔・最上階 -> セイレーン)", "1110404", 1),
        ("1115005 -> 2 (炎獣の座 -> イフリート)", "1115005", 2),
        ("1125204 -> 3 (氷魔の寝所 -> シヴァ)", "1125204", 3),
        ("1125105 -> 6 (守護神 -> ゴーレム)", "1125105", 6),
        ("11315101 -> 12 (水神の座 -> リヴァイアサン)", "11315101", 12),
        ("1110403 must be None -- a tower floor that grants no esper", "1110403", None),
        ("1110303 must be None -- the Dalnakya boss", "1110303", None),
        ("1110100 must be None -- the tutorial battle", "1110100", None),
    ]
    esper_fail = 0
    for why, mid, want in esper_cases:
        got = story_beast_for_mission(mid)
        ok = got == want
        esper_fail += 0 if ok else 1
        print(f"esper {'ok  ' if ok else 'FAIL'} {why} (got {got})")
    total = 0
    for _l in _load_ndjson("F_MISSION_MST.ndjson", MISSION_KEY).values():
        _sw = str(_l.get(MISSION_OPEN_SWITCH) or "")
        for _raw in _sw.split(","):
            _sid = _raw.strip()
            if len(_sid) == 8 and _sid.startswith("820") and _sid.endswith("100") and _sid[3:5].isdigit() \
                    and beast_status(str(int(_sid[3:5])), 1) is not None:
                total += 1
                break
    print(f"esper ok   the corpus still carries exactly 19 esper beats (got {total})")
    if total != 19:
        esper_fail += 1
    if esper_fail:
        print(f"SELFTEST FAIL: story-esper derivation broken ({esper_fail} case(s))")
        rc = 1

    # --- ungranting: the inverse op must leave no deck slot naming an esper the account no longer owns.
    bstate = PlayerState.load("/nonexistent", slot="selftest")
    bstate.grant_beast(1, rarity=3, level=60, board=BEAST_BOARD_ALL, link_deck=0)   # digest row, as in §3
    owned_before = bstate.owns_beast(1)
    linked_before = 1 in (bstate.data["beast_decks"].get("0") or [])
    removed = bstate.remove_beast(1)
    clean_after = not bstate.owns_beast(1) and 1 not in (bstate.data["beast_decks"].get("0") or [])
    no_orphan = not [i for i in state_coherence_issues(bstate.data) if "beast" in i.lower()]
    print(f"ungrant: granted-and-linked={owned_before and linked_before} removed={removed} "
          f"clean={clean_after} no-orphan-deck-slot={no_orphan}")
    if not (owned_before and linked_before and removed and clean_after and no_orphan):
        print("SELFTEST FAIL: ungrant_beast left the state incoherent")
        rc = 1
    if curve_fail:
        # Wired into the exit code on purpose: the curve checks PRINTED `FAIL` but did not reach `rc`
        # in the first revision, which is precisely the "a gate that cannot fail" defect this workspace
        # has been bitten by twice. A printed FAIL nobody can fail on is not a check.
        print(f"SELFTEST FAIL: exp-curve audit broken ({curve_fail} case(s))")
        rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(_selftest())
