"""BlackbirdBot projects: the worker's research and choice, one model call per step.

Spec: docs/specs/2026-10-22-idea-pitching-prototype-design.md §5.3 (research) and §5.4
(choice). The engine requests a project per lab (``hub_projects``, status ``requested``);
this module takes the oldest due project of the live run, while that run's Projects session is
active (``live_projects_run_id``), and advances it by ONE model call per step. The worker
runs up to ``project_research_concurrency`` lanes (``research_round``) that take steps back to
back, and a claimed step leases its project (``STEP_LEASE``) so no other lane takes it:

- five research sessions (``RESEARCH_DOMAINS``), one after another. Each session's system
  prompt is ``prompts/projects/research.md`` plus that specialist's "What you own" and "What
  you do not own" sections (``persona_domain_sections``); its first user message is the lab's
  public profile (the seven exported sections, each fenced as ``lab_profile``) and its
  earlier projects. A reply asking for tools is answered in the same step (``search_pubmed``,
  ``search_trials``, ``search_prior_art``, ``retrieve_abstract``) and the conversation is
  stored in ``hub_projects.research``, so the next step continues it; after
  ``project_research_max_tool_rounds`` rounds the next call has no tools and asks for the
  answer. A session's candidates keep only anchors that are whole profile items and sources a
  tool returned in that session (``screen_candidates``);
- then the choice (``prompts/projects/choose.md``, structured output, ``CHOOSE_SCHEMA``) over
  the surviving candidates, numbered from 1 in ``RESEARCH_DOMAINS`` order. A choice that fails
  ``check_choice`` is retried once with its failures stated; a second failure ends the project
  ``not_finished`` / ``choice_failed``. A passing choice is stored resolved (never as the
  model's indices) with the posting code assembled (``project_plan.assemble_posting``), and the
  project becomes ``ready_to_post``.

There is no dollar ceiling (removed by the owner, 2026-10-27). Every call is still claimed
first in its own ``hub_project_calls`` row at the call's worst case and updated with its real
usage after, so a call the worker died in is still on the ledger at its reserve and the same
step simply runs again. A rate limit or 5xx answer (never billed) waits
``TRANSIENT_RETRY_AFTER`` without counting; PubMed being unreachable waits ``RETRY_AFTER``
without counting; any other failure counts, ``MAX_SESSION_FAILURES`` end a session, and a
refusal ends its session at once. Every project write is a compare-and-set on the status
this step read, so a project the engine moved meanwhile (ended, say) is left as the engine
wrote it; a step's own writes also require its lease, so a step that outlived it stores only
its call row.

The inputs are the lab's public persona file only: no private profile, staff review or
interview transcript. Nothing here logs candidate, plan or posting text.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import anthropic
from sqlalchemy import func, insert, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.agent import persona_sections
from src.agent.prompt_safety import delimit
from src.agent.role_capabilities import hub_role_names
from src.agent.specialists import persona_path
from src.config import get_settings
from src.models import AgentRegistry
from src.models.hub_project import (
    CALL_DONE,
    CALL_FAILED,
    CALL_IN_PROGRESS,
    CALL_REFUSED,
    REASON_CHOICE_FAILED,
    REASON_NO_IDEA,
    REASON_RESEARCH_FAILED,
    STATUS_NOT_FINISHED,
    STATUS_READY_TO_POST,
    STATUS_REQUESTED,
    STATUS_RESEARCHING,
    HubProject,
    HubProjectCall,
    HubProjectClock,
)
from src.services import llm, profile_export, project_search
from src.services.assessment_chat import FALLBACK_BETA, SPEND_RESERVE_USD, entries_cost
from src.services.assessment_chat_stream import served_by, usage_entries
from src.services.blackbird_rubric import load_rubric
from src.services.json_extract import extract_json
from src.services.llm_pricing import PRICES, cost_for_tokens
from src.services.project_plan import (
    ANCHOR_SECTIONS,
    POSTING_MAX_MESSAGES,
    TEMPLATE_FIELDS,
    TITLE_MAX_CHARS,
    NameEntry,
    assemble_posting,
    check_anchors,
    display_id,
    foreign_names,
    has_link,
    has_method_or_model_anchor,
    match_anchor,
    normalize_item,
    resolve_source,
)
from src.services.runs import live_projects_run_id
from src.services.trajectory_generation import _api_error_code, _in_flight_entry, _is_transient

logger = logging.getLogger(__name__)

#: Read per step: ``prompts/`` is bind-mounted into the worker, so an edit applies to the
#: next step (the research state records each prompt's hash when research starts).
RESEARCH_PROMPT_PATH = Path("prompts/projects/research.md")
CHOOSE_PROMPT_PATH = Path("prompts/projects/choose.md")

RESEARCH_DOMAINS = ("clinical", "scientific", "technologic", "commercial", "legal")
#: Each session's angle (spec §5.3, "Angles").
RESEARCH_ANGLES: dict[str, str] = {
    "clinical": (
        "Look for indications, today's standard of care and clinical trials where the lab's "
        "work could open a new use."
    ),
    "scientific": (
        "Look for mechanisms, and for the published evidence that a new use of the lab's work "
        "could work."
    ),
    "technologic": (
        "Look for fields beyond the lab's current one where its methods or platforms could "
        "apply."
    ),
    "commercial": (
        "Look for unmet needs, and for who would want the result of a new use of the lab's "
        "work."
    ),
    "legal": "Look for patent activity around possible new uses of the lab's work.",
}

#: Under NONSTREAMING_MAX_TOKENS (src/services/llm.py); tests/unit/test_project_research.py
#: pins both.
RESEARCH_MAX_TOKENS = 8_000
CHOOSE_MAX_TOKENS = 8_000
#: One call's limit, with no SDK retry: a job waits behind at most one call of at most
#: 3 minutes. Both requests use ``effort: "medium"`` to bound thinking inside it.
CALL_TIMEOUT_SECONDS = 180.0
#: Input-token floor for one call's reserve; a larger request is reserved at its own
#: estimate (``estimate_input_tokens``).
RESERVE_INPUT_FLOOR = 16_000
#: The worker asks for due work at most this often while idle.
SWEEP_INTERVAL_SECONDS = 30.0
#: A lane takes steps back to back until one makes no call or this many seconds have passed
#: since its round began, so a job waits behind at most one round plus one step.
RESEARCH_ROUND_SECONDS = 60.0
#: The most lanes a round runs, whatever ``project_research_concurrency`` says: each lane holds
#: one connection of the worker's pool (5 + 10 overflow, one kept by the worker lock) and makes
#: one API call at a time (8 per process, ``src/services/llm.py``).
RESEARCH_MAX_LANES = 8
#: A claimed step holds its project this long through ``next_step_after``, so no other lane
#: takes it while its call runs. A worker that dies mid-step leaves the lease, and the step
#: runs again after it (its claimed call row stays on the ledger).
STEP_LEASE = timedelta(minutes=10)
#: Counted failures that end a research session with no candidates.
MAX_SESSION_FAILURES = 3
#: The wait after a counted failure, and while PubMed cannot be reached.
RETRY_AFTER = timedelta(minutes=10)
#: The wait after a rate limit or a 5xx answer (never billed, never counted).
TRANSIENT_RETRY_AFTER = timedelta(minutes=2)
CANDIDATES_PER_SESSION = 3
#: The rubric dimensions the choice weighs (keys in prompts/rubric/blackbird-rubric.toml).
#: Team & executability is left out: it is about the lab, the same for every candidate.
CHOICE_DIMENSIONS = (
    "differentiation_unmet_need",
    "scientific_credibility",
    "translational_path",
    "fundable_experiment",
    "venture_potential",
)
#: Failed choice attempts that end a project ``choice_failed``.
MAX_CHOICE_ATTEMPTS = 2
MAX_LAB_TOOLS = 10

FINAL_ANSWER_TEXT = "You have used your searches. Answer now."
_EPHEMERAL = {"type": "ephemeral"}

#: Research session statuses kept in the research state.
SESSION_RUNNING = "running"
SESSION_DONE = "done"
SESSION_SKIPPED = "skipped"
SESSION_FAILED = "failed"
SESSION_REFUSED = "refused"

#: ``NextStep.kind`` values.
STEP_RESEARCH = "research"
STEP_CHOOSE = "choose"
STEP_NO_IDEA = "no_idea"


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Prompts:
    research: str
    choose: str
    #: ``{"research": <12 hex>, "choose": <12 hex>}``: the first 12 hex of each sha256.
    shas: dict[str, str]


def load_prompts() -> Prompts | None:
    """Both prompt files, or None when either is missing, unreadable or empty."""
    texts: dict[str, str] = {}
    for name, path in (("research", RESEARCH_PROMPT_PATH), ("choose", CHOOSE_PROMPT_PATH)):
        try:
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return None
        if not text:
            return None
        texts[name] = text
    shas = {name: _sha12(text) for name, text in texts.items()}
    return Prompts(research=texts["research"], choose=texts["choose"], shas=shas)


def _sha12(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def persona_domain_sections(domain: str) -> str | None:
    """The specialist persona's text from the line ``## What you own`` up to (not
    including) the line ``## You do not decide``: its "What you own" and "What you do not
    own" sections, without the one question, verdict signal or answer format. None when
    either heading is missing or the file cannot be read."""
    try:
        lines = persona_path(domain).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    stripped = [line.rstrip() for line in lines]
    try:
        start = stripped.index("## What you own")
        end = stripped.index("## You do not decide", start)
    except ValueError:
        return None
    return "\n".join(lines[start:end]).strip()


@dataclass(frozen=True)
class LabContext:
    agent_id: str
    pi_name: str
    #: The lab's public persona file (``profiles/public/<agent_id>.md``).
    persona: str
    #: ``persona_sections.section_items(persona)``: the items an anchor may name.
    items: dict[str, list[str]]
    #: ``{"title", "anchors"}`` of the lab's earlier projects, in any run.
    earlier: list[dict]


def build_lab_context(agent_id: str, pi_name: str, earlier: list[dict]) -> LabContext | None:
    """The lab's context from its public persona file, or None when the file cannot be
    read or has no Research Summary."""
    path = profile_export.PROFILES_DIR / f"{agent_id}.md"
    try:
        persona = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    summary = persona_sections.sections(persona).get(persona_sections.RESEARCH_SUMMARY_HEADING)
    if not (summary or "").strip():
        return None
    return LabContext(
        agent_id=agent_id,
        pi_name=pi_name,
        persona=persona,
        items=persona_sections.section_items(persona),
        earlier=earlier,
    )


def _lab_surname(pi_name: str) -> str:
    parts = pi_name.split()
    return parts[-1] if parts else pi_name


def _profile_text(lab: LabContext) -> str:
    """The seven public sections, each under its heading and fenced as ``lab_profile``."""
    parts = persona_sections.sections(lab.persona)
    blocks = []
    for heading in persona_sections.PUBLIC_PROFILE_SECTIONS:
        body = (parts.get(heading) or "").strip()
        if body:
            blocks.append(f"### {heading}\n\n{delimit(body, 'lab_profile')}")
    return "\n\n".join(blocks) or "None"


def _earlier_text(lab: LabContext) -> str:
    if not lab.earlier:
        return "None"
    lines = []
    for entry in lab.earlier:
        anchors = [str(a) for a in entry.get("anchors") or []]
        suffix = f" (builds on: {'; '.join(anchors)})" if anchors else ""
        lines.append(f"- {entry.get('title') or ''}{suffix}")
    return delimit("\n".join(lines), "earlier_projects")


# ---------------------------------------------------------------------------
# The research state and the research request
# ---------------------------------------------------------------------------


def new_state(lab: LabContext, *, prompt_shas: Mapping[str, str]) -> dict:
    """The research state stored in ``hub_projects.research`` when research starts. A
    domain whose persona lacks either heading starts ``skipped`` (and is logged)."""
    sessions = []
    for domain in RESEARCH_DOMAINS:
        status = SESSION_RUNNING
        if persona_domain_sections(domain) is None:
            logger.warning(
                "Projects: the %s persona lacks its 'What you own'/'You do not decide' "
                "headings; that research session is skipped", domain,
            )
            status = SESSION_SKIPPED
        sessions.append({
            "domain": domain, "status": status, "rounds": 0, "failures": 0,
            "messages": [], "retrieved": {}, "candidates": [], "dropped": [],
        })
    return {
        "version": 1,
        "profile_sha256_12": _sha12(lab.persona),
        "prompt_sha256_12": dict(prompt_shas),
        "sessions": sessions,
        "choice_failures": [],
    }


def _valid_state(state: object) -> bool:
    return (
        isinstance(state, dict)
        and state.get("version") == 1
        and isinstance(state.get("sessions"), list)
        and len(state["sessions"]) == len(RESEARCH_DOMAINS)
        and all(isinstance(s, dict) for s in state["sessions"])
        and isinstance(state.get("choice_failures"), list)
    )


def _opening_message(lab: LabContext, domain: str) -> dict:
    text = (
        f"## Your angle\n\n{RESEARCH_ANGLES[domain]}\n\n"
        f"## The lab\n\n{_profile_text(lab)}\n\n"
        f"## Earlier projects for this lab\n\n{_earlier_text(lab)}\n\n"
        "Research now."
    )
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def research_request(
    state: dict, index: int, lab: LabContext, *, model: str, frame: str, max_rounds: int
) -> dict:
    """The request for research session ``index``: its stored conversation (or the
    opening message), with tools while fewer than ``max_rounds`` rounds have run and
    without them after, when ``FINAL_ANSWER_TEXT`` closes the last user message. The last
    ``tool_result`` block carries the message-side cache breakpoint and no earlier block
    does (it rolls forward round by round); the stored messages are copied, never marked."""
    session = state["sessions"][index]
    domain = session["domain"]
    system = f"{frame}\n\n## Your domain\n\n{persona_domain_sections(domain) or ''}"
    messages = copy.deepcopy(session.get("messages") or []) or [_opening_message(lab, domain)]
    last_result = None
    for message in messages:
        for block in message.get("content") or []:
            if isinstance(block, dict):
                block.pop("cache_control", None)
                if block.get("type") == "tool_result":
                    last_result = block
    if last_result is not None:
        last_result["cache_control"] = dict(_EPHEMERAL)
    request: dict[str, Any] = {
        "model": model,
        "max_tokens": RESEARCH_MAX_TOKENS,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "medium"},
        "betas": [FALLBACK_BETA],
        "fallbacks": "default",
        "system": system,  # abeta_create marks a str system prompt for caching
        "messages": messages,
    }
    if int(session.get("rounds") or 0) < max_rounds:
        request["tools"] = project_search.research_tool_definitions()
    else:
        final = {"type": "text", "text": FINAL_ANSWER_TEXT}
        if messages[-1].get("role") == "user":
            messages[-1]["content"] = [*messages[-1]["content"], final]
        else:
            messages.append({"role": "user", "content": [final]})
    return request


def estimate_input_tokens(request: dict) -> int:
    """A generous estimate: one token per three characters of the system prompt and of
    every message block's text (``text``, a ``tool_result``'s content, a ``tool_use``'s
    input as JSON)."""
    chars = len(str(request.get("system") or ""))
    for message in request.get("messages") or []:
        content = message.get("content")
        if isinstance(content, str):
            chars += len(content)
            continue
        for block in content or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                chars += len(str(block.get("text") or ""))
            elif kind == "tool_result":
                inner = block.get("content")
                if isinstance(inner, list):
                    chars += sum(
                        len(str(part.get("text") or "")) for part in inner if isinstance(part, dict)
                    )
                else:
                    chars += len(str(inner or ""))
            elif kind == "tool_use":
                chars += len(json.dumps(block.get("input") or {}))
    return -(-chars // 3)


# ---------------------------------------------------------------------------
# A session's answer
# ---------------------------------------------------------------------------


def _str(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _list(value: object) -> list:
    return value if isinstance(value, list) else []


def parse_session_output(text: str) -> list[dict]:
    """The candidates of a session's final answer, read tolerantly (``extract_json``
    handles fences and prose around the object): each keeps ``use``, ``anchors``
    (``section``, ``item``), ``sources`` (``id``, ``shows``) and ``why``; at most
    ``CANDIDATES_PER_SESSION``. No object, a non-dict or no ``candidates`` list gives
    ``[]``."""
    if not isinstance(text, str):
        return []
    try:
        payload = extract_json(text)
    except ValueError:
        return []
    if not isinstance(payload, dict) or not isinstance(payload.get("candidates"), list):
        return []
    found: list[dict] = []
    for raw in payload["candidates"]:
        if not isinstance(raw, dict):
            continue
        anchors = [
            {"section": _str(a.get("section")), "item": _str(a.get("item"))}
            for a in raw.get("anchors") or []
            if isinstance(a, dict) and _str(a.get("item"))
        ]
        sources = [
            {"id": _str(s.get("id")), "shows": _str(s.get("shows"))}
            for s in raw.get("sources") or []
            if isinstance(s, dict) and _str(s.get("id"))
        ]
        found.append({
            "use": _str(raw.get("use")), "anchors": anchors, "sources": sources,
            "why": _str(raw.get("why")),
        })
        if len(found) == CANDIDATES_PER_SESSION:
            break
    return found


def screen_candidates(
    candidates: list[dict],
    *,
    items: Mapping[str, Sequence[str]],
    retrieved: Mapping[str, dict],
    domain: str,
) -> tuple[list[dict], list[dict]]:
    """Kept and dropped candidates. Anchors go through ``check_anchors`` (kept ones carry
    the profile's own text); a cited source is kept only when ``resolve_source`` finds it
    among the session's retrieved keys, and gains its ``key``, ``kind`` and ``title``. A
    candidate is kept only with a method-or-model anchor and at least one source. Both
    lists carry ``domain``; dropped ones carry ``why``; the anchors and source ids a kept
    candidate lost are listed under ``dropped_anchors`` and ``unretrieved_sources``."""
    kept: list[dict] = []
    dropped: list[dict] = []
    for candidate in candidates:
        good_anchors, bad_anchors = check_anchors(candidate.get("anchors") or [], items)
        sources: list[dict] = []
        unretrieved: list[str] = []
        for source in candidate.get("sources") or []:
            key = resolve_source(source["id"], retrieved)
            if key is None:
                unretrieved.append(source["id"])
                continue
            if any(s["key"] == key for s in sources):
                continue
            found = retrieved[key]
            sources.append({
                "id": source["id"], "shows": source.get("shows", ""), "key": key,
                "kind": found.get("kind"), "title": found.get("title") or "",
            })
        record = {
            **candidate, "domain": domain, "anchors": good_anchors, "sources": sources,
            "dropped_anchors": bad_anchors, "unretrieved_sources": unretrieved,
        }
        if not has_method_or_model_anchor(good_anchors):
            dropped.append({
                **record,
                "why": "no anchor from Key Methods and Technologies or Model Systems",
            })
        elif not sources:
            dropped.append({**record, "why": "no source that a search in this session returned"})
        else:
            kept.append(record)
    return kept, dropped


def survivors_of(state: dict) -> list[dict]:
    """Every kept candidate, in ``RESEARCH_DOMAINS`` order and, within a session, in its
    candidates' order: survivor ``n`` (from 1) is the choice's candidate ``n``."""
    return [c for session in state["sessions"] for c in session.get("candidates") or []]


# ---------------------------------------------------------------------------
# The choice
# ---------------------------------------------------------------------------

_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        key: (
            {"type": "array", "items": {"type": "string"}}
            if key == "lab_tools" else {"type": "string"}
        )
        for key, _label in TEMPLATE_FIELDS
    },
    "required": [key for key, _label in TEMPLATE_FIELDS],
    "additionalProperties": False,
}

#: The choice's structured answer. No score, rank, band or rating is asked for (D43).
CHOOSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "chosen": {"type": "integer"},
        "why_chosen": {"type": "string"},
        "others": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"index": {"type": "integer"}, "note": {"type": "string"}},
                "required": ["index", "note"],
                "additionalProperties": False,
            },
        },
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "plan": _PLAN_SCHEMA,
        "anchors": {"type": "array", "items": {"type": "string"}},
        "sources": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "chosen", "why_chosen", "others", "title", "summary", "plan", "anchors", "sources",
    ],
    "additionalProperties": False,
}


def _candidates_text(survivors: Sequence[dict]) -> str:
    lines = []
    for number, candidate in enumerate(survivors, 1):
        anchors = "; ".join(f"{a['section']}: {a['item']}" for a in candidate["anchors"])
        sources = "; ".join(
            f"{display_id(s['key'])} — {s.get('title') or ''} (shows: {s.get('shows') or ''})"
            for s in candidate["sources"]
        )
        lines += [
            f"{number}. From the {candidate['domain']} specialist: {candidate.get('use') or ''}",
            f"   Builds on: {anchors}",
            f"   Sources: {sources}",
            f"   Why: {candidate.get('why') or ''}",
        ]
    return "\n".join(lines)


def _criteria_text() -> str:
    by_key = {d.key: d for d in load_rubric().dimensions}
    parts = []
    for key in CHOICE_DIMENSIONS:
        dimension = by_key.get(key)
        if dimension is not None:
            parts.append(f"### {dimension.title}\n\n{dimension.anchors}")
    return "\n\n".join(parts)


def choose_request(
    lab: LabContext,
    survivors: list[dict],
    *,
    model: str,
    frame: str,
    failures: Sequence[str],
) -> dict:
    """The structured choice request: the numbered candidates (``research_candidates``),
    the lab's sections (``lab_profile``), its earlier projects (``earlier_projects``),
    Blackbird's criteria (the five ``CHOICE_DIMENSIONS``' title and anchors) and, on a
    retry, the previous attempt's failures."""
    text = (
        f"## The candidates\n\n{delimit(_candidates_text(survivors), 'research_candidates')}\n\n"
        f"## The lab\n\n{_profile_text(lab)}\n\n"
        f"## Earlier projects for this lab\n\n{_earlier_text(lab)}\n\n"
        f"## Blackbird's criteria\n\n{_criteria_text()}"
    )
    if failures:
        listed = "\n".join(f"- {failure}" for failure in failures)
        text += f"\n\n## Your previous answer failed these checks\n\n{listed}"
    text += "\n\nChoose now."
    return {
        "model": model,
        "max_tokens": CHOOSE_MAX_TOKENS,
        "thinking": {"type": "adaptive"},
        "output_config": {
            "effort": "medium",
            "format": {"type": "json_schema", "schema": CHOOSE_SCHEMA},
        },
        "betas": [FALLBACK_BETA],
        "fallbacks": "default",
        "system": frame,
        "messages": [{"role": "user", "content": [{"type": "text", "text": text}]}],
    }


