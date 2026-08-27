#!/usr/bin/env python3
"""Keyed before/after comparison of what kofin wrote to a Kodi profile.

The live analogue of the L2 suite's byte-identical idempotency dump
(docs/sync-refactor-phase1-plan.md §3). Kodi's own ids renumber on a Repair,
so nothing here is keyed on them: every record is keyed on the *server's* id
through kofin.db's mapping table, and every Kodi id inside a row is replaced
by the thing it points at (a rating row by its type, a uniqueid row by its
type+value, a path row by its string, a set by its name, a show by its
title). Per-user and per-run columns are dropped (playcount, last played,
bookmarks, the MyMusic trigger clocks), everything the writers derive from
the server is kept.

    dump_diff.py snapshot OUT.json --kodi-home DIR [--profile NAME]
    dump_diff.py diff BEFORE.json AFTER.json [--show N]

``snapshot`` copies each database together with its -wal/-shm into a temp
directory before reading (a .db copied on its own hides every row still in
the WAL) and works while Kodi is running. ``diff`` exits 1 when anything
differs, and prints, per media type, the keys only one side has and the
first N changed fields.
"""

import argparse
import glob
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile

DBID = re.compile(r"dbid=\d+")

# Columns that are Kodi ids or per-user / per-run state, dropped from every
# row they appear in. Pointer columns (c05/c09 etc.) are handled per table.
DROP_ALWAYS = {
    "idMovie",
    "idFile",
    "idPath",
    "idSet",
    "idShow",
    "idSeason",
    "idEpisode",
    "idMVideo",
    "idParentPath",
    "idAlbum",
    "idArtist",
    "idSong",
    "idGenre",
    "idSource",
    "idRating",
    "uniqueid_id",
    "rating_id",
    "art_id",
    "playCount",
    "lastPlayed",
    "iTimesPlayed",
    "lastplayed",
    "userrating",
    "dateNew",
    "dateModified",
    "lastScraped",
    "iStartOffset",
    "iEndOffset",
    "idBookmark",
}

VIDEO_POINTERS = {
    # table: {column: what it points at}
    "movie": {"c05": "rating", "c09": "uniqueid", "c23": "drop"},
    "tvshow": {"c04": "rating", "c12": "uniqueid", "c17": "drop"},
    "episode": {"c03": "rating", "c20": "uniqueid", "c19": "drop"},
    "musicvideo": {"c14": "drop"},
}


def rpc_free_copy(paths, tmp):
    out = {}
    for path in paths:
        if not os.path.exists(path):
            continue
        for suffix in ("", "-wal", "-shm"):
            src = path + suffix
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(tmp, os.path.basename(src)))
        out[os.path.basename(path)] = os.path.join(tmp, os.path.basename(path))
    return out


def newest(pattern):
    files = sorted(
        glob.glob(pattern), key=lambda p: int(re.search(r"(\d+)\.db$", p).group(1))
    )
    return files[-1] if files else None


def rows(conn, sql, params=()):
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    return [dict(r) for r in cur.execute(sql, params)]


def norm(value):
    if isinstance(value, str):
        return DBID.sub("dbid=#", value)
    return value


def strip(row, drop=()):
    return {
        k: norm(v) for k, v in row.items() if k not in DROP_ALWAYS and k not in drop
    }


