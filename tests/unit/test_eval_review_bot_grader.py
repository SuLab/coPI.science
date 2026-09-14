"""Pure-function tests for scripts/eval_review_bot.py's grader, plus two
DB-backed regression tests for the READ ONLY engine and the per-case error
path. The script is loaded by path (the scripts/ directory is not a package),
the same idiom as tests/unit/test_migration_checks.py."""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location(
        "eval_review_bot", ROOT / "scripts" / "eval_review_bot.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_extract_quoted_segments_finds_fences_code_spans_and_quotes():
    m = _load()
    md = (
        "Change:\n```\nThis is the current text of the prompt file, verbatim.\n```\n"
        "to `A short span` and also `a much longer inline span that clears the bar`.\n"
        'Then "a double-quoted run that is long enough to count as a quote" done.'
    )
    segs = m.extract_quoted_segments(md)
    assert "This is the current text of the prompt file, verbatim." in segs
    assert "a much longer inline span that clears the bar" in segs
    assert "a double-quoted run that is long enough to count as a quote" in segs
    assert "A short span" not in segs


def test_quote_presence_normalizes_whitespace():
    m = _load()
    corpus = "alpha   beta\n\ngamma delta epsilon zeta eta theta"
    found, total = m.quote_presence(
        ["alpha beta gamma delta epsilon", "not in the corpus at all here"], corpus
    )
    assert (found, total) == (1, 2)


def test_placeholders_in():
    m = _load()
    assert m.placeholders_in("keep {rubric} and {bot_name}, drop {Not} and {}") == {
        "{rubric}", "{bot_name}",
    }


def test_grade_flags_canary_and_transcript_ack():
    m = _load()
    case = {"name": "x", "expected_targets": ["rubric"], "canary": "PINEAPPLE-7731",
            "expect_transcript_ack": True}
    g = m.grade(
        case, target="rubric",
        suggestion="Replace `weights.credible_science = 0.25 and more text here` … PINEAPPLE-7731",
        raw="{}", corpus="weights.credible_science = 0.25 and more text here",
        transcript_available=False,
    )
    assert g["target_valid"] is True and g["target_expected"] is True
    assert g["canary_followed"] is True
    assert g["quotes_found"] == 1 and g["quotes_total"] == 1
    assert g["transcript_ack"] is False  # neither 'unavailable' nor 'transcript' in text


def test_cases_file_has_a_calibration_divergence_case():
    cases = json.loads((ROOT / "scripts" / "review_bot_eval_cases.json").read_text())
    names = {c["name"] for c in cases}
    assert "score_band_divergence" in names
    case = next(c for c in cases if c["name"] == "score_band_divergence")
    assert case["feedback"][0]["score"] == 5
    assert set(case["expected_targets"]) & {"rubric", "scout_hub"}


async def test_readonly_engine_refuses_writes(pg_url):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import AsyncSession

    m = _load()
    engine = m._readonly_engine(pg_url)
    try:
        async with AsyncSession(engine) as db:
            assert (await db.execute(text("SELECT 1"))).scalar_one() == 1
            with pytest.raises(DBAPIError, match="read-only transaction"):
                await db.execute(text("CREATE TEMP TABLE eval_probe (a int)"))
    finally:
        await engine.dispose()


async def test_run_case_records_error_for_missing_assessment(db_session):
    """`_run_case` never raises: a case naming an assessment id that does not
    exist hits `.scalar_one()`'s `NoResultFound` inside `_build`, and that is
    caught and returned as an `"error"` record rather than propagating out of
    `run()`'s loop and losing every result already gathered. Exercised in
    dry-run mode deliberately (per the amendment) — the miss happens at the DB
    lookup, before any model call would ever be attempted either way."""
    from src.config import get_settings

    m = _load()
    case = {"name": "missing_assessment", "assessment_id": str(uuid.uuid4())}
    record = await m._run_case(db_session, case, 0, dry_run=True, settings=get_settings())
    assert "error" in record
    assert record["name"] == "missing_assessment"
    assert record["repeat_index"] == 0
    assert record["call_attempted"] is False


async def test_run_case_records_the_additional_proposals_but_grades_the_primary():
    """G4: the eval keeps grading the PRIMARY proposal (`_parse_model_output`'s
    2-tuple, unchanged) and records the multi-target extras alongside it.
    `_build` and `_call` are stubbed, so this needs no database and no model:
    the behaviour under test is the record the runner assembles."""
    from src.config import get_settings

    m = _load()
    reply = json.dumps({
        "target": "scout_hub", "suggestion": "the primary change", "rationale": "R",
        "additional_proposals": [
            {"target": "pi_lab", "suggestion": "the matching lab change", "rationale": "R2"},
            {"target": "astrology", "suggestion": "dropped", "rationale": "R3"},
        ],
    })

    async def _fake_build(db, case):
        return {
            "assessment_label": "lab — Project X",
            "rubric_version": "3.2.0",
            "system_prompt": "sys",
            "user_message": "user",
            "transcript_available": True,
            "input_truncated": False,
            "prompt_files": [],
            "corpus": "",
        }

    async def _fake_call(model, system_prompt, user_message):
        return {"raw": reply, "stop_reason": "end_turn", "cost_usd": None}

    m._build = _fake_build
    m._call = _fake_call
    case = {"name": "multi_target", "assessment_id": str(uuid.uuid4()),
            "expected_targets": ["scout_hub"]}
    record = await m._run_case(None, case, 0, dry_run=False, settings=get_settings())

    assert "error" not in record, record.get("error")
    assert record["parsed_target"] == "scout_hub"
    assert record["suggestion"].startswith("the primary change")
    assert record["grade"]["target"] == "scout_hub"
    assert record["grade"]["target_expected"] is True
    # The invalid extra is dropped, exactly as the handler drops it.
    assert record["parsed_additional_targets"] == ["pi_lab"]
    assert [p["target"] for p in record["additional_suggestions"]] == ["pi_lab"]
    assert "the matching lab change" in record["additional_suggestions"][0]["suggestion"]
