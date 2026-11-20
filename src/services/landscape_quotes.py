"""Verified quotes of a Landscape member idea
(docs/plans/2026-11-19-landscape-ui-remediation-plan.md, contract C5).

Two bases. The summary basis is S3's extraction of each document the idea rests on: the
candidate idea that fed it (``landscape_idea_evidence.candidate_index``) carries quotes in
``target_rubric``, ``key_results``, ``novelty_claims``, ``limitations`` and
``unmet_need_statements`` (``src/landscaping/schemas.py``). The full-text basis is S6's
full-text extractions that its ``full_text_evidence`` names, which the landscaping chat
record also quotes (``src.services.landscape_chat_record``).

Quotes stay visible under the expert rule: they are verbatim source text, not the pipeline's
judgement. The blind and expert hidings reach the full-text basis only through the
``deep_values`` the caller passes (after ``landscape_view.hide_deep``).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.landscape import LandscapeDocument, LandscapeExtraction, LandscapeIdeaEvidence
from src.services.landscape_sources import DocInfo, Quote, short_title

#: The candidate idea's quote-bearing fields, in the order their quotes are listed.
_SUMMARY_FIELDS = (
    "target_rubric", "key_results", "novelty_claims", "limitations", "unmet_need_statements",
)


def quotes_in(raw: object, section: str = "") -> list[tuple[str, str]]:
    """Every ``(section, quote)`` in a full-text extraction's parsed output, in order: an
    object with a string ``quote`` is one, with its ``section`` when it names one."""
    found: list[tuple[str, str]] = []
    if isinstance(raw, Mapping):
        here = raw.get("section") if isinstance(raw.get("section"), str) else section
        quote = raw.get("quote")
        if isinstance(quote, str) and quote.strip():
            found.append((here, quote.strip()))
        for key in sorted(raw):
            if key != "quote":
                found.extend(quotes_in(raw[key], here))
    elif isinstance(raw, list):
        for item in raw:
            found.extend(quotes_in(item, section))
    return found


async def _full_text_rows(
    db: AsyncSession, deep_values: Mapping[str, Any] | None
) -> list[tuple[uuid.UUID, Any, str | None]]:
    """``(document id, raw, document title)`` of the done full-text extractions S6's
    ``full_text_evidence`` names, by document id; none when that evidence is hidden or
    absent."""
    evidence = deep_values.get("full_text_evidence") if deep_values is not None else None
    entries = evidence.get("documents") if isinstance(evidence, Mapping) else None
    ids = []
    for entry in entries if isinstance(entries, list) else []:
        try:
            ids.append(uuid.UUID(str(entry.get("extraction_id"))))
        except (AttributeError, ValueError):
            continue
    if not ids:
        return []
    rows = (
        await db.execute(
            select(LandscapeExtraction.document_id, LandscapeExtraction.raw, LandscapeDocument.title)
            .join(LandscapeDocument, LandscapeDocument.id == LandscapeExtraction.document_id)
            .where(LandscapeExtraction.id.in_(ids), LandscapeExtraction.status == "done")
            .order_by(LandscapeExtraction.document_id)
        )
    ).all()
    return [(document_id, raw, title) for document_id, raw, title in rows]


async def full_text_quotes(
    db: AsyncSession, deep_values: Mapping[str, Any] | None, titles: Mapping[uuid.UUID, str]
) -> tuple[tuple[str, str, str], ...]:
    """``(document title, section, quote)`` of the verified quotes in the full-text
    extractions S6's evidence names; none when that evidence is hidden or absent. The title
    is ``titles[document id]`` when set, else the document's own title, else ""."""
    return tuple(
        (titles.get(document_id) or title or "", section, quote)
        for document_id, raw, title in await _full_text_rows(db, deep_values)
        for section, quote in quotes_in(raw)
    )


def _candidate_quotes(raw: object, index: int) -> list[tuple[str, str]]:
    """``(section, quote)`` of the summary fields of ``raw["ideas"][index]``, in field order;
    none when the extraction lacks that candidate."""
    ideas = raw.get("ideas") if isinstance(raw, Mapping) else None
    if not isinstance(ideas, list) or not 0 <= index < len(ideas):
        return []
    candidate = ideas[index]
    if not isinstance(candidate, Mapping):
        return []
    found: list[tuple[str, str]] = []
    for field in _SUMMARY_FIELDS:
        entries = candidate.get(field)
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, Mapping):
                continue
            quote = entry.get("quote")
            if isinstance(quote, str) and quote.strip():
                section = entry.get("section")
                found.append((section if isinstance(section, str) else "", quote.strip()))
    return found


async def member_quotes(
    db: AsyncSession,
    idea_id: uuid.UUID,
    deep_values: Mapping[str, Any] | None,
    documents: Mapping[str, DocInfo],
) -> list[Quote]:
    """The member idea's verified quotes: summary basis then full-text basis (C5).

    Summary quotes come from each ``landscape_idea_evidence`` row of ``idea_id`` whose
    extraction is done, ordered by document id then candidate index. Full-text quotes come
    from ``deep_values`` (S6's values as the page shows them) via ``full_text_quotes``'s
    query. ``document`` is the short title of the ``documents`` entry (keyed by the document
    uuid string) when there is one, else the document's stored title, else "". A repeated
    ``(document id, quote)`` is listed once, at its first place.
    """
    summary = (
        await db.execute(
            select(
                LandscapeExtraction.document_id, LandscapeExtraction.raw,
                LandscapeDocument.title, LandscapeIdeaEvidence.candidate_index,
            )
            .select_from(LandscapeIdeaEvidence)
            .join(
                LandscapeExtraction, LandscapeExtraction.id == LandscapeIdeaEvidence.extraction_id
            )
            .join(LandscapeDocument, LandscapeDocument.id == LandscapeExtraction.document_id)
            .where(LandscapeIdeaEvidence.idea_id == idea_id, LandscapeExtraction.status == "done")
            .order_by(LandscapeExtraction.document_id, LandscapeIdeaEvidence.candidate_index)
        )
    ).all()
    full_text = await _full_text_rows(db, deep_values)
    found: list[Quote] = []
    seen: set[tuple[str, str]] = set()

    def _add(document_id: uuid.UUID, title: str | None, pairs: list[tuple[str, str]],
             basis: Literal["summary", "full_text"]) -> None:
        doc = documents.get(str(document_id))
        name = short_title(doc.get("title")) if doc is not None else (title or "")
        for section, quote in pairs:
            if (str(document_id), quote) in seen:
                continue
            seen.add((str(document_id), quote))
            found.append({"document": name, "document_id": str(document_id),
                          "section": section, "quote": quote, "basis": basis})

    for document_id, raw, title, index in summary:
        _add(document_id, title, _candidate_quotes(raw, index), "summary")
    for document_id, raw, title in full_text:
        _add(document_id, title, quotes_in(raw), "full_text")
    return found
