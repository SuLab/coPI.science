"""Which rubric scale scored a verdict follows from the verdict's funnel stage.

Rubric v2.0.0 (docs/specs/2026-08-20-rubric-v2-incubation-rebaseline-proposal.md)
gave the document a second scale: incubation weights and lower band lines, for
the stage the screened population is actually at. The selection happens in ONE
place — `SimulationEngine._persist_assessment` passes the verdict's raw
`funnel_stage` into `weighted_score`/`band` — and this file is the end-to-end
check on it, from a verdict dict to the two stored columns.

Why an integration test and not a unit test of the library: the library's own
stage handling is covered in tests/unit/, but the bug this guards against is a
wiring bug. `_persist_assessment` reads `funnel_stage` twice — once raw, for
scoring, and once through `_bounded_str` for the column — and a version that
scored before selecting, or passed the stage to only one of the two calls, would
store a score from one scale next to a band from the other and look entirely
plausible on the page.

Expected scores are recomputed here from `RUBRIC_WEIGHTS_INCUBATION` /
`RUBRIC_WEIGHTS` rather than written as literals: the point is "the stored number
came from THIS weight set", and a literal would also pass if the weights were
right and the selection wrong. The BAND, by contrast, is asserted as a literal —
that is the decision, and it must be pinned to a name, not to a re-derivation of
the thresholds under test.

Engine-driving pattern (a real `SimulationEngine` with no agents, called
directly) follows tests/integration/test_specialist_consult_capture.py §5: the
specialist-floor gate reaches for `self._specialist_consults` and friends, so a
SimpleNamespace stub does not survive the call. `_persist_assessment` is called
directly rather than through `_reply_to_thread`, so none of the sidecar-capture
gates (turn phase, one-verdict-per-thread) are in play — those are covered where
they live, and seeding a MessageLog history here would only test them again.
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.simulation import SimulationEngine
from src.models import OpportunityAssessment, SimulationRun
from src.services.blackbird_rubric import (
    RUBRIC_VERSION,
    RUBRIC_WEIGHTS,
    RUBRIC_WEIGHTS_INCUBATION,
)

pytestmark = pytest.mark.integration


# One score vector, used for every case below, so the ONLY thing that differs
# between them is the funnel stage. Deliberately not flat: the four dimensions
# whose weight moves most between the scales (workplan_capital_efficiency 1->8,
# external_signals 8->2, ip_fto 6->4, experimental_rigor 10->8) carry different
# values here, so the two scales cannot arithmetically agree. A flat "all 3s"
# vector scores exactly 3.0 on both and would make this whole file vacuous.
_SCORES = {
    "differentiation": 4,
    "market_unmet_need": 4,
    "team": 4,
    "external_signals": 1,
    "ip_fto": 2,
    "platform": 3,
    "dev_regulatory_feasibility": 3,
    "workplan_capital_efficiency": 5,
    "exit_thesis": 2,
    "mechanism_validation": 4,
    "toxicity_selectivity": 3,
    "experimental_rigor": 4,
    "chemistry_dc_path": 2,
}


def _expected(weights: dict[str, int]) -> float:
    """The weighted mean of ``_SCORES`` under ``weights``, computed here.

    Every score is in range and every dimension is present, so none of
    weighted_score's clamp/missing/non-finite branches applies and this plain
    sum is the whole computation.
    """
    return round(
        sum(_SCORES[k] * w for k, w in weights.items()) / sum(weights.values()), 2
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
        "company_or_project": "Stage-aware scoring fixture",
        "recommendation": "route-to-incubation",
        "scores": dict(_SCORES),
    }
    verdict.update(overrides)
    return verdict


@pytest.mark.asyncio
async def test_an_incubation_verdict_is_scored_on_the_incubation_scale(engine):
    """The re-baseline's whole purpose: an incubation-stage verdict scores on
    the incubation weights and bands on the incubation lines.

    Under the investment scale this same vector bands "conditional" (its score
    sits below 4.0); under the incubation scale it clears 3.4 and advances. That
    is the calibration working — the population is ~100% incubation-stage, and
    before this every one of 34 production verdicts banded "pass".
    """
    row = await _persist_and_read(engine, _verdict(funnel_stage="incubation"))

    expected = _expected(RUBRIC_WEIGHTS_INCUBATION)
    assert row.weighted_score == pytest.approx(expected)
    assert row.band == "advance"
    # Not accidentally equal to the investment answer — if it were, this test
    # would pass against a version that ignored the stage entirely.
    assert expected != _expected(RUBRIC_WEIGHTS)
    # The stage itself is stored, so a reader (and the page's per-row weight
    # selection) can tell which scale produced the number.
    assert row.funnel_stage == "incubation"
    # And the row says which rubric revision it was scored under. A score is
    # only comparable to another score from the same document. ("2.1.0" is the
    # 2026-08-24 prose revision — same weights, thresholds and scales as 2.0.0,
    # so the stage-aware arithmetic this file pins is unchanged.)
    assert row.rubric_version == RUBRIC_VERSION == "2.1.0"


@pytest.mark.asyncio
async def test_a_later_stage_verdict_keeps_the_investment_scale(engine):
    """"seed" is not "incubation", so nothing changes for it. The investment
    scale is not deprecated — it is the right bar for an opportunity that has
    reached a stage where later-stage evidence is a fair thing to ask for."""
    row = await _persist_and_read(engine, _verdict(funnel_stage="seed"))

    assert row.weighted_score == pytest.approx(_expected(RUBRIC_WEIGHTS))
    assert row.band == "conditional"
    assert row.funnel_stage == "seed"


@pytest.mark.asyncio
async def test_a_verdict_with_no_funnel_stage_falls_back_to_investment(engine):
    """A missing stage must not silently pick the more permissive scale.

    The model is asked for `funnel_stage` but the field is not enforced, and
    "we were never told the stage" is not evidence of an early one. Falling back
    to investment is also what keeps every pre-v2 caller and stored row
    interpretable.
    """
    row = await _persist_and_read(engine, _verdict())

    assert row.funnel_stage is None
    assert row.weighted_score == pytest.approx(_expected(RUBRIC_WEIGHTS))
    assert row.band == "conditional"


@pytest.mark.asyncio
async def test_an_unrecognized_funnel_stage_falls_back_to_investment(engine):
    """Same rule for a stage nobody defined. The value comes from an LLM through
    a free-text field, so "something unexpected" is a real input, and it must
    not be treated as a licence to use the lower band lines."""
    row = await _persist_and_read(engine, _verdict(funnel_stage="wildcat"))

    assert row.weighted_score == pytest.approx(_expected(RUBRIC_WEIGHTS))
    assert row.band == "conditional"


@pytest.mark.asyncio
async def test_the_documents_own_incubation_spelling_is_recognized(engine):
    """The rubric's funnel section says "Incubation/Grant"; the sidecar skeleton
    says "incubation". Both are the same stage, and the model reads both in the
    same prompt — so the match is a case-insensitive PREFIX, not equality. A
    verdict that wrote the rubric's own wording back must not fall through to
    the investment scale."""
    row = await _persist_and_read(engine, _verdict(funnel_stage=" Incubation/Grant "))

    assert row.weighted_score == pytest.approx(_expected(RUBRIC_WEIGHTS_INCUBATION))
    assert row.band == "advance"


@pytest.mark.asyncio
async def test_the_score_and_the_band_never_come_from_different_scales(engine):
    """The failure mode a two-scale rubric invites: passing the stage to one of
    the two calls and not the other. Checked as a property rather than by
    reading the source — for both stages, the stored band must be the band the
    stored score falls in on the SAME scale.
    """
    from src.services.blackbird_rubric import band as rubric_band

    for stage in ("incubation", "seed", None):
        row = await _persist_and_read(engine, _verdict(funnel_stage=stage))
        assert row.band == rubric_band(row.weighted_score, stage), (
            f"stage {stage!r}: stored band {row.band!r} is not the band "
            f"{row.weighted_score} falls in on that scale"
        )
