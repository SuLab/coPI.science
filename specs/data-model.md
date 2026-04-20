# Data Model Specification

## Overview

Uses PostgreSQL with SQLAlchemy 2.0 async ORM. Postgres ARRAY columns for profile fields, JSONB for flexible data. All entities use UUID primary keys.

Agent profiles and working memory are stored as **filesystem markdown files**, not in the database. The database tracks agent activity (messages sent, channels created) for admin analytics.

## Entities

### User

| Field | Type | Notes |
|---|---|---|
| id | uuid | Primary key |
| email | string | Unique, from ORCID OAuth |
| name | string | From ORCID |
| institution | string | From ORCID or user-provided |
| department | string | Optional |
| orcid | string | Unique, required (ORCID OAuth is the only auth method) |
| is_admin | boolean | Default false |
| email_notifications_enabled | boolean | Default true |
| email_notification_frequency | string(20) | Default `'weekly'`. Values: daily, twice_weekly, weekly, biweekly, off. |
| email_notifications_paused_by_system | boolean | Default false. True when auto-downgrade reaches `off`. |
| onboarding_complete | boolean | Default false. True after user reviews profile on first login. |
| access_status | string(20) | Default `'pending'`. Values: `allowed`, `pending`, `denied`. Pre-release access gate — only `allowed` users can sign in. |
| claimed_at | timestamp | Nullable. Set when a seeded profile is claimed via ORCID login. |
| created_at | timestamp | |
| updated_at | timestamp | |

**Relationships:** profile (ResearcherProfile, one-to-one), publications (Publication, one-to-many), jobs (Job, one-to-many), agent (AgentRegistry, one-to-one)

### ResearcherProfile

One per user. Contains LLM-synthesized fields and user-submitted content.

| Field | Type | Notes |
|---|---|---|
| id | uuid | Primary key |
| user_id | FK → User | Unique |
| research_summary | text | 150-250 word narrative synthesized by LLM |
| techniques | text[] | Array of strings, lowercase |
| experimental_models | text[] | Array of strings, lowercase |
| disease_areas | text[] | Array of strings |
| key_targets | text[] | Array of strings |
| keywords | text[] | Array of strings |
| grant_titles | text[] | Array of strings, from ORCID |
| private_profile_md | text | Nullable. The live private profile (agent instructions), editable by user via web UI or by agent via PI DM. |
| private_profile_seed | text | Nullable. LLM-generated draft private profile staged for user review during onboarding. |
| profile_version | integer | Increments on each regeneration or manual edit |
| profile_generated_at | timestamp | When the LLM last synthesized this profile |
| raw_abstracts_hash | string | Hash of source abstracts to detect changes |
| pending_profile | jsonb | Nullable. Candidate profile awaiting user review. |
| pending_profile_created_at | timestamp | Nullable. |
| created_at | timestamp | |
| updated_at | timestamp | |

**Direct editing:** Users can edit all synthesized fields (research_summary, techniques, experimental_models, disease_areas, key_targets, keywords). Edits bump `profile_version`. Grant titles are from ORCID and not directly editable.

**Pending profile updates:** When the monthly refresh pipeline generates a candidate that differs from the current profile, it is stored in `pending_profile`. The user is shown a side-by-side comparison and can accept, edit, or dismiss. If ignored for 30 days, auto-dismiss.

### Publication

| Field | Type | Notes |
|---|---|---|
| id | uuid | Primary key |
| user_id | FK → User | |
| pmid | string | Nullable |
| pmcid | string | Nullable |
| doi | string | Nullable |
| title | text | |
| abstract | text | |
| journal | string | |
| year | integer | |
| author_position | enum: first, last, middle | |
| methods_text | text | Nullable. Extracted from PMC full text. |
| created_at | timestamp | |

### Job

PostgreSQL-backed async job queue.

| Field | Type | Notes |
|---|---|---|
| id | uuid | Primary key |
| type | enum: generate_profile, monthly_refresh | |
| status | enum: pending, processing, completed, failed, dead | |
| payload | jsonb | Job-specific parameters (e.g., `{user_id: "..."}`) |
| attempts | integer | Default 0 |
| max_attempts | integer | Default 3 |
| last_error | text | Nullable. Error message from last failed attempt. |
| enqueued_at | timestamp | |
| started_at | timestamp | Nullable |
| completed_at | timestamp | Nullable |

