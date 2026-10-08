# OR phase 3 — movie lifecycle

Implementation of the approved phase 3 in `kofin-or-implementation-plan.md`. The P1D Piers Flatpak is the approved live target for development and release validation; its passing behavioral checks satisfy the corresponding acceptance gates.

The API worker owns one movie source per server/user namespace. Selected library membership lives in the private catalogue and native tags; stable query-style item URLs never contain a Kodi ID or library ID, so moving a movie between selected libraries does not change playback identity. Interactive browsing remains live and independent. Native TV/music/collections, downloads and texture SQL remain disabled.

Publish validated complete movie library listings atomically into immutable directory generations. Failed, duplicate, short or changing-total pages cannot replace a usable directory or infer removal. Explicit websocket removals become durable tombstones. Pin scanner generations until completion/readback; retain pending operations across restart and verify identities by both stable URL and namespaced unique ID before updating or removing.

Implement complete scanner tags and refresh callbacks, scalar detail patches, cast/stream refresh, post-import userdata and API-visible reconciliation. Expected userdata values carry generations and expiry; real differing local changes are queued durably for delivery. A persistent first-run gate requires an empty, prepared native library and compatible private backend state; browsing stays usable when blocked. No automatic broad reset or adoption of existing native rows.

Verify store atomicity and restart recovery, stale acknowledgements, native ID replacement, ownership, partial server replies, feedback suppression, scanner handle closure and packaged import/file boundaries. Run the full repository quality gates and exercise the packaged movie lifecycle against P1D with owned fixture content, retaining scrubbed evidence. Prepare the release artifact and scope notes; publish after its lifecycle and distribution release gates are met.
