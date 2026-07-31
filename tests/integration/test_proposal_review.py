"""T11 — the proposal review loop, from a concluded thread up to (but not through)
the email seam.

Scope, stated once so the gap stays visible:

* **In scope.** The engine's thread-conclusion path (`_check_thread_outcome` ->
  `_close_thread`) writing a `ThreadDecision`; the agent dashboard rendering it; the
  `/review` and `/reopen` endpoints and the `ProposalReview` rows they write; the
  private-channel migration the reopen action triggers.
* **Out of scope by instruction.** Everything inside `src/services/email.py` and
  `src/services/email_notifications.py` below `send_proposal_notification`: MIME
  assembly, the Reply-To / unsubscribe token wiring, and the SES call itself. The one
  test that touches the notification path replaces `send_proposal_notification` with a
  recording double and says so in every assertion message, so a reader cannot mistake a
  green run here for "proposal email is covered". It is not covered. See
  `.notes/full-system-test-plan.md` § Global Constraints.

Dependencies: the database is REAL (the rolled-back `db_session` from
tests/conftest.py). The LLM, Slack and SES are all doubled — and the autouse
`no_outbound_side_effects` fixture below turns any escape into a hard failure rather
than a silent network call.

**The state machine, as the code actually implements it.** There is no status column
anywhere; "reviewed" is the *existence* of a `ProposalReview` row for
(thread_decision_id, agent_id), and the rating column doubles as the discriminator:

    thread concluded (ThreadDecision.outcome='proposal')   -- no review row
        -- POST /review   rating 1..4  -->  decided, terminal for this agent
        -- POST /reopen   rating 0     -->  reopened, ALSO terminal for this agent
                                            (+ collab_private channel,
                                             ThreadDecision.refined_in_channel set)

Both edges are one-way and mutually exclusive: the endpoints reject any second action
by the same agent (`/review` with 400 "Already reviewed", `/reopen` with a silent
redirect), and a uniqueness constraint backs it at the DB level. The two agents on a
proposal transition independently. `rating=0` is not reachable through `/review` —
the form validates 1..4 — so it is genuinely a reopen sentinel and not a rating.
"""

import base64
import html
import json
import uuid
from types import SimpleNamespace

import boto3
import pytest
import slack_sdk
from itsdangerous import TimestampSigner
from sqlalchemy import func, select

from src.agent.agent import Agent
from src.agent.message_log import LogEntry
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState
from src.config import get_settings
from src.models import (
    AgentChannel,
    AgentMessage,
    AgentRegistry,
    EmailEngagementTracker,
    EmailNotification,
    PrivateChannelMember,
    ProposalReview,
    ThreadDecision,
)
from src.visibility import VISIBILITY_COLLAB_PRIVATE
from tests import factories
from tests.fakes import FakeSlackClient

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _FixtureSessionFactory:
    """Route a self-opened session (the engine's, the notification worker's) at the
    rolled-back test session.

    Both `SimulationEngine._close_thread` and `check_and_send_notifications` do
    ``async with self.session_factory() as db: ...; await db.commit()``. The test
    session runs in ``create_savepoint`` mode, so that commit only releases a savepoint
    and the outer transaction still rolls back at teardown. ``__aexit__`` must NOT close
    the fixture-owned session. Same shape as tests/integration/test_message_persistence.py.
    """

    def __init__(self, session):
        self._s = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._s

    async def __aexit__(self, *exc):
        return False


def _auth(user_id) -> dict:
    """Forge the signed session cookie SessionMiddleware would issue."""
    signer = TimestampSigner(get_settings().secret_key)
    data = base64.b64encode(json.dumps({"user_id": str(user_id)}).encode())
    return {"Cookie": f"copi-session={signer.sign(data).decode()}"}


@pytest.fixture(autouse=True)
def no_outbound_side_effects(monkeypatch):
    """Belt and braces: nothing in this module may reach SES or Slack.

    Individual tests install their own doubles at a higher level; this exists so that a
    *missed* seam cannot quietly send mail to a real inbox or create a channel in the
    workspace another agent owns. Verified armed: with the recorder in
    `test_the_proposal_notification_is_addressed_...` removed, the real path reaches
    ``boto3.client('ses', region_name=...)`` and hits this.

    Caveat on the loudness, so nobody over-trusts it: `send_proposal_notification`
    wraps its SES call in a bare ``except Exception``, so on that particular path the
    AssertionError is swallowed and logged rather than failing the test. The *block*
    still holds — no SES client is ever constructed — but the thing that actually
    fails a test when a seam is missed is the recorder assertion, not this fixture.
    """

    def _no_ses(*args, **kwargs):
        raise AssertionError(
            "boto3.client() was called from a T11 test. Email delivery is out of "
            "scope for this plan and must never be exercised — see the module "
            f"docstring. args={args!r} kwargs={kwargs!r}"
        )

    def _no_slack(*args, **kwargs):
        raise AssertionError(
            "slack_sdk.WebClient() was constructed from a T11 test. The live "
            "workspace belongs to another agent; every Slack seam here must be "
            "doubled."
        )

    monkeypatch.setattr(boto3, "client", _no_ses)
    monkeypatch.setattr(slack_sdk, "WebClient", _no_slack)
    monkeypatch.setattr("src.agent.slack_client.WebClient", _no_slack)


@pytest.fixture
def llm(monkeypatch):
    """Double for the only LLM call the conclusion path makes (working-memory
    synthesis, `simulation._update_agent_memory`).

    Returns "" so the caller's `if not response: return` short-circuits before it
    writes a memory file to disk. The returned list is the evidence that the double was
    actually installed on the path under test.
    """
    calls: list[dict] = []

    async def _fake(*args, **kwargs):
        calls.append(kwargs)
        return ""

    monkeypatch.setattr("src.agent.simulation.generate_agent_response", _fake)
    return calls


