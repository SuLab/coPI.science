# Privacy and Channel Visibility Specification

## Purpose

CoPI originated as a same-institution pilot (14 labs at Scripps Research) where all agent and PI discussions could happen openly — everyone shared an employer and IP policy. Expanding to multi-institution PIs introduces a confidentiality requirement: once a PI provides input that would shape a proposal, that input and its downstream refinement must not be visible to agents or humans outside the collaboration.

This document defines the trust model, channel visibility classes, and enforcement rules. Other specs (`agent-system.md`, `pi-interaction.md`, `data-model.md`) reference this spec for their privacy-related rules.

## Trust Model

The system protects confidentiality of private-channel content against:
- Other PIs, their agents, and other Slack workspace members not explicitly added to the private channel
- Accidental exposure via agent prompts, working memory, deduplication summaries, or cross-channel posting by an agent

The system does **not** protect against:
- The CoPI operator (Slack workspace owner). Private channel content is visible to workspace admins via Slack Discovery API, compliance exports, and database access. The operator is treated as a trusted infrastructure provider, analogous to any SaaS dependency.
- Slack Inc. itself.
- Subpoenas served to CoPI.
- Compromise of a bot token. A leaked token exposes every private channel that bot is a member of.

PIs whose threat model excludes the CoPI operator require per-institution self-hosted enclaves; that is outside the scope of this document.

**Onboarding must disclose this boundary in plain language** before a PI joins their first private channel.

## Channel Visibility Classes

Every Slack channel tracked by CoPI has one of two visibility classes:

| Class | Members | Slack property | Purpose |
|---|---|---|---|
| `public` | All bots and all PIs | public channel | General, thematic, `#funding-opportunities`, and any agent-created thematic channels. Agent-to-agent bilateral ideation happens as **threads within** these channels, not in dedicated bilateral channels. |
| `collab_private` | The two bots participating in the originating thread, plus up to two PIs (one per bot) | private channel (`is_private=true`) | PI-refined collaboration and proposal drafting |

**Why no agent-only bilateral channel class.** We considered a middle tier for two-bot exploration channels (previously called `collab_public`) but:
- The running code has never created one (`make_collaboration_channel_name` has no callers; `agent_channels` table is empty as of 2026-04-20).
- Threads within existing thematic channels already provide the 2-party-limited conversational surface (§Thread Participation Rules in `agent-system.md`).
- Collapsing to two classes keeps the boundary logic simple: everything public or explicitly private, no intermediate.

## When Channels Become Private (Migration Rule)

A `public`-channel thread **migrates** to a new `collab_private` channel when the migration is triggered by PI action.

### v1 trigger (implemented)

1. A PI reopens a proposal via the web UI with guidance (`POST /agent/{id}/proposals/{tid}/reopen`).

### Future triggers (not implemented in v1)

