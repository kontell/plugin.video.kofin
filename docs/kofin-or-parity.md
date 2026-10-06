# Kofin OR parity ledger

Baseline: main `db709a28905ca3b697d7135814407e9476d29361`, Kofin **0.29.0**.
Created in phase 0 on 4 October 2026. [Plan](kofin-or-implementation-plan.md) · [Phase 0 evidence](research/kofin-or/phase0/README.md) · [Feasibility report](kodi-api-sync-feasibility.md).

This is the maintained acceptance ledger. The first 48 rows correspond, in order, to report §8. Additional rows cover callers outside sync and dynamic browsing. Every common feature added to main must update this ledger and identify its OR counterpart in the PR. Neither a successful probe nor a planned fallback counts as a shipped port. **Phase 1 implements the shared boundary and internal API package foundation; native API ingestion is still disabled.** See the [phase 1 implementation and evidence](research/kofin-or/phase1/README.md).

Status: **P** = public route probed, implementation/test pending; **D** = design pending; **G** = exact native parity blocked by a Kodi gap; **R** = legacy mechanism deliberately retired; **I** = implemented for the stated internal-build scope. A row closes only with a linked implementation and passing acceptance evidence. Kodi contribution numbers refer to feasibility report §9; their inclusion is a requirement/proposal, not a promise of upstream acceptance.

## Sync functionality

