import re
from dataclasses import dataclass

from src.config import get_settings
from src.services.industry_sources import EvidenceItem, Paged
from src.services.industry_sources.companies import classify_company
from src.services.industry_sources.registry import SourceUnavailable
from src.services.odp import ODP_PACER, ODP_SEARCH_URL, odp_client, odp_headers
from src.services.person_names import _ASCII_LETTERS, fold, parse_person_name, strip_accents
from src.services.query_escaping import phrase_term

SEARCH_URL = ODP_SEARCH_URL
_FIELDS = ["applicationNumberText", "applicationMetaData.inventionTitle", "applicationMetaData.filingDate",
           "applicationMetaData.firstInventorName", "applicationMetaData.applicantBag", "assignmentBag"]
# [a-z0-9]+ naturally splits on hyphens ("beta-lactam" -> "beta", "lactam"),
# so no explicit hyphen handling is needed. A 4-char floor excluded common
# short acronyms (TB, HIV, RNA); the stopword set instead screens out
# boilerplate patent-title words that would otherwise "match" on their own.
_TOKEN = re.compile(r"[a-z0-9]+")
_STOPWORDS = {"for", "and", "the", "of", "method", "methods", "treatment", "treating",
              "composition", "compositions", "use", "uses", "thereof", "with", "using"}
_ACADEMIC = re.compile(r"\b(university|institute|college|hospital|foundation)\b", re.I)


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall((text or "").lower()) if len(t) >= 2 and t not in _STOPWORDS}


def _is_jhu(name: str) -> bool:
    return "johns hopkins" in (name or "").lower()


def _is_industry_company(name: str) -> bool:
    """True only for a non-JHU, non-academic name that classifies as an
    actual industry company — never ``other`` (which would admit any
    university, hospital or foundation not literally named "Johns Hopkins")
    or ``cro_vendor``/``unknown``."""
    if not name or _is_jhu(name) or _ACADEMIC.search(name):
        return False
    return classify_company(name, "company") in ("pharma_biotech", "device_dx")


#: Applications per ODP request, and the most requests one PI's search makes (spec
#: 2026-10-05 §6.2, D12). A PI past 1,000 JHU applications is recorded as truncated.
ODP_PAGE_LIMIT = 100
MAX_PAGES = 10


def _ascii(text: str) -> str:
    """`text` with accents stripped and the Latin letters NFKD keeps (ø, ł, æ, ...) spelled
    in ASCII, case and punctuation kept."""
    return strip_accents(text.translate(_ASCII_LETTERS))


def _words(text: str | None) -> list[str]:
    """`text`'s words in ASCII, case folded ("O'Brien" -> ["o", "brien"])."""
    return _TOKEN.findall(fold(_ascii(text or "")))


@dataclass(frozen=True)
class InventorName:
    """What the search asks of an application's first inventor: the PI's first spelled-out
    given name (`given`; "" when the name has only initials, and then `initial` stands in)
    and surname, in ASCII, because ODP stores inventor names unaccented and does not fold a
    query's accents (measured 2026-10-10: "José" matched no JHU application, "Jose" two)."""

    given: str
    initial: str
    surname: str


def inventor_name(full_name: str | None) -> InventorName | None:
    """The PI's name parsed (`person_names.parse_person_name`: honorifics, comma degrees and
    suffixes dropped) into an InventorName ("D. Scott Wilson" gives given "Scott", initial
    "d"; "D.J. Smith" only the initial). A one-word name is all surname. None for an
    ORCID-iD name."""
    parsed = parse_person_name(full_name)
    if parsed.is_orcid_id or not parsed.full:
        return None
    tokens = parsed.full.split()
    surname = parsed.display_surname or parsed.full
    given_words = tokens[:len(tokens) - len(surname.split())]
    spelled = next((w for w in given_words if any(len(t) >= 2 for t in _words(w))), "")
    initial = next(iter(_words(given_words[0])), "")[:1] if given_words else ""
    return InventorName(_ascii(spelled), initial, _ascii(surname))


def inventor_query(full_name: str | None) -> str | None:
    """The ODP query for applications whose applicant is Johns Hopkins and whose first
    inventor's name holds the PI's given name and surname anywhere in it, so a middle name
    or initial no longer hides an application (2026-10-10: the full-name phrase missed 234
    JHU applications of 15 PIs, e.g. all 9 of "Justin R. Bailey"); `is_pi_first_inventor`
    then keeps the PI's own. With only an initial the query is the surname alone, so a
    common surname can fill MAX_PAGES and be recorded as truncated. Each part is escaped for
    its own quotes (`query_escaping.phrase_term`, P29). None for an ORCID-iD name or one that
    leaves nothing to search on."""
    name = inventor_name(full_name)
    surname = phrase_term(name.surname) if name else ""
    if not surname:
        return None
    given = phrase_term(name.given)
    clause = f'("{given}" AND "{surname}")' if given else f'"{surname}"'
    return (f"applicationMetaData.firstInventorName:{clause} AND "
            'applicationMetaData.applicantBag.applicantNameText:"Johns Hopkins"')


