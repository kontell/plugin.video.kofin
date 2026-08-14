"""Well-formedness and safety checks on the generated locale files.

Stands in for `msgfmt -c` where gettext is not installed, and adds the checks a
Python add-on needs that msgfmt does not do:

  * every %s/%d specifier survives translation, in the same order -- kofin
    formats with a plain % tuple and has no positional (%1$s) form anywhere, so
    a reordered pair swaps the arguments silently;
  * Kodi bbcode tags survive ([B]%s[/B] in #30015);
  * #30794 keeps quoting #30618's wording, the cross-string dependency
    tests/unit/test_userprefs.py asserts for English only.

    python3 tools/i18n/pocheck.py
"""

import re
import sys

from gen import LANG_META
from po_lib import EN, LANG_DIR, parse_entries

QUOTED = re.compile(r'^(?:msgctxt |msgid |msgstr |)"(.*)"\s*$')
ESC_OK = set('"\\nt')
SPEC = re.compile(r"%[sd]")
BBCODE = re.compile(r"\[/?[A-Za-z][^\]]*\]")

# Help strings that quote another string's wording verbatim, so the user can
# match what the help says against what the settings list shows. Translate the
# two apart and the help names a control that is not on screen under that name.
# Nothing but this check ties them together -- tests/unit/test_userprefs.py
# asserts the #30794 pair for English only.
QUOTES_VERBATIM = {
    "#30794": ["#30618"],  # playback caveat -> the default-tracks setting label
    "#30607": ["#30609", "#30610"],  # lyrics help -> its own two option labels
}


def check_lines(path, errs):
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        errs.append("not valid UTF-8: %s" % exc)
        return None
    if "\r" in text:
        errs.append("carriage returns in file")
    if not text.endswith("\n"):
        errs.append("no trailing newline")
    for n, line in enumerate(text.splitlines(), 1):
        if not (line.startswith(("msgctxt ", "msgid ", "msgstr ", '"'))):
            continue
        m = QUOTED.match(line)
        if not m:
            errs.append("line %d: malformed quoted string" % n)
            continue
        body = m.group(1)
        i = 0
        while i < len(body):
            c = body[i]
            if c == "\\":
                if i + 1 >= len(body):
                    errs.append("line %d: trailing backslash" % n)
                    break
                if body[i + 1] not in ESC_OK:
                    errs.append("line %d: illegal escape \\%s" % (n, body[i + 1]))
                i += 2
                continue
            if c == '"':
                errs.append("line %d: unescaped quote" % n)
            i += 1
    return text


def check_entries(locale, entries, source, errs):
    if len(entries) != len(source):
        errs.append("entry count %d != source %d" % (len(entries), len(source)))
        return
    by_ctx = {}
    for got, want in zip(entries, source):
        by_ctx[want["ctx"]] = got["msgstr"]
        want_spec = SPEC.findall(want["msgid"])
        got_spec = SPEC.findall(got["msgstr"])
        if want_spec != got_spec:
            errs.append(
                "%s: format specifiers %s became %s"
                % (want["ctx"], want_spec or "[]", got_spec or "[]")
            )
        want_tags = sorted(BBCODE.findall(want["msgid"]))
        got_tags = sorted(BBCODE.findall(got["msgstr"]))
        if want_tags != got_tags:
            errs.append("%s: bbcode %s became %s" % (want["ctx"], want_tags, got_tags))
    for quoting, quoted_ids in QUOTES_VERBATIM.items():
        help_text = by_ctx.get(quoting)
        if not help_text:
            continue
        for quoted in quoted_ids:
            label = by_ctx.get(quoted)
            if label and ('"%s"' % label) not in help_text:
                errs.append(
                    "%s must quote %s verbatim (%r), the label it names"
                    % (quoting, quoted, label)
                )


def main(argv):
    source = parse_entries(EN)
    unknown = [a for a in argv if a not in LANG_META]
    if unknown:
        raise SystemExit("unknown locale(s): %s" % ", ".join(unknown))
    failed = 0
    checked = 0
    for locale in argv or list(LANG_META):
        path = LANG_DIR / ("resource.language.%s/strings.po" % locale)
        if not path.exists():
            print("SKIP %s: not generated yet" % locale)
            continue
        checked += 1
        errs = []
        if check_lines(path, errs) is not None:
            check_entries(locale, parse_entries(path), source, errs)
        if errs:
            failed += 1
            print("FAIL %s (%d issue(s))" % (locale, len(errs)))
            for line in errs[:20]:
                print("    %s" % line)
            if len(errs) > 20:
                print("    ... and %d more" % (len(errs) - 20))
        else:
            print("OK %s" % locale)
    if failed:
        raise SystemExit("%d locale(s) failed" % failed)
    print("ALL GOOD (%d locale(s))" % checked)


if __name__ == "__main__":
    main(sys.argv[1:])
