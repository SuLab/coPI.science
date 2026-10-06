"""``scripts/audit_pub_dois.py --fix`` stamps ``publications.doi_verified`` from the
reconcile action (spec 2026-10-05 §6.3)."""
import pytest

from scripts import audit_pub_dois
from src.models import Publication
from tests import factories

pytestmark = pytest.mark.integration


async def test_fix_sets_doi_verified_from_the_reconcile_action(db_session, monkeypatch):
    pi = await factories.make_user(db_session)
    right = Publication(user_id=pi.id, pmid="11", title="Right", doi="10.5555/right")
    wrong = Publication(user_id=pi.id, pmid="12", title="Wrong", doi="10.5555/wrong")
    unchecked = Publication(user_id=pi.id, pmid="13", title="Unchecked", doi="10.5555/c")
    db_session.add_all([right, wrong, unchecked])
    await db_session.flush()

    async def authoritative(pmids):
        return {"11": "10.5555/right", "12": "10.5555/actual"}

    monkeypatch.setattr(audit_pub_dois, "fetch_authoritative_dois", authoritative)
    counts = await audit_pub_dois.audit(db_session, {pi.id}, True)

    assert (counts["verify"], counts["corrected"], counts["unverified"]) == (1, 1, 1)
    for row in (right, wrong, unchecked):
        await db_session.refresh(row)
    assert (right.doi, right.doi_verified) == ("10.5555/right", True)
    assert (wrong.doi, wrong.doi_verified) == ("10.5555/actual", True)
    assert (unchecked.doi, unchecked.doi_verified) == ("10.5555/c", False)

    again = await audit_pub_dois.audit(db_session, {pi.id}, False)
    assert (again["ok"], again["unverified"], again["verify"]) == (2, 1, 0)
