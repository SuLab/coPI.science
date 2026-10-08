"""Cohort labels on the admin user pages (/admin/users, /admin/users/{id}).

Display only. Membership is keyed by agent slug (src/models/cohort.py), so a
user's cohorts are those of the agent they OWN through AgentRegistry.user_id —
never a delegated agent's. Real ASGI requests, real Postgres, real Jinja: the
same harness as tests/integration/test_admin_users.py.
"""

import base64
import json
import re
import uuid
from datetime import UTC, datetime

import pytest
from itsdangerous import TimestampSigner

from src.config import get_settings
from src.models import AgentDelegate, Cohort, CohortMembership
from tests import factories

pytestmark = pytest.mark.integration

# Name, Institution, ORCID, Access, Status, Agent, Cohorts, Pubs, Version,
# Claimed, Joined, Last Login — keep in step with test_admin_users._COLUMN_COUNT.
_COLUMN_COUNT = 12
_COHORTS = 6


def _auth(user_id) -> dict:
    """Forge the signed session cookie SessionMiddleware would issue."""
    signer = TimestampSigner(get_settings().secret_key)
    data = base64.b64encode(json.dumps({"user_id": str(user_id)}).encode())
    return {"Cookie": f"copi-session={signer.sign(data).decode()}"}


def _rendered_names(html: str) -> list[str]:
    return re.findall(r'<div class="text-sm font-medium text-gray-900">([^<]+)</div>', html)


def _cohorts_cell(html: str, name: str) -> str:
    rows = [
        m.group(0)
        for m in re.finditer(r"<tr\b.*?</tr>", html, re.S)
        if f">{name}</div>" in m.group(0)
    ]
    assert len(rows) == 1, f"expected exactly one row for {name!r}, got {len(rows)}"
    cells = re.findall(r"<td\b.*?</td>", rows[0], re.S)
    assert len(cells) == _COLUMN_COUNT, f"expected {_COLUMN_COUNT} cells, got {len(cells)}"
    return cells[_COHORTS]


def _cohorts_dd(html: str) -> str:
    m = re.search(r'<dd[^>]*\bid="user-cohorts"[^>]*>(.*?)</dd>', html, re.S)
    assert m, "the Cohorts <dd> is missing from the user detail page"
    return m.group(1)


@pytest.fixture
async def admin(db_session):
    return await factories.make_user(
        db_session, name="Site Admin", is_admin=True, email="admin@example.org"
    )


@pytest.fixture
async def world(db_session, admin):
    """Every case the labels must tell apart, in one roster."""

    async def owner(name, slug, status="active"):
        user = await factories.make_user(db_session, name=name, email=f"{slug}@example.org")
        agent = await factories.make_agent(
            db_session, user=user, agent_id=slug, bot_name=f"{slug.title()}Bot",
            pi_name=name, status=status,
        )
        return user, agent

    two, two_agent = await owner("Two Cohorts", "twoc")
    unco, _ = await owner("Uncohorted", "unco")
    parked, _ = await owner("Parked", "parked", status="inactive")
    agentless = await factories.make_user(db_session, name="Agentless", email="na@example.org")
    delegate = await factories.make_user(
        db_session, name="Delegate Only", email="del@example.org"
    )
    db_session.add(AgentDelegate(agent_registry_id=two_agent.id, user_id=delegate.id))

    # beta inserted first, so name order differs from insertion order.
    beta = Cohort(name="beta-c", created_by=admin.id)
    alpha = Cohort(name="alpha-c", created_by=admin.id)
    db_session.add_all([beta, alpha])
    await db_session.flush()
    for cohort, slug in ((beta, "twoc"), (alpha, "twoc"), (alpha, "parked")):
        db_session.add(
            CohortMembership(cohort_id=cohort.id, agent_id=slug, added_by=admin.id)
        )
    await db_session.flush()
    return {
        "two": two, "two_agent": two_agent, "unco": unco, "parked": parked,
        "agentless": agentless, "delegate": delegate, "alpha": alpha, "beta": beta,
    }


# --- /admin/users column -----------------------------------------------------


