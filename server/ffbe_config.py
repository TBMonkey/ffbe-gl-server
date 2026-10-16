#!/usr/bin/env python3
"""ffbe_config.py — every constant the synthetic server has, in one place.

Extracted 2026-09-26 from `capture_server.py` (docs/SERVER_REFACTOR_PLAN.md, Phase 3). Two reasons, and the
second is the interesting one.

**One, navigation.** These ~95 names were scattered through a 3900-line file, each sitting under the section
banner that explained it. That is good documentation and poor structure: a wire token's meaning was recorded
next to the function that used it, so a token had no single home and could be read as "the drop token" from
three different screens of the file.

**Two, and the reason the split is forced rather than merely tidy.** `capture_server` holds
`reply_pipeline`, which is now a loop over the `PIPELINE` table, and the transforms it drives need these
constants. A transform module that imported them from `capture_server` would make
`capture_server -> transforms -> capture_server`: a cycle. So the constants have to sit *below* both. That
dependency is invisible until you attempt the refactor, and it is what decided the whole module layout.

Every constant keeps the comment block that documented it, verbatim. The comments are the recovered
provenance -- an address, a note ID, a measured consequence -- and separating them from the constant would
make this module a list of magic tokens.

WHAT IS **NOT** HERE, and why, matters as much as what is:
  * **runtime state** -- `LAST_DROPS`, `NOTES`, `SEASON`, and the `STATE_*` knobs `main()` rebinds. These
    change while the server runs, and a config module holding them would be a lie about what it is. They
    live in `ffbe_state`.
  * **lazy caches** -- `_CHALLENGE_TABLE`, `VERSION_TABLE`, `_CAMPAIGN_DOC`, `_LOGIN_CALENDAR`, the two art
    indexes, and the log-once sets. A cache belongs with its reader, not in a table of constants.
  * **the pipeline** -- `PIPELINE` and its edges are behaviour, not configuration.

`__all__` is explicit rather than implied, and it is load-bearing rather than decorative:
`from ffbe_config import *` is the intended way to consume this module, and `_MONTH_NO` starts with an
underscore, so an implicit star import would silently drop it. A star import normally hides names; here the
module has no behaviour at all -- it is a table -- so the one thing a star import could hide, a missing
constant, is impossible, and the explicit list makes the whole surface reviewable in one place.
"""
from __future__ import annotations

import os

import data_paths   # the data-root contract (`FFBE_DATA` / `--data`); see its docstring


HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.dirname(HERE)  # APK_emulator_work/
# --- banner ART served from the extracted CPKs ---------------------------------------------------
# The client downloads banner art as a LOOSE file, not from an MST:
#   GET /banner/<lang>/sd/static_banner/gacha/<dir>/<file>
# `files/` (the APK tree) holds banner art INSIDE `<lang>/sd/banner/Ver<N>_banner1.cpk`, so the loose path
# misses and the client reports a connection error (observed 2026-09-20, `generalpu_bn_gacha1_blackfriday.png`).
# Our extraction of those CPKs is under `cpk_extract/<lang>/banner/Ver<N>_*/`; serve by BASENAME from there,
# bounded to image files whose request path names a banner, and only when the exact path misses.
# The AREA art roots. The gate/list tile art a gate row names (the 4yT7FUqj token, e.g.
# spd_bn_list_00003.png) lives HERE, not under banner/, and the CDN basename fallback used to fire only for
# paths containing "banner" -- so a tile art that was not already in the client's own store 404ed and the
# tile drew its frame with nothing in it (operator report 2026-10-12). 2681 PNGs, small enough to index.
# The art roots follow `ART_ROOT` (new env override; `<data root>/cpk_extract` when a root is set,
# else the checkout-relative default). The shipped `server/assets/banner_art` and the game tree's
# `files/homeMarquee` are NOT extracted CPKs and stay anchored to the code/game tree respectively.
AREA_ART_ROOTS = [os.path.join(data_paths.ART_ROOT, "en", "area"),
                  os.path.join(data_paths.ART_ROOT, "common_lang", "area")]
BANNER_ART_ROOTS = [os.path.join(data_paths.ART_ROOT, "en", "banner"),
                    os.path.join(data_paths.ART_ROOT, "common_lang", "banner"),
                    os.path.join(data_paths.ART_ROOT, "common_lang", "gacha"),
                    # the COMMUNITY calendar's fetched art (harness/fetch_gacha_community.py; gitignored)
                    data_paths.COMMUNITY_ART_ROOT,
                    # SHIPPED daily-login banner art, cropped once out of the client's own atlas
                    # (`en/resources/Ver202_resources/image/ui/dailylogin/dailylogin.plist`) -- the frames the
                    # atlas holds and the layout never draws, because the calendar banner is DOWNLOADED
                    # (`DailyLoginBonusBannerResponse` -> `DailyLoginBonusInfo::setImageName`; the client asks
                    # for `<P_GS_BANNER_PATH>/login/<name>`). docs/notes/daily_login_bonus.md §7.
                    os.path.join(HERE, "assets", "banner_art"),
                    # the HOME MARQUEE's own cache -- the ONLY tree holding real marquee art
                    # (`files/homeMarquee/HomeBanner_<id>.png`, 8 files, all 600x120) plus the client's
                    # `marquee_banner.ver` index. This is the client's runtime files/ dir, i.e. what it
                    # downloaded from Square's CDN before our server was ever in the path.
                    os.path.join(data_paths.GAME_FILES, "homeMarquee")]


# --- the HOME MARQUEE (`sgHomeMarqueeInfoResponse`, tag `DxCN9SNZ`) -------------------------------
# The marquee is 100% server-authoritative: `sgHomeMarqueeManager::getBannerMstList()` (`0x1a4df94`)
# builds its list from the REPLY's own records, never a table lookup, and the row carries the image URL
# (`TsZhcqbT`), the window (`twb8TQPx`/`ZkGp8XbQ`) and the version key (`JZryxaYY`). Nothing in the
# shipped tables dates a banner, so WHICH era is live is a decision, exactly like `GACHA_CAMPAIGN`:
#   BANNER_CAMPAIGN=<era>   e.g. `2024-08` -- one era from server/banner_campaigns.json
#   BANNER_CAMPAIGN=all     every era's rows (or `0`/`off`)
#   BANNER_CAMPAIGN=promos  the client's cached banners that no dated event claims (see `PROMOS`)
#   UNSET                   the file's own `default` era, so a harness reset cannot silently lose it.
# Docs: docs/notes/home_screen_payloads.md S11, S11.7.
BANNER_CAMPAIGN_FILE = os.path.join(HERE, "banner_campaigns.json")
BANNER_CAMPAIGN = os.environ.get("BANNER_CAMPAIGN", "")
BANNER_CAMPAIGN_OFF = ("all", "0", "off", "none")   # explicit "serve everything"
# The window is AUTHORED, always: the client filters the list by `getNowUnixTime()` against the row's own
# start/end, and every era in the table is in the PAST (the client's own cache spans 2022-2024). So the
# served window is rebased onto the run -- the era dates are PROVENANCE for which art this is, not a
# window we can serve. `MARQUEE_WINDOW_DAYS` is the width; the start sits one day back so a row is inside
# its window on the second it arrives rather than on a boundary.
MARQUEE_WINDOW_DAYS = int(os.environ.get("MARQUEE_WINDOW_DAYS", "21"))
# `P_GS_FEATURE_HOME_MARQUEE` ("sgHomeMarqueeManager enable") must be "1" for the marquee to draw at all.
# Set it to `0` to A/B the whole feature off while leaving the rows served.
MARQUEE_FLAG = os.environ.get("MARQUEE_FLAG", "1")
# The base the image URL is built on. It MUST be a host the harness redirects -- the URL in the client's
# own `marquee_banner.ver` index is on `d3syu63yncawjw.cloudfront.net`, which is NOT redirected, so a row
# pointing there sends the client to the real internet. `P_GS_BANNER_PATH` is already this host.
MARQUEE_URL_BASE = os.environ.get("MARQUEE_URL_BASE", "https://v900-lapis.gumi.sg/banner/en/sd/static_banner/marquee")
# `uP4695k3` is the compound `"<gate>:<locationId>"` row field, and BOTH halves are gates -- read out of
# `_canTransit` (0x1a4ce48, size 0xb4), which every displayed row must pass:
#   0x1a4ce68  ldr w8,[x1,#0xcc] ; cmp w8,#0xa ; b.ne  -> the int half MUST equal 10
#   0x1a4ce74  add x0,x1,#0x30 ; bl BannerMst::getUrl ; bl CommonUtils::StrToInt
#   0x1a4ceac  bl FunctionMstList::shared ; bl FunctionMstList::isLocationValid(int)
# `isLocationValid` (0x1d30be8) is a switch from location id to a `FunctionMst` KEY, then a dictionary
# lookup and `FunctionMst::isValid()`. 601 -> 'FUNCTION_REEL_DIMENSION', whose shipped `F_FUNCTION_MST`
# row has `5U0bAqgR = "1"` -- i.e. it resolves TRUE from data we already serve. Both values are AUTHORED:
# the real server's values were never captured (the marquee has 0 captured rows), so these are the values
# the client's own gate demands, not data we recovered.
MARQUEE_LOCATION_GATE = os.environ.get("MARQUEE_LOCATION_GATE", "10")

# --- the 8th-ANNIVERSARY ticket: awarded by MAIL, one per calendar day --------------------------
# The original game sent these to the inbox rather than granting them (`docs/notes/home_screen_payloads.md`
# SS11.14), which is also why an over-full bag was harmless: nothing is granted until the mail is received.
# Item 1209002123 "8th Anniversary 10+1 Summon Ticket" (`F_ITEM_MST` + `MST_ITEM_NAME_1209002123`), spent
# on the seven shipped banners named "8th Anniversary 10+1 Ticket Summon" (MST_GACHA_NAME_1241 .. 1305).
#
# `ANNIVERSARY_TICKET_TYPE` = 20 is DERIVED, not chosen: `UserItemInfo::getItemType()` is the compile-time
# constant 20 (`0x1d8cae8 mov w0,#0x14`), so the regular bag is the type-20 singleton projected by
# `item_rows` (`0dNXQa3R`). It is NOT 23: `F_IMPORTANT_ITEM_MST` is a catalogue of named item CONCEPTS keyed
# by `2dfixko3` in a hand-assigned space (20/30/40 ... 110431), and 1209002123 is in neither its `2dfixko3`
# nor its `0HUPxDf1` column -- so the client has no record for this ticket in the important-item singleton
# and a type-23 row would render as a nameless entry. (That table DOES hold anniversary tickets -- ids 280/290
# for the 2nd and 110000-110002 for the 4th -- but there is no 8th, which is why ours is a plain item.)
ANNIVERSARY_TICKET = os.environ.get("ANNIVERSARY_TICKET", "1") == "1"
ANNIVERSARY_TICKET_ITEM = int(os.environ.get("ANNIVERSARY_TICKET_ITEM", "1209002123"))
ANNIVERSARY_TICKET_TYPE = int(os.environ.get("ANNIVERSARY_TICKET_TYPE", "20"))
ANNIVERSARY_TICKET_COUNT = int(os.environ.get("ANNIVERSARY_TICKET_COUNT", "1"))
ANNIVERSARY_TICKET_TITLE = os.environ.get("ANNIVERSARY_TICKET_TITLE", "8th Anniversary Ticket")
# THE CARRIERS, and why there is more than one. The game did not "push" this mail: the server held it and
# the client collected it, so the honest shape is "the mail exists the moment the server has it, and the next
# inbox fetch shows it" -- NOT "restart the app".
#   * `75fYdNxq` InitializeRequest -- boot step 3, once per boot, so a FRESH account has the ticket in the
#     inbox before HOME rather than up to 30 minutes later.
#   * `Daud71Hn` RoutineHomeUpdateRequest -- MEASURED at a 30-minute cadence while HOME is up (captures:
#     17:36:55, 18:06:55, 18:36:55, 19:06:55, 19:36:55): a background sweep.
#   * `KQHpi0D7` MailListRequest -- the inbox refresh, and the carrier that makes "server pushed" literally
#     true: the mail is created **because the player asked what is in the inbox**, so it is there on the
#     first open with no boot and no wait. This was added after MEASURING that opening the inbox does NOT
#     bring a home update with it (19:41:03 and 19:44:44 are `KQHpi0D7`/`CQ4jTm2F` only, no `Daud71Hn`), so
#     the 30-minute poll alone would have left the ticket invisible for up to half an hour after the server
#     gained the feature.
# All three, because each covers the others' gap; the daily mail id makes the overlap idempotent (the inbox
# request arrives in BURSTS -- three `KQHpi0D7` within 2 s on one open).
# Deliberately NOT `P6pTz4WA` (CreateUser): that scene's result gate is INVERTED (responses.json `_comment`).
ANNIVERSARY_TICKET_CARRIER = os.environ.get("ANNIVERSARY_TICKET_CARRIER",
                                           "75fYdNxq,Daud71Hn,KQHpi0D7")
MARQUEE_LOCATION_ID = os.environ.get("MARQUEE_LOCATION_ID", "601")


# --- gacha BUTTON art (`GachaDetailMst.XFLm4Mx6`, `img_btn`) -------------------------------------
# The client appends `_1.png`/`_2.png` (normal/pressed), and `_ticket_1/2.png` for a ticket-only banner.
# The families (gacha_banner_draw_path.md §13): `gacha_btns_gold_<N>` = N-pull; `gacha_btn_r_ticket<itemId>`
# = a ticket button keyed by the ticket item id; `gacha_btn_r_multi_ticket<itemId>` = the BULK variant.
BUTTON_ART_ROOTS = [os.path.join(data_paths.ART_ROOT, "en", "gacha"),
                    os.path.join(data_paths.ART_ROOT, "common_lang", "gacha")]


DROP_TAG_ROWS = "y35YvQjZ"      # WaveBattleInfo rows carry the per-phase drop CANDIDATE list
DROP_TOKEN = "9BetM2Ds"         # MissionDropInfo::setDropInfo (+0x38)
RESULT_TAG = "09HRWXDf"         # MissionResultResponse: the result screen's own item channels
RESULT_DROP_TOKEN = "xF9Sr1a6"  # addItem(20, 0, csv)  -- kind 0 = the DROPS channel
# The result reply's ITEM channels -- `addItem(20, kind, csv)`, mapped in `mission_end_progression.md` S4.2:
# kind 0/1/2/3/5/6 -> these six. `2U05aV9Z` (units) is deliberately NOT here: it is a separate list, and
# the A/B these feed is about the ITEM strip. Read only by `RESULT_ITEM_COLUMNS=0` (SW-93b).
RESULT_ITEM_TOKENS = ("xF9Sr1a6", "JHMit30L", "Sp7fF6I9", "02aARqrv", "Z7G0yxXe", "EWmQy52k")

# The top bar's LAPIS, and why the server must push it. `J3pAG0I5` -> `UserDiamondInfoResponse`
# (`readParam` 0x1921bcc, 2 tokens) is the only writer of the balance besides the purchase path:
# `UserDiamondInfo::incFreeDia` has exactly 4 call sites and none is in the mission path
# (`ClsmVsCpu*::addReward`, `MailSceneCommon::{canAddItem,receiveItem}`) -- so the client does not
# credit a challenge award itself, and `UIHeader::update` (0x209a9d0) renders whatever the two
# singletons hold. Evidence: docs/notes/audit_stat_bar.md.
DIAMOND_TAG = "J3pAG0I5"        # UserDiamondInfoResponse
DIAMOND_FREE = "T7sah9rc"       # -> UserDiamondInfo+0x24 (free)
DIAMOND_PAID = "cb3WmiD0"       # -> UserDiamondInfo+0x28 (paid)
# A reply carrying one of these groups is a user-state refresh; that is where the balance rides.
USER_STATE_GROUPS = ("LhVz6aD2", "3oU9Ktb7")  # UserInfoResponse / UserTeamInfoResponse
TEAM_INFO_TAG = "3oU9Ktb7"        # UserTeamInfoResponse -- carries n69quiCJ, the DISPLAYED emblem
PARTY_DECK_GROUP = "5Eb0Rig6"              # UserPartyDeckInfoResponse -- the deck rows, BOTH directions
# --- the EMBLEM family (status HUD's `header_emblem_icon` -> PlayerEmblemSelectScene) ---------------
# One group, BOTH directions: `PlayerEmblemSettingRequest::createBody` (0x199c570) emits `3Ys8WPS6`
# with `9Tbns0eI` = the user id and `0w6XzofV` = the chosen id; `UserPlayerEmblemInfoResponse::readParam`
# (0x192304c) reads `0w6XzofV` back off `UserPlayerEmblemInfo` +0x24 -- as the OWNED list.
EMBLEM_GROUP = "3Ys8WPS6"                  # UserPlayerEmblemInfoResponse
EMBLEM_ID = "0w6XzofV"                      # the emblem id (also `F_EMBLEM_ITEM_MST`'s key column)

# The item-set (carry list) group. `UserCarryItemInfoResponse` and `ItemCarryEditRequest` share ONE tag and
# ONE key, and the value is the class's own CSV -- `dispOrder:itemId:itemNum` per element, elements joined
# with `,`. **`UserCarryItemInfoResponse::onReadStarted` CLEARS `UserCarryItemInfoList` before the parse
# rebuilds it**, so any reply carrying this group must carry the COMPLETE list: a partial echo deletes the
# rest. Derived and audited 2026-10-07 -- `docs/notes/item_state_tokens.md` §2, `currency_model.md` G3.
CARRY_ITEM_GROUP = "nE7pV2wr"
CARRY_ITEM_CSV = "jsvoa0I2"
CARRY_EDIT_REQUEST = "UM7hA0Zd"        # `ItemCarryEditRequest`'s constant requestID (the reply key)
EMBLEM_USER_ID = "9Tbns0eI"                 # the user id the setting request carries alongside it
EMBLEM_SETTING_REQUEST = "19YwqU2T"         # PlayerEmblemSettingRequest
EMBLEM_ENTRY_REQUEST = "Z7J9H6TK"           # PlayerEmblemEntryRequest

