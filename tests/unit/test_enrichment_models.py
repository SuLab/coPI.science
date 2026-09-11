import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.models import PiGrant, PiIndustryEvidence, PiIndustryScore, User

pytestmark = pytest.mark.integration


async def _user(db):
    u = User(orcid=f"0000-0001-{uuid.uuid4().hex[:4]}-{uuid.uuid4().hex[:4]}", name="T", user_role="pi")
    db.add(u)
    await db.flush()
    return u


async def test_grant_rows_are_unique_per_user_and_core(db_session):
    u = await _user(db_session)
    db_session.add(PiGrant(user_id=u.id, core_project_num="R01AI137329", title="x",
                           org_name="JOHNS HOPKINS UNIVERSITY", first_fy=2019, last_fy=2024,
                           tenure_filter_mode="org_and_year", identity_evidence={"pmid_links": 3}))
    await db_session.flush()
    db_session.add(PiGrant(user_id=u.id, core_project_num="R01AI137329", title="dup",
                           org_name="JOHNS HOPKINS UNIVERSITY", tenure_filter_mode="org_and_year"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_score_row_allows_null_score_with_reason(db_session):
    u = await _user(db_session)
    db_session.add(PiIndustryScore(user_id=u.id, score=None, reason="no_tenure_start",
                                   scorer_version="1.0.0", components={}))
    await db_session.flush()
    row = (await db_session.execute(select(PiIndustryScore).where(PiIndustryScore.user_id == u.id))).scalar_one()
    assert row.score is None and row.reason == "no_tenure_start"


async def test_evidence_row_defaults_not_vetoed(db_session):
    u = await _user(db_session)
    e = PiIndustryEvidence(user_id=u.id, source="openalex", kind="coauthor_company",
                           external_id="W1", company_name="Paratek Pharmaceuticals",
                           company_class="pharma_biotech", year=2021, in_tenure=True, evidence={})
    db_session.add(e)
    await db_session.flush()
    assert e.vetoed_at is None
