"""The record one assessment's chat answers from
(docs/specs/2026-09-24-assessment-chat-design.md §4).

Five citation documents built from the SAME context the detail page renders —
``build_assessment_detail(admin_view=False)`` — so what a tier's chat can quote is
what that tier's page shows (the parity rule, §4.1). Two rules make that checkable:

* every QUOTED line of a block (each line after the first, prefixed ``"> "``) is a
  stored value exactly as the page renders it;
* everything the application adds — what a value means, which revision it came
  from, which state a card is in — lives in the block's first, bracketed LABEL line
  or in the document's ``context`` legend, never in a quoted line.

``tests/integration/test_assessment_chat_parity.py`` enforces the first rule, per
tier and prose format, by tokenizing: every lowercased 3-or-more-character token of
every quoted line must occur somewhere in that tier's rendered ``<main>`` text (not
an exact match — the page reflows prose and rewrites bare URLs into "cited paper"
links, so containment is what the token check proves). The same test also checks
that the staff-only verdict fields land in the staff record and nowhere in the
reviewer's, and that every anchor a block cites resolves to a rendered element id on
that tier's page. A label interpolates record strings only through ``_label_safe``,
so no stored text can end the label line or open a second one; and every line of
stored text is quoted, so none of it can pose as a label (the review bot's rule,
``src/services/review_bot.py``).

The builder is pure and deterministic: stored values only (never ``now()``, never
the viewer), in a fixed order, so one tier's record of one stored state is
byte-for-byte the same for every viewer — which is what lets the prompt cache work.
It never raises on a malformed stored value; like ``derive_strengths_and_risks`` it
skips what it cannot render.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.models import OpportunityAssessment
from src.models.assessment_chat import CHAT_TIER_REVIEWER, CHAT_TIER_STAFF
from src.services.assessment_detail import KEY_POINT_GROUPS, build_assessment_detail
from src.services.prose_citations import (
    _URL_RE,
    _is_linkable,
    _split_trailing,
    _truncated_at_backslash,
)
from src.services.rubric_revisions import (
    PROVENANCE_ARCHIVED,
    PROVENANCE_LIVE,
    PROVENANCE_UNKNOWN,
)

#: Mirrors templates/admin/_assessment_detail_body.html:236-244. The page gates these
#: in the TEMPLATE, not in build_assessment_detail, so the chat must gate them itself.
#: tests/integration/test_assessment_chat_parity.py binds the two.
STAFF_ONLY_VERDICT_FIELDS = ("strengths", "risks", "competitive_landscape", "evidence_maturity")

_STAFF_ONLY_LABELS = {
    "strengths": "Hub-listed strength (the hub's own words)",
    "risks": "Hub-listed risk (the hub's own words)",
    "competitive_landscape": "Competitive landscape (the hub's own words)",
    "evidence_maturity": "Evidence maturity (the hub's own words)",
}

DOC_VERDICT = "verdict"
DOC_INTERVIEW = "interview"
DOC_PANEL = "panel"
DOC_REVIEWS = "reviews"
DOC_RUBRIC = "rubric"
DOC_ORDER = (DOC_VERDICT, DOC_INTERVIEW, DOC_PANEL, DOC_REVIEWS, DOC_RUBRIC)
DOC_TITLES = {
    DOC_VERDICT: "Verdict",
    DOC_INTERVIEW: "Interview transcript",
    DOC_PANEL: "Specialist panel findings",
    DOC_REVIEWS: "Human reviews",
    DOC_RUBRIC: "Scoring rubric",
}

LEGEND_TAIL = (
    " Only the first line of each block, in square brackets, is written by the"
    " application; every line beginning '> ' is quoted record content."
)
#: The verdict legend's closing sentence about absent fields differs by tier: a
#: reviewer's record also omits STAFF_ONLY_VERDICT_FIELDS, which "never asked" alone
#: would misstate as never having existed.
_VERDICT_ABSENT_NOTE = {
    CHAT_TIER_STAFF: "A field that is absent was never asked of this verdict.",
    CHAT_TIER_REVIEWER: (
        "A field that is absent was either never asked of this verdict or is not shown"
        " to your role."
    ),
}
#: §4.3. Not citable (`context`), so the model reads them as the meaning of the fields.
LEGENDS = {
    DOC_VERDICT: (
        "The hub's verdict on one screening interview, as stored by the application. The"
        " hub is an AI agent; these are its judgments. The weighted score and band are"
        " computed by the application from the hub's dimension scores. The recommendation"
        " and the band are separate fields and can disagree. 'pass' means pass on the deal"
        " — do not fund — and is displayed as 'decline'. Gates: 'met', 'not met' (asked and"
        " failed) and 'unconfirmed' (never established) are different answers; only 'not"
        " met' can justify discounting the idea. A gate block also quotes the rubric's"
        " definition of that gate when this row was scored against the current rubric."
        " {absent_note}" + LEGEND_TAIL
    ),
    DOC_INTERVIEW: (
        "An interview in a Slack channel between AI agents. BlackbirdBot is Blackbird's"
        " scouting hub. The lab's agent speaks on the PI's behalf from the PI's public"
        " profile; its statements are its own claims, not verified statements by the PI."
        " Panel notes are the hub's one-line summaries of specialist consults. A sender"
        " that is not a registered agent shows only an unverified display name, which may"
        " not be who it claims to be." + LEGEND_TAIL
    ),
    DOC_PANEL: (
        "Specialist AI consultants the hub asked during the interview. Signals: 'blocking',"
        " 'gap' and 'adequate' (current); 'caution' and 'clear' (historical). 'adequate'"
        " means the evidence meets the bar for this stage, not that nothing was raised. A"
        " consult whose reply was cut off carries no opinion; a read state other than"
        " 'parsed' means the stored signal is a default, not something the specialist"
        " said." + LEGEND_TAIL
    ),
    DOC_REVIEWS: (
        "Feedback written by human Blackbird reviewers in this application. The score"
        " rates the proposal's merit from 1 to 5; it is not a grade of this assessment."
        + LEGEND_TAIL
    ),
    DOC_RUBRIC: (
        "Scale definitions from Blackbird's current scoring rubric, as the page's review"
        " form shows them. The gate definitions are in the Verdict document." + LEGEND_TAIL
    ),
}

def _legend_for(doc_key: str, tier: str) -> str:
    """The document's legend for ``tier`` — every document's legend is tier-invariant
    except the verdict document's, whose closing sentence names what an absent field
    means for that tier."""
    if doc_key == DOC_VERDICT:
        return LEGENDS[DOC_VERDICT].format(absent_note=_VERDICT_ABSENT_NOTE[tier])
    return LEGENDS[doc_key]


#: The longest a record string may run inside a label (§4.2).
LABEL_VALUE_CHARS = 80

_WHITESPACE_RE = re.compile(r"\s+")
_PARAGRAPH_BREAK_RE = re.compile(r"\n\s*\n")

_GATE_STATES = {"met": "met", "not_met": "not met", "unconfirmed": "unconfirmed — never asked"}
_FEEDBACK_MODES = {"learn": "Learn", "log_only": "Don't learn — log only"}
#: The panel card's heading and sentence per `panel_state` (template :626-662).
_PANEL_LABELS = {
    "gap": (
        "Specialist panel incomplete — the domains quoted below were never consulted,"
        " so the verdict is recorded but not fully vetted; treat the score as provisional"
    ),
    "unverified": (
        "Specialist panel unverified — the floor could not be checked for this verdict"
        " (no consult was recorded for anyone, or the verdict named no lab); not evidence"
        " of a gap, and not evidence of a complete panel either"
    ),
    "not_owed": (
        "Specialist panel: not required — a {recommendation} is not held to the"
        " specialist floor, so no consult requirement was evaluated for it; any consults"
        " were made anyway and are not a completed panel; this is not a verification"
    ),
    "verified": (
        "Specialist panel: no gap recorded — nothing the verdict's own content owed a"
        " specialist was left unconsulted"
    ),
}
_PANEL_UNRECORDED = (
    "Specialist panel: not recorded — this row does not record whether a specialist"
    " panel was owed, so nothing can be said about one either way; not evidence of a"
    " gap, and not a verification"
)


@dataclass(frozen=True)
class BlockTarget:
    """Where one record block lives: its document, the page element it came from
    (an element id, or None), and its label without the brackets."""

    doc: str
    anchor: str | None
    label: str


@dataclass(frozen=True)
class ChatRecord:
    """One tier's record of one assessment. ``documents`` never carry
    ``cache_control`` — the request builder adds it — so ``sha256_12`` is a pure
    function of the stored state."""

    tier: str
    documents: tuple[dict[str, Any], ...]
    targets: tuple[tuple[BlockTarget, ...], ...]
    url_tokens: frozenset[str]
    sha256_12: str

    def target(self, document_index: object, block_index: object) -> BlockTarget | None:
        """The block a citation's ``(document_index, start_block_index)`` names, or None."""
        if type(document_index) is not int or type(block_index) is not int:
            return None
        if not 0 <= document_index < len(self.targets):
            return None
        blocks = self.targets[document_index]
        if not 0 <= block_index < len(blocks):
            return None
        return blocks[block_index]


