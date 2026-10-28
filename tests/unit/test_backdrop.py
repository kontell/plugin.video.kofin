"""The server backdrop lands in addon_data, and the bundled asset is never
touched.
"""

import json

import pytest

from kofin.service import backdrop
from tests.unit.fakes import FakeAddon

# The asset addon.xml names; the service module has no reason to know it.
BUNDLED = "fanart.jpeg"
DEFAULT_BYTES = b"bundled-artwork-jpeg-bytes"
SPLASH_BYTES = b"server-splashscreen-webp-bytes"
OTHER_SPLASH = b"a-different-server-splashscreen"


class FakeApi:
    def __init__(self, splash=SPLASH_BYTES, enabled=True):
        self.splash = splash
        self.enabled = enabled
        self.branding_calls = 0
        self.splash_calls = 0

    def branding_configuration(self):
        self.branding_calls += 1
        if isinstance(self.enabled, Exception):
            raise self.enabled
        return {"SplashscreenEnabled": self.enabled}

    def splashscreen(self):
        self.splash_calls += 1
        if isinstance(self.splash, Exception):
            raise self.splash
        return self.splash


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    FakeAddon.store = {"useServerBackdrop": "true"}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)

    addon_dir = tmp_path / "addon"
    media = addon_dir / "resources" / "media"
    media.mkdir(parents=True)
    (media / BUNDLED).write_bytes(DEFAULT_BYTES)

    data_dir = tmp_path / "addon_data"
    data_dir.mkdir()

    monkeypatch.setattr(backdrop.settings, "addon_path", lambda: str(addon_dir))
    monkeypatch.setattr(backdrop.settings, "addon_data_path", lambda: str(data_dir))

    yield {"media": media, "data": data_dir}


def live(env):
    return (env["media"] / BUNDLED).read_bytes()


def state(env):
    path = env["data"] / backdrop.STATE_NAME
    return json.loads(path.read_text()) if path.exists() else {}


def test_backdrop_writes_only_profile_and_keeps_install_immutable(env, monkeypatch):
    from pathlib import Path

    api = FakeApi()
    backdrop.apply(api, 1000)
    first = Path(backdrop.profile_path())
    assert first.parent == env["data"]
    assert first.read_bytes() == SPLASH_BYTES
    api.splash = OTHER_SPLASH
    backdrop.apply(api, 2000, force=True)
    second = Path(backdrop.profile_path())
    assert second != first and second.read_bytes() == OTHER_SPLASH
    assert live(env) == DEFAULT_BYTES
    FakeAddon.store["useServerBackdrop"] = "false"
    assert backdrop.profile_path() == ""
    assert live(env) == DEFAULT_BYTES
    assert not list(env["data"].glob("*.part"))
