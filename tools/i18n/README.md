# Translations

**The translations here are machine-generated (LLM) and tagged `pending native review` in each file header.**
They are a starting point, not a substitute for a native speaker's pass — especially for the longer help texts.

`resources/language/resource.language.en_gb/strings.po` is the English source.
In every other locale file the `msgid` stays English (it is the lookup key) and only `msgstr` is filled in.
Kodi auto-discovers `resource.language.*` directories and falls back to English for any string a locale does not translate, so a partial or absent translation never regresses anything.

This directory holds a generator and two validators. There is no translation service involved: the translations are authored by hand or by a model into `tr/<locale>.json`, and `gen.py` renders them into `.po` files. `PROMPT.md` is the brief those translations are written against.

## Layout

```
PROMPT.md          the translation brief -- read this before writing a locale
classify.py        PASSTHROUGH allowlist + the worklist dumper
po_lib.py          PO parsing, escaping, and the template renderer
gen.py             LANG_META + the only thing that writes a .po
validate.py        structural check on generated files
pocheck.py         well-formedness + placeholder/bbcode preservation
tr/_source.json    the English text each translation was made from
tr/<locale>.json   ctx -> translated msgstr, 26 locales
```

Everything is stdlib-only and lives under `tools/`, which `tools/build.py` excludes from the add-on zip.

## Generating

```bash
python3 tools/i18n/gen.py                 # every locale in LANG_META
python3 tools/i18n/gen.py de_de fr_fr     # named locales
python3 tools/i18n/validate.py            # structural
python3 tools/i18n/pocheck.py             # well-formedness + placeholders
```

Where gettext is installed, the authoritative syntax check is:

```bash
for f in resources/language/resource.language.*/strings.po; do
  msgfmt -c -o /dev/null "$f"; done
```

Output is deterministic — `REVISION_DATE` in `gen.py` is fixed — so regenerating with no data change produces no diff. `git diff --stat` being empty after a `gen.py` run is itself a test.

## Why the renderer copies the source line by line

`po_lib.render()` walks the English file and substitutes `msgstr` values in place, rather than rebuilding each entry from parsed data.

That is deliberate. kofin's `strings.po` carries multi-line comment blocks that record *why* ids are the way they are (retired ids that must never be reused, the sync-block ids that kept their numbers after moving tab), and its `msgctxt` order is not ascending — there are four inversions. A block re-assembler flattens the comments and invites someone to "fix" the ordering. Copying the body verbatim makes every locale structurally identical to the source for free.

## The `_source.json` snapshot

`gen.py` hard-fails when a key is missing from a locale. That catches an *added* string but not a *reworded* one: the translation is still present and still valid-looking, it just now describes something else.

That is not hypothetical — the sibling add-on's 26 locales silently went two English revisions stale on one string, and it took a manual audit to find (`pvr.kofin` commit `bb5de9b`).

So `tr/_source.json` records the English text every translation was made from, and `gen.py` refuses to write when the current source disagrees with it. Restamping it is a deliberate act that asserts "the locales now match the English":

```bash
python3 tools/i18n/gen.py --snapshot
```

The snapshot is shared across all locales rather than duplicated into each one, so a reworded string invalidates every locale at once. That is the correct blast radius — a reworded English string does invalidate all 26 — but it does mean you cannot restamp after fixing only some of them.

## Adding or changing an English string

1. Edit `resources/language/resource.language.en_gb/strings.po` as usual.
2. If the new entry is a bare technical token, add its id to `PASSTHROUGH` in `classify.py` and stop — the generator supplies `msgstr == msgid` itself.
3. Otherwise add the key to all 26 `tr/<locale>.json` files. `gen.py` lists exactly what is missing, so this cannot drift silently.
4. `python3 tools/i18n/gen.py --snapshot`
5. `python3 tools/i18n/gen.py && python3 tools/i18n/validate.py && python3 tools/i18n/pocheck.py`
6. Commit the English source, the JSONs and the generated `.po` files together. Splitting them is how the drift above happened.

Rewording an existing string means step 3 as well — `gen.py` will say so.

`tests/unit/test_translations.py` runs the validators, so a PR that skips this fails CI rather than shipping 26 stale locales.

## Adding a locale

1. Add an entry to `LANG_META` in `gen.py` — locale key, `Language:` header value, `Language-Team` name, and the correct `Plural-Forms` for that language.
2. Write `tr/<locale>.json` with a translation for every non-passthrough entry (`classify.py` prints the worklist).
3. `python3 tools/i18n/gen.py <locale> && python3 tools/i18n/validate.py <locale> && python3 tools/i18n/pocheck.py <locale>`

The directory is created for you. No packaging change is needed: `tools/build.py` is exclude-list based and ships `resources/language/**` wholesale.

## `OK_IDENTICAL`

`validate.py` treats a `msgstr` equal to its `msgid` as an untranslated leftover, because that is usually what it is.
`OK_IDENTICAL` is the escape hatch for words a locale legitimately keeps in English — loanwords like *Auto*, or feature names like *Quick Connect*.
Add to it only after checking that the locale really does use the English word.
