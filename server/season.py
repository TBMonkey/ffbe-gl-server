#!/usr/bin/env python3
"""season.py — the server's authored SEASON: one selector over the levers we already serve.

A season is a **selection**, not content: every value names an id that exists in a shipped table or a reply
class, and every facet projects onto a lever that already exists. The design, the data inventory and the
per-icon test plan are `docs/notes/server_seasons.md` — this module is the mechanism, that note is the home.

Facets today:
  * `gacha_campaign` -> `GACHA_CAMPAIGN` (the existing campaign slice; `gacha_campaigns.md`)
  * `badges`         -> the `merqbE18` `EventBadgeInfo` value (the HOME tiles' base art + event-announce
                       strips; `home_map_button.md`, `home_screen_icons.md`). `default_badges` is the
                       invariant baseline; a season's own rows override it by badge key.
  * `open.switches`  -> in-memory switch bits (NOT persisted; a season must not accumulate in a slot)
  * `login_month`    -> the daily-login calendar's month: the key into `server/login_calendar.json`
                       (`2019-Jul`), which `serve_login_bonus` serves with the player's own day counter.
                       An explicit `login_month` wins; otherwise it is DERIVED from the season key when that
                       is a `YYYY-MM` (`2019-07` -> `2019-Jul`, the table's own key shape). A season whose
                       month has no table falls back to `LOGIN_MONTH` and says so in the log.
  * `login_bonus`    -> the CATCH-UP bonus/gift schedules a new account is given: the cumulative
                       total-login ladder and one special-login campaign, both selected from the SHIPPED
                       tables by `harness/gen_catchup_bonus.py` (`server/catchup_bonus.json`), plus the
                       AUTHORED welcome grant. `default_login_bonus` is the baseline (the ladder + the
                       welcome), merged key-wise the same way `default_badges` is; a season only states the
                       campaign it wants. Home: `docs/notes/catchup_bonus_family.md`.
  * `home_icons` / `events` / `notices` -> recorded and logged, applied per-feature when that feature lands
"""
from __future__ import annotations

import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
SEASONS_FILE = os.path.join(HERE, "seasons.json")

# The keys `HomeMainMenuIcon::setIcon` looks up (see `home_map_button.md`). A badge row whose key is not one
# of these is harmless but suspicious, so it is reported rather than silently ignored.
BADGE_KEYS = frozenset({
    "HOME_MAINMENU_ARENA_ICON",
    "HOME_MAINMENU_DIMENSIONS_ICON",
    "HOME_MAINMENU_WORLD_ICON",
    "HOME_MAINMENU_ARMS_ICON",
    "HOME_MAINMENU_DIMENSIONS_EVENT_ICON",
    "HOME_MAINMENU_WORLD_EVENT_ICON",
})

# "no season": leave every lever at its own default. Also the fallback when a named season is unknown.
OFF = ("off", "none", "0", "all")

# `server/login_calendar.json` keys months as `YYYY-Mon`; a season key is `YYYY-MM`. One mapping, here.
MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _login_month(spec: dict, key: str) -> str:
    """The calendar month a season selects: explicit `login_month`, else derived from a `YYYY-MM` key.

    Nobody has to restate it: `2019-07` already names July 2019, and the community table keys months exactly
    that way (`2019-Jul`). A season that wants a different month, or none, says so explicitly -- `"off"`
    means "leave the calendar's own default alone".
    """
    explicit = str(spec.get("login_month") or "").strip()
    if explicit:
        return "" if explicit.lower() in OFF else explicit
    m = re.match(r"^(\d{4})-(\d{2})$", str(key))
    if not m:
        return ""
    no = int(m.group(2))
    return f"{m.group(1)}-{MONTH_ABBR[no - 1]}" if 1 <= no <= 12 else ""


