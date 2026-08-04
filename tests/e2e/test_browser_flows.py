"""Task 12 — browser flows, as repeatable scripts.

Two layers, because the two things worth recording are different:

1. **``FLOWS``** — a machine-readable transcript of every flow: what to open,
   what to click, and what must be visible. This is the part a human (or an
   MCP-driven browser agent) replays. Playwright-over-MCP is interactive, so the
   plan (.notes/full-system-test-plan.md, Task 12) asks for scripts rather than
   pytest tests; ``FLOWS`` is that script, and ``test_every_flow_is_well_formed``
   keeps it honest.

2. **HTTP replays** — for every flow whose steps are ordinary form posts, a
   pytest test drives the *same* route sequence against the running server with
   ``httpx`` and asserts the same visible strings. Those have teeth without a
   browser and are what you run in anger. They are skipped unless
   ``E2E_BASE_URL`` is set, because they need a live server on a migrated
   database — see ``tests/e2e/README.md`` for the two-command setup.

Authentication is a forged session cookie (``tests/e2e/session.py``), not ORCID
login. ORCID login is *broken in this deployment* and the root cause is in
``README.md``; holding the admin and agent surfaces hostage to it would test
ORCID rather than us.

What cannot be automated at all, and why, is in ``HUMAN_ONLY``.
"""

import os
import re
import uuid

import httpx
import pytest

from tests.e2e.session import COOKIE_NAME, forge_session_cookie

BASE_URL = os.environ.get("E2E_BASE_URL", "")
# A second instance of the same app on the same database with
# COHORT_ISOLATION_ENABLED=true (and optionally COHORT_DEFAULT_POLICY=isolated).
# Needed for the banner control: cohort settings are read once per process, so
# proving the banner tracks them requires a second process, not a second request.
ISOLATION_URL = os.environ.get("E2E_ISOLATION_BASE_URL", "")

requires_server = pytest.mark.skipif(
    not BASE_URL,
    reason="needs E2E_BASE_URL pointing at a running app on a migrated database",
)
requires_isolation_server = pytest.mark.skipif(
    not ISOLATION_URL,
    reason="needs E2E_ISOLATION_BASE_URL (same app, COHORT_ISOLATION_ENABLED=true)",
)


# ---------------------------------------------------------------------------
# The scripts
# ---------------------------------------------------------------------------