def _valid_index(value: object, survivors: Sequence[dict]) -> bool:
    return (
        isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= len(survivors)
    )


#: Where a source's shown id ends in ``_candidates_text``'s ``"<id> — <title> (shows: …)"``:
#: the first em or en dash, or a hyphen with spaces around it. No id contains one.
_SOURCE_ID_END_RE = re.compile(r"\s*[—–]\s*|\s+-\s+")


def _kept_anchor(text: str, candidate: Mapping[str, Any]) -> dict | None:
    """The chosen candidate's kept anchor ``text`` names, in the profile's own words.

    ``text`` is the item alone or, as ``_candidates_text`` shows it and the choice prompt
    says to copy it, ``"<section>: <item>"``; a section prefix matches only that section's
    items."""
    grouped: dict[str, list[str]] = {section: [] for section in ANCHOR_SECTIONS}
    for anchor in candidate.get("anchors") or []:
        grouped.setdefault(anchor["section"], []).append(anchor["item"])
    section = match_anchor(text, grouped)
    if section is not None:
        target = normalize_item(text)
        item = next(i for i in grouped[section] if normalize_item(i) == target)
        return {"section": section, "item": item}
    head, colon, rest = text.partition(":")
    if colon:
        target = normalize_item(rest)
        for section, items in grouped.items():
            if normalize_item(head) == normalize_item(section):
                for item in items:
                    if normalize_item(item) == target:
                        return {"section": section, "item": item}
    return None


