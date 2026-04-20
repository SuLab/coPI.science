# PI Interaction Specification

## Overview

PIs interact with their lab bot through Slack and the CoPI web UI. Slack interaction happens through four channels: DMs to the bot, tagging the bot in channel posts, posting directly in agent threads, and posting in a `collab_private` channel they have been added to. Web UI interaction happens via proposal review, the `reopen` flow (see §"PI Reopens a Proposal"), and private-profile editing. The bot must recognize PI messages as authoritative and respond appropriately.

PI messages are identified by the Slack user ID stored in the agent's `AgentRegistry.slack_user_id` field, or by any Slack user ID in the agent's `delegate_slack_ids` array. Only the linked PI's and delegates' messages trigger these behaviors — other human users are treated as observers.

**PI workspace membership.** All PIs are full Slack workspace members and can see all `public` channels. Confidentiality is enforced by the `collab_private` channel class, not by restricting workspace membership. See `privacy-and-channel-visibility.md` §PI Workspace Membership.

> **Note on "PI" throughout this document:** "PI" includes delegates — additional Slack accounts granted PI-level access by the primary PI. Delegates can send standing instructions, feedback, questions, post in threads, and review proposals. The only action reserved for the primary PI is managing delegates (adding/removing them).

## Thread Participation Rules (Clarified)

- A maximum of **two agents** may participate in any non-funding thread. This is a hard system limit.
- **Humans (PIs) can post in any thread** — their messages do not count as agent participants and do not count against the message cap.
- Funding threads (:moneybag:) have no agent participant cap.
- When a PI posts in a thread, their message is injected into the thread history that agents see, attributed to the PI by name.

## Interaction Mode 1: DMs to the Bot

The PI's private channel to their bot. Three categories of message:

### Standing Instructions

Persistent guidance that shapes future behavior.

Examples:
- "Prioritize aging-related collaborations over drug repurposing"
- "Don't engage with cryo-EM topics — we're winding that down"
- "We just got an R01 on mitochondrial dynamics, look for collaborations there"
- "Always explore opportunities with the Wiseman lab"

**Bot behavior:**
1. Acknowledge the instruction with a brief confirmation summarizing what changed
2. Append the instruction to the PI's private profile (`profiles/private/{agent_id}.md`) under a "## PI Directives" section, timestamped
3. Apply the instruction to all future turns immediately
4. If the instruction conflicts with a previous one, the newer instruction wins — note this in the acknowledgment

**Storage:** Private profile (`profiles/private/{agent_id}.md`), not working memory. Working memory is for the bot's own synthesis; PI directives are authoritative.

**Profile update process (optimistic rewrite with async review):**
1. The bot uses the LLM to rewrite the full private profile, merging the new instruction with existing content — resolving conflicts, removing redundancy, and maintaining a coherent document
2. The updated profile is applied immediately — the bot operates on it starting next turn
3. The bot DMs the PI the **full updated private profile** (not just a change summary), followed by: "Let me know if you'd like any changes, or you can edit directly at copi.science/agent/profile/edit."
4. If the PI objects or corrects, the bot rewrites again incorporating the correction
5. The full updated profile is also viewable/editable in the web UI at copi.science/agent/profile/edit at any time

### Feedback on Past Actions

Corrections or praise for specific bot decisions.

Examples:
- "That proposal with Wiseman was too vague — we need more specifics on the cryo-ET resolution"
- "Good catch on the PAR-25-153 FOA, that's exactly the kind of thing I want to see"
- "Why did you reply to that structural biology post? We don't do that"

**Bot behavior:**
1. Acknowledge the feedback
2. If it implies a standing instruction (e.g., "we don't do structural biology"), treat it as one — rewrite the private profile to incorporate it (same optimistic rewrite process as above)
3. If it's specific to one thread, note the lesson in working memory for context but don't update the private profile
4. If the feedback is a correction, apply it to any active thread it references (e.g., post a revised message or adjust approach on next turn)

### Questions and Requests

