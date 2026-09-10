#!/usr/bin/env bash
# Run the services_v2 unit tests ON A JAM PLAYER.
#
# These tests import the real service modules (dbus, GLib, sdnotify, requests,
# nacl) and therefore run in the device's venv on a Pi -- not on a developer
# Mac. They never touch the D-Bus system bus, NetworkManager, or the network:
# every external call is patched with unittest.mock, so they are safe to run
# on a fielded/registered player.
#
#   sudo /opt/jam/venv/bin/python3 -m unittest discover -s tests -t . -v
#
# (from /opt/jam/jam-player/src/jam_player/services_v2, or wherever the
# checkout lives on the device). This wrapper just resolves those paths.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SV="$(dirname "$HERE")"
PY="${JAM_PYTHON:-/opt/jam/venv/bin/python3}"
[ -x "$PY" ] || PY="$(command -v python3)"
cd "$SV"
exec "$PY" -m unittest discover -s tests -t . "${@:--v}"
