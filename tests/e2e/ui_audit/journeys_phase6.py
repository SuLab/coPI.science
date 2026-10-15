"""Help tab and onboarding browser journeys (spec
docs/specs/2026-10-14-help-and-onboarding-design.md §10.3). Run with
``python -m tests.e2e.ui_audit.run all --phase 6``; help_assets/shots/ must be captured
first (tests/e2e/help_shots/README.md), or every help page with a figure fails.

Every journey creates its own manager or reviewer accounts with no help_intro_dismissals
row, through the harness environment, so no journey depends on another's answer to the
first-sign-in prompt or on the order they run in. Each fails on a console error, an
uncaught page error, an enforced CSP violation, a failed request or an unexpected 4xx/5xx.
"""

from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import urljoin, urlsplit

from tests.e2e.ui_audit.journeys_phase1_1b import _cookie, _seed
from tests.e2e.ui_audit.journeys_phase3 import (
    _CSP_COLLECTOR_JS,
    _drain_csp,
    _flash,
    _goto,
    _submit,
)
from tests.e2e.ui_audit.journeys_phase5 import _healthy

_PROMPT = '[role="region"][aria-label="Getting started"]'
_STEP_NAV = 'nav[aria-label="Tutorial steps"]'
_HELP_LINK = 'a:has-text("Help with this page")'
_NARROW = 375
_AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]


def _new_account(h, role: str, name: str, number: int) -> str:
    """A fresh ``role`` account with no dismissal row; returns its user id. ``number``
    keeps the ORCID iD and email unique across journeys."""
    made = _seed(h, f'''
u = await factories.make_user(s, user_role={role!r}, name={name!r},
                              orcid="0000-0002-6600-{number:04d}",
                              email="help-journey-{number}@uiaudit.test")
return {{"user": str(u.id)}}
''')
    return made["user"]


async def _open_as(h, user_id: str, *, width: int = 1280, bypass_csp: bool = False):
    """A page signed in as ``user_id`` with the CSP collector installed."""
    ctx, page, log = await h.page(None, width=width, bypass_csp=bypass_csp)
    await ctx.add_cookies([{"name": h.cookie_name, "value": _cookie(h, user_id),
                            "url": h.base_url}])
    await ctx.add_init_script(_CSP_COLLECTOR_JS)
    return ctx, page, log, []


def _path(page) -> str:
    """The current URL's path and query, without the base URL or a fragment."""
    parts = urlsplit(page.url)
    return parts.path + (f"?{parts.query}" if parts.query else "")


async def _walk_tutorial(h, page, csp: list, key: str, landing: str, steps: int) -> dict:
    """From ``landing`` (prompt shown): Start the tutorial, every step's "Open this page"
    answers 200, Next through the last step, Finish; the flash shows on /help and the
    prompt is gone from ``landing``."""
    from src.web.help_pages import TUTORIALS

    tutorial = next(t for t in TUTORIALS if t.key == key)
    checks: dict = {"registry_steps": len(tutorial.steps) == steps}
    await _goto(h, page, landing, csp)
    checks["prompt_shown"] = await page.locator(_PROMPT).count() == 1
    await _submit(h, page, page.locator(_PROMPT).get_by_role(
        "link", name="Start the tutorial"), csp)
    visited, opened = [], {}
    for number, step in enumerate(tutorial.steps, start=1):
        visited.append(_path(page))
        nav = page.locator(_STEP_NAV)
        hrefs = await nav.locator('a[target="_blank"]').evaluate_all(
            "(links) => links.map((a) => a.getAttribute('href'))")
        statuses = []
        for href in hrefs:
            response = await page.request.get(urljoin(h.base_url + "/", href))
            statuses.append(response.status)
        opened[number] = {"expected": step.open_route is not None, "hrefs": hrefs,
                          "statuses": statuses}
        if number < len(tutorial.steps):
            await _submit(h, page, nav.locator('a:has-text("Next step")'), csp)
        else:
            await _submit(h, page, nav.get_by_role("button", name="Finish"), csp)
    checks["steps_in_order"] = visited == [f"/help/tutorials/{key}/{n}"
                                           for n in range(1, len(tutorial.steps) + 1)]
    checks["open_links_200"] = all(
        len(o["hrefs"]) == (1 if o["expected"] else 0) and all(s == 200 for s in o["statuses"])
        for o in opened.values())
    checks["finished_on_help"] = _path(page) == "/help"
    checks["finish_flash"] = "Tutorial finished." in await _flash(page)
    await _goto(h, page, landing, csp)
    checks["prompt_gone"] = await page.locator(_PROMPT).count() == 0
    return {"checks": checks, "visited": visited, "opened": opened}


