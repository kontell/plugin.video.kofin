#!/usr/bin/env python3
"""Play counts and watched marks, Kodi against the server, scenario by scenario.

Runs on the development host against one rig and the scratch libraries
(``scratch.py``; never a real library, since every scenario changes play
state). For each scenario it reads the Kodi rows and the server's user data
before, does what a viewer would do, waits for both sides to settle, reads
them again and keeps kofin's own log lines from in between.

    KOFIN_RIG_SSH="ssh user@rig" KOFIN_RIG_PROBE=/path/on/rig/kodi_probe.py \\
        userdata_matrix.py USER_ID [SCENARIO ...] > result.json

``KOFIN_RIG_SSH`` is the command that runs a shell command on the rig; the
server is ``JELLYFIN_URL`` with ``JELLYFIN_API_KEY``. The summary goes to
stderr, the full record to stdout.
"""

import json
import os
import shlex
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scratch  # noqa: E402

ALBUM = "Scratch Album One"
ALBUM_TWO = "Scratch Album Two"
# The album of one song: opened by its album id it plays exactly that song.
# A song cannot be opened by its own id (see scratch.ALBUMS).
SOLO = "Scratch Album Solo"
LOG = (
    r"kofin\.service\.player: (claim|-->|<--)|kofin\.service\.kodiuserdata"
    r"|patching \d+ (episode|audio|movie)|kofin.*(local|outbox|userdata)"
    r"|stop report failed|backfill"
)


def quote(text):
    return "'" + text.replace("'", "'\\''") + "'"


def probe(*args, timeout=900):
    command = "python3 %s %s" % (
        os.environ["KOFIN_RIG_PROBE"],
        " ".join(quote(str(a)) for a in args),
    )
    done = subprocess.run(
        shlex.split(os.environ["KOFIN_RIG_SSH"]) + [command],
        capture_output=True,
        timeout=timeout,
    )
    if done.returncode:
        raise RuntimeError(done.stderr.decode("utf-8", "replace")[-800:])
    return json.loads(done.stdout.decode("utf-8"))


def rpc(method, params=None):
    return probe("rpc", method, json.dumps(params or {}))


class Rig:
    def __init__(self, user):
        self.user = user
        self.data = scratch.state()
        shows = rpc(
            "VideoLibrary.GetTVShows",
            {"filter": {"field": "title", "operator": "contains", "value": "Scratch"}},
        ).get("tvshows", [])
        if not shows:
            raise SystemExit("the scratch show is not in this Kodi")
        self.show = shows[0]["tvshowid"]

    # -- Kodi -----------------------------------------------------------

    def episodes(self):
        rows = rpc(
            "VideoLibrary.GetEpisodes",
            {
                "tvshowid": self.show,
                "properties": [
                    "season",
                    "episode",
                    "playcount",
                    "lastplayed",
                    "resume",
                ],
            },
        ).get("episodes", [])
        return {
            "S%02dE%02d"
            % (r["season"], r["episode"]): {
                "id": r["episodeid"],
                "playcount": r["playcount"],
                "lastplayed": r["lastplayed"],
                "resume": round(r["resume"]["position"]),
            }
            for r in rows
        }

    def songs(self, album=ALBUM):
        rows = rpc(
            "AudioLibrary.GetSongs",
            {
                "filter": {"field": "album", "operator": "is", "value": album},
                "properties": ["title", "playcount", "lastplayed", "track", "albumid"],
                "sort": {"method": "track"},
            },
        ).get("songs", [])
        return {
            r["title"]: {
                "id": r["songid"],
                "albumid": r["albumid"],
                "playcount": r["playcount"],
                "lastplayed": r["lastplayed"],
            }
            for r in rows
        }

    # -- server ---------------------------------------------------------

    def server(self, kind, album=ALBUM):
        rows = {}
        for item in scratch._items(self.data, [kind], user=self.user):
            if kind == "Episode":
                name = "S%02dE%02d" % (
                    item.get("ParentIndexNumber", 0),
                    item.get("IndexNumber", 0),
                )
            else:
                if item.get("Album") != album:
                    continue
                name = item["Name"]
            data = item.get("UserData") or {}
            rows[name] = {
                "id": item["Id"],
                "Played": bool(data.get("Played")),
                "PlayCount": data.get("PlayCount") or 0,
                "LastPlayedDate": data.get("LastPlayedDate"),
                "Position": round((data.get("PlaybackPositionTicks") or 0) / 1e7),
            }
        return rows


def agree(kodi, server):
    """Watched state equal on both sides for every row."""
    return all(
        bool(kodi[name]["playcount"]) == server[name]["Played"]
        for name in kodi
        if name in server
    )


def settle(read_kodi, read_server, seconds=75):
    """Read both sides until two successive readings are identical and they
    agree, or the time is up; returns the last readings and how long."""
    began = time.monotonic()
    last = None
    while True:
        now = (read_kodi(), read_server())
        if now == last and agree(*now):
            return now + (round(time.monotonic() - began, 1), True)
        if time.monotonic() - began > seconds:
            return now + (round(time.monotonic() - began, 1), False)
        last = now
        time.sleep(4)


def brief(kodi, server):
    return {
        name: "kodi %d / server %s%d"
        % (
            kodi[name]["playcount"],
            "played " if server.get(name, {}).get("Played") else "unplayed ",
            server.get(name, {}).get("PlayCount", -1),
        )
        for name in sorted(kodi)
    }


