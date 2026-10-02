"""Phase 1 browser journeys (spec §9).

Run ``python -m tests.e2e.ui_audit.run journeys --phase 1`` against the instance that
``python -m tests.e2e.ui_audit.run serve`` starts. Each journey takes the harness
object and returns ``{"ok": bool, ...evidence}``.
"""

from __future__ import annotations

import re
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
