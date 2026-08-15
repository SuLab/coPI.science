"""Real multi-turn cohort scenarios. Marked real_llm — skipped without an API key.

Everything else in the cohort suite asserts on logic: given these rows, the gate
computes this. These four assert on an **emergent** outcome — who actually ends up
holding a threaded conversation with whom after real Opus/Sonnet turns — which is not
derivable from the gate computation and is the thing the feature is actually for.

Two design decisions are load-bearing, and both come from a run that proved nothing:

1. **Every lab is complementary to every other.** An earlier version gave each cohort
   internally-matching interests and made the cohorts mutually irrelevant. The gate-OFF
   baseline then produced zero cross-cohort threads for reasons of scientific relevance,
   so the gate-ON result was unfalsifiable. Here all four labs are facets of one
   problem, so every pair is a plausible collaboration and the gate is the only thing
   that can prevent one.

2. **The roster is trimmed to the agents under test.** With four agents over a dozen
   turns each agent gets two or three turns, which is not enough for a *specific* pair
   to form a thread — outcome claims came back inconclusive rather than confirmed.

A third: messages the harness itself posts (the lab introductions, and the opening
messages in a private channel) are recorded and excluded from every pair measurement.
Counting them would make "these two agents conversed" true by construction — the
harness put both of them in the channel.
"""

import os
import time
import uuid
from dataclasses import dataclass, field

import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker

import src.agent.simulation as sim
from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from src.agent.transport import NullTransport
from src.config import get_settings as real_settings
from src.models import (
    AgentChannel,
    AgentMessage,
    AgentRegistry,
    Cohort,
    CohortAuditEvent,
    CohortMembership,
    SimulationRun,
    User,
)
from src.visibility import VISIBILITY_COLLAB_PRIVATE

pytestmark = [
    pytest.mark.integration,
    pytest.mark.real_llm,
    pytest.mark.skipif(
        not os.environ.get("ANTHROPIC_API_KEY"),
        reason="no ANTHROPIC_API_KEY — scenario runs are opt-in and cost money",
    ),
]

LABS = {
    "su": (
        "SuBot",
        "genome-scale CRISPR screens mapping E3 ligases to substrates; we need "
        "degrader chemistry, imaging of substrate loss, and ternary-complex modelling",
    ),
    "cravatt": (
        "CravattBot",
        "covalent chemoproteomics finding ligandable cysteines on E3 ligases and "
        "building degraders; we need screen hits, imaging readouts, and structural "
        "modelling",
    ),
    "wiseman": (
        "WisemanBot",
        "quantitative single-cell imaging of substrate degradation kinetics; we need "
        "screen hits to watch, degrader chemistry to perturb, and analysis pipelines",
    ),
    "lotz": (
        "LotzBot",
        "ternary-complex modelling and image-analysis pipelines for degradation "
        "kinetics; we need screen hits, degrader chemistry, and imaging series",
    ),
}

# 8 turns is enough now that scenarios start from an open thread: Phase 4 fires on
# the first turn rather than waiting for Phase 5 to spontaneously choose to reply.
TURNS = int(os.environ.get("SCENARIO_TURNS", "8"))
BUDGET = int(os.environ.get("SCENARIO_BUDGET", "10"))


@dataclass
class ScenarioResult:
    """What a scenario run produced. Pairs are always ``(a, b)`` with ``a < b``."""

    gates: dict = field(default_factory=dict)
    # Loose: both agents appear in the same public thread and at least one message in
    # it is agent-authored. Used for LEAK assertions, where over-detection is safe.
    public_pairs: set = field(default_factory=set)
    # Strict: both agents authored a message in the same public thread during a turn.
    public_exchanges: set = field(default_factory=set)
    private_pairs: set = field(default_factory=set)
    # Captured AT the mid-run recompute, not at the end. A concluded thread is popped
    # out of active_threads (_close_thread), so reading this at the end of a run
    # reports [] for a thread that was correctly grandfathered and then finished —
    # which is the successful outcome, misread as the failure.
    grandfathered: list = field(default_factory=list)
    grandfathered_at_end: list = field(default_factory=list)
    strips: dict = field(default_factory=dict)
    messages: int = 0
    agent_messages: int = 0
    turns_taken: int = 0
    errors: list = field(default_factory=list)
    # {(root_agent, replier): root_ts} for threads the harness opened as preconditions.
    seeded_threads: dict = field(default_factory=dict)
    # Diagnostics, so a zero-pair result says WHY rather than just failing.
    threads: dict = field(default_factory=dict)
    posts_by_agent: dict = field(default_factory=dict)

    def authored_in(self, pair) -> list:
        """Who posted into a seeded thread during a turn (seeds excluded)."""
        ts = self.seeded_threads.get(tuple(pair))
        if ts is None:
            return []
        return self.threads.get(ts, {}).get("authored", [])

    def diagnosis(self) -> str:
        return (
            f"turns={self.turns_taken} agent_msgs={self.agent_messages} "
            f"by_agent={self.posts_by_agent} "
            f"gf_at_split={self.grandfathered} gf_at_end={self.grandfathered_at_end} "
            f"threads={self.threads} "
            f"loose={sorted(self.public_pairs)} strict={sorted(self.public_exchanges)} "
            f"private={sorted(self.private_pairs)} errors={self.errors}"
        )


