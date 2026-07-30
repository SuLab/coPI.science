"""Live end-to-end cohort tests: real SimulationEngine, real Postgres, Slack OFF.

The unit suite exercises the gate with fake DB objects. This module runs the real
engine methods against a real database with real rows committed, because that is
where the DB-primary conversation interface and the cohort gate actually meet:

- `_recompute_allowed_sender_ids` issuing real SQL against cohort_memberships
- `_poll_inbound_from_db` ingesting real agent_messages rows written by "another
  process", then a gated read filtering them per agent
- `_rebuild_state_from_db` + `_rebuild_agent_state` reconstructing threads on a
  *resumed* run and the first recompute grandfathering them (v2 §8)
- `_record_topology_snapshot` actually writing a row — it is wrapped in try/except,
  so a broken write would otherwise only log a warning
- `_sync_roster_from_db` adding/removing agents mid-run under an active gate
- the full topology matrix (v2 §5.2) evaluated through the engine, not the helper

Slack is off (NullTransport) throughout: that is the configuration where the DB is
the sole conversation store, so the gate's correctness rests entirely on read-side
filtering with no second path to incidentally catch a miss (v2 §9.1).
"""

import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from src.agent.transport import NullTransport
from src.models import (
    COHORT_ACTION_TOPOLOGY_SNAPSHOT,
    AgentMessage,
    AgentRegistry,
    Cohort,
    CohortAuditEvent,
    CohortMembership,
    SimulationRun,
)
from src.visibility import VISIBILITY_COLLAB_PRIVATE, VISIBILITY_PUBLIC

pytestmark = pytest.mark.integration

AGENT_IDS = ("su", "wiseman", "cravatt", "lotz")


@pytest.fixture
async def live(engine, monkeypatch):
    """A committing session factory plus a cleanup of everything we write.

    Deliberately NOT the rolled-back `db_session` fixture: the engine opens its own
    sessions and commits, and the whole point here is to exercise that path.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = uuid.uuid4()

    async with factory() as db:
        db.add(SimulationRun(id=run_id, status="running"))
        for aid in AGENT_IDS:
            db.add(AgentRegistry(
                agent_id=aid, bot_name=f"{aid.capitalize()}Bot",
                pi_name=f"PI {aid}", status="active",
            ))
        await db.commit()

    yield factory, run_id

    async with factory() as db:
        await db.execute(delete(CohortAuditEvent))
        await db.execute(delete(CohortMembership))
        await db.execute(delete(Cohort))
        await db.execute(delete(AgentMessage).where(AgentMessage.simulation_run_id == run_id))
        await db.execute(delete(AgentRegistry).where(AgentRegistry.agent_id.in_(AGENT_IDS)))
        await db.execute(delete(SimulationRun).where(SimulationRun.id == run_id))
        await db.commit()


def _engine(factory, run_id, agent_ids=AGENT_IDS):
    """A real SimulationEngine with Slack off."""
    agents = [
        Agent(agent_id=a, bot_name=f"{a.capitalize()}Bot", pi_name=f"PI {a}")
        for a in agent_ids
    ]
    eng = SimulationEngine(
        agents=agents,
        slack_clients={a: NullTransport(a) for a in agent_ids},
        budget_cap=0,
        session_factory=factory,
        simulation_run_id=run_id,
        slack_enabled=False,
    )
    eng.message_log.set_bot_name_map({f"{a}bot": a for a in agent_ids})
    eng._bot_name_to_id = {f"{a}bot": a for a in agent_ids}
    # start() registers this; without it nothing reaches agent_messages and a test
    # asserting on the persisted row would silently pass on an empty result.
    eng.message_log.set_persist_callback(eng._enqueue_persist)
    return eng


def _cfg(monkeypatch, *, enabled=True, policy="isolated", valve=3, delay=0.0):
    """Override only the cohort knobs on the REAL Settings object.

    A SimpleNamespace would work for the gate alone, but these tests drive real
    phases, which read a dozen unrelated settings. Copying the real object keeps
    every other value authentic and means a new setting cannot break the harness.
    """
    from src.config import get_settings as _real

    patched = _real().model_copy(update={
        "cohort_isolation_enabled": enabled,
        "cohort_default_policy": policy,
        "max_consecutive_reactive_turns": valve,
        "turn_delay_seconds": delay,
    })
    monkeypatch.setattr("src.agent.simulation.get_settings", lambda: patched)
    monkeypatch.setattr("src.config.get_settings", lambda: patched)


async def _topology(factory, mapping):
    """mapping: {cohort_name: [agent_id, ...]} — committed for real."""
    async with factory() as db:
        await db.execute(delete(CohortMembership))
        await db.execute(delete(Cohort))
        for name, members in mapping.items():
            c = Cohort(name=name)
            db.add(c)
            await db.flush()
            for aid in members:
                db.add(CohortMembership(cohort_id=c.id, agent_id=aid))
        await db.commit()


async def _write_message(factory, run_id, **kw):
    """A row written as if by another process (web app, second engine, backfill)."""
    defaults = dict(
        simulation_run_id=run_id, channel_id="C1", channel_name="general",
        message_length=10, phase="new_post", visibility=VISIBILITY_PUBLIC,
        is_bot=True, thread_ts=None,
    )
    defaults.update(kw)
    async with factory() as db:
        db.add(AgentMessage(**defaults))
        await db.commit()


# A roster the size of the intended pilot. Kept separate from AGENT_IDS so the
# four-agent tests — which is most of this module — stay fast.
AGENT_IDS_20 = (
    "su", "wiseman", "cravatt", "lotz", "racki", "schultz", "wolan", "paegel",
    "joseph", "ward", "lairson", "bollong", "shen", "chatterjee", "kelly",
    "hull", "baran", "sharpless", "nolan", "wu",
)


@pytest.fixture
async def live20(engine):
    """20 active agents, same contract as `live`."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = uuid.uuid4()

    async with factory() as db:
        db.add(SimulationRun(id=run_id, status="running"))
        for aid in AGENT_IDS_20:
            db.add(AgentRegistry(
                agent_id=aid, bot_name=f"{aid.capitalize()}Bot",
                pi_name=f"PI {aid}", status="active",
            ))
        await db.commit()

    yield factory, run_id

    async with factory() as db:
        await db.execute(delete(CohortAuditEvent))
        await db.execute(delete(CohortMembership))
        await db.execute(delete(Cohort))
        await db.execute(delete(AgentMessage).where(AgentMessage.simulation_run_id == run_id))
        await db.execute(delete(AgentRegistry).where(AgentRegistry.agent_id.in_(AGENT_IDS_20)))
        await db.execute(delete(SimulationRun).where(SimulationRun.id == run_id))
        await db.commit()


