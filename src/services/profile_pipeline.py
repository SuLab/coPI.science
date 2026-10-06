"""Profile ingestion pipeline orchestrator.

Implements the pipeline from profile-ingestion.md:
1. Fetch ORCID profile
2. Fetch ORCID fundings into pi_orcid_fundings (soft)
3. Resolve the publication corpus (ORCID works + OpenAlex + PubMed; corpus.py) and store it
   (corpus_additions.apply_corpus_result)
4. Fetch PubMed abstracts
5. Deep mining: PMC methods sections
6. Prepare profile record
7. LLM synthesis (a failure fails the job)
8. Validation
9. Store, gated on validation; a profile edited by a person since its last generation gets
   a draft instead (profile_drafts)
10. Enrichment and company discovery jobs
11. Export: the persona is written and "complete" recorded after the job's commit

Locking: steps 1-2 commit before the corpus is resolved, and the run then takes the
persona writer locks (``profile_publish.lock_persona_writer``) BEFORE its first write
to the PI's rows (tenure keys, publications, the profile) and holds them until the job
commits, through the LLM synthesis and the export. Manager writes and account deletion
for this PI wait for the run meanwhile (lock order: ``profile_publish`` module
docstring).
"""

import functools
import logging
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import AgentRegistry, Publication, ResearcherProfile, User
from src.models.job import BULK_PRIORITY
from src.services import job_progress
from src.services.corpus import (
    DEFAULT_CAP,
    EXCLUDED_TYPES,  # noqa: F401 — re-exported; tests and callers import it from here
    resolve_corpus,
)
from src.services.corpus_additions import apply_corpus_result, lock_corpus
from src.services.grant_sections import GrantSections, grant_blocks, load_grant_sections
from src.services.jhu_rules import (
    clear_provisional_tenure_start,
    derive_employment_start,
    derive_start_from_papers,
    get_tenure_start,
    set_provisional_tenure_start,
    set_tenure_start,
    tenure_filter,
)
from src.services.job_queue import AfterCommit, NonRetryableJobError
from src.services.llm import SynthesisRefused, synthesize_profile
from src.services.orcid import fetch_orcid_profile
from src.services.orcid_fundings import fetch_orcid_fundings, store_orcid_fundings
from src.services.person_names import is_orcid_like, name_from_machine_source
from src.services.profile_drafts import build_draft_payload, edited_since_generation
from src.services.profile_limits import (
    SUMMARY_MAX_CHARS,
    SUMMARY_MAX_WORDS,
    SUMMARY_MIN_WORDS,
    SUMMARY_RULE,
    TAG_RULE,
    cap_tags,
)
from src.services.pubmed import (
    convert_pmids_to_pmcids,
    fetch_pmc_methods,
)
from src.services.tenure_scope import (
    publication_in_use,
    publication_order_by,
    publication_sort_key,
)

logger = logging.getLogger(__name__)

# The resolver's reporting cap (coverage design §4.1): bounds `CorpusResult.kept` only;
# storage is uncapped (corpus_additions.apply_corpus_result, spec 2026-10-05 D14).
CORPUS_CAP = DEFAULT_CAP

#: Newest in-tenure stored rows a synthesis reads (spec D14 "rolling newest-50").
SYNTHESIS_WINDOW = 50
ORCID_ID_NAME_REFUSAL = "name is an ORCID iD; enter the PI's name"
EMPTY_NAME_REFUSAL = "the PI has no name; enter the PI's name"
#: Appended to the context for the one retry after a failed validation.
RETRY_INSTRUCTION = (
    f"\n\nIMPORTANT: the research_summary must be {SUMMARY_RULE}. Tag lists: {TAG_RULE}."
)
#: The synthesized list fields, each cut to the D24 limits before storing.
_LIST_FIELDS = ("techniques", "experimental_models", "disease_areas", "key_targets", "keywords")


class SynthesisOutputError(ValueError):
    """The model's JSON is not a profile: research_summary is not a non-empty string, or a
    list field is not a list of strings (spec 2026-10-05 §6.3, G-15). Retried like any
    synthesis failure."""

# Progress-text label for each ``CorpusResult.flagged`` reason code that
# ``resolve_corpus`` emits. A code missing here shows raw rather than being
# mislabelled; tests/unit/test_corpus.py keeps the two sets equal.
_FLAG_REASON_LABELS = {
    "no_individual_author_match": "no individual author match",
    "bare_initial_unconfirmed": "initial-only name match not confirmed",
    "s4_affiliation_mismatch": (
        "name+affiliation search hit with a different author affiliation"
    ),
}


def _flag_reason_summary(flagged: list[dict[str, Any]]) -> str:
    """``"N <label>; M <label>"`` over the flagged records' reasons."""
    counts = Counter(str(f.get("reason")) for f in flagged)
    return "; ".join(
        f"{n} {_FLAG_REASON_LABELS.get(reason, reason)}"
        for reason, n in counts.most_common()
    )


