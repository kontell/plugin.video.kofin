#!/usr/bin/env python3
"""Watch upstream Kodi for a database schema version newer than the gate.

``lib/kofin/sync/schema.py`` refuses to write any MyVideos/MyMusic/Textures
version not in its ``SUPPORTED`` map, so a Kodi that bumps one takes kofin's
write sync offline until the version is fixture-backed and admitted. That is
the correct failure, but it is a *silent* one until a user hits it: nothing in
this repo watches upstream. This script does, and
``.github/workflows/kodi-schema-watch.yml`` runs it on a schedule and opens an
issue per new version.

Two things it deliberately does not do:

* It does not guess. Every version it reports is read out of Kodi's own
  source at a named ref, and if it cannot find the constant where it expects
  one it **fails** rather than reporting "nothing new" -- the constant has
  moved file twice already (Omega has it in ``VideoDatabase.cpp``, master in
  ``VideoDatabaseMigration.cpp``), and a rename that read as "up to date"
  would disable the watch permanently with a green tick. Same shape as the
  rule above ``downloader._get_items``: a swallowed error here reads
  downstream as good news.
* It does not open the gate, and the issue it files does not either. Admitting
  a version means dumping fixtures from a real install and passing the L2
  suite against them (``docs/myvideos148-gate.md`` is the worked example).

Usage:

    tools/check_kodi_schema.py                 # human summary
    tools/check_kodi_schema.py --json out.json # machine-readable findings
    tools/check_kodi_schema.py --ref master    # probe one ref only
    tools/check_kodi_schema.py --fail-on-new   # exit 3 when something is new
"""

import argparse
import ast
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PY = ROOT / "lib" / "kofin" / "sync" / "schema.py"

REPO = "xbmc/xbmc"
RAW = "https://raw.githubusercontent.com/%s/%%s/%%s" % REPO
API = "https://api.github.com/repos/%s" % REPO

USER_AGENT = "kofin-schema-watch"
TIMEOUT = 30
RETRIES = 3

# Release branches are named after the codename and the codenames advance
# alphabetically (… Matrix, Nexus, Omega, Piers, …), so this picks up a branch
# that does not exist yet -- Piers has not been cut at the time of writing and
# Kodi 22 development still lives on master -- without anyone remembering to
# add it. The floor is Omega because everything below it is a Kodi kofin does
# not support and never will; those branches are frozen at versions *older*
# than the gate, which the comparison ignores anyway.
BRANCH_RE = re.compile(r"^[O-Z][a-z]+$")
ALWAYS_REFS = ("master",)

# Where each kind's schema constant lives, newest known location first. A miss
# on all candidates is an error, never a pass -- see the module docstring.
#
#   member: ``int CXDatabase::GetSchemaVersion() const { return N; }``
#   inline: ``int GetSchemaVersion() const override { return N; }`` in the
#           class body, which is how TextureDatabase still declares it.
CANDIDATES: Dict[str, Tuple[Tuple[str, str], ...]] = {
    "video": (
        ("xbmc/video/VideoDatabaseMigration.cpp", "CVideoDatabase"),
        ("xbmc/video/VideoDatabase.cpp", "CVideoDatabase"),
        ("xbmc/video/VideoDatabase.h", ""),
    ),
    "music": (
        ("xbmc/music/MusicDatabaseMigration.cpp", "CMusicDatabase"),
        ("xbmc/music/MusicDatabase.cpp", "CMusicDatabase"),
        ("xbmc/music/MusicDatabase.h", ""),
    ),
    "texture": (
        ("xbmc/TextureDatabase.h", ""),
        ("xbmc/TextureDatabaseMigration.cpp", "CTextureDatabase"),
        ("xbmc/TextureDatabase.cpp", "CTextureDatabase"),
    ),
}

# ``int GetSchemaVersion() const override { return 14; }``. The literal
# "GetSchemaVersion" after "int " is what keeps GetMinSchemaVersion -- the
# floor for migration, declared two lines above it in every one of these
# headers -- from matching.
INLINE_RE = re.compile(
    r"\bint\s+GetSchemaVersion\s*\(\s*\)\s*const\s*(?:override\s*)?\{\s*return\s+(\d+)"
)


class ProbeError(Exception):
    """Upstream could not be read, or the constant was not where it should be."""