class Video:
    def __init__(self, conn):
        self.c = conn

    def path_str(self, id_path):
        r = rows(self.c, "SELECT strPath FROM path WHERE idPath=?", (id_path,))
        return r[0]["strPath"] if r else None

    def path_row(self, id_path):
        r = rows(
            self.c,
            "SELECT strPath, strContent, strScraper, scanRecursive, useFolderNames, noUpdate, exclude FROM path WHERE idPath=?",
            (id_path,),
        )
        return strip(r[0]) if r else None

    def file_block(self, id_file):
        f = rows(
            self.c,
            "SELECT idPath, strFilename, dateAdded FROM files WHERE idFile=?",
            (id_file,),
        )
        if not f:
            return None
        f = f[0]
        streams = rows(
            self.c,
            "SELECT * FROM streamdetails WHERE idFile=? ORDER BY iStreamType, rowid",
            (id_file,),
        )
        return {
            "file": {
                "strFilename": norm(f["strFilename"]),
                "dateAdded": f["dateAdded"],
            },
            "path": self.path_row(f["idPath"]),
            "streams": [strip(s) for s in streams],
        }

    def links(self, media_type, media_id):
        out = {}
        for table, name_table, key in (
            ("genre_link", "genre", "genre_id"),
            ("studio_link", "studio", "studio_id"),
            ("country_link", "country", "country_id"),
            ("tag_link", "tag", "tag_id"),
        ):
            out[table] = sorted(
                r["name"]
                for r in rows(
                    self.c,
                    f"SELECT n.name FROM {table} l JOIN {name_table} n ON n.{key}=l.{key} WHERE l.media_id=? AND l.media_type=?",
                    (media_id, media_type),
                )
            )
        for table in ("actor_link", "director_link", "writer_link"):
            cols = "a.name, l.role, l.cast_order" if table == "actor_link" else "a.name"
            order = "l.cast_order, a.name" if table == "actor_link" else "a.name"
            out[table] = [
                strip(r)
                for r in rows(
                    self.c,
                    f"SELECT {cols} FROM {table} l JOIN actor a ON a.actor_id=l.actor_id WHERE l.media_id=? AND l.media_type=? ORDER BY {order}",
                    (media_id, media_type),
                )
            ]
        out["art"] = {
            r["type"]: r["url"].split("?", 1)[0]
            for r in rows(
                self.c,
                "SELECT type, url FROM art WHERE media_id=? AND media_type=?",
                (media_id, media_type),
            )
        }
        out["uniqueid"] = sorted(
            (r["type"], r["value"])
            for r in rows(
                self.c,
                "SELECT type, value FROM uniqueid WHERE media_id=? AND media_type=?",
                (media_id, media_type),
            )
        )
        out["rating"] = sorted(
            (r["rating_type"], r["rating"], r["votes"])
            for r in rows(
                self.c,
                "SELECT rating_type, rating, votes FROM rating WHERE media_id=? AND media_type=?",
                (media_id, media_type),
            )
        )
        return out

    def pointer(self, kind, value):
        if value in (None, ""):
            return None
        try:
            ident = int(value)
        except (TypeError, ValueError):
            return norm(value)
        if kind == "rating":
            r = rows(
                self.c, "SELECT rating_type FROM rating WHERE rating_id=?", (ident,)
            )
            return ("rating->", r[0]["rating_type"] if r else "MISSING")
        if kind == "uniqueid":
            r = rows(
                self.c, "SELECT type, value FROM uniqueid WHERE uniqueid_id=?", (ident,)
            )
            return ("uniqueid->", (r[0]["type"], r[0]["value"]) if r else "MISSING")
        return None

    def main_row(self, table, id_col, kodi_id):
        r = rows(self.c, f"SELECT * FROM {table} WHERE {id_col}=?", (kodi_id,))
        if not r:
            return None
        row = r[0]
        pointers = VIDEO_POINTERS.get(table, {})
        out = strip(row, drop=[c for c, k in pointers.items() if k == "drop"])
        for col, kind in pointers.items():
            if kind != "drop" and col in row:
                out[col] = self.pointer(kind, row[col])
        return out, row

    def show_title(self, id_show):
        r = rows(self.c, "SELECT c00 FROM tvshow WHERE idShow=?", (id_show,))
        return r[0]["c00"] if r else None

    def record(self, media_type, kodi_id, kodi_fileid):
        if media_type == "movie":
            got = self.main_row("movie", "idMovie", kodi_id)
            if not got:
                return None
            out, raw = got
            sets = (
                rows(
                    self.c, "SELECT strSet FROM sets WHERE idSet=?", (raw.get("idSet"),)
                )
                if raw.get("idSet")
                else []
            )
            out["set"] = sets[0]["strSet"] if sets else None
            out.update(self.links("movie", kodi_id))
            out["fileblock"] = self.file_block(raw["idFile"])
            out["versions"] = [
                strip(v, ("idFile", "idMedia")) | {"file": self.file_block(v["idFile"])}
                for v in rows(
                    self.c,
                    "SELECT * FROM videoversion WHERE idMedia=? AND media_type='movie' ORDER BY idFile",
                    (kodi_id,),
                )
            ]
            return out
        if media_type == "set":
            r = rows(
                self.c, "SELECT strSet, strOverview FROM sets WHERE idSet=?", (kodi_id,)
            )
            if not r:
                return None
            out = strip(r[0])
            out["members"] = sorted(
                m["c00"]
                for m in rows(self.c, "SELECT c00 FROM movie WHERE idSet=?", (kodi_id,))
            )
            out.update(self.links("set", kodi_id))
            return out
        if media_type == "tvshow":
            got = self.main_row("tvshow", "idShow", kodi_id)
            if not got:
                return None
            out, raw = got
            out["paths"] = sorted(
                self.path_row(p["idPath"]) and self.path_row(p["idPath"])["strPath"]
                for p in rows(
                    self.c,
                    "SELECT idPath FROM tvshowlinkpath WHERE idShow=?",
                    (kodi_id,),
                )
            )
            out["pathrows"] = [
                self.path_row(p["idPath"])
                for p in rows(
                    self.c,
                    "SELECT idPath FROM tvshowlinkpath WHERE idShow=? ORDER BY idPath",
                    (kodi_id,),
                )
            ]
            # A season row may carry a NULL name (Kodi fills it from the number
            # at render time), so the sort key cannot be the bare tuple.
            out["seasons"] = sorted(
                (s["season"], s["name"] or "")
                for s in rows(
                    self.c,
                    "SELECT season, name FROM seasons WHERE idShow=?",
                    (kodi_id,),
                )
            )
            out.update(self.links("tvshow", kodi_id))
            return out
        if media_type == "season":
            r = rows(
                self.c,
                "SELECT idShow, season, name FROM seasons WHERE idSeason=?",
                (kodi_id,),
            )
            if not r:
                return None
            out = strip(r[0])
            out["show"] = self.show_title(r[0]["idShow"])
            out.update(self.links("season", kodi_id))
            return out
        if media_type == "episode":
            got = self.main_row("episode", "idEpisode", kodi_id)
            if not got:
                return None
            out, raw = got
            out["show"] = self.show_title(raw["idShow"])
            s = rows(
                self.c,
                "SELECT season FROM seasons WHERE idSeason=?",
                (raw["idSeason"],),
            )
            out["season_number"] = s[0]["season"] if s else None
            out.update(self.links("episode", kodi_id))
            out["fileblock"] = self.file_block(raw["idFile"])
            return out
        if media_type == "musicvideo":
            got = self.main_row("musicvideo", "idMVideo", kodi_id)
            if not got:
                return None
            out, raw = got
            out.update(self.links("musicvideo", kodi_id))
            out["fileblock"] = self.file_block(raw["idFile"])
            return out
        return {"unhandled": media_type}