def _merge_events(spec: dict) -> dict:
    """`events` normalised: `off`/`false` drops that key, `_`-prefixed keys are documentation."""
    out: dict = {}
    for k, v in (spec or {}).items():
        if k.startswith("_") or v is None or v is False:
            continue
        if isinstance(v, str) and v.strip().lower() in OFF:
            continue
        out[k] = v
    return out


def _login_bonus(spec: dict) -> dict:
    """The `login_bonus` facet, normalised: `off`/`false` anywhere means "not this schedule".

    The facet SELECTS shipped schedules (`server/catchup_bonus.json`) and carries the one authored number
    set (the fresh-account welcome), so the normalisations are: a falsey/`off` value disables that key, a
    `welcome` of `"off"` disables the welcome, and everything else is passed through untouched. Nothing here
    invents a schedule -- a campaign id that does not resolve is reported by the server, not replaced.
    """
    out: dict = {}
    for k, v in (spec or {}).items():
        if k.startswith("_"):
            continue                                     # `_label` and friends are documentation
        if v is None or v is False:
            continue
        if isinstance(v, str) and v.strip().lower() in OFF:
            continue
        if k == "welcome" and isinstance(v, dict):
            v = _welcome_amounts({"welcome": v})
            if not v:
                continue
        out[k] = v
    return out


def _welcome_amounts(lb: dict) -> dict:
    """`{currency: int}` from `lb['welcome']` -- only the four currencies the player state actually holds.

    A welcome that names an unknown currency is DROPPED, not coerced: `PlayerState.currencies` has exactly
    four fields, so a typo would otherwise become a silent no-op grant that looks like it worked.
    """
    raw = (lb or {}).get("welcome")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for cur, amount in raw.items():
        cur = str(cur).strip()
        if cur in ("_label", "label"):
            continue
        try:
            n = int(amount)
        except (TypeError, ValueError):
            continue
        if n > 0 and cur in WELCOME_CURRENCIES:
            out[cur] = n
    return out


# The player state's `currencies` keys (player_state.py `default_state`). The one list.
WELCOME_CURRENCIES = ("lapis_free", "lapis_paid", "gil", "friend_point")


