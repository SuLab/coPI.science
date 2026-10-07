"""X-05: a clickable row is reachable by keyboard: a navigating row has a real link in its
first cell (ui.js keeps the whole-row click through data-row-href); the discussions row
that expands a detail row does it through a button with aria-expanded."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NAVIGATING = {
    "templates/workspace/activity.html": "{{ page_url(request, 'workspace_activity_detail', run_id=run.id) }}",
    "templates/admin/users.html": "/admin/users/{{ item.user.id }}",
    "templates/workspace/pis.html": "{{ page_url(request, 'workspace_pi_detail', user_id=item.user.id) }}",
}


def _row_and_first_cell(text: str, marker: str) -> tuple[str, str]:
    start = text.index(marker)
    row_start = text.rindex("<tr", 0, start)
    row_tag = text[row_start: text.index(">", row_start) + 1]
    first_td = text[text.index("<td", row_start): text.index("</td>", row_start)]
    return row_tag, first_td


def test_navigating_rows_link_from_their_first_cell():
    for rel, href in NAVIGATING.items():
        text = (ROOT / rel).read_text(encoding="utf-8")
        row, cell = _row_and_first_cell(text, f'data-row-href="{href}"')
        assert "onclick" not in row, rel
        assert f'<a href="{href}"' in cell, rel


def test_the_discussion_row_expands_through_a_button():
    text = (ROOT / "templates/discussions/_threads.html").read_text(encoding="utf-8")
    assert "onclick" not in text
    row, cell = _row_and_first_cell(text, "data-row-toggles")
    assert 'data-toggles="detail-{{ loop.index }}"' in cell
    assert 'aria-controls="detail-{{ loop.index }}"' in cell
    assert 'aria-expanded="false"' in cell


def test_ui_js_drives_the_disclosure():
    js = (ROOT / "static/js/ui.js").read_text(encoding="utf-8")
    assert re.search(r'closest\("button\[data-toggles\]"\)', js)
    assert 'setAttribute("aria-expanded"' in js
    assert re.search(r'closest\("tr\[data-row-toggles\]"\)', js)
