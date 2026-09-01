"""Gate the upstream schema watcher (``tools/check_kodi_schema.py``).

The watcher's one real failure mode is reporting "nothing new" when it has in
fact lost track of the constant: Kodi has moved ``GetSchemaVersion`` between
files twice already, and a rename that read as good news would leave the watch
green and blind for a whole release cycle. So the tests that matter here are
the extraction against every form the constant has actually taken, and the
assertion that a miss raises.
"""

import sys
from pathlib import Path

import pytest

from kofin.sync import schema

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import check_kodi_schema as watch  # noqa: E402

# Kodi 21 (Omega), xbmc/video/VideoDatabase.cpp.
OUT_OF_LINE = """\
void CVideoDatabase::UpdateTables(int iVersion)
{
  if (iVersion < 131)
    m_pDS->exec("ALTER TABLE settings ADD Something INTEGER");
}

int CVideoDatabase::GetSchemaVersion() const
{
  return 131;
}

int CVideoDatabase::GetSomethingElse() const
{
  return 999;
}
"""

# Kodi 22 development, xbmc/video/VideoDatabase.h -- the trap is two lines
# above the constant, and matching it would report a version from 2015.
HEADER = """\
  int GetMinSchemaVersion() const override { return 75; }
  int GetSchemaVersion() const override { return 14; }
"""


def test_reads_the_out_of_line_definition():
    assert watch._version_from(OUT_OF_LINE, "CVideoDatabase") == 131


def test_does_not_run_on_into_the_next_function():
    """The window after the anchor is bounded, so the 999 below is not read."""
    assert watch._version_from(OUT_OF_LINE, "CVideoDatabase") != 999


def test_reads_the_inline_declaration():
    assert watch._version_from(HEADER, "") == 14


def test_min_schema_version_is_not_mistaken_for_the_schema_version():
    only_min = "  int GetMinSchemaVersion() const override { return 75; }\n"
    assert watch._version_from(only_min, "") is None


def test_a_missing_constant_reads_as_absent_not_as_zero():
    assert (
        watch._version_from("int Unrelated() { return 5; }", "CVideoDatabase") is None
    )


def test_the_ast_reader_agrees_with_the_real_schema_module():
    """The watcher parses schema.py rather than importing it (no Kodistubs in
    the CI container), so the two readings have to be pinned together."""
    assert watch.supported() == {k: v for k, v in schema.SUPPORTED.items() if v}
    assert watch.prefixes() == schema.PREFIXES


def test_every_gated_kind_has_somewhere_to_look():
    assert set(watch.CANDIDATES) == set(schema.SUPPORTED)


def fake_upstream(monkeypatch, versions):
    """Serve one file per kind, keyed (ref, kind) -> version."""
    paths = {kind: watch.CANDIDATES[kind][0][0] for kind in watch.CANDIDATES}
    classes = {kind: watch.CANDIDATES[kind][0][1] for kind in watch.CANDIDATES}

    def fetch(url):
        ref, path = url.split("/xbmc/xbmc/", 1)[1].split("/", 1)
        for kind, candidate in paths.items():
            if candidate == path:
                version = versions.get((ref, kind))
                if version is None:
                    return None
                cls = classes[kind]
                if not cls:
                    # Texture's constant is inline in the class body.
                    return (
                        "  int GetMinSchemaVersion() const override { return 1; }\n"
                        "  int GetSchemaVersion() const override { return %d; }\n"
                        % version
                    )
                return "int %s::GetSchemaVersion() const\n{\n  return %d;\n}\n" % (
                    cls,
                    version,
                )
        return None

    monkeypatch.setattr(watch, "_fetch", fetch)


