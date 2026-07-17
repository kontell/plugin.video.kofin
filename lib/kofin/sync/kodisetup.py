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
        notification(localized(30414), time_ms=8000, error=True)
        return True

    return False


def verify_kodi_defaults():
    """Make sure we have the kodi default node files in place."""

    source_base_path = xbmcvfs.translatePath("special://xbmc/system/library/video")
    dest_base_path = xbmcvfs.translatePath("special://profile/library/video")

    if not os.path.exists(source_base_path):
        LOG.error("XMLs source path `%s` not found.", source_base_path)
        return

    # Make sure the files exist in the local profile.
    for source_path, dirs, files in os.walk(source_base_path):
        relative_path = os.path.relpath(source_path, source_base_path)
        dest_path = os.path.join(dest_base_path, relative_path)

        if not os.path.exists(dest_path):
            os.mkdir(os.path.normpath(dest_path))

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

    # The fork forced its own ordering onto the default movie/tvshow/musicvideo
    # nodes here; kofin does not touch the user's node order (plan §3).

    playlist_path = xbmcvfs.translatePath("special://profile/playlists/video")

    if not xbmcvfs.exists(playlist_path):
        xbmcvfs.mkdirs(playlist_path)
