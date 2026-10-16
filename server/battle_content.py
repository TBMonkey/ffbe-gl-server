#!/usr/bin/env python3
"""battle_content.py — the ONE implementation of the JP battle-derivation chain, for server and CLI.

SOURCE OF TRUTH
---------------
`jp_battle_oracle/decoded/*.ndjson` — the JP EoS / Memorial client-side MST tables, fetched and decoded
by `harness/fetch_jp_battle_oracle.py`. They are first-party (SE's own data, redistributed by the
community CDN) and supersede both hand-authoring and the community `exvius_reborn` DB (which is a
faithful copy of this same lineage). The fact home is `APK_emulator_work/docs/notes/battle_oracle_jp.md`.

CHAIN (all first-party)
    F_MISSION_PHASE_MST      `Wg1a5VJR`  one row per phase; a battle phase's `6uIYE15X` is a LOTTERY id
    F_BATTLE_LOTTERY_MST     `9VK5gHyN`  lottery id (`c2gnh6Jd`) -> battle group (`2fY1IomW`), weight `ixDq7yJ8`
    F_BATTLE_GROUP_MST       `Z23dpayU`  group -> one row per enemy slot (part id, order, position, flags)
    F_MONSTER_MST            `BrjD06sM`  part id -> dictionary MonsterId + name
    F_MONSTER_PARTS_MST      `4F1Zpy7V`  the monster's battle stats, DamageFrame, AiID, and its drop list
    F_MONSTER_SKILL_MST      `wJE09yxD`  the in-battle action definitions the parts row references

BREAK (COMMUNITY, not first-party -- labelled as such)
    harness/break_status_community.json   a part whose `e56NZY42` (MonsterBreakID) is non-zero needs the
    `BJ6A0KTt` + `7KCkJd1E` rows or the client NULL-DEREFS. Those two tables were transmitted, never
    shipped and no corpus has them, so the values come from the community wiki; parts absent from the
    dataset keep `e56NZY42=0` (mechanic off, crash-safe) and the file's `_provenance` records the gaps.

WHY THE POOL HOP LIVES HERE: the GL client has **no** `BattleLotteryMstList` (`BattleLotteryMstList` = 0 in
`libgame.so`), so it cannot resolve a pool — the server must, and send a *resolved* group id in the phase's
`2fY1IomW` (the client looks that up directly in `Bi6hVv34`). JP resolves client-side, which is why JP
ships the lottery.

TWO SOURCES, ONE CHAIN
----------------------
The derivation below reads the same rows through a `Source`:
  * `NdjsonSource` — the raw oracle `.ndjson` tables, the authoring CLI's `--oracle` (behaviour identical
    to the original `derive_battle_payload.py`, which now imports this module);
  * `IndexSource`  — the tracked, compact TSVs under `server/content/battle/`, built by
    `harness/build_battle_index.py`. It carries only the fields the chain READS, so the server never
    parses 75 MB of ndjson inside the reply path.

`assemble()` is the server's entry point: authored `responses.json` wins -> per-area cache -> derive from
the index, write the cache, return -> nothing (the caller keeps its bare-key fallback). The authored layer
is never written by any of this; `assemble` only reads it.
"""
from __future__ import annotations

import json
import os
import threading

import data_paths

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.dirname(HERE)
REPO = os.path.dirname(WORK)
# The JP/Memorial oracle is OPTIONAL at serve time (the battle index ships in `server/content/battle/`).
# It follows the data-root contract: `<root>/jp_battle_oracle/decoded` when a root is set, else REPO's.
DEFAULT_ORACLE = data_paths.ORACLE_DIR
INDEX_DIR = os.path.join(HERE, "content", "battle")
DEFAULT_RESPONSES = os.path.join(HERE, "responses.json")
# The break table lives in the harness tree in a checkout; a frozen bundle ships it beside the
# engine's own data (built by server_app/build_dist.sh). Prefer whichever exists; env override wins.
def _break_status_default() -> str:
    env = os.environ.get("BREAK_STATUS_FILE")
    if env:
        return env
    for cand in (os.path.join(WORK, "harness", "break_status_community.json"),
                 os.path.join(HERE, "break_status_community.json")):
        if os.path.isfile(cand):
            return cand
    return os.path.join(WORK, "harness", "break_status_community.json")


