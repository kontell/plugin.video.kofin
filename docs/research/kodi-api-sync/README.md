# Kodi API research evidence

Companion to the [feasibility report](../../kodi-api-sync-feasibility.md), researched on 3 October 2026.

## Contents

| File | Purpose |
|---|---|
| [observations.json](observations.json) | Sixteen groups of recorded probe results, including failures, timing samples and cleanup. Environment-specific home paths are replaced with placeholders. |
| [capabilities.json](capabilities.json) | Live application/API version, 68 relevant library/file/texture method definitions, upstream commit IDs and a comparison of method parameter names. Referenced schema types are not duplicated; this is an evidence snapshot, not a standalone JSON Schema bundle. |
| [provider/addon.xml](provider/addon.xml), [provider/default.py](provider/default.py) | The synthetic plugin used for native scanning. It has no Jellyfin connection and does not open Kodi databases. The final source includes routes/configuration added as the investigation progressed. |
| [inprocess_probe.py](inprocess_probe.py) | The actual in-Kodi metadata benchmark, userdata announcement capture and non-atomic batch probe. It operates only on tagged movies whose files belong to the synthetic provider. |
| [image_probe.py](image_probe.py) | The actual in-Kodi image-VFS experiment. It generates a tiny PNG in the research addon profile and records texture API readback. |

No credentials, target environment file, connection helper, private media catalogue or raw Kodi log is included. Read-only SQL research observations in the JSON are explicitly labelled. They establish results that Kodi does not expose through its API and must not become part of an official addon's implementation.

## How to reproduce the important results

These are research fixtures that create and change native library items. Use a test Kodi profile. The provider intentionally cannot play real media. The report distinguishes observations from design proposals and from behaviors that were not tested.

Install the `provider` directory as `plugin.video.kofin.apiresearch` through your development installation mechanism, enable it, and refresh Kodi's addon discovery if necessary. Its private profile is:

```text
special://profile/addon_data/plugin.video.kofin.apiresearch/
```

The provider reads an optional `config.json` there. It writes route invocations to `trace.jsonl`. Initial configuration:

```json
{
  "movies": 2,
  "music": 2,
  "music-modern": 2,
  "query_urls": false,
  "modern_mark_loaded": false
}
```

Use an existing authenticated JSON-RPC client or execute a script in Kodi with this helper:

```python
import json
import xbmc

def rpc(method, params=None):
    return json.loads(xbmc.executeJSONRPC(json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": method, "params": params or {}
    })))
```

The method/parameter/response triples for the original external probes are in `observations.json`. Do not reuse their recorded Kodi IDs. Discover IDs from your own run and verify the file belongs to the fixture before writing/removing anything.

### Video insertion, source layout and refresh

Bind and scan a movie directory:

```python
base = "plugin://plugin.video.kofin.apiresearch/"
rpc("VideoLibrary.SetSourceContent", {
    "path": base + "movies/", "content": "movies",
    "scraperid": "metadata.local", "scanrecursive": False,
    "refresh": False
})
rpc("VideoLibrary.Scan", {
    "directory": base + "movies/", "showdialogs": False
})
```

Wait for the scan to finish and verify the records; an immediate `OK` only establishes that the call was accepted. Read movies using the `kofin-api-research` tag and request `file`, `uniqueid`, `cast`, `streamdetails`, `art`, `playcount` and `resume`.

The initial provider uses path-only movie URLs and demonstrates the source-inheritance pitfall described in the report. For the successful query-path experiment, set `query_urls` to true and leave `movies` at 2. This produces movies numbered 1000 and 1001 with `movies/?id=...` URLs. Update `movie_plot` in configuration, call `VideoLibrary.RefreshMovie` with `ignorenfo: false`, and find the movie again by its owned file/custom ID. The old Kodi ID may be gone.

The TV fixture needs both `shows/` and `shows/show/` bound as `tvshows` with `metadata.local`; the show binding used `containssingleitem: true`. Scan `shows/`. The provider supplies a show, one episode, and named seasons 1 and 2. Only season 1 has an episode. The `musicvideos/` route is bound/scanned as `musicvideos`.

The provider's `refresh_info` implementation covers the movie routes used in the experiment. It is not a complete production refresh implementation for every media type. Its existence callback uses the `absent` configuration flag to return a synthetic answer; a real addon needs committed catalogue membership and offline handling.

### Native music and loaded tags

Scan `plugin://plugin.video.kofin.apiresearch/music/` with `AudioLibrary.Scan`. It returns modern music tags plus a legacy title `setInfo` call that marks them loaded. Query `AudioLibrary.GetSongs` for titles starting with `Kofin API research`.

