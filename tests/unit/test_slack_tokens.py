"""Slack token validity, source precedence, and the `slack_enabled` tri-state.

These three decide whether the whole Slack integration is on, and which credential each
agent uses. Nothing tested them directly before: `test_roster_sync.py` exercises
`token_for_agent_row` incidentally, and the `slack_enabled` resolution in
`src/agent/main.py` had no coverage at all.

Why the token-shape table matters more than it looks: `slack_globally_enabled()`
auto-detects Slack as ON from the mere *presence* of a "valid" token. So whatever
`is_valid_token` accepts is what can silently switch the integration on — and then fail
every API call.
"""

import pytest

from src.config import Settings, get_settings
from src.models import AgentRegistry
from src.services.slack_tokens import (
    env_token,
    get_agent_bot_token,
    get_any_bot_token,
    is_valid_token,
    slack_globally_enabled,
    token_for_agent_row,
)
from tests import factories

# Slack bot tokens are always `xoxb-`. Every consumer of is_valid_token passes a bot
# token, so anything else reaching it is a misconfiguration that must not turn Slack on.
TOKEN_CASES = [
    ("xoxb-1111-2222-abcdefghijklmnop", True),
    ("xoxb-real-looking-token-value", True),
    ("", False),
    (None, False),
    ("   ", False),
    ("\n", False),
    ("xoxb-placeholder", False),
    ("xoxb-placeholder-su", False),
    # A USER token in the bot-token field. Accepted by the pre-hardening
    # implementation, which meant one paste could flip slack_enabled on and then fail
    # every call with not_allowed_token_type.
    ("xoxp-EXAMPLE-NOT-A-REAL-TOKEN", False),
    # A CONFIG token in the bot-token field — same hazard. This is the exact token type
    # used for provisioning, so the two live side by side in the same .env.
    ("xoxe.xoxp-1-EXAMPLE-NOT-A-REAL-TOKEN", False),
    ("xoxe-1-EXAMPLE-NOT-A-REAL-TOKEN", False),
    # Unfilled template values.
    ("REPLACE_ME", False),
    ("xoxb-your-token-here", True),  # indistinguishable from a real token; documented
]


@pytest.mark.parametrize("tok,valid", TOKEN_CASES,
                         ids=[repr(c[0])[:26] for c in TOKEN_CASES])
def test_is_valid_token(tok, valid):
    assert is_valid_token(tok) is valid


def test_token_cases_have_both_polarities():
    """Control for the table: an is_valid_token that returned a constant would satisfy
    an all-True or all-False table without anyone noticing."""
    assert {c[1] for c in TOKEN_CASES} == {True, False}


def test_the_only_accepted_shape_is_a_bot_token():
    """States the rule the table encodes, so a future edit that loosens the check has
    to delete an assertion that says why rather than just flip a row."""
    for prefix in ("xoxp-", "xoxe.xoxp-", "xoxe-", "xapp-", "xoxa-", "Bearer "):
        assert is_valid_token(prefix + "something") is False, prefix
    assert is_valid_token("xoxb-something") is True


# --- source precedence: the DB column is authoritative, .env is a fallback ---------


def _clear_settings_cache():
    get_settings.cache_clear()


def test_token_for_agent_row_prefers_the_db_column(monkeypatch):
    """CLAUDE.md: the AgentRegistry column is the source of truth, .env is a read
    fallback. Both halves, so a resolver that only ever read one source fails."""
    monkeypatch.setenv("SLACK_BOT_TOKEN_SU", "xoxb-from-the-env-file")
    _clear_settings_cache()
    try:
        row = AgentRegistry(agent_id="su", bot_name="SuBot", pi_name="PI Su",
                            slack_bot_token="xoxb-from-the-database")
        assert token_for_agent_row(row) == "xoxb-from-the-database"
        # Control: with the column empty the env value IS used, so the assertion above
        # is about precedence rather than about the fallback being dead.
        row.slack_bot_token = None
        assert token_for_agent_row(row) == "xoxb-from-the-env-file"
        # And an invalid column value falls back rather than winning.
        row.slack_bot_token = "xoxb-placeholder"
        assert token_for_agent_row(row) == "xoxb-from-the-env-file"
    finally:
        _clear_settings_cache()


