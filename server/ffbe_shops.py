#!/usr/bin/env python3
"""ffbe_shops.py — the two shop requests that are not a gacha banner.

**Magic Key exchange** (`ExchangeShopRequest`, `I7fmVX3R`): a plain lapis purchase from the shipped
`F_EXCHANGE_SHOP_ITEM_MST` five-row table. The client has no response class for the request; it
re-reads its own UserInfo (lapis + item bag) from the reply, so the answer is the ordinary user-state
projection after the state has been changed. That is the small, fully validated port from the
collaborator's 26101817 build.

**Moogle/Star Quartz medal exchange** (`MedalExchangeRequest`, `LiM9Had2`): the WW 9.0.0 sender
(`AbstractStoreScene::setRequestForTradeStore`) sends the store id (`Z20mNDvL`) and
`<already-purchased count>:<reward item id>` entries (`L2gHC5ta`); the JP 11/Lapis
`<exchange id>:<count>` shape is accepted as a compatibility path. The catalogue is the shipped
`F_MEDAL_EXCHANGE_MST` and the store's goods are `F_STORE_ITEM_MST` type 50 (Moogle) / 40 (Star Quartz).
The 4th-anniversary Moogle King is event store `7000122` in town 9001; its six base-store aliases are
resolved from the shipped store rows, and `VariableStoreCheckRequest` points the base NPC at it.

AUTHORED, and deliberately so:
  * the schedule window (2015..2099) — the event is long over, but the store must be reachable;
  * the medal source is the shipped `F_SP_CHALLENGE_MST` group **99062** (the 4th-anniversary daily
    objectives, each `20:1209001258:20`); this module does not mint it. One objective is already scored;
    the other six need progress hooks. Seed with `--grant-item 1209001258 --grant-count <n>` for a shop-only test;
  * the 999 stack cap and the refusal logging, matching the collaborator's exchange-shop rule.

The master ids/tokens are from `F_EXCHANGE_SHOP_ITEM_MST`, `F_MEDAL_EXCHANGE_MST`, `F_STORE_ITEM_MST`,
`F_TOWN_STORE_MST` and `F_TOWN_STORE_COMMENT_MST`; the request body was confirmed by disassembly at
`0x198bc3c` (ExchangeShop) and `0x1994638` (MedalExchange).
"""
from __future__ import annotations

import csv
import json
import logging
import os
from functools import lru_cache

import ffbe_state as st
from player_state import BAG_ITEM_TYPE, _load_ndjson, item_mst, unit_mst, vision_card_mst

log = logging.getLogger(__name__)

# --- request tokens (WW 9.0.0) ---------------------------------------------------------------
EXCHANGE_SHOP_REQUEST = "I7fmVX3R"
EXCHANGE_SHOP_GROUP = "V5W9ZAPx"
EXCHANGE_SHOP_PRODUCT = "wpM08VZ7"

MEDAL_EXCHANGE_REQUEST = "LiM9Had2"
MEDAL_EXCHANGE_GROUP = "2d6RAkvh"
MEDAL_EXCHANGE_STORE = "Z20mNDvL"
MEDAL_EXCHANGE_ENTRIES = "L2gHC5ta"

VARIABLE_STORE_REQUEST = "i0woEP4B"
VARIABLE_STORE_TAG = "3VS1Bhmf"

# --- the gacha-select exchange family (APP-63, 2026-11-08) ------------------------------------
# Tapping a banner on "Exchanges Available" sends GachaSelectEntryRequest with ONE own group, the SHOP TYPE
# (`hyKs6Sn9.JV8AIH3n`; disassembly 0x19a356c reads it from the request's +0x30 and adds it to that group).
# The class has no response object of its own -- the client parses the reply's groups straight into its
# GachaSelectDetailMstList / GachaSelectProductMstList / UserGachaSelectProductInfo singletons, which is why
# the reply is built here as plain groups:
#   F2rNo6qf -> GachaSelectDetailMstResponse    (one row per SHOP)
#   TPrh6Ay3 -> GachaSelectProductMstResponse   (one row per PRODUCT; its 3X1gLufw must equal the detail id)
#   qS62ewjH -> UserGachaSelectProductInfoResponse (the player's purchased counts; key = product id)
# The shape and every column's meaning are in `harness/response_field_map.json` (the three classes are 100%
# known, hand-annotated), and the token constants below are those classes' own.
GACHA_SELECT_ENTRY_REQUEST = "6nTcSp4R"
GACHA_SELECT_ENTRY_GROUP = "hyKs6Sn9"
GACHA_SELECT_SHOP_TYPE = "JV8AIH3n"
GACHA_SELECT_EXCHANGE_REQUEST = "W58vo4Hp"
GACHA_SELECT_EXCHANGE_GROUP = "4mrD6JuA"
GS_DETAILS, GS_PRODUCTS, GS_COUNTS = "F2rNo6qf", "TPrh6Ay3", "qS62ewjH"
GS_SELL_COUNTS = "4owH1uT2"
GS_PRODUCT, GS_QUANTITY = "jPV7Xc4N", "Qy5EvcK1"
# Column tokens of the three response classes (`harness/response_field_map.json`).
GS_DETAIL_ID, GS_DETAIL_GACHA, GS_DETAIL_TYPE = "3X1gLufw", "X1IuZnj2", "JV8AIH3n"
GS_DETAIL_ORDER, GS_DETAIL_BANNER, GS_DETAIL_LIMIT = "Z0EN6jSh", "fSxqkF10", "uYcL8IQ1"
GS_DETAIL_TEXT, GS_DETAIL_START, GS_DETAIL_END, GS_DETAIL_COST = "6uvHX2JG", "VjJQ51uG", "m8ivD4NX", "0GgK6ytT"
# THE BANNER SCHEDULE (APP-77). `ShopExchangeItemListScene::setStoreBannerList` keeps a type-4 banner only when
# `BannerScheduleMst::withinPeriod(banner id, now)` is true, and that singleton is filled by the reply group
# `jBL5XNf6` (`BannerScheduleMstResponse`, tokens below). Nothing else in the client writes it, and we have never
# served the group -- which is why every shop page drew with no banner. The dispatcher is the global
# `GameResponseFactory::createResponseData`, so the rows can ride ANY reply the client parses; we put them on the
# shop entry reply itself, which is the last reply before the banners are built.
GS_SCHEDULE = "jBL5XNf6"
GS_SCHEDULE_ID, GS_SCHEDULE_START, GS_SCHEDULE_END = "fSxqkF10", "VjJQ51uG", "m8ivD4NX"
GS_DETAIL_LIMIT_ROW = GS_DETAIL_LIMIT
GS_PRODUCT_DETAIL, GS_PRODUCT_REWARD = "3X1gLufw", "P4puB2ch"
GS_PRODUCT_PRICE, GS_PRODUCT_LIMIT = "e4mG8jTc", "uYcL8IQ1"
GS_PRODUCT_ORDER, GS_PRODUCT_BONUS = "Z0EN6jSh", "fbcj5Aq9"
GS_PRODUCT_TEXT, GS_PRODUCT_START, GS_PRODUCT_END, GS_PRODUCT_INERT = "6uvHX2JG", "VjJQ51uG", "m8ivD4NX", "JasF1S3u"
GS_COUNT_PRODUCT, GS_COUNT_DETAIL, GS_COUNT_ALL = "jPV7Xc4N", "3X1gLufw", "Q8GLK1pz"
# The exchange family's own shop type. The client asked with this value on the 2026-11-08 run; every one of
# our ten exchange banners is that one family, so the reply answers with the type the request names.
DEFAULT_SHOP_TYPE = "5"