# ===========================================================================
# The topology matrix — every shape, both policies, through the real engine
# ===========================================================================


TOPOLOGIES = {
    "empty": {},
    "one_empty_cohort": {"alpha": []},
    "single_solo": {"alpha": ["su"]},
    "one_pair": {"alpha": ["su", "wiseman"]},
    "two_disjoint_pairs": {"alpha": ["su", "wiseman"], "beta": ["cravatt", "lotz"]},
    "overlapping": {"alpha": ["su", "wiseman"], "beta": ["su", "cravatt"]},
    "one_big_cohort": {"alpha": list(AGENT_IDS)},
    "hub_in_all": {
        "alpha": ["su", "wiseman"], "beta": ["su", "cravatt"], "gamma": ["su", "lotz"],
    },
    "partial": {"alpha": ["su", "wiseman"]},          # cravatt + lotz uncohorted
    "offline_member_only": {"alpha": ["ghost"]},       # member not on the roster
}

EXPECTED_ISOLATED = {
    # (topology, policy) -> {agent_id: expected gate}  (None = unrestricted)
    ("empty", "isolated"): "REFUSED",
    ("one_empty_cohort", "isolated"): "REFUSED",
    ("offline_member_only", "isolated"): "REFUSED",
    ("single_solo", "isolated"): {
        "su": {"su"}, "wiseman": set(), "cravatt": set(), "lotz": set(),
    },
    ("one_pair", "isolated"): {
        "su": {"su", "wiseman"}, "wiseman": {"su", "wiseman"},
        "cravatt": set(), "lotz": set(),
    },
    ("two_disjoint_pairs", "isolated"): {
        "su": {"su", "wiseman"}, "wiseman": {"su", "wiseman"},
        "cravatt": {"cravatt", "lotz"}, "lotz": {"cravatt", "lotz"},
    },
    ("overlapping", "isolated"): {
        "su": {"su", "wiseman", "cravatt"}, "wiseman": {"su", "wiseman"},
        "cravatt": {"su", "cravatt"}, "lotz": set(),
    },
    ("one_big_cohort", "isolated"): {a: set(AGENT_IDS) for a in AGENT_IDS},
    ("hub_in_all", "isolated"): {
        "su": {"su", "wiseman", "cravatt", "lotz"},
        "wiseman": {"su", "wiseman"}, "cravatt": {"su", "cravatt"},
        "lotz": {"su", "lotz"},
    },
    ("partial", "isolated"): {
        "su": {"su", "wiseman"}, "wiseman": {"su", "wiseman"},
        "cravatt": set(), "lotz": set(),
    },
}

EXPECTED_OPEN = {
    # policy="open": uncohorted agents are unrestricted (None), never silenced.
    ("empty", "open"): {a: None for a in AGENT_IDS},
    ("one_empty_cohort", "open"): {a: None for a in AGENT_IDS},
    ("offline_member_only", "open"): {a: None for a in AGENT_IDS},
    ("single_solo", "open"): {
        "su": {"su", "wiseman", "cravatt", "lotz"},
        "wiseman": None, "cravatt": None, "lotz": None,
    },
    ("one_pair", "open"): {
        "su": {"su", "wiseman", "cravatt", "lotz"},
        "wiseman": {"su", "wiseman", "cravatt", "lotz"},
        "cravatt": None, "lotz": None,
    },
    ("two_disjoint_pairs", "open"): {
        "su": {"su", "wiseman"}, "wiseman": {"su", "wiseman"},
        "cravatt": {"cravatt", "lotz"}, "lotz": {"cravatt", "lotz"},
    },
    ("overlapping", "open"): {
        "su": {"su", "wiseman", "cravatt", "lotz"},
        "wiseman": {"su", "wiseman", "lotz"},
        "cravatt": {"su", "cravatt", "lotz"}, "lotz": None,
    },
    ("one_big_cohort", "open"): {a: set(AGENT_IDS) for a in AGENT_IDS},
    ("hub_in_all", "open"): {   # every agent is cohorted, so nothing to add
        "su": {"su", "wiseman", "cravatt", "lotz"},
        "wiseman": {"su", "wiseman"}, "cravatt": {"su", "cravatt"},
        "lotz": {"su", "lotz"},
    },
    ("partial", "open"): {
        "su": {"su", "wiseman", "cravatt", "lotz"},
        "wiseman": {"su", "wiseman", "cravatt", "lotz"},
        "cravatt": None, "lotz": None,
    },
}


