# plugin.video.kofin — Phase 3 implementation plan

Date: 2026-07-17. Operationalizes build phase 3 from `rewrite-research.md` §11 — the **player features**: media-segment skipping, Up Next coordination, and extras (native for movies, browseable for TV). The report governs design (§8.2, §8.3); the fork (`ref/jellyfin-kodi`, branch `combined/syncplay-sync`) is the source for the segment/skip code, which ports; extras are new code the report specifies. Test gates reference `testing-plan.md` (S3.x). Phases 1 and 2 are complete, merged to `main`, and live-verified (S1.x, S2.1–S2.5/S2.8/S2.9 pass on the `kofin-test` profile).

**Phase 3 deliverable**: while an episode plays, kofin fetches its Jellyfin media segments and skips or offers to skip Intro/Credits/Recap/Preview/Commercial per per-type settings; when Up Next is installed it hands the credits moment to Up Next instead of stacking a second popup; movies with special features get native Kodi **Extras** entries in the library, and shows with specials get a browseable Extras node. Everything is service/plugin-side — no writer changes to the core sync tables except the movie extras asset rows (which reuse the `videoversion` machinery phase 2 already drives).

---

## 1. Scope

**In**: segment fetch + per-type skip engine (`service/segments.py` checker + player `check_skip_segments`, ported); the skip-button overlay (custom skin window — the *only* custom window in kofin, ported from the fork's `dialogs/skip.py` + `script-jellyfin-skip.xml`); Up Next data send with `notification_time` = credits-segment start, and the suppression rule that lets Up Next own the credits moment; movie extras synced as native Kodi video assets (`videoversion` rows, `itemType = EXTRA`, per-schema constant), incremental via the same added/updated events; a TV-extras browse node + "Browse extras" context item; the Media Segments settings group; playback of extra items through the existing `play` handler; S3.x live gates.

**Out (later phases)**: SyncPlay (`syncplay/` package — phase 4); KofinSyncQueue and tier-1 (phase 5); plugin-free fallback (phase 6); Kodi "versions" sync of Jellyfin MediaSources (YAGNI §9 — extras exercise the asset tables, native versions UI is a clean later addition); any vendored/forked Up Next popup (§9 — the data+play_info signal API is the sanctioned integration point); theme media.

## 2. Decisions locked in phase 3

* **Segment skip = ported behavior, coordination = new.** The fork's segment checker, `check_skip_segments`, `_process_segment` (EOF clamping), the per-type mode map, and the `SkipDialog` overlay port with mechanical adaptation (imports, settings ids, addon id, skin path). What changes is the Up Next interaction (§8.2): the report's "one popup at a time" rule replaces the fork's stacking behavior. No restyling of the skip SQL/segment math.
* **One popup at a time (report §8.2).** On episode playback start, fetch segments and next-episode info, and send `upnext_data` immediately **with `notification_time` = the Credits/Outro segment start** when one exists (service.upnext supports `notification_time`/`notification_offset`). Suppression: if a next episode exists *and* Up Next is installed+enabled (`System.HasAddon(service.upnext)` + our toggle), the **Credits segment does not raise our skip button** — Up Next owns that moment. Intro/Recap/Preview/Commercial behave normally. Fallbacks: no Up Next, a movie, or the season's last episode → our credits button shows; inside a SyncPlay group → neither (phase 4 concern; guarded now so the hook exists).
* **Event-driven skip button, not a busy-loop.** The fork's `_monitor_skip_dialog` polls in a loop that blocks the checker thread. kofin drives the overlay's lifetime from the 1 Hz segment tick (open at segment start, auto-close past `end`, close on OK/skip) — no second monitor thread, no busy-wait. The checker thread stays decoupled from the player (report's "keep the fork's decoupled segment checker").
* **Up Next wire format via the existing primitive.** `core/ipc.py` already has `encode_hex` and the hexlified-array scheme; the Up Next `upnext_data` signal (`sender=<id>.upnext_data`, reply on `upnextprovider.<id>_play_action`) is produced with it — no AddonSignals dependency. The play-action reply routes back through the service's `onNotification`, resolving the next episode's kofin id and starting playback through the normal `play` path (reports `PlaySessionId` etc. unchanged).
* **Movie extras are native, reusing phase-2 asset machinery.** Jellyfin `SpecialFeatureCount > 0` → fetch `/Users/{u}/Items/{id}/SpecialFeatures` → one `files` row + one `videoversion` row per feature (`media_type='movie'`, `idMedia = idMovie`, `itemType = EXTRA`, `idType` = a named `videoversiontype` mapped from Jellyfin `ExtraType`), with a `plugin://plugin.video.kofin/…mode=play&id=<extraId>` path. The `videoversion` add/delete/idempotency already works (phase 2 writes the main version and the L2 suite covers it); extras are additional rows on the same tables. **Schema constant: EXTRA = 1 on Omega**; Piers (EXTRA = 2) stays refused by the schema gate until its fixtures land, so only the Omega constant is wired now — but `schema.py` carries the per-version value so Piers is a data change, not a code change.
* **TV extras are a plugin browse** (Kodi has no native TV extras): a dynamic Extras node for series/seasons whose DTO reports `SpecialFeatureCount > 0`, plus a context item on library shows, both routing to a `mode=extras` listing over the same `SpecialFeatures` endpoint. No DB writes for TV extras — they are a live listing, like the phase-1 browse nodes.
* **Extras never gate the core sync.** A movie whose SpecialFeatures fetch fails still syncs (the extra request is best-effort, one per movie that advertises `SpecialFeatureCount > 0`, incremental via the same added/updated events). Degrade-not-die, as phase 2.
* **Trailers unchanged.** The existing local-trailer / YouTube handling (phase 2 `trailer()` in the movie/tvshow writers) stays; extras are a separate asset path.
* **Strings**: 30450–30499 Media Segments settings + skip UI; 30500–30549 extras (node labels, context items).

## 3. Port map (fork `jellyfin_kodi/` → kofin)

| Source (fork) | Target | ~Lines | Adaptation notes |
|---|---|---|---|
| `segments.py` | `service/segments.py` | 45 | `SegmentChecker` thread verbatim; `settings()` → kofin settings, `window`/exit checks → kofin state; 1 Hz `waitForAbort` loop kept |
| `player.py` segment methods (`check_skip_segments`, `_process_segment`, `_get_segment_skip_mode`, `_handle_skip_segment`, segment-type map) | `service/player.py` (additions) | 250 | EOF clamping and per-type mode logic verbatim; `_monitor_skip_dialog` busy-loop replaced by tick-driven open/close; Up Next suppression rule rewritten per §8.2 |
| `player.py` `next_up()` + `upnext_data` send | `service/player.py` (`next_up`) | 90 | `event(…, hexlify=True)` → `ipc.encode_hex`; add `notification_time` = credits start; next-episode fetch via `Api` |
| `dialogs/skip.py` (`SkipDialog`) | `plugin/skip.py` | 90 | `WindowXMLDialog` port; addon id, skin filename, string ids |
| `resources/skins/default/1080i/script-jellyfin-skip.xml` | `resources/skins/default/1080i/script-kofin-skip.xml` | 60 | rename; native Estuary-ish styling, stock textures only (no addon-branded art, per phase-2 §2) |
| `jellyfin/api.py` `MediaSegments/{id}`, `SpecialFeatures` | `core/api.py` (`media_segments`, `special_features`) | 25 | typed additions to the phase-2 `Api`; user-scoped SpecialFeatures |

New code (report §8.3, no fork source): extras writer additions (`sync/writers/movies.py` extras pass + `sync/kodidb/movies.py` asset helpers, ~150), `ExtraType → videoversiontype` map + `EXTRA` schema constant (`sync/schema.py`, ~30), TV-extras browse (`plugin/browse.py` `mode=extras` + context item, ~120), extras play-path handling in `plugin/play.py` (~20), settings XML/strings (~250), fixtures + tests (~400).

## 4. Settings spec

### Media Segments group (Playback tab, `category` TBD, 30450+)

The Playback tab itself is a phase-3 addition (phase 2 shipped Account/Transcoding/Library/Sync). Minimum for segments:

| id | type/control | notes |
|---|---|---|
| `mediaSegmentsEnabled` | bool, default **true** | master toggle; gates the checker thread and the API fetch |
| `skipIntroductionMode` | spinner Off/Auto/Ask, default **Auto** | 0/1/2; Auto seeks past, Ask shows the skip button |
| `skipCreditsMode` | spinner Off/Auto/Ask, default **Ask** | Credits defers to Up Next when applicable (see below) |
| `skipRecapMode` | spinner Off/Auto/Ask, default **Ask** | |
| `skipPreviewMode` | spinner Off/Auto/Ask, default **Off** | |
| `skipCommercialMode` | spinner Off/Auto/Ask, default **Auto** | |
| `upnextCoordination` | bool, default **true** | when on + `service.upnext` present, Up Next owns the credits moment; when off, our credits button behaves per `skipCreditsMode` |

Extras: no user setting in v1 (always synced for movies, always browseable for TV). An `enableExtras` bool can be added if the SpecialFeatures request cost proves noticeable on very large movie libraries — defer until measured (§7).

The report (§7 line 232) also lists resume/`markPlayed`/offer-delete/cinema-mode groups for the Playback tab; those are Playback-tab scope but not phase-3 *player-feature* work — land the tab shell here, fill the non-segment groups opportunistically (they are settings + small handlers, no new subsystems).

## 5. Work breakdown (ordered; each step lands green on L0/L1/L2)

1. **Api + Playback tab shell** (S): `Api.media_segments(item_id)` and `Api.special_features(item_id)`; the Playback settings category with the Media Segments group + strings. *DoD*: L0 green; settings render (S1.1-style screenshot); `media_segments` returns the typed shape against a segments-capable test item.
2. **Segment engine transplant** (L): `service/segments.py` checker + player segment methods + `plugin/skip.py` + the skip skin xml. Wire the checker to start/stop with playback (phase-1 player lifecycle). *DoD*: L1 on `_process_segment` (EOF clamp: a segment ending past runtime never seeks past it; already-prompted de-dup; mode 0 skips nothing); S3.1 (auto-skip) and S3.2 (button) live on the test show.
3. **Up Next coordination** (M): `next_up()` sends `upnext_data` with `notification_time` = credits start; suppression rule (credits button withheld when Up Next present + enabled + next episode exists); play-action reply → resolve next kofin id → play. *DoD*: L1 on the suppression predicate (matrix: upnext present/absent × next-episode yes/no × movie/episode × group/no-group → button vs Up Next vs neither); S3.3 live with `service.upnext` installed.
4. **Movie extras (native)** (M): `ExtraType → videoversiontype` map + `EXTRA` constant in `schema.py`; writer pass that, on `SpecialFeatureCount > 0`, fetches SpecialFeatures and writes one asset (`files` + `videoversion`) per feature with a plugin play path; removal cleans the asset rows (extend the movie `remove` cascade); `play` handles extra ids. *DoD*: L2 fixture — a movie with two special features writes two `videoversion` rows with `itemType = 1` (Omega) and sensible `videoversiontype` names; idempotency (second write byte-identical); removal leaves zero orphan `videoversion`/`files` rows. S3.4 live (Extras button on the info dialog; an extra plays + reports).
5. **TV extras (browse)** (S): `mode=extras` listing over SpecialFeatures for a series/season; an Extras node in the tvshows node menu when the view has any specials; "Browse extras" context item on library shows (addon.xml visibility extended to `DBTYPE=tvshow/season`). *DoD*: L1 on the listing builder; S3.5 live.
6. **Live gates** (M): S3.1–S3.5 on the `kofin-test` profile against a segments-capable show and the two-extras movie in the test set; screenshots for the skip button and the Up Next popup timing; masking grep clean over a segment+extras playback session.

Steps 1→2→3 are sequential (each builds on the last); 4 and 5 can overlap with 3 (independent code paths); 6 gates everything.

## 6. Test gates (from `testing-plan.md` §S3)

* **S3.1 segment auto-skip**: seek to shortly before a known intro; position jumps past the segment end within the poll interval; notification shown; an EOF-clamped segment never seeks past runtime.
* **S3.2 skip button**: button mode shows the overlay at segment start (screenshot), OK skips, ignoring it disappears at segment end; never two overlays at once.
* **S3.3 Up Next coordination**: with `service.upnext` installed, an episode with a credits segment and a next episode shows **only** the Up Next popup, exactly at credits start; accepting plays the next episode through kofin; without Up Next (or on the season's last episode) our credits button appears instead; inside a SyncPlay group, neither.
* **S3.4 movie extras**: after sync, the two-extras movie has `videoversion` rows (itemType per schema) with sensible names; the info dialog shows the Extras button; an extra plays and reports.
* **S3.5 TV extras**: the show with specials exposes the Extras browse node; entries play.

The test set needs one **segments-capable show** (Intro Skipper-analyzed or chapter-based) and one **two-extras movie** — both are already called out in `testing-plan.md` §1 as required test media; coordinate with Conor to add them to the test libraries before S3.x (the current live set — Movies/Documentaries/Music-Alt — has neither guaranteed).

## 7. Risks / watch items

* **service.upnext timing contract**: the whole §8.2 design hinges on `notification_time` making Up Next fire at credits start. Verified in the report against `service.upnext/resources/lib/api.py:186-193`, but the addon is external and versioned — S3.3 must confirm the popup lands at the segment, not at the fork's old 2%-progress default. If a version ignores `notification_time`, fall back to sending `upnext_data` at the credits segment tick instead of at playback start.
* **Custom skin window portability**: `script-kofin-skip.xml` is the only custom window; some skins/resolutions have historically fought `WindowXMLDialog` overlays. Keep it minimal (a label + two buttons, stock textures), and test under the box's actual skin (contuary) as well as Estuary — a skip button that renders under Estuary but not the daily skin is a real failure.
* **Extras request cost**: one SpecialFeatures fetch per movie advertising specials. On the 1758-movie library that is bounded by how many actually have extras (likely a small fraction), but measure on the first full sync with extras enabled before deciding whether `enableExtras` needs to exist (§4). Incremental syncs pay it only for changed items.
* **EXTRA constant drift on Piers**: `itemType` for EXTRA renumbers 1→2 in Kodi migration 134 (Omega→Piers). The gate refuses Piers today, so only EXTRA=1 is exercised; when Piers fixtures land (phase-2 hardening), the L2 extras test must assert `itemType = 2` there. Keep the constant in `schema.py` keyed by version, never inlined in the writer.
* **Segment checker vs SyncPlay**: the suppression "inside a SyncPlay group → neither" hook is stubbed in phase 3 (SyncPlay is phase 4). Ensure the stub reads a state flag that phase 4 will set, so wiring SyncPlay later does not require touching the segment engine.

## 8. Exit checklist

* L0 green (mypy relaxed only over `kofin/sync/**` as phase 2; new `service`/`plugin` code is strict); L1/L2 green including the extras asset idempotency + removal-integrity invariants and the Up Next suppression matrix.
* S3.1–S3.5 pass on the test profile with evidence in `tests/live/results/`; skip-button and Up Next timing screenshots captured under both Estuary and skin.contuary.
* Masking grep clean over a segment+extras playback session.
* Report/testing-plan updated in the same commit as any deviation (as phase 2's S2.9 widget-policy revision was).
* No regression in the phase-2 gates: a quick S2.1 re-sync of the test set still matches counts (the extras writer additions must not perturb the main-version rows — the A/B `ab_diff.py` is the guard).
