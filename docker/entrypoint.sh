#!/bin/sh
# Install the extensions this install names, migrate the workspace, then run compass.
#
# What is installed: COMPASS_EXTENSIONS (pip requirements without spaces,
# separated by spaces or commas: a PyPI name, name@git+https://…, or a wheel
# under /compass/extensions/wheels) plus the captains installed from the
# Captains page. compass_core.extensions keeps them in /compass/extensions/site,
# rebuilt when the set changes or a new Compass version runs; a failed install
# keeps the previous set.
#
# For the long-running commands (serve, api) `compass migrate` then creates the
# tables a new version needs and runs its migration steps, once per workspace,
# under the same lock. It never keeps Compass from starting.
set -e
EXT_DIR="${COMPASS_EXTENSIONS_DIR:-/compass/extensions}"
export COMPASS_EXTENSIONS_DIR="$EXT_DIR"
python -m compass_core.extensions sync || echo "compass: extension sync failed; starting with what was installed" >&2
export PYTHONPATH="${EXT_DIR}/site${PYTHONPATH:+:$PYTHONPATH}"
case "${1:-}" in
  serve|api)
    compass migrate >/dev/null || echo "compass: migrate did not run (no workspace yet, or the database is down); starting anyway" >&2
    ;;
esac
exec compass "$@"
