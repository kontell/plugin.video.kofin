#!/usr/bin/env python3
"""Run phase-1 SQL comparison and API package startup on a Piers Flatpak.

Owns only plugin.video.kofin.phase1 in addon_data. Does not install an addon,
replace the active Kofin, or touch production library rows. Target addresses
and credentials are read only through kodi-drive and never printed.
"""

import argparse
import importlib.machinery
import importlib.util
import urllib.request
import ast
import base64
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import time


class Driver:
    def __init__(self, driver_dir, target):
        self.driver_dir = Path(driver_dir).resolve()
        sys.path.insert(0, str(self.driver_dir / "lib"))
        import kodi_target

        self.config = kodi_target.load(target)
        if not self.config.get("ADDR"):
            raise RuntimeError("the named target must configure SSH ADDR")

    def remote(self, code):
        process = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=8",
                self.config["ADDR"],
                "python3 -",
            ],
            input=code,
            text=True,
            capture_output=True,
            timeout=55,
        )
        if process.returncode:
            # SSH stderr can contain private addresses. Keep it out of logs.
            raise RuntimeError("remote operation failed (exit %d)" % process.returncode)
        return process.stdout

    def rpc(self, method, params=None):
        cfg = self.config
        request = urllib.request.Request(
            "http://%s:%s/jsonrpc" % (cfg["HOST"], cfg["PORT"]),
            data=json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Basic "
                + base64.b64encode((cfg["USER"] + ":" + cfg["PASS"]).encode()).decode(),
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
        if "error" in result:
            raise RuntimeError(method + ": " + json.dumps(result["error"]))
        return result["result"]

    def builtin(self, command):
        loader = importlib.machinery.SourceFileLoader(
            "phase0_builtin", str(self.driver_dir / "bin/kodi-builtin")
        )
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        module.send_builtin(command, self.config["HOST"], int(self.config["ESPORT"]))


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("action", choices=("deploy", "refresh", "run", "check", "remove"))
parser.add_argument("mode", nargs="?", choices=("sql", "api"))
parser.add_argument("label", nargs="?", choices=("baseline", "shared", "api"))
parser.add_argument("--driver-dir", type=Path, required=True)
parser.add_argument("--workdir", type=Path, required=True)
parser.add_argument("--target", default="P1D")
parser.add_argument("--baseline", default="fd382a5bc7440880b9eb41f702c8a7cd0629de0c")
args = parser.parse_args()
STAGE = args.workdir.resolve()
REMOTE = 'Path.home()/".var/app/tv.kodi.Kodi/data/userdata/addon_data/plugin.video.kofin.phase1"'
d = Driver(args.driver_dir, args.target)


def counts():
    out = {}
    for kind in (
        "Movies",
        "TVShows",
        "Episodes",
        "MusicVideos",
        "Songs",
        "Albums",
        "Artists",
    ):
        lib = (
            "AudioLibrary" if kind in ("Songs", "Albums", "Artists") else "VideoLibrary"
        )
        data = d.rpc(lib + ".Get" + kind, {"limits": {"start": 0, "end": 1}})
        out[kind] = data.get("limits", {}).get("total")
    return out


def deploy():
    assert not d.rpc("Player.GetActivePlayers"), "postpone while playing"
    STAGE.mkdir(exist_ok=True)
    data = subprocess.check_output(["git", "archive", args.baseline], cwd=ROOT)
    dest = STAGE / "packages/baseline"
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(dest, filter="data")
    sys.path.insert(0, str(ROOT / "tools"))
    import build

    for profile, label in (("sql", "shared"), ("api", "api")):
        dest = STAGE / "packages" / label
        if dest.exists():
            import shutil

            shutil.rmtree(dest)
        build.stage(dest, profile)
    payload = {}
    for p in (STAGE / "packages").rglob("*"):
        rel = p.relative_to(STAGE)
        # Only runtime Python/metadata needed for the baseline; built profiles
        # are copied in full so the API check uses actual distribution bytes.
        if not p.is_file():
            continue
        if rel.parts[1] == "baseline" and rel.parts[2] not in (
            "lib",
            "resources",
            "addon.xml",
        ):
            continue
        payload[str(rel)] = base64.b64encode(p.read_bytes()).decode()
    for name in ("probe.py", "sql_pipeline.py"):
        payload[name] = base64.b64encode(
            (ROOT / "tests/live/or_phase1" / name).read_bytes()
        ).decode()
    for name in (
        "myvideos149.sql",
        "myvideos149_seed.sql",
        "mymusic84.sql",
        "mymusic84_seed.sql",
    ):
        payload["fixtures/" + name] = base64.b64encode(
            (ROOT / "tests/fixtures" / name).read_bytes()
        ).decode()
    for source, dest in (("sync_dtos.py", "fixture_dtos.py"), ("fakes.py", "fakes.py")):
        payload[dest] = base64.b64encode(
            (ROOT / "tests/unit" / source).read_bytes()
        ).decode()
    source = (ROOT / "tests/unit/test_sync_writers.py").read_text()
    node = next(
        x
        for x in ast.parse(source).body
        if isinstance(x, ast.ClassDef) and x.name == "FakeApi"
    )
    payload["fixture_api.py"] = base64.b64encode(
        ast.get_source_segment(source, node).encode()
    ).decode()
    result = d.remote(
        "from pathlib import Path\nimport base64\nroot="
        + REMOTE
        + '\nif root.exists():\n assert (root/".phase1-owner").read_text()=="kofin-phase1"\nroot.mkdir(parents=True,exist_ok=True)\n(root/".phase1-owner").write_text("kofin-phase1")\nfor name,data in '
        + repr(payload)
        + '.items():\n p=root/name\n p.parent.mkdir(parents=True,exist_ok=True)\n p.write_bytes(base64.b64decode(data))\nprint("phase1 probe staged")\n'
    )
    before = {
        "counts": counts(),
        "application": d.rpc(
            "Application.GetProperties", {"properties": ["version", "name"]}
        ),
    }
    (STAGE / "before.json").write_text(json.dumps(before, indent=2) + "\n")
    print(result.strip(), json.dumps(before))


def run(mode, label):
    # Each label is a distinct disposable run, never overwrite production data.
    d.remote(
        "from pathlib import Path\nimport shutil\nroot="
        + REMOTE
        + '\nassert (root/".phase1-owner").read_text()=="kofin-phase1"\np=root/'
        + repr(label + "-work")
        + "\nif p.exists(): shutil.rmtree(p)\n(root/"
        + repr(label + ".json")
        + ").unlink(missing_ok=True)"
    )
    d.builtin(
        "RunScript(special://profile/addon_data/plugin.video.kofin.phase1/probe.py,"
        + mode
        + ","
        + label
        + ")"
    )
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        text = d.remote(
            "from pathlib import Path\np=("
            + REMOTE
            + ")/"
            + repr(label + ".json")
            + '\nprint(p.read_text() if p.exists() else "")'
        )
        if text.strip():
            result = json.loads(text)
            (STAGE / (label + ".json")).write_text(json.dumps(result, indent=2) + "\n")
            print(json.dumps(result, indent=2))
            if not result.get("complete"):
                raise RuntimeError("probe failed")
            return
        time.sleep(2)
    raise RuntimeError("probe timed out")


def check():
    before = json.loads((STAGE / "before.json").read_text())["counts"]
    after = counts()
    result = {
        "counts_before": before,
        "counts_after": after,
        "production_counts_unchanged": before == after,
    }
    baseline = json.loads((STAGE / "baseline.json").read_text())
    shared = json.loads((STAGE / "shared.json").read_text())
    result["row_counts_match"] = (
        baseline["result"]["row_counts"] == shared["result"]["row_counts"]
    )
    result["hashes_match"] = baseline["result"]["hashes"] == shared["result"]["hashes"]
    result["timings"] = {
        k: v["result"]["timings"]
        for k, v in (("baseline", baseline), ("shared", shared))
    }
    (STAGE / "comparison.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    if not all((result["production_counts_unchanged"], result["hashes_match"])):
        raise RuntimeError("comparison failed")


try:
    action = args.action
    if action == "deploy":
        deploy()
    elif action == "refresh":
        payload = {
            name: base64.b64encode(
                (ROOT / "tests/live/or_phase1" / name).read_bytes()
            ).decode()
            for name in ("probe.py", "sql_pipeline.py")
        }
        print(
            d.remote(
                "from pathlib import Path\nimport base64\nroot="
                + REMOTE
                + '\nassert (root/".phase1-owner").read_text()=="kofin-phase1"\nfor name,data in '
                + repr(payload)
                + '.items():\n (root/name).write_bytes(base64.b64decode(data))\nprint("probe refreshed")'
            )
        )
    elif action == "run":
        run(args.mode, args.label)
    elif action == "check":
        check()
    elif action == "remove":
        assert json.loads((STAGE / "comparison.json").read_text())[
            "production_counts_unchanged"
        ]
        print(
            d.remote(
                "from pathlib import Path\nimport shutil\nroot="
                + REMOTE
                + '\nassert (root/".phase1-owner").read_text()=="kofin-phase1"\nshutil.rmtree(root)\nprint("phase1 fixture removed")'
            )
        )
except Exception as error:
    print(type(error).__name__)
    raise SystemExit(1) from None
