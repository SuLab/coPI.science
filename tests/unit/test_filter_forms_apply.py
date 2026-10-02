"""FN-04: a dropdown never submits on change; every GET filter form with a select
has a visible submit button."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_GET_FORM = re.compile(r'<form\b(?=[^>]*\bmethod="get")[^>]*>(.*?)</form>', re.I | re.S)


def _templates():
    return sorted((ROOT / "templates").rglob("*.html"))


def test_nothing_submits_on_change():
    offenders = []
    for path in [*_templates(), *(ROOT / "static/js").rglob("*.js")]:
        text = path.read_text(encoding="utf-8")
        for needle in ("autosubmit", "applyFilter", "this.form.submit()", "<noscript><button"):
            if needle in text:
                offenders.append(f"{path.relative_to(ROOT)}: {needle}")
    assert offenders == []


def test_every_get_form_with_a_select_has_a_submit_button():
    missing = []
    for path in _templates():
        for match in _GET_FORM.finditer(path.read_text(encoding="utf-8")):
            body = match.group(1)
            if "<select" in body and 'type="submit"' not in body:
                missing.append(f"{path.relative_to(ROOT)}: {match.group(0)[:90]}")
    assert missing == []
