#!/usr/bin/env python3
"""Unwrap hard-wrapped Markdown.

Joins the physical lines of each paragraph or list item onto a single line so
the file soft-wraps in viewers, leaving structure alone: headings, table rows,
thematic breaks, blank lines, and fenced code blocks pass through verbatim.
A list item keeps its marker line and absorbs its indented continuation lines.

The rewrite is validated before saving: the whitespace-normalized token stream
of the output must equal the input's, so no text can be lost or reordered.

Usage: unwrap_md.py FILE [FILE ...]     (rewrites each file in place)
"""

import re
import sys

FENCE = re.compile(r"^\s*(```|~~~)")
LIST_ITEM = re.compile(r"^\s*(?:[*+-]|\d+[.)])\s+")
HEADING = re.compile(r"^\s{0,3}#{1,6}\s")
TABLE_ROW = re.compile(r"^\s*\|")
THEMATIC_BREAK = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")


def is_verbatim(line):
    """Lines that must keep their own physical line and never absorb others."""
    return bool(
        not line.strip()
        or HEADING.match(line)
        or TABLE_ROW.match(line)
        or THEMATIC_BREAK.match(line)
    )


def unwrap(text):
    out = []
    in_code = False
    can_join = False  # whether the last emitted line accepts continuations

    for line in text.splitlines():
        if FENCE.match(line):
            in_code = not in_code
            out.append(line)
            can_join = False
        elif in_code or is_verbatim(line):
            out.append(line)
            can_join = False
        elif can_join and not LIST_ITEM.match(line):
            out[-1] = out[-1] + " " + line.strip()
        else:
            # Keep a trailing double-space (Markdown hard break) if present.
            hard_break = line.rstrip("\n").endswith("  ")
            out.append(line.rstrip() + ("  " if hard_break else ""))
            can_join = not hard_break

    return "\n".join(out) + "\n"


def main(paths):
    for path in paths:
        with open(path, encoding="utf-8") as f:
            original = f.read()

        result = unwrap(original)

        if result.split() != original.split():
            sys.exit(f"{path}: token stream changed, refusing to write")

        with open(path, "w", encoding="utf-8") as f:
            f.write(result)

        before, after = original.count("\n"), result.count("\n")
        print(f"{path}: {before} -> {after} lines ({before - after} joined)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__.strip())
    main(sys.argv[1:])
