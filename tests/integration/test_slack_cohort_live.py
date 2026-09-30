"""The cohort gate and the Slack mirror together.

This block used to be untestable: no agent carried a bot token. With three probe bots
it is.

The claim that matters is a distinction the Slack-off suite structurally cannot make:
the gate filters **reads**, never **writes**. Every agent's message must reach Slack —
the channel is shared and a human reads it — while a gated agent must not act on it.
With NullTransport those two are the same observation.
"""

import time
import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
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
from tests.slack_live_support import thread_replies

pytestmark = [pytest.mark.integration, pytest.mark.live_slack]

AGENTS = ("su", "cravatt", "wiseman")
POST_GAP = 1.1


@pytest.fixture
async def cohort_engine(engine, slack_clients, slack_probe_channel, monkeypatch):
    import src.agent.simulation as sim
    from src.config import get_settings as _real

    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = uuid.uuid4()
    name, cid = slack_probe_channel
    monkeypatch.setattr(sim, "SEEDED_CHANNELS", [name])

    patched = _real().model_copy(update={
        "cohort_isolation_enabled": True, "cohort_default_policy": "isolated",
        "turn_delay_seconds": 0.0,
    })
    monkeypatch.setattr(sim, "get_settings", lambda: patched)

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


async def _topology(factory, mapping):
    async with factory() as db:
        await db.execute(delete(CohortMembership))
        await db.execute(delete(Cohort))
        for cname, members in mapping.items():
            c = Cohort(name=cname)
            db.add(c)
            await db.flush()
            for aid in members:
                db.add(CohortMembership(cohort_id=c.id, agent_id=aid))
        await db.commit()


# --- T9.1 --------------------------------------------------------------------------


async def test_the_gate_filters_reads_and_never_the_mirror(cohort_engine):
    """The distinction Slack-off cannot make.

    su+cravatt share a cohort, wiseman is outside it. All three messages must reach
    Slack — the channel is shared and a human reads it, so suppressing the WRITE would
    be a bug, not the feature. Only su's *read* is filtered.
    """
    eng, factory, run_id, name, cid = cohort_engine
    await _topology(factory, {"alpha": ["su", "cravatt"], "beta": ["wiseman"]})
    await eng._recompute_allowed_sender_ids()
    assert eng.agents["su"].allowed_sender_ids == {"su", "cravatt"}
    assert eng.agents["wiseman"].allowed_sender_ids == {"wiseman"}

    for aid, text in (("su", "from su"), ("cravatt", "from cravatt"),
                      ("wiseman", "from wiseman")):
        await eng._post_message(aid, name, text)
        time.sleep(POST_GAP)
    await eng._flush_persisted()

    # Every message is in Slack. The gate is a read filter, not a mute button.
    live = [m.get("text")
            for m in eng.slack_clients["su"].poll_channel_messages(cid, oldest="0")]
    for text in ("from su", "from cravatt", "from wiseman"):
        assert text in live, f"{text!r} never reached Slack: {live}"

    # And every row landed, un-gated (§6.2: ingestion is never gated).
    async with factory() as db:
        rows = (await db.execute(select(AgentMessage).where(
            AgentMessage.simulation_run_id == run_id))).scalars().all()
    assert len(rows) == 3, [r.content for r in rows]

    # su's gated read excludes wiseman and includes its cohort-mate.
    su = eng.agents["su"]
    visible = {e.content for e in eng.message_log.get_new_top_level_posts(
        since=0, channels={name}, exclude_agent_id="su",
        allowed_sender_ids=su.allowed_sender_ids)}
    assert visible == {"from cravatt"}, visible


# --- T9.2: mention stripping, observable only in Slack --------------------------------


async def test_a_cross_cohort_mention_is_stripped_in_the_message_slack_receives(
    cohort_engine
):
    """The strip runs inside _post_message, so Slack is the only place its effect is
    observable end to end. Both halves in ONE message: the outsider's mention is gone
    and the cohort-mate's survives, so a strip that deleted every mention fails.
    """
    eng, factory, run_id, name, cid = cohort_engine
    await _topology(factory, {"alpha": ["su", "cravatt"]})
    await eng._recompute_allowed_sender_ids()
    assert eng.agents["su"].allowed_sender_ids == {"su", "cravatt"}

    marker = uuid.uuid4().hex[:6]
    await eng._post_message(
        "su", name,
        f"[{marker}] cc @CravattProbeBot and @WisemanProbeBot on this",
    )
    time.sleep(POST_GAP)
    await eng._flush_persisted()

    live = [m.get("text")
            for m in eng.slack_clients["su"].poll_channel_messages(cid, oldest="0")]
    posted = [t for t in live if marker in t]
    assert posted, f"the message never reached Slack: {live}"
    text = posted[0]
    assert "WisemanProbeBot" not in text, (
        f"a cross-cohort mention survived into Slack: {text!r}"
    )
    assert "@CravattProbeBot" in text, (
        f"the cohort-mate's mention was stripped too: {text!r}"
    )
    assert eng._cohort_tags_stripped.get("su", 0) >= 1

    # And the stored row matches what Slack shows — the strip is not display-only.
    async with factory() as db:
        row = (await db.execute(select(AgentMessage).where(
            AgentMessage.simulation_run_id == run_id))).scalars().one()
    assert "WisemanProbeBot" not in row.content


# --- T9.3: grandfathering across a restart, with Slack on -------------------------------


async def test_a_cross_cohort_thread_is_grandfathered_and_still_replies_in_slack(
    cohort_engine
):
    """§8 calls the resumed run the normal path, because the DB rebuild reconstructs
    threads gate-blind before the first recompute. This is the only test that exercises
    that with Slack present.

    Grandfathering is reporting-only (D12): the reply lane still owes the thread its
    reply after the partner leaves the cohort, and that reply must reach Slack.
    """
    from src.agent.state import ThreadState

    eng, factory, run_id, name, cid = cohort_engine
    await _topology(factory, {"alpha": ["su", "cravatt"]})
    await eng._recompute_allowed_sender_ids()

    await eng._post_message("su", name, "thread root")
    time.sleep(POST_GAP)
    await eng._flush_persisted()
    async with factory() as db:
        root = (await db.execute(select(AgentMessage).where(
            AgentMessage.content == "thread root"))).scalars().one()
    await eng._post_message("cravatt", name, "a reply", thread_ts=root.message_ts)
    time.sleep(POST_GAP)
    await eng._flush_persisted()

    su = eng.agents["su"]
    su.state.active_threads[root.message_ts] = ThreadState(
        thread_id=root.message_ts, channel=name, other_agent_id="cravatt",
        message_count=2)
    assert any(
        t.thread_id == root.message_ts for a, t in eng._pending_reply_pairs() if a is su
    ), "precondition: the thread owes a reply in-cohort"

    await _topology(factory, {"alpha": ["su"], "beta": ["cravatt"]})
    await eng._recompute_allowed_sender_ids()
    assert su.state.active_threads[root.message_ts].grandfathered is True
    assert any(
        t.thread_id == root.message_ts for a, t in eng._pending_reply_pairs() if a is su
    ), "a grandfathered thread still owes its reply (D12)"

    # And the reply must reach the real Slack thread.
    await eng._post_message("su", name, "wrapping up", thread_ts=root.message_ts)
    time.sleep(POST_GAP)
    await eng._flush_persisted()
    replies = thread_replies(eng.slack_clients["su"], cid, root.slack_ts)
    assert "wrapping up" in [m.get("text") for m in replies], (
        "the grandfathered thread's concluding reply never reached Slack"
    )
