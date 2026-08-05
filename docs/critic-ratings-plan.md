# Critic ratings and the default-rating pointer

## What was there before

One rating row per movie, show and episode, typed with the literal string `default`, carrying Jellyfin's `CommunityRating`.
That is the fork's shape, transplanted unchanged: `add_rating_movie_obj` and friends passed `"default"` as `rating_type` and nothing ever wrote a second row.

Measured on a real install (kofin-test profile, MyVideos131, 1771 movies / 83 shows / 4502 episodes):

```
default|episode|4502|0.5 – 10.0 |votes: 0 non-null
default|movie |1771|2.545 – 9.7 |votes: 0 non-null
default|tvshow|  83|6.5 – 9.5   |votes: 0 non-null
```

Every item had exactly one row, every `movie.c05` pointed at it, and every `votes` was NULL — Jellyfin does not send `VoteCount`, whatever the object map asks for.
The three-decimal values (`6.538`, `7.222`) are TMDB `vote_average`; which provider backs `CommunityRating` is a per-library metadata-downloader decision on the server, invisible to a client.

## What the server actually offers

| Field | Movies | Series | Episodes | Scale |
|---|---|---|---|---|
| `CommunityRating` | 1763/1771 | 81/83 | 3829/4504 | 0–10 |
| `CriticRating` | 1581/1771 | 2/83 | 0/4504 | 0–100 |

`CriticRating` is the Rotten Tomatoes tomatometer, filled by the server's OMDb plugin (spot-checked: *3:10 to Yuma* 2007 → 89, which is RT's 89% and not Metacritic's 76).
Both fields are base DTO properties and arrive without being asked for, so this feature adds no `Fields` to any request and costs no bandwidth.
`Metascore` — requested by `downloader.info()` since the fork — is an Emby-era field Jellyfin never answers.

There is no per-user rating preference in Jellyfin (`UserConfiguration` carries audio/subtitle/view preferences and nothing else), so the choice has to be a kofin setting.

## The shape

Kodi's `rating` table is a set keyed by (media, type), and the item's c-column — `movie.c05`, the column `movie_view` LEFT JOINs the rating table on — names which member is *the* default: the one `ListItem.Rating` renders, stars are derived from, and rating sorts use.
So "support multiple ratings" and "choose the default" are two halves of one change: write every rating the server has, then point the c-column at the one the user asked for.

- `fields.ratings(obj)` builds the ordered `{type: (rating, votes)}` map: `default` (community) always, `critic` only when the server has one.
- Critic percentages are stored /10 — 78 becomes 7.8. Kodi's star meters and rating sorts assume 0–10, and a raw 78 sitting next to a community 7.1 breaks both the moment the user makes critic the default.
- The community row keeps the fork's `default` type name. Renaming it to something prettier would rewrite every row on every existing install for cosmetics, and Kodi itself types unnamed scraper ratings `default`.
- `kodidb.Movies.sync_ratings` upserts the set and returns the pointer: rows of a type the server no longer sends are deleted, and the pointer is rewritten on every pass, so a dropped rating can never leave `c05` at a deleted row (the LEFT JOIN would render the film unrated).
- Insertion order is the map's order, which keeps rating_id allocation deterministic for the idempotency dumps.
- Fallback: a film with no critic rating keeps its community one. Without it, turning the setting on would blank the rating on 190 of 1771 films here.

Movies only, and not by special-casing: the Series and Episode object maps have no `CriticRating` entry, so shows and episodes keep the single-row path they had, which is also what the data supports (2 shows, 0 episodes).
That is what the setting label promises — "Movies: prefer critic rating".

## Changing the default does not need a repair

Both rows are already local, so the flip is a pointer rewrite, not a resync: `settings_apply` enqueues `RepointRatings`, and `library.repoint_ratings` runs one `UPDATE movie SET c05 = COALESCE(<preferred>, <community>, c05)` per chunk of ids.
The COALESCE arms are the fallback rule in SQL.

Two things it deliberately does not do:

- It touches only kofin-owned `idMovie` values, read from kofin.db. Kodi's own scrapers write `default`-typed ratings too, and which of a scraped film's ratings is its default is not ours to move — the same discipline as the `kofin` name gate on generated node deletion.
- It fetches nothing. A film synced before this landed has no critic row yet and simply keeps its community rating until something re-syncs it; the setting's help text asks for a Repair to fetch them all at once. That one-time backfill is the only repair this feature ever needs.

## Widgets

`widgetstate` gained a `ratings` section — per item, the rating Kodi renders (the row the pointer names, not the whole set).
Without it the repoint pass would be invisible to home widgets: it moves no checksum and no userdata, so the fingerprint gate would suppress the refresh it asks for.
The rating rides along on the scan `_VIDEO_USERDATA` already makes over every row rather than paying for a second one, and a rating row nothing points at moving is deliberately not visible state.

## Tests

- L2 (`test_sync_writers.py`, all three schema legs): both rows written with the critic value rescaled, the pointer on the community row by default and on the critic row when the setting is on, the fallback when a film has no critic rating, the dropped-rating path (row deleted, pointer moved, never dangling), and the repoint pass moving pointers only — leaving rating rows and foreign movies alone.
- L1: `fields.ratings` scaling and ordering (`test_sync_fields.py`), the settings handler enqueueing `RepointRatings` both ways (`test_settings_apply.py`), the command's end-to-end apply and its no-op path (`test_sync_library.py`), and the fingerprint section moving on a pointer change while holding still for a non-default rating edit (`test_widgetstate.py`).
