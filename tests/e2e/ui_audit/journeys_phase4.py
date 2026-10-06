"""Phase-4 PI profile editing and lifecycle browser acceptance journeys."""

from __future__ import annotations

from pathlib import Path

from tests.e2e.ui_audit.journeys_phase3 import _clean, _goto, _open, _submit


async def _body(page) -> str:
    return await page.locator("body").inner_text()


async def _manager_form(page, summary: str) -> None:
    await page.fill("#pi-summary", summary)


async def journey_manager_edit(h) -> dict:
    """J4-1: manager edits a profile and the post-commit persona reflects the change."""
    pi, slug = h.ids["p4_pi_a"], h.ids["p4_pi_a_slug"]
    summary, tag = "P4 manager saved summary.", "P4 manager tag"
    ctx, page, log, csp = await _open(h, "manager")
    try:
        await _goto(h, page, f"/manager/pis/{pi}", csp)
        await _manager_form(page, summary)
        await page.locator("#tag-input-keywords").fill(tag)
        await page.locator("#tag-input-keywords").press("Enter")
        await _submit(h, page, page.locator('form[action$="/profile"] button[type="submit"]'), csp)
        text = await _body(page)
        persona = (Path.cwd() / "profiles" / "public" / f"{slug}.md").read_text()
        clean = _clean(log, csp)
    finally:
        await ctx.close()
    checks = {"flash": "Profile saved." in text, "card": summary in text,
              "revision": "Persona revisions" in text and "web" in text and "Audit Manager" in text,
              "persona": summary in persona}
    return {"ok": all(checks.values()) and clean["clean"], "checks": checks, "log": clean}


async def journey_stale_and_noop_save(h) -> dict:
    """J4-2: stale saves are refused, while a repeated identical save adds no revision."""
    pi = h.ids["p4_pi_a"]
    path = f"/manager/pis/{pi}"
    one, two = "P4 tab one summary.", "P4 stale tab two summary."
    c1, p1, l1, x1 = await _open(h, "manager")
    c2, p2, l2, x2 = await _open(h, "manager")
    try:
        await _goto(h, p1, path, x1)
        await _goto(h, p2, path, x2)
        before = await p1.locator("#revisions li, #revisions tbody tr").count()
        await _manager_form(p1, one)
        await _submit(h, p1, p1.locator('form[action$="/profile"] button[type="submit"]'), x1)
        await _manager_form(p2, two)
        await _submit(h, p2, p2.locator('form[action$="/profile"] button[type="submit"]'), x2)
        stale = await _body(p2)
        await _submit(h, p1, p1.locator('form[action$="/profile"] button[type="submit"]'), x1)
        final = await _body(p1)
        after = await p1.locator("#revisions li, #revisions tbody tr").count()
        clean = _clean(l1, x1)["clean"] and _clean(l2, x2)["clean"]
    finally:
        await c1.close()
        await c2.close()
    checks = {"stale": "This profile changed while you were editing it" in stale and one in stale,
              "noop": "Profile saved." in final and before + 1 == after}
    return {"ok": all(checks.values()) and clean, "checks": checks}


async def journey_caps_and_name_refusals(h) -> dict:
    """J4-3: cap and name refusals leave the rendered account and profile unchanged."""
    pi = h.ids["p4_pi_a"]
    ctx, page, log, csp = await _open(h, "manager")
    try:
        await _goto(h, page, f"/manager/pis/{pi}", csp)
        baseline = await _body(page)
        await _manager_form(page, " ".join(["word"] * 351))
        await _submit(h, page, page.locator('form[action$="/profile"] button[type="submit"]'), csp)
        capped = await _body(page)
        await page.fill("#pi-name", "Jane <b>Wang")
        await _submit(h, page, page.locator('form[action$="/profile"] button[type="submit"]'), csp)
        invalid = await _body(page)
        clean = _clean(log, csp)
    finally:
        await ctx.close()
    checks = {"cap": "The research summary is limited to 350 words." in capped,
              "name": "Names may contain letters" in invalid,
              "unchanged": "P4 tab one summary." in invalid and "Jane Wang" in invalid
              and "P4 tab one summary." in baseline}
    return {"ok": all(checks.values()) and clean["clean"], "checks": checks, "log": clean}


