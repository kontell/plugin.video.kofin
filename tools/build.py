#!/usr/bin/env python3
"""Build a Kodi package or stage the identical tree for development install."""

import argparse
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import package_manifest as manifest


def addon_meta():
    root = ET.parse(ROOT / "addon.xml").getroot()
    return root.get("id"), root.get("version")


def iter_files():
    return manifest.iter_files(ROOT)


def stage(directory):
    directory = Path(directory)
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("staging directory must be empty")
    for path in iter_files():
        target = directory / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(manifest.contents(ROOT, path))


def build(outdir):
    addon_id, version = addon_meta()
    if not addon_id or not version:
        sys.exit("addon.xml is missing an id or version")
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    target = outdir / f"{addon_id}-{version}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in iter_files():
            archive.writestr(
                f"{addon_id}/{path.as_posix()}", manifest.contents(ROOT, path)
            )
    with zipfile.ZipFile(target) as archive:
        if (
            archive.testzip() is not None
            or f"{addon_id}/addon.xml" not in archive.namelist()
        ):
            sys.exit("invalid addon archive")
    print(f"{target} ({target.stat().st_size // 1024} KiB)")
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("outdir", nargs="?", type=Path, default=ROOT / "dist")
    parser.add_argument("--stage", type=Path)
    args = parser.parse_args()
    if args.stage:
        stage(args.stage)
    else:
        build(args.outdir)
