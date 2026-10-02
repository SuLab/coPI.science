"""Web UI remediation Phase 2, Part 2B journeys (B-16, B-17, B-20, X-03/X-04,
R-01/FN-03/R-02; Task 2B-15 adds the enforced CSP). Run through journeys_phase2.JOURNEYS by
`python -m tests.e2e.ui_audit.run journeys --phase 2`."""

from __future__ import annotations

from pathlib import Path

NARROW = 375
WIDE = 1280
BAD_UUID = "00000000-0000-0000-0000-000000000000"
_AXE = Path(__file__).with_name("axe.min.js")


def _detail_path(ids: dict) -> str:
    return f"/admin/assessments/{ids['assessments'][0]}"


def _history_payload(streaming: bool) -> dict:
    """A synthetic chat history: six long complete turns (the first cites the page's
    #brief card) and, when `streaming`, a seventh still being answered so the drawer
    keeps polling every 5 s."""
    def turn(i: int, status: str) -> dict:
        return {
            "id": f"turn-{i}", "question": f"Question {i} " + "about the proposal " * 20,
            "segments": [] if status == "streaming" else [{"text": "Answer paragraph. " * 60, "cites": [1] if i == 0 else []}],
            "citations": [{"n": 1, "label": "Brief", "anchor": "brief"}] if i == 0 else [],
            "allowed_links": [], "status": status, "stop_reason": None, "refusal_category": None,
            "error_code": None, "served_by_model": None, "fallback_used": False, "in_window": True,
            "verdict_revision": 1, "record_changed": False, "created_at": None, "completed_at": None,
        }

    turns = [turn(i, "complete") for i in range(6)]
    if streaming:
        turns.append(turn(6, "streaming"))
    return {
        "tier": "staff", "verdict_revision": 1, "turns": turns, "questions_used_24h": 6,
        "daily_limit": 50, "max_question_chars": 4000, "max_turns": 20, "verdict_may_change": False,
    }


async def _fake_history(page, ids: dict, streaming: bool) -> list[str]:
    """Answer the drawer's history GETs with `_history_payload`; returns the list the
    handler appends each answered URL to."""
    answered: list[str] = []

    async def handler(route):
        if route.request.method != "GET":
            await route.continue_()
            return
        answered.append(route.request.url)
        await route.fulfill(status=200, json=_history_payload(streaming))

    await page.route(f"**/assessment-chat/{ids['assessments'][0]}*", handler)
    return answered


async def _open_drawer(page) -> bool:
    if not await page.locator("[data-chat-bubble]").count():
        return False
    await page.locator("[data-chat-bubble]").click()
    await page.wait_for_selector("#assessment-chat:not(.hidden)")
    return True


async def _modality(page) -> dict:
    return await page.evaluate(
        """() => { const d = document.getElementById('assessment-chat');
                   return {role: d.getAttribute('role'), modal: d.getAttribute('aria-modal'),
                           inert: document.querySelectorAll('[inert]').length}; }"""
    )


def _routes(ids: dict) -> dict[str, list[str]]:
    """The crawl set per seeded role (the PI's /agent is expanded to its agent pages
    after its redirect is followed)."""
    run, first = ids["run"], ids["assessments"][0]
    return {
        "anon": ["/login"],
        "admin": [
            "/admin/users", f"/admin/users/{ids['pi']}", f"/admin/users/{BAD_UUID}", "/admin/jobs",
            "/admin/activity", f"/admin/activity/{run}", f"/admin/activity/{run}/llm-calls",
            "/admin/discussions", "/admin/agents", "/admin/assessments",
            *[f"/admin/assessments/{a}" for a in ids["assessments"]],
            "/admin/cohorts", "/admin/cohorts/topology", "/admin/access-requests",
            "/admin/simulation", "/manager/prompt-suggestions",
        ],
        "manager": [
            "/manager/pis", f"/manager/pis/{ids['pi']}", "/manager/assessments",
            f"/manager/assessments/{first}", "/manager/discussions", "/manager/activity",
            f"/manager/activity/{run}", "/manager/slack-bots", "/manager/prompt-suggestions",
        ],
        "reviewer": ["/manager/pis", "/manager/assessments", f"/manager/assessments/{first}"],
        "pi": ["/profile", "/profile/edit", "/settings", "/agent"],
    }


