"""OpenAlex works-by-ORCID client (corpus stage S2).

OpenAlex is often the largest discovery source for sparse-ORCID PIs (the
2026-08-13 rehearsal: gill had 0 ORCID works vs 33 via OpenAlex) — exactly the
new-PI onboarding case. Its identity is NOT trusted here: the rehearsal's one
OpenAlex error linked a stranger's paper to a PI's ORCID, so every S2 candidate
PubMed has goes through the resolver's author-match gate. The works PubMed lacks
are the exception: for a PubMed-thin PI they are stored without staff review (owner
decision D6, spec 2026-10-09 §5), but only when the ORCID's own authorship on the work
names one of the PI's institutions or the publisher's metadata carries the ORCID
(``fetch_orcid_authorships``; D6 amended after the 2026-10-09 rollout). This module
fetches each work's identifiers and year, plus the title, type, venue, abstract and
retraction flag that non-PubMed corpus reads, and offers the free single-DOI lookup
(``fetch_work_by_doi``). It RAISES on failure (a silent empty result is a thin corpus, not
an answer — audit M5).
"""

import logging
import re
from urllib.parse import quote

import httpx

from src.config import get_settings
from src.services import openalex_budget
from src.services.http_pacing import TRANSIENT_STATUSES, with_retries
from src.services.identifiers import normalize_orcid, normalize_pmid
from src.services.openalex_auth import openalex_headers
from src.services.query_escaping import openalex_filter_value

logger = logging.getLogger(__name__)

OPENALEX_WORKS_URL = "https://api.openalex.org/works"
_PER_PAGE = 200
_MAX_PAGES = 5  # 1,000 works — far past any realistic single-PI corpus


def _make_client() -> httpx.AsyncClient:
    """Client factory — a seam so tests can inject httpx.MockTransport."""
    return httpx.AsyncClient(timeout=60, follow_redirects=True, headers=openalex_headers())


def _pmid_from_url(url: str | None) -> str | None:
    """The PMID of OpenAlex's ``https://pubmed.ncbi.nlm.nih.gov/<n>`` form, canonical
    (``identifiers.normalize_pmid``), or None (R2.5)."""
    return normalize_pmid(url)


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
#: What ``fetch_orcid_authorships`` selects. A list response caps ``authorships`` at 100
#: per work (measured 2026-10-09), and the request costs one credit per page.
AUTHORSHIP_FIELDS = "ids,authorships"
#: Kept from one authorship, each cut to _AFFILIATION_MAX characters: the strings are only
#: compared with the PI's institutions, never stored or shown.
_MAX_AFFILIATIONS = 10
_AFFILIATION_MAX = 300


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


def _orcid_filter(orcid: str) -> str:
    return f"author.orcid:https://orcid.org/{openalex_filter_value(orcid)}"


async def _list_works(filter_: str, select: str) -> list[dict]:
    """Every work a list filter matches, as OpenAlex returns it (the ``select`` fields),
    cursor-paginated up to ``_MAX_PAGES``; includes a ``mailto`` (OpenAlex's polite pool)
    when a contact email is configured. Each page is a charged request, so the request
    budget is checked before every attempt."""
    settings = get_settings()
    out: list[dict] = []
    cursor = "*"
    async with _make_client() as client:
        for _ in range(_MAX_PAGES):
            params = {
                "filter": filter_,
                "per-page": str(_PER_PAGE),
                "cursor": cursor,
                "select": select,
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
            out.extend(data.get("results", []))
            cursor = (data.get("meta") or {}).get("next_cursor")
            if not cursor:
                break
    return out


async def fetch_works_by_orcid(orcid: str) -> list[dict]:
    """All OpenAlex works for an ORCID, each as ``_work_record`` shapes it."""
    return [_work_record(work) for work in await _list_works(_orcid_filter(orcid), WORK_FIELDS)]


def _orcid_key(value: object) -> str | None:
    """An ORCID iD, bare or as an orcid.org URL, in canonical form; None when it is not
    text or fails the check digit (``identifiers.normalize_orcid``, R2.2)."""
    return normalize_orcid(value) if isinstance(value, str) else None


def _texts(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    return [v[:_AFFILIATION_MAX] for v in values if isinstance(v, str) and v.strip()][
        :_MAX_AFFILIATIONS
    ]


def _authorship_of(work: dict, orcid: str) -> dict | None:
    """The authorship OpenAlex assigned to ``orcid`` on ``work``, or None when its author list
    has none (a list response keeps the first 100 authors only)."""
    want = _orcid_key(orcid)
    if want is None:
        return None
    for authorship in work.get("authorships") or []:
        author = authorship.get("author") if isinstance(authorship, dict) else None
        if not isinstance(author, dict) or _orcid_key(author.get("orcid")) != want:
            continue
        institutions = authorship.get("institutions") or []
        return {
            "institutions": _texts([
                inst.get("display_name") for inst in institutions if isinstance(inst, dict)
            ]),
            "raw_affiliations": _texts(authorship.get("raw_affiliation_strings")),
            "orcid_in_metadata": _orcid_key(authorship.get("raw_orcid")) == want,
        }
    return None


async def fetch_orcid_authorships(orcid: str) -> dict[str, dict | None]:
    """The ORCID's own authorship on each PMID-less work OpenAlex attributes to it, by DOI
    (``ids.doi`` without the resolver prefix; a work with no DOI is left out):
    ``institutions`` (OpenAlex's names for them), ``raw_affiliations`` (as printed) and
    ``orcid_in_metadata`` (the publisher's metadata carries the ORCID, so the work is not
    only OpenAlex's assignment). None for a work whose author list, capped at 100 in a list
    response, lacks that authorship. One charged request per page; RAISES on failure."""
    out: dict[str, dict | None] = {}
    works = await _list_works(f"{_orcid_filter(orcid)},has_pmid:false", AUTHORSHIP_FIELDS)
    for work in works:
        doi = _doi_from_url((work.get("ids") or {}).get("doi"))
        if doi:
            out.setdefault(doi, _authorship_of(work, orcid))
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