# --- the EMBLEM EVENT (`sgEmblemEventScene`) ---------------------------------------------------------
# ONE request (`sgEmblemEventRequest`) whose reply carries THREE class-typed groups; the client's response
# factory dispatches by TAG (`harness/response_tag_map.json` maps each tag to its class), so one body
# serves all three -- which is why the corpus shows "nothing sending" them separately. The schedule is
# date-windowed and server-authoritative: the TABLES are shipped (`F_EMBLEM_WEEKLY_MST` /
# `F_EMBLEM_MONTHLY_MST`), the CHOICE of campaign is ours. One home: docs/notes/emblem_event.md.
EMBLEM_EVENT_REQUEST = "wMWvnYDR"           # sgEmblemEventRequest (url wMWvnYDR.php, key U9QMZrFq)
EMBLEM_MONTHLY_GROUP = "VY9DTFhP"           # sgUserEmblemMonthlyInfoResponse -- campaign + crest count
EMBLEM_WEEKLY_GROUP = "VVzhCSFB"            # sgUserEmblemWeeklyInfoResponse -- per-milestone progress
EMBLEM_MILESTONE_GROUP = "ru7MthFL"         # sgEmblemMilestoneInfoResponse -- the milestone windows
EMBLEM_CAMPAIGN_TOKEN = "a75e27e0"          # monthly: the campaign id (the F_EMBLEM_MONTHLY_MST group id)
EMBLEM_TOKEN_COUNT = "HBeUpWRe"             # monthly: CampaignTotalToken = the crests earned so far
EMBLEM_BUNDLE_ID = "EzXXHtTY"               # monthly: BundleId -- authored, no shipped source
EMBLEM_PASS_STATUS = "LeSXDwwn"             # monthly: EmblemPassStatus; authored lever EMBLEM_PASS
EMBLEM_BANNER_IMAGE = "br61hsM7"            # monthly: BannerImage resource name -- authored
EMBLEM_PERSISTENCE = "CPf4J1yw"             # monthly: Persistence
EMBLEM_DISPLAY_ORDER = "QvqubbO7"           # monthly: DisplayOrder
EMBLEM_MILESTONE_TOKEN = "sVhVVRu8"         # weekly + milestone: the milestone id (the weekly MST join key)
EMBLEM_WEEKLY_PROGRESS = "oz5Vt6AR"         # weekly: progress toward the milestone
EMBLEM_WEEKLY_CLEARED = "Ag8bisM0"          # weekly: the cleared flag
# The window is `CommonUtils::convertToTime` input: `sgEmblemMilestoneInfoResponse::readParam`
# (0x1946bd0 StartDate / 0x1946be8 EndDate) each `bl CommonUtils::convertToTime` 0x136fd10 -- the same call
# the PROVEN panel-quest schedule feeds PANEL_QUEST_WINDOW_FORMAT, so that format is the wire shape, not an
# epoch number (the field map's "time_t" is what the client STORES after the conversion).
EMBLEM_START_DATE = "VjJQ51uG"              # monthly + milestone: window start
EMBLEM_END_DATE = "m8ivD4NX"                # monthly + milestone: window end
# The tier mail. `ynP26rjD` is a u64 client-side (`toNumber<long long>`), so the id must be NUMERIC or its
# receipt can never match (the daily-login mail's measured lesson, 2026-09-22); this base is well clear of
# login's 900000000000 + day.
EMBLEM_TIER_MAIL_TITLE = "Adventurer Rewards"   # authored: the campaign's own name (the community table's)
EMBLEM_MAIL_ID_BASE = 900000100000

# --- unit management (SW-106): the three request groups + their reply classes --------------------
# Read in the binary 2026-09-24; one home: docs/notes/unit_management.md.
FAV_GROUP = "R8U3uD1i"                     # OpeUnitFavoriteResponse -- BOTH directions (request AND reply)
FAV_SET = "9Eb7m1NR"                       # comma unique-unit-ids to SET favourite
FAV_CLEAR = "mGuM1Z9e"                     # comma unique-unit-ids to CLEAR favourite
FAV_INFO_TAG = "F2pYJGt5"                  # UserUnitFavoriteResponse() -- the no-arg ctor; full-list push
STORAGE_GROUP = "JMxPV87y"                 # OpeUnitStorageUpdateResponse -- readParam {return 1}
STORAGE_STATE = "km9gKXJ5"                 # int state (1 = into the warehouse)
STORAGE_IDS = "ng9xPNt8"                   # comma unique-unit-ids
SELL_GROUP = "Ray54PNQ"                    # UnitSellResponse -- readParam {return 1} (the tag map says
                                           # BaseResponse: the factory ctor + vptr-override shape, SW-106)
SELL_GIL = "FBjo93gK"                      # the client's own computed total sell gil
SELL_IDS = "S5Ib7En1"                      # comma unique-unit-ids
# Unit FUSION (`UnitMixRequest` `UiSC9y8R` -> `UnitMixResponse`), tag `f08du6EW` -- BOTH directions, like the
# favourite pair. Request tokens are the same two the reply class reads (verified from a live capture
# 2026-09-24, out/requests/20260924T075911_0003.json): `og2GHy49` = the BASE unit's instance id,
# `WbGYcN17` = the comma-separated MATERIAL instance ids.
MIX_GROUP = "f08du6EW"
# --- the BEAST (esper) progression (APP-26) ------------------------------------------------------
# `BeastBoardPieceOpenRequest` `0gk3Tfbz` and `BeastMixRequest` `C8X1KUpV` — both `addGroup` requests
# with NO factory response class, so the client reads nothing back: it applied its change at the tap and
# these carry the REPORT. What must agree is the server's copy (the next `gP9TW2Bf`/`1S8P2u9f`).
BEAST_BOARD_GROUP = "M7t6Iksb"             # the board request's own group
BEAST_BOARD_LEARNED = "2fim9g1S"           # `PieceInfo`: piece ids LEARNED (CP spent at the tap)
BEAST_BOARD_UNLOCKED = "0bMAwC1t"          # `UnlockPieceInfo`: piece ids whose UNLOCK anime ended
BEAST_MIX_GROUP = "Simv74We"               # the mix request's own group
BEAST_MIX_ITEMS = "MgAVu3X6"               # `ItemInfo`: `20:<itemId>:<count>,…` (kind 20 = the bag)
BEAST_ID_TOKEN = "Iwfx42Wo"                # EsperId — the same token in both request groups
# The cumulative favourite list rides the user-state refreshes (serve_challenge_clear_all's mechanism,
# same allow-list) -- a restarted client would otherwise lose every favourite flag, because
# UserFavoriteInfoList::shared() is rebuilt empty and nothing else re-feeds it.
FAVOURITE_INFO_ALL = os.environ.get("FAVOURITE_INFO_ALL", "1") == "1"
# The summon result: `GachaExeResponse` (`readParam` 0x196da28) stores into the UserGachaResult SINGLETON, so
# ONE node is the whole result. The award is `UJz5QED2` = "<type>:<id>" with type 10 = unit; `zoU5J3Kq` is the
# effect-pattern id. Until the roster became state this was cosmetic (SW-61); `serve_gacha` now grants it.
GACHA_TAG = "5yLD4dYI"
GACHA_AUTHORED = os.path.join(HERE, "gacha_authored.json")
# The GENERATED broad catalogue (SW-68, `harness/gen_gacha_catalogue.py`). Identity is shipped; cost/pool/
# art are authored. `GACHA_CATALOGUE=0` disables it; `GACHA_CATALOGUE_LIMIT`/`_OFFSET` select a "rotation"
# window over the id-sorted set (0 = all). Curated `gacha_authored.json` banners OVERRIDE a catalogue id.
GACHA_CATALOGUE = os.path.join(HERE, "gacha_catalogue.json")
GACHA_CATALOGUE_ON = os.environ.get("GACHA_CATALOGUE", "1") == "1"
GACHA_CATALOGUE_LIMIT = int(os.environ.get("GACHA_CATALOGUE_LIMIT", "0"))
GACHA_CATALOGUE_OFFSET = int(os.environ.get("GACHA_CATALOGUE_OFFSET", "0"))
# The COMMUNITY calendar (harness/fetch_gacha_community.py -> server/gacha_community.json): the 2016-2022 Fandom
# wiki calendar with real dates/units/art. A [Community] HINT, not verification (COMMUNITY_RESEARCH.md §1.2).
GACHA_COMMUNITY = os.path.join(HERE, "gacha_community.json")
GACHA_COMMUNITY_ON = os.environ.get("GACHA_COMMUNITY", "1") == "1"
GACHA_COMMUNITY_LIMIT = int(os.environ.get("GACHA_COMMUNITY_LIMIT", "0"))
# A CAMPAIGN-SCOPED view (`harness/gen_gacha_campaigns.py` -> `server/gacha_campaigns.json`): serve the
# evergreen STANDARD set + ONE campaign period's banners instead of all 649 -- which the in-game listing
# cannot present readably. `GACHA_CAMPAIGN=<key>` (e.g. `2019-07`, the 3rd-anniversary month); **UNSET means
# the file's own `default` campaign** (so a harness/client reset cannot silently lose it -- the reason this is
# a file default and not an env var alone); `GACHA_CAMPAIGN=all` (or `0`/`off`) serves everything. The
# selection is a pure id filter over the three sources, so the banners' own fields are unchanged.
# Why: `docs/notes/gacha_campaigns.md`.
GACHA_CAMPAIGN_FILE = os.path.join(HERE, "gacha_campaigns.json")
GACHA_CAMPAIGN = os.environ.get("GACHA_CAMPAIGN", "")
GACHA_CAMPAIGN_OFF = ("all", "0", "off", "none")   # explicit "serve everything"
# A SINGLE-BANNER mode for focused work: serve ONLY the listed gacha ids (`GACHA_ONLY=9100203`, or a comma
# list). `GACHA_ONLY=all|off` disables it. The env var wins; when it is UNSET the ids come from
# `server/gacha_only.txt` (one id per line) so a client/harness reset cannot lose the selection -- the same
# reason the campaign's default lives in a file. This is a triage aid, not content.
GACHA_ONLY = os.environ.get("GACHA_ONLY", "")
GACHA_ONLY_FILE = os.path.join(HERE, "gacha_only.txt")
# The STANDARD FAMILIES (`harness/gen_gacha_standard.py` -> `server/gacha_standard.json`): the static,
# never-campaign-tied FREE-tab summons (Arena / EXVIUS / Raid / 5+1 Ticket). Their ids are text-only (none
# ships a `F_GACHA_DETAIL_EXTRA_MST` pair), so the file is authored with a shipped name. `GACHA_STANDARD=1`
# merges them into `all_banners()` ON TOP of the campaign slice -- a standard banner is evergreen and must not
# be filtered out by a campaign. Default OFF, so an unset env leaves the served set untouched.
# Why: `docs/notes/gacha_campaigns.md` SS7.
GACHA_STANDARD_FILE = os.path.join(HERE, "gacha_standard.json")
GACHA_STANDARD_ON = os.environ.get("GACHA_STANDARD", "0") == "1"
# The multi-button layout. `"0"` suppresses the SECOND `GachaDetailMst` row (`exeType=2`) even where a banner
# authors `secondary_offer`. `"swap"` is the DIAGNOSTIC: it exchanges the two rows' `exeType`, so if the client
# honours `exeType` the single button's price changes to `secondary_offer`'s (5000) and the multi button's to the
# banner's (500). A price that does NOT move means the client is not reading our `ijEey5f7` as `exeType`.
# Unset = honour each banner. Mechanism + proof: `docs/notes/gacha_multi_button.md`.
GACHA_MULTI_BTN = os.environ.get("GACHA_MULTI_BTN", "")
# --- the authored banner layer (G1, 2026-09-20) -----------------------------------------------------
# A banner's SEMANTIC names in `gacha_authored.json` -> the client's numeric codes. Every code below is
# from the recovered maps (`docs/notes/gacha_reconstruction.md` SS3-SS4 + the cost/type pass): cost kinds
# via `GameUtils::getItemNum`'s jump table (10 unit / 20 item / 50 lapis / 51 friend point / 52 exvius
# point), `GachaDetailMst.exeType` (+0x68: 1 primary row, 2 secondary). CALIBRATED 2026-09-21 against the
# recovered tab classifier (`docs/notes/gacha_banner_types.md` SS1-2): `GachaMst.type` picks the TAB --
# FREE = {0,6,900}, LIMITED = {50,7}, EXCHANGE = {8,6}, PAY = everything else. So `normal` MUST NOT be 0
# (that is the FREE tab); `normal` = 1 (a plain pay banner), `friend`/`free` = 0, `limited` = 50,
# `box` = 7, `exchange` = 8, `step` = 5 (a step-up is a PAY-tab banner).
GACHA_TYPE_CODE = {"normal": 1, "free": 0, "friend": 0, "step": 5, "box": 7, "exchange": 8,
                   "limited": 50}
GACHA_EXE_TYPE = {"primary": 1, "normal": 1, "friend": 1, "secondary": 2}
# Cost kinds are NOT defined here -- the ONE table is `player_state.COST_KINDS` (docs/notes/currency_model.md).
GACHA_TARGET_TYPE = {"unit": 10, "item": 20, "materia": 22, "vision_card": 27}
# Per-10000 integer for `targetInfo`'s rate slot. The ENCODING is not recovered -- the SERVER rolls, so
# the client never needs it to produce a result (`GachaExeRequest::createBody` sends only UserState ints).
GACHA_RATE_SCALE = int(os.environ.get("GACHA_RATE_SCALE", "10000"))
# The banner IMAGE the top-list button loads. `GachaTopScene::setListLayout` (0x2120184) calls
# `GachaScheduleMst::getBannerInfo()` (0x1cf1a30 -> `+0x60`) and feeds it to `GameSprite::init(Texture2D*)`;
# if that std::string is EMPTY the whole button creation is skipped (0x21208ac `cbz x8, 0x2120a74`), which is
# why an empty `4yT7FUqj` rendered no banner. `GachaScheduleMst::setBannerInfo` (0x1cf1560) splits
# `4yT7FUqj` on `,` and each entry on `:`: a 2-field `int:string` entry fills the vector at `+0x78`, but a
# 1-field BARE string assigns `+0x60` -- the field the list reads. So the served value is a bare filename.
GACHA_DEFAULT_BANNER_ART = os.environ.get("GACHA_DEFAULT_BANNER_ART",
                                          "generalpu_bn_gacha1_blackfriday.png")