async def test_the_column_renders_with_its_header(client, admin, world):
    r = await client.get("/admin/users", headers=_auth(admin.id))
    assert r.status_code == 200
    # Assert on the table's own <thead>: the admin sub-nav already renders
    # ">Cohorts<" on every admin page (base.html), so a page-wide check proves nothing.
    thead = re.search(r"<thead\b.*?</thead>", r.text, re.S).group(0)
    ths = re.findall(r"<th\b[^>]*>([^<]*)</th>", thead)
    assert len(ths) == _COLUMN_COUNT and ths[_COHORTS] == "Cohorts"
    _cohorts_cell(r.text, "Two Cohorts")  # asserts the 12-cell row shape


async def test_cohorts_are_listed_in_name_order_and_link_above_the_row_overlay(
    client, admin, world
):
    r = await client.get("/admin/users", headers=_auth(admin.id))
    cell = _cohorts_cell(r.text, "Two Cohorts")
    assert cell.index("alpha-c") < cell.index("beta-c")
    for c in (world["alpha"], world["beta"]):
        # relative z-10 lifts the link above the row's covering ::after link.
        assert f'<a href="/admin/cohorts/{c.id}" class="relative z-10">' in cell


async def test_active_badges_are_brand_and_parked_badges_are_muted(client, admin, world):
    r = await client.get("/admin/users", headers=_auth(admin.id))
    active = _cohorts_cell(r.text, "Two Cohorts")
    parked = _cohorts_cell(r.text, "Parked")
    assert "bg-indigo-100 text-indigo-700" in active
    assert "alpha-c" in parked
    assert "bg-gray-100 text-gray-700" in parked
    assert "bg-indigo-100" not in parked


async def test_no_agent_and_no_cohort_render_differently(client, admin, world):
    r = await client.get("/admin/users", headers=_auth(admin.id))
    assert "No agent" in _cohorts_cell(r.text, "Agentless")
    unco = _cohorts_cell(r.text, "Uncohorted")
    assert "—" in unco and "No agent" not in unco


async def test_a_delegate_does_not_inherit_the_agents_cohorts(client, admin, world):
    r = await client.get("/admin/users", headers=_auth(admin.id))
    cell = _cohorts_cell(r.text, "Delegate Only")
    assert "No agent" in cell
    assert "alpha-c" not in cell and "beta-c" not in cell


# --- /admin/users cohort_filter ----------------------------------------------


async def test_cohort_filter_narrows_to_the_cohorts_members(client, admin, world):
    unfiltered = await client.get("/admin/users", headers=_auth(admin.id))
    assert {"Two Cohorts", "Uncohorted", "Parked", "Agentless", "Delegate Only"} <= set(
        _rendered_names(unfiltered.text)
    )

    r = await client.get(
        f"/admin/users?cohort_filter={world['alpha'].id}", headers=_auth(admin.id)
    )
    assert r.status_code == 200
    assert set(_rendered_names(r.text)) == {"Two Cohorts", "Parked"}

    r = await client.get(
        f"/admin/users?cohort_filter={world['beta'].id}", headers=_auth(admin.id)
    )
    assert set(_rendered_names(r.text)) == {"Two Cohorts"}


async def test_the_sentinels_split_no_cohort_from_no_agent(client, admin, world):
    r = await client.get("/admin/users?cohort_filter=__none__", headers=_auth(admin.id))
    names = set(_rendered_names(r.text))
    assert "Uncohorted" in names
    assert not names & {"Two Cohorts", "Parked", "Agentless", "Delegate Only", "Site Admin"}

    r = await client.get("/admin/users?cohort_filter=__no_agent__", headers=_auth(admin.id))
    names = set(_rendered_names(r.text))
    assert {"Agentless", "Delegate Only", "Site Admin"} <= names
    assert not names & {"Two Cohorts", "Uncohorted", "Parked"}


