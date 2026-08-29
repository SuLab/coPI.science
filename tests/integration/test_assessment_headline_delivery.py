"""Every assessment whose interview has ENDED gets exactly one
#assessments-summary headline, exactly once, durably.

See docs/audits/2026-08-29-lost-assessment-headlines/README.md. Production run
61ccad6d lost the rothstein verdict's headline (conditional, 2.85 — the run's
highest score) because the interview ended by `max_thread_messages` timeout
instead of by a terminal reply, and nothing announces on that path.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.channels import ASSESSMENTS_SUMMARY_CHANNEL
from src.models import OpportunityAssessment, SimulationRun
from tests.integration.test_hub_assessment_capture_gate import (  # noqa: F401
    _assessments,
    _delete_run,
    _drive_reply,
    _reply_with_sidecar,
)

pytestmark = pytest.mark.integration

# `phase4_guidance` takes the ORDINAL (message_count + 1). Seeding N prior
# messages makes the generated reply ordinal N+1.
_CONCLUDE_COUNT = 11     # ordinal 12 — the hub's own concluding turn
_LAST_DECIDE_COUNT = 10  # ordinal 11 — the turn that lost rothstein's headline


def _wire_summary_channel(sim):
    """Without this a headline is skipped for an unrelated reason
    (`channel_id=None, transport not connected`) and a delivery test passes
    while proving nothing. Production fills this in via
    `_ensure_assessments_summary_channel`. Must be applied via `_drive_reply`'s
    `configure=` seam — BEFORE `_reply_to_thread` runs internally — or it is
    wired too late to affect the drive it is meant to cover."""
    sim._assessments_summary_channel_id = "C-SUMMARY"
    sim._channel_id_map[ASSESSMENTS_SUMMARY_CHANNEL] = "C-SUMMARY"
    sim._channel_id_map["single-cell-omics"] = "C_OMICS"


def _headlines(client):
    return [p for p in client.posted if p.get("channel") == ASSESSMENTS_SUMMARY_CHANNEL]


@pytest.mark.asyncio
async def test_summary_posted_at_defaults_to_null(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        run = SimulationRun()
        db.add(run)
        await db.commit()
        run_id = run.id
    try:
        async with factory() as db:
            row = OpportunityAssessment(
                simulation_run_id=run_id, agent_id="blackbird",
                channel_name="general", thread_id="t1",
            )
            db.add(row)
            await db.commit()
            row_id = row.id
        async with factory() as db:
            stored = (await db.execute(
                select(OpportunityAssessment).where(OpportunityAssessment.id == row_id)
            )).scalar_one()
            assert stored.summary_posted_at is None, (
                "a fresh verdict has not been announced"
            )
    finally:
        async with factory() as db:
            stale = (await db.execute(
                select(SimulationRun).where(SimulationRun.id == run_id)
            )).scalar_one_or_none()
            if stale is not None:
                await db.delete(stale)
                await db.commit()


@pytest.mark.asyncio
async def test_a_posted_headline_is_recorded_on_the_row(engine, monkeypatch):
    sim, agent, thread, client, factory, run_id = await _drive_reply(
        engine, monkeypatch, _reply_with_sidecar(), prior_messages=_CONCLUDE_COUNT,
        configure=_wire_summary_channel,
    )
    try:
        # The ordinal-12 reply announces on its own — the wiring landed before
        # `_reply_to_thread` ran, so `_post_assessment_summary` had a connected
        # transport and channel id to post through.
        rows = await _assessments(factory, run_id)
        assert len(rows) == 1
        assert len(_headlines(client)) == 1, "the CONCLUDE turn announces"
        assert rows[0].summary_posted_at is not None, (
            "a posted headline is recorded durably, so a restart cannot re-post it"
        )
    finally:
        await _delete_run(factory, run_id)
