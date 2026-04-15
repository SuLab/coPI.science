"""Export a ResearcherProfile from the database to a markdown file for agent consumption."""

import logging
import re
from pathlib import Path

from src.models import Publication, ResearcherProfile, User

logger = logging.getLogger(__name__)

PROFILES_DIR = Path("profiles/public")
PRIVATE_PROFILES_DIR = Path("profiles/private")

# Map ORCID → agent ID for pilot labs
ORCID_TO_AGENT_ID = {
    "0000-0002-9859-4104": "su",
    "0000-0001-9287-6840": "wiseman",
    "0000-0002-6299-8799": "lotz",
    "0000-0001-5330-3492": "cravatt",
    "0000-0001-5908-7882": "grotjahn",
    "0000-0002-1010-145X": "petrascheck",
    "0000-0001-8336-9935": "ken",
    "0000-0003-2209-7301": "racki",
    "0000-0001-5718-5542": "saez",
    "0000-0002-2629-6124": "wu",
    "0000-0001-7153-3769": "ward",
    "0000-0001-9535-2866": "briney",
    "0000-0002-5964-7111": "forli",
    "0000-0003-2819-4049": "deniz",
    "0000-0001-6701-996X": "lairson",
}


def export_profile_to_markdown(
    user: User,
    profile: ResearcherProfile,
    publications: list[Publication] | None = None,
) -> Path | None:
    """Export a database profile to profiles/public/{agent_id}.md.

    Returns the path written, or None if the user isn't a pilot lab.
    """
    agent_id = ORCID_TO_AGENT_ID.get(user.orcid)
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

    path = PROFILES_DIR / f"{agent_id}.md"
    try:
        PROFILES_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
        logger.info("Exported profile for %s to %s", user.name, path)
        return path
    except Exception as exc:
        logger.error("Failed to export profile for %s: %s", user.name, exc)
        return None


def export_private_profile(user: User, profile: ResearcherProfile) -> Path | None:
    """Export private_profile_md to profiles/private/{agent_id}.md.

    Returns the path written, or None if the user isn't a pilot lab or has no content.
    """
    agent_id = ORCID_TO_AGENT_ID.get(user.orcid)
    if not agent_id:
        return None
    if not profile.private_profile_md:
        return None

    path = PRIVATE_PROFILES_DIR / f"{agent_id}.md"
    try:
        PRIVATE_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(profile.private_profile_md + "\n", encoding="utf-8")
        logger.info("Exported private profile for %s to %s", user.name, path)
        return path
    except Exception as exc:
        logger.error("Failed to export private profile for %s: %s", user.name, exc)
        return None


# Known DOI prefix → journal name patterns for validation.
# If a DOI prefix belongs to a specific publisher/journal but the publication's
# journal doesn't match, the DOI is likely wrong.
_DOI_PUBLISHER_PATTERNS: dict[str, list[str]] = {
    "10.1126/science": ["science"],
    "10.1038/s41586": ["nature"],
    "10.1038/s41556": ["nature cell biology"],
    "10.1038/s41587": ["nature biotechnology"],
    "10.1038/s41592": ["nature methods"],
    "10.1038/nmeth": ["nature methods"],
    "10.1038/s41467": ["nature communications"],
    "10.1016/j.cell": ["cell"],
    "10.7554/elife": ["elife"],
    "10.1083/jcb": ["journal of cell biology"],
    "10.1101/": ["biorxiv", "medrxiv", "preprint"],
    "10.1074/jbc": ["journal of biological chemistry"],
    "10.1073/pnas": ["proceedings of the national academy"],
    "10.15252/embj": ["embo journal"],
    "10.1109/": ["ieee"],
    "10.1371/journal.pgen": ["plos genetics"],
    "10.1371/journal.pbio": ["plos biology"],
    "10.1371/journal.pone": ["plos one"],
    "10.1021/acs.jproteome": ["journal of proteome research"],
    "10.1093/bioinformatics": ["bioinformatics"],
    "10.1016/j.bpj": ["biophysical journal"],
    "10.1016/j.sbi": ["current opinion in structural biology"],
}


def _validate_doi_journal(doi: str, journal: str | None) -> bool:
    """Check whether a DOI plausibly belongs to the given journal.

    Returns True if validation passes or is inconclusive (unknown prefix).
    Returns False only when there's a clear mismatch.
    """
    if not doi or not journal:
        return True  # Can't validate — assume ok

    doi_lower = doi.lower()
    journal_lower = journal.lower()

    for prefix, expected_patterns in _DOI_PUBLISHER_PATTERNS.items():
        if doi_lower.startswith(prefix.lower()):
            # This DOI has a known prefix — check if journal matches
            if any(pat in journal_lower for pat in expected_patterns):
                return True
            logger.warning(
                "DOI/journal mismatch: DOI %s (prefix %s) vs journal '%s'",
                doi, prefix, journal,
            )
            return False

    return True  # Unknown prefix — can't invalidate
