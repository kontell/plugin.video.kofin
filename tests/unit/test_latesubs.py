"""The chase for a subtitle Jellyfin was still extracting when the play began.

The behaviour these pin was measured against a real 10.11.11 library: a cold
embedded-subtitle extraction takes 28-146 s depending on the source file, the
result is cached afterwards (~25 ms), and abandoning the request does not
abandon the extraction. That is what makes waiting worthwhile rather than a
second cold start, and it is why the play route no longer waits itself.
"""

import threading

import pytest

from kofin.core.streams import Attachment
from kofin.service import latesubs


def attachment(index=2, **kwargs):
    base = {
        "stream_index": index,
        "url": "http://s:8096/Videos/m1/src1/Subtitles/%d/0/Stream.subrip" % index,
        "sidecar": False,
        "language": "eng",
        "title": "",
        "forced": False,
    }
    base.update(kwargs)
    return Attachment(**base)


def item(method="Transcode", deferred=(), attached=(), session="ps1", fetchable=None):
    """A claimed play item. ``fetchable`` defaults to describing whatever is
    deferred, which is the normal case — the play route publishes every text
    track it *could* be handed and then names the one it missed."""
    if fetchable is None:
        fetchable = [attachment(index) for index in deferred] or [attachment(2)]
    return {
        "Id": "m1",
        "PlaySessionId": session,
        "PlayMethod": method,
        "SubtitleStreamIndex": 2,
        "Streams": {
            "MediaStreams": [],
            "Attached": list(attached),
            "Fetchable": [one._asdict() for one in fetchable],
            "Deferred": list(deferred),
        },
    }


class FakePlayer:
    """Only the things the chase reaches for on the real Player."""

    def __init__(self, live=None):
        self._live = live
        self.added = []
        self.republished = []
        self.applied = 0
        self.selected = []
        self.shown = []

    def current_item(self):
        return self._live

    def setSubtitles(self, path):
        # The *Player* setSubtitles: one path, added to the playback in
        # flight. Not the ListItem method of the same name, which takes a
        # list and only applies before the stream is opened — and not
        # addSubtitle, which xbmc.Player does not have at all (it exists over
        # JSON-RPC, which is what made the first cut of this AttributeError
        # against a live transcode).
        self.added.append(path)

    def republish_streams(self, item):
        self.republished.append(item)

    def apply_default_tracks(self):
        self.applied += 1

    def setSubtitleStream(self, ordinal):
        self.selected.append(ordinal)

    def showSubtitles(self, visible):
        self.shown.append(visible)


