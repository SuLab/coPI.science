"""``simulation_stats.spend_by_system``: what each system spent in one run, as the panel's
Interviews and Projects cards show it (independent-controls plan, Task 7)."""

from decimal import Decimal

import pytest

from src.models import HubProject, HubProjectCall
from src.models.hub_project import CALL_DONE, STATUS_POSTED
from src.services.simulation_stats import spend_by_system
from tests import factories

pytestmark = pytest.mark.integration

PRICED = "claude-opus-5"  # $5 / MTok input, $25 / MTok output
REPLY_TS = "1830000000.000100"


def _usage(inp: int, out: int) -> list[dict]:
    return [{
        "model": PRICED, "input_tokens": inp, "output_tokens": out,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    }]


async def _call(db_session, run, *, phase, thread_ts=None, model=PRICED):
    """One call of 1,000 input and 100 output tokens: $0.0075 when priced."""
    return await factories.make_llm_call_log(
        db_session, run=run, phase=phase, thread_ts=thread_ts, model=model,
        input_tokens=1000, output_tokens=100,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )


async def _project_with_ledger(db_session, run) -> HubProject:
    """A posted project whose research ledger cost $0.10."""
    project = HubProject(
        simulation_run_id=run.id, agent_id="tstspendlab", slot=1, status=STATUS_POSTED,
        requested_run_seconds=0.0, reply_ts=REPLY_TS,
    )
    db_session.add(project)
    await db_session.flush()
    db_session.add(HubProjectCall(project_id=project.id, step="research:scientific",
                                  model=PRICED, status=CALL_DONE,
                                  usage_by_model=_usage(10_000, 2_000)))
    await db_session.flush()
    return project


async def test_project_calls_and_the_ledger_are_projects_and_the_rest_is_interviews(db_session):
    run = await factories.make_simulation_run(db_session)
    await _project_with_ledger(db_session, run)
    # Projects: a project-thread turn, the lab's reply step, and a specialist consulted
    # inside the project thread.
    await _call(db_session, run, phase="project_thread", thread_ts=REPLY_TS)
    await _call(db_session, run, phase="project_reply")
    await _call(db_session, run, phase="consult_chemistry", thread_ts=REPLY_TS)
    # Interviews: two interview turns and a thread-less post.
    await _call(db_session, run, phase="thread_reply", thread_ts="1830000000.000200")
    await _call(db_session, run, phase="thread_reply", thread_ts="1830000000.000200")
    await _call(db_session, run, phase="new_post")
    # An unpriced model counts zero.
    await _call(db_session, run, phase="thread_reply", model="claude-unknown-42")
    # Another run's project and calls never leak in.
    other = await factories.make_simulation_run(db_session)
    await _project_with_ledger(db_session, other)
    await _call(db_session, other, phase="project_thread", thread_ts=REPLY_TS)
    await _call(db_session, other, phase="new_post")

    spend = await spend_by_system(db_session, run.id)

    assert spend == {
        "projects": Decimal("0.10") + 3 * Decimal("0.0075"),
        "interviews": 3 * Decimal("0.0075"),
    }


async def test_an_empty_run_spent_nothing(db_session):
    run = await factories.make_simulation_run(db_session)
    assert await spend_by_system(db_session, run.id) == {
        "projects": Decimal(0), "interviews": Decimal(0),
    }
