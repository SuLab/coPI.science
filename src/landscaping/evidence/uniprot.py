"""UniProt REST: a protein's function text, for the card's target biology (spec §6.3, §7.7
step 4)."""
from __future__ import annotations

from urllib.parse import quote

import httpx

from src.landscaping import evidence
from src.services.http_pacing import Pacer

ENTRY_URL = "https://rest.uniprot.org/uniprotkb/{accession}.json"
FIELDS = "accession,protein_name,gene_primary,cc_function"
MIN_INTERVAL_S = 0.2
_PACER = Pacer(MIN_INTERVAL_S)


def _make_client() -> httpx.AsyncClient:
    """Client factory: a seam so tests can inject an httpx.MockTransport."""
    return evidence.make_client()


def _protein_name(entry: dict) -> str | None:
    recommended = (entry.get("proteinDescription") or {}).get("recommendedName") or {}
    return (recommended.get("fullName") or {}).get("value")


def _gene(entry: dict) -> str | None:
    genes = entry.get("genes") or []
    return ((genes[0].get("geneName") or {}).get("value")) if genes else None


async def uniprot_function(accession: str) -> dict:
    """``{"accession", "protein_name", "gene", "function": [text, …]}``, the ``FUNCTION``
    comment texts in entry order; ``function`` is empty for an unknown accession."""
    url = ENTRY_URL.format(accession=quote(accession, safe=""))
    entry = await evidence.request_json(_make_client, url, pacer=_PACER,
                                        params={"fields": FIELDS}, none_on_404=True) or {}
    texts = [t.get("value") for c in entry.get("comments") or []
             if c.get("commentType") == "FUNCTION" for t in c.get("texts") or []]
    return {
        "accession": entry.get("primaryAccession") or accession,
        "protein_name": _protein_name(entry),
        "gene": _gene(entry),
        "function": [t for t in texts if t],
    }
