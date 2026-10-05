"""The chat's opening questions end to end (docs/operations/assessment-chat.md,
"Opening questions"): the worker's generation, what the detail pages render, the
drawer-opening counter, and the question origin on the ledger."""

import json
import re
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import anthropic
import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from src.config import get_settings
from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    USER_ROLE_REVIEWER,
    AssessmentChatOpen,
    AssessmentChatSuggestionSet,
    AssessmentChatUsage,
)
from src.services import assessment_chat_suggestions as sug
from src.services import llm
from tests import factories
from tests.assessment_chat_support import (
    STAFF_ONLY_TEXT,
    ask_url,
    seed_interview,
    use_fake_llm,
    use_test_session,
)
from tests.fakes import ChatScript, FakeAnthropic, FakeAsyncAnthropic
from tests.integration.test_manager_access import auth_headers

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
    monkeypatch.setattr(settings, "assessment_chat_enabled", True)
    monkeypatch.setattr(settings, "assessment_chat_suggestions_enabled", True)
    monkeypatch.setattr(settings, "llm_assessment_chat_suggestions_model", "claude-opus-5")
    monkeypatch.setattr(settings, "assessment_chat_suggestions_daily_usd_limit", 30.0)
    sug._UNBUILDABLE.clear()
    return settings


def _ids(kwargs) -> dict[str, str]:
    """``{label: id}`` from the request's block list (first id per label)."""
    text = kwargs["messages"][0]["content"][-1]["text"]
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"(V\d+): (.*)$", line)
        if m:
            out.setdefault(m.group(2), m.group(1))
    return out


def _good_reply(kwargs):
    ids = _ids(kwargs)
    gate = next(v for k, v in ids.items() if k.startswith("Gate — translational"))
    flag = next(v for k, v in ids.items() if k.startswith("Red flag 1"))
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps({"questions": [
            {"block": gate, "question": "Was translational potential ever put to the lab's agent?"},
            {"block": flag, "question": "How did the lab's agent answer CHAT-FIXTURE-RED-FLAG?"},
        ]}))],
        stop_reason="end_turn",
        stop_details=None,
        model="claude-opus-5",
        usage=SimpleNamespace(
            input_tokens=80_000, output_tokens=700, cache_read_input_tokens=0,
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


async def _sets(db_session) -> list[AssessmentChatSuggestionSet]:
    rows = await db_session.execute(
        select(AssessmentChatSuggestionSet)
        .order_by(AssessmentChatSuggestionSet.created_at, AssessmentChatSuggestionSet.context_tier)
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars())


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


async def test_the_staff_set_is_generated_once_and_only_staff_without_reviewers(
    db_session, factory, calls
):
    seeded = await seed_interview(db_session)
    await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)

    assert await sug.generate_due(factory) is True
    (row,) = await _sets(db_session)
    assert (row.assessment_id, row.context_tier, row.verdict_revision) == (seeded.assessment_id, "staff", 1)
    assert (row.status, row.error_code, row.attempts) == ("ready", None, 1)
    assert [s["anchor"] for s in row.suggestions] == ["gating", "red-flags"]
    assert row.served_by_model == "claude-opus-5"
    assert row.usage_by_model[0]["input_tokens"] == 80_000
    assert len(row.prompt_sha256_12) == 12 and len(row.record_sha256_12) == 12
    # The staff record went out, staff-only fields included, citations off.
    (request,) = calls.kwargs
    assert STAFF_ONLY_TEXT in json.dumps(request["messages"])
    assert request["model"] == "claude-opus-5"
    assert calls.options == [{"timeout": sug.CALL_TIMEOUT_SECONDS, "max_retries": 0}]

    assert await sug.generate_due(factory) is False  # nothing left to do
    assert len(calls.kwargs) == 1


