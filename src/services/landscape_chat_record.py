"""The record a landscaping opportunity's chat answers from
(docs/specs/2026-10-30-blackbird-landscaping-design.md §13.5; D50, D63, L50, L53).

Six citation documents — Verdict, Evidence, Disease profile, Panel, Reviews, Rubric — built
from the opportunity's current assessment in the active epoch, in
``assessment_chat_record``'s document shape and digest: every line of stored text is
quoted (``"> "``), and everything the application adds lives in a block's bracketed label
or the document's legend, with record strings put into a label only through
``_label_safe``. There is no interview document.

Unlike the assessment and project records, this one is built PER VIEWER, because what a
viewer may read depends on who they are, and it applies the read model's own hiding
helpers (``src.services.landscape_view``), so the chat never disagrees with the pages:

* the Verdict document goes through ``hide_assessment``: an undecided card of a blind batch
  loses its scores, band, rank and both rationale fields (L53, D56); on the viewer's own
  unsubmitted expert-sample item S9's calls go too (D63). Its rank is the page's
  (``landscape_view.shown_rank``), so no card of an unsuperseded blind batch has one (F2);
* each member's S6 record is the one the page shows (``landscape_view.member_deep``);
* the Disease profile, Evidence and Panel documents take S6's values through ``hide_deep``,
  and the Panel document leaves out the specialist opinions while the expert hiding holds;
* the Reviews document holds only what ``visible_votes`` returns (independent votes, L50);
* a reviewer's record leaves out the staff-only verdict fields
  (``STAFF_ONLY_VERDICT_FIELDS``), as the landscaping read model does for its pages.

Block anchors name sections of the landscaping detail page (``brief``, ``verdict``,
``gating``, ``scores``, ``evidence``, ``disease-profile``, ``panel``, ``reviews``,
``review``). A URL is a link token only where the record quotes it (a source's DOI link).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.landscaping import epochs
from src.landscaping.ideas import dossier_documents
from src.landscaping.structured_lines import structured_line
from src.models.assessment_chat import CHAT_TIER_STAFF
from src.models.landscape import (
    LandscapeAssessment,
    LandscapeConsult,
    LandscapeDecision,
    LandscapeDocument,
    LandscapeExtraction,
    LandscapeIdea,
    LandscapeIdeaAlias,
    LandscapeIdeaEvidence,
    LandscapeLab,
    LandscapeOpportunityVersion,
    LandscapeReview,
    LandscapeReviewEvent,
)
from src.services import landscape_view
from src.services.assessment_chat_record import (
    LEGEND_TAIL,
    STAFF_ONLY_VERDICT_FIELDS,
    ChatRecord,
    _Doc,
    _label_safe,
    _minute,
    quoted_lines,
    tier_for,
    url_tokens_in,
)
from src.services.blackbird_rubric import Rubric

DOC_VERDICT = "verdict"
DOC_EVIDENCE = "evidence"
DOC_DISEASE = "disease"
DOC_PANEL = "panel"
DOC_REVIEWS = "reviews"
DOC_RUBRIC = "rubric"
DOC_TITLES = {
    DOC_VERDICT: "Verdict",
    DOC_EVIDENCE: "Evidence",
    DOC_DISEASE: "Disease profile",
    DOC_PANEL: "Panel",
    DOC_REVIEWS: "Reviews",
    DOC_RUBRIC: "Rubric",
}

_WITHHELD = (
    " Some fields may be withheld from this viewer until they record their own quick decision"
    " on a blind-batch card, or submit their expert judgment on a sample item; a field that is"
    " absent says nothing about its value."
)
#: Not citable (`context`), so the model reads them as the meaning of the fields.
LEGENDS = {
    DOC_VERDICT: (
        "The landscaping pipeline's final assessment of one opportunity: a group of one or"
        " more therapeutic ideas drawn from published research, grants, patents and"
        " technology listings of Johns Hopkins labs. The assessment is an AI model's"
        " judgment in Blackbird's verdict format. The weighted score and band are computed"
        " by the application from the dimension scores and are provisional and advisory."
        " The rank is the best member idea's position in the pipeline's ranked list."
        " Gates: 'met', 'not met' and 'unconfirmed' are different answers." + _WITHHELD
        + LEGEND_TAIL
    ),
    DOC_EVIDENCE: (
        "The evidence behind each member idea: its normalized facets, the database evidence"
        " the pipeline assembled, the deal-breaker flags raised from database records and"
        " the deep analysis's review of them, the source documents with how each qualified,"
        " and quotes from full texts, each checked to appear verbatim in the stored text."
        + _WITHHELD + LEGEND_TAIL
    ),
    DOC_DISEASE: (
        "The deep analysis's disease profile and target biology for each member idea. Each"
        " field names its support: the references it cites, or 'estimate' when it cites"
        " none." + _WITHHELD + LEGEND_TAIL
    ),
    DOC_PANEL: (
        "The deep analysis's own calls on each member idea (its summary, gates, provisional"
        " scores and their rationales), then the opinions of the specialist AI personas it"
        " consulted. Provisional scores precede the final assessment and can differ from it."
        + _WITHHELD + LEGEND_TAIL
    ),
    DOC_REVIEWS: (
        "Quick decisions (pursue or pass), full reviews and status changes written by human"
        " Blackbird reviewers in this application. A viewer sees other people's decisions"
        " and reviews only after recording their own quick decision; until then none are"
        " listed, which says nothing about whether any exist." + LEGEND_TAIL
    ),
    DOC_RUBRIC: (
        "Blackbird's scoring rubric as the assessment's judgment epoch stored it: the gate"
        " definitions and each dimension's scale definition and weight." + LEGEND_TAIL
    ),
}

#: Plain text verdict fields, in the detail page's reading order: (column, label, anchor).
_TEXT_FIELDS = (
    ("company_or_project", "Project label", "brief"),
    ("headline", "Headline", "brief"),
    ("elevator_pitch", "In one minute — the elevator pitch", "brief"),
    ("recommended_next_experiment", "The ask — the recommended next experiment", "brief"),
    ("recommendation", "Recommendation", "verdict"),
    ("confidence", "Confidence label", "verdict"),
    ("funnel_stage", "Funnel stage", "verdict"),
    ("score_rationale", "Why this score — the assessment's own explanation", "verdict"),
    ("rationale", "Rationale", "verdict"),
)
#: List verdict fields, one quoted line per item: (column, label, anchor).
_LIST_FIELDS = (
    ("red_flags", "Red flag", "verdict"),
    ("derisking_milestones", "De-risking milestone", "verdict"),
    ("strengths", "Listed strength", "verdict"),
    ("risks", "Listed risk", "verdict"),
    ("competitive_landscape", "Competitive landscape", "verdict"),
    ("evidence_maturity", "Evidence maturity", "verdict"),
    ("member_notes", "Member note", "verdict"),
)


@dataclass(frozen=True)
class _Member:
    """One member idea as the record shows it to one viewer."""

    idea: Any
    #: S6's values after ``hide_deep``; None when the idea has no passed S6 record.
    deep: Mapping[str, Any] | None
    #: (domain, opinion) pairs; empty while the expert hiding holds.
    consults: tuple[tuple[str, Any], ...]
    documents: tuple[Any, ...]
    labs: Mapping[str, str]
    #: (document title, section, quote) from the idea's full-text extractions.
    quotes: tuple[tuple[str, str, str], ...]


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _lines(value: object) -> list[str]:
    """A stored value as quoted lines: a string as itself, a list item by item, a mapping
    as one JSON line; empty values yield nothing."""
    if value is None or isinstance(value, bool):
        return [] if value is None else [str(value).lower()]
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [line for item in value for line in _lines(item)]
    if isinstance(value, Mapping):
        return [_json(value)] if value else []
    return [str(value)]


def _get(item: object, key: str) -> Any:
    """``key`` of a dict or an attribute of a row (``visible_votes`` may return either)."""
    return item.get(key) if isinstance(item, Mapping) else getattr(item, key, None)


def _when(value: object) -> str | None:
    return f"{_minute(value)} UTC" if isinstance(value, datetime) else None


def _withheld_block(doc: _Doc, hiding: Any, *, anchor: str) -> None:
    reasons = []
    if getattr(hiding, "blind", False):
        reasons.append("this is a blind-batch card you have not decided on")
    if getattr(hiding, "expert", False):
        reasons.append("this is your unsubmitted expert-sample item")
    if reasons:
        doc.add(f"Withheld from you for now — {'; '.join(reasons)}", anchor=anchor)


def _gate_title(rubric: Rubric | None, key: str) -> str:
    gate = rubric.gating.get(key) if rubric is not None else None
    title = gate.get("title") if isinstance(gate, Mapping) else None
    return _label_safe(title or key)


def _dimension(rubric: Rubric | None, key: str) -> tuple[str, str]:
    """(title, weight note) of a rubric dimension, the key itself when unknown."""
    for d in rubric.dimensions if rubric is not None else ():
        if d.key == key:
            return _label_safe(d.title), f"; {d.weight}% weight"
    return _label_safe(key), ""


def _gate_blocks(
    doc: _Doc, gating: object, rationales: object, rubric: Rubric | None, *, who: str,
    anchor: str, prefix: str = "",
) -> None:
    """One block per gate: its state in the label, then ``who``'s rationale quoted."""
    gating = gating if isinstance(gating, Mapping) else {}
    rationales = rationales if isinstance(rationales, Mapping) else {}
    for key in sorted(set(gating) | set(rationales)):
        state = gating.get(key)
        label = f"{prefix}Gate — {_gate_title(rubric, key)}"
        if isinstance(state, str):
            label += f": {_label_safe(state.replace('_', ' '))}"
        doc.add(f"{label}; then {who}'s rationale", _lines(rationales.get(key)), anchor=anchor)


