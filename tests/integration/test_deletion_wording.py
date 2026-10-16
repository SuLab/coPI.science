"""Both deletion pages say the simulation logs keep profile text (spec 2026-10-05 D35)."""
import html
import re

import pytest

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_REVIEWER, Publication
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def test_the_self_service_page_mentions_the_logs(client, db_session):
    pi = await factories.make_user(db_session)
    body = (await client.get("/profile/delete-account", headers=auth_headers(pi.id))).text
    assert "model-call logs are also kept" in body


async def test_the_admin_page_mentions_the_logs(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session)
    body = (await client.get(f"/workspace/pis/{pi.id}", headers=auth_headers(admin.id))).text
    assert "simulation's model-call logs remain" in body


def _dialog(body: str, name: str) -> str:
    """The Staff page's delete confirmation for ``name``, unescaped."""
    match = re.search(rf'data-confirm="(Delete reviewer {re.escape(name)}\?[^"]*)"', body)
    assert match, f"no delete dialog for {name}"
    return html.unescape(match.group(1))


async def _reviewer_with_research_data(db, name="Rae Viewer"):
    rev = await factories.make_user(db, user_role=USER_ROLE_REVIEWER, name=name)
    await factories.make_profile(db, user=rev)
    db.add_all([Publication(user_id=rev.id, pmid="31000001", title="One"),
                Publication(user_id=rev.id, pmid="31000002", title="Two")])
    await factories.make_agent(db, user=rev, status="suspended")
    await db.flush()
    return rev


async def test_the_staff_dialog_lists_research_data_for_an_admin(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    await _reviewer_with_research_data(db_session)
    body = (await client.get("/workspace/staff", headers=auth_headers(admin.id))).text
    dialog = _dialog(body, "Rae Viewer")
    for phrase in ("its researcher profile", "its 2 stored publications",
                   "its lab agent's persona files (the agent is suspended and its Slack bot token revoked)",
                   "copies of its profile text in the model-call logs"):
        assert phrase in dialog


async def test_the_staff_dialog_without_research_data_is_the_base_text(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER, name="Plain Rev")
    body = (await client.get("/workspace/staff", headers=auth_headers(admin.id))).text
    dialog = _dialog(body, "Plain Rev")
    assert "Removed: the account" in dialog
    assert "researcher profile" not in dialog and "model-call logs" not in dialog


async def test_a_manager_sees_the_admin_label_not_delete(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    rev = await _reviewer_with_research_data(db_session)
    body = (await client.get("/workspace/staff", headers=auth_headers(mgr.id))).text
    assert f"/workspace/staff/{rev.id}/delete" not in body
    assert "Holds research data: an admin deletes it" in body


async def test_the_non_pi_refusal_panel_claims_no_purge_fact(client, db_session):
    rev = await _reviewer_with_research_data(db_session)
    body = (await client.get("/profile/delete-account", headers=auth_headers(rev.id))).text
    assert "archived" not in body.lower()
    assert "linked to someone else" not in body.lower()


async def test_the_self_delete_page_sends_research_data_to_an_admin(client, db_session):
    rev = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    await factories.make_profile(db_session, user=rev)
    db_session.add_all([Publication(user_id=rev.id, pmid="31000001", title="One"),
                        Publication(user_id=rev.id, pmid="31000002", title="Two")])
    await db_session.flush()
    body = html.unescape(
        (await client.get("/profile/delete-account", headers=auth_headers(rev.id))).text)
    for phrase in ("You cannot delete this account yourself.",
                   "It holds research data: a researcher profile and 2 stored publications.",
                   "Ask an admin to delete your account.", "model-call logs are also kept"):
        assert phrase in body
    assert 'action="/profile/delete-account"' not in body
    assert "Delete My Account" not in body


async def test_the_non_pi_self_delete_page_without_research_data_is_unchanged(client, db_session):
    rev = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    body = (await client.get("/profile/delete-account", headers=auth_headers(rev.id))).text
    assert 'action="/profile/delete-account"' in body
    for phrase in ("researcher profile", "model-call logs", "You cannot delete"):
        assert phrase not in body


async def test_an_unknown_error_value_shows_no_banner(client, db_session):
    rev = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    body = (await client.get("/profile/delete-account?error=research_data",
                             headers=auth_headers(rev.id))).text
    assert "exactly to confirm" not in body
    assert 'action="/profile/delete-account"' in body


async def _three_deletion_pages(client, db):
    admin = await factories.make_user(db, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db)
    reviewer = await factories.make_user(db, user_role=USER_ROLE_REVIEWER)
    return [
        html.unescape((await client.get("/profile/delete-account",
                                        headers=auth_headers(pi.id))).text),
        html.unescape((await client.get(f"/workspace/pis/{pi.id}",
                                        headers=auth_headers(admin.id))).text),
        html.unescape((await client.get(f"/admin/users/{reviewer.id}",
                                        headers=auth_headers(admin.id))).text),
    ]


async def test_deletion_pages_name_archived_copies(client, db_session):
    """Only owner-tagged archived copies are purged (R5.21): no page promises "any" copy, and
    each says an untagged one is kept."""
    for body in await _three_deletion_pages(client, db_session):
        assert "archived copy" in body
        assert "any archived copy" not in body
        assert "records no owner is kept" in " ".join(body.split())


async def test_deletion_pages_qualify_unowned_revisions(client, db_session):
    """Unstamped revisions are purged only for the agent held at deletion (R5.19)."""
    pages = await _three_deletion_pages(client, db_session)
    assert "deleted only with the lab agent you hold now" in pages[0]
    for body in pages[1:]:
        assert "older revisions with no recorded owner of the agent they hold now" in " ".join(
            body.split())


async def test_deletion_pages_name_relinked_revisions(client, db_session):
    for body in await _three_deletion_pages(client, db_session):
        assert "linked to someone else" in body


async def test_staff_dialog_names_the_purge_facts_for_research_data(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    with_profile = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER,
                                             name="Rhea Profile")
    await factories.make_profile(db_session, user=with_profile)
    await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER, name="Paul Plain")
    body = (await client.get("/workspace/staff", headers=auth_headers(admin.id))).text
    rich, plain = _dialog(body, "Rhea Profile"), _dialog(body, "Paul Plain")
    for phrase in ("every profile revision recorded as written while it owned a lab agent",
                   "every archived copy of its persona file recorded as its own"):
        assert phrase in rich
        assert phrase not in plain
