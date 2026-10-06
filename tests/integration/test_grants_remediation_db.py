"""scripts/grants_remediation.py against the test database: the dry run writes nothing,
the completeness gate, the render-diff check."""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

import scripts.grants_remediation as gr
from src.models import Job, PiGrant, PiGrantIdentity, PiOrcidFunding
from src.services import grant_enrichment as ge
from src.services import profile_export
from src.services.profile_publish import write_persona_files
from tests import factories

pytestmark = pytest.mark.integration

_TABLES = (PiGrant, PiGrantIdentity, PiOrcidFunding)


async def _counts(db):
    return [await db.scalar(select(func.count()).select_from(t)) for t in _TABLES]


async def test_the_dry_run_writes_nothing(db_session, monkeypatch, tmp_path):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path / "public")
    pi = await factories.make_user(db_session, name="Fidel Zavala")
    name = pi.name

    async def fake_search(criteria, fields, max_total=500):
        return []

    async def fake_fundings(orcid, *, strict):
        return []

    monkeypatch.setattr(ge, "search_projects", fake_search)
    monkeypatch.setattr(gr, "fetch_orcid_fundings", fake_fundings)
    before = await _counts(db_session)
    report = await gr.dry_run(db_session, [(pi.id, pi.orcid, name)], pace=0)
    after = await _counts(db_session)
    assert before == after and name in report["no_match"]


async def test_completeness_gaps_name_unevaluated_pis(db_session):
    deploy = datetime.now(UTC) - timedelta(minutes=5)
    done = await factories.make_user(db_session)
    pending = await factories.make_user(db_session)
    db_session.add(PiGrantIdentity(user_id=done.id, status="no_match",
                                   evaluated_at=datetime.now(UTC),
                                   orcid_fetched_at=datetime.now(UTC)))
    db_session.add(PiGrantIdentity(user_id=pending.id, orcid_fetched_at=datetime.now(UTC)))
    await db_session.flush()
    gaps = await gr.completeness_gaps(db_session, [done.id, pending.id], deploy)
    assert gaps == [pending.name]


async def test_the_flag_gate_ignores_a_dead_job_from_an_earlier_run(db_session):
    pi = await factories.make_user(db_session)
    now = datetime.now(UTC)
    db_session.add(PiGrantIdentity(user_id=pi.id, status="no_match", evaluated_at=now,
                                   orcid_fetched_at=now))
    db_session.add_all([
        Job(type="enrich_grants", user_id=pi.id, status="dead", payload={},
            enqueued_at=datetime(2026, 1, 1, tzinfo=UTC)),
        Job(type="enrich_grants", user_id=pi.id, status="completed",
            payload={"bulk_tag": "grants-x"}, enqueued_at=now),
    ])
    await db_session.flush()
    since = now - timedelta(minutes=1)
    assert await gr.flag_gate(db_session, [pi.id], tag="grants-x", since=since,
                              deploy_ts=since) == []
    db_session.add(Job(type="enrich_grants", user_id=pi.id, status="dead",
                       payload={"bulk_tag": "grants-x"}, enqueued_at=now + timedelta(seconds=1)))
    await db_session.flush()
    assert await gr.flag_gate(db_session, [pi.id], tag="grants-x", since=since, deploy_ts=since)


async def test_verify_flags_a_stale_persona_file(db_session, monkeypatch, tmp_path):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path / "public")
    pi = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=pi)
    agent = await factories.make_agent(db_session, user=pi, agent_id="verifystale")
    db_session.add(PiGrantIdentity(user_id=pi.id, status="no_match",
                                   evaluated_at=datetime.now(UTC),
                                   orcid_fetched_at=datetime.now(UTC)))
    await db_session.flush()
    await write_persona_files(db_session, pi.id)
    diffs = await gr.render_diffs(db_session)
    assert diffs == []
    (tmp_path / "public" / f"{agent.agent_id}.md").write_text("stale")
    assert await gr.render_diffs(db_session) == [agent.agent_id]
