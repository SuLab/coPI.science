"""Per-agent state dataclasses for the turn-based simulation."""

from dataclasses import dataclass, field


@dataclass
class PostRef:
    """Reference to a top-level post in the message log."""

    post_id: str  # message timestamp (Slack ts)
    channel: str
    sender_agent_id: str
    content_snippet: str  # first ~200 chars for LLM context
    posted_at: float
    pi_priority: bool = False  # PI tagged this for engagement
    pi_context: str | None = None  # PI's comment when tagging
    foa_number: str | None = None  # FOA number extracted from funding posts


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
    pi_context: str | None = None  # PI posted in this thread — their message
    message_count_offset: int = 0  # subtract from message_count for PI-reopened threads
    foa_number: str | None = None  # FOA number for funding threads
    funding_reject_count: int = 0  # drafts rejected by funding-rules validators


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

    interesting_posts: list[PostRef] = field(default_factory=list)
    active_threads: dict[str, ThreadState] = field(default_factory=dict)  # thread_id -> ThreadState
    subscribed_channels: set[str] = field(default_factory=set)
    pending_proposals: list[ProposalRef] = field(default_factory=list)
    last_selected: float = 0.0
    last_seen_cursor: float = 0.0  # for scanning new posts since last turn

    # Phase 5 throttling (state-change gate + skip backoff)
    consecutive_phase5_skips: int = 0
    last_phase5_action_time: float = 0.0  # last time Phase 5 produced a real post
    has_pi_directive: bool = False  # set when PI sends a message, cleared after Phase 5
