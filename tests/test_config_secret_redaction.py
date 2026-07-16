"""Settings repr()/str() must not leak credentials (SEC-19)."""

from src.config import Settings


def _settings():
    # _env_file=None isolates the test from any real .env mounted in the
    # container, so defaults are empty unless we set them here.
    return Settings(
        _env_file=None,
        environment="development",
        base_url="http://example.test",
        secret_key="supersecretvalue",
        anthropic_api_key="sk-ant-LEAKME",
        orcid_client_secret="orcid-LEAKME",
        slack_bot_token_su="xoxb-LEAKME",
        posthog_api_key="phc-LEAKME",
    )


def test_repr_and_str_redact_secrets():
    s = _settings()
    for rendered in (repr(s), str(s)):
        assert "supersecretvalue" not in rendered
        assert "sk-ant-LEAKME" not in rendered
        assert "orcid-LEAKME" not in rendered
        assert "xoxb-LEAKME" not in rendered
        assert "phc-LEAKME" not in rendered
        assert "***REDACTED***" in rendered


def test_non_secret_fields_still_visible():
    r = repr(_settings())
    assert "http://example.test" in r
    assert "development" in r


def test_attribute_reads_return_real_values():
    # Redaction is display-only — reading a field still yields the secret.
    s = _settings()
    assert s.secret_key == "supersecretvalue"
    assert s.slack_bot_token_su == "xoxb-LEAKME"
    assert s.get_slack_tokens()["su"] == "xoxb-LEAKME"


def test_empty_secret_not_labeled_redacted():
    # A credential explicitly set empty stays empty (not masked), so the mask
    # means "a real value is present but hidden". Init kwargs outrank env vars.
    s = Settings(_env_file=None, secret_key="", slack_bot_token_su="filled")
    args = dict(s.__repr_args__())
    assert args["secret_key"] == ""  # empty -> not masked
    assert args["slack_bot_token_su"] == "***REDACTED***"  # non-empty -> masked
