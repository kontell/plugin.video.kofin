"""The capability report, as the rest of the add-on reads it."""

import json

import pytest

from kofin.sync.backends.api import capabilities


@pytest.fixture
def profile(monkeypatch, tmp_path):
    monkeypatch.setattr(capabilities.private, "addon_data_path", lambda: str(tmp_path))
    return tmp_path


def _write(profile, document):
    (profile / capabilities.REPORT).write_text(json.dumps(document), encoding="utf-8")


def test_every_feature_is_off_until_the_probe_has_written_a_report(profile):
    assert capabilities.table() == {name: False for name in capabilities.FEATURES}
    assert capabilities.features() == frozenset()
    assert capabilities.has("season_plot") is False


def test_the_report_names_what_this_kodi_has(profile):
    _write(profile, {"features": {"season_plot": True, "tempo": False, "other": True}})
    assert capabilities.has("season_plot") is True
    assert capabilities.has("tempo") is False
    # A name the build does not know is not a feature of it.
    assert capabilities.features() == frozenset({"season_plot"})
    assert capabilities.table()["remove_set"] is False


@pytest.mark.parametrize("document", ["[]", '{"features": 1}', "not json", ""])
def test_a_report_of_another_shape_reads_as_the_baseline(profile, document):
    (profile / capabilities.REPORT).write_text(document, encoding="utf-8")
    assert capabilities.features() == frozenset()


def test_the_feature_labels_cover_the_contract():
    assert set(capabilities.FEATURES) == set(capabilities.contract()["features"])


def test_the_summary_names_the_off_features_with_their_reasons():
    report = {
        "features": {name: name == "tempo" for name in capabilities.FEATURES},
        "missing": {"season_plot": ["missing parameter X.plot"]},
    }
    line = capabilities.summary(report)
    assert line.startswith("on: tempo; off: season_plot (missing parameter X.plot), ")
