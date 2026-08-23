"""Per-agent state dataclasses for the turn-based simulation."""

import time
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
    empty_response_count: int = 0  # consecutive empty/unparseable Phase 4 replies
    # Consecutive Phase 4 replies that were composed but never reached Slack
    # (_post_message returned None — e.g. the text stripped to empty once its own
    # sidecar/tag stripping ran). Distinct from empty_response_count: there the
    # MODEL produced nothing, here it did and the POST was dropped. Without a
    # counter this retried forever at one Opus call per turn, since a suppressed
    # post writes no log row and so never advances message_count toward the
    # max_thread_messages close either.
    suppressed_post_count: int = 0
    # Cohort gate: True when `other_agent_id` is no longer a permitted sender for
    # the owning agent (membership changed, or — on every resumed run — the DB
    # state rebuild reconstructed the thread before the first gate recompute).
    # A grandfathered thread still gets Phase 4 replies so the conversation can
    # conclude, but it is barred from the reactive-priority tier so it cannot
    # outrank gate-compliant work. Cleared if the partner becomes permitted again.
    # See .notes/cohort-system-v2.md §8.
    grandfathered: bool = False
    # Whether the specialist floor (SimulationEngine._specialist_floor_gap) has
    # seen the GLOBAL SimulationEngine._specialist_consults map be non-empty at
    # some point during this thread's life. Initialized at activation (the
    # four ThreadState(...) construction sites) to whatever the map held at
    # that moment, then MONOTONICALLY LATCHED at the top of every
    # `_reply_to_thread` turn on this thread (`floor_armed = floor_armed or
    # bool(_specialist_consults)`) — never reset, only ever flipped False ->
    # True.
    #
    # Deliberately NOT a live read of the global map at persist time, and
    # deliberately NOT frozen forever at whatever activation saw either:
    #   - A pure live read at persist time (the original bug) lets a
    #     DIFFERENT interview's consult, landing mid-await in some other
    #     task's turn, retroactively arm the floor for an in-flight verdict
    #     that began under fail-open. When that bug was found this refused the
    #     verdict after the concluding reply was already posted to Slack, with
    #     no later turn to recover it; the floor no longer refuses anything, so
    #     what it costs today is a `panel_incomplete=True` flag pinned on an
    #     interview whose panel may well have been convened — a wrong number in
    #     the one metric this instrumentation exists to report.
    #   - Freezing forever at activation (this field's first shape) instead
    #     made a thread that activated while the map was empty permanently
    #     unable to arm even once ITS OWN later specialist consults made the
    #     global map non-empty — silently exempting an under-vetted verdict.
    #     It also made every `_rebuild_agent_state`-restored thread (always
    #     constructed with floor_armed=False) permanently unenforceable no
    #     matter how many consults the restarted process went on to record.
    # The per-turn re-latch fixes both: it re-reads the global map once per
    # turn, at a point before that turn's own `await`s run, so the value used
    # at persist time (later in that same turn) cannot be changed by another
    # task's concurrent write — but a LATER turn on this same thread gets a
    # fresh chance to see the map having become non-empty since.
    #
    # Re-latched on the GLOBAL map, never on this PI's own consulted domains:
    # an earlier version of the floor failed open whenever the SUBJECT had no
    # consults, which quietly excused the commonest failure of all — a hub
    # that simply never convenes a panel. See _specialist_floor_gap's
    # docstring for that history and the full fail-open rationale.
    #
    # False here is NOT "the panel was skipped" — it is "this process cannot
    # tell", which is the ordinary state of every thread rebuilt after a
    # restart (production's last exit was a SIGKILL). That is why the row
    # written under an unarmed floor records `missing_domains=[]`, unverified,
    # rather than the NULL that means verified-complete — see
    # SimulationEngine._floor_verifiable and the column's own comment.
    floor_armed: bool = False


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
    # Anchored at CONSTRUCTION, not at the epoch, and not by the caller.
    #
    # `SimulationEngine._select_agent` weights on
    # `max(now - last_selected, 1.0) * load`, so a `0.0` default scores a
    # never-selected agent at ~1.79e9 against ~187 for one selected a tick ago
    # — a 10^7 ratio that turns a weighted random draw into a shuffle WITHOUT
    # replacement. `start()` used to paper over this with a one-shot loop over
    # `self.agents`, which covered the startup roster and nothing else:
    # `_sync_roster_from_db`'s ADD path constructs `Agent(...)` mid-run and
    # never anchored it, so 3 new agents out of 13 took 100% of 2,000 draws on
    # a harness, and in production (where the loop re-anchors on every
    # selection) N additions take N CONSECUTIVE turns — 48 of them for the
    # documented bulk provisioning case.
    #
    # Safe as a default because only RELATIVE staleness matters and every agent
    # in one startup roster is constructed within milliseconds of the others.
    # It also makes a brand-new agent the LEAST stale rather than the most,
    # which is the right way round: it has no backlog to catch up on.
    last_selected: float = field(default_factory=time.time)
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

    # True for the duration of this agent's post-lane turn (_run_post_turn),
    # excluded from selection while set (_turn_eligible). Replaces
    # _last_llm_caller's crude "was this the last caller" back-to-back guard
    # with a precise "is this specific agent's turn actively running" check —
    # a no-op today (the post lane is strictly sequential, so the previous
    # turn always finishes before the next selection), but load-bearing once
    # loop iterations can overlap. Reset in a finally so an exception mid-turn
    # cannot strand an agent permanently ineligible.
    in_flight: bool = False
