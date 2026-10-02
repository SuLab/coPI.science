"""Phase 1 browser journeys (spec §9).

Run ``python -m tests.e2e.ui_audit.run journeys --phase 1`` against the instance that
``python -m tests.e2e.ui_audit.run serve`` starts. Each journey takes the harness
object and returns ``{"ok": bool, ...evidence}``.
"""

from __future__ import annotations

import base64
import json
import re
import tempfile
import time
from pathlib import Path

from tests.e2e.ui_audit.harness import AXE_PATH
from tests.e2e.ui_audit.journeys_phase1_1b import JOURNEYS_1B

REPO = Path(__file__).resolve().parents[3]

JOURNEYS: list = []


def _seed_path(template: str, ids: dict) -> str:
    """A path template with the seed's ids filled in: {pi}, {run}, {a0}."""
    return template.format(pi=ids["pi"], run=ids["run"], a0=ids["assessments"][0])


# --- §6.2: the chat and page sanitizers on the vendored copies -----------------

_SANITIZER_CHAT_JS = REPO / "static" / "js" / "assessment_chat.js"
_SANITIZER_PURIFY_RE = re.compile(r"const PURIFY = (\{.*?\n  \});", re.S)
_SANITIZER_OLD_PURIFY = "https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js"

SANITIZER_PROBES = [
    "<img src=x onerror=window.__xss=1>",
    "<script>window.__xss=1</script>",
    "<code>x</code><img src=x onerror=window.__xss=1>",
    "[js](javascript:window.__xss=1)",
    "[http](http://example.org/)",
    "![alt](https://example.org/a.png)",
    '<svg><a href="javascript:window.__xss=1">x</a></svg>',
    '<a href="https://example.org/" title="t" data-x="1" aria-label="l">a</a>',
    "~30-37% and ~x~",
]
#: Positive controls: a sanitizer that stripped everything would pass the probes.
SANITIZER_CONTROLS = [
    ["[ok](https://example.org/x)", 'a[href="https://example.org/x"]'],
    ["**bold**", "strong"],
]

_SANITIZER_INSPECT_JS = """
  const inspect = (html) => {
    const t = document.createElement("template");
    t.innerHTML = html;
    const all = [...t.content.querySelectorAll("*")];
    return {
      html: html.slice(0, 300),
      bad_tags: all.filter((e) => /^(img|svg|script|iframe|form|style|object|embed|math|video|audio)$/i.test(e.tagName)).length,
      bad_attrs: all.flatMap((e) => [...e.attributes].map((a) => a.name)).filter((n) => n !== "href").length,
      bad_hrefs: all.filter((e) => e.hasAttribute("href") && !/^https:/i.test(e.getAttribute("href"))).length,
      del: t.content.querySelectorAll("del").length,
    };
  };
  const count = (html, sel) => {
    const t = document.createElement("template");
    t.innerHTML = html;
    return t.content.querySelectorAll(sel).length;
  };
"""

_SANITIZER_LIVE_BODY = """
  const md = window.createSanitizingMarked("chat");
  const render = (p) => window.DOMPurify.sanitize(md.parse(p), PURIFY);
  return {
    purify_version: window.DOMPurify.version,
    probes: probes.map((p) => Object.assign({probe: p}, inspect(render(p)))),
    controls: controls.map(([p, sel]) => ({probe: p, selector: sel, found: count(render(p), sel)})),
    page: inspect(window.copiRenderMarkdown("<img src=x onerror=window.__xss=1> [js](javascript:window.__xss=1)")),
    xss: window.__xss || 0,
  };
}
"""

_SANITIZER_COMPARE_BODY = """
  const md = window.createSanitizingMarked("chat");
  const all = probes.concat(controls.map((c) => c[0]));
  const chat = all.map((p) => {
    const html = md.parse(p);
    return {probe: p, old: window.OldPurify.sanitize(html, PURIFY), now: window.DOMPurify.sanitize(html, PURIFY)};
  });
  const vendored = window.DOMPurify;
  const page = all.map((p) => {
    window.DOMPurify = window.OldPurify;
    const old = window.copiRenderMarkdown(p);
    window.DOMPurify = vendored;
    return {probe: p, old: old, now: window.copiRenderMarkdown(p)};
  });
  return {chat: chat.filter((r) => r.old !== r.now), page: page.filter((r) => r.old !== r.now)};
}
"""


