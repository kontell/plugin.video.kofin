#!/usr/bin/env python3
"""Run the packaged phase-3 movie lifecycle on a Piers Flatpak.

Installs an isolated plugin.video.kofin.phase3 fixture and removes its own rows.
Does not replace the active Kofin or touch production library rows. Target addresses
and credentials are read only through kodi-drive and never printed.
"""

import argparse
import importlib.machinery
import importlib.util
import urllib.request
import base64
import json
from pathlib import Path
import subprocess
import sys
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
ADDON = "plugin.video.kofin.phase3"
REMOTE = 'Path.home()/".var/app/tv.kodi.Kodi/data"'
parser = argparse.ArgumentParser()
parser.add_argument(
    "action",
    choices=(
        "deploy",
        "run",
        "result",
        "remove",
        "diagnose",
        "restart-prepare",
        "restart",
        "restart-verify",
        "restart-result",
    ),
)
parser.add_argument("--target", default="P1D")
parser.add_argument("--driver-dir", type=Path, default=ROOT.parent / "kodi-drive")
parser.add_argument("--out", type=Path, default=ROOT / "tests/live/results/or-phase3")
args = parser.parse_args()
d = Driver(args.driver_dir, args.target)
try:
    args.out.mkdir(parents=True, exist_ok=True)
    if args.action == "deploy":
        assert not d.rpc("Player.GetActivePlayers"), "postpone during playback"
        sys.path.insert(0, str(ROOT / "tools"))
        import build
        import tempfile
        import xml.etree.ElementTree as ET

        with tempfile.TemporaryDirectory(prefix="kofin-phase3-") as temp:
            stage = Path(temp) / "addon"
            build.stage(stage, "api")
            payload = {}
            for p in stage.rglob("*"):
                if not p.is_file():
                    continue
                data = p.read_bytes()
                if p.suffix in (".py", ".xml", ".json", ".po"):
                    data = data.replace(b"plugin.video.kofin", ADDON.encode())
                if p.name == "state.py":
                    data = data.replace(b'"kofin.', b'"kofinphase3.').replace(
                        b'"syncsession.state"', b'"phase3.syncsession.state"'
                    )
                if p.name == "addon.xml":
                    tree = ET.fromstring(data)
                    tree.set("name", "Kofin phase 3 fixture")
                    for ext in list(tree.findall("extension")):
                        if ext.get("point") in ("xbmc.service", "kodi.context.item"):
                            tree.remove(ext)
                    data = ET.tostring(tree)
                payload[str(p.relative_to(stage))] = base64.b64encode(data).decode()
        payload["playback.py"] = base64.b64encode(
            (HERE / "playback.py").read_bytes()
        ).decode()
        payload["recovery.py"] = base64.b64encode(
            (HERE / "recovery.py").read_bytes()
        ).decode()
        clip = args.out / "fixture.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=320x180:r=24",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=stereo",
                "-t",
                "12",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                str(clip),
            ],
            check=True,
            capture_output=True,
        )
        payload["fixture.mp4"] = base64.b64encode(clip.read_bytes()).decode()
        payload["probe.py"] = base64.b64encode(
            (HERE / "probe.py").read_bytes()
        ).decode()
        code = (
            "from pathlib import Path\nimport base64\nroot=("
            + REMOTE
            + ')/"addons"/'
            + repr(ADDON)
            + "\n"
        )
        code += 'if root.exists():\n assert (root/".phase3-owner").read_text()=="kofin-phase3"\n'
        code += 'root.mkdir(parents=True,exist_ok=True)\n(root/".phase3-owner").write_text("kofin-phase3")\n'
        code += (
            "for name,data in "
            + repr(payload)
            + ".items():\n p=root/name\n p.parent.mkdir(parents=True,exist_ok=True)\n p.write_bytes(base64.b64decode(data))\n"
        )
        d.remote(code)
        d.builtin("UpdateLocalAddons")
        for _ in range(30):
            time.sleep(0.2)
            try:
                d.rpc("Addons.SetAddonEnabled", {"addonid": ADDON, "enabled": True})
                break
            except RuntimeError:
                continue
        else:
            raise RuntimeError("fixture discovery failed")
        print("phase3 fixture deployed; production addon unchanged")
    elif args.action == "run":
        assert not d.rpc("Player.GetActivePlayers"), "postpone during playback"
        d.remote(
            "from pathlib import Path\np=("
            + REMOTE
            + ')/"userdata/addon_data"/'
            + repr(ADDON)
            + '/"result.json"\np.unlink(missing_ok=True)'
        )
        d.builtin("RunScript(special://home/addons/" + ADDON + "/probe.py)")
        print("phase3 probe started")
    elif args.action in ("restart-prepare", "restart-verify"):
        assert not d.rpc("Player.GetActivePlayers"), "postpone during playback"
        d.builtin(
            "RunScript(special://home/addons/"
            + ADDON
            + "/recovery.py,"
            + args.action.split("-")[1]
            + ")"
        )
        print(args.action + " started")
    elif args.action == "restart":
        assert not d.rpc("Player.GetActivePlayers"), "postpone during playback"
        d.builtin("RestartApp()")
        print("Kodi restart requested; verify readiness before recovery")
    elif args.action in ("result", "restart-result"):
        name = (
            "restart-result.json" if args.action == "restart-result" else "result.json"
        )
        result = d.remote(
            "from pathlib import Path\np=("
            + REMOTE
            + ')/"userdata/addon_data"/'
            + repr(ADDON)
            + "/"
            + repr(name)
            + '\nprint(p.read_text() if p.exists() else "{}")'
        )
        obj = json.loads(result)
        (args.out / name).write_text(json.dumps(obj, indent=2) + "\n")
        print(json.dumps(obj, indent=2))
    elif args.action == "diagnose":
        code = (
            "from pathlib import Path\nimport re\np=("
            + REMOTE
            + ')/"temp/kodi.log"\ns=p.read_text(errors="replace")[-100000:]\nblocks=re.findall(r"Error Type:.*?End of Python script error report",s,re.S)\nprint("\\n".join(b for b in blocks if "phase3" in b))'
        )
        print(d.remote(code))
        print(
            d.remote(
                "from pathlib import Path\np=("
                + REMOTE
                + ')/"userdata/addon_data"/'
                + repr(ADDON)
                + '\nprint("profile files",[f.name for f in p.iterdir()] if p.exists() else [])\ns=(('
                + REMOTE
                + ')/"temp/kodi.log").read_text(errors="replace")[-100000:]\nprint("\\n".join(line for line in s.splitlines() if "phase3" in line and not any(t in line.lower() for t in ("http","token","password","api_key"))))'
            )
        )
    elif args.action == "remove":
        result = json.loads((args.out / "result.json").read_text())
        assert result.get("cleanup") and result.get("foreign_unchanged")
        d.remote(
            "from pathlib import Path\nimport json\np=("
            + REMOTE
            + ')/"userdata/addon_data"/'
            + repr(ADDON)
            + '/"restart-result.json"\nif p.exists():\n r=json.loads(p.read_text())\n assert r.get("cleanup") and r.get("foreign_unchanged")'
        )
        d.rpc("Addons.SetAddonEnabled", {"addonid": ADDON, "enabled": False})
        d.remote(
            "from pathlib import Path\nimport shutil\nroot=("
            + REMOTE
            + ')/"addons"/'
            + repr(ADDON)
            + '\nassert (root/".phase3-owner").read_text()=="kofin-phase3"\nshutil.rmtree(root)\np=('
            + REMOTE
            + ')/"userdata/addon_data"/'
            + repr(ADDON)
            + "\nif p.exists():shutil.rmtree(p)"
        )
        d.builtin("UpdateLocalAddons")
        print("phase3 fixture removed")
except Exception as error:
    print(
        type(error).__name__
        + ": phase3 action failed; private transport details omitted"
    )
    raise SystemExit(1) from None
