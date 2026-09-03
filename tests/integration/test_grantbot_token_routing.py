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
from tests.integration.test_grantbot_live import (
    _ExplodingWebClient,
    _StageRecorder,
    synthetic_opportunity,
)

pytestmark = pytest.mark.integration


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
    with a real token can still post it, and log the refusal.
    """
    # cache_foa (grantbot.py step 4b) writes into the repo's data/ directory before the
    # token check is ever reached — redirect it, same as test_grantbot_live.py's
    # module-wide _isolate_foa_cache autouse fixture.
    monkeypatch.setattr("src.agent.foa_cache.CACHE_DIR", tmp_path / "foa_cache")

    async def _enabled(db):
        return True

    monkeypatch.setattr("src.services.slack_tokens.slack_globally_enabled", _enabled)
    monkeypatch.setattr("slack_sdk.WebClient", _ExplodingWebClient)
    monkeypatch.setattr("src.services.slack_web._client", _ExplodingWebClient)
    real_settings = grantbot.get_settings()
    monkeypatch.setattr(
        grantbot, "get_settings", lambda: _NoGrantbotTokenSettings(real_settings)
    )

    _stub_detail_fetch(monkeypatch)
    now = datetime.now(UTC)
    opportunity = synthetic_opportunity("TEST-NO-GRANTBOT-CRED", now)
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
    assert await _claimed_numbers(db_session) == set(), (
        "the FOA's claim was not released — a future run with a real grantbot token could "
        "never post it"
    )
    assert any(
        "grantbot" in r.getMessage().lower() and "token" in r.getMessage().lower()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]
