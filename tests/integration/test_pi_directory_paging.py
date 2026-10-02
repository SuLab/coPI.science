"""C-15: the users list and the PI directory filter, order and page in SQL."""

import pytest

from src.models import USER_ROLE_ADMIN, Job
from src.services import directory
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def test_pages_partition_the_filtered_directory_in_name_order(db_session, monkeypatch):
    monkeypatch.setattr(directory, "PI_DIRECTORY_PAGE_SIZE", 2)
    for i in (3, 0, 4, 1, 2):
        await factories.make_user(db_session, name=f"Pager {i}", institution="Paging University")
    kw = dict(institution_filter="paging university")
    assert await directory.count_pi_directory(db_session, **kw) == 5
    pages = [await directory.list_pi_directory(db_session, page=p, **kw) for p in (1, 2, 3)]
    assert [len(p) for p in pages] == [2, 2, 1]
    assert [r["user"].name for p in pages for r in p] == [f"Pager {i}" for i in range(5)]


async def test_profile_status_is_computed_and_filtered_in_sql(db_session):
    inst = dict(institution="Status University")
    generating = await factories.make_user(db_session, **inst)
    await factories.make_profile(db_session, user=generating, research_summary="")
    db_session.add(Job(type="generate_profile", user_id=generating.id, payload={}, status="pending"))
    pending = await factories.make_user(db_session, **inst)
    await factories.make_profile(db_session, user=pending, pending_profile={"research_summary": "draft"})
    complete = await factories.make_user(db_session, **inst)
    await factories.make_profile(db_session, user=complete)
    bare = await factories.make_user(db_session, **inst)
    await db_session.flush()
    kw = dict(institution_filter="status university")
    rows = {r["user"].id: r["profile_status"] for r in await directory.list_pi_directory(db_session, **kw)}
    assert rows == {
        generating.id: "generating",
        pending.id: "pending_update",
        complete.id: "complete",
        bare.id: "no_profile",
    }
    only = await directory.list_pi_directory(db_session, status_filter="generating", **kw)
    assert [r["user"].id for r in only] == [generating.id]
    assert await directory.count_pi_directory(db_session, status_filter="generating", **kw) == 1


async def test_the_users_page_shows_the_total_and_a_pager(client, db_session, monkeypatch):
    monkeypatch.setattr(directory, "PI_DIRECTORY_PAGE_SIZE", 1)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    await factories.make_user(db_session)
    html = (await client.get("/admin/users?page=2", headers=auth_headers(admin.id))).text
    total = await directory.count_pi_directory(db_session)
    assert f"{total} users" in html
    assert f"Page 2 of {total}" in html
