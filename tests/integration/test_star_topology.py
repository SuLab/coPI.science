"""ensure_star_spokes — the audited, idempotent star-topology maintainer.

The running topology is one cohort per lab, ``hub-{agent_id}``, holding
{lab, hub, grantbot}. The 62 originals were created by ad-hoc SQL and left no
audit rows; ``SimulationEngine._validate_star_topology`` hard-fails run startup
for any live pi_lab whose gate lacks the hub (seven agents activated on
2026-08-26 were in exactly that state). These tests pin the replacement:
creates what is missing, adds only what is missing, removes nothing, audits
every write, and refuses to guess when the roster has no single hub.

Hermeticity: the shared test database carries committed leftovers from other
suites (e.g. test_provisioning_loop.py's ``stub`` agent survives its own run),
and ensure_star_spokes deliberately operates on the WHOLE roster — so every
assertion here is scoped to this file's own slugs, never to whole tables.
"""

import base64
import json

import pytest
from itsdangerous import TimestampSigner
from sqlalchemy import select

from src.config import get_settings
from src.models import (
    COHORT_ACTION_AGENT_ADDED,
    COHORT_ACTION_CREATED,
    USER_ROLE_ADMIN,
    Cohort,
    CohortAuditEvent,
    CohortMembership,
)
from src.services.star_topology import EXTRA_SPOKE_MEMBERS, ensure_star_spokes
from tests import factories

pytestmark = pytest.mark.integration

LAB_A = "starlab1"
LAB_B = "starlab2"
MY_COHORTS = (f"hub-{LAB_A}", f"hub-{LAB_B}")


@pytest.fixture
async def hub(db_session):
    return await factories.make_agent(
        db_session, agent_id="blackbird", bot_name="BlackbirdBot",
        pi_name="Blackbird", role="scout_hub", status="active",
    )


@pytest.fixture
async def labs(db_session, hub):
    out = {}
    for aid in (LAB_A, LAB_B):
        user = await factories.make_user(db_session, email=f"{aid}@example.org")
        out[aid] = await factories.make_agent(
            db_session, user=user, agent_id=aid, bot_name=f"{aid.title()}Bot",
            pi_name=f"PI {aid}", status="active",
        )
    await db_session.flush()
    return out


async def _members(db_session, name: str) -> set[str]:
    cohort = (
        await db_session.execute(select(Cohort).where(Cohort.name == name))
    ).scalar_one_or_none()
    if cohort is None:
        return set()
    rows = (
        await db_session.execute(
            select(CohortMembership.agent_id).where(
                CohortMembership.cohort_id == cohort.id
            )
        )
    ).all()
    return {r[0] for r in rows}


async def _my_events(db_session, action: str) -> list[CohortAuditEvent]:
    return (
        await db_session.execute(
            select(CohortAuditEvent).where(
                CohortAuditEvent.action == action,
                CohortAuditEvent.cohort_name.in_(MY_COHORTS),
            )
        )
    ).scalars().all()


async def test_a_missing_spoke_is_created_with_the_canonical_members(
    db_session, hub, labs
):
    report = await ensure_star_spokes(db_session, apply=True)

    assert set(MY_COHORTS) <= set(report.created_cohorts)
    for aid in labs:
        assert await _members(db_session, f"hub-{aid}") == {
            aid, "blackbird", *EXTRA_SPOKE_MEMBERS,
        }

    created_events = await _my_events(db_session, COHORT_ACTION_CREATED)
    assert sorted(e.cohort_name for e in created_events) == sorted(MY_COHORTS)
    added_events = await _my_events(db_session, COHORT_ACTION_AGENT_ADDED)
    assert len(added_events) == 2 * (2 + len(EXTRA_SPOKE_MEMBERS))


async def test_running_twice_changes_nothing(db_session, hub, labs):
    await ensure_star_spokes(db_session, apply=True)
    report = await ensure_star_spokes(db_session, apply=True)

    assert report.created_cohorts == []
    assert report.added_members == []
    assert set(MY_COHORTS) <= set(report.complete)


async def test_a_partial_spoke_gains_only_its_missing_members(db_session, hub, labs):
    cohort = Cohort(name=f"hub-{LAB_A}", description="hand-made")
    db_session.add(cohort)
    await db_session.flush()
    db_session.add(CohortMembership(cohort_id=cohort.id, agent_id=LAB_A))
    await db_session.flush()

    report = await ensure_star_spokes(db_session, apply=True)

    assert f"hub-{LAB_A}" not in report.created_cohorts
    assert (f"hub-{LAB_A}", "blackbird") in report.added_members
    assert (f"hub-{LAB_A}", LAB_A) not in report.added_members
    assert await _members(db_session, f"hub-{LAB_A}") == {
        LAB_A, "blackbird", *EXTRA_SPOKE_MEMBERS,
    }


async def test_dry_run_reports_the_plan_and_writes_nothing(db_session, hub, labs):
    report = await ensure_star_spokes(db_session, apply=False)

    assert set(MY_COHORTS) <= set(report.created_cohorts)
    assert not report.applied
    assert await _members(db_session, f"hub-{LAB_A}") == set()
    for action in (COHORT_ACTION_CREATED, COHORT_ACTION_AGENT_ADDED):
        assert await _my_events(db_session, action) == []


async def test_a_foreign_member_is_reported_never_removed(db_session, hub, labs):
    # A lab-to-lab membership is the topology violation the engine's startup
    # validator exists to refuse — this maintainer must surface it, not "fix"
    # it by deleting someone's row.
    cohort = Cohort(name=f"hub-{LAB_A}", description="contaminated")
    db_session.add(cohort)
    await db_session.flush()
    for aid in (LAB_A, "blackbird", *EXTRA_SPOKE_MEMBERS, LAB_B):
        db_session.add(CohortMembership(cohort_id=cohort.id, agent_id=aid))
    await db_session.flush()

    report = await ensure_star_spokes(db_session, apply=True)

    assert any(LAB_B in a and f"hub-{LAB_A}" in a for a in report.anomalies)
    assert LAB_B in await _members(db_session, f"hub-{LAB_A}")


