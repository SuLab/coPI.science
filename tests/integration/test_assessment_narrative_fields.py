"""The three reviewer-facing narrative fields on an assessment (migration 0043).

Task 2 covers the columns themselves; Task 8 extends this file with the engine
write path that fills them from the sidecar.
"""

import pytest
from sqlalchemy import select

from src.models import OpportunityAssessment, SimulationRun

pytestmark = pytest.mark.integration


async def _delete_run(factory, run_id):
    """Clean up a run committed outside the rollback-scoped `db_session` fixture.

    The two `engine`-based tests below open their own `async_sessionmaker` and
    call `.commit()` for real, so nothing rolls their `SimulationRun` back —
    unlike the rest of this file, which uses `db_session` and is cleaned up by
    its per-test transaction. `tests/integration/test_harness_smoke.py`'s
    `test_writes_are_rolled_back_*` pair is the canary that catches a leak
    like this (it sorts alphabetically after this file, so a leaked row here
    is still present when it runs); a future test copying this `engine`/
    `async_sessionmaker` harness needs its own cleanup too. Deleting the run
    is sufficient — `OpportunityAssessment.simulation_run_id` is
    ON DELETE CASCADE, so its row goes with it.
    """
    async with factory() as cleanup:
        stale = (await cleanup.execute(
            select(SimulationRun).where(SimulationRun.id == run_id)
        )).scalar_one_or_none()
        if stale is not None:
            await cleanup.delete(stale)  # cascades to the assessment
            await cleanup.commit()


async def _seed_run(db):
    run = SimulationRun()
    db.add(run)
    await db.flush()
    return run


async def test_narrative_fields_round_trip(db_session):
    run = await _seed_run(db_session)
    row = OpportunityAssessment(
        simulation_run_id=run.id,
        agent_id="blackbird",
        channel_name="general",
        company_or_project="Short label",
        headline="A blood test that says who responds to immunotherapy in liver cancer.",
        key_points=["The classifier does not exist yet", "Circadian confound unmeasured"],
        elevator_pitch="Hopkins has 39-plex cytokine data on 124 patients.",
    )
    db_session.add(row)
    await db_session.flush()
    db_session.expunge(row)

    stored = (
        await db_session.execute(
            select(OpportunityAssessment).where(OpportunityAssessment.id == row.id)
        )
    ).scalar_one()
    assert stored.headline.startswith("A blood test")
    assert stored.key_points == [
        "The classifier does not exist yet",
        "Circadian confound unmeasured",
    ]
    assert stored.elevator_pitch.startswith("Hopkins has")


async def test_narrative_fields_default_to_sql_null_not_json_null(db_session):
    """`key_points` is JSONB, and `none_as_null=True` is what keeps Python None
    a real SQL NULL. Without it, `WHERE key_points IS NULL` misses the row —
    the exact defect 0031 and 0036 each had to repair once (A14)."""
    run = await _seed_run(db_session)
    row = OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", channel_name="general",
        key_points=None,
    )
    db_session.add(row)
    await db_session.flush()

    found = (
        await db_session.execute(
            select(OpportunityAssessment.id).where(
                OpportunityAssessment.id == row.id,
                OpportunityAssessment.key_points.is_(None),
            )
        )
    ).scalar_one_or_none()
    assert found == row.id


async def test_persist_assessment_stores_the_three_narrative_fields(engine):
    """Sidecar items 6-8 reach their columns."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    try:
        stub = SimulationEngine(
            agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
        )
        await SimulationEngine._persist_assessment(stub, "blackbird", "general", {
            "subject_agent_id": "wang",
            "company_or_project": "Short label",
            "headline": "A blood test that says who responds to immunotherapy.",
            "key_points": ["No classifier exists yet", "Circadian confound unmeasured"],
            "elevator_pitch": "Hopkins has cytokine data on 124 patients.",
            "recommendation": "conditional",
            "scores": {},
        })

        async with factory() as db:
            row = (await db.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalars().one()
        assert row.headline.startswith("A blood test")
        assert row.key_points == ["No classifier exists yet", "Circadian confound unmeasured"]
        assert row.elevator_pitch.startswith("Hopkins has")
    finally:
        await _delete_run(factory, run_id)


async def test_a_non_list_key_points_degrades_to_null_and_keeps_raw_verdict(engine):
    """A20. A model that answers `key_points` with a string must not DataError
    the row out of existence — the row IS the archive. It degrades to NULL and
    raw_verdict keeps what was actually emitted, exactly like `red_flags` and
    `derisking_milestones` already do."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    try:
        stub = SimulationEngine(
            agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
        )
        await SimulationEngine._persist_assessment(stub, "blackbird", "general", {
            "company_or_project": "Short label",
            "key_points": "not a list at all",
            "recommendation": "pass",
            "scores": {},
        })

        async with factory() as db:
            row = (await db.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalars().one()
        assert row.key_points is None
        assert row.raw_verdict["key_points"] == "not a list at all"
    finally:
        await _delete_run(factory, run_id)


def test_normalize_accepts_legacy_list_and_the_three_group_object():
    from src.services.assessment_detail import normalize_key_points

    assert normalize_key_points(["a", "b", "c"]) == ["a", "b", "c"]
    obj = {"significance": ["s"], "innovation": ["i1", "i2"], "commercial_potential": ["c"]}
    assert normalize_key_points(obj) == obj


def test_normalize_rejects_wrong_shapes():
    from src.services.assessment_detail import normalize_key_points

    assert normalize_key_points("not a list") is None
    assert normalize_key_points({"significance": ["s"]}) is None            # missing keys
    assert normalize_key_points(
        {"significance": "s", "innovation": [], "commercial_potential": []}
    ) is None
    assert normalize_key_points(
        {"significance": [], "innovation": [], "commercial_potential": [], "extra": []}
    ) is None
