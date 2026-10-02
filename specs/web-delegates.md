# Web Delegate System Specification

## Overview

Expands the existing Slack-only delegate system to give delegates full web app access. PIs invite delegates by email from the web UI. Delegates create accounts via ORCID (with full onboarding and profile generation), then gain access to the PI's agent dashboard, proposals, and profile editing — everything the PI can do except managing delegates and deleting the account.

Delegation is a **relationship**, not a user type. A user can be a PI with their own agent, a delegate for one or more other PIs' agents, or both.

## Current State

Delegates are currently Slack-only:
- Stored as Slack user IDs in `AgentRegistry.delegate_slack_ids` (Postgres ARRAY column)
- Added by PI via the agent dashboard: PI enters email → Slack API lookup → Slack user ID stored
- Delegates have full PI powers in Slack (DMs, thread posts, proposal review, standing instructions) except managing other delegates
- The simulation engine treats delegate Slack IDs identically to the primary PI's Slack ID

This spec replaces the Slack-only delegate model with a web-first system where delegation is tied to user accounts, and Slack delegation is derived automatically.

## Data Model

### DelegateInvitation (new table)

Tracks pending and accepted invitations.

| Field | Type | Notes |
|---|---|---|
| id | uuid | Primary key |
| agent_registry_id | FK → AgentRegistry | The agent being delegated |
| invited_by_user_id | FK → User | The PI who sent the invitation |
| email | string | Invitee's email address (lowercase, trimmed) |
| token | string(64) | Unique, cryptographically random URL token |
| status | enum: pending, accepted, expired, revoked | Default: pending |
| accepted_by_user_id | FK → User | Nullable. Set when invitation is accepted. |
| created_at | timestamp | |
| accepted_at | timestamp | Nullable |
| expires_at | timestamp | created_at + 30 days |

**Constraints:**
- Unique on (agent_registry_id, email) where status = 'pending' — prevents duplicate pending invitations to the same person for the same agent
- `token` is unique and indexed

### AgentDelegate (new table)

Represents an active delegation relationship.

| Field | Type | Notes |
|---|---|---|
| id | uuid | Primary key |
| agent_registry_id | FK → AgentRegistry | The agent being delegated |
| user_id | FK → User | The delegate user |
| created_at | timestamp | |
| invitation_id | FK → DelegateInvitation | Nullable. The invitation that created this relationship (null for migrated Slack-only delegates). |
| notify_proposals | boolean | Default true. Whether to email this delegate when proposals are ready for review. Reserved for future email notification system. |

**Constraints:**
- Unique on (agent_registry_id, user_id) — one delegation relationship per user per agent
- ON DELETE CASCADE from both sides: if the agent or user is deleted, the delegation is removed

### Changes to ProposalReview

Add one nullable column for audit tracking:

| Field | Type | Notes |
|---|---|---|
| delegate_user_id | FK → User | Nullable. If the review was submitted by a delegate, this records which delegate. Null means the PI acted directly. |

The existing `user_id` column continues to reference the PI (the agent owner). When a delegate submits a review, `user_id` is set to the PI and `delegate_user_id` is set to the delegate. This preserves the current semantics (one review per agent per thread decision, attributed to the PI) while recording who actually took the action.

### Changes to AgentRegistry

- `delegate_slack_ids` column is **retained** but becomes a derived/synced field. It is recomputed from the AgentDelegate relationships + delegate users' Slack IDs. This maintains backward compatibility with the simulation engine, which reads this column at startup.

### Changes to User

No schema changes. Delegates are regular users. The `agent` relationship (User → AgentRegistry via `user_id`) continues to represent PI ownership. Delegation is accessed via the AgentDelegate join table.

### Relationships

```
User (PI) ──owns──> AgentRegistry ──has many──> AgentDelegate ──refers to──> User (delegate)
                                   ──has many──> DelegateInvitation
```

A User's delegated agents are queried via:
```sql
SELECT agent_registry.* FROM agent_registry
JOIN agent_delegate ON agent_delegate.agent_registry_id = agent_registry.id
WHERE agent_delegate.user_id = :user_id
```

## Invitation Flow

### PI Sends Invitation

1. PI navigates to their agent dashboard (delegate management section)
2. PI enters one or more email addresses and clicks "Invite"
3. For each email:
   a. Validate email format
   b. Check not already an active delegate for this agent
   c. Check no pending invitation for this email + agent combination
   d. Check email is not the PI's own email
   e. Create DelegateInvitation record with cryptographic token and 30-day expiry
   f. Send invitation email via AWS SES

### Invitation Email

**From:** `noreply@copi.science`
**Subject:** `[PI name] invited you to join their lab on CoPI`

**Body:**
- Brief explanation: "[PI name] has invited you as a delegate for [bot name] on CoPI. As a delegate, you can view and manage the lab's AI agent, review collaboration proposals, and provide guidance."
- Accept link: `https://copi.science/invite/{token}`
- Expiry note: "This invitation expires in 30 days."
- No account required note: "You'll sign in with your ORCID account."

