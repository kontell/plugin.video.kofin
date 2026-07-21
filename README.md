[![License: GPL-3.0-only](https://img.shields.io/badge/License-GPL%20v3-blue.svg)](LICENSE)

# Kofin for Jellyfin

Kodi video and music addon for [Jellyfin](https://jellyfin.org). Mirrors selected server libraries directly into Kodi's own databases (`MyVideos` / `MyMusic`), so Jellyfin content appears as a **native Kodi library** — browsable in the standard views, widgets and skins — with playback resolved back to the server through `plugin://` paths.

A rewrite of [jellyfin-kodi](https://github.com/jellyfin/jellyfin-kodi) on the principle "rewrite the shell, transplant the organs": new entry points, settings and lifecycle wrapped around the fork's proven Kodi database writers.

For Jellyfin Live TV, see the companion [Kofin PVR for Jellyfin](https://github.com/kontell/pvr.kofin) addon.

**Requires:** Kodi 21 "Omega" or Kodi 22 "Piers". Jellyfin 10.11.x or later.

## Features

- Mirror Jellyfin **movies, TV shows, music and music videos** into Kodi's native library
- Real Kodi database rows — skins, widgets and "recently added" work with no plugin awareness
- Incremental and full sync, resumable across restarts; items removed on the server are pruned
- Real-time updates over websocket, with a fast change-feed path when the server companion plugin is installed
- Per-item playback decision — direct play, remux or transcode — from a device profile you control
- **Play with transcoding** context item: pick a bitrate, or the source bitrate
- **Media segment skipping** and **Play Next** prompt for the next episode integrated.
- **SyncPlay** — watch in sync with other Jellyfin clients
- **Who's watching?** — toggle additional users onto the session for multi-user watch state
- Browse special features / extras
- Login with username and password, or Quick Connect

## Installation

Install via the [Kontell Repository](https://github.com/kontell/repository.kontell).

## Configuration

- After install the addon appears under **Add-ons → Video add-ons → Kofin**.
- Open the **Account** tab (from the addon's Settings entry, or Add-ons → My add-ons → Video add-ons → Kofin → Configure), enter your server address and sign in.
- On the **Library** tab, choose which server libraries to mirror. The background service syncs them into Kodi's library — the first sync can take a while, later ones are incremental.
- **Sync**, **Playback** and **Transcoding** each have their own settings tab for sync behaviour, media segments / Play Next / SyncPlay, and the transcoding device profile.

### Server address

- The server address may be a bare host or IP (e.g. `192.168.1.10`), a `host:port`, or a full URL. `http` and port `8096` are assumed when the scheme and port are omitted. **Use `https://` when connecting over the internet.**
- On login the addon stores a Jellyfin access token (not your password) in Kodi's addon settings. Like all Kodi addon settings it is kept **in plaintext** under `userdata/addon_data/plugin.video.kofin/` — be aware of this when sharing Kodi backups or your addon_data folder. Signing out revokes the token on the server.

## Companion server plugin

For fast, low-cost syncing, install the **KodiSyncQueue** (or **KofinSyncQueue** for improved performance) server plugin on Jellyfin: it gives the addon a typed change feed so catch-up only touches what actually changed. Without it Kofin still works, using real-time websocket updates only.

## Supported platforms

Kofin is pure Python and runs anywhere Kodi 21/22 does. Because library sync writes Kodi's own database, it is gated to the schema versions it has been proven against; any other version is refused for writing (browsing and playback still work) until support is added.

| Kodi | Video database | Music database |
|------|----------------|----------------|
| 21 "Omega" | MyVideos131 | MyMusic83 |
| 22 "Piers" | MyVideos146 | MyMusic84 |

## License

This project is licensed under the [GPL-3.0-only](LICENSE) license.
