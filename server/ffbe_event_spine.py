#!/usr/bin/env python3
"""ffbe_event_spine.py — the authored EVENT SPINE: Global event windows, runs, and quest first-runs.

**This layer is AUTHORED and REMOVABLE.** It is `[Community]` — the FFBE wiki's Update Schedule and event
pages, reconciled by the Exvius Revival fork — not `[FFBEWW]`. The shipped tables carry no dates at all, so
every value here is secondary. Nothing in the reply path imports this module: delete
`server/content/event_spine.json` and this file and the server is exactly what it was. `--selftest` enforces
that contract (it fails if another server module starts importing the module).

Why it exists: our own calendar (`server/community_events.json`) is a 2022+ wiki-window fetch; this spine
starts at 2016 and carries **runs** — the week markers a season needs — plus the 67 explicit Clash-of-Wills /
Dark-Visions windows and the first-run dates of quests that later runs added.

What it is not: a content source. Every mission and area id is checked against `F_MISSION_MST` by
`--validate`; the reward/currency/shop side is not modelled here.

    python3 server/ffbe_event_spine.py --report     # counts, years, the 2016-2021 fill, validation
    python3 server/ffbe_event_spine.py --validate   # every mission/area id exists in our shipped table
    python3 server/ffbe_event_spine.py --selftest   # the load + the removal contract (server_audit runs this)
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "content", "event_spine.json")
_DOC: dict | None = None
MISSING: dict = {}


def enabled() -> bool:
    """Whether a consumer may use the spine. Not consulted by anything today; a future season facet
    should gate on it so the layer stays one switch away from absent."""
    return str(os.environ.get("EVENT_SPINE", "0")).strip().lower() in ("1", "on", "true", "yes")


def load(path: str | None = None) -> dict:
    """The spine document, cached. A missing or unreadable file is an EMPTY spine, never an exception --
    that is the removal contract: the layer disappears without a trace."""
    global _DOC
    if path is None and _DOC is not None:
        return _DOC
    target = path or PATH
    try:
        with open(target, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"event spine: {target} not loadable ({exc!r}); the layer is absent", flush=True)
        doc = {}
    if path is None:
        _DOC = doc
    return doc


def meta() -> dict:
    return load().get("_meta") or {}


def events() -> dict:
    return load().get("events") or {}


def event(area) -> dict:
    return events().get(str(area)) or {}


def runs(area) -> list:
    return list((load().get("runs") or {}).get(str(area)) or [])


def windows(area) -> list:
    return [list(w) for w in ((load().get("windows") or {}).get(str(area)) or [])]


def missions(area) -> list:
    return list(event(area).get("missions") or [])


def _starts(area: str, row: dict) -> list[str]:
    """Every date at which this event opens: its release, its runs, and its explicit window starts."""
    out = []
    if row.get("release"):
        out.append(str(row["release"]))
    out.extend(str(d) for d in runs(area))
    out.extend(str(w[0]) for w in windows(area) if w)
    return sorted(set(out))


def launched_between(start: str, end: str) -> dict:
    """area -> the dates in [start, end] on which the event opened (inclusive, ISO dates)."""
    out = {}
    for area, row in events().items():
        hits = [d for d in _starts(area, row) if start <= d <= end]
        if hits:
            out[area] = hits
    return out


def season_events(month: str) -> dict:
    """`YYYY-MM` -> area -> opening dates inside that month."""
    if len(month) != 7 or month[4] != "-":
        raise ValueError(f"season_events wants 'YYYY-MM', got {month!r}")
    return launched_between(month + "-01", month + "-31")


def open_on(day: str) -> dict:
    """area -> why it is open on `day`: {'run': True} and/or {'window': [start, end]}.

    A run date is a WEEK MARKER, not a duration (see the spine's caveat); only an explicit window has an
    end. This answers "what does the spine say opens today", not "what is still running".
    """
    out = {}
    for area in events():
        if day in runs(area):
            out.setdefault(area, {})["run"] = True
        for start, end in windows(area):
            if start <= day <= end:
                out.setdefault(area, {})["window"] = [start, end]
    return out


def quest_first_run(mission) -> str | None:
    qd = load().get("quest_dates") or {}
    return (qd.get("missions") or {}).get(str(mission))


# ---------------------------------------------------------------------------------------------------
# Validation against OUR shipped tables (the only oracle the spine can be checked against).
# ---------------------------------------------------------------------------------------------------
def _mission_ids() -> tuple[set, set]:
    from data_paths import MST_DIR
    ids, areas = set(), set()
    with open(os.path.join(MST_DIR, "F_MISSION_MST.ndjson"), encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            ids.add(str(row.get("qo3PECw6")))
            areas.add(str(row.get("uv60hgDW")))
    return ids, areas


def validate() -> dict:
    """Every spine mission and area id must exist in `F_MISSION_MST` (mission id / area `uv60hgDW`).

    The spine is a calendar, not a content claim: a row whose missions we do not ship is still a true
    Global event, but it is not SERVABLE. The report counts both.
    """
    ids, areas = _mission_ids()
    spine_missions = {m for row in events().values() for m in (row.get("missions") or [])}
    spine_areas = set(events())
    missing_missions = sorted(spine_missions - ids)
    missing_areas = sorted(spine_areas - areas)
    return {"missions": len(spine_missions), "missions_missing": missing_missions,
            "areas": len(spine_areas), "areas_missing": missing_areas}


# ---------------------------------------------------------------------------------------------------
# The report: what the spine holds, and the gap it fills.
# ---------------------------------------------------------------------------------------------------
def report() -> int:
    m = meta()
    counts = m.get("counts") or {}
    legacy = ((m.get("source") or {}).get("commit") or "?")
    fresh = (m.get("events_source") or {}).get("commit") or legacy
    print(f"EVENT SPINE — authored={m.get('authored')}  events source commit {fresh[:12]}  "
          f"runs/quests source commit {legacy[:12]}")
    print(f"  {counts.get('events')} events / {counts.get('missions')} missions / {counts.get('dungeons')} dungeons; "
          f"{counts.get('runs_dates')} run dates over {counts.get('runs_areas')} areas; "
          f"{counts.get('windows_spans')} windows over {counts.get('windows_areas')} areas; "
          f"{counts.get('quest_dates')} quest first-runs")
    years = Counter(str(row.get("release"))[:4] for row in events().values() if row.get("release"))
    print("  release years:", dict(sorted(years.items())))
    print("  no release date:", counts.get("events_without_release"), "(kept with release=null, not guessed)")
    groups = Counter(row.get("group") for row in events().values())
    print("  groups:", groups.most_common())

    v = validate()
    print(f"  validate: {v['missions']} missions, {len(v['missions_missing'])} missing from F_MISSION_MST; "
          f"{v['areas']} areas, {len(v['areas_missing'])} unknown")
    if v["missions_missing"][:5]:
        print("    missing missions (first 5):", v["missions_missing"][:5])

    # The fill: our own calendar by year vs the spine's.
    try:
        with open(os.path.join(HERE, "community_events.json"), encoding="utf-8") as fh:
            ours = json.load(fh).get("events") or []
        our_years = Counter(str(e.get("start"))[:4] for e in ours if e.get("start"))
        print("  our community_events by year:", dict(sorted(our_years.items())))
        ours_2016_21 = sum(n for y, n in our_years.items() if "2016" <= y <= "2021")
        spine_2016_21 = sum(n for y, n in years.items() if "2016" <= y <= "2021")
        print(f"  fill 2016-2021: spine {spine_2016_21} events vs our calendar {ours_2016_21}")
    except (OSError, ValueError) as exc:
        print(f"  (community_events.json not readable: {exc!r})")
    print("  REMOVAL: delete server/content/event_spine.json and server/ffbe_event_spine.py; "
          "no reply path imports this module")
    return 0


# ---------------------------------------------------------------------------------------------------
# Selftest — the data is consistent, and the layer is removable.
# ---------------------------------------------------------------------------------------------------
def _selftest() -> int:
    failed: list[str] = []

    def case(name, ok, want, got):
        print(f"event-spine {'ok  ' if ok else 'FAIL'} {name} (want {want}, got {got})")
        if not ok:
            failed.append(name)

    doc = load()
    meta = doc.get("_meta") or {}
    counts = meta.get("counts") or {}
    case("the spine is labelled AUTHORED", meta.get("authored") is True, True, meta.get("authored"))
    case("the removal recipe is stated", bool(meta.get("removal")), True, bool(meta.get("removal")))
    case("875 events", len(events()) == counts.get("events") == 875, 875, len(events()))
    case("10,325 missions", counts.get("missions") == 10325, 10325, counts.get("missions"))
    ev = event("6211")
    case("area 6211 is 'Vow of the Revived Legend'", ev.get("name") == "Vow of the Revived Legend",
         "Vow of the Revived Legend", ev.get("name"))
    case("area 6211 releases 2022-11-22", ev.get("release") == "2022-11-22", "2022-11-22", ev.get("release"))
    june = season_events("2019-06")
    case("2019-06 opens events", bool(june), ">0", len(june))
    case("2019-06 events all open inside the month",
         all(all(d.startswith("2019-06") for d in days) for days in june.values()), True,
         sorted({d for days in june.values() for d in days})[:4])
    case("a run date is not a window", windows("1199") or runs("1199"),
         "runs 1199 non-empty", len(runs("1199")))

    v = validate()
    case("every spine mission exists in F_MISSION_MST", not v["missions_missing"], 0, len(v["missions_missing"]))
    case("every spine area exists in F_MISSION_MST", not v["areas_missing"], 0, len(v["areas_missing"]))

    # THE REMOVAL CONTRACT: no other server module may import this one.
    others = []
    for name in sorted(os.listdir(HERE)):
        if not name.endswith(".py") or name == os.path.basename(__file__):
            continue
        try:
            text = open(os.path.join(HERE, name), encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        if "ffbe_event_spine" in text:
            others.append(name)
    case("no reply-path module imports the spine", not others, "[]", others)

    # ...and a missing file is an EMPTY spine, not a crash.
    empty = load("/nonexistent/event_spine.json")
    case("a missing spine loads as empty", empty == {} and events() != {}, "{}", empty)
    return 1 if failed else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    if "--validate" in sys.argv:
        v = validate()
        print(json.dumps({k: (len(x) if isinstance(x, list) else x) for k, x in v.items()}, indent=1))
        raise SystemExit(1 if (v["missions_missing"] or v["areas_missing"]) else 0)
    if "--report" in sys.argv:
        raise SystemExit(report())
    print("ffbe_event_spine is a library module (authored, removable); "
          "the server entry point is server/capture_server.py")
    raise SystemExit(0)
