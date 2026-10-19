"""Trajectory-line generation end to end (src/services/trajectory_generation.py): which
interviews are due, the claim before the call, retries, the post-set hash and the ceiling."""

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import anthropic
import httpx
import pytest
from sqlalchemy import select, update

from src.config import get_settings
from src.models import InterviewTrajectory
from src.services import llm
from src.services import trajectory_generation as tg
from tests import factories
from tests.fakes import FakeAnthropic

pytestmark = pytest.mark.integration


@pytest.fixture
def factory(db_session):
    """The worker's session factory, routed to the test's rolled-back session."""

    @asynccontextmanager
    async def _factory():
        yield db_session

    return _factory


@pytest.fixture(autouse=True)
def switches(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "interview_trajectories_enabled", True)
    monkeypatch.setattr(settings, "llm_interview_trajectories_model", "claude-opus-5")
    monkeypatch.setattr(settings, "interview_trajectories_daily_usd_limit", 10.0)
    tg._VERIFIED.clear()
    tg._UNBUILDABLE.clear()

    async def no_live_run(db, **kw):
        return None

    monkeypatch.setattr(tg, "live_run_id", no_live_run)
    return settings


def _good_reply(kwargs):
    docs = [b for b in kwargs["messages"][0]["content"] if b["type"] == "document"]
    lines = [
        {"post": d["title"].split(" ")[0], "line": f"Line about {d['title'].split(' ')[0]}."}
        for d in docs
    ]
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps({"lines": lines}))],
        stop_reason="end_turn",
        stop_details=None,
        model="claude-opus-5",
        usage=SimpleNamespace(
            input_tokens=4_000, output_tokens=300, cache_read_input_tokens=0,
            cache_creation_input_tokens=0, iterations=None,
        ),
    )


@pytest.fixture
def calls(monkeypatch):
    """Every `abeta_create` request; `calls.reply(kwargs)` or `calls.error` answers it."""
    box = SimpleNamespace(kwargs=[], reply=_good_reply, error=None)

    async def fake(client, **kwargs):
        box.kwargs.append(kwargs)
        if box.error is not None:
            raise box.error
        return box.reply(kwargs)

    client = FakeAnthropic()
    box.options = client.options_calls
    monkeypatch.setattr(llm, "abeta_create", fake)
    monkeypatch.setattr(llm, "get_anthropic_client", lambda: client)
    return box


_ts = iter(range(1_000, 10**6))


async def _seed_interview(db_session, *, run=None, stopped=True, replies=1):
    """A pitch plus ``replies`` hub replies, seconds apart; the run is stopped (so the
    interview is done) unless ``stopped`` is False."""
    if run is None:
        run = await factories.make_simulation_run(
            db_session,
            started_at=datetime.now(UTC) - timedelta(hours=1),
            **({"ended_at": datetime.now(UTC), "status": "stopped"} if stopped else {}),
        )
    root_ts = f"{next(_ts)}.000100"
    channel = dict(channel_id=f"C{next(_ts)}", channel_name=f"hub-{next(_ts)}")
    root = await factories.make_agent_message(
        db_session, run=run, agent_id="lab1", phase="new_post", message_ts=root_ts,
        sender_name="LabBot", content="The pitch.", posted_at=100.0, **channel,
    )
    rows = [root]
    for i in range(replies):
        rows.append(await factories.make_agent_message(
            db_session, run=run, agent_id="hub", phase="thread_reply",
            message_ts=f"{next(_ts)}.000200", thread_ts=root_ts, sender_name="BlackbirdBot",
            content=f"Reply {i}.", posted_at=130.0 + 30 * i, **channel,
        ))
    return SimpleNamespace(run=run, thread_id=root_ts, rows=rows)


async def _rows(db_session) -> list[InterviewTrajectory]:
    result = await db_session.execute(
        select(InterviewTrajectory)
        .order_by(InterviewTrajectory.created_at)
        .execution_options(populate_existing=True)
    )
    return list(result.scalars())


def _status_error(code: int) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.invalid/v1/messages")
    return anthropic.APIStatusError(
        f"status {code}", response=httpx.Response(code, request=request), body=None,
    )


