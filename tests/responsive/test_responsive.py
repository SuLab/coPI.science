"""Structural Playwright tier: 36 renders x 4 viewports, plus nav + export tests.

One module so the module-scoped seed/server/browser fixtures start once (see
``conftest.py``). Screenshots land in ``tests/responsive/_out/`` (gitignored).
"""

from pathlib import Path
from urllib.parse import urlparse

import pytest

from tests.e2e.session import COOKIE_NAME, forge_session_cookie
from tests.responsive.checks import assert_all
from tests.responsive.conftest import OUT_SUBDIR, SCREENSHOT_ONLY, VIEWPORTS, get_context

_OUT_DIR = Path(__file__).resolve().parent / "_out" / OUT_SUBDIR

# (name, path template, who, has_h1). Path templates are formatted against the
# ``seeded`` ids dict. ``who`` selects which forged-session cookie the shared
# context for this (width, who) pair carries; None means unauthenticated.
ROUTES = [
    ("landing", "/", None, True),
    ("login", "/login", None, True),
    ("access_pending", "/access-pending", None, True),
    ("unsubscribe", "/settings/unsubscribe/{unsub_token}", None, True),
    ("invite_accept", "/invite/{invite_token}", "researcher", True),
    ("invite_error", "/invite/not-a-token", None, True),
    ("profile_view", "/profile", "researcher", True),
    ("profile_edit", "/profile/edit", "researcher", True),
    ("delete_refusal", "/profile/delete-account", "researcher", True),
    ("delete_confirm", "/profile/delete-account", "onboarding", True),
    ("onboarding_review", "/onboarding", "onboarding", True),
    ("onboarding_private", "/onboarding/private-profile", "onboarding", True),
    ("settings", "/settings", "researcher", True),
    ("agent_request", "/agent", "onboarding", True),
    ("agent_listing", "/agent", "researcher", True),
    ("agent_dashboard", "/agent/{res_agent}/dashboard", "researcher", True),
    ("agent_conversations", "/agent/{res_agent}/conversations", "researcher", True),
    ("agent_thread", "/agent/{res_agent}/thread/{thread_ts}", "researcher", False),
    ("agent_profile", "/agent/{res_agent}/profile", "researcher", True),
    ("agent_profile_edit", "/agent/{res_agent}/profile/edit", "researcher", True),
    ("agent_public", "/agent/{res_agent}/public-profile", "researcher", True),
    ("agent_public_edit", "/agent/{res_agent}/public-profile/edit", "researcher", True),
    ("admin_users", "/admin/users", "admin", True),
    ("admin_user_detail", "/admin/users/{user_detail_id}", "admin", True),
    ("admin_jobs", "/admin/jobs", "admin", True),
    ("admin_activity", "/admin/activity", "admin", True),
    ("admin_activity_detail", "/admin/activity/{run_id}", "admin", True),
    ("admin_llm_calls", "/admin/activity/{run_id}/llm-calls", "admin", True),
    ("admin_discussions", "/admin/discussions", "admin", True),
    ("admin_agents", "/admin/agents", "admin", True),
    ("admin_agent_detail", "/admin/agents/{agent_row_id}", "admin", True),
    ("admin_access", "/admin/access-requests", "admin", True),
    ("admin_waitlist", "/admin/waitlist", "admin", True),
    ("admin_cohorts", "/admin/cohorts", "admin", True),
    ("admin_topology", "/admin/cohorts/topology", "admin", True),
    ("admin_cohort_detail", "/admin/cohorts/{cohort_id}", "admin", True),
]

_ROUTE_IDS = [f"{name}" for name, *_ in ROUTES]

# Branch-disambiguating markers: routes that serve two templates with status 200.
ROUTE_MARKERS = {
    "invite_accept": "form[action$='/accept']",
    "invite_error": "text=/invitation/i",
    "delete_confirm": "form[action='/profile/delete-account'] input",
    "agent_request": "h2:has-text(\"Get Your Own Lab Agent\")",
    "agent_listing": "a[href$='/dashboard']",
    "onboarding_review": "form[action='/onboarding/save-profile']",
}
ROUTE_ABSENT = {
    "invite_accept": ("text=/invitation is no longer valid|expired|not found/i",),
    "delete_refusal": ("form[action='/profile/delete-account'] input",),
}


def _attach_error_collectors(page):
    page._console_errors = []
    page._page_errors = []
    page.on(
        "console",
        lambda msg: page._console_errors.append(msg.text) if msg.type == "error" else None,
    )
    page.on("pageerror", lambda exc: page._page_errors.append(str(exc)))


@pytest.fixture
def out_dir():
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    return _OUT_DIR


@pytest.mark.parametrize("route", ROUTES, ids=_ROUTE_IDS)
@pytest.mark.parametrize("width", [320, 390, 768, 1280])
def test_page(route, width, browser, server, seeded, contexts, out_dir):
    name, path_template, who, has_h1 = route
    path = path_template.format(**seeded)
    url = server + path
    context = get_context(contexts, browser, server, seeded, width, who)
    page = context.new_page()
    _attach_error_collectors(page)
    try:
        resp = page.goto(url, wait_until="networkidle")
        if SCREENSHOT_ONLY:
            page.screenshot(
                path=str(out_dir / f"{name}-{width}.png"), full_page=True, animations="disabled"
            )
            return
        assert resp is not None and resp.status == 200, (
            f"GET {path} -> {resp.status if resp else 'no response'} "
            f"(final URL: {page.url})"
        )
        final_path = urlparse(page.url).path
        assert final_path == path.split("?")[0], (
            f"GET {path} was redirected to {final_path}; the seed does not satisfy this route"
        )
        marker = ROUTE_MARKERS.get(name)
        if marker is not None:
            assert page.locator(marker).count() >= 1, (
                f"{name}: expected marker {marker!r} not rendered (wrong branch of the route)"
            )
        for absent in ROUTE_ABSENT.get(name, ()):
            assert page.locator(absent).count() == 0, (
                f"{name}: {absent!r} rendered, meaning the other branch of the route was served"
            )
        assert_all(page, width, has_h1=has_h1)
        page.screenshot(
            path=str(out_dir / f"{name}-{width}.png"),
            full_page=True,
            animations="disabled",
        )
    finally:
        page.close()


