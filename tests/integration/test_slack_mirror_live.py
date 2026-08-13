"""The DB<->Slack mirror, against the real workspace.

The highest-value file in the Slack plan: the last five commits on this branch were all
mirror fixes, and every one of them was invisible to the Slack-off suite (Rule S2). The
whole engine test suite runs with NullTransport, so a mirror that silently no-ops looks
identical from inside our own database.

Rule S1 is enforced in every test here: an assertion on `agent_messages.slack_ts` is
paired with a read of that exact ts from Slack.

| commit | fix | test |
|---|---|---|
| 10c240c | stop inferring slack_ts from the channel id | T5.4 |
| 7d8b177 | record the mirror mapping on polled bot messages | T5.3 |
| baa5583 | order "most recent" reads by posted_at | T5.5 |
| a93d136 | never hand a canonical id to Slack | T5.2 |
"""

import time
import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from src.agent.slack_client import ThreadNotFound
from src.models import (
    AgentChannel,
    AgentMessage,
    AgentRegistry,
    Cohort,
    CohortAuditEvent,
    CohortMembership,
    SimulationRun,
)
from src.visibility import VISIBILITY_PUBLIC

pytestmark = [pytest.mark.integration, pytest.mark.live_slack]

AGENTS = ("su", "cravatt", "wiseman")
POST_GAP = 1.1


@pytest.fixture
async def slack_engine(engine, slack_clients, slack_probe_channel, monkeypatch):
    """A real SimulationEngine with real Slack clients and slack_enabled=True.

    Everything the cohort suite does with NullTransport, but with the mirror live. The
    probe channel is registered as the engine's only channel so nothing lands in a
    seeded channel name.
    """
    import src.agent.simulation as sim

    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = uuid.uuid4()
    name, cid = slack_probe_channel

    # _poll_slack_for_human_messages only polls channels whose name is in SEEDED_CHANNELS
    # (or that are collab_private) — polling every public channel would sweep up
    # archived channels from prior sims. The probe channel is neither, so without this
    # the poller would silently skip it and every ingestion test would fail for a
    # reason unrelated to the mirror.
    monkeypatch.setattr(sim, "SEEDED_CHANNELS", [name])

    async with factory() as db:
        db.add(SimulationRun(id=run_id, status="running"))
        for aid in AGENTS:
            db.add(AgentRegistry(agent_id=aid, bot_name=f"{aid.capitalize()}ProbeBot",
                                 pi_name=f"PI {aid}", status="active"))
        await db.commit()

    agents = [Agent(agent_id=a, bot_name=f"{a.capitalize()}ProbeBot", pi_name=f"PI {a}")
              for a in AGENTS]
    eng = SimulationEngine(
        agents=agents, slack_clients=dict(slack_clients), budget_cap=0,
        session_factory=factory, simulation_run_id=run_id, slack_enabled=True,
    )
    eng.message_log.set_bot_name_map({f"{a}probebot": a for a in AGENTS})
    eng._bot_name_to_id = {f"{a}probebot": a for a in AGENTS}
    eng.message_log.set_persist_callback(eng._enqueue_persist)
    eng._channel_id_map = {name: cid}
    eng._channel_visibility = {name: VISIBILITY_PUBLIC}
    for a in eng.agents.values():
        a.state.subscribed_channels = {name}
        a.state.last_seen_cursor = 0.0

    yield eng, factory, run_id, name, cid

    async with factory() as db:
        await db.execute(delete(CohortAuditEvent))
        await db.execute(delete(CohortMembership))
        await db.execute(delete(Cohort))
        await db.execute(delete(AgentMessage).where(AgentMessage.simulation_run_id == run_id))
        await db.execute(delete(AgentChannel).where(AgentChannel.simulation_run_id == run_id))
        await db.execute(delete(AgentRegistry).where(AgentRegistry.agent_id.in_(AGENTS)))
        await db.execute(delete(SimulationRun).where(SimulationRun.id == run_id))
        await db.commit()


async def _rows(factory, run_id):
    async with factory() as db:
        return (await db.execute(
            select(AgentMessage).where(AgentMessage.simulation_run_id == run_id)
            .order_by(AgentMessage.posted_at)
        )).scalars().all()


async def _write_row(factory, run_id, **kw):
    """A row written as if by another process — no Slack presence."""
    defaults = dict(simulation_run_id=run_id, channel_id="C_LOCAL", message_length=10,
                    phase="new_post", visibility=VISIBILITY_PUBLIC, is_bot=True,
                    thread_ts=None)
    defaults.update(kw)
    async with factory() as db:
        db.add(AgentMessage(**defaults))
        await db.commit()


# --- T5.1 -----------------------------------------------------------------------------


