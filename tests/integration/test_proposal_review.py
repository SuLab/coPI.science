"""T11 — the proposal review loop, from a concluded thread up to (but not through)
the email seam.

Scope, stated once so the gap stays visible:

* **In scope.** The agent dashboard rendering a `ThreadDecision`; the `/review` and
  `/reopen` endpoints and the `ProposalReview` rows they write; reopen's post-guidance-
  in-place behavior (fix 9, 2026-08-12 final audit wave — reopen no longer migrates
  anything to a collab_private channel; see §5's module comment below). The engine's
  thread-conclusion path
  (`_check_thread_outcome` -> `_close_thread`) writing a `ThreadDecision` with
  outcome='no_proposal' (the ⏸️ close) or 'timeout' is also in scope and driven for
  real; outcome='proposal' is NOT — see the note on `_conclude_thread` below.
* **Out of scope by instruction.** Everything inside `src/services/email.py` and
  `src/services/email_notifications.py` below `send_proposal_notification`: MIME
  assembly, the Reply-To / unsubscribe token wiring, and the SES call itself. The one
  test that touches the notification path replaces `send_proposal_notification` with a
  recording double and says so in every assertion message, so a reader cannot mistake a
  green run here for "proposal email is covered". It is not covered. See
  `.notes/full-system-test-plan.md` § Global Constraints.

**outcome='proposal' is legacy-only as of the pitch-only reconciliation (Task 7,
docs/plans/2026-08-12-pr34-branch2-engine-reconciliation.md).** The live ✅-confirms-
:memo: handshake that used to write these rows was retired: `_check_thread_outcome` has
no arm left that produces outcome='proposal', and `_check_private_channel_outcome` /
`_finalize_private_proposal` (the collab_private analog) no longer exist at all. The
review/reopen/dashboard machinery below still has to keep serving proposals that
already exist in the DB, so every fixture in this module that needs one (`proposal`,
via `_conclude_thread(outcome="proposal", ...)`) fabricates the row directly instead of
driving the (now nonexistent) live path — see that helper's docstring. This is a
deliberate scope narrowing, not test debt: `test_a_concluded_thread_records_a_proposal_
decision` is the control that pins what the live handshake actually does now (produces
no ThreadDecision at all), alongside the ⏸️ path it still does drive for real.

Dependencies: the database is REAL (the rolled-back `db_session` from
tests/conftest.py). The LLM, Slack and SES are all doubled — and the autouse
`no_outbound_side_effects` fixture below turns any escape into a hard failure rather
than a silent network call.

**The state machine, as the code actually implements it.** There is no status column
anywhere; "reviewed" is the *existence* of a `ProposalReview` row for
(thread_decision_id, agent_id), and the rating column doubles as the discriminator:

    thread concluded (ThreadDecision.outcome='proposal', legacy rows only)  -- no review row
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
    ProposalReview,
    ThreadDecision,
)
from src.visibility import VISIBILITY_COLLAB_PRIVATE
from tests import factories

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
    """Produce a concluded thread's ThreadDecision row and return its thread_id.

    ``outcome='no_proposal'`` drives the REAL conclusion path — replays the ⏸️
    close through the live `_check_thread_outcome` -> `_close_thread` — because
    that arm survived the pitch-only reconciliation (see
    docs/plans/2026-08-12-pr34-branch2-engine-reconciliation.md Task 7).

    ``outcome='proposal'`` does NOT drive the engine. The ✅-confirms-:memo:
    handshake that used to produce these rows was retired by that same task —
    `_check_thread_outcome` has no arm left that can write outcome='proposal'
    (see `_check_private_channel_outcome` too: also gone). A row with this
    outcome is legacy data only, so this branch fabricates exactly the row
    shape a legacy run would have left behind, directly via the DB, so the
    review/reopen/dashboard machinery below — which still has to serve
    existing proposals regardless of how they were created — has one to
    serve. It is NOT simulating reachable behavior: see
    `test_a_concluded_thread_records_a_proposal_decision`'s control pair for
    what the live handshake actually does now (nothing).
    """
    root_ts = f"{1_700_000_000 + len(channel) * 7 + abs(hash(channel)) % 9000}.000100"

    if outcome == "proposal":
        summary = f":memo: **Summary — Joint programme**\n\n{body}"
        db_session.add(ThreadDecision(
            simulation_run_id=lab.run_id,
            thread_id=root_ts,
            channel=channel,
            agent_a="alpha",
            agent_b="beta",
            outcome="proposal",
            summary_text=summary,
        ))
        await db_session.flush()
        db_session.expire_all()
        return root_ts

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
    engine.message_log.append(LogEntry(
        ts=root_ts, channel=channel, sender_agent_id="beta", sender_name="BetaBot",
        content="Opening the discussion.", posted_at=float(root_ts),
    ))
    thread = ThreadState(thread_id=root_ts, channel=channel, other_agent_id="beta")
    agents[0].state.active_threads[root_ts] = thread

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
    """One concluded PROPOSAL thread, fabricated as a legacy row, ready to review.

    ``llm`` is accepted (and unused) only to keep this fixture's shape stable for
    the tests that request it alongside other fixtures needing the double.
    """
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


async def test_the_no_proposal_close_still_produces_a_thread_decision(db_session, lab, llm):
    """Control half 1/2. The ⏸️ close survived the pitch-only reconciliation — pin
    that `_check_thread_outcome` -> `_close_thread` still writes a ThreadDecision
    for the arm that remains, with no ProposalReview yet (concluding a thread
    leaves the proposal UNREVIEWED and waiting; the review row is created only
    by a PI action).
    """
    body = _marker()
    thread_id = await _conclude_thread(
        db_session, lab, llm, channel="cold-lead", outcome="no_proposal", body=body,
    )
    decision = await _decision(db_session, thread_id)

    assert decision.outcome == "no_proposal", (
        f"the ⏸️ close did not record outcome='no_proposal': {decision.outcome}"
    )
    assert {decision.agent_a, decision.agent_b} == {"alpha", "beta"}
    assert (await db_session.scalar(
        select(func.count(ProposalReview.id)).where(
            ProposalReview.thread_decision_id == decision.id
        )
    )) == 0, (
        "a ProposalReview row appeared without any PI action — concluding a thread is "
        "supposed to leave the proposal UNREVIEWED and waiting"
    )


async def test_a_memo_and_check_mark_reply_no_longer_produces_a_thread_decision(
    db_session, lab,
):
    """Control half 2/2 — and the actual regression pin for Task 7.

    Before the pitch-only reconciliation this was
    `test_a_concluded_thread_records_a_proposal_decision`'s "yes" half: replaying
    a `:memo: Summary` + ✅ reply through the REAL, live `_check_thread_outcome`
    used to write a ThreadDecision with outcome='proposal'. That handshake is
    retired now (see the module docstring) — this asserts it does NOTHING: no
    ThreadDecision is written and the thread is not closed. No `llm` double is
    installed because nothing here should reach `_close_thread`, which is the
    only path that would call it.
    """
    channel = "degrader-chem"
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
    root_ts = "1700000000.000100"
    engine.message_log.append(LogEntry(
        ts=root_ts, channel=channel, sender_agent_id="beta", sender_name="BetaBot",
        content="Opening the discussion.", posted_at=float(root_ts),
    ))
    thread = ThreadState(thread_id=root_ts, channel=channel, other_agent_id="beta")
    agents[0].state.active_threads[root_ts] = thread

    summary = f":memo: **Summary — Joint programme**\n\n{_marker()}"
    engine.message_log.append(LogEntry(
        ts=f"{float(root_ts) + 1:.6f}", channel=channel, sender_agent_id="beta",
        sender_name="BetaBot", content=summary, thread_ts=root_ts,
        posted_at=float(root_ts) + 1,
    ))
    await engine._check_thread_outcome(agents[0], thread, "✅ Agreed, let's do it.")

    assert (await db_session.scalar(
        select(func.count(ThreadDecision.id)).where(ThreadDecision.thread_id == root_ts)
    )) == 0, (
        "a ✅ reply to a :memo: Summary wrote a ThreadDecision — the retired "
        "handshake is still live somewhere in _check_thread_outcome"
    )
    assert thread.status != "closed", "the thread was closed by the retired handshake"
    assert root_ts in agents[0].state.active_threads, (
        "the thread was evicted from active_threads by the retired handshake"
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


# ---------------------------------------------------------------------------
# 5. Reopen -> posts guidance in place (fix 9, 2026-08-12 final audit wave;
#    Slack-post branch removed outright in the same date's PI-interaction
#    removal cycle)
#
# reopen used to migrate a public-origin proposal thread into a NEW
# collab_private channel by default before posting the PI's guidance there.
# The engine-side private-channel collaboration/refinement flow was deleted
# (docs/plans/2026-08-12-pr34-pitch-only-reconciliation-design.md §8 — no
# agent converses inside a collab_private channel anymore), so a freshly
# migrated channel would be a dead room nothing ever posts in again. reopen no
# longer creates ANY private channel, and — as of the same date's final
# removal wave — no longer posts to Slack either: there is no PI-bot
# interaction surface left for a bot token to re-engage through, so the DB
# inbox is now the only path, unconditionally.
# ---------------------------------------------------------------------------


async def _reopen_inbox_count(db_session, proposal) -> int:
    """PI-authored (agent_id IS NULL) messages reopen wrote into the origin thread."""
    return await db_session.scalar(select(func.count(AgentMessage.id)).where(
        AgentMessage.thread_ts == proposal.thread_id,
        AgentMessage.agent_id.is_(None),
    ))


async def test_reopen_posts_the_guidance_and_files_the_review_together(
    client, db_session, lab, proposal,
):
    """ONE request produces BOTH the PI-authored inbox message in the origin
    thread AND the rating=0 ProposalReview that marks the proposal acted-on.
    reopen never creates a collab_private channel any more.
    """
    guidance = "Nail down the ternary-complex geometry before any chemistry."
    r = await client.post(
        f"/agent/alpha/proposals/{proposal.id}/reopen",
        data={"guidance": guidance}, headers=_auth(lab.pi_a_id),
    )
    assert r.status_code == 302, r.text[:400]

    db_session.expire_all()
    assert (await db_session.scalar(select(func.count(AgentChannel.id)).where(
        AgentChannel.visibility == VISIBILITY_COLLAB_PRIVATE
    ))) == 0, "reopen must never create a collab_private channel"

    td = await _decision(db_session, proposal.thread_id)
    assert td.refined_in_channel is None

    review = (await db_session.execute(
        select(ProposalReview).where(ProposalReview.thread_decision_id == proposal.id)
    )).scalar_one()
    assert review.rating == 0, (
        f"reopen is supposed to file the rating=0 sentinel, got {review.rating}"
    )
    assert review.comment.startswith("[Reopened] "), review.comment
    assert guidance in review.comment
    assert review.user_id == lab.pi_a_id

    inbox = (await db_session.execute(
        select(AgentMessage).where(
            AgentMessage.thread_ts == proposal.thread_id,
            AgentMessage.agent_id.is_(None),
        )
    )).scalars().all()
    assert len(inbox) == 1, "the guidance never landed in the origin thread's DB inbox"
    assert guidance in inbox[0].content
    assert inbox[0].channel_name == proposal.channel

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


async def test_a_rating_never_writes_reopen_guidance(
    client, db_session, lab, proposal,
):
    """Rating and reopen are two separate PI actions behind two separate
    endpoints; only `/reopen` posts guidance into the origin thread.

    Control: the identical setup, driven through `/reopen` instead, DOES post
    — so "no post" is a fact about the rating action, not about a route that
    stopped working.
    """
    r = await client.post(
        f"/agent/alpha/proposals/{proposal.id}/review",
        data={"rating": "4", "comment": "approved"}, headers=_auth(lab.pi_a_id),
    )
    assert r.status_code == 302
    db_session.expire_all()
    assert await _reopen_inbox_count(db_session, proposal) == 0, (
        "a plain rating wrote a PI-authored message into the origin thread"
    )
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
    assert await _reopen_inbox_count(db_session, proposal) == 1, (
        "the /reopen control did not post either, so the assertion above proves "
        "nothing about the rating action"
    )
    assert (await db_session.scalar(select(func.count(AgentChannel.id)).where(
        AgentChannel.visibility == VISIBILITY_COLLAB_PRIVATE
    ))) == 0, "reopen must never create a collab_private channel"


async def test_a_rated_proposal_cannot_then_be_reopened_by_the_same_agent(
    client, db_session, lab, proposal,
):
    """The two edges out of "awaiting review" are mutually exclusive. Once alpha has
    rated, alpha's reopen is swallowed by the same guard that catches a replayed POST
    — note it is a silent 302, not an error, so the PI gets no feedback that their
    guidance was discarded (observed behaviour, reported).

    Control: beta, which has not acted, CAN still reopen the same proposal — so "no
    post" is a fact about alpha's spent transition, not about the route being
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
    assert await _reopen_inbox_count(db_session, proposal) == 0, (
        "a proposal alpha had already rated was reopened by alpha anyway"
    )
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
    assert await _reopen_inbox_count(db_session, proposal) == 1, (
        "beta's reopen posted nothing either, so the assertion above proves nothing"
    )


