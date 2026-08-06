import base64
import json
import re

import pytest
from itsdangerous import TimestampSigner
from sqlalchemy import select

from src.config import get_settings
from src.models import OpportunityAssessment, SimulationRun
from tests import factories

pytestmark = pytest.mark.integration


def _auth(user_id) -> dict:
    """Forge the signed session cookie SessionMiddleware would issue."""
    signer = TimestampSigner(get_settings().secret_key)
    data = base64.b64encode(json.dumps({"user_id": str(user_id)}).encode())
    return {"Cookie": f"copi-session={signer.sign(data).decode()}"}


@pytest.fixture
async def admin(db_session):
    return await factories.make_user(
        db_session, is_admin=True, email="assessments-admin@example.org"
    )


@pytest.mark.asyncio
async def test_assessment_row_round_trips(db_session):
    run = SimulationRun()
    db_session.add(run)
    await db_session.flush()

    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id,
        agent_id="blackbird",
        subject_agent_id="wang",
        channel_name="general",
        slack_ts="1754480000.000100",
        company_or_project="DBT / BCAA-autophagy axis",
        funnel_stage="incubation",
        recommendation="route-to-incubation",
        confidence="Speculative",
        weighted_score=3.05,
        band="conditional",
        gating={"baltimore_commitment": False, "life_sciences_domain": True,
                "credible_tech_source": True, "fto_achievable": False},
        scores={"differentiation": 4, "external_signals": 1},
        red_flags=["No external validation yet"],
        derisking_milestones=["TDP-43 mouse rescue"],
        rationale="Differentiated metabolic angle.",
        raw_verdict={"weighted_score": 0},
    ))
    await db_session.flush()

    row = (await db_session.execute(select(OpportunityAssessment))).scalar_one()
    assert row.subject_agent_id == "wang"
    assert row.weighted_score == pytest.approx(3.05)
    assert row.band == "conditional"
    assert row.gating["baltimore_commitment"] is False
    assert row.red_flags == ["No external validation yet"]
    assert row.created_at is not None


@pytest.mark.asyncio
async def test_nullable_columns_tolerate_a_sparse_verdict(db_session):
    run = SimulationRun()
    db_session.add(run)
    await db_session.flush()
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", channel_name="general",
    ))
    await db_session.flush()
    row = (await db_session.execute(select(OpportunityAssessment))).scalar_one()
    assert row.subject_agent_id is None
    assert row.weighted_score is None


