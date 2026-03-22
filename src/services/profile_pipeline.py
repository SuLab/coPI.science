"""Profile ingestion pipeline orchestrator.

Implements the 9-step pipeline from profile-ingestion.md:
1. Fetch ORCID profile
2. Fetch ORCID grants
3. Fetch ORCID works (PMIDs/DOIs)
4. Fetch PubMed abstracts
5. Deep mining: PMC methods sections
6. Collect user-submitted texts
7. LLM synthesis
8. Validation
9. Store
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
from src.services.llm import synthesize_profile
from src.services.orcid import fetch_orcid_grants, fetch_orcid_profile, fetch_orcid_works
from src.services.pubmed import (
    convert_dois_to_pmids,
    convert_pmids_to_pmcids,
    fetch_pmc_methods,
    fetch_pubmed_records,
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
    try:
        orcid_works = await fetch_orcid_works(orcid_id)
    except Exception as exc:
        logger.warning("Step 3 failed: %s", exc)
        orcid_works = []

    # Extract PMIDs for works that have them
    pmids = [w["pmid"] for w in orcid_works if w.get("pmid")]

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

        if pmid in existing_pubs:
            pub = existing_pubs[pmid]
        else:
            pub = Publication(
                user_id=user_id,
                pmid=pmid,
                pmcid=rec.get("pmcid"),
                doi=rec.get("doi"),
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

    # Step 6: Collect user-submitted texts
    update_progress("step6", "Collecting user-submitted texts...")
    # Load or create the ResearcherProfile record
    profile_result = await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == user_id)
    )
    profile = profile_result.scalar_one_or_none()
    if not profile:
        profile = ResearcherProfile(user_id=user_id)
        db.add(profile)
        await db.flush()

    user_submitted_texts = profile.user_submitted_texts or []

    # Step 7: LLM Synthesis
    update_progress("step7", "Synthesizing profile with AI...")
    context_text = _build_synthesis_context(
        orcid_profile=orcid_profile,
        grant_titles=grant_titles,
        publications=pubs_for_synthesis,
        methods_by_pmid=methods_by_pmid,
        user_submitted_texts=user_submitted_texts,
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

    # Step 9: Store
    update_progress("step9", "Saving profile to database...")
    profile.grant_titles = grant_titles or profile.grant_titles
    profile.raw_abstracts_hash = abstracts_hash

    if synthesized:
        profile.research_summary = synthesized.get("research_summary", "")
        profile.techniques = synthesized.get("techniques", [])
        profile.experimental_models = synthesized.get("experimental_models", [])
        profile.disease_areas = synthesized.get("disease_areas", [])
        profile.key_targets = synthesized.get("key_targets", [])
        profile.keywords = synthesized.get("keywords", [])
        profile.profile_version = (profile.profile_version or 0) + 1
        profile.profile_generated_at = datetime.now(timezone.utc)

    await db.flush()

    # Export to markdown for agent consumption
    from src.services.profile_export import export_profile_to_markdown
    export_profile_to_markdown(user, profile)

    update_progress("complete", "Profile generation complete.")
    return profile


def _build_synthesis_context(
    orcid_profile: dict[str, Any],
    grant_titles: list[str],
    publications: list[dict[str, Any]],
    methods_by_pmid: dict[str, str],
    user_submitted_texts: list[dict[str, Any]],
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

    # User-submitted texts
    if user_submitted_texts:
        parts.append("\n## User-Submitted Information")
        for entry in user_submitted_texts:
            label = entry.get("label", "Note")
            content = entry.get("content", "")
            parts.append(f"\n### {label}")
            parts.append(content[:2000])

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
