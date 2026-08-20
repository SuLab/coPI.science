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
from functools import cache
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


# --- panel notes ------------------------------------------------------------
#
# The one line the hub posts into the interview thread when a consult succeeds.
# Lives here, with the signals it renders, because it is pure and because an
# interview thread is workspace-visible: what may be published about a consult
# is a property of the specialist contract, not of the engine.

_PANEL_NOTE_SIGNAL_EMOJI: dict[str, str] = {
    "blocking": "⛔",
    "caution": "⚠️",
    "clear": "✅",
}

# How much of the hub's question the note carries. The question is the only
# free text in the note and is model-written, so it is clipped — long enough to
# be recognisable in a thread, short enough not to become a second transcript.
PANEL_NOTE_QUESTION_CHARS = 200


def format_panel_note(*, domain: str, verdict_signal: str, question: str) -> str:
    """The workspace-visible note for one successful consult.

    SIGNAL-LEVEL ONLY, and the signature is the enforcement: this function is
    handed the domain, the parsed verdict signal and the hub's question, and
    there is no parameter through which ``concerns``, ``questions_to_ask``,
    ``confidence`` or the opinion body could reach a Slack post. That is
    deliberate — an interview thread is visible to every lab in the workspace,
    and a specialist's opinion paraphrases both the PI's confidential
    statements and Blackbird's internal rubric. The full opinion has two homes
    already, both staff-only: ``specialist_consults.raw_opinion`` and the
    consult's own ``llm_call_logs`` row.

    An unrecognised signal renders as the bare word with no emoji rather than
    being mapped to something reassuring — the same rule
    ``parse_opinion``'s ``_DEFAULT_SIGNAL`` follows, for the same reason: a
    signal we could not read has not cleared anything.
    """
    emoji = _PANEL_NOTE_SIGNAL_EMOJI.get(verdict_signal, "")
    signal = f"{emoji} {verdict_signal}".strip()
    return (
        f"🧪 Panel · {domain} — {signal} — "
        f'asked: "{clip_question(question)}"'
    )


def clip_question(question: str, limit: int = PANEL_NOTE_QUESTION_CHARS) -> str:
    """Clip to ``limit`` characters on a word boundary, with an ellipsis.

    Falls back to a hard cut when the first ``limit`` characters contain no
    space at all (a single long token), so the bound always holds — a
    word-boundary search that finds nothing must not return the whole string.
    """
    text = (question or "").strip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = head.rfind(" ")
    if cut <= 0:
        return head.rstrip() + "…"
    return head[:cut].rstrip() + "…"


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


def has_usable_content(raw: str) -> bool:
    """Whether a specialist's reply carries an opinion at all.

    Narrower than "did it parse". Prose IS an opinion — a specialist answering
    in sentences has answered, and ``parse_opinion`` keeps treating it that way
    on purpose. This excludes only what the design's error table meant by a
    failed call: a reply with nothing in it.

    The distinction matters because ``on_consult`` is what satisfies the
    enforcement floor. If an empty reply counted, "the specialist was
    unreachable" would silently become "the specialist approved".
    """
    text = _strip_fence(raw or "").strip()
    if not text:
        return False
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return True  # unparseable prose is still an answer
    if not isinstance(data, dict):
        return False  # null, [], a bare number or string say nothing
    # A populated object is an answer even when none of its keys is one WE
    # named. This used to require one of ("verdict_signal", "concerns",
    # "questions_to_ask", "confidence"), which reported
    # {"signal": "blocking", "analysis": "<500 words of real analysis>"} to the
    # hub as "returned an empty response" — a false statement that discarded
    # the analysis and denied the domain its credit, while the SAME words sent
    # as bare prose were kept by the branch above. Only `{}` says nothing.
    # `parse_opinion` still reads the four known keys and degrades gracefully
    # when they are absent, so an unrecognised shape costs a default signal,
    # not the reply.
    return bool(data)


# Cues that make a domain required. Matched via `_cue_matches` below, not raw
# substring containment — see `_WORD_ONLY_CUES` and `_cue_pattern` for how a
# cue like "compound" is kept from firing on "compounding".
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


# Cues with a DEMONSTRATED false-positive risk in the corpus — not simply the
# short ones. These match as WHOLE WORDS (plus a simple plural); every other
# cue stays prefix-anchored so stems like "medicinal chem" -> "medicinal
# chemistry" and "neurodegener" -> "neurodegeneration" keep working.
#
# Measured on the 18 production verdicts of run 1787010946: "aso" matched
# "reasons" on 7, "hit" matched "architecture" on 6, "als" matched
# "also"/"signals"/"animals"/"journals" on 9, and "compound" matched
# "compounding" (as in "several compounding reasons") on at least one. Four
# verdicts had a specialist required by one of these ALONE.
_WORD_ONLY_CUES: frozenset[str] = frozenset({"als", "aso", "hit", "adc", "compound"})


@cache
def _cue_pattern(cue: str) -> re.Pattern[str]:
    escaped = re.escape(cue)
    if cue in _WORD_ONLY_CUES:
        return re.compile(rf"(?<![a-z0-9]){escaped}(?:s|es)?(?![a-z0-9])")
    # The prefix tier. The leading lookbehind is load-bearing and is NOT
    # decoration shared with the branch above: drop it here and ~30 cues
    # revert to raw substring containment ("nonclinical" would summon the
    # clinical specialist, "polypeptide" the chemistry one). Every
    # false-positive test used to exercise only `_WORD_ONLY_CUES`, so that
    # deletion shipped green; `test_the_prefix_tier_is_anchored_at_a_word_
    # boundary` in tests/unit/test_specialists.py now fails if it is removed.
    # No trailing anchor on purpose — that is the whole point of this tier:
    # "medicinal chem" must reach "medicinal chemistry" and "neurodegener"
    # must reach "neurodegeneration".
    return re.compile(rf"(?<![a-z0-9]){escaped}")


def _cue_matches(cue: str, text: str) -> bool:
    """Whether ``cue`` occurs in ``text`` as a word rather than as a fragment.

    The lookbehind is what kills the false positives: "reasons" contains "aso"
    but not at a word boundary. Hyphens count as boundaries, so "aso-based" and
    "known-compound" still match.

    The old comment here claimed "a false positive costs one consult, a false
    negative costs the whole point of the floor." That cost model was inverted:
    this runs AFTER the interview is over, so a false positive cost the whole
    verdict.
    """
    return _cue_pattern(cue).search(text) is not None


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

    if any(_cue_matches(cue, text) for cue in _CHEMISTRY_CUES):
        required.add("chemistry")
    if any(_cue_matches(cue, text) for cue in _CLINICAL_CUES):
        required.add("clinical")

    gating = verdict.get("gating")
    if isinstance(gating, dict) and gating.get("fto_achievable") == "met":
        required.add("legal")

    scores = verdict.get("scores")
    platform_scored = isinstance(scores, dict) and isinstance(
        scores.get("platform"), (int, float)
    ) and scores.get("platform", 0) >= 4
    if platform_scored or any(_cue_matches(cue, text) for cue in _PLATFORM_CUES):
        required.add("technologic")

    return frozenset(required)
