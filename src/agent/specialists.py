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

# The one import outside the standard library, and it is chosen to keep this
# module's "dependency-free on the engine" promise: src/services/json_extract.py
# is pure (no SDK, no DB, no engine) and exists precisely so this module need not
# import src.services.llm to get its extractor. See that module's docstring.
from src.services.json_extract import extract_json

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
    """One consultable perspective. ``maps_to_dimensions`` names the rubric
    dimensions this specialist's concerns belong in, so a blocking signal has
    somewhere to land — empty where the specialist informs judgement without
    owning any dimension.

    A tuple, not one dimension. It was 1:1 until 2026-08-22, when a census found
    5 of the 13 dimensions with no owning specialist at all — 25% of the
    incubation weight, and including ``mechanism_validation`` (10) and
    ``toxicity_selectivity`` (8), which are the two most-cited rejection reasons
    in the stakeholder document that justified building the panel. Both are
    squarely inside an existing persona's remit (mechanism belongs to
    scientific, in-family selectivity to chemistry), so the fix is a second
    dimension each, not a ninth specialist nobody would consult.

    Order matters only to a reader: the first entry is the dimension the persona
    file was written against. No dimension may appear twice across the table —
    ``src/services/directory.py`` inverts it into a dict keyed by dimension, so
    a second owner would silently become "whichever came last".
    """

    domain: str
    title: str
    owns: str
    consult_when: str
    maps_to_dimensions: tuple[str, ...] = ()


