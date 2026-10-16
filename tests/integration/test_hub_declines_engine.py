"""A hub ⏸️ decline with no assessment is recorded in ``hub_declines`` and announced.

Owner decision 2026-10-16: the hub's Outcome-2 close (phase4-thread-reply.md, no
``<assessment_json>`` sidecar) used to leave no row anywhere. The engine now writes one
``hub_declines`` row per interview and posts a one-line ``#assessments-summary`` headline
for it under the decline claim protocol (``src/services/decline_claims.py``): in-turn,
at the shutdown sweep, and at Finalize run.
"""

import logging
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.agent import Agent
from src.agent.channels import ASSESSMENTS_SUMMARY_CHANNEL
from src.agent.engine import constants
from src.agent.engine.headlines import HeadlineAnnouncer, decline_report_key
from src.agent.simulation import SimulationEngine
from src.agent.slack_client import markdown_to_mrkdwn
from src.agent.state import ThreadState
from src.models import HubDecline, SimulationRun
from src.services.assessment_headline import decline_reason, render_decline_headline
from tests.fakes import FakeSlackClient
from tests.integration.test_hub_assessment_capture_gate import (  # noqa: F401
    _delete_run,
    _drive_reply,
    _drops,
    _hub,
    _new_run,
    _reply_with_sidecar,
)

pytestmark = pytest.mark.integration

_DECIDE_COUNT = 7  # ordinal 8: an ordinary DECIDE turn
_DECLINE_BODY = "⏸️ Not a fit for the hub right now — the translational path is unclear."
_DECLINE_REPLY = f"<slack_message>{_DECLINE_BODY}</slack_message>"


def _wire_summary_channel(sim):
    """Without this every headline is skipped (``channel_id=None``) and a test about
    delivery proves nothing."""
    sim._assessments_summary_channel_id = "C-SUMMARY"
    sim._channel_id_map[ASSESSMENTS_SUMMARY_CHANNEL] = "C-SUMMARY"
    sim._channel_id_map["single-cell-omics"] = "C_OMICS"


def _headlines(client):
    return [p for p in client.posted if p.get("channel") == ASSESSMENTS_SUMMARY_CHANNEL]


def _thread_replies(client):
    return [p for p in client.posted if p.get("channel") != ASSESSMENTS_SUMMARY_CHANNEL]


async def _declines(factory, run_id):
    async with factory() as db:
        return (await db.execute(
            select(HubDecline)
            .where(HubDecline.simulation_run_id == run_id)
            .order_by(HubDecline.created_at)
        )).scalars().all()


async def _seed_decline(factory, run_id, *, thread_id="t9", slack_ts="1700000099.000000"):
    async with factory() as db:
        row = HubDecline(
            simulation_run_id=run_id, agent_id="blackbird", subject_agent_id="gordy",
            channel_name="single-cell-omics", thread_id=thread_id, slack_ts=slack_ts,
            reason_text=_DECLINE_BODY,
        )
        db.add(row)
        await db.commit()
        return row.id


def _summary_post_patch(monkeypatch, client, behaviour):
    """Make ``client``'s #assessments-summary post ``behaviour`` ("refuse" answers None,
    "raise" raises with no response) while thread replies post normally."""
    original = client.apost_message

    async def _apost(channel, *args, **kwargs):
        if channel == ASSESSMENTS_SUMMARY_CHANNEL:
            if behaviour == "raise":
                raise ConnectionError("connection reset")
            return None
        return await original(channel, *args, **kwargs)

    monkeypatch.setattr(client, "apost_message", _apost)


