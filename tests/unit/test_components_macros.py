"""Fast, no-DB tests for templates/_components.html macros and templates/base.html.

Renders macros directly through a bare Jinja2 environment (no FastAPI request
needed) and renders base.html through the app's own Jinja2Templates construction,
mirroring tests/unit/test_base_html_posthog.py.
"""

import re
import types
from pathlib import Path

from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = REPO_ROOT / "templates"

_env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True)


def _render(snippet: str, **ctx) -> str:
    template = _env.from_string('{% import "_components.html" as ui %}' + snippet)
    return template.render(**ctx)


def test_data_table_region_semantics():
    html = _render(
        "{% call ui.data_table(caption='Widgets', id='t') %}"
        "<thead><tr><th>A</th></tr></thead><tbody><tr><td>1</td></tr></tbody>"
        "{% endcall %}"
    )
    assert 'role="region"' in html
    assert 'tabindex="0"' in html
    assert 'aria-labelledby="t-cap"' in html
    assert '<caption id="t-cap"' in html


def test_button_danger_variant():
    html = _render("{{ ui.button('Go', variant='danger', testid='primary') }}")
    assert 'data-testid="primary"' in html
    assert "bg-red-600" in html
    assert 'type="button"' in html


def test_button_href_renders_anchor():
    html = _render("{{ ui.button('L', href='/x') }}")
    assert '<a href="/x"' in html


def test_field_textarea_required():
    html = _render("{{ ui.field('Notes', 'notes', type='textarea', required=True) }}")
    assert "<textarea" in html
    assert "required" in html


def test_field_autocomplete_kwarg():
    html = _render("{{ ui.field(label='E', name='e', autocomplete='email') }}")
    assert 'autocomplete="email"' in html


def test_badge_every_tone_renders_nonempty_class():
    text = (TEMPLATES_DIR / "_components.html").read_text()
    match = re.search(r"macro badge.*?tones = \{(.*?)\}", text, re.DOTALL)
    assert match, "could not locate badge's tones dict literal"
    tones = re.findall(r"'(\w+)':\s*'([^']+)'", match.group(1))
    assert tones, "no tones parsed from badge macro"
    for tone, expected_class in tones:
        html = _render(f"{{{{ ui.badge('hi', tone='{tone}') }}}}")
        assert expected_class in html
        assert expected_class.strip() != ""


def test_disclosure_renders_details_and_caller_content():
    html = _render(
        "{% call ui.disclosure('Summary text') %}Body content here{% endcall %}"
    )
    assert "<details" in html
    assert "Body content here" in html


def test_tag_pill_remove_label():
    html = _render("{{ ui.tag_pill('x', 'k') }}")
    assert 'aria-label="Remove x"' in html


_BASE_TEMPLATE = Jinja2Templates(directory=str(TEMPLATES_DIR)).env.get_template("base.html")


def _render_base(*, current_user):
    request = types.SimpleNamespace(state=types.SimpleNamespace(posthog_api_key=None))
    return _BASE_TEMPLATE.render(
        request=request,
        current_user=current_user,
        impersonation_banner=None,
        active_page="admin",
        active_admin="jobs",
        flash_message=None,
    )


def test_base_html_admin_user_has_exactly_one_aria_current_per_nav_region():
    current_user = types.SimpleNamespace(
        is_admin=True, name="A", id="u1", email=None,
    )
    html = _render_base(current_user=current_user)

    primary_nav = re.search(
        r'<nav aria-label="Primary".*?</nav>\s*</header>', html, re.DOTALL
    )
    assert primary_nav, "Primary nav region not found"
    # One per link list: the desktop row and the mobile <details> panel both mark it.
    assert primary_nav.group(0).count('aria-current="page"') == 2

    admin_subnav = re.search(r'<div id="admin-subnav".*?</div>', html, re.DOTALL)
    assert admin_subnav, "admin-subnav region not found"
    assert admin_subnav.group(0).count('aria-current="page"') == 1

    assert html.count("<details") == 1
    assert html.count('id="primary-menu"') == 1
    assert "cdn.tailwindcss.com" not in html
    assert "x-cloak" not in html
    assert "viewport-fit=cover" in html
    assert "maximum-scale" not in html
    assert 'href="/static/css/app.min.css?v=' in html


def test_base_html_anonymous_user_shows_sign_in_and_primary_menu():
    html = _render_base(current_user=None)
    assert '>Sign in<' in html
    assert 'id="primary-menu"' in html
