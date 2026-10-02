"""FN-07: every human-facing timestamp goes through `display_format.timestamp` (the
`ts` filter) and so always shows its zone; nothing converts to browser-local time."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "templates"


def test_no_template_formats_a_timestamp_itself():
    offenders = [
        f"{path.relative_to(ROOT)}:{n}"
        for path in sorted(TEMPLATES.rglob("*.html"))
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "strftime(" in line or "data-utc" in line
    ]
    assert offenders == []


def test_nothing_converts_to_browser_local_time():
    sources = [*TEMPLATES.rglob("*.html"), *(ROOT / "static/js").rglob("*.js")]
    offenders = [
        str(path.relative_to(ROOT))
        for path in sources
        if "toLocaleTimeString" in path.read_text(encoding="utf-8")
        or "toLocaleDateString" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
