#!/usr/bin/env python3
"""ffbe_gacha.py — the GACHA axis: the authored banner layer, the pool/cost grammar, and the server-authoritative pull.

It is the largest module by line count and the one most likely to be edited by someone new, because the
banner data is JSON and inviting. The rule that keeps it honest: **the rates are ours.** They were
transmitted, never shipped, so nothing in this module is recovered data unless the docstring says so --
`gacha_community_rates.md` is the source for the community-sourced halves and every authored default is
labelled at its definition.

Extracted 2026-09-26 from `capture_server.py` (docs/SERVER_REFACTOR_PLAN.md, Phase 3, last seam).
"""
from __future__ import annotations

import copy, json, os, re

from ffbe_config import *          # noqa: F401,F403 -- constants; the table has no behaviour
import ffbe_state as st           # the REBOUND runtime state; see ffbe_state's docstring
from ffbe_state import State, _note
from player_state import COST_KINDS, cost_numeric_kind
from data_paths import TEXT_DIR


__all__ = [
    "GACHA_PLAYER_REFRESH_GROUPS",
    "all_banners",
    "authored_banners",
    "banner_art_index",
    "button_art_index",
    "campaign_selection",
    "catalogue_banners",
    "community_banners",
    "gacha_button_base",
    "gacha_field_units",
    "gacha_groups",
    "gacha_offer_for",
    "gacha_only_ids",
    "gacha_secondary_detail_id",
    "gacha_tutorial_award",
    "serve_gacha",
    "serve_gacha_info",
    "standard_banners",
    "unit_name",
]

_BANNER_ART_INDEX: dict = {}
def banner_art_index() -> dict:
    """basename -> extracted file, built once (the banner CPK trees are small)."""
    if not _BANNER_ART_INDEX:
        for root in BANNER_ART_ROOTS:
            for dirpath, _dirs, files in os.walk(root):
                for fn in files:
                    _BANNER_ART_INDEX.setdefault(fn, os.path.join(dirpath, fn))
    return _BANNER_ART_INDEX
_AREA_ART_INDEX: dict = {}
def area_art_index() -> dict:
    """basename -> extracted file for the AREA art (the gate/list tile art family).

    A separate index from the banners on purpose: the two live in different CPK trees, so a name collision
    between them would be a coincidence rather than a meaning. Built once (2681 PNGs).
    """
    if not _AREA_ART_INDEX:
        for root in AREA_ART_ROOTS:
            for dirpath, _dirs, files in os.walk(root):
                for fn in files:
                    _AREA_ART_INDEX.setdefault(fn, os.path.join(dirpath, fn))
    return _AREA_ART_INDEX
_BUTTON_ART_INDEX: dict = {}
def button_art_index() -> dict:
    if not _BUTTON_ART_INDEX:
        for root in BUTTON_ART_ROOTS:
            for dirpath, _dirs, files in os.walk(root):
                for fn in files:
                    _BUTTON_ART_INDEX.setdefault(fn, os.path.join(dirpath, fn))
    return _BUTTON_ART_INDEX
def _btn_exists(base: str) -> bool:
    idx = button_art_index()
    return f"{base}_1.png" in idx or f"{base}_ticket_1.png" in idx
def _primary_cost(banner: dict) -> dict:
    """The banner's FIRST cost entry as a dict (for a class check). `cost` may be a LIST (multi-entry)."""
    cost = banner.get("cost")
    if isinstance(cost, list):
        cost = cost[0] if cost else {}
    return cost if isinstance(cost, dict) else {}
def gacha_button_base(banner: dict) -> str:
    """`img_btn` base: `gacha_btn_n` for the friend-point summon, else `gacha_btn_r` (see the note above)."""
    kind = str(_primary_cost(banner).get("kind") or "")
    return "gacha_btn_n" if kind == "friend_point" else "gacha_btn_r"
_UNIT_NAMES: dict = {}
def unit_name(master_id) -> str:
    """The English display name of a unit master id (shipped `F_TEXT_UNITS_NAME`), "" if unknown."""
    if not _UNIT_NAMES:
        path = os.path.join(TEXT_DIR, "F_TEXT_UNITS_NAME.ndjson")
        try:
            with open(path, encoding="utf-8") as fh:
                for ln in fh:
                    ln = ln.strip()
                    if not ln:
                        continue
                    r = json.loads(ln)
                    m = re.match(r"MST_UNIT_NAME_(\d+)$", str(r.get("key", "")))
                    if m:
                        v = r.get("v")
                        _UNIT_NAMES[m.group(1)] = v[0] if isinstance(v, list) and v else str(v)
        except (OSError, ValueError):
            pass
    return _UNIT_NAMES.get(str(master_id), "")