@dataclass
class PipelineRun:
    """Everything one pipeline run carries from step to step (spec §7.4). Field names are the locals of the pre-split function, so each step body is the old code with those locals read as run.<name>."""

    user_id: uuid.UUID
    db: AsyncSession
    job_id: uuid.UUID | None
    user: User
    orcid_id: str
    orcid_profile: dict = field(default_factory=dict)
    step1_failed: bool = False
    agent_reg: AgentRegistry | None = None
    corpus_result: Any = None
    tenure_start: int | None = None
    in_tenure: list[dict] = field(default_factory=list)
    pubs_for_synthesis: list[dict] = field(default_factory=list)
    methods_by_pmid: dict[str, str] = field(default_factory=dict)
    profile: ResearcherProfile | None = None
    loaded_version: int = 0
    context_text: str = ""
    flagged_count: int = 0
    synthesized: dict = field(default_factory=dict)
    validated: bool = False
    after_commit: list[AfterCommit] | None = None
    followon_not_before: datetime | None = None

    async def progress(self, step: str, detail: str = "") -> None:
        """Record a progress entry on the job, if there is one, and log it. The
        entry commits in its own transaction (`job_progress.record`)."""
        if self.job_id is not None:
            await job_progress.record(self.job_id, step, detail)
            logger.info("[pipeline] %s %s", step, detail)


async def run_profile_pipeline(
    user_id: uuid.UUID,
    db: AsyncSession,
    job_id: uuid.UUID | None = None,
    after_commit: list[AfterCommit] | None = None,
    followon_not_before: datetime | None = None,
) -> ResearcherProfile:
    """
    Full profile generation pipeline. Records job progress if job_id is provided.
    Returns the updated/created ResearcherProfile.

    ``after_commit`` is the worker's JobContext.after_commit: the persona file is written
    there, after the job's commit (spec 2026-10-05 §4.3). None (a direct caller with no
    commit hook) writes it at the end of the run, inside the caller's transaction.
    ``followon_not_before`` is a bulk repair's slot for the step-10 follow-on jobs (§4.2).
    """
    run = await _start(user_id, db, job_id)
    run.after_commit = after_commit
    run.followon_not_before = followon_not_before
    await _step1_orcid_profile(run)
    await _step2_grants(run)
    await _steps3_4_resolve_corpus(run)
    await _lock_persona(run)
    await _derive_tenure_start(run)
    await _store_corpus_publications(run)
    await _load_synthesis_inputs(run)
    await _step5_methods(run)
    await _step6_profile_record(run)
    await _steps7_8_synthesize(run)
    await _step9_store(run)
    await _enqueue_enrichment(run)
    await _export_profile(run)
    return run.profile


async def _start(
    user_id: uuid.UUID, db: AsyncSession, job_id: uuid.UUID | None
) -> PipelineRun:
    # Load user
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise ValueError(f"User {user_id} not found")

    orcid_id = user.orcid
    if not orcid_id:
        raise NonRetryableJobError(f"User {user_id} has no ORCID iD")
    run = PipelineRun(
        user_id=user_id, db=db, job_id=job_id, user=user, orcid_id=orcid_id
    )
    await run.progress("start", f"Starting pipeline for {user.name} ({orcid_id})")
    return run


async def _step1_orcid_profile(run: PipelineRun) -> None:
    user = run.user
    orcid_id = run.orcid_id
    # Step 1: Fetch ORCID profile
    await run.progress("step1", "Fetching ORCID profile...")
    try:
        orcid_profile = await fetch_orcid_profile(orcid_id)
        # Update user record with fresh data
        if orcid_profile.get("name") and (not user.name or is_orcid_like(user.name)):
            # An empty or ORCID-iD name is replaced by ORCID's real one (spec §6.3), cut
            # to the D60 allowlist (§4.1); an iD-like ORCID name is stored unchanged and
            # refused below.
            user.name, cut = name_from_machine_source(orcid_profile["name"])
            if cut:
                user.name_sanitized_at = datetime.now(UTC)
        if orcid_profile.get("institution") and not user.institution:
            user.institution = orcid_profile["institution"]
        if orcid_profile.get("department") and not user.department:
            user.department = orcid_profile["department"]
        run.step1_failed = False
    except Exception as exc:
        logger.warning("Step 1 failed for %s: %s", orcid_id, exc)
        orcid_profile = {"name": user.name, "orcid": orcid_id}
        # The fallback has no ``employments``, so the tenure block below must
        # not treat "no Hopkins employment found" as an answer (D8).
        run.step1_failed = True
    run.orcid_profile = orcid_profile
    # Deterministic refusals (spec §6.3 Names): after step 1, which is what repairs the
    # name, and before step 2's commit, the corpus and any LLM call.
    refusal = (EMPTY_NAME_REFUSAL if not (user.name or "").strip()
               else ORCID_ID_NAME_REFUSAL if is_orcid_like(user.name) else None)
    if refusal:
        await run.progress("refused", refusal)
        raise NonRetryableJobError(refusal)


