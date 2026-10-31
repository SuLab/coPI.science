"""HGNC REST: gene symbol, alias or previous symbol → HGNC id, Entrez, Ensembl, UniProt
(spec §6.3, §7.4 step 4). HGNC asks for at most 10 requests a second."""
from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

import httpx

from src.landscaping import evidence
from src.services.http_pacing import Pacer

BASE_URL = "https://rest.genenames.org"
MIN_INTERVAL_S = 0.15
_PACER = Pacer(MIN_INTERVAL_S)
_HEADERS = {"Accept": "application/json"}
#: The fields searched, in the order a match is reported.
SEARCH_FIELDS = ("symbol", "alias_symbol", "prev_symbol")


def _make_client() -> httpx.AsyncClient:
    """Client factory: a seam so tests can inject an httpx.MockTransport."""
    return evidence.make_client()


def _gene(doc: dict, matched_on: str) -> dict:
    return {
        "hgnc_id": doc.get("hgnc_id"),
        "symbol": doc.get("symbol"),
        "status": doc.get("status"),
        "locus_group": doc.get("locus_group"),
        "entrez_id": doc.get("entrez_id"),
        "ensembl_gene_id": doc.get("ensembl_gene_id"),
        "uniprot_ids": list(doc.get("uniprot_ids") or []),
        "gene_group": list(doc.get("gene_group") or []),
        "matched_on": matched_on,
    }


async def hgnc_release() -> str:
    """HGNC's data month (``YYYY-MM`` of ``/info``'s ``lastModified``). HGNC updates daily
    and publishes monthly files; the month keeps the cache from expiring every day."""
    body = await evidence.request_json(_make_client, f"{BASE_URL}/info", pacer=_PACER,
                                       headers=_HEADERS)
    stamp = datetime.fromisoformat(str(body["lastModified"]).replace("Z", "+00:00"))
    return stamp.strftime("%Y-%m")


async def hgnc_lookup(symbol: str) -> list[dict]:
    """Every gene whose approved symbol, alias or previous symbol is ``symbol`` (HGNC
    matches case-insensitively), one entry per HGNC id with ``matched_on`` the first field
    that matched. The caller decides whether one approved gene is an unambiguous answer."""
    found: dict[str, dict] = {}
    for field in SEARCH_FIELDS:
        url = f"{BASE_URL}/fetch/{field}/{quote(symbol, safe='')}"
        body = await evidence.request_json(_make_client, url, pacer=_PACER, headers=_HEADERS,
                                           none_on_404=True)
        for doc in ((body or {}).get("response") or {}).get("docs") or []:
            hgnc_id = doc.get("hgnc_id")
            if hgnc_id and hgnc_id not in found:
                found[hgnc_id] = _gene(doc, field)
    return list(found.values())
