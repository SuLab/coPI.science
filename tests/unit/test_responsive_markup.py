"""R-01/FN-03 (filters wrap, tables scroll, tiles stack) and R-02 (long strings wrap)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
T = ROOT / "templates"


def test_no_table_sits_in_a_clipping_wrapper():
    offenders = []
    for path in sorted(T.rglob("*.html")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for n, line in enumerate(lines):
            if "overflow-hidden" in line and "<div" in line:
                following = "\n".join(lines[n + 1:n + 6])
                if "<table" in following:
                    offenders.append(f"{path.relative_to(ROOT)}:{n + 1}")
    assert offenders == []


def test_summary_tiles_stack_on_a_phone():
    assert '<div class="grid grid-cols-2 sm:grid-cols-5 gap-4 mb-8">' in (T / "admin/_assessments_body.html").read_text()
    for name in ("admin/agents.html", "admin/access_requests.html", "admin/_run_detail_body.html"):
        assert not re.search(r'class="grid grid-cols-[3-5] gap', (T / name).read_text()), name


def test_long_strings_and_selects_cannot_widen_the_page():
    css = (ROOT / "static/css/input.css").read_text()
    for rule in ("main {\n  overflow-wrap: anywhere;", "main table {\n  overflow-wrap: break-word;",
                 "select {\n  max-width: 100%;"):
        assert rule in css, rule
