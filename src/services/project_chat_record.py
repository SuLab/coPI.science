"""The record one project's chat answers from, and its opening questions
(docs/specs/2026-10-27-projects-pages-redesign-design.md §8.4, §8.6).

Five citation documents — Project, Conversation, Specialists, Research, Reviews — built
from the SAME read model the details page renders, ``build_project_detail(viewer=None,
is_admin=False, impersonating=False)``, with ``assessment_chat_record``'s document shape,
URL-token rule and digest. Two rules make the record checkable, as there:

* the record is the page: every QUOTED line of a block (each line after the first,
  prefixed ``"> "``) is a stored value, or a word the page prints beside it ("matches:",
  "Asked:", a group heading), exactly as the details page renders it for every role;
* no label or confidence: a specialist's ``verdict_signal``, ``confidence`` and
  ``raw_opinion`` never enter the record (D55; the read model does not select them),
  and neither do the admin-only costs, the calls link or the ``research`` JSON. The
  legends name no label or confidence either. Everything the application adds — what a
  value means, a date, which state a card is in — lives in the block's bracketed LABEL
  line or the document's ``context`` legend, through ``_label_safe``.

``tests/integration/test_project_chat_parity.py`` enforces the first rule for an admin,
a manager and a reviewer, and checks that every anchor a block cites is an element id
on that role's page. The page shows every role the same project apart from the
admin-only cost and calls link, so the record is the same for both tiers; ``tier`` is
kept on the record for the chat service.

A URL is a link token (``ChatRecord.url_tokens``) only where the page links it: in the
lab's reply and the thread's posts (rendered through the markdown sanitizer) and in a
source's link. The posting is plain text on the page, so its URLs are not tokens.

The builder is pure and deterministic and never raises on a malformed stored value; it
skips what it cannot render.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.models.assessment_chat import CHAT_TIER_REVIEWER, CHAT_TIER_STAFF
from src.models.hub_project import HubProject
from src.services.assessment_chat_record import (
    LEGEND_TAIL,
    ChatRecord,
    _Doc,
    _label_safe,
    _minute,
    quoted_lines,
    url_tokens_in,
)
from src.services.assessment_chat_suggestions import (
    KIND_TEMPLATE,
    MAX_SUGGESTIONS,
    Suggestion,
)
from src.services.project_plan import TEMPLATE_FIELDS
from src.services.projects_view import build_project_detail

DOC_PROJECT = "project"
DOC_CONVERSATION = "conversation"
DOC_SPECIALISTS = "specialists"
DOC_RESEARCH = "research"
DOC_REVIEWS = "reviews"
DOC_ORDER = (DOC_PROJECT, DOC_CONVERSATION, DOC_SPECIALISTS, DOC_RESEARCH, DOC_REVIEWS)
DOC_TITLES = {
    DOC_PROJECT: "Project",
    DOC_CONVERSATION: "Conversation",
    DOC_SPECIALISTS: "Specialists",
    DOC_RESEARCH: "Research",
    DOC_REVIEWS: "Reviews",
}

#: Not citable (`context`), so the model reads them as the meaning of the fields. None
#: of them may name a specialist label or a confidence (D55).
LEGENDS = {
    DOC_PROJECT: (
        "One project as stored by the application. BlackbirdBot, Blackbird's AI scouting"
        " hub, chose a new use of a lab's work from published research, posted it with an"
        " opening plan, and refined the plan with the lab's AI agent in a Slack thread. A"
        " close, when recorded, holds the final plan and each side's stated position:"
        " 'Agreed', 'Not agreed' or 'No position stated'. The positions are what each side"
        " said and decide nothing. The outcome is Ready when the plan's fields are filled"
        " (time and budget aside, which staff settle) and the close records no scientific"
        " disagreement; Not agreed when a field was left open, a disagreement was recorded,"
        " or the close did not say whether one remains; and Not finished when the project"
        " never reached a recorded close, or the close was cut off before it filled a field."
        " The project states which reason applies. A plan field revised after"
        " posting is marked as changed, and its value as first posted is quoted in a block"
        " of its own. A tool or model 'in the lab's profile' matches an item of the lab's"
        " public profile — the profile as it stood at the close where the close recorded"
        " the marks, otherwise today's; 'stated by the lab' found no such item."
        + LEGEND_TAIL
    ),
    DOC_CONVERSATION: (
        "The project's posting and its Slack thread, between AI agents: BlackbirdBot posted"
        " the project, and the lab's agent, speaking on the PI's behalf from the PI's"
        " public profile, replied. Their statements are their own claims, not verified"
        " statements by the PI." + LEGEND_TAIL
    ),
    DOC_SPECIALISTS: (
        "Specialist AI personas BlackbirdBot consulted in the project's thread while"
        " refining the plan. Each block quotes what one consult asked and what that"
        " specialist's stored reply established, raised as concerns and suggested asking."
        " A reply that was cut off stopped before it finished, so what is quoted is"
        " partial." + LEGEND_TAIL
    ),
    DOC_RESEARCH: (
        "The published sources BlackbirdBot's research found when choosing the project,"
        " grouped as the page groups them, each with the domain of the specialist research"
        " that found it and, where the page links it, its link." + LEGEND_TAIL
    ),
    DOC_REVIEWS: (
        "Feedback written by human Blackbird reviewers in this application. The rating"
        " rates the project itself, whether the idea and its plan are worth pursuing, from"
        " 1 (weak) to 5 (strong); it is not a grade of the bots. 'Fund' or 'Don't fund' is"
        " whether that reviewer would fund the project as planned." + LEGEND_TAIL
    ),
}

_TIME_AND_BUDGET = dict(TEMPLATE_FIELDS)["time_and_budget"]

GENERIC_PROJECT_QUESTION = "What is being proposed, in plain terms?"


class _ProjectDoc(_Doc):
    """A ``_Doc`` that also keeps the quoted text of the blocks whose URLs the page
    links, so only those become link tokens."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.linked: list[str] = []

    def add_linked(self, label: str, lines: Iterable[object], *, anchor: str | None) -> None:
        self.add(label, lines, anchor=anchor)
        self.linked.append("\n".join(quoted_lines(self.blocks[-1])))


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _dicts(value: object) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _when(value: object) -> str | None:
    return f"{_minute(value)} UTC" if isinstance(value, datetime) else None