async def _visit_all(h, width: int, on_page, *, bypass_csp: bool = False) -> list[dict]:
    """Load every crawl route at `width`, calling `await on_page(page, role, url, response)`
    for each, and return the list of its non-None results. Journeys that inject axe-core
    pass `bypass_csp=True`: Playwright injects it as an inline script, which the enforced
    script-src (Task 2B-15) blocks; the CSP journey itself never bypasses."""
    results = []
    for role, paths in _routes(h.ids).items():
        context, page, _log = await h.page(role, width=width, bypass_csp=bypass_csp)
        try:
            queue = list(paths)
            while queue:
                path = queue.pop(0)
                response = await page.goto(h.base_url + path, wait_until="networkidle")
                if path == "/agent" and page.url.endswith("/dashboard"):
                    queue += [page.url.replace(h.base_url, "").replace("/dashboard", tail)
                              for tail in ("/conversations", "/public-profile")]
                result = await on_page(page, role, page.url.replace(h.base_url, ""), response)
                if result is not None:
                    results.append(result)
        finally:
            await context.close()
    return results


_X04_RULES = ["region", "landmark-unique", "landmark-one-main", "page-has-heading-one",
              "heading-order", "empty-table-header", "link-in-text-block"]


async def journey_landmarks_headings_and_link_underlines(h) -> dict:
    """X-04 and X-03 via axe-core at 1280 px: zero violations of the landmark, heading,
    empty-header and link-in-text-block rules on every crawled page."""
    async def on_page(page, role, url, response):
        if "html" not in (response.headers.get("content-type", "") if response else ""):
            return None
        await page.add_script_tag(path=str(_AXE))
        violations = await page.evaluate(
            """async (rules) => (await axe.run(document, {runOnly: {type: 'rule', values: rules}}))
                 .violations.map(v => ({id: v.id, n: v.nodes.length,
                                        sample: v.nodes.slice(0, 2).map(n => n.target.join(' '))}))""",
            _X04_RULES,
        )
        return {"role": role, "url": url, "violations": violations} if violations else None

    failing = await _visit_all(h, WIDE, on_page, bypass_csp=True)
    return {"ok": not failing, "failing_pages": failing}


async def journey_narrow_crawl_has_no_overflow(h) -> dict:
    """R-01/FN-03/R-02 (spec §9): at 375 px no crawled page overflows the viewport
    horizontally on the seeded realistic data (long names, 300-character URLs)."""
    async def on_page(page, role, url, response):
        if "html" not in (response.headers.get("content-type", "") if response else ""):
            return None
        overflow = await page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        if overflow <= 0:
            return None
        widest = await page.evaluate(
            """() => [...document.querySelectorAll('body *')]
                 .filter(e => e.getBoundingClientRect().right > window.innerWidth + 1
                              && getComputedStyle(e).position !== 'fixed')
                 .slice(0, 4).map(e => e.tagName + '.' + String(e.className).slice(0, 60))"""
        )
        return {"role": role, "url": url, "overflow_px": overflow, "widest": widest}

    failing = await _visit_all(h, NARROW, on_page)
    return {"ok": not failing, "failing_pages": failing}


async def journey_drawer_modality_follows_resize(h) -> dict:
    """B-16: opened wide (not modal), narrowed to 375 px (modal, page inert), widened
    again (not modal, nothing left inert)."""
    context, page, log = await h.page("admin", width=WIDE)
    try:
        await page.goto(h.base_url + _detail_path(h.ids), wait_until="networkidle")
        if not await _open_drawer(page):
            return {"ok": False, "reason": "no chat opener on the detail page", "log": log}
        wide = await _modality(page)
        await page.set_viewport_size({"width": NARROW, "height": 800})
        await page.wait_for_timeout(300)
        narrow = await _modality(page)
        await page.set_viewport_size({"width": WIDE, "height": 900})
        await page.wait_for_timeout(300)
        wide_again = await _modality(page)
    finally:
        await context.close()
    ok = (
        wide == {"role": None, "modal": None, "inert": 0}
        and narrow["role"] == "dialog" and narrow["modal"] == "true" and narrow["inert"] > 0
        and wide_again == wide
    )
    return {"ok": ok, "wide": wide, "narrow": narrow, "wide_again": wide_again, "log": log}