BREAK_STATUS_DEFAULT = _break_status_default()
CACHE_ROOT = os.path.join(WORK, "out", "battle_cache")
BATTLE_BUNDLE_REPLY_ID = "jnw49dUq"

# The GL `MissionPhaseMstResponse` token set (readParam 0x197aa90). Only these go on the wire.
PHASE_TOKENS = ("qo3PECw6", "6v7y3QcJ", "gou39JeI", "q4f9IdMs", "i2ar9yXM", "5LZN8C9I", "YS9kfCX7",
                "DYTx1Is3", "T03qgLWA", "2fY1IomW", "6uIYE15X", "J9gafF0t", "V2mCH5UR", "4K8t5dVh",
                "juA0Z4m7", "1C7GDEv4", "Us0znif6", "k6Y8ysMA", "wTMK0x5p")
# The GL `MonsterPartsMstResponse` token set (readParam 0x1964f84, 52 tokens). JP rows carry extras
# (9BetM2Ds, MZn8LC6H, Vf5DGw07, 8zH7CioM, u2I6pR9T, H4zD2IQ5) that the GL reader does not dispatch.
PARTS_TOKENS = ("xqartN26", "6Yc4RkdM", "G4L0YIB2", "fh31sk7B", "PC97pWQj", "9NBV1XCS", "Pn5w9dfh",
                "84FJM3PD", "T8xMDAZ4", "hn42BL3Q", "D9Nb7jgd", "2U3Zbxpy", "0ePkR9nZ", "4Y2DGP7m",
                "LBye01Wi", "e56NZY42", "B6H34Mea", "7UR4J2SE", "5A8SmwyC", "a9DvZ70c", "8a6gAwN9",
                "A5x02C6a", "jmKe42Yg", "n0UEb71s", "7wV3QZ80", "f67khsQZ", "Q1qLbx93", "zPu2iUK8",
                "Y9Vz8WNH", "3frn4ILX", "IRf10dwx", "e5xoiC84", "ATM35pjf", "a9qg8vnP", "0dQ4JPAp",
                "7KYufEd5", "w04FtZqB", "h8V3AzqP", "9rXm2fJP", "Wm93vGVU", "Rcqa1Zt7", "3HriTp6B",
                "rB70Ls63", "rtw4F6hn", "c76s0ehT", "kSL0M69n", "cNzS27x6", "tPh7ue1c", "wDW93BxJ",
                "G5HbL0vM", "2RZQ0yfb", "m28pD1GM")
PARTS_DEFAULTS = {"2U3Zbxpy": "0", "0ePkR9nZ": "0"}

GROUP_TOKENS = ("2fY1IomW", "xqartN26", "MxLFKZ13", "Z0EN6jSh", "3t7pZM5V", "WJdBp9f0", "m9BvFk2X")
BATTLE_TAGS = ("an6TB4WS", "Bi6hVv34", "wbet57nF", "bV34YIJQ", "y35YvQjZ", "Wp39QX52")

# `MonsterPartsBreakMstResponse` (`BJ6A0KTt`) + `MonsterPartsExtStatusMstResponse` (`7KCkJd1E`). A part whose
# `e56NZY42` (MonsterBreakID) is non-zero makes `MonsterUnit::initialize` (guard 0x1b636e8) fetch BOTH rows
# and use them UN-null-checked -- the 8960101 crash of 2026-10-10. The break row shares 27 token NAMES with
# the parts row, so it is built from the served part and only the broken-form stat tokens, the broken-form
# resists (where they differ) and the description come from `harness/break_status_community.json` -- a community
# datamine (aEnigmatic's per-encounter gists), wiki-corroborated, NOT first-party; its `_provenance` lists every
# corpus that does NOT have these tables. While `MonsterUnit::isBreak()` the stat getters return the dataset's
# values (0x1b65fbc, +0x40/+0x48/+0x50/+0x58). The ext row is the break gauge: `getMaxBp`
# (0x1b6c680-0x1b6c6c8) reads `CX5D2V1j`, then -- ONLY once `BattleUnit::getBreakNum() >= 1` -- adds
# `8HnQR9Wx` when `zW97Bico == 1`, or multiplies by `8HnQR9Wx/100` when it is 2. `zW97Bico = 2` with value 0
# ZEROES the pool: never emit that. `RHe5r72C`/`EwZ40mt3` (weapon-resist CSVs) are left unsent: no source.
BREAK_TAG = "BJ6A0KTt"
EXT_STATUS_TAG = "7KCkJd1E"
BREAK_TOKENS = ("e56NZY42", "G4L0YIB2", "9NBV1XCS", "Pn5w9dfh", "84FJM3PD", "hn42BL3Q", "D9Nb7jgd",
                "T8xMDAZ4", "5A8SmwyC", "a9DvZ70c", "a9qg8vnP", "3HriTp6B", "rB70Ls63", "rtw4F6hn",
                "f67khsQZ", "Q1qLbx93", "zPu2iUK8", "Rcqa1Zt7", "n0UEb71s", "h8V3AzqP", "9rXm2fJP",
                "2RZQ0yfb", "kSL0M69n", "cNzS27x6", "6uvHX2JG", "tPh7ue1c", "G5HbL0vM", "LBye01Wi")