@pytest.mark.asyncio
async def test_simulation_run_delete_cascades_to_opportunity_assessment(engine):
    """The suite's own cleanup pattern for every ``_persist_assessment`` test below
    is "delete the run, trust the FK cascade to take the assessment with it" — but
    that cascade is never itself exercised. There is no ORM ``relationship()``
    between SimulationRun and OpportunityAssessment (unlike AgentMessage/AgentChannel/
    LlmCallLog, which declare ``cascade="all, delete-orphan"``), so this is purely
    the database's ``ON DELETE CASCADE`` on the FK (migration 0025) — an ORM
    ``session.delete()`` with no relationship configured would NOT cascade on its
    own if the DB constraint were ever weakened or dropped."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        run = SimulationRun()
        db.add(run)
        await db.flush()
        run_id = run.id
        db.add(OpportunityAssessment(
            simulation_run_id=run_id, agent_id="blackbird", channel_name="general",
        ))
        await db.commit()

    async with factory() as db:
        before = (await db.execute(
            select(OpportunityAssessment).where(
                OpportunityAssessment.simulation_run_id == run_id
            )
        )).scalars().all()
        assert len(before) == 1

        run = (await db.execute(
            select(SimulationRun).where(SimulationRun.id == run_id)
        )).scalar_one()
        await db.delete(run)
        await db.commit()

    async with factory() as db:
        after = (await db.execute(
            select(OpportunityAssessment).where(
                OpportunityAssessment.simulation_run_id == run_id
            )
        )).scalars().all()
        assert after == []


@pytest.mark.asyncio
async def test_persist_assessment_recomputes_the_score_it_is_handed(engine):
    """The model is told to leave weighted_score at 0, and it will sometimes fill in
    a flattering number anyway. The stored score must be computed from its own
    dimension scores, with the original verdict kept verbatim in raw_verdict."""
    import uuid as _uuid
    from types import SimpleNamespace

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    stub = SimpleNamespace(simulation_run_id=run_id, session_factory=factory)
    await SimulationEngine._persist_assessment(stub, "blackbird", "general", {
        "subject_agent_id": "wang",
        "company_or_project": "DBT / BCAA-autophagy axis",
        "funnel_stage": "incubation",
        "recommendation": "route-to-incubation",
        "confidence": "Speculative",
        "weighted_score": 4.8,  # the model's inflated claim — must be ignored
        "scores": {
            "differentiation": 4, "market_unmet_need": 4, "team": 4,
            "external_signals": 1, "ip_fto": 2, "platform": 3,
            "dev_regulatory_feasibility": 3, "workplan_capital_efficiency": 3,
            "exit_thesis": 2,
        },
        # Tri-state strings — the current contract (see corrections to Task 11:
        # the prompt emits "met"/"not_met"/"unconfirmed", never booleans).
        "gating": {"baltimore_commitment": "unconfirmed", "life_sciences_domain": "met",
                   "credible_tech_source": "met", "fto_achievable": "not_met"},
        "red_flags": ["No external validation yet"],
        "suggested_derisking_milestones": ["TDP-43 mouse rescue"],
        "rationale": "Differentiated metabolic angle.",
    })

    try:
        async with factory() as check:
            row = (await check.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalar_one()
            assert row.weighted_score == pytest.approx(3.05)  # computed, not 4.8
            assert row.band == "conditional"
            assert row.subject_agent_id == "wang"
            assert row.agent_id == "blackbird"
            assert row.channel_name == "general"
            assert row.recommendation == "route-to-incubation"  # never derived from band()
            assert row.derisking_milestones == ["TDP-43 mouse rescue"]
            # Tri-state gating strings round-trip unchanged.
            assert row.gating == {
                "baltimore_commitment": "unconfirmed", "life_sciences_domain": "met",
                "credible_tech_source": "met", "fto_achievable": "not_met",
            }
            # The original verdict survives verbatim for audit.
            assert row.raw_verdict["weighted_score"] == 4.8
    finally:
        async with factory() as cleanup:
            stale = (await cleanup.execute(
                select(SimulationRun).where(SimulationRun.id == run_id)
            )).scalar_one_or_none()
            if stale is not None:
                await cleanup.delete(stale)  # cascades to the assessment
                await cleanup.commit()
    assert _uuid.UUID(str(run_id))  # run_id was a real uuid, not a stub artefact


@pytest.mark.asyncio
async def test_persist_assessment_gating_drops_only_the_invalid_key(engine):
    """The old contract wrote gating as booleans; the current one writes
    "met"/"not_met"/"unconfirmed" strings. Fix round 1, Finding 4: a map with
    three valid gates and one stray boolean must keep the three valid gates —
    dropping the whole map wholesale (the original Task 11 behavior) denied
    the triage page three gates it could have shown for no correctness
    benefit. The invalid key is still never guessed/coerced (a legacy False is
    genuinely ambiguous between not_met and unconfirmed), so it alone is
    omitted. The original four-key map is untouched in raw_verdict regardless."""
    from types import SimpleNamespace

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    stub = SimpleNamespace(simulation_run_id=run_id, session_factory=factory)
    original_gating = {
        "baltimore_commitment": False,  # legacy boolean — the one bad key
        "life_sciences_domain": "met",
        "credible_tech_source": "met",
        "fto_achievable": "not_met",
    }
    verdict = {
        "subject_agent_id": "wang",
        "scores": {"differentiation": 3},
        "gating": dict(original_gating),
    }
    await SimulationEngine._persist_assessment(stub, "blackbird", "general", verdict)

    try:
        async with factory() as check:
            row = (await check.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalar_one()
            assert row.gating == {
                "life_sciences_domain": "met",
                "credible_tech_source": "met",
                "fto_achievable": "not_met",
            }
            assert "baltimore_commitment" not in row.gating  # not guessed, omitted
            assert row.raw_verdict["gating"] == original_gating
    finally:
        async with factory() as cleanup:
            stale = (await cleanup.execute(
                select(SimulationRun).where(SimulationRun.id == run_id)
            )).scalar_one_or_none()
            if stale is not None:
                await cleanup.delete(stale)
                await cleanup.commit()


@pytest.mark.asyncio
async def test_persist_assessment_gating_that_is_not_a_dict_is_dropped(engine):
    """A `gating` value that isn't even a mapping (a string, in this case) must
    become None wholesale — there are no keys to filter — while raw_verdict
    still holds the original for audit."""
    from types import SimpleNamespace

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    stub = SimpleNamespace(simulation_run_id=run_id, session_factory=factory)
    verdict = {"subject_agent_id": "wang", "gating": "all four met, trust me"}
    await SimulationEngine._persist_assessment(stub, "blackbird", "general", verdict)

    try:
        async with factory() as check:
            row = (await check.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalar_one()
            assert row.gating is None
            assert row.raw_verdict["gating"] == "all four met, trust me"
    finally:
        async with factory() as cleanup:
            stale = (await cleanup.execute(
                select(SimulationRun).where(SimulationRun.id == run_id)
            )).scalar_one_or_none()
            if stale is not None:
                await cleanup.delete(stale)
                await cleanup.commit()


@pytest.mark.asyncio
async def test_persist_assessment_never_raises_when_the_write_fails(caplog):
    """Best-effort by contract: the Slack post has already gone out, so losing the
    DB row must never take down the turn."""
    import uuid as _uuid
    from types import SimpleNamespace

    from src.agent.simulation import SimulationEngine

    def _boom():
        raise RuntimeError("database is gone")

    stub = SimpleNamespace(simulation_run_id=_uuid.uuid4(), session_factory=_boom)
    await SimulationEngine._persist_assessment(
        stub, "blackbird", "general", {"scores": {}}
    )
    assert "Failed to persist assessment" in caplog.text


@pytest.mark.asyncio
async def test_persist_assessment_skips_quietly_without_a_database(caplog):
    """SimulationEngine can run with no database at all — session_factory and
    simulation_run_id are both None in that mode (see __init__). Persistence must
    be a silent no-op then, never an attempted write against a null session
    factory or a null simulation_run_id foreign key."""
    from types import SimpleNamespace

    from src.agent.simulation import SimulationEngine

    stub = SimpleNamespace(simulation_run_id=None, session_factory=None)
    with caplog.at_level("DEBUG"):
        await SimulationEngine._persist_assessment(
            stub, "blackbird", "general", {"scores": {"differentiation": 5}}
        )
    assert "Skipping assessment persistence" in caplog.text
    assert "Failed to persist assessment" not in caplog.text


@pytest.mark.asyncio
async def test_persist_assessment_tolerates_a_sparse_verdict(engine):
    """A partly-unparseable verdict must still be recorded — losing the assessment
    is strictly worse than storing an incomplete one."""
    from types import SimpleNamespace

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    stub = SimpleNamespace(simulation_run_id=run_id, session_factory=factory)
    # No scores, no gating, red_flags the wrong type entirely.
    await SimulationEngine._persist_assessment(
        stub, "blackbird", "general", {"red_flags": "not a list"}
    )

    try:
        async with factory() as check:
            row = (await check.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalar_one()
            # F6: no scores at all is "we don't know", not a decisive 0.00/pass
            # decline the model never made.
            assert row.weighted_score is None
            assert row.band is None
            assert row.red_flags is None  # wrong type discarded, not stored
            assert row.scores is None
    finally:
        async with factory() as cleanup:
            stale = (await cleanup.execute(
                select(SimulationRun).where(SimulationRun.id == run_id)
            )).scalar_one_or_none()
            if stale is not None:
                await cleanup.delete(stale)
                await cleanup.commit()


@pytest.mark.asyncio
async def test_persist_assessment_bounds_oversized_short_string_fields(engine):
    """Fix round 1, Finding 5: subject_agent_id/funnel_stage/recommendation/
    confidence are bounded VARCHAR columns. Every other field on this row
    degrades per-field on a bad *type*, but an oversized value of the *right*
    type (a plain str, just too long) sails past any isinstance check and
    raised DataError at commit — which the outer except then dropped the
    WHOLE row for. The fields must be truncated to fit before the insert, not
    left to blow up the write, and raw_verdict must still hold the untruncated
    original."""
    from types import SimpleNamespace

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    oversized_recommendation = "advance" + "!" * 200  # column is String(30)
    oversized_subject_agent_id = "w" * 200  # column is String(50)
    stub = SimpleNamespace(simulation_run_id=run_id, session_factory=factory)
    verdict = {
        "subject_agent_id": oversized_subject_agent_id,
        "recommendation": oversized_recommendation,
        "funnel_stage": "incubation",  # well within String(20) — unaffected
        "scores": {"differentiation": 3},
    }
    await SimulationEngine._persist_assessment(stub, "blackbird", "general", verdict)

    try:
        async with factory() as check:
            row = (await check.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalar_one()
            # The row exists at all — the oversized fields did not sink the write.
            assert len(row.subject_agent_id) == 50
            assert oversized_subject_agent_id.startswith(row.subject_agent_id)
            assert len(row.recommendation) == 30
            assert oversized_recommendation.startswith(row.recommendation)
            assert row.funnel_stage == "incubation"
            # The untruncated originals still survive verbatim for audit.
            assert row.raw_verdict["subject_agent_id"] == oversized_subject_agent_id
            assert row.raw_verdict["recommendation"] == oversized_recommendation
    finally:
        async with factory() as cleanup:
            stale = (await cleanup.execute(
                select(SimulationRun).where(SimulationRun.id == run_id)
            )).scalar_one_or_none()
            if stale is not None:
                await cleanup.delete(stale)
                await cleanup.commit()


@pytest.mark.asyncio
async def test_persist_assessment_empty_scores_dict_stores_null_score_and_band(engine):
    """F6: `weighted_score({})` is 0.0, which bands as "pass" — a real,
    decisive decline the model never made. An explicitly empty `scores` map
    (not just a missing key) must still store None/None, not 0.00/"pass",
    the same as the missing-key case covered by the sparse-verdict test
    above."""
    from types import SimpleNamespace

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    stub = SimpleNamespace(simulation_run_id=run_id, session_factory=factory)
    await SimulationEngine._persist_assessment(
        stub, "blackbird", "general", {"subject_agent_id": "wang", "scores": {}}
    )

    try:
        async with factory() as check:
            row = (await check.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalar_one()
            assert row.weighted_score is None
            assert row.band is None
            assert row.scores is None
            assert row.subject_agent_id == "wang"  # unaffected sibling field
    finally:
        async with factory() as cleanup:
            stale = (await cleanup.execute(
                select(SimulationRun).where(SimulationRun.id == run_id)
            )).scalar_one_or_none()
            if stale is not None:
                await cleanup.delete(stale)
                await cleanup.commit()


@pytest.mark.asyncio
async def test_persist_assessment_drops_non_string_text_fields_instead_of_dying(engine):
    """F9: company_or_project/rationale are Text columns guarded with only
    `or None`, not an isinstance check. A model emitting a structured (dict)
    rationale — or a company_or_project that comes back as a list — is a
    plain Python object of the wrong type for a str column; passing it
    straight to the ORM raises DataError at commit, which the outer except
    then drops the ENTIRE row for, losing everything else in the verdict too.
    Both fields must degrade to None per-field, the same as every other
    wrong-typed field on this row."""
    from types import SimpleNamespace

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    stub = SimpleNamespace(simulation_run_id=run_id, session_factory=factory)
    verdict = {
        "subject_agent_id": "wang",
        "company_or_project": ["DBT", "BCAA-autophagy axis"],  # wrong type
        "rationale": {"summary": "structured, not prose"},  # wrong type
        "scores": {"differentiation": 3},
    }
    await SimulationEngine._persist_assessment(stub, "blackbird", "general", verdict)

    try:
        async with factory() as check:
            row = (await check.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalar_one()
            # The row exists at all — the wrong-typed fields did not sink the write.
            assert row.subject_agent_id == "wang"
            assert row.company_or_project is None
            assert row.rationale is None
            # The untruncated originals still survive verbatim for audit.
            assert row.raw_verdict["company_or_project"] == ["DBT", "BCAA-autophagy axis"]
            assert row.raw_verdict["rationale"] == {"summary": "structured, not prose"}
    finally:
        async with factory() as cleanup:
            stale = (await cleanup.execute(
                select(SimulationRun).where(SimulationRun.id == run_id)
            )).scalar_one_or_none()
            if stale is not None:
                await cleanup.delete(stale)
                await cleanup.commit()


# --- Phase 5 wiring: the real "New top-level post" branch, not the
# _persist_assessment stub (Task 11 fix round 1, Finding 2) -----------------
#
# Every test above drives _persist_assessment directly on a SimpleNamespace
# stub, which bypasses the `if verdict is not None:` gate in
# SimulationEngine._phase5_new_post entirely — exactly why Finding 1 (a valid
# `{}` sidecar misrouted to the "verdict lost" branch) had zero coverage. These
# tests build a real SimulationEngine + Agent + FakeSlackClient and drive
# _phase5_new_post end-to-end against a canned LLM response, so the assertions
# exercise the actual wiring code, not a re-description of it.

async def _drive_phase5_new_post(engine, monkeypatch, response_text):
    """Build a real engine wired to the test DB and run _phase5_new_post
    against ``response_text`` as if it were the LLM's raw output. Returns
    (agent, client, factory, run_id) for the caller's own assertions/cleanup.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.agent import Agent
    from src.agent.simulation import SimulationEngine
    from tests.fakes import FakeSlackClient

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    agent = Agent("blackbird", "BlackbirdBot", "Blackbird")
    client = FakeSlackClient(agent_id="blackbird")
    sim = SimulationEngine(
        agents=[agent], slack_clients={"blackbird": client},
        session_factory=factory, simulation_run_id=run_id,
    )
    # Bypass real prompt construction (profile files on disk, etc.) — this
    # class tests what happens AFTER the LLM responds, not prompt building.
    monkeypatch.setattr(agent, "build_phase5_prompt", lambda **kw: ("sys", []))

    async def _fake_generate(**kwargs):
        return response_text

    monkeypatch.setattr("src.agent.simulation.generate_agent_response", _fake_generate)

    await sim._phase5_new_post(agent)
    return agent, client, factory, run_id


