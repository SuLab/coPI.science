"""Tool definitions and execution for Anthropic tool-use API (Phase 4 thread replies)."""

import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from src.agent.agent import _extract_dois
from src.agent.prompt_safety import delimit
from src.agent.roles import load_role
from src.agent.specialists import (
    SPECIALIST_DOMAINS,
    has_usable_content,
    parse_opinion,
    persona_path,
)
from src.services.llm import generate_agent_response
from src.services.patents import PriorArtResult, search_prior_art
from src.services.pubmed import fetch_abstract, fetch_full_text

logger = logging.getLogger(__name__)

PROFILES_DIR = Path("profiles")

# An agent_id as the registry mints them: lowercase last name, optionally with a
# first-initial prefix on a collision (`wu` / `pwu`). No separators, no dots — so
# nothing that could walk out of the directory it is joined onto. Used to validate
# the model-supplied argument to retrieve_profile.
_SAFE_AGENT_ID = re.compile(r"[A-Za-z0-9_-]+")

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
    {
        "name": "consult_specialist",
        "description": (
            "Ask one member of the Blackbird evaluation panel for an opinion in "
            "their domain. Use this DURING an interview, as soon as the PI says "
            "something that falls in a specialist's area — their questions_to_ask "
            "become your next question to the PI, which is worth far more than "
            "asking them after you have already formed a view.\n\n"
            "Domains: 'scientific' (rigor, controls, power, interpretability, "
            "mouse-to-human translatability), 'chemistry' (path to a development "
            "candidate, medchem tractability, tolerability, in-family off-targets), "
            "'clinical' (unmet need vs standard of care, indication, patient "
            "numbers), 'commercial' (competitive landscape, named competing "
            "programs, deal comps), 'legal' (FTO, licensing, research-tool "
            "encumbrance), 'technologic' (platform feasibility, whether the work "
            "would test it), 'talent' (execution probability, conflicts of "
            "interest, over-commitment), 'budget' (scope against Blackbird's grant "
            "bands and 12-24 month durations).\n\n"
            "An advance or conditional verdict is REFUSED if the domains the idea "
            "touches were never consulted, and you cannot consult during the "
            "assessment turn — only here, in the interview. Consult early."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "enum": [
                        "scientific", "chemistry", "clinical", "commercial",
                        "legal", "technologic", "talent", "budget",
                    ],
                    "description": "Which specialist to ask.",
                },
                "question": {
                    "type": "string",
                    "description": (
                        "The specific question, in your own words. Not 'what do you "
                        "think' — name the claim you want tested."
                    ),
                },
                "context": {
                    "type": "string",
                    "description": (
                        "The relevant part of the interview so far: what the PI "
                        "actually said about this. Quote them where you can."
                    ),
                },
            },
            "required": ["domain", "question", "context"],
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
    *,
    on_consult: Callable[[str, str], None] | None = None,
    on_api_call: Callable[[], None] | None = None,
    own_dois: set[str] | None = None,
) -> str:
    """
    Execute a tool call and return the result as a string.

    Enforces per-thread rate limits for retrieve_abstract (other lab) and
    retrieve_full_text. Refuses (without raising) any tool not allowed for
    ``role``.

    ``on_consult`` is forwarded to ``consult_specialist`` and is called with
    the domain AND the parsed verdict signal, and fires only on a fully
    successful consult — see ``_execute_consult_specialist``.

    ``own_dois``: the calling agent's own-lab publication DOIs (see
    ``Agent.own_publication_dois``, GitHub issue #7). A ``retrieve_abstract``
    lookup whose ``pmid_or_doi`` contains one of these DOIs is exempt from
    BOTH the per-thread cap check and its increment — citing your own paper
    isn't "using up" the budget meant to limit how much of another lab's work
    you pull in. Only recognizes DOI form: a bare PMID has no DOI substring to
    match, so it always counts against the cap (documented limit, design §10).
    """
    if tool_name not in load_role(role).tools:
        logger.warning("[tools] %s: role %r may not call %s", agent_id, role, tool_name)
        return f"Tool '{tool_name}' is not available to this agent."
    try:
        if tool_name == "retrieve_profile":
            return await _execute_retrieve_profile(tool_input["agent_id"])

        elif tool_name == "retrieve_abstract":
            ref = str(tool_input.get("pmid_or_doi", ""))
            is_own = bool(own_dois) and bool(_extract_dois(ref) & own_dois)
            if thread_state and not is_own:
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

        elif tool_name == "search_prior_art":
            return await _execute_search_prior_art(tool_input["query"])

        elif tool_name == "consult_specialist":
            return await _execute_consult_specialist(
                tool_input["domain"],
                tool_input["question"],
                tool_input["context"],
                agent_id=agent_id,
                on_consult=on_consult,
                on_api_call=on_api_call,
            )

        else:
            return f"Unknown tool: {tool_name}"

    except Exception as exc:
        logger.error("Tool execution failed: %s(%s) — %s", tool_name, tool_input, exc)
        return f"Error executing {tool_name}: {exc}"


