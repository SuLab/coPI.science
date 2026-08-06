"""Tool definitions and execution for Anthropic tool-use API (Phase 4 thread replies)."""

import logging
from pathlib import Path
from typing import Any

from src.agent.prompt_safety import delimit
from src.agent.roles import load_role
from src.services.patents import PriorArtResult, search_prior_art
from src.services.pubmed import fetch_abstract, fetch_full_text

logger = logging.getLogger(__name__)

PROFILES_DIR = Path("profiles")

# Anthropic tool-use schema definitions
TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "retrieve_profile",
        "description": (
            "Retrieve the public profile of another lab's agent. "
            "Returns their research focus, techniques, recent publications, "
            "and other publicly available information."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "The agent ID to look up (e.g., 'wiseman', 'su', 'cravatt')",
                }
            },
            "required": ["agent_id"],
        },
    },
    {
        "name": "retrieve_abstract",
        "description": (
            "Fetch a paper's abstract from PubMed. Accepts a PMID (e.g., '12345678') "
            "or DOI (e.g., '10.1234/journal.2024'). Returns title, abstract, journal, year."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pmid_or_doi": {
                    "type": "string",
                    "description": "PubMed ID or DOI of the paper",
                }
            },
            "required": ["pmid_or_doi"],
        },
    },
    {
        "name": "retrieve_full_text",
        "description": (
            "Fetch full text (methods section) from PubMed Central. Use sparingly — "
            "only when the abstract is insufficient and the paper is central to a "
            "potential collaboration. Up to 2 uses per thread."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pmid_or_doi": {
                    "type": "string",
                    "description": "PubMed ID or DOI of the paper",
                }
            },
            "required": ["pmid_or_doi"],
        },
    },
    {
        "name": "retrieve_foa",
        "description": (
            "Fetch the full details of a federal funding opportunity from Grants.gov. "
            "Accepts an FOA number (e.g., 'RFA-AI-27-019', 'PAR-24-293'). "
            "Returns the title, agency, description, synopsis, eligibility, dates, "
            "and award amounts. Use this to read the full FOA before engaging in "
            "any funding-related discussion."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "foa_number": {
                    "type": "string",
                    "description": "The FOA number (e.g., 'RFA-AI-27-019')",
                }
            },
            "required": ["foa_number"],
        },
    },
    {
        "name": "search_prior_art",
        "description": (
            "Search issued and published US patent filings (USPTO Open Data Portal) "
            "for prior art. Matches on INVENTION TITLE ONLY. Pass 2-4 highly specific "
            "terms — gene/target symbols, a compound name, a modality — NOT a sentence. "
            "A long descriptive query cannot match any real patent title and will come "
            "back empty no matter how crowded the field is. Good: 'TFEB melanoma'. "
            "Bad: 'TFEB inhibitor nuclear translocation melanoma BRAF resistance'. "
            "US filings only — absence of a hit is NOT proof of novelty or FTO."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "2-4 specific terms matched against the patent title (e.g. "
                        "'C9orf72 repeat', 'TFEB melanoma'). Not a description."
                    ),
                },
            },
            "required": ["query"],
        },
    },
]

_PATENT_CAVEAT = (
    "Source: USPTO Open Data Portal (api.uspto.gov), US filings only, matched on "
    "INVENTION TITLE ONLY — abstracts and claims are NOT searched. Absence of a hit "
    "is weak evidence at best: it does not cover EP/WO/JP filings, unpublished "
    "applications, non-patent prior art, or any patent whose title happens to use "
    "different words. NEVER report a clean title search as novelty or as "
    "freedom-to-operate; report it as what it is, a title search that found nothing.\n\n"
)


def tools_for_role(role: str) -> list[dict[str, Any]]:
    """``TOOL_DEFINITIONS`` filtered down to what ``role`` is allowed to call."""
    allowed = load_role(role).tools
    return [t for t in TOOL_DEFINITIONS if t["name"] in allowed]


