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
from pathlib import Path

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
    inside a row, a filter change, a show/hide toggle, a submit-on-change select."""
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
        await page.wait_for_url(f"{base}/admin/users?status_filter=complete")
        out["filter_nav"] = page.url == f"{base}/admin/users?status_filter=complete"

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
        await page.wait_for_url(f"**sort={other}**")
        out["autosubmit"] = f"sort={other}" in page.url
    finally:
        await context.close()
    checks = ("row_href", "inner_link_kept_list", "filter_nav", "toggle", "autosubmit")
    ok = all(out.get(k) for k in checks) and not errors
    return {"ok": ok, **out, "page_errors": [str(e)[:300] for e in errors]}


JOURNEYS += [journey_ui_behaviours]


# --- §6.3 / §9: CSP report-only violations over a crawl --------------------------

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
    report-only violation the browser raises. Phase 1 expects none; any entry is what
    Phase 2's enforced policy would block, and is reviewed before deploy."""
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
