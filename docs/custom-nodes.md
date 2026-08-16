# Custom nodes and widgets

Kofin's listings are ordinary `plugin://` paths, so you can point a library node, a skin widget or a favourite at any of them — including combinations the add-on does not ship, like a single genre or one library's unwatched films.

## The path

```
plugin://plugin.video.kofin/?mode=browse&view=<library id>&type=<type>&folder=<key>
```

Leave `view` out and the listing spans every library.

## Finding your library ids

The folder names under `userdata/library/video/kofin/` are the id with a prefix — `kofinmoviesf137a2dd…` is a movies library with id `f137a2dd…`.

Or ask Kodi for the add-on's root listing, which prints a ready-made path per library:

```sh
curl -s -u kodi:kodi -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"Files.GetDirectory","params":{"directory":"plugin://plugin.video.kofin/","media":"video"}}' \
  http://localhost:8080/jsonrpc
```

The same trick gives you genre ids: fetch a `folder=genres` path and each row's `file` is the finished `folder=genre-…` URL.

## Keys

| `type` | `folder` | gives you |
| --- | --- | --- |
| `movies`, `musicvideos` | `all` | everything |
| | `recent` | 25 newest |
| | `inprogress` | 25 resumable |
| | `unwatched` | everything unplayed |
| | `favorites` | server favourites |
| | `sets` | box sets (movies) |
| | `random` | 25 at random |
| | `genres` | the genre list |
| | `genre-<genre id>` | one genre |
| `tvshows` | `all` | every show |
| | `recentepisodes` | 25 newest episodes |
| | `nextup` | next up, 25 |
| | `inprogressepisodes` | 25 resumable episodes |
| | `favorites`, `random`, `genres`, `genre-<id>` | as above |
| `music` | `artists`, `albums`, `genres` | the full list |
| | `recentalbums` | 25 newest albums |
| | `favoritealbums` | server favourites |
| `playlists`, `boxsets`, `recordings` | `children` | the library's contents |

Three listings take no library at all:

```
plugin://plugin.video.kofin/?mode=continuewatching
plugin://plugin.video.kofin/?mode=nextepisodes&id=<library id>
plugin://plugin.video.kofin/?mode=extras&id=<series or season id>
```

And you can pin one item by putting its id in `folder`, with `type` naming what it is — `series`, `season`, `boxset`, `playlist`, `musicartist`, `musicalbum`:

```
plugin://plugin.video.kofin/?mode=browse&folder=<item id>&type=series
```

## Making the node

Save as `userdata/library/video/unwatched.xml` (music nodes go in `userdata/library/music/`):

```xml
<node type="folder">
	<label>Unwatched films</label>
	<path>plugin://plugin.video.kofin/?mode=browse&amp;view=f137a2dd21bbc1b99aa5c0f6bf02a805&amp;type=movies&amp;folder=unwatched</path>
</node>
```

`&` must be written `&amp;` — it is XML.

Reload the skin (or restart Kodi) and the node is live at `library://video/unwatched.xml/`, which is also what you point a skin's widget at.

## Examples

One genre:

```
plugin://plugin.video.kofin/?mode=browse&view=<movies library id>&type=movies&folder=genre-ce06903d834d2c3417e0889dd4049f3b
```

Newest films across every movie library:

```
plugin://plugin.video.kofin/?mode=browse&type=movies&folder=recent
```

Next up for one show library:

```
plugin://plugin.video.kofin/?mode=nextepisodes&id=<tvshows library id>
```

## Rules

- **Do not name the file `kofin…`.** Kofin deletes `kofin`-prefixed files and folders from its node tree when it regenerates. Any other name is left alone.
- **A new or edited node file needs a skin reload** before Kodi reads it. Until then the fetch fails with `GetDirectory - Error getting library://…` in the log and no add-on line above it.
- **You cannot change how many items come back.** `&limit=5` and the node's own `<limit>` are both ignored. The counts above are fixed, so prefer a capped listing for a widget — an uncapped one re-fetches the whole library every time the widget refreshes.