async def test_a_reviewer_account_adds_the_reviewer_set_from_the_reviewer_record(
    db_session, factory, calls
):
    await seed_interview(db_session)
    await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)

    assert await sug.generate_due(factory) is True
    assert await sug.generate_due(factory) is True
    assert await sug.generate_due(factory) is False
    assert sorted(r.context_tier for r in await _sets(db_session)) == ["reviewer", "staff"]
    staff_request, reviewer_request = calls.kwargs
    assert STAFF_ONLY_TEXT in json.dumps(staff_request["messages"])
    assert STAFF_ONLY_TEXT not in json.dumps(reviewer_request["messages"])


async def test_the_newest_assessment_goes_first(db_session, factory, calls):
    older = await seed_interview(db_session, channel="older-channel")
    newer = await seed_interview(db_session, channel="newer-channel")
    await db_session.execute(
        update(older.assessment.__class__)
        .where(older.assessment.__class__.id == older.assessment_id)
        .values(created_at=datetime.now(UTC) - timedelta(days=3))
    )
    await sug.generate_due(factory)
    (row,) = await _sets(db_session)
    assert row.assessment_id == newer.assessment_id


async def test_an_api_error_is_retried_after_the_wait_and_then_given_up(db_session, factory, calls):
    await seed_interview(db_session)
    calls.error = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.invalid"))

    assert await sug.generate_due(factory) is True
    (row,) = await _sets(db_session)
    assert (row.status, row.error_code, row.attempts) == ("failed", "api_connection", 1)
    # Cost unknown (the request may have run): priced at the chat's reserve.
    assert [e.get("partial") for e in row.usage_by_model] == [True]
    assert await sug.spend_24h(db_session) == Decimal("2.50")
    assert await sug.generate_due(factory) is False  # inside RETRY_AFTER
    assert len(calls.kwargs) == 1

    for attempt in (2, 3):
        await db_session.execute(
            update(AssessmentChatSuggestionSet).values(
                updated_at=datetime.now(UTC) - sug.RETRY_AFTER - timedelta(minutes=1)
            )
        )
        assert await sug.generate_due(factory) is True
        (row,) = await _sets(db_session)
        assert (row.status, row.attempts) == ("failed", attempt)

    await db_session.execute(
        update(AssessmentChatSuggestionSet).values(updated_at=datetime.now(UTC) - timedelta(days=1))
    )
    assert await sug.generate_due(factory) is False  # MAX_ATTEMPTS reached
    assert len(calls.kwargs) == 3


def _status_error(code: int) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.invalid/v1/messages")
    return anthropic.APIStatusError(
        f"status {code}", response=httpx.Response(code, request=request), body=None,
    )


async def test_an_overloaded_api_uses_no_attempt_and_costs_nothing(db_session, factory, calls):
    await seed_interview(db_session)
    calls.error = _status_error(529)
    for _ in range(4):
        assert await sug.generate_due(factory) is True
        (row,) = await _sets(db_session)
        assert (row.status, row.error_code, row.attempts) == ("failed", "api_status_529", 0)
        assert row.usage_by_model == []
        await db_session.execute(
            update(AssessmentChatSuggestionSet).values(
                updated_at=datetime.now(UTC) - sug.RETRY_AFTER - timedelta(minutes=1)
            )
        )
    assert await sug.spend_24h(db_session) == 0
    calls.error = None
    assert await sug.generate_due(factory) is True
    (row,) = await _sets(db_session)
    assert (row.status, row.attempts) == ("ready", 1)


async def test_a_rejected_request_uses_an_attempt_but_costs_nothing(db_session, factory, calls):
    await seed_interview(db_session)
    calls.error = _status_error(400)
    await sug.generate_due(factory)
    (row,) = await _sets(db_session)
    assert (row.error_code, row.attempts, row.usage_by_model) == ("api_status_400", 1, [])


async def test_an_attempt_the_worker_never_finished_counts_at_the_reserve(
    db_session, factory, calls
):
    """The claim is committed before the call: a worker killed mid-call leaves the
    attempt counted and priced."""
    await seed_interview(db_session)
    calls.error = RuntimeError("worker killed")
    with pytest.raises(RuntimeError):
        await sug.generate_due(factory)
    (row,) = await _sets(db_session)
    assert (row.status, row.error_code, row.attempts) == ("failed", "in_progress", 1)
    assert await sug.spend_24h(db_session) == Decimal("2.50")
    assert await sug.generate_due(factory) is False  # inside RETRY_AFTER


