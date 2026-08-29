"""Task 10: the review bot job handler (``src/services/review_bot.py``).

DB-backed despite living in ``tests/unit`` — same precedent as
``tests/unit/test_worker_deletion_races.py`` and
``tests/unit/test_directory_assessments.py``: the ``db_session`` fixture is
not gated to ``tests/integration``.

``generate_agent_response`` is patched on the module-local binding
(``review_bot.generate_agent_response``), never through ``FakeAnthropic`` —
the handler never reaches the real client in these tests.
"""

from __future__ import annotations

import ast
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from src.models import AssessmentReview, Job, OpportunityAssessment, PromptChangeSuggestion
from src.services import review_bot
from tests import factories

# No `pytestmark = pytest.mark.asyncio`: `asyncio_mode = "auto"` (pyproject.toml)
# already collects every `async def test_*` here without it, and this module
# also has one plain sync test (`test_the_bot_module_imports_no_transport`)
# that a blanket module-level asyncio mark would warn on.

ROOT = Path(__file__).resolve().parents[2]

_HAPPY_RESPONSE = json.dumps({"target": "scout_hub", "suggestion": "S", "rationale": "R"})


def _install(monkeypatch, response: str = _HAPPY_RESPONSE) -> list[dict]:
    """Patch the module-local `generate_agent_response` binding; return the
    list every call's kwargs get appended to."""
    calls: list[dict] = []

    async def _fake(system_prompt, messages, model=None, max_tokens=None, **kwargs):
        calls.append(
            {
                "system_prompt": system_prompt,
                "messages": messages,
                "model": model,
                "max_tokens": max_tokens,
            }
        )
        return response

    monkeypatch.setattr(review_bot, "generate_agent_response", _fake)
    return calls


async def _seed_assessment(db, **overrides) -> tuple[OpportunityAssessment, object]:
    run = await factories.make_simulation_run(db)
    data = dict(simulation_run_id=run.id, agent_id="blackbird", channel_name="c1")
    data.update(overrides)
    assessment = OpportunityAssessment(**data)
    db.add(assessment)
    await db.flush()
    return assessment, run


def _make_review(assessment, *, feedback_mode="learn", **overrides) -> AssessmentReview:
    data = dict(
        assessment_id=assessment.id,
        reviewer_name="Dr. Reviewer",
        score=3,
        comment="looks off",
        feedback_mode=feedback_mode,
    )
    data.update(overrides)
    return AssessmentReview(**data)


def _make_job(assessment_id) -> Job:
    return Job(type="review_feedback_analysis", payload={"assessment_id": str(assessment_id)})


async def _suggestion_count(db) -> int:
    return await db.scalar(select(func.count()).select_from(PromptChangeSuggestion))


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


async def test_happy_path_stores_a_suggestion_and_consumes_feedback(db_session, monkeypatch):
    calls = _install(monkeypatch)
    assessment, _run = await _seed_assessment(db_session)

    pre_consumed = _make_review(
        assessment,
        reviewer_name="Old Reviewer",
        consumed_at=datetime.now(UTC) - timedelta(days=1),
    )
    unconsumed = _make_review(assessment, reviewer_name="Fresh Reviewer", score=2, comment="c2")
    log_only = _make_review(assessment, feedback_mode="log_only", reviewer_name="Logger")
    db_session.add_all([pre_consumed, unconsumed, log_only])
    await db_session.flush()

    job = _make_job(assessment.id)
    db_session.add(job)
    await db_session.flush()

    await review_bot.execute_review_analysis(job, db_session)

    suggestions = (await db_session.execute(select(PromptChangeSuggestion))).scalars().all()
    assert len(suggestions) == 1
    suggestion = suggestions[0]
    assert suggestion.target == "scout_hub"
    assert "S" in suggestion.suggestion
    assert "R" in suggestion.suggestion
    assert suggestion.model == review_bot.get_settings().llm_review_model

    assert suggestion.prompt_files, "prompt_files must be non-empty"
    for entry in suggestion.prompt_files:
        assert set(entry) == {"path", "sha256_12"}

    assert suggestion.feedback_snapshot == [
        {
            "id": str(unconsumed.id),
            "reviewer_name": unconsumed.reviewer_name,
            "score": unconsumed.score,
            "feedback_mode": unconsumed.feedback_mode,
            "comment": unconsumed.comment,
            "created_at": unconsumed.created_at.isoformat(),
        }
    ]

    await db_session.refresh(unconsumed)
    await db_session.refresh(pre_consumed)
    await db_session.refresh(log_only)
    assert unconsumed.consumed_at is not None
    assert pre_consumed.consumed_at is not None  # untouched, still consumed from before
    assert log_only.consumed_at is None  # log_only is never a candidate

    assert len(calls) == 1
    assert calls[0]["model"] == review_bot.get_settings().llm_review_model
    assert calls[0]["max_tokens"] == 8000


