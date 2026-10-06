"""Publish dynamic skin entries without consulting native sync state."""

from typing import NamedTuple

from kofin.sync.nodes import props


class View(NamedTuple):
    view_id: str
    view_name: str
    media_type: str


def publish(items, server):
    views = [
        View(i["Id"], i.get("Name", ""), i.get("CollectionType") or "mixed")
        for i in items
        if i.get("Id") and i.get("CollectionType") != "livetv"
    ]
    props.publish(
        views,
        {"Whitelist": [], "SortedViews": [v.view_id for v in views]},
        [],
        items,
        server,
    )
