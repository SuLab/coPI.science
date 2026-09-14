"""Structural acceptance checks shared by every responsive page test.

Six checks, each returning a list of human-readable failure strings so a
failing page reports every defect in one shot rather than one-at-a-time.
``assert_all`` joins them and fails once.
"""

import json

from playwright.sync_api import Page

_INTERACTIVE_SELECTOR = "a, button, input, select, textarea, summary, [role=button]"

_TARGET_JS = """
() => {
    const els = Array.from(document.querySelectorAll(__SELECTOR__));
    const out = [];
    for (const el of els) {
        if (!el.checkVisibility({checkVisibilityCSS: true, contentVisibilityAuto: true})) continue;
        // Content of a closed <details> is not rendered; only its <summary> counts.
        const closed = el.closest('details:not([open])');
        if (closed && !el.closest('summary')) continue;
        // Inline text links (WCAG 2.5.8 exception) are skipped entirely; buttons,
        // inputs and selects inside the same containers are still measured.
        const tag0 = el.tagName.toLowerCase();
        if (tag0 === 'a' && el.closest("p, li, td, th, dd, [data-inline-link]")) continue;
        const exemptWrapper = el.closest('[data-target-exempt]');
        const measureEl = exemptWrapper ? exemptWrapper : el;
        const rect = measureEl.getBoundingClientRect();
        const tag = el.tagName.toLowerCase();
        const type = (el.getAttribute('type') || '').toLowerCase();
        const isCheckish = tag === 'input' && (type === 'checkbox' || type === 'radio');
        out.push({
            tag, type,
            w: rect.width, h: rect.height,
            isCheckish,
            text: (el.textContent || el.value || el.getAttribute('aria-label') || '').trim().slice(0, 40),
            cls: (el.getAttribute('class') || '').slice(0, 70),
        });
    }
    return out;
}
"""

_FONT_JS = """
() => {
    const out = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
        if (!node.nodeValue || !node.nodeValue.trim()) continue;
        const el = node.parentElement;
        if (!el) continue;
        const style = getComputedStyle(el);
        if (style.visibility === 'hidden' || style.display === 'none') continue;
        const size = parseFloat(style.fontSize);
        out.push({
            tag: el.tagName.toLowerCase(),
            size,
            text: node.nodeValue.trim().slice(0, 40),
        });
    }
    return out;
}
"""

_TABLE_JS = """
() => {
    const out = [];
    for (const table of Array.from(document.querySelectorAll('table'))) {
        const region = table.closest('[role=region]');
        out.push({
            hasCaption: table.querySelector('caption') != null,
            hasRegion: region != null,
            regionTabindex: region ? region.getAttribute('tabindex') : null,
        });
    }
    return out;
}
"""

_BODY_TEXT_TAGS = {"p", "td", "li", "label", "dd"}


def check_overflow(page: Page, width: int) -> list[str]:
    overflow = page.evaluate(
        "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    if overflow > 0:
        culprits = page.evaluate(
            """() => Array.from(document.querySelectorAll('body *'))
                .filter(el => !el.closest('details:not([open])') && el.getBoundingClientRect().right > document.documentElement.clientWidth + 0.5)
                .slice(0, 4)
                .map(el => el.tagName.toLowerCase() + '.' + (el.getAttribute('class') || '').slice(0, 60)
                     + ' right=' + Math.round(el.getBoundingClientRect().right))"""
        )
        return [f"horizontal overflow of {overflow}px at width {width}; widest: {culprits}"]
    return []


def check_targets(page: Page, width: int) -> list[str]:
    failures = []
    rows = page.evaluate(_TARGET_JS.replace("__SELECTOR__", json.dumps(_INTERACTIVE_SELECTOR)))
    min_h = 44 if width in (320, 390) else 0
    for r in rows:
        if r["isCheckish"]:
            min_w, min_h_row = 24, 24
        else:
            min_w, min_h_row = 24, max(24, min_h)
        if r["w"] < min_w or r["h"] < min_h_row:
            failures.append(
                f"{r['tag']} {r['text']!r} is {r['w']:.0f}x{r['h']:.0f}, "
                f"needs >= {min_w}x{min_h_row} [class={r['cls']!r}]"
            )
    return failures


def check_h1_and_primary(page: Page, width: int, *, has_h1: bool) -> list[str]:
    failures = []
    if has_h1:
        h1 = page.locator("h1").first
        if h1.count() == 0:
            return ["no <h1> found"]
        box = h1.bounding_box()
        viewport = page.viewport_size
        if box is None:
            failures.append("h1 has no bounding box (not rendered)")
        elif not (0 <= box["y"] <= viewport["height"]):
            failures.append(
                f"h1 at y={box['y']:.0f} is outside the viewport height {viewport['height']}"
            )
    primary = page.locator("[data-testid=primary]").first
    if primary.count() > 0:
        box = primary.bounding_box()
        viewport = page.viewport_size
        if box is not None and box["x"] + box["width"] > viewport["width"]:
            failures.append(
                f"[data-testid=primary] right edge {box['x'] + box['width']:.0f} "
                f"exceeds viewport width {viewport['width']}"
            )
    return failures


def check_tables(page: Page, width: int) -> list[str]:
    failures = []
    for t in page.evaluate(_TABLE_JS):
        if not t["hasCaption"]:
            failures.append("table missing <caption>")
        if not t["hasRegion"]:
            failures.append("table has no enclosing [role=region]")
        elif t["regionTabindex"] != "0":
            failures.append(
                f"table's [role=region] has tabindex={t['regionTabindex']!r}, expected '0'"
            )
    return failures


def check_console(page: Page, width: int) -> list[str]:
    failures = []
    for msg in getattr(page, "_console_errors", []):
        if "favicon" in msg:
            continue
        failures.append(f"console error: {msg}")
    for msg in getattr(page, "_page_errors", []):
        if "favicon" in msg:
            continue
        failures.append(f"pageerror: {msg}")
    return failures


def check_fonts(page: Page, width: int) -> list[str]:
    failures = []
    body_min = 14 if width in (320, 390) else 12
    for r in page.evaluate(_FONT_JS):
        floor = body_min if r["tag"] in _BODY_TEXT_TAGS else 12
        if r["size"] < floor:
            failures.append(
                f"{r['tag']} {r['text']!r} has font-size {r['size']:.1f}px, needs >= {floor}px"
            )
    return failures


def assert_all(page: Page, width: int, *, has_h1: bool = True) -> None:
    failures: list[str] = []
    failures += check_overflow(page, width)
    failures += check_targets(page, width)
    failures += check_h1_and_primary(page, width, has_h1=has_h1)
    failures += check_tables(page, width)
    failures += check_console(page, width)
    failures += check_fonts(page, width)
    if failures:
        joined = "\n  - ".join(failures)
        raise AssertionError(f"{len(failures)} responsive check failure(s):\n  - {joined}")
