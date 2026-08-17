"""The /manager surface: deny-by-default, read-only, and PI-scoped."""

import re
import uuid
from datetime import UTC, datetime

import pytest

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    OpportunityAssessment,
    SimulationRun,
)
from src.routers import manager as manager_router
from tests import factories
from tests.integration.test_manager_access import auth_headers
from tests.integration.test_opportunity_assessment_persistence import (
    _band_label,
    _gating_state_for,
    _score_cell,
)

pytestmark = pytest.mark.integration

_PARAM_RE = re.compile(r"\{(\w+)\}")


def _manager_get_paths(param_values: dict[str, str] | None = None) -> list[str]:
    """Full ``/manager``-prefixed path for every GET route on the *live*
    router, with each ``{param}`` slot filled from ``param_values`` (a fresh
    UUID for anything not supplied — syntactically valid, even if it 404s).

    Enumerated from ``manager_router.router.routes`` rather than hand-listed,
    so a route added later — Tasks 5/6 add four more — is picked up by both
    sweeps below automatically instead of silently skipped. That enumeration
    is what keeps deny-by-default honest instead of aspirational: a hand-list
    only ever proves the routes someone remembered to add to it.
    """
    param_values = param_values or {}
    paths = []
    for route in manager_router.router.routes:
        if "GET" not in getattr(route, "methods", ()):
            continue
        path = f"/manager{route.path}"
        for name in _PARAM_RE.findall(path):
            path = path.replace("{" + name + "}", param_values.get(name, str(uuid.uuid4())))
        paths.append(path)
    return sorted(paths)


def test_manager_router_exposes_no_mutating_routes():
    """D12. If there is no mutation route there is no mutation risk, and this
    turns that from a promise into a check."""
    methods = {m for r in manager_router.router.routes for m in getattr(r, "methods", ())}
    assert methods == {"GET"}, f"non-GET route on the manager router: {methods}"


async def test_unauthenticated_manager_root_redirects_to_login(client):
    r = await client.get("/manager", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers["location"]


async def test_pi_is_denied_the_manager_surface(client, db_session):
    """Enumerates every current GET route on the live router (not a
    hand-list), so a route added later without a matching test entry still
    gets exercised here — see ``_manager_get_paths``."""
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    paths = _manager_get_paths({"user_id": str(pi.id)})
    assert len(paths) >= 3, "the enumeration matched too few routes to be meaningful"
    for path in paths:
        r = await client.get(path, headers=auth_headers(pi.id), follow_redirects=False)
        assert r.status_code == 403, f"{path} was reachable by a PI"


async def test_staff_can_reach_every_manager_route(client, db_session):
    """Paired with the denial sweep above: the same live-router enumeration
    also proves staff are NOT accidentally locked out of a route added later.
    A 200 (rendered page) or a 302 (the root's redirect to /manager/pis) both
    count as reached; a 403 or 404 does not."""
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    paths = _manager_get_paths({"user_id": str(pi.id)})
    for path in paths:
        r = await client.get(path, headers=auth_headers(mgr.id), follow_redirects=False)
        assert r.status_code in (200, 302), f"{path} was unreachable by a manager: {r.status_code}"


@pytest.mark.parametrize("role", [USER_ROLE_MANAGER, USER_ROLE_ADMIN])
async def test_staff_can_read_the_pi_directory(client, db_session, role):
    staff = await factories.make_user(db_session, user_role=role)
    await factories.make_user(db_session, user_role=USER_ROLE_PI, name="Dr Target")
    r = await client.get("/manager/pis", headers=auth_headers(staff.id))
    assert r.status_code == 200
    assert "Dr Target" in r.text


async def test_manager_root_redirects_to_the_pi_directory(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    r = await client.get("/manager", headers=auth_headers(mgr.id), follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/manager/pis"


async def test_directory_excludes_staff_accounts(client, db_session):
    """Ruling A: `templates/base.html` renders `{{ current_user.name }}`
    unconditionally in the nav, so the *viewing* manager's own name is always
    present in the page — asserting it is absent can never pass. Link-based
    assertions test the query (which staff rows are excluded from the
    directory) rather than the nav."""
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER, name="Mgr Self")
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN, name="Sneaky Admin")
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI, name="Real PI")
    r = await client.get("/manager/pis", headers=auth_headers(mgr.id))
    assert "Real PI" in r.text
    assert f"/manager/pis/{pi.id}" in r.text
    assert f"/manager/pis/{admin.id}" not in r.text
    assert "Sneaky Admin" not in r.text


