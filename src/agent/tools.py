"""Tool definitions and execution for Anthropic tool-use API (Phase 4 thread replies)."""

import logging
import re
from collections.abc import Awaitable, Callable
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
    read_state_for,
)
from src.services.llm import generate_agent_response, is_truncated_stop
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
            "Your terms are ANDed for you: do NOT write AND/OR/NOT, which are "
            "query syntax and are dropped rather than searched. "
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


# How much of the interview excerpt a consult record keeps. The context the hub
# passes is a quotation from the thread, so it is bounded in practice, but it is
# model-generated text on a Text column that is read back by a page — 2000
# characters is enough to show what the specialist was actually asked about
# without turning the consult table into a second copy of the transcript
# (`llm_call_logs` already keeps the full prompt verbatim).
_CONSULT_CONTEXT_EXCERPT_CHARS = 2000


def _require_arg(tool_input: dict[str, Any], name: str, tool_name: str) -> str:
    """Read a required tool argument, or raise with a message the MODEL can act on.

    Reading `tool_input["name"]` directly raises a bare KeyError, which the
    dispatcher below turns into `Error executing consult_specialist: 'context'`.
    That tells the model nothing — not which parameter, not that it was omitted,
    not what to do — so it cannot correct the call, and the tool is simply lost.

    Observed in production on 2026-08-19 15:12: the hub called
    consult_specialist with `domain` and `question` but no `context`, and the
    chemistry opinion was dropped. Zero occurrences across the four preceding
    runs on Sonnet 4.6, one within ten minutes of moving to Opus 5 — consistent
    with the documented change in how the 5-series models emit tool inputs.
    A schema marking a field `required` constrains the model, it does not
    guarantee the field.
    """
    value = tool_input.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(
            f"missing required argument {name!r}. Call {tool_name} again with "
            f"{name!r} supplied."
        )
    return str(value)


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
    on_consult_record: Callable[..., Awaitable[None]] | None = None,
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

    ``on_consult_record`` is the DURABLE half of the same event: an awaitable
    the engine supplies to write the consult to ``specialist_consults``,
    defaulting to None so every caller without one (a pi_lab reply, a direct
    test call) behaves exactly as before. It fires on a STRICTLY WIDER path than
    ``on_consult``: a consult the API cut off mid-reply is recorded and not
    counted, because a truncated opinion is the only evidence the attempt
    happened at all — and the record carries ``truncated=True`` so the row can
    say which it was. See ``_execute_consult_specialist``.

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
            return await _execute_retrieve_profile(
                _require_arg(tool_input, "agent_id", tool_name)
            )

        elif tool_name == "retrieve_abstract":
            ref = _require_arg(tool_input, "pmid_or_doi", tool_name)
            is_own = bool(own_dois) and bool(_extract_dois(ref) & own_dois)
            if thread_state and not is_own:
                from src.config import get_settings
                settings = get_settings()
                if thread_state.abstracts_other >= settings.max_abstracts_other_per_thread:
                    return "Rate limit: you have used all your abstract retrievals for other labs in this thread."
            result = await fetch_abstract(ref)
            if "error" in result:
                return result["error"]
            # Charge only a retrieval that returned a paper — the debit used
            # to land before the fetch, so an outage consumed the budget with
            # no refund (issue #23 COR-30). Safe against double-spend: tool
            # rounds are sequential within a turn, and the thread lock
            # serializes turns per thread.
            if thread_state and not is_own:
                thread_state.abstracts_other += 1
            return _format_abstract(result)

        elif tool_name == "retrieve_full_text":
            ref = _require_arg(tool_input, "pmid_or_doi", tool_name)
            if thread_state:
                from src.config import get_settings
                settings = get_settings()
                if thread_state.full_text >= settings.max_full_text_per_thread:
                    return "Rate limit: you have used all your full-text retrievals in this thread."
            result = await fetch_full_text(ref)
            if "error" in result:
                return result["error"]
            if thread_state:
                thread_state.full_text += 1
            return _format_full_text(result)

        elif tool_name == "search_prior_art":
            return await _execute_search_prior_art(
                _require_arg(tool_input, "query", tool_name)
            )

        elif tool_name == "consult_specialist":
            return await _execute_consult_specialist(
                _require_arg(tool_input, "domain", tool_name),
                _require_arg(tool_input, "question", tool_name),
                # `context` DEGRADES rather than failing: it is grounding, not
                # the ask. A specialist handed a question with no transcript
                # excerpt still gives a usable domain opinion; losing the
                # consult entirely costs the panel a domain and, on an
                # advance/conditional verdict, flags the whole assessment.
                # `domain` and `question` genuinely cannot be defaulted.
                tool_input.get("context") or "",
                agent_id=agent_id,
                # The interview's channel, for the consult's own llm_call_logs
                # row (which carried a NULL channel until now, so a consult
                # could not be joined to the discussion it came from) and for
                # the durable consult record. `getattr` because thread_state is
                # untyped here and legitimately None for a direct caller.
                channel=getattr(thread_state, "channel", None),
                on_consult=on_consult,
                on_consult_record=on_consult_record,
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


def _format_abstract(result: dict) -> str:
    """Format an already-fetched paper abstract."""
    # Title/abstract come from PubMed — untrusted external text (SEC-14).
    parts = [
        f"Title: {delimit(result['title'], 'paper_title')}",
        f"Journal: {result.get('journal', 'Unknown')} ({result.get('year', '?')})",
        f"PMID: {result['pmid']}",
        "",
        f"Abstract: {delimit(result.get('abstract', 'No abstract available.'), 'paper_abstract')}",
    ]
    return "\n".join(parts)


def _format_full_text(result: dict) -> str:
    """Format already-fetched paper full text (methods)."""
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
# punctuation-only so `patents._tokenise` yields nothing), not a tool outage. No HTTP
# call was ever made, so this must never read like a negative title search.
_PATENT_NO_QUERY = (
    "No search was performed: the query had no usable terms once punctuation was "
    "removed (it was empty or punctuation-only); nothing was sent to USPTO. " +
    _PATENT_NOT_EVIDENCE_WARNING + " Retry with 2-4 specific terms."
)


def _rewrite_note(result: "PriorArtResult") -> str:
    """Name the caller's own words that are not what was searched, or "".

    The model judges the hits against the phrase it THINKS it sent, so a silent
    rewrite is a disclosure gap rather than a cosmetic one: `Qbeta` results read
    as a clean answer about `Qβ`, and a dropped `NOT` turns a syntax accident
    into apparent novelty. See ``src/services/patents.py::_prepare``.
    """
    if not result.dropped_or_rewritten:
        return ""
    return (
        "REWRITTEN: the USPTO title index is ASCII-only and treats AND/OR/NOT as "
        "syntax, so your query text was changed before it was sent — "
        + "; ".join(result.dropped_or_rewritten)
        + ". Judge the filings below against the terms named above, not against "
        "your original wording."
    )


def _scope_note(result: "PriorArtResult") -> str:
    """Tell the model what breadth actually produced this answer, and how much of
    its own query survived. A broadened search must never be read as an on-point
    clean result.

    Precondition: ``result.terms_used`` is non-empty. The only caller,
    ``_execute_search_prior_art``, already short-circuits an empty ``terms_used`` to
    ``_PATENT_NO_QUERY`` before this is ever called, so that case is asserted here
    rather than handled — a silent `return ""` would mask a real bug (a caveat posted
    with no scope disclosure) instead of failing loudly.

    Assembled by joining the sentences that apply, rather than interpolating them:
    the previous form ran ``truncation_note`` straight onto a full stop and
    produced "...for TFEB AND melanoma.COMPLETENESS: showing..." with a trailing
    ``\\n\\n\\n\\n``, because that property already ended in ``\\n\\n``. It no
    longer does; spacing is decided here, once, for all three sentences.
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
        scope = (
            f"SCOPE: your full phrase matched no title, so this searched {narrowed}. "
            f"That is a BROADER search than you asked for — any hits "
            f"may be adjacent rather than on point, and an empty result at this "
            f"breadth is the strongest negative this tool can give you (still not FTO)."
        )
    else:
        scope = f"SCOPE: searched titles for {terms}."
    return (
        " ".join(
            part
            for part in (scope, result.truncation_note, _rewrite_note(result))
            if part
        )
        + "\n\n"
    )


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
    channel: str | None = None,
    on_consult: Callable[[str, str], None] | None = None,
    on_consult_record: Callable[..., Awaitable[None]] | None = None,
    on_api_call: Callable[[], None] | None = None,
) -> str:
    """Ask one specialist persona for an opinion.

    ``on_consult`` is invoked with the domain and the parsed verdict signal
    ONLY on a successful call. A refused domain, a missing persona file, a
    failed LLM call, an empty reply, or a reply the API stopped mid-sentence
    (``is_truncated_stop`` — ``refusal`` OR ``max_tokens``) must not satisfy the
    enforcement floor — otherwise "the specialist was unreachable" would
    silently become "the specialist approved".

    ``on_consult_record`` fires on a strictly WIDER path — every case above that
    produced text at all, truncated ones included — and is the durable record of
    the attempt (``specialist_consults``). It carries only what this function
    knows — the ask, the parsed opinion, and whether the reply was cut off
    (``truncated``); the engine's own closure supplies who asked, about whom,
    and in which thread and channel. Deliberately
    SECOND: the in-memory ``on_consult`` is what the floor reads in-process and
    stays authoritative there, so a write that fails must not un-count a
    consult that really happened. It is awaited rather than
    fired-and-forgotten (a bare task would outlive the turn and race the
    engine's shutdown flush), but it can neither raise into nor change the
    string this returns — see the guard at the call.

    ``channel`` is the interview's channel, used only for this call's own
    ``llm_call_logs`` metadata. None for a direct caller with no thread.

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
    # Filled by `on_stop_reason` below — a list rather than a scalar because the
    # callback fires once per generate_agent_response invocation and a truncation
    # retry makes two. Only the LAST one describes the text we were handed.
    stop_reasons: list[str] = []
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
            #
            # 4000, up from 2500, on 2026-08-21. `call_stats` (migration 0032)
            # makes per-call output measurable for the first time, and over run
            # 076e80b6 the largest consult returned 2299 output tokens — 92% of
            # 2500. The headroom above was real when it was measured and is not
            # any more: the specialists write longer under rubric v2, and this is
            # the one call in the system whose truncation is load-bearing rather
            # than cosmetic — a lost specialist opinion is a domain missing from
            # the panel the verdict's floor is checked against, so the verdict
            # comes out `panel_incomplete` for a reason that was never about the
            # science. Still not a spend: a 900-token opinion bills 900 at either
            # ceiling.
            max_tokens=4000,
            # `channel` joins this consult's log row to the interview it was
            # made during. Without it every `consult_*` row landed with
            # channel NULL — the phase name gave the domain but nothing tied
            # the call to a discussion, so the panel behind a verdict could
            # not be reconstructed from the logs at all. Same key
            # `thread_reply` uses (see _reply_to_thread's log_meta); the
            # column is nullable, so None from a caller with no thread is the
            # honest value rather than a hole.
            log_meta={
                "agent_id": agent_id,
                "phase": f"consult_{domain}",
                "channel": channel,
            },
            # A truncation retry is a second billed call — book it too, same
            # contract every other generate_agent_response caller uses.
            on_retry=on_api_call,
            # Why a consult needs the stop_reason at all: `stop_reason` was
            # compared against "max_tokens" at nine sites in llm.py and branched
            # on NOWHERE else in src/, so a `refusal` — the API's own word for a
            # reply it stopped mid-sentence — was indistinguishable from a
            # complete answer at every call site. See the `truncated` branch
            # below, which reads BOTH stops through `is_truncated_stop`.
            on_stop_reason=stop_reasons.append,
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
    # A reply the API stopped mid-sentence is not an opinion either, and it is a
    # WORSE failure than an empty one because it looks like a complete answer.
    # Measured over run 8b64a0e0: 3 consults ended in `refusal`, all 3 were
    # credited to the panel, all 3 contributed zero concerns and zero questions
    # (the JSON was cut mid-array, so `parse_opinion` defaulted the signal), and
    # all 3 were published into the PI's own interview thread as "⚠️ caution".
    # markham's discarded 3.04 verdict rested on that panel. See
    # docs/audits/2026-08-22-run-8b64a0e0/rca-and-corrections.md, H4/H5.
    #
    # Note what this does NOT do: it does not suppress the durable record below.
    # A truncated opinion is the only evidence the attempt happened at all — the
    # same run's `sinnis` has NO chemistry consult on record while chemistry was
    # attempted and refused five times, which is how a panel record becomes
    # substantially fictional. Record it; just do not count it.
    #
    # `is_truncated_stop`, not `== "refusal"`: `refusal` is the classifier
    # cutting the reply and `max_tokens` is the ceiling doing it, and the text
    # in hand is equally partial either way. The one-value test credited every
    # consult that hit the 4000-token ceiling, retried and truncated AGAIN — the
    # loudest case in the module, since `max_tokens=4000`'s own comment above
    # records consults already returning 2299 output tokens. It also decides
    # whether `generate_agent_response` may hand back a first-pass answer whose
    # retry died: that fallthrough reports `max_tokens`, so it is safe here only
    # while this predicate is the shared one (src/services/llm.py).
    truncated = bool(stop_reasons) and is_truncated_stop(stop_reasons[-1])
    read_state = read_state_for(truncated=truncated, opinion=opinion)
    if truncated:
        logger.error(
            "[specialists] %s consult for %s was cut off mid-reply "
            "(stop_reason=%r) — recorded, but NOT counted as consulted",
            domain, agent_id, stop_reasons[-1],
        )
    elif on_consult is not None:
        on_consult(domain, opinion.verdict_signal)
    logger.info(
        "[specialists] %s consulted %s -> %s (%s)%s",
        agent_id, domain, opinion.verdict_signal, opinion.confidence,
        " [TRUNCATED, uncounted]" if truncated else "",
    )
    if on_consult_record is not None:
        try:
            await on_consult_record(
                domain=domain,
                question=question,
                context_excerpt=context[:_CONSULT_CONTEXT_EXCERPT_CHARS] or None,
                verdict_signal=opinion.verdict_signal,
                confidence=opinion.confidence,
                # Lists, not the dataclass's tuples: these land in JSONB.
                concerns=list(opinion.concerns),
                questions_to_ask=list(opinion.questions_to_ask),
                established=list(opinion.established),
                raw_opinion=opinion.raw,
                # The row's own copy of the refusal above — a `refusal` OR a
                # `max_tokens`, whichever cut the reply off. Without it the
                # stored consult is byte-indistinguishable from a complete
                # one, so `_seed_consults_from_db` rehydrates it after a
                # restart as a domain that counts and the floor is satisfied
                # by an opinion nobody finished reading — the in-process
                # refusal undone by the next `docker stop`. Always sent, False
                # included: NULL in that column means "written before 0036",
                # which is a third state and not this one.
                truncated=truncated,
                # Generalises the cancellation above: `_post_panel_note`
                # (src/agent/simulation.py) refuses to publish a signal for
                # any opinion this predicate did not mark "parsed" — a
                # complete reply that failed to parse is just as unread as a
                # truncated one, and until now it still posted a
                # workspace-visible verdict nobody produced. Not stored yet:
                # `_record_specialist_consult` accepts and ignores it until
                # migration 0038 (a later task) adds the column.
                read_state=read_state,
            )
        except Exception as exc:  # noqa: BLE001 — a record must not cost the opinion
            # The engine's writer is itself best-effort and already logs its own
            # failures, so reaching here means something outside it broke (a
            # callback with the wrong shape, say). Caught HERE rather than left
            # to execute_tool's outer handler, which would replace this
            # function's return value with "Error executing consult_specialist:
            # ..." — telling the model its consult failed when the opinion is
            # right there, parsed, and already credited to the floor.
            logger.error(
                "[specialists] %s consult recorded in memory but NOT durably: %s",
                domain, exc, exc_info=True,
            )
    if truncated:
        # Say so in the string the MODEL reads, not just in the log. Otherwise
        # the hub consults once, sees a plausible opinion, believes the domain is
        # covered, and concludes — while the floor it is about to be checked
        # against has recorded nothing. The signal is deliberately not repeated
        # here: it was defaulted, and quoting it would dress a parse failure up
        # as a verdict.
        return (
            f"The {domain} specialist's reply was TRUNCATED mid-answer, so it "
            f"does not count as consulted — consult {domain} again before you "
            f"conclude. The partial text follows, unparsed:\n\n{opinion.raw}"
        )
    # Label AFTER the body, deliberately. This used to be
    # f"{spec.title} — signal: {signal}\n\n{raw}", which put a verdict word
    # ahead of the evidence in the hub's context — the worst position for it.
    # Anchoring on a score already in context reaches Cohen's d = 0.71 and is
    # not removable by instruction (arXiv:2608.25869); generating evidence
    # before rating is worth +6 to +11 accuracy points (arXiv:2305.17926).
    # `read: parsed` is stated so the hub can tell a read opinion from one whose
    # signal was defaulted — the same distinction `read_state` draws for the
    # panel note.
    return (
        f"{spec.title}\n\n{opinion.raw}\n\n"
        f"— signal: {opinion.verdict_signal} (read: {read_state})"
    )