def _kept_source(raw: str, candidate: Mapping[str, Any]) -> dict | None:
    """The chosen candidate's kept source ``raw`` names: by its key, its displayed id or
    ``resolve_source``, tried on the whole text and then on the id before the title, since
    ``_candidates_text`` shows a source as ``"<id> — <title> (shows: …)"`` and the choice
    prompt says to copy it."""
    by_key = {s["key"]: s for s in candidate.get("sources") or []}
    raw = raw.strip()
    shown_id = _SOURCE_ID_END_RE.split(raw, maxsplit=1)[0].strip()
    for cited in dict.fromkeys((raw, shown_id)):
        for key, source in by_key.items():
            if cited in (key, display_id(key)):
                return source
        key = resolve_source(cited, by_key)
        if key:
            return by_key[key]
    return None


def _plan_of(choice: Mapping[str, Any]) -> dict[str, Any]:
    plan = choice.get("plan") if isinstance(choice.get("plan"), dict) else {}
    resolved: dict[str, Any] = {}
    for key, _label in TEMPLATE_FIELDS:
        value = plan.get(key)
        if key == "lab_tools":
            resolved[key] = [v.strip() for v in value if isinstance(v, str)] if isinstance(
                value, list) else []
        else:
            resolved[key] = _str(value)
    return resolved


