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
    # Video codecs. 30150-30153 carry a parenthesised second name (AVC, HEVC,
    # Hi10P) that is part of the token, not a gloss.
    "#30150",  # H.264 (AVC)
    "#30151",  # H.264 10-bit (Hi10P)
    "#30152",  # H.265 (HEVC)
    "#30153",  # HEVC RExt (4:2:2/4:4:4)
    "#30154",  # AV1
    "#30155",  # MPEG-2
    "#30156",  # VP9
    "#30157",  # VC-1
    # Audio codecs. Dolby's own product spellings in 30161/30162.
    "#30158",  # AAC
    "#30159",  # MP2
    "#30160",  # MP3
    "#30161",  # AC3 (Dolby Digital)
    "#30162",  # E-AC3 (Dolby Digital Plus)
    "#30163",  # Opus
    "#30164",  # FLAC
    "#30165",  # DTS
    # HDR formats. 30170-30175 are deliberately absent: they pair the token
    # with "with ... fallback", and the descriptor needs translating.
    "#30166",  # HDR10
    "#30167",  # HLG
    "#30168",  # HDR10+
    "#30169",  # Dolby Vision
    # Resolutions
    "#30177",  # 720p
    "#30178",  # 1080p
    "#30179",  # 4K
    "#30323",  # 720p
    "#30324",  # 1080p
    "#30325",  # 2160p
    "#30604",  # 480p
    "#30605",  # 1440p
    # Bitrate spinner values: a number and an SI unit. Locales differ on the
    # capitalisation (MBit/s) and the decimal separator (0,5), but these sit in
    # a spinner beside the numbers they filter, where matching the rest of the
    # list matters more than matching local typography -- and it spares every
    # locale 23 translations that carry no meaning.
    *("#%d" % n for n in range(30183, 30206)),
    # Jellyfin feature name, used as a menu heading and a settings group label
    "#30550",  # SyncPlay
    "#30560",  # SyncPlay
    # The alphabet node. Its rows are the Latin letters A-Z in every language,
    # because that is what Jellyfin's NameStartsWith takes -- so a translated
    # label would name a range the menu does not offer.
    "#30818",  # A-Z
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
