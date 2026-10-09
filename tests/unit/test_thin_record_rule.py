"""The thin-record rule (spec 2026-10-09 §4.1, §5.1): ``tenure_scope.papers_in_window``,
``full_career_fallback_applies`` and ``pubmed_thin``."""

from src.services.tenure_scope import (
    THIN_RECORD_MAX_PAPERS,
    full_career_fallback_applies,
    papers_in_window,
    pubmed_thin,
)


def test_the_threshold_is_ten():
    assert THIN_RECORD_MAX_PAPERS == 10


def test_papers_in_window_counts_from_the_tenure_year_inclusive_and_skips_undated():
    assert papers_in_window([2019, 2020, 2021, None], 2020) == 2


def test_papers_in_window_counts_everything_without_a_tenure_year():
    assert papers_in_window([2019, None, 2021], None) == 3


def test_ten_jhu_era_papers_are_thin_and_eleven_are_not():
    assert full_career_fallback_applies([2020] * 10 + [2010] * 30, 2020) is True
    assert full_career_fallback_applies([2020] * 11, 2020) is False


def test_no_tenure_year_never_falls_back():
    assert full_career_fallback_applies([2020], None) is False


def test_undated_and_pre_tenure_papers_do_not_count_toward_the_jhu_era_record():
    # Eleven papers, one of them dated in the window: thin.
    assert full_career_fallback_applies([None] * 5 + [2001] * 5 + [2024], 2020) is True


def test_a_tenure_year_with_no_papers_at_all_is_thin():
    assert full_career_fallback_applies([], 2026) is True


def test_pubmed_thin_counts_the_window_or_every_paper_without_a_tenure_year():
    assert pubmed_thin([2020] * 10, 2020) is True
    assert pubmed_thin([2020] * 11, 2020) is False
    assert pubmed_thin([2001] * 11, 2020) is True
    assert pubmed_thin([2001] * 11, None) is False
    assert pubmed_thin([], None) is True