async def test_reopen_is_idempotent_under_a_replayed_post(
    client, db_session, lab, proposal,
):
    """A stale page or the Back button replays the reopen POST. The guard must make the
    second one a no-op rather than post a duplicate inbox message.

    Control: the first POST is asserted to have posted exactly once, so "still one
    post" is not satisfied by a reopen that never worked.
    """
    for _ in range(2):
        r = await client.post(
            f"/agent/alpha/proposals/{proposal.id}/reopen",
            data={"guidance": "Same guidance, submitted twice."},
            headers=_auth(lab.pi_a_id),
        )
        assert r.status_code == 302
        db_session.expire_all()
        assert await _reopen_inbox_count(db_session, proposal) == 1

    assert (await db_session.scalar(
        select(func.count(ProposalReview.id)).where(
            ProposalReview.thread_decision_id == proposal.id
        )
    )) == 1, "the replayed reopen filed a second ProposalReview"


async def test_reopen_is_blocked_for_an_inactive_agent_but_rating_is_not(
    client, db_session, lab, proposal,
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
    assert await _reopen_inbox_count(db_session, proposal) == 0

    allowed = await client.post(
        f"/agent/alpha/proposals/{proposal.id}/review",
        data={"rating": "2", "comment": "still allowed to rate"},
        headers=_auth(lab.pi_a_id),
    )
    assert allowed.status_code == 302, (
        "rating was blocked too — the inactive state is not the narrow, "
        f"reopen-only gate it is documented to be: {allowed.status_code}"
    )