async def test_an_overlong_slug_is_an_anomaly_not_a_crash(db_session, hub):
    long_slug = "x" * 45  # hub- + 45 = 49 > the 48-char Cohort.name column
    await factories.make_agent(
        db_session, agent_id=long_slug, bot_name="LongBot",
        pi_name="PI Long", status="active",
    )
    report = await ensure_star_spokes(db_session, apply=True)

    assert any(long_slug in a for a in report.anomalies)
    assert await _members(db_session, f"hub-{long_slug}") == set()


async def test_refuses_a_roster_without_exactly_one_hub(db_session, labs):
    # ``labs`` depends on ``hub``, so delete the hub row to simulate its absence.
    from src.models import AgentRegistry
    hub_row = (
        await db_session.execute(
            select(AgentRegistry).where(AgentRegistry.agent_id == "blackbird")
        )
    ).scalar_one()
    await db_session.delete(hub_row)
    await db_session.flush()

    with pytest.raises(ValueError, match="scout_hub"):
        await ensure_star_spokes(db_session, apply=True)


async def test_only_limits_the_scope_to_the_named_labs(db_session, hub, labs):
    report = await ensure_star_spokes(db_session, apply=True, only={LAB_A})

    assert report.created_cohorts == [f"hub-{LAB_A}"]
    assert await _members(db_session, f"hub-{LAB_A}") == {
        LAB_A, "blackbird", *EXTRA_SPOKE_MEMBERS,
    }
    assert await _members(db_session, f"hub-{LAB_B}") == set()


async def test_only_with_an_ineligible_slug_is_an_anomaly(db_session, hub, labs):
    report = await ensure_star_spokes(
        db_session, apply=True, only={"no-such-agent"}
    )

    assert report.created_cohorts == []
    assert report.added_members == []
    assert any("no-such-agent" in a for a in report.anomalies)


# --- the admin UI buttons ----------------------------------------------------


def _auth(user_id) -> dict:
    """Forge the signed session cookie SessionMiddleware would issue."""
    signer = TimestampSigner(get_settings().secret_key)
    data = base64.b64encode(json.dumps({"user_id": str(user_id)}).encode())
    return {"Cookie": f"copi-session={signer.sign(data).decode()}"}


@pytest.fixture
async def admin(db_session):
    return await factories.make_user(
        db_session, user_role=USER_ROLE_ADMIN, email="star-admin@example.org"
    )


async def test_the_wire_all_button_creates_the_missing_spokes(
    client, db_session, admin, hub, labs
):
    r = await client.post(
        "/admin/cohorts/ensure-star-spokes", headers=_auth(admin.id)
    )

    assert r.status_code == 302
    assert "notice" in r.headers["location"]
    for aid in labs:
        assert await _members(db_session, f"hub-{aid}") == {
            aid, "blackbird", *EXTRA_SPOKE_MEMBERS,
        }
    # The button click is attributed — unlike the script, which writes no actor.
    events = await _my_events(db_session, COHORT_ACTION_CREATED)
    assert {e.actor_id for e in events} == {admin.id}


async def test_the_single_agent_button_wires_just_that_lab(
    client, db_session, admin, hub, labs
):
    agent = labs[LAB_A]
    r = await client.post(
        f"/admin/agents/{agent.id}/ensure-spoke", headers=_auth(admin.id)
    )

    assert r.status_code == 302
    assert r.headers["location"].startswith(f"/admin/agents/{agent.id}")
    assert await _members(db_session, f"hub-{LAB_A}") == {
        LAB_A, "blackbird", *EXTRA_SPOKE_MEMBERS,
    }
    assert await _members(db_session, f"hub-{LAB_B}") == set()


async def test_the_spoke_buttons_are_admin_only(client, db_session, hub, labs):
    pi = await factories.make_user(db_session, email="star-pi@example.org")

    r = await client.post(
        "/admin/cohorts/ensure-star-spokes", headers=_auth(pi.id)
    )
    assert r.status_code == 403
    r = await client.post(
        f"/admin/agents/{labs[LAB_A].id}/ensure-spoke", headers=_auth(pi.id)
    )
    assert r.status_code == 403
    assert await _members(db_session, f"hub-{LAB_A}") == set()


async def test_the_cohorts_page_offers_the_button_while_spokes_are_missing(
    client, db_session, admin, hub, labs
):
    r = await client.get("/admin/cohorts", headers=_auth(admin.id))
    assert r.status_code == 200
    assert "/admin/cohorts/ensure-star-spokes" in r.text

    await ensure_star_spokes(db_session, apply=True)
    r = await client.get("/admin/cohorts", headers=_auth(admin.id))
    assert r.status_code == 200
    assert "/admin/cohorts/ensure-star-spokes" not in r.text


async def test_agent_detail_shows_spoke_state_and_the_wire_button(
    client, db_session, admin, hub, labs
):
    agent = labs[LAB_A]
    r = await client.get(f"/admin/agents/{agent.id}", headers=_auth(admin.id))
    assert r.status_code == 200
    assert f"/admin/agents/{agent.id}/ensure-spoke" in r.text

    await ensure_star_spokes(db_session, apply=True, only={LAB_A})
    r = await client.get(f"/admin/agents/{agent.id}", headers=_auth(admin.id))
    assert r.status_code == 200
    assert f"/admin/agents/{agent.id}/ensure-spoke" not in r.text
