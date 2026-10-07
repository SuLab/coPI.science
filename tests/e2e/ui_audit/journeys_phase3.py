"""Hub 1.10.0 browser journeys (Task V4 of
docs/plans/2026-10-02-hub-1-10-summary-risks-gates-plan.md; spec
docs/specs/2026-10-02-hub-1-10-summary-risks-gates-design.md §4, §5.2, §6.1, §7.2, §7.4).
Run with ``python -m tests.e2e.ui_audit.run journeys --phase 3``.

The rows they read are seeded by ``seed._seed_phase3`` under the ``p3_*`` ids. Every
journey also fails on a console error, an uncaught page error or an ENFORCED CSP
violation on any page it loads. ``journey_companies_card`` writes (it confirms the seeded
suggestion), so it runs last: ``journey_key_point_labels_and_companies`` expects that
suggestion still unconfirmed.
"""

from __future__ import annotations

import re
import time
import tomllib

from tests.e2e.ui_audit.env import REPO_ROOT
from tests.e2e.ui_audit.seed import (
    P3_COMPANIES_BODY,
    P3_CONFIRMED_COMPANY,
    P3_GATING_RATIONALES,
    P3_REJECTED_COMPANY,
    P3_RISK_BODY,
    P3_SUGGESTED_COMPANY,
)

_CSP_COLLECTOR_JS = (
    "window.__cspViolations = [];\n"
    "document.addEventListener('securitypolicyviolation', (e) => {\n"
    "  window.__cspViolations.push({directive: e.effectiveDirective, blocked: e.blockedURI,\n"
    "    source: e.sourceFile, line: e.lineNumber, disposition: e.disposition});\n"
    "});\n"
)


def _gate_titles() -> dict[str, str]:
    """Gate key -> rubric title, from the live rubric document the app renders."""
    doc = tomllib.loads((REPO_ROOT / "prompts/rubric/blackbird-rubric.toml").read_text())
    return {key: meta["title"] for key, meta in doc["gating"].items()}


async def _open(h, role: str, *, accept_dialogs: bool = False):
    """A page as `role` with the CSP collector installed. With `accept_dialogs`, every
    dialog (a `data-confirm` form) is accepted and its message recorded in
    ``log["dialogs"]``; otherwise the harness dismisses them."""
    ctx, page, log = await h.page(role, answer_dialogs=not accept_dialogs)
    await ctx.add_init_script(_CSP_COLLECTOR_JS)
    if accept_dialogs:
        async def _accept(d):
            log["dialogs"].append(f"{d.type}: {d.message[:160]}")
            await d.accept()

        page.on("dialog", _accept)
    return ctx, page, log, []


async def _drain_csp(h, page, sink: list) -> None:
    """Move this document's CSP violations into `sink` (call after every load)."""
    for v in await page.evaluate("() => window.__cspViolations || []"):
        sink.append({"path": page.url.replace(h.base_url, ""), **v})


async def _goto(h, page, path: str, csp: list) -> None:
    await page.goto(h.base_url + path, wait_until="networkidle")
    await _drain_csp(h, page, csp)


async def _submit(h, page, locator, csp: list) -> None:
    """Click a submit control and wait for the POST/redirect/GET to settle."""
    async with page.expect_navigation(wait_until="networkidle"):
        await locator.click()
    await _drain_csp(h, page, csp)


def _clean(log: dict, csp: list) -> dict:
    """The console/CSP part of a journey's verdict: no console error, no page error,
    no enforced CSP violation (report-only ones are reported, not failed)."""
    enforced = [v for v in csp if v.get("disposition") != "report"]
    return {
        "clean": not log["console"] and not log["pageerror"] and not enforced,
        "console_errors": log["console"],
        "page_errors": log["pageerror"],
        "csp_violations": csp,
    }


async def _flash(page) -> str:
    region = page.locator("[data-flash-region]")
    return (await region.inner_text()).strip() if await region.count() else ""


# --------------------------------------------------------------------------- 1. nav

