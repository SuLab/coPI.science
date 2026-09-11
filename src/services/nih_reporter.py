"""NIH RePORTER v2 client (api.reporter.nih.gov). Live-verified 2026-09-11.

Two guards exist because RePORTER SILENTLY IGNORES unknown criteria keys:
``{"criteria": {"orcid": [...]}}`` returns HTTP 200 and the whole database
(2,975,461 rows on 2026-09-11). A typo must fail loudly, not attach every NIH
grant to one PI. Rate limit measured at ~200 requests/minute per IP
(``x-rate-limit-limit: 1m``), so requests are paced 0.35 s apart.
"""
import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

BASE = "https://api.reporter.nih.gov/v2"
PAGE = 500
_PACE_INTERVAL = 0.35
_next_slot = 0.0

ALLOWED_CRITERIA = frozenset({
    "pi_names", "pi_profile_ids", "org_names", "org_names_exact_match", "fiscal_years",
    "project_nums", "core_project_nums", "include_active_projects", "appl_ids",
})
PROJECT_FIELDS = [
    "ProjectNum", "CoreProjectNum", "FiscalYear", "ProjectTitle", "PhrText", "Terms",
    "ActivityCode", "AgencyIcAdmin", "FundingMechanism", "Organization",
    "PrincipalInvestigators", "ProjectStartDate", "ProjectEndDate", "AwardAmount",
    "SubprojectId", "IsActive",
]
JHU_ORG_EXACT = "JOHNS HOPKINS UNIVERSITY"


class ReporterCriteriaError(ValueError):
    """A criteria key outside ALLOWED_CRITERIA — RePORTER would ignore it silently."""


class ReporterFirehoseError(RuntimeError):
    """meta.total exceeded the per-PI ceiling; the filter did not bite."""


async def _pace() -> None:
    global _next_slot
    loop = asyncio.get_running_loop()
    now = loop.time()
    wait = _next_slot - now
    _next_slot = max(now, _next_slot) + _PACE_INTERVAL
    if wait > 0:
        await asyncio.sleep(wait)


async def _post(client: httpx.AsyncClient, path: str, body: dict) -> dict:
    await _pace()
    for attempt in range(3):
        resp = await client.post(f"{BASE}/{path}", json=body)
        if resp.status_code == 429 and attempt < 2:
            await asyncio.sleep(2.0 * (attempt + 1))
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("unreachable")


def _check_criteria(criteria: dict) -> None:
    bad = set(criteria) - ALLOWED_CRITERIA
    if bad:
        raise ReporterCriteriaError(f"RePORTER would silently ignore criteria keys: {sorted(bad)}")


async def search_projects(criteria: dict, include_fields: list[str], *, max_total: int = 500) -> list[dict]:
    _check_criteria(criteria)
    out: list[dict] = []
    offset = 0
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            data = await _post(client, "projects/search", {
                "criteria": criteria, "include_fields": include_fields,
                "offset": offset, "limit": PAGE,
            })
            total = int(data.get("meta", {}).get("total", 0))
            if total > max_total:
                raise ReporterFirehoseError(f"RePORTER returned total={total} > {max_total} for {criteria}")
            rows = data.get("results") or []
            out.extend(rows)
            offset += PAGE
            if offset >= total or not rows:
                return out


async def publications_for_cores(core_project_nums: list[str]) -> dict[str, set[str]]:
    links: dict[str, set[str]] = {}
    if not core_project_nums:
        return links
    async with httpx.AsyncClient(timeout=30) as client:
        for i in range(0, len(core_project_nums), 50):
            chunk = core_project_nums[i:i + 50]
            offset = 0
            while True:
                data = await _post(client, "publications/search", {
                    "criteria": {"core_project_nums": chunk}, "offset": offset, "limit": PAGE,
                })
                for r in data.get("results") or []:
                    links.setdefault(r["coreproject"], set()).add(str(r["pmid"]))
                total = int(data.get("meta", {}).get("total", 0))
                offset += PAGE
                if offset >= total:
                    break
    return links
