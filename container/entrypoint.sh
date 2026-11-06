#!/bin/sh
# Entrypoint of the dash-server container image.
# Points per-app environments at the image's dash-server wheel, unless the operator chose another
# helper package, and starts the server with the settings from the environment.
set -eu

if [ -z "${DASH_SERVER_APP_HELPER_PACKAGE:-}" ]; then
  for wheel in /opt/dash-server/dist/dash_server-*.whl; do
    DASH_SERVER_APP_HELPER_PACKAGE="$wheel"
  done
  export DASH_SERVER_APP_HELPER_PACKAGE
fi

exec dash-server "$@"
