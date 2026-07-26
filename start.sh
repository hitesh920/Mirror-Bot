#!/usr/bin/env bash
set -euo pipefail

mkdir -p /app/downloads /app/data/qbittorrent /app/logs

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
first_pid=""
while [[ -z "$first_pid" ]]; do
  wait -n -p first_pid "$bot_pid" "$qbit_pid"
  first_status=$?
done

if [[ "$first_pid" == "$qbit_pid" ]]; then
  kill -TERM "$bot_pid" 2>/dev/null || true
  wait "$bot_pid" 2>/dev/null || true
  bot_status=$first_status
else
  bot_status=$first_status
fi

kill -TERM "$qbit_pid" 2>/dev/null || true
wait "$qbit_pid" 2>/dev/null || true
exit "$bot_status"
