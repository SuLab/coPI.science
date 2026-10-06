"""apply_profile_edits stamps human_edited_at only on a real text change (spec §6.3, D19)."""
import pytest

from src.models import USER_ROLE_MANAGER
from src.services.jhu_rules import set_tenure_start
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def _post(client, db_session, data, *, tenure=""):
    """Post the manager form at the profile's current version; assert the save succeeded
    (no error, version bumped) so a missing stamp cannot come from a refused save."""
    pi = await factories.make_user(db_session)
    profile = await factories.make_profile(db_session, user=pi, research_summary="Old summary.",
                                           techniques=["a"], keywords=["k"])
    await set_tenure_start(pi.id, 2005, "manual", db=db_session)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    await db_session.commit()
    version = profile.profile_version
    r = await client.post(f"/manager/pis/{pi.id}/profile", data={
        "name": pi.name, "email": pi.email, "institution": pi.institution or "",
        "department": "", "jhu_tenure_start": tenure,
        "profile_version": str(version), **data,
    }, headers=auth_headers(mgr.id), follow_redirects=False)
    assert r.status_code == 302
    assert "error=" not in r.headers["location"]
    await db_session.refresh(profile)
    assert profile.profile_version == version + 1
    return profile


async def test_a_changed_summary_stamps_human_edited_at(client, db_session):
    profile = await _post(client, db_session, {"research_summary": "New summary."})
    assert profile.human_edited_at is not None


async def test_an_identical_save_does_not_stamp(client, db_session):
    profile = await _post(client, db_session, {"research_summary": "Old summary."})
    assert profile.human_edited_at is None


async def test_a_tenure_only_save_does_not_stamp(client, db_session):
    profile = await _post(client, db_session, {"research_summary": "Old summary."}, tenure="2012")
    assert profile.human_edited_at is None


async def test_a_changed_tag_list_stamps(client, db_session):
    profile = await _post(client, db_session, {"research_summary": "Old summary.",
                                               "techniques": ["a", "b"],
                                               "tag_fields": ["techniques"]})
    assert profile.human_edited_at is not None
