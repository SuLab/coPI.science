"""ORCID API client — fetch profile and works (fundings: `orcid_fundings`)."""

import logging
from typing import Any

import httpx

from src.services.http_pacing import TRANSIENT_STATUSES, with_retries
from src.services.identifiers import is_preprint_doi, normalize_doi, normalize_pmid

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


#: The ORCID record's affiliation sections that name places a PI publishes from, with each
#: section's summary key.
_AFFILIATION_SECTIONS = (
    ("employments", "employment-summary"),
    ("educations", "education-summary"),
    ("qualifications", "qualification-summary"),
    ("invited-positions", "invited-position-summary"),
)


def _organizations(record: dict[str, Any]) -> list[str]:
    """Each organization name the record lists under employments, educations,
    qualifications and invited positions, once, in record order. The non-PubMed corpus
    matches OpenAlex affiliations against them (``non_pubmed_works._attributed``)."""
    activities = record.get("activities-summary") or {}
    names: list[str] = []
    for section, summary_key in _AFFILIATION_SECTIONS:
        for group in (activities.get(section) or {}).get("affiliation-group") or []:
            if not isinstance(group, dict):
                continue
            for summary in group.get("summaries") or []:
                entry = summary.get(summary_key) if isinstance(summary, dict) else None
                organization = entry.get("organization") if isinstance(entry, dict) else None
                name = organization.get("name") if isinstance(organization, dict) else None
                if isinstance(name, str) and name.strip() and name.strip() not in names:
                    names.append(name.strip())
    return names


async def fetch_orcid_profile(orcid_id: str) -> dict[str, Any]:
    """Extract name, affiliation, and email from ORCID record.

    Also every employment, and the record's organizations (``_organizations``). ``name`` is
    None when the record has no public name (spec 2026-10-05 §6.3): callers fall back to the
    OAuth name or ask for one, never to the iD."""
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
    result["name"] = f"{given} {family}".strip() or None

    # Email (first public email)
    emails = (person.get("emails") or {}).get("email") or []
    for e in emails:
        if not isinstance(e, dict):
            continue
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
        key=lambda e: _display_index(e.get("display-index"))
    )
    if current_employments:
        emp = current_employments[0]
        org = emp.get("organization") or {}
        result["institution"] = org.get("name")
        dept = emp.get("department-name")
        if dept:
            result["department"] = dept
    result["employments"] = all_employments
    result["organizations"] = _organizations(record)

    # Researcher URLs (lab website)
    urls = (person.get("researcher-urls") or {}).get("researcher-url") or []
    for u in urls:
        result["lab_website"] = (u.get("url") or {}).get("value")
        break

    return result


