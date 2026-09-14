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


def test_feedback_bullet_defines_score_as_proposal_merit():
    given = PROMPT.split("## What you will be given", 1)[1]
    feedback = given.split("- **FEEDBACK**", 1)[1].split("- **ASSESSMENT**", 1)[0]
    low = feedback.lower()
    assert "proposal" in low and "merit" in low
    # It must NOT tell the model the score grades the assessment/verdict.
    assert "grade of the assessment" not in low
    assert "quality of the verdict" not in low


# ---------------------------------------------------------------------------
# G1/G2: the two prompt sets, and the ordering the vocabulary check depends on
# ---------------------------------------------------------------------------


def test_the_vocabulary_line_is_the_first_target_occurrence_in_both_prompts():
    """`_targets_in` uses `re.search` — the FIRST `"target":` in the text — so
    the pipe-joined vocabulary line must stay ahead of every worked example
    and of the `additional_proposals` element. Pinned rather than remembered:
    a concrete example moved above it silently narrows what
    `test_target_vocabulary_matches_the_validator_in_both_prompts` checks to
    that one example's target."""
    pattern = re.compile(r'"target":\s*"([^"]+)"')
    for text in (PROMPT, review_bot._DEFAULT_REVIEW_PROMPT):
        occurrences = pattern.findall(text)
        assert occurrences, "no target line found"
        assert "|" in occurrences[0], occurrences[0]
        assert len(occurrences) >= 2, "the additional_proposals element is missing"


def test_both_prompts_describe_the_additional_proposals_contract():
    for text in (PROMPT, review_bot._DEFAULT_REVIEW_PROMPT):
        assert "additional_proposals" in text
        low = text.lower()
        assert "at most two" in low
        # `re.search`, not `"first" in low`: the bare word occurs in unrelated
        # prose in both texts, so the substring form asserted nothing about the
        # first-key rule it names. The rule itself is separately pinned by
        # test_the_vocabulary_line_is_the_first_target_occurrence_in_both_prompts;
        # this asserts the prompt TELLS the model about it.
        # Emphasis stripped before matching: review-bot.md writes "the
        # object's **first** key" and _DEFAULT_REVIEW_PROMPT writes "the
        # object's FIRST key", so a regex over the raw text would pass on one
        # and fail on the other for a reason that has nothing to do with the
        # rule being stated.
        plain = re.sub(r"[*`_]", "", low)
        assert re.search(r"first\s+key", plain), (
            "the prompt must state that `target` stays the object's FIRST key"
        )


def test_prompt_names_both_prompt_sets_and_the_absence_of_overrides_rule():
    """Finding PS1/PS3: the corpus now carries two prompt sets under partly
    identical filenames, so the prompt must name both and say which wins."""
    files = PROMPT.split("- **CURRENT PROMPT FILES**", 1)[1].split("### Placeholders", 1)[0]
    assert "pi_lab" in files and "scout_hub" in files
    assert "prompts/agent-system.md" in files
    assert "prompts/roles/scout_hub/" in files
    assert "prompts/roles/pi_lab/role.toml" in files
    assert "prompts/roles/scout_hub/role.toml" in files
    low = files.lower()
    assert "override" in low  # the hub's copy wins for the hub
    assert "inherit" in low   # ... and absent an override it inherits the base file
    assert "post_types = []" in files  # reply-only is configuration, not prose
    assert "src/agent/thread_guidance.py" in files  # the one thing it cannot be shown
    # The same two sets and the override rule are mirrored in the fallback.
    default = review_bot._DEFAULT_REVIEW_PROMPT
    assert "prompts/roles/scout_hub/" in default and "prompts/roles/pi_lab/role.toml" in default
    assert "OVERRIDES" in default or "overrides" in default
    assert "src/agent/thread_guidance.py" in default