@pytest.mark.parametrize("name", sorted(TOPOLOGIES))
@pytest.mark.parametrize("policy", ["open", "isolated"])
async def test_topology_matrix_through_the_real_engine(
    live, monkeypatch, name, policy
):
    """Every topology shape x both policies, computed from real SQL."""
    factory, run_id = live
    await _topology(factory, TOPOLOGIES[name])
    _cfg(monkeypatch, enabled=True, policy=policy)
    eng = _engine(factory, run_id)
    await eng._recompute_allowed_sender_ids()

    expected = (EXPECTED_OPEN if policy == "open" else EXPECTED_ISOLATED)[(name, policy)]
    if expected == "REFUSED":
        assert eng._cohort_preflight_error is not None, (
            f"{name}/{policy} would silence the roster and must be refused"
        )
        assert all(a.allowed_sender_ids is None for a in eng.agents.values())
        return

    assert eng._cohort_preflight_error is None, eng._cohort_preflight_error
    actual = {aid: a.allowed_sender_ids for aid, a in eng.agents.items()}
    assert actual == expected, f"{name}/{policy}: {actual} != {expected}"


@pytest.mark.parametrize("name", sorted(TOPOLOGIES))
async def test_isolation_disabled_is_always_a_no_op(live, monkeypatch, name):
    """Whatever the topology, the flag off means no filtering at all."""
    factory, run_id = live
    await _topology(factory, TOPOLOGIES[name])
    _cfg(monkeypatch, enabled=False)
    eng = _engine(factory, run_id)
    await eng._recompute_allowed_sender_ids()
    assert all(a.allowed_sender_ids is None for a in eng.agents.values())
    assert eng._cohort_gate_active is False


async def test_gate_relation_is_symmetric_for_every_topology(live, monkeypatch):
    """If A may act on B then B may act on A — a shared cohort is symmetric, and
    an asymmetric gate would let one side monologue."""
    factory, run_id = live
    for name, mapping in TOPOLOGIES.items():
        if not any(mapping.values()):
            continue
        await _topology(factory, mapping)
        _cfg(monkeypatch, enabled=True, policy="isolated")
        eng = _engine(factory, run_id)
        await eng._recompute_allowed_sender_ids()
        def _may_act(viewer, target_id):
            """None means unrestricted, so it may act on anyone."""
            g = viewer.allowed_sender_ids
            return True if g is None else target_id in g

        for a_id, a in eng.agents.items():
            for b_id, b in eng.agents.items():
                if a_id == b_id:
                    continue
                # Deliberately NOT skipping the None-vs-set case. The earlier version
                # of this test skipped it, and that is precisely how it missed the
                # policy=open asymmetry: an uncohorted agent (gate None) could act on
                # a cohorted one, but not the reverse, so the two could never converse.
                assert _may_act(a, b_id) == _may_act(b, a_id), (
                    f"{name}: asymmetric gate between {a_id} and {b_id} — "
                    f"{a_id} gate={a.allowed_sender_ids}, {b_id} gate={b.allowed_sender_ids}"
                )


# ===========================================================================
# The DB conversation interface under an active gate
# ===========================================================================


async def test_db_ingestion_is_complete_and_reads_are_per_agent(live, monkeypatch):
    """The path that matters most: rows written by another process are ingested
    whole into the shared log, and only the per-agent read is filtered."""
    factory, run_id = live
    await _topology(factory, {"alpha": ["su", "wiseman"], "beta": ["cravatt"]})
    _cfg(monkeypatch, enabled=True, policy="isolated")
    eng = _engine(factory, run_id)
    await eng._recompute_allowed_sender_ids()

    await _write_message(factory, run_id, agent_id="wiseman", sender_name="WisemanBot",
                         content="from a cohort-mate", message_ts="1000.0001",
                         posted_at=1000.0001)
    await _write_message(factory, run_id, agent_id="cravatt", sender_name="CravattBot",
                         content="from outside", message_ts="1000.0002",
                         posted_at=1000.0002)
    await _write_message(factory, run_id, agent_id=None, sender_name="Dr PI",
                         content="from a human", message_ts="1000.0003",
                         posted_at=1000.0003, is_bot=False)

    await eng._poll_inbound_from_db()

    # Shared log is complete — ingestion is never gated (v2 §6.2).
    assert len(eng.message_log) == 3, "ingestion must not drop anything"

    su = eng.agents["su"]
    su.state.subscribed_channels = {"general"}
    visible = eng.message_log.get_new_top_level_posts(
        since=0, channels={"general"}, exclude_agent_id="su",
        allowed_sender_ids=su.allowed_sender_ids,
    )
    assert {e.content for e in visible} == {"from a cohort-mate", "from a human"}

    # The excluded agent's own read sees its own side, plus the human.
    cr = eng.agents["cravatt"]
    visible_cr = eng.message_log.get_new_top_level_posts(
        since=0, channels={"general"}, exclude_agent_id="cravatt",
        allowed_sender_ids=cr.allowed_sender_ids,
    )
    assert {e.content for e in visible_cr} == {"from a human"}


