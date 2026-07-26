"""Reads of Kodi's own library state over JSON-RPC.

Both processes need these. The play route has to start where Kodi is about to
seek, and the service has to confirm a resume bookmark really is gone before
acting on the announcement that says so.
"""

import json
from typing import Any, Dict, Optional

import xbmc

from kofin.core.log import Logger

LOG = Logger(__name__)

# Kodi media type -> (JSON-RPC method, id parameter, result key). The three
# video types kofin syncs; songs carry no resume point.
RESUME_QUERY = {
    "movie": ("VideoLibrary.GetMovieDetails", "movieid", "moviedetails"),
    "episode": ("VideoLibrary.GetEpisodeDetails", "episodeid", "episodedetails"),
    "musicvideo": (
        "VideoLibrary.GetMusicVideoDetails",
        "musicvideoid",
        "musicvideodetails",
    ),
}


def resume_seconds(kodi_id: int, media: str) -> Optional[float]:
    """Kodi's stored resume position for a library row.

    0.0 when the row has no bookmark — which is an answer, not a failure, and
    the callers rely on telling the two apart. None only when the row cannot
    be read at all.
    """
    query = RESUME_QUERY.get(media)
    if query is None:
        return None
    method, id_field, result_field = query
    try:
        response: Dict[str, Any] = json.loads(
            xbmc.executeJSONRPC(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": method,
                        "params": {id_field: kodi_id, "properties": ["resume"]},
                    }
                )
            )
        )
        return float(response["result"][result_field]["resume"]["position"])
    except Exception as error:
        LOG.debug("resume read failed for %s/%s: %s", media, kodi_id, error)
        return None