_NAV_STATE_JS = """() => {
  const links = (nav) => nav ? [...nav.querySelectorAll('a')].map((a) => ({
      text: a.textContent.trim(),
      highlighted: a.classList.contains('text-indigo-600') && a.classList.contains('font-semibold')}))
    : null;
  const topWorkspace = [...document.querySelectorAll('nav[aria-label="Main"] a')]
    .find((a) => a.textContent.trim() === 'Workspace');
  return {
    admin: links(document.querySelector('nav[aria-label="Admin sections"]')),
    workspace: links(document.querySelector('nav[aria-label="Workspace sections"]')),
    top_workspace_highlighted: topWorkspace ? topWorkspace.classList.contains('text-indigo-600') : null,
  };
}"""


def _admin_nav_ok(state: dict) -> bool:
    workspace = state["workspace"]
    return (
        workspace is not None
        and [a["text"] for a in workspace if a["highlighted"]] == ["Prompt Suggestions"]
        and state["top_workspace_highlighted"] is True
    )


def _manager_nav_ok(state: dict) -> bool:
    return _admin_nav_ok(state)


async def journey_admin_nav_round_trip(h) -> dict:
    """An admin and manager retain the shared Workspace navigation on prompt pages."""
    detail_path = f"/workspace/prompt-suggestions/{h.ids['p3_prompt_suggestion']}"
    states: dict[str, dict] = {}
    ctx, page, log, csp = await _open(h, "admin")
    try:
        await _goto(h, page, "/admin/users", csp)
        link = page.locator('nav[aria-label="Main"]').get_by_role(
            "link", name="Workspace", exact=True)
        await _submit(h, page, link, csp)
        await _submit(h, page, page.locator(
            'nav[aria-label="Workspace sections"]').get_by_role(
                "link", name="Prompt Suggestions", exact=True), csp)
        states["admin list"] = {"path": page.url.replace(h.base_url, ""),
                                **await page.evaluate(_NAV_STATE_JS)}
        await _submit(h, page, page.locator(f'main a[href="{detail_path}"]').first, csp)
        states["admin detail"] = {"path": page.url.replace(h.base_url, ""),
                                  **await page.evaluate(_NAV_STATE_JS)}
        admin_clean = _clean(log, csp)
    finally:
        await ctx.close()
    ctx, page, log, csp = await _open(h, "manager")
    try:
        for name, path in (("manager list", "/workspace/prompt-suggestions"),
                           ("manager detail", detail_path)):
            await _goto(h, page, path, csp)
            states[name] = {"path": page.url.replace(h.base_url, ""),
                            **await page.evaluate(_NAV_STATE_JS)}
        manager_clean = _clean(log, csp)
    finally:
        await ctx.close()
    ok = (
        states["admin list"]["path"] == "/workspace/prompt-suggestions"
        and states["admin detail"]["path"] == detail_path
        and _admin_nav_ok(states["admin list"]) and _admin_nav_ok(states["admin detail"])
        and _manager_nav_ok(states["manager list"]) and _manager_nav_ok(states["manager detail"])
        and admin_clean["clean"] and manager_clean["clean"]
    )
    return {"ok": ok, "states": states, "admin_log": admin_clean, "manager_log": manager_clean}


# --------------------------------------------------------------------------- 2. gates

_SIGNAL_GATES_JS = """() => [...document.querySelectorAll('#signals li.signal-source-gating')]
  .map((li) => {
    const second = li.querySelector(':scope > .signal-rationale');
    const label = li.querySelector('span.font-semibold');
    const box = second ? second.getBoundingClientRect() : null;
    return {
      label: label ? label.textContent.trim() : null,
      second: second ? second.textContent.trim() : null,
      second_visible: !!(second && box.width > 0 && box.height > 0
                         && getComputedStyle(second).visibility !== 'hidden'),
      is_definition: !!(second && second.classList.contains('signal-gate-definition')),
      // The kind glyph (role=img, "Strength"/"Risk"/"Not established") keeps its title;
      // the gate's own tooltip (the description) is what spec §5.2 removed.
      titles: [li, ...li.querySelectorAll('[title]')]
        .filter((e) => e.hasAttribute('title') && e.getAttribute('role') !== 'img')
        .map((e) => e.getAttribute('title')),
      info_glyph: li.textContent.includes('\\u24d8'),
    };
  })"""

