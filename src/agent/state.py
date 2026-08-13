"""Per-agent state dataclasses for the turn-based simulation."""

from collections import deque
from dataclasses import dataclass, field


@dataclass
class ThreadState:
    """Tracks an active thread between two agents."""

    thread_id: str  # timestamp of root message
    channel: str
    other_agent_id: str
    message_count: int = 0
    has_pending_reply: bool = False  # other agent posted since last turn
    status: str = "active"  # active | proposed | closed
    abstracts_other: int = 0  # tool-use counters
    full_text: int = 0
    # message_count_offset: had one writer, SimulationEngine
    # ._sync_proposal_reviews_from_db's web-guidance reopen path, which the
    # reply-only-hub reconciliation task deleted (nothing on this branch
    # creates or reviews proposals anymore, so there was nothing left to sync
    # a web review of). It is now permanently at its dataclass default (0) —
    # no code writes it anymore — but is kept because it is still read
    # generically at simulation.py:_reply_to_thread's message-count
    # computation (a no-op subtraction of 0 now, harmless). Its sibling field
    # `pi_context` had the same writer and was read nowhere at all once
    # Agent._compose_system_prompt's phase-4 injection that used to read it
    # was removed alongside the live PI-Slack/DM paths — it was dropped
    # outright in the 2026-08-12 removal-cycle consolidation sweep.
    message_count_offset: int = 0  # subtract from message_count for PI-reopened threads
    empty_response_count: int = 0  # consecutive empty/unparseable Phase 4 replies
    # Cohort gate: True when `other_agent_id` is no longer a permitted sender for
    # the owning agent (membership changed, or — on every resumed run — the DB
    # state rebuild reconstructed the thread before the first gate recompute).
    # A grandfathered thread still gets Phase 4 replies so the conversation can
    # conclude, but it is barred from the reactive-priority tier so it cannot
    # outrank gate-compliant work. Cleared if the partner becomes permitted again.
    # See .notes/cohort-system-v2.md §8.
    grandfathered: bool = False


@dataclass
class ProposalRef:
    """A collaboration proposal awaiting PI review."""

    thread_id: str
    channel: str
    other_agent_id: str
    summary_text: str  # the :memo: Summary content
    proposed_at: float
    reviewed: bool = False


@dataclass
class AgentState:
    """Full mutable state for one agent during a simulation."""

    active_threads: dict[str, ThreadState] = field(default_factory=dict)  # thread_id -> ThreadState
    subscribed_channels: set[str] = field(default_factory=set)
    pending_proposals: list[ProposalRef] = field(default_factory=list)
    last_selected: float = 0.0
    last_seen_cursor: float = 0.0  # for scanning new posts since last turn

    # Sliding-window LLM call ledger, maintained by Agent.record_api_call.
    # Distinct from Agent.api_call_count on purpose: api_call_count is LIFETIME
    # accounting (it feeds the run summary and SimulationRun.total_api_calls),
    # while call_times is the LIVE throttle and its entries age out. Only the
    # latter gates eligibility, which is why throttling can no longer be
    # permanent. See docs/specs/2026-08-06-hub-budget-scheduler-design.md §4.2.
    call_times: deque[float] = field(default_factory=deque)

    # True while the agent is rate-limited. Tracked only so the transition into
    # throttling can be logged once instead of once per scheduler tick — a silent
    # throttle is what turned the original incident into a 2.5-hour undetected
    # outage. See design §6.
    throttled: bool = False

    # Phase 5 throttling (state-change gate + skip backoff)
    consecutive_phase5_skips: int = 0
    last_phase5_action_time: float = 0.0  # last time Phase 5 was evaluated (gates the spontaneous-post timer)