def gacha_field_units(banner: dict) -> list:
    """The unit ids the PAY detail scene poses on its field (`GachaDetailMst.t6aQ1voV`). The community rows
    carry the wiki's featured `unit_ids`; otherwise the pool's `featured` units. Empty -> no composite
    (`gacha_pay_field_research.md`: an empty `t6aQ1voV` suppresses the unit poses AND the monster).

    **ONE unit only.** `BUW0I4JL` carries one motion segment per unit and opcode `0` queues that unit's CG
    (cgs type 1); with several units the scene queues several CGs and cycles through them on screen — the
    live "flipping fast" plus a per-unit loading screen. One unit -> one pose, one load."""
    ids = [str(u) for u in (((banner.get("community") or {}).get("unit_ids")) or [])]
    if not ids:
        pool = banner.get("pool")
        if isinstance(pool, dict):
            for t in pool.get("tiers") or []:
                ids += [str(u) for u in (t.get("featured") or [])]
    return ids[:1]
_CAMPAIGN_DOC = None          # lazily loaded `gacha_campaigns.json`
_CAMPAIGN_LOGGED = [False]    # the selection is announced once, not on every reply
_ONLY_LOGGED = [False]        # ditto for the single-banner mode
_STANDARD_DOC = None          # lazily loaded `gacha_standard.json`
_STANDARD_LOGGED = [False]    # the merge is announced once, not on every reply
def gacha_tutorial_award():
    """The authored forced-opening award `(unitId, granted_rarity)` from `server/gacha_authored.json`.

    AUTHORED, never recovered: the tutorial request carries no banner, so the client cannot be choosing it
    (schema §3.8 / shop_gacha_scope.md §4). The id is a real `F_UNIT_MST` id so the unit renders.
    """
    try:
        with open(GACHA_AUTHORED, encoding="utf-8") as fh:
            award = json.load(fh).get("tutorial_award") or {}
        return award.get("unitId"), award.get("granted_rarity")
    except (OSError, ValueError):
        return None, None
def _banner_doc(path: str, *, offset: int = 0, limit: int = 0, note: str) -> list:
    """The `banners` list from one gacha doc, sliced by `offset`/`limit`; [] and a note when unreadable."""
    try:
        with open(path, encoding="utf-8") as fh:
            bs = [b for b in (json.load(fh).get("banners") or [])
                  if isinstance(b, dict) and b.get("id")]
    except (OSError, ValueError) as exc:  # noqa: BLE001 - a bad doc must not kill the reply
        _note(f"gacha: {path} unreadable ({exc!r}) -- {note}")
        return []
    bs = bs[offset:]
    return bs[:limit] if limit else bs
def authored_banners() -> list:
    """The AUTHORED banners from `gacha_authored.json`. Never a shipped/recovered table -- the gacha content
    was transmitted, not shipped (docs/notes/gacha_reconstruction.md SS0)."""
    return _banner_doc(GACHA_AUTHORED, note="no banner can be served")
def catalogue_banners() -> list:
    """The GENERATED broad catalogue (SW-68): 273 real gacha ids from the shipped `F_GACHA_DETAIL_EXTRA_MST`,
    their shipped titles, and authored tab/cost/pool/art. Off with `GACHA_CATALOGUE=0`; sliced by
    `GACHA_CATALOGUE_OFFSET`/`GACHA_CATALOGUE_LIMIT` (the rotation window)."""
    if not GACHA_CATALOGUE_ON:
        return []
    return _banner_doc(GACHA_CATALOGUE, offset=GACHA_CATALOGUE_OFFSET, limit=GACHA_CATALOGUE_LIMIT,
                       note="serving the authored set only")
def community_banners() -> list:
    """The COMMUNITY calendar (2016-2022 Fandom wiki): real banner names, dates, units and art. `[Community]`,
    a hint — off with `GACHA_COMMUNITY=0`, sliced by `GACHA_COMMUNITY_LIMIT`."""
    if not GACHA_COMMUNITY_ON:
        return []
    return _banner_doc(GACHA_COMMUNITY, limit=GACHA_COMMUNITY_LIMIT, note="no community calendar")
def standard_banners() -> list:
    """The STANDARD families (SW-75) from `gacha_standard.json` -- the static, never-campaign-tied FREE-tab
    summons. Authored (`source: standard-authored`); the ids/titles are shipped. Off unless `GACHA_STANDARD=1`."""
    if not GACHA_STANDARD_ON:
        return []
    global _STANDARD_DOC
    if _STANDARD_DOC is None:
        try:
            with open(GACHA_STANDARD_FILE, encoding="utf-8") as fh:
                _STANDARD_DOC = json.load(fh)
        except (OSError, ValueError) as exc:  # noqa: BLE001 - a bad standard file must not kill the reply
            _note(f"gacha standard: {GACHA_STANDARD_FILE} unreadable ({exc!r}) -- serving none")
            _STANDARD_DOC = {}
    return [b for b in (_STANDARD_DOC.get("banners") or [])
            if isinstance(b, dict) and b.get("id")]
