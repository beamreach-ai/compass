#!/bin/sh
# Install the extensions this install names, then run compass.
#
# COMPASS_EXTENSIONS: pip requirement specifiers, separated by spaces or commas
# (a distribution on PyPI, a git URL, a wheel under /compass/extensions/wheels).
# They are installed into /compass/extensions, a volume, so a restart keeps
# them and a changed list installs only what is new. Each extension's
# `compass.captains` entry points are found from there (compass_core.captains).
set -e
EXT_DIR="${COMPASS_EXTENSIONS_DIR:-/compass/extensions}"
export PYTHONPATH="${EXT_DIR}${PYTHONPATH:+:$PYTHONPATH}"
if [ -n "${COMPASS_EXTENSIONS:-}" ]; then
  mkdir -p "$EXT_DIR"
  wanted="$(printf '%s' "$COMPASS_EXTENSIONS" | tr ',' ' ')"
  if [ "$(cat "$EXT_DIR/.installed" 2>/dev/null)" != "$wanted" ]; then
    echo "compass: installing extensions into $EXT_DIR: $wanted" >&2
    # shellcheck disable=SC2086
    pip install --quiet --no-cache-dir --target "$EXT_DIR" --upgrade --find-links "$EXT_DIR/wheels" $wanted
    printf '%s' "$wanted" > "$EXT_DIR/.installed"
  fi
fi
exec compass "$@"
