"""The `company_discovery` worker job (spec §7.5, O9-O13).

Suggests the companies a PI founded, from the PI's own disclosures only (O11): a
competing-interest sentence whose subject is the PI (`company_sources.coi_founders`), or
Wikidata's "founded by" (P112) on the PI's ORCID item (`company_sources.wikidata`).
SEC Form D only attaches funding and corroborates (`company_sources.sec_form_d`).

Every row written here is `status="suggested"`, `origin="discovered"`, with no creator:
a manager confirms or rejects it (O9), and only confirmed rows reach the hub file or an
assessment page. A name already present for the PI in ANY status (suggested, confirmed,
rejected) is never suggested again, which is what a rejected row exists for.

Each source runs behind its own try/except. A failure becomes a per-source note in the
job's final progress entry (`DISCOVERY_DONE_STEP`) and the job completes with whatever
the other sources found: `industry_evidence` lost whole runs to one upstream 429 (F13).
Only a database error fails the job.

The worker holds one transaction for the whole job (`src.worker.main.process_job`), so
every network lookup (PubMed, Wikidata, then SEC Form D for each new candidate) runs
before the first insert: an uncommitted `pi_companies` row would otherwise hold its
`(user_id, normalized_name)` key through minutes of SEC requests and block a manager's
manual add of the same name until the job commits.

Upstream text is untrusted: every candidate name passes the same cleaner a manual entry
does (`pi_companies._clean_name`), and a refused one is skipped with a note.

Must not import the modules tests/unit/test_enrichment_isolation.py forbids, nor
`industry_sources/pubmed_coi.py` (tests/unit/test_company_discovery_isolation.py).
"""
from __future__ import annotations

import dataclasses
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.models import Job, PiCompany, Publication, User
from src.services import job_progress, pubmed
from src.services.company_sources import PiName, coi_founders, pi_name, sec_form_d, wikidata
from src.services.job_queue import insert_job_if_absent
from src.services.pi_companies import (
    _MAX_FUNDING_USD,
    CompanyValidationError,
    _clean_name,
    normalize_company_name,
)

if TYPE_CHECKING:
    from src.worker.main import JobContext

logger = logging.getLogger(__name__)

COMPANY_DISCOVERY = "company_discovery"
#: The step of the job's last progress entry; its detail is the one-line outcome the
#: PI page's Companies card shows ("2 suggested; sec: funding lookup unavailable").
DISCOVERY_DONE_STEP = "discovery_done"
PUBMED_URL = "https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
#: Evidence bounds per suggestion: the newest attributed sentences, each clipped.
MAX_COI_EVIDENCE = 10
MAX_SENTENCE_CHARS = 1000
#: PMIDs per EFetch request; each batch fails alone (`_fetch_coi_records`).
PUBMED_BATCH = 100
#: How much of a refused upstream name a skip note quotes.
_SHOWN_NAME_CHARS = 40


# --- enqueue -----------------------------------------------------------------


async def enqueue_company_discovery(
    db: AsyncSession, user_id: uuid.UUID, *, priority: int
) -> uuid.UUID | None:
    """Queue discovery for one PI and return the new job id, or None when the PI
    already has one pending or processing (whose priority is raised to `priority` if
    lower, `job_queue.insert_job_if_absent`) or the user does not exist. Adds to the
    caller's transaction; the caller commits."""
    orcid = await db.scalar(select(User.orcid).where(User.id == user_id))
    if orcid is None:
        return None
    return await insert_job_if_absent(
        db, type=COMPANY_DISCOVERY, user_id=user_id,
        payload={"user_id": str(user_id), "orcid": orcid}, priority=priority,
    )


async def enqueue_first_company_discovery(
    db: AsyncSession, user_id: uuid.UUID, *, priority: int
) -> uuid.UUID | None:
    """The profile pipeline's step-10 enqueue: only when the PI has NO
    `company_discovery` row in any status, so only the first successful generation
    queues it and regenerations and refreshes do not (spec §7.5 Job)."""
    seen = await db.scalar(
        select(Job.id).where(Job.user_id == user_id, Job.type == COMPANY_DISCOVERY).limit(1)
    )
    if seen is not None:
        return None
    return await enqueue_company_discovery(db, user_id, priority=priority)


