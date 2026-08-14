"""Generate Kodi strings.po files for kofin from authored translations.

Reads:  tools/i18n/tr/_source.json   (ctx -> the English text each translation was made from)
        tools/i18n/tr/<locale>.json  (ctx -> translated msgstr)
Writes: resources/language/resource.language.<locale>/strings.po

PASSTHROUGH ids get msgstr == msgid automatically and are absent from the JSON.

The generator refuses to write when a locale is missing a key, has an empty
value, carries a key the source does not have, or when the English text has been
reworded since the translations were made. That last check is the one the
sibling add-on lacks, and is why its locales silently went two English revisions
stale (pvr.kofin commit bb5de9b).

Output is deterministic -- PO-Revision-Date is fixed -- so regenerating with no
data change produces no diff.

    python3 tools/i18n/gen.py                 # every locale
    python3 tools/i18n/gen.py de_de fr_fr     # named locales
    python3 tools/i18n/gen.py --snapshot      # restamp _source.json (see README)
"""

import json
import sys
from pathlib import Path

from classify import PASSTHROUGH
from po_lib import LANG_DIR, parse_entries, render

HERE = Path(__file__).parent
TR = HERE / "tr"
SOURCE = TR / "_source.json"

# locale -> (Language header value, Language-Team name, Plural-Forms)
LANG_META = {
    "fr_fr": ("fr_FR", "French", "nplurals=2; plural=(n > 1);"),
    "fr_ca": ("fr_CA", "French (Canada)", "nplurals=2; plural=(n > 1);"),
    "de_de": ("de_DE", "German", "nplurals=2; plural=(n != 1);"),
    "es_es": ("es_ES", "Spanish", "nplurals=2; plural=(n != 1);"),
    "es_mx": ("es_MX", "Spanish (Mexico)", "nplurals=2; plural=(n != 1);"),
    "it_it": ("it_IT", "Italian", "nplurals=2; plural=(n != 1);"),
    "pt_pt": ("pt_PT", "Portuguese", "nplurals=2; plural=(n != 1);"),
    "pt_br": ("pt_BR", "Portuguese (Brazil)", "nplurals=2; plural=(n > 1);"),
    "nl_nl": ("nl_NL", "Dutch", "nplurals=2; plural=(n != 1);"),
    "pl_pl": (
        "pl_PL",
        "Polish",
        "nplurals=3; plural=(n==1 ? 0 : n%10>=2 && n%10<=4 && "
        "(n%100<10 || n%100>=20) ? 1 : 2);",
    ),
    "ru_ru": (
        "ru_RU",
        "Russian",
        "nplurals=3; plural=(n%10==1 && n%100!=11 ? 0 : n%10>=2 && n%10<=4 && "
        "(n%100<10 || n%100>=20) ? 1 : 2);",
    ),
    "sv_se": ("sv_SE", "Swedish", "nplurals=2; plural=(n != 1);"),
    "uk_ua": (
        "uk_UA",
        "Ukrainian",
        "nplurals=3; plural=(n%10==1 && n%100!=11 ? 0 : n%10>=2 && n%10<=4 && "
        "(n%100<10 || n%100>=20) ? 1 : 2);",
    ),
    "cs_cz": (
        "cs_CZ",
        "Czech",
        "nplurals=3; plural=(n==1) ? 0 : (n>=2 && n<=4) ? 1 : 2;",
    ),
    "sk_sk": (
        "sk_SK",
        "Slovak",
        "nplurals=3; plural=(n==1) ? 0 : (n>=2 && n<=4) ? 1 : 2;",
    ),
    "da_dk": ("da_DK", "Danish", "nplurals=2; plural=(n != 1);"),
    "nb_no": ("nb_NO", "Norwegian Bokmal", "nplurals=2; plural=(n != 1);"),
    "fi_fi": ("fi_FI", "Finnish", "nplurals=2; plural=(n != 1);"),
    "el_gr": ("el_GR", "Greek", "nplurals=2; plural=(n != 1);"),
    "ro_ro": (
        "ro_RO",
        "Romanian",
        "nplurals=3; plural=(n==1 ? 0 : (n==0 || (n%100>0 && n%100<20)) ? 1 : 2);",
    ),
    "hu_hu": ("hu_HU", "Hungarian", "nplurals=2; plural=(n != 1);"),
    "ca_es": ("ca_ES", "Catalan", "nplurals=2; plural=(n != 1);"),
    "ja_jp": ("ja_JP", "Japanese", "nplurals=1; plural=0;"),
    "zh_cn": ("zh_CN", "Chinese (Simplified)", "nplurals=1; plural=0;"),
    "zh_tw": ("zh_TW", "Chinese (Traditional)", "nplurals=1; plural=0;"),
    "ko_kr": ("ko_KR", "Korean", "nplurals=1; plural=0;"),
}

