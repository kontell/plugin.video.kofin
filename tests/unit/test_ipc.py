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


def _split_params(text):
    """CUtil::SplitParams' quote handling, as Kodi 21.3 has it: a quote
    preceded by a backslash is escaped — unless that backslash was itself
    escaped, because "only every second character can be escaped" — and
    the parameter ends at the first unescaped quote after the opening
    one. Returns the first parameter's unescaped text."""
    out = []
    in_quotes = False
    last_escaped = False
    for index, ch in enumerate(text):
        escaped = index > 0 and text[index - 1] == "\\" and not last_escaped
        last_escaped = escaped
        if ch == '"' and not escaped:
            if in_quotes:
                break
            in_quotes = True
            continue
        if ch == "\\" and index + 1 < len(text) and text[index + 1] == '"':
            continue  # the escaping backslash itself is not part of the value
        if in_quotes:
            out.append(ch)
    return "".join(out)


def test_encode_decode_round_trip():
    payload = {"a": 1, "b": "two", "nested": {"c": [1, 2]}}
    assert ipc.decode(_split_params(ipc._encode(payload))) == payload


def test_a_payload_with_quotes_and_backslashes_survives_the_builtin_parser():
    """json.dumps writes a quote inside a value as \\" and the old encoder
    then escaped that quote again, producing \\\\" — which SplitParams reads
    as an escaped backslash followed by the closing quote (audit M1). No
    payload carries free text today; this is the trap for the next field
    that does (a name, a title)."""
    payload = {"Name": 'The "Example" \\ Film', "Id": "abc", "Types": ["Movie"]}
    assert ipc.decode(_split_params(ipc._encode(payload))) == payload


def test_the_wire_form_has_nothing_for_the_builtin_parser_to_misread():
    encoded = ipc._encode({"x": '"'})
    inner = encoded[1:-1]
    assert inner.startswith('[\\"') and inner.endswith('\\"]')
    body = inner[3:-3]
    assert body and all(ch in "0123456789abcdef" for ch in body)


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


def test_the_secret_is_owner_only_from_the_moment_it_exists(nonce_file, monkeypatch):
    """The temp file used to be created under the process umask and
    chmod'ed afterwards — a window in which it was world-readable (audit
    M2). With the chmod taken away, the mode must already be right."""
    import os

    monkeypatch.setattr(ipc.os, "chmod", lambda path, mode: None)
    monkeypatch.setattr(ipc.os, "umask", os.umask)
    previous = os.umask(0o022)  # the common default: 0644 for open()
    try:
        ipc.rotate_nonce()
    finally:
        os.umask(previous)

    assert oct(nonce_file.stat().st_mode)[-3:] == "600"


def test_verify_compares_in_constant_time(nonce_file, monkeypatch):
    seen = []

    def compare(a, b):
        seen.append((a, b))
        return a == b

    monkeypatch.setattr(ipc.hmac, "compare_digest", compare)
    secret = ipc.rotate_nonce()

    assert ipc.verify(ipc.REMOVE_LIBRARY, {ipc.NONCE_KEY: secret}, secret) is True
    assert ipc.verify(ipc.REMOVE_LIBRARY, {ipc.NONCE_KEY: "x"}, secret) is False
    assert len(seen) == 2


def test_a_guarded_command_carries_the_secret_and_others_do_not(
    nonce_file, monkeypatch
):
    sent = []
    monkeypatch.setattr(ipc.xbmc, "executebuiltin", sent.append)
    secret = ipc.rotate_nonce()

    ipc.notify(ipc.REMOVE_LIBRARY, {"Id": "lib1"})
    ipc.notify(ipc.PRECACHE_ART, {})

    def payload_of(builtin):
        # NotifyAll(sender, method, <param>) — the third argument, as the
        # builtin parser would hand it to the receiver.
        return ipc.decode(_split_params(builtin.split(", ", 2)[2].rstrip(")")))

    assert payload_of(sent[0]) == {"Id": "lib1", ipc.NONCE_KEY: secret}
    assert payload_of(sent[1]) == {}


def test_verify_rejects_a_forged_destructive_command(nonce_file):
    secret = ipc.rotate_nonce()
    # What a forger can send: our sender string, the right method, no secret.
    assert ipc.verify(ipc.REMOVE_LIBRARY, {"Id": "lib1"}, secret) is False
    assert ipc.verify(ipc.REPAIR_LIBRARY, {"_nonce": "guessed"}, secret) is False
    assert ipc.verify(ipc.REMOVE_LIBRARY, {"_nonce": secret}, secret) is True


def test_unguarded_commands_are_not_gated(nonce_file):
    secret = ipc.rotate_nonce()
    assert ipc.verify(ipc.PRECACHE_ART, {}, secret) is True
    assert ipc.verify(ipc.WHO_IS_WATCHING, {}, secret) is True


def test_every_library_command_is_guarded(nonce_file):
    """A prune deletes rows and a boxsets refresh re-walks every collection —
    the guard's own rationale — so UpdateLibrary and RefreshBoxsets carry
    the secret like Remove and Repair do."""
    secret = ipc.rotate_nonce()
    assert {
        ipc.REMOVE_LIBRARY,
        ipc.REPAIR_LIBRARY,
        ipc.UPDATE_LIBRARY,
        ipc.REFRESH_BOXSETS,
    } <= ipc.GUARDED
    assert ipc.verify(ipc.UPDATE_LIBRARY, {}, secret) is False
    assert ipc.verify(ipc.REFRESH_BOXSETS, {}, secret) is False


def test_sync_library_is_not_a_message(monkeypatch):
    """Nothing ever sent it over NotifyAll (its producers enqueue on the
    Library directly), and unguarded it was a forgeable full sync."""
    monkeypatch.setattr("xbmc.executebuiltin", lambda cmd: None)
    assert not hasattr(ipc, "SYNC_LIBRARY")
    with pytest.raises(ValueError):
        ipc.notify("SyncLibrary", {"Id": "lib1"})


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