class Season:
    def __init__(self, key: str, spec: dict):
        self.key = key
        spec = spec or {}
        self.spec = spec
        self.label = str(spec.get("label") or key)
        self.gacha_campaign = str(spec.get("gacha_campaign") or "")
        self.badges = [str(b) for b in (spec.get("badges") or [])]
        self.home_icons = spec.get("home_icons") or {}
        self.events = _merge_events(spec.get("events") or {})
        self.open = spec.get("open") or {}
        self.notices = spec.get("notices") or {}
        self.login_month = _login_month(spec, key)
        # `_login_bonus` is idempotent (`load()` already normalised the merge), so a Season built directly
        # in a test gets the same contract as one built by `load()`.
        self.login_bonus = _login_bonus(spec.get("login_bonus") or {})

    # --- the catch-up bonus plan (`login_bonus`), read by `deliver_catchup_rewards` in ffbe_player.py ---

    @property
    def catchup_on(self) -> bool:
        """True when the season asks for any catch-up schedule at all (ladder, campaign or welcome)."""
        lb = self.login_bonus
        return bool(lb.get("total_ladder") or lb.get("campaign") or _welcome_amounts(lb))

    @property
    def sp_campaign_id(self) -> str:
        """The `F_LOGIN_BONUS_SP_REWARD_MST` group id this season replays ("" = none)."""
        return str(self.login_bonus.get("campaign") or "")

    @property
    def welcome(self) -> dict:
        """The AUTHORED fresh-account grant: `{currency: amount}`. Empty when off."""
        return _welcome_amounts(self.login_bonus)

    @property
    def panel_quest_groups(self) -> list:
        """The `F_PANEL_QUEST_GROUP_MST` group ids this season shows (SW-121). Empty = the screen stays shut.

        Selection only, like every other facet: the rows themselves are the shipped tables, read at serve time
        and never copied (`ffbe_player.serve_panel_quest`). A group id that does not resolve is reported and
        dropped, not replaced with an invented one.
        """
        out = []
        for gid in (self.events.get("panel_quest_groups") or []):
            try:
                out.append(int(gid))
            except (TypeError, ValueError):
                continue
        return out

    @property
    def sp_challenge_groups(self) -> list:
        """The `F_SP_CHALLENGE_MST` group ids this season shows (2026-09-27). Empty = the default applies.

        Selection only, like every other facet: the rows are the shipped table, read at serve time and never
        copied (`ffbe_player.serve_sp_challenge`). A group id that does not resolve is reported and dropped,
        not replaced -- the same contract as `panel_quest_groups`, because a facet that invents content is how
        "the screen shows nothing" becomes unexplainable. 1001 is Rookie Quests, 1101 ADVANCED Quests, 1201
        Expert, 4001 Parameter, and 9001 is the nameless hub (its 100 rows are exactly the table's nameless
        ones, so it can only ever be a selection mistake).
        """
        out = []
        for gid in (self.events.get("sp_challenge_groups") or []):
            try:
                out.append(int(gid))
            except (TypeError, ValueError):
                continue
        return out

    @property
    def daily_quests(self) -> dict:
        """The DAILY QUEST selection: `{pools, count}` -- which pools today's set is drawn from and how many.

        Selection only, like `panel_quest_groups`: the rows themselves are `server/daily_quests.json`
        (generated from the SHIPPED text tables), and the id space is the shipped one. A pool name that does
        not exist is reported and dropped by the server, never replaced -- the same contract as every other
        facet, because a facet that invents content is how "the screen shows nothing" becomes unexplainable.
        """
        from ffbe_config import DAILY_QUEST_DEFAULT_COUNT, DAILY_QUEST_DEFAULT_POOLS
        raw = self.events.get("daily_quests")
        pools, count = [], 0
        if isinstance(raw, dict):
            pools = [str(p) for p in (raw.get("pools") or [])]
            try:
                count = int(raw.get("count") or 0)
            except (TypeError, ValueError):
                count = 0
        if not pools and not count:
            # No season, or a season that says nothing: the floor, because the daily quest is reachable by
            # DEFAULT (it is a mode of the SP-challenge list screen, not a gated tile) and a feature that is
            # reachable by default must be SERVED by default -- idiom 39, learned the hard way on the panel
            # quest, where a season-only facet left the default run with a tappable icon and an error.
            return {"pools": list(DAILY_QUEST_DEFAULT_POOLS), "count": int(DAILY_QUEST_DEFAULT_COUNT),
                    "defaulted": True}
        return {"pools": pools or list(DAILY_QUEST_DEFAULT_POOLS),
                "count": count or int(DAILY_QUEST_DEFAULT_COUNT), "defaulted": False}

    @property
    def badge_string(self) -> str:
        """The comma-joined `merqbE18` value (`parseBadgeInfo` splits on `,` then `:`)."""
        return ",".join(self.badges)

    def switch_sets(self) -> dict:
        """`{type: {bits}}` the season forces on, in memory only. A bad bit is skipped, not guessed."""
        out: dict[str, set] = {}
        for stype, bits in (self.open.get("switches") or {}).items():
            good = set()
            for b in bits or []:
                try:
                    good.add(int(b))
                except (TypeError, ValueError):
                    continue
            out[str(stype)] = good
        return out

    def unknown_badge_keys(self) -> list:
        keys = []
        for record in self.badges:
            head = record.split(":", 1)[0]
            if head not in BADGE_KEYS:
                keys.append(head)
        return keys

    def summary(self) -> str:
        bits = sum(len(v) for v in self.switch_sets().values())
        return (f"season {self.key!r} ({self.label}): gacha_campaign="
                f"{self.gacha_campaign or '-'}, {len(self.badges)} badge(s), {bits} forced switch bit(s), "
                f"login_month={self.login_month or '-'}, "
                f"icons.on={len(self.home_icons.get('on') or [])}"
                + (f", catchup(ladder={bool(self.login_bonus.get('total_ladder'))}, "
                   f"sp_campaign={self.sp_campaign_id or '-'}, "
                   f"welcome={','.join(f'{k} {v}' for k, v in self.welcome.items()) or '-'})"
                   if self.catchup_on else "")
                + (f", daily_quests(pools={'/'.join(self.daily_quests['pools'])}, "
                   f"count={self.daily_quests['count']}"
                   f"{', DEFAULTED' if self.daily_quests['defaulted'] else ''})"))


