# Email-Based Proposal Review Specification

## Overview

When an agent produces a `:memo:` collaboration proposal, the PI's agent is blocked until the proposal is reviewed. Currently, review requires logging into the web app. This feature adds email notifications that nudge PIs and delegates to review pending proposals, and allows them to review or give agent instructions entirely via email reply.

Email is a **periodic nudge** — Slack DMs remain the immediate notification channel (per pi-interaction.md). Emails are sent at a user-configurable frequency as reminders for unreviewed proposals.

## Email Notification Flow

### Trigger

A notification cycle is triggered on a schedule (cron or worker loop) that checks each user's configured frequency. When a user's next scheduled email time arrives:

1. Query all unreviewed proposals for agents the user has access to (as PI or delegate)
2. If there are unreviewed proposals and no outstanding (unanswered) notification email for this user: send **one email** for the oldest unreviewed proposal
3. If there is already an outstanding notification email (sent but not yet replied to or reviewed via web): do not send another. Wait for the user to respond to the current one before sending the next.

This **one-at-a-time sequencing** prevents inbox flooding and keeps the interaction focused. After the user replies (or reviews via web), the next unreviewed proposal email is sent at the next scheduled check.

### Email Content

**From:** `noreply@copi.science`
**Reply-To:** `review+{token}@reply.copi.science` (unique per proposal + user)
**Subject:** `[BotName] has a new collaboration proposal to review`

**Body:**

```
[BotName] and [OtherBotName] developed a collaboration proposal in #[channel]:

---
[The :memo: summary text from the ThreadDecision]
---

To review this proposal, you can:

1. Reply to this email with a rating (1-4) and any comments:
   1 = Not interesting
   2 = Weak — unlikely to pursue
   3 = Promising — worth exploring further
   4 = Strong — let's pursue this

2. Reply with instructions for your agent (e.g., "focus on the
   mitochondrial angle instead") and it will re-engage to refine
   the proposal.

3. Review on the web: [deep link to proposal on dashboard]

---
[Unsubscribe link] | [Manage notification preferences]
```

**Backlog notice:** When additional unreviewed proposals exist beyond the one in this email, append a notice above the footer:

> There are [N] additional proposals waiting for review. Your agent is blocked from starting new collaborations until proposals are reviewed. You can review all proposals at [dashboard link].

This creates urgency and gives the PI an escape hatch to clear the backlog via the web app rather than waiting for the one-at-a-time email sequence.

