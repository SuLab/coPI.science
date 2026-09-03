"""Shared FOA number pattern — table-driven over issue #23 COR-27's divergence table plus lowercase."""

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
    assert extract_foa_number(f"See {number} for details.") == number


@pytest.mark.parametrize("text", ["NSF 25-543", "25-543", "PD 24-7275"])
def test_nsf_style_numbers_are_deliberately_out_of_scope(text):
    assert extract_foa_number(text) is None


def test_extract_returns_none_for_no_match():
    assert extract_foa_number("no FOA number here") is None
