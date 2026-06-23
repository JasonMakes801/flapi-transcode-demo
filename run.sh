#!/usr/bin/env bash
# Launch the transcode demo web server (after ./bootstrap.sh).
#   ./run.sh                 # serve on 0.0.0.0:8080
#   TCDEMO_PORT=9000 ./run.sh
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -d .venv ]]; then
  echo "No .venv found — run ./bootstrap.sh first." >&2
  exit 1
fi
# shellcheck disable=SC1091
source .venv/bin/activate
exec transcode-demo