async def _assessment_rows(factory, run_id):
    async with factory() as check:
        return (await check.execute(
            select(OpportunityAssessment).where(
                OpportunityAssessment.simulation_run_id == run_id
            )
        )).scalars().all()


async def _delete_run(factory, run_id):
    async with factory() as cleanup:
        stale = (await cleanup.execute(
            select(SimulationRun).where(SimulationRun.id == run_id)
        )).scalar_one_or_none()
        if stale is not None:
            await cleanup.delete(stale)  # cascades to any assessment
            await cleanup.commit()


_ACTION_JSON = (
    '```json\n'
    '{"action": "new_post", "channel": "general", "post_type": "opportunity_assessment", '
    '"tagged_agent": null, "target_post_id": null}\n'
    '```\n\n'
)
_SLACK_BODY = (
    "<slack_message>\n"
    ":mag: *Opportunity Assessment — Wang Lab*\n"
    "Recommendation: proceed to diligence.\n"
    "</slack_message>"
)


@pytest.mark.asyncio
async def test_phase5_valid_sidecar_persists_a_row(engine, monkeypatch):
    response = (
        _ACTION_JSON + _SLACK_BODY + "\n\n"
        '<assessment_json>\n'
        '{"subject_agent_id": "wang", "recommendation": "advance", '
        '"scores": {"differentiation": 5}}\n'
        '</assessment_json>'
    )
    agent, client, factory, run_id = await _drive_phase5_new_post(engine, monkeypatch, response)
    try:
        assert len(client.posted) == 1  # the post really went out
        assert agent.message_count == 1
        rows = await _assessment_rows(factory, run_id)
        assert len(rows) == 1
        assert rows[0].subject_agent_id == "wang"
        assert rows[0].recommendation == "advance"
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_phase5_empty_sidecar_object_still_persists_a_row(engine, monkeypatch):
    """Finding 1: `{}` is a successfully parsed, if sparse, verdict — it must
    not be treated as "no sidecar" and silently discarded."""
    response = (
        _ACTION_JSON + _SLACK_BODY + "\n\n"
        "<assessment_json>\n{}\n</assessment_json>"
    )
    agent, client, factory, run_id = await _drive_phase5_new_post(engine, monkeypatch, response)
    try:
        assert len(client.posted) == 1
        rows = await _assessment_rows(factory, run_id)
        assert len(rows) == 1
        assert rows[0].raw_verdict == {}
        # F6: an empty `scores` map is "we don't know", not a decisive
        # 0.00/pass decline the model never made.
        assert rows[0].weighted_score is None
        assert rows[0].band is None
        # F7: the row links back to the Slack post it summarises.
        assert rows[0].slack_ts == client.posted[0]["ts"]
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_phase5_no_sidecar_persists_nothing_and_logs_its_absence(
    engine, monkeypatch, caplog
):
    response = _ACTION_JSON + _SLACK_BODY  # no <assessment_json> at all
    agent, client, factory, run_id = await _drive_phase5_new_post(engine, monkeypatch, response)
    try:
        assert len(client.posted) == 1  # the post itself still went out
        assert (await _assessment_rows(factory, run_id)) == []
        assert "had no <assessment_json> sidecar present" in caplog.text
        assert "unparseable" not in caplog.text  # must not claim the wrong failure
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_phase5_unparseable_sidecar_persists_nothing_and_names_the_failure(
    engine, monkeypatch, caplog
):
    response = (
        _ACTION_JSON + _SLACK_BODY + "\n\n"
        '<assessment_json>\n{this is not valid json}\n</assessment_json>'
    )
    agent, client, factory, run_id = await _drive_phase5_new_post(engine, monkeypatch, response)
    try:
        assert len(client.posted) == 1
        assert (await _assessment_rows(factory, run_id)) == []
        assert "sidecar was present but unparseable" in caplog.text
        assert "had no <assessment_json> sidecar present" not in caplog.text
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_phase5_suppressed_post_persists_nothing_and_does_not_count(
    engine, monkeypatch, caplog
):
    """Cross-task Finding 3: _post_message now suppresses a post that strips
    to nothing (e.g. the sidecar nested *inside* <slack_message>, leaving no
    real body once stripped). Before this fix the caller still counted the
    turn and — worse — still persisted an assessment row extracted from the
    raw response, for a post that never reached Slack."""
    response = (
        _ACTION_JSON +
        "<slack_message>"
        '<assessment_json>{"subject_agent_id": "wang", "scores": {"differentiation": 5}}'
        "</assessment_json>"
        "</slack_message>"
    )
    agent, client, factory, run_id = await _drive_phase5_new_post(engine, monkeypatch, response)
    try:
        assert client.posted == []  # nothing actually reached Slack
        assert agent.message_count == 0  # the turn was not counted
        assert (await _assessment_rows(factory, run_id)) == []  # no phantom row
        assert "Suppressed a post" in caplog.text
    finally:
        await _delete_run(factory, run_id)