async def _step2_grants(run: PipelineRun) -> None:
    db = run.db
    user = run.user
    # Step 2: ORCID fundings, soft (spec 2026-10-05 §6.1). Step 1's writes commit first, the
    # fetch runs with no transaction open, and the store commits in its own short
    # transaction before the main work: an ORCID outage keeps the stored rows and never
    # fails generation (the activation gate refuses a dead generate_profile). The ORCID
    # veto still waits, bounded, behind the persona lock the run takes from tenure
    # derivation on (_lock_persona).
    await run.progress("step2", "Fetching ORCID fundings...")
    await db.commit()
    fundings = await fetch_orcid_fundings(run.orcid_id, strict=False)
    if fundings is None:
        await run.progress(
            "step2_orcid_unavailable", "ORCID fundings unavailable; kept the stored ones."
        )
    else:
        from src.services.profile_publish import lock_persona_writer
        try:
            async with db.begin_nested():
                # Persona writer locks before the child-row writes (lock order:
                # profile_publish module docstring); a lock error stays soft too.
                await lock_persona_writer(db, run.user_id)
                await store_orcid_fundings(db, run.user_id, fundings)
        except SQLAlchemyError as exc:
            logger.warning(
                "Step 2: storing ORCID fundings for %s failed: %s", run.orcid_id, exc
            )
    await db.commit()

    # The agent row (may be None) is needed EARLY now: the legacy tenure map
    # is keyed by agent_id, and step 9's export/revision use it too.
    agent_result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == user.id)
    )
    run.agent_reg = agent_result.scalar_one_or_none()


async def _steps3_4_resolve_corpus(run: PipelineRun) -> None:
    user = run.user
    # Steps 3+4: resolve the corpus — S1 ORCID works, S2 OpenAlex, S3 PubMed
    # {orcid}[auid], S4 name+affiliation, identity-gated, ranked year-DESC; the 50-cap
    # bounds only the reported kept set (coverage design §4.1). A stage failure RAISES so the job
    # retries rather than storing a thin ORCID-only corpus (defect D1/D2).
    await run.progress(
        "step3",
        "Resolving publication corpus (ORCID + OpenAlex + PubMed)...",
    )
    run.corpus_result = await resolve_corpus(
        run.orcid_id, user.name, user.institution, cap=CORPUS_CAP
    )
    corpus_result = run.corpus_result
    await run.progress(
        "step4",
        f"Corpus resolved: kept {len(corpus_result.kept)} "
        f"(stages {corpus_result.stage_counts}, dropped {corpus_result.dropped})",
    )
    if corpus_result.flagged:
        sample = ", ".join(
            str(f.get("pmid")) for f in corpus_result.flagged[:10]
        )
        await run.progress(
            "corpus_flagged",
            f"{len(corpus_result.flagged)} records withheld for review "
            f"({_flag_reason_summary(corpus_result.flagged)}): {sample}",
        )
    if corpus_result.truncated_stages:
        await run.progress(
            "corpus_truncated",
            f"Search stopped at its cap for {', '.join(corpus_result.truncated_stages)}: "
            "older papers may be missing, so a paper-derived tenure year is kept "
            "provisionally.",
        )
    if len(corpus_result.kept) < 5:
        await run.progress(
            "sparse_corpus",
            f"Only {len(corpus_result.kept)} publications resolved across "
            "ORCID, OpenAlex and PubMed.",
        )


async def _lock_persona(run: PipelineRun) -> None:
    """Take the persona writer locks for the rest of the job's transaction.

    Everything before this point either committed (steps 1-2) or only read and called
    the network (steps 3-4), so the run holds no lock on the PI's rows that a concurrent
    ``delete_user_account`` needs: a deletion either commits first, which this waits
    for and then refuses below, or queues behind the whole run. Writing first and
    locking later held FK and row locks through the LLM calls while deletion held the
    job's ``jobs`` row, which ``job_progress.record`` then waited on unboundedly.
    """
    from src.services.profile_publish import lock_persona_writer
    await lock_persona_writer(run.db, run.user_id)
    if await run.db.scalar(select(User.id).where(User.id == run.user_id)) is None:
        raise ValueError(
            f"User {run.user_id} was deleted mid-pipeline; aborting before any write"
        )
    # Agent creation/link/rename can commit during the network-only corpus
    # resolution. Use its authoritative current row for tenure and export.
    run.agent_reg = (await run.db.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == run.user_id)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()