async def journey_reviewer_tutorial(h) -> dict:
    """A new reviewer starts the tutorial from the prompt, walks all 8 steps and
    finishes; the prompt does not come back."""
    uid = _new_account(h, "reviewer", "Tutorial Reviewer", 1)
    ctx, page, log, csp = await _open_as(h, uid)
    try:
        walk = await _walk_tutorial(h, page, csp, "reviewer", "/workspace/assessments", 8)
        clean = _healthy(log, csp)
    finally:
        await ctx.close()
    ok = all(walk["checks"].values()) and clean["clean"]
    return {"ok": ok, **walk, "log": clean}


def _help_link_pages(h) -> tuple[tuple[str, str], ...]:
    """(page, its "Help with this page" target), per spec §6.6."""
    ids = h.ids
    return (
        ("/workspace/pis", "/help/pis#directory"),
        (f"/workspace/pis/{ids['p3_pi']}", "/help/pis#detail"),
        ("/workspace/assessments", "/help/assessments#list"),
        (f"/workspace/assessments/{ids['assessments'][0]}", "/help/assessments#detail"),
        ("/workspace/slack-bots", "/help/managing-pis#slack-bots"),
        ("/workspace/staff", "/help/staff-accounts"),
        ("/workspace/discussions", "/help/discussions-activity#discussions"),
        ("/workspace/activity", "/help/discussions-activity#activity"),
        (f"/workspace/activity/{ids['run']}", "/help/discussions-activity#run-detail"),
        ("/workspace/prompt-suggestions", "/help/prompt-suggestions"),
        (f"/workspace/prompt-suggestions/{ids['p3_prompt_suggestion']}",
         "/help/prompt-suggestions#detail"),
        ("/settings", "/help/getting-started#settings"),
    )


async def journey_manager_tutorial(h) -> dict:
    """A new manager walks all 10 steps and finishes; then every §6.6 page's Help link
    is the listed target and lands on an element whose id is the fragment."""
    uid = _new_account(h, "manager", "Tutorial Manager", 2)
    ctx, page, log, csp = await _open_as(h, uid)
    links: dict[str, dict] = {}
    try:
        walk = await _walk_tutorial(h, page, csp, "manager", "/workspace/pis", 10)
        for source, target in _help_link_pages(h):
            await _goto(h, page, source, csp)
            link = page.locator(_HELP_LINK)
            count = await link.count()
            href = await link.first.get_attribute("href") if count else None
            landed = None
            if href:
                response = await page.goto(urljoin(h.base_url + "/", href),
                                           wait_until="networkidle")
                await _drain_csp(h, page, csp)
                fragment = urlsplit(href).fragment
                landed = bool(response and response.status == 200) and (
                    not fragment or await page.evaluate(
                        "(id) => document.getElementById(id) !== null", fragment))
            links[source] = {"count": count, "href": href, "landed": landed,
                             "ok": count == 1 and href == target and landed is True}
        clean = _healthy(log, csp)
    finally:
        await ctx.close()
    ok = (all(walk["checks"].values()) and all(v["ok"] for v in links.values())
          and clean["clean"])
    return {"ok": ok, **walk, "help_links": links, "log": clean}


