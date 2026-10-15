"""profile_pipeline.select_synthesis_inputs (spec 2026-10-09 §4.2)."""
from types import SimpleNamespace

from src.services import profile_pipeline
from src.services.corpus import CorpusResult
from src.services.profile_pipeline import (
    SYNTHESIS_WINDOW,
    PipelineRun,
    predicted_pubmed_years,
    select_synthesis_inputs,
)


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


def _records(n, pmcid_at=()):
    """n in-scope records with abstracts, newest first (position 1 = index 0)."""
    return [{"pmid": str(1000 + i), "year": 2070 - i, "abstract": "A.", "title": f"P{i}",
             "pmcid": f"PMC{i + 1}" if i + 1 in pmcid_at else None} for i in range(n)]


async def _methods_fetched(monkeypatch, pubs):
    fetched = []

    async def no_conversion(pmids):
        return {}

    async def methods(pmcid):
        fetched.append(pmcid)
        return None

    monkeypatch.setattr(profile_pipeline, "convert_pmids_to_pmcids", no_conversion)
    monkeypatch.setattr(profile_pipeline, "fetch_pmc_methods", methods)
    run = PipelineRun(user_id=None, db=SimpleNamespace(), job_id=None,
                      user=SimpleNamespace(name="Jane Wang"), orcid_id="x",
                      pubs_for_synthesis=pubs)
    await profile_pipeline._step5_methods(run)
    return fetched


async def test_methods_come_from_the_context_selection(monkeypatch):
    pubs = _records(50, pmcid_at=(3, 8, 15, 29, 31, 32, 33, 34, 35, 36))
    assert await _methods_fetched(monkeypatch, pubs) == ["PMC3", "PMC8", "PMC15", "PMC29"]


async def test_methods_are_fetched_for_the_newest_selected_papers(monkeypatch):
    pubs = _records(40, pmcid_at=range(1, 41))
    fetched = await _methods_fetched(monkeypatch, list(reversed(pubs)))
    assert fetched == [f"PMC{i}" for i in range(1, 11)]


def test_predicted_years_drop_update_of_earlier_versions():
    stored = [(str(i), 2021, False, "s1") for i in range(1, 11)]
    result = CorpusResult(kept=[], flagged=[], ranked=[
        {"pmid": "99", "year": 2023, "stages": ["s1"], "update_of": ["1"]}])
    assert len(predicted_pubmed_years(stored, result)) == 10


def test_predicted_years_use_refreshed_years():
    result = CorpusResult(kept=[], flagged=[], ranked=[
        {"pmid": "1", "year": 2021, "stages": ["s1"]}])
    assert predicted_pubmed_years([("1", 2019, False, "s1")], result) == [2021]


def test_manual_rows_count_even_when_superseded():
    result = CorpusResult(kept=[], flagged=[], ranked=[
        {"pmid": "2", "year": 2020, "stages": ["s1"], "update_of": ["1"]}])
    assert sorted(predicted_pubmed_years([("1", 2019, False, "manual")], result)) == [2019, 2020]
