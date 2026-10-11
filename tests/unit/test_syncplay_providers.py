"""The provider seam (plan G1.1-G1.2).

The registry is the engine's one dispatch from a queue item to a playing
stream. The Jellyfin provider must produce byte-identical URLs to the
pre-seam ``play_item``, because Kodi keys what it remembers about a plugin
row — resume bookmark, play count — on the exact URL string (core/urls.py).
"""

import pytest

import kofin.sync.private as database_module
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


def test_the_target_carries_the_item_s_runtime():
    item = {"Id": ITEM_ID, "Type": "Movie", "RunTimeTicks": 14289000000}

    assert jellyfin(item).play_target(ITEM_ID, 0)["runtime_ticks"] == 14289000000


def test_the_runtime_is_the_longest_version_s():
    """The item's own figure is its primary version's; a member on the
    extended cut is still inside the item after the theatrical one ends."""
    item = {
        "Id": ITEM_ID,
        "Type": "Movie",
        "RunTimeTicks": 70000000000,
        "MediaSources": [
            {"Id": "a", "RunTimeTicks": 70000000000},
            {"Id": "b", "RunTimeTicks": 75000000000},
            {"Id": "c"},
        ],
    }

    assert jellyfin(item).play_target(ITEM_ID, 0)["runtime_ticks"] == 75000000000


@pytest.mark.parametrize(
    "item",
    [
        {"Id": ITEM_ID, "Type": "Movie"},
        {"Id": ITEM_ID, "Type": "Movie", "RunTimeTicks": 0},
        {"Id": ITEM_ID, "Type": "Movie", "RunTimeTicks": None, "MediaSources": None},
        # A recording still being written is longer than it was probed at.
        {"Id": ITEM_ID, "Type": "Recording", "RunTimeTicks": 6000000000},
        {"Id": ITEM_ID, "Type": "TvChannel", "RunTimeTicks": 6000000000},
    ],
    ids=["absent", "zero", "nulls", "recording", "channel"],
)
def test_no_runtime_is_claimed_where_none_is_known(item):
    assert "runtime_ticks" not in jellyfin(item).play_target(ITEM_ID, 0)


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


# --- the API build has no table of Kodi ids ---------------------------------
#
# sync.private.get_item answers None for every row there, so a provider that
# asked it called every library play foreign.


@pytest.fixture
def api_build(monkeypatch):
    from kofin import buildconfig
    from kofin.sync.backends.api import identity

    monkeypatch.setattr(buildconfig, "BACKEND", "api")
    return identity


def test_on_the_api_build_a_native_row_maps_through_its_identity(
    api_build, monkeypatch
):
    asked = []

    def mapped_item(kodi_id, media):
        asked.append((kodi_id, media))
        return "jf-9"

    monkeypatch.setattr(api_build, "mapped_item", mapped_item)

    assert providers.JellyfinProvider(None).resolve_kodi_id(42, "movie") == "jf-9"
    assert asked == [(42, "movie")]


def test_on_the_api_build_a_row_that_is_not_ours_resolves_to_none(
    api_build, monkeypatch
):
    monkeypatch.setattr(api_build, "mapped_item", lambda kid, media: None)

    assert providers.JellyfinProvider(None).resolve_kodi_id(42, "movie") is None


def test_on_the_api_build_a_failed_lookup_is_not_an_answer(api_build, monkeypatch):
    """None means "not ours" and releases the play as unmanaged; a lookup
    that could not be made must not be read as that."""

    def refuse(kodi_id, media):
        raise RuntimeError("Kodi refused VideoLibrary.GetMovieDetails")

    monkeypatch.setattr(api_build, "mapped_item", refuse)

    with pytest.raises(RuntimeError):
        providers.JellyfinProvider(None).resolve_kodi_id(42, "movie")


def test_on_the_sql_build_the_api_identity_is_never_asked(monkeypatch):
    """The SQL distribution does not ship the API backend at all."""
    from kofin import buildconfig
    from kofin.sync.backends.api import identity

    monkeypatch.setattr(buildconfig, "BACKEND", "sql")
    monkeypatch.setattr(
        identity, "mapped_item", lambda kid, media: pytest.fail("API lookup asked")
    )
    monkeypatch.setattr(database_module, "get_item", lambda kid, media: ("jf-9", "x"))

    assert providers.JellyfinProvider(None).resolve_kodi_id(42, "movie") == "jf-9"


def test_the_registry_dispatches_to_the_default_provider():
    registry = providers.jellyfin_registry(FakeApi({"Id": ITEM_ID, "Type": "Movie"}))

    target = registry.play_target(ITEM_ID, 0)

    assert target["url"].startswith("plugin://plugin.video.kofin/")


def test_the_registry_refuses_an_unknown_provider():
    registry = providers.jellyfin_registry(None)

    with pytest.raises(KeyError):
        registry.get("youtube")


# --- the template provider (plan G2.4) ------------------------------------


def test_a_template_substitutes_key_and_position():
    provider = providers.TemplateProvider(
        "plugin://plugin.video.youtube/play/?video_id={key}&seek={position_s}"
    )

    target = provider.play_target("dQw4w9WgXcQ", 1230000000)

    assert target == {
        "url": "plugin://plugin.video.youtube/play/?video_id=dQw4w9WgXcQ&seek=123",
        "audio": False,
    }


def test_a_template_keeps_a_zero_position_and_clamps_a_negative_one():
    provider = providers.TemplateProvider("p://x/?id={key}&s={position_s}")

    assert provider.play_target("k", 0)["url"].endswith("&s=0")
    assert provider.play_target("k", -240000)["url"].endswith("&s=0")


def test_a_template_key_is_url_quoted_and_braces_are_inert():
    """The template is foreign text: token replace only, never str.format."""
    provider = providers.TemplateProvider("p://x/?id={key}&odd={brace}")

    target = provider.play_target("a b/c", 0)

    assert "id=a%20b%2Fc" in target["url"]
    assert "{brace}" in target["url"]  # not a substitution point; left alone


def test_a_template_provider_has_no_claim_on_the_kodi_library():
    assert providers.TemplateProvider("p://{key}").resolve_kodi_id(1, "movie") is None