class Music:
    def __init__(self, conn):
        self.c = conn

    def art(self, media_type, media_id):
        return {
            r["type"]: r["url"].split("?", 1)[0]
            for r in rows(
                self.c,
                "SELECT type, url FROM art WHERE media_id=? AND media_type=?",
                (media_id, media_type),
            )
        }

    def record(self, media_type, kodi_id):
        if media_type == "artist":
            r = rows(self.c, "SELECT * FROM artist WHERE idArtist=?", (kodi_id,))
            if not r:
                return None
            out = strip(r[0])
            out["art"] = self.art("artist", kodi_id)
            return out
        if media_type == "album":
            r = rows(self.c, "SELECT * FROM album WHERE idAlbum=?", (kodi_id,))
            if not r:
                return None
            out = strip(r[0])
            out["artists"] = [
                strip(a)
                for a in rows(
                    self.c,
                    "SELECT ar.strArtist, aa.strArtist AS credit, aa.iOrder FROM album_artist aa JOIN artist ar ON ar.idArtist=aa.idArtist WHERE aa.idAlbum=? ORDER BY aa.iOrder",
                    (kodi_id,),
                )
            ]
            out["sources"] = sorted(
                s["strName"]
                for s in rows(
                    self.c,
                    "SELECT s.strName FROM album_source al JOIN source s ON s.idSource=al.idSource WHERE al.idAlbum=?",
                    (kodi_id,),
                )
            )
            out["art"] = self.art("album", kodi_id)
            return out
        if media_type == "song":
            r = rows(self.c, "SELECT * FROM song WHERE idSong=?", (kodi_id,))
            if not r:
                return None
            raw = r[0]
            out = strip(raw)
            a = rows(
                self.c, "SELECT strAlbum FROM album WHERE idAlbum=?", (raw["idAlbum"],)
            )
            out["album"] = a[0]["strAlbum"] if a else None
            p = rows(
                self.c, "SELECT strPath FROM path WHERE idPath=?", (raw["idPath"],)
            )
            out["path"] = norm(p[0]["strPath"]) if p else None
            out["artists"] = [
                strip(x)
                for x in rows(
                    self.c,
                    "SELECT ar.strArtist, sa.strArtist AS credit, sa.idRole, sa.iOrder FROM song_artist sa JOIN artist ar ON ar.idArtist=sa.idArtist WHERE sa.idSong=? ORDER BY sa.idRole, sa.iOrder",
                    (kodi_id,),
                )
            ]
            out["genres"] = sorted(
                g["strGenre"]
                for g in rows(
                    self.c,
                    "SELECT g.strGenre FROM song_genre sg JOIN genre g ON g.idGenre=sg.idGenre WHERE sg.idSong=?",
                    (kodi_id,),
                )
            )
            out["art"] = self.art("song", kodi_id)
            return out
        return {"unhandled": media_type}


VIDEO_TYPES = {"movie", "set", "tvshow", "season", "episode", "musicvideo"}
MUSIC_TYPES = {"artist", "album", "song"}


