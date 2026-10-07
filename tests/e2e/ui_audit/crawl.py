"""Role x route crawl. Invariants (any violation fails the run):

* no 5xx on any page;
* the XSS canary ``window.__xss`` stays 0 on every HTML page;
* no uncaught page error;
* an anonymous visitor never gets a 200 on a non-public route without being
  redirected to /login;
* the role gates in EXPECTED hold (a role outside a prefix's allowed set never gets a
  200 there);
* no console error from an ENFORCED Content-Security-Policy (report-only messages,
  which Chromium prefixes "[Report Only]", are reported, not failed).

Overflow and axe findings (1280 px, five roles) include page-level evidence;
the consolidation phase fails on accessibility findings as well as regressions.
"""

from __future__ import annotations

import asyncio

ROLES = ("anon", "admin", "manager", "reviewer", "pi", "pi2", "delegate", "pending")
PUBLIC = ("/", "/login", "/access-pending", "/invite/")
AXE_ROLES = ("anon", "admin", "manager", "reviewer", "pi")

#: Authorization gates the 2026-10-01 audit observed and the remediation keeps:
#: (path prefix, roles that may get a 200 there). First matching prefix wins.
EXPECTED: tuple[tuple[str, frozenset[str]], ...] = (
    ("/admin/", frozenset({"admin"})),
    ("/workspace/pis", frozenset({"admin", "manager", "reviewer"})),
    ("/workspace/assessments", frozenset({"admin", "manager", "reviewer"})),
    ("/workspace/", frozenset({"admin", "manager"})),
    ("/agent/obrien/", frozenset({"pi", "delegate"})),
)


def routes(ids: dict) -> list[str]:
    a1, a2, a3 = ids["assessments"]
    run, bad = ids["run"], ids["bad_uuid"]
    return [
        "/", "/login", "/access-pending", "/settings", "/onboarding", "/profile",
        "/profile/edit", "/profile/delete-account", "/agent", "/agent/obrien/dashboard",
        "/agent/obrien/conversations", "/agent/obrien/public-profile",
        "/agent/obrien/public-profile/edit", "/agent/nosuch/dashboard",
        "/admin/users", f"/admin/users/{ids['pi']}", f"/admin/users/{bad}",
        f"/admin/users/{ids['script_user']}", "/admin/jobs", "/workspace/activity",
        f"/workspace/activity/{run}", f"/admin/activity/{run}/llm-calls", "/workspace/discussions",
        "/admin/agents", f"/admin/agents/{ids['agent_obrien']}", "/workspace/assessments",
        f"/workspace/assessments/{a1}", f"/workspace/assessments/{a2}", f"/workspace/assessments/{a3}",
        f"/workspace/assessments/{bad}", "/admin/cohorts", "/admin/cohorts/topology",
        "/admin/access-requests", "/admin/simulation", "/workspace/pis",
        f"/workspace/pis/{ids['pi']}", "/workspace/assessments", f"/workspace/assessments/{a2}",
        "/workspace/discussions", "/workspace/activity", f"/workspace/activity/{run}",
        "/workspace/slack-bots", "/workspace/prompt-suggestions", f"/assessment-chat/{a2}",
        "/invite/badtoken",
    ]


async def _one(h, role: str, route: str, width: int) -> dict:
    ctx, page, log = await h.page(None if role == "anon" else role, width)
    rec: dict = {"role": role, "route": route, "width": width, **log}
    try:
        resp = await page.goto(h.base_url + route, wait_until="networkidle", timeout=30000)
        rec["status"] = resp.status if resp else None
        rec["final"] = page.url.replace(h.base_url, "")
        ctype = resp.headers.get("content-type", "") if resp else ""
        rec["ctype"] = ctype
        if "html" in ctype:
            await page.wait_for_timeout(1500)  # markdown renders after DOMContentLoaded
            rec["xss"] = await page.evaluate("window.__xss || 0")
            rec["overflow"] = await page.evaluate(
                "document.documentElement.scrollWidth - window.innerWidth")
    except Exception as exc:  # noqa: BLE001 - every failure is evidence
        rec["error"] = str(exc)[:300]
    finally:
        await ctx.close()
    if width == 1280 and role in AXE_ROLES and h.axe_source and "html" in rec.get("ctype", ""):
        rec["axe"] = await _axe(h, role, route)
    return rec


