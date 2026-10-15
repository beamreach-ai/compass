#!/bin/sh
# Install the extensions this install names, then run compass.
#
# What is installed: COMPASS_EXTENSIONS (pip requirements without spaces,
# separated by spaces or commas: a PyPI name, name@git+https://…, or a wheel
# under /compass/extensions/wheels) plus the captains installed from the
# Captains page. compass_core.extensions keeps them in /compass/extensions/site,
# rebuilt only when the set changes; a failed install keeps the previous set.
set -e
EXT_DIR="${COMPASS_EXTENSIONS_DIR:-/compass/extensions}"
export COMPASS_EXTENSIONS_DIR="$EXT_DIR"
python -m compass_core.extensions sync || echo "compass: extension sync failed; starting with what was installed" >&2
export PYTHONPATH="${EXT_DIR}/site${PYTHONPATH:+:$PYTHONPATH}"
exec compass "$@"