### Accepting an Invitation

**Route:** `GET /invite/{token}`

1. Look up DelegateInvitation by token
2. If expired, revoked, or already accepted → show error page with appropriate message
3. If valid:
   a. If user is logged in:
      - Create AgentDelegate relationship
      - Mark invitation as accepted (set `accepted_by_user_id`, `accepted_at`, status = 'accepted')
      - Sync delegate's Slack user ID to `delegate_slack_ids` (if they have a linked Slack account)
      - Redirect to `/agent/{agent_id}/dashboard` with success flash
   b. If user is not logged in:
      - Store token in session (`pending_invite_token`)
      - Redirect to ORCID OAuth login
      - After login (new or existing account), resume: check token, create relationship, redirect to agent dashboard
      - If this is a new user, the invitation acceptance happens after onboarding completes

### Edge Cases

- **Invitee already has a CoPI account:** Token is accepted, AgentDelegate record created, no new account needed. Same email, same link.
- **Invitee is a PI with their own agent:** Works fine — they now have access to both their own agent and the delegated agent.
- **Invitee is already a delegate for this agent:** Invitation is rejected at send time ("already a delegate").
- **Multiple pending invitations from different PIs:** Each is independent. Accepting one doesn't affect others.
- **PI revokes invitation before it's accepted:** Status set to 'revoked', token no longer works.
- **Invitation expires:** Checked at accept time. PI can re-send.

## Revocation

1. PI navigates to delegate management section of agent dashboard
2. PI clicks "Remove" next to a delegate
3. AgentDelegate record is deleted
4. Delegate's Slack user ID is removed from `delegate_slack_ids`
5. Delegate's user account is **not** affected — they retain their CoPI account and any other delegation relationships

## Slack Linkage

> **Removed 2026-10-01 (D16 of `docs/specs/2026-10-01-web-ui-remediation-design.md`).** Nothing
> reads or writes `delegate_slack_ids` any more; the column and its data are kept. The text
> below describes the removed behaviour.

Web delegation implies Slack delegation. When an AgentDelegate relationship is created or removed, the `delegate_slack_ids` column on AgentRegistry is synced:

**On delegation created:**
- Attempt Slack user ID lookup via the Slack API (`users.lookupByEmail`) using the delegate's CoPI email
- If found: append to `delegate_slack_ids` automatically
- If not found: delegation is created without Slack linkage. The delegate sees a notice on the agent dashboard (see below)

**On delegation removed:**
- Remove the delegate's Slack user ID from `delegate_slack_ids` (if present)

**Delegate Slack connection UI:**

When a delegate views an agent dashboard and their Slack user ID is not in that agent's `delegate_slack_ids`, show a notice:

> "To interact with [bot name] in Slack (DMs, thread replies, standing instructions), join the Slack workspace and connect your account."
> - Link to Slack workspace join URL
> - "Connect Slack Account" button: triggers `POST /agent/{agent_id}/delegates/connect-slack`

**Connect Slack endpoint:** `POST /agent/{agent_id}/delegates/connect-slack`
1. Look up the current user's email via Slack API (`users.lookupByEmail`)
2. If found: add the Slack user ID to `delegate_slack_ids`, show success flash
3. If not found: show error "No Slack account found for [email]. Please join the Slack workspace first and use the same email address."

This endpoint is available to delegates at any time — they can join Slack later and connect whenever ready.

**Migration:** Existing Slack-only delegates (IDs in `delegate_slack_ids` without corresponding AgentDelegate records) continue to work. No forced migration — PIs can re-invite via the new system at their own pace.

## Agent Dashboard URL Restructuring

### Current Routes (all under `/agent`)

```
GET  /agent                    → dashboard
GET  /agent/profile            → view private profile
GET  /agent/profile/edit       → edit private profile
POST /agent/profile/save       → save private profile
POST /agent/delegates/add      → add delegate (Slack-only)
POST /agent/delegates/{id}/remove → remove delegate
POST /agent/proposals/{id}/review  → review proposal
POST /agent/proposals/{id}/reopen  → reopen proposal
```

### New Routes (namespaced by agent_id)

```
GET  /agent                              → agent listing / auto-redirect
GET  /agent/{agent_id}/dashboard         → dashboard
GET  /agent/{agent_id}/profile           → view private profile
GET  /agent/{agent_id}/profile/edit      → edit private profile
POST /agent/{agent_id}/profile/save      → save private profile
POST /agent/{agent_id}/delegates/invite  → send delegate invitation(s)
POST /agent/{agent_id}/delegates/{id}/remove → remove delegate
POST /agent/{agent_id}/delegates/connect-slack → link delegate's Slack account
POST /agent/{agent_id}/proposals/{id}/review  → review proposal
POST /agent/{agent_id}/proposals/{id}/reopen  → reopen proposal
GET  /invite/{token}                     → accept invitation
```