async def test_null_agent_id_bot_row_from_the_db_does_not_leak(live, monkeypatch):
    """agent_messages.agent_id is nullable. A bot row with a NULL agent_id must not
    pass the gate as a human once ingested."""
    factory, run_id = live
    await _topology(factory, {"alpha": ["su", "wiseman"]})
    _cfg(monkeypatch, enabled=True, policy="isolated")
    eng = _engine(factory, run_id)
    await eng._recompute_allowed_sender_ids()

    await _write_message(factory, run_id, agent_id=None, sender_name="bot",
                         content="unattributable bot row", message_ts="1000.0009",
                         posted_at=1000.0009, is_bot=True)
    await eng._poll_inbound_from_db()
    assert len(eng.message_log) == 1, "the row is still ingested"

    su = eng.agents["su"]
    visible = eng.message_log.get_new_top_level_posts(
        since=0, channels={"general"}, exclude_agent_id="su",
        allowed_sender_ids=su.allowed_sender_ids,
    )
    assert visible == [], "an unattributable bot row must fail closed"


async def test_private_channel_message_from_a_non_mate_is_visible(live, monkeypatch):
    """A PI-created pairing outranks the cohort, end to end through the DB."""
    factory, run_id = live
    await _topology(factory, {"alpha": ["su"], "beta": ["cravatt"]})
    _cfg(monkeypatch, enabled=True, policy="isolated")
    eng = _engine(factory, run_id)
    await eng._recompute_allowed_sender_ids()

    await _write_message(
        factory, run_id, agent_id="cravatt", sender_name="CravattBot",
        content="my angle on the refinement", message_ts="1000.0011",
        posted_at=1000.0011, channel_name="collab-priv-su-cravatt",
        visibility=VISIBILITY_COLLAB_PRIVATE,
    )
    await eng._poll_inbound_from_db()

    su = eng.agents["su"]
    su.state.subscribed_channels = {"collab-priv-su-cravatt"}
    visible = eng.message_log.get_new_top_level_posts(
        since=0, channels=su.state.subscribed_channels, exclude_agent_id="su",
        allowed_sender_ids=su.allowed_sender_ids,
    )
    assert [e.content for e in visible] == ["my angle on the refinement"]


async def test_resumed_run_rebuild_then_first_recompute_grandfathers(live, monkeypatch):
    """The §8 path that only exists on a restart, exercised for real.

    Messages from a previous process are in the DB. The rebuild reconstructs the
    thread cohort-blind; the first recompute must mark it grandfathered.
    """
    factory, run_id = live
    await _topology(factory, {"alpha": ["su"], "beta": ["cravatt"]})
    _cfg(monkeypatch, enabled=True, policy="isolated")

    # A thread from "before the restart": su's root + cravatt's reply.
    await _write_message(factory, run_id, agent_id="su", sender_name="SuBot",
                         content="root post @CravattBot", message_ts="1000.0021",
                         posted_at=1000.0021)
    await _write_message(factory, run_id, agent_id="cravatt", sender_name="CravattBot",
                         content="replying", message_ts="1000.0022",
                         posted_at=1000.0022, thread_ts="1000.0021")

    eng = _engine(factory, run_id)
    await eng._rebuild_state_from_db()
    await eng._rebuild_agent_state()

    su = eng.agents["su"]
    assert su.allowed_sender_ids is None, "the rebuild is gate-blind by construction"
    threads = su.state.active_threads
    assert threads, "the rebuild must reconstruct the thread"
    t = next(iter(threads.values()))
    assert t.other_agent_id == "cravatt"
    assert t.grandfathered is False

    await eng._recompute_allowed_sender_ids()
    assert t.grandfathered is True, (
        "the first recompute after a rebuild must grandfather inherited "
        "cross-cohort threads"
    )
    assert eng._owes_reply(su) is False, "and it must not win reactive priority"


async def test_topology_snapshot_is_actually_written(live, monkeypatch):
    """_record_topology_snapshot is wrapped in try/except, so a broken write would
    only log a warning. Prove a row lands, with the applied gate in it."""
    factory, run_id = live
    await _topology(factory, {"alpha": ["su", "wiseman"]})
    _cfg(monkeypatch, enabled=True, policy="isolated")
    eng = _engine(factory, run_id)
    await eng._recompute_allowed_sender_ids()
    await eng._record_topology_snapshot()

    async with factory() as db:
        rows = (await db.execute(
            select(CohortAuditEvent).where(
                CohortAuditEvent.action == COHORT_ACTION_TOPOLOGY_SNAPSHOT,
                CohortAuditEvent.simulation_run_id == run_id,
            )
        )).scalars().all()
    assert len(rows) == 1, "no snapshot row was written"
    topo = rows[0].topology
    assert topo["cohort_default_policy"] == "isolated"
    assert topo["agents"]["su"] == ["su", "wiseman"]
    assert topo["agents"]["cravatt"] == []
    assert "counters" in topo


