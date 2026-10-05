#!/usr/bin/env python3
"""Drive the phase-0 fixture on an SSH-accessible Piers Flatpak.

Credentials and target addresses come only from kodi-drive. Results contain
synthetic media and API definitions. Real-library baseline captures stay local.
"""

import argparse
import base64
import copy
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import subprocess
import runpy
import sys
import time
import urllib.request

HERE = Path(__file__).resolve().parent
ADDON = "plugin.video.kofin.phase0"
REMOTE_ROOT = 'Path.home() / ".var/app/tv.kodi.Kodi/data"'


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

    def deploy(self):
        if self.rpc("Player.GetActivePlayers"):
            raise RuntimeError("Kodi is playing; postpone fixture deployment")
        payload = {
            "provider/" + path.name: base64.b64encode(path.read_bytes()).decode()
            for path in (HERE / "provider").iterdir()
            if path.suffix in (".py", ".xml")
        }
        for name in ("probe.py", "sql_baseline.py"):
            payload[name] = base64.b64encode((HERE / name).read_bytes()).decode()
        for name in ("myvideos149.sql", "myvideos149_seed.sql"):
            payload[name] = base64.b64encode(
                (HERE.parents[1] / "fixtures" / name).read_bytes()
            ).decode()
        movie = runpy.run_path(str(HERE.parents[1] / "unit/sync_dtos.py"))["MOVIE"]
        fixed = []
        for index in range(100):
            row = copy.deepcopy(movie)
            row.update(
                Id="phase0-%03d" % index,
                Name="Phase0 movie %03d" % index,
                SortName="Phase0 movie %03d" % index,
                ProviderIds={},
                Path="/phase0/movie-%03d.mkv" % index,
            )
            row["MediaSources"][0]["Id"] = row["Id"] + "-source"
            fixed.append(row)
        payload["fixed-movies.json"] = base64.b64encode(
            json.dumps(fixed).encode()
        ).decode()
        code = (
            """from pathlib import Path
import base64,json
root = ROOT_EXPR
addon = root / 'addons' / ADDON_ID
profile = root / 'userdata/addon_data' / ADDON_ID
if addon.exists() and not (addon / '.phase0-owner').exists():
 raise RuntimeError('refusing to overwrite an unowned fixture install')
addon.mkdir(parents=True, exist_ok=True)
profile.mkdir(parents=True, exist_ok=True)
for name, data in PAYLOAD.items():
 dest = addon / name.split('/', 1)[1] if name.startswith('provider/') else profile / name
 dest.write_bytes(base64.b64decode(data))
(addon / '.phase0-owner').write_text(ADDON_ID)
if not (profile / 'config.json').exists():
 (profile / 'config.json').write_text('{}')
print('fixture staged')
""".replace("ROOT_EXPR", REMOTE_ROOT)
            .replace("ADDON_ID", repr(ADDON))
            .replace("PAYLOAD", repr(payload))
        )
        self.remote(code)
        self.builtin("UpdateLocalAddons")
        for _ in range(30):
            time.sleep(0.2)
            try:
                self.rpc("Addons.SetAddonEnabled", {"addonid": ADDON, "enabled": True})
                break
            except RuntimeError:
                continue
        else:
            raise RuntimeError("fixture did not appear in Kodi addon discovery")
        print("fixture deployed")

    def run(self, mode, output):
        if mode not in (
            "capture",
            "lifecycle",
            "prepare-forced",
            "read-forced",
            "set-forced-metadata",
            "cleanup",
            "sql-baseline",
        ):
            raise ValueError("unknown probe mode")
        self.remote(
            "from pathlib import Path\np = ("
            + REMOTE_ROOT
            + ") / 'userdata/addon_data' / "
            + repr(ADDON)
            + " / "
            + repr(mode + ".json")
            + "\np.unlink(missing_ok=True)\n"
        )
        script = "/sql_baseline.py" if mode == "sql-baseline" else "/probe.py," + mode
        self.builtin("RunScript(special://profile/addon_data/" + ADDON + script + ")")
        deadline = time.monotonic() + (900 if mode == "sql-baseline" else 240)
        while time.monotonic() < deadline:
            text = self.remote(
                "from pathlib import Path\np = ("
                + REMOTE_ROOT
                + ") / 'userdata/addon_data' / "
                + repr(ADDON)
                + " / "
                + repr(mode + ".json")
                + "\nprint(p.read_text() if p.exists() else '')\n"
            )
            if text.strip():
                result = json.loads(text)
                if mode == "sql-baseline" and not result.get("complete"):
                    time.sleep(2)
                    continue
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(result, indent=2) + "\n")
                print(
                    json.dumps(
                        {
                            "mode": mode,
                            "saved": str(output),
                            "error": result.get("error", result.get("error_type")),
                        }
                    )
                )
                if "error" in result or "error_type" in result:
                    raise RuntimeError("probe failed; inspect the saved result")
                return
            time.sleep(1)
        raise RuntimeError("probe timed out; fixture retained for diagnosis/cleanup")

    def remove(self):
        sources = self.rpc("Files.GetSources", {"media": "music"})["sources"]
        if any(
            source["file"].startswith("plugin://" + ADDON + "/") for source in sources
        ):
            raise RuntimeError(
                "remove the fixture's user music source before its files"
            )
        check = self.remote(
            "from pathlib import Path\nimport json\np = ("
            + REMOTE_ROOT
            + ") / 'userdata/addon_data' / "
            + repr(ADDON)
            + " / 'cleanup.json'\nprint(json.loads(p.read_text()).get('result',{}).get('clean',False))\n"
        )
        if check.strip() != "True":
            raise RuntimeError("successful native cleanup must precede fixture removal")
        self.rpc("Addons.SetAddonEnabled", {"addonid": ADDON, "enabled": False})
        self.remote(
            "from pathlib import Path\nimport shutil\nroot = "
            + REMOTE_ROOT
            + "\naddon = root / 'addons' / "
            + repr(ADDON)
            + "\nassert (addon / '.phase0-owner').read_text() == "
            + repr(ADDON)
            + "\nshutil.rmtree(addon)\nshutil.rmtree(root / 'userdata/addon_data' / "
            + repr(ADDON)
            + ")\n"
        )
        self.builtin("UpdateLocalAddons")
        print("fixture files removed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "deploy",
            "capture",
            "lifecycle",
            "prepare-forced",
            "read-forced",
            "set-forced-metadata",
            "cleanup",
            "remove",
            "sql-baseline",
        ),
    )
    parser.add_argument("--target", default="P1D")
    parser.add_argument("--driver-dir", default=str(HERE.parents[3] / "kodi-drive"))
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    driver = Driver(args.driver_dir, args.target)
    if args.action == "deploy":
        driver.deploy()
    elif args.action == "remove":
        driver.remove()
    elif args.out is None:
        parser.error("probe actions require --out")
    else:
        driver.run(args.action, args.out)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Network exceptions may contain the private endpoint or credentials.
        print("phase0 driver failed:", type(error).__name__, file=sys.stderr)
        raise SystemExit(1) from None