_CARD_GATES_JS = """(id) => {
  const card = document.getElementById('a-' + id);
  if (!card) return null;
  return [...card.querySelectorAll('details.assessment-card-scores span.gating-row')].map((row) => {
    const lines = [...row.querySelectorAll(':scope > .gating-reason, :scope > .gating-definition')];
    return {
      text: row.textContent.trim(),
      lines: lines.map((l) => ({kind: l.classList.contains('gating-reason') ? 'reason' : 'definition',
                                text: l.textContent.trim(),
                                visible: l.getBoundingClientRect().height > 0})),
    };
  });
}"""


def _detail_gates_ok(rows: list[dict], titles: dict[str, str], *, with_reasons: bool) -> bool:
    if sorted(r["label"] or "" for r in rows) != sorted(titles.values()):
        return False
    by_title = {title: key for key, title in titles.items()}
    for r in rows:
        if not r["second_visible"] or r["titles"] or r["info_glyph"]:
            return False
        if with_reasons:
            if r["is_definition"] or r["second"] != (
                "Hub's reason: " + P3_GATING_RATIONALES[by_title[r["label"]]]
            ):
                return False
        elif not (r["is_definition"] and r["second"].startswith("Rubric definition:")):
            return False
    return True


def _card_gates_ok(rows: list[dict] | None, titles: dict[str, str], *,
                   with_reasons: bool) -> bool:
    if not rows or len(rows) != len(titles):
        return False
    for r in rows:
        if len(r["lines"]) != 1 or not r["lines"][0]["visible"]:
            return False
        if not any(title in r["text"] for title in titles.values()):
            return False
        line = r["lines"][0]
        if with_reasons:
            if line["kind"] != "reason" or line["text"] not in P3_GATING_RATIONALES.values():
                return False
        elif line["kind"] != "definition" or not line["text"].startswith("Rubric definition:"):
            return False
    return True


async def journey_gate_lines(h) -> dict:
    """Spec §5.2: on both seeded detail pages every gate row of the Evidence summary
    (#signals) has a visible second line, "Hub's reason: …" on the row with
    `gating_rationales` and "Rubric definition: …" on the row without, labelled with
    the rubric title and with no title tooltip; on the list card each gate is its own
    line carrying one reason or definition."""
    titles = _gate_titles()
    gated, ungated = h.ids["p3_assessment_gated"], h.ids["p3_assessment_ungated"]
    ctx, page, log, csp = await _open(h, "admin")
    try:
        detail = {}
        for name, aid in (("gated", gated), ("ungated", ungated)):
            await _goto(h, page, f"/workspace/assessments/{aid}", csp)
            detail[name] = await page.evaluate(_SIGNAL_GATES_JS)
        await _goto(h, page, f"/workspace/assessments?run_id={h.ids['run']}", csp)
        card = {}
        for name, aid in (("gated", gated), ("ungated", ungated)):
            summary = page.locator(f'[id="a-{aid}"] details.assessment-card-scores > summary')
            if await summary.count():
                await summary.click()
            card[name] = await page.evaluate(_CARD_GATES_JS, aid)
        await _drain_csp(h, page, csp)
        clean = _clean(log, csp)
    finally:
        await ctx.close()
    ok = (
        _detail_gates_ok(detail["gated"], titles, with_reasons=True)
        and _detail_gates_ok(detail["ungated"], titles, with_reasons=False)
        and _card_gates_ok(card["gated"], titles, with_reasons=True)
        and _card_gates_ok(card["ungated"], titles, with_reasons=False)
        and clean["clean"]
    )
    return {"ok": ok, "detail": detail, "card": card, "log": clean}


# --------------------------------------------------------------------------- 3. key points

