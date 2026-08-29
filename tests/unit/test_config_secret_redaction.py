"""Settings repr()/str() must not leak credentials (SEC-19)."""

import pytest

from src.config import _SECRET_NAME_HINTS, Settings, _redact_url_credentials

# A DSN with the password embedded in the userinfo — the shape the app ships with
# (docker-compose sets DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi).
# `database_url` matches none of the credential name hints, so before the positional
# URL redaction the whole DSN, password included, appeared verbatim in repr(settings).
LEAKY_DSN = "postgresql+asyncpg://copi:sup3rs3cr3t@postgres:5432/copi"


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


# --- credentials embedded in a URL/DSN --------------------------------------------


def test_database_url_password_is_redacted():
    """The leak this file missed: DATABASE_URL carries the DB password in its
    userinfo, and `database_url` matches no credential name hint."""
    s = Settings(_env_file=None, database_url=LEAKY_DSN)
    for rendered in (repr(s), str(s)):
        assert "sup3rs3cr3t" not in rendered
        assert LEAKY_DSN not in rendered


def test_only_the_password_component_of_a_dsn_is_masked():
    """Positional, not whole-value, masking. An operator debugging a deploy needs to
    see which host/port/database the app is pointed at; only the password is secret.
    (Same choice as SQLAlchemy's URL.render_as_string(hide_password=True).)"""
    r = repr(Settings(_env_file=None, database_url=LEAKY_DSN))
    assert "postgresql+asyncpg://copi:***REDACTED***@postgres:5432/copi" in r


def test_dsn_with_no_userinfo_is_shown_in_full():
    """Documented decision: a URL with no userinfo holds no credential, so it is NOT
    masked. Masking it would destroy the one field an operator most needs in a deploy
    postmortem, and would make the mask ambiguous about whether a password exists."""
    plain = "postgresql://postgres:5432/copi"
    r = repr(Settings(_env_file=None, database_url=plain))
    assert plain in r


def test_dsn_with_a_bare_username_keeps_the_username_visible():
    """A userinfo with no ":" is a username, not a credential. A URL whose userinfo
    *is* the credential would live in a *_token field and be masked whole by name."""
    r = repr(Settings(_env_file=None, database_url="postgresql://copi@postgres/copi"))
    assert "postgresql://copi@postgres/copi" in r


def test_dsn_with_an_empty_password_is_not_labeled_redacted():
    """Mirrors test_empty_secret_not_labeled_redacted for the positional path: the
    mask must mean "a real value is hidden here"."""
    r = repr(Settings(_env_file=None, database_url="postgresql://copi:@postgres/copi"))
    assert "postgresql://copi:@postgres/copi" in r


def test_a_password_in_the_dsn_query_string_is_redacted():
    """libpq/asyncpg also accept `?password=`. Control in the same assertion: a
    non-credential parameter next to it stays visible."""
    dsn = "postgresql://postgres:5432/copi?sslmode=require&password=hunter2"
    r = repr(Settings(_env_file=None, database_url=dsn))
    assert "hunter2" not in r
    assert "sslmode=require" in r
    assert "password=***REDACTED***" in r


def test_a_key_file_path_in_the_dsn_query_string_stays_visible():
    """Over-redaction is a real cost: `?sslkey=` is a filename an operator needs, not
    a secret, so the query-parameter hints are narrower than the field-name hints."""
    dsn = "postgresql://postgres:5432/copi?sslkey=/etc/ssl/client.key"
    r = repr(Settings(_env_file=None, database_url=dsn))
    assert "/etc/ssl/client.key" in r


def test_reading_database_url_still_returns_the_real_dsn():
    """Redaction is display-only — create_async_engine(settings.database_url) must
    still get a connectable DSN. No field changed type."""
    s = Settings(_env_file=None, database_url=LEAKY_DSN)
    assert s.database_url == LEAKY_DSN
    assert "REDACTED" not in s.database_url


# --- systematic sweep over every string field -------------------------------------

# Every `str`-annotated field whose value cannot carry a credential, and is therefore
# expected to render in the clear. Criterion: the value is a public identifier, an
# infrastructure name, or an operator-facing diagnostic — never a bearer credential,
# a signing key, or a password. Adding a field to Settings that renders in the clear
# forces an edit here, i.e. an explicit classification.
NON_SECRET_STR_FIELDS = {
    "environment",
    "orcid_client_id",       # OAuth *public* client id; ships in the browser redirect
    "orcid_redirect_uri",
    "database_url",          # a plain sentinel has no userinfo -> nothing to mask
    "ncbi_contact_email",
    "base_url",
    "aws_region",
    "ses_sender_email",
    "ses_reply_domain",
    "ses_inbound_s3_bucket",
    "ses_inbound_s3_prefix",
    "outbound_email_allowlist",
    "llm_profile_model",
    "llm_agent_model",
    "llm_agent_model_opus",
    "llm_agent_model_sonnet",
    "llm_review_model",
    # Channel names for the run-start announcement — public channel names,
    # not credentials.
    "run_start_announce_channels",
}


