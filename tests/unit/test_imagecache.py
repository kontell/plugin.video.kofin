"""L1: the shared image-cache plumbing (core/imagecache.py) — the parser
and the store step both texture-cache seeders ride (P1.10). The size and
extension assertions moved here from the two callers' suites."""

from kofin.core import imagecache


def jpeg(width: int, height: int) -> bytes:
    return (
        b"\xff\xd8"  # SOI
        + b"\xff\xe0\x00\x04\x00\x00"  # APP0, length 4
        + b"\xff\xc0\x00\x11\x08"  # SOF0, length, precision
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x00" * 12
    )


def png(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\x0dIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x00" * 5
    )


def test_image_size_reads_both_formats_and_degrades():
    assert imagecache.image_size(jpeg(267, 400)) == (267, 400)
    assert imagecache.image_size(png(400, 267)) == (400, 267)
    assert imagecache.image_size(jpeg(640, 360)) == (640, 360)
    # Unparseable is bookkeeping-only: the sizes row still lands (size=1 is
    # what Kodi's lookup keys on).
    assert imagecache.image_size(b"\xff\xd8not-a-real-jpeg") == (0, 0)
    assert imagecache.image_size(b"not an image") == (0, 0)


def test_extension_follows_the_bytes_not_the_url():
    """Kodi stores a PNG source as .png, and the extension is part of the
    cachedurl the seeders write — a wrong one is a file Kodi never finds."""
    assert imagecache.extension_for(png(1, 1)) == ".png"
    assert imagecache.extension_for(jpeg(1, 1)) == ".jpg"
    assert imagecache.extension_for(b"") == ".jpg"


def test_store_image_writes_and_measures(tmp_path):
    destination = tmp_path / "a" / "b.jpg"
    width, height = imagecache.store_image(jpeg(640, 360), str(destination))
    assert (width, height) == (640, 360)
    assert destination.read_bytes() == jpeg(640, 360)