async def test_done_interview_is_claimed_then_stored_ready(db_session, factory, calls):
    seeded = await _seed_interview(db_session, replies=2)

    assert await tg.generate_due(factory) is True
    (row,) = await _rows(db_session)
    assert (row.simulation_run_id, row.thread_id) == (seeded.run.id, seeded.thread_id)
    assert (row.status, row.error_code, row.attempts) == ("ready", None, 1)
    assert set(row.lines) == {str(r.id) for r in seeded.rows}
    assert row.served_by_model == "claude-opus-5"
    assert row.usage_by_model[0]["input_tokens"] == 4_000
    assert len(row.prompt_sha256_12) == 12 and len(row.posts_sha256_12) == 12
    (request,) = calls.kwargs
    titles = [b["title"] for b in request["messages"][0]["content"] if b["type"] == "document"]
    assert titles == ["P1 · LabBot · pitch", "P2 · BlackbirdBot · reply", "P3 · BlackbirdBot · reply"]
    assert calls.options == [{"timeout": tg.CALL_TIMEOUT_SECONDS, "max_retries": 0}]

    assert await tg.generate_due(factory) is False  # nothing left to do
    assert len(calls.kwargs) == 1


async def test_the_claim_is_committed_before_the_call(db_session, factory, calls, monkeypatch):
    await _seed_interview(db_session)
    commits: list[int] = []
    real_commit = db_session.commit

    async def counting_commit():
        commits.append(1)
        await real_commit()

    monkeypatch.setattr(db_session, "commit", counting_commit)
    calls.error = RuntimeError("worker killed")
    with pytest.raises(RuntimeError):
        await tg.generate_due(factory)
    # The call raised, so nothing after it ran: any commit seen was the claim's.
    assert commits
    (row,) = await _rows(db_session)
    assert (row.status, row.error_code, row.attempts) == ("failed", "in_progress", 1)
    assert await tg.spend_24h(db_session) == tg.attempt_reserve("claude-opus-5")


async def test_live_open_interview_is_not_a_candidate(db_session, factory, calls, monkeypatch):
    seeded = await _seed_interview(db_session, stopped=False)

    async def live(db, **kw):
        return seeded.run.id

    monkeypatch.setattr(tg, "live_run_id", live)
    assert await tg.generate_due(factory) is False
    assert calls.kwargs == [] and await _rows(db_session) == []


async def test_stopped_run_decisionless_interview_is_a_candidate(db_session, factory, calls):
    await _seed_interview(db_session)
    assert await tg.next_candidate(db_session) is not None


async def test_a_live_interview_with_a_decision_is_a_candidate(
    db_session, factory, calls, monkeypatch
):
    seeded = await _seed_interview(db_session, stopped=False)
    await factories.make_thread_decision(
        db_session, run=seeded.run, thread_id=seeded.thread_id, outcome="proposal",
    )

    async def live(db, **kw):
        return seeded.run.id

    monkeypatch.setattr(tg, "live_run_id", live)
    candidate = await tg.next_candidate(db_session)
    assert candidate is not None and candidate.thread_id == seeded.thread_id


async def test_the_newest_run_goes_first(db_session, factory, calls):
    older = await _seed_interview(db_session)
    newer = await _seed_interview(db_session)
    await db_session.execute(
        update(older.run.__class__)
        .where(older.run.__class__.id == older.run.id)
        .values(started_at=datetime.now(UTC) - timedelta(days=3))
    )
    await tg.generate_due(factory)
    (row,) = await _rows(db_session)
    assert row.simulation_run_id == newer.run.id


async def test_changed_post_set_regenerates(db_session, factory, calls):
    seeded = await _seed_interview(db_session)
    await tg.generate_due(factory)
    first_hash = (await _rows(db_session))[0].posts_sha256_12
    assert await tg.generate_due(factory) is False

    await factories.make_agent_message(
        db_session, run=seeded.run, agent_id="lab1", phase="thread_reply",
        message_ts=f"{next(_ts)}.000300", thread_ts=seeded.thread_id,
        sender_name="LabBot", content="A late answer.", posted_at=500.0,
        channel_id=seeded.rows[0].channel_id, channel_name=seeded.rows[0].channel_name,
    )
    assert await tg.generate_due(factory) is True
    (row,) = await _rows(db_session)
    assert row.status == "ready" and row.attempts == 1
    assert row.posts_sha256_12 != first_hash and len(row.lines) == 3
    assert len(calls.kwargs) == 2