def _sanitizer_fn(purify_literal: str, body: str) -> str:
    return (
        "([probes, controls]) => {\n"
        f"  const PURIFY = {purify_literal};\n"
        + _SANITIZER_INSPECT_JS
        + body
    )


def _sanitizer_clean(r: dict) -> bool:
    return r["bad_tags"] == 0 and r["bad_attrs"] == 0 and r["bad_hrefs"] == 0 and r["del"] == 0


async def journey_chat_sanitizer_vendored(h) -> dict:
    """§6.2: the chat's PURIFY profile and the page renderer, on the vendored marked and
    DOMPurify. Part 1 runs on a real assessment page (the files actually served).
    Part 2 sanitizes the same markup with DOMPurify 3.1.6 (the CDN copy this replaces)
    and with the vendored copy, and reports every output that differs."""
    match = _SANITIZER_PURIFY_RE.search(_SANITIZER_CHAT_JS.read_text(encoding="utf-8"))
    if match is None:
        return {"ok": False, "error": "no `const PURIFY = {...};` block in assessment_chat.js"}
    purify_literal = match.group(1)
    vendor = REPO / "static" / "vendor"
    purify_file = sorted(vendor.glob("purify-*.min.js"))[-1]
    args = [SANITIZER_PROBES, SANITIZER_CONTROLS]

    context, page, _log = await h.page("admin")
    try:
        await page.goto(
            h.base_url + _seed_path("/admin/assessments/{a0}", h.ids), wait_until="networkidle"
        )
        srcs = await page.evaluate(
            "() => [...document.scripts].map((s) => s.getAttribute('src')).filter(Boolean)"
        )
        live = await page.evaluate(_sanitizer_fn(purify_literal, _SANITIZER_LIVE_BODY), args)

        await page.goto("about:blank")
        await page.add_script_tag(url=_SANITIZER_OLD_PURIFY)
        await page.evaluate("() => { window.OldPurify = window.DOMPurify; delete window.DOMPurify; }")
        await page.add_script_tag(path=str(vendor / "marked-12.0.2.min.js"))
        await page.add_script_tag(path=str(purify_file))
        await page.add_script_tag(path=str(REPO / "static" / "js" / "markdown.js"))
        changed = await page.evaluate(_sanitizer_fn(purify_literal, _SANITIZER_COMPARE_BODY), args)
    finally:
        await context.close()

    vendored = "/static/vendor/marked-12.0.2.min.js" in srcs and any(
        re.fullmatch(r"/static/vendor/purify-3\.4\.\d+\.min\.js", s) for s in srcs
    )
    ok = (
        vendored
        and not any("cdn." in s for s in srcs)
        and live["purify_version"].startswith("3.4.")
        and all(_sanitizer_clean(p) for p in live["probes"])
        and all(c["found"] == 1 for c in live["controls"])
        and _sanitizer_clean(live["page"])
        and live["xss"] == 0
        and not changed["chat"]
        and not changed["page"]
    )
    return {"ok": ok, "script_srcs": srcs, "live": live, "changed_vs_3_1_6": changed}


JOURNEYS += [journey_chat_sanitizer_vendored]


# --- §6.3: the ui.js behaviours that replaced inline handlers --------------------


async def _ui_abort(route) -> None:
    await route.abort()