def _strings(choice: Mapping[str, Any]) -> list[str]:
    """The model-written texts a name or a link must not appear in: the title, the summary,
    why it was chosen, the notes on the others, every plan string and ``lab_tools``."""
    texts = [_str(choice.get("title")), _str(choice.get("summary")), _str(choice.get("why_chosen"))]
    for other in _list(choice.get("others")):
        if isinstance(other, dict):
            texts.append(_str(other.get("note")))
    plan = _plan_of(choice)
    for key, _label in TEMPLATE_FIELDS:
        value = plan[key]
        texts.extend(value if isinstance(value, list) else [value])
    return [t for t in texts if t]


def _length_failures(choice: Mapping[str, Any]) -> list[str]:
    """An empty title or summary, a title over ``TITLE_MAX_CHARS``, too many lab tools. The
    summary and the plan fields have no length limit of their own: only the whole posting's
    (``check_choice``) bounds them."""
    failures = []
    title, summary = _str(choice.get("title")), _str(choice.get("summary"))
    if not title:
        failures.append("the title is empty")
    if len(title) > TITLE_MAX_CHARS:
        failures.append(f"the title is longer than {TITLE_MAX_CHARS} characters")
    if not summary:
        failures.append("the summary is empty")
    if len(_plan_of(choice)["lab_tools"]) > MAX_LAB_TOOLS:
        failures.append(f"more than {MAX_LAB_TOOLS} lab tools are listed")
    return failures


def _reference_failures(choice: Mapping[str, Any], candidate: Mapping[str, Any]) -> list[str]:
    failures = []
    anchors = _list(choice.get("anchors"))
    if not anchors:
        failures.append("no anchor is listed")
    for anchor in anchors:
        if not isinstance(anchor, str) or _kept_anchor(anchor, candidate) is None:
            failures.append(f"the anchor {anchor!r} is not one of the chosen candidate's anchors")
    sources = _list(choice.get("sources"))
    if not sources:
        failures.append("no source is listed")
    for source in sources:
        if not isinstance(source, str) or _kept_source(source, candidate) is None:
            failures.append(f"the source {source!r} is not one of the chosen candidate's sources")
    return failures


def check_choice(
    choice: dict,
    survivors: list[dict],
    *,
    lab: LabContext,
    roster: Sequence[NameEntry],
    hub_agent_ids: AbstractSet[str],
) -> list[str]:
    """The model's choice's failures, in plain words (``[]`` when it passes): an index out
    of range; an anchor or source that is not the chosen candidate's; another lab's, PI's or
    bot's name; any ``@``; any link (``escape_for_slack`` would keep it live in
    ``#general``); an empty title or summary; a title over ``TITLE_MAX_CHARS``; more than
    ``MAX_LAB_TOOLS`` lab tools; a posting that ``assemble_posting`` cannot fit."""
    if not _valid_index(choice.get("chosen"), survivors):
        return [f"chosen ({choice.get('chosen')!r}) is not the number of a listed candidate"]
    candidate = survivors[choice["chosen"] - 1]
    failures = []
    for other in _list(choice.get("others")):
        index = other.get("index") if isinstance(other, dict) else None
        if not _valid_index(index, survivors):
            failures.append(f"others names {index!r}, which is not the number of a listed candidate")
    failures += _reference_failures(choice, candidate)
    texts = _strings(choice)
    names = dict.fromkeys(
        name
        for text in texts
        for name in foreign_names(
            text, roster=roster, own_agent_id=lab.agent_id, hub_agent_ids=hub_agent_ids
        )
    )
    failures += [f"names another lab, PI or bot: {name}" for name in names]
    every = [*texts, *(a for a in _list(choice.get("anchors")) if isinstance(a, str))]
    if any("@" in text for text in every):
        failures.append('uses "@"')
    if any(has_link(text) for text in every):
        failures.append("contains a link")
    failures += _length_failures(choice)
    if not failures:
        stored = resolve_choice(choice, survivors)
        # The posting shows each anchor in the profile's own wording, which this function's
        # text checks above never read.
        failures += [
            f"the anchor {a['item']!r} carries a link or an @ in the lab's profile; choose another"
            for a in stored["anchors"] if "@" in a["item"] or has_link(a["item"])
        ]
    if not failures:
        try:
            posting_for(stored, lab)
        except ValueError as exc:
            failures.append(
                f"the posting is too long for {POSTING_MAX_MESSAGES} Slack messages; shorten "
                "the summary or the plan" if "too long" in str(exc)
                else "the posting would carry a link or an @"
            )
    return failures