async def execute_tool(
    tool_name: str,
    tool_input: dict[str, Any],
    agent_id: str,
    thread_state: Any | None = None,
    role: str = "pi_lab",
) -> str:
    """
    Execute a tool call and return the result as a string.

    Enforces per-thread rate limits for retrieve_abstract (other lab) and
    retrieve_full_text. Refuses (without raising) any tool not allowed for
    ``role``.
    """
    if tool_name not in load_role(role).tools:
        logger.warning("[tools] %s: role %r may not call %s", agent_id, role, tool_name)
        return f"Tool '{tool_name}' is not available to this agent."
    try:
        if tool_name == "retrieve_profile":
            return await _execute_retrieve_profile(tool_input["agent_id"])

        elif tool_name == "retrieve_abstract":
            if thread_state:
                # Check if this is the agent's own paper (no limit) vs other lab
                # We don't enforce limits on own-lab lookups, but we track other-lab ones
                from src.config import get_settings
                settings = get_settings()
                if thread_state.abstracts_other >= settings.max_abstracts_other_per_thread:
                    return "Rate limit: you have used all your abstract retrievals for other labs in this thread."
                thread_state.abstracts_other += 1
            return await _execute_retrieve_abstract(tool_input["pmid_or_doi"])

        elif tool_name == "retrieve_full_text":
            if thread_state:
                from src.config import get_settings
                settings = get_settings()
                if thread_state.full_text >= settings.max_full_text_per_thread:
                    return "Rate limit: you have used all your full-text retrievals in this thread."
                thread_state.full_text += 1
            return await _execute_retrieve_full_text(tool_input["pmid_or_doi"])

        elif tool_name == "retrieve_foa":
            return await _execute_retrieve_foa(tool_input["foa_number"])

        elif tool_name == "search_prior_art":
            return await _execute_search_prior_art(tool_input["query"])

        else:
            return f"Unknown tool: {tool_name}"

    except Exception as exc:
        logger.error("Tool execution failed: %s(%s) — %s", tool_name, tool_input, exc)
        return f"Error executing {tool_name}: {exc}"


async def _execute_retrieve_foa(foa_number: str) -> str:
    """Fetch full details of a funding opportunity — checks local cache first."""
    from src.agent.foa_cache import format_foa_for_prompt

    cached = format_foa_for_prompt(foa_number)
    if cached:
        return cached

    # Fall back to Grants.gov API
    from src.services.grants import fetch_opportunity_by_number

    result = await fetch_opportunity_by_number(foa_number)
    if not result:
        return f"No funding opportunity found for '{foa_number}'."

    # Cache for future use
    from src.agent.foa_cache import cache_foa
    cache_foa(foa_number, result)

    parts = [
        f"Title: {result.get('title', 'Unknown')}",
        f"Number: {result.get('number', foa_number)}",
        f"Agency: {result.get('agency', 'Unknown')}",
        f"Open Date: {result.get('open_date', 'Not specified')}",
        f"Close Date: {result.get('close_date', 'Not specified')}",
    ]
    if result.get("award_ceiling") or result.get("award_floor"):
        parts.append(f"Award Range: ${result.get('award_floor', '?')} – ${result.get('award_ceiling', '?')}")
    if result.get("eligibility"):
        parts.append(f"Eligibility: {result['eligibility']}")
    if result.get("category"):
        parts.append(f"Category: {result['category']}")
    parts.append("")
    if result.get("description"):
        parts.append(f"Description:\n{result['description']}")
    if result.get("synopsis"):
        parts.append(f"\nSynopsis:\n{result['synopsis']}")
    if result.get("additional_info_url"):
        parts.append(f"\nMore info: {result['additional_info_url']}")
    return "\n".join(parts)


async def _execute_retrieve_profile(agent_id: str) -> str:
    """Read a public profile from disk."""
    profile_path = PROFILES_DIR / "public" / f"{agent_id}.md"
    try:
        # Profiles are user-editable text — fence as untrusted data (SEC-14).
        return delimit(profile_path.read_text(encoding="utf-8"), "agent_profile")
    except FileNotFoundError:
        return f"No public profile found for agent '{agent_id}'."