# --- the Clash of Wills (TFA) exchange family (APP-72, 2026-11-08) --------------------------------
# The client reaches THIS shop with the same `GachaSelectEntryRequest`, but shop type **4** instead of the
# Moogle family's 5 (captured live: `{"hyKs6Sn9":[{"JV8AIH3n":"4"}]}` twice on 2026-11-08). Answering a
# type-4 tap with the type-5 catalogue is what drew ten unrelated store pages -- the operator's "it's pulling
# random store listings". `gacha_select_catalogue` therefore DISPATCHES on the type.
#
# THE PAGES ARE SHIPPED. `F_BANNER_MST` type 4 (`w2A76noc`) holds the ten banners, and their art is in the
# served CPK (`en/sd/banner/Ver328_banner1/TFA_*_Exchange.png`, 40 files shipped, 10 referenced by our table).
# Their display names are the banner's own `G4L0YIB2`; the three where `F_TEXT_SHOP` also has a row agree.
#
# THE GOODS ARE NOT SHIPPED, and that is the honest gap: the reader is
# `ShopExchangeItemListScene_sg::getStoreItemList`, which walks `PrizeTranslatorPointMst` (+0x24 exchange id,
# +0x44 price, +0x48 limit) -- a table in NEITHER our 220 decoded WW tables NOR the JP 11.0.0 corpus, and
# `F_MEDAL_EXCHANGE_MST` / `F_EXCHANGE_SHOP_ITEM_MST` hold no Xenostone/Obnixus row. So `tfa_exchange.tsv` is
# **AUTHORED FROM THE COMMUNITY WIKI** (`Category:Clash_of_Wills` §Exchange, fetched 2026-11-08); each row
# keeps the wiki's price and limit, and its reward id was resolved against the shipped tables (exact / fuzzy /
# unresolved in the file). A row without a resolved id is DROPPED here and counted in the log -- never invented.
#
# Page -> banner id is by SLOT, the one inference in this block: the banner ART names the slot
# (`TFA_Helm_Exchange.png`), the wiki page is the same slot ("Celestite Helm of Will"), and the table's own
# names agree wherever both exist. Seasonal pages are the ones whose currency is an Obnixus Drop.
TFA_ENTRY_SHOP_TYPE = "4"
TFA_HYDRO_PAGE = "2900070"     # ours; see the map below
TFA_PAGE_BY_SLOT = {
    "Celestite Helm of Will": "61",
    "Celestite Garb of Will": "2000000",
    "Celestite Circlet of Will": "2000001",
    "Celestite Ring of Will": "2000002",
    "Celestite Vest of Will": "2000003",
    "Celestite Breastplate of Will": "2000004",
    "High Celestite Exchange": "2000005",
    "Obnixus Drop (Snow) Exchange Shop": "2900000",
    "Obnixus Drop (Volt) Exchange Shop": "2900001",
    # THE AUTHORED VINTAGE (APP-73): the operator's archive video's season, S7 Hydro. Its page is OURS
    # because the shipped `F_BANNER_MST` kept only the two last seasonal rows; `2900070` is an id we chose
    # (`29` + `007` for the season + a `0`), NOT a shipped one and NOT extrapolated from those two. The art is
    # the game's own (`TFA_ObnixusDrop7_Exchange.png`, shipped), and the matching banner row is written by
    # `harness/patch_tfa_banner.py` so the CLIENT can resolve it; this constant is what the catalogue reads,
    # because `mst_decoded/` is read-only shipped data and must not gain our row.
    "Obnixus Drop (Hydro) Exchange Shop": TFA_HYDRO_PAGE,
}
# `2910000` ("Xenostone Shard Equippable Ability Pop-Up Exchange") is shipped as a PAGE but the wiki has no
# row list for it, so it is served as an empty page rather than given invented prices.
TFA_EMPTY_PAGES = {"2910000"}
TFA_TSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tfa_exchange.tsv")

# The PERMANENT exchange shops the client's "Exchanges Available" list must draw (SW-130). A store appears
# there ONLY when `VariableStoreScheduleInfoList::getCurrentStoreInfo` resolves it, so each is self-mapped by
# `serve_variable_store`; see that function's docstring for the evidence.
EXCHANGE_STORES = (
    # The vision-card pair (SW-130's own feature).
    ("7000574", "Ticket Exchange (Perma-VC Select II)"),
    ("7000216", "Ticket Exchange (Perma-VC Select)"),
    # The other permanent exchanges the shipped F_GACHA_STORE_MST type-2 rows name (2026-11-08). Each is its
    # own shop with its own currency; the currencies are mailed by `exchange_test.json` while testing.
    ("7000414", "Intrinsic Ability Ticket Exchange"),
    ("7000434", "Mystery Crystal Shop"),
    ("7000174", "NV Beginner's Select"),
    ("7000089", "Beginner's 5-star Select"),
    ("7000458", "Star Quartz Exchange (3-star Units)"),
    ("7000457", "Star Quartz Exchange (4-star Units)"),
    ("7000456", "Star Quartz Exchange (Time-Limited)"),
    ("7000459", "Log Quartz Exchange"),
)

# The 4th-anniversary Moogle King.  The store and its currency are shipped rows; the choice to open it
# is ours (the event period ended in 2020).
EVENT_STORE = "7000122"
EVENT_CURRENCY = "1209001258"
SCHEDULE_START = "2015-01-01 00:00:00"
SCHEDULE_END = "2099-12-31 23:59:59"
STACK_MAX = 999

# Reward kinds this module can project.  `20` is the item bag, `23` the important-item list, `10` the
# roster, `21` owned equipment and `27` the VISION CARD list (SW-130 slice C).  Kind `22`/`60`/`50` have no
# served reader here and are refused rather than banked invisibly (the `_grant_items` rule).
_KINDS = {"10", "20", "21", "23", "27", "60"}
# KIND 60 IS A RECIPE (APP-76). `GachaPanelItemObj::drawIcon` `0x2173728` compares the item's kind to 0x3c and,
# only on a match, calls `GameUtils::getRecipeIconBgFileName(id)` -- which looks the id up in
# `RecipeBookMstList`, takes `RecipeBookMst::getCraftItemMst()` and composites "recipe_<category>.png" with the
# crafted gear's cutout. A recipe served as kind 20 takes the plain-item branch instead: no lookup, no art.
# F_MEDAL_EXCHANGE_MST prices its own recipe rewards exactly this way (`60:1000000022:1:100`).

# `F_EXCHANGE_SHOP_ITEM_MST` columns.
EI_ID, EI_DIAMOND, EI_TYPE, EI_TARGET, EI_COUNT = "wpM08VZ7", "8f7mxN3j", "4ubeU0nI", "6uIYE15X", "Qy5EvcK1"
# `F_MEDAL_EXCHANGE_MST` columns.
ME_ID, ME_CURRENCY, ME_PRICE, ME_REWARD, ME_LIMIT = "S21FT3L8", "ak2dhKm3", "Qy5EvcK1", "7iJpH5zZ", "x0NDnEC9"
# `F_STORE_ITEM_MST` / `F_TOWN_STORE_MST` columns.
SI_STORE, SI_TYPE, SI_ID = "Z20mNDvL", "YWF08vZS", "6uIYE15X"
TS_ID, TS_NAME, TS_TOWN, TS_TYPE, TS_OWNER = "Z20mNDvL", "G4L0YIB2", "Gh92V3Tx", "kaG4xf3I", "mBVN12cS"
COMMENT_STORE, COMMENT_KIND, COMMENT_TEXT = "Z20mNDvL", "5uDYV3nK", "85DIQ1nK"


def _rows(name: str) -> list[dict]:
    """One shipped ndjson table, in file order.  `_load_ndjson` caches the file."""
    return _load_ndjson(name).get("__rows__") or []


@lru_cache(maxsize=1)
def _exchange_items() -> dict[str, dict]:
    return {str(r.get(EI_ID)): r for r in _rows("F_EXCHANGE_SHOP_ITEM_MST.ndjson")}


@lru_cache(maxsize=1)
def _medal_rows() -> dict[str, dict]:
    return {str(r.get(ME_ID)): r for r in _rows("F_MEDAL_EXCHANGE_MST.ndjson")}


@lru_cache(maxsize=1)
def _store_rows() -> dict[str, dict]:
    return {str(r.get(TS_ID)): r for r in _rows("F_TOWN_STORE_MST.ndjson")}


@lru_cache(maxsize=1)
def _comment_rows() -> dict[tuple[str, str], str]:
    return {(str(r.get(COMMENT_STORE)), str(r.get(COMMENT_KIND))): str(r.get(COMMENT_TEXT) or "")
            for r in _rows("F_TOWN_STORE_COMMENT_MST.ndjson")}


