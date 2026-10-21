# Auth and User Management Specification

## Public Landing Page

`GET /` renders a public landing page (`templates/landing.html`) for unauthenticated visitors. It describes what CoPI does — lab agents that discover collaborations in Slack — and notes that the site is in **pre-release**: new users must be added to the system before they can sign in.

The page has two CTAs:
- **Sign in with ORCID** → starts the ORCID OAuth flow (subject to the access gate, see below)
- **Notify me when access opens** → waitlist signup form

Authenticated users hitting `/` are redirected to `/profile` (existing behavior). `/login` continues to work as a direct ORCID entry point for backward compatibility.

## Waitlist

Non-researchers (or researchers who don't want to commit an ORCID login yet) can join a waitlist from the landing page. The waitlist is intentionally separate from the ORCID access gate — they capture different audiences and are reconciled by the admin.

### WaitlistSignup (new table)

| Field | Type | Notes |
|---|---|---|
| id | uuid | Primary key |
| email | string | Required, lowercased, trimmed |
| name | string | Nullable |
| institution | string | Nullable |
| note | text | Nullable. Free-form "tell us about yourself" field. |
| created_at | timestamp | |
| contacted_at | timestamp | Nullable. Set by admin when the person has been notified that access is open. |

**Constraints:** Unique on `email`. Repeat submissions update `created_at` and `note` rather than erroring.

### Submission

`POST /waitlist` accepts the form, validates the email format, upserts the row, and shows a thank-you state. Rate-limited per IP to discourage abuse. No email verification — this is a lead-capture form, not an authentication step.

### Admin

See `admin-dashboard.md` for the `/admin/waitlist` page (list, CSV export, mark-contacted).

## Authentication

ORCID is the only way to create an account and is always available; it cannot be unlinked. A user may also link one work or school Microsoft account (Microsoft Entra ID) as a second way to sign in (see Microsoft sign-in below). No email/password authentication.

### ORCID OAuth Flow

1. User clicks "Sign in with ORCID"
2. Redirect to ORCID OAuth authorization endpoint
3. User authenticates on ORCID
4. ORCID redirects back with authorization code
5. Backend exchanges code for access token (scope: `/authenticate`)
6. Backend fetches user's ORCID ID, name, and email from ORCID Public API (`pub.orcid.org`)
7. If ORCID ID matches an existing User record → log in (handles seeded profile claiming)
8. If no matching User record → create new account

### Session Management

HTTP-only signed session cookie using FastAPI's `SessionMiddleware` (backed by `SECRET_KEY`). Sessions expire after 30 days of inactivity.

### Sessions

- Sign-in stores the account's `users.session_epoch` (NULL reads as 0) in the session as `epoch`. `get_current_user` refuses, and clears, a session whose epoch differs from the stored value.
- Logout, the user's own Microsoft unlink and an admin's removal of a Microsoft link bump the epoch, which ends every session the account holds on every device. Sessions issued before the epoch existed carry none and the column is NULL, so nobody is signed out by the deploy.
- Every sign-in (ORCID or Microsoft) starts a fresh session that keeps only a vetted `post_login_redirect` and a `pending_invite_token`, and deletes the `copi-impersonate` cookie, so a new sign-in does not inherit an impersonation.
- The remembered `next` destination is at most 1024 characters, each printable ASCII other than `"` and `\`, and never starts with `//`; anything else is not remembered. This keeps the session cookie plus the redirect to Microsoft inside nginx's 4 KB upstream-header buffer.
- The invite routes (`GET /invite/{token}`, `POST /invite/{token}/accept`) resolve the signed-in account through the same checks as `get_current_user`, so they honour revocation.

### Implementation

Use **Authlib** for the OAuth 2.0 flow. Register ORCID as an OAuth provider with:
- Authorization URL: `https://orcid.org/oauth/authorize`
- Token URL: `https://orcid.org/oauth/token`
- Scope: `/authenticate`
- User info from: `https://pub.orcid.org/v3.0/{ORCID-ID}/record`

## Microsoft sign-in

Any existing account can link one work or school Microsoft account and use it as a second login, through the same access gate and landing logic as ORCID (pending invite, onboarding, saved destination, `/profile`). Microsoft never creates an account, never writes `users.email` or `users.name`, and the access gate (`users.access_status`) is the only eligibility rule. Only members (`acct` = 0) of the tenants in `MICROSOFT_ALLOWED_TENANT_IDS` are accepted: no personal accounts, no guests. A link is looked up by `(tid, oid)`, never by email.

The feature is off until `MICROSOFT_CLIENT_ID` is set. The routes are always registered and return 404 while it is off.

### Flows

| Route | Purpose |
|---|---|
| `GET /auth/microsoft/start` | Begin sign-in (button on `/login`) |
| `GET /auth/microsoft/link` | Begin linking (link on `/settings`) |
| `GET /auth/microsoft/callback` | The single redirect URI for both |
| `GET /login/start?reauth=1` | ORCID re-sign-in before linking |
| `POST /settings/microsoft/unlink` | User removes their link |
| `POST /admin/users/{user_id}/microsoft/unlink` | Admin removes a user's link |

- **Sign-in.** Authorization-code flow with PKCE, `state` and `nonce` held in the session that started it. The callback validates the ID token (signature from the tenant's keys, issuer, audience, expiry, nonce, `tid` in the allowlist, `oid`, `acct`), finds the link by `(tid, oid)` and ends in the shared sign-in tail. No link → `/login?error=microsoft_not_linked`.
- **Linking** needs a proven ORCID re-sign-in: `/login/start?reauth=1` asks ORCID for `prompt=login`, and the returned ID token's `auth_time` must be at most 300 s old, for the same ORCID iD. The link must then start and complete within 15 minutes of it. Linking and the re-sign-in are refused while an admin is impersonating. One link per user; a Microsoft account can be linked to one CoPI account only.
- **Unlink.** The user's unlink and an admin's removal delete the link and end every session of the account. An admin can view and remove a link, never create one. Settings shows the section to any signed-in, non-impersonated account while the feature is on; the admin removal is on `/admin/users/{id}`.
- Results are fixed codes in the redirect URL (`/login?error=<code>`, `/settings?microsoft=<code>`) mapped to fixed text in code; an unknown code renders nothing.

### Settings

| Setting | Meaning |
|---|---|
| `MICROSOFT_CLIENT_ID` | Application (client) ID. Empty = feature off. |
| `MICROSOFT_REDIRECT_URI` | `<BASE_URL>/auth/microsoft/callback`; must equal the registered URI exactly (case-sensitive). |
| `MICROSOFT_ALLOWED_TENANT_IDS` | Comma-separated tenant GUIDs. |
| `MICROSOFT_CERTIFICATE_B64` | Base64 of the PEM certificate uploaded to the registration. |
| `MICROSOFT_PRIVATE_KEY_B64` | Base64 of the unencrypted PEM RSA private key (at least 2048 bits, matching the certificate); masked in `repr`. |

`create_app()` validates them when `MICROSOFT_CLIENT_ID` is set; an invalid configuration stops the web app at start. The worker, agent and grantbot are unaffected. An expired or not-yet-valid certificate logs an ERROR (Microsoft sign-ins fail, ORCID is unaffected); one within 30 days of expiry logs a WARNING. A change to `.env` needs the `app` container recreated.

### Open decision: admin exposure

Every existing account may link, admins included. Whoever controls a linked Microsoft account, including that tenant's own IT administrators, can therefore sign in as the CoPI account, an admin's too, and impersonate any user. The turn-on precondition is that each allowed tenant enforces MFA for this app. Whether to exclude `is_admin` accounts from Microsoft sign-in is an open owner decision (a one-line change).

### Turn on (runbook)

1. Decide where the app registration lives and list the allowed tenants. For each allowed tenant, confirm with its IT team that it enforces MFA for this app (Conditional Access), and how its consent policy treats `openid profile` for an app from another tenant (users may consent, or an admin must: AADSTS90094).
2. Create the registration: supported account types "Accounts in any organizational directory (multitenant)", platform Web, redirect URI `<BASE_URL>/auth/microsoft/callback` only (https; case-sensitive), implicit grant off. Since June 2026 the "Enable property lock" setting is Enabled by default for new applications; confirm it. Add the optional claim `acct` to the ID token (Token configuration → Add optional claim → ID).
3. Outside the repository: `d=$(mktemp -d) && openssl req -x509 -newkey rsa:2048 -sha256 -days 365 -nodes -keyout "$d/key.pem" -out "$d/cert.pem" -subj "/CN=copi-microsoft-signin"`; upload `$d/cert.pem` under Certificates & secrets → Certificates.
4. Put the five settings in `.env` (`base64 -w0 "$d/cert.pem"`, `base64 -w0 "$d/key.pem"`); `chmod 600 .env`; `rm -r "$d"`.
5. Recreate the web container with both prod compose files (`docker compose -f docker-compose.prod.yml -f docker-compose.override.yml up -d --force-recreate app`), then reload nginx (`... exec -T nginx nginx -s reload`), as `scripts/redeploy.sh` does after recreating `app`.
6. Verify the live checks below with one link and sign-in per allowed organisation before announcing the feature.
7. Calendar reminder 30 days before the certificate expires.

### Live checks before turn-on

- Microsoft accepts the client assertion: PS256, `x5t#S256`, `aud` = the `organizations` token endpoint, `client_id` in the body.
- Microsoft echoes the `nonce` in the code-flow ID token (a missing nonce fails every sign-in closed).
- `acct` arrives once configured, and its JSON type.
- The ID token's JWS header holds only registered members (an unknown member fails every sign-in closed).
- What a user sees under each allowed organisation's consent setting, and whether a refusal ever comes back to the redirect URI.
- ORCID: with an active ORCID session in the browser, `/login/start?reauth=1` shows the password page; the returned `auth_time` is the moment the password was entered (not `iat`); the nonce and `aud` (our client ID) are present. Do not turn linking on unless all hold.

### Renew, move to CA-signed, roll back

Renew: upload the new certificate (a registration holds several), change the two `.env` values, recreate `app`, delete the old certificate. Turn the feature off: clear `MICROSOFT_CLIENT_ID`, recreate `app`; links stay, unused; sessions already signed in through Microsoft stay valid until they expire. If the reason is a flaw in Microsoft sign-in, also rotate `SECRET_KEY`. Roll back the code (or downgrade migration `0031`): the previous image ignores `epoch`, so every session revoked by logout, unlink or removal in the last 30 days works again; rotate `SECRET_KEY` with it if those revocations must hold. Rotating `SECRET_KEY` signs everyone out and invalidates outstanding unsubscribe links.

## Access Gate (Pre-Release)

While CoPI is in pre-release, ORCID login alone is not sufficient — each User has an `access_status` that determines whether they can enter the app.

### User.access_status

| Value | Meaning |
|---|---|
| `allowed` | User can sign in and use the app normally |
| `pending` | User has authenticated via ORCID (or been seeded) but is waiting for admin approval. No session is established and no profile pipeline runs. |
| `denied` | Admin has explicitly denied access. Treated like `pending` from the user's perspective, but admin sees them separately. |

Default for new users: `pending`. Seeded pilot-lab profiles and existing users at the time of migration are backfilled to `allowed`.

### Allowlist

An admin-managed list of pre-approved ORCID IDs (stored in the database, manageable via CLI and admin UI). When a user authenticates via ORCID, if their ORCID is on the allowlist, their User record is created (or updated) with `access_status='allowed'` automatically — they bypass the pending state. The 12 pilot-lab ORCIDs are seeded onto this list.

### Gated Callback Flow

The ORCID OAuth callback runs to completion (we want to capture every attempt), but the post-callback behavior branches on `access_status`:

- **`allowed`** → set session, run profile pipeline if needed, redirect to onboarding or `/profile` (existing behavior)
- **`pending` or `denied`** → do **not** set `user_id` in the session, do **not** enqueue the `generate_profile` job, redirect to `/access-pending`

The `/access-pending` page thanks the user, confirms their ORCID was received, and offers a form to capture an email address (since ORCID may not return one) so the admin can notify them when access opens. The captured email is written to the `User.email` field.

### Admin Approval

Admins approve pending users via `/admin/access-requests` (see `admin-dashboard.md`). Approving a user:
1. Sets `access_status` to `allowed`
2. Enqueues a `generate_profile` job (skipped if the user already has a profile)
3. Sends a notification email to the user that access is open (via the existing SES email service)

## Signup / Onboarding Flow

Account creation happens automatically on first ORCID login (subject to the access gate above):

1. **ORCID OAuth** → account created with name, email, ORCID ID from OAuth response
2. **Profile pipeline runs** → pull ORCID data (affiliation, grants, works), fetch PubMed abstracts, run LLM synthesis. Show progress indicator: "Pulling your publications... Analyzing your research... Building your profile..."
3. **Review generated profile** → user sees their synthesized research summary, techniques, models, disease areas, key targets. User can edit any field directly.
4. **Review and edit seeded private profile / agent instructions** — LLM generates a draft private profile (agent instructions) from the user's research data; user reviews and edits before saving
5. **Onboarding complete** → `onboarding_complete` set to true, redirect to profile page

If the user closes during steps 2-4, they resume from where they left off on next login.

### Seeded Profile Claiming

When a user logs in with ORCID and their ORCID ID matches a seeded (unclaimed) profile:
- The existing User record is linked to their OAuth session
- `claimed_at` is set to now
- Their profile is already generated — show it for review/editing (step 3)
- Proceed to onboarding step 4

## Profile Management

### Viewing and Editing

Users can view and directly edit all synthesized profile fields:
- Research summary (text area)
- Techniques (editable tag list)
- Experimental models (editable tag list)
- Disease areas (editable tag list)
- Key targets (editable tag list)
- Keywords (editable tag list)
- Grant titles (from ORCID, displayed but not directly editable — user should update on ORCID)

Edits save immediately (AJAX or form post) and bump `profile_version`.

### Profile Refresh

**Manual:** User can click "Refresh profile" to re-run the full pipeline (fetch ORCID data, fetch publications, re-synthesize). Enqueues a `generate_profile` job.

**Automatic (monthly):**
1. Cron job re-fetches ORCID works for all users
2. Diffs against stored publications
3. If new publications found: runs synthesis pipeline to generate candidate profile
4. Compares candidate profile arrays against current profile
5. NOT IMPLEMENTED as a review queue: `pending_profile` has no writer (issue #22 V6-pend).
   A differing candidate is written directly to the live profile fields, gated by
   `_validate_profile`.
6. If no arrays changed: stores new publications but does not bother user
7. (removed — no side-by-side review exists to accept/edit/dismiss)
8. (removed — nothing is staged, so nothing auto-dismisses)

## Settings

Accessible at `/settings`:
- Email notification frequency (daily, twice a week, weekly, every two weeks, off)
- Status indicator: "Active" or "Paused by system — review pending proposals to reactivate"
- Account deletion link

Additional settings are accessible from other pages:
- Display name and institution (editable from `/profile/edit`)
- Private profile / agent instructions (editable from `/agent/{agent_id}/profile/edit`)
- Delegate management (from `/agent/{agent_id}/dashboard`)

### Delegate Management

PIs can grant delegate access to additional Slack accounts from the settings page. Delegates have full PI powers — DMs, thread posts, proposal review, standing instructions — except they cannot add or remove other delegates.

- **Add delegate:** PI enters the delegate's email address. The system looks up the corresponding Slack user ID via the Slack API (`users.lookupByEmail`). The Slack user ID is appended to the agent's `delegate_slack_ids` array.
- **Remove delegate:** PI removes a delegate from the list. The Slack user ID is removed from `delegate_slack_ids`.
- **Restrictions:** Only the primary PI (the account linked via `AgentRegistry.slack_user_id`) can manage delegates. Delegates cannot add or remove other delegates.

## Account Deletion

The settings page should:
1. Explain what will be deleted (profile, publications, all submitted texts)
2. Require confirmation (type "delete" or similar)
3. On confirmation: delete User, ResearcherProfile, Publications, Jobs

## Admin Functions

### Seed Profiles

Admin provides a list of ORCID IDs (via admin panel or CLI). For each:
1. Create User record with ORCID ID, name, and affiliation from ORCID API
2. Enqueue `generate_profile` job
3. Profile is visible in admin dashboard once generated

**CLI:**
```bash
python -m src.cli seed-profile --orcid 0000-0002-1825-0097
python -m src.cli seed-profiles --file orcids.txt
```

### Grant/Revoke Admin

```bash
python -m src.cli admin:grant --orcid 0000-0002-1825-0097
python -m src.cli admin:revoke --orcid 0000-0002-1825-0097
```

### Admin Impersonation

Admins can view the app as any user for testing and debugging. See `admin-dashboard.md` for the full impersonation flow.

- `POST /admin/impersonate` with an ORCID — sets `copi-impersonate` cookie
- Auto-creates unclaimed users for ORCIDs not yet in the system
- `get_current_user` dependency checks the cookie and returns the impersonated user
- Impersonated user's profile is eagerly loaded to prevent async lazy-load errors
- Agent badge in nav shows the impersonated user's notification count
- 24-hour cookie expiry
