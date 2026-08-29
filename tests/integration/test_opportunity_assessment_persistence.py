import base64
import json
import re

import pytest
from itsdangerous import TimestampSigner
from sqlalchemy import select

from src.config import get_settings
from src.models import USER_ROLE_ADMIN, USER_ROLE_PI, OpportunityAssessment, SimulationRun
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
        db_session, user_role=USER_ROLE_ADMIN, email="assessments-admin@example.org"
    )


@pytest.fixture(autouse=True)
async def _reset_assessment_tables(engine):
    """Start every test in this module with these three tables empty.

    `db_session`-based tests are already isolated (rolled back per test), but
    the many tests here that take `engine` directly commit for real, and two
    of them — test_a_gapped_verdict_is_stored_and_flagged_not_discarded and
    test_a_complete_panel_stores_an_unflagged_row — intentionally query the
    WHOLE table with no `simulation_run_id` filter (that is the point: a
    gapped verdict must be the only row, not merely present among others).
    A row left behind by an earlier `engine`-based test that forgot its own
    cleanup would silently corrupt exactly those two. Wiping here, before each
    test runs, makes that guarantee independent of every other test's
    bookkeeping.
    """
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.models import AssessmentDrop

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        await db.execute(delete(OpportunityAssessment))
        await db.execute(delete(AssessmentDrop))
        await db.execute(delete(SimulationRun))
        await db.commit()
    yield


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
        # Tri-state strings — the shipped contract (see src/services/blackbird_rubric.py
        # and _normalize_gating): "not_met" (the PI declined) and "unconfirmed" (nobody
        # asked) are different answers, never a boolean.
        gating={"baltimore_commitment": "not_met", "life_sciences_domain": "met",
                "credible_science": "met", "translational_potential": "unconfirmed"},
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
    assert row.gating == {
        "baltimore_commitment": "not_met", "life_sciences_domain": "met",
        "credible_science": "met", "translational_potential": "unconfirmed",
    }
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
    """Since rubric v3.2.0 the sidecar has no weighted_score key at all, but a
    model (or an old image) may still emit one — a stray, flattering number
    that must be ignored. The stored score must be computed from the verdict's
    own dimension scores, with the original verdict kept verbatim in
    raw_verdict. (`suggested_derisking_milestones` left the contract in the
    same revision; the engine still stores a stray one as passthrough, which
    this fixture also covers.)"""
    import uuid as _uuid

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    # A real (if agent-less) SimulationEngine rather than a bare SimpleNamespace:
    # _persist_assessment's specialist-floor gate calls self._specialist_floor_gap,
    # which in turn needs self._consulted_domains/self._specialist_consults/
    # self._specialist_consults — attributes a SimpleNamespace stub does not carry.
    stub = SimulationEngine(
        agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
    )
    await SimulationEngine._persist_assessment(stub, "blackbird", "general", {
        "subject_agent_id": "wang",
        "company_or_project": "DBT / BCAA-autophagy axis",
        "funnel_stage": "incubation",
        "recommendation": "route-to-incubation",
        "confidence": "Speculative",
        "weighted_score": 4.8,  # the model's inflated claim — must be ignored
        "scores": {
            "differentiation_unmet_need": 4, "scientific_credibility": 3,
            "translational_path": 3, "fundable_experiment": 4,
            "venture_potential": 2, "team_executability": 4,
        },
        # Tri-state strings — the current contract (see corrections to Task 11:
        # the prompt emits "met"/"not_met"/"unconfirmed", never booleans).
        "gating": {"life_sciences_domain": "met",
                   "credible_science": "met", "translational_potential": "not_met"},
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
            # Hand-computed under the six-dimension rubric (see
            # test_a_real_verdict_scores_as_hand_computed in
            # tests/unit/test_blackbird_rubric.py for the same fixture and
            # math: 100 + 60 + 45 + 60 + 30 + 40 = 335 / 100). The claim under
            # test: the stored score is COMPUTED, never the model's flattering
            # 4.8.
            assert row.weighted_score == pytest.approx(3.35)  # computed, not 4.8
            assert row.band == "conditional"
            assert row.subject_agent_id == "wang"
            assert row.agent_id == "blackbird"
            assert row.channel_name == "general"
            assert row.recommendation == "route-to-incubation"  # never derived from band()
            assert row.derisking_milestones == ["TDP-43 mouse rescue"]
            # Tri-state gating strings round-trip unchanged.
            assert row.gating == {
                "life_sciences_domain": "met",
                "credible_science": "met", "translational_potential": "not_met",
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

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    # A real (if agent-less) SimulationEngine rather than a bare SimpleNamespace:
    # _persist_assessment's specialist-floor gate calls self._specialist_floor_gap,
    # which in turn needs self._consulted_domains/self._specialist_consults/
    # self._specialist_consults — attributes a SimpleNamespace stub does not carry.
    stub = SimulationEngine(
        agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
    )
    original_gating = {
        "baltimore_commitment": False,  # legacy boolean — the one bad key
        "life_sciences_domain": "met",
        "credible_science": "met",
        "translational_potential": "not_met",
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
                "credible_science": "met",
                "translational_potential": "not_met",
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

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    # A real (if agent-less) SimulationEngine rather than a bare SimpleNamespace:
    # _persist_assessment's specialist-floor gate calls self._specialist_floor_gap,
    # which in turn needs self._consulted_domains/self._specialist_consults/
    # self._specialist_consults — attributes a SimpleNamespace stub does not carry.
    stub = SimulationEngine(
        agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
    )
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

    from src.agent.simulation import SimulationEngine

    def _boom():
        raise RuntimeError("database is gone")

    stub = SimulationEngine(
        agents=[], slack_clients={}, session_factory=_boom,
        simulation_run_id=_uuid.uuid4(),
    )
    await SimulationEngine._persist_assessment(
        stub, "blackbird", "general", {"scores": {}}
    )
    assert "Failed to persist assessment" in caplog.text


@pytest.mark.asyncio
async def test_persist_assessment_failure_is_buffered_and_a_later_flush_persists_it(engine):
    """Task 2 fix round 1, Finding 1: a write that fails on its first attempt
    (e.g. the pool-checkout timeout Task 2 sized the pool for) must not be
    logged-and-dropped — the fully-built row must survive in
    ``_pending_assessments`` and actually reach ``opportunity_assessments`` on
    the next ``_flush_pending_assessments``, the same durability contract
    ``_flush_persisted`` already gives the message log. Discriminating test:
    this asserts the row is really in the DB after the retry, not merely that
    a list became non-empty (a bug that only clears/repopulates the list
    would pass a weaker assertion)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    real_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with real_factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    # Fails exactly once (the "pool checkout timed out" moment), then behaves
    # normally — so one engine instance can prove both halves of the
    # contract: the failed first attempt is queued, and a later flush against
    # a now-healthy pool actually persists it.
    calls = {"n": 0}

    def flaky_factory():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("pool checkout timed out")
        return real_factory()

    # A real (if agent-less) SimulationEngine, not a bare SimpleNamespace —
    # same reason as the neighboring tests above: _specialist_floor_gap needs
    # real engine attributes a stub wouldn't carry.
    stub = SimulationEngine(
        agents=[], slack_clients={},
        session_factory=flaky_factory, simulation_run_id=run_id,
    )

    # The flaky factory fails the FIRST session of the call, and this test needs
    # that session to be the WRITE. `_persist_assessment` also opens one earlier,
    # for `_seed_consults_from_db`'s read-back — which used to skip a verdict
    # with no `recommendation` (the old recommendation-only gate, since deleted) and
    # now runs for it, because an unreadable recommendation fails CLOSED and IS
    # owed a panel. That read would swallow the one scheduled failure and the
    # write would succeed, so this test would pass its own premise by accident.
    #
    # Stubbed out rather than dodged by choosing an exempt verdict: the claim
    # under test is about the retry queue, and it should not silently depend on
    # how many sessions an unrelated fallback happens to open.
    async def _no_seed(*_args, **_kwargs):
        return None

    stub._seed_consults_from_db = _no_seed

    # No `recommendation` key: a sparse verdict, which is exactly the kind whose
    # write is worth not losing.
    verdict = {"subject_agent_id": "wang", "scores": {"differentiation": 3}}

    await SimulationEngine._persist_assessment(stub, "blackbird", "general", verdict)

    try:
        # First attempt failed: nothing in the DB yet, but the row survived
        # as a queued retry rather than vanishing.
        assert len(stub._pending_assessments) == 1
        async with real_factory() as check:
            none_yet = (await check.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalars().all()
            assert none_yet == []

        await SimulationEngine._flush_pending_assessments(stub)

        # The retry succeeded against the now-healthy factory: the buffer
        # drained AND the row genuinely landed in the table.
        assert stub._pending_assessments == []
        async with real_factory() as check:
            row = (await check.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalar_one()
            assert row.agent_id == "blackbird"
            assert row.subject_agent_id == "wang"
    finally:
        async with real_factory() as cleanup:
            stale = (await cleanup.execute(
                select(SimulationRun).where(SimulationRun.id == run_id)
            )).scalar_one_or_none()
            if stale is not None:
                await cleanup.delete(stale)  # cascades to the assessment
                await cleanup.commit()


@pytest.mark.asyncio
async def test_persist_assessment_skips_quietly_without_a_database(caplog):
    """SimulationEngine can run with no database at all — session_factory and
    simulation_run_id are both None in that mode (see __init__). Persistence must
    be a silent no-op then, never an attempted write against a null session
    factory or a null simulation_run_id foreign key."""

    from src.agent.simulation import SimulationEngine

    stub = SimulationEngine(agents=[], slack_clients={}, session_factory=None, simulation_run_id=None)
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

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    # A real (if agent-less) SimulationEngine rather than a bare SimpleNamespace:
    # _persist_assessment's specialist-floor gate calls self._specialist_floor_gap,
    # which in turn needs self._consulted_domains/self._specialist_consults/
    # self._specialist_consults — attributes a SimpleNamespace stub does not carry.
    stub = SimulationEngine(
        agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
    )
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
    # A real (if agent-less) SimulationEngine rather than a bare SimpleNamespace:
    # _persist_assessment's specialist-floor gate calls self._specialist_floor_gap,
    # which in turn needs self._consulted_domains/self._specialist_consults/
    # self._specialist_consults — attributes a SimpleNamespace stub does not carry.
    stub = SimulationEngine(
        agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
    )
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

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    # A real (if agent-less) SimulationEngine rather than a bare SimpleNamespace:
    # _persist_assessment's specialist-floor gate calls self._specialist_floor_gap,
    # which in turn needs self._consulted_domains/self._specialist_consults/
    # self._specialist_consults — attributes a SimpleNamespace stub does not carry.
    stub = SimulationEngine(
        agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
    )
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

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    # A real (if agent-less) SimulationEngine rather than a bare SimpleNamespace:
    # _persist_assessment's specialist-floor gate calls self._specialist_floor_gap,
    # which in turn needs self._consulted_domains/self._specialist_consults/
    # self._specialist_consults — attributes a SimpleNamespace stub does not carry.
    stub = SimulationEngine(
        agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
    )
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


@pytest.mark.asyncio
async def test_persist_assessment_stores_the_recommended_next_experiment(engine):
    """Item 10 of the sidecar contract: the single experiment Blackbird should
    fund next is a first-class column staff can read on the detail pages, not
    something they dig out of raw_verdict. A non-string value degrades to None
    per-field, like its Text siblings company_or_project and rationale."""

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    stub = SimulationEngine(
        agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
    )
    await SimulationEngine._persist_assessment(stub, "blackbird", "general", {
        "subject_agent_id": "wang",
        "recommendation": "advance",
        "recommended_next_experiment": (
            "EXPERIMENT-MARKER: 384-well selectivity panel; pass = >30-fold margin"
        ),
    })
    await SimulationEngine._persist_assessment(stub, "blackbird", "general", {
        "subject_agent_id": "hart",
        "recommendation": "advance",
        "recommended_next_experiment": {"summary": "structured, not prose"},
    })

    try:
        async with factory() as check:
            rows = (await check.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalars().all()
            by_subject = {r.subject_agent_id: r for r in rows}
            assert by_subject["wang"].recommended_next_experiment == (
                "EXPERIMENT-MARKER: 384-well selectivity panel; pass = >30-fold margin"
            )
            # Wrong type degrades to None without sinking the row; the original
            # survives verbatim in raw_verdict for audit.
            assert by_subject["hart"].recommended_next_experiment is None
            assert by_subject["hart"].raw_verdict["recommended_next_experiment"] == {
                "summary": "structured, not prose"
            }
    finally:
        async with factory() as cleanup:
            stale = (await cleanup.execute(
                select(SimulationRun).where(SimulationRun.id == run_id)
            )).scalar_one_or_none()
            if stale is not None:
                await cleanup.delete(stale)
                await cleanup.commit()


async def test_persist_assessment_stamps_prose_format_markdown(engine):
    """0040: every NEW row is stamped `prose_format="markdown"` at write time —
    the stamp ships together with the phase4 prompt instruction to write
    `rationale`/`recommended_next_experiment` as Markdown, and it is what
    gates rendering on the read path (never re-derived)."""

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    stub = SimulationEngine(
        agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
    )
    await SimulationEngine._persist_assessment(stub, "blackbird", "general", {
        "subject_agent_id": "wang",
        "recommendation": "advance",
        "rationale": "**Bold** rationale.",
    })

    try:
        async with factory() as check:
            row = (await check.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalar_one()
            assert row.prose_format == "markdown"
    finally:
        async with factory() as cleanup:
            stale = (await cleanup.execute(
                select(SimulationRun).where(SimulationRun.id == run_id)
            )).scalar_one_or_none()
            if stale is not None:
                await cleanup.delete(stale)
                await cleanup.commit()


# --- Phase 4 wiring: the real Option A relocation, not the
# _persist_assessment stub (Task 11 fix round 1, Finding 2; relocated by the
# reply-only-hub reconciliation, Task 6) -------------------------------------
#
# The hub's :mag: Opportunity Assessment is no longer a Phase-5 "New
# top-level post" at all — Option A extracts the `<assessment_json>` sidecar
# from the hub's own Phase-4 CONCLUDE reply instead (see simulation.py's
# `_reply_to_thread`/`_capture_hub_assessment`; `_phase5_new_post` hard-gates
# `scout_hub` out before doing any work whatsoever, per decision 9). These
# tests build a real SimulationEngine + Agent + ThreadState + FakeSlackClient
# and drive `_reply_to_thread` end-to-end against a canned LLM response, so
# the assertions exercise the actual wiring code, not a re-description of it.

async def _drive_reply_to_thread(
    engine, monkeypatch, raw_response, *, other_agent_id="wang",
    initial_floor_armed=False, pre_consults=(), on_generate=None,
    prior_messages=11,
):
    """Build a real engine wired to the test DB and run `_reply_to_thread`
    for a scout_hub agent against ``raw_response`` as if it were the LLM's
    raw output (everything, including any `<assessment_json>` sidecar — not
    just the `<slack_message>` body). Returns
    (agent, thread, client, factory, run_id) for the caller's own
    assertions/cleanup.

    ``initial_floor_armed`` seeds ``ThreadState.floor_armed`` — defaulting to
    ``False``, matching every real activation site when the consult map is
    still globally empty (and, permanently, every thread `_rebuild_agent_state`
    restores after a restart).

    ``pre_consults``, an iterable of ``(pi_agent_id, domain)`` pairs, is
    recorded on the engine's live ``_specialist_consults`` map, keyed under
    THIS thread's own id (``"t1"``), BEFORE `_reply_to_thread` runs —
    simulating consults recorded on an earlier turn of this same interview,
    i.e. before this turn's own top-of-turn latch reads the map. A pair naming
    a different PI than the thread's subject still works as "some other
    interview's consult" for tests that only care about the global map's
    emptiness (``floor_armed``), since the join key's PI half will not match
    this thread's subject regardless of which thread id the pair is keyed
    under.

    ``prior_messages`` is how many messages the thread ALREADY holds, seeded
    into the engine's real ``MessageLog``. It defaults to 11, which makes this
    turn's reply the 12th — the interview's CONCLUDE turn, the only turn whose
    guidance asks for an ``<assessment_json>`` sidecar and therefore the only
    one `_capture_hub_assessment` will persist one from.
    ``ThreadState.message_count`` cannot carry this on its own: `_reply_to_thread`
    overwrites it with ``len(get_thread_history(thread_id))`` before computing the
    phase, so a ThreadState built with ``message_count=11`` over an EMPTY log was
    silently an ordinal-1 EXPLORE turn. Every test in this class was written
    against that, and none of them was exercising the concluding turn it named.

    ``on_generate``, if given, is called (with the engine) from INSIDE the
    faked ``generate_with_tools`` — i.e. AFTER `_reply_to_thread`'s
    top-of-turn latch has already run and captured `thread.floor_armed`, but
    BEFORE the verdict is extracted and persisted. This simulates a
    *different* interview's consult landing concurrently, mid-turn (the
    original race), without needing real concurrent tasks.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.agent import Agent
    from src.agent.message_log import LogEntry
    from src.agent.simulation import SimulationEngine
    from src.agent.state import ThreadState
    from tests.fakes import FakeSlackClient

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    agent = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    thread = ThreadState(
        thread_id="t1", channel="general", other_agent_id=other_agent_id,
        message_count=prior_messages, has_pending_reply=True,
        floor_armed=initial_floor_armed,
    )
    agent.state.active_threads["t1"] = thread
    client = FakeSlackClient(agent_id="blackbird")
    sim = SimulationEngine(
        agents=[agent], slack_clients={"blackbird": client},
        session_factory=factory, simulation_run_id=run_id,
    )
    # The thread's existing messages, in the real log `_reply_to_thread` reads
    # the phase ordinal from. The root is "t1" itself so `get_thread_history`
    # pins it first; the rest alternate PI and hub the way an interview does.
    for i in range(prior_messages):
        ts = "t1" if i == 0 else f"t1.{i}"
        sim.message_log.append(LogEntry(
            ts=ts,
            channel="general",
            sender_agent_id=other_agent_id if i % 2 == 0 else "blackbird",
            sender_name="WangBot" if i % 2 == 0 else "BlackbirdBot",
            content=f"prior interview message {i}",
            thread_ts=None if i == 0 else "t1",
            posted_at=float(i),
            # slack_ts == ts is pure-Slack-on mode. Without it the seeded root has
            # no Slack presence, `_slack_parent_ts` returns None, and the reply is
            # kept DB-only instead of mirrored — which would silently stop these
            # tests asserting on `client.posted` from seeing anything at all.
            slack_ts=ts,
            slack_channel_id="C_GENERAL",
        ))
    for pi, domain in pre_consults:
        sim._record_consult(pi, domain, thread.thread_id)
    # Bypass real prompt construction (profile files on disk, etc.) — this
    # class tests what happens AFTER the LLM responds, not prompt building.
    monkeypatch.setattr(agent, "build_phase4_prompt", lambda **kw: ("sys", []))

    async def _fake_generate_with_tools(**kwargs):
        if on_generate is not None:
            on_generate(sim)
        return raw_response

    monkeypatch.setattr(
        "src.agent.simulation.generate_with_tools", _fake_generate_with_tools
    )

    await sim._reply_to_thread(agent, thread)
    return agent, thread, client, factory, run_id


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


# The sidecar lives OUTSIDE <slack_message> by design (phase4-thread-reply.md's
# "Concluding with an Opportunity Assessment" section) — this body carries no
# hint of it at all, unlike the old Phase-5 fixture that concatenated a
# separate action-JSON block ahead of it (Phase 4 has no action envelope).
_SLACK_BODY = (
    "<slack_message>\n"
    ":mag: Closing note — thanks for walking me through this. "
    "Recommendation: proceed to diligence.\n"
    "</slack_message>"
)


@pytest.mark.asyncio
async def test_reply_valid_sidecar_persists_a_row_and_the_post_is_stripped(
    engine, monkeypatch,
):
    """The mission pin: a hub concluding reply carrying a sidecar produces
    an OpportunityAssessment row AND the posted Slack text never contains
    the sidecar — Option A's whole premise in one test."""
    response = (
        _SLACK_BODY + "\n\n"
        '<assessment_json>\n'
        '{"subject_agent_id": "wang", "recommendation": "advance", '
        '"scores": {"differentiation": 5}}\n'
        '</assessment_json>'
    )
    agent, thread, client, factory, run_id = await _drive_reply_to_thread(
        engine, monkeypatch, response,
    )
    try:
        assert len(client.posted) == 1  # the reply really went out
        assert agent.message_count == 1
        posted_text = client.posted[0]["text"]
        assert posted_text == (
            ":mag: Closing note — thanks for walking me through this. "
            "Recommendation: proceed to diligence."
        )
        for leaked in ("assessment_json", "subject_agent_id", "differentiation"):
            assert leaked not in posted_text, f"sidecar leaked into Slack: {leaked!r}"
        rows = await _assessment_rows(factory, run_id)
        assert len(rows) == 1
        assert rows[0].subject_agent_id == "wang"
        assert rows[0].recommendation == "advance"
        assert rows[0].slack_ts == client.posted[0]["ts"]
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_reply_sidecar_missing_subject_id_falls_back_to_the_thread(
    engine, monkeypatch,
):
    """Unlike Phase 5's old standalone post (no thread to infer a subject
    from), a Phase-4 CONCLUDE reply always has a real interview thread
    behind it — the PI being screened is exactly `thread.other_agent_id`.
    A sidecar that leaves `subject_agent_id` blank must not lose the row
    over a field the engine already knows the answer to."""
    response = (
        _SLACK_BODY + "\n\n"
        '<assessment_json>\n'
        '{"recommendation": "pass", "scores": {"differentiation": 2}}\n'
        '</assessment_json>'
    )
    agent, thread, client, factory, run_id = await _drive_reply_to_thread(
        engine, monkeypatch, response, other_agent_id="wang",
    )
    try:
        rows = await _assessment_rows(factory, run_id)
        assert len(rows) == 1
        assert rows[0].subject_agent_id == "wang"
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_reply_empty_sidecar_object_still_persists_a_row(engine, monkeypatch):
    """Finding 1 (ported): `{}` is a successfully parsed, if sparse, verdict
    — it must not be treated as "no sidecar" and silently discarded."""
    response = (
        _SLACK_BODY + "\n\n"
        "<assessment_json>\n{}\n</assessment_json>"
    )
    agent, thread, client, factory, run_id = await _drive_reply_to_thread(
        engine, monkeypatch, response,
    )
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
async def test_reply_unscored_sidecar_logs_success_not_a_false_failure(
    engine, monkeypatch, caplog
):
    """A verdict with no `scores` key legitimately leaves `computed_score`/
    `computed_band` as None (F6 — see `test_persist_assessment_empty_scores_dict_
    stores_null_score_and_band` above). `_persist_assessment`'s success log line
    formatted that None with `%.2f`, which raises TypeError *after* the row is
    already committed; the outer `except` then logs "Failed to persist
    assessment" for a write that actually succeeded — a false failure that looks
    like data loss for every unscored verdict, which is most of them."""
    response = (
        _SLACK_BODY + "\n\n"
        '<assessment_json>\n'
        '{"subject_agent_id": "wang", "recommendation": "advance"}\n'
        '</assessment_json>'
    )
    with caplog.at_level("INFO"):
        agent, thread, client, factory, run_id = await _drive_reply_to_thread(
            engine, monkeypatch, response,
        )
    try:
        assert len(client.posted) == 1
        rows = await _assessment_rows(factory, run_id)
        assert len(rows) == 1  # the row really was written
        assert rows[0].weighted_score is None
        assert rows[0].band is None
        assert "Assessment stored" in caplog.text
        assert "Failed to persist assessment" not in caplog.text
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_reply_no_sidecar_persists_nothing_and_is_silent_about_it(
    engine, monkeypatch, caplog
):
    """Deliberate behaviour change from the ported Phase-5 test this
    replaces (`test_phase5_no_sidecar_persists_nothing_and_logs_its_absence`):
    Phase 5 only ever reached this code after the model explicitly declared
    `post_type: "opportunity_assessment"`, so an absent sidecar there was a
    genuine anomaly worth a WARNING every time. Every Phase-4 reply runs
    through `_capture_hub_assessment` regardless of whether it is the
    interview's concluding turn, and a sidecar is the exception (at most 1
    of ~12 messages), not the rule — logging "no sidecar" on every ordinary
    interview turn would be pure noise. See `_capture_hub_assessment`'s
    docstring for the full rationale.

    `prior_messages=5` makes this an ordinary DECIDE turn (ordinal 6), which is
    what "every ordinary interview turn" means and what this test always meant to
    cover. It used to get there by accident: the harness left the message log
    empty, so every test in this class ran at ordinal 1 whatever its ThreadState
    said. At a real CONCLUDE turn a missing sidecar IS an anomaly and
    `_warn_if_hub_conclude_missing_assessment` says so — covered by the
    "absent-sidecar detection gap" section of tests/unit/test_simulation_logic.py,
    not here."""
    response = _SLACK_BODY  # no <assessment_json> at all — the ordinary case
    with caplog.at_level("WARNING"):
        agent, thread, client, factory, run_id = await _drive_reply_to_thread(
            engine, monkeypatch, response, prior_messages=5,
        )
    try:
        assert len(client.posted) == 1  # the reply itself still went out
        assert (await _assessment_rows(factory, run_id)) == []
        assert "assessment" not in caplog.text.lower()
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_reply_unparseable_sidecar_persists_nothing_and_names_the_failure(
    engine, monkeypatch, caplog
):
    response = (
        _SLACK_BODY + "\n\n"
        '<assessment_json>\n{this is not valid json}\n</assessment_json>'
    )
    agent, thread, client, factory, run_id = await _drive_reply_to_thread(
        engine, monkeypatch, response,
    )
    try:
        assert len(client.posted) == 1
        assert (await _assessment_rows(factory, run_id)) == []
        assert "sidecar was present but unparseable" in caplog.text
        assert "had no <assessment_json> sidecar present" not in caplog.text
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_reply_non_object_sidecar_persists_nothing_and_names_the_right_failure(
    engine, monkeypatch, caplog
):
    """Finding A3 (ported): a sidecar that parsed as valid JSON but wasn't an
    object (e.g. a bare array) is a real parse — the wrong shape, not
    "unparseable". Misreporting the two failure modes under the same message
    makes them indistinguishable in logs."""
    response = (
        _SLACK_BODY + "\n\n"
        '<assessment_json>\n[1, 2, 3]\n</assessment_json>'
    )
    agent, thread, client, factory, run_id = await _drive_reply_to_thread(
        engine, monkeypatch, response,
    )
    try:
        assert len(client.posted) == 1
        assert (await _assessment_rows(factory, run_id)) == []
        assert "parsed as valid JSON but was not an object" in caplog.text
        assert "sidecar was present but unparseable" not in caplog.text
        assert "had no <assessment_json> sidecar present" not in caplog.text
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_reply_malformed_sidecar_still_posts_no_row_error_logged(
    engine, monkeypatch, caplog
):
    """Mission pin (d): a malformed sidecar must not cost the reply that
    already posted — the reply still reaches Slack, no row is written, and
    the failure is logged (not silently swallowed, and not raised out of
    `_reply_to_thread` to crash the turn)."""
    response = (
        _SLACK_BODY + "\n\n"
        '<assessment_json>\nnot even close to json\n</assessment_json>'
    )
    with caplog.at_level("WARNING"):
        agent, thread, client, factory, run_id = await _drive_reply_to_thread(
            engine, monkeypatch, response,
        )
    try:
        assert len(client.posted) == 1  # the reply still posted
        assert agent.message_count == 1
        assert (await _assessment_rows(factory, run_id)) == []  # no row
        assert "sidecar was present but unparseable" in caplog.text  # logged
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_reply_suppressed_post_persists_nothing_and_does_not_count(
    engine, monkeypatch, caplog
):
    """Cross-task Finding 3 (ported): `_post_message` suppresses a reply that
    strips to nothing (e.g. the sidecar nested *inside* `<slack_message>`,
    leaving no real body once stripped). The turn must not be counted and —
    Option A's own guarantee — no assessment row may be persisted for a
    reply that never reached Slack."""
    caplog.set_level("INFO")
    response = (
        "<slack_message>"
        '<assessment_json>{"subject_agent_id": "wang", "scores": {"differentiation": 5}}'
        "</assessment_json>"
        "</slack_message>"
    )
    agent, thread, client, factory, run_id = await _drive_reply_to_thread(
        engine, monkeypatch, response,
    )
    try:
        assert client.posted == []  # nothing actually reached Slack
        assert agent.message_count == 0  # the turn was not counted
        assert (await _assessment_rows(factory, run_id)) == []  # no phantom row
        assert "suppressed" in caplog.text.lower()
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
    # Red flags surface as a count badge only — the flag text itself is
    # detail-page content since the inline detail rows were removed 2026-08-27
    # (test_admin_assessments_page_renders_no_inline_detail_rows).
    assert "1 flag" in resp.text
    assert "No external validation yet" not in resp.text
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
    # The computed band, legible as text. "pass" renders with its meaning
    # spelled out — it is the PDF's deal vocabulary (pass ON the deal), and a
    # bare "pass" reads as the opposite of the decline it records.
    assert _band_label(html) == "decline"
    # band and recommendation must never be presented as each other.
    assert _band_label(html) != "route-to-incubation"


@pytest.mark.asyncio
async def test_admin_assessments_page_requires_admin(client, db_session):
    plain = await factories.make_user(db_session, user_role=USER_ROLE_PI, email="plain-assess@example.org")
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
            "credible_science": "met",
            "translational_potential": "unconfirmed",
        },
    ))
    await db_session.flush()

    resp = await client.get("/admin/assessments", headers=_auth(admin.id))
    assert resp.status_code == 200
    html = resp.text

    met_state = _gating_state_for(html, "life sciences domain")
    not_met_state = _gating_state_for(html, "baltimore commitment")
    unconfirmed_state = _gating_state_for(html, "translational potential")

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


