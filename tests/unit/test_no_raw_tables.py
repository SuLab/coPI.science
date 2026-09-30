"""Every page-level template renders tables via the ``data_table`` macro.

``templates/_components.html`` is the one permitted site of a raw ``<table``;
every template that extends ``base.html`` (i.e. renders a real page, not a
fragment or a component) must not contain one directly.
"""

import re
from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"


_EXTENDS_BASE = re.compile(r"""{%-?\s*extends\s*['"]base\.html['"]""")


def _templates_extending_base() -> list[Path]:
    return [
        p
        for p in TEMPLATES_DIR.rglob("*.html")
        if p.name != "_components.html" and _EXTENDS_BASE.search(p.read_text())
    ]


def test_no_raw_table_outside_components():
    offenders = [
        str(p.relative_to(TEMPLATES_DIR))
        for p in _templates_extending_base()
        if "<table" in p.read_text().lower()
    ]
    assert not offenders, f"raw <table found outside _components.html in: {offenders}"


def test_discussions_export_has_no_raw_table():
    path = TEMPLATES_DIR / "admin" / "discussions_export.html"
    assert path.exists(), f"{path} not found"
    assert "<table" not in path.read_text().lower()