def _overview_section(doc: _Doc, p: Mapping[str, Any]) -> None:
    """The brief's headline, outcome, lab and dates, summary, reasons and anchors."""
    doc.add("Project title", [_text(p.get("title"))], anchor="brief")
    parts = [f"Outcome: {_label_safe(p.get('outcome_label')) or 'not recorded'}"]
    if p.get("reason_label"):
        parts.append(f"reason: {_label_safe(p['reason_label'])}")
    parts.append(f"lab: the agent id {_label_safe(p.get('lab')) or 'not recorded'}")
    for key, name in (
        ("run_started_at", "run started"),
        ("posted_at", "posted"),
        ("finished_at", "finished"),
    ):
        when = _when(p.get(key))
        if when:
            parts.append(f"{name} {when}")
    doc.add(" · ".join(parts), anchor="brief")
    if _text(p.get("summary")):
        doc.add("In one minute — the project's summary", [_text(p["summary"])], anchor="brief")
    if _text(p.get("why_chosen")):
        doc.add(
            "Why this use — BlackbirdBot's reasons for choosing it",
            [_text(p["why_chosen"])],
            anchor="brief",
        )
    for a in _dicts(p.get("anchors")):
        if _text(a.get("item")):
            section = _label_safe(a.get("section")) or "an unnamed section"
            doc.add(
                f"Built on — an item of the lab's public profile, section {section}",
                [_text(a["item"])],
                anchor="brief",
            )
    others = [o for o in _dicts(p.get("others")) if _text(o.get("use"))]
    for i, o in enumerate(others, 1):
        lines = [_text(o["use"])]
        if _text(o.get("note")):
            lines.append(_text(o["note"]))
        doc.add(
            f"Also considered {i} of {len(others)} — another use BlackbirdBot considered,"
            " then its note",
            lines,
            anchor="others",
        )


