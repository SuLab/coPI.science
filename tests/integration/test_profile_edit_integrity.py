"""apply_profile_edits: refusals write nothing (D-02), a re-posted tenure year keeps its
provenance (D-01), and a save that races a profile generation is refused with a message
instead of a 500 (D-08)."""
import json

import pytest
from sqlalchemy import select, text

from src.models import AppSetting, Job, ResearcherProfile, User
from src.services import profile_edit, profile_export
from src.services.jhu_rules import TENURE_KEY_PREFIX, get_tenure_start, set_tenure_start
from src.services.profile_edit import PROFILE_GENERATING, apply_profile_edits
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _exports_to_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)


async def _edit(db_session, user, **kwargs):
    form = kwargs.pop("form", {"research_summary": "Edited."})
    return await apply_profile_edits(
        db_session, target_user=user, changed_by_user_id=user.id, form=form,
        expected_version=None, **kwargs,
    )


async def _tenure_source(db_session, user_id):
    raw = await db_session.scalar(
        select(AppSetting.value).where(AppSetting.key == f"{TENURE_KEY_PREFIX}{user_id}")
    )
    return json.loads(raw)["source"] if raw else None


async def test_a_reposted_tenure_year_keeps_its_machine_source(db_session):
    user = await factories.make_user(db_session)
    await set_tenure_start(user.id, 2018, "orcid_employment", db=db_session)
    await db_session.flush()
    assert await _edit(db_session, user, jhu_tenure_start="2018") is None
    assert await _tenure_source(db_session, user.id) == "orcid_employment"


async def test_a_changed_tenure_year_is_recorded_as_manual(db_session):
    user = await factories.make_user(db_session)
    await set_tenure_start(user.id, 2018, "orcid_employment", db=db_session)
    await db_session.flush()
    assert await _edit(db_session, user, jhu_tenure_start="2016") is None
    assert await get_tenure_start(db_session, user.id) == 2016
    assert await _tenure_source(db_session, user.id) == "manual"


async def test_an_invalid_email_writes_no_tenure(db_session):
    user = await factories.make_user(db_session)
    error = await _edit(db_session, user, form={"email": "not-an-email"}, jhu_tenure_start="2016")
    assert error == "invalid_email"
    assert await get_tenure_start(db_session, user.id) is None


async def test_a_taken_email_writes_no_tenure(db_session):
    await factories.make_user(db_session, email="taken@example.org")
    user = await factories.make_user(db_session)
    error = await _edit(db_session, user, form={"email": "taken@example.org"}, jhu_tenure_start="2016")
    assert error == "email_taken"
    assert await get_tenure_start(db_session, user.id) is None


@pytest.mark.parametrize("status", ["pending", "processing"])
async def test_a_pending_generation_job_refuses_the_save_and_writes_nothing(db_session, status):
    user = await factories.make_user(db_session, name="Before")
    db_session.add(Job(type="generate_profile", user_id=user.id, status=status,
                       payload={"user_id": str(user.id)}))
    await db_session.flush()
    error = await _edit(db_session, user, form={"name": "After", "research_summary": "x"},
                        jhu_tenure_start="2016")
    assert error == PROFILE_GENERATING
    assert (await db_session.scalar(select(User.name).where(User.id == user.id))) == "Before"
    assert await get_tenure_start(db_session, user.id) is None
    assert await db_session.scalar(
        select(ResearcherProfile.id).where(ResearcherProfile.user_id == user.id)
    ) is None


async def test_a_finished_generation_job_does_not_block(db_session):
    user = await factories.make_user(db_session)
    db_session.add(Job(type="generate_profile", user_id=user.id, status="completed",
                       payload={"user_id": str(user.id)}))
    await db_session.flush()
    assert await _edit(db_session, user) is None


async def test_the_profile_insert_gives_up_on_a_held_lock(db_session, engine, monkeypatch):
    """The worker's uncommitted profile row stands in as a table lock held by another
    connection; the insert must time out into the refusal, not wait or 500."""
    monkeypatch.setattr(profile_edit, "PROFILE_INSERT_LOCK_TIMEOUT", "200ms")
    user = await factories.make_user(db_session)
    await db_session.commit()  # savepoint release: the refusal's rollback keeps the user
    # Read before the refusal's rollback expires `user` (a lazy load under asyncio
    # raises MissingGreenlet; plan audit Q1-05).
    user_id = user.id
    async with engine.connect() as blocker:
        await blocker.begin()
        await blocker.execute(text("LOCK TABLE researcher_profiles IN SHARE ROW EXCLUSIVE MODE"))
        try:
            error = await _edit(db_session, user)
        finally:
            await blocker.rollback()
    assert error == PROFILE_GENERATING
    assert await db_session.scalar(
        select(ResearcherProfile.id).where(ResearcherProfile.user_id == user_id)
    ) is None


async def test_the_profile_page_explains_the_refusal(client, db_session):
    user = await factories.make_user(db_session)
    db_session.add(Job(type="generate_profile", user_id=user.id, status="pending",
                       payload={"user_id": str(user.id)}))
    await db_session.flush()
    r = await client.post(
        "/profile/save",
        data={"name": user.name, "email": user.email, "research_summary": "x"},
        headers=auth_headers(user.id), follow_redirects=False,
    )
    assert r.headers["location"] == "/profile/edit?error=profile_generating"
    page = await client.get(r.headers["location"], headers=auth_headers(user.id))
    assert "Profile is being generated — try again shortly" in page.text