#: Each flow: ``steps`` are (action, target, note); ``expect`` are substrings
#: that MUST appear in the rendered page at the end of the flow.
FLOWS: dict[str, dict] = {
    "admin_cohort_and_topology": {
        "as": "admin",
        "human_needed": False,
        "steps": [
            ("open", "/admin/cohorts", "banner must state the LIVE setting"),
            ("click", "New Cohort", "reveals the inline create form"),
            ("fill", "name=t12-browser-flow", "lowercase/hyphen only, max 48"),
            ("fill", "description=Created by the Task 12 browser flow", ""),
            ("click", "Create Cohort", "302s to /admin/cohorts/{id}"),
            ("open", "/admin/cohorts/topology", "agent x cohort matrix"),
            ("check", "cell SuBot x t12-browser-flow", ""),
            ("check", "cell WisemanBot x t12-browser-flow", ""),
            ("click", "Save topology", "302s with ?notice=2+added,+0+removed"),
        ],
        "expect": [
            "2 added, 0 removed",
            "Cohort isolation is OFF",
            "everyone (gate off for this agent)",
        ],
        "control": (
            "Repeat the last open against a process started with "
            "COHORT_ISOLATION_ENABLED=true: the banner must read 'Cohort "
            "isolation is ACTIVE' and SuBot's 'Acts on' cell must stop saying "
            "'gate off'. Without this half, the banner could be static text."
        ),
    },
    "agent_self_service_signup": {
        "as": "signup",
        "human_needed": False,
        "steps": [
            ("open", "/agent", "no agent yet -> request page"),
            ("click", "Request Agent", "POST /agent/request"),
        ],
        "expect": [
            "Agent Request Pending",
            "ProbesmithBot",
        ],
        "control": (
            "'Quinn Probesmith' has no last-name collision, so the agent_id is "
            "unprefixed 'probesmith'. The collision half of the rule (wu -> "
            "pwu) is a unit-level concern; assert it in "
            "tests/integration/test_agent_page.py, not here."
        ),
    },
    "public_graph": {
        "as": None,  # unauthenticated on purpose: these routes take no auth
        "human_needed": False,
        "steps": [
            ("open", "/scripps-graph", "D3 force layout over real rows"),
            ("click", "an edge", "opens the proposal modal"),
        ],
        "expect": [
            "Scripps Research collaboration network",
            "5 Scripps PIs",
            "4 collaborating pairs",
            "4 joint proposals",
        ],
        "control": (
            "The counts come from tests.e2e.seed's 5 agents / 4 edges. A route "
            "that rendered an empty graph would still return 200, so the "
            "assertion is on the counts, not on the status code."
        ),
    },
    "onboarding": {
        "as": "onboarding",
        "human_needed": False,
        "stops_at": (
            "Step 3 of 4, 'Building Your Profile'. /onboarding auto-enqueues a "
            "generate_profile job and the template shows that spinner for "
            "job_status in (none, pending, processing). Completing the step "
            "needs the worker to run run_profile_pipeline, which fetches the "
            "user's ORCID record — so with no usable ORCID credentials it can "
            "never finish. The steps below therefore substitute the profile row "
            "the pipeline would have written; the pipeline itself is Task 4's "
            "subject, not this one."
        ),
        "steps": [
            ("open", "/onboarding", "Step 3 of 4 spinner, job enqueued"),
            ("substitute", "ResearcherProfile + jobs.status='completed'",
             "stands in for the ORCID-fed pipeline"),
            ("open", "/onboarding", "now renders the editable review form"),
            ("click", "Save & Continue", "POST /onboarding/save-profile"),
            # The button lives in private_profile.html and always posted here;
            # this note said POST /onboarding/complete, which was wrong even
            # before that duplicate route was deleted for setting
            # onboarding_complete with no validation.
            ("click", "Save & Complete Onboarding", "POST /onboarding/private-profile"),
        ],
        "expect": [
            "onboarding_complete=1",
        ],
        "control": (
            "Assert users.onboarding_complete was FALSE at /onboarding and TRUE "
            "only after the final POST. Otherwise a route that set the flag on "
            "first view would pass."
        ),
    },
    "slack_provisioning": {
        "as": "admin",
        "human_needed": True,
        "steps": [
            ("arm", "SLACK_CONFIG_REFRESH_TOKEN on the app process",
             "single-use; the first click spends it"),
            ("open", "/admin/agents/{probe_agent_row_id}", ""),
            ("click", "Provision",
             "POST .../slack/provision -> apps.manifest.create -> 302 to Slack"),
            ("human", "Allow, on Slack's install screen",
             "the automation browser has no Slack session"),
            ("land", "/admin/agents/{id}?slack_ok=1",
             "Slack redirects to BASE_URL/admin/agents/slack/callback"),
            ("verify", "auth.test on the resulting xoxb- token",
             "read back from Slack, never from our own column"),
        ],
        "expect": [
            "Slack bot provisioned",
        ],
        "control": (
            "A set slack_bot_token column proves only that we wrote a column. "
            "The assertion is auth.test returning ok=true with the expected "
            "team_id and a bot_id — i.e. Slack agrees the app exists and is "
            "installed."
        ),
    },
}