@pytest.fixture
async def lab(db_session):
    """Two active agents, each owned by a PI with an email address, plus a run.

    ``alpha``/``beta`` are deliberately not real roster ids: `slack_tokens.env_token`
    falls back to `Settings.get_slack_tokens()`, which is keyed by real agent ids, so a
    test agent named ``su`` could pick up a production token and flip Slack on.
    """
    run = await factories.make_simulation_run(db_session)
    pi_a = await factories.make_user(
        db_session, name="Ada Alpha", email="ada.alpha@lab.test"
    )
    pi_b = await factories.make_user(
        db_session, name="Bo Beta", email="bo.beta@lab.test"
    )
    reg_a = await factories.make_agent(
        db_session, user=pi_a, agent_id="alpha", bot_name="AlphaBot",
        pi_name="Ada Alpha", status="active",
    )
    reg_b = await factories.make_agent(
        db_session, user=pi_b, agent_id="beta", bot_name="BetaBot",
        pi_name="Bo Beta", status="active",
    )
    await db_session.flush()
    return SimpleNamespace(
        run_id=run.id,
        pi_a_id=pi_a.id, pi_a_email=pi_a.email, pi_a_name=pi_a.name,
        pi_b_id=pi_b.id, pi_b_email=pi_b.email,
        reg_a_id=reg_a.id, reg_b_id=reg_b.id,
    )


def _marker() -> str:
    """A token that cannot appear anywhere else in the rendered page."""
    return f"PROPOSALBODY{uuid.uuid4().hex[:10].upper()}"


async def _conclude_thread(
    db_session, lab, llm_calls, *, channel: str, outcome: str, body: str,
) -> str:
    """Drive the REAL conclusion path and return the thread_id.

    Not a factory call: the point of this task's first bullet is that a concluded
    thread produces the decision row, so the row has to come out of
    `_check_thread_outcome`. ``outcome='proposal'`` replays the :memo:-Summary -> ✅
    handshake; ``outcome='no_proposal'`` replays the ⏸️ close.
    """
    agents = [
        Agent(agent_id="alpha", bot_name="AlphaBot", pi_name="Ada Alpha"),
        Agent(agent_id="beta", bot_name="BetaBot", pi_name="Bo Beta"),
    ]
    engine = SimulationEngine(
        agents=agents,
        slack_clients={},
        session_factory=_FixtureSessionFactory(db_session),
        simulation_run_id=lab.run_id,
    )
    root_ts = f"{1_700_000_000 + len(channel) * 7 + abs(hash(channel)) % 9000}.000100"
    engine.message_log.append(LogEntry(
        ts=root_ts, channel=channel, sender_agent_id="beta", sender_name="BetaBot",
        content="Opening the discussion.", posted_at=float(root_ts),
    ))
    thread = ThreadState(thread_id=root_ts, channel=channel, other_agent_id="beta")
    agents[0].state.active_threads[root_ts] = thread

    if outcome == "proposal":
        summary = f":memo: **Summary — Joint programme**\n\n{body}"
        engine.message_log.append(LogEntry(
            ts=f"{float(root_ts) + 1:.6f}", channel=channel, sender_agent_id="beta",
            sender_name="BetaBot", content=summary, thread_ts=root_ts,
            posted_at=float(root_ts) + 1,
        ))
        await engine._check_thread_outcome(agents[0], thread, "✅ Agreed, let's do it.")
    else:
        engine.message_log.append(LogEntry(
            ts=f"{float(root_ts) + 1:.6f}", channel=channel, sender_agent_id="beta",
            sender_name="BetaBot", content=body, thread_ts=root_ts,
            posted_at=float(root_ts) + 1,
        ))
        await engine._check_thread_outcome(
            agents[0], thread, f"⏸️ No viable overlap. {body}",
        )

    assert llm_calls, (
        "the working-memory synthesis never ran, so the conclusion path was not "
        "actually driven end to end (or the LLM double was installed on the wrong "
        "module and a real Anthropic call was attempted)"
    )
    db_session.expire_all()
    return root_ts


async def _decision(db_session, thread_id: str) -> ThreadDecision:
    return (await db_session.execute(
        select(ThreadDecision).where(ThreadDecision.thread_id == thread_id)
    )).scalar_one()


@pytest.fixture
async def proposal(db_session, lab, llm):
    """One concluded PROPOSAL thread, produced by the engine, ready to review."""
    body = _marker()
    thread_id = await _conclude_thread(
        db_session, lab, llm, channel="degrader-chem", outcome="proposal", body=body,
    )
    td = await _decision(db_session, thread_id)
    return SimpleNamespace(id=td.id, thread_id=thread_id, body=body,
                           channel="degrader-chem")


# ---------------------------------------------------------------------------
# 1. A concluded thread produces the decision the review loop consumes
# ---------------------------------------------------------------------------


