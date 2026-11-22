# Acknowledgements

`ffbe-gl-server` is an **unofficial fan project**. It is not affiliated with, endorsed by, sponsored by,
or supported by Square Enix.

## FINAL FANTASY BRAVE EXVIUS

*FINAL FANTASY BRAVE EXVIUS*, its name, logo, characters, artwork, music, story, and game data are the
property of Square Enix Co., Ltd. "FINAL FANTASY", "BRAVE EXVIUS", and the names of the game's characters
are trademarks or registered trademarks of Square Enix Holdings Co., Ltd. All rights in the game and its
content remain with their owners. Nothing here is a claim on any of it.

## What is ours

The MIT licence in [`LICENSE`](LICENSE) covers the code in this repository:

- the engine (`server/`) — the protocol implementation, the player-state model, and the authored reply content;
- the desktop app (`server_app/`);
- the checks in `harness/`.

**The gacha rates are authored, not recovered.** They were never shipped with the game; the real values
lived on a server that no longer exists. The rates in this repository are a design choice made so the
engine can run, and they are labelled as such wherever they are defined. Do not read them as game data.

## What is not ours, and what this repository does ship

To be accurate rather than merely reassuring — the repository is **not** free of game content:

| what | where | whose |
|---|---|---|
| The game client (`libgame.so`, the APK) | **not included** — you bring your own | Square Enix |
| Battle content tables, derived from the game's shipped data | `server/content/battle/*.tsv` | Square Enix's data |
| One in-game banner image | `server/assets/banner_art/` | Square Enix |
| Event release dates and run history | `server/content/event_spine.json` | the FFBE wiki's editors (**CC BY-SA**, see *Community*) |
| Clash of Wills event content | `server/content/cow/*` | mixed: rosters/dates from the wiki, stats from the game, formations and scoring authored here |
| English names for 222 unit forms | `server/content/unit_name_repairs.json` | authored translations of the Japanese names in the game's tables |

The battle tables are included because the server cannot replay a battle without them. Each file carries
its derivation and a `sha256` in its header comment, so its origin is checkable rather than implied.

If you hold rights in this material and want something removed, open an issue and it will be handled.

## How the protocol was recovered

The contract in [`docs/analysis/FFBE_SERVER_CONTRACT.md`](docs/analysis/FFBE_SERVER_CONTRACT.md) was
recovered by static analysis of the shipped client (v9.0.0) and its data tables, then checked against the
running client. **No Square Enix code is redistributed here**: the document describes observed behaviour
— routes, fields, envelopes — not decompiled source.

## Third-party software

- **`cryptography`** (Python) — dual-licensed Apache-2.0 / BSD-3-Clause, © the Python Cryptographic Authority
  and contributors. Used for the TLS side.
- **Rust crates** in `server_app/`: `eframe` and `egui` (© the egui authors), `serde` and `serde_json`
  (© the Serde developers), `webbrowser`, and `libc` — each dual-licensed MIT / Apache-2.0. `Cargo.lock`
  pins the exact versions and each crate ships its own licence text.
- Everything else in `server/` is the Python standard library.

## Community

Public datamining lexicons and community documentation were used as **cross-references** while
reconstructing the protocol and the battle content. Every such claim was verified independently against
the client; agreement with a community source was never treated as proof.

Two community sources are **redistributed** here, and they are credited:

- **The FINAL FANTASY BRAVE EXVIUS Wiki** — <https://exvius.fandom.com>. The event release dates and run
  history in `server/content/event_spine.json` come from its Update Schedule and event pages, transcribed by
  its editors and reused under the wiki's own licence (**CC BY-SA**; see the wiki's licence page). Every row
  keeps its `source_page`, and the file records the revision it was read from. These dates are the
  community's best record of when an event ran: the game's shipped tables carry no dates at all, so they
  cannot be checked against the game — only against another transcription of the same wiki. Where the two
  disagree, the game is silent and the wiki wins by default; treat the dates as the best available, not as
  verified fact.
- **Exvius Revival / Exvius Offline** — <https://github.com/CreamySeaman6/Exvius-revival-ios> and
  <https://github.com/Elvlin/exvius-offline>. The spine's reconciliation, the Clash of Wills rosters and the
  222 unit-name translations in `server/content/unit_name_repairs.json` come from those forks. They are
  credited as the source of that work; there is no endorsement in either direction, and the engine here is
  not their engine.

The Clash of Wills content under `server/content/cow/` is mixed in provenance, and each file says so in its
own header: boss identities and stats are the game's own data, while the formations and the scoring/reward
policy were authored here. Nothing in that directory is a recovered retail table.

## Provenance

This engine names itself when it runs: **`ffbe-gl/<engine version>[+<build id>] (TBMonkey)`** — for this
release, `ffbe-gl/0.1.5+v0.1.8 (TBMonkey)`. The string is in `server/data_paths.py` (`SERVER_BANNER`) and
reaches the outside three ways — the HTTP `Server:` header on every response, the first field of the startup
line, and the `server_banner=` / `build_id=` fields the app's doctor reads from `--print-paths`. The build id
is stamped by the export that cuts a release; a development checkout carries none, so an empty id in a
shipped bundle means it was built by hand. It is published here on purpose: a mark nobody can claim to have
removed by accident is the only kind worth having. If you fork and change the engine, say so — but do not quietly rename it to
look like the upstream build.

## Reusing this

MIT — see [`LICENSE`](LICENSE). If you fork or redistribute it, keep the copyright notice and this file:
for the code, that is the condition; for the game data above, it is the honest thing to do.
