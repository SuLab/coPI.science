"""Pure-function tests for scripts/eval_review_bot.py's grader. The script is
loaded by path (the scripts/ directory is not a package), the same idiom as
tests/unit/test_migration_checks.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

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
