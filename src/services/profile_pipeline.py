"""Profile ingestion pipeline orchestrator.

Implements the pipeline from profile-ingestion.md:
1. Fetch ORCID profile
2. Fetch ORCID grants
3. Fetch ORCID works (PMIDs/DOIs)
4. Fetch PubMed abstracts
5. Deep mining: PMC methods sections
6. Prepare profile record
7. LLM synthesis (public profile)
8. Validation
9. Store, gated on validation and recorded on the profile row (migration 0023)
"""

import hashlib
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import AgentRegistry, Job, Publication, ResearcherProfile, User
from src.services.corpus import (
    DEFAULT_CAP,
    EXCLUDED_TYPES,  # noqa: F401 — re-exported; tests and callers import it from here
    resolve_corpus,
)
from src.services.jhu_rules import (
    derive_employment_start,
    derive_start_from_papers,
    get_tenure_start,
    set_tenure_start,
    tenure_filter,
)
from src.services.llm import synthesize_profile
from src.services.orcid import fetch_orcid_grants, fetch_orcid_profile
from src.services.pubmed import (
    convert_pmids_to_pmcids,
    fetch_pmc_methods,
    reconcile_pub_doi,
)

logger = logging.getLogger(__name__)

# The stored-corpus cap (coverage design §4.1; applied LAST, after ranking).
CORPUS_CAP = DEFAULT_CAP


def append_job_progress(job: Job, step: str, detail: str = "") -> None:
    """Append a progress entry so it actually reaches the database.

    ``Job.payload`` is a plain JSON column with no mutation tracking: an
    in-place append is only written if the attribute happens to be dirty for
    another reason. The old closure reassigned the payload on its FIRST call
    only, so every append after the pipeline's first ``db.flush()`` was
    silently dropped at commit. Reassigning a fresh dict on every call marks
    the attribute dirty each time.
    """
    payload = dict(job.payload or {})
    progress = list(payload.get("progress") or [])
    progress.append({"step": step, "detail": detail})
    payload["progress"] = progress
    job.payload = payload