async def test_mid_run_topology_change_snapshots_again(live, monkeypatch):
    """A topology edited during a run must leave a second snapshot, so the run's
    output stays attributable to every configuration it ran under."""
    factory, run_id = live
    await _topology(factory, {"alpha": ["su", "wiseman"]})
    _cfg(monkeypatch, enabled=True, policy="isolated")
    eng = _engine(factory, run_id)
    await eng._recompute_allowed_sender_ids()   # first signature
    await eng._record_topology_snapshot()       # startup snapshot

    await _topology(factory, {"alpha": ["su", "wiseman", "cravatt"]})
    await eng._recompute_allowed_sender_ids()   # signature changes -> snapshots

    async with factory() as db:
        rows = (await db.execute(
            select(CohortAuditEvent)
            .where(CohortAuditEvent.action == COHORT_ACTION_TOPOLOGY_SNAPSHOT)
            .order_by(CohortAuditEvent.created_at)
        )).scalars().all()
    assert len(rows) >= 2, "a mid-run change must be recorded"
    assert rows[-1].topology["agents"]["su"] == ["cravatt", "su", "wiseman"]


async def test_membership_change_takes_effect_without_restart(live, monkeypatch):
    """The live-edit promise, against real SQL."""
    factory, run_id = live
    await _topology(factory, {"alpha": ["su"], "beta": ["cravatt"]})
    _cfg(monkeypatch, enabled=True, policy="isolated")
    eng = _engine(factory, run_id)
    await eng._recompute_allowed_sender_ids()
    assert eng.agents["su"].allowed_sender_ids == {"su"}

    await _topology(factory, {"alpha": ["su", "cravatt"]})
    await eng._recompute_allowed_sender_ids()
    assert eng.agents["su"].allowed_sender_ids == {"su", "cravatt"}


async def test_roster_change_under_an_active_gate(live, monkeypatch):
    """An agent deactivated mid-run must leave the roster and the gate cleanly."""
    factory, run_id = live
    await _topology(factory, {"alpha": ["su", "wiseman", "cravatt"]})
    _cfg(monkeypatch, enabled=True, policy="isolated")
    eng = _engine(factory, run_id)
    await eng._recompute_allowed_sender_ids()
    assert "cravatt" in eng.agents

    async with factory() as db:
        reg = (await db.execute(
            select(AgentRegistry).where(AgentRegistry.agent_id == "cravatt")
        )).scalar_one()
        reg.status = "suspended"
        await db.commit()

    eng._last_roster_poll = 0.0  # force the throttle open
    await eng._sync_roster_from_db()
    assert "cravatt" not in eng.agents, "suspended agent must leave the roster"
    # The remaining agents' gates still name cravatt (it is a cohort member), which
    # is inert: it is not a live sender. Pinned so the behaviour is deliberate.
    assert "cravatt" in eng.agents["su"].allowed_sender_ids


async def test_gate_survives_a_membership_row_for_an_unknown_agent(live, monkeypatch):
    """A membership naming an agent that is not on the roster must not crash or
    silence anyone."""
    factory, run_id = live
    await _topology(factory, {"alpha": ["su", "ghost-agent"]})
    _cfg(monkeypatch, enabled=True, policy="isolated")
    eng = _engine(factory, run_id)
    await eng._recompute_allowed_sender_ids()
    assert eng._cohort_preflight_error is None
    assert eng.agents["su"].allowed_sender_ids == {"su", "ghost-agent"}
    assert eng.agents["wiseman"].allowed_sender_ids == set()


# ===========================================================================
# A real turn, with a faked LLM: does the gate actually reach the prompt?
# ===========================================================================


async def test_phase2_prompt_omits_non_cohort_posts(live, monkeypatch):
    """The claim the whole feature rests on, verified at the LLM boundary.

    Phase 2 is the one batched Sonnet call per turn, and its prompt is where the
    token saving is either real or imaginary. Drive a real Phase 2 with a scripted
    LLM and assert the excluded agent's content never reaches the prompt, while the
    cohort-mate's and the human's do.
    """
    from tests.fakes import FakeAnthropic

    factory, run_id = live
    await _topology(factory, {"alpha": ["su", "wiseman"], "beta": ["cravatt"]})
    _cfg(monkeypatch, enabled=True, policy="isolated")

    fake = FakeAnthropic(['{"selected_post_ids": []}'])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    eng = _engine(factory, run_id)
    await eng._recompute_allowed_sender_ids()

    await _write_message(factory, run_id, agent_id="wiseman", sender_name="WisemanBot",
                         content="MATE-CONTENT spatial multiomics",
                         message_ts="1000.0031", posted_at=1000.0031)
    await _write_message(factory, run_id, agent_id="cravatt", sender_name="CravattBot",
                         content="EXCLUDED-CONTENT chemoproteomics",
                         message_ts="1000.0032", posted_at=1000.0032)
    await _write_message(factory, run_id, agent_id=None, sender_name="Dr PI",
                         content="HUMAN-CONTENT please collaborate",
                         message_ts="1000.0033", posted_at=1000.0033, is_bot=False)
    await eng._poll_inbound_from_db()

    su = eng.agents["su"]
    su.state.subscribed_channels = {"general"}
    su.state.last_seen_cursor = 0.0
    await eng._phase2_scan_filter(su)

    assert fake.calls, "Phase 2 should have made exactly one LLM call"
    prompt = repr(fake.calls[0])
    assert "MATE-CONTENT" in prompt, "a cohort-mate's post must reach the prompt"
    assert "HUMAN-CONTENT" in prompt, "a human's post must always reach the prompt"
    assert "EXCLUDED-CONTENT" not in prompt, (
        "a non-cohort post reached the Phase 2 prompt — the gate is not saving "
        "the tokens it claims to"
    )


