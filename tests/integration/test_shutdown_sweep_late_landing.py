"""stop()'s headline sweep: a verdict that lands only in the final flush still
gets its headline (a second sweep), and a lost engine lock posts nothing."""
import logging

import pytest

from src.agent.engine.context import RunState
from tests.integration.test_assessment_headline_delivery import _headlines, _wire_summary_channel
from tests.integration.test_hub_assessment_capture_gate import (
    _assessments,
    _delete_run,
    _drive_reply,
    _reply_with_sidecar,
)

pytestmark = pytest.mark.integration

_CONCLUDE_COUNT = 11


def _dependent_writes_stuck(sim):
    """Every non-final dependent write is queued: the capture, and stop()'s step-1
    flushes (the final flush skips the gate, so the verdict lands only there)."""
    _wire_summary_channel(sim)

    async def _stuck():
        return False

    sim.persistence.flush_before_dependent = _stuck


@pytest.mark.asyncio
async def test_a_verdict_that_lands_only_in_the_final_flush_is_announced(engine, monkeypatch):
    sim, _agent, _thread, client, factory, run_id = await _drive_reply(
        engine, monkeypatch, _reply_with_sidecar(), prior_messages=_CONCLUDE_COUNT,
        configure=_dependent_writes_stuck,
    )
    try:
        assert [r["thread_id"] for r in sim.verdicts._pending_assessments] == ["t1"]
        assert _headlines(client) == [], "a queued verdict is never announced at capture"
        await sim.stop()
        [row] = await _assessments(factory, run_id)
        assert row.summary_posted_at is not None
        assert len(_headlines(client)) == 1, "the second sweep posted it, once"
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_nothing_queued_at_the_final_flush_means_no_second_sweep(engine, monkeypatch):
    """The normal path: one sweep."""
    sim, _agent, _thread, _client, factory, run_id = await _drive_reply(
        engine, monkeypatch, _reply_with_sidecar(), prior_messages=_CONCLUDE_COUNT,
        configure=_wire_summary_channel,
    )
    calls = []
    real = sim.headlines.shutdown_sweep

    async def _counted(end_reason, **kw):
        calls.append(kw.get("only"))
        return await real(end_reason, **kw)

    sim.headlines.shutdown_sweep = _counted
    try:
        await sim.stop()
        assert calls == [None]
    finally:
        await _delete_run(factory, run_id)


def _first_headline_refused(sim):
    """The capture's own headline is refused (claim released, not announced), so
    the interview still owes one at the stop."""
    from src.agent.channels import ASSESSMENTS_SUMMARY_CHANNEL

    _wire_summary_channel(sim)
    client = sim.slack_clients["blackbird"]
    real = client.apost_message
    state = {"refused": False}

    async def refuse_once(channel, text, **kw):
        if channel == ASSESSMENTS_SUMMARY_CHANNEL and not state["refused"]:
            state["refused"] = True
            return None
        return await real(channel, text, **kw)

    client.apost_message = refuse_once


@pytest.mark.asyncio
async def test_a_lost_engine_lock_posts_no_headline_at_shutdown(engine, monkeypatch, caplog):
    """The closing reply ended the interview, so a HOLD sweep would announce it;
    after lock_lost another writer may hold the lock, so nothing posts."""
    sim, _agent, _thread, client, factory, run_id = await _drive_reply(
        engine, monkeypatch, _reply_with_sidecar(closing=True), prior_messages=_CONCLUDE_COUNT,
        configure=_first_headline_refused,
    )
    try:
        assert sim.headlines.is_announced("t1") is False
        sim.run_state.request_stop("lock_lost")
        with caplog.at_level(logging.ERROR, logger="src.agent.simulation"):
            await sim.stop()
        [row] = await _assessments(factory, run_id)
        assert row.summary_posted_at is None and row.summary_claimed_at is None
        assert _headlines(client) == []
        assert "engine lock was lost" in caplog.text
        assert "backfill_assessment_headlines.py --run" in caplog.text
    finally:
        await _delete_run(factory, run_id)


def test_lock_lost_is_remembered_when_the_precedence_keeps_another_hold_reason():
    state = RunState()
    state.request_stop("operator_hold")
    state.request_stop("lock_lost")
    assert state.end_reason == "operator_hold"
    assert state.lock_lost is True
