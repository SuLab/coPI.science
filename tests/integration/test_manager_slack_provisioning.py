"""F2: a manager may install a PI's Slack bot and activate the agent from
/workspace/pis/{id}, without an admin round-trip.

The Slack redirect URI ``/admin/agents/slack/callback`` is baked into every
Slack app manifest already issued, so the path does not move — only its gate
widens (admin → staff) and its redirects branch on the caller's role. The
bridge row records who started the install (``initiated_by_user_id``,
migration 0046) so a second staff account cannot land someone else's token.
"""

import pytest
from sqlalchemy import select, text

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    USER_ROLE_REVIEWER,
    SlackAppProvision,
)
from tests import factories
from tests.flash_support import session_cookie_header, session_flashes
from tests.integration._webui_helpers import follow
from tests.integration.test_manager_access import auth_headers
from tests.persona_support import write_persona

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

    monkeypatch.setattr("src.routers.workspace.pi_bots.start_provisioning", fake_start)
    r = await client.post(
        f"/workspace/pis/{pi.id}/slack/provision",
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

    monkeypatch.setattr("src.routers.workspace.pi_bots.start_provisioning", fake_start)
    r = await client.post(
        f"/workspace/pis/{pi.id}/slack/provision",
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == f"/workspace/pis/{pi.id}"
    assert session_flashes(r) == [{
        "text": "Slack provisioning failed: Could not create the Slack app: boom",
        "kind": "error",
    }]


async def test_reviewer_is_refused_and_impersonating_admin_is_admitted(
    client, db_session, monkeypatch
):
    """Operator decision 2026-09-11: the PI-management controls on
    /workspace/pis (Add-PI, Edit Profile, mute, Slack provision/activate/
    callback) are neither hidden nor refused while an admin impersonates a
    manager. An admin wearing a manager reaches both provisioning POSTs,
    attributed to the impersonated manager; a reviewer still cannot.

    Still refused while impersonating: reviewer assign/unassign and
    prompt-suggestion generate/status (`refuse_impersonation`, src/dependencies.py),
    every assessment-chat route (`_refused`, assessment_chat.py), self-service
    account deletion (profile.py) and the admin user delete (admin.py). Review
    feedback and review-status writes are allowed, attributed to the
    impersonated user (docs/operations/pis-and-access.md, Account Types)."""
    manager = await _manager(db_session)
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi, agent = await _pending_pi(db_session, agent_id="refused")

    for path in ("slack/provision", "activate"):
        r = await client.post(
            f"/workspace/pis/{pi.id}/{path}",
            headers=auth_headers(reviewer.id), follow_redirects=False,
        )
        assert r.status_code == 403, path

    seen = {}

    async def fake_start(db, a, *, initiated_by):
        seen["initiated_by"] = initiated_by.id
        return "https://slack.test/authorize?imp=1"

    monkeypatch.setattr("src.routers.workspace.pi_bots.start_provisioning", fake_start)
    headers = auth_headers(admin.id, impersonate=manager.id)
    r = await client.post(
        f"/workspace/pis/{pi.id}/slack/provision", headers=headers, follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == "https://slack.test/authorize?imp=1"
    assert seen == {"initiated_by": manager.id}

    # activate: no token yet, so the route bounces with the "install first"
    # message rather than 403ing — it was reached.
    r = await client.post(
        f"/workspace/pis/{pi.id}/activate", headers=headers, follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == f"/workspace/pis/{pi.id}"
    assert "Install" in session_flashes(r)[0]["text"]


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
    assert r.headers["location"] == f"/workspace/pis/{pi.id}"
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
    assert r.headers["location"] == f"/admin/agents/{agent.id}"


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
    assert r.headers["location"] == "/workspace/pis"
    assert session_flashes(r)[0]["text"].startswith("Slack provisioning failed: ")
    await db_session.refresh(agent)
    assert agent.slack_bot_token is None


async def test_manager_activate_is_gated_and_has_no_override(client, db_session):
    manager = await _manager(db_session)
    pi, agent = await _pending_pi(
        db_session, agent_id="activateme", slack_bot_token="xoxb-x",
    )

    r = await client.post(
        f"/workspace/pis/{pi.id}/activate",
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == f"/workspace/pis/{pi.id}?activation_blocked=1"
    await db_session.refresh(agent)
    assert agent.status == "pending"

    # The override an admin has is deliberately not offered here: a manager
    # only gets past the gate by fixing the profile.
    r = await client.post(
        f"/workspace/pis/{pi.id}/activate", data={"override": "on"},
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.headers["location"] == f"/workspace/pis/{pi.id}?activation_blocked=1"
    await db_session.refresh(agent)
    assert agent.status == "pending"

    await factories.make_profile(
        db_session, user=pi, evidence_pmid_count=10, evidence_pub_count=8,
    )
    write_persona(agent.agent_id)  # R2: the persona-file gate (spec 2026-10-05 §6.4)
    await db_session.flush()
    r = await client.post(
        f"/workspace/pis/{pi.id}/activate",
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.headers["location"] == f"/workspace/pis/{pi.id}?activated=1"
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
        f"/workspace/pis/{pi.id}/activate",
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == f"/workspace/pis/{pi.id}"
    assert session_flashes(r) == [
        {"text": "Slack provisioning failed: Install the Slack bot first.", "kind": "error"}
    ]
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
            f"/workspace/pis/{pi.id}/{path}",
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
            f"/workspace/pis/{pi.id}/{path}",
            headers=auth_headers(manager.id), follow_redirects=False,
        )
        assert r.status_code == 404, path


async def test_the_callback_admits_an_impersonated_session(
    client, db_session, monkeypatch
):
    """Operator decision 2026-09-11: the Slack provisioning callback admits an
    impersonated session. The impersonated manager is both the initiator and the completer, so the
    initiator check (migration 0046) lines up and the token lands."""
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    manager = await _manager(db_session)
    pi, agent = await _pending_pi(db_session, agent_id="cbimperson")
    db_session.add(SlackAppProvision(
        agent_registry_id=agent.id, state="s4",
        client_id="cid", client_secret="secret",
        initiated_by_user_id=manager.id,
    ))
    await db_session.flush()

    monkeypatch.setattr(
        "src.services.admin_provisioning.exchange_code",
        lambda *a, **k: "xoxb-imp-token",
    )
    headers = auth_headers(admin.id, impersonate=manager.id)
    r = await client.get(
        "/admin/agents/slack/callback?code=c&state=s4",
        headers=headers, follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == f"/workspace/pis/{pi.id}"
    await db_session.refresh(agent)
    assert agent.slack_bot_token == "xoxb-imp-token"


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
    assert r.headers["location"] == "/workspace/pis"
    assert session_flashes(r)[0]["text"].startswith("Slack provisioning failed: ")
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
    assert r.headers["location"] == f"/admin/agents/{agent.id}"
    await db_session.refresh(agent)
    assert agent.slack_bot_token == "xoxb-bulk"


async def test_the_pi_directory_renders_the_callbacks_flashed_error(client, db_session):
    """The callback's manager-surface errors land on /workspace/pis as a flash; a
    ?slack_error= query string no longer puts any text on the page (A-14)."""
    manager = await _manager(db_session)
    r = await client.get(
        "/admin/agents/slack/callback?error=access_denied",
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.status_code == 302 and r.headers["location"] == "/workspace/pis"
    page = await client.get("/workspace/pis", headers=session_cookie_header(r))
    assert "Slack provisioning failed: the installation was cancelled in Slack" in page.text

    crafted = await client.get(
        "/admin/agents/slack/callback?error=Re-authorize+at+evil.example",
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    flashed = " ".join(f["text"] for f in session_flashes(crafted))
    assert "Slack reported an error" in flashed and "evil" not in flashed

    forged = await client.get(
        "/workspace/pis?slack_error=Forged+text", headers=auth_headers(manager.id)
    )
    assert "Forged text" not in forged.text


async def test_a_reviewer_cannot_render_a_fake_success_banner(client, db_session):
    """The four status banners on the PI detail page are staff-only. A
    reviewer can load the page read-only, so an unguarded banner lets any
    reviewer manufacture "Slack bot installed" out of a query string.
    """
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    pi, _agent = await _pending_pi(db_session, agent_id="revbanner")
    r = await client.get(
        f"/workspace/pis/{pi.id}?slack_ok=1&activated=1"
        "&slack_error=nope&activation_blocked=1",
        headers=auth_headers(reviewer.id),
    )
    assert r.status_code == 200
    assert "Slack bot installed" not in r.text
    assert "Slack provisioning failed" not in r.text
    assert "Agent activated" not in r.text
    assert "Activation refused" not in r.text


async def _grounded(db_session, pi):
    await factories.make_profile(
        db_session, user=pi, evidence_pmid_count=10, evidence_pub_count=8,
    )
    await db_session.flush()


async def test_activate_does_not_overwrite_a_concurrent_suspend(client, db_session, monkeypatch):
    import src.routers.workspace.pi_bots as manager_routes

    manager = await _manager(db_session)
    pi, agent = await _pending_pi(db_session, agent_id="raced", slack_bot_token="xoxb-raced")
    await _grounded(db_session, pi)
    write_persona(agent.agent_id)  # R2: the persona-file gate (spec 2026-10-05 §6.4)
    # Committed (a savepoint release under conftest's create_savepoint mode), so the
    # route's rollback on a lost race cannot undo the fixtures (plan audit Q2-02).
    await db_session.commit()
    real_gate = manager_routes.ensure_activation_allowed

    async def gate_then_suspend(db, a, **kwargs):
        blockers = await real_gate(db, a, **kwargs)
        # The concurrent writer: committed before the route's conditional UPDATE runs,
        # so the route's own rollback leaves it in place.
        await db.execute(text("UPDATE agents SET status = 'suspended' WHERE id = :id"), {"id": a.id})
        await db.commit()
        return blockers

    monkeypatch.setattr(manager_routes, "ensure_activation_allowed", gate_then_suspend)
    # Read before the call: the route's rollback expires every object of the shared
    # session, and an expired attribute read here would be sync IO.
    pi_id = pi.id
    r = await client.post(
        f"/workspace/pis/{pi_id}/activate", headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == f"/workspace/pis/{pi_id}"
    await db_session.refresh(agent)
    assert agent.status == "suspended"
    assert agent.approved_by is None
    page = await follow(client, r)
    assert "changed while you were activating it" in page.text


async def test_activate_accepts_an_env_only_bot_token(client, db_session, monkeypatch):
    monkeypatch.setattr(
        "src.services.slack_tokens.env_token",
        lambda aid: "xoxb-env-only" if aid == "envonly" else None,
    )
    manager = await _manager(db_session)
    pi, agent = await _pending_pi(db_session, agent_id="envonly")
    await _grounded(db_session, pi)
    write_persona(agent.agent_id)  # R2: the persona-file gate (spec 2026-10-05 §6.4)
    r = await client.post(
        f"/workspace/pis/{pi.id}/activate", headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.headers["location"] == f"/workspace/pis/{pi.id}?activated=1"
    await db_session.refresh(agent)
    assert agent.status == "active"


async def test_pi_detail_offers_activate_for_an_env_only_token(client, db_session, monkeypatch):
    monkeypatch.setattr(
        "src.services.slack_tokens.env_token",
        lambda aid: "xoxb-env-only" if aid == "envdetail" else None,
    )
    manager = await _manager(db_session)
    pi, _agent = await _pending_pi(db_session, agent_id="envdetail")
    r = await client.get(f"/workspace/pis/{pi.id}", headers=auth_headers(manager.id))
    assert f'action="/workspace/pis/{pi.id}/activate"' in r.text
    assert f'action="/workspace/pis/{pi.id}/slack/provision"' not in r.text


async def test_unmute_accepts_an_env_only_bot_token(client, db_session, monkeypatch):
    monkeypatch.setattr(
        "src.services.slack_tokens.env_token",
        lambda aid: "xoxb-env-only" if aid == "envmute" else None,
    )
    manager = await _manager(db_session)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    agent = await factories.make_agent(db_session, user=pi, agent_id="envmute", status="inactive")
    await _grounded(db_session, pi)
    write_persona(agent.agent_id)  # R2: the persona-file gate (spec 2026-10-05 §6.4)
    r = await client.post(
        f"/workspace/pis/{pi.id}/unmute", headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.headers["location"] == f"/workspace/pis/{pi.id}"
    await db_session.refresh(agent)
    assert agent.status == "active"
