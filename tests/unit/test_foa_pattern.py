"""Shared FOA number pattern — table-driven over each FOA family's spelling divergence plus lowercase."""

import pytest

from src.agent.foa_pattern import FOA_NUMBER_RE, extract_foa_number


@pytest.mark.parametrize("number", [
    "PAR-24-293", "par-24-293",
    "PA-24-293", "pa-24-293",
    "PAS-24-293", "pas-24-293",
    "NOT-OD-24-001", "not-od-24-001",
    "DE-FOA-0003456", "de-foa-0003456",
    "RFA-AI-27-019", "rfa-ai-27-019",
    "PAR-2024-293", "RFA-AI-2027-019",  # 4-digit years
])
def test_all_families_case_insensitive_and_both_year_lengths(number):
    assert FOA_NUMBER_RE.search(number), f"{number} did not match FOA_NUMBER_RE"
    # The pattern matches either case but the *return* is canonical — see
    # test_extract_canonicalises_the_number.
    assert extract_foa_number(f"See {number} for details.") == number.upper()


@pytest.mark.parametrize("text", ["NSF 25-543", "25-543", "PD 24-7275"])
def test_nsf_style_numbers_are_deliberately_out_of_scope(text):
    assert extract_foa_number(text) is None


def test_extract_returns_none_for_no_match():
    assert extract_foa_number("no FOA number here") is None


# ---------------------------------------------------------------------------
# The return value is a cache *file name*, so it must canonicalise
# ---------------------------------------------------------------------------

# Every row is a spelling that appears in real Slack/LLM text; the expected
# value is the form NIH publishes and the form Grants.gov returns in its
# `number` field — which is what `foa_cache.cache_foa` uses as the file name.
# Upper-case is the canonical spelling of the cache key.
CANONICALISATION_CASES = [
    ("all lower, PA family", "See par-24-293 for details.", "PAR-24-293"),
    ("all lower, agency-coded", "See rfa-ai-27-019 for details.", "RFA-AI-27-019"),
    ("all lower, notice", "See not-od-24-001 for details.", "NOT-OD-24-001"),
    ("all lower, DOE", "See de-foa-0003456 for details.", "DE-FOA-0003456"),
    ("mixed case", "See Rfa-Ai-27-019 for details.", "RFA-AI-27-019"),
    ("already canonical", "See RFA-AI-27-019 for details.", "RFA-AI-27-019"),
    # The real shape this defect takes in production: NIH's own permalink
    # lower-cases the number, so a post whose first mention is the link
    # yields a lower-case extraction and a cache key that never resolves.
    (
        "NIH permalink first",
        ":moneybag: *Funding Opportunity*\n"
        "<https://grants.nih.gov/search-results-detail/rfa-hg-27-011|View full FOA>\n"
        "RFA-HG-27-011 | Closes: 10/30/2026",
        "RFA-HG-27-011",
    ),
]


@pytest.mark.parametrize(
    "content,expected",
    [(c, e) for _, c, e in CANONICALISATION_CASES],
    ids=[i for i, _, _ in CANONICALISATION_CASES],
)
def test_extract_canonicalises_the_number(content, expected):
    """The pattern is IGNORECASE, but `extract_foa_number`'s result is used verbatim as a
    cache file name (`foa_cache.cache_foa` → `data/foa_cache/<number>.json`), and the writers key
    it on Grants.gov's canonical upper-case `number`. Returning the post's own casing therefore
    turns a case difference into a permanent cache miss."""
    assert extract_foa_number(content) == expected


def test_canonical_form_is_a_stable_cache_key():
    """Every spelling of one FOA must produce the same cache key, or one opportunity occupies
    several cache files and each agent's prompt sees a different one."""
    keys = {
        extract_foa_number(f"{spelling} is open.")
        for spelling in ("PAR-24-293", "par-24-293", "Par-24-293", "pAr-24-293")
    }
    assert keys == {"PAR-24-293"}