@pytest.mark.parametrize("width", [320, 390, 768, 1280])
def test_admin_discussions_export(width, browser, server, seeded, contexts, out_dir):
    """The export route sets Content-Disposition: attachment, which Chromium
    treats as a download rather than a navigable response — fetch it out of
    band and render the body via set_content instead of page.goto."""
    context = get_context(contexts, browser, server, seeded, width, "admin")
    page = context.new_page()
    _attach_error_collectors(page)
    try:
        page.goto(server + "/login", wait_until="networkidle")
        resp = page.request.get(server + "/admin/discussions?export=html")
        assert resp.status == 200, f"export GET -> {resp.status}"
        page.set_content(resp.text())
        assert_all(page, width, has_h1=True)
        page.screenshot(
            path=str(out_dir / f"admin_discussions_export-{width}.png"),
            full_page=True,
            animations="disabled",
        )
    finally:
        page.close()


# --------------------------------------------------------------------------
# Mobile nav tests
# --------------------------------------------------------------------------


@pytest.mark.parametrize("width", [320, 390])
def test_nav_menu_opens_and_closes(width, browser, server, seeded, contexts):
    context = get_context(contexts, browser, server, seeded, width, "researcher")
    page = context.new_page()
    _attach_error_collectors(page)
    try:
        page.goto(server + "/profile", wait_until="networkidle")
        summary = page.locator("#primary-menu summary")
        box = summary.bounding_box()
        assert box is not None
        assert box["width"] >= 44 and box["height"] >= 44, (
            f"nav summary is {box['width']:.0f}x{box['height']:.0f}, needs >= 44x44"
        )

        summary.click()
        details = page.locator("#primary-menu[open]")
        assert details.count() == 1, "clicking the summary did not open <details id=primary-menu>"

        for link in page.locator("#primary-menu a").all():
            assert link.is_visible(), f"nav link {link.inner_text()!r} not visible while menu is open"

        page.keyboard.press("Escape")
        assert page.locator("#primary-menu[open]").count() == 0, "Escape did not close the nav menu"

        summary.click()
        assert page.locator("#primary-menu[open]").count() == 1
        page.mouse.click(width - 5, 5)
        assert page.locator("#primary-menu[open]").count() == 0, (
            "outside pointerdown did not close the nav menu"
        )
    finally:
        page.close()


@pytest.mark.parametrize("width", [320, 390])
def test_admin_subnav_active_link_in_view(width, browser, server, seeded, contexts):
    context = get_context(contexts, browser, server, seeded, width, "admin")
    page = context.new_page()
    _attach_error_collectors(page)
    try:
        # Waitlist is the LAST of eight links: off-screen at 320/390 unless scrollIntoView ran.
        page.goto(server + "/admin/waitlist", wait_until="networkidle")
        active = page.locator('#admin-subnav [aria-current="page"]')
        assert active.count() == 1
        box = active.bounding_box()
        assert box is not None
        assert box["x"] >= 0 and box["x"] + box["width"] <= width, (
            f"active sub-nav link at x={box['x']:.0f} w={box['width']:.0f} "
            f"is outside the viewport width {width}"
        )
    finally:
        page.close()


@pytest.mark.parametrize("width", [320, 390])
def test_nav_menu_js_off(width, browser, server, seeded):
    """Zero-JS smoke test: the native <details>/<summary> still opens on click."""
    context = browser.new_context(**VIEWPORTS[width], java_script_enabled=False)
    context.add_cookies(
        [{
            "name": COOKIE_NAME,
            "value": forge_session_cookie(seeded["researcher_id"]),
            "url": server,
        }]
    )
    try:
        page = context.new_page()
        page.goto(server + "/profile", wait_until="networkidle")
        summary = page.locator("#primary-menu summary")
        summary.click()
        assert page.locator("#primary-menu[open]").count() == 1
        for link in page.locator("#primary-menu a").all():
            assert link.is_visible()
    finally:
        context.close()


@pytest.mark.parametrize(
    ("path", "table_id", "detail_prefix"),
    [("/admin/users", "users-table", "/admin/users/"), ("/admin/activity", "activity-runs-table", "/admin/activity/")],
)
def test_covering_row_link_makes_the_whole_row_clickable(
    path, table_id, detail_prefix, browser, server, seeded, contexts
):
    """The old onclick=location.href rows became a first-cell <a> whose ::after covers
    the row. A sticky first cell would shrink that overlay to the cell, so click the
    LAST cell and require navigation to the detail page."""
    context = get_context(contexts, browser, server, seeded, 1280, "admin")
    page = context.new_page()
    try:
        page.goto(server + path, wait_until="networkidle")
        last_cell = page.locator(f"#{table_id} tbody tr").first.locator("td").last
        box = last_cell.bounding_box()
        assert box is not None
        with page.expect_navigation(wait_until="networkidle"):
            page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        assert urlparse(page.url).path.startswith(detail_prefix), page.url
    finally:
        page.close()