async def journey_ui_behaviours(h) -> dict:
    """Each ui.js behaviour, driven in a real browser: a row click, a click on a link
    inside a row, a filter applied with its button, a show/hide toggle, a sort select
    applied with its button (FN-04 removed submit-on-change)."""
    context, page, _log = await h.page("admin")
    errors: list = []
    page.on("pageerror", lambda e: errors.append(e))
    await context.route("https://orcid.org/**", _ui_abort)
    base = h.base_url
    out: dict = {}
    try:
        await page.goto(f"{base}/admin/users", wait_until="networkidle")
        row = page.locator("tr[data-row-href]").first
        want = await row.get_attribute("data-row-href")
        await row.locator("td").first.click()
        await page.wait_for_url(f"{base}{want}")
        out["row_href"] = page.url == f"{base}{want}"

        await page.goto(f"{base}/admin/users", wait_until="networkidle")
        async with context.expect_page() as popup_info:
            await page.locator("tr[data-row-href] a[target=_blank]").first.click()
        popup = await popup_info.value
        await popup.close()
        await page.wait_for_timeout(300)
        out["inner_link_kept_list"] = page.url == f"{base}/admin/users"

        await page.select_option("#status-filter", "complete")
        await page.locator("form:has(#status-filter) button[type=submit]").click()
        await page.wait_for_url("**status_filter=complete**")
        out["filter_nav"] = "status_filter=complete" in page.url

        await page.goto(f"{base}/admin/cohorts", wait_until="networkidle")
        button = page.locator('[data-toggle-target="new-cohort-form"]')
        await button.click()
        shown = await page.locator("#new-cohort-form").is_visible()
        expanded = await button.get_attribute("aria-expanded")
        await button.click()
        hidden = not await page.locator("#new-cohort-form").is_visible()
        out["toggle"] = shown and hidden and expanded == "true"

        await page.goto(f"{base}/admin/assessments", wait_until="networkidle")
        sort = page.locator("#assessments-sort-select")
        current = await sort.input_value()
        values = await sort.locator("option").evaluate_all("(os) => os.map((o) => o.value)")
        other = next(v for v in values if v != current)
        await sort.select_option(other)
        await page.locator("form:has(#assessments-sort-select) button[type=submit]").click()
        await page.wait_for_url(f"**sort={other}**")
        out["autosubmit"] = f"sort={other}" in page.url
    finally:
        await context.close()
    checks = ("row_href", "inner_link_kept_list", "filter_nav", "toggle", "autosubmit")
    ok = all(out.get(k) for k in checks) and not errors
    return {"ok": ok, **out, "page_errors": [str(e)[:300] for e in errors]}


JOURNEYS += [journey_ui_behaviours]


# --- §6.3 / §9: CSP violations over a crawl (report-only in Phase 1, enforced since Phase 2)

CSP_PAGES: tuple[tuple[str, str], ...] = (
    ("admin", "/admin/users"),
    ("admin", "/admin/users/{pi}"),
    ("admin", "/admin/jobs"),
    ("admin", "/admin/activity"),
    ("admin", "/admin/activity/{run}"),
    ("admin", "/admin/discussions"),
    ("admin", "/admin/agents"),
    ("admin", "/admin/assessments"),
    ("admin", "/admin/assessments/{a0}"),
    ("admin", "/admin/cohorts"),
    ("admin", "/admin/cohorts/topology"),
    ("admin", "/admin/access-requests"),
    ("admin", "/admin/simulation"),
    ("admin", "/admin/simulation?run={run}"),
    ("admin", "/manager/prompt-suggestions"),
    ("manager", "/manager/pis"),
    ("manager", "/manager/pis/{pi}"),
    ("manager", "/manager/assessments"),
    ("manager", "/manager/assessments/{a0}"),
    ("manager", "/manager/discussions"),
    ("manager", "/manager/activity"),
    ("manager", "/manager/activity/{run}"),
    ("reviewer", "/manager/assessments/{a0}"),
    ("pi", "/profile"),
    ("pi", "/profile/edit"),
    ("pi", "/settings"),
)

_CSP_COLLECTOR_JS = (
    "window.__cspViolations = [];\n"
    "document.addEventListener('securitypolicyviolation', (e) => {\n"
    "  window.__cspViolations.push({directive: e.effectiveDirective, blocked: e.blockedURI,\n"
    "    source: e.sourceFile, line: e.lineNumber, sample: e.sample, disposition: e.disposition});\n"
    "});\n"
)


async def _csp_agent_paths(page, base_url: str) -> list[str]:
    """/agent and up to five /agent/... pages it links (the PI's own agent pages)."""
    await page.goto(base_url + "/agent", wait_until="networkidle")
    hrefs = await page.evaluate(
        "() => [...document.querySelectorAll('a[href^=\"/agent/\"]')].map((a) => a.getAttribute('href'))"
    )
    return ["/agent", *list(dict.fromkeys(hrefs))[:5]]