Scan `music-modern/` with `modern_mark_loaded: false`. This initially supplied modern tags alone and imported no tracks. Set `modern_mark_loaded` to true and rescan. The provider adds a non-deprecated `size` field using `setInfo`, and two tracks imported on the tested build.

Initial song playcount/lastplayed did not follow the supplied tags. Apply them with `AudioLibrary.SetSongDetails` and confirm readback. Change `music` from 2 to 1 and rescan its directory: the omitted track should disappear, and the remaining track can retain its state/identity. Finally, scanning with each music count at 0 exercises scoped removal and native orphan cleanup.

### In-process benchmark and announcements

Set `movies` to 100 with `query_urls: false`, then scan the movie directory and verify that exactly 100 owned tagged movies exist. The original experiment grew the initial two movies to 100. Keep unrelated synthetic movie generations out of this benchmark: the script uses the number of matching movies it actually finds.

Copy `inprocess_probe.py` into the research addon profile and invoke:

```text
RunScript(special://profile/addon_data/plugin.video.kofin.apiresearch/inprocess_probe.py)
```

For example, the adjacent driver's builtin tool accepts the command with its target selected through its normal environment:

```sh
KODI_TARGET=P1D ../kodi-drive/bin/kodi-builtin 'RunScript(special://profile/addon_data/plugin.video.kofin.apiresearch/inprocess_probe.py)'
```

The script requires at least two matching movies. It writes `inprocess-result.json` in the same profile. It changes a movie's playcount/resume, captures notifications for owned movie IDs, performs three samples each of individual/25-item/100-item plot update calls, and runs a success/error/success batch. The saved original observations include all timing samples and readback showing both successful batch changes persisted.

### Image VFS

Copy `image_probe.py` to the same profile and run it with `RunScript`. It writes `image-result.json`. Compare `Textures.GetTextures` before/after, and the returned byte count. The original result was an empty match set before the read and one 2×2 cached texture afterward. This probes ordinary caching, not arbitrary chapter-image alias injection.

### Native NFO versions and extras

The original asset fixture was created beneath:

```text
special://profile/addon_data/plugin.video.kofin.apiresearch/fixtures/video/
  NfoVersions/
    Extended.strm
    Extended.nfo
    Theatrical.strm
    Theatrical.nfo
    Extras/
      Research bonus.strm
```

The STRM content is a synthetic URL such as:

```text
plugin://plugin.video.kofin.apiresearch/nfo/?version=Theatrical
```

The matching NFO uses:

```xml
<movie>
  <title>Kofin API research NFO versions</title>
  <uniqueid type="kofinresearch" default="true">kofin-api-research-nfo-versions</uniqueid>
  <plot>Synthetic local NFO</plot>
  <hasvideoversions>true</hasvideoversions>
  <isdefaultvideoversion>true</isdefaultvideoversion>
  <videoassettitle>Research Theatrical</videoassettitle>
  <runtime>2</runtime>
</movie>
```

The Extended NFO has the same unique ID, `isdefaultvideoversion: false`, and asset title `Research Extended`. The bonus STRM contains `plugin://plugin.video.kofin.apiresearch/nfo/?extra=bonus`.

Bind the `fixtures/video/` source as movies with `metadata.local`, `scanrecursive: true`, `usedirectorynames: true`. The original test recorded `videolibrary.ignorevideoextras`, temporarily set it false, scanned the fixture, then restored it in a `finally` block. It was true both before and after the test. Keep that restoration if reproducing the temporary preference change.

`GetMovies` returned one movie with `Theatrical.strm` as its default file. The native asset menu or a separate research-only read-only inspection establishes that it has two versions and an extra; the ordinary movie getter does not enumerate their full native records. The saved observation includes that labelled audit. No SQL insertion was involved.

## Cleanup procedure used

1. Set the `music` and `music-modern` listing counts to 0; scan each owned directory and wait for completion.
2. Query synthetic video records by title/tag and verify that their `file` starts with the fixture plugin namespace or the owned NFO fixture directory.
3. Remove matching movies/shows/music videos through the corresponding `VideoLibrary.Remove*` methods. Never copy IDs from another run.
4. Clear the owned source bindings with `SetSourceContent(content="none", clearmode="remove")`. Include the extra root/show bindings used during the source-layout probes.
5. Find the `texture-probe.png` cache entry through `Textures.GetTextures`, verify its URL contains the fixture namespace, and call `Textures.RemoveTexture`.
6. Confirm no synthetic songs/albums/artists or video items remain and confirm the extras preference is restored.
7. Disable the fixture addon, remove its installed directory and own profile, and refresh addon discovery. Remove any research bootstrap scripts from their staging location.

The recorded cleanup did not call global `AudioLibrary.Clean`, delete Kodi database files, or issue SQL writes. Native internal housekeeping was left to Kodi. The supporting source remains in this repository as research material; it is not wired into Kofin's runtime.
