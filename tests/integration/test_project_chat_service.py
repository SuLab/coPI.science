"""The chat service with a project subject (docs/specs/2026-10-27-projects-pages-redesign-design.md
§8.2, §8.3): a project's conversation, cap, Clear and opens are its own; one answer in flight
and the daily limits span both chats. The producer runs on the test's session and the model is
FakeAsyncAnthropic, so nothing here spends money."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.config import get_settings
from src.models import USER_ROLE_MANAGER, AssessmentChatOpen, AssessmentChatUsage
from src.models.hub_project import HubProject
from src.services import assessment_chat as chat
from tests import factories
from tests.assessment_chat_support import seed_interview, use_fake_llm, use_test_session
from tests.fakes import ChatScript, FakeAsyncAnthropic
from tests.integration.test_projects_pages import CHOICE, FINAL

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
PROJECT_PROMPT = "PROJECT-CHAT-PROMPT-SENTINEL: answer from the project record only."
_SLOTS = iter(range(1, 10_000))


@pytest.fixture
def install_llm(monkeypatch, db_session, tmp_path):
    """Route the producer to the test session, point the project prompt at a fixture file,
    and return a function that installs a FakeAsyncAnthropic with the given scripts."""
    use_test_session(monkeypatch, db_session)
    prompt = tmp_path / "project-chat.md"
    prompt.write_text(PROJECT_PROMPT, encoding="utf-8")
    monkeypatch.setattr(chat, "PROJECT_PROMPT_PATH", prompt)

    def _install(*scripts: ChatScript) -> FakeAsyncAnthropic:
        fake = FakeAsyncAnthropic(list(scripts))
        use_fake_llm(monkeypatch, fake)
        return fake

    return _install


async def seed_project(db, *, status="not_agreed", title="CHAT PROJECT", run=None):
    """One project; finished (with an opening and a closing plan) unless ``status`` is open."""
    run = run or await factories.make_simulation_run(db, status="stopped", started_at=T0)
    finished = status in ("ready", "not_agreed", "not_finished")
    project = HubProject(
        simulation_run_id=run.id, agent_id="lab1", slot=next(_SLOTS), status=status,
        requested_run_seconds=0.0, requested_at=T0, title=title, choice=CHOICE,
        final=FINAL if finished else None,
        finished_at=T0 + timedelta(hours=4) if finished else None,
    )
    db.add(project)
    await db.flush()
    return project.id


async def _manager(db):
    return await factories.make_user(db, user_role=USER_ROLE_MANAGER)


async def _ask(db, user, question="What is proposed?", **subject):
    prepared = await chat.prepare_turn(db, user=user, question_raw=question, **subject)
    queue: asyncio.Queue = asyncio.Queue()
    await chat.run_turn(prepared, queue)
    return prepared


async def _questions(db, user, **subject) -> list[str]:
    history = await chat.list_history(db, user=user, **subject)
    return [turn["question"] for turn in history["turns"]]


async def test_project_turn_round_trip(db_session, install_llm):
    fake = install_llm(ChatScript(segments=[("A project answer.", [])]))
    pid = await seed_project(db_session)
    user = await _manager(db_session)
    prepared = await _ask(db_session, user, "What is the plan?", project_id=pid)
    assert (prepared.project_id, prepared.assessment_id) == (pid, None)
    assert len(fake.calls) == 1
    history = await chat.list_history(db_session, project_id=pid, user=user)
    assert [(t["question"], t["answer_text"], t["status"]) for t in history["turns"]] == [
        ("What is the plan?", "A project answer.", "complete")
    ]
    assert history["turns"][0]["in_window"] is True
    assert history["verdict_revision"] == 1
    assert history["verdict_may_change"] is False
    assert history["questions_used_24h"] == 1


async def test_conversations_do_not_mix(db_session, install_llm):
    install_llm()
    project_a = await seed_project(db_session, title="A")
    project_b = await seed_project(db_session, title="B")
    seeded = await seed_interview(db_session)
    user = await _manager(db_session)
    await _ask(db_session, user, "ABOUT PROJECT A", project_id=project_a)
    await _ask(db_session, user, "ABOUT THE ASSESSMENT", assessment_id=seeded.assessment_id)
    assert await _questions(db_session, user, project_id=project_a) == ["ABOUT PROJECT A"]
    assert await _questions(db_session, user, project_id=project_b) == []
    assert await _questions(db_session, user, assessment_id=seeded.assessment_id) == [
        "ABOUT THE ASSESSMENT"
    ]


async def test_turn_cap_per_project(db_session, install_llm, monkeypatch):
    fake = install_llm()
    monkeypatch.setattr(get_settings(), "assessment_chat_max_turns", 1)
    project_a = await seed_project(db_session, title="A")
    project_b = await seed_project(db_session, title="B")
    user = await _manager(db_session)
    await _ask(db_session, user, project_id=project_a)
    with pytest.raises(chat.ChatError) as refused:
        await chat.prepare_turn(db_session, project_id=project_a, user=user, question_raw="again")
    assert (refused.value.status, refused.value.code) == (409, "conversation_full")
    await _ask(db_session, user, project_id=project_b)  # another project's cap is its own
    assert len(fake.calls) == 2


async def test_clear_is_per_subject(db_session, install_llm):
    install_llm()
    project_a = await seed_project(db_session, title="A")
    project_b = await seed_project(db_session, title="B")
    seeded = await seed_interview(db_session)
    user = await _manager(db_session)
    await _ask(db_session, user, "QA", project_id=project_a)
    await _ask(db_session, user, "QB", project_id=project_b)
    await _ask(db_session, user, "QX", assessment_id=seeded.assessment_id)
    assert await chat.clear_history(db_session, project_id=project_a, user_id=user.id) == 1
    assert await _questions(db_session, user, project_id=project_a) == []
    assert await _questions(db_session, user, project_id=project_b) == ["QB"]
    assert await _questions(db_session, user, assessment_id=seeded.assessment_id) == ["QX"]
    # And the reverse: clearing the assessment leaves the remaining project's turns.
    assert await chat.clear_history(
        db_session, assessment_id=seeded.assessment_id, user_id=user.id
    ) == 1
    assert await _questions(db_session, user, project_id=project_b) == ["QB"]


async def test_one_answer_in_flight_across_subjects(db_session, install_llm):
    fake = install_llm()
    pid = await seed_project(db_session)
    seeded = await seed_interview(db_session)
    user = await _manager(db_session)
    turn, usage = chat._new_rows(
        assessment_id=seeded.assessment_id, user_id=user.id, tier="staff", question="in flight",
        model="claude-opus-5-5", record_sha="a" * 12, prompt_sha="b" * 12,
        created_at=datetime.now(UTC), verdict_revision=1,
    )
    db_session.add_all([turn, usage])
    await db_session.flush()
    with pytest.raises(chat.ChatError) as refused:
        await chat.prepare_turn(db_session, project_id=pid, user=user, question_raw="q")
    assert (refused.value.status, refused.value.code) == (409, "answer_in_progress")
    assert fake.calls == []


async def test_limits_are_shared(db_session, install_llm):
    fake = install_llm()
    pid = await seed_project(db_session)
    seeded = await seed_interview(db_session)
    user = await _manager(db_session)
    # $20 of assessment questions (8 unpriced rows at the $2.50 reserve) reaches the per-user
    # ceiling, which a project ask then meets.
    db_session.add_all([
        AssessmentChatUsage(
            id=uuid.uuid4(), user_id=user.id, assessment_id=seeded.assessment_id,
            subject="assessment", context_tier="staff", model="claude-opus-5-5",
            status="failed", usage_by_model=None, created_at=datetime.now(UTC) - timedelta(hours=1),
        )
        for _ in range(8)
    ])
    await db_session.flush()
    with pytest.raises(chat.ChatError) as refused:
        await chat.prepare_turn(db_session, project_id=pid, user=user, question_raw="q")
    assert (refused.value.status, refused.value.code) == (429, "daily_spend_limit")
    assert fake.calls == []


async def test_exactly_one_subject_required(db_session, install_llm):
    install_llm()
    pid = await seed_project(db_session)
    user = await _manager(db_session)
    aid = uuid.uuid4()
    for ids in ({}, {"assessment_id": aid, "project_id": pid}):
        with pytest.raises(ValueError):
            await chat.prepare_turn(db_session, user=user, question_raw="q", **ids)
        with pytest.raises(ValueError):
            await chat.list_history(db_session, user=user, **ids)
        with pytest.raises(ValueError):
            await chat.clear_history(db_session, user_id=user.id, **ids)
        with pytest.raises(ValueError):
            await chat.record_open(db_session, user=user, via_raw="bubble", **ids)
        with pytest.raises(ValueError):
            await chat._conversation(db_session, user_id=user.id, tier="staff", **ids)
        with pytest.raises(ValueError):
            chat._new_rows(
                user_id=user.id, tier="staff", question="q", model="m", record_sha="a" * 12,
                prompt_sha="b" * 12, created_at=datetime.now(UTC), verdict_revision=1, **ids,
            )
    with pytest.raises(ValueError):
        chat.ChatSubject("interview", pid)
    with pytest.raises(ValueError):
        chat.ChatSubject("project", None)


async def test_ledger_and_opens_record_subject(db_session, install_llm):
    install_llm()
    pid = await seed_project(db_session)
    seeded = await seed_interview(db_session)
    user = await _manager(db_session)
    project_turn = await _ask(db_session, user, project_id=pid)
    assessment_turn = await _ask(db_session, user, assessment_id=seeded.assessment_id)
    usage = {
        row.id: row
        for row in (
            await db_session.execute(
                select(AssessmentChatUsage)
                .where(AssessmentChatUsage.user_id == user.id)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    }
    project_row = usage[project_turn.usage_id]
    assert (project_row.subject, project_row.project_id, project_row.assessment_id) == (
        "project", pid, None
    )
    assessment_row = usage[assessment_turn.usage_id]
    assert (assessment_row.subject, assessment_row.project_id, assessment_row.assessment_id) == (
        "assessment", None, seeded.assessment_id
    )
    await chat.record_open(db_session, project_id=pid, user=user, via_raw="link")
    await chat.record_open(
        db_session, assessment_id=seeded.assessment_id, user=user, via_raw="bubble"
    )
    opens = (
        await db_session.execute(
            select(AssessmentChatOpen).where(AssessmentChatOpen.user_id == user.id)
        )
    ).scalars().all()
    assert sorted(
        (o.subject, o.project_id, o.assessment_id, o.opened_via) for o in opens
    ) == [
        ("assessment", None, seeded.assessment_id, "bubble"),
        ("project", pid, None, "link"),
    ]


async def test_project_prompt_path_used(db_session, install_llm):
    fake = install_llm()
    pid = await seed_project(db_session)
    seeded = await seed_interview(db_session)
    user = await _manager(db_session)
    await _ask(db_session, user, project_id=pid)
    assert fake.calls[0]["system"] == [{"type": "text", "text": PROJECT_PROMPT}]
    await _ask(db_session, user, assessment_id=seeded.assessment_id)
    assessment_prompt, _ = chat.load_system_prompt()
    assert fake.calls[1]["system"] == [{"type": "text", "text": assessment_prompt}]
    assert assessment_prompt != PROJECT_PROMPT


async def test_missing_project_prompt_is_503(db_session, install_llm, monkeypatch, tmp_path):
    fake = install_llm()
    monkeypatch.setattr(chat, "PROJECT_PROMPT_PATH", tmp_path / "absent.md")
    pid = await seed_project(db_session)
    user = await _manager(db_session)
    with pytest.raises(chat.ChatError) as refused:
        await chat.prepare_turn(db_session, project_id=pid, user=user, question_raw="q")
    assert (refused.value.status, refused.value.code) == (503, "prompt_missing")
    assert fake.calls == []