def _score_blocks(
    doc: _Doc, scores: object, rationales: object, rubric: Rubric | None, *, who: str,
    anchor: str, prefix: str = "",
) -> None:
    """One block per dimension: its score in the label, then ``who``'s rationale quoted."""
    scores = scores if isinstance(scores, Mapping) else {}
    rationales = rationales if isinstance(rationales, Mapping) else {}
    scale = f" of {rubric.scale_max}" if rubric is not None else ""
    for key in sorted(set(scores) | set(rationales)):
        title, weight = _dimension(rubric, key)
        label = f"{prefix}Dimension — {title}{weight}"
        score = scores.get(key)
        if isinstance(score, int | float) and not isinstance(score, bool):
            label += f"; score {score}{scale}"
        doc.add(f"{label}; then {who}'s rationale", _lines(rationales.get(key)), anchor=anchor)


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


def _key_point_blocks(doc: _Doc, verdict: Mapping[str, Any]) -> None:
    key_points = verdict.get("key_points")
    if isinstance(key_points, Mapping):
        for group in sorted(key_points):
            doc.add(
                f"Key points — {_label_safe(group.replace('_', ' '))}",
                _lines(key_points[group]), anchor="brief",
            )
    elif key_points:
        doc.add("Key points", _lines(key_points), anchor="brief")
    if verdict.get("claim_support"):
        doc.add(
            "Key-point support — per group and bullet, the cited references or 'estimate'",
            _lines(verdict["claim_support"]), anchor="brief",
        )