# --- /admin/assessments (task 12) -------------------------------------------


def _band_label(html: str) -> str:
    """Pull the rendered text of the ``band-label`` span next to the score.

    ``band`` (computed server-side from the weighted score) and
    ``recommendation`` (the model's own call) can legitimately disagree, and
    that disagreement is the page's most valuable signal — so ``band`` must
    render as text a reader can actually read, not just as a font colour on
    the score. Matching the dedicated ``band-label`` class (rather than a
    bare substring check) also dodges the page's intro prose, which already
    contains the literal words "advance"/"conditional"/"pass" while
    describing the threshold bands in general.
    """
    match = re.search(r'<span class="band-label[^"]*"[^>]*>([^<]*)</span>', html)
    if match is None:
        raise AssertionError(f"no band-label span rendered in: {html}")
    return match.group(1).strip()


def _gating_state_for(html: str, label: str) -> str:
    """Pull the ``gating-<state>`` class rendered for a given gate's row.

    Matches the template's ``<div class="gating-row gating-{state}">...{label}
    </div>`` markup, so this fails loudly if a future template refactor drops
    the class rather than silently passing on unrelated markup.
    """
    for state, content in re.findall(
        r'<div class="gating-row gating-(\w+)">(.*?)</div>', html, re.S
    ):
        if label in content:
            return state
    raise AssertionError(f"no gating row rendered for label {label!r} in: {html}")


