"""A download's mark on its library row, carried by the pass.

The SQL build stamps the badge art and the "Kofin Downloads" tag itself;
here they are part of the desired state: a downloaded movie or episode
carries the badge, a downloaded movie and the show of a downloaded episode
carry the tag. The state is an input of the desired state, so it joins the
inputs token (metadata.inputs_token) and a change of it re-plans the row
(downloads/nativeport.NativeApi). The badge key has no dot: a dotted art key
is accepted by the setter and never stored (feasibility report, §7).
"""

from typing import Dict, Set, Tuple

from kofin.core.log import Logger

LOG = Logger(__name__)

BADGE_ART = "kofindownloaded"
BADGE_URL = "special://home/addons/plugin.video.kofin/resources/media/downloaded.png"


def marks() -> Tuple[Set[str], Set[str]]:
    """``(downloaded item ids, series ids with a downloaded episode)``,
    read once a pass from kofin's own store."""
    try:
        from kofin.downloads import store

        rows = store.rows(store.DONE)
    except Exception:
        LOG.exception("download rows unavailable to the pass")
        return set(), set()
    done = {row.jellyfin_id for row in rows}
    series = {
        row.series_id for row in rows if row.media_type == "episode" and row.series_id
    }
    return done, series


def flag(kind: str, item_id: str, done: Set[str], series: Set[str]) -> str:
    """What of the download state a row of this kind reads: an input token."""
    if kind in ("Movie", "Episode") and item_id in done:
        return "downloaded"
    if kind == "Series" and item_id in series:
        return "downloaded"
    return ""


def apply(
    kind: str, item_id: str, desired: Dict, done: Set[str], series: Set[str]
) -> None:
    """Put the badge and the tag into a desired state."""
    from kofin.downloads import TAG

    if not flag(kind, item_id, done, series):
        return
    if kind in ("Movie", "Episode"):
        desired.setdefault("art", {})[BADGE_ART] = BADGE_URL
    if kind in ("Movie", "Series") and isinstance(desired.get("tag"), list):
        desired["tag"] = sorted(set(desired["tag"]) | {TAG})
