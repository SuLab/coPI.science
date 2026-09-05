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
redirect), and a uniqueness constraint backs it at the DB level — except that the
engine's implicit `rating=-1` marker is upgraded in place by the first explicit action.
The two agents on a proposal transition independently. `rating=0` is not reachable
through `/review` — the form validates 1..4 — so it is genuinely a reopen sentinel and
not a rating.
"""

import base64
import html
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import boto3
import pytest
import slack_sdk
from fastapi import HTTPException
from itsdangerous import TimestampSigner
from sqlalchemy import func, select, text

from src.agent.agent import Agent
from src.agent.message_log import LogEntry
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState
from src.config import get_settings
from src.models import (
    AgentChannel,
    AgentDelegate,
    AgentMessage,
    AgentRegistry,
    EmailEngagementTracker,
    EmailNotification,
    PrivateChannelMember,
    ProposalReview,
    ThreadDecision,
)
from src.routers import agent_page
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


async def test_an_explicit_web_review_upgrades_the_engines_implicit_rating_marker(
    client, db_session, lab, proposal,
):
    """D6/COR-13 (ruling-D6-implicit-review-upsert.md): Task 20.9 has the engine
    persist an implicit ProposalReview(rating=-1, submitted_via="engine") the first
    time a PI engages a proposal thread in Slack/DB. That row is NOT "already acted
    on" — pre-ruling, the SELECT guard above treated ANY existing row as terminal and
    returned 400 "Already reviewed", making the review form 20.9c re-shows a dead end.
    The first explicit web review must upgrade that row in place instead.

    Control for the same file: `test_a_decided_review_cannot_be_re_decided` above pins
    that an existing REAL rating (!= -1) still produces today's 400 rejection.
    """
    implicit = ProposalReview(
        thread_decision_id=proposal.id,
        agent_id="alpha",
        user_id=lab.pi_a_id,
        rating=-1,
        comment=None,
        submitted_via="engine",
        reviewed_at=datetime(2020, 1, 1, tzinfo=UTC),
    )
    db_session.add(implicit)
    await db_session.flush()
    implicit_id, old_reviewed_at = implicit.id, implicit.reviewed_at

    r = await client.post(
        f"/agent/alpha/proposals/{proposal.id}/review",
        data={"rating": "3", "comment": "great"}, headers=_auth(lab.pi_a_id),
    )
    assert r.status_code == 302, r.text[:400]

    db_session.expire_all()
    rows = (await db_session.execute(
        select(ProposalReview).where(ProposalReview.thread_decision_id == proposal.id)
    )).scalars().all()
    assert len(rows) == 1, (
        f"the implicit marker must be upgraded in place, not duplicated: {rows}"
    )
    (review,) = rows
    assert review.id == implicit_id, "the same row must be reused (upsert, not a second insert)"
    assert review.rating == 3
    assert review.comment == "great"
    assert review.submitted_via == "web"
    assert review.user_id == lab.pi_a_id
    assert review.reviewed_by_user_id == lab.pi_a_id
    assert review.delegate_user_id is None, "the PI acted in person"
    assert review.reviewed_at > old_reviewed_at, (
        "reviewed_at must be bumped to when the explicit action happened, not left at "
        "the engine's implicit-marker timestamp"
    )


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


async def test_review_proposal_recovery_with_no_winning_row_returns_a_clean_response(
    db_session, lab, llm, proposal,
):
    """N2 (#24 closure audit; final-C-races-brief.md commit 3): review_proposal's
    ``except IntegrityError`` arm re-selected the presumed race winner with
    ``.scalar_one()`` -- correct when the IntegrityError really was the
    review-uniqueness conflict, but ANY OTHER IntegrityError also lands in this same
    except arm (e.g. an FK/NOT-NULL violation from a concurrently deleted User), and
    ``.scalar_one()`` then finds no row and raises ``NoResultFound`` -- a 500 out of
    the very handler that exists to avoid one. ``reopen_proposal``'s sibling recovery
    already uses ``.scalar_one_or_none()`` (agent_page.py:942-969, logging and
    continuing instead of crashing); this pins that ``review_proposal`` now matches.

    **R8 / #24 V5 (Task 14): why the 302 in this test became a 409.** N2's fix was
    right that the arm must not raise ``NoResultFound``; it was wrong about what a
    "clean response" is. ``winner is None`` means the rollback threw the PI's insert
    away AND no other row took its place, so *nothing was persisted* -- and the arm
    answered a redirect byte-identical to the success path while retiring the
    outstanding ``EmailNotification``, so the PI saw the normal post-review page, lost
    their rating and comment, and no reminder chased the proposal either. The arm now
    raises ``HTTPException(409)``, copied from ``post_agent_message``'s own
    rollback-then-409 (agent_page.py:1461-1466) -- the in-repo pattern issue #24's
    ``Fix:`` clause names -- and leaves the notification ``sent``. The redirect
    carriers this route also owns (``?slack_error=``/``?delegate_error=``) were
    rejected because ``templates/agent/dashboard.html`` renders both only inside
    ``{% if agent.status == 'active' %}`` (and ``delegate_error`` only inside
    ``{% if is_owner %}``), while this handler deliberately serves inactive agents and
    delegates; see docs/plans/2026-09-04-decisions/task-14.md.

    What N2 pinned and this test still pins: no 500, no ``NoResultFound``, and no
    partial ``ProposalReview`` row left behind.

    Reproduced with a genuine, non-uniqueness IntegrityError, not a scripted fake:
    a delegate reviews on "alpha"'s behalf while "alpha"'s PI (`lab.pi_a_id`) is
    deleted out from under the request. ``agents.user_id -> users.id`` is
    ``ON DELETE SET NULL``, so "alpha" survives with ``user_id=NULL`` -- a fresh
    ``get_agent_with_access`` SELECT sees that NULL, so the delegate path (not PI
    ownership) is what authorizes the request. ``review_proposal``'s happy-path INSERT
    always writes ``user_id=agent.user_id`` ("Always the PI"), which is now NULL, and
    ``proposal_reviews.user_id`` is NOT NULL -- so ``record_engagement``'s autoflush
    raises a genuine ``IntegrityError`` that is NOT the review-uniqueness conflict.
    """
    delegate = await factories.make_user(db_session, name="Delegate Dee", email="dee@lab.test")
    db_session.add(AgentDelegate(agent_registry_id=lab.reg_a_id, user_id=delegate.id))
    # The outstanding reminder this branch must NOT retire. Hung off the DELEGATE, not
    # the PI: email_notifications.user_id is ON DELETE CASCADE (models/
    # email_notification.py:19-23), so a PI-owned row would vanish with the DELETE
    # below and "still sent" would be unfalsifiable. mark_notification_responded is
    # scoped to agent_registry_id + thread_decision_id, not to the responder (V4-4b),
    # so this row is exactly what the arm used to flip.
    notification = EmailNotification(
        user_id=delegate.id, thread_decision_id=proposal.id,
        agent_registry_id=lab.reg_a_id, reply_token=f"tok-{uuid.uuid4().hex}",
        category="proposal_review", status="sent",
    )
    db_session.add(notification)
    # Release lab/proposal/delegate's setup as a savepoint boundary BEFORE the
    # destructive delete, so review_proposal's own rollback() (triggered by the
    # IntegrityError below) discards only the failed insert attempt, not the fixture.
    await db_session.commit()
    notification_id = notification.id
    await db_session.execute(text("DELETE FROM users WHERE id = :u"), {"u": lab.pi_a_id})
    await db_session.commit()

    current_user = SimpleNamespace(id=delegate.id, name="Delegate Dee")
    with pytest.raises(HTTPException) as raised:
        await agent_page.review_proposal(
            agent_id="alpha", thread_decision_id=proposal.id, request=SimpleNamespace(),
            rating=3, comment="", db=db_session, current_user=current_user,
        )

    # Still N2's assertion, restated: HTTPException is a handled, coded response, so
    # the arm is still not raising NoResultFound or any other 500 out of the handler.
    assert raised.value.status_code == 409, (
        "a non-uniqueness IntegrityError must answer a coded 4xx that tells the PI "
        "nothing was saved, not a 302 indistinguishable from a successful review "
        f"(got {raised.value.status_code})"
    )
    assert "retry" in str(raised.value.detail).lower(), (
        f"the 409 must tell the PI what to do; detail was {raised.value.detail!r}"
    )

    db_session.expire_all()
    reviews = (await db_session.execute(
        select(ProposalReview).where(ProposalReview.thread_decision_id == proposal.id)
    )).scalars().all()
    assert reviews == [], "the failed insert must not have left a partial row behind"

    # The half of R8 the response code cannot cover: nothing was persisted, so the
    # reminder loop must keep chasing this proposal. The positive control that this is
    # not simply "mark_notification_responded never runs" is
    # test_reviewing_on_the_web_retires_the_outstanding_email_notification, which
    # requires a SUCCESSFUL review to flip the same row to 'responded'.
    after = await db_session.get(EmailNotification, notification_id)
    assert after.status == "sent", (
        "the outstanding reminder was retired for a review that was never persisted "
        f"(status={after.status!r}) -- no e-mail will ever chase this proposal again"
    )
    assert after.responded_at is None and after.response_type is None


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

    # Hermetic: this test's premise is an unrestricted outbound allowlist (the
    # field's own default, "" = send to everyone) — the fictitious lab.pi_*_email
    # addresses must reach _process_user_notifications' is_allowed_recipient
    # check. Pin it rather than inherit the deployed .env's
    # OUTBOUND_EMAIL_ALLOWLIST on this host, which would otherwise suppress
    # both sends and turn `sent` into 0.
    monkeypatch.setattr(get_settings(), "outbound_email_allowlist", "")

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


async def test_a_delegates_review_also_retires_the_pis_own_notification(
    client, db_session, lab, proposal,
):
    """V4-4b: mark_notification_responded is scoped to agent_registry_id, not the
    responding user, so a delegate's review also retires the PI's own outstanding
    reminder about the same agent's proposal. Without this the PI's row stayed 'sent'
    forever, with no way to clear it themselves — a second /review hits 'Already
    reviewed' before mark_notification_responded ever runs (agent_page.py's
    existing-review check matches on thread_decision_id + agent_id, not on who is
    asking).
    """
    delegate = await factories.make_user(
        db_session, name="Deledda Delegate", email="delegate@lab.test"
    )
    db_session.add(AgentDelegate(agent_registry_id=lab.reg_a_id, user_id=delegate.id))

    pi_notification = EmailNotification(
        user_id=lab.pi_a_id, thread_decision_id=proposal.id,
        agent_registry_id=lab.reg_a_id, reply_token=f"tok-{uuid.uuid4().hex}",
        category="proposal_review", status="sent",
    )
    other_agents_notification = EmailNotification(
        user_id=lab.pi_b_id, thread_decision_id=proposal.id,
        agent_registry_id=lab.reg_b_id, reply_token=f"tok-{uuid.uuid4().hex}",
        category="proposal_review", status="sent",
    )
    db_session.add_all([pi_notification, other_agents_notification])
    await db_session.flush()
    pi_notification_id = pi_notification.id
    other_agents_notification_id = other_agents_notification.id

    r = await client.post(
        f"/agent/alpha/proposals/{proposal.id}/review",
        data={"rating": "3", "comment": ""}, headers=_auth(delegate.id),
    )
    assert r.status_code == 302

    db_session.expire_all()
    notif = await db_session.get(EmailNotification, pi_notification_id)
    assert notif.status == "responded", (
        "the delegate's review did not retire the PI's own outstanding notification "
        "for this proposal — the PI would be nagged forever, with no self-service way "
        "to clear it"
    )
    other_notif = await db_session.get(EmailNotification, other_agents_notification_id)
    assert other_notif.status == "sent", (
        "reviewing alpha's copy of the proposal retired beta's own outstanding "
        "notification too -- mark_notification_responded must stay scoped to "
        "agent_registry_id, not cross-retire the other agent on the same proposal"
    )


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
        # Offset each instance's ts counter. FakeSlackClient starts every instance's
        # `_ts` at the same constant, which two real Slack calls minutes apart never
        # would, so a test that drives a SECOND migration (the retry pin below) would
        # otherwise trip uq_agent_messages_run_ts on the handover rows and report a
        # constraint violation instead of the duplicate channel it is looking for.
        # Same reason as test_email_inbound_reply_paths.py's `slack_migration_stub`.
        c._ts += len(made) * 100_000
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


async def test_an_explicit_web_reopen_upgrades_the_engines_implicit_rating_marker(
    client, db_session, lab, proposal, slack_off,
):
    """D6/COR-13 (ruling-D6-implicit-review-upsert.md): same rule for reopen_proposal.
    Pre-ruling, the `already_reviewed` guard treated the engine's implicit rating=-1
    row as a completed reopen and silently redirected — the Slack migration never ran
    and the PI's guidance was dropped.

    Control for the same file: `test_a_rated_proposal_cannot_then_be_reopened_by_the_
    same_agent` above pins that an existing REAL rating (!= -1) still swallows the
    reopen.
    """
    implicit = ProposalReview(
        thread_decision_id=proposal.id,
        agent_id="alpha",
        user_id=lab.pi_a_id,
        rating=-1,
        comment=None,
        submitted_via="engine",
        reviewed_at=datetime(2020, 1, 1, tzinfo=UTC),
    )
    db_session.add(implicit)
    await db_session.flush()
    implicit_id, old_reviewed_at = implicit.id, implicit.reviewed_at

    guidance = "Actually, let's narrow the scope first."
    r = await client.post(
        f"/agent/alpha/proposals/{proposal.id}/reopen",
        data={"guidance": guidance}, headers=_auth(lab.pi_a_id),
    )
    assert r.status_code == 302, r.text[:400]

    db_session.expire_all()
    td = await _decision(db_session, proposal.thread_id)
    assert td.refined_in_channel is not None, (
        "the Slack migration never ran — the implicit marker was treated as an "
        "already-completed reopen"
    )

    rows = (await db_session.execute(
        select(ProposalReview).where(ProposalReview.thread_decision_id == proposal.id)
    )).scalars().all()
    assert len(rows) == 1, (
        f"the implicit marker must be upgraded in place, not duplicated: {rows}"
    )
    (review,) = rows
    assert review.id == implicit_id, "the same row must be reused (upsert, not a second insert)"
    assert review.rating == 0
    assert review.comment.startswith("[Reopened] "), review.comment
    assert guidance in review.comment
    assert review.submitted_via == "web"
    assert review.reviewed_by_user_id == lab.pi_a_id
    assert review.reviewed_at > old_reviewed_at, (
        "reviewed_at must be bumped to when the explicit action happened, not left at "
        "the engine's implicit-marker timestamp"
    )


async def test_reopen_leaves_a_concurrent_real_review_alone(
    client, db_session, lab, proposal, monkeypatch,
):
    """D6/COR-13 fix round 1 (ruling-D6-implicit-review-upsert.md): the SECOND
    `ProposalReview` SELECT in `reopen_proposal` happens AFTER
    `migrate_public_thread_to_private` -- a multi-call Slack round-trip -- precisely
    because a row can appear or change in that window. Pre-fix the branch was
    `if existing_row is not None: <upgrade>`, so a REAL review filed concurrently (the
    PI rating on the still-rendered dashboard, a delegate, or an e-mail reply) while the
    migration ran got overwritten with rating=0 / "[Reopened] ..." / submitted_via="web"
    / a fresh reviewed_at.

    Control: `test_an_explicit_web_reopen_upgrades_the_engines_implicit_rating_marker`
    above pins that an untouched -1 marker IS still upgraded.
    """
    implicit = ProposalReview(
        thread_decision_id=proposal.id,
        agent_id="alpha",
        user_id=lab.pi_a_id,
        rating=-1,
        comment=None,
        submitted_via="engine",
        reviewed_at=datetime(2020, 1, 1, tzinfo=UTC),
    )
    db_session.add(implicit)
    await db_session.flush()
    implicit_id = implicit.id

    async def _migrate_then_race(
        db, *, thread_decision, creator_agent_id, creator_pi_user, guidance_text,
    ):
        # While this (fake) Slack round-trip is "in flight", a real review is filed
        # for the SAME (thread_decision, agent) -- the ruling's exact race window.
        real_review = (await db.execute(
            select(ProposalReview).where(
                ProposalReview.thread_decision_id == thread_decision.id,
                ProposalReview.agent_id == creator_agent_id,
            )
        )).scalar_one()
        real_review.rating = 3
        real_review.comment = "Rated for real while the migration ran."
        real_review.submitted_via = "web"
        await db.commit()
        return SimpleNamespace(channel_name="fake-priv-channel-race")

    monkeypatch.setattr(
        "src.services.private_channels.migrate_public_thread_to_private",
        _migrate_then_race,
    )

    r = await client.post(
        f"/agent/alpha/proposals/{proposal.id}/reopen",
        data={"guidance": "Actually, let's narrow the scope first."},
        headers=_auth(lab.pi_a_id),
    )
    assert r.status_code == 302, r.text[:400]

    db_session.expire_all()
    rows = (await db_session.execute(
        select(ProposalReview).where(ProposalReview.thread_decision_id == proposal.id)
    )).scalars().all()
    assert len(rows) == 1, (
        f"the reopen must not insert a second row alongside the concurrent real "
        f"review: {rows}"
    )
    (review,) = rows
    assert review.id == implicit_id
    assert review.rating == 3, (
        "a real review filed while the migration ran was overwritten by the reopen's "
        "rating=0 marker — the ruling requires it be left alone"
    )
    assert review.comment == "Rated for real while the migration ran.", (
        "the concurrent real review's comment was clobbered by '[Reopened] ...'"
    )
    assert review.submitted_via == "web"


async def test_reopen_write_race_does_not_500_and_recovers_refined_in_channel(
    client, db_session, lab, proposal, slack_off, monkeypatch,
):
    """I2 (#24 V5, audit-issue-24.md): before this fix, reopen_proposal's write block
    (`db.add(review)` through `commit()`) had no `except IntegrityError` -- the exact
    guard Task 24.2 added to review_proposal, left off its sibling. With no existing
    review, the reopen takes the INSERT branch; migrate_public_thread_to_private has
    already created the private Slack channel and (flush-only) set
    `refined_in_channel` by this point. If a concurrent writer wins the race on
    `uq_proposal_reviews_decision_agent`, the pre-fix code 500s, `get_db` rolls back,
    and the channel that already exists for real is orphaned (refined_in_channel and
    the AgentChannel rows are gone) -- and a PI retry then passes the `:695` `!= -1`
    guard (D6) and migrates AGAIN, minting a SECOND channel.

    Harness note (empirically confirmed, not just asserted): `db_session`
    (tests/conftest.py) binds one nested SAVEPOINT inside a single outer transaction
    that is only ever rolled back at teardown -- the `lab`/`proposal` fixture rows are
    never truly COMMITted to the physical database, only savepoint-released within
    that same open transaction. A genuinely separate connection therefore cannot see
    them at all (verified directly: a row inserted and `db_session.commit()`'d, then
    queried from a second `engine.connect()`, comes back with count 0) -- and a
    RELEASE SAVEPOINT is not protected against a later ROLLBACK TO an earlier
    SAVEPOINT on the SAME connection either, so no same-connection trick can make a
    "concurrent" row survive `reopen_proposal`'s own `rollback()`. A true two-
    transaction race where the winner survives is therefore only provable with a fake
    session (see `test_concurrent_write_guards.py`'s
    `test_reopen_proposal_upgrades_the_engines_implicit_marker_after_a_lost_race` and
    `test_reopen_proposal_leaves_a_real_winner_alone_after_a_lost_race`); what THIS
    test proves against a real Postgres constraint is the other half of I2: the
    guard actually catches the IntegrityError (no 500) and `refined_in_channel`
    survives the recovery rather than being silently dropped.

    Since the migration commits its own rows (391e545 for the Slack-off path, 34d3c15
    for Slack-on), the retry at the end also exercises the #24 N1-b guard: the losing
    request leaves exactly the production wreckage -- a committed private channel, a
    set `refined_in_channel`, and no review row -- and the second POST must not migrate
    again. `test_a_retried_reopen_after_a_lost_race_does_not_mint_a_second_channel`
    below covers the same ground on both fixtures and counts the Slack calls.

    The race is reproduced by adding a second, competing `ProposalReview` for the SAME
    (thread_decision, agent) from inside `record_engagement` -- the next `db.execute()`
    after `db.add(review)`, so SQLAlchemy's autoflush (default True) is what actually
    raises the real `IntegrityError` on `uq_proposal_reviews_decision_agent`.
    """
    from src.services import email_notifications

    real_record_engagement = email_notifications.record_engagement
    calls = {"n": 0}

    async def _add_a_competing_review_and_flush(user_id, db):
        calls["n"] += 1
        if calls["n"] == 1:
            # Only the FIRST call (inside reopen_proposal's try, right after
            # db.add(review)) stages the race. The except arm's own recovery calls
            # record_engagement again to retire the notification (V4-4b) -- that call
            # must behave normally, not queue a second competing insert.
            db.add(ProposalReview(
                thread_decision_id=proposal.id, agent_id="alpha", user_id=lab.pi_a_id,
                rating=-1, comment=None, submitted_via="engine",
            ))
            await db.flush()
        else:
            await real_record_engagement(user_id, db)

    monkeypatch.setattr(email_notifications, "record_engagement", _add_a_competing_review_and_flush)

    r = await client.post(
        f"/agent/alpha/proposals/{proposal.id}/reopen",
        data={"guidance": "Actually, let's narrow the scope first."},
        headers=_auth(lab.pi_a_id),
    )
    assert r.status_code == 302, f"expected a redirect, not a 500: {r.text[:400]}"

    db_session.expire_all()
    td = await _decision(db_session, proposal.thread_id)
    assert td.refined_in_channel is not None, (
        "the Slack channel already exists for real -- losing refined_in_channel here "
        "orphans it"
    )

    # The two counts below sit on DIFFERENT durability boundaries, which is why one
    # is 0 and the other is 1.
    #
    # The review rows -- `review`'s own insert and the competing one -- both live in
    # this test's single shared savepoint, so `rollback()` really does undo both and
    # the re-select finds no winner ("no winning row was found on re-select" in the
    # captured log). That is the harness, not production: a real concurrent writer
    # commits on its own connection and survives, which is what
    # test_concurrent_write_guards.py's fake-session tests pin.
    #
    # The migration's rows are no longer the caller's to lose. `_migrate_offline`
    # commits them (391e545, #24 N1-a) at the same durability boundary the Slack-on
    # path has drawn since 34d3c15, so the AgentChannel row and `refined_in_channel`
    # outlive this rollback. That is what makes the retry below a test of the ROUTE's
    # idempotency rather than of the savepoint: the retry meets exactly the production
    # state -- no review row, a committed private channel, `refined_in_channel` set.
    # Before 391e545 both numbers were 0 and 1 and the comment here called the leading
    # 0 a shared-savepoint limitation; that was true of the code as it then stood.
    rows = (await db_session.execute(
        select(ProposalReview).where(ProposalReview.thread_decision_id == proposal.id)
    )).scalars().all()
    assert rows == [], (
        "harness limitation, not a new assertion about correct behaviour: within one "
        "shared savepoint neither insert can outlive the other's rollback"
    )
    channel_count_before_retry = await db_session.scalar(
        select(func.count(AgentChannel.id)).where(AgentChannel.visibility == VISIBILITY_COLLAB_PRIVATE)
    )
    assert channel_count_before_retry == 1, (
        "the migration commits its own AgentChannel row, so the lost review write must "
        "not take it down -- if this is 0 the retry below tests nothing"
    )
    channel_id_before_retry = await db_session.scalar(
        select(AgentChannel.channel_id).where(
            AgentChannel.visibility == VISIBILITY_COLLAB_PRIVATE
        )
    )

    r2 = await client.post(
        f"/agent/alpha/proposals/{proposal.id}/reopen",
        data={"guidance": "Retry after the race."}, headers=_auth(lab.pi_a_id),
    )
    assert r2.status_code == 302
    db_session.expire_all()
    channel_count_after_retry = await db_session.scalar(
        select(func.count(AgentChannel.id)).where(AgentChannel.visibility == VISIBILITY_COLLAB_PRIVATE)
    )
    assert channel_count_after_retry == 1, (
        "the retry re-ran migrate_public_thread_to_private and minted a SECOND private "
        "channel on top of the one the lost attempt already created and committed. No "
        "review row survives the race to stop it, so `refined_in_channel` -- which the "
        "migration commits -- is the only durable record that the migration happened, "
        "and reopen_proposal has to read it (#24 N1-b / #21 COR-19.6)"
    )
    td_after = await _decision(db_session, proposal.thread_id)
    assert td_after.refined_in_channel == channel_id_before_retry, (
        "the retry repointed refined_in_channel at a new channel, orphaning the first "
        "-- which already has the handover and the PI's guidance in it"
    )


@pytest.mark.parametrize("slack_fixture", ["slack_off", "slack_on"])
async def test_a_retried_reopen_after_a_lost_race_does_not_mint_a_second_channel(
    client, db_session, lab, proposal, monkeypatch, request, slack_fixture,
):
    """#24 N1-b / #21 COR-19.6 — the web twin of
    `test_email_inbound_reply_paths.py::test_a_retried_inbound_email_does_not_create_a_second_private_channel`.

    `d1146a4` gave the e-mail handler a second idempotency guard and stopped there;
    `reopen_proposal` gated the migration on `enable_private_refinement and
    origin_visibility == "public"` alone and read `refined_in_channel` nowhere. The
    review-row guard at the top of the route is not enough on its own, because the
    review row is exactly what a lost race destroys:

      1. `migrate_public_thread_to_private` commits the AgentChannel, its members, the
         handover and `refined_in_channel` as soon as its side effects are real
         (34d3c15 for Slack-on, 391e545 for Slack-off).
      2. The route's own `db.add(review)` then loses the race on
         `uq_proposal_reviews_decision_agent` and the `except IntegrityError` arm rolls
         it back.
      3. The PI retries (the dashboard still shows the reopen form, or the Back
         button). `already_reviewed` is None again, `origin_visibility` is still
         'public' — the migration never flips it — so the route migrated a SECOND time:
         two AgentChannel rows, a second real Slack channel, and `refined_in_channel`
         repointed at it, orphaning the first channel that already holds the handover
         and the PI's guidance. Both bots keep talking in the dead one.

    Slack does not refuse the second create: `AgentSlackClient.create_private_channel`
    (src/agent/slack_client.py:933-936) appends a fresh `%Y%m%d-%H%M%S` stamp to the
    otherwise-deterministic `priv-{a}-{b}-{origin}` slug on every call, so the retry
    asks for a name that has never existed. (`_migrate_offline` builds its `local:` id
    the same way, at src/services/private_channels.py:344-346.)

    Driven on BOTH fixtures on purpose. The Slack-off path is the one that only became
    testable with 391e545 — before that its rows were flush-only, so the retry saw a
    `refined_in_channel` pointing at rows that no longer existed and a guard reading it
    would have produced *zero* channels. The `local:` / `G_` assertion on the channel id
    below is what keeps the parametrisation honest: it fails if either case silently
    ran the other path.
    """
    from src.services import email_notifications
    from src.services import private_channels as pc

    made = request.getfixturevalue(slack_fixture)  # `slack_on` returns the fakes
    slack_on_path = slack_fixture == "slack_on"

    # Count the migrations themselves rather than inferring them from row counts: on
    # the Slack-off path there is no Slack call to count, and on both paths one call
    # == one channel created. The route imports this name inside the function body, so
    # patching the module attribute catches it.
    migrations: list[str] = []
    real_migrate = pc.migrate_public_thread_to_private

    async def _counting_migrate(db, **kwargs):
        migrations.append(kwargs["thread_decision"].thread_id)
        return await real_migrate(db, **kwargs)

    monkeypatch.setattr(pc, "migrate_public_thread_to_private", _counting_migrate)

    # Same race harness as test_reopen_write_race_does_not_500_and_recovers_
    # refined_in_channel above: a competing ProposalReview for the SAME
    # (thread_decision, agent) is staged from inside the first record_engagement call,
    # which is the next db.execute() after db.add(review), so autoflush raises the real
    # IntegrityError on uq_proposal_reviews_decision_agent.
    real_record_engagement = email_notifications.record_engagement
    calls = {"n": 0}

    async def _add_a_competing_review_and_flush(user_id, db):
        calls["n"] += 1
        if calls["n"] == 1:
            db.add(ProposalReview(
                thread_decision_id=proposal.id, agent_id="alpha", user_id=lab.pi_a_id,
                rating=-1, comment=None, submitted_via="engine",
            ))
            await db.flush()
        else:
            await real_record_engagement(user_id, db)

    monkeypatch.setattr(
        email_notifications, "record_engagement", _add_a_competing_review_and_flush,
    )

    # --- Pass 1: the migration commits; the review write loses the race.
    r1 = await client.post(
        f"/agent/alpha/proposals/{proposal.id}/reopen",
        data={"guidance": "Narrow the scope to the ternary complex first."},
        headers=_auth(lab.pi_a_id),
    )
    assert r1.status_code == 302, f"expected a redirect, not a 500: {r1.text[:400]}"

    db_session.expire_all()
    first = (await db_session.execute(select(AgentChannel).where(
        AgentChannel.visibility == VISIBILITY_COLLAB_PRIVATE
    ))).scalar_one()
    first_channel_id = first.channel_id
    assert migrations == [proposal.thread_id], (
        f"the first reopen did not run the migration exactly once: {migrations}"
    )
    if slack_on_path:
        assert first_channel_id.startswith("G_"), (
            f"the `slack_on` case did not take the Slack path: {first_channel_id}"
        )
        assert sum(len(c.created_channels) for c in made) == 1
    else:
        assert first_channel_id.startswith("local:"), (
            f"the `slack_off` case did not take the DB-only path: {first_channel_id}"
        )

    td = await _decision(db_session, proposal.thread_id)
    assert td.refined_in_channel == first_channel_id

    # Control on the harness: the race really did cost the review row. If one survived,
    # the route's FIRST guard (`already_reviewed is not None and rating != -1`) would
    # block the retry and this test would pass without the refined_in_channel guard.
    assert (await db_session.scalar(select(func.count(ProposalReview.id)).where(
        ProposalReview.thread_decision_id == proposal.id
    ))) == 0, (
        "a review row survived the lost race, so the retry below is blocked by the "
        "review-row guard and proves nothing about the migration guard"
    )

    # --- Pass 2: the PI retries the same action.
    r2 = await client.post(
        f"/agent/alpha/proposals/{proposal.id}/reopen",
        data={"guidance": "Narrow the scope to the ternary complex first."},
        headers=_auth(lab.pi_a_id),
    )
    assert r2.status_code == 302, f"the retry did not redirect: {r2.text[:400]}"

    db_session.expire_all()
    channels = (await db_session.execute(select(AgentChannel).where(
        AgentChannel.visibility == VISIBILITY_COLLAB_PRIVATE
    ))).scalars().all()
    assert len(channels) == 1, (
        f"the retry minted a second private refinement channel: "
        f"{[c.channel_id for c in channels]}"
    )
    assert migrations == [proposal.thread_id], (
        f"the retry re-ran migrate_public_thread_to_private: {migrations}"
    )
    if slack_on_path:
        assert sum(len(c.created_channels) for c in made) == 1, (
            "the retry asked Slack for a second private channel: "
            f"{[ch['name'] for c in made for ch in c.created_channels]}"
        )

    td = await _decision(db_session, proposal.thread_id)
    assert td.refined_in_channel == first_channel_id, (
        "refined_in_channel was repointed by the retry, orphaning the channel that "
        "already holds the handover and the PI's guidance"
    )

    # The retry still has to do the rest of its job — skipping the migration must not
    # turn the whole request into a no-op, or the proposal stays unreviewed forever.
    review = (await db_session.execute(select(ProposalReview).where(
        ProposalReview.thread_decision_id == proposal.id
    ))).scalar_one()
    assert review.rating == 0, (
        "the retry skipped the migration but also failed to record the reopen marker"
    )


@pytest.fixture
def legacy_db_only(monkeypatch):
    """The legacy rollback lever with Slack off — reopen's DB-inbox branch.

    Two independent switches, both pinned rather than relied on: the flag routes past
    `migrate_public_thread_to_private`, and `slack_globally_enabled` (which auto-detects
    from bot tokens, and the `lab` agents have none) routes past the Slack post. This is
    the only reopen path whose PI text lives in a row THIS route has to commit, which is
    what makes it the only one a rollback can destroy.
    """
    monkeypatch.setattr(get_settings(), "enable_private_refinement", False)

    async def _off(db):
        return False

    monkeypatch.setattr("src.services.slack_tokens.slack_globally_enabled", _off)


async def _guidance_rows(db, marker: str) -> int:
    return await db.scalar(
        select(func.count(AgentMessage.id)).where(
            AgentMessage.is_bot.is_(False), AgentMessage.content.contains(marker),
        )
    )


def _stage_a_lost_review_race(monkeypatch, *, td_id, agent_id, user_id):
    """Make the FIRST record_engagement call insert a competing ProposalReview.

    That call is the next `db.execute()` after the route's own `db.add(review)`, so
    SQLAlchemy's autoflush is what raises the real IntegrityError on
    `uq_proposal_reviews_decision_agent` — the same seam
    `test_reopen_write_race_does_not_500_and_recovers_refined_in_channel` uses. The
    except arm calls record_engagement again to retire the notification; that call must
    behave normally.
    """
    from src.services import email_notifications

    real = email_notifications.record_engagement
    calls = {"n": 0}

    async def _race(uid, db):
        calls["n"] += 1
        if calls["n"] == 1:
            db.add(ProposalReview(
                thread_decision_id=td_id, agent_id=agent_id, user_id=user_id,
                rating=-1, comment=None, submitted_via="engine",
            ))
            await db.flush()
        else:
            await real(uid, db)

    monkeypatch.setattr(email_notifications, "record_engagement", _race)
    return calls


async def test_the_slack_off_reopen_writes_the_pis_guidance_to_the_db_inbox(
    client, db_session, lab, proposal, legacy_db_only,
):
    """Control for the lost-race test below: one request, one inbox row.

    Without this, "exactly one row after the race" could be satisfied by a route that
    had started writing the guidance twice, or by one that never wrote it at all.
    """
    marker = _marker()
    r = await client.post(
        f"/agent/alpha/proposals/{proposal.id}/reopen",
        data={"guidance": f"Tighten the aims. {marker}"},
        headers=_auth(lab.pi_a_id),
    )
    assert r.status_code == 302, r.text[:400]
    db_session.expire_all()
    assert await _guidance_rows(db_session, marker) == 1

    row = (await db_session.execute(select(AgentMessage).where(
        AgentMessage.content.contains(marker)
    ))).scalar_one()
    assert row.thread_ts == proposal.thread_id, (
        "the guidance must land in the origin proposal thread, not the channel root"
    )
    assert row.sender_name == f"{lab.pi_a_name} (PI)"
    assert row.is_bot is False, "the engine routes PI handling off is_bot=False"


async def test_a_lost_reopen_race_keeps_the_pis_inbox_guidance(
    client, db_session, lab, proposal, legacy_db_only, monkeypatch,
):
    """#24 V5 (iii) — the recovery arm re-creates the guidance row the rollback ate.

    `record_pi_message` does not commit, deliberately: the e-mail twin needs this row on
    the same commit that retires the notification, so committing early would let a
    retried S3 delivery mint a second one (#21 COR-19.6). That makes durability the
    CALLER's job, and this caller was rolling back without doing it. With Slack off the
    DB inbox is the whole conversation store — nothing was posted anywhere else — so
    losing the row loses what the PI typed, silently: the request still answers 302 and
    the dashboard still says the proposal was reopened.

    Measured before the fix with Task 11's probe: "the PI's inbox guidance row was
    discarded by the lost-race rollback and never re-created (found 0)".

    Harness note, so the count is not over-read. `db_session` runs one shared savepoint,
    so the competing insert cannot outlive the route's own rollback and the arm logs "no
    winning row was found on re-select" — a state production only reaches when the
    IntegrityError was NOT the uniqueness conflict (a concurrently deleted
    ThreadDecision/User), and a retry after that 404s on the missing decision. In the
    real winner-survives race the arm upgrades or keeps the winning row, so the retry
    returns early at the `rating != -1` guard and never reaches this branch twice. What
    this test pins is the arm itself: after it runs, the guidance is in the database.
    """
    marker = _marker()
    _stage_a_lost_review_race(
        monkeypatch, td_id=proposal.id, agent_id="alpha", user_id=lab.pi_a_id,
    )

    r = await client.post(
        f"/agent/alpha/proposals/{proposal.id}/reopen",
        data={"guidance": f"Nail down the geometry first. {marker}"},
        headers=_auth(lab.pi_a_id),
    )
    assert r.status_code == 302, f"expected a redirect, not a 500: {r.text[:400]}"

    db_session.expire_all()
    n = await _guidance_rows(db_session, marker)
    assert n == 1, (
        "the PI's guidance is gone: record_pi_message added it to the session, the "
        "lost-race rollback threw it away, and the recovery arm re-created only the "
        f"review row (found {n} inbox rows). With Slack off this row is the only copy "
        "of what the PI wrote (#24 V5 iii)"
    )


async def test_the_reopen_recovery_arm_survives_the_proposal_row_vanishing(
    client, db_session, lab, proposal, legacy_db_only, monkeypatch,
):
    """#24 V5 (i) — the identical defect N2 fixed in review_proposal's arm (:626-638).

    Not every IntegrityError out of this write block is the review-uniqueness conflict.
    If the ThreadDecision is deleted between this route's SELECT and its flush, the
    ProposalReview insert violates the foreign key instead — and the arm's
    `td_reload = (...).scalar_one()` then raised `NoResultFound` on the re-select, a 500
    out of the very handler that exists to avoid one. Measured before the fix:
    `sqlalchemy.exc.NoResultFound: No row was found when one was required` at
    `agent_page.py:995`, escaping the route.

    The deletion is committed from inside the route's own session (releasing the
    savepoint) because a genuinely separate connection cannot see this test's uncommitted
    fixture rows at all — see the harness note on
    `test_reopen_write_race_does_not_500_and_recovers_refined_in_channel`. The FK
    violation it produces is real, and so is the empty re-select.
    """
    from sqlalchemy import delete

    from src.services import pi_inbox

    real = pi_inbox.record_pi_message

    async def _delete_the_decision_first(db, **kwargs):
        await db.execute(
            delete(ThreadDecision).where(ThreadDecision.id == proposal.id)
        )
        await db.commit()
        return await real(db, **kwargs)

    monkeypatch.setattr(pi_inbox, "record_pi_message", _delete_the_decision_first)

    r = await client.post(
        f"/agent/alpha/proposals/{proposal.id}/reopen",
        data={"guidance": "Guidance against a proposal that is being deleted."},
        headers=_auth(lab.pi_a_id),
    )
    assert r.status_code == 302, (
        f"the recovery arm 500'd on an IntegrityError that was not the uniqueness "
        f"conflict: {r.text[:400]}"
    )
    assert r.headers["location"] == "/agent/alpha/dashboard"

    # Control: the FK violation really happened — no review row was written for a
    # decision that no longer exists.
    db_session.expire_all()
    assert (await db_session.scalar(select(func.count(ThreadDecision.id)).where(
        ThreadDecision.id == proposal.id
    ))) == 0
    assert (await db_session.scalar(select(func.count(ProposalReview.id)).where(
        ProposalReview.thread_decision_id == proposal.id
    ))) == 0


async def test_refined_in_channel_survives_the_lost_race_without_the_arm_re_binding_it(
    client, db_session, engine, lab, proposal, slack_off, monkeypatch,
):
    """verify-deploy finding 1a — the re-bind machinery is dead, and this is why.

    Until 34d3c15/391e545 the recovery arm re-selected the ThreadDecision and re-bound
    `refined_in_channel` from a value captured before the try, because
    `migrate_public_thread_to_private` only FLUSHED it and the rollback undid the flush,
    orphaning a Slack channel that existed for real. Both migration paths now commit it
    themselves (`private_channels.py:415` and `:644`), so the rollback has nothing to
    undo and the re-bind could only ever write back the value it had just read.

    This is the unreachability proof the plan asks for rather than a red-then-green
    test: it passed before the removal too. What it pins is the property the machinery
    claimed to provide, and it is armed against the regression that would bring the
    machinery back — if a migration path stopped committing, the first assertion goes to
    None and the AgentChannel count to 0.

    The second assertion is the direct measurement: a `before_cursor_execute` recorder
    on the test engine shows no UPDATE of `thread_decisions` after the route's
    `ROLLBACK TO SAVEPOINT`. That was already true with the re-bind in place — assigning
    the identical value emits no SQL — which is exactly what "dead" means here.
    """
    from sqlalchemy import event

    seen: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        seen.append(" ".join(statement.split()))

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        _stage_a_lost_review_race(
            monkeypatch, td_id=proposal.id, agent_id="alpha", user_id=lab.pi_a_id,
        )
        r = await client.post(
            f"/agent/alpha/proposals/{proposal.id}/reopen",
            data={"guidance": "Narrow the scope."}, headers=_auth(lab.pi_a_id),
        )
        assert r.status_code == 302, r.text[:400]
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)

    db_session.expire_all()
    channels = (await db_session.execute(select(AgentChannel).where(
        AgentChannel.visibility == VISIBILITY_COLLAB_PRIVATE
    ))).scalars().all()
    assert len(channels) == 1, (
        "the migration's own commit is what keeps its rows through this rollback"
    )
    td = await _decision(db_session, proposal.thread_id)
    assert td.refined_in_channel == channels[0].channel_id, (
        "refined_in_channel must survive the recovery on the migration's commit, not "
        "on a re-bind by the route"
    )

    rollbacks = [i for i, s in enumerate(seen) if "ROLLBACK TO SAVEPOINT" in s]
    assert rollbacks, "the route never rolled back — the race did not fire"
    after = [s for s in seen[rollbacks[-1]:] if s.startswith("UPDATE thread_decisions")]
    assert after == [], (
        f"the recovery arm wrote thread_decisions after the rollback: {after}"
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