@pytest.mark.asyncio
async def test_admin_assessments_page_lists_verdicts(client, db_session, admin):
    run = SimulationRun()
    db_session.add(run)
    await db_session.flush()
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="wang",
        channel_name="general", company_or_project="DBT / BCAA-autophagy axis",
        funnel_stage="incubation", recommendation="route-to-incubation",
        confidence="Speculative", weighted_score=3.05, band="conditional",
        red_flags=["No external validation yet"],
    ))
    await db_session.flush()

    resp = await client.get("/admin/assessments", headers=_auth(admin.id))
    assert resp.status_code == 200
    assert "DBT / BCAA-autophagy axis" in resp.text
    assert "route-to-incubation" in resp.text
    assert "3.05" in resp.text
    assert "No external validation yet" in resp.text
    # band must be legible as text, not just a font colour on the score.
    assert _band_label(resp.text) == "conditional"


@pytest.mark.asyncio
async def test_admin_assessments_page_does_not_double_wrap_confidence(
    client, db_session, admin
):
    """F11: the prompt shows the confidence label bracketed in the Slack body
    text (*[High]*) but bare in the sidecar ("High"). If a model ever copies
    the bracketed body style into the sidecar field too, the template's own
    unconditional `[{{ a.confidence }}]` wrap used to double it up into
    "[[High]]". The fix strips any brackets the value already carries before
    re-wrapping, so the page always shows exactly one bracket pair regardless
    of which form the stored value took."""
    run = SimulationRun()
    db_session.add(run)
    await db_session.flush()
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="wang",
        channel_name="general", company_or_project="Bracketed confidence fixture",
        confidence="[High]",  # as if the model copied the body's bracketed style
    ))
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="fu",
        channel_name="general", company_or_project="Bare confidence fixture",
        confidence="Moderate",  # the documented, bare sidecar form
    ))
    await db_session.flush()

    resp = await client.get("/admin/assessments", headers=_auth(admin.id))
    assert resp.status_code == 200
    assert "[[High]]" not in resp.text
    assert "[High]" in resp.text
    assert "[Moderate]" in resp.text