def campaign_selection() -> tuple | None:
    """`(standard_ids, allowed_ids, label)` for the active campaign, or None when serving everything.
    A campaign is a SELECTION -- the standard set plus one community release month. The key comes from
    `GACHA_CAMPAIGN`, or from the campaign file's own `default` when that env var is UNSET (so a reset cannot
    lose it); `all`/`off`/`0` explicitly disables the filter."""
    global _CAMPAIGN_DOC
    key = str(st.GACHA_CAMPAIGN or "").strip()
    if key.lower() in GACHA_CAMPAIGN_OFF:
        return None
    if _CAMPAIGN_DOC is None:
        try:
            with open(GACHA_CAMPAIGN_FILE, encoding="utf-8") as fh:
                _CAMPAIGN_DOC = json.load(fh)
        except (OSError, ValueError) as exc:  # noqa: BLE001 - a bad campaign file must not kill the reply
            _note(f"gacha campaign: {GACHA_CAMPAIGN_FILE} unreadable ({exc!r}) -- serving EVERYTHING")
            _CAMPAIGN_DOC = {}
    doc = _CAMPAIGN_DOC or {}
    if not key:
        key = str(doc.get("default") or "")     # the file's default, not "everything"
        if not key:
            return None
    camp = (doc.get("campaigns") or {}).get(key)
    if not camp:
        known = sorted((doc.get("campaigns") or {}))
        _note(f"gacha campaign: no campaign {key!r} ({len(known)} known: "
              f"{', '.join(known[:8])}...) -- serving EVERYTHING")
        return None
    std = {str(e["id"]) for e in (doc.get("standard") or []) if isinstance(e, dict) and e.get("id")}
    ids = std | {str(e["id"]) for e in (camp.get("banners") or [])
                 if isinstance(e, dict) and e.get("id")}
    return std, ids, str(camp.get("label") or key)
def gacha_only_ids() -> set:
    """The ids `GACHA_ONLY` (or `server/gacha_only.txt`) restricts the served set to; empty = no restriction."""
    raw = str(GACHA_ONLY or "").strip()
    if raw.lower() in GACHA_CAMPAIGN_OFF:
        return set()
    if not raw:
        try:
            with open(GACHA_ONLY_FILE, encoding="utf-8") as fh:
                raw = " ".join(fh.read().split())
        except OSError:
            return set()
    return {t for t in (s.strip() for s in raw.replace(",", " ").split()) if t}
def all_banners() -> list:
    """Catalogue + community + authored; the authored (curated FOCUSED SET) OVERRIDES by id so its exact
    art/cost/pool is kept. Ids never collide (generated/catalogue 1-8xxxxx, community 9xxxxxx, curated low).
    `GACHA_CAMPAIGN` slices the result to standard + one campaign period (see `campaign_selection`);
    `GACHA_STANDARD=1` then merges the STANDARD families (`standard_banners()`) on top of that slice."""
    by_id: dict[str, dict] = {}
    for b in catalogue_banners():
        by_id[str(b["id"])] = b
    for b in community_banners():
        by_id[str(b["id"])] = b
    for b in authored_banners():
        by_id[str(b["id"])] = b
    only = gacha_only_ids()
    if only:
        kept = {k: v for k, v in by_id.items() if k in only}
        missing = sorted(only - set(kept))
        if not _ONLY_LOGGED[0]:
            _ONLY_LOGGED[0] = True
            _note(f"gacha ONLY: serving {len(kept)} of {len(by_id)} banner(s) "
                  f"({', '.join(sorted(kept))})" + (f" -- UNKNOWN ids: {', '.join(missing)}" if missing else ""))
        return list(kept.values())
    sel = campaign_selection()
    if sel:
        std, ids, label = sel
        kept = {k: v for k, v in by_id.items() if k in ids}
        if not _CAMPAIGN_LOGGED[0]:
            _CAMPAIGN_LOGGED[0] = True
            _note(f"gacha campaign {st.GACHA_CAMPAIGN or '(file default)'!r} "
                  f"({label}): serving {len(kept)} banner(s) "
                  f"= {len(std)} standard + {len(set(kept) - std)} campaign, of {len(by_id)} available")
        result = list(kept.values())
    else:
        result = list(by_id.values())
    # The STANDARD families are evergreen: merged ON TOP of (never filtered by) the campaign slice.
    std_banners = standard_banners()
    if std_banners:
        have = {str(b["id"]) for b in result}
        added = [b for b in std_banners if str(b["id"]) not in have]
        if added and not _STANDARD_LOGGED[0]:
            _STANDARD_LOGGED[0] = True
            fams = _STANDARD_DOC.get("_meta", {}).get("families") if _STANDARD_DOC else []
            _note(f"gacha standard: serving {len(added)} standard banner(s) "
                  f"({len(std_banners)} available, {len(fams)} families) on top of {len(result)}")
        result = result + added
    return result
