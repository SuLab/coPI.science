import asyncio, json, os, sys
S = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, os.path.join(S, "app"))
os.environ["SECRET_KEY"] = "uiaudit-local-only"
from tests.e2e.session import forge_session_cookie
from playwright.async_api import async_playwright
ids = json.load(open(os.path.join(S, "seed_ids.json")))
EXE = "/home/a/.cache/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell"
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path=EXE)
        for role, path in (("admin", f"/admin/assessments/{ids['assessments'][1]}"), ("reviewer", f"/manager/assessments/{ids['assessments'][1]}")):
            ctx = await b.new_context(); await ctx.add_cookies([{"name": "copi-session", "value": forge_session_cookie(ids[role]), "url": "http://localhost:8790"}])
            pg = await ctx.new_page()
            await pg.goto("http://localhost:8790" + path, wait_until="networkidle")
            await pg.wait_for_timeout(3000)
            n = await pg.locator("#injected-btn").count()
            html = await pg.content()
            print(role, 'raw-form-escaped-in-html:', '&lt;form method' in html, 'data-markdown count:', html.count('data-markdown='), 'injected text present:', 'Thanks for the question' in html)
            info = {"role": role, "injected_form_in_dom": n}
            if n:
                info["form_action"] = await pg.evaluate("document.querySelector('#injected-btn').form.getAttribute('action')")
                info["btn_style"] = await pg.evaluate("document.querySelector('#injected-btn').getAttribute('style')")
                info["visible_overlay"] = await pg.evaluate("(() => {const r=document.querySelector('#injected-btn').getBoundingClientRect(); return [r.width, r.height]})()")
                info["timeline_open"] = await pg.evaluate("document.getElementById('timeline') && document.getElementById('timeline').open")
                await pg.evaluate("document.getElementById('timeline').open = true")
                await pg.wait_for_timeout(300)
                info["overlay_after_open"] = await pg.evaluate("(() => {const r=document.querySelector('#injected-btn').getBoundingClientRect(); return [r.width, r.height]})()")
                await pg.mouse.click(400, 400)   # an innocent click anywhere
                await pg.wait_for_timeout(1500)
                info["url_after_click"] = pg.url
            print(info)
            await ctx.close()
        await b.close()
asyncio.run(main())
