#!/usr/bin/env python3
"""Build a Kodi-installable zip of the addon.

Packages the working tree into ``<outdir>/<id>-<version>.zip`` with every file
nested under a top-level ``<id>/`` directory, exactly as Kodi's "Install from
zip file" expects. Development-only files are left out — the same set
``tools/dev-install.sh`` keeps out of the installed tree (docs, tests, tools,
lint/test config, caches, VCS) — so the zip carries only what the addon needs
at runtime. The id and version come straight from ``addon.xml``.

Usage: build.py [OUTDIR]      (default OUTDIR: ./dist)
"""

import os
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Path components skipped wherever they appear: VCS, virtualenvs, caches.
EXCLUDE_ANYWHERE = {
    "CLAUDE.md",
    ".git",
    ".venv",
    "venv",
    ".tox",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".DS_Store",
}
# Repo-root entries that are development-only (mirrors dev-install.sh, plus the
# build's own output dir and common editor/CI folders).
EXCLUDE_TOP = {
    "docs",
    "tests",
    "tools",
    "dist",
    "mypy.ini",
    "tox.ini",
    "pyproject.toml",
    "requirements-dev.txt",
    ".gitignore",
    ".github",
    ".vscode",
    ".idea",
}
# Suffixes never shipped (byte-compiled Python).
EXCLUDE_SUFFIX = (".pyc", ".pyo")


def addon_meta():
    """(id, version) read from addon.xml — the zip name and top-level dir."""
    root = ET.parse(ROOT / "addon.xml").getroot()
    return root.get("id"), root.get("version")


def iter_files():
    """Every repo-relative path to package, in deterministic order."""
    for dirpath, dirnames, filenames in os.walk(ROOT):
        rel = Path(dirpath).relative_to(ROOT)
        at_root = rel == Path(".")
        # Prune (and order) directories in place so os.walk skips them.
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in EXCLUDE_ANYWHERE and not (at_root and name in EXCLUDE_TOP)
        )
        for name in sorted(filenames):
            if name in EXCLUDE_ANYWHERE or name.endswith(EXCLUDE_SUFFIX):
                continue
            if at_root and name in EXCLUDE_TOP:
                continue
            yield rel / name


def build(outdir):
    addon_id, version = addon_meta()
    if not addon_id or not version:
        sys.exit("addon.xml is missing an id or version attribute")

    files = list(iter_files())
    if Path("addon.xml") not in files:
        sys.exit("addon.xml not found in the tree; refusing to build")

    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    zip_path = outdir / f"{addon_id}-{version}.zip"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for rel in files:
            arcname = (Path(addon_id) / rel).as_posix()
            archive.write(ROOT / rel, arcname=arcname)

    # Verify the result is a well-formed archive Kodi will accept.
    with zipfile.ZipFile(zip_path) as archive:
        broken = archive.testzip()
        if broken is not None:
            sys.exit(f"built zip is corrupt at {broken}")
        if f"{addon_id}/addon.xml" not in archive.namelist():
            sys.exit(f"built zip lacks {addon_id}/addon.xml")

    size_kib = zip_path.stat().st_size / 1024
    print(f"{zip_path}  ({len(files)} files, {size_kib:.0f} KiB)")
    return zip_path


if __name__ == "__main__":
    if len(sys.argv) > 2:
        sys.exit(__doc__.strip())
    build(sys.argv[1] if len(sys.argv) == 2 else ROOT / "dist")
