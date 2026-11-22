# FFBE server app

This app runs a **local server for the FINAL FANTASY BRAVE EXVIUS game you already own**. You keep your own
copy of the game; the app answers the requests it makes, so the game can run against your machine instead of
the original servers.

It ships **no game content** and needs nothing installed — the server, its runtime and its crypto are all
inside the app.

## What you need

- **Your own copy of the game**: the APK, and the game's *data folder* — what it has downloaded on a device
  where it has run (tables, text, version info). The server reads those files; it cannot create them.
- **An Android emulator or device you can change**: it must let you add a **system** certificate and redirect
  one hostname. The game ships arm64 code only, so a stock x86_64 emulator image will not run it.
- **Linux** for the server today (the engine is a Linux binary; Windows/macOS builds are planned).

No Python, no `openssl`, no source checkout, no network install: everything the server needs is in the bundle.

## Getting started — three steps

**1. Prepare the data folder (once).** Point the tool at your two files:

```
./run-me-first.sh /path/to/FFBEWW.apk /path/to/com.square_enix.android_googleplay.FFBEWW
```

(Double-click friendly: with no arguments it asks for both paths. `run-me-first.cmd` is the Windows
counterpart.) It extracts the table keys from the APK, decodes the tables and text, generates the CA, and
writes `ffbe-data/prepare.json` describing what it read. The data folder is disposable: delete it and re-run.

**2. Start the server — no arguments needed.**

```
./ffbe-server-app start
```

`prepare` remembered the data folder, port, mode and bind, so `start`, `stop`, `status` and `logs` are bare
verbs from then on (`logs -f` follows the log, `status` prints where it is running). Nothing else needs to be
typed, and no environment variable needs to be set. Open `./ffbe-server-app` with no arguments for the GUI:
**Server** starts and stops it, **Setup** shows what is present and what is missing.

**3. Point the game at it.** Use the app's **Android side** pane: install `ffbe-data/certs/ca.pem` into the
emulator's **system** trust store, then make `v900-lapis.gumi.sg` resolve to the machine running the server.

## If something goes wrong

- Run `./ffbe-server-app status` — it says whether the server is up, and on which port/mode/data folder.
- `./ffbe-server-app logs -n 50` shows the engine log; `-f` follows it.
- `./engine/ffbe-engine --print-paths --data ./ffbe-data` lists every path it resolved; `crypto=cryptography`
  means no external crypto binary is needed.
- Re-run `./run-me-first.sh` if a step of the data folder looks wrong; it rewrites it from the same two
  inputs and never touches your game files.
- The emulator menus are yours to match to your emulator. Nothing about the emulator is automatic, by design.

# For developers

## How it fits together

The app is a small desktop launcher (Rust / egui) around the **canonical Python engine** (`../server/`). The
engine does all protocol and content work; the app starts it, checks that everything it needs is present,
and walks a non-terminal user through the Android side. The engine is the one artifact — the app is a shell
around it and contains **no protocol logic**.

## Engine contract

The app drives the server through this CLI (implemented in `../server/capture_server.py`):

```
capture_server.py --data DIR --print-paths          # resolved layout, machine-readable, then exit
capture_server.py --data DIR --campaign             # normal save
capture_server.py --data DIR --maxroster            # sandbox: every unit maxed, EX served
```

Expected layout under `--data DIR` (also what `--print-paths` reports):

```
DIR/
  mst_decoded/            decoded MST ndjson            (scripts/mst_decode.py)
  text_decoded/           decoded localized text        (scripts/text_decode.py)
  client/files/           CDN root from a client dump
  client/shared_prefs/gamedata.xml   MST version registry
  cpk_extract/            art by basename (optional)
  overlay/                patched tables + fetched packs (optional)
  certs/leaf.pem leaf.key
  state/                  state.json, per-slot saves, requests/  (writable)
```

## Headless: how we run the server

The GUI and the CLI are the same binary, so a script (and the Waydroid harness) can drive it:

```bash
ffbe-server-app start  --data DIR [--port 443] [--mode campaign|maxroster] [--env K=V ...]
ffbe-server-app status --data DIR      # pid + port + mode; exit 1 when not running
ffbe-server-app stop   --data DIR
ffbe-server-app paths  --data DIR      # the engine's --print-paths output
ffbe-server-app prepare --data DIR [--from CHECKOUT] [--state SLOT]
                                       # build the root: symlink farm + one save slot, then check deps
```