async def _execute_retrieve_profile(agent_id: str) -> str:
    """Read a public profile from disk.

    ``agent_id`` comes straight from the model, so it is validated before it
    reaches the filesystem. Unvalidated, it escaped ``profiles/public/``
    entirely: ``../private/blackbird`` returned the hub's private screening
    rubric and ``../memory/<id>/public`` returned another lab's working memory
    — a confidentiality hole one tool call wide, in a topology whose whole
    point is that spokes cannot read each other. Real agent ids are lowercase
    identifiers (``wu``, ``pwu``, ``hamacherbrady``), so anything with a
    separator or a dot is not a lookup, it is an escape attempt.
    """
    if not isinstance(agent_id, str) or not _SAFE_AGENT_ID.fullmatch(agent_id):
        logger.warning(
            "[tools] retrieve_profile refused a malformed agent_id: %r", agent_id
        )
        return f"No public profile found for agent '{agent_id}'."

    base = (PROFILES_DIR / "public").resolve()
    profile_path = (base / f"{agent_id}.md").resolve()
    # Belt and braces: the pattern above already excludes separators, but a
    # containment check keeps this safe if that pattern is ever loosened.
    if not profile_path.is_relative_to(base):
        logger.warning(
            "[tools] retrieve_profile refused an out-of-tree path for %r", agent_id
        )
        return f"No public profile found for agent '{agent_id}'."

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


# Shared by _PATENT_UNAVAILABLE and _PATENT_NO_QUERY below: both describe a case where
# no title search of USPTO actually happened (a tool outage vs. a caller-side empty
# query), and a model must never read either one as a negative result. The two stay
# separate messages on purpose — "could not search" and "never asked" are different
# situations a model must be able to tell apart — but they share this exact clause so a
# future edit to one warning cannot accidentally leave the other weaker.
_PATENT_NOT_EVIDENCE_WARNING = (
    "This is NOT a prior-art result: do NOT treat the absence of results as evidence "
    "of novelty or freedom-to-operate."
)

_PATENT_UNAVAILABLE = (
    "Prior-art search is UNAVAILABLE right now (the patent endpoint could not be "
    "reached, errored, or no API key is configured). " + _PATENT_NOT_EVIDENCE_WARNING +
    " Note the search could not be run at all."
)

# Distinct from _PATENT_UNAVAILABLE: this is a caller-side bad query (empty, or
# punctuation-only so _Q_SANITISE strips it to nothing), not a tool outage. No HTTP
# call was ever made, so this must never read like a negative title search.
_PATENT_NO_QUERY = (
    "No search was performed: the query had no usable terms once punctuation was "
    "removed (it was empty or punctuation-only); nothing was sent to USPTO. " +
    _PATENT_NOT_EVIDENCE_WARNING + " Retry with 2-4 specific terms."
)


def _scope_note(result: "PriorArtResult") -> str:
    """Tell the model what breadth actually produced this answer. A broadened
    search must never be read as an on-point clean result.

    Precondition: ``result.terms_used`` is non-empty. The only caller,
    ``_execute_search_prior_art``, already short-circuits an empty ``terms_used`` to
    ``_PATENT_NO_QUERY`` before this is ever called, so that case is asserted here
    rather than handled — a silent `return ""` would mask a real bug (a caveat posted
    with no scope disclosure) instead of failing loudly.
    """
    assert result.terms_used, "caller must filter empty terms_used before calling _scope_note"
    terms = " AND ".join(result.terms_used)
    if result.broadened:
        n = len(result.terms_used)
        narrowed = (
            f"the single most specific of your {result.total_terms} terms ({terms})"
            if n == 1
            else f"the {n} most specific of your {result.total_terms} terms ({terms})"
        )
        return (
            f"SCOPE: your full phrase matched no title, so this searched {narrowed}. "
            f"That is a BROADER search than you asked for — any hits "
            f"may be adjacent rather than on point, and an empty result at this "
            f"breadth is the strongest negative this tool can give you (still not FTO).\n\n"
        )
    return f"SCOPE: searched titles for {terms}.\n\n"


async def _execute_search_prior_art(query: str) -> str:
    """Search the USPTO ODP for prior art.

    Distinguishes four outcomes so the hub never mistakes an unreachable/unconfigured
    tool, or a caller-side empty query, for a clean novelty result:
      * ``None``          → the search could not run → an explicit UNAVAILABLE notice;
      * no usable terms   → nothing was ever sent to USPTO → an explicit NO-QUERY notice;
      * empty ``hits``    → the search ran and matched nothing → caveat + scope + "no matches";
      * results           → caveat + scope + the filings.
    """
    result = await search_prior_art(query)
    if result is None:
        return _PATENT_UNAVAILABLE
    if not result.terms_used:
        return _PATENT_NO_QUERY
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


