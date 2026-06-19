#!/usr/bin/env bash
# Real Utility Savings - local launcher
# Usage:  ./run.sh            (localhost only)
#         HOST=0.0.0.0 ./run.sh   (expose on your LAN / staging box)
set -e

cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "Creating virtualenv..."
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

pip install -q -r requirements.txt

echo
echo "Starting engine. Open the site at:  http://${HOST:-127.0.0.1}:${PORT:-5001}/site"
echo "(Ctrl+C to stop)"
echo
python3 server.py
