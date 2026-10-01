import asyncio
from playwright.async_api import async_playwright
EXE = "/home/a/.cache/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell"
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path=EXE); pg = await b.new_page()
        await pg.goto("file:///tmp/claude-1000/-home-a-mounts-ubuntu-blackbird-copi-science/d88e4f17-1d50-42f2-9bb2-09615cc8aa0d/scratchpad/export.html", wait_until="load")
        await pg.wait_for_timeout(2000)
        print(await pg.evaluate("[...document.querySelectorAll('[data-markdown]')].map(e => [e.innerHTML.length, e.getAttribute('data-markdown').length])"))
        await b.close()
asyncio.run(main())
