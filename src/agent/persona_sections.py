"""Read an exported persona (``profiles/public/<agent_id>.md``) by its ``## `` headings.

Shared by the engine's channel subscription (spec 2026-10-05 §6.5, D28), the agent's
own-paper ids (D49) and the activation gate (``src/services/persona_lifecycle.py``). The
headings are defined here, and ``src/services/profile_export.py`` writes them (main SPEC
EXP-11). The reader and the exporter share one line-break set, ``LINE_BREAK_CHARS`` (C10
of the 2026-10-15 publication-handling design): ``sections`` normalises every break in it
to ``\\n`` first, and the exporter writes no other break, so the engine (which reads with
universal newlines) and the gate (which reads the raw file) agree on where a section ends.
A Research Summary line that starts with ``#`` is exported as ``\\#``, so summary text
cannot open a section here. Dependency-free.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from src.agent.dois import extract_pmids

#: Every line break the persona exporter neutralises and this reader splits on (C10):
#: CR, LF, VT, FF, the information separators FS, GS and RS, NEL, LINE SEPARATOR and
#: PARAGRAPH SEPARATOR, which is exactly the set ``str.splitlines`` splits on, so no reader
#: that splits lines can find a heading the exporter did not write. The one definition;
#: ``profile_export`` and ``profile_edit`` import it.
LINE_BREAK_CHARS: str = "\r\n\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"
_TO_LF = str.maketrans(dict.fromkeys(LINE_BREAK_CHARS, "\n"))

#: The persona's summary section, as ``profile_export.render_profile_markdown`` titles it.
RESEARCH_SUMMARY_HEADING = "Research Summary"

#: The persona's tag sections, as ``profile_export.render_profile_markdown`` titles them.
TAG_SECTION_HEADINGS: tuple[str, ...] = (
    "Key Methods and Technologies",
    "Model Systems",
    "Disease Areas / Biological Processes",
    "Key Molecular Targets",
    "Keywords",
)
RECENT_PUBLICATIONS_HEADING = "Recent Publications"

#: Every ``## `` section the exporter writes from the profile, in its order (main SPEC
#: EXP-11). The grant headings are ``grant_sections.grant_blocks``'.
PUBLIC_PROFILE_SECTIONS: tuple[str, ...] = (
    RESEARCH_SUMMARY_HEADING, *TAG_SECTION_HEADINGS, RECENT_PUBLICATIONS_HEADING,
)


def normalize_line_breaks(text: str) -> str:
    """``text`` with ``\\r\\n``, then every other character of ``LINE_BREAK_CHARS``,
    replaced by ``\\n``."""
    return text.replace("\r\n", "\n").translate(_TO_LF)

_HEADING_RE = re.compile(r"^## (.+?)[ \t]*$", re.MULTILINE)


def sections(persona: str | None) -> dict[str, str]:
    """Each ``## `` heading's body keyed by the heading text. A body runs to the next
    ``## `` line or the end; when a heading repeats, the first occurrence wins. The text is
    normalised with ``normalize_line_breaks`` first, and the bodies are slices of the
    normalised text."""
    text = normalize_line_breaks(persona or "")
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
