"""Read an exported persona (``profiles/public/<agent_id>.md``) by its ``## `` headings.

Shared by the engine's channel subscription (spec 2026-10-05 §6.5, D28) and the
agent's own-paper ids (D49). Headings are the ones ``src/services/profile_export.py``
writes; a Research Summary line that starts with ``#`` is exported as ``\\#``, so
summary text cannot open a section here. Dependency-free.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from src.agent.dois import extract_pmids

#: The persona's tag sections, as ``profile_export.render_profile_markdown`` titles them.
TAG_SECTION_HEADINGS: tuple[str, ...] = (
    "Key Methods and Technologies",
    "Model Systems",
    "Disease Areas / Biological Processes",
    "Key Molecular Targets",
    "Keywords",
)
RECENT_PUBLICATIONS_HEADING = "Recent Publications"

_HEADING_RE = re.compile(r"^## (.+?)[ \t]*$", re.MULTILINE)


def sections(persona: str | None) -> dict[str, str]:
    """Each ``## `` heading's body keyed by the heading text. A body runs to the next
    ``## `` line or the end; when a heading repeats, the first occurrence wins."""
    text = persona or ""
    found: dict[str, str] = {}
    matches = list(_HEADING_RE.finditer(text))
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        found.setdefault(match.group(1), text[match.end():end])
    return found


def match_channels(persona: str | None, keywords: Mapping[str, Sequence[str]]) -> set[str]:
    """The channels one of whose keywords starts a word (``\\b<keyword>``, case-insensitive)
    in the persona's tag sections. Heading text, the summary and the publications are not
    searched, so "Key Molecular Targets" never matches "target" and "imaging" never
    matches "aging", while a stem such as "repurpos" still matches "repurposing"."""
    parts = sections(persona)
    text = "\n".join(parts.get(heading, "") for heading in TAG_SECTION_HEADINGS).lower()
    return {
        channel for channel, words in keywords.items()
        if any(re.search(r"\b" + re.escape(word.lower()), text) for word in words)
    }


def recent_publication_pmids(persona: str | None) -> set[str]:
    """PMIDs written in the Recent Publications section (``(PMID n)``, ``PMID: n`` or a
    PubMed link); a PMID anywhere else in the persona is not counted."""
    return extract_pmids(sections(persona).get(RECENT_PUBLICATIONS_HEADING, ""))