@pytest.mark.asyncio
async def test_admin_assessments_page_renders_no_inline_detail_rows(
    client, db_session, admin
):
    """The expandable per-row detail (rationale + thirteen score chips +
    red-flag list + milestones, toggled by an onclick on the triage row) was
    removed 2026-08-27: the triage table is scan-only, and a verdict's full
    content lives on the detail page behind each row's "detail →" link —
    test_admin_detail_page_renders_the_whole_verdict
    (tests/integration/test_assessment_detail_page.py) pins that page rendering
    all four fields, and its scale tests pin the version/stage weight gate the
    chips used to exercise here. Even the richest possible verdict must render
    none of the old machinery, while the link out survives. The blanket
    "assessment-detail" not-in check also keeps the wrapper macros' class name
    honest (templates/admin/assessments.html deliberately avoids that
    substring)."""
    run = SimulationRun()
    db_session.add(run)
    await db_session.flush()
    assessment = OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="wang",
        channel_name="general", company_or_project="Rich verdict fixture",
        weighted_score=3.05, band="conditional",
        rationale="RATIONALE-ONLY-ON-THE-DETAIL-PAGE",
        red_flags=["FLAG-ONLY-ON-THE-DETAIL-PAGE"],
        derisking_milestones=["MILESTONE-ONLY-ON-THE-DETAIL-PAGE"],
        scores={"differentiation": 4},
    )
    db_session.add(assessment)
    await db_session.flush()

    resp = await client.get("/admin/assessments", headers=_auth(admin.id))
    assert resp.status_code == 200
    html = resp.text
    assert "Rich verdict fixture" in html

    # None of the removed machinery, even for a row with every detail field.
    assert "assessment-detail" not in html
    assert "Expand all" not in html and "Collapse all" not in html
    assert "Click for rationale" not in html

    # The detail fields themselves stay off the triage page — the flag count
    # badge is their only trace here.
    assert "RATIONALE-ONLY-ON-THE-DETAIL-PAGE" not in html
    assert "FLAG-ONLY-ON-THE-DETAIL-PAGE" not in html
    assert "MILESTONE-ONLY-ON-THE-DETAIL-PAGE" not in html
    assert "1 flag" in html

    # The one remaining route to that content: the per-row detail link.
    assert f'href="/admin/assessments/{assessment.id}"' in html


