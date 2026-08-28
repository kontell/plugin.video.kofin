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


def test_decode_garbage_is_empty():
    assert ipc.decode("[]") == {}


def test_method_name_strips_kodi_prefix():
    assert ipc.method_name("Other.Restart") == "Restart"
    assert ipc.method_name("Restart") == "Restart"


# --- the nonce on destructive commands (audit finding #20) --------------------


@pytest.fixture
def nonce_file(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "xbmcvfs.translatePath", lambda path: str(tmp_path / "ipc.nonce")
    )
    return tmp_path / "ipc.nonce"


def test_rotate_writes_a_fresh_secret_each_generation(nonce_file):
    first = ipc.rotate_nonce()
    second = ipc.rotate_nonce()
    assert first and second and first != second
    assert ipc.nonce() == second
    # Owner-only: the whole point is that it is harder to read than a window
    # property, which JSON-RPC can fetch outright.
    assert oct(nonce_file.stat().st_mode)[-3:] == "600"


def test_a_guarded_command_carries_the_secret_and_others_do_not(
    nonce_file, monkeypatch
):
    sent = []
    monkeypatch.setattr(ipc.xbmc, "executebuiltin", sent.append)
    secret = ipc.rotate_nonce()

    ipc.notify(ipc.REMOVE_LIBRARY, {"Id": "lib1"})
    ipc.notify(ipc.SYNC_LIBRARY, {"Id": "lib1"})

    assert secret in sent[0]
    assert secret not in sent[1]


def test_verify_rejects_a_forged_destructive_command(nonce_file):
    secret = ipc.rotate_nonce()
    # What a forger can send: our sender string, the right method, no secret.
    assert ipc.verify(ipc.REMOVE_LIBRARY, {"Id": "lib1"}, secret) is False
    assert ipc.verify(ipc.REPAIR_LIBRARY, {"_nonce": "guessed"}, secret) is False
    assert ipc.verify(ipc.REMOVE_LIBRARY, {"_nonce": secret}, secret) is True


def test_unguarded_commands_are_not_gated(nonce_file):
    secret = ipc.rotate_nonce()
    assert ipc.verify(ipc.SYNC_LIBRARY, {}, secret) is True
    assert ipc.verify(ipc.WHO_IS_WATCHING, {}, secret) is True


def test_a_service_with_no_secret_accepts_nothing_guarded():
    """Absence must not be a way to disable the guard: deleting the file would
    otherwise re-open exactly the hole this closes."""
    assert ipc.verify(ipc.RESTART, {"_nonce": "anything"}, "") is False


def test_decode_never_raises_on_rubbish():
    """It runs on Kodi's notification thread, where the payload is whatever
    the sender chose to put there (audit finding #21)."""
    assert ipc.decode("not json") == {}
    assert ipc.decode('["zz-not-hex"]') == {}
    assert ipc.decode("[]") == {}
    assert ipc.decode('[{"Id": "lib1"}]') == {"Id": "lib1"}


def test_download_commands_are_guarded():
    """REMOVE deletes files, ADD pulls gigabytes on someone else's say-so,
    CANCEL wastes work, REMOVE_ALL empties the lot — every one of them
    carries the shared secret."""
    from kofin.core import ipc

    assert {
        ipc.DOWNLOAD_ADD,
        ipc.DOWNLOAD_CANCEL,
        ipc.DOWNLOAD_REMOVE,
        ipc.DOWNLOAD_REMOVE_ALL,
    } <= ipc.GUARDED
