import asyncio, json, os, sys
S = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, os.path.join(S, "app"))
os.environ["SECRET_KEY"] = "uiaudit-local-only"
from tests.e2e.session import forge_session_cookie
from playwright.async_api import async_playwright
ids = json.load(open(os.path.join(S, "seed_ids.json"))); x = json.load(open(os.path.join(S, "xss_ids.json")))
EXE = "/home/a/.cache/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell"
async def run(uid, accept):
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path=EXE)
        ctx = await b.new_context(); await ctx.add_cookies([{"name": "copi-session", "value": forge_session_cookie(ids["admin"]), "url": "http://localhost:8790"}])
        pg = await ctx.new_page(); out = {"dialogs": [], "pageerror": []}
        async def dlg(d):
            out["dialogs"].append(d.message); await (d.accept() if accept else d.dismiss())
        pg.on("dialog", dlg); pg.on("pageerror", lambda e: out["pageerror"].append(str(e)[:200]))
        await pg.goto(f"http://localhost:8790/admin/users/{uid}", wait_until="networkidle")
        out["handler"] = await pg.evaluate("document.querySelector('form[action$=\"/delete\"]').getAttribute('onsubmit')")
        await pg.click("form[action$='/delete'] button[type=submit], form[action$='/delete'] button")
        await pg.wait_for_timeout(1500)
        out["xss"] = await pg.evaluate("window.__xss || 0") if "/admin/users/" in pg.url and uid in pg.url else "navigated"
        out["url_after"] = pg.url
        await b.close(); return out
async def main():
    print("expression-injection (dismiss):", await run(x["xss_user"], False))
    print("apostrophe name (dismiss):", await run(x["apos_user"], False))
asyncio.run(main())
