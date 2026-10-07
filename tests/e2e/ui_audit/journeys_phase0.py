"""Phase 0 journeys (spec §9). Each returns {"ok": bool, ...evidence}."""

from __future__ import annotations

import asyncio


async def _delete_dialog(h, user_key: str) -> dict:
    ctx, page, log = await h.page("admin")
    try:
        await page.goto(f"{h.base_url}/admin/users/{h.ids[user_key]}", wait_until="networkidle")
        landed = page.url
        posts = []
        page.on("request", lambda request: posts.append(request.url)
                if request.method == "POST" else None)
        await page.click("form[action$='/delete'] button[type=submit]")
        await page.wait_for_timeout(1500)
        still_here = page.url == landed
        xss = await page.evaluate("window.__xss || 0")
        return {"ok": bool(log["dialogs"]) and still_here and not posts and not xss and not log["pageerror"],
                "dialogs": log["dialogs"], "url": page.url, "xss": xss,
                "pageerror": log["pageerror"], "cancelled_post_count": len(posts)}
    finally:
        await ctx.close()


async def journey_delete_confirm_script_name(h) -> dict:
    return await _delete_dialog(h, "script_user")


async def journey_delete_confirm_apostrophe(h) -> dict:
    return await _delete_dialog(h, "apostrophe_user")


async def journey_delete_confirm_hostile_names(h) -> dict:
    """Review focus 1: backslash, double quote, </script>, curly apostrophe."""
    import json
    import subprocess
    import sys

    names = ['ends in a backslash \\', 'has "double" quotes', 'has </script> inside',
             "curly O’Brien"]
    script = (
        "import asyncio, json, sys\n"
        "from src.database import get_engine, get_session_factory\n"
        "from tests import factories\n"
        "async def m():\n"
        "    out = []\n"
        "    async with get_session_factory()() as s:\n"
        "        for i, n in enumerate(json.loads(sys.argv[1])):\n"
        "            u = await factories.make_user(s, name=n, orcid=f'0000-0002-2222-{i:04d}',"
        " email=f'h{i}@uiaudit.test')\n"
        "            out.append(str(u.id))\n"
        "        await s.commit()\n"
        "    await get_engine().dispose()\n"
        "    print(json.dumps(out))\n"
        "asyncio.run(m())\n"
    )
    made = subprocess.run([sys.executable, "-c", script, json.dumps(names)], env=h.env,
                          check=True, capture_output=True, text=True)
    user_ids = json.loads(made.stdout.strip().splitlines()[-1])
    results = {}
    for i, uid in enumerate(user_ids):
        h.ids[f"hostile_{i}"] = uid
        results[names[i]] = await _delete_dialog(h, f"hostile_{i}")
    return {"ok": all(r["ok"] for r in results.values()), "results": results}


async def journey_injected_transcript_form(h) -> dict:
    out = {}
    for role, prefix in (("admin", "/admin"), ("reviewer", "/workspace")):
        ctx, page, log = await h.page(role)
        try:
            await page.goto(f"{h.base_url}{prefix}/assessments/{h.ids['assessments'][1]}",
                            wait_until="networkidle")
            await page.wait_for_timeout(3000)
            await page.evaluate("document.getElementById('timeline').open = true")
            await page.wait_for_timeout(500)
            out[role] = {
                "forms_injected": await page.locator("#injected-btn").count(),
                "escaped_visible": await page.evaluate(
                    "document.body.innerText.includes('<form method=\"post\"')"),
            }
        finally:
            await ctx.close()
    ok = all(v["forms_injected"] == 0 and v["escaped_visible"] for v in out.values())
    return {"ok": ok, **out}