EXT_STATUS_TOKENS = ("e56NZY42", "CX5D2V1j", "JX6mCav4", "zW97Bico", "8HnQR9Wx", "RHe5r72C",
                     "EwZ40mt3")

# `MonsterSkillMstResponse` (tag `Wp39QX52`). Emitted VERBATIM from the JP row's 26 tokens rather than
# filtered to a GL subset, because here the dangerous direction is an OMISSION, not an extra: readParam
# (0x19631dc) dispatches on known tokens and ignores the rest, while `BattleUnit::setMonsterSkill`
# (0x1bd7818) does `MonsterSkillMstList::shared()->objectForKey(id)` and `cbz x0` (0x1bd7a40) SILENTLY
# SKIPS the skill when the row is absent -- so a missing row costs the monster its entire action list and
# its AI degrades to the fallback plain attack (SW-50: Belmodar served Megavolt-less because this group
# was never sent).
SKILL_TAG = "Wp39QX52"

# --- the index: what the derivation READS, per oracle table ------------------------------------------
# The builder writes exactly these columns (intersected with the source's own key set), in THIS order, so
# the reconstructed rows keep the same dict/insertion order the derivation would have produced from the
# ndjson. A field listed here that the oracle lacks (the JP phase rows have no `2fY1IomW` -- the deriver
# SETS it -- and the parts rows lack the two GL-only defaults) is recorded in the header's `absent` list.
# `F_MONSTER_SKILL_MST: None` means "every field of the source row", because the skill rows go on the wire
# verbatim. `F_MISSION_MST` is not part of the derivation: it is the mission -> area lookup the per-area
# cache filename needs (see `assemble`).
INDEX_FIELDS: dict[str, tuple[str, ...] | None] = {
    "F_MISSION_PHASE_MST": PHASE_TOKENS,
    "F_BATTLE_LOTTERY_MST": ("c2gnh6Jd", "ixDq7yJ8", "2fY1IomW"),
    "F_BATTLE_GROUP_MST": GROUP_TOKENS,
    "F_MONSTER_MST": ("xqartN26", "G4L0YIB2", "V9j8wJcC"),
    "F_MONSTER_PARTS_MST": tuple(PARTS_TOKENS) + ("9BetM2Ds",),
    "F_MONSTER_SKILL_SET_MST": ("A5x02C6a", "7qxLC5hP"),
    "F_MONSTER_SKILL_MST": None,
    "F_MISSION_MST": ("qo3PECw6", "U9iHsau3"),
}
INDEX_TABLES = tuple(INDEX_FIELDS)
# The key column a table must carry for the index to be readable at all (checked by the builder).
INDEX_KEY = {
    "F_MISSION_PHASE_MST": "qo3PECw6",
    "F_BATTLE_LOTTERY_MST": "c2gnh6Jd",
    "F_BATTLE_GROUP_MST": "2fY1IomW",
    "F_MONSTER_MST": "xqartN26",
    "F_MONSTER_PARTS_MST": "xqartN26",
    "F_MONSTER_SKILL_SET_MST": "A5x02C6a",
    "F_MONSTER_SKILL_MST": "6E5u1xrs",
    "F_MISSION_MST": "qo3PECw6",
}

