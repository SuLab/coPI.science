"""The industry-evidence sources and their score weights, in one place (DP-08).

Each source declares the evidence kinds it produces with (per-company weight, cap);
`industry_score.WEIGHTS` is built from this registry and a test pins it EQUAL to the
pre-registry table (SCORER_VERSION stays 1.0.0 because nothing changed). A source
that cannot be reached raises `SourceUnavailable`; the job then keeps that source's
stored rows and refreshes the others (DP-04) instead of failing whole.
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
    """The upstream failed (transport, status, unreadable body)."""


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
    primary_field: str | None = None
    note: str = ""


class IndustrySource(Protocol):
    name: str
    kinds: dict[str, tuple[float, float]]

    async def fetch(self, ctx: SourceContext) -> SourceResult: ...


def _unavailable(name: str, exc: BaseException) -> SourceUnavailable:
    return SourceUnavailable(f"{name}: {type(exc).__name__}: {exc}")


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
            works = await fetch_works_for_pmids(ctx.pmids)
            funder_ids = {_oaid(f.get("id")) for w in works for f in w.get("funders") or []}
            cfids = await company_funder_ids(funder_ids)
        except (httpx.HTTPError, ValueError) as exc:
            raise _unavailable(self.name, exc) from exc
        items: list[EvidenceItem] = []
        primary_field = None
        for w in works:
            primary_field = primary_field or ((w.get("primary_topic") or {}).get("field") or {}).get("display_name")
            items += evidence_from_work(w, ctx.user.orcid, ctx.tenure_start, company_funder_ids=cfids)
        return SourceResult(items, primary_field, f"openalex works={len(works)} items={len(items)}")


class PubMedCoiSource:
    name = "pubmed"
    kinds = {"coi_relationship": (5.0, 25.0)}

    async def fetch(self, ctx: SourceContext) -> SourceResult:
        from src.services import pubmed
        from src.services.industry_sources.pubmed_coi import evidence_from_record
        try:
            records = await pubmed.fetch_pubmed_records(ctx.pmids, strict=True)
        except Exception as exc:
            raise _unavailable(self.name, exc) from exc
        items: list[EvidenceItem] = []
        for rec in records:
            y = rec.get("year") or ctx.year_by_pmid.get(str(rec.get("pmid")))
            items += evidence_from_record(rec, pi_year_ok=bool(y and y >= ctx.tenure_start))
        return SourceResult(items)


class UsptoSource:
    name = "uspto"
    kinds = {"patent_filed": (4.0, 12.0), "patent_assigned": (10.0, 30.0)}

    async def fetch(self, ctx: SourceContext) -> SourceResult:
        from src.services.industry_sources.uspto_inventor import (
            evidence_from_application,
            fetch_jhu_applications,
        )
        try:
            apps = await fetch_jhu_applications(ctx.user.name)
        except (httpx.HTTPError, ValueError) as exc:
            raise _unavailable(self.name, exc) from exc
        items: list[EvidenceItem] = []
        for app in apps:
            items += evidence_from_application(app, ctx.tenure_start, ctx.keywords)
        return SourceResult(items)


class CtGovSource:
    name = "ctgov"
    kinds = {"trial_industry_collab": (3.0, 9.0)}

    async def fetch(self, ctx: SourceContext) -> SourceResult:
        from src.services.industry_sources.ctgov import (
            evidence_from_study,
            fetch_jhu_industry_trials,
        )
        try:
            studies = await fetch_jhu_industry_trials(ctx.user.name)
        except (httpx.HTTPError, ValueError) as exc:
            raise _unavailable(self.name, exc) from exc
        items: list[EvidenceItem] = []
        for st in studies:
            items += evidence_from_study(st, ctx.tenure_start, ctx.conditions)
        return SourceResult(items)


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
