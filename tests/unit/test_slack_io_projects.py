"""Projects on the Slack path (spec §5.6–§5.8): the poller never mirrors a project posting
or reply it did not log, `_post_message` strips the `<project_json>` block, and the
authorship claim check skips the posting and keys a lab's project reply on its own."""
import types
from unittest.mock import AsyncMock

import pytest

from src.agent import claim_check
from src.agent.message_log import PHASE_HUB_PROJECT, PHASE_PROJECT_REPLY
from src.agent.publication_records import PublicationIds
from src.config import get_settings
from src.services import project_plan
from tests.characterization.freeze.fixtures import (
    apply_frozen_settings,
    build_engine,
    install_profiles,
)

pytestmark = pytest.mark.asyncio

CH_NAME = "general"
CH_ID = "C_general"
CLAIM = "Our 2023 Nature paper showed this."


@pytest.fixture
def world(tmp_path, monkeypatch):
    """``world(mode)`` -> engine, agents, clients, the collected claim-check rows."""
    def make(mode: str = "log"):
        apply_frozen_settings(monkeypatch)
        monkeypatch.setenv("AUTHORSHIP_CLAIM_CHECK_MODE", mode)
        get_settings.cache_clear()
        root = install_profiles(tmp_path, monkeypatch)
        eng, agents, clients = build_engine()
        rows: list[dict] = []

        async def _collect(session_factory, **kwargs):
            rows.append(kwargs)

        monkeypatch.setattr(claim_check, "record_check", _collect)
        return types.SimpleNamespace(eng=eng, agents=agents, clients=clients, rows=rows,
                                     root=root)
    yield make
    get_settings.cache_clear()


async def test_poller_skips_project_posts_it_did_not_log(world):
    w = world()
    ordinary, posting, reply = "1700000100.000000", "1700000101.000000", "1700000102.000000"
    messages = [
        {"ts": ordinary, "bot_id": "B_wang", "user": "U_wang", "text": "An ordinary pitch."},
        {"ts": posting, "bot_id": "B_blackbird", "user": "U_blackbird",
         "text": project_plan.POSTING_HEADER.format(surname="Wang") + "\nA plan."},
        {"ts": reply, "bot_id": "B_wang", "user": "U_wang",
         "text": project_plan.REPLY_HEADER.format(title="Organoids") + "\nWe can do this."},
    ]
    for client in w.clients.values():
        client.channel_history[CH_ID] = list(messages)
    w.eng._last_channel_poll = 0.0
    await w.eng._poll_slack_for_bot_messages()
    assert w.eng.message_log.get_entry(ordinary) is not None
    assert w.eng.message_log.get_entry(posting) is None
    assert w.eng.message_log.get_entry(reply) is None
    assert w.eng._poll_cursors[CH_ID] == reply


async def test_post_message_strips_the_project_block(world):
    w = world()
    text = 'We agree on the plan.\n<project_json>{"plan": {"model": "organoid"}}</project_json>'
    assert await w.eng._post_message("blackbird", CH_NAME, text)
    posted = w.clients["blackbird"].posted[-1]["text"]
    assert "project_json" not in posted
    assert posted == "We agree on the plan."


async def test_authorship_gate_records_nothing_for_the_posting(world, monkeypatch):
    w = world("enforce")
    record = AsyncMock()
    monkeypatch.setattr(claim_check, "record_check", record)
    blocked = await w.eng.slack_io._authorship_gate(
        w.agents["hub"], CH_NAME, CLAIM, None, PHASE_HUB_PROJECT, None,
    )
    assert blocked is False
    record.assert_not_called()


async def test_a_blocked_project_reply_stores_feedback_under_its_own_key(world):
    w = world("enforce")
    lab = w.agents["lab"]
    (w.root / "public" / f"{lab.agent_id}.md").write_text(
        f"# {lab.pi_name} Lab\n\nNo publications listed.\n", encoding="utf-8")
    lab.reload_profiles()
    lab.set_publication_ids(PublicationIds())
    reasons: list[str] = []
    result = await w.eng._post_message(
        "wang", CH_NAME, CLAIM, phase=PHASE_PROJECT_REPLY, on_claim_block=reasons.append,
    )
    assert result is None
    assert w.clients["wang"].posted == []
    assert reasons
    assert w.eng.ctx.claim_feedback.get("wang", claim_check.PROJECT_REPLY_THREAD_KEY) == reasons[0]
    assert w.eng.ctx.claim_feedback.get("wang", claim_check.PHASE5_THREAD_KEY) is None
    assert w.rows[0]["thread_key"] == "project_reply"