async def test_no_unconsumed_learn_feedback_is_a_silent_noop(db_session, monkeypatch):
    calls = _install(monkeypatch)
    assessment, _run = await _seed_assessment(db_session)
    db_session.add(_make_review(assessment, feedback_mode="log_only"))
    await db_session.flush()
    job = _make_job(assessment.id)
    db_session.add(job)
    await db_session.flush()

    await review_bot.execute_review_analysis(job, db_session)

    assert calls == []
    assert await _suggestion_count(db_session) == 0


async def test_vanished_assessment_is_a_noop_not_a_dead_job(db_session, monkeypatch):
    calls = _install(monkeypatch)
    job = _make_job(uuid.uuid4())
    db_session.add(job)
    await db_session.flush()

    await review_bot.execute_review_analysis(job, db_session)  # must not raise

    assert calls == []
    assert await _suggestion_count(db_session) == 0


# ---------------------------------------------------------------------------
# model-output parsing degradations — always stored, never crashed
# ---------------------------------------------------------------------------


async def test_unparseable_model_output_is_stored_not_dropped(db_session, monkeypatch):
    raw = "I don't think this maps to any specific prompt file, sorry."
    _install(monkeypatch, response=raw)
    assessment, _run = await _seed_assessment(db_session)
    db_session.add(_make_review(assessment))
    await db_session.flush()
    job = _make_job(assessment.id)
    db_session.add(job)
    await db_session.flush()

    await review_bot.execute_review_analysis(job, db_session)

    suggestion = (await db_session.execute(select(PromptChangeSuggestion))).scalar_one()
    assert suggestion.target == "out_of_scope"
    assert suggestion.suggestion == raw
    assert suggestion.raw_response == raw


async def test_non_dict_json_is_stored_not_crashed(db_session, monkeypatch):
    raw = "```json\n[1, 2]\n```"
    _install(monkeypatch, response=raw)
    assessment, _run = await _seed_assessment(db_session)
    db_session.add(_make_review(assessment))
    await db_session.flush()
    job = _make_job(assessment.id)
    db_session.add(job)
    await db_session.flush()

    await review_bot.execute_review_analysis(job, db_session)  # must not AttributeError

    suggestion = (await db_session.execute(select(PromptChangeSuggestion))).scalar_one()
    assert suggestion.target == "out_of_scope"
    assert suggestion.suggestion == raw
    assert suggestion.raw_response == raw


@pytest.mark.parametrize("bad_target", ["bogus", "specialist:astrology"])
async def test_invalid_target_is_coerced_to_out_of_scope_and_raw_kept(
    db_session, monkeypatch, bad_target
):
    raw = json.dumps({"target": bad_target, "suggestion": "S", "rationale": "R"})
    _install(monkeypatch, response=raw)
    assessment, _run = await _seed_assessment(db_session)
    db_session.add(_make_review(assessment))
    await db_session.flush()
    job = _make_job(assessment.id)
    db_session.add(job)
    await db_session.flush()

    await review_bot.execute_review_analysis(job, db_session)

    suggestion = (await db_session.execute(select(PromptChangeSuggestion))).scalar_one()
    assert suggestion.target == "out_of_scope"
    assert suggestion.suggestion == raw
    assert suggestion.raw_response == raw