async def _two_runs_with_one_assessment_each(db_session):
    """Older run + a 'stale' verdict, newer run + a 'current' verdict —
    the exact shape --fresh (src/agent/main.py) leaves behind: a fresh
    restart creates a new SimulationRun but never deletes
    opportunity_assessments, so the old run's verdict is still tied to Slack
    messages that no longer exist.

    started_at is set explicitly rather than left to the column's
    ``server_default=func.now()``: Postgres's ``now()`` is pinned to
    transaction start, and this whole test runs in one transaction, so both
    rows would otherwise get the identical timestamp and "current run"
    ordering would be a coin flip. Real runs are minutes-to-days apart and
    never hit this.
    """
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    old_run = SimulationRun(started_at=now - timedelta(hours=1))
    db_session.add(old_run)
    await db_session.flush()
    db_session.add(OpportunityAssessment(
        simulation_run_id=old_run.id, agent_id="blackbird", subject_agent_id="wang",
        channel_name="general", company_or_project="Stale pre-fresh verdict",
    ))
    await db_session.flush()

    new_run = SimulationRun(started_at=now)
    db_session.add(new_run)
    await db_session.flush()
    db_session.add(OpportunityAssessment(
        simulation_run_id=new_run.id, agent_id="blackbird", subject_agent_id="fu",
        channel_name="general", company_or_project="Current verdict",
    ))
    await db_session.flush()
    return old_run, new_run


