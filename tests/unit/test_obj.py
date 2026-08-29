"""The obj_map mapping engine's two traps (audit R6, fixes plan H10).

Neither was reachable — no mapping in obj_map.json has two ``:`` segments
or two ``&``-joined filters — so these pin the engine for the next mapping
that does, not a live defect.
"""

from kofin.sync.obj import Objects

ITEM = {
    "MediaSources": [
        {
            "Id": "s1",
            "MediaStreams": [
                {"Type": "Video", "Codec": "hevc"},
                {"Type": "Audio", "Codec": "aac"},
            ],
        },
        {
            "Id": "s2",
            "MediaStreams": [
                {"Type": "Audio", "Codec": "flac"},
            ],
        },
    ]
}


def test_a_two_level_list_query_yields_every_leaf():
    """``__recursiveloop__`` recursed without ``yield from`` and discarded
    the inner generator, so ``A:B:`` yielded nothing at all."""
    engine = Objects()

    leaves = list(engine.__recursiveloop__(ITEM, "MediaSources:MediaStreams:"))

    assert [leaf["Codec"] for leaf in leaves] == ["hevc", "aac", "flac"]


def test_a_single_level_list_query_is_unchanged():
    engine = Objects()

    sources = list(engine.__recursiveloop__(ITEM, "MediaSources:"))

    assert [source["Id"] for source in sources] == ["s1", "s2"]


def test_every_filter_must_hold():
    """``__filters__`` assigned the result on each pass, so with two
    filters only the last one decided: an Audio/aac stream passed a
    ``Type=Video&Codec=aac`` filter."""
    engine = Objects()
    stream = {"Type": "Audio", "Codec": "aac"}

    assert engine.__filters__(stream, {"Type": "Audio", "Codec": "aac"}) is True
    assert engine.__filters__(stream, {"Type": "Video", "Codec": "aac"}) is False
    assert engine.__filters__(stream, {"Type": "Audio", "Codec": "flac"}) is False


def test_single_filter_semantics_are_unchanged():
    engine = Objects()
    stream = {"Type": "Audio", "Codec": "aac", "Language": None}

    assert engine.__filters__(stream, {"Type": "Audio"}) is True
    assert engine.__filters__(stream, {"Type": "!Audio"}) is False
    assert engine.__filters__(stream, {"Language": "null"}) is True
    assert engine.__filters__(stream, {"Language": "!null"}) is False
    assert engine.__filters__(stream, {}) is False  # as before: no match