async def journey_csp_report_only(h) -> dict:
    """Every page with an inline script or a moved handler, per role, collecting each
    CSP violation the browser raises. Phase 1 reported them (report-only); since Phase 2
    the policy is enforced, so an entry is something the browser blocked. Either way
    the journey expects none. The name is kept from Phase 1."""
    violations: list[dict] = []
    errors: list = []
    visited: list[str] = []
    for role in ("admin", "manager", "reviewer", "pi"):
        context, page, _log = await h.page(role)
        await context.add_init_script(_CSP_COLLECTOR_JS)
        page.on("pageerror", lambda e: errors.append(e))
        try:
            paths = [_seed_path(p, h.ids) for r, p in CSP_PAGES if r == role]
            if role == "pi":
                paths += await _csp_agent_paths(page, h.base_url)
            for path in paths:
                await page.goto(h.base_url + path, wait_until="networkidle")
                await page.wait_for_timeout(300)
                visited.append(f"{role} {path}")
                for v in await page.evaluate("() => window.__cspViolations || []"):
                    violations.append({"role": role, "path": path, **v})
        finally:
            await context.close()
    return {
        "ok": not violations and not errors,
        "visited": visited,
        "violations": violations,
        "page_errors": [str(e)[:300] for e in errors],
    }


JOURNEYS += [journey_csp_report_only]


# --- §6.1: compiled CSS against the Play CDN, pixel by pixel -----------------------

PARITY_PAGES: tuple[tuple[str, str], ...] = (
    ("admin", "/admin/users"),
    ("admin", "/admin/jobs"),
    ("admin", "/admin/activity"),
    ("admin", "/admin/discussions"),
    ("admin", "/admin/agents"),
    ("admin", "/admin/assessments"),
    ("admin", "/admin/assessments/{a0}"),
    ("admin", "/admin/cohorts"),
    ("admin", "/admin/cohorts/topology"),
    ("manager", "/manager/pis"),
    ("manager", "/manager/assessments/{a0}"),
    ("pi", "/profile"),
    ("pi", "/settings"),
)
PARITY_DIR = Path(tempfile.gettempdir()) / "ui_audit_parity"
#: Reviewed pixel differences (spec §6.1: "every pixel difference is reviewed before
#: merge"): committed JSON, slug -> {"diff_pixels": int | None, "size": list[int],
#: "reason": str}. Absent = none.
PARITY_REVIEWED = Path(__file__).with_name("css_parity_reviewed.json")

_PARITY_LINK = '<link rel="stylesheet" href="/static/css/app.css">'
#: What base.html loaded before §6.1: the Play CDN as a parser-blocking head script.
#: Injecting it later (an init script, after DOMContentLoaded) leaves the page unstyled.
_PARITY_CDN_TAG = '<script src="https://cdn.tailwindcss.com"></script>'

_PARITY_DIFF_JS = """async ([a, b]) => {
  const load = (data) => new Promise((resolve, reject) => {
    const img = new Image();
    img.addEventListener("load", () => resolve(img));
    img.addEventListener("error", reject);
    img.src = "data:image/png;base64," + data;
  });
  const [ia, ib] = await Promise.all([load(a), load(b)]);
  if (ia.width !== ib.width || ia.height !== ib.height) {
    return {size: [ia.width, ia.height, ib.width, ib.height], diff: null};
  }
  const canvas = document.createElement("canvas");
  canvas.width = ia.width;
  canvas.height = ia.height;
  const g = canvas.getContext("2d");
  g.drawImage(ia, 0, 0);
  const da = g.getImageData(0, 0, canvas.width, canvas.height).data;
  g.clearRect(0, 0, canvas.width, canvas.height);
  g.drawImage(ib, 0, 0);
  const db = g.getImageData(0, 0, canvas.width, canvas.height).data;
  let diff = 0;
  for (let i = 0; i < da.length; i += 4) {
    if (da[i] !== db[i] || da[i + 1] !== db[i + 1] || da[i + 2] !== db[i + 2] || da[i + 3] !== db[i + 3]) diff += 1;
  }
  return {size: [ia.width, ia.height], diff: diff};
}"""


