#!/usr/bin/env bash
# Store (or remove) the Anthropic API key for the worker. Usage: sudo bash deploy/ki-schluessel.sh [--aus]
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077
mkdir -p secrets && chmod 700 secrets
if [[ "${1:-}" == "--aus" ]]; then
  : > secrets/anthropic_key
  echo "KI ausgeschaltet (Schlüssel geleert)."
else
  read -rsp "Anthropic-API-Schlüssel (sk-ant-…, Eingabe bleibt unsichtbar): " KEY; echo
  [[ "$KEY" == sk-ant-* ]] || { echo "Das sieht nicht wie ein Anthropic-Schlüssel aus."; exit 1; }
  printf '%s\n' "$KEY" > secrets/anthropic_key
  unset KEY
fi
chown 10001:10001 secrets/anthropic_key
chmod 400 secrets/anthropic_key
docker compose up -d --force-recreate worker
sleep 3
docker compose exec worker schulblick ai-status || true
