"""Manager UI: industry-interest score panel, evidence drawer + veto, PI-list
column (Task 9)."""
import pytest
from sqlalchemy import select

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    PiIndustryEvidence,
    PiIndustryScore,
)
from tests import factories
from tests.integration.test_manager_access import _session_cookie, auth_headers

pytestmark = pytest.mark.integration


async def test_unscored_pi_shows_reason_not_zero(client, db_session):
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    db_session.add(PiIndustryScore(
        user_id=pi.id, score=None, reason="no_tenure_start", components={}, scorer_version="1.0.0",
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
        user_id=pi.id, score=50.0, raw_sum=4.5, reason="ok", components={},
        scorer_version="1.0.0", tenure_start_used=2018,
    ))
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
    # rescore produced a no-evidence row instead of trusting timestamp order.
    scores = (await db_session.execute(
        select(PiIndustryScore).where(PiIndustryScore.user_id == pi.id)
    )).scalars().all()
    assert any(s.reason == "no_evidence" and s.raw_sum == 0 for s in scores)


async def test_veto_is_idempotent_on_replay(client, db_session):
    """Fix round 1, ruling 4: a replayed POST on an already-vetoed row must
    not rescore a second time."""
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
    """Fix round 1, ruling 1: vetoed_by_user_id names the worn (impersonated)
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

    headers = {"Cookie": f"copi-session={_session_cookie(admin.id)}; copi-impersonate={mgr.id}"}
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
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    db_session.add(PiIndustryScore(
        user_id=pi.id, score=73.2, raw_sum=20, reason="ok", components={}, scorer_version="1.0.0",
    ))
    await db_session.commit()
    html = (await client.get("/manager/pis", headers=auth_headers(mgr.id))).text
    assert "Industry interest" in html and "73.2" in html
