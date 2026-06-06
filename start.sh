#!/usr/bin/env bash
set -e

mkdir -p /app/downloads
mkdir -p /app/data/qbittorrent
qbittorrent-nox --webui-port=8080 --profile=/app/data/qbittorrent >/app/qbittorrent.log 2>&1 &
python -m mirrorbot