async def _derive_tenure_start(run: PipelineRun) -> None:
    db = run.db
    user_id = run.user_id
    orcid_id = run.orcid_id
    agent_reg = run.agent_reg
    corpus_result = run.corpus_result
    # JHU tenure window (R2): recorded value, else ORCID employment, else the
    # earliest paper the PI herself wrote at Hopkins. Derived values are
    # persisted with provenance, on the job session, and only from a COMPLETE
    # corpus AND a successful step 1: resolve_corpus raises on any stage
    # failure before this point runs, and a failed job's writes never commit
    # (process_job rolls back before its failure bookkeeping). Two cases
    # return normally without that guarantee, and in both a paper-derived year
    # is used for this run's filtering and kept only PROVISIONALLY (later
    # exports scope by it; the next run replaces or deletes it) — never
    # recorded, because get_tenure_start prefers a recorded year on every later
    # run, so a wrong one would never self-correct:
    # * step 1 failed, so ORCID employment was never consulted and would
    #   outlive ORCID's recovery (D8);
    # * the corpus has ``permanently_dropped`` records (a per-item 4xx or an
    #   unreadable body), any of which could be the earliest Hopkins paper;
    # * a search stopped at its cap (``truncated_stages``), so older papers may be
    #   missing.
    # Only ORCID-anchored papers date tenure (derive_start_from_papers filters them).
    provisional_written = False
    tenure_start = await get_tenure_start(
        db, user_id, agent_id=agent_reg.agent_id if agent_reg else None
    )
    if tenure_start is None:
        tenure_start = derive_employment_start(
            run.orcid_profile.get("employments") or []
        )
        if tenure_start is not None:
            await set_tenure_start(
                user_id, tenure_start, "orcid_employment", db=db
            )
            await run.progress(
                "tenure_derived",
                f"JHU tenure start {tenure_start} (ORCID employment).",
            )
    if tenure_start is None:
        # The UNCAPPED list: the earliest Hopkins-affiliated paper of a PI
        # with more than the cap's worth of papers lies outside ``kept``.
        tenure_start = derive_start_from_papers(corpus_result.ranked)
        if run.step1_failed:
            unrecorded_because = "ORCID profile unavailable"
        elif corpus_result.permanently_dropped:
            unrecorded_because = (
                f"corpus incomplete: {len(corpus_result.permanently_dropped)} "
                "records could not be fetched"
            )
        elif corpus_result.truncated_stages:
            unrecorded_because = (
                "corpus incomplete: search capped at "
                f"{', '.join(corpus_result.truncated_stages)}"
            )
        else:
            unrecorded_because = None
        if tenure_start is not None and unrecorded_because is not None:
            logger.warning(
                "JHU tenure start %s for %s derived from papers (%s); used for this "
                "run and kept provisionally for later exports, not recorded",
                tenure_start, orcid_id, unrecorded_because,
            )
            await run.progress(
                "tenure_derived",
                f"JHU tenure start {tenure_start} used for this run and kept "
                f"provisionally ({unrecorded_because}; not recorded).",
            )
            await set_provisional_tenure_start(db, user_id, tenure_start)
            provisional_written = True
        elif tenure_start is not None:
            await set_tenure_start(
                user_id, tenure_start, "earliest_hopkins_paper", db=db
            )
            await run.progress(
                "tenure_derived",
                f"JHU tenure start {tenure_start} "
                "(earliest Hopkins-affiliated paper).",
            )
    if not provisional_written:
        # A provisional year never outlives a run that did not write one, so a
        # healthy run exports exactly as before.
        await clear_provisional_tenure_start(db, user_id)
    if tenure_start is None:
        await run.progress(
            "tenure_unknown",
            "No JHU tenure start could be derived (no current Hopkins ORCID "
            "employment, no Hopkins-affiliated paper in the corpus); the "
            "profile is FULL-CAREER scope until a year is set on the manager "
            "Edit Profile form.",
        )
    run.tenure_start = tenure_start


async def _store_corpus_publications(run: PipelineRun) -> None:
    """Store the resolve through the shared rule (corpus_additions.apply_corpus_result:
    anchored records of the uncapped ranked list, candidates for the rest, metadata and
    provenance refresh; never deletes). Storage is full-career (R2): the tenure filter
    applies at synthesis and export."""
    await lock_corpus(run.db, run.user_id)
    run.flagged_count = len(run.corpus_result.flagged)
    outcome = await apply_corpus_result(run.db, run.user_id, run.corpus_result)
    if outcome.stored and not outcome.first_ingest:
        await run.progress(
            "corpus_additions",
            f"Added {len(outcome.stored)} ORCID-anchored publications: "
            + ", ".join(outcome.stored[:10]),
        )
    if outcome.candidates_new:
        await run.progress(
            "corpus_addition_review",
            f"{len(outcome.candidates_new)} candidates without an ORCID anchor (found by "
            "OpenAlex or name+affiliation search only) were NOT stored; review them on the "
            "manager PI page: " + ", ".join(outcome.candidates_new[:10]),
        )
    if outcome.candidates_accepted:
        await run.progress(
            "corpus_candidates_accepted",
            f"Stored {len(outcome.candidates_accepted)} pending candidates now found with an "
            "ORCID anchor: " + ", ".join(outcome.candidates_accepted[:10]),
        )
    if outcome.refreshed:
        await run.progress(
            "corpus_metadata_refreshed",
            f"Refreshed {len(outcome.refreshed)} stored publications from PubMed "
            f"({len(outcome.year_changes)} year changes)",
        )


async def _load_synthesis_inputs(run: PipelineRun) -> None:
    db = run.db
    user_id = run.user_id
    # Synthesis basis: the STORED corpus (both cohorts — additions included,
    # audited rows the resolver missed included too), rows staff excluded left out,
    # tenure-filtered (R2). Newest first (the shared publication order); the newest
    # SYNTHESIS_WINDOW in-tenure rows are offered (D14).
    stored_result = await db.execute(
        select(Publication)
        .where(Publication.user_id == user_id, publication_in_use())
        .order_by(*publication_order_by())
    )
    corpus_records: list[dict[str, Any]] = [
        {
            "pmid": p.pmid,
            "pmcid": p.pmcid,
            "title": p.title,
            "abstract": p.abstract,
            "journal": p.journal,
            "year": p.year,
            "doi": p.doi,
            "id": str(p.id),
        }
        for p in stored_result.scalars().all()
    ]
    run.in_tenure = tenure_filter(corpus_records, run.tenure_start)
    run.pubs_for_synthesis = [
        r for r in run.in_tenure[:SYNTHESIS_WINDOW] if r.get("abstract")
    ]


