"""Live integration tests for the cohort admin surface.

Real ASGI requests, real Postgres, real Jinja templates. Covers the granular
topology control (.notes/cohort-system-v2.md §12), the audit trail (§4.1/§13.1),
the delete guard, and the rule that the gate never becomes access control (§6.2).
"""

import base64
import json
import uuid

import pytest
from itsdangerous import TimestampSigner
from sqlalchemy import select

from src.config import get_settings
from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_PI,
    AgentRegistry,
    Cohort,
    CohortAuditEvent,
    CohortMembership,
)
from tests import factories

pytestmark = pytest.mark.integration


def _auth(user_id) -> dict:
    """Forge the signed session cookie SessionMiddleware would issue."""
    signer = TimestampSigner(get_settings().secret_key)
    data = base64.b64encode(json.dumps({"user_id": str(user_id)}).encode())
    return {"Cookie": f"copi-session={signer.sign(data).decode()}"}


@pytest.fixture
async def admin(db_session):
    return await factories.make_user(db_session, user_role=USER_ROLE_ADMIN, email="admin@example.org")


@pytest.fixture
async def roster(db_session):
    """Three active agents, mirroring the real bot-name convention."""
    out = {}
    for aid, bot in (("su", "SuBot"), ("wiseman", "WisemanBot"), ("cravatt", "CravattBot")):
        user = await factories.make_user(db_session, email=f"{aid}@example.org")
        out[aid] = await factories.make_agent(
            db_session, user=user, agent_id=aid, bot_name=bot,
            pi_name=f"PI {aid}", status="active",
        )
    await db_session.flush()
    return out


async def _cohort(db_session, name, admin, members=()):
    c = Cohort(name=name, created_by=admin.id)
    db_session.add(c)
    await db_session.flush()
    for aid in members:
        db_session.add(CohortMembership(cohort_id=c.id, agent_id=aid, added_by=admin.id))
    await db_session.flush()
    return c


# --- agent role editing (task 11) -------------------------------------------


async def test_admin_can_set_agent_role(client, db_session, admin, roster):
    agent = roster["su"]
    assert agent.role == "pi_lab"
    r = await client.post(
        f"/admin/agents/{agent.id}/role",
        data={"role": "scout_hub"},
        headers=_auth(admin.id),
    )
    assert r.status_code == 302
    row = (await db_session.execute(
        select(AgentRegistry).where(AgentRegistry.id == agent.id)
    )).scalar_one()
    assert row.role == "scout_hub"


async def test_setting_an_unknown_role_is_rejected_without_a_500(
    client, db_session, admin, roster
):
    agent = roster["su"]
    r = await client.post(
        f"/admin/agents/{agent.id}/role",
        data={"role": "not-a-real-role"},
        headers=_auth(admin.id),
    )
    assert r.status_code == 302
    assert "error" in r.headers["location"]
    row = (await db_session.execute(
        select(AgentRegistry).where(AgentRegistry.id == agent.id)
    )).scalar_one()
    assert row.role == "pi_lab", "an unknown role must never be persisted"


async def test_setting_agent_role_requires_admin(client, db_session, admin, roster):
    agent = roster["su"]
    plain = await factories.make_user(db_session, user_role=USER_ROLE_PI, email="plain2@example.org")
    await db_session.flush()
    r = await client.post(
        f"/admin/agents/{agent.id}/role",
        data={"role": "scout_hub"},
        headers=_auth(plain.id),
    )
    assert r.status_code == 403
    row = (await db_session.execute(
        select(AgentRegistry).where(AgentRegistry.id == agent.id)
    )).scalar_one()
    assert row.role == "pi_lab"


async def test_setting_agent_role_requires_login(client, db_session, roster):
    agent = roster["su"]
    r = await client.post(
        f"/admin/agents/{agent.id}/role",
        data={"role": "scout_hub"},
    )
    assert r.status_code == 302
    assert "/login" in r.headers["location"]
    row = (await db_session.execute(
        select(AgentRegistry).where(AgentRegistry.id == agent.id)
    )).scalar_one()
    assert row.role == "pi_lab"


async def test_agent_detail_page_shows_the_current_role(client, admin, roster):
    agent = roster["su"]
    r = await client.get(f"/admin/agents/{agent.id}", headers=_auth(admin.id))
    assert r.status_code == 200
    assert "pi_lab" in r.text


async def test_topology_page_shows_each_agents_role(client, db_session, admin, roster):
    # The matrix table (and therefore any per-agent role cell) only renders once
    # there is at least one cohort — see the "No cohorts yet" empty state.
    await _cohort(db_session, "wave", admin)
    r = await client.get("/admin/cohorts/topology", headers=_auth(admin.id))
    assert r.status_code == 200
    assert "pi_lab" in r.text


# --- access control ---------------------------------------------------------


async def test_cohort_pages_require_login(client):
    for path in ("/admin/cohorts", "/admin/cohorts/topology"):
        r = await client.get(path)
        assert r.status_code == 302, path
        assert "/login" in r.headers["location"]


async def test_cohort_pages_require_admin(client, db_session):
    plain = await factories.make_user(db_session, user_role=USER_ROLE_PI, email="plain@example.org")
    await db_session.flush()
    r = await client.get("/admin/cohorts", headers=_auth(plain.id))
    assert r.status_code == 403


# --- list + create ---------------------------------------------------------


