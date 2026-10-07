"""Canonical workspace UI consolidation acceptance journeys."""

from __future__ import annotations

from contextlib import contextmanager

from tests.e2e.ui_audit.journeys_phase1_1b import _seed
from tests.e2e.ui_audit.journeys_phase3 import _clean, _drain_csp, _open


async def _goto(h, page, path, csp):
    # The open chat polls generated questions while they are queued. Network-idle
    # is not a terminal state. Finish its initial history load and render before
    # leaving the page, so navigation cannot cancel the operation under test.
    if path.endswith('#chat'):
        assessment_id = path.split('?', 1)[0].split('#', 1)[0].rsplit('/', 1)[1]
        history_url = f'{h.base_url}/assessment-chat/{assessment_id}'
        async with page.expect_response(lambda response: response.url == history_url
                                        and response.request.method == 'GET') as history:
            await page.goto(h.base_url + path, wait_until='load')
        response = await history.value
        assert response.status == 200
        await response.body()
        await page.locator('#assessment-chat:not(.hidden)').wait_for(state='visible')
        await page.wait_for_function("() => document.querySelector('[data-chat-usage]')"
                                     ".textContent.includes('questions used today')")
    else:
        await page.goto(h.base_url + path, wait_until='load')
    await _drain_csp(h, page, csp)


def _healthy(log: dict, csp: list) -> dict:
    result = _clean(log, csp)
    result["clean"] = result["clean"] and not log["console"] and not log["requestfailed"] and not log["unexpected_network"]
    result["requestfailed"] = log["requestfailed"]
    result["unexpected_network"] = log["unexpected_network"]
    return result


async def _card_ids(page) -> list[str]:
    return await page.locator(".assessment-card").evaluate_all(
        "(cards) => cards.map((card) => card.id.replace(/^a-/, ''))")


async def journey_admin_workspace_operations(h) -> dict:
    """Admin reaches every former manager surface and sees a live control."""
    expected = {
        "/workspace/pis": 'form[action="/workspace/pis"]',
        "/workspace/assessments": "#assessments-assignment-select",
        "/workspace/slack-bots": "table",
        "/workspace/discussions": 'button[name="export"][value="html"]',
        "/workspace/activity": "table",
        "/workspace/prompt-suggestions": 'form[action="/reviews/suggestions/generate"]',
    }
    ctx, page, log, csp = await _open(h, "admin")
    try:
        checks = {}
        for path, control in expected.items():
            await _goto(h, page, path, csp)
            checks[path] = (page.url.replace(h.base_url, "").split("?")[0] == path
                            and await page.locator(control).count() > 0)
        clean = _healthy(log, csp)
    finally:
        await ctx.close()
    return {"ok": all(checks.values()) and clean["clean"], "checks": checks, "log": clean}


async def journey_reviewer_pi_privacy(h) -> dict:
    """Reviewer research HTML has positive sentinels and excludes private DTO values."""
    private = h.ids["phase5_private"]
    ctx, page, log, csp = await _open(h, "reviewer")
    try:
        await _goto(h, page, f"/workspace/pis/{private['pi']}", csp)
        html, text = await page.content(), await page.locator("body").inner_text()
        await page.emulate_media(media='print')
        print_html = await page.content()
        await page.emulate_media(media='screen')
        controls = await page.locator("main form button").all_text_contents()
        clean = _healthy(log, csp)
    finally:
        await ctx.close()
    absent = (private["email"], private["job"], private["draft"], private["suggestion"],
              "Verify email", "Edit Profile", "Persona revisions", "PHASE5 COVERAGE DIAGNOSTIC",
              "scorer v", "industry job", ">partial</span>", "PHASE5 COMPONENT DIAGNOSTIC",
              "set a tenure year")
    checks = {
        "research_positive": "Helix Bio Inc" in text and "P3 stored summary." in text,
        "industry_evidence_and_scores": all(value in html for value in
                                             ("PHASE5 RESEARCH PATENT", "Raw score 13.5", "100.0")),
        "private_absent_from_html": all(value not in html for value in absent),
        "private_absent_from_print": all(value not in print_html for value in absent),
        "staff_controls_absent": not controls,
    }
    return {"ok": all(checks.values()) and clean["clean"], "checks": checks, "log": clean}


@contextmanager
def _assignment_warning_fixture(h):
    """Exercise run-wide warnings outside mine without changing other journeys."""
    assignment = h.ids['phase5_assignments']
    fixture = _seed(h, f'''
from uuid import UUID
from src.models import AssessmentDrop, OpportunityAssessment
assert get_engine().url.database == "copi_uiaudit"
row = await s.get(OpportunityAssessment, UUID({assignment['unassigned']!r}))
old_owed = row.panel_owed
row.panel_owed = None
drop = AssessmentDrop(simulation_run_id=UUID({h.ids['run']!r}), agent_id="blackbird",
                      subject_agent_id="unassigned-lab", reason="missing_sidecar")
s.add(drop)
await s.flush()
return {{"drop_id": str(drop.id), "old_owed": old_owed}}
''')
    try:
        yield
    finally:
        _seed(h, f'''
from uuid import UUID
from src.models import AssessmentDrop, OpportunityAssessment
from sqlalchemy import delete
assert get_engine().url.database == "copi_uiaudit"
row = await s.get(OpportunityAssessment, UUID({assignment['unassigned']!r}))
row.panel_owed = {fixture['old_owed']!r}
await s.execute(delete(AssessmentDrop).where(AssessmentDrop.id == UUID({fixture['drop_id']!r})))
return {{}}
''')


