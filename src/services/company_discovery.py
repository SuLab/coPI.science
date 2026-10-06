"""The `company_discovery` worker job (spec §7.5, O9-O13).

Suggests the companies a PI founded, from the PI's own disclosures only (O11): a
competing-interest sentence whose subject is the PI, as Claude extracts it from each
gated record (`company_sources.coi_llm`, O14), or Wikidata's "founded by" (P112) on the
PI's ORCID item (`company_sources.wikidata`).
SEC Form D only attaches funding and corroborates (`company_sources.sec_form_d`).

Every row written here is `status="suggested"`, `origin="discovered"`, with no creator:
a manager confirms or rejects it (O9), and only confirmed rows reach the hub file or an
assessment page. A name already present for the PI in ANY status (suggested, confirmed,
rejected) is never suggested again, which is what a rejected row exists for.

Each source runs behind its own try/except. A failure becomes a per-source note in the
job's final progress entry (`DISCOVERY_DONE_STEP`) and the job completes with whatever
the other sources found: `industry_evidence` lost whole runs to one upstream 429 (F13).
Only a database error fails the job.

Transactions: `src.worker.main.process_job` commits the handler's session once, after
the handler returns, and writes the job's status in separate sessions. The job reads
what it needs (the user, the publication PMIDs; later the names already listed) and
commits each read before the network work that follows it, so no snapshot or lock is
held while PubMed, Claude, Wikidata or SEC answer; those commits persist nothing,
because nothing has been written yet. The COI budget's usage and ledger rows are the
exception: they commit in short transactions of their own, on separate sessions
(`company_discovery_budget`), while the extraction calls run. Every network lookup (PubMed, the COI
extraction calls, Wikidata, then SEC Form D for each new candidate) runs before the
first insert: an uncommitted `pi_companies` row would otherwise hold its
`(user_id, normalized_name)` key through minutes of SEC requests and block a manager's
manual add of the same name until the job commits.

Bounds per run: only records that pass the deterministic gate
(`coi_founders.locate_pi` and `mentions_founding`) are candidates for extraction, at
most `coi_llm.MAX_COI_CALLS_PER_PI` of them (newest first), at most `COI_CONCURRENCY`
calls in flight; at most `MAX_NEW_CANDIDATES` new names get a Form D lookup and a row.
The outcome line reports the extraction's token spend ("coi: N calls, I in / O out
tokens"). Each send is reserved against the COI budget first and settled with its ledger
row after it (`company_discovery_budget`): a statement already settled as "ok" or
"skipped" under the PI's current name forms is not sent again and its stored claims are
merged back in, so a candidate `capped_new` cut on an earlier run returns; at the ceiling
the job defers itself (`job_queue.JobDeferred`) once the calls in flight have settled,
writing no suggestion. Discovery is requested after every successful generation
(`request_company_discovery`, spec 2026-10-05 D37).

Upstream text is untrusted: every candidate name passes the same cleaner a manual entry
does (`pi_companies._clean_name`), and a refused one is skipped with a note.

Must not import the modules tests/unit/test_enrichment_isolation.py forbids, nor
`industry_sources/pubmed_coi.py` (tests/unit/test_company_discovery_isolation.py).
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.models import Job, PiCompany, Publication, User
from src.models.pi_company import COI_MAX_ATTEMPTS
from src.services import job_progress, pubmed
from src.services.company_discovery_budget import (
    BudgetExhausted,
    CallTooLarge,
    CoiBudget,
    LedgerKey,
    budget_for,
    ledger_key,
)
from src.services.company_sources import (
    PiName,
    coi_founders,
    coi_llm,
    pi_name,
    sec_form_d,
    wikidata,
)
from src.services.job_queue import JobDeferred, insert_job_if_absent, request_job
from src.services.pi_companies import (
    _MAX_FUNDING_USD,
    CompanyValidationError,
    _clean_name,
    normalize_company_name,
)
from src.services.tenure_scope import publication_in_use

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
#: COI extraction calls in flight at once; results keep the newest-first order.
COI_CONCURRENCY = 4
#: New candidates (names the PI does not have in any status) one run looks up on SEC
#: and suggests; the rest wait for the next run ("candidates: capped at N").
MAX_NEW_CANDIDATES = 20
#: How much of a refused upstream name a skip note quotes.
_SHOWN_NAME_CHARS = 40


# --- enqueue -----------------------------------------------------------------


async def enqueue_company_discovery(
    db: AsyncSession, user_id: uuid.UUID, *, priority: int,
    not_before: datetime | None = None,
) -> uuid.UUID | None:
    """Queue discovery for one PI and return the new job id, or None when the PI
    already has one pending or processing (whose priority is raised to `priority` if
    lower, `job_queue.insert_job_if_absent`) or the user does not exist. `not_before`
    delays a new job (a bulk repair's pacing slot, spec 2026-10-05 §4.2). Adds to the
    caller's transaction; the caller commits."""
    orcid = await db.scalar(select(User.orcid).where(User.id == user_id))
    if orcid is None:
        return None
    return await insert_job_if_absent(
        db, type=COMPANY_DISCOVERY, user_id=user_id,
        payload={"user_id": str(user_id), "orcid": orcid}, priority=priority,
        not_before=not_before,
    )


async def request_company_discovery(
    db: AsyncSession, user_id: uuid.UUID, *, priority: int,
    not_before: datetime | None = None,
) -> uuid.UUID | None:
    """The profile pipeline's step-10 request (spec 2026-10-05 §6.2, D37): make sure a
    discovery run follows this generation (`job_queue.request_job`): a new job (its id), or
    the pending one kept and row-locked until the caller commits, or the processing one
    flagged for a rerun (None). None also when the user does not exist. Adds to the
    caller's transaction; the caller commits."""
    orcid = await db.scalar(select(User.orcid).where(User.id == user_id))
    if orcid is None:
        return None
    return await request_job(
        db, type=COMPANY_DISCOVERY, user_id=user_id,
        payload={"user_id": str(user_id), "orcid": orcid}, priority=priority, not_before=not_before,
    )


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


async def _publication_years(db: AsyncSession, user_id: uuid.UUID) -> dict[str, int | None]:
    """PMID -> publication year of the PI's publications that have a PMID and that staff
    have not excluded."""
    rows = (await db.execute(
        select(Publication.pmid, Publication.year)
        .where(Publication.user_id == user_id, Publication.pmid.isnot(None),
               publication_in_use())
        .order_by(Publication.pmid)
    )).all()
    return {str(pmid): year for pmid, year in rows}


async def _coi_claims(
    user_id: uuid.UUID, year_by_pmid: dict[str, int | None], name: PiName | None,
    notes: list[str], budget: CoiBudget,
) -> list[coi_founders.FounderClaim]:
    """Founder claims from the PubMed records of `year_by_pmid`'s PMIDs. Network only:
    the caller has already read the PMIDs and ended its transaction."""
    if not year_by_pmid:
        return []
    if name is None:
        notes.append("pubmed: no usable PI name")
        return []
    records = await _fetch_coi_records(user_id, list(year_by_pmid), notes)
    return await _extract_claims(user_id, records, name, year_by_pmid, notes, budget)


def _gated(record: dict, name: PiName) -> bool:
    """The deterministic gate `coi_llm.extract_founder_claims` applies before its call:
    a statement that mentions founding, on a record where the PI is located."""
    statement = (record.get("coi_statement") or "").strip()
    return coi_founders.mentions_founding(statement) and coi_founders.locate_pi(record, name) is not None


@dataclass
class _Stop:
    """Set when the budget refused a reservation: no further record is sent this run."""

    refused: bool = False
    #: The largest reservation refused (`BudgetExhausted.amount`), for `wake_time`.
    needed: Decimal = Decimal(0)


async def _extract_one(
    user_id: uuid.UUID, record: dict, key: LedgerKey, name: PiName,
    slots: asyncio.Semaphore, budget: CoiBudget, stop: _Stop,
) -> coi_llm.CoiOutcome | None:
    """One record's extraction under `slots`. The reservation comes first and outside the
    catch-all, so a refusal stops the remaining records (None) and is never turned into
    "unavailable". A raise from the extraction itself becomes "unavailable", so one odd
    record never costs the others. The settle (usage and ledger row) commits before this
    returns, so a paid result is kept whatever the job does next. A cancellation (a
    sibling's settle failed, or the job is stopping) still settles: mid-call as
    "unavailable" at the reservation, since the request may already be billed; while
    waiting to settle, with the real outcome. A record whose reservation would exceed the
    whole ceiling is not sent ("unavailable", reason "too_large")."""
    async with slots:
        if stop.refused:
            return None
        reserving = asyncio.ensure_future(
            budget.reserve(key, prompt_chars=coi_llm.prompt_chars(record, name)))
        try:
            reservation = await asyncio.shield(reserving)
        except asyncio.CancelledError:
            # A reservation committed under the cancellation is settled at $0 (no call).
            await asyncio.wait([reserving])
            if not reserving.cancelled() and reserving.exception() is None:
                await _settle_after_cancel(
                    budget, reserving.result(),
                    coi_llm.CoiOutcome("unavailable", [], reason="cancelled", entries=[]),
                    record.get("pmid"))
            raise
        except BudgetExhausted as exc:
            stop.refused = True
            stop.needed = max(stop.needed, exc.amount)
            return None
        except CallTooLarge:
            logger.warning("company_discovery %s: PMID %s is too large to send under the ceiling",
                           user_id, record.get("pmid"))
            return coi_llm.CoiOutcome("unavailable", [], reason="too_large", entries=[])
        try:
            outcome = await coi_llm.extract_founder_claims(record, name)
        except asyncio.CancelledError:
            await _settle_after_cancel(
                budget, reservation, coi_llm.CoiOutcome("unavailable", [], reason="cancelled"),
                record.get("pmid"))
            raise
        except Exception:
            logger.exception("company_discovery %s: COI extraction failed for PMID %s", user_id, record.get("pmid"))
            outcome = coi_llm.CoiOutcome("unavailable", [], reason="error")
        await _settle_through_cancel(budget, reservation, outcome)
        return outcome


async def _settle_after_cancel(budget: CoiBudget, reservation, outcome, pmid) -> None:
    """Settle under a cancellation already raised: shielded, and a failure only logged, so
    the caller re-raises the cancellation, not a database error."""
    try:
        await asyncio.shield(budget.settle(reservation, outcome))
    except Exception:
        logger.exception("company_discovery: settling PMID %s after a cancellation failed", pmid)


async def _settle_through_cancel(budget: CoiBudget, reservation, outcome) -> None:
    """Settle with the real outcome even if this task is cancelled while it waits on the
    budget's lock (the call is already billed); a cancellation is re-raised once settled."""
    task = asyncio.ensure_future(budget.settle(reservation, outcome))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.wait([task])
        raise


async def _send(
    user_id: uuid.UUID, to_send: list[tuple[dict, LedgerKey]], name: PiName, budget: CoiBudget,
) -> dict[LedgerKey, coi_llm.CoiOutcome]:
    """Run the extractions, COI_CONCURRENCY at a time. When the budget refused a
    reservation, every call already sent finishes and settles, then JobDeferred is raised
    (spec §6.2) with `budget.wake_time()`, read after those settles: the worker returns the
    job to pending without counting the attempt."""
    slots = asyncio.Semaphore(COI_CONCURRENCY)
    stop = _Stop()
    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(_extract_one(user_id, record, key, name, slots, budget, stop))
                     for record, key in to_send]
    except ExceptionGroup as failed:  # a settle's database error: the other tasks were cancelled
        raise failed.exceptions[0] from None
    if stop.refused:
        raise JobDeferred(await budget.wake_time(stop.needed),
                          "company discovery: the daily COI extraction budget is spent")
    return {key: outcome for (_, key), task in zip(to_send, tasks, strict=True)
            if (outcome := task.result()) is not None}


