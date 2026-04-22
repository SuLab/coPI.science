# Admin Dashboard Specification

## Overview

A read-only admin dashboard for monitoring platform activity and inspecting data. Accessible only to users with `is_admin = true` at `/admin`. Server-rendered with Jinja2 templates, minimal styling using Tailwind.

## Access Control

- All `/admin/*` routes check `is_admin` on the session user. Non-admins get a 403.
- The admin link is only shown in the nav for admin users.
- `is_admin` is set via CLI only — no self-service admin promotion.

## Dashboard Pages

### 1. Users Overview (`/admin/users`)

Default landing page. Table of all users.

**Columns:**
- Name (with department as subtext)
- Institution
- ORCID (linked to orcid.org)
- Profile status: `no_profile` | `generating` | `complete` | `pending_update`
- Agent status: `not_requested` | `awaiting_token` | `active` | `suspended`
- Publication count
- Profile version
- Claimed (date or "No")
- Joined date

**Filters:**
- Profile status
- Institution
- Claimed vs. unclaimed

**Row click** → user detail page.

### 2. User Detail (`/admin/users/{id}`)

Full view of a single user's data.

**Sections:**

**Account:**
- Name, email, ORCID, institution, department
- Admin status, onboarding complete, claimed_at

**Profile:**
- Research summary
- Techniques, experimental models, disease areas, key targets, keywords
- Grant titles
- Profile version and generation timestamp
- Pending profile (if any) — show side-by-side diff vs current

**Publications:**
- Table: title, journal, year, author position, PMID/DOI links
- Whether methods text was extracted (yes/no)

**Jobs:**
- All jobs for this user: type, status, attempts, enqueued/completed timestamps, last error

### 3. Job Queue (`/admin/jobs`)

All jobs across all users.

**Summary counts** (above table): total, pending, processing, completed, failed, dead

**Columns:**
- Type (Generate Profile, Monthly Refresh)
- Status (color-coded badge: pending=yellow, processing=blue, completed=green, failed=red, dead=gray)
- User name
- Attempts (N/maxN)
- Enqueued timestamp
- Completed timestamp
- Last error (truncated, shown for failed/dead)

**Filters:**
- Status
- Type

**Sortable:** Type, Status, Enqueued, Completed

### 4. Agent Activity (`/admin/activity`)

Analytics on Slack simulation runs.

**Summary cards:**
- Total simulation runs
- Total messages sent (all time)
- Total channels created (all time)
- Most active agent (by message count, all time)

**Simulation Runs Table:**
- Start time, end time, status
- Total messages, total API calls
- Config summary (max runtime, budget cap)
- Row click → run detail

**Run Detail (`/admin/activity/{run_id}`):**

Per-run breakdown:

*Messages by agent* — table: agent name, message count, avg message length

*Messages by channel* — table: channel name, message count, which agents participated

*Channels created this run* — table: channel name, type (thematic/collaboration), created by agent, archived (yes/no)

*Message timeline* — ordered list of all messages: timestamp, agent, channel, first 100 chars of message

### 5. Agents (`/admin/agents`)

Manage agent registrations and lifecycle.

**Sections:**
- **Pending agents** — requests awaiting admin approval. Actions: approve, reject.
- **Active agents** — currently active agents with Slack connection status
- **Suspended agents** — agents that have been suspended

**Per-agent info:**
- Agent ID, bot name, PI name
- Linked user
- Status
- Slack token availability (env tokens detected)
- Proposal and review counts

### 6. Discussions (`/admin/discussions`)

Analytics on agent-to-agent thread conversations and outcomes.

**Columns:**
- Date/time
- Channel
- Agent A, Agent B
- Outcome (proposal, no proposal, timeout)
- Summary text (truncated)

**Filters:**
- Agent filter (multi-select)
- Outcome filter

**Export:** HTML and plain text export options for proposal review.

### 7. Matchmaker (`/admin/matchmaker`)

Admin tool for generating collaboration proposals between two PIs on demand, without running an agent simulation. See `labbot-matchmaker.md` for the full specification.

**Generate form (top of page):**
- Two dropdowns listing all users with a complete `ResearcherProfile`, sorted by name
- Client-side enforcement: same user cannot be selected in both dropdowns; Generate button disabled until both are selected
- On submit: POSTs to `/admin/matchmaker/generate`, shows spinner, redirects to detail view on success

**Proposals table:**
- Confidence badge (High = green, Moderate = yellow, Speculative = gray)
- PI A / PI B names
- Proposal title (truncated)
- Generated timestamp
- View / Delete actions

**Filters:**
- PI multi-select (matches either side)
- Confidence filter

**Row click** → proposal detail page (`/admin/matchmaker/{id}`)

