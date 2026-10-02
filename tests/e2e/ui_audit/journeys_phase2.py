"""Phase 2 browser journeys (spec §9). Run with
``python -m tests.e2e.ui_audit.run journeys --phase 2``.

Part 2A: D-14 thread fetch after the session ends, B-04 double-click on a review
submit, M-08 staff redirects away from PI-only pages."""

import time


async def journey_thread_fetch_after_session_expiry(h) -> dict:
    """D-14: with the session gone, expanding a thread shows a session notice (not
    the login page) and marks the link expanded."""
    ctx, page, log = await h.page("pi2")
    try:
        await page.goto(h.base_url + "/agent/vogelstein/conversations", wait_until="networkidle")
        link = page.locator("[data-thread-expand]").first
        if not await link.count():
            return {"ok": False, "reason": "the seed has no expandable thread for pi2", "log": log}
        panel_id = await link.get_attribute("aria-controls")
        await ctx.clear_cookies()
        await link.click()
        panel = page.locator(f'[id="{panel_id}"]')
        await panel.wait_for(state="visible", timeout=5000)
        text = await panel.inner_text()
        expanded = await link.get_attribute("aria-expanded")
        injected = await panel.locator("form, a[href^='/login']").count()
        return {
            "ok": "session has ended" in text and expanded == "true" and injected == 0,
            "panel_text": text[:200],
            "aria_expanded": expanded,
            "injected_login_nodes": injected,
            "log": log,
        }
    finally:
        await ctx.close()


async def journey_review_double_click_stores_one(h) -> dict:
    """B-04: a double click on "Submit feedback" stores one review."""
    ctx, page, log = await h.page("reviewer")
    try:
        assessment_id = h.ids["assessments"][0]
        await page.goto(
            f"{h.base_url}/manager/assessments/{assessment_id}", wait_until="networkidle"
        )
        comment = f"double-click-{time.time_ns()}"
        await page.select_option("#add-score", "4")
        await page.fill("#add-comment", comment)
        await page.select_option("#add-mode", "log_only")
        submit = page.locator("#add-comment").locator("xpath=ancestor::form").locator(
            "button[type=submit]"
        )
        await submit.dblclick()
        await page.wait_for_load_state("networkidle")
        await page.goto(
            f"{h.base_url}/manager/assessments/{assessment_id}", wait_until="networkidle"
        )
        stored = await page.get_by_text(comment, exact=True).count()
        return {"ok": stored == 1, "stored": stored, "log": log}
    finally:
        await ctx.close()


async def journey_staff_redirected_from_pi_pages(h) -> dict:
    """M-08: managers and reviewers land on their own pages, not PI forms."""
    expected = {
        ("manager", "/profile"): "/manager/pis",
        ("manager", "/profile/edit"): "/manager/pis",
        ("manager", "/agent"): "/manager/pis",
        ("reviewer", "/agent"): "/manager/assessments",
    }
    landed = {}
    for (role, path), _target in expected.items():
        ctx, page, _log = await h.page(role)
        try:
            await page.goto(h.base_url + path, wait_until="networkidle")
            landed[f"{role} {path}"] = page.url[len(h.base_url):]
        finally:
            await ctx.close()
    ok = all(
        landed[f"{role} {path}"].startswith(target) for (role, path), target in expected.items()
    )
    return {"ok": ok, "landed": landed}


JOURNEYS = [
    journey_thread_fetch_after_session_expiry,
    journey_review_double_click_stores_one,
    journey_staff_redirected_from_pi_pages,
]
