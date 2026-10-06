"""The manager's whole Add-PI path ends in a lab that can run (2026-10-05 audit).

F1: activation wires the lab into its ``hub-{slug}`` star spoke, on every path that
makes a lab active, so a manager-activated lab is neither isolated mid-run nor the
reason the next run start fails ``_validate_star_topology``. F2: a manager can queue
profile generation again for a dead job or an ungrounded profile. N3: Add-PI adopts a
PI account that signed in before being added. N7/F7: every Add-PI refusal rolls back.
"""
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from src.config import get_settings
from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    AgentRegistry,
    Cohort,
    CohortMembership,
    Job,
    User,
)
from src.services.agent_form import agent_form_version
from src.services.star_topology import EXTRA_SPOKE_MEMBERS
from tests import factories
from tests.integration._webui_helpers import follow
from tests.integration.test_manager_access import auth_headers
from tests.persona_support import write_persona

pytestmark = pytest.mark.integration


async def _manager(db_session):
    return await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)


async def _hub(db_session, agent_id="blackbird", status="active"):
    return await factories.make_agent(
        db_session, agent_id=agent_id, bot_name="BlackbirdBot", pi_name="Blackbird",
        role="scout_hub", status=status,
    )


async def _lab(db_session, *, agent_id, status="pending", token="xoxb-lab", grounded=True):
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    agent = await factories.make_agent(
        db_session, user=pi, agent_id=agent_id, bot_name=f"{agent_id.capitalize()}Bot",
        status=status, slack_bot_token=token,
    )
    if grounded:
        await factories.make_profile(
            db_session, user=pi, evidence_pmid_count=10, evidence_pub_count=8,
        )
    # R2: the persona-file gate (spec 2026-10-05 §6.4); every activation here needs the file
    write_persona(agent_id)
    await db_session.flush()
    return pi, agent


async def _spoke_members(db_session, slug) -> set[str] | None:
    cohort = (
        await db_session.execute(select(Cohort).where(Cohort.name == f"hub-{slug}"))
    ).scalar_one_or_none()
    if cohort is None:
        return None
    rows = await db_session.execute(
        select(CohortMembership.agent_id).where(CohortMembership.cohort_id == cohort.id)
    )
    return set(rows.scalars().all())


# --- F1: activation wires the star spoke ----------------------------------------


async def test_manager_activation_creates_the_labs_spoke(client, db_session):
    manager = await _manager(db_session)
    hub = await _hub(db_session)
    pi, agent = await _lab(db_session, agent_id="newlab")
    assert await _spoke_members(db_session, "newlab") is None

    r = await client.post(
        f"/manager/pis/{pi.id}/activate", headers=auth_headers(manager.id),
        follow_redirects=False,
    )
    assert r.headers["location"] == f"/manager/pis/{pi.id}?activated=1"
    await db_session.refresh(agent)
    assert agent.status == "active"
    assert await _spoke_members(db_session, "newlab") == {
        "newlab", hub.agent_id, *EXTRA_SPOKE_MEMBERS,
    }


async def test_an_unwireable_lab_is_refused_while_isolation_is_on(
    client, db_session, monkeypatch,
):
    """Two active hubs make the star's centre ambiguous: ensure_star_spokes
    refuses, and with isolation on the activation is refused WHOLE — the status
    flip rolls back with it."""
    monkeypatch.setattr(get_settings(), "cohort_isolation_enabled", True)
    manager = await _manager(db_session)
    await _hub(db_session)
    await _hub(db_session, agent_id="blackbird2")
    pi, agent = await _lab(db_session, agent_id="stuck")
    await db_session.commit()
    pi_id = pi.id

    r = await client.post(
        f"/manager/pis/{pi_id}/activate", headers=auth_headers(manager.id),
        follow_redirects=False,
    )
    assert r.headers["location"] == f"/manager/pis/{pi_id}"
    await db_session.refresh(agent)
    assert agent.status == "pending"
    assert await _spoke_members(db_session, "stuck") is None
    page = await follow(client, r)
    assert "could not be connected to the hub" in page.text


async def test_without_isolation_an_unwireable_lab_still_activates(
    client, db_session, monkeypatch,
):
    monkeypatch.setattr(get_settings(), "cohort_isolation_enabled", False)
    manager = await _manager(db_session)
    await _hub(db_session)
    await _hub(db_session, agent_id="blackbird2")
    pi, agent = await _lab(db_session, agent_id="openlab")
    r = await client.post(
        f"/manager/pis/{pi.id}/activate", headers=auth_headers(manager.id),
        follow_redirects=False,
    )
    assert r.headers["location"] == f"/manager/pis/{pi.id}?activated=1"
    await db_session.refresh(agent)
    assert agent.status == "active"