async def _step5_methods(run: PipelineRun) -> None:
    db = run.db
    user_id = run.user_id
    pubs_for_synthesis = run.pubs_for_synthesis
    # Step 5: Deep mining — PMC methods sections
    await run.progress("step5", "Fetching methods sections from PMC...")
    # Get PMCIDs for the synthesis papers that don't already have them
    pmids_needing_conversion = [
        r["pmid"]
        for r in pubs_for_synthesis
        if r.get("pmid") and not r.get("pmcid")
    ]

    pmcid_map: dict[str, str] = {}
    if pmids_needing_conversion:
        try:
            pmcid_map = await convert_pmids_to_pmcids(pmids_needing_conversion)
        except Exception as exc:
            logger.warning("Step 5 PMCID conversion failed: %s", exc)

    # Fill in PMCIDs from conversion, and save each on the PI's row when it has none
    # (spec 2026-10-05 G-16).
    for rec in pubs_for_synthesis:
        if rec.get("pmid") and not rec.get("pmcid") and rec["pmid"] in pmcid_map:
            rec["pmcid"] = pmcid_map[rec["pmid"]]
    for pmid, pmcid in pmcid_map.items():
        await db.execute(
            update(Publication)
            .where(Publication.user_id == user_id, Publication.pmid == pmid,
                   Publication.pmcid.is_(None))
            .values(pmcid=pmcid)
        )

    # Fetch methods for papers with PMCIDs (limit to 10 to avoid too many API calls)
    papers_with_pmcid = [r for r in pubs_for_synthesis if r.get("pmcid")][:10]
    methods_by_pmid: dict[str, str] = {}
    run.methods_by_pmid = methods_by_pmid

    for rec in papers_with_pmcid:
        pmcid = rec.get("pmcid")
        if not pmcid:
            continue
        try:
            methods_text = await fetch_pmc_methods(pmcid)
            if methods_text:
                methods_by_pmid[rec["pmid"]] = methods_text
                # Update DB publication with methods text
                existing_result2 = await db.execute(
                    select(Publication).where(
                        Publication.user_id == user_id,
                        Publication.pmid == rec["pmid"],
                    )
                )
                pub = existing_result2.scalar_one_or_none()
                if pub:
                    pub.methods_text = methods_text[:10000]  # Cap at 10k chars
        except Exception as exc:
            logger.debug("Methods fetch failed for %s: %s", pmcid, exc)


async def _step6_profile_record(run: PipelineRun) -> None:
    db = run.db
    user_id = run.user_id
    # Step 6: Load or create profile record
    await run.progress("step6", "Preparing profile record...")
    profile_result = await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == user_id)
    )
    profile = profile_result.scalar_one_or_none()
    if not profile:
        profile = ResearcherProfile(user_id=user_id)
        db.add(profile)
        await db.flush()
    run.profile = profile
    # The version this run read. Step 9 writes the synthesized text only if it
    # still holds. Writers that take the persona lock cannot commit before this run
    # does (_lock_persona); the check keeps any other writer's edit.
    run.loaded_version = profile.profile_version or 0


def _researcher_info(user: User, orcid_profile: dict[str, Any]) -> dict[str, Any]:
    """The Researcher Information block's inputs: name, institution and department from
    the user record (staff-correctable; G-9), the lab website from ORCID."""
    info = {"name": user.name, "institution": user.institution, "department": user.department,
            "lab_website": orcid_profile.get("lab_website")}
    return {k: v for k, v in info.items() if v}


def checked_synthesis(raw: Any) -> dict[str, Any]:
    """The model's reply as a storable profile: research_summary stripped, each list field
    cut to the D24 limits (an absent list field reads as []). Raises SynthesisOutputError
    unless research_summary is a non-empty string within SUMMARY_MAX_CHARS and every
    list field present is a list of strings. The character cap is a hard storage/staging
    boundary, unlike the soft quality validation. Pure."""
    if not isinstance(raw, dict):
        raise SynthesisOutputError(f"synthesis reply is a {type(raw).__name__}, not an object")
    summary = raw.get("research_summary")
    if not isinstance(summary, str) or not summary.strip():
        raise SynthesisOutputError("research_summary is not a non-empty string")
    if len(summary.strip()) > SUMMARY_MAX_CHARS:
        raise SynthesisOutputError(f"research_summary exceeds {SUMMARY_MAX_CHARS} characters")
    checked: dict[str, Any] = {"research_summary": summary.strip()}
    for name in _LIST_FIELDS:
        values = raw.get(name)
        if values is None:
            values = []
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise SynthesisOutputError(f"{name} is not a list of strings")
        checked[name] = cap_tags(values)
    return checked


async def _synthesize(run: PipelineRun, context: str) -> dict[str, Any]:
    """One synthesis call, checked. Every failure propagates and fails the job (D18): a
    model refusal as NonRetryableJobError (dead at once), anything else as raised (retried
    by process_job). Each records its progress entry first."""
    try:
        return checked_synthesis(await synthesize_profile(context, run.user.name))
    except SynthesisRefused as exc:
        # A model refusal repeats on every attempt: dead at once (spec §7, DECISION).
        await run.progress("synthesis_refused", str(exc)[:500])
        raise NonRetryableJobError(f"synthesis refused by the model: {exc}") from exc
    except Exception as exc:
        await run.progress("synthesis_failed", f"{type(exc).__name__}: {exc}"[:500])
        raise