def _display_index(value: Any) -> int:
    """An employment's ORCID display-index for sorting; 999 (last) when absent or
    non-numeric."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 999


# Statuses the public API returns about the state of a RECORD, which a retry
# would get back identically. 301 and 409 (locked) are documented in ORCID's
# api_errors.md; 409 for a deactivated record was measured on 2026-09-25
# (error-code 9044); 410 is HTTP's own "gone".
_RECORD_STATE_STATUSES = frozenset({301, 404, 409, 410})


class OrcidResponseError(ValueError):
    """ORCID answered 200 with a body that is not a works listing"""


def _listing_problem(data: object) -> str | None:
    """Why a 200 works body is not a listing (R2.4, Q22), or None when it is one: a dict
    whose "group" is a list or null."""
    if not isinstance(data, dict):
        return f"top level is {type(data).__name__}"
    if "group" not in data:
        return 'no "group" key'
    if not isinstance(data["group"], list | None):
        return f'"group" is {type(data["group"]).__name__}'
    return None


def _work_ids(ext_ids: object, put_code: object) -> tuple[str | None, str | None]:
    """(doi, pmid) of one work summary's ``external-id`` list (R2.5; main ORC-2, ORC-3).

    Only ``self`` ids count (a missing relationship is ``self``); a ``version-of`` or
    ``part-of`` DOI is never the work's. The DOI is the first self DOI that is not a
    preprint's (``is_preprint_doi``), else the first self DOI; the PMID is the first self
    value ``normalize_pmid`` accepts. Values are normalised, so a labelled PMID reaches
    EFetch canonical and a non-DOI ("N/A") is never a DOI; each rejected value is logged
    at DEBUG with the put-code."""
    if not isinstance(ext_ids, list):
        return None, None
    dois: list[str] = []
    seen: set[str] = set()
    pmid: str | None = None
    for eid in ext_ids:
        if not isinstance(eid, dict):
            continue
        if (eid.get("external-id-relationship") or "self").lower() != "self":
            continue
        id_type = (eid.get("external-id-type") or "").lower()
        value = eid.get("external-id-value")
        if id_type == "doi":
            doi = normalize_doi(value)
            if doi is None:
                logger.debug("ORCID work put-code %s: DOI %r is not a DOI; ignored", put_code, value)
            elif doi.casefold() not in seen:
                seen.add(doi.casefold())
                dois.append(doi)
        elif id_type == "pmid" and pmid is None:
            pmid = normalize_pmid(value)
            if pmid is None:
                logger.debug("ORCID work put-code %s: PMID %r is not a PMID; ignored", put_code, value)
    doi = next((d for d in dois if not is_preprint_doi(d)), dois[0] if dois else None)
    return doi, pmid


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

    A 200 whose body is not a works listing (not a dict, no ``group`` key, or a ``group``
    that is neither a list nor null) raises ``OrcidResponseError`` in strict mode, which
    ``resolve_corpus``'s ``_stage`` turns into ``CorpusStageError`` (Q22), and reads as
    ``[]`` with one WARNING otherwise. Each work's ``doi`` and ``pmid`` come from
    ``_work_ids``.
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

    problem = _listing_problem(data)
    if problem is not None:
        if strict:
            raise OrcidResponseError(
                f"ORCID answered 200 for {orcid_id}'s works with a body that is not a "
                f"works listing ({problem})"
            )
        logger.warning("ORCID works for %s: %s; reading it as no works", orcid_id, problem)
        return []
    works = []
    for grp in data.get("group") or []:
        if not isinstance(grp, dict):
            continue
        for summary in grp.get("work-summary") or []:
            if not isinstance(summary, dict):
                continue
            # D11/item 6: ORCID emits ``"external-ids": null`` (and the same
            # for "title"/"publication-date") for some work-summaries — 15 of
            # 155 for 0000-0003-3474-019X. ``dict.get(k, {})`` only supplies
            # its default when the key is ABSENT, not when it is present and
            # None, so a chained `.get(k, {}).get(...)` raised AttributeError
            # on those rows, escaped the try/except below (which wraps only
            # the HTTP call), and turned into a CorpusStageError that killed
            # the whole `generate_profile` job after 3 retries. `(X.get(k) or
            # {})` treats absent and null identically, which is what every
            # chain below now does; the same holds for a null group list,
            # summary, external-id entry, id type or title value, and a
            # non-numeric year is skipped (G-20, spec 2026-10-05).
            title_block = summary.get("title") or {}
            work: dict[str, Any] = {
                "title": (title_block.get("title") or {}).get("value") or "",
                "year": None,
                "pmid": None,
                "doi": None,
                "type": summary.get("type"),
                "journal": (summary.get("journal-title") or {}).get("value") or None,
            }
            # Publication year
            pub_date = summary.get("publication-date") or {}
            year_block = pub_date.get("year") or {}
            if year_block.get("value"):
                try:
                    work["year"] = int(year_block["value"])
                except (TypeError, ValueError):
                    pass

            # External IDs (R2.5): relationship-aware and normalised.
            work["doi"], work["pmid"] = _work_ids(
                (summary.get("external-ids") or {}).get("external-id"), summary.get("put-code")
            )

            works.append(work)
    return works
