"""Web UI remediation Phase 2, Part 2B journeys (B-16, B-17, B-20; later tasks add
X-04, R-01/R-02 and the enforced CSP). Run through journeys_phase2.JOURNEYS by
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


JOURNEYS = [
    journey_drawer_modality_follows_resize,
    journey_poll_rerender_keeps_scroll_and_focus,
    journey_show_in_page_clears_the_drawer,
]
