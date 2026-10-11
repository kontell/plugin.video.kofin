"""Runs ON the device: which of Kodi's scan-time name lookups scan a table,
and what a NOCASE index does to each. Works on copies of the snapshot's
databases; nothing of Kodi's is touched.

    python3 index-survey.py
"""

import os
import shutil
import sqlite3
import time

N = os.environ.get("KOFIN_BENCH_DIR", "/storage/kofin-bench")
SNAP = N + "/snapshot-user/Database/"
TMP = N + "/tmp/"

# database -> (table, column, Kodi's statement with the name left out)
CASES = {
    "MyVideos149.db": [
        ("actor", "name", "select actor_id from actor where name like '%s'"),
        ("tag", "name", "select tag_id from tag where name like '%s'"),
        ("studio", "name", "select studio_id from studio where name like '%s'"),
        ("genre", "name", "select genre_id from genre where name like '%s'"),
        ("country", "name", "select country_id from country where name like '%s'"),
    ],
    "MyMusic84.db": [
        (
            "artist",
            "strArtist",
            "SELECT idArtist FROM artist WHERE strArtist LIKE '%s'"
            " AND strMusicBrainzArtistID IS NULL",
        ),
        (
            "album",
            "strAlbum",
            "SELECT idAlbum FROM album WHERE strArtistDisp LIKE 'Nobody'"
            " AND strAlbum LIKE '%s' AND strMusicBrainzAlbumID IS NULL",
        ),
        (
            "genre",
            "strGenre",
            "SELECT idGenre, strGenre FROM genre WHERE strGenre LIKE '%s'",
        ),
    ],
}


def quoted(name):
    return name.replace("'", "''")


def measure(conn, statement, names):
    """(ms a lookup, rows found, the plan) for Kodi's statement, the name
    inlined the way PrepareSQL inlines it."""
    plan = "; ".join(
        row[3]
        for row in conn.execute("EXPLAIN QUERY PLAN " + statement % quoted(names[0]))
    )
    began = time.time()
    found = 0
    for name in names:
        found += len(conn.execute(statement % quoted(name)).fetchall())
    return 1000 * (time.time() - began) / len(names), found, plan


def main():
    os.makedirs(TMP, exist_ok=True)
    for database, cases in CASES.items():
        copy = TMP + "survey-" + database
        shutil.copyfile(SNAP + database, copy)
        conn = sqlite3.connect(copy)
        print("==", database)
        for table, column, statement in cases:
            rows = conn.execute("SELECT count(*) FROM %s" % table).fetchone()[0]
            names = [
                row[0]
                for row in conn.execute(
                    "SELECT %s FROM %s WHERE %s IS NOT NULL"
                    " ORDER BY rowid DESC LIMIT 25" % (column, table, column)
                )
            ]
            before = measure(conn, statement, names)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_kofin_%s_nocase ON %s (%s COLLATE NOCASE)"
                % (table, table, column)
            )
            conn.commit()
            after = measure(conn, statement, names)
            print(
                "%-8s %6d rows  %6.2f ms -> %5.2f ms  (same rows: %s)"
                % (table, rows, before[0], after[0], before[1] == after[1])
            )
            print("      before:", before[2][:100])
            print("      after: ", after[2][:100])
        conn.close()
        os.remove(copy)


if __name__ == "__main__":
    main()
