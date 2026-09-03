"""Drift alarm between prompts/review-bot.md and the code that builds the bot's
user message (spec §3 "Prompt/code drift"). The prompt is bind-mounted and
editable without a rebuild, so the only thing keeping the two in step is this
file."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from src.models import OpportunityAssessment
from src.services import review_bot

ROOT = Path(__file__).resolve().parents[2]
PROMPT = (ROOT / review_bot._REVIEW_PROMPT_PATH).read_text()


def _sections_the_code_sends() -> list[str]:
    assessment = OpportunityAssessment(
        simulation_run_id=uuid.uuid4(), agent_id="blackbird", channel_name="c",
    )
    msg = review_bot._build_user_message(
        feedback_snapshot=[], assessment=assessment,
        transcript_text="TRANSCRIPT: unavailable", prompt_files_text="",
    )
    return re.findall(r"^## (.+)$", msg, flags=re.MULTILINE)


def _sections_the_prompt_describes() -> list[str]:
    given = PROMPT.split("## What you will be given", 1)[1].split("### Placeholders", 1)[0]
    return re.findall(r"^- \*\*([A-Z][A-Z ]+)\*\*", given, flags=re.MULTILINE)


def test_prompt_describes_exactly_the_sections_the_code_sends():
    assert _sections_the_prompt_describes() == _sections_the_code_sends() == [
        "FEEDBACK", "ASSESSMENT", "INTERVIEW TRANSCRIPT", "CURRENT PROMPT FILES",
    ]


def test_prompt_does_not_promise_a_rubric_section_or_five_sections():
    assert "**RUBRIC**" not in PROMPT
    assert "five sections" not in PROMPT
    assert "prompts/rubric/blackbird-rubric.toml" in PROMPT
    assert "RUBRIC section" not in PROMPT


def _targets_in(text: str) -> set[str]:
    match = re.search(r'"target":\s*"([^"]+)"', text)
    assert match, "no target line found"
    return {t.strip() for t in match.group(1).split("|")}


def test_target_vocabulary_matches_the_validator_in_both_prompts():
    expected = set(review_bot._STATIC_TARGETS) | {"specialist:<domain>"}
    assert _targets_in(PROMPT) == expected
    assert _targets_in(review_bot._DEFAULT_REVIEW_PROMPT) == expected


def test_prompt_describes_the_real_feedback_mode_and_transcript_prefix():
    assert "`learn`" in PROMPT
    assert "agree/disagree" not in PROMPT
    assert "`> `" in PROMPT
