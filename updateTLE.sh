#!/bin/bash
TLEDIR="${TLEDIR:-/home/slowimmediate/groundstation-data/TLE}"
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)" || exit 1

# One validated download; preserve the existing cache on failure. Additional
# arguments (e.g. --url for a mirror) are passed to the shared updater.
exec "${PYTHON:-python3}" "$SCRIPT_DIR/tle.py" --cache "$TLEDIR/noaa.txt" "$@"
