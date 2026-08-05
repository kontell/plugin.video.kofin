# Clean databases: the migration cleaner — plan

A "Clean databases" button in the Account settings tab, visible only when logged out, that wipes every trace of plugin.video.kofin *and* jellyfin-kodi from Kodi's databases, library nodes and playlists. Two user stories: migrating from jellyfin-kodi (run it after disabling jellyfin-kodi, before first kofin login — especially for users who already uninstalled jellyfin-kodi and so cannot use its own reset), and removing kofin (log out, clean, then uninstall). Kodi's uninstall flow only offers to delete addon_data; synced library rows, nodes, playlists and `Database/jellyfin.db` all survive it, and Kodi's own Clean library cannot remove plugin-path rows.

The design is grounded in a live audit of jellyfin-kodi's own "Reset local Kodi database" (the only shipped precedent for this operation), run on this box on 2026-08-05: Kodi 21.3 Omega, jellyfin-kodi 1.6.4, profile `jellyfin-kodi`, fixture of 1771 movies / 83 shows / 4502 episodes / 20770 songs / 29664 `jellyfin.db` mappings, addon-path mode. Method: sqlite `.backup` + tar snapshot of the profile, drive `plugin://plugin.video.jellyfin/?mode=reset` to completion (video+music wipe, artwork No, settings-delete No, RestartApp), audit every table and file while Kodi was down, restore the snapshot. Evidence (before/after per-table counts, file diffs, snapshot): `tests/live/results/jellyfin-reset-gap/` (gitignored).

## What jellyfin-kodi's reset does (verified live)

`jellyfin_kodi/database/__init__.py:204` `reset()`: confirm dialog → wait for sync idle → `reset_kodi()` (DELETE FROM every MyVideos table except `version` and `videoversiontype`; same for MyMusic except `version`, but only when `enableMusic` is set or a second dialog is answered Yes) → `reset_jellyfin()` (DROP all jellyfin.db tables) → delete `playlists/video/jellyfin*.xsp` → delete `library/video/jellyfin*` files and dirs → optional full-texture wipe → optional own-settings delete → delete its sync.json → `RestartApp`.

It worked as designed: all video and music content rows gone, all `jellyfin*`-prefixed nodes and playlists gone. The gaps are in what "as designed" leaves behind.

## Gaps confirmed by the audit

**G1 — the standard node tree survives.** 35 files under `library/video/` remained: `addons.xml`, `files.xml`, `playlists.xml` and the full `movies/` (12), `tvshows/` (9), `musicvideos/` (10) node dirs, including a customised `movies/index.xml` (`order="17"`). Deletion by `jellyfin` prefix cannot claim them — and neither can ours: the identical tree exists in the master profile and in `kofin-test`, written variously by profile-creation copy, plugin.library.node.editor and skin tooling. They are shared property of unknowable ownership. Consequence: pattern-owned deletion by default, plus an explicit everything-goes toggle (below).

**G2 — the music wipe lands *below* pristine.** After reset, MyMusic83 held only `version`; a pristine Omega MyMusic83 is seeded at creation with one `artist` row (`[Missing Tag]`, idArtist 1) and one `role` row (`Artist`, idRole 1) — see `tests/fixtures/mymusic83_seed.sql`. jellyfin-kodi's blanket DELETE removes Kodi's own seed rows. Kofin's writers are proven equivalent only against pristine databases, so a migrating user who ran jellyfin-kodi's reset hands kofin a database shape the L2 suite has never blessed. Consequence: our wipe must *restore to pristine* (re-insert seeds), and must repair an already-sub-pristine DB, not just a full one.

**G3 — `jellyfin.db` survives as a file.** `reset_jellyfin()` drops the tables but leaves the zero-table file plus `-wal`/`-shm` litter in `Database/`, which Kodi's addon uninstall never touches (it only offers addon_data). Consequence: the cleaner deletes `Database/jellyfin.db{,-wal,-shm}` outright.