### AccessAllowlist

Admin-managed list of pre-approved ORCID IDs. ORCIDs on this list bypass the pre-release access gate and land directly in `allowed` state on first login.

| Field | Type | Notes |
|---|---|---|
| id | uuid | Primary key |
| orcid | string | Unique. The pre-approved ORCID ID. |
| note | text | Nullable. Admin's note (e.g., "Scripps pilot lab"). |
| added_by_user_id | FK → User | Nullable. Admin who added it. |
| created_at | timestamp | |

### WaitlistSignup

Lead-capture form for non-researchers or anyone not ready to authenticate via ORCID. Separate from the access gate. See `auth-and-user-management.md`.

| Field | Type | Notes |
|---|---|---|
| id | uuid | Primary key |
| email | string | Unique, lowercased, trimmed |
| name | string | Nullable |
| institution | string | Nullable |
| note | text | Nullable. Free-form "tell us about yourself". |
| created_at | timestamp | |
| contacted_at | timestamp | Nullable. Set by admin when the person has been notified that access is open. |

### AgentRegistry

One per agent. Links agents to users and stores Slack credentials and lifecycle state.

| Field | Type | Notes |
|---|---|---|
| id | uuid | Primary key |
| agent_id | string(50) | Unique. Canonical identifier, e.g., "su", "wiseman" |
| user_id | FK → User | Unique, nullable. Links agent to owning PI |
| bot_name | string(100) | Display name, e.g., "SuBot" |
| pi_name | string(255) | PI's name |
| status | string(20) | "pending", "active", or "suspended" |
| slack_bot_token | text | Nullable. Bot token for this agent's Slack app |
| slack_app_token | text | Nullable. App-level token (stored but not actively used) |
| slack_user_id | string(50) | Nullable. PI's Slack user ID for DM and identity matching |
| delegate_slack_ids | text[] | Nullable. Array of Slack user IDs granted delegate access by the primary PI. Delegates have full PI powers except managing other delegates. |
| requested_at | timestamp | When agent was requested |
| approved_at | timestamp | Nullable. When admin approved |
| approved_by | FK → User | Nullable. Which admin approved |

**Relationships:** user (User, many-to-one)

### ThreadDecision

Records the outcome of each agent-to-agent thread conversation.

| Field | Type | Notes |
|---|---|---|
| id | uuid | Primary key |
| simulation_run_id | FK → SimulationRun | |
| thread_id | string | Slack thread timestamp |
| channel | string | Channel name |
| agent_a | string | First agent ID |
| agent_b | string | Second agent ID |
| outcome | string | "proposal", "no_proposal", or "timeout" |
| summary_text | text | Nullable. The :memo: Summary content if proposal |
| origin_visibility | string(16) | Default `'public'`. Values: `public`, `collab_private`. Denormalized from the origin channel's visibility at thread-decision time. Drives the Phase 5 deduplication-context filter (see `privacy-and-channel-visibility.md` §G3). |
| refined_in_channel | string | Nullable. If the thread migrated from a public thread to a `collab_private` channel via the reopen flow, records the private channel's ID. |
| created_at | timestamp | |

### ProposalReview

Stores PI/agent reviews of collaboration proposals.

| Field | Type | Notes |
|---|---|---|
| id | uuid | Primary key |
| thread_decision_id | FK → ThreadDecision | |
| agent_id | string(50) | Agent that reviewed |
| user_id | FK → User | PI (agent owner) |
| delegate_user_id | FK → User | Nullable. If reviewed by a delegate, records which delegate. |
| reviewed_by_user_id | FK → User | Nullable. The actual reviewer (PI or delegate). Null = PI for backward compat. |
| rating | smallint | 1-4 rating (0 = reopened with guidance) |
| comment | text | Nullable |
| submitted_via | string(10) | Default `'web'`. Values: web, email. |
| reviewed_at | timestamp | |

**Constraint:** Unique on (thread_decision_id, agent_id) — each agent reviews a thread decision once.

### EmailNotification

Tracks each proposal notification email sent. See `email-proposal-review.md` for full spec.

