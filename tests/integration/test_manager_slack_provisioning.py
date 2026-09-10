"""F2: a manager may install a PI's Slack bot and activate the agent from
/manager/pis/{id}, without an admin round-trip.

The Slack redirect URI ``/admin/agents/slack/callback`` is baked into every
Slack app manifest already issued, so the path does not move — only its gate
widens (admin → staff) and its redirects branch on the caller's role. The
bridge row records who started the install (``initiated_by_user_id``,
migration 0046) so a second staff account cannot land someone else's token.
"""

import pytest
from sqlalchemy import select

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    USER_ROLE_REVIEWER,
    SlackAppProvision,
)
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def _manager(db_session):
    return await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)


async def _pending_pi(db_session, *, agent_id="pendingpi", **agent_kwargs):
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    agent = await factories.make_agent(
        db_session, user=pi, agent_id=agent_id,
        bot_name=f"{agent_id.capitalize()}Bot", status="pending", **agent_kwargs,
    )
    await db_session.flush()
    return pi, agent


async def test_manager_can_start_provisioning_for_a_pending_pi(
    client, db_session, monkeypatch
):
    manager = await _manager(db_session)
    pi, agent = await _pending_pi(db_session, agent_id="startprov")
    seen = {}

    async def fake_start(db, a, *, initiated_by):
        seen["agent_id"] = a.id
        seen["initiated_by"] = initiated_by.id
        return "https://slack.test/authorize?x=1"

    monkeypatch.setattr("src.routers.manager.start_provisioning", fake_start)
    r = await client.post(
        f"/manager/pis/{pi.id}/slack/provision",
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == "https://slack.test/authorize?x=1"
    assert seen == {"agent_id": agent.id, "initiated_by": manager.id}


async def test_provisioning_failure_returns_to_the_manager_page(
    client, db_session, monkeypatch
):
    from src.services.admin_provisioning import ProvisioningError

    manager = await _manager(db_session)
    pi, _agent = await _pending_pi(db_session, agent_id="provfail")

    async def fake_start(db, a, *, initiated_by):
        raise ProvisioningError("Could not create the Slack app: boom")

    monkeypatch.setattr("src.routers.manager.start_provisioning", fake_start)
    r = await client.post(
        f"/manager/pis/{pi.id}/slack/provision",
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith(f"/manager/pis/{pi.id}?slack_error=")
    assert " " not in loc, "the message must be percent-encoded into the Location"


async def test_reviewer_and_impersonating_admin_are_refused(client, db_session):
    manager = await _manager(db_session)
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi, _agent = await _pending_pi(db_session, agent_id="refused")

    for path in ("slack/provision", "activate"):
        r = await client.post(
            f"/manager/pis/{pi.id}/{path}",
            headers=auth_headers(reviewer.id), follow_redirects=False,
        )
        assert r.status_code == 403, path

        headers = auth_headers(admin.id)
        headers["Cookie"] += f"; copi-impersonate={manager.id}"
        r = await client.post(
            f"/manager/pis/{pi.id}/{path}", headers=headers, follow_redirects=False,
        )
        assert r.status_code == 403, path


async def test_callback_completes_for_the_initiating_manager(
    client, db_session, monkeypatch
):
    manager = await _manager(db_session)
    pi, agent = await _pending_pi(db_session, agent_id="cbmanager")
    db_session.add(SlackAppProvision(
        agent_registry_id=agent.id, state="s1",
        client_id="cid", client_secret="secret",
        initiated_by_user_id=manager.id,
    ))
    await db_session.flush()
    monkeypatch.setattr(
        "src.services.admin_provisioning.exchange_code",
        lambda *a, **k: "xoxb-manager",
    )

    r = await client.get(
        "/admin/agents/slack/callback?code=c&state=s1",
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == f"/manager/pis/{pi.id}?slack_ok=1"
    await db_session.refresh(agent)
    assert agent.slack_bot_token == "xoxb-manager"


async def test_callback_redirects_an_admin_to_the_admin_surface(
    client, db_session, monkeypatch
):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    _pi, agent = await _pending_pi(db_session, agent_id="cbadmin")
    db_session.add(SlackAppProvision(
        agent_registry_id=agent.id, state="s2",
        client_id="cid", client_secret="secret",
        initiated_by_user_id=admin.id,
    ))
    await db_session.flush()
    monkeypatch.setattr(
        "src.services.admin_provisioning.exchange_code",
        lambda *a, **k: "xoxb-admin",
    )

    r = await client.get(
        "/admin/agents/slack/callback?code=c&state=s2",
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == f"/admin/agents/{agent.id}?slack_ok=1"


async def test_callback_refuses_a_different_user(client, db_session, monkeypatch):
    manager = await _manager(db_session)
    other = await _manager(db_session)
    _pi, agent = await _pending_pi(db_session, agent_id="cbother")
    db_session.add(SlackAppProvision(
        agent_registry_id=agent.id, state="s3",
        client_id="cid", client_secret="secret",
        initiated_by_user_id=manager.id,
    ))
    await db_session.flush()

    def _never(*a, **k):
        raise AssertionError("the code must not be exchanged for a stranger")

    monkeypatch.setattr("src.services.admin_provisioning.exchange_code", _never)
    r = await client.get(
        "/admin/agents/slack/callback?code=c&state=s3",
        headers=auth_headers(other.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"].startswith("/manager/pis?slack_error=")
    await db_session.refresh(agent)
    assert agent.slack_bot_token is None


async def test_manager_activate_is_gated_and_has_no_override(client, db_session):
    manager = await _manager(db_session)
    pi, agent = await _pending_pi(
        db_session, agent_id="activateme", slack_bot_token="xoxb-x",
    )

    r = await client.post(
        f"/manager/pis/{pi.id}/activate",
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == f"/manager/pis/{pi.id}?activation_blocked=1"
    await db_session.refresh(agent)
    assert agent.status == "pending"

    # The override an admin has is deliberately not offered here: a manager
    # only gets past the gate by fixing the profile.
    r = await client.post(
        f"/manager/pis/{pi.id}/activate", data={"override": "on"},
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.headers["location"] == f"/manager/pis/{pi.id}?activation_blocked=1"
    await db_session.refresh(agent)
    assert agent.status == "pending"

    await factories.make_profile(
        db_session, user=pi, evidence_pmid_count=10, evidence_pub_count=8,
    )
    await db_session.flush()
    r = await client.post(
        f"/manager/pis/{pi.id}/activate",
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.headers["location"] == f"/manager/pis/{pi.id}?activated=1"
    await db_session.refresh(agent)
    assert agent.status == "active"
    assert agent.approved_by == manager.id


async def test_activate_without_a_slack_token_is_refused(client, db_session):
    manager = await _manager(db_session)
    pi, agent = await _pending_pi(db_session, agent_id="notoken")
    await factories.make_profile(
        db_session, user=pi, evidence_pmid_count=10, evidence_pub_count=8,
    )
    await db_session.flush()

    r = await client.post(
        f"/manager/pis/{pi.id}/activate",
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert "slack_error" in r.headers["location"]
    await db_session.refresh(agent)
    assert agent.status == "pending"


async def test_a_non_pending_agent_is_not_provisionable_from_the_manager_page(
    client, db_session
):
    manager = await _manager(db_session)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    await factories.make_agent(
        db_session, user=pi, agent_id="alreadyactive",
        bot_name="AlreadyActiveBot", status="active",
    )
    await db_session.flush()
    for path in ("slack/provision", "activate"):
        r = await client.post(
            f"/manager/pis/{pi.id}/{path}",
            headers=auth_headers(manager.id), follow_redirects=False,
        )
        assert r.status_code == 404, path


async def test_a_non_pi_lab_agent_is_not_reachable_from_the_manager_page(
    client, db_session
):
    """A `pi` user hand-linked on /admin/agents to a hub or specialist row must
    not be activatable here: `activation_blockers` short-circuits to [] for any
    role but `pi_lab`, so the gate would wave it through with no profile check
    at all."""
    manager = await _manager(db_session)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    await factories.make_agent(
        db_session, user=pi, agent_id="hublike", bot_name="HubLikeBot",
        status="pending", role="scout_hub", slack_bot_token="xoxb-hub",
    )
    await db_session.flush()
    for path in ("slack/provision", "activate"):
        r = await client.post(
            f"/manager/pis/{pi.id}/{path}",
            headers=auth_headers(manager.id), follow_redirects=False,
        )
        assert r.status_code == 404, path


async def test_the_callback_refuses_an_impersonated_session(
    client, db_session, monkeypatch
):
    """Landing a live bot token on someone else's agent is a write, and the
    manager POSTs already refuse an impersonated session — the callback does
    too, rather than recording the install against whoever is being worn."""
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    manager = await _manager(db_session)
    _pi, agent = await _pending_pi(db_session, agent_id="cbimperson")
    db_session.add(SlackAppProvision(
        agent_registry_id=agent.id, state="s4",
        client_id="cid", client_secret="secret",
        initiated_by_user_id=manager.id,
    ))
    await db_session.flush()

    def _never(*a, **k):
        raise AssertionError("the code must not be exchanged while impersonating")

    monkeypatch.setattr("src.services.admin_provisioning.exchange_code", _never)
    headers = auth_headers(admin.id)
    headers["Cookie"] += f"; copi-impersonate={manager.id}"
    r = await client.get(
        "/admin/agents/slack/callback?code=c&state=s4",
        headers=headers, follow_redirects=False,
    )
    assert r.status_code == 403
    await db_session.refresh(agent)
    assert agent.slack_bot_token is None


async def test_callback_refuses_a_null_initiator_row_for_a_non_admin(
    client, db_session, monkeypatch
):
    """A NULL ``initiated_by_user_id`` is the bulk-CLI bridge row
    (``scripts/make_install_links.py``), which mints install links for an
    ADMIN to open later. It is not "unknown initiator, allow": with the
    callback's gate now staff-wide, allowing it would let any manager who
    guessed or intercepted a state land a live bot token from a link an
    admin was issued. Only an admin may complete one; the row is left in
    place so the admin still can.
    """
    manager = await _manager(db_session)
    _pi, agent = await _pending_pi(db_session, agent_id="cbnullmgr")
    db_session.add(SlackAppProvision(
        agent_registry_id=agent.id, state="s4",
        client_id="cid", client_secret="secret",
        initiated_by_user_id=None,
    ))
    await db_session.flush()

    def _never(*a, **k):
        raise AssertionError("a NULL-initiator row must not be exchanged by a manager")

    monkeypatch.setattr("src.services.admin_provisioning.exchange_code", _never)
    r = await client.get(
        "/admin/agents/slack/callback?code=c&state=s4",
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"].startswith("/manager/pis?slack_error=")
    await db_session.refresh(agent)
    assert agent.slack_bot_token is None
    # The bridge row survives, so the admin the link was minted for can finish.
    surviving = (
        await db_session.execute(
            select(SlackAppProvision).where(SlackAppProvision.state == "s4")
        )
    ).scalar_one_or_none()
    assert surviving is not None


async def test_callback_completes_a_null_initiator_row_for_an_admin(
    client, db_session, monkeypatch
):
    """The other half: the bulk install-links path still works, for an admin."""
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    _pi, agent = await _pending_pi(db_session, agent_id="cbnulladm")
    db_session.add(SlackAppProvision(
        agent_registry_id=agent.id, state="s5",
        client_id="cid", client_secret="secret",
        initiated_by_user_id=None,
    ))
    await db_session.flush()
    monkeypatch.setattr(
        "src.services.admin_provisioning.exchange_code",
        lambda *a, **k: "xoxb-bulk",
    )

    r = await client.get(
        "/admin/agents/slack/callback?code=c&state=s5",
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == f"/admin/agents/{agent.id}?slack_ok=1"
    await db_session.refresh(agent)
    assert agent.slack_bot_token == "xoxb-bulk"


async def test_the_pi_directory_renders_a_slack_error_banner(client, db_session):
    """The callback's manager-surface error redirects land on /manager/pis
    with ``?slack_error=…``. Without a banner there the message is dropped
    silently and a refused install looks like nothing happened at all.
    """
    manager = await _manager(db_session)
    r = await client.get(
        "/manager/pis?slack_error=This+install+was+started+by+a+different+account.",
        headers=auth_headers(manager.id),
    )
    assert r.status_code == 200
    assert "Slack provisioning failed" in r.text
    assert "This install was started by a different account." in r.text


async def test_a_reviewer_cannot_render_a_fake_success_banner(client, db_session):
    """The four status banners on the PI detail page are staff-only. A
    reviewer can load the page read-only, so an unguarded banner lets any
    reviewer manufacture "Slack bot installed" out of a query string.
    """
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    pi, _agent = await _pending_pi(db_session, agent_id="revbanner")
    r = await client.get(
        f"/manager/pis/{pi.id}?slack_ok=1&activated=1"
        "&slack_error=nope&activation_blocked=1",
        headers=auth_headers(reviewer.id),
    )
    assert r.status_code == 200
    assert "Slack bot installed" not in r.text
    assert "Slack provisioning failed" not in r.text
    assert "Agent activated" not in r.text
    assert "Activation refused" not in r.text