async def test_pi_directory_has_no_admin_controls(client, db_session):
    """F6, applied to pis.html rather than pi_detail.html: this is the
    template actually derived from templates/admin/users.html, which is the
    one that carries the impersonation widget. The reachability gate cannot
    catch a regression here — /admin/impersonate is a real route, so a stray
    link to it would resolve fine — so this inspects the rendered body
    directly instead."""
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    await factories.make_user(db_session, user_role=USER_ROLE_PI)
    body = (await client.get("/manager/pis", headers=auth_headers(mgr.id))).text
    assert "impersonate" not in body.lower()
    assert "/admin/" not in body
    assert "/delete" not in body


async def test_pi_directory_renders_a_complete_profile_row(client, db_session):
    """No other test in this file creates a claimed PI with a profile, so
    pis.html's profile-status/profile-version columns only ever exercised
    their empty branches. This exercises the populated path.

    The name deliberately avoids the substring "Complete" (unlike, say, "Dr
    Complete") because the status-filter dropdown always renders a "Complete"
    `<option>` regardless of data — a name collision would let this test pass
    even if the status badge itself never rendered. Requiring a *second*
    occurrence of "Complete" is what actually proves the populated badge, not
    just the dropdown, rendered.
    """
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(
        db_session,
        user_role=USER_ROLE_PI,
        name="Dr Populated",
        claimed_at=datetime.now(UTC),
    )
    await factories.make_profile(db_session, user=pi)
    r = await client.get("/manager/pis", headers=auth_headers(mgr.id))
    assert r.status_code == 200
    assert "Dr Populated" in r.text
    assert r.text.count("Complete") >= 2, "expected the dropdown option AND this row's badge"


async def test_pi_detail_is_readable(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(
        db_session, user_role=USER_ROLE_PI, name="Dr Detail", email="pi@example.edu"
    )
    r = await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(mgr.id))
    assert r.status_code == 200
    assert "Dr Detail" in r.text
    assert "pi@example.edu" in r.text  # D3: contact info is in scope


async def test_pi_detail_404s_for_a_staff_account(client, db_session):
    """Closes UUID enumeration of admin/manager records."""
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    r = await client.get(f"/manager/pis/{admin.id}", headers=auth_headers(mgr.id))
    assert r.status_code == 404


async def test_pi_detail_has_no_delete_or_impersonate_control(client, db_session):
    """F6: the admin templates carry both; the manager templates must not."""
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    body = (await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(mgr.id))).text
    assert "/delete" not in body
    assert "impersonate" not in body.lower()
    assert "Danger Zone" not in body


async def test_manager_is_denied_every_admin_route(client, db_session):
    from src.routers import admin as admin_router

    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    checked = 0
    for route in admin_router.router.routes:
        if "GET" not in getattr(route, "methods", ()) or "{" in route.path:
            continue
        r = await client.get(
            f"/admin{route.path}", headers=auth_headers(mgr.id), follow_redirects=False
        )
        assert r.status_code == 403, f"/admin{route.path} leaked to a manager"
        checked += 1
    assert checked >= 8, "the admin sweep matched too few routes to be meaningful"


async def test_manager_cannot_impersonate(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    r = await client.post(
        "/admin/impersonate", data={"orcid": pi.orcid}, headers=auth_headers(mgr.id)
    )
    assert r.status_code == 403


async def test_a_hand_set_impersonate_cookie_is_ignored_for_a_manager(client, db_session):
    """F7: the cookie is unsigned and client-supplied. get_current_user honours
    it only for is_admin, which a manager never satisfies."""
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER, name="Mgr")
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN, name="TheAdmin")
    headers = auth_headers(mgr.id)
    headers["Cookie"] += f"; copi-impersonate={admin.id}"
    r = await client.get("/manager/pis", headers=headers, follow_redirects=False)
    assert r.status_code == 200          # still the manager, not the admin
    r2 = await client.get("/admin/users", headers=headers, follow_redirects=False)
    assert r2.status_code == 403         # did NOT become an admin


