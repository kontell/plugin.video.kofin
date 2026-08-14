"""Split the English source into what gets translated and what is copied.

PASSTHROUGH is the do-not-translate allowlist: codec names, resolutions and
product names, where a translation is the defect rather than the feature. The
generator supplies msgstr == msgid for these, so they never appear in a
tr/<locale>.json and a translator is never asked about them.

Run this to get the worklist:

    python3 tools/i18n/classify.py            # counts to stderr, worklist to stdout
"""

import sys

from po_lib import parse_entries

# Codec, container and HDR tokens: brand names that stay as they are in every
# language. Entries that pair a token with descriptor words ("Dolby Vision with
# HDR10 fallback") are deliberately NOT here -- the descriptor needs translating.
PASSTHROUGH = {
    # video codecs
    "#30154",  # AV1
    "#30156",  # VP9
    # audio codecs
    "#30158",  # AAC
    "#30160",  # MP3
    "#30163",  # Opus
    "#30164",  # FLAC
    "#30165",  # DTS
    "#30166",  # HDR10
    # resolutions
    "#30177",  # 720p
    "#30178",  # 1080p
    "#30179",  # 4K
    "#30323",  # 720p
    "#30324",  # 1080p
    "#30325",  # 2160p
    "#30604",  # 480p
    "#30605",  # 1440p
    # Jellyfin feature name, used as a menu heading and a settings group label
    "#30550",  # SyncPlay
    "#30560",  # SyncPlay
}


def main():
    entries = parse_entries()
    ctxs = {e["ctx"] for e in entries}
    dangling = sorted(c for c in PASSTHROUGH if c not in ctxs)
    if dangling:
        raise SystemExit(
            "PASSTHROUGH ids no longer in the source (renumbered or removed): %s"
            % ", ".join(dangling)
        )
    work = [e for e in entries if e["ctx"] not in PASSTHROUGH]
    print(
        "# translatable: %d   passthrough: %d   total: %d"
        % (len(work), len(PASSTHROUGH), len(entries)),
        file=sys.stderr,
    )
    for e in work:
        print("%s\t%s" % (e["ctx"], e["msgid"]))


if __name__ == "__main__":
    main()
