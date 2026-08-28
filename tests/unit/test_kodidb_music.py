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


def paths(cur):
    return dict(cur.execute("SELECT idPath, strPath FROM path").fetchall())


def add_song(cur, song_id, path_id, title):
    cur.execute(
        "INSERT INTO song(idSong, idPath, strTitle, strFileName) VALUES (?, ?, ?, ?)",
        (song_id, path_id, title, "stream.flac?mode=play&id=x"),
    )


def test_deleting_a_song_takes_its_path_row(musicdb):
    """Kodi's music schema has no cascade, so a song removal that leaves the
    path row behind means every repair abandons one row per song."""
    cur, _conn = musicdb
    db = Music(cur)
    path_id = db.add_path("plugin://plugin.video.kofin/lib/song-1/")
    add_song(cur, 1, path_id, "Doomed")

    db.delete_song(1)

    assert paths(cur) == {}


def test_a_shared_path_row_outlives_one_of_its_songs(musicdb):
    """The cleanup is conditional on purpose: the row goes only when nothing
    else points at it."""
    cur, _conn = musicdb
    db = Music(cur)
    path_id = db.add_path("plugin://plugin.video.kofin/lib/")
    add_song(cur, 1, path_id, "First")
    add_song(cur, 2, path_id, "Second")

    db.delete_song(1)

    assert list(paths(cur)) == [path_id]

    db.delete_song(2)

    assert paths(cur) == {}


def test_a_path_backing_a_music_source_is_left_alone(musicdb):
    """A Kodi music source sharing the path is not kofin's to delete."""
    cur, _conn = musicdb
    db = Music(cur)
    path_id = db.add_path("/music/albums/doomed/")
    add_song(cur, 1, path_id, "Doomed")
    cur.execute(
        "INSERT INTO source_path(idSource, idPath, strPath) VALUES (?, ?, ?)",
        (1, path_id, "/music/albums/doomed/"),
    )

    db.delete_song(1)

    assert list(paths(cur)) == [path_id]


def test_deleting_a_song_that_is_not_there_is_harmless(musicdb):
    cur, _conn = musicdb
    db = Music(cur)
    path_id = db.add_path("plugin://plugin.video.kofin/lib/song-1/")
    add_song(cur, 1, path_id, "Doomed")

    db.delete_song(99)

    assert list(paths(cur)) == [path_id]


def test_pruning_removes_paths_kofin_abandoned(musicdb, tmp_path):
    """Heals databases leaked into before delete_song cleaned up: one dead row
    per song per repair ever run."""
    cur, _conn = musicdb
    db = Music(cur)
    live = db.add_path("plugin://plugin.video.kofin/lib/song-1/")
    add_song(cur, 1, live, "Kept")
    db.add_path("plugin://plugin.video.kofin/lib/song-2/")  # leaked plugin row
    db.add_path("http://server:8096/Audio/song-3/")  # leaked direct row
    db.add_path("https://server/Audio/song-4/")
    attach_mapping(db, tmp_path, [])

    assert db.prune_orphan_paths() == 3
    assert list(paths(cur)) == [live]


def test_pruning_leaves_paths_that_are_not_kofins(musicdb, tmp_path):
    """An orphaned path row is legitimate for Kodi -- its scanner keeps folder
    rows holding no songs -- so a blanket sweep is not ours to make."""
    cur, _conn = musicdb
    db = Music(cur)
    theirs = db.add_path("/mnt/music/an empty folder/")
    source = db.add_path("smb://nas/music/")
    cur.execute(
        "INSERT INTO source_path(idSource, idPath, strPath) VALUES (?, ?, ?)",
        (1, source, "smb://nas/music/"),
    )
    attach_mapping(db, tmp_path, [])

    assert db.prune_orphan_paths() == 0
    assert sorted(paths(cur)) == sorted([theirs, source])


def test_pruning_spares_a_kofin_path_a_source_still_claims(musicdb, tmp_path):
    cur, _conn = musicdb
    db = Music(cur)
    claimed = db.add_path("http://server:8096/Audio/song-1/")
    cur.execute(
        "INSERT INTO source_path(idSource, idPath, strPath) VALUES (?, ?, ?)",
        (1, claimed, "http://server:8096/Audio/song-1/"),
    )
    attach_mapping(db, tmp_path, [])

    assert db.prune_orphan_paths() == 0
    assert list(paths(cur)) == [claimed]