async def _parity_cdn_document(route) -> None:
    """Serve each HTML document with the compiled stylesheet link swapped for the CDN
    script tag; every other request passes through."""
    if route.request.resource_type != "document":
        await route.continue_()
        return
    response = await route.fetch()
    body = await response.text()
    if _PARITY_LINK not in body:
        raise AssertionError(f"no compiled stylesheet link in {route.request.url}")
    await route.fulfill(response=response, body=body.replace(_PARITY_LINK, _PARITY_CDN_TAG))


async def _parity_shot(h, role: str, url: str, *, cdn: bool) -> bytes:
    """A full-page screenshot with the compiled stylesheet, or with its link replaced
    by the Play CDN script (what production served before §6.1). CSP is
    bypassed for BOTH shots: the comparison is about CSS, and the injected CDN script
    must load after Phase 2 enforces script-src."""
    context, page, _log = await h.page(role, bypass_csp=True)
    try:
        if cdn:
            await context.route("**/*", _parity_cdn_document)
        await page.goto(url, wait_until="networkidle")
        if cdn:
            await page.wait_for_function("() => window.tailwind !== undefined", timeout=15000)
        await page.wait_for_timeout(500)
        return await page.screenshot(full_page=True, animations="disabled")
    finally:
        await context.close()


async def journey_css_parity(h) -> dict:
    """§6.1: the compiled CSS against the CDN on a fixed page set. Every page with a
    non-zero diff (or a size change) is reviewed from its PNG pair before merge:
    fix the config or safelist, or record why the difference is acceptable in
    PARITY_REVIEWED (slug -> {"diff_pixels": n, "size": [...], "reason": "..."}). The
    journey passes when every page either matches or has a reviewed entry with the same
    diff count and sizes."""
    PARITY_DIR.mkdir(parents=True, exist_ok=True)
    pages = []
    diff_ctx, diff_page, _log = await h.page("admin")
    try:
        await diff_page.goto("about:blank")
        for role, template in PARITY_PAGES:
            path = _seed_path(template, h.ids)
            url = h.base_url + path
            compiled = await _parity_shot(h, role, url, cdn=False)
            cdn = await _parity_shot(h, role, url, cdn=True)
            slug = re.sub(r"[^a-z0-9]+", "_", f"{role}{path}".lower()).strip("_")
            compiled_png = PARITY_DIR / f"{slug}.compiled.png"
            cdn_png = PARITY_DIR / f"{slug}.cdn.png"
            compiled_png.write_bytes(compiled)
            cdn_png.write_bytes(cdn)
            result = await diff_page.evaluate(
                _PARITY_DIFF_JS,
                [base64.b64encode(compiled).decode(), base64.b64encode(cdn).decode()],
            )
            pages.append({
                "role": role,
                "path": path,
                "size": result["size"],
                "diff_pixels": result["diff"],
                "compiled_png": str(compiled_png),
                "cdn_png": str(cdn_png),
            })
    finally:
        await diff_ctx.close()
    reviewed = json.loads(PARITY_REVIEWED.read_text()) if PARITY_REVIEWED.exists() else {}
    unreviewed = []
    for p in pages:
        slug = Path(p["compiled_png"]).name.removesuffix(".compiled.png")
        entry = reviewed.get(slug)
        if p["diff_pixels"] == 0:
            continue
        # diff_pixels is None when the two shots differ in size: that is a change too.
        if not (entry and entry.get("diff_pixels") == p["diff_pixels"]
                and entry.get("size") == p["size"] and entry.get("reason")):
            unreviewed.append(slug)
    return {"ok": not unreviewed, "unreviewed": unreviewed, "dir": str(PARITY_DIR),
            "pages": pages}


JOURNEYS += [journey_css_parity]

# --- Part 1B: sessions, impersonation, invites (journeys_phase1_1b.py) ---------

JOURNEYS += [*JOURNEYS_1B]

# --- Part 1C: confirm dialogs accepted and dismissed (announcing Stop, Reset to file
# default, review delete, Finalize with a wrong and the right short id), the two-tab
# topology save, chat focus after an answer, zero axe color-contrast violations.
_ROLES = ("anon", "pi", "reviewer", "manager", "admin")


