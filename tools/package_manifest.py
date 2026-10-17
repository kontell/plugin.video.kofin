"""One installable tree for release ZIPs and development deployment."""

import ast
import os
from pathlib import Path
import xml.etree.ElementTree as ET

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


# Prefixes, relative to the addon root. These files must be absent, not merely
# unreachable behind a setting, in every API distribution.
SQL_ONLY = (
    "lib/kofin/core/addonxml.py",
    "lib/kofin/sync/backends/sql/",
    "lib/kofin/sync/kodidb/",
    "lib/kofin/sync/writers/",
    "lib/kofin/sync/db.py",
    "lib/kofin/sync/schema.py",
    "lib/kofin/sync/clean.py",
    "lib/kofin/sync/widgetstate.py",
    "lib/kofin/sync/refresh.py",
    "lib/kofin/sync/musicsources.py",
    "lib/kofin/service/artcache.py",
    "lib/kofin/service/chapters.py",
    "lib/kofin/downloads/repoint.py",
    "lib/kofin/plugin/clean.py",
)
PROFILES = ("sql", "api")

# Settings each profile removes from resources/settings.xml. The two
# presentation toggles are one per build: syncMusicPlaylists is the SQL
# build's playlist sync, libraryNodes the API build's nodes-and-playlists
# switch (sync/dynamic.py), and a toggle the build never reads must not be
# on screen. The scheduled full update is the API coordinator's clock; the
# SQL build has no such pass.
DISABLED_SETTINGS = {
    "sql": {"libraryNodes", "libraryStatus", "fullUpdateInterval"},
    "api": {
        # The two read-only lines the SQL coordinator writes; the API build
        # has the status dialog in their place (sync/backends/api/status.py).
        "syncedLibraries",
        "syncStatus",
        "cleanDatabases",
        "reuseLanguageInvoker",
        "chapterImages",
        "precacheActorArt",
        "precacheActorArtNow",
        "syncDuringPlay",
        "syncMusicPlaylists",
        "limitIndex",
        "limitThreads",
        "refreshBoxsets",
    },
}


# Settings a profile shows only while another setting holds a value:
# {profile: {setting: (the other setting, its value)}}. The three lists are
# what a device can play and what its display accepts, and a play under
# force direct play reads none of them. Hidden, they keep their values, and
# two things still read those: a transcode asked for from the context menu
# and the download decision (deviceprofile.build, ProfileConfig.for_downloads).
# They carry the rule in the API profile alone: on Kodi 21 a <dependencies>
# block on a list[string] setting unregisters the setting it names (the note
# in settings.xml), and the SQL profile still installs there. The API
# profile's floor is Kodi 22.
VISIBLE_WHILE = {
    "sql": {},
    "api": {
        "directPlayVideoCodecs": ("forceDirectPlay", "false"),
        "directPlayAudioCodecs": ("forceDirectPlay", "false"),
        "allowedHdrTypes": ("forceDirectPlay", "false"),
    },
}


# A setting whose help text differs in a profile: {profile: {setting: id}}.
# "Transcode music" decides how a song's path is written in the SQL build,
# which is why its help asks for a repair there. In the API build every song
# is a plugin path whatever it says, and it only decides whether the bitrate
# limit applies. "Update libraries" catches up and prunes the libraries the
# viewer picks in the SQL build; in the API build it lists every library,
# after a confirmation (plugin/actions.py update_libraries).
HELP = {
    "sql": {},
    "api": {"musicTranscode": "30861", "updateLibraries": "30874"},
}


def show_while(setting, other, value):
    """Give a setting a visibility rule on another setting's value, beside
    any rule it already has."""
    dependencies = setting.find("dependencies")
    if dependencies is None:
        dependencies = ET.Element("dependencies")
        control = setting.find("control")
        position = list(setting).index(control) if control is not None else len(setting)
        setting.insert(position, dependencies)
    dependency = ET.SubElement(dependencies, "dependency", {"type": "visible"})
    condition = ET.SubElement(
        dependency, "condition", {"operator": "is", "setting": other}
    )
    condition.text = value


def selected_profile(root):
    source = ast.parse((root / "lib/kofin/buildconfig.py").read_text())
    for statement in source.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "BACKEND"
            for target in statement.targets
        ):
            value = ast.literal_eval(statement.value)
            if value in PROFILES:
                return value
    raise ValueError("missing explicit build backend")


def allowed(path, profile):
    if profile not in PROFILES:
        raise ValueError("unknown package profile")
    name = path.as_posix()
    if profile == "api":
        return not any(
            name.startswith(p) if p.endswith("/") else name == p for p in SQL_ONLY
        )
    return not name.startswith("lib/kofin/sync/backends/api/")


def iter_files(root, profile):
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
            if allowed(path, profile) and not (root / path).is_symlink():
                yield path


def contents(root, path, profile):
    data = (root / path).read_bytes()
    if path.as_posix() == "lib/kofin/buildconfig.py":
        source = data.decode("utf-8")
        current = selected_profile(root)
        return source.replace(
            'BACKEND = "' + current + '"', 'BACKEND = "' + profile + '"'
        ).encode("utf-8")
    if path.as_posix() == "resources/settings.xml":
        tree = ET.fromstring(data)
        disabled = DISABLED_SETTINGS[profile]
        conditional = VISIBLE_WHILE[profile]
        for group in tree.findall(".//group"):
            for setting in list(group):
                if setting.get("id") in disabled:
                    group.remove(setting)
                    continue
                if setting.get("id") in conditional:
                    show_while(setting, *conditional[setting.get("id")])
                if setting.get("id") in HELP[profile]:
                    setting.set("help", HELP[profile][setting.get("id")])
        return ET.tostring(tree, encoding="utf-8", xml_declaration=True)
    if profile != "api":
        return data
    if path.as_posix() == "addon.xml":
        tree = ET.fromstring(data)
        # Only the committed native tree is scannable in this preview: the
        # interactive root, search and the user-dependent menus never are.
        for extension in tree.findall("extension"):
            for entry in extension.findall("medialibraryscanpath"):
                if entry.get("content") in ("movies", "tvshows", "musicvideos"):
                    entry.text = "/native/"
                else:
                    extension.remove(entry)
        requires = tree.find("requires")
        floor = requires.find("import[@addon='xbmc.addon']")
        if floor is None:
            ET.SubElement(
                requires, "import", {"addon": "xbmc.addon", "version": "21.90.802"}
            )
        return ET.tostring(tree, encoding="utf-8", xml_declaration=True)
    return data
