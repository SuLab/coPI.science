"""Verdict scoring end-to-end: one scale, one evidence bar (rubric v3).

The wiring under test is `SimulationEngine._persist_assessment` — it computes
`weighted_score`/`band` from the verdict's scores — checked from a verdict dict
to the stored columns. Since v3.1.0 the sidecar contract carries no
`funnel_stage` at all; a stray one (an old image, a hallucinated field) must be
inert: stored as passthrough into the retained nullable column, never an input
to anything.

Why an integration test and not a unit test of the library: the library's own
scoring is covered in tests/unit/, but the bug this guards against is a wiring
bug — a version that quietly reintroduced stage-selected scoring (the v2
regime) would store stage-dependent numbers while every unit test of
weighted_score stayed green.

Expected scores are recomputed here from `RUBRIC_WEIGHTS` rather than written
as literals: the point is "the stored number came from THIS weight set". The
BAND, by contrast, is asserted as a literal — that is the decision, and it must
be pinned to a name, not to a re-derivation of the thresholds under test.

Engine-driving pattern (a real `SimulationEngine` with no agents, called
directly) follows tests/integration/test_specialist_consult_capture.py §5: the
specialist-floor gate reaches for `self._specialist_consults` and friends, so a
SimpleNamespace stub does not survive the call.
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.simulation import SimulationEngine
from src.models import OpportunityAssessment, SimulationRun
from src.services.blackbird_rubric import (
    RUBRIC_VERSION,
    RUBRIC_WEIGHTS,
)
from src.services.blackbird_rubric import (
    band as rubric_band,
)

pytestmark = pytest.mark.integration


# Deliberately not flat: a flat "all 3s" vector would score 3.0 under any
# weight permutation and make the came-from-these-weights assertion vacuous.
_SCORES = {
    "differentiation_unmet_need": 4,
    "scientific_credibility": 3,
    "translational_path": 3,
    "fundable_experiment": 5,
    "venture_potential": 2,
    "team_executability": 4,
}


def _expected() -> float:
    """The weighted mean of ``_SCORES`` under the document's weights, computed
    here. Every score is in range and every dimension is present, so none of
    weighted_score's clamp/missing/non-finite branches applies and this plain
    sum is the whole computation."""
    return round(
        sum(_SCORES[k] * w for k, w in RUBRIC_WEIGHTS.items())
        / sum(RUBRIC_WEIGHTS.values()),
        2,
    )


async def _new_run(factory):
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        return run.id


async def _delete_run(factory, run_id):
    async with factory() as cleanup:
        stale = (await cleanup.execute(
            select(SimulationRun).where(SimulationRun.id == run_id)
        )).scalar_one_or_none()
        if stale is not None:
            await cleanup.delete(stale)  # cascades to the assessment
            await cleanup.commit()


async def _persist_and_read(engine, verdict):
    """Drive `_persist_assessment` with ``verdict`` and return the stored row."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    sim = SimulationEngine(
        agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
    )
    try:
        await sim._persist_assessment("blackbird", "general", verdict)
        async with factory() as db:
            return (await db.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalar_one()
    finally:
        await _delete_run(factory, run_id)


def _verdict(**overrides):
    verdict = {
        "subject_agent_id": "wang",
        "company_or_project": "Single-scale scoring fixture",
        "recommendation": "route-to-incubation",
        "scores": dict(_SCORES),
    }
    verdict.update(overrides)
    return verdict


@pytest.mark.asyncio
async def test_a_verdict_is_scored_on_the_one_scale(engine):
    row = await _persist_and_read(engine, _verdict())

    assert row.weighted_score == pytest.approx(_expected())
    assert row.band == "advance"
    # The contract carries no funnel_stage since v3.1.0; the retained column
    # stays NULL on a contract-shaped verdict.
    assert row.funnel_stage is None
    # And the row says which rubric revision it was scored under. A score is
    # only comparable to another score from the same document.
    assert row.rubric_version == RUBRIC_VERSION


@pytest.mark.asyncio
async def test_a_stray_funnel_stage_field_is_inert(engine):
    """A verdict carrying the removed field (old image, hallucination) must
    score identically — the field is stored as passthrough and read by
    nothing. A version that quietly reintroduced stage-selected scoring fails
    here first."""
    plain = await _persist_and_read(engine, _verdict())
    for stray in ("incubation", "seed", "wildcat"):
        row = await _persist_and_read(engine, _verdict(funnel_stage=stray))
        assert (row.weighted_score, row.band) == (
            plain.weighted_score, plain.band,
        ), f"stray funnel_stage {stray!r} changed the arithmetic"
        assert row.funnel_stage == stray  # passthrough, not an input


@pytest.mark.asyncio
async def test_the_score_and_the_band_are_always_consistent(engine):
    """The stored band must be the band the stored score falls in — checked as
    a property rather than by reading the source."""
    row = await _persist_and_read(engine, _verdict())
    assert row.band == rubric_band(row.weighted_score), (
        f"stored band {row.band!r} is not the band {row.weighted_score} falls in"
    )
