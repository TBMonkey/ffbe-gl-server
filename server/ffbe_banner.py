#!/usr/bin/env python3
"""ffbe_banner.py — the HOME marquee (`sgHomeMarqueeInfoResponse`, tag `DxCN9SNZ`).

The marquee is the easiest home payload to serve because it is **100% server-authoritative**:
`sgHomeMarqueeManager::getBannerMstList()` (`0x1a4df94`) builds its list by iterating the manager's own
`sgHomeMarqueeInfo` records and never looks a banner up in a table, and
`sgHomeMarqueeManager::createAllBannerDownloadRequests` (`0x1a4d4e4`) then fetches each row's OWN url:

```
0x1a4d558  ldr x8,[x8,#0x48] ; vptr+0x38 -> getEndTime()
0x1a4d564  blr x8
0x1a4d568  cmp x0, x20        ; x20 = CommonUtils::getNowUnixTime() from 0x1a4d514
0x1a4d56c  b.lt -> next       ; an EXPIRED row is never even downloaded
0x1a4d578  ldr x9,[x8,#0x58] ; vptr+0x48 -> getDownloadUrl()
0x1a4d580  blr x9             ; the string goes straight into createRequest
0x1a4d684  add x1, sp, #0x50  ; arg1 = that url, VERBATIM
0x1a4d688  add x2, sp, #0x38  ; arg2 = getLocalPath() + _getBannerImage(info)
0x1a4d68c  mov w3, #0x3a98   ; 15000
0x1a4d694  bl FileDownloadManager::createRequest(url, localPath, 15000)
```

So the server owns the banner's start, end, image URL and version — the four tokens this module writes.
What it does NOT own is *which* banners are live: the shipped tables carry no banner dates at all
(`F_BANNER_MST` = 481 rows and not one date column), so "the active set" is a SELECTION, exactly like
`GACHA_CAMPAIGN`. That is why the selection lives in `server/banner_campaigns.json`
(`harness/gen_banner_campaigns.py`, gated by `--check`) and not in code.

**Three things here are OURS and are labelled as such, because presenting them as recovered data would
be a lie:**

1. **The window.** Every era in the table is in the PAST (the client's own banner cache spans 2022-2024,
   our client is 9.0.0 / 2024-06-30) and the client filters the list against `getNowUnixTime()`, so a real
   window would make every row invisible. The era's own dates are kept as PROVENANCE — which event the art
   belongs to — and the served window is rebased onto the run by `MARQUEE_WINDOW_DAYS`.
2. **Which era.** `BANNER_CAMPAIGN`, defaulting to the table's `default` (the latest era with joinable art).
3. **The undated promos.** Four of the eight cached banners are summon/ticket promos the community corpus
   does not date at all. They are real art with the client's own id/version/path, so they are served with
   every era — and they are what lets the carousel advance: **an era's dated rows alone are ONE banner**
   (measured: the best era, 2024-08, holds 1 event with art), and a one-row carousel cannot visibly rotate.

Docs: `docs/notes/home_screen_payloads.md` §11, §11.6, §11.7 · `docs/HOME_SCREEN_REFERENCE.md`
(the "marquee" trap — the Main bar is NOT a marquee).
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta

from ffbe_config import (BANNER_CAMPAIGN, BANNER_CAMPAIGN_FILE, MARQUEE_FLAG, MARQUEE_LOCATION_GATE,
                         MARQUEE_LOCATION_ID, MARQUEE_URL_BASE, MARQUEE_WINDOW_DAYS)
from ffbe_state import _note

__all__ = ["MARQUEE_GROUP", "MARQUEE_FLAG_KEY", "banner_selection", "marquee_rows", "serve_home_marquee"]

# The reply group and the GameSetting key. Both names come from the generated field map
# (`harness/response_field_map.json`, `harness/response_tag_map.json`), not from prose:
# `DxCN9SNZ` -> sgHomeMarqueeInfoResponse, and `P_GS_FEATURE_HOME_MARQUEE` -> GameSettingResponse
# (semantics "sgHomeMarqueeManager enable", `docs/notes/gamesetting_payload_tree.md` S1).
MARQUEE_GROUP = "DxCN9SNZ"
MARQUEE_FLAG_KEY = "P_GS_FEATURE_HOME_MARQUEE"

# The row's tokens. Only `twb8TQPx`/`ZkGp8XbQ` (the window), `TsZhcqbT` (the url) and `JZryxaYY` (the
# version/cache key) are recovered from the client's own reader (`sgHomeMarqueeInfoResponse::readParam`
# `0x19667dc`, size 0x3d8). The four `BannerMst` ints (`nZfbucEw` +0x40, `zRZY8dcE` +0x44, `3c8348af`
# +0x60, `uP4695k3` +0x9c) are NOT decoded — the note records them as plain members with no known reader,
# so they are emitted as the neutral 0 the constructor already holds. Emitting them at all is deliberate:
# `uP4695k3` is compound `"<int>:<string>"` and its string half calls `BannerMst::setUrl`, so it is the
# one place the image name could reach the catalogue.
_BANNER_ID_TOKEN = "nZfbucEw"      # -> BannerMst+0x40 (atoi). Also the `_getBannerImage` filename key.
_BANNER_VERSION_TOKEN = "JZryxaYY"  # -> +0x24, atoi. The client's cache key (`marquee_banner.ver`).
_START_TOKEN = "twb8TQPx"           # -> +0xd0, CommonUtils::convertToTime
_END_TOKEN = "ZkGp8XbQ"             # -> +0xd8, CommonUtils::convertToTime
_URL_TOKEN = "TsZhcqbT"             # -> +0xe0, setDownloadUrl(string)
_MST_URL_TOKEN = "uP4695k3"         # -> +0x9c, compound "<gate>:<locationId>" -- BOTH halves are gates
_MST_A_TOKEN = "zRZY8dcE"           # -> +0x44
_MST_B_TOKEN = "3c8348af"           # -> +0x60

# The window's on-wire spelling. `CommonUtils::convertToTime` (`0x136fd10`) parses it on the client; the
# same shape is what every time field in `server/responses.json` already uses (272 of them, measured),
# so a marquee row is spelled like the rest of our replies rather than inventing a second dialect.
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

_BANNER_DOC: dict | None = None
_SELECTION_LOGGED: list = [False]


def _doc() -> dict:
    """The era table, loaded once. A missing/broken table must not kill the reply — it serves nothing."""
    global _BANNER_DOC
    if _BANNER_DOC is None:
        try:
            with open(BANNER_CAMPAIGN_FILE, encoding="utf-8") as fh:
                _BANNER_DOC = json.load(fh)
        except (OSError, ValueError) as exc:  # noqa: BLE001 - a bad table must not kill the reply
            _note(f"marquee: {BANNER_CAMPAIGN_FILE} unreadable ({exc!r}) -- serving NO banners")
            _BANNER_DOC = {}
    return _BANNER_DOC or {}


def banner_selection() -> tuple[str, list]:
    """`(label, rows)` for the active selection, mirroring `ffbe_gacha.campaign_selection`.

    The key is `BANNER_CAMPAIGN`; unset means the table's own `default` era, so a harness/client reset
    cannot silently lose the selection (the same reason the gacha's default lives in a file).
    `off`/`0`/`none` serves NOTHING — which is the OPPOSITE of the gacha's `BANNER_CAMPAIGN_OFF`, where
    "off" means "no *filter*, i.e. everything". Here a filter always exists (the era), so "off" has to
    mean "no marquee", and `all` is the escape that serves every era.
    """
    doc = _doc()
    key = str(BANNER_CAMPAIGN or "").strip()
    if key.lower() in ("off", "0", "none"):
        return "off", []
    if not key:
        key = str(doc.get("default") or "")
    promos = [p for p in (doc.get("promos") or []) if isinstance(p, dict) and p.get("file")]
    if key.lower() == "all":
        rows = [e for era in (doc.get("eras") or {}).values() for e in era.get("events") or [] if e.get("art")]
        return "all", _with_promos(rows, promos)
    if key.lower() == "promos":
        return "promos", _with_promos([], promos)
    era = (doc.get("eras") or {}).get(key)
    if not era:
        known = sorted(doc.get("eras") or {})
        _note(f"marquee: no era {key!r} ({len(known)} known: {', '.join(known[:8])}...) -- serving NO banners")
        return key, []
    rows = [e for e in era.get("events") or [] if e.get("art")]
    return key, _with_promos(rows, promos)


def _with_promos(rows: list, promos: list) -> list:
    """Era rows + the undated promos, in one order (dated first, then promos by banner id).

    The promos ride with EVERY era because nothing dates them — that is stated in the table's own
    `kind` field — and because an era's dated rows alone are one banner, which cannot rotate.
    """
    seen = {str(r.get("art", {}).get("banner_id")) for r in rows if isinstance(r.get("art"), dict)}
    out = list(rows) + [p for p in promos if str(p.get("banner_id")) not in seen]
    return sorted(out, key=lambda r: str((r.get("art") or {}).get("banner_id") or r.get("banner_id") or ""))


def _now() -> datetime:
    """The clock, read through the `time` module and NOT `datetime.now()`.

    The reply golden replays every capture and hashes the served body, so a wall-clock read makes the
    golden un-saveable: the marquee window is a function of now, so every `--save` run writes a different
    stamp for `PBSP9qn5` and the gate is red immediately after it is green. `harness/server_regression.py`
    pins a `FrozenClock` over every server module whose `time` name has `strftime` -- a module that reads
    `datetime.now()` escapes that pin silently, which is exactly how the login calendar's day key started
    drifting after the Phase 3 split moved its function. So: go through `time`, and the harness owns the
    instant. (`FrozenClock` exposes `time/gmtime/localtime/strftime`; a call it lacks is a loud
    AttributeError, which is the intended way to find a new entry point.)
    """
    lt = time.localtime()
    return datetime(lt.tm_year, lt.tm_mon, lt.tm_mday, lt.tm_hour, lt.tm_min, lt.tm_sec)


def _stamp(dt: datetime) -> str:
    return dt.strftime(TIME_FORMAT)


def _row(entry: dict, now: datetime) -> tuple[dict, dict]:
    """`(wire_row, provenance)` for one entry.

    Two values, not one dict with `_`-prefixed keys: the wire row is what the client parses and a stray
    key is a field the reader does not know, and the provenance is what the note and the log line are
    about. Keeping them apart means there is no code path on which provenance can reach the wire.
    """
    art = entry.get("art") if isinstance(entry.get("art"), dict) else entry
    banner_id = str(art.get("banner_id") or "")
    # The URL's basename is the file's OWN name in the client's store (`HomeBanner_<id>.png`), NOT the
    # remote name Square served (`FFBELandSwarmingwithOmegaSpawn.png`, the one recorded in the client's
    # `marquee_banner.ver`). Those differ for every row, and the CDN handler resolves banner art by
    # BASENAME -- so serving the remote name 404s for every banner. Caught by curling the exact served URL
    # before asking for a driven run, which is the whole point of that check: the reply looked perfect.
    served_as = os.path.basename(art.get("store_path") or "") or art.get("file") or ""
    url = f"{MARQUEE_URL_BASE.rstrip('/')}/{served_as}" if served_as else ""
    served_start = now - timedelta(days=1)
    served_end = now + timedelta(days=MARQUEE_WINDOW_DAYS)
    row = {
        _BANNER_ID_TOKEN: banner_id,
        _BANNER_VERSION_TOKEN: str(art.get("banner_version") or 0),
        _START_TOKEN: _stamp(served_start),
        _END_TOKEN: _stamp(served_end),
        _URL_TOKEN: url,
        _MST_A_TOKEN: "0",
        _MST_B_TOKEN: "0",
        # `uP4695k3` is `"<gate>:<locationId>"` and `_canTransit` reads BOTH: `info+0xcc` must equal 10 and
        # the string half must `StrToInt` to a location `FunctionMstList::isLocationValid` accepts. Serving
        # "0:<url>" here -- the obvious reading, and what this module did first -- makes `_canTransit` fail,
        # so `getBannerMstList` drops EVERY row and the marquee is empty while every log line looks healthy.
        _MST_URL_TOKEN: f"{MARQUEE_LOCATION_GATE}:{MARQUEE_LOCATION_ID}",
    }
    provenance = {
        "banner_id": banner_id,
        "name": entry.get("name") or art.get("derived_name") or "(unnamed)",
        "kind": "dated event" if entry.get("start") else "undated promo (window AUTHORED)",
        "real_start": entry.get("start"),
        "real_end": entry.get("end"),
        "remote_name": art.get("file"),
        "served_start": _stamp(served_start),
        "served_end": _stamp(served_end),
        "url": url,
    }
    return row, provenance


def marquee_rows(now: datetime | None = None) -> list:
    """The wire rows for the active selection. Empty when the feature is off."""
    rows, _ = _rows_with_provenance(now)
    return rows


def _rows_with_provenance(now: datetime | None = None) -> tuple[list, list]:
    label, entries = banner_selection()
    if not entries:
        return [], []
    now = now or _now()
    built = [_row(e, now) for e in entries]
    if not _SELECTION_LOGGED[0]:
        _SELECTION_LOGGED[0] = True
        _note(f"marquee campaign={BANNER_CAMPAIGN or label + ' (table default)'} flag={MARQUEE_FLAG}: "
              f"serving {len(built)} banner(s) on {MARQUEE_GROUP} "
              f"[{', '.join(p['name'][:34] for _, p in built)}] "
              f"window {built[0][1]['served_start']} .. {built[0][1]['served_end']} (AUTHORED)")
    return [r for r, _ in built], [p for _, p in built]


def serve_home_marquee(spec, reply_id: str | None = None):
    """Fill `DxCN9SNZ` on any reply that carries it, and turn `P_GS_FEATURE_HOME_MARQUEE` on.

    Two carriers, one function, because they are one feature: the flag is read at the GameSetting reply
    (boot step 1) and the rows at the marquee reply (boot step 10), and a marquee with the flag off draws
    nothing however many rows the reply carries.

    `MARQUEE_FLAG` has THREE values, and the third exists because of a crash, not a feature:
    `1` (default) the feature; `0` nothing — today's behaviour, byte for byte; and **`flag`, the flag alone
    with no rows**. `P_GS_FEATURE_HOME_MARQUEE=1` turns on `sgHomeMarqueeManager::update`'s window detector
    and the `HomeScene::updateEvent` path that wraps `updateBannerDownloadStatus` + `setGateEventBanner` —
    a code path this harness has never run — so it is the FIRST suspect when the client faults on a marquee
    run, and it cannot be bisected away from the rows while rows imply the flag. `MARQUEE_FLAG=flag` is that
    arm, and it costs one env var and no run beyond the server restart
    (`harness/restart_server.sh MARQUEE_FLAG=flag`). The skill's lesson is the reason: two crash classes
    here were written off as "the emulator" because nobody built the arm that separates the enable from the
    content.

    **The two carriers are found two different ways, and conflating them is a silent no-op** -- caught by
    the reply golden, not by a screen. `DxCN9SNZ` is a GROUP: `spec["DxCN9SNZ"]` IS the list of banner
    rows, so the carrier is recognised by the group KEY at the top level, and the payload we ship today is
    the empty list `{"DxCN9SNZ": []}` -- a "does any row contain this key?" test finds nothing there and
    the transform quietly does nothing while every other reply changes. `P_GS_FEATURE_HOME_MARQUEE` is
    the opposite shape: a TOKEN inside a row of the `PJm9d197` group, so it is found by scanning rows.
    (`server_shape_coverage.md` records the same three-way idiom disagreement elsewhere.)

    Returns a COPY when it changes anything, so the loaded payload table is never mutated (the reply
    golden gate asserts that; see `harness/server_regression.py`).
    """
    if MARQUEE_FLAG not in ("1", "flag"):
        return spec
    rows = marquee_rows()
    # With `MARQUEE_FLAG=1` an empty selection means no marquee, and the flag must go with it: flipping it
    # alone would leave the client with the marquee ENABLED and an empty list -- not the state the feature is
    # off in, which is what the A/B baseline has to be. `flag` is the one value that deliberately does the
    # opposite. (The selftest's `off` case is this; it caught the flag flipping regardless.)
    if (not rows and MARQUEE_FLAG != "flag") or not isinstance(spec, dict):
        return spec
    has_group = isinstance(spec.get(MARQUEE_GROUP), list)
    flagged = [key for key, value in spec.items()
               if isinstance(value, list) and any(isinstance(r, dict) and MARQUEE_FLAG_KEY in r for r in value)]
    if not has_group and not flagged:
        return spec
    out = dict(spec)
    if has_group:
        out[MARQUEE_GROUP] = rows
    for key in flagged:
        out[key] = [dict(r, **{MARQUEE_FLAG_KEY: "1"}) if isinstance(r, dict) and MARQUEE_FLAG_KEY in r
                    else r for r in spec[key]]
    if reply_id and (has_group or flagged):
        _note(f"marquee: {reply_id} carries {len(rows)} banner row(s) on {MARQUEE_GROUP}"
              if has_group else f"marquee: {reply_id} carries no {MARQUEE_GROUP} group (rows NOT sent on it)")
        if flagged:
            _note(f"marquee: {MARQUEE_FLAG_KEY}=1 written on {', '.join(flagged)}")
    return out


def _selftest() -> int:
    """Pure-function checks: the selection, the row shape, and the two failure modes that would make a
    driven run look like a client bug. Every case below names the defect it is a regression for.

    The knobs are patched in THIS module's globals, which is where the lookup happens (idiom #35:
    patching the defining module reported `indistinguishable` once already).
    """
    import copy as _copy
    import sys as _sys
    from datetime import datetime as _dt

    self_mod = _sys.modules[__name__]
    g = globals()
    rc = 0
    ran = 0

    def want(label, got, expected):
        nonlocal rc, ran
        ran += 1
        ok = got == expected
        rc += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} marquee: {label}"
              + ("" if ok else f"\n       got  {got!r}\n       want {expected!r}"))

    now = _dt(2026, 10, 1, 12, 0, 0)

    def sel(key):
        """Run a selection with `BANNER_CAMPAIGN` set to `key`, restoring it after."""
        was = g["BANNER_CAMPAIGN"]
        g["BANNER_CAMPAIGN"] = key
        try:
            return self_mod.banner_selection()
        finally:
            g["BANNER_CAMPAIGN"] = was

    # --- the selection -------------------------------------------------------------------
    label, entries = sel("")
    want("unset picks the table's default era", label, self_mod._doc().get("default"))
    want("...and that era has art (an era without art renders an empty marquee, i.e. today's bug)",
         sum(1 for e in entries if e.get("art")), 1)
    want("...with the undated promos riding along, because a ONE-row carousel cannot rotate",
         len(entries) - sum(1 for e in entries if e.get("art")),
         len(self_mod._doc().get("promos") or []))
    want("...every entry has a file to serve", all((e.get("art") or e).get("file") for e in entries), True)
    want("...and no banner id appears twice",
         len({str((e.get("art") or e).get("banner_id")) for e in entries}), len(entries))

    want("`all` serves more than one era does",
         len(sel("all")[1]) > len(sel("2024-08")[1]), True)
    want("`promos` serves only the undated promos",
         len(sel("promos")[1]), len(self_mod._doc().get("promos") or []))
    want("`off` serves nothing", sel("off")[1], [])
    want("an UNKNOWN era serves nothing rather than everything -- the gacha's `all` reading, which is "
         "safe there, would put 8 banners on screen here", sel("1999-01")[1], [])

    # --- the row ------------------------------------------------------------------------
    row, prov = self_mod._row({"name": "X", "start": "2024-08-01", "end": "2024-10-31",
                               "art": {"banner_id": 13630, "banner_version": 2, "file": "F.png",
                                       "store_path": "homeMarquee/HomeBanner_13630.png"}}, now)
    want("the url is the row's own, on the REDIRECTED host (the cloudfront url in the client's own "
         "marquee_banner.ver is NOT redirected, so serving it sends the client to the internet)",
         row["TsZhcqbT"], "https://v900-lapis.gumi.sg/banner/en/sd/static_banner/marquee/HomeBanner_13630.png")
    want("...under the file's OWN name in the store, not Square's remote name -- the CDN handler resolves "
         "banner art by basename and those two differ for every row, so the remote name 404s (found by "
         "curling the served URL: a perfect reply that fetched nothing)",
         prov["remote_name"], "F.png")
    want("the window is rebased onto the run, because every era in the table is in the past",
         (row["twb8TQPx"], row["ZkGp8XbQ"]), ("2026-09-30 12:00:00", "2026-10-22 12:00:00"))
    want("the era's own dates are kept as provenance, not served",
         (prov["real_start"], prov["real_end"], "2024-08-01" in json.dumps(row)),
         ("2024-08-01", "2024-10-31", False))
    want("the version is the client's own cache key", row["JZryxaYY"], "2")
    want("the compound token is `<gate>:<locationId>`, and BOTH halves are gates `_canTransit` reads",
         row["uP4695k3"], "10:601")
    # `_canTransit` (0x1a4ce48) is what makes a row displayable, and both of its gates are values the REAL
    # server chose that we have never captured -- so they are authored, and each is pinned here because a
    # wrong one produces an empty marquee with a healthy-looking server log.
    want("the gate half is the literal `_canTransit` demands (cmp w8,#0xa)",
         row["uP4695k3"].split(":")[0], "10")
    want("the location half resolves through isLocationValid -> FUNCTION_REEL_DIMENSION, which is on "
         "in the shipped F_FUNCTION_MST", row["uP4695k3"].split(":")[1], "601")
    g = globals()
    g["MARQUEE_LOCATION_GATE"] = "0"
    wrong = self_mod._row({"art": {"banner_id": 1, "banner_version": 1, "file": "f.png"}}, now)[0]
    want("...so a gate the client rejects is visible in the payload, not only on screen",
         wrong["uP4695k3"].split(":")[0], "0")
    g["MARQUEE_LOCATION_GATE"] = "10"
    want("no provenance key can reach the wire",
         sorted(self_mod.marquee_rows(now)[0]), sorted(row))
    # THE COLD-START SHAPE: TWO carriers, and the clear is what makes it safe. Boot step 1 (the game-setting
    # packet) carries the rows so `createAllBannerDownloadRequests` starts the image downloads ~14 requests
    # and tens of seconds before HOME is built; step 10 re-delivers them for display. They do NOT stack,
    # because `sgHomeMarqueeInfoResponse::onReadStarted` tail-calls `sgHomeMarqueeManager::onReadStarted`
    # (0x1a4cefc), which `Ref::release`es and NULLS every slot of the list before the rows re-parse. That is
    # also why the client sends `PBSP9qn5` twice per boot without ever showing ten slots.
    # Two carriers means TWO SPECS: a reply body is flat per reply key, so each carrier is its own spec.
    early = self_mod.serve_home_marquee({"DxCN9SNZ": []}, "OTX6Fmvu")
    late = self_mod.serve_home_marquee({"DxCN9SNZ": []}, "PBSP9qn5")
    want("both carriers are filled (step 1 starts the download, step 10 supplies the display)",
         (len(early["DxCN9SNZ"]) >= 2, len(late["DxCN9SNZ"]) >= 2), (True, True))
    # ...identical ONLY because both calls share one `now`. In a real boot the two carriers are served
    # 152 s apart (measured), so the rebased window stamps DIFFER between them -- which is harmless,
    # because the client's clear discards the step-1 rows before step 10 re-adds them. The property that
    # actually matters is the ROW SET (same ids, same urls, same count), not byte-equality across time.
    want("...with the same ROW SET on both carriers (identical bytes only because `now` is pinned; in a "
         "real boot the two are 152 s apart and the window stamps differ)",
         [r["nZfbucEw"] for r in early["DxCN9SNZ"]], [r["nZfbucEw"] for r in late["DxCN9SNZ"]])
    want("...and the same urls, which is what the download actually keys on",
         [r["TsZhcqbT"] for r in early["DxCN9SNZ"]], [r["TsZhcqbT"] for r in late["DxCN9SNZ"]])

    # THE TWO ROW-COUNT GATES, both in `getBannerMstList` (0x1a4df94) and both invisible on the wire:
    #   0x1a4e00c cmp x8,#2 ; b.lo -> FEWER THAN 2 rows returns an EMPTY array
    #   0x1a4e17c cmp w8,#9 ; b.le -> the list stops growing at ~10
    # So one banner is not a carousel. The era table's best era holds ONE dated row, which is why the
    # undated promos ride with it.
    rows_now = self_mod.marquee_rows(now)
    want("the served selection clears the client's 2-row minimum", len(rows_now) >= 2, True)
    want("...and stays under its ~10 cap", len(rows_now) <= 10, True)

    # --- the two silent failures --------------------------------------------------------
    base = {"DxCN9SNZ": [], "PJm9d197": [{"P_GS_FEATURE_HOME_MARQUEE": "0"}]}
    out = self_mod.serve_home_marquee(_copy.deepcopy(base), "PBSP9qn5")
    want("rows land on the marquee carrier", len(out["DxCN9SNZ"]) >= 2, True)
    want("...and the feature flag is turned on in the SAME reply chain: the flag is read on the boot "
         "packet (step 1) and the rows on the marquee request (step 10), so flag-without-rows and "
         "rows-without-flag both render nothing",
         out["PJm9d197"][0]["P_GS_FEATURE_HOME_MARQUEE"], "1")
    # THE CARRIER SHAPE. `DxCN9SNZ` is a GROUP (spec["DxCN9SNZ"] IS the row list) and what we ship today
    # is the EMPTY list, so a "does a row carry this key?" test finds nothing and the transform is a
    # silent no-op on the one reply that matters. Found by the reply golden (`server_regression.py`:
    # `OTX6Fmvu` changed and `PBSP9qn5` did not, which is backwards), pinned here so it cannot come back.
    want("the empty-list group is still recognised as a carrier",
         len(self_mod.serve_home_marquee({"DxCN9SNZ": []})["DxCN9SNZ"]) >= 2, True)
    untouched = {"someOther": [{"x": 1}]}
    want("a reply carrying neither group is returned untouched (identity, not a copy)",
         self_mod.serve_home_marquee(untouched) is untouched, True)
    spec = _copy.deepcopy(base)
    snap = _copy.deepcopy(base)
    self_mod.serve_home_marquee(spec, "PBSP9qn5")
    want("the loaded payload table is NOT mutated (the golden gate asserts exactly this)", spec, snap)

    was = g["BANNER_CAMPAIGN"]
    g["BANNER_CAMPAIGN"] = "off"
    off = self_mod.serve_home_marquee(_copy.deepcopy(base), "PBSP9qn5")
    want("`off` restores today's behaviour EXACTLY (empty list, flag \"0\") -- the A/B baseline",
         (off["DxCN9SNZ"], off["PJm9d197"][0]["P_GS_FEATURE_HOME_MARQUEE"]), ([], "0"))
    g["BANNER_CAMPAIGN"] = was
    was_flag = g["MARQUEE_FLAG"]
    g["MARQUEE_FLAG"] = "0"
    gated = self_mod.serve_home_marquee(_copy.deepcopy(base), "PBSP9qn5")
    want("MARQUEE_FLAG=0 is the whole-feature A/B (rows AND flag suppressed)",
         (gated["DxCN9SNZ"], gated["PJm9d197"][0]["P_GS_FEATURE_HOME_MARQUEE"]), ([], "0"))
    # THE CRASH-TRIAGE ARM. The flag enables a client code path this harness has never run, so it is the
    # first suspect on a marquee run -- and it cannot be separated from the rows while rows imply the flag.
    g["MARQUEE_FLAG"] = "flag"
    g["BANNER_CAMPAIGN"] = "off"
    arm = self_mod.serve_home_marquee(_copy.deepcopy(base), "PBSP9qn5")
    want("MARQUEE_FLAG=flag with BANNER_CAMPAIGN=off is the arm that isolates the ENABLE from the CONTENT",
         (arm["DxCN9SNZ"], arm["PJm9d197"][0]["P_GS_FEATURE_HOME_MARQUEE"]), ([], "1"))
    g["BANNER_CAMPAIGN"] = was
    g["MARQUEE_FLAG"] = was_flag

    print("marquee selftest: %d case(s), %d failure(s)" % (ran, rc))
    return rc


if __name__ == "__main__":
    raise SystemExit(_selftest())