async def test_a_verdict_revised_after_it_was_picked_is_left_for_the_next_sweep(
    db_session, factory, calls, monkeypatch
):
    await seed_interview(db_session)
    real = sug.load_chat_record

    async def revised_meanwhile(db, assessment_id, *, tier):
        record, assessment = await real(db, assessment_id, tier=tier)
        assessment.verdict_revision = 2
        return record, assessment

    monkeypatch.setattr(sug, "load_chat_record", revised_meanwhile)
    assert await sug.generate_due(factory) is False
    assert calls.kwargs == [] and await _sets(db_session) == []


async def test_a_database_error_is_not_mistaken_for_a_bad_record(
    db_session, factory, calls, monkeypatch
):
    await seed_interview(db_session)

    async def db_down(*args, **kwargs):
        raise OperationalError("SELECT 1", {}, Exception("connection reset"))

    monkeypatch.setattr(sug, "load_chat_record", db_down)
    with pytest.raises(OperationalError):
        await sug.generate_due(factory)
    assert sug._UNBUILDABLE == set()


async def test_a_retry_that_succeeds_keeps_every_attempts_usage(db_session, factory, calls):
    await seed_interview(db_session)
    calls.reply = lambda kwargs: SimpleNamespace(
        content=[SimpleNamespace(type="text", text="not json")], stop_reason="end_turn",
        stop_details=None, model="claude-opus-5",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
                              cache_creation_input_tokens=0, iterations=None),
    )
    await sug.generate_due(factory)
    (row,) = await _sets(db_session)
    assert (row.status, row.error_code) == ("failed", "malformed_json")

    await db_session.execute(
        update(AssessmentChatSuggestionSet).values(
            updated_at=datetime.now(UTC) - sug.RETRY_AFTER - timedelta(minutes=1)
        )
    )
    calls.reply = _good_reply
    await sug.generate_due(factory)
    (row,) = await _sets(db_session)
    assert (row.status, row.attempts) == ("ready", 2)
    # Each attempt's own usage; the claim's in-flight placeholder is replaced.
    assert [e["input_tokens"] for e in row.usage_by_model] == [10, 80_000]
    assert not any(e.get("partial") for e in row.usage_by_model)


async def test_a_refusal_is_never_retried(db_session, factory, calls):
    await seed_interview(db_session)
    calls.reply = lambda kwargs: SimpleNamespace(
        content=[], stop_reason="refusal", stop_details=SimpleNamespace(category="bio"),
        model="claude-opus-5",
        usage=SimpleNamespace(input_tokens=0, output_tokens=0, cache_read_input_tokens=0,
                              cache_creation_input_tokens=0, iterations=None),
    )
    await sug.generate_due(factory)
    await db_session.execute(
        update(AssessmentChatSuggestionSet).values(updated_at=datetime.now(UTC) - timedelta(days=2))
    )
    assert await sug.generate_due(factory) is False
    (row,) = await _sets(db_session)
    assert (row.status, row.error_code) == ("refused", "refusal:bio")


async def test_a_revised_verdict_gets_its_own_set(db_session, factory, calls):
    seeded = await seed_interview(db_session)
    await sug.generate_due(factory)
    await db_session.execute(
        update(seeded.assessment.__class__)
        .where(seeded.assessment.__class__.id == seeded.assessment_id)
        .values(verdict_revision=2)
    )
    assert await sug.generate_due(factory) is True
    assert sorted(r.verdict_revision for r in await _sets(db_session)) == [1, 2]