# ---------------------------------------------------------------------------
# transcript disclosure
# ---------------------------------------------------------------------------


async def test_missing_transcript_is_disclosed(db_session, monkeypatch):
    calls = _install(monkeypatch)
    assessment, _run = await _seed_assessment(db_session)  # slack_ts left None
    db_session.add(_make_review(assessment))
    await db_session.flush()
    job = _make_job(assessment.id)
    db_session.add(job)
    await db_session.flush()

    await review_bot.execute_review_analysis(job, db_session)

    assert len(calls) == 1
    content = calls[0]["messages"][0]["content"]
    assert "TRANSCRIPT: unavailable" in content

    suggestion = (await db_session.execute(select(PromptChangeSuggestion))).scalar_one()
    assert suggestion.transcript_available is False


async def test_oversized_transcript_is_truncated_and_flagged(db_session, monkeypatch):
    calls = _install(monkeypatch)
    assessment, run = await _seed_assessment(
        db_session, channel_name="chan-x", slack_ts="1000.000001"
    )
    await factories.make_agent_message(
        db_session, run=run, channel_name="chan-x", message_ts="1000.000001",
        thread_ts=None, content="x" * 60_000, sender_name="blackbird", phase="thread_reply",
    )
    await factories.make_agent_message(
        db_session, run=run, channel_name="chan-x", message_ts="1000.000002",
        thread_ts="1000.000001", content="y" * 60_000, sender_name="pardoll_lab",
        phase="thread_reply",
    )
    await factories.make_agent_message(
        db_session, run=run, channel_name="chan-x", message_ts="1000.000003",
        thread_ts="1000.000001", content="z" * 60_000, sender_name="blackbird",
        phase="thread_reply",
    )
    db_session.add(_make_review(assessment))
    await db_session.flush()
    job = _make_job(assessment.id)
    db_session.add(job)
    await db_session.flush()

    await review_bot.execute_review_analysis(job, db_session)

    suggestion = (await db_session.execute(select(PromptChangeSuggestion))).scalar_one()
    assert suggestion.transcript_available is True
    assert suggestion.input_truncated is True

    content = calls[0]["messages"][0]["content"]
    assert "ELIDED" in content
    transcript_section = content.split("## INTERVIEW TRANSCRIPT")[1].split(
        "## CURRENT PROMPT FILES"
    )[0]
    # 3 messages x 60_000 chars of raw content would be ~180_000 before elision.
    assert len(transcript_section) < 160_000


# ---------------------------------------------------------------------------
# failure atomicity
# ---------------------------------------------------------------------------


async def test_llm_exception_propagates_for_worker_retry(db_session, monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("Opus is down")

    monkeypatch.setattr(review_bot, "generate_agent_response", _boom)

    assessment, _run = await _seed_assessment(db_session)
    review = _make_review(assessment)
    db_session.add(review)
    await db_session.flush()
    job = _make_job(assessment.id)
    db_session.add(job)
    await db_session.flush()

    with pytest.raises(RuntimeError, match="Opus is down"):
        await review_bot.execute_review_analysis(job, db_session)

    assert await _suggestion_count(db_session) == 0
    assert review.consumed_at is None


# ---------------------------------------------------------------------------
# no transport
# ---------------------------------------------------------------------------


def test_the_bot_module_imports_no_transport():
    src = (ROOT / "src/services/review_bot.py").read_text()
    tree = ast.parse(src)
    mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {
        n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module
    }
    assert not any("slack" in m for m in mods), mods