def _gacha_cost_string(cost, draws: int = 1, item_id: int = 0) -> str:
    """`GachaDetailMst.costInfo` (0GgK6ytT) = `entry ("," entry)*`, each `kind:itemId:useCost:exeCount`
    (`parseCostInfo` 0x1cec090; `getUseCostValue` reads field[2], `getExeCount` field[3] --
    `gacha_banner_types.md` SS4). `draws` becomes the exeCount (a 10+1 is `exeCount=11` at ONE cost).
    Friend point = kind 51 -- `isFriendGacha` 0x1cecb24 is exactly that test. Empty string = no cost.

    `cost` may be a DICT (one entry) or a LIST of dicts (the `isCostInfoMulti` form) -- the PAY detail scene
    builds its buttons from the entries (`GachaPayDetailScene::setBtnLayout` 0x2157b94 reads `GACHA_MULTI` + the
    left/right prices + `_ticket`), so a single/double/ticket banner needs an entry per offer. An entry's own
    `draws` overrides the row's (the 11-summon is `50:0:5000:11`)."""
    entries = cost if isinstance(cost, list) else [cost or {}]
    out: list = []
    for e in entries:
        e = e if isinstance(e, dict) else {}
        kind = str(e.get("kind", "free"))
        if kind not in COST_KINDS:
            _note(f"gacha cost: unknown kind {kind!r} -- entry dropped (known: {sorted(COST_KINDS)})")
            continue
        num = cost_numeric_kind(kind)
        if num == 0:
            continue
        d = int(e.get("draws", draws))
        out.append(f"{num}:{int(e.get('item_id', item_id) or 0)}:{int(e.get('amount', 0))}:{d}")
    return ",".join(out)
def _gacha_art(art: dict, key: str) -> str:
    """The art filename for `key` in the preferred locale. `art` may carry a `key_en` English override;
    `GACHA_LANG=en` (default) prefers it when present, else falls back to `key` (the JP/`common_lang` file).
    All FOCUSED-SET banners are currently `region: jp`, so this is a no-op until EN art is added."""
    if GACHA_LANG == "en" and art.get(key + "_en"):
        return str(art[key + "_en"])
    return str(art.get(key, ""))
def gacha_secondary_detail_id(b: dict) -> str:
    """The detail id of a banner's SECOND (`exeType=2`) row. Distinct from the primary `nqzG3b2v` -- the client
    keys the rows by it, and `GachaExeRequest.nqzG3b2v` names the row the player TAPPED, so it is also how the
    roll resolves which offer to charge."""
    sec = b.get("secondary_offer") or {}
    return str(sec.get("detail_id") or (str(int(b["id"]) * 10 + 9)
                                         if str(b["id"]).isdigit() else str(b["id"]) + "2"))
def gacha_offer_for(banner: dict, detail_id: str):
    """The offer the tapped detail row means, or None for the primary row (whose cost/draws are the banner's
    own). `docs/notes/gacha_multi_button.md` SS9: the rows are `exeType`-partitioned, so the requested
    `nqzG3b2v` is the row selector."""
    if not detail_id or GACHA_MULTI_BTN == "0":
        return None
    sec = banner.get("secondary_offer")
    if isinstance(sec, dict) and sec and gacha_secondary_detail_id(banner) == str(detail_id):
        return sec
    return None
def _gacha_detail_row(b, art, detail_id, cost, draws, exe_type=None, img_btn=None) -> dict:
    """One `GachaDetailMst` row. `RZ6z12Dt` (ticketGroupId, +0x88) is emitted only for a ticket-only banner
    (empty cost + ticketGroupId>0 -> `isTicketOnlyGacha` true -> `TicketOnlyGachaDetailScene`);
    `LFoU0M3v` (gachaPanelId) only when the banner declares a panel. Step-ups get one row per step, with a
    detail id following the shipped convention `gachaId*10 + step`.

    `exe_type` overrides the banner's own `exe_type` (1 primary / 2 secondary, `ijEey5f7` +0x68) so ONE banner
    can emit several rows of different kinds. That field is the ONLY lever on the summon-button COUNT:    `GachaDetailScene::setLayout` (0x2140934) calls `setSingleButtonLayout` and `setMultiButtonLayout`
    UNCONDITIONALLY (both behind the `scene+0x6a0` suppression flag), and each self-gates on the presence of its
    OWN row -- `setSingleButtonLayout` (0x213cf3c) on `+0x5f0` (the `exeType==1` row), `setMultiButtonLayout`
    (0x213d3a4, gate at 0x213d3c8) on `+0x5f8` (the `exeType==2` row). `setGachaData` (0x213ca64) fills those
    two slots from `GachaDetailMstList::getListWithExeType(id, 1)` / `(id, 2)`. So a 1-row banner gets the
    standard single (+ticket) button and a 2-row banner gets the three-button layout.
    Evidence: `docs/notes/gacha_multi_button.md`."""
    det = {"X1IuZnj2": str(b["id"]), "nqzG3b2v": str(detail_id),
           "G4L0YIB2": str(b.get("name", "")),
           "ijEey5f7": str(GACHA_EXE_TYPE.get(str(exe_type or b.get("exe_type", "primary")), 1)),
           "0GgK6ytT": _gacha_cost_string(cost, draws,
                                          (cost.get("item_id", 0) if isinstance(cost, dict) else 0)),
           "4yT7FUqj": _gacha_banner_detail_string(_gacha_art(art, "detail_banner")),
           "XFLm4Mx6": img_btn or gacha_button_base(b)}
    # The BGM rides the DETAIL row (`9ZCEWn0z`/`wM9AfX6I` = +0x120/+0x124), which `getBgmMapResourceId` reads
    # FIRST (detail -> pay detail -> Mst+0x94). EMITTED ONLY WHEN AUTHORED -- see the `GACHA_BGM_RES_ID` block
    # above for why the default is "play nothing special and keep the home track". Emitted WITHOUT a version
    # still works (the resource is local); `GachaEntryScene::checkDownloadGachaBgm(id, ver)` is the only reader
    # of the version, and it is a CDN download check.
    bgm = str(b.get("bgm", "") or GACHA_BGM_RES_ID)
    if bgm and bgm != "0":
        det["9ZCEWn0z"] = bgm
        ver = str(b.get("bgm_version", "") or GACHA_BGM_RES_VERSION)
        if ver:
            det["wM9AfX6I"] = ver
    if b.get("ticket_group_id") and str(exe_type or b.get("exe_type", "primary")) != "secondary":
        det["RZ6z12Dt"] = str(b["ticket_group_id"])
    if b.get("panel_id"):
        det["LFoU0M3v"] = str(b["panel_id"])
    # The PAY detail scene's FIELD composite (`gacha_pay_field_rect`): the poses come from
    # `GachaDetailMst.t6aQ1voV` (`0:<unitId>`,...), the display names from `Rw4fS2m1` (`0:<name>`,...), and the
    # per-unit motion from `BUW0I4JL` (one `@`-segment per unit; opcode `0` = pose the unit). An EMPTY
    # `t6aQ1voV` makes `GachaPayDetailScene::setNewUILayout` (0x214a17c) return early -> no units AND no monster.
    # `gacha_pay_field_research.md`; bg/monster fall back to 100022 (Earth Shrine) / 3110010 (Ochu).
    units = gacha_field_units(b)
    if units and str(b.get("type")) != "friend":
        det["t6aQ1voV"] = ",".join(f"{i}:{u}" for i, u in enumerate(units))
        names = [f"{i}:{unit_name(u)}" for i, u in enumerate(units) if unit_name(u)]
        if names:
            det["Rw4fS2m1"] = ",".join(names)
        det["BUW0I4JL"] = "@".join("1,5" for _ in units)   # opcode 1 = NV/brave-shift, 5 = the LB/skill pair
    return det