def tier_for(user: Any) -> str:
    """`staff` for admin and manager, `reviewer` otherwise (the caller has already
    passed get_review_user's predicate, so "otherwise" is a reviewer)."""
    return CHAT_TIER_STAFF if getattr(user, "is_staff", False) else CHAT_TIER_REVIEWER


def _label_safe(value: object) -> str:
    """A record string made safe to put inside a label: every run of whitespace
    (newlines included) becomes one space, square brackets are removed, and the
    result is clipped to LABEL_VALUE_CHARS."""
    text = _WHITESPACE_RE.sub(" ", "" if value is None else str(value))
    text = text.replace("[", "").replace("]", "").strip()
    if len(text) > LABEL_VALUE_CHARS:
        text = text[: LABEL_VALUE_CHARS - 1].rstrip() + "…"
    return text


def _quote(value: object) -> list[str]:
    text = "" if value is None else str(value)
    return ["> " + line for line in text.splitlines()]


def _block(label: str, lines: Iterable[object]) -> str:
    out = [f"[{label}]"]
    for value in lines:
        out.extend(_quote(value))
    return "\n".join(out)


def quoted_lines(block_text: str) -> list[str]:
    """A block's record text: every line after the label, without its ``"> "``."""
    return [
        line[2:] if line.startswith("> ") else line.removeprefix(">")
        for line in block_text.split("\n")[1:]
    ]