def _fetch(url: str) -> Optional[str]:
    """GET ``url``; None on 404, raise on anything else.

    404 is the expected answer for a candidate path that does not exist on
    this ref (``VideoDatabaseMigration.cpp`` on Omega); every other failure is
    a failure.
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last: Optional[Exception] = None
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            last = exc
        except OSError as exc:
            # URLError and socket timeouts are both OSError subclasses; the
            # HTTPError leg above runs first, so this is everything else.
            last = exc
        if attempt < RETRIES - 1:
            time.sleep(2 * (attempt + 1))
    raise ProbeError("could not fetch %s: %s" % (url, last))


def _version_from(text: str, cls: str) -> Optional[int]:
    """The schema version in one source file, or None if it is not in there."""
    if cls:
        anchor = text.find("%s::GetSchemaVersion" % cls)
        if anchor < 0:
            return None
        # The body follows within a couple of lines; bounding the window keeps
        # this from picking up the next function's return.
        found = re.search(r"return\s+(\d+)\s*;", text[anchor : anchor + 400])
        return int(found.group(1)) if found else None
    found = INLINE_RE.search(text)
    return int(found.group(1)) if found else None


def probe(ref: str, kind: str) -> Tuple[int, str]:
    """(version, path) for one kind at one ref. Raises if it cannot be read."""
    tried: List[str] = []
    for path, cls in CANDIDATES[kind]:
        text = _fetch(RAW % (ref, path))
        tried.append(path)
        if text is None:
            continue
        version = _version_from(text, cls)
        if version is not None:
            return version, path
    raise ProbeError(
        "no %s schema version found at %s (looked in %s). The constant has "
        "moved -- add its new home to CANDIDATES in this script; do not treat "
        "this as 'nothing new'." % (kind, ref, ", ".join(tried))
    )


def _literal(name: str) -> object:
    """Read a module-level literal out of schema.py without importing it.

    schema.py imports xbmcvfs, so importing it here would drag Kodistubs into
    a script whose whole job is to run in a bare CI container.
    """
    tree = ast.parse(SCHEMA_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        targets: Sequence[ast.expr] = ()
        value: Optional[ast.expr] = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        for target in targets:
            if isinstance(target, ast.Name) and target.id == name:
                assert value is not None
                return ast.literal_eval(value)
    raise ProbeError("%s not found in %s" % (name, SCHEMA_PY))


def supported() -> Dict[str, set]:
    raw = _literal("SUPPORTED")
    assert isinstance(raw, dict)
    return {k: set(v) for k, v in raw.items() if v}


def prefixes() -> Dict[str, str]:
    """kind -> the MyVideos/MyMusic/Textures name users and issues see."""
    raw = _literal("PREFIXES")
    assert isinstance(raw, dict)
    return raw


def _api(path: str, token: Optional[str]) -> object:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = "Bearer %s" % token
    req = urllib.request.Request(API + path, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise ProbeError("GitHub API %s failed: %s" % (path, exc)) from exc


def discover_refs(token: Optional[str]) -> List[str]:
    """master plus every release branch from Omega on."""
    branches = _api("/branches?per_page=100", token)
    assert isinstance(branches, list)
    names = {b["name"] for b in branches if isinstance(b, dict)}
    if not names:
        raise ProbeError("the branch listing came back empty")
    found = sorted(n for n in names if BRANCH_RE.match(n))
    return [r for r in ALWAYS_REFS if r in names] + found


def check(refs: Sequence[str]) -> Tuple[List[dict], List[dict]]:
    """(findings, observations) -- one observation per ref/kind probed."""
    gate = supported()
    names = prefixes()
    observations: List[dict] = []
    new: Dict[Tuple[str, int], dict] = {}
    for ref in refs:
        for kind in sorted(CANDIDATES):
            version, path = probe(ref, kind)
            known = version in gate.get(kind, set())
            observations.append(
                {
                    "ref": ref,
                    "kind": kind,
                    "name": "%s%d" % (names[kind], version),
                    "version": version,
                    "path": path,
                    "supported": known,
                }
            )
            # Only a version *newer* than everything in the gate is news. An
            # older unknown one would mean a Kodi kofin has already declined
            # to support, not a bump to chase.
            if known or version <= max(gate.get(kind, {0})):
                continue
            entry = new.setdefault(
                (kind, version),
                {
                    "kind": kind,
                    "version": version,
                    "name": "%s%d" % (names[kind], version),
                    "path": path,
                    "refs": [],
                    "supported": sorted(gate.get(kind, set())),
                },
            )
            entry["refs"].append(ref)
    return [new[k] for k in sorted(new)], observations


def issue_title(finding: dict) -> str:
    """Stable per (kind, version) -- the workflow dedupes on it, so the same
    version appearing on a second branch must not change it."""
    return "Schema gate: Kodi ships %s" % finding["name"]


def issue_body(finding: dict) -> str:
    refs = ", ".join("`%s`" % r for r in finding["refs"])
    supported = ", ".join(str(v) for v in finding["supported"])
    return """\
