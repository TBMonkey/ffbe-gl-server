# ffbe-gl-server

A **local server for the global client of FINAL FANTASY BRAVE EXVIUS** (v9.0.0), so the game can run against
your own machine. It answers the client's HTTPS requests and serves the content the client asks for. **The game
client is not included** — you bring your own copy of the game.

An **unofficial fan project**, not affiliated with Square Enix. It does ship battle content derived from the
game's data, and Square Enix's rights in that remain theirs — [`ACKNOWLEDGEMENTS.md`](ACKNOWLEDGEMENTS.md) states
exactly what is and is not included, and under what licence.

| directory | what it is |
|---|---|
| `server/` | the engine: the protocol, the player state, the authored reply content |
| `server_app/` | the desktop app (Rust) that runs the engine, checks the data it needs, and guides the Android side |
| `scripts/` | the two decoders `prepare` uses (`mst_decode.py`, `text_decode.py`) |
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

**0.1.8** is the release. The app owns the whole path from a user's two inputs:

```bash
ffbe-server-app prepare --apk <your APK> --client-data <your app-data folder>
ffbe-server-app start
```

`prepare` extracts `libgame.so` from the APK, decodes the MST and localized-text trees itself (the reference
run: **219 MST tables / 590,632 records** and **145 text tables / 335,637 records**), generates the CA and
leaf in-process, and writes `prepare.json`. No checkout, no Python, no PyYAML and no `openssl` is needed on
the target machine; the bundle's smoke test unpacks the zip into an empty directory and runs exactly that
path.

The engine is **0.1.5** and names the release it came from: `ffbe-gl/0.1.5+v0.1.8 (TBMonkey)` — in the HTTP
`Server:` header, the startup line, and the app's doctor.
