#!/bin/bash
# Install the working tree into a local Kodi and (re)load it.
#
# Usage: tools/dev-install.sh [--flatpak]
#
#   (default)   the native Kodi under ~/.kodi
#   --flatpak   the flatpak Kodi (tv.kodi.Kodi) under ~/.var/app/tv.kodi.Kodi/data
#
# The JSON-RPC port, its credentials and the EventServer port are read from the
# target's own userdata/guisettings.xml, so the two generations can sit on
# different ports (the flatpak is on 8081/9778 so both can run at once). The
# credentials are parsed inside this script and passed to curl through a
# variable; they are never printed.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
KODI_HOME="$HOME/.kodi"
TARGET="native"

case "${1:-}" in
    --flatpak) KODI_HOME="$HOME/.var/app/tv.kodi.Kodi/data"; TARGET="flatpak" ;;
    "") ;;
    *) echo "usage: $0 [--flatpak]" >&2; exit 2 ;;
esac

DEST="$KODI_HOME/addons/plugin.video.kofin"
GUISETTINGS="$KODI_HOME/userdata/guisettings.xml"

# port|esport|user|password, one line, from the target's guisettings.xml.
read -r RPC_PORT ES_PORT RPC_USER RPC_PASS < <(python3 - "$GUISETTINGS" <<'PY'
import sys, xml.etree.ElementTree as E
try:
    root = E.parse(sys.argv[1]).getroot()
except Exception:
    root = None
def get(key, default):
    if root is None:
        return default
    node = root.find(f"./setting[@id='{key}']")
    return default if node is None or not (node.text or "").strip() else node.text.strip()
print(get("services.webserverport", "8080"), get("services.esport", "9777"),
      get("services.webserverusername", "kodi"), get("services.webserverpassword", "kodi"))
PY
)
KODI_RPC="http://localhost:${RPC_PORT}/jsonrpc"

rsync -a --delete \
    --exclude '.git' --exclude '.venv' --exclude '.tox' \
    --exclude '__pycache__' --exclude '.mypy_cache' --exclude '.pytest_cache' \
    --exclude 'docs' --exclude 'tests' --exclude 'tools' \
    --exclude 'mypy.ini' --exclude 'tox.ini' --exclude 'pyproject.toml' \
    --exclude 'requirements-dev.txt' \
    "$SRC/" "$DEST/"

rpc() {
    curl -s -m "${2:-5}" -u "$RPC_USER:$RPC_PASS" -X POST -H 'Content-Type: application/json' \
        -d "$1" "$KODI_RPC"
}

if ! rpc '{"jsonrpc":"2.0","id":1,"method":"JSONRPC.Ping"}' 2 | grep -q '"pong"'; then
    echo "installed to $DEST ($TARGET Kodi not reachable on :$RPC_PORT — skipped reload/enable)"
    exit 0
fi

KODI_HOST=localhost KODI_ESPORT="$ES_PORT" "$HOME/bin/kodi-builtin" 'UpdateLocalAddons()'
sleep 2
rpc '{"jsonrpc":"2.0","id":1,"method":"Addons.SetAddonEnabled","params":{"addonid":"plugin.video.kofin","enabled":true}}' > /dev/null
echo "installed and enabled plugin.video.kofin ($TARGET Kodi on :$RPC_PORT)"
