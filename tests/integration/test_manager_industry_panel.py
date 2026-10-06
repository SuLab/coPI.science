"""Manager UI: industry-interest score panel, evidence drawer + veto, PI-list
column."""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    PiIndustryEvidence,
    PiIndustryScore,
)
from src.routers import manager as manager_routes
from src.services import directory
from src.services.industry_score import SCORER_VERSION
from src.services.jhu_rules import set_tenure_start
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def test_unscored_pi_shows_reason_not_zero(client, db_session):
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    db_session.add(PiIndustryScore(
        user_id=pi.id, components={}, coverage={}, scorer_version=SCORER_VERSION,
    ))
    await db_session.commit()
    html = (await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(mgr.id))).text
    assert "Unscored" in html and "no JHU tenure start" in html and ">0.0<" not in html


async def test_veto_evidence_rescored_and_hidden(client, db_session):
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    e = PiIndustryEvidence(
        user_id=pi.id, source="openalex", kind="coauthor_company", external_id="W1:I1",
        company_name="Pfizer", company_class="pharma_biotech", year=2021, pi_role="last",
        in_tenure=True, evidence={},
    )
    db_session.add(e)
    db_session.add(PiIndustryScore(
        user_id=pi.id, raw_sum=4.5, components={}, scorer_version=SCORER_VERSION,
        tenure_start_used=2018, coverage={"openalex": "ok"},
    ))
    # The veto's rescore re-reads the tenure start (rescore_user), so the PI needs one.
    await set_tenure_start(pi.id, 2018, "manual", db=db_session)
    await db_session.commit()

    r = await client.post(
        f"/manager/pis/{pi.id}/industry/{e.id}/veto", data={}, headers=auth_headers(mgr.id),
        follow_redirects=False,
    )
    assert r.status_code == 302

    # Not `.order_by(computed_at.desc()).first()`: within a single test
    # transaction (tests/conftest.py's db_session shares one Postgres
    # transaction across the seed commit and the route's own commit via
    # savepoints) `func.now()` is the transaction start time, so the seed row
    # and the rescored row can share an identical `computed_at` and tie-break
    # in insertion order — a test-harness artifact, not a real-world one,
    # since separate HTTP requests get separate transactions. Assert the
    # rescore produced a zero row carrying the seed's coverage instead of trusting
    # timestamp order.
    scores = (await db_session.execute(
        select(PiIndustryScore).where(PiIndustryScore.user_id == pi.id)
    )).scalars().all()
    assert any(s.raw_sum == 0 and s.coverage == {"openalex": "ok"} for s in scores)
    assert (await directory.industry_views(db_session, [pi.id]))[pi.id].reason == "no_evidence"


async def test_veto_is_idempotent_on_replay(client, db_session):
    """A replayed POST on an already-vetoed row must not rescore a second
    time."""
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    e = PiIndustryEvidence(
        user_id=pi.id, source="openalex", kind="coauthor_company", external_id="W1:I1",
        company_name="Pfizer", company_class="pharma_biotech", year=2021, pi_role="last",
        in_tenure=True, evidence={},
    )
    db_session.add(e)
    await db_session.commit()

    for _ in range(2):
        r = await client.post(
            f"/manager/pis/{pi.id}/industry/{e.id}/veto", data={}, headers=auth_headers(mgr.id),
            follow_redirects=False,
        )
        assert r.status_code == 302

    scores = (await db_session.execute(
        select(PiIndustryScore).where(PiIndustryScore.user_id == pi.id)
    )).scalars().all()
    assert len(scores) == 1


async def test_veto_evidence_belonging_to_another_pi_is_404(client, db_session):
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    other_pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    e = PiIndustryEvidence(
        user_id=other_pi.id, source="openalex", kind="coauthor_company", external_id="W1:I1",
        company_name="Pfizer", company_class="pharma_biotech", year=2021, pi_role="last",
        in_tenure=True, evidence={},
    )
    db_session.add(e)
    await db_session.commit()

    r = await client.post(
        f"/manager/pis/{pi.id}/industry/{e.id}/veto", data={}, headers=auth_headers(mgr.id),
        follow_redirects=False,
    )
    assert r.status_code == 404

    await db_session.refresh(e)
    assert e.vetoed_at is None


async def test_veto_attribution_under_impersonation(client, db_session, caplog):
    """vetoed_by_user_id names the worn (impersonated)
    account, per branch convention, but the real session holder is logged."""
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    e = PiIndustryEvidence(
        user_id=pi.id, source="openalex", kind="coauthor_company", external_id="W1:I1",
        company_name="Pfizer", company_class="pharma_biotech", year=2021, pi_role="last",
        in_tenure=True, evidence={},
    )
    db_session.add(e)
    await db_session.commit()

    headers = auth_headers(admin.id, impersonate=mgr.id)
    with caplog.at_level("WARNING"):
        r = await client.post(
            f"/manager/pis/{pi.id}/industry/{e.id}/veto", data={}, headers=headers,
            follow_redirects=False,
        )
    assert r.status_code == 302

    await db_session.refresh(e)
    assert e.vetoed_by_user_id == mgr.id
    assert str(admin.id) in caplog.text


async def test_pi_list_has_industry_column(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    for raw in (1.0, 2.0, 3.0, 20.0):
        pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
        db_session.add(PiIndustryScore(
            user_id=pi.id, raw_sum=raw, components={}, scorer_version=SCORER_VERSION,
            tenure_start_used=2015, coverage={"openalex": "ok"},
        ))
    await db_session.commit()
    html = (await client.get("/manager/pis", headers=auth_headers(mgr.id))).text
    assert "Industry interest" in html and "100.0" in html and "75.0" in html


async def test_the_veto_refuses_on_a_lock_timeout(engine, monkeypatch):
    """An industry_evidence job holding the row: the veto gives up after
    INDUSTRY_VETO_LOCK_TIMEOUT instead of waiting, and vetoes nothing."""
    monkeypatch.setattr(manager_routes, "INDUSTRY_VETO_LOCK_TIMEOUT", "200ms")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        pi = await factories.make_user(s, user_role=USER_ROLE_PI)
        mgr = await factories.make_user(s, user_role=USER_ROLE_MANAGER)
        e = PiIndustryEvidence(
            user_id=pi.id, source="openalex", kind="coauthor_company", external_id="W1:I1",
            company_name="Pfizer", company_class="pharma_biotech", year=2021, pi_role="last",
            in_tenure=True, evidence={},
        )
        s.add(e)
        await s.commit()
    try:
        async with factory() as holder:
            await holder.execute(
                text("SELECT 1 FROM pi_industry_evidence WHERE id = :i FOR UPDATE"), {"i": e.id}
            )
            async with factory() as s2:
                resp = await asyncio.wait_for(manager_routes.manager_veto_industry_evidence(
                    pi.id, e.id, request=SimpleNamespace(session={}), db=s2, current_user=mgr,
                ), timeout=10)
            assert "error=industry_busy" in resp.headers["location"]
            await holder.rollback()
        async with factory() as s:
            vetoed_at = (await s.execute(
                select(PiIndustryEvidence.vetoed_at).where(PiIndustryEvidence.id == e.id)
            )).scalar_one()
            assert vetoed_at is None
    finally:
        async with factory() as s:
            await s.execute(text("DELETE FROM users WHERE id IN (:a, :b)"),
                            {"a": pi.id, "b": mgr.id})
            await s.commit()