async def test_list_renders_with_no_cohorts(client, admin, monkeypatch):
    # Hermetic: the banner's "OFF" premise is cohort_isolation_enabled at its
    # default (False). Pin it rather than inherit whatever the deployed .env on
    # this host sets (COHORT_ISOLATION_ENABLED=true) — get_settings() is a
    # process-wide lru_cache, so patch the cached instance's attribute directly.
    monkeypatch.setattr(get_settings(), "cohort_isolation_enabled", False)
    r = await client.get("/admin/cohorts", headers=_auth(admin.id))
    assert r.status_code == 200
    assert "No cohorts yet" in r.text
    # The banner must state what is actually in force.
    assert "Cohort isolation is OFF" in r.text
    assert "forward-only" in r.text


async def test_create_writes_an_audit_event(client, db_session, admin):
    r = await client.post(
        "/admin/cohorts/create",
        data={"name": "pilot-wave-1", "description": "first wave"},
        headers=_auth(admin.id),
    )
    assert r.status_code == 302
    c = (await db_session.execute(
        select(Cohort).where(Cohort.name == "pilot-wave-1")
    )).scalar_one()
    ev = (await db_session.execute(
        select(CohortAuditEvent).where(CohortAuditEvent.cohort_id == c.id)
    )).scalars().all()
    assert [e.action for e in ev] == ["created"]
    assert ev[0].actor_email == "admin@example.org"


async def test_create_rejects_a_bad_name(client, db_session, admin):
    r = await client.post(
        "/admin/cohorts/create", data={"name": "Not A Slug!"}, headers=_auth(admin.id)
    )
    assert r.status_code == 302 and "error=Invalid+name" in r.headers["location"]
    assert (await db_session.execute(select(Cohort))).scalars().all() == []


async def test_create_rejects_a_duplicate_name(client, db_session, admin):
    await _cohort(db_session, "dupe", admin)
    r = await client.post(
        "/admin/cohorts/create", data={"name": "dupe"}, headers=_auth(admin.id)
    )
    assert "error=A+cohort+with+that+name" in r.headers["location"]


# --- membership ------------------------------------------------------------


async def test_add_and_remove_agent_are_both_audited(client, db_session, admin, roster):
    c = await _cohort(db_session, "wave", admin)
    r = await client.post(
        f"/admin/cohorts/{c.id}/add-agent", data={"agent_id": "su"}, headers=_auth(admin.id)
    )
    assert r.status_code == 302
    assert (await db_session.execute(
        select(CohortMembership).where(CohortMembership.cohort_id == c.id)
    )).scalars().all()

    r = await client.post(
        f"/admin/cohorts/{c.id}/remove-agent", data={"agent_id": "su"},
        headers=_auth(admin.id),
    )
    assert r.status_code == 302
    assert (await db_session.execute(
        select(CohortMembership).where(CohortMembership.cohort_id == c.id)
    )).scalars().all() == []

    actions = [e.action for e in (await db_session.execute(
        select(CohortAuditEvent)
        .where(CohortAuditEvent.cohort_id == c.id)
        .order_by(CohortAuditEvent.created_at)
    )).scalars().all()]
    assert actions == ["agent_added", "agent_removed"]


async def test_add_unknown_agent_is_refused(client, db_session, admin):
    c = await _cohort(db_session, "wave", admin)
    r = await client.post(
        f"/admin/cohorts/{c.id}/add-agent", data={"agent_id": "nobody"},
        headers=_auth(admin.id),
    )
    assert "error=Unknown+agent" in r.headers["location"]
    assert (await db_session.execute(select(CohortMembership))).scalars().all() == []


async def test_add_duplicate_member_is_refused(client, db_session, admin, roster):
    c = await _cohort(db_session, "wave", admin, members=["su"])
    r = await client.post(
        f"/admin/cohorts/{c.id}/add-agent", data={"agent_id": "su"}, headers=_auth(admin.id)
    )
    assert "already+a+member" in r.headers["location"]


# --- delete guard ----------------------------------------------------------


async def test_delete_is_refused_while_members_exist(client, db_session, admin, roster):
    c = await _cohort(db_session, "populated", admin, members=["su", "wiseman"])
    r = await client.post(f"/admin/cohorts/{c.id}/delete", headers=_auth(admin.id))
    assert r.status_code == 302
    assert "Remove+all+2+members" in r.headers["location"]
    assert (await db_session.execute(
        select(Cohort).where(Cohort.id == c.id)
    )).scalar_one_or_none() is not None, "populated cohort must survive"


async def test_delete_succeeds_when_empty_and_keeps_the_trail(client, db_session, admin):
    c = await _cohort(db_session, "empty", admin)
    cid = c.id
    r = await client.post(f"/admin/cohorts/{cid}/delete", headers=_auth(admin.id))
    assert r.status_code == 302 and "notice=Deleted" in r.headers["location"]
    assert (await db_session.execute(
        select(Cohort).where(Cohort.id == cid)
    )).scalar_one_or_none() is None
    trail = (await db_session.execute(
        select(CohortAuditEvent).where(CohortAuditEvent.cohort_id == cid)
    )).scalars().all()
    assert "deleted" in {e.action for e in trail}, (
        "the audit trail must outlive the cohort row"
    )
    assert all(e.cohort_name == "empty" for e in trail)


async def test_detail_hides_the_delete_button_when_populated(
    client, db_session, admin, roster
):
    c = await _cohort(db_session, "populated", admin, members=["su"])
    r = await client.get(f"/admin/cohorts/{c.id}", headers=_auth(admin.id))
    assert r.status_code == 200
    assert "Remove all 1 members first" in r.text


# --- detail page + audit log ----------------------------------------------