class _Dialogs:
    """Records every dialog and answers it with the current ``accept`` setting."""

    def __init__(self, page):
        # The page must come from h.page(..., answer_dialogs=False): a dialog answered
        # twice raises "already handled".
        self.messages: list[str] = []
        self.accept = False
        page.on("dialog", self._on_dialog)

    async def _on_dialog(self, dialog):
        self.messages.append(dialog.message)
        if self.accept:
            await dialog.accept()
        else:
            await dialog.dismiss()


def _posts(page, path: str) -> list[str]:
    seen: list[str] = []
    page.on("request", lambda r: seen.append(r.url) if r.method == "POST" and r.url.endswith(path) else None)
    return seen


async def _flash_text(page) -> str:
    region = page.locator("[data-flash-region]")
    return (await region.inner_text()) if await region.count() else ""


async def journey_confirm_stop(h) -> dict:
    """FN-01: the announcing Stop asks first; "Stop — hold open interviews" does not (D11).
    The harness runs no engine, so both buttons render disabled; the journey enables them in
    the DOM to reach the dialog, and the server then refuses with "Nothing is running."."""
    context, page, log = await h.page("admin", answer_dialogs=False)
    dialogs = _Dialogs(page)
    posts = _posts(page, "/admin/simulation/stop")
    enable = """() => document.querySelectorAll('form[action="/admin/simulation/stop"] button')
                         .forEach(b => { b.disabled = false; })"""
    announcing = page.locator('form[action="/admin/simulation/stop"]:not(:has(input[name="hold_open"])) button')
    hold = page.locator('form[action="/admin/simulation/stop"]:has(input[name="hold_open"]) button')

    await page.goto(h.base_url + "/admin/simulation")
    await page.evaluate(enable)
    await announcing.click()
    await page.wait_for_timeout(500)
    dismissed_posted = len(posts)

    dialogs.accept = True
    async with page.expect_navigation():
        await announcing.click()
    accepted_posted = len(posts) - dismissed_posted
    refusal = await _flash_text(page)

    asked_before_hold = len(dialogs.messages)
    await page.evaluate(enable)
    async with page.expect_navigation():
        await hold.click()
    hold_asked = len(dialogs.messages) > asked_before_hold
    await context.close()

    first = dialogs.messages[0] if dialogs.messages else ""
    ok = (first.startswith("Posts ") and "cannot be undone" in first
          and dismissed_posted == 0 and accepted_posted == 1
          and "Nothing is running." in refusal and not hold_asked)
    return {"ok": ok, "dialogs": dialogs.messages, "dismissed_posted": dismissed_posted,
            "accepted_posted": accepted_posted, "flash": refusal, "hold_asked": hold_asked,
            "log": log}


async def journey_confirm_reset_template(h) -> dict:
    """FN-06: Reset to file default asks first; dismissing posts nothing."""
    context, page, log = await h.page("admin", answer_dialogs=False)
    dialogs = _Dialogs(page)
    posts = _posts(page, "/admin/simulation/announce-template")
    reset = page.locator('form[action="/admin/simulation/announce-template"]:has(input[name="reset"][value="true"]) button')
    await page.goto(h.base_url + "/admin/simulation")
    await reset.click()
    await page.wait_for_timeout(500)
    dismissed_posted = len(posts)
    dialogs.accept = True
    async with page.expect_navigation():
        await reset.click()
    url_after = page.url
    confirmed = "Template reset to file default." in await page.content()
    await context.close()
    ok = (len(dialogs.messages) == 2 and dialogs.messages[0].startswith("Reset the run-start announcement")
          and dismissed_posted == 0 and len(posts) == 1 and confirmed)
    return {"ok": ok, "dialogs": dialogs.messages, "posts": posts, "url_after": url_after, "log": log}