Upstream Kodi has bumped the **{kind}** database schema to **{name}**, which is
newer than anything `sync/schema.py` admits (`SUPPORTED["{kind}"]` = {{{supported}}}).

| | |
|---|---|
| Seen on | {refs} |
| Read from | [`{path}`](https://github.com/xbmc/xbmc/blob/{first}/{path}) ([history](https://github.com/xbmc/xbmc/commits/{first}/{path})) |
| Reach | {reach} |

Until the version is admitted, a Kodi carrying it runs kofin with **write sync
disabled** — the gate refuses, the library manager raises one notification and
the Library tab status line explains. That is the designed behaviour, not a
regression; this issue is the prompt to do the work that opens the gate.

### Opening the gate

Per `CLAUDE.md` (**The schema gate**) — do not skip to the last step:

- [ ] Dump `.schema` fixtures from a real, untouched install into
      `tests/fixtures/` (`{name_lower}.sql` and its `_seed.sql`).
- [ ] Extract the creation-time seed rows.
- [ ] Add the version to the L2 parameterization in `tests/unit/kodifixtures.py`
      and `tests/unit/test_sync_writers.py`.
- [ ] Key any version-dependent constants in `schema.py` — `EXTRA_ITEM_TYPE`
      for video, `MUSIC_SEED_SQL` for music, `CHAPTER_ART_WRAPPED` for texture.
      `test_sync_schema.py` refuses a `SUPPORTED` entry without them.
- [ ] Confirm the suite passes, then add {version} to `SUPPORTED["{kind}"]`.
- [ ] Write up what the bump moves, as `docs/{name_lower}-gate.md`.

Check what the bump actually does to the DDL before reusing an older fixture:
`docs/myvideos147-gate.md` is the worked example of a data-only bump that could
reuse one, and `docs/myvideos148-gate.md` of one that could not. The shortcut is
not available by analogy.

<!-- {marker} -->
""".format(
        kind=finding["kind"],
        name=finding["name"],
        name_lower=finding["name"].lower(),
        version=finding["version"],
        supported=supported,
        refs=refs,
        reach=reach(finding),
        path=finding["path"],
        first=finding["refs"][0],
        marker=marker(finding),
    )


def reach(finding: dict) -> str:
    """How much of a hurry this is.

    A version on the development line alone will very likely move again before
    it ships -- Piers went through five video versions between a1 and b2 -- so
    fixtures dumped today may be superseded. One that has reached a release
    branch is the one users will run, and is frozen: no point release has ever
    bumped a schema version (19.0-19.5 all MyVideos119, 20.0-20.5 all 121,
    21.0-21.3 all 131).
    """
    branches = [r for r in finding["refs"] if r not in ALWAYS_REFS]
    if not branches:
        return (
            "development line only (%s) — likely to move again before it ships, "
            "so fixtures dumped now may not be the shipping schema"
            % ", ".join("`%s`" % r for r in finding["refs"])
        )
    return (
        "**on a release branch** (%s) — frozen for that series, and what users "
        "will actually run" % ", ".join("`%s`" % b for b in branches)
    )


def marker(finding: dict) -> str:
    return "kofin-schema-watch:%s:%d" % (finding["kind"], finding["version"])


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--ref",
        action="append",
        dest="refs",
        help="probe this ref instead of discovering branches (repeatable)",
    )
    parser.add_argument("--json", help="write the findings to this file as JSON")
    parser.add_argument("--token", help="GitHub token for the branch listing")
    parser.add_argument(
        "--fail-on-new",
        action="store_true",
        help="exit 3 when a version newer than the gate is found",
    )
    args = parser.parse_args(argv)

    try:
        refs = args.refs or discover_refs(args.token)
        findings, observations = check(refs)
    except ProbeError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    width = max(len(o["name"]) for o in observations)
    for obs in observations:
        print(
            "%-10s %-8s %-*s %s"
            % (
                obs["ref"],
                obs["kind"],
                width,
                obs["name"],
                "supported" if obs["supported"] else "NOT SUPPORTED",
            )
        )
    print()
    if findings:
        for finding in findings:
            print(
                "new: %s on %s (gate has %s)"
                % (
                    finding["name"],
                    ", ".join(finding["refs"]),
                    ", ".join(str(v) for v in finding["supported"]),
                )
            )
    else:
        print("the gate is level with upstream on every ref probed")

    if args.json:
        payload = [
            dict(f, title=issue_title(f), body=issue_body(f), marker=marker(f))
            for f in findings
        ]
        Path(args.json).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    return 3 if (findings and args.fail_on_new) else 0


if __name__ == "__main__":
    sys.exit(main())
