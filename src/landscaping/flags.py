"""Data deal-breaker flags (spec 2026-10-30 §7.4 step 5; L24).

Flags only: none of them ever excludes an idea. Each carries the evidence it was raised on,
so the S4 dossier and the detail page can show why.

- ``target_essential``: Open Targets ``isEssential``.
- ``safety_liabilities``: a non-empty Open Targets ``safetyLiabilities``.
- ``same_target_in_clinic``: a drug or clinical candidate against the target for the idea's
  disease or a Mondo descendant of it (Open Targets ``drugAndClinicalCandidates``), or a
  ClinicalTrials.gov trial for the disease and the target.
"""
from __future__ import annotations

from src.landscaping.evidence.mondo import is_ancestor

#: The three flag names, in display order.
FLAG_NAMES = ("target_essential", "safety_liabilities", "same_target_in_clinic")
#: At most this many evidence entries are kept per flag.
_EVIDENCE_CAP = 10


def _mondo(value: object) -> str | None:
    """A Mondo id in ``MONDO:nnnnnnn`` form; Open Targets writes ``MONDO_nnnnnnn``."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.upper().startswith("MONDO_"):
        return "MONDO:" + text[6:]
    return text


def _rows(ot_drugs: object) -> list[dict]:
    if isinstance(ot_drugs, dict):
        ot_drugs = ot_drugs.get("rows") or []
    return [row for row in ot_drugs or [] if isinstance(row, dict)]


def _row_diseases(row: dict) -> list[str]:
    """The disease ids a ``drugAndClinicalCandidates`` row names."""
    found: list[object] = [row.get("diseaseId")]
    single = row.get("disease")
    if isinstance(single, dict):
        found.append(single.get("id"))
    for entry in row.get("diseases") or []:
        if isinstance(entry, dict):
            inner = entry.get("disease")
            found.append(inner.get("id") if isinstance(inner, dict) else entry.get("id"))
    return [d for d in (_mondo(v) for v in found) if d]


def _row_label(row: dict) -> str:
    drug = row.get("drug")
    if isinstance(drug, dict):
        return str(drug.get("name") or drug.get("id") or "")
    return str(row.get("name") or row.get("id") or "")


def _matches(candidate: str, disease: str) -> bool:
    """`candidate` is the idea's disease or one of its Mondo descendants."""
    return candidate == disease or is_ancestor(disease, candidate)


def _trial_count(trials: dict | None) -> int:
    """Trials across phases: ``total`` when given, else the sum of the per-phase counts."""
    if not isinstance(trials, dict):
        return 0
    total = trials.get("total")
    if isinstance(total, int):
        return total
    phases = trials.get("by_phase") if isinstance(trials.get("by_phase"), dict) else trials
    return sum(v for v in phases.values() if isinstance(v, int) and not isinstance(v, bool))


def data_flags(ot_target: dict, ot_drugs: dict, trials: dict, *, disease: str) -> dict:
    """The three data flags for one target and the idea's disease (a Mondo id, or ``""``
    when the idea has none, which never matches a clinical row).

    Returns ``{flag: {"raised": bool, "evidence": ...}}`` for every name in `FLAG_NAMES`.
    Missing payload fields count as not raised."""
    ot_target = ot_target or {}
    essential = ot_target.get("isEssential")
    liabilities = [x for x in ot_target.get("safetyLiabilities") or [] if x]
    disease_id = _mondo(disease)
    clinic = []
    if disease_id:
        clinic = [
            {"candidate": _row_label(row), "diseases": ids}
            for row in _rows(ot_drugs)
            if (ids := [d for d in _row_diseases(row) if _matches(d, disease_id)])
        ]
    trial_count = _trial_count(trials) if disease_id else 0
    return {
        "target_essential": {
            "raised": essential is True,
            "evidence": {
                "isEssential": essential,
                "depMapEssentiality": ot_target.get("depMapEssentiality"),
            },
        },
        "safety_liabilities": {
            "raised": bool(liabilities),
            "evidence": liabilities[:_EVIDENCE_CAP],
        },
        "same_target_in_clinic": {
            "raised": bool(clinic) or trial_count > 0,
            "evidence": {"candidates": clinic[:_EVIDENCE_CAP], "trials": trial_count},
        },
    }


def combine_flags(per_target: dict[str, dict]) -> dict:
    """One idea's flags from ``{target id: data_flags(...)}``: a flag is raised when any
    target raises it, and its evidence maps each raising target (every target, when none
    raises) to that target's evidence. No targets gives every flag unraised."""
    out: dict = {}
    for name in FLAG_NAMES:
        entries = {t: f[name] for t, f in per_target.items() if name in f}
        raising = {t: e for t, e in entries.items() if e["raised"]}
        out[name] = {
            "raised": bool(raising),
            "evidence": {t: e["evidence"] for t, e in (raising or entries).items()},
        }
    return out