async def latest_discovery_job(db: AsyncSession, user_id: uuid.UUID) -> Job | None:
    """The PI's most recently enqueued discovery job, in any status (None if never run)."""
    return (await db.execute(
        select(Job)
        .where(Job.user_id == user_id, Job.type == COMPANY_DISCOVERY)
        .order_by(Job.enqueued_at.desc(), Job.id.desc())
        .limit(1)
    )).scalar_one_or_none()


# --- candidates --------------------------------------------------------------


@dataclass
class Candidate:
    normalized_name: str
    company_name: str
    pi_role: str
    coi_claims: list[coi_founders.FounderClaim] = field(default_factory=list)
    wikidata_items: list[wikidata.WikidataCompany] = field(default_factory=list)

    @property
    def source_url(self) -> str:
        """The newest attributed statement's PubMed page, else the Wikidata item."""
        if self.coi_claims:
            return PUBMED_URL.format(pmid=self.coi_claims[0].pmid)
        return self.wikidata_items[0].url


def _newest_first(claim: coi_founders.FounderClaim) -> tuple[int, int]:
    return (claim.year or 0, int(claim.pmid) if claim.pmid.isdigit() else 0)


def merge_candidates(
    claims: list[coi_founders.FounderClaim], companies: list[wikidata.WikidataCompany]
) -> dict[str, Candidate]:
    """One candidate per `normalize_company_name` key ("DELFI Diagnostics" and "Delfi
    Diagnostics, Inc." are one). Name and role come from the newest statement; a
    Wikidata-only candidate takes the item's label and `founder` (P112 does not say co-)."""
    out: dict[str, Candidate] = {}
    for claim in sorted(claims, key=_newest_first, reverse=True):
        key = normalize_company_name(claim.company_name)
        if not key:
            continue
        cand = out.setdefault(key, Candidate(key, claim.company_name, claim.pi_role))
        cand.coi_claims.append(claim)
    for company in companies:
        key = normalize_company_name(company.company_name)
        if not key:
            continue
        cand = out.setdefault(key, Candidate(key, company.company_name, "founder"))
        if all(w.item != company.item for w in cand.wikidata_items):
            cand.wikidata_items.append(company)
    return out


# --- sources (each failure is a note, never a job failure) --------------------


async def _coi_claims(
    db: AsyncSession, user_id: uuid.UUID, name: PiName | None, notes: list[str]
) -> list[coi_founders.FounderClaim]:
    rows = (await db.execute(
        select(Publication.pmid, Publication.year)
        .where(Publication.user_id == user_id, Publication.pmid.isnot(None))
        .order_by(Publication.pmid)
    )).all()
    if not rows:
        return []
    if name is None:
        notes.append("pubmed: no usable PI name")
        return []
    year_by_pmid = {str(pmid): year for pmid, year in rows}
    records = await _fetch_coi_records(user_id, list(year_by_pmid), notes)
    claims: list[coi_founders.FounderClaim] = []
    for record in records:
        try:
            found = coi_founders.founder_claims(record, name)
        except Exception:  # one odd record never costs the others
            logger.exception("company_discovery %s: COI parse failed for PMID %s", user_id, record.get("pmid"))
            continue
        for claim in found:
            if claim.year is None:
                claim = dataclasses.replace(claim, year=year_by_pmid.get(claim.pmid))
            claims.append(claim)
    return claims


