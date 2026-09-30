"""`--fresh` must mean fresh: no pre-run Slack history enters the run.

Run 8b64a0e0 (2026-08-22) started with `--fresh`, wiped `agent_messages`, and
then immediately re-imported 914 messages across 86 threads from Slack —
**916 of the 1354 rows attributed to that run were posted before it started**,
the oldest eight days earlier. Three of the seven hub interviews the reconcile
resurrected refused twice on their first turn and were abandoned, burning
139,257 input tokens for no output. See
docs/audits/2026-08-22-run-8b64a0e0/README.md finding M2.

The root cause was a split brain: `--fresh` was implemented entirely in
`src/agent/main.py` (it deleted the rows and opened a new `SimulationRun`),
while `SimulationEngine.start()` called `_rebuild_state_from_slack()`
unconditionally. The engine was never told the run was fresh.

The second test here is the one that matters for `--fresh`: skipping the reconcile
alone did NOT fix the bug, because the live poller reads a SEPARATE cursor map
(`_poll_cursors`, defaulting to "0") that a fresh run populates with nothing.
A RESUME now takes the same cursor-seeding path: the Slack reconcile is gone and
the database is the only thing a restart restores.
"""
import pytest

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from tests.fakes import FakeSlackClient

CH_NAME = "general"
CH_ID = "C_general"

# Two messages that already exist in the Slack channel when the engine starts.
# `1700000001` is deliberately far below any wall clock the test will see, so a
# cursor left at "0" picks them up and a cursor advanced past them does not.
PRE_EXISTING = [
    {"ts": "1700000001.000000", "user": "U_wang", "bot_id": "B1",
     "text": "a pitch from a previous run", "reply_count": 0},
    {"ts": "1700000002.000000", "user": "U_wang", "bot_id": "B1",
     "text": "and the hub's reply to it", "reply_count": 0},
]


def _engine(*, fresh_start: bool) -> SimulationEngine:
    agent = Agent("wang", "WangBot", "Wang", role="pi_lab")
    client = FakeSlackClient(agent_id="wang")
    client.channel_history[CH_ID] = list(PRE_EXISTING)
    eng = SimulationEngine(
        agents=[agent],
        slack_clients={"wang": client},
        fresh_start=fresh_start,
    )
    eng._channel_id_map = {CH_NAME: CH_ID}
    return eng


@pytest.mark.asyncio
async def test_fresh_start_does_not_ingest_pre_existing_slack_history():
    eng = _engine(fresh_start=True)

    await eng._restore_slack_state()

    assert len(eng.message_log) == 0, (
        "--fresh re-imported Slack history from before the run: "
        f"{[e.content for e in eng.message_log._entries]}"
    )


@pytest.mark.asyncio
async def test_fresh_start_poller_ignores_pre_existing_history():
    """The naive fix (gate the reconcile call and nothing else) fails here."""
    eng = _engine(fresh_start=True)

    await eng._restore_slack_state()
    eng._last_channel_poll = 0.0  # defeat the poll interval guard
    await eng._poll_slack_for_bot_messages()

    assert len(eng.message_log) == 0, (
        "the first Slack poll re-ingested pre-run history because the fresh "
        "start left _poll_cursors at \"0\": "
        f"{[e.content for e in eng.message_log._entries]}"
    )


@pytest.mark.asyncio
async def test_fresh_start_is_a_no_op_with_slack_disabled():
    """Structural guard, not a TDD cycle — this passed before the fix too.

    With `SLACK_ENABLED=false` the transports are `NullTransport`, which reports
    `is_connected == False` precisely so the engine's `if client and
    client.is_connected` branches no-op. The fresh-start seed has to honour that
    the same way the reconcile does: a local-DB run has no Slack to read, and an
    unguarded `aget_full_channel_history` on a NullTransport would raise on the
    startup path, i.e. fail the whole run rather than one call.
    """
    from src.agent.transport import NullTransport

    agent = Agent("wang", "WangBot", "Wang", role="pi_lab")
    eng = SimulationEngine(
        agents=[agent],
        slack_clients={"wang": NullTransport("wang")},
        fresh_start=True,
    )
    eng._channel_id_map = {CH_NAME: CH_ID}

    await eng._restore_slack_state()

    assert len(eng.message_log) == 0
    assert eng._poll_cursors == {}


@pytest.mark.asyncio
async def test_a_resumed_run_ingests_nothing_from_slack():
    """A resume rebuilds from the DATABASE only. Slack history is never reconciled
    into the log (the reconcile re-imported every other bot's messages and, across
    runs, other runs' threads), and the live poller starts past it instead of
    re-importing it on the first tick."""
    eng = _engine(fresh_start=False)

    await eng._restore_slack_state()
    assert len(eng.message_log) == 0

    eng._last_channel_poll = 0.0  # defeat the poll interval guard
    await eng._poll_slack_for_bot_messages()
    assert len(eng.message_log) == 0, (
        f"resume ingested Slack history: {[e.content for e in eng.message_log._entries]}"
    )
