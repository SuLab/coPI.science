"""The eight consultable specialist personas, and the rules around them.

The stakeholder document (BBL Evaluator Notes) names nine "Potential Agentic
Evaluators". Eight are consultable domains; the ninth, Blackbird, is the hub
doing the integrating and is therefore not in this table.

Dependency-free on the engine (no src.models, no DB, no SimulationEngine), like
src/agent/post_types.py and src/agent/roles.py, so the contract and the
requirement rule are unit-testable without a database or a running loop.

Why these eight and not some other cut: six of them already existed as weighted
rubric dimensions. The two that did not — scientific and chemistry — are the two
that decide real cases. Counted over the document's own 15 rejections: mechanism
validation 5, toxicity/selectivity 4, chemistry-to-DC-path 4. FTO appears once
and already carried a gate, a 10% dimension, a red flag and a dedicated tool.
See docs/specs/2026-08-07-nine-evaluator-panel-design.md §1.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path("prompts")
SPECIALISTS_DIR = PROMPTS_DIR / "specialists"

VERDICT_SIGNALS: frozenset[str] = frozenset({"blocking", "caution", "clear"})

# Unknown or missing signals degrade here rather than to "clear": a specialist
# whose answer we could not read has NOT cleared anything.
_DEFAULT_SIGNAL = "caution"
_CONFIDENCES: frozenset[str] = frozenset({"high", "moderate", "low"})


@dataclass(frozen=True)
class SpecialistSpec:
    """One consultable perspective. ``maps_to_dimension`` names the rubric
    dimension this specialist's concerns belong in, so a blocking signal has
    somewhere to land — None where the specialist informs judgement without
    owning a single dimension."""

    domain: str
    title: str
    owns: str
    consult_when: str
    maps_to_dimension: str | None = None


SPECIALIST_DOMAINS: dict[str, SpecialistSpec] = {
    s.domain: s
    for s in (
        SpecialistSpec(
            "scientific", "Scientific Specialist",
            "experimental rigor, controls, statistical power, interpretability, "
            "mouse-to-human translatability",
            "any experimental claim, any animal-model result, any 'we showed that'",
            maps_to_dimension="experimental_rigor",
        ),
        SpecialistSpec(
            "chemistry", "Chemistry Specialist",
            "path to a development candidate, medicinal-chemistry tractability, "
            "tolerability, in-family off-target liability",
            "chemical matter, a compound series, or a choice of modality",
            maps_to_dimension="chemistry_dc_path",
        ),
        SpecialistSpec(
            "clinical", "Clinical Specialist",
            "unmet need against the current standard of care, indication choice, "
            "patient numbers, the clinical development path",
            "any disease or indication claim",
            maps_to_dimension="market_unmet_need",
        ),
        SpecialistSpec(
            "commercial", "Commercial Specialist",
            "competitive landscape, named competing programs, deal comparables, "
            "investor sentiment",
            "any differentiation or first/best-in-class claim",
            maps_to_dimension="differentiation",
        ),
        SpecialistSpec(
            "legal", "Legal Specialist",
            "freedom to operate, licensing, research-tool and animal-model "
            "encumbrance, co-ownership",
            "any IP claim, or reliance on third-party materials",
            maps_to_dimension="ip_fto",
        ),
        SpecialistSpec(
            "technologic", "Technologic Specialist",
            "platform feasibility, and whether the proposed work would actually "
            "test that feasibility",
            "any platform or novel-technology claim",
            maps_to_dimension="platform",
        ),
        SpecialistSpec(
            "talent", "Talent Specialist",
            "probability the team completes the work, conflicts of interest, "
            "over-commitment across projects",
            "always, before concluding an interview",
            maps_to_dimension="team",
        ),
        SpecialistSpec(
            "budget", "Budget Specialist",
            "scope against Blackbird's grant bands and 12-24 month durations",
            "any workplan, cost, or timeline claim",
            maps_to_dimension="workplan_capital_efficiency",
        ),
    )
}


@dataclass(frozen=True)
class SpecialistOpinion:
    domain: str
    verdict_signal: str
    concerns: tuple[str, ...]
    questions_to_ask: tuple[str, ...]
    confidence: str
    raw: str


def persona_path(domain: str) -> Path:
    """Where a domain's persona file lives. Not validated here — the caller
    checks existence, because a missing file must not satisfy the floor."""
    return SPECIALISTS_DIR / f"{domain}.md"


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def _strip_fence(raw: str) -> str:
    m = _FENCE_RE.match(raw)
    return m.group(1) if m else raw


def _str_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(v) for v in value if isinstance(v, str) and v.strip())


def parse_opinion(raw: str, *, domain: str) -> SpecialistOpinion:
    """Parse a specialist's reply. Never raises.

    Prose is a valid opinion — a specialist that answers in sentences has still
    answered, and the hub reads ``raw`` regardless. Only a call that FAILED must
    not satisfy the floor, and that is decided by the caller, not here.

    Models fence JSON by reflex, so a fenced block is unwrapped rather than
    treated as a parse failure.
    """
    text = _strip_fence(raw or "")
    data: object = None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        data = None

    if not isinstance(data, dict):
        return SpecialistOpinion(
            domain=domain, verdict_signal=_DEFAULT_SIGNAL, concerns=(),
            questions_to_ask=(), confidence="low", raw=raw,
        )

    signal = data.get("verdict_signal")
    if signal not in VERDICT_SIGNALS:
        signal = _DEFAULT_SIGNAL
    confidence = data.get("confidence")
    if confidence not in _CONFIDENCES:
        confidence = "low"

    return SpecialistOpinion(
        domain=domain,
        verdict_signal=signal,
        concerns=_str_tuple(data.get("concerns")),
        questions_to_ask=_str_tuple(data.get("questions_to_ask")),
        confidence=confidence,
        raw=raw,
    )


# Substrings that make a domain required. Deliberately broad: a false positive
# costs one consult, a false negative costs the whole point of the floor.
_CHEMISTRY_CUES = (
    "small molecule", "compound", "inhibitor", "activator", "antagonist",
    "agonist", "chemical matter", "medicinal chem", "antibody", "adc",
    "oligonucleotide", "aso", "sirna", "peptide", "modality", "scaffold",
    "lead series", "hit", "pharmacophore",
)
_CLINICAL_CUES = (
    "disease", "patient", "indication", "clinical", "therapeutic", "treatment",
    "cancer", "tumor", "tumour", "syndrome", "disorder", "epilepsy",
    "schizophrenia", "als", "neurodegener", "diagnosis",
)
_PLATFORM_CUES = ("platform", "pipeline", "multiple shots", "reusable")

_ALWAYS: frozenset[str] = frozenset({"scientific", "talent"})


def _haystack(verdict: dict) -> str:
    """Every free-text field of a verdict, lowercased, for cue matching."""
    parts: list[str] = []
    for key in ("company_or_project", "rationale", "funnel_stage", "recommendation"):
        value = verdict.get(key)
        if isinstance(value, str):
            parts.append(value)
    for key in ("red_flags", "suggested_derisking_milestones"):
        value = verdict.get(key)
        if isinstance(value, list):
            parts.extend(str(v) for v in value)
    return " ".join(parts).lower()


def required_domains_for(verdict: object) -> frozenset[str]:
    """Which specialists an ``advance``/``conditional`` verdict was obliged to
    consult, derived from the verdict's OWN content.

    Derived rather than self-reported on purpose: a hub that skipped Chemistry
    must not also get to declare Chemistry unnecessary.

    Never raises — this runs inside ``_persist_assessment``'s try block, where an
    exception would be caught and logged as "Failed to persist assessment"
    *after* the row had already been committed.
    """
    if not isinstance(verdict, dict):
        return _ALWAYS

    required = set(_ALWAYS)
    text = _haystack(verdict)

    if any(cue in text for cue in _CHEMISTRY_CUES):
        required.add("chemistry")
    if any(cue in text for cue in _CLINICAL_CUES):
        required.add("clinical")

    gating = verdict.get("gating")
    if isinstance(gating, dict) and gating.get("fto_achievable") == "met":
        required.add("legal")

    scores = verdict.get("scores")
    platform_scored = isinstance(scores, dict) and isinstance(
        scores.get("platform"), (int, float)
    ) and scores.get("platform", 0) >= 4
    if platform_scored or any(cue in text for cue in _PLATFORM_CUES):
        required.add("technologic")

    return frozenset(required)
