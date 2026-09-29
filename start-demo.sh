#!/usr/bin/env bash
# Run me:  bash start-demo.sh   (Mac / Linux). Sets everything up, then opens the dashboard.
cd "$(dirname "$0")" || exit 1

command -v python3 >/dev/null 2>&1 || { echo "Python 3 not found. Install it from python.org"; exit 1; }
[ -d .venv ] || python3 -m venv .venv || { echo "Could not create the virtual environment"; exit 1; }
# shellcheck disable=SC1091
source .venv/bin/activate
command -v reviewer >/dev/null 2>&1 || pip install -e . -q || { echo "Install failed. Check your internet."; exit 1; }

echo "Starting the demo dashboard. Keep this window open. Press Ctrl+C to stop."
reviewer serve --demo --open
