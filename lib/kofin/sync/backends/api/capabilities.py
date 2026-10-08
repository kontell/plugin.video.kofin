"""Read public Kodi capabilities inside the addon; no HTTP server required."""

import json
from pathlib import Path

import xbmcaddon
import xbmcgui

from kofin.core import kodirpc
from .contract import check


def inspect(allow_dirty=False):
    contract = json.loads(Path(__file__).with_name("requirements.json").read_text())
    schema = kodirpc.call(
        "JSONRPC.Introspect", {"getdescriptions": False, "getmetadata": True}
    )
    application = kodirpc.call(
        "Application.GetProperties", {"properties": ["version", "name"]}
    )
    capture = {
        "application": application if isinstance(application, dict) else {},
        "methods": schema.get("methods", {}) if isinstance(schema, dict) else {},
        "system_addons": {},
    }
    for name in contract["system_addons"]:
        try:
            capture["system_addons"][name] = xbmcaddon.Addon(name).getAddonInfo(
                "version"
            )
        except Exception:
            capture["system_addons"][name] = "0"
    item = xbmcgui.ListItem(offscreen=True)
    for key, tag in (
        ("python_video_methods", item.getVideoInfoTag()),
        ("python_music_methods", item.getMusicInfoTag()),
    ):
        capture[key] = {name: hasattr(tag, name) for name in contract["python"][key]}
    report = check(capture, contract, allow_dirty=allow_dirty)
    report.update(
        {
            "backend": "api",
            "native_sync_enabled": report["interfaces_passed"],
            "native_sync_scope": ["Movie"] if report["interfaces_passed"] else [],
            "application": capture["application"],
        }
    )
    return report
