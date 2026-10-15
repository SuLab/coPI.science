from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[2] / "templates"


def test_no_template_says_copi():
    hits = [f"{p.relative_to(TEMPLATES)}:{i}" for p in TEMPLATES.rglob("*.html")
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
            if "CoPI" in line]
    assert not hits, hits
