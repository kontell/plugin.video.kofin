"""The downloads IPC wire format (shell refactor P2.4): the pairing and
origin rules that used to live inline in Service.onNotification, and that
test_service still exercises end to end."""

from kofin.downloads import store, wire


def test_types_pair_positionally_before_blank_ids_are_dropped():
    parsed = wire.parse_add(
        {"Ids": ["a", "", "c"], "Types": ["Movie", "Episode", "Audio"]}
    )
    assert parsed.ids == ["a", "c"]
    # the blank's type went with the blank
    assert parsed.media_types == ["Movie", "Audio"]
    assert parsed.origin == store.ORIGIN_USER


def test_a_short_or_absent_types_list_leaves_kinds_unknown():
    parsed = wire.parse_add({"Ids": ["d", "e"], "Types": ["Movie"]})
    assert parsed.ids == ["d", "e"]
    assert parsed.media_types == ["Movie", ""]
    assert wire.parse_add({"Ids": ["f"]}).media_types == [""]


def test_a_request_is_optional_and_defaults_to_none_at_all():
    """D6: a plugin process left over from before the add-on update sends
    no Request, and a row without one announces per item exactly as every
    row did before."""
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
    """The batch is the shape a container removal sends. The single Id is
    what the service's own automatic paths send, and what a plugin process
    from before an add-on update would send — reading it is what keeps a
    mid-session update from silently dropping a removal."""
    assert wire.item_ids({"Ids": ["a", "b"]}) == ["a", "b"]
    assert wire.item_ids({"Ids": [1, 2]}) == ["1", "2"]
    assert wire.item_ids({"Ids": ["a", "", None]}) == ["a"]
    assert wire.item_ids({"Id": "c"}) == ["c"]
    assert wire.item_ids({"Ids": []}) == []
    assert wire.item_ids({}) == []