**Proposal detail (`/admin/matchmaker/{id}`):**
- Header: PI A × PI B, confidence badge, generated timestamp, token counts
- Full proposal rendered as markdown
- Delete button

### 8. LLM Call Logs (`/admin/llm-calls`)

Debugging view for all LLM API calls.

**Columns:**
- Timestamp
- Agent ID
- Phase
- Model
- Input/output tokens
- Latency (ms)
- System prompt and response (expandable)

### 9. Access Requests (`/admin/access-requests`)

Pre-release access gate management.

**Sections:**
- **Pending** — users with `access_status='pending'`. Columns: name, email, ORCID, institution (if known), created_at, "Approve" / "Deny" buttons.
- **Allowed** — recently approved users, for confirmation. Columns: name, email, ORCID, approved date.
- **Denied** — users explicitly denied.

**Actions:**
- `POST /admin/access-requests/{user_id}/approve` — sets `access_status='allowed'`, enqueues `generate_profile` job (if no profile yet), sends notification email
- `POST /admin/access-requests/{user_id}/deny` — sets `access_status='denied'`

**Allowlist management** (same page or sub-page):
- List of pre-approved ORCIDs from `AccessAllowlist`
- Add ORCID + note form
- Remove ORCID button

### 10. Waitlist (`/admin/waitlist`)

Lead-capture signups from the public landing page.

**Columns:** email, name, institution, note (truncated), created_at, contacted_at, "Mark contacted" button

**Actions:**
- `GET /admin/waitlist/export` — CSV export of all signups
- `POST /admin/waitlist/{id}/mark-contacted` — sets `contacted_at = now()`

No outbound email is sent automatically — the admin uses the export to reach out manually, then marks rows contacted.

### 11. User Impersonation

Admins can assume the identity of any user to see the app as they see it.

**Entry point:** ORCID text input + "Go" button in admin header, available on all `/admin/*` pages.

**Flow:**
1. Admin enters ORCID, clicks Go
2. `POST /api/admin/impersonate` validates ORCID, looks up user, sets `copi-impersonate` httpOnly cookie
3. If ORCID doesn't exist, fetch from ORCID public API and create a User record (unclaimed)
4. Admin is redirected to `/` and sees the app as that user
5. Amber banner at top of every page: "Impersonating [Name] (ORCID)" with "Stop Impersonating" button
6. Stop calls `DELETE /api/admin/impersonate`, clears cookie, redirects to `/admin`

**Implementation:**
- `copi-impersonate` cookie stores target user's database ID
- Session middleware checks this cookie; if present and session user has `is_admin`, overrides session user identity
- Cookie expires after 24 hours, httpOnly, secure in production, sameSite=lax

## API Routes

| Route | Purpose |
|---|---|
| `GET /admin/users` | Users overview |
| `GET /admin/users/{id}` | User detail |
| `GET /admin/jobs` | Job queue |
| `GET /admin/activity` | Agent activity overview |
| `GET /admin/activity/{run_id}` | Simulation run detail |
| `GET /admin/agents` | Agent registry management |
| `POST /admin/agents/{id}/approve` | Approve pending agent |
| `GET /admin/discussions` | Thread discussions and outcomes |
| `GET /admin/discussions/export` | Export discussions (HTML/text) |
| `GET /admin/matchmaker` | Matchmaker tab with generate form and proposals table |
| `POST /admin/matchmaker/generate` | Run LLM pipeline and store result |
| `GET /admin/matchmaker/{id}` | Proposal detail view |
| `POST /admin/matchmaker/{id}/delete` | Delete a proposal |
| `GET /admin/access-requests` | Pending access requests + allowlist management |
| `POST /admin/access-requests/{user_id}/approve` | Approve a pending user |
| `POST /admin/access-requests/{user_id}/deny` | Deny a pending user |
| `POST /admin/access-allowlist/add` | Add an ORCID to the allowlist |
| `POST /admin/access-allowlist/{id}/remove` | Remove an ORCID from the allowlist |
| `GET /admin/waitlist` | Waitlist signups |
| `GET /admin/waitlist/export` | CSV export of waitlist |
| `POST /admin/waitlist/{id}/mark-contacted` | Mark a waitlist signup as contacted |
| `POST /admin/impersonate` | Start impersonating a user |
| `POST /admin/impersonate/stop` | Stop impersonating |

## Design Principles

- **Read-only.** No edit/delete/trigger actions in v1 (except impersonation). Admin actions stay in CLI.
- **Server-rendered.** Jinja2 templates, no client-side data fetching.
- **Minimal styling.** Tailwind. Tables, basic badges. No charts library — just numbers and text.
- **No pagination in v1.** For pilot scale, load all data. Add pagination later.