async def journey_poll_rerender_keeps_scroll_and_focus(h) -> dict:
    """B-17: with an answer streaming the drawer re-renders every poll; a reader
    scrolled to the top with focus on a Sources toggle keeps both."""
    context, page, log = await h.page("admin", width=WIDE)
    try:
        answered = await _fake_history(page, h.ids, streaming=True)
        await page.goto(h.base_url + _detail_path(h.ids), wait_until="networkidle")
        if not await _open_drawer(page):
            return {"ok": False, "reason": "no chat opener on the detail page", "log": log}
        await page.wait_for_selector("#chat-sources-toggle-turn-0")
        await page.evaluate("document.querySelector('[data-chat-log]').scrollTop = 0")
        await page.focus("#chat-sources-toggle-turn-0")
        before = len(answered)
        await page.wait_for_timeout(6500)
        state = await page.evaluate(
            """() => ({focus: document.activeElement && document.activeElement.id,
                       top: document.querySelector('[data-chat-log]').scrollTop})"""
        )
        polls = len(answered) - before
    finally:
        await context.close()
    ok = polls >= 1 and state["focus"] == "chat-sources-toggle-turn-0" and state["top"] < 50
    return {"ok": ok, "polls": polls, "after_poll": state,
            "poll_urls": answered[-2:], "log": log}


async def journey_show_in_page_clears_the_drawer(h) -> dict:
    """B-20: at 1280 px "Show in page" leaves the target visible: the drawer is
    either closed or entirely to the right of the target."""
    context, page, log = await h.page("admin", width=WIDE)
    try:
        await _fake_history(page, h.ids, streaming=False)
        await page.goto(h.base_url + _detail_path(h.ids), wait_until="networkidle")
        if not await _open_drawer(page):
            return {"ok": False, "reason": "no chat opener on the detail page", "log": log}
        await page.click("#chat-sources-toggle-turn-0")
        await page.click("#chat-src-show-turn-0-1")
        await page.wait_for_timeout(1200)
        geometry = await page.evaluate(
            """() => { const d = document.getElementById('assessment-chat');
                       const t = document.getElementById('brief').getBoundingClientRect();
                       return {drawer_hidden: d.classList.contains('hidden'),
                               drawer_left: d.getBoundingClientRect().left, target_right: t.right}; }"""
        )
    finally:
        await context.close()
    ok = geometry["drawer_hidden"] or geometry["target_right"] <= geometry["drawer_left"]
    return {"ok": ok, **geometry, "log": log}


_CSP_PROBE = """
  window.__cspViolations = [];
  document.addEventListener('securitypolicyviolation', function (e) {
    window.__cspViolations.push(e.violatedDirective + ' ' + (e.blockedURI || 'inline'));
  });
"""


async def journey_enforced_csp_crawl_has_no_violations(h) -> dict:
    """Spec §9 Phase 2: with the policy enforced, no crawled page logs a CSP violation,
    every HTML response carries the enforced header and no report-only header, and
    the two script-driven interactions (an LLM-call body loaded on expand, the chat
    drawer opened) run clean."""
    async def on_page(page, role, url, response):
        headers = response.headers if response else {}
        problems = []
        if "html" in headers.get("content-type", ""):
            if "script-src" not in headers.get("content-security-policy", ""):
                problems.append("no enforced script-src")
            if "content-security-policy-report-only" in headers:
                problems.append("report-only header still sent")
        if url.endswith("/llm-calls") and await page.locator("details summary").count():
            await page.locator("details summary").first.click()
            await page.wait_for_timeout(800)
        if "/admin/assessments/" in url and await page.locator("[data-chat-bubble]").count():
            await page.locator("[data-chat-bubble]").click()
            await page.wait_for_timeout(800)
        problems += await page.evaluate("window.__cspViolations || []")
        problems += [m for m in console if "Content Security Policy" in m]
        console.clear()
        return {"role": role, "url": url, "problems": problems} if problems else None

    console: list[str] = []
    failing = []
    for role, paths in _routes(h.ids).items():
        context, page, _log = await h.page(role, width=WIDE)
        page.on("console", lambda m: console.append(m.text))
        await page.add_init_script(_CSP_PROBE)
        try:
            for path in paths:
                response = await page.goto(h.base_url + path, wait_until="networkidle")
                result = await on_page(page, role, page.url.replace(h.base_url, ""), response)
                if result is not None:
                    failing.append(result)
        finally:
            await context.close()
    return {"ok": not failing, "failing_pages": failing}


JOURNEYS = [
    journey_drawer_modality_follows_resize,
    journey_poll_rerender_keeps_scroll_and_focus,
    journey_show_in_page_clears_the_drawer,
    journey_landmarks_headings_and_link_underlines,
    journey_narrow_crawl_has_no_overflow,
    journey_enforced_csp_crawl_has_no_violations,
]