def load(oracle: str, table: str) -> list[dict]:
    """Read one oracle ndjson table into rows (the NdjsonSource backend)."""
    path = os.path.join(oracle, table + ".ndjson")
    if not os.path.exists(path):
        raise SystemExit(f"oracle table missing: {path}\n  run: harness/fetch_jp_battle_oracle.py")
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def resolve_pool(lottery: list[dict], pool: str) -> tuple[str, list[dict]] | None:
    """Lottery id -> (group id, the candidate rows). Deterministic: highest weight, then lowest group id.

    The pool is a distribution; a live server would roll it per battle. The payload is a committed
    artifact, so this picks one member and the caller may `--roll` instead.
    """
    cands = [r for r in lottery if r.get("c2gnh6Jd") == pool and int(r.get("ixDq7yJ8") or 0) > 0]
    if not cands:
        return None
    cands.sort(key=lambda r: (-int(r["ixDq7yJ8"]), r["2fY1IomW"]))
    return cands[0]["2fY1IomW"], cands


def load_break_status(path: str) -> dict[str, dict]:
    """The community break dataset: part id -> {break_hp, break_hp_after_first, turns, broken{atk,def,mag,spr}}.

    NOT first-party data -- see the file's `_provenance` for sources, the blue-span decision and the gaps.
    A part with a non-zero `e56NZY42` that is MISSING here keeps `e56NZY42=0`: the mechanic is off for it,
    which is a documented gap, whereas a served-but-wrong row would silently change the fight (and a missing
    row crashes).
    """
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    return doc.get("parts") or {}


# --- the source abstraction --------------------------------------------------------------------------

class Source:
    """What `derive_mission` reads the chain through. `load(table)` -> list of row dicts."""

    _areas: dict[str, str] | None = None

    def load(self, table: str) -> list[dict]:  # pragma: no cover - interface
        raise NotImplementedError

    def area_of(self, mission: str) -> str:
        """The mission's AREA id (`F_MISSION_MST.U9iHsau3`), for the per-area cache filename."""
        if self._areas is None:
            self._areas = {str(r.get("qo3PECw6") or ""): str(r.get("U9iHsau3") or "")
                           for r in self.load("F_MISSION_MST")}
        return self._areas.get(str(mission), "")


class NdjsonSource(Source):
    """The raw oracle: behaviour identical to the original `derive_battle_payload.load()`."""

    def __init__(self, oracle: str = DEFAULT_ORACLE):
        self.oracle = oracle

    def load(self, table: str) -> list[dict]:
        return load(self.oracle, table)


def escape_cell(value: str) -> str:
    """One TSV cell, JSON-escaped only when it carries a character TSV cannot hold literally."""
    if "\\" not in value and "\t" not in value and "\n" not in value and "\r" not in value:
        return value
    return json.dumps(value, ensure_ascii=False)[1:-1]


def unescape_cell(cell: str) -> str:
    if "\\" not in cell:
        return cell
    return json.loads('"' + cell + '"')


def read_index_table(path: str, table: str) -> list[dict]:
    """Parse one index TSV (`# {header json}` then rows) into row dicts, in cheap-path/fast form."""
    if not os.path.exists(path):
        raise SystemExit(
            f"battle index table missing: {path}\n"
            f"  build it with: python3 harness/build_battle_index.py   (writes server/content/battle/)")
    with open(path, encoding="utf-8") as fh:
        first = fh.readline()
        if not first.startswith("#"):
            raise SystemExit(f"{path}: first line is not the provenance header")
        meta = json.loads(first[1:].strip())
        if meta.get("table") != table:
            raise SystemExit(f"{path}: header names table {meta.get('table')!r}, wanted {table!r}")
        fields = [str(f) for f in meta.get("fields") or []]
        # CODE-vs-INDEX DRIFT: if the chain gained a token since this index was built, the field would be
        # silently absent and `.get` defaults would quietly disagree with the ndjson path. The builder's
        # `absent` list makes that loud here instead.
        wanted = INDEX_FIELDS.get(table)
        if wanted:
            drift = [f for f in wanted if f not in fields and f not in (meta.get("absent") or [])]
            if drift:
                raise SystemExit(f"{path}: the chain reads {drift} but this index predates them; "
                                 f"rebuild with harness/build_battle_index.py")
        rows: list[dict] = []
        for line in fh:
            line = line.rstrip("\n")
            if not line:
                continue
            cells = line.split("\t")
            if len(cells) != len(fields):
                raise SystemExit(f"{path}: row has {len(cells)} cells, header declares {len(fields)}")
            if "\\" in line:
                cells = [unescape_cell(c) for c in cells]
            rows.append(dict(zip(fields, cells)))
        expect = int(meta.get("rows") or 0)
        if len(rows) != expect:
            # A truncated/partial checkout must fail loud: a short table would derive a WRONG bundle, and
            # the field-count check above cannot see a missing tail.
            raise SystemExit(f"{path}: {len(rows)} rows on disk, header declares {expect}; "
                             f"rebuild with harness/build_battle_index.py")
    return rows