def resolve_choice(choice: Mapping[str, Any], survivors: Sequence[dict]) -> dict:
    """The stored ``choice`` (Tasks 5, 7 and 9 read it): the model's numbers resolved to
    the candidates they name, anchors in the profile's own text and the chosen candidate's
    kept sources the model listed. Call only on a choice ``check_choice`` passes."""
    candidate = survivors[choice["chosen"] - 1]
    others = [
        {
            "use": survivors[o["index"] - 1].get("use") or "",
            "domain": survivors[o["index"] - 1]["domain"],
            "note": _str(o.get("note")),
        }
        for o in _list(choice.get("others"))
        if isinstance(o, dict) and _valid_index(o.get("index"), survivors)
        and o["index"] != choice["chosen"]
    ]
    anchors: list[dict] = []
    for raw in _list(choice.get("anchors")):
        anchor = _kept_anchor(raw, candidate) if isinstance(raw, str) else None
        if anchor is not None and anchor not in anchors:
            anchors.append(anchor)
    sources: list[dict] = []
    for raw in _list(choice.get("sources")):
        source = _kept_source(raw, candidate) if isinstance(raw, str) else None
        if source is not None and all(s["key"] != source["key"] for s in sources):
            sources.append({
                "key": source["key"], "kind": source.get("kind"),
                "title": source.get("title") or "", "domain": candidate["domain"],
                "shows": source.get("shows") or "",
            })
    return {
        "chosen": {
            "use": candidate.get("use") or "", "why": candidate.get("why") or "",
            "domain": candidate["domain"],
        },
        "why_chosen": _str(choice.get("why_chosen")),
        "others": others,
        "title": _str(choice.get("title")),
        "summary": _str(choice.get("summary")),
        "plan": _plan_of(choice),
        "anchors": anchors,
        "sources": sources,
    }


def posting_for(stored: Mapping[str, Any], lab: LabContext) -> str:
    """The posting text for a stored choice; raises ``ValueError`` when it cannot fit."""
    return assemble_posting(
        surname=_lab_surname(lab.pi_name),
        summary=stored["summary"],
        anchors=[a["item"] for a in stored["anchors"]],
        plan=stored["plan"],
        sources=stored["sources"],
    )


# ---------------------------------------------------------------------------
# One step: plan it, make the call, apply the reply
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NextStep:
    kind: str  # STEP_RESEARCH, STEP_CHOOSE or STEP_NO_IDEA
    #: The ledger's ``step``: ``research:<domain>`` or ``choose``.
    step: str = ""
    index: int = -1
    request: dict | None = None
    survivors: list[dict] = field(default_factory=list)


@dataclass
class StepOutcome:
    """What one call stores: the ledger row's fields, and how the project moves."""

    call_status: str
    error_code: str | None
    entries: list[dict[str, Any]]
    served_by_model: str | None
    #: Wait this long before the project's next step (``next_step_after``).
    wait: timedelta | None = None
    #: End the project ``not_finished`` with this reason.
    end_reason: str | None = None
    #: The stored choice and posting: the project becomes ``ready_to_post``.
    choice: dict | None = None
    posting_text: str | None = None


def plan_next_step(
    state: dict, lab: LabContext, *, model: str, prompts: Prompts, max_rounds: int
) -> NextStep:
    """The project's next call: the first running session's, else the choice, else
    ``STEP_NO_IDEA`` when no candidate survived. A running session whose persona headings
    have gone is marked skipped here (``state`` is updated in place)."""
    for index, session in enumerate(state["sessions"]):
        if session.get("status") != SESSION_RUNNING:
            continue
        if persona_domain_sections(session["domain"]) is None:
            logger.warning(
                "Projects: the %s persona lacks its headings; that session is skipped",
                session["domain"],
            )
            session["status"] = SESSION_SKIPPED
            continue
        request = research_request(
            state, index, lab, model=model, frame=prompts.research, max_rounds=max_rounds
        )
        return NextStep(STEP_RESEARCH, f"research:{session['domain']}", index, request)
    survivors = survivors_of(state)
    if not survivors:
        return NextStep(STEP_NO_IDEA)
    failures = state["choice_failures"][-1] if state["choice_failures"] else []
    request = choose_request(
        lab, survivors, model=model, frame=prompts.choose, failures=failures
    )
    return NextStep(STEP_CHOOSE, "choose", -1, request, survivors)


async def call_model(request: dict) -> tuple[Any, Exception | None]:
    """``(message, None)`` or ``(None, the error)``: one call, no SDK retry.

    Any exception is returned, not only an SDK error: one escaping here (a ``TypeError`` from
    an SDK that rejects a parameter, ``llm``'s own pre-send guard) would leave the claimed
    ledger row ``in_progress`` at its reserve and the project due again at the next sweep, so
    every sweep would book another reserve, with no end (there is no dollar ceiling). As
    an error it is a counted failure, reserved at the request's worst case like a timeout."""
    try:
        message = await llm.abeta_create(
            llm.get_anthropic_client().with_options(timeout=CALL_TIMEOUT_SECONDS, max_retries=0),
            **request,
        )
    except Exception as exc:  # noqa: BLE001 — see the docstring
        return None, exc
    return message, None


def _refusal_code(message: Any) -> str:
    category = getattr(getattr(message, "stop_details", None), "category", None)
    return (f"refusal:{category}" if category else "refusal")[:40]


def _text_of(message: Any) -> str:
    return "".join(
        getattr(block, "text", "") or ""
        for block in (getattr(message, "content", None) or [])
        if getattr(block, "type", None) == "text"
    )


def _end_session(session: dict, status: str) -> None:
    """A session ends; its conversation is dropped (its candidates, dropped candidates and
    retrieved sources stay)."""
    session["status"] = status
    session["messages"] = []
    if status != SESSION_DONE:
        session["candidates"] = []


def _counted_failure(
    session: dict, code: str, entries: list[dict[str, Any]], served: str | None
) -> StepOutcome:
    session["failures"] = int(session.get("failures") or 0) + 1
    session["last_error"] = code
    if session["failures"] >= MAX_SESSION_FAILURES:
        _end_session(session, SESSION_FAILED)
        return StepOutcome(CALL_FAILED, code, entries, served)
    return StepOutcome(CALL_FAILED, code, entries, served, wait=RETRY_AFTER)