**G4 — textures are all-or-nothing.** On the answer-No path (taken here) every cached server image survives: 76 of 216 `Textures13` rows were `http://…/Items/<id>/Images/…` art. The answer-Yes path (`reset_artwork()`) wipes the *entire* cache including the 140 innocent addon/skin rows. Consequence: our optional texture step purges only rows matching `http(s)://%/Items/%` (both addons' art shares that URL shape) plus their cached files.

**G5 — the music wipe is conditional.** `reset_kodi()` touches MyMusic only `if settings("enableMusic.bool") or dialog(...)`; a user who synced music once and later disabled it can answer one dialog wrong and keep 20k orphaned songs forever. Consequence: kofin always offers the music wipe explicitly, with the default derived from detection (`strPath LIKE 'plugin://plugin.video.kofin/%'` or `http(s)://%/Audio/%` rows present — the same family `prune_orphan_paths` already matches in `sync/kodidb/queries_music.py:365`).

**G6 — not a gap, recorded so nobody "fixes" it:** the 387 surviving `videoversiontype` rows are Omega's creation-time seeds (`tests/fixtures/myvideos131_seed.sql` inserts exactly 387; the fixture had zero `owner=1` rows). Keeping that table is correct; our invariant below keeps it correct even when user-defined version types exist.

**Meta-observation.** jellyfin-kodi's reset is a plugin *directory entry*, and during shutdown Kodi's window refresh re-invoked `?mode=reset` twice (`GetDirectory` retries observed in the log); a stray remote press while the confirm dialog is up can also re-launch it from the still-focused list item. Kofin's cleaner is a settings-dialog RunPlugin button — never a directory path — which is immune to both, and is also what the modal constraint in CLAUDE.md already demands.

## Cleanup inventory

- MyVideos (always) and MyMusic (offered, detection-defaulted): wipe to pristine as defined below. Watched states and resume points go with the rows; the server's copy is untouched and a later kofin sync restores kofin-relevant state.
- Kofin state: delete `addon_data/plugin.video.kofin/kofin.db` and `sync.json`; clear `viewsHash`, `lastIncrementalSync`, `librarySelection`, `syncedLibraries` settings. Deleting kofin.db with the DBs is mandatory, not hygiene: full sync skips items whose stored checksum matches (`sync/full_sync.py:76`), so wiped Kodi DBs plus a surviving kofin.db is an empty library that reports "synced" — the exact trap manual file deletion sets for users today.
- jellyfin-kodi state: delete `Database/jellyfin.db{,-wal,-shm}`. Its addon_data stays (Kodi's uninstall prompt owns that, per the feature's scope line).
- Nodes, default sweep: kofin's own tree via `views.delete_nodes()` (`sync/views.py:1227`, works server-less via `Views(server=None)`), legacy flat-layout `kofinmedia<id>/` dirs and root `kofin_*.xml` (the `migrate_flat_nodes` patterns — the master profile on this box still carries such debris), and jellyfin-kodi's patterns mirrored from its own `delete_nodes` (`jellyfin_kodi/views.py:1043`): root `jellyfin*` files and dirs.
- Playlists: `playlists/video/kofin*.xsp` via `views.delete_playlists()`, the managed music folder via `playlists.cleanup_managed_playlists()` — plus delete the `playlists/music/Kofin/` folder itself — and `playlists/video/jellyfin*.xsp`.
- User-nodes toggle (required): a separate opt-in step that deletes the *entire* `special://profile/library/video/` (and `library/music/` when present) tree, reverting Kodi to its shipped default nodes. This is the only sound answer to G1's ownership ambiguity, and it removes hand-made and node-editor files by design — the dialog text and the button's help string must both say so plainly. Default No.
- Textures (offered): targeted `/Items/` purge per G4, rows plus cached files, through the schema-gated texture machinery (`sync/kodidb/texture.py`).
- Stays behind, documented in README: favourites.xml entries pointing at dead plugin paths (cosmetic, Kodi-owned), skin home-widget assignments (re-pick after cleaning), `sources.xml`/`passwords.xml` additions from jellyfin-kodi *direct-path* installs (hand-edited files we must not touch; the audit fixture was addon-path mode, direct-path rows are also why row-level differentiation is unsound), other profiles (the cleaner is profile-scoped; run it in each profile that synced), dormant older-version DB files (e.g. a stale `MyVideos121.db`) invisible to the running Kodi.

## Wipe semantics: restore to pristine, proven by fixtures

The invariant, enforced in the L2 suite: cleaning any gated database — freshly created, fully written by the L2 full-fidelity build, or artificially damaged below pristine — yields a dump byte-identical to the pristine fixture dump for that schema version. Implementation sketch that should satisfy it: DELETE FROM every `sqlite_master` table except `version`; restore seed tables from per-version keyed constants in `sync/schema.py` (the `EXTRA_ITEM_TYPE` pattern) — MyMusic re-inserts the seed `artist` and `role` rows; MyVideos restores `videoversiontype` to its seed set (delete `owner != 0` rows suffices if seeds are never mutated — the dump comparison decides); clear `sqlite_sequence`. Music comparisons go through `music_dump()` for the `DATETIME('now')` columns, as ever. No VACUUM — file size is cosmetic and the restart makes it moot.

The wipe opens databases through `Database('video')`/`Database('music')`, so the schema gate applies: an unsupported Kodi version refuses before anything is written, consistent with the addon everywhere else.

## UX and control flow

Settings: a plain string button `cleanDatabases` in the Account category, `<dependency type="visible">` on `isLoggedIn` **false** — the exact inverse of `logoutButton` (`resources/settings.xml:102`). It must stay a plain button: the Omega list[string] dependency bug does not apply, but keep it away from list settings regardless. New string ids need a full Kodi restart to render during dev.

Route: `?mode=cleandatabases` in `router.py` → new `plugin/clean.py`. Dialogs are fine in the plugin process (logout already shows a yesno, `plugin/account.py:154`); this is not a library-node route, so the modal constraint is satisfied.

Flow: guard checks → scope confirm ("removes EVERYTHING from Kodi's video library — including items other addons or local sources added — and their watched states") → music yesno (G5 default) → user-nodes toggle yesno (default No) → textures yesno → `DialogProgress` through the steps → summary OK → `RestartApp()`. RestartApp relaunches via the `/usr/bin/kodi` wrapper on desktop Linux (exit 65) and plain-quits on platforms without a respawn loop — same behaviour users already know from jellyfin-kodi's reset; the closing dialog says "Kodi will now restart".

Guards: re-check `isLoggedIn` (the visibility dependency is UI-only); refuse with a pointed message when `plugin.video.jellyfin` is installed *and enabled* (`xbmc.getCondVisibility("System.AddonIsEnabled(plugin.video.jellyfin)")`) — its live service would resync or fight the wipe mid-write, and README's migration order already says disable first; belt-and-braces check that no kofin sync is running (logged-out means the service is idle by construction).

## Tests

- L2 (`tests/unit/test_clean.py`, parameterized over every gated schema leg like `test_sync_writers.py`): clean(pristine) is a byte-identical no-op; clean(full-fidelity build) == pristine dump; clean(sub-pristine music — seeds deleted first, simulating post-jellyfin-reset damage) == pristine dump.
- Units against `tests/unit/fakes.py`: node/playlist deletion matrix — `kofin*`, legacy flat `kofinmedia<id>`, `jellyfin*` files/dirs die; hand-made files survive the default sweep and die only under the toggle; the standard-tree files (`movies/index.xml` et al.) survive the default sweep. jellyfin.db file trio deletion. Settings clears. Guard refusals (logged in; jellyfin-kodi enabled).
- Texture purge: row selection by `/Items/` pattern plus cached-file unlink, against the existing texture fixtures; must not touch non-`/Items/` rows.
- Live scenario (append to `docs/testing-plan.md`): on the `jellyfin-kodi` profile fixture — disable jellyfin-kodi, run the cleaner, assert only the documented survivors remain, Kodi restarts clean, kofin login + full sync succeeds into the wiped DBs. The snapshot/restore harness from the audit (`tests/live/results/jellyfin-reset-gap/backup/`) makes the fixture reusable.

## README

Migration section becomes: 1. Disable or uninstall jellyfin-kodi. 2. Install Kofin → Settings → Account → Clean databases (removes all jellyfin-kodi and Kofin library data, nodes and playlists; optionally music, textures and custom nodes). 3. Sign in and select libraries. Keep the old jellyfin-kodi-native steps as the alternative for users who prefer resetting before uninstalling, with a note that its reset leaves the node tree and music seed rows for our cleaner to repair. Add the uninstall mirror: before uninstalling Kofin — sign out, Clean databases, uninstall, accept Kodi's addon-data delete. Note the deliberate leftovers (favourites, widget re-picks, direct-path sources/passwords entries, per-profile scope).

## Implementation notes (2026-08-05)

The sweeps live standalone in `sync/clean.py` rather than calling `views.delete_nodes`/`migrate_flat_nodes`/`delete_playlists`: one prefix-gated, root-parameterized pass covers all three (the `kofin`/`jellyfin` prefixes subsume the NODE_ROOT tree, the legacy flat layout and the favourites files), avoids `Views()`'s sync.json read entirely, and unit-tests against plain temp dirs. The managed music playlist folder is deleted whole (`playlists.FOLDER_NAME`) instead of via `cleanup_managed_playlists`, which only prunes *within* the folder.

`syncStatus` joined the cleared settings — a stale "N synced" status line would otherwise survive the wipe.

The music seed constants live in `schema.MUSIC_SEED_SQL` and `test_sync_schema` refuses a SUPPORTED music version without them, as planned. `wipe_music` reads the version off the database's own `version` row rather than re-running discovery, so the L2 suite exercises it through path overrides exactly like the writers, and a version without stated seeds fails loudly before any deletion.

## Non-goals

Differentiating kofin rows from jellyfin-kodi rows (full wipe by design: direct-path installs are indistinguishable from native libraries, and both addons' music rows share the `/Audio/` URL shape); favourites/sources/passwords edits; multi-profile sweeps; VACUUM; preserving native-library content through a clean (the scope dialog owns that warning).