def _score_summary(doc: _Doc, verdict: Mapping[str, Any], epoch_label: str) -> None:
    lines = []
    if verdict.get("weighted_score") is not None:
        lines.append(f"weighted score: {verdict['weighted_score']:.2f}")
    if verdict.get("band"):
        lines.append(f"band: {verdict['band']}")
    if verdict.get("rank") is not None:
        lines.append(f"rank: {verdict['rank']}")
    if lines:
        doc.add(
            f"Score, band and rank — computed by the application; provisional and advisory;"
            f" {epoch_label}",
            lines, anchor="verdict",
        )


def _verdict_doc(
    opportunity: Mapping[str, Any], verdict: Mapping[str, Any], members: list[_Member],
    rubric: Rubric | None, hiding: Any,
) -> _Doc:
    doc = _Doc(DOC_VERDICT)
    epoch_label = (
        f"rubric {_label_safe(verdict.get('rubric_version') or 'unstamped')},"
        f" epoch {_label_safe(opportunity.get('epoch_id'))}"
    )
    doc.add(
        f"Opportunity — its label and theme; version {_label_safe(opportunity.get('version'))}",
        _lines([opportunity.get("label"), opportunity.get("theme")]), anchor="brief",
    )
    structured = _lines(verdict.get("structured_lines")) or [
        structured_line(m.idea) for m in members
    ]
    doc.add("Member ideas — one structured line per idea", structured, anchor="brief")
    _withheld_block(doc, hiding, anchor="verdict")
    for column, label, anchor in _TEXT_FIELDS:
        if _lines(verdict.get(column)):
            doc.add(label, _lines(verdict[column]), anchor=anchor)
    _key_point_blocks(doc, verdict)
    _score_summary(doc, verdict, epoch_label)
    _gate_blocks(doc, verdict.get("gating"), verdict.get("gating_rationales"), rubric,
                 who="the assessment", anchor="gating")
    _score_blocks(doc, verdict.get("scores"), verdict.get("dimension_rationales"), rubric,
                  who="the assessment", anchor="scores")
    for column, label, anchor in _LIST_FIELDS:
        items = _lines(verdict.get(column))
        for i, item in enumerate(items, 1):
            doc.add(f"{label} {i} of {len(items)}", [item], anchor=anchor)
    return doc