async def _steps7_8_synthesize(run: PipelineRun) -> None:
    user = run.user
    # Step 7: LLM Synthesis
    await run.progress("step7", "Synthesizing profile with AI...")
    grants = await load_grant_sections(run.db, run.user_id)
    run.context_text = _build_synthesis_context(
        orcid_profile=_researcher_info(user, run.orcid_profile),
        grants=grants,
        publications=run.pubs_for_synthesis,
        methods_by_pmid=run.methods_by_pmid,
    )
    run.synthesized = await _synthesize(run, run.context_text)

    # Step 8: Validation
    await run.progress("step8", "Validating synthesized profile...")
    run.validated = _validate_profile(run.synthesized)

    if not run.validated:
        # One retry with the limits restated.
        logger.warning("Profile validation failed for %s, retrying...", user.name)
        run.synthesized = await _synthesize(run, run.context_text + RETRY_INSTRUCTION)
        run.validated = _validate_profile(run.synthesized)


async def _step9_store(run: PipelineRun) -> None:
    db = run.db
    profile = run.profile
    # Step 9: Store.
    #
    # `validated` is READ here. It used to gate only the retry above: step 9 stored
    # on `if synthesized:` alone, so the retry's validation result was computed and
    # thrown away, and a profile that failed _validate_profile twice was persisted
    # as though it had passed. Nothing recorded the difference, so no test could
    # see it — hardwiring _validate_profile to `return True` changed no observable
    # behaviour at all. Two columns now record the decision (migration 0023):
    # `synthesis_validated`, and the evidence counts that say what the stored
    # fields are grounded in.
    #
    # The failure mode on a double validation failure is deliberate: store the
    # draft and MARK it, rather than raise or store nothing.
    #   * A synthesis that raises fails the job (D18, step 7); a reply that parses
    #     but fails validation twice is stored and marked, as below.
    #   * Storing nothing is indistinguishable from "the pipeline never ran" and
    #     throws away the only draft the PI has to edit. (It would not cause the
    #     /onboarding re-enqueue loop: that self-heal is gated on `job is None and
    #     profile is None`, and step 6 above always creates the row first.)
    #   * Storing + marking keeps onboarding moving — the PI edits the draft and
    #     POSTs /onboarding/save-profile — while being distinguishable (one column,
    #     one ERROR log, one job-progress entry) and recoverable (POST
    #     /onboarding/retry, or the manager's POST /manager/pis/{id}/profile/retry).
    #
    # What it will NOT do is let a worse synthesis overwrite a better stored one.
    # A monthly refresh that fails validation, or one that runs while PubMed is
    # down, keeps the profile that is already there. Nor does it overwrite a
    # person's edit: a profile edited since its last generation gets a draft (D19).
    await run.progress("step9", "Saving profile to database...")
    # The persona writer locks are already held (_lock_persona, before tenure
    # derivation) and stay held until the job commits.

    # What the pipeline had in scope, and what actually reached the prompt.
    # Both zero means there was nothing in the tenure window to synthesize
    # from; the first non-zero with the second zero means the in-scope papers
    # had no abstracts and whatever the model wrote is ungrounded. (A corpus
    # stage failure RAISES and the job retries, so this line is only reached
    # with a complete corpus.) See ResearcherProfile.evidence_state.
    evidence_pmid_count = len(run.in_tenure)
    evidence_pub_count = len(run.pubs_for_synthesis)

    if edited_since_generation(profile):
        await _stage_draft(run, evidence_pmid_count, evidence_pub_count)
        await db.flush()
        return

    keep_reason = _keep_stored_reason(run, evidence_pub_count)
    if keep_reason is not None:
        logger.error(
            "Discarding synthesized profile for %s (%s); keeping stored version %d",
            run.user.name, keep_reason, profile.profile_version,
        )
        await run.progress(
            "validation_rejected",
            f"Kept the existing profile (version {profile.profile_version}): "
            f"the new synthesis {keep_reason}.",
        )
    elif await _store_synthesis(run, evidence_pmid_count, evidence_pub_count):
        await _report_stored(run, evidence_pmid_count, evidence_pub_count)

    await db.flush()


async def _stage_draft(run: PipelineRun, evidence_pmid_count: int, evidence_pub_count: int) -> None:
    """Stage this run's synthesis as a draft for staff review (spec 2026-10-05 §6.3, D19):
    the text fields, profile_version and profile_generated_at stay as they are, and a
    newer draft replaces an older one."""
    profile = run.profile
    profile.pending_profile = build_draft_payload(
        fields=run.synthesized,
        synthesis_validated=run.validated,
        evidence_pmid_count=evidence_pmid_count,
        evidence_pub_count=evidence_pub_count,
        evidence_flagged_count=run.flagged_count,
        base_profile_version=run.loaded_version,
        job_id=run.job_id,
    )
    profile.pending_profile_created_at = datetime.now(UTC)
    await run.progress(
        "draft_staged",
        "The profile was edited by a person since its last generation, so this synthesis "
        "waits as a draft for staff review on the manager PI page; the stored profile is "
        "unchanged.",
    )


def _keep_stored_reason(run: PipelineRun, evidence_pub_count: int) -> str | None:
    """Why the stored profile is kept over this synthesis, or None to store it."""
    profile = run.profile
    stored_is_worth_keeping = (
        (profile.profile_version or 0) > 0
        and bool(profile.research_summary)
        # A stored profile already known to have failed validation is not
        # worth protecting. NULL (legacy/unknown) is.
        and profile.synthesis_validated is not False
    )
    if not stored_is_worth_keeping:
        return None
    if not run.validated:
        return "failed validation twice"
    if evidence_pub_count == 0 and (profile.evidence_pub_count or 0) > 0:
        return f"grounded in 0 publications, down from {profile.evidence_pub_count}"
    return None