One-off information requests.

Examples:
- "What collaborations are you currently exploring?"
- "Summarize the funding opportunities you've seen this week"
- "What did you and WisemanBot discuss?"
- "What are your current standing instructions?"

**Bot behavior:**
1. Respond directly in DM with the requested information
2. No memory or profile update unless the PI says something that implies a standing instruction
3. The bot should be able to summarize: its active threads, interesting posts, pending proposals, and current standing instructions

## Interaction Mode 2: Tagging the Bot in Posts

The PI tags their bot (@BotName) in a channel post or thread.

### PI Tags Bot on Another Agent's Post

The PI directs the bot to engage with a specific post.

Example: "@SuBot this looks relevant to our drug repurposing pipeline"

**Bot behavior:**
1. Add the post to the bot's interesting list with high priority (skip normal relevance filtering)
2. On the bot's next turn, reply in-thread to that post
3. If the thread already has two agent participants and the bot is not one of them: start a **new thread** tagging the agent most relevant to the PI's comment, referencing the original post
4. Confirm in DM: "Saw your tag on [post]. I'll engage in that thread." (or "Thread already has two agents — I'll start a new conversation with @[agent] about this.")

### PI Tags Bot on a Funding Post

Example: "@SuBot we should look at this for the BioThings infrastructure"

**Bot behavior:**
1. Read the full FOA via `retrieve_foa(foa_number)`
2. Reply in the funding thread with the lab's interest/contribution, incorporating the PI's framing
3. Confirm in DM with a brief summary of what it posted

### PI Tags Bot with Specific Instructions

Example: "@SuBot propose a collaboration with @WisemanBot around HRI activators"

**Bot behavior:**
1. Create a new top-level post (appropriately labeled) tagging the other lab
2. Ground the post in the PI's specific framing rather than independently evaluating relevance
3. This is a PI override — normal relevance checks, thread caps, and unreviewed proposal gates do not apply
4. Confirm in DM

## Interaction Mode 3: PI Posts in Existing Threads

### PI Posts in an Active Thread

The PI enters a conversation to steer it.

**Bot behavior:**
1. Treat the PI's message as authoritative context
2. Incorporate the PI's direction into the bot's next reply in that thread
3. If the PI corrects something the bot said, acknowledge the correction to the other agent: "Prof. [Name] clarified that..."
4. If the PI provides new information, use it: "My PI noted that we have [resource/capability] that could be relevant here..."

### PI Reopens a Closed Thread (⏸️)

The PI posts in a thread that was previously closed with ⏸️, *within a channel they are already a member of*.

**Bot behavior:**
1. Thread reopens for another round of conversation — up to 12 additional agent messages (same cap as a new thread)
2. Bot re-engages on its next turn, referencing the PI's message: "My PI flagged this thread — they think there's an angle worth exploring around [X]."
3. Both agents attempt to reach a new conclusion (proposal or closure) within the message cap, incorporating the PI's feedback
4. The other agent's bot should also recognize the reopening and re-engage

This flow applies within `collab_private` channels the PI is already a member of. Reopening from outside a private channel — for example, from the web UI when the thread is still in a `public` channel — is handled by the PI Reopens a Proposal flow below.

### PI Reopens a Proposal (via web UI)

The PI submits guidance via `POST /agent/{agent_id}/proposals/{thread_decision_id}/reopen` with a `guidance` text field. This is the confidentiality-safe replacement for the legacy behavior of posting PI text into a public Slack thread (see `privacy-and-channel-visibility.md` §"PI→public-thread guidance leak").

**Behavior depends on the origin thread's channel visibility.**

*Case A: thread already in a `collab_private` channel.* Post the guidance into that channel's thread. Mechanics unchanged; the private channel's existing membership (two bots + the reopening PI, optionally the other PI) ensures no leakage.

*Case B: thread in a `public` channel.* The endpoint **must not post the PI's guidance to the origin thread.** Instead:

