"""A resume's view of BlackbirdBot projects (spec §5.8), from stored rows.

* a posting (phase ``hub_project``) and anything in its thread restore no thread:
  the lab answers with a new root, and only that root's thread is a conversation;
* an open project thread (root phase ``project_reply``) is restored for both bots;
* a closed project thread joins the closed set but adds no prior-thread summary,
  even when its messages are older than ``REBUILD_WINDOW_S`` and so never reach the
  in-memory log;
* a lab's ``project_reply`` root is neither one of its top-level posts nor a pitch.

Driven through the real ``_rebuild_state_from_db`` then ``_rebuild_agent_state``,
as ``test_state_rebuild.py`` does.
"""

import time

import pytest

from src.agent.agent import Agent
from src.agent.message_log import PHASE_HUB_PROJECT, PHASE_PROJECT_REPLY
from src.agent.simulation import SimulationEngine
from src.agent.transport import NullTransport
from tests import factories

pytestmark = pytest.mark.integration

HUB, LAB = "blackbird", "wang"
DAY_S = 86400.0


class _FixtureSessionFactory:
    """Route the engine's own sessions at the rolled-back test session (see
    ``test_state_rebuild.py``); ``__aexit__`` leaves the fixture's session open."""

    def __init__(self, session):
        self._s = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._s

    async def __aexit__(self, *exc):
        return False


def _engine_for(session, run_id, *, max_proposals=0):
    agents = [
        Agent(HUB, "BlackbirdBot", "Blackbird", role="scout_hub"),
        Agent(LAB, "WangBot", "Wang", role="pi_lab"),
    ]
    return SimulationEngine(
        agents=agents,
        slack_clients={a.agent_id: NullTransport(a.agent_id) for a in agents},
        budget_cap=0,
        session_factory=_FixtureSessionFactory(session),
        simulation_run_id=run_id,
        slack_enabled=False,
        max_proposals=max_proposals,
    )


async def _stored_thread(session, run, *, root, root_phase, replier, replies=1, base=None):
    """One root and ``replies`` replies, as rows from a previous process; returns the root ts."""
    base = round(time.time() if base is None else base, 4)
    root_ts = f"{base:.6f}"
    await factories.make_agent_message(
        session, run=run, agent_id=root, channel_id="C1", channel_name="general",
        message_ts=root_ts, thread_ts=None, posted_at=base, phase=root_phase,
        content=f"root by {root}", sender_name=f"{root.capitalize()}Bot", is_bot=True,
    )
    for i in range(replies):
        ts = f"{base + i + 1:.6f}"
        await factories.make_agent_message(
            session, run=run, agent_id=replier, channel_id="C1", channel_name="general",
            message_ts=ts, thread_ts=root_ts, posted_at=base + i + 1, phase="thread_reply",
            content=f"reply {i} by {replier}", sender_name=f"{replier.capitalize()}Bot",
            is_bot=True,
        )
    await session.flush()
    return root_ts


async def _rebuild(eng):
    await eng._rebuild_state_from_db()
    await eng._rebuild_agent_state()


async def test_a_posting_with_a_reply_in_its_thread_restores_no_thread(db_session):
    run = await factories.make_simulation_run(db_session)
    await _stored_thread(
        db_session, run, root=HUB, root_phase=PHASE_HUB_PROJECT, replier=LAB, replies=2,
    )

    eng = _engine_for(db_session, run.id)
    await _rebuild(eng)

    for agent_id in (HUB, LAB):
        assert eng.agents[agent_id].state.active_threads == {}, (
            f"a posting's thread was restored for {agent_id}"
        )


async def test_an_open_project_thread_is_restored_for_both_bots(db_session):
    run = await factories.make_simulation_run(db_session)
    root_ts = await _stored_thread(
        db_session, run, root=LAB, root_phase=PHASE_PROJECT_REPLY, replier=HUB, replies=1,
    )

    eng = _engine_for(db_session, run.id)
    await _rebuild(eng)

    assert list(eng.agents[LAB].state.active_threads) == [root_ts]
    assert list(eng.agents[HUB].state.active_threads) == [root_ts]
    lab_thread = eng.agents[LAB].state.active_threads[root_ts]
    assert lab_thread.other_agent_id == HUB
    assert lab_thread.has_pending_reply is True


@pytest.mark.parametrize(
    ("age_s", "outcome"),
    [
        (0.0, "project_closed"),
        # Older than REBUILD_WINDOW_S and decided: the rows never reach the log,
        # and the outcome is not project_closed, so only the database's project
        # roots can tell this decision belongs to a project.
        (20 * DAY_S, "timeout"),
    ],
)
async def test_a_closed_project_thread_adds_no_prior_thread(db_session, age_s, outcome):
    run = await factories.make_simulation_run(db_session)
    root_ts = await _stored_thread(
        db_session, run, root=LAB, root_phase=PHASE_PROJECT_REPLY, replier=HUB,
        replies=2, base=time.time() - age_s,
    )
    await factories.make_thread_decision(
        db_session, run=run, thread_id=root_ts, channel="general",
        agent_a=HUB, agent_b=LAB, outcome=outcome,
    )
    # An interview decision in the same run still restores its summary.
    interview_ts = await _stored_thread(
        db_session, run, root=LAB, root_phase="new_post", replier=HUB, replies=1,
        base=time.time() - age_s + 100,
    )
    await factories.make_thread_decision(
        db_session, run=run, thread_id=interview_ts, channel="general",
        agent_a=HUB, agent_b=LAB, outcome="no_proposal", summary_text="screened",
    )
    await db_session.flush()

    eng = _engine_for(db_session, run.id)
    await _rebuild(eng)

    assert {root_ts, interview_ts} <= eng._closed_thread_ids
    priors = eng.threads._prior_threads.get((HUB, LAB), [])
    assert [p["outcome"] for p in priors] == ["no_proposal"], (
        f"a project close was restored as prior-thread context: {priors}"
    )
    for agent_id in (HUB, LAB):
        assert eng.agents[agent_id].state.active_threads == {}


async def test_rehydrated_proposal_count_ignores_project_replies(db_session):
    run = await factories.make_simulation_run(db_session)
    pitch_ts = await _stored_thread(
        db_session, run, root=LAB, root_phase="new_post", replier=HUB, replies=0,
        base=time.time() - 100,
    )
    await _stored_thread(
        db_session, run, root=LAB, root_phase=PHASE_PROJECT_REPLY, replier=HUB, replies=1,
    )

    eng = _engine_for(db_session, run.id, max_proposals=5)
    await _rebuild(eng)

    posts = eng.message_log.get_agent_top_level_posts(LAB)
    assert [e.ts for e in posts] == [pitch_ts], "a project reply is not one of the lab's posts"

    await eng._rehydrate_proposal_count()
    assert eng._proposals_posted == 1, "a project reply is not a pitch"