async def _axe(h, role: str, route: str) -> list:
    """axe-core in its OWN context with CSP bypassed: Playwright injects the bundle as an
    inline script, which an enforced script-src (Phase 2) blocks. The CSP-active load
    above is the one whose console is checked."""
    ctx, page, _log = await h.page(None if role == "anon" else role, 1280, bypass_csp=True)
    try:
        await page.goto(h.base_url + route, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(1500)
        await page.add_script_tag(content=h.axe_source)
        return await page.evaluate("""async () => (await axe.run(document,
            {runOnly: {type: 'tag', values: ['wcag2a','wcag2aa','wcag21a','wcag21aa',
             'wcag22aa']}})).violations.map(v =>
                [v.id, v.impact, v.nodes.length, v.nodes.map(n => n.target)])""")
    except Exception as exc:  # noqa: BLE001 - a failed accessibility check is not green
        return [["axe-error", str(exc)[:120], 1, []]]
    finally:
        await ctx.close()


def violations(records: list[dict]) -> list[str]:
    out = []
    for r in records:
        where = f"{r['role']} {r['width']} {r['route']}"
        if r.get("error"):
            out.append(f"{where}: {r['error']}")
        if (r.get("status") or 0) >= 500:
            out.append(f"{where}: HTTP {r['status']}")
        if r.get("xss"):
            out.append(f"{where}: XSS canary fired")
        if r.get("pageerror"):
            out.append(f"{where}: page error {r['pageerror'][0]}")
        if r.get("requestfailed"):
            out.append(f"{where}: request failed {r['requestfailed'][0]}")
        if r.get("unexpected_network"):
            out.append(f"{where}: unexpected network {r['unexpected_network'][0]}")
        public = r["route"] in PUBLIC[:3] or r["route"].startswith(PUBLIC[3])
        if (r["role"] == "anon" and not public and r.get("status") == 200
                and not (r.get("final") or "").startswith("/login")):
            out.append(f"{where}: anonymous 200 without a login redirect")
        allowed = next((roles for prefix, roles in EXPECTED if r["route"].startswith(prefix)),
                       None)
        landed = (r.get("final") or "").split("?")[0]
        if (allowed is not None and r["role"] not in allowed and r.get("status") == 200
                and landed == r["route"].split("?")[0]):
            out.append(f"{where}: {r['role']} got a 200 outside the gate")
        for line in r.get("console", []):
            if "[Report Only]" not in line:
                out.append(f"{where}: console error: {line[:160]}")
    return out


async def crawl(h, widths: tuple[int, ...] = (1280, 375)) -> dict:
    sem = asyncio.Semaphore(6)

    async def job(role, route, width):
        async with sem:
            return await _one(h, role, route, width)

    tasks = [job(role, route, 1280) for role in ROLES for route in routes(h.ids)]
    if 375 in widths:
        tasks += [job(role, route, 375) for role in ("admin", "pi", "reviewer", "anon")
                  for route in routes(h.ids)]
    records = await asyncio.gather(*tasks)
    axe_counts: dict[str, int] = {}
    axe_findings = []
    for r in records:
        for rule, impact, nodes, targets in r.get("axe", []):
            axe_counts[rule] = axe_counts.get(rule, 0) + nodes
            axe_findings.append({"role": r["role"], "route": r["route"],
                                 "final": r.get("final"), "rule": rule,
                                 "impact": impact, "count": nodes, "targets": targets})
    return {"records": len(records), "violations": violations(records), "axe": axe_counts,
            "axe_findings": axe_findings,
            "overflow_375": sorted({(r["role"], r["route"], r["overflow"])
                                    for r in records
                                    if r["width"] == 375 and (r.get("overflow") or 0) > 0})}
