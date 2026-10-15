"""Export a ResearcherProfile from the database to the persona markdown the lab agent reads.

Sanitising (C10 of the 2026-10-15 publication-handling design; main SPEC EXP-1, EXP-2;
Q18): every stored text field passes through ``one_line`` or ``escape_block`` when it is
rendered, so the rule covers rows stored before it too. The rendered text keeps this
invariant:

1. Its only line break is ``\\n``.
2. Every line whose first non-blank character is ``#`` is the title line or an exporter
   ``## `` heading: ``persona_sections.PUBLIC_PROFILE_SECTIONS``, ``## Active Grants`` and
   ``## Past Grants (since <year>)``, each at most once.
3. No user-text line starts with a code fence (three backticks or three tildes), and none
   is a run of only ``=`` or only ``-`` (a setext underline or a rule).
4. Every line under ``## Recent Publications`` is an exporter-built ``- `` item.
5. Escaping is idempotent: an escaped line starts with ``\\``.

Mid-line ``#`` runs (``C#``), bullets, numbered lines and inline Markdown are untouched; a
line-leading ``#1`` becomes ``\\#1`` (spec 2026-10-05 D49). Links come from
``publication_links`` (Q28, F22).
"""

import logging
import re
from pathlib import Path

from src.agent.persona_sections import (
    LINE_BREAK_CHARS,
    RECENT_PUBLICATIONS_HEADING,
    RESEARCH_SUMMARY_HEADING,
    TAG_SECTION_HEADINGS,
    normalize_line_breaks,
)
from src.models import ResearcherProfile, User
from src.services.fs import atomic_write_text
from src.services.grant_sections import GrantSections, grant_section_lines
from src.services.publication_links import doi_url, pubmed_url
from src.services.tenure_scope import TenureScopedPublications, publication_sort_key

logger = logging.getLogger(__name__)

PROFILES_DIR = Path("profiles/public")

_LINE_BREAK_RUN = re.compile(r"[ \t]*[" + LINE_BREAK_CHARS + r"]+[ \t]*")
_HEADING_LINE = re.compile(r"^([ \t]*)#", re.MULTILINE)
_FENCE_LINE = re.compile(r"^([ \t]*)(?=`{3}|~{3})", re.MULTILINE)
_SETEXT_LINE = re.compile(r"^([ \t]*)(?=(?:=+|-+)[ \t]*$)", re.MULTILINE)


def one_line(text: object) -> str:
    """``text`` on one line (C10): ``None`` is ``""``; each run of line breaks
    (``LINE_BREAK_CHARS``), with the spaces and tabs around it, becomes one space; the
    result is stripped."""
    if text is None:
        return ""
    return _LINE_BREAK_RUN.sub(" ", str(text)).strip()


def escape_block(text: object) -> str:
    """``text`` safe to place under a persona heading (C10): ``None`` is ``""``; every line
    break becomes ``\\n``; the whole is stripped; then a backslash goes before the first
    non-blank character of every line that starts with ``#`` (a heading, D49), a code
    fence, or a run of only ``=`` or only ``-`` (a setext underline or a rule). The three
    rules never apply to the same line, and an escaped line starts with ``\\``, so
    escaping is idempotent."""
    if text is None:
        return ""
    block = normalize_line_breaks(str(text)).strip()
    block = _HEADING_LINE.sub(r"\1\\#", block)
    block = _FENCE_LINE.sub(r"\1\\", block)
    return _SETEXT_LINE.sub(r"\1\\", block)


def _inline(text: object) -> str:
    """A single-line field: ``escape_block(one_line(text))``."""
    return escape_block(one_line(text))


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
    """Append a ``## title`` bullet list of the non-empty ``_inline`` items to ``lines``;
    nothing when none is left."""
    cleaned = [item for item in map(_inline, items or ()) if item]
    if cleaned:
        lines.append(f"## {title}\n")
        lines.extend(f"- {item}" for item in cleaned)
        lines.append("")


def _citation(pub) -> str:
    """One publication's citation text and link (spec 2026-10-05 §6.3, P28; F22, EXP-5):
    the DOI link when the DOI is verified against PubMed's record for the PMID
    (``doi_verified``), is DOI-shaped and fits the journal; else the PubMed page of an
    all-digit PMID; else a DOI-shaped DOI that fits the journal; else no link. Title and
    journal are ``_inline``."""
    parts = []
    title = _inline(pub.title).rstrip(".")
    if title:
        parts.append(title)
    journal = _inline(pub.journal)
    if journal:
        parts.append(f"*{journal}*")
    if pub.year:
        parts.append(f"({pub.year})")
    citation = ". ".join(parts) + "."
    doi_href = doi_url(pub.doi) if pub.doi and _validate_doi_journal(pub.doi, pub.journal) else None
    pubmed_href = pubmed_url(pub.pmid)
    if doi_href and pub.doi_verified:
        link = doi_href
    elif pubmed_href:
        link = pubmed_href
    elif doi_href:
        link = doi_href
    else:
        link = None          # the citation ends with "." and no URL
    return f"{citation} {link}" if link else citation