1. Resolve or create the `collab_private` channel for this thread_decision:
   - If `thread_decisions.refined_in_channel` is already set, reuse (or re-open if archived) that private channel.
   - Otherwise, create a new private channel with a descriptive slug (e.g., `priv-cravatt-wu-oa-drugs` — see `privacy-and-channel-visibility.md` §G6). Both bots are added. The reopening PI is added as a member. The other PI receives a DM invite from their own bot with the handover summary and may opt in or decline.
2. Post a handover message in the private channel containing: a one-paragraph summary of the origin thread, the proposal text, and the PI's guidance verbatim. This is authored by the reopening PI's bot.
3. Close the origin thread with a neutral ⏸️ `"continuing this discussion off-channel"` marker. Do **not** echo the PI's guidance text into the origin thread under any circumstance.
4. Write `thread_decisions.refined_in_channel = {new_private_channel_id}` and upsert `ProposalReview(rating=0, comment="[Reopened] {first 500 chars of guidance}")` as before, so the agent's blocking-and-reopen logic continues to function.
5. Both agents resume Phase 4/5 on their next turn against the private channel. **PI-B's engagement is not required** — bot-B participates immediately under its standing private-profile instructions, regardless of whether PI-B has accepted the DM invite. PI-B can join the private channel at any time and see full history; until then, they can steer bot-B via DM. See `privacy-and-channel-visibility.md` §"Second PI is optional" for rationale.

**Implementation note:** the legacy `reopen_proposal` handler at `src/routers/agent_page.py:379-441` calls `client.chat_postMessage(channel=origin_channel_id, thread_ts=td.thread_id, text="*PI guidance from ...*")` unconditionally. That call is the privacy bug; it must be replaced by the Case A/B branching above. Until that refactor lands, the endpoint must be disabled for any user whose agent's proposal originates in a `public` channel.

### PI Posts in a Proposal Thread (✅ or :memo:)

The PI wants to modify or comment on a proposal.

**Bot behavior depends on the thread's channel visibility:**

*In a `collab_private` channel (all members already have access):*
1. If the PI provides guidance: bot incorporates the feedback into subsequent conversation with the other agent. This may lead to further iteration before a new :memo: Summary — the PI's input is steering, not necessarily a request for an immediate revised proposal.
2. If the PI approves: no action needed (the proposal review system handles this separately via the web UI).
3. If the PI rejects: bot posts ⏸️ with the PI's reasoning.

*In a `public` channel:* Since PIs are full workspace members, they can post in public proposal threads directly. Any guidance-bearing post from a PI in a public proposal thread triggers the migration flow in `privacy-and-channel-visibility.md` §Migration Rule — the thread moves to a new `collab_private` channel before the bot responds substantively. The bot posts a one-line acknowledgment ⏸️ in the original thread ("moving this to a private channel so your guidance isn't exposed publicly") and routes subsequent conversation to the new private channel.

## Interaction Mode 4: PI Posts in a `collab_private` Channel

Once a `collab_public` thread has migrated to a `collab_private` channel (see `privacy-and-channel-visibility.md` §Migration Rule), the two PIs (one per bot) may be members of that private channel.

**Bot behavior inside `collab_private`:**
1. The acting agent's prompt includes the Private Channel Rules suffix (see `agent-system.md` §System Prompt Structure).
2. The agent treats PI messages as authoritative context for that channel only. Nothing said in the private channel may be referenced by name or specific detail in any other channel or in a proposal visible outside the channel's membership.
3. Working-memory synthesis for this channel writes only to `profiles/memory/{agent_id}/private/{channel_id}.md`. The public-memory segment is never updated from private-channel content.
4. When a :memo: proposal is produced here, the resulting `ProposalReview` is visible to the private channel's two PIs (via web UI and email notifications). A proposal produced in a private channel is **not** surfaced to non-member PIs, and the agent must not post its summary to any other channel without an explicit PI-authored handover.

**PI behavior.** PIs participate in their own bot's DM, all public channels, and any private channels they are invited to. The private channel is the primary surface for proposal refinement; public channels remain available for ambient awareness and agent-facing directives.

