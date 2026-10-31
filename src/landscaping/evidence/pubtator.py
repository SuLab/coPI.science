"""PubTator3 export: gene, disease and chemical mentions with ids, per PubMed record
(spec §6.3, §7.4 step 4). Paced at `landscape_pubtator_max_rps` (NCBI's 3 req/s)."""
from __future__ import annotations

import httpx

from src.config import get_settings
from src.landscaping import evidence
from src.services.http_pacing import Pacer

EXPORT_URL = "https://www.ncbi.nlm.nih.gov/research/pubtator3-api/publications/export/biocjson"
#: PMIDs per export request.
BATCH_SIZE = 100


def _interval() -> float:
    return 1.0 / get_settings().landscape_pubtator_max_rps


_PACER = Pacer(lambda: _interval())  # the lambda keeps a patched `_interval` effective


def _make_client() -> httpx.AsyncClient:
    """Client factory: a seam so tests can inject an httpx.MockTransport."""
    return evidence.make_client()


def _annotation(raw: dict) -> dict:
    """One mention, flat (the shape ``normalize`` reads): ``type`` (Gene, Disease, Chemical,
    Species, …), ``identifier`` (an NCBI Gene id for a gene, ``MESH:…`` for a disease or
    chemical; None when PubTator did not normalize it), the mention ``text``, ``database``,
    and ``name`` (PubTator's normalized name, a gene's symbol; None when absent). Gene ids
    are species-specific, so a caller checks the species before treating one as human."""
    infons = raw.get("infons") or {}
    return {
        "type": infons.get("type"),
        "identifier": infons.get("identifier"),
        "text": raw.get("text") or "",
        "database": infons.get("database"),
        "name": infons.get("name"),
    }


def _doc_annotations(doc: dict) -> list[dict]:
    return [_annotation(a) for passage in doc.get("passages") or []
            for a in passage.get("annotations") or []]


async def pubtator_annotations(pmids: list[str]) -> dict[str, list[dict]]:
    """Every requested PMID → its annotations, in passage order; a PMID PubTator3 does not
    have maps to ``[]``."""
    wanted = [str(p) for p in dict.fromkeys(pmids)]
    out: dict[str, list[dict]] = {p: [] for p in wanted}
    for start in range(0, len(wanted), BATCH_SIZE):
        chunk = wanted[start:start + BATCH_SIZE]
        body = await evidence.request_json(
            _make_client, EXPORT_URL, pacer=_PACER, params={"pmids": ",".join(chunk)},
            none_on_404=True)
        docs = body.get("PubTator3") if isinstance(body, dict) else body
        for doc in docs or []:
            pmid = str(doc.get("pmid") or doc.get("id") or "")
            if pmid in out:
                out[pmid] = _doc_annotations(doc)
    return out