# The banner's ANIME (SS5/SSBP) resource, `GachaMst.g38Pjk10` (+0x58, `setAnimeFileName`). Inside
# `GachaDetailScene::setLayout` the client does `addResourceWithKey("gacha_moogle_anime", <this> + ".ssbp")`
# (0x214118c/0x21411b4). If it is EMPTY the path becomes ".ssbp", `ss::ResourceManager` cannot resolve it,
# and the SS5 player null-derefs (si_addr=0xc) -- the 2026-09-20 detail-screen crash. The shipped SSBP is
# `cpk_extract/common_lang/gacha/Ver31_gachaeffect1_common/gacha_animation.ssbp`, and libgame.so carries the
# literal `gacha_animation.ssbp` (0x2f8fae1), so the base name is the value below.
GACHA_DEFAULT_ANIME_FILE = os.environ.get("GACHA_DEFAULT_ANIME_FILE", "gacha_animation")
# The SSBP ANIMATION KEY, `GachaMst.JDFMe3A7` (+0x70). `SS5PlayerList::addPlayer` (0x1ee6a54) does
# `Player::setData(<key>)` then `Player::play(<JDFMe3A7>)` (0x1ee6ab8); `Player::play` (0x1ee0fb8) does a RAW
# `map.find(<JDFMe3A7>)` in the AnimeCache map at +0x28 and a MISS is silent (0x1ee1050).
# **That map is keyed `"<packName>/<animationName>"`** -- `AnimeCache::init` (0x1ee55e0) emplaces
# `StringUtils::format("%s/%s", packName, animName)` (0x1ee5794). Every shipped gacha SSBP names its single
# animation **`anime`**, so the key is e.g. `gacha_moogle_normal/anime`; serving only the PACK name
# (`gacha_moogle_normal`) is a silent miss and the moogle never draws (fixed 2026-09-21). Cross-checked against
# the SHIPPED `F_ANIMATION_MST` (row 90001): `gacha_animation.ssbp#gacha_animation,gacha_moogle_rare/anime`.
# `gacha_animation.ssbp` provides the packs `gacha_moogle_normal` and `gacha_moogle_rare`.
GACHA_DEFAULT_ANIME_NAME = os.environ.get("GACHA_DEFAULT_ANIME_NAME", "gacha_moogle_normal/anime")
# The gacha screen's BGM. `GachaMst.9ZCEWn0z`/`wM9AfX6I` (+0x94/+0x98); `GachaDetailMst` mirrors them at
# +0x120/+0x124. `GachaDetailScene/PayDetailScene::getBgmMapResourceId` reads the DETAIL row first, else the
# Mst's +0x94 -- we sent NEITHER, so it resolved to 0 and the BGM did not resume after a summon. The id is a
# `F_RESOURCE_MST` id of kind `sound/bgm`; 400003 = `la003_mypage_normal1.acb` (My Page / home).
# The gacha screen's BGM. `GachaDetailScene::initialize` (0x2138a38) starts a TEMPORARY adx player whenever
# `getBgmMapResourceId() >= 1`, and `stopGachaBgm()` (0x2137ec8) calls `LapisSoundPlayer::restartBgm()` on exit --
# so ANY value here replays that track from 0 on every detail entry/exit. The base home track is 400003
# (`la003_mypage_normal1.acb`), the SAME piano HOME plays, so serving it is an audible changeover over an
# identical song. Default is therefore NONE: the home BGM simply continues, and `stopGachaBgm()` early-returns.
# Author `bgm` (+ optional `bgm_version`) per banner only where the campaign really had its own track --
# `F_RESOURCE_MST` kind `sound/bgm`; that mapping was server-side and is in NO shipped file.
# Evidence + the full 79-track catalogue: `docs/notes/gacha_banner_bgm.md`.
GACHA_BGM_RES_ID = os.environ.get("GACHA_BGM_RES_ID", "")
GACHA_BGM_RES_VERSION = os.environ.get("GACHA_BGM_RES_VERSION", "3")
# Which art LOCALE to prefer when a banner ships BOTH a default (JP/`common_lang`) and an English (`*_en`)
# file. `en` (default) prefers the English file when present, else falls back to the default; the banner's
# `region` metadata records which locales actually EXIST. See `gacha_banner_draw_path.md` §12.
GACHA_LANG = os.environ.get("GACHA_LANG", "en")
# The requestIDs whose reply must carry the banner groups: entering the gacha sends `GachaEntryRequest`
# (rj6dxU9w); opening a banner detail sends `GachaInfoRequest` (UNP1GR5n).
GACHA_INFO_REPLIES = ("rj6dxU9w", "UNP1GR5n")
GACHA_MST_TAG = "PcUrGH13"        # GachaMstResponse
GACHA_DETAIL_TAG = "IC0ueV5s"     # GachaDetailMstResponse
GACHA_SCHEDULE_TAG = "4D5k8dpI"   # GachaScheduleMstResponse
GACHA_PANEL_TAG = "IF4BRuh5"      # GachaPanelMstResponse
GACHA_PANEL_GROUP_TAG = "MjBKwG37"  # GachaPanelGroupMstResponse (the pool)
GACHA_USER_INFO_TAG = "80nGg32w"   # UserGachaInfoResponse (the player's per-banner state)
GACHA_STEP_INFO_TAG = "7k0FbQ8B"   # UserGachaStepInfoResponse (a step-up banner's current step)
# The SHIPPED challenge table, so the credited amount is the game's own number (see
# challenge_rewards_by_mission). `dX6cor8j` = `ChallengeMst::setRewardInfo`.
CHALLENGE_MST = os.path.join(data_paths.MST_DIR, "F_CHALLENGE_MST.ndjson")
LAPIS_AWARD = os.environ.get("LAPIS_AWARD", "1") == "1"        # credit a mission's challenge reward
LAPIS_REWARD_KIND = os.environ.get("LAPIS_REWARD_KIND", "50")  # see credit_mission_lapis
# The rank-up bonus lapis, served as `GfQmi4h5` (`MissionResultInfoBase::setLevelupBonusDiaNum`) on the
# mission-end reply whenever a battle raises the rank. The token pairing is READ, not inferred:
# `0x1979468 cbz w0, #0x1979b40` selects the block whose call is `0x1979b48 bl setLevelupBonusDiaNum`, and
# the literal that reaches it is `GfQmi4h5` (`0x197945c add x1,x1,#0x81b`, 0x2f2881b). The AMOUNT is
# AUTHORED: the client taking this from the reply is what proves the server sends it, and no shipped table
# carries it (`F_RANKUP_REWARD_MST` is a single row rewarding important item `23:100:10`, not lapis).
# 100 per rank is the CLIENT'S OWN NUMBER, read off its Rank Up help screen (2026-09-22): "When you rank up,
# your maximum energy and friend limit increase, and your energy will be fully restored. You will also obtain
# 100 lapis." Capture: `out/acceptance/rankup_help_2026-09-22.png` -- cite the screen, because the string is
# NOT in `text_decoded/` (it ships in a table we have not decoded, or in a CDN pack; not chased).
# `RANKUP_BONUS_LAPIS=0` disables both the token and the credit.
RANKUP_BONUS_LAPIS = int(os.environ.get("RANKUP_BONUS_LAPIS", "100"))
# Serve `qdhWF7z9` (`UserChallengeInfo`'s clear info) from the PAID set instead of a static list of all four
# challenge ids. CLEAR_INFO=0 restores that older payload for an A/B -- but then the reply claims every
# challenge is already cleared while the balance is separately paid, i.e. the two contradict each other.
CLEAR_INFO_FROM_STATE = os.environ.get("CLEAR_INFO", "1") == "1"
# Serve the cumulative cleared-challenge set on the USER-STATE replies too, not just the mission-end reply
# (see `serve_challenge_clear_all`). One variable per run: CHALLENGE_INFO_ALL=0 restores the old behaviour,
# where a client that restarted could never relearn which challenges it had cleared.
CHALLENGE_INFO_ALL = os.environ.get("CHALLENGE_INFO_ALL", "1") == "1"
# WHICH replies may carry that injected group. Deliberately NARROW -- see serve_challenge_clear_all.
# CHALLENGE_INFO_REPLIES="*" restores the broad "any reply carrying a USER_STATE_GROUPS member" behaviour
# for an A/B, or give a comma-separated reqID list to override.
CHALLENGE_INFO_ANY = os.environ.get("CHALLENGE_INFO_REPLIES") == "*"
CHALLENGE_INFO_ALLOW = tuple(x for x in (os.environ.get("CHALLENGE_INFO_REPLIES") or "").split(",") if x) or (
    "75fYdNxq",   # InitializeRequest            -- the login chain the client walks at every launch
    "X07iYtp5",   # GetUserInfoRequest
    "2eK5Vkr8",   # GetUserInfo2Request
    "4rjw5pnv",   # GetUserInfo3Request
    "lEHBdOEf",   # GetBackgroundDownloadInfoRequest
    "OTX6Fmvu",   # GameSettingRequest
    "P6pTz4WA",   # CreateUserRequest
    "aQm3zP2c",   # UpdateDeviceIdRequest
    "Daud71Hn",   # RoutineHomeUpdateRequest     -- the HOME refresh
    "6H1R9WID",   # RoutineWorldUpdateRequest    -- the map's mission nodes
    "4kA1Ne05",   # RoutineEventUpdateRequest
    "jnw49dUq",   # DungeonResourceLoadMstListRequest -- the mission-list screen's own request
)

# Mirroring this battle's roll into the result screen's drop channel is OFF by default.
#
# Two attempts to enable it both produced a BLACK SCREEN with the client faulting BEFORE ResultSummaryScene
# was built (the frida instrument shows zero hooks firing past arming, and the app's crash reporter loops).
# The cause is not established: the first attempt blanked item channels (reverted), the second handed 4-field
# wave-format items to `addItem` (also reverted, by stripping the rate to 3 fields -- which still faulted).
# So the mirror is now an explicit, reversible switch and the default is the LAST KNOWN-GOOD behaviour.
# Do not re-enable it until `addItem`'s csv parser has been READ (MissionResultInfoBase::addItemEiiPKc)
# rather than inferred from a sibling. The `9BetM2Ds` roll is unaffected and stays on: that channel
# demonstrably works, and it is what made the in-battle drops vary.
MIRROR_DROPS = os.environ.get("MIRROR_DROPS") == "1"

# SW-93(b) A/B -- do the `09HRWXDf` addItem columns change what `ResultSummaryScene` shows?
# `mission_end_progression.md` S9.5 records that S9.3's "these tokens are inert for this screen" was TOO
# STRONG: removing them made the result screen *briefer* (a section that had been present -- a listing of
# possible drops -- went away), yet their item icons never rendered. So they are only PARTIALLY load-bearing
# and the render half was never measured under a controlled A/B. `RESULT_ITEM_COLUMNS=0` strips the six
# `RESULT_ITEM_TOKENS` from every `09HRWXDf` row; the default 1 leaves the known-good payload untouched.
# One variable per run: capture the result summary with the columns present vs omitted.
RESULT_ITEM_COLUMNS = os.environ.get("RESULT_ITEM_COLUMNS", "1") == "1"

# --- the BATTLE BGM A/B (SW-105) ------------------------------------------------------------------
# `MissionPhaseMst.YS9kfCX7` (`+0x88`, `BgmResourceID`) is the ONLY BGM cue we serve: 400005 on the normal
# phases, 400006 on the boss phase (`F_RESOURCE_MST` -> la005_battle1.acb / la006_battle_normalboss.acb).
# `BattleScene::initBattle` `0x1e41314` queues it through `SoundRequestList::playLocalBgm`, and the queue's
# `SoundRequest::process` `0x1f1e1f8` runs `LapisSoundPlayer::stopBgm(true)` -- stop AND SAVE the playing
# track -- before starting the queued one. The client's HOME BGM is fire-and-forget (`HomeScene::
# onSceneVisible` does not re-arm it), so if the post-battle restore is missed the music stays stopped.
#
# `BATTLE_BGM=0` omits the token from every served phase row, which makes that cue the ONE variable in a
# run: fight one battle and listen. Music survives -> the battle cue is the cause and the fault is the
# stop/restore around it. Music still stops -> the cause is elsewhere (the trace on 2026-09-24 ruled out
# both the unit-detail path, which has no BGM call at all, and the missing `MeF3Z05E` home schedule, whose
# empty case plays a PRESENT local fallback -- see `docs/notes/gacha_banner_bgm.md` §7).
# Default 1 = serve the payload as authored. One variable per run.
BATTLE_BGM = os.environ.get("BATTLE_BGM", "1") == "1"
PHASE_TAG = "an6TB4WS"                     # MissionPhaseMstResponse rows
PHASE_BGM_TOKEN = "YS9kfCX7"              # MissionPhaseMst BgmResourceID (+0x88)

# The 9BetM2Ds roll is also switchable, and for the same reason: it is the one remaining delta between the
# payload and the last state that produced results. Turn it off to serve the payload verbatim -- the
# reference state -- and re-enable one variable at a time.
#
# Why this flag exists at all: five attempts at this fault each changed MORE THAN ONE THING (item-channel
# emptying, the result-screen mirror, the 9BetM2Ds arity, the enemy exp), so a failure could not be
# attributed and every fix was a guess. One variable per run, or the next failure teaches nothing.
# The Dire Rat drop table: `cat:item:count:rate`. It lives HERE and not in the payload because the
# payload must always be safe to serve verbatim -- with the roll off it is exactly what the client
# receives, and a 4-field table in the payload is the shape that caused the original black screen.
# [Community] ExviusReborn lineage; the client never receives this table.
DROP_TABLE = ("20:290050100:1:30,20:290010000:1:30,20:290050200:1:30,20:202000100:1:30,"
              "20:290020100:1:30,20:290020500:1:30,20:202000400:1:30")

# DEFAULT ON since 2026-09-20. The served `9BetM2Ds` is the per-monster CANDIDATE pool (6-8 items at rate 30
# for the shrine mobs, straight from the oracle) and the client DISCARDS the rate, so serving the pool
# verbatim hands the player every candidate at once -- the operator's "4-5 items, where retail is 1-2".
# Rolling it is what the real server did. Measured with the served pools (20000 trials/phase): mean 2.2-2.5
# items, 1-2 items 55-65% of the time. Set ROLL_DROPS=0 only for a deterministic reference state (SW-26).
ROLL_DROPS = os.environ.get("ROLL_DROPS", "1") == "1"
STATE_PERSIST = os.environ.get("STATE", "1") == "1"

# The battle-log group the challenge conditions read from: `2urzfX6d` = `MissionEndChallenge`, emitted
# by `MissionChallengeLog::toJsonGroup()` (0x1b25ec8). Its three use-counters are ABSENT rather than zero
# when unused, because `JsonNode::addParam(const char*, const string&)` (0x1f11bd8) drops an empty string
# unless a per-node flag at `JsonNode+0x30` is set -- which `addNode()` leaves 0 by construction and
# `toJsonGroup` never raises (it contains no non-stack store at all). So "absent <=> empty" for these.
# Verified 2026-09-19 by two independent agents: `docs/notes/audit_challenge_tokens.md`.
CHALLENGE_GROUP = "2urzfX6d"
BATTLE_CLEAR_TOKEN = "69ieJGhD"     # per-wave `waveNum:battleGroupID:playerTurnCnt`
USE_ITEM_TOKEN = "4p6CrcGt"         # addUseItemLog  0x1b1911c
USE_MAGIC_TOKEN = "i1yJ3hmT"        # addUseMagicLog 0x1b19230
USE_LB_TOKEN = "s2rX7g0o"           # addUseLbLog    0x1b19454
# "token absent => the counter is zero" is TRUE by the mechanism above, but in 2242 recorded bodies the
# three tokens are never once PRESENT, so the drop rule explains their absence without witnessing their
# identity (same-lineage name sources only). "Pass iff absent" therefore tends to OVER-credit if a token
# name were wrong, so those two checks stay off until a driven run exhibits the token: run a battle using
# an item and a spell, and confirm `4p6CrcGt` / `i1yJ3hmT` appear. `USE_LB` needs no such witness -- it is
# "present => used", so a wrong name can only under-credit.
ABSENCE_WITNESSED = os.environ.get("ABSENCE_WITNESSED", "0") == "1"



# --- the mission-end SWITCH grant -------------------------------------------------------------------
# `UserSwitchInfoResponse` rows are keyed by switch TYPE and the container is a hex string, **4 bits per
# character, MSB first inside each character** (`isSet` 0x1a8a638; `switch_reply_encoding.md`). The container
# **replaces the whole type**, so every type-101 row in one reply must carry the same value.
#
# This state is what the client's map-event gate reads. `MissionResultBaseScene::exitResult` (`0x2372bcc`) at
# `0x2372cc0` calls `MapEventMstList::getPlayEvent(2, 1, UserState+0x140 /*lastMissionId*/)`; a null return
# (`0x2372ce0 cbz`) just goes back to the map, which is the "no cutscene, no new area" we saw.
# `getPlayEvent` (`0x1c99774`) accepts the mission's `F_MAP_EVENT_MST` row only if every switch in `juA0Z4m7`
# is SET and none in `dIPkNn61` is. For the shrine row those are `10002200` (required) and `10002002`
# (forbidden) -- and `10002200` is mission **1110103's own cleared flag** (`F_MISSION_MST.dIPkNn61`), which a
# STATIC container can never set. That is why the ending cutscene (map event `1001006`, whose content
# `111010102_event.bin` contains no movie at all -- `usm` x0) never fired. Evidence: `story_event_probe.md`.
SWITCH_TYPE_FIELD = "5opcUT9t"    # the switch type
SWITCH_BITS_FIELD = "juA0Z4m7"    # the bit container
SWITCH_TYPE_MISSION = "101"       # the mission switch type
PUSHED_SWITCH_GROUP = "0jqUZuT1"  # update_switch_info_response: the client's granted-switch report
PUSHED_SWITCH_FIELD = "dIPkNn61"  # lexicon: GrantedSwitchId
# Baseline indices, i.e. exactly what the static payload served: 0 = `10002100` (1110101's clear flag, which
# is also 1110103's requirement) and 117 = `10001100` (the deliberate 1110100/FMV skip -- dropping it brings
# the pretutorial movie back). Mission flags accrue on top as missions actually clear.
#
# `SWITCH_BASE_INDICES=0` is the ONE variable that re-exposes the pretutorial FMV (`docs/notes/fmv_pretutorial_brief.md`,
# SW-35): 117 is 1110100's own clear switch (`F_MISSION_MST.dIPkNn61`), and with it unset `HomeScene::getTutoState()`
# (`0x1e48fa4`) returns 0, so `HomeScene::initialize` (`0x1e49988`) auto-starts `MissionStartScene(1110100)` and
# `changeNextScene` (`0x1e622a4`) constructs `CreateUserSpecificMovieScene` -> `op_clip.usm`. The default keeps the
# skip: a fresh account still must not wedge on the movie until it plays.
SWITCH_BASE_INDICES = tuple(
    int(x) for x in os.environ.get("SWITCH_BASE_INDICES", "0,117").split(",") if x.strip()
)
SWITCH_CONTAINER_MIN = 30


# --- the MST version table (VersionInfoResponse, group `c1qYg84Q`) -------------------------------
# A FRESH client has no persisted version table of its own, so this reply group is the ONLY source of the
# per-table versions it will download -- and `MST_LOAD_FAILED` is literally
# `DownloadMstFileList::loadMsts(false) == false`. Serving four rows worked for months only because every
# run inherited a warm `gamedata.xml` AND a warm asset cache from an earlier full download; the first
# `pm clear` (the 2026-09-20 kosher new-user run) deleted both, and the boot died before character
# creation having fetched two tables. Row shape, the `version < 1` drop, and the registered-name gate:
# `docs/notes/mst_version_source.md` SS2.3/SS3.
# Resolved by the data-root contract (env wins verbatim, else `<root>/client/shared_prefs/gamedata.xml`,
# else the checkout-relative default). See data_paths.py.
VERSION_TABLE_XML = data_paths.VERSION_TABLE_XML

