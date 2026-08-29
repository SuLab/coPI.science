"""Reviewer-role predicates, login/onboarding routing, and PI-write denial.

A reviewer (Task 1's `USER_ROLE_REVIEWER` / `User.is_reviewer`) can score and
comment on assessments but is neither staff (no manager/admin surfaces) nor a
PI (no lab profile or agent). This file pins:

  - `get_review_user` (new): admin, manager, OR reviewer; still refuses a PI.
  - `get_staff_user` still refuses a reviewer (is_staff deliberately excludes
    it — a reviewer must never reach manager writes, discussions, activity).
  - `get_pi_user` now also refuses a reviewer, alongside the existing manager
    denial (additive denylist — admins keep every PI write).
  - Login/onboarding/profile GETs bounce a reviewer to /manager/assessments
    rather than rendering a PI page it can never complete.
  - Task 3 (below): the manager router actually admits a reviewer to exactly
    four read routes — GET /manager, /manager/pis, /manager/pis/{id},
    /manager/assessments, /manager/assessments/{id} — and refuses it
    everywhere else on that router, and the manager templates render
    read-only for a reviewer (and for an admin impersonating one).

The full follow-the-chain-to-200 assertion on /manager/assessments, deferred
from Task 2 because the manager router did not yet admit a reviewer, is
`test_reviewer_full_login_chain_terminates` below.
"""

import re
import uuid

import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import func, select

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    USER_ROLE_REVIEWER,
    Job,
    OpportunityAssessment,
    ResearcherProfile,
    User,
)
from src.services.jhu_rules import set_tenure_start
from tests import factories
from tests.integration.test_assessment_queue_controls import _row_slice, _seed_reviewed_row
from tests.integration.test_manager_access import auth_headers
from tests.integration.test_pi_only_writes import (
    _IDS,
    PI_ONLY_WRITES,
    _save_profile_form,
    _snapshot,
)

pytestmark = pytest.mark.integration

_PARAM_RE = re.compile(r"\{(\w+)\}")