def _api_failure(exc: Exception, model: str, request: dict) -> tuple[str, list, bool]:
    """``(error code, usage entries, transient)`` for a call's error. An error status is
    never billed; a timeout, a cut connection or any other error is reserved at this
    request's worst case."""
    if not isinstance(exc, anthropic.AnthropicError):
        # A client-side bug (an SDK that rejects a parameter, llm's pre-send guard), not an
        # API answer: name it, or every failed session reads "api_error".
        return (f"error:{type(exc).__name__}"[:40],
                [_in_flight_entry(model, estimate_input_tokens(request))], False)
    code = _api_error_code(exc)
    if isinstance(exc, anthropic.APIStatusError):
        return code, [], _is_transient(exc)
    return code, [_in_flight_entry(model, estimate_input_tokens(request))], False


async def _run_tools(blocks: Sequence[Any], session: dict) -> list[dict]:
    """One ``tool_result`` per ``tool_use`` block, in block order; every source a tool
    returned is recorded in ``session["retrieved"]``. ``PubMedUnavailable`` propagates."""
    returned = []
    for block in blocks:
        tool_input = block.input if isinstance(block.input, Mapping) else {}
        returned.append(await project_search.execute_research_tool(block.name, tool_input))
    # Recorded only once every tool ran: a round PubMed interrupted is not stored, so its
    # sources must not count as retrieved either.
    for result in returned:
        for source in result.sources:
            if source.get("key"):
                session["retrieved"][source["key"]] = {
                    "kind": source.get("kind"), "title": source.get("title") or "",
                }
    return [
        {"type": "tool_result", "tool_use_id": block.id, "content": result.text}
        for block, result in zip(blocks, returned, strict=True)
    ]


async def _research_reply(
    session: dict, lab: LabContext, message: Any, request: dict
) -> StepOutcome:
    entries, served = usage_entries(message), served_by(message)
    stop = getattr(message, "stop_reason", None)
    if stop == "refusal":
        _end_session(session, SESSION_REFUSED)
        return StepOutcome(CALL_REFUSED, _refusal_code(message), entries, served)
    if stop == "max_tokens":
        return _counted_failure(session, "truncated", entries, served)
    blocks = list(getattr(message, "content", None) or [])
    tool_blocks = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
    if tool_blocks and "tools" in request:
        try:
            results = await _run_tools(tool_blocks, session)
        except project_search.PubMedUnavailable:
            # PubMed is the source every specialist relies on: wait, uncounted.
            return StepOutcome(CALL_FAILED, "pubmed_unavailable", entries, served, wait=RETRY_AFTER)
        base = session.get("messages") or [_opening_message(lab, session["domain"])]
        session["messages"] = [
            *base,
            {"role": "assistant", "content": [b.model_dump() for b in blocks]},
            {"role": "user", "content": results},
        ]
        session["rounds"] = int(session.get("rounds") or 0) + 1
        return StepOutcome(CALL_DONE, None, entries, served)
    kept, dropped = screen_candidates(
        parse_session_output(_text_of(message)),
        items=lab.items, retrieved=session.get("retrieved") or {}, domain=session["domain"],
    )
    session["candidates"], session["dropped"] = kept, dropped
    _end_session(session, SESSION_DONE)
    return StepOutcome(CALL_DONE, None, entries, served)


def _failed_choice(
    state: dict,
    failures: list[str],
    code: str,
    entries: list[dict[str, Any]],
    served: str | None,
    *,
    call_status: str = CALL_FAILED,
    wait: timedelta | None = None,
    final: bool = False,
) -> StepOutcome:
    """A failed choice attempt: recorded, retried once, then the project ends."""
    state["choice_failures"].append(failures)
    if final or len(state["choice_failures"]) >= MAX_CHOICE_ATTEMPTS:
        return StepOutcome(call_status, code, entries, served, end_reason=REASON_CHOICE_FAILED)
    return StepOutcome(call_status, code, entries, served, wait=wait)


def _read_choice(message: Any) -> dict | None:
    """The structured answer: structured output returns bare JSON, so a text that does not
    parse is a failed attempt."""
    try:
        payload = json.loads(_text_of(message))
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _choice_reply(
    state: dict,
    message: Any,
    survivors: list[dict],
    lab: LabContext,
    roster: Sequence[NameEntry],
    hub_agent_ids: AbstractSet[str],
) -> StepOutcome:
    entries, served = usage_entries(message), served_by(message)
    stop = getattr(message, "stop_reason", None)
    if stop == "refusal":
        return _failed_choice(
            state, ["the model declined to answer"], _refusal_code(message), entries, served,
            call_status=CALL_REFUSED, final=True,
        )
    if stop == "max_tokens":
        return _failed_choice(
            state, ["the answer was cut off; keep every field short"], "truncated", entries,
            served,
        )
    raw = _read_choice(message)
    if raw is None:
        return _failed_choice(
            state, ["the answer was not the JSON object asked for"], "malformed_json", entries,
            served,
        )
    failures = check_choice(raw, survivors, lab=lab, roster=roster, hub_agent_ids=hub_agent_ids)
    if failures:
        return _failed_choice(state, failures, "check_failed", entries, served)
    stored = resolve_choice(raw, survivors)
    return StepOutcome(
        CALL_DONE, None, entries, served, choice=stored, posting_text=posting_for(stored, lab)
    )


async def apply_result(
    state: dict,
    step: NextStep,
    lab: LabContext,
    *,
    message: Any,
    exc: Exception | None,
    model: str,
    roster: Sequence[NameEntry],
    hub_agent_ids: AbstractSet[str],
) -> StepOutcome:
    """Apply one call's reply (or SDK error) to ``state`` in place and say what to store."""
    if exc is not None:
        code, entries, transient = _api_failure(exc, model, step.request or {})
        if transient:
            return StepOutcome(CALL_FAILED, code, entries, None, wait=TRANSIENT_RETRY_AFTER)
        if step.kind == STEP_RESEARCH:
            return _counted_failure(state["sessions"][step.index], code, entries, None)
        return _failed_choice(
            state, [f"the previous attempt gave no usable answer ({code})"], code, entries, None,
            wait=RETRY_AFTER,
        )
    if step.kind == STEP_RESEARCH:
        return await _research_reply(
            state["sessions"][step.index], lab, message, step.request or {}
        )
    return _choice_reply(state, message, step.survivors, lab, roster, hub_agent_ids)


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


def _call_reserve(model: str, step: str, reserve_input_tokens: int = 0) -> Decimal:
    """One call's worst case: ``max(reserve_input_tokens, RESERVE_INPUT_FLOOR)`` in and the
    step's ``max_tokens`` out at the model's price, or ``SPEND_RESERVE_USD`` unpriced."""
    output = RESEARCH_MAX_TOKENS if step.startswith("research:") else CHOOSE_MAX_TOKENS
    cost = cost_for_tokens(
        model, input_tokens=max(int(reserve_input_tokens), RESERVE_INPUT_FLOOR),
        output_tokens=output, cache_read=0, cache_creation=0,
    )
    return SPEND_RESERVE_USD if cost is None else cost