def test_pruning_spares_a_path_the_mapping_still_names(musicdb, tmp_path):
    """A downloaded song's server row: the repoint moved the song onto the
    album directory, so no song references the row -- but kofin.db does, and
    the restore puts the song back onto it. This sweep, run at every service
    start, is what emptied four albums on a Bravia (2026-08-28). A *video*
    mapping naming the same number is a MyVideos id and spares nothing."""
    cur, _conn = musicdb
    db = Music(cur)
    server = db.add_path("https://server/Audio/song-1/")
    local = db.add_path("/dl/Music/The Band/Greatest Hits/")
    add_song(cur, 1, local, "Downloaded")
    leaked = db.add_path("https://server/Audio/song-2/")
    attach_mapping(
        db,
        tmp_path,
        [("song", "lib-music", 1, server), ("movie", "lib-movies", 2, leaked)],
    )

    assert db.prune_orphan_paths() == 1
    assert sorted(paths(cur)) == sorted([server, local])


# -- the per-library source rows -----------------------------------------------


def sources(cur):
    return cur.execute(
        "SELECT idSource, strName, strMultipath FROM source ORDER BY idSource"
    ).fetchall()


def album_sources(cur):
    return sorted(cur.execute("SELECT idSource, idAlbum FROM album_source").fetchall())


def make_album(cur, title):
    """A bare album row -- enough for a link, without the writer stack."""
    cur.execute("SELECT coalesce(max(idAlbum), 0) + 1 FROM album")
    album_id = cur.fetchone()[0]
    cur.execute("INSERT INTO album(idAlbum, strAlbum) VALUES (?, ?)", (album_id, title))
    return album_id


def test_a_library_source_is_created_once_and_renamed_in_place(musicdb):
    """Keyed on the library id, not its name: keyed on the name a server-side
    rename writes a second source and leaves every album linked to the old
    one, so the renamed node matches nothing at all."""
    cur, _conn = musicdb
    db = Music(cur)

    source_id = db.ensure_source("lib-music", "Tunes")
    album_id = make_album(cur, "Greatest Hits")
    db.link_album_source(album_id, source_id)

    assert db.ensure_source("lib-music", "Anthems") == source_id
    assert sources(cur) == [
        (source_id, "Anthems", "plugin://plugin.video.kofin/lib-music/")
    ]
    assert album_sources(cur) == [(source_id, album_id)]


def test_linking_an_album_to_its_source_is_idempotent(musicdb):
    cur, _conn = musicdb
    db = Music(cur)
    source_id = db.ensure_source("lib-music", "Tunes")
    album_id = make_album(cur, "Greatest Hits")

    db.link_album_source(album_id, source_id)
    db.link_album_source(album_id, source_id)

    assert album_sources(cur) == [(source_id, album_id)]


def test_an_album_that_moved_library_loses_its_old_link(musicdb):
    """The album comes back on the same idAlbum (get_album matches on MBID),
    so without the unlink it sits in both libraries' nodes for good."""
    cur, _conn = musicdb
    db = Music(cur)
    first = db.ensure_source("lib-one", "Tunes")
    second = db.ensure_source("lib-two", "More")
    album_id = make_album(cur, "Greatest Hits")

    db.link_album_source(album_id, first)
    db.link_album_source(album_id, second)

    assert album_sources(cur) == [(second, album_id)]


def test_a_moved_album_keeps_a_link_the_user_owns(musicdb):
    """Scoped to kofin's own sources: an album the user also has scanned
    locally is not ours to unlink from their source."""
    cur, _conn = musicdb
    db = Music(cur)
    cur.execute(
        "INSERT INTO source(idSource, strName, strMultipath) VALUES (?, ?, ?)",
        (90, "My CDs", "smb://nas/music/"),
    )
    album_id = make_album(cur, "Greatest Hits")
    cur.execute(
        "INSERT INTO album_source(idSource, idAlbum) VALUES (?, ?)", (90, album_id)
    )

    source_id = db.ensure_source("lib-music", "Tunes")
    db.link_album_source(album_id, source_id)

    assert album_sources(cur) == sorted([(90, album_id), (source_id, album_id)])


def test_deleting_a_source_takes_its_album_links_with_it(musicdb):
    """tgrDeleteSource is Kodi's, so this is really asking whether the trigger
    fires under plain sqlite3 the way it does under Kodi -- the removal path
    leans on it rather than deleting the links itself."""
    cur, _conn = musicdb
    db = Music(cur)
    source_id = db.ensure_source("lib-music", "Tunes")
    db.link_album_source(make_album(cur, "Greatest Hits"), source_id)

    assert db.delete_source_for("lib-music") is True
    assert sources(cur) == []
    assert album_sources(cur) == []
    assert db.delete_source_for("lib-music") is False