async def test_a_parked_second_hub_does_not_make_the_centre_ambiguous(
    client, db_session, monkeypatch,
):
    """D14 allows an inactive second hub row; the spoke goes to the active hub."""
    monkeypatch.setattr(get_settings(), "cohort_isolation_enabled", True)
    manager = await _manager(db_session)
    hub = await _hub(db_session)
    await _hub(db_session, agent_id="parkedhub", status="inactive")
    pi, agent = await _lab(db_session, agent_id="parkedcase")
    r = await client.post(
        f"/manager/pis/{pi.id}/activate", headers=auth_headers(manager.id),
        follow_redirects=False,
    )
    assert r.headers["location"] == f"/manager/pis/{pi.id}?activated=1"
    assert await _spoke_members(db_session, "parkedcase") == {
        "parkedcase", hub.agent_id, *EXTRA_SPOKE_MEMBERS,
    }


async def test_unmute_wires_a_lab_that_has_no_spoke(client, db_session):
    manager = await _manager(db_session)
    hub = await _hub(db_session)
    pi, agent = await _lab(db_session, agent_id="muted", status="inactive")
    r = await client.post(
        f"/manager/pis/{pi.id}/unmute", headers=auth_headers(manager.id),
        follow_redirects=False,
    )
    assert r.headers["location"] == f"/manager/pis/{pi.id}"
    await db_session.refresh(agent)
    assert agent.status == "active"
    assert await _spoke_members(db_session, "muted") == {
        "muted", hub.agent_id, *EXTRA_SPOKE_MEMBERS,
    }


async def test_admin_approval_wires_the_renamed_slug(client, db_session):
    """The spoke is the FINAL slug's: an admin may rename a pending agent in the same
    approval, and a spoke for the old slug would leave the live lab uncohorted."""
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    hub = await _hub(db_session)
    _pi, agent = await _lab(db_session, agent_id="oldslug")
    r = await client.post(
        f"/admin/agents/{agent.id}/approve",
        data={
            "agent_slug": "newslug", "bot_name": "NewslugBot",
            "form_version": agent_form_version(agent),
        },
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.headers["location"] == "/admin/agents"
    await db_session.refresh(agent)
    assert (agent.agent_id, agent.status) == ("newslug", "active")
    assert await _spoke_members(db_session, "newslug") == {
        "newslug", hub.agent_id, *EXTRA_SPOKE_MEMBERS,
    }
    assert await _spoke_members(db_session, "oldslug") is None


async def test_a_refused_activation_keeps_the_committed_admin_rename(
    client, db_session, monkeypatch,
):
    """D31 commits the rename and account edits before checking activation; a refused
    activation keeps those edits but leaves the agent pending and creates no spoke."""
    monkeypatch.setattr(get_settings(), "cohort_isolation_enabled", True)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    await _hub(db_session)
    await _hub(db_session, agent_id="blackbird2")
    _pi, agent = await _lab(db_session, agent_id="keepme")
    await db_session.commit()
    agent_pk = agent.id
    form = {
        "agent_slug": "renamed", "bot_name": "RenamedBot",
        "replace_slack_bot_token": "xoxb-new", "form_version": agent_form_version(agent),
    }
    r = await client.post(
        f"/admin/agents/{agent_pk}/approve", data=form,
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.headers["location"] == f"/admin/agents/{agent_pk}?activation_blocked=1"
    row = (await db_session.execute(
        select(AgentRegistry).where(AgentRegistry.id == agent_pk)
    )).scalar_one()
    await db_session.refresh(row)
    assert (row.agent_id, row.bot_name, row.slack_bot_token, row.status) == (
        "renamed", "RenamedBot", "xoxb-new", "pending",
    )
    assert await _spoke_members(db_session, "renamed") is None


async def test_an_admin_role_save_on_an_active_lab_wires_its_spoke(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    hub = await _hub(db_session)
    _pi, agent = await _lab(db_session, agent_id="rolelab", status="active")
    r = await client.post(
        f"/admin/agents/{agent.id}/role", data={"role": "pi_lab"},
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert await _spoke_members(db_session, "rolelab") == {
        "rolelab", hub.agent_id, *EXTRA_SPOKE_MEMBERS,
    }


# --- F2: the manager's profile retry ---------------------------------------------


async def test_a_dead_generation_job_can_be_retried_by_a_manager(client, db_session):
    manager = await _manager(db_session)
    pi, _agent = await _lab(db_session, agent_id="deadjob", grounded=False)
    db_session.add(Job(
        type="generate_profile", user_id=pi.id, status="dead", attempts=3,
        payload={"user_id": str(pi.id)}, last_error="ORCID 503",
    ))
    await db_session.flush()

    page = await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(manager.id))
    assert f'action="/manager/pis/{pi.id}/profile/retry"' in page.text
    assert "impersonate" not in page.text

    r = await client.post(
        f"/manager/pis/{pi.id}/profile/retry", headers=auth_headers(manager.id),
        follow_redirects=False,
    )
    assert r.headers["location"] == f"/manager/pis/{pi.id}"
    statuses = (await db_session.execute(
        select(Job.status).where(Job.user_id == pi.id, Job.type == "generate_profile")
    )).scalars().all()
    assert sorted(statuses) == ["dead", "pending"]


async def test_an_ungrounded_profile_can_be_retried(client, db_session):
    manager = await _manager(db_session)
    pi, _agent = await _lab(db_session, agent_id="thin", grounded=False)
    await factories.make_profile(
        db_session, user=pi, evidence_pmid_count=12, evidence_pub_count=0,
    )
    r = await client.post(
        f"/manager/pis/{pi.id}/profile/retry", headers=auth_headers(manager.id),
        follow_redirects=False,
    )
    assert r.status_code == 302
    jobs = (await db_session.execute(select(Job).where(Job.user_id == pi.id))).scalars().all()
    assert [j.status for j in jobs] == ["pending"]


async def test_a_grounded_profile_is_not_regenerated_from_the_retry_route(client, db_session):
    manager = await _manager(db_session)
    pi, _agent = await _lab(db_session, agent_id="fine")
    page = await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(manager.id))
    assert f'action="/manager/pis/{pi.id}/profile/retry"' not in page.text
    r = await client.post(
        f"/manager/pis/{pi.id}/profile/retry", headers=auth_headers(manager.id),
        follow_redirects=False,
    )
    assert r.status_code == 302
    jobs = (await db_session.execute(select(Job).where(Job.user_id == pi.id))).scalars().all()
    assert jobs == []


# --- N3 / N7 / F7: Add-PI ----------------------------------------------------------


_PROFILE = {"name": "Grace Hopper", "employments": []}


async def test_add_pi_adopts_a_pi_who_signed_in_before_being_added(client, db_session):
    manager = await _manager(db_session)
    signed_in = await factories.make_user(
        db_session, orcid="0000-0031-0000-0001", name="Grace Hopper",
        access_status="pending", onboarding_complete=False,
    )
    with patch(
        "src.services.pi_onboarding.fetch_orcid_profile", new=AsyncMock(return_value=_PROFILE),
    ):
        r = await client.post(
            "/manager/pis", data={"orcid": signed_in.orcid},
            headers=auth_headers(manager.id), follow_redirects=False,
        )
    assert r.headers["location"] == f"/manager/pis/{signed_in.id}"
    agent = (await db_session.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == signed_in.id)
    )).scalar_one()
    assert (agent.agent_id, agent.bot_name, agent.status) == ("hopper", "HopperBot", "pending")
    jobs = (await db_session.execute(
        select(Job.status).where(Job.user_id == signed_in.id, Job.type == "generate_profile")
    )).scalars().all()
    assert jobs == ["pending"]
    users = (await db_session.execute(
        select(User).where(User.orcid == signed_in.orcid)
    )).scalars().all()
    assert len(users) == 1, "adoption reuses the account; it never creates a second one"


