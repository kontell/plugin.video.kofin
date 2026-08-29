"""The one builder of ``plugin://`` URLs (shell refactor P1.5).

Kodi keys what it remembers about a plugin row — its resume bookmark, its
play count — on the exact URL string, so the four independent builders the
tree had grown (listitems, two node generators, the lyrics directory) were
four chances to disagree by a byte. Everything builds through here now.
"""

from typing import Dict
from urllib.parse import urlencode

BASE_URL = "plugin://plugin.video.kofin/"

# The play route's own parameters, named once (shell refactor P2.2): the
# context item and the stream menu build a play URL with them, the play
# route reads them, and the three used to spell each name independently —
# 4/4/5/3/3/3 copies across play.py, context.py and streams.py. A request
# carrying any of STREAM_REQUEST_PARAMS names a stream or a quality the
# server must serve, which is what keeps it off a download.
PARAM_TRANSCODE = "transcode"
PARAM_BITRATE = "bitrate"
PARAM_MEDIA_SOURCE = "mediasourceid"
PARAM_AUDIO_INDEX = "audioindex"
PARAM_SUBTITLE_INDEX = "subtitleindex"
PARAM_BURN_SUBS = "burnsubs"
STREAM_REQUEST_PARAMS = (
    PARAM_TRANSCODE,
    PARAM_BITRATE,
    PARAM_MEDIA_SOURCE,
    PARAM_AUDIO_INDEX,
    PARAM_SUBTITLE_INDEX,
    PARAM_BURN_SUBS,
)


def plugin_url(params: Dict[str, str]) -> str:
    return BASE_URL + "?" + urlencode(params)