async def journey_confirm_review_delete(h) -> dict:
    """B-10: deleting a review asks first; dismissing keeps it, accepting removes it."""
    context, page, log = await h.page("admin", answer_dialogs=False)
    dialogs = _Dialogs(page)
    marker = f"journey-delete-{int(time.time())}"
    url = f"{h.base_url}/admin/assessments/{h.ids['assessments'][0]}"
    await page.goto(url)
    await page.select_option("#add-score", "3")
    await page.select_option("#add-mode", "learn")
    await page.fill("#add-comment", marker)
    async with page.expect_navigation():
        await page.click('button:has-text("Submit feedback")')
    await page.goto(url)
    delete = page.locator(f'li:has-text("{marker}") form[action$="/delete"] button')
    if await delete.count() != 1:
        await context.close()
        return {"ok": False, "error": "the submitted review or its delete form was not found", "log": log}
    await delete.click()
    await page.wait_for_timeout(500)
    kept = await page.locator(f'li:has-text("{marker}")').count() == 1
    dialogs.accept = True
    async with page.expect_navigation():
        await delete.click()
    await page.goto(url)
    gone = await page.locator(f'li:has-text("{marker}")').count() == 0
    await context.close()
    ok = (len(dialogs.messages) == 2
          and dialogs.messages[0] == "Delete this review? This cannot be undone."
          and kept and gone)
    return {"ok": ok, "dialogs": dialogs.messages, "kept_after_dismiss": kept,
            "gone_after_accept": gone, "log": log}


async def journey_cohort_two_tab_save(h) -> dict:
    """C-07: two tabs open the topology; each ticks a different box and saves; both
    memberships survive."""
    context, tab1, log = await h.page("admin")
    name = f"two-tab-{int(time.time())}"
    created = await tab1.request.post(
        h.base_url + "/admin/cohorts/create", form={"name": name},
        headers={"Origin": h.base_url}, max_redirects=0,
    )
    tab2 = await context.new_page()
    topology = h.base_url + "/admin/cohorts/topology"
    await tab1.goto(topology)
    await tab2.goto(topology)
    href = await tab1.locator(f'thead a:text-is("{name}")').get_attribute("href")
    cohort_id = href.rsplit("/", 1)[1]
    agents = await tab1.eval_on_selector_all(
        'input[name="present_agent"]', "els => els.map(e => e.value)"
    )
    if len(agents) < 2:
        await context.close()
        return {"ok": False, "error": "the seed has fewer than two agents", "log": log}
    first, second = f'input[name="cell"][value="{cohort_id}:{agents[0]}"]', f'input[name="cell"][value="{cohort_id}:{agents[1]}"]'
    await tab1.check(first)
    async with tab1.expect_navigation():
        await tab1.click('button:has-text("Save topology")')
    await tab2.check(second)
    async with tab2.expect_navigation():
        await tab2.click('button:has-text("Save topology")')
    await tab1.goto(topology)
    both = await tab1.is_checked(first) and await tab1.is_checked(second)
    await context.close()
    return {"ok": created.status == 302 and both, "cohort": name, "create_status": created.status,
            "both_memberships_kept": both, "log": log}


async def journey_chat_focus_after_answer(h) -> dict:
    """B-06: the question box is read-only (not disabled) while answering and holds focus
    once the answer finishes."""
    context, page, log = await h.page("admin")
    await page.goto(f"{h.base_url}/admin/assessments/{h.ids['assessments'][0]}")
    opener = page.locator("[data-chat-open]").first
    if await opener.count() == 0:
        await context.close()
        return {"ok": False, "error": "no chat opener: is the assessment chat enabled in the harness?", "log": log}
    await opener.click()
    box = page.locator("#assessment-chat-question")
    await box.fill("What is being proposed, in plain terms?")
    await box.press("Enter")
    during = await page.evaluate(
        """() => { const t = document.getElementById('assessment-chat-question');
                   return {readOnly: t.readOnly, ariaBusy: t.getAttribute('aria-busy'),
                           disabled: t.disabled, focused: document.activeElement === t}; }"""
    )
    await page.wait_for_function(
        "() => !document.getElementById('assessment-chat-question').readOnly", timeout=60000
    )
    await page.wait_for_timeout(300)
    focused_id = await page.evaluate("() => document.activeElement && document.activeElement.id")
    await context.close()
    ok = focused_id == "assessment-chat-question" and not during["disabled"]
    return {"ok": ok, "during": during, "focused_after": focused_id, "log": log}