def test_a_newer_version_is_a_finding(monkeypatch):
    newest = max(schema.SUPPORTED["video"]) + 1
    fake_upstream(
        monkeypatch,
        {
            ("master", "video"): newest,
            ("master", "music"): max(schema.SUPPORTED["music"]),
            ("master", "texture"): max(schema.SUPPORTED["texture"]),
        },
    )
    findings, observations = watch.check(["master"])
    assert [f["version"] for f in findings] == [newest]
    assert findings[0]["kind"] == "video"
    assert sum(1 for o in observations if not o["supported"]) == 1


def test_a_supported_version_is_not_a_finding(monkeypatch):
    fake_upstream(
        monkeypatch,
        {
            ("Omega", kind): max(versions)
            for kind, versions in schema.SUPPORTED.items()
            if versions
        },
    )
    findings, _ = watch.check(["Omega"])
    assert findings == []


def test_an_older_unknown_version_is_not_a_finding(monkeypatch):
    """A Kodi below the floor is a Kodi kofin has already declined to support,
    not a bump to chase."""
    fake_upstream(
        monkeypatch,
        {
            ("Nexus", "video"): min(schema.SUPPORTED["video"]) - 10,
            ("Nexus", "music"): min(schema.SUPPORTED["music"]) - 1,
            ("Nexus", "texture"): min(schema.SUPPORTED["texture"]),
        },
    )
    findings, observations = watch.check(["Nexus"])
    assert findings == []
    assert any(not o["supported"] for o in observations)


def test_one_version_on_two_refs_files_one_issue(monkeypatch):
    """Once Piers branches, master and Piers carry the same version for a
    while. That is one finding with two refs, and one stable title."""
    newest = max(schema.SUPPORTED["video"]) + 1
    versions = {}
    for ref in ("master", "Piers"):
        versions[(ref, "video")] = newest
        versions[(ref, "music")] = max(schema.SUPPORTED["music"])
        versions[(ref, "texture")] = max(schema.SUPPORTED["texture"])
    fake_upstream(monkeypatch, versions)

    findings, _ = watch.check(["master", "Piers"])
    assert len(findings) == 1
    assert findings[0]["refs"] == ["master", "Piers"]
    assert watch.issue_title(findings[0]) == watch.issue_title(
        dict(findings[0], refs=["Piers"])
    )


def test_the_constant_having_moved_raises_rather_than_passing(monkeypatch):
    """The whole point: a rename must go red, not quiet."""
    monkeypatch.setattr(watch, "_fetch", lambda url: None)
    with pytest.raises(watch.ProbeError) as excinfo:
        watch.probe("master", "video")
    assert "CANDIDATES" in str(excinfo.value)


def test_the_issue_body_names_the_version_and_carries_its_marker():
    finding = {
        "kind": "video",
        "version": 149,
        "name": "MyVideos149",
        "path": "xbmc/video/VideoDatabaseMigration.cpp",
        "refs": ["master"],
        "supported": sorted(schema.SUPPORTED["video"]),
    }
    body = watch.issue_body(finding)
    assert "MyVideos149" in body
    assert "myvideos149-gate.md" in body
    assert watch.marker(finding) in body
    assert "tests/unit/kodifixtures.py" in body


@pytest.mark.parametrize("name", ["Omega", "Piers", "Zephyr"])
def test_release_branches_are_discovered(name):
    assert watch.BRANCH_RE.match(name)


@pytest.mark.parametrize("name", ["Nexus", "Matrix", "Krypton", "Gotham_ios8", "main"])
def test_branches_below_the_floor_are_not_probed(name):
    assert not watch.BRANCH_RE.match(name)


def test_a_dev_line_finding_says_it_may_move_again():
    finding = {"refs": ["master"]}
    assert "development line only" in watch.reach(finding)


def test_a_release_branch_finding_says_users_will_run_it(monkeypatch):
    """Once Piers branches, a bump landing there is the one that ships."""
    finding = {"refs": ["master", "Piers"]}
    text = watch.reach(finding)
    assert "release branch" in text
    assert "`Piers`" in text
    assert "`master`" not in text