SPECIALIST_DOMAINS: dict[str, SpecialistSpec] = {
    s.domain: s
    for s in (
        SpecialistSpec(
            "scientific", "Scientific Specialist",
            "experimental rigor, controls, statistical power, interpretability, "
            "mouse-to-human translatability",
            "any experimental claim, any animal-model result, any 'we showed that'",
            maps_to_dimensions=("experimental_rigor", "mechanism_validation"),
        ),
        SpecialistSpec(
            "chemistry", "Chemistry Specialist",
            "path to a development candidate, medicinal-chemistry tractability, "
            "tolerability, in-family off-target liability",
            "chemical matter, a compound series, or a choice of modality",
            maps_to_dimensions=("chemistry_dc_path", "toxicity_selectivity"),
        ),
        SpecialistSpec(
            "clinical", "Clinical Specialist",
            "unmet need against the current standard of care, indication choice, "
            "patient numbers, the clinical development path",
            "any disease or indication claim",
            maps_to_dimensions=("market_unmet_need",),
        ),
        SpecialistSpec(
            "commercial", "Commercial Specialist",
            "competitive landscape, named competing programs, deal comparables, "
            "investor sentiment",
            "any differentiation or first/best-in-class claim",
            maps_to_dimensions=("differentiation",),
        ),
        SpecialistSpec(
            "legal", "Legal Specialist",
            "freedom to operate, licensing, research-tool and animal-model "
            "encumbrance, co-ownership",
            "any IP claim, or reliance on third-party materials",
            maps_to_dimensions=("ip_fto",),
        ),
        SpecialistSpec(
            "technologic", "Technologic Specialist",
            "platform feasibility, and whether the proposed work would actually "
            "test that feasibility",
            "any platform or novel-technology claim",
            maps_to_dimensions=("platform",),
        ),
        SpecialistSpec(
            "talent", "Talent Specialist",
            "probability the team completes the work, conflicts of interest, "
            "over-commitment across projects",
            "always, before concluding an interview",
            maps_to_dimensions=("team",),
        ),
        SpecialistSpec(
            "budget", "Budget Specialist",
            "scope against Blackbird's grant bands and 12-24 month durations",
            "any workplan, cost, or timeline claim",
            maps_to_dimensions=("workplan_capital_efficiency",),
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
#
# 600, not 200: a production measurement (n=134 consults, 2026-08-20) found
# the SHORTEST hub question was 241 chars, median 398, p90 552, max 814 — so
# 200 truncated every single note mid-sentence. 600 renders 95% of observed
# questions complete and clips only the tail of the 814-char worst case.
PANEL_NOTE_QUESTION_CHARS = 600


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

    TOLERANT, since 2026-08-22, and measurably so. This used to be a bare
    ``json.loads`` over the fence-stripped text, which saw the whole reply or
    nothing: run 8b64a0e0 laundered 6 of 168 consults into
    ``caution``/``low``/no-concerns, and one of the six was a ``blocking`` /
    ``high`` opinion that reached the PI's own interview thread as "⚠️ caution".
    Three of the six were a complete object followed by the model's own
    commentary (in one case after the closing fence, which is why ``_strip_fence``
    alone could not save it); ``json_extract`` recovers all three. The other
    three were cut mid-array by a ``refusal`` and are not recoverable by anyone —
    keeping THOSE at ``caution`` is the point, not a shortfall. See
    docs/audits/2026-08-22-run-8b64a0e0/rca-and-corrections.md, H5.

    ``_DEFAULT_SIGNAL`` stays ``caution`` and must: ``clear`` would turn a
    specialist we could not read into an approval, which is the exact failure
    ``has_usable_content`` exists to prevent. The remaining defence against a
    laundered opinion is the WARNING below plus the caller's refusal to credit a
    truncated consult (``_execute_consult_specialist``).
    """
    data: object = None
    try:
        data = extract_json(_strip_fence(raw or ""))
    except (ValueError, TypeError):
        # ValueError covers json.JSONDecodeError and extract_json's own raise.
        # TypeError guards a non-str `raw` reaching `.strip()`, which this
        # function's never-raises contract has always absorbed.
        data = None

    if not isinstance(data, dict):
        _warn_defaulted(domain, "no JSON object could be read from the reply")
        return SpecialistOpinion(
            domain=domain, verdict_signal=_DEFAULT_SIGNAL, concerns=(),
            questions_to_ask=(), confidence="low", raw=raw,
        )

    signal = data.get("verdict_signal")
    if signal not in VERDICT_SIGNALS:
        _warn_defaulted(
            domain,
            f"the object parsed but its verdict_signal was {signal!r}, not one of "
            f"{sorted(VERDICT_SIGNALS)}",
        )
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


def _warn_defaulted(domain: str, why: str) -> None:
    """Say out loud that this opinion's signal was DEFAULTED, not read.

    The six laundered consults were invisible precisely because a defaulted
    opinion is indistinguishable from a genuine cautious one — same signal, same
    confidence, same stored row, no log line anywhere. This is the greppable
    difference. Deliberately not a field on ``SpecialistOpinion`` or a column on
    ``specialist_consults``: a column belongs in its own migration, and the WARNING
    is what makes the next six countable today.

    Rare by construction — 141 of 168 consults in the motivating run parsed
    cleanly — so this is not a per-consult line.
    """
    logger.warning(
        "[specialists] %s opinion did NOT parse; signal DEFAULTED to %r (%s). "
        "This is not a cautious specialist, it is an unread one.",
        domain, _DEFAULT_SIGNAL, why,
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


# --- panel discrimination ---------------------------------------------------
#
# Whether the panel, taken as a whole, is telling us anything. Pure and here
# rather than in the engine for the same reason the panel note is: what counts
# as a usable specialist signal is a property of the specialist contract. The
# engine owns only the counting (``SimulationEngine._consult_signal_counts``)
# and the logging.

#: Smallest number of consults the alarm will speak on. At n=10 a zero `clear`
#: rate is ordinary luck, and an alarm that cries at the start of every run is
#: an alarm that gets muted. Unchanged from the original zero-test.
MIN_CONSULTS_FOR_CLEAR_RATE = 50

#: The `clear` share below which the panel is not discriminating. 5% is the
#: first floor this alarm has ever had; it replaced a ZERO test
#: (``total >= 50 and not counts.get("clear")``), which run 8b64a0e0 silenced
#: with a single `clear` in 168 consults — 0.6%, and the only `clear` in the
#: whole database across every run. A zero-test is silenced by one outlier; a
#: rate test is not. Set an order of magnitude above the observed 0.6% and well
#: below a genuinely selective panel clearing one idea in twenty.
MIN_CLEAR_RATE = 0.05


def clear_rate_warning(counts: dict[str, int] | None) -> str | None:
    """The warning to log about panel discrimination, or None if there is none.

    Returns the MESSAGE rather than logging it, so the sample it judges and the
    logger it speaks through both stay the caller's (the engine counts consults
    across a run; this module does not know a run exists). Never raises and
    never divides by zero — it runs in ``SimulationEngine.stop``, after the
    durable flushes, where an exception would be the last thing a shutdown does.
    """
    if not counts:
        return None
    total = sum(v for v in counts.values() if isinstance(v, int))
    if total < MIN_CONSULTS_FOR_CLEAR_RATE:
        return None
    clears = counts.get("clear") or 0
    rate = clears / total
    if rate >= MIN_CLEAR_RATE:
        return None
    return (
        f"[specialists] {clears} of {total} consults this run returned 'clear' "
        f"({rate:.1%}, floor {MIN_CLEAR_RATE:.0%}). A panel that clears almost "
        f"nothing cannot discriminate — check persona calibration."
    )


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

# `commercial` and `budget` were unrequirable by ANY input until 2026-08-22 —
# finding M7, and `tests/unit/test_specialists.py::
# test_only_the_documented_domains_are_reachable` proves reachability
# exhaustively rather than trusting it. That mattered most for `commercial`,
# which owns `differentiation`: the heaviest dimension on both scales (15
# investment / 16 incubation). The rubric weighted one dimension above all
# others and the floor could not demand an opinion on it.
#
# Both cue sets are deliberately narrow, and mirror the domain's own
# `consult_when` string rather than its whole `owns` remit. The cost model here
# is the inverted one `_cue_matches` documents: this runs AFTER the interview
# has ended, so a false positive cannot be repaired by asking one more question
# — it marks a finished verdict `panel_incomplete`.
_COMMERCIAL_CUES = (
    "differentiat", "first-in-class", "best-in-class", "first in class",
    "best in class", "competit", "competing", "investor", "commercial",
    "market opportunity", "market size",
)
# Four obvious candidates are absent by decision, not oversight, all on
# false-positive grounds: bare "deal" ("deal-breaking"), "landscape" (generic in
# rationale prose), "comparable" ("a comparable effect size"), and "market"
# alone — the prefix tier has no right-hand anchor, so "market" reaches
# "marketing", which is why the two multi-word forms are listed instead.
# `test_the_new_cues_do_not_fire_on_ordinary_verdict_prose` pins the first and
# the last.

_BUDGET_CUES = (
    "workplan", "work plan", "budget", "timeline", "burn rate",
    "capital efficien", "runway", "cost",
)
# "milestone" is NOT here on purpose. `_haystack` folds in every
# `suggested_derisking_milestones` entry, and a verdict at advance/conditional
# essentially always carries some, so the cue would fire on nearly every verdict
# the floor is checked against — a requirement that is always on is not a
# requirement, it is `_ALWAYS` with extra steps.

_ALWAYS: frozenset[str] = frozenset({"scientific", "talent"})

#: The outcomes held to the specialist floor. Read as BAND names as well as
#: recommendation names — ``band()`` returns exactly ``advance`` / ``conditional``
#: / ``pass`` on both scales, so one set covers both sides of ``panel_is_owed``.
#:
#: Lives here, next to ``required_domains_for``, because two very different
#: callers need the same answer and must not each keep their own copy: the engine
#: decides whether to COMPUTE a gap (``_specialist_floor_gap``), and the admin
#: detail page decides whether an empty gap means "verified" or merely "never
#: evaluated" (``assessment_detail._panel_state``). When those two disagreed, the
#: page claimed a verification the engine had never performed.
PANEL_REQUIRED_FOR: frozenset[str] = frozenset({"advance", "conditional"})

#: The only recommendation that buys an exemption: a decline. Use
#: ``panel_is_owed`` rather than this set — the exemption is conditional on the
#: computed band agreeing, and that logic must not be re-derived per caller.
#:
#: This used to be "everything not in ``PANEL_REQUIRED_FOR``", which made
#: ``route-to-incubation`` exempt on the rationale that "a decline costs
#: Blackbird nothing". Two things were wrong with that, both measured in run
#: 8b64a0e0:
#:
#: * ``route-to-incubation`` is not a decline. It is Blackbird's own positive
#:   outcome — the grant the programme exists to award — and 8 of the run's
#:   emitted verdicts carried it. The one recommendation that commits real money
#:   was the one nobody had to review.
#: * The test keyed on the MODEL-WRITTEN recommendation while the band is
#:   COMPUTED, and they disagree: 3 of the 4 ``conditional`` bands in the stored
#:   corpus carry ``recommendation='pass'``. So a verdict could score into
#:   ``conditional`` and exempt itself from the panel by writing one word.
#:
#: The prompt still tells the model the old rule
#: (``prompts/roles/scout_hub/phase4-thread-reply.md``: "`pass` and
#: `route-to-incubation` verdicts require no panel at all"), and it also does not
#: name ``commercial`` or ``budget`` under "Mandatory consults". Closing that gap
#: is a prompt change with its own sign-off; until it lands, a hub that follows
#: its prompt can be marked ``panel_incomplete`` for a rule it was never given.
#: That is a flag on a stored row, not a lost verdict — see
#: ``_persist_assessment``'s ``panel_incomplete=bool(gap)``.
PANEL_EXEMPT_RECOMMENDATIONS: frozenset[str] = frozenset({"pass"})


def _normalized(value: object) -> str | None:
    """A model-written field, lowercased and stripped, or None if unreadable.

    Both fields the gate reads arrive from an LLM through free text.
    ``_haystack`` already lowercases for cue matching; the gate must not be the
    one place where ``"Pass"`` means something different from ``"pass"``.
    """
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    return text or None


def panel_is_owed(recommendation: object, band: object = None) -> bool:
    """Was this verdict obliged to convene a panel at all?

    Either field can pull a verdict INTO the panel; neither can pull it out. A
    hub that wrote ``advance`` has said the thing the floor exists to check
    whatever the arithmetic came to, and a score that BANDS advance/conditional
    owes a panel whatever the hub chose to call it.

    Fail-CLOSED on anything unreadable — a missing, empty or off-contract
    recommendation is owed a panel. The old test was "not in {advance,
    conditional} ⇒ exempt", so every junk value bought an exemption silently.
    The asymmetry is deliberate and cheap in one direction only: a wrongly-owed
    panel costs a ``panel_incomplete`` flag on a row, while a wrongly-exempt one
    costs the review of a funding decision.

    Never raises: this runs inside ``_persist_assessment``'s try block, where an
    exception would be caught and logged as "Failed to persist assessment"
    *after* the row had already been committed.

    THREE call sites still test ``recommendation not in PANEL_REQUIRED_FOR``
    directly and must migrate here, or the engine and the page will answer this
    question differently — the exact drift ``PANEL_REQUIRED_FOR``'s own comment
    records: ``SimulationEngine._specialist_floor_gap`` and
    ``_floor_unverifiable_reason`` (both in src/agent/simulation.py, which is
    where the computed band is in scope), and
    ``assessment_detail._panel_state``, which has both columns on the row in
    front of it (``assessment.recommendation``, ``assessment.band``).
    """
    if _normalized(band) in PANEL_REQUIRED_FOR:
        return True
    return _normalized(recommendation) not in PANEL_EXEMPT_RECOMMENDATIONS


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
# "cost" joins them on the same evidence class rather than on a production
# count: the prefix tier would take it to "costume", "costly" and "costing",
# and only the first of those is a false positive worth naming — but a cue
# whose stems are that common is exactly what this tier is for. "costs" is
# still reached, by the plural rule.
_WORD_ONLY_CUES: frozenset[str] = frozenset({
    "als", "aso", "hit", "adc", "compound", "cost",
})


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


def required_domains_for(
    verdict: object, *, band: str | None = None,
) -> frozenset[str]:
    """Which specialists this verdict was obliged to consult, derived from the
    verdict's OWN content — or nothing at all, if it owed no panel.

    Derived rather than self-reported on purpose: a hub that skipped Chemistry
    must not also get to declare Chemistry unnecessary.

    ``band`` is the COMPUTED band (``blackbird_rubric.band`` over
    ``weighted_score``), and it participates in the gate alongside the verdict's
    own ``recommendation`` — see ``panel_is_owed`` for why one field was not
    enough. It is optional because every pre-existing caller predates it and
    must keep working unchanged; ``None`` means "no band computed", not "banded
    pass".

    The gate is applied HERE, not only in the engine's caller, so that a future
    caller which forgets it still cannot invent a panel for a decline — and so
    that an exempt verdict derives an empty set rather than a set the caller is
    trusted to ignore.

    Never raises — this runs inside ``_persist_assessment``'s try block, where an
    exception would be caught and logged as "Failed to persist assessment"
    *after* the row had already been committed.
    """
    if not isinstance(verdict, dict):
        # Unreadable, so unexempted: `panel_is_owed(None)` is True and the
        # always-required pair stands. This is the pre-gate behaviour, preserved.
        return _ALWAYS

    if not panel_is_owed(verdict.get("recommendation"), band):
        return frozenset()

    required = set(_ALWAYS)
    text = _haystack(verdict)

    if any(_cue_matches(cue, text) for cue in _CHEMISTRY_CUES):
        required.add("chemistry")
    if any(_cue_matches(cue, text) for cue in _CLINICAL_CUES):
        required.add("clinical")
    if any(_cue_matches(cue, text) for cue in _COMMERCIAL_CUES):
        required.add("commercial")
    if any(_cue_matches(cue, text) for cue in _BUDGET_CUES):
        required.add("budget")

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
