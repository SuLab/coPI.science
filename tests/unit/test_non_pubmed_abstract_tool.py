"""retrieve_abstract / retrieve_full_text for a DOI PubMed does not have
(spec 2026-10-09 §5.6)."""
import pytest

from src.agent import tools as tools_mod
from src.services import openalex, pubmed

#: Byte-identical to today's message: the prompt-freeze golden pins it.
_PUBMED_MISS = (
    "Could not resolve DOI 10.1109/lra.2022.1 to a PMID — either PubMed has no record for "
    "it or the lookup failed (both are swallowed by the converter, so they are "
    "indistinguishable here). This is not evidence that the paper does not exist."
)


@pytest.fixture(autouse=True)
def pubmed_has_no_pmid(monkeypatch):
    async def convert(dois, *, strict=False, permanently_dropped=None):
        return {}

    monkeypatch.setattr(pubmed, "convert_dois_to_pmids", convert)


def _work(**kw):
    return {"pmid": None, "doi": "10.1109/lra.2022.1", "year": 2022, "title": "SHARP",
            "type": "article", "venue": "IEEE RA-L", "abstract": "Shielding-aware planning.",
            "is_retracted": False, **kw}


async def test_a_doi_pubmed_lacks_is_answered_from_openalex(monkeypatch):
    async def lookup(doi, **kw):
        return _work()

    monkeypatch.setattr(openalex, "fetch_work_by_doi", lookup)
    assert await pubmed.fetch_abstract("10.1109/LRA.2022.1") == {
        "pmid": None, "doi": "10.1109/LRA.2022.1", "title": "SHARP",
        "abstract": "Shielding-aware planning.", "journal": "IEEE RA-L", "year": 2022}


@pytest.mark.parametrize("answer", ["none", "raise", "untitled"])
async def test_without_an_openalex_answer_the_pubmed_message_is_unchanged(monkeypatch, answer):
    async def lookup(doi, **kw):
        if answer == "raise":
            raise RuntimeError("OpenAlex down")
        return None if answer == "none" else _work(title="")

    monkeypatch.setattr(openalex, "fetch_work_by_doi", lookup)
    assert await pubmed.fetch_abstract("10.1109/lra.2022.1") == {"error": _PUBMED_MISS}


async def test_full_text_of_a_non_pubmed_doi_says_there_is_no_pmc_copy(monkeypatch):
    async def lookup(doi, **kw):
        return _work()

    async def no_pmc(pmids):
        raise AssertionError("no PubMed Central lookup without a PMID")

    monkeypatch.setattr(openalex, "fetch_work_by_doi", lookup)
    monkeypatch.setattr(pubmed, "convert_pmids_to_pmcids", no_pmc)
    out = await pubmed.fetch_full_text("10.1109/lra.2022.1")
    assert out["pmid"] is None and out["methods"] is None
    assert "not in PubMed" in out["note"]


def test_a_paper_without_a_pmid_is_formatted_by_its_doi():
    result = {"pmid": None, "doi": "10.1109/lra.2022.1", "title": "SHARP", "abstract": "A.",
              "journal": "IEEE RA-L", "year": 2022}
    text = tools_mod._format_abstract(result)
    assert "DOI: 10.1109/lra.2022.1" in text and "PMID" not in text
    full = tools_mod._format_full_text({**result, "pmcid": None, "methods": None, "note": "N."})
    assert "DOI: 10.1109/lra.2022.1" in full and "PMID" not in full


def test_a_pubmed_paper_is_still_formatted_by_its_pmid():
    text = tools_mod._format_abstract(
        {"pmid": "123", "title": "T", "abstract": "A.", "journal": "J", "year": 2020})
    assert "PMID: 123" in text and "DOI:" not in text


async def test_the_agent_lookup_is_one_short_attempt(monkeypatch):
    seen = {}

    async def lookup(doi, **kw):
        seen.update(kw)
        return _work()

    monkeypatch.setattr(openalex, "fetch_work_by_doi", lookup)
    await pubmed.fetch_abstract("10.1109/lra.2022.1")
    assert seen == {"attempts": 1, "timeout": 10.0}


async def test_a_retracted_work_reads_as_the_pubmed_miss(monkeypatch):
    async def lookup(doi, **kw):
        return _work(is_retracted=True)

    monkeypatch.setattr(openalex, "fetch_work_by_doi", lookup)
    assert await pubmed.fetch_abstract("10.1109/lra.2022.1") == {"error": _PUBMED_MISS}


async def test_a_prefixed_doi_is_normalised_before_the_lookup(monkeypatch):
    seen = []

    async def lookup(doi, **kw):
        seen.append(doi)
        return _work()

    monkeypatch.setattr(openalex, "fetch_work_by_doi", lookup)
    out = await pubmed.fetch_abstract("https://doi.org/10.1109/LRA.2022.1")
    assert seen == ["10.1109/LRA.2022.1"] and out["title"] == "SHARP"