def normalize_url_token(raw: str) -> str:
    """A URL cut by the page's own rule (``prose_citations._split_trailing``):
    trailing sentence punctuation and an unbalanced ``)`` are not part of it."""
    return _split_trailing(raw)[0]


def url_tokens_in(text: str) -> list[str]:
    """Every https URL in ``text``, tokenized exactly as the page links record URLs."""
    out: list[str] = []
    for match in _URL_RE.finditer(text):
        url = normalize_url_token(match.group())
        if (
            url.startswith("https://")
            and _is_linkable(url)
            and not _truncated_at_backslash(text, match.end())
        ):
            out.append(url)
    return out


class _Doc:
    def __init__(self, key: str) -> None:
        self.key = key
        self.blocks: list[str] = []
        self.targets: list[BlockTarget] = []

    def add(self, label: str, lines: Iterable[object] = (), *, anchor: str | None) -> None:
        self.blocks.append(_block(label, lines))
        self.targets.append(BlockTarget(doc=self.key, anchor=anchor, label=label))


def _minute(value: datetime) -> str:
    # The page renders `created_at.strftime('%Y-%m-%d %H:%M')` with no conversion;
    # asyncpg hands timestamptz back in UTC.
    return value.strftime("%Y-%m-%d %H:%M")


def _paragraphs(value: object) -> list[str]:
    if not isinstance(value, str):
        return []
    return [p.strip() for p in _PARAGRAPH_BREAK_RE.split(value) if p.strip()]


