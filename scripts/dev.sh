#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ ! -f web/dist/index.html ]; then
  npm --prefix web run build
fi
exec uv run uvicorn switchproof.api:app --host 127.0.0.1 --port 18765
