#!/bin/sh
# Entrypoint of the dash-server container image.
# Points per-app environments at the image's dash-server wheel, unless the operator chose another
# helper package, and starts the server with the settings from the environment.
set -eu

if [ -z "${DASH_SERVER_APP_HELPER_PACKAGE:-}" ]; then
  for wheel in /opt/dash-server/dist/dash_server-*.whl; do
    # An unmatched glob stays literal; only take files that exist.
    if [ -f "$wheel" ]; then
      DASH_SERVER_APP_HELPER_PACKAGE="$wheel"
    fi
  done
  if [ -z "${DASH_SERVER_APP_HELPER_PACKAGE:-}" ]; then
    echo "dash-server: no dash-server wheel in /opt/dash-server/dist; set DASH_SERVER_APP_HELPER_PACKAGE." >&2
    exit 1
  fi
  export DASH_SERVER_APP_HELPER_PACKAGE
fi

exec dash-server "$@"