def test_env_token_rejects_an_invalid_env_value(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN_SU", "xoxb-placeholder")
    _clear_settings_cache()
    try:
        assert env_token("su") is None
        # Control: a real-looking value comes back.
        monkeypatch.setenv("SLACK_BOT_TOKEN_SU", "xoxb-good")
        _clear_settings_cache()
        assert env_token("su") == "xoxb-good"
    finally:
        _clear_settings_cache()


def test_env_token_for_an_unknown_agent_is_none():
    assert env_token("nobody-by-that-name") is None


@pytest.mark.integration
async def test_get_agent_bot_token_reads_the_db_then_env(db_session, monkeypatch):
    user = await factories.make_user(db_session, email="su-tok@example.org")
    agent = await factories.make_agent(
        db_session, user=user, agent_id="su", bot_name="SuBot", pi_name="PI Su",
        status="active", slack_bot_token="xoxb-db-value",
    )
    await db_session.flush()
    monkeypatch.setenv("SLACK_BOT_TOKEN_SU", "xoxb-env-value")
    _clear_settings_cache()
    try:
        assert await get_agent_bot_token(db_session, "su") == "xoxb-db-value"
        agent.slack_bot_token = None
        await db_session.flush()
        assert await get_agent_bot_token(db_session, "su") == "xoxb-env-value"
    finally:
        _clear_settings_cache()


@pytest.mark.integration
async def test_get_any_bot_token_ignores_invalid_rows(db_session, monkeypatch):
    """A placeholder row must not satisfy 'any usable token' — that is what
    auto-detect keys on, so a placeholder would switch Slack on for the deployment."""
    monkeypatch.delenv("SLACK_BOT_TOKEN_SU", raising=False)
    _clear_settings_cache()
    try:
        u1 = await factories.make_user(db_session, email="a-tok@example.org")
        await factories.make_agent(db_session, user=u1, agent_id="a1", bot_name="A1Bot",
                                   status="active", slack_bot_token="xoxb-placeholder")
        await db_session.flush()
        assert await get_any_bot_token(db_session) is None
        # Control: one real token and it is found.
        u2 = await factories.make_user(db_session, email="b-tok@example.org")
        await factories.make_agent(db_session, user=u2, agent_id="a2", bot_name="A2Bot",
                                   status="active", slack_bot_token="xoxb-real")
        await db_session.flush()
        assert await get_any_bot_token(db_session) == "xoxb-real"
    finally:
        _clear_settings_cache()


# --- the slack_enabled tri-state (mirrors src/agent/main.py:110-114) ---------------


ENABLED_CASES = [
    # (name, settings value, a usable token exists, expected)
    ("forced off, token present", False, True, False),
    ("forced off, no token", False, False, False),
    ("forced on, no token", True, False, True),
    ("forced on, token present", True, True, True),
    ("auto, no token", None, False, False),
    ("auto, token present", None, True, True),
]


@pytest.mark.integration
@pytest.mark.parametrize("name,setting,has_token,expected", ENABLED_CASES,
                         ids=[c[0] for c in ENABLED_CASES])
async def test_slack_globally_enabled_tri_state(
    db_session, monkeypatch, name, setting, has_token, expected
):
    monkeypatch.delenv("SLACK_BOT_TOKEN_SU", raising=False)
    if setting is None:
        monkeypatch.delenv("SLACK_ENABLED", raising=False)
    else:
        monkeypatch.setenv("SLACK_ENABLED", "true" if setting else "false")
    _clear_settings_cache()
    try:
        if has_token:
            u = await factories.make_user(db_session, email=f"{name[:8]}@example.org")
            await factories.make_agent(
                db_session, user=u, agent_id="su", bot_name="SuBot",
                status="active", slack_bot_token="xoxb-real",
            )
            await db_session.flush()
        assert await slack_globally_enabled(db_session) is expected, name
    finally:
        _clear_settings_cache()


def test_enabled_cases_cover_all_three_branches():
    """Control: the table must exercise forced-on, forced-off AND auto-detect, and
    auto-detect must appear with both outcomes. Otherwise a resolver that ignored the
    setting, or one that ignored the tokens, would pass."""
    assert {c[1] for c in ENABLED_CASES} == {True, False, None}
    auto = {c[3] for c in ENABLED_CASES if c[1] is None}
    assert auto == {True, False}, "auto-detect is only tested in one direction"


# --- secret redaction over the fields that exist today ----------------------------


def test_every_slack_secret_is_redacted_in_the_settings_repr():
    """`test_config_secret_redaction.py` predates the config-token pair, so the two
    provisioning credentials were never checked. A settings object reaches logs and
    error pages; a bot or config token in there is a workspace takeover.

    Control included: a non-secret field must still be visible, so a repr that returned
    nothing at all would not pass.
    """
    s = Settings(
        slack_bot_token_su="xoxb-secret-aaaaaaa",
        slack_config_token="xoxe.xoxp-secret-bbbbbbb",
        slack_config_refresh_token="xoxe-1-secret-ccccccc",
    )
    text = repr(s) + str(s)
    for secret in ("xoxb-secret-aaaaaaa", "xoxe.xoxp-secret-bbbbbbb",
                   "xoxe-1-secret-ccccccc"):
        # A token is a whole-value credential: nothing of it survives, unlike the
        # positional masking applied to a DSN's password.
        assert secret not in text, f"{secret[:14]}... leaked into the settings repr"
    assert s.aws_region in text, "control leg failed: the repr shows nothing at all"


def test_model_dump_is_not_used_on_settings_anywhere_in_src():
    """The redaction covers repr()/str() ONLY, by explicit design.

    `Settings.__repr_args__` masks credential-named fields, and its docstring records
    that as closing "the only described leak path" for SEC-19 — deliberately leaving
    fields as plain `str` rather than SecretStr to avoid churning ~130 call sites.
    `model_dump()` therefore returns every secret in the clear. Measured: it does.

    That scoping is only safe while nothing dumps the settings object, so the invariant
    that actually protects SEC-19 is this one, not a redaction test. If a future caller
    needs `model_dump()`, the redaction has to be widened first.

    Adding the DSN redaction (`database_url`'s password) did not change this: it is a
    `__repr_args__` rule, so `model_dump()` still returns that password in the clear
    too. The measurement below pins the rationale instead of just asserting it.
    """
    import pathlib
    import re

    dump = Settings(_env_file=None, secret_key="dump-secret-value",
                    database_url="postgresql://u:dump-dsn-password@h/db").model_dump()
    assert dump["secret_key"] == "dump-secret-value"
    assert "dump-dsn-password" in dump["database_url"]

    src = pathlib.Path(__file__).resolve().parents[2] / "src"
    offenders = []
    for f in src.rglob("*.py"):
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if re.search(r"(settings|get_settings\(\))\s*\.model_dump", line):
                offenders.append(f"{f.relative_to(src.parent)}:{i}: {line.strip()}")
    assert not offenders, (
        "Settings.model_dump() returns unredacted secrets — see "
        "Settings.__repr_args__. Widen the redaction before adding these:\n"
        + "\n".join(offenders)
    )
    # Control: the scan is actually looking at files. A glob that matched nothing would
    # make the assertion above vacuous.
    assert len(list(src.rglob("*.py"))) > 20