async def test_detail_renders_members_and_audit_log(client, db_session, admin, roster):
    c = await _cohort(db_session, "wave", admin)
    await client.post(
        f"/admin/cohorts/{c.id}/add-agent", data={"agent_id": "su"}, headers=_auth(admin.id)
    )
    r = await client.get(f"/admin/cohorts/{c.id}", headers=_auth(admin.id))
    assert r.status_code == 200
    assert "SuBot" in r.text
    assert "Audit log" in r.text
    assert "agent_added" in r.text
    assert "admin@example.org" in r.text


async def test_detail_404s_for_an_unknown_cohort(client, admin):
    r = await client.get(f"/admin/cohorts/{uuid.uuid4()}", headers=_auth(admin.id))
    assert r.status_code == 404


# --- topology matrix: granular control ------------------------------------


async def test_topology_route_is_not_shadowed_by_the_uuid_path(client, admin):
    """"/cohorts/topology" must resolve to the matrix, not to a UUID lookup."""
    r = await client.get("/admin/cohorts/topology", headers=_auth(admin.id))
    assert r.status_code == 200
    assert "Topology matrix" in r.text


async def test_topology_renders_a_cell_per_pair(client, db_session, admin, roster):
    a = await _cohort(db_session, "alpha", admin, members=["su"])
    b = await _cohort(db_session, "beta", admin)
    r = await client.get("/admin/cohorts/topology", headers=_auth(admin.id))
    assert r.status_code == 200
    for c in (a, b):
        for aid in ("su", "wiseman", "cravatt"):
            assert f'value="{c.id}:{aid}"' in r.text, f"missing cell {c.name}/{aid}"
    # Exactly the pre-existing membership is pre-ticked. Matched on the input tag
    # itself: a bare count of "checked" also picks up the column-toggle script.
    import re as _re
    ticked = set(_re.findall(
        r'name="cell"\s+value="([^"]+)"[^>]*?\bchecked\b', r.text, _re.S
    ))
    assert ticked == {f"{a.id}:su"}, ticked


def _hidden_marker_values(html: str, name: str) -> set[str]:
    """Every ``value`` of a hidden ``<input>`` with the given ``name``."""
    import re

    out = set()
    for tag in re.finditer(r"<input\b[^>]*>", html):
        t = tag.group(0)
        if f'name="{name}"' not in t:
            continue
        value = re.search(r'value="([^"]*)"', t)
        if value:
            out.add(value.group(1))
    return out


def _all_cell_values(html: str) -> set[str]:
    """Every ``value`` of a ``name="cell"`` checkbox, checked or not.

    Unlike ``_ticked_cells`` (below), this does not filter on ``checked`` — it
    is used to assert the RENDERED cell set, not the pre-ticked one.
    """
    import re

    out = set()
    for tag in re.finditer(r"<input\b[^>]*>", html):
        t = tag.group(0)
        if 'name="cell"' not in t:
            continue
        value = re.search(r'value="([^"]*)"', t)
        if value:
            out.add(value.group(1))
    return out


async def test_rendered_cells_are_exactly_the_marker_cross_product(
    client, db_session, admin, roster
):
    """Structural safety property (audit finding F5): the ``present_agent`` /
    ``present_cohort`` markers the save route trusts to reconstruct ``rendered``
    must equal the ACTUAL cross product of cells the table drew, or a save can
    silently delete memberships for a cell that was never shown (see
    ``test_topology_save_only_touches_rendered_cells`` and friends above).

    The old per-cell ``present`` input was emitted INSIDE the nested cell loop,
    so it was structurally impossible for a cell to render without a matching
    marker. The markers now sit OUTSIDE the table (``cohort_topology.html:46-47``),
    so that guarantee is no longer enforced by the template's structure — only by
    convention. If a future change wraps the inner ``{% for c in cohorts %}``
    loop in a per-agent conditional (e.g. skip cohorts for a suspended agent),
    the markers would still claim the full cross product while the table drew
    fewer cells, and this test is what would catch the mismatch (it would fail
    the other direction too: a cell rendered with no corresponding marker pair).

    A non-active agent is included deliberately — production has 3 ``pending``
    agents, and the "Acts on" column already special-cases non-active status
    (a real conditional in that neighborhood), so a regression is plausible
    exactly there.
    """
    a = await _cohort(db_session, "alpha", admin, members=["su"])
    b = await _cohort(db_session, "beta", admin)
    pending_user = await factories.make_user(db_session, email="pendingpi@example.org")
    await factories.make_agent(
        db_session, user=pending_user, agent_id="pendingagent", bot_name="PendingAgentBot",
        pi_name="Pending PI", status="pending",
    )

    r = await client.get("/admin/cohorts/topology", headers=_auth(admin.id))
    assert r.status_code == 200

    present_agents = _hidden_marker_values(r.text, "present_agent")
    present_cohorts = _hidden_marker_values(r.text, "present_cohort")
    assert present_agents == {"su", "wiseman", "cravatt", "pendingagent"}, present_agents
    assert present_cohorts == {str(a.id), str(b.id)}, present_cohorts

    expected_cross_product = {
        f"{cid}:{aid}" for cid in present_cohorts for aid in present_agents
    }
    rendered_cells = _all_cell_values(r.text)
    assert rendered_cells == expected_cross_product, (
        f"markers claim {len(expected_cross_product)} cells but the table drew "
        f"{len(rendered_cells)}; missing={expected_cross_product - rendered_cells}, "
        f"extra={rendered_cells - expected_cross_product}"
    )


