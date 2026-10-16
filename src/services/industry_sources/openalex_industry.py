"""OpenAlex-derived industry evidence: company co-authors and company funders."""
import logging
from functools import partial
from urllib.parse import quote

import httpx

from src.config import get_settings
from src.services import openalex_budget
from src.services.http_pacing import TRANSIENT_STATUSES, Pacer, with_retries
from src.services.identifiers import normalize_orcid
from src.services.industry_sources import JHU_OPENALEX_IDS, EvidenceItem, Paged
from src.services.industry_sources.companies import classify_company
from src.services.jhu_rules import is_hopkins_affiliation
from src.services.openalex import _NO_SUCH_WORK, _lookup_doi_ok
from src.services.openalex_auth import openalex_headers
from src.services.person_names import PersonName, given_names_agree, name_key, parse_person_name
from src.services.query_escaping import openalex_filter_value

logger = logging.getLogger(__name__)
OA = "https://api.openalex.org"
_SELECT = "id,ids,title,publication_year,authorships,funders"

# Every request here once went out unpaced and unretried, so one 429 failed the job
# outright; 52 jobs died that way on 2026-09-22/25. OpenAlex answers 429 above 100
# requests/second or once the day's budget is spent (keyless: 1000 credits/day per IP,
# x-ratelimit-limit); help.openalex.org asks for exponential backoff. The pacer keeps one
# worker far below the per-second limit; the backoff rides out a burst. A spent daily
# budget still answers 429 after the retries: the source is then unavailable for that run
# (`registry.OpenAlexSource` raises SourceUnavailable), so industry_evidence keeps the PI's
# stored OpenAlex rows, records `unavailable:http_429` in the score row's coverage and
# refreshes the other sources (spec 2026-10-05 §6.2).
_PACER = Pacer(0.2)


async def _get(client: httpx.AsyncClient, url: str, params: dict) -> httpx.Response:
    async def attempt():
        await openalex_budget.check_request_budget()
        return await client.get(url, params=params)

    resp = await with_retries(
        attempt,
        attempts=4, backoff=lambda a: 2.0 * (2 ** a),
        retry_statuses=TRANSIENT_STATUSES, retry_exceptions=(httpx.TransportError,),
        pacer=_PACER,
    )
    resp.raise_for_status()
    return resp


def _oaid(url: str | None) -> str:
    return (url or "").rsplit("/", 1)[-1]


#: Seniority of a `_role`, most senior first; any other role ranks last.
_ROLE_RANK = {"corresponding": 0, "last": 1, "first": 2}


def _printed_as(raw_name: str | None, pi: PersonName) -> int:
    """How strongly an authorship's printed name is the PI's (P15): 0 when the surname keys
    do not intersect or the first given names disagree; 2 when they agree and both given
    names are spelled out; 1 when they agree on an initial only."""
    raw = parse_person_name(raw_name or "")
    if not (raw.surname_keys & pi.surname_keys) or not given_names_agree(pi.first, raw.first):
        return 0
    return 2 if len(name_key(pi.first)) > 1 and len(name_key(raw.first)) > 1 else 1


def _pi_authorship(work: dict, pi_orcid: str, pi_name: str | None = None) -> dict | None:
    """The authorship carrying the PI's ORCID (compared through
    ``identifiers.normalize_orcid``, R2.2). OpenAlex can put one ORCID on several
    authorships of a work (2026-10-11: Erika Pearce's on three of W4318981448's, the first
    printed as "Edward J. Pearce"). Then the most senior of those whose printed name is the
    PI's most strongly (``_printed_as``: a spelled-out given name beats an initial, P15) is
    the PI's; when none agrees, or ``pi_name`` is not given, the first."""
    want = normalize_orcid(pi_orcid)
    if want is None:
        return None
    mine = [a for a in work.get("authorships") or []
            if normalize_orcid((a.get("author") or {}).get("orcid")) == want]
    if len(mine) < 2 or not pi_name:
        return mine[0] if mine else None
    pi = parse_person_name(pi_name)
    scores = [_printed_as(a.get("raw_author_name"), pi) for a in mine]
    best = max(scores)
    if best == 0:
        return mine[0]
    strongest = [a for a, score in zip(mine, scores, strict=True) if score == best]
    return min(strongest, key=lambda a: _ROLE_RANK.get(_role(a), len(_ROLE_RANK)))


def _pi_at_jhu(auth: dict) -> bool:
    if any(_oaid(i.get("id")) in JHU_OPENALEX_IDS for i in auth.get("institutions") or []):
        return True
    return any(is_hopkins_affiliation(s) for s in auth.get("raw_affiliation_strings") or [])