async def test_post_message_mirrors_and_records_a_usable_mapping(slack_engine):
    """The row's slack_ts must name a message that really exists in Slack, in the
    channel the row names. A mirror that wrote the row and skipped Slack, or posted to
    Slack and skipped the row, fails one half."""
    eng, factory, run_id, name, cid = slack_engine

    await eng._post_message("su", name, "mirrored post")
    time.sleep(POST_GAP)
    await eng._flush_persisted()

    rows = await _rows(factory, run_id)
    assert len(rows) == 1, [r.content for r in rows]
    row = rows[0]
    assert row.slack_ts, "no slack_ts recorded — the mirror silently no-oped"
    assert row.slack_channel_id == cid, row.slack_channel_id

    live = {m["ts"]: m.get("text")
            for m in eng.slack_clients["su"].poll_channel_messages(cid, oldest="0")}
    assert row.slack_ts in live, (
        f"slack_ts {row.slack_ts} does not exist in Slack: {sorted(live)}"
    )
    assert live[row.slack_ts] == "mirrored post"


async def test_a_threaded_reply_carries_the_parent_mapping(slack_engine):
    eng, factory, run_id, name, cid = slack_engine
    await eng._post_message("su", name, "thread root")
    time.sleep(POST_GAP)
    await eng._flush_persisted()
    root = (await _rows(factory, run_id))[0]

    await eng._post_message("cravatt", name, "thread reply", thread_ts=root.message_ts)
    time.sleep(POST_GAP)
    await eng._flush_persisted()

    reply = [r for r in await _rows(factory, run_id) if r.content == "thread reply"][0]
    assert reply.slack_thread_ts == root.slack_ts, (
        f"reply points at {reply.slack_thread_ts}, root is at {root.slack_ts}"
    )
    live = eng.slack_clients["su"].get_thread_replies(cid, root.slack_ts)
    assert "thread reply" in [m.get("text") for m in live], (
        "the reply is not in the Slack thread"
    )


# --- T5.2: a93d136 — never hand a canonical id to Slack ---------------------------------


async def test_a_db_origin_root_never_produces_a_phantom_slack_thread(slack_engine):
    """A root minted while Slack was off has a canonical ts Slack has never seen.
    Threading against it either errors or creates a phantom thread that no one can find.

    Control: a root that DOES have a slack_ts threads for real, so the skip is
    conditional rather than the mirror having given up entirely.
    """
    eng, factory, run_id, name, cid = slack_engine

    canonical = "9000.000100"
    await _write_row(factory, run_id, agent_id="su", sender_name="SuProbeBot",
                     content="db-origin root", message_ts=canonical, posted_at=9000.0001,
                     channel_name=name, channel_id=cid)
    await eng._poll_inbound_from_db()
    assert eng._slack_parent_ts(canonical) is None, (
        "a canonical id with no slack_ts must not be offered to Slack"
    )

    await eng._post_message("cravatt", name, "reply to a db-origin root",
                            thread_ts=canonical)
    time.sleep(POST_GAP)
    await eng._flush_persisted()
    # Slack raises thread_not_found for an id it never issued. That is stronger than an
    # empty list: it proves no phantom thread exists rather than merely that it is
    # empty. (If the mirror had posted, this would return the reply instead.)
    with pytest.raises(ThreadNotFound):
        eng.slack_clients["su"].get_thread_replies(cid, canonical)
    # And the message is still durable in the DB — the mirror is skipped, not the write.
    assert "reply to a db-origin root" in [
        r.content for r in await _rows(factory, run_id)
    ], "the reply was lost entirely rather than merely not mirrored"

    # Control: a Slack-origin root threads for real.
    await eng._post_message("su", name, "slack-origin root")
    time.sleep(POST_GAP)
    await eng._flush_persisted()
    root = [r for r in await _rows(factory, run_id) if r.content == "slack-origin root"][0]
    assert root.slack_ts
    assert eng._slack_parent_ts(root.message_ts) == root.slack_ts

    await eng._post_message("cravatt", name, "real threaded reply",
                            thread_ts=root.message_ts)
    time.sleep(POST_GAP)
    await eng._flush_persisted()
    live = eng.slack_clients["su"].get_thread_replies(cid, root.slack_ts)
    assert "real threaded reply" in [m.get("text") for m in live], (
        "control leg failed: the mirror is not threading at all"
    )


# --- T5.3: 7d8b177 — polled bot messages get a mapping too --------------------------------


async def test_a_polled_bot_message_records_its_mirror_mapping(slack_engine):
    """A message posted by ANOTHER process's bot arrives via the Slack poller. Its row
    must carry slack_ts/slack_channel_id, or a later reply to it cannot be threaded.

    Control: a human-authored message in the same poll must also land, so a poller that
    dropped every bot message would not pass.
    """
    eng, factory, run_id, name, cid = slack_engine

    # A bot message that this engine did NOT post: use cravatt's raw client directly,
    # bypassing _post_message so nothing is written to the DB by us.
    marker = f"posted out of band {uuid.uuid4().hex[:6]}"
    out = eng.slack_clients["cravatt"].post_message(cid, marker)
    time.sleep(POST_GAP)
    assert out and out.get("ts")

    eng._last_channel_poll = 0.0
    await eng._poll_slack_for_human_messages()
    await eng._flush_persisted()

    rows = [r for r in await _rows(factory, run_id) if r.content == marker]
    assert rows, (
        "the out-of-band bot message was never ingested. "
        f"rows={[r.content for r in await _rows(factory, run_id)]}"
    )
    row = rows[0]
    assert row.slack_ts == out["ts"], (
        f"the polled bot message has slack_ts={row.slack_ts!r}, Slack says {out['ts']!r}"
    )
    assert row.slack_channel_id == cid
    assert row.is_bot is True, "a bot message was ingested as a human"


