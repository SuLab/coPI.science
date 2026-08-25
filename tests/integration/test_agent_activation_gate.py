"""The activation gate on admin_approve_agent (audit H4, coverage plan P3).

Auto-created pending agents broke the old structural guarantee that a pending
row implies a completed profile (`/agent/request` required one; the manager
Add-PI flow mints the row before the job even runs). The admin's provisioning
habit is bulk and trusting (slack_install_links.md), so the refusal must live
in the HANDLER — on BOTH status branches, the pending→active approval and the
edit form's status dropdown (the bypass P3 warned about) — not in a warning
panel a bulk workflow never renders. `pi_lab`-scoped: the hub and specialists
have no PI profile by design. An explicit override checkbox exists and is
logged; silence is not an option, activation-by-default is not either.
"""

import pytest

from src.models import USER_ROLE_ADMIN, Job
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def _admin(db_session):
    return await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)


def _approve_form(agent, **extra):
    form = {"agent_slug": agent.agent_id, "bot_name": agent.bot_name}
    form.update(extra)
    return form


async def _pending_pi_agent(db_session, *, orcid, agent_id, profile=None, job_status=None):
    user = await factories.make_user(db_session, orcid=orcid)
    agent = await factories.make_agent(
        db_session, user=user, agent_id=agent_id,
        bot_name=f"{agent_id.capitalize()}Bot", status="pending",
    )
    if profile is not None:
        await factories.make_profile(db_session, user=user, **profile)
    if job_status is not None:
        db_session.add(
            Job(type="generate_profile", user_id=user.id, status=job_status,
                payload={"user_id": str(user.id)})
        )
    await db_session.flush()
    return user, agent


async def test_a_profile_less_agent_is_refused_activation(client, db_session):
    admin = await _admin(db_session)
    user, agent = await _pending_pi_agent(
        db_session, orcid="0000-0020-0000-0001", agent_id="noprofile",
    )
    r = await client.post(
        f"/admin/agents/{agent.id}/approve", data=_approve_form(agent),
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert "activation_blocked" in r.headers["location"]

    await db_session.refresh(agent)
    assert agent.status == "pending", "the refusal must not half-apply"
    assert agent.approved_at is None


async def test_an_ungrounded_profile_is_refused_activation(client, db_session):
    admin = await _admin(db_session)
    user, agent = await _pending_pi_agent(
        db_session, orcid="0000-0020-0000-0002", agent_id="ungrounded",
        profile={"evidence_pmid_count": 10, "evidence_pub_count": 0},
        job_status="completed",
    )
    r = await client.post(
        f"/admin/agents/{agent.id}/approve", data=_approve_form(agent),
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert "activation_blocked" in r.headers["location"]
    await db_session.refresh(agent)
    assert agent.status == "pending"


async def test_a_dead_generation_job_blocks_even_a_grounded_profile(
    client, db_session
):
    admin = await _admin(db_session)
    user, agent = await _pending_pi_agent(
        db_session, orcid="0000-0020-0000-0003", agent_id="deadjob",
        profile={"evidence_pmid_count": 10, "evidence_pub_count": 8},
        job_status="dead",
    )
    r = await client.post(
        f"/admin/agents/{agent.id}/approve", data=_approve_form(agent),
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert "activation_blocked" in r.headers["location"]
    await db_session.refresh(agent)
    assert agent.status == "pending"


async def test_a_grounded_agent_with_a_healthy_job_activates(client, db_session):
    admin = await _admin(db_session)
    user, agent = await _pending_pi_agent(
        db_session, orcid="0000-0020-0000-0004", agent_id="healthy",
        profile={"evidence_pmid_count": 10, "evidence_pub_count": 8},
        job_status="completed",
    )
    r = await client.post(
        f"/admin/agents/{agent.id}/approve", data=_approve_form(agent),
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert "activation_blocked" not in r.headers["location"]
    await db_session.refresh(agent)
    assert agent.status == "active"
    assert agent.approved_at is not None
    assert agent.approved_by == admin.id


async def test_the_logged_override_activates_despite_blockers(client, db_session):
    admin = await _admin(db_session)
    user, agent = await _pending_pi_agent(
        db_session, orcid="0000-0020-0000-0005", agent_id="overridden",
    )
    r = await client.post(
        f"/admin/agents/{agent.id}/approve",
        data=_approve_form(agent, activation_override="1"),
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert "activation_blocked" not in r.headers["location"]
    await db_session.refresh(agent)
    assert agent.status == "active"


async def test_the_status_dropdown_branch_is_gated_too(client, db_session):
    """P3's bypass: an already-approved agent flipped inactive→active through
    the edit form's dropdown must pass the same gate."""
    admin = await _admin(db_session)
    user, agent = await _pending_pi_agent(
        db_session, orcid="0000-0020-0000-0006", agent_id="parked",
    )
    agent.status = "inactive"
    await db_session.flush()

    r = await client.post(
        f"/admin/agents/{agent.id}/approve",
        data=_approve_form(agent, agent_status="active"),
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert "activation_blocked" in r.headers["location"]
    await db_session.refresh(agent)
    assert agent.status == "inactive"

    # ...but a non-activating edit (inactive → suspended) is not blocked.
    r = await client.post(
        f"/admin/agents/{agent.id}/approve",
        data=_approve_form(agent, agent_status="suspended"),
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert "activation_blocked" not in r.headers["location"]
    await db_session.refresh(agent)
    assert agent.status == "suspended"


async def test_non_pi_lab_roles_are_exempt(client, db_session):
    admin = await _admin(db_session)
    agent = await factories.make_agent(
        db_session, agent_id="blackbird2", bot_name="Blackbird2Bot",
        status="pending", role="scout_hub",
    )
    r = await client.post(
        f"/admin/agents/{agent.id}/approve", data=_approve_form(agent),
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert "activation_blocked" not in r.headers["location"]
    await db_session.refresh(agent)
    assert agent.status == "active"


async def test_the_detail_page_shows_the_evidence_panel(client, db_session):
    admin = await _admin(db_session)
    user, agent = await _pending_pi_agent(
        db_session, orcid="0000-0020-0000-0007", agent_id="paneled",
        profile={"evidence_pmid_count": 10, "evidence_pub_count": 0},
        job_status="dead",
    )
    r = await client.get(
        f"/admin/agents/{agent.id}", headers=auth_headers(admin.id)
    )
    assert r.status_code == 200
    assert "evidence_lost" in r.text
    assert "activate anyway" in r.text.lower()
