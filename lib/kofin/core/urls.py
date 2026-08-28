"""The one builder of ``plugin://`` URLs (shell refactor P1.5).

Kodi keys what it remembers about a plugin row — its resume bookmark, its
play count — on the exact URL string, so the four independent builders the
tree had grown (listitems, two node generators, the lyrics directory) were
four chances to disagree by a byte. Everything builds through here now.
"""

from typing import Dict
from urllib.parse import urlencode

BASE_URL = "plugin://plugin.video.kofin/"


def plugin_url(params: Dict[str, str]) -> str:
    return BASE_URL + "?" + urlencode(params)