def spend_note(outcomes: list[coi_llm.CoiOutcome]) -> str | None:
    """"coi: N calls, I in / O out tokens" over the outcomes that carry token counts
    (one per answered API call): I is input plus cache read plus cache creation, O is
    output. None when no call was answered."""
    usages = [o.usage for o in outcomes if o.usage is not None]
    if not usages:
        return None
    tokens_in = sum(
        u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
        + u.get("cache_creation_input_tokens", 0)
        for u in usages
    )
    tokens_out = sum(u.get("output_tokens", 0) for u in usages)
    return f"coi: {len(usages)} calls, {tokens_in} in / {tokens_out} out tokens"


async def _extract_claims(
    user_id: uuid.UUID, records: list[dict], name: PiName,
    year_by_pmid: dict[str, int | None], notes: list[str], budget: CoiBudget,
) -> list[coi_founders.FounderClaim]:
    """Extraction over the records that pass the gate (`_gated`), newest first. A record
    whose ledger row is terminal under the current name forms (`budget.terminal`) is not
    sent: its stored claims are used. Of the rest, the first `coi_llm.MAX_COI_CALLS_PER_PI`
    are sent, `COI_CONCURRENCY` at a time ("coi: capped at N disclosures" when some were
    left unsent); none when the model is unpriced. A record that fails the gate costs
    nothing and takes no slot. An "unavailable" outcome, or a raise, costs that record
    alone ("coi: N of M disclosures unavailable", M = records sent and not skipped). Claims
    keep the records' newest-first order whatever order the calls finish in."""
    def newest(record: dict) -> tuple[int, int]:
        pmid = str(record.get("pmid") or "")
        year = year_by_pmid.get(pmid) or record.get("year") or 0
        return (year, int(pmid) if pmid.isdigit() else 0)

    cap = coi_llm.MAX_COI_CALLS_PER_PI
    gated = [r for r in sorted(records, key=newest, reverse=True) if _gated(r, name)]
    keyed = [(r, key) for r in gated if (key := ledger_key(r, name)) is not None]
    stored = await budget.terminal([key for _, key in keyed])
    unsent = [(r, key) for r, key in keyed if key not in stored]
    if len(unsent) > cap:
        notes.append(f"coi: capped at {cap} disclosures")
    to_send = unsent[:cap]
    if to_send and not budget.priced:
        notes.append(f"coi: model {budget.model} is not priced; nothing sent")
        to_send = []
    outcomes = await _send(user_id, to_send, name, budget)
    claims: list[coi_founders.FounderClaim] = []
    sent = unavailable = 0
    exhausted = 0
    for _record, key in keyed:
        if key in stored:
            found = stored[key]
            if found is None:  # not retried after COI_MAX_ATTEMPTS billed failures
                exhausted += 1
                continue
        else:
            outcome = outcomes.get(key)
            if outcome is None or outcome.status == "skipped":
                continue
            sent += 1
            if outcome.status != "ok":
                unavailable += 1
                continue
            found = outcome.claims
        for claim in found:
            if claim.year is None:
                claim = dataclasses.replace(claim, year=year_by_pmid.get(claim.pmid))
            claims.append(claim)
    if unavailable:
        notes.append(f"coi: {unavailable} of {sent} disclosures unavailable")
    if exhausted:
        notes.append(f"coi: {exhausted} disclosures not retried after {COI_MAX_ATTEMPTS} failed extractions")
    spend = spend_note(list(outcomes.values()))
    if spend:
        notes.append(spend)
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