@pytest.fixture
async def scenario_db(engine):
    """A committing factory plus cleanup.

    Deliberately not the rolled-back ``db_session``: the engine opens its own sessions
    and commits, and that is the path under test.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = uuid.uuid4()
    # _build_engine rebinds these module globals directly rather than via monkeypatch,
    # because the engine reads them from a dozen call sites during a real turn. Restore
    # them here: leaving them patched would silently reconfigure every test that runs
    # after a scenario in the same session.
    saved = {
        name: getattr(sim, name)
        for name in ("get_settings", "_UNIVERSAL_CHANNELS", "_CHANNEL_KEYWORDS")
    }
    try:
        yield factory, run_id
    finally:
        for name, value in saved.items():
            setattr(sim, name, value)
        # In a finally too: a failing scenario would otherwise leave its roster and
        # cohorts behind for every later test to trip over.
        async with factory() as db:
            await db.execute(delete(CohortAuditEvent))
            await db.execute(
                delete(AgentMessage).where(AgentMessage.simulation_run_id == run_id)
            )
            await db.execute(
                delete(AgentChannel).where(AgentChannel.simulation_run_id == run_id)
            )
            await db.execute(delete(CohortMembership))
            await db.execute(delete(Cohort))
            await db.execute(
                delete(AgentRegistry).where(AgentRegistry.agent_id.in_(tuple(LABS)))
            )
            await db.execute(delete(User).where(User.email.like("%@scen.test")))
            await db.execute(delete(SimulationRun).where(SimulationRun.id == run_id))
            await db.commit()


async def _seed_roster(factory, run_id, roster):
    async with factory() as db:
        db.add(SimulationRun(id=run_id, status="running"))
        for i, aid in enumerate(roster):
            u = User(
                id=uuid.uuid4(),
                orcid=f"9999-0000-0009-{i:04d}",
                email=f"{aid}@scen.test",
                name=f"PI {aid}",
                onboarding_complete=True,
                access_status="allowed",
            )
            db.add(u)
            await db.flush()
            db.add(AgentRegistry(
                agent_id=aid, bot_name=LABS[aid][0], pi_name=f"PI {aid}",
                user_id=u.id, status="active",
            ))
        await db.commit()


async def _set_topology(factory, mapping):
    async with factory() as db:
        await db.execute(delete(CohortMembership))
        await db.execute(delete(Cohort))
        for name, members in (mapping or {}).items():
            c = Cohort(name=name)
            db.add(c)
            await db.flush()
            for aid in members:
                db.add(CohortMembership(cohort_id=c.id, agent_id=aid))
        await db.commit()


async def _all_message_ts(factory, run_id) -> set[str]:
    async with factory() as db:
        rows = (await db.execute(
            text("select message_ts from agent_messages where simulation_run_id = :r"),
            {"r": run_id},
        )).all()
    return {r[0] for r in rows}


async def _public_threads(factory, run_id, exclude_ts):
    """``{thread_key: {"all": {agent_ids}, "authored": {agent_ids}}}`` for public bots.

    The thread key is ``coalesce(thread_ts, message_ts)``, so a root and its replies
    share one key. ``authored`` excludes the harness's own seeded messages, which is
    what keeps "these two conversed" from being true by construction.
    """
    async with factory() as db:
        rows = (await db.execute(text("""
            select agent_id, coalesce(thread_ts, message_ts) as k, message_ts,
                   thread_ts is not null as is_reply
            from agent_messages
            where simulation_run_id = :r and is_bot and visibility = 'public'
              and agent_id is not null
        """), {"r": run_id})).all()
    out: dict[str, dict] = {}
    for aid, key, mts, is_reply in rows:
        slot = out.setdefault(key, {"all": set(), "authored": set(), "replies": 0})
        slot["all"].add(aid)
        if mts not in exclude_ts:
            slot["authored"].add(aid)
            if is_reply:
                slot["replies"] += 1
    return out


def _pairs_from_threads(threads, *, strict):
    """Pairs co-present in a thread.

    ``strict`` requires both agents to have authored a message in it during a turn.
    Loose requires both to be present with at least one authored message in the thread
    from either side — that is, a real interaction happened there, even if one-sided.
    """
    out = set()
    for slot in threads.values():
        who = slot["authored"] if strict else slot["all"]
        if not strict and not slot["authored"]:
            continue
        members = sorted(who)
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                out.add((a, b))
    return out


async def _private_channel_pairs(factory, run_id, exclude_ts):
    """Pairs that both posted into the same collab_private channel *during turns*.

    Private channels are flat — the agents post messages, not threaded replies — so
    co-presence is the signal. The harness's own opening messages are excluded, without
    which the pair would be true by construction.
    """
    async with factory() as db:
        rows = (await db.execute(text("""
            select agent_id, channel_name, message_ts from agent_messages
            where simulation_run_id = :r and is_bot
              and visibility = 'collab_private'
        """), {"r": run_id})).all()
    by_channel: dict[str, set[str]] = {}
    for aid, ch, mts in rows:
        if mts in exclude_ts or aid is None:
            continue
        by_channel.setdefault(ch, set()).add(aid)
    out = set()
    for who in by_channel.values():
        for a in sorted(who):
            for b in sorted(who):
                if a < b:
                    out.add((a, b))
    return out


def _build_engine(factory, run_id, roster, policy):
    # ONE public channel for the whole scenario.
    #
    # Phase 1 joins channels by keyword-matching the profile, and Phase 5 posts into
    # whichever of the agent's subscribed channels the model names. With the real
    # seven-channel workspace the agents scattered: in a measured 8-turn run su joined
    # {general, aging-and-longevity, funding-opportunities} and cravatt joined all
    # seven, then posted into #chemical-biology and #drug-repurposing — channels su does
    # not read. Neither agent ever saw enough of the other to open a thread, and every
    # outcome claim came back INCONCLUSIVE.
    #
    # Collapsing the workspace to #general is a property of the test *environment*, not
    # of the behaviour under test: it makes the agents co-present, which is the
    # precondition for the gate to be the deciding factor in whether they converse. The
    # gate itself, the read paths and the topology are all untouched.
    sim._UNIVERSAL_CHANNELS = {"general"}
    sim._CHANNEL_KEYWORDS = {}

    patched = real_settings().model_copy(update={
        "cohort_isolation_enabled": True,
        "cohort_default_policy": policy,
        "turn_delay_seconds": 0.0,
        "phase5_skip_probability": 0.0,
    })
    sim.get_settings = lambda: patched

    agents = []
    for aid in roster:
        bot, summary = LABS[aid]
        a = Agent(agent_id=aid, bot_name=bot, pi_name=f"PI {aid}")
        # The cached-profile seam: a real profile without touching disk or the DB.
        a._public_profile = f"# {aid.capitalize()} Lab\n\n{summary}\n"
        agents.append(a)

    eng = SimulationEngine(
        agents=agents,
        slack_clients={a: NullTransport(a) for a in roster},
        budget_cap=BUDGET,
        session_factory=factory,
        simulation_run_id=run_id,
        slack_enabled=False,
    )
    eng.message_log.set_bot_name_map({LABS[a][0].lower(): a for a in roster})
    eng._bot_name_to_id = {LABS[a][0].lower(): a for a in roster}
    eng.message_log.set_persist_callback(eng._enqueue_persist)
    # Populates _channel_id_map and _channel_visibility for the seeded channels.
    # Resets _channel_visibility wholesale, so any private channel must be registered
    # after this call, not before.
    eng._ensure_seeded_channels()
    return eng


async def _seed_thread(factory, eng, run_id, root_agent, replier):
    """Open a thread between two agents and register it on both, as a resumed run does.

    The thread's *existence* is a precondition here, not the claim. Left to chance it is
    an unreliable one: measured over 16 real turns with two agents, Phase 5 chose "skip"
    or "new post" almost every time and produced 3 agent messages and zero threaded
    replies. Waiting for a specific pair to spontaneously thread up made every downstream
    outcome claim INCONCLUSIVE rather than wrong.

    This mirrors `_rebuild_agent_state`, which is what happens on every resumed run: the
    thread exists in the log and both agents carry a ThreadState for it. What the
    scenario then measures is emergent — whether a real model continues the thread, and
    whether the gate marks or blocks it. See v2 §8, which frames the resumed-run rebuild
    as the normal path rather than an edge case.

    Returns the thread's root ts. Both messages are seeds and are excluded from every
    pair measurement.
    """
    from src.agent.state import ThreadState

    await eng._post_message(
        root_agent, "general",
        f"Concretely: {LABS[root_agent][1]}. Proposing a first joint experiment — what "
        "would you need from us to make it work?",
    )
    await eng._flush_persisted()
    root_ts = max(
        e.ts for e in eng.message_log.get_new_top_level_posts(
            since=0, channels={"general"}, exclude_agent_id=replier,
            allowed_sender_ids=None,
        ) if e.sender_agent_id == root_agent
    )
    await eng._post_message(
        replier, "general",
        f"Interested. On our side: {LABS[replier][1]}. What is the smallest pilot that "
        "would tell us whether this works?",
        thread_ts=root_ts,
    )
    await eng._flush_persisted()

    for owner, other in ((root_agent, replier), (replier, root_agent)):
        eng.agents[owner].state.active_threads[root_ts] = ThreadState(
            thread_id=root_ts, channel="general", other_agent_id=other,
            message_count=2, has_pending_reply=(owner == root_agent),
        )
    return root_ts


async def run_scenario(
    factory, run_id, *, policy, topology, roster,
    turns=TURNS, private_pair=None, mid_run=None, seed_threads=(),
):
    """Drive real turns and return the emergent outcome.

    ``mid_run`` is ``(turn_index, new_topology)`` — applied before that turn and
    followed by a gate recompute, which is how grandfathering gets triggered.

    ``seed_threads`` is a sequence of ``(root_agent, replier)`` pairs; each opens a
    thread as a resumed run would (see ``_seed_thread``).
    """
    await _seed_roster(factory, run_id, roster)
    await _set_topology(factory, topology)
    eng = _build_engine(factory, run_id, roster, policy)
    await eng._recompute_allowed_sender_ids()
    gates = {
        a: (None if x.allowed_sender_ids is None else set(x.allowed_sender_ids))
        for a, x in eng.agents.items()
    }

    if private_pair:
        a, b = private_pair
        name = f"collab-priv-{a}-{b}"
        async with factory() as db:
            db.add(AgentChannel(
                simulation_run_id=run_id, channel_id=f"local:{name}",
                channel_name=name, channel_type="collaboration",
                visibility=VISIBILITY_COLLAB_PRIVATE, created_by_agent=a,
            ))
            await db.commit()
        eng._channel_visibility[name] = VISIBILITY_COLLAB_PRIVATE
        eng._channel_id_map[name] = f"local:{name}"
        for aid in (a, b):
            eng.agents[aid].state.subscribed_channels.add(name)
            await eng._post_message(
                aid, name,
                f"(private refinement channel) {LABS[aid][1]} — what would a concrete "
                f"first experiment between our labs look like?",
            )

    for aid in roster:
        await eng._post_message(
            aid, "general",
            f"Introducing our lab: {LABS[aid][1]}. Keen to hear from complementary "
            "groups.",
        )
    await eng._flush_persisted()

    seeded_thread_ids = {}
    for root_agent, replier in seed_threads:
        seeded_thread_ids[(root_agent, replier)] = await _seed_thread(
            factory, eng, run_id, root_agent, replier
        )

    # Everything written so far is the harness's, not the agents'. Excluded from every
    # pair measurement below.
    seed_ts = await _all_message_ts(factory, run_id)

    def _grandfathered_now():
        return sorted(
            (a, th.thread_id)
            for a, x in eng.agents.items()
            for th in x.state.active_threads.values()
            if th.grandfathered
        )

    errors = []
    taken = 0
    grandfathered_at_split = []
    for t in range(turns):
        if mid_run and t == mid_run[0]:
            await _set_topology(factory, mid_run[1])
            await eng._recompute_allowed_sender_ids()
            # Snapshot here: by the end of the run a grandfathered thread that did what
            # §8 wants — concluded — has been popped out of active_threads.
            grandfathered_at_split = _grandfathered_now()
        # Reply lane first (Task 11): every (agent, thread) pair owing a
        # reply, every tick, unpaced — mirrors _run_main_loop's ordering.
        await eng._dispatch_reply_lane()
        agent = eng._select_agent()
        if agent is None:
            break
        taken += 1
        try:
            await eng._run_post_turn(agent)
        except Exception as exc:  # a turn must never abort the whole scenario
            errors.append(f"{agent.agent_id}: {type(exc).__name__}: {exc}")
        agent.state.last_selected = time.time()
        await eng._flush_persisted()

    await eng._flush_persisted()
    async with factory() as db:
        total = (await db.execute(text(
            "select count(*) from agent_messages where simulation_run_id = :r"
        ), {"r": run_id})).scalar()
        by_agent = dict((await db.execute(text("""
            select agent_id, count(*) from agent_messages
            where simulation_run_id = :r and is_bot and agent_id is not null
              and message_ts not in (select unnest(cast(:seeds as text[])))
            group by agent_id
        """), {"r": run_id, "seeds": list(seed_ts)})).all())

    threads = await _public_threads(factory, run_id, seed_ts)
    return ScenarioResult(
        gates=gates,
        public_pairs=_pairs_from_threads(threads, strict=False),
        public_exchanges=_pairs_from_threads(threads, strict=True),
        private_pairs=await _private_channel_pairs(factory, run_id, seed_ts),
        threads={
            k: {"all": sorted(v["all"]), "authored": sorted(v["authored"]),
                "replies": v["replies"]}
            for k, v in threads.items()
        },
        posts_by_agent=by_agent,
        grandfathered=grandfathered_at_split,
        grandfathered_at_end=_grandfathered_now(),
        seeded_threads=seeded_thread_ids,
        strips=dict(eng._cohort_tags_stripped),
        messages=total,
        agent_messages=total - len(seed_ts),
        turns_taken=taken,
        errors=errors,
    )


async def test_harness_produces_conversation_at_all(scenario_db):
    """Self-test, and the positive control the other four rest on.

    A permissive single-cohort run with one open thread must produce at least one
    agent-authored message in that thread. If it does not, every absence assertion in
    this module is worthless, and this is the test that tells you so — run it first
    whenever a scenario comes back empty.
    """
    factory, run_id = scenario_db
    res = await run_scenario(
        factory, run_id, policy="isolated",
        topology={"alpha": ["su", "cravatt"]}, roster=["su", "cravatt"],
        seed_threads=[("su", "cravatt")],
    )
    assert not res.errors, res.errors
    assert res.agent_messages >= 1, (
        f"no agent authored anything in {res.turns_taken} turns. {res.diagnosis()}"
    )
    assert res.authored_in(("su", "cravatt")), (
        "no real model replied into an OPEN thread between two cohort-mates, so no "
        f"scenario built on this harness can prove anything. {res.diagnosis()}"
    )
    assert ("cravatt", "su") in res.public_pairs, res.diagnosis()


# `test_open_policy_lets_an_uncohorted_agent_be_acted_on` used to live here. It
# proved §5.2's open-policy asymmetry fix via Phase 2's gated scan: `su`'s
# **gated** scan had to accept a post authored by the uncohorted `cravatt`, on
# the first turn, without waiting for organic thread formation (which four
# agents over eight turns cannot reliably produce — see this module's own
# docstring, reason 2). Removal-cycle task 7 deleted Phase 2 outright
# (`_phase2_scan_filter`/`build_phase2_scan_prompt`/`interesting_posts`), so
# that evidentiary leg no longer exists, and no other surviving engine path
# gives a first-turn, pre-conversation signal of "would this agent act on
# that peer" — Phase 5 no longer scans/replies to a bank of interesting
# posts at all (locked decision 4 deleted that action), so the only
# remaining evidence of "the open policy lets this happen" is real thread/
# message formation, which this same module already treats as unreliable at
# this roster size and turn count for a single specific pair (hence the
# deterministic gate-computation checks below, not a repeat here). The
# claim's gate-computation half is already pinned without any LLM in
# `tests/unit/test_cohort_isolation.py::TestComputeGates::
# test_open_policy_uncohorted_agent_is_unrestricted` and
# `test_open_policy_is_symmetric_with_uncohorted_agents` — deleted rather
# than left half-working against a field that no longer exists.


async def test_hub_converses_with_both_sides_but_spokes_do_not(scenario_db):
    """Non-transitivity under real conversation.

    su is in both cohorts; cravatt and wiseman share none. One thread is opened on each
    of su's two legs as a precondition. The claims are emergent: a real model keeps at
    least one of them alive, and no thread ever forms between the two spokes.

    The presence leg is the control — a run where nothing was said at all would satisfy
    the leak assertion by itself.
    """
    factory, run_id = scenario_db
    res = await run_scenario(
        factory, run_id, policy="isolated",
        topology={"alpha": ["su", "wiseman"], "beta": ["su", "cravatt"]},
        roster=["su", "cravatt", "wiseman"],
        seed_threads=[("su", "wiseman"), ("cravatt", "su")],
    )
    assert not res.errors, res.errors
    assert res.gates["su"] == {"su", "cravatt", "wiseman"}
    assert "cravatt" not in res.gates["wiseman"]
    assert "wiseman" not in res.gates["cravatt"]

    assert res.authored_in(("su", "wiseman")) or res.authored_in(("cravatt", "su")), (
        f"INCONCLUSIVE: the hub said nothing on either leg. {res.diagnosis()}"
    )
    assert ("cravatt", "wiseman") not in res.public_pairs, (
        f"LEAK: two spokes sharing no cohort ended up in one thread. {res.diagnosis()}"
    )
    # And neither spoke posted into the other spoke's thread with the hub.
    assert "cravatt" not in res.authored_in(("su", "wiseman")), res.diagnosis()
    assert "wiseman" not in res.authored_in(("cravatt", "su")), res.diagnosis()


async def test_grandfathered_thread_survives_a_mid_run_split(scenario_db):
    """§8 under real conversational load.

    A thread is open between two cohort-mates; the topology then splits them. Three
    things must hold, and the third is the one a marked-but-stalled thread would fail:

    1. the engine marks the thread grandfathered on the recompute;
    2. it loses reactive priority (asserted deterministically in the live suite);
    3. a **real model** still writes into it, so the conversation can conclude.
    """
    factory, run_id = scenario_db
    res = await run_scenario(
        factory, run_id, policy="isolated",
        topology={"alpha": ["su", "cravatt"]}, roster=["su", "cravatt"],
        seed_threads=[("su", "cravatt")],
        # Split before the first turn. The thread was opened while they were mates (the
        # topology above), so the precondition holds; splitting later is a race — at
        # turn 4 the thread had already concluded and been popped out of
        # active_threads, leaving nothing to mark. Every authored message below is
        # therefore post-split, which is what makes the third assertion mean something.
        mid_run=(0, {"alpha": ["su"], "beta": ["cravatt"]}),
    )
    assert not res.errors, res.errors
    assert res.gates["su"] == {"su", "cravatt"}, "the gate BEFORE the split"
    root_ts = res.seeded_threads[("su", "cravatt")]
    assert res.grandfathered, (
        "the open cross-cohort thread was not marked when the topology split. "
        f"{res.diagnosis()}"
    )
    assert {t for _, t in res.grandfathered} == {root_ts}, (
        f"the wrong thread was grandfathered. {res.diagnosis()}"
    )
    assert sorted(a for a, _ in res.grandfathered) == ["cravatt", "su"], (
        f"both sides of the thread must be marked, not just one. {res.diagnosis()}"
    )
    assert res.authored_in(("su", "cravatt")), (
        "the grandfathered thread received nothing at all — it stalled instead of "
        f"concluding. {res.diagnosis()}"
    )


async def test_private_channel_beats_the_cohort_gate(scenario_db):
    """§7: a PI-created pairing outranks an admin grouping.

    su and cravatt are in different cohorts and maximally gated — each can act only on
    itself. They must still converse in the channel the PI made for them.

    Control: they must NOT converse in the public channel. Without that leg the private
    result is equally explained by the gate not being in force at all.
    """
    factory, run_id = scenario_db
    res = await run_scenario(
        factory, run_id, policy="isolated",
        topology={"alpha": ["su"], "beta": ["cravatt"]}, roster=["su", "cravatt"],
        private_pair=("su", "cravatt"),
    )
    assert not res.errors, res.errors
    assert res.gates["su"] == {"su"} and res.gates["cravatt"] == {"cravatt"}
    assert ("cravatt", "su") in res.private_pairs, (
        "INCONCLUSIVE OR REGRESSED: the two agents did not both post into the channel "
        f"the PI created for them, during a turn. {res.diagnosis()}"
    )
    assert ("cravatt", "su") not in res.public_pairs, (
        f"control leg failed: they also conversed publicly, so the gate is off. "
        f"{res.diagnosis()}"
    )
