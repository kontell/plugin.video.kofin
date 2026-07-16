from kofin.core import log


def test_registered_token_is_redacted():
    log.register_secret("sekrit-token-value")
    assert log.mask("auth with sekrit-token-value done") == "auth with *** done"


def test_registered_id_keeps_prefix():
    log.register_secret("215f5fc3f7ff4a5581e8518b28203a4f", keep=6)
    masked = log.mask("user 215f5fc3f7ff4a5581e8518b28203a4f played")
    assert masked == "user 215f5f… played"


def test_api_key_query_param_masked_without_registration():
    masked = log.mask("GET http://s/System/Info?api_key=deadbeef&x=1")
    assert "deadbeef" not in masked
    assert "api_key=***" in masked


def test_authorization_header_token_masked():
    masked = log.mask('MediaBrowser Client="Kofin", Token="abc123"')
    assert "abc123" not in masked
    assert 'Token="***"' in masked


def test_json_credential_fields_masked():
    for field in ("AccessToken", "Pw", "Password", "Secret"):
        masked = log.mask('{"%s": "hunter2"}' % field)
        assert "hunter2" not in masked, field


def test_short_or_empty_secrets_are_ignored():
    log.register_secret("")
    log.register_secret("abc", keep=6)
    assert log.mask("abc") == "abc"


def test_logger_lazy_formatting_survives_bad_args(monkeypatch):
    lines = []
    monkeypatch.setattr("xbmc.log", lambda msg, level=0: lines.append(msg))
    logger = log.Logger("test")
    logger.info("one %s", "arg")
    logger.info("bad %d", "not-a-number")
    assert "one arg" in lines[0]
    assert "not-a-number" in lines[1]
