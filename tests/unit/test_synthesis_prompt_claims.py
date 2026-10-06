"""prompts/profile-synthesis.md must not ask for what its context never carries.

The prompt told the model to weight "Last-author publications" more heavily,
but ``_build_synthesis_context`` writes only title, journal, year and abstract
per paper — no author position — so the model could only guess the PI's role
from nothing. This pins the rule: the prompt may mention last-author weighting
only if the context actually marks last-author papers.
"""

from pathlib import Path

from src.services.grant_sections import EMPTY_GRANT_SECTIONS
from src.services.profile_pipeline import _build_synthesis_context

_PROMPT = Path(__file__).resolve().parents[2] / "prompts" / "profile-synthesis.md"


def _context() -> str:
    return _build_synthesis_context(
        {"name": "Rachel Green", "institution": "Johns Hopkins University"},
        EMPTY_GRANT_SECTIONS,
        [
            {"pmid": "1", "title": "A paper", "journal": "J", "year": 2020,
             "abstract": "Findings."},
            {"pmid": "2", "title": "Another paper", "journal": "J", "year": 2021,
             "abstract": "More findings."},
        ],
        {},
    )


def test_prompt_only_requests_author_weighting_the_context_supplies():
    # Lower-cased: the pre-fix prompt spelled it "Last-author", which a
    # case-sensitive check never sees.
    prompt = _PROMPT.read_text()
    if "last-author" in prompt.lower():
        assert "[last author]" in _context(), (
            "the synthesis prompt asks for last-author weighting, but the "
            "context it is given never marks a last-author paper"
        )
