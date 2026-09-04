"""Live integration test for /admin/agents' review-count column.

The `review_counts` query in src/routers/admin.py's `admin_agents` route (feeding
`templates/admin/agents.html`'s `unreviewed = total_proposals - total_reviewed`)
counted every ProposalReview row, including the engine's implicit `rating = -1`
marker row (src.agent's `_persist_implicit_proposal_review`). That marker is not
a real PI review — Task 20.9c already taught the badge, the review e-mail, the
digest and the dashboard form to ignore it (see
.superpowers/sdd/2026-09-02-close-issues-20-27/task-20.9c-report.md); this is the
same fix applied to the one remaining reader, the admin agents page.

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


async def test_implicit_minus_one_review_does_not_count_as_reviewed(
    client, db_session, admin
):
    """A lone engine rating=-1 marker must show as 1 unreviewed / 0 reviewed."""
    pi = await factories.make_user(db_session, email="pi@example.org")
    agent = await factories.make_agent(
        session=db_session, user=pi, agent_id="agentx", bot_name="AgentXBot", status="active"
    )
    td = await factories.make_thread_decision(
        db_session, agent_a=agent.agent_id, agent_b="beta", outcome="proposal"
    )
    db_session.add(
        ProposalReview(
            thread_decision_id=td.id,
            agent_id=agent.agent_id,
            user_id=pi.id,
            rating=-1,
            submitted_via="engine",
        )
    )
    await db_session.flush()

    resp = await client.get("/admin/agents", headers=_auth(admin.id))
    assert resp.status_code == 200
    html = resp.text

    # Isolate the agent's own row so this doesn't accidentally match another
    # agent's counts elsewhere on the page.
    row_start = html.index(agent.agent_id)
    row = html[row_start : row_start + 1500]

    assert "1 to review" in row
    assert "(1 total)" in row
    assert "1 reviewed" not in row
