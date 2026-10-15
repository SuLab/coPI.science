"""Engine glue for the authorship-claim check (the 2026-10-15 publication-handling design,
§11; contract C7).

The rules live in ``src.agent.authorship_rules``. This module adapts them to the engine:
which mode applies (``effective_mode``: a hub role is always ``log``), the roster and
records a check reads (``roster_entries``, ``record_for_agent``), the thread root used as
context (``root_context``), the rejection-feedback store the next draft reads
(``RejectionFeedback``, ``feedback_text``), and the row every check writes
(``record_check``, table ``authorship_claim_checks``).

It sits outside ``src/agent/engine/`` so that the engine import graph
(``tests/unit/test_engine_import_graph.py::ALLOWED``) gains no edge, and so the replay
script (``scripts/replay_authorship_claims.py``) can share ``root_context``.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from src.agent.mentions import RosterEntry
from src.agent.publication_records import LabPublicationRecord
from src.agent.role_capabilities import hub_role_names
from src.models.authorship_claim_check import (
    EXCERPT_MAX_CHARS,
    MODE_LOG,
    OUTCOME_GROUNDED,
    OUTCOME_NO_CLAIM,
    OUTCOME_REJECTED,
)
from src.visibility import VISIBILITY_COLLAB_PRIVATE

if TYPE_CHECKING:
    from src.agent.agent import Agent
    from src.agent.authorship_rules import AuthorshipVerdict, ClaimReason

logger = logging.getLogger(__name__)

#: The feedback and row key of a pitch, which has no thread (main AGT-14).
PHASE5_THREAD_KEY = "phase5"
#: Rejection reasons kept in memory, oldest evicted first (main AGT-14).
FEEDBACK_CAP = 1000
#: The reason code of a memory line the sweep would strip (R6.13).
MEMORY_REASON_CODE = "memory_ungrounded"
#: AGT-14's text, adapted: blackbird's persona heading is "Recent Publications"
#: (src/agent/persona_sections.py). Engine-injected guidance, like TRUNCATION_NOTICE, and
#: reachable only in enforce mode for pi_lab, so it lives here rather than under prompts/.
FEEDBACK_TEMPLATE = (
    "Your previous draft was not posted: {reason}. Cite the DOI or PubMed link from your "
    "Recent Publications section for any paper you describe as your lab's, or rephrase "
    "without claiming authorship."
)


def effective_mode(role: str | None, setting: str) -> str:
    """``"log"`` for a hub role whatever the setting (Q20); otherwise the setting."""
    return MODE_LOG if role in hub_role_names() else setting


def roster_entries(agents: Mapping[str, Agent]) -> tuple[RosterEntry, ...]:
    """The live agents as the rules' roster, sorted by agent id. A hub's registry
    ``pi_name`` is not a person's name, so it is left out."""
    hubs = hub_role_names()
    return tuple(
        RosterEntry(agent.agent_id, agent.bot_name, None if agent.role in hubs else agent.pi_name)
        for _, agent in sorted(agents.items())
    )


def record_for_agent(agent: Agent | None) -> LabPublicationRecord:
    """The lab's C12 record, or an empty one for an unknown agent and for a hub role
    (record-less, as main treats service bots)."""
    if agent is None or agent.role in hub_role_names():
        return LabPublicationRecord()
    return agent.publication_record()


def outcome_of(verdict: AuthorshipVerdict) -> str:
    if not verdict.has_claim:
        return OUTCOME_NO_CLAIM
    return OUTCOME_GROUNDED if verdict.ok else OUTCOME_REJECTED


def root_context(
    root_sender: str | None, root_content: str, following: Iterable[tuple[str | None, str]],
) -> str:
    """The thread root's text, with the root author's continuation chunks rejoined.

    ``following`` is the thread after the root, in order, as ``(sender, content)``. Items
    are joined while their sender is the root's, and the join stops at the first other
    sender (``SlackIO._post_message`` hangs a split root's continuation chunks off chunk 0
    under the same sender). A root with no sender is returned alone.
    """
    parts = [root_content]
    if root_sender is not None:
        for sender, content in following:
            if sender != root_sender:
                break
            parts.append(content)
    return "\n".join(parts)


def memory_thread_key(visibility: str, channel_id: str | None) -> str:
    """``"memory:<channel_id>"`` for a collab_private segment, else ``"memory:public"``."""
    if visibility == VISIBILITY_COLLAB_PRIVATE and channel_id:
        return f"memory:{channel_id}"
    return "memory:public"


def feedback_text(reason: str) -> str:
    return FEEDBACK_TEMPLATE.format(reason=reason)


class RejectionFeedback:
    """The latest rejection reason per ``(agent_id, thread_key)``, LRU-capped at
    ``FEEDBACK_CAP``. In memory only: a restart drops it."""

    def __init__(self, cap: int = FEEDBACK_CAP) -> None:
        self._cap = cap
        self._reasons: OrderedDict[tuple[str, str], str] = OrderedDict()

    def __len__(self) -> int:
        return len(self._reasons)

    def note(self, agent_id: str, thread_key: str, reason: str) -> None:
        key = (agent_id, thread_key)
        self._reasons[key] = reason
        self._reasons.move_to_end(key)
        while len(self._reasons) > self._cap:
            self._reasons.popitem(last=False)

    def get(self, agent_id: str, thread_key: str) -> str | None:
        return self._reasons.get((agent_id, thread_key))

    def clear(self, agent_id: str, thread_key: str) -> None:
        self._reasons.pop((agent_id, thread_key), None)

    def clear_thread(self, thread_key: str) -> None:
        for key in [k for k in self._reasons if k[1] == thread_key]:
            del self._reasons[key]


async def record_check(
    session_factory: Any,
    *,
    simulation_run_id: Any,
    agent_id: str,
    thread_key: str,
    phase: str,
    mode: str,
    outcome: str,
    reasons: Sequence[ClaimReason],
    excerpt: str,
) -> None:
    """Insert one ``authorship_claim_checks`` row and commit. Does nothing without a
    session factory or a run id (a DB-less engine). Never raises: a lost row must never
    block or break a post, so every failure (``UndefinedTable`` before migration 0070
    included) is a WARNING."""
    if session_factory is None or simulation_run_id is None:
        return
    try:
        from src.models.authorship_claim_check import AuthorshipClaimCheck

        async with session_factory() as db:
            db.add(AuthorshipClaimCheck(
                simulation_run_id=simulation_run_id,
                agent_id=agent_id,
                thread_key=thread_key[:64],
                phase=phase,
                mode=mode,
                outcome=outcome,
                reasons=[{"code": r.code, "text": r.text} for r in reasons],
                excerpt=excerpt[:EXCERPT_MAX_CHARS],
            ))
            await db.commit()
    except Exception as exc:  # noqa: BLE001 — see the docstring
        logger.warning(
            "Authorship claim check row not recorded for %s: %s",
            agent_id, f"{type(exc).__name__}: {exc}",
        )