def run(rig, name, kind, action, album=ALBUM):
    read_kodi = rig.episodes if kind == "Episode" else lambda: rig.songs(album)
    read_server = lambda: rig.server(kind, album)  # noqa: E731
    before = (read_kodi(), read_server())
    start = probe("log-lines")
    began = time.monotonic()
    note = action(before[0], before[1])
    acted = round(time.monotonic() - began, 1)
    kodi, server, waited, settled = settle(read_kodi, read_server)
    lines = [
        line.split("[kofin] ", 1)[-1][:170] for line in probe("log-since", start, LOG)
    ]
    record = {
        "scenario": name,
        "note": note,
        "before": brief(*before),
        "after": brief(kodi, server),
        "action_s": acted,
        "settle_s": waited,
        "settled_and_agree": settled,
        "log": lines,
    }
    sys.stderr.write(
        "%-34s %s  (settled in %.0f s)\n"
        % (name, "AGREE" if settled else "DISAGREE", waited)
    )
    for row in sorted(record["after"]):
        if record["after"][row] != record["before"][row]:
            sys.stderr.write(
                "    %-14s %s -> %s\n"
                % (row, record["before"][row], record["after"][row])
            )
    return record


def play(item, stop_after=None, next_after=None, seek_percent=None):
    """Play on the rig, to the end or to a stop; the probe stays with it."""
    args = [
        "play",
        json.dumps(item),
        "-" if stop_after is None else stop_after,
        "-" if next_after is None else next_after,
    ]
    if seek_percent is not None:
        args.append(seek_percent)
    done = probe(*args)
    if done.get("started_s") is None:
        raise RuntimeError("playback did not start")
    return done


def scenarios(rig):
    def song_to_end(kodi, server):
        play({"albumid": next(iter(kodi.values()))["albumid"]})
        return "one song played to its end"

    def album_through(kodi, server):
        play({"albumid": next(iter(kodi.values()))["albumid"]})
        return "an album of %d songs played through" % len(kodi)

    def song_skipped(kodi, server):
        play(
            {"albumid": next(iter(kodi.values()))["albumid"]},
            stop_after=6,
            next_after=8,
        )
        return "first song skipped after 8 s, second stopped after 6 s"

    def episode_to_end(kodi, server):
        play({"episodeid": kodi["S01E01"]["id"]}, seek_percent=96)
        return "an unwatched episode played to its end (from 96 %)"

    def episode_stopped_early(kodi, server):
        play({"episodeid": kodi["S01E02"]["id"]}, stop_after=8, seek_percent=40)
        return "an unwatched episode stopped at about 40 %"

    def mark_watched(kodi, server):
        rpc(
            "VideoLibrary.SetEpisodeDetails",
            {"episodeid": kodi["S01E03"]["id"], "playcount": 1},
        )
        return "one episode marked watched in Kodi"

    def mark_season(kodi, server):
        for name in ("S02E01", "S02E02"):
            rpc(
                "VideoLibrary.SetEpisodeDetails",
                {"episodeid": kodi[name]["id"], "playcount": 1},
            )
        return "a season of two episodes marked watched in Kodi"

    def mark_unwatched(kodi, server):
        rpc(
            "VideoLibrary.SetEpisodeDetails",
            {"episodeid": kodi["S01E03"]["id"], "playcount": 0},
        )
        return "a watched episode marked unwatched in Kodi"

    def other_client_plays(kodi, server):
        scratch.api(
            "POST", "/UserPlayedItems/%s" % server["S01E03"]["id"], {"userId": rig.user}
        )
        return "an episode marked played by another client"

    def other_client_unplays(kodi, server):
        scratch.api(
            "DELETE",
            "/UserPlayedItems/%s" % server["S02E01"]["id"],
            {"userId": rig.user},
        )
        return "a watched episode marked unplayed by another client"

    def rewatch_stopped(kodi, server):
        play({"episodeid": kodi["S01E01"]["id"]}, stop_after=8, seek_percent=50)
        return "a watched episode played again and stopped at about 50 %"

    def song_again(kodi, server):
        play({"albumid": next(iter(kodi.values()))["albumid"]})
        return "a played song played to its end again"

    return [
        ("song to end", "Audio", song_to_end, SOLO),
        ("song again", "Audio", song_again, SOLO),
        ("album through", "Audio", album_through, ALBUM),
        ("album through again", "Audio", album_through, ALBUM),
        ("song skipped", "Audio", song_skipped, "Scratch Album Three"),
        ("episode to end", "Episode", episode_to_end, None),
        ("episode stopped early", "Episode", episode_stopped_early, None),
        ("mark watched in Kodi", "Episode", mark_watched, None),
        ("mark season in Kodi", "Episode", mark_season, None),
        ("mark unwatched in Kodi", "Episode", mark_unwatched, None),
        ("other client plays", "Episode", other_client_plays, None),
        ("other client unplays", "Episode", other_client_unplays, None),
        ("rewatch stopped half-way", "Episode", rewatch_stopped, None),
    ]


def main(argv):
    if len(argv) < 2:
        raise SystemExit(__doc__)
    rig = Rig(argv[1])
    wanted = argv[2:]
    results = []
    for name, kind, action, album in scenarios(rig):
        if wanted and name not in wanted:
            continue
        results.append(run(rig, name, kind, action, album or ALBUM))
    json.dump(results, sys.stdout, indent=1)
    print()
    failed = [r["scenario"] for r in results if not r["settled_and_agree"]]
    sys.stderr.write(
        "%d of %d scenarios settled in agreement%s\n"
        % (
            len(results) - len(failed),
            len(results),
            "; not: " + ", ".join(failed) if failed else "",
        )
    )


if __name__ == "__main__":
    main(sys.argv)