@pytest.mark.parametrize(
    "role,expected",
    [
        (USER_ROLE_PI, 403),
        (USER_ROLE_MANAGER, 200),
        (USER_ROLE_ADMIN, 200),
        (USER_ROLE_REVIEWER, 200),
    ],
)
async def test_get_review_user_gates_by_role(db_session, monkeypatch, role, expected):
    """Throwaway-app probe cloned from
    test_manager_access.py::test_get_staff_user_gates_by_role, targeting the
    new get_review_user instead."""
    import httpx
    from httpx import ASGITransport

    from src.config import get_settings
    from src.database import get_db
    from src.dependencies import get_review_user

    user = await factories.make_user(db_session, user_role=role)

    app = FastAPI()

    @app.get("/probe")
    async def probe(u: User = Depends(get_review_user)):  # noqa: B008
        return {"role": u.user_role}

    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    from starlette.middleware.sessions import SessionMiddleware

    app.add_middleware(
        SessionMiddleware,
        secret_key=get_settings().secret_key,
        session_cookie="copi-session",
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/probe", headers=auth_headers(user.id))
    assert r.status_code == expected


async def test_get_staff_user_still_refuses_a_reviewer(db_session, monkeypatch):
    """Same probe, but against get_staff_user: a reviewer must stay off the
    manager router entirely, not just off get_pi_user."""
    import httpx
    from httpx import ASGITransport

    from src.config import get_settings
    from src.database import get_db
    from src.dependencies import get_staff_user

    user = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)

    app = FastAPI()

    @app.get("/probe")
    async def probe(u: User = Depends(get_staff_user)):  # noqa: B008
        return {"role": u.user_role}

    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    from starlette.middleware.sessions import SessionMiddleware

    app.add_middleware(
        SessionMiddleware,
        secret_key=get_settings().secret_key,
        session_cookie="copi-session",
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/probe", headers=auth_headers(user.id))
    assert r.status_code == 403


@pytest.mark.parametrize("path,build", PI_ONLY_WRITES, ids=_IDS)
async def test_a_reviewer_is_refused_every_pi_write(client, db_session, path, build):
    """Clone of test_pi_only_writes.py::test_a_manager_is_refused_every_pi_write
    for a reviewer: all four PI_ONLY_WRITES 403, state unchanged."""
    rev = await factories.make_user(
        db_session, user_role=USER_ROLE_REVIEWER, onboarding_complete=True
    )
    await factories.make_profile(db_session, user=rev, research_summary="untouched")
    await db_session.flush()

    before = await _snapshot(db_session, rev.id)
    r = await client.post(path, data=build(rev), headers=auth_headers(rev.id))
    assert r.status_code == 403, f"{path} served a reviewer"
    assert await _snapshot(db_session, rev.id) == before, f"{path} acted for a reviewer"


async def test_a_reviewer_cannot_save_a_pi_profile(client, db_session):
    """Clone of test_pi_only_writes.py::test_a_manager_cannot_save_a_pi_profile
    for a reviewer: POST /profile/save is the fifth PI write."""
    rev = await factories.make_user(
        db_session, user_role=USER_ROLE_REVIEWER, name="Rhonda Reviewer"
    )
    original_email = rev.email
    await db_session.flush()

    form = {**_save_profile_form(rev), "email": "reviewer-rewrote-this@example.edu"}
    r = await client.post("/profile/save", data=form, headers=auth_headers(rev.id))
    assert r.status_code == 403, "POST /profile/save served a reviewer"

    assert await db_session.scalar(
        select(func.count(ResearcherProfile.id)).where(ResearcherProfile.user_id == rev.id)
    ) == 0, "a reviewer got a ResearcherProfile out of /profile/save"
    assert await db_session.scalar(
        select(User.email).where(User.id == rev.id)
    ) == original_email, "a reviewer rewrote their own email via /profile/save"

    # Control: the same request from a PI must still do all of that.
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI, name="Percy Pi Two")
    await db_session.flush()
    pi_form = {**_save_profile_form(pi), "email": "pi-rewrote-this-too@example.edu"}
    r2 = await client.post("/profile/save", data=pi_form, headers=auth_headers(pi.id))
    assert r2.status_code == 302, "POST /profile/save refused a PI"
    assert await db_session.scalar(
        select(func.count(ResearcherProfile.id)).where(ResearcherProfile.user_id == pi.id)
    ) == 1
    assert await db_session.scalar(
        select(User.email).where(User.id == pi.id)
    ) == "pi-rewrote-this-too@example.edu"


async def test_reviewer_visiting_onboarding_is_bounced_and_enqueues_no_job(
    client, db_session
):
    rev = await factories.make_user(
        db_session, user_role=USER_ROLE_REVIEWER, onboarding_complete=False
    )
    r = await client.get(
        "/onboarding", headers=auth_headers(rev.id), follow_redirects=False
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/manager/assessments"

    count = await db_session.scalar(
        select(func.count(Job.id)).where(
            Job.user_id == rev.id, Job.type == "generate_profile"
        )
    )
    assert count == 0


async def test_reviewer_profile_and_edit_pages_redirect(client, db_session):
    """The follow-the-full-chain-to-200 assertion belongs to Task 3, when the
    manager router actually admits reviewers — asserting 200 here cannot pass."""
    rev = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)

    r1 = await client.get(
        "/profile", headers=auth_headers(rev.id), follow_redirects=False
    )
    assert r1.status_code == 302
    assert r1.headers["location"] == "/manager/assessments"

    r2 = await client.get(
        "/profile/edit", headers=auth_headers(rev.id), follow_redirects=False
    )
    assert r2.status_code == 302
    assert r2.headers["location"] == "/manager/assessments"


# --- Task 3: the manager router's reviewer-visible read slice --------------

REVIEWER_MANAGER_EXPECTATIONS = {
    ("GET", "/manager"): 302,
    ("GET", "/manager/pis"): 200,
    ("GET", "/manager/pis/{user_id}"): 200,
    ("GET", "/manager/assessments"): 200,
    ("GET", "/manager/assessments/{assessment_id}"): 200,
    ("GET", "/manager/discussions"): 403,
    ("GET", "/manager/activity"): 403,
    ("GET", "/manager/activity/{run_id}"): 403,
    ("GET", "/manager/prompt-suggestions"): 403,
    ("GET", "/manager/prompt-suggestions/{suggestion_id}"): 403,
    ("POST", "/manager/pis"): 403,
    ("POST", "/manager/pis/{user_id}/profile"): 403,
    ("POST", "/manager/pis/{user_id}/mute"): 403,
    ("POST", "/manager/pis/{user_id}/unmute"): 403,
}