async def test_a_concluded_thread_records_a_proposal_decision(db_session, lab, llm):
    """The ✅-confirms-:memo: handshake writes a ThreadDecision with outcome='proposal'
    and the summary text starting at the :memo: marker.

    Control: the SAME engine, same session, driven with ⏸️ instead, writes
    outcome='no_proposal'. Without it, `outcome == 'proposal'` would also be satisfied
    by a `_close_thread` that hard-coded the value.
    """
    yes_body, no_body = _marker(), _marker()
    yes_ts = await _conclude_thread(
        db_session, lab, llm, channel="degrader-chem", outcome="proposal", body=yes_body,
    )
    no_ts = await _conclude_thread(
        db_session, lab, llm, channel="cold-lead", outcome="no_proposal", body=no_body,
    )

    yes = await _decision(db_session, yes_ts)
    no = await _decision(db_session, no_ts)

    assert yes.outcome == "proposal", (
        f"the ✅/:memo: handshake did not close the thread as a proposal: {yes.outcome}"
    )
    assert no.outcome == "no_proposal", (
        "the ⏸️ control also came back as 'proposal', so outcome is not being derived "
        f"from the conversation at all: {no.outcome}"
    )
    assert yes.summary_text.startswith(":memo:"), (
        f"the summary was not extracted from the :memo: marker: {yes.summary_text!r}"
    )
    assert yes_body in yes.summary_text
    assert {yes.agent_a, yes.agent_b} == {"alpha", "beta"}
    assert yes.origin_visibility == "public"
    assert yes.refined_in_channel is None, (
        "a freshly concluded thread must not already point at a refinement channel"
    )

    # No ProposalReview exists yet. This is the state machine's real entry point: the
    # review row is created by the PI's action, never by the thread concluding.
    assert (await db_session.scalar(
        select(func.count(ProposalReview.id)).where(
            ProposalReview.thread_decision_id.in_([yes.id, no.id])
        )
    )) == 0, (
        "a ProposalReview row appeared without any PI action — concluding a thread is "
        "supposed to leave the proposal UNREVIEWED and waiting"
    )


# ---------------------------------------------------------------------------
# 2. The dashboard renders it
# ---------------------------------------------------------------------------


async def test_the_dashboard_renders_the_proposal_with_a_review_form(
    client, db_session, lab, llm, proposal,
):
    """The PI's dashboard lists the concluded proposal, with the rate form and the
    reopen form pointed at this proposal's id.

    Control for the absence assertion: a `no_proposal` decision in the same run is NOT
    listed. Asserting only "the proposal is on the page" would also pass for a
    dashboard that listed every thread_decision regardless of outcome.
    """
    hidden = _marker()
    await _conclude_thread(
        db_session, lab, llm, channel="cold-lead", outcome="no_proposal", body=hidden,
    )

    r = await client.get("/agent/alpha/dashboard", headers=_auth(lab.pi_a_id))
    assert r.status_code == 200, r.text[:400]
    page = r.text

    assert html.escape(proposal.body, quote=True) in page, (
        "the proposal summary is not on the dashboard the PI is asked to review from"
    )
    assert hidden not in page, (
        "a thread that concluded WITHOUT a proposal is being offered for review — the "
        "dashboard is listing every ThreadDecision, not just outcome='proposal'"
    )
    assert f'action="/agent/alpha/proposals/{proposal.id}/review"' in page, (
        "no review form for this proposal — the PI has no way to act"
    )
    assert f'action="/agent/alpha/proposals/{proposal.id}/reopen"' in page, (
        "no reopen form for an ACTIVE agent"
    )
    assert "Proposals Awaiting Your Review" in page


async def test_the_dashboard_is_not_another_pis_review_surface(
    client, db_session, lab, proposal,
):
    """Authorization, asserted as a pair: the owning PI gets 200, an unrelated
    logged-in user gets 403 on the same URL."""
    outsider = await factories.make_user(db_session, email="nosy@lab.test")
    await db_session.flush()

    owner = await client.get("/agent/alpha/dashboard", headers=_auth(lab.pi_a_id))
    other = await client.get("/agent/alpha/dashboard", headers=_auth(outsider.id))
    assert owner.status_code == 200
    assert other.status_code == 403, (
        f"a user with no relationship to agent 'alpha' reached its dashboard: "
        f"{other.status_code}"
    )


# ---------------------------------------------------------------------------
# 3. The transitions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rating,label",
    [(1, "Not a good idea"), (4, "Excellent idea")],
    ids=["reject", "approve"],
)
async def test_rating_transitions_the_proposal_to_reviewed(
    client, db_session, lab, proposal, rating, label,
):
    """Approve (4) and reject (1) are the same transition with a different payload:
    unreviewed -> reviewed, recorded as one ProposalReview row.

    Both directions are asserted on the DB row AND on the re-rendered page, because a
    row written but never surfaced would leave the PI staring at a proposal they have
    already decided.
    """
    r = await client.post(
        f"/agent/alpha/proposals/{proposal.id}/review",
        data={"rating": str(rating), "comment": f"{label} — because reasons."},
        headers=_auth(lab.pi_a_id),
    )
    assert r.status_code == 302, r.text[:400]
    assert r.headers["location"] == "/agent/alpha/dashboard"

    db_session.expire_all()
    review = (await db_session.execute(
        select(ProposalReview).where(ProposalReview.thread_decision_id == proposal.id)
    )).scalar_one()
    assert review.rating == rating
    assert review.agent_id == "alpha"
    assert review.user_id == lab.pi_a_id, "the row must be attributed to the PI"
    assert review.reviewed_by_user_id == lab.pi_a_id
    assert review.delegate_user_id is None, (
        "the PI acted in person; delegate_user_id is only for a delegate's review"
    )
    assert review.submitted_via == "web"
    assert review.comment == f"{label} — because reasons."

    page = (await client.get(
        "/agent/alpha/dashboard", headers=_auth(lab.pi_a_id))).text
    assert f"Rating: {rating}/4" in page, (
        "the decided proposal is not shown with its rating in the Reviewed section"
    )
    assert f'action="/agent/alpha/proposals/{proposal.id}/review"' not in page, (
        "the review form is still on the page after the proposal was decided — the "
        "dashboard has not moved it out of the awaiting-review list"
    )


