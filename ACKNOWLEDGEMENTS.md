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
the client; agreement with a community source was never treated as proof. No community work is
redistributed here.

## Provenance

This engine names itself when it runs: **`ffbe-gl/<engine version> (TBMonkey)`**. The string is in
`server/data_paths.py` (`SERVER_BANNER`) and reaches the outside three ways — the HTTP `Server:` header on
every response, the first field of the startup line, and the `server_banner=` field the app's doctor reads
from `--print-paths`. It is published here on purpose: a mark nobody can claim to have removed by accident
is the only kind worth having. If you fork and change the engine, say so — but do not quietly rename it to
look like the upstream build.

## Reusing this

MIT — see [`LICENSE`](LICENSE). If you fork or redistribute it, keep the copyright notice and this file:
for the code, that is the condition; for the game data above, it is the honest thing to do.
