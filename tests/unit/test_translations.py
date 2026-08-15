"""Gate the generated locale files.

pvr.kofin runs its i18n validators by hand and has no test suite, which is how
its 26 locales quietly went two English revisions stale on one string. kofin has
a suite, so the validators run here: a PR that adds or rewords an English string
cannot merge with the locales out of step with it.

Everything is checked in memory. Nothing here writes a .po file — the
regenerability test compares what is on disk against what the generator would
produce, which catches a hand-edited locale and a stale one with the same
assertion.
"""

import sys
from pathlib import Path

import pytest


def _repo_root():
    return Path(__file__).resolve().parents[2]


sys.path.insert(0, str(_repo_root() / "tools/i18n"))

import classify  # noqa: E402
import gen  # noqa: E402
import po_lib  # noqa: E402
import pocheck  # noqa: E402
import validate  # noqa: E402


def _generated_locales():
    """The locales that actually have a file, so this suite passes on a branch
    where only some of them have landed."""
    return [
        locale
        for locale in gen.LANG_META
        if (po_lib.LANG_DIR / ("resource.language.%s/strings.po" % locale)).exists()
    ]


@pytest.fixture(scope="module")
def source():
    return po_lib.parse_entries()


def test_passthrough_ids_all_still_exist(source):
    """A renumbered id would leave PASSTHROUGH pointing at nothing, and the
    token it protects would start being translated with no other signal."""
    ctxs = {entry["ctx"] for entry in source}
    assert sorted(c for c in classify.PASSTHROUGH if c not in ctxs) == []


def test_the_source_snapshot_matches_the_english_file(source):
    """tr/_source.json records the English each translation was made from.
    When this fails, an English string moved and its 26 translations now
    describe something else -- see tools/i18n/README.md."""
    gen.check_source(source)


@pytest.mark.parametrize("locale", _generated_locales())
def test_locale_is_exactly_what_the_generator_would_write(locale, source):
    """Catches a hand-edited .po and a locale left unregenerated after its JSON
    changed. The generated file is output, not source: edit tr/<locale>.json."""
    path = po_lib.LANG_DIR / ("resource.language.%s/strings.po" % locale)
    translations = {
        entry["ctx"]: (
            entry["msgid"]
            if entry["ctx"] in classify.PASSTHROUGH
            else gen.load_tr(locale)[entry["ctx"]]
        )
        for entry in source
    }
    expected = po_lib.render(gen.header(locale), translations)
    assert path.read_text(encoding="utf-8") == expected


@pytest.mark.parametrize("locale", _generated_locales())
def test_locale_is_structurally_sound(locale, source):
    """msgctxt order, untouched msgids, no empty or left-in-English msgstr."""
    assert validate.check(locale, source) == []


@pytest.mark.parametrize("locale", _generated_locales())
def test_locale_preserves_placeholders_and_markup(locale, source):
    """The one that matters at runtime: kofin formats with a plain % tuple and
    has no positional form, so a reordered pair of %s swaps the arguments and
    nothing else would notice."""
    path = po_lib.LANG_DIR / ("resource.language.%s/strings.po" % locale)
    errors = []
    assert pocheck.check_lines(path, errors) is not None
    pocheck.check_entries(locale, po_lib.parse_entries(path), source, errors)
    assert errors == []
