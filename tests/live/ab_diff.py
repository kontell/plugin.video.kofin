#!/usr/bin/env python3
"""S2.2 A/B equivalence harness — the transplant proof.

Compares kofin's synced MyVideos against the old jellyfin-kodi addon's sync of
the *same* server library, movie by movie, keyed on IMDB id (stable across both
addons; Kodi's internal idMovie is not). Normalizes the two things that must
differ — the plugin path prefix (addon id) and per-user state — then diffs every
field the movie writer produces. Any surviving difference must be explainable
and intended.

Usage: python3 ab_diff.py [MASTER_DB] [KOFIN_DB]
Defaults to the live box's master profile and kofin-test profile.
"""

import os
import sqlite3
import sys
from collections import defaultdict

MASTER = os.path.expanduser("~/.kodi/userdata/Database/MyVideos131.db")
KOFIN = os.path.expanduser(
    "~/.kodi/userdata/profiles/kofin-test/Database/MyVideos131.db"
)

JELLYFIN_PLUGIN = "plugin://plugin.video.jellyfin/"
KOFIN_PLUGIN = "plugin://plugin.video.kofin/"


def norm_path(value):
    """Map either addon's plugin prefix to a common token, and strip the
    library-id / item-id path segments and query (they encode addon-internal
    ids and the differing library uuids, not content)."""
    if value is None:
        return None
    value = value.replace(JELLYFIN_PLUGIN, "@/").replace(KOFIN_PLUGIN, "@/")
    # A movie filename is "@/<libId>/?...&id=<item>&dbid=<n>&...": keep only
    # that it is a kofin/jellyfin plugin play URL, not the addon-specific ids.
    if value.startswith("@/"):
        return "@/<plugin-play-url>"
    return value


def norm_art(url):
    """Art URLs point at the same server + item + image type in both addons;
    the query string (tag/format/quality/maxheight) is addon-configurable, so
    compare only the base ``<server>/Items/<id>/Images/<type>[/<index>]``."""
    if not url:
        return url
    return url.split("?", 1)[0]


def movie_record(conn, kodi_id):
    """The normalized, user-state-free field set for one movie."""
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    m = cur.execute("SELECT * FROM movie WHERE idMovie=?", (kodi_id,)).fetchone()
    rec = {
        "title": m["c00"],
        "plot": m["c01"],
        "shortplot": m["c02"],
        "tagline": m["c03"],
        "votes": m["c04"],
        "writers_str": m["c06"],
        "year": m["c07"],
        "sorttitle": m["c10"],
        "runtime": m["c11"],
        "mpaa": m["c12"],
        "genre_str": m["c14"],
        "directors_str": m["c15"],
        "studio_str": m["c18"],
        "country_str": m["c21"],
        "premiered": m["premiered"],
    }

    def links(table, order=""):
        q = (
            "SELECT a.name FROM actor a JOIN %s l ON l.actor_id=a.actor_id "
            "WHERE l.media_id=? AND l.media_type='movie' %s" % (table, order)
        )
        return [r[0] for r in cur.execute(q, (kodi_id,))]

    rec["cast"] = links("actor_link", "ORDER BY l.cast_order")
    rec["directors"] = sorted(links("director_link"))
    rec["writers"] = sorted(links("writer_link"))
    rec["genres"] = sorted(
        r[0]
        for r in cur.execute(
            "SELECT g.name FROM genre g JOIN genre_link l ON l.genre_id=g.genre_id "
            "WHERE l.media_id=? AND l.media_type='movie'",
            (kodi_id,),
        )
    )
    rec["studios"] = sorted(
        r[0]
        for r in cur.execute(
            "SELECT s.name FROM studio s JOIN studio_link l ON l.studio_id=s.studio_id "
            "WHERE l.media_id=? AND l.media_type='movie'",
            (kodi_id,),
        )
    )
    rec["countries"] = sorted(
        r[0]
        for r in cur.execute(
            "SELECT c.name FROM country c "
            "JOIN country_link l ON l.country_id=c.country_id "
            "WHERE l.media_id=? AND l.media_type='movie'",
            (kodi_id,),
        )
    )
    rec["uniqueids"] = dict(
        cur.execute(
            "SELECT type, value FROM uniqueid "
            "WHERE media_id=? AND media_type='movie'",
            (kodi_id,),
        ).fetchall()
    )
    rec["art"] = {
        t: norm_art(u)
        for t, u in cur.execute(
            "SELECT type, url FROM art WHERE media_id=? AND media_type='movie'",
            (kodi_id,),
        ).fetchall()
    }
    # streamdetails, keyed by type; user-state-free
    file_id = m["idFile"]
    vids, auds, subs = [], [], []
    for s in cur.execute(
        "SELECT iStreamType,strVideoCodec,iVideoWidth,iVideoHeight,strHdrType,"
        "strAudioCodec,iAudioChannels,strAudioLanguage,strSubtitleLanguage "
        "FROM streamdetails WHERE idFile=?",
        (file_id,),
    ):
        if s[0] == 0:
            vids.append((s[1], s[2], s[3], s[4]))
        elif s[0] == 1:
            auds.append((s[5], s[6], s[7]))
        elif s[0] == 2:
            subs.append(s[8])
    rec["video_streams"] = vids
    rec["audio_streams"] = sorted(
        auds, key=lambda t: tuple("" if x is None else str(x) for x in t)
    )
    rec["subtitles"] = sorted(x for x in subs if x)
    rec["path"] = norm_path(
        cur.execute(
            "SELECT p.strPath FROM path p JOIN files f ON f.idPath=p.idPath "
            "WHERE f.idFile=?",
            (file_id,),
        ).fetchone()[0]
    )
    return rec


