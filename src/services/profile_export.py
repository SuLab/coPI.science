"""Export a ResearcherProfile from the database to a markdown file for agent consumption."""

import logging
from pathlib import Path

from src.models import Publication, ResearcherProfile, User

logger = logging.getLogger(__name__)

PROFILES_DIR = Path("profiles/public")

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
                # Add link
                if pub.doi:
                    citation += f" https://doi.org/{pub.doi}"
                elif pub.pmid:
                    citation += f" https://pubmed.ncbi.nlm.nih.gov/{pub.pmid}/"
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
