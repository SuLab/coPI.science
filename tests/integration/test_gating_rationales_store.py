"""`gating_rationales` (scout_hub 1.10.0, migration 0058) through the engine's
write path: stored beside `dimension_rationales` on insert, degraded to SQL NULL
on a malformed map or a sidecar that carries none, and carried by the in-place
update `Verdicts.upsert` makes when a later verdict supersedes the interview's
row (one row per run and thread since 0055)."""
import logging
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.engine.verdicts import Verdicts
from src.agent.simulation import SimulationEngine
from src.models import OpportunityAssessment, SimulationRun
from tests.integration.test_assessment_narrative_fields import _delete_run, _persist_and_read
from tests.integration.test_verdict_upsert import _cleanup, _row, _rows, _setup, _thread

pytestmark = pytest.mark.integration

GATES = {
    "life_sciences_domain": "met",
    "credible_science": "met",
    "translational_potential": "unconfirmed",
}


async def test_gate_reasons_are_stored_and_an_overlong_one_warned(engine, caplog):
    reasons = {
        "life_sciences_domain": "A blood test for liver cancer.",
        "credible_science": "Two published cohorts replicate the signal.",
        "translational_potential": "x" * 201,
    }
    row, warnings = await _persist_and_read(engine, caplog, {
        "company_or_project": "Short label",
        "gating": GATES,
        "gating_rationales": reasons,
        "recommendation": "conditional",
        "scores": {},
    })
    assert row.gating_rationales == reasons
    assert row.gating == GATES
    assert "gating_rationales.translational_potential is 201 chars" in warnings
    assert "gating_rationales.credible_science is" not in warnings
    assert "gate(s) with no reason" not in warnings


async def test_a_malformed_gate_reason_map_is_dropped_to_null_and_named(engine, caplog):
    row, warnings = await _persist_and_read(engine, caplog, {
        "company_or_project": "Short label",
        "gating": {"credible_science": "met"},
        "gating_rationales": {"credible_science": 3},
        "recommendation": "conditional",
        "scores": {},
    })
    assert row.gating_rationales is None
    assert row.raw_verdict["gating_rationales"] == {"credible_science": 3}
    assert "gating_rationales was DROPPED" in warnings
    assert "has 1 gate(s) with no reason: credible_science" in warnings


async def test_a_sidecar_without_gate_reasons_stores_sql_null(engine, caplog):
    """NULL means "never asked" (a pre-1.10.0 prompt) and must be a real SQL
    NULL, not JSON null, or `WHERE gating_rationales IS NULL` misses the row
    (the A14 defect class)."""
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
        with caplog.at_level(logging.WARNING):
            await Verdicts._persist_assessment(stub, "blackbird", "general", {
                "company_or_project": "Short label",
                "gating": GATES,
                "recommendation": "conditional",
                "scores": {},
            })
        async with factory() as db:
            found = (await db.execute(
                select(OpportunityAssessment.id).where(
                    OpportunityAssessment.simulation_run_id == run_id,
                    OpportunityAssessment.gating_rationales.is_(None),
                )
            )).scalar_one_or_none()
        assert found is not None
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_superseding_verdict_replaces_the_gate_reasons_in_place(engine):
    """The interview's one row is updated in place (§8.1); the later verdict's
    reasons replace the earlier ones, and a later verdict with none leaves
    NULL rather than keeping reasons it never gave."""
    factory, run_id, sim = await _setup(engine)
    try:
        first = await sim.verdicts.upsert(
            _thread(), {**_row(run_id, n=1), "gating_rationales": {"credible_science": "first"}},
            uuid.uuid4(), 8,
        )
        second = await sim.verdicts.upsert(
            _thread(), {**_row(run_id, n=2), "gating_rationales": {"credible_science": "second"}},
            uuid.uuid4(), 10,
        )
        assert second.outcome == "updated"
        assert second.assessment_id == first.assessment_id
        (row,) = await _rows(factory, run_id)
        assert row.gating_rationales == {"credible_science": "second"}
        third = await sim.verdicts.upsert(
            _thread(), {**_row(run_id, n=3), "gating_rationales": None}, uuid.uuid4(), 12,
        )
        assert third.outcome == "updated"
        (row,) = await _rows(factory, run_id)
        assert row.gating_rationales is None
    finally:
        await _cleanup(factory, run_id)
