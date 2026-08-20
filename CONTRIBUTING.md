# Contributing to Kofin

Kofin is a Jellyfin client for Kodi, and contributions are welcome: bug reports, fixes, features, translations, docs. This page is the short version. The detail lives in [`CLAUDE.md`](CLAUDE.md) (architecture, and the constraints that are easy to re-break), the plans in [`docs/`](docs/), and the [wiki](https://github.com/kontell/plugin.video.kofin/wiki).

## Philosophy

**Integrate natively with Kodi.** Jellyfin content lands in Kodi's own library — real MyVideos/MyMusic rows, library nodes, playlists — so skins, widgets, resume, watched state and the context menu work with no plugin awareness. When a feature can be done through a Kodi mechanism, use the Kodi mechanism: Kodi's resume prompt rather than a dialog of our own, Kodi's context entries and core strings rather than look-alikes, Kodi's playlist player rather than a custom queue, a library node rather than a custom window.

**Minimal UI, and only where Jellyfin needs it.** Kofin adds an element of its own only when a Jellyfin feature has no native Kodi home: the Jellyfin actions context menu for server-side state Kodi cannot reach, the Who's watching? picker, SyncPlay groups, the stream menu a transcode needs. Before adding a dialog, a root entry, a toast or a setting, ask what Kodi already offers for it and whether a viewer would miss it. Settings and strings earn their place — every user-visible string is 28 files, and every setting is one more thing a viewer has to understand.

Two corollaries shape the code. The plugin process stays thin and stateless, with everything long-lived in the service. And the sync writers are a transplant from jellyfin-kodi whose semantics stay put, because their equivalence was proven against real libraries. `CLAUDE.md` has the specifics.

## Kodi knowledge lives in kodi-drive

Everything general about Kodi — how it behaves, what breaks, what a log line means — lives in [kodi-drive](https://github.com/kontell/kodi-drive), a shared set of verified skills, not in this repo. Read its `kodi-orientation` skill before touching Kodi code; it has the map and the four working rules. When a session teaches you something about Kodi that another add-on would need, contribute it there — its `contribute` skill walks the bar: observed, sourced, or inferred and labelled as such. `CLAUDE.md` holds only what is specific to kofin.

## Before you start

- Read `CLAUDE.md` end to end. The list of constraints that are easy to re-break is the accumulated cost of past bugs.
- For anything beyond a small fix, write a plan in `docs/` first and get it agreed. The existing plans show the shape: what is wrong, the evidence, the options, the decision. One line per paragraph (`tools/unwrap_md.py` fixes wrapped files).
- Verify against a real Kodi, not by reading. Kodi fails silently almost everywhere. Live scenarios live in `docs/testing-plan.md` and evidence under `tests/live/results/` (gitignored). Hosts and credentials go in `~/.config/kodi-drive/targets.env`, never in the repo — and kodi.log carries stream URLs with tokens at debug level, so scrub a log before pasting it anywhere.

## Making a change

- Branch from `main`: `feat/…`, `fix/…`, `perf/…`, `refactor/…`, `chore/…`. Commit subjects are conventional (`feat(browse): …`, `fix(syncplay): …`); the body says why, with the measurement or the failure that motivated it.
- Run `tox` — black, mypy and the unit suite — before opening a PR; CI runs the same three plus a package build. Writer or schema changes also need the L2 suite (`tests/unit/test_sync_writers.py`) green on every gated database version.
- Add tests with the change: L1 units against Kodistubs and `tests/unit/fakes.py` for shell code, L2 for writers, and a live gate in `docs/testing-plan.md` for anything user-visible.
- Comments explain why, not what: the number you measured, the Kodi behaviour you hit, the alternative you rejected. Match the dialect of the file you are in — shell code follows current idioms, the transplant keeps the fork's.
- Keep a PR to one topic. A version bump and changelog entry travel with a release, or with the feature when the maintainer asks for it.

## Strings and translations

English is `resources/language/resource.language.en_gb/strings.po`; the other 26 locales are generated from `tools/i18n/tr/<locale>.json` and never hand-edited. Prefer a Kodi core string id where Kodi already has the word — it costs nothing in any language. Otherwise follow `tools/i18n/README.md`: add the id, add the key to every locale, `gen.py --snapshot`, then `gen.py && validate.py && pocheck.py`, and commit the English source, the JSONs and the `.po` files together.

## Changelog and releases

`changelog.txt` is written for viewers: what changed for them and, where it helps, why. The top paragraph becomes the GitHub release body. Version bumps, tagging and publishing are described in `CLAUDE.md`.

## Reporting a problem

Open an issue with the Kodi version and platform, the Jellyfin version, what you did, what happened, and a debug log with credentials scrubbed. Diagnoses are welcome; say what you verified and what you inferred — the issues that help most are the ones that keep the two apart.
