#!/usr/bin/env bash
# build_dist.sh — assemble a release bundle: the GUI binary + a frozen engine. Nothing else.
#
# The bundle contains CODE ONLY. It never ships game content: the app's doctor tells the user which
# local data trees to produce, and from which tool. That is a deliberate design rule (see README).
#
# Layout produced (also the layout the app auto-detects at startup):
#
#   dist/ffbe-server-app-<version>/
#     ffbe-server-app          the GUI (cargo --release)
#     engine/ffbe-engine       a PyInstaller onefile — needs no Python on the target
#     README.md  CHANGELOG.md  deps.json  VERSION
#
# Usage:  ./build_dist.sh            # cached venv + PyInstaller; ~1-3 min cold
#         PYTHON=python3.12 ./build_dist.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(dirname "$HERE")"                      # APK_emulator_work/
ENGINE_SRC="$WORK/server"
HARNESS="$WORK/harness"
VERSION="$(grep -m1 '^version' "$HERE/Cargo.toml" | cut -d'"' -f2)"
BUILD="$HERE/build"
DIST="$HERE/dist"
PLATFORM="$(uname -s | tr '[:upper:]' '[:lower:]')-$(uname -m)"
OUT="$DIST/ffbe-server-app-$VERSION"
PY="${PYTHON:-python3}"

[ "$(uname -s)" = "Linux" ] || { echo "build_dist.sh is Linux-only for now (venv/bin layout, uname, ':' in --add-data)" >&2; exit 1; }
[ -n "$VERSION" ] || { echo "could not read the version from $HERE/Cargo.toml" >&2; exit 1; }

say() { printf '== %s\n' "$*"; }
[ -f "$ENGINE_SRC/capture_server.py" ] || { echo "engine not found at $ENGINE_SRC" >&2; exit 1; }
[ -f "$HARNESS/break_status_community.json" ] || { echo "missing $HARNESS/break_status_community.json" >&2; exit 1; }

# 1. PyInstaller venv (cached under build/, which is gitignored)
if [ ! -x "$BUILD/venv/bin/pyinstaller" ]; then
  say "creating the build venv and installing PyInstaller"
  "$PY" -m venv "$BUILD/venv"
  "$BUILD/venv/bin/pip" install -q --upgrade pip
  "$BUILD/venv/bin/pip" install -q pyinstaller pyyaml cryptography
fi
# The bundle SWAPS the openssl CLI for the cryptography library, so the venv must have it even when the
# venv already existed (the block above only installs on a fresh venv).
"$BUILD/venv/bin/python" -c 'import cryptography' 2>/dev/null \
  || "$BUILD/venv/bin/pip" install -q cryptography

say "checking the two AES backends agree (openssl CLI vs the bundled cryptography)"
"$BUILD/venv/bin/python" "$WORK/harness/crypto_backend_check.py" --require-lib
say "checking the cert generator (in-process CA + leaf; subject hash vs openssl)"
"$BUILD/venv/bin/python" "$WORK/harness/cert_check.py"

# The dispatch table is generated, engine-side data: ALWAYS rebuild it (a stale one ships silently
# otherwise), and add it beside the frozen modules (the engine resolves HERE/endpoints.json when out/
# is absent).
say "generating the dispatch table (harness/build_endpoints.py)"
( cd "$WORK" && "$PY" harness/build_endpoints.py >/dev/null )

