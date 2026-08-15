# Translation plan

## Context

Kofin ships one language: `resources/language/resource.language.en_gb/strings.po`, 500 `msgctxt` entries, every `msgstr` empty.
Kodi falls back to English for any locale it cannot find, so today every non-English user gets an entirely English add-on — settings labels, help prose, dialogs and notifications alike.

The sibling add-on `../pvr.kofin` solved this in PR #26 (`kontell/machine-translate`, merged 2026-06-26) for 26 languages, and has kept them in sync through four subsequent string-adding commits.
This plan ports that method here, with the same 26 languages, and fixes the four weaknesses the sibling's own history exposed.

Kofin is roughly 3.1× the sibling's volume: 500 entries against 160, ~19.3K characters of English `msgid` per language, ~472 of them actually needing translation.
That scale is the main thing this plan has to design around.

## What the pvr.kofin method actually is

It is worth being precise, because the name "machine translation toolchain" suggests something the sibling does not do: there is **no translation API call anywhere in it**.

The translations are authored by Claude in-session and committed as per-locale JSON data files.
The scripts are a deterministic generator plus two validators, nothing more.
The pipeline is:

1. `classify.py` prints the translatable worklist (`msgctxt` + `msgid`, tab-separated) to stdout, having first asserted that every id in its `PASSTHROUGH` allowlist still exists in the English source.
2. Claude translates that worklist and writes `tr/<locale>.json` — a flat `{msgctxt: msgstr}` map, keys in source order, literal UTF-8, 2-space indent.
3. `gen.py` renders each JSON into a `.po` against the English source. It supplies `msgstr = msgid` for `PASSTHROUGH` ids itself, so those never appear in the JSON. It refuses to write at all if the JSON is missing a key, has an empty value, or carries a key the source does not have.
4. `validate.py` checks the written file structurally: `msgctxt` sequence identical to source, every `msgid` byte-identical to source, no empty `msgstr`, passthrough ids equal to their `msgid`, and no untranslated leakage (`msgstr == msgid` for a translatable id) except for an `OK_IDENTICAL` escape-hatch set.
5. `pocheck.py` is a stdlib stand-in for `msgfmt -c`: strict UTF-8, quoted-line syntax, legal escapes only, entry count matches source.

Output is byte-deterministic — `PO-Revision-Date` is hard-coded — so regenerating with no data change produces no diff.
Every locale is declared once, in `LANG_META` in `gen.py`; the validators import it from there.

The English `msgid` stays English in every locale, because it is the lookup key. Only `msgstr` is filled.

## Languages

The same 26 as pvr.kofin, which is also exactly the set of `tr/*.json` files there:

`ca_es cs_cz da_dk de_de el_gr es_es es_mx fi_fi fr_ca fr_fr hu_hu it_it ja_jp ko_kr nb_no nl_nl pl_pl pt_br pt_pt ro_ro ru_ru sk_sk sv_se uk_ua zh_cn zh_tw`

One difference in starting position: pvr.kofin inherited 76 `resource.language.*` directories from its pvr.iptvsimple fork and left 49 of them as header-only stubs.
Kofin has only `en_gb`, so this work creates exactly 26 new directories and no empty ones. Do not import the stub set — Kodi's English fallback makes them pure dead weight in the zip.

## Layout

Put the toolchain at `tools/i18n/`, not `scripts/i18n/`.
`tools` is already in `EXCLUDE_TOP` in `tools/build.py:41` and in the `dev-install.sh` rsync excludes, so nothing there reaches an installed add-on; a new top-level `scripts/` would be packaged into every install.

```
tools/i18n/
  README.md      maintenance recipe, mirroring pvr.kofin's
  PROMPT.md      the translation brief (new — see Deltas)
  po_lib.py      PO parser, escaping, repo paths
  classify.py    PASSTHROUGH allowlist + worklist dumper
  gen.py         LANG_META + the only writer
  validate.py    structural check
  pocheck.py     well-formedness check
  tr/<locale>.json   × 26
```

