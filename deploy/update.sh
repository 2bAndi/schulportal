#!/usr/bin/env bash
# Rebuild and restart after copying a new version into /opt/schulblick. Data and secrets are kept.
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose build
docker compose up -d
docker compose ps
curl -fsS http://127.0.0.1:8130/healthz >/dev/null && echo "Web-App läuft."