HEADER_COMMENTS = (
    "# Kodi Media Center language file\n"
    "# Addon Name: Kofin\n"
    "# Addon id: plugin.video.kofin\n"
    "# Addon Provider: kontell\n"
    "# Note: machine-translated (LLM), pending native review.\n"
)

# Fixed so that regenerating without a data change is a no-op diff.
REVISION_DATE = "2026-08-14 00:00+0000"


def header(locale):
    po_lang, team, plural = LANG_META[locale]
    return (
        HEADER_COMMENTS + 'msgid ""\n'
        'msgstr ""\n'
        '"Project-Id-Version: plugin.video.kofin\\n"\n'
        '"Report-Msgid-Bugs-To: \\n"\n'
        '"PO-Revision-Date: %s\\n"\n'
        '"Last-Translator: Claude (machine translation) '
        '<noreply@anthropic.com>\\n"\n'
        '"Language-Team: %s\\n"\n'
        '"MIME-Version: 1.0\\n"\n'
        '"Content-Type: text/plain; charset=UTF-8\\n"\n'
        '"Content-Transfer-Encoding: 8bit\\n"\n'
        '"Language: %s\\n"\n'
        '"Plural-Forms: %s\\n"\n' % (REVISION_DATE, team, po_lang, plural)
    )


def load_tr(locale):
    p = TR / ("%s.json" % locale)
    if not p.exists():
        raise SystemExit("missing translation file: %s" % p)
    return json.loads(p.read_text(encoding="utf-8"))


def check_source(entries):
    """Fail when an English string was reworded after it was translated.

    gen.py cannot tell a stale translation from a good one by looking at it, so
    the English text every translation was made from is snapshotted in
    _source.json. Reword a msgid and this fires until the locales are redone and
    the snapshot is restamped with --snapshot.
    """
    if not SOURCE.exists():
        raise SystemExit(
            "missing %s -- run 'python3 tools/i18n/gen.py --snapshot' once the "
            "translations match the current English source" % SOURCE
        )
    snap = json.loads(SOURCE.read_text(encoding="utf-8"))
    current = {e["ctx"]: e["msgid"] for e in entries}
    changed = sorted(c for c, t in current.items() if c in snap and snap[c] != t)
    added = sorted(c for c in current if c not in snap)
    dropped = sorted(c for c in snap if c not in current)
    if changed or added or dropped:
        raise SystemExit(
            "English source has moved since the translations were made:\n"
            "  reworded: %s\n  added: %s\n  removed: %s\n"
            "Update every tr/<locale>.json, then restamp with --snapshot."
            % (changed or "none", added or "none", dropped or "none")
        )


def snapshot(entries):
    TR.mkdir(parents=True, exist_ok=True)
    data = {e["ctx"]: e["msgid"] for e in entries}
    SOURCE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("stamped %s (%d entries)" % (SOURCE, len(data)))


def generate(locale, entries):
    tr = load_tr(locale)
    known = {e["ctx"] for e in entries}
    missing, empty = [], []
    for e in entries:
        if e["ctx"] in PASSTHROUGH:
            continue
        if e["ctx"] not in tr:
            missing.append(e["ctx"])
        elif not str(tr[e["ctx"]]).strip():
            empty.append(e["ctx"])
    # A passthrough id in the JSON is dead weight the generator would ignore --
    # and a sign someone translated a token that must stay verbatim, so say so
    # rather than silently dropping it.
    extra = [k for k in tr if k not in known or k in PASSTHROUGH]
    if missing or empty or extra:
        raise SystemExit(
            "%s: missing=%s empty=%s unknown_keys=%s" % (locale, missing, empty, extra)
        )

    translations = {
        e["ctx"]: (e["msgid"] if e["ctx"] in PASSTHROUGH else tr[e["ctx"]])
        for e in entries
    }
    out = LANG_DIR / ("resource.language.%s/strings.po" % locale)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(header(locale), translations), encoding="utf-8")
    return out, len(entries)


def main(argv):
    entries = parse_entries()
    if argv and argv[0] == "--snapshot":
        snapshot(entries)
        return
    unknown = [a for a in argv if a not in LANG_META]
    if unknown:
        raise SystemExit("unknown locale(s): %s" % ", ".join(unknown))
    check_source(entries)
    for loc in argv or list(LANG_META):
        out, n = generate(loc, entries)
        print("wrote %s  (%d entries)" % (out, n))


if __name__ == "__main__":
    main(sys.argv[1:])