async def _store_synthesis(
    run: PipelineRun, evidence_pmid_count: int, evidence_pub_count: int
) -> bool:
    """Write the synthesis over the stored text if the version step 6 read still holds.
    True when it was written."""
    db = run.db
    profile = run.profile
    synthesized = run.synthesized
    # This run's other writes (publications, methods text) are stored whatever
    # happens to the text below.
    await db.flush()
    # SQL-side increment (the Python read-modify-write lost updates when
    # two writers raced), and conditional on the version step 6 read: an
    # edit committed since by a writer that bypasses the persona lock wins,
    # and the run keeps that text.
    result = await db.execute(
        update(ResearcherProfile)
        .where(
            ResearcherProfile.id == profile.id,
            ResearcherProfile.profile_version == run.loaded_version,
        )
        .values(
            research_summary=synthesized.get("research_summary", ""),
            techniques=synthesized.get("techniques", []),
            experimental_models=synthesized.get("experimental_models", []),
            disease_areas=synthesized.get("disease_areas", []),
            key_targets=synthesized.get("key_targets", []),
            keywords=synthesized.get("keywords", []),
            synthesis_validated=run.validated,
            evidence_pmid_count=evidence_pmid_count,
            evidence_pub_count=evidence_pub_count,
            evidence_flagged_count=run.flagged_count,
            # A direct store supersedes an older draft.
            pending_profile=None,
            pending_profile_created_at=None,
            profile_version=func.coalesce(ResearcherProfile.profile_version, 0) + 1,
            profile_generated_at=datetime.now(UTC),
        )
        .execution_options(synchronize_session=False)
    )
    # Load the row as it now stands (the UPDATE bypassed the session, and
    # the log lines below read profile_version: a lazy re-load would
    # raise MissingGreenlet in an async session).
    await db.refresh(profile)
    if not result.rowcount:
        logger.warning(
            "Kept the edit made to %s's profile while this run synthesized "
            "(version %d at step 6, %d now); stored publications only",
            run.user.name, run.loaded_version, profile.profile_version,
        )
        await run.progress(
            "concurrent_edit_kept",
            f"Kept the profile edit saved while this ran (version "
            f"{profile.profile_version}); publications were updated.",
        )
        return False
    return True


async def _report_stored(
    run: PipelineRun, evidence_pmid_count: int, evidence_pub_count: int
) -> None:
    """Mark a stored profile that failed validation or is ungrounded, in the log and
    the job progress."""
    user = run.user
    profile = run.profile
    if not run.validated:
        logger.error(
            "Stored an UNVALIDATED profile for %s (version %d): failed "
            "_validate_profile on both attempts. Marked "
            "synthesis_validated=False for regeneration.",
            user.name, profile.profile_version,
        )
        await run.progress(
            "unvalidated",
            "The generated profile did not meet the quality checks "
            f"({SUMMARY_RULE} summary, 3+ techniques, 1+ disease area). "
            "It was saved as a draft for you to edit.",
        )
    if evidence_pub_count == 0:
        # Nothing the researcher wrote reached the prompt, so whatever the
        # model produced came from its own priors plus a name and a
        # department. It is stored (a PubMed outage must not stop a PI
        # being onboarded, and some researchers really have no indexed
        # papers) but it is no longer indistinguishable from a real one.
        found = str(evidence_pmid_count)
        logger.error(
            "Stored an UNGROUNDED profile for %s: 0 publication abstracts "
            "reached the synthesis prompt (%s publication IDs in hand, "
            "evidence_state=%s)",
            user.name, found, profile.evidence_state,
        )
        await run.progress(
            "ungrounded",
            f"No publication abstracts reached the profile synthesis "
            f"({found} publication IDs were found): "
            f"{profile.evidence_state}.",
        )


async def _enqueue_enrichment(run: PipelineRun) -> None:
    """Step 10: grant and industry enrichment, and company discovery, on every successful
    generation (spec 2026-10-05 §6.2, D37). Discovery goes through `request_job`
    (`request_company_discovery`): a pending discovery job is kept and runs after this
    commit, a processing one is flagged for a rerun, so a regenerated profile always gets a
    discovery run that sees it. Every insert joins this transaction, so a run that fails
    later queues none."""
    from src.services.company_discovery import request_company_discovery
    from src.services.grant_enrichment import enqueue_enrichment_jobs
    await enqueue_enrichment_jobs(
        run.db, run.user.id, run.orcid_id, priority=BULK_PRIORITY,
        not_before=run.followon_not_before,
    )
    await request_company_discovery(
        run.db, run.user.id, priority=BULK_PRIORITY, not_before=run.followon_not_before
    )
    await run.progress("step10", "Enqueued grant + industry enrichment jobs and requested company discovery")