async def test_topology_save_applies_adds_and_removes_in_one_pass(
    client, db_session, admin, roster
):
    a = await _cohort(db_session, "alpha", admin, members=["su"])
    b = await _cohort(db_session, "beta", admin)
    # Drop su from alpha, add wiseman to alpha, add cravatt to beta — one save.
    ticked = [f"{a.id}:wiseman", f"{b.id}:cravatt"]
    r = await client.post(
        "/admin/cohorts/topology",
        data={
            "present_cohort": [str(a.id), str(b.id)],
            "present_agent": ["su", "wiseman", "cravatt"],
            "cell": ticked,
        },
        headers=_auth(admin.id),
    )
    assert r.status_code == 302
    assert "1+added" not in r.headers["location"]  # 2 added, 1 removed
    rows = {
        (str(m.cohort_id), m.agent_id)
        for m in (await db_session.execute(select(CohortMembership))).scalars().all()
    }
    assert rows == {(str(a.id), "wiseman"), (str(b.id), "cravatt")}


async def test_topology_save_audits_every_change(client, db_session, admin, roster):
    a = await _cohort(db_session, "alpha", admin, members=["su"])
    await client.post(
        "/admin/cohorts/topology",
        data={
            "present_cohort": [str(a.id)],
            "present_agent": ["su", "wiseman"],
            "cell": [f"{a.id}:wiseman"],
        },
        headers=_auth(admin.id),
    )
    events = (await db_session.execute(
        select(CohortAuditEvent).where(CohortAuditEvent.cohort_id == a.id)
    )).scalars().all()
    assert {e.action for e in events} == {"agent_added", "agent_removed"}
    assert {e.agent_id for e in events} == {"su", "wiseman"}


async def test_topology_save_only_touches_rendered_cells(
    client, db_session, admin, roster
):
    """The data-loss guard: a partial form must not delete what it never showed."""
    a = await _cohort(db_session, "alpha", admin, members=["su"])
    b = await _cohort(db_session, "beta", admin, members=["cravatt"])
    # Submit ONLY alpha's cells, all unticked. Beta's membership must survive.
    r = await client.post(
        "/admin/cohorts/topology",
        data={
            "present_cohort": [str(a.id)],
            "present_agent": ["su", "wiseman", "cravatt"],
        },
        headers=_auth(admin.id),
    )
    assert r.status_code == 302
    rows = {
        (str(m.cohort_id), m.agent_id)
        for m in (await db_session.execute(select(CohortMembership))).scalars().all()
    }
    assert rows == {(str(b.id), "cravatt")}, (
        "a form that did not render beta must not delete beta's memberships"
    )


async def test_topology_save_rejects_an_empty_submission(client, db_session, admin, roster):
    await _cohort(db_session, "alpha", admin, members=["su"])
    r = await client.post("/admin/cohorts/topology", data={}, headers=_auth(admin.id))
    assert "error=Nothing+to+save" in r.headers["location"]
    assert len((await db_session.execute(select(CohortMembership))).scalars().all()) == 1


async def test_topology_save_rejects_a_tick_outside_the_rendered_set(
    client, db_session, admin, roster
):
    a = await _cohort(db_session, "alpha", admin)
    r = await client.post(
        "/admin/cohorts/topology",
        data={
            "present_cohort": [str(a.id)],
            "present_agent": ["su"],
            "cell": [f"{a.id}:wiseman"],
        },
        headers=_auth(admin.id),
    )
    assert "error=Malformed+submission" in r.headers["location"]
    assert (await db_session.execute(select(CohortMembership))).scalars().all() == []


async def test_topology_save_ignores_unknown_ids(client, db_session, admin, roster):
    """A stale form naming a deleted cohort or a removed agent writes nothing."""
    ghost = uuid.uuid4()
    a = await _cohort(db_session, "alpha", admin)
    r = await client.post(
        "/admin/cohorts/topology",
        data={
            "present_cohort": [str(ghost), str(a.id)],
            "present_agent": ["su", "nobody"],
            "cell": [f"{ghost}:su", f"{a.id}:nobody"],
        },
        headers=_auth(admin.id),
    )
    assert r.status_code == 302
    assert (await db_session.execute(select(CohortMembership))).scalars().all() == []


# --- gate preview ---------------------------------------------------------


async def test_preview_matches_the_engine_semantics(
    client, db_session, admin, roster, monkeypatch
):
    """The admin preview must be computed by the same function the engine uses."""
    from src.services.cohorts import compute_gates

    # Hermetic: "isolation off (the default)" is this test's stated premise. Pin
    # it — see test_list_renders_with_no_cohorts for why ambient .env cannot be
    # trusted here.
    monkeypatch.setattr(get_settings(), "cohort_isolation_enabled", False)

    a = await _cohort(db_session, "alpha", admin, members=["su", "wiseman"])
    rows = [(a.id, "su"), (a.id, "wiseman")]
    gates, _ = compute_gates(
        membership_rows=rows, agent_ids=["cravatt", "su", "wiseman"],
        isolation_enabled=True, policy="open", cohort_count=1,
    )
    # Under policy=open the uncohorted agent is included in su's gate, so the two can
    # actually converse (both directions). See the unit-level regression test.
    assert gates["su"] == {"su", "wiseman", "cravatt"}
    assert gates["cravatt"] is None

    r = await client.get("/admin/cohorts/topology", headers=_auth(admin.id))
    assert r.status_code == 200
    # With isolation off (the default) every agent is unrestricted.
    assert "gate off for this agent" in r.text or "Cohort isolation is OFF" in r.text


