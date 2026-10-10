"""The companion's status, as kofin-or reads it: folded into the capability
report, each refusal logged once, and the ping that prompts it."""

import json
import os

import pytest

from kofin.sync import private
from kofin.sync.backends.api import companion
from tests.unit import contractschema

SCHEMA = contractschema.load(companion.SCHEMA_PATH)


def _status(**values):
    document = {
        "v": 1,
        "companion": "0.1.0",
        "contract": [1, 2],
        "kinds": ["group_versions"],
        "trigger": "ping",
        "updated": "2026-10-10T20:00:00",
        "generation": 4,
        "busy": False,
        "gate": {
            "video": {"version": 149, "ok": True, "reason": ""},
            "music": {"version": 84, "ok": True, "reason": ""},
            "texture": {"version": 13, "ok": False, "reason": "unsupported"},
        },
        "requests": {
            "r1": {"state": "done", "reason": "", "hash": "a", "at": "t"},
            "r2": {"state": "refused", "reason": "not owned", "hash": "b", "at": "t"},
            "r3": {"state": "waiting", "reason": "scan", "hash": "c", "at": "t"},
        },
        "summary": {"done": 1, "waiting": 1, "refused": 1},
    }
    document.update(values)
    return document


@pytest.fixture
def profile(tmp_path, monkeypatch):
    kofin = tmp_path / "addon_data" / "plugin.video.kofin"
    kofin.mkdir(parents=True)
    (tmp_path / "addon_data" / companion.ADDON_ID).mkdir(parents=True)
    monkeypatch.setattr(private, "addon_data_path", lambda: str(kofin))
    monkeypatch.setattr(companion.kodirpc, "call", lambda method, params: {})
    return tmp_path


def test_the_sample_status_is_what_the_schema_describes():
    contractschema.check_status(_status(), SCHEMA)


def test_status_summary_keeps_what_the_report_needs():
    summary = companion.status_summary(_status())
    assert summary["generation"] == 4 and summary["kinds"] == ["group_versions"]
    assert summary["gate"] == {
        "video": {"version": 149, "ok": True},
        "music": {"version": 84, "ok": True},
        "texture": {"version": 13, "ok": False},
    }
    assert summary["summary"] == {"done": 1, "waiting": 1, "refused": 1}
    assert summary["refused"] == {"r2": "not owned"}
    assert summary["waiting"] == {"r3": "scan"}
    assert summary["idle"] is None
    assert companion.status_summary(None) == {}
    assert companion.status_summary({"v": 1, "requests": "no"})["refused"] == {}


def test_status_block_reads_the_file_beside_kofins_profile_data(profile, monkeypatch):
    assert companion.status_path() == str(
        profile / "addon_data" / companion.ADDON_ID / "status.json"
    )
    block = companion.status_block()
    assert block == {"addon": companion.ADDON_ID, "version": None, "status": {}}
    with open(companion.status_path(), "w", encoding="utf-8") as handle:
        json.dump(_status(), handle)
    monkeypatch.setattr(
        companion.kodirpc,
        "call",
        lambda method, params: {"addon": {"enabled": True, "version": "0.1.0"}},
    )
    block = companion.status_block()
    assert block["version"] == "0.1.0" and block["status"]["generation"] == 4


def test_absorb_folds_the_status_into_the_report_and_logs_each_refusal_once(
    profile, monkeypatch
):
    lines = []
    monkeypatch.setattr(
        companion.LOG, "info", lambda msg, *args: lines.append(msg % args)
    )
    with open(companion.report_path(), "w", encoding="utf-8") as handle:
        json.dump(
            {"backend": "api", "companion": {"addon": companion.ADDON_ID}}, handle
        )
    with open(companion.status_path(), "w", encoding="utf-8") as handle:
        json.dump(_status(), handle)
    seen = set()
    companion.absorb_status(seen)
    companion.absorb_status(seen)
    with open(companion.report_path(), encoding="utf-8") as handle:
        report = json.load(handle)
    assert report["backend"] == "api"
    assert report["companion"]["status"]["refused"] == {"r2": "not owned"}
    assert not os.path.exists(companion.report_path() + ".part")
    assert lines.count("companion refused request r2: not owned") == 1
    assert (
        lines.count("companion status: generation 4, 1 done, 1 waiting, 1 refused") == 2
    )
    # A new reason for the same request is news again.
    with open(companion.status_path(), "w", encoding="utf-8") as handle:
        json.dump(
            _status(
                requests={
                    "r2": {
                        "state": "refused",
                        "reason": "gate: MyVideos v150 is not served",
                        "hash": "b",
                    }
                }
            ),
            handle,
        )
    companion.absorb_status(seen)
    assert (
        lines[-1] == "companion refused request r2: gate: MyVideos v150 is not served"
    )
    # Idle is logged once per reason, and the report still carries it.
    with open(companion.status_path(), "w", encoding="utf-8") as handle:
        json.dump(
            _status(idle="kofin's sql build writes its own rows", generation=None),
            handle,
        )
    companion.absorb_status(seen)
    companion.absorb_status(seen)
    assert lines.count("companion idle: kofin's sql build writes its own rows") == 1
    with open(companion.report_path(), encoding="utf-8") as handle:
        assert json.load(handle)["companion"]["status"]["idle"].startswith(
            "kofin's sql"
        )


def test_absorb_without_a_report_or_status_is_quiet(profile, monkeypatch):
    lines = []
    monkeypatch.setattr(
        companion.LOG, "info", lambda msg, *args: lines.append(msg % args)
    )
    block = companion.absorb_status(set())
    assert block["status"] == {} and lines == []
    assert not os.path.exists(companion.report_path())


def test_the_service_reads_a_status_ping_from_the_companion_only(monkeypatch, tmp_path):
    from kofin import buildconfig
    from kofin.service.main import Service

    monkeypatch.setattr(
        "xbmcvfs.translatePath", lambda path: str(tmp_path / "ipc.nonce")
    )
    monkeypatch.setattr(buildconfig, "BACKEND", "api")
    absorbed = []
    monkeypatch.setattr(companion, "absorb_status", lambda seen: absorbed.append(seen))
    service = Service()
    ping = '{"v": 1, "generation": 3, "path": "/x/status.json"}'
    service.onNotification("someone.else", "Other.Companion.Status", ping)
    assert absorbed == []
    service.onNotification(companion.ADDON_ID, "Other.Companion.Status", ping)
    assert absorbed == [service._companion_seen]
    # The SQL build has no companion module and no status to read.
    monkeypatch.setattr(buildconfig, "BACKEND", "sql")
    service.onNotification(companion.ADDON_ID, "Other.Companion.Status", ping)
    assert len(absorbed) == 1
