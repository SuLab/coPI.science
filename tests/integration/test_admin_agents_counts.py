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


async def test_explicit_review_counts_as_reviewed(client, db_session, admin):
    """Positive control: a real PI rating must still show as "1 reviewed"."""
    pi = await factories.make_user(db_session, email="pi2@example.org")
    agent = await factories.make_agent(
        session=db_session, user=pi, agent_id="agenty", bot_name="AgentYBot", status="active"
    )
    td = await factories.make_thread_decision(
        db_session, agent_a=agent.agent_id, agent_b="beta", outcome="proposal"
    )
    db_session.add(
        ProposalReview(
            thread_decision_id=td.id,
            agent_id=agent.agent_id,
            user_id=pi.id,
            rating=3,
            submitted_via="web",
        )
    )
    await db_session.flush()

    resp = await client.get("/admin/agents", headers=_auth(admin.id))
    assert resp.status_code == 200
    html = resp.text

    row_start = html.index(agent.agent_id)
    row = html[row_start : row_start + 1500]

    assert "1 reviewed" in row
    assert "1 to review" not in row


async def test_a_review_of_a_non_proposal_decision_is_not_counted_as_reviewing_a_proposal(
    client, db_session, admin
):
    """The reviewed count must be scoped to the rows the total counts.

    `proposal_counts` counts decisions with `outcome='proposal'` that this agent
    participated in; the reviewed count used to count every ProposalReview bearing the
    agent's id, including reviews of decisions whose outcome later moved off 'proposal'.
    `total - reviewed` then went negative — measured on production data: 22 of 53 active
    agents, with the roster reporting 98 outstanding proposals against 166 actually
    outstanding, and the template rendering a green "N reviewed" for agents whose own
    dashboards still listed review forms (issue #20 closure audit).
    """
    pi = await factories.make_user(db_session, email="scope@example.org")
    agent = await factories.make_agent(
        session=db_session, user=pi, agent_id="scopey", bot_name="ScopeyBot", status="active"
    )
    # One real, unreviewed proposal.
    await factories.make_thread_decision(
        db_session, agent_a=agent.agent_id, agent_b="beta", outcome="proposal"
    )
    # ...and a review attached to a decision that is NOT a proposal. It must not be
    # allowed to cancel out the proposal above.
    other = await factories.make_thread_decision(
        db_session, agent_a=agent.agent_id, agent_b="beta", outcome="no_proposal"
    )
    db_session.add(
        ProposalReview(
            thread_decision_id=other.id,
            agent_id=agent.agent_id,
            user_id=pi.id,
            rating=3,
            submitted_via="web",
        )
    )
    await db_session.flush()

    resp = await client.get("/admin/agents", headers=_auth(admin.id))
    assert resp.status_code == 200
    row_start = resp.text.index(agent.agent_id)
    row = resp.text[row_start : row_start + 1500]

    assert "1 to review" in row, "the unreviewed proposal must still be reported"
    assert "(1 total)" in row
    assert "1 reviewed" not in row
