"""GrantBot must never publish funding posts under SuBot's Slack identity.

Split out of ``test_grantbot_live.py``'s module-level ``pytestmark`` on purpose:
every test in that file is ``live_api`` because most of them exercise the real
grants.gov catalogue, and ``tests/conftest.py`` skips the whole module unless
``LIVE_API_TESTS=1`` is set. This test touches no third-party API at all — the
catalogue, the LLM selection/draft stages and grants.gov's detail endpoint are
all stubbed, exactly like ``test_grantbot_live.py``'s own Slack-transport
tests — so gating it behind ``LIVE_API_TESTS=1`` would mean ``./scripts/ci.sh``'s
full pytest run (issue #23 COR-26c's only regression coverage) never actually
executes it.
"""

import logging
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from src.agent import grantbot
from src.models import GrantbotPostedFoa
from tests.factories import make_agent
from tests.integration.test_grantbot_live import (
    _RecordingWebClient,
    _StageRecorder,
    synthetic_opportunity,
)

pytestmark = pytest.mark.integration


class _CountingWebClient:
    """Records every construction; never talks to a real Slack transport.

    Replaces ``_ExplodingWebClient`` for this file's negative-path test (opus review of
    23.3-23.5, finding 1). The exploding double discriminates on a side effect — its
    ``AssertionError`` message getting caught by grantbot's own ``except``/log/continue
    handling and landing in ``caplog`` — and that indirection is exactly what let a stale
    FOA number's literal "TOKEN" substring make an earlier version of this test pass
    against unfixed code for the wrong reason (see commit dd82c91). Counting construction
    and asserting the count is zero is a direct behavioural check: pre-fix,
    ``_ensure_channel_membership`` reaches ``src.services.slack_web._client(token)`` with
    the borrowed token, so this double gets built at least once — RED on behaviour, not on
    log text.

    Construction does not raise, so if a regression does reach Slack the rest of the run
    completes (posting "succeeds" against a benign double) rather than crashing partway
    through on an unrelated ``AttributeError`` from a method this double doesn't
    implement.
    """

    instances: list["_CountingWebClient"] = []

    def __init__(self, *args, **kwargs):
        _CountingWebClient.instances.append(self)

    def conversations_list(self, **kwargs):
        return {"channels": [], "response_metadata": {"next_cursor": ""}}

    def conversations_join(self, channel):
        return {"ok": True}

    def chat_postMessage(self, channel, text):
        return {"ok": True, "ts": "1700000000.000001"}


class _NoGrantbotTokenSettings:
    """Real settings, but with an empty/placeholder grantbot token and a genuine (fake) SuBot
    token — the exact shape that used to trigger the SuBot fallback (issue #23 COR-26c)."""

    def __init__(self, real, su_token: str = "xoxb-fake-su-token"):
        self._real, self._su_token = real, su_token

    def __getattr__(self, name):
        if name == "slack_bot_token_grantbot":
            return ""
        if name == "slack_bot_token_su":
            return self._su_token
        return getattr(self._real, name)


async def _claimed_numbers(db_session) -> set[str]:
    rows = await db_session.execute(select(GrantbotPostedFoa.foa_number))
    return set(rows.scalars().all())


def _stub_detail_fetch(monkeypatch) -> None:
    async def _no_detail(opportunity_id: str):
        return None

    monkeypatch.setattr(grantbot, "fetch_opportunity_detail", _no_detail)