def _source_lines(doc_row: Any, labs: Mapping[str, str]) -> list[str]:
    kind = f"{doc_row.kind} ({doc_row.document_type})" if doc_row.document_type else doc_row.kind
    published = doc_row.published_on.isoformat() if doc_row.published_on else "undated"
    lines = [doc_row.title or "untitled", f"{kind}; {published}"]
    lab = labs.get(doc_row.lab_key) or doc_row.lab_key
    if lab:
        lines.append(f"lab: {lab}")
    for name, value in (
        ("institution", doc_row.qualifying_institution),
        ("qualified as", doc_row.role_basis),
        ("license", doc_row.license),
        ("PMID", doc_row.pmid),
    ):
        if value:
            lines.append(f"{name}: {value}")
    if doc_row.doi:
        lines.append(f"https://doi.org/{doc_row.doi}")
    return lines


def _member_evidence(doc: _Doc, i: int, n: int, m: _Member) -> None:
    idea = m.idea
    facts = [
        structured_line(idea),
        f"type: {idea.idea_type}",
        f"stage: {idea.stage or 'not recorded'}",
        f"evidence grade: {idea.evidence_grade or 'not recorded'}",
        f"Johns Hopkins labs: {idea.jhu_group_count}",
    ]
    if idea.outside_group_count is not None:
        facts.append(f"outside groups: {idea.outside_group_count}")
    doc.add(f"Idea {i} of {n} — its structured line and facets", facts, anchor="evidence")
    if idea.evidence_summary:
        doc.add(f"Idea {i} — database evidence", _lines(idea.evidence_summary), anchor="evidence")
    if idea.data_flags:
        doc.add(
            f"Idea {i} — deal-breaker flags raised from database records",
            _lines(idea.data_flags), anchor="evidence",
        )
    if m.deep is not None and m.deep.get("flags_reviewed"):
        doc.add(
            f"Idea {i} — the deep analysis's review of those flags: confirmed or cleared",
            _lines(m.deep["flags_reviewed"]), anchor="evidence",
        )
    for k, row in enumerate(m.documents, 1):
        doc.add(
            f"Idea {i}, source {k} of {len(m.documents)} — title, kind and date, lab, how it"
            " qualified, license and link",
            _source_lines(row, m.labs), anchor="evidence",
        )
    for k, (title, section, quote) in enumerate(m.quotes, 1):
        where = f", section {_label_safe(section)}" if section else ""
        doc.add(
            f"Idea {i}, verified full-text quote {k} of {len(m.quotes)} — from"
            f" {_label_safe(title) or 'an untitled source'}{where}",
            [quote], anchor="evidence",
        )


