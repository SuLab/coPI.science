"""ORCID API client — fetch profile and works (fundings: `orcid_fundings`)."""

import logging
from typing import Any

import httpx

from src.services.http_pacing import TRANSIENT_STATUSES, with_retries

logger = logging.getLogger(__name__)

ORCID_API_BASE = "https://pub.orcid.org/v3.0"


async def fetch_orcid_record(orcid_id: str) -> dict[str, Any]:
    """Fetch full ORCID record for a given ORCID ID."""
    url = f"{ORCID_API_BASE}/{orcid_id}/record"
    headers = {"Accept": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await with_retries(
            lambda: client.get(url, headers=headers),
            attempts=3, backoff=lambda a: 1.0 * (2 ** a),
            retry_statuses=TRANSIENT_STATUSES, retry_exceptions=(httpx.TransportError,),
        )
        resp.raise_for_status()
        return resp.json()


async def fetch_orcid_profile(orcid_id: str) -> dict[str, Any]:
    """Extract name, affiliation, and email from ORCID record."""
    record = await fetch_orcid_record(orcid_id)
    result: dict[str, Any] = {"orcid": orcid_id}

    # ORCID emits a present-but-null value for an absent part (a single-name
    # researcher has "family-name": null), and ``.get(k, {})`` defaults only an
    # ABSENT key, so every chain here is ``(x.get(k) or {})`` — the same fix
    # fetch_orcid_works carries (D11/item 6).
    person = record.get("person") or {}

    # Name
    name_block = person.get("name") or {}
    given = (name_block.get("given-names") or {}).get("value") or ""
    family = (name_block.get("family-name") or {}).get("value") or ""
    result["name"] = f"{given} {family}".strip() or orcid_id

    # Email (first public email)
    emails = (person.get("emails") or {}).get("email") or []
    for e in emails:
        if e.get("primary") or not result.get("email"):
            result["email"] = e.get("email")

    # Current employment (affiliation) — prefer primary (lowest display-index)
    employments = (
        ((record.get("activities-summary") or {}).get("employments") or {})
        .get("affiliation-group") or []
    )
    # Full employment list (org, start year, current) — the tenure derivation
    # in src/services/jhu_rules.py needs every stint, ended ones included.
    all_employments: list[dict[str, Any]] = []
    current_employments: list[dict[str, Any]] = []
    for grp in employments:
        summaries_list = grp.get("summaries") or []
        if summaries_list:
            emp = summaries_list[0].get("employment-summary") or {}
            start_year = None
            year_val = ((emp.get("start-date") or {}).get("year") or {}).get("value")
            if year_val:
                try:
                    start_year = int(year_val)
                except (TypeError, ValueError):
                    start_year = None
            all_employments.append(
                {
                    "organization": (emp.get("organization") or {}).get("name"),
                    "start_year": start_year,
                    "current": emp.get("end-date") is None,
                }
            )
        for summaries in summaries_list:
            emp = summaries.get("employment-summary") or {}
            if emp.get("end-date") is None:  # Current employment
                current_employments.append(emp)
                break  # one per group
    # Sort by display-index ascending: 0 = primary/preferred position
    current_employments.sort(
        key=lambda e: 999 if e.get("display-index") is None else int(e["display-index"])
    )
    if current_employments:
        emp = current_employments[0]
        org = emp.get("organization") or {}
        result["institution"] = org.get("name")
        dept = emp.get("department-name")
        if dept:
            result["department"] = dept
    result["employments"] = all_employments

    # Researcher URLs (lab website)
    urls = (person.get("researcher-urls") or {}).get("researcher-url") or []
    for u in urls:
        result["lab_website"] = (u.get("url") or {}).get("value")
        break

    return result


# Statuses the public API returns about the state of a RECORD, which a retry
# would get back identically. 301 and 409 (locked) are documented in ORCID's
# api_errors.md; 409 for a deactivated record was measured on 2026-09-25
# (error-code 9044); 410 is HTTP's own "gone".
_RECORD_STATE_STATUSES = frozenset({301, 404, 409, 410})


async def fetch_orcid_works(
    orcid_id: str, *, strict: bool = False
) -> list[dict[str, Any]]:
    """Return list of works (publications) from ORCID, with PMIDs/DOIs.

    ``strict`` decides what a failed request means:

    * ``strict=False`` (the default, for ingest scripts): any failure is logged
      and reads as ``[]`` — indistinguishable from a PI with no works.
    * ``strict=True`` (``resolve_corpus``): a status that is ORCID's answer
      about the RECORD reads as ``[]``, as does an empty works list —
      ``_RECORD_STATE_STATUSES``: 404 (no such record), 409 (deactivated or
      locked), 410 (gone) and 301 (deprecated into another iD; not followed).
      Each repeats on every retry, so raising would dead-letter every
      regeneration of that PI. Every other failure — a transport error or
      timeout, a 429, a 5xx, any other status, an unreadable body — RAISES.
      Swallowing it would thin the corpus, and a paper-derived tenure start
      could then be stored from the thinner one; the job retry is the
      recovery.
    """
    url = f"{ORCID_API_BASE}/{orcid_id}/works"
    headers = {"Accept": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await with_retries(
                lambda: client.get(url, headers=headers),
                attempts=3, backoff=lambda a: 1.0 * (2 ** a),
                retry_statuses=TRANSIENT_STATUSES, retry_exceptions=(httpx.TransportError,),
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            if not strict:
                logger.warning("Failed to fetch ORCID works for %s: %s", orcid_id, exc)
                return []
            if (
                isinstance(exc, httpx.HTTPStatusError)
                and exc.response.status_code in _RECORD_STATE_STATUSES
            ):
                logger.warning(
                    "ORCID answered %d for %s's works (record not readable); "
                    "reading it as no works",
                    exc.response.status_code, orcid_id,
                )
                return []
            raise

    works = []
    for grp in data.get("group", []):
        for summary in grp.get("work-summary", []):
            # D11/item 6: ORCID emits ``"external-ids": null`` (and the same
            # for "title"/"publication-date") for some work-summaries — 15 of
            # 155 for 0000-0003-3474-019X. ``dict.get(k, {})`` only supplies
            # its default when the key is ABSENT, not when it is present and
            # None, so a chained `.get(k, {}).get(...)` raised AttributeError
            # on those rows, escaped the try/except below (which wraps only
            # the HTTP call), and turned into a CorpusStageError that killed
            # the whole `generate_profile` job after 3 retries. `(X.get(k) or
            # {})` treats absent and null identically, which is what every
            # chain below now does.
            title_block = summary.get("title") or {}
            work: dict[str, Any] = {
                "title": (title_block.get("title") or {}).get("value", ""),
                "year": None,
                "pmid": None,
                "doi": None,
                "type": summary.get("type"),
            }
            # Publication year
            pub_date = summary.get("publication-date") or {}
            year_block = pub_date.get("year") or {}
            if year_block.get("value"):
                work["year"] = int(year_block["value"])

            # External IDs
            ext_ids = (summary.get("external-ids") or {}).get("external-id") or []
            for eid in ext_ids:
                id_type = eid.get("external-id-type", "").lower()
                id_value = eid.get("external-id-value", "")
                if id_type == "pmid":
                    work["pmid"] = id_value
                elif id_type == "doi":
                    work["doi"] = id_value

            works.append(work)
    return works