### Landing Page (`GET /agent`)

- Query: user's own agent (if any) + all agents delegated to them (via AgentDelegate)
- If exactly one agent → redirect to `/agent/{agent_id}/dashboard`
- If multiple agents → show listing page:
  - "Your Agent" section (if PI): agent name, status, link to dashboard
  - "Delegated Agents" section: list of agents with PI name, bot name, link to dashboard
- If no agents → show "Request an Agent" page (existing behavior)

### Authorization

Every `/agent/{agent_id}/*` route checks that the current user is either:
1. The PI (agent's `user_id` matches session user), OR
2. An active delegate (AgentDelegate record exists for this user + agent)

If neither → 403.

**Write access differentiation:**
- Delegates can do everything except:
  - `POST /agent/{agent_id}/delegates/invite` → 403 for delegates
  - `POST /agent/{agent_id}/delegates/{id}/remove` → 403 for delegates
- The dashboard template hides delegate management UI for delegate users

## Email Infrastructure (AWS SES)

### Setup

1. Verify `copi.science` domain in SES (DNS records: DKIM, SPF, DMARC)
2. Request production access (move out of SES sandbox) to send to unverified recipients
3. Set sender: `noreply@copi.science`

### Configuration

New environment variables:
```
AWS_REGION=us-east-2
AWS_ACCESS_KEY_ID=...          # or use instance IAM role
AWS_SECRET_ACCESS_KEY=...      # or use instance IAM role
SES_SENDER_EMAIL=noreply@copi.science
```

### Implementation

New service: `src/services/email.py`
- `send_delegate_invitation(to_email, pi_name, bot_name, invite_url)` — sends the invitation email via SES
- Uses `boto3.client('ses').send_email()` with both HTML and plain text bodies
- Errors are logged but do not block the invitation creation (invitation record is created regardless; PI can share the link manually)

### Dependencies

Add `boto3` to `pyproject.toml`.

## Migration Plan

### Database Migration (alembic)

`0007_add_web_delegates.py`:
1. Create `delegate_invitations` table
2. Create `agent_delegates` table
3. **Do not** drop `delegate_slack_ids` — it remains as a synced column for the simulation engine

### Backward Compatibility

- The simulation engine continues to read `delegate_slack_ids` at startup (`_load_pi_mappings`). No changes needed to the agent system.
- Existing Slack-only delegates continue to work. The new system is additive.
- The old "add delegate by email" UI (which did Slack lookup) is replaced by the invitation flow. The Slack user ID is resolved automatically when the delegate links their account.

## UI Changes

### Agent Dashboard — Delegate Section

Replace the current "add by email → Slack lookup" form with:

**Active Delegates:**
- Table: name, email, added date, "Remove" button
- Sourced from AgentDelegate join with User

**Pending Invitations:**
- Table: email, sent date, expires date, "Revoke" button
- Sourced from DelegateInvitation where status = 'pending'

**Invite New Delegates:**
- Text input for email addresses (comma-separated or one per line)
- "Send Invitations" button

**Slack-Only Delegates (migration period):**
- If `delegate_slack_ids` contains IDs not matched by AgentDelegate records, show them in a separate "Slack-only delegates" section with a note: "These delegates were added via Slack. They can be re-invited through the web to get full access."

### Agent Listing Page (`/agent`)

Shown when a user has access to multiple agents.

- Simple card layout
- Each card: bot name, PI name, status badge, "View Dashboard →" link
- User's own agent (if any) appears first with a "Your Agent" label
- Delegated agents appear below with "Delegate" label

### Navigation

- The "My Agent" nav link points to `/agent` (which auto-redirects for single-agent users)
- When viewing a delegated agent's dashboard, show a subtle banner: "You are viewing [bot name] as a delegate of [PI name]"

## Implementation Priority

### Phase 1: Data Model + Invitation Flow
1. Database migration (DelegateInvitation, AgentDelegate tables)
2. SES setup and email service
3. Invitation send endpoint
4. Invitation accept endpoint (with ORCID login redirect)
5. Revocation endpoint

### Phase 2: URL Restructuring
1. Refactor all `/agent/*` routes to `/agent/{agent_id}/*`
2. Add authorization middleware (PI or delegate check)
3. Agent listing page at `/agent`
4. Auto-redirect for single-agent users
5. Update all template links

### Phase 3: UI + Polish
1. New delegate management UI (invitations, active delegates)
2. Delegate banner on dashboard
3. Hide delegate-management controls for delegate users
4. Slack-only delegate migration display

### Phase 4: Slack Sync
1. Auto-sync Slack user IDs to `delegate_slack_ids` on delegation create/remove
2. Handle edge case where delegate has no Slack account