_BRIEF_JS = """() => {
  const box = document.querySelector('#brief .assessment-brief-points');
  const sections = box ? [...box.children].filter((d) => d.tagName === 'DIV'
      && !d.classList.contains('assessment-brief-companies')) : [];
  const section = (label) => sections.find((d) => {
    const head = d.querySelector(':scope > span.font-semibold');
    return head && head.textContent.trim() === label;
  });
  const extra = (d, kind) => {
    const p = d && d.querySelector('p.key-point-extra.key-point-' + kind);
    if (!p) return null;
    const lab = p.querySelector('span.font-semibold');
    return {label: lab ? lab.textContent.trim() : null,
            weight: lab ? parseInt(getComputedStyle(lab).fontWeight, 10) : 0,
            text: p.textContent.trim()};
  };
  const lab = section('Lab Background'), com = section('Commercial Opportunity');
  const blocks = [...document.querySelectorAll('#brief .assessment-brief-companies')];
  const block = blocks[0];
  return {
    risk: extra(com, 'risk'),
    companies: extra(lab, 'companies'),
    stray_companies_extra: sections.filter((d) => d !== lab
        && d.querySelector('p.key-point-companies')).length,
    block_count: blocks.length,
    block: block ? {
      heading: ((block.querySelector('span.font-semibold') || {}).textContent || '').trim(),
      entries: [...block.querySelectorAll('li.assessment-brief-company')]
        .map((li) => li.textContent.trim()),
      under_lab_background: !!lab && block.previousElementSibling === lab,
    } : null,
    brief_text: (document.getElementById('brief') || {}).textContent || '',
  };
}"""

_AS_OF = re.compile(r"Companies \(staff-confirmed, as of \d{4}-\d{2}-\d{2}\)")


def _brief_ok(b: dict) -> bool:
    risk, comp, block = b["risk"], b["companies"], b["block"]
    return (
        risk is not None and risk["label"] == "Risk:" and risk["weight"] >= 600
        and P3_RISK_BODY in risk["text"]
        and comp is not None and comp["label"] == "Companies:" and comp["weight"] >= 600
        and P3_COMPANIES_BODY in comp["text"]
        and b["stray_companies_extra"] == 0
        and b["block_count"] == 1 and block is not None
        and _AS_OF.fullmatch(block["heading"] or "") is not None
        and len(block["entries"]) == 1 and P3_CONFIRMED_COMPANY in block["entries"][0]
        and block["under_lab_background"]
        and P3_SUGGESTED_COMPANY not in b["brief_text"]
    )


async def journey_key_point_labels_and_companies(h) -> dict:
    """Spec §6.1 Rendering and §7.4: the seeded 1.10.0 row's brief shows a bold "Risk:"
    line under Commercial Opportunity and a bold "Companies:" line under Lab
    Background, and directly under Lab Background the "Companies (staff-confirmed, as
    of …)" block with the confirmed company only (the suggested one appears nowhere in
    the brief). Checked on the admin and the manager detail page."""
    aid = h.ids["p3_assessment_gated"]
    results: dict[str, dict] = {}
    logs: dict[str, dict] = {}
    for role, path in (("admin", f"/workspace/assessments/{aid}"),
                       ("manager", f"/workspace/assessments/{aid}")):
        ctx, page, log, csp = await _open(h, role)
        try:
            await _goto(h, page, path, csp)
            brief = await page.evaluate(_BRIEF_JS)
            brief_ok = _brief_ok(brief)
            brief.pop("brief_text")  # checked by _brief_ok, too long to report
            results[role] = {"ok": brief_ok, **brief}
            logs[role] = _clean(log, csp)
        finally:
            await ctx.close()
    ok = all(r["ok"] for r in results.values()) and all(lg["clean"] for lg in logs.values())
    return {"ok": ok, "pages": results, "logs": logs}


# --------------------------------------------------------------------------- 4. companies

