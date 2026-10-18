#!/bin/sh
set -eu
exec /app/.venv/bin/uvicorn switchproof.api:app --host 0.0.0.0 --port "${PORT:-8080}" --proxy-headers --forwarded-allow-ips='*'