async def test_the_daily_ceiling_stops_generation(db_session, factory, calls, switches):
    seeded = await seed_interview(db_session)
    other = await seed_interview(db_session, channel="other-channel")
    switches.assessment_chat_suggestions_daily_usd_limit = 0.40
    db_session.add(AssessmentChatSuggestionSet(
        assessment_id=other.assessment_id, context_tier="staff", verdict_revision=1,
        status="ready", model="claude-opus-5", record_sha256_12="0" * 12,
        prompt_sha256_12="1" * 12,
        # $0.45 at claude-opus-5's $5 per million input tokens.
        usage_by_model=[{"model": "claude-opus-5", "billed": True, "input_tokens": 90_000,
                         "output_tokens": 0, "cache_read_input_tokens": 0,
                         "cache_creation_input_tokens": 0}],
    ))
    await db_session.flush()
    assert await sug.generate_due(factory) is False
    assert calls.kwargs == []
    assert await sug.spend_24h(db_session) > 0
    assert seeded.assessment_id  # still due; only the ceiling held it back


@pytest.mark.parametrize("setting,value", [
    ("assessment_chat_suggestions_enabled", False),
    ("assessment_chat_enabled", False),
    ("llm_assessment_chat_suggestions_model", "claude-unpriced-9"),
])
async def test_every_switch_stops_generation(db_session, factory, calls, switches, setting, value):
    await seed_interview(db_session)
    setattr(switches, setting, value)
    assert await sug.generate_due(factory) is False
    assert calls.kwargs == []
    assert await _sets(db_session) == []


async def test_a_record_that_cannot_be_built_is_skipped_without_a_call(
    db_session, factory, calls, monkeypatch
):
    await seed_interview(db_session)

    async def boom(*args, **kwargs):
        raise RuntimeError("bad stored value")

    monkeypatch.setattr(sug, "load_chat_record", boom)
    assert await sug.generate_due(factory) is False
    assert await sug.generate_due(factory) is False
    assert calls.kwargs == []
    assert len(sug._UNBUILDABLE) == 1


# ---------------------------------------------------------------------------
# The detail pages
# ---------------------------------------------------------------------------


def _main(html: str) -> str:
    return html.split("<main", 1)[1].split("</main>", 1)[0]


def _drawer(body: str) -> str:
    return body[body.index("data-chat-starters"):].split("</div>", 1)[0]


async def _page(client, user, aid, surface="manager", **headers_kw):
    resp = await client.get(
        f"/{surface}/assessments/{aid}", headers=auth_headers(user.id, **headers_kw)
    )
    assert resp.status_code == 200
    return _main(resp.text)


@pytest.mark.parametrize("role,surface", [
    (USER_ROLE_ADMIN, "admin"), (USER_ROLE_MANAGER, "manager"), (USER_ROLE_REVIEWER, "manager"),
])
async def test_without_a_generated_set_the_page_offers_the_template_questions(
    client, db_session, role, surface
):
    seeded = await seed_interview(db_session)
    user = await factories.make_user(db_session, user_role=role)
    body = await _page(client, user, seeded.assessment_id, surface)
    drawer = _drawer(body)
    assert drawer.count("data-chat-starter ") == 4
    assert drawer.count('data-chat-origin="drawer_template"') == 4
    assert "A click sends one" in drawer
    # The seeded gate is unconfirmed; the question names it and sits in the gating card.
    gating = body[body.index('id="gating"'):].split("</details>", 1)[0]
    assert 'data-chat-origin="inline_template"' in gating
    assert "was never confirmed" in gating
    red = body[body.index('id="red-flags"'):].split('id="rationale"', 1)[0]
    assert "CHAT-FIXTURE-RED-FLAG" in red and "data-chat-ask" in red


