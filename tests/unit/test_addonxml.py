"""The Advanced-tab toggle rewrites addon.xml's <reuselanguageinvoker>."""

from kofin.core import addonxml

SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<addon id="plugin.video.kofin">
  <extension point="xbmc.addon.metadata">
    <!-- keep the surrounding comment -->
    <reuselanguageinvoker>true</reuselanguageinvoker>
    <platform>all</platform>
  </extension>
</addon>
"""


def test_with_reuse_invoker_flips_only_the_tag():
    off = addonxml.with_reuse_invoker(SAMPLE, False)
    assert off is not None
    assert "<reuselanguageinvoker>false</reuselanguageinvoker>" in off
    assert "keep the surrounding comment" in off
    assert addonxml.with_reuse_invoker(off, True) == SAMPLE


def test_with_reuse_invoker_returns_none_when_the_tag_is_missing():
    assert addonxml.with_reuse_invoker("<addon/>", True) is None


def test_apply_writes_and_is_idempotent(tmp_path):
    path = tmp_path / "addon.xml"
    path.write_text(SAMPLE, encoding="utf-8")

    assert addonxml.apply(False, str(path)) is True
    assert "<reuselanguageinvoker>false</reuselanguageinvoker>" in path.read_text(
        encoding="utf-8"
    )
    assert addonxml.apply(False, str(path)) is False
    assert addonxml.apply(True, str(path)) is True
    assert path.read_text(encoding="utf-8") == SAMPLE


def test_apply_returns_none_when_the_file_is_missing(tmp_path):
    assert addonxml.apply(False, str(tmp_path / "missing.xml")) is None


def test_apply_returns_none_when_the_tag_is_absent(tmp_path):
    path = tmp_path / "addon.xml"
    path.write_text("<addon/>\n", encoding="utf-8")
    assert addonxml.apply(True, str(path)) is None
    assert path.read_text(encoding="utf-8") == "<addon/>\n"