async def _execute_consult_specialist(
    domain: str,
    question: str,
    context: str,
    *,
    agent_id: str,
    on_consult: Callable[[str, str], None] | None = None,
    on_api_call: Callable[[], None] | None = None,
) -> str:
    """Ask one specialist persona for an opinion.

    ``on_consult`` is invoked with the domain and the parsed verdict signal
    ONLY on a successful call. A refused domain, a missing persona file, or a
    failed LLM call must not satisfy the enforcement floor — otherwise "the
    specialist was unreachable" would silently become "the specialist
    approved".

    ``on_api_call`` is a DIFFERENT question — "was an Opus call billed?", not
    "does this count as consulted?" — so it fires on a different schedule: once
    per call actually issued, before it is issued, whatever the outcome. A
    consult is a real Opus call; booking it is what keeps the hub visible to the
    sliding-window limiter and to ``SimulationRun.total_api_calls`` (see
    ``Agent.record_api_call``'s invariant). Callers pass
    ``agent.record_api_call``. The two callbacks deliberately disagree on a
    consult that was billed but did not parse.
    """
    spec = SPECIALIST_DOMAINS.get(domain)
    if spec is None:
        return (
            f"Unknown specialist domain {domain!r}. Valid domains: "
            + ", ".join(sorted(SPECIALIST_DOMAINS))
        )

    path = persona_path(domain)
    if not path.is_file():
        logger.error("[specialists] persona file missing for %s: %s", domain, path)
        return (
            f"The {domain} specialist is unavailable (persona file missing). "
            "Proceed without this opinion; it will not count as consulted."
        )

    persona = path.read_text(encoding="utf-8")
    # Book the call before issuing it, the same way _reply_to_thread books its
    # own: a call that is made and then fails is still billed, so charging only
    # on success would let a flapping specialist run free.
    if on_api_call is not None:
        on_api_call()
    # Local import, matching the two other get_settings uses in this module.
    from src.config import get_settings

    try:
        raw = await generate_agent_response(
            system_prompt=persona,
            messages=[{
                "role": "user",
                "content": f"## Question from the hub\n\n{question}\n\n"
                           f"## What the PI has said\n\n{context}",
            }],
            # PINNED, not inherited. This call used to pass no `model` at all,
            # so it silently fell through to `llm_agent_model` — the Sonnet
            # setting — while comments elsewhere described a consult as "a real
            # Opus call". Verified against production llm_call_logs: every
            # consult in run 2026-08-19 13:35 ran on claude-sonnet-4-6. A
            # specialist opinion gates whether a verdict may be recorded, so the
            # model behind it is a deliberate choice and belongs at the call
            # site where it can be seen.
            model=get_settings().llm_agent_model_opus,
            # 2500, measured — not guessed. History: 900 (Sonnet 4.6 era, already
            # truncating), then 1500 for the Opus 5 migration on a ~30% tokenizer
            # estimate, which turned out to be BELOW the observed minimum.
            # Opus 5 writes ~2.2x longer specialist opinions than Sonnet 4.6 did:
            # measured over run 2026-08-19 14:45, consults returned 1687-1731
            # output tokens (avg 1697) against a Sonnet 4.6 average of 789.
            # At 1500 essentially EVERY consult truncated and retried, doubling
            # the cost of the most numerous call in the system for no benefit.
            # 2500 clears the observed ceiling with headroom, and max_tokens is a
            # ceiling rather than a spend — a short opinion still bills short.
            max_tokens=2500,
            log_meta={"agent_id": agent_id, "phase": f"consult_{domain}"},
            # A truncation retry is a second billed call — book it too, same
            # contract every other generate_agent_response caller uses.
            on_retry=on_api_call,
        )
    except Exception as exc:  # noqa: BLE001 — a dead specialist must not kill the turn
        logger.error("[specialists] %s consult failed: %s", domain, exc)
        return f"Error consulting the {domain} specialist: {exc}"

    # A billed call that came back empty is not an opinion. `on_api_call`
    # already fired above (it answers "was this billed?"); `on_consult` answers
    # "does this satisfy the floor?" and the two must disagree here. Checked
    # BEFORE parsing: this branch returns a fixed string and reads nothing off
    # the opinion, so parsing first was work whose only possible consumer was
    # the branch that never runs.
    if not has_usable_content(raw):
        logger.error(
            "[specialists] %s returned no usable content — NOT counted as "
            "consulted", domain,
        )
        return (
            f"The {domain} specialist returned an empty response. Proceed "
            "without this opinion; it will not count as consulted."
        )
    opinion = parse_opinion(raw, domain=domain)
    if on_consult is not None:
        on_consult(domain, opinion.verdict_signal)
    logger.info(
        "[specialists] %s consulted %s -> %s (%s)",
        agent_id, domain, opinion.verdict_signal, opinion.confidence,
    )
    return f"{spec.title} — signal: {opinion.verdict_signal}\n\n{opinion.raw}"
