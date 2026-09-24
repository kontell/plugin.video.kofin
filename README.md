[![License: GPL-3.0-only](https://img.shields.io/badge/License-GPL%20v3-blue.svg)](/tmp/.mount_JoplindJEoiY/resources/app.asar/LICENSE "LICENSE")

# Kofin for Jellyfin

Kodi video & music addon for Jellyfin. Browse Jellyfin libraries through the add-on or sync selected libraries directly into Kodi's own databases, so Jellyfin content appears as a Kodi library.

A thin plugin over a background service that syncs, listens on the server's websocket and drives playback. Library writes (optional) go straight into Kodi's own databases and are verified against every supported Kodi version by the add-on's test suite.

For Jellyfin Live TV, see the companion [Kofin PVR](https://github.com/kontell/pvr.kofin) addon.

Requires: Kodi 21 "Omega" or Kodi 22 "Piers". Jellyfin 10.11 or later.

## Features

- Hands-off, resumable & robust [sync](docs/benchmark-report.md)
- Downloads (including transcoding)
- SyncPlay, watch in sync with other Jellyfin clients
- Integrated media segment skipping and play next episode prompt
- Access movie special features/ extras
- Multi-version movies as Kodi video versions
- Flexible playback methods, direct play, remux or transcode. Choose supported HDR formats & max resolution
- Jellyfin chapter images in Kodi's chapter list
- Sync playlists
- Transcode music
- Jellyfin song lyrics via companion add-on
- Edit Jellyfin user audio and subtitle preferences
- Who's watching? - toggle additional users onto the session for multi-user watch state (persistent after restart)

## Installation

Install via the [Kontell Repository](https://github.com/kontell/repository.kontell).

### Configuration

- After install the addon appears under Add-ons -> Video add-ons -> Kofin.
- From settings enter your server address and log in.
- Libraries can now be browsed in a similar manner to typical Kodi add-ons.
- To sync libraries into the local Kodi database:
    - From settings go to the Library tab, choose which server libraries to mirror and the background service syncs them into Kodi's library.
- See [wiki](https://github.com/kontell/plugin.video.kofin/wiki) for further details.

### Migration from Jellyfin-Kodi

1.  Disable or uninstall jellyfin-kodi, its background service must not be running.
2.  Install Kofin and enter Add-ons -> Video add-ons -> Kofin -> Settings -> Account
3.  Run `Clean databases` (the button is only visible while logged out).
    - It removes all jellyfin-kodi and Kofin library data, nodes and playlists, and optionally the music library, cached server artwork and custom library nodes.
    - Cleaning is per Kodi profile, run it in each profile that synced.

## Companion server plugins

- For improved syncing performance install the [KofinSyncQueue](https://github.com/kontell/plugin.video.kofin/wiki/Companion-server-plugins) server plugin. it gives the add-on a typed change feed so catch-up only touches what actually changed. Without it Kofin still works, using the official KodiSyncQueue plugin or real-time websocket updates.

- For reliable syncplay install the [SyncPlay V2](https://github.com/kontell/plugin.video.kofin/wiki/Companion-server-plugins) server plugin.

## Supported platforms

Kofin is pure Python and runs anywhere Kodi 21 /22 does. Because library sync writes Kodi's own database, it is gated to the schema versions it has been proven against; any other version is refused for writing (browsing and playback still work) until support is added.

| Kodi | Video database | Music database |
| --- | --- | --- |
| 21 "Omega" | MyVideos131 | MyMusic83 |
| 22 "Piers" | MyVideos149 | MyMusic84 |

## Uninstalling Kofin

Log out (Settings -> Account), run `Clean databases`, then uninstall the add-on and accept Kodi's offer to delete the add-on data.