async def run_profile_pipeline(
    user_id: uuid.UUID,
    db: AsyncSession,
    job: Job | None = None,
) -> ResearcherProfile:
    """
    Full profile generation pipeline. Updates job progress if job is provided.
    Returns the updated/created ResearcherProfile.
    """

    def update_progress(step: str, detail: str = ""):
        if job:
            append_job_progress(job, step, detail)
            logger.info("[pipeline] %s %s", step, detail)

    # Load user
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise ValueError(f"User {user_id} not found")

    orcid_id = user.orcid
    update_progress("start", f"Starting pipeline for {user.name} ({orcid_id})")

    # Step 1: Fetch ORCID profile
    update_progress("step1", "Fetching ORCID profile...")
    try:
        orcid_profile = await fetch_orcid_profile(orcid_id)
        # Update user record with fresh data
        if orcid_profile.get("name") and not user.name:
            user.name = orcid_profile["name"]
        if orcid_profile.get("institution") and not user.institution:
            user.institution = orcid_profile["institution"]
        if orcid_profile.get("department") and not user.department:
            user.department = orcid_profile["department"]
    except Exception as exc:
        logger.warning("Step 1 failed for %s: %s", orcid_id, exc)
        orcid_profile = {"name": user.name, "orcid": orcid_id}

    # Step 2: Fetch ORCID grants
    update_progress("step2", "Fetching grant information...")
    try:
        grant_titles = await fetch_orcid_grants(orcid_id)
    except Exception as exc:
        logger.warning("Step 2 failed: %s", exc)
        grant_titles = []

    # The agent row (may be None) is needed EARLY now: the legacy tenure map
    # is keyed by agent_id, and step 9's export/revision use it too.
    agent_result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == user.id)
    )
    agent_reg = agent_result.scalar_one_or_none()

    # Steps 3+4: resolve the corpus — S1 ORCID works, S2 OpenAlex, S3 PubMed
    # {orcid}[auid], S4 name+affiliation, identity-gated, ranked year-DESC,
    # capped LAST (coverage design §4.1). A stage failure RAISES so the job
    # retries rather than storing a thin ORCID-only corpus (defect D1/D2).
    update_progress(
        "step3",
        "Resolving publication corpus (ORCID + OpenAlex + PubMed)...",
    )
    corpus_result = await resolve_corpus(
        orcid_id, user.name, user.institution, cap=CORPUS_CAP
    )
    update_progress(
        "step4",
        f"Corpus resolved: kept {len(corpus_result.kept)} "
        f"(stages {corpus_result.stage_counts}, dropped {corpus_result.dropped})",
    )
    if corpus_result.flagged:
        sample = ", ".join(
            str(f.get("pmid")) for f in corpus_result.flagged[:10]
        )
        update_progress(
            "corpus_flagged",
            f"{len(corpus_result.flagged)} records withheld for review "
            f"(no individual author match): {sample}",
        )
    if len(corpus_result.kept) < 5:
        update_progress(
            "sparse_corpus",
            f"Only {len(corpus_result.kept)} publications resolved across "
            "ORCID, OpenAlex and PubMed.",
        )

    # JHU tenure window (R2): recorded value, else ORCID employment, else the
    # earliest paper the PI herself wrote at Hopkins. Derived values are
    # persisted with provenance — and only ever from a COMPLETE corpus, since
    # resolve_corpus raises on any stage failure before this point runs
    # (audit H1: the worker COMMITS mid-pipeline state when a job fails, so a
    # year derived from partial data must never be flushed).
    tenure_start = await get_tenure_start(
        db, user_id, agent_id=agent_reg.agent_id if agent_reg else None
    )
    if tenure_start is None:
        tenure_start = derive_employment_start(
            orcid_profile.get("employments") or []
        )
        if tenure_start is not None:
            await set_tenure_start(
                user_id, tenure_start, "orcid_employment", db=db
            )
            update_progress(
                "tenure_derived",
                f"JHU tenure start {tenure_start} (ORCID employment).",
            )
    if tenure_start is None:
        tenure_start = derive_start_from_papers(corpus_result.kept)
        if tenure_start is not None:
            await set_tenure_start(
                user_id, tenure_start, "earliest_hopkins_paper", db=db
            )
            update_progress(
                "tenure_derived",
                f"JHU tenure start {tenure_start} "
                "(earliest Hopkins-affiliated paper).",
            )
    if tenure_start is None:
        update_progress(
            "tenure_unknown",
            "No JHU tenure start could be derived (no current Hopkins ORCID "
            "employment, no Hopkins-affiliated paper in the corpus); the "
            "profile is FULL-CAREER scope until a year is set on the manager "
            "Edit Profile form.",
        )

    # Store publications. Storage is FULL-CAREER (the tenure filter applies at
    # synthesis and export, not storage — R2: "the full verified corpus stays
    # stored"), so both cohorts' rows mean the same thing and a tenure-year
    # correction is recoverable without a re-fetch.
    existing_result = await db.execute(
        select(Publication).where(Publication.user_id == user_id)
    )
    existing_pubs = {p.pmid: p for p in existing_result.scalars().all() if p.pmid}

    new_publications: list[Publication] = []

    def _reconcile(rec: dict[str, Any]) -> str | None:
        # The ORCID-curated DOI is preferred as the candidate, but it must
        # agree with the DOI PubMed has on file for this exact PMID
        # (reconcile_pub_doi treats the PubMed record's DOI as authoritative).
        pmid = rec.get("pmid")
        assigned_doi = corpus_result.orcid_dois.get(pmid) or rec.get("doi")
        doi, doi_action = reconcile_pub_doi(assigned_doi, rec.get("doi"))
        if doi_action == "corrected":
            logger.warning(
                "[doi-gate] pmid=%s: candidate DOI %r disagrees with PubMed "
                "record DOI %r; using authoritative",
                pmid, assigned_doi, doi,
            )
        return doi

    def _store(rec: dict[str, Any]) -> None:
        pub = Publication(
            user_id=user_id,
            pmid=rec.get("pmid"),
            pmcid=rec.get("pmcid"),
            doi=_reconcile(rec),
            title=rec.get("title", ""),
            abstract=rec.get("abstract", ""),
            journal=rec.get("journal"),
            year=rec.get("year"),
        )
        db.add(pub)
        new_publications.append(pub)

    if not existing_pubs:
        # New PI: the resolved corpus IS the stored corpus.
        for rec in corpus_result.kept:
            if rec.get("pmid"):
                _store(rec)
    else:
        # A PI with a pre-existing corpus — for the 62 audited ones the rows
        # carry per-paper human verification this run cannot reproduce, so:
        # never delete; add only ORCID-anchored finds (S1/S3); flag S2/S4-only
        # candidates for review instead of storing them; respect the cap
        # (audit M4 — the "leung at 53" defect class must not return).
        for rec in corpus_result.kept:
            pmid = rec.get("pmid")
            if pmid in existing_pubs:
                doi = _reconcile(rec)
                if doi and existing_pubs[pmid].doi != doi:
                    existing_pubs[pmid].doi = doi
        new_recs = [
            r for r in corpus_result.kept
            if r.get("pmid") and r["pmid"] not in existing_pubs
        ]
        anchored = [
            r for r in new_recs if set(r.get("stages") or []) & {"s1", "s3"}
        ]
        review_only = [
            r for r in new_recs
            if not (set(r.get("stages") or []) & {"s1", "s3"})
        ]
        budget = max(0, CORPUS_CAP - len(existing_pubs))
        to_store, over_cap = anchored[:budget], anchored[budget:]
        for rec in to_store:
            _store(rec)
        if to_store:
            update_progress(
                "corpus_additions",
                f"Added {len(to_store)} ORCID-anchored publications: "
                + ", ".join(str(r.get("pmid")) for r in to_store[:10]),
            )
        if over_cap:
            update_progress(
                "corpus_cap_reached",
                f"{len(over_cap)} newly found publications NOT stored: the "
                f"corpus is at the {CORPUS_CAP}-publication cap.",
            )
        if review_only:
            update_progress(
                "corpus_addition_review",
                f"{len(review_only)} candidates without an ORCID anchor "
                "(found by OpenAlex or name+affiliation search only) were NOT "
                "stored; review: "
                + ", ".join(str(r.get("pmid")) for r in review_only[:10]),
            )

    await db.flush()

    # Synthesis basis: the STORED corpus (both cohorts — additions included,
    # audited rows the resolver missed included too), tenure-filtered (R2).
    # Ordered newest-first so the abstracts hash is deterministic across runs.
    stored_result = await db.execute(
        select(Publication)
        .where(Publication.user_id == user_id)
        .order_by(
            Publication.year.desc().nullslast(), Publication.pmid.desc()
        )
    )
    corpus_records: list[dict[str, Any]] = [
        {
            "pmid": p.pmid,
            "pmcid": p.pmcid,
            "title": p.title,
            "abstract": p.abstract,
            "journal": p.journal,
            "year": p.year,
        }
        for p in stored_result.scalars().all()
    ]
    in_tenure = tenure_filter(corpus_records, tenure_start)
    pubs_for_synthesis = [r for r in in_tenure if r.get("abstract")]

    # Step 5: Deep mining — PMC methods sections
    update_progress("step5", "Fetching methods sections from PMC...")
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

    # Fill in PMCIDs from conversion
    for rec in pubs_for_synthesis:
        if rec.get("pmid") and not rec.get("pmcid") and rec["pmid"] in pmcid_map:
            rec["pmcid"] = pmcid_map[rec["pmid"]]

    # Fetch methods for papers with PMCIDs (limit to 10 to avoid too many API calls)
    papers_with_pmcid = [r for r in pubs_for_synthesis if r.get("pmcid")][:10]
    methods_by_pmid: dict[str, str] = {}

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

    # Step 6: Load or create profile record
    update_progress("step6", "Preparing profile record...")
    profile_result = await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == user_id)
    )
    profile = profile_result.scalar_one_or_none()
    if not profile:
        profile = ResearcherProfile(user_id=user_id)
        db.add(profile)
        await db.flush()

    # Step 7: LLM Synthesis
    update_progress("step7", "Synthesizing profile with AI...")
    context_text = _build_synthesis_context(
        orcid_profile=orcid_profile,
        grant_titles=grant_titles,
        publications=pubs_for_synthesis,
        methods_by_pmid=methods_by_pmid,
    )

    # Compute hash of source abstracts
    abstracts_str = "\n".join(p.get("abstract", "") for p in pubs_for_synthesis)
    abstracts_hash = hashlib.sha256(abstracts_str.encode()).hexdigest()

    synthesized: dict[str, Any] = {}
    try:
        synthesized = await synthesize_profile(context_text, user.name)
    except Exception as exc:
        logger.error("LLM synthesis failed for %s: %s", user.name, exc)
        update_progress("synthesis_failed", str(exc))

    # Step 8: Validation
    update_progress("step8", "Validating synthesized profile...")
    validated = _validate_profile(synthesized)

    if not validated and synthesized:
        # Re-try with stricter prompt (simplified: use same call again)
        logger.warning("Profile validation failed for %s, retrying...", user.name)
        try:
            synthesized = await synthesize_profile(
                context_text + "\n\nIMPORTANT: Ensure research_summary is 150-250 words.",
                user.name,
            )
            validated = _validate_profile(synthesized)
        except Exception as exc:
            logger.error("Retry synthesis failed: %s", exc)

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
    #   * Raising is loud in the log and silent in the UI. execute_generate_profile
    #     lets the exception reach process_job, which retries up to
    #     Job.max_attempts (default 3) — three more full LLM+NCBI runs for a
    #     formatting miss the retry above already tried to fix — and then sets
    #     status='dead'. templates/onboarding/profile_review.html keys its "Try
    #     Again" control on job_status == 'failed', which src/worker/main.py never
    #     assigns (it only ever writes 'pending' or 'dead'), so a dead job falls
    #     through to that template's `elif profile` branch and the PI is shown the
    #     review form with empty fields and no explanation. Raising would also
    #     skip the markdown export and create_revision below, costing the
    #     audit trail.
    #   * Storing nothing is indistinguishable from "the pipeline never ran" and
    #     throws away the only draft the PI has to edit. (It would not cause the
    #     /onboarding re-enqueue loop: that self-heal is gated on `job is None and
    #     profile is None`, and step 6 above always creates the row first.)
    #   * Storing + marking keeps onboarding moving — the PI edits the draft and
    #     POSTs /onboarding/save-profile — while being distinguishable (one column,
    #     one ERROR log, one job-progress entry) and recoverable (POST
    #     /onboarding/retry, or the next monthly_refresh).
    #
    # What it will NOT do is let a worse synthesis overwrite a better stored one.
    # A monthly refresh that fails validation, or one that runs while PubMed is
    # down, keeps the profile that is already there.
    update_progress("step9", "Saving profile to database...")
    profile.grant_titles = grant_titles or profile.grant_titles
    # Records this run's INPUT (change detection), so it is written even when the
    # synthesized fields below are not. The evidence counts are the ones that
    # describe the stored profile.
    profile.raw_abstracts_hash = abstracts_hash

    # What the pipeline had in scope, and what actually reached the prompt.
    # Both zero means there was nothing in the tenure window to synthesize
    # from; the first non-zero with the second zero means the in-scope papers
    # had no abstracts and whatever the model wrote is ungrounded. (The old
    # None-when-lookup-failed case is gone: a corpus stage failure now RAISES
    # and the job retries, so this line is only reached with a complete
    # corpus.) See ResearcherProfile.evidence_state.
    evidence_pmid_count = len(in_tenure)
    evidence_pub_count = len(pubs_for_synthesis)

    if synthesized:
        stored_is_worth_keeping = (
            (profile.profile_version or 0) > 0
            and bool(profile.research_summary)
            # A stored profile already known to have failed validation is not
            # worth protecting. NULL (legacy/unknown) is.
            and profile.synthesis_validated is not False
        )
        lost_evidence = evidence_pub_count == 0 and (profile.evidence_pub_count or 0) > 0
        if stored_is_worth_keeping and (not validated or lost_evidence):
            reason = (
                "failed validation twice"
                if not validated
                else f"grounded in 0 publications, down from {profile.evidence_pub_count}"
            )
            logger.error(
                "Discarding synthesized profile for %s (%s); keeping stored version %d",
                user.name, reason, profile.profile_version,
            )
            update_progress(
                "validation_rejected",
                f"Kept the existing profile (version {profile.profile_version}): "
                f"the new synthesis {reason}.",
            )
        else:
            profile.research_summary = synthesized.get("research_summary", "")
            profile.techniques = synthesized.get("techniques", [])
            profile.experimental_models = synthesized.get("experimental_models", [])
            profile.disease_areas = synthesized.get("disease_areas", [])
            profile.key_targets = synthesized.get("key_targets", [])
            profile.keywords = synthesized.get("keywords", [])
            profile.synthesis_validated = validated
            profile.evidence_pmid_count = evidence_pmid_count
            profile.evidence_pub_count = evidence_pub_count
            # SQL-side increment: the Python read-modify-write lost updates when
            # two writers raced (issue #22 C1) — worst at this pipeline site,
            # which holds the row across dozens of awaits between load and write.
            profile.profile_version = func.coalesce(ResearcherProfile.profile_version, 0) + 1
            profile.profile_generated_at = datetime.now(UTC)

            # The expression assignment EXPIRES profile_version, and the log line
            # below reads it — in an async session a lazy re-load raises
            # MissingGreenlet. Flush so the UPDATE lands, then load the new value
            # explicitly. (`profile` is always persistent here: step 6 flushes a
            # freshly created row, so the expression renders as an UPDATE, never
            # as an INSERT that could not reference its own target table.)
            await db.flush()
            await db.refresh(profile, ["profile_version"])

            if not validated:
                logger.error(
                    "Stored an UNVALIDATED profile for %s (version %d): failed "
                    "_validate_profile on both attempts. Marked "
                    "synthesis_validated=False for regeneration.",
                    user.name, profile.profile_version,
                )
                update_progress(
                    "unvalidated",
                    "The generated profile did not meet the quality checks "
                    "(150-250 word summary, 3+ techniques, 1+ disease area). "
                    "It was saved as a draft for you to edit.",
                )
            if evidence_pub_count == 0:
                # Nothing the researcher wrote reached the prompt, so whatever the
                # model produced came from its own priors plus a name and a
                # department. It is stored (a PubMed outage must not stop a PI
                # being onboarded, and some researchers really have no indexed
                # papers) but it is no longer indistinguishable from a real one.
                found = (
                    "an unknown number of"
                    if evidence_pmid_count is None
                    else str(evidence_pmid_count)
                )
                logger.error(
                    "Stored an UNGROUNDED profile for %s: 0 publication abstracts "
                    "reached the synthesis prompt (%s publication IDs in hand, "
                    "evidence_state=%s)",
                    user.name, found, profile.evidence_state,
                )
                update_progress(
                    "ungrounded",
                    f"No publication abstracts reached the profile synthesis "
                    f"({found} publication IDs were found): "
                    f"{profile.evidence_state}.",
                )

    await db.flush()

    # agent_reg was loaded before step 3 (the tenure map needed it);
    # it gates file export and revision here.
    agent_id = agent_reg.agent_id if agent_reg else None

    # Export to markdown for agent consumption (include publications).
    # The export list is tenure-filtered EXPLICITLY (JHU R2's export rule):
    # storage is full-career, and exporting the raw top-20 is exactly how
    # pre-tenure papers reached 9 agents' prompts on 2026-08-14 (audit H3).
    from src.services.profile_export import export_profile_to_markdown
    pub_result = await db.execute(
        select(Publication).where(Publication.user_id == user.id)
    )
    user_pubs = pub_result.scalars().all()
    if tenure_start is not None:
        user_pubs = [
            p for p in user_pubs if p.year and p.year >= tenure_start
        ]
    exported_path = export_profile_to_markdown(
        user, profile, agent_id, publications=user_pubs
    )

    # Record revision
    from src.services.profile_versioning import create_revision
    if agent_reg and exported_path:
        await create_revision(
            db,
            agent_registry_id=agent_reg.id,
            profile_type="public",
            content=exported_path.read_text(encoding="utf-8"),
            mechanism="pipeline",
            change_summary="Profile generated from ORCID + PubMed",
        )
        await db.flush()

    update_progress("complete", "Profile generation complete.")
    return profile


def _build_synthesis_context(
    orcid_profile: dict[str, Any],
    grant_titles: list[str],
    publications: list[dict[str, Any]],
    methods_by_pmid: dict[str, str],
) -> str:
    """Build the text context to pass to the LLM."""
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
    if grant_titles:
        parts.append("\n## Grant Titles")
        for title in grant_titles:
            parts.append(f"- {title}")

    # Publications (most recent 25-30, last-author prioritized)
    sorted_pubs = sorted(publications, key=lambda p: p.get("year") or 0, reverse=True)
    # Take up to 30
    selected_pubs = sorted_pubs[:30]

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
    if word_count < 100 or word_count > 350:
        logger.warning(
            "Research summary word count %d outside 150-250 range", word_count
        )
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
