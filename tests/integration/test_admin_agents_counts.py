"""Live integration test for /admin/agents' review-count column.

The `review_counts` query in src/routers/admin.py's `admin_agents` route (feeding
`templates/admin/agents.html`'s `unreviewed = total_proposals - total_reviewed`)
must not count the engine's implicit `rating = -1` marker row
(src.agent's `_persist_implicit_proposal_review`) as a real PI review, matching
how the badge, the review e-mail, and the dashboard form already treat it.

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
    participated in; the reviewed count must not count ProposalReview rows for
    decisions whose outcome later moved off 'proposal', or `total - reviewed`
    goes negative and the page renders a "N reviewed" count that exceeds what
    the agent's own dashboard still lists as outstanding.
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


# ---------------------------------------------------------------------------
# The reopen marker (rating = 0)
#
# A PI who reopens a proposal with guidance gets a ProposalReview row carrying
# `rating=0`, `comment="[Reopened] …"` and `submitted_via="web"` — a second
# sentinel value alongside `-1`. It is not a score: the dashboard form offers
# 1-4 only and both writers reject anything outside that range
# (`agent_page.py:509`, `email_inbound.py:383`), so no PI can submit a 0.
#
# The reviewed-count query must exclude rating=0 the same way it excludes
# rating=-1, or a reopen marker is counted as a completed review and the
# unreviewed count understates what is actually outstanding.
# ---------------------------------------------------------------------------


async def test_reopen_marker_does_not_count_as_reviewed(client, db_session, admin):
    """A lone rating=0 reopen marker must show as 1 unreviewed / 0 reviewed."""
    pi = await factories.make_user(db_session, email="reopen-count@example.org")
    agent = await factories.make_agent(
        session=db_session, user=pi, agent_id="reopeny", bot_name="ReopenyBot", status="active"
    )
    td = await factories.make_thread_decision(
        db_session, agent_a=agent.agent_id, agent_b="beta", outcome="proposal"
    )
    db_session.add(
        ProposalReview(
            thread_decision_id=td.id,
            agent_id=agent.agent_id,
            user_id=pi.id,
            rating=0,
            comment="[Reopened] please cost the mouse work first",
            submitted_via="web",
        )
    )
    await db_session.flush()

    resp = await client.get("/admin/agents", headers=_auth(admin.id))
    assert resp.status_code == 200
    row_start = resp.text.index(agent.agent_id)
    row = resp.text[row_start : row_start + 1500]

    assert "1 to review" in row, "a reopened proposal is still awaiting a rating"
    assert "(1 total)" in row
    assert "1 reviewed" not in row


async def test_reopen_marker_is_not_rendered_as_a_score_on_admin_discussions(
    client, db_session, admin
):
    """/admin/discussions must not print a reopen marker as "0/4"."""
    pi = await factories.make_user(db_session, email="reopen-disc@example.org")
    agent = await factories.make_agent(
        session=db_session, user=pi, agent_id="discy", bot_name="DiscyBot", status="active"
    )
    reopened = await factories.make_thread_decision(
        db_session,
        agent_a=agent.agent_id,
        agent_b="beta",
        outcome="proposal",
        summary_text="A joint proposal.",
    )
    rated = await factories.make_thread_decision(
        db_session,
        agent_a=agent.agent_id,
        agent_b="beta",
        outcome="proposal",
        summary_text="Another joint proposal.",
    )
    db_session.add_all([
        ProposalReview(
            thread_decision_id=reopened.id,
            agent_id=agent.agent_id,
            user_id=pi.id,
            rating=0,
            comment="[Reopened] please cost the mouse work first",
            submitted_via="web",
        ),
        # Positive control: a real rating on a second proposal must still render.
        ProposalReview(
            thread_decision_id=rated.id,
            agent_id=agent.agent_id,
            user_id=pi.id,
            rating=3,
            submitted_via="web",
        ),
    ])
    await db_session.flush()

    resp = await client.get("/admin/discussions?run_id=all", headers=_auth(admin.id))
    assert resp.status_code == 200
    assert "3/4" in resp.text, "a real PI rating must still be shown"
    assert "0/4" not in resp.text, "a reopen marker is not a score of zero"


async def test_reopen_marker_is_not_rendered_as_a_score_on_the_pi_dashboard(
    client, db_session
):
    """The PI's own dashboard must not tell them they scored a proposal 0/4.

    The dashboard's reviewed/unreviewed split is decided in
    src/routers/agent_page.py, which still admits a rating=0 row into
    ``reviewed``; the template is the last line of defence and is what the PI
    actually reads.
    """
    pi = await factories.make_user(db_session, email="reopen-dash@example.org")
    agent = await factories.make_agent(
        session=db_session, user=pi, agent_id="dashy", bot_name="DashyBot", status="active"
    )
    reopened = await factories.make_thread_decision(
        db_session,
        agent_a=agent.agent_id,
        agent_b="beta",
        outcome="proposal",
        summary_text="A joint proposal.",
    )
    rated = await factories.make_thread_decision(
        db_session,
        agent_a=agent.agent_id,
        agent_b="beta",
        outcome="proposal",
        summary_text="Another joint proposal.",
    )
    db_session.add_all([
        ProposalReview(
            thread_decision_id=reopened.id,
            agent_id=agent.agent_id,
            user_id=pi.id,
            rating=0,
            comment="[Reopened] please cost the mouse work first",
            submitted_via="web",
        ),
        ProposalReview(
            thread_decision_id=rated.id,
            agent_id=agent.agent_id,
            user_id=pi.id,
            rating=3,
            submitted_via="web",
        ),
    ])
    await db_session.flush()

    resp = await client.get(f"/agent/{agent.agent_id}/dashboard", headers=_auth(pi.id))
    assert resp.status_code == 200
    assert "Rating: 3/4" in resp.text, "a real PI rating must still be shown"
    assert "Rating: 0/4" not in resp.text, "a reopen marker is not a score of zero"
