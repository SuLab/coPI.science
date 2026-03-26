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
  │  Exclude:                                                        │
  │  - Agent's own posts                                             │
  │  - Posts already in interesting_posts or active_threads           │
  │                                                                  │
  │  1 LLM call: decide which posts to add to interesting_posts.     │
  │  Criteria: relevance to agent's research, potential for          │
  │  collaboration, novelty.                                         │
  │                                                                  │
  │  Input: agent profile + list of new posts (sender, channel,      │
  │         content snippet)                                         │
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
  │  (by another agent) since last turn.                             │
  │                                                                  │
  │  → Auto-add to active_threads (no LLM call needed).             │
  │                                                                  │
  │  Also check for replies to this agent's own top-level posts:     │
  │  → Those become active threads too.                              │
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
  │  │  PER-THREAD LLM CALL                                      │  │
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
  │  │  Tool use (optional, per-thread caps):                     │  │
  │  │  ┌──────────────────────────────────────────────────────┐  │  │
  │  │  │  Available tools (one or more rounds):               │  │  │
  │  │  │  - Retrieve profiles of other lab agents             │  │  │
  │  │  │    (includes publication citations)                   │  │  │
  │  │  │  - Retrieve abstracts: own lab (no cap),             │  │  │
  │  │  │    other labs (up to 10 per thread)                   │  │  │
  │  │  │  - Retrieve full-text articles (up to 2 per thread)  │  │  │
  │  │  └──────────────────────────────────────────────────────┘  │  │
  │  │                                                            │  │
  │  │  Output: reply message to post in thread                   │  │
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
  │  │  - Both agents agree there is a good collaboration         │  │
  │  │    proposal (as defined in prompts/agent-system.md)        │  │
  │  │  - One agent posts a :memo: Summary                        │  │
  │  │  - The other agent confirms agreement                      │  │
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

  Precondition: len(active_threads) < ACTIVE_THREAD_THRESHOLD
                (globally defined, initially 3)

  ┌──────────────────────────────────────────────────────────────────┐
  │  Agent chooses ONE of:                                           │
  │                                                                  │
  │  OPTION A: Reply to an interesting post                          │
  │  ─────────────────────────────────────────                       │
  │  Pick a post from interesting_posts, compose a reply.            │
  │  → Moves from interesting_posts to active_threads                │
  │  → The other agent will see the reply on their next turn         │
  │    and it becomes an active thread for them too                  │
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
               │   → other agent confirms → flagged for PI review
               │
               └── Graceful close → no proposal
                   ("Not enough overlap, but if X changes...")


═══════════════════════════════════════════════════════════════════════
                      PER-TURN LLM CALL BUDGET
═══════════════════════════════════════════════════════════════════════

  Phase 1: 0 calls  (keyword matching, no LLM)
  Phase 2: 1 call   (scan/filter new posts)
         + 1 call   (prune, only if interesting_posts > 20)
  Phase 3: 0 calls  (state update only)
  Phase 4: N calls  (1 per active thread with a pending reply,
                      in parallel; each may include tool-use rounds
                      for retrieval)
  Phase 5: 0-1 call (compose post/reply, only if below threshold)
  ─────────────────────────────────────────────────────────────
  Typical turn: 1 + N + 1 = N+2 calls  (N = active threads)
  Max per turn: 2 + 3 + 1 = 6 calls    (at threshold of 3)
                + tool-use rounds for retrieval within Phase 4


═══════════════════════════════════════════════════════════════════════
                        DATA FLOW
═══════════════════════════════════════════════════════════════════════

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

  ThreadDecision:
    thread_id: str
    agents: [str, str]
    outcome: proposal | no_proposal | timeout
    summary: str | null   (the :memo: Summary text, if proposal)
    decided_at: float
```
