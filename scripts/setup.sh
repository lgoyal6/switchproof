#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
uv sync --python 3.12 --locked
npm --prefix web ci --no-audit --no-fund
npm --prefix web run build
docker compose up -d --wait