async def test_inactive_agent_is_labelled_not_unrestricted(
    client, db_session, admin, roster
):
    roster["cravatt"].status = "suspended"
    await db_session.flush()
    await _cohort(db_session, "alpha", admin, members=["su"])
    r = await client.get("/admin/cohorts/topology", headers=_auth(admin.id))
    assert "not active — the engine will not load this agent" in r.text


# --- the gate is not access control --------------------------------------


async def test_pi_facing_thread_view_is_never_cohort_filtered(
    client, db_session, admin, roster
):
    """A cohort must never change what a human can read (v2 §6.2).

    Two agents in different cohorts exchange messages; the admin discussion view
    must still show both.
    """
    run = await factories.make_simulation_run(db_session)
    await _cohort(db_session, "alpha", admin, members=["su"])
    await _cohort(db_session, "beta", admin, members=["cravatt"])
    await factories.make_agent_message(
        db_session, run=run, agent_id="su", content="from su",
        channel_name="general", message_ts="100.1",
    )
    await factories.make_agent_message(
        db_session, run=run, agent_id="cravatt", content="from cravatt",
        channel_name="general", message_ts="100.2",
    )
    await db_session.flush()
    r = await client.get("/admin/discussions", headers=_auth(admin.id))
    assert r.status_code == 200


# --- §11: what takes effect immediately and what needs a restart ------------


def _ticked_cells(html: str) -> set[str]:
    """The cells the matrix rendered as already-checked.

    Matches one whole ``<input>`` tag at a time. A regex that let the ``checked``
    lookahead run past the end of the tag picks up the column-toggle JavaScript's
    ``b.checked`` for whichever agent happens to render last.
    """
    import re

    out = set()
    for tag in re.finditer(r"<input\b[^>]*>", html):
        t = tag.group(0)
        if 'name="cell"' not in t:
            continue
        value = re.search(r'value="([^"]*)"', t)
        if value and re.search(r"\bchecked\b", t):
            out.add(value.group(1))
    return out


def test_ticked_cells_helper_distinguishes_checked_from_unchecked():
    """Control for the helper the live test depends on. An extractor that returned
    every cell (or none) would make the assertion below meaningless."""
    html = (
        '<input type="checkbox" name="cell" value="c1:su" class="x" checked>'
        '<input type="checkbox" name="cell" value="c1:wiseman" class="x">'
        '<input type="hidden" name="present" value="c1:cravatt">'
        "<script>boxes.forEach(function (b) { b.checked = true; });</script>"
    )
    assert _ticked_cells(html) == {"c1:su"}


async def test_membership_is_live_but_settings_are_cached(client, db_session, admin, roster):
    """§11's asymmetry, both halves, so neither can pass alone.

    A topology edit takes effect on the engine's next roster sync (~30s, no restart).
    The flag and the policy do not, because get_settings() is lru_cached — that is why
    the admin banner says "restart required" for those and not for membership. If the
    caching half ever stops being true, the banner is lying.
    """
    import os

    from src.config import Settings, get_settings

    c = await _cohort(db_session, "alpha", admin)
    before = get_settings().cohort_isolation_enabled

    os.environ["COHORT_ISOLATION_ENABLED"] = "true" if not before else "false"
    try:
        assert get_settings().cohort_isolation_enabled is before, (
            "get_settings() is no longer cached — §11 and the admin banner's "
            "'restart required' wording are both wrong"
        )
        # Control: a FRESH Settings() DOES see the env var. Without this leg the
        # assertion above is also satisfied by an env var that never took effect.
        assert Settings().cohort_isolation_enabled is not before, (
            "control leg failed: the env var had no effect even on a fresh Settings(), "
            "so the caching assertion above proves nothing"
        )
    finally:
        os.environ.pop("COHORT_ISOLATION_ENABLED", None)

    # Positive: a membership change IS visible to the very next request, no restart.
    r = await client.post(
        f"/admin/cohorts/{c.id}/add-agent", data={"agent_id": "su"},
        headers=_auth(admin.id),
    )
    assert r.status_code == 302
    page = await client.get("/admin/cohorts/topology", headers=_auth(admin.id))
    assert page.status_code == 200
    assert _ticked_cells(page.text) == {f"{c.id}:su"}, (
        "the membership edit is not reflected in the matrix"
    )


async def test_matrix_save_is_one_transaction(client, db_session, admin, roster):
    """A mid-loop commit would expose an empty topology to a concurrent recompute.

    The engine reads cohort_memberships in a separate session. A save that committed
    per row would let a recompute landing between commits see a partial — or, at the
    instant every delete has landed and no insert has, an EMPTY — topology, which
    under policy=isolated silences the whole roster. Structural rather than timing
    based on purpose: a sleep-and-race test would be flaky and would not say why.
    """
    import inspect

    from src.routers import admin as admin_mod

    src = inspect.getsource(admin_mod.admin_cohort_topology_save)
    assert src.count("await db.commit()") == 1, (
        "the matrix save must commit exactly once; a per-row commit exposes an "
        "empty-topology window to a concurrent gate recompute"
    )
    assert "for cell in sorted(rendered)" in src
    assert src.index("for cell in sorted(rendered)") < src.index("await db.commit()"), (
        "the commit must come after the whole diff loop"
    )