@pytest.mark.asyncio
async def test_admin_assessments_page_defaults_to_the_current_run(
    client, db_session, admin
):
    """Finding B1: the page must be able to tell "this verdict belongs to the
    current run" from "this is a leftover from a wiped run". Defaulting to
    the most recently started SimulationRun excludes the stale one by
    construction, without deleting it."""
    await _two_runs_with_one_assessment_each(db_session)

    resp = await client.get("/admin/assessments", headers=_auth(admin.id))
    assert resp.status_code == 200
    assert "Current verdict" in resp.text
    assert "Stale pre-fresh verdict" not in resp.text


@pytest.mark.asyncio
async def test_admin_assessments_page_all_runs_reaches_the_stale_verdict(
    client, db_session, admin
):
    """Finding B1: excluded-by-default must never mean deleted or
    unreachable — ?run_id=all is the escape hatch back to everything."""
    await _two_runs_with_one_assessment_each(db_session)

    resp = await client.get("/admin/assessments?run_id=all", headers=_auth(admin.id))
    assert resp.status_code == 200
    assert "Current verdict" in resp.text
    assert "Stale pre-fresh verdict" in resp.text
    # All-runs view names which run each row came from.
    assert "(current)" in resp.text


@pytest.mark.asyncio
async def test_admin_assessments_page_can_select_a_specific_older_run(
    client, db_session, admin
):
    old_run, _new_run = await _two_runs_with_one_assessment_each(db_session)

    resp = await client.get(
        f"/admin/assessments?run_id={old_run.id}", headers=_auth(admin.id)
    )
    assert resp.status_code == 200
    assert "Stale pre-fresh verdict" in resp.text
    assert "Current verdict" not in resp.text


