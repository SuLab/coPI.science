"""NIH RePORTER v2 client (api.reporter.nih.gov). Live-verified 2026-09-11.

Two guards exist because RePORTER SILENTLY IGNORES unknown criteria keys:
``{"criteria": {"orcid": [...]}}`` returns HTTP 200 and the whole database
(2,975,461 rows on 2026-09-11). A typo must fail loudly, not attach every NIH
grant to one PI. First, each endpoint has its own criteria allowlist
(``PROJECTS_CRITERIA``, ``PUBLICATIONS_CRITERIA``), so a key one endpoint honours
cannot reach the other, which would ignore it; second, ``search_projects`` refuses a
``meta.total`` over the caller's ``max_total`` (``ReporterFirehoseError``).

Rate limit measured at ~200 requests/minute per IP (``x-rate-limit-limit: 1m``), so
requests are paced 0.35 s apart.
"""
import logging

import httpx

from src.services.http_pacing import Pacer, with_retries

logger = logging.getLogger(__name__)

BASE = "https://api.reporter.nih.gov/v2"
PAGE = 500
_PACE_INTERVAL = 0.35
_PACER = Pacer(_PACE_INTERVAL)

#: Criteria keys each endpoint honours. RePORTER SILENTLY IGNORES an unknown key, and the
#: two endpoints differ: projects/search ignores core_project_nums (meta.total 2,983,191
#: on 2026-10-05, spec 2026-10-05 §3), which publications/search requires. Split per
#: endpoint (NEW-2) so a key valid on one cannot reach the other.
PROJECTS_CRITERIA = frozenset({
    "pi_names", "pi_profile_ids", "org_names", "org_names_exact_match", "fiscal_years",
    "project_nums", "include_active_projects", "appl_ids",
})
PUBLICATIONS_CRITERIA = frozenset({"core_project_nums"})
PROJECT_FIELDS = [
    "ProjectNum", "CoreProjectNum", "FiscalYear", "ProjectTitle", "PhrText", "Terms",
    "ActivityCode", "AgencyIcAdmin", "FundingMechanism", "Organization",
    "PrincipalInvestigators", "ProjectStartDate", "ProjectEndDate", "AwardAmount",
    "SubprojectId", "IsActive",
]
JHU_ORG_EXACT = "JOHNS HOPKINS UNIVERSITY"


class ReporterCriteriaError(ValueError):
    """A criteria key outside the endpoint's allowlist — RePORTER would ignore it silently."""


class ReporterFirehoseError(RuntimeError):
    """meta.total exceeded the per-PI ceiling; the filter did not bite."""


async def _post(client: httpx.AsyncClient, path: str, body: dict) -> dict:
    # Paces once per _post (not per 429 retry), as before.
    resp = await with_retries(
        lambda: client.post(f"{BASE}/{path}", json=body),
        attempts=3, backoff=lambda a: 2.0 * (a + 1),
        retry_statuses=frozenset({429}), pacer=_PACER, pace_each_attempt=False,
    )
    resp.raise_for_status()
    return resp.json()


def _check_criteria(criteria: dict, allowed: frozenset[str], endpoint: str) -> None:
    bad = set(criteria) - allowed
    if bad:
        raise ReporterCriteriaError(
            f"RePORTER {endpoint} would silently ignore criteria keys: {sorted(bad)}"
        )


async def search_projects(criteria: dict, include_fields: list[str], *, max_total: int = 500) -> list[dict]:
    _check_criteria(criteria, PROJECTS_CRITERIA, "projects/search")
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
            criteria = {"core_project_nums": chunk}
            _check_criteria(criteria, PUBLICATIONS_CRITERIA, "publications/search")
            offset = 0
            while True:
                data = await _post(client, "publications/search", {
                    "criteria": criteria, "offset": offset, "limit": PAGE,
                })
                for r in data.get("results") or []:
                    links.setdefault(r["coreproject"], set()).add(str(r["pmid"]))
                total = int(data.get("meta", {}).get("total", 0))
                offset += PAGE
                if offset >= total:
                    break
    return links


async def profile_id_exists(profile_id: int) -> bool:
    """One pi_profile_ids search (limit 1): True when RePORTER lists a project naming
    that profile id. The manager pin form's check of a typed id (spec 2026-10-05 §6.1)."""
    criteria = {"pi_profile_ids": [profile_id]}
    _check_criteria(criteria, PROJECTS_CRITERIA, "projects/search")
    async with httpx.AsyncClient(timeout=30) as client:
        data = await _post(client, "projects/search", {
            "criteria": criteria, "include_fields": ["PrincipalInvestigators"],
            "offset": 0, "limit": 1,
        })
    for row in data.get("results") or []:
        if any(p.get("profile_id") == profile_id for p in row.get("principal_investigators") or []):
            return True
    return False
