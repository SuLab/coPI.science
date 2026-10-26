"""The search tools of BlackbirdBot's project research (PubMed, ClinicalTrials.gov, US
patents, abstracts), shared by the worker's research sessions and the hub in a project
thread.

Every result is untrusted external text and is fenced with ``prompt_safety.delimit``.
Each tool also reports the sources it returned, keyed by ``project_plan.source_key`` with
the kind of the tool that found them, so a later citation can be checked against what was
actually retrieved.

This module must not import ``src.services.industry_sources`` (or anything that reaches
it): ``src/agent/tools.py`` imports it, and ``tests/unit/test_enrichment_isolation.py``
fails any module under ``src/agent/`` that reaches the industry-score sources.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from src.agent.prompt_safety import delimit
from src.agent.tool_definitions import PROJECT_TOOL_DEFINITIONS, TOOL_DEFINITIONS
from src.services.http_pacing import Pacer
from src.services.patents import search_prior_art
from src.services.project_plan import source_key
from src.services.pubmed import fetch_abstract, fetch_pubmed_records, search_pmids

logger = logging.getLogger(__name__)

#: The ClinicalTrials.gov v2 endpoint, the same as ``industry_sources.ctgov.BASE``;
#: written out rather than imported (see the module docstring).
CTGOV_STUDIES_URL = "https://clinicaltrials.gov/api/v2/studies"
#: The v2 modules to return, nested ``protocolSection.*`` shape (checked against the live
#: API on 2026-10-26: ``designModule.phases``, ``armsInterventionsModule.interventions[]``
#: with ``type`` and ``name``).
TRIALS_FIELDS = (
    "protocolSection.identificationModule,protocolSection.statusModule,"
    "protocolSection.conditionsModule,protocolSection.designModule,"
    "protocolSection.armsInterventionsModule"
)
#: Seconds between the starts of two ClinicalTrials.gov requests, process-wide.
TRIALS_MIN_INTERVAL_S = 1.0
_TRIALS_PACER = Pacer(TRIALS_MIN_INTERVAL_S)
_TRIALS_PAGE_SIZE = 10
_PUBMED_RETMAX = 10
#: An abstract is clipped to this many characters: tool results are re-sent with the
#: stored conversation on every later step.
_ABSTRACT_MAX_CHARS = 800

_NO_PAPERS = "PubMed returned no papers for this query."
_NO_STUDIES = "ClinicalTrials.gov returned no studies for this query."
_TRIALS_FAILED = "ClinicalTrials.gov could not be searched just now."


class PubMedUnavailable(Exception):
    """The PubMed search itself failed (transport or parse); the caller decides whether to
    retry later or tell the model the search could not run."""


@dataclass(frozen=True)
class ToolResult:
    """What a research tool returns: the text the model sees, and one
    ``{"key", "kind", "title"}`` dict per source in it (``kind`` is ``pubmed``,
    ``trial`` or ``patent``, the tool that returned it)."""

    text: str
    sources: tuple[dict, ...] = ()


def _source(raw: object, kind: str, title: object) -> dict | None:
    key = source_key(str(raw or "").strip(), kind)
    if key is None:
        return None
    return {"key": key, "kind": kind, "title": str(title or "")}


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


async def search_pubmed(query: str) -> ToolResult:
    """Up to 10 PubMed papers for ``query``, by relevance, one ``pubmed_record`` block
    each. Raises ``PubMedUnavailable`` when the search itself fails; a failed record fetch
    is logged by ``fetch_pubmed_records`` and those papers are simply missing."""
    try:
        pmids = await search_pmids(query, retmax=_PUBMED_RETMAX, sort="relevance")
    except Exception as exc:
        raise PubMedUnavailable(f"PubMed search failed: {type(exc).__name__}") from exc
    records = await fetch_pubmed_records(pmids) if pmids else []
    if not records:
        return ToolResult(_NO_PAPERS)
    blocks: list[str] = []
    sources: list[dict] = []
    for rec in records:
        abstract = _clip(str(rec.get("abstract") or "No abstract available."), _ABSTRACT_MAX_CHARS)
        blocks.append(delimit(
            f"PMID {rec.get('pmid', '')}\nYear: {rec.get('year') or '?'}\n"
            f"Title: {rec.get('title', '')}\nAbstract: {abstract}",
            "pubmed_record",
        ))
        src = _source(rec.get("pmid"), "pubmed", rec.get("title"))
        if src is not None:
            sources.append(src)
    return ToolResult("\n\n".join(blocks), tuple(sources))


def _study_block(study: Mapping[str, Any]) -> tuple[str, str, str]:
    """``(nct_id, title, block text)`` of one v2 study."""
    proto = study.get("protocolSection") or {}
    ident = proto.get("identificationModule") or {}
    status = proto.get("statusModule") or {}
    conditions = (proto.get("conditionsModule") or {}).get("conditions") or []
    phases = (proto.get("designModule") or {}).get("phases") or []
    interventions = (proto.get("armsInterventionsModule") or {}).get("interventions") or []
    nct_id = str(ident.get("nctId") or "")
    title = str(ident.get("briefTitle") or "")
    lines = [
        f"NCT number: {nct_id}",
        f"Title: {title}",
        f"Phases: {', '.join(str(p) for p in phases) or 'not given'}",
        f"Overall status: {status.get('overallStatus') or 'not given'}",
        f"Conditions: {'; '.join(str(c) for c in conditions) or 'not given'}",
        "Interventions: " + ("; ".join(
            f"{i.get('type') or '?'}: {i.get('name') or '?'}"
            for i in interventions if isinstance(i, Mapping)
        ) or "not given"),
    ]
    return nct_id, title, "\n".join(lines)


async def search_trials(query: str) -> ToolResult:
    """Up to 10 ClinicalTrials.gov studies for ``query``, one ``trial_record`` block each.
    Never raises: any HTTP error, non-200 answer or unreadable body is reported to the
    model as "could not be searched", with no sources, so it is never read as "no
    trials"."""
    params = {"query.term": query, "fields": TRIALS_FIELDS, "pageSize": _TRIALS_PAGE_SIZE}
    try:
        await _TRIALS_PACER.wait()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(CTGOV_STUDIES_URL, params=params)
        if resp.status_code != 200:
            logger.warning("ClinicalTrials.gov search answered %s", resp.status_code)
            return ToolResult(_TRIALS_FAILED)
        studies = resp.json().get("studies") or []
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        logger.warning("ClinicalTrials.gov search failed: %s", type(exc).__name__)
        return ToolResult(_TRIALS_FAILED)
    blocks: list[str] = []
    sources: list[dict] = []
    for study in studies[:_TRIALS_PAGE_SIZE]:
        if not isinstance(study, Mapping):
            continue
        nct_id, title, block = _study_block(study)
        blocks.append(delimit(block, "trial_record"))
        src = _source(nct_id, "trial", title)
        if src is not None:
            sources.append(src)
    if not blocks:
        return ToolResult(_NO_STUDIES)
    return ToolResult("\n\n".join(blocks), tuple(sources))


