"""The engine's thread-conclusion path and the dashboard, over legacy proposal data.

Scope:

* The engine's thread-conclusion path (`_check_thread_outcome` -> `_close_thread`)
  writing a `ThreadDecision` with outcome='no_proposal' (the ⏸️ close), driven for real
  (the 'timeout' arm is not exercised here).
* The agent dashboard not being another PI's surface.

The proposal review and reopen routes were retired. outcome='proposal' is legacy-only:
`_check_thread_outcome` has no arm that produces it, so the `proposal` fixture
fabricates the row directly. `test_a_memo_and_check_mark_reply_no_longer_produces_a_thread_decision`
pins what the live handshake does now (no ThreadDecision at all), alongside
`test_the_no_proposal_close_still_produces_a_thread_decision` for the ⏸️ path.

The database is REAL (the rolled-back `db_session` from tests/conftest.py). The LLM and
Slack are doubled, and the autouse `no_outbound_side_effects` fixture turns any escape
into a hard failure rather than a silent network call.
"""

import base64
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
    ProposalReview,
    ThreadDecision,
)
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
    workspace another agent owns.
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

    monkeypatch.setattr("src.agent.engine.deps.generate_agent_response", _fake)
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
    docs/plans/2026-08-12-pr34-pitch-only-reconciliation-design.md §8).

    ``outcome='proposal'`` does NOT drive the engine. The ✅-confirms-:memo:
    handshake that used to produce these rows was retired by that same reconciliation —
    `_check_thread_outcome` has no arm left that can write outcome='proposal'
    (see `_check_private_channel_outcome` too: also gone). A row with this
    outcome is legacy data only, so this branch fabricates exactly the row
    shape a legacy run would have left behind, directly via the DB, so the
    dashboard tests have a legacy proposal to ignore. It is NOT simulating
    reachable behavior: see the control pair
    `test_the_no_proposal_close_still_produces_a_thread_decision` /
    `test_a_memo_and_check_mark_reply_no_longer_produces_a_thread_decision` for
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
    # _close_thread now QUEUES its working-memory synthesis calls instead of
    # awaiting them inline (Task 1 of
    # docs/plans/2026-08-21-perf-memory-race-remediation.md) — drain
    # before asserting the double actually ran.
    await engine._drain_memory_events()

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
    """Control half 2/2 — and the actual regression pin for the handshake's retirement.

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

