"""Open Targets Platform GraphQL (spec §6.3, §7.4 step 5).

The query fields are exactly those verified against the live API on 2026-10-30 (release
26.09). The platform names no rate limit but points bulk users to its downloads, so requests
are paced and counted: past `BULK_QUERY_TRIGGER` queries in one process a warning asks the
builder to move target lookups to the release files (§6.3, §17).
"""
from __future__ import annotations

import logging

import httpx

from src.landscaping import evidence
from src.services.http_pacing import Pacer

logger = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.platform.opentargets.org/api/v4/graphql"
MIN_INTERVAL_S = 0.5
_PACER = Pacer(MIN_INTERVAL_S)
#: §6.3's trigger for switching to the release files.
BULK_QUERY_TRIGGER = 20_000
#: Baseline-expression rows kept per target (the card needs the top tissues, not all 1,000+).
BASELINE_ROWS = 50

_queries = 0
_warned = False

TARGET_QUERY = """
query Target($id: String!, $baselineSize: Int!) {
  target(ensemblId: $id) {
    id
    approvedSymbol
    tractability { label modality value }
    isEssential
    depMapEssentiality { tissueName screens { cellLineName geneEffect } }
    safetyLiabilities { event eventId datasource }
    geneticConstraint { constraintType oe oeLower oeUpper score }
    functionDescriptions
    baselineExpression(page: {index: 0, size: $baselineSize}) {
      count
      rows { tissueBiosample { biosampleId biosampleName } median unit datasourceId specificity_score }
    }
    subcellularLocations { location source termSL }
    chemicalProbes { id control isHighQuality mechanismOfAction origin }
    drugAndClinicalCandidates {
      count
      rows { id maxClinicalStage drug { id name } diseases { diseaseFromSource disease { id name } } }
    }
    homologues { speciesId speciesName targetGeneId targetGeneSymbol homologyType isHighConfidence }
  }
}
"""

ASSOCIATION_QUERY = """
query Association($disease: String!, $target: String!) {
  disease(efoId: $disease) {
    associatedTargets(Bs: [$target]) { count rows { score datatypeScores { id score } } }
  }
}
"""

RELEASE_QUERY = "{ meta { dataVersion { year month } } }"

#: The target fields `ot_target` returns, in the order of §7.4 step 5's list.
TARGET_FIELDS = (
    "tractability", "isEssential", "depMapEssentiality", "safetyLiabilities",
    "geneticConstraint", "functionDescriptions", "baselineExpression", "subcellularLocations",
    "chemicalProbes", "drugAndClinicalCandidates", "homologues",
)


def _make_client() -> httpx.AsyncClient:
    """Client factory: a seam so tests can inject an httpx.MockTransport."""
    return evidence.make_client()


def _count_query() -> None:
    global _queries, _warned
    _queries += 1
    if _queries > BULK_QUERY_TRIGGER and not _warned:
        _warned = True
        logger.warning(
            "Open Targets: %d GraphQL queries in this process, past the %d trigger of spec "
            "§6.3; consider the release files for target lookups", _queries, BULK_QUERY_TRIGGER)


async def _graphql(query: str, variables: dict) -> dict:
    _count_query()
    body = await evidence.request_json(
        _make_client, GRAPHQL_URL, pacer=_PACER,
        json_body={"query": query, "variables": variables})
    if body.get("errors"):
        raise ValueError(f"Open Targets GraphQL error: {body['errors'][0].get('message')!r}")
    return body.get("data") or {}


def to_efo_id(mondo_id: str) -> str:
    """``MONDO:0005233`` → ``MONDO_0005233``, the form Open Targets uses."""
    return mondo_id.replace(":", "_", 1)


async def ot_release() -> str:
    """The platform's data release, e.g. ``26.09``."""
    data = await _graphql(RELEASE_QUERY, {})
    version = (data.get("meta") or {}).get("dataVersion") or {}
    return f"{version.get('year')}.{version.get('month')}"


async def ot_target(ensembl_id: str) -> dict:
    """The target's `TARGET_FIELDS`, plus ``id`` and ``approvedSymbol``; ``{}`` when Open
    Targets has no such target. A list field the API returns as null comes back empty."""
    data = await _graphql(TARGET_QUERY, {"id": ensembl_id, "baselineSize": BASELINE_ROWS})
    target = data.get("target")
    if not target:
        return {}
    out = {"id": target.get("id"), "approvedSymbol": target.get("approvedSymbol")}
    for name in TARGET_FIELDS:
        value = target.get(name)
        if name == "isEssential":
            out[name] = value
        elif name in ("baselineExpression", "drugAndClinicalCandidates"):
            out[name] = value or {"count": 0, "rows": []}
        else:
            out[name] = value or []
    return out


async def ot_association(ensembl_id: str, mondo_id: str) -> dict:
    """``{"overall": float | None, "genetic": float | None, "datatypes": {id: score}}`` for
    the target-disease pair; ``genetic`` is the ``genetic_association`` datatype score. Both
    are None when Open Targets has no association (or no such disease)."""
    data = await _graphql(ASSOCIATION_QUERY,
                          {"disease": to_efo_id(mondo_id), "target": ensembl_id})
    rows = (((data.get("disease") or {}).get("associatedTargets") or {}).get("rows")) or []
    if not rows:
        return {"overall": None, "genetic": None, "datatypes": {}}
    row = rows[0]
    datatypes = {d["id"]: d["score"] for d in row.get("datatypeScores") or [] if d.get("id")}
    return {"overall": row.get("score"), "genetic": datatypes.get("genetic_association"),
            "datatypes": datatypes}