@pytest.mark.asyncio
async def test_admin_assessments_page_renders_band_as_text_not_just_colour(
    client, db_session, admin
):
    """Regression for the finding that ``band`` only ever selected a font
    colour on the score cell (green/amber/gray) with no label and no
    ``title`` — so a colour-blind reader, or anyone who had scrolled past the
    legend, could not see a `recommendation`/`band` disagreement. This is
    the exact concrete failure case: the model recommends
    ``route-to-incubation`` but the server-computed band is ``pass``. Both
    must render as their own literal, readable text, and neither may be
    presented as the other.
    """
    run = SimulationRun()
    db_session.add(run)
    await db_session.flush()
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="wang",
        channel_name="general",
        company_or_project="Band/recommendation disagreement fixture",
        recommendation="route-to-incubation", weighted_score=1.80, band="pass",
    ))
    await db_session.flush()

    resp = await client.get("/admin/assessments", headers=_auth(admin.id))
    assert resp.status_code == 200
    html = resp.text
    assert "route-to-incubation" in html  # the model's own call, unchanged
    assert _band_label(html) == "pass"  # the computed band, legible as text
    # band and recommendation must never be presented as each other.
    assert _band_label(html) != "route-to-incubation"


@pytest.mark.asyncio
async def test_admin_assessments_page_requires_admin(client, db_session):
    plain = await factories.make_user(db_session, is_admin=False, email="plain-assess@example.org")
    await db_session.flush()
    resp = await client.get("/admin/assessments", headers=_auth(plain.id))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_assessments_page_distinguishes_gating_tri_state(client, db_session, admin):
    """``gating`` is a tri-state string (met/not_met/unconfirmed), not a boolean.

    A template that renders it as ``{% if ok %}...{% else %}...{% endif %}``
    would show "not_met" (the PI declined to commit) and "unconfirmed" (nobody
    asked) with the exact same glyph — collapsing the one distinction this
    column exists to preserve. This asserts all three states render three
    different ways, not just that the page returns 200.
    """
    run = SimulationRun()
    db_session.add(run)
    await db_session.flush()
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="wang",
        channel_name="general", company_or_project="Tri-state gating fixture",
        gating={
            "baltimore_commitment": "not_met",
            "life_sciences_domain": "met",
            "credible_tech_source": "met",
            "fto_achievable": "unconfirmed",
        },
    ))
    await db_session.flush()

    resp = await client.get("/admin/assessments", headers=_auth(admin.id))
    assert resp.status_code == 200
    html = resp.text

    met_state = _gating_state_for(html, "life sciences domain")
    not_met_state = _gating_state_for(html, "baltimore commitment")
    unconfirmed_state = _gating_state_for(html, "fto achievable")

    assert met_state == "met"
    assert not_met_state == "not_met"
    assert unconfirmed_state == "unconfirmed"
    # The whole point: three distinct states must not collapse to two renderings.
    assert len({met_state, not_met_state, unconfirmed_state}) == 3

    # And the glyphs themselves differ, not just the (invisible) CSS class.
    assert "&#9989;" in html  # met
    assert "&#10060;" in html  # not_met
    assert "&#10067;" in html  # unconfirmed