_CARD_STATE_JS = """() => {
  const card = document.getElementById('companies');
  if (!card) return null;
  const btn = document.getElementById('find-companies');
  const jobsHead = [...document.querySelectorAll('h2')].find((e) => e.textContent.trim() === 'Jobs');
  const jobsCard = jobsHead ? jobsHead.closest('div') : null;
  return {
    confirmed: [...card.querySelectorAll(':scope > ul > li span.font-medium')]
      .map((e) => e.textContent.trim()),
    suggested: [...card.querySelectorAll(':scope > div.rounded-lg > p > span.font-medium')]
      .map((e) => e.textContent.trim()),
    card_text: card.textContent,
    find_present: !!btn,
    find_disabled: btn ? btn.disabled : null,
    discovery_jobs: jobsCard ? [...jobsCard.querySelectorAll('tbody > tr > td:first-child')]
      .filter((td) => td.textContent.trim() === 'Company Discovery').length : 0,
  };
}"""


async def journey_companies_card(h) -> dict:
    """Spec §7.2, as a manager on /workspace/pis/{id}: a manual add appears under
    Confirmed; confirming the seeded suggestion moves it to Confirmed; a second manual
    add removed again (Delete, accepting its data-confirm dialog, the only removal a
    confirmed entry has) is gone from the card; Find companies leaves the button
    disabled with exactly one Company Discovery job row, and a second POST to the
    discover route adds none."""
    pi = h.ids["p3_pi"]
    path = f"/workspace/pis/{pi}"
    stamp = time.time_ns() % 10**8
    manual, removed = f"Manual Add Co {stamp}", f"Short Lived Co {stamp}"
    steps: dict[str, dict] = {}
    ctx, page, log, csp = await _open(h, "manager", accept_dialogs=True)
    try:
        await _goto(h, page, path, csp)
        steps["initial"] = await page.evaluate(_CARD_STATE_JS)

        add_form = page.locator(f'form[action="/workspace/pis/{pi}/companies"]')

        async def add(name: str) -> None:
            await page.fill("#company-name", name)
            await page.select_option("#company-role", "advisor")
            await page.fill("#company-source", "https://example.org/" + name.replace(" ", "-"))
            await _submit(h, page, add_form.locator("button[type=submit]"), csp)

        await add(manual)
        steps["added"] = {**await page.evaluate(_CARD_STATE_JS), "flash": await _flash(page)}

        confirm = page.locator(
            f'form[action="/workspace/pis/{pi}/companies/{h.ids["p3_company_suggested"]}/confirm"]'
            " button[type=submit]")
        await _submit(h, page, confirm, csp)
        steps["confirmed"] = {**await page.evaluate(_CARD_STATE_JS), "flash": await _flash(page)}

        reject = page.locator(
            f'form[action="/workspace/pis/{pi}/companies/{h.ids["p3_company_to_reject"]}/reject"]'
            " button[type=submit]")
        await _submit(h, page, reject, csp)
        steps["rejected"] = {**await page.evaluate(_CARD_STATE_JS), "flash": await _flash(page)}

        await add(removed)
        steps["added_second"] = await page.evaluate(_CARD_STATE_JS)
        delete = page.locator("#companies > ul > li").filter(has_text=removed).locator(
            "form[data-confirm] button[type=submit]")
        await _submit(h, page, delete, csp)
        steps["removed"] = {**await page.evaluate(_CARD_STATE_JS), "flash": await _flash(page)}

        await _submit(h, page, page.locator("#find-companies"), csp)
        steps["discovery"] = {**await page.evaluate(_CARD_STATE_JS), "flash": await _flash(page)}
        again = await page.request.post(h.base_url + f"/workspace/pis/{pi}/companies/discover",
                                        headers={"Origin": h.base_url})
        await _goto(h, page, path, csp)
        steps["discovery_again"] = {**await page.evaluate(_CARD_STATE_JS),
                                    "second_post_status": again.status}
        clean = _clean(log, csp)
    finally:
        await ctx.close()

    s = steps
    checks = {
        "initial": s["initial"] is not None
        and s["initial"]["confirmed"] == [P3_CONFIRMED_COMPANY]
        and sorted(s["initial"]["suggested"]) == sorted([P3_SUGGESTED_COMPANY, P3_REJECTED_COMPANY])
        and s["initial"]["find_disabled"] is False and s["initial"]["discovery_jobs"] == 0,
        "manual_add_confirmed": manual in s["added"]["confirmed"]
        and manual not in s["added"]["suggested"],
        "suggestion_moved": P3_SUGGESTED_COMPANY in s["confirmed"]["confirmed"]
        and s["confirmed"]["suggested"] == [P3_REJECTED_COMPANY],
        "rejected_gone": s["rejected"]["suggested"] == []
        and P3_REJECTED_COMPANY not in s["rejected"]["confirmed"]
        and P3_REJECTED_COMPANY not in s["rejected"]["card_text"],
        "removed_gone": removed in s["added_second"]["confirmed"]
        and removed not in s["removed"]["card_text"]
        and manual in s["removed"]["confirmed"]
        and len(log["dialogs"]) == 1,
        "discovery_disabled_one_job": s["discovery"]["find_disabled"] is True
        and s["discovery"]["discovery_jobs"] == 1
        and s["discovery_again"]["find_disabled"] is True
        and s["discovery_again"]["discovery_jobs"] == 1,
    }
    for state in s.values():
        state.pop("card_text", None)
    ok = all(checks.values()) and clean["clean"]
    return {"ok": ok, "checks": checks, "steps": steps, "dialogs": log["dialogs"], "log": clean}