def _publication_lines(publications: TenureScopedPublications | None) -> list[str]:
    """Recent Publications section (up to 20, publication_sort_key order); empty when none.
    A row whose ``_inline`` title is empty is dropped before the sort and the cap. Each
    line ends with its link (``_citation``) and, for an all-digit PMID, ``(PMID n)``
    (D49)."""
    if not publications:
        return []
    titled = [p for p in publications if _inline(p.title).rstrip(".")]
    sorted_pubs = sorted(titled, key=publication_sort_key)[:20]
    if not sorted_pubs:
        return []
    lines = [f"## {RECENT_PUBLICATIONS_HEADING}\n"]
    for pub in sorted_pubs:
        line = f"- {_citation(pub)}"
        pmid = str(pub.pmid or "").strip()
        if pubmed_url(pmid):
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
    Grants (since <year>). Every field is sanitised (module docstring); the section headings
    are ``persona_sections``' constants (main SPEC EXP-11)."""
    _check_inputs(publications, grants)
    lines = _header_lines(user)

    summary = escape_block(profile.research_summary)
    if summary:
        lines.append(f"## {RESEARCH_SUMMARY_HEADING}\n")
        lines.append(summary)
        lines.append("")

    for heading, items in zip(
        TAG_SECTION_HEADINGS[:4],
        (profile.techniques, profile.experimental_models, profile.disease_areas,
         profile.key_targets),
        strict=True,
    ):
        _bullet_section(lines, heading, items)

    keywords = ", ".join(k for k in map(_inline, profile.keywords or ()) if k)
    if keywords:
        lines.append(f"## {TAG_SECTION_HEADINGS[4]}\n")
        lines.append(keywords)
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
    "10.1101/gr": ["genome research"],
    "10.1101/sqb": ["cold spring harbor symposia"],
    "10.1101/pdb": ["cold spring harbor protocols"],
    "10.1101/lm": ["learning & memory", "learning and memory"],
    "10.1101/": ["biorxiv", "medrxiv", "preprint"],
    "10.1074/jbc": ["journal of biological chemistry"],
    "10.1073/pnas": ["proceedings of the national academy"],
    "10.15252/embj": ["embo journal"],
    "10.1371/journal.pgen": ["plos genetics"],
    "10.1371/journal.pbio": ["plos biology"],
    "10.1371/journal.pone": ["plos one"],
    "10.1021/acs.jproteome": ["journal of proteome research"],
    "10.1093/bioinformatics": ["bioinformatics"],
    "10.1016/j.bpj": ["biophysical journal"],
    "10.1016/j.sbi": ["current opinion in structural biology"],
}

#: Each prefix matches only when the character after it is not a letter (main SPEC
#: EXP-7), so 10.1016/j.cell does not claim 10.1016/j.cellsig and the bare 10.1101/ does
#: not claim 10.1101/mcs. Compiled once; dict order and first match win as before.
_DOI_PUBLISHER_RULES: tuple[tuple[str, re.Pattern[str], list[str]], ...] = tuple(
    (prefix, re.compile(re.escape(prefix.lower()) + r"(?![a-z])"), patterns)
    for prefix, patterns in _DOI_PUBLISHER_PATTERNS.items()
)


def _validate_doi_journal(doi: str, journal: str | None) -> bool:
    """Check whether a DOI plausibly belongs to the given journal.

    Returns True if validation passes or is inconclusive (unknown prefix).
    Returns False only when there's a clear mismatch.
    """
    if not doi or not journal:
        return True  # Can't validate — assume ok

    doi_lower = doi.lower()
    journal_lower = journal.lower()

    for prefix, rule, expected_patterns in _DOI_PUBLISHER_RULES:
        if rule.match(doi_lower):
            # This DOI has a known prefix — check if journal matches
            if any(pat in journal_lower for pat in expected_patterns):
                return True
            logger.warning(
                "DOI/journal mismatch: DOI %s (prefix %s) vs journal '%s'",
                doi, prefix, journal,
            )
            return False

    return True  # Unknown prefix — can't invalidate
