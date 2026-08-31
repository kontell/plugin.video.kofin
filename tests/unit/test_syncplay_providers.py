"""The provider seam (plan G1.1-G1.2).

The registry is the engine's one dispatch from a queue item to a playing
stream. The Jellyfin provider must produce byte-identical URLs to the
pre-seam ``play_item``, because Kodi keys what it remembers about a plugin
row — resume bookmark, play count — on the exact URL string (core/urls.py).
"""

import pytest

import kofin.sync.db as database_module
from kofin.syncplay import providers

ITEM_ID = "696f7d7c6cf19390f1f4911c83f2954a"


class FakeApi:
    def __init__(self, item=None):
        self._item = item

    def item(self, item_id):
        return self._item


def jellyfin(item):
    return providers.JellyfinProvider(FakeApi(item))


def test_play_target_url_is_byte_identical_to_the_pre_seam_builder():
    target = jellyfin({"Id": ITEM_ID, "Type": "Movie"}).play_target(ITEM_ID, 1230000)

    assert target["url"] == (
        "plugin://plugin.video.kofin/?mode=play&id=" + ITEM_ID + "&startticks=1230000"
    )
    assert target["audio"] is False


def test_an_audio_item_routes_to_the_music_playlist():
    target = jellyfin({"Id": ITEM_ID, "Type": "Audio"}).play_target(ITEM_ID, 0)

    assert target["audio"] is True


def test_a_zero_start_is_sent_not_dropped():
    """A falsy 0 dropped from the URL falls back to the member's own resume
    point, minutes from the group — the pre-seam comment, kept true here."""
    target = jellyfin({"Id": ITEM_ID, "Type": "Movie"}).play_target(ITEM_ID, 0)

    assert target["url"].endswith("&startticks=0")


def test_a_negative_start_clamps_to_zero():
    """Extrapolation across a clock offset was measured reaching the route
    as startticks=-240000."""
    target = jellyfin({"Id": ITEM_ID, "Type": "Movie"}).play_target(ITEM_ID, -240000)

    assert target["url"].endswith("&startticks=0")


def test_a_missing_item_is_a_failed_lookup():
    with pytest.raises(LookupError):
        jellyfin(None).play_target(ITEM_ID, 0)


def test_no_api_client_is_a_failed_lookup():
    """A manager built without a client answered every lookup with None
    before the seam; the provider keeps that a failed start, not a crash."""
    with pytest.raises(LookupError):
        providers.JellyfinProvider(None).play_target(ITEM_ID, 0)


def test_resolve_kodi_id_maps_through_the_sync_db(monkeypatch):
    monkeypatch.setattr(database_module, "get_item", lambda kid, media: ("jf-9", "x"))

    assert providers.JellyfinProvider(None).resolve_kodi_id(42, "movie") == "jf-9"


def test_an_unmapped_kodi_id_resolves_to_none(monkeypatch):
    monkeypatch.setattr(database_module, "get_item", lambda kid, media: None)

    assert providers.JellyfinProvider(None).resolve_kodi_id(42, "movie") is None


def test_the_registry_dispatches_to_the_default_provider():
    registry = providers.jellyfin_registry(FakeApi({"Id": ITEM_ID, "Type": "Movie"}))

    target = registry.play_target(ITEM_ID, 0)

    assert target["url"].startswith("plugin://plugin.video.kofin/")


def test_the_registry_refuses_an_unknown_provider():
    registry = providers.jellyfin_registry(None)

    with pytest.raises(KeyError):
        registry.get("youtube")