def _plan_section(doc: _Doc, p: Mapping[str, Any]) -> None:
    """The plan card: one block per field, a changed field's first value, the tools."""
    which = (
        "the plan recorded at the close"
        if p.get("has_close")
        else "the plan as posted; no close was recorded"
    )
    for row in _dicts(p.get("plan_rows")):
        name = _label_safe(row.get("label")) or "unnamed field"
        value = _text(row.get("value"))
        label = f"Plan — {name}; {which}"
        if row.get("changed"):
            label += "; changed since posting"
        doc.add(label, [value] if value else [], anchor="plan")
        if row.get("changed") and _text(row.get("opening_value")):
            doc.add(
                f"Plan — {name}, as first posted",
                [_text(row["opening_value"])],
                anchor="plan",
            )
    tools = [t for t in _dicts(p.get("tool_rows")) if _text(t.get("item"))]
    for i, t in enumerate(tools, 1):
        lines = [_text(t["item"])]
        if t.get("in_profile"):
            lines.append("in the lab's profile")
            if _text(t.get("matched")):
                lines.append(f"matches: {_text(t['matched'])}")
        else:
            lines.append("stated by the lab")
        doc.add(
            f"The lab's tools and models — {i} of {len(tools)}, then its mark",
            lines,
            anchor="plan",
        )


def _positions_section(doc: _Doc, p: Mapping[str, Any]) -> None:
    """Positions, then gaps or acknowledged limits, then disagreements."""
    if not p.get("has_close"):
        doc.add("Positions — no close recorded", anchor="positions")
        return
    doc.add(
        f"Positions at the close — BlackbirdBot: {_label_safe(p.get('hub_position'))};"
        f" the lab: {_label_safe(p.get('lab_position'))}",
        anchor="positions",
    )
    gaps = _strings(p.get("gaps"))
    name = "Acknowledged limit" if p.get("outcome_kind") == "ready" else "Gap"
    for i, gap in enumerate(gaps, 1):
        doc.add(f"{name} {i} of {len(gaps)} — recorded at the close", [gap], anchor="positions")
    disagreements = _strings(p.get("disagreements"))
    for i, item in enumerate(disagreements, 1):
        doc.add(
            f"Disagreement {i} of {len(disagreements)} — recorded at the close",
            [item],
            anchor="positions",
        )


def _project_doc(p: Mapping[str, Any]) -> _Doc:
    doc = _Doc(DOC_PROJECT)
    _overview_section(doc, p)
    _plan_section(doc, p)
    _positions_section(doc, p)
    return doc


def _timeline(p: Mapping[str, Any], kinds: tuple[str, ...]) -> list[Mapping[str, Any]]:
    return [
        e
        for e in _dicts(p.get("timeline"))
        if e.get("kind") in kinds and isinstance(e.get("anchor"), str)
    ]


def _conversation_doc(p: Mapping[str, Any]) -> _ProjectDoc:
    doc = _ProjectDoc(DOC_CONVERSATION)
    messages = _timeline(p, ("posting", "post"))
    if not any(m.get("kind") == "posting" for m in messages):
        doc.add(
            "Posting unavailable — no posting text is stored or logged for this project",
            anchor=None,
        )
    for i, m in enumerate(messages, 1):
        parts = [f"Message {i} of {len(messages)}", _label_safe(m.get("speaker")) or "unnamed"]
        when = _when(m.get("at"))
        if when:
            parts.append(when)
        if m.get("kind") == "posting":
            parts.append("the project's posting")
            if m.get("partial"):
                parts.append("part of this posting may not have reached Slack")
            doc.add(" · ".join(parts), [m.get("text") or ""], anchor=m["anchor"])
            continue
        if m.get("closed_thread"):
            parts.append("closed the thread")
        doc.add_linked(" · ".join(parts), [m.get("text") or ""], anchor=m["anchor"])
    if p.get("thread_unavailable"):
        doc.add(
            "Thread messages unavailable — the posts of this project's thread are not in the"
            " message log",
            anchor=None,
        )
    return doc