`prepare` is the one-command answer to the doctor's missing rows: point `--from` at a checkout (default:
found above the engine) and it links the trees the root needs, seeds one save slot's server half from the
checkout's `out/state/`, and prints every `deps.json` row. The client half of a save is restored by the
harness: `harness/ffbe_run.sh --state SLOT`.

`start` detaches (setsid), appends to the server log inside the data folder, writes a pid file and
`server.json` beside it, and returns only once the port answers. The GUI writes the same pidfile, so
the two front ends agree on what is running.

The harness attaches instead of starting its own server:

```
harness/ffbe_run.sh --no-server …
```

That split is the point: the app owns which engine, which mode and which log; the harness owns the
client (container, display, scrcpy, save slots).

## Build / run

```bash
cargo run            # dev (needs Python 3.10+ and PyYAML for the engine)
cargo build --release
```

On first launch the app looks for the engine next to itself (a bundle's `engine/`) and in a repo
checkout; the data root comes from the saved config or `FFBE_DATA`. Both fields are editable (with a
`Browse…` folder picker) and saved
to a per-user config file in the platform's standard config location:

```
Linux      ~/.config/ffbe-server-app/config.json
Windows    %APPDATA%\ffbe-server-app\config.json
```

## Versioning and releases

Two version numbers exist and the app shows both:

* **the app** — SemVer in `Cargo.toml` (the one home). `ffbe-server-app --version` prints it.
* **the engine** — `ENGINE_VERSION` and `CONTRACT_VERSION` in `../server/data_paths.py`, reported
  through `--print-paths`. A missing `contract` line means the engine predates the data-root
  contract; the app then runs it in repo-layout mode instead of guessing.

To tell users a newer release exists, publish a small JSON manifest and point clients at it — via the
**Update URL** field, or the `FFBE_UPDATE_URL` environment variable:

```json
{ "version": "0.2.0", "url": "https://example.invalid/releases/v0.2.0",
  "notes": "optional one-liner shown next to the version" }
```

The app checks once at launch and from the **Check now** button, compares SemVer, and offers the
release page when the manifest is newer. An empty URL means no check and no network traffic.

Cutting a release: bump the version in `Cargo.toml` (and `ENGINE_VERSION` if the engine changed),
add a `CHANGELOG.md` entry, run `./build_dist.sh`, tag, and publish the zip plus the manifest.

## Release bundle

`./build_dist.sh` assembles a self-contained bundle under `dist/` — the GUI binary plus a PyInstaller
frozen engine, so the target machine needs **no Python, no PyYAML and no `openssl` binary** (the bundle
ships the `cryptography` library for the wire AES, and cert generation is in-process):

```
ffbe-server-app-<version>/
  ffbe-server-app            GUI (cargo --release)
  run-me-first.sh            the one command to run first (prepare the data folder)
  run-me-first.cmd           the same for Windows, when that build lands
  engine/ffbe-engine         frozen engine (PyInstaller onefile; also carries the two table decoders
                             and the text key manifest, so `prepare` needs no repo)
  README.md  CHANGELOG.md  deps.json  VERSION
```

The build refuses to ship a `deps.json` whose producers name `scripts/` or `harness/` (a command the bundle
cannot run — that is how 0.1.7 sent a user looking for files it never carried), refuses a frozen engine
without a working `--prepare`, then smoke-tests the bundle: `prepare` → `start` → `status` → one real encrypted request/response through the frozen engine (`harness/wire_probe.py`)
→ `stop`, with a PATH that holds only `ss` — so a bundle that would need `openssl`, or a frozen engine that
cannot answer a request, fails the build instead of shipping. The app auto-detects `engine/` next to its own
binary. `build/` (the PyInstaller venv and work dirs) and `dist/` are gitignored; only the app source is
tracked.

## Standalone checkout

This directory is also pushable on its own (`git subtree split --prefix=APK_emulator_work/server_app`).
A standalone clone builds the GUI but brings no engine: point the engine directory at a ffbe checkout's
`APK_emulator_work/server`, or set `FFBE_SERVER_ENGINE` to it. The doc paths inside `deps.json` are
relative to that workspace.

## Status

Bundle 0.1.7: the frozen engine serves over TLS with no Python, no PyYAML and no `openssl`, and the build
smoke proves it with a live encrypted request (220 request classes). Open items live in the workstream queue:
`APP-50` (move the MST/text decoders into the engine) and `APP-51` (prepare a data folder from your own APK
and data folder — the guided setup the top of this file points forward to), then `APP-46` (Windows/macOS
builds, first published update manifest).
