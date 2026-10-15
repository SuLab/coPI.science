"""pubmed.classify_idconv and lookup_pmcids (R4.14): only a positive "absent" clears."""
import httpx

from src.services import pubmed
from src.services.pubmed import IDCONV_ABSENT, classify_idconv


def test_ok_record():
    assert classify_idconv(["1"], {"records": [
        {"requested-id": "1", "pmid": 1, "pmcid": "PMC9"}]}) == {"1": ("pmcid", "PMC9")}


def test_absent_record():
    assert classify_idconv(["1"], {"records": [
        {"requested-id": "1", "status": "error", "errmsg": IDCONV_ABSENT}]}) == {
        "1": ("absent", None)}


def test_other_error_is_unknown():
    assert classify_idconv(["1"], {"records": [
        {"requested-id": "1", "status": "error", "errmsg": "Invalid article id"}]})["1"][0] == (
        "unknown")


def test_missing_record_is_unknown():
    assert classify_idconv(["1"], {"records": []}) == {"1": ("unknown", "no-record")}


def test_pmid_mismatch_is_unknown():
    assert classify_idconv(["1"], {"records": [
        {"requested-id": "1", "pmid": 2, "pmcid": "PMC9"}]})["1"][0] == "unknown"


async def test_a_failed_chunk_puts_its_pmids_in_neither_set(monkeypatch):
    calls: list[list[str]] = []

    async def fake_get(url, params):
        assert url == pubmed.IDCONV_BASE and params["format"] == "json"
        calls.append(params["ids"].split(","))
        if len(calls) == 2:
            raise httpx.TransportError("connection reset")
        return httpx.Response(200, json={"records": [
            {"requested-id": p, "status": "error", "errmsg": IDCONV_ABSENT}
            for p in calls[-1]]})

    monkeypatch.setattr(pubmed, "_ncbi_get", fake_get)
    pmids = [str(n) for n in range(1, 202)]
    lookup = await pubmed.lookup_pmcids(pmids)
    assert len(calls) == 2 and calls[1] == ["201"]
    assert "201" not in lookup.absent and "201" not in lookup.found
    assert lookup.absent == frozenset(pmids[:200]) and lookup.found == {}
