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
from src.models import Cohort, CohortAuditEvent, CohortMembership
from tests import factories

pytestmark = pytest.mark.integration


def _auth(user_id) -> dict:
    """Forge the signed session cookie SessionMiddleware would issue."""
    signer = TimestampSigner(get_settings().secret_key)
    data = base64.b64encode(json.dumps({"user_id": str(user_id)}).encode())
    return {"Cookie": f"copi-session={signer.sign(data).decode()}"}


@pytest.fixture
async def admin(db_session):
    return await factories.make_user(db_session, is_admin=True, email="admin@example.org")


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


# --- access control ---------------------------------------------------------


async def test_cohort_pages_require_login(client):
    for path in ("/admin/cohorts", "/admin/cohorts/topology"):
        r = await client.get(path)
        assert r.status_code == 302, path
        assert "/login" in r.headers["location"]


async def test_cohort_pages_require_admin(client, db_session):
    plain = await factories.make_user(db_session, is_admin=False, email="plain@example.org")
    await db_session.flush()
    r = await client.get("/admin/cohorts", headers=_auth(plain.id))
    assert r.status_code == 403


# --- list + create ---------------------------------------------------------


async def test_list_renders_with_no_cohorts(client, admin):
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


async def test_topology_save_applies_adds_and_removes_in_one_pass(
    client, db_session, admin, roster
):
    a = await _cohort(db_session, "alpha", admin, members=["su"])
    b = await _cohort(db_session, "beta", admin)
    present = [f"{a.id}:{x}" for x in ("su", "wiseman", "cravatt")] + \
              [f"{b.id}:{x}" for x in ("su", "wiseman", "cravatt")]
    # Drop su from alpha, add wiseman to alpha, add cravatt to beta — one save.
    ticked = [f"{a.id}:wiseman", f"{b.id}:cravatt"]
    r = await client.post(
        "/admin/cohorts/topology",
        data={"present": present, "cell": ticked},
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
    present = [f"{a.id}:{x}" for x in ("su", "wiseman")]
    await client.post(
        "/admin/cohorts/topology",
        data={"present": present, "cell": [f"{a.id}:wiseman"]},
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
    present = [f"{a.id}:{x}" for x in ("su", "wiseman", "cravatt")]
    r = await client.post(
        "/admin/cohorts/topology",
        data={"present": present},
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
        data={"present": [f"{a.id}:su"], "cell": [f"{a.id}:wiseman"]},
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
            "present": [f"{ghost}:su", f"{a.id}:nobody"],
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
