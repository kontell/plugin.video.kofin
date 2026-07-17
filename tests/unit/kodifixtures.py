"""Build pristine Kodi databases from the checked-in schema dumps (L2).

The schema SQL was dumped from the live box's untouched MyVideos131/MyMusic83
files (``sqlite3 -readonly <db> .schema``), so the fixtures are exact by
construction. Seed files carry the rows Kodi itself writes at creation time
(videoversiontype for video, the default 'Artist' role for music); the
version row is inserted here because its number is the fixture's identity.
"""

import os
import sqlite3

FIXTURES = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "fixtures"))

VIDEO_VERSION = 131
MUSIC_VERSION = 83


def _apply(conn: sqlite3.Connection, filename: str) -> None:
    with open(os.path.join(FIXTURES, filename), "r", encoding="utf-8") as infile:
        conn.executescript(infile.read())


def create_video_db(path: str) -> str:
    conn = sqlite3.connect(path)
    try:
        _apply(conn, "myvideos131.sql")
        _apply(conn, "myvideos131_seed.sql")
        conn.execute(
            "INSERT INTO version (idVersion, iCompressCount) VALUES (?, 0)",
            (VIDEO_VERSION,),
        )
        conn.commit()
    finally:
        conn.close()
    return path


def create_music_db(path: str) -> str:
    conn = sqlite3.connect(path)
    try:
        _apply(conn, "mymusic83.sql")
        _apply(conn, "mymusic83_seed.sql")
        conn.execute(
            "INSERT INTO version (idVersion, iCompressCount) VALUES (?, 0)",
            (MUSIC_VERSION,),
        )
        conn.commit()
    finally:
        conn.close()
    return path