async def test_a_ready_set_for_the_viewers_tier_and_revision_replaces_the_templates(
    client, db_session
):
    seeded = await seed_interview(db_session)
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)

    def row(tier, revision, text, status="ready"):
        return AssessmentChatSuggestionSet(
            assessment_id=seeded.assessment_id, context_tier=tier, verdict_revision=revision,
            status=status, model="claude-opus-5", record_sha256_12="0" * 12,
            prompt_sha256_12="1" * 12,
            suggestions=[{"text": text, "anchor": "scores", "label": "Dimension score"},
                         {"text": text + " (interview)", "anchor": "m-1", "label": "Message"}],
        )

    db_session.add_all([
        row("staff", 1, "STAFF-GENERATED question?"),
        row("reviewer", 1, "REVIEWER-GENERATED question?"),
        row("staff", 2, "OLD-REVISION question?"),
    ])
    await db_session.flush()

    staff_body = await _page(client, admin, seeded.assessment_id, "admin")
    assert "STAFF-GENERATED question?" in _drawer(staff_body)
    assert 'data-chat-origin="drawer_generated"' in _drawer(staff_body)
    assert "REVIEWER-GENERATED" not in staff_body and "OLD-REVISION" not in staff_body
    scores = staff_body[staff_body.index('id="scores"'):].split("</details>", 1)[0]
    assert 'data-chat-origin="inline_generated"' in scores
    # An interview-anchored question is offered in the drawer only.
    assert staff_body.count("STAFF-GENERATED question? (interview)") == 2  # text + attribute

    reviewer_body = await _page(client, reviewer, seeded.assessment_id)
    assert "REVIEWER-GENERATED question?" in _drawer(reviewer_body)
    assert "STAFF-GENERATED" not in reviewer_body


async def test_a_failed_set_leaves_the_templates_in_place(client, db_session):
    seeded = await seed_interview(db_session)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    db_session.add(AssessmentChatSuggestionSet(
        assessment_id=seeded.assessment_id, context_tier="staff", verdict_revision=1,
        status="failed", error_code="in_progress", model="claude-opus-5",
        record_sha256_12="0" * 12, prompt_sha256_12="1" * 12,
    ))
    await db_session.flush()
    body = await _page(client, admin, seeded.assessment_id, "admin")
    assert 'data-chat-origin="drawer_template"' in _drawer(body)


async def test_no_question_is_offered_while_impersonating_or_with_the_chat_off(
    asgi_app, client, db_session
):
    seeded = await seed_interview(db_session)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    body = await _page(client, admin, seeded.assessment_id, impersonate=manager.id)
    assert "data-chat-ask" not in body and "data-chat-starter" not in body

    asgi_app.state.assessment_chat_enabled = False
    body = await _page(client, admin, seeded.assessment_id, "admin")
    assert "data-chat-ask" not in body and "data-chat-starter" not in body


async def test_the_drawer_names_the_opened_url(client, db_session):
    seeded = await seed_interview(db_session)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    body = await _page(client, admin, seeded.assessment_id, "admin")
    assert f'openedUrl: "/assessment-chat/{seeded.assessment_id}/opened"' in body


# ---------------------------------------------------------------------------
# The counters
# ---------------------------------------------------------------------------


def _opened_url(aid) -> str:
    return f"/assessment-chat/{aid}/opened"


async def _opens(db_session) -> list[AssessmentChatOpen]:
    rows = await db_session.execute(
        select(AssessmentChatOpen).order_by(AssessmentChatOpen.created_at)
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars())


@pytest.mark.parametrize("role,tier", [(USER_ROLE_MANAGER, "staff"), (USER_ROLE_REVIEWER, "reviewer")])
async def test_an_opening_is_counted_without_content(client, db_session, role, tier):
    seeded = await seed_interview(db_session)
    user = await factories.make_user(db_session, user_role=role)
    resp = await client.post(
        _opened_url(seeded.assessment_id), json={"via": "inline"}, headers=auth_headers(user.id)
    )
    assert (resp.status_code, resp.json()) == (200, {"ok": True})
    assert resp.headers["cache-control"] == "no-store"
    (row,) = await _opens(db_session)
    assert (row.user_id, row.assessment_id, row.context_tier, row.opened_via) == (
        user.id, seeded.assessment_id, tier, "inline",
    )


async def test_an_unknown_via_is_counted_as_unknown(client, db_session):
    seeded = await seed_interview(db_session)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    await client.post(
        _opened_url(seeded.assessment_id), json={"via": "<script>"}, headers=auth_headers(admin.id)
    )
    (row,) = await _opens(db_session)
    assert row.opened_via is None