def _evidence_doc(members: list[_Member]) -> _Doc:
    doc = _Doc(DOC_EVIDENCE)
    for i, m in enumerate(members, 1):
        _member_evidence(doc, i, len(members), m)
    if not members:
        doc.add("No member ideas — none could be loaded in this epoch", anchor="evidence")
    return doc


def _disease_doc(members: list[_Member]) -> _Doc:
    doc = _Doc(DOC_DISEASE)
    for i, m in enumerate(members, 1):
        if m.deep is None:
            doc.add(f"Idea {i} — no completed deep analysis", anchor="disease-profile")
            continue
        profile = m.deep.get("disease_profile")
        for field in sorted(profile) if isinstance(profile, Mapping) else ():
            entry = profile[field] if isinstance(profile[field], Mapping) else {}
            support = entry.get("support")
            cited = "estimate" if support in (None, "estimate") else "; ".join(_lines(support))
            doc.add(
                f"Idea {i}, disease profile — {_label_safe(field.replace('_', ' '))};"
                f" support: {_label_safe(cited)}",
                _lines(entry.get("value")), anchor="disease-profile",
            )
        biology = m.deep.get("target_biology")
        for k, entry in enumerate(biology if isinstance(biology, list) else [], 1):
            entry = entry if isinstance(entry, Mapping) else {}
            doc.add(
                f"Idea {i}, target biology {k} — support:"
                f" {_label_safe('; '.join(_lines(entry.get('support'))) or 'estimate')}",
                _lines(entry.get("line")), anchor="disease-profile",
            )
    if not members:
        doc.add("No member ideas — none could be loaded in this epoch", anchor="disease-profile")
    return doc


def _member_panel(doc: _Doc, i: int, m: _Member, rubric: Rubric | None, hiding: Any) -> None:
    deep = m.deep or {}
    if m.deep is None:
        doc.add(f"Idea {i} — no completed deep analysis", anchor="panel")
    if _lines(deep.get("summary")):
        doc.add(f"Idea {i} — the deep analysis's summary", _lines(deep["summary"]), anchor="panel")
    if deep.get("provisional_weighted_score") is not None:
        doc.add(
            f"Idea {i} — provisional weighted score, computed by the application",
            [f"{deep['provisional_weighted_score']:.2f}"], anchor="panel",
        )
    prefix = f"Idea {i}, deep analysis — "
    _gate_blocks(doc, deep.get("gating"), deep.get("gating_rationales"), rubric,
                 who="the deep analysis", anchor="panel", prefix=prefix)
    _score_blocks(doc, deep.get("provisional_scores"), deep.get("dimension_rationales"), rubric,
                  who="the deep analysis", anchor="panel", prefix=prefix)
    if getattr(hiding, "expert", False):
        doc.add(
            f"Idea {i} — the specialist opinions are withheld until you submit your expert"
            " judgment",
            anchor="panel",
        )
        return
    for domain, opinion in m.consults:
        doc.add(
            f"Idea {i} — the {_label_safe(domain)} specialist's opinion",
            _lines(opinion), anchor="panel",
        )


def _panel_doc(members: list[_Member], rubric: Rubric | None, hiding: Any) -> _Doc:
    doc = _Doc(DOC_PANEL)
    _withheld_block(doc, hiding, anchor="panel")
    for i, m in enumerate(members, 1):
        _member_panel(doc, i, m, rubric, hiding)
    if not members:
        doc.add("No member ideas — none could be loaded in this epoch", anchor="panel")
    return doc


