import pytest

from kofin.core import settings
from tests.unit.fakes import FakeAddon


@pytest.fixture(autouse=True)
def fake_addon(monkeypatch):
    FakeAddon.store = {}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    return FakeAddon


def test_get_list_splits_and_strips():
    settings.set_str("codecs", "h264, hevc ,av1,,")
    assert settings.get_list("codecs") == ["h264", "hevc", "av1"]


def test_credentials_generate_stable_device_id():
    first = settings.Credentials.load()
    assert first.device_id
    second = settings.Credentials.load()
    assert second.device_id == first.device_id


def test_credentials_round_trip():
    creds = settings.Credentials.load()
    creds.server_address = "http://jelly:8096"
    creds.server_name = "minipie"
    creds.server_id = "srv1"
    creds.user_id = "u1"
    creds.display_user = "conor"
    creds.token = "tok1"
    creds.is_logged_in = True
    creds.save()

    loaded = settings.Credentials.load()
    assert loaded.server_address == "http://jelly:8096"
    assert loaded.server_name == "minipie"
    assert loaded.token == "tok1"
    assert loaded.is_logged_in is True


def test_clear_logs_out_but_keeps_server_address_and_device():
    creds = settings.Credentials.load()
    creds.server_address = "http://jelly:8096"
    creds.token = "tok1"
    creds.user_id = "u1"
    creds.is_logged_in = True
    creds.save()

    settings.Credentials.clear()
    loaded = settings.Credentials.load()
    assert loaded.server_address == "http://jelly:8096"
    assert loaded.device_id == creds.device_id
    assert loaded.token == ""
    assert loaded.user_id == ""
    assert loaded.is_logged_in is False


def test_clear_drops_who_is_watching_but_keeps_the_shortlist():
    """Additional-user membership is tied to this primary user; the shortlist
    is a device preference about who the picker offers and survives logout."""
    FakeAddon.store["whoIsWatching"] = "u2,u4"
    FakeAddon.store["whoIsWatchingShortlist"] = "u2,u3,u4"
    creds = settings.Credentials.load()
    creds.is_logged_in = True
    creds.save()

    settings.Credentials.clear()

    assert FakeAddon.store.get("whoIsWatching", "") == ""
    assert FakeAddon.store["whoIsWatchingShortlist"] == "u2,u3,u4"


def test_resume_offset_is_a_magnitude():
    # The slider stores a negative number ("-10s") because that reads right in
    # the settings UI; every caller wants seconds to rewind by.
    assert settings.resume_offset() == 0.0  # unset
    FakeAddon.store["resumeJumpBack"] = "-10"
    assert settings.resume_offset() == 10.0
    FakeAddon.store["resumeJumpBack"] = "0"
    assert settings.resume_offset() == 0.0


def test_adjusted_resume_rewinds_by_the_offset():
    FakeAddon.store["resumeJumpBack"] = "-10"
    assert settings.adjusted_resume(600.0) == 590.0
    assert settings.adjusted_resume(0.0) == 0.0


def test_adjusted_resume_leaves_a_position_shorter_than_the_offset():
    # Fork semantics: clamping to zero would flip a barely-started item back to
    # unwatched, losing the in-progress bookmark the viewer expects to see.
    FakeAddon.store["resumeJumpBack"] = "-60"
    assert settings.adjusted_resume(45.0) == 45.0
    assert settings.adjusted_resume(60.0) == 60.0
    assert settings.adjusted_resume(60.5) == 0.5


def test_the_artwork_query_carries_only_parameters_the_server_honours():
    """Measured against 10.11: EnableImageEnhancers is not in the OpenAPI
    spec at all (an Emby-era feature Jellyfin dropped), so the setting that
    sent it could never have done anything. MaxHeight and Quality are both
    declared and both work."""
    import xml.etree.ElementTree as etree
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / "lib/kofin/sync/fields.py"
    ).read_text()
    assert "EnableImageEnhancers" not in source
    assert "MaxHeight" in source and "Quality" in source

    root = etree.parse(
        str(Path(__file__).resolve().parents[2] / "resources/settings.xml")
    ).getroot()
    ids = {s.get("id") for s in root.iter("setting")}
    assert "enableCoverArt" not in ids
    assert {"compressArt", "maxArtResolution"} <= ids


def _repo_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[2]


def _declared_strings():
    import re

    text = (
        _repo_root() / "resources/language/resource.language.en_gb/strings.po"
    ).read_text()
    return {int(found) for found in re.findall(r'^msgctxt "#(\d+)"', text, re.M)}


def test_every_settings_label_resolves_to_a_string():
    """A settings id renders blank rather than failing when its string is
    missing, and Kodi caches the string table for the process lifetime — so
    a typo here is invisible until someone restarts Kodi and squints at an
    empty row. This pass reworded and renumbered a whole category.

    Only the 30000+ addon range is checked: Kodi-core ids (below 30000)
    resolve against Kodi's own table, which is not ours to enumerate.
    """
    import xml.etree.ElementTree as etree

    root = etree.parse(str(_repo_root() / "resources/settings.xml")).getroot()
    declared = _declared_strings()

    missing = []
    for element in root.iter():
        if element.tag not in ("category", "group", "setting", "option"):
            continue
        for attribute in ("label", "help"):
            value = element.get(attribute) or ""
            if value.isdigit() and int(value) >= 30000 and int(value) not in declared:
                missing.append(
                    "%s %s=%s" % (element.get("id") or element.tag, attribute, value)
                )
    assert missing == []


def test_the_retired_download_settings_are_gone_from_the_schema():
    """downloadsAutoCleanup and the three-way downloadsDeleteAfterPlay were
    replaced by one toggle plus its sub-toggle; a leftover control would go
    on being read by nothing."""
    import xml.etree.ElementTree as etree

    root = etree.parse(str(_repo_root() / "resources/settings.xml")).getroot()
    ids = {element.get("id") for element in root.iter("setting")}
    assert "downloadsAutoCleanup" not in ids
    assert "downloadsDeleteAfterPlay" not in ids
    assert {"downloadsDeleteAfterWatching", "downloadsDeleteAutomatically"} <= ids


def test_the_downloads_category_stays_a_single_group():
    """Every control there bar the master toggle is gated visible on
    downloadsEnabled, and Kodi does not recompute a *group's* visibility
    when a member's changes — so a group without downloadsEnabled in it
    stayed hidden until the dialog was rebuilt, which is what made half the
    page appear only after backing out and coming in again."""
    import xml.etree.ElementTree as etree

    root = etree.parse(str(_repo_root() / "resources/settings.xml")).getroot()
    category = next(
        element for element in root.iter("category") if element.get("id") == "downloads"
    )
    assert len(list(category.iter("group"))) == 1


def test_get_float_reads_the_fractional_bitrate_options():
    """downloadsMaxBitrate had to leave Kodi's integer type to offer 0.5 and
    0.75 Mbit/s; an unreadable or empty value means unlimited, which is what
    every caller already does with 0."""
    FakeAddon.store["downloadsMaxBitrate"] = "0.75"
    assert settings.get_float("downloadsMaxBitrate") == 0.75

    FakeAddon.store["downloadsMaxBitrate"] = "3"  # the pre-upgrade integer
    assert settings.get_float("downloadsMaxBitrate") == 3.0

    FakeAddon.store["downloadsMaxBitrate"] = ""
    assert settings.get_float("downloadsMaxBitrate") == 0.0

    FakeAddon.store["downloadsMaxBitrate"] = "unlimited"
    assert settings.get_float("downloadsMaxBitrate") == 0.0