# 2. the frozen engine. Data files land beside the frozen modules, which is where the engine reads
# them (HERE-relative), including the break table and this table.
say "freezing the engine (onefile)"
DATA_ARGS=()
for f in "$ENGINE_SRC"/*.json "$ENGINE_SRC"/*.yaml "$ENGINE_SRC"/*.txt; do
  [ -e "$f" ] && DATA_ARGS+=(--add-data "$f:.")
done
DATA_ARGS+=(--add-data "$ENGINE_SRC/content:content"
            --add-data "$ENGINE_SRC/assets:assets"
            --add-data "$ENGINE_SRC/webcontent:webcontent"
            --add-data "$HARNESS/break_status_community.json:."
            --add-data "$WORK/out/endpoints.json:.")
"$BUILD/venv/bin/pyinstaller" --noconfirm --clean --onefile \
  --name ffbe-engine \
  --distpath "$BUILD/pyi-dist" --workpath "$BUILD/pyi-work" --specpath "$BUILD/pyi-spec" \
  --paths "$ENGINE_SRC" \
  "${DATA_ARGS[@]}" \
  "$ENGINE_SRC/capture_server.py"

# 3. the GUI
say "building the GUI (release)"
( cd "$HERE" && cargo build --release --quiet )

# 4. assemble into build/ FIRST, smoke-test, and only then move it into dist/ -- a failure midway must
# not destroy the previous good bundle or leave a half one looking publishable.
STAGE="$BUILD/out-$VERSION"
say "assembling $STAGE"
rm -rf "$STAGE"
mkdir -p "$STAGE/engine"
cp "$HERE/target/release/ffbe-server-app" "$STAGE/ffbe-server-app"
cp "$BUILD/pyi-dist/ffbe-engine" "$STAGE/engine/ffbe-engine"
cp "$HERE/README.md" "$HERE/deps.json" "$HERE/CHANGELOG.md" "$STAGE/"
printf '%s\n' "$VERSION" > "$STAGE/VERSION"

say "smoke-testing the bundle"
"$STAGE/ffbe-server-app" --version >/dev/null || { echo "app binary failed --version" >&2; exit 1; }
"$STAGE/engine/ffbe-engine" --print-paths 2>/dev/null | grep -q '^contract=' \
  || { echo "frozen engine failed --print-paths (a data file is probably missing from DATA_ARGS)" >&2; exit 1; }
"$STAGE/engine/ffbe-engine" --print-paths 2>/dev/null | grep -q '^crypto=cryptography' \
  || { echo "frozen engine has no bundled crypto backend (it would spawn the openssl CLI per request)" >&2; exit 1; }

# The START smoke: the one that would have caught 0.1.3. Its bug (the port guard killing the frozen
# engine's child) was invisible to --version/--print-paths and got as far as a built bundle. Prepare a
# throwaway root from the checkout and drive the real path on a free port. It needs the data store,
# which is untracked -- so a clean clone says SKIPPED loudly instead of passing the only test that starts.
if [ -d "$WORK/../mst_decoded" ] && [ -d "$WORK/out" ]; then
  SMOKE_ROOT="$BUILD/smoke-root"
  SMOKE_PORT=$(( 20000 + (RANDOM % 20000) ))
  # The smoke PATH holds only `ss` (the app's port-ownership check). That is the point of audit F1: the
  # frozen engine must answer an encrypted request with NO `openssl` binary available to it.
  SMOKE_BIN="$BUILD/smoke-bin"
  rm -rf "$SMOKE_BIN"; mkdir -p "$SMOKE_BIN"
  if [ -n "$(command -v ss)" ]; then ln -sf "$(command -v ss)" "$SMOKE_BIN/ss"; fi
  rm -rf "$SMOKE_ROOT"
  "$STAGE/ffbe-server-app" prepare --data "$SMOKE_ROOT" --from "$(cd "$WORK/.." && pwd)" >/dev/null \
    || { echo "bundle prepare failed" >&2; exit 1; }
  PATH="$SMOKE_BIN" "$STAGE/ffbe-server-app" start --data "$SMOKE_ROOT" --port "$SMOKE_PORT" --mode campaign \
    || { echo "bundle start failed on port $SMOKE_PORT (the 0.1.3 onefile-pid class)" >&2; exit 1; }
  if ! "$STAGE/ffbe-server-app" status --data "$SMOKE_ROOT" > "$BUILD/smoke-status.txt"; then
    echo "status says not running after start:" >&2
    cat "$BUILD/smoke-status.txt" >&2
    "$STAGE/ffbe-server-app" stop --data "$SMOKE_ROOT" >/dev/null || true
    exit 1
  fi
  grep -q "^listening on 0.0.0.0:${SMOKE_PORT}" "$SMOKE_ROOT/state/server.log" \
    || { echo "engine never logged listening on :$SMOKE_PORT" >&2; exit 1; }
  # One real encrypted request/response. The in-process goldens cannot see the wire cipher, so without this
  # a request-time crypto regression in the FROZEN engine would ship (audit F1, 2026-10-16).
  PATH="$SMOKE_BIN" "$BUILD/venv/bin/python" "$WORK/harness/wire_probe.py" --port "$SMOKE_PORT" \
      --ca "$SMOKE_ROOT/certs/ca.pem" --endpoints "$WORK/out/endpoints.json" \
    || { echo "wire probe failed: the frozen engine did not answer an encrypted request" >&2; \
         "$STAGE/ffbe-server-app" stop --data "$SMOKE_ROOT" >/dev/null || true; exit 1; }
  "$STAGE/ffbe-server-app" stop --data "$SMOKE_ROOT" >/dev/null || true
  rm -rf "$SMOKE_ROOT"
  say "start smoke ok (prepare + start + status + wire probe + stop on :$SMOKE_PORT, frozen engine)"
else
  say "start smoke SKIPPED: no data store at $WORK/.. (mst_decoded or out/ absent)"
fi

rm -rf "$OUT"
mv "$STAGE" "$OUT"

# 5. zip
ZIP="ffbe-server-app-$VERSION-$PLATFORM.zip"
if command -v zip >/dev/null 2>&1; then
  say "zipping (zip)"
  ( cd "$DIST" && rm -f "$ZIP" && zip -qr "$ZIP" "ffbe-server-app-$VERSION" )
  say "bundle: $DIST/$ZIP"
else
  # No Info-ZIP? Python ships a zip writer; use it rather than leaving no archive at all.
  say "zipping (python -m zipfile)"
  rm -f "$DIST/$ZIP"
  ( cd "$DIST" && "$PY" -m zipfile -c "$ZIP" "ffbe-server-app-$VERSION" )
  say "bundle: $DIST/$ZIP"
fi