# ONE IndexSource per process. Its `load` parses the tracked TSVs (~13 MB across six tables), and the
# class docstring promises "reads a table once per process" -- but `assemble()` used to build a fresh one
# per call, re-parsing F_MISSION_MST.tsv (~10 ms) on every derived-bundle request, cache hit or not.
_INDEX_SOURCE = None


def _default_index_source() -> "IndexSource":
    global _INDEX_SOURCE
    if _INDEX_SOURCE is None:
        _INDEX_SOURCE = IndexSource()
    return _INDEX_SOURCE


class IndexSource(Source):
    """The tracked compact index. Reads a table once per process, then serves it from memory."""

    def __init__(self, root: str = INDEX_DIR):
        self.root = root
        self._tables: dict[str, list[dict]] = {}

    def load(self, table: str) -> list[dict]:
        if table not in self._tables:
            self._tables[table] = read_index_table(os.path.join(self.root, table + ".tsv"), table)
        return self._tables[table]


def as_source(source: "Source | str") -> Source:
    """Accept a Source or an oracle directory path (the authoring CLI's historical signature)."""
    if isinstance(source, Source):
        return source
    if isinstance(source, str):
        return NdjsonSource(source)
    raise TypeError(f"expected a Source or an oracle path, got {type(source).__name__}")