# --- FORCE A RE-FETCH of specific tables, so the client asks US for them ---------------------------------
# THE CLASS OF BUG THIS FIXES (two live reports, 2026-09-26). A table the client BELIEVES it has but does not:
#   * the reply advertises the version the shipped registry says, so the client never fetches, so a `--fresh`
#     run whose asset restore skipped the file runs with it MISSING -- and the client has no way to notice.
#     The symptom is always the same shape: the screen's FRAMEWORK draws (layouts and reply-borne rows) while
#     the content that comes OUT of the table is empty. Report 1: the login/title screen rendered raw keys --
#     the main-text table's plain `SG_*`/`BTN_*`/`Pop_Up_*` strings. Report 2: the login-bonus calendar drew
#     but its award display was EMPTY, because EVERY calendar in that family reads its reward ROWS from a
#     shipped MST (`F_LOGIN_BONUS_MST`, `F_LOGIN_BONUS_TOTAL_REWARD_MST`, `F_LOGIN_BONUS_SP_REWARD_MST`,
#     `F_COMEBACK_LOGIN_MST`) while the daily 28-day one is reply-borne and therefore unaffected.
#   * THE TABLES ALL SHIP, at exactly the versions the registry advertises, so the fix needs NO crypto: bump
#     the advertised version, let the client ask for a name that does not exist locally, and let
#     `_cdn_fetch`'s same-token fallback serve the shipped bytes under the token it asked for.
# WHY A BUMP IS ENOUGH: the client resolves a table by NAME -> `getMstFileName()`/`getMstFileKey()` (the
# 8-char token + key), not by the `Ver<N>` it is told; `Ver<N>` only names the file it fetches
# (`docs/notes/mst_version_source.md`). The retired `harness/patch_localized_text_manifest.py` is the same
# trick for a text table, and `F_DEFINE_MST=225` is the same trick, live.
# `NAME=VERSION,...`; `off` disables. Bump a number to force another fetch after a client cache change.
# Homes: docs/notes/title_screen_loc_keys.md, docs/notes/catchup_bonus_family.md.
FORCE_TABLE_VERSIONS = os.environ.get(
    "FORCE_TABLE_VERSIONS",
    # the main-text table (every plain UI key), then the four login-bonus MSTs the calendars read rows from,
    # then the PANEL QUEST family: the quest rows, the group rows (ids + rewards) and the name table are all
    # MST parsers, NOT factory replies, so they ship and are never fetched either -- and without them the
    # screen has no rows and no title even with every reply group served (SW-121).
    "F_TEXT_TEXT_EN=265,F_LOGIN_BONUS_MST=27,F_LOGIN_BONUS_TOTAL_REWARD_MST=12,"
    "F_LOGIN_BONUS_SP_REWARD_MST=297,F_COMEBACK_LOGIN_MST=27,"
    "F_PANEL_QUEST_MST=16,F_PANEL_QUEST_GROUP_MST=14,F_TEXT_PANEL_QUEST_NAME=10,"
    # ...and the ADVENTURE QUEST (regular quest) tables, PRE-EMPTIVELY and NOT because a failure was observed:
    # the mission list is client tables plus the cleared/switch state we already serve, so no reply class is
    # involved and `--gaps` cannot see it -- which is exactly the class of problem no instrument here covers.
    # The evidence is INDIRECT and stated as such: the shipped registry advertises exactly the versions the
    # shipped files carry, so a client that has them has no reason to fetch, and the operator's story missions
    # have run in past sessions (so the table was present then -- by download or by the asset restore, and
    # the run logs that would settle it are gone). FALSIFIABLE TEST: on a `--fresh` run the log will either
    # show these fetches or the mission list will be empty.
    # COST, because one of them is not small: `F_MISSION_MST` is 20.5 MB / 17690 rows, `F_AREA_MST` 785 KB. It
    # is a ONE-TIME fetch per client (the version is then cached), and against a first-run asset download in
    # the GB range it is noise -- but if it ever bites, this list is where to cut it, not the table itself.
    "F_MISSION_MST=525,F_WORLD_MST=35,F_AREA_MST=611,F_MISSION_INCREASE_MST=19,"
    "F_MISSION_SKILL_POINT_MST=5,F_AREA_POS_OVERRIDE_MST=4,"
    # ...and F_DEFINE_MST, because the GL table SHIPS WITHOUT the three BREAK_THROUGH_* rows the survival
    # page reads. Captured live 2026-09-29: `GateScene::setListLayout` -> `DefineMst::getIntValue
    # ("BREAK_THROUGH_POSSIBLE_CNT")` -> a null+0x28 deref (the first fault, seen by gdb before libndk's
    # handler; the visible GL-thread crash is the crash reporter's SECOND fault). UNLIKE every other entry
    # here, the same-token fallback would serve the WRONG (shipped) bytes, so this one REQUIRES the patched
    # copy `harness/derive_define_mst.py` writes to `out/cdn_overlay/mst/Ver227_zg5P1AxM.dat` (the shipped
    # 426 rows + the 3 JP rows + TNC_SWITCH_ID=0). Homes: docs/notes/survival_mode_identification.md SS8 and
    # docs/notes/tos_update_notice_gate.md.
    # VERSION 227 (2026-10-04) -- WHY IT MOVED AGAIN: the 225 build carried the TNC_SWITCH_ID=0 edit that
    # switches the "Terms of Use have been updated" notice off; the 226 regeneration for survival was
    # produced from the shipped table and silently DROPPED that edit, so the notice came back. The script now
    # applies TNC_SWITCH_ID=0 as a DEFAULT, and 227 invalidates the client's cached 226. `derive_define_mst.py
    # --set KEY=VALUE` still rewrites a shipped row (the 2026-10-02 title-gate experiment used it); whatever
    # number this variable advertises MUST match the overlay filename on disk.
    "F_DEFINE_MST=227"
    # ...and F_TEXT_DUNGEON_NAME (APP-7, 2026-09-30), the area-1006 tile's NAME. Token `AyVqkz2B`
    # (`docs/analysis/localized_text_keys.json`), shipped as `Ver223_AyVqkz2B.dat` (247,724 B).
    # WHY A NEW ENTRY IS NEEDED IS NOW PROVEN, not inferred: the client's own shipped registry
    # (`com.square_enix.android_googleplay.FFBEWW/shared_prefs/gamedata.xml`) already carries
    # `<string name="keyF_TEXT_DUNGEON_NAME">223</string>`, and `ffbe_battle.serve_version_table` builds the
    # `c1qYg84Q` rows from that registry -- so the client is told 223, believes it has the table, and
    # never asks. 224 is one above it, which is the same mechanism as every other entry here.
    # The table is COMPLETE as shipped (the key `MST_DUNGEONS_NAME_100601` is in it), so unlike
    # F_DEFINE_MST the same-token fallback serves the correct bytes and NO overlay copy is needed.
    # VERIFIED OFFLINE 2026-09-30 against a scratch server: `GET /localized_texts/en/Ver224_AyVqkz2B.dat
    # -> 200 247724 B (token-match Ver223_AyVqkz2B.dat)`, sha256-IDENTICAL to the shipped file. (The
    # fallback is directory-relative and needs EXACTLY ONE hit or it 404s AMBIGUOUS; there is one,
    # checked.) FALSIFIABLE TEST still owed by the RUN: the client's own fetch must appear in the server
    # log as that same line -- no such line means the client believed it already had the table and the
    # version has to be bumped again (the 225-define trap, note SS8).
    # NOT VERIFIED: the guest's cache could not be listed (the app data dir is mode 700; reading it needs
    # sudo), so "the guest does not have the bytes" rests on the note plus the run's log line, not on a
    # directory listing. WHAT IS PROVEN is the half that matters: the client is ADVERTISING 223 and so
    # has no reason to fetch -- which is the defect, independent of what is on disk.
    ",F_TEXT_DUNGEON_NAME=224")


# --- SERVE the unit row's first-get locator (`Z06cK4Qi`) FROM STATE (the AX unit-ability arms) -----------
# `Z06cK4Qi` is `UnitInfoBase+0x294` (`setFirstGetUnitId` 0x1969038); `getFirstGetRarity` 0x1dd2a88 resolves it
# through `UnitMstList`, so the value is a UNIT ID whose MST row supplies the "granted rarity" the acquire gate
# compares against. Our `harness/response_field_map.json` records the token `served: false`, and the state has
# carried `first_get_unit_id` since `grant_unit` did -- i.e. we hold a field we never project. OFF by default (the
# served row stays byte-for-byte what it was); when ON the value is the state's own field, never a literal.
# The unit's STARS are NOT served here and are not a row value at all: they come from `F_UNIT_MST.9fW0TePj` of the
# unit's own master id, which is why a per-instance rarity cannot be staged (`notes/unit_skill_vector_capture.md`).
SERVE_UNIT_HISTORY = os.environ.get("SERVE_UNIT_HISTORY", "") == "1"

# The NV EX level (`UserUnitInfoResponse` token #21 `f8vk4JrD` -> `+0x550 setNvExLv`, `atoi`). OFF by default
# so a served unit row is byte-for-byte what it was (the golden gate depends on that): when ON the value comes
# from the STATE's `nv_ex_level`, which `--grant-unit all --nv-ex N` seeds. Why it exists: without it every
# unit is presented at EX 0, so the operator's Brave-Shift theory (a shift form needs EX to be switchable)
# cannot be tested at all.
SERVE_NV_EX = os.environ.get("SERVE_NV_EX", "") == "1"


# --- the daily-login calendar (SW-86) -------------------------------------------------------------
# WHICH MONTH is served, and which single banner rides it. The reward SCHEDULES were transmitted by the
# original servers and never shipped (`F_LOGIN_BONUS_MST` holds exactly ONE generic 15-day cycle), so they
# come from the community table `server/login_calendar.json`; every `id` in that file is a SHIPPED id, so the
# client draws its own in-game icon. Defaults pick one of the 21 months whose 28 days all resolved.
LOGIN_MONTH = os.environ.get("LOGIN_MONTH", "2021-Jun")
# The calendar banner is a DOWNLOADED image, not the atlas frame the layout names: the scene asks for
# `<P_GS_BANNER_PATH>/login/<name>` and SKIPS the banner when the file is absent, so a filename is required.
# This is one shipped atlas frame, cropped once into `server/assets/banner_art/` (no per-month rotation).
LOGIN_BANNER = os.environ.get("LOGIN_BANNER", "daily_login_banner_dailylogin_00.png")
# The community table keys months as `YYYY-Mon` (2017-Jan .. 2022-Jan); the reply's `loginMonth` wants the
# calendar number. It only feeds the month-change popup and `setCurrentMonth`, never a cell key.
_MONTH_NO = {m: i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}

# --- the CATCH-UP bonus/gift schedules (the login-bonus family beyond the daily calendar) -------------
# Home: docs/notes/catchup_bonus_family.md. The SCHEDULES are shipped content selected by id
# (`server/catchup_bonus.json`, generated by harness/gen_catchup_bonus.py); the wire groups below are the
# user's own progress in them, which is server-side because the client keeps no count (the same reason the
# daily calendar's `state` is ours: `daily_login_bonus.md` §7.3).
#
# `n5W9Et2I` UserLoginBonusSpInfoResponse -- WHICH special-login campaign is active and how far the player
#   is: `deDr9L46` id, `uJK9ac2V` the day list, `IFQ70DYS`/`c46Ayz8I` the window (readParam 0x191d4f0).
# `Bbc6pF9Q` UserLoginBonusTotalInfoResponse -- the cumulative ladder's AXIS: `1ACjIe9S` = total login days
#   (readParam 0x19284cc). The 29 tiers themselves are the shipped `F_LOGIN_BONUS_TOTAL_REWARD_MST`.
# `yL2jdmM3` OpeLoginBonusUpdateResponse -- `68Vn2FrK` consecutive-day count, `cksL9bW8` the completed-day
#   info, `fHSZ74Qm` the process day (readParam 0x193b068). `HomeScene::checkLoginBonusScene` 0x1e4f3d0
#   and `LoginBonusUtil::getNextLoginBonusSceneId` 0x1abd6a8 read these.
# `In9jMbU7` UserLoginBonusInfoResponse -- `a3ZcU6gv` id + `uJK9ac2V` the day list; read by
#   `UserLoginBonusInfoList::checkLoginBonusUpdated` 0x1db95d2c, which tests today's YYYYMMDD for membership.
CATCHUP_SP_TAG = "n5W9Et2I"
CATCHUP_TOTAL_TAG = "Bbc6pF9Q"
CATCHUP_OPE_TAG = "yL2jdmM3"
CATCHUP_INFO_TAG = "In9jMbU7"
CATCHUP_SP_ID = "deDr9L46"
CATCHUP_DAY_LIST = "uJK9ac2V"
CATCHUP_START_YMD = "IFQ70DYS"
CATCHUP_END_YMD = "c46Ayz8I"
CATCHUP_LOGIN_DAYS = "1ACjIe9S"
CATCHUP_SERIAL = "68Vn2FrK"
CATCHUP_DAY_INFO = "cksL9bW8"
CATCHUP_PROCESS_DAY = "fHSZ74Qm"
CATCHUP_BONUS_ID = "a3ZcU6gv"
# The one request whose reply the family rides. `LoginBonusRequest` (`vw9RP3i4`) is the ONLY `*Bonus*Request`
# in `.dynsym` (daily_login_bonus.md §4) and it is already the reply that opens the calendar, so the user's
# progress in the OTHER schedules rides it too; the user-state groups below are belt-and-braces for a
# client that has not opened the calendar yet. Widening the carrier is a one-line change HERE.
CATCHUP_REQUESTS = ("vw9RP3i4",)
# The one request that refreshes the whole user state, where the same groups are attached when the reply is a
# user-state refresh -- the same rule `serve_diamonds` uses, because these groups are read by HOME.
CATCHUP_USER_STATE = True
# ...with ONE reply excluded, and it is a trap rather than a preference. `RoutineHomeUpdateRespose`
# (tag `QB47tpMF`, served on `Daud71Hn`) DOES carry `LhVz6aD2`, so a "any user-state refresh" rule would put
# `n5W9Et2I` on it -- and its `readParam` (0x195342c) then calls `UserLoginBonusSpInfoList::clearObject()`.
# Every ~30-minute home cycle would wipe the SP list the campaign was built from. `ecK58yUC` is the token
# that triggers the clear and we never send it, but the clear is in the same class, so the group must not
# ride that reply at all. Home: docs/notes/catchup_bonus_family.md §2.
CATCHUP_BLOCKED_REQUESTS = ("Daud71Hn",)   # RoutineHomeUpdateRequest -- clears the SP list on arrival
# `x14KARPJ` = `SpecialLoginBonusMstResponse`: the ONE content reply in the family. It is the campaign's
# header row -- without it `LoginBonusUtil::getSpLoginScene` returns null for the id and the campaign yields
# no scene at all, so the `n5W9Et2I` instance is a pointer to nothing. Read at 0x193b8f0 (catchup note §5).
CATCHUP_SP_MST_TAG = "x14KARPJ"
CATCHUP_SP_TODAY = "xm7c08IS"        # -> +0x24: 1 = open the "today" page, anything else = the full calendar
CATCHUP_SP_TARGET_TYPE = "89EvGKHx"  # -> +0x64 setTargetType
CATCHUP_SP_BANNER = "4yT7FUqj"       # -> +0x30 setBannerInfo (a bare filename, or nothing)
CATCHUP_SP_BG = "IS4s7uYk"           # -> +0x48 setBgImage
# `uJK9ac2V` means a DAY LIST and its SEPARATOR differs per class, which is the trap this comment exists for
# (`RE_VERIFICATION_IDIOMS` #26, one token / different meaning per class):
#   * `UserLoginBonusSpInfo::getSpecialLoginDayList` 0x1abd8e4 splits it on `,` -> the campaign's 1..N days;
#   * `UserLoginBonusInfoList::checkLoginBonusUpdated` 0x1d95d2c splits on `;` and compares each part with
#     TODAY's YYYYMMDD -- a today-GATE. Miss it and the total/weekly pages refuse with LOGIN_BONUS_RETURN_HOME,
#     which the note names as the most likely first-run failure. So the `In9jMbU7` copy is `;`-separated DATES
#     ending with today, and the `n5W9Et2I` copy is the campaign's day numbers.
CATCHUP_DAY_SEP_SP = ","
CATCHUP_DAY_SEP_INFO = ";"
# How many past dates the `In9jMbU7` list carries. It only has to CONTAIN today for the gate, and
# `LoginBonusUtil::getWeeklyLoginCount` (0x1abd9f8) counts its `,` parts -- which a `;`-separated list has
# none of, so the weekly page reads 1. That is a cosmetic miss on a view we do not serve, and it is the price
# of the today-gate, which is the one that blocks the screen.
CATCHUP_INFO_DAYS = 28
# A catch-up grant is announced with the DAILY-LOGIN mail convention, because that is the one mail shape
# proven to render and to be recognised by the client (`isDailyLoginReward()` 0x1d80058: title
# `SG_DAILY_LOGIN_TITLE` + an integer message), and because such a mail is informational -- the reward is
# granted by the server, exactly as the daily calendar's is (mailbox.md §4). The id base is distinct from
# the daily calendar's `900000000000 + day` so the two never collide (`ynP26rjD` is a u64).
CATCHUP_MAIL_ID_BASE = {"welcome": 910000000000, "ladder": 920000000000, "sp": 930000000000}

