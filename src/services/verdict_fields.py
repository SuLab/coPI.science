"""Every verdict field the hub's sidecar carries or the engine derives, in one
place (S2-05 = LC-03). It drives what `_persist_assessment` stores for the plain
sidecar fields; the chat record's labels (`assessment_chat_record._verdict_doc`,
byte-identical, C27), the staff-only set and the template gating are meant to
read the same rows.

`treatment` is how a sidecar value becomes a column value: "text" =
`_str_or_none`, "bullets" = `normalize_bullets`, "bounded" = `_bounded_str(value,
max_len)`; "engine" fields are computed by the engine (scores, band, gating, …)
and are listed for the record/template, never built from here. `soft_bound` is the
contract length the engine WARNS about and still stores (characters; words for
`elevator_pitch`). Changing a label changes a model prompt: labels here are
exactly the strings `_verdict_doc` renders.

Why each plain field stores what it does:

- `company_or_project`, `headline`, `elevator_pitch` (sidecar items 6-8,
  2026-09-09): the reviewer-facing narrative; `company_or_project` is the short
  label, the other two are what the assessment pages lead with. Each degrades
  alike: a wrong type becomes None and `raw_verdict` keeps the original, because a
  malformed narrative field must never cost the verdict (A20).
- `score_rationale` (item 10, 0048): why the dimension scores came out where they
  did. App-only by design (D3): the six-field #assessments-summary headline never
  renders it, which is why it is a column of its own rather than more pitch.
- `strengths`, `risks` (items 11/12, 0049) and `competitive_landscape`,
  `evidence_maturity` (items 13/14, 0050): the hub's own bullets. They degrade to
  None on a wrong type; `raw_verdict` keeps the original either way.
- `funnel_stage`, `recommendation`, `confidence`: bounded VARCHAR columns. Every
  other field degrades per-field on a bad value (wrong type -> None), but a
  too-long *string* in one of these is still the right type and would sail past an
  isinstance check into a DataError at commit, which drops the WHOLE row. They are
  clipped instead: a truncated recommendation is still useful for triage.
- `rationale`, `recommended_next_experiment` (item 5, rubric v2.1.0): the verdict's
  reasoning and the single experiment Blackbird should fund next. Text siblings
  that degrade to None on a wrong type.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class VerdictField:
    key: str
    column: str | None
    sidecar_key: str | None
    label: str | None
    staff_only: bool
    anchor: str | None
    soft_bound: int | None
    treatment: Literal["text", "bullets", "bounded", "engine"]
    max_len: int | None = None


VERDICT_FIELDS: tuple[VerdictField, ...] = (
    VerdictField("company_or_project", "company_or_project", "company_or_project", "Project label", False, "brief", 70, "text"),
    VerdictField("headline", "headline", "headline", "Headline", False, "brief", 110, "text"),
    VerdictField("confidence", "confidence", "confidence", "Hub's confidence label", False, "brief", None, "bounded", 20),
    VerdictField("elevator_pitch", "elevator_pitch", "elevator_pitch", "In one minute — the hub's elevator pitch", False, "brief", 250, "text"),
    VerdictField("key_points", "key_points", "key_points", None, False, "brief", None, "engine"),
    VerdictField("score_rationale", "score_rationale", "score_rationale", "Why this score — the hub's own explanation", False, "score-rationale", None, "text"),
    VerdictField("strengths", "strengths", "strengths", "Hub-listed strength (the hub's own words)", True, "signals", 200, "bullets"),
    VerdictField("risks", "risks", "risks", "Hub-listed risk (the hub's own words)", True, "signals", 200, "bullets"),
    VerdictField("competitive_landscape", "competitive_landscape", "competitive_landscape", "Competitive landscape (the hub's own words)", True, "signals", 200, "bullets"),
    VerdictField("evidence_maturity", "evidence_maturity", "evidence_maturity", "Evidence maturity (the hub's own words)", True, "signals", 200, "bullets"),
    VerdictField("recommended_next_experiment", "recommended_next_experiment", "recommended_next_experiment", None, False, "ask", None, "text"),
    VerdictField("funnel_stage", "funnel_stage", "funnel_stage", None, False, None, None, "bounded", 20),
    VerdictField("recommendation", "recommendation", "recommendation", None, False, "verdict", None, "bounded", 30),
    VerdictField("rationale", "rationale", "rationale", None, False, "rationale", None, "text"),
    VerdictField("dimension_rationales", "dimension_rationales", "dimension_rationales", None, False, "scores", 200, "engine"),
    VerdictField("scores", "scores", "scores", None, False, "scores", None, "engine"),
    VerdictField("gating", "gating", "gating", None, False, "gating", None, "engine"),
    VerdictField("red_flags", "red_flags", "red_flags", None, False, "red-flags", None, "engine"),
    VerdictField("derisking_milestones", "derisking_milestones", "suggested_derisking_milestones", None, False, None, None, "engine"),
    VerdictField("weighted_score", "weighted_score", None, None, False, "verdict", None, "engine"),
    VerdictField("band", "band", None, None, False, "verdict", None, "engine"),
)

VERDICT_FIELD: dict[str, VerdictField] = {f.key: f for f in VERDICT_FIELDS}

#: Sidecar keys stored only inside `raw_verdict` (never a plain column built from
#: the sidecar): `subject_agent_id` is read by the engine from the subject view and
#: bounded there, not taken from this registry.
RAW_ONLY_SIDECAR_KEYS: frozenset[str] = frozenset({"subject_agent_id"})


def sidecar_column_kwargs(verdict: dict) -> dict[str, Any]:
    """Column -> stored value for every "text"/"bullets"/"bounded" field."""
    from src.agent.engine.sidecar import _bounded_str, _str_or_none
    from src.services.assessment_detail import normalize_bullets

    out: dict[str, Any] = {}
    for f in VERDICT_FIELDS:
        if f.treatment == "engine" or f.column is None or f.sidecar_key is None:
            continue
        value = verdict.get(f.sidecar_key)
        if f.treatment == "text":
            out[f.column] = _str_or_none(value)
        elif f.treatment == "bullets":
            out[f.column] = normalize_bullets(value)
        else:
            out[f.column] = _bounded_str(value, f.max_len)
    return out