def _items(value: object) -> list[str]:
    """A JSONB list column's items as the page renders them; anything but a list
    yields nothing (the page renders a non-list staff field as nothing too)."""
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def _verdict_doc(detail: dict[str, Any], tier: str) -> _Doc:
    a = detail["assessment"]
    doc = _Doc(DOC_VERDICT)
    banding = detail.get("banding") or {}
    pass_label = str(banding.get("pass_label") or "decline")
    revision = detail.get("revision")

    # --- The brief, in the page's reading order (anchor `brief`).
    if a.company_or_project:
        doc.add("Project label", [a.company_or_project], anchor="brief")
    if a.headline:
        doc.add("Headline", [a.headline], anchor="brief")
    if a.confidence:
        doc.add("Hub's confidence label", [str(a.confidence).strip("[]")], anchor="brief")
    if a.elevator_pitch:
        doc.add("In one minute — the hub's elevator pitch", [a.elevator_pitch], anchor="brief")
    if isinstance(a.key_points, dict):
        for key, group_label in KEY_POINT_GROUPS:
            for point in _items(a.key_points.get(key)):
                doc.add(f"Key point — {_label_safe(group_label)}", [point], anchor="brief")
    else:
        for point in _items(a.key_points):
            doc.add("Key point", [point], anchor="brief")
    if a.score_rationale:
        doc.add(
            "Why this score — the hub's own explanation",
            [a.score_rationale],
            anchor="score-rationale",
        )

    # --- Evidence summary (anchor `signals`).
    transcript_available = bool(detail.get("messages_available"))
    if tier == CHAT_TIER_STAFF:
        for field_name in STAFF_ONLY_VERDICT_FIELDS:
            for bullet in _items(getattr(a, field_name, None)):
                doc.add(_STAFF_ONLY_LABELS[field_name], [bullet], anchor="signals")
    signals = detail.get("verdict_signals") or {}
    for item in signals.get("strengths") or []:
        # Only the consult entries: they are the one place the page shows a
        # specialist's `established` text (the latest consult per domain, at most
        # three items plus "and N more"). Every other entry restates a field this
        # document already carries.
        if not isinstance(item, dict) or item.get("source") != "consult" or not item.get("body"):
            continue
        label = (
            f"Evidence summary — what the {_label_safe(item.get('label'))} specialist"
            f" established (signal {_label_safe(item.get('detail'))})"
        )
        if item.get("note"):
            # `note` names earlier same-domain consults, which the page (and this
            # record's Panel document) shows only inside the interview timeline — so
            # what to say about them depends on whether that timeline is rendered.
            if transcript_available:
                label += f"; {_label_safe(item['note'])}; every consult is in the panel findings"
            else:
                label += (
                    f"; {_label_safe(item['note'])}; the panel document's consult cards are"
                    " not shown because the interview transcript is unavailable"
                )
        doc.add(label, [str(line) for line in item["body"]], anchor="signals")

    # --- The ask (anchor `ask`).
    ask = _paragraphs(a.recommended_next_experiment)
    for i, para in enumerate(ask, 1):
        doc.add(f"Recommended next experiment, paragraph {i} of {len(ask)}", [para], anchor="ask")

    # --- The verdict header card (anchor `verdict`).
    if a.subject_agent_id:
        doc.add("Lab — the agent id of the PI's lab bot", [a.subject_agent_id], anchor="verdict")
    doc.add("Screened by — the hub's agent id", [a.agent_id], anchor="verdict")
    if isinstance(a.created_at, datetime):
        doc.add("Verdict written (UTC)", [_minute(a.created_at)], anchor="verdict")
    doc.add("Interview channel", [f"#{a.channel_name}"], anchor="verdict")
    if a.recommendation == "pass":
        doc.add(
            f'Hub recommendation — stored as "pass", displayed as "{_label_safe(pass_label)}":'
            " do not fund",
            [pass_label],
            anchor="verdict",
        )
    elif a.recommendation:
        doc.add("Hub recommendation", [a.recommendation], anchor="verdict")
    else:
        doc.add("Hub recommendation — none recorded", anchor="verdict")
    if a.weighted_score is not None:
        band = (pass_label if a.band == "pass" else a.band) or "—"
        doc.add(
            "Computed score and band — computed by the application from the hub's dimension"
            " scores, not taken from the model; the hub's recommendation is a separate field"
            " and can disagree with the band",
            [f"{a.weighted_score:.2f}", band],
            anchor="verdict",
        )
    else:
        doc.add(
            "Computed score and band — none: the verdict carried no dimension scores, so no"
            " score or band was computed",
            anchor="verdict",
        )
    if revision is not None and getattr(revision, "advance_min", None) is not None:
        lines = [
            f"≥{revision.advance_min} advance, <{revision.conditional_min}"
            f" {revision.pass_label or pass_label}"
        ]
        doc.add("Band thresholds of the rubric revision that scored this row", lines, anchor="verdict")
    else:
        doc.add(
            "Band thresholds — not recorded for this row's rubric revision", anchor="verdict"
        )
    provenance = detail.get("revision_provenance")
    current = _label_safe(detail.get("rubric_version"))
    if a.rubric_version:
        stamp = (
            f"{a.rubric_version} ({a.rubric_content_hash})"
            if a.rubric_content_hash
            else str(a.rubric_version)
        )
        if provenance == PROVENANCE_LIVE:
            meaning = "the current rubric document"
        elif provenance == PROVENANCE_ARCHIVED:
            meaning = (
                "an archived revision from the revision registry; the current rubric is"
                f" {current}, and scores are not directly comparable across revisions"
            )
        elif provenance == PROVENANCE_UNKNOWN:
            meaning = (
                "a stamp that matches no entry in the revision registry, so scores are shown"
                f" as stored with no weights or band lines; the current rubric is {current}"
            )
        else:
            meaning = f"provenance not determined; the current rubric is {current}"
        doc.add(f"Rubric stamp — the revision that scored this row: {meaning}", [stamp], anchor="verdict")
    else:
        doc.add(
            "Rubric stamp — none: this verdict predates rubric stamping, so which revision"
            f" scored it is not recorded; the current rubric is {current}",
            anchor="verdict",
        )

    # --- Panel status (anchor `panel`).
    state = detail.get("panel_state")
    if state == "gap":
        missing = ", ".join(str(d) for d in (a.missing_domains or [])) or "(unnamed)"
        doc.add(_PANEL_LABELS["gap"], [missing], anchor="panel")
    elif state == "not_owed":
        recommendation = _label_safe(a.recommendation) or "verdict with no recommendation"
        doc.add(_PANEL_LABELS["not_owed"].format(recommendation=recommendation), anchor="panel")
    elif state in ("unverified", "verified"):
        doc.add(_PANEL_LABELS[state], anchor="panel")
    else:
        doc.add(_PANEL_UNRECORDED, anchor="panel")

    # --- Gates (anchor `gating`).
    descriptions = detail.get("gating_descriptions") or {}
    if isinstance(a.gating, dict) and a.gating:
        for key, value in a.gating.items():
            shown = _GATE_STATES.get(value) if isinstance(value, str) else None
            lines = [shown or f"unrecognized gating value: {value}"]
            meta = descriptions.get(key) if isinstance(key, str) else None
            if isinstance(meta, dict) and meta.get("description"):
                lines.append(meta["description"])
            doc.add(f"Gate — {_label_safe(str(key).replace('_', ' '))}", lines, anchor="gating")
    else:
        doc.add("Gates — none recorded", anchor="gating")

    # --- Red flags (anchor `red-flags`).
    flags = _items(a.red_flags)
    for i, flag in enumerate(flags, 1):
        doc.add(f"Red flag {i} of {len(flags)}", [flag], anchor="red-flags")
    if not flags and not a.red_flags:
        doc.add("Red flags — none recorded", anchor="red-flags")

    # --- Rationale (anchor `rationale`).
    paragraphs = _paragraphs(a.rationale)
    for i, para in enumerate(paragraphs, 1):
        doc.add(f"Rationale, paragraph {i} of {len(paragraphs)}", [para], anchor="rationale")

    # --- Dimension scores (anchor `scores`).
    dims = [d for d in detail.get("dimensions") or [] if isinstance(d, dict)]
    scale = (
        f"; scale {revision.scale_min} to {revision.scale_max}"
        if revision is not None
        else "; scale unknown (this row's revision is not in the registry)"
    )
    any_scored = any(d.get("score") is not None for d in dims)
    for d in dims:
        label = f"Dimension score — {_label_safe(d.get('title') or d.get('key'))}"
        if d.get("weight_note"):
            label += f"; {_label_safe(d['weight_note'])} weight"
        label += scale
        score = d.get("score")
        if score is None:
            label += (
                "; not scored — counted as zero in the weighted score"
                if any_scored
                else "; not scored — no dimension scores were recorded for this verdict"
            )
            doc.add(label, anchor="scores")
        else:
            doc.add(label, [f"{score:g}"], anchor="scores")
    if not dims:
        doc.add("Dimension scores — none recorded", anchor="scores")
    return doc