async def journey_corpus_and_draft_review(h) -> dict:
    """Staff can resolve all seeded review cards, while a reviewer sees none of them."""
    pi = h.ids["p3_pi"]
    path = f"/workspace/pis/{pi}"
    steps: dict[str, str] = {}
    ctx, page, log, csp = await _open(h, "manager")
    try:
        await _goto(h, page, path, csp)
        steps["initial"] = await page.locator("body").inner_text()
        await _submit(h, page, page.locator(
            f'form[action="/workspace/pis/{pi}/persona/reexport"] button'), csp)
        steps["reexported"] = await page.locator("body").inner_text()
        await _submit(h, page, page.locator(
            f'form[action="/workspace/pis/{pi}/candidates/{h.ids["p3_candidate"]}/accept"] button'), csp)
        steps["accepted"] = await page.locator("body").inner_text()
        await _submit(h, page, page.locator(
            f'form[action="/workspace/pis/{pi}/publications/{h.ids["p3_unanchored"]}/exclude"] button'), csp)
        steps["excluded"] = await page.locator("body").inner_text()
        await _submit(h, page, page.locator(
            f'form[action="/workspace/pis/{pi}/draft/discard"] button'), csp)
        steps["discarded"] = await page.locator("body").inner_text()
        manager_clean = _clean(log, csp)
    finally:
        await ctx.close()
    ctx, page, log, csp = await _open(h, "reviewer")
    try:
        await _goto(h, page, path, csp)
        reviewer_text = await page.locator("body").inner_text()
        reviewer_clean = _clean(log, csp)
    finally:
        await ctx.close()
    checks = {
        "initial_cards": all(text in steps["initial"] for text in (
            "P3 candidate paper", "P3 unanchored paper", h.ids["phase5_private"]["draft"],
            "Persona file out of date")),
        "candidate_accepted": "Paper added to the corpus." in steps["accepted"]
        and "P3 candidate paper" not in steps["accepted"],
        "unanchored_excluded": "Paper excluded." in steps["excluded"]
        and "P3 unanchored paper" not in steps["excluded"],
        "draft_discarded": "Draft discarded." in steps["discarded"]
        and "Pending draft" not in steps["discarded"],
        "reexported": "Persona file out of date" not in steps["reexported"],
        "reviewer_isolated": all(text not in reviewer_text for text in (
            "Corpus and profile review", "P3 candidate paper", "P3 unanchored paper", "Pending draft")),
    }
    ok = all(checks.values()) and manager_clean["clean"] and reviewer_clean["clean"]
    return {"ok": ok, "checks": checks, "manager_log": manager_clean,
            "reviewer_log": reviewer_clean}


JOURNEYS = [
    journey_admin_nav_round_trip,
    journey_gate_lines,
    journey_key_point_labels_and_companies,
    journey_companies_card,
    journey_corpus_and_draft_review,
]
