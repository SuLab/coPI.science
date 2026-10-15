"""Encoded links on the PI's profile page and the staff PI page (spec 2026-10-15 §10 R5.12,
R5.13; F22)."""
import pytest

from src.models import USER_ROLE_MANAGER, Publication
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration
SICI = "10.1002/(SICI)1097-4636(199601)30:1<1::AID-JBM1>3.0.CO;2-#"


async def _pi_with(db, *rows):
    pi = await factories.make_user(db)
    await factories.make_profile(db, user=pi)
    db.add_all([Publication(user_id=pi.id, **row) for row in rows])
    await db.flush()
    return pi


async def test_profile_page_encodes_doi_href(client, db_session):
    pi = await _pi_with(db_session, dict(pmid=None, doi=SICI, title="SICI paper", year=1996))
    body = (await client.get("/profile", headers=auth_headers(pi.id))).text
    assert ('href="https://doi.org/10.1002/(SICI)1097-4636(199601)30:1%3C1::AID-JBM1%3E3.0.CO;'
            '2-%23"') in body


async def test_profile_page_has_no_doi_link_for_non_doi(client, db_session):
    pi = await _pi_with(db_session, dict(pmid=None, doi="not-a-doi", title="Unlinked paper",
                                         year=2020))
    body = (await client.get("/profile", headers=auth_headers(pi.id))).text
    assert "Unlinked paper" in body
    assert 'href="https://doi.org/' not in body


async def test_staff_page_links_only_numeric_pmids(client, db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await _pi_with(db_session,
                        dict(pmid="123", title="Numbered paper", year=2021),
                        dict(pmid=None, doi="N/A", title="Doi-less paper", year=2020))
    body = (await client.get(f"/workspace/pis/{pi.id}", headers=auth_headers(manager.id))).text
    assert 'href="https://pubmed.ncbi.nlm.nih.gov/123/"' in body
    assert "Doi-less paper" in body
    assert "pubmed.ncbi.nlm.nih.gov/None" not in body
    assert 'href="https://doi.org/' not in body
