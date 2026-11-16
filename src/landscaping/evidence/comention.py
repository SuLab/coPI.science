"""PubMed co-mention counts of a target and a disease in a date window, the novelty signal of
spec §6.3 and §7.4 step 5.

Every request goes through `src.services.pubmed`, so NCBI pacing (`landscape_ncbi_max_rps`,
set once at worker start, D58) and the NCBI key stay there. PubMed's ``[mh]`` field does not
accept a MeSH unique id, so an id is first resolved to its heading through NCBI's ``mesh``
database.
"""
from __future__ import annotations

import re
from datetime import date

from src.services import pubmed

_MESH_UI = re.compile(r"^(?:mesh:)?([CD])(\d+)$", re.IGNORECASE)
#: NCBI ``mesh`` database uid prefix per MeSH UI kind: descriptors (D) and supplementary
#: concepts (C).
_MESH_DB_PREFIX = {"D": "68", "C": "67"}
#: The PubMed field a heading is searched in, per kind.
_MESH_FIELD = {"D": "mh", "C": "nm"}


def _quoted(text: str) -> str:
    return '"' + text.replace('"', " ").strip() + '"'


async def _mesh_heading(kind: str, digits: str) -> str | None:
    uid = _MESH_DB_PREFIX[kind] + digits
    resp = await pubmed._ncbi_get(f"{pubmed.EUTILS_BASE}/esummary.fcgi",
                                  {"db": "mesh", "id": uid, "retmode": "json"})
    record = (resp.json().get("result") or {}).get(uid) or {}
    terms = record.get("ds_meshterms") or []
    return str(terms[0]) if terms else None


async def _disease_clause(disease_mesh: str) -> str | None:
    """The PubMed clause for a MeSH id (``D003920``, ``MESH:D003920``) or heading; None
    when an id has no heading."""
    match = _MESH_UI.match(disease_mesh.strip())
    if match is None:
        return f"{_quoted(disease_mesh)}[mh]"
    kind, digits = match.group(1).upper(), match.group(2)
    heading = await _mesh_heading(kind, digits)
    return f"{_quoted(heading)}[{_MESH_FIELD[kind]}]" if heading else None


async def _count(term: str) -> int:
    count, _ = await pubmed.esearch_page(term, retstart=0, retmax=0)
    return count


async def comention_counts(target: str, disease_mesh: str, *, window: tuple[date, date]) -> dict:
    """PubMed record counts within ``window`` (publication dates, inclusive): ``pair`` for
    the target in title/abstract together with the disease's MeSH heading, and ``target``
    and ``disease`` alone as the base rates a novelty reading needs. Counts are None when
    the MeSH id has no heading."""
    start, end = window
    dates = f"{start:%Y/%m/%d}:{end:%Y/%m/%d}[dp]"
    target_clause = f"{_quoted(target)}[tiab]"
    disease_clause = await _disease_clause(disease_mesh)
    out: dict = {"target": None, "disease": None, "pair": None,
                 "window": [start.isoformat(), end.isoformat()]}
    if disease_clause is None:
        return out
    out["target"] = await _count(f"{target_clause} AND {dates}")
    out["disease"] = await _count(f"{disease_clause} AND {dates}")
    out["pair"] = await _count(f"{target_clause} AND {disease_clause} AND {dates}")
    return out