async def test_out_of_range_ratings_are_rejected_and_write_nothing(
    client, db_session, lab, proposal,
):
    """0 and 5 are refused with 400 and leave no row.

    Positive control in the same test: 3 is accepted and DOES write a row, so "no row"
    is not the answer this endpoint gives to everything. 0 matters specifically —
    `reopen_proposal` writes rating=0 as its sentinel, and the rating form must not be
    able to mint that state directly.
    """
    for bad in ("0", "5", "-1"):
        r = await client.post(
            f"/agent/alpha/proposals/{proposal.id}/review",
            data={"rating": bad, "comment": ""}, headers=_auth(lab.pi_a_id),
        )
        assert r.status_code == 400, f"rating={bad} was not rejected: {r.status_code}"

    db_session.expire_all()
    assert (await db_session.scalar(
        select(func.count(ProposalReview.id)).where(
            ProposalReview.thread_decision_id == proposal.id
        )
    )) == 0, "a rejected rating still wrote a ProposalReview row"

    ok = await client.post(
        f"/agent/alpha/proposals/{proposal.id}/review",
        data={"rating": "3", "comment": ""}, headers=_auth(lab.pi_a_id),
    )
    assert ok.status_code == 302
    db_session.expire_all()
    assert (await db_session.scalar(
        select(func.count(ProposalReview.id)).where(
            ProposalReview.thread_decision_id == proposal.id
        )
    )) == 1, "the in-range control did not write a row either — the endpoint is broken"


async def test_a_decided_review_cannot_be_re_decided(
    client, db_session, lab, proposal,
):
    """The control the task asks for: reviewed is terminal for that agent.

    Positive control: the OTHER agent in the same proposal can still review it. The
    lock is per (thread_decision, agent), not "this proposal is now closed to
    everyone" — without this half, a 400 from a globally broken endpoint would look
    like correct idempotency.
    """
    first = await client.post(
        f"/agent/alpha/proposals/{proposal.id}/review",
        data={"rating": "4", "comment": "first word"}, headers=_auth(lab.pi_a_id),
    )
    assert first.status_code == 302

    second = await client.post(
        f"/agent/alpha/proposals/{proposal.id}/review",
        data={"rating": "1", "comment": "changed my mind"}, headers=_auth(lab.pi_a_id),
    )
    assert second.status_code == 400, (
        f"a second review by the same agent was accepted ({second.status_code}) — the "
        "PI's decision is overwritable"
    )
    assert "Already reviewed" in second.text

    db_session.expire_all()
    rows = (await db_session.execute(
        select(ProposalReview).where(ProposalReview.thread_decision_id == proposal.id)
    )).scalars().all()
    assert len(rows) == 1, f"the rejected re-review still wrote a row: {rows}"
    assert rows[0].rating == 4 and rows[0].comment == "first word", (
        "the first decision was mutated by the rejected second attempt"
    )

    # Positive control — the other side of the proposal is still open.
    other = await client.post(
        f"/agent/beta/proposals/{proposal.id}/review",
        data={"rating": "2", "comment": "from the other lab"},
        headers=_auth(lab.pi_b_id),
    )
    assert other.status_code == 302, (
        f"the other agent's PI was ALSO refused ({other.status_code}), so the 400 "
        "above is not evidence of a per-agent lock"
    )
    db_session.expire_all()
    assert sorted(r.rating for r in (await db_session.execute(
        select(ProposalReview).where(ProposalReview.thread_decision_id == proposal.id)
    )).scalars().all()) == [2, 4]


async def test_a_review_cannot_be_filed_against_someone_elses_proposal(
    client, db_session, lab, llm, proposal,
):
    """An agent that is not a participant is refused, and files nothing.

    Control: the participating agent's PI succeeds on the very same proposal id.
    """
    stranger_user = await factories.make_user(db_session, email="gamma@lab.test")
    await factories.make_agent(
        db_session, user=stranger_user, agent_id="gamma", bot_name="GammaBot",
        pi_name="Gia Gamma", status="active",
    )
    await db_session.flush()

    bad = await client.post(
        f"/agent/gamma/proposals/{proposal.id}/review",
        data={"rating": "4", "comment": ""}, headers=_auth(stranger_user.id),
    )
    assert bad.status_code == 403, (
        f"an agent that never took part in the thread reviewed it: {bad.status_code}"
    )
    db_session.expire_all()
    assert (await db_session.scalar(
        select(func.count(ProposalReview.id)).where(
            ProposalReview.agent_id == "gamma"
        )
    )) == 0

    good = await client.post(
        f"/agent/alpha/proposals/{proposal.id}/review",
        data={"rating": "4", "comment": ""}, headers=_auth(lab.pi_a_id),
    )
    assert good.status_code == 302, (
        "the participant control was refused too, so the 403 above proves nothing"
    )


# ---------------------------------------------------------------------------
# 4. The email seam
# ---------------------------------------------------------------------------


