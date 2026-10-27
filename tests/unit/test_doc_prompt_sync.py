"""Docs-vs-prompts sync + retired-model phrase guard.

The three prompt-set docs reproduce every prompt file verbatim inside
*Source:*-labeled four-backtick blocks; the interview docs' §4 sections reproduce the
thread_guidance strings, and the projects doc's ````text blocks the projects' code
strings. This test is the reviewed, permanent form of the
branch-1 verify script (see docs/plans/2026-08-12-pr34-pitch-only-
reconciliation-design.md §12.5).
"""
import re
from pathlib import Path

import pytest

from src.agent.project_guidance import project_turn
from src.agent.thread_guidance import phase4_guidance
from src.services.project_research import FINAL_ANSWER_TEXT, RESEARCH_ANGLES, RESEARCH_DOMAINS

ROOT = Path(__file__).resolve().parents[2]
DOCS = [
    ROOT / "docs/specs/2026-08-07-pi-bot-prompts.md",
    ROOT / "docs/specs/2026-08-07-hub-bot-prompts.md",
    ROOT / "docs/specs/2026-10-27-projects-prompts.md",
]
PROJECTS_DOC = DOCS[2]
_BLOCK_RE = re.compile(
    r"\*Source: `([^`]+)`[^*]*\*.*?\n````(?:markdown|text)?\n(.*?)\n````",
    re.DOTALL,
)


def _doc_blocks():
    for doc in DOCS:
        for m in _BLOCK_RE.finditer(doc.read_text()):
            yield doc.name, m.group(1), m.group(2)


@pytest.mark.parametrize(
    "doc_name,src,block",
    [pytest.param(d, s, b, id=f"{d}::{s}") for d, s, b in _doc_blocks()],
)
def test_doc_block_matches_disk(doc_name, src, block):
    if not src.endswith(".md"):
        pytest.skip("§4 python-sourced blocks are checked separately")
    assert (ROOT / src).read_text().rstrip("\n") == block.rstrip("\n"), (
        f"{doc_name} embeds {src} but the block has drifted from disk"
    )


def _NORM(s: str) -> str:
    return " ".join(s.split())


_COUNTS = {"EXPLORE": 2, "DECIDE": 5, "MUST CONCLUDE": 12}


@pytest.mark.parametrize("role,doc", [("pi_lab", DOCS[0]), ("scout_hub", DOCS[1])])
def test_doc_section4_matches_thread_guidance(role, doc):
    sec = doc.read_text().split("## 4. Interview phase guidance", 1)[1]
    sec = sec.split("\n## 5.", 1)[0]
    blocks = re.findall(r"````text\n(.*?)\n````", sec, re.DOTALL)
    assert len(blocks) == 6
    i = 0
    for phase, count in _COUNTS.items():
        _, guidance, instructions = phase4_guidance(role, count)
        for name, actual in (("guidance", guidance), ("instructions", instructions)):
            assert _NORM(actual) == _NORM(blocks[i]), f"{role} {phase}/{name} drifted"
            i += 1


def _project_code_blocks() -> list[str]:
    """The projects doc's ````text blocks in document order (mirrors
    scripts/sync_prompt_set_docs.py's project_code_blocks)."""
    blocks = [RESEARCH_ANGLES[domain] for domain in RESEARCH_DOMAINS] + [FINAL_ANSWER_TEXT]
    for ordinal in (2, 6, 12):
        turn = project_turn("scout_hub", ordinal, lab_agrees=False)
        blocks += [turn.guidance, turn.instructions]
    base = project_turn("scout_hub", 2, lab_agrees=False).guidance + "\n\n"
    agreeing = project_turn("scout_hub", 2, lab_agrees=True).guidance
    assert agreeing.startswith(base)
    blocks.append(agreeing[len(base):])
    for ordinal in (2, 6, 10):
        turn = project_turn("pi_lab", ordinal, lab_agrees=False)
        blocks += [turn.guidance, turn.instructions]
    return blocks


def test_projects_doc_code_blocks_match_the_code():
    blocks = re.findall(r"````text\n(.*?)\n````", PROJECTS_DOC.read_text(), re.DOTALL)
    assert blocks == _project_code_blocks()


def test_projects_doc_embeds_every_project_prompt():
    """Every file in prompts/projects/ is reproduced, so the doc is the whole set."""
    embedded = {src for doc, src, _ in _doc_blocks() if doc == PROJECTS_DOC.name}
    on_disk = {f"prompts/projects/{p.name}" for p in (ROOT / "prompts/projects").glob("*.md")}
    assert on_disk and on_disk <= embedded, sorted(on_disk - embedded)


# Retired-model phrases that must never reappear in any live prompt file.
# Chosen to not collide with legitimate prohibitions ("Never post a :memo:").
_FORBIDDEN = [
    "do not scout",
    "only way an interview",
    "never open a thread at a lab",
    "Baltimore",
    "genuine complementarity",
    "build toward a :memo:",
    "collaboration preferences",
    "wet-lab partners",
    # 2026-08-12 removal cycle (private instructions + reply-only hub + PI
    # interaction + phase-2 prompts) — guards that cycle's deletions against
    # silently reappearing. Decision 10 keeps PI-*intent* attribution
    # language ("that's a question for my PI", "cannot commit your PI"),
    # which none of these phrases collide with.
    "your pi flagged",
    "private instructions",
    "dm rules",
    "phase 2",
]


@pytest.mark.parametrize("phrase", _FORBIDDEN)
def test_no_retired_model_phrases_in_prompts(phrase):
    hits = [
        str(p.relative_to(ROOT))
        for p in (ROOT / "prompts").rglob("*.md")
        if phrase.lower() in p.read_text().lower()
    ]
    assert hits == [], f"retired phrase {phrase!r} found in {hits}"