| ID / main behavior | Piers implementation or fallback | v23 requirement | Acceptance scenario / phase | Status |
|---|---|---|---|---|
| S01 Server fetches, pagination, field transforms | Share network/pure transforms; keep browser fields lightweight | None | Same DTOs; paginated failure must not publish incomplete generation / 1, 3 | D: shared transforms implemented; snapshot completeness in 3 |
| S02 Whitelists, checksums, watermarks, repair guards | Private desired/applied generations; separate fetched watermark | None | Crash between fetch and native apply; replay without duplication / 3 | D |
| S03 Native movies | Complete plugin snapshot; SetSourceContent and scan | Optional bulk/job API §9.3 | Add, refresh, query stable identity, remove; foreign item survives / 3 | P |
| S04 Native shows/episodes | Show hierarchy, bind root and individual show | Optional bulk/job API | Show + season + episode; refresh hierarchy; no duplicate parents / 4 | P |
| S05 Native music videos | Typed plugin listing and source binding | Optional bulk/job API | Add/update/remove clip / 4 | P |
| S06 Songs/albums/artists | Complete tagged directories; size bridge marks loaded; post-import state | Loaded contract and failure handling §9.1 | Normal/forced scan, failed/partial listing, empty replacement, recovery / 5 | G: failed listing loses songs |
| S07 Common scalar metadata | Type-specific setters, readback; refresh missing fields | Song release-date fix §9.1 | Set, replace and clear each mapped field / 3–5 | P; field matrix pending |
| S08 Cast/roles/order/thumbs | Scanner/refresh tags; preserve userdata around refresh | Direct cast setter optional §9.5 | Change role/order/art and remove a cast member / 3 | P |
| S09 Video/audio/subtitle streams | InfoTag ingestion/refresh; verify post-playback authority | Stream flags/authority, setters §9.5 | Multi-stream import; playback must not corrupt provider facts / 3, 8 | G: exact flags/authority |
| S10 Provider IDs/named ratings/default ID | Set maps; refresh to establish default ID | Clarified merge/authority if needed §9.5 | Multiple IDs/ratings; remove stale entry; preserve default / 3 | P; defaults partial |
| S11 Video date added | Explicit detail setter | None | Import date survives refresh and replay / 3 | P |
| S12 Exact music date/scan/scrape bookkeeping | Native-derived state; retain unavailable facts privately | Public fields if exact native parity required | Compare import dates; do not counterfeit scanner internals / 5 | G |
| S13 Movie/show/music-video favourites | Supported tag setters, preserve unrelated tags | None | Server favourite toggles with foreign tag retained / 3–4 | P |
| S14 Episode native favourite tags | Live Kofin favourites view | Persist/query episode tags §9.1 | Episode favourite in native filter and live browser / 4, 8 | G |
| S15 Video playcount/lastplayed/resume | Public setters plus expected-update echo suppression | Job identity support helps §9.1 | Import, user change, server change, refresh ID churn, no echo / 3 | P |
| S16 Song playcount/lastplayed/rating | Post-scan SetSongDetails | Initial-state preservation desirable §9.1 | Initial playcount is currently discarded; restore and avoid echo / 5 | P |
| S17 Season title/art | Set details for discovered seasons; own empty-season views | Empty season visibility §9.5 | Populated and empty season readback / 4 | P; empty partial |
| S18 Season plot | Supply addSeason plot on ingestion | Get/set season plot §9.1 | Ingest, read, update, clear and refresh / 4, 8 | G: read/update surface |
| S19 Display/sort episode numbering | InfoTag ingestion; refresh changes | Direct setters optional §9.5 | Specials/sort order differ from aired numbering / 4 | P; updates pending |
| S20 Shared show/season pooling and aliases | Owned identities plus native scanner matching | Provider ownership contract if matching insufficient §9.3 | Same show in two libraries; remove one without losing the other / 4 | D |
| S21 Missing-parent healing | Stage valid hierarchy, rescan, reconcile returned IDs | None initially | Delete parent externally; restore without unrelated changes / 4 | D |
| S22 Collection membership/set metadata | Movie set assignment and set details | None for populated sets | Relink changed members; stale IDs and duplicate names / 4 | P |
| S23 Empty sets/precise set removal | Kofin collection views | Owned empty objects/removal §9.3, §9.5 | Empty set appears and only owned set removed / 4, 8 | G |
| S24 Native movie versions/default/extras creation | Stable resolver, live extras; addon version chooser to add | Python asset ingestion or asset API §9.2 | Two versions, default, extra; no NFO/STRM bridge / 4, 8 | G; deferred priority |
| S25 Incremental version/extra editing/state | Own asset state; no promised native whole-movie rebuild | Asset lifecycle API §9.2 | Rename/remove/default change; per-file userdata / 8–9 | G; deferred priority |
| S26 Version selection/special-feature playback | Preserve resolver media-source parameter and extras browser; add chooser | None for addon UI | Unsynced/synced playback of selected source and extra / 4 | D |
| S27 Empty artists/albums/singles shells | Song-backed native objects; live Kofin views for empty objects | Provider object creation §9.3 | Empty album/artist and singles presentation / 5, 8 | G: exact native objects |
| S28 Artist credits/fallback artists | Supported updates and complete rescans; reconcile matching | Rich credit API if needed §9.3 | Multiple artists, missing MBIDs, duplicate names, changed credits / 5 | D |
| S29 Exact discography/role rows | Retain unavailable relationships in own/live views | Typed relationship surface §9.3 | Role change and missing/empty releases / 5, 8 | G |
| S30 Synthetic native music source/album links | Register real user source; own views for arbitrary grouping | Scoped source ownership/membership §9.4 | Two server libraries sharing album/artist; independent removal / 5, 8 | G; manual source proven |
| S31 Music browsing by server/library | Stable paths and Kofin views | Only for exact native source filters §9.4 | Root/drill-down remains correct before sync and after overlap / 5 | D |
| S32 Media artwork | Scanner art plus supported setters | None for accepted keys | Replace, clear, inheritance and URL normalization / 3–6 | P |
| S33 Actor artwork precaching | DTO/API discovery plus image VFS | Optional precache convenience API | Cold/warm actor image and no SQL/cache-row synthesis / 6 | P |
| S34 Texture CRC/dimensions/usage rows | Kodi owns cache | None; do not request raw row API | No native cache DB access from OR archive / 1, 6 | R |
| S35 Server chapter images in native bookmarks | Addon chapter chooser can retain server images | Chapter-art association §9.5 | Open native bookmarks and select a server chapter / 6, 8 | G |
| S36 Downloads/subscriptions | Own store; stable resolver chooses local file | None for resolver route | Download, offline play, subscription update, removal / 6 | D |
| S37 Native file/song relocation | Avoid changing native path; keep resolver stable | Relocation API only if resolver insufficient §9.5 | Download/undownload preserves native identity and resume / 6 | G as-is; redesign |
| S38 Dotted download artwork key | Rename badge and all consumers to supported key | None | Badge shown/cleared in native and dynamic views / 6 | P |
| S39 Parent badges/video download tags | Resolve parents and patch supported art/tags | Episode tag gap as S14 | All/partial downloaded state propagates and clears / 6 | D |
| S40 Downloaded music/path-driven views | Own download membership; paths remain stable | None for Kofin view | Downloaded music view correct offline / 6 | D |
| S41 Ordered playlists/duplicate entries | Owned M3U8 snapshots; public/private identity lookup | None | Duplicate tracks, reorder, remove, offline rebuild / 6 | D |
| S42 Global nodes/playlists installation | Explicit opt-in, ownership manifests, narrow cleanup | None | Foreign files survive reset/update; user consent preserved / 6 | D |
| S43 Notifications/refresh coalescing | Announce confirmed generation after readback | Job/result reporting §9.1; optional bulk §9.3 | Mixed success and failed listing do not announce full apply / 3 | D: confirmed-result notifications implemented; API readback in 3 |
| S44 Widget SQL fingerprints | Own generation + notifications/API readback | None | Widget refresh only after material applied change / 3, 6 | R; replacement pending |
| S45 Page transaction/rollback | Durable intent/retry/reconciliation; no RPC transaction fiction | Owned jobs/transactions §9.3 optional | Crash at each mutation; mixed batch success; replay converges / 3 | G: exact atomicity |
| S46 Kodi schema gates/seeding/migrations | Public capability contract; retain private schema migration | None | Unsupported API fails before sync; no Omega path / 1 | R: private split, package exclusion and runtime capability report implemented |
| S47 Library removal/repair | Ownership-checked video removal; complete music replacement | Safe scoped music removal §9.4 | Remove one selected library, keep overlapping/foreign content / 3–5 | D |
| S48 Wipe/reseed native DBs | Fresh transition performed by old main/external setup; OR resets owned content only | None; omit broad DB nuke | New profile/library required; no legacy adoption or SQL reset / 3, 7 | R |