@lru_cache(maxsize=1)
def _store_goods() -> dict[str, list[tuple[str, str]]]:
    """store id -> [(goods type, exchange id)] for every store row whose exchange id is a MEDAL ROW.

    THE TEST IS MEMBERSHIP IN `F_MEDAL_EXCHANGE_MST`, NOT THE GOODS TYPE (SW-130 slice C, 2026-11-06). The
    old filter admitted types 40/50 only, which is what the 4th-anniversary store happens to use — but the
    Perma-VC store (7000574) lists its 33 cards as types 50 (19) + 20 (14), and the Master Card store
    (7000434) as type 20, so a type filter hid 14 of 33 cards and the whole Master Card row. A goods row
    whose id has no exchange row is not a medal good at all, so the join is the correct test and the type
    carries no meaning for the resolver.
    """
    medal = _medal_rows()
    out: dict[str, list[tuple[str, str]]] = {}
    for r in _rows("F_STORE_ITEM_MST.ndjson"):
        eid = str(r.get(SI_ID))
        if eid in medal:
            out.setdefault(str(r.get(SI_STORE)), []).append((str(r.get(SI_TYPE)), eid))
    return out


@lru_cache(maxsize=1)
def _typed_goods() -> dict[str, list[tuple[str, str]]]:
    """store id -> [(goods type, exchange id)] for the Moogle (50) / Star Quartz (40) rows ONLY.

    This is the ORIGINAL filter, kept for `_has_goods` alone. It is a property of the base-NPC-stub test, not
    of the medal resolver: SW-130 slice C's membership test lives in `_store_goods`. Keeping the two apart is
    what stops a resolver fix from silently re-routing five normal shops' banners.
    """
    out: dict[str, list[tuple[str, str]]] = {}
    for r in _rows("F_STORE_ITEM_MST.ndjson"):
        kind = str(r.get(SI_TYPE))
        if kind in ("40", "50"):
            out.setdefault(str(r.get(SI_STORE)), []).append((kind, str(r.get(SI_ID))))
    return out


def _has_goods(store_id: str) -> bool:
    """Does this store have ANY `F_STORE_ITEM_MST` row? The `_event_aliases` predicate.

    CORRECTED 2026-11-07 (SW-130). It used to mean "has a Moogle (50) / Star Quartz (40) row", which is what
    made the client's "Exchanges Available" list draw two STORES THAT ARE NOT THE EVENT STORE: `7000414`
    (Intrinsic Ability Ticket Exchange) and `7000434` (Mystery Crystal Shop) have only type 22 / 10+20 rows,
    so the old predicate called them "empty base NPCs" and aliased them to `7000122` — the list drew their
    banners and tapping one opened the ANNIVERSARY shop. A store with any row at all is a real shop and must
    not be aliased; the base Moogle NPC stubs have none.
    """
    return any(str(r.get(SI_STORE)) == str(store_id) for r in _rows("F_STORE_ITEM_MST.ndjson"))


@lru_cache(maxsize=1)
def _store_targets() -> dict[str, dict[str, list[str]]]:
    """store id -> {target item id: [exchange ids]}.

    WW 9.0.0's `AbstractStoreScene::setRequestForTradeStore` does NOT send the exchange id: it sends
    `<already-purchased count>:<getItemId()>`, and `StoreExchangeItem::getItemId` is the REWARD item
    (`+0x48`), while `getExchangeId` is the separate `+0x5c` used only to ask
    `UserMedalExchangeInfo::getExchangeCnt`.  A target can have several historical exchange rows in one
    store, so this index keeps the shipped order and the resolver prefers the row whose stored count
    matches the request's first token.
    """
    out: dict[str, dict[str, list[str]]] = {}
    rows = _medal_rows()
    for store, goods in _store_goods().items():
        for _kind, eid in goods:
            row = rows.get(str(eid)) or {}
            target = str(row.get(ME_REWARD) or "").split(":")
            if len(target) < 2 or not target[1].isdigit():
                continue
            out.setdefault(store, {}).setdefault(target[1], []).append(str(eid))
    return out


def _resolve_entry(store: str, left: int, right: int, history: dict[str, int]):
    """WW entry -> (exchange id, how many times); None when it names no sold exchange.

    Two wire shapes are accepted:
      * the WW 9.0.0 sender: `<already-purchased count>:<reward item id>`, one exchange per entry;
      * the JP 11/Lapis shape `<exchange id>:<count>`, kept as a compatibility path because the
        collaborator's handler documents it and a future capture may show it.
    The shapes cannot be confused by a shipped id: a real history count is small, while a real
    exchange id (21800+) is not.
    """
    rows, goods = _medal_rows(), {eid for _kind, eid in _store_goods().get(store, [])}
    # JP/Lapis shape: a shipped exchange id on the left, a positive count on the right.
    if str(left) in rows and str(left) in goods:
        return str(left), right
    # A direct exchange-id form (history:exchange) -- defensive, and cheap to support.
    if str(right) in rows and str(right) in goods:
        return str(right), 1
    candidates = [eid for eid in _store_targets().get(store, {}).get(str(right), []) if eid in goods]
    if not candidates:
        return None
    # The first token is the row's current purchased count; use it to pick among duplicate targets.
    for eid in candidates:
        if int(history.get(eid, 0) or 0) == int(left):
            return eid, 1
    return candidates[0], 1


@lru_cache(maxsize=1)
def _event_aliases() -> dict[str, str]:
    """Base Moogle store -> the event store that replaces it.

    The shipped rows give the base NPCs (e.g. 900104) and the event store (7000122) the same town,
    type and owner name; the base row has no goods, while the event row does.  That is the same
    grouping the collaborator's `event_stores.py` uses, but here it is a schedule mapping rather than
    an edit to `F_STORE_ITEM_MST`.
    """
    stores = _store_rows()
    event = stores.get(EVENT_STORE)
    if not event:
        return {EVENT_STORE: EVENT_STORE}
    out: dict[str, str] = {EVENT_STORE: EVENT_STORE}
    for sid, row in stores.items():
        if str(row.get(TS_TYPE)) != "5" or sid == EVENT_STORE:
            continue
        if (str(row.get(TS_TOWN)) == str(event.get(TS_TOWN))
                and str(row.get(TS_OWNER)) == str(event.get(TS_OWNER))
                and str(row.get(TS_NAME)) == str(event.get(TS_NAME))
                and not _has_goods(sid)):
            out[sid] = EVENT_STORE
    return out


def _resolve_store(store_id: str) -> str:
    return _event_aliases().get(str(store_id), str(store_id))


def _request_group(plaintext: str | None, tag: str):
    if not plaintext:
        return None
    try:
        body = json.JSONDecoder().raw_decode(plaintext)[0]
    except Exception:  # noqa: BLE001 - a body that will not parse must not break the reply
        return None
    rows = body.get(tag) if isinstance(body, dict) else None
    return rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None


def _int(value):
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _parse_entries(text) -> list[tuple[int, int]] | None:
    """`<left>:<right>,...` -> [(left, right)]; [] when empty, None when malformed.

    WW 9.0.0 uses left = already-purchased count (0 on the first exchange), right = reward item id;
    the JP/Lapis shape uses left = exchange id, right = count.  Both are non-negative; the resolver
    decides which shape an entry is.
    """
    if text in (None, ""):
        return []
    out = []
    for entry in str(text).split(","):
        parts = entry.strip().split(":")
        if len(parts) != 2:
            return None
        left, right = _int(parts[0]), _int(parts[1])
        if left is None or right is None or left < 0 or right <= 0:
            return None
        out.append((left, right))
    return out if len(out) <= 20 else None


@lru_cache(maxsize=1)
def _equip_ids() -> set[str]:
    return {str(r.get("J1YX9kmM")) for r in _rows("F_EQUIP_ITEM_MST.ndjson")}