def _reviews_doc(decisions: list, reviews: list, events: list) -> _Doc:
    doc = _Doc(DOC_REVIEWS)
    for i, d in enumerate(decisions, 1):
        parts = [
            f"Quick decision {i} of {len(decisions)}",
            _label_safe(_get(d, "reviewer_name")) or "unnamed reviewer",
            _label_safe(_get(d, "decision")),
        ]
        tags = _lines(_get(d, "reason_tags"))
        if tags:
            parts.append(f"tags: {_label_safe(', '.join(tags))}")
        when = _when(_get(d, "updated_at") or _get(d, "created_at"))
        if when:
            parts.append(when)
        if _get(d, "edited"):
            parts.append("edited")
        doc.add(" · ".join(parts) + " · then the reason", _lines(_get(d, "reason")),
                anchor="reviews")
    for i, r in enumerate(reviews, 1):
        parts = [
            f"Full review {i} of {len(reviews)}",
            _label_safe(_get(r, "reviewer_name")) or "unnamed reviewer",
            f"rating {_label_safe(_get(r, 'score'))} of 5",
        ]
        when = _when(_get(r, "updated_at") or _get(r, "created_at"))
        if when:
            parts.append(when)
        lines = _lines(_get(r, "comment"))
        if _get(r, "dimension_scores"):
            lines.append(_json(_get(r, "dimension_scores")))
        doc.add(" · ".join(parts) + " · then the comment and dimension scores", lines,
                anchor="reviews")
    for i, e in enumerate(events, 1):
        when = _when(_get(e, "created_at"))
        doc.add(
            f"Status change {i} of {len(events)} — {_label_safe(_get(e, 'action'))} by"
            f" {_label_safe(_get(e, 'actor_name')) or 'unnamed'}" + (f", {when}" if when else ""),
            anchor="reviews",
        )
    if not (decisions or reviews or events):
        doc.add(
            "No decisions or reviews listed — none are recorded, or you have not yet recorded"
            " your own quick decision",
            anchor="reviews",
        )
    return doc


def _rubric_doc(rubric: Rubric | None) -> _Doc:
    doc = _Doc(DOC_RUBRIC)
    if rubric is None:
        doc.add("Rubric unavailable — the epoch's stored rubric could not be read", anchor="review")
        return doc
    doc.add(
        f"Rubric {_label_safe(rubric.version)} — dimensions scored {rubric.scale_min} to"
        f" {rubric.scale_max}; band advance from {rubric.advance_min}, conditional from"
        f" {rubric.conditional_min}; then the banding note",
        _lines(rubric.banding_advisory_note), anchor="review",
    )
    for key in sorted(rubric.gating):
        gate = rubric.gating[key]
        doc.add(
            f"Gate definition — {_gate_title(rubric, key)}",
            _lines(gate.get("description") if isinstance(gate, Mapping) else None),
            anchor="review",
        )
    for d in rubric.dimensions:
        doc.add(
            f"Scale definition — {_label_safe(d.title)}; {d.weight}% weight",
            _lines(d.anchors), anchor="review",
        )
    return doc


