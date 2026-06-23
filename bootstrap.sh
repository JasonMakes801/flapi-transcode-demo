#!/usr/bin/env bash
# Bootstrap the transcode demo on a Mac that has Baselight/flapid installed.
#
# Creates a local .venv, installs the demo + FastAPI/uvicorn, and pip-installs
# the FilmLight `filmlightapi` wheel from the installed Baselight build (the
# wheel is versioned to the build, not on PyPI). No Docker, no network needed
# beyond PyPI for FastAPI/uvicorn.
#
#   ./bootstrap.sh            # auto-detect the running build's wheel
#   FLAPI_WHEEL=/path.whl ./bootstrap.sh   # force a specific wheel
set -euo pipefail
cd "$(dirname "$0")"

echo "==> Locating the FilmLight FLAPI wheel"
WHEEL="${FLAPI_WHEEL:-}"
if [[ -z "$WHEEL" ]]; then
  # Prefer the build whose flapid is actually running (the one serving :1984).
  APP_ROOT="$(ps -ax -o command= 2>/dev/null \
    | grep -oE '/Applications/[^ ]+\.app/Contents/bin/flapid' \
    | head -1 | sed 's#/Contents/bin/flapid##' || true)"
  if [[ -n "$APP_ROOT" ]]; then
    WHEEL="$(ls -t "$APP_ROOT"/Contents/share/flapi/python/filmlightapi-*.whl 2>/dev/null | head -1 || true)"
  fi
fi
if [[ -z "$WHEEL" ]]; then
  # Fall back to the newest wheel of any installed Baselight/BaselightLOOK/Nara.
  WHEEL="$(ls -t /Applications/{Baselight,BaselightLOOK,Nara}/*/*.app/Contents/share/flapi/python/filmlightapi-*.whl 2>/dev/null | head -1 || true)"
fi
if [[ -z "$WHEEL" || ! -f "$WHEEL" ]]; then
  echo "ERROR: could not find a filmlightapi-*.whl. Is Baselight installed?" >&2
  echo "       Set FLAPI_WHEEL=/path/to/filmlightapi-*.whl and re-run." >&2
  exit 1
fi
echo "    wheel: $WHEEL"

echo "==> Choosing a Python interpreter"
PY=""
for cand in "$HOME/.local/bin/python3.11" python3.11 python3.10 python3.12 python3; do
  if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
done
[[ -z "$PY" ]] && { echo "ERROR: no python3 found" >&2; exit 1; }
echo "    python: $PY ($($PY --version 2>&1))"

echo "==> Creating .venv"
"$PY" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --quiet --upgrade pip

echo "==> Installing demo + FastAPI/uvicorn"
pip install --quiet -e .

echo "==> Installing FLAPI wheel"
pip install --quiet "$WHEEL"

echo "==> Verifying 'import flapi'"
python - <<'PY'
import flapi
print("    flapi OK:", flapi.__file__)
PY

echo
echo "Done. Start the demo with:  ./run.sh"
echo "Then open:  http://$(hostname):8080  (or http://localhost:8080 on this Mac)"
