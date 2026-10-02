"""B-08: the drawer's 5 s poll (`?poll=1`) does not run the every-user stale sweep,
unless the caller's own turn on this assessment is itself stale."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, AssessmentChatTurn
from src.services.assessment_chat import tier_for
from tests import factories
from tests.assessment_chat_support import history_url, seed_interview
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


def _streaming_turn(assessment_id, user, age):
    return AssessmentChatTurn(
        id=uuid.uuid4(), assessment_id=assessment_id, user_id=user.id, context_tier=tier_for(user),
        question="q", status="streaming", model="claude-opus-5-5", record_sha256_12="0" * 12,
        prompt_sha256_12="0" * 12, created_at=datetime.now(UTC) - age,
    )


async def _status(db_session, turn_id):
    return await db_session.scalar(select(AssessmentChatTurn.status).where(AssessmentChatTurn.id == turn_id))


async def test_a_poll_leaves_other_users_stale_turns_to_the_next_full_load(client, asgi_app, db_session):
    asgi_app.state.assessment_chat_enabled = True
    seeded = await seed_interview(db_session)
    viewer = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    other = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    stale = _streaming_turn(seeded.assessment_id, other, timedelta(seconds=301))
    db_session.add(stale)
    await db_session.flush()

    polled = await client.get(history_url(seeded.assessment_id) + "?poll=1", headers=auth_headers(viewer.id))
    assert polled.status_code == 200
    assert await _status(db_session, stale.id) == "streaming"

    opened = await client.get(history_url(seeded.assessment_id), headers=auth_headers(viewer.id))
    assert opened.status_code == 200
    assert await _status(db_session, stale.id) == "interrupted"


async def test_a_poll_still_sweeps_when_the_callers_own_turn_is_stale(client, asgi_app, db_session):
    asgi_app.state.assessment_chat_enabled = True
    seeded = await seed_interview(db_session)
    viewer = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    own = _streaming_turn(seeded.assessment_id, viewer, timedelta(seconds=301))
    db_session.add(own)
    await db_session.flush()

    polled = await client.get(history_url(seeded.assessment_id) + "?poll=1", headers=auth_headers(viewer.id))
    assert polled.status_code == 200
    assert await _status(db_session, own.id) == "interrupted"
    assert all(t["status"] != "streaming" for t in polled.json()["turns"])
