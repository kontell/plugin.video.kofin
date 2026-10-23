"""One installable tree for release ZIPs and development deployment."""

import os
from pathlib import Path

# Path components skipped wherever they appear: VCS, virtualenvs, caches.
EXCLUDE_ANYWHERE = {
    "CLAUDE.md",
    # Sits alongside CLAUDE.md rather than in EXCLUDE_TOP because agent config
    # can be directory-scoped, and it holds local settings that must never
    # reach an installed addon. It is gitignored, but the build copies the
    # working tree, so being ignored is not enough on its own.
    ".claude",
    ".codex",
    ".agents",
    ".aws",
    ".ruff_cache",
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
    "README.md",
    "CONTRIBUTING.md",
    ".gitignore",
    ".github",
    ".vscode",
    ".idea",
}
# Suffixes never shipped: byte-compiled Python, and the stray SQLite files a
# test run can leave in the repo root. The databases are gitignored for
# exactly that reason, but this build copies the *working tree* — a shipped
# kofin.db is 70-odd KB of somebody's test data riding into every install
# (found in the 0.13.0 zip).
EXCLUDE_SUFFIX = (".pyc", ".pyo", ".db", ".db-wal", ".db-shm")


def iter_files(root):
    """Every repo-relative path to package, in deterministic order."""
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
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
            path = rel / name
            if not (root / path).is_symlink():
                yield path


def contents(root, path):
    return (root / path).read_bytes()
