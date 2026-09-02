"""The public provider contract (plan G2.1): validation and conversion.

The decoder runs on Kodi's notification thread against payloads written by
arbitrary add-ons — every test here is a promise about what cannot crash it
and what cannot get through it.
"""

import json


from kofin.core import contract


def claim_payload(**extra):
    payload = {"v": 1, "provider": "example", "key": "abc123"}
    payload.update(extra)
    return payload


# --- decode ---------------------------------------------------------------


def test_a_valid_claim_decodes():
    payload = contract.decode(contract.CLAIM, json.dumps(claim_payload()))

    assert payload is not None
    assert payload["provider"] == "example"


def test_the_builtin_wrapped_list_form_is_accepted():
    payload = contract.decode(contract.CLAIM, json.dumps([claim_payload()]))

    assert payload is not None


def test_an_unknown_message_name_is_refused():
    assert contract.decode("SyncProvider.Delete", json.dumps(claim_payload())) is None


def test_an_unsupported_version_is_dropped_not_guessed():
    assert contract.decode(contract.CLAIM, json.dumps(claim_payload(v=2))) is None
    assert (
        contract.decode(contract.CLAIM, json.dumps({"provider": "x", "key": "y"}))
        is None
    )


def test_a_missing_required_field_is_refused():
    assert (
        contract.decode(contract.CLAIM, json.dumps({"v": 1, "provider": "x"})) is None
    )
    assert (
        contract.decode(contract.REGISTER, json.dumps({"v": 1, "provider": "x"}))
        is None
    )


def test_a_bad_provider_name_is_refused():
    for name in ("", "UPPER", "has space", "x" * 41, None, 7):
        assert (
            contract.decode(contract.CLAIM, json.dumps(claim_payload(provider=name)))
            is None
        )


def test_an_oversize_or_unparseable_payload_never_raises():
    assert (
        contract.decode(contract.CLAIM, "x" * (contract.MAX_PAYLOAD_BYTES + 1)) is None
    )
    assert contract.decode(contract.CLAIM, "{not json") is None
    assert contract.decode(contract.CLAIM, json.dumps("a string")) is None
    assert contract.decode(contract.CLAIM, json.dumps([1, 2])) is None


def test_an_oversize_key_is_refused():
    long_key = "k" * (contract.MAX_KEY_LENGTH + 1)
    assert (
        contract.decode(contract.CLAIM, json.dumps(claim_payload(key=long_key))) is None
    )


def test_menu_needs_only_the_version():
    assert contract.decode(contract.MENU, json.dumps({"v": 1})) is not None


def test_unknown_fields_ride_along():
    """Additive evolution: a newer provider's extra fields must not break
    an older service."""
    payload = contract.decode(
        contract.CLAIM, json.dumps(claim_payload(future_field="yes"))
    )

    assert payload is not None


# --- register_template ----------------------------------------------------


def test_a_register_template_validates():
    play = contract.register_template(
        {"play": {"url_template": "plugin://x/?id={key}&seek={position_s}"}}
    )

    assert play == {
        "url_template": "plugin://x/?id={key}&seek={position_s}",
        "audio": False,
    }


def test_a_delegated_registration_needs_no_template():
    # The content is tuned, not fetched (a PVR EPG tag has no URL): the
    # provider executes SyncSession.Start itself.
    play = contract.register_template({"play": {"delegated": True}})
    assert play == {"delegated": True, "audio": False}


def test_a_template_without_the_key_token_is_useless_and_refused():
    assert contract.register_template({"play": {"url_template": "plugin://x/"}}) is None
    assert contract.register_template({"play": "nope"}) is None
    assert contract.register_template({}) is None
    assert (
        contract.register_template(
            {"play": {"url_template": "{key}" + "x" * contract.MAX_TEMPLATE_LENGTH}}
        )
        is None
    )


# --- engine_claim ---------------------------------------------------------


def test_engine_claim_maps_the_wire_shape():
    claim = contract.engine_claim(
        claim_payload(
            play_method="Transcode",
            play_session="ps1",
            tempo={"file": "/tmp/t", "queue_secs": 2.5, "manifest_type": "hls"},
        )
    )

    assert claim == {
        "Id": "abc123",
        "Provider": "example",
        "PlayMethod": "Transcode",
        "PlaySessionId": "ps1",
        "Tempo": {"File": "/tmp/t", "QueueSecs": 2.5, "ManifestType": "hls"},
    }


def test_engine_claim_defaults_and_refuses_smuggling():
    claim = contract.engine_claim(claim_payload(play_method="Nonsense", Extra="x"))

    assert claim["PlayMethod"] == "DirectPlay"
    assert "Extra" not in claim
    assert "Tempo" not in claim


def test_engine_claim_survives_a_broken_tempo_route():
    claim = contract.engine_claim(
        claim_payload(tempo={"file": "/t", "queue_secs": "?"})
    )

    assert claim["Tempo"]["QueueSecs"] == 8.0


# --- publish_state --------------------------------------------------------


def test_publish_state_pings_over_json_rpc(monkeypatch):
    sent = []
    monkeypatch.setattr("xbmc.executeJSONRPC", lambda raw: sent.append(raw) or "")

    contract.publish_state()

    body = json.loads(sent[0])
    assert body["method"] == "JSONRPC.NotifyAll"
    assert body["params"]["message"] == contract.STATE
    assert body["params"]["data"] == {"v": contract.VERSION}