async def journey_profileless_and_reviewer_isolation(h) -> dict:
    """J4-4: staff receive the profileless explanation; reviewers receive no staff cards."""
    path = f"/manager/pis/{h.ids['p4_pi_b']}"
    results, logs = {}, {}
    for role in ("manager", "reviewer"):
        ctx, page, log, csp = await _open(h, role)
        try:
            await _goto(h, page, path, csp)
            results[role] = await _body(page)
            logs[role] = _clean(log, csp)
        finally:
            await ctx.close()
    checks = {"manager": "The Edit Profile form appears once the profile exists." in results["manager"]
              and "2016, provisional" in results["manager"] and "Persona revisions" in results["manager"],
              "reviewer": "Edit Profile" not in results["reviewer"] and "Persona revisions" not in results["reviewer"]}
    return {"ok": all(checks.values()) and all(x["clean"] for x in logs.values()),
            "checks": checks, "logs": logs}


async def journey_admin_rename_activation_refusal(h) -> dict:
    """J4-5: a pending lab is renamed but cannot override a missing persona file."""
    agent, slug = h.ids["p4_pi_c_agent"], "p4-refusal-renamed"
    ctx, page, log, csp = await _open(h, "admin")
    try:
        await _goto(h, page, f"/admin/agents/{agent}", csp)
        before = await _body(page)
        await page.fill("#agent-slug", slug)
        await page.locator('input[name="activation_override"]').check()
        await _submit(h, page, page.get_by_role("button", name="Approve & Activate", exact=True), csp)
        text = await _body(page)
        clean = _clean(log, csp)
    finally:
        await ctx.close()
    expected = f"Activation refused: persona file profiles/public/{slug}.md is missing"
    checks = {"hard_blocker": "Cannot be activated (no override)" in before,
              "renamed_refused": f"Renamed to {slug} (saved). {expected}" in text,
              "pending": "Pending" in text and slug in text}
    # Resolve the deliberately failed writer after observing the hard refusal so the
    # package-level scratch persona audit checks only successfully exported fixtures.
    ctx, page, repair_log, repair_csp = await _open(h, "manager")
    try:
        await _goto(h, page, f"/manager/pis/{h.ids['p4_pi_c']}", repair_csp)
        await _submit(h, page, page.locator('form[action$="/persona/reexport"] button'), repair_csp)
        repaired = await _body(page)
        repair_clean = _clean(repair_log, repair_csp)
    finally:
        await ctx.close()
    checks["fixture_resolved"] = "Persona file out of date" not in repaired
    return {"ok": all(checks.values()) and clean["clean"] and repair_clean["clean"],
            "checks": checks, "log": clean, "repair_log": repair_clean}


async def journey_public_profile_save(h) -> dict:
    """J4-6: the PI-facing public-profile save reports a successful export."""
    slug, summary = h.ids["p4_pi_a_slug"], "P4 public profile saved summary."
    ctx, page, log, csp = await _open(h, "p4_pi_a")
    try:
        await _goto(h, page, f"/agent/{slug}/public-profile/edit", csp)
        await page.fill("#public-summary", summary)
        await _submit(h, page, page.locator('#profile-form button[type="submit"]'), csp)
        text = await _body(page)
        clean = _clean(log, csp)
    finally:
        await ctx.close()
    checks = {"flash": "Public profile saved and exported." in text, "profile": summary in text}
    return {"ok": all(checks.values()) and clean["clean"], "checks": checks, "log": clean}


async def journey_deletion_wording(h) -> dict:
    """J4-7: the PI deletion confirmation says model-call-log copies remain."""
    ctx, page, log, csp = await _open(h, "p4_pi_a")
    try:
        await _goto(h, page, "/profile/delete-account", csp)
        text = await _body(page)
        clean = _clean(log, csp)
    finally:
        await ctx.close()
    checks = {"wording": "Copies of your profile text inside the simulation's model-call logs are also kept" in text}
    return {"ok": all(checks.values()) and clean["clean"], "checks": checks, "log": clean}


JOURNEYS = [
    journey_manager_edit,
    journey_stale_and_noop_save,
    journey_caps_and_name_refusals,
    journey_profileless_and_reviewer_isolation,
    journey_admin_rename_activation_refusal,
    journey_public_profile_save,
    journey_deletion_wording,
]
