#!/usr/bin/env bash
# Back to healthy: capture off, MongoDB's disk wiped, everything restarted.
set -euo pipefail
cd "$(dirname "$0")/.."
curl -s -X POST http://127.0.0.1:8080/admin/debug-capture/off || true; echo
docker compose stop mongodb orders-api loadgen
sudo find /mnt/mongo-data -mindepth 1 -delete
docker compose up -d
df -h /mnt/mongo-data | tail -1
