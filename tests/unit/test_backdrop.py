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
    # Only the live asset ships; the pristine copy is captured into addon_data
    # on first run (a second shipped PNG would be a second source of truth).
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


# --- the asset being replaced underneath us ----------------------------------


def test_addon_update_replacing_the_asset_is_noticed_and_healed(env):
    """An addon update restores the bundled artwork under a state file that
    still claims the server splash is installed. Without a re-hash the daily
    floor leaves that wrong for a day, and Kodi keeps serving the old texture
    on top of it."""
    backdrop.apply(FakeApi(), now=1000.0)
    assert live(env) == SPLASH_BYTES
    env["dropped"].clear()

    # What an addon update (or dev-install rsync) does: file back to bundled,
    # state file untouched.
    (env["media"] / backdrop.LIVE_NAME).write_bytes(DEFAULT_BYTES)

    # Well inside the daily floor, which would otherwise skip the fetch.
    backdrop.apply(FakeApi(), now=1001.0)

    assert live(env) == SPLASH_BYTES
    assert env["dropped"] == [(backdrop.settings.ADDON_ID, backdrop.LIVE_NAME)]


def test_drift_check_does_not_fire_when_the_asset_is_intact(env):
    backdrop.apply(FakeApi(), now=1000.0)
    env["dropped"].clear()

    api = FakeApi()
    backdrop.apply(api, now=1001.0)

    assert api.branding_calls == 0  # floor still holds; no false drift
    assert env["dropped"] == []


def test_missing_asset_counts_as_drift(env):
    backdrop.apply(FakeApi(), now=1000.0)
    (env["media"] / backdrop.LIVE_NAME).unlink()

    backdrop.apply(FakeApi(), now=1001.0)

    assert live(env) == SPLASH_BYTES


# --- the bundled copy is captured, not shipped twice -------------------------


def test_bundled_artwork_is_captured_into_addon_data_not_shipped(env):
    """Only the asset addon.xml names ships. A second identical PNG beside it
    would be a second source of truth for one image."""
    assert not (env["media"] / backdrop.DEFAULT_NAME).exists()

    backdrop.apply(FakeApi(), now=1000.0)

    captured = env["data"] / backdrop.DEFAULT_NAME
    assert captured.read_bytes() == DEFAULT_BYTES
    assert live(env) == SPLASH_BYTES  # and the swap still happened


def test_capture_is_taken_before_the_first_swap_even_when_offline(env):
    backdrop.apply(None, now=1000.0)  # no api: nothing to fetch

    assert (env["data"] / backdrop.DEFAULT_NAME).read_bytes() == DEFAULT_BYTES


def test_addon_update_with_new_artwork_refreshes_the_capture(env):
    backdrop.apply(FakeApi(), now=1000.0)
    assert (env["data"] / backdrop.DEFAULT_NAME).read_bytes() == DEFAULT_BYTES

    # An update ships different bundled artwork and rewrites resources/.
    new_bundled = b"redesigned-bundled-artwork"
    (env["media"] / backdrop.LIVE_NAME).write_bytes(new_bundled)

    backdrop.apply(FakeApi(), now=1001.0)

    assert (env["data"] / backdrop.DEFAULT_NAME).read_bytes() == new_bundled
    FakeAddon.store["useServerBackdrop"] = "false"
    backdrop.apply(None, now=1002.0)
    assert live(env) == new_bundled


def test_capture_is_not_retaken_once_the_server_image_is_live(env):
    """The snapshot may only be taken while the asset is still what shipped —
    re-taking it after a swap would record the splashscreen as the default."""
    backdrop.apply(FakeApi(), now=1000.0)
    assert live(env) == SPLASH_BYTES

    backdrop.apply(FakeApi(), now=1000.0 + backdrop.REFRESH_INTERVAL_SECONDS + 1)

    assert (env["data"] / backdrop.DEFAULT_NAME).read_bytes() == DEFAULT_BYTES


def test_lost_capture_leaves_the_live_image_rather_than_blanking(env):
    """addon_data cleared while a server image was live: there is nothing to
    restore, and a blank backdrop is worse than a stale one."""
    backdrop.apply(FakeApi(), now=1000.0)
    (env["data"] / backdrop.DEFAULT_NAME).unlink()
    env["dropped"].clear()

    FakeAddon.store["useServerBackdrop"] = "false"
    backdrop.apply(None, now=2000.0)

    assert live(env) == SPLASH_BYTES
    assert env["dropped"] == []


def test_reinstalling_identical_bytes_touches_nothing(env):
    """The drift reset re-runs the install path; when the bytes already match
    it must not rewrite the file or evict the texture."""
    FakeAddon.store["useServerBackdrop"] = "false"
    backdrop.apply(None, now=1000.0)
    before = (env["media"] / backdrop.LIVE_NAME).stat().st_mtime_ns
    env["dropped"].clear()

    (env["data"] / backdrop.STATE_NAME).unlink()  # forces the install path again
    backdrop.apply(None, now=2000.0)

    assert (env["media"] / backdrop.LIVE_NAME).stat().st_mtime_ns == before
    assert env["dropped"] == []