`mypy.ini` sets `files = lib, default.py, service.py, context_play.py`, so these scripts are not type-checked; `black` runs over `.`, so they must be black-clean.
Keep them stdlib-only, as the originals are.

`po_lib.py` needs one edit beyond paths: kofin's `resources/language/` sits at the repo root, one `parents[]` level shallower than the sibling's `pvr.kofin/resources/language/`.

## Kofin-specific constraints the sibling's scripts do not cover

These are the things that will break if the toolchain is ported verbatim.

**Source order is not ascending.** `msgctxt` order in `strings.po` has four inversions — `30030` after `30080`, `30041` after `30206`, `30414` after `30420`, `30804` after `30810`. `gen.py` already emits in source order and `validate.py` already compares sequences rather than sorted sets, so this works as-is; the point is not to "fix" it by sorting.

**53 ids carry `%s` or `%d`, and nine of those carry more than one with no positional form.** `#30021 #30602 #30628 #30629 #30716 #30771 #30774 #30806 #30810` are formatted with a plain `%` tuple, so a translation that reorders the placeholders swaps the arguments silently. Count *and* order must be preserved.

**Three help strings contain a literal `%` that is not a placeholder** — the `100%`, `78%` and `80%` in the media-segments, critic-ratings and download help texts. A placeholder validator must not flag them, and translators must not "fix" them into `%%`.

**Four ids embed `\"` escapes** (`#30505 #30506 #30607 #30794`). `po_lib.py`'s escaping already handles this; translators must keep the escaped-quote form rather than substituting typographic quotes inside them.

**One id carries Kodi bbcode**: `#30015`, `Enter code [B]%s[/B] in the Jellyfin app or web interface.` The tags must survive intact.

**`#30794` must quote `#30618` verbatim.** `tests/unit/test_userprefs.py:190` asserts that the caveat string contains the playback-tab setting label in quotes. That invariant is only checked for `en_gb`, so a locale that translates the two inconsistently breaks the user-visible wording with no test failure. Translate them as a pair.

**Notification strings have a ~33-character budget** before Kodi scrolls them — the English source was deliberately shortened for this (see the comment at `strings.po:2020`). Translations of the notification ids should aim for the same length, not the same literal wording.

**~20 msgids already contain non-ASCII** — em dashes, ellipses, and the `→` in `#30611`. These are source text, not encoding damage; keep them.

**Duplicate `msgid` text across distinct ids is deliberate** (`docs/new-content-notifications-plan.md:178` explains `#30633`/`#30635`). Translate each on its own terms; do not collapse them.

## Passthrough set

Starting list for `PASSTHROUGH` in `classify.py` — technical tokens and proper nouns where translating is the defect:

`#30154 #30156 #30158 #30160 #30163 #30164 #30165 #30166` (codec names), `#30169`–`#30175` (Dolby Vision variants — check each: several append descriptor words like "with HDR10 fallback" and are only *partial* passthrough, so they belong in the translatable set), `#30177 #30178 #30179 #30323 #30324 #30325 #30604 #30605` (resolutions), `#30550 #30560` (SyncPlay, a Jellyfin feature name).

`Off` and `Auto` (`#30465 #30466 #30608`) are borderline; pvr.kofin translated its equivalents, so translate them here too and rely on `OK_IDENTICAL` for the locales that legitimately keep the English word.

Finalise this by reading `classify.py`'s stdout dump rather than from this list — the allowlist is asserted against the source, so a wrong id fails loudly.

## Header shape

Kofin's `strings.po` header is leaner than the sibling's: no `Report-Msgid-Bugs-To`, `PO-Revision-Date`, `Last-Translator`, `Language-Team` or `Plural-Forms`, and no `# Addon Provider` comment.