async def test_matrix_save_never_touches_an_unrendered_cohort(
    client, db_session, admin, roster
):
    """The classic checkbox-matrix data-loss bug, asserted from the outside.

    A form that rendered only alpha's cells must not delete beta's memberships, even
    though beta's rows are absent from the submission and therefore look "unticked".
    """
    a = await _cohort(db_session, "alpha", admin, members=["su"])
    b = await _cohort(db_session, "beta", admin, members=["cravatt"])
    await db_session.commit()

    r = await client.post(
        "/admin/cohorts/topology",
        data={
            "present_cohort": [str(a.id)],
            "present_agent": ["su", "wiseman", "cravatt"],
        },
        headers=_auth(admin.id),
    )
    assert r.status_code == 302

    rows = {
        (str(m.cohort_id), m.agent_id)
        for m in (await db_session.execute(select(CohortMembership))).scalars().all()
    }
    assert rows == {(str(b.id), "cravatt")}, (
        f"a form that did not render beta must not delete beta's memberships. "
        f"rows={rows}"
    )
    # Control: alpha's rendered-and-unticked cell WAS removed, so the diff did run.
    assert (str(a.id), "su") not in rows, (
        "control leg failed: the save did nothing at all, so the beta assertion "
        "above proves nothing"
    )


async def test_matrix_save_ignores_a_cell_for_a_deleted_cohort(
    client, db_session, admin, roster
):
    """A stale form must not resurrect or crash on a cohort that no longer exists."""
    a = await _cohort(db_session, "alpha", admin, members=["su"])
    ghost = uuid.uuid4()
    await db_session.commit()

    r = await client.post(
        "/admin/cohorts/topology",
        data={
            "present_cohort": [str(a.id), str(ghost)],
            "present_agent": ["su", "wiseman"],
            "cell": [f"{a.id}:su", f"{ghost}:wiseman"],
        },
        headers=_auth(admin.id),
    )
    assert r.status_code == 302, "a stale cell must not 500"

    rows = {
        (str(m.cohort_id), m.agent_id)
        for m in (await db_session.execute(select(CohortMembership))).scalars().all()
    }
    assert rows == {(str(a.id), "su")}, f"the ghost cell was written: {rows}"


async def test_matrix_save_ignores_a_cell_for_an_unknown_agent(
    client, db_session, admin, roster
):
    """Same for an agent id that is not in AgentRegistry — a membership naming a
    nonexistent agent would be invisible in the UI and would survive forever."""
    a = await _cohort(db_session, "alpha", admin)
    await db_session.commit()

    r = await client.post(
        "/admin/cohorts/topology",
        data={
            "present_cohort": [str(a.id)],
            "present_agent": ["su", "nobody"],
            "cell": [f"{a.id}:su", f"{a.id}:nobody"],
        },
        headers=_auth(admin.id),
    )
    assert r.status_code == 302

    rows = {
        (str(m.cohort_id), m.agent_id)
        for m in (await db_session.execute(select(CohortMembership))).scalars().all()
    }
    assert rows == {(str(a.id), "su")}, f"an unknown agent id was written: {rows}"


# --- attribution and the not-found / no-op edges --------------------------
#
# The handlers above are all entered by the tests before this line, but five
# branches inside them were never taken: the creator-name lookup on the list
# page, the empty-members path on the detail page, and the three
# missing-row paths (delete, add-agent, remove-agent). Each one is a place
# where the route can either 500 or silently do the wrong thing, so each gets
# its own test asserting the observable outcome.


async def test_list_attributes_each_cohort_to_its_creator(client, db_session, admin):
    """The "Created by" column resolves created_by to a user name.

    A distinct creator (not the logged-in admin) is used deliberately: the page's
    nav bar already prints the current user's name, so asserting on the admin's
    own name would pass even if creator_map were never populated.
    """
    creator = await factories.make_user(
        db_session, name="Zelda Creator", email="zelda@example.org"
    )
    await _cohort(db_session, "attributed", creator)
    orphan = Cohort(name="orphaned", created_by=None)
    db_session.add(orphan)
    await db_session.flush()

    r = await client.get("/admin/cohorts", headers=_auth(admin.id))
    assert r.status_code == 200
    assert "attributed" in r.text and "orphaned" in r.text
    assert "Zelda Creator" in r.text, "created_by was never resolved to a name"
    # A cohort whose creator row is gone (ondelete=SET NULL) must render without
    # borrowing the other row's name.
    orphan_row = next(f for f in r.text.split("<tr ") if ">orphaned</a>" in f)
    assert "Zelda Creator" not in orphan_row


async def test_detail_of_an_empty_cohort_renders_the_no_members_state(
    client, db_session, admin, roster
):
    """With no memberships there are no adders to look up, and the members table
    is replaced by the empty state rather than rendering a headless table."""
    c = await _cohort(db_session, "empty", admin)
    r = await client.get(f"/admin/cohorts/{c.id}", headers=_auth(admin.id))
    assert r.status_code == 200
    assert "No members yet" in r.text
    # The add-agent picker still offers the whole active roster.
    for bot in ("SuBot", "WisemanBot", "CravattBot"):
        assert bot in r.text, f"{bot} missing from the picker"


async def test_deleting_an_unknown_cohort_is_a_404(client, db_session, admin):
    """A double-submitted delete (or a stale bookmark) must not raise, and must not
    claim to have deleted anything.

    This used to be a bare redirect to the list carrying neither ``error=`` nor
    ``notice=``. The successful path redirects with ``notice=Deleted+cohort+{name}``,
    so the silent version was the only outcome in the whole surface that reported
    nothing whatsoever — a second submit of an already-processed delete just landed
    back on the list. It is now a 404, the same answer ``add-agent`` and
    ``remove-agent`` give for a missing cohort and the same answer
    ``GET /admin/cohorts/{id}`` gives for this very id.
    """
    ghost = uuid.uuid4()
    r = await client.post(f"/admin/cohorts/{ghost}/delete", headers=_auth(admin.id))
    assert r.status_code == 404
    assert (await db_session.execute(
        select(CohortAuditEvent).where(CohortAuditEvent.cohort_id == ghost)
    )).scalars().all() == [], "a delete that deleted nothing must not be audited"


