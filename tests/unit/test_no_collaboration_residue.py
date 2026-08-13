"""Guard against retired mesh-era / private-instructions language reappearing.

Issue #29 audit (PR34 branch-2 review) found two src/ code paths that still
hardcoded the retired private-profile section contract even though the
prompts-only phrase guard (tests/unit/test_doc_prompt_sync.py) can't see them:

- src/services/llm.py: synthesize_private_profile's FileNotFoundError fallback
  string, used when prompts/private-profile-synthesis.md is missing on disk.
- src/routers/onboarding.py: the default template rendered for brand-new
  users with no profile anywhere yet.

The 2026-08-12 removal cycle (Task 5 of the engine-reconciliation plan) then
deleted the private-profile feature itself outright: synthesize_private_profile
and its FileNotFoundError fallback, the onboarding private-profile step (GET/POST
/onboarding/private-profile), src/services/profile_export.py::export_private_profile,
and agent.py's ``## Your Private Instructions`` prompt injection / private_profile
property / update_private_profile / persist_private_profile_to_db. There is no
section-list contract left to pin (test_llm_fallback_section_list_matches_new_contract
used to pin synthesize_private_profile's fallback text; that function no longer
exists) — this file pins the ABSENCE of the removed feature instead.

This test does plain string checks on source text — no imports of llm.py,
onboarding.py, agent.py, or email.py — so it can't be fooled by a docstring-only
fix and doesn't need any app/DB fixtures.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

LLM_PY = ROOT / "src/services/llm.py"
ONBOARDING_PY = ROOT / "src/routers/onboarding.py"
EMAIL_PY = ROOT / "src/services/email.py"
AGENT_PY = ROOT / "src/agent/agent.py"
SIMULATION_PY = ROOT / "src/agent/simulation.py"

# The Output Format headers from the (also-deleted) private-profile-synthesis.md
# contract. Kept only as a defensive phrase guard against these specific retired
# terms reappearing anywhere in llm.py/onboarding.py — not a claim that the
# feature they describe still exists in any form.
RETIRED_PHRASES = ["collaboration preferences", "criteria to always explore"]


@pytest.mark.parametrize("path", [LLM_PY, ONBOARDING_PY], ids=["llm.py", "onboarding.py"])
@pytest.mark.parametrize("phrase", RETIRED_PHRASES)
def test_retired_phrase_absent_from_source(path, phrase):
    text = path.read_text(encoding="utf-8").lower()
    assert phrase not in text, f"retired phrase {phrase!r} still present in {path}"


def test_synthesize_private_profile_is_fully_removed_from_llm_py():
    """The private-profile synthesis pipeline (function + its FileNotFoundError
    fallback section list) is retired outright, not rewritten — pin its
    absence rather than its former fallback contract."""
    text = LLM_PY.read_text(encoding="utf-8")
    assert "synthesize_private_profile" not in text, (
        f"{LLM_PY} still references synthesize_private_profile — the private-profile "
        "synthesis pipeline was supposed to be removed outright"
    )


ONBOARDING_PRIVATE_PROFILE_MARKERS = [
    "/private-profile",
    "export_private_profile",
    "PRIVATE_PROFILES_DIR",
    "private_profile_md",
    "private_profile_seed",
]


@pytest.mark.parametrize("marker", ONBOARDING_PRIVATE_PROFILE_MARKERS)
def test_private_profile_step_is_fully_removed_from_onboarding_py(marker):
    """The onboarding private-profile step (GET/POST /onboarding/private-profile,
    its live/seed/disk/template fallback chain) is retired outright — the
    surviving final step (save_profile) now owns onboarding completion
    (onboarding_complete flip, welcome email, invite/redirect resume)."""
    text = ONBOARDING_PY.read_text(encoding="utf-8")
    assert marker not in text, (
        f"{ONBOARDING_PY} still references {marker!r} — the onboarding "
        "private-profile step was supposed to be removed outright"
    )


AGENT_PY_PRIVATE_INSTRUCTION_MARKERS = [
    "## Your Private Instructions",
    "synthesize_private_profile",
    "update_private_profile",
    "persist_private_profile_to_db",
]


@pytest.mark.parametrize("marker", AGENT_PY_PRIVATE_INSTRUCTION_MARKERS)
def test_private_instruction_markers_absent_from_agent_py(marker):
    text = AGENT_PY.read_text(encoding="utf-8")
    assert marker not in text, (
        f"private-instruction marker {marker!r} still present in {AGENT_PY}"
    )


def test_weigh_in_yourself_absent_from_welcome_email():
    """Decision 7 (removal cycle): the welcome email stays as a one-way
    notification, but must not claim the PI can personally weigh in on the
    bot's interview thread — there is no human-PI-to-bot interaction surface
    left."""
    text = EMAIL_PY.read_text(encoding="utf-8")
    assert "weigh in yourself" not in text, (
        f"{EMAIL_PY} still implies a PI can personally interact in the thread"
    )


# ---------------------------------------------------------------------------
# Final audit wave (2026-08-12): the same mesh-era residue turned up in three
# more places outside the prompts-only phrase guard's blind spot -- the
# welcome email (src/services/email.py), agent.py's emergency system-prompt
# fallback (_default_system_prompt), and simulation.py's memory-synthesis
# prompt (_update_agent_memory). Each was rewritten to the pitch-only model
# (a PI's agent pitches ideas to BlackbirdBot, the scouting hub; there is no
# lab-to-lab collaboration or refinement handshake) in three sibling commits.
# This guard covers all three so the phrases can't reappear silently.
# ---------------------------------------------------------------------------

MESH_ERA_PHRASES = [
    "collaboration opportunities",
    "facilitate scientific collaboration",
    "re-engage to refine",
    "complementarity",
]


def _default_system_prompt_source() -> str:
    """Isolate agent.py's `_default_system_prompt` fallback.

    It is the last top-level function in the module (verified: nothing
    follows it), so marker-to-EOF is exactly its region -- no need to hunt
    for a closing boundary.
    """
    text = AGENT_PY.read_text(encoding="utf-8")
    marker = "def _default_system_prompt"
    assert marker in text, f"{AGENT_PY} no longer defines _default_system_prompt"
    return text[text.index(marker):]


def _update_agent_memory_prompt_source() -> str:
    """Isolate simulation.py's `_update_agent_memory` method body.

    Bounded by the next MODULE-level ``def`` (0 indentation), since this
    method is the last one on its class and module-level helper functions
    resume immediately afterward.
    """
    text = SIMULATION_PY.read_text(encoding="utf-8")
    marker = "async def _update_agent_memory"
    assert marker in text, f"{SIMULATION_PY} no longer defines _update_agent_memory"
    body = text[text.index(marker):]
    next_def = body.find("\ndef ", 1)
    return body if next_def == -1 else body[:next_def]


@pytest.mark.parametrize("phrase", MESH_ERA_PHRASES)
def test_retired_phrase_absent_from_welcome_email(phrase):
    text = EMAIL_PY.read_text(encoding="utf-8").lower()
    assert phrase not in text, f"retired phrase {phrase!r} still present in {EMAIL_PY}"


@pytest.mark.parametrize("phrase", MESH_ERA_PHRASES)
def test_retired_phrase_absent_from_default_system_prompt(phrase):
    text = _default_system_prompt_source().lower()
    assert phrase not in text, (
        f"retired phrase {phrase!r} still present in agent.py's _default_system_prompt"
    )


@pytest.mark.parametrize("phrase", MESH_ERA_PHRASES)
def test_retired_phrase_absent_from_update_agent_memory_prompt(phrase):
    text = _update_agent_memory_prompt_source().lower()
    assert phrase not in text, (
        f"retired phrase {phrase!r} still present in simulation.py's _update_agent_memory"
    )
