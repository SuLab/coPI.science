"""Docs-vs-prompts sync + retired-model phrase guard.

The two prompt-set docs reproduce every prompt file verbatim inside
*Source:*-labeled four-backtick blocks, and their §4 sections reproduce the
thread_guidance strings. This test is the reviewed, permanent form of the
branch-1 verify script (see docs/plans/2026-08-12-pr34-pitch-only-
reconciliation-design.md §12.5).
"""
import re
from pathlib import Path

import pytest

from src.agent.thread_guidance import phase4_guidance

ROOT = Path(__file__).resolve().parents[2]
DOCS = [
    ROOT / "docs/specs/2026-08-07-pi-bot-prompts.md",
    ROOT / "docs/specs/2026-08-07-hub-bot-prompts.md",
]
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
]


@pytest.mark.parametrize("phrase", _FORBIDDEN)
def test_no_retired_model_phrases_in_prompts(phrase):
    hits = [
        str(p.relative_to(ROOT))
        for p in (ROOT / "prompts").rglob("*.md")
        if phrase.lower() in p.read_text().lower()
    ]
    assert hits == [], f"retired phrase {phrase!r} found in {hits}"
