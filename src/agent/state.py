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