@pytest.mark.parametrize("holder", ["pi_with_agent", "manager", "denied_pi"])
async def test_add_pi_still_refuses_any_other_holder_of_the_orcid(client, db_session, holder):
    manager = await _manager(db_session)
    orcid = {"pi_with_agent": "0000-0032-0000-0001", "manager": "0000-0032-0000-0002",
             "denied_pi": "0000-0032-0000-0003"}[holder]
    user = await factories.make_user(
        db_session, orcid=orcid,
        user_role=USER_ROLE_MANAGER if holder == "manager" else USER_ROLE_PI,
        access_status="denied" if holder == "denied_pi" else "allowed",
    )
    if holder == "pi_with_agent":
        await factories.make_agent(db_session, user=user, status="inactive")
    # Committed: the refusal rolls the request's work back (N7), which in this
    # suite's savepoint mode would also undo uncommitted fixtures.
    await db_session.commit()
    user_id = user.id
    fetch = AsyncMock(return_value=_PROFILE)
    with patch("src.services.pi_onboarding.fetch_orcid_profile", new=fetch):
        r = await client.post(
            "/manager/pis", data={"orcid": orcid},
            headers=auth_headers(manager.id), follow_redirects=False,
        )
    assert r.headers["location"] == "/manager/pis?error=exists"
    fetch.assert_not_awaited()
    agents = (await db_session.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == user_id)
    )).scalars().all()
    assert len(agents) == (1 if holder == "pi_with_agent" else 0)


async def test_an_identity_derivation_failure_rolls_back_instead_of_500ing(client, db_session):
    manager = await _manager(db_session)
    await db_session.commit()  # so the route's rollback undoes only its own work
    with (
        patch(
            "src.services.pi_onboarding.fetch_orcid_profile",
            new=AsyncMock(return_value=_PROFILE),
        ),
        patch(
            "src.routers.manager.create_pending_agent_for",
            new=AsyncMock(side_effect=RuntimeError("no free agent_id")),
        ),
    ):
        r = await client.post(
            "/manager/pis", data={"orcid": "0000-0033-0000-0001"},
            headers=auth_headers(manager.id), follow_redirects=False,
        )
    assert r.headers["location"] == "/manager/pis?error=create_failed"
    ghost = (await db_session.execute(
        select(User).where(User.orcid == "0000-0033-0000-0001")
    )).scalar_one_or_none()
    assert ghost is None, "the User and its job roll back with the failed agent"