def _has_surname(words: list[str], surname: list[str]) -> bool:
    """Whether `words` hold every word of `surname`, or the surname written as one run of
    adjacent words joined ("OBRIEN" or "O BRIEN" for "O'Brien", "VANDERBERG" for "van der
    Berg")."""
    if set(surname) <= set(words):
        return True
    joined = "".join(surname)
    return any("".join(words[i:j]) == joined
               for i in range(len(words)) for j in range(i + 1, len(words) + 1))


def is_pi_first_inventor(first_inventor: str | None, name: InventorName) -> bool:
    """Whether an application's first inventor is the PI: the PI's surname (`_has_surname`)
    and every word of the given name are among its words, in any order (ODP holds "BAILEY,
    JUSTIN R." and "Velculescu E. Victor"); with only an initial, some word outside the
    surname starts with it. A different middle initial is not told apart, since the PI's
    own is unknown."""
    words = _words(first_inventor)
    surname = _words(name.surname)
    if not surname or not _has_surname(words, surname):
        return False
    if name.given:
        return set(_words(name.given)) <= set(words)
    return not name.initial or any(w.startswith(name.initial) for w in set(words) - set(surname))


async def fetch_jhu_applications(inventor_full_name: str | None) -> Paged:
    """Every JHU application whose first inventor is the PI (`inventor_query`, then
    `is_pi_first_inventor`), ODP_PAGE_LIMIT a request by `offset` (newest filing first,
    deduplicated by application number), until a short page, the reported `count`, or a
    404 (ODP's answer for "no matches", on any page); `truncated` after MAX_PAGES full
    pages with more reported. Raises SourceUnavailable without a key or a usable name: an
    empty answer would read as "no patents" and the job would delete the PI's stored patent
    rows. Every other error status raises, so the source is unavailable for the run."""
    key = get_settings().uspto_api_key
    if not key:
        raise SourceUnavailable("uspto: USPTO_API_KEY is not set", reason="no_api_key")
    q = inventor_query(inventor_full_name)
    if q is None:
        raise SourceUnavailable("uspto: no usable inventor name", reason="no_usable_name")
    name = inventor_name(inventor_full_name)

    def mine(found: dict[str, dict]) -> list[dict]:
        return [app for app in found.values() if is_pi_first_inventor(
            (app.get("applicationMetaData") or {}).get("firstInventorName"), name)]

    items: dict[str, dict] = {}
    async with odp_client(timeout=30, follow_redirects=False) as client:
        for page in range(MAX_PAGES):
            await ODP_PACER.wait()
            resp = await client.post(
                SEARCH_URL, headers=odp_headers(key),
                json={"q": q,
                      "pagination": {"offset": page * ODP_PAGE_LIMIT, "limit": ODP_PAGE_LIMIT},
                      "sort": [{"field": "applicationMetaData.filingDate", "order": "desc"}],
                      "fields": _FIELDS},
            )
            if resp.status_code == 404:
                return Paged(mine(items))
            resp.raise_for_status()
            data = resp.json()
            bag = data.get("patentFileWrapperDataBag") or []
            for app in bag:
                items.setdefault(app.get("applicationNumberText") or f"#{len(items)}", app)
            total = data.get("count")
            reached = isinstance(total, int) and (page + 1) * ODP_PAGE_LIMIT >= total
            if len(bag) < ODP_PAGE_LIMIT or reached:
                return Paged(mine(items))
    return Paged(mine(items), truncated=True)


def evidence_from_application(app: dict, tenure_start: int | None, pi_keywords: set[str]) -> list[EvidenceItem]:
    meta = app.get("applicationMetaData") or {}
    applicants = [a.get("applicantNameText", "") for a in meta.get("applicantBag") or []]
    if not any(_is_jhu(a) for a in applicants):
        return []
    filing = meta.get("filingDate") or ""
    year = int(filing[:4]) if filing[:4].isdigit() else None
    if tenure_start is None or year is None or year < tenure_start:
        return []
    title = meta.get("inventionTitle") or ""
    kw = {k.lower() for k in pi_keywords}
    if not (_tokens(title) & {t for k in kw for t in _tokens(k)}):
        return []
    appno = app.get("applicationNumberText") or title[:40]
    base = {"title": title, "filing_date": filing, "applicants": applicants}
    items = [EvidenceItem("uspto", "patent_filed", appno, None, None, "unknown", year, "inventor", True, dict(base))]
    companies = [a for a in applicants if _is_industry_company(a)]
    for bag in app.get("assignmentBag") or []:
        companies += [s.get("assigneeNameText", "") for s in bag.get("assigneeBag") or [] if _is_industry_company(s.get("assigneeNameText", ""))]
    for c in dict.fromkeys(c for c in companies if c):
        items.append(EvidenceItem("uspto", "patent_assigned", f"{appno}:{c.lower()}", c, None, classify_company(c, "company"), year, "inventor", True, dict(base)))
    return items