@pytest.mark.asyncio
async def test_a_hub_decline_is_recorded_and_announced(engine, monkeypatch):
    sim, agent, thread, client, factory, run_id = await _drive_reply(
        engine, monkeypatch, _DECLINE_REPLY, prior_messages=_DECIDE_COUNT,
        configure=_wire_summary_channel,
    )
    try:
        assert thread.status == "closed"
        [reply] = _thread_replies(client)
        [row] = await _declines(factory, run_id)
        assert row.agent_id == "blackbird"
        assert row.subject_agent_id == "gordy"
        assert row.channel_name == "single-cell-omics"
        assert row.thread_id == "t1"
        assert row.slack_ts == reply["ts"], "the closing reply's own Slack ts"
        assert row.reason_text == _DECLINE_BODY, "the reply as posted, tags stripped"
        assert row.summary_claimed_at is not None and row.summary_posted_at is not None

        [headline] = _headlines(client)
        expected = render_decline_headline(
            pi_label="gordy",  # not on this roster: the assessment path's fallback label
            reason=decline_reason(row.reason_text),
            permalink=client.get_permalink("C_OMICS", row.slack_ts),
        )
        assert headline["text"] == markdown_to_mrkdwn(expected)
        assert await _drops(factory, run_id) == [], "a hub decline is not a lost verdict"
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_second_close_on_the_same_thread_writes_nothing_more(engine, monkeypatch):
    sim, agent, thread, client, factory, run_id = await _drive_reply(
        engine, monkeypatch, _DECLINE_REPLY, prior_messages=_DECIDE_COUNT,
        configure=_wire_summary_channel,
    )
    try:
        await sim._record_hub_decline(agent, thread, "⏸️ Closing again.", None)

        [row] = await _declines(factory, run_id)
        assert row.reason_text == _DECLINE_BODY
        assert len(_headlines(client)) == 1, "the conflict posts no second headline"
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_close_on_an_assessed_thread_records_no_decline(engine, monkeypatch):
    sim, agent, thread, client, factory, run_id = await _drive_reply(
        engine, monkeypatch, _reply_with_sidecar(closing=True), prior_messages=_DECIDE_COUNT,
        configure=_wire_summary_channel,
    )
    try:
        assert thread.status == "closed"
        assert await _declines(factory, run_id) == []
        assert len(_headlines(client)) == 1, "only the assessment's own headline"
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_lab_close_records_no_decline_and_still_drops(engine, monkeypatch):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    lab = Agent("gordy", "GordyBot", "Gordy")
    client = FakeSlackClient(agent_id="gordy")
    sim = SimulationEngine(
        agents=[hub, lab],
        slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird"), "gordy": client},
        session_factory=factory, simulation_run_id=run_id,
    )
    _wire_summary_channel(sim)

    async def _fast_memory_update(*args, **kwargs):
        return None

    monkeypatch.setattr(sim, "_update_agent_memory", _fast_memory_update)
    thread = ThreadState(thread_id="t1", channel="single-cell-omics", other_agent_id="blackbird")
    lab.state.active_threads["t1"] = thread
    try:
        await sim._check_thread_outcome(lab, thread, "⏸️ We will pass on this one.")

        assert await _declines(factory, run_id) == []
        assert [d.reason for d in await _drops(factory, run_id)] == ["closed_before_verdict"]
        assert _headlines(sim.slack_clients["blackbird"]) == []
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_refused_headline_releases_the_claim(engine, monkeypatch):
    def configure(sim):
        _wire_summary_channel(sim)
        _summary_post_patch(monkeypatch, sim.slack_clients["blackbird"], "refuse")

    sim, agent, thread, client, factory, run_id = await _drive_reply(
        engine, monkeypatch, _DECLINE_REPLY, prior_messages=_DECIDE_COUNT, configure=configure,
    )
    try:
        [row] = await _declines(factory, run_id)
        assert row.summary_claimed_at is None, "a definite failure releases the claim"
        assert row.summary_posted_at is None
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_transport_error_keeps_the_claim(engine, monkeypatch):
    def configure(sim):
        _wire_summary_channel(sim)
        _summary_post_patch(monkeypatch, sim.slack_clients["blackbird"], "raise")

    sim, agent, thread, client, factory, run_id = await _drive_reply(
        engine, monkeypatch, _DECLINE_REPLY, prior_messages=_DECIDE_COUNT, configure=configure,
    )
    try:
        [row] = await _declines(factory, run_id)
        assert row.summary_claimed_at is not None, "in doubt: the claim stays"
        assert row.summary_posted_at is None
        assert "t1" in sim.headlines._in_doubt_headlines
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("end_reason", [None, "operator_hold", "time_limit"])
async def test_the_shutdown_sweep_posts_an_owed_decline(engine, end_reason):
    """Every end-reason class, HOLD included: a decline is an ended interview."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    decline_id = await _seed_decline(factory, run_id)
    sim, _agent = _hub(factory, run_id)
    _wire_summary_channel(sim)
    client = sim.slack_clients["blackbird"]
    try:
        await sim.headlines.shutdown_sweep(end_reason)

        [headline] = _headlines(client)
        expected = render_decline_headline(
            pi_label="gordy", reason=decline_reason(_DECLINE_BODY),
            permalink=client.get_permalink("C_OMICS", "1700000099.000000"),
        )
        assert headline["text"] == markdown_to_mrkdwn(expected)
        [row] = await _declines(factory, run_id)
        assert row.id == decline_id and row.summary_posted_at is not None
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_the_shutdown_sweep_shares_the_headline_bound(engine, monkeypatch, caplog):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    await _seed_decline(factory, run_id)
    sim, _agent = _hub(factory, run_id)
    _wire_summary_channel(sim)
    monkeypatch.setattr(constants, "HEADLINES_MAX_AT_SHUTDOWN", 0)
    try:
        with caplog.at_level(logging.ERROR, logger="src.agent.simulation"):
            await sim.headlines.shutdown_sweep(None)

        assert _headlines(sim.slack_clients["blackbird"]) == []
        assert "LOST 1 #assessments-summary decline headline(s) at shutdown" in caplog.text
        [row] = await _declines(factory, run_id)
        assert row.summary_claimed_at is None, "never attempted: still owed"
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_finalize_posts_owed_declines_and_reports_them(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    await _seed_decline(factory, run_id)
    client = FakeSlackClient(agent_id="blackbird")
    announcer = HeadlineAnnouncer(
        session_factory=factory, run_id=run_id, client_for=lambda agent_id: client,
        summary_channel_id=lambda c: "C-SUMMARY",
        source_channel_id={"single-cell-omics": "C_OMICS"}.get,
        pi_label_for=lambda subject: "Gordy Lab",
    )
    try:
        report = await announcer.finalize()

        assert report.posted == [decline_report_key("t9")] == ["decline:t9"]
        assert report.lost == [] and report.in_doubt == []
        [headline] = _headlines(client)
        expected = render_decline_headline(
            pi_label="Gordy Lab", reason=decline_reason(_DECLINE_BODY),
            permalink=client.get_permalink("C_OMICS", "1700000099.000000"),
        )
        assert headline["text"] == markdown_to_mrkdwn(expected)
        [row] = await _declines(factory, run_id)
        assert row.summary_posted_at is not None
        async with factory() as db:
            run = await db.get(SimulationRun, run_id)
            assert run.finalized_at is not None
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_database_failure_on_the_record_warns_and_does_not_raise(caplog):
    def _broken_factory():
        raise RuntimeError("database unavailable")

    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    client = FakeSlackClient(agent_id="blackbird")
    sim = SimulationEngine(
        agents=[hub], slack_clients={"blackbird": client},
        session_factory=_broken_factory, simulation_run_id=uuid.uuid4(),
    )
    _wire_summary_channel(sim)
    thread = ThreadState(thread_id="t1", channel="single-cell-omics", other_agent_id="gordy")

    with caplog.at_level(logging.WARNING, logger="src.agent.simulation"):
        await sim._record_hub_decline(hub, thread, _DECLINE_BODY, None)

    warnings = [r for r in caplog.records if "Could not record the hub decline" in r.getMessage()]
    assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING
    assert _headlines(client) == [], "nothing recorded, nothing announced"


@pytest.mark.asyncio
async def test_without_a_database_the_headline_posts_unclaimed():
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    client = FakeSlackClient(agent_id="blackbird")
    sim = SimulationEngine(agents=[hub], slack_clients={"blackbird": client})
    _wire_summary_channel(sim)
    thread = ThreadState(thread_id="t1", channel="single-cell-omics", other_agent_id="gordy")

    await sim._record_hub_decline(hub, thread, _DECLINE_BODY, None)

    [headline] = _headlines(client)
    expected = render_decline_headline(
        pi_label="gordy", reason=decline_reason(_DECLINE_BODY), permalink=None,
    )
    assert headline["text"] == markdown_to_mrkdwn(expected)


@pytest.mark.asyncio
async def test_a_close_that_carried_a_broken_sidecar_is_not_a_decline(engine, monkeypatch):
    """A ⏸️ reply whose sidecar could not be used is a dropped verdict, not a decline:
    calling it "declined without an assessment" in public could misstate a positive."""
    raw = f"{_DECLINE_REPLY}\n<assessment_json>{{\"recommendation\": \"advance\""
    sim, agent, thread, client, factory, run_id = await _drive_reply(
        engine, monkeypatch, raw, prior_messages=_DECIDE_COUNT, configure=_wire_summary_channel,
    )
    try:
        assert thread.status == "closed"
        assert await _declines(factory, run_id) == []
        assert _headlines(client) == []
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_hub_close_with_a_non_lab_counterparty_is_not_a_decline(engine, monkeypatch):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    other_hub = Agent("hub2", "Hub2Bot", "Hub Two", role="scout_hub")
    sim = SimulationEngine(
        agents=[hub, other_hub],
        slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird")},
        session_factory=factory, simulation_run_id=run_id,
    )
    _wire_summary_channel(sim)

    async def _fast_memory_update(*args, **kwargs):
        return None

    monkeypatch.setattr(sim, "_update_agent_memory", _fast_memory_update)
    thread = ThreadState(thread_id="t1", channel="single-cell-omics", other_agent_id="hub2")
    hub.state.active_threads["t1"] = thread
    try:
        await sim._check_thread_outcome(hub, thread, _DECLINE_BODY)
        assert await _declines(factory, run_id) == []
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_stored_assessment_blocks_the_decline(engine, monkeypatch):
    """The in-memory ledger can miss a stored assessment (a stale capture); the row in
    the database still decides."""
    from src.models import OpportunityAssessment

    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    sim, hub = _hub(factory, run_id)
    _wire_summary_channel(sim)

    async def _fast_memory_update(*args, **kwargs):
        return None

    monkeypatch.setattr(sim, "_update_agent_memory", _fast_memory_update)
    async with factory() as db:
        db.add(OpportunityAssessment(simulation_run_id=run_id, agent_id="blackbird",
                                     subject_agent_id="gordy", channel_name="single-cell-omics",
                                     thread_id="t1"))
        await db.commit()
    thread = ThreadState(thread_id="t1", channel="single-cell-omics", other_agent_id="gordy")
    hub.state.active_threads["t1"] = thread
    try:
        await sim._check_thread_outcome(hub, thread, _DECLINE_BODY)
        assert await _declines(factory, run_id) == []
        assert _headlines(sim.slack_clients["blackbird"]) == []
    finally:
        await _delete_run(factory, run_id)
