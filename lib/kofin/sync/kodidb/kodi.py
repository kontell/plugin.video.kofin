# -*- coding: utf-8 -*-
"""Shared Kodi-database primitives (fork ``objects/kodi/kodi.py`` port).

Adaptation: the Kodi 19 streamdetails variant is gone — the schema gate
refuses anything below MyVideos131 (Kodi 21), so only the strHdrType arm
survives. Everything else is verbatim.
"""

from kofin.core.log import Logger
from kofin.sync.shims import values

from kofin.sync.kodidb import artwork
from kofin.sync.kodidb import queries as QU

LOG = Logger(__name__)


class Kodi(object):

    # Shared name -> person id cache, primed once per process. Writer instances
    # are created per work batch, so loading the actor table in __init__ re-read
    # the whole table for every batch and dominated batch startup on large
    # libraries. Filling it lazily instead is worse: get_person matches with
    # COLLATE NOCASE, which cannot use actor's BINARY index, so every miss scans
    # the table (43k rows, ~3.5 ms) -- tens of thousands of misses on a full
    # sync. Priming a shared cache once avoids both. Person rows are only ever
    # added by the addon, so cached ids stay valid for the process lifetime (a
    # database reset restarts Kodi).
    _people_cache: dict = {}
    _people_cache_primed = False

    def __init__(self):
        self.artwork = artwork.Artwork(self.cursor)

    @classmethod
    def reset_people_cache(cls):
        """Test hook: fixture databases start empty per test."""
        cls._people_cache = {}
        cls._people_cache_primed = False

    def _prime_people_cache(self):
        """Load the actor table into the shared cache, once per process."""
        if Kodi._people_cache_primed:
            return

        try:
            self.cursor.execute(QU.get_all_people)
        except Exception:
            # Failed to load the table. Has the table been created?
            pass
        else:
            Kodi._people_cache = dict(self.cursor.fetchall())

        Kodi._people_cache_primed = True

    def create_entry_path(self):
        self.cursor.execute(QU.create_path)

        return self.cursor.fetchone()[0] + 1

    def create_entry_file(self):
        self.cursor.execute(QU.create_file)

        return self.cursor.fetchone()[0] + 1

    def create_entry_genre(self):
        self.cursor.execute(QU.create_genre)

        return self.cursor.fetchone()[0] + 1

    def create_entry_studio(self):
        self.cursor.execute(QU.create_studio)

        return self.cursor.fetchone()[0] + 1

    def create_entry_bookmark(self):
        self.cursor.execute(QU.create_bookmark)

        return self.cursor.fetchone()[0] + 1

    def create_entry_tag(self):
        self.cursor.execute(QU.create_tag)

        return self.cursor.fetchone()[0] + 1

    def add_path(self, *args):
        path_id = self.get_path(*args)

        if path_id is None:

            path_id = self.create_entry_path()
            self.cursor.execute(QU.add_path, (path_id,) + args)

        return path_id

    def get_path(self, *args):

        try:
            self.cursor.execute(QU.get_path, args)

            return self.cursor.fetchone()[0]
        except TypeError:
            return

    def update_path_parent_id(self, path_id, parent_path_id):
        self.cursor.execute(QU.update_path_parent_id, (parent_path_id, path_id))

    def update_path(self, *args):
        self.cursor.execute(QU.update_path, args)

    def remove_path(self, *args):
        self.cursor.execute(QU.delete_path, args)

    def add_file(self, filename, path_id):
        # NOTE: callers pass (PathId, Filename) against this (filename,
        # path_id) signature — a historic double swap. get_file binds
        # correctly because of it; the INSERT writes swapped columns that
        # every caller immediately corrects via update_file. Proven fork
        # behavior — do not "fix" one half of it.
        try:
            self.cursor.execute(
                QU.get_file,
                (
                    filename,
                    path_id,
                ),
            )
            file_id = self.cursor.fetchone()[0]
        except TypeError:

            file_id = self.create_entry_file()
            self.cursor.execute(QU.add_file, (file_id, path_id, filename))

        return file_id

    def update_file(self, *args):
        self.cursor.execute(QU.update_file, args)

    def remove_file(self, path, *args):
        path_id = self.get_path(path)

        if path_id is not None:
            self.cursor.execute(QU.delete_file_by_path, (path_id,) + args)

    def get_filename(self, *args):

        try:
            self.cursor.execute(QU.get_filename, args)

            return self.cursor.fetchone()[0]
        except TypeError:
            return ""

    def add_people(self, people, *args):

        def add_thumbnail(person_id, person, person_type):

            if person["imageurl"]:

                art = person_type.lower()
                if "writing" in art:
                    art = "writer"
                elif "gueststar" in art:
                    # Kodi doesn't differentiate gueststars from actors like Jellyfin does.
                    art = "actor"

                self.artwork.update(person["imageurl"], person_id, art, "thumb")

        cast_order = 1

        bulk_updates = {}

        # Define the types of people considered "actors" in a "cast".
        cast_types = ["Actor", "GuestStar"]

        for person in people:
            person_id = self.get_person(person["Name"])
            person_type = person["Type"]

            if person_type in cast_types:
                sql = QU.update_actor
                role = person.get("Role")
                bulk_updates.setdefault(sql, []).append(
                    (person_id,)
                    + args
                    + (
                        role,
                        cast_order,
                    )
                )
                cast_order += 1

            elif person_type == "Director":
                sql = QU.update_link.replace("{LinkType}", "director_link")
                bulk_updates.setdefault(sql, []).append((person_id,) + args)

            elif person_type == "Writer":
                sql = QU.update_link.replace("{LinkType}", "writer_link")
                bulk_updates.setdefault(sql, []).append((person_id,) + args)

            elif person_type == "Artist":
                sql = QU.insert_link_if_not_exists.replace("{LinkType}", "actor_link")
                bulk_updates.setdefault(sql, []).append(
                    (person_id,) + args + (person_id,) + args
                )

            add_thumbnail(person_id, person, person_type)

        for sql, parameters in bulk_updates.items():
            self.cursor.executemany(sql, parameters)

    def add_person(self, *args):
        self.cursor.execute(QU.add_person, args)
        return self.cursor.lastrowid

    def _get_person(self, name):
        """Retrieve person from the database, or add them if they don't exist"""
        resp = self.cursor.execute(QU.get_person, (name,)).fetchone()
        if resp is not None:
            return resp[0]
        else:
            return self.add_person(name)

    def get_person(self, name):
        """Retrieve person from cache, else forward to db query"""
        self._prime_people_cache()

        if name in self._people_cache:
            return self._people_cache[name]
        else:
            person_id = self._get_person(name)
            self._people_cache[name] = person_id
            return person_id

    def add_genres(self, genres, *args):
        """Delete current genres first for clean slate."""
        self.cursor.execute(QU.delete_genres, args)

        for genre in genres:
            self.cursor.execute(QU.update_genres, (self.get_genre(genre),) + args)

    def add_genre(self, *args):

        genre_id = self.create_entry_genre()
        self.cursor.execute(QU.add_genre, (genre_id,) + args)

        return genre_id

    def get_genre(self, *args):

        try:
            self.cursor.execute(QU.get_genre, args)

            return self.cursor.fetchone()[0]
        except TypeError:
            return self.add_genre(*args)

    def add_studios(self, studios, *args):

        for studio in studios:

            studio_id = self.get_studio(studio)
            self.cursor.execute(QU.update_studios, (studio_id,) + args)

    def add_studio(self, *args):

        studio_id = self.create_entry_studio()
        self.cursor.execute(QU.add_studio, (studio_id,) + args)

        return studio_id

    def get_studio(self, *args):

        try:
            self.cursor.execute(QU.get_studio, args)

            return self.cursor.fetchone()[0]
        except TypeError:
            return self.add_studio(*args)

    def add_streams(self, file_id, streams, runtime):
        """First remove any existing entries
        Then re-add video, audio and subtitles.
        """
        self.cursor.execute(QU.delete_streams, (file_id,))

        if streams:
            for track in streams["video"]:

                track["FileId"] = file_id
                track["Runtime"] = runtime
                self.add_stream_video(*values(track, QU.add_stream_video_obj))

            for track in streams["audio"]:

                track["FileId"] = file_id
                self.add_stream_audio(*values(track, QU.add_stream_audio_obj))

            for track in streams["subtitle"]:
                self.add_stream_sub(
                    *values(
                        {"language": track, "FileId": file_id}, QU.add_stream_sub_obj
                    )
                )

    def add_stream_video(self, *args):
        self.cursor.execute(QU.add_stream_video, args)

    def add_stream_audio(self, *args):
        self.cursor.execute(QU.add_stream_audio, args)

    def add_stream_sub(self, *args):
        self.cursor.execute(QU.add_stream_sub, args)

    def get_video_duration(self, file_id):
        """Duration in seconds recorded for the file's video stream, if any."""
        self.cursor.execute(QU.get_video_duration, (file_id,))
        row = self.cursor.fetchone()

        return row[0] if row and row[0] else 0

    def resolve_runtime(self, runtime_ticks, file_id):
        """Bookmark total time in seconds. Direct userdata payloads carry no
        RunTimeTicks; fall back to the duration recorded with the streams.
        """
        if runtime_ticks is None:
            return self.get_video_duration(file_id)

        return round(float(runtime_ticks / 10000000.0), 6)

    def add_playstate(self, file_id, playcount, date_played, resume, *args):
        """Delete the existing resume point.
        Set the watched count.
        """
        self.cursor.execute(QU.delete_bookmark, (file_id,))
        self.set_playcount(playcount, date_played, file_id)

        if resume:

            bookmark_id = self.create_entry_bookmark()
            self.cursor.execute(
                QU.add_bookmark,
                (
                    bookmark_id,
                    file_id,
                    resume,
                )
                + args,
            )

    def set_playcount(self, *args):
        self.cursor.execute(QU.update_playcount, args)

    def add_tags(self, tags, *args):
        self.cursor.execute(QU.delete_tags, args)

        for tag in tags:
            self.get_tag(tag, *args)

    def add_tag(self, *args):

        tag_id = self.create_entry_tag()
        self.cursor.execute(QU.add_tag, (tag_id,) + args)

        return tag_id

    def get_tag(self, tag, *args):

        try:
            self.cursor.execute(QU.get_tag, (tag,))
            tag_id = self.cursor.fetchone()[0]
        except TypeError:
            tag_id = self.add_tag(tag)

        self.cursor.execute(QU.update_tag, (tag_id,) + args)

        return tag_id

    def remove_tag(self, tag, *args):

        try:
            self.cursor.execute(QU.get_tag, (tag,))
            tag_id = self.cursor.fetchone()[0]
        except TypeError:
            return

        self.cursor.execute(QU.delete_tag, (tag_id,) + args)