def _specialists_doc(p: Mapping[str, Any]) -> _Doc:
    doc = _Doc(DOC_SPECIALISTS)
    consults = _timeline(p, ("consult",))
    if not consults:
        doc.add(
            "No consults — no specialist was consulted in this project's thread", anchor=None
        )
        return doc
    for k, c in enumerate(consults, 1):
        concerns = _strings(c.get("concerns"))
        domain = _label_safe(c.get("domain")) or "unnamed"
        parts = [f"Consult {k} of {len(consults)}", f"{domain} specialist"]
        count = c.get("concern_count")
        count = count if isinstance(count, int) and not isinstance(count, bool) else len(concerns)
        parts.append(f"{count} concern{'' if count == 1 else 's'}")
        when = _when(c.get("at"))
        if when:
            parts.append(when)
        if c.get("truncated"):
            parts.append(
                "reply cut off — it stopped before it finished, so what is quoted is partial"
            )
        lines: list[str] = []
        if _text(c.get("question")):
            lines.append(f"Asked: {_text(c['question'])}")
        for heading, items in (
            ("Established:", _strings(c.get("established"))),
            ("Concerns:", concerns),
            ("Questions to ask:", _strings(c.get("questions_to_ask"))),
        ):
            if items:
                lines.append(heading)
                lines.extend(f"- {item}" for item in items)
        doc.add(" · ".join(parts), lines, anchor=c["anchor"])
    return doc


def _research_doc(p: Mapping[str, Any]) -> _ProjectDoc:
    doc = _ProjectDoc(DOC_RESEARCH)
    sources = [
        (_text(group.get("label")), item)
        for group in _dicts(p.get("sources_by_kind"))
        for item in _dicts(group.get("items"))
    ]
    if not sources:
        doc.add("Research — no sources recorded", anchor="research")
        return doc
    for i, (group, item) in enumerate(sources, 1):
        label = f"Source {i} of {len(sources)} — its group, identifier, title and link"
        if item.get("specialist"):
            label += f"; found by the {_label_safe(item['specialist'])} specialist"
        shown = (group, _text(item.get("ident")), _text(item.get("title")))
        lines = [line for line in shown if line]
        url = _text(item.get("url"))
        if url:
            lines.append(url)
            doc.add_linked(label, lines, anchor="research")
        else:
            doc.add(label, lines, anchor="research")
    return doc


def _reviews_doc(p: Mapping[str, Any]) -> _Doc:
    doc = _Doc(DOC_REVIEWS)
    reviews = _dicts(p.get("reviews"))
    for i, r in enumerate(reviews, 1):
        name = _label_safe(r.get("reviewer_name")) or "unnamed reviewer"
        parts = [f"Review {i} of {len(reviews)}", name]
        if r.get("rating") is not None:
            parts.append(f"rating {_label_safe(r['rating'])} of 5")
        if isinstance(r.get("fund"), bool):
            parts.append("Fund" if r["fund"] else "Don't fund")
        when = _when(r.get("created_at"))
        if when:
            parts.append(f"written {when}")
        if r.get("edited"):
            parts.append("edited")
        comment = _text(r.get("comment"))
        if comment:
            parts.append("the reviewer's comment")
        doc.add(" · ".join(parts), [comment] if comment else [], anchor="reviews")
    if not reviews:
        doc.add("No reviews — no reviews of this project have been recorded", anchor="reviews")
    return doc