def _str_field_names():
    return [n for n, f in Settings.model_fields.items() if f.annotation is str]


def _sweep_settings(value_for):
    return Settings(_env_file=None, **{n: value_for(n) for n in _str_field_names()})


def test_every_string_field_is_classified_secret_or_not():
    """Fills every str field with a unique sentinel and asserts that exactly the
    documented non-secret fields survive in the repr. Fails both ways: a new
    credential field whose name misses the hints shows up as an unexpected leak, and
    a repr that masked everything shows up as a missing control."""
    s = _sweep_settings(lambda n: f"sentinelvalue-{n}")
    rendered = repr(s) + str(s)
    visible = {n for n in _str_field_names() if f"sentinelvalue-{n}" in rendered}
    assert visible == NON_SECRET_STR_FIELDS


def test_no_string_field_leaks_a_password_embedded_in_a_url():
    """Second sweep: every str field gets a DSN carrying a password in its userinfo.
    Catches such a field present or future, regardless of its name.

    Scope, stated precisely because the obvious reading is wider than the truth: this
    sweeps the two places `_redact_url_credentials` looks — the userinfo and the query
    string. A URL whose credential is a PATH SEGMENT is not covered; see
    test_a_credential_in_a_url_path_is_a_known_gap below."""
    s = _sweep_settings(lambda n: f"postgresql://user:pw-{n}@host:5432/db")
    rendered = repr(s) + str(s)
    leaked = [n for n in _str_field_names() if f"pw-{n}" in rendered]
    assert leaked == []
    # Control: the sweep really did populate the object.
    assert s.database_url == "postgresql://user:pw-database_url@host:5432/db"


def test_a_credential_in_a_url_path_is_a_known_gap():
    """Pins the one credential shape the positional path does NOT mask, so it is a
    recorded limitation rather than a surprise.

    A Slack/Discord incoming-webhook URL, an S3 presigned URL and a Twilio-style
    callback all carry their secret in the PATH, not the userinfo or the query string.
    `_redact_url_credentials` masks neither, deliberately: `base_url` and
    `orcid_redirect_uri` have paths that an operator needs to read, and there is no
    way to tell a secret path segment from a route without knowing the field.

    No Settings field has this shape today — `test_every_string_field_is_classified_
    secret_or_not` is what keeps that true, because a new field renders in the clear
    only if someone adds it to NON_SECRET_STR_FIELDS in the same diff. If one is ever
    added whose name misses `_SECRET_NAME_HINTS` (e.g. `slack_incoming_webhook`), the
    right fix is the name hint, not a path heuristic."""
    webhook = "https://hooks.slack.com/services/T00000/B00000/xxxxSECRETxxxx"
    assert _redact_url_credentials(webhook) == webhook

    # And why that is not a live leak: every str field is either masked whole by name
    # or explicitly classified non-secret. There is no third, unreviewed category for a
    # path-credential field to hide in.
    unclassified = [
        n for n in _str_field_names()
        if n not in NON_SECRET_STR_FIELDS
        and not any(h in n.lower() for h in _SECRET_NAME_HINTS)
    ]
    assert unclassified == [], (
        "str field(s) that are neither name-masked nor listed as non-secret — a "
        f"path-credential field could hide here: {unclassified}"
    )


def test_sweep_covers_the_bot_tokens_and_the_dsn():
    """Control for the sweep helpers themselves — a _str_field_names() that returned
    [] would make both sweeps vacuous."""
    names = _str_field_names()
    assert "database_url" in names
    assert "secret_key" in names
    assert len([n for n in names if n.startswith("slack_bot_token_")]) > 100


@pytest.mark.parametrize("render", [repr, str], ids=["repr", "str"])
def test_both_repr_and_str_route_through_repr_args(render):
    """pydantic v2 implements BaseModel.__str__ via __repr_str__ -> __repr_args__, so
    one override covers both. Asserted rather than assumed."""
    s = Settings(_env_file=None, secret_key="supersecretvalue", database_url=LEAKY_DSN)
    out = render(s)
    assert "supersecretvalue" not in out
    assert "sup3rs3cr3t" not in out
    assert "***REDACTED***" in out
    assert "http" in out or "postgres" in out  # control: something is still rendered