# --- PANEL QUEST (the PANEL QUEST home icon; SW-121) ---------------------------------------------------
# The screen sends exactly ONE request -- `sgPanelQuestScene::initConnect` 0x24c0ce0 builds
# `sgPanelQuestEntryRequest` and there is no other call site -- and an EMPTY reply is the connection error the
# operator hit. What it needs splits three ways, and the split is the whole job:
#   * SHIPPED MST, never a reply: `sgPanelQuestMstResponse` and `sgPanelQuestGroupMstResponse` are ABSENT from
#     `GameResponseFactory` (they are `XxxMstList::createParser` classes, the same shape as
#     `LoginBonusMstResponse`), so the 162 quest rows, the 19 group rows and the campaign NAMES come from the
#     CDN tables `F_PANEL_QUEST_MST` / `F_PANEL_QUEST_GROUP_MST` / `F_TEXT_PANEL_QUEST_NAME` -- forced in
#     FORCE_TABLE_VERSIONS above, or the screen has no rows and no title.
#   * SERVER, this file: the schedule rows, the per-schedule STATUS and the player's progress.
# `sgPanelQuestScheduleMstList::getActivePanelQuests` 0x1cbbc08 gates on a schedule having **at least one
# status row** (`ldr x9,[x21,#0x30]` -> `cmp w8,#0; b.le` skip, then it sorts the status vector), so
# `PANEL_QUEST_STATUS_TAG` is NOT optional: without it every schedule is filtered out and the screen is empty
# even with the other two groups served. That is the "served but invisible" trap, and it is why the status
# group is emitted first in the docstring order below.
PANEL_QUEST_REQUEST = "GVyjSq16"          # sgPanelQuestEntryRequest, the screen's only request
# Tapping a panel is a CLAIM, and it is a different request: `sgPanelQuestRewardRequest` `6uue8aKx`, whose
# body is the same token the ENTRY request carries (`QxrSeO56` -- absent from the factory, so it builds no
# object: it is request-side). The client's own capture, verbatim:
#     QxrSeO56 = [{"fK3W9qYA":"100", "zF6QIY2r":"10001", "V36ygRYs":"1000101"}]
# i.e. scheduleId, groupId, questId. Answering it is what puts the CROSSED SWORDS on the cell: `QCLs6SV8`
# is *is-received*, which is a different state from `Ag8bisM0` *is-cleared* -- the operator hit this by
# tapping a panel quest that was already ticked (docs/notes/panel_quest.md §4h).
PANEL_QUEST_CLAIM_REQUEST = "6uue8aKx"   # sgPanelQuestRewardRequest
PANEL_QUEST_CLAIM_GROUP = "QxrSeO56"     # request-side: {scheduleId, groupId, questId}[]
PANEL_QUEST_STATUS_TAG = "UvwpPAtp"       # sgPanelQuestStatusResponse -- the activity gate
PANEL_QUEST_SCHEDULE_TAG = "nnR59m0N"     # sgPanelQuestScheduleMstResponse
PANEL_QUEST_INFO_TAG = "ELPwhZj0"         # sgPanelQuestUserQuestInfoResponse (zXJsTmiM = the merge mode)
PQ_SCHEDULE_ID = "fK3W9qYA"               # the shared ScheduleId token (cf. SpChallengeScheduleMst)
PQ_GROUP_ID = "zF6QIY2r"                  # GroupId
PQ_QUEST_ID = "V36ygRYs"                  # the quest row id (ChallengeId in the reply's vocabulary)
PQ_PERSISTENT = "BbnAH74Z"                # -> +0x40 IsPersistent
PQ_DISP_ORDER = "Z0EN6jSh"                # -> +0x60 DispOrder
PQ_START = "VjJQ51uG"                     # -> +0x68 StartDate  (string)
PQ_END = "m8ivD4NX"                       # -> +0x80 EndDate    (string)
PQ_LIMIT = "4P7BaEed"                     # -> +0x98 LimitDate  (string)
PQ_PROGRESS = "oz5Vt6AR"                  # -> +0x30 ProgressValue (int64)
PQ_CLEAR = "Ag8bisM0"                     # -> +0x38 IsClear
PQ_REWARD_FLAG = "QCLs6SV8"               # -> +0x39 RewardFlg (true iff > 0)
PQ_REWARD_COUNT = "es0Yun3Z"              # -> +0x2c RewardCount (claimable rewards)
PQ_IS_NEW = "uX54AqwV"                    # -> +0x30 IsNew (true iff the wire value is exactly "1")
# The window is AUTHORED (no shipped table dates a panel-quest schedule) and its string FORMAT is INFERRED from
# the client's one documented string-time convention (`CommonUtils::convertToTime`, `%Y-%m-%d %H:%M:%S` -- the
# same shape the shipped `F_COMEBACK_LOGIN_MST` rows carry). The three getters are virtual, so a direct-caller
# scan cannot enumerate their consumers (the recorded virtual-getter blind spot): if the period label renders
# wrong, this format is the first thing to change. `medium`.
PANEL_QUEST_WINDOW_FORMAT = "%Y-%m-%d %H:%M:%S"
PANEL_QUEST_WINDOW_DAYS = 365
# The groups served when no season is loaded. **The icon is on by construction** -- its `F_FUNCTION_MST` gate
# ships true, so the tile is visible and tappable in a default run -- which means the SCREEN must work in a
# default run too. Hanging it off the season facet alone was a wiring mistake: `seasons.json` ships
# `"default": "off"`, so `default_events` never applied and the tap kept erroring with no season selected. A
# season's `events.panel_quest_groups` OVERRIDES this list; it does not enable it.
#
# 10001, NOT the first group by number. `F_TEXT_PANEL_QUEST_NAME` names the quests as
# `MST_PANEL_QUEST_NAME_<quest row id>` -- the id VERBATIM, no arithmetic (group 10001's rows are 1000101..1000109,
# which are the keys; the operator's own screenshot reading `MST_PANEL_QUEST_NAME_10011` was group 1001's row id
# and therefore both the rule and the proof that the row is missing). The shipped text table names **10 groups**,
# all 5-digit, and group **1001** is a 4-digit one with NO name rows -- so serving it produces a screen of raw
# keys. 10001 is schedule 100 = "Rookie Panel Quest" / "Start your adventure by clearing these quests!", and its
# nine quests are the new-player onboarding checklist (equip a weapon, go on a quest with a friend, send an
# invite, use a limit burst, defeat Siren, enhance a unit, clear Earth Shrine, summon an esper, clear Fulan Pass),
# which is exactly the catch-up schedule this screen is for.
PANEL_QUEST_DEFAULT_GROUPS = (10001,)
# The groups `F_TEXT_PANEL_QUEST_NAME` actually names, MEASURED by decoding the shipped
# `Ver9_w8GuiUwc.dat` and grouping the `MST_PANEL_QUEST_NAME_*` keys: 90 rows = 10 groups x 9 quests, and the
# group is the key minus its last two digits. A group outside this set serves rows whose names are raw keys, so
# `serve_panel_quest` WARNS rather than serving a screen full of placeholders. Not a rule -- a measurement, and
# the note records how to re-derive it.
PANEL_QUEST_NAMED_GROUPS = ("10001", "10002", "10101", "10102", "10201", "10202",
                            "10301", "10302", "10303", "10304")

# --- SP CHALLENGE (the quest-schedule family: Rookie / ADVANCED / Expert / Parameter Quests) -----------
# THE THIRD QUEST FAMILY, and the one the two permanent schedules live in. `SpChallengeEntryRequest`
# `MTf2j9aK` is a **collided wire id** (shared with `GrandMissionEntry`), so ONE reply serves both halves of
# the vortex portal's quest flow. What is shipped and what is ours, measured:
#   * SHIPPED: the 1,468 challenge rows of `F_SP_CHALLENGE_MST` (client-side, `SpChallengeMstList::createParser`
#     exists) -- ids, group, name, ProgressMax, the `Pzn5h0Ga` condition spec and the `dX6cor8j` reward. The
#     names are `MST_SPCHALLENGE_NAME_<challengeId>` in `F_TEXT_SPCHALLENGE`.
#   * OURS: the SCHEDULE master. There is no schedule table in the GL corpus NOR in the 98 JP/Memorial tables
#     (its absence is re-checked there, against `Ver544_4b6NcpLo.dat` = the JP `F_SP_CHALLENGE_MST`, which is
#     the challenge table and not a schedule master), so `NI12Hvxy` is authored -- with ONE exception below,
#     which is recovered rather than authored.
#
# THE SCHEDULE ID IS NOT THE GROUP ID, and that is measured, not inferred. `SpChallengeScheduleMst::getName`
# (0x1c7a510) reads `+0x24` (`fK3W9qYA`, the schedule id), `IntToString`s it and prepends
# `MST_SPCHALLENGE_SCHEDULE_NAME_` (a 30-byte literal at 0x2f474c3, `insert(0, .., 0x1e)`), then asks
# TextManager. So the schedule's DISPLAY NAME is keyed on the schedule id, and the shipped text table holds
# 115 such keys: `MST_SPCHALLENGE_SCHEDULE_NAME_11011` = "Advanced Quests", `10011` = "Rookie Quests",
# `12011` = "Expert Quests", `40011` = "Parameter Quests". **83 of the 135 schedule groups have a name row at
# `group*10+1`** (the other 32 are event schedules with other id conventions), and all four permanent groups
# are among them. So `SP_NAME_KEY_MULT/SP_NAME_KEY_ADD` is the RECOVERED rule that makes the name resolve:
# serving `fK3W9qYA = 1101` would build `..._1101`, which no row carries, and the screen would show nothing
# where the name should be. The generator applies the rule AND reports any served group whose name key is
# absent (idiom 40: prefer a content id that HAS shipped text, and say so loudly when one does not).
SP_CHALLENGE_REQUEST = "MTf2j9aK"       # SpChallengeEntryRequest (== GrandMissionEntry's wire id)
SP_CHALLENGE_CLAIM_REQUEST = "2G7ZVs4A" # SpChallengeRewardGetRequest -- the CLAIM (tapping a cleared row)
# `SpChallengeRewardGetRequest::createBody` 0x199e10c adds the group `Dtw7GU1j` and, per 12-byte entry in the
# request's vector at +0x30, three ints under three tokens. The values come from
# `SpChallengeListObject::onButtonTouched` 0x253e308, which calls
# `addChallengeInfo(mst->getChallengeId(), mst->getRank(), mst->getChallengeId())` -- and `addChallengeInfo`
# DEDUPS on the triple (0x199e23c compares all three before appending). So the key NAMES are positional
# reuse, not the reply's semantics: `fK3W9qYA` carries the CHALLENGE id here, not a schedule id. There are 0
# captures of this request, so `claim_sp_challenge` identifies the quest by whichever field IS a shipped
# challenge id and refuses the triple otherwise -- fail closed rather than trust a key name (idiom 26).
SP_CHALLENGE_CLAIM_GROUP = "Dtw7GU1j"   # request-side: three ints per node, deduped
SP_CHALLENGE_SCHEDULE_TAG = "NI12Hvxy"  # SpChallengeScheduleMstResponse (one row per schedule)
SP_CHALLENGE_STATUS_TAG = "WS1g0ECn"    # SpChallengeStatusResponse (one row per schedule)
SP_CHALLENGE_INFO_TAG = "3KLU4mSD"      # UserSpChallengeInfoResponse (one row per challenge)
SP_CHALLENGE_TICKER_TAG = "cT7S0ikb"   # SpChallengeTickerInfoResponse -- served, see ffbe_player
SP_SCHEDULE_ID = "fK3W9qYA"             # +0x24 scheduleId
# MEASURED on `SpChallengeScheduleMstResponse::readParam` (0x195491c), twice, and the comment here was wrong
# for two sessions: `zF6QIY2r` lands at **+0x2c** (setGroupId 0x1cb50c0 = `str w1,[x0,#0x2c]`), and +0x28 is
# `dispType`. On the OTHER class, `SpChallengeMst`, the challenge id is +0x24 and the group id +0x28 -- so
# "the same token, a different offset" is exactly what happens here, and the two must not be conflated
# (RE_VERIFICATION_IDIOMS #26).
SP_GROUP_ID = "zF6QIY2r"                # +0x2c groupId ON SpChallengeScheduleMst
SP_CHALLENGE_ID = "V36ygRYs"            # +0x24 challengeId ON SpChallengeMst (not on the schedule row)
SP_DISP_TYPE = "8s9wNSpd"               # +0x28 dispType (byte)
SP_RESET_TYPE = "sa1IipR6"              # +0x30 resetType (byte) -- 1 = daily
SP_PERSISTENT = "BbnAH74Z"              # +0x31 persistent (byte)
SP_SWITCH_INFO = "juA0Z4m7"             # +0x38 switchInfo -- StrToInt'd, so "" is the always-open sentinel
SP_DISP_ORDER = "Z0EN6jSh"              # +0x50 dispOrder
SP_START = "VjJQ51uG"                   # +0x58 startDate
SP_END = "m8ivD4NX"                     # +0x70 endDate
SP_REWARD_LIMIT = "9yEe4dt6"            # +0x88 reward/claim deadline
SP_REWARD_COUNT = "es0Yun3Z"            # +0x2c rewardCount -- the HOME icon badge
# The STATUS LIST is keyed by GROUP id, not by schedule id: `SpChallengeStatusResponse::readParam` stores the
# row with `setObject(info, [x19,#0x2c])` (0x1954838) and `ChallengeData::init` looks it up with the
# schedule's `+0x2c` -- the same field, so one status row per GROUP is what makes the lookup land. (The
# comment in `serve_sp_challenge` used to say "per schedule"; the emission was always right, the reason was
# not.)
SP_IS_NEW = "uX54AqwV"                  # +0x30 isNew
SP_PROGRESS = "oz5Vt6AR"                # +0x30 progressValue (int64)
SP_CLEAR = "Ag8bisM0"                   # +0x38 isClear
SP_REWARD_FLAG = "QCLs6SV8"             # +0x39 isGetReward
# The ticker's two MST text KEYS (`SpChallengeTickerInfoResponse::readParam` 0x19586f0): the schedule name at
# +0x28 and the challenge name at +0x40, each handed to `SpChallengeTicker::getNextTickerLog` (0x2092704) as
# a key TextManager resolves.
SP_TICKER_SCHEDULE_NAME = "LVG65KBQ"
SP_TICKER_CHALLENGE_NAME = "Zmka8t36"
# The schedule NAME key, as the client builds it (see `getName` above).
SP_NAME_KEY_PREFIX = "MST_SPCHALLENGE_SCHEDULE_NAME_"
SP_NAME_KEY_MULT = 10                   # scheduleId = groupId * 10 + SP_NAME_KEY_ADD, when that key ships
SP_NAME_KEY_ADD = 1
SP_NAME_KEY = 10                        # the OTHER shape: 8 of the 91 named schedules key at groupId*10 + 0
# The schedule rows' three flag columns are AUTHORED (no shipped table holds them), and their CONSUMERS are
# measured, so the tab a schedule lands in is derivable rather than guessed (`getCategory` 0x1cb5044):
#   dispType == 3                 -> category 3
#   else resetType != 0           -> category 0        (resetType 1 = daily; `isDailyMission` 0x1cb5008)
#   else persistent == 1          -> category 1
#   else                          -> category 2
# and `SpChallengeTopScene` walks a 4-element table of those categories, read at 0x2fccc54: **{0, 1, 3, 2}**
# (the ints are immediately followed by the `_ZTSN19SpChallengeTopScene` typeinfo string, so the extent is
# certain). `getMstWithCategory(cat)` 0x1cb4c70 drops fully-cleared schedules first, then keeps `cat`. So
# with the values below EVERY permanent schedule lands in the SAME tab (category 1), which is what one
# "Quests" tab holding Rookie/Advanced/Expert/Parameter would look like. The dates are an AUTHORED evergreen
# window in the same format the panel quest uses (no shipped table dates a schedule), and the same `medium`
# caveat applies.
SP_SCHEDULE_WINDOW_FORMAT = "%Y-%m-%d %H:%M:%S"
SP_SCHEDULE_WINDOW_DAYS = 365
# The groups served when no season is loaded. The SP-CHALLENGE home icon ships on (and its badge is this
# family's `SpChallengeStatus::getRewardCount`, `sgHomeIconUtils::setSpChallengeNotif`), so a default run must
# serve something -- same rule as the panel quest (idiom 39).
#   1001 Rookie Quests (31 rows) and 1101 ADVANCED QUESTS (18 rows) -- the two PERMANENT schedules an operator
#   works through in order. 1201 (Expert) and 4001 (Parameter) are deliberately NOT in the default: the point
#   of narrowing was "hide all but a singular quest to get started", and serving four tabs' worth of permanent
#   schedules at once is how nobody can tell which list a fix landed in. 9001 is never a candidate: its 100
#   rows are exactly the table's nameless ones, so it renders as 100 empty rows.
# A season's `events.sp_challenge_groups` OVERRIDES this list; it does not enable it.
SP_CHALLENGE_DEFAULT_GROUPS = (1001, 1101)

# --- SURVIVAL (the 2020 trial gauntlet; the mode's data is server-only) --------------------------------
# The mode cannot be reached from shipped data alone: `SurvivalUtil::isPlayingSurvivalMission(areaId)`
# (0x1a6ee20) is literally `SurvivalEventInfoList::getObjectWithAreaId(areaId) != nullptr`, that list's
# ONLY writer is `SurvivalEventInfoResponse::readParam` (tag `NsC8t4Qr`), and `GateScene::setListLayout`,
# `SealAreaListScene::setLayoutList` and `AreaMapManager::drawDungeon` each ask that predicate for every
# tile they draw. Serving `NsC8t4Qr` for an area is therefore what makes a survival tile EXIST; the tap
# then opens `SurvivalEntryScene`. Evidence, the mode's identity and the row semantics:
# `docs/notes/survival_mode_identification.md` (full wire contract + the subtype-13 census).
# The one DERIVED fact is the GL client's own survival set: area 1006 (`Vault of Hidden Treasures` /
# Defiance of Fate) with two `gQR24kST == 13` missions, `10060201` / `10060301`.
# DEFAULT OFF: everything else here is AUTHORED (the schedule master is not shipped), so a default run
# must serve byte-identical replies. Enable for a run with `--server-env 'SURVIVAL=1'`.
SURVIVAL_ON = os.environ.get("SURVIVAL", "0") == "1"
# THE FAMILY, not an instance. An "area" is the unit of a Survival event family -- e.g. Boss Rush Survival
# is SIX areas (3122/3123/3124 and 3140/3141/3142, tiered BGN/INT/ADV) -- and the server's job is to name
# the areas it wants declared, then let the corpus supply each area's dungeon and first mission. That is
# why the default is still the single 1006: one knob, one default, and `SURVIVAL_AREAS` only ever adds.
SURVIVAL_AREA = os.environ.get("SURVIVAL_AREA", "1006")            # a single area; NOT read by SURVIVAL_AREAS
# THE FAMILY IS THE DEFAULT, and deliberately so. `ffbe_run.sh` starts its OWN server from a preset bundle,
# so any knob passed on a `restart_server.sh` command line is GONE the next time the operator runs the
# harness -- measured 2026-09-30: the family was verified live on one process, the operator reloaded, the
# harness replaced the server, and the six areas silently reverted to the single-area default with nothing
# in any log to say so. A default survives every start path; an env var only survives the one you set it on.
SURVIVAL_AREAS = tuple(x for x in (os.environ.get("SURVIVAL_AREAS")
                                   or "3122,3123,3124,3140,3141,3142").split(",") if x)