async def test_the_proposal_notification_is_addressed_but_the_delivery_leg_is_untested(
    db_session, lab, proposal, monkeypatch,
):
    """THE EMAIL SEAM. Read this before trusting the coverage.

    What this asserts: the notification worker's real selection logic — which users are
    eligible, which proposals count as unreviewed, and how the backlog is counted —
    reaches `send_proposal_notification` with the right recipient and the right
    proposal, for BOTH PIs on the proposal.

    What this deliberately does NOT assert, and what therefore has NO test anywhere in
    this plan: the subject line, the text/HTML bodies, the `review+<token>@…` Reply-To
    that the inbound reply path depends on, the unsubscribe token, the
    `EmailNotification` row `send_proposal_notification` writes, and the SES call. All
    of that lives in `src/services/email_notifications.py`, which the plan excludes by
    instruction. `send_proposal_notification` is replaced wholesale by the recorder
    below, so none of it runs. A green result here means "the system decided to notify
    the right people about the right thing", NOT "the email is correct" and NOT "the
    email was sent".

    Control: the same call, made again after the PIs have reviewed, records nothing.
    Without it, a recorder that fired for every user in the database would look
    identical.
    """
    from src.services import email_notifications as en

    recorded: list[dict] = []

    async def _recording_double(
        *, user, thread_decision, agent, other_bot_name, total_unreviewed, db,
    ):
        recorded.append({
            "to": user.email,
            "thread_decision_id": thread_decision.id,
            "agent_id": agent.agent_id,
            "bot_name": agent.bot_name,
            "other_bot_name": other_bot_name,
            "total_unreviewed": total_unreviewed,
        })
        return True

    monkeypatch.setattr(en, "send_proposal_notification", _recording_double)

    sent = await en.check_and_send_notifications(_FixtureSessionFactory(db_session))

    assert sent == 2, (
        "expected the notification worker to decide to notify BOTH PIs on this "
        f"proposal; it decided to notify {sent}. (Nothing was delivered either way — "
        "the SES leg is stubbed out and out of scope.)"
    )
    by_recipient = {r["to"]: r for r in recorded}
    assert set(by_recipient) == {lab.pi_a_email, lab.pi_b_email}, (
        "wrong recipients queued for the proposal notification: "
        f"{sorted(by_recipient)}. NOTE: no message was composed or sent — this "
        "asserts only the addressing decision, which is the last thing before the "
        "excluded email module."
    )
    for email_addr, expect_bot, expect_other in (
        (lab.pi_a_email, "AlphaBot", "BetaBot"),
        (lab.pi_b_email, "BetaBot", "AlphaBot"),
    ):
        call = by_recipient[email_addr]
        assert call["thread_decision_id"] == proposal.id, (
            f"{email_addr} would be told about the wrong proposal"
        )
        assert call["bot_name"] == expect_bot and call["other_bot_name"] == expect_other, (
            f"{email_addr}'s notification names the wrong pair of bots: {call}. The "
            "rendered subject/body that would carry these names is NOT asserted here "
            "— composition is inside the excluded module."
        )
        assert call["total_unreviewed"] == 1, (
            f"backlog count wrong for {email_addr}: {call['total_unreviewed']}"
        )

    # --- Control: once reviewed, the same worker queues nothing -------------
    for agent_id, pi_id in (("alpha", lab.pi_a_id), ("beta", lab.pi_b_id)):
        db_session.add(ProposalReview(
            thread_decision_id=proposal.id, agent_id=agent_id, user_id=pi_id,
            reviewed_by_user_id=pi_id, rating=3, submitted_via="web",
        ))
    # The double returned True, so `_process_user_notifications` advanced each
    # tracker's send clock. Rewind it, or the second pass would be skipped by the
    # weekly frequency gate and the control would pass for the wrong reason.
    for tracker in (await db_session.execute(
        select(EmailEngagementTracker)
    )).scalars().all():
        tracker.last_notification_sent_at = None
    await db_session.flush()
    recorded.clear()

    again = await en.check_and_send_notifications(_FixtureSessionFactory(db_session))
    assert again == 0 and recorded == [], (
        "a reviewed proposal is still being queued for a reminder email: "
        f"{recorded}"
    )


async def test_reviewing_on_the_web_retires_the_outstanding_email_notification(
    client, db_session, lab, llm, proposal,
):
    """The DB half of the email loop, which is on OUR side of the seam: submitting a
    web review flips the outstanding `EmailNotification` to 'responded' so the worker
    stops nagging. No message is composed, sent, or parsed here.

    Control: an outstanding notification for a DIFFERENT proposal, same user, is left
    alone — otherwise `mark_notification_responded` clearing everything unconditionally
    would pass.
    """
    other_ts = await _conclude_thread(
        db_session, lab, llm, channel="second-topic", outcome="proposal",
        body=_marker(),
    )
    other_td = await _decision(db_session, other_ts)

    mine = EmailNotification(
        user_id=lab.pi_a_id, thread_decision_id=proposal.id,
        agent_registry_id=lab.reg_a_id, reply_token=f"tok-{uuid.uuid4().hex}",
        category="proposal_review", status="sent",
    )
    untouched = EmailNotification(
        user_id=lab.pi_a_id, thread_decision_id=other_td.id,
        agent_registry_id=lab.reg_a_id, reply_token=f"tok-{uuid.uuid4().hex}",
        category="proposal_review", status="sent",
    )
    db_session.add_all([mine, untouched])
    await db_session.flush()
    mine_id, untouched_id = mine.id, untouched.id

    r = await client.post(
        f"/agent/alpha/proposals/{proposal.id}/review",
        data={"rating": "3", "comment": ""}, headers=_auth(lab.pi_a_id),
    )
    assert r.status_code == 302

    db_session.expire_all()
    after = {
        n.id: n for n in (await db_session.execute(select(EmailNotification)))
        .scalars().all()
    }
    assert after[mine_id].status == "responded", (
        "the outstanding reminder for the proposal the PI just reviewed is still "
        "'sent' — the worker will keep emailing about a decided proposal"
    )
    assert after[mine_id].response_type == "review"
    assert after[mine_id].responded_at is not None
    assert after[untouched_id].status == "sent", (
        "reviewing one proposal retired the reminder for an unrelated one"
    )

    tracker = (await db_session.execute(
        select(EmailEngagementTracker).where(
            EmailEngagementTracker.user_id == lab.pi_a_id
        )
    )).scalar_one_or_none()
    if tracker is not None:
        assert tracker.consecutive_missed == 0