def _role(auth: dict) -> str:
    if auth.get("is_corresponding"):
        return "corresponding"
    return auth.get("author_position") or "middle"


def evidence_from_work(work: dict, pi_orcid: str, tenure_start: int | None, *, company_funder_ids: set[str],
                       pi_name: str | None = None) -> list[EvidenceItem]:
    year = work.get("publication_year")
    if tenure_start is None or not year or year < tenure_start:
        return []
    me = _pi_authorship(work, pi_orcid, pi_name)
    if me is None or not _pi_at_jhu(me):
        return []
    wid = _oaid(work.get("id"))
    n_authors = len(work.get("authorships") or [])
    base = {"work_id": wid, "pmid": _oaid((work.get("ids") or {}).get("pmid")), "title": work.get("title"), "author_count": n_authors}
    items: list[EvidenceItem] = []
    seen: set[str] = set()
    for a in work.get("authorships") or []:
        if a is me:
            continue
        for inst in a.get("institutions") or []:
            if inst.get("type") != "company":
                continue
            cid = _oaid(inst.get("id"))
            if cid in seen:
                continue
            seen.add(cid)
            items.append(EvidenceItem("openalex", "coauthor_company", f"{wid}:{cid}", inst.get("display_name"), cid,
                                      classify_company(inst.get("display_name", ""), "company"), year, _role(me), True, dict(base)))
    for f in work.get("funders") or []:
        fid = _oaid(f.get("id"))
        if fid in company_funder_ids:
            items.append(EvidenceItem("openalex", "company_funder", f"{wid}:{fid}", f.get("display_name"), fid,
                                      classify_company(f.get("display_name", ""), "company"), year, _role(me), True, dict(base)))
    return items


#: PMIDs per /works request: OpenAlex accepts 100 OR'd values in one filter, and a list
#: request costs one credit whatever its size (measured 2026-10-06, spec D67).
_PMID_CHUNK = 100
#: Works per page: a PMID can map to more than one work, so a chunk's page holds
#: twice the chunk (OpenAlex's per-page maximum).
_PMID_PAGE = 200
#: At most this many unreturned in-use PMIDs are looked up by DOI per run, newest first
#: (P14); the rest make the source "truncated".
DOI_FALLBACK_MAX = 50
#: This many consecutive DOI lookups failing the same way is OpenAlex failing, not bad DOIs.
_DOI_FAILURE_RUN = 3