async def test_a_cohort_named_none_is_filtered_by_id_not_by_name(
    client, db_session, admin, world
):
    none = Cohort(name="none", created_by=admin.id)
    db_session.add(none)
    await db_session.flush()
    db_session.add(CohortMembership(cohort_id=none.id, agent_id="unco", added_by=admin.id))
    await db_session.flush()

    r = await client.get(f"/admin/users?cohort_filter={none.id}", headers=_auth(admin.id))
    assert set(_rendered_names(r.text)) == {"Uncohorted"}
    r = await client.get("/admin/users?cohort_filter=none", headers=_auth(admin.id))
    assert _rendered_names(r.text) == []


@pytest.mark.parametrize("value", ["not-a-uuid", str(uuid.uuid4()), "alpha-c"])
async def test_an_unknown_cohort_filter_matches_nothing_rather_than_500ing(
    client, admin, world, value
):
    r = await client.get(f"/admin/users?cohort_filter={value}", headers=_auth(admin.id))
    assert r.status_code == 200
    assert _rendered_names(r.text) == []
    assert "No users found" in r.text
    assert f'colspan="{_COLUMN_COUNT}"' in r.text


async def test_cohort_filter_preselects_its_own_option(client, admin, world):
    alpha, beta = world["alpha"].id, world["beta"].id
    r = await client.get(f"/admin/users?cohort_filter={alpha}", headers=_auth(admin.id))
    assert re.search(rf'<option value="{alpha}"[^>]*\bselected\b', r.text)
    assert not re.search(rf'<option value="{beta}"[^>]*\bselected\b', r.text)

    r = await client.get("/admin/users?cohort_filter=__none__", headers=_auth(admin.id))
    assert re.search(r'<option value="__none__"[^>]*\bselected\b', r.text)


async def test_cohort_filter_composes_with_the_access_filter(client, db_session, admin, world):
    world["parked"].access_status = "pending"
    # Pending but outside alpha: only cohort_filter can drop it, so the test fails if
    # the route ignores cohort_filter (and fails on "Two Cohorts" if it ignores access).
    world["unco"].access_status = "pending"
    await db_session.flush()
    r = await client.get(
        f"/admin/users?cohort_filter={world['alpha'].id}&access_filter=pending",
        headers=_auth(admin.id),
    )
    assert set(_rendered_names(r.text)) == {"Parked"}


async def test_cohort_filter_composes_with_the_claimed_filter(client, db_session, admin, world):
    claimed = datetime(2026, 2, 1, tzinfo=UTC)
    world["parked"].claimed_at = claimed
    world["unco"].claimed_at = claimed  # claimed but outside alpha
    await db_session.flush()
    r = await client.get(
        f"/admin/users?cohort_filter={world['alpha'].id}&claimed_filter=claimed",
        headers=_auth(admin.id),
    )
    assert set(_rendered_names(r.text)) == {"Parked"}


# --- /admin/users/{id} ---------------------------------------------------------


async def test_detail_shows_the_owned_agent_and_links_each_cohort(client, admin, world):
    r = await client.get(f"/admin/users/{world['two'].id}", headers=_auth(admin.id))
    assert r.status_code == 200
    assert f'href="/admin/agents/{world["two_agent"].id}"' in r.text
    assert "TwocBot" in r.text
    assert "(active)" in r.text
    dd = _cohorts_dd(r.text)
    assert dd.index("alpha-c") < dd.index("beta-c")
    for c in (world["alpha"], world["beta"]):
        assert f'href="/admin/cohorts/{c.id}"' in dd


@pytest.mark.parametrize("who", ["agentless", "delegate"])
async def test_detail_without_an_owned_agent_says_cohorts_are_per_agent(
    client, admin, world, who
):
    r = await client.get(f"/admin/users/{world[who].id}", headers=_auth(admin.id))
    assert r.status_code == 200
    dd = _cohorts_dd(r.text)
    assert "No agent — cohorts are assigned per agent" in dd
    assert "alpha-c" not in dd


async def test_detail_uncohorted_agent_is_unrestricted_under_the_open_policy(
    client, admin, world, monkeypatch
):
    monkeypatch.setattr(get_settings(), "cohort_isolation_enabled", True)
    monkeypatch.setattr(get_settings(), "cohort_default_policy", "open")
    r = await client.get(f"/admin/users/{world['unco'].id}", headers=_auth(admin.id))
    assert "None — unrestricted" in _cohorts_dd(r.text)


