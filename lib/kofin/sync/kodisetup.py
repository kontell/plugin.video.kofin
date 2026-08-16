# -*- coding: utf-8 -*-
"""Kodi profile prerequisites for library sync (fork ``helper/xmls.py``
subset).

Adaptations per plan §2/§3: ``verify_kodi_defaults`` ports without the
forced node ordering (the fork rewrote index.xml order attributes to pin
its own layout — kofin leaves the user's ordering alone), and
advancedsettings.xml is **detected, never mutated**: ``cleanonupdate`` is
incompatible with plugin paths, so its presence raises one warning at
service start and the user fixes it themselves.
"""

import os
import xml.etree.ElementTree as etree

import xbmcvfs

from kofin.core.log import Logger
from kofin.sync.shims import localized, notification

LOG = Logger(__name__)


def cleanonupdate_enabled():
    """True when advancedsettings.xml enables videolibrary cleanonupdate."""
    path = xbmcvfs.translatePath("special://profile/")
    file = os.path.join(path, "advancedsettings.xml")

    try:
        xml = etree.parse(file).getroot()
    except Exception:
        return False

    video = xml.find("videolibrary")

    if video is not None:
        cleanonupdate = video.find("cleanonupdate")

        if cleanonupdate is not None and cleanonupdate.text == "true":
            return True

    return False


def warn_incompatible_settings():
    """One notification at service start when cleanonupdate is present.

    Never edits the file (report §6): Kodi cleaning "missing" sources would
    wipe plugin-path library rows on every scan, but that is the user's
    file to change.
    """
    if cleanonupdate_enabled():
        LOG.warning(
            "advancedsettings.xml enables videolibrary cleanonupdate; "
            "this is incompatible with plugin paths — library sync rows "
            "would be removed by Kodi's clean pass. Please remove it."
        )
        # A warning, not an error: nothing has failed yet — the user is being
        # told to remove a setting that will cost them library rows if they
        # don't.
        notification(localized(30414), time_ms=8000, warning=True)
        return True

    return False


def verify_kodi_defaults():
    """Make sure we have the kodi default node files in place.

    Both trees. Kodi keeps video and music nodes in two entirely separate
    trees, and ``CLibraryDirectory::GetDirectory`` swaps the whole shipped
    tree for the profile's as soon as the profile has one — there is no
    merge and nothing is logged. So generating a music node into a profile
    that has no music tree of its own is what replaces Genres, Artists,
    Albums, Songs and the rest with the single folder we wrote, on every
    skin at once. Measured on Omega 21.3: a profile ``library/music/``
    holding only ``kofin/`` made ``library://music/`` return one entry.

    Seeding video only was the fork's behaviour, from before there were
    music nodes to generate (``sync/views.py::write_music_nodes``).
    """
    for kind in ("video", "music"):
        _seed_default_nodes(kind)

    # The fork forced its own ordering onto the default movie/tvshow/musicvideo
    # nodes here; kofin does not touch the user's node order (plan §3).

    playlist_path = xbmcvfs.translatePath("special://profile/playlists/video")

    if not xbmcvfs.exists(playlist_path):
        xbmcvfs.mkdirs(playlist_path)


def _seed_default_nodes(kind):
    """Copy Kodi's shipped ``kind`` node tree into the profile.

    Missing files only: a node the user has edited is theirs, and the copy
    is what makes the profile tree a superset of the shipped one rather
    than a replacement for it. An unparseable XML is recovered from the
    default, since Kodi drops it silently otherwise.
    """
    source_base_path = xbmcvfs.translatePath("special://xbmc/system/library/%s" % kind)
    dest_base_path = xbmcvfs.translatePath("special://profile/library/%s" % kind)

    if not os.path.exists(source_base_path):
        LOG.error("XMLs source path `%s` not found.", source_base_path)
        return

    # Make sure the files exist in the local profile.
    for source_path, dirs, files in os.walk(source_base_path):
        relative_path = os.path.relpath(source_path, source_base_path)
        dest_path = os.path.join(dest_base_path, relative_path)

        # makedirs, not mkdir: on a profile that has never had a tree of this
        # kind the parent does not exist either.
        if not os.path.exists(dest_path):
            os.makedirs(os.path.normpath(dest_path))

        for file_name in files:
            dest_file = os.path.join(dest_path, file_name)
            copy = False

            if not os.path.exists(dest_file):
                copy = True
            elif os.path.splitext(file_name)[1].lower() == ".xml":
                try:
                    etree.parse(dest_file)
                except etree.ParseError:
                    LOG.warning(
                        "Unable to parse `%s`, recovering from default.", dest_file
                    )
                    copy = True

            if copy:
                source_file = os.path.join(source_path, file_name)
                LOG.debug("Copying `%s` -> `%s`", source_file, dest_file)
                xbmcvfs.copy(source_file, dest_file)