**HTML version** includes formatting and styled rating buttons (linking to the web app as fallback for email clients that don't support reply).

### Confirmation Emails

After processing any email reply, send a brief confirmation:

- **Review parsed:** "Got it — you rated the [OtherBotName] collaboration proposal a [N]. [BotName] is unblocked and can start new conversations." (If more proposals remain, append: "You have [N] more proposals to review. The next one is on its way.")
- **Agent instruction parsed:** "Got it — I've passed your feedback to [BotName]. It will re-engage with [OtherBotName] to refine the proposal. You'll get another email when the revised proposal is ready."
- **Unparseable:** "I couldn't tell if you wanted to rate this proposal or give your agent instructions. To rate: reply with a number 1-4 and any comments. To direct your agent: describe what you'd like changed."

## Email Reply Processing

### Inbound Infrastructure (AWS SES)

Inbound email is received on a dedicated subdomain `reply.copi.science` to avoid interfering with MX records on the main domain. Using the main domain directly would route **all** email to `*@copi.science` through SES, breaking any personal or team email (e.g., Google Workspace) on that domain. The subdomain isolates inbound processing while outbound continues to send from `noreply@copi.science`.

**DNS setup:**
- Add MX record for `reply.copi.science` pointing to SES inbound endpoint (`inbound-smtp.us-east-2.amazonaws.com`, priority 10)
- Main domain `copi.science` MX records remain untouched

**SES inbound pipeline:**
1. SES receives email to `review+{token}@reply.copi.science`
2. SES Receipt Rule stores the email in S3 (`copi-inbound-email` bucket) and publishes to SNS
3. Worker polls S3 for new inbound emails (see Processing section)

**Recommended approach for pilot:** Worker-based polling. The existing worker process (`src/worker/main.py`) adds a loop that checks S3 for new inbound emails every 60 seconds. This avoids adding Lambda to the infrastructure. Switch to SNS-triggered Lambda if latency becomes an issue.

### Reply-To Address Format

```
review+{reply_token}@reply.copi.science
```

The `+` subaddressing is handled by SES receipt rules matching on the domain. The full local part (including the token) is available in the received email headers for extraction.

### LLM Reply Classification

The reply text (after stripping quoted content and signatures) is classified by Sonnet into one of three categories:

**1. Review** — The reply contains a proposal rating (1-4) and optional comment.
- Extract: rating (integer 1-4), comment (remaining text)
- Action: Create ProposalReview record. Mark EmailNotification as responded. Send confirmation email.

**2. Agent Instruction** — The PI wants the agents to refine, adjust, or continue working on the proposal. The reply describes what they'd like changed but does not contain a rating.
- Extract: the instruction text
- Action: Pass to the PI interaction handler as if the PI posted in the proposal thread (per pi-interaction.md — bot incorporates feedback, re-engages with the other agent). Mark EmailNotification as responded. Send confirmation email. When the revised `:memo:` proposal is posted, a new notification cycle begins for it.

**3. Unparseable** — Cannot determine intent.
- Action: Send the help/clarification email. Do not mark the notification as responded (user can reply again).

**Prompt context:** The LLM receives the proposal summary text and the user's reply. It returns structured JSON: `{"category": "review|instruction|unparseable", "rating": null|1-4, "comment": "...", "instruction": "..."}`.

### Email Parsing Steps

1. Extract the `To` address to get the reply token
2. Look up EmailNotification by token
3. Validate: token exists, status is `sent`
4. Verify sender email matches the User record for the notification. Reject if mismatched (prevents forwarded-email abuse).
5. Strip quoted content (`>` prefix) and email signatures (`-- ` delimiter) from the reply body
6. Pass cleaned body to LLM for classification
7. Process based on category

### Security

- Reply tokens are 64-character cryptographically random strings
- A `sent` notification can receive multiple replies until it gets a parseable one (review or instruction). Once marked `responded`, the token is dead.
- Sender email verification against User record
- Rate limit: max 10 replies per token per hour

## Notification Frequency & Scheduling

### Frequency Options

| Setting | Schedule |
|---|---|
| `daily` | Once per day (8am UTC) |
| `twice_weekly` | Monday and Thursday |
| `weekly` | Monday |
| `biweekly` | Every other Monday |
| `off` | No email notifications |

Default for new users: `weekly`.

Users configure their frequency in the web app at `/settings` or via the "Manage notification preferences" link in any notification email.

### One-at-a-Time Sequencing

At each scheduled check for a user:
1. Are there unreviewed proposals for this user's agent(s)?
2. Is there an outstanding (unanswered) email notification for this user?
   - If yes: skip. Wait for a response or web review.
   - If no: send one email for the oldest unreviewed proposal. Record it as outstanding.

When the user responds (email reply or web review of that specific proposal), the notification is marked as responded. On the next scheduled check, if more unreviewed proposals remain, the next email is sent.

This means a PI on weekly frequency with 3 pending proposals receives one email per week, reviewing them sequentially over 3 weeks — unless they clear the backlog via the web app (the backlog notice in every email encourages this).

## Engagement Tracking & Auto-Downgrade

### What Counts as Engagement

Any of the following resets the missed-email counter to 0:
- Replying to a notification email (review or agent instruction — not unparseable)
- Reviewing any proposal via the web app
- Any meaningful web app interaction (profile edit, settings change, etc.)

### Downgrade Ladder

After 3 consecutive notification emails with no engagement:
- Bump the user's frequency down one notch: `daily` → `twice_weekly` → `weekly` → `biweekly` → `off`

After reaching `off` via auto-downgrade:
- Send a final email: "We've paused proposal notifications since you haven't reviewed recently. To turn them back on, log into CoPI and review your pending proposals: [link]. You can adjust your notification frequency anytime in settings: [link]."
- Set `email_notifications_paused_by_system` to true

### Re-Enablement

The user can change their frequency at any time from `/settings`, regardless of current state. Choosing a frequency resets the missed counter to 0, clears `email_notifications_paused_by_system`, and reactivates notifications immediately.

## Data Model

### New Fields on User

| Field | Type | Notes |
|---|---|---|
| `email_notification_frequency` | enum: daily, twice_weekly, weekly, biweekly, off | Default: `weekly` |
| `email_notifications_paused_by_system` | boolean | Default false. True when auto-downgrade reaches `off`. Distinguished from user manually choosing `off`. |

### AgentDelegate Changes

The existing `notify_proposals` boolean (already stubbed) controls whether a delegate receives proposal notification emails for that specific agent:
- `true` (default): delegate receives emails at their own frequency
- `false`: no proposal emails for this agent

Delegates configure their own `email_notification_frequency` on their User record. This applies across all agents they have access to (own + delegated). Per-agent opt-out is via `notify_proposals`.

### EmailNotification (new table)

Tracks each notification email sent.

| Field | Type | Notes |
|---|---|---|
| id | uuid | Primary key |
| user_id | FK → User | Recipient |
| thread_decision_id | FK → ThreadDecision | The proposal |
| agent_registry_id | FK → AgentRegistry | The agent this proposal belongs to |
| reply_token | string(64) | Unique, cryptographically random |
| status | enum: sent, responded, expired | Default: `sent` |
| response_type | enum: review, instruction, unparseable | Nullable. Set when a reply is processed. |
| sent_at | timestamp | |
| responded_at | timestamp | Nullable |
| created_at | timestamp | |

**Constraints:**
- `reply_token` is unique and indexed
- Unique on `(user_id, thread_decision_id)` — one notification per user per proposal

### EmailEngagementTracker (new table)

| Field | Type | Notes |
|---|---|---|
| user_id | FK → User | Primary key (one row per user) |
| consecutive_missed | integer | Default 0 |
| last_engagement_at | timestamp | Nullable |
| last_notification_sent_at | timestamp | Nullable |
| last_downgrade_at | timestamp | Nullable |

### Changes to ProposalReview

Add columns:

| Field | Type | Notes |
|---|---|---|
| `reviewed_by_user_id` | FK → User | Nullable. The actual reviewer. Null = PI (backward compat with existing rows). When a delegate reviews, this is the delegate's user_id. |
| `submitted_via` | enum: web, email | Default: `web`. How the review was submitted. |

**Constraint change:** Relax the existing unique constraint from `(thread_decision_id, agent_id)` to `(thread_decision_id, agent_id, reviewed_by_user_id)`. This allows both a PI and a delegate to independently review the same proposal. The agent is unblocked when **any** review is recorded for its side of the proposal.

## Unsubscribe

Every notification email includes:

1. **One-click unsubscribe** (RFC 8058): `List-Unsubscribe` and `List-Unsubscribe-Post` headers. Sets `email_notification_frequency` to `off` via a signed POST request. No login required.
2. **Footer link:** "Unsubscribe from proposal notifications" — same behavior as the header.
3. **Preference link:** "Manage notification frequency" — links to `/settings` (requires login).

## Environment Variables

```
# Inbound email (new)
SES_INBOUND_S3_BUCKET=copi-inbound-email
SES_INBOUND_S3_PREFIX=inbound/
SES_REPLY_DOMAIN=reply.copi.science

# Outbound email (extends existing SES config from web-delegates.md)
SES_SENDER_EMAIL=noreply@copi.science
```

## New Files

| File | Purpose |
|---|---|
| `src/services/email_notifications.py` | Notification scheduling, sending, engagement tracking. Email templates are inlined (matching the pattern in `src/services/email.py`). |
| `src/services/email_inbound.py` | S3 polling, reply parsing, LLM classification, dispatch, confirmation/help emails |
| `src/routers/settings.py` | Settings page (frequency preferences) and unsubscribe endpoints |
| `src/models/email_notification.py` | EmailNotification and EmailEngagementTracker SQLAlchemy models |
| `prompts/email-reply-classify.md` | LLM prompt for classifying email replies |
| `templates/settings.html` | Settings page template (frequency dropdown, status display) |
| `templates/unsubscribe.html` | One-click unsubscribe confirmation page (no auth required) |
| `alembic/versions/0008_email_notifications.py` | Migration: new tables + new columns on users and proposal_reviews |

## Settings UI

Add to `/settings`:

**Email Notifications** section:
- Frequency dropdown: Daily, Twice a week, Weekly, Every two weeks, Off
- Current status: "Active — next check Monday" or "Paused — review pending proposals to reactivate"
- If `email_notifications_paused_by_system` is true, show prompt to review proposals and reactivate

## Implementation Priority

### Phase 1: Outbound Notifications
1. Database migration (EmailNotification, EmailEngagementTracker, User fields)
2. Notification scheduler in worker (frequency checks, one-at-a-time sequencing)
3. Email templates (proposal notification with backlog notice and deep links)
4. Settings UI for frequency preference
5. Engagement tracking and auto-downgrade logic
6. One-click unsubscribe (RFC 8058 headers)

### Phase 2: Inbound Reply Processing
1. DNS setup (`reply.copi.science` MX record)
2. SES inbound receiving → S3
3. Worker polling for inbound emails
4. LLM reply classification prompt and service
5. Review processing (save ProposalReview, unblock agent)
6. Agent instruction processing (pass to PI interaction handler)
7. Confirmation and help reply emails
8. Sender verification and rate limiting

### Phase 3: Polish
1. HTML email styling
2. ProposalReview constraint relaxation for delegate reviews
3. Delegate notification preferences (per-agent opt-out)
4. Auto-downgrade final notice email

---

# Feature: Multiple Notification Categories

## Motivation

The system today exposes a **single** email control — `User.email_notification_frequency` — which governs exactly one activity: *proposal-review reminders*. The settings page presents one toggle + one frequency dropdown (`templates/settings.html`), and the worker (`check_and_send_notifications`) sends only that one kind of email.

This feature generalizes the design from "one frequency for one activity" to **independent notification categories**, each with its own enable/disable state, its own delivery model (periodic vs. event-driven), and its own content. It introduces two new categories — **Status overview** and **New proposal** — alongside the existing reminder, which becomes the `proposal_review` category.

## Concept: Notification Categories

A **category** is a named class of email with three properties:

| Property | Description |
|---|---|
| `key` | Stable identifier (`proposal_review`, `status_overview`, `new_proposal`) |
| delivery model | `periodic_reminder`, `periodic_digest`, or `event_driven` |
| user controls | `enabled` (on/off) + optional `frequency` (for periodic categories) |

| Category | Delivery model | User controls | Source of content |
|---|---|---|---|
| `proposal_review` (existing) | periodic_reminder (one-at-a-time, frequency ladder) | on/off + frequency | Oldest unreviewed `ThreadDecision` |
| `status_overview` (new) | periodic_digest (time-windowed summary) | on/off + frequency | Aggregated `ThreadDecision` + `ProposalReview` over the window |
| `new_proposal` (new) | event_driven (one email per proposal, near-real-time) | on/off | The just-created `ThreadDecision` (its `summary_text`) |

The existing engagement tracking / auto-downgrade ladder (`EmailEngagementTracker`) continues to apply **only** to `proposal_review` — it is a nudge mechanism for an action the user owes. `status_overview` and `new_proposal` are informational and are not auto-downgraded; they are simply on or off.

## Data Model Changes

### New table: `email_notification_preferences`

Replaces the single `User.email_notification_frequency` field with one row per (user, category).

| Field | Type | Notes |
|---|---|---|
| user_id | FK → User | Part of composite PK |
| category | enum: proposal_review, status_overview, new_proposal | Part of composite PK |
| enabled | boolean | Default per category (see below) |
| frequency | enum: daily, twice_weekly, weekly, biweekly, monthly, off | Used by periodic categories; ignored by `new_proposal` |
| created_at / updated_at | timestamp | |

**Defaults for new/backfilled users:**
- `proposal_review`: enabled, frequency = `weekly` (migrated from the existing `email_notification_frequency` value)
- `status_overview`: enabled, frequency = `weekly`
- `new_proposal`: **disabled** by default (event-driven email can be high-volume; opt-in)

**Backward compatibility / migration (`alembic` follow-up to 0008):**
1. Create `email_notification_preferences`.
2. Backfill a `proposal_review` row for every user from their current `email_notification_frequency` (preserving `off`, and preserving `email_notifications_paused_by_system` semantics — a paused user gets `enabled=false`).
3. Backfill `status_overview` (weekly, enabled) and `new_proposal` (disabled) rows.
4. Keep `User.email_notification_frequency` as a **deprecated read-through** for one release, or drop it and have `proposal_review` be the source of truth. Recommended: keep the column nullable for one release, write to both, then drop.

A thin accessor — `get_pref(user, category)` returning `(enabled, frequency)` with category defaults when no row exists — keeps call sites clean and avoids null-checks throughout the services.

### Changes to `EmailNotification`

Add a `category` column (enum, default `proposal_review`) so the table can log all three kinds of email, not just review reminders. The existing unique constraint `(user_id, thread_decision_id)` becomes `(user_id, thread_decision_id, category)` — a single proposal can legitimately produce both a `new_proposal` email and (later) a `proposal_review` reminder.

For `status_overview` (which is not tied to a single proposal), `thread_decision_id` is nullable and a separate lightweight `digest_runs` row (or a `last_status_overview_sent_at` column on the preference row) tracks the window boundary.

## Category: Status Overview

A periodic **digest** summarizing the user's agent activity over a selectable window. Informational; no reply action required.

### Trigger & windowing

A worker loop (`check_and_send_status_overviews`, sibling of the existing notification loop) runs on schedule. For each user with `status_overview` enabled, when their frequency interval has elapsed since `last_status_overview_sent_at`, build and send a digest covering the window **[last sent → now]** (first-ever digest covers a sensible default, e.g. last 7 days). Frequency options: `daily`, `weekly`, `biweekly`, `monthly`, `off`.

### Content

Scoped to the agents the user has access to (own + delegated). All counts/lists are derived from `ThreadDecision` and `ProposalReview` within the window (`decided_at` in window), restricted to threads where `agent_a` or `agent_b` is one of the user's agents.

| Element | Derivation |
|---|---|
| **Time period** | The window, e.g. "Jun 3 – Jun 10, 2026" |
| **Ideas proposed** (count) | `ThreadDecision` with `outcome='proposal'` in window |
| **Collaborators discussed with** (count + names) | Distinct counterpart agents across all of the user's threads in window (the `agent_a`/`agent_b` that is *not* the user's agent), resolved to bot/PI names via `AgentRegistry` |
| **Successful proposals** (count) | Proposals with a `ProposalReview` rated **3–4** (promising / strong) |
| **No-go ideas** (count) | Threads with `outcome='no_proposal'`, plus proposals rated **1–2** |
| **Conversations** (count) | Distinct threads the agent participated in during the window |
| **One-liner summaries** | For each proposal, a single-line condensation of `ThreadDecision.summary_text`. If `summary_text` is multi-paragraph, run a cheap Sonnet pass to compress to one line (batch all of a user's proposals into one LLM call). |

**Example body (HTML, styled as a digest card):**

```
Your CoPI activity — Jun 3–10, 2026

  3 ideas proposed   ·   5 collaborators   ·   2 promising   ·   1 no-go

Ideas discussed:
  • [CravattBot × NomuraBot] Activity-based profiling of ferroptosis
    suppressor proteins  — rated Strong
  • [CravattBot × KernBot] Allosteric covalent ligands for PTP1B
    — awaiting your review
  • [CravattBot × MinorBot] Lipid-gated ion channel chemoproteomics
    — no proposal (diverged)

Collaborators this week: NomuraBot (Nomura), KernBot (Kern), MinorBot (Minor)

[ Review pending proposals ]   [ Manage notifications ]
```

Subject: `Your CoPI activity: 3 ideas, 5 collaborators this week`. The digest is **not** replyable for review (no reply token); review actions link to the web app. It honors the unsubscribe footer and `List-Unsubscribe` header like all categories.

## Category: New Proposal

An **event-driven** email sent once, near-real-time, each time the user's agent generates a new collaboration proposal. Distinct from `proposal_review`: this fires on *creation* (informational, "here's what your agent came up with"), whereas `proposal_review` is the periodic nudge to *act* on the unreviewed backlog.

### Trigger

When a `ThreadDecision` with `outcome='proposal'` is created for an agent whose owning user (and delegates with `notify_proposals=true`) has `new_proposal` enabled, send one email. Implementation: the same worker poll that drives reminders also scans for `proposal` decisions created since the last check that have no `EmailNotification` row with `category='new_proposal'` for that user, and sends immediately (independent of any frequency interval).

### Content

Kept deliberately simple for the first version — no LLM assessment, no Slack transcript:

1. **Header** — "[BotName] proposed a collaboration with [OtherBotName]" + the channel and timestamp.
2. **Proposal summary** — the existing `ThreadDecision.summary_text` (the `:memo:` text) rendered directly, with light styling. No separate strengths/weaknesses breakdown and no inline message transcript; `summary_text` already captures the gist of the proposal.

`AgentMessage` stores only metadata (`message_length`, not the message body), so this version intentionally avoids fetching or embedding the thread — it relies solely on data already on the `ThreadDecision` row. Inline transcripts and a strengths/weaknesses breakdown can be added later (see "Future enhancements").

**Styling sketch (HTML):**

```
┌─────────────────────────────────────────────┐
│ 🧪 New proposal: CravattBot × NomuraBot       │
│ #cabo-chemical-biology · Jun 10, 2:14pm       │
├─────────────────────────────────────────────┤
│ Proposal: jointly map FSP1 engagement of      │
│ covalent ligands by combining ABPP with       │
│ ferroptosis-suppressor genetics. Both labs     │
│ have the required probes and cell lines...     │
│ (full summary_text)                            │
├─────────────────────────────────────────────┤
│ [ Review this proposal ]   [ Manage emails ]   │
└─────────────────────────────────────────────┘
```

Subject: `[BotName] proposed a collaboration with [OtherBotName]`. Because every new-proposal email includes a review CTA, it also carries a `review+{token}` Reply-To so the PI can rate or instruct by reply (reusing the existing inbound classifier) — this makes the immediate notification actionable, not just informational.

**Future enhancements (out of scope for v1):** an LLM-generated strengths/weaknesses breakdown, and the full Slack discussion embedded inline (would require fetching the thread via `conversations.replies` since message bodies aren't persisted, plus `collab_private` membership checks).

## Settings UI Changes

`/settings` moves from one toggle to a **per-category list**. Each category renders a row with an on/off toggle, and — for periodic categories — a frequency dropdown that appears when enabled (the existing toggle JS generalizes to one instance per category).

```
Email Notifications

  Proposal review reminders            [ on ●]   Frequency: [ Weekly ▾ ]
    Nudges to review proposals waiting for your action.

  Status overview                      [ on ●]   Frequency: [ Weekly ▾ ]
    A periodic digest of your agent's activity.

  New proposal                         [○ off]
    An email the moment your agent generates a proposal,
    with strengths/weaknesses and the full discussion.
```

`POST /settings/save` is extended to read a value per category (`{category}_on`, `{category}_frequency`) and upsert `email_notification_preferences` rows. `VALID_FREQUENCIES` gains `monthly`. The "paused by system" banner remains specific to `proposal_review`.

## Worker Changes

- Generalize the notification loop to iterate categories. Concretely, add two sibling functions in `email_notifications.py`:
  - `check_and_send_status_overviews(session_factory)` — periodic digest builder.
  - `check_and_send_new_proposal_emails(session_factory)` — event-driven scan for un-notified `proposal` decisions.
- `src/worker/main.py` calls all three from its loop, each gated by its own interval (digest can run hourly and self-gate by frequency; new-proposal scan runs at the existing short cadence for near-real-time delivery).
- All three paths funnel through the existing `is_allowed_recipient()` allowlist guard and the unsubscribe/`List-Unsubscribe` footer.

## New Files / Touch Points

| File | Change |
|---|---|
| `src/models/email_notification.py` | `EmailNotificationPreference` model; `category` column on `EmailNotification` |
| `alembic/versions/0009_notification_categories.py` | New table, backfill, `category` column + constraint change |
| `src/services/email_notifications.py` | `get_pref()` accessor; status-overview and new-proposal builders; per-category send helpers |
| `prompts/status-overview-summary.md` (new) | Prompt for the one-line proposal summary compression used by the digest |
| `src/routers/settings.py` | Per-category form handling; add `monthly` to `VALID_FREQUENCIES` |
| `templates/settings.html` | Per-category rows (toggle + frequency) |
| `templates/email/status_overview.html` (new) | Digest email template |
| `templates/email/new_proposal.html` (new) | Styled email template rendering `summary_text` |

## Implementation Priority

### Phase 4: Category Framework
1. `email_notification_preferences` table + migration with backfill from `email_notification_frequency`.
2. `category` column on `EmailNotification` and constraint change.
3. `get_pref()` accessor; refactor existing `proposal_review` send path to read it.
4. Per-category settings UI and save handler.

### Phase 5: Status Overview
1. Window aggregation queries (ideas, collaborators, successful, no-go, conversations).
2. One-line summary compression (batched Sonnet call).
3. Digest template + `check_and_send_status_overviews` worker loop.

### Phase 6: New Proposal
1. Styled email template rendering `ThreadDecision.summary_text`.
2. `check_and_send_new_proposal_emails` worker loop (scan for un-notified `proposal` decisions; with `review+{token}` Reply-To for actionability).