async def journey_raw_html_shapes(h) -> dict:
    """Review focus 2: inline <b> and block <form> both render as text; a lone <br>
    stays a line break; markdown images become links."""
    ctx, page, log = await h.page("admin")
    try:
        await page.goto(f"{h.base_url}/workspace/assessments/{h.ids['assessments'][1]}",
                        wait_until="networkidle")
        await page.wait_for_timeout(3000)
        await page.evaluate("document.getElementById('timeline').open = true")
        probe = await page.evaluate("""() => {
            const md = window.copiRenderMarkdown(
              'a <b>x</b> b\\n\\n<div id="z">block</div>\\n\\nline<br>break ![p](https://e.example/p.png)');
            const box = document.createElement('div'); box.innerHTML = md;
            return {html: md, bold: box.querySelectorAll('b').length,
                    div: box.querySelectorAll('div').length, br: box.querySelectorAll('br').length,
                    img: box.querySelectorAll('img').length, a: box.querySelectorAll('a').length};
        }""")
        shapes = await page.evaluate("""() => {
            const md = window.copiRenderMarkdown(
              '1. a\\n\\nx\\n\\n2. b\\n\\n|a|b|\\n|:-:|-:|\\n|1|2|\\n\\n- [x] done\\n- [ ] todo\\n\\n'
              + '<br>\\n**after** text\\n\\n[j](javascript:alert(1))');
            const box = document.createElement('div'); box.innerHTML = md;
            return {start: !!box.querySelector('ol[start="2"]'),
                    center: !!box.querySelector('[align="center"]'),
                    right: !!box.querySelector('[align="right"]'),
                    tasks: box.textContent.includes('[x] done') && box.textContent.includes('[ ] todo'),
                    after_br: !!box.querySelector('strong'),
                    js_href: [...box.querySelectorAll('a')].some(a => (a.getAttribute('href') || '').startsWith('javascript'))};
        }""")
        ok = probe["bold"] == 0 and probe["div"] == 0 and probe["br"] == 1 \
            and probe["img"] == 0 and probe["a"] == 1 \
            and shapes["start"] and shapes["center"] and shapes["right"] and shapes["tasks"] \
            and shapes["after_br"] and not shapes["js_href"]
        return {"ok": ok, "probe": probe, "shapes": shapes}
    finally:
        await ctx.close()


async def _refreshed(page) -> bool:
    """A refresh tick completed (not merely stopped): "last updated" is set and the
    stop notice is hidden."""
    return await page.evaluate("""() => {
        const u = document.getElementById('sim-refresh-updated');
        const n = document.getElementById('sim-refresh-notice');
        return !!(u && u.textContent.trim()) && !!(n && n.hidden);
    }""")


async def _wait_refreshed(page, timeout_s: float = 70.0) -> bool:
    """Poll until a refresh tick has completed (the timer is 30 s; a busy machine can
    skip a tick while one is in flight), up to ``timeout_s``."""
    waited = 0.0
    while waited < timeout_s:
        if await _refreshed(page):
            return True
        await page.wait_for_timeout(2000)
        waited += 2.0
    return False


async def _sim_values(page) -> list[str]:
    return await page.evaluate(
        "[...document.querySelectorAll(\"input[name='max_runtime'], input[name='max_proposals']\")]"
        ".map(e => e.value)")


async def journey_sim_start_form_survives_refresh(h) -> dict:
    ctx, page, log = await h.page("admin")
    try:
        await page.goto(f"{h.base_url}/admin/simulation", wait_until="networkidle")
        await page.fill("input[name='max_runtime']", "60")
        await page.fill("input[name='max_proposals']", "20")
        await page.mouse.click(5, 300)
        await page.wait_for_timeout(63000)  # two refresh ticks
        refreshed = await _wait_refreshed(page)
        values = await _sim_values(page)
        return {"ok": values == ["60", "20"] and refreshed, "values": values,
                "refreshed": refreshed, "log": log}
    finally:
        await ctx.close()


async def journey_sim_focus_kept(h) -> dict:
    """Review focus 4: focus on a non-input control survives a refresh."""
    ctx, page, log = await h.page("admin")
    try:
        await page.goto(f"{h.base_url}/admin/simulation", wait_until="networkidle")
        await page.focus("nav.sim-jump-nav a[href='#sec-status']")
        await page.wait_for_timeout(33000)
        focused = await page.evaluate(
            "document.activeElement && document.activeElement.getAttribute('href')")
        refreshed = await _wait_refreshed(page)
        return {"ok": focused == "#sec-status" and refreshed, "focused": focused,
                "refreshed": refreshed}
    finally:
        await ctx.close()


async def journey_sim_notice_on_session_expiry(h) -> dict:
    ctx, page, log = await h.page("admin")
    try:
        await page.goto(f"{h.base_url}/admin/simulation?msg=Start+requested.",
                        wait_until="networkidle")
        cleaned = "msg=" not in page.url
        await ctx.clear_cookies()
        await page.wait_for_timeout(33000)
        notice = page.locator("#sim-refresh-notice")
        visible = await notice.is_visible()
        text = await notice.inner_text() if visible else ""
        return {"ok": cleaned and visible and "reload" in text.lower(), "url": page.url,
                "notice": text}
    finally:
        await ctx.close()


