"""GET /admin/authorship-checks (the 2026-10-15 publication-handling design §11 R6.17,
F31): admin-only, read-only, filtered, paged, and escaping the model text it shows."""
import re
from urllib.parse import quote

import pytest

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    USER_ROLE_REVIEWER,
    AuthorshipClaimCheck,
)
from src.routers import admin
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration
URL = "/admin/authorship-checks"
XSS_EXCERPT = "<script>alert(1)</script> & <b>x</b>"
XSS_REASON = "<img src=x onerror=alert(1)>"


async def _seed(db_session):
    r1 = await factories.make_simulation_run(db_session)
    r2 = await factories.make_simulation_run(db_session)

    def row(run, agent, mode, outcome, excerpt="text", reasons=()):
        return AuthorshipClaimCheck(
            simulation_run_id=run.id, agent_id=agent, thread_key="phase5", phase="new_post",
            mode=mode, outcome=outcome, excerpt=excerpt,
            reasons=[{"code": c, "text": t} for c, t in reasons],
        )

    db_session.add_all([
        row(r1, "wang", "log", "rejected", reasons=[("uncited", "no citation")]),
        row(r1, "wang", "log", "grounded"),
        row(r1, "gordy", "log", "no_claim"),
        row(r2, "wang", "enforce", "rejected", excerpt=XSS_EXCERPT,
            reasons=[("partner_not_held", XSS_REASON)]),
    ])
    await db_session.flush()
    return r1, r2


async def _admin(db_session):
    return await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)


def _rows(html: str) -> int:
    return html.count('data-row="claim-check"')


def _count_card(html: str, key: str) -> int:
    return int(re.search(rf'id="count-{key}"[^>]*>(\d+)<', html).group(1))


async def test_admin_sees_the_rows(client, db_session):
    await _seed(db_session)
    resp = await client.get(URL, headers=auth_headers((await _admin(db_session)).id))
    assert resp.status_code == 200
    assert _rows(resp.text) == 4
    assert [_count_card(resp.text, k) for k in ("no_claim", "grounded", "rejected")] == [1, 1, 2]


@pytest.mark.parametrize("role", [USER_ROLE_MANAGER, USER_ROLE_REVIEWER, USER_ROLE_PI])
async def test_non_admins_are_refused(client, db_session, role):
    user = await factories.make_user(db_session, user_role=role)
    assert (await client.get(URL, headers=auth_headers(user.id))).status_code == 403


async def test_anonymous_is_treated_like_other_admin_pages(client):
    page, jobs = await client.get(URL), await client.get("/admin/jobs")
    # The same redirect to sign-in; only the ``next`` path differs.
    assert page.status_code == jobs.status_code
    assert page.headers.get("location") == jobs.headers.get("location", "").replace(
        quote("/admin/jobs", safe=""), quote(URL, safe=""))


async def test_filters_narrow_the_rows(client, db_session):
    r1, _ = await _seed(db_session)
    headers = auth_headers((await _admin(db_session)).id)
    cases = [
        (f"?run={r1.id}", 3, f'value="{r1.id}" selected'),
        ("?agent=gordy", 1, 'value="gordy" selected'),
        ("?outcome=rejected", 2, 'value="rejected" selected'),
        ("?mode=enforce", 1, 'value="enforce" selected'),
        (f"?run={r1.id}&agent=wang&outcome=rejected", 1, 'value="wang" selected'),
    ]
    for query, n, marker in cases:
        html = (await client.get(URL + query, headers=headers)).text
        assert (_rows(html), marker in html) == (n, True), query


@pytest.mark.parametrize(("query", "detail"), [
    ("?run=x", "Unknown run filter"),
    ("?outcome=blocked", "Unknown outcome filter"),
    ("?mode=shadow", "Unknown mode filter"),
    ("?agent=" + "a" * 51, "Unknown agent filter"),
])
async def test_unknown_filter_values_are_400(client, db_session, query, detail):
    resp = await client.get(URL + query, headers=auth_headers((await _admin(db_session)).id))
    assert resp.status_code == 400
    assert detail in resp.text


async def test_pagination_carries_the_filters(client, db_session):
    r1, _ = await _seed(db_session)
    db_session.add_all([
        AuthorshipClaimCheck(simulation_run_id=r1.id, agent_id="wang", thread_key="phase5",
                             phase="new_post", mode="log", outcome="no_claim", excerpt=f"n{i}")
        for i in range(130)
    ])
    await db_session.flush()
    html = (await client.get(URL + "?agent=wang&page=2",
                             headers=auth_headers((await _admin(db_session)).id))).text
    assert _rows(html) == 133 - 100
    assert "Page 2 of 2" in html
    prev = re.search(r'<a href="([^"]*)"[^>]*>&larr; Prev', html).group(1)
    assert "agent=wang" in prev


async def test_excerpt_and_reasons_are_escaped(client, db_session):
    _, r2 = await _seed(db_session)
    html = (await client.get(f"{URL}?run={r2.id}",
                             headers=auth_headers((await _admin(db_session)).id))).text
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &lt;b&gt;x&lt;/b&gt;" in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html
    assert "<script>alert(1)" not in html and "<img src=x" not in html


async def test_there_are_no_write_routes(client, asgi_app, db_session):
    # The admin router is mounted under /admin, so its own paths carry no prefix
    # (as tests/unit/test_admin_route_table.py reads them).
    routes = [r for r in admin.router.routes if f"/admin{r.path}" == URL]
    assert routes and all(set(r.methods) <= {"GET", "HEAD"} for r in routes)
    resp = await client.post(URL, headers=auth_headers((await _admin(db_session)).id))
    assert resp.status_code == 405


async def test_admin_nav_links_the_page(client, db_session):
    html = (await client.get("/admin/jobs", headers=auth_headers((await _admin(db_session)).id))).text
    nav = html.split('aria-label="Admin sections"', 1)[1].split("</nav>", 1)[0]
    assert 'href="/admin/authorship-checks"' in nav
