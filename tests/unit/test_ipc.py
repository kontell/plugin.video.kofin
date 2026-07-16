import pytest

from kofin.core import ipc


def test_notify_rejects_unregistered(monkeypatch):
    monkeypatch.setattr("xbmc.executebuiltin", lambda cmd: None)
    with pytest.raises(ValueError):
        ipc.notify("NotAMessage")


def test_notify_encodes_payload(monkeypatch):
    sent = []
    monkeypatch.setattr("xbmc.executebuiltin", lambda cmd: sent.append(cmd))
    ipc.notify(ipc.RESTART, {"why": "test"})
    assert sent and sent[0].startswith("NotifyAll(plugin.video.kofin, Restart,")


def test_encode_decode_round_trip():
    payload = {"a": 1, "b": "two", "nested": {"c": [1, 2]}}
    encoded = ipc._encode(payload)
    # Kodi's builtin parser strips the outer quotes and unescapes; simulate.
    wire = encoded[1:-1].replace('\\"', '"')
    assert ipc.decode(wire) == payload


def test_decode_hex_signal_payload():
    payload = {"play_info": {"ItemIds": ["x"]}}
    wire = '["%s"]' % ipc.encode_hex(payload)
    assert ipc.decode(wire) == payload


def test_decode_garbage_is_empty():
    assert ipc.decode("[]") == {}


def test_method_name_strips_kodi_prefix():
    assert ipc.method_name("Other.Restart") == "Restart"
    assert ipc.method_name("Restart") == "Restart"