@pytest.mark.asyncio
async def test_admin_assessments_page_handles_null_and_unrecognized_gating(
    client, db_session, admin
):
    """A dropped gating map (``None``, per ``_normalize_gating``) and a stray
    unrecognized value must render without crashing the page — never a 500,
    never a literal "None"."""
    run = SimulationRun()
    db_session.add(run)
    await db_session.flush()
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="wang",
        channel_name="general", company_or_project="Null gating fixture",
        gating=None,
    ))
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="fu",
        channel_name="general", company_or_project="Unrecognized gating fixture",
        # Not a value _normalize_gating would ever persist, but the template
        # must not assume the column can only ever hold the three known states.
        gating={"baltimore_commitment": "sort-of"},
    ))
    await db_session.flush()

    resp = await client.get("/admin/assessments", headers=_auth(admin.id))
    assert resp.status_code == 200
    assert _gating_state_for(resp.text, "baltimore commitment") == "unknown"


def _score_cell(html: str, key: str) -> str:
    """The rendered detail-row cell for one rubric dimension.

    Scoped to the `score-<key>` class rather than searching the whole page: the
    dimension names also appear in the page's intro prose and in tooltips, so a
    bare substring check would pass against a template that renders no scores at
    all.
    """
    m = re.search(
        rf'<span class="score-{re.escape(key)}[^"]*"[^>]*>(.*?)</span>\s*</span>',
        html,
        re.DOTALL,
    )
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip() if m else ""


