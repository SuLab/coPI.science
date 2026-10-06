"""Export a ResearcherProfile from the database to a markdown file for agent consumption."""

import logging
import re
from pathlib import Path

from src.models import ResearcherProfile, User
from src.services.fs import atomic_write_text
from src.services.grant_sections import GrantSections, grant_section_lines
from src.services.tenure_scope import TenureScopedPublications, publication_sort_key

logger = logging.getLogger(__name__)

PROFILES_DIR = Path("profiles/public")


def _check_inputs(publications: object, grants: object) -> None:
    """Refuse a bare publication list and anything but a GrantSections for ``grants``."""
    if publications is not None and not isinstance(publications, TenureScopedPublications):
        raise TypeError(  # message unchanged: tests match "scoped_publications_for_export"
            "export_profile_to_markdown requires a TenureScopedPublications "
            "(see src.services.tenure_scope.scoped_publications_for_export), "
            f"not {type(publications).__name__}"
        )
    if not isinstance(grants, GrantSections):
        raise TypeError(
            "export_profile_to_markdown requires a GrantSections "
            "(see src.services.grant_sections.load_grant_sections), "
            f"not {type(grants).__name__}"
        )


def export_profile_to_markdown(
    user: User,
    profile: ResearcherProfile,
    agent_id: str | None,
    publications: TenureScopedPublications | None = None,
    *,
    grants: GrantSections,
) -> Path | None:
    """Export a database profile to profiles/public/{agent_id}.md.

    ``publications`` must be a :class:`TenureScopedPublications`, obtained
    from ``src.services.tenure_scope.scoped_publications_for_export`` (or
    ``scope_for_export`` for callers that already hold the tenure year and
    the rows). A bare list is refused rather than silently accepted: that is
    exactly the shape six of eight call sites passed before the 2026-09-22
    tenure-attribution audit, which is how pre-tenure publications reached
    agent personas (audit H3).

    ``grants`` is the persona's grant sections from
    ``src.services.grant_sections.load_grant_sections``, required (spec
    2026-10-05 §6.1); any other type raises ``TypeError``. The caller is the
    post-commit writer ``profile_publish.write_persona_files``.

    Returns the path written, or None if the user has no AgentRegistry entry
    (``agent_id`` is empty) or the write fails.
    """
    _check_inputs(publications, grants)
    if not agent_id:
        return None

    text = render_profile_markdown(user, profile, publications=publications, grants=grants)
    path = PROFILES_DIR / f"{agent_id}.md"
    try:
        PROFILES_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, text)
        logger.info("Exported profile for %s to %s", user.name, path)
        return path
    except Exception as exc:
        logger.error("Failed to export profile for %s: %s", user.name, exc)
        return None


def _header_lines(user: User) -> list[str]:
    """Title, PI, institution and department lines, ending with a blank line. The name,
    institution and department are whitespace-collapsed, so a newline in any of them
    cannot start a line (a heading or a section) of its own in the persona."""
    # A missing name renders as before ("None"); only its whitespace is collapsed.
    name = user.name if user.name is None else " ".join(user.name.split())
    lines = [f"# {name} Lab — Public Profile\n", f"**PI:** {name}"]
    institution = " ".join((user.institution or "").split())
    department = " ".join((user.department or "").split())
    if institution:
        lines.append(f"**Institution:** {institution}")
    if department:
        lines.append(f"**Department:** {department}")
    lines.append("")
    return lines


def _bullet_section(lines: list[str], title: str, items) -> None:
    """Append a ``## title`` bullet list to ``lines``; nothing when ``items`` is falsy."""
    if items:
        lines.append(f"## {title}\n")
        for item in items:
            lines.append(f"- {item}")
        lines.append("")


def _citation(pub) -> str:
    """One publication's citation text and link (spec 2026-10-05 §6.3, P28): the DOI only
    when it is verified against PubMed's record for the PMID (``doi_verified``) and fits
    the journal; else the PubMed page; a PMID-less row keeps a DOI that fits the journal,
    and gets no link when the DOI contradicts it."""
    parts = []
    if pub.title:
        parts.append(pub.title.rstrip("."))
    if pub.journal:
        parts.append(f"*{pub.journal}*")
    if pub.year:
        parts.append(f"({pub.year})")
    citation = ". ".join(parts) + "."
    doi_fits = bool(pub.doi) and _validate_doi_journal(pub.doi, pub.journal)
    if doi_fits and pub.doi_verified:
        citation += f" https://doi.org/{pub.doi}"
    elif pub.pmid:
        citation += f" https://pubmed.ncbi.nlm.nih.gov/{pub.pmid}/"
    elif doi_fits:
        citation += f" https://doi.org/{pub.doi}"
    return citation


def _escape_heading_lines(text: str) -> str:
    """``text`` with a backslash before the ``#`` of every line whose first non-blank
    character is ``#`` (indentation kept), so a Research Summary cannot open a persona
    section for any parser (spec 2026-10-05 D49)."""
    return re.sub(r"(?m)^([ \t]*)#", r"\1\\#", text)


def _publication_lines(publications: TenureScopedPublications | None) -> list[str]:
    """Recent Publications section (up to 20, publication_sort_key order); empty when none.
    Each line ends with its link (``_citation``) and, where the row has a PMID,
    ``(PMID n)`` (D49)."""
    if not publications:
        return []
    sorted_pubs = sorted([p for p in publications if p.title], key=publication_sort_key)[:20]
    if not sorted_pubs:
        return []
    lines = ["## Recent Publications\n"]
    for pub in sorted_pubs:
        line = f"- {_citation(pub)}"
        pmid = str(pub.pmid or "").strip()
        if pmid.isdigit():
            line += f" (PMID {pmid})"
        lines.append(line)
    lines.append("")
    return lines


def render_profile_markdown(
    user: User,
    profile: ResearcherProfile,
    publications: TenureScopedPublications | None,
    *,
    grants: GrantSections,
) -> str:
    """Render the exported markdown. Section order is part of the bot-facing contract:
    header, Research Summary, the tag sections, Recent Publications, Active Grants, Past
    Grants (since <year>). A summary line starting with ``#`` is written ``\\#``."""
    _check_inputs(publications, grants)
    lines = _header_lines(user)

    if profile.research_summary:
        lines.append("## Research Summary\n")
        lines.append(_escape_heading_lines(profile.research_summary))
        lines.append("")

    _bullet_section(lines, "Key Methods and Technologies", profile.techniques)
    _bullet_section(lines, "Model Systems", profile.experimental_models)
    _bullet_section(lines, "Disease Areas / Biological Processes", profile.disease_areas)
    _bullet_section(lines, "Key Molecular Targets", profile.key_targets)

    if profile.keywords:
        lines.append("## Keywords\n")
        lines.append(", ".join(profile.keywords))
        lines.append("")

    lines.extend(_publication_lines(publications))
    lines.extend(grant_section_lines(grants))
    return "\n".join(lines)


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
    # Cold Spring Harbor journals: more specific prefixes must come BEFORE
    # the bare 10.1101/ so they win the first-match check. The generic
    # 10.1101/ falls through to the bioRxiv/medRxiv preprint check.
    "10.1101/cshperspect": ["cold spring harbor perspectives"],
    "10.1101/gad": ["genes & development", "genes and development", "genes dev"],
    "10.1101/gr.": ["genome research"],
    "10.1101/sqb": ["cold spring harbor symposia"],
    "10.1101/pdb": ["cold spring harbor protocols"],
    "10.1101/lm": ["learning & memory", "learning and memory"],
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