def capped_new(new: list[Candidate], notes: list[str]) -> list[Candidate]:
    """The first `MAX_NEW_CANDIDATES` of `new` (merge order: newest statement first,
    then Wikidata-only names); the rest are skipped with "candidates: capped at N" and
    come back on a later run, since nothing was written for them."""
    if len(new) <= MAX_NEW_CANDIDATES:
        return new
    notes.append(f"candidates: capped at {MAX_NEW_CANDIDATES}")
    return new[:MAX_NEW_CANDIDATES]


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
    display_name, orcid = user.name, user.orcid
    name = pi_name(display_name)
    sec_user_agent = get_settings().sec_user_agent
    notes: list[str] = []
    budget = budget_for(db, user_id, slots=COI_CONCURRENCY)
    year_by_pmid = await _publication_years(db, user_id)
    # Nothing is written yet: end the read transaction before PubMed and Claude run
    # (module docstring, "Transactions").
    await db.commit()

    claims = valid_names(await _coi_claims(user_id, year_by_pmid, name, notes, budget), notes)
    companies = valid_names(await _wikidata_companies(user_id, orcid, sec_user_agent, notes), notes)
    candidates = merge_candidates(claims, companies)

    existing = set((await db.execute(
        select(PiCompany.normalized_name).where(PiCompany.user_id == user_id)
    )).scalars())
    await db.commit()  # again before the SEC lookups; ON CONFLICT absorbs a racing add
    new = [cand for key, cand in candidates.items() if key not in existing]
    already_listed = len(candidates) - len(new)
    new = capped_new(new, notes)

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
    outcome = outcome_line(suggested, already_listed, notes)
    logger.info("company_discovery %s (%s): %s", user_id, display_name, outcome)
    await job_progress.record(ctx.id, DISCOVERY_DONE_STEP, outcome)