def build_project_chat_record(detail: dict[str, Any], *, tier: str) -> ChatRecord:
    """The record of the project in ``detail`` (the ``{"project": {...}}`` dict of a
    viewer-free, non-admin ``build_project_detail``). Pure; both tiers get the same
    documents (see the module docstring). Raises ValueError for an unknown tier."""
    if tier not in (CHAT_TIER_STAFF, CHAT_TIER_REVIEWER):
        raise ValueError(f"unknown chat tier {tier!r}")
    p = detail["project"]
    docs: list[_Doc] = [
        _project_doc(p),
        _conversation_doc(p),
        _specialists_doc(p),
        _research_doc(p),
        _reviews_doc(p),
    ]
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
        if isinstance(d, _ProjectDoc)
        for text in d.linked
        for token in url_tokens_in(text)
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


async def load_project_chat_record(
    db: AsyncSession, project_id: uuid.UUID, *, tier: str
) -> tuple[ChatRecord, HubProject] | None:
    """The record of one project and its row, or None when the project does not exist.
    Built from the viewer-free, non-admin detail for EVERY tier, so no viewer key, cost
    or calls link can enter it."""
    detail = await build_project_detail(
        db, project_id, viewer=None, is_admin=False, impersonating=False
    )
    if detail is None:
        return None
    project = await db.get(HubProject, project_id)
    if project is None:
        return None
    return build_project_chat_record(detail, tier=tier), project


def project_template_suggestions(detail: dict[str, Any]) -> list[Suggestion]:
    """At most MAX_SUGGESTIONS opening questions, each anchored to the card it is about
    (spec §8.6): the open points of a Not agreed project or the acknowledged limits of a
    Ready one, the specialists' concerns, how the project builds on the lab's work, what
    the plan's time and budget cover; then the generic question while there is room.
    None asks for a rating or a fund decision (D43)."""
    p = detail["project"]
    out: list[Suggestion] = []
    kind = p.get("outcome_kind")
    if kind == "not_agreed":
        out.append(Suggestion(
            text="What did BlackbirdBot and the lab leave unresolved, and why?",
            anchor="positions", kind=KIND_TEMPLATE,
        ))
    elif kind == "ready":
        out.append(Suggestion(
            text="Which of the acknowledged limits matter most for this plan?",
            anchor="positions", kind=KIND_TEMPLATE,
        ))
    if any(_strings(e.get("concerns")) for e in _dicts(p.get("evidence"))):
        out.append(Suggestion(
            text="Which specialist concerns does the final plan not address?",
            anchor="evidence", kind=KIND_TEMPLATE,
        ))
    if any(_text(a.get("item")) for a in _dicts(p.get("anchors"))):
        out.append(Suggestion(
            text="How does this project build on the lab's own work?",
            anchor="brief", kind=KIND_TEMPLATE,
        ))
    if any(
        r.get("label") == _TIME_AND_BUDGET and _text(r.get("value"))
        for r in _dicts(p.get("plan_rows"))
    ):
        out.append(Suggestion(
            text="What do the plan's time and budget cover, and what is left out?",
            anchor="plan", kind=KIND_TEMPLATE,
        ))
    if len(out) < MAX_SUGGESTIONS:
        out.append(Suggestion(text=GENERIC_PROJECT_QUESTION, anchor=None, kind=KIND_TEMPLATE))
    return out[:MAX_SUGGESTIONS]


def project_page_suggestions(
    detail: dict[str, Any], user: Any, *, chat_enabled: bool
) -> list[Suggestion]:
    """What the details page offers ``user``: the template set, or nothing when the
    chat is off or the session is impersonating (the page then renders no chat control)."""
    if not chat_enabled or getattr(user, "_is_impersonated", False):
        return []
    return project_template_suggestions(detail)