async def test_a_real_delete_still_reports_success(client, db_session, admin):
    """Positive control for the 404 above: the same route, given a cohort that does
    exist, still redirects with a notice. Without this, a handler that 404'd
    unconditionally would pass the test above."""
    c = await _cohort(db_session, "realdelete", admin)
    r = await client.post(f"/admin/cohorts/{c.id}/delete", headers=_auth(admin.id))
    assert r.status_code == 302
    assert "notice=Deleted+cohort+realdelete" in r.headers["location"]


async def test_adding_an_agent_to_an_unknown_cohort_is_a_404(
    client, db_session, admin, roster
):
    """The membership must not be created against a cohort id that does not exist:
    there is no FK from cohort_memberships.agent_id, and an orphan row would be
    invisible in every cohort view."""
    r = await client.post(
        f"/admin/cohorts/{uuid.uuid4()}/add-agent",
        data={"agent_id": "su"},
        headers=_auth(admin.id),
    )
    assert r.status_code == 404
    assert (await db_session.execute(select(CohortMembership))).scalars().all() == []


async def test_removing_an_agent_that_is_not_a_member_is_a_silent_no_op(
    client, db_session, admin, roster
):
    """A stale Remove button must neither 500 nor forge an audit event."""
    c = await _cohort(db_session, "wave", admin, members=["su"])
    r = await client.post(
        f"/admin/cohorts/{c.id}/remove-agent",
        data={"agent_id": "wiseman"},
        headers=_auth(admin.id),
    )
    assert r.status_code == 302
    assert r.headers["location"] == f"/admin/cohorts/{c.id}"
    rows = {
        (str(m.cohort_id), m.agent_id)
        for m in (await db_session.execute(select(CohortMembership))).scalars().all()
    }
    assert rows == {(str(c.id), "su")}, (
        f"removing a non-member touched the real membership: {rows}"
    )
    assert (await db_session.execute(
        select(CohortAuditEvent).where(CohortAuditEvent.cohort_id == c.id)
    )).scalars().all() == [], "a removal that removed nothing must not be audited"


async def test_removing_an_agent_from_an_unknown_cohort_is_a_404(
    client, db_session, admin, roster
):
    """Same handler, cohort row missing too.

    This used to redirect to ``/admin/cohorts/{ghost}`` — a detail page that then
    404s on its own, so the admin spent two round trips to reach the same error.
    The handler now answers 404 directly. Note this is *not* the same case as
    ``test_removing_an_agent_that_is_not_a_member_is_a_silent_no_op`` above, where
    the cohort exists and the redirect target is a real page.
    """
    ghost = uuid.uuid4()
    r = await client.post(
        f"/admin/cohorts/{ghost}/remove-agent",
        data={"agent_id": "su"},
        headers=_auth(admin.id),
    )
    assert r.status_code == 404
    assert (await db_session.execute(select(CohortAuditEvent))).scalars().all() == []


async def test_topology_save_round_trips_with_marker_payload(client, db_session, admin):
    """The new payload adds and removes exactly the ticked/unticked cells."""
    c1 = await _cohort(db_session, "alpha-marker", admin)
    c2 = await _cohort(db_session, "beta-marker", admin)
    await factories.make_agent(db_session, agent_id="ta1", bot_name="Ta1Bot")
    await factories.make_agent(db_session, agent_id="ta2", bot_name="Ta2Bot")
    # Pre-existing membership that the save must REMOVE (unticked but rendered).
    db_session.add(CohortMembership(cohort_id=c1.id, agent_id="ta2", added_by=admin.id))
    await db_session.commit()

    r = await client.post(
        "/admin/cohorts/topology",
        data={
            "present_agent": ["ta1", "ta2"],
            "present_cohort": [str(c1.id), str(c2.id)],
            "cell": [f"{c1.id}:ta1"],
        },
        headers=_auth(admin.id),
    )
    assert r.status_code == 302
    assert "1+added,+1+removed" in r.headers["location"], r.headers["location"]

    rows = {
        (str(cid), aid)
        for cid, aid in (await db_session.execute(
            select(CohortMembership.cohort_id, CohortMembership.agent_id)
        )).all()
    }
    assert rows == {(str(c1.id), "ta1")}


async def test_a_form_omitting_a_column_cannot_delete_that_columns_memberships(
    client, db_session, admin
):
    """The stale-form data-loss guard survives the cross-product reconstruction."""
    c1 = await _cohort(db_session, "shown", admin)
    c2 = await _cohort(db_session, "hidden", admin)
    await factories.make_agent(db_session, agent_id="tb1", bot_name="Tb1Bot")
    db_session.add(CohortMembership(cohort_id=c2.id, agent_id="tb1", added_by=admin.id))
    await db_session.commit()

    # c2 is NOT in present_cohort, so its cell was never rendered.
    r = await client.post(
        "/admin/cohorts/topology",
        data={"present_agent": ["tb1"], "present_cohort": [str(c1.id)]},
        headers=_auth(admin.id),
    )
    assert r.status_code == 302

    survivors = {
        (str(cid), aid)
        for cid, aid in (await db_session.execute(
            select(CohortMembership.cohort_id, CohortMembership.agent_id)
        )).all()
    }
    assert survivors == {(str(c2.id), "tb1")}, "a hidden column's membership was deleted"


