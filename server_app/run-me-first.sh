#!/bin/sh
# run-me-first.sh — the one thing to run before anything else.
#
# It builds the server's data folder from the TWO things you own:
#   (1) your FINAL FANTASY BRAVE EXVIUS APK   (the tables' keys live in its libgame.so)
#   (2) your client's data folder             (the game's own downloaded tables and text)
# Nothing else is needed: no Python, no openssl, no source checkout.
#
#   ./run-me-first.sh [/path/to/FFBEWW.apk] [/path/to/client-data] [data folder]
#
# `client-data` may be the app-data folder (…/com.square_enix.android_googleplay.FFBEWW) or its `files/`.
# Everything it writes goes into the data folder (default: ./ffbe-data next to this script) and can be
# deleted and rebuilt at any time.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
ENGINE="$HERE/engine/ffbe-engine"
[ -f "$ENGINE" ] || { echo "error: engine not found at $ENGINE -- unpack the whole archive first" >&2; exit 1; }
# Some unzippers (`python -m zipfile -e`, some GUIs) do not restore the executable bit. Self-heal rather than
# fail: the file is already ours, and "not executable" is not something a user should have to diagnose.
[ -x "$ENGINE" ] || chmod +x "$ENGINE" 2>/dev/null || { echo "error: cannot make $ENGINE executable (read-only unpack?)" >&2; exit 1; }

APK=${1:-}
CLIENT=${2:-}
ROOT=${3:-$HERE/ffbe-data}

if [ -z "$APK" ]; then
  printf 'Path to your FFBEWW APK: '
  read -r APK
fi
if [ -z "$CLIENT" ]; then
  printf 'Path to your client data folder (…/com.square_enix.android_googleplay.FFBEWW, or its files/): '
  read -r CLIENT
fi

"$ENGINE" --prepare --apk "$APK" --client-data "$CLIENT" --data "$ROOT"

cat <<EOF

Prepared. Next steps:
  1. start the server:   $HERE/ffbe-server-app start --data "$ROOT"
     (or open $HERE/ffbe-server-app with no arguments and use the Server pane)
  2. on the emulator:    trust $ROOT/certs/ca.pem in the SYSTEM store,
                         and point v900-lapis.gumi.sg at this machine
  3. launch the game.
EOF