# ---------------------------------------------------------------------------
# 5. Reopen -> private channel
# ---------------------------------------------------------------------------


@pytest.fixture
def slack_off(monkeypatch):
    """Force the migration down its DB-only path.

    `_slack_enabled_for_migration` auto-detects from bot tokens. Our agents have none,
    so it would already choose the offline path — but pinning it makes the test's
    intent explicit and immune to a stray token appearing in the environment.
    """
    async def _off(*args, **kwargs):
        return False

    monkeypatch.setattr(
        "src.services.private_channels._slack_enabled_for_migration", _off,
    )


@pytest.fixture
def slack_on(monkeypatch):
    """Force the Slack migration path with a recording fake in place of the real
    client. Returns the list of fakes that were constructed."""
    made: list[FakeSlackClient] = []

    async def _on(*args, **kwargs):
        return True

    async def _token(db, agent_id):
        return f"xoxb-fake-{agent_id}"

    def _client(agent_id, bot_token):
        c = FakeSlackClient(agent_id=agent_id, bot_token=bot_token)
        made.append(c)
        return c

    monkeypatch.setattr(
        "src.services.private_channels._slack_enabled_for_migration", _on)
    monkeypatch.setattr(
        "src.services.private_channels._get_or_fail_bot_token", _token)
    monkeypatch.setattr("src.services.private_channels._make_client", _client)
    return made


async def test_reopen_opens_the_private_channel_and_files_the_review_together(
    client, db_session, lab, proposal, slack_off,
):
    """The wiring assertion the task asks for: ONE request produces BOTH the
    collab_private channel (with its members and handover) and the rating=0
    ProposalReview that marks the proposal acted-on, and it points the decision at the
    new channel.

    They share a transaction on purpose — `migrate_public_thread_to_private` adds rows
    to the caller's session and leaves the commit to the reopen endpoint (see the
    comment in tests/integration/test_slack_private_live.py). If they ever stop
    committing together, one of these two halves disappears.
    """
    guidance = "Nail down the ternary-complex geometry before any chemistry."
    r = await client.post(
        f"/agent/alpha/proposals/{proposal.id}/reopen",
        data={"guidance": guidance}, headers=_auth(lab.pi_a_id),
    )
    assert r.status_code == 302, r.text[:400]

    db_session.expire_all()
    channels = (await db_session.execute(
        select(AgentChannel).where(
            AgentChannel.visibility == VISIBILITY_COLLAB_PRIVATE
        )
    )).scalars().all()
    assert len(channels) == 1, (
        f"expected exactly one private refinement channel, got {len(channels)}"
    )
    ch = channels[0]
    assert ch.created_by_agent == "alpha"
    assert ch.migrated_from_channel_id == f"local:{proposal.channel}", (
        f"the new channel does not record where it came from: "
        f"{ch.migrated_from_channel_id}"
    )
    assert "alpha" in ch.channel_name and "beta" in ch.channel_name

    td = await _decision(db_session, proposal.thread_id)
    assert td.refined_in_channel == ch.channel_id, (
        "the proposal was migrated but the decision row still does not point at the "
        f"refinement channel: {td.refined_in_channel!r} != {ch.channel_id!r}"
    )

    review = (await db_session.execute(
        select(ProposalReview).where(ProposalReview.thread_decision_id == proposal.id)
    )).scalar_one()
    assert review.rating == 0, (
        f"reopen is supposed to file the rating=0 sentinel, got {review.rating}"
    )
    assert review.comment.startswith("[Reopened] "), review.comment
    assert guidance in review.comment
    assert review.user_id == lab.pi_a_id

    members = (await db_session.execute(
        select(PrivateChannelMember).where(
            PrivateChannelMember.agent_channel_id == ch.id
        )
    )).scalars().all()
    assert {m.agent_id for m in members if m.agent_id} == {"alpha", "beta"}
    assert [m.user_id for m in members if m.user_id] == [lab.pi_a_id], (
        "the triggering PI is not a member of the channel that holds their guidance"
    )

    handover = (await db_session.execute(
        select(AgentMessage).where(AgentMessage.channel_id == ch.channel_id)
    )).scalars().all()
    assert any(guidance in (m.content or "") for m in handover), (
        "the PI's guidance never reached the private channel's message history"
    )
    assert all(m.visibility == VISIBILITY_COLLAB_PRIVATE for m in handover)

    origin_rows = (await db_session.execute(
        select(AgentMessage).where(AgentMessage.channel_name == proposal.channel)
    )).scalars().all()
    assert origin_rows, "the public origin thread was left with no closing marker"
    assert not any(guidance in (m.content or "") for m in origin_rows), (
        "the PI's private guidance was echoed into the PUBLIC origin thread"
    )

    # Observed behaviour, pinned because it is surprising rather than because it is
    # right: the rating=0 sentinel puts the reopened proposal in the dashboard's
    # "Reviewed Proposals" section labelled "Rating: 0/4" — on a scale the form only
    # offers 1..4 on. The PI sees a rating they never gave, and the proposal is no
    # longer rateable. Reported as a finding, not fixed here.
    page = (await client.get(
        "/agent/alpha/dashboard", headers=_auth(lab.pi_a_id))).text
    assert "Rating: 0/4" in page, (
        "reopen no longer renders the rating=0 sentinel as a rating — if this was "
        "fixed deliberately, update this assertion; the finding is in the T11 report"
    )
    assert f'action="/agent/alpha/proposals/{proposal.id}/review"' not in page, (
        "the proposal is still rateable after being reopened for refinement"
    )