## Whole-addon and dynamic browsing gates

Paths below are relative to `lib/kofin/`. These are reachable call chains, including callers that themselves only open the private database. Splitting private DB access must not accidentally retain an import of native discovery/seeding code.

| ID / current behavior and caller | OR route / fallback | v23 requirement | Acceptance scenario / phase | Status |
|---|---|---|---|---|
| A01 `downloads/manager.py`, `downloads/repoint.py` write native paths/badges; `sync/kodidb/downloads.py` implements mechanics | Backend download operations; stable resolver and own membership | S37 only if needed | Finished, cancelled, replaced and missing local download / 1, 6 | D: manager/routes gated and native modules omitted in 1; port in 6 |
| A02 `service/artcache.py`, `core/imagecache.py` and texture adapter cache artwork | Image VFS; API/DTO actor discovery | None | Empty/cold/warm cache and failed URL / 6 | D: actor precache gated and texture modules omitted in 1; port in 6 |
| A03 `service/chapters.py` injects texture aliases | Disable native alias route; explicit chapter chooser fallback | S35 | No texture DB access on playback / 1, 6 | G: native texture aliases omitted; chapter startup gated |
| A04 `service/libraryclaim.py` maps player/native IDs and clears bookmarks through native SQL | Validated private mappings, public setters, dynamic Jellyfin identity | None | Start native/dynamic item, reset resume; foreign item ignored / 3 | D: legacy mapping/backfill gated; API identity work in 3 |
| A05 `plugin/actions.py`, `plugin/context.py`, `service/remote.py`, `service/settings_apply.py` use sync/private mappings and backend helpers | Import private store independently; backend-aware actions | None | Favourite/watched/reset/remote play work without sync mappings / 1, 3 | I: private imports and dynamic actions tested; native reconciliation in 3 |
| A06 `plugin/clean.py`, `sync/clean.py`, removal/prune/refresh helpers | Transition gate; narrow owned-content cleanup | S47 | Block old mappings from starting OR sync; live browsing still works / 3 | D: cleaner omitted; backend marker refuses legacy adoption; full reset gate in 3 |
| A07 `downloads/store.py`, `pending.py`, `subscriptions.py` use own DB through mixed DB module | Keep own SQLite; separate it from native schema imports | None | Existing download records and tasks survive supported branch changes / 1, 6 | I: independent private storage; download feature port remains in 6 |
| A08 `service/main.py` primes native people cache and wires workers | Select backend explicitly; SQL-only startup absent from OR package | None | Start OR with native SQL modules physically excluded / 1 | I: extracted package and P1D service startup with native imports blocked |
| A09 `sync/views.py`, nodes/backdrop consumers install/generated presentation | Preserve `Kofin.nodes.*`; move mutable backdrop into profile | None | Addon installation remains immutable; home widgets retain content / 6 | I: dynamic properties and profile backdrop; native presentation in 6 |
| B01 `plugin/browse.py` live root, filters, search, Next up/Continue watching | Preserve Jellyfin-backed routes independently of scanner snapshots | None | No selected libraries; mixed sync; initial/paused/failed sync / 1 and every preview | I: extracted-package goldens for all four sync states |
| B02 `plugin/listitems.py` lightweight dynamic metadata | Share pure transforms with separate browser/scanner serializers | None | Bounded fields/cast requests; compare listing latency during large scan / 1, 3 | D: field/listing regression gate passes; large-scan latency in 3 |
| B03 Existing browse/play URLs, widgets and favourites | Keep supported aliases; resolve unsynced identity without Kodi-ID lookup | None | Saved shortcut and widget play before first sync / 3 | I: existing dynamic URLs/playback retained; native ownership in 3 |
| B04 Music Play all/Shuffle, server context actions, resume, extras | Live server actions plus backend reconciliation where mapped | None for live functions | All media remain browsable when native preview supports only movies / 3 | I: live listing/actions/source/resume regression gate; native integration in 3 |
| B05 Native and live views share one item | Expected-update tokens; current mapping ownership checks | Job identity improves robustness | Toggle watched from each view; server change; no feedback loop / 3 | D |
| O01 Main/OR maintenance and distribution | Separate version/backend commits; CI on both; OR prereleases only | None | Shared fix has counterpart; no OR package advertised as stable / 0, 2 | Shared commits ported with -x; both branch CI; publisher pending |
| O02 Official archive cannot access native DBs, mutate installed files, or expose unfinished SQL routes | Package exclusion + import/access tests; disable incomplete features | None | Exercise every enabled UI/background route with native-file guard / 1, 6–7 | I: phase 1 package/import/file guards and delayed startup; repeat audit as features return |

## Milestone review

- [x] Main baseline identity, public capability floor, probes and initial writer timings recorded.
- [x] Failed music enumeration, normal metadata scan and manual forced rescan investigated.
- [ ] Revalidate on stock RC1 **binaries** when available; current dirty build explicitly approved for development until binaries are distributed.
- [x] Phase 1: shared boundary, independent private store, package gates and dynamic-browsing regression suite; [P1D verification](research/kofin-or/phase1/README.md).
- [ ] Phase 3: native movie lifecycle and recovery; first public `0.90.0` prerelease.
- [ ] Phase 5: reliability decision and recovery evidence for native music.
- [ ] Phase 7: stock Piers package/whole-addon audit; repository submission readiness.
- [ ] Phase 9: review each remaining native gap and fallback with the user before declaring parity and retiring main.
