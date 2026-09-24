"""A conversation end to end (spec §5.3, §6, §7.3, §9, §11.2), plus the plan's
Review Focus inputs. The producer runs on the test's session (use_test_session) and
the model is FakeAsyncAnthropic, so nothing here spends money.

Ids are read into locals before any request that can roll the shared session back:
a rollback expires every loaded object, and touching an expired attribute outside a
greenlet raises."""

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import anthropic
import httpx
import pytest
from sqlalchemy import select, text

from src.config import get_settings
from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_REVIEWER,
    AssessmentChatTurn,
    AssessmentChatUsage,
    AssessmentReview,
)
from src.services import assessment_chat as chat
from src.services.assessment_chat_stream import StreamOutcome
from tests import factories
from tests.assessment_chat_support import (
    ask_url,
    clear_url,
    history_url,
    parse_sse,
    seed_interview,
    use_fake_llm,
    use_test_session,
)
from tests.fakes import ChatScript, FakeAsyncAnthropic, api_status_error, connection_error
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


@pytest.fixture
def install_llm(monkeypatch, db_session):
    use_test_session(monkeypatch, db_session)

    def _install(*scripts: ChatScript) -> FakeAsyncAnthropic:
        fake = FakeAsyncAnthropic(list(scripts))
        use_fake_llm(monkeypatch, fake)
        return fake

    return _install


async def _setup(db_session, role=USER_ROLE_MANAGER, **seed):
    seeded = await seed_interview(db_session, **seed)
    user = await factories.make_user(db_session, user_role=role)
    return seeded.assessment_id, user.id, auth_headers(user.id), seeded


async def _ask(client, assessment_id, headers, question="What is proposed?"):
    resp = await client.post(ask_url(assessment_id), json={"question": question}, headers=headers)
    return resp, (parse_sse(resp.text) if resp.status_code == 200 else [])


async def _turns(db_session, assessment_id):
    rows = await db_session.execute(
        select(AssessmentChatTurn)
        .where(AssessmentChatTurn.assessment_id == assessment_id)
        .order_by(AssessmentChatTurn.created_at)
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars())


async def _ledger(db_session, user_id):
    rows = await db_session.execute(
        select(AssessmentChatUsage)
        .where(AssessmentChatUsage.user_id == user_id)
        .order_by(AssessmentChatUsage.created_at)
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars())


def _ledger_row(user_id, *, age=timedelta(hours=1), status="complete", usage=None, turn_id=None,
                assessment_id=None, tier="staff"):
    return AssessmentChatUsage(
        id=uuid.uuid4(), turn_id=turn_id, user_id=user_id, assessment_id=assessment_id,
        context_tier=tier, model="claude-opus-5-5", status=status, usage_by_model=usage,
        created_at=datetime.now(UTC) - age,
    )


