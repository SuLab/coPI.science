"""The pure, versioned industry-interest scorer, and the read-time view of a score row.

Bump SCORER_VERSION on ANY change to what a row's raw_sum means. 2.0.0 (spec 2026-10-05
§6.2, D10, D11): a COI relationship counts only when its statement credits the PI by name
or as "all/each author(s)" (`evidence.attributed`, written by
industry_sources/pubmed_coi.py), so a row written before 2.0.0 scores nothing and the
founder/equity x2 applies only to attributed hits; with a tenure start, a row dated before
it scores nothing (a source that could not refresh keeps its old rows).

The job writes raw_sum, components, coverage and evidence_count; the percentile and the
reason are computed when a page renders (`industry_view`, D13), against every PI's latest
row of this version, so they never go stale while other PIs are rescored."""
from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from src.services.industry_sources.registry import weights as _registry_weights

SCORER_VERSION = "2.0.0"
#: A coverage value meaning the source was not refreshed at this version (written by
#: industry_evidence.rescore_user): such a row never joins the read-time cohort.
NOT_REFRESHED = "unavailable:not_refreshed"
SCORED_CLASSES = {"pharma_biotech", "device_dx", "other"}
CLASS_FACTOR = {"pharma_biotech": 1.0, "device_dx": 1.0, "other": 0.25}
WEIGHTS = _registry_weights()  # kind: (per distinct company, cap) — built from the source registry
_ROLE_FACTOR = {"first": 1.5, "last": 1.5, "corresponding": 1.5, "inventor": 1.0, "overall_official": 1.0}
#: Other PIs with a cohort row (`cohort_raw`) a percentile needs; fewer is
#: "cohort_too_small" (the global cohort, not a per-field one).
MIN_COHORT_PEERS = 3
READ_TIME_REASONS = ("no_tenure_start", "no_evidence", "cohort_too_small", "ok")


def _attributed(e) -> bool:
    return (e.evidence or {}).get("attributed") is True


def _item_factor(e) -> float:
    ev = e.evidence or {}
    if e.kind == "coi_relationship":
        f = 1.0
    else:
        f = _ROLE_FACTOR.get(e.pi_role or "", 1.0)
        if (e.pi_role or "middle") == "middle" and (ev.get("author_count") or 0) > 30:
            f = 0.25
    if e.kind == "coi_relationship" and _attributed(e) and ev.get("relationship") in ("founder", "equity"):
        f *= 2.0
    return f


def _scores(e, tenure_start: int | None) -> bool:
    """Whether a row enters the sum: not vetoed, in tenure, a weighted kind, a scored class
    (patent_filed is exempt: its only producer, uspto_inventor.evidence_from_application,
    always emits company_class="unknown", since a merely filed patent has no assignee to
    classify, so gating it would make the kind dead weight despite its nonzero weight), an
    attributed COI hit, and, with a tenure start, dated at or after it."""
    if getattr(e, "vetoed_at", None) is not None or not e.in_tenure or e.kind not in WEIGHTS:
        return False
    if e.company_class not in SCORED_CLASSES and e.kind != "patent_filed":
        return False
    if e.kind == "coi_relationship" and not _attributed(e):
        return False
    return tenure_start is None or (e.year is not None and e.year >= tenure_start)


def score_evidence(rows, *, tenure_start: int | None = None) -> tuple[float, dict]:
    """(raw sum, per-kind components) over the rows that score (`_scores`): per kind, each
    distinct company's best value, summed and capped."""
    components: dict[str, dict] = {}
    best: dict[tuple[str, str], float] = {}
    for e in rows:
        if not _scores(e, tenure_start):
            continue
        key = (e.kind, (e.company_name or e.external_id).lower())
        per, _ = WEIGHTS[e.kind]
        val = per * _item_factor(e) * CLASS_FACTOR.get(e.company_class, 1.0)
        best[key] = max(best.get(key, 0.0), val)
    for (kind, _), val in best.items():
        c = components.setdefault(kind, {"distinct_companies": 0, "uncapped": 0.0})
        c["distinct_companies"] += 1
        c["uncapped"] += val
    raw = 0.0
    for kind, c in components.items():
        c["weighted"] = min(c["uncapped"], WEIGHTS[kind][1])
        raw += c["weighted"]
    return raw, components


def normalise(raw: float, cohort_raws: list[float]) -> float:
    if not cohort_raws:
        return 0.0
    s = sorted(cohort_raws)
    return round(100.0 * bisect_right(s, raw) / len(s), 1)


@dataclass(frozen=True)
class IndustryView:
    """One PI's industry score as a page shows it (spec 2026-10-05 §6.2, D13). `reason`
    is one of READ_TIME_REASONS for a current-version row, "rescoring" when the PI has
    only older-version rows and "not_computed" with none; `percentile` is set only for
    "ok"; `row` is the latest current-version PiIndustryScore (None otherwise)."""

    reason: str
    percentile: float | None
    row: Any | None
    coverage: dict[str, str] = field(default_factory=dict)

    @property
    def partial(self) -> bool:
        """Some source's coverage is not "ok" (the manager pages' badge)."""
        return any(state != "ok" for state in self.coverage.values())


def cohort_raw(row) -> float | None:
    """`row.raw_sum` when the row counts toward the cohort (a tenure start recorded and a
    positive raw sum: the old peer rule, which counted reasons ok/cohort_too_small with
    raw_sum > 0, and no NOT_REFRESHED source, so evidence scored before this version never
    shifts the peers), else None."""
    if row.tenure_start_used is None or row.raw_sum is None or row.raw_sum <= 0:
        return None
    if NOT_REFRESHED in (getattr(row, "coverage", None) or {}).values():
        return None
    return row.raw_sum


def industry_view(row, cohort: Sequence[float], *, has_older_row: bool) -> IndustryView:
    """The read-time view of `row`, a PI's latest SCORER_VERSION row (or None). `cohort`
    is `cohort_raw` of every PI's latest current-version row, the PI's own included:
    no tenure start is "no_tenure_start"; raw_sum <= 0 is "no_evidence" (the fix wave's
    guard: a percentile off an all-zero contribution means nothing); fewer than
    MIN_COHORT_PEERS other cohort members is "cohort_too_small"; else "ok" with
    `normalise(raw_sum, cohort)`, i.e. 100 x cume_dist."""
    if row is None:
        return IndustryView("rescoring" if has_older_row else "not_computed", None, None)
    coverage = dict(row.coverage or {})
    if row.tenure_start_used is None:
        return IndustryView("no_tenure_start", None, row, coverage)
    if row.raw_sum is None or row.raw_sum <= 0:
        return IndustryView("no_evidence", None, row, coverage)
    if len(cohort) - 1 < MIN_COHORT_PEERS:
        return IndustryView("cohort_too_small", None, row, coverage)
    return IndustryView("ok", normalise(row.raw_sum, list(cohort)), row, coverage)