async def test_phase2_makes_no_llm_call_when_everything_is_filtered(live, monkeypatch):
    """When the only new posts are from excluded agents there is nothing to scan,
    so the Sonnet call is skipped entirely — the actual saving."""
    from tests.fakes import FakeAnthropic

    factory, run_id = live
    await _topology(factory, {"alpha": ["su"], "beta": ["cravatt"]})
    _cfg(monkeypatch, enabled=True, policy="isolated")
    fake = FakeAnthropic(['{"selected_post_ids": []}'])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    eng = _engine(factory, run_id)
    await eng._recompute_allowed_sender_ids()
    await _write_message(factory, run_id, agent_id="cravatt", sender_name="CravattBot",
                         content="only excluded traffic", message_ts="1000.0041",
                         posted_at=1000.0041)
    await eng._poll_inbound_from_db()

    su = eng.agents["su"]
    su.state.subscribed_channels = {"general"}
    su.state.last_seen_cursor = 0.0
    await eng._phase2_scan_filter(su)
    assert fake.calls == [], "no scannable posts must mean no LLM call"


async def test_phase3_does_not_activate_a_thread_from_a_non_cohort_tag(live, monkeypatch):
    """Phase 3 is pure bookkeeping, but activating a thread with an excluded agent
    would commit a thread slot and then drive Phase 4 spend."""
    factory, run_id = live
    await _topology(factory, {"alpha": ["su"], "beta": ["cravatt"]})
    _cfg(monkeypatch, enabled=True, policy="isolated")
    eng = _engine(factory, run_id)
    await eng._recompute_allowed_sender_ids()

    await _write_message(factory, run_id, agent_id="cravatt", sender_name="CravattBot",
                         content="hey @SuBot want to work together?",
                         message_ts="1000.0051", posted_at=1000.0051)
    await eng._poll_inbound_from_db()

    su = eng.agents["su"]
    su.state.subscribed_channels = {"general"}
    su.state.last_seen_cursor = 0.0
    eng._phase3_activate_threads(su)
    assert su.state.active_threads == {}, (
        "a tag from an excluded agent must not open a thread"
    )


async def test_phase3_does_activate_for_a_cohort_mate(live, monkeypatch):
    """The same path must still work for a permitted sender — proving the previous
    test is measuring the gate and not a broken Phase 3."""
    factory, run_id = live
    await _topology(factory, {"alpha": ["su", "wiseman"]})
    _cfg(monkeypatch, enabled=True, policy="isolated")
    eng = _engine(factory, run_id)
    await eng._recompute_allowed_sender_ids()

    await _write_message(factory, run_id, agent_id="wiseman", sender_name="WisemanBot",
                         content="hey @SuBot want to work together?",
                         message_ts="1000.0061", posted_at=1000.0061)
    await eng._poll_inbound_from_db()

    su = eng.agents["su"]
    su.state.subscribed_channels = {"general"}
    su.state.last_seen_cursor = 0.0
    eng._phase3_activate_threads(su)
    assert su.state.active_threads, "a cohort-mate's tag must still open a thread"


async def test_outbound_post_strips_a_cross_cohort_mention_for_real(live, monkeypatch):
    """_post_message is the choke point, so drive it and read the persisted row."""
    factory, run_id = live
    await _topology(factory, {"alpha": ["su", "wiseman"], "beta": ["cravatt"]})
    _cfg(monkeypatch, enabled=True, policy="isolated")
    eng = _engine(factory, run_id)
    await eng._recompute_allowed_sender_ids()

    await eng._post_message(
        "su", "general", "Good idea @WisemanBot — and @CravattBot too?"
    )
    await eng._flush_persisted()

    async with factory() as db:
        rows = (await db.execute(
            select(AgentMessage).where(
                AgentMessage.simulation_run_id == run_id,
                AgentMessage.agent_id == "su",
            )
        )).scalars().all()
    assert len(rows) == 1
    content = rows[0].content
    assert "@WisemanBot" in content, "a cohort-mate mention must survive"
    assert "CravattBot" not in content, "a cross-cohort mention must be stripped"
    assert eng._cohort_tags_stripped.get("su") == 1


async def test_grandfathered_thread_still_gets_a_phase4_reply(live, monkeypatch):
    """§8's central promise, driven end to end rather than asserted structurally.

    A thread whose partner has left the cohort must still be answered — abandoning
    it mid-flight wastes every call already spent — while losing reactive priority.
    """
    from tests.fakes import FakeAnthropic

    factory, run_id = live
    await _topology(factory, {"alpha": ["su"], "beta": ["cravatt"]})
    _cfg(monkeypatch, enabled=True, policy="isolated")

    fake = FakeAnthropic(["<slack_message>Happy to wrap this up.</slack_message>"])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    eng = _engine(factory, run_id)
    await eng._recompute_allowed_sender_ids()

    # An open thread with an agent who is now outside the cohort.
    await _write_message(factory, run_id, agent_id="su", sender_name="SuBot",
                         content="root post", message_ts="1000.0071",
                         posted_at=1000.0071)
    await _write_message(factory, run_id, agent_id="cravatt", sender_name="CravattBot",
                         content="a reply that deserves an answer",
                         message_ts="1000.0072", posted_at=1000.0072,
                         thread_ts="1000.0071")
    await eng._poll_inbound_from_db()

    su = eng.agents["su"]
    su.state.subscribed_channels = {"general"}
    su.state.last_seen_cursor = 0.0
    from src.agent.state import ThreadState
    su.state.active_threads["1000.0071"] = ThreadState(
        thread_id="1000.0071", channel="general", other_agent_id="cravatt",
        message_count=2,
    )
    await eng._recompute_allowed_sender_ids()
    thread = su.state.active_threads["1000.0071"]
    assert thread.grandfathered is True

    # It must not win reactive priority...
    assert eng._owes_reply(su) is False
    # ...but Phase 4 must still pick it up and reply.
    replied = await eng._phase4_reply_threads(su)
    assert "1000.0071" in replied, (
        "a grandfathered thread must still be answered so it can conclude"
    )
    assert fake.calls, "Phase 4 should have called the LLM for the grandfathered thread"