def _reward(text):
    """`<kind>:<id>:<count>[:...]` -> (kind, id, count), or None."""
    parts = str(text or "").split(":")
    if len(parts) < 3 or parts[0] not in _KINDS:
        return None
    rid, count = _int(parts[1]), _int(parts[2])
    if rid is None or count is None or rid <= 0 or count <= 0:
        return None
    if parts[0] == "20" and item_mst(rid) is None and str(rid) not in _book_ids():
        return None
    if parts[0] == "60" and str(rid) not in _book_ids():
        return None
    if parts[0] == "10" and unit_mst(rid) is None:
        return None
    if parts[0] == "21" and str(rid) not in _equip_ids():
        return None
    if parts[0] == "23" and not any(str(r.get("2dfixko3")) == str(rid)
                                    for r in _rows("F_IMPORTANT_ITEM_MST.ndjson")):
        return None
    if parts[0] == "27" and vision_card_mst(rid) is None:
        return None                      # kind 27 is a VISION CARD id, not an item: validate against its own table
    return parts[0], rid, count


def _grant(player, kind: str, item_id: int, count: int) -> None:
    if kind == "10":
        for _ in range(count):
            player.grant_unit(str(item_id))
    elif kind == "21":
        player.grant_equipment(item_id, count)
    elif kind == "27":
        # A VISION CARD. `grant_vision_card` is the one writer for `data['vision_cards']`, so the mandatory
        # Level and the duplicate refusal live there (see its docstring) rather than being re-implemented here.
        for _ in range(count):
            if player.grant_vision_card(item_id) is None:
                break
    elif kind in ("20", "60") and str(item_id) in _book_ids():
        books = player.data.setdefault("craft", {}).setdefault("books", {})
        books[str(item_id)] = {"count": books.get(str(item_id), {}).get("count", 0) + count}
        log.info("exchange: granted recipe book %s x%d", item_id, count)
    else:
        player.grant_item(int(kind), item_id, count)


def serve_exchange_shop(spec, reply_id, plaintext: str | None):
    """`ExchangeShopRequest`: buy one Magic Key product with lapis.

    The purchase is a state write only.  The reply's `LhVz6aD2` placeholder is replaced by
    `apply_to_reply` with the bag/lapis projection, which is what the client re-reads after the shop
    scene's own confirm handler (`ShopExchangeListScene::checkConnectResult` 0x2505db8).
    """
    if str(reply_id) != EXCHANGE_SHOP_REQUEST or st.State.player is None or not isinstance(spec, dict):
        return spec
    node = _request_group(plaintext, EXCHANGE_SHOP_GROUP)
    product = str((node or {}).get(EXCHANGE_SHOP_PRODUCT) or "")
    row = _exchange_items().get(product)
    if row is None or str(row.get(EI_TYPE)) != "2":
        log.info("exchange shop: unknown/product-not-lapis %r; no change", product)
        return spec
    price, count = _int(row.get(EI_DIAMOND)), _int(row.get(EI_COUNT))
    target = str(row.get(EI_TARGET) or "")
    if not price or not count or price <= 0 or count <= 0 or item_mst(target) is None:
        log.info("exchange shop: product %s has an invalid price/count/target; no change", product)
        return spec
    player = st.State.player
    if player.mode == "frozen":
        log.info("exchange shop: frozen state; product %s not bought", product)
        return spec
    if player.item_count(BAG_ITEM_TYPE, target) + count > STACK_MAX:
        log.info("exchange shop: product %s would exceed the stack cap %d", product, STACK_MAX)
        return spec
    if not player.spend_lapis(price):
        log.info("exchange shop: cannot pay %d lapis for product %s", price, product)
        return spec
    player.grant_item(BAG_ITEM_TYPE, target, count)
    log.info("exchange shop: product %s -> item %s x%d for %d lapis", product, target, count, price)
    return spec


def _can_grant(player, kind: str, item_id: int) -> bool:
    """Can this account RECEIVE the reward? Checked before any spend, so a purchase is all-or-nothing.

    Only kind 27 needs a pre-check today: the shop spends the currency first, and `grant_vision_card` refuses
    a card the account already owns (no recovered per-instance key scheme). Without this, buying a duplicate
    spent a ticket and granted nothing — the exact shape the mail claim's pre-check exists to prevent.
    """
    if str(kind) != "27":
        return True
    if vision_card_mst(item_id) is None:
        return False
    return not any(str(c.get("vc_id")) == str(item_id)
                   for c in player.data.get("vision_cards") or [])


def serve_medal_exchange(spec, reply_id, plaintext: str | None):
    """`MedalExchangeRequest`: spend the event currency for the shipped exchange rows."""
    if str(reply_id) != MEDAL_EXCHANGE_REQUEST or st.State.player is None or not isinstance(spec, dict):
        return spec
    node = _request_group(plaintext, MEDAL_EXCHANGE_GROUP)
    store = str((node or {}).get(MEDAL_EXCHANGE_STORE) or "")
    entries = _parse_entries((node or {}).get(MEDAL_EXCHANGE_ENTRIES))
    if not store or not entries:
        log.info("medal exchange: no store/entries (%r, %r); no change", store, entries)
        return spec
    resolved = _resolve_store(store)
    history = st.State.player.medal_exchange_counts()
    total: dict[str, int] = {}
    grants: list[tuple[str, int, int]] = []
    vc_targets: set = set()
    times: dict[str, int] = {}
    # A repeated id in one request is one purchase count, not two independent limit checks.
    wanted: dict[str, int] = {}
    for left, right in entries:
        picked = _resolve_entry(resolved, left, right, history)
        if picked is None:
            log.info("medal exchange: entry %s:%s is not sold at store %s (%s); no change",
                     left, right, store, resolved)
            return spec
        eid, count = picked
        wanted[eid] = wanted.get(eid, 0) + count
    times = _execute_exchange(resolved, wanted)
    if times is None:
        return spec
    log.info("medal exchange: store %s (%s), %s", store, resolved, times)
    return spec


def _execute_exchange(resolved: str, wanted: dict[str, int], source: str = "medal") -> dict[str, int] | None:
    """Validate, spend and grant `wanted` (exchange id -> count) at one store. None = nothing changed.

    SPLIT OUT OF `serve_medal_exchange` (2026-11-08) because the gacha-select exchange
    (`GachaSelectExchangeExeRequest`) buys from the SAME `F_MEDAL_EXCHANGE_MST` rows at the same stores. The
    grant kinds, the per-row limit, the currency balance test and the one-vision-card-per-request rule must
    not fork into two copies that can drift: a second copy is how the type-20 goods went missing for a week
    (SW-130 slice C).
    """
    # TWO CATALOGUES, ONE GUARD SET (APP-72). `medal` is the shipped F_MEDAL_EXCHANGE_MST family; `tfa` is the
    # authored Clash of Wills catalogue (`_tfa_products`). They differ only in where a row and its store
    # membership come from -- every limit, price and grant check below is the same code for both.
    if source == "tfa":
        rows = _tfa_products()
        goods = {pid for pid, p in rows.items() if p["detail"] == resolved}
    else:
        rows = _medal_rows()
        goods = {eid for _kind, eid in _store_goods().get(resolved, [])}
    history = st.State.player.medal_exchange_counts()
    total: dict[str, int] = {}
    grants: list[tuple[str, int, int]] = []
    vc_targets: set = set()
    times: dict[str, int] = {}
    for eid, count in wanted.items():
        row = rows.get(str(eid))
        if row is None or str(eid) not in goods:
            log.info("exchange: %s is not sold at store %s; no change", eid, resolved)
            return None
        if source == "tfa":
            currency, price = row["currency"], row["price"]
            limit, reward_text = row["limit"], row["reward"]
        else:
            currency, price = str(row.get(ME_CURRENCY) or ""), _int(row.get(ME_PRICE))
            limit, reward_text = _int(row.get(ME_LIMIT)) or 0, row.get(ME_REWARD)
        reward = _reward(reward_text)
        if not currency or not price or price <= 0 or reward is None or item_mst(currency) is None:
            log.info("exchange: row %s is not payable/readable; no change", eid)
            return None
        if limit > 0 and history.get(str(eid), 0) + count > limit:
            log.info("exchange: row %s limit %d reached (%d held + %d); no change",
                     eid, limit, history.get(str(eid), 0), count)
            return None
        total[currency] = total.get(currency, 0) + price * count
        kind, item_id, n = reward
        if not _can_grant(st.State.player, kind, item_id) or (str(kind) == "27" and item_id in vc_targets):
            log.info("exchange: row %s grants %s:%s, which this account cannot receive; no change",
                     eid, kind, item_id)
            return None
        if str(kind) == "27":
            vc_targets.add(item_id)
        grants.append((kind, item_id, n * count))
        times[str(eid)] = count
    if st.State.player.mode == "frozen":
        log.info("exchange: frozen state; no change")
        return None
    for currency, amount in total.items():
        if st.State.player.item_count(BAG_ITEM_TYPE, currency) < amount:
            log.info("exchange: %s held %d of %s, needs %d; no change",
                     st.State.player.data["identity"]["user_id"],
                     st.State.player.item_count(BAG_ITEM_TYPE, currency), currency, amount)
            return None
    for currency, amount in total.items():
        if not st.State.player.spend_item(BAG_ITEM_TYPE, currency, amount):
            log.info("exchange: failed to spend %d of %s; no change", amount, currency)
            return None
    for kind, item_id, count in grants:
        _grant(st.State.player, kind, item_id, count)
    st.State.player.record_medal_exchange(times)
    return times