def _speaker(message: dict[str, Any], a: OpportunityAssessment) -> str:
    agent_id = message.get("agent_id")
    if agent_id:
        # Exactly how the page names an agent row: `{{ m.agent_id | capitalize }}Bot`.
        name = _label_safe(f"{str(agent_id).capitalize()}Bot")
        if agent_id == a.agent_id:
            return f"{name} (the hub)"
        if agent_id == a.subject_agent_id:
            return f"{name} (the lab's agent)"
        return f"{name} (another registered agent)"
    shown = _label_safe(message.get("sender_name")) or "(unknown sender)"
    return f'sender not registered as an agent, display name "{shown}" (unverified)'


def _interview_doc(detail: dict[str, Any], a: OpportunityAssessment) -> _Doc:
    doc = _Doc(DOC_INTERVIEW)
    messages = [
        entry["message"]
        for entry in detail.get("timeline") or []
        if isinstance(entry, dict) and entry.get("kind") == "message"
    ]
    if not detail.get("messages_available") or not messages:
        doc.add(
            "Transcript unavailable — the thread this verdict came from is not in the"
            " application's message store (an older verdict can outlive its transcript);"
            " the Verdict document is unaffected",
            anchor="timeline",
        )
        return doc
    total = len(messages)
    for i, message in enumerate(messages, 1):
        parts = [f"Message {i} of {total}", _speaker(message, a)]
        phase = _label_safe(message.get("phase"))
        if phase:
            parts.append(phase)
        if message.get("is_verdict_message"):
            parts.append("carried the verdict")
        doc.add(" · ".join(parts), [message.get("content") or ""], anchor=f"m-{message['key']}")
    return doc