async def _fetch_coi_records(user_id: uuid.UUID, pmids: list[str], notes: list[str]) -> list[dict]:
    """The PubMed records of `pmids`, fetched `PUBMED_BATCH` at a time with each batch
    strict (`pubmed.fetch_pubmed_records`) but failing alone: a transient failure of
    one batch costs that batch, not every record. Notes "pubmed: partial (N of M
    records)" when some batch failed and "pubmed: lookup unavailable" when every one did."""
    records: list[dict] = []
    failed = 0
    batches = [pmids[i:i + PUBMED_BATCH] for i in range(0, len(pmids), PUBMED_BATCH)]
    for batch in batches:
        try:
            records.extend(await pubmed.fetch_pubmed_records(batch, strict=True))
        except Exception as exc:  # an NCBI failure is a note, not a job failure
            logger.warning("company_discovery %s: PubMed batch %s... failed: %r", user_id, batch[:3], exc)
            failed += 1
    if failed == len(batches):
        notes.append("pubmed: lookup unavailable")
    elif failed:
        notes.append(f"pubmed: partial ({len(records)} of {len(pmids)} records)")
    return records


async def _wikidata_companies(
    user_id: uuid.UUID, orcid: str, sec_user_agent: str, notes: list[str]
) -> list[wikidata.WikidataCompany]:
    try:
        result = await wikidata.founded_by_orcid(orcid, contact=wikidata.contact_from(sec_user_agent))
    except Exception as exc:  # SourceUnavailable, or a bug: a note either way
        logger.warning("company_discovery %s: Wikidata lookup failed: %s", user_id, exc)
        notes.append("wikidata: lookup unavailable")
        return []
    return result.companies


async def _funding(cand: Candidate, name: PiName | None, sec_user_agent: str) -> sec_form_d.FundingResult:
    try:
        return await sec_form_d.lookup_funding(
            cand.company_name, cand.normalized_name, name, user_agent=sec_user_agent
        )
    except Exception as exc:  # lookup_funding maps upstream failures itself
        logger.exception("company_discovery: Form D lookup for %r failed", cand.company_name)
        return sec_form_d.FundingResult.unavailable(type(exc).__name__)


# --- untrusted input -----------------------------------------------------------


def _shown(name: str) -> str:
    """A refused name as a note quotes it: repr (control characters escaped), clipped."""
    clipped = name[:_SHOWN_NAME_CHARS]
    return repr(clipped) + ("..." if len(name) > _SHOWN_NAME_CHARS else "")


def valid_names(items: list, notes: list[str]) -> list:
    """`items` (`FounderClaim`s or `WikidataCompany`s) whose `company_name` passes the
    manual-entry cleaner, each with the cleaned name. A name with nothing alphanumeric
    is dropped silently, as `merge_candidates` always has; any other refusal (control
    characters, longer than `MAX_NAME_CHARS` before or after normalization) is dropped
    with one note per distinct name."""
    kept, refused = [], set()
    for item in items:
        if not normalize_company_name(item.company_name):
            continue
        try:
            name, _normalized = _clean_name(item.company_name)
        except CompanyValidationError as exc:
            if item.company_name not in refused:
                refused.add(item.company_name)
                notes.append(f"skipped name {_shown(item.company_name)}: {exc}")
            continue
        kept.append(dataclasses.replace(item, company_name=name))
    return kept


def bounded_funding(
    cand: Candidate, funding: sec_form_d.FundingResult, notes: list[str]
) -> sec_form_d.FundingResult:
    """`funding`, or, when its figure does not fit the column's Postgres bigint, the same
    result with the figure and its date dropped (status "no_amount") and a note; the
    suggestion itself is kept."""
    usd = funding.funding_usd
    if usd is None or 0 <= usd <= _MAX_FUNDING_USD:
        return funding
    notes.append(f"sec: funding figure out of range for {cand.company_name}")
    return dataclasses.replace(
        funding, status="no_amount", funding_usd=None, funding_as_of=None,
        funding_source_url=None, note="funding figure out of range; not recorded",
        filings=[dataclasses.replace(f, counted=False) for f in funding.filings],
    )


# --- write -------------------------------------------------------------------