def ledger_row_cost(usage_by_model: object, model: str, step: str) -> Decimal:
    """One ledger row's dollars: each ``partial`` entry (in flight, timed out) at its
    reserve, each other entry at its priced cost (an unpriced one at a reserve), and NULL
    usage at one reserve. ``[]`` (an unbilled error status) is free."""
    if not isinstance(usage_by_model, list):
        return _call_reserve(model, step)
    total = Decimal(0)
    for entry in usage_by_model:
        if not isinstance(entry, dict):
            total += _call_reserve(model, step)
        elif entry.get("partial"):
            total += _call_reserve(
                str(entry.get("model") or model), step,
                int(entry.get("reserve_input_tokens") or 0),
            )
        else:
            cost = entries_cost([entry])
            total += _call_reserve(model, step) if cost is None else cost
    return total


# ---------------------------------------------------------------------------
# The worker's side
# ---------------------------------------------------------------------------


async def load_roster(db: AsyncSession) -> tuple[list[NameEntry], frozenset[str]]:
    """Every registered agent's names, and the hub agents' ids (whose names may appear)."""
    rows = (
        await db.execute(
            select(AgentRegistry.agent_id, AgentRegistry.bot_name, AgentRegistry.pi_name,
                   AgentRegistry.role)
        )
    ).all()
    hubs = set(hub_role_names())
    roster = [NameEntry(agent_id, bot_name or "", pi_name or "")
              for agent_id, bot_name, pi_name, _role in rows]
    return roster, frozenset(agent_id for agent_id, _b, _p, role in rows if role in hubs)


async def load_lab(
    db: AsyncSession, agent_id: str, *, exclude_project: uuid.UUID | None = None
) -> LabContext | None:
    """The lab's context: its registry name, public persona and earlier projects (any run,
    other than ``exclude_project``). None when the lab or its persona cannot be read."""
    pi_name = await db.scalar(
        select(AgentRegistry.pi_name).where(AgentRegistry.agent_id == agent_id)
    )
    if pi_name is None:
        return None
    query = (
        select(HubProject.title, HubProject.choice)
        .where(HubProject.agent_id == agent_id, HubProject.title.is_not(None))
        .order_by(HubProject.requested_at, HubProject.id)
    )
    if exclude_project is not None:
        query = query.where(HubProject.id != exclude_project)
    earlier = []
    for title, choice in (await db.execute(query)).all():
        anchors = choice.get("anchors") if isinstance(choice, dict) else None
        earlier.append({
            "title": title,
            "anchors": [a.get("item") for a in anchors or [] if isinstance(a, dict)],
        })
    return build_lab_context(agent_id, pi_name, earlier)


_WARNED: set[str] = set()


def _warn_once(problem: str) -> None:
    """One WARNING per problem per process: the sweep asks every 30 s."""
    if problem not in _WARNED:
        _WARNED.add(problem)
        logger.warning("Projects: %s; no project research runs", problem)


@dataclass(frozen=True)
class _Config:
    model: str
    prompts: Prompts
    max_rounds: int


def _check() -> tuple[Prompts | None, str | None, str | None]:
    """``(prompts, None, None)`` when research can run; else ``(None, the admin's sentence,
    the log's problem)``, the problem None when the switch is simply off."""
    settings = get_settings()
    if not settings.project_research_enabled:
        return None, "Project research is switched off (PROJECT_RESEARCH_ENABLED=false).", None
    model = settings.llm_project_research_model
    if model not in PRICES:
        return (
            None,
            f"The research model {model} is not priced in src/services/llm_pricing.py.",
            f"model {model!r} is not priced in src/services/llm_pricing.py",
        )
    prompts = load_prompts()
    if prompts is None:
        return (
            None,
            f"The research or choice prompt ({RESEARCH_PROMPT_PATH} or {CHOOSE_PROMPT_PATH}) "
            "is missing or empty.",
            f"{RESEARCH_PROMPT_PATH} or {CHOOSE_PROMPT_PATH} is missing or empty",
        )
    return prompts, None, None


def research_unavailable_reason() -> str | None:
    """Why the worker cannot research projects, as one sentence for an admin, or None when
    it can: research switched off (``PROJECT_RESEARCH_ENABLED``), the research model not
    priced in ``src/services/llm_pricing.py``, or the research or choice prompt missing or
    empty. Reads the settings and the prompt files on each call."""
    return _check()[1]


def _config() -> _Config | None:
    prompts, _reason, problem = _check()
    if prompts is None:
        if problem is not None:
            _warn_once(problem)
        return None
    settings = get_settings()
    return _Config(
        model=settings.llm_project_research_model,
        prompts=prompts,
        max_rounds=int(settings.project_research_max_tool_rounds),
    )


@dataclass(frozen=True)
class _Claim:
    project_id: uuid.UUID
    run_id: uuid.UUID
    call_id: uuid.UUID
    lab: LabContext
    state: dict
    step: NextStep
    roster: list[NameEntry]
    hub_agent_ids: frozenset[str]
    lease_until: datetime


