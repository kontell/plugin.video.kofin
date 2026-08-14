"""Structural check on the generated locale files.

Per locale, asserts that:
  * the msgctxt sequence is identical to the English source, in source order
    (kofin's strings.po is deliberately not in ascending id order);
  * every msgid is byte-identical to the source -- the msgid is the lookup key,
    so a translated one simply never matches;
  * no msgstr is empty;
  * PASSTHROUGH ids carry msgstr == msgid;
  * no translatable entry was left in English, apart from OK_IDENTICAL.

    python3 tools/i18n/validate.py            # every locale
    python3 tools/i18n/validate.py de_de      # one locale
"""

import sys

from classify import PASSTHROUGH
from gen import LANG_META
from po_lib import LANG_DIR, parse_entries

# Translatable ids where msgstr == msgid is a legitimate translation rather than
# an untranslated leftover: loanwords and product names that most locales keep.
# Extend this as locales land; every addition should be a word you have checked.
OK_IDENTICAL = {
    "#30011",  # Quick Connect (Jellyfin feature name)
    "#30465",  # Off
    "#30466",  # Auto
    "#30608",  # Off
}


def check(locale, source):
    path = LANG_DIR / ("resource.language.%s/strings.po" % locale)
    if not path.exists():
        return None
    entries = parse_entries(path)
    issues = []

    if [e["ctx"] for e in entries] != [e["ctx"] for e in source]:
        return ["msgctxt sequence differs from the English source"]

    for got, want in zip(entries, source):
        ctx = want["ctx"]
        if got["msgid"] != want["msgid"]:
            issues.append("%s: msgid altered (it is the lookup key)" % ctx)
            continue
        if not got["msgstr"].strip():
            issues.append("%s: empty msgstr" % ctx)
        elif ctx in PASSTHROUGH:
            if got["msgstr"] != want["msgid"]:
                issues.append("%s: passthrough id was translated" % ctx)
        elif got["msgstr"] == want["msgid"] and ctx not in OK_IDENTICAL:
            issues.append("%s: left in English" % ctx)
    return issues


def main(argv):
    source = parse_entries()
    unknown = [a for a in argv if a not in LANG_META]
    if unknown:
        raise SystemExit("unknown locale(s): %s" % ", ".join(unknown))
    failed = 0
    checked = 0
    for locale in argv or list(LANG_META):
        issues = check(locale, source)
        if issues is None:
            print("SKIP %s: not generated yet" % locale)
            continue
        checked += 1
        if issues:
            failed += 1
            print("FAIL %s (%d issue(s))" % (locale, len(issues)))
            for line in issues[:20]:
                print("    %s" % line)
            if len(issues) > 20:
                print("    ... and %d more" % (len(issues) - 20))
        else:
            print("OK %s" % locale)
    if failed:
        raise SystemExit("%d locale(s) failed" % failed)
    print("ALL GOOD (%d locale(s))" % checked)


if __name__ == "__main__":
    main(sys.argv[1:])
