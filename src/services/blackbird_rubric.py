"""Blackbird's weighted screening rubric (Part C.3 of
data/Blackbird_initial_priorities-criteria_v1.pdf).

The rubric CONTENT — weights, band thresholds, the 1-5 scale, scale anchors,
gating criteria, checklist, red flags, the decision heuristic — lives in one
manually reviewable document, ``prompts/rubric/blackbird-rubric.toml`` (its
header comment carries the editing workflow; the design is
docs/plans/2026-08-20-assessments-rca-ux-specialist-visibility.md §4). This
module loads that document ONCE at import, fails fast with ``RubricError`` if
it is invalid, computes the weighted score and band from it, and renders the
markdown section the scouting hub's system prompt carries (the ``{rubric}``
placeholder in prompts/roles/scout_hub/agent-system.md). Load-once is
deliberate: one process (and so one simulation run) scores against one rubric,
and a half-saved edit can never become a mid-run scoring incident — applying
an edit is a restart, verified by the version + content hash in the startup
banner.

The ``<assessment_json>`` sidecar's JSON contract is NOT here: it lives in
prompts/roles/scout_hub/phase4-thread-reply.md, which is authoritative for the
sidecar's shape (this module and that file are kept in sync by
tests/unit/test_rubric_prompt_sync.py). Before the rubric was extracted into
its own document, it lived in untracked profiles/private/blackbird.md; that
file was retired in the 2026-08-12 removal cycle and has since been diffed
against this document and archived — see
docs/audits/2026-08-20-rubric-extraction/.

The score is computed here rather than taken from the model's own
``weighted_score`` field: thirteen weights times thirteen 1-5 scores is
precisely the arithmetic an LLM gets plausibly wrong, and the band it lands in
decides whether a proposal advances.

**Two scales, selected by funnel stage** (v2.0.0, docs/specs/2026-08-20-rubric-
v2-incubation-rebaseline-proposal.md). The document carries an investment
weight/anchor/band-line set and an incubation one. ``weighted_score`` and
``band`` take an optional ``stage``; anything whose normalized form starts
"incubation" scores on the incubation scale, and **None, an unknown value, or
any other stage reproduces the v1 investment behaviour exactly** — that
default is deliberate and is what keeps every pre-v2 caller and pin correct.
The investment scale is not deprecated: it is the right scale for a later-stage
opportunity, and the reason every 2026-08 verdict banded "pass" was that it was
being applied to a 100% incubation-stage population, not that it was wrong.

Two failure modes matter enough to call out explicitly:

* NaN and +/-inf must never reach the same clamp as an ordinary out-of-range
  number. Python's min()/max() are order-dependent on NaN (every NaN
  comparison is False), so a naive numeric clamp scores NaN as a perfect 5.0 —
  and a malformed LLM verdict can produce NaN: ``json.loads`` accepts bare
  ``NaN``/``Infinity`` tokens by default. Non-finite values are therefore
  treated as unscorable (count as 0), the same as any other non-numeric value.
* The 2dp rounding of the return value is a display concern only. Rounding
  must never be what decides which side of a ``band()`` threshold a score
  lands on — see ``_round_for_band`` below.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# CWD-relative, the same convention src/agent/roles.py uses for PROMPTS_DIR:
# every process (uvicorn, the agent run, pytest) starts at the repo root, and
# prompts/ is bind-mounted into blackbird-app and agent (NOT worker, which
# mounts only ./profiles and never imports this module) so a document edit
# needs a restart of those two, not an image rebuild.
RUBRIC_PATH = Path("prompts/rubric/blackbird-rubric.toml")

# How many dimensions the document must define. Deliberate friction: adding or
# removing a dimension is a schema-level calibration change and must touch the
# validator and the characterization pin together, not slip in as a data edit.
_EXPECTED_DIMENSION_COUNT = 13

# The gating keys are structural, not editorial: they are the exact JSON keys
# of the `<assessment_json>` skeleton's `gating` object and of the
# `opportunity_assessments.gating` column, so the document may reword their
# titles/descriptions but must define exactly these three.
# v2.1.0 renamed the third gate: freedom-to-operate is diligence, not a gate,
# and translational potential took over the key (sidecar, TOML table and this
# tuple all rename together — tests/unit/test_rubric_prompt_sync.py holds them).
# Historical rows keep the `fto_achievable` key they were written with; the
# gating column is JSONB and the read paths render its keys generically.
_REQUIRED_GATING_KEYS = (
    "life_sciences_domain", "credible_tech_source", "translational_potential",
)


class RubricError(RuntimeError):
    """The rubric document is missing, unreadable, or fails validation.

    Raised at import time on purpose — a process that cannot read its rubric
    must not start, score, or render a prompt with a hole where the rubric
    belongs."""


@dataclass(frozen=True)
class RubricDimension:
    """One weighted scoring dimension, on both scales.

    ``weight``/``anchors`` are the investment scale; ``weight_incubation``/
    ``anchors_incubation`` the incubation one. ``specialist`` names the
    consultable evaluation-panel domain that owns the dimension (None where no
    single domain does) and is stage-independent — who to ask does not change
    with the funnel stage, only what a good answer looks like.
    """

    key: str
    weight: int
    title: str
    anchors: str
    weight_incubation: int
    anchors_incubation: str
    specialist: str | None = None


@dataclass(frozen=True)
class Rubric:
    """The parsed, validated rubric document."""

    version: str
    date: str
    source: str
    content_hash: str
    scale_min: int
    scale_max: int
    advance_min: float
    conditional_min: float
    advance_min_incubation: float
    conditional_min_incubation: float
    banding_incubation_semantics: str
    pass_label: str
    vocabulary_note: str
    banding_conditional_note: str
    banding_pass_note: str
    intro: str
    gating: dict[str, dict[str, str]]
    dimensions: tuple[RubricDimension, ...]
    funnel: str
    scoring_preamble: str
    checklist_intro: str
    checklist: tuple[str, ...]
    red_flags: tuple[str, ...]
    recommendation: str
    heuristic: str


def _require_str(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RubricError(f"rubric document: {where} must be a non-empty string")
    return value.strip()


def _require_number(value: object, where: str) -> float:
    # bool is an int subclass; `advance_min = true` must not parse as 1.0.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RubricError(f"rubric document: {where} must be a number")
    return float(value)


def _require_str_list(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise RubricError(f"rubric document: {where} must be a non-empty list of strings")
    return tuple(_require_str(item, f"{where}[{i}]") for i, item in enumerate(value))


def parse_rubric(path: Path) -> Rubric:
    """Parse and validate a rubric document. Raises ``RubricError`` on any
    defect — the caller (module import, below) deliberately does not catch it.

    Public so the validator is testable against scratch files without
    monkeypatching the module-level singleton.
    """
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise RubricError(f"rubric document unreadable at {path}: {exc}") from exc
    try:
        data = tomllib.loads(raw_bytes.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise RubricError(f"rubric document is not valid TOML ({path}): {exc}") from exc

    meta = data.get("meta")
    if not isinstance(meta, dict):
        raise RubricError("rubric document: missing [meta] table")
    version = _require_str(meta.get("version"), "[meta].version")
    if len(version) > 20:
        # opportunity_assessments.rubric_version is String(20) (alembic/versions/
        # 0030_specialist_consults_rubric_version.py). Never clip here or at the
        # write site to fit: silent truncation would let two distinct long
        # versions stamp identically, destroying pre/post-calibration
        # comparability. Fail fast instead.
        raise RubricError(
            "rubric document: [meta].version must be at most 20 characters "
            f"(opportunity_assessments.rubric_version is String(20)); got "
            f"{len(version)}: {version!r}"
        )

    scale = data.get("scale")
    if not isinstance(scale, dict):
        raise RubricError("rubric document: missing [scale] table")
    scale_min = _require_number(scale.get("min"), "[scale].min")
    scale_max = _require_number(scale.get("max"), "[scale].max")
    if scale_min != int(scale_min) or scale_max != int(scale_max):
        raise RubricError("rubric document: [scale] min/max must be integers")
    if not scale_min < scale_max:
        raise RubricError("rubric document: [scale].min must be < [scale].max")

    banding = data.get("banding")
    if not isinstance(banding, dict):
        raise RubricError("rubric document: missing [banding] table")
    advance_min = _require_number(banding.get("advance_min"), "[banding].advance_min")
    conditional_min = _require_number(
        banding.get("conditional_min"), "[banding].conditional_min"
    )
    banding_incubation = banding.get("incubation")
    if not isinstance(banding_incubation, dict):
        raise RubricError("rubric document: missing [banding.incubation] table")
    advance_min_incubation = _require_number(
        banding_incubation.get("advance_min"), "[banding.incubation].advance_min"
    )
    conditional_min_incubation = _require_number(
        banding_incubation.get("conditional_min"),
        "[banding.incubation].conditional_min",
    )
    # band()'s decision lines, on BOTH scales. _round_for_band's up-only
    # correction relies on every threshold it is checked against sitting exactly
    # on the 0.01 display grid: round(raw, 2) moves a value by less than half a
    # grid step, which can never carry it past a grid-aligned point in the
    # direction away from ``raw`` — only toward it. An off-grid threshold breaks
    # that guarantee silently, so it is rejected here (this used to be a
    # module-load assert). Both scales are checked because weighted_score()
    # rounds against whichever pair its ``stage`` selected.
    for name, threshold in (
        ("[banding].advance_min", advance_min),
        ("[banding].conditional_min", conditional_min),
        ("[banding.incubation].advance_min", advance_min_incubation),
        ("[banding.incubation].conditional_min", conditional_min_incubation),
    ):
        if round(threshold, 2) != threshold:
            raise RubricError(
                f"rubric document: {name} = {threshold} is not on the "
                "0.01 grid — _round_for_band's correction only handles rounding "
                "crossing a threshold upward; see its docstring"
            )
    for table, hi, lo in (
        ("[banding]", advance_min, conditional_min),
        ("[banding.incubation]", advance_min_incubation, conditional_min_incubation),
    ):
        if not hi > lo:
            raise RubricError(
                f"rubric document: {table}.advance_min must be > conditional_min"
            )

    gating_raw = data.get("gating")
    if not isinstance(gating_raw, dict):
        raise RubricError("rubric document: missing [gating] tables")
    gating: dict[str, dict[str, str]] = {}
    for key in _REQUIRED_GATING_KEYS:
        entry = gating_raw.get(key)
        if not isinstance(entry, dict):
            raise RubricError(f"rubric document: missing [gating.{key}] table")
        gating[key] = {
            "title": _require_str(entry.get("title"), f"[gating.{key}].title"),
            "description": _require_str(
                entry.get("description"), f"[gating.{key}].description"
            ),
        }
    unknown_gates = sorted(set(gating_raw) - set(_REQUIRED_GATING_KEYS))
    if unknown_gates:
        raise RubricError(
            "rubric document: unknown gating key(s) "
            f"{unknown_gates} — the three gating keys are structural (they are "
            "the sidecar's JSON keys) and cannot be added to by editing this file"
        )

    dims_raw = data.get("dimension")
    if not isinstance(dims_raw, list):
        raise RubricError("rubric document: missing [[dimension]] entries")
    if len(dims_raw) != _EXPECTED_DIMENSION_COUNT:
        raise RubricError(
            f"rubric document: expected exactly {_EXPECTED_DIMENSION_COUNT} "
            f"dimensions, found {len(dims_raw)}"
        )
    dimensions: list[RubricDimension] = []
    for i, entry in enumerate(dims_raw):
        if not isinstance(entry, dict):
            raise RubricError(f"rubric document: [[dimension]] #{i + 1} is not a table")
        key = _require_str(entry.get("key"), f"[[dimension]] #{i + 1}.key")
        # Both weight sets, same rule: a positive int (bool is an int subclass,
        # so `weight = true` must not parse as 1).
        weights: dict[str, int] = {}
        for field in ("weight", "weight_incubation"):
            value = entry.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise RubricError(
                    f"rubric document: [[dimension]] {key!r}.{field} must be a "
                    "positive integer"
                )
            weights[field] = value
        specialist_raw = entry.get("specialist")
        specialist = (
            _require_str(specialist_raw, f"[[dimension]] {key!r}.specialist")
            if specialist_raw is not None
            else None
        )
        dimensions.append(RubricDimension(
            key=key,
            weight=weights["weight"],
            title=_require_str(entry.get("title"), f"[[dimension]] {key!r}.title"),
            anchors=_require_str(entry.get("anchors"), f"[[dimension]] {key!r}.anchors"),
            weight_incubation=weights["weight_incubation"],
            # Non-empty for all thirteen, same as `anchors`: the anchors ARE the
            # scale, so a dimension with an incubation weight and no incubation
            # anchor would be scored against the investment bar it was
            # re-baselined away from.
            anchors_incubation=_require_str(
                entry.get("anchors_incubation"),
                f"[[dimension]] {key!r}.anchors_incubation",
            ),
            specialist=specialist,
        ))
    keys = [d.key for d in dimensions]
    if len(set(keys)) != len(keys):
        duplicates = sorted({k for k in keys if keys.count(k) > 1})
        raise RubricError(f"rubric document: duplicate dimension key(s): {duplicates}")
    # Each scale sums to 100 INDEPENDENTLY — a weighted mean whose denominator
    # is not 100 is still computable, but it is no longer on the 1-5 dimension
    # scale the band lines are expressed in, so the two scales' scores would
    # stop being comparable to each other or to their own thresholds.
    for label, total_weight in (
        ("", sum(d.weight for d in dimensions)),
        ("_incubation", sum(d.weight_incubation for d in dimensions)),
    ):
        if total_weight != 100:
            raise RubricError(
                f"rubric document: dimension weight{label} values must sum to 100, "
                f"found {total_weight}"
            )

    def _section_text(table_name: str, field: str = "text") -> str:
        table = data.get(table_name)
        if not isinstance(table, dict):
            raise RubricError(f"rubric document: missing [{table_name}] table")
        return _require_str(table.get(field), f"[{table_name}].{field}")

    checklist_table = data.get("checklist")
    if not isinstance(checklist_table, dict):
        raise RubricError("rubric document: missing [checklist] table")
    red_flags_table = data.get("red_flags")
    if not isinstance(red_flags_table, dict):
        raise RubricError("rubric document: missing [red_flags] table")

    return Rubric(
        version=version,
        date=_require_str(meta.get("date"), "[meta].date"),
        source=_require_str(meta.get("source"), "[meta].source"),
        content_hash=hashlib.sha256(raw_bytes).hexdigest()[:12],
        scale_min=int(scale_min),
        scale_max=int(scale_max),
        advance_min=advance_min,
        conditional_min=conditional_min,
        advance_min_incubation=advance_min_incubation,
        conditional_min_incubation=conditional_min_incubation,
        banding_incubation_semantics=_require_str(
            banding_incubation.get("semantics"), "[banding.incubation].semantics"
        ),
        pass_label=_require_str(banding.get("pass_label"), "[banding].pass_label"),
        vocabulary_note=_require_str(
            banding.get("vocabulary_note"), "[banding].vocabulary_note"
        ),
        banding_conditional_note=_require_str(
            banding.get("conditional_note"), "[banding].conditional_note"
        ),
        banding_pass_note=_require_str(banding.get("pass_note"), "[banding].pass_note"),
        intro=_section_text("intro"),
        gating=gating,
        dimensions=tuple(dimensions),
        funnel=_section_text("funnel"),
        scoring_preamble=_section_text("scoring", "preamble"),
        checklist_intro=_require_str(checklist_table.get("intro"), "[checklist].intro"),
        checklist=_require_str_list(checklist_table.get("items"), "[checklist].items"),
        red_flags=_require_str_list(red_flags_table.get("items"), "[red_flags].items"),
        recommendation=_section_text("recommendation"),
        heuristic=_section_text("heuristic"),
    )


# Loaded ONCE, at import, failing fast — see the module docstring for why
# load-once beats live reload here.
_RUBRIC = parse_rubric(RUBRIC_PATH)


def load_rubric() -> Rubric:
    """The parsed rubric document (the import-time singleton)."""
    return _RUBRIC


# Percentage weights, from the document. Each sums to 100 (validated above).
# Both dicts iterate in the document's display order, which is deliberately the
# SAME order for both scales: a page or prompt that renders one after the other
# is then comparing rows, and a caller can swap dicts without reordering.
RUBRIC_WEIGHTS: dict[str, int] = {d.key: d.weight for d in _RUBRIC.dimensions}
RUBRIC_WEIGHTS_INCUBATION: dict[str, int] = {
    d.key: d.weight_incubation for d in _RUBRIC.dimensions
}

# Display metadata for the pages that render scores: the thresholds and the
# decline-vocabulary label. band()'s RETURN VALUE stays the stable stored
# vocabulary ("advance"/"conditional"/"pass") — pass_label is presentation
# only and must never be written to the database.
BANDING: dict[str, object] = {
    "advance_min": _RUBRIC.advance_min,
    "conditional_min": _RUBRIC.conditional_min,
    "pass_label": _RUBRIC.pass_label,
}

# The same three keys, on the incubation scale — same SHAPE on purpose, so a
# template or view that renders one can render the other with no branching.
# The band names (and so pass_label) are shared between the scales; only the
# lines move.
BANDING_INCUBATION: dict[str, object] = {
    "advance_min": _RUBRIC.advance_min_incubation,
    "conditional_min": _RUBRIC.conditional_min_incubation,
    "pass_label": _RUBRIC.pass_label,
}

RUBRIC_VERSION: str = _RUBRIC.version
RUBRIC_CONTENT_HASH: str = _RUBRIC.content_hash

_MIN_SCORE = _RUBRIC.scale_min
_MAX_SCORE = _RUBRIC.scale_max
_TOTAL_WEIGHT = sum(RUBRIC_WEIGHTS.values())
_TOTAL_WEIGHT_INCUBATION = sum(RUBRIC_WEIGHTS_INCUBATION.values())

# band()'s decision lines, in ascending order, mirrored here so the display
# rounding in weighted_score() can be checked against them. Grid-alignment is
# validated by parse_rubric (it used to be asserted here at module load).
_BAND_THRESHOLDS = (_RUBRIC.conditional_min, _RUBRIC.advance_min)
_BAND_THRESHOLDS_INCUBATION = (
    _RUBRIC.conditional_min_incubation, _RUBRIC.advance_min_incubation,
)

# The funnel-stage values that select the incubation scale. Matched as a PREFIX
# of the normalized stage, because the value comes from an LLM through a free
# text field: the sidecar skeleton offers "incubation", the rubric's own funnel
# section says "Incubation/Grant", and production has emitted both. A prefix
# match covers those and "Incubation (grant)" without enumerating spellings.
_INCUBATION_STAGE_PREFIX = "incubation"


def is_incubation_stage(stage: object) -> bool:
    """Does ``stage`` name the incubation/grant funnel stage?

    ``None``, an empty/whitespace value, an unknown stage, and every later
    stage all answer False and therefore score on the investment scale. That
    asymmetry is the safe default in both directions: an unrecognized stage
    keeps the pre-v2 behaviour every existing caller and pin expects, and the
    incubation scale — the more permissive one, with lower band lines — is only
    ever applied when the verdict actually asked for it.

    Public (not ``_is_incubation``) because it is not just this module's own
    scoring decision any more: a page rendering an ALREADY-STORED assessment
    (src/services/assessment_detail.py, src/services/directory.py) needs the
    identical normalization to pick which scale to DISPLAY a row against, and
    a second, template- or view-level reimplementation of "strip, lower,
    prefix-match 'incubation'" is exactly the kind of copy that drifts. See
    ``display_scale_for`` below, which is what those callers actually use.
    """
    if stage is None:
        return False
    return str(stage).strip().lower().startswith(_INCUBATION_STAGE_PREFIX)


def weighted_score(scores: dict[str, object] | None, stage: object = None) -> float:
    """Weighted mean of the thirteen dimensions, on the same 1-5 scale.

    A dimension that is missing, not a number, or not finite (NaN, +inf,
    -inf) counts as 0 — an unscored dimension must drag the total down, never
    be quietly excluded from the denominator, or a verdict that skipped its
    weakest dimensions would outscore one that answered honestly.

    Keys are matched case- and whitespace-insensitively: a verdict spelling a
    dimension ``"Differentiation"`` instead of ``"differentiation"`` must
    still hit its rubric weight rather than being silently treated as missing
    (and thus scored 0) purely because of casing (Finding A5).

    ``stage`` is the verdict's raw ``funnel_stage``; normalization is this
    module's job, not the caller's. An incubation stage selects the incubation
    weights and rounds against the incubation band lines. Omitting it — or
    passing any other stage — is exactly the v1 investment computation.
    """
    if not scores:
        return 0.0
    incubation = is_incubation_stage(stage)
    weights = RUBRIC_WEIGHTS_INCUBATION if incubation else RUBRIC_WEIGHTS
    total_weight = _TOTAL_WEIGHT_INCUBATION if incubation else _TOTAL_WEIGHT
    thresholds = _BAND_THRESHOLDS_INCUBATION if incubation else _BAND_THRESHOLDS
    # Normalize once. If two differently-cased keys collapse to the same
    # canonical name (e.g. both "team" and "Team" present), the later one in
    # iteration order wins — an unlikely input, but a deterministic pick beats
    # an arbitrary dict-merge accident.
    normalized = {
        key.strip().lower(): value for key, value in scores.items() if isinstance(key, str)
    }
    total = 0.0
    clamped: dict[str, float] = {}
    for key, weight in weights.items():
        raw = normalized.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        value = float(raw)
        if not math.isfinite(value):
            # NaN and +/-inf are unscorable, not "off the scale" — without
            # this explicit check they would sail past the isinstance guard
            # above (isinstance(float("nan"), float) is True) and into the
            # min()/max() clamp below, where NaN comparisons are always False
            # and silently produce a perfect 5.0.
            continue
        if not _MIN_SCORE <= value <= _MAX_SCORE:
            clamped[key] = value
        total += max(_MIN_SCORE, min(_MAX_SCORE, value)) * weight

    if clamped:
        # Same policy as `unmatched` below: diagnosable, not fatal. The clamp
        # keeps an out-of-contract number from poisoning the mean, but it also
        # means the STORED score no longer equals the score that was counted —
        # run ee419dd3 stored three explicit 0s on the 1-5 scale that were
        # silently counted as 1s, and an audit averaging the stored JSONB got
        # per-dimension means that matched nothing the computation ever saw.
        # (NaN/inf take the unscorable branch above, which counts as 0, and
        # are deliberately not reported as clamped — they were not.)
        logger.warning(
            "weighted_score: score(s) outside the %s-%s scale were clamped "
            "into range: %s", _MIN_SCORE, _MAX_SCORE, clamped,
        )

    unmatched = sorted(set(normalized) - weights.keys())
    if unmatched:
        # Diagnosable, not fatal: each unmatched key already counts as 0 via
        # the .get(key) miss above (or was never a rubric key to begin with).
        # This just makes a malformed/misspelled verdict findable instead of
        # a silently low score.
        logger.warning(
            "weighted_score: verdict has key(s) not in the thirteen rubric "
            "dimensions, scored as unset: %s", unmatched,
        )
    return _round_for_band(total / total_weight, thresholds)


def _round_for_band(
    raw: float, thresholds: tuple[float, ...] = _BAND_THRESHOLDS
) -> float:
    """Round ``raw`` to 2dp without letting the rounding cross a band()
    threshold.

    ``thresholds`` must be the decision lines of the SAME scale the score was
    computed on — rounding away from the investment lines while banding against
    the incubation ones is how a 2.695 would display as 2.70 and then band
    "conditional" off a value that is truly below the 2.7 line. It defaults to
    the investment pair so the pre-v2 single-argument call is unchanged.

    round(3.995, 2) == 4.0: a true mean of 3.995 is < 4.0 and must band as
    "conditional", but the naively-rounded display value of 4.0 would band as
    "advance". Rounding is for display; which band a score falls in must be
    decided from the true value. If naive rounding would move the value to
    the other side of a threshold from where the true value sits, round
    toward the true value's side instead — still to 2dp, just not to the
    nearest one.

    Only one direction is handled: naive rounding pushing ``raw`` UP across a
    threshold it was truly below (the floor correction). The opposite
    correction (``raw`` truly at/above a threshold, naive rounding pushing it
    below) can only happen if a threshold is off the 0.01 grid — rejected by
    parse_rubric at import — so it is not implemented here; adding an
    unreachable branch "just in case" would be dead code with no test able
    to prove it correct.
    """
    rounded = round(raw, 2)
    for threshold in thresholds:
        if raw < threshold <= rounded:
            return round(math.floor(raw * 100) / 100, 2)
    return rounded


def band(score: float, stage: object = None) -> str:
    """Part C.3 banding: >=advance_min advance, conditional_min-… conditional,
    below that pass.

    'pass' here means pass ON the deal (decline), matching the PDF's vocabulary —
    not 'passing' the screen. The returned value is the stable stored vocabulary;
    the display label for the decline band is ``BANDING["pass_label"]``.

    The three band NAMES are shared by both scales — only the lines move — so
    ``stage`` changes which thresholds are applied, never what can come back.
    That is what makes the incubation re-baseline a data change rather than a
    schema one: ``opportunity_assessments.band`` holds the same three values as
    before, and a stored band stays interpretable against whichever scale the
    row's ``rubric_version`` and ``funnel_stage`` say produced it. Pass the same
    ``stage`` here as to ``weighted_score`` — banding a score computed on one
    scale against the other's lines is meaningless.
    """
    if is_incubation_stage(stage):
        if score >= _RUBRIC.advance_min_incubation:
            return "advance"
        if score >= _RUBRIC.conditional_min_incubation:
            return "conditional"
        return "pass"
    if score >= _RUBRIC.advance_min:
        return "advance"
    if score >= _RUBRIC.conditional_min:
        return "conditional"
    return "pass"


# Leading digits of a version string, e.g. "2.0.0" -> "2", "10.0.0" -> "10".
# `re.match` anchors at position 0 already; `.strip()` before matching absorbs
# incidental whitespace without needing it in the pattern.
_MAJOR_VERSION_RE = re.compile(r"^(\d+)")


def scored_stage_aware(rubric_version: str | None) -> bool:
    """Did the document that produced ``rubric_version`` select its scale from
    the verdict's funnel stage, the way v2.0.0+ does?

    Rows scored before the incubation re-baseline — ``rubric_version`` is
    ``None`` (29 in production, unstamped rows predating the column) or
    ``"1.0.0"`` (5 more, stamped but pre-scale) — were scored on the
    INVESTMENT weights unconditionally, regardless of what their
    ``funnel_stage`` says. So a page that displays a stored row must gate its
    choice of scale on the row's OWN ``rubric_version``, not on funnel_stage
    alone: reading a v1 row's funnel_stage as "incubation" and showing it next
    to incubation weights would show numbers that were never applied to it.
    See ``display_scale_for`` below, which does exactly that.

    Compares the leading MAJOR version as an integer, never the raw string:
    ``"10.0.0" < "2.0.0"`` lexically, but major version 10 is very much
    ``>= 2``. ``None`` and any version with no leading integer ("garbage")
    both answer False — the same conservative default as
    ``is_incubation_stage``: an unparseable or absent stamp renders as the
    scale that was actually (and knowably) applied, the investment one, never
    a guess at the newer one.
    """
    if rubric_version is None:
        return False
    match = _MAJOR_VERSION_RE.match(rubric_version.strip())
    if match is None:
        return False
    return int(match.group(1)) >= 2


@dataclass(frozen=True)
class DisplayScale:
    """Which scale to render an ALREADY-STORED assessment against.

    Distinct from the SCORING decision (`weighted_score`/`band`, which act on
    a fresh verdict and take a bare ``stage``): this is a DISPLAY decision
    about a row that already has a ``weighted_score`` and a ``band`` computed
    at write time, so it must reconstruct — not recompute — what scale
    actually produced them.
    """

    incubation: bool
    weights: dict[str, int]
    banding: dict[str, object]
    label: str


def display_scale_for(rubric_version: str | None, funnel_stage: object) -> DisplayScale:
    """The scale a stored assessment was actually scored on, for display.

    Incubation iff BOTH hold: the row was scored by a stage-aware document
    (``scored_stage_aware(rubric_version)``) AND its ``funnel_stage``
    normalizes to incubation (``is_incubation_stage``) — the same rule
    ``weighted_score``/``band`` apply at write time, replayed here from the
    two columns a stored row actually carries. A legacy row (pre-v2
    ``rubric_version``) therefore always renders on the investment scale it
    was really scored on, even when its ``funnel_stage`` says "incubation":
    34 such rows exist in production, and showing them against incubation
    weights they were never scored against would be a caption, not a
    correction.
    """
    incubation = scored_stage_aware(rubric_version) and is_incubation_stage(funnel_stage)
    return DisplayScale(
        incubation=incubation,
        weights=RUBRIC_WEIGHTS_INCUBATION if incubation else RUBRIC_WEIGHTS,
        banding=BANDING_INCUBATION if incubation else BANDING,
        label="incubation scale (rubric v2)" if incubation else "investment scale",
    )


def _format_threshold(value: float) -> str:
    """A threshold for prose: 2dp with a bare trailing zero dropped —
    4.0 -> "4.0", 3.9 -> "3.9", 3.25 -> "3.25"."""
    text = f"{value:.2f}"
    return text[:-1] if text.endswith("0") else text


def render_rubric_markdown() -> str:
    """The markdown rubric section the scouting hub's system prompt carries.

    Fills the ``{rubric}`` placeholder in prompts/roles/scout_hub/
    agent-system.md (see Agent._compose_system_prompt). Every number in the
    output — weights, thresholds, the scale — comes from the document, so the
    prompt the hub reads and the score this module computes can never drift
    apart.

    BOTH scales are rendered, side by side in one table rather than as two
    stage-scoped blocks. Two reasons: the model is choosing a funnel stage in
    the same turn it scores, so seeing "what a 4 means here vs there" on one
    row is the comparison it actually has to make; and it keeps the investment
    columns leftmost and byte-identical, so the existing row pins in
    tests/unit/test_rubric_document.py and test_rubric_prompt_sync.py still
    hold as prefixes of the wider row.
    """
    r = _RUBRIC
    lines: list[str] = [
        "## Blackbird's Screening Rubric",
        "",
        r.intro,
        "",
        '### 1. Gating criteria (pass/fail — a "no" blocks or heavily discounts)',
    ]
    for gate in r.gating.values():
        lines.append(f"- **{gate['title']}** — {gate['description']}")
    lines += [
        "",
        "### 2. Funnel stage (sets the evidence bar)",
        r.funnel,
        "",
        f"### 3. Weighted scoring dimensions (score each {r.scale_min}–{r.scale_max}; "
        f"{r.scale_max} = strongly meets the bar)",
        r.scoring_preamble,
        "",
        "**Score against the anchor column for the funnel stage you assigned in step "
        "(2)** — the incubation columns for an Incubation/Grant idea, the investment "
        "columns for pre-seed and later. The two are different bars, not a strict and "
        "a lenient version of one bar: the investment anchors ask what has been "
        "PROVEN, the incubation anchors ask whether a grant could prove it.",
        "",
        "| # | Dimension | What to look for (investment: pre-seed and later) | Wt "
        "(inv) | What to look for (INCUBATION / grant stage) | Wt (inc) |",
        "|---|---|---|---|---|---|",
    ]
    for i, dim in enumerate(r.dimensions, start=1):
        lines.append(
            f"| {i} | {dim.title} | {dim.anchors} | {dim.weight}% | "
            f"{dim.anchors_incubation} | {dim.weight_incubation}% |"
        )
    conditional_upper = _format_threshold(r.advance_min - 0.1)
    conditional_upper_incubation = _format_threshold(r.advance_min_incubation - 0.1)
    lines += [
        "",
        f"**Banding (investment scale):** ≥{_format_threshold(r.advance_min)} → "
        f"advance/recommend; {_format_threshold(r.conditional_min)}–"
        f"{conditional_upper} → conditional ({r.banding_conditional_note}); "
        f"<{_format_threshold(r.conditional_min)} → pass ({r.banding_pass_note}).",
        "",
        "**Banding (incubation scale):** "
        f"≥{_format_threshold(r.advance_min_incubation)} → advance; "
        f"{_format_threshold(r.conditional_min_incubation)}–"
        f"{conditional_upper_incubation} → conditional; "
        f"<{_format_threshold(r.conditional_min_incubation)} → pass. "
        f"What each band commits someone to: {r.banding_incubation_semantics}.",
        "",
        "The score itself is computed for you from your dimension scores and the "
        "weight column for the stage you assigned — do not compute it, and do not "
        "pick a stage to reach a band.",
        "",
        "### 4. Target-level scientific checklist (for therapeutic/target proposals)",
        r.checklist_intro,
    ]
    lines += [f"- {item}" for item in r.checklist]
    lines += [
        "",
        "### 5. Red flags / disqualifiers (call out explicitly)",
    ]
    lines += [f"- {item}" for item in r.red_flags]
    lines += [
        "",
        "### 6. Structured recommendation",
        r.recommendation,
        "",
        "### One-line decision heuristic",
        r.heuristic,
    ]
    return "\n".join(lines)
