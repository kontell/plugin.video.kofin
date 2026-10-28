"""Image size and cache-extension helpers."""

import os
import struct
from typing import Tuple

THUMBNAILS = "special://thumbnails/"

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_SOF_MARKERS = frozenset(
    (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF)
)


def image_size(data: bytes) -> Tuple[int, int]:
    """(width, height) for a JPEG or PNG; (0, 0) when unparseable."""
    if data[:8] == _PNG_MAGIC and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return int(width), int(height)
    index = 2
    while index < len(data) - 9:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in _SOF_MARKERS:
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return int(width), int(height)
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        (length,) = struct.unpack(">H", data[index + 2 : index + 4])
        index += 2 + length
    return 0, 0


def extension_for(data: bytes) -> str:
    """The cache file's extension: Kodi stores a PNG source as ``.png`` and
    everything else as ``.jpg``."""
    return ".png" if data[:8] == _PNG_MAGIC else ".jpg"


def store_image(data: bytes, destination: str) -> Tuple[int, int]:
    """Write the fetched image where the cache will look for it and answer
    its stored dimensions."""
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    with open(destination, "wb") as handle:
        handle.write(data)
    return image_size(data)