def _doi_failure(exc: BaseException) -> tuple[str, int | str] | None:
    """The signature of a per-DOI failure, or None for one that must raise at once (a 429
    after the retries)."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return None if code == 429 else ("HTTP", code)
    if isinstance(exc, httpx.TransportError):
        return ("transport", type(exc).__name__)
    return ("body", type(exc).__name__)


async def _lookup_doi(client: httpx.AsyncClient, doi: str, params: dict) -> dict | None:
    """One singleton DOI lookup: the work, or None for "no such work" (400, 404, 410) or a
    non-dict body. Raises on any other failure."""
    resp = await with_retries(
        partial(client.get, f"{OA}/works/doi:{quote(doi, safe='/')}", params=params),
        attempts=4, backoff=lambda a: 2.0 * (2 ** a),
        retry_statuses=TRANSIENT_STATUSES, retry_exceptions=(httpx.TransportError,),
        pacer=_PACER,
    )
    if resp.status_code in _NO_SUCH_WORK:
        if resp.status_code == 400:  # also what a request OpenAlex rejects would get
            logger.warning("OpenAlex refused DOI %s (400): %s", doi, resp.text[:200])
        return None
    resp.raise_for_status()
    body = resp.json()
    return body if isinstance(body, dict) else None


async def fetch_works_for_pmids(pmids: list[str]) -> Paged:
    """The OpenAlex works of `pmids`, `_PMID_CHUNK` PMIDs a request, each escaped for the
    filter (`query_escaping.openalex_filter_value`, P29). A PMID can map to more than one
    work, so a chunk can match more than the `_PMID_PAGE` a page holds: when a chunk's
    `meta.count` exceeds the works it returned, the result is `truncated` (spec 2026-10-05
    §6.2), so the job keeps the OpenAlex rows it could not see instead of deleting them."""
    out: list[dict] = []
    truncated = False
    contact = getattr(get_settings(), "ncbi_contact_email", None)
    clean = [v for v in (openalex_filter_value(p) for p in pmids) if v]
    async with httpx.AsyncClient(timeout=30, headers=openalex_headers()) as client:
        for i in range(0, len(clean), _PMID_CHUNK):
            params = {"filter": f"pmid:{'|'.join(clean[i:i + _PMID_CHUNK])}", "select": _SELECT,
                      "per-page": _PMID_PAGE}
            if contact:
                params["mailto"] = contact
            data = (await _get(client, f"{OA}/works", params)).json()
            results = data.get("results") or []
            out.extend(results)
            count = (data.get("meta") or {}).get("count")
            truncated = truncated or (isinstance(count, int) and count > len(results))
    return Paged(out, truncated)


async def fetch_works_for_dois(dois: list[str], *, skipped: list[str] | None = None) -> list[dict]:
    """The OpenAlex works of `dois`, one singleton lookup each (`GET /works/doi:<doi>`): the
    fallback for in-use papers the PMID filter did not return, because OpenAlex can file a
    merged preprint and article under the preprint's PMID (2026-10-10: 216 of 6,263 in-use
    PMIDs unreturned, hiding two PIs' company co-authors). A singleton lookup costs no
    credit, so the request budget is not consulted. A malformed DOI (`openalex`'s check, no
    request made) or an unknown one (400, 404, 410) yields nothing. A DOI whose lookup fails
    (an HTTP error other than 429, a final transport error, an unreadable body) is logged,
    appended to `skipped` and passed over (P14); the `_DOI_FAILURE_RUN`-th consecutive
    failure with the same signature, a 429 after the retries, or any other exception
    raises, so the source is unavailable for the run."""
    out: list[dict] = []
    contact = getattr(get_settings(), "ncbi_contact_email", None)
    params = {"select": _SELECT, **({"mailto": contact} if contact else {})}
    run_sig: tuple[str, int | str] | None = None
    run_len = 0
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=openalex_headers()) as client:
        for doi in dict.fromkeys(d.strip() for d in dois):
            if not _lookup_doi_ok(doi):
                continue
            try:
                work = await _lookup_doi(client, doi, params)
            except (httpx.HTTPStatusError, httpx.TransportError, ValueError) as exc:
                sig = _doi_failure(exc)
                if sig is None:
                    raise
                run_len = run_len + 1 if sig == run_sig else 1
                run_sig = sig
                if run_len >= _DOI_FAILURE_RUN:
                    logger.error(
                        "OpenAlex DOI lookup: %d consecutive DOIs failed %s; treating it as systemic",
                        run_len, f"{sig[0]} {sig[1]}",
                    )
                    raise
                logger.warning(
                    "OpenAlex DOI lookup for %s failed (%s); skipping that DOI",
                    doi, f"{sig[0]} {sig[1]}",
                )
                if skipped is not None:
                    skipped.append(doi)
                continue
            run_sig, run_len = None, 0
            if work is not None:
                out.append(work)
    return out


async def company_funder_ids(funder_ids: set[str]) -> set[str]:
    """Funder ids whose entity also has an ``institution`` role of type company (e.g. GSK).

    One funders request and one institutions request per chunk of 50 funders (DP-12);
    the answer is the same set the per-funder lookup returned."""
    if not funder_ids:
        return set()
    hits: set[str] = set()
    contact = getattr(get_settings(), "ncbi_contact_email", None)
    mailto = {"mailto": contact} if contact else {}
    ordered = sorted(v for v in (openalex_filter_value(f) for f in funder_ids) if v)
    async with httpx.AsyncClient(timeout=30, headers=openalex_headers()) as client:
        for i in range(0, len(ordered), 50):
            chunk = "|".join(ordered[i:i + 50])
            resp = await _get(client, f"{OA}/funders", {
                "filter": f"ids.openalex:{chunk}", "select": "id,display_name,roles", "per-page": 50, **mailto,
            })
            inst_by_funder: dict[str, list[str]] = {}
            for f in resp.json().get("results") or []:
                inst_ids = [_oaid(r.get("id")) for r in f.get("roles") or [] if r.get("role") == "institution"]
                if inst_ids:
                    inst_by_funder[_oaid(f.get("id"))] = inst_ids
            all_insts = sorted(
                {openalex_filter_value(i) for ids in inst_by_funder.values() for i in ids} - {""}
            )
            if not all_insts:
                continue
            companies: set[str] = set()
            for j in range(0, len(all_insts), 50):
                r2 = await _get(client, f"{OA}/institutions", {
                    "filter": f"ids.openalex:{'|'.join(all_insts[j:j + 50])}",
                    "select": "id,type", "per-page": 50, **mailto,
                })
                companies |= {_oaid(i.get("id")) for i in r2.json().get("results") or []
                              if i.get("type") == "company"}
            hits |= {fid for fid, ids in inst_by_funder.items() if companies & set(ids)}
    return hits