def serve_variable_store(spec, reply_id):
    """Point the Moogle NPC's base store at the shipped 4th-anniversary event store, AND name the permanent
    exchange shops so the client's "Exchanges Available" list can draw them.

    The schedule is the one server-owned part of the store route: `EventStoreTopScene::updateEvent`
    (`0x229e350`) calls `getCurrentStoreInfo(storeId, now)` and then looks the *variableStoreId* up in
    `TownStoreMstList`.  The rows here are therefore base store -> event store, and the event's own
    store is included so a request that names it directly resolves too.

    THE LIST ONLY DRAWS WHAT THE SCHEDULE NAMES (SW-130, 2026-11-07). `sgShopSummonCoinTopScene::
    setGachaStoreDataList` (`0x24f7cf0`) walks the store list and calls `getCurrentStoreInfo(storeId, now)`
    for each entry, keeping only the ones that resolve. That is why the vision-card exchange never appeared on
    screen even though every piece of it was shipped: the store (`7000574`), its 33 kind-27 rows, its banner
    row (`F_GACHA_STORE_MST` 801582) and the banner art were all present, but no schedule row named it, so the
    list dropped it. The permanent exchange stores below are SELF-MAPPED rows (`storeId -> itself`) with an
    open window; their names and currencies come from the shipped tables.
    """
    if str(reply_id) != VARIABLE_STORE_REQUEST:
        return spec
    stores = _store_rows()
    event = stores.get(EVENT_STORE)
    if not event:
        return spec
    rows = []
    for base in sorted(_event_aliases(), key=lambda x: int(x) if x.isdigit() else x):
        rows.append({
            "Z20mNDvL": base,
            "G4L0YIB2": str(event.get(TS_NAME) or ""),
            "za2xsDj5": EVENT_STORE,
            "w7VR6ypY": "1",
            "VjJQ51uG": SCHEDULE_START,
            "m8ivD4NX": SCHEDULE_END,
            "suR36Jpm": "20:%s" % EVENT_CURRENCY,
            "89YmCAzo": _comment_rows().get((EVENT_STORE, "1"), ""),
            "0euar5mZ": _comment_rows().get((EVENT_STORE, "3"), ""),
            "7Vsr6DXp": "",
        })
    # THE PERMANENT EXCHANGE SHOPS, self-mapped (SW-130). See the docstring: without a schedule row the
    # client's list silently omits the store. Currency is the store's own first good, so the header shows
    # the ticket the shop actually takes.
    medal = _medal_rows()
    listed = {str(r["Z20mNDvL"]) for r in rows}
    for store_id, name in EXCHANGE_STORES:
        if store_id in listed or store_id not in stores:
            continue
        currency = ""
        for _kind, eid in _store_goods().get(store_id, []):
            row = medal.get(str(eid)) or {}
            if str(row.get(ME_CURRENCY) or ""):
                currency = str(row[ME_CURRENCY])
                break
        rows.append({
            "Z20mNDvL": store_id,
            "G4L0YIB2": name,
            "za2xsDj5": store_id,
            "w7VR6ypY": "1",
            "VjJQ51uG": SCHEDULE_START,
            "m8ivD4NX": SCHEDULE_END,
            "suR36Jpm": ("20:%s" % currency) if currency else "",
            "89YmCAzo": _comment_rows().get((store_id, "1"), ""),
            "0euar5mZ": _comment_rows().get((store_id, "3"), ""),
            "7Vsr6DXp": "",
        })
    if not rows:
        return spec
    out = dict(spec) if isinstance(spec, dict) else {}
    out[VARIABLE_STORE_TAG] = rows
    return out


def _gacha_store_banners() -> dict[str, str]:
    """store id -> the banner (gacha) id that names it, from the shipped `F_GACHA_STORE_MST` type-2 rows.

    This is the link `GachaSelectDetailMst.X1IuZnj2` calls "the row's banner id: the link back to the banner".
    `YtNcg3E3` is the table's own type column and is 2 for all ten exchange rows, which is why the banner art
    and the store resolve together.
    """
    out: dict[str, str] = {}
    for row in _rows("F_GACHA_STORE_MST.ndjson"):
        if str(row.get("YtNcg3E3")) != "2":
            continue
        out[str(row.get("Z20mNDvL"))] = str(row.get("X1IuZnj2") or "")
    return out


@lru_cache(maxsize=1)
@lru_cache(maxsize=1)
def _book_ids() -> frozenset[str]:
    """The recipe-BOOK id space (`F_RECIPE_BOOK_MST.staFu8U1`, 2,048 rows).

    A Clash of Wills "recipe" row is a BOOK, not a bag item: its id is `1000000xxx` and it is absent from
    `F_ITEM_MST`, which is why `_reward`'s kind-20 check refused 48 permanent-page rows until this set existed.
    Ownership lives in the craft block (`data['craft']['books']`), the store `ffbe_craft._owned_books` reads,
    so granting one here is what makes the recipe craftable.
    """
    return frozenset(str(r.get("staFu8U1")) for r in _rows("F_RECIPE_BOOK_MST.ndjson"))


def _store_currency() -> dict[str, str]:
    """store id -> the currency item id its first payable good costs, in shipped goods order.

    The same rule `serve_variable_store` uses for the list header: a shop's currency IS the first good's
    currency, because `F_MEDAL_EXCHANGE_MST` rows carry the price in that item.
    """
    medal = _medal_rows()
    out: dict[str, str] = {}
    for store, goods in _store_goods().items():
        for _kind, eid in goods:
            currency = str((medal.get(str(eid)) or {}).get(ME_CURRENCY) or "")
            if currency:
                out[store] = currency
                break
    return out


# --- THE VINTAGE GATE (APP-74) -------------------------------------------------------------------
# The shop is not "every banner the table holds" -- it is the SIX lists the player saw in the operator's
# archive footage of season 7: the five Celestite equipment lists and the unit list. Everything else in the
# shipped table belongs to another era (Plate and High Celestite are later, `2910000` is the Xenostone ability
# shop, and `2900000`/`2900001` are the Snow/Volt seasons' unit lists), so serving them puts other seasons'
# units on a Hydro screen -- measured live 2026-11-08 and confirmed against the footage.
TFA_VINTAGE_PAGES = (
    "61",          # Celestite Helm of Will
    "2000000",     # Celestite Garb of Will
    "2000001",     # Celestite Circlet of Will
    "2000002",     # Celestite Ring of Will
    "2000003",     # Celestite Vest of Will
    TFA_HYDRO_PAGE,  # the unit list, starting with Frostblade Fryevia
)

# The pages WE add on top of the shipped banner rows. One entry so far: the Hydro vintage.
TFA_AUTHORED_PAGES = (
    {"id": TFA_HYDRO_PAGE, "name": "Obnixus Drop (Hydro) Exchange",
     "art": "TFA_ObnixusDrop7_Exchange.png"},
)


