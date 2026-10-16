# ffbe-gl-server

A **local server for the global client of FINAL FANTASY BRAVE EXVIUS** (v9.0.0), so the game can run against
your own machine. It answers the client's HTTPS requests and serves the content the client asks for. **No game
content is included** — you bring your own copy of the game.

| directory | what it is |
|---|---|
| `server/` | the engine: the protocol, the player state, the authored reply content |
| `server_app/` | the desktop app (Rust) that runs the engine, checks the data it needs, and guides the Android side |
| `harness/` | the checks the bundle build runs; `wire_probe.py` is one real encrypted request/response |
| `docs/analysis/FFBE_SERVER_CONTRACT.md` | the recovered protocol contract (the routing table's one home) |

**Read [`server_app/README.md`](server_app/README.md) first** — it is the user guide: what you need, how to
run it, how to point your emulator at it. Build, versioning and bundle details are further down in the same
file.

## Quick look (developers)

```bash
cd server_app
cargo run                 # GUI (needs Python 3.10+ and PyYAML for the engine)
./build_dist.sh           # release bundle: dist/ffbe-server-app-<version>/ + zip
```

The engine can also be run directly:

```bash
python3 server/capture_server.py --data <data folder> --campaign
```

## Status

The Linux bundle is built and self-tested: its build smoke starts the frozen engine and drives one real
encrypted request/response through it, with no Python, PyYAML or `openssl` on the target. Preparing a data
folder (decoding the game's MST/text trees) still needs the development workspace's tools; moving that into
the app is the next step.