SURVIVAL_DUNGEON = os.environ.get("SURVIVAL_DUNGEON", "")      # `U9iHsau3` (the gate row's dungeon);
                                                                   # EMPTY = resolve per area from F_DUNGEON_MST
SURVIVAL_MISSION = os.environ.get("SURVIVAL_MISSION", "")      # `qo3PECw6` (its first mission); same
SURVIVAL_EVENT = os.environ.get("SURVIVAL_EVENT", "11002")         # `pvS5A4kE` = EventId: a SHIPPED
                                                                   # F_SEASON_EVENT_REWARD_MST id, and
                                                                   # (11002, 1) is a pair our own
                                                                   # `ofp9t72b` schedule already serves
SURVIVAL_PAIR = os.environ.get("SURVIVAL_PAIR", "1")               # `L9IAY8iW`, paired with the event id
SURVIVAL_SUMALLY = os.environ.get("SURVIVAL_SUMALLY", "0")         # `L84eYzxv` -> F_SUMALLY_REWARD_MST;
                                                                   # "0" = no reward ladder (a legal empty)
SURVIVAL_BANNER = os.environ.get("SURVIVAL_BANNER", "0")           # `fSxqkF10` -> BannerMstList; "0" = none
# THE TAB SELECTOR, and **1 is correct** — `qnUhs32P` is `SpDungeonCondMst+0x24` (`setEventType`
# 0x1ce4030) and `GateScene::setListLayout` filters the gate rows on it at 0x21970c4:
#     cmp w9,#1 ; csinc w9,w9,wzr,gt ; sub w9,w9,#1      ->  index = qnUhs32P - 1, 0-BASED
# against the tab enum {Event, Enhance, Special, Nemesis, Chronicle}. So 1=Event, 2=Enhance, 3=Special,
# 4=Nemesis, 5=Chronicle.
#
# I set this to 2 on 2026-09-30 and it was a REGRESSION, from a bad inference: area 226's row is 2 and I
# assumed "the tab we were looking at" was therefore 2. The operator's tab census settled it -- area 226
# (Chamber of Experience) is in ENHANCE, and the family belongs in EVENT. So 1 was right all along, and
# the original "no new tiles" symptom was simply looking at Enhance, where only the shipped row lives.
#
# The trap that caused it, worth writing down: the wire value is 1-BASED while the index is 0-BASED, so
# `1` is index 0 -- and a comment calling `1` "the Event tab" makes `2` LOOK like it should also be Event.
SURVIVAL_EVENT_TYPE = os.environ.get("SURVIVAL_EVENT_TYPE", "1")   # 1-based tab number; 1 = Event
# THE BANNER ART, and it is per-AREA in the shipped data. Six Boss Rush banners exist, two event dates x
# three tiers: spd_{20200410,20200511}_bn_list_bossrush_{01,02,03}.png, which is exactly the family's
# shape (two runs of BGN/INT/ADV). `SURVIVAL_BANNER_ART` was `spd_bn_list_00003.png` -- which is
# BYTE-IDENTICAL to area 226's own art (sha256 1f7a18db...), so six family tiles wore the *Chamber of
# Experience* banner, 226's wore the same image, and the two were indistinguishable on screen. That one
# value produced THREE reported symptoms at once: a duplicated experience banner, a family that looked
# like the experience event, and -- because the tiles could not be told apart -- a tap that opened the
# wrong battle. Fixed, and gated (the art must not equal any SHIPPED row's art).
SURVIVAL_BANNER_ART = os.environ.get("SURVIVAL_BANNER_ART", "spd_20200511_bn_list_bossrush_01.png")
# Per-area overrides, e.g. SURVIVAL_BANNER_ART_3123=spd_20200511_bn_list_bossrush_02.png. The tier art is
# derivable in principle (run 1 = 20200410, run 2 = 20200511; _01/_02/_03 = BGN/INT/ADV) but WHICH run got
# WHICH date is not recorded anywhere shipped, so it stays a per-area knob rather than a guess.
SURVIVAL_BANNER_ART_BY_AREA = {
    k.split("=", 1)[0].rsplit("_", 1)[-1]: k.split("=", 1)[1]
    for k in os.environ.get("SURVIVAL_BANNER_ART_BY_AREA", "").split(",") if "=" in k
}
SURVIVAL_WINDOW_START = os.environ.get("SURVIVAL_WINDOW_START", "1577836800")          # 2020-01-01Z
SURVIVAL_WINDOW_END = os.environ.get("SURVIVAL_WINDOW_END", "4102444800")              # 2100-01-01Z
SURVIVAL_TAG = "NsC8t4Qr"                # SurvivalEventInfoResponse (DESTRUCTIVE list)
SURVIVAL_USER_TAG = "65cMLkuQ"           # UserSurvivalInfoResponse (DESTRUCTIVE list)
# `Z0EN6jSh` is the event row's +0x100, and `SurvivalUtil::getAreaIdWithBannerTouch` (0x1a6fa00) reads
# it as a 1/2 SELECTOR, not a display order: `== 1` is the only path that assigns the candidate area
# (`w20 = element->uv60hgDW`), and `== 2` assigns the fallback (`w19`). With anything else the loop
# skips both (`cmp w9,#1 ; b.ne`) and the function returns 0. The ledger's "DispOrder" label is
# therefore incomplete for this consumer; 1 is the value that names the banner-touch area.
SURVIVAL_EVENT_KIND = os.environ.get("SURVIVAL_EVENT_KIND", "1")
# `UserBreakThroughProgressInfoResponse` (DESTRUCTIVE list) -- a SECOND route to the mode's area, and NOT
# the gate it was first taken for. `getAreaIdWithBannerTouch` (0x1a6fa00) reaches the area id by an early
# `mov w0, w20 ; b <epilogue>` (0x1a6fb04) taken only when `w22 == 0`, i.e. only when
# `UserBreakThroughProgressInfoList::getObjectWithAreaId(area)` resolves -- but the FALL-THROUGH path
# returns the same `w20` with an EMPTY break list, so `Z0EN6jSh = 1` alone already names the area and this
# reply is belt-and-braces, not the gate (note SS10.4b).
# THE LOOKUP IS TWO-HOP AND THE PAIR IS SWAPPED, which is what made the first attempt inert: the list is
# reached THROUGH the area (`SurvivalEventInfoList::getObjectWithAreaId`, 0x1db8e84) and then matched on
# `break(+0x28) == event(+0x24)` and `break(+0x24) == event(+0x28)` (0x1db8ec8 / 0x1db8ed4). So this row's
# `L9IAY8iW` (@+0x24) must be the EVENT row's `L9IAY8iW` -- `SURVIVAL_PAIR` -- and NOT the area. `IRca7hx9`
# (@+0x2c) is the fallback's `w21` gate (> 0); `hN57axnr` (@+0x40) the early-return gate (!= 0).
SURVIVAL_BREAK_TAG = "in6Fpa2y"         # UserBreakThroughProgressInfoResponse (DESTRUCTIVE list)
SURVIVAL_BREAK_PROGRESS = os.environ.get("SURVIVAL_BREAK_PROGRESS", "1")  # `IRca7hx9` (+0x2c) > 0
SURVIVAL_BREAK_GATE = os.environ.get("SURVIVAL_BREAK_GATE", "1")          # `hN57axnr` (+0x40) != 0
# Its own carrier set, for the same reason `SURVIVAL_COND_CARRIERS` exists: the list clears on arrival,
# so a carrier that replaced a longer list with our one row would delete the others'.
SURVIVAL_BREAK_CARRIERS = tuple(x for x in (os.environ.get("SURVIVAL_BREAK_CARRIERS") or
                                           "6H1R9WID,cH9DnW03").split(",") if x)
SURVIVAL_COND_TAG = "0xkoNP8U"           # SpDungeonCondMstResponse (shared with the gate rows)
# Which GROUPS to serve -- for the bisect as much as for A/B: the reference behaviour needs the event list
# (it is the predicate `isPlayingSurvivalMission` reads) AND a gate row (it is what puts a tile on the
# screen), so "all" reproduces, "event" isolates the list from the tile, "gate" isolates the tile from the
# predicate. `SURVIVAL_PARTS=event,gate` etc.; unknown words are reported by the step, never silently dropped.
SURVIVAL_PARTS = tuple(x for x in (os.environ.get("SURVIVAL_PARTS") or "event,user,gate,break,state").split(",") if x)
# Which replies carry the rows: the two carriers the gate rows already ride (boot + the scene's own
# routine world update), the vortex entry (`MTf2j9aK`, shared with SpChallengeEntry), and the entry
# scene's OWN request (`cH9DnW03` = SurvivalMissionEntryRequest) -- its reply has no `responses.json`
# spec, so the user-state projection builds the body (the SW-112 "minimal ack" pattern) and these lists
# ride on top. Without it the tap would get an empty body, i.e. the connection error this project has
# already paid for twice.
SURVIVAL_CARRIERS = tuple(x for x in (os.environ.get("SURVIVAL_CARRIERS") or "").split(",") if x) or \
    ("lEHBdOEf", "6H1R9WID", "MTf2j9aK", "cH9DnW03")
# ...and the GATE row only where the `0xkoNP8U` list already rides, because that list is replace-on-read:
# a carrier that drops rows another carrier served would DELETE those tiles on the next arrival.
# --- SURVIVAL PARTY STATE (the `2w9Iyeaq` block; the party carried across floors) -------------------
# The mode's one remaining unserved reply, and the ONLY genuinely large piece of APP-8. Decoded from
# the client by reading the packer AND the parser, which mirror each other:
#   packer  SurvivalBattleUnitStateList::createSendInfoEv  0x1db69c0 (.dynsym size 1624)
#   parser  SurvivalBattleUnitStateResponse::readParam     0x192a84c (656 B)
#              -> SurvivalBattleUnitState::setUnitStateInfo 0x1db7694 (1196 B)
#              -> GenericDictionary<int, SurvivalBattleUnitState*>
#              -> SurvivalPlayerParty::entry                0x1ae4f98
#              -> SurvivalBattleUnitStateList::setUnitState 0x1db7018 -> BattleUnit::setHpEx/setMpEx/...
# FORMAT (one flat string, no escaping; the only delimiters are `@`, `|` and `,`):
#   `<version>``@``<summonGauge>` ( `@` <unitBlock> )*
#   `<unitBlock>` = `<pos>`|`<hp>`|`<mp>`|`<lb>`|`<badStates>`|`<limitBulletCsv>`|`<skillIntervalCsv>`|`<braveShiftCsv>`|`<buffCsv>`
# NINE unit fields, EIGHT pipes. The separators are named in .dynsym, not guessed:
#   SurvivalBattleUnitState::SEND_DATA_SEPARATOR_BLOCK = `@`   (GOT 0x3974990)
#   SurvivalBattleUnitState::SEND_DATA_SEPARATOR_VALUE = `|`   (GOT 0x3978570)
#   the bad-state list splits on `,` (inline literal 0x2f2d200)
# and they are the ONLY two in the binary: a 7,238,543-instruction sweep for the two GOT addresses
# returns exactly three sites -- the packer (x2) and the parser (x1).
#
# THREE HARD REQUIREMENTS the parser imposes, each of which fails SILENTLY:
#   1. the value must not be EMPTY (`strlen`==0 -> readParam stores nothing), and
#   2. it needs at least TWO `@`-blocks or only the version is read (`cmp w8,#1 ; b.lt 0x192a90c`),
#   3. `<pos>` MUST equal the unit's live `BattleUnit::getPartyPosition()`: readParam files each
#      state under a dictionary keyed on `parts[0]`, and `entry()` looks it up by
#      `getPartyPosition()` and `cbz`-skips the unit on a miss. A wrong position therefore
#      restores nothing, with no log line anywhere.
# The parser does NOT validate `version`, and the three CSV fields are assigned opaquely, so they
# are served EMPTY -- which is also what the client emits for a unit with no limit bullets, no skill
# intervals, no brave shift and no buffs. `StrToInt` is called UNGUARDED on fields 0-3, so those
# are plain decimals; the bad-state list is separately guarded and may be empty.
# DESTRUCTIVE **AND UN-GATED** (`onReadStarted` -> `SurvivalBattleUnitStateList::clearInfo()` with
# no flag check), i.e. strictly riskier than `in6fpa2y`: every arrival clears and repopulates, so it
# rides `SURVIVAL_STATE_CARRIERS` only.
SURVIVAL_STATE_TAG = "2w9Iyeaq"          # SurvivalBattleUnitStateResponse (DESTRUCTIVE, UN-GATED)
SURVIVAL_STATE_TOKEN = "3KJpzuC5"        # its one and only token
SURVIVAL_STATE_VERSION = "1"             # SurvivalBattleUnitStateList::SEND_DATA_FORMAT_VERSION (0x2f561c4)
SURVIVAL_SUMMON_GAUGE = "0"              # <summonGauge>; AUTHORED -- no shipped table carries it
SURVIVAL_STATE_CARRIERS = tuple(x for x in (os.environ.get("SURVIVAL_STATE_CARRIERS") or "6H1R9WID,cH9DnW03").split(",") if x)

SURVIVAL_COND_CARRIERS = ("lEHBdOEf", "6H1R9WID")
# --- the EVENT GATE: a STORY-EVENT area's tile in the gate/dungeon list ------------------------------
# The same mechanism as the survival family -- `docs/notes/chamber_battle_authoring.md` section 6, "a new
# area needs its `0xkoNP8U` row first" -- for a story event instead of a survival mode. The row's
# area/dungeon/first-mission are DERIVED from the shipped tables (naming an area is enough, which is the
# point: `_event_area_facts` reads them, nobody types them); the window, the tab, the art and the currency
# are AUTHORED. Env: `EVENT_GATE_AREAS=970,9935` (empty = off), `EVENT_GATE_TAB` (1-BASED: 1 = Event).
# The default is the vortex Event tab as it stands: 970 (Fundamental Forces) and 888 (Edel the Mole,
# area-name text EN via F_TEXT_AREA_NAME) -- one word per event, and the tile's dungeon + first mission are
# derived per area, so a new event is one more word here.
EVENT_GATE_AREAS = tuple(x for x in (os.environ.get("EVENT_GATE_AREAS") or "970,888").split(",") if x)
EVENT_GATE_TAB = os.environ.get("EVENT_GATE_TAB", "1")     # `qnUhs32P`, 1-BASED: 1 = Event tab
EVENT_GATE_KIND = os.environ.get("EVENT_GATE_KIND", "1")   # `Z0EN6jSh` -- the +0x100 1/2 selector
# The art. MEASURED 2026-10-12, after the operator reported the tile wearing area 226's banner: the tile
# art a STORY EVENT used was never shipped. No gate row exists for one (the only shipped rows are 226's and
# 899's), the numbered list arts live in per-event versioned folders and there is none for the
# Fundamental Forces arcs, and the family's own arts in the bundles are GACHA banners
# (cpk_extract/en/banner/Ver328_banner1/generalpu_bn_gacha1_FFBEFundamentalForcesNVPremiumStepup.png and
# friends) -- a different asset family from the area list tiles. So the honest value is the client's OWN
# dummy list plate: it is distinct from every shipped row (so two tiles can never be confused), it comes
# from the common bundle (so it always resolves), and it reads as "no art yet" rather than as another
# event. THE REAL FIX is an overlay: copy the event's banner art into the served CDN under a list-art name
# the client requests and point this at it (docs/notes/limited_events.md section 6).
EVENT_GATE_ART = os.environ.get("EVENT_GATE_ART", "spd_bn_list_dummy.png")
EVENT_GATE_WINDOW_START = os.environ.get("EVENT_GATE_WINDOW_START", "1577836800")   # 2020-01-01Z
EVENT_GATE_WINDOW_END = os.environ.get("EVENT_GATE_WINDOW_END", "4102444800")       # 2100-01-01Z
EVENT_GATE_TAG = "0xkoNP8U"                                # SpDungeonCondMstResponse
# The two carriers the SHIPPED gate rows ride. Appended, never replaced: the list is replace-on-read, so
# only a carrier that already serves it may be written.
EVENT_GATE_CARRIERS = tuple(x for x in (os.environ.get("EVENT_GATE_CARRIERS") or
                                        "lEHBdOEf,6H1R9WID").split(",") if x)
# THE EVENT CURRENCY. A story event pays an event item, and the family's item is shipped
# (`F_ITEM_MST` 310001240 = Fundamental Forces Fragment; 1209002117 = Summon Coin). AUTHORED, and labelled:
# which item, how many, and onto which areas. GL's drop tables are not shipped (the server resolved drops
# live and that code is gone) and the JP oracle's drop ids are JP ids with no item table beside them, so
# there is nothing to derive this from. Format `cat:id:count` comma-joined; empty = off.
EVENT_CURRENCY = os.environ.get("EVENT_CURRENCY", "20:310001240:5")


