"""A-01: the Delete User confirm carries the name as an inert attribute."""

import pytest

from src.models import USER_ROLE_ADMIN
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

HOSTILE = [
    "x'+(window.__xss=1)+'",
    "Mary O'Brien",
    "ends in a backslash \\",
    'has "double" quotes',
    "has </script> inside",
]


@pytest.mark.parametrize("name", HOSTILE)
async def test_delete_user_confirm_is_an_escaped_data_attribute(client, db_session, name):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    target = await factories.make_user(db_session, name=name)
    await db_session.commit()
    html = (await client.get(f"/admin/users/{target.id}", headers=auth_headers(admin.id))).text
    form = html[html.index(f'action="/admin/users/{target.id}/delete"'):]
    form = form[: form.index(">")]
    assert "onsubmit" not in form
    assert 'data-confirm="Delete ' in form
    assert "'" not in form.split('data-confirm="', 1)[1].split('"', 1)[0]
    assert "</script>" not in form
    assert '<script src="/static/js/confirm.js"></script>' in html
    # Armed before the body renders: no window where a click submits unconfirmed.
    assert html.index('<script src="/static/js/confirm.js"></script>') < html.index("</head>")