@pytest.mark.asyncio
async def test_admin_assessments_page_bounds_the_query_and_says_so(
    client, db_session, admin, monkeypatch
):
    """Finding B1: the query had no LIMIT at all. Rather than create hundreds
    of rows to exercise the real cap, shrink it via monkeypatch and prove the
    truncation is visible on the page, not silent — and that it drops the
    lowest-scoring rows first, never the highest."""
    from src.services import directory as directory_service

    # The limit constant and the query that consumes it both live in
    # src.services.directory as of the admin-router extraction (Task 3 of
    # the user-account-types plan) — patch it there rather than on the old
    # src.routers.admin._ASSESSMENTS_LIMIT alias, which no longer exists.
    monkeypatch.setattr(directory_service, "ASSESSMENTS_LIMIT", 1)

    run = SimulationRun()
    db_session.add(run)
    await db_session.flush()
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="wang",
        channel_name="general", company_or_project="Highest scoring",
        weighted_score=4.5, band="advance",
    ))
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="fu",
        channel_name="general", company_or_project="Lower scoring",
        weighted_score=1.0, band="pass",
    ))
    await db_session.flush()

    resp = await client.get("/admin/assessments", headers=_auth(admin.id))
    assert resp.status_code == 200
    html = resp.text
    assert "Highest scoring" in html
    assert "Lower scoring" not in html
    assert "top 1 of 2" in html


