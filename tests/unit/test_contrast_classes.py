"""X-01 source guard: the text/background classes that fail WCAG AA 4.5:1 for normal
text are not used anywhere a page is built from. The rendered-page gate
(tests/integration/test_rendered_page_gate.py) checks what actually renders; this scans
every template branch, script and src/ class string, rendered or not."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
#: Bare (unprefixed) classes retired by X-01, with the replacement each now has.
FORBIDDEN = {
    "text-gray-300": "text-gray-600",
    "text-gray-400": "text-gray-600",
    "text-green-500": "text-green-700",
    "text-green-600": "text-green-700",
    "text-amber-600": "text-amber-700",
    "text-yellow-600": "text-yellow-700",
    "text-red-500": "text-red-600",
    "text-blue-500": "text-blue-600",
    "text-indigo-500": "text-indigo-600",
    "bg-green-600": "bg-green-700",
    "bg-amber-600": "bg-amber-700",
    "bg-yellow-600": "bg-yellow-700",
    "bg-gray-400": "bg-gray-600",
}
_TOKEN = re.compile(r"(?<![\w:-])(" + "|".join(map(re.escape, FORBIDDEN)) + r")(?![\w-])")


def _sources():
    yield from (ROOT / "templates").rglob("*.html")
    yield from (p for p in (ROOT / "static/js").rglob("*.js"))
    yield from (ROOT / "src").rglob("*.py")


def test_no_retired_contrast_class_is_used():
    found = []
    for path in _sources():
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in _TOKEN.finditer(line):
                found.append(f"{path.relative_to(ROOT)}:{n}: {m.group(1)} -> {FORBIDDEN[m.group(1)]}")
            if "bg-gray-100 text-gray-500" in line:
                found.append(f"{path.relative_to(ROOT)}:{n}: text-gray-500 on bg-gray-100 -> text-gray-600")
    assert not found, "\n".join(found)


def test_dynamic_shades_are_the_passing_ones():
    body = (ROOT / "templates/assessments/_body.html").read_text()
    assert "band_class(a.band, 600, 400)" not in body
    for rel in ("templates/admin/jobs.html", "templates/workspace/discussions.html"):
        assert "}}-600" not in (ROOT / rel).read_text(), rel
