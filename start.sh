#!/usr/bin/env bash
set -euo pipefail

mkdir -p /app/downloads /app/data/qbittorrent /app/data/google /app/logs

python /app/scripts/run_qbittorrent.py &
qbit_pid=$!
python -m mirrorbot &
bot_pid=$!

shutdown() {
  trap - TERM INT
  kill -TERM "$bot_pid" 2>/dev/null || true
}
trap shutdown TERM INT

set +e
while kill -0 "$bot_pid" 2>/dev/null; do
  wait "$bot_pid"
  bot_status=$?
done
wait "$bot_pid" 2>/dev/null
bot_status=${bot_status:-$?}
kill -TERM "$qbit_pid" 2>/dev/null || true
wait "$qbit_pid" 2>/dev/null || true
exit "$bot_status"
