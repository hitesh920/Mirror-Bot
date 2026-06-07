#!/usr/bin/env bash
set -e

mkdir -p /app/downloads
mkdir -p /app/data/qbittorrent
mkdir -p /app/data/google
mkdir -p /app/logs
qbittorrent-nox --confirm-legal-notice --webui-port=8080 --profile=/app/data/qbittorrent >/app/logs/qbittorrent.log 2>&1 &
python -m mirrorbot