async def journey_contrast_zero(h) -> dict:
    """X-01: axe reports no color-contrast violation on the Phase 1 crawl pages, any role."""
    if not AXE_PATH.exists():
        return {"ok": False, "error": f"{AXE_PATH} is missing"}
    axe = AXE_PATH.read_text(encoding="utf-8")
    run, pi = h.ids["run"], h.ids["pi"]
    routes = [
        "/login", "/access-pending", "/settings", "/profile", "/profile/edit", "/agent",
        "/admin/users", f"/admin/users/{pi}", "/admin/jobs", "/admin/activity",
        f"/admin/activity/{run}", f"/admin/activity/{run}/llm-calls", "/admin/discussions",
        "/admin/agents", "/admin/assessments", "/admin/cohorts", "/admin/cohorts/topology",
        "/admin/access-requests", "/admin/simulation", "/manager/pis", f"/manager/pis/{pi}",
        "/manager/assessments", "/manager/discussions", "/manager/activity",
        f"/manager/activity/{run}", "/manager/slack-bots", "/manager/prompt-suggestions",
        *[f"/admin/assessments/{a}" for a in h.ids["assessments"]],
        *[f"/manager/assessments/{a}" for a in h.ids["assessments"]],
    ]
    violations: list[str] = []
    for role in _ROLES:
        # CSP bypassed: axe is injected as an inline script, which an enforced script-src
        # (Phase 2) would block. CSP is checked by its own journeys.
        context, page, log = await h.page(role, bypass_csp=True)
        for route in routes:
            response = await page.goto(h.base_url + route, wait_until="networkidle")
            if response is None or "text/html" not in response.headers.get("content-type", ""):
                continue
            # Report-only CSP in Phase 1 lets the injected script run; Phase 2's enforced
            # CSP needs a CSP-exempt injection (page.evaluate) instead.
            await page.add_script_tag(content=axe)
            found = await page.evaluate(
                """async () => {
                    const r = await axe.run(document, {runOnly: {type: 'rule', values: ['color-contrast']}});
                    return r.violations.flatMap(v => v.nodes.map(n =>
                        n.target.join(' ') + ' :: ' + ((n.any[0] && n.any[0].message) || '').slice(0, 160)));
                }"""
            )
            violations += [f"{role} {route}: {v}" for v in found]
        await context.close()
    return {"ok": not violations, "count": len(violations), "violations": violations[:200]}


async def journey_confirm_finalize(h) -> dict:
    """C-10: Finalize asks first and the server refuses a wrong short id; the right one,
    confirmed, enqueues the finalize."""
    run_id = h.ids.get("stopped_run")
    if not run_id:
        return {"ok": False, "error": "the harness seed has no stopped_run id"}
    context, page, log = await h.page("admin", answer_dialogs=False)
    dialogs = _Dialogs(page)
    posts = _posts(page, "/admin/simulation/finalize-run")
    url = f"{h.base_url}/admin/activity/{run_id}"
    button = page.locator('form[action="/admin/simulation/finalize-run"] button')

    await page.goto(url)
    dialogs.accept = True
    await page.fill("#confirm-run", "00000000")
    async with page.expect_navigation():
        await button.click()
    wrong_refused = f"Type the run's short id ({run_id[:8]})" in await _flash_text(page)

    dialogs.accept = False
    await page.fill("#confirm-run", run_id[:8])
    await button.click()
    await page.wait_for_timeout(500)
    dismissed_posted = len(posts) - 1

    dialogs.accept = True
    async with page.expect_navigation():
        await button.click()
    requested = "Finalize run requested." in await page.content()
    await context.close()
    ok = (wrong_refused and dismissed_posted == 0 and requested and len(dialogs.messages) == 3
          and all(m.startswith(f"Finalize run {run_id[:8]}: ") for m in dialogs.messages))
    return {"ok": ok, "dialogs": dialogs.messages, "wrong_refused": wrong_refused,
            "dismissed_posted": dismissed_posted, "requested": requested, "log": log}


JOURNEYS += [
    journey_confirm_stop, journey_confirm_reset_template, journey_confirm_review_delete,
    journey_cohort_two_tab_save, journey_chat_focus_after_answer, journey_contrast_zero,
    journey_confirm_finalize,
]
