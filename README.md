[![License: GPL-3.0-only](https://img.shields.io/badge/License-GPL%20v3-blue.svg)](LICENSE)

# Kofin for Jellyfin — API development branch

`kofin-or` develops an official-repository variant of Kofin using Kodi's public APIs and Kofin-owned storage. It targets **Piers or later**. The internal version is **0.90.0**; no OR prerelease has been published and official repository acceptance has not been obtained.

Phase 1 provides shared metadata/sync contracts, private storage, package boundaries and an API capability report. **Native library ingestion is not enabled yet.** Movie ingestion is phase 3. The [implementation plan](docs/kofin-or-implementation-plan.md), [parity ledger](docs/kofin-or-parity.md) and [phase 1 evidence](docs/research/kofin-or/phase1/README.md) track what is implemented and what remains.

The maintained SQL variant and its releases continue on [main](https://github.com/kontell/plugin.video.kofin/tree/main).

## Available in the internal API build

- Live Jellyfin browsing across all media types, including unsynced libraries.
- Filters, search, Next up, Continue watching and extras.
- Streaming with direct play, remux/transcode and media-source selection.
- Music Play all/Shuffle, watched/favourite actions and resume controls.
- Existing SyncPlay, segment skipping, Play next and additional-user playback features.
- Dynamic skin entries and a server backdrop stored in the addon profile.

Native sync, downloads/subscriptions, native chapter art, actor precache, native node/playlist maintenance and legacy repair/cleaning are disabled until their public API ports are ready. The distribution physically omits their native SQL dependencies. Old settings cannot enable them.

## Development builds

Requires the measured Piers API floor (`xbmc.addon` 21.90.802, Python addon API 3.0.1) and Jellyfin 10.11 or later. Version/method presence alone does not qualify a stock release. The existing dirty P1D Flatpak is explicitly approved for development until RC1 binaries are available; a public preview still needs stock-binary behavioral qualification. Omega is outside this branch's scope.

```bash
python3 tools/build.py
```

The branch selects the `api` package by default. ZIPs and `tools/dev-install.sh` use the same package manifest. The first public GitHub prerelease and development repository delivery follow the later phases; current CI ZIPs are internal artifacts.

Use an isolated, fresh Kodi profile for development. Both variants have the same addon ID and cannot coexist in one profile. Switching an existing library requires clearing the native library and Kofin sync state before rebuilding; the API build neither adopts old native IDs nor provides the old broad database cleaner. This phase performs no automatic reset.

After installation, configure the server and log in from Kofin's Account settings. Dynamic browsing works independently of native sync. The service writes `api-capabilities.json` in its addon profile, with native sync explicitly disabled.

For Jellyfin Live TV, see [Kofin PVR](https://github.com/kontell/pvr.kofin). The optional lyrics and SyncPlay companion integrations retain their separate installation requirements.
