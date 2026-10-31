"""The NIH RePORTER grants source: every award of the §5.1 RePORTER organizations in one
fiscal year (spec 2026-10-30 §6.1), grouped by core project and mapped to `DocumentRecord`s.

RePORTER is reached only through `nih_reporter.search_projects`, which pages by offset at
its own 500-row page, paces with that module's pacer, and refuses a reported total over
`max_total`.
"""
from __future__ import annotations

from datetime import date

import httpx

from src.landscaping.institutions import REPORTER_ORGS
from src.landscaping.sources import DocumentRecord, SliceResult
from src.services import nih_reporter

#: The extra fields §6.1 adds to the shared RePORTER field list (`PhrText` and `Terms` are
#: already in it). RePORTER returns `appl_id`, the §6.1 upsert key, only when `ApplId` is
#: asked for (checked live 2026-10-31).
EXTRA_FIELDS = ["AbstractText", "PrefTerms", "ApplId"]
#: The ceiling on one fiscal year's total (≈1.7k a year, §6.1); above it `search_projects`
#: raises `ReporterFirehoseError` rather than return a filter that did not bite.
MAX_TOTAL = 5000

_ORGS = frozenset(o.upper() for o in REPORTER_ORGS)


def _day(value: object) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _pis(row: dict) -> list[dict]:
    return [p for p in row.get("principal_investigators") or [] if isinstance(p, dict)]


def _pi_display_name(pi: dict) -> str:
    full = pi.get("full_name")
    if not full:
        full = " ".join(
            str(pi.get(k) or "") for k in ("first_name", "middle_name", "last_name")
        )
    return " ".join(str(full).split())


def _senior(row: dict) -> list[dict]:
    """The contact PI (else the first PI listed) as a §5.2 senior name; RePORTER has no
    ORCID, so the lab key comes from the profile id."""
    pis = _pis(row)
    if not pis:
        return []
    pi = next((p for p in pis if p.get("is_contact_pi") is True), pis[0])
    profile_id = pi.get("profile_id")
    return [{
        "name": _pi_display_name(pi),
        "orcid": None,
        "openalex_author_id": None,
        "nih_pi_profile_id": str(profile_id) if profile_id is not None else None,
    }]


def _primary(rows: list[dict]) -> dict:
    """The row that speaks for a core project: the parent award (no subproject id) with the
    largest amount, else the largest subproject."""
    return min(rows, key=lambda r: (bool(r.get("subproject_id")),
                                    -int(r.get("award_amount") or 0)))


def to_document(rows: list[dict]) -> DocumentRecord:
    """One core project's RePORTER rows of a fiscal year (at least one) as a grant record.

    Text, PIs and `appl_id` come from the primary row (`_primary`); `extra` keeps every
    row's application ids and project numbers, the earliest start and latest end, and the
    summed award amount, for the §5.3 grant window and the dossier. `role_basis` is
    ``"grant_pi"`` when the organization is a §5.1 RePORTER name, else None."""
    row = _primary(rows)
    core = row.get("core_project_num")
    ids = {"core_project": str(core)} if core else {}
    if row.get("appl_id") is not None:
        ids["appl_id"] = str(row["appl_id"])
    summary = row.get("abstract_text") or row.get("phr_text") or None
    org = ((row.get("organization") or {}).get("org_name") or "").strip()
    starts = [d for d in (_day(r.get("project_start_date")) for r in rows) if d]
    ends = [d for d in (_day(r.get("project_end_date")) for r in rows) if d]
    start = min(starts) if starts else None
    end = max(ends) if ends else None
    return DocumentRecord(
        kind="grant",
        ids=ids,
        title=" ".join(str(row.get("project_title") or "").split()),
        summary=summary,
        published=start,
        doc_type="grant",
        language=None,
        license=None,
        authorships=None,
        role_basis="grant_pi" if org.upper() in _ORGS else None,
        raw_qualifier={"org_name": org},
        summary_source="reporter" if summary else None,
        senior_names=_senior(row),
        extra={
            "appl_ids": [str(r["appl_id"]) for r in rows if r.get("appl_id") is not None],
            "project_nums": [r.get("project_num") for r in rows if r.get("project_num")],
            "fiscal_year": row.get("fiscal_year"),
            "project_start": start.isoformat() if start else None,
            "project_end": end.isoformat() if end else None,
            "award_amount": sum(int(r.get("award_amount") or 0) for r in rows),
            "activity_code": row.get("activity_code"),
            "agency_ic": (row.get("agency_ic_admin") or {}).get("code"),
            "funding_mechanism": row.get("funding_mechanism"),
            "is_active": row.get("is_active"),
            "terms": row.get("terms"),
            "pref_terms": row.get("pref_terms"),
            "phr_text": row.get("phr_text"),
            "principal_investigators": [
                {"name": _pi_display_name(p), "profile_id": p.get("profile_id"),
                 "is_contact_pi": p.get("is_contact_pi")} for p in _pis(row)
            ],
        },
    )


async def fetch_fiscal_year(fy: int) -> SliceResult:
    """Every award of the §5.1 RePORTER organizations in fiscal year `fy`, one record per
    core project (a row without one stands alone, keyed by its project number or appl_id).

    `received` counts RePORTER rows, not grouped records; `expected` is RePORTER's own
    `meta.total` (`_reported_total`), so a short page shows in the completeness check
    (spec §12.4). RAISES `ReporterFirehoseError` over `MAX_TOTAL`, and on any HTTP
    failure."""
    criteria = {"org_names_exact_match": list(REPORTER_ORGS), "fiscal_years": [fy]}
    expected = await _reported_total(criteria)
    rows = await nih_reporter.search_projects(
        criteria, nih_reporter.PROJECT_FIELDS + EXTRA_FIELDS, max_total=MAX_TOTAL,
    )
    groups: dict[str, list[dict]] = {}
    for i, row in enumerate(rows):
        key = (row.get("core_project_num") or row.get("project_num")
               or (f"appl:{row['appl_id']}" if row.get("appl_id") is not None else f"#{i}"))
        groups.setdefault(str(key), []).append(row)
    docs = [to_document(group) for group in groups.values()]
    return SliceResult(rows=docs, expected=expected, received=len(rows))


async def _reported_total(criteria: dict) -> int:
    """RePORTER's `meta.total` for `criteria`, read with one 1-row request through the
    client module's paced `_post`: `search_projects` reads the total but returns only rows."""
    async with httpx.AsyncClient(timeout=30) as client:
        data = await nih_reporter._post(client, "projects/search", {
            "criteria": criteria, "include_fields": ["ApplId"], "offset": 0, "limit": 1,
        })
    return int(data.get("meta", {}).get("total", 0))
