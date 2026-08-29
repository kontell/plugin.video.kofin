"""The downloads IPC wire format (shell refactor P2.4).

What ``DOWNLOAD_ADD`` carries and how the service reads it — kept beside the
downloads package rather than inside ``Service.onNotification``, because it
is the contract between the plugin process that builds the payload
(``plugin/actions.py``, ``downloads/auto.py``) and the manager that consumes
it, not a fact about the notification bus.
"""

from typing import Any, Dict, List, Tuple

from kofin.downloads import store as downloads_store

JsonDict = Dict[str, Any]


def parse_add(payload: JsonDict) -> Tuple[List[str], str, List[str]]:
    """``(ids, origin, media_types)`` for a ``DownloadAdd`` payload.

    The optional Origin marks automatic downloads for W4.2's retention
    sweep; anything unrecognized is a user download — the label that is
    never auto-deleted. Types is optional and positional against Ids: the
    sender holds each item's DTO type, and passing it here is what lets the
    queue split by media kind without an item fetch per row. Paired before
    the empty ids are dropped, so a blank in the middle cannot shift the
    rest by one; a short or absent list just leaves kinds unknown, which the
    video pool claims.
    """
    origin = str(payload.get("Origin") or downloads_store.ORIGIN_USER)
    if origin != downloads_store.ORIGIN_USER and not downloads_store.is_auto_origin(
        origin
    ):
        origin = downloads_store.ORIGIN_USER
    raw_ids = [str(one) for one in (payload.get("Ids") or [])]
    raw_types = [str(one) for one in (payload.get("Types") or [])]
    pairs = [
        (item_id, raw_types[index] if index < len(raw_types) else "")
        for index, item_id in enumerate(raw_ids)
        if item_id
    ]
    return (
        [item_id for item_id, _ in pairs],
        origin,
        [dto_type for _, dto_type in pairs],
    )


def item_id(payload: JsonDict) -> str:
    """The one id a ``DownloadCancel`` / ``DownloadRemove`` names."""
    return str(payload.get("Id") or "")
