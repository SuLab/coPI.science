"""The login page carries the Blackbird Laboratories brand and no
collaboration-platform wording (operator decision 2026-09-10, F7)."""
import pytest

pytestmark = pytest.mark.integration


async def test_login_page_shows_the_blackbird_logo_and_no_collaboration_copy(client):
    r = await client.get("/login")
    assert r.status_code == 200
    html = r.text
    assert 'src="/static/img/blackbird-laboratories.svg"' in html
    assert 'alt="Blackbird Laboratories"' in html
    assert "collaborat" not in html.lower()
    assert "Research Collaboration" not in html


async def test_logo_asset_is_served(client):
    r = await client.get("/static/img/blackbird-laboratories.svg")
    assert r.status_code == 200
    assert 'fill="#353A41"' in r.text
