#!/usr/bin/env sh
# macOS / Linux launcher.  ./run.sh        (this PC only)
#                          ./run.sh lan    (reachable from other PCs on the network)
set -e
cd "$(dirname "$0")"
if ! .venv/bin/python -c "import beeplabel" 2>/dev/null; then
  echo "[first run] creating virtualenv and installing..."
  [ -x .venv/bin/python ] || python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip >/dev/null
  .venv/bin/python -m pip install .
fi
HOST=127.0.0.1
[ "$1" = "lan" ] && HOST=0.0.0.0
exec .venv/bin/python -m beeplabel serve --host "$HOST"
