"""Tool definitions and execution for Anthropic tool-use API (Phase 4 thread replies)."""

import logging
from pathlib import Path
from typing import Any

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
]


async def execute_tool(
    tool_name: str,
    tool_input: dict[str, Any],
    agent_id: str,
    thread_state: Any | None = None,
) -> str:
    """
    Execute a tool call and return the result as a string.

    Enforces per-thread rate limits for retrieve_abstract (other lab) and
    retrieve_full_text.
    """
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

        else:
            return f"Unknown tool: {tool_name}"

    except Exception as exc:
        logger.error("Tool execution failed: %s(%s) — %s", tool_name, tool_input, exc)
        return f"Error executing {tool_name}: {exc}"


async def _execute_retrieve_foa(foa_number: str) -> str:
    """Fetch full details of a funding opportunity from Grants.gov."""
    from src.services.grants import fetch_opportunity_by_number

    result = await fetch_opportunity_by_number(foa_number)
    if not result:
        return f"No funding opportunity found for '{foa_number}'."

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
        return profile_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"No public profile found for agent '{agent_id}'."


async def _execute_retrieve_abstract(pmid_or_doi: str) -> str:
    """Fetch and format a paper abstract."""
    result = await fetch_abstract(pmid_or_doi)
    if "error" in result:
        return result["error"]
    parts = [
        f"Title: {result['title']}",
        f"Journal: {result.get('journal', 'Unknown')} ({result.get('year', '?')})",
        f"PMID: {result['pmid']}",
        "",
        f"Abstract: {result.get('abstract', 'No abstract available.')}",
    ]
    return "\n".join(parts)


async def _execute_retrieve_full_text(pmid_or_doi: str) -> str:
    """Fetch and format paper full text (methods)."""
    result = await fetch_full_text(pmid_or_doi)
    if "error" in result:
        return result["error"]
    parts = [
        f"Title: {result['title']}",
        f"Journal: {result.get('journal', 'Unknown')} ({result.get('year', '?')})",
        f"PMID: {result['pmid']}",
    ]
    if result.get("pmcid"):
        parts.append(f"PMCID: {result['pmcid']}")
    parts.append("")
    parts.append(f"Abstract: {result.get('abstract', 'No abstract available.')}")
    if result.get("methods"):
        parts.append("")
        parts.append(f"Methods: {result['methods'][:3000]}")
    elif result.get("note"):
        parts.append("")
        parts.append(f"Note: {result['note']}")
    return "\n".join(parts)