async def test_detail_uncohorted_agent_is_isolated_under_the_isolated_policy(
    client, admin, world, monkeypatch
):
    # twoc is an active cohorted agent, so the isolated-policy preflight passes.
    monkeypatch.setattr(get_settings(), "cohort_isolation_enabled", True)
    monkeypatch.setattr(get_settings(), "cohort_default_policy", "isolated")
    r = await client.get(f"/admin/users/{world['unco'].id}", headers=_auth(admin.id))
    assert "None — isolated" in _cohorts_dd(r.text)


async def test_detail_with_isolation_off_says_recorded_not_enforced(
    client, admin, world, monkeypatch
):
    monkeypatch.setattr(get_settings(), "cohort_isolation_enabled", False)
    r = await client.get(f"/admin/users/{world['two'].id}", headers=_auth(admin.id))
    assert "Recorded, not enforced: cohort isolation is off" in _cohorts_dd(r.text)
    r = await client.get(f"/admin/users/{world['unco'].id}", headers=_auth(admin.id))
    assert "None (cohort isolation is off)" in _cohorts_dd(r.text)


async def test_detail_parked_agent_is_marked_not_running(
    client, admin, world, monkeypatch
):
    monkeypatch.setattr(get_settings(), "cohort_isolation_enabled", True)
    monkeypatch.setattr(get_settings(), "cohort_default_policy", "open")
    r = await client.get(f"/admin/users/{world['parked'].id}", headers=_auth(admin.id))
    dd = _cohorts_dd(r.text)
    assert "alpha-c" in dd
    assert "bg-gray-100 text-gray-700" in dd
    # Not "not in effect": compute_gates builds mate sets from every membership row,
    # so the parked agent still sits in its active mates' allowed senders.
    assert "This agent is inactive and does not run" in dd
    assert "not in effect" not in dd


async def test_detail_says_when_the_isolated_policy_was_forced_off(
    client, db_session, admin, world, monkeypatch
):
    # Isolated policy with no ACTIVE cohorted agent: the preflight refuses and every
    # gate is None, so "unrestricted" would hide why — the page must say forced off.
    monkeypatch.setattr(get_settings(), "cohort_isolation_enabled", True)
    monkeypatch.setattr(get_settings(), "cohort_default_policy", "isolated")
    world["two_agent"].status = "inactive"
    await db_session.flush()
    r = await client.get(f"/admin/users/{world['unco'].id}", headers=_auth(admin.id))
    dd = _cohorts_dd(r.text)
    assert "None (isolation was forced off" in dd
    assert "unrestricted" not in dd and "isolated (" not in dd
    r = await client.get(f"/admin/users/{world['two'].id}", headers=_auth(admin.id))
    assert "Recorded, not enforced: isolation was forced off" in _cohorts_dd(r.text)


async def test_detail_non_active_uncohorted_agent_reads_plain_none(
    client, db_session, admin, world, monkeypatch
):
    # A non-active agent is absent from the gate preview: no unrestricted/isolated claim.
    monkeypatch.setattr(get_settings(), "cohort_isolation_enabled", True)
    idle = await factories.make_user(db_session, name="Idle", email="idle@example.org")
    await factories.make_agent(
        db_session, user=idle, agent_id="idle", bot_name="IdleBot",
        pi_name="Idle", status="inactive",
    )
    await db_session.flush()
    r = await client.get(f"/admin/users/{idle.id}", headers=_auth(admin.id))
    dd = _cohorts_dd(r.text)
    assert "None" in dd
    for wrong in ("unrestricted", "isolated", "isolation is off"):
        assert wrong not in dd


async def test_detail_enforced_membership_carries_no_caveat(client, admin, world, monkeypatch):
    monkeypatch.setattr(get_settings(), "cohort_isolation_enabled", True)
    r = await client.get(f"/admin/users/{world['two'].id}", headers=_auth(admin.id))
    dd = _cohorts_dd(r.text)
    assert "bg-indigo-100 text-indigo-700" in dd
    assert "Recorded" not in dd