def _load_file(path: str = SEASONS_FILE) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) else {}
    except FileNotFoundError:
        return {}
    except ValueError as exc:  # noqa: BLE001 - a bad seasons file must not kill the server
        print(f"season: {path} unreadable ({exc!r}) -- no season", flush=True)
        return {}


def list_keys(path: str = SEASONS_FILE) -> list:
    return sorted((_load_file(path).get("seasons") or {}).keys())


def load(name: str | None = None, path: str = SEASONS_FILE) -> "Season | None":
    """Resolve the active season: explicit `name` > `SEASON` env > the file's `default`.

    Returns None for `off`/unknown-key (with a message for the unknown case), so every caller can treat
    None as "leave the levers alone".
    """
    doc = _load_file(path)
    seasons = doc.get("seasons") or {}
    key = (name if name is not None else os.environ.get("SEASON", "")).strip()
    if not key:
        key = str(doc.get("default") or "")
    if not key or key.lower() in OFF:
        return None
    spec = seasons.get(key)
    if spec is None:
        known = ", ".join(sorted(seasons)) or "none authored"
        print(f"season: unknown season {key!r} -- no season (known: {known})", flush=True)
        return None
    # `default_badges` is the invariant baseline (the rows every season needs -- e.g. the Map tile has no
    # fallback art) and a season's own `badges` OVERRIDE it by key. One home for the invariant rows, so a
    # new baseline badge is added in exactly one place; a season that omits `badges` inherits the baseline.
    spec = dict(spec)
    spec["badges"] = _merge_badges(doc.get("default_badges") or [], spec.get("badges"))
    # Same shape for the catch-up facet: `default_login_bonus` is the baseline every season inherits (the
    # cumulative ladder + the welcome grant), and a season states only what DIFFERS -- normally just the
    # campaign id. Key-wise merge, so `{"campaign": 1033}` adds to the baseline instead of replacing it.
    spec["login_bonus"] = _merge_login_bonus(doc.get("default_login_bonus") or {}, spec.get("login_bonus"))
    # Same for the event facets: `default_events` is the baseline every season inherits (currently the panel
    # quest groups), merged key-wise, so a season lists only the layers it adds.
    spec["events"] = _merge_events({**(doc.get("default_events") or {}), **(spec.get("events") or {})})
    return Season(key, spec)


def _merge_badges(baseline, overrides) -> list:
    """Baseline rows first, then `overrides`; a same-key override replaces the baseline row in place."""
    by_key: dict[str, str] = {}
    order: list[str] = []
    for record in list(baseline) + list(overrides or []):
        key = str(record).split(":", 1)[0]
        if key not in by_key:
            order.append(key)
        by_key[key] = str(record)
    return [by_key[key] for key in order]


def _merge_login_bonus(baseline, overrides) -> dict:
    """`default_login_bonus` overlaid by the season's own `login_bonus`, key-wise, then normalised.

    Same contract as `_merge_badges` and for the same reason: the baseline (the cumulative ladder + the
    welcome grant) is what every season wants, so it lives in ONE place and a season states only the
    campaign it wants. A season that genuinely wants a schedule OFF says `"total_ladder": "off"`, which
    `_login_bonus` then drops -- so "off" is an override, not a merge-away.
    """
    merged = dict(baseline or {})
    merged.update({k: v for k, v in (overrides or {}).items() if not str(k).startswith("_")})
    return _login_bonus(merged)
