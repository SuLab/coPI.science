"""Support lists and in-text ``doc:`` references on the Landscape pages
(docs/plans/2026-11-19-landscape-ui-remediation-plan.md, C3 and C4). Every title and id is
invented."""

from src.services.landscape_sources import (
    DocInfo,
    Quote,
    resolve_support,
    rewrite_doc_refs,
    short_title,
)

DOC_A = "1234abcd-0000-4000-8000-000000000001"
DOC_B = "5678ef90-0000-4000-8000-000000000002"
DOC_C = "5678ef90-1111-4000-8000-000000000003"


def _doc(doc_id: str, title: str | None, doi: str | None = None, pmid: str | None = None):
    return DocInfo(id=doc_id, title=title, doi=doi, pmid=pmid, kind="work")


DOCS = {
    DOC_A: _doc(DOC_A, "An invented paper on fibrosis", doi="10.9999/invented-a"),
    DOC_B: _doc(DOC_B, "Another invented paper", pmid="12345"),
}
QUOTES = [
    Quote(document="An invented paper on fibrosis", document_id=DOC_A, section="Results",
          quote="The invented compound cut fibrosis by half.", basis="summary"),
]


def _one(ref: str, *, expert_hidden: bool = False) -> dict:
    return resolve_support([ref], DOCS, QUOTES, expert_hidden=expert_hidden)


def test_resolve_support_classes():
    assert _one(f"doc:{DOC_A}") == {"kind": "sourced", "items": [{
        "label": "An invented paper on fibrosis", "url": "https://doi.org/10.9999/invented-a",
        "quote": None,
    }]}
    assert _one("doc:5678EF90")["items"] == [{
        "label": "Another invented paper", "url": "https://pubmed.ncbi.nlm.nih.gov/12345/",
        "quote": None,
    }]
    assert _one("pmid:999") == {"kind": "sourced", "items": [
        {"label": "PubMed 999", "url": "https://pubmed.ncbi.nlm.nih.gov/999/", "quote": None},
    ]}
    assert _one("nct:NCT01234567")["items"] == [
        {"label": "NCT01234567", "url": "https://clinicaltrials.gov/study/NCT01234567",
         "quote": None},
    ]
    assert _one("nct:not-an-id")["items"][0]["url"] is None
    labels = {
        "patent:US1234567": "Patent US1234567",
        "grant:R01XX000001": "Grant R01XX000001",
        "orcid:0000-0000-0000-0001": "ORCID 0000-0000-0000-0001",
        "uniprot:Q9HBA0": "UniProt Q9HBA0",
        "opentargets:ENSG00000000001": "Open Targets ENSG00000000001",
        "hpa:ENSG00000000002": "HPA ENSG00000000002",
        "mondo:MONDO:0009999": "Mondo MONDO:0009999",
        "orphadata:ORPHA:999": "Orphadata ORPHA:999",
        "chembl:CHEMBL0001": "ChEMBL CHEMBL0001",
        "openfda:invented-label": "openFDA invented-label",
        "trials:invented compound": "ClinicalTrials.gov records (invented compound)",
    }
    for ref, label in labels.items():
        assert _one(ref) == {"kind": "sourced",
                             "items": [{"label": label, "url": None, "quote": None}]}, ref
    quoted = _one("  the invented COMPOUND cut\nfibrosis by half. ")
    assert quoted == {"kind": "sourced", "items": [{
        "label": "An invented paper on fibrosis", "url": "https://doi.org/10.9999/invented-a",
        "quote": "The invented compound cut fibrosis by half.",
    }]}
    assert _one("estimate") == {"kind": "estimate", "items": []}
    assert _one("estimate: from an invented cohort") == {"kind": "estimate", "items": []}
    analysis = {
        "specialist:budget": "budget specialist",
        "budget specialist: an invented note": "budget specialist",
        "talent-specialist: an invented note": "talent specialist",
        "deep_analysis:chemistry.concerns": "deep analysis (chemistry concerns)",
        "flag:target_essential": "deal-breaker review (target essential)",
        "flags:same_target_in_clinic": "deal-breaker review (same target in clinic)",
        "flags_reviewed:safety_liabilities": "deal-breaker review (safety liabilities)",
        "disease_profile:unmet_need from S6": "disease profile (unmet need)",
        "search_prior_art: invented query": "prior-art search",
        "retrieved_sources: invented list": "retrieved sources",
        "An invented free-text reason the model gave.": "analysis note",
        "doc:ffffffff": "an unnamed dossier document",
    }
    for ref, label in analysis.items():
        assert _one(ref) == {"kind": "analysis",
                             "items": [{"label": label, "url": None, "quote": None}]}, ref
    assert resolve_support(None, DOCS, QUOTES, expert_hidden=False) == {
        "kind": "estimate", "items": [],
    }
    assert resolve_support("pmid:1", DOCS, QUOTES, expert_hidden=False)["kind"] == "sourced"