async def _export_profile(run: PipelineRun) -> None:
    db = run.db
    user = run.user
    profile = run.profile
    agent_reg = run.agent_reg
    # agent_reg was loaded before step 3 (the tenure map needed it);
    # it gates the revision and the persona write here.

    # Deletion takes the per-PI advisory lock before it deletes anything, so with
    # the persona lock held since _lock_persona it cannot commit mid-run; this
    # re-check is defence in depth so a deleted account gets no revision and no
    # scheduled write (deletion audit 2026-08-25, F10). The writer itself re-selects
    # under the per-PI lock and writes nothing for a deleted account (spec
    # 2026-10-05 §4.3).
    if await db.scalar(select(User.id).where(User.id == user.id)) is None:
        raise ValueError(
            f"User {user.id} was deleted mid-pipeline; aborting before export"
        )

    # Re-read what the export will render: an edit committed after step 6 (only
    # possible for a writer that bypasses the persona lock) must reach the persona
    # file in BOTH branches, the kept-edit branch above and the keep-stored branch
    # that never wrote the text at all.
    await db.refresh(profile)
    await db.refresh(user)

    # Render the persona (publications and grant sections included).
    # The export list is tenure-filtered EXPLICITLY (JHU R2's export rule):
    # storage is full-career, and exporting the raw top-20 is exactly how
    # pre-tenure papers reached 9 agents' prompts on 2026-08-14 (audit H3).
    # tenure_start is already resolved above, so scope the rows already
    # loaded here rather than re-querying it via scoped_publications_for_export.
    from src.services.profile_publish import export_and_record, schedule_persona_write
    from src.services.tenure_scope import scope_for_export
    pub_result = await db.execute(
        select(Publication)
        .where(Publication.user_id == user.id, publication_in_use())
        .order_by(*publication_order_by())
    )
    user_pubs = scope_for_export(pub_result.scalars().all(), run.tenure_start)
    grants = await load_grant_sections(db, user.id)

    # The revision records this render; the file is written after the job's commit
    # (re-rendered from the database under the per-PI lock, spec 2026-10-05 §4.3).
    text = await export_and_record(
        db, user=user, profile=profile, agent=agent_reg, publications=user_pubs,
        grants=grants, mechanism="pipeline",
        change_summary="Profile generated from ORCID + PubMed",
    )
    await db.flush()
    if text is not None:
        await schedule_persona_write(db, user.id, run.after_commit)

    if run.after_commit is None:
        await run.progress("complete", "Profile generation complete.")
    else:
        # After the job's commit (spec 2026-10-05 §4.3, G-14): "complete" must not be
        # visible while the transaction that stores the profile can still roll back.
        run.after_commit.append(functools.partial(_record_complete, run))


async def _record_complete(run: PipelineRun, _db: AsyncSession) -> None:
    await run.progress("complete", "Profile generation complete.")


def _build_synthesis_context(
    orcid_profile: dict[str, Any],
    grants: GrantSections,
    publications: list[dict[str, Any]],
    methods_by_pmid: dict[str, str],
) -> str:
    """Build the text context to pass to the LLM. ``orcid_profile`` is the Researcher
    Information block: name, institution and department from the user record, lab website
    from ORCID (`_researcher_info`). Grants are the persona's own sections (Active, Past
    since tenure; spec 2026-10-05 D41), stored ones only: a PI's first run sees only ORCID
    items because enrich_grants runs after it (D63)."""
    parts = []

    # Researcher info
    parts.append("## Researcher Information")
    parts.append(f"- Name: {orcid_profile.get('name', 'Unknown')}")
    if orcid_profile.get("institution"):
        parts.append(f"- Institution: {orcid_profile['institution']}")
    if orcid_profile.get("department"):
        parts.append(f"- Department: {orcid_profile['department']}")
    if orcid_profile.get("lab_website"):
        parts.append(f"- Lab Website: {orcid_profile['lab_website']}")

    # Grants
    for heading, lines in grant_blocks(grants):
        parts.append(f"\n## {heading}")
        parts.extend(f"- {line.render()}" for line in lines)

    # Publications (most recent 30, in the shared publication order)
    selected_pubs = sorted(publications, key=publication_sort_key)[:30]

    if selected_pubs:
        parts.append("\n## Publications")
        for pub in selected_pubs:
            year = pub.get("year", "")
            journal = pub.get("journal", "")
            title = pub.get("title", "")
            parts.append(f"\n### {title} ({journal}, {year})")
            if pub.get("abstract"):
                parts.append(f"Abstract: {pub['abstract'][:1500]}")

    # Methods sections
    if methods_by_pmid:
        parts.append("\n## Methods Sections (from open-access papers)")
        for pmid, methods in methods_by_pmid.items():
            parts.append(f"\n### Methods from PMID {pmid}")
            parts.append(methods[:2000])

    return "\n".join(parts)


def _validate_profile(profile: dict[str, Any]) -> bool:
    """
    Validate synthesized profile fields.
    Returns True if valid.
    """
    if not profile:
        return False

    research_summary = profile.get("research_summary", "")
    word_count = len(research_summary.split())
    if len(research_summary) > SUMMARY_MAX_CHARS:
        logger.warning("Research summary character count %d exceeds %d",
                       len(research_summary), SUMMARY_MAX_CHARS)
        return False
    if word_count < SUMMARY_MIN_WORDS or word_count > SUMMARY_MAX_WORDS:
        logger.warning("Research summary word count %d outside %s", word_count, SUMMARY_RULE)
        return False

    techniques = profile.get("techniques", [])
    if len(techniques) < 3:
        logger.warning("Only %d techniques found (min 3)", len(techniques))
        return False

    disease_areas = profile.get("disease_areas", [])
    if not disease_areas:
        logger.warning("No disease areas found")
        return False

    return True