#: Flows that cannot run headless, with the reason. Kept as data so
#: ``tests/e2e/README.md`` and this module cannot drift apart.
HUMAN_ONLY = {
    "slack_provisioning": (
        "Slack's OAuth consent screen requires a browser with a Slack session. "
        "The Playwright/MCP browser has none and there is no API to obtain one "
        "(Slack has no headless install grant). Everything either side of the "
        "Allow click is automated: the Provision POST is driven by the test, "
        "and the callback is the app's own route."
    ),
    "orcid_login": (
        "Not in FLOWS at all: it cannot be driven end to end from any browser "
        "in this deployment. ORCID rejects the configured client_id (HTTP 400 "
        "invalid_request / 'Invalid parameter: client_id'), so there is no "
        "consent screen to click. See README.md."
    ),
}


# ---------------------------------------------------------------------------
# Script well-formedness (runs offline — this is the part that stops FLOWS
# from rotting into prose)
# ---------------------------------------------------------------------------

_ACTIONS = {
    "open", "click", "fill", "check", "human", "land", "verify", "arm",
    "substitute",
}


def test_every_flow_is_well_formed():
    assert FLOWS, "no flows recorded"
    for name, flow in FLOWS.items():
        assert flow["steps"], f"{name}: no steps"
        assert flow["expect"], f"{name}: nothing asserted visible"
        assert flow.get("control"), f"{name}: no control stated"
        assert "human_needed" in flow, f"{name}: does it need a human?"
        for action, target, _note in flow["steps"]:
            assert action in _ACTIONS, f"{name}: unknown action {action!r}"
            assert target, f"{name}: empty target for {action!r}"


def test_human_only_matches_the_flows():
    """Every human_needed flow is explained in HUMAN_ONLY, and vice versa.

    Control: HUMAN_ONLY also carries ``orcid_login``, which is deliberately not
    a flow — so this asserts a subset relation in one direction only, and the
    ``needs`` check below is what actually has teeth.
    """
    needs = {n for n, f in FLOWS.items() if f["human_needed"]}
    assert needs == {"slack_provisioning"}, needs
    assert needs <= set(HUMAN_ONLY), needs - set(HUMAN_ONLY)


# ---------------------------------------------------------------------------
# HTTP replays against a live server
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def admin_id() -> str:
    """Admin user id. Provided by the harness, since the forged cookie needs it
    before any authenticated request can be made."""
    value = os.environ.get("E2E_ADMIN_USER_ID", "")
    if not value:
        pytest.skip("needs E2E_ADMIN_USER_ID (printed by python -m tests.e2e.seed)")
    uuid.UUID(value)  # fail loudly on a malformed id rather than at the route
    return value


@pytest.fixture
def client():
    """An anonymous client, FRESH per test.

    Deliberately not module-scoped: ``SessionMiddleware`` re-issues
    ``copi-session`` on every response that carries a session, so a shared
    client accumulates one identity's cookie and the next test silently runs as
    that identity.
    """
    with httpx.Client(base_url=BASE_URL, follow_redirects=True, timeout=30) as c:
        yield c


@pytest.fixture
def as_user():
    """``as_user(user_id) -> httpx.Client`` authenticated as that user.

    The forged cookie goes into the **cookie jar**, not into a per-request
    ``Cookie`` header. That distinction is load-bearing and cost two red tests:

    * the app re-signs and re-sets ``copi-session`` on every response, so the
      jar fills up during the flow;
    * httpx then sends the jar cookie *in addition to* an explicit ``Cookie``
      header, producing two ``Cookie`` header values. Starlette reads
      ``headers.get("cookie")``, which joins them with ", ", and the resulting
      ``copi-session=A, copi-session=B`` fails to parse — so the request arrives
      **unauthenticated**.
    * that only bites on a redirect-follow (302 -> GET), i.e. exactly on the
      form posts, which is why the GETs looked fine.

    Using the jar is also what a browser actually does.
    """
    created: list[httpx.Client] = []

    def _make(user_id: str) -> httpx.Client:
        c = httpx.Client(base_url=BASE_URL, follow_redirects=True, timeout=30)
        c.cookies.set(COOKIE_NAME, forge_session_cookie(user_id))
        created.append(c)
        return c

    yield _make
    for c in created:
        c.close()