def _gacha_banner_detail_string(banner: str) -> str:
    """`GachaDetailMst.bannerInfo` (`4yT7FUqj`, +0xc0): the `index:file` form `parseSetBannerInfo` requires.

    `GachaDetailScene::parseSetBannerInfo` `0x21433cc` reads `[mst+0xc0]` (`add x1, x2, #0xc0` at `0x21433f8`),
    splits on `,` then `:`, and `cmp x8, #0x30` at `0x21434ac` (= 2 x 24-byte `std::string`) DROPS any entry
    that is not exactly two fields; an accepted entry becomes `GachaBannerInfo{int @+0x0, string @+0x8}`.

    **This is NOT the field that draws the entry screen's `gacha_banner` slot.** It feeds a separate
    (virtual/address-taken) `getBannerInfoList` consumer (`0x2143a70`). The entry slot is fed by the
    *schedule* row -- see `_gacha_schedule_banner_string`. The earlier live run with `0:<file>` here was
    inconclusive about the entry slot for exactly that reason; the field is still kept in the 2-field form the
    parser demands. Evidence: `docs/notes/gacha_banner_draw_path.md` S4a/S4d.
    """
    return f"0:{banner}" if banner else ""
def _gacha_schedule_banner_string(banner: str, detail: str = "") -> str:
    """`GachaScheduleMst.bannerInfo` (`4yT7FUqj`): the MIXED form the two screens need, and they are TWO
    different SLOTS (confirmed 2026-09-21):
      * the **LIST** entry reads the **bare** field `+0x60` -- `GachaTopScene::setListLayout` (`0x2120880`
        `getBannerInfo()`, then `0x21208cc` `getBannerPath(true, 0 /*main*/)` + the bare name + `createBanner`).
        A 1-field BARE entry fills it. That slot is WIDE (a `bnr_*_rotation_*` 600x120 file).
      * the **DETAIL** carousel reads the `vector<std::string>` at `+0x78` -- `GachaDetailScene::setBannerLayout`
        (`0x2138e54`), fetched from the `sub` dir (`getBannerPath(true, 1)`). A **2-field `index:file`** entry
        pushes the *string* into that vector. That slot is 640x392.

    One comma entry cannot satisfy both, so serve `"<list_bare>,0:<detail_file>"`. Previously we sent ONE file
    for both, so the wide list slot got the tall detail art.
    Evidence: `docs/notes/gacha_banner_draw_path.md` S0/S4d/S8.
    """
    name = banner or GACHA_DEFAULT_BANNER_ART
    detail = detail or name
    return f"{name},0:{detail}"
def _gacha_target_info(entry: dict) -> str:
    """`GachaPanelGroupMst.targetInfo` (7iJpH5zZ) = `TargetType:TargetID:TargetNum:TargetRate:TargetLv`
    (`setAnalyzeTargetInfo` 0x1ceb934 stores into +0x68/6c/70/74/78)."""
    rate = int(round(float(entry.get("rate", 0)) * GACHA_RATE_SCALE))
    return (f"{GACHA_TARGET_TYPE.get('unit', 10)}:{entry.get('unit_id')}:"
            f"{int(entry.get('num', 1))}:{rate}:{int(entry.get('lv', 1))}")