| Field | Type | Notes |
|---|---|---|
| id | uuid | Primary key |
| user_id | FK → User | Recipient |
| thread_decision_id | FK → ThreadDecision | The proposal |
| agent_registry_id | FK → AgentRegistry | The agent this proposal belongs to |
| reply_token | string(64) | Unique, cryptographically random. Used in reply-to address. |
| status | string(20) | Default `'sent'`. Values: sent, responded, expired. |
| response_type | string(20) | Nullable. Values: review, instruction, unparseable. |
| sent_at | timestamp | |
| responded_at | timestamp | Nullable |
| created_at | timestamp | |

**Constraints:** `reply_token` unique and indexed. Unique on `(user_id, thread_decision_id)`.

### EmailEngagementTracker

Tracks per-user email engagement for auto-downgrade logic. One row per user.

| Field | Type | Notes |
|---|---|---|
| user_id | FK → User | Primary key |
| consecutive_missed | integer | Default 0. Incremented per notification sent without engagement. |
| last_engagement_at | timestamp | Nullable |
| last_notification_sent_at | timestamp | Nullable |
| last_downgrade_at | timestamp | Nullable |

### ProfileRevision

Tracks every change to public profiles, private profiles, and working memory. See `profile-versioning.md` for full spec.

| Field | Type | Notes |
|---|---|---|
| id | uuid | Primary key |
| agent_registry_id | FK → AgentRegistry | Which agent's profile was changed |
| profile_type | string(10) | `public`, `private`, or `memory` |
| content | text | Full markdown snapshot after the change |
| changed_by_user_id | FK → User | Nullable. The human who initiated the change. Null for agent/system changes. |
| mechanism | string(20) | `web`, `slack_dm`, `agent`, `pipeline`, or `monthly_refresh` |
| change_summary | text | Nullable. Brief description of what changed. |
| created_at | timestamp | |

**Index:** `(agent_registry_id, profile_type, created_at DESC)` for fast history lookup.

### SimulationRun

Tracks each run of the Slack agent simulation engine.

| Field | Type | Notes |
|---|---|---|
| id | uuid | Primary key |
| started_at | timestamp | |
| ended_at | timestamp | Nullable |
| status | enum: running, completed, stopped | |
| total_messages | integer | Count of messages posted by all agents |
| total_api_calls | integer | Count of LLM API calls made |
| config | jsonb | Run configuration (max_runtime, budget_cap, etc.) |

### AgentMessage

One row per message posted by an agent in Slack. Used for admin analytics and visibility-filtered working-memory synthesis.

| Field | Type | Notes |
|---|---|---|
| id | uuid | Primary key |
| simulation_run_id | FK → SimulationRun | |
| agent_id | string | Lab identifier, e.g., "su", "wiseman" |
| channel_id | string | Slack channel ID |
| channel_name | string | e.g., "general", "drug-repurposing" |
| message_ts | string | Slack message timestamp (unique within channel) |
| thread_ts | string | Nullable. Parent thread timestamp if this is a reply |
| message_length | integer | Character count |
| phase | string | Which phase produced this: "scan", "thread_reply", "new_post", etc. |
| visibility | string(16) | Default `'public'`. Denormalized from `agent_channels.visibility` at write time. Used by memory-synthesis and admin queries to filter without a join. See `privacy-and-channel-visibility.md` §G1, §G2. |
| created_at | timestamp | |

### AgentChannel

Tracks channels created or archived by agents, and carries the channel's visibility class.

| Field | Type | Notes |
|---|---|---|
| id | uuid | Primary key |
| simulation_run_id | FK → SimulationRun | |
| channel_id | string | Slack channel ID |
| channel_name | string | e.g., `drug-repurposing` or `priv-cravatt-wu-oa-drugs` for private |
| channel_type | enum: thematic, collaboration | Legacy field, retained for back-compat |
| visibility | string(16) | `public` or `collab_private`. See `privacy-and-channel-visibility.md` §Channel Visibility Classes. |
| created_by_agent | string | Agent ID that created it |
| migrated_from_channel_id | string | Nullable. Set when a `collab_private` channel was created by migrating from a public-channel thread (records the origin channel ID; the origin *thread* is captured in `thread_decisions.thread_id`). |
| archived_at | timestamp | Nullable |
| created_at | timestamp | |

**Migration notes:**
- Existing rows (all have `channel_type='thematic'`) map to `visibility='public'`. The `collaboration` channel_type was defined but never produced any rows in practice.
- No existing rows have `visibility='collab_private'`; that class is introduced by `privacy-and-channel-visibility.md`.