async def test_pi_dm_path_is_unaffected_by_any_topology(live, monkeypatch):
    """PI DMs bypass MessageLog entirely (_poll_pi_dms_from_db -> PIHandler), so no
    cohort configuration may suppress them."""
    from src.models import PiDmMessage

    factory, run_id = live
    await _topology(factory, {"alpha": ["su"], "beta": ["cravatt"]})
    _cfg(monkeypatch, enabled=True, policy="isolated")
    eng = _engine(factory, run_id)
    await eng._recompute_allowed_sender_ids()
    # su is maximally gated: only itself.
    assert eng.agents["su"].allowed_sender_ids == {"su"}

    handled = []

    class _Handler:
        async def handle_dm(self, agent_id, pi_user_id, content):
            handled.append((agent_id, content))

    eng._pi_handler = _Handler()

    async with factory() as db:
        db.add(PiDmMessage(
            simulation_run_id=run_id, agent_id="su", pi_user_id="Uweb",
            direction="inbound", content="please prioritise the immunology angle",
            ts="1000.0081",
        ))
        await db.commit()

    await eng._poll_pi_dms_from_db()
    assert handled == [("su", "please prioritise the immunology angle")], (
        "a PI DM must reach the agent under every cohort configuration"
    )
    assert eng.agents["su"].state.has_pi_directive is True


# ===========================================================================
# Concurrency: membership writes must be atomic
# ===========================================================================


async def test_matrix_save_writes_memberships_atomically(live, monkeypatch):
    """A wipe committed separately from the re-insert transiently opens the gate.

    Measured: with a two-transaction writer, ~57% of concurrent gate recomputes
    landed in the preflight-refused state under `policy="isolated"` — i.e. the gate
    was fully OPEN for those ticks — because a reader in the gap sees zero
    memberships and the preflight correctly (but unhelpfully) refuses. With a
    single-transaction writer it was 0 of ~4,500.

    The shipped `/admin/cohorts/topology` route is safe because it accumulates every
    add and delete and commits once. This test pins that, because the dependency is
    invisible: a future bulk importer that truncates and then inserts would silently
    un-gate the roster about half the time.
    """
    import inspect

    from src.routers import admin

    src = inspect.getsource(admin.admin_cohort_topology_save)
    # Exactly one commit, and it is the last statement of the write path.
    assert src.count("await db.commit()") == 1, (
        "the matrix save must commit exactly once; a mid-loop commit exposes an "
        "empty-topology window to any concurrent gate recompute"
    )
    body_after_loop = src[src.rindex("for cell in sorted(rendered):"):]
    assert body_after_loop.index("await db.commit()") > body_after_loop.rindex(
        "COHORT_ACTION_AGENT_REMOVED"
    ), "the commit must come after every add/remove has been staged"


async def test_empty_topology_fails_open_not_closed(live, monkeypatch):
    """If a reader ever does see an empty topology mid-write, the outcome must be
    'everyone unrestricted', never 'everyone silenced'."""
    factory, run_id = live
    await _topology(factory, {"alpha": []})
    _cfg(monkeypatch, enabled=True, policy="isolated")
    eng = _engine(factory, run_id)
    await eng._recompute_allowed_sender_ids()
    assert eng._cohort_preflight_error is not None
    assert all(a.allowed_sender_ids is None for a in eng.agents.values()), (
        "an empty topology must fail OPEN — a transient write window must never "
        "silence the roster"
    )


async def test_post_message_stamps_private_channel_visibility(live, monkeypatch):
    """A message posted into a collab_private channel must persist as collab_private.

    Regression for a defect that made the §7 exemption dead code. `_post_message`
    omitted `visibility` when constructing the LogEntry, so every agent-authored
    message defaulted to "public" — even in a PI-created refinement channel. The gate's
    private-channel exemption reads that field, so it never fired: two agents in
    different cohorts could not converse in the channel the PI made for them.

    The existing §7 test could not catch this because it writes the AgentMessage row
    directly with the visibility already set, exercising only the read path. This one
    goes through `_post_message` and reads back what actually landed.
    """
    factory, run_id = live
    await _topology(factory, {"alpha": ["su"], "beta": ["cravatt"]})
    _cfg(monkeypatch, enabled=True, policy="isolated")
    eng = _engine(factory, run_id)
    await eng._recompute_allowed_sender_ids()

    priv = "collab-priv-su-cravatt"
    eng._channel_visibility[priv] = VISIBILITY_COLLAB_PRIVATE
    eng._channel_id_map[priv] = f"local:{priv}"

    await eng._post_message("cravatt", priv, "my angle on the refinement")
    await eng._post_message("su", "general", "a public post")
    await eng._flush_persisted()

    async with factory() as db:
        rows = {
            r.channel_name: r.visibility
            for r in (await db.execute(
                select(AgentMessage).where(AgentMessage.simulation_run_id == run_id)
            )).scalars().all()
        }
    assert rows[priv] == VISIBILITY_COLLAB_PRIVATE, (
        "a message posted into a collab_private channel persisted as "
        f"{rows[priv]!r} — the §7 exemption keys on this field and would never fire"
    )
    assert rows["general"] == VISIBILITY_PUBLIC

    # And the exemption now actually fires: su is maximally gated, yet sees the message.
    su = eng.agents["su"]
    su.state.subscribed_channels = {priv}
    visible = eng.message_log.get_new_top_level_posts(
        since=0, channels={priv}, exclude_agent_id="su",
        allowed_sender_ids=su.allowed_sender_ids,
    )
    assert [e.content for e in visible] == ["my angle on the refinement"]


