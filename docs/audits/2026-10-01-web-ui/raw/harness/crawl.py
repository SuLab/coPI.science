"""Role x route crawl of the local UI-audit instance: status matrix, console/page
errors, failed subrequests, XSS canary, horizontal overflow, axe-core violations."""

import asyncio
import json
import os
import sys

S = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(S, "app"))
os.environ["SECRET_KEY"] = "uiaudit-local-only"
from tests.e2e.session import forge_session_cookie  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

BASE = "http://localhost:8790"
ids = json.load(open(os.path.join(S, "seed_ids.json")))
A1, A2, A3 = ids["assessments"]
RUN = ids["run"]
OBRIEN_ROW = "984cbfed-2cd9-46d6-9b1f-04c2f2f2209c"
PROBE_ROW = "ed5f78a3-6eaa-4d27-b0ac-14bd330c650d"
ROOT_TS = "1790866301.697524"
BAD = "00000000-0000-0000-0000-000000000000"

ROUTES = [
    "/", "/login", "/access-pending", "/settings", "/cabo-graph", "/scripps-graph",
    "/schultz-alumni-pilot", "/schultz-group-alumni",
    "/onboarding", "/profile", "/profile/edit", "/profile/delete-account",
    "/agent", "/agent/obrien/dashboard", "/agent/obrien/conversations",
    f"/agent/obrien/thread/{ROOT_TS}", "/agent/obrien/public-profile",
    "/agent/obrien/public-profile/edit", "/agent/nosuch/dashboard",
    "/admin", "/admin/users", f"/admin/users/{ids['pi']}", f"/admin/users/{BAD}",
    "/admin/users/not-a-uuid", "/admin/jobs", "/admin/activity", f"/admin/activity/{RUN}",
    f"/admin/activity/{RUN}/llm-calls", f"/admin/activity/{BAD}", "/admin/discussions",
    "/admin/agents", f"/admin/agents/{OBRIEN_ROW}", f"/admin/agents/{PROBE_ROW}",
    "/admin/assessments", f"/admin/assessments/{A1}", f"/admin/assessments/{A2}",
    f"/admin/assessments/{A3}", f"/admin/assessments/{BAD}", "/admin/cohorts",
    "/admin/cohorts/topology", "/admin/access-requests", "/admin/simulation",
    "/manager", "/manager/pis", f"/manager/pis/{ids['pi']}", f"/manager/pis/{BAD}",
    "/manager/assessments", f"/manager/assessments/{A2}", "/manager/discussions",
    "/manager/activity", f"/manager/activity/{RUN}", "/manager/slack-bots",
    "/manager/prompt-suggestions", f"/manager/prompt-suggestions/{BAD}",
    f"/assessment-chat/{A2}", "/invite/badtoken",
    "/admin/assessments?page=-1&sort=zzz", "/admin/assessments?page=99999",
    "/manager/assessments?status=<script>&page=abc", "/admin/discussions?run=garbage",
    "/admin/jobs?status=nonsense", "/admin/activity?page=-5",
]
ROLES = ["anon", "admin", "manager", "reviewer", "pi", "pi2", "delegate", "pending"]


async def crawl_one(browser, role, route, width, do_axe, axe_src, shots):
    ctx = await browser.new_context(viewport={"width": width, "height": 900})
    if role != "anon":
        await ctx.add_cookies([{"name": "copi-session", "value": forge_session_cookie(ids[role]),
                                "url": BASE}])
    page = await ctx.new_page()
    rec = {"role": role, "route": route, "width": width, "console": [], "pageerror": [],
           "failed": [], "dialogs": []}
    page.on("console", lambda m: rec["console"].append(f"{m.type}: {m.text}"[:300])
            if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: rec["pageerror"].append(str(e)[:300]))
    page.on("requestfailed", lambda r: rec["failed"].append(f"{r.url[:120]} {r.failure}"))
    page.on("response", lambda r: rec["failed"].append(f"{r.status} {r.url[:150]}")
            if r.status >= 400 and r.request.resource_type != "document" else None)

    async def on_dialog(d):
        rec["dialogs"].append(f"{d.type}: {d.message[:100]}")
        await d.dismiss()
    page.on("dialog", on_dialog)
    try:
        resp = await page.goto(BASE + route, wait_until="networkidle", timeout=30000)
        rec["status"] = resp.status if resp else None
        rec["final"] = page.url.replace(BASE, "")
        rec["ctype"] = (resp.headers.get("content-type", "") if resp else "")
        if "html" in rec["ctype"]:
            m = await page.evaluate("""() => ({
                xss: window.__xss || 0,
                overflow: document.documentElement.scrollWidth - window.innerWidth,
                title: document.title,
                h1: document.querySelectorAll('h1').length,
                imgs_noalt: [...document.images].filter(i => !i.hasAttribute('alt')).length,
                wide: [...document.querySelectorAll('body *')].filter(e => {
                    const r = e.getBoundingClientRect(); return r.right > window.innerWidth + 1
                        && getComputedStyle(e).position !== 'fixed'
                        && !e.closest('.overflow-x-auto,.overflow-auto,.overflow-x-scroll,[style*=overflow]');
                }).slice(0, 4).map(e => e.tagName + '.' + (e.className && e.className.baseVal === undefined ? String(e.className).slice(0, 60) : '') + ' ' + Math.round(e.getBoundingClientRect().right)),
            })""")
            rec.update(m)
            if do_axe:
                await page.add_script_tag(content=axe_src)
                res = await page.evaluate("""async () => {
                    const r = await axe.run(document, {runOnly: {type: 'tag',
                        values: ['wcag2a','wcag2aa','wcag21a','wcag21aa','wcag22aa','best-practice']}});
                    return r.violations.map(v => ({id: v.id, impact: v.impact, n: v.nodes.length,
                        sample: v.nodes.slice(0, 2).map(n => n.target.join(' ') + ' :: ' + (n.failureSummary || '').slice(0, 160))}));
                }""")
                rec["axe"] = res
            if shots:
                name = f"{role}_{width}_{route.strip('/').replace('/', '_').replace('?', '_')[:80] or 'root'}.png"
                await page.screenshot(path=os.path.join(S, "shots", name), full_page=True)
    except Exception as e:  # noqa: BLE001
        rec["error"] = str(e)[:300]
    await ctx.close()
    return rec


async def main():
    os.makedirs(os.path.join(S, "shots"), exist_ok=True)
    axe_src = open(os.path.join(S, "axe.min.js")).read()
    out = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path="/home/a/.cache/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell")
        sem = asyncio.Semaphore(6)

        async def job(role, route, width, do_axe, shots):
            async with sem:
                out.append(await crawl_one(browser, role, route, width, do_axe, axe_src, shots))
        tasks = []
        for role in ROLES:
            for route in ROUTES:
                tasks.append(job(role, route, 1280, role in ("admin", "manager", "reviewer", "pi", "anon"), False))
        # mobile pass for the roles that see the most surfaces
        for role in ("admin", "pi", "reviewer", "anon"):
            for route in ROUTES:
                tasks.append(job(role, route, 375, False, True))
        await asyncio.gather(*tasks)
        await browser.close()
    json.dump(out, open(os.path.join(S, "crawl.json"), "w"), indent=1)
    print(len(out), "records")


asyncio.run(main())