async def test_a_rating_never_opens_a_private_channel(
    client, db_session, lab, proposal, slack_off,
):
    """FINDING, pinned as a test. Approving a proposal does NOT trigger the
    private-channel reopen — rating and reopen are two separate PI actions behind two
    separate endpoints, and only `/reopen` migrates. See the module report.

    Control: the identical setup, driven through `/reopen` instead, DOES create the
    channel — so "no channel" is a fact about the rating action, not about a migration
    that cannot run in this fixture.
    """
    r = await client.post(
        f"/agent/alpha/proposals/{proposal.id}/review",
        data={"rating": "4", "comment": "approved"}, headers=_auth(lab.pi_a_id),
    )
    assert r.status_code == 302
    db_session.expire_all()
    assert (await db_session.scalar(select(func.count(AgentChannel.id)).where(
        AgentChannel.visibility == VISIBILITY_COLLAB_PRIVATE
    ))) == 0, "a plain rating opened a private refinement channel"
    td = await _decision(db_session, proposal.thread_id)
    assert td.refined_in_channel is None

    # Control: the reopen action on the same proposal, from the other side.
    r2 = await client.post(
        f"/agent/beta/proposals/{proposal.id}/reopen",
        data={"guidance": "Try the orthogonal readout."}, headers=_auth(lab.pi_b_id),
    )
    assert r2.status_code == 302, r2.text[:400]
    db_session.expire_all()
    assert (await db_session.scalar(select(func.count(AgentChannel.id)).where(
        AgentChannel.visibility == VISIBILITY_COLLAB_PRIVATE
    ))) == 1, (
        "the /reopen control did not create a channel either, so the assertion above "
        "proves nothing about the rating action"
    )


async def test_a_rated_proposal_cannot_then_be_reopened_by_the_same_agent(
    client, db_session, lab, proposal, slack_off,
):
    """The two edges out of "awaiting review" are mutually exclusive. Once alpha has
    rated, alpha's reopen is swallowed by the same guard that catches a replayed POST
    — note it is a silent 302, not an error, so the PI gets no feedback that their
    guidance was discarded (observed behaviour, reported).

    Control: beta, which has not acted, CAN still reopen the same proposal — so "no
    channel" is a fact about alpha's spent transition, not about the migration being
    unavailable in this fixture.
    """
    rated = await client.post(
        f"/agent/alpha/proposals/{proposal.id}/review",
        data={"rating": "3", "comment": "decided"}, headers=_auth(lab.pi_a_id),
    )
    assert rated.status_code == 302

    swallowed = await client.post(
        f"/agent/alpha/proposals/{proposal.id}/reopen",
        data={"guidance": "Actually, refine it instead."},
        headers=_auth(lab.pi_a_id),
    )
    assert swallowed.status_code == 302
    db_session.expire_all()
    assert (await db_session.scalar(select(func.count(AgentChannel.id)).where(
        AgentChannel.visibility == VISIBILITY_COLLAB_PRIVATE
    ))) == 0, "a proposal alpha had already rated was reopened by alpha anyway"
    rows = (await db_session.execute(select(ProposalReview).where(
        ProposalReview.agent_id == "alpha"
    ))).scalars().all()
    assert [r.rating for r in rows] == [3], (
        f"the swallowed reopen mutated alpha's decision: {[r.rating for r in rows]}"
    )

    control = await client.post(
        f"/agent/beta/proposals/{proposal.id}/reopen",
        data={"guidance": "The other lab still gets a say."},
        headers=_auth(lab.pi_b_id),
    )
    assert control.status_code == 302
    db_session.expire_all()
    assert (await db_session.scalar(select(func.count(AgentChannel.id)).where(
        AgentChannel.visibility == VISIBILITY_COLLAB_PRIVATE
    ))) == 1, (
        "beta's reopen created nothing either, so the assertion above proves nothing"
    )


async def test_reopen_is_idempotent_under_a_replayed_post(
    client, db_session, lab, proposal, slack_off,
):
    """A stale page or the Back button replays the reopen POST. The guard must make the
    second one a no-op rather than mint a duplicate channel.

    Control: the first POST is asserted to have created exactly one channel, so "still
    one channel" is not satisfied by a reopen that never worked.
    """
    for _ in range(2):
        r = await client.post(
            f"/agent/alpha/proposals/{proposal.id}/reopen",
            data={"guidance": "Same guidance, submitted twice."},
            headers=_auth(lab.pi_a_id),
        )
        assert r.status_code == 302
        db_session.expire_all()
        assert (await db_session.scalar(select(func.count(AgentChannel.id)).where(
            AgentChannel.visibility == VISIBILITY_COLLAB_PRIVATE
        ))) == 1

    assert (await db_session.scalar(
        select(func.count(ProposalReview.id)).where(
            ProposalReview.thread_decision_id == proposal.id
        )
    )) == 1, "the replayed reopen filed a second ProposalReview"