async def test_ceiling_stops_generation(db_session, factory, calls, switches, monkeypatch):
    await _seed_interview(db_session)
    await _seed_interview(db_session)
    monkeypatch.setattr(switches, "interview_trajectories_daily_usd_limit", 0.001)
    assert await tg.generate_due(factory) is True  # nothing spent yet: one call
    assert await tg.spend_24h(db_session) > Decimal("0.001")
    assert await tg.generate_due(factory) is False  # ceiling reached; one interview waits
    assert len(calls.kwargs) == 1


async def test_the_switches_and_the_price_list_stop_generation(
    db_session, factory, calls, switches, monkeypatch
):
    await _seed_interview(db_session)
    monkeypatch.setattr(switches, "interview_trajectories_enabled", False)
    assert await tg.generate_due(factory) is False
    monkeypatch.setattr(switches, "interview_trajectories_enabled", True)
    monkeypatch.setattr(switches, "llm_interview_trajectories_model", "not-a-priced-model")
    assert await tg.generate_due(factory) is False
    assert calls.kwargs == []


async def test_transient_error_keeps_the_attempt_refusal_is_not_retried(
    db_session, factory, calls
):
    await _seed_interview(db_session)
    calls.error = _status_error(529)
    for _ in range(4):
        assert await tg.generate_due(factory) is True
        (row,) = await _rows(db_session)
        assert (row.status, row.error_code, row.attempts) == ("failed", "api_status_529", 0)
        assert row.usage_by_model == []
        await db_session.execute(
            update(InterviewTrajectory).values(
                updated_at=datetime.now(UTC) - tg.RETRY_AFTER - timedelta(minutes=1)
            )
        )
    assert await tg.spend_24h(db_session) == 0

    calls.error = None
    calls.reply = lambda kwargs: SimpleNamespace(
        content=[], stop_reason="refusal", stop_details=SimpleNamespace(category="bio"),
        model="claude-opus-5",
        usage=SimpleNamespace(
            input_tokens=4_000, output_tokens=0, cache_read_input_tokens=0,
            cache_creation_input_tokens=0, iterations=None,
        ),
    )
    assert await tg.generate_due(factory) is True
    (row,) = await _rows(db_session)
    assert (row.status, row.error_code, row.lines, row.attempts) == ("refused", "refusal:bio", None, 1)

    await db_session.execute(
        update(InterviewTrajectory).values(updated_at=datetime.now(UTC) - timedelta(days=1))
    )
    assert await tg.generate_due(factory) is False  # a refusal is final


async def test_a_failing_interview_is_retried_after_the_wait_and_then_given_up(
    db_session, factory, calls
):
    await _seed_interview(db_session)
    calls.error = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.invalid"))

    assert await tg.generate_due(factory) is True
    (row,) = await _rows(db_session)
    assert (row.status, row.error_code, row.attempts) == ("failed", "api_connection", 1)
    assert [e.get("partial") for e in row.usage_by_model] == [True]
    assert await tg.generate_due(factory) is False  # inside RETRY_AFTER

    for attempt in (2, 3):
        await db_session.execute(
            update(InterviewTrajectory).values(
                updated_at=datetime.now(UTC) - tg.RETRY_AFTER - timedelta(minutes=1)
            )
        )
        assert await tg.generate_due(factory) is True
        (row,) = await _rows(db_session)
        assert (row.status, row.attempts) == ("failed", attempt)

    await db_session.execute(
        update(InterviewTrajectory).values(updated_at=datetime.now(UTC) - timedelta(days=1))
    )
    assert await tg.generate_due(factory) is False  # MAX_ATTEMPTS reached
    assert len(calls.kwargs) == 3


async def test_an_unreadable_reply_stores_the_paid_attempt_as_failed(db_session, factory, calls):
    await _seed_interview(db_session)
    calls.reply = lambda kwargs: SimpleNamespace(
        content=[SimpleNamespace(type="text", text="not json")], stop_reason="end_turn",
        stop_details=None, model="claude-opus-5",
        usage=SimpleNamespace(
            input_tokens=4_000, output_tokens=10, cache_read_input_tokens=0,
            cache_creation_input_tokens=0, iterations=None,
        ),
    )
    await tg.generate_due(factory)
    (row,) = await _rows(db_session)
    assert (row.status, row.error_code, row.lines, row.attempts) == (
        "failed", "malformed_json", None, 1,
    )
    assert row.usage_by_model[-1]["input_tokens"] == 4_000
