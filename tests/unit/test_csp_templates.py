"""Every inline <script> in templates/ carries the per-request nonce (spec §6.3).

Scans the source, so an inline block added by any later change (Phase 0, Parts 1B/1C)
fails here without a page that renders it."""

import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[2] / "templates"
NONCE_ATTR = 'nonce="{{ request.state.csp_nonce }}"'
_SCRIPT_TAG = re.compile(r"<script\b[^>]*>", re.I)


def test_every_inline_script_carries_the_request_nonce():
    offenders = []
    checked = 0
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for m in _SCRIPT_TAG.finditer(text):
            tag = m.group(0)
            if re.search(r"\bsrc\s*=", tag):
                continue
            checked += 1
            if NONCE_ATTR not in tag:
                line = text.count("\n", 0, m.start()) + 1
                offenders.append(f"{path.relative_to(TEMPLATES)}:{line}: {tag}")
    assert offenders == []
    # Control: the scan finds inline scripts at all. Not pinned to the inventory's count:
    # later tasks move inline scripts into files (1C-10 tag_widget.js, 2B-2), so a fixed
    # floor would break without any regression (assembly audit PX-08a).
    assert checked >= 1
