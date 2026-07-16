#!/bin/bash
# Install the working tree into the local Kodi and (re)load it.
# Usage: tools/dev-install.sh
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$HOME/.kodi/addons/plugin.video.kofin"
KODI_RPC="http://localhost:8080/jsonrpc"

rsync -a --delete \
    --exclude '.git' --exclude '.venv' --exclude '.tox' \
    --exclude '__pycache__' --exclude '.mypy_cache' --exclude '.pytest_cache' \
    --exclude 'docs' --exclude 'tests' --exclude 'tools' \
    "$SRC/" "$DEST/"

if ! curl -s -m 2 -u kodi:kodi -o /dev/null "$KODI_RPC" \
        -X POST -H 'Content-Type: application/json' \
        -d '{"jsonrpc":"2.0","id":1,"method":"JSONRPC.Ping"}'; then
    echo "installed to $DEST (Kodi not reachable — skipped reload/enable)"
    exit 0
fi

"$HOME/bin/kodi-builtin" 'UpdateLocalAddons()'
sleep 2
curl -s -u kodi:kodi -X POST -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","id":1,"method":"Addons.SetAddonEnabled","params":{"addonid":"plugin.video.kofin","enabled":true}}' \
    "$KODI_RPC" > /dev/null
echo "installed and enabled plugin.video.kofin"