async def journey_assignment_filter_and_global_detail(h) -> dict:
    """All/mine sets are exact; an unassigned reviewer still opens detail and chat."""
    assignment = h.ids["phase5_assignments"]
    with _assignment_warning_fixture(h):
        ctx, page, log, csp = await _open(h, "reviewer")
        try:
            await _goto(h, page, "/workspace/assessments?assignment=all", csp)
            all_cards = await _card_ids(page)
            await page.set_viewport_size({'width': 375, 'height': 900})
            await _goto(h, page, "/workspace/assessments?assignment=mine", csp)
            mine_cards = await _card_ids(page)
            html = await page.content()
            text = ' '.join((await page.locator('main').inner_text()).split())
            run_label = await page.locator('#assessments-run-select option:checked').inner_text()
            scope_labels = (f"{len(assignment['all'])} stored across all labs and assignments" in run_label
                            and 'Tab counts follow the selected run, lab and assignment filters.' in html
                            and 'across all labs and assignments.' in html)
            warning_counts = ('1 verdict lost' in text and
                              '1 verdict with an incomplete, unverified or unrecorded specialist panel' in text)
            narrow_viewport = await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            run_private_absent = all(value not in html for value in
                                     ('PHASE5 PRIVATE RUN CONFIG', 'PHASE5 PRIVATE ANNOUNCEMENT'))
            await _goto(h, page, f"/workspace/assessments/{assignment['unassigned']}?assignment=mine#chat", csp)
            unassigned = (await page.locator("#review").count() == 1
                          and await page.locator("[data-chat-bubble]").count() == 1)
            clean = _healthy(log, csp)
        finally:
            await ctx.close()
    checks = {
        "all_exact": set(all_cards) == set(assignment["all"]) and bool(all_cards),
        "mine_exact": set(mine_cards) == set(assignment["mine"]) and bool(mine_cards),
        "population_scope_labels": scope_labels,
        "run_wide_warning_counts": warning_counts,
        "narrow_scope_viewport": narrow_viewport,
        "private_run_configuration_absent": run_private_absent,
        "unassigned_global_detail_chat": unassigned,
    }
    return {"ok": all(checks.values()) and clean["clean"], "checks": checks, "log": clean}


async def journey_feedback_return_state_and_next_card(h) -> dict:
    """A canonical feedback POST retains filters and lands on the next card."""
    assignment = h.ids["phase5_assignments"]
    first, next_card = assignment["mine"][0], assignment["next_after_feedback"]
    query = f"?run_id={h.ids['run']}&sort=score&lab=&review=unreviewed&assignment=mine"
    ctx, page, log, csp = await _open(h, "reviewer")
    try:
        await _goto(h, page, "/workspace/assessments" + query, csp)
        form = page.locator(f'form[action="/reviews/assessments/{first}/feedback"]')
        await form.locator('select[name="score"]').select_option("5")
        await form.locator('textarea[name="comment"]').fill("PHASE5 canonical feedback")
        await form.locator('select[name="feedback_mode"]').select_option("learn")
        async with page.expect_navigation(wait_until="networkidle"):
            await form.get_by_role("button", name="Submit feedback").click()
        final = page.url.replace(h.base_url, "")
        next_visible = await page.locator(f"#a-{next_card}").is_visible()
        await _goto(h, page, f"/workspace/assessments/{next_card}" + query + "#review", csp)
        back = await page.locator('main a[href^="/workspace/assessments?"]').first.get_attribute("href")
        clean = _healthy(log, csp)
    finally:
        await ctx.close()
    checks = {
        "filtered_location": "assignment=mine" in final and "review=unreviewed" in final,
        "next_surviving_card": final.endswith(f"#a-{next_card}") and next_visible,
        "detail_back_state": back is not None and all(token in back for token in
                              ("run_id=", "sort=", "review=unreviewed", "assignment=mine")),
    }
    return {"ok": all(checks.values()) and clean["clean"], "checks": checks, "log": clean}