def _tfa_pages() -> list[dict]:
    """The TFA banners we serve: the shipped ones in table order, then the authored pages."""
    out = []
    for row in _rows("F_BANNER_MST.ndjson"):
        if str(row.get("w2A76noc")) != TFA_ENTRY_SHOP_TYPE:
            continue
        art = str(row.get("6Dgaqir0") or "")
        if not art.startswith("TFA_"):
            continue
        out.append({"id": str(row.get("fSxqkF10") or ""),
                    "name": str(row.get("G4L0YIB2") or ""),
                    "art": art})
    # FILE ORDER, not `Z0EN6jSh`: the display-order column collides (2900001 and 2000005 are both 22), and the
    # table already lists the pages in the order retail drew them. Our own pages go last, beside the seasonal
    # ones they belong with rather than in front of the permanent set.
    return out + list(TFA_AUTHORED_PAGES)


@lru_cache(maxsize=1)
def _tfa_shipped_pages() -> dict[str, list[dict]]:
    """page id -> the client's OWN product rows ({id, name}) for that page, in shipped order.

    `server/tfa_products.json` is generated by `harness/build_tfa_products.py` from
    `F_TEXT_NV_SHOP_PRODUCT_NAME` — 132 Clash of Wills product names under real ids. The shipped order is the
    same star ladder the wiki lists (3★, 4★, 5★, +6★, +7★, ++8★, ++9★, then the 10★ packs), and the counts
    match the wiki page-for-page (17/17/17/28/17/17/20), so pairing them by index needs no fuzzy matching.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tfa_products.json")
    if not os.path.exists(path):
        return {}
    try:
        data = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        log.info("tfa: %s is unreadable; falling back to our own product ids", path)
        return {}
    return {str(k): v for k, v in (data.get("pages") or {}).items()}


@lru_cache(maxsize=1)
def _tfa_products() -> dict[str, dict]:
    """product id -> {detail, currency, price, limit, reward}, from the authored wiki catalogue.

    PRODUCT IDS ARE OURS AND MUST BE STABLE: the client echoes the id back in `GachaSelectExchangeExeRequest`,
    and the purchased count is keyed by it, so `<banner id> * 100 + <row index>` keeps both across restarts.
    The index is the row's position in the file, NOT its position among the resolved rows -- resolving one
    more row later therefore never renumbers the others.  The range does not overlap
    `F_MEDAL_EXCHANGE_MST`'s ids (checked: 0 collisions over 900 candidates).
    """
    if not os.path.exists(TFA_TSV):
        log.info("tfa: %s is missing; the Clash of Wills pages will draw with no goods", TFA_TSV)
        return {}
    out: dict[str, dict] = {}
    dropped = 0
    by_page: dict[str, list[dict]] = {}
    with open(TFA_TSV, encoding="utf-8") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            by_page.setdefault(row["page"], []).append(row)
    shipped_pages = _tfa_shipped_pages()
    for page, rows in by_page.items():
        banner = TFA_PAGE_BY_SLOT.get(page)
        if banner is None:
            continue                      # a season our F_BANNER_MST does not list
        shipped = shipped_pages.get(banner) or []
        for idx, row in enumerate(rows, 1):
            rid = str(row.get("reward_id") or "").strip()
            currency = str(row.get("currency_id") or "").strip()
            price = _int(row.get("price"))
            limit_txt = str(row.get("limit") or "").strip()
            if not rid or not currency or not price or price <= 0 or item_mst(currency) is None:
                dropped += 1
                continue
            reward = rid if rid.count(":") >= 2 else "%s:1" % rid
            # A "recipe" row is a RECIPE BOOK and must go out as kind 60 (APP-76): kind 20 takes the plain-item
            # icon branch, which is the white square the operator saw.
            if str(row.get("kind") or "") == "recipe" and reward.startswith("20:"):
                reward = "60:" + reward[3:]
            if _reward(reward) is None:
                dropped += 1
                continue
            # USE THE CLIENT'S OWN ID AND NAME when that page is one it ships (APP-75): the name then comes
            # from the shipped text table with no patch of ours, and the star ladder reads correctly.
            if idx <= len(shipped):
                pid, name = str(shipped[idx - 1]["id"]), str(shipped[idx - 1]["name"])
            else:
                pid, name = str(int(banner) * 100 + idx), str(row.get("reward_name") or "").strip()
            out[pid] = {
                "detail": banner,
                "name": name,
                "currency": currency,
                "price": price,
                "limit": _int(limit_txt) if limit_txt.isdigit() else 0,
                "reward": reward,
            }
    if dropped:
        log.info("tfa: %d catalogue row(s) dropped -- no resolved reward id or no shipped currency", dropped)
    return out


def tfa_product_names() -> dict[str, str]:
    """product id -> display name, for the products the vintage gate serves.

    The CLIENT builds a product's label from the text key `MST_NVSHOP_PRODUCT_NAME_<product id>`
    (`GachaSelectProductMst::getProductName` `0x1ce958c`: `IntToString(+0x24)` -> insert the prefix ->
    `TextManager::getMstText`), so an id with no text row renders the raw key. `harness/patch_nvshop_text.py`
    writes these rows into `F_TEXT_NV_SHOP_PRODUCT_NAME`; the name is the catalogue's own reward name.
    """
    return {pid: p["name"] for pid, p in _tfa_products().items()
            if p["detail"] in TFA_VINTAGE_PAGES and p["name"]}


def tfa_catalogue(shop_type: str = TFA_ENTRY_SHOP_TYPE):
    """The Clash of Wills pages: the ten shipped banners, each with the authored goods that resolved."""
    products = _tfa_products()
    details: list[dict] = []
    prod_rows: list[dict] = []
    for page in _tfa_pages():
        bid = page["id"]
        if bid not in TFA_VINTAGE_PAGES:
            continue                  # another era's page; see TFA_VINTAGE_PAGES
        sold = sorted((pid, p) for pid, p in products.items() if p["detail"] == bid)
        if not sold and bid not in TFA_EMPTY_PAGES:
            continue
        currency = sold[0][1]["currency"] if sold else ""
        details.append({
            GS_DETAIL_ID: bid,
            GS_DETAIL_GACHA: bid,
            GS_DETAIL_TYPE: shop_type,
            GS_DETAIL_ORDER: str(len(details) + 1),
            GS_DETAIL_BANNER: bid,
            GS_DETAIL_LIMIT: "-1",
            GS_DETAIL_TEXT: page["name"],
            GS_DETAIL_START: SCHEDULE_START,
            GS_DETAIL_END: SCHEDULE_END,
            GS_DETAIL_COST: ("20:%s" % currency) if currency else "",
        })
        for pid, p in sold:
            prod_rows.append({
                GS_PRODUCT: pid,
                GS_PRODUCT_DETAIL: bid,
                GS_PRODUCT_REWARD: p["reward"],
                GS_PRODUCT_PRICE: str(p["price"]),
                GS_PRODUCT_LIMIT: str(p["limit"]) if p["limit"] > 0 else "-1",
                GS_PRODUCT_ORDER: str(len(prod_rows) + 1),
                GS_PRODUCT_TEXT: "",
                GS_PRODUCT_START: SCHEDULE_START,
                GS_PRODUCT_END: SCHEDULE_END,
                GS_PRODUCT_BONUS: "",
                GS_PRODUCT_INERT: "",
            })
    return details, prod_rows


def gacha_select_catalogue(shop_type: str = DEFAULT_SHOP_TYPE):
    """(details, products, count rows) for the exchange family, priced from the shipped tables.

    CONTENT IS SHIPPED, NOT INVENTED: every product is an `F_MEDAL_EXCHANGE_MST` row sold by one of the
    `EXCHANGE_STORES` (`F_STORE_ITEM_MST` joins them), so the reward, the price and the purchase limit are the
    game's own numbers. Only the shop text and the window are ours (the exchange period ended), the same
    authored-window rule `serve_variable_store` already follows.
    """
    if str(shop_type) == TFA_ENTRY_SHOP_TYPE:
        # A DIFFERENT FAMILY, not a filter on the Moogle one (APP-72). Its goods are authored from the
        # community wiki because `PrizeTranslatorPointMst` is a server-side table we do not hold.
        return tfa_catalogue(shop_type)
    banners = _gacha_store_banners()
    currency = _store_currency()
    medal = _medal_rows()
    goods = _store_goods()
    details: list[dict] = []
    products: list[dict] = []
    for store_id, name in EXCHANGE_STORES:
        sold = goods.get(store_id) or []
        if not sold:
            continue
        banner = banners.get(store_id, "")
        details.append({
            GS_DETAIL_ID: store_id,
            GS_DETAIL_GACHA: banner,
            GS_DETAIL_TYPE: shop_type,
            GS_DETAIL_ORDER: str(len(details) + 1),
            GS_DETAIL_BANNER: banner,
            GS_DETAIL_LIMIT: "-1",
            GS_DETAIL_TEXT: name,
            GS_DETAIL_START: SCHEDULE_START,
            GS_DETAIL_END: SCHEDULE_END,
            GS_DETAIL_COST: ("20:%s" % currency[store_id]) if store_id in currency else "",
        })
        for _kind, eid in sold:
            row = medal.get(str(eid)) or {}
            price = _int(row.get(ME_PRICE))
            reward = str(row.get(ME_REWARD) or "")
            if not price or not reward:
                continue
            limit = _int(row.get(ME_LIMIT))
            products.append({
                GS_PRODUCT: str(eid),
                GS_PRODUCT_DETAIL: store_id,
                GS_PRODUCT_REWARD: reward,
                GS_PRODUCT_PRICE: str(price),
                GS_PRODUCT_LIMIT: str(limit) if limit > 0 else "-1",
                GS_PRODUCT_ORDER: str(len(products) + 1),
                GS_PRODUCT_TEXT: "",
                GS_PRODUCT_START: SCHEDULE_START,
                GS_PRODUCT_END: SCHEDULE_END,
                GS_PRODUCT_BONUS: "",
                GS_PRODUCT_INERT: "",
            })
    return details, products


def _select_counts() -> list[dict]:
    """The player's purchased counts, in the shape `UserGachaSelectProductInfo` reads."""
    history = (st.State.player.medal_exchange_counts() if st.State.player else {}) or {}
    out = []
    for eid, count in history.items():
        detail = ""
        for store, goods in _store_goods().items():
            if any(str(x) == str(eid) for _k, x in goods):
                detail = store
                break
        out.append({GS_COUNT_PRODUCT: str(eid), GS_COUNT_DETAIL: detail, GS_COUNT_ALL: str(count)})
    return out