def gacha_groups(banners: list) -> dict:
    """The five banner-layer reply groups + the player's per-banner state, from the authored banners.
    One `GachaPanelGroupMst` row PER POOL ENTRY (that row's `targetInfo` is the pool + rate)."""
    mst, detail, sched, panel, pgroup = [], [], [], [], []
    for b in banners:
        art = b.get("art") or {}
        sd = b.get("schedule") or {}
        window = sd.get("window") or {}
        has_panel = bool(b.get("panel_id"))
        row = {"X1IuZnj2": str(b["id"]),
               "sx4kyW0i": str(GACHA_TYPE_CODE.get(str(b.get("type", "normal")), 0)),
               "XFLm4Mx6": gacha_button_base(b),
               "g38Pjk10": str(art.get("anime_file") or GACHA_DEFAULT_ANIME_FILE),
               "JDFMe3A7": str(art.get("anime_name") or GACHA_DEFAULT_ANIME_NAME),
               "2RtVPS3b": "0"}
        # `7xBNR2MH` = `GachaMst.setGachaLayout` (+0x9c). `GachaMst::isPayGacha()` (0x1cea448) is exactly
        # `layout == 1`, and `GachaUtil::changeSummonSecondScene` (0x1a5314c branches on PLT `isPayGacha`)
        # routes on it: **1 -> `GachaPayDetailScene`** (the GOLD lapis/ticket buttons), else the base
        # `GachaDetailScene` (the BLUE friend button). So a pay banner MUST send 1 or it wears the friend
        # button; a step-up sends 2 (0x1cea458). An authored `gacha_layout` still wins.
        layout = b.get("gacha_layout")
        if layout is None:
            kind = str(_primary_cost(b).get("kind") or "")
            if str(b.get("type")) == "step":
                layout = 2
            elif kind not in ("free", "friend_point"):
                layout = 1                       # lapis / ticket / gil -> the pay detail scene
        if layout:
            row["7xBNR2MH"] = str(layout)
        mst.append(row)
        steps = b.get("steps") or []
        if steps:
            # One `GachaDetailMst` row per step, detail id `gachaId*10 + step` (the shipped convention for
            # the step-up ids, e.g. 810520 -> 8105201..5). Each step carries its own costInfo + draws.
            for i, step in enumerate(steps):
                sid = str(int(b["id"]) * 10 + i + 1)
                detail.append(_gacha_detail_row(b, art, sid, step.get("cost"), step.get("draws", 1)))
        else:
            swap = GACHA_MULTI_BTN == "swap"      # see the constant's comment: an exeType probe, not a feature
            detail.append(_gacha_detail_row(b, art, b.get("detail_id", ""),
                                            b.get("cost"), b.get("draws", 1),
                                            exe_type=("secondary" if swap else None),
                                            img_btn=b.get("img_btn_primary")))
            # A SECOND row (`exeType = 2`) is what produces the archive's three summon buttons (single /
            # 11-summons / ticket) -- see `_gacha_detail_row`'s docstring and `gacha_multi_button.md`. Opt-in
            # per banner via `secondary_offer`, because the shape is only PROVEN readable, not yet live-run.
            sec = b.get("secondary_offer")
            if GACHA_MULTI_BTN != "0" and isinstance(sec, dict) and sec:
                detail.append(_gacha_detail_row(b, art, gacha_secondary_detail_id(b),
                                                sec.get("cost") or b.get("cost"),
                                                # `exe_count` is the costInfo ENTRY [3] (the client's
                                                # `getExeCount()`/`getMaxPlayCount()`), which is NOT the same
                                                # thing as `draws` (the units we grant). Keep it 1: with
                                                # exeCount != 1 the pay multi button prints
                                                # `useCost * min(have/useCost, exeCount)` AND renders the
                                                # `GACHA_MULTI` "Nx Summon" caption -- neither is retail. The
                                                # "11" is the button art; the 11 units come from `draws`.
                                                # `docs/notes/gacha_multi_button.md` SS15.
                                                sec.get("exe_count", sec.get("draws", 11)),
                                                exe_type=("primary" if swap else "secondary"),
                                                img_btn=sec.get("img_btn")))
        sched.append({"X1IuZnj2": str(b["id"]),
                      "Z0EN6jSh": str(sd.get("disp_order", 1)),
                      "w2A76noc": str(sd.get("banner_type", 0)),
                      "4yT7FUqj": _gacha_schedule_banner_string(_gacha_art(art, "banner"),
                                                                 _gacha_art(art, "detail_banner")),
                      "VjJQ51uG": str(window.get("from", "")),
                      "m8ivD4NX": str(window.get("to", "")),
                      "N2PkD9sy": str(window.get("from", ""))})
        if not has_panel:
            continue
        panel.append({"LFoU0M3v": str(b.get("panel_id", "")), "X1IuZnj2": str(b["id"]),
                      "vFqVXP19": str(b.get("panel_group_id", ""))})
        pool_rows = b.get("pool") if isinstance(b.get("pool"), list) else []
        for i, entry in enumerate(pool_rows):
            pgroup.append({"vFqVXP19": str(b.get("panel_group_id", "")),
                           "q4m8aKr0": str(b.get("detail_id", "")),
                           "7iJpH5zZ": _gacha_target_info(entry),
                           "Z0EN6jSh": str(i)})
    return {GACHA_MST_TAG: mst, GACHA_DETAIL_TAG: detail, GACHA_SCHEDULE_TAG: sched,
            GACHA_PANEL_TAG: panel, GACHA_PANEL_GROUP_TAG: pgroup}
