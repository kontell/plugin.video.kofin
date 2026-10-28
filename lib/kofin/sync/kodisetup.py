# -*- coding: utf-8 -*-
"""Kodi's shipped node trees, seeded into the profile before kofin writes
its own folder there (``sync/dynamic.py``).
"""

import os
import xml.etree.ElementTree as etree

import xbmcvfs

from kofin.core.log import Logger

LOG = Logger(__name__)


def seed_default_nodes(kind):
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

    for source_path, _dirs, files in os.walk(source_base_path):
        relative_path = os.path.relpath(source_path, source_base_path)
        dest_path = os.path.join(dest_base_path, relative_path)

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
