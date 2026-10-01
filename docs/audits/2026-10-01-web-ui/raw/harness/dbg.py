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
        ctx = await b.new_context(); await ctx.add_cookies([{"name": "copi-session", "value": forge_session_cookie(ids["admin"]), "url": "http://localhost:8790"}])
        pg = await ctx.new_page(); errs=[]; pg.on("pageerror", lambda e: errs.append(str(e))); pg.on("console", lambda m: errs.append(m.text) if m.type=='error' else None)
        await pg.goto("http://localhost:8790/admin/assessments/" + ids['assessments'][1], wait_until="networkidle")
        print("errors:", errs)
        print(await pg.evaluate("""() => {
          const els=[...document.querySelectorAll('[data-markdown]')].filter(e => (e.getAttribute('data-markdown')||'').includes('Thanks for the question'));
          return els.map(e => ({tag: e.tagName, cls: e.className, inner: e.innerHTML.slice(0,300), forms: e.querySelectorAll('form').length}));
        }"""))
        print(await pg.evaluate("[typeof DOMPurify, typeof marked, typeof window.copiRenderMarkdown]"))
        print(await pg.evaluate("""() => { const e=[...document.querySelectorAll('[data-markdown]')].find(e => (e.getAttribute('data-markdown')||'').includes('Thanks for the question'));
            const all=[...document.querySelectorAll('[data-markdown]')].map(x => [x.innerHTML.length, (x.getAttribute('data-markdown')||'').slice(0,40)]);
            return {render: window.copiRenderMarkdown(e.getAttribute('data-markdown')).slice(0,300), all: all, parent: e.parentElement.outerHTML.slice(0,400)}; }"""))
        await b.close()
asyncio.run(main())