async def _due_project(db: AsyncSession, run_id: uuid.UUID) -> HubProject | None:
    """The live run's oldest ``requested`` or ``researching`` project whose wait is over."""
    return (
        await db.execute(
            select(HubProject)
            .where(
                HubProject.simulation_run_id == run_id,
                HubProject.status.in_((STATUS_REQUESTED, STATUS_RESEARCHING)),
                or_(
                    HubProject.next_step_after.is_(None),
                    HubProject.next_step_after <= func.clock_timestamp(),
                ),
            )
            .order_by(HubProject.requested_at, HubProject.requested_run_seconds, HubProject.id)
            .limit(1)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
    ).scalars().first()


async def _move(
    db: AsyncSession,
    project_id: uuid.UUID,
    expected: str,
    *,
    lease: datetime | None = None,
    **values: Any,
) -> bool:
    """Compare-and-set on the project's status and, given ``lease``, on ``next_step_after``
    still being that lease; False when the engine moved the project or the lease was lost."""
    values["updated_at"] = func.clock_timestamp()
    conditions = [HubProject.id == project_id, HubProject.status == expected]
    if lease is not None:
        conditions.append(HubProject.next_step_after == lease)
    result = await db.execute(
        update(HubProject)
        .where(*conditions)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def _ended(reason: str, run_id: uuid.UUID) -> dict[str, Any]:
    """The values of a project the worker ends: ``not_finished`` with ``reason``, finished
    now at the run's stored clock."""
    return {
        "status": STATUS_NOT_FINISHED,
        "reason_code": reason,
        "finished_at": func.clock_timestamp(),
        "finished_run_seconds": (
            select(HubProjectClock.run_seconds)
            .where(HubProjectClock.simulation_run_id == run_id)
            .scalar_subquery()
        ),
        "next_step_after": None,
    }


async def _claim(db: AsyncSession, config: _Config) -> _Claim | None:
    """Take the next step of the live run's oldest due project, lease the project for
    ``STEP_LEASE`` and claim its call in the ledger (both committed before the call). None
    when no call is due; a project that ends without a call (no lab persona, no surviving
    candidate) is ended and committed here."""
    run_id = await live_projects_run_id(db)
    if run_id is None:
        return None
    project = await _due_project(db, run_id)
    if project is None:
        return None
    expected = project.status
    lab = await load_lab(db, project.agent_id, exclude_project=project.id)
    if lab is None:
        await _move(db, project.id, expected, **_ended(REASON_RESEARCH_FAILED, run_id))
        await db.commit()
        logger.info("Projects: project=%s ended research_failed (no persona)", project.id)
        return None
    if expected == STATUS_REQUESTED or not _valid_state(project.research):
        state = new_state(lab, prompt_shas=config.prompts.shas)
    else:
        state = copy.deepcopy(project.research)
    step = plan_next_step(
        state, lab, model=config.model, prompts=config.prompts, max_rounds=config.max_rounds
    )
    if step.kind == STEP_NO_IDEA:
        await _move(db, project.id, expected, research=state, **_ended(REASON_NO_IDEA, run_id))
        await db.commit()
        logger.info("Projects: project=%s ended no_idea", project.id)
        return None
    lease_until = datetime.now(UTC) + STEP_LEASE
    if not await _move(
        db, project.id, expected, status=STATUS_RESEARCHING, research=state,
        next_step_after=lease_until,
    ):
        await db.commit()
        return None
    roster, hub_agent_ids = await load_roster(db)
    call_id = uuid.uuid4()
    request = step.request or {}
    await db.execute(
        insert(HubProjectCall).values(
            id=call_id, project_id=project.id, step=step.step, model=config.model,
            status=CALL_IN_PROGRESS,
            usage_by_model=[_in_flight_entry(config.model, estimate_input_tokens(request))],
            updated_at=func.clock_timestamp(),
        )
    )
    await db.commit()
    return _Claim(
        project.id, run_id, call_id, lab, state, step, roster, hub_agent_ids, lease_until
    )


async def _store(
    db: AsyncSession, claim: _Claim, state: dict, outcome: StepOutcome, latency_ms: int
) -> bool:
    """The call row's result (always, since the call is paid) and the project's move; False
    when the engine moved the project or the step lost its lease."""
    await db.execute(
        update(HubProjectCall)
        .where(HubProjectCall.id == claim.call_id)
        .values(
            status=outcome.call_status, error_code=outcome.error_code,
            usage_by_model=outcome.entries, served_by_model=outcome.served_by_model,
            latency_ms=latency_ms, updated_at=func.clock_timestamp(),
        )
        .execution_options(synchronize_session=False)
    )
    if outcome.choice is not None:
        return await _move(
            db, claim.project_id, STATUS_RESEARCHING, lease=claim.lease_until,
            status=STATUS_READY_TO_POST, research=state, choice=outcome.choice,
            title=outcome.choice["title"], posting_text=outcome.posting_text,
            next_step_after=None,
        )
    if outcome.end_reason is not None:
        return await _move(
            db, claim.project_id, STATUS_RESEARCHING, lease=claim.lease_until, research=state,
            **_ended(outcome.end_reason, claim.run_id),
        )
    wait_until = datetime.now(UTC) + outcome.wait if outcome.wait is not None else None
    return await _move(
        db, claim.project_id, STATUS_RESEARCHING, lease=claim.lease_until, research=state,
        next_step_after=wait_until,
    )


async def _research_once(session_factory: async_sessionmaker) -> bool:
    config = _config()
    if config is None:
        return False
    async with session_factory() as db:
        claim = await _claim(db, config)
    if claim is None:
        return False

    started = time.monotonic()
    message, exc = await call_model(claim.step.request or {})
    latency_ms = int((time.monotonic() - started) * 1000)
    state = copy.deepcopy(claim.state)
    try:
        outcome = await apply_result(
            state, claim.step, claim.lab, message=message, exc=exc, model=config.model,
            roster=claim.roster, hub_agent_ids=claim.hub_agent_ids,
        )
    except Exception as error:  # noqa: BLE001 — a bug here must still store the paid call
        logger.warning(
            "Projects: project=%s step=%s reply could not be applied (%s)",
            claim.project_id, claim.step.step, type(error).__name__,
        )
        # Counted, so a deterministic bug ends the session (or the choice) after the usual
        # limit instead of paying for the same step every RETRY_AFTER.
        state = copy.deepcopy(claim.state)
        entries = usage_entries(message) if message is not None else []
        served = served_by(message) if message is not None else None
        if claim.step.kind == STEP_RESEARCH:
            outcome = _counted_failure(
                state["sessions"][claim.step.index], "internal_error", entries, served,
            )
        else:
            outcome = _failed_choice(
                state, ["the previous attempt could not be applied (internal_error)"],
                "internal_error", entries, served, wait=RETRY_AFTER,
            )

    async with session_factory() as db:
        moved = await _store(db, claim, state, outcome, latency_ms)
        await db.commit()
    # Ids, statuses, models, tokens and latency — never candidate or plan text.
    billed = [e for e in outcome.entries if e.get("billed", True) and not e.get("partial")]
    logger.info(
        "Projects: project=%s step=%s call=%s error=%s served_by=%s stored=%s end=%s "
        "ready=%s input=%s output=%s latency_ms=%d",
        claim.project_id, claim.step.step, outcome.call_status, outcome.error_code,
        outcome.served_by_model, moved, outcome.end_reason, outcome.choice is not None,
        sum(int(e.get("input_tokens") or 0) + int(e.get("cache_read_input_tokens") or 0)
            + int(e.get("cache_creation_input_tokens") or 0) for e in billed),
        sum(int(e.get("output_tokens") or 0) for e in billed),
        latency_ms,
    )
    return True


async def research_projects(session_factory: async_sessionmaker) -> bool:
    """Take at most one model call's step of the live run's oldest due project; True when
    a call was made (so the worker polls for a job before sleeping). Every refusal to
    spend — the switch, an unpriced model, a missing prompt, no live run or its Projects
    session not active, nothing due — returns False. Never raises: the worker loop's ``try`` also
    holds the stale-processing sweep, so any error is one WARNING and False."""
    try:
        return await _research_once(session_factory)
    except Exception as exc:  # noqa: BLE001 — see the docstring
        # By type only for database errors: their bound parameters carry research text.
        logger.warning(
            "Projects: the research step failed (%s)", type(exc).__name__,
            exc_info=not isinstance(exc, SQLAlchemyError),
        )
        return False


async def _research_lane(
    session_factory: async_sessionmaker, deadline: float,
    should_continue: Callable[[], bool] | None,
) -> bool:
    made_call = False
    while (should_continue is None or should_continue()) and await research_projects(
        session_factory
    ):
        made_call = True
        if time.monotonic() >= deadline:
            break
    return made_call


async def research_round(
    session_factory: async_sessionmaker,
    slots: int | None = None,
    should_continue: Callable[[], bool] | None = None,
) -> bool:
    """Run ``slots`` lanes at once (default ``project_research_concurrency``, clamped to
    1..``RESEARCH_MAX_LANES``). Each lane takes steps (``research_projects``) back to back
    until one makes no call, ``RESEARCH_ROUND_SECONDS`` have passed since the round began, or
    ``should_continue`` (checked before each step; the worker passes its shutdown flag)
    returns False. True when any lane made a call. Never raises: a lane that raises ends
    alone and counts as making no call."""
    try:
        if slots is None:
            slots = get_settings().project_research_concurrency
        slots = min(max(1, int(slots)), RESEARCH_MAX_LANES)
    except Exception as exc:  # noqa: BLE001 — see the docstring
        logger.warning("Projects: research concurrency unreadable (%s)", type(exc).__name__)
        return False
    deadline = time.monotonic() + RESEARCH_ROUND_SECONDS
    results = await asyncio.gather(
        *(_research_lane(session_factory, deadline, should_continue) for _ in range(slots)),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            logger.warning("Projects: a research lane failed (%s)", type(result).__name__)
    return any(result is True for result in results)