@requires_server
def test_public_graph_renders_with_real_data(client):
    """FLOWS['public_graph'].

    Rule L3-style attribution: each assertion says which failure it saw.
    """
    r = client.get("/scripps-graph")
    assert r.status_code == 200, f"/scripps-graph did not render: {r.status_code}"
    for want in FLOWS["public_graph"]["expect"]:
        assert want in r.text, (
            f"{want!r} missing from /scripps-graph — either the seed is absent "
            "(run python -m tests.e2e.seed) or the graph query stopped matching "
            "the seeded rows"
        )
    # Control: an empty graph would also be a 200, so assert the payload has
    # nodes AND that a proposal summary reached the page (the modal's content).
    assert '"nodes": [{' in r.text, "graph payload has no nodes"
    assert "propose a joint study" in r.text, (
        "no proposal summary in the payload: thread_decisions did not join to "
        "the in-window new_post messages"
    )


@requires_server
def test_admin_cohort_create_and_topology_edit(as_user, admin_id):
    """FLOWS['admin_cohort_and_topology'] — create, then edit the matrix.

    Idempotent: the cohort name is reused, and a second run sees
    "already exists" and still exercises the topology save.
    """
    c = as_user(admin_id)

    r = c.get("/admin/cohorts")
    assert r.status_code == 200, "admin cohort list is not reachable"
    assert "Cohort isolation is" in r.text, "the gate banner is missing entirely"

    c.post(
        "/admin/cohorts/create",
        data={"name": "t12-browser-flow", "description": "Task 12 browser flow"},
    )

    r = c.get("/admin/cohorts/topology")
    assert r.status_code == 200, "topology matrix is not reachable"
    cells = re.findall(r'name="present" value="([0-9a-f-]{36}:[a-z0-9]+)"', r.text)
    assert cells, "the matrix rendered no cells — no cohorts or no agents seeded"
    wanted = [x for x in cells if x.endswith((":su", ":wiseman"))]
    assert len(wanted) == 2, f"expected su and wiseman cells, got {wanted}"

    # httpx wants a dict-of-lists for repeated form keys; a list of 2-tuples is
    # sent as raw content and h11 rejects it.
    r = c.post("/admin/cohorts/topology", data={"present": cells, "cell": wanted})
    assert r.status_code == 200
    assert "added," in r.text and "removed" in r.text, (
        "the save did not report a diff — either the form shape changed or the "
        f"redirect landed somewhere else: {r.url}"
    )
    # Both halves: the memberships are now reflected back in the rendered form.
    # `checked` is several attributes after `value` in the template, so match the
    # whole <input> element rather than assuming attribute order.
    r = c.get("/admin/cohorts/topology")
    checked = [
        m.group(1)
        for m in re.finditer(
            r'name="cell" value="([0-9a-f-]{36}:[a-z0-9]+)"([^>]*)>', r.text
        )
        if "checked" in m.group(2)
    ]
    assert set(checked) == set(wanted), (
        f"saved memberships not reflected on reload: {checked} != {wanted}"
    )


@requires_server
def test_the_banner_states_the_live_setting_not_a_constant(as_user, admin_id):
    """The control for the banner. Two processes, two settings, two banners.

    Without this, ``Cohort isolation is OFF`` passing proves nothing: a template
    that hardcoded it would pass too.
    """
    off = as_user(admin_id).get("/admin/cohorts").text
    assert "Cohort isolation is OFF" in off, (
        "the default process should report isolation OFF; if this fails, "
        "COHORT_ISOLATION_ENABLED leaked into the E2E_BASE_URL process"
    )
    assert "Cohort isolation is ACTIVE" not in off


