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


def test_a_version_2_document_needs_its_identity_fields():
    """The schema's draft-07 conditional, which the validator here evaluates:
    version 2 is the version that names the namespace, the device and the
    kinds, and a document claiming it without them is not one."""
    document = {
        "v": 2,
        "generation": 1,
        "namespace": "ns",
        "device": "dev",
        "kinds": [],
        "requests": [],
    }
    contractschema.check_document(document, SCHEMA)
    for field in ("namespace", "device", "kinds"):
        broken = {k: v for k, v in document.items() if k != field}
        try:
            contractschema.check_document(broken, SCHEMA)
        except AssertionError as error:
            assert "missing %r" % field in str(error)
        else:
            raise AssertionError("a version 2 document without %s passed" % field)
    contractschema.check_document({"v": 1, "generation": 1, "requests": []}, SCHEMA)


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


def test_a_malformed_status_is_summarized_never_raised(profile, monkeypatch):
    """The status is another add-on's file and the summary is read while
    the backend is prepared at start: a field of the wrong type is the
    field left out, never an exception that leaves native sync off."""
    broken = {
        "generation": 5,
        "kinds": 7,
        "summary": ["done"],
        "gate": {"video": 149, "music": {"version": 84, "ok": 1}},
        "requests": {"r1": "done", "r2": {"state": "refused", "reason": None}},
    }
    assert companion.status_summary(broken) == {
        "generation": 5,
        "updated": None,
        "idle": None,
        "kinds": [],
        "gate": {"music": {"version": 84, "ok": True}},
        "summary": {},
        "refused": {"r2": ""},
        "waiting": {},
    }
    assert companion.status_summary({"summary": {"done": "many", "refused": 2}})[
        "summary"
    ] == {"refused": 2}
    with open(companion.status_path(), "w", encoding="utf-8") as handle:
        json.dump(broken, handle)
    monkeypatch.setattr(
        companion.kodirpc,
        "call",
        lambda method, params: {"addon": {"enabled": True, "version": "0.1.0"}},
    )
    assert companion.status_block()["status"]["kinds"] == []
    lines = []
    monkeypatch.setattr(
        companion.LOG, "info", lambda msg, *args: lines.append(msg % args)
    )
    companion.absorb_status(set(), {"backend": "api"})
    assert lines[0] == "companion status: generation 5, 0 done, 0 waiting, 0 refused"


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


def _present(monkeypatch, version="0.1.0"):
    monkeypatch.setattr(
        companion.kodirpc,
        "call",
        lambda method, params: (
            {"addon": {"enabled": version is not None, "version": version or ""}}
        ),
    )


def _write_requests(generation):
    os.makedirs(os.path.dirname(companion.requests_path()), exist_ok=True)
    with open(companion.requests_path(), "w", encoding="utf-8") as handle:
        json.dump({"v": 2, "generation": generation, "requests": []}, handle)


def test_a_status_that_is_not_this_runs_counts_as_absent(profile, monkeypatch):
    """Plan section 8, "A stale status". The status file outlives an
    uninstall, and a status that answered an older generation says nothing
    about the document being served now."""
    with open(companion.status_path(), "w", encoding="utf-8") as handle:
        json.dump(_status(), handle)  # generation 4
    _present(monkeypatch, None)
    assert companion.status_block() == {
        "addon": companion.ADDON_ID,
        "version": None,
        "status": {},
    }
    _present(monkeypatch)
    _write_requests(4)
    assert companion.status_block()["status"]["generation"] == 4
    _write_requests(5)
    assert companion.status_block()["status"] == {}
    # An idle status names no generation: it is about the companion, not
    # about a document, and is kept.
    with open(companion.status_path(), "w", encoding="utf-8") as handle:
        json.dump(
            _status(idle="document version 9 is not served", generation=None), handle
        )
    assert companion.status_block()["status"]["idle"].startswith("document version")


def test_absorb_folds_the_status_into_the_report_and_logs_each_refusal_once(
    profile, monkeypatch
):
    lines = []
    monkeypatch.setattr(
        companion.LOG, "info", lambda msg, *args: lines.append(msg % args)
    )
    _present(monkeypatch)
    # On disk: the previous run's report. In hand: the one this run wrote.
    with open(companion.report_path(), "w", encoding="utf-8") as handle:
        json.dump({"backend": "api", "private_state": "fresh library required"}, handle)
    mine = {"backend": "api", "private_state": "ready"}
    with open(companion.status_path(), "w", encoding="utf-8") as handle:
        json.dump(_status(), handle)
    seen = set()
    companion.absorb_status(seen, mine)
    companion.absorb_status(seen, mine)
    with open(companion.report_path(), encoding="utf-8") as handle:
        report = json.load(handle)
    # The file is written from this run's report, never read back: the
    # previous run's fields are not restored by a status ping.
    assert report["backend"] == "api" and report["private_state"] == "ready"
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
    companion.absorb_status(seen, mine)
    assert (
        lines[-1] == "companion refused request r2: gate: MyVideos v150 is not served"
    )
    # Idle is logged once per reason, and the report still carries it.
    with open(companion.status_path(), "w", encoding="utf-8") as handle:
        json.dump(
            _status(idle="kofin's sql build writes its own rows", generation=None),
            handle,
        )
    companion.absorb_status(seen, mine)
    companion.absorb_status(seen, mine)
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
    # Before this run has written its report nothing is written at all,
    # whatever an earlier run left on disk.
    with open(companion.report_path(), "w", encoding="utf-8") as handle:
        json.dump({"backend": "api", "private_state": "stale"}, handle)
    companion.absorb_status(set())
    with open(companion.report_path(), encoding="utf-8") as handle:
        assert json.load(handle) == {"backend": "api", "private_state": "stale"}


def test_the_service_reads_a_status_ping_from_the_companion_only(monkeypatch, tmp_path):
    from kofin import buildconfig
    from kofin.service.main import Service

    monkeypatch.setattr(
        "xbmcvfs.translatePath", lambda path: str(tmp_path / "ipc.nonce")
    )
    monkeypatch.setattr(buildconfig, "BACKEND", "api")
    absorbed = []
    monkeypatch.setattr(
        companion, "absorb_status", lambda seen, report=None: absorbed.append(seen)
    )
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


def test_kofins_own_ping_to_the_companion_is_not_an_ipc_command(monkeypatch, tmp_path):
    from kofin.core import ipc
    from kofin.service.main import Service

    monkeypatch.setattr(
        "xbmcvfs.translatePath", lambda path: str(tmp_path / "ipc.nonce")
    )
    service = Service()
    lines = []
    monkeypatch.setattr(
        "kofin.service.main.LOG.warning", lambda msg, *a: lines.append(msg % a)
    )
    monkeypatch.setattr(
        "kofin.service.main.LOG.debug", lambda msg, *a: lines.append(msg % a)
    )
    service.onNotification(
        ipc.SENDER, "Other.Companion.Requests", '{"v": 2, "generation": 3}'
    )
    assert lines == []