def test_pruning_sources_spares_the_users_own(musicdb):
    cur, _conn = musicdb
    db = Music(cur)
    cur.execute(
        "INSERT INTO source(idSource, strName, strMultipath) VALUES (?, ?, ?)",
        (90, "My CDs", "smb://nas/music/"),
    )
    kept = db.ensure_source("lib-one", "Tunes")
    db.ensure_source("lib-two", "More")

    assert db.prune_sources(["lib-one"]) == 1
    assert sorted(row[0] for row in sources(cur)) == sorted([90, kept])


def attach_mapping(db, tmp_path, rows):
    """Give ``db`` a kofin mapping database holding ``rows``.

    Built with the real ``kofin_tables`` rather than a hand-written CREATE, so
    a column the reconcile reads cannot drift out from under these tests.
    ``rows`` are ``(media_type, media_folder, kodi_id)``, or with a fourth
    ``kodi_pathid`` for the path prune's mapping arm.
    """
    from kofin.sync.db import kofin_tables

    path = str(tmp_path / "kofin.db")
    mapping = sqlite3.connect(path)
    kofin_tables(mapping.cursor())
    mapping.executemany(
        "INSERT INTO jellyfin(jellyfin_id, media_type, media_folder, kodi_id, "
        "kodi_pathid) VALUES (?, ?, ?, ?, ?)",
        [
            ("%s-%s" % (row[0], row[2]), row[0], row[1], row[2], (row + (None,))[3])
            for row in rows
        ],
    )
    mapping.commit()
    mapping.close()
    db.cursor.connection.commit()  # ATTACH is refused inside a transaction
    db.attach_mapping(path)


def test_song_albums_reach_a_source_without_an_album_mapping(musicdb, tmp_path):
    """The singles path: a single's album is created on the fly by the writer
    and has no kofin.db reference, so walking the album mappings alone drops
    every single out of its library's nodes."""
    cur, _conn = musicdb
    db = Music(cur)
    source_id = db.ensure_source("lib-music", "Tunes")
    album_id = make_album(cur, "Singles")
    path_id = db.add_path("http://server:8096/Audio/song-1/")
    cur.execute(
        "INSERT INTO song(idSong, idAlbum, idPath, strTitle) VALUES (?, ?, ?, ?)",
        (7, album_id, path_id, "Opening Track"),
    )
    attach_mapping(db, tmp_path, [("song", "lib-music", 7)])

    db.link_library_song_albums("lib-music", source_id)

    assert album_sources(cur) == [(source_id, album_id)]


def test_a_library_links_every_mapped_album_in_one_statement(musicdb, tmp_path):
    """The album leg, which used to be two statements per album."""
    cur, _conn = musicdb
    db = Music(cur)
    source_id = db.ensure_source("lib-music", "Tunes")
    mine = [make_album(cur, "First"), make_album(cur, "Second")]
    theirs = make_album(cur, "Someone Else's")
    attach_mapping(
        db,
        tmp_path,
        [("album", "lib-music", album_id) for album_id in mine]
        + [("album", "lib-other", theirs)],
    )

    db.link_library_albums("lib-music", source_id)

    assert album_sources(cur) == sorted((source_id, album) for album in mine)


def test_a_library_move_unlinks_the_album_from_its_old_source(musicdb, tmp_path):
    """The half that makes a move stick: the album comes back on the same
    idAlbum, so without the unlink it sits in both libraries' nodes for good."""
    cur, _conn = musicdb
    db = Music(cur)
    old = db.ensure_source("lib-old", "Old")
    new = db.ensure_source("lib-new", "New")
    album_id = make_album(cur, "Moved")
    db.link_album_source(album_id, old)
    assert album_sources(cur) == [(old, album_id)]

    attach_mapping(db, tmp_path, [("album", "lib-new", album_id)])
    db.link_library_albums("lib-new", source_id=new)

    assert album_sources(cur) == [(new, album_id)]


def test_the_unlink_spares_a_source_kofin_does_not_own(musicdb, tmp_path):
    """A user's own scanned source shares this table and is never ours."""
    cur, _conn = musicdb
    db = Music(cur)
    mine = db.ensure_source("lib-music", "Tunes")
    cur.execute(
        "INSERT INTO source(idSource, strName, strMultipath) VALUES (?, ?, ?)",
        (90, "Ripped CDs", "/home/me/music/"),
    )
    album_id = make_album(cur, "Shared")
    cur.execute(
        "INSERT INTO album_source(idSource, idAlbum) VALUES (?, ?)", (90, album_id)
    )
    attach_mapping(db, tmp_path, [("album", "lib-music", album_id)])

    db.link_library_albums("lib-music", mine)

    assert album_sources(cur) == sorted([(90, album_id), (mine, album_id)])
