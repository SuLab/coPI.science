from types import SimpleNamespace

from src.services.industry_score import (
    NOT_REFRESHED,
    SCORER_VERSION,
    cohort_raw,
    industry_view,
    normalise,
    score_evidence,
)
from src.services.industry_sources import EvidenceItem


def ev(kind, company, cls="pharma_biotech", role="last", n_authors=5, rel=None, vetoed=False,
       attributed=False, year=2021):
    e = EvidenceItem("x", kind, f"{kind}:{company}", company, None, cls, year, role, True,
                     {"author_count": n_authors, **({"relationship": rel} if rel else {}),
                      **({"attributed": True} if attributed else {})})
    e.vetoed_at = "2026" if vetoed else None
    return e


def test_distinct_companies_count_once_per_kind():
    raw, comp = score_evidence([ev("coauthor_company", "Pfizer"), ev("coauthor_company", "Pfizer")])
    assert comp["coauthor_company"]["distinct_companies"] == 1 and raw == 4.5  # 3 × 1.5 (last author)


def test_vendor_and_vetoed_contribute_zero():
    raw, _ = score_evidence([ev("coauthor_company", "Agilent", cls="cro_vendor"), ev("coauthor_company", "Pfizer", vetoed=True)])
    assert raw == 0


def test_consortium_middle_author_is_downweighted():
    raw, _ = score_evidence([ev("coauthor_company", "Pfizer", role="middle", n_authors=80)])
    assert raw == 0.75  # 3 × 0.25


def test_caps_apply():
    raw, comp = score_evidence([ev("coauthor_company", f"C{i}", role="middle") for i in range(20)])
    assert comp["coauthor_company"]["weighted"] == 30 and raw == 30


def test_normalise_is_percentile_rank():
    assert normalise(5.0, [1.0, 2.0, 5.0, 9.0]) == 75.0 and normalise(1.0, []) == 0.0


def test_scorer_version_is_semver():
    assert SCORER_VERSION.count(".") == 2


def test_patent_filed_is_exempt_from_the_class_gate():
    e = EvidenceItem("uspto", "patent_filed", "APP1", None, None, "unknown", 2021, "inventor", True, {})
    raw, _ = score_evidence([e])
    assert raw == 4.0


def test_founder_equity_double_only_when_attributed():
    raw, _ = score_evidence([ev("coi_relationship", "Startup Inc", rel="founder", attributed=True)])
    assert raw == 10


def test_an_unattributed_coi_row_scores_nothing():
    """D10: a pre-2.0.0 row (no `attributed`) cannot credit the PI, founder or not."""
    raw, comp = score_evidence([ev("coi_relationship", "Startup Inc", rel="founder"),
                                ev("coi_relationship", "Other Inc", rel="employee")])
    assert raw == 0 and comp == {}


def test_an_attributed_employee_scores_once():
    raw, _ = score_evidence([ev("coi_relationship", "Acme Inc", rel="employee", attributed=True)])
    assert raw == 5


def test_rows_before_the_tenure_start_score_nothing():
    rows = [ev("coauthor_company", "Pfizer", year=2012), ev("coauthor_company", "Merck", year=2020)]
    assert score_evidence(rows, tenure_start=2015)[0] == 4.5
    assert score_evidence(rows)[0] == 9.0
    undated = ev("coauthor_company", "Lilly", year=None)
    assert score_evidence([undated], tenure_start=2015)[0] == 0


def test_scorer_version_is_two():
    assert SCORER_VERSION == "2.0.0"


def _row(raw, tenure=2015, coverage=None):
    return SimpleNamespace(raw_sum=raw, tenure_start_used=tenure, coverage=coverage)


def test_read_time_reasons():
    cohort = [1.0, 2.0, 5.0, 9.0]
    assert industry_view(_row(5.0), cohort, has_older_row=False).percentile == 75.0
    assert industry_view(_row(5.0, tenure=None), cohort, has_older_row=False).reason == "no_tenure_start"
    assert industry_view(_row(0.0), cohort, has_older_row=False).reason == "no_evidence"
    assert industry_view(_row(None), cohort, has_older_row=False).reason == "no_evidence"
    assert industry_view(_row(5.0), [5.0, 1.0, 2.0], has_older_row=False).reason == "cohort_too_small"
    assert industry_view(None, cohort, has_older_row=True).reason == "rescoring"
    assert industry_view(None, cohort, has_older_row=False).reason == "not_computed"


def test_partial_is_any_source_not_ok():
    view = industry_view(_row(5.0, coverage={"openalex": "ok", "uspto": "truncated"}),
                         [1.0, 2.0, 5.0, 9.0], has_older_row=False)
    assert view.partial and not industry_view(_row(5.0, coverage={"openalex": "ok"}),
                                              [1.0, 2.0, 5.0, 9.0], has_older_row=False).partial


def test_cohort_raw_admits_positive_tenured_rows_only():
    assert cohort_raw(_row(3.0)) == 3.0
    assert cohort_raw(_row(0.0)) is None and cohort_raw(_row(3.0, tenure=None)) is None


def test_a_not_refreshed_row_never_joins_the_cohort():
    """A veto before the PI's first 2.0.0 job scores 1.0.0-era evidence; it must not shift
    the peers' percentiles (semantic review 2026-10-06)."""
    stale = _row(5.0, coverage={"openalex": NOT_REFRESHED, "uspto": NOT_REFRESHED})
    assert cohort_raw(stale) is None
    assert cohort_raw(_row(5.0, coverage={"openalex": "ok", "uspto": "unavailable:http_429"})) == 5.0
