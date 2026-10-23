#!/usr/bin/env bash
# Rebuild and restart after copying a new version into /opt/schulblick. Data and secrets are kept.
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose build
docker compose up -d
# the web app needs a few seconds to start; wait up to 60 s
for i in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8130/healthz >/dev/null 2>&1; then
    docker compose ps
    echo "Web-App läuft."
    exit 0
  fi
  sleep 2
done
docker compose ps
echo "Web-App antwortet nach 60 s nicht. Log:"
docker compose logs --tail 40 web
exit 1
