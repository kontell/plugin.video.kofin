"""L2 units for the artist-row rules in ``kodidb.music``.

Kodi reserves idArtist 1 for the [Missing Tag] blank artist and renders
whatever occupies it as "[Missing]". These run against pristine databases
built from the checked-in schema dumps, parameterized over both Kodi
generations, because the damage is only visible against the real schema:
idArtist is an INTEGER PRIMARY KEY, so what sqlite does with a NULL is the
whole bug.
"""

import sqlite3

import pytest

from kofin.sync.kodidb.music import Music
from tests.unit import kodifixtures

# Spelled out rather than imported from the module under test: these are
# Kodi's values (xbmc/music/Artist.h), so a test that took them from kofin
# would agree with itself no matter what kofin decided they were.
BLANKARTIST_ID = 1
BLANKARTIST_NAME = "[Missing Tag]"
BLANKARTIST_MBID = "Artist Tag Missing"


@pytest.fixture(params=[kodifixtures.MUSIC_VERSION, kodifixtures.PIERS_MUSIC_VERSION])
def musicdb(request, tmp_path):
    path = kodifixtures.create_music_db(
        str(tmp_path / "MyMusic.db"), version=request.param
    )
    conn = sqlite3.connect(path)
    yield conn.cursor(), conn
    conn.close()


def artists(cur):
    return dict(cur.execute("SELECT idArtist, strArtist FROM artist").fetchall())


def test_a_new_artist_never_takes_the_blank_artist_id(musicdb):
    """The insert used to pass the caller's id — None for a new artist — and
    let sqlite pick the rowid. On an artist table with no rows that is 1, so a
    real artist landed in Kodi's reserved slot and showed as [Missing]."""
    cur, _conn = musicdb
    cur.execute("DELETE FROM artist")  # the state a wiped music library is in

    db = Music(cur)
    returned = db.add_artist(None, "“Weird Al” Yankovic", "7746d775")

    rows = artists(cur)
    assert BLANKARTIST_ID not in rows, "took Kodi's [Missing Tag] id"
    assert rows[returned] == "“Weird Al” Yankovic"


def test_the_recorded_id_is_where_the_row_actually_landed(musicdb):
    """It returned create_entry()'s answer while sqlite assigned a different
    rowid, so kofin.db pointed at somebody else's artist."""
    cur, _conn = musicdb
    cur.execute("DELETE FROM artist")

    db = Music(cur)
    first = db.add_artist(None, "First Artist", "mbid-1")
    second = db.add_artist(None, "Second Artist", "mbid-2")

    rows = artists(cur)
    assert rows[first] == "First Artist"
    assert rows[second] == "Second Artist"
    assert first != second


def test_the_seeded_case_is_unchanged(musicdb):
    """Kodi as shipped: the blank artist is present, so the first real artist
    has always correctly landed on 2. This must not move."""
    cur, _conn = musicdb

    db = Music(cur)
    returned = db.add_artist(None, "First Artist", "mbid-1")

    assert returned == 2
    assert artists(cur)[BLANKARTIST_ID] == BLANKARTIST_NAME


def test_a_rename_actually_renames(musicdb):
    """get() passed the update its arguments backwards, so against an INTEGER
    idArtist the name matched nothing and it changed 0 rows — an artist
    renamed on the server silently never updated."""
    cur, _conn = musicdb
    db = Music(cur)
    artist_id = db.add_artist(None, "Old Name", "mbid-1")

    resolved = db.get(None, "New Name", "mbid-1")

    assert resolved == artist_id
    assert artists(cur)[artist_id] == "New Name"


def test_the_blank_artist_is_never_deleted(musicdb):
    """A reference pointing at it means something was mis-recorded earlier,
    not that Kodi's row should go."""
    cur, _conn = musicdb
    db = Music(cur)

    db.delete(BLANKARTIST_ID)

    assert artists(cur)[BLANKARTIST_ID] == BLANKARTIST_NAME


def test_real_artists_are_still_deleted(musicdb):
    cur, _conn = musicdb
    db = Music(cur)
    artist_id = db.add_artist(None, "Doomed", "mbid-1")

    db.delete(artist_id)

    assert artist_id not in artists(cur)


def test_a_missing_blank_artist_is_restored(musicdb):
    """Heals databases already damaged: Kodi renders whatever sits in the id
    as [Missing], so leaving a real artist there is not recoverable by
    re-syncing."""
    cur, _conn = musicdb
    cur.execute("DELETE FROM artist")
    db = Music(cur)

    assert db.ensure_blank_artist() is True

    row = cur.execute(
        "SELECT strArtist, strMusicBrainzArtistID FROM artist WHERE idArtist = ?",
        (BLANKARTIST_ID,),
    ).fetchone()
    assert row == (BLANKARTIST_NAME, BLANKARTIST_MBID)


def test_restoring_is_a_no_op_when_it_is_already_there(musicdb):
    cur, _conn = musicdb
    db = Music(cur)

    assert db.ensure_blank_artist() is False
    assert artists(cur)[BLANKARTIST_ID] == BLANKARTIST_NAME


def test_the_constants_match_kodi():
    """kofin's own names for the reserved row, checked against Kodi's."""
    from kofin.sync.kodidb import music

    assert music.BLANKARTIST_ID == BLANKARTIST_ID
    assert music.BLANKARTIST_NAME == BLANKARTIST_NAME
    assert music.BLANKARTIST_MBID == BLANKARTIST_MBID


def test_an_impostor_in_the_slot_is_reported_not_silently_accepted(
    musicdb, monkeypatch
):
    """The state both damaged devices are actually in: a row exists at the
    reserved id, it is just the wrong one. An existence check alone reports
    'nothing to do' here, which is how this went unnoticed."""
    cur, _conn = musicdb
    cur.execute("DELETE FROM artist")
    cur.execute(
        "INSERT INTO artist(idArtist, strArtist) VALUES (?, ?)",
        (BLANKARTIST_ID, "“Weird Al” Yankovic"),
    )
    db = Music(cur)

    # kofin logs through xbmc.log, not the logging module, so caplog is blind.
    warnings = []
    from kofin.sync.kodidb import music as music_mod

    monkeypatch.setattr(
        music_mod.LOG, "warning", lambda msg, *a: warnings.append(msg % a if a else msg)
    )

    assert db.ensure_blank_artist() is False

    assert any("blank-artist id" in w for w in warnings), warnings
    # Not evicted: relocating it means rewriting album_artist, song_artist and
    # discography plus kofin.db, which is not a startup job.
    assert artists(cur)[BLANKARTIST_ID] == "“Weird Al” Yankovic"