def derive_mission(mission: int, source: Source | str, break_status: dict[str, dict] | None = None) -> dict:
    source = as_source(source)
    break_status = load_break_status(BREAK_STATUS_DEFAULT) if break_status is None else break_status
    phases = source.load("F_MISSION_PHASE_MST")
    lottery = source.load("F_BATTLE_LOTTERY_MST")
    groups = source.load("F_BATTLE_GROUP_MST")
    monsters = source.load("F_MONSTER_MST")
    parts = source.load("F_MONSTER_PARTS_MST")
    mon_by_part = {m["xqartN26"]: m for m in monsters}
    # A monster's PARTS are several `F_MONSTER_PARTS_MST` rows SHARING one `xqartN26` (MonsterID) and
    # differing in `6Yc4RkdM` (PartsNum): 1 = the body, 2..N the limbs. The GL client builds one battle unit
    # per row -- `MonsterParty::entryMonster` (0x1ae25e4) calls `MonsterPartsMstList::getPartsList(partId)`
    # and `MonsterUnit::initialize` (0x1b636dc) looks each unit up with `getObject(MonsterID, PartsNum)` --
    # so a dict keyed by part id alone DROPS the PartsNum>1 rows and gives every multi-part monster a single
    # limb. 74 monsters have PartsNum 2..4 in the oracle (e.g. `303010000` Dark Maelstrom + 3 tentacles);
    # fixed 2026-10-10 from the Antenolla report.
    parts_all: dict[str, list] = {}
    for p in parts:
        parts_all.setdefault(str(p["xqartN26"]), []).append(p)
    for rows in parts_all.values():
        rows.sort(key=lambda r: int(r.get("6Yc4RkdM") or 1))

    mission_phases = sorted((p for p in phases if p.get("qo3PECw6") == str(mission)),
                            key=lambda p: int(p["6v7y3QcJ"]))
    if not mission_phases:
        raise SystemExit(f"mission {mission}: no F_MISSION_PHASE_MST rows")

    phase_rows, group_ids, part_ids, drop_rows = [], {}, set(), []
    for p in mission_phases:
        target = p.get("6uIYE15X", "")
        resolved = resolve_pool(lottery, target) if p.get("gou39JeI") == "2" else None
        if p.get("gou39JeI") == "2" and resolved is None:
            raise SystemExit(f"mission {mission} phase {p['6v7y3QcJ']}: lottery id {target} has no weighted member")
        group = resolved[0] if resolved else ""
        row = {tok: p[tok] for tok in PHASE_TOKENS if tok in p}
        row["2fY1IomW"] = group
        row.setdefault("6uIYE15X", target)
        phase_rows.append(row)
        if not resolved:
            continue
        slot_rows = sorted((g for g in groups if g.get("2fY1IomW") == group), key=lambda g: int(g["Z0EN6jSh"]))
        if not slot_rows:
            raise SystemExit(f"mission {mission} phase {p['6v7y3QcJ']}: group {group} has no slots")
        group_ids.setdefault(group, slot_rows)
        drops: list[str] = []
        for s in slot_rows:
            pid = str(s["xqartN26"])
            part_ids.add(pid)
            # Every part of the monster contributes drops, not just the body (PartsNum 1).
            for pr in parts_all.get(pid, []):
                for it in (pr.get("9BetM2Ds") or "").split(","):
                    if it and it not in drops:
                        drops.append(it)
        drop_rows.append({
            "qo3PECw6": str(mission), "6v7y3QcJ": p["6v7y3QcJ"], "pIHCrf40": "1", "2fY1IomW": "1",
            "xqartN26": slot_rows[0]["xqartN26"], "Z0EN6jSh": "1", "6Yc4RkdM": "1",
            "9BetM2Ds": ",".join(drops), "MZn8LC6H": "", "g4voc2Aw": "", "3frn4ILX": "",
            "IRf10dwx": "", "ATM35pjf": "", "e5xoiC84": "", "Vf5DGw07": "",
        })

    group_rows = [{tok: s[tok] for tok in GROUP_TOKENS if tok in s}
                  for rows in group_ids.values() for s in rows]
    monster_rows = []
    for pid in sorted(part_ids):
        m = mon_by_part.get(pid)
        if m is None:
            raise SystemExit(f"part {pid}: no F_MONSTER_MST row")
        monster_rows.append({k: m.get(k, "") for k in ("xqartN26", "G4L0YIB2", "V9j8wJcC")})
    parts_rows = []
    break_parts: list[tuple[str, dict]] = []
    for pid in sorted(part_ids):
        rows = parts_all.get(pid)
        if not rows:
            raise SystemExit(f"part {pid}: no F_MONSTER_PARTS_MST row")
        # EVERY PartsNum row, in order: one battle unit is created per row.
        for pr in rows:
            row = {tok: pr.get(tok, PARTS_DEFAULTS.get(tok, "")) for tok in PARTS_TOKENS}
            # MONSTER BREAK. Keep the id only when BOTH rows it will look up can be served; otherwise send 0,
            # which takes the `cbz w20` branch at 0x1b636e8 and skips both lookups. Zeroing costs the part its
            # break mechanic; serving a fabricated row would cost the fight's correctness instead.
            bid = str(row.get("e56NZY42") or "")
            if bid not in ("", "0"):
                if pid in break_status:
                    break_parts.append((pid, row))
                else:
                    print(f"    break: mission {mission} part {pid}: MonsterBreakID {bid} -> 0 "
                          f"(no community entry for it; break stays off)", flush=True)
                    row["e56NZY42"] = "0"
            parts_rows.append(row)

    # One `BJ6A0KTt` + `7KCkJd1E` pair per part that kept a non-zero MonsterBreakID.
    break_rows: list[dict] = []
    ext_status_rows: list[dict] = []
    for pid, row in break_parts:
        info = break_status[pid]
        bid = str(row["e56NZY42"])
        br = {tok: row[tok] for tok in BREAK_TOKENS if tok in row}
        absent = [tok for tok in BREAK_TOKENS if tok not in br]
        if absent != ["6uvHX2JG"]:  # the ONE break-row token the parts table does not carry
            raise SystemExit(f"mission {mission} part {pid}: break row is missing {absent}; "
                             f"expected only 6uvHX2JG (PARTS_TOKENS gained or lost a break token)")
        br["9NBV1XCS"] = str(info["broken"]["atk"])
        br["Pn5w9dfh"] = str(info["broken"]["def"])
        br["84FJM3PD"] = str(info["broken"]["mag"])
        br["T8xMDAZ4"] = str(info["broken"]["spr"])
        # The broken form's RESISTS are not always the base's: for the Intangir, the Antenolla and the 2-Headed
        # Dragon the gist's `# Broken Form` block drops the elemental immunity (99% -> 0% / 49% / -50%), which is
        # what breaking is FOR. `resists` carries wire-ready strings, so this is a straight override of the values
        # copied from the part row a few lines above.
        for tok, value in (info.get("resists") or {}).items():
            if tok not in BREAK_TOKENS:
                raise SystemExit(f"mission {mission} part {pid}: unknown resist token {tok!r}")
            br[tok] = value
        desc = (info.get("break_description") or "").strip()
        if desc:
            br["6uvHX2JG"] = desc
        break_rows.append(br)
        ext = {"e56NZY42": bid, "CX5D2V1j": str(info["break_hp"]), "JX6mCav4": str(info["turns"]),
               "zW97Bico": "0"}
        # RHe5r72C / EwZ40mt3: the ext row's 16-entry weapon CSVs. NOT optional -- both consumers
        # (`MonsterUnit::getWeakWeaponPointRelationship` 0x1b6c734, called from BattleCalc::calcAttack on every
        # attack, and `BattleLibraScene::setLayoutWeakWeapon` 0x202b208) `parseList` the member on "," and then
        # index ALL SIXTEEN entries with no length check, so an absent field is `CommonUtils::StrToInt` on a
        # null std::string -- the 2026-10-11 first fault, si_addr 0, captured in
        # out/gdb_first_fault.log. Values come from the gist's per-weapon `# Damage` table, equip-category
        # order 1..16 (Dagger..Fist).
        for tok in ("RHe5r72C", "EwZ40mt3"):
            if info.get(tok):
                ext[tok] = str(info[tok])
        after = info.get("break_hp_after_first")
        if after:
            ext["zW97Bico"] = "1"
            ext["8HnQR9Wx"] = str(int(after) - int(info["break_hp"]))
        ext_status_rows.append(ext)

    # The monster-skill rows (SW-50). `BattleUnit::setMonsterSkill` resolves every id in the parts row's
    # skill set (`A5x02C6a` -> F_MONSTER_SKILL_SET_MST.7qxLC5hP) against `MonsterSkillMstList` and silently
    # skips what it cannot find -- so every referenced id must be served, or the monster loses that action.
    set_by_id = {r["A5x02C6a"]: r for r in source.load("F_MONSTER_SKILL_SET_MST") if "A5x02C6a" in r}
    skill_by_id = {r["6E5u1xrs"]: r for r in source.load("F_MONSTER_SKILL_MST") if "6E5u1xrs" in r}
    want: list[str] = []
    for row in parts_rows:
        ssid = str(row.get("A5x02C6a") or "").strip()
        # `0` is the "no skill set" sentinel, NOT a missing row: 2415 of the oracle's parts carry it and it
        # is not a key in F_MONSTER_SKILL_SET_MST. A silent skip is CORRECT here -- the monster simply has no
        # skills. (This strict check aborted the first post-tutorial derive, 1110201.)
        if ssid in ("", "0"):
            continue
        ss = set_by_id.get(ssid)
        if ss is None:
            raise SystemExit(f"mission {mission}: skill set {ssid!r} is not in F_MONSTER_SKILL_SET_MST")
        for sid in (ss.get("7qxLC5hP") or "").split(","):
            if sid.strip() and sid.strip() not in want:
                want.append(sid.strip())
    skill_rows = []
    for sid in want:
        sr = skill_by_id.get(sid)
        if sr is None:
            raise SystemExit(f"mission {mission}: skill {sid} is not in F_MONSTER_SKILL_MST")
        # A COPY: with the memoizing IndexSource the table row is shared, and a served bundle must never
        # alias the index (a later transform could otherwise mutate the cache for every future request).
        skill_rows.append(dict(sr))
    bundle = {"an6TB4WS": phase_rows, "Bi6hVv34": group_rows, "wbet57nF": monster_rows,
              "bV34YIJQ": parts_rows, "y35YvQjZ": drop_rows, SKILL_TAG: skill_rows}
    if break_rows:
        bundle[BREAK_TAG] = break_rows
        bundle[EXT_STATUS_TAG] = ext_status_rows
    return bundle


