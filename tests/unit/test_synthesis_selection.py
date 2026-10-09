"""profile_pipeline.select_synthesis_inputs (spec 2026-10-09 §4.2)."""
from src.services.profile_pipeline import SYNTHESIS_WINDOW, select_synthesis_inputs


def _r(year, abstract="A."):
    return {"year": year, "abstract": abstract, "title": f"P{year}"}


def test_a_thin_record_selects_the_whole_career():
    sel = select_synthesis_inputs([_r(2024), _r(2010), _r(None)], 2020)
    assert sel.full_career_fallback is True and sel.jhu_era_papers == 1
    assert [r["year"] for r in sel.in_scope] == [2024, 2010, None]


def test_eleven_jhu_era_records_keep_the_tenure_window():
    sel = select_synthesis_inputs([_r(2021)] * 11 + [_r(2010)], 2020)
    assert sel.full_career_fallback is False and len(sel.in_scope) == 11


def test_without_a_tenure_year_everything_is_in_scope_and_there_is_no_fallback():
    sel = select_synthesis_inputs([_r(2010), _r(None)], None)
    assert sel.full_career_fallback is False and len(sel.in_scope) == 2


def test_offered_is_the_newest_window_that_carries_an_abstract():
    records = [_r(2024, abstract="")] + [_r(2023)] * (SYNTHESIS_WINDOW + 5)
    sel = select_synthesis_inputs(records, None)
    assert len(sel.offered) == SYNTHESIS_WINDOW - 1


def test_a_missing_venue_or_year_prints_nothing_rather_than_none():
    from src.services.grant_sections import EMPTY_GRANT_SECTIONS
    from src.services.profile_pipeline import _build_synthesis_context

    context = _build_synthesis_context(
        orcid_profile={"name": "Haimin Hu"}, grants=EMPTY_GRANT_SECTIONS,
        publications=[{"title": "SHARP", "journal": None, "year": None, "abstract": "A."}],
        methods_by_pmid={},
    )
    assert "### SHARP (, )" in context and "None" not in context
