"""The four PI-only write endpoints, and the manager escalation they closed.

Before ``get_pi_user`` these four were gated on plain
``Depends(get_current_user)``, so a manager could:

    POST /onboarding/save-profile   -> onboarding_complete = True + a
                                       ResearcherProfile row
    POST /agent/request             -> an AgentRegistry row of their own

which is a lab bot for an account D7 says has no lab. The read-only bounces
elsewhere (auth.py's post-login redirect, onboarding.py's GET) never touched
either POST.

Every case here asserts the *effect*, not only the status code: a 403 on a
route that had stopped working anyway would prove nothing, so each denial is
paired with the identical request from a PI, which must change exactly the
same state the manager failed to change.
"""

import pytest
from sqlalchemy import func, select

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    AgentRegistry,
    Job,
    ResearcherProfile,
    User,
)
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


def _save_profile_form(u: User) -> dict:
    return {
        "email": u.email or "",
        "research_summary": f"WRITE-SWEEP-{u.orcid}",
        "techniques": "cryo-EM",
    }


# (path, form builder). The audit found exactly these four still on
# get_current_user; they are listed here rather than enumerated off the
# routers because the property under test is per-endpoint authorization, not
# route coverage — the router-level enumeration lives in
# test_onboarding_flow.py's endpoint inventory.
PI_ONLY_WRITES = [
    ("/onboarding/save-profile", _save_profile_form),
    ("/onboarding/retry", lambda u: {}),
    ("/profile/refresh", lambda u: {}),
    ("/agent/request", lambda u: {}),
]
_IDS = [p for p, _ in PI_ONLY_WRITES]


async def _snapshot(db, user_id) -> tuple:
    """Everything the four endpoints can change, for one user.

    Column-level selects, not attribute reads off a live ORM instance: they
    always emit SQL, so they see the route's committed savepoint without an
    ``expire_all()`` that would detach the caller's own fixture objects.
    """
    return (
        await db.scalar(select(User.onboarding_complete).where(User.id == user_id)),
        await db.scalar(
            select(func.count(Job.id)).where(
                Job.user_id == user_id, Job.type == "generate_profile"
            )
        ),
        await db.scalar(
            select(func.count(AgentRegistry.id)).where(AgentRegistry.user_id == user_id)
        ),
        await db.scalar(
            select(ResearcherProfile.research_summary).where(
                ResearcherProfile.user_id == user_id
            )
        ),
    )


@pytest.mark.parametrize("path,build", PI_ONLY_WRITES, ids=_IDS)
async def test_a_manager_is_refused_every_pi_write(client, db_session, path, build):
    """The manager half. Deliberately given the *most* privileged manager
    possible — onboarding already complete, a ResearcherProfile already on the
    row — so the refusal comes from the role gate and not from the readiness
    check inside request_agent (which would 400, not 403, and would stop
    protecting anything the moment a manager acquired those two fields)."""
    mgr = await factories.make_user(
        db_session, user_role=USER_ROLE_MANAGER, onboarding_complete=True
    )
    await factories.make_profile(db_session, user=mgr, research_summary="untouched")
    await db_session.flush()

    before = await _snapshot(db_session, mgr.id)
    r = await client.post(path, data=build(mgr), headers=auth_headers(mgr.id))
    assert r.status_code == 403, f"{path} served a manager"
    assert await _snapshot(db_session, mgr.id) == before, f"{path} acted for a manager"


@pytest.mark.parametrize("path,build", PI_ONLY_WRITES, ids=_IDS)
async def test_a_pi_can_still_use_every_pi_write(client, db_session, path, build):
    """The control for the sweep above. Without it, four endpoints that had
    simply broken would score as correctly protected."""
    pi = await factories.make_user(
        db_session, user_role=USER_ROLE_PI, name="Ada Controlcase"
    )
    await factories.make_profile(db_session, user=pi, research_summary="untouched")
    await db_session.flush()

    before = await _snapshot(db_session, pi.id)
    r = await client.post(path, data=build(pi), headers=auth_headers(pi.id))
    assert r.status_code == 302, f"{path} refused a PI"
    assert await _snapshot(db_session, pi.id) != before, (
        f"{path} changes nothing even for a PI, so the manager denial above "
        "proves nothing"
    )


@pytest.mark.parametrize("path,build", PI_ONLY_WRITES, ids=_IDS)
async def test_an_admin_keeps_every_pi_write(client, db_session, path, build):
    """Deliberate scope call: the gate is `is_manager`, not `user_role ==
    'pi'`. An admin is not a `pi` either, and templates/base.html still offers
    admins the My Profile and My Agent links, so a `== 'pi'` gate would 403
    every admin on their own navigation. Admins keep these surfaces exactly as
    they did before this branch; only managers lose them."""
    admin = await factories.make_user(
        db_session, user_role=USER_ROLE_ADMIN, name="Ozzy Adminson"
    )
    await factories.make_profile(db_session, user=admin, research_summary="untouched")
    await db_session.flush()

    before = await _snapshot(db_session, admin.id)
    r = await client.post(path, data=build(admin), headers=auth_headers(admin.id))
    assert r.status_code == 302, f"{path} refused an admin"
    assert await _snapshot(db_session, admin.id) != before, f"{path} was inert for an admin"


async def test_a_manager_with_a_completed_profile_still_gets_no_agent(client, db_session):
    """The finding itself, end to end, and the assertion that matters most.

    The two POSTs are fired in the order an attacker would use them. The
    second one is the whole point: even granting the manager the state that
    POST #1 was supposed to produce — onboarding_complete plus a
    ResearcherProfile, handed over directly here — request_agent's body check
    is satisfied, so nothing but the role gate stands between a manager and an
    AgentRegistry row.
    """
    mgr = await factories.make_user(
        db_session, user_role=USER_ROLE_MANAGER, name="Mallory Manager",
        onboarding_complete=False,
    )
    await db_session.flush()

    # Step 1: the onboarding write that mints onboarding_complete + a profile.
    r1 = await client.post(
        "/onboarding/save-profile", data=_save_profile_form(mgr), headers=auth_headers(mgr.id)
    )
    assert r1.status_code == 403
    assert await db_session.scalar(
        select(User.onboarding_complete).where(User.id == mgr.id)
    ) is False
    assert await db_session.scalar(
        select(func.count(ResearcherProfile.id)).where(ResearcherProfile.user_id == mgr.id)
    ) == 0

    # Step 2: hand the manager that state anyway, then ask for the bot.
    mgr.onboarding_complete = True
    await factories.make_profile(db_session, user=mgr)
    await db_session.flush()

    r2 = await client.post("/agent/request", headers=auth_headers(mgr.id))
    assert r2.status_code == 403, "a manager obtained a lab agent (D7)"
    assert await db_session.scalar(
        select(func.count(AgentRegistry.id)).where(AgentRegistry.user_id == mgr.id)
    ) == 0

    # Control: the identical request from a PI in the identical state DOES
    # create the row, so the zero above is the gate and not a dead endpoint.
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI, name="Vera Verity")
    await factories.make_profile(db_session, user=pi)
    await db_session.flush()
    r3 = await client.post("/agent/request", headers=auth_headers(pi.id))
    assert r3.status_code == 302
    assert await db_session.scalar(
        select(func.count(AgentRegistry.id)).where(AgentRegistry.user_id == pi.id)
    ) == 1