# --- assemble: the server's derived-content path -----------------------------------------------------

_VALID_MODES = ("off", "cache", "on")
_mode_warned = False


def derive_mode(default: str = "cache") -> str:
    """`BATTLE_DERIVE=off|cache|on`, read per call so a golden run can flip it without an edit."""
    global _mode_warned
    raw = (os.environ.get("BATTLE_DERIVE") or default).strip().lower()
    if raw in _VALID_MODES:
        return raw
    if not _mode_warned:
        print(f"battle: BATTLE_DERIVE={raw!r} is not one of {_VALID_MODES}; using {default!r}", flush=True)
        _mode_warned = True
    return default


_CACHE_LOCK = threading.Lock()


def _read_cache(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) else {}
    except Exception as exc:  # noqa: BLE001 - a corrupt cache must not break a reply
        print(f"battle: cache {os.path.relpath(path, WORK)} is not loadable ({exc!r}); rebuilding", flush=True)
        return {}


def wire_bundle(bundle: dict, authored: dict | None) -> dict:
    """The derived bundle as it goes on the WIRE, with the envelope an authored key carries.

    `harness/derive_battle_payload.py --apply` gives every new `jnw49dUq@<mission>` key `1IfDqR4n: []`
    plus the BARE key's `LhVz6aD2` row. A derived bundle must carry the same or the reply would differ in
    shape from every authored one -- and `PlayerState.apply_to_reply`'s gate (`"LhVz6aD2" in spec or any
    player group`) would not fire. This only READS the authored bare key; the authored layer is untouched.
    """
    base = (authored or {}).get(BATTLE_BUNDLE_REPLY_ID)
    if not isinstance(base, dict):
        return bundle
    out: dict = {}
    if "1IfDqR4n" in base:
        out["1IfDqR4n"] = base["1IfDqR4n"]
    if "LhVz6aD2" in base:
        out["LhVz6aD2"] = base["LhVz6aD2"]
    out.update(bundle)
    return out


