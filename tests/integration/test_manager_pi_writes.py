"""POST routes on /manager: create, edit, mute, unmute a PI (design D1)."""
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_PI, User
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def _manager(db_session):
    return await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)


async def test_pi_is_denied_all_four_write_routes(client, db_session):
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    target = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    headers = auth_headers(pi.id)

    r = await client.post("/manager/pis", data={"orcid": "0000-0003-0000-0000"}, headers=headers)
    assert r.status_code == 403
    r = await client.post(f"/manager/pis/{target.id}/profile", data={}, headers=headers)
    assert r.status_code == 403
    r = await client.post(f"/manager/pis/{target.id}/mute", headers=headers)
    assert r.status_code == 403
    r = await client.post(f"/manager/pis/{target.id}/unmute", headers=headers)
    assert r.status_code == 403


async def test_manager_creates_a_pi_via_orcid(client, db_session):
    """Mocks only the ORCID fetch (as tests/unit/test_pi_onboarding.py does),
    so the real find_or_create_pi_by_orcid — including the route's own
    db.commit() — actually runs. Asserting on the redirect alone would still
    pass even if that commit were deleted, since the User object created in
    the request's own session would still carry an id in memory; querying the
    database directly is what proves the row was actually persisted."""
    manager = await _manager(db_session)
    with patch(
        "src.services.pi_onboarding.fetch_orcid_profile",
        new=AsyncMock(return_value={
            "name": "Ada Lovelace", "email": "ada@example.edu",
            "institution": "Example University", "department": "Computing",
        }),
    ):
        r = await client.post(
            "/manager/pis", data={"orcid": "0000-0004-0000-0000"},
            headers=auth_headers(manager.id), follow_redirects=False,
        )
    assert r.status_code == 302
    assert "/manager/pis/" in r.headers["location"]

    created = (await db_session.execute(
        select(User).where(User.orcid == "0000-0004-0000-0000")
    )).scalar_one_or_none()
    assert created is not None
    assert created.name == "Ada Lovelace"
    assert created.user_role == USER_ROLE_PI
    assert f"/manager/pis/{created.id}" == r.headers["location"]


