"""OpenAlex works-by-ORCID client (corpus stage S2).

OpenAlex is often the largest discovery source for sparse-ORCID PIs (the
2026-08-13 rehearsal: gill had 0 ORCID works vs 33 via OpenAlex) — exactly the
new-PI onboarding case. Its identity is NOT trusted here: the rehearsal's one
OpenAlex error linked a stranger's paper to a PI's ORCID, so every S2 candidate
PubMed has goes through the resolver's author-match gate. The works PubMed lacks
are the exception: for a PubMed-thin PI they are stored without a gate (owner
decision D6, spec 2026-10-09 §5). This module fetches each work's identifiers and
year, plus the title, type, venue, abstract and retraction flag that non-PubMed
corpus reads, and offers the free single-DOI lookup (``fetch_work_by_doi``). It
RAISES on failure (a silent empty result is a thin corpus, not an answer — audit
M5).
"""

import logging
import re
from urllib.parse import quote

import httpx

from src.config import get_settings
from src.services import openalex_budget
from src.services.http_pacing import TRANSIENT_STATUSES, with_retries
from src.services.query_escaping import openalex_filter_value

logger = logging.getLogger(__name__)

OPENALEX_WORKS_URL = "https://api.openalex.org/works"
_PER_PAGE = 200
_MAX_PAGES = 5  # 1,000 works — far past any realistic single-PI corpus


def _make_client() -> httpx.AsyncClient:
    """Client factory — a seam so tests can inject httpx.MockTransport."""
    return httpx.AsyncClient(timeout=60, follow_redirects=True)


def _pmid_from_url(url: str | None) -> str | None:
    if not url:
        return None
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return tail or None


def _doi_from_url(url: str | None) -> str | None:
    if not url:
        return None
    doi = url.split("doi.org/", 1)[-1].strip()
    return doi or None


#: What the S2 list and the DOI lookup select (spec 2026-10-09 §5.2): identifiers and year
#: for the PubMed path, plus title, type, venue, abstract and retraction for the non-PubMed
#: corpus. Free to widen: a list request costs one credit whatever it selects, and a
#: singleton lookup costs none.
WORK_FIELDS = (
    "ids,publication_year,title,type,primary_location,abstract_inverted_index,is_retracted"
)


def reconstruct_abstract(inverted_index: object) -> str | None:
    """OpenAlex's ``abstract_inverted_index`` ({word: [positions]}) as text: each word placed
    at each of its non-negative integer positions, gaps closed. None when absent, empty or
    malformed throughout."""
    if not isinstance(inverted_index, dict):
        return None
    placed: dict[int, str] = {}
    for word, positions in inverted_index.items():
        if not isinstance(word, str) or not isinstance(positions, list):
            continue
        for pos in positions:
            if isinstance(pos, int) and not isinstance(pos, bool) and pos >= 0:
                placed[pos] = word
    return " ".join(placed[i] for i in sorted(placed)) or None


def _one_line(value: object) -> str | None:
    """Text collapsed to single spaces, so no newline reaches a persona or a prompt line;
    None when blank or not text."""
    if not isinstance(value, str):
        return None
    return " ".join(value.split()) or None


def _venue(work: dict) -> str | None:
    source = (work.get("primary_location") or {}).get("source") or {}
    return _one_line(source.get("display_name"))


def _work_record(work: dict) -> dict:
    """One OpenAlex work as the corpus reads it. Title and venue are one line each:
    whoever registered a DOI wrote them, and they reach prompt text."""
    ids = work.get("ids") or {}
    return {
        "pmid": _pmid_from_url(ids.get("pmid")),
        "doi": _doi_from_url(ids.get("doi")),
        "year": work.get("publication_year"),
        "title": _one_line(work.get("title")) or "",
        "type": work.get("type"),
        "venue": _venue(work),
        "abstract": reconstruct_abstract(work.get("abstract_inverted_index")),
        "is_retracted": work.get("is_retracted") is True,
    }


async def fetch_works_by_orcid(orcid: str) -> list[dict]:
    """All OpenAlex works for an ORCID, each as ``_work_record`` shapes it.

    Cursor-paginated; includes a ``mailto`` (OpenAlex's polite pool) when a
    contact email is configured.
    """
    settings = get_settings()
    out: list[dict] = []
    cursor = "*"
    async with _make_client() as client:
        for _ in range(_MAX_PAGES):
            params = {
                "filter": f"author.orcid:https://orcid.org/{openalex_filter_value(orcid)}",
                "per-page": str(_PER_PAGE),
                "cursor": cursor,
                "select": WORK_FIELDS,
            }
            contact = getattr(settings, "ncbi_contact_email", None)
            if contact:
                params["mailto"] = contact
            async def attempt(page_params=params):
                await openalex_budget.check_request_budget()
                return await client.get(OPENALEX_WORKS_URL, params=page_params)

            resp = await with_retries(
                attempt,
                attempts=3, backoff=lambda a: 1.0 * (2 ** a),
                retry_statuses=TRANSIENT_STATUSES, retry_exceptions=(httpx.TransportError,),
            )
            resp.raise_for_status()
            data = resp.json()
            for work in data.get("results", []):
                out.append(_work_record(work))
            cursor = (data.get("meta") or {}).get("next_cursor")
            if not cursor:
                break
    return out


#: A DOI the lookup will send: "10.<registrant>[.<sub>]/<suffix>", no whitespace. A path
#: segment of "." or ".." is refused as well, so a crafted DOI cannot walk to another
#: OpenAlex path (security review 2026-10-09).
_DOI_RE = re.compile(r"10\.\d{4,9}(?:\.\d+)*/\S+")
#: Answers that mean "no such work" rather than a failure: an unknown DOI (404), one
#: OpenAlex will not parse (400), one withdrawn (410).
_NO_SUCH_WORK = frozenset({400, 404, 410})


def _lookup_doi_ok(doi: str) -> bool:
    return bool(_DOI_RE.fullmatch(doi)) and not any(
        segment in (".", "..") for segment in doi.split("/")
    )


async def fetch_work_by_doi(
    doi: str, *, attempts: int = 3, timeout: float = 60.0,
) -> dict | None:
    """One OpenAlex work by DOI (``GET /works/doi:<doi>``) as ``_work_record`` shapes it, or
    None when the DOI is malformed (no request is made) or OpenAlex has no such work (400,
    404, 410). A singleton lookup is free on the meter (measured 2026-10-09:
    ``x-ratelimit-cost-usd: 0``), so the request budget is not consulted. Transient statuses
    and transport errors are retried up to ``attempts`` times, each request bounded by
    ``timeout`` seconds; any other failure RAISES."""
    if not _lookup_doi_ok(doi):
        return None
    settings = get_settings()
    params = {"select": WORK_FIELDS}
    contact = getattr(settings, "ncbi_contact_email", None)
    if contact:
        params["mailto"] = contact
    url = f"{OPENALEX_WORKS_URL}/doi:{quote(doi, safe='/')}"
    async with _make_client() as client:
        resp = await with_retries(
            lambda: client.get(url, params=params, timeout=timeout),
            attempts=attempts, backoff=lambda a: 1.0 * (2 ** a),
            retry_statuses=TRANSIENT_STATUSES, retry_exceptions=(httpx.TransportError,),
        )
        if resp.status_code in _NO_SUCH_WORK:
            return None
        resp.raise_for_status()
        body = resp.json()
    return _work_record(body) if isinstance(body, dict) else None