def _record(docs: list[_Doc], tier: str) -> ChatRecord:
    documents = tuple(
        {
            "type": "document",
            "source": {
                "type": "content",
                "content": [{"type": "text", "text": block} for block in d.blocks],
            },
            "title": DOC_TITLES[d.key],
            "context": LEGENDS[d.key],
            "citations": {"enabled": True},
        }
        for d in docs
    )
    tokens = frozenset(
        token
        for d in docs
        for block in d.blocks
        for token in url_tokens_in("\n".join(quoted_lines(block)))
    )
    digest = hashlib.sha256(
        json.dumps(list(documents), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        .encode("utf-8")
    ).hexdigest()[:12]
    return ChatRecord(
        tier=tier,
        documents=documents,
        targets=tuple(tuple(d.targets) for d in docs),
        url_tokens=tokens,
        sha256_12=digest,
    )


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _values(row: Any) -> dict[str, Any]:
    """A row's column values keyed by column name, the shape the hiding helpers take."""
    return {column.key: getattr(row, column.key) for column in row.__table__.columns}


async def _current_assessment(
    db: AsyncSession, opportunity_id: uuid.UUID, epoch_id: uuid.UUID
) -> LandscapeAssessment | None:
    """The opportunity's newest complete assessment in ``epoch_id``."""
    A = LandscapeAssessment
    return await db.scalar(
        select(A)
        .where(A.opportunity_id == opportunity_id, A.epoch_id == epoch_id, A.status == "done")
        .order_by(A.opportunity_version.desc(), A.created_at.desc(), A.id.desc())
        .limit(1)
    )


async def _idea_for_key(db: AsyncSession, epoch_id: uuid.UUID, key: str) -> LandscapeIdea | None:
    """The epoch's idea for ``key``, directly or through the epoch's aliases (D36, D64)."""
    idea = await db.scalar(
        select(LandscapeIdea).where(
            LandscapeIdea.epoch_id == epoch_id, LandscapeIdea.idea_key == key
        )
    )
    if idea is not None:
        return idea
    return await db.scalar(
        select(LandscapeIdea)
        .join(LandscapeIdeaAlias, LandscapeIdeaAlias.idea_id == LandscapeIdea.id)
        .where(LandscapeIdeaAlias.epoch_id == epoch_id, LandscapeIdeaAlias.old_key == key)
    )


async def _documents(db: AsyncSession, idea: LandscapeIdea) -> tuple[list, dict[str, str]]:
    """The idea's dossier documents (D35) and their labs' display names by lab key."""
    rows = (
        await db.scalars(
            select(LandscapeDocument)
            .join(LandscapeExtraction, LandscapeExtraction.document_id == LandscapeDocument.id)
            .join(
                LandscapeIdeaEvidence,
                LandscapeIdeaEvidence.extraction_id == LandscapeExtraction.id,
            )
            .where(
                LandscapeIdeaEvidence.idea_id == idea.id,
                LandscapeDocument.merged_into.is_(None),
            )
        )
    ).all()
    listed = dossier_documents(idea, list({row.id: row for row in rows}.values()))
    lab_keys = sorted({row.lab_key for row in listed if row.lab_key})
    labs: dict[str, str] = {}
    if lab_keys:
        result = await db.execute(
            select(LandscapeLab.lab_key, LandscapeLab.display_name)
            .where(LandscapeLab.lab_key.in_(lab_keys))
        )
        labs = {key: name for key, name in result.all() if name}
    return listed, labs


def _quotes_in(raw: object, section: str = "") -> list[tuple[str, str]]:
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
                found.extend(_quotes_in(raw[key], here))
    elif isinstance(raw, list):
        for item in raw:
            found.extend(_quotes_in(item, section))
    return found


async def _quotes(
    db: AsyncSession, deep: Mapping[str, Any] | None, documents: list
) -> tuple[tuple[str, str, str], ...]:
    """The verified quotes of the full-text extractions S6's evidence names; none when that
    evidence is hidden or absent."""
    evidence = deep.get("full_text_evidence") if deep is not None else None
    entries = evidence.get("documents") if isinstance(evidence, Mapping) else None
    ids = []
    for entry in entries if isinstance(entries, list) else []:
        try:
            ids.append(uuid.UUID(str(entry.get("extraction_id"))))
        except (AttributeError, ValueError):
            continue
    if not ids:
        return ()
    titles = {row.id: row.title or "" for row in documents}
    rows = (
        await db.execute(
            select(LandscapeExtraction.document_id, LandscapeExtraction.raw, LandscapeDocument.title)
            .join(LandscapeDocument, LandscapeDocument.id == LandscapeExtraction.document_id)
            .where(LandscapeExtraction.id.in_(ids), LandscapeExtraction.status == "done")
            .order_by(LandscapeExtraction.document_id)
        )
    ).all()
    return tuple(
        (titles.get(document_id) or title or "", section, quote)
        for document_id, raw, title in rows
        for section, quote in _quotes_in(raw)
    )


async def _member(db: AsyncSession, idea: LandscapeIdea, hiding: Any) -> _Member:
    deep_row = await landscape_view.member_deep(db, idea)
    deep = landscape_view.hide_deep(_values(deep_row), hiding) if deep_row is not None else None
    consults: tuple[tuple[str, Any], ...] = ()
    if deep_row is not None and not getattr(hiding, "expert", False):
        rows = (
            await db.scalars(
                select(LandscapeConsult)
                .where(LandscapeConsult.deep_id == deep_row.id)
                .order_by(LandscapeConsult.domain)
            )
        ).all()
        consults = tuple((row.domain, row.opinion) for row in rows if row.opinion)
    documents, labs = await _documents(db, idea)
    return _Member(
        idea=idea, deep=deep, consults=consults, documents=tuple(documents), labs=labs,
        quotes=await _quotes(db, deep, documents),
    )


async def _members_and_rank(
    db: AsyncSession, assessment: LandscapeAssessment,
    version: LandscapeOpportunityVersion | None, hiding: Any,
) -> tuple[list[_Member], int | None]:
    """Each member idea in ``idea_key`` order (one per idea, a missing one skipped), resolved in
    the assessment's epoch, and the page's rank for the opportunity
    (``landscape_view.shown_rank``; ``LandscapeAssessment.rank`` is never written)."""
    keys = list(version.member_idea_keys or []) if version is not None else []
    ideas: dict[uuid.UUID, LandscapeIdea] = {}
    for key in sorted(keys):
        idea = await _idea_for_key(db, assessment.epoch_id, key)
        if idea is not None:
            ideas.setdefault(idea.id, idea)
    members = [await _member(db, idea, hiding) for idea in ideas.values()]
    rank = await landscape_view.shown_rank(db, assessment.opportunity_id, version, list(ideas))
    return members, rank


async def _votes(db: AsyncSession, user: Any, opportunity_id: uuid.UUID) -> tuple:
    """The decisions, reviews and status events ``user`` may see (independent votes, L50)."""
    def _rows(model):
        return (
            select(model)
            .where(model.opportunity_id == opportunity_id)
            .order_by(model.created_at, model.id)
        )

    decisions = list((await db.scalars(_rows(LandscapeDecision))).all())
    reviews = list((await db.scalars(_rows(LandscapeReview))).all())
    events = list((await db.scalars(_rows(LandscapeReviewEvent))).all())
    return landscape_view.visible_votes(user, opportunity_id, decisions, reviews, events)


async def _rubric(db: AsyncSession, epoch_id: uuid.UUID) -> Rubric | None:
    """The rubric the epoch stored (spec §7.11), or None when it has no snapshot."""
    try:
        return (await epochs.epoch_inputs(db, epoch_id)).rubric
    except LookupError:
        return None


async def build_landscape_chat_record(
    db: AsyncSession, opportunity_id: uuid.UUID, user: Any
) -> ChatRecord | None:
    """``user``'s record of the opportunity's current assessment in the active epoch, or None
    (the router's 404) unless ``landscape_view.can_view`` admits them and that assessment
    exists. The tier is ``tier_for(user)``; see the module docstring for what each viewer's
    record leaves out."""
    if not await landscape_view.can_view(db, user, opportunity_id):
        return None
    epoch = await epochs.active_epoch(db)
    if epoch is None:
        return None
    assessment = await _current_assessment(db, opportunity_id, epoch.id)
    if assessment is None:
        return None
    version = await db.scalar(
        select(LandscapeOpportunityVersion).where(
            LandscapeOpportunityVersion.opportunity_id == opportunity_id,
            LandscapeOpportunityVersion.version == assessment.opportunity_version,
        )
    )
    hiding = await landscape_view.hiding_for(db, user, opportunity_id)
    members, rank = await _members_and_rank(db, assessment, version, hiding)
    values = _values(assessment)
    values["rank"] = rank
    verdict = dict(landscape_view.hide_assessment(values, hiding))
    tier = tier_for(user)
    if tier != CHAT_TIER_STAFF:
        for key in STAFF_ONLY_VERDICT_FIELDS:
            verdict.pop(key, None)
    opportunity = {
        "epoch_id": epoch.id,
        "version": assessment.opportunity_version,
        "label": version.label if version is not None else None,
        "theme": version.theme if version is not None else None,
    }
    decisions, reviews, events = await _votes(db, user, opportunity_id)
    rubric = await _rubric(db, epoch.id)
    docs = [
        _verdict_doc(opportunity, verdict, members, rubric, hiding),
        _evidence_doc(members),
        _disease_doc(members),
        _panel_doc(members, rubric, hiding),
        _reviews_doc(list(decisions), list(reviews), list(events)),
        _rubric_doc(rubric),
    ]
    return _record(docs, tier)
