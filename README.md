[![License: GPL-3.0-only](https://img.shields.io/badge/License-GPL%20v3-blue.svg)](/tmp/.mount_JoplindJEoiY/resources/app.asar/LICENSE "LICENSE")

# Kofin for Jellyfin

Kodi video & music addon for Jellyfin. Browse Jellyfin libraries through the add-on or sync selected libraries directly into Kodi's own databases, so Jellyfin content appears as a Kodi library.

A rewrite of [jellyfin-kodi](https://github.com/jellyfin/jellyfin-kodi) on the principle "rewrite the shell, transplant the organs": new entry points, settings and lifecycle wrapped around the proven Kodi database writers.

For Jellyfin Live TV, see the companion [Kofin PVR](https://github.com/kontell/pvr.kofin) addon.

Requires: Kodi 21 "Omega" or Kodi 22 "Piers". Jellyfin 10.11.x or later.

## Features

### New & Improved

- Hands-off, fast, robust & resumable [sync](docs/benchmark-report.md)
- Downloads
- Consolidated, simplified settings for all add-on configuration
- SyncPlay - watch in sync with other Jellyfin clients
  - For the best experience install the SyncPlay V2 server [plugin](https://github.com/kontell/jellyfin-plugin-syncplayv2)
- Integrated media segment skipping and play next episode prompt
- Access movie special features/ extras
- Multi-version movies as Kodi video versions
- Flexible playback methods - direct play, remux or transcode. Choose supported HDR formats & max resolution
    - For transcoded playback, audio streams and image-based subtitles can be accessed *after playback starts* by returning to the playing item and bringing up the context menu.
- Play with transcoding context item: pick a bitrate
- Jellyfin chapter images in Kodi's chapter list
- Transcode music
- Jellyfin song lyrics, timed or plain (via companion add-on)
- Sync Jellyfin music playlists into Kodi
- Edit Jellyfin user audio and subtitle preferences
- Who's watching? - toggle additional users onto the session for multi-user watch state (persistent after restart)

### Legacy

- Sync Jellyfin movies, TV shows, music and music videos into Kodi's library
- Real Kodi database rows - skins, widgets and "recently added" work with no plugin awareness
- Incremental and full sync
- Real-time updates over websocket, with a fast change-feed path when the server companion plugin is installed

### Not implemented

- Native mode playback (bypassing Jellyfin server on a local network)
- Cinema mode
- Sync theme media

## Installation

Install via the [Kontell Repository](https://github.com/kontell/repository.kontell).

### Migration from Jellyfin-Kodi

1.  Disable or uninstall jellyfin-kodi, its background service must not be running.
2.  Install Kofin and enter Add-ons -> Video add-ons -> Kofin -> Settings -> Account
3.  Run `Clean databases` (the button is only visible while logged out).
    - It removes all jellyfin-kodi and Kofin library data, nodes and playlists, and optionally the music library, cached server artwork and custom library nodes.
    - Cleaning is per Kodi profile, run it in each profile that synced.

### Configuration

- After install the addon appears under Add-ons -> Video add-ons -> Kofin.
- Enter settings and open the Account tab, enter your server address and log in.
- Libraries can now be browsed in a similar manner to typical Kodi add-ons.
- To sync libraries into the local Kodi database:
    - From settings go to the Library tab, choose which server libraries to mirror. The background service syncs them into Kodi's library - the first sync can take a while, later ones are incremental.
- See [wiki](https://github.com/kontell/plugin.video.kofin/wiki) for further details.

### Server address

- The server address may be a bare host or IP (e.g. `192.168.1.10`), a `host:port`, or a full URL. `http` and port `8096` are assumed when the scheme and port are omitted. Use `https://` when connecting over the internet.
- On login the addon stores a Jellyfin access token (not your password) in Kodi's addon settings. Like all Kodi addon settings it is kept in plaintext under `userdata/addon_data/plugin.video.kofin/` - be aware of this when sharing Kodi backups or your addon_data folder. Logging out revokes the token on the server.

## Custom nodes and widgets

Kofin's listings are ordinary `plugin://` paths, so a library node, a skin widget or a favourite can point at any of them - including combinations the add-on does not ship, such as a single genre or one library's unwatched films.

See [docs/custom-nodes.md](docs/custom-nodes.md) for the path format, the list of keys, and a node file to copy.

## Companion server plugins

For improved syncing performance install the [KofinSyncQueue](https://github.com/kontell/jellyfin-plugin-kofinsyncqueue) server plugin. it gives the add-on a typed change feed so catch-up only touches what actually changed. Without it Kofin still works, using the official KodiSyncQueue plugin or real-time websocket updates.

For reliable syncplay install the [SyncPlay V2](https://github.com/kontell/jellyfin-plugin-syncplayv2) server plugin.

## Supported platforms

Kofin is pure Python and runs anywhere Kodi 21 /22 does. Because library sync writes Kodi's own database, it is gated to the schema versions it has been proven against; any other version is refused for writing (browsing and playback still work) until support is added.

| Kodi | Video database | Music database |
| --- | --- | --- |
| 21 "Omega" | MyVideos131 | MyMusic83 |
| 22 "Piers" | MyVideos147 | MyMusic84 |

## Uninstalling Kofin

Log out (Settings -> Account), run `Clean databases`, then uninstall the add-on and accept Kodi's offer to delete the add-on data.