async def journey_dismiss_returns(h) -> dict:
    """"Don't show again" from a filtered workspace page returns to that exact URL, with
    the info flash and no prompt."""
    uid = _new_account(h, "reviewer", "Dismissing Reviewer", 3)
    start = "/workspace/assessments?review=all"
    ctx, page, log, csp = await _open_as(h, uid)
    try:
        await _goto(h, page, start, csp)
        shown = await page.locator(_PROMPT).count() == 1
        await _submit(h, page, page.locator(_PROMPT).get_by_role(
            "button", name="Don't show again"), csp)
        final = _path(page)
        checks = {
            "prompt_shown": shown,
            "same_url": final == start,
            "flash": "The tutorials are always under Help." in await _flash(page),
            "prompt_gone": await page.locator(_PROMPT).count() == 0,
        }
        clean = _healthy(log, csp)
    finally:
        await ctx.close()
    return {"ok": all(checks.values()) and clean["clean"], "checks": checks,
            "final": final, "log": clean}


def _narrow_paths(staff: bool) -> list[str]:
    """Every help page, tutorial step and the two prompt-bearing workspace pages a
    reviewer (``staff`` False) or manager can open."""
    from src.web.help_pages import pages_for, tutorials_for

    permissions = SimpleNamespace(staff=staff)
    paths = ["/help", *[f"/help/{p.slug}" for p in pages_for(permissions)]]
    for tutorial in tutorials_for(permissions):
        paths += [f"/help/tutorials/{tutorial.key}/{n}"
                  for n in range(1, len(tutorial.steps) + 1)]
    return [*paths, "/workspace/assessments", "/workspace/pis"]


_LAYOUT_JS = """() => ({
  overflow: document.documentElement.scrollWidth - window.innerWidth,
  wide_images: [...document.querySelectorAll('main img')]
    .filter((img) => img.getBoundingClientRect().width > window.innerWidth)
    .map((img) => img.getAttribute('src')),
})"""


async def journey_help_narrow_layout(h) -> dict:
    """At 375 px, for a new reviewer and a new manager: every help page, every tutorial
    step, and /workspace/assessments and /workspace/pis with the prompt, have no
    horizontal scroll, no image wider than the viewport and zero axe findings."""
    if not h.axe_source:
        return {"ok": False, "error": "no axe-core source on the harness"}
    pages: dict[str, dict] = {}
    logs: dict[str, dict] = {}
    for role, number in (("reviewer", 4), ("manager", 5)):
        uid = _new_account(h, role, f"Narrow {role.title()}", number)
        ctx, page, log, csp = await _open_as(h, uid, width=_NARROW)
        axe_ctx, axe_page, _axe_log, _axe_csp = await _open_as(h, uid, width=_NARROW,
                                                               bypass_csp=True)
        try:
            for path in _narrow_paths(role == "manager"):
                response = await page.goto(h.base_url + path, wait_until="networkidle")
                await _drain_csp(h, page, csp)
                layout = await page.evaluate(_LAYOUT_JS)
                record = {"status": response.status if response else None, **layout}
                if path.startswith("/workspace/"):
                    record["prompt"] = await page.locator(_PROMPT).count() == 1
                # axe in its own CSP-bypassing context: the bundle is injected as an
                # inline script, which the enforced script-src blocks (crawl.py _axe).
                await axe_page.goto(h.base_url + path, wait_until="networkidle")
                await axe_page.add_script_tag(content=h.axe_source)
                record["axe"] = await axe_page.evaluate(
                    """async (tags) => (await axe.run(document, {runOnly: {type: 'tag',
                        values: tags}})).violations.map((v) => [v.id, v.nodes.length])""",
                    _AXE_TAGS)
                record["ok"] = (record["status"] == 200 and layout["overflow"] <= 0
                                and not layout["wide_images"] and not record["axe"]
                                and record.get("prompt", True))
                pages[f"{role} {path}"] = record
            logs[role] = _healthy(log, csp)
        finally:
            await ctx.close()
            await axe_ctx.close()
    failing = {key: value for key, value in pages.items() if not value["ok"]}
    ok = bool(pages) and not failing and all(lg["clean"] for lg in logs.values())
    return {"ok": ok, "pages_checked": len(pages), "failing": failing, "logs": logs}


JOURNEYS = [
    journey_reviewer_tutorial,
    journey_manager_tutorial,
    journey_dismiss_returns,
    journey_help_narrow_layout,
]