# --- T5.4: 10c240c — slack_ts is never inferred ------------------------------------------


async def test_slack_ts_is_never_inferred_from_the_channel_id(slack_engine):
    """A row with a slack_channel_id but no slack_ts means "we know the channel, we do
    not know the message". Synthesising a ts from the channel id produces an identifier
    Slack will reject or, worse, silently mis-thread against.

    Control: a row that DOES have a slack_ts returns it, so this is about the NULL case
    and not about the reader being broken.
    """
    from src.agent.simulation import _restored_slack_ts

    eng, factory, run_id, name, cid = slack_engine

    await _write_row(factory, run_id, agent_id="su", sender_name="SuProbeBot",
                     content="no slack presence", message_ts="9100.000100",
                     posted_at=9100.0001, channel_name=name, channel_id=cid,
                     slack_channel_id=cid, slack_ts=None)
    rows = await _rows(factory, run_id)
    assert _restored_slack_ts(rows[0]) is None, (
        f"a ts was invented for a row that has none: {_restored_slack_ts(rows[0])!r}"
    )

    await eng._post_message("su", name, "has slack presence")
    time.sleep(POST_GAP)
    await eng._flush_persisted()
    real = [r for r in await _rows(factory, run_id) if r.content == "has slack presence"][0]
    assert _restored_slack_ts(real) == real.slack_ts and real.slack_ts


# --- T5.5: baa5583 — "most recent" reads order by posted_at -------------------------------


async def test_thread_history_is_ordered_by_posted_at_not_insertion(slack_engine):
    """The DB poller and the Slack poller append independently, so insertion order can
    disagree with real time. A scrambled thread is handed to the LLM verbatim.

    The rows are appended deliberately out of order; the assertion is the exact expected
    sequence, not merely that the list is non-empty.
    """
    eng, factory, run_id, name, cid = slack_engine

    await eng._post_message("su", name, "root")
    time.sleep(POST_GAP)
    await eng._flush_persisted()
    root = (await _rows(factory, run_id))[0]

    # Two replies written straight to the DB, appended newest-first.
    await _write_row(factory, run_id, agent_id="cravatt", sender_name="CravattProbeBot",
                     content="second reply", message_ts="9200.000200", posted_at=9200.0002,
                     channel_name=name, channel_id=cid, thread_ts=root.message_ts)
    await eng._poll_inbound_from_db()
    await _write_row(factory, run_id, agent_id="wiseman", sender_name="WisemanProbeBot",
                     content="first reply", message_ts="9200.000100", posted_at=9200.0001,
                     channel_name=name, channel_id=cid, thread_ts=root.message_ts)
    await eng._poll_inbound_from_db()

    hist = [e.content for e in eng.message_log.get_thread_history(root.message_ts)]
    assert hist == ["root", "first reply", "second reply"], (
        f"thread history is in insertion order, not posted_at order: {hist}"
    )


# --- T5.6: our own mirrored message must not come back as new ------------------------------


async def test_polling_does_not_re_ingest_our_own_mirrored_message(slack_engine):
    """The mapping exists so the poller can recognise our own post coming back. Without
    it every mirrored message is re-ingested as a fresh inbound one, doubling the log
    and giving other agents a phantom post to react to.

    Control: an out-of-band message posted between the two polls IS ingested, so this is
    dedup and not a poller that stopped working.
    """
    eng, factory, run_id, name, cid = slack_engine

    await eng._post_message("su", name, "mine, mirrored")
    time.sleep(POST_GAP)
    await eng._flush_persisted()
    assert len(await _rows(factory, run_id)) == 1

    for _ in range(2):
        eng._last_channel_poll = 0.0
        await eng._poll_slack_for_human_messages()
        await eng._flush_persisted()

    rows = await _rows(factory, run_id)
    assert [r.content for r in rows] == ["mine, mirrored"], (
        f"our own message was re-ingested: {[r.content for r in rows]}"
    )
    assert len(eng.message_log) == 1, "the in-memory log double-counted"

    marker = f"someone else {uuid.uuid4().hex[:6]}"
    eng.slack_clients["cravatt"].post_message(cid, marker)
    time.sleep(POST_GAP)
    eng._last_channel_poll = 0.0
    await eng._poll_slack_for_human_messages()
    await eng._flush_persisted()
    assert marker in [r.content for r in await _rows(factory, run_id)], (
        "control leg failed: the poller ingests nothing at all"
    )