def assemble(mission: int | str, *, authored: dict | None = None, mode: str | None = None,
             source: Source | None = None, cache_root: str = CACHE_ROOT,
             log=print, origin: dict | None = None) -> dict | None:
    """The wire bundle for `mission`, from authored -> cache -> the tracked index, else None.

    None means "nothing could be assembled": the caller keeps its bare-key fallback. `origin` is an
    out-parameter ({'where': 'authored'|'cache'|'index', 'path': ...}) so the server can record which
    layer answered; the return contract stays the bundle itself.
    """
    mid = str(mission)
    key = f"{BATTLE_BUNDLE_REPLY_ID}@{mid}"
    if authored is None:
        with open(DEFAULT_RESPONSES, encoding="utf-8") as fh:
            authored = {k: v for k, v in json.load(fh).items() if not k.startswith("_")}
    if key in authored:
        if origin is not None:
            origin["where"] = "authored"
        return authored[key]
    mode = mode or derive_mode()
    if mode == "off":
        return None
    src = source if source is not None else _default_index_source()
    area = src.area_of(mid) or "_unknown"
    path = os.path.join(cache_root, area + ".json")
    with _CACHE_LOCK:
        cached = _read_cache(path) if mode != "on" else {}
        if mid in cached:
            if origin is not None:
                origin["where"] = "cache"
                origin["path"] = path
            log(f"battle: mission {mid} served from cache {os.path.relpath(path, WORK)} (BATTLE_DERIVE={mode})")
            return cached[mid]
        try:
            bundle = derive_mission(mid, src)
        except (SystemExit, Exception) as exc:  # a mission the oracle cannot resolve falls back, never 500s
            log(f"battle: mission {mid}: NOT derivable from the index ({exc}); "
                f"falling back to the bare key")
            return None
        bundle = wire_bundle(bundle, authored)
        cached[mid] = bundle
        os.makedirs(cache_root, exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cached, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
        os.replace(tmp, path)   # atomic: a reader sees the old file or the new one, never a half one
        if origin is not None:
            origin["where"] = "index"
            origin["path"] = path
        log(f"battle: mission {mid} derived from the index -> {os.path.relpath(path, WORK)} "
            f"({len(bundle)} groups, BATTLE_DERIVE={mode})")
        return bundle
