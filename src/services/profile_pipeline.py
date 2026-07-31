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
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
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

    # Extract PMIDs for works that have them
    pmids = [w["pmid"] for w in orcid_works if w.get("pmid")]

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

        if is_research and rec.get("abstract"):
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
    #     skip step 9b, the markdown export and create_revision below, costing the
    #     private-profile seed and the audit trail.
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
            profile.profile_version = (profile.profile_version or 0) + 1
            profile.profile_generated_at = datetime.now(timezone.utc)

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

    # Step 9b: Generate private profile seed (if no live profile and no existing seed)
    if not profile.private_profile_md and not profile.private_profile_seed:
        update_progress("step9b", "Generating agent instructions seed...")
        try:
            seed = await synthesize_private_profile(context_text, user.name)
            profile.private_profile_seed = seed
        except Exception as exc:
            logger.error("Private profile seed generation failed for %s: %s", user.name, exc)

    await db.flush()

    # Look up agent_id (gates file export and revision)
    from src.models import AgentRegistry
    agent_result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == user.id)
    )
    agent_reg = agent_result.scalar_one_or_none()
    agent_id = agent_reg.agent_id if agent_reg else None

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
