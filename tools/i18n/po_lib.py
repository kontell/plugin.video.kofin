"""PO parsing/escaping helpers for the kofin translation generator.

The English file at resources/language/resource.language.en_gb/strings.po is the
source of truth. Every other locale is rendered from it by substituting msgstr
values, so the two files stay structurally identical line for line.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]  # <repo>/tools/i18n/po_lib.py -> <repo>
LANG_DIR = REPO / "resources/language"
EN = LANG_DIR / "resource.language.en_gb/strings.po"

_quoted = re.compile(r'"((?:[^"\\]|\\.)*)"')


def po_unescape(s: str) -> str:
    return s.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")


def po_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def parse_entries(path: Path = EN):
    """Parse a Kodi strings.po. Return a list of {ctx, msgid, msgstr}.

    The PO header (the msgid "" block, which carries no msgctxt) is skipped.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    entries = []
    ctx = None
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("msgctxt"):
            ctx = po_unescape(_quoted.search(line).group(1))
            i += 1
        elif line.startswith("msgid"):
            msgid = po_unescape(_quoted.search(line).group(1))
            i += 1
            while i < n and lines[i].startswith('"'):
                msgid += po_unescape(_quoted.search(lines[i]).group(1))
                i += 1
            pending = ctx
            ctx = None
            msgstr = ""
            if i < n and lines[i].startswith("msgstr"):
                msgstr = po_unescape(_quoted.search(lines[i]).group(1))
                i += 1
                while i < n and lines[i].startswith('"'):
                    msgstr += po_unescape(_quoted.search(lines[i]).group(1))
                    i += 1
            if pending is not None:  # header msgid "" has no msgctxt -> skipped
                entries.append({"ctx": pending, "msgid": msgid, "msgstr": msgstr})
        else:
            i += 1
    return entries


def header_end(lines):
    """Index of the first line after the source file's header block.

    The header runs from the top through the msgid ""/msgstr "" pair and its
    continuation lines. Everything from the returned index onward is the body --
    starting with the blank line that separates them, so a rendered locale keeps
    the same spacing as the source.
    """
    seen_msgid = False
    for i, line in enumerate(lines):
        if line.startswith("msgid"):
            seen_msgid = True
        elif seen_msgid and not (line.startswith("msgstr") or line.startswith('"')):
            return i
    raise SystemExit("could not find the end of the PO header block")


def render(header: str, translations, path: Path = EN) -> str:
    """Render a locale file: the given header, then the source body with each
    msgstr replaced by translations[ctx].

    Copying the body line by line is what keeps blank lines and the source's
    deliberately non-ascending msgctxt order identical across locales.
    Rebuilding it from parsed entries would reorder them.

    Comments are the exception: they stay in en_gb and are not copied out. They
    are section markers and notes to whoever is *writing* a translation, and
    translations are written in tr/<locale>.json -- so in a generated file they
    are 74 lines nobody reads, repeated 26 times, that turn a four-line string
    addition into a hundred-line diff.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    out = header.splitlines()
    i = header_end(lines)
    ctx = None
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("msgctxt"):
            ctx = po_unescape(_quoted.search(line).group(1))
            out.append(line)
            i += 1
        elif line.startswith("msgid"):
            out.append(line)
            i += 1
            while i < n and lines[i].startswith('"'):
                out.append(lines[i])
                i += 1
        elif line.startswith("msgstr"):
            if ctx is None:
                raise SystemExit(
                    "msgstr with no preceding msgctxt at line %d" % (i + 1)
                )
            out.append('msgstr "%s"' % po_escape(translations[ctx]))
            i += 1
            while i < n and lines[i].startswith('"'):
                i += 1  # drop source continuation lines; we emit one line
            ctx = None
        elif line.startswith("#"):
            i += 1  # source-only: see the note above
        else:
            out.append(line)
            i += 1
    # Dropping a comment can leave the blank line that preceded it stranded
    # against the next one, so runs collapse to a single separator.
    body = []
    for line in out:
        if line == "" and body and body[-1] == "":
            continue
        body.append(line)
    return "\n".join(body) + "\n"


if __name__ == "__main__":
    es = parse_entries()
    print("entries: %d" % len(es))
    print("with a translation: %d" % sum(1 for e in es if e["msgstr"]))