def snapshot(args):
    home = os.path.expanduser(args.kodi_home)
    if args.profile:
        base = os.path.join(home, "userdata", "profiles", args.profile)
    else:
        base = os.path.join(home, "userdata")
    kofin_db = os.path.join(base, "addon_data", "plugin.video.kofin", "kofin.db")
    video_db = newest(os.path.join(base, "Database", "MyVideos*.db"))
    music_db = newest(os.path.join(base, "Database", "MyMusic*.db"))
    tmp = tempfile.mkdtemp(prefix="kofin-dump-")
    try:
        copies = rpc_free_copy([p for p in (kofin_db, video_db, music_db) if p], tmp)
        kofin = sqlite3.connect(copies[os.path.basename(kofin_db)])
        video = (
            Video(sqlite3.connect(copies[os.path.basename(video_db)]))
            if video_db and os.path.basename(video_db) in copies
            else None
        )
        music = (
            Music(sqlite3.connect(copies[os.path.basename(music_db)]))
            if music_db and os.path.basename(music_db) in copies
            else None
        )
        records = {}
        counts = {}
        mapping = rows(
            kofin,
            "SELECT jellyfin_id, media_folder, jellyfin_type, media_type, kodi_id, kodi_fileid, checksum, jellyfin_parent_id FROM jellyfin ORDER BY media_type, jellyfin_id",
        )
        for m in mapping:
            mt = m["media_type"]
            entry = {
                "ref": {
                    k: m[k]
                    for k in (
                        "media_folder",
                        "jellyfin_type",
                        "checksum",
                        "jellyfin_parent_id",
                    )
                }
            }
            if mt in VIDEO_TYPES and video is not None:
                entry["kodi"] = video.record(mt, m["kodi_id"], m["kodi_fileid"])
            elif mt in MUSIC_TYPES and music is not None:
                entry["kodi"] = music.record(mt, m["kodi_id"])
            else:
                entry["kodi"] = {"unread": mt}
            records.setdefault(mt, {})[m["jellyfin_id"]] = entry
            counts[mt] = counts.get(mt, 0) + 1
        out = {
            "meta": {
                "kodi_home": home,
                "profile": args.profile,
                "video_db": os.path.basename(video_db) if video_db else None,
                "music_db": os.path.basename(music_db) if music_db else None,
                "counts": counts,
            },
            "records": records,
        }
        with open(args.out, "w") as handle:
            json.dump(out, handle, sort_keys=True, indent=0, default=str)
        print("snapshot:", json.dumps(out["meta"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def flatten(value, prefix=""):
    if isinstance(value, dict):
        for k, v in value.items():
            yield from flatten(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(value, list):
        yield prefix, json.dumps(value, sort_keys=True, default=str)
    else:
        yield prefix, value


def diff(args):
    before = json.load(open(args.before))["records"]
    after = json.load(open(args.after))["records"]
    total_changed = total_only = 0
    for mt in sorted(set(before) | set(after)):
        b = before.get(mt, {})
        a = after.get(mt, {})
        only_b = sorted(set(b) - set(a))
        only_a = sorted(set(a) - set(b))
        changed = []
        for key in sorted(set(b) & set(a)):
            fb = dict(flatten(b[key]))
            fa = dict(flatten(a[key]))
            fields = [
                (f, fb.get(f), fa.get(f))
                for f in sorted(set(fb) | set(fa))
                if fb.get(f) != fa.get(f)
            ]
            if fields:
                changed.append((key, fields))
        total_only += len(only_b) + len(only_a)
        total_changed += len(changed)
        status = "identical" if not (only_b or only_a or changed) else "DIFFERENT"
        print(
            f"[{mt}] before={len(b)} after={len(a)} only-before={len(only_b)} only-after={len(only_a)} changed={len(changed)} -> {status}"
        )
        for key in only_b[: args.show]:
            print(f"    only-before {key}")
        for key in only_a[: args.show]:
            print(f"    only-after  {key}")
        for key, fields in changed[: args.show]:
            print(f"    changed {key}:")
            for f, vb, va in fields[:12]:
                print(f"        {f}: {vb!r} -> {va!r}")
    print(
        "RESULT:",
        (
            "identical"
            if not (total_only or total_changed)
            else f"{total_only} only-one-side, {total_changed} changed"
        ),
    )
    return 0 if not (total_only or total_changed) else 1


def main(argv):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot")
    s.add_argument("out")
    s.add_argument("--kodi-home", required=True)
    s.add_argument("--profile", default=None)
    s.set_defaults(func=snapshot)
    d = sub.add_parser("diff")
    d.add_argument("before")
    d.add_argument("after")
    d.add_argument("--show", type=int, default=5)
    d.set_defaults(func=diff)
    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