async def test_a_form_omitting_a_row_cannot_delete_that_rows_memberships(
    client, db_session, admin
):
    c1 = await _cohort(db_session, "only", admin)
    await factories.make_agent(db_session, agent_id="tc1", bot_name="Tc1Bot")
    await factories.make_agent(db_session, agent_id="tc2", bot_name="Tc2Bot")
    db_session.add(CohortMembership(cohort_id=c1.id, agent_id="tc2", added_by=admin.id))
    await db_session.commit()

    r = await client.post(
        "/admin/cohorts/topology",
        data={"present_agent": ["tc1"], "present_cohort": [str(c1.id)]},
        headers=_auth(admin.id),
    )
    assert r.status_code == 302

    survivors = {
        aid for (aid,) in (await db_session.execute(
            select(CohortMembership.agent_id)
        )).all()
    }
    assert survivors == {"tc2"}, "a hidden row's membership was deleted"


async def test_full_matrix_payload_stays_under_the_field_limit(client, db_session, admin):
    """60x56 used to post 3,528 fields against Starlette's max_fields=1000."""
    cohorts = []
    for i in range(56):
        c = Cohort(name=f"c{i:03d}", created_by=admin.id)
        db_session.add(c)
        cohorts.append(c)
    await db_session.flush()
    for i in range(60):
        await factories.make_agent(
            db_session, agent_id=f"td{i:03d}", bot_name=f"Td{i:03d}Bot"
        )
    await db_session.commit()

    present_agents = [f"td{i:03d}" for i in range(60)]
    present_cohorts = [str(c.id) for c in cohorts]
    cell = [f"{cohorts[0].id}:td000"]
    total_fields = len(present_agents) + len(present_cohorts) + len(cell)
    assert total_fields == 117, f"expected 116 markers + 1 cell, got {total_fields}"

    r = await client.post(
        "/admin/cohorts/topology",
        data={"present_agent": present_agents, "present_cohort": present_cohorts, "cell": cell},
        headers=_auth(admin.id),
    )
    assert r.status_code == 302, r.text
    assert "1+added" in r.headers["location"]


async def test_a_payload_of_unknown_marker_ids_does_not_blow_up_or_delete_anything(
    client, db_session, admin, roster
):
    """Many garbage present_cohort/present_agent ids must not explode the
    cross product and must not be treated as an empty (``Nothing to save``) or
    malformed submission — they are simply inert, like any other stale id.

    ``rendered`` used to be built as the cross product of the RAW, unfiltered
    marker sets, so a payload naming only ids that no longer exist made the
    product multiplicative in attacker-controlled input: N garbage cohort ids
    times M garbage agent ids, regardless of how few real rows exist. This
    posts several hundred of each (a full-scale reproduction of the reported
    bound — tens of thousands squared — would itself be irresponsible to run
    in a test process) to confirm the request still completes quickly and
    behaves as a harmless no-op, and that it does not disturb a real,
    unrelated membership that was never named by any marker.
    """
    # A real membership, named by nothing in the payload below, that must survive.
    a = await _cohort(db_session, "untouched", admin, members=["su"])

    ghost_cohorts = [str(uuid.uuid4()) for _ in range(500)]
    ghost_agents = [f"ghost-agent-{i}" for i in range(500)]
    r = await client.post(
        "/admin/cohorts/topology",
        data={"present_cohort": ghost_cohorts, "present_agent": ghost_agents},
        headers=_auth(admin.id),
    )
    assert r.status_code == 302
    assert "error" not in r.headers["location"], (
        f"an all-unknown payload must be a harmless no-op, not an error: "
        f"{r.headers['location']}"
    )

    rows = {
        (str(m.cohort_id), m.agent_id)
        for m in (await db_session.execute(select(CohortMembership))).scalars().all()
    }
    assert rows == {(str(a.id), "su")}, "an all-unknown-id payload touched real data"


async def test_every_cohort_route_answers_a_missing_cohort_the_same_way(
    client, db_session, admin, roster
):
    """The three mutating cohort routes once disagreed three ways about a cohort id
    that does not exist: add-agent raised 404, delete redirected silently to the
    list, remove-agent redirected to a detail page that 404s. They now all match
    the GET detail page, which is the convention the rest of this module uses for a
    missing path-addressed row (see ``admin_user_delete``, ``admin_approve_agent``,
    ``admin_approve_access`` and friends). ``?error=`` redirects stay reserved for
    bad form input against a cohort that really exists.
    """
    ghost = uuid.uuid4()
    calls = [
        ("GET", f"/admin/cohorts/{ghost}", None),
        ("POST", f"/admin/cohorts/{ghost}/delete", {}),
        ("POST", f"/admin/cohorts/{ghost}/add-agent", {"agent_id": "su"}),
        ("POST", f"/admin/cohorts/{ghost}/remove-agent", {"agent_id": "su"}),
    ]
    codes = {}
    for method, path, data in calls:
        if method == "GET":
            r = await client.get(path, headers=_auth(admin.id))
        else:
            r = await client.post(path, data=data, headers=_auth(admin.id))
        codes[path.rsplit("/", 1)[-1]] = r.status_code
    assert set(codes.values()) == {404}, f"routes still disagree: {codes}"
    # And nothing was written on any of the four attempts.
    assert (await db_session.execute(select(CohortMembership))).scalars().all() == []
    assert (await db_session.execute(select(CohortAuditEvent))).scalars().all() == []