async def search_prior_art_text(query: str) -> ToolResult:
    """BlackbirdBot's patent search with its own formatting, so "could not run" stays
    distinct from "no matches"; one ``patent`` source per filing listed."""
    from src.agent.tools import format_prior_art  # tools imports this module

    result = await search_prior_art(query)
    sources: list[dict] = []
    if result is not None:
        for hit in result.hits:
            src = _source(hit.get("patent_id"), "patent", hit.get("title"))
            if src is not None:
                sources.append(src)
    return ToolResult(format_prior_art(result), tuple(sources))


async def retrieve_abstract_text(pmid_or_doi: str) -> ToolResult:
    """One paper's title, journal, year and abstract, fenced as ``paper_abstract``. A
    lookup error is returned as its text with no sources; a paper found without a PMID
    (an OpenAlex answer for a DOI) is shown but records no source."""
    result = await fetch_abstract(pmid_or_doi)
    if "error" in result:
        return ToolResult(str(result["error"]))
    text = delimit(
        f"Title: {result.get('title', '')}\n"
        f"Journal: {result.get('journal') or 'Unknown'} ({result.get('year') or '?'})\n"
        f"PMID: {result.get('pmid') or 'none'}\n"
        f"Abstract: {result.get('abstract') or 'No abstract available.'}",
        "paper_abstract",
    )
    src = _source(result.get("pmid"), "pubmed", result.get("title"))
    return ToolResult(text, (src,) if src is not None else ())


def research_tool_definitions() -> list[dict]:
    """The research sessions' four tools, in order: ``search_pubmed``, ``search_trials``,
    then the ``search_prior_art`` and ``retrieve_abstract`` entries of
    ``TOOL_DEFINITIONS``, the same dicts, unchanged."""
    by_name = {d["name"]: d for d in TOOL_DEFINITIONS}
    return [*PROJECT_TOOL_DEFINITIONS, by_name["search_prior_art"], by_name["retrieve_abstract"]]


async def execute_research_tool(name: str, tool_input: Mapping[str, object]) -> ToolResult:
    """Run one research tool call. An unknown tool or a missing argument comes back as a
    one-line error text with no sources; the only exception raised is
    ``PubMedUnavailable``."""
    arg_name = "pmid_or_doi" if name == "retrieve_abstract" else "query"
    handlers = {
        "search_pubmed": search_pubmed,
        "search_trials": search_trials,
        "search_prior_art": search_prior_art_text,
        "retrieve_abstract": retrieve_abstract_text,
    }
    handler = handlers.get(name)
    if handler is None:
        return ToolResult(f"Error: unknown tool {name!r}.")
    value = tool_input.get(arg_name) if isinstance(tool_input, Mapping) else None
    if not isinstance(value, str) or not value.strip():
        return ToolResult(f"Error: {name} needs a non-empty {arg_name!r}.")
    return await handler(value.strip())
