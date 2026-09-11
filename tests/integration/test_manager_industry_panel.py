"""Manager UI: industry-interest score panel, evidence drawer + veto, PI-list
column (Task 9)."""
import pytest
from sqlalchemy import select

from src.models import USER_ROLE_MANAGER, USER_ROLE_PI, PiIndustryEvidence, PiIndustryScore
from tests import factories
from tests.integration.test_manager_access import auth_headers

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


async def test_pi_list_has_industry_column(client, db_session):
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    db_session.add(PiIndustryScore(
        user_id=pi.id, score=73.2, raw_sum=20, reason="ok", components={}, scorer_version="1.0.0",
    ))
    await db_session.commit()
    html = (await client.get("/manager/pis", headers=auth_headers(mgr.id))).text
    assert "Industry interest" in html and "73.2" in html
