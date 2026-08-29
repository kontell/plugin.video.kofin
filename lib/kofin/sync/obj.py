# -*- coding: utf-8 -*-
"""Jellyfin-DTO-to-writer-dict mapping engine (fork ``objects/obj.py``,
verbatim port; the mapping file ``obj_map.json`` is data)."""

import json
import os
from typing import Any, Dict

from kofin.core.log import Logger

LOG = Logger(__name__)


class Objects(object):

    # Borg - multiple instances, shared state
    _shared_state: dict = {}

    def __init__(self):
        """Hold all persistent data here."""

        self.__dict__ = self._shared_state

    def mapping(self):
        """Load objects mapping."""
        file_dir = os.path.dirname(__file__)

        with open(os.path.join(file_dir, "obj_map.json")) as infile:
            self.objects = json.load(infile)

    def map(self, item, mapping_name):
        """Syntax to traverse the item dictionary.
        This of the query almost as a url.

        Item is the Jellyfin item json object structure

        ",": each element will be used as a fallback until a value is found.
        "?": split filters and key name from the query part, i.e. MediaSources/0?$Name
        "$": lead the key name with $. Only one key value can be requested per element.
        ":": indicates it's a list of elements [], i.e. MediaSources/0/MediaStreams:?$Name
            MediaStreams is a list.
        "/": indicates where to go directly
        """
        # Build into a local dict: Objects is a Borg, so an attribute here
        # would be shared state — concurrent writers (video and music run in
        # parallel) would corrupt each other's mapping mid-build.
        mapped_item: Dict[str, Any] = {}

        if not mapping_name:
            raise Exception("execute mapping() first")

        mapping = self.objects[mapping_name]

        for key, value in mapping.items():

            mapped_item[key] = None
            params = value.split(",")

            for param in params:

                obj = item
                obj_param = param
                obj_key = ""
                obj_filters = {}

                if "?" in obj_param:

                    if "$" in obj_param:
                        obj_param, obj_key = obj_param.rsplit("$", 1)

                    obj_param, filters = obj_param.rsplit("?", 1)

                    if filters:
                        for filter in filters.split("&"):
                            filter_key, filter_value = filter.split("=")
                            obj_filters[filter_key] = filter_value

                if ":" in obj_param:
                    result = []

                    for d in self.__recursiveloop__(obj, obj_param):

                        if not obj_filters or self.__filters__(d, obj_filters):
                            result.append(d)

                    obj = result
                    obj_filters = {}

                elif "/" in obj_param:
                    obj = self.__recursive__(obj, obj_param)

                elif obj is item and obj is not None:
                    obj = item.get(obj_param)

                if obj_filters and obj and not self.__filters__(obj, obj_filters):
                    obj = None

                if obj is None and len(params) != params.index(param):
                    continue

                if obj_key:
                    obj = (
                        [d[obj_key] for d in obj if d.get(obj_key)]
                        if isinstance(obj, list)
                        else obj.get(obj_key)
                    )

                mapped_item[key] = obj
                break

        if (
            not mapping_name.startswith("Browse")
            and not mapping_name.startswith("Artwork")
            and not mapping_name.startswith("UpNext")
        ):

            mapped_item["ProviderName"] = self.objects.get(
                "%sProviderName" % mapping_name
            )
            # The fork defaulted this to json.dumps(item["UserData"]) — a
            # checksum no comparator matches and every playback moves, so an
            # Etag-less item took the full write cascade on every walk
            # forever (healing-loops-plan F4). check_unchanged overwrites
            # this with the real Etag spelling; None is the honest default
            # for the paths that never get one.
            mapped_item["Checksum"] = None

        return mapped_item

    def __recursiveloop__(self, obj, keys):

        first, rest = keys.split(":", 1)
        obj = self.__recursive__(obj, first)

        if obj:
            for item in obj:
                if rest:
                    # ``yield from``: the recursive call is a generator, and
                    # calling it without iterating yielded nothing at all for
                    # any mapping with two or more ``:`` segments (audit R6;
                    # no mapping in obj_map.json has one yet).
                    yield from self.__recursiveloop__(item, rest)
                else:
                    yield item

    def __recursive__(self, obj, keys):

        for string in keys.split("/"):

            if not obj:
                return

            obj = obj[int(string)] if string.isdigit() else obj.get(string)

        return obj

    def __filters__(self, obj, filters):
        # Every filter must hold: the fork assigned the result on each pass,
        # so with two ``&``-joined filters only the last one decided (audit
        # R6; no mapping in obj_map.json joins two yet). No filters is still
        # no match, as before — callers never reach here without one.
        result = bool(filters)

        for key, value in filters.items():

            inverse = False

            if value.startswith("!"):

                inverse = True
                value = value.split("!", 1)[1]

            if value.lower() == "null":
                value = None

            matched = obj.get(key) != value if inverse else obj.get(key) == value
            result = result and matched

        return result