# --- DAILY QUEST -------------------------------------------------------------------------------
# The one quest family whose CONTENT is not shipped. Measured, in the order that establishes it:
#   * there is NO `F_DAILY_QUEST_MST` -- not in the GL corpus (219 decoded tables) and not in the 98
#     JP/Memorial tables whose tokens match ours 98 for 98 (`docs/notes/jp_gl_mst_diff.md`). The rows are
#     DELIVERED BY A REPLY: `DailyQuestMstResponse`, tag `Hqdgx83H`, `readParam` `0x1944fd8`.
#   * `DailyQuestMstList` has NO `createParser` (the panel-quest MSTs do), so unlike the panel quest this
#     is not a CDN table at all -- it is reply-delivered, and there is nothing to force.
#   * the LABELS are built from the row's ID: `getName` `0x1945404` = `getMstText("MST_DAILY_QUEST_NAME_" +
#     IntToString(*(this+0x24)))` and `getDetails` `0x1945524` is the `_DETAIL_` twin, where `+0x24` is
#     `DailyQuestId` (`setDailyQuestId` `0x19453ac` stores there). The Name/Details WIRE columns are dead:
#     their setters (`0x1945520`, `0x1945640`) are a bare `ret` between two unrelated functions, so
#     `G4L0YIB2` and `56rsqHAp` are accepted and DISCARDED. Consequence: a served id must exist in BOTH
#     shipped text tables or the row shows one real string beside a raw key (idiom 40, the panel quest's
#     `MST_PANEL_QUEST_NAME_10011`). `harness/gen_daily_quests.py` intersects them: 46 ids qualify, 43 in
#     a pool.
#   * it lives on `SpChallengeListScene` -- the SP-challenge / Grand-Mission / comeback screen -- whose
#     entry request is `SpChallengeEntryRequest` (`MTf2j9aK`, shared with `GrandMissionEntryRequest`) and
#     whose ONE handler `SpChallengeListScene::checkConnectResult` `0x2544128` serves all four daily-quest
#     requests. So the rows ride a reply we already send.
DAILY_QUEST_TAG = "Hqdgx83H"             # DailyQuestMstResponse -- the rows AND the progress columns
DAILY_QUEST_INFO_TAG = "sxe5C48P"        # DailyQuestInfoResponse -- the screen's own state (5 tokens)
DQ_ID = "nYD7LvSG"                       # DailyQuestId -- ONE token, three homes: the row id, the info
                                         # response's current id, and the claim request's id
DQ_TYPE = "M9f3P48S"                     # -> setType      (+0x28)
DQ_STATUS = "ZLuw4DLa"                   # -> setStatus    (+0x2c)
DQ_CURRENT = "XCD3VwHk"                  # -> setCurrentNumTries (+0x30)
DQ_TARGET = "97CxqAgk"                   # -> setTargetNumTries  (+0x34)
DQ_LOCATION = "3cEGYLhA"                 # -> setLocation  (+0x38)
DQ_NAME = "G4L0YIB2"                     # -> setName      -- INERT (empty setter); the label is the id
DQ_DETAILS = "56rsqHAp"                  # -> setDetails   -- INERT, same
DQ_REWARD = "dX6cor8j"                   # -> setRewardInfo (+0x40), the one real string column
DQI_SHOW_COMPLETE = "9vmYua2D"           # -> setIsShowCompleteScreen (true iff the value is exactly "1")
DQI_TEAM_LV = "tzCNMJ87"                 # -> parseTeamLvInfo(string); the format is UNKNOWN, so not served
DQI_LEVEL_UP_DIA = "GfQmi4h5"            # -> setLevelUpRewardDia. The SAME token as
                                         # `MissionResultInfoBase::setLevelupBonusDiaNum` (idiom #26: one
                                         # token, two classes, two meanings) -- read as the daily quest's
                                         # own level-up lapis here, and served with the SAME authored
                                         # amount the mission-result rank-up already pays
                                         # (`RANKUP_BONUS_LAPIS`, live-proven 2026-09-22). The popup
                                         # DISPLAYS a reward that was already credited; it does not
                                         # credit it again, which is why the two numbers must agree.
DQI_RANKUP_LAPIS = RANKUP_BONUS_LAPIS
DQI_REMAINING = "L4vx6m3T"               # -> setRemainingTime; a value >= 1 ALSO stamps lastCheckedTime
# The four requests, all answered by `SpChallengeListScene::checkConnectResult` 0x2544128. The claim's body
# is measured, not assumed: `DailyQuestClaimRewardRequest::createBody` 0x1999ef4 emits
#     {"vj87UqEd": [{"nYD7LvSG": "<dailyQuestId>"}]}
# and emits the group EMPTY when the id is 0 -- so the id is the scene's current daily quest
# (`SpDailyQuestObject::onButtonTouched` 0x253f698 reads it from the scene, vtable +0x18).
DAILY_QUEST_CARRIERS = ("MTf2j9aK", "6QYd5Hym", "Zy8fYJ5e", "DCmya9WD", "hW0804Q9")
# THE INFO GROUP RIDES MORE CARRIERS THAN THE ROWS, and the difference is measured rather than tidier.
# `DailyQuestInfoResponse::onReadStarted` `0x1944ef4` is a **bare `ret`** -- it clears nothing (the function at
# `0x1944ef8` is the destructor), so `sxe5C48P` is safe anywhere. The ROWS are the opposite:
# `DailyQuestMstResponse::onReadStarted` `0x1945378` is `ldr w8,[x0,#0x24]; cbz -> removeAllObjects`, i.e.
# DESTRUCTIVE at flag 0, so `Hqdgx83H` stays on the quest-screen carriers only. And it has to: the popup that
# reads the info is pushed by **`HomeScene::updateEvent` `0x1e4d5e4`**, behind a
# `FunctionMstList::isValid("FUNCTION_COMEBACK_QUEST")` check and
# `HomeScene::isComebackQuestGeneralPopupTriggering()`. A run proved the consequence: with the flag forced, the
# popup appeared **from HOME** while the quest screen showed nothing -- so a home refresh has to carry the info
# group or the feature is invisible where it is actually shown. `Daud71Hn` is `RoutineHomeUpdateRequest`.
DAILY_QUEST_INFO_CARRIERS = DAILY_QUEST_CARRIERS + ("Daud71Hn",)
DAILY_QUEST_CLAIM = "Zy8fYJ5e"           # DailyQuestClaimRewardRequest (one quest)
DAILY_QUEST_CLAIM_ALL = "DCmya9WD"       # DailyQuestClaimAllRewardRequest (the confirm dialog)
DAILY_QUEST_CLAIM_GROUP = "vj87UqEd"
# `DAILY_QUEST_INFO_CARRIERS` is defined with the carriers block above (it is that tuple plus the home
# refresh), not here.
# The `Status` ENUM, from the client's own two readers (2026-09-26) -- they agree on the two states that
# matter, and a RUN corrected the consequence I drew from the second one:
#   * `DailyQuestMstList::getUnclaimedRewardCount` 0x1c74db4: `ldr x8,[x8,#0x38]` (vtable call offset
#     +0x38 = `getStatus`) -> `cmp w0,#1` -> `cinc w20,w20,eq`. So **Status == 1 IS the "reward unclaimed"
#     state** -- complete, not yet claimed -- and it is what the unclaimed badge counts. CONSISTENT with the
#     run: the claimed row stopped being counted.
#   * `DailyQuestMstList::getDailyQuest` 0x1c74c58: the same `getStatus` -> `cmp w0,#2` -> `b.eq` skips the
#     `addObject`. **I first read that as "Status 2 rows are EXCLUDED from the drawn list" and wrote it in the
#     note. THE RUN REFUTED IT**: the claimed row is still drawn -- it DARKENS, takes a `CLEAR` stamp and
#     RE-SORTS to the bottom of the list (the operator's own screenshot, 2026-09-26 19:19). So the `b.eq` is
#     a partition/sort step inside a list BUILDER, not a filter on what the screen shows. The mapping below is
#     unchanged and still measured -- 1 = done-unclaimed, 2 = received -- but the visible consequence of 2 is
#     "dimmed and sorted last", which is what the client actually does with it.
# 0 is the residual and the ctor's zero (the ctor zeroes `+0x2c`), i.e. "nothing done yet".
# `DAILY_QUEST_STATUS_MODE` stays as the one-line A/B (`"zero"` serves 0 for everything, honest but inert).
DAILY_QUEST_STATUS_MODE = os.environ.get("DAILY_QUEST_STATUS_MODE", "measured")
DQ_STATUS_INCOMPLETE, DQ_STATUS_COMPLETE, DQ_STATUS_CLAIMED = 0, 1, 2
# `type` (`M9f3P48S`) is the one part of the row with NO OBSERVED EFFECT, and the first reading of it was
# REFUTED by a run. It was read as an index into `sq_quest_type1..5.png` (five frames across two atlases);
# what the operator actually saw is that the single icon on each list row is the REWARD's icon -- a lapis gem
# on the Summoner row (`50:1:150`) and a gold coin on each of the two gil rows (`30:20000:1`): three rows,
# three icons, matching the three authored rewards. So whatever reads `type`, it is not this list row. The
# value is still sent (the field exists; omitting it would be a different guess) and it is `low`/unverified.
#
# The pools served when no season is loaded, and how many of them. The daily quest is REACHABLE BY DEFAULT
# (it is a mode of the SP-challenge list screen, not a gated tile), so the default run must have one --
# idiom 39, which is the panel quest's own lesson. A season's `events.daily_quests` overrides both.
#
# `vortex_gil_summon` is FIRST and deliberately: it is the only default pool holding a row whose action a
# request we can already attribute exactly (`summon` <- `GachaExeRequest` `9fVIioy1`, a bare "Summon a
# unit" with no id to resolve), so a default run has at least one square the player can actually tick. The
# other two (`gifter` = friend gifts, `explorer` = clear exploration quests) have NO located request yet
# (SW-122), so they sit at 0 until one is -- and `serve_daily_quest` says so in the log rather than letting
# an untickable square look broken.
DAILY_QUEST_DEFAULT_POOLS = ("vortex_gil_summon", "gifter", "explorer")
DAILY_QUEST_DEFAULT_COUNT = 3
# `Location` is served 0 and is NOT interpreted. It is a second state column whose meaning nothing in the
# corpus names; serving the honest "unset" beats inventing a value that would filter the row out of an
# unknown predicate. OPEN: what reads it.
DAILY_QUEST_LOCATION = 0
# PIN the served set, for an A/B and for a validation run: `DAILY_QUEST_SET=6000505,6000101` serves exactly
# those ids, in that order, bypassing the pool pick. An id the catalogue does not have is REPORTED and
# dropped, and an empty value means "no pin" -- so the default run is unaffected. This exists because the
# pool pick can legitimately serve a set in which NO row is tickable (two of the three floor pools name an
# action no request carries yet), and a validation run needs at least one row the player can actually
# complete. It is an override, not a default: nothing sets it unless a run asks.
DAILY_QUEST_SET = os.environ.get("DAILY_QUEST_SET", "")
# FORCE the completion popup. `DQI_SHOW_COMPLETE` is the client's own "everything is done" flag
# (`setIsShowCompleteScreen`, true iff the value is exactly "1"), and it is the ONLY consumer left for the
# NAME column: the list row draws `getDetails()` (a run confirmed it -- "Summon a unit", never "Summoner"),
# and the `sub_quest_detail_*` layouts belong to the EXPEDITION family (`sgExpdTopScene`, request
# `hW0804Q9`), not to the daily quest. `DailyQuestCompletionPopupScene` takes a `std::string` in its ctor and
# builds scrolling text, so it is the natural place for a title -- which makes this knob the way to settle
# whether `MST_DAILY_QUEST_NAME_<id>` is read at all, and therefore whether the servable set must be the
# name x detail INTERSECTION or could be the detail-only ids (185 of them rather than 46). Until it is
# answered the conservative intersection stands, because being wrong the other way shows a raw key.
# `1`/`true`/`yes` force it; anything else is the earned state.
DAILY_QUEST_SHOW_COMPLETE = os.environ.get("DAILY_QUEST_SHOW_COMPLETE", "")
# Make the SERVED ROWS read complete -- `Status` 1 and `Current == Target` -- WITHOUT touching the player
# state. Two reasons this exists, both from what a run actually showed:
#   * forcing only `DAILY_QUEST_SHOW_COMPLETE` fakes ONE flag, so the popup opens over a quest page that still
#     says 0/5. That is a lie in one place and a truth in another, and it is exactly the state a reviewer
#     cannot reason about. With this knob the page reads finished and `9vmYua2D` becomes **earned** from the
#     rows rather than forced, so the whole thing is one faithful reproduction.
#   * two of the three default-pool rows cannot tick yet (SW-122), so "everything done" is otherwise
#     unreachable and the finished appearance of the page had never been seen at all.
# It is a DISPLAY A/B: no progress, no claim, no state write. `claim_daily_quest` still refuses these rows,
# because the refusal reads the STATE, not the reply -- deliberately, so this knob cannot be used to farm.
DAILY_QUEST_FORCE_COMPLETE = os.environ.get("DAILY_QUEST_FORCE_COMPLETE", "")
# Serve a team-level before/after when no rank-up has ever been recorded. `tzCNMJ87` is omitted in that case
# ON PURPOSE (a fabricated "1 -> 1" would draw a level-up that never happened), which is correct and also
# means the popup's awards list is empty for a player who has not levelled up since this existed -- measured:
# a run at rank 4 with 10 exp had `rank.last_up = null`, because a battle grants 10 rank exp and 4 -> 5 needs
# 40. This knob fills the popup for a DISPLAY run using the CURRENT rank, derived as before = level-1 at the
# previous rank's exp threshold, and says so in the log. It is not a substitute for a real rank-up: it exists
# so the awards list can be SEEN once, which is the only way to tell whether it renders at all.
DAILY_QUEST_TEAM_LV = os.environ.get("DAILY_QUEST_TEAM_LV", "")
# Make TODAY'S QUESTS GENUINELY DONE, by driving each row's real counter to its target. This one WRITES
# STATE, and it is deliberately a different knob from the two above for exactly that reason:
#   * `DAILY_QUEST_FORCE_COMPLETE` is a DISPLAY switch -- the rows read complete, and the claim path still
#     refuses because the counters are at 0. It exists to see what a finished page looks like.
#   * this one moves the COUNTERS, so the whole earned path runs for real: the claim, the claim-all, the
#     reward payment, the `Status` transition and the completion popup. Without it, two of the three
#     default-pool rows can never be completed (SW-122), so the parts of the feature that only happen on a
#     finished day had never been executed once.
# Idempotent and capped at each row's own target, so it is safe to leave set; and the log line says
# INJECTION, because a counter that moved without the player doing anything must never be mistakable for a
# fact about the account.
DAILY_QUEST_COMPLETE_TODAY = os.environ.get("DAILY_QUEST_COMPLETE_TODAY", "")
# The day boundary. `remainingTime` is in SECONDS (`DailyQuestInfo::getRemainingTimeInSecs` 0x1d970e4) and
# the client stamps `lastCheckedTime` from it, so it is the server's clock that decides when the day rolls.
DAILY_QUEST_DAY_FORMAT = "%Y-%m-%d"
# --- the QUEST progress store: which action satisfies which condition code ---------------------------------
# The client does NOT report account-level milestones (see docs/notes/panel_quest.md §4d): quest progress is
# server-authoritative and travels to the client in `oz5Vt6AR` (ProgressValue) on both quest families, which
# share the `Pzn5h0Ga` condition spec. So the server has to decide what a quest's progress IS, and the only
# honest inputs are the actions the client already sends.
#
# `action -> condition codes`. The codes are MEASURED from `F_SP_CHALLENGE_MST`/`F_PANEL_QUEST_MST`; the
# mapping to an action is `[authored, inferred]` from each quest's own shipped name, and every row here is
# falsifiable by one tap in the client -- which is the validation loop, not a guess. `11`/`12`/`36` are all
# satisfied by the SAME request (one equip touches weapon, armor and accessory slots), so the codes are a
# tuple and the bump is one per quest.
#
# PHASE 1 IS `equip` ONLY: it is the one the operator can validate with a single tap, and it exercises the
# whole loop (state -> counter -> both families' projections). The other 28 of Rookie Quests' 31 are listed in
# `PROGRESS_PHASE2` with the request that would satisfy them, so the sequence is on the record.
PROGRESS_ACTIONS = {
    # `UnitEquipRequest` (pB3st6Tg) group `Er92Kdhm`: `93Y6evuT` = setEquipInfo, `80NUYFMJ` = materia.
    "equip": ("11", "12", "36"),
    # `summon` is DAILY-QUEST ONLY, and the empty tuple is the point, not an oversight. Its panel-quest side
    # would be condition code `5`, whose shipped spec is `5:51` -- and `51` is almost certainly a BANNER id,
    # so a quest "summon on banner 51" cannot be honestly ticked by "a summon happened" (SW-122 item 3, still
    # unconfirmed). The DAILY quest's own spec is a bare "Summon a unit" with no id, so `summon` scores it
    # exactly. One `PROGRESS_REQUESTS`, two different joins, and the empty tuple says which side is off
    # rather than a second map existing to be out of sync with the first.
    "summon": (),
    # MISSION CLEAR, condition code `15` -- the biggest single code in the corpus (855 of the SP-challenge
    # rows, 16 of Rookie Quests' 31, 4 of the Advanced 18) and the one the operator's first quest needs.
    # Its spec is PARAMETERISED, which is the whole reason it gets its own entry rather than joining
    # `equip`'s shape: `15:8991106` names ONE mission and `15:*` means "any quest" (227 rows), so a clear of
    # 1110100 must score `15:*` and `15:1110100` and nothing else. The join is therefore parameter-aware
    # (`_quest_matches` in ffbe_player), and the parameter is read from the shipped `Pzn5h0Ga`, never typed
    # here. The id space is the client's own: `MissionEndRequest`'s `jQsE54Iz.qo3PECw6` is
    # `F_MISSION_MST.qo3PECw6`, the same column the spec names (verbatim capture:
    # `jQsE54Iz = [{"qo3PECw6": "1110100", "xojJ2w0S": "0"}]`).
    "mission_clear": ("15",),
}
PROGRESS_REQUESTS = {
    # Each action maps to a TUPLE of request ids, not one. It is a tuple because FIVE mission-end classes
    # exist in the client and the server SERVES two of them (audit 2026-09-27): `JiglLZx6`
    # (`TfaMissionEndRequest`) deliberately carries a COPY of the `x5Unqg2d` payload -- the copy is what
    # makes `grant_cleared_mission` fire on a TFA win and open the next chamber tier -- and the client
    # reaches a trial through `TfaMissionStartRequest` (`BSq28mwY`), so that is the request a TFA win
    # actually arrives on. Keying on `x5Unqg2d` alone made a TFA clear grant its UNLOCK and pay no quest
    # progress at all, silently. Both bodies carry the mission id in the same group, so one reader serves both.
    "equip": ("pB3st6Tg",),             # UnitEquipRequest
    "summon": ("9fVIioy1",),            # GachaExeRequest (75 captures) -- the arrival IS the fact
    "mission_clear": ("x5Unqg2d", "JiglLZx6"),   # MissionEndRequest (62 captures) + the TFA twin
}
# The parameter the `mission_clear` join matches on: a spec parameter of `*` (or an absent one) means "any
# quest" and matches every clear; anything else must CONTAIN the cleared mission id, split on `@` -- because
# three shipped quests carry a fourteen-id list there (`15:8210107@8210108@...@8210120`). Kept as a constant
# because it is the one place a "match anything" wildcard is spelled.
PROGRESS_PARAM_ANY = "*"
# The separator a `Pzn5h0Ga` parameter uses for a LIST of alternatives. Measured over both quest tables:
# **three** rows carry one (`500201`, `500301`, `500401` -- fourteen mission ids each, ProgressMax 8,
# shipped name "Clear the 'Once-a-Day Special Quest' 8 times!"), and the same table uses `@` as a plain
# field separator in `B2FW5Jq` (`"9001:60@6@24"`). `F_CHALLENGE_MST` uses it in 0 of 42,757 specs, so this
# is a form with no precedent in the sibling table -- hence a named constant, so the one reader is obvious.
PROGRESS_PARAM_LIST_SEP = "@"
# The request-side group the mission id rides in, on `MissionEndRequest`: `jQsE54Iz:[{"qo3PECw6": <id>}]`
# (verbatim capture, `out/requests/20260920T095533_0016.json`). Read from THAT group rather than by scanning
# every group for a mission-id token, because the mission-end body also carries result rows that name the
# same mission and a first-match scan would be answering on dict order.
PROGRESS_MISSION_GROUP = "jQsE54Iz"
PROGRESS_MISSION_TOKEN = "qo3PECw6"
# The same action, per family, as the counter key prefix. Keys are `<family>/<schedule>/<questId>`; the
# schedule ids are the tables' own group ids (`F_SP_CHALLENGE_MST.zF6QIY2r` / `F_PANEL_QUEST_MST.zF6QIY2r`).
# The condition code a quest must match for an action to bump it. Read from the shipped tables at serve time
# (see `ffbe_player._quest_index`), so a code is never re-typed here and cannot drift from the data.
PROGRESS_PHASE2 = (
    # (action, condition codes, the request that would carry it, status)
    ("mission_clear", ("15",), "MissionEndRequest x5Unqg2d (62 captures) + its TFA twin JiglLZx6 -- DONE "
     "2026-09-27, parameter-aware: the spec's `15:<missionId>` must CONTAIN the cleared mission (a few "
     "ship `15:a@b@c`), and `15:*` matches any", "done"),
    ("limit_burst", ("23",), "MissionEndRequest `s2rX7g0o` present (challenge_conditions.md §2)", "next"),
    ("summon", ("5",), "GachaExeRequest 5yLD4dYI (75 captures) -- the param 51 is a banner id, unconfirmed", "next"),
    ("craft", ("7",), "NOT LOCATED -- no craft request seen in the corpus", "open"),
    ("awaken_unit", ("28",), "NOT LOCATED", "open"),
    ("awaken_ability", ("1",), "NOT LOCATED", "open"),
    ("friend_invite", ("14",), "NOT LOCATED", "open"),
    ("friend_gift", ("6",), "the GIFT family (h5XGny0N / PmR4z1S7) -- no request seen", "open"),
    ("friend_quest", ("17",), "NOT LOCATED (a mission-end flag, probably)", "open"),
    # --- and the DAILY QUEST's own join key is the ACTION, not a condition code -----------------------
    # The daily quest has no `Pzn5h0Ga` condition column at all: its row is id/type/status/current/target/
    # location/reward, and what the player must DO is carried by the SHIPPED TEXT of the row
    # (`MST_DAILY_QUEST_DETAIL_<id>` -- "Send gifts to 5 friends"). So the join into the shared progress
    # store is by the action name `harness/gen_daily_quests.py` parsed out of that sentence, and the rows
    # live in `server/daily_quests.json`. One table of "things the player did" still serves both families:
    # an `equip` satisfies a panel-quest condition code AND a daily-quest action.
    # `DAILY_QUEST_ACTIONS` is the measured action vocabulary; `PROGRESS_ACTIONS` above stays the
    # condition-code side. A new action is DATA in `server/daily_quests.json`, not a new branch here.
    ("esper_summon", ("27",), "MissionEndRequest + an esper in the party", "open"),
    ("arena", ("2",), "MissionEndRequest in an arena mission", "open"),
    ("colosseum", ("8",), "MissionEndRequest in a colosseum mission", "open"),
)