async def _execute_retrieve_abstract(pmid_or_doi: str) -> str:
    """Fetch and format a paper abstract."""
    result = await fetch_abstract(pmid_or_doi)
    if "error" in result:
        return result["error"]
    # Title/abstract come from PubMed — untrusted external text (SEC-14).
    parts = [
        f"Title: {delimit(result['title'], 'paper_title')}",
        f"Journal: {result.get('journal', 'Unknown')} ({result.get('year', '?')})",
        f"PMID: {result['pmid']}",
        "",
        f"Abstract: {delimit(result.get('abstract', 'No abstract available.'), 'paper_abstract')}",
    ]
    return "\n".join(parts)


async def _execute_retrieve_full_text(pmid_or_doi: str) -> str:
    """Fetch and format paper full text (methods)."""
    result = await fetch_full_text(pmid_or_doi)
    if "error" in result:
        return result["error"]
    # Title/abstract/methods come from PubMed/PMC — untrusted external text (SEC-14).
    parts = [
        f"Title: {delimit(result['title'], 'paper_title')}",
        f"Journal: {result.get('journal', 'Unknown')} ({result.get('year', '?')})",
        f"PMID: {result['pmid']}",
    ]
    if result.get("pmcid"):
        parts.append(f"PMCID: {result['pmcid']}")
    parts.append("")
    parts.append(f"Abstract: {delimit(result.get('abstract', 'No abstract available.'), 'paper_abstract')}")
    if result.get("methods"):
        parts.append("")
        parts.append(f"Methods: {delimit(result['methods'][:3000], 'paper_methods')}")
    elif result.get("note"):
        parts.append("")
        parts.append(f"Note: {result['note']}")
    return "\n".join(parts)


_PATENT_UNAVAILABLE = (
    "Prior-art search is UNAVAILABLE right now (the patent endpoint could not be "
    "reached, errored, or no API key is configured). This is NOT a prior-art result: "
    "do NOT treat the absence of results as evidence of novelty or freedom-to-operate. "
    "Note the search could not be run at all."
)


def _scope_note(result: "PriorArtResult") -> str:
    """Tell the model what breadth actually produced this answer. A broadened
    search must never be read as an on-point clean result."""
    if not result.terms_used:
        return ""
    terms = " AND ".join(result.terms_used)
    if result.broadened:
        return (
            f"SCOPE: your full phrase matched no title, so this searched the "
            f"{len(result.terms_used)} most specific of your {result.total_terms} "
            f"terms ({terms}). That is a BROADER search than you asked for — any hits "
            f"may be adjacent rather than on point, and an empty result at this "
            f"breadth is the strongest negative this tool can give you (still not FTO).\n\n"
        )
    return f"SCOPE: searched titles for {terms}.\n\n"


async def _execute_search_prior_art(query: str) -> str:
    """Search the USPTO ODP for prior art.

    Distinguishes three outcomes so the hub never mistakes an unreachable/unconfigured
    tool for a clean novelty result:
      * ``None``        → the search could not run → an explicit UNAVAILABLE notice;
      * empty ``hits``  → the search ran and matched nothing → caveat + scope + "no matches";
      * results         → caveat + scope + the filings.
    """
    result = await search_prior_art(query)
    if result is None:
        return _PATENT_UNAVAILABLE
    preamble = _PATENT_CAVEAT + _scope_note(result)
    if not result.hits:
        return preamble + "No US filings matched this query."
    lines = [preamble]
    for h in result.hits:
        applicant = h.get("applicant") or "Unknown applicant"
        inventor = h.get("inventor") or "Unknown inventor"
        status = h.get("status") or ""
        block = (
            f"{h.get('patent_id','')} ({h.get('date','')}) — {h.get('title','')}\n"
            f"  applicant: {applicant} | inventor: {inventor}"
            + (f" | status: {status}" if status else "")
        )
        if h.get("abstract"):
            block += f"\n  abstract: {h['abstract']}"
        if h.get("claim"):
            block += f"\n  claim 1: {h['claim']}"
        # Title/applicant/abstract/claim come from the USPTO API — untrusted
        # external text (SEC-14); fence it.
        lines.append(delimit(block, "patent"))
    return "\n\n".join(lines)