def serve_gacha_info(spec, reply_id: str):
    """Attach the authored banner layer to the reply that opens the gacha screen.

    `GachaEntryScene::initConnect` (0x211d7ac) sends `GachaEntryRequest` after `runDownloadMstAndRes(true)`,
    so the reply to that request is where the client expects the list; `GachaInfoRequest` (from
    `GachaDetailScene::updateGachaInfoRequest` 0x213ccf8) is the detail-scene refresh. A reply with neither
    id is left byte-for-byte alone. `spec` may be None (no keyed reply) -- the groups alone are a valid body.
    """
    if reply_id not in GACHA_INFO_REPLIES:
        return spec
    banners = all_banners()
    if not banners:
        return spec
    out = dict(spec) if isinstance(spec, dict) else {}
    out.update(gacha_groups(banners))
    out[GACHA_USER_INFO_TAG] = State.player.gacha_user_rows(banners) if State.player else []
    out[GACHA_STEP_INFO_TAG] = State.player.gacha_step_rows(banners) if State.player else []
    _note(f"gacha: served {len(banners)} authored banner(s) on reply {reply_id}")
    return out
def _request_gacha(plaintext: str | None) -> dict:
    """The client's OWN `5yLD4dYI` request node (banner id / detail id / ticket / repeat count).

    `raw_decode`, NOT `json.loads`: the client's manual PKCS#7 pad leaves trailing pad bytes (the pad
    VALUE varies with length), so `json.loads` raises "Extra data" -- same reason `mission_id_from` uses
    raw_decode. A partial parse is harmless here: no node means the tutorial/forced-summon path.
    """
    if not plaintext:
        return {}
    try:
        body = json.JSONDecoder().raw_decode(plaintext)[0]
    except Exception:  # noqa: BLE001
        return {}
    rows = body.get(GACHA_TAG) if isinstance(body, dict) else None
    return rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
def _award_str(a):
    """One `UJz5QED2` entry: `<type>:<unitId>:<userUnitId>:<lv>`.

    `GachaResultScene::initialize` (`0x217b008`-`0x217b01c`) splits each entry on `:` and calls
    `UserUnitInfoList::getObjectWithUserUnitID(field[2])`; a 2-field `10:<unitId>` reads field[2] out of
    bounds and SIGSEGVs (SW-78, `docs/notes/gacha_multi_button.md` SS17). Field[3] is the fallback lv
    (`UnitDataUtil::createMaxStatusUnitInfoForConvertUnit` `0x217b0a0`). The instance id comes from the
    `grant_unit` that `roll_gacha`/`tutorial_gacha` already performed -- no second source.
    """
    base = f"10:{a['unit_id']}"
    if a.get("instance_id") is None:
        return base
    return f"{base}:{a['instance_id']}:{int(a.get('level') or 1)}"
GACHA_PLAYER_REFRESH_GROUPS = (
    "B71MekS8",  # UserUnitInfoResponse — full roster
    "TJ9eL80N",  # UnitEquipResponse — full per-unit equipment
    "5Eb0Rig6",  # UserPartyDeckInfoResponse — full normal party list
    "8PEB5o7G",  # UserActualInfoResponse — active normal party id
    "49rQB3fP", "gP9TW2Bf", "1S8P2u9f",  # beast deck / owned espers / pieces
)
def _gacha_player_refresh(out: dict) -> None:
    """Push the complete player-object projection after a real award.

    A gacha result tells the client which unit was awarded, but the roster, equipment and party
    singletons are separate response objects. Without these groups, a successful award can leave the
    result screen's unit object ahead of the lists used by the party editor. This is served data only;
    it does not patch or alter the client.
    """
    if State.player is None:
        return
    groups = State.player.groups()
    for tag in GACHA_PLAYER_REFRESH_GROUPS:
        out[tag] = groups[tag]