async def test_manager_create_pi_rejects_a_duplicate_orcid(client, db_session):
    manager = await _manager(db_session)
    existing = await factories.make_user(db_session, orcid="0000-0005-0000-0000")

    r = await client.post(
        "/manager/pis", data={"orcid": existing.orcid},
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert "error=" in r.headers["location"]


async def test_manager_edits_a_pi_profile(client, db_session):
    manager = await _manager(db_session)
    pi = await factories.make_user(db_session, name="Old Name")

    r = await client.post(
        f"/manager/pis/{pi.id}/profile",
        data={
            "name": "New Name", "email": pi.email or "", "institution": "",
            "department": "", "research_summary": "Edited.", "techniques": "",
            "experimental_models": "", "disease_areas": "", "key_targets": "",
            "keywords": "",
        },
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.status_code == 302
    await db_session.refresh(pi)
    assert pi.name == "New Name"


async def test_manager_edit_404s_on_a_non_pi_target(client, db_session):
    manager = await _manager(db_session)
    other_admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)

    r = await client.post(
        f"/manager/pis/{other_admin.id}/profile", data={}, headers=auth_headers(manager.id),
    )
    assert r.status_code == 404


async def test_manager_mute_404s_on_a_non_pi_target(client, db_session):
    """The 404 guard must run before the agent lookup, not just happen to
    404 because there's no agent to find — a real AgentRegistry row on the
    non-PI target proves that ordering."""
    manager = await _manager(db_session)
    other_admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    await factories.make_agent(db_session, user=other_admin, status="active")

    r = await client.post(
        f"/manager/pis/{other_admin.id}/mute", headers=auth_headers(manager.id),
    )
    assert r.status_code == 404


async def test_manager_unmute_404s_on_a_non_pi_target(client, db_session):
    manager = await _manager(db_session)
    other_admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    await factories.make_agent(db_session, user=other_admin, status="inactive")

    r = await client.post(
        f"/manager/pis/{other_admin.id}/unmute", headers=auth_headers(manager.id),
    )
    assert r.status_code == 404


async def test_manager_mutes_and_unmutes_a_pi(client, db_session):
    manager = await _manager(db_session)
    pi = await factories.make_user(db_session)
    agent = await factories.make_agent(db_session, user=pi, status="active")

    r = await client.post(
        f"/manager/pis/{pi.id}/mute", headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.status_code == 302
    await db_session.refresh(agent)
    assert agent.status == "inactive"
    assert agent.muted_by == manager.id

    r = await client.post(
        f"/manager/pis/{pi.id}/unmute", headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.status_code == 302
    await db_session.refresh(agent)
    assert agent.status == "active"
    assert agent.muted_by is None


async def test_muting_a_pi_with_no_agent_redirects_with_an_error(client, db_session):
    manager = await _manager(db_session)
    pi = await factories.make_user(db_session)

    r = await client.post(
        f"/manager/pis/{pi.id}/mute", headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert "error=" in r.headers["location"]


async def test_muting_a_pending_agent_redirects_with_an_error(client, db_session):
    manager = await _manager(db_session)
    pi = await factories.make_user(db_session)
    await factories.make_agent(db_session, user=pi, status="pending")

    r = await client.post(
        f"/manager/pis/{pi.id}/mute", headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert "error=" in r.headers["location"]


async def test_pis_page_shows_an_add_pi_form(client, db_session):
    manager = await _manager(db_session)
    r = await client.get("/manager/pis", headers=auth_headers(manager.id))
    assert r.status_code == 200
    assert '<form' in r.text and 'action="/manager/pis"' in r.text
    assert 'name="orcid"' in r.text


async def test_pi_detail_shows_mute_button_for_an_active_agent(client, db_session):
    manager = await _manager(db_session)
    pi = await factories.make_user(db_session)
    await factories.make_agent(db_session, user=pi, status="active")

    r = await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(manager.id))
    assert r.status_code == 200
    assert f'/manager/pis/{pi.id}/mute' in r.text


async def test_pi_detail_hides_mute_button_for_a_pending_agent(client, db_session):
    manager = await _manager(db_session)
    pi = await factories.make_user(db_session)
    await factories.make_agent(db_session, user=pi, status="pending")

    r = await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(manager.id))
    assert r.status_code == 200
    assert f'/manager/pis/{pi.id}/mute' not in r.text


_ORCID_PROFILE = {
    "name": "Ada Lovelace", "email": "ada2@example.edu",
    "institution": "Johns Hopkins University", "department": "Computing",
    "employments": [
        {"organization": "Johns Hopkins University",
         "start_year": 2019, "current": True},
    ],
}


async def test_manager_create_pi_also_mints_a_pending_agent_and_tenure_entry(
    client, db_session
):
    """The 2026-08-24 Add-PI auto-flow: one POST creates the User, the
    generate_profile Job, a PENDING (inert) AgentRegistry row, and the
    employment-derived JHU tenure entry — atomically, in one commit, so the
    worker can never run the job before the agent row exists (the README's
    seed-then-create-row ordering gap)."""
    from src.models import AgentRegistry, Job
    from src.services.jhu_rules import get_tenure_start

    manager = await _manager(db_session)
    with patch(
        "src.services.pi_onboarding.fetch_orcid_profile",
        new=AsyncMock(return_value=dict(_ORCID_PROFILE)),
    ):
        r = await client.post(
            "/manager/pis", data={"orcid": "0000-0007-0000-0000"},
            headers=auth_headers(manager.id), follow_redirects=False,
        )
    assert r.status_code == 302

    created = (await db_session.execute(
        select(User).where(User.orcid == "0000-0007-0000-0000")
    )).scalar_one()

    agent = (await db_session.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == created.id)
    )).scalar_one_or_none()
    assert agent is not None, "the Add-PI flow must mint the agent row"
    assert agent.status == "pending"
    assert agent.agent_id == "lovelace"
    assert agent.user_id != manager.id, "D7: the bot belongs to the PI, never the manager"

    jobs = (await db_session.execute(
        select(Job).where(Job.user_id == created.id)
    )).scalars().all()
    assert len(jobs) == 1

    assert await get_tenure_start(db_session, created.id) == 2019


async def test_manager_create_pi_rejects_a_malformed_orcid_with_a_canned_code(
    client, db_session
):
    manager = await _manager(db_session)
    fetch = AsyncMock()
    with patch("src.services.pi_onboarding.fetch_orcid_profile", new=fetch):
        r = await client.post(
            "/manager/pis", data={"orcid": "0000-0001; DROP[Affiliation]"},
            headers=auth_headers(manager.id), follow_redirects=False,
        )
    assert r.status_code == 302
    assert r.headers["location"] == "/manager/pis?error=invalid_orcid", (
        "raw exception text must not be interpolated into the redirect"
    )
    fetch.assert_not_awaited()

    ghosts = (await db_session.execute(
        select(User).where(User.orcid.like("0000-0001%"))
    )).scalars().all()
    assert ghosts == []


async def test_manager_create_pi_maps_known_failures_to_canned_codes(
    client, db_session
):
    manager = await _manager(db_session)
    existing = await factories.make_user(db_session, orcid="0000-0008-0000-0000")

    r = await client.post(
        "/manager/pis", data={"orcid": existing.orcid},
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.headers["location"] == "/manager/pis?error=exists"

    with patch(
        "src.services.pi_onboarding.fetch_orcid_profile",
        new=AsyncMock(side_effect=RuntimeError("ORCID down")),
    ):
        r = await client.post(
            "/manager/pis", data={"orcid": "0000-0009-0000-0000"},
            headers=auth_headers(manager.id), follow_redirects=False,
        )
    assert r.headers["location"] == "/manager/pis?error=fetch_failed"


async def test_a_collision_race_rolls_back_cleanly_instead_of_500ing(
    client, db_session
):
    """Two managers adding same-surname PIs can race derive_agent_identity's
    SELECT-then-INSERT; the loser's IntegrityError must roll the whole
    creation back (no half-created PI) and answer with a canned error."""
    from sqlalchemy.exc import IntegrityError

    manager = await _manager(db_session)
    with (
        patch(
            "src.services.pi_onboarding.fetch_orcid_profile",
            new=AsyncMock(return_value=dict(_ORCID_PROFILE)),
        ),
        patch(
            "src.routers.manager.create_pending_agent_for",
            new=AsyncMock(side_effect=IntegrityError("dup", None, Exception())),
        ),
    ):
        r = await client.post(
            "/manager/pis", data={"orcid": "0000-0010-0000-0000"},
            headers=auth_headers(manager.id), follow_redirects=False,
        )
    assert r.status_code == 302
    assert r.headers["location"] == "/manager/pis?error=agent_conflict"

    ghost = (await db_session.execute(
        select(User).where(User.orcid == "0000-0010-0000-0000")
    )).scalar_one_or_none()
    assert ghost is None, "the User row must roll back with the agent row"


async def test_pi_detail_shows_agent_state_and_the_admin_deep_link_to_admins_only(
    client, db_session
):
    """The pending-agent header: job-state-aware copy (a dead generation job
    must not read as 'awaiting Slack install'), with the /admin/agents/{uuid}
    deep link rendered only when the viewer is an admin — a manager gets told
    to ask one, since provisioning is admin-only by design (FD-6)."""
    from src.models import USER_ROLE_ADMIN, Job

    manager = await _manager(db_session)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(
        db_session, user_role=USER_ROLE_PI, orcid="0000-0012-0000-0000",
        name="Pending Pi",
    )
    agent = await factories.make_agent(
        db_session, user=pi, agent_id="pendingpi", bot_name="PendingPiBot",
        status="pending",
    )
    job = Job(type="generate_profile", user_id=pi.id,
              payload={"user_id": str(pi.id)}, status="pending")
    db_session.add(job)
    await db_session.flush()

    r = await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(manager.id))
    assert r.status_code == 200
    assert "Profile generating" in r.text
    assert f"/admin/agents/{agent.id}" not in r.text
    assert "ask an admin" in r.text.lower()

    job.status = "dead"
    await db_session.flush()
    r = await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(manager.id))
    assert "generation failed" in r.text.lower()
    assert "awaiting Slack install" not in r.text

    job.status = "completed"
    await db_session.flush()
    r = await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(admin.id))
    assert "Awaiting Slack install" in r.text
    assert f"/admin/agents/{agent.id}" in r.text, (
        "an admin viewer gets the provisioning deep link"
    )


async def test_manager_can_correct_the_tenure_year_from_the_edit_form(
    client, db_session
):
    from src.services.jhu_rules import get_tenure_start

    manager = await _manager(db_session)
    pi = await factories.make_user(
        db_session, user_role=USER_ROLE_PI, orcid="0000-0013-0000-0000",
    )
    r = await client.post(
        f"/manager/pis/{pi.id}/profile",
        data={"name": pi.name, "research_summary": "S", "jhu_tenure_start": "2014"},
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.status_code == 302 and "saved=1" in r.headers["location"]
    assert await get_tenure_start(db_session, pi.id) == 2014