async def test_reopen_drives_the_slack_client_when_slack_is_on(
    client, db_session, lab, proposal, slack_on,
):
    """The Slack-on branch of the same wiring, with a recording fake standing in for
    AgentSlackClient (the live workspace belongs to another agent).

    Asserts the migration really calls Slack — creates a private channel, invites the
    other bot, posts the handover — and that the DB rows still land in the same
    request. `no_outbound_side_effects` guarantees nothing reached slack_sdk.
    """
    guidance = "Push on the kinetics readout, not the chemistry."
    r = await client.post(
        f"/agent/alpha/proposals/{proposal.id}/reopen",
        data={"guidance": guidance}, headers=_auth(lab.pi_a_id),
    )
    assert r.status_code == 302, r.text[:400]

    assert [c.agent_id for c in slack_on] == ["alpha", "beta"], (
        f"the migration did not build a client for each bot: {slack_on}"
    )
    creator = slack_on[0]
    assert creator.created_channels and creator.created_channels[0]["is_private"], (
        "no private channel was requested from Slack"
    )
    new_name = creator.created_channels[0]["name"]
    assert any("U_beta" in inv["users"] for inv in creator.invites), (
        f"the other bot was never invited to the new channel: {creator.invites}"
    )
    posted_here = [p for p in creator.posted if p["channel"] == f"G_{new_name}"]
    assert any(guidance in p["text"] for p in posted_here), (
        f"the guidance was never posted into the private channel: {posted_here}"
    )
    origin_posts = [p for p in creator.posted if p["channel"] == f"C_{proposal.channel}"]
    assert origin_posts and all(guidance not in p["text"] for p in origin_posts), (
        "the origin thread got no close marker, or it leaked the PI's guidance"
    )
    assert all(p["thread_ts"] == proposal.thread_id for p in origin_posts), (
        "the close marker was posted top-level instead of in the origin thread"
    )

    db_session.expire_all()
    ch = (await db_session.execute(select(AgentChannel).where(
        AgentChannel.visibility == VISIBILITY_COLLAB_PRIVATE
    ))).scalar_one()
    assert ch.channel_id == f"G_{new_name}"
    review = (await db_session.execute(select(ProposalReview).where(
        ProposalReview.thread_decision_id == proposal.id
    ))).scalar_one()
    assert review.rating == 0


async def test_a_failed_migration_files_no_review(
    client, db_session, lab, proposal, monkeypatch,
):
    """If Slack refuses the channel, the reopen must leave NOTHING behind — no
    half-written review that would make the proposal look acted-on and permanently
    block the retry (the idempotency guard keys off any review by this agent).

    Positive control: the same request, with the fake repaired, writes both rows.
    """
    refuse = {"on": True}

    async def _on(*args, **kwargs):
        return True

    async def _token(db, agent_id):
        return f"xoxb-fake-{agent_id}"

    class _Refusing(FakeSlackClient):
        def create_private_channel(self, name):
            if refuse["on"]:
                return None
            return super().create_private_channel(name)

    monkeypatch.setattr(
        "src.services.private_channels._slack_enabled_for_migration", _on)
    monkeypatch.setattr(
        "src.services.private_channels._get_or_fail_bot_token", _token)
    monkeypatch.setattr(
        "src.services.private_channels._make_client",
        lambda agent_id, bot_token: _Refusing(agent_id=agent_id, bot_token=bot_token),
    )

    bad = await client.post(
        f"/agent/alpha/proposals/{proposal.id}/reopen",
        data={"guidance": "This one will fail."}, headers=_auth(lab.pi_a_id),
    )
    assert bad.status_code == 500, bad.status_code
    db_session.expire_all()
    assert (await db_session.scalar(select(func.count(ProposalReview.id)).where(
        ProposalReview.thread_decision_id == proposal.id
    ))) == 0, (
        "a failed migration still filed a ProposalReview — the idempotency guard will "
        "now treat every retry as a duplicate and the proposal is stuck"
    )
    assert (await db_session.scalar(select(func.count(AgentChannel.id)).where(
        AgentChannel.visibility == VISIBILITY_COLLAB_PRIVATE
    ))) == 0

    refuse["on"] = False
    good = await client.post(
        f"/agent/alpha/proposals/{proposal.id}/reopen",
        data={"guidance": "Retry after the outage."}, headers=_auth(lab.pi_a_id),
    )
    assert good.status_code == 302, (
        f"the retry control also failed ({good.status_code}); the assertions above "
        "cannot distinguish 'clean abort' from 'reopen never works'"
    )
    db_session.expire_all()
    assert (await db_session.scalar(select(func.count(ProposalReview.id)).where(
        ProposalReview.thread_decision_id == proposal.id
    ))) == 1
    assert (await db_session.scalar(select(func.count(AgentChannel.id)).where(
        AgentChannel.visibility == VISIBILITY_COLLAB_PRIVATE
    ))) == 1


async def test_reopen_is_blocked_for_an_inactive_agent_but_rating_is_not(
    client, db_session, lab, proposal, slack_off,
):
    """The documented asymmetry in agent_page.py: an inactive agent's PI can still rate
    a proposal (passive, DB-only) but cannot reopen it (re-injects the bot into a live
    discussion). Both halves, so neither can silently flip.
    """
    reg = (await db_session.execute(
        select(AgentRegistry).where(AgentRegistry.agent_id == "alpha")
    )).scalar_one()
    reg.status = "inactive"
    await db_session.flush()

    blocked = await client.post(
        f"/agent/alpha/proposals/{proposal.id}/reopen",
        data={"guidance": "Please refine."}, headers=_auth(lab.pi_a_id),
    )
    assert blocked.status_code == 403, (
        f"an inactive agent was reopened into a live discussion: {blocked.status_code}"
    )
    db_session.expire_all()
    assert (await db_session.scalar(select(func.count(AgentChannel.id)).where(
        AgentChannel.visibility == VISIBILITY_COLLAB_PRIVATE
    ))) == 0

    allowed = await client.post(
        f"/agent/alpha/proposals/{proposal.id}/review",
        data={"rating": "2", "comment": "still allowed to rate"},
        headers=_auth(lab.pi_a_id),
    )
    assert allowed.status_code == 302, (
        "rating was blocked too — the inactive state is not the narrow, "
        f"reopen-only gate it is documented to be: {allowed.status_code}"
    )