@pytest.fixture
def quick(monkeypatch):
    """No real waiting: the poll gap is what bounds a stop, not a test."""
    monkeypatch.setattr(latesubs, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(latesubs, "DEADLINE_SECONDS", 0.05)


def test_the_player_calls_are_ones_kodi_actually_has():
    """A fake player answers to whatever the code calls, so the fake alone
    cannot catch a method that does not exist. The first cut called
    ``addSubtitle`` — which Kodi exposes over JSON-RPC as
    ``Player.AddSubtitle`` but not on ``xbmc.Player`` — and the fetch it had
    just waited 24 s for died on AttributeError against a live transcode."""
    import xbmc

    assert hasattr(xbmc.Player, "setSubtitles")
    assert not hasattr(xbmc.Player, "addSubtitle")
    for name in ("setSubtitles", "showSubtitles", "setSubtitleStream"):
        assert hasattr(FakePlayer, name) or hasattr(xbmc.Player, name)


# -- what is worth chasing -----------------------------------------------------


def test_a_transcodes_deferred_tracks_are_rebuilt():
    assert latesubs.deferred_of(item(deferred=[2])) == [attachment(2)]


def test_every_text_track_is_fetchable_not_just_the_deferred_one():
    """What makes the stream menu free: a track nobody asked for at play time
    can still be fetched onto the running playback, so picking it costs a
    download instead of a new stream."""
    payload = item(deferred=[2], fetchable=[attachment(2), attachment(5)])
    assert sorted(latesubs.fetchable_of(payload)) == [2, 5]
    # Only the one this play resolved with is chased unasked.
    assert latesubs.deferred_of(payload) == [attachment(2)]


def test_an_index_the_playback_cannot_be_handed_is_absent():
    assert 9 not in latesubs.fetchable_of(item(fetchable=[attachment(2)]))


def test_nothing_is_chased_on_a_direct_play():
    """The container already holds every embedded track, so there is nothing
    to fetch — and a late addSubtitle would land after the demuxed ones, the
    one arrangement subtitle_ordinal cannot describe."""
    payload = item(method="DirectStream", deferred=[2])
    assert latesubs.deferred_of(payload) == []
    assert latesubs.fetchable_of(payload) == {}


def test_a_payload_from_another_version_is_ignored():
    payload = item()
    payload["Streams"]["Fetchable"] = [{"stream_index": 2, "surprise": True}]
    assert latesubs.fetchable_of(payload) == {}
    assert latesubs.deferred_of(payload) == []


def test_a_play_with_nothing_outstanding_chases_nothing():
    assert latesubs.deferred_of(item()) == []
    # ...though the track is still there to be asked for.
    assert 2 in latesubs.fetchable_of(item())


# -- the chase -----------------------------------------------------------------


def run(chase):
    """Run the worker to completion on this thread (start() would race)."""
    chase._run()


def test_a_track_that_lands_is_attached_to_the_running_playback(monkeypatch, quick):
    live = item(attached=[])
    player = FakePlayer(live)
    monkeypatch.setattr(latesubs.subtitles, "fetch_to", lambda *a, **k: "/tmp/eng.srt")

    run(latesubs.LateSubtitles(object(), player, live, [attachment(2)]))

    assert player.added == ["/tmp/eng.srt"]
    # Kodi appends it, and on a transcode the attached list is the whole list,
    # so recording it in the same order keeps subtitle_ordinal able to answer.
    assert live["Streams"]["Attached"] == [2]
    assert player.republished == [live]
    # Landed the way it would have landed on time: one rule for shown-or-not.
    assert player.applied == 1


def test_a_track_the_viewer_picked_goes_on_screen(monkeypatch, quick):
    """apply_default_tracks answers with the *resolved* index, so running it
    for a menu pick would switch the viewer straight back off the track they
    just chose — or off altogether, when the server nominated none."""
    live = item(attached=[7])  # something already attached ahead of it
    player = FakePlayer(live)
    monkeypatch.setattr(latesubs.subtitles, "fetch_to", lambda *a, **k: "/tmp/ger.srt")

    run(latesubs.LateSubtitles(object(), player, live, [attachment(5)], requested=True))

    assert player.added == ["/tmp/ger.srt"]
    assert live["Streams"]["Attached"] == [7, 5]
    # Kodi appended it, so it is the last ordinal — and it is shown, whatever
    # the resolved index was and whether subtitles were off at the time.
    assert player.selected == [1] and player.shown == [True]
    assert player.applied == 0


def test_it_keeps_asking_until_the_extraction_finishes(monkeypatch, quick):
    monkeypatch.setattr(latesubs, "DEADLINE_SECONDS", 5.0)
    live = item()
    player = FakePlayer(live)
    attempts = []

    def answer(http, attachment, *args, **kwargs):
        attempts.append(attachment.stream_index)
        return "/tmp/eng.srt" if len(attempts) == 3 else ""

    monkeypatch.setattr(latesubs.subtitles, "fetch_to", answer)

    run(latesubs.LateSubtitles(object(), player, live, [attachment(2)]))

    assert attempts == [2, 2, 2]
    assert player.added == ["/tmp/eng.srt"]


def test_it_gives_up_rather_than_asking_forever(monkeypatch, quick):
    """A track that has not appeared by the deadline is one the server cannot
    produce. The stream menu still offers it."""
    player = FakePlayer(item())
    monkeypatch.setattr(latesubs.subtitles, "fetch_to", lambda *a, **k: "")

    run(latesubs.LateSubtitles(object(), player, item(), [attachment(2)]))

    assert player.added == []


def test_a_track_that_arrives_after_its_playback_is_not_attached(monkeypatch, quick):
    """The stream menu's restart makes exactly this race: it replaces the
    playback while the chase is still waiting. Landing the old subtitle on the
    new stream would attach a track nothing else believes is there."""
    started = item(session="ps1")
    player = FakePlayer(item(session="ps2"))  # a different playback now
    monkeypatch.setattr(latesubs.subtitles, "fetch_to", lambda *a, **k: "/tmp/eng.srt")

    run(latesubs.LateSubtitles(object(), player, started, [attachment(2)]))

    assert player.added == [] and player.republished == []


def test_nothing_is_attached_once_playback_has_stopped(monkeypatch, quick):
    player = FakePlayer(None)
    monkeypatch.setattr(latesubs.subtitles, "fetch_to", lambda *a, **k: "/tmp/eng.srt")

    run(latesubs.LateSubtitles(object(), player, item(), [attachment(2)]))

    assert player.added == []


def test_a_stop_is_answered_within_one_attempt(monkeypatch):
    """CLAUDE.md's sync-thread rule applies to any thread an addon starts:
    Kodi will not finalise a script while one is alive. So the chase must not
    park a teardown for the length of an extraction — it sleeps on the cancel
    event, and each attempt carries the play route's own short budget."""
    monkeypatch.setattr(latesubs, "POLL_SECONDS", 30.0)
    player = FakePlayer(item())
    entered = threading.Event()

    def answer(*args, **kwargs):
        entered.set()
        return ""

    monkeypatch.setattr(latesubs.subtitles, "fetch_to", answer)
    chase = latesubs.LateSubtitles(object(), player, item(), [attachment(2)])
    chase.start()

    assert entered.wait(5.0)
    chase.stop()
    chase._thread.join(timeout=5.0)
    assert not chase._thread.is_alive()


def test_a_fetch_that_raises_never_reaches_the_player(monkeypatch, quick):
    """Nothing here may raise into the service: this runs beside a playback
    that must not notice it."""

    def explode(*args, **kwargs):
        raise RuntimeError("transport gone")

    monkeypatch.setattr(latesubs.subtitles, "fetch_to", explode)
    player = FakePlayer(item())

    run(latesubs.LateSubtitles(object(), player, item(), [attachment(2)]))

    assert player.added == []
