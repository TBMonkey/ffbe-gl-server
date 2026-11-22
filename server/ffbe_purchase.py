#!/usr/bin/env python3
"""ffbe_purchase.py — the "purchase lapis" shop: the product LEDGER, from the shipped `F_DIAMOND_MST`.

WHAT THE CLIENT DOES (read at the binary; `docs/notes/purchase_setting_response.md` §6)
----------------------------------------------------------------------------------------
`ShopListScene::queryProductInfo` `0x1e6bed0` builds the tile list from the client's OWN MST:

    DiamondMstList::getListWithPlatform(CommonUtils::getTargetOs())   ; the platform filter
    ProductPriceInfo::getPrice(<diamondId>)                           ; price, from billing or the MST
    ... then requestPurchaseList()                                    ; ← this request

So the shipped `F_DIAMOND_MST` is what the client renders: the tile NAME comes from the localized key
`MST_DIAMOND_NAME_<id>` and the tile's lapis text from `MST_DIAMOND_DESC_<id>` (`F_TEXT_DIAMOND_NAME`, both
shipped). The server's job is only the **ledger** — which products to offer, in what order — and that is
`UserPurchaseListInfoResponse` (`TxDI2r9g`).

THE PLATFORM FILTER IS A REAL FILTER, NOT DECORATION (`[FFBEWW]` `0x1d2d454`):
`getListWithPlatform(os)` keeps a row only when `row+0x24 == os` AND `row+0x68 == 1`. In `F_DIAMOND_MST` the
`K1G4fBjF` column holds that platform int, and the client sends its own OS as `K1G4fBjF` in the user-info
group every boot (`2` on this Android build, `docs/notes/boot_packet_capture.md` L38). Serving rows from
another family therefore produces tiles that match nothing. `F_DIAMOND_MST` carries SIX families of the same
seven packs — `1`/`2` (iOS/Android WW, identical USD prices), `101`/`102`/`103` (the same packs addressed by
other store ids) and `5` (`ff_be_payment_*`, the JP packs with JP prices absent).
So we filter by the platform the request itself names, and fall back to the whole table only when that
filter would serve nothing (a miss must report, idiom #25).

WHAT IS *NOT* OURS: the money. The prices in `F_DIAMOND_MST` (`AOHRcCC2`) are the shipped USD list prices, and
they are what the tile shows when `ProductPriceInfo` has no billing answer. Executing a purchase is Play
Billing (`LapisJNI_*`), which cannot run in the harness — so this serves a catalogue, not a checkout.
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache

import ffbe_state as st
from ffbe_config import TEAM_INFO_TAG
from player_state import _load_ndjson, _now_iso

log = logging.getLogger(__name__)

PURCHASE_LIST_REQUEST = "BT28S96F"
PURCHASE_LIST_TAG = "TxDI2r9g"
USER_INFO_GROUP = "LhVz6aD2"
USER_PLATFORM = "K1G4fBjF"

# `F_DIAMOND_MST` columns.
D_ID, D_PLATFORM, D_SKU = "D7FX3MYh", "K1G4fBjF", "TzvJwA60"
D_TOTAL, D_BASE, D_BONUS, D_PRICE = "FBjo93gK", "T7sah9rc", "cb3WmiD0", "AOHRcCC2"
D_ORDER, D_STATE, D_DESC = "Z0EN6jSh", "hnLD21Gs", "96ej0dxb"

# `UserPurchaseListInfoResponse` columns (`harness/response_field_map.json`, 18 tokens, 100% known).
R_ID, R_EXT_ID, R_EVENT_ID = "D7FX3MYh", "bj4na3FV", "pKEF6B5m"
R_BONUS_P, R_TYPE = "Mpw1RHa7", "sx4kyW0i"
R_BALLOON_MSG, R_BALLOON_DATE, R_BONUS_MSG = "6A0UrG1R", "Y6Imoyq3", "mAS53v92"
R_REWARD_ID, R_REWARD_MSG, R_REWARD_TEXT, R_REWARD_INFO = "PWN98f6B", "25HVQ3rg", "JtayH9d7", "dX6cor8j"
R_ORDER, R_FRAME, R_REWARD_TITLE = "Z0EN6jSh", "8s9wNSpd", "i1H4Whof"
R_REWARD_BALLOON, R_INFO_TYPE, R_SALE_STATE = "E90oBqct", "K3BV17gk", "hnLD21Gs"

# Product-type discriminator (`setType`, +0x34): 0 is the plain lapis pack, which is every row here.
TYPE_NORMAL_PACK = "0"
DEFAULT_PLATFORM = "2"          # Android — `CommonUtils::getTargetOs()` on the Waydroid build
# THE LEDGER'S SALE STATE IS THE SAME FLAG THE MST GATE READS (APP-68). The shipped F_DIAMOND_MST carries
# `hnLD21Gs = 0` on every row (the archived data says the lapis shop is closed), and our served overlay flips
# it to 1 for this platform's seven packs — so the ledger rows must say 1 as well or the tile renders with a
# SOLD OUT mark over a product it will happily sell.
SALE_STATE_ON = "1"


@lru_cache(maxsize=1)
def diamond_rows() -> list[dict]:
    return [row for row in (_load_ndjson("F_DIAMOND_MST.ndjson").get("__rows__") or [])]


def platform_of(plaintext: str | None) -> str:
    """The OS int the CLIENT named in its user-info group; `DEFAULT_PLATFORM` when the body has none.

    Reading it from the request rather than assuming is what keeps the platform filter honest: the same
    client sends its OS here, so a build on another store gets that store's family.
    """
    import json
    try:
        body = json.loads(plaintext or "{}")
    except ValueError:
        return DEFAULT_PLATFORM
    group = body.get(USER_INFO_GROUP)
    node = group[0] if isinstance(group, list) and group and isinstance(group[0], dict) else {}
    value = str(node.get(USER_PLATFORM) or "").strip()
    return value or DEFAULT_PLATFORM


def _bonus_percent(row: dict) -> str:
    base = int(row.get(D_BASE) or 0)
    bonus = int(row.get(D_BONUS) or 0)
    return str(round(bonus * 100 / base)) if base > 0 and bonus > 0 else "0"


def purchase_rows(platform: str) -> list[dict]:
    """The ledger rows for one platform, in the shipped display order.

    Every row is a plain pack: no reward payload (`PWN98f6B` 0 keeps `IsReward()` false), no sale event, no
    balloon. The tile therefore renders from the client's own MST + localized text, which is why the ledger
    carries ids and ordering and nothing cosmetic.
    """
    rows = [r for r in diamond_rows() if str(r.get(D_PLATFORM)) == str(platform)]
    if not rows:
        log.info("purchase list: no F_DIAMOND_MST row for platform %r; serving every family", platform)
        rows = diamond_rows()
    rows = sorted(rows, key=lambda r: (int(r.get(D_ORDER) or 0), int(r.get(D_ID) or 0)))
    out = []
    for row in rows:
        out.append({
            R_ID: str(row.get(D_ID) or ""),
            R_EXT_ID: "0",
            R_EVENT_ID: "0",
            R_BONUS_P: _bonus_percent(row),
            R_TYPE: TYPE_NORMAL_PACK,
            R_BALLOON_MSG: "",
            R_BALLOON_DATE: "",
            R_BONUS_MSG: "",
            R_REWARD_ID: "0",
            R_REWARD_MSG: "",
            R_REWARD_TEXT: "",
            R_REWARD_INFO: "",
            R_ORDER: str(row.get(D_ORDER) or ""),
            R_FRAME: "0",
            R_REWARD_TITLE: "",
            R_REWARD_BALLOON: "0",
            R_INFO_TYPE: "0",
            R_SALE_STATE: SALE_STATE_ON,
        })
    return out


def serve_purchase_list(spec, reply_id, plaintext: str | None):
    """`PurchaseListRequest` → `TxDI2r9g` (the ledger). An empty list is a valid reply, so this can only
    improve on what was there; a failure to parse the table leaves the spec exactly as it was."""
    if str(reply_id) != PURCHASE_LIST_REQUEST:
        return spec
    platform = platform_of(plaintext)
    rows = purchase_rows(platform)
    if not rows:
        log.info("purchase list: F_DIAMOND_MST yielded nothing; ledger left empty")
        return spec
    out = dict(spec) if isinstance(spec, dict) else {}
    out.setdefault(USER_INFO_GROUP, [{}])
    out[PURCHASE_LIST_TAG] = rows
    # THE EX-POINT BALANCE rides the same reply: `ShopListScene::checkConnectResult` `0x1e6e58c` (the
    # PurchaseList carrier's connect handler) refreshes `updateExviusPointInfo` `0x1337fd0`, so the header
    # and the per-tile labels are re-read from the singleton after this reply lands.
    ex_rows = ex_point_rows()
    if ex_rows:
        out[EX_POINTS_TAG] = ex_rows
    log.info("purchase list: platform %s -> %d pack(s): %s", platform, len(rows),
             ", ".join("%s@%s" % (r[R_ID], next((x.get(D_PRICE) for x in diamond_rows()
                                                 if str(x.get(D_ID)) == r[R_ID]), "?")) for r in rows))
    return out


# =====================================================================================================
# THE SIMULATED PURCHASE (AUTHORED — Play Billing cannot run in the harness)
# =====================================================================================================
# WHAT THE CLIENT SENDS (observed live). `PurchaseStartRequest` `qAUzP3R6` is a `DiamondMst` in its OWN
# settlement group: `{iq8w45vP: [{D7FX3MYh:"207", TzvJwA60:"ffbe_ww_pack007", 9gCG8jnK:"190001",
# Gy9iws0u:"1", bj4na3FV:"0", pKEF6B5m:"0"}]}`. That group tag is the SAME tag as
# `PurchaseSettlementResponse`, which is why the answer is keyed with it: the client's factory builds
# `PurchaseSettlementResponse` (`readParam` `0x195d774`) and its `onReadStarted` `0x195d760` clears
# `UserPurchaseResult` first, so the reply REPLACES the result rather than merging into a stale one.
#
# WHY THE REPLY CONTROLS THE OUTCOME. `UserPurchaseResult+0x90` is the transaction state and the client's
# own state machine switches on it: `ShopListScene::updateEvent` `0x1e6ca28` loads +0x90, computes `state-2`
# and jumps through a 6-entry halfword table at `0x2f5d01e` (states 2..7). The branches read straight out of
# the binary:
#   2  -> `0x1e6ca50`  `BUY_COIN_END_CANCEL_MSG_GOOGLE`      (the "purchase cancelled" the operator saw)
#   3  -> `0x1e6d234`  `BUY_COIN_END_DEFERRED_MSG`
#   4  -> `0x1e6d140`  `BUY_COIN_END_COMP_MSG_GOOGLE`        <- COMPLETED: what we serve
#   5  -> `0x1e6d264`  `BUY_COIN_END_GIVEUP_MSG`
#   6  -> `0x1e6d0bc`  (UserInfo+0x158 dispatch)
#   7  -> `0x1e6d110`  `BUY_COIN_END_NG_MSG_GOOGLE`
#   81 -> `0x1e6cfdc`  `BUY_COIN_END_APPLIED_MSG_GOOGLE`     <- "already applied": served for a RETRY
# So 4 is the completed state and 81 is the client's own "this transaction was already consumed" message.
# What is NOT proven: `ShopListScene` installs `BuyCoinStoreKitScene` for the pre-settlement state and THAT
# scene's `sendPaymentRequest` `0x24e0d3c` is what calls `PaymentUtil::paymentRequest` — whether the client
# reaches the store scene before it parses our reply is a LIVE question, not a static one. See the note in
# `serve_purchase_start`.
#
# THE GRANT IS OURS. Play never settles, so the server credits the pack itself: `F_DIAMOND_MST.FBjo93gK`
# lapis into the PAID pool (`UserDiamondInfo+0x24` = `T7sah9rc` in the reply the projection serves), and EX
# points per the table below. LABEL: simulated. Nothing here is recovered except the lapis amount.
PURCHASE_START_REQUEST = "qAUzP3R6"
PURCHASE_FAILED_REQUEST = "jSe80Gx7"
SETTLEMENT_TAG = "iq8w45vP"          # PurchaseSettlementResponse — and the request's OWN group tag
S_DIA_ID, S_DIA_ALIAS = "D7FX3MYh", "9gCG8jnK"   # both assign UserPurchaseResult+0x28
S_MODEL, S_RECEIPT = "Gy9iws0u", "X9Y2fHVk"     # +0x58 purchaseModelStatus, +0x60 receipt
S_SIGNATURE, S_STATE = "y2Xrq6jM", "HVnLs7I0"    # +0x78 signature, +0x90 purchaseState
S_PRODUCT = "TzvJwA60"                           # +0x98 productId

PURCHASE_STATE_COMPLETED = "4"       # 0x1e6d140 — BUY_COIN_END_COMP_MSG_GOOGLE
PURCHASE_STATE_APPLIED = "81"        # 0x1e6cfdc — BUY_COIN_END_APPLIED_MSG_GOOGLE (a replayed receipt)
RECEIPT_SIGNATURE = "SIMULATED-NOT-A-PLAY-RECEIPT"

# --- EX POINTS (currency kind 52) ---------------------------------------------------------------------
# STORAGE, read out of the binary: the header number is `UserExviusPointInfo+0x30`, and the wire class is
# `UserExviusPointInfoResponse` `NVC6JP91` — `readParam` `0x19231bc` has exactly three tokens:
#   `kRPFs6T4` -> atoi -> +0x30   (`getCurrentPoint` `0x1db10e8` / `setCurrentPoint` `0x1db10f0`)
#   `Jh4Y5zTN` -> atoi -> +0x34   (`getGetPoint` `0x1db10f8` / `setGetPoint` `0x1db1100`)
#   `3Qtws0oX` -> `parseExviusPointInfoList` `0x1db0700` (per-diamond rows, see below)
# The header itself: `ShopListScene::updateExviusPointLabel` `0x1e70550` does `UserExviusPointInfo::shared()`
# then `ldr w0,[x0,#0x30]` -> `CommonUtils::IntToString` -> `BitmapLabelEx::updateString`. The label text key
# is `EX_POINT` ("EX pts", `F_TEXT_TEXT_EN`), and `ShopListScene::isEnableExviusPoint` `0x1e74f90` gates the
# feature on `DefineMst["SPPT_DEF"] == 0xd431`, which the shipped `F_DEFINE_MST` row satisfies (54321).
# The tag is not named by any request body, so it is a CARRIER group any reply may add; `NVC6JP91` is
# "gated, non-clearing" (`harness/request_api_map.json`), so appending it is not destructive.
EX_POINTS_TAG = "NVC6JP91"
EX_CURRENT, EX_EARNED, EX_LIST = "kRPFs6T4", "Jh4Y5zTN", "3Qtws0oX"

# THE EARN RULE IS **COMMUNITY-SOURCED, NOT RECOVERED** — exvius.fandom.com/wiki/EXVIUS_Points. No shipped
# table carries a per-pack EX-point value: `F_DIAMOND_MST`'s 12 columns have none, and the only EX data the
# client ships is the MILESTONE table `F_EXVIUS_POINT_REWARD_MST` (15 rows at 1..300 points, reward strings
# only). Keyed by the pack's TOTAL lapis (`FBjo93gK`) rather than by diamond id, because the seven ids
# differ per store family while the pack contents do not.
EX_POINTS_BY_LAPIS = {"120": 1, "500": 4, "1000": 7, "1500": 10, "3200": 20, "8500": 50, "18000": 100}
EX_POUCH_LAPIS = "120"      # the once-a-day cap belongs to this pack alone (community: "1 (once a day)")
EX_POUCH_DAILY_CAP = 1


def ex_points_for(total_lapis) -> int:
    """The AUTHORED per-pack award; 0 for a pack the table does not name (a miss reports, idiom #25)."""
    return int(EX_POINTS_BY_LAPIS.get(str(total_lapis), 0))


def ex_point_rows() -> list[dict]:
    """The `NVC6JP91` rows for the current player: the header balance and the lifetime total.

    `3Qtws0oX` is sent as `<diamondId>@<points>` pairs — and the earlier reading of it as a TEXT KEY was
    wrong, corrected 2026-11-08 from the callee list of `parseExviusPointInfoList` `0x1db0700`: it uses
    `CommonUtils::parseList`, `CommonUtils::split` and **`CommonUtils::StrToInt`** to build a
    `cocos2d::__Dictionary` of id -> NUMBER. That is why no per-pack text key was ever found in the 155
    English packs: the second field is not a key at all. `ShopListScene::setLayout` `0x1e73a18` then reads
    that dictionary per tile, so sending the award per pack is what puts the number on the line.
    """
    player = getattr(st.State, "player", None)
    if player is None:
        return []
    state = player.ex_point_state()
    row = {EX_CURRENT: str(state["current"]), EX_EARNED: str(state["earned"])}
    # The per-tile line: one `<id>@<award>` entry per pack the shop actually offers (this platform's family),
    # keyed by diamond id so the tile can find its own. Empty when nothing in the table maps to an award —
    # a miss must report rather than paint a 0.
    awards = []
    for r in diamond_rows():
        if str(r.get(D_PLATFORM)) != DEFAULT_PLATFORM:
            continue
        points = ex_points_for(r.get(D_TOTAL))
        if points:
            # THREE FIELDS, NOT ONE (2026-11-08). `<id>@<labelKey>:<formatKey>:<value>`: `parseExviusPointInfoList`
            # `0x1db0700` splits on '@' then ':', and the tile line is `getDispName() + "\n" + getText(p0) +
            # format(getText(p1), p2)` — the local formatter `0x1db0ea8` runs `vsnprintf` with the p1 text as
            # the format. `SERVER_MSG_247` = "Exvius pts <color=255:255:0>+%s</color>", so p1 gives the label
            # and the yellow, and p2 the number. The EMPTY p0 is deliberate (split keeps empty tokens).
            awards.append("%s@:SERVER_MSG_247:%d" % (r.get(D_ID), points))
    if awards:
        row[EX_LIST] = ",".join(awards)
    return [row]


def _settlement_request(plaintext: str | None) -> dict | None:
    """The first `iq8w45vP` row of a `PurchaseStartRequest` body, or None."""
    if not plaintext:
        return None
    try:
        body = json.JSONDecoder().raw_decode(plaintext)[0]
    except Exception:  # noqa: BLE001 - a body that will not parse must not break the reply
        return None
    rows = body.get(SETTLEMENT_TAG) if isinstance(body, dict) else None
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return rows[0]
    return None


def serve_purchase_start(spec, reply_id, plaintext: str | None):
    """`PurchaseStartRequest` -> simulate the settlement: credit the pack, answer as a completed purchase.

    GATED on the request id (the group tag is shared by the request and the response, so keying on the tag
    alone would fire on any reply that happens to carry it).

    THE SPLIT THAT MATTERS: the money is OURS (authored), the pack is the SHIPPED one — the diamond id is
    resolved in `F_DIAMOND_MST` and the credit is that row's `FBjo93gK`. An id the table does not know is a
    REFUSAL: the spec is returned untouched (the empty-body floor keeps the reply valid), no lapis is
    granted, and the client falls back to its own billing path. A refusal that looks like success would be
    a silent lie about someone else's money.

    UNVERIFIED LIVE (2026-11-08): whether the client reaches `BuyCoinStoreKitScene` (which calls
    `PaymentUtil::paymentRequest`) before it parses this reply. If it does, the tap ALSO reports
    "cancelled" even though the grant has landed — the credit is unconditional on that, so the lapis and
    the EX points are real either way; what is not proven is the completion notice.
    """
    if str(reply_id) != PURCHASE_START_REQUEST:
        return spec
    req = _settlement_request(plaintext)
    if req is None:
        log.info("purchase start: no iq8w45vP row in the body; nothing settled")
        return spec
    diamond_id = str(req.get(S_DIA_ID) or req.get(S_DIA_ALIAS) or "").strip()
    product = str(req.get(S_PRODUCT) or "").strip()
    row = next((r for r in diamond_rows() if str(r.get(D_ID)) == diamond_id), None)
    if row is None:
        log.info("purchase start: %r is not an F_DIAMOND_MST id; refused (no credit)", diamond_id)
        return spec
    player = getattr(st.State, "player", None)
    if player is None:
        log.info("purchase start: no player state loaded; refused (no credit)")
        return spec
    total = int(row.get(D_TOTAL) or 0)
    points = ex_points_for(total)
    # The POUCH's once-a-day cap (community rule, authored): the pack still pays its lapis, but a second
    # purchase of it on the same UTC day pays no EX points.
    today = _now_iso()[:10]
    pouch_day = None
    if str(total) == EX_POUCH_LAPIS:
        if player.ex_point_state().get("pouch_day") == today:
            points = 0
            log.info("purchase start: pouch EX point already paid today (%s); cap %d applied",
                     today, EX_POUCH_DAILY_CAP)
        else:
            pouch_day = today
    settle = player.settle_simulated_purchase(
        diamond_id, product or str(row.get(D_SKU) or ""), total, points, pouch_day=pouch_day)
    if not settle.get("ok"):
        log.info("purchase start: settlement refused (%s); no credit", settle.get("reason"))
        return spec
    state = PURCHASE_STATE_APPLIED if settle.get("replay") else PURCHASE_STATE_COMPLETED
    if settle.get("replay"):
        log.info("purchase start: REPLAY of receipt %s for %s; no second credit",
                 settle.get("receipt"), diamond_id)
    else:
        log.info("purchase start: SIMULATED BUY %s (%s) -> +%d lapis, +%d EX pts (paid balance %s, EX %s)",
                 diamond_id, product or row.get(D_SKU), settle["lapis"], settle["ex_points"],
                 settle["balance"], settle["ex"])
    out = dict(spec) if isinstance(spec, dict) else {}
    out[SETTLEMENT_TAG] = [{
        S_DIA_ID: diamond_id,
        # The alias token also writes +0x28, so it must carry the SAME id — the request puts the client's
        # birth-year-month ("190001") in this slot, and echoing that would name the wrong product.
        S_DIA_ALIAS: diamond_id,
        S_MODEL: str(req.get(S_MODEL) or "1"),
        S_RECEIPT: str(settle.get("receipt") or ""),
        S_SIGNATURE: RECEIPT_SIGNATURE,
        S_STATE: state,
        S_PRODUCT: product or str(row.get(D_SKU) or ""),
    }]
    out[EX_POINTS_TAG] = ex_point_rows()
    # The projection trigger: `apply_to_reply` rewrites this tag with the team row and, in the same pass,
    # adds the diamond group — that is how the new PAID lapis reaches the HUD (`apply_shop_use`'s precedent).
    out[TEAM_INFO_TAG] = [{}]
    return out


def record_purchase_failure(plaintext: str | None):
    """`PurchaseFailedRequest` -> close the retry window so the next tap is a NEW purchase, not a replay.

    Deliberately no credit and no rollback: a failure AFTER we settled (the client gave up on the store
    while our reply was in flight) must not take back lapis the player has already been shown. It only
    stops the idempotency window from swallowing the next deliberate purchase.
    """
    req = _settlement_request(plaintext) or {}
    product = str(req.get(S_PRODUCT) or "").strip() or None
    player = getattr(st.State, "player", None)
    if player is None:
        return
    closed = player.close_purchase_retry_window(product)
    log.info("purchase failed: closed %d open settlement window(s) (%s)", closed, product or "any product")


if __name__ == "__main__":
    import json
    import sys
    if "--selftest" in sys.argv:
        rows = purchase_rows(DEFAULT_PLATFORM)
        print("platform %s: %d row(s)" % (DEFAULT_PLATFORM, len(rows)))
        assert rows, "no Android pack rows"
        assert [r[R_ID] for r in rows] == sorted((r[R_ID] for r in rows), key=int), "display order is not stable"
        for r in rows:
            src = next(x for x in diamond_rows() if str(x.get(D_ID)) == r[R_ID])
            print("  id %-4s $%-6s %6s lapis  %s" % (r[R_ID], src[D_PRICE], src[D_TOTAL], src[D_DESC]))
        print("selftest: PASS")
