"""The industry-evidence sources and their score weights, in one place (DP-08).

Each source declares the evidence kinds it produces with (per-company weight, cap);
`industry_score.WEIGHTS` is built from this registry and a test pins the weights EQUAL
to the pre-registry table; SCORER_VERSION is 2.0.0 since COI attribution and the tenure
filter (spec 2026-10-05 §6.2). A source that cannot be reached raises
`SourceUnavailable`; the job then keeps that source's stored rows and refreshes the
others (DP-04) instead of failing whole, and records `unavailable:<reason>` in the score
row's coverage; a paged source that stopped at its cap returns coverage `truncated`.
`sbir_sttr` has a weight and no producer yet (`EVIDENCE_KINDS` reserves it); its
source returns nothing."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import httpx

from src.services.industry_sources import EvidenceItem

if TYPE_CHECKING:
    from src.models import User


class SourceUnavailable(RuntimeError):
    """The upstream failed (transport, status, unreadable body), or the source cannot ask
    (no API key, no usable name). `reason` is the short code a score row's coverage
    records as "unavailable:<reason>"."""

    def __init__(self, message: str, *, reason: str = "error") -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class SourceContext:
    user: User
    tenure_start: int
    keywords: set[str]
    conditions: set[str]
    pmids: list[str]
    year_by_pmid: dict[str, int | None]


@dataclass
class SourceResult:
    items: list[EvidenceItem] = field(default_factory=list)
    note: str = ""
    #: "ok", or "truncated" when paging stopped at its cap with results left.
    coverage: str = "ok"


class IndustrySource(Protocol):
    name: str
    kinds: dict[str, tuple[float, float]]

    async def fetch(self, ctx: SourceContext) -> SourceResult: ...


def _unavailable(name: str, exc: BaseException) -> SourceUnavailable:
    reason = (f"http_{exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError)
              else type(exc).__name__)
    return SourceUnavailable(f"{name}: {type(exc).__name__}: {exc}", reason=reason)


class OpenAlexSource:
    name = "openalex"
    kinds = {"coauthor_company": (3.0, 30.0), "company_funder": (4.0, 20.0)}

    async def fetch(self, ctx: SourceContext) -> SourceResult:
        from src.services.industry_sources.openalex_industry import (
            _oaid,
            company_funder_ids,
            evidence_from_work,
            fetch_works_for_pmids,
        )
        try:
            paged = await fetch_works_for_pmids(ctx.pmids)
            works = paged.items
            funder_ids = {_oaid(f.get("id")) for w in works for f in w.get("funders") or []}
            cfids = await company_funder_ids(funder_ids)
        except (httpx.HTTPError, ValueError) as exc:
            raise _unavailable(self.name, exc) from exc
        items: list[EvidenceItem] = []
        for w in works:
            items += evidence_from_work(w, ctx.user.orcid, ctx.tenure_start, company_funder_ids=cfids)
        return SourceResult(items, note=f"openalex works={len(works)} items={len(items)}",
                            coverage="truncated" if paged.truncated else "ok")


class PubMedCoiSource:
    name = "pubmed"
    kinds = {"coi_relationship": (5.0, 25.0)}

    async def fetch(self, ctx: SourceContext) -> SourceResult:
        from src.services import pubmed
        from src.services.company_sources import pi_name
        from src.services.industry_sources.pubmed_coi import evidence_from_record
        pi = pi_name(ctx.user.name or "")
        if pi is None:
            # No name to attribute with: unavailable, so the PI's stored rows are kept rather
            # than deleted as "not returned" (spec 2026-10-05 §6.2).
            raise SourceUnavailable("pubmed: no usable name", reason="no_usable_name")
        try:
            records = await pubmed.fetch_pubmed_records(ctx.pmids, strict=True)
        except Exception as exc:
            raise _unavailable(self.name, exc) from exc
        items: list[EvidenceItem] = []
        for rec in records:
            y = rec.get("year") or ctx.year_by_pmid.get(str(rec.get("pmid")))
            items += evidence_from_record(
                rec, pi_year_ok=bool(y and y >= ctx.tenure_start), pi=pi, year=y)
        return SourceResult(items)


class UsptoSource:
    name = "uspto"
    kinds = {"patent_filed": (4.0, 12.0), "patent_assigned": (10.0, 30.0)}

    async def fetch(self, ctx: SourceContext) -> SourceResult:
        from src.services.industry_sources.uspto_inventor import (
            evidence_from_application,
            fetch_jhu_applications,
        )
        # The SourceUnavailable the fetcher raises (no key, no usable name) passes through.
        try:
            paged = await fetch_jhu_applications(ctx.user.name)
        except (httpx.HTTPError, ValueError) as exc:
            raise _unavailable(self.name, exc) from exc
        items: list[EvidenceItem] = []
        for app in paged.items:
            items += evidence_from_application(app, ctx.tenure_start, ctx.keywords)
        return SourceResult(items, coverage="truncated" if paged.truncated else "ok")


class CtGovSource:
    name = "ctgov"
    kinds = {"trial_industry_collab": (3.0, 9.0)}

    async def fetch(self, ctx: SourceContext) -> SourceResult:
        from src.services.industry_sources.ctgov import (
            evidence_from_study,
            fetch_jhu_industry_trials,
        )
        # The SourceUnavailable the fetcher raises (no usable name) passes through.
        try:
            paged = await fetch_jhu_industry_trials(ctx.user.name)
        except (httpx.HTTPError, ValueError) as exc:
            raise _unavailable(self.name, exc) from exc
        items: list[EvidenceItem] = []
        for st in paged.items:
            items += evidence_from_study(st, ctx.tenure_start, ctx.conditions)
        return SourceResult(items, coverage="truncated" if paged.truncated else "ok")


class NihReporterSource:
    name = "nih_reporter"
    kinds = {"sbir_sttr": (6.0, 12.0)}

    async def fetch(self, ctx: SourceContext) -> SourceResult:
        return SourceResult()


#: Order is the order the handler collected evidence in before the registry (openalex,
#: pubmed, uspto, ctgov); the de-duplication in the handler keeps the first occurrence,
#: so the order must not change.
SOURCES: tuple[IndustrySource, ...] = (
    OpenAlexSource(), PubMedCoiSource(), UsptoSource(), CtGovSource(), NihReporterSource(),
)


def weights() -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    for source in SOURCES:
        out.update(source.kinds)
    return out
