"""Migration 0030 and its two models agree: specialist_consults round-trips via
the ORM, and the rubric-version stamp columns on opportunity_assessments accept
and return values.

These run against the conftest ephemeral Postgres, which is migrated with the
real alembic chain — so a column-name or nullability mismatch between
``src/models/specialist_consult.py`` and ``0030_specialist_consults_rubric_version``
fails here rather than at first production write.
"""

import pytest
from sqlalchemy import select

from src.models import OpportunityAssessment, SimulationRun, SpecialistConsult

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_specialist_consult_row_round_trips(db_session):
    run = SimulationRun()
    db_session.add(run)
    await db_session.flush()

    db_session.add(SpecialistConsult(
        simulation_run_id=run.id,
        agent_id="blackbird",
        subject_agent_id="wang",
        thread_id="1755550000.000200",
        channel_name="molecular-mechanisms",
        domain="chemistry",
        question="Is a selective ATG4B inhibitor tractable as chemical matter?",
        context_excerpt="PI: we have a covalent fragment hit series...",
        verdict_signal="caution",
        confidence="moderate",
        concerns=["No selectivity data against ATG4A", "Covalent liability unprofiled"],
        questions_to_ask=["What is the fold selectivity vs ATG4A?"],
        raw_opinion='{"verdict_signal": "caution", "concerns": ["..."]}',
    ))
    await db_session.flush()

    row = (await db_session.execute(select(SpecialistConsult))).scalar_one()
    assert row.agent_id == "blackbird"
    assert row.subject_agent_id == "wang"
    assert row.thread_id == "1755550000.000200"
    assert row.channel_name == "molecular-mechanisms"
    assert row.domain == "chemistry"
    assert row.verdict_signal == "caution"
    assert row.confidence == "moderate"
    assert row.concerns == [
        "No selectivity data against ATG4A", "Covalent liability unprofiled",
    ]
    assert row.questions_to_ask == ["What is the fold selectivity vs ATG4A?"]
    assert row.raw_opinion.startswith('{"verdict_signal"')
    assert row.created_at is not None


@pytest.mark.asyncio
async def test_specialist_consult_nullable_columns_tolerate_sparse_rows(db_session):
    """subject/thread/channel/context/lists are nullable — a consult recorded
    outside a full interview context must still be storable."""
    run = SimulationRun()
    db_session.add(run)
    await db_session.flush()

    db_session.add(SpecialistConsult(
        simulation_run_id=run.id,
        agent_id="blackbird",
        domain="scientific",
        question="Is the claimed rescue adequately controlled?",
        verdict_signal="caution",
        confidence="low",
        raw_opinion="prose answer",
    ))
    await db_session.flush()

    row = (await db_session.execute(select(SpecialistConsult))).scalar_one()
    assert row.subject_agent_id is None
    assert row.thread_id is None
    assert row.channel_name is None
    assert row.context_excerpt is None
    assert row.concerns is None
    assert row.questions_to_ask is None


@pytest.mark.asyncio
async def test_simulation_run_delete_cascades_to_specialist_consults(db_session):
    """Cleanup contract matches every other run-scoped table: deleting the run
    takes its consults with it via the FK cascade (there is no ORM
    relationship(), so this is the DDL's job, and only a live FK proves it)."""
    run = SimulationRun()
    db_session.add(run)
    await db_session.flush()
    db_session.add(SpecialistConsult(
        simulation_run_id=run.id,
        agent_id="blackbird",
        domain="talent",
        question="Can this lab execute the proposed workplan?",
        verdict_signal="clear",
        confidence="high",
        raw_opinion="{}",
    ))
    await db_session.flush()

    await db_session.delete(run)
    await db_session.flush()

    remaining = (await db_session.execute(select(SpecialistConsult))).scalars().all()
    assert remaining == []


@pytest.mark.asyncio
async def test_assessment_rubric_version_stamp_round_trips(db_session):
    run = SimulationRun()
    db_session.add(run)
    await db_session.flush()

    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id,
        agent_id="blackbird",
        channel_name="general",
        rubric_version="1.0.0",
        rubric_content_hash="a1b2c3d4e5f6",
    ))
    await db_session.flush()

    row = (await db_session.execute(select(OpportunityAssessment))).scalar_one()
    assert row.rubric_version == "1.0.0"
    assert row.rubric_content_hash == "a1b2c3d4e5f6"


@pytest.mark.asyncio
async def test_assessment_rubric_stamp_is_nullable_for_preexisting_rows(db_session):
    """Every row written before 0030 has NULL stamps; the model must accept and
    report that, not invent a default."""
    run = SimulationRun()
    db_session.add(run)
    await db_session.flush()
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", channel_name="general",
    ))
    await db_session.flush()
    row = (await db_session.execute(select(OpportunityAssessment))).scalar_one()
    assert row.rubric_version is None
    assert row.rubric_content_hash is None
