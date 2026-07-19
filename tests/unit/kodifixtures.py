"""Build pristine Kodi databases from the checked-in schema dumps (L2).

The schema SQL was dumped from untouched Kodi databases (``sqlite3 -readonly
<db> .schema``) — Omega's MyVideos131/MyMusic83 from the live box, Piers's
MyVideos146/MyMusic84 from the Bravia install — so the fixtures are exact by
construction. Seed files carry the rows Kodi itself writes at creation time
(videoversiontype for video, the default 'Artist' role for music); the
version row is inserted here because its number is the fixture's identity.
"""

import os
import sqlite3

FIXTURES = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "fixtures"))

# Omega defaults; the L2 writer suite parameterizes over these and Piers.
VIDEO_VERSION = 131
MUSIC_VERSION = 83

PIERS_VIDEO_VERSION = 146
PIERS_MUSIC_VERSION = 84


def _apply(conn: sqlite3.Connection, filename: str) -> None:
    with open(os.path.join(FIXTURES, filename), "r", encoding="utf-8") as infile:
        conn.executescript(infile.read())


def create_video_db(path: str, version: int = VIDEO_VERSION) -> str:
    conn = sqlite3.connect(path)
    try:
        _apply(conn, "myvideos%d.sql" % version)
        _apply(conn, "myvideos%d_seed.sql" % version)
        conn.execute(
            "INSERT INTO version (idVersion, iCompressCount) VALUES (?, 0)",
            (version,),
        )
        conn.commit()
    finally:
        conn.close()
    return path


def create_music_db(path: str, version: int = MUSIC_VERSION) -> str:
    conn = sqlite3.connect(path)
    try:
        _apply(conn, "mymusic%d.sql" % version)
        _apply(conn, "mymusic%d_seed.sql" % version)
        conn.execute(
            "INSERT INTO version (idVersion, iCompressCount) VALUES (?, 0)",
            (version,),
        )
        conn.commit()
    finally:
        conn.close()
    return path
