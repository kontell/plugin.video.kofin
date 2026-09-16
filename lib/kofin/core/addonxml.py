"""The installed addon.xml, for the few settings that live in it.

Kodi reads ``<reuselanguageinvoker>`` from ``xbmc.addon.metadata`` at addon
load (kodi-addon-manifest). A settings toggle can only change the on-disk
file; the next Kodi restart is what makes ExtraInfo() move. The zip always
ships true; after an update overwrites the file, the service reconciles a
false setting back onto disk.
"""

import os
import re
from typing import Optional

from kofin.core import settings
from kofin.core.log import Logger

LOG = Logger(__name__)

REUSE_TAG = re.compile(r"(<reuselanguageinvoker>)(true|false)(</reuselanguageinvoker>)")


def with_reuse_invoker(text: str, enabled: bool) -> Optional[str]:
    """``text`` with the tag set to true/false, or None if the tag is absent."""
    wanted = "true" if enabled else "false"
    new, count = REUSE_TAG.subn(r"\g<1>%s\g<3>" % wanted, text, count=1)
    return new if count else None


def apply(enabled: bool, path: Optional[str] = None) -> Optional[bool]:
    """Align the installed addon.xml with ``enabled``.

    Returns True if a write happened, False if the file already matched,
    None if the file could not be read or written or had no tag.
    """
    target = path
    if target is None:
        root = settings.addon_path()
        if not root:
            LOG.warning("reuseLanguageInvoker: addon path unavailable")
            return None
        target = os.path.join(root, "addon.xml")
    try:
        with open(target, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as error:
        LOG.warning("reuseLanguageInvoker: could not read %s: %s", target, error)
        return None
    updated = with_reuse_invoker(text, enabled)
    if updated is None:
        LOG.warning("reuseLanguageInvoker: no <reuselanguageinvoker> tag in %s", target)
        return None
    if updated == text:
        return False
    temporary = target + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(updated)
        os.replace(temporary, target)
    except OSError as error:
        LOG.warning("reuseLanguageInvoker: could not write %s: %s", target, error)
        try:
            os.remove(temporary)
        except OSError:
            pass
        return None
    LOG.info("addon.xml reuselanguageinvoker set to %s", "true" if enabled else "false")
    return True
