import json
import pathlib
import struct
import zlib
from urllib.parse import quote
import xbmc
import xbmcvfs

profile = pathlib.Path(
    xbmcvfs.translatePath(
        "special://profile/addon_data/plugin.video.kofin.apiresearch/"
    )
)
fixture = profile / "texture-probe.png"


def chunk(kind, data):
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


fixture.write_bytes(
    b"\x89PNG\r\n\x1a\n"
    + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
    + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\x00\xff\x00" * 2))
    + chunk(b"IEND", b"")
)


def rpc(method, params):
    return json.loads(
        xbmc.executeJSONRPC(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        )
    )


params = {
    "properties": ["url", "cachedurl", "sizes"],
    "filter": {"field": "url", "operator": "contains", "value": "texture-probe.png"},
}
out = {"before": rpc("Textures.GetTextures", params)}
try:
    handle = xbmcvfs.File("image://" + quote(str(fixture), safe="") + "/")
    out["read_bytes"] = len(handle.readBytes())
    handle.close()
except Exception as exc:
    out["error"] = type(exc).__name__ + ": " + str(exc)
out["after"] = rpc("Textures.GetTextures", params)
(profile / "image-result.json").write_text(json.dumps(out, indent=2))
