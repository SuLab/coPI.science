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
9. Store, gated on validation and recorded on the profile row (migration 0023),
   + seed private profile (first creation only)
"""

import hashlib
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Update, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import Job, Publication, ResearcherProfile, User
from src.services.llm import synthesize_private_profile, synthesize_profile
from src.services.orcid import fetch_orcid_grants, fetch_orcid_profile, fetch_orcid_works
from src.services.pubmed import (
    convert_dois_to_pmids,
    convert_pmids_to_pmcids,
    fetch_pmc_methods,
    fetch_pubmed_records,
    reconcile_pub_doi,
)

logger = logging.getLogger(__name__)

# Non-research article types to exclude from profile synthesis
EXCLUDED_TYPES = {
    "editorial",
    "comment",
    "letter",
    "news",
    "published erratum",
    "retraction of publication",
    "correction",
    "biography",
}


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
            if "progress" not in job.payload:
                job.payload = dict(job.payload)
                job.payload["progress"] = []
            job.payload["progress"].append({"step": step, "detail": detail})
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

    # Step 3: Fetch ORCID works
    update_progress("step3", "Fetching publication list from ORCID...")
    # When this lookup FAILS we do not know how many works the researcher has, so
    # step 9 must not record "0 identifiers" — that reads as "nothing to fetch"
    # (a genuinely publication-less researcher) when it means "we could not ask".
    works_lookup_failed = False
    try:
        orcid_works = await fetch_orcid_works(orcid_id)
    except Exception as exc:
        logger.warning("Step 3 failed: %s", exc)
        orcid_works = []
        works_lookup_failed = True

    # Extract PMIDs for works that have them. Deduplicated (COR-16): ORCID lists a
    # work once per activities-summary source, so the same paper linked to two
    # co-author affiliations reaches this loop as the same PMID twice.
    pmids, seen_pmids = _dedup_pmids(orcid_works)

    # Build PMID → ORCID DOI map so we can prefer ORCID DOIs over PubMed DOIs.
    # PubMed's ArticleId DOIs are sometimes wrong (stale or from a different article).
    pmid_to_orcid_doi: dict[str, str] = {}
    for w in orcid_works:
        if w.get("pmid") and w.get("doi"):
            pmid_to_orcid_doi[w["pmid"]] = w["doi"]

    # Resolve DOIs → PMIDs for works that only have DOIs
    doi_only_works = [w for w in orcid_works if not w.get("pmid") and w.get("doi")]
    if doi_only_works:
        # Deduplicate DOIs (ORCID often lists the same work multiple times)
        seen_dois: set[str] = set()
        unique_doi_works: list[dict] = []
        for w in doi_only_works:
            if w["doi"] not in seen_dois:
                seen_dois.add(w["doi"])
                unique_doi_works.append(w)
        doi_only_works = unique_doi_works

        update_progress(
            "doi_resolve",
            f"Resolving {len(doi_only_works)} DOIs to PMIDs...",
        )
        try:
            dois = [w["doi"] for w in doi_only_works]
            doi_to_pmid = await convert_dois_to_pmids(dois)
            for w in doi_only_works:
                resolved_pmid = doi_to_pmid.get(w["doi"])
                if resolved_pmid:
                    w["pmid"] = resolved_pmid
                    if resolved_pmid not in seen_pmids:
                        seen_pmids.add(resolved_pmid)
                        pmids.append(resolved_pmid)
                    # Track the ORCID DOI that resolved to this PMID
                    pmid_to_orcid_doi[resolved_pmid] = w["doi"]
            logger.info(
                "DOI→PMID resolution: %d/%d resolved",
                len(doi_to_pmid), len(doi_only_works),
            )
        except Exception as exc:
            logger.warning("DOI→PMID resolution failed: %s", exc)

    if len(pmids) < 5:
        update_progress(
            "sparse_orcid",
            f"Only {len(pmids)} publications found on ORCID. "
            "For better matching, please update your ORCID profile at orcid.org.",
        )

    # Step 4: Fetch PubMed abstracts
    update_progress("step4", f"Fetching abstracts for {len(pmids)} publications...")
    pubmed_records: list[dict[str, Any]] = []
    if pmids:
        try:
            pubmed_records = await fetch_pubmed_records(pmids)
        except Exception as exc:
            logger.warning("Step 4 failed: %s", exc)

    # Determine author position for each record using orcid_works data
    # (PubMed records have author count but not which one is ours)
    orcid_works_by_pmid = {w["pmid"]: w for w in orcid_works if w.get("pmid")}

    # Store publications in DB
    # First, get existing publications for this user
    existing_result = await db.execute(
        select(Publication).where(Publication.user_id == user_id)
    )
    existing_pubs = {p.pmid: p for p in existing_result.scalars().all() if p.pmid}

    new_publications: list[Publication] = []
    pubs_for_synthesis: list[dict[str, Any]] = []
    # Tracks which PMIDs already have a record in pubs_for_synthesis, so a PMID
    # whose pubmed_records entry appears more than once (issue #22 COR-16
    # residual) contributes its abstract to the synthesis context once, not
    # once per duplicate record.
    synthesis_pmids_seen: set[str] = set()

    for rec in pubmed_records:
        pmid = rec.get("pmid")
        if not pmid:
            continue

        # Skip non-research articles for synthesis
        pub_types_lower = [t.lower() for t in rec.get("pub_types", [])]
        is_research = not any(exc_type in pub_types_lower for exc_type in EXCLUDED_TYPES)

        # DOI assignment + validation gate. The ORCID-curated DOI is preferred
        # as the candidate, but it must agree with the DOI PubMed has on file
        # for this exact PMID — otherwise the candidate points at a different
        # paper (the failure mode behind the bad-link incident). reconcile_pub_doi
        # treats rec["doi"] (this PMID's PubMed record) as authoritative: on a
        # verifiable mismatch it returns the authoritative DOI rather than
        # persisting a wrong one, and it canonicalizes format drift on a match.
        assigned_doi = pmid_to_orcid_doi.get(pmid) or rec.get("doi")
        doi, doi_action = reconcile_pub_doi(assigned_doi, rec.get("doi"))
        if doi_action == "corrected":
            logger.warning(
                "[doi-gate] pmid=%s: candidate DOI %r disagrees with PubMed record "
                "DOI %r; using authoritative",
                pmid, assigned_doi, doi,
            )

        if pmid in existing_pubs:
            pub = existing_pubs[pmid]
            # Apply the validated DOI if it changed.
            if doi and pub.doi != doi:
                pub.doi = doi
        else:
            pub = Publication(
                user_id=user_id,
                pmid=pmid,
                pmcid=rec.get("pmcid"),
                doi=doi,
                title=rec.get("title", ""),
                abstract=rec.get("abstract", ""),
                journal=rec.get("journal"),
                year=rec.get("year"),
            )
            db.add(pub)
            new_publications.append(pub)
            # COR-16: a PMID repeated later in this same loop (e.g. two ORCID
            # works resolving to one PMID) must hit the update branch above,
            # not db.add a second row that collides with
            # uq_publications_user_pmid (migration 0025).
            existing_pubs[pmid] = pub

        if is_research and rec.get("abstract") and pmid not in synthesis_pmids_seen:
            synthesis_pmids_seen.add(pmid)
            pubs_for_synthesis.append(rec)

    await db.flush()

    # Step 5: Deep mining — PMC methods sections
    update_progress("step5", "Fetching methods sections from PMC...")
    all_pmids_with_records = [r["pmid"] for r in pubmed_records if r.get("pmid")]

    # Get PMCIDs for papers that don't already have them
    pmids_needing_conversion = [
        r["pmid"]
        for r in pubmed_records
        if r.get("pmid") and not r.get("pmcid")
    ]

    pmcid_map: dict[str, str] = {}
    if pmids_needing_conversion:
        try:
            pmcid_map = await convert_pmids_to_pmcids(pmids_needing_conversion)
        except Exception as exc:
            logger.warning("Step 5 PMCID conversion failed: %s", exc)

    # Fill in PMCIDs from conversion
    for rec in pubmed_records:
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
                    select(Publication)
                    .where(
                        Publication.user_id == user_id,
                        Publication.pmid == rec["pmid"],
                    )
                    .order_by(Publication.id)
                )
                pub = existing_result2.scalars().first()
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
        # extract_json can return a parsed JSON array (or other non-dict) for a
        # malformed LLM response; normalize so validation/apply_synthesis below
        # never call a dict method on a non-dict (Minor 2).
        if not isinstance(synthesized, dict):
            synthesized = {}
    except Exception as exc:
        logger.error("LLM synthesis failed for %s: %s", user.name, exc)
        update_progress("synthesis_failed", str(exc))

    # Step 8: Validation
    update_progress("step8", "Validating synthesized profile...")
    try:
        validated = _validate_profile(synthesized)
    except Exception as exc:
        logger.error("Profile validation crashed for %s: %s", user.name, exc)
        validated = False

    if not validated and synthesized:
        # Re-try with stricter prompt (simplified: use same call again)
        logger.warning("Profile validation failed for %s, retrying...", user.name)
        try:
            # Prompt text intentionally unchanged (no prompt changes in this PR); the
            # gate below is the lenient _MIN_SUMMARY_WORDS-_MAX_SUMMARY_WORDS range.
            synthesized = await synthesize_profile(
                context_text + "\n\nIMPORTANT: Ensure research_summary is 150-250 words.",
                user.name,
            )
            # Same non-dict normalization as the first attempt (Minor 2).
            if not isinstance(synthesized, dict):
                synthesized = {}
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
    #     status='failed' (#21 COR-18e/f). templates/onboarding/profile_review.html
    #     keys its "Try Again" control on exactly that job_status == 'failed', so a
    #     job that exhausts its attempts here now reaches that control and the PI
    #     sees the retry button and an explanation, not the blank body this comment
    #     used to describe. Raising would also skip step 9b, the markdown export and
    #     create_revision below, costing the private-profile seed and the audit trail.
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

    # What the pipeline should have been able to fetch, and what actually reached
    # the prompt. Both zero means there was nothing to fetch; the first non-zero
    # with the second zero means the fetch failed and whatever the model wrote is
    # ungrounded. None for the first means step 3 could not even enumerate the
    # works, so "nothing to fetch" cannot be claimed. See
    # ResearcherProfile.evidence_state.
    evidence_pmid_count = None if works_lookup_failed else len(set(pmids))
    evidence_pub_count = len(pubs_for_synthesis)

    if synthesized:
        stored_is_worth_keeping = _stored_is_worth_keeping(profile)
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
            if apply_synthesis(profile, synthesized, validated=validated):
                profile.evidence_pmid_count = evidence_pmid_count
                profile.evidence_pub_count = evidence_pub_count
                profile.profile_version = await bump_profile_version(db, profile.id)

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
                    f"({_MIN_SUMMARY_WORDS}-{_MAX_SUMMARY_WORDS} word summary, 3+ techniques, "
                    "1+ disease area). It was saved as a draft for you to edit.",
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

    # Look up agent_id (gates file export and revision)
    from src.models import AgentRegistry
    agent_result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == user.id)
    )
    agent_reg = agent_result.scalar_one_or_none()
    agent_id = agent_reg.agent_id if agent_reg else None

    # Step 9b: Generate private profile seed, but ONLY for a PI who has never
    # completed onboarding (issue #22 COR-23 fix round). `POST
    # /onboarding/private-profile` always sets onboarding_complete=True and,
    # when submitted blank, clears both private_profile_md and
    # private_profile_seed (onboarding.py:save_private_profile) — that is a
    # deliberate "no private instructions" choice, not an absence of one yet.
    # Without this gate, the next pipeline run (regenerate, admin re-enqueue,
    # monthly refresh) would re-enter this branch, synthesize a fresh seed, and
    # (since the export below is unconditional) write model-authored private
    # instructions to disk for an agent whose PI explicitly cleared them. An
    # admin-seeded PI who never onboarded (onboarding_complete=False) is
    # unaffected and still gets a seed generated and exported.
    if (
        not profile.private_profile_md
        and not profile.private_profile_seed
        and not user.onboarding_complete
    ):
        update_progress("step9b", "Generating agent instructions seed...")
        try:
            seed = await synthesize_private_profile(context_text, user.name)
            profile.private_profile_seed = seed
        except Exception as exc:
            logger.error("Private profile seed generation failed for %s: %s", user.name, exc)

    await db.flush()

    # Export private profile to disk (COR-23): export_private_profile falls back to
    # private_profile_seed when there is no live private_profile_md yet, so an
    # admin-seeded lab whose PI never logs in still gets agent instructions on disk
    # instead of agent.py's "No private instructions yet." default.
    from src.services.profile_export import export_private_profile
    export_private_profile(user, profile, agent_id)

    # Export to markdown for agent consumption (include publications)
    from src.services.profile_export import export_profile_to_markdown
    pub_result = await db.execute(
        select(Publication).where(Publication.user_id == user.id)
    )
    user_pubs = pub_result.scalars().all()
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


def _dedup_pmids(orcid_works: list[dict[str, Any]]) -> tuple[list[str], set[str]]:
    """PMIDs from an ORCID works listing, first occurrence only (issue #22 COR-16).

    ORCID lists a work once per activities-summary source, so a paper linked to two
    co-author affiliations arrives here as the same PMID twice. Returns the ordered
    list and the seen-set, so the DOI->PMID resolution loop below can keep it up to
    date instead of appending a PMID the list already has.
    """
    pmids: list[str] = []
    seen: set[str] = set()
    for w in orcid_works:
        pmid = w.get("pmid")
        if pmid and pmid not in seen:
            seen.add(pmid)
            pmids.append(pmid)
    return pmids, seen


def _stored_is_worth_keeping(profile: ResearcherProfile) -> bool:
    """Whether `profile` already holds a synthesis worth protecting from being
    overwritten by a new one that failed validation or lost evidence (issue
    #22 COR-22 residual: this predicate used to be duplicated verbatim between
    `run_profile_pipeline` and `apply_synthesis`).

    A stored profile already known to have failed validation is not worth
    protecting. NULL (legacy/unknown, or PI-edited) is.
    """
    return (
        (profile.profile_version or 0) > 0
        and bool(profile.research_summary)
        and profile.synthesis_validated is not False
    )


_MIN_SUMMARY_WORDS = 100
_MAX_SUMMARY_WORDS = 350


def _validate_profile(profile: dict[str, Any] | None) -> bool:
    """
    Validate synthesized profile fields.
    Returns True if valid.
    """
    # extract_json's return type is annotated dict[str, Any] but is a bare
    # json.loads under the hood — a fenced JSON array/scalar parses fine and
    # comes back as a non-dict. vet_publications.py and
    # resynth_from_current_pubs.py call this function directly on their own
    # extract_json result before ever reaching apply_synthesis's own guard, so
    # this has to reject non-dicts too (#22 COR-22 fix-round review).
    if not isinstance(profile, dict):
        return False
    if not profile:
        return False

    research_summary = profile.get("research_summary")
    if not isinstance(research_summary, str):
        logger.warning(
            "research_summary is %s, not a string", type(research_summary).__name__
        )
        return False
    word_count = len(research_summary.split())
    if word_count < _MIN_SUMMARY_WORDS or word_count > _MAX_SUMMARY_WORDS:
        logger.warning(
            "Research summary word count %d outside %d-%d range",
            word_count, _MIN_SUMMARY_WORDS, _MAX_SUMMARY_WORDS,
        )
        return False

    techniques = profile.get("techniques", [])
    if not isinstance(techniques, list):
        logger.warning("techniques is %s, not a list", type(techniques).__name__)
        return False
    if len(techniques) < 3:
        logger.warning("Only %d techniques found (min 3)", len(techniques))
        return False

    disease_areas = profile.get("disease_areas", [])
    if not isinstance(disease_areas, list):
        logger.warning("disease_areas is %s, not a list", type(disease_areas).__name__)
        return False
    if not disease_areas:
        logger.warning("No disease areas found")
        return False

    return True


def apply_synthesis(
    profile: ResearcherProfile, synthesized: dict[str, Any] | None, *, validated: bool
) -> bool:
    """Apply a synthesized profile's fields to `profile` if it passes the same
    keep-what-you-have gate `run_profile_pipeline` uses, so a script-driven
    resynthesis can't do what only the pipeline used to protect against:
    overwrite a stored, validated profile with one that failed validation
    (issue #22 COR-22 residual — the four scripts/ synthesizers previously wrote
    `profile.research_summary` etc. directly with no gate and no provenance).

    Does NOT touch `evidence_pmid_count`/`evidence_pub_count` — those describe
    *how the synthesis was grounded*, which only `run_profile_pipeline` (the only
    caller that does its own ORCID/PubMed fetch) can compute; script callers leave
    the existing evidence counts as they are.

    Returns True if the fields were applied (and `profile.synthesis_validated`
    updated), False if the existing stored profile was kept unchanged.
    """
    # extract_json's return type is annotated dict[str, Any] but is a bare
    # json.loads under the hood — a fenced JSON *array* (or any other non-object
    # top-level value) parses fine and comes back as a non-dict. The pipeline's
    # own two synthesize_profile call sites normalize for this before this
    # function ever sees `synthesized`, but the four scripts/ callers pass their
    # own extract_json result straight through, so this guard has to be here too
    # (issue #22 COR-22/COR-23 residual).
    if not isinstance(synthesized, dict):
        return False

    if not synthesized:
        return False

    if _stored_is_worth_keeping(profile) and not validated:
        return False

    techniques = synthesized.get("techniques", [])
    profile.research_summary = synthesized.get("research_summary", "")
    profile.techniques = techniques if isinstance(techniques, list) else []
    profile.experimental_models = synthesized.get("experimental_models", [])
    profile.disease_areas = synthesized.get("disease_areas", [])
    profile.key_targets = synthesized.get("key_targets", [])
    profile.keywords = synthesized.get("keywords", [])
    profile.synthesis_validated = validated
    profile.profile_generated_at = datetime.now(UTC)
    return True


def bump_profile_version_stmt(profile_id: uuid.UUID) -> Update:
    """UPDATE ... SET profile_version = COALESCE(profile_version, 0) + 1 RETURNING it."""
    return (
        update(ResearcherProfile)
        .where(ResearcherProfile.id == profile_id)
        .values(profile_version=func.coalesce(ResearcherProfile.profile_version, 0) + 1)
        .returning(ResearcherProfile.profile_version)
    )


async def bump_profile_version(db: AsyncSession, profile_id: uuid.UUID) -> int:
    """Atomically increment ResearcherProfile.profile_version and return the new
    value (issue #22 C1). SQL-side `COALESCE(...) + 1`, not a Python
    read-modify-write — the profile row is loaded well before this point (often
    many awaited calls earlier), so `(profile.profile_version or 0) + 1` in
    Python silently drops a concurrent writer's increment.

    The row must already exist (flushed) — callers creating a brand-new
    ResearcherProfile must `await db.flush()` after `db.add(profile)` before
    calling this. `scalar_one()` raises `NoResultFound` if the row has since
    vanished (e.g. the user was deleted mid-pipeline) — this call does not
    catch that; the worker's failure path around `run_profile_pipeline`
    already handles a raised exception there.

    Callers MUST assign the return value back to `profile.profile_version`
    (see the call sites below) — this is not merely to keep a cached value
    from going stale. `SET profile_version = COALESCE(...)` is a SQL
    expression, not something SQLAlchemy's ORM-enabled UPDATE can evaluate in
    Python, so `synchronize_session` EXPIRES `profile_version` on the
    in-session `profile` instance rather than guessing its new value. An
    unassigned read of `profile.profile_version` after this call therefore
    triggers a lazy-load refresh from the DB — which on an `AsyncSession`
    raises `MissingGreenlet` (implicit IO outside an `await`), not merely a
    stale value. The explicit write-back re-dirties the ORM attribute with the
    integer this call already obtained atomically; re-flushing that value is
    harmless (the row lock serialized the increment, so it is the same value
    already committed) and it keeps the in-memory object usable for the rest
    of the request.
    """
    return (await db.execute(bump_profile_version_stmt(profile_id))).scalar_one()
