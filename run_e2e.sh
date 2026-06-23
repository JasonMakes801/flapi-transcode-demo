#!/usr/bin/env bash
# Watch the demo drive itself through the happy path in a real browser.
# Needs a licensed Baselight (it does an actual scan + transcode) and the test
# media folder (set TCDEMO_TEST_MEDIA, or it uses the default dev folder).
#
# One-time setup, into the venv ./bootstrap.sh made:
#   pip install -e ".[test]" && python -m playwright install chromium
# Then:
#   ./run_e2e.sh                 # headed + slowed down, so you can watch
#   ./run_e2e.sh --headless      # or pass any extra pytest/playwright flags
set -euo pipefail
cd "$(dirname "$0")"

# Use the project venv if present.
[ -d .venv ] && source .venv/bin/activate

# Pure runner — no installs. Point the way if the test extra isn't there yet.
if ! python -c "import pytest_playwright" >/dev/null 2>&1; then
  echo "Test deps missing. Install them once with:" >&2
  echo "  pip install -e \".[test]\" && python -m playwright install chromium" >&2
  exit 1
fi

# Default to headed + slowmo; anything you pass overrides/extends it.
if [[ "$*" == *--headless* ]]; then
  exec pytest tests/test_e2e_happy_path.py -s "$@"
else
  exec pytest tests/test_e2e_happy_path.py --headed --slowmo 500 -s "$@"
fi
