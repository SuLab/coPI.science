"""The landscaping prompt texts: retired phrases, the precedence frame, placeholders and the
set stamp (spec §8.1, §8.2)."""
import re
from pathlib import Path

import pytest

from src.landscaping.prompt_set import JUDGING_FILES, load_prompt_set
from tests.unit.test_doc_prompt_sync import _FORBIDDEN

PROMPT_DIR = Path(__file__).resolve().parents[3] / "prompts" / "landscaping"
PROMPT_FILES = (*JUDGING_FILES, "chat.md")

# Spec §8.2, verbatim: the frame every stage that reads BlackbirdBot extracts opens with.
FRAME = (
    "There is no interview, no PI and no lab agent here, and nothing is posted anywhere. Where "
    "the BlackbirdBot text refers to them, read it as the published record in the dossier. From "
    "Core Principle 6, keep its rule that `unconfirmed` is an honest, non-blocking record and that "
    "freedom-to-operate is never resolved by an empty search; its advice about asking a lab agent "
    "does not apply. From Core Principle 8, keep its definition of \"PI credibility\" as the record "
    "of executing comparable work; its rules about the agent's conduct do not apply. Emit only the "
    "requested JSON."
)
KNOWN_PLACEHOLDERS = {
    "criteria_scope", "criteria_vocab", "deal_breakers", "target_rubric", "data_notice",
}
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _text(name: str) -> str:
    return (PROMPT_DIR / name).read_text()


def _flat(text: str) -> str:
    return " ".join(text.split())


@pytest.mark.parametrize("name", PROMPT_FILES)
@pytest.mark.parametrize("phrase", _FORBIDDEN)
def test_no_forbidden_phrases(name, phrase):
    assert phrase.lower() not in _text(name).lower()


@pytest.mark.parametrize("name", ["triage.md", "deep.md", "final.md"])
def test_frame_present(name):
    assert _flat(_text(name)).startswith(_flat(FRAME))


@pytest.mark.parametrize("name", PROMPT_FILES)
def test_placeholders_known(name):
    assert set(_PLACEHOLDER_RE.findall(_text(name))) <= KNOWN_PLACEHOLDERS


def test_checklist_and_notice_where_needed():
    for name in ("extract.md", "triage.md"):
        assert "{target_rubric}" in _text(name), name
        assert "{criteria_vocab}" in _text(name), name
    for name in ("triage.md", "deep.md", "final.md"):
        assert "{deal_breakers}" in _text(name), name
    for name in JUDGING_FILES:
        assert "{criteria_scope}" in _text(name), name
        assert "{data_notice}" in _text(name), name


def test_shipped_set_loads():
    ps = load_prompt_set()
    assert set(PROMPT_FILES) <= set(ps.files)
