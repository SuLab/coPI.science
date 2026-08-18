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
    count as reached; a 403 or 404 does not.

    A real SimulationRun backs the {run_id} slot: /manager/activity/{run_id}
    correctly 404s on a random UUID, and a 404 does not count as "reached" per
    the assertion above, so this sweep needs a run that actually exists rather
    than a syntactically-valid-but-absent one."""
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    run = await factories.make_simulation_run(db_session)
    paths = _manager_get_paths({"user_id": str(pi.id), "run_id": str(run.id)})
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


async def test_admin_impersonating_a_manager_has_a_way_back(client, db_session):
    """The hotfix: an admin impersonating a manager satisfies get_staff_user
    (is_staff is true for the impersonated manager) and reaches /manager/*,
    but the effective user's is_admin is false, so the nav's Admin link is
    hidden (base.html gates it on `not impersonation_banner` anyway). Without
    src/routers/manager.py::_template_context setting `impersonation_banner`,
    there was no banner and no Stop form anywhere on the page — the admin was
    stranded. Assert on the actual form action, not just the word
    "Impersonating", so a revert of the fix fails this test."""
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN, name="Adm One")
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER, name="Mgr Two")
    headers = auth_headers(admin.id)
    headers["Cookie"] += f"; copi-impersonate={mgr.id}"
    r = await client.get("/manager/pis", headers=headers)
    assert r.status_code == 200
    assert 'action="/admin/impersonate/stop"' in r.text
    assert "Mgr Two" in r.text  # banner names the impersonated user


async def test_admin_impersonating_another_admin_has_a_way_back(client, db_session):
    """Same omission in src/routers/admin.py::_template_context: two admins
    both satisfy get_admin_user, so impersonating one admin from another
    reaches /admin/* with no banner and no Stop form either."""
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN, name="Adm Real")
    other_admin = await factories.make_user(
        db_session, user_role=USER_ROLE_ADMIN, name="Adm Borrowed"
    )
    headers = auth_headers(admin.id)
    headers["Cookie"] += f"; copi-impersonate={other_admin.id}"
    r = await client.get("/admin/users", headers=headers)
    assert r.status_code == 200
    assert 'action="/admin/impersonate/stop"' in r.text
    assert "Adm Borrowed" in r.text  # banner names the impersonated user


async def test_admin_impersonating_a_manager_sees_the_manager_nav_link(client, db_session):
    """The regression this hotfix closes: templates/base.html gated the
    Manager nav link on ``current_user.is_staff and not impersonation_banner``.
    Before c6cca1e, /manager/* pages never set ``impersonation_banner``, so
    ``not impersonation_banner`` was vacuously true and the link rendered off
    the real admin's own is_staff. Once c6cca1e set the banner (to give the
    admin a Stop-impersonating button), that same clause flipped false and
    hid the nav link exactly while impersonating — the one time it's needed.
    The fix gates on the *effective* user's is_staff instead, with no
    impersonation-banner clause at all, so both the link and the Stop form
    must be present together here — that pairing is exactly what regressed."""
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN, name="Adm Nav")
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER, name="Mgr Nav")
    headers = auth_headers(admin.id)
    headers["Cookie"] += f"; copi-impersonate={mgr.id}"
    r = await client.get("/manager/pis", headers=headers)
    assert r.status_code == 200
    assert 'href="/manager"' in r.text
    assert 'action="/admin/impersonate/stop"' in r.text


async def test_admin_impersonating_a_pi_has_no_manager_nav_link(client, db_session):
    """The effective user while impersonating a PI is the PI, who cannot
    reach /manager (get_staff_user 403s) — so the nav link must not render;
    a visible link that 403s on click is worse than no link at all (F6). The
    PI's own onboarding gate makes most PI-only pages an unreliable place to
    check this (they may redirect), so this reads /settings, which every
    logged-in user — any role, impersonated or not — can always reach, and
    confirms the actual route still 403s so hiding the link is the right
    call, not an accidental omission."""
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN, name="Adm PI Nav")
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI, name="Impersonated PI")
    headers = auth_headers(admin.id)
    headers["Cookie"] += f"; copi-impersonate={pi.id}"

    r = await client.get("/settings", headers=headers)
    assert r.status_code == 200
    assert 'href="/manager"' not in r.text

    r2 = await client.get("/manager/pis", headers=headers, follow_redirects=False)
    assert r2.status_code == 403


async def test_a_plain_manager_sees_the_manager_nav_link(client, db_session):
    """Not impersonated at all: the effective-user rule must reduce to the
    real user's own is_staff, same as before this hotfix."""
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER, name="Plain Mgr Nav")
    r = await client.get("/manager/pis", headers=auth_headers(mgr.id))
    assert r.status_code == 200
    assert 'href="/manager"' in r.text


async def test_a_non_impersonating_manager_sees_no_banner(client, db_session):
    """The banner must not render unconditionally — only under real
    impersonation. Paired negative for the two positive tests above."""
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER, name="Plain Mgr")
    r = await client.get("/manager/pis", headers=auth_headers(mgr.id))
    assert r.status_code == 200
    assert 'action="/admin/impersonate/stop"' not in r.text


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


async def test_manager_assessments_renders_the_incomplete_panel_marker(client, db_session):
    """``incomplete_panel_count`` and ``a.panel_incomplete``/``a.missing_domains``
    reach ``/manager/assessments`` only via ``manager_assessments``'s ``**view``
    splat (``src/routers/manager.py``) — there is no per-key allowlist the way
    ``admin.py`` has one. Jinja's ``Undefined`` is falsy in ``{% if %}`` and
    never raises, so if a future change stopped forwarding that key (or
    dropped it from ``list_assessments``'s return dict), the banner and the
    per-row marker would both silently stop rendering, the page would still
    200 with a clean-looking table, and every other manager assessments test
    would keep passing — exactly the silent regression managers, the
    read-only audience this warning exists for, would never see. This seeds a
    ``panel_incomplete=True`` row and asserts on both the banner text and the
    per-row marker, neither of which appears anywhere else on the page.
    """
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    run = SimulationRun()
    db_session.add(run)
    await db_session.flush()
    db_session.add(
        OpportunityAssessment(
            simulation_run_id=run.id,
            agent_id="blackbird",
            subject_agent_id="gordy",
            channel_name="general",
            company_or_project="Gapped Panel Fixture Co",
            recommendation="conditional",
            panel_incomplete=True,
            missing_domains=["chemistry"],
        )
    )
    await db_session.flush()

    resp = await client.get("/manager/assessments", headers=auth_headers(mgr.id))
    assert resp.status_code == 200
    html = resp.text

    assert "stored with an incomplete specialist panel" in html
    assert "Gapped Panel Fixture Co" in html
    assert "panel</span>" in html
    assert "Missing: chemistry" in html


async def test_manager_can_read_discussions_and_activity(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    for path in ("/manager/discussions", "/manager/activity"):
        r = await client.get(path, headers=auth_headers(mgr.id))
        assert r.status_code == 200, path


async def test_manager_has_no_llm_calls_route(client, db_session):
    """D10: those rows carry full system prompts and raw model output."""
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    run = uuid.uuid4()
    r = await client.get(
        f"/manager/activity/{run}/llm-calls",
        headers=auth_headers(mgr.id),
        follow_redirects=False,
    )
    assert r.status_code == 404
    r2 = await client.get(
        f"/admin/activity/{run}/llm-calls",
        headers=auth_headers(mgr.id),
        follow_redirects=False,
    )
    assert r2.status_code == 403


async def test_manager_activity_detail_404s_on_an_unknown_run(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    r = await client.get(
        f"/manager/activity/{uuid.uuid4()}", headers=auth_headers(mgr.id)
    )
    assert r.status_code == 404


async def test_manager_activity_detail_renders_the_populated_shared_partial(
    client, db_session
):
    """Every table in ``templates/admin/_run_detail_body.html`` sits behind an
    ``{% if %}`` guard (agent_stats / channel_stats / channels / messages), and
    the only other manager request that reaches it — the route sweep in
    ``test_staff_can_reach_every_manager_route`` — seeds a bare SimulationRun
    and asserts nothing about the body. So a manager route that dropped a key
    from its ``**view`` splat, or a wrapper that lost the ``{% include %}``
    entirely, would still 200 with an empty page and every other test here
    would keep passing. This is the third instance of that trap on this branch
    (the assessments and discussions partials were the first two).

    Seeds one message and one channel so all four guards open, then asserts on
    markup that can ONLY come from the partial's populated path:

    * the channel name, printed by both the Channels Created and Messages by
      Channel rows. It is a fixture-unique literal, and the wrapper (which
      prints only the run's date and status) has no channel names at all.
    * ``PartialbotBot``, i.e. the partial's own ``{{ msg.agent_id }}Bot``
      concatenation, not the bare agent_id — that suffix exists nowhere in the
      wrapper.
    * the ``Message Timeline (1)`` heading, whose count comes from
      ``messages | length``: it distinguishes "the timeline block rendered with
      a row in it" from "the block rendered empty", which a bare "Message
      Timeline" substring could not.
    * the summary card values, which prove the ``run`` object itself carried
      through the splat.

    Each assertion was confirmed to fail without the data: rerunning with the
    message/channel seeding removed drops all four (the guards close, the page
    renders as the three summary cards alone), and deleting the
    ``{% include %}`` from ``manager/activity_detail.html`` drops all four as
    well while the wrapper's own heading assertions keep passing.
    """
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    run = await factories.make_simulation_run(db_session, total_messages=1, total_api_calls=7)
    await factories.make_agent_channel(
        db_session,
        run=run,
        channel_name="partial-proof-channel",
        channel_type="thematic",
        created_by_agent="partialbot",
    )
    await factories.make_agent_message(
        db_session,
        run=run,
        agent_id="partialbot",
        channel_name="partial-proof-channel",
        message_length=140,
        phase="new_post",
    )
    await db_session.flush()

    r = await client.get(f"/manager/activity/{run.id}", headers=auth_headers(mgr.id))
    assert r.status_code == 200
    body = r.text

    # The wrapper rendered (control for the four partial assertions below).
    assert "Simulation Run" in body
    assert "/manager/activity" in body

    # The partial's populated path.
    assert "partial-proof-channel" in body
    assert "PartialbotBot" in body
    assert "Message Timeline (1)" in body
    assert "Messages by Agent" in body
    assert "Channels Created" in body
    assert "140 chars" in body

    # Still no admin-only drill-down anywhere on the page (D10).
    assert "/admin/" not in body
    assert "llm-calls" not in body


async def test_manager_discussions_renders_a_real_thread_with_no_export_control(
    client, db_session
):
    """With no ``SimulationRun`` at all, ``build_discussions_view`` returns
    ``selected_run_id=None`` and ``manager/discussions.html`` renders only its
    empty-state branch — the filter form that carries the admin export
    buttons is never emitted regardless of what the template contains. An
    earlier version of this test asserted export's absence against exactly
    that empty page, so it would have passed even with the export buttons
    copied in verbatim. This seeds a run, a root post and a decision with
    ``summary_text`` so a real thread renders through
    ``templates/admin/_discussions_threads.html``, then checks three things
    that can only be verified once a thread is actually on the page:

    * the partial rendered a real row (scoped to the agent name it prints,
      not the wrapper's static chrome) rather than the "No discussions
      found" empty-table placeholder.
    * R4: the sanitizing markdown renderer (``/static/js/markdown.js``,
      loaded from ``{% block extra_head %}``) and the ``data-markdown``
      attribute it renders both survived the extract-partial/wrapper split —
      dropping ``extra_head`` would leave ``data-markdown`` with nothing to
      render it, silently, with no error.
    * export now means something: the filter form (where the admin
      template's two export buttons live) is actually being rendered on
      this response, so its absence is no longer vacuous.
    """
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    run = await factories.make_simulation_run(db_session)
    await factories.make_agent_message(
        db_session,
        run=run,
        agent_id="gill",
        is_bot=True,
        message_ts="1786000000.000100",
        thread_ts=None,
        phase="new_post",
        channel_name="general",
    )
    await factories.make_thread_decision(
        db_session,
        run=run,
        thread_id="1786000000.000100",
        channel="general",
        agent_a="gill",
        agent_b="wang",
        outcome="proposal",
        summary_text="A **markdown** summary of the proposal.",
    )
    await db_session.flush()

    body = (await client.get("/manager/discussions", headers=auth_headers(mgr.id))).text

    # The partial rendered a real row, not the empty-state placeholder. The
    # rendered `data-markdown` attribute's *value* is asserted, not just its
    # presence as an attribute name, and not "GillBot" — the wrapper's own
    # agent_filter <option> also prints "GillBot" ({{ a | capitalize }}Bot in
    # the filter form), so that string is not scoped to the partial and would
    # false-pass even if the include were deleted (confirmed by temporarily
    # deleting {% include "admin/_discussions_threads.html" %} and rerunning
    # this test: "GillBot" alone kept passing, the exact attribute value did
    # not). This fixture's summary_text is unique to this test, so it can
    # only appear here via the partial's `data-markdown="{{
    # t.decision.summary_text | e }}"` div.
    assert 'data-markdown="A **markdown** summary of the proposal."' in body
    assert "No discussions found" not in body

    # R4: renderer + the attribute it renders both present. The literal
    # <script src="..."> tag is asserted rather than a bare substring check,
    # because {% block scripts %} carries an explanatory HTML *comment* that
    # also mentions "/static/js/markdown.js" — a bare substring check would
    # false-pass off that comment alone even with extra_head (and the actual
    # <script> tag) missing entirely.
    assert '<script src="/static/js/markdown.js">' in body

    # Export stays admin-only. Both forms: the loose lower-cased substring
    # check is the intent ("no mention of export anywhere"), and the precise
    # ``name="export"`` check is the actual invariant that would survive
    # unrelated copy changes to the page (e.g. a future "export" word in
    # prose would trip the loose check without indicating a real
    # regression).
    assert "export" not in body.lower()
    assert 'name="export"' not in body
    assert "/admin/" not in body