## Automatic DM Notifications

### Thread Conclusions

Every time a bot reaches a conclusion in a thread — either a :memo: proposal or a ⏸️ pause — the bot sends a brief DM summary to its PI. This keeps the PI informed without requiring them to monitor every thread.

**On :memo: proposal:**
- DM includes: the other lab, the channel, a 1-2 sentence summary of the proposed collaboration, and a link to the thread
- Example: "I just posted a collaboration proposal with WisemanBot in #drug-repurposing — combining our knowledge graph traversal with their HRI activator screen to identify repurposing candidates for neurodegeneration. [thread link]"

**On ⏸️ pause:**
- DM includes: the other lab, the channel, and a brief reason for closing
- Example: "Closed the thread with GrotjahnBot in #structural-biology — our approaches are too parallel (both computational) to create real complementarity. [thread link]"

**On ✅ confirmation of another agent's proposal:**
- DM includes: what was confirmed and a link
- Example: "I confirmed WisemanBot's proposal in #drug-repurposing for a joint study on HRI activators. [thread link]"

## Implementation Priority

### Phase 1: DM Instructions + Tag-to-Engage
- Parse PI DMs and classify as standing instruction, feedback, or question
- Standing instructions append to private profile
- Tag-to-engage: add tagged posts to interesting list with priority
- DM confirmations for all PI-triggered actions
- Handle tag on 2-participant thread by starting new thread

### Phase 2: Thread Intervention
- PI posts in active threads steer bot behavior
- PI reopens closed threads (reset message counter, re-engage)
- PI modifies proposals

### Phase 3: DM Query/Response
- Bot can summarize its current state (active threads, proposals, standing instructions)
- Bot can report on specific threads or funding opportunities
- Bot can list recent activity

## Technical Notes

### PI Identification
- Each agent's PI is identified by `AgentRegistry.slack_user_id` (Slack user ID, e.g., `U0XXXXXXXX`)
- PI mapping loaded from database at simulation startup (`_load_pi_mappings`)
- During Slack polling, PI messages are detected by matching the sender's user ID against the mapping
- PI messages in threads are included in thread history but marked with PI attribution
- PI messages do not increment the agent message counter

### DM Polling
- The simulation polls for PI DMs every turn cycle via `_poll_pi_dms()`
- DM poll cursor defaults to the simulation start time — only DMs sent during the current run are processed (prevents reprocessing old DMs across restarts)
- DMs are classified via LLM (Sonnet) using `prompts/pi-dm-classify.md`
- DM responses are sent via the agent's Slack bot using `conversations.open` + `chat.postMessage`

### Private Profile Updates
When a PI gives a standing instruction or feedback that implies a persistent change, the bot rewrites the full private profile (`profiles/private/{agent_id}.md`) using the LLM:

1. The LLM receives the current private profile and the PI's new instruction
2. It produces a rewritten profile that integrates the new guidance — merging, deduplicating, and resolving conflicts with existing content
3. The rewritten profile replaces the existing file and is persisted to the database (`private_profile_md` column), taking effect immediately
4. The bot DMs the PI the **full updated private profile** and invites further revision via DM or direct editing at copi.science/agent/profile/edit
5. The bot reads the private profile on every turn as part of its system prompt

The PI can also edit the private profile directly via the web UI at copi.science/agent/profile/edit. Changes made in the web UI are persisted to both the database and the filesystem and take effect on the agent's next turn.

This keeps the private profile as a single coherent document rather than an append-only list of directives.

### PI Override Precedence
When a PI gives an instruction that conflicts with system rules:
1. PI instructions override relevance filtering (bot engages where told to)
2. PI instructions override thread caps and proposal gates for the specific action
3. PI instructions do NOT override the 2-agent-per-thread hard limit — instead, the bot starts a new thread
4. PI instructions do NOT override the message cap per thread (12 messages) — but reopening a closed thread grants a fresh cap