@pytest.mark.asyncio
async def test_admin_assessments_page_renders_rationale_and_scores(
    client, db_session, admin
):
    """A triage row without the basis for its number is a scoreboard, not a
    triage tool. The reviewer has to be able to read WHY, and has to be able to
    tell a dimension that scored low from one that was never answered — an
    unscored dimension counts as zero in the weighted score, so the two are very
    different findings that produce the same digit.
    """
    run = SimulationRun()
    db_session.add(run)
    await db_session.flush()
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="wang",
        channel_name="general", company_or_project="DBT / BCAA-autophagy axis",
        weighted_score=3.05, band="conditional",
        rationale="Differentiated metabolic angle; needs mammalian in vivo rescue.",
        # external_signals deliberately omitted — must render as a gap, not a 0.
        scores={
            "differentiation": 4, "market_unmet_need": 4, "team": 4,
            "ip_fto": 2, "platform": 3, "dev_regulatory_feasibility": 3,
            "workplan_capital_efficiency": 3, "exit_thesis": 2,
        },
    ))
    await db_session.flush()

    resp = await client.get("/admin/assessments", headers=_auth(admin.id))
    assert resp.status_code == 200
    html = resp.text

    assert "Differentiated metabolic angle" in html, "rationale not rendered"

    # Scored dimensions carry their value and their weight.
    assert _score_cell(html, "differentiation") == "differentiation 4 /20%"
    assert _score_cell(html, "exit_thesis") == "exit thesis 2 /5%"

    # The omitted dimension is still listed, as a gap rather than a zero.
    assert _score_cell(html, "external_signals") == "external signals — /15%"

    # All nine appear, in descending weight order, so the dimensions that move
    # the score read first.
    order = [
        m.group(1)
        for m in re.finditer(r'<span class="score-([a-z_]+)', html)
    ]
    assert order == [
        "differentiation", "market_unmet_need", "team", "external_signals",
        "ip_fto", "platform", "dev_regulatory_feasibility",
        "workplan_capital_efficiency", "exit_thesis",
    ], order


@pytest.mark.asyncio
async def test_admin_assessments_page_omits_detail_row_when_empty(
    client, db_session, admin
):
    """A sparse verdict is stored deliberately rather than lost, so the page must
    tolerate one without rendering an empty grey band under it."""
    run = SimulationRun()
    db_session.add(run)
    await db_session.flush()
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", channel_name="general",
        company_or_project="Sparse verdict",
    ))
    await db_session.flush()

    resp = await client.get("/admin/assessments", headers=_auth(admin.id))
    assert resp.status_code == 200
    assert "Sparse verdict" in resp.text
    assert "assessment-detail" not in resp.text
    assert "assessment-rationale" not in resp.text