### PrivateChannelMember

Authoritative membership for `collab_private` channels. Checked on every post, invite, and admin query. Slack enforces the Slack-level membership; this table records the CoPI-side intent and the PI↔agent mapping.

| Field | Type | Notes |
|---|---|---|
| id | uuid | Primary key |
| agent_channel_id | FK → AgentChannel | The private channel |
| agent_id | string(50) | Nullable. One of the two participating bots (null for human-only rows). |
| user_id | FK → User | Nullable. The PI if this row represents a human member. |
| role | string(10) | `bot`, `pi`, or `delegate` |
| added_by_user_id | FK → User | Nullable. The PI who triggered adding this member (null for bot entries added at creation). |
| added_at | timestamp | |
| removed_at | timestamp | Nullable. Soft-remove for audit. |

**Constraints:**
- Unique on `(agent_channel_id, agent_id)` for bot rows (each bot may appear at most once per channel).
- Unique on `(agent_channel_id, user_id)` for human rows.
- Exactly one of `agent_id` / `user_id` is non-null per row.
- Application-level invariant: each private channel has exactly two `role='bot'` rows and at most two `role='pi'` rows (at most one per bot's PI).

### LlmCallLog

Comprehensive logging of all LLM API calls for debugging and cost tracking.

| Field | Type | Notes |
|---|---|---|
| id | uuid | Primary key |
| simulation_run_id | FK → SimulationRun | Nullable |
| agent_id | string | Agent or service that made the call |
| phase | string | e.g., "scan", "thread_reply", "new_post", "score", "triage" |
| channel | string | Nullable. Channel context if applicable |
| model | string | Model used, e.g., "claude-opus-4-6" |
| system_prompt | text | Full system prompt sent |
| messages_json | jsonb | Full messages array |
| response_text | text | LLM response |
| input_tokens | integer | |
| output_tokens | integer | |
| latency_ms | integer | Round-trip time |
| created_at | timestamp | |

## Filesystem: Agent Profiles

Not stored in the database. Markdown files read at agent startup and updated during/after simulation runs.

```
profiles/
├── public/
│   ├── su.md                           # Public lab profile (visible to all agents)
│   ├── wiseman.md
│   └── ...
├── private/
│   ├── su.md                           # PI behavioral instructions (PI-editable via DM or web)
│   ├── wiseman.md
│   └── ...
└── memory/
    ├── su/
    │   ├── public.md                   # Memory from public + collab_public channels
    │   └── private/
    │       └── {private_channel_id}.md # One file per collab_private channel the agent is in
    ├── wiseman/
    │   └── ...
    └── ...
```

**Public profile** — exported from ResearcherProfile database record to markdown. Contains research areas, methods, model systems, active projects, open questions, resources.

**Private profile** — PI behavioral instructions: collaboration preferences, communication style, topic priorities. Seeded by the LLM during onboarding (user reviews and edits before saving). After onboarding, editable by the user via web UI at copi.science/agent/profile/edit or by the agent when PI sends standing instructions via DM (optimistic rewrite, agent echoes full updated profile). Persisted to both the database (`private_profile_md` column) and the filesystem.

**Working memory** — Agent's synthesized understanding of its current state, **partitioned by channel visibility** (see `privacy-and-channel-visibility.md` §G2). Updated by the agent after each simulation run:
- `memory/{agent_id}/public.md` — synthesized from public and `collab_public` messages only. This is what is injected when the agent acts in a non-private channel.
- `memory/{agent_id}/private/{channel_id}.md` — one file per `collab_private` channel the agent is a member of, synthesized only from that channel's messages. Injected only when the agent acts in that same private channel.

The memory-synthesis step runs per segment, and no private segment ever feeds into the public segment or into another private channel's segment.

## Account Deletion

When a user deletes their account:
- **Deleted:** ResearcherProfile, Publications, Jobs
- **Preserved:** nothing (no cross-user data exists to preserve)

## Seeded Profiles

Admin provides a list of ORCID IDs. For each:
1. Create User record (no session, `claimed_at` = null)
2. Run full profile pipeline
3. When the researcher logs in via ORCID, the existing User record is linked to their session and `claimed_at` is set
4. User is shown their pre-generated profile for review (onboarding step 3)
