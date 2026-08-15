"""The cohort gate and the Slack mirror together.

`.notes/cohort-thorough-test-plan.md` excluded this block by instruction and noted it
was not testable anyway: no agent carried a bot token. With three probe bots it is.

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
from src.visibility import VISIBILITY_COLLAB_PRIVATE, VISIBILITY_PUBLIC

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
    assert eng._owes_reply(su) is True, "precondition: the thread owes a reply in-cohort"

    await _topology(factory, {"alpha": ["su"], "beta": ["cravatt"]})
    await eng._recompute_allowed_sender_ids()
    assert su.state.active_threads[root.message_ts].grandfathered is True
    assert eng._owes_reply(su) is False, "a grandfathered thread must lose priority"

    # It may still conclude — and the reply must reach the real Slack thread.
    await eng._post_message("su", name, "wrapping up", thread_ts=root.message_ts)
    time.sleep(POST_GAP)
    await eng._flush_persisted()
    replies = eng.slack_clients["su"].get_thread_replies(cid, root.slack_ts)
    assert "wrapping up" in [m.get("text") for m in replies], (
        "the grandfathered thread's concluding reply never reached Slack"
    )


# --- T9.4: private-channel polling needs a member bot -----------------------------------


async def test_a_private_channel_is_polled_only_by_a_member_bot(cohort_engine, slack_clients):
    """`_client_for_channel` picks a bot that is actually in the channel. A non-member
    gets channel_not_found, so a wrong pick silently loses every message in the channel.

    Control: after inviting the second bot, its client does read it.
    """
    eng, factory, run_id, name, cid = cohort_engine
    su, wiseman = slack_clients["su"], slack_clients["wiseman"]

    priv = su.create_private_channel(f"t-priv-poll-{uuid.uuid4().hex[:6]}")
    assert priv and priv.get("id"), priv
    pname, pcid = priv["name"], priv["id"]
    try:
        eng._channel_id_map[pname] = pcid
        eng._channel_visibility[pname] = VISIBILITY_COLLAB_PRIVATE

        su.post_message(pcid, "members only")
        time.sleep(POST_GAP)
        assert wiseman.poll_channel_messages(pcid, oldest="0") == [], (
            "a non-member read the private channel"
        )

        # Documented behaviour worth pinning: _client_for_channel keys ONLY on
        # _private_channel_members, never on _channel_visibility. With the membership
        # map empty it hands back the fallback even for a channel marked private, and
        # that fallback then fails with channel_not_found on every poll tick. The real
        # path (_sync_private_channels_from_db) populates both maps together, so this
        # is a fail-soft rather than a defect — but the docstring's "returns None if
        # the channel is private and no connected member is available" is only true
        # once the map has been loaded.
        assert eng._client_for_channel(pcid, wiseman) is wiseman

        # With membership known, the member bot is chosen.
        eng._private_channel_members[pcid] = ["su"]
        chosen = eng._client_for_channel(pcid, wiseman)
        assert chosen is not None, "no member bot was found for a channel su is in"
        assert "members only" in [
            m.get("text") for m in chosen.poll_channel_messages(pcid, oldest="0")
        ], "the chosen client cannot read the channel it was chosen for"

        # And a private channel whose only member is disconnected yields None, so the
        # caller skips it rather than erroring every tick.
        eng._private_channel_members[pcid] = ["nobody"]
        assert eng._client_for_channel(pcid, wiseman) is None

        # Control: invite wiseman and it can read it too.
        assert su.invite_to_channel(pcid, [wiseman.bot_user_id]) is True
        assert "members only" in [
            m.get("text") for m in wiseman.poll_channel_messages(pcid, oldest="0")
        ]
    finally:
        su._call_with_retry(su._client.conversations_archive, channel=pcid)


async def test_the_private_channel_exemption_holds_over_slack(cohort_engine, slack_clients):
    """§7 end to end with the mirror on: two agents in DIFFERENT cohorts, maximally
    gated, still converse in the channel the PI made for them — and the messages are
    really in Slack.

    Control: the same two agents' public traffic IS filtered from each other's reads, so
    the private result cannot be explained by the gate being off.
    """
    eng, factory, run_id, name, cid = cohort_engine
    await _topology(factory, {"alpha": ["su"], "beta": ["cravatt"]})
    await eng._recompute_allowed_sender_ids()
    assert eng.agents["su"].allowed_sender_ids == {"su"}
    assert eng.agents["cravatt"].allowed_sender_ids == {"cravatt"}

    su, cravatt = slack_clients["su"], slack_clients["cravatt"]
    priv = su.create_private_channel(f"t-priv-exempt-{uuid.uuid4().hex[:6]}")
    assert priv and priv.get("id"), priv
    pname, pcid = priv["name"], priv["id"]
    try:
        assert su.invite_to_channel(pcid, [cravatt.bot_user_id]) is True
        eng._channel_id_map[pname] = pcid
        eng._channel_visibility[pname] = VISIBILITY_COLLAB_PRIVATE
        # cravatt posts to this channel by NAME below, and only su's client learned the
        # id from create_private_channel. Without the shared cache, cravatt's
        # _resolve_channel_id falls back to list_channels() — which never returns private
        # channels at all in its default mode — and the raw name is handed to
        # chat.postMessage. The engine shares the map for exactly this reason in
        # production (_sync_private_channels_from_db / cache_channel_ids).
        for c in slack_clients.values():
            c.cache_channel_ids({pname: pcid})
        for a in eng.agents.values():
            a.state.subscribed_channels.add(pname)

        await eng._post_message("cravatt", pname, "private: my angle")
        time.sleep(POST_GAP)
        await eng._post_message("su", name, "public: my angle")
        time.sleep(POST_GAP)
        await eng._flush_persisted()

        # Persisted with the right visibility, and really in Slack.
        async with factory() as db:
            byname = {r.channel_name: r for r in (await db.execute(select(AgentMessage)
                      .where(AgentMessage.simulation_run_id == run_id))).scalars().all()}
        assert byname[pname].visibility == VISIBILITY_COLLAB_PRIVATE
        assert byname[name].visibility == VISIBILITY_PUBLIC
        assert "private: my angle" in [
            m.get("text") for m in su.poll_channel_messages(pcid, oldest="0")]

        # su is maximally gated yet sees cravatt's private-channel message.
        sua = eng.agents["su"]
        seen = {e.content for e in eng.message_log.get_new_top_level_posts(
            since=0, channels={pname}, exclude_agent_id="su",
            allowed_sender_ids=sua.allowed_sender_ids)}
        assert seen == {"private: my angle"}, seen

        # Control: cravatt does NOT see su's public post.
        cra = eng.agents["cravatt"]
        pub = [e.content for e in eng.message_log.get_new_top_level_posts(
            since=0, channels={name}, exclude_agent_id="cravatt",
            allowed_sender_ids=cra.allowed_sender_ids)]
        assert pub == [], f"control leg failed: the gate is not filtering public: {pub}"
    finally:
        su._call_with_retry(su._client.conversations_archive, channel=pcid)
