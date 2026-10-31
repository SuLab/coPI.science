"""ChEMBL web services (spec §6.3, release 37): target mechanisms with max phase, the target
of a UniProt accession, and molecule lookup by name for agent normalization (§7.4 step 4)."""
from __future__ import annotations

import httpx

from src.landscaping import evidence
from src.services.http_pacing import Pacer

BASE_URL = "https://www.ebi.ac.uk/chembl/api/data"
MIN_INTERVAL_S = 0.5
_PACER = Pacer(MIN_INTERVAL_S)
PAGE_LIMIT = 1000
_MOLECULE_FIELDS = "molecule_chembl_id,pref_name,max_phase,molecule_type"


def _make_client() -> httpx.AsyncClient:
    """Client factory: a seam so tests can inject an httpx.MockTransport."""
    return evidence.make_client()


def _phase(value: object) -> float | None:
    """ChEMBL gives max phase as an int on mechanisms and a string ("4.0") on molecules."""
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


async def _paged(path: str, key: str, params: dict) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        body = await evidence.request_json(
            _make_client, f"{BASE_URL}/{path}", pacer=_PACER,
            params={**params, "limit": PAGE_LIMIT, "offset": offset})
        page = body.get(key) or []
        rows.extend(page)
        if not page or not (body.get("page_meta") or {}).get("next"):
            return rows
        offset += len(page)


async def chembl_release() -> str:
    """The served release, e.g. ``ChEMBL_37``."""
    body = await evidence.request_json(_make_client, f"{BASE_URL}/status.json", pacer=_PACER)
    return str(body["chembl_db_version"])


async def chembl_mechanisms(target_chembl_id: str) -> list[dict]:
    """The target's mechanisms of action: molecule, action type, mechanism text and max
    phase (a float, 4.0 = approved; None when ChEMBL gives none)."""
    rows = await _paged("mechanism.json", "mechanisms", {"target_chembl_id": target_chembl_id})
    return [{
        "molecule_chembl_id": r.get("molecule_chembl_id"),
        "parent_molecule_chembl_id": r.get("parent_molecule_chembl_id"),
        "action_type": r.get("action_type"),
        "mechanism_of_action": r.get("mechanism_of_action"),
        "max_phase": _phase(r.get("max_phase")),
    } for r in rows]


async def chembl_target_for_uniprot(accession: str) -> str | None:
    """The ChEMBL id of the one human SINGLE PROTEIN target whose component is the UniProt
    ``accession``; None when there is none or more than one (§7.4 step 4)."""
    rows = await _paged("target.json", "targets", {
        "target_components__accession": accession,
        "target_type": "SINGLE PROTEIN",
        "organism": "Homo sapiens",
    })
    found = {
        r["target_chembl_id"] for r in rows
        if r.get("target_chembl_id") and r.get("target_type") == "SINGLE PROTEIN"
        and r.get("organism") == "Homo sapiens"
        and any(c.get("accession") == accession for c in r.get("target_components") or [])
    }
    return found.pop() if len(found) == 1 else None


async def chembl_molecule(name: str) -> list[dict]:
    """Every molecule whose preferred name or a synonym equals ``name`` (case-insensitive),
    one per ChEMBL id. The caller takes an id only when exactly one comes back."""
    found: dict[str, dict] = {}
    for lookup in ("pref_name__iexact", "molecule_synonyms__molecule_synonym__iexact"):
        rows = await _paged("molecule.json", "molecules",
                            {lookup: name, "only": _MOLECULE_FIELDS})
        for r in rows:
            chembl_id = r.get("molecule_chembl_id")
            if chembl_id and chembl_id not in found:
                found[chembl_id] = {
                    "molecule_chembl_id": chembl_id,
                    "pref_name": r.get("pref_name"),
                    "max_phase": _phase(r.get("max_phase")),
                    "molecule_type": r.get("molecule_type"),
                }
    return list(found.values())
