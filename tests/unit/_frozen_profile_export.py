"""Verbatim copy of export_profile_to_markdown before the Task 31 split.

Differential oracle for tests/unit/test_profile_export_split.py: the write step is
replaced by returning the joined text. Do not edit.
"""

from src.models import ResearcherProfile, User
from src.services.profile_export import _validate_doi_journal
from src.services.tenure_scope import TenureScopedPublications


def frozen_export(
    user: User,
    profile: ResearcherProfile,
    agent_id: str | None,
    publications: TenureScopedPublications | None = None,
) -> str | None:
    """Export a database profile to profiles/public/{agent_id}.md.

    ``publications`` must be a :class:`TenureScopedPublications`, obtained
    from ``src.services.tenure_scope.scoped_publications_for_export`` (or
    ``scope_for_export`` for callers that already hold the tenure year and
    the rows). A bare list is refused rather than silently accepted: that is
    exactly the shape six of eight call sites passed before the 2026-09-22
    tenure-attribution audit, which is how pre-tenure publications reached
    agent personas (audit H3).

    Returns the path written, or None if the user has no AgentRegistry entry
    (``agent_id`` is empty) or the write fails.
    """
    if publications is not None and not isinstance(publications, TenureScopedPublications):
        raise TypeError(
            "export_profile_to_markdown requires a TenureScopedPublications "
            "(see src.services.tenure_scope.scoped_publications_for_export), "
            f"not {type(publications).__name__}"
        )
    if not agent_id:
        return None

    lines = []
    lines.append(f"# {user.name} Lab — Public Profile\n")
    lines.append(f"**PI:** {user.name}")
    if user.institution:
        lines.append(f"**Institution:** {user.institution}")
    if user.department:
        lines.append(f"**Department:** {user.department}")
    lines.append("")

    # Research Summary
    if profile.research_summary:
        lines.append("## Research Summary\n")
        lines.append(profile.research_summary)
        lines.append("")

    # Techniques
    if profile.techniques:
        lines.append("## Key Methods and Technologies\n")
        for t in profile.techniques:
            lines.append(f"- {t}")
        lines.append("")

    # Experimental Models
    if profile.experimental_models:
        lines.append("## Model Systems\n")
        for m in profile.experimental_models:
            lines.append(f"- {m}")
        lines.append("")

    # Disease Areas
    if profile.disease_areas:
        lines.append("## Disease Areas / Biological Processes\n")
        for d in profile.disease_areas:
            lines.append(f"- {d}")
        lines.append("")

    # Key Targets
    if profile.key_targets:
        lines.append("## Key Molecular Targets\n")
        for k in profile.key_targets:
            lines.append(f"- {k}")
        lines.append("")

    # Keywords
    if profile.keywords:
        lines.append("## Keywords\n")
        lines.append(", ".join(profile.keywords))
        lines.append("")

    # Recent Publications (up to 20, most recent first)
    if publications:
        sorted_pubs = sorted(
            [p for p in publications if p.title],
            key=lambda p: p.year or 0,
            reverse=True,
        )[:20]
        if sorted_pubs:
            lines.append("## Recent Publications\n")
            for pub in sorted_pubs:
                # Build citation line with link
                parts = []
                if pub.title:
                    parts.append(pub.title.rstrip("."))
                if pub.journal:
                    parts.append(f"*{pub.journal}*")
                if pub.year:
                    parts.append(f"({pub.year})")
                citation = ". ".join(parts) + "."
                # Add link — validate DOI before including
                doi_ok = pub.doi and _validate_doi_journal(pub.doi, pub.journal)
                if doi_ok:
                    citation += f" https://doi.org/{pub.doi}"
                elif pub.pmid:
                    citation += f" https://pubmed.ncbi.nlm.nih.gov/{pub.pmid}/"
                elif pub.doi:
                    # DOI failed validation but no PMID fallback — include anyway
                    citation += f" https://doi.org/{pub.doi}"
                lines.append(f"- {citation}")
            lines.append("")

    # Grants
    if profile.grant_titles:
        lines.append("## Active Grants\n")
        for g in profile.grant_titles:
            lines.append(f"- {g}")
        lines.append("")

    return "\n".join(lines)
