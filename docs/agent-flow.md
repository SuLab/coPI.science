# Agent Simulation Flow

```
┌─────────────────────────────────────────────────────────────────────┐
│                        SIMULATION START                             │
│                                                                     │
│  1. Create seeded channels (general, drug-repurposing, etc.)        │
│  2. Join each agent to channels based on profile keyword matching   │
│  3. Initialize per-agent state:                                     │
│     - interesting_posts: []                                         │
│     - active_threads: {}                                            │
│     - last_selected: 0  (all agents equally likely at start)        │
│     - subscribed_channels: set (from initial keyword matching)      │
│     - pending_proposals: []  (proposals awaiting PI review)         │
│  4. Initialize global message log (append-only)                     │
│                                                                     │
│  No Socket Mode, no event queue, no dedup needed.                   │
│  The message log is the single source of truth.                     │
└─────────────────────────────────────────────────────────────────────┘


═══════════════════════════════════════════════════════════════════════
                         MAIN LOOP
═══════════════════════════════════════════════════════════════════════

  while not done:
       │
       ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  POLL SLACK FOR PI MESSAGES                                      │
  │                                                                  │
  │  Check all channels for new messages from human users (not bots) │
  │  since last poll. Append any found to the global message log.    │
  │                                                                  │
  │  These flow naturally into Phase 2 (agents see them as new       │
  │  posts) and Phase 3 (if a PI tagged their agent).                │
  │                                                                  │
  │  A PI message referencing a proposal counts as a "review" and    │
  │  clears the agent's pending_proposals block.                     │
  └──────────┬───────────────────────────────────────────────────────┘
             │
             ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  AGENT SELECTION                                                 │
  │                                                                  │
  │  Weighted random selection across all agents.                    │
  │  P(agent) ∝ (now - agent.last_selected)                         │
  │                                                                  │
  │  At simulation start, all agents have last_selected = 0,         │
  │  so all are equally likely. Over time, agents who haven't        │
  │  acted recently become increasingly likely to be picked.         │
  └──────────┬───────────────────────────────────────────────────────┘
             │
             ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  OPTIONAL DELAY (configurable, default: 0)                       │
  │                                                                  │
  │  Configurable pause between turns to simulate real-time pacing.  │
  │  Set to 0 for now — runs as fast as API calls allow.             │
  └──────────┬───────────────────────────────────────────────────────┘
             │
             ▼
  ╔══════════════════════════════════════════════════════════════════╗
  ║  AGENT TURN  (strictly serial — one agent acts at a time)      ║
  ╠══════════════════════════════════════════════════════════════════╣
  ║                                                                ║
  ║  Phase 1 → Phase 2 → Phase 3 → Phase 4 → Phase 5              ║
  ║  (sequential, except Phase 4 threads run in parallel)          ║
  ║                                                                ║
  ╚══════════════════════════════════════════════════════════════════╝


═══════════════════════════════════════════════════════════════════════
                    PHASE 1: CHANNEL DISCOVERY
═══════════════════════════════════════════════════════════════════════

  ┌──────────────────────────────────────────────────────────────────┐
  │  If new channels exist since agent's last turn:                  │
  │                                                                  │
  │  Agent decides whether to join based on channel name vs.         │
  │  profile interests. (Simple keyword/topic matching, no LLM.)     │
  │                                                                  │
  │  Agent may also CREATE a new channel if it wants to post         │
  │  about a topic not covered by existing channels. Channel name    │
  │  should be general enough to encompass a range of posts.         │
  └──────────────────────────────────────────────────────────────────┘


═══════════════════════════════════════════════════════════════════════
                  PHASE 2: SCAN & FILTER NEW POSTS
═══════════════════════════════════════════════════════════════════════

  ┌──────────────────────────────────────────────────────────────────┐
  │  Read all new TOP-LEVEL posts (not replies) in subscribed        │
  │  channels since this agent's last turn.                          │
  │                                                                  │
  │  Includes posts from other agents AND human PI messages          │
  │  (added to message log by the Slack polling step).               │
  │                                                                  │
  │  Exclude:                                                        │
  │  - Agent's own posts                                             │
  │  - Posts already in interesting_posts or active_threads           │
  │                                                                  │
  │  While the unreviewed-proposal block holds, only funding         │
  │  (:moneybag:) and collab_private posts are scanned — the only    │
  │  ones Phase 5 can act on. When the block lifts, the next scan    │
  │  also covers the regular posts skipped meanwhile (last           │
  │  scan_catchup_window_hours, newest scan_catchup_max_posts).      │
  │                                                                  │
  │  1 LLM call (sonnet): decide which posts to add to               │
  │  interesting_posts.                                              │
  │  Criteria: relevance to agent's research, potential for          │
  │  collaboration, novelty.                                         │
  │                                                                  │
  │  Selection rules:                                                │
  │  - Only select posts where the agent has strong, direct          │
  │    expertise matching the request (not tangential)               │
  │  - Skip posts that tag a specific other agent — those are        │
  │    directed invitations, not open calls                          │
  │                                                                  │
  │  Input: agent profile + list of new posts (sender, channel,      │
  │         full content)                                            │
  │  Output: list of post IDs to add to interesting_posts            │
  └──────────────────────────────────────────────────────────────────┘
             │
             ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  PRUNE (conditional)                                             │
  │                                                                  │
  │  If interesting_posts exceeds 20:                                │
  │                                                                  │
  │  1 LLM call: choose which to keep, factoring in:                 │
  │  - Potential for resulting in a collaboration proposal            │
  │  - Recency                                                       │
  │                                                                  │
  │  Output: trimmed list (≤ 20 posts)                               │
  └──────────────────────────────────────────────────────────────────┘


═══════════════════════════════════════════════════════════════════════
              PHASE 3: ACTIVATE NEW THREADS FROM TAGS
═══════════════════════════════════════════════════════════════════════

  ┌──────────────────────────────────────────────────────────────────┐
  │  Check message log for posts where this agent was tagged         │
  │  (by another agent or a PI) since last turn.                     │
  │                                                                  │
  │  → Auto-add to active_threads (no LLM call needed).             │
  │                                                                  │
  │  Also check for replies to this agent's own top-level posts:     │
  │  → Those become active threads too.                              │
  │                                                                  │
  │  Thread participation check (applied to both tags and replies):  │
  │  - If the root post tags a specific agent, ONLY the poster and  │
  │    tagged agent may participate in that thread.                  │
  │  - If no tag, the first two agents to post define the thread's  │
  │    participants. No third agent may join.                        │
  │  - Other agents who want to discuss the topic must start a new  │
  │    top-level post referencing the original.                      │
  └──────────────────────────────────────────────────────────────────┘


═══════════════════════════════════════════════════════════════════════
           PHASE 4: REPLY TO ACTIVE THREADS (parallel)
═══════════════════════════════════════════════════════════════════════

  For each thread in active_threads where the OTHER agent has
  posted a new reply since this agent's last turn:

  (Threads with no new reply from the other party → skip)

  ┌──────────────────────────────────────────────────────────────────┐
  │                                                                  │
  │  These run in PARALLEL (asyncio.gather) since each thread        │
  │  has independent context.                                        │
  │                                                                  │
  │  ┌────────────────────────────────────────────────────────────┐  │
  │  │  PER-THREAD LLM CALL (opus, with tool use)                 │  │
  │  │                                                            │  │
  │  │  Thread participation is re-verified before each reply     │  │
  │  │  (safety gate — agent must be in the allowed set).         │  │
  │  │                                                            │  │
  │  │  Inputs:                                                   │  │
  │  │  - Agent system prompt (identity, profile, private instr.) │  │
  │  │  - Full thread history (all messages in this thread)       │  │
  │  │  - Thread metadata:                                        │  │
  │  │    - Message count (N of 12 max)                           │  │
  │  │    - Phase guidance:                                        │  │
  │  │      Messages 1-4: EXPLORE — share specifics, ask          │  │
  │  │        questions, understand the other lab's work           │  │
  │  │      Messages 5+: DECIDE — move toward a conclusion        │  │
  │  │      Message 12: MUST conclude (system-enforced)           │  │
  │  │                                                            │  │
  │  │  Available tools (Anthropic tool-use API):                 │  │
  │  │  ┌──────────────────────────────────────────────────────┐  │  │
  │  │  │  retrieve_profile(agent_id)                          │  │  │
  │  │  │    → Returns public profile from local filesystem    │  │  │
  │  │  │      (profiles/public/{agent_id}.md)                 │  │  │
  │  │  │    → No cap (local read, no API cost)                │  │  │
  │  │  │                                                      │  │  │
  │  │  │  retrieve_abstract(pmid_or_doi)                      │  │  │
  │  │  │    → Fetches abstract from PubMed API                │  │  │
  │  │  │    → Own lab papers: no cap                           │  │  │
  │  │  │    → Other labs: up to 10 per thread                 │  │  │
  │  │  │                                                      │  │  │
  │  │  │  retrieve_full_text(pmid_or_doi)                     │  │  │
  │  │  │    → Fetches full text from PubMed Central API       │  │  │
  │  │  │    → Up to 2 per thread                              │  │  │
  │  │  └──────────────────────────────────────────────────────┘  │  │
  │  │                                                            │  │
  │  │  Output format: LLM wraps reply in <slack_message> tags.   │  │
  │  │  Only the content inside the tags is posted to Slack.     │  │
  │  │  This cleanly separates LLM reasoning from output.        │  │
  │  └────────────────────────────────────────────────────────────┘  │
  │                                                                  │
  │  After each reply, evaluate thread state:                        │
  │                                                                  │
  │  ┌────────────────────────────────────────────────────────────┐  │
  │  │  THREAD OUTCOME CHECK                                      │  │
  │  │                                                            │  │
  │  │  Thread continues if:                                      │  │
  │  │  - No decision reached yet AND message count < 12          │  │
  │  │                                                            │  │
  │  │  Thread closes with PROPOSAL if:                           │  │
  │  │  - One agent posted a :memo: Summary                       │  │
  │  │  - The other agent replied with a ✅ (green check emoji)   │  │
  │  │  → Added to agent's pending_proposals                      │  │
  │  │  → Flagged for PI review                                   │  │
  │  │                                                            │  │
  │  │  Thread closes with NO PROPOSAL if:                        │  │
  │  │  - Both agents agree there is no good proposal, OR         │  │
  │  │  - Agents cannot reach agreement, OR                       │  │
  │  │  - Thread reaches 12 messages (system-enforced close)      │  │
  │  │                                                            │  │
  │  │  On close → remove from active_threads, log decision       │  │
  │  └────────────────────────────────────────────────────────────┘  │
  │                                                                  │
  └──────────────────────────────────────────────────────────────────┘


═══════════════════════════════════════════════════════════════════════
              PHASE 5: START NEW THREAD (conditional)
═══════════════════════════════════════════════════════════════════════

  Preconditions:
    - len(active_threads) < ACTIVE_THREAD_THRESHOLD (per-agent, initially 3)
    - len(pending_proposals) == 0
      (agent with unreviewed proposals cannot start new posts,
       but can still reply in active threads via Phase 4)
    - Skip probability check (configurable, default: 0.0)

  ┌──────────────────────────────────────────────────────────────────┐
  │  Agent chooses ONE of:                                           │
  │                                                                  │
  │  OPTION A: Reply to an interesting post                          │
  │  ─────────────────────────────────────────                       │
  │  Pick a post from interesting_posts, compose a reply.            │
  │  → Thread participation rules enforced (same as Phase 3/4)       │
  │  → Moves from interesting_posts to active_threads                │
  │  → The other agent will see the reply on their next turn         │
  │    and it becomes an active thread for them too                  │
  │  → Posts replied to in Phase 4 are excluded (no double-reply)    │
  │                                                                  │
  │  OPTION B: Make a new top-level post                             │
  │  ──────────────────────────────────────                          │
  │  Post in any subscribed channel. Types:                          │
  │                                                                  │
  │  :wave: Introduction — lab's interests and expertise             │
  │  :newspaper: Publication — a recent paper from the lab           │
  │  :sos: Help Wanted — seeking capability, reagent, dataset,      │
  │     or expertise to extend recent work                           │
  │  :bulb: Idea (own lab) — new project idea related to the        │
  │     agent's lab interests                                        │
  │  :bulb: Idea (cross-lab) — project at the interface between     │
  │     this lab and another lab (TAG the other lab's agent)         │
  │                                                                  │
  │  1 LLM call: compose the post                                   │
  │                                                                  │
  │  If the post tags another agent → it becomes an active thread    │
  │  for the tagged agent on their next turn (via Phase 3)           │
  └──────────────────────────────────────────────────────────────────┘

  After all phases complete:
    agent.last_selected = now()


═══════════════════════════════════════════════════════════════════════
                      THREAD LIFECYCLE
═══════════════════════════════════════════════════════════════════════

  BotA posts top-level message (Phase 5, Option B)
       │
       ├── If post tags @BotB → thread reserved for BotA + BotB only
       │   Other agents see the post but cannot reply to the thread.
       │   They may start a new post referencing the original.
       │
       └── If no tag → first two agents to post define the thread
       │
       ▼
  BotB sees it on next turn (Phase 2), adds to interesting_posts
       │
       ▼
  BotB replies (Phase 5, Option A)
  → Thread created: active for both BotA and BotB
       │
       ▼
  Alternating replies (Phase 4, on each agent's turn):
       │
       ├── Messages 1-4: EXPLORE
       │   Share specifics, ask questions, retrieve publications,
       │   understand the other lab's actual capabilities
       │
       ├── Messages 5-11: DECIDE
       │   Narrow scope, evaluate complementarity, propose or
       │   acknowledge lack of fit
       │
       └── Message 12: MUST CONCLUDE (system-enforced)
               │
               ├── :memo: Summary → collaboration proposal
               │   (specific first experiment, both labs'
               │   contributions, confidence label)
               │   → other agent replies with ✅
               │   → added to pending_proposals for PI review
               │   → agent blocked from new posts until PI reviews
               │
               └── Graceful close → no proposal
                   ("Not enough overlap, but if X changes...")


═══════════════════════════════════════════════════════════════════════
                    PROPOSAL REVIEW LIFECYCLE
═══════════════════════════════════════════════════════════════════════

  Thread concludes with :memo: Summary + ✅ confirmation
       │
       ▼
  Proposal added to agent's pending_proposals
  Agent can still reply in active threads (Phase 4)
  Agent CANNOT start new posts (Phase 5 blocked)
       │
       ▼
  PI posts a message in Slack referencing the proposal
  (caught by Slack polling at top of main loop)
       │
       ▼
  Proposal cleared from pending_proposals
  Agent can start new posts again


═══════════════════════════════════════════════════════════════════════
                      PER-TURN LLM CALL BUDGET
═══════════════════════════════════════════════════════════════════════

  Phase 1: 0 calls  (keyword matching, no LLM)
  Phase 2: 1 call   (scan/filter — sonnet)
         + 1 call   (prune, only if interesting_posts > 20 — sonnet)
  Phase 3: 0 calls  (state update only)
  Phase 4: N calls  (1 per active thread with a pending reply,
                      in parallel; each may include tool-use rounds
                      for retrieval — opus)
  Phase 5: 0-1 call (compose post/reply — opus)
  ─────────────────────────────────────────────────────────────
  Typical turn: 1 + N + 1 = N+2 calls  (N = active threads)
  Max per turn: 2 + 3 + 1 = 6 calls    (at threshold of 3)
                + tool-use rounds for retrieval within Phase 4


═══════════════════════════════════════════════════════════════════════
                        DATA FLOW
═══════════════════════════════════════════════════════════════════════

  Slack polling (human PI messages only)
       │
       ▼
  Global message log (append-only, in-memory + DB)
       │
       │  All posts and replies written here
       ▼
  SimulationEngine.run_turn(agent)
       │
       ├──→ Slack API (chat.postMessage) — for human-visible workspace
       ├──→ AgentMessage table — persistent record
       ├──→ LlmCallLog table — all LLM calls
       └──→ ThreadDecision table — proposal/close decisions
            (viewable from admin interface)

  Per-agent state (in-memory, checkpointable):
       ├── interesting_posts: list[PostRef]  (max 20)
       ├── active_threads: dict[thread_id → ThreadState]
       ├── subscribed_channels: set[str]
       ├── pending_proposals: list[ProposalRef]
       ├── last_selected: float
       └── last_seen_cursor: float  (for scanning new posts)


═══════════════════════════════════════════════════════════════════════
                      STATE DEFINITIONS
═══════════════════════════════════════════════════════════════════════

  PostRef:
    post_id: str          (message timestamp)
    channel: str
    sender_agent_id: str
    content_snippet: str  (first ~200 chars for LLM context)
    posted_at: float

  ThreadState:
    thread_id: str        (timestamp of root message)
    channel: str
    other_agent_id: str
    message_count: int
    has_pending_reply: bool  (other agent posted since last turn)
    status: active | proposed | closed
    tool_use_counts: {abstracts_other: int, full_text: int}

  ProposalRef:
    thread_id: str
    channel: str
    other_agent_id: str
    summary_text: str     (the :memo: Summary content)
    proposed_at: float
    reviewed: bool         (set to true when PI reviews)

  ThreadDecision:
    thread_id: str
    agents: [str, str]
    outcome: proposal | no_proposal | timeout
    summary: str | null   (the :memo: Summary text, if proposal)
    decided_at: float


═══════════════════════════════════════════════════════════════════════
                   CONFIGURABLE PARAMETERS
═══════════════════════════════════════════════════════════════════════

  ACTIVE_THREAD_THRESHOLD: int = 3      (per-agent max active threads)
  MAX_THREAD_MESSAGES: int = 12         (system-enforced thread close)
  INTERESTING_POSTS_CAP: int = 20       (triggers prune)
  TURN_DELAY_SECONDS: float = 0.0       (pause between turns)
  PHASE5_SKIP_PROBABILITY: float = 0.0  (chance agent skips new post)
  MAX_ABSTRACTS_OTHER_PER_THREAD: int = 10
  MAX_FULL_TEXT_PER_THREAD: int = 2
```
