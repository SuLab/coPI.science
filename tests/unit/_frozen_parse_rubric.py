"""Verbatim copy of blackbird_rubric.parse_rubric at PHASE_BASE (Task 30), renamed
`frozen_parse_rubric`. It imports the module's unchanged helpers; only the function
body is frozen. Never edit this file except to re-point an import that moved."""
import hashlib
import tomllib
from pathlib import Path

from src.services.blackbird_rubric import (
    _EXPECTED_DIMENSION_COUNT,
    _REQUIRED_GATING_KEYS,
    Rubric,
    RubricDimension,
    RubricError,
    StageBar,
    _require_number,
    _require_str,
    _require_str_list,
)


def frozen_parse_rubric(path: Path) -> Rubric:
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
    # band()'s decision lines. _round_for_band's up-only correction relies on
    # every threshold it is checked against sitting exactly on the 0.01 display
    # grid: round(raw, 2) moves a value by less than half a grid step, which can
    # never carry it past a grid-aligned point in the direction away from
    # ``raw`` — only toward it. An off-grid threshold breaks that guarantee
    # silently, so it is rejected here.
    for name, threshold in (
        ("[banding].advance_min", advance_min),
        ("[banding].conditional_min", conditional_min),
    ):
        if round(threshold, 2) != threshold:
            raise RubricError(
                f"rubric document: {name} = {threshold} is not on the "
                "0.01 grid — _round_for_band's correction only handles rounding "
                "crossing a threshold upward; see its docstring"
            )
    if not advance_min > conditional_min:
        raise RubricError(
            "rubric document: [banding].advance_min must be > conditional_min"
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
        # A positive int (bool is an int subclass, so `weight = true` must not
        # parse as 1).
        weight = entry.get("weight")
        if isinstance(weight, bool) or not isinstance(weight, int) or weight <= 0:
            raise RubricError(
                f"rubric document: [[dimension]] {key!r}.weight must be a "
                "positive integer"
            )
        specialist_raw = entry.get("specialist")
        specialist = (
            _require_str(specialist_raw, f"[[dimension]] {key!r}.specialist")
            if specialist_raw is not None
            else None
        )
        # `evidence` is optional (four of the six dimensions have none), but a
        # PRESENT list must be non-empty strings — an empty evidence list is a
        # deleted checklist wearing the key.
        evidence_raw = entry.get("evidence")
        evidence: tuple[str, ...] = ()
        if evidence_raw is not None:
            evidence = _require_str_list(
                evidence_raw, f"[[dimension]] {key!r}.evidence"
            )
        dimensions.append(RubricDimension(
            key=key,
            weight=weight,
            title=_require_str(entry.get("title"), f"[[dimension]] {key!r}.title"),
            anchors=_require_str(entry.get("anchors"), f"[[dimension]] {key!r}.anchors"),
            evidence=evidence,
            specialist=specialist,
        ))
    keys = [d.key for d in dimensions]
    if len(set(keys)) != len(keys):
        duplicates = sorted({k for k in keys if keys.count(k) > 1})
        raise RubricError(f"rubric document: duplicate dimension key(s): {duplicates}")
    # Weights sum to 100 — a weighted mean whose denominator is not 100 is
    # still computable, but it is no longer on the 1-5 dimension scale the band
    # lines are expressed in.
    total_weight = sum(d.weight for d in dimensions)
    if total_weight != 100:
        raise RubricError(
            "rubric document: dimension weight values must sum to 100, "
            f"found {total_weight}"
        )

    def _section_text(table_name: str, field: str = "text") -> str:
        table = data.get(table_name)
        if not isinstance(table, dict):
            raise RubricError(f"rubric document: missing [{table_name}] table")
        return _require_str(table.get(field), f"[{table_name}].{field}")

    red_flags_table = data.get("red_flags")
    if not isinstance(red_flags_table, dict):
        raise RubricError("rubric document: missing [red_flags] table")

    # Per-domain stage bars. `source` names the clause each bar condenses and is
    # validated against the document's OWN keys — the point of the field is that
    # a reviewer can check the condensation against the original, and a source
    # naming nothing means the bar has drifted from the text it claims to quote.
    # `red_flags` and `scoring_preamble` are the two non-keyed sections a bar may
    # legitimately quote (both carry incubation-stage policy that belongs to no
    # single dimension).
    valid_sources = (
        {d.key for d in dimensions} | set(gating) | {"red_flags", "scoring_preamble"}
    )
    global_raw = data.get("stage_bar_global")
    if not isinstance(global_raw, dict):
        raise RubricError("rubric document: missing [stage_bar_global] table")
    global_source = _require_str(global_raw.get("source"), "[stage_bar_global].source")
    for named in (part.strip() for part in global_source.split(",")):
        if named not in valid_sources:
            raise RubricError(
                f"rubric document: stage_bar_global names unknown source {named!r}"
            )
    stage_bar_global = StageBar(
        domain="*",
        source=global_source,
        text=_require_str(global_raw.get("text"), "[stage_bar_global].text"),
    )
    stage_bars_raw = data.get("stage_bar")
    if not isinstance(stage_bars_raw, dict):
        raise RubricError("rubric document: missing [stage_bar.*] tables")
    stage_bars: dict[str, StageBar] = {}
    for domain, entry in stage_bars_raw.items():
        if not isinstance(entry, dict):
            raise RubricError(f"rubric document: [stage_bar.{domain}] is not a table")
        source = _require_str(entry.get("source"), f"[stage_bar.{domain}].source")
        for named in (part.strip() for part in source.split(",")):
            if named not in valid_sources:
                raise RubricError(
                    f"rubric document: stage_bar.{domain} names unknown source "
                    f"{named!r} — a bar's `source` must name a dimension key, a "
                    "gating key, `red_flags` or `scoring_preamble`, so the "
                    f"condensation stays checkable ({sorted(valid_sources)})"
                )
        stage_bars[domain] = StageBar(
            domain=domain,
            source=source,
            text=_require_str(entry.get("text"), f"[stage_bar.{domain}].text"),
        )

    return Rubric(
        version=version,
        date=_require_str(meta.get("date"), "[meta].date"),
        source=_require_str(meta.get("source"), "[meta].source"),
        content_hash=hashlib.sha256(raw_bytes).hexdigest()[:12],
        scale_min=int(scale_min),
        scale_max=int(scale_max),
        advance_min=advance_min,
        conditional_min=conditional_min,
        banding_semantics=_require_str(
            banding.get("semantics"), "[banding].semantics"
        ),
        banding_advisory_note=_require_str(
            banding.get("advisory_note"), "[banding].advisory_note"
        ),
        pass_label=_require_str(banding.get("pass_label"), "[banding].pass_label"),
        banding_conditional_note=_require_str(
            banding.get("conditional_note"), "[banding].conditional_note"
        ),
        intro=_section_text("intro"),
        gating=gating,
        dimensions=tuple(dimensions),
        scoring_preamble=_section_text("scoring", "preamble"),
        red_flags_intro=_require_str(red_flags_table.get("intro"), "[red_flags].intro"),
        red_flags=_require_str_list(red_flags_table.get("items"), "[red_flags].items"),
        recommendation=_section_text("recommendation"),
        heuristic=_section_text("heuristic"),
        stage_bars=stage_bars,
        stage_bar_global=stage_bar_global,
    )
