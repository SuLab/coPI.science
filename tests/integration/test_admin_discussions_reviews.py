"""Live integration test for /admin/discussions' "PI Reviews" panel.

The `reviews_query` in src/routers/admin.py's `admin_discussions` route (feeding
`templates/admin/discussions.html`'s "PI Reviews" section, which renders
`{{ rev.rating }}/4`) counted every ProposalReview row, including the engine's
implicit `rating = -1` marker row (src.agent's `_persist_implicit_proposal_review`).
That marker is not a real PI review — Task 20.9c already taught the badge, the
review e-mail, the digest and the dashboard form to ignore it (see
.superpowers/sdd/2026-09-02-close-issues-20-27/task-20.9c-report.md), and part 3
taught the admin agents page too. This is the same fix applied to the one
remaining reader, the admin discussions page: a lone `rating = -1` row must not
render as "-1/4".

Real ASGI requests, real Postgres, real Jinja templates — same harness as
tests/integration/test_cohort_admin.py.
"""

import base64
import json

import pytest
from itsdangerous import TimestampSigner

from src.config import get_settings
from src.models import ProposalReview
from tests import factories

pytestmark = pytest.mark.integration


def _auth(user_id) -> dict:
    """Forge the signed session cookie SessionMiddleware would issue."""
    signer = TimestampSigner(get_settings().secret_key)
    data = base64.b64encode(json.dumps({"user_id": str(user_id)}).encode())
    return {"Cookie": f"copi-session={signer.sign(data).decode()}"}


@pytest.fixture
async def admin(db_session):
    return await factories.make_user(db_session, is_admin=True, email="admin@example.org")


async def test_implicit_minus_one_review_is_not_rendered_in_discussions_panel(
    client, db_session, admin
):
    """A thread whose only review is the engine's rating=-1 marker must not show "-1/4"."""
    run = await factories.make_simulation_run(db_session)
    await factories.make_agent_message(
        db_session,
        run=run,
        agent_id="agentx",
        channel_name="general",
        phase="new_post",
        message_ts="100.0001",
        thread_ts=None,
    )
    td = await factories.make_thread_decision(
        db_session,
        run=run,
        thread_id="100.0001",
        channel="general",
        agent_a="agentx",
        agent_b="beta",
        outcome="proposal",
    )
    pi = await factories.make_user(db_session, email="pi@example.org")
    db_session.add(
        ProposalReview(
            thread_decision_id=td.id,
            agent_id="agentx",
            user_id=pi.id,
            rating=-1,
            submitted_via="engine",
        )
    )
    await db_session.flush()

    resp = await client.get("/admin/discussions", headers=_auth(admin.id))
    assert resp.status_code == 200
    assert "-1/4" not in resp.text
