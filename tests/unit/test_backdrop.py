"""L1: the addon backdrop swap — what gets written, and (mostly) what does not.

The invariants worth guarding are the negative ones. Every service connect
runs this, so an unchanged splashscreen must not rewrite the asset or drop the
cached texture, and a server that is down must never leave the install with a
half-written PNG.
"""

import json
import os

import pytest

from kofin.core.http import ServerUnreachable
from kofin.service import backdrop
from tests.unit.fakes import FakeAddon

DEFAULT_BYTES = b"bundled-artwork-png-bytes"
SPLASH_BYTES = b"server-splashscreen-png-bytes"
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
    (media / backdrop.DEFAULT_NAME).write_bytes(DEFAULT_BYTES)
    (media / backdrop.LIVE_NAME).write_bytes(DEFAULT_BYTES)

    data_dir = tmp_path / "addon_data"
    data_dir.mkdir()

    monkeypatch.setattr(backdrop.settings, "addon_path", lambda: str(addon_dir))
    monkeypatch.setattr(backdrop.settings, "addon_data_path", lambda: str(data_dir))

    dropped = []
    monkeypatch.setattr(
        backdrop.kodirpc,
        "drop_cached_texture",
        lambda needle, require="": dropped.append((needle, require)),
    )
    yield {"media": media, "data": data_dir, "dropped": dropped}


def live(env):
    return (env["media"] / backdrop.LIVE_NAME).read_bytes()


def state(env):
    path = env["data"] / backdrop.STATE_NAME
    return json.loads(path.read_text()) if path.exists() else {}


# --- the happy path ----------------------------------------------------------


def test_installs_server_splashscreen_and_drops_cached_texture(env):
    api = FakeApi()

    backdrop.apply(api, now=1000.0)

    assert live(env) == SPLASH_BYTES
    assert env["dropped"] == [(backdrop.settings.ADDON_ID, backdrop.LIVE_NAME)]
    assert state(env)["source"] == backdrop.SOURCE_SERVER
    assert state(env)["fetched"] == 1000.0


def test_changed_splashscreen_replaces_the_previous_one(env):
    backdrop.apply(FakeApi(), now=1000.0)
    env["dropped"].clear()

    backdrop.apply(FakeApi(splash=OTHER_SPLASH), now=1000.0 + 90000)

    assert live(env) == OTHER_SPLASH
    assert env["dropped"] == [(backdrop.settings.ADDON_ID, backdrop.LIVE_NAME)]


# --- the writes that must not happen -----------------------------------------


def test_unchanged_splashscreen_rewrites_nothing_but_advances_the_floor(env):
    backdrop.apply(FakeApi(), now=1000.0)
    env["dropped"].clear()
    before = (env["media"] / backdrop.LIVE_NAME).stat().st_mtime_ns

    later = 1000.0 + backdrop.REFRESH_INTERVAL_SECONDS + 1
    backdrop.apply(FakeApi(), now=later)

    assert (env["media"] / backdrop.LIVE_NAME).stat().st_mtime_ns == before
    assert env["dropped"] == []  # a re-cache of a 2.3MB image for no change
    assert state(env)["fetched"] == later


def test_daily_floor_skips_the_fetch_entirely(env):
    backdrop.apply(FakeApi(), now=1000.0)
    api = FakeApi(splash=OTHER_SPLASH)

    backdrop.apply(api, now=1000.0 + backdrop.REFRESH_INTERVAL_SECONDS - 1)

    assert api.branding_calls == 0 and api.splash_calls == 0
    assert live(env) == SPLASH_BYTES


def test_force_bypasses_the_floor(env):
    backdrop.apply(FakeApi(), now=1000.0)

    backdrop.apply(FakeApi(splash=OTHER_SPLASH), now=1001.0, force=True)

    assert live(env) == OTHER_SPLASH


def test_repeated_restore_is_a_no_op(env):
    FakeAddon.store["useServerBackdrop"] = "false"
    backdrop.apply(None, now=1000.0)
    env["dropped"].clear()
    before = (env["media"] / backdrop.LIVE_NAME).stat().st_mtime_ns

    backdrop.apply(None, now=2000.0)

    assert (env["media"] / backdrop.LIVE_NAME).stat().st_mtime_ns == before
    assert env["dropped"] == []


# --- turning it off ----------------------------------------------------------


def test_disabling_restores_the_bundled_artwork_without_a_server(env):
    backdrop.apply(FakeApi(), now=1000.0)
    assert live(env) == SPLASH_BYTES
    env["dropped"].clear()

    FakeAddon.store["useServerBackdrop"] = "false"
    backdrop.apply(None, now=2000.0)

    assert live(env) == DEFAULT_BYTES
    assert env["dropped"] == [(backdrop.settings.ADDON_ID, backdrop.LIVE_NAME)]
    assert state(env)["source"] == backdrop.SOURCE_DEFAULT


def test_server_side_splashscreen_disabled_falls_back(env):
    backdrop.apply(FakeApi(), now=1000.0)

    api = FakeApi(enabled=False)
    backdrop.apply(api, now=1000.0 + 90000)

    assert live(env) == DEFAULT_BYTES
    assert api.splash_calls == 0  # nothing to fetch


# --- failure leaves the current image alone ----------------------------------


@pytest.mark.parametrize(
    "api",
    [
        FakeApi(splash=ServerUnreachable("down")),
        FakeApi(enabled=ServerUnreachable("down")),
        FakeApi(splash=b""),
    ],
    ids=["fetch-fails", "branding-fails", "empty-body"],
)
def test_failures_leave_the_live_asset_untouched(env, api):
    backdrop.apply(api, now=1000.0)

    assert live(env) == DEFAULT_BYTES
    assert env["dropped"] == []
    assert state(env) == {}


def test_no_api_yet_is_not_an_error(env):
    backdrop.apply(None, now=1000.0)

    assert live(env) == DEFAULT_BYTES
    assert state(env) == {}


def test_unexpected_errors_are_contained(env, monkeypatch):
    monkeypatch.setattr(
        backdrop.settings, "get_bool", lambda _id: (_ for _ in ()).throw(RuntimeError)
    )

    backdrop.apply(FakeApi(), now=1000.0)  # must not raise

    assert live(env) == DEFAULT_BYTES


def test_write_is_atomic_and_leaves_no_partial_file(env, monkeypatch):
    real_replace = os.replace
    monkeypatch.setattr(
        os, "replace", lambda *a: (_ for _ in ()).throw(OSError("no space"))
    )

    backdrop.apply(FakeApi(), now=1000.0)

    monkeypatch.setattr(os, "replace", real_replace)
    assert live(env) == DEFAULT_BYTES
    assert not (env["media"] / (backdrop.LIVE_NAME + ".part")).exists()
    assert env["dropped"] == []
