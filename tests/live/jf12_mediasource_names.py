#!/usr/bin/env python3
"""What Jellyfin puts in ``MediaSource.Name`` for a movie (audit A4-1).

Read-only. Lists movies with their media sources and says, per source,
whether the name equals the file's own stem. On jf12 v12 (2026-08-29) it was
the stem for 6 of 6 single-file movies — Jellyfin's ``GetMediaSourceName``
returns the file name without extension unless the item has local alternate
versions, in which case the folder-name prefix is stripped and the suffix
label is what remains. That is the rule H2's ``resolve_version_type``
applies: a name equal to the stem is no label at all.

    tests/live/jf12_mediasource_names.py [--limit N]
"""

import argparse
import os

from jf12api import Jf12


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=12)
    args = parser.parse_args()

    jf = Jf12()
    token = jf.login_admin()
    status, _, body = jf.call(
        "GET",
        "/Items?IncludeItemTypes=Movie&Recursive=true&Fields=MediaSources,Path&Limit=%d"
        % args.limit,
        None,
        token,
    )
    if status != 200:
        raise SystemExit("listing failed: %s" % status)

    equal = total = 0
    for item in body.get("Items", []):
        sources = item.get("MediaSources") or []
        for source in sources:
            stem = os.path.splitext(os.path.basename(source.get("Path") or ""))[0]
            same = (source.get("Name") or "") == stem
            equal += same
            total += 1
            print(
                "%-40s | %d source(s) | name==stem: %-5s | %r"
                % (
                    item["Name"][:40],
                    len(sources),
                    same,
                    (source.get("Name") or "")[:60],
                )
            )
    print("%d of %d sources named as their file stem" % (equal, total))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
