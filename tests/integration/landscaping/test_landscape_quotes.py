"""A Landscape member idea's verified quotes
(docs/plans/2026-11-19-landscape-ui-remediation-plan.md, C5): the summary basis reads only
the member's own candidate of each extraction, and the full-text basis reads the extractions
S6's evidence names. Every name below is invented."""

import itertools
import uuid

import pytest

from src.landscaping import epochs
from src.models.landscape import (
    LandscapeDocument,
    LandscapeEpoch,
    LandscapeExtraction,
    LandscapeIdea,
    LandscapeIdeaEvidence,
)
from src.services.landscape_quotes import full_text_quotes, member_quotes
from src.services.landscape_sources import DocInfo

pytestmark = pytest.mark.integration

_KEYS = itertools.count(1)

#: Candidate 1 of the extraction, the member's; candidate 0 belongs to another idea.
MEMBER_CANDIDATE = {
    "target_rubric": [{"item": 1, "status": "met", "quote": "Rubric quote.", "section": "Intro"}],
    "key_results": [
        {"claim": "c", "quote": "Result quote.", "section": "Results"},
        {"claim": "blank", "quote": "   ", "section": "Results"},
    ],
    "novelty_claims": [{"quote": "Novelty quote.", "section": "Discussion"}],
    "limitations": [{"quote": "Limitation quote."}],
    "unmet_need_statements": [{"quote": "Unmet need quote.", "section": "Abstract"}],
}
OTHER_CANDIDATE = {"key_results": [{"claim": "x", "quote": "Other idea quote.", "section": "R"}]}


async def _epoch(db) -> uuid.UUID:
    epoch = LandscapeEpoch(status="candidate", snapshot=epochs.build_snapshot())
    db.add(epoch)
    await db.flush()
    return epoch.id


async def _idea(db, epoch_id: uuid.UUID) -> LandscapeIdea:
    idea = LandscapeIdea(
        epoch_id=epoch_id, idea_key=f"biology;invented-quotes-{next(_KEYS)}",
        idea_type="biology", direction="inhibit",
        targets=[{"hgnc_id": "HGNC:90002", "symbol": "INVQT"}],
        disease={"mondo_id": "MONDO:9999998", "label": "invented sclerosis"},
        version_hash="v1", jhu_group_count=1,
    )
    db.add(idea)
    await db.flush()
    return idea


async def _document(db, title: str) -> LandscapeDocument:
    doc = LandscapeDocument(
        kind="work", canonical_external_id=f"doi:10.9999/invented-quotes-{uuid.uuid4().hex}",
        s1_status="eligible", title=title, doi=f"10.9999/invented-{uuid.uuid4().hex[:8]}",
    )
    db.add(doc)
    await db.flush()
    return doc


async def _extraction(db, doc, epoch_id, *, basis="summary", status="done", raw=None):
    extraction = LandscapeExtraction(
        document_id=doc.id, epoch_id=epoch_id, basis=basis, status=status, raw=raw,
    )
    db.add(extraction)
    await db.flush()
    return extraction


def _info(doc: LandscapeDocument, title: str | None = None) -> DocInfo:
    return DocInfo(id=str(doc.id), title=title if title is not None else doc.title,
                   doi=doc.doi, pmid=None, kind="work")


async def test_member_quotes_candidate_index(db_session):
    epoch_id = await _epoch(db_session)
    idea = await _idea(db_session, epoch_id)
    doc = await _document(db_session, "An invented summary paper")
    extraction = await _extraction(
        db_session, doc, epoch_id, raw={"ideas": [OTHER_CANDIDATE, MEMBER_CANDIDATE]}
    )
    db_session.add(LandscapeIdeaEvidence(
        idea_id=idea.id, extraction_id=extraction.id, candidate_index=1
    ))
    await db_session.flush()

    quotes = await member_quotes(
        db_session, idea.id, None, {str(doc.id): _info(doc, "A short invented title")}
    )

    assert [(q["section"], q["quote"]) for q in quotes] == [
        ("Intro", "Rubric quote."),
        ("Results", "Result quote."),
        ("Discussion", "Novelty quote."),
        ("", "Limitation quote."),
        ("Abstract", "Unmet need quote."),
    ]
    assert {q["basis"] for q in quotes} == {"summary"}
    assert {q["document"] for q in quotes} == {"A short invented title"}
    assert {q["document_id"] for q in quotes} == {str(doc.id)}

    # A document missing from `documents` falls back to its stored title.
    fallback = await member_quotes(db_session, idea.id, None, {})
    assert {q["document"] for q in fallback} == {"An invented summary paper"}


async def test_member_quotes_skips_not_done(db_session):
    epoch_id = await _epoch(db_session)
    idea = await _idea(db_session, epoch_id)
    doc = await _document(db_session, "An invented failed extraction")
    extraction = await _extraction(
        db_session, doc, epoch_id, status="failed:invented", raw={"ideas": [MEMBER_CANDIDATE]}
    )
    db_session.add(LandscapeIdeaEvidence(
        idea_id=idea.id, extraction_id=extraction.id, candidate_index=0
    ))
    await db_session.flush()

    assert await member_quotes(db_session, idea.id, None, {str(doc.id): _info(doc)}) == []


async def test_full_text_quotes_reads_extraction_ids(db_session):
    epoch_id = await _epoch(db_session)
    idea = await _idea(db_session, epoch_id)
    named = await _document(db_session, "An invented full text")
    unnamed = await _document(db_session, "An invented full text S6 did not name")
    raw = {"findings": [{"quote": "Full-text quote.", "section": "Methods"}]}
    named_ext = await _extraction(db_session, named, epoch_id, basis="full_text", raw=raw)
    await _extraction(db_session, unnamed, epoch_id, basis="full_text",
                      raw={"findings": [{"quote": "Unnamed quote."}]})
    deep = {"full_text_evidence": {"documents": [
        {"extraction_id": str(named_ext.id)}, {"extraction_id": "not-a-uuid"}, "junk",
    ]}}

    assert await full_text_quotes(db_session, deep, {}) == (
        ("An invented full text", "Methods", "Full-text quote."),
    )
    assert await full_text_quotes(db_session, deep, {named.id: "Given title"}) == (
        ("Given title", "Methods", "Full-text quote."),
    )
    assert await full_text_quotes(db_session, None, {}) == ()

    quotes = await member_quotes(db_session, idea.id, deep, {str(named.id): _info(named)})
    assert quotes == [{
        "document": "An invented full text", "document_id": str(named.id),
        "section": "Methods", "quote": "Full-text quote.", "basis": "full_text",
    }]
