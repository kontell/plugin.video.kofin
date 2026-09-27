"""Kodi's Clean library probes the plugin URLs stored in MyVideos."""

from pathlib import Path
from xml.etree import ElementTree

import pytest
import xbmcplugin

from kofin.plugin import exists, router
from kofin.plugin.router import Request

LIBRARY = "f137a2dd21bbc1b99aa5c0f6bf02a805"
SHOW = "c5c43ca77de5cd90e2553622232afd43"
ITEM = "5955d03a79f7dc64a5e8e92fcabaf85f"


@pytest.mark.parametrize(
    ("path", "params", "expected"),
    [
        ("/%s/" % LIBRARY, {}, True),
        ("/%s/%s/" % (LIBRARY, SHOW), {}, True),
        ("/%s/" % LIBRARY, {"mode": "play", "id": ITEM, "dbid": "42"}, True),
        (
            "/%s/" % LIBRARY,
            {"mode": "play", "id": ITEM, "mediasourceid": SHOW},
            True,
        ),
        ("/%s/%s/" % (LIBRARY, SHOW), {"mode": "play", "id": ITEM}, True),
        ("/%s/" % LIBRARY, {"mode": "browse", "id": ITEM}, False),
        ("/not-a-library/", {"mode": "play", "id": ITEM}, False),
        ("/%s/" % LIBRARY, {"mode": "play", "id": ""}, False),
    ],
)
def test_check_exists_recognizes_synced_video_paths(
    monkeypatch, path, params, expected
):
    resolved = []
    monkeypatch.setattr(
        xbmcplugin,
        "setResolvedUrl",
        lambda handle, success, item: resolved.append((handle, success)),
    )
    request = Request(
        "plugin://plugin.video.kofin" + path,
        7,
        dict(params, kodi_action="check_exists"),
    )

    exists.check_exists(request)

    assert resolved == [(7, expected)]


def test_check_exists_never_treats_a_normal_play_as_a_probe(monkeypatch):
    resolved = []
    monkeypatch.setattr(
        xbmcplugin,
        "setResolvedUrl",
        lambda handle, success, item: resolved.append(success),
    )
    exists.check_exists(
        Request(
            "plugin://plugin.video.kofin/%s/" % LIBRARY,
            7,
            {"mode": "play", "id": ITEM},
        )
    )
    assert resolved == [False]


def test_clean_probe_takes_precedence_over_play(monkeypatch):
    seen = []
    monkeypatch.setattr(
        router, "_resolve", lambda mode: lambda request: seen.append(mode)
    )
    router.dispatch(
        [
            "plugin://plugin.video.kofin/%s/" % LIBRARY,
            "-1",
            "?mode=play&id=%s&kodi_action=check_exists" % ITEM,
        ]
    )
    assert seen == ["check_exists"]


def test_manifest_allows_kodi_to_probe_every_synced_video_type():
    addon = ElementTree.parse(Path(__file__).parents[2] / "addon.xml").getroot()
    plugin = next(
        ext
        for ext in addon.findall("extension")
        if ext.get("point") == "xbmc.python.pluginsource"
    )
    assert {
        (path.get("content"), path.text)
        for path in plugin.findall("medialibraryscanpath")
    } == {("movies", "/"), ("tvshows", "/"), ("musicvideos", "/")}
