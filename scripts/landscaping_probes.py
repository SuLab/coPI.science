"""Phase 0 probes of the landscaping pipeline (spec 2026-10-30 §15), and ``enable-source``.

    python scripts/landscaping_probes.py <probe>
    python scripts/landscaping_probes.py enable-source <name>

Run inside a one-off container off the new image, so the production keys are used without
being read on a workstation. Each probe prints its finding as JSON and stores it in a
``LandscapeReport("probes")`` row (payload ``{"probe", "at", "finding"}``), which the pilot
report reads. A probe whose request fails stores ``{"error": ...}`` as its finding and still
exits 0: a failed probe is itself a finding. ``enable-source`` runs ``intake.ensure_sources``
first, so it works before the worker's first start; it is how an operator enables ``jhtv``
after reading its terms (spec §15).

Probes:
  odp-fields   one ODP applicant query: the field names present, and whether inventor cities are
  ortholog     Open Targets ``homologues`` against HGNC HCOP, on a fixed list of mouse genes
  reporter-pi  one RePORTER project's PI fields, and whether any carries an ORCID
  effort       one request each on Sonnet 5 and Haiku 5.5 through ``llm_client.build_request``
  rate-limits  ClinicalTrials.gov and Europe PMC at the starting pace, counting 429s

JHTV's terms are read by a person; S6 and S9 truncation rates come from the ledger.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import httpx
from sqlalchemy import func, select

from src.landscaping import intake, llm_client
from src.landscaping.evidence import opentargets
from src.landscaping.fulltext import EUROPEPMC_SEARCH_URL
from src.landscaping.sources import odp_patents
from src.models.landscape import LandscapeReport, LandscapeSource
from src.services import nih_reporter
from src.services.job_queue import job_error_text
from src.services.odp import ODP_SEARCH_URL, odp_headers

#: Mouse symbol and the human target's Ensembl gene id whose Open Targets ``homologues``
#: should list it (well-studied genes, so a missing ortholog is a route defect, not data).
ORTHOLOG_PAIRS = (
    ("Trp53", "ENSG00000141510"),
    ("Egfr", "ENSG00000146648"),
    ("Brca1", "ENSG00000012048"),
    ("Kras", "ENSG00000133703"),
    ("Apoe", "ENSG00000130203"),
)
#: HGNC Comparison of Orthology Predictions. The route's exact shape is what the probe
#: establishes, so a non-JSON or error answer is recorded as the finding.
HCOP_URL = "https://www.genenames.org/cgi-bin/hcop/search"
CTGOV_URL = "https://clinicaltrials.gov/api/v2/studies"
#: Seconds between requests, and requests per service, for the rate-limit probe (spec §6.1:
#: ClinicalTrials.gov 1/s, Europe PMC starting at 1/s).
RATE_PACE_S = 1.0
RATE_REQUESTS = 10
#: Models whose acceptance of adaptive thinking with ``output_config.effort`` is probed.
EFFORT_MODELS = ("claude-sonnet-5", "claude-haiku-5-5")
_EFFORT_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def _http_client() -> httpx.AsyncClient:
    """Client factory: a seam so tests can inject an httpx.MockTransport."""
    return httpx.AsyncClient(timeout=60.0, follow_redirects=True)


def _session_factory():
    """The application's session factory: a seam for tests."""
    from src.database import get_session_factory
    return get_session_factory()


async def probe_odp_fields(client: httpx.AsyncClient) -> dict:
    key = odp_patents._api_key()
    if not key:
        return {"error": "USPTO_API_KEY is not set"}
    body = {"q": odp_patents.QUERY, "pagination": {"offset": 0, "limit": 1}}
    resp = await client.post(ODP_SEARCH_URL, json=body, headers=odp_headers(key))
    resp.raise_for_status()
    bag = resp.json().get("patentFileWrapperDataBag") or []
    if not bag:
        return {"records": 0}
    app = bag[0]
    doc = odp_patents.to_document(app)
    return {
        "records": len(bag),
        "top_level_fields": sorted(app),
        "metadata_fields": sorted(app.get("applicationMetaData") or {}),
        "missing_fields": doc.extra["missing_fields"],
        "inventor_cities_present": bool(doc.raw_qualifier["inventor_cities"]),
    }


async def probe_ortholog(client: httpx.AsyncClient) -> dict:
    """Open Targets' mouse ``homologues`` of each human target, against HCOP's answer for
    the same mouse symbol."""
    genes = []
    for mouse, ensembl in ORTHOLOG_PAIRS:
        entry: dict = {"mouse": mouse, "ensembl": ensembl}
        try:
            resp = await client.post(opentargets.GRAPHQL_URL, json={
                "query": opentargets.TARGET_QUERY,
                "variables": {"id": ensembl, "baselineSize": 1}})
            resp.raise_for_status()
            target = ((resp.json().get("data") or {}).get("target")) or {}
            homologues = target.get("homologues") or []
            entry["open_targets_mouse"] = any(
                str(h.get("speciesName") or "").lower().startswith("mouse")
                and str(h.get("targetGeneSymbol") or "").lower() == mouse.lower()
                for h in homologues)
            entry["open_targets_homologue_count"] = len(homologues)
        except Exception as exc:  # noqa: BLE001 - a failing route is the finding
            entry["open_targets_error"] = job_error_text(exc)
        try:
            resp = await client.get(HCOP_URL, params={"query": mouse, "species": "mouse"})
            entry["hcop_status"] = resp.status_code
            entry["hcop_mentions_ensembl"] = ensembl in resp.text
        except Exception as exc:  # noqa: BLE001
            entry["hcop_error"] = job_error_text(exc)
        genes.append(entry)
    return {
        "genes": genes,
        "open_targets_found": sum(1 for g in genes if g.get("open_targets_mouse")),
        "hcop_found": sum(1 for g in genes if g.get("hcop_mentions_ensembl")),
    }