def suggestion_evidence(cand: Candidate, funding: sec_form_d.FundingResult) -> dict:
    """The `pi_companies.evidence` document of a discovered row, which the PI page's
    Companies card renders: `coi` (newest first, at most `MAX_COI_EVIDENCE`: pmid,
    year, sentence, former, pi_role, company_name, url), `wikidata` (item, url, label),
    `form_d` (`FundingResult.evidence()`: status, funding, filings page and one entry
    per filing) and `former` (any attributed sentence used former-tie wording)."""
    return {
        "coi": [
            {
                "pmid": c.pmid, "year": c.year, "sentence": c.sentence[:MAX_SENTENCE_CHARS],
                "former": c.former, "pi_role": c.pi_role, "company_name": c.company_name,
                "url": PUBMED_URL.format(pmid=c.pmid),
            }
            for c in cand.coi_claims[:MAX_COI_EVIDENCE]
        ],
        "wikidata": [{"item": w.item, "url": w.url, "label": w.company_name} for w in cand.wikidata_items],
        "form_d": funding.evidence(),
        "former": any(c.former for c in cand.coi_claims),
    }


async def _write_suggestion(
    db: AsyncSession, user_id: uuid.UUID, cand: Candidate, funding: sec_form_d.FundingResult
) -> bool:
    """Insert one suggested row; False when the name appeared meanwhile (a manual add
    racing this job), which `ON CONFLICT (user_id, normalized_name) DO NOTHING` absorbs."""
    funded = funding.funding_usd is not None
    stmt = (
        pg_insert(PiCompany)
        .values(
            id=uuid.uuid4(), user_id=user_id, company_name=cand.company_name,
            normalized_name=cand.normalized_name, pi_role=cand.pi_role,
            funding_usd=funding.funding_usd if funded else None,
            funding_as_of=funding.funding_as_of if funded else None,
            funding_source_url=funding.funding_source_url if funded else None,
            source_url=cand.source_url, status="suggested", origin="discovered",
            evidence=suggestion_evidence(cand, funding), created_by_user_id=None,
        )
        .on_conflict_do_nothing(index_elements=["user_id", "normalized_name"])
        .returning(PiCompany.id)
    )
    return (await db.execute(stmt)).scalar_one_or_none() is not None


def outcome_line(suggested: int, already_listed: int, notes: list[str]) -> str:
    """"2 suggested", "0 suggested (2 already listed)", "2 suggested; sec: funding
    lookup unavailable"."""
    head = f"{suggested} suggested"
    if already_listed:
        head += f" ({already_listed} already listed)"
    return "; ".join([head, *notes])


async def execute_company_discovery(ctx: JobContext, db: AsyncSession) -> None:
    user_id = uuid.UUID(ctx.payload["user_id"])
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        await job_progress.record(ctx.id, DISCOVERY_DONE_STEP, "0 suggested; the account no longer exists")
        return
    name = pi_name(user.name)
    sec_user_agent = get_settings().sec_user_agent
    notes: list[str] = []

    claims = valid_names(await _coi_claims(db, user_id, name, notes), notes)
    companies = valid_names(await _wikidata_companies(user_id, user.orcid, sec_user_agent, notes), notes)
    candidates = merge_candidates(claims, companies)

    existing = set((await db.execute(
        select(PiCompany.normalized_name).where(PiCompany.user_id == user_id)
    )).scalars())
    new = [cand for key, cand in candidates.items() if key not in existing]

    # Every lookup first, then one short write phase (module docstring).
    fundings = [await _funding(cand, name, sec_user_agent) for cand in new]
    sec_down = any(f.status == "unavailable" for f in fundings)
    fundings = [bounded_funding(cand, f, notes) for cand, f in zip(new, fundings, strict=True)]
    suggested = 0
    for cand, funding in zip(new, fundings, strict=True):
        if await _write_suggestion(db, user_id, cand, funding):
            suggested += 1
    await db.flush()
    if sec_down:
        notes.append(f"sec: {sec_form_d.FUNDING_UNAVAILABLE}")
    outcome = outcome_line(suggested, len(candidates) - len(new), notes)
    logger.info("company_discovery %s (%s): %s", user_id, user.name, outcome)
    await job_progress.record(ctx.id, DISCOVERY_DONE_STEP, outcome)