def serve_gacha_select_entry(spec, reply_id, plaintext: str | None):
    """`GachaSelectEntryRequest` (6nTcSp4R): the exchange screen's catalogue. NO response class of its own.

    THE EMPTY-BODY TRAP APPLIES HERE TOO (APP-65): a reply with no group is a zero-length body, which the
    client shows as its connection dialog. This handler therefore always answers with at least the universal
    user-info tag, whether or not the catalogue is built.

    Shop type is ECHOED, not filtered on (see DEFAULT_SHOP_TYPE): the client asks for one type and we have a
    single exchange family, so filtering would be our guess at a mapping we have not measured. Which of the
    ten banners the player tapped is NOT in the request, so all ten details go out and the client draws them.
    """
    if str(reply_id) != GACHA_SELECT_ENTRY_REQUEST:
        return spec
    out = dict(spec) if isinstance(spec, dict) else {}
    out.setdefault("LhVz6aD2", [{}])
    node = _request_group(plaintext, GACHA_SELECT_ENTRY_GROUP)
    shop_type = str((node or {}).get(GACHA_SELECT_SHOP_TYPE) or DEFAULT_SHOP_TYPE)
    details, products = gacha_select_catalogue(shop_type)
    if not details:
        log.info("gacha select: no exchange catalogue built (shop type %r)", shop_type)
        return out
    out[GS_DETAILS] = details
    out[GS_PRODUCTS] = products
    out[GS_COUNTS] = _select_counts()
    # ...and the schedule rows that let those banners pass `withinPeriod` (APP-77).
    out[GS_SCHEDULE] = [{GS_SCHEDULE_ID: str(d[GS_DETAIL_BANNER]),
                         GS_SCHEDULE_START: SCHEDULE_START,
                         GS_SCHEDULE_END: SCHEDULE_END}
                        for d in details if str(d.get(GS_DETAIL_BANNER) or "").isdigit()]
    log.info("gacha select: shop type %r -> %d detail(s), %d product(s)", shop_type, len(details), len(products))
    return out


def serve_gacha_select_exchange(spec, reply_id, plaintext: str | None):
    """`GachaSelectExchangeExeRequest` (W58vo4Hp): buy one exchange product. Same tables, same guards as the
    medal exchange -- literally the same function (`_execute_exchange`), only the request shape differs:

        {4mrD6JuA: [{jPV7Xc4N: <exchange id>, Qy5EvcK1: <count>}]}

    The reply carries the purchased count (`4owH1uT2`, the class `UserGachaSelectProductInfoResponse` reads) and
    the full count list (`qS62ewjH`) so the screen's "held/limit" labels refresh without a second round trip.
    """
    if str(reply_id) != GACHA_SELECT_EXCHANGE_REQUEST:
        return spec
    out = dict(spec) if isinstance(spec, dict) else {}
    out.setdefault("LhVz6aD2", [{}])
    node = _request_group(plaintext, GACHA_SELECT_EXCHANGE_GROUP)
    product = str((node or {}).get(GS_PRODUCT) or "")
    quantity = _int((node or {}).get(GS_QUANTITY)) or 1
    if not product or quantity < 1:
        log.info("gacha select exchange: no product/quantity (%r, %r); no change", product, quantity)
        return out
    detail, source = "", "medal"
    for store, goods in _store_goods().items():
        if any(str(eid) == product for _k, eid in goods):
            detail = store
            break
    if not detail:
        tfa = _tfa_products().get(str(product))
        if tfa is not None:
            detail, source = tfa["detail"], "tfa"
    if not detail:
        log.info("gacha select exchange: product %r is sold nowhere we know; no change", product)
        return out
    if st.State.player is None:
        return out
    times = _execute_exchange(detail, {product: quantity}, source=source)
    if times is None:
        return out
    log.info("gacha select exchange: store %s, %s", detail, times)
    out[GS_SELL_COUNTS] = [{GS_COUNT_PRODUCT: product, GS_COUNT_DETAIL: detail,
                            GS_COUNT_ALL: str(times.get(product, 0))}]
    out[GS_COUNTS] = _select_counts()
    return out