# ===========================================================================
# Scale + roster churn (v2 §14.6)
# ===========================================================================


async def test_gate_is_correct_and_affordable_at_20_agents(live20, monkeypatch):
    """The pilot-scale recompute, with correctness as the control.

    A timing bound on its own is satisfied by a recompute that returns empty gates
    instantly — which is the failure mode that actually matters here, since an empty
    gate silences an agent. So the same gates are checked for being non-empty,
    symmetric, and self-inclusive before the timing assertion runs.
    """
    import statistics
    import time

    factory, run_id = live20
    # 100 cohorts x 5 members, rotating through the roster: dense overlap, 500 rows.
    mapping = {
        f"c{i:03d}": [AGENT_IDS_20[(i * 5 + j) % 20] for j in range(5)]
        for i in range(100)
    }
    await _topology(factory, mapping)
    _cfg(monkeypatch, enabled=True, policy="isolated")
    eng = _engine(factory, run_id, agent_ids=AGENT_IDS_20)

    timings = []
    for _ in range(10):
        t0 = time.perf_counter()
        await eng._recompute_allowed_sender_ids()
        timings.append(time.perf_counter() - t0)

    gates = {a: x.allowed_sender_ids for a, x in eng.agents.items()}
    assert len(gates) == 20
    assert eng._cohort_preflight_error is None
    assert all(g for g in gates.values()), (
        f"an agent ended up with an empty gate: "
        f"{[a for a, g in gates.items() if not g]}"
    )
    for a, ga in gates.items():
        assert a in ga, f"{a} cannot see its own messages"
        for b in ga:
            assert a in gates[b], f"asymmetric gate {a} -> {b}"

    p50 = statistics.median(timings)
    assert p50 < 0.5, (
        f"recompute p50 {p50 * 1000:.0f}ms at 100 cohorts / 500 memberships / 20 agents"
    )


async def test_deactivating_an_agent_mid_run_updates_roster_and_gate(live20, monkeypatch):
    """A suspended agent leaves the live roster on the next sync, and the surviving
    agents keep working gates rather than being reset to None or empty."""
    factory, run_id = live20
    await _topology(
        factory, {"alpha": list(AGENT_IDS_20[:10]), "beta": list(AGENT_IDS_20[10:])}
    )
    _cfg(monkeypatch, enabled=True, policy="isolated")
    eng = _engine(factory, run_id, agent_ids=AGENT_IDS_20)
    await eng._recompute_allowed_sender_ids()
    assert "wu" in eng.agents
    assert "wu" in eng.agents["nolan"].allowed_sender_ids

    async with factory() as db:
        reg = (await db.execute(
            select(AgentRegistry).where(AgentRegistry.agent_id == "wu")
        )).scalar_one()
        reg.status = "suspended"
        await db.commit()

    eng._last_roster_poll = 0.0
    await eng._sync_roster_from_db()

    assert "wu" not in eng.agents, "a suspended agent must leave the live roster"
    # Control: the remaining agents still have real gates. A sync that wiped every gate
    # would also satisfy the assertion above.
    assert eng.agents["su"].allowed_sender_ids, "the gate was cleared by the sync"
    assert "wiseman" in eng.agents["su"].allowed_sender_ids
    assert "su" not in eng.agents["nolan"].allowed_sender_ids, (
        "cross-cohort isolation must survive a roster sync"
    )


async def test_activating_an_agent_mid_run_gives_it_a_gate(live20, monkeypatch):
    """The mirror case. A newly activated agent must arrive WITH a gate applied, not
    with allowed_sender_ids left at None — that would be a hole that opens itself."""
    factory, run_id = live20
    await _topology(
        factory, {"alpha": list(AGENT_IDS_20[:10]), "beta": list(AGENT_IDS_20[10:])}
    )
    _cfg(monkeypatch, enabled=True, policy="isolated")
    # Start with 19 agents; "wu" exists in the registry but is not in the process.
    eng = _engine(factory, run_id, agent_ids=AGENT_IDS_20[:19])
    await eng._recompute_allowed_sender_ids()
    assert "wu" not in eng.agents

    eng._last_roster_poll = 0.0
    await eng._sync_roster_from_db()

    assert "wu" in eng.agents, "an active registry row must join the live roster"
    gate = eng.agents["wu"].allowed_sender_ids
    assert gate is not None, "a newly added agent arrived with NO gate — an open hole"
    assert gate == set(AGENT_IDS_20[10:]), gate