async def journey_discussion_alias_queries_and_export_gate(h) -> dict:
    """Aliases preserve repeated filters; export has exact role/content contracts."""
    query = "?run_id=all&agent_filter=obrien&agent_filter=vogelstein"
    outcomes = {}
    for role, path in (("manager", "/manager/discussions" + query), ("admin", "/admin/discussions" + query)):
        ctx, page, log, csp = await _open(h, role)
        try:
            await _goto(h, page, path, csp)
            outcomes[role] = {"path": page.url.replace(h.base_url, ""), "log": _healthy(log, csp)}
        finally:
            await ctx.close()
    ctx, page, _log, _csp = await _open(h, "admin")
    manager_ctx, manager_page, _manager_log, _manager_csp = await _open(h, "manager")
    try:
        text_export = await page.request.get(h.base_url + "/workspace/discussions?run_id=all&export=true")
        html_export = await page.request.get(h.base_url + "/workspace/discussions?run_id=all&export=html")
        denied = await manager_page.request.get(h.base_url + "/workspace/discussions?run_id=all&export=html")
        exports = {
            "manager_403": denied.status == 403,
            "text_attachment": text_export.status == 200 and "attachment" in text_export.headers.get("content-disposition", ""),
            "html_attachment": html_export.status == 200 and "attachment" in html_export.headers.get("content-disposition", ""),
            "proposals": "proposal" in (await text_export.text()).lower() and "<html" in (await html_export.text()).lower(),
        }
    finally:
        await ctx.close()
        await manager_ctx.close()
    checks = {
        "manager_alias": outcomes["manager"]["path"].startswith("/workspace/discussions?")
                         and outcomes["manager"]["path"].count("agent_filter=") == 2,
        "admin_alias": outcomes["admin"]["path"].startswith("/workspace/discussions?")
                       and outcomes["admin"]["path"].count("agent_filter=") == 2,
        **exports,
    }
    clean = all(result["log"]["clean"] for result in outcomes.values())
    return {"ok": all(checks.values()) and clean, "checks": checks, "outcomes": outcomes}


async def journey_fragment_targets_and_retained_admin(h) -> dict:
    """Each fragment identifies a visible element and causes an in-viewport scroll."""
    private, assessment = h.ids["phase5_private"], h.ids["assessments"][0]
    targets = (
        (f"/workspace/pis/{private['pi']}#account", "#account"),
        (f"/workspace/pis/{private['pi']}#grants", "#grants"),
        (f"/workspace/pis/{private['pi']}#industry", "#industry"),
        (f"/workspace/pis/{private['pi']}#companies", "#companies"),
        (f"/workspace/assessments/{assessment}#review", "#review"),
        (f"/workspace/assessments/{assessment}#chat", "#chat"),
    )
    ctx, page, log, csp = await _open(h, "admin")
    try:
        checks = {}
        for path, selector in targets:
            await _goto(h, page, path, csp)
            checks[selector] = await page.locator(selector).evaluate(
                "(node) => { const box = node.getBoundingClientRect(); return box.width > 0 && box.height > 0 && box.top >= 0 && box.top < innerHeight; }")
        await _goto(h, page, f"/admin/users/{h.ids['script_user']}", csp)
        retained = (page.url.replace(h.base_url, '') == f"/admin/users/{h.ids['script_user']}"
                    and "Account" in await page.locator("body").inner_text())
        await _goto(h, page, "/admin/agents", csp)
        agents = "Agents" in await page.locator("body").inner_text()
        clean = _healthy(log, csp)
    finally:
        await ctx.close()
    checks.update({"non_pi_admin": retained, "all_agents": agents})
    return {"ok": all(checks.values()) and clean["clean"], "checks": checks, "log": clean}


async def journey_impersonation_navigation_and_stop(h) -> dict:
    """Seeded effective identities lose unavailable controls and always retain Stop."""
    checks, logs = {}, {}
    for role, target in h.ids["phase5_impersonation"].items():
        ctx, page, log, csp = await _open(h, "admin")
        try:
            await _goto(h, page, "/admin/users", csp)
            await page.fill("form[action='/admin/impersonate'] input[name=orcid]", target["orcid"])
            await page.click("form[action='/admin/impersonate'] button[type=submit]")
            await page.wait_for_load_state("networkidle")
            await _goto(h, page, target["destination"], csp)
            body = await page.locator("body").inner_text()
            stop = await page.locator("form[action='/admin/impersonate/stop']").count() == 1
            forbidden = role in ("reviewer", "pi") and "Prompt Suggestions" not in body and "Add a PI" not in body
            await page.locator("form[action='/admin/impersonate/stop'] button[type=submit]").click()
            await page.wait_for_load_state("networkidle")
            checks[role] = stop and target["name"] in body and (forbidden if role in ("reviewer", "pi") else True)
            logs[role] = _healthy(log, csp)
        finally:
            await ctx.close()
    return {"ok": all(checks.values()) and all(log["clean"] for log in logs.values()),
            "checks": checks, "logs": logs}


JOURNEYS = [
    journey_admin_workspace_operations,
    journey_reviewer_pi_privacy,
    journey_assignment_filter_and_global_detail,
    journey_feedback_return_state_and_next_card,
    journey_discussion_alias_queries_and_export_gate,
    journey_fragment_targets_and_retained_admin,
    journey_impersonation_navigation_and_stop,
]
