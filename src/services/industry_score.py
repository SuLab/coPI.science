"""Pure, versioned industry-interest scorer. Bump SCORER_VERSION on ANY weight change."""
from bisect import bisect_right

SCORER_VERSION = "1.0.0"
SCORED_CLASSES = {"pharma_biotech", "device_dx", "other"}
CLASS_FACTOR = {"pharma_biotech": 1.0, "device_dx": 1.0, "other": 0.25}
WEIGHTS = {  # kind: (per distinct company, cap)
    "coauthor_company": (3.0, 30.0),
    "company_funder": (4.0, 20.0),
    "coi_relationship": (5.0, 25.0),
    "patent_filed": (4.0, 12.0),
    "patent_assigned": (10.0, 30.0),
    "trial_industry_collab": (3.0, 9.0),
    "sbir_sttr": (6.0, 12.0),
}
_ROLE_FACTOR = {"first": 1.5, "last": 1.5, "corresponding": 1.5, "inventor": 1.0, "overall_official": 1.0}


def _item_factor(e) -> float:
    ev = e.evidence or {}
    if e.kind == "coi_relationship":
        f = 1.0
    else:
        f = _ROLE_FACTOR.get(e.pi_role or "", 1.0)
        if (e.pi_role or "middle") == "middle" and (ev.get("author_count") or 0) > 30:
            f = 0.25
    if e.kind == "coi_relationship" and ev.get("relationship") in ("founder", "equity"):
        f *= 2.0
    return f


def score_evidence(rows) -> tuple[float, dict]:
    components: dict[str, dict] = {}
    best: dict[tuple[str, str], float] = {}
    for e in rows:
        if getattr(e, "vetoed_at", None) is not None or not e.in_tenure or e.kind not in WEIGHTS:
            continue
        if e.company_class not in SCORED_CLASSES:
            continue
        key = (e.kind, (e.company_name or e.external_id).lower())
        per, _ = WEIGHTS[e.kind]
        val = per * _item_factor(e) * CLASS_FACTOR[e.company_class]
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