def imdb_map(conn):
    return {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT value, media_id FROM uniqueid "
            "WHERE media_type='movie' AND type='imdb' AND value LIKE 'tt%'"
        )
    }


def main():
    master_path = sys.argv[1] if len(sys.argv) > 1 else MASTER
    kofin_path = sys.argv[2] if len(sys.argv) > 2 else KOFIN
    m = sqlite3.connect("file:%s?mode=ro" % master_path, uri=True)
    k = sqlite3.connect("file:%s?mode=ro" % kofin_path, uri=True)

    mmap, kmap = imdb_map(m), imdb_map(k)
    shared = sorted(set(mmap) & set(kmap))
    print("A/B set: %d movies matched by imdb id" % len(shared))
    print(
        "  only master: %d | only kofin: %d"
        % (len(set(mmap) - set(kmap)), len(set(kmap) - set(mmap)))
    )

    diffs_by_field = defaultdict(list)
    examples = {}
    for imdb in shared:
        mr = movie_record(m, mmap[imdb])
        kr = movie_record(k, kmap[imdb])
        for field in mr:
            if mr[field] != kr[field]:
                diffs_by_field[field].append(imdb)
                if field not in examples:
                    examples[field] = (imdb, mr["title"], mr[field], kr[field])

    print("\n%-16s %8s   %s" % ("field", "# diff", "example (master vs kofin)"))
    print("-" * 78)
    if not diffs_by_field:
        print("  (identical across every field for all %d movies)" % len(shared))
    for field in sorted(diffs_by_field, key=lambda f: -len(diffs_by_field[f])):
        imdb, title, mv, kv = examples[field]
        n = len(diffs_by_field[field])
        print("%-16s %8d   %s" % (field, n, title))
        print("     master=%r" % (mv,))
        print("     kofin =%r" % (kv,))

    total_fields = len(shared) * 21
    total_diffs = sum(len(v) for v in diffs_by_field.values())
    print(
        "\n%d of %d field-comparisons differ (%.3f%%)"
        % (total_diffs, total_fields, 100.0 * total_diffs / total_fields)
    )


if __name__ == "__main__":
    main()