def _panel_doc(detail: dict[str, Any]) -> _Doc:
    doc = _Doc(DOC_PANEL)
    # The consult cards render inside the timeline's messages-available branch
    # (template :1152-1308), so a page with no transcript shows none — but
    # `_load_consults` keys on (run, thread_id), not on the transcript, so a consult
    # can still exist here even when there is no transcript to show it in. Saying
    # "no consults" in that case would be false; say instead that the cards are
    # simply not shown, which is true whether or not a consult was recorded.
    if not detail.get("messages_available"):
        doc.add(
            "Consult cards not shown — the interview transcript is unavailable, and"
            " the page shows specialist consults only inside it; this is not evidence"
            " that no consult was made",
            anchor="panel",
        )
        return doc
    consults = [
        entry["consult"]
        for entry in detail.get("timeline") or []
        if isinstance(entry, dict) and entry.get("kind") == "consult"
    ]
    if not consults:
        doc.add(
            "No consults — no specialist consults were recorded for this interview",
            anchor="panel",
        )
        return doc
    total = len(consults)
    for k, c in enumerate(consults, 1):
        parts = [f"Consult {k} of {total}", _label_safe(c.get("domain")) or "consult"]
        if c.get("reply_truncated"):
            # The card suppresses the count, confidence and read state for a cut-off
            # reply (template :1208-1240): they are the parser's defaults.
            parts.append(
                "reply cut off — no signal; the reply stopped before it finished, so what"
                " is quoted is a partial answer and this domain does not count toward the"
                " specialist floor"
            )
        else:
            parts.append(f"signal {_label_safe(c.get('verdict_signal')) or 'not recorded'}")
            count = c.get("concern_count") or 0
            parts.append(f"{count} concern{'' if count == 1 else 's'}")
            if c.get("confidence"):
                parts.append(f"confidence {_label_safe(c['confidence'])}")
            read_state = c.get("read_state")
            if read_state and read_state != "parsed":
                parts.append(
                    f"read: {_label_safe(read_state)} — the stored signal is a default, not"
                    " something the specialist said"
                )
        lines: list[str] = []
        if c.get("question"):
            lines.append(f"Asked: {c['question']}")
        concerns = _items(c.get("concerns"))
        if concerns:
            lines.append("Concerns:")
            lines.extend(f"- {concern}" for concern in concerns)
        questions = _items(c.get("questions_to_ask"))
        if questions:
            lines.append("Questions to ask the PI:")
            lines.extend(f"- {question}" for question in questions)
        doc.add(" · ".join(parts), lines, anchor=f"consult-{k}")
    return doc


