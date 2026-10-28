"""The downloads IPC wire format: the ids, types, origin and request a
download message carries."""

from kofin.downloads import store, wire


def test_types_pair_positionally_before_blank_ids_are_dropped():
    parsed = wire.parse_add(
        {"Ids": ["a", "", "c"], "Types": ["Movie", "Episode", "Audio"]}
    )
    assert parsed.ids == ["a", "c"]
    assert parsed.media_types == ["Movie", "Audio"]
    assert parsed.origin == store.ORIGIN_USER


def test_a_short_or_absent_types_list_leaves_kinds_unknown():
    parsed = wire.parse_add({"Ids": ["d", "e"], "Types": ["Movie"]})
    assert parsed.ids == ["d", "e"]
    assert parsed.media_types == ["Movie", ""]
    assert wire.parse_add({"Ids": ["f"]}).media_types == [""]


def test_a_request_is_optional_and_defaults_to_none_at_all():
    """A payload without a Request, as the automatic paths and an older
    plugin process send it, parses to an empty request."""
    parsed = wire.parse_add({"Ids": ["a"]})
    assert parsed.request_id == "" and parsed.request_name == ""

    parsed = wire.parse_add(
        {"Ids": ["a", "b"], "Request": "pl1", "RequestName": "Road Trip"}
    )
    assert parsed.request_id == "pl1"
    assert parsed.request_name == "Road Trip"


def test_origin_is_user_unless_it_is_a_known_automatic_one():
    assert wire.parse_add({"Ids": ["a"], "Origin": "auto:s1"})[1] == "auto:s1"
    assert wire.parse_add({"Ids": ["a"], "Origin": "junk"})[1] == store.ORIGIN_USER
    assert wire.parse_add({"Ids": ["a"]})[1] == store.ORIGIN_USER


def test_item_id_is_a_string_or_empty():
    assert wire.item_id({"Id": "c"}) == "c"
    assert wire.item_id({"Id": 7}) == "7"
    assert wire.item_id({}) == ""


def test_remove_reads_a_list_and_still_reads_a_bare_id():
    """A removal names a list of ids; a bare Id, the shape an older plugin
    process sends, is read as well, so a mid-session add-on update drops no
    removal."""
    assert wire.item_ids({"Ids": ["a", "b"]}) == ["a", "b"]
    assert wire.item_ids({"Ids": [1, 2]}) == ["1", "2"]
    assert wire.item_ids({"Ids": ["a", "", None]}) == ["a"]
    assert wire.item_ids({"Id": "c"}) == ["c"]
    assert wire.item_ids({"Ids": []}) == []
    assert wire.item_ids({}) == []