async def test_manager_can_read_assessments(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    r = await client.get("/manager/assessments", headers=auth_headers(mgr.id))
    assert r.status_code == 200
    assert "Opportunity Assessments" in r.text


async def test_pi_is_denied_assessments(client, db_session):
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    r = await client.get(
        "/manager/assessments", headers=auth_headers(pi.id), follow_redirects=False
    )
    assert r.status_code == 403


async def test_manager_assessments_never_links_into_admin(client, db_session):
    """A live-looking control that 403s on click is worse than no control (F6)."""
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    body = (await client.get("/manager/assessments", headers=auth_headers(mgr.id))).text
    assert "/admin/" not in body


async def test_manager_assessments_renders_a_populated_verdict_row(client, db_session):
    """The three tests above never populate ``opportunity_assessments``, so they
    only prove the static wrapper (title, empty-state) renders through
    ``/manager``. The verdict table itself — the whole point of the shared
    partial — sits behind ``{% if assessments %}``
    (``templates/admin/_assessments_body.html``) and is the only consumer of
    ``rubric_weights``/``runs_by_id``; a manager route that dropped either key
    from its ``**view`` splat would still 200 with a clean empty state and
    every other test here would keep passing. This drives one row through the
    real table markup and asserts on content that can only come from it:

    * the project name — a literal fixture string, present nowhere else on
      the page.
    * the band label — scoped to the dedicated ``band-label`` span via
      ``_band_label``, not a bare ``"advance" in body`` check: the intro
      prose already contains the lowercase words "advance"/"conditional"/
      "pass" while explaining the band thresholds, so a substring check
      would false-pass even against the empty-state page.
    * two distinct gating states (``met`` and ``unconfirmed``) via
      ``_gating_state_for``, scoped to the per-row ``gating-<state>`` class —
      not a bare substring check, because the gating legend paragraph above
      the table already prints the words "met" and "unconfirmed" while
      explaining the glyphs, on every page render including the empty state.
    * a scored dimension's label/value/weight and the omitted dimension's
      em-dash placeholder, via ``_score_cell``, scoped to the per-row
      ``score-<key>`` class — this class only exists inside a detail row, so
      it cannot be satisfied by the tooltip text or intro prose either.

    All three helpers are imported from
    ``test_opportunity_assessment_persistence`` (the suite that already
    proves this same markup renders correctly on ``/admin/assessments``)
    rather than reimplemented, so a future template change that broke one of
    these scoped matchers would show up as a failure in both suites, not a
    silently-diverged regex in this one.
    """
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    run = SimulationRun()
    db_session.add(run)
    await db_session.flush()
    db_session.add(
        OpportunityAssessment(
            simulation_run_id=run.id,
            agent_id="blackbird",
            subject_agent_id="wang",
            channel_name="general",
            company_or_project="Manager View Fixture Co",
            recommendation="advance",
            weighted_score=4.20,
            band="advance",
            gating={
                "life_sciences_domain": "met",
                "fto_achievable": "unconfirmed",
            },
            scores={
                "differentiation": 4,
                "market_unmet_need": 4,
                "team": 4,
                "ip_fto": 2,
                "platform": 3,
                "dev_regulatory_feasibility": 3,
                "workplan_capital_efficiency": 3,
                "exit_thesis": 2,
                "mechanism_validation": 4,
                "toxicity_selectivity": 3,
                "experimental_rigor": 4,
                "chemistry_dc_path": 2,
                # external_signals deliberately omitted: must render as a
                # gap ("—"), not indistinguishable from a scored 0.
            },
        )
    )
    await db_session.flush()

    resp = await client.get("/manager/assessments", headers=auth_headers(mgr.id))
    assert resp.status_code == 200
    html = resp.text

    assert "Manager View Fixture Co" in html
    assert _band_label(html) == "advance"
    assert _gating_state_for(html, "life sciences domain") == "met"
    assert _gating_state_for(html, "fto achievable") == "unconfirmed"
    assert _score_cell(html, "differentiation") == "differentiation 4 /15%"
    assert _score_cell(html, "external_signals") == "external signals — /8%"
