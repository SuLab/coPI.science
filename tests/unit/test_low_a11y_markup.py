"""X-03 (links in text are underlined, not colour-only) and X-06 (no colour-only
signals; glyph labels on role=img; grouped dimension selects)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
T = ROOT / "templates"


def test_links_in_text_are_underlined_in_the_compiled_css():
    assert 'main p a:not([class*="bg-"])' in (ROOT / "static/css/input.css").read_text()
    assert "main p a:not(" in (ROOT / "static/css/app.css").read_text()


def test_the_gantt_table_states_announced_in_text():
    text = (T / "admin/simulation.html").read_text()
    assert ">Announced</th>" in text
    assert "{{ 'yes' if link.announced else 'no' }}" in text


def test_glyph_labels_sit_on_role_img():
    text = (T / "assessments/_detail_body.html").read_text()
    for span in re.findall(r"<span[^>]*\baria-label=[^>]*>", text):
        assert 'role="img"' in span, span


def test_dimension_selects_are_a_fieldset_with_a_legend():
    text = (T / "assessments/_detail_body.html").read_text()
    assert text.count("<legend") >= 2
    assert '<label class="block text-sm font-medium text-gray-600 mb-1 mt-3">Rubric dimensions (optional)</label>' not in text
