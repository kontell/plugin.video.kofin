"""The capability contract: the baseline refuses version-only compatibility,
and each feature is on by its own probes alone."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "or_capabilities", ROOT / "tools/check_or_capabilities.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
EVIDENCE = ROOT / "docs/research/kofin-or/phase0"


@pytest.fixture
def contract():
    return json.loads(
        (ROOT / "lib/kofin/sync/backends/api/requirements.json").read_text()
    )


@pytest.fixture
def capture():
    return json.loads((EVIDENCE / "capabilities.json").read_text())["result"]


@pytest.mark.parametrize("dirty", [False, True])
def test_build_provenance_does_not_block_capable_piers(capture, contract, dirty):
    from kofin.sync.backends.api.contract import check

    capture["application"]["version"]["revision"] = (
        "fixture-dirty" if dirty else "fixture"
    )
    for checker in (module.check, check):
        accepted = checker(capture, contract)
        assert accepted["interfaces_passed"]
        assert accepted["build_is_dirty"] is dirty
        assert accepted["behavioral_qualification_required"]
        assert accepted == checker(capture, contract, allow_dirty=True)


@pytest.mark.parametrize(
    "mutation",
    [
        "omega",
        "old_core",
        "missing_method",
        "missing_parameter",
        "missing_enum",
        "missing_python",
    ],
)
def test_incompatible_capture_is_rejected(capture, contract, mutation):
    changed = copy.deepcopy(capture)
    source = changed["methods"]["VideoLibrary.SetSourceContent"]
    if mutation == "omega":
        changed["application"]["version"]["major"] = 21
    elif mutation == "old_core":
        changed["system_addons"]["xbmc.addon"] = "21.0.1"
    elif mutation == "missing_method":
        del changed["methods"]["VideoLibrary.SetSourceContent"]
    elif mutation == "missing_parameter":
        source["params"] = [
            p for p in source["params"] if p["name"] != "containssingleitem"
        ]
    elif mutation == "missing_enum":
        next(p for p in source["params"] if p["name"] == "content")["enums"].remove(
            "tvshows"
        )
    else:
        changed["python_video_methods"]["setUniqueID"] = False
    from kofin.sync.backends.api.contract import check

    assert not module.check(changed, contract)["interfaces_passed"]
    assert not check(changed, contract)["interfaces_passed"]


def test_newer_clean_kodi_still_needs_behavioral_qualification(capture, contract):
    capture["application"]["version"].update(major=23, revision="upstream")
    result = module.check(capture, contract)
    assert result["interfaces_passed"]
    assert result["behavioral_qualification_required"]
    assert result["behavioral_gates"]


def test_the_piers_capture_passes_the_baseline_with_every_feature_off(
    capture, contract
):
    """The Piers capture is the baseline, and nothing more."""
    from kofin.sync.backends.api.contract import check

    result = check(capture, contract)
    assert result["interfaces_passed"]
    assert set(result["features"]) == set(contract["features"])
    assert not any(result["features"].values())
    assert set(result["missing"]) == set(contract["features"])


def test_each_feature_turns_on_by_its_own_probes(capture, contract):
    from kofin.sync.backends.api.contract import check

    changed = copy.deepcopy(capture)
    methods = changed["methods"]
    methods["VideoLibrary.SetSeasonDetails"]["params"].append({"name": "plot"})
    assert check(changed, contract)["features"]["season_plot"] is True
    methods["VideoLibrary.SetEpisodeDetails"]["params"].append({"name": "tag"})
    # The setter alone is not enough: a readback asks for the field by name,
    # and a Kodi whose enum lacks it refuses the whole call.
    assert check(changed, contract)["features"]["episode_tags"] is False
    changed["types"]["Video.Fields.Episode"]["items"]["enums"].append("tag")
    assert check(changed, contract)["features"]["episode_tags"] is True
    methods["VideoLibrary.RemoveMovieSet"] = {"params": [{"name": "setid"}]}
    changed["python_video_methods"].update(
        setHasVideoVersions=True, setIsDefaultVideoVersion=True
    )
    changed["python_stream_methods"] = {
        "AudioStreamDetail.setFlags": True,
        "SubtitleStreamDetail.setFlags": True,
    }
    changed["python_music_methods"]["setDateAdded"] = True
    changed["system_addons"]["xbmc.python"] = "3.2.0"
    methods["Player.SetTempo"] = {"params": [{"name": "tempo"}, {"name": "correction"}]}
    result = check(changed, contract)
    assert all(result["features"].values()), result["missing"]
    assert result["missing"] == {}


def test_a_proxy_feature_follows_what_it_stands_for(capture, contract):
    """asset_clean and tempo stand for behaviour Kodi cannot be asked about,
    and are read off the marks the change sets leave."""
    from kofin.sync.backends.api.contract import check

    changed = copy.deepcopy(capture)
    changed["system_addons"]["xbmc.python"] = "3.1.1"  # stock Kodi 23
    assert check(changed, contract)["features"]["asset_clean"] is False
    changed["methods"]["Player.SetTempo"] = {"params": [{"name": "tempo"}]}
    assert check(changed, contract)["features"]["tempo"] is False