def serve_gacha(spec, plaintext: str | None = None):
    """Make a summon real: roll an AUTHORED banner and GRANT the result into the roster.

    Two paths. (1) A request naming an authored banner (`X1IuZnj2`) rolls that banner's pool at its rates,
    spends its cost (`PlayerState.roll_gacha`), and serves the drawn units. (2) A bare/zero request is the
    forced tutorial summon -- one authored award (SW-61). The award is `UJz5QED2` = `<type>:<id>` with type
    10 = unit; `zoU5J3Kq` is the effect pattern, which the client does NOT validate (`shop_gacha_scope.md`
    SS6) -- but an ABSENT pattern is the `GACHA_EFFECT_ERROR` popup, so it is always set on a real result.
    Each `UJz5QED2` entry is `<type>:<unitId>:<userUnitId>:<lv>` (`_award_str`), not `<type>:<id>`.
    """
    if not isinstance(spec, dict) or GACHA_TAG not in spec:
        return spec
    req = _request_gacha(plaintext)
    banner = next((b for b in all_banners() if str(b["id"]) == str(req.get("X1IuZnj2"))), None)
    out = copy.deepcopy(spec)
    if banner is not None:
        # The CLIENT's request tokens (verified live 2026-09-21, `out/requests/*`, GachaExeRequest):
        #   `2RtVPS3b` = the REPEAT COUNT (1 for a single, 10 for a 10x)  -- NOT the ticket id
        #   `zJ1A6HXm` = the TICKET ID the player chose (12 = F_TICKET_MST item 180, the 5-star EX ticket)
        # Reading `zJ1A6HXm` as the repeat count made a 1x ticket pull try to spend 12 tickets and refuse.
        # NOTE the RESPONSE class (`GachaExeResponse`) uses the opposite token semantics (2RtVPS3b =
        # `setTicketId`, zJ1A6HXm = `setRepeatCnt`; shop_gacha_scope.md S6), so the node below follows THAT.
        repeats = int(req.get("2RtVPS3b") or 0) or 1
        ticket_id = req.get("zJ1A6HXm")
        # WHICH ROW was tapped: the client names it in `nqzG3b2v` (the detail row id), because the button row is
        # `exeType`-partitioned. The 11-summons button sends repeats=1 -- the count lives in that row's own
        # `exeCount` -- so without this the pull would charge the primary offer (`gacha_multi_button.md` SS9).
        req_detail = str(req.get("nqzG3b2v") or "")
        offer = gacha_offer_for(banner, req_detail)
        _note(f"gacha: pull banner {banner['id']} row={req_detail or '?'} "
              f"({'secondary offer' if offer else 'primary'}) repeats={repeats} ticket={ticket_id}")
        res = State.player.roll_gacha(banner, repeats, ticket_id, offer=offer) if State.player else None
        if not res:
            _note(f"gacha: banner {banner['id']} pull x{repeats} (ticket {ticket_id}) refused "
                  f"(cost/affordability, or frozen) -- serving the request echo, no award")
            if State.player:
                out.update(State.player.currency_groups())   # let the client show the real balance
            return out
        detail_id = res["detail_id"]
        if offer is not None and req_detail:
            detail_id = req_detail                            # echo the row the player tapped
        _note(f"gacha: awarded {len(res['awards'])} draw(s) banner {banner['id']} row={detail_id} "
              f"({'secondary offer' if offer else 'primary'}) -- the count above is what the CLIENT asked for")
        if banner.get("steps"):   # the step's own detail row (gachaId*10 + step)
            detail_id = str(int(banner["id"]) * 10 + res["step"] + 1)
        node = {"X1IuZnj2": res["banner_id"], "nqzG3b2v": detail_id,
                "zJ1A6HXm": str(res["repeats"]),                 # GachaExeResponse: setRepeatCnt
                "2RtVPS3b": str(res.get("ticket_id") or 0),       # GachaExeResponse: setTicketId
                "UJz5QED2": ",".join(_award_str(a) for a in res["awards"]),
                "zoU5J3Kq": str(res["pattern_id"] or 1),
                # possibleCnt (UserGachaResult+0x24, `getPossibleCnt` 0x1ddf980). It GATES the multi/box
                # path: judgeExe errors at 0x21321b0 and onSceneVisible switches state when it is 0. Serve
                # the realised pull count (>0); see notes/response_field_map.md / gaps triage 2026-09-23.
                "oY0vAuE9": str(max(1, len(res["awards"])))}
        out[GACHA_TAG] = [node]
        # Refresh the balances / held-ticket count alongside the result, so the client's currency display
        # updates without a separate user-state request (the result reply carries no LhVz6aD2).
        if State.player:
            out.update(State.player.currency_groups())
            _gacha_player_refresh(out)
        step_note = f" step {res['step'] + 1}" if banner.get("steps") else ""
        print(f"    gacha: BANNER {res['banner_id']}{step_note} x{res['repeats']} -> {node['UJz5QED2']} "
              f"(pattern {node['zoU5J3Kq']}, ticket {res.get('ticket_id')}, "
              f"roster {len(State.player.data['roster'])}, stepNow {State.player.gacha_step(banner) + 1})",
              flush=True)
        return out
    master_id, rarity = gacha_tutorial_award()
    res = State.player.tutorial_gacha(master_id, rarity)
    rows = spec.get(GACHA_TAG)
    node = dict(rows[0]) if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
    node["3HriTp6B"] = res["unit_id"]
    node["UJz5QED2"] = _award_str(res)
    node["zoU5J3Kq"] = str(res["pattern_id"])
    node["oY0vAuE9"] = "1"        # possibleCnt: >0 so the multi/box path is not errored (see above)
    out[GACHA_TAG] = [node]
    _gacha_player_refresh(out)
    print(f"    gacha: awarded {res['unit_id']} (granted rarity {res['granted_rarity']}, pattern "
          f"{res['pattern_id']}) -> roster now {len(State.player.data['roster'])} unit(s)", flush=True)
    return out