# What each write route needs in its POST body to get PAST FastAPI's own
# request validation and actually reach (and be refused by) the per-handler
# dependency — mirrors test_manager_pi_writes.py::test_pi_is_denied_all_four_write_routes,
# which supplies the same shapes for the same reason. `None` means "no data".
_REVIEWER_POST_BODIES = {
    "/manager/pis": {"orcid": "0000-0006-0000-0000"},
    "/manager/pis/{user_id}/profile": {},
    "/manager/pis/{user_id}/mute": None,
    "/manager/pis/{user_id}/unmute": None,
}


async def test_reviewer_manager_surface_is_exactly_the_read_slice(client, db_session):
    """Replaces the "gated by construction" claim the module docstring lost
    when the router grew a write allowlist and a three-tier read audience:
    enumerated from the LIVE router for BOTH methods (the
    test_manager_views.py::_manager_get_paths shape, extended to POST), so a
    manager route added later with no entry in the map above fails this test
    loudly instead of silently reaching — or silently refusing — a reviewer."""
    from src.routers import manager as manager_router

    rev = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    run = await factories.make_simulation_run(db_session)
    assessment = OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", channel_name="general",
    )
    db_session.add(assessment)
    await db_session.flush()

    param_values = {
        "user_id": str(pi.id),
        "run_id": str(run.id),
        "assessment_id": str(assessment.id),
        "suggestion_id": str(uuid.uuid4()),
    }

    live_routes = set()
    for route in manager_router.router.routes:
        methods = getattr(route, "methods", ())
        for method in ("GET", "POST"):
            if method in methods:
                live_routes.add((method, f"/manager{route.path}"))

    assert live_routes == set(REVIEWER_MANAGER_EXPECTATIONS), (
        "the manager router's live GET+POST routes no longer match "
        "REVIEWER_MANAGER_EXPECTATIONS -- update the map in this test"
    )

    for (method, template_path), expected in REVIEWER_MANAGER_EXPECTATIONS.items():
        path = template_path
        for name in _PARAM_RE.findall(path):
            path = path.replace("{" + name + "}", param_values[name])
        if method == "GET":
            r = await client.get(
                path, headers=auth_headers(rev.id), follow_redirects=False
            )
        else:
            data = _REVIEWER_POST_BODIES[template_path]
            kwargs = {"data": data} if data is not None else {}
            r = await client.post(
                path, headers=auth_headers(rev.id), follow_redirects=False, **kwargs
            )
        assert r.status_code == expected, (
            f"{method} {path} was {r.status_code}, expected {expected}"
        )


async def test_reviewer_nav_shows_review_link_and_nothing_else(client, db_session):
    rev = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    r = await client.get("/settings", headers=auth_headers(rev.id))
    assert r.status_code == 200
    assert 'href="/manager/assessments"' in r.text
    assert "My Profile" not in r.text
    assert "My Agent" not in r.text
    assert 'href="/admin/users"' not in r.text


async def test_reviewer_subnav_hides_staff_items(client, db_session):
    rev = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    body = (await client.get("/manager/pis", headers=auth_headers(rev.id))).text
    assert 'href="/manager/pis"' in body
    assert 'href="/manager/assessments"' in body
    assert "/manager/discussions" not in body
    assert "/manager/activity" not in body


async def test_reviewer_pi_detail_is_read_only(client, db_session):
    """Never assert on the bare word "Mute": pi_detail.html legitimately
    renders "Muted"/"mute" elsewhere (the inactive-agent timestamp line and
    the terminal-status caption) even with the write chain hidden."""
    rev = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    await factories.make_agent(db_session, user=pi, status="active")
    await factories.make_profile(db_session, user=pi, keywords=["gene-editing"])
    await set_tenure_start(pi.id, 2015, "manual", db=db_session)
    await db_session.flush()

    body = (await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(rev.id))).text
    assert 'action="/manager/pis/' not in body
    assert ">Mute<" not in body
    assert ">Unmute<" not in body
    assert "gene-editing" in body
    assert "2015" in body


