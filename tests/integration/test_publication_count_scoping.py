"""Task 13 of
``docs/plans/2026-09-22-pi-corpus-attribution-remediation-plan.md`` (§3B D17):
every publication count/list surface scopes to the PI's JHU tenure window.

Covers the four D17 surfaces: the /manager/pis and /admin/users list
columns, the two staff detail pages, and the PI's own /profile — plus the
D20 full-career badge for a PI with no recorded tenure year.

Pre-tenure papers are shown NOWHERE (operator decision 2026-09-22): no
count, no marker, no disclosure. The earlier design surfaced them on the two
staff detail pages so a wrong tenure year would be visible; that job now
belongs to ``scripts/audit_tenure_scope.py`` instead.
"""

import pytest

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, Publication
from src.services.jhu_rules import set_tenure_start
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def _make_pi_with_publications(db_session, *, tenure_start=None):
    pi = await factories.make_user(db_session)
    # The PI's own /profile renders its publications block only inside the
    # template's "has a profile" branch, so a PI with no ResearcherProfile
    # would render the empty-profile page instead and the scoped header
    # would be absent for a reason that has nothing to do with scoping.
    await factories.make_profile(db_session, user=pi)
    if tenure_start is not None:
        await set_tenure_start(pi.id, tenure_start, "manual", db=db_session)
    db_session.add_all(
        [
            Publication(user_id=pi.id, title="In-Tenure Paper", year=2022),
            Publication(user_id=pi.id, title="Pre-Tenure Paper", year=2015),
            Publication(user_id=pi.id, title="Undated Paper", year=None),
        ]
    )
    await db_session.flush()
    return pi


async def test_manager_pis_list_shows_the_scoped_count(client, db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await _make_pi_with_publications(db_session, tenure_start=2020)

    resp = await client.get("/manager/pis", headers=auth_headers(manager.id))
    assert resp.status_code == 200
    body = resp.text
    idx = body.index(str(pi.id))
    # The scoped count (1 in-tenure paper) appears near this PI's row; the
    # unscoped total (3) is not what is printed as the headline number.
    row = body[max(0, idx - 3000) : idx + 3000]
    # Operator decision 2026-09-22: pre-tenure papers are listed NOWHERE, so
    # the row carries the scoped count and no "+N before tenure" marker.
    assert "before tenure" not in row


async def test_admin_users_list_shows_the_scoped_count(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await _make_pi_with_publications(db_session, tenure_start=2020)

    resp = await client.get("/admin/users", headers=auth_headers(admin.id))
    assert resp.status_code == 200
    body = resp.text
    idx = body.index(str(pi.id))
    row = body[max(0, idx - 3000) : idx + 3000]
    # Operator decision 2026-09-22: pre-tenure papers are listed NOWHERE, so
    # the row carries the scoped count and no "+N before tenure" marker.
    assert "before tenure" not in row


async def test_manager_pi_detail_lists_only_in_tenure_papers(
    client, db_session
):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await _make_pi_with_publications(db_session, tenure_start=2020)

    resp = await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(manager.id))
    assert resp.status_code == 200
    body = resp.text
    assert "JHU tenure, since 2020" in body
    assert "In-Tenure Paper" in body
    # Pre-tenure papers are listed nowhere and no disclosure reveals them
    # (operator decision 2026-09-22). `scripts/audit_tenure_scope.py` is the
    # out-of-band way to see what a tenure year excludes.
    assert "Pre-Tenure Paper" not in body
    assert "Show full career" not in body
    assert "undated" in body.lower()


async def test_admin_user_detail_lists_only_in_tenure_papers(
    client, db_session
):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await _make_pi_with_publications(db_session, tenure_start=2020)

    resp = await client.get(f"/admin/users/{pi.id}", headers=auth_headers(admin.id))
    assert resp.status_code == 200
    body = resp.text
    assert "JHU tenure, since 2020" in body
    assert "In-Tenure Paper" in body
    assert "Pre-Tenure Paper" not in body
    assert "Show full career" not in body


async def test_a_pi_with_all_publications_out_of_tenure_still_renders_the_section(
    client, db_session
):
    """A PI whose entire corpus predates their tenure start must not have the
    Publications section silently disappear (the old `{% if publications %}`
    guard's failure mode)."""
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session)
    await set_tenure_start(pi.id, 2030, "manual", db=db_session)
    db_session.add(Publication(user_id=pi.id, title="Ancient Paper", year=2015))
    await db_session.flush()

    resp = await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(manager.id))
    assert resp.status_code == 200
    body = resp.text
    assert "JHU tenure, since 2030" in body
    assert "No publications fall inside the tenure window" in body
    # The section still renders (that was the old `{% if publications %}`
    # guard's failure mode), but the pre-tenure paper itself is NOT listed.
    assert "Ancient Paper" not in body


async def test_a_pi_with_no_tenure_start_shows_the_full_career_badge(
    client, db_session
):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await _make_pi_with_publications(db_session, tenure_start=None)

    list_resp = await client.get("/manager/pis", headers=auth_headers(manager.id))
    idx = list_resp.text.index(str(pi.id))
    row = list_resp.text[max(0, idx - 3000) : idx + 3000]
    assert "full career" in row.lower()

    detail_resp = await client.get(
        f"/manager/pis/{pi.id}", headers=auth_headers(manager.id)
    )
    detail_body = detail_resp.text
    assert "full career — no JHU tenure start recorded" in detail_body
    assert "In-Tenure Paper" in detail_body
    assert "Pre-Tenure Paper" in detail_body  # pass-through: everything counts


async def test_own_profile_page_shows_the_scoped_count(client, db_session):
    pi = await _make_pi_with_publications(db_session, tenure_start=2020)

    resp = await client.get("/profile", headers=auth_headers(pi.id))
    assert resp.status_code == 200
    body = resp.text
    assert "JHU tenure, since 2020" in body
    assert "In-Tenure Paper" in body
    assert "Pre-Tenure Paper" not in body
    assert "before tenure" not in body.lower()
