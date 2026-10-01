"""Verbatim copy of assessment_chat_record._verdict_doc at PHASE_BASE (Task 25).
It imports the module's unchanged helpers; only the function body is frozen.
Never edit this file except to re-point an import that moved."""
from datetime import datetime
from typing import Any

from src.models.assessment_chat import CHAT_TIER_STAFF
from src.services.assessment_chat_record import (
    _GATE_STATES,
    _PANEL_LABELS,
    _PANEL_UNRECORDED,
    DOC_VERDICT,
    _Doc,
    _items,
    _label_safe,
    _minute,
    _paragraphs,
)
from src.services.assessment_detail import key_point_sections
from src.services.rubric_revisions import (
    PROVENANCE_ARCHIVED,
    PROVENANCE_LIVE,
    PROVENANCE_UNKNOWN,
)

STAFF_ONLY_VERDICT_FIELDS = ("strengths", "risks", "competitive_landscape", "evidence_maturity")
_STAFF_ONLY_LABELS = {
    "strengths": "Hub-listed strength (the hub's own words)",
    "risks": "Hub-listed risk (the hub's own words)",
    "competitive_landscape": "Competitive landscape (the hub's own words)",
    "evidence_maturity": "Evidence maturity (the hub's own words)",
}


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
    for group_label, points in key_point_sections(a.key_points):
        for point in points:
            if group_label is None:
                doc.add("Key point", [point], anchor="brief")
            else:
                doc.add(f"Key point — {_label_safe(group_label)}", [point], anchor="brief")
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
        # The hub's own reason for THIS dimension (0052). Quotable because the
        # page renders it in two places — the Evidence summary row and the
        # `#scores` disclosure — and the anchor below points at the second, so
        # "Show in page" lands on the sentence being quoted. Without the
        # `#scores` rendering this would breach parity for any row whose rubric
        # revision is unknown: `derive_strengths_and_risks` contributes no
        # dimension entries at all for those, so the Evidence summary shows
        # nothing to contain the quote. Not staff-only (spec D5): both tiers'
        # pages render it, so both tiers' records carry it.
        lines = [] if score is None else [f"{score:g}"]
        if d.get("rationale"):
            lines.append(str(d["rationale"]))
        doc.add(label, lines, anchor="scores")
    if not dims:
        doc.add("Dimension scores — none recorded", anchor="scores")
    return doc