__all__ = [
    # The 8th-anniversary ticket knobs (deliver_anniversary_ticket reads them via `import *`, and this
    # module has an explicit __all__, so an unlisted constant is a NameError at call time, not a typo
    # the reader would catch).
    "ANNIVERSARY_TICKET",
    "ANNIVERSARY_TICKET_ITEM",
    "ANNIVERSARY_TICKET_TYPE",
    "ANNIVERSARY_TICKET_COUNT",
    "ANNIVERSARY_TICKET_TITLE",
    "ANNIVERSARY_TICKET_CARRIER",
    "HERE",
    "WORK",
    "BANNER_ART_ROOTS",
    "AREA_ART_ROOTS",
    "BUTTON_ART_ROOTS",
    "DROP_TAG_ROWS",
    "DROP_TOKEN",
    "RESULT_TAG",
    "RESULT_DROP_TOKEN",
    "RESULT_ITEM_TOKENS",
    "DIAMOND_TAG",
    "DIAMOND_FREE",
    "DIAMOND_PAID",
    "USER_STATE_GROUPS",
    "TEAM_INFO_TAG",
    "PARTY_DECK_GROUP",
    "EMBLEM_GROUP",
    "EMBLEM_ID",
    "CARRY_ITEM_GROUP",
    "CARRY_ITEM_CSV",
    "CARRY_EDIT_REQUEST",
    "EMBLEM_USER_ID",
    "EMBLEM_SETTING_REQUEST",
    "EMBLEM_ENTRY_REQUEST",
    "EMBLEM_EVENT_REQUEST",
    "EMBLEM_MONTHLY_GROUP",
    "EMBLEM_WEEKLY_GROUP",
    "EMBLEM_MILESTONE_GROUP",
    "EMBLEM_CAMPAIGN_TOKEN",
    "EMBLEM_TOKEN_COUNT",
    "EMBLEM_BUNDLE_ID",
    "EMBLEM_PASS_STATUS",
    "EMBLEM_BANNER_IMAGE",
    "EMBLEM_PERSISTENCE",
    "EMBLEM_DISPLAY_ORDER",
    "EMBLEM_MILESTONE_TOKEN",
    "EMBLEM_WEEKLY_PROGRESS",
    "EMBLEM_WEEKLY_CLEARED",
    "EMBLEM_START_DATE",
    "EMBLEM_END_DATE",
    "EMBLEM_TIER_MAIL_TITLE",
    "EMBLEM_MAIL_ID_BASE",
    "FAV_GROUP",
    "FAV_SET",
    "FAV_CLEAR",
    "FAV_INFO_TAG",
    "STORAGE_GROUP",
    "STORAGE_STATE",
    "STORAGE_IDS",
    "SELL_GROUP",
    "SELL_GIL",
    "SELL_IDS",
    "MIX_GROUP",
    "BEAST_BOARD_GROUP",
    "BEAST_BOARD_LEARNED",
    "BEAST_BOARD_UNLOCKED",
    "BEAST_MIX_GROUP",
    "BEAST_MIX_ITEMS",
    "BEAST_ID_TOKEN",
    "FAVOURITE_INFO_ALL",
    "GACHA_TAG",
    "GACHA_AUTHORED",
    "GACHA_CATALOGUE",
    "GACHA_CATALOGUE_ON",
    "GACHA_CATALOGUE_LIMIT",
    "GACHA_CATALOGUE_OFFSET",
    "GACHA_COMMUNITY",
    "GACHA_COMMUNITY_ON",
    "GACHA_COMMUNITY_LIMIT",
    "GACHA_CAMPAIGN_FILE",
    "GACHA_CAMPAIGN",
    "GACHA_CAMPAIGN_OFF",
    "GACHA_ONLY",
    "GACHA_ONLY_FILE",
    "GACHA_STANDARD_FILE",
    "GACHA_STANDARD_ON",
    "GACHA_MULTI_BTN",
    "GACHA_TYPE_CODE",
    "GACHA_EXE_TYPE",
    "GACHA_TARGET_TYPE",
    "GACHA_RATE_SCALE",
    "GACHA_DEFAULT_BANNER_ART",
    "GACHA_DEFAULT_ANIME_FILE",
    "GACHA_DEFAULT_ANIME_NAME",
    "GACHA_BGM_RES_ID",
    "GACHA_BGM_RES_VERSION",
    "GACHA_LANG",
    "GACHA_INFO_REPLIES",
    "GACHA_MST_TAG",
    "GACHA_DETAIL_TAG",
    "GACHA_SCHEDULE_TAG",
    "GACHA_PANEL_TAG",
    "GACHA_PANEL_GROUP_TAG",
    "GACHA_USER_INFO_TAG",
    "GACHA_STEP_INFO_TAG",
    "CHALLENGE_MST",
    "LAPIS_AWARD",
    "LAPIS_REWARD_KIND",
    "RANKUP_BONUS_LAPIS",
    "CLEAR_INFO_FROM_STATE",
    "CHALLENGE_INFO_ALL",
    "CHALLENGE_INFO_ANY",
    "CHALLENGE_INFO_ALLOW",
    "MIRROR_DROPS",
    "RESULT_ITEM_COLUMNS",
    "BATTLE_BGM",
    "PHASE_TAG",
    "PHASE_BGM_TOKEN",
    "DROP_TABLE",
    "ROLL_DROPS",
    "STATE_PERSIST",
    "CHALLENGE_GROUP",
    "BATTLE_CLEAR_TOKEN",
    "USE_ITEM_TOKEN",
    "USE_MAGIC_TOKEN",
    "USE_LB_TOKEN",
    "ABSENCE_WITNESSED",
    "SWITCH_TYPE_FIELD",
    "SWITCH_BITS_FIELD",
    "SWITCH_TYPE_MISSION",
    "PUSHED_SWITCH_GROUP",
    "PUSHED_SWITCH_FIELD",
    "SWITCH_BASE_INDICES",
    "SWITCH_CONTAINER_MIN",
    "VERSION_TABLE_XML",
    "LOGIN_MONTH",
    "LOGIN_BANNER",
    "FORCE_TABLE_VERSIONS",
    "SERVE_UNIT_HISTORY",
    "SERVE_NV_EX",
    "_MONTH_NO",
    "CATCHUP_SP_TAG",
    "CATCHUP_TOTAL_TAG",
    "CATCHUP_OPE_TAG",
    "CATCHUP_INFO_TAG",
    "CATCHUP_SP_ID",
    "CATCHUP_DAY_LIST",
    "CATCHUP_START_YMD",
    "CATCHUP_END_YMD",
    "CATCHUP_LOGIN_DAYS",
    "CATCHUP_SERIAL",
    "CATCHUP_DAY_INFO",
    "CATCHUP_PROCESS_DAY",
    "CATCHUP_BONUS_ID",
    "CATCHUP_REQUESTS",
    "CATCHUP_USER_STATE",
    "CATCHUP_BLOCKED_REQUESTS",
    "CATCHUP_SP_MST_TAG",
    "CATCHUP_SP_TODAY",
    "CATCHUP_SP_TARGET_TYPE",
    "CATCHUP_SP_BANNER",
    "CATCHUP_SP_BG",
    "CATCHUP_DAY_SEP_SP",
    "CATCHUP_DAY_SEP_INFO",
    "CATCHUP_INFO_DAYS",
    "CATCHUP_MAIL_ID_BASE",
    "PANEL_QUEST_REQUEST",
    "PANEL_QUEST_CLAIM_REQUEST",
    "PANEL_QUEST_CLAIM_GROUP",
    "PANEL_QUEST_STATUS_TAG",
    "PANEL_QUEST_SCHEDULE_TAG",
    "PANEL_QUEST_INFO_TAG",
    "PQ_SCHEDULE_ID",
    "PQ_GROUP_ID",
    "PQ_QUEST_ID",
    "PQ_PERSISTENT",
    "PQ_DISP_ORDER",
    "PQ_START",
    "PQ_END",
    "PQ_LIMIT",
    "PQ_PROGRESS",
    "PQ_CLEAR",
    "PQ_REWARD_FLAG",
    "PQ_REWARD_COUNT",
    "PQ_IS_NEW",
    "PANEL_QUEST_WINDOW_FORMAT",
    "PANEL_QUEST_WINDOW_DAYS",
    "PANEL_QUEST_DEFAULT_GROUPS",
    "PANEL_QUEST_NAMED_GROUPS",
    "SP_CHALLENGE_REQUEST",
    "SP_CHALLENGE_CLAIM_REQUEST",
    "SP_CHALLENGE_CLAIM_GROUP",
    "SP_CHALLENGE_SCHEDULE_TAG",
    "SP_CHALLENGE_STATUS_TAG",
    "SP_CHALLENGE_INFO_TAG",
    "SP_CHALLENGE_TICKER_TAG",
    "SP_SCHEDULE_ID",
    "SP_GROUP_ID",
    "SP_CHALLENGE_ID",
    "SP_DISP_TYPE",
    "SP_RESET_TYPE",
    "SP_PERSISTENT",
    "SP_SWITCH_INFO",
    "SP_DISP_ORDER",
    "SP_START",
    "SP_END",
    "SP_REWARD_LIMIT",
    "SP_REWARD_COUNT",
    "SP_IS_NEW",
    "SP_PROGRESS",
    "SP_CLEAR",
    "SP_REWARD_FLAG",
    "SP_TICKER_SCHEDULE_NAME",
    "SP_TICKER_CHALLENGE_NAME",
    "SP_NAME_KEY_PREFIX",
    "SP_NAME_KEY_MULT",
    "SP_NAME_KEY_ADD",
    "SP_NAME_KEY",
    "SP_SCHEDULE_WINDOW_FORMAT",
    "SP_SCHEDULE_WINDOW_DAYS",
    "SP_CHALLENGE_DEFAULT_GROUPS",
    "PROGRESS_PARAM_ANY",
    "PROGRESS_PARAM_LIST_SEP",
    "PROGRESS_MISSION_GROUP",
    "PROGRESS_MISSION_TOKEN",
    "PROGRESS_ACTIONS",
    "PROGRESS_REQUESTS",
    "PROGRESS_PHASE2",
    "DAILY_QUEST_TAG",
    "DAILY_QUEST_INFO_TAG",
    "DQ_ID",
    "DQ_TYPE",
    "DQ_STATUS",
    "DQ_CURRENT",
    "DQ_TARGET",
    "DQ_LOCATION",
    "DQ_NAME",
    "DQ_DETAILS",
    "DQ_REWARD",
    "DQI_SHOW_COMPLETE",
    "DQI_TEAM_LV",
    "DQI_LEVEL_UP_DIA",
    "DQI_REMAINING",
    "DAILY_QUEST_CARRIERS",
    "DQI_RANKUP_LAPIS",
    "DAILY_QUEST_CLAIM",
    "DAILY_QUEST_CLAIM_ALL",
    "DAILY_QUEST_CLAIM_GROUP",
    "DAILY_QUEST_INFO_CARRIERS",
    "DAILY_QUEST_STATUS_MODE",
    "DQ_STATUS_INCOMPLETE",
    "DQ_STATUS_COMPLETE",
    "DQ_STATUS_CLAIMED",
    "DAILY_QUEST_DEFAULT_POOLS",
    "DAILY_QUEST_DEFAULT_COUNT",
    "DAILY_QUEST_LOCATION",
    "DAILY_QUEST_SET",
    "DAILY_QUEST_SHOW_COMPLETE",
    "DAILY_QUEST_FORCE_COMPLETE",
    "DAILY_QUEST_TEAM_LV",
    "DAILY_QUEST_COMPLETE_TODAY",
    "DAILY_QUEST_DAY_FORMAT",
    "SURVIVAL_ON",
    "SURVIVAL_AREA",
    "SURVIVAL_AREAS",
    "SURVIVAL_DUNGEON",
    "SURVIVAL_MISSION",
    "SURVIVAL_EVENT",
    "SURVIVAL_PAIR",
    "SURVIVAL_SUMALLY",
    "SURVIVAL_BANNER",
    "SURVIVAL_EVENT_TYPE",
    "SURVIVAL_BANNER_ART",
    "SURVIVAL_BANNER_ART_BY_AREA",
    "SURVIVAL_WINDOW_START",
    "SURVIVAL_WINDOW_END",
    "SURVIVAL_TAG",
    "SURVIVAL_USER_TAG",
    "SURVIVAL_COND_TAG",
    "SURVIVAL_PARTS",
    "SURVIVAL_CARRIERS",
    "SURVIVAL_COND_CARRIERS",
    "EVENT_GATE_AREAS",
    "EVENT_GATE_TAB",
    "EVENT_GATE_KIND",
    "EVENT_GATE_ART",
    "EVENT_GATE_WINDOW_START",
    "EVENT_GATE_WINDOW_END",
    "EVENT_GATE_TAG",
    "EVENT_GATE_CARRIERS",
    "EVENT_CURRENCY",
    "SURVIVAL_EVENT_KIND",
    "SURVIVAL_BREAK_TAG",
    "SURVIVAL_BREAK_PROGRESS",
    "SURVIVAL_BREAK_GATE",
    "SURVIVAL_BREAK_CARRIERS",
    "SURVIVAL_STATE_TAG",
    "SURVIVAL_STATE_TOKEN",
    "SURVIVAL_STATE_VERSION",
    "SURVIVAL_SUMMON_GAUGE",
    "SURVIVAL_STATE_CARRIERS",
]