@pytest.mark.asyncio
async def test_engine_known_subject_overrides_the_models_guess(engine):
    """The engine's own knowledge of who the interview is with wins over the
    model's `subject_agent_id` string.

    The phase-4 prompt never shows the hub a PI's `agent_id` — it is handed
    `{other_agent_name}` (the bot_name, "WangBot") and `{other_agent_lab}` (the
    pi_name, "Wang"). So the model can only guess, and it guesses the identifier
    it was actually shown. Consults, meanwhile, are recorded under the real
    `agent_id` ("wang"). When the guess was trusted, `_specialist_floor_gap`
    joined the consult record against "WangBot", found nothing, and refused the
    whole verdict — with the concluding reply already posted to Slack and no
    later turn to recover it. Failing this way is silent: one warning line and a
    missing row.

    Perversely, LEAVING the field blank always worked (the fallback applied, or
    the floor failed open), so the model doing more was punished.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    stub = SimulationEngine(
        agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
    )
    # The panel really was convened — recorded, as always, under the agent_id.
    for domain in ("scientific", "talent"):
        stub._record_consult("wang", domain)

    try:
        await SimulationEngine._persist_assessment(
            stub, "blackbird", "general",
            {
                # The bot_name form: the only identifier the prompt ever showed it.
                "subject_agent_id": "WangBot",
                "company_or_project": "DBT / BCAA-autophagy axis",
                "funnel_stage": "incubation",
                "recommendation": "advance",
                "confidence": "Speculative",
                "scores": {"differentiation": 4, "platform": 2},
                "gating": {"life_sciences_domain": "met", "credible_science": "met",
                           "translational_potential": "unconfirmed"},
            },
            subject_agent_id_fallback="wang",
        )

        async with factory() as check:
            rows = (await check.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalars().all()

        assert len(rows) == 1, "a row must be stored regardless of which id it lands under"
        # Stored under the real agent_id, so /admin/assessments and every join
        # against `agents` resolves.
        assert rows[0].subject_agent_id == "wang"
        # raw_verdict stays byte-for-byte what the model sent.
        assert rows[0].raw_verdict["subject_agent_id"] == "WangBot"
        # The override must reach the floor's own consult join, not just the
        # column: if _specialist_floor_gap joined consults against "WangBot"
        # instead of the engine's known "wang", it would find none recorded
        # and report a gap even though the panel really was convened above.
        assert rows[0].panel_incomplete is False, (
            "the floor joined consults against the model's 'WangBot' instead of "
            "the engine's known 'wang' — the override reached the column but not "
            "the floor"
        )
    finally:
        async with factory() as cleanup:
            stale = (await cleanup.execute(
                select(SimulationRun).where(SimulationRun.id == run_id)
            )).scalar_one_or_none()
            if stale is not None:
                await cleanup.delete(stale)  # cascades to the assessment
                await cleanup.commit()


@pytest.mark.asyncio
async def test_a_gapped_verdict_persists_with_no_drop_row(engine):
    """A gapped verdict is stored flagged, never routed through the drop table.

    This used to be `test_a_refused_verdict_is_recorded_as_a_drop`: until the
    fix in test_a_gapped_verdict_is_stored_and_flagged_not_discarded (above),
    an incomplete panel meant `_persist_assessment` recorded an
    `AssessmentDrop` with reason "specialist_floor" and returned early,
    discarding the verdict — after the concluding reply was already in Slack.
    That call site is gone; the three "specialist_floor" rows already in the
    database (see src/models/opportunity.py, _assessments_body.html) are
    historical, from before this fix, and no new ones can be produced by this
    path. What is left to check here is that a gap still creates no drop row
    of any kind — only the flagged assessment row.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine
    from src.models import AssessmentDrop

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    stub = SimulationEngine(
        agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
    )
    # A consult exists for someone else, so the floor does not fail open, and
    # none exists for THIS subject — the "hub never convened the panel" case.
    stub._record_consult("someone-else", "scientific")

    try:
        await SimulationEngine._persist_assessment(
            stub, "blackbird", "general",
            {
                "subject_agent_id": "wang",
                "company_or_project": "Refused thing",
                "recommendation": "advance",
                "scores": {"differentiation": 4},
            },
            subject_agent_id_fallback="wang",
        )

        async with factory() as check:
            assessments = (await check.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalars().all()
            drops = (await check.execute(
                select(AssessmentDrop).where(AssessmentDrop.simulation_run_id == run_id)
            )).scalars().all()

        assert len(assessments) == 1, "the verdict must be stored, not discarded"
        assert assessments[0].panel_incomplete is True
        assert "scientific" in (assessments[0].missing_domains or [])
        assert drops == [], "an incomplete panel is a flag on the row now, not a drop"
    finally:
        async with factory() as cleanup:
            stale = (await cleanup.execute(
                select(SimulationRun).where(SimulationRun.id == run_id)
            )).scalar_one_or_none()
            if stale is not None:
                await cleanup.delete(stale)  # cascades to the assessment
                await cleanup.commit()


@pytest.mark.asyncio
async def test_a_persisted_verdict_records_no_drop(engine):
    """The happy path must not pollute the drop count — otherwise the banner
    cries wolf on a perfectly healthy run."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine
    from src.models import AssessmentDrop

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    stub = SimulationEngine(
        agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
    )
    for domain in ("scientific", "talent"):
        stub._record_consult("wang", domain)

    try:
        await SimulationEngine._persist_assessment(
            stub, "blackbird", "general",
            {
                "subject_agent_id": "wang",
                "company_or_project": "Good thing",
                "recommendation": "advance",
                "scores": {"differentiation": 4, "platform": 2},
                "gating": {"translational_potential": "unconfirmed"},
            },
            subject_agent_id_fallback="wang",
        )

        async with factory() as check:
            assessments = (await check.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalars().all()
            drops = (await check.execute(
                select(AssessmentDrop).where(AssessmentDrop.simulation_run_id == run_id)
            )).scalars().all()

        assert len(assessments) == 1
        assert drops == []
    finally:
        async with factory() as cleanup:
            stale = (await cleanup.execute(
                select(SimulationRun).where(SimulationRun.id == run_id)
            )).scalar_one_or_none()
            if stale is not None:
                await cleanup.delete(stale)  # cascades to the assessment
                await cleanup.commit()


@pytest.mark.asyncio
async def test_admin_page_warns_about_dropped_verdicts(client, db_session, admin):
    """An empty page must not be able to mean two different things.

    "Nothing has been screened yet" and "everything was screened and every
    verdict was thrown away" rendered identically before this — the only trace
    of a loss was a WARNING in a container log.
    """
    from src.models import AssessmentDrop

    run = SimulationRun()
    db_session.add(run)
    await db_session.flush()
    db_session.add(AssessmentDrop(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="wang",
        thread_id="t1", reason="specialist_floor",
        detail="recommendation 'advance' required the talent specialist(s)",
    ))
    db_session.add(AssessmentDrop(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="gill",
        thread_id="t2", reason="unparseable_sidecar", detail="truncated",
    ))
    await db_session.flush()

    resp = await client.get("/admin/assessments", headers=_auth(admin.id))
    assert resp.status_code == 200
    # Collapse whitespace: the banner's sentence is wrapped across source lines,
    # and this test is about what it SAYS, not how the markup is folded.
    flat = " ".join(resp.text.split())
    assert "2 verdicts lost" in flat
    assert "generated and never stored, or never produced at all" in flat
    # Each reason is broken out, because each has a different fix.
    assert "specialist_floor" in flat
    assert "unparseable_sidecar" in flat
    # And the operator is told the Slack side already went out.
    assert "was posted normally" in flat


@pytest.mark.asyncio
async def test_admin_page_has_no_drop_banner_on_a_clean_run(client, db_session, admin):
    """No drops, no banner — it must not cry wolf on a healthy run."""
    run = SimulationRun()
    db_session.add(run)
    await db_session.flush()
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="wang",
        channel_name="general", company_or_project="A clean verdict",
        recommendation="advance", weighted_score=4.2, band="advance",
    ))
    await db_session.flush()

    resp = await client.get("/admin/assessments", headers=_auth(admin.id))
    assert resp.status_code == 200
    assert "generated but not stored" not in resp.text


# --- Round 2: the per-turn monotonic latch (Task 7 fix round) ---------------
#
# `ThreadState.floor_armed` was originally frozen forever at whatever value
# activation saw. Review caught that this reintroduced exactly the failure
# mode the field was built to fix, from the other direction:
#
# - CRITICAL: a thread activated while `_specialist_consults` was globally
#   empty (floor_armed=False) could never arm again — not even once THIS
#   SAME interview went on to consult specialists for its own subject. An
#   incomplete panel was silently persisted as if fully vetted.
# - IMPORTANT: `_rebuild_agent_state` always constructs a restart-restored
#   ThreadState with floor_armed=False. Frozen forever, that made every
#   restart-restored thread permanently unenforceable, no matter how many
#   consults the restarted process went on to record.
#
# The fix (Ruling R3): re-latch `thread.floor_armed = thread.floor_armed or
# bool(self._specialist_consults)` at the very top of `_reply_to_thread`,
# before any `await` in that method — monotonic (never un-arms), global (never
# keyed on this thread's own subject, so "the hub never convenes a panel at
# all" still bites), and still race-safe (captured once per turn, before that
# turn's own tool calls or any other task's writes can reach it).
#
# These three tests drive `_reply_to_thread` end-to-end via
# `_drive_reply_to_thread` (same harness as the "Option A relocation" tests
# above), because the latch lives inside that method — a test that only calls
# `_specialist_floor_gap` directly cannot exercise it at all.


@pytest.mark.asyncio
async def test_floor_arms_from_this_threads_own_later_consult(engine, monkeypatch):
    """Critical finding: a thread activated while the map was globally empty
    (floor_armed=False — exactly what every real activation site produces
    then) must not stay permanently unable to arm. Here THIS SAME interview's
    subject ("wang") already got one of the two always-required consults
    ("scientific", missing "talent") — recorded as if it happened on an
    earlier turn, before this concluding turn's own top-of-turn latch runs.

    Before the per-turn latch, `floor_armed` stayed frozen at its
    activation-time False forever, so this incomplete panel would have been
    silently persisted as if fully vetted — the bug reported empirically:
    "with an incomplete panel, the frozen snapshot returned set() (no gap)
    where the live global correctly returned {'talent'}". The panel gap is no
    longer a refusal (see test_a_gapped_verdict_is_stored_and_flagged_not_discarded)
    — the row is always stored — so what distinguishes the fixed latch from
    the frozen one is now `panel_incomplete`/`missing_domains` on that row,
    not whether a row exists at all.
    """
    from src.models import AssessmentDrop

    response = (
        _SLACK_BODY + "\n\n"
        '<assessment_json>\n'
        '{"subject_agent_id": "wang", "recommendation": "advance", '
        '"scores": {"differentiation": 5}}\n'
        '</assessment_json>'
    )
    agent, thread, client, factory, run_id = await _drive_reply_to_thread(
        engine, monkeypatch, response,
        initial_floor_armed=False,
        pre_consults=[("wang", "scientific")],
    )
    try:
        assert thread.floor_armed is True, (
            "the concluding turn's own top-of-turn latch must see the map "
            "non-empty and arm, since 'wang' already has a recorded consult"
        )
        assert len(client.posted) == 1  # the reply still went out to Slack
        rows = await _assessment_rows(factory, run_id)
        assert len(rows) == 1, "the verdict must be stored, not discarded"
        assert rows[0].panel_incomplete is True, (
            "an incomplete panel (missing 'talent') must be flagged — the "
            "frozen snapshot bug would have computed an empty gap here "
            "(set()) instead of the live global's {'talent'}"
        )
        assert rows[0].missing_domains == ["talent"]
        async with factory() as check:
            drops = (await check.execute(
                select(AssessmentDrop).where(AssessmentDrop.simulation_run_id == run_id)
            )).scalars().all()
        assert drops == [], "an incomplete panel is a flag on the row now, not a drop"
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_floor_arms_for_a_restart_rebuilt_thread_once_any_consult_is_recorded(
    engine, monkeypatch,
):
    """Important finding: `_rebuild_agent_state` always constructs a restored
    ThreadState with floor_armed=False (the process has recorded nothing yet
    — it just started). Frozen forever, that was permanent: even after the
    restarted process went on to record consults for OTHER PIs, a restored
    thread's own concluding turn still read the stale False and failed open
    forever, no matter how long the process had been running.

    This constructs the thread with floor_armed=False directly — exactly the
    shape `_rebuild_agent_state` always produces — rather than re-running the
    whole rebuild machinery, which does not touch this latch at all. The
    consult recorded here is for a DIFFERENT PI entirely, proving the latch
    arms off the GLOBAL map, never off this thread's own subject: keying it
    on the subject would walk back into the exact hole
    `_specialist_floor_gap`'s docstring already warns about — "a hub that
    simply never convenes a panel" for THIS PI must still be caught. Being
    caught now means stored and flagged, not refused (see
    test_a_gapped_verdict_is_stored_and_flagged_not_discarded) — the row
    always exists, so `panel_incomplete` is the assertion that carries this
    test's finding.
    """
    response = (
        _SLACK_BODY + "\n\n"
        '<assessment_json>\n'
        '{"subject_agent_id": "wang", "recommendation": "advance", '
        '"scores": {"differentiation": 5}}\n'
        '</assessment_json>'
    )
    agent, thread, client, factory, run_id = await _drive_reply_to_thread(
        engine, monkeypatch, response,
        initial_floor_armed=False,  # exactly what _rebuild_agent_state always seeds
        pre_consults=[("someone-else", "scientific")],
    )
    try:
        assert thread.floor_armed is True
        rows = await _assessment_rows(factory, run_id)
        assert len(rows) == 1, "the verdict must be stored, not discarded"
        assert rows[0].panel_incomplete is True, (
            "wang's own panel was never convened at all — the floor must "
            "still catch that, not fail open forever just because this "
            "thread survived a restart"
        )
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_concurrent_consult_landing_mid_turn_does_not_flip_this_verdict(
    engine, monkeypatch,
):
    """The ORIGINAL race this whole mechanism exists to close, re-pinned
    against the per-turn latch (not just the frozen-at-activation snapshot):
    a DIFFERENT interview's first-ever consult must not retroactively arm
    THIS turn's floor.

    `on_generate` fires a consult for an unrelated PI from INSIDE the faked
    `generate_with_tools` call — i.e. AFTER `_reply_to_thread`'s top-of-turn
    latch has already captured `thread.floor_armed=False` from the
    then-still-empty map, simulating a concurrent task's write landing
    mid-await. Because the latch already ran before that write, this
    interview — which began this turn under fail-open — must persist
    normally, exactly as it would have if no consult had ever been recorded
    anywhere.
    """
    response = (
        _SLACK_BODY + "\n\n"
        '<assessment_json>\n'
        '{"subject_agent_id": "wang", "recommendation": "advance", '
        '"scores": {"differentiation": 5}}\n'
        '</assessment_json>'
    )
    agent, thread, client, factory, run_id = await _drive_reply_to_thread(
        engine, monkeypatch, response,
        initial_floor_armed=False,  # map is empty when this turn starts
        on_generate=lambda sim: sim._record_consult("someone-else", "scientific"),
    )
    try:
        assert thread.floor_armed is False, (
            "the latch already ran, before this turn's own generate call, "
            "and must not be re-read live at persist time"
        )
        rows = await _assessment_rows(factory, run_id)
        assert len(rows) == 1, (
            "this interview began under fail-open and must stay there for "
            "this turn, even though the global map is no longer empty by "
            "the time persistence runs"
        )
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_gapped_verdict_is_stored_and_flagged_not_discarded(engine):
    """The floor's finding must not cost the verdict.

    _persist_assessment runs AFTER the concluding reply is already in Slack, so
    refusing meant the PI had been told and Blackbird kept nothing. Both
    production refusals (gordy, 2026-08-17) lost a real conditional verdict
    exactly this way.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine
    from src.services.blackbird_rubric import RUBRIC_WEIGHTS

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    stub = SimulationEngine(
        agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
    )
    # Arm the floor: a consult for SOME OTHER PI proves this process records
    # consults, so an absent record for `gordy` means the panel was skipped
    # rather than that we restarted mid-interview.
    stub._record_consult("someone_else", "scientific")

    await SimulationEngine._persist_assessment(
        stub, "blackbird", "general",
        {
            "subject_agent_id": "gordy",
            "recommendation": "conditional",
            "rationale": "A peptide-based vaccine platform for tuberculosis.",
            "scores": {k: 3 for k in RUBRIC_WEIGHTS},
        },
        slack_ts="1.1",
        subject_agent_id_fallback="gordy",
    )

    async with factory() as db:
        rows = (await db.execute(select(OpportunityAssessment))).scalars().all()

    assert len(rows) == 1, "the verdict must be stored, not discarded"
    row = rows[0]
    assert row.panel_incomplete is True
    assert "chemistry" in row.missing_domains
    assert row.recommendation == "conditional", "the verdict itself is unchanged"
    assert row.weighted_score is not None, "scoring still runs on a flagged row"


@pytest.mark.asyncio
async def test_a_complete_panel_stores_an_unflagged_row(engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine
    from src.services.blackbird_rubric import RUBRIC_WEIGHTS

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    stub = SimulationEngine(
        agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
    )
    for domain in ("scientific", "talent", "chemistry", "clinical", "technologic"):
        stub._record_consult("gordy", domain)

    await SimulationEngine._persist_assessment(
        stub, "blackbird", "general",
        {
            "subject_agent_id": "gordy",
            "recommendation": "conditional",
            "rationale": "A peptide-based platform for a tuberculosis indication.",
            "scores": {k: 3 for k in RUBRIC_WEIGHTS},
        },
        slack_ts="1.1",
        subject_agent_id_fallback="gordy",
    )

    async with factory() as db:
        row = (await db.execute(select(OpportunityAssessment))).scalars().one()
    assert row.panel_incomplete is False
    assert row.missing_domains is None, (
        "NULL is reserved for a VERIFIED-complete panel. [] is the third state "
        "— the floor could not be checked at all — and this row was checked"
    )


@pytest.mark.asyncio
async def test_an_unverifiable_floor_stores_the_empty_sentinel_not_null(engine):
    """The post-restart verdict must not read as a verified-complete panel.

    `_specialist_floor_gap` returns an empty set for two different reasons:
    the panel really was complete, or there was nothing to check it against.
    The second is the ORDINARY state after a restart — `_specialist_consults`
    is in-memory, every `_rebuild_agent_state` thread starts `floor_armed=False`,
    and production's last exit was a SIGKILL — so it is not a corner case.

    Both used to store `panel_incomplete=False, missing_domains=NULL`, which
    counted every unverifiable verdict as a vetted one and silently
    under-reported the exact number spec §10's panel-gap surface exists to
    produce. The third state the column already reserved says which is which.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine
    from src.services.blackbird_rubric import RUBRIC_WEIGHTS

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    stub = SimulationEngine(
        agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
    )
    # No consult recorded for ANYONE: the floor is unarmed, exactly as it is in
    # the first turns after a restart. Note this is NOT the same as the gapped
    # case above, which records a consult for some other PI to prove the
    # process does record them.
    assert stub._specialist_consults == {}

    try:
        await SimulationEngine._persist_assessment(
            stub, "blackbird", "general",
            {
                "subject_agent_id": "gordy",
                "recommendation": "conditional",
                "rationale": "A peptide-based vaccine platform for tuberculosis.",
                "scores": {k: 3 for k in RUBRIC_WEIGHTS},
            },
            slack_ts="1.1",
            subject_agent_id_fallback="gordy",
        )

        rows = await _assessment_rows(factory, run_id)
        assert len(rows) == 1, "failing open must still store the verdict"
        row = rows[0]
        assert row.panel_incomplete is False, (
            "we have no evidence of a gap — only an inability to look — so the "
            "flag must not fire and false-accuse a panel that may have been "
            "convened before the restart"
        )
        assert row.missing_domains == [], (
            "the floor could not be checked, so this row is UNVERIFIED. NULL "
            "here would be indistinguishable from the verified-complete row in "
            "test_a_complete_panel_stores_an_unflagged_row"
        )
        assert row.recommendation == "conditional", "the verdict itself is unchanged"
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_verdict_with_no_subject_is_unverifiable_too(engine):
    """The floor's OTHER fail-open case reaches the same third state.

    Nothing to join a consult record to is "we could not check", not "the
    panel was fine" — even though this engine demonstrably records consults.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    stub = SimulationEngine(
        agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
    )
    stub._record_consult("someone-else", "scientific")  # the floor IS armed

    try:
        await SimulationEngine._persist_assessment(
            stub, "blackbird", "general",
            {"recommendation": "advance", "company_or_project": "Unattributed"},
        )

        rows = await _assessment_rows(factory, run_id)
        assert len(rows) == 1
        assert rows[0].subject_agent_id is None
        assert rows[0].panel_incomplete is False
        assert rows[0].missing_domains == [], (
            "no subject_agent_id means the floor had nothing to join on, which "
            "is unverified — not verified complete"
        )
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_pass_verdict_owes_no_panel_and_is_not_marked_unverified(engine):
    """`[]` must mean "we could not check", not "we did not need to".

    A `pass` whose computed band agrees is not held to the panel at all
    (`specialists.panel_is_owed`), so there is nothing about it that failed to
    verify. If the sentinel leaked onto
    every declined idea it would drown the signal it exists to carry — and
    `pass` is the commonest verdict there is.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    stub = SimulationEngine(
        agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
    )
    assert stub._specialist_consults == {}  # unarmed, as after a restart

    try:
        await SimulationEngine._persist_assessment(
            stub, "blackbird", "general",
            {
                "subject_agent_id": "gordy",
                "recommendation": "pass",
                "rationale": "A peptide-based vaccine platform for tuberculosis.",
            },
            subject_agent_id_fallback="gordy",
        )

        rows = await _assessment_rows(factory, run_id)
        assert len(rows) == 1
        assert rows[0].panel_incomplete is False
        assert rows[0].missing_domains is None, (
            "no panel was owed, so nothing failed to verify — NULL, not []"
        )
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_admin_assessments_page_triage_restructure(client, db_session, admin):
    """The 2026-08-20 triage restructure, pinned end to end (minus the inline
    detail rows, removed 2026-08-27 — the per-row "detail →" link is now the
    only route to rationale, flags and scores; see
    test_admin_assessments_page_renders_no_inline_detail_rows):

    * the recommendation renders as a chip whose ``pass`` value is labelled
      "decline" — the PDF's deal vocabulary means the decline, and a
      bare "pass" reads as its opposite;
    * red flags collapse to a count badge in the triage row — the full list
      is detail-page content;
    * the headline cards count by RECOMMENDATION (route-to-incubation is the
      designed positive outcome for incubation-stage ideas and lives inside
      the <3.0 band, so band-only cards read "everything failed").
    """
    run = SimulationRun()
    db_session.add(run)
    await db_session.flush()
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="wang",
        channel_name="general", company_or_project="Triage fixture Co",
        recommendation="pass", weighted_score=2.10, band="pass",
        rationale="Single-asset, no external validation.",
        red_flags=["No external validation", "Single-shot asset"],
    ))
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="fu",
        channel_name="general", company_or_project="Routed fixture Co",
        recommendation="route-to-incubation", weighted_score=2.66, band="pass",
    ))
    await db_session.flush()

    resp = await client.get("/admin/assessments", headers=_auth(admin.id))
    assert resp.status_code == 200
    html = resp.text

    # Recommendation chip: pass is labelled with its meaning; the routed row
    # keeps its verbatim value.
    assert "decline" in html
    assert "route-to-incubation" in html

    # Red flags: count badge only — the list itself lives on the detail page.
    assert "2 flags" in html
    assert "No external validation" not in html
    assert "Single-shot asset" not in html

    # Headline cards count recommendations, not bands: both fixtures band
    # "pass", but one is a positive routing and must be counted as one.
    assert "Route-to-incubation" in html
    assert "Decline" in html


@pytest.mark.asyncio
async def test_admin_assessments_cards_count_recommendation_not_band(
    client, db_session, admin
):
    """Both rows band "pass" (the all-pass production shape) while the
    recommendations split 1/1 — the cards must follow the split, not the
    band. A card layout that still counted ``band`` would render
    Route-to-incubation as 0 here."""
    run = SimulationRun()
    db_session.add(run)
    await db_session.flush()
    for subject, rec in (("wang", "route-to-incubation"), ("fu", "pass")):
        db_session.add(OpportunityAssessment(
            simulation_run_id=run.id, agent_id="blackbird",
            subject_agent_id=subject, channel_name="general",
            company_or_project=f"{subject} cards fixture",
            recommendation=rec, weighted_score=2.5, band="pass",
        ))
    await db_session.flush()

    resp = await client.get("/admin/assessments", headers=_auth(admin.id))
    assert resp.status_code == 200
    html = resp.text

    cards = re.search(r'<div class="grid grid-cols-5[^"]*">(.*?)\n</div>', html, re.S)
    assert cards, "five headline cards must render"
    block = cards.group(1)
    counts = {
        label: int(count)
        for count, label in re.findall(
            r'>\s*(\d+)\s*</div>\s*<div class="text-sm text-gray-500 mt-1">'
            r'([^<]+)</div>',
            block,
        )
    }
    assert counts["Total"] == 2
    assert counts["Route-to-incubation"] == 1
    assert counts["Decline"] == 1
    assert counts["Advance"] == 0
    assert counts["Conditional"] == 0


# ---------------------------------------------------------------------------
# `panel_owed` and `thread_id` — the two durable facts the row used to omit
#
# `panel_owed` exists because the admin detail page used to re-derive "was this
# verdict held to the floor?" at RENDER time, from a predicate that has widened
# twice in one month — so every widening silently relabelled every older row.
# 12 production rows rendered the green "panel verified" box for a floor that
# never ran. The write path is what makes the read path's fix possible: without
# a stored answer the page has nothing to replay. See
# `src/services/assessment_detail.panel_state`.
#
# `thread_id` exists so the one-interview-one-verdict invariant is enforceable
# and, above all, REHYDRATABLE: before it the table did not record which
# interview a verdict came from, so a restarted process could not tell a first
# verdict from a re-capture of a turn it had already stored.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_assessment_records_that_a_panel_was_owed(engine):
    """An `advance`/`conditional` verdict IS held to the floor, so the floor
    evaluated it — and the row has to say so, because an empty `missing_domains`
    only means "verified" on a row where a floor actually ran."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine
    from src.services.blackbird_rubric import RUBRIC_WEIGHTS

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    stub = SimulationEngine(
        agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
    )
    for domain in ("scientific", "talent", "chemistry", "clinical", "technologic"):
        stub._record_consult("gordy", domain)
    try:
        await SimulationEngine._persist_assessment(
            stub, "blackbird", "general",
            {
                "subject_agent_id": "gordy",
                "recommendation": "conditional",
                "rationale": "A peptide-based platform for a tuberculosis indication.",
                "scores": {k: 3 for k in RUBRIC_WEIGHTS},
            },
            slack_ts="1.1",
            subject_agent_id_fallback="gordy",
        )
        rows = await _assessment_rows(factory, run_id)
        assert len(rows) == 1
        assert rows[0].panel_owed is True, (
            "the floor was owed a panel and evaluated this verdict; storing "
            "NULL here is what forced the page to guess"
        )
        # The pairing that makes the green box legitimate, asserted together so
        # neither half can drift: owed AND no gap AND checkable.
        assert rows[0].panel_incomplete is False
        assert rows[0].missing_domains is None
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_persist_assessment_records_that_no_panel_was_owed(engine):
    """A decline the floor exempted stores `False`, not NULL. `False` is a
    finding ("we looked at the rules and none applied"); NULL is the absence of
    one, and the page renders them differently on purpose.

    Both halves have to be unowed for the exemption to hold, which is why the
    scores below are straight 2s and not straight 3s. The floor keys on the
    COMPUTED band as well as the written recommendation, and straight 3s band
    `conditional` on the investment scale — which would make this verdict OWED a
    panel and `panel_owed` True, inverting the claim. Straight 2s band `pass`,
    so recommendation and band agree and the exemption holds.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine
    from src.services.blackbird_rubric import RUBRIC_WEIGHTS

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    stub = SimulationEngine(
        agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
    )
    try:
        await SimulationEngine._persist_assessment(
            stub, "blackbird", "general",
            {
                "subject_agent_id": "gordy",
                "recommendation": "pass",
                "rationale": "Not differentiated enough to pursue.",
                "scores": {k: 2 for k in RUBRIC_WEIGHTS},
            },
            slack_ts="1.1",
            subject_agent_id_fallback="gordy",
        )
        rows = await _assessment_rows(factory, run_id)
        assert len(rows) == 1
        # `is False`, not `is not None` and not falsy: NULL is a THIRD state
        # ("we do not know"), the page renders it differently, and `is False`
        # already excludes it — a follow-up `is not None` assertion cannot fail
        # after it and only reads as coverage.
        assert rows[0].panel_owed is False
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_persist_assessment_records_the_interview_thread(engine):
    """The verdict's link back to the interview it came out of. Without it a
    restarted process cannot rehydrate `_assessed_threads`, so the interview's
    own concluding verdict looks like a first verdict and lands a second row."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine
    from src.agent.state import ThreadState
    from src.services.blackbird_rubric import RUBRIC_WEIGHTS

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    stub = SimulationEngine(
        agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
    )
    thread = ThreadState(
        thread_id="1787151586.955459", channel="general", other_agent_id="gordy",
        message_count=11,
    )
    try:
        await SimulationEngine._persist_assessment(
            stub, "blackbird", "general",
            {
                "subject_agent_id": "gordy",
                "recommendation": "conditional",
                "scores": {k: 3 for k in RUBRIC_WEIGHTS},
            },
            slack_ts="1.1",
            subject_agent_id_fallback="gordy",
            thread=thread,
        )
        rows = await _assessment_rows(factory, run_id)
        assert len(rows) == 1
        assert rows[0].thread_id == "1787151586.955459"
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_caller_with_no_thread_stores_a_null_thread_id(engine):
    """Direct callers (and every pre-existing test) pass no thread. NULL is the
    honest answer there — never the empty string, which `WHERE thread_id IS NULL`
    would not match and which would collide across interviews."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine
    from src.services.blackbird_rubric import RUBRIC_WEIGHTS

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    stub = SimulationEngine(
        agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
    )
    try:
        await SimulationEngine._persist_assessment(
            stub, "blackbird", "general",
            {"subject_agent_id": "gordy", "recommendation": "pass",
             "scores": {k: 2 for k in RUBRIC_WEIGHTS}},
            slack_ts="1.1",
        )
        rows = await _assessment_rows(factory, run_id)
        assert len(rows) == 1
        assert rows[0].thread_id is None
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_the_retry_queue_carries_panel_owed_and_thread_id(engine):
    """`_flush_pending_assessments` rebuilds the row from the SAME kwargs dict,
    so a column added to the immediate write but not to that dict would be
    silently NULL on exactly the rows a busy pool produced — the ones written
    under load, which is when this instrumentation matters most."""
    import uuid as _uuid

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine
    from src.agent.state import ThreadState
    from src.services.blackbird_rubric import RUBRIC_WEIGHTS

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    stub = SimulationEngine(
        agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
    )
    for domain in ("scientific", "talent", "chemistry", "clinical", "technologic"):
        stub._record_consult("gordy", domain)
    thread = ThreadState(
        thread_id="t-queued", channel="general", other_agent_id="gordy",
        message_count=11,
    )
    try:
        # A run id that violates the FK: the insert raises and the row is queued,
        # exactly as a pool-checkout timeout would leave it.
        stub.simulation_run_id = _uuid.uuid4()
        await SimulationEngine._persist_assessment(
            stub, "blackbird", "general",
            {"subject_agent_id": "gordy", "recommendation": "conditional",
             "scores": {k: 3 for k in RUBRIC_WEIGHTS}},
            slack_ts="1.1", subject_agent_id_fallback="gordy", thread=thread,
        )
        assert len(stub._pending_assessments) == 1
        stub.simulation_run_id = run_id
        stub._pending_assessments[0]["simulation_run_id"] = run_id
        await stub._flush_pending_assessments()

        rows = await _assessment_rows(factory, run_id)
        assert len(rows) == 1
        assert rows[0].panel_owed is True
        assert rows[0].thread_id == "t-queued"
    finally:
        await _delete_run(factory, run_id)
