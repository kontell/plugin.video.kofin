"""The research gate must reject misleading version-only compatibility."""

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
    return json.loads((EVIDENCE / "requirements.json").read_text())


@pytest.fixture
def capture():
    return json.loads((EVIDENCE / "capabilities.json").read_text())["result"]


def test_dirty_capture_needs_explicit_research_exception(capture, contract):
    assert not module.check(capture, contract)["interfaces_passed"]
    accepted = module.check(capture, contract, allow_dirty=True)
    assert accepted["interfaces_passed"]
    assert accepted["dirty_build_exception_used"]
    assert not accepted["stock_release_qualified"]


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
    assert not module.check(changed, contract, allow_dirty=True)["interfaces_passed"]


def test_newer_clean_kodi_still_needs_behavioral_qualification(capture, contract):
    capture["application"]["version"].update(major=23, revision="upstream")
    result = module.check(capture, contract)
    assert result["interfaces_passed"]
    assert not result["stock_release_qualified"]
    assert result["behavioral_gates"]