Adopt the sibling's fuller header for the generated locales, and extend `en_gb` to match so the two files have the same shape.
That means adding the five missing header fields to `en_gb` with the usual empty/placeholder values, and having `gen.py` emit, per locale, the fixed `PO-Revision-Date`, `Last-Translator: Claude (machine translation) <noreply@anthropic.com>`, the locale's `Language-Team` and `Plural-Forms`, and the fifth comment line:

```
# Note: machine-translated (LLM), pending native review.
```

That line is the honest label and the thing that invites a native speaker to correct it later. Keep it.

## Addon summary and description

`addon.xml` currently has `<summary lang="en_GB">` and `<description lang="en_GB">` only, and `strings.po` has no non-numeric `msgctxt` entries.

Mirror pvr.kofin: add `msgctxt "Addon Summary"` and `msgctxt "Addon Description"` as the first two entries of `en_gb/strings.po`, with text matching `addon.xml` exactly (the sibling's two copies drifted apart — do not repeat that), and leave the `en_GB` attributes in `addon.xml` as the fallback.
Kodi reads these po entries for the add-on browser. That is 2 extra entries per locale, 52 in total.

## Work sequence

**W1 — toolchain.** Port the five scripts to `tools/i18n/` with kofin's paths, `LANG_META` for the 26 locales, kofin's `PASSTHROUGH`, and the generalised placeholder check (see Deltas). Write `PROMPT.md` and `README.md`. Add the two addon-metadata entries and the header fields to `en_gb`. Nothing generated yet; `classify.py` and `pocheck.py` must run clean against `en_gb` alone.

**W2 — pilot locale.** Author `tr/de_de.json` in full (~474 entries), generate, validate, and read the output. German is the right pilot: it is long-winded enough to expose the notification length budget, and pvr.kofin has a German file to lift terminology from. Fix whatever the pilot exposes in the scripts or the prompt before touching another locale.

**W3 — the remaining 25.** One locale per batch, in the order of `LANG_META`. Each batch is: author the JSON, `gen.py <locale>`, `validate.py <locale>`, commit. Do not attempt all 25 in one pass — ~472 entries × 25 is roughly 470K characters of authored output, and a partial batch that fails `gen.py`'s completeness gate leaves nothing written, so per-locale commits are the resumable unit.

Seed each locale from `../pvr.kofin/scripts/i18n/tr/<locale>.json` rather than starting cold. The two add-ons share terminology — Jellyfin, Kodi, EPG, Quick Connect, "Direct play", "Transcoding", "Advanced" — and the sibling's choices per locale are already reviewed-by-use. Reusing them also keeps the two add-ons consistent for a user who runs both.

**W4 — CI gate.** Add `tools/i18n/validate.py` + `pocheck.py` to `.github/workflows/ci.yml` as a job, or as a unit test (see Verification). Both are stdlib-only and sub-second.

**W5 — release.** Bump `addon.xml`, prepend a `changelog.txt` entry naming the languages and the "machine-translated, pending native review" status, per `CLAUDE.md`'s release recipe. No packaging change is needed: `tools/build.py` is exclude-list based and `resources/language/resource.language.*` matches none of its excludes, so the new directories ship automatically.

## Deltas from pvr.kofin

Four changes, each answering something the sibling's own git history shows going wrong.

**1. Commit the prompt.** `tools/i18n/PROMPT.md` holds the translation brief: tone (informal second person where the locale's Kodi convention is informal), the do-not-translate list (Jellyfin, Kodi, SyncPlay, Quick Connect, codec/resolution tokens), locale-correct typographic quotes, the placeholder rules above, and the notification length budget. In pvr.kofin this contract exists only in two commit messages and is not reconstructable without git archaeology.

**2. Generalise the placeholder check.** `pocheck.py` in the sibling checks exactly one hard-coded literal fragment. Replace that with: extract the multiset of `%s`/`%d` specifiers from each `msgid`, assert the same multiset appears in the `msgstr`, and assert the sequence matches for the nine multi-placeholder ids. Whitelist the three literal-`%` help strings by id. This is the check that would otherwise let a locale swap two `%s` and crash nothing until a user sees "Needs 4.2 GB, only 900 MB free" backwards.

**3. Detect reworded `msgid`s, not just missing keys.** `gen.py` hard-fails on a missing key but a *changed* English string silently keeps its stale translation — exactly the drift pvr.kofin had to repair by hand in `bb5de9b`, where locales were two English revisions behind on `#30732`. Store the English source text (or its hash) alongside each translation in `tr/<locale>.json` and have `gen.py` refuse to write when it disagrees with the current `msgid`. The JSON becomes `{"#30021": {"en": "...", "tr": "..."}}` or a parallel `_en` map — either is fine, but decide in W1, before 26 files exist in the old shape.

**4. Gate it in CI.** pvr.kofin runs its validators by hand and has no test suite. Kofin has one, and `tests/unit/test_settings.py:113` already parses the shipped po — so the natural home is a new `tests/unit/test_translations.py` that shells the two validators or reimplements their asserts against every locale. Then a PR that adds an English string cannot merge with 26 locales out of sync.

## Verification

- `python3 tools/i18n/classify.py` — prints counts and the worklist; non-zero exit if a `PASSTHROUGH` id no longer exists.
- `python3 tools/i18n/gen.py` then `git diff --stat` — must be empty on a re-run with no data change. This is the determinism check.
- `python3 tools/i18n/validate.py` — `OK <locale>` for all 26.
- `python3 tools/i18n/pocheck.py` — all 26 well-formed, all placeholders preserved.
- `for f in resources/language/resource.language.*/strings.po; do msgfmt -c -o /dev/null "$f"; done` — the authoritative check where gettext is installed.
- `.venv/bin/python -m pytest tests/unit -q` — the existing string tests (`test_settings.py`, `test_new_content.py`, `test_userprefs.py`, `test_plugin_actions.py`) plus the new `test_translations.py`.
- `.venv/bin/black --check --diff .` over the new scripts.
- Live: `tools/dev-install.sh`, then set Kodi's language to German and **restart Kodi fully** — new `strings.po` content needs a full restart, not an add-on bounce (`CLAUDE.md`, `kodi-process-control`). Walk the settings dialog and the Kofin browse root, and fire a notification path (a sync toast) to confirm the ~33-character budget holds in the longest locale. Screenshot rather than assume (`kodi-screenshot-review`).

## Maintenance

Once this lands, adding a user-visible string means touching 27 files, and the toolchain is what makes that mechanical rather than error-prone:

1. Add the entry to `en_gb/strings.po`.
2. Add the key (with its English text, per Delta 3) to all 26 `tr/*.json` — or add the id to `PASSTHROUGH` if it is a technical token.
3. `python3 tools/i18n/gen.py && python3 tools/i18n/validate.py && python3 tools/i18n/pocheck.py`.
4. Commit English source, JSONs and generated `.po` files together — the sibling's four maintenance commits all do this, and the one time it was skipped produced `bb5de9b`.

Reword an English string and step 2 is mandatory too: Delta 3 makes `gen.py` say so instead of shipping a stale translation.

Add a `## Translations` section to `CLAUDE.md` pointing at `tools/i18n/README.md`, since the string surface is now 27 files rather than one.

## Risks

The volume is the risk. ~472 entries × 26 locales is roughly 12,300 authored translations, most of them settings help prose rather than single-word labels.
Per-locale commits and seeding from the sibling's JSON are what keep that tractable; attempting it as one pass is how a half-finished locale ends up committed.

The second risk is honesty about quality. These are machine translations. The header line saying so is not decoration — it is what stops a native speaker assuming the file has been reviewed, and it is why `pending native review` should stay until someone actually reviews it.
