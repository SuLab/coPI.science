"""Guard against the retired "Collaboration Preferences" contract reappearing.

Issue #29 audit (PR34 branch-2 review) found two src/ code paths that still
hardcode the retired private-profile section contract even though the
prompts-only phrase guard (tests/unit/test_doc_prompt_sync.py) can't see them:

- src/services/llm.py: synthesize_private_profile's FileNotFoundError fallback
  string, used when prompts/private-profile-synthesis.md is missing on disk.
- src/routers/onboarding.py: the default template rendered for brand-new
  users with no profile anywhere yet.

The authoritative replacement contract comes from the rewritten
prompts/private-profile-synthesis.md Output Format section (landed in a
sibling task on this deployment's pitch-only model: a PI pitches ideas to a
scouting hub; there are no "collaborations"). That rewrite's headers are:

    ### Pitch Preferences
    ### Communication Style
    ### Topic Priorities

("Criteria to Always Explore" is retired outright, not renamed — the new
"Pitch Preferences" guidance already folds in "how much evidence to demand
before pitching".)

This test does plain string checks on source text — no imports of llm.py or
onboarding.py — so it can't be fooled by a docstring-only fix and doesn't
need any app/DB fixtures.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

LLM_PY = ROOT / "src/services/llm.py"
ONBOARDING_PY = ROOT / "src/routers/onboarding.py"

# The Output Format headers from the rewritten private-profile-synthesis.md
# contract. Hardcoded rather than read off prompts/private-profile-synthesis.md
# on disk: THIS worktree forked before that rewrite landed, so its local copy
# of the prompt still says "Collaboration Preferences" (see
# test_doc_prompt_sync.py's docstring / the taskgap report for the fork note).
# The src/ fallback and template must mirror the NEW contract regardless.
NEW_SECTION_HEADERS = ["Pitch Preferences", "Communication Style", "Topic Priorities"]

RETIRED_PHRASES = ["collaboration preferences", "criteria to always explore"]


@pytest.mark.parametrize("path", [LLM_PY, ONBOARDING_PY], ids=["llm.py", "onboarding.py"])
@pytest.mark.parametrize("phrase", RETIRED_PHRASES)
def test_retired_phrase_absent_from_source(path, phrase):
    text = path.read_text(encoding="utf-8").lower()
    assert phrase not in text, f"retired phrase {phrase!r} still present in {path}"


def test_llm_fallback_section_list_matches_new_contract():
    """The FileNotFoundError fallback's section list must mirror the
    rewritten prompt's Output Format headers exactly (same order, same
    names) — not the retired Collaboration-Preferences-era list."""
    text = LLM_PY.read_text(encoding="utf-8")

    # Isolate synthesize_private_profile's fallback assignment so a match
    # elsewhere in the file (e.g. synthesize_profile's own fallback) can't
    # produce a false pass.
    marker = "async def synthesize_private_profile"
    assert marker in text, f"{LLM_PY} no longer defines synthesize_private_profile"
    body = text[text.index(marker):]
    fallback_start = body.index("system_prompt = (")
    fallback_end = body.index(")", fallback_start)
    fallback_src = body[fallback_start:fallback_end]

    positions = [fallback_src.find(h) for h in NEW_SECTION_HEADERS]
    assert all(p != -1 for p in positions), (
        f"fallback string missing one of {NEW_SECTION_HEADERS}: {fallback_src!r}"
    )
    assert positions == sorted(positions), (
        f"fallback string headers out of order relative to {NEW_SECTION_HEADERS}: "
        f"{fallback_src!r}"
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

EMAIL_PY = ROOT / "src/services/email.py"
AGENT_PY = ROOT / "src/agent/agent.py"
SIMULATION_PY = ROOT / "src/agent/simulation.py"

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