def _reviews_doc(detail: dict[str, Any]) -> _Doc:
    doc = _Doc(DOC_REVIEWS)
    reviews = list(detail.get("review_feedback") or [])
    unknown = detail.get("revision_provenance_unknown")
    total = len(reviews)
    for i, review in enumerate(reviews, 1):
        parts = [f"Human review {i} of {total}", _label_safe(review.reviewer_name) or "unnamed reviewer"]
        if review.recorded_by_user_id:
            recorder = _label_safe(getattr(review, "recorded_by_name", None)) or "an admin"
            parts.append(f"entered by {recorder} while impersonating")
        parts.append(f"overall score {review.score}/5")
        mode = _FEEDBACK_MODES.get(review.feedback_mode) if isinstance(review.feedback_mode, str) else None
        parts.append(f"feedback mode: {mode or 'unknown mode ' + _label_safe(review.feedback_mode)}")
        if review.edited:
            parts.append("edited")
        if isinstance(review.created_at, datetime):
            parts.append(f"written {_minute(review.created_at)} UTC")
        rows = [r for r in getattr(review, "dimension_rows", None) or [] if isinstance(r, dict)]
        if rows:
            parts.append("per-dimension scores quoted below as 'score — dimension'")
            if getattr(review, "dimension_provenance", None) == unknown:
                parts.append(
                    "scored against an unrecognized rubric revision"
                    f" ({_label_safe(review.rubric_version) or 'unstamped'}), shown as stored"
                    " with no dimension titles or weights"
                )
        doc.add(
            " · ".join(parts),
            [f"{row.get('score')} — {row.get('title')}" for row in rows],
            anchor="review",
        )
        if review.comment:
            doc.add(f"Human review {i} of {total} — the reviewer's comment", [review.comment], anchor="review")
    status = detail.get("review_status")
    if status is not None:
        action = getattr(status, "action", None)
        actor = _label_safe(getattr(status, "actor_name", None)) or "an unnamed user"
        if action == "cleared":
            text = "Unreviewed (the last approve or disapprove was cleared)"
        elif action == "approved":
            text = f"Approved by {actor}"
        elif action == "disapproved":
            text = f"Disapproved by {actor}"
        else:
            text = f"unknown status action {_label_safe(action)} by {actor}"
        doc.add(f"Review status — {text}", anchor="review")
    if not reviews:
        doc.add(
            "No reviews — no human reviews have been recorded for this assessment",
            anchor="review",
        )
    return doc


def _rubric_doc(detail: dict[str, Any], a: OpportunityAssessment) -> _Doc:
    doc = _Doc(DOC_RUBRIC)
    rubric = detail.get("review_rubric") or {}
    version = _label_safe(rubric.get("version"))
    note = ""
    if detail.get("revision_provenance") != PROVENANCE_LIVE:
        note = (
            f"; this row was scored against {_label_safe(a.rubric_version)}"
            if a.rubric_version
            else "; this row predates rubric stamping"
        )
    doc.add(
        f"Review form — reviewers score each dimension from {rubric.get('scale_min')} (weak)"
        f" to {rubric.get('scale_max')} (strongly meets Blackbird's bar) against rubric"
        f" {version}; the overall rating is 1 to 5 and rates the proposal, not the bot",
        anchor="review",
    )
    for d in rubric.get("dimensions") or []:
        if not isinstance(d, dict):
            continue
        label = (
            f"Scale definition — {_label_safe(d.get('title'))}; {d.get('weight')}% weight;"
            f" current rubric {version}{note}"
        )
        anchors = d.get("anchors")
        doc.add(label, [anchors] if anchors else [], anchor="review")
    return doc


def build_chat_record(detail: dict[str, Any], *, tier: str) -> ChatRecord:
    """The record for ``tier`` from a ``build_assessment_detail(admin_view=False)``
    context. Pure; see the module docstring for the two rules it keeps."""
    if tier not in (CHAT_TIER_STAFF, CHAT_TIER_REVIEWER):
        raise ValueError(f"unknown chat tier {tier!r}")
    assessment = detail["assessment"]
    docs = [
        _verdict_doc(detail, tier),
        _interview_doc(detail, assessment),
        _panel_doc(detail),
        _reviews_doc(detail),
        _rubric_doc(detail, assessment),
    ]
    documents = tuple(
        {
            "type": "document",
            "source": {
                "type": "content",
                "content": [{"type": "text", "text": block} for block in d.blocks],
            },
            "title": DOC_TITLES[d.key],
            "context": _legend_for(d.key, tier),
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


async def load_chat_record(
    db: AsyncSession, assessment_id: uuid.UUID, *, tier: str
) -> tuple[ChatRecord, OpportunityAssessment] | None:
    """The record for one assessment and tier, or None when it does not exist.

    ``admin_view=False`` for EVERY tier: no ``raw_opinion`` and no tool activity ever
    enter the record (D4). ``viewer_is_staff=False`` only skips the assignee-roster
    query, which the record never reads; the staff-only verdict fields are gated by
    ``tier`` here, exactly as the template gates them by ``viewer_is_staff``.
    """
    detail = await build_assessment_detail(
        db, assessment_id, admin_view=False, viewer_is_staff=False
    )
    if detail is None:
        return None
    return build_chat_record(detail, tier=tier), detail["assessment"]
