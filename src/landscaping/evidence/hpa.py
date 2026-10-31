"""Human Protein Atlas: a gene's tissue RNA expression summary, for the card's target biology
(spec §6.3, §7.7 step 4)."""
from __future__ import annotations

from urllib.parse import quote

import httpx

from src.landscaping import evidence
from src.services.http_pacing import Pacer

GENE_URL = "https://www.proteinatlas.org/{ensembl_id}.json"
MIN_INTERVAL_S = 1.0
_PACER = Pacer(MIN_INTERVAL_S)
#: The HPA entry keys kept, under the names returned.
FIELDS = {
    "RNA tissue specificity": "tissue_specificity",
    "RNA tissue distribution": "tissue_distribution",
    "RNA tissue specific nTPM": "tissue_specific_ntpm",
    "RNA tissue cell type enrichment": "tissue_cell_type_enrichment",
    "RNA single cell type specificity": "single_cell_type_specificity",
}


def _make_client() -> httpx.AsyncClient:
    """Client factory: a seam so tests can inject an httpx.MockTransport."""
    return evidence.make_client()


async def hpa_expression(ensembl_id: str) -> dict:
    """``{"ensembl_id", "tissue_specificity", …}``, one key per `FIELDS` value (None when
    HPA gives none, every value None for a gene HPA does not have)."""
    url = GENE_URL.format(ensembl_id=quote(ensembl_id, safe=""))
    body = await evidence.request_json(_make_client, url, pacer=_PACER, none_on_404=True)
    entry = (body[0] if body else {}) if isinstance(body, list) else (body or {})
    return {"ensembl_id": ensembl_id, **{out: entry.get(raw) for raw, out in FIELDS.items()}}