async def test_manager_still_sees_the_edit_form(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    body = (await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(mgr.id))).text
    assert f'action="/manager/pis/{pi.id}/profile"' in body
    assert 'name="jhu_tenure_start"' in body


async def test_admin_impersonating_a_reviewer_sees_no_staff_forms(client, db_session):
    """get_review_user PASSES an impersonated reviewer (it gates on the
    effective session user, which get_current_user already swapped), so both
    pages 200 — the template identity trap is exactly why the gates on them
    use effective_user/impersonation_banner rather than current_user."""
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN, name="Adm Rev")
    rev = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER, name="Rev Imp")
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    await factories.make_agent(db_session, user=pi, status="active")
    headers = auth_headers(admin.id)
    headers["Cookie"] += f"; copi-impersonate={rev.id}"

    pis_body = (await client.get("/manager/pis", headers=headers)).text
    assert pis_body  # sanity: the page actually rendered (200, not a redirect body)
    assert 'action="/manager/pis"' not in pis_body

    detail_body = (await client.get(f"/manager/pis/{pi.id}", headers=headers)).text
    assert 'action="/manager/pis/' not in detail_body
    assert ">Mute<" not in detail_body
    assert ">Unmute<" not in detail_body


async def test_reviewer_is_denied_every_admin_route(client, db_session):
    """Clone of test_manager_views.py::test_manager_is_denied_every_admin_route
    for a reviewer."""
    from src.routers import admin as admin_router

    rev = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    checked = 0
    for route in admin_router.router.routes:
        if "GET" not in getattr(route, "methods", ()) or "{" in route.path:
            continue
        r = await client.get(
            f"/admin{route.path}", headers=auth_headers(rev.id), follow_redirects=False
        )
        assert r.status_code == 403, f"/admin{route.path} leaked to a reviewer"
        checked += 1
    assert checked >= 8, "the admin sweep matched too few routes to be meaningful"


async def test_reviewer_full_login_chain_terminates(client, db_session):
    """reviewer -> /profile -> /manager/assessments, with no loop. Task 2
    stopped at the 302 (test_reviewer_profile_and_edit_pages_redirect above);
    now that Task 3 admits a reviewer to /manager/assessments, the full chain
    is assertable. Mirrors
    test_manager_onboarding.py::test_manager_profile_url_bounce_terminates,
    including its cookie-jar seeding comment trick: httpx strips a
    manually-set Cookie header on every redirect hop and rebuilds "Cookie"
    from the client's cookie jar instead
    (httpx._client.Client._redirect_headers unconditionally pops it), so a
    header-only auth_headers() cookie silently disappears once
    follow_redirects=True actually follows the 302."""
    rev = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    cookie_value = auth_headers(rev.id)["Cookie"].split("=", 1)[1]
    client.cookies.set("copi-session", cookie_value)
    r = await client.get("/profile", follow_redirects=True)
    assert r.status_code == 200
    assert str(r.url).endswith("/manager/assessments")


async def test_reviewer_sees_the_review_columns_on_manager_assessments(
    client, db_session
):
    """Task 7: the Assigned/Reviewed-by columns and the approval-status chip
    are not staff-only — a reviewer reaches the same /manager/assessments
    page (Task 3) and must see the same names and chip a manager/admin does.
    Clone of test_assessment_queue_controls.py::test_list_pages_show_reviewer_columns,
    for the one role that page doesn't already parametrize over."""
    rev = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    run = await factories.make_simulation_run(db_session)
    await _seed_reviewed_row(db_session, run, project="Reviewer Role Co")

    html = (
        await client.get(
            f"/manager/assessments?run_id={run.id}", headers=auth_headers(rev.id)
        )
    ).text

    assert "Reviewer Role Co" in html
    row = _row_slice(html, "Reviewer Role Co")
    assert "Alice A" in row
    assert "Bob B, Cara C, Dana D" in row
    assert "Disapproved" in row