async def test_the_opening_counter_refuses_like_every_chat_route(client, db_session):
    seeded = await seed_interview(db_session)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    url = _opened_url(seeded.assessment_id)
    responses = [
        await client.post(url, json={}, headers=auth_headers(admin.id, impersonate=manager.id)),
        await client.post(url, json={}, headers=auth_headers(pi.id)),
        await client.post(_opened_url(uuid.uuid4()), json={}, headers=auth_headers(admin.id)),
        await client.post(url, content=b"{}", headers=auth_headers(admin.id)),
    ]
    assert [(r.status_code, r.json()["error"]) for r in responses] == [
        (403, "impersonating"), (403, "forbidden"), (404, "not_found"),
        (415, "unsupported_media_type"),
    ]
    assert await _opens(db_session) == []


@pytest.mark.parametrize("sent,stored", [
    ("inline_generated", "inline_generated"),
    ("typed", "typed"),
    ("made-up", None),
    (None, None),
])
async def test_the_ledger_records_where_a_question_came_from(
    monkeypatch, client, db_session, sent, stored
):
    use_test_session(monkeypatch, db_session)
    use_fake_llm(monkeypatch, FakeAsyncAnthropic([ChatScript()]))
    seeded = await seed_interview(db_session)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    body = {"question": "What is proposed?"}
    if sent is not None:
        body["origin"] = sent
    resp = await client.post(ask_url(seeded.assessment_id), json=body, headers=auth_headers(admin.id))
    assert resp.status_code == 200
    ledger = (
        await db_session.execute(
            select(AssessmentChatUsage).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert ledger.question_origin == stored


# ---------------------------------------------------------------------------
# The worker loop
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("generated,sleeps", [(True, []), (False, [5])])
async def test_the_worker_generates_only_when_idle_and_skips_the_sleep_after_a_call(
    engine, monkeypatch, generated, sleeps
):
    import asyncio as real_asyncio

    from src.worker import main as worker

    monkeypatch.setattr(worker, "_shutdown", False)
    monkeypatch.setattr(worker, "make_engine", lambda *a, **k: engine)
    monkeypatch.setattr(worker, "job_progress", SimpleNamespace(configure=lambda factory: None))
    monkeypatch.setattr(get_settings(), "worker_poll_interval", 5)

    async def no_requeue(*args, **kwargs):
        return 0

    async def no_job(db):
        return None

    called = []

    async def generate(factory):
        called.append(factory)
        worker._shutdown = True  # one round only
        return generated

    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(worker, "requeue_stale_processing_jobs", no_requeue)
    monkeypatch.setattr(worker, "claim_job", no_job)
    monkeypatch.setattr(worker, "generate_chat_suggestions", generate)
    monkeypatch.setattr(
        worker, "asyncio",
        SimpleNamespace(sleep=sleep, get_event_loop=real_asyncio.get_event_loop),
    )
    await worker.run_worker()
    assert len(called) == 1
    assert slept == sleeps


async def test_the_worker_never_generates_while_a_job_is_waiting(engine, monkeypatch):
    from src.worker import main as worker

    monkeypatch.setattr(worker, "_shutdown", False)
    monkeypatch.setattr(worker, "make_engine", lambda *a, **k: engine)
    monkeypatch.setattr(worker, "job_progress", SimpleNamespace(configure=lambda factory: None))

    async def no_requeue(*args, **kwargs):
        return 0

    async def a_job(db):
        return SimpleNamespace(id=uuid.uuid4(), type="generate_profile", attempts=1, max_attempts=3)

    async def process(*args, **kwargs):
        worker._shutdown = True

    async def generate(factory):
        raise AssertionError("generated while a job was waiting")

    monkeypatch.setattr(worker, "requeue_stale_processing_jobs", no_requeue)
    monkeypatch.setattr(worker, "claim_job", a_job)
    monkeypatch.setattr(worker, "process_job", process)
    monkeypatch.setattr(worker, "generate_chat_suggestions", generate)
    await worker.run_worker()
