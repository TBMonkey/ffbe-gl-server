# Changelog

All notable changes to the launcher/doctor app. Versioning is SemVer; the version lives in
`Cargo.toml` (one home) and is what `--version` prints and the UI shows.

The **engine** carries its own identifiers — `ENGINE_VERSION` and `CONTRACT_VERSION` in
`../server/data_paths.py` — and reports both through `--print-paths`; the app shows them in the
doctor and treats a missing `contract` as "engine predates the data-root contract".

## [0.1.7] — 2026-10-16

### Added
- `--help` prints the command list and exits, instead of falling through to the GUI (which could hang on a
  headless machine).
- `harness/wire_probe.py` — one real encrypted request/response against a running server, for the case the
  in-process suites cannot see.

### Changed
- `build_dist.sh`'s start smoke now drives that wire probe through the FROZEN engine, with a PATH holding
  only `ss`: the smoke proves the AES path answers (not just that the engine starts) and that no external
  `openssl` was consulted (audit F1).
- `cbc_decrypt` rejects unaligned ciphertext itself, so both backends raise the same error for the same bad
  input (audit F2); an unknown `FFBE_CRYPTO` value warns instead of silently meaning `auto` (F4);
  `cert_check.py` fails when it cannot verify the chain (F5).

## [0.1.6] — 2026-10-16

### Added
- The doctor shows the **wire-crypto backend**: the engine now reports `crypto=` through `--print-paths`, and
  a checkout with no `openssl` binary and no bundled library is called out instead of failing on the first
  request.
- `harness/crypto_backend_check.py` (the two AES backends must agree byte-for-byte, double-pad included) and
  `harness/cert_check.py` (in-process CA + leaf; `subject_hash_old` compared against `openssl`).
  `build_dist.sh` runs both before freezing, so a bundle that lost either cannot be cut.

### Changed
- The engine no longer needs an `openssl` binary to serve with the release bundle: `server/ffbe_crypto.py`
  prefers the `cryptography` library (shipped in the bundle) and falls back to the CLI, so a bare checkout
  is unchanged. Cert generation gained an open(s)-free implementation, `server/ffbe_certs.py` (the shape is
  `harness/gen_ca.sh`'s, kept; the script remains for checkouts), and the bundle build asserts
  `crypto=cryptography` in the frozen engine.

## [0.1.5] — 2026-10-16

### Added
- `prepare --data DIR [--from CHECKOUT] [--state SLOT]`: one command builds a data root as a symlink farm
  from a checkout (`mst_decoded`, `text_decoded`, `client/files` + `client/shared_prefs`, `certs`, plus the
  optional `cpk_extract`/`overlay`/`gacha_community`/`jp_battle_oracle`), seeds `state/<slot>.json` from the
  checkout's save slots (server half only — the client half is the harness's `ffbe_run.sh --state`), and
  closes with the same `deps.json` rows the doctor shows, so anything still missing names its producer.
  The doctor and the helper now share one `check_data_root()`, so they cannot disagree; idempotent and
  never destructive — an existing entry, a live root's state included, is skipped.

### Changed
- `build_dist.sh`: the bundle smoke now exercises `start` against the frozen engine — `prepare` → `start` →
  `status` → `stop` on a free port — the path that let 0.1.3 ship an engine the port guard killed. It
  needs the checkout's data store (untracked), so a clean clone says SKIPPED loudly rather than passing.

## [0.1.4] — 2026-10-16

### Fixed
- **`start` against the release bundle**: the port-ownership guard compared the listener's pid to the
  pid `spawn()` returned, but a PyInstaller **onefile** bootloader runs the engine as a *child*, so the
  bundle's healthy engine was declared a foreign process and killed (`port 443 is served by pid N, not by
  the engine we started`). The owner must now be the spawned pid **or a descendant of it**. Found by the
  APP-47 dogfood — the first time `start` was run with the bundled engine.

## [0.1.3] — 2026-10-16

### Changed
- Dead-code / duplication sweep from the review (APP-44), every cut grep-verified first: the
  `_qic38_set_after_field12` orphan, the unused `VERSION_TABLE` export, `Source.kind` and the two
  duplicate `area_of` overrides, unused imports in the engine entry point, four unused `__all__` names,
  the unread `panel_quest_tables` column, one duplicated 6-key row literal, eight `State.state_dirty`
  assignments that duplicated `player.dirty`, and the three duplicate gacha banner readers (one
  `_banner_doc` now). **Skipped with a reason, not force-cut:** `battle_content.REPO` (a live harness
  reader), `SURVIVAL_AREA` (the player selftest reads it), the `STATE_SLOT` env (the harness loaders
  observe it), the drop-CSV parsers (the grammars differ), the once-log idiom (no 1:1 shape) and
  `unit_name` (cross-module API).
- App shell: one `Running` pid source, one log-cap helper, one update-banner comparison; `headless
  status` prints one key=value fact per line.

## [0.1.2] — 2026-10-16

### Changed
- The doctor runs off the UI thread: the window appears immediately, Re-check never blocks a frame, and
  a wedged interpreter on PATH can no longer stop the window from opening.
- One engine argv builder behind the GUI and the CLI (they had already drifted), one detach helper, and
  the probe now returns the resolved layout — so the separate "Resolve paths" spawn and button are gone.
- **The catch-up ladder / special-campaign / welcome awards are delivered ONLY to the inbox**: nothing is
  credited when the mail arrives, and the claim goes through the one credit path, so a mailed unit or
  equipment is owned and visible — and a reward row with no projection refuses the claim rather than
  crediting part of it. The mail *is* the award, as it already was for the daily calendar and the
  anniversary ticket.
- `build_dist.sh`: always regenerates the dispatch table (was: only when missing), smoke-tests the frozen
  engine and the app binary, stages the bundle and moves it into place last, and refuses to run off Linux.

## [0.1.1] — 2026-10-16

### Added
- The headless CLI: `start` / `stop` / `status` / `paths` — the same binary as the GUI, so scripts and
  the Waydroid harness (`ffbe_run.sh --no-server`) can drive the server. `status` prints one key=value
  fact per line, for scripts.

### Fixed
- Season switch bits are volatile: a newly forced bit is served but subtracted from every save, so a
  later run without the season cannot inherit it (server side; the frozen engine picks it up on the
  next bundle build).
- A state file that fails to *parse* no longer silently becomes a fresh account; only OSError /
  JSONDecodeError fall back, and a repair crash is loud.
- One `IndexSource` per engine process (a per-request ~10 ms re-parse of the tracked TSVs).
- The doctor accepts a frozen engine (`engine/ffbe-engine`) instead of marking a working bundle red,
  and the dispatch-table row uses the engine-reported path instead of guessing.
- The GUI adopts a CLI-started server through `state/server.pid`; GUI and CLI share one stop semantic.
- `start` no longer reports "started" for a child that died instantly or for a port owned by another
  process.
- The update check passes `--` to curl and restricts it to http/https.

## [0.1.0] — 2026-10-16

### Added
- Setup / doctor: the `deps.json` check table with the local producer for every missing piece, the
  resolved layout from `--print-paths`, and engine/app version display.
- Server pane: Campaign / Max roster modes, start/stop, live engine log tail.
- Android side: system-certificate and hostname-redirect instructions, adb detection.
- Update check: point the update URL at a JSON manifest; the app compares versions and offers the
  release page. Empty URL = no check, no network traffic.
- Frozen-engine support: an executable next to the app is used directly, so a bundle needs no Python.
