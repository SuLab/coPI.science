from src.services.industry_score import SCORER_VERSION, normalise, score_evidence
from src.services.industry_sources import EvidenceItem


def ev(kind, company, cls="pharma_biotech", role="last", n_authors=5, rel=None, vetoed=False):
    e = EvidenceItem("x", kind, f"{kind}:{company}", company, None, cls, 2021, role, True, {"author_count": n_authors, **({"relationship": rel} if rel else {})})
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


def test_founder_equity_double():
    raw, _ = score_evidence([ev("coi_relationship", "Startup Inc", rel="founder")])
    assert raw == 10


def test_normalise_is_percentile_rank():
    assert normalise(5.0, [1.0, 2.0, 5.0, 9.0]) == 75.0 and normalise(1.0, []) == 0.0


def test_scorer_version_is_semver():
    assert SCORER_VERSION.count(".") == 2