if __name__ == "__main__":
    # Offline smoke test: no server socket, no saved state.
    from player_state import PlayerState, default_state
    st.State.player = PlayerState(default_state())
    st.State.player.add_currency("lapis_free", 10000)
    st.State.player.grant_item(BAG_ITEM_TYPE, EVENT_CURRENCY, 1000)
    serve_exchange_shop({"LhVz6aD2": [{}]}, EXCHANGE_SHOP_REQUEST,
                        json.dumps({EXCHANGE_SHOP_GROUP: [{EXCHANGE_SHOP_PRODUCT: "200001"}]}))
    assert st.State.player.item_count(BAG_ITEM_TYPE, "106100100") == 1
    assert st.State.player.currencies["lapis_free"] == 9900
    # WW 9.0.0 sender shape: already-purchased count : reward item id.
    assert _parse_entries("0:250000070") == [(0, 250000070)]
    assert _resolve_entry("7000122", 0, 250000070, {}) == ("21800", 1)
    serve_medal_exchange({"LhVz6aD2": [{}]}, MEDAL_EXCHANGE_REQUEST,
                         json.dumps({MEDAL_EXCHANGE_GROUP: [{MEDAL_EXCHANGE_STORE: "900104",
                                                             MEDAL_EXCHANGE_ENTRIES: "0:250000070"}]}))
    assert st.State.player.item_count(BAG_ITEM_TYPE, EVENT_CURRENCY) == 950
    assert st.State.player.item_count(BAG_ITEM_TYPE, "250000070") == 5
    assert st.State.player.medal_exchange_counts() == {"21800": 1}
    assert st.State.player.apply_to_reply({"LhVz6aD2": [{}]}, MEDAL_EXCHANGE_REQUEST)["R5kozET8"] == [
        {"S21FT3L8": "21800", "ngQX7a3N": "1"}]
    # --- APP-63: the GACHA-SELECT exchange family ------------------------------------------------
    # The screen's catalogue is reply GROUPS (the class has no response object): details -> shops,
    # products -> what they sell, counts -> what the player already bought.
    _sel = serve_gacha_select_entry({"LhVz6aD2": [{}]}, GACHA_SELECT_ENTRY_REQUEST,
                                    json.dumps({GACHA_SELECT_ENTRY_GROUP: [{GACHA_SELECT_SHOP_TYPE: "5"}]}))
    _det, _prod = _sel[GS_DETAILS], _sel[GS_PRODUCTS]
    assert len(_det) == len(EXCHANGE_STORES), (len(_det), len(EXCHANGE_STORES))

    # --- APP-72: shop type 4 is the CLASH OF WILLS family, and it must NOT be the Moogle one ----------
    _tfa = serve_gacha_select_entry({}, GACHA_SELECT_ENTRY_REQUEST,
                                    json.dumps({GACHA_SELECT_ENTRY_GROUP: [{GACHA_SELECT_SHOP_TYPE: "4"}]}))
    _tdet, _tprod = _tfa[GS_DETAILS], _tfa[GS_PRODUCTS]
    assert _tdet, "shop type 4 served no pages"
    assert {d[GS_DETAIL_ID] for d in _tdet} & {s for s, _n in EXCHANGE_STORES} == set(), \
        "shop type 4 served a Moogle store page -- the two families must not mix"
    assert {d[GS_DETAIL_TYPE] for d in _tdet} == {"4"}, "a Clash of Wills detail is stamped with the wrong type"
    assert _tprod, "the Clash of Wills catalogue sells nothing"
    assert {p[GS_PRODUCT_DETAIL] for p in _tprod} <= {d[GS_DETAIL_ID] for d in _tdet}, \
        "a Clash of Wills product points at a page that was not served"
    # Re-running must give identical ids: the client echoes one back when it buys.
    assert [p[GS_PRODUCT] for p in _tprod] == [p[GS_PRODUCT] for p in tfa_catalogue("4")[1]]
    _tpid, _trow = next(iter(_tfa_products().items()))
    _tdetail = _trow["detail"]
    st.State.player.grant_item(BAG_ITEM_TYPE, _trow["currency"], _trow["price"])
    _tbuy = serve_gacha_select_exchange({}, GACHA_SELECT_EXCHANGE_REQUEST,
                                        json.dumps({GACHA_SELECT_EXCHANGE_GROUP:
                                                    [{GS_PRODUCT: _tpid, GS_QUANTITY: "1"}]}))
    assert _tbuy[GS_SELL_COUNTS][0][GS_COUNT_DETAIL] == _tdetail, "a Clash of Wills buy lost its page"
    assert st.State.player.medal_exchange_counts().get(_tpid) == 1, "a Clash of Wills buy was not recorded"
    assert st.State.player.item_count(BAG_ITEM_TYPE, _trow["currency"]) == 0, "a Clash of Wills buy did not spend"
    assert _prod, "the catalogue sells nothing"
    assert _sel.get(GS_SCHEDULE), "no banner schedule rows -- every banner would fail withinPeriod"
    assert {p[GS_PRODUCT_DETAIL] for p in _prod} <= {d[GS_DETAIL_ID] for d in _det}, \
        "a product points at a detail that was not served -- the row would be dropped"
    assert all(p[GS_PRODUCT_START] < p[GS_PRODUCT_END] for p in _prod), "a product window is closed"
    assert all(set(r) == {GS_COUNT_PRODUCT, GS_COUNT_DETAIL, GS_COUNT_ALL} for r in _sel[GS_COUNTS]), \
        "a count row is not in the shape UserGachaSelectProductInfo reads"
    # Buy one product through the gacha-select request: the SAME guards as the medal exchange.
    _shop, _eid = next((sid, x) for sid, goods in _store_goods().items()
                       for _k, x in goods if sid in dict(EXCHANGE_STORES))
    _price = _int((_medal_rows()[_eid] or {}).get(ME_PRICE))
    _cur = _store_currency()[_shop]
    st.State.player.grant_item(BAG_ITEM_TYPE, _cur, _price)
    _buy = serve_gacha_select_exchange({}, GACHA_SELECT_EXCHANGE_REQUEST,
                                       json.dumps({GACHA_SELECT_EXCHANGE_GROUP: [
                                           {GS_PRODUCT: _eid, GS_QUANTITY: "1"}]}))
    assert _buy[GS_SELL_COUNTS] == [{GS_COUNT_PRODUCT: _eid, GS_COUNT_DETAIL: _shop, GS_COUNT_ALL: "1"}], \
        _buy.get(GS_SELL_COUNTS)
    assert st.State.player.item_count(BAG_ITEM_TYPE, _cur) == 0, "the price was not taken"
    assert {GS_COUNT_PRODUCT: _eid, GS_COUNT_DETAIL: _shop, GS_COUNT_ALL: "1"} in \
        serve_gacha_select_entry({}, GACHA_SELECT_ENTRY_REQUEST,
                                 json.dumps({GACHA_SELECT_ENTRY_GROUP: [{GACHA_SELECT_SHOP_TYPE: "5"}]})
                                 )[GS_COUNTS], "the count did not reach the reply"
    _schedule = serve_variable_store({"LhVz6aD2": [{}]}, VARIABLE_STORE_REQUEST)[VARIABLE_STORE_TAG]
    _aliases = _event_aliases()
    assert len(_aliases) == 5, len(_aliases)          # base Moogle NPC stubs only, NOT real shops
    assert "7000414" not in _aliases and "7000434" not in _aliases, "a real shop is aliased to the event store"
    assert len(_schedule) == len(_aliases) + len(EXCHANGE_STORES), len(_schedule)
    assert {r["Z20mNDvL"] for r in _schedule} >= {sid for sid, _n in EXCHANGE_STORES}, "a permanent exchange is not scheduled"

    # --- SW-130 slice C: the PERMA-VC store (7000574) actually sells a card -------------------------
    # The store is 33 kind-27 rows at 1 x 1209001443; both halves of that were unservable before: the goods
    # index read types 40/50 only (this store is 50 + 20) and kind 27 had no grant path at all.
    goods = _store_goods().get("7000574") or []
    assert len(goods) == 33, f"perma-VC goods index: {len(goods)} of 33"
    assert "7000434" in _store_goods(), "the Master Card store's type-20 row is still invisible"
    target, exchanges = next(iter(_store_targets()["7000574"].items()))
    st.State.player.grant_item(BAG_ITEM_TYPE, "1209001443", 2)
    before_cards = len(st.State.player.data.get("vision_cards") or [])
    serve_medal_exchange({"LhVz6aD2": [{}]}, MEDAL_EXCHANGE_REQUEST,
                         json.dumps({MEDAL_EXCHANGE_GROUP: [{MEDAL_EXCHANGE_STORE: "7000574",
                                                             MEDAL_EXCHANGE_ENTRIES: "0:" + target}]}))
    cards = st.State.player.data.get("vision_cards") or []
    assert len(cards) == before_cards + 1, f"a card was not granted: {cards[-1:]}"
    assert str(cards[-1]["vc_id"]) == str(target) and int(cards[-1]["level"]) == 1, cards[-1]
    assert st.State.player.item_count(BAG_ITEM_TYPE, "1209001443") == 1, "the ticket was not spent"
    assert str(exchanges[0]) in st.State.player.medal_exchange_counts(), "purchase not recorded"
    # ...and a SECOND copy is refused without spending (no recovered per-instance key scheme).
    serve_medal_exchange({"LhVz6aD2": [{}]}, MEDAL_EXCHANGE_REQUEST,
                         json.dumps({MEDAL_EXCHANGE_GROUP: [{MEDAL_EXCHANGE_STORE: "7000574",
                                                             MEDAL_EXCHANGE_ENTRIES: "1:" + target}]}))
    assert len(st.State.player.data.get("vision_cards") or []) == before_cards + 1, "a duplicate was banked"
    assert st.State.player.item_count(BAG_ITEM_TYPE, "1209001443") == 1, "a refused duplicate spent a ticket"
    print("ffbe_shops: PASS")