async def test_no_grantbot_token_refuses_to_post_as_subot(
    db_session, monkeypatch, tmp_path, caplog,
):
    """COR-26c: GrantBot must never publish funding posts under SuBot's Slack identity. The old
    code fell back to slack_bot_token_su whenever slack_bot_token_grantbot was missing/placeholder,
    and the engine's _bot_uid_map resolves roster bots first, so those posts were attributed to
    `su`, not `grantbot`. The fix must refuse to post, release the FOA's claim so a future run
    with a real token can still post it, and log the refusal — all without ever constructing a
    Slack transport.
    """
    # cache_foa (grantbot.py step 4b) writes into the repo's data/ directory before the
    # token check is ever reached — redirect it, same as test_grantbot_live.py's
    # module-wide _isolate_foa_cache autouse fixture.
    monkeypatch.setattr("src.agent.foa_cache.CACHE_DIR", tmp_path / "foa_cache")

    async def _enabled(db):
        return True

    monkeypatch.setattr("src.services.slack_tokens.slack_globally_enabled", _enabled)

    _CountingWebClient.instances = []
    monkeypatch.setattr("slack_sdk.WebClient", _CountingWebClient)
    monkeypatch.setattr("src.services.slack_web._client", _CountingWebClient)

    # Seed the negative precondition explicitly (finding 1d): an agents row for
    # 'grantbot' exists — as it would once someone has run the onboarding flow — but
    # carries no token, rather than relying on the row being absent altogether.
    await make_agent(db_session, agent_id="grantbot", slack_bot_token=None)

    real_settings = grantbot.get_settings()
    monkeypatch.setattr(
        grantbot, "get_settings", lambda: _NoGrantbotTokenSettings(real_settings)
    )

    _stub_detail_fetch(monkeypatch)
    now = datetime.now(UTC)
    # No "grantbot" or "token" substring in the FOA number (finding 1c) — commit dd82c91's
    # own predecessor test passed against unfixed code because "TEST-NO-GRANTBOT-TOKEN"
    # leaked the word "TOKEN" into the generic "Failed to post <number> to #<channel>: ..."
    # log line, satisfying the log-message assertion for the wrong reason.
    opportunity = synthetic_opportunity("TEST-NO-CRED-42", now)
    _StageRecorder(channel="funding-opportunities").install(monkeypatch)

    async def _listed(agencies=None):
        return [opportunity]

    monkeypatch.setattr(grantbot, "list_posted_opportunities", _listed)

    with caplog.at_level(logging.ERROR, logger="src.agent.grantbot"):
        posted = await grantbot._run_grantbot_with_session(
            db_session, channel="funding-opportunities", dry_run=False,
            max_posts=5, max_per_channel=5,
        )

    assert posted == [], f"posted under a borrowed token: {posted}"
    assert _CountingWebClient.instances == [], (
        "a Slack client was constructed even though grantbot has no dedicated token — "
        "pre-fix, _ensure_channel_membership reaches slack_web._client with the "
        "borrowed SuBot token"
    )
    assert await _claimed_numbers(db_session) == set(), (
        "the FOA's claim was not released — a future run with a real grantbot token could "
        "never post it"
    )
    messages = [r.getMessage() for r in caplog.records]
    assert not any("Failed to post" in m for m in messages), (
        "no post should ever have been attempted, so there is nothing to fail",
        messages,
    )
    assert any(
        "grantbot" in m.lower() and "token" in m.lower() for m in messages
    ), messages


async def test_db_token_is_used_ahead_of_the_env_field(
    db_session, monkeypatch, tmp_path, caplog,
):
    """D10: AgentRegistry (what /admin/agents writes) is the first source, .env the
    fallback. This test is GREEN on the current (post-dd82c91) code — it pins that
    precedence rather than driving a fix. A mutant that drops the DB lookup and reads
    only settings.slack_bot_token_grantbot (which this test leaves empty) would fail it:
    reason, do not commit.
    """
    monkeypatch.setattr("src.agent.foa_cache.CACHE_DIR", tmp_path / "foa_cache")

    async def _enabled(db):
        return True

    monkeypatch.setattr("src.services.slack_tokens.slack_globally_enabled", _enabled)

    _RecordingWebClient.instances = []
    _RecordingWebClient.next_fail_post = False
    monkeypatch.setattr("slack_sdk.WebClient", _RecordingWebClient)
    monkeypatch.setattr("src.services.slack_web._client", _RecordingWebClient)

    # The DB row is the only place a valid grantbot token exists — the env field below
    # is left empty, so any post can only have happened via get_agent_bot_token's DB read.
    await make_agent(
        db_session, agent_id="grantbot", slack_bot_token="xoxb-fake-grantbot-token",
    )

    real_settings = grantbot.get_settings()
    monkeypatch.setattr(
        grantbot, "get_settings", lambda: _NoGrantbotTokenSettings(real_settings)
    )

    _stub_detail_fetch(monkeypatch)
    now = datetime.now(UTC)
    opportunity = synthetic_opportunity("TEST-DB-TOKEN-77", now)
    _StageRecorder(channel="funding-opportunities").install(monkeypatch)

    async def _listed(agencies=None):
        return [opportunity]

    monkeypatch.setattr(grantbot, "list_posted_opportunities", _listed)

    with caplog.at_level(logging.ERROR, logger="src.agent.grantbot"):
        posted = await grantbot._run_grantbot_with_session(
            db_session, channel="funding-opportunities", dry_run=False,
            max_posts=5, max_per_channel=5,
        )

    assert posted != [], "the DB-provisioned token should have let GrantBot post"
    assert _RecordingWebClient.instances, "no Slack client was ever constructed"
    assert all(
        double.token == "xoxb-fake-grantbot-token" for double in _RecordingWebClient.instances
    ), [double.token for double in _RecordingWebClient.instances]
    assert await _claimed_numbers(db_session) == {"TEST-DB-TOKEN-77"}
