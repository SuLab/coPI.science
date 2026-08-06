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
async def test_persist_assessment_drops_boolean_gating_but_keeps_raw_verdict(engine):
    """The old contract wrote gating as booleans; the current one writes
    "met"/"not_met"/"unconfirmed" strings. A gating map is stored in the
    structured `gating` column only when EVERY value already conforms to the
    tri-state contract — a map with even one boolean (or otherwise invalid)
    value is dropped to None wholesale rather than partially normalized, so the
    column never silently mixes the two conventions. The original is untouched
    in raw_verdict, so nothing is lost — only left where it can't be
    misread as structured data it isn't."""
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
        "scores": {"differentiation": 3},
        # One legacy boolean mixed with one valid tri-state string — the whole
        # map must be rejected, not just the bad key.
        "gating": {"baltimore_commitment": False, "life_sciences_domain": "met"},
    }
    await SimulationEngine._persist_assessment(stub, "blackbird", "general", verdict)

    try:
        async with factory() as check:
            row = (await check.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalar_one()
            assert row.gating is None
            assert row.raw_verdict["gating"] == {
                "baltimore_commitment": False, "life_sciences_domain": "met",
            }
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
            assert row.weighted_score == pytest.approx(0.0)
            assert row.band == "pass"
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


# --- /admin/assessments (task 12) -------------------------------------------


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