def _priced(dollars: float) -> list[dict]:
    """One billed entry worth `dollars` at claude-opus-5-5's $4/MTok input rate."""
    return [{
        "model": "claude-opus-5-5", "billed": True, "input_tokens": int(dollars * 250_000),
        "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    }]


def _paused() -> ChatScript:
    return ChatScript(pause_after_events=2, paused=asyncio.Event(), release=asyncio.Event())


def _mid_stream_status_error(error_type: str) -> anthropic.APIStatusError:
    """An ``anthropic.APIStatusError`` shaped the way a mid-stream error EVENT
    raises one (SB-6): the stream's own HTTP 200 response, with the SSE error's
    body attached — built like ``tests/fakes.py::api_status_error``, but with the
    status and body a real mid-stream error carries."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(200, request=request, headers={"request-id": "req_scripted"})
    body = {"type": "error", "error": {"type": error_type, "message": "scripted mid-stream error"}}
    return anthropic.APIStatusError("scripted mid-stream error", response=response, body=body)


# --- conversation -----------------------------------------------------------


async def test_a_follow_up_replays_the_prior_turn_as_text(client, db_session, install_llm):
    fake = install_llm(
        ChatScript(segments=[("First answer.", [])]), ChatScript(segments=[("Second answer.", [])])
    )
    aid, _, headers, _ = await _setup(db_session)
    await _ask(client, aid, headers, "First question?")
    _, frames = await _ask(client, aid, headers, "Second question?")
    assert frames[-1][0] == "done"
    second = fake.calls[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    assert second[0]["content"][-1] == {"type": "text", "text": "First question?"}
    assert second[1]["content"] == [{"type": "text", "text": "First answer."}]
    assert second[2]["content"] == [{"type": "text", "text": "Second question?"}]
    assert {block["type"] for m in second for block in m["content"]} == {"document", "text"}


async def test_a_refused_turn_is_shown_but_never_replayed(client, db_session, install_llm):
    fake = install_llm(
        ChatScript(
            segments=[("Partial", [])], stop_reason="refusal",
            stop_details={"type": "refusal", "category": "bio", "explanation": "x"},
        ),
        ChatScript(segments=[("Fine.", [])]),
    )
    aid, _, headers, _ = await _setup(db_session)
    _, frames = await _ask(client, aid, headers, "Q1")
    turn = frames[-1][1]["turn"]
    assert (turn["status"], turn["refusal_category"], turn["answer_text"]) == ("refused", "bio", "")
    # SB-8/PA1-7/SW-9: a refused turn is never in the replay window, and the `done`
    # payload now says so instead of hard-coding True.
    assert turn["in_window"] is False
    _, frames2 = await _ask(client, aid, headers, "Q2")
    assert frames2[-1][1]["turn"]["in_window"] is True
    assert len(fake.calls[1]["messages"]) == 1
    history = (await client.get(history_url(aid), headers=headers)).json()
    assert [t["status"] for t in history["turns"]] == ["refused", "complete"]


async def test_a_move_between_tiers_hides_the_old_tier(client, db_session, install_llm):
    install_llm()
    seeded = await seed_interview(db_session)
    user = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    headers = auth_headers(user.id)
    await _ask(client, seeded.assessment_id, headers)
    user.user_role = USER_ROLE_REVIEWER
    await db_session.flush()
    history = (await client.get(history_url(seeded.assessment_id), headers=headers)).json()
    assert (history["tier"], history["turns"]) == ("reviewer", [])
    cleared = await client.post(clear_url(seeded.assessment_id), json={}, headers=headers)
    assert cleared.json() == {"deleted": 1}  # D16: Clear reaches every tier


async def test_clear_deletes_turns_but_keeps_the_ledger_and_the_count(client, db_session, install_llm):
    install_llm()
    aid, user_id, headers, _ = await _setup(db_session)
    await _ask(client, aid, headers)
    cleared = await client.post(clear_url(aid), json={}, headers=headers)
    assert (cleared.status_code, cleared.json()) == (200, {"deleted": 1})
    assert await _turns(db_session, aid) == []
    [row] = await _ledger(db_session, user_id)
    assert (row.turn_id, row.status) == (None, "complete")
    history = (await client.get(history_url(aid), headers=headers)).json()
    assert history["questions_used_24h"] == 1


async def test_two_users_never_see_each_other(client, db_session, install_llm):
    install_llm()
    aid, _, alice, _ = await _setup(db_session, role=USER_ROLE_ADMIN)
    bob = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    await _ask(client, aid, alice)
    assert (await client.get(history_url(aid), headers=auth_headers(bob.id))).json()["turns"] == []
    assert len((await client.get(history_url(aid), headers=alice)).json()["turns"]) == 1


async def test_an_answer_completes_and_persists_without_a_listener(db_session, install_llm):
    install_llm(ChatScript(segments=[("Answered while nobody listened.", [])]))
    seeded = await seed_interview(db_session)
    user = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    prepared = await chat.prepare_turn(
        db_session, assessment_id=seeded.assessment_id, user=user, question_raw="Anyone there?"
    )
    chat.start_turn(prepared)  # the queue is never read: the browser went away
    await chat.drain_live_tasks()
    [turn] = await _turns(db_session, seeded.assessment_id)
    assert (turn.status, turn.answer_text) == ("complete", "Answered while nobody listened.")


async def _drain(queue: asyncio.Queue) -> list:
    events = []
    while True:
        item = await queue.get()
        if item is None:
            return events
        events.append(item)


async def test_a_cancelled_turn_is_interrupted_and_still_ends_the_queue(db_session, install_llm):
    # [PB4/PA1-3] The queue must end with None even on the CancelledError path
    # (SB-1/SW-5's finally), so the SSE response completes rather than hanging.
    script = _paused()
    install_llm(script)
    seeded = await seed_interview(db_session)
    user = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    prepared = await chat.prepare_turn(
        db_session, assessment_id=seeded.assessment_id, user=user, question_raw="Q"
    )
    queue = chat.start_turn(prepared)
    await asyncio.wait_for(script.paused.wait(), 10)
    [task] = list(chat._LIVE_TASKS)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    events = await _drain(queue)
    assert events[0][0] == "turn"
    [turn] = await _turns(db_session, seeded.assessment_id)
    assert turn.status == "interrupted"
    [row] = await _ledger(db_session, user.id)
    # The snapshot kept message_start's input tokens, marked partial (no
    # message_delta ever landed), so the ceilings still floor it at the reserve.
    assert row.status == "interrupted" and row.input_tokens == 1200
    assert any(isinstance(e, dict) and e.get("partial") for e in row.usage_by_model)
    assert await chat.spend_24h(db_session, user_id=user.id) == Decimal("2.50")


async def test_a_non_sdk_exception_mid_stream_is_logged_by_class_only(
    client, db_session, install_llm, caplog
):
    # [PB4/PA1-3] run_turn's catch-all: an exception that is not one of anthropic's
    # own SDK error classes still ends the turn `failed`/`upstream_error`, and the
    # log names only the exception's class, never its message (which could carry
    # request content).
    install_llm(ChatScript(raise_after_events=2, raise_exc=RuntimeError("SECRET-DETAIL-8842")))
    aid, _, headers, _ = await _setup(db_session)
    with caplog.at_level(logging.ERROR, logger="src.services.assessment_chat"):
        _, frames = await _ask(client, aid, headers)
    assert frames[-1] == ("error", {"code": "upstream_error"})
    [turn] = await _turns(db_session, aid)
    assert (turn.status, turn.error_code) == ("failed", "upstream_error")
    assert "RuntimeError" in caplog.text
    assert "SECRET-DETAIL-8842" not in caplog.text


async def test_an_unpriced_model_is_refused_before_any_call_or_row(
    client, db_session, install_llm, monkeypatch
):
    # [PB5/PA1-4] The model_unpriced guard runs before the request is ever built
    # (before a row is committed), so a mid-flight repricing mistake spends
    # nothing and leaves no trace to clean up.
    fake = install_llm()
    monkeypatch.setattr(get_settings(), "llm_assessment_chat_model", "claude-unpriced-9")
    aid, user_id, headers, _ = await _setup(db_session)
    resp = await client.post(ask_url(aid), json={"question": "q"}, headers=headers)
    assert (resp.status_code, resp.json()) == (503, {"error": "model_unpriced"})
    assert fake.calls == []
    assert await _turns(db_session, aid) == []
    assert await _ledger(db_session, user_id) == []


# --- limits -----------------------------------------------------------------


async def test_a_question_committed_while_waiting_for_the_lock_is_counted(
    client, db_session, install_llm, monkeypatch
):
    # Another ask of this user's commits between the unlocked head-start check and
    # the lock (R2SEC-3): the count read under the lock must see it.
    fake = install_llm()
    aid, user_id, headers, _ = await _setup(db_session)
    db_session.add_all(
        [_ledger_row(user_id, age=timedelta(hours=23 - i * 0.2), usage=[]) for i in range(99)]
    )
    await db_session.flush()
    take = chat._take_spend_lock

    async def racing_take(db):
        db.add(_ledger_row(user_id, age=timedelta(seconds=1), usage=[]))
        await db.flush()
        await take(db)

    monkeypatch.setattr(chat, "_take_spend_lock", racing_take)
    resp = await client.post(ask_url(aid), json={"question": "q"}, headers=headers)
    assert (resp.status_code, resp.json()["error"]) == (429, "daily_limit")
    assert fake.calls == []


async def test_the_101st_question_is_refused_even_after_clear(client, db_session, install_llm):
    fake = install_llm()
    aid, user_id, headers, _ = await _setup(db_session)
    db_session.add_all(
        [_ledger_row(user_id, age=timedelta(hours=23 - i * 0.2), usage=[]) for i in range(100)]
    )
    await db_session.flush()
    first = await client.post(ask_url(aid), json={"question": "q"}, headers=headers)
    assert first.status_code == 429
    assert first.json()["error"] == "daily_limit" and first.json()["resets_at"]
    await client.post(clear_url(aid), json={}, headers=headers)
    again = await client.post(ask_url(aid), json={"question": "q"}, headers=headers)
    assert again.json()["error"] == "daily_limit"
    assert fake.calls == []


async def test_the_per_user_dollar_ceiling(client, db_session, install_llm):
    fake = install_llm()
    aid, user_id, headers, _ = await _setup(db_session)
    db_session.add(_ledger_row(user_id, usage=_priced(20)))
    await db_session.flush()
    resp = await client.post(ask_url(aid), json={"question": "q"}, headers=headers)
    assert (resp.status_code, resp.json()) == (429, {"error": "daily_spend_limit"})
    assert fake.calls == []


async def test_the_global_dollar_ceiling(client, db_session, install_llm):
    fake = install_llm()
    aid, user_id, headers, _ = await _setup(db_session)
    others = [await factories.make_user(db_session) for _ in range(5)]
    db_session.add_all([_ledger_row(o.id, usage=_priced(19.99)) for o in others])  # $99.95
    db_session.add(_ledger_row(user_id, usage=_priced(0.10)))                       # +$0.10
    await db_session.flush()
    resp = await client.post(ask_url(aid), json={"question": "q"}, headers=headers)
    assert (resp.status_code, resp.json()) == (429, {"error": "global_spend_limit"})
    assert fake.calls == []


async def test_rows_with_no_recorded_usage_count_the_reserve(client, db_session, install_llm):
    fake = install_llm()
    aid, user_id, headers, _ = await _setup(db_session)
    db_session.add_all([_ledger_row(user_id, status="failed", usage=None) for _ in range(8)])
    await db_session.flush()  # 8 x $2.50 = the $20 ceiling
    resp = await client.post(ask_url(aid), json={"question": "q"}, headers=headers)
    assert (resp.status_code, resp.json()) == (429, {"error": "daily_spend_limit"})
    assert fake.calls == []


async def test_a_second_question_while_one_is_in_flight_is_409(client, db_session, install_llm):
    script = _paused()
    fake = install_llm(script, ChatScript())
    aid, _, headers, _ = await _setup(db_session)
    first = asyncio.create_task(client.post(ask_url(aid), json={"question": "Q1"}, headers=headers))
    await asyncio.wait_for(script.paused.wait(), 10)
    second = await client.post(ask_url(aid), json={"question": "Q2"}, headers=headers)
    assert (second.status_code, second.json()) == (409, {"error": "answer_in_progress"})
    cleared = await client.post(clear_url(aid), json={}, headers=headers)
    assert (cleared.status_code, cleared.json()) == (409, {"error": "answer_in_progress"})
    script.release.set()
    assert parse_sse((await first).text)[-1][0] == "done"
    assert len(fake.calls) == 1


async def test_the_in_flight_refusal_is_the_early_check_and_never_takes_the_lock(
    client, db_session, install_llm, monkeypatch
):
    # RSEC-3: a repeat click is refused before the spend lock is ever taken, so it
    # can never queue behind another ask holding it.
    script = _paused()
    fake = install_llm(script, ChatScript())
    aid, _, headers, _ = await _setup(db_session)
    first = asyncio.create_task(client.post(ask_url(aid), json={"question": "Q1"}, headers=headers))
    await asyncio.wait_for(script.paused.wait(), 10)

    async def _must_not_be_called(db):
        raise AssertionError("a repeat click must be refused before the lock is taken")

    monkeypatch.setattr(chat, "_take_spend_lock", _must_not_be_called)
    second = await client.post(ask_url(aid), json={"question": "Q2"}, headers=headers)
    assert (second.status_code, second.json()) == (409, {"error": "answer_in_progress"})
    script.release.set()
    assert parse_sse((await first).text)[-1][0] == "done"
    assert len(fake.calls) == 1  # the second ask never reached the model either


@pytest.mark.parametrize("route", ["history", "ask", "clear"])
async def test_the_sweep_frees_stale_rows_of_any_user_and_they_keep_their_reserve(
    client, db_session, install_llm, route
):
    install_llm()
    seeded = await seed_interview(db_session)
    aid = seeded.assessment_id
    a = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    b = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    c = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    a_id, b_id = a.id, b.id
    now = datetime.now(UTC)

    def streaming_turn(user, tier, age):
        return AssessmentChatTurn(
            id=uuid.uuid4(), assessment_id=aid, user_id=user.id, context_tier=tier, question="q",
            status="streaming", model="claude-opus-5-5", record_sha256_12="0" * 12,
            prompt_sha256_12="0" * 12, created_at=now - age,
        )

    stale = streaming_turn(b, "staff", timedelta(seconds=301))
    live = streaming_turn(c, "reviewer", timedelta(seconds=200))
    db_session.add_all([stale, live])
    await db_session.flush()
    stale_id, live_id = stale.id, live.id
    db_session.add_all([
        _ledger_row(b_id, age=timedelta(seconds=301), status="streaming", turn_id=stale_id,
                    assessment_id=aid),
        _ledger_row(c.id, age=timedelta(seconds=200), status="streaming", turn_id=live_id,
                    assessment_id=aid, tier="reviewer"),
    ])
    await db_session.flush()

    headers = auth_headers(a_id)
    if route == "history":
        resp = await client.get(history_url(aid), headers=headers)
    elif route == "ask":
        resp = await client.post(ask_url(aid), json={"question": "q"}, headers=headers)
    else:
        resp = await client.post(clear_url(aid), json={}, headers=headers)
    assert resp.status_code == 200

    statuses = {t.id: t.status for t in await _turns(db_session, aid) if t.user_id != a_id}
    assert statuses == {stale_id: "interrupted", live_id: "streaming"}
    [b_row] = await _ledger(db_session, b_id)
    assert (b_row.status, b_row.usage_by_model) == ("interrupted", None)
    # Its tokens are unknown but probably billed: it keeps counting the reserve
    # (the plan's clarification 5).
    assert await chat.spend_24h(db_session, user_id=b_id) == Decimal("2.50")


async def test_an_answer_that_outlives_the_sweep_is_dropped_but_its_usage_is_kept(
    client, db_session, install_llm
):
    script = _paused()
    install_llm(script)
    aid, user_id, headers, _ = await _setup(db_session)
    task = asyncio.create_task(client.post(ask_url(aid), json={"question": "q"}, headers=headers))
    await asyncio.wait_for(script.paused.wait(), 10)
    for table in ("assessment_chat_turns", "assessment_chat_usage"):
        await db_session.execute(
            text(f"UPDATE {table} SET created_at = created_at - interval '400 seconds' WHERE user_id = :u"),
            {"u": user_id},
        )
    await client.get(history_url(aid), headers=headers)  # sweeps the in-flight turn
    script.release.set()
    assert parse_sse((await task).text)[-1] == ("error", {"code": "storage_error"})
    [turn] = await _turns(db_session, aid)
    assert (turn.status, turn.answer_text) == ("interrupted", "")
    [row] = await _ledger(db_session, user_id)
    assert (row.status, row.input_tokens) == ("complete", 1200)


async def test_a_stuck_lock_holder_degrades_the_asker_to_busy(
    client, db_session, engine, install_llm, monkeypatch
):
    # RSEC-3: the lock's wait is bounded. The holder must be a SEPARATE connection —
    # Postgres advisory locks are re-entrant only within one session, so taking the
    # lock again on db_session's own connection would just succeed at once rather
    # than reproduce a stuck holder.
    monkeypatch.setattr(chat, "SPEND_LOCK_WAIT_SECONDS", 0.2)
    fake = install_llm()
    aid, _, headers, _ = await _setup(db_session)
    async with engine.connect() as holder:
        await holder.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": chat._SPEND_LOCK_KEY})
        resp = await client.post(ask_url(aid), json={"question": "q"}, headers=headers)
        assert (resp.status_code, resp.json()) == (503, {"error": "busy"})
        await holder.rollback()  # releases the advisory lock
    assert fake.calls == []


async def test_a_full_conversation_is_409(client, db_session, install_llm, monkeypatch):
    fake = install_llm()
    monkeypatch.setattr(get_settings(), "assessment_chat_max_turns", 2)
    aid, _, headers, _ = await _setup(db_session)
    await _ask(client, aid, headers, "one")
    await _ask(client, aid, headers, "two")
    resp = await client.post(ask_url(aid), json={"question": "three"}, headers=headers)
    assert (resp.status_code, resp.json()) == (409, {"error": "conversation_full"})
    assert len(fake.calls) == 2


# --- upstream failures ------------------------------------------------------


async def test_the_deadline_fails_the_turn_and_prices_what_streamed(client, db_session, install_llm, monkeypatch):
    install_llm(ChatScript(pause_after_events=3, pause_seconds=5.0))
    monkeypatch.setattr(chat, "DEADLINE_SECONDS", 0.2)
    aid, user_id, headers, _ = await _setup(db_session)
    _, frames = await _ask(client, aid, headers)
    assert frames[-1] == ("error", {"code": "timeout"})
    [turn] = await _turns(db_session, aid)
    assert (turn.status, turn.error_code) == ("failed", "timeout")
    [row] = await _ledger(db_session, user_id)
    assert (row.input_tokens, row.cache_creation_input_tokens, row.output_tokens) == (1200, 30000, 1)
    # A partial entry (message_start's usage, never followed by a message_delta) is
    # floored at the reserve (SEC-4/SB-5): its priced cost here is far under $2.50.
    assert await chat.spend_24h(db_session, user_id=user_id) == Decimal("2.50")


@pytest.mark.parametrize(
    "cls,status,code",
    [
        (anthropic.RateLimitError, 429, "upstream_rate_limited"),
        (anthropic.APIStatusError, 529, "upstream_overloaded"),
        (anthropic.BadRequestError, 400, "upstream_bad_request"),
        (anthropic.InternalServerError, 500, "upstream_error"),
    ],
)
async def test_an_http_error_before_any_output_records_no_cost(
    client, db_session, install_llm, caplog, cls, status, code
):
    install_llm(ChatScript(raise_on_open=api_status_error(cls, status)))
    aid, user_id, headers, _ = await _setup(db_session)
    with caplog.at_level(logging.ERROR, logger="src.services.assessment_chat"):
        _, frames = await _ask(client, aid, headers)
    assert frames[-1] == ("error", {"code": code})
    [row] = await _ledger(db_session, user_id)
    assert (row.status, row.usage_by_model, row.input_tokens) == ("failed", [], 0)
    assert await chat.spend_24h(db_session, user_id=user_id) == Decimal("0")
    if code == "upstream_bad_request":
        assert "req_scripted" in caplog.text


async def test_a_connection_error_counts_the_reserve(client, db_session, install_llm):
    install_llm(ChatScript(raise_on_open=connection_error()))
    aid, user_id, headers, _ = await _setup(db_session)
    _, frames = await _ask(client, aid, headers)
    assert frames[-1] == ("error", {"code": "upstream_error"})
    [row] = await _ledger(db_session, user_id)
    assert row.usage_by_model is None
    assert await chat.spend_24h(db_session, user_id=user_id) == Decimal("2.50")


async def test_a_mid_stream_error_event_after_output_counts_the_reserve(
    client, db_session, install_llm
):
    # Both SDKs raise a mid-stream error EVENT as an APIStatusError over the
    # stream's own HTTP 200 response (SB-6) — status_code alone cannot tell that
    # apart from a real HTTP failure, so the body's declared error type decides.
    install_llm(ChatScript(raise_after_events=3, raise_exc=_mid_stream_status_error("overloaded_error")))
    aid, user_id, headers, _ = await _setup(db_session)
    _, frames = await _ask(client, aid, headers)
    assert frames[-1] == ("error", {"code": "upstream_overloaded"})
    [row] = await _ledger(db_session, user_id)
    assert row.usage_by_model is not None  # message_start had already landed
    assert await chat.spend_24h(db_session, user_id=user_id) == Decimal("2.50")


async def test_a_mid_stream_rate_limit_event_is_classified_by_body(client, db_session, install_llm):
    install_llm(ChatScript(raise_after_events=3, raise_exc=_mid_stream_status_error("rate_limit_error")))
    aid, _, headers, _ = await _setup(db_session)
    _, frames = await _ask(client, aid, headers)
    assert frames[-1] == ("error", {"code": "upstream_rate_limited"})


async def test_a_mid_stream_error_before_any_event_records_unknown_usage(
    client, db_session, install_llm
):
    # Nothing was ever absorbed into the snapshot, and status 200 means the
    # request cannot be assumed unbilled either — usage stays genuinely unknown.
    install_llm(ChatScript(raise_after_events=0, raise_exc=_mid_stream_status_error("overloaded_error")))
    aid, user_id, headers, _ = await _setup(db_session)
    _, frames = await _ask(client, aid, headers)
    assert frames[-1] == ("error", {"code": "upstream_overloaded"})
    [row] = await _ledger(db_session, user_id)
    assert row.usage_by_model is None
    assert await chat.spend_24h(db_session, user_id=user_id) == Decimal("2.50")


async def test_an_unknown_stop_reason_is_a_named_failure(client, db_session, install_llm):
    install_llm(ChatScript(segments=[("Some text.", [])], stop_reason="pause_turn"))
    aid, _, headers, _ = await _setup(db_session)
    _, frames = await _ask(client, aid, headers)
    assert frames[-1] == ("error", {"code": "unexpected_stop"})
    [turn] = await _turns(db_session, aid)
    assert turn.error_code == "unexpected_stop"
    history = (await client.get(history_url(aid), headers=headers)).json()
    assert history["turns"][0]["error_code"] == "unexpected_stop"


# --- logging ----------------------------------------------------------------


async def test_a_completed_turn_logs_ids_and_tokens_but_no_content(client, db_session, install_llm, caplog):
    install_llm(ChatScript(segments=[("ANSWER-NOT-IN-LOGS", [])]))
    aid, _, headers, _ = await _setup(db_session)
    with caplog.at_level(logging.DEBUG):
        _, frames = await _ask(client, aid, headers, "QUESTION-NOT-IN-LOGS")
    assert f"Assessment chat turn {frames[-1][1]['turn']['id']}" in caplog.text
    assert "QUESTION-NOT-IN-LOGS" not in caplog.text
    assert "ANSWER-NOT-IN-LOGS" not in caplog.text


async def test_a_failed_insert_is_a_500_that_logs_no_question_text(
    client, db_session, install_llm, monkeypatch, caplog
):
    fake = install_llm()
    aid, _, headers, _ = await _setup(db_session)
    real_new_rows = chat._new_rows

    def broken(**kwargs):
        turn, usage = real_new_rows(**kwargs)
        turn.context_tier = "not-a-tier"  # violates ck_assessment_chat_turns_tier
        return turn, usage

    monkeypatch.setattr(chat, "_new_rows", broken)
    secret = "SECRET-QUESTION-TEXT-7731"
    with caplog.at_level(logging.DEBUG):
        resp = await client.post(ask_url(aid), json={"question": secret}, headers=headers)
    assert (resp.status_code, resp.json()) == (500, {"error": "storage_error"})
    assert secret not in caplog.text
    assert "IntegrityError" in caplog.text
    assert fake.calls == []


async def test_a_failed_persist_ends_the_stream_and_logs_no_content(
    client, db_session, install_llm, monkeypatch, caplog
):
    install_llm(ChatScript(segments=[("SECRET-ANSWER-TEXT-4410", [])]))
    aid, _, headers, _ = await _setup(db_session)

    def unstorable(final, *, record, requested_model):
        return StreamOutcome(status="bogus-state", answer_text="SECRET-ANSWER-TEXT-4410")

    monkeypatch.setattr(chat, "outcome_from_final", unstorable)
    question = "SECRET-QUESTION-TEXT-9920"
    with caplog.at_level(logging.DEBUG):
        resp = await client.post(ask_url(aid), json={"question": question}, headers=headers)
    assert parse_sse(resp.text)[-1] == ("error", {"code": "storage_error"})
    assert "could not persist turn" in caplog.text
    assert "SECRET-ANSWER-TEXT-4410" not in caplog.text
    assert question not in caplog.text


async def test_a_non_sqlalchemy_persist_failure_still_fails_the_turn_at_once(
    client, db_session, install_llm, monkeypatch, caplog
):
    # SB-1/SW-5: _persist's outer catch is not narrowed to SQLAlchemyError — a
    # session that cannot even be opened must still end the turn rather than
    # strand it in `streaming` for STALE_AFTER_SECONDS.
    install_llm(ChatScript(segments=[("An answer.", [])]))
    aid, _, headers, _ = await _setup(db_session)
    real_own_session = chat._own_session
    calls = {"n": 0}

    @asynccontextmanager
    async def flaky_once():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("scripted disk failure")
        async with real_own_session() as db:
            yield db

    monkeypatch.setattr(chat, "_own_session", flaky_once)
    with caplog.at_level(logging.DEBUG):
        resp = await client.post(ask_url(aid), json={"question": "q1"}, headers=headers)
    assert parse_sse(resp.text)[-1] == ("error", {"code": "storage_error"})
    assert "OSError" in caplog.text
    [turn] = await _turns(db_session, aid)
    assert (turn.status, turn.error_code) == ("failed", "storage_error")
    # _mark_storage_failed's own write is the SECOND call and succeeds, so the
    # user's very next question is accepted at once rather than waiting on the sweep.
    again = await client.post(ask_url(aid), json={"question": "q2"}, headers=headers)
    assert again.status_code == 200
    assert parse_sse(again.text)[-1][0] == "done"


# --- deletion ---------------------------------------------------------------


async def test_deleting_the_assessment_or_the_user_keeps_the_ledger(client, db_session, install_llm):
    install_llm()
    aid, user_id, headers, _ = await _setup(db_session)
    await _ask(client, aid, headers)
    await db_session.execute(text("DELETE FROM opportunity_assessments WHERE id = :id"), {"id": aid})
    turns = await db_session.execute(
        text("SELECT count(*) FROM assessment_chat_turns WHERE user_id = :u"), {"u": user_id}
    )
    assert turns.scalar_one() == 0
    row = (
        await db_session.execute(
            text("SELECT assessment_id, input_tokens FROM assessment_chat_usage WHERE user_id = :u"),
            {"u": user_id},
        )
    ).one()
    assert (row.assessment_id, row.input_tokens) == (None, 1200)
    await db_session.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
    orphans = await db_session.execute(
        text("SELECT count(*) FROM assessment_chat_usage WHERE user_id IS NULL AND input_tokens = 1200")
    )
    assert orphans.scalar_one() == 1


async def test_verdict_may_change_true_while_the_run_is_running(client, db_session, install_llm):
    install_llm()
    aid, _, headers, _ = await _setup(db_session)
    assert (await client.get(history_url(aid), headers=headers)).json()["verdict_may_change"] is True


async def test_verdict_may_change_false_once_announced_even_while_running(
    client, db_session, install_llm
):
    # [PB6/PA1-5] `summary_posted_at` wins outright: an announced headline cannot
    # be retracted no matter what the run is doing.
    install_llm()
    aid, _, headers, _ = await _setup(db_session, summary_posted_at=datetime.now(UTC))
    assert (await client.get(history_url(aid), headers=headers)).json()["verdict_may_change"] is False


async def test_verdict_may_change_true_on_a_stopped_but_latest_run(client, db_session, install_llm):
    # [PA1-5] SB-10: a stopped run that is still the LATEST run by `started_at` is
    # the one `src/agent/main.py` resumes next, so it can still supersede its own
    # verdicts — this is what the previous flip-status-only version of this test
    # got wrong under the new rule.
    install_llm()
    aid, _, headers, seeded = await _setup(db_session)
    seeded.run.status = "stopped"
    await db_session.flush()
    assert (await client.get(history_url(aid), headers=headers)).json()["verdict_may_change"] is True


async def test_verdict_may_change_false_on_a_stopped_run_that_is_not_the_latest(
    client, db_session, install_llm
):
    install_llm()
    aid, _, headers, seeded = await _setup(db_session)
    seeded.run.status = "stopped"
    await db_session.flush()
    await factories.make_simulation_run(
        db_session, status="completed", started_at=datetime.now(UTC) + timedelta(hours=1)
    )
    assert (await client.get(history_url(aid), headers=headers)).json()["verdict_may_change"] is False


# --- Review Focus -----------------------------------------------------------


async def test_an_assessment_without_a_transcript_can_still_be_asked(client, db_session, install_llm):
    fake = install_llm()
    aid, _, headers, _ = await _setup(db_session, with_messages=False)
    _, frames = await _ask(client, aid, headers)
    assert frames[-1][0] == "done"
    documents = fake.calls[0]["messages"][0]["content"][:5]
    interview = documents[1]["source"]["content"]
    assert len(interview) == 1 and interview[0]["text"].startswith("[Transcript unavailable")
    assert documents[2]["source"]["content"][0]["text"].startswith("[Consult cards not shown")


async def test_history_shows_an_answer_in_progress_and_then_its_result(client, db_session, install_llm):
    script = _paused()
    script.segments = [("Done now.", [])]
    install_llm(script)
    aid, _, headers, _ = await _setup(db_session)
    task = asyncio.create_task(client.post(ask_url(aid), json={"question": "Q"}, headers=headers))
    await asyncio.wait_for(script.paused.wait(), 10)
    during = (await client.get(history_url(aid), headers=headers)).json()
    assert [t["status"] for t in during["turns"]] == ["streaming"]
    assert during["questions_used_24h"] == 1
    script.release.set()
    await task
    after = (await client.get(history_url(aid), headers=headers)).json()
    assert [(t["status"], t["answer_text"]) for t in after["turns"]] == [("complete", "Done now.")]


async def test_an_answer_is_flagged_when_the_record_changes_after_it(client, db_session, install_llm):
    install_llm()
    aid, _, headers, _ = await _setup(db_session)
    await _ask(client, aid, headers)
    before = (await client.get(history_url(aid), headers=headers)).json()
    assert before["turns"][0]["record_changed"] is False
    db_session.add(AssessmentReview(
        assessment_id=aid, reviewer_user_id=None, reviewer_name="Later Reviewer", score=3,
        comment="A later review.", feedback_mode="log_only",
    ))
    await db_session.flush()
    after = (await client.get(history_url(aid), headers=headers)).json()
    assert after["turns"][0]["record_changed"] is True


async def test_markup_in_a_question_is_stored_and_sent_verbatim(client, db_session, install_llm):
    fake = install_llm()
    aid, _, headers, _ = await _setup(db_session)
    question = '<img src=x onerror="alert(1)"> **bold** [link](https://attacker.example/x) `code`'
    _, frames = await _ask(client, aid, headers, question)
    assert frames[-1][1]["turn"]["question"] == question
    assert fake.calls[0]["messages"][-1]["content"][-1] == {"type": "text", "text": question}
    [turn] = await _turns(db_session, aid)
    assert turn.question == question


async def test_an_assessment_deleted_mid_answer_keeps_its_usage(client, db_session, install_llm):
    script = _paused()
    install_llm(script)
    aid, user_id, headers, _ = await _setup(db_session)
    task = asyncio.create_task(client.post(ask_url(aid), json={"question": "Q"}, headers=headers))
    await asyncio.wait_for(script.paused.wait(), 10)
    await db_session.execute(text("DELETE FROM opportunity_assessments WHERE id = :id"), {"id": aid})
    script.release.set()
    assert parse_sse((await task).text)[-1] == ("error", {"code": "storage_error"})
    left = await db_session.execute(
        text("SELECT count(*) FROM assessment_chat_turns WHERE user_id = :u"), {"u": user_id}
    )
    assert left.scalar_one() == 0
    row = (
        await db_session.execute(
            text("SELECT assessment_id, status, input_tokens FROM assessment_chat_usage WHERE user_id = :u"),
            {"u": user_id},
        )
    ).one()
    assert (row.assessment_id, row.status, row.input_tokens) == (None, "complete", 1200)
    assert (await client.get(history_url(aid), headers=headers)).status_code == 404
