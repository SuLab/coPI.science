"""Orphadata API: the prevalence class of a rare disease by ORPHAcode (spec §6.3, §7.7 step
3). Orphadata names no rate limit; requests are paced at one a second."""
from __future__ import annotations

import httpx

from src.landscaping import evidence
from src.services.http_pacing import Pacer

EPIDEMIOLOGY_URL = "https://api.orphadata.com/rd-epidemiology/orphacodes/{code}"
MIN_INTERVAL_S = 1.0
_PACER = Pacer(MIN_INTERVAL_S)


def _make_client() -> httpx.AsyncClient:
    """Client factory: a seam so tests can inject an httpx.MockTransport."""
    return evidence.make_client()


def _orpha_code(value: str) -> str:
    """``ORPHA:558``, ``Orphanet:558`` or ``558`` → ``558``."""
    return value.rsplit(":", 1)[-1].strip()


async def orphadata_prevalence(orpha_code: str) -> dict | None:
    """``{"orpha_code", "name", "prevalence": [...]}`` with one entry per Orphadata
    prevalence record (class, type, geography, validation status, source); None when
    Orphadata has no epidemiology for the code."""
    code = _orpha_code(orpha_code)
    body = await evidence.request_json(_make_client, EPIDEMIOLOGY_URL.format(code=code),
                                       pacer=_PACER, none_on_404=True)
    results = ((body or {}).get("data") or {}).get("results")
    if not isinstance(results, dict):
        return None
    return {
        "orpha_code": code,
        "name": results.get("Preferred term"),
        "prevalence": [{
            "class": p.get("PrevalenceClass"),
            "type": p.get("PrevalenceType"),
            "geography": p.get("PrevalenceGeographic"),
            "validation": p.get("PrevalenceValidationStatus"),
            "source": p.get("Source"),
        } for p in results.get("Prevalence") or [] if isinstance(p, dict)],
    }
