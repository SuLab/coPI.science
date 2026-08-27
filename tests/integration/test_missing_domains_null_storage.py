"""``missing_domains`` NULL must be a SQL NULL, not a JSON ``null``.

`OpportunityAssessment.missing_domains` documents three states, and the middle
one is spelled NULL: names = a demonstrated gap, NULL = the panel was verified
complete (or none was owed), ``[]`` = the floor could not be checked at all.

The column was `mapped_column(JSONB, nullable=True)`, and SQLAlchemy's JSON type
defaults `none_as_null=False` — so Python `None` was persisted as the JSONB
scalar `null`, not as SQL NULL. Measured on production 2026-08-20: all 15 rows
written since 2026-08-19 had `jsonb_typeof(missing_domains) = 'null'` and
`missing_domains IS NULL` = false, while the 18 older rows held a true SQL NULL.
One logical state, two physical encodings, in one column.

Nothing was mis-rendering, because every consumer reads it in Python
(`assessment_detail.panel_state` asks `is None`) or in Jinja (`or []`), and both
encodings deserialize to `None`. That is exactly what made it worth fixing: the
documented contract says NULL, so the first SQL-level reader to be written
against the docs — `WHERE missing_domains IS NULL`, the obvious way to count
verified panels — silently reclassifies every recent verified row as unverified,
inverting the one number this instrumentation exists to produce.

These assertions are deliberately at the SQL level. Through the ORM the bug is
invisible, so an ORM-level test could never have caught it and cannot protect
against its return.
"""

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState
from src.models import OpportunityAssessment, SimulationRun
from src.services.blackbird_rubric import RUBRIC_WEIGHTS

pytestmark = pytest.mark.integration


# `advance` is one of the two recommendations held to the panel floor, so these
# verdicts exercise the real gap arithmetic rather than the "no panel owed"
# shortcut a `pass` takes.
def _verdict() -> dict:
    return {
        "subject_agent_id": "gordy",
        "recommendation": "advance",
        "rationale": "A peptide-based vaccine platform.",
        "scores": {key: 4 for key in RUBRIC_WEIGHTS},
    }


async def _new_run(factory):
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        return run.id


async def _storage_shape(factory, run_id):
    """How Postgres actually holds the value — the question the ORM cannot ask."""
    async with factory() as db:
        return (await db.execute(text(
            "SELECT missing_domains IS NULL AS sql_null, "
            "       jsonb_typeof(missing_domains) AS json_type "
            "FROM opportunity_assessments WHERE simulation_run_id = :run"
        ), {"run": str(run_id)})).one()


async def _delete_run(factory, run_id):
    async with factory() as cleanup:
        stale = (await cleanup.execute(
            select(SimulationRun).where(SimulationRun.id == run_id)
        )).scalar_one_or_none()
        if stale is not None:
            await cleanup.delete(stale)
            await cleanup.commit()


@pytest.mark.asyncio
async def test_a_verified_panel_stores_a_real_sql_null(engine):
    """The verified-complete state. `missing_domains IS NULL` must be true in
    SQL, which is what the column's own documentation promises."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    sim = SimulationEngine(
        agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
    )
    # A complete panel for this verdict, recorded in memory the way a real
    # interview's consults arrive, so the floor finds no gap AND is verifiable.
    # Exactly what `required_domains_for` asks of this verdict: the two always-
    # required domains, plus chemistry (the "peptide" cue), technologic
    # (platform scored 4), and legal (ip_fto scored 4 — the trigger that
    # the third gating key since rubric v2.1.0).
    for domain in ("scientific", "talent", "chemistry", "technologic", "legal"):
        sim._record_consult("gordy", domain, "t1")
    thread = ThreadState(
        thread_id="t1", channel="general", other_agent_id="gordy",
        message_count=11, floor_armed=True,
    )
    try:
        await sim._persist_assessment(
            "blackbird", "general", _verdict(), slack_ts="1.1",
            subject_agent_id_fallback="gordy", thread=thread,
        )
        async with factory() as db:
            row = (await db.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalar_one()
            assert row.panel_incomplete is False
            assert row.missing_domains is None

        shape = await _storage_shape(factory, run_id)
        assert shape.sql_null is True, (
            "the verified state must be a SQL NULL — a JSONB 'null' scalar makes "
            "`WHERE missing_domains IS NULL` skip this row"
        )
        assert shape.json_type is None
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_the_unverified_state_stays_an_empty_json_array(engine):
    """The third state must remain distinguishable from the second. `[]` is not
    None, so `none_as_null` must not touch it — otherwise the fix for one
    conflation creates another, and every post-restart verdict reads as vetted."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    # No consults recorded anywhere, so the floor cannot be checked at all.
    sim = SimulationEngine(
        agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
    )
    thread = ThreadState(
        thread_id="t1", channel="general", other_agent_id="gordy",
        message_count=11, floor_armed=False,
    )
    try:
        await sim._persist_assessment(
            "blackbird", "general", _verdict(), slack_ts="1.1",
            subject_agent_id_fallback="gordy", thread=thread,
        )
        async with factory() as db:
            row = (await db.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalar_one()
            assert row.missing_domains == []

        shape = await _storage_shape(factory, run_id)
        assert shape.sql_null is False
        assert shape.json_type == "array"
    finally:
        await _delete_run(factory, run_id)
