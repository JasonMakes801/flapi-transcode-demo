#!/usr/bin/env bash
# Watch the demo drive itself through the happy path in a real browser.
# Needs a licensed Baselight (it does an actual scan + transcode) and the test
# media folder (set TCDEMO_TEST_MEDIA, or it uses the default dev folder).
#
#   ./run_e2e.sh                 # headed + slowed down, so you can watch
#   ./run_e2e.sh --headless      # or pass any extra pytest/playwright flags
set -euo pipefail
cd "$(dirname "$0")"

# Use the project venv if present.
[ -d .venv ] && source .venv/bin/activate

# Dev-only test deps + the Chromium browser (both idempotent).
pip install -q pytest pytest-playwright
python -m playwright install chromium

# Default to headed + slowmo; anything you pass overrides/extends it.
if [[ "$*" == *--headless* ]]; then
  exec pytest tests/test_e2e_happy_path.py -s "$@"
else
  exec pytest tests/test_e2e_happy_path.py --headed --slowmo 500 -s "$@"
fi
