#!/usr/bin/env bash
set -euo pipefail

mkdir -p /app/downloads /app/data/qbittorrent /app/data/google /app/logs

qbittorrent-nox --confirm-legal-notice --webui-port=8080 --profile=/app/data/qbittorrent >/app/logs/qbittorrent.log 2>&1 &
qbit_pid=$!
python -m mirrorbot &
bot_pid=$!

shutdown() {
  trap - TERM INT
  kill -TERM "$bot_pid" "$qbit_pid" 2>/dev/null || true
}
trap shutdown TERM INT

set +e
wait "$bot_pid"
bot_status=$?
kill -TERM "$qbit_pid" 2>/dev/null || true
wait "$qbit_pid" 2>/dev/null || true
exit "$bot_status"