async def probe_reporter_pi(client: httpx.AsyncClient) -> dict:
    body = {
        "criteria": {"org_names": [nih_reporter.JHU_ORG_EXACT],
                     "fiscal_years": [datetime.now(UTC).year - 1]},
        "include_fields": ["ProjectNum", "PrincipalInvestigators"],
        "offset": 0, "limit": 1,
    }
    resp = await client.post(f"{nih_reporter.BASE}/projects/search", json=body)
    resp.raise_for_status()
    results = resp.json().get("results") or []
    pis = (results[0].get("principal_investigators") or []) if results else []
    fields = sorted({k for pi in pis if isinstance(pi, dict) for k in pi})
    return {
        "records": len(results),
        "pi_fields": fields,
        "orcid_present": any("orcid" in f.lower() for f in fields),
    }


async def probe_rate_limits(client: httpx.AsyncClient) -> dict:
    targets = {
        "clinicaltrials": (CTGOV_URL, {"query.term": "cancer", "pageSize": 1}),
        "europepmc": (EUROPEPMC_SEARCH_URL,
                      {"query": "cancer", "format": "json", "pageSize": 1}),
    }
    out: dict = {"pace_s": RATE_PACE_S, "requests_each": RATE_REQUESTS}
    for name, (url, params) in targets.items():
        statuses: Counter = Counter()
        for i in range(RATE_REQUESTS):
            if i:
                await asyncio.sleep(RATE_PACE_S)
            try:
                statuses[str((await client.get(url, params=params)).status_code)] += 1
            except httpx.HTTPError as exc:
                statuses[type(exc).__name__] += 1
        out[name] = {"statuses": dict(statuses), "rate_limited": statuses.get("429", 0)}
    return out


async def probe_effort(db_factory) -> dict:
    """One request per model through ``build_request`` (the screen builder: adaptive
    thinking, ``output_config.effort`` and a JSON schema). ``accepted`` is false on an API
    error (a 400 for an unsupported parameter lands in ``error``) or a refusal. Each call is
    ledgered."""
    out: dict = {}
    for model in EFFORT_MODELS:
        request = llm_client.build_request(
            "screen", model=model, system="Answer with a JSON object.",
            user='Reply {"answer": "ok"}.', schema=_EFFORT_SCHEMA)
        result = await llm_client.call_sync(
            "screen", request, db_factory=db_factory, item_ref=f"probe:effort:{model}")
        out[model] = {
            "accepted": result.error is None and result.stop_reason != "refusal",
            "stop_reason": result.stop_reason,
            "served_by": result.served_by,
            "effort": request["output_config"]["effort"],
            "error": result.error,
        }
    return out


#: Probes that need only an HTTP client.
_HTTP_PROBES: dict[str, Callable[[httpx.AsyncClient], Awaitable[dict]]] = {
    "odp-fields": probe_odp_fields,
    "ortholog": probe_ortholog,
    "reporter-pi": probe_reporter_pi,
    "rate-limits": probe_rate_limits,
}
PROBES = (*_HTTP_PROBES, "effort")


async def _run_probe(name: str, db_factory) -> dict:
    try:
        if name == "effort":
            return await probe_effort(db_factory)
        async with _http_client() as client:
            return await _HTTP_PROBES[name](client)
    except Exception as exc:  # noqa: BLE001 - a failed probe is recorded, not raised
        return {"error": job_error_text(exc)}


async def enable_source(db_factory, name: str) -> int:
    async with db_factory() as db:
        await intake.ensure_sources(db)
        row = (await db.execute(
            select(LandscapeSource).where(LandscapeSource.source == name))).scalar_one_or_none()
        if row is None:
            print(f"unknown source {name!r}; known: {', '.join(intake.SOURCES)}", file=sys.stderr)
            return 2
        row.enabled = True
        await db.commit()
    print(f"source {name} enabled")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in PROBES:
        sub.add_parser(name)
    enable = sub.add_parser("enable-source")
    enable.add_argument("name")
    return parser


async def run(argv: list[str] | None = None, *, db_factory=None) -> int:
    """Run one command; an unknown one exits nonzero through argparse (SystemExit 2)."""
    args = _parser().parse_args(argv)
    db_factory = db_factory or _session_factory()
    if args.command == "enable-source":
        return await enable_source(db_factory, args.name)
    finding = await _run_probe(args.command, db_factory)
    payload = {"probe": args.command, "at": datetime.now(UTC).isoformat(), "finding": finding}
    async with db_factory() as db:
        # clock_timestamp, not the transaction start: two reports of a kind need distinct
        # created_at values (uq_landscape_reports_kind_created).
        db.add(LandscapeReport(kind="probes", payload=payload, created_at=func.clock_timestamp()))
        await db.commit()
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(argv))


if __name__ == "__main__":
    sys.exit(main())