@requires_server
@requires_isolation_server
def test_the_banner_and_gate_change_when_isolation_is_on(admin_id):
    """Second half of the control, against the isolation-on process."""
    with httpx.Client(base_url=ISOLATION_URL, follow_redirects=True, timeout=30) as c:
        c.cookies.set(COOKIE_NAME, forge_session_cookie(admin_id))
        page = c.get("/admin/cohorts/topology").text
    assert "Cohort isolation is ACTIVE" in page, (
        "the isolation-on process still reports OFF — E2E_ISOLATION_BASE_URL is "
        "probably pointing at the same process as E2E_BASE_URL"
    )
    assert "active agents are gated" in page
    # The gate preview must stop saying "gate off" for the agents we put in the
    # cohort. That is the assertion with teeth: the banner alone could be text.
    assert page.count("everyone (gate off for this agent)") < 5, (
        "isolation is reported ACTIVE but every agent is still ungated — the "
        "topology saved by the previous test is not reaching compute_gates"
    )


@requires_server
def test_agent_self_service_signup(as_user):
    """FLOWS['agent_self_service_signup'].

    Needs E2E_SIGNUP_USER_ID. Idempotent: a second run finds the agent already
    present and still lands on the pending page.
    """
    user_id = os.environ.get("E2E_SIGNUP_USER_ID", "")
    if not user_id:
        pytest.skip("needs E2E_SIGNUP_USER_ID (printed by python -m tests.e2e.seed)")

    r = as_user(user_id).post("/agent/request")
    assert r.status_code == 200, f"POST /agent/request failed: {r.status_code}"
    for want in FLOWS["agent_self_service_signup"]["expect"]:
        assert want in r.text, (
            f"{want!r} missing after signup — either the request was rejected "
            "(the user needs onboarding_complete AND a profile) or the pending "
            "template changed"
        )
    # Control: the derived bot name comes from the user's surname, so a wrong
    # derivation (e.g. using the first name) would still render a pending page.
    assert "ProbesmithBot" in r.text


@requires_server
def test_onboarding_goes_as_far_as_the_orcid_dependency(as_user):
    """FLOWS['onboarding'] — the honest stopping point.

    Asserts the spinner, NOT the completion: a fresh onboarding user cannot get
    past step 3 without a profile, and a profile cannot be built without a
    usable ORCID record. See FLOWS['onboarding']['stops_at'].
    """
    user_id = os.environ.get("E2E_ONBOARDING_USER_ID", "")
    if not user_id:
        pytest.skip("needs E2E_ONBOARDING_USER_ID (printed by tests.e2e.seed)")
    r = as_user(user_id).get("/onboarding")
    assert r.status_code == 200, "/onboarding is not reachable with a session"
    if "Building Your Profile" in r.text:
        assert "Step 3 of 4" in r.text
        return
    # The fixture has already been walked to completion by a previous run; then
    # /onboarding redirects to /profile. Both outcomes are correct, and saying
    # which one we saw is the Rule L3 part.
    assert "Profile" in r.text, (
        "/onboarding neither showed the pipeline spinner nor the completed "
        "profile — the flow is in neither documented state"
    )


@requires_server
def test_orcid_login_cannot_be_driven_and_says_why(client):
    """Not a flow: a pin on the finding, so it cannot be silently 'fixed'.

    /login/start must 302 to orcid.org with the configured client_id. If that
    client_id is a placeholder, ORCID answers 400 and there is no consent
    screen — which is why every other flow here forges a cookie instead.
    """
    r = client.get("/login/start", follow_redirects=False)
    assert r.status_code == 302, "login/start no longer redirects"
    location = r.headers["location"]
    assert location.startswith("https://orcid.org/oauth/authorize"), location
    m = re.search(r"client_id=([^&]+)", location)
    assert m, f"no client_id in the ORCID authorize URL: {location}"
    if m.group(1) in ("test-client-id", ""):
        pytest.xfail(
            "ORCID_CLIENT_ID is the placeholder 'test-client-id': ORCID answers "
            "HTTP 400 invalid_request / 'Invalid parameter: client_id', so no "
            "browser can complete login. Configure real ORCID OAuth credentials."
        )
