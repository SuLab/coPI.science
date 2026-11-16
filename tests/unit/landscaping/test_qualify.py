"""S1, lab keys and the pilot slice pick (plan Task 3.5; spec §5.2, §5.3, §7.2, D68).

Every name is invented.
"""
from dataclasses import replace
from datetime import date, timedelta

from src.config import get_settings
from src.landscaping.intake import canonical_id, pilot_slices, slice_fetcher, summary_of
from src.landscaping.labs import lab_key, name_key_of
from src.landscaping.qualify import s1_status
from src.landscaping.sources import (
    DocumentRecord,
    jhtv,
    nsf,
    odp_patents,
    openalex_works,
    pubmed_works,
    reporter,
)

TODAY = date(2026, 10, 30)
WINDOW = 1096


def _work(**changes) -> DocumentRecord:
    base = DocumentRecord(
        kind="work", ids={"doi": "10.1234/inv.1"}, title="An invented kinase result",
        summary="Kinases matter.", published=date(2025, 3, 14), doc_type="article",
        language="en", license=None, authorships=None, role_basis="last",
    )
    return replace(base, **changes)


def test_s1_types_and_window():
    def status(rec):
        return s1_status(rec, today=TODAY, window_days=WINDOW)

    assert status(_work()) == "eligible"
    assert status(_work(doc_type="review")) == "excluded:type_review_evidence_only"
    assert status(_work(retracted=True)) == "excluded:retracted"
    assert status(_work(language="de")) == "excluded:language"
    assert status(_work(published=date(2022, 1, 5))) == "excluded:window"
    # The rest of D4, and the other exclusions S1 owns.
    for doc_type in ("erratum", "retraction", "editorial", "letter", "comment", "conference"):
        assert status(_work(doc_type=doc_type)) == f"excluded:type_{doc_type}"
    assert status(_work(role_basis=None)) == "excluded:not_qualified"
    assert status(_work(kind="dataset", role_basis=None)) == "excluded:dataset_evidence_only"
    patent = _work(kind="patent", doc_type="patent_application", language=None,
                   role_basis="assignee", raw_qualifier={"apl": True})
    assert status(patent) == "excluded:apl"
    assert status(replace(patent, raw_qualifier={"apl": False})) == "eligible"
    # A grant counts by any overlap of its project period with the window (§5.3).
    grant = _work(kind="grant", doc_type="grant", language=None, role_basis="grant_pi",
                  published=date(2019, 7, 1),
                  extra={"project_start": "2019-07-01", "project_end": "2025-06-30"})
    assert status(grant) == "eligible"
    assert status(replace(grant, extra={"project_start": "2019-07-01",
                                        "project_end": "2022-06-30"})) == "excluded:window"


def test_lab_key_every_record_type():
    openalex_rec = openalex_works.to_document({
        "id": "https://openalex.org/W4400000001", "type": "article", "title": "Invented",
        "publication_date": "2025-05-01", "language": "en",
        "authorships": [{
            "author_position": "last", "is_corresponding": False,
            "author": {"id": "https://openalex.org/A5000000001", "display_name": "Cora Lindqvist"},
            "institutions": [{"id": "https://openalex.org/I145311948"}],
            "raw_affiliation_strings": ["Johns Hopkins University, Baltimore, MD"],
        }],
    })
    pubmed_rec = pubmed_works.to_document(
        {"pmid": "39000002", "title": "Invented"},
        {"last_author": {"last": "Marrowby", "fore": "Tamsin", "orcid": None,
                         "affiliations": ["Johns Hopkins University, Baltimore, MD"]}},
    )
    nih_rec = reporter.to_document([{
        "core_project_num": "R01XX000001", "appl_id": 11000001, "project_title": "Invented",
        "organization": {"org_name": "JOHNS HOPKINS UNIVERSITY"},
        "principal_investigators": [
            {"full_name": "Ada Quill", "profile_id": 9000001, "is_contact_pi": True}],
    }])
    nsf_rec = nsf.to_document({"id": "2400001", "awardeeName": "Johns Hopkins University",
                               "piFirstName": "Ben", "piLastName": "Ostrow", "title": "Invented"})
    patent_rec = odp_patents.to_document({
        "applicationNumberText": "17000001",
        "applicationMetaData": {
            "inventionTitle": "Invented", "firstInventorName": "Rowan Petrakis",
            "applicantBag": [{"applicantNameText": "The Johns Hopkins University"}],
        },
    })
    jhtv_rec = jhtv.to_document({"case_id": "C12345", "title": "Invented",
                                 "author": "Imogen Vasquez-Tarr", "description": "x"})

    assert lab_key(openalex_rec) == "oa:A5000000001"
    assert lab_key(pubmed_rec) == "name:marrowby|t"
    assert lab_key(nih_rec) == "nih:9000001"
    assert lab_key(nsf_rec) == "name:ostrow|b"
    assert lab_key(patent_rec) == "name:petrakis|r"
    assert lab_key(jhtv_rec) == "name:vasquez-tarr|i"
    with_orcid = replace(openalex_rec, senior_names=[
        {**openalex_rec.senior_names[0], "orcid": "0000-0002-1825-0097"}])
    assert lab_key(with_orcid) == "orcid:0000-0002-1825-0097"


def test_name_key_forms():
    assert name_key_of("Lindqvist, Cora J.") == "name:lindqvist|c"
    assert name_key_of("Cora J. Lindqvist Jr.") == "name:lindqvist|c"
    assert name_key_of("Zoë Ångström") == "name:angstrom|z"
    assert name_key_of("Lindqvist") is None
    assert name_key_of("") is None


def test_canonical_id_precedence():
    ids = {"openalex": "W1", "pmid": "39000001", "doi": "10.1234/ABC"}
    assert canonical_id(ids) == "doi:10.1234/abc"
    assert canonical_id({"openalex": "W1", "pmid": "39000001"}) == "pmid:39000001"
    assert canonical_id({"openalex": "W1"}) == "openalex:W1"
    assert canonical_id({"appl_id": "1", "core_project": "R01XX1"}) == "core_project:R01XX1"


def test_summary_of_prefers_pubmed_abstract():
    rec = _work(summary="OpenAlex text", summary_source="openalex")
    assert summary_of(rec, "PubMed text") == "PubMed text"
    assert summary_of(rec, None) == "OpenAlex text"
    assert summary_of(replace(rec, summary="  "), None) is None


def test_pilot_slices_one_month_per_window_year():
    window_start = TODAY - timedelta(days=get_settings().landscape_window_days)
    slices = pilot_slices(today=TODAY, seed=7)
    months = sorted(key for source, key in slices if source == "openalex")
    years = [int(key[len("month:"):][:4]) for key in months]
    assert years == list(range(window_start.year, TODAY.year + 1))
    for key in months:
        year, month = (int(x) for x in key[len("month:"):].split("-"))
        assert (year, month) >= (window_start.year, window_start.month)
        assert (year, month) <= (TODAY.year, TODAY.month)
    # Every source covers the same months; the pick is reproducible from the seed.
    for source in ("pubmed", "nsf", "datacite", "geo"):
        assert sorted(k for s, k in slices if s == source) == months
    assert len([k for s, k in slices if s == "odp"]) <= len(years)
    assert len([k for s, k in slices if s == "reporter"]) <= len(years)
    assert ("jhtv", "feed:pilot") in slices
    assert pilot_slices(today=TODAY, seed=7) == slices
    # Each slice names a fetch its source takes.
    for source, key in slices:
        assert callable(slice_fetcher(source, key))