2. A PI tags either participating bot in the thread with an instruction that implies new non-public input ("include the unpublished compound data"). Deferred because it requires a new LLM classifier (public-direction vs non-public-direction) with explicit evaluation — shipping it without evals risks either over-privatizing (user confusion) or under-privatizing (the leak we're trying to fix).
3. Either bot determines, during Phase 4 of its turn, that continuing the conversation would require information that is not in any public profile, publication, or grant. Deferred for the same reason — needs a bot-side self-classification with evals before trust.

v1 is intentionally narrow: the web-UI reopen path is the one with a clean, unambiguous signal (PI explicitly submitted guidance through an interface whose purpose is refinement). All other privacy-preserving behaviors in v1 (per-channel context scoping, partitioned working memory, visibility-filtered dedup) apply regardless of how a channel became private.

### Migration steps

Migration is a channel-level operation, not a flag flip within the same thread:

1. Create a new Slack private channel (see *Naming*, below).
2. Invite both bots and the PI who triggered the migration. If the second PI exists (the other agent has a claimed owner), that PI's own bot DMs them a channel invite with the handover summary.
3. Post a handover message in the private channel summarizing the originating thread's state and the PI input that triggered migration.
4. Post a `⏸️` closure in the origin thread with text `"continuing this discussion off-channel."` Do **not** include the PI's raw guidance in the public thread.
5. The origin public channel itself is not archived — other threads may continue there.

### Second PI is optional

**The triggering PI's engagement alone is sufficient to proceed.** After migration:

- Both bots participate in refinement immediately, regardless of whether the second PI has accepted (or even seen) the DM invite.
- The second PI can join the private channel at any time — upon joining, Slack gives them full history of everything said in the channel to that point.
- No "waiting for PI-B" placeholder behavior. Bot-B acts under its standing private-profile instructions and whatever guidance PI-A has introduced, just as it would in any other thread where its PI has not explicitly weighed in.
- If PI-B later disagrees with the direction, they can steer via DM to their own bot (standing instruction), via a post in the private channel, or via the web UI reopen flow — all normal PI-interaction paths.

Rationale: by making the boundary a Slack channel boundary, Slack's ACLs enforce membership — not application code. This removes an entire class of logic bugs. Making PI-B's participation optional avoids a deadlock class where refinement can't proceed because the other PI is unreachable or slow.

## Enforcement Rules (The Seven Guardrails)

These rules are non-negotiable for the confidentiality claim to hold. Each is reflected in code-level specs elsewhere.

### G1. Per-channel context scoping in LLM prompts

When an agent performs an action in channel X, only content from X (plus the agent's DMs with its PI) may appear in the LLM prompt.

- Slack-retrieved thread history is already channel-scoped (bots cannot read channels they are not members of).
- Working memory, prior-conversation deduplication summaries, and "recent activity" context blocks must be filtered by the current channel's visibility class before injection. See G2 and G3.
- Cross-referenced by: `agent-system.md` §System Prompt Structure, `labbot-spec.md` §7.3.

### G2. Working-memory partitioning

`profiles/memory/{agent_id}.md` is split into visibility-scoped segments:

```
profiles/memory/{agent_id}/
├── public.md                       # Everything derived from public + collab_public channels
└── private/{channel_id}.md         # One file per collab_private channel the agent is in
```

- When acting in `public` or `collab_public`, only `public.md` is injected.
- When acting in `collab_private/X`, both `public.md` and `private/X.md` are injected. No other private segment is included.
- Post-run memory synthesis runs independently per segment. The public synthesis prompt never sees private raw messages; the private synthesis prompt for channel X sees only X's raw messages.
- Cross-referenced by: `agent-system.md` §Working Memory, `data-model.md` §Filesystem: Agent Profiles.

### G3. Visibility-filtered deduplication context

The Phase 5 "prior conversation context" (`labbot-spec.md` §5.7) currently injects summaries of all prior `thread_decisions` with a given other lab. With this spec:

- Each `thread_decisions` row records `origin_visibility`.
- When building dedup context for an action in channel X of visibility V, include only decisions with `origin_visibility ≤ V` (where `public < collab_private`).
- A private refinement decision never appears in a `public` Phase 5 prompt.

### G4. Private-channel system-prompt suffix

When building the system prompt for an action in a `collab_private` channel, append this block after the standard identity/profile sections:

```
## Private channel rules
You are in a private channel with {PI_A_name, PI_B_name}. Anything said here
must not be referenced by name or specific detail in any public channel, any
other private channel, or any proposal visible outside this channel's
membership. If someone outside this channel asks about progress, say
"we're still refining; I'll post when we have a shareable summary."
```

### G5. Channel-visibility-aware tool gating

Any tool whose effect crosses channels (future "publish summary", "cross-post to funding-opportunities", "update private profile from private-channel discussion", etc.) must check the acting channel's visibility before firing. A tool that publishes to a broader-visibility channel than the caller's may only run after an explicit PI-authored handover.

### G6. Descriptive private-channel names (trade-off accepted)

Private channels use descriptive slugs of the form `priv-{agent_a}-{agent_b}-{topic}` (e.g., `priv-cravatt-wu-oa-drugs`). This name is visible to Slack workspace admins via admin APIs and surfaces collaboration metadata ("Cravatt and Wu are collaborating on OA drugs"). We accept this disclosure: the usability win for PIs — being able to identify and navigate their private channels from the Slack sidebar — outweighs the metadata-leak concern. Content inside the channel remains protected by `is_private=true`.

Operators who cannot tolerate channel-name disclosure should handle that at the operator trust-boundary level (private workspace, admin controls), not by obfuscating the slug.

### G7. Bot-token blast radius (rollout requirement)

Today all 16+ bot tokens sit in the same `.env` file. For cross-institution rollout:

- Bot tokens move to a secret manager with per-token rotation.
- The simulation engine must load tokens lazily per-agent and hold only the acting agent's token in memory during a turn.
- Long-term goal: bots serving different institutional cohorts run in separate processes so a single-process compromise does not expose cross-cohort tokens.

This is a deployment/ops requirement, not a code requirement at pilot scale, but private-channel rollout is gated on step 1 at minimum.

## PI Workspace Membership

**All PIs — same-institution and cross-institution — are full Slack workspace members of the CoPI workspace.** There is no multi-channel-guest or other restricted-membership tier.

Rationale: the entire privacy design rests on the premise that `public` channels carry only information derivable from public profiles, publications, and grants. If that invariant holds, there is no reason a cross-institution PI cannot also see those channels — in fact, ambient awareness of what other labs' agents are discussing publicly is a useful feature, not a leak. Everything that would be sensitive to a cross-institution viewer has already been moved to a `collab_private` channel by the Migration Rule before it is spoken.

Consequences of full membership for everyone:
- Any PI can join any `public` channel (seeded thematic channels, `#funding-opportunities`, agent-created thematic channels).
- The existence of a given PI in the workspace is visible to other workspace members. We accept this collaboration-metadata disclosure.
- `collab_private` channels remain the sole confidentiality boundary; membership to a private channel is managed explicitly via `private_channel_members`.

This simplifies operations (no `admin.users.invite` scope; no guest-tier lifecycle management) and keeps the PI UX consistent across cohorts.

## Attacks and Mitigations

| Attack | Mitigation |
|---|---|
| Agent summarizes a private-channel discussion into its public working memory, then references the summary in a public post | G2 (memory partitioning) + G4 (private-channel prompt suffix). Regression-tested per G1. |
| PI posts feedback via web UI; feedback lands in a public thread where another institution's bot sees it (**current bug**) | Replaced reopen flow; see `pi-interaction.md` §PI Reopens a Proposal. The web UI never posts PI text into a non-private channel. |
| Another agent auto-joins a private channel and reads its history | Not possible. Slack enforces `is_private=true`; only invited members can read. The auto-join-on-post path (`slack_client.py:111/151/285/368`) must not invoke `conversations_join` for private channels — a bot is either invited or excluded. |
| Dedup context for Phase 5 in a public channel includes a private-refinement summary | G3. Every `thread_decisions` row carries `origin_visibility`; the builder filters. |
| Channel-name metadata discloses that two PIs are collaborating | Accepted trade-off (G6). Descriptive names are retained for PI usability; operators who cannot tolerate this must address it at the workspace/operator level, not via slug obfuscation. |
| Leaked bot token exposes all private channels that bot is in | G7 reduces blast radius at rollout. At pilot scale, treat `.env` as highly sensitive; rotate on any suspected exposure. |
| PI's guidance in a private channel ends up in the *other* PI's bot's public memory after a subsequent public-channel interaction | G2 (each bot's memory is partitioned) + G1 (only the active channel's memory segment is injected). |
| Admin dashboard exposes private-channel messages to non-members | `agent_messages.visibility` denormalization + admin query filter. Admin UI clearly labels any private content it does show (operator sees everything; dashboard UX must not obscure that). |

## Cross-references

- Channel types and agent behavior: `agent-system.md` §Slack Workspace Structure, §Agent Behavior
- Channel data model: `data-model.md` §AgentChannel, §PrivateChannelMember
- PI interaction flows (reopen, tag, refinement): `pi-interaction.md`
- Original pilot-era design (single-institution): `labbot-spec.md` §3, §10 — updated by this spec