async def journey_sim_new_sections_inserted(h) -> dict:
    """Review focus 3: reconciliation by unit id. The stats sections exist whenever any
    run exists (`_resolve_selected_run` falls back to the newest run,
    src/services/simulation_view.py:139-157), so the journey simulates the two
    transitions directly: it removes `#sec-cost` (a unit the next fetch brings back,
    which must be re-inserted after `#sec-run`) and adds a bogus `#sec-bogus` (a unit
    the fetch no longer has, which must be removed), with a dirty Start form that must
    survive."""
    ctx, page, log = await h.page("admin")
    try:
        await page.goto(f"{h.base_url}/admin/simulation", wait_until="networkidle")
        await page.fill("input[name='max_runtime']", "45")
        await page.mouse.click(5, 300)
        await page.evaluate("""() => {
            document.getElementById('sec-cost').remove();
            const bogus = document.createElement('section');
            bogus.id = 'sec-bogus';
            document.getElementById('sim-body').appendChild(bogus);
        }""")
        await page.wait_for_timeout(33000)
        state = await page.evaluate("""() => ({
            cost: !!document.getElementById('sec-cost'),
            bogus: !!document.getElementById('sec-bogus'),
            costAfterRun: (() => {
                const ids = [...document.querySelectorAll('#sim-body > section[id]')].map(s => s.id);
                return ids.indexOf('sec-cost') === ids.indexOf('sec-run') + 1;
            })()
        })""")
        values = await _sim_values(page)
        ok = state["cost"] and not state["bogus"] and state["costAfterRun"] \
            and values[:1] == ["45"]
        return {"ok": ok, **state, "values": values}
    finally:
        await ctx.close()


async def journey_sim_template_error_kept(h) -> dict:
    """Q1-04: a rejected template and its error survive a refresh tick."""
    ctx, page, log = await h.page("admin")
    try:
        await page.goto(f"{h.base_url}/admin/simulation", wait_until="networkidle")
        area = page.locator("form[action='/admin/simulation/announce-template'] textarea[name=body]")
        await area.fill("Bad {nope} template")
        await page.locator("form[action='/admin/simulation/announce-template'] "
                           "button[type=submit]").first.click()
        await page.wait_for_load_state("networkidle")
        before = await area.input_value()
        await page.wait_for_timeout(33000)
        refreshed = await _wait_refreshed(page)
        after = await area.input_value()
        error_kept = await page.locator("#sec-announce").inner_text()
        status = await page.evaluate("""() => [
            (document.getElementById('sim-refresh-updated') || {}).textContent,
            (document.getElementById('sim-refresh-notice') || {}).textContent,
            location.pathname]""")
        return {"ok": before == after == "Bad {nope} template" and "KeyError" in error_kept
                and refreshed, "before": before, "after": after, "refreshed": refreshed,
                "status": status, "console": log["console"][-3:]}
    finally:
        await ctx.close()


async def journey_timeline_toggle_after_open(h) -> dict:
    """B-01 and review focus 5: open the timeline with "Expand all". Chromium can
    measure content inside a closed <details>, so the journey emulates engines that
    cannot (no box for closed content) with a style added before any page script
    runs; without the toggle listener the long message's button stays hidden."""
    ctx, page, log = await h.page("admin")
    await page.add_init_script("""document.addEventListener('DOMContentLoaded', () => {
        const s = document.createElement('style');
        s.textContent = 'details:not([open]) > :not(summary) { display: none !important; }';
        document.head.appendChild(s);
    }, {once: true});""")
    try:
        await page.goto(f"{h.base_url}/workspace/assessments/{h.ids['assessments'][1]}",
                        wait_until="networkidle")
        await page.wait_for_timeout(1500)
        await page.click("[data-details-toggle='open']")
        await asyncio.sleep(1)
        state = await page.evaluate("""() => [...document.querySelectorAll('#timeline [data-clamp]')]
            .map(box => ({sh: box.scrollHeight, ch: box.clientHeight,
                          btn: !box.nextElementSibling.hidden
                               && box.nextElementSibling.offsetParent !== null}))""")
        long_ones = [s for s in state if s["sh"] > s["ch"] + 2]
        return {"ok": bool(long_ones) and all(s["btn"] for s in long_ones), "state": state}
    finally:
        await ctx.close()


JOURNEYS = [
    journey_delete_confirm_script_name,
    journey_delete_confirm_apostrophe,
    journey_delete_confirm_hostile_names,
    journey_injected_transcript_form,
    journey_raw_html_shapes,
    journey_sim_start_form_survives_refresh,
    journey_sim_focus_kept,
    journey_sim_notice_on_session_expiry,
    journey_sim_new_sections_inserted,
    journey_sim_template_error_kept,
    journey_timeline_toggle_after_open,
]
