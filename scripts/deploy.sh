#!/usr/bin/env bash
# Copy the app to the board and (re)start it.
#
#   scripts/deploy.sh              copy + restart the App Lab app
#   scripts/deploy.sh --homekit    also rebuild/restart the HomeKit companion
#   scripts/deploy.sh --copy-only  copy files, restart nothing
#
# HOST defaults to the ssh alias "valentina" (arduino@192.168.0.115).
# The board has no rsync, so this streams a tar over ssh. Runtime data
# (data/: events, snapshots, config overrides) and HomeKit pairing state
# (homekit/state/) live only on the board and are never overwritten.
set -euo pipefail

HOST="${HOST:-valentina}"
REMOTE_DIR="ArduinoApps/unoq-doorbell"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

homekit=0
restart=1
for arg in "$@"; do
  case "$arg" in
    --homekit) homekit=1 ;;
    --copy-only) restart=0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

echo "==> copying to $HOST:~/$REMOTE_DIR"
COPYFILE_DISABLE=1 tar -C "$ROOT" -czf - \
  --exclude .git --exclude .venv --exclude data --exclude .cache \
  --exclude __pycache__ --exclude .pytest_cache --exclude homekit/state \
  --exclude .DS_Store --exclude sketch/build \
  . | ssh "$HOST" "mkdir -p ~/$REMOTE_DIR && tar -xzf - -C ~/$REMOTE_DIR --no-same-owner 2>/dev/null"

if [[ $restart == 1 ]]; then
  echo "==> restarting app (compiles and flashes the sketch too)"
  ssh "$HOST" "arduino-app-cli app restart ~/$REMOTE_DIR"
fi

if [[ $homekit == 1 ]]; then
  echo "==> rebuilding HomeKit companion"
  ssh "$HOST" "cd ~/$REMOTE_DIR/homekit && docker compose up -d --build"
fi

echo "==> done: http://192.168.0.115:7000"