def test_resolve_support_list_kind():
    mixed = resolve_support(["estimate", "specialist:budget", "pmid:1"], DOCS, QUOTES,
                            expert_hidden=False)
    assert mixed["kind"] == "sourced"
    assert [item["label"] for item in mixed["items"]] == ["budget specialist", "PubMed 1"]
    assert resolve_support(["estimate", "an invented note"], DOCS, QUOTES,
                           expert_hidden=False)["kind"] == "analysis"
    assert resolve_support(["estimate", "estimate: invented"], DOCS, QUOTES,
                           expert_hidden=False)["kind"] == "estimate"


def test_resolve_support_expert_hides_analysis_labels():
    hidden = resolve_support(
        ["talent-specialist: an invented note", "deep_analysis:chemistry.concerns", "pmid:7"],
        DOCS, QUOTES, expert_hidden=True,
    )
    assert hidden["items"] == [
        {"label": "analysis", "url": None, "quote": None},
        {"label": "analysis", "url": None, "quote": None},
        {"label": "PubMed 7", "url": "https://pubmed.ncbi.nlm.nih.gov/7/", "quote": None},
    ]


def test_rewrite_doc_refs_unique_ambiguous_missing():
    docs = {**DOCS, DOC_C: _doc(DOC_C, "A third invented paper")}
    url_a = "https://doi.org/10.9999/invented-a"
    assert rewrite_doc_refs(f"See doc:{DOC_A}.", docs, markdown=True) == (
        f"See [An invented paper on fibrosis](<{url_a}>)."
    )
    assert rewrite_doc_refs("See doc:1234ABCD, too.", docs, markdown=True) == (
        f"See [An invented paper on fibrosis](<{url_a}>), too."
    )
    # 5678ef90 prefixes both DOC_B and DOC_C.
    assert rewrite_doc_refs("See doc:5678ef90.", docs, markdown=True) == (
        "See an unnamed dossier document."
    )
    assert rewrite_doc_refs("See doc:99999999.", docs, markdown=False) == (
        "See an unnamed dossier document."
    )
    assert rewrite_doc_refs("doc:1234abc is too short", docs, markdown=True) == (
        "doc:1234abc is too short"
    )
    assert rewrite_doc_refs(None, docs, markdown=True) is None


def test_rewrite_doc_refs_escapes():
    hostile = {DOC_A: _doc(DOC_A, "<b>Bold</b> *x* a]b [c]", doi="10.1000/(abc)")}
    out = rewrite_doc_refs(f"From doc:{DOC_A}.", hostile, markdown=True)
    assert out == (
        "From [&lt;b&gt;Bold&lt;/b&gt; \\*x\\* a b c](<https://doi.org/10.1000/(abc)>)."
    )
    assert "<b>" not in out
    simple = {DOC_A: _doc(DOC_A, "a]b", doi="10.1000/(abc)")}
    assert rewrite_doc_refs(f"doc:{DOC_A}", simple, markdown=True) == (
        "[a b](<https://doi.org/10.1000/(abc)>)"
    )
    no_link = {DOC_A: _doc(DOC_A, "*x*")}
    assert rewrite_doc_refs(f"doc:{DOC_A}", no_link, markdown=True) == "\\*x\\*"
    untitled = {DOC_A: _doc(DOC_A, None)}
    assert rewrite_doc_refs(f"doc:{DOC_A}", untitled, markdown=True) == "Untitled document"


def test_rewrite_doc_refs_plain():
    assert rewrite_doc_refs(f"See doc:{DOC_B}.", DOCS, markdown=False) == (
        "See Another invented paper (https://pubmed.ncbi.nlm.nih.gov/12345/)."
    )
    no_link = {DOC_A: _doc(DOC_A, "An invented <i>title</i>")}
    assert rewrite_doc_refs(f"doc:{DOC_A}", no_link, markdown=False) == (
        "An invented <i>title</i>"
    )
    long_title = "word " * 30
    shortened = short_title(long_title)
    assert shortened.endswith("…") and len(shortened) <= 81
    assert rewrite_doc_refs(f"doc:{DOC_A}", {DOC_A: _doc(DOC_A, long_title)},
                            markdown=False) == shortened
