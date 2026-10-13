# Microsoft sign-in for staff — design

Date: 2026-10-13 · Revision 2 (after literature review and two adversarial audits) · Status: draft,
all owner decisions made (§2.1) · Code baseline: `blackbird` @ `b62eba5d`, alembic head `0065`

## 1. Goal

Admins, managers and reviewers can sign in with a work or school Microsoft account as a **second**
way into the account they already have. ORCID stays the only way to create an account and stays
available to everyone. PIs are unaffected.

Success: a linked user can use either button on `/login` and lands in the same account, with the
same role, access gate, session epoch, sign-out and landing page as an ORCID sign-in.

## 2. Decisions

### 2.1 Owner decisions (2026-10-13)

| # | Question | Decision |
|---|---|---|
| D1 | What is Microsoft sign-in for? | A second login linked to an existing ORCID account. Microsoft never creates an account. |
| D2 | Which roles? | `admin`, `manager`, `reviewer` — defined once as `MICROSOFT_SIGN_IN_ROLES`. This is **not** `User.is_staff`, which excludes reviewers (`src/models/user.py:160-167`). Not `pi`. |
| D3 | Which Microsoft accounts? | Organisational accounts from a configured list of tenants. No personal accounts. |
| D4 | Where does the app registration live? | Undecided; all of it is configuration. Decide before turning the feature on (§7.2 explains what each choice means for consent). |
| D5 | Who links and unlinks? | The user, from Settings, while signed in with ORCID. Admins can view and remove a link, not create one. |
| D6 | Library | Extend authlib (already the ORCID client) and validate the ID token with joserfc. Not MSAL, not hand-written HTTP. |
| D7 | Credential | Certificate (`private_key_jwt`), not a client secret. |
| D8 | Certificate | Self-signed for now, CA-signed later (configuration-only change). |
| D9 | Flow details | Linking requires a recent ORCID sign-in; admin removal is admin-only and ends all the user's sessions; a user changed to `pi` keeps the link but cannot use it. |
| D10 | Strength of "sign in with ORCID again" (was O1) | Strong: the reauth request uses `scope=openid&prompt=login`, and the callback validates ORCID's ID token and requires `auth_time` under 5 minutes old (§5.5.1). |
| D11 | What a user's own unlink ends (was O2; replaces D9's "other sessions") | Every session, including the current one; the user signs in again. |
| D12 | Guest accounts (was O3) | Members only: the registration adds the optional ID-token claim `acct`, and the app requires `acct == 0`. |

### 2.2 Decisions made in this spec (flag any you disagree with)

- **S1** A missing or invalid Microsoft configuration makes the **web app** refuse to start. The check
  runs in `create_app()`, not in `Settings`: all three services load the same `.env` into `Settings`
  (`docker-compose.prod.yml` `env_file`, `src/config.py:103`), so a `Settings` validator would also
  stop the worker and the agent. (The `SECRET_KEY` guard *is* a `Settings` validator and does affect
  every process; this check deliberately is not.) A bad configuration therefore takes the whole web
  app down at start, ORCID sign-in included; rollback is clearing `MICROSOFT_CLIENT_ID`.
- **S2** The certificate's `x5t#S256` header is computed from the certificate in `.env`, not typed in.
- **S3** One linked Microsoft account per user. No email notification when a link is added (risk in §6).
- **S4** Delegate invitations keep sending a signed-out visitor to ORCID (`src/routers/invite.py:151`).
- **S5** An admin may remove their own link from the admin pages; it ends their own sessions, as for
  anyone else.
- **S6** While impersonating, the Settings section is hidden and every Microsoft route refuses
  (`refuse_impersonation`), because impersonation keeps the admin's own session keys
  (`src/dependencies.py:150-160`): the admin's fresh sign-in must never count for the impersonated user.

### 2.3 Open — owner decisions needed

- **O1** — decided, see D10.
- **O2** — decided, see D11.
- **O3** — decided, see D12.

## 3. Non-goals

Personal Microsoft accounts; creating accounts from Microsoft; Microsoft sign-in for PIs; calling
Microsoft Graph; Microsoft single sign-out; an admin UI for the tenant list; storing or using the
Microsoft `email` claim; Graph's beta `signInAudienceRestrictions` (beta, at most 20 tenants, and
Microsoft says it "isn't a replacement" for checking the tenant in the app).

## 4. Constraints from the current code (verified 2026-10-13)

- ORCID is the only provider. `users.orcid` is `String(50) NOT NULL UNIQUE` and stays so: under D1
  every account has an ORCID. A PI without one could never finish onboarding
  (`src/services/profile_pipeline.py:252-254`); D1/D2 avoid that.
- Sessions: Starlette `SessionMiddleware`, cookie `__Host-copi-session`, signed but not encrypted,
  stored entirely client-side, `SameSite=lax`, 30 days (`src/main.py:283-290`). "Popping" a session
  key is therefore not server-enforced single use: an older copy of the cookie can be replayed. A
  Microsoft callback is single-use because Microsoft's authorization code is (about one minute, one
  redemption) and PKCE binds it to the verifier.
- `session["user_id"]` is written in one place, `_post_login_redirect` (`src/routers/auth.py:275`),
  which has one caller. `get_current_user` refuses a session whose `epoch` differs from
  `users.session_epoch`; `bump_session_epoch` returns `None` (`src/services/session_epoch.py:42-58`).
- `OriginGuardMiddleware` refuses cross-site non-GET; only `POST /api/csp-report` is exempt. So the
  callback is a GET (`response_mode=query`); `form_post` would be refused and the Lax cookie not sent.
  Enforced CSP `form-action 'self' https://slack.com https://*.slack.com`: every Microsoft round trip
  starts from a plain link.
- `/login` and `/login/start` redirect a signed-in user to `/` (`src/routers/auth.py:147-148,163-164`).
- `/admin/users/{id}` redirects a `pi` target to the workspace PI page (`src/routers/admin/users.py:85-86`),
  whose admin-only account block is `templates/workspace/pi_detail.html:272-326`.
- `Settings` reads `.env` in every service and `get_settings()` is `lru_cache`d. pydantic-settings
  JSON-decodes list-typed env values, so list settings are `str` fields parsed by hand
  (`src/config.py:168-169`). The host test suite reads the production `.env`
  (`tests/conftest.py:210-215`), so tests must not depend on it.
- Feature switches follow `application.state.assessment_chat_enabled` (`src/main.py:276`), which tests
  flip on `asgi_app.state`.
- `admin_audit_events`: `action` String(60), `actor_user_id` nullable FK, `payload` JSONB; no CHECK on
  `action`; payload values must be JSON-native (no `uuid.UUID`). `record_audit` and
  `mark_email_verified` commit on their own.
- authlib: used only in `src/routers/auth.py`; images install `pip install .` with `authlib>=1.3.0`
  and no lockfile (`Dockerfile:43`, `python:3.11-slim`). In 1.7.2, `PrivateKeyJWT` defaults to
  `exp = iat + 3600`, no `nbf`, a 36-character random `jti` written into the instance's `claims` dict
  (so a reused instance reuses it), and adds only `client_assertion*` to the body, not `client_id`. A
  string `token_endpoint_auth_method` that was never registered raises `TypeError` at use. authlib's
  `parse_id_token` cannot handle the multi-tenant issuer template.

## 5. Design

### 5.1 Data model — migration `0066`, additive

New table `user_microsoft_identities` (model `src/models/microsoft_identity.py`):

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `user_id` | UUID FK `users.id` `ON DELETE CASCADE`, `UNIQUE` | one link per user (S3) |
| `tenant_id` | UUID NOT NULL | `tid` |
| `object_id` | UUID NOT NULL | `oid` |
| `sign_in_name` | String(255) NULL | `preferred_username`; display only |
| `linked_at` | timestamptz NOT NULL, server default now() | |
| `last_sign_in_at` | timestamptz NULL | |

`UNIQUE (tenant_id, object_id)`. Lookups use `(tenant_id, object_id)` only (Microsoft: "use the
immutable claim values `tid` and `oid` as a combined key"). No relationship is declared on `User`
(the `PiCompany` / enrichment pattern), so deleting a user removes the row through the database
cascade and `delete_user_account` is unchanged. A new table with NOT NULL columns follows `0042`.

### 5.2 Configuration

All `str`, default `""`:

| Setting | Masked in `repr` | Meaning |
|---|---|---|
| `MICROSOFT_CLIENT_ID` | no | Application (client) ID. Empty = feature off. |
| `MICROSOFT_REDIRECT_URI` | no | e.g. `https://blackbird.copi.science/auth/microsoft/callback`; must equal the registered URI exactly. |
| `MICROSOFT_ALLOWED_TENANT_IDS` | no | Comma-separated tenant GUIDs. |
| `MICROSOFT_CERTIFICATE_B64` | no | Base64 of the PEM certificate uploaded to the registration. |
| `MICROSOFT_PRIVATE_KEY_B64` | yes (`key`) | Base64 of the PEM RSA private key. |

`create_app()` builds a validated, immutable `MicrosoftSignInConfig` and stores it on
`application.state.microsoft_sign_in` (`None` = off). When `MICROSOFT_CLIENT_ID` is set it requires:
every other setting present; a non-empty list of GUIDs; an RSA key of at least 2048 bits (Entra
"currently supports only RSA") matching the certificate's public key; an `https` redirect URI unless
`ALLOW_HTTP_SESSIONS=true`. A certificate within 30 days of expiry logs a warning. The routes are always
registered; every handler 404s while the state is `None`. Changes need `blackbird-app` recreated.

### 5.3 Routes and flows

Routes in a new `src/routers/microsoft_auth.py`, protocol and token logic in
`src/services/microsoft_identity.py`:

| Route | Purpose |
|---|---|
| `GET /auth/microsoft/start` | begin sign-in (button on `/login`) |
| `GET /auth/microsoft/link` | begin linking (button on `/settings`) |
| `GET /auth/microsoft/callback` | the single redirect URI for both |
| `POST /settings/microsoft/unlink` | user removes their link |
| `POST /admin/users/{user_id}/microsoft/unlink` | admin removes a user's link |

**Session keys.** `microsoft_flow` = `{state, nonce, code_verifier, purpose ("signin"|"link"),
user_id (link only), started_at}`, popped at the callback and refused after 10 minutes. It is separate
from ORCID's `oauth_state`. `_post_login_redirect(request, user, via)` also writes `signed_in_at`
(Unix seconds) and `signed_in_via` (`"orcid"`/`"microsoft"`); a session without them counts as stale.

**Shared sign-in tail.** The end of the ORCID callback (`src/routers/auth.py:380-400`: `last_login_at`,
commit, `_start_fresh_session`, `pending_access`, `_post_login_redirect`) becomes
`complete_sign_in(request, db, user, via)`, called by both callbacks. `pending_access` is built by one
helper also used by `src/dependencies.py:121-126` (`"orcid": user.orcid`). ORCID-only work stays in the
ORCID callback: `fetch_orcid_profile`, the allowlist lookup, `_find_or_create_user`.

**Sign-in.** `/auth/microsoft/start` redirects a signed-in user to `/`, otherwise stores
`microsoft_flow` and redirects to Microsoft (§5.4). The callback, after §5.5:

1. finds the link by `(tid, oid)`; none → `/login?error=microsoft_not_linked`;
2. refuses a role outside `MICROSOFT_SIGN_IN_ROLES` → `/login?error=microsoft_not_staff`;
3. sets `last_sign_in_at` and calls `complete_sign_in(..., via="microsoft")` (a non-`allowed` account
   goes to `/access-pending` exactly as with ORCID).

It never creates a user and never writes `users.email` or `users.name`.

**Linking.** The Settings section shows only for an allowed account with a role in
`MICROSOFT_SIGN_IN_ROLES`, and not while impersonating (S6). `/auth/microsoft/link` and the link
callback use `get_current_user` and `refuse_impersonation`, never the raw `session["user_id"]`, and
require `signed_in_via == "orcid"` and an `orcid_reauth_at` under 15 minutes old. Only the reauth
callback below writes `orcid_reauth_at`, after its §5.5.1 checks pass; an ordinary ORCID sign-in, which
can succeed on ORCID's remembered browser session without a password, never does (the audit of
2026-10-13 found the earlier `signed_in_at` test let it through). When stale, the button is
"Sign in with ORCID again to link" → `GET /login/start?reauth=1`. Unlike today's `/login/start`, it
proceeds for a signed-in user (refused while impersonating), stores `/settings` as
`post_login_redirect`, and stores `{state, nonce, reauth: true}` for the ORCID callback. It asks ORCID
for `scope=openid`, `prompt=login` and that `nonce` (D10); ordinary ORCID sign-in keeps
`scope=/authenticate` unchanged. The ORCID callback, when the stored flow is a reauth, requires an
`id_token` in the token response and validates it (§5.5.1); it also requires the returned ORCID iD to
be the signed-in user's. On failure the current session is left as it was, and the user gets a flash
on `/settings`. On success the callback continues as a normal ORCID login (fresh session, new
`signed_in_at`, `signed_in_via="orcid"`), then writes `orcid_reauth_at` into that fresh session. Notes: an admin with `onboarding_complete=False` passes
through `/onboarding` first; a pending invite token takes priority.
The link request sends `prompt=select_account`. The callback, after §5.5, re-checks the current user
equals the stored `user_id` and is still eligible, then inserts the row inside `begin_nested()`;
`IntegrityError` (concurrent link, or the account/user already linked) maps to "this Microsoft account
is already linked to a Blackbird account" or "unlink your current Microsoft account first". Success
and every link-flow failure → `flash(...)` and `/settings`.

A cross-site page can make a signed-in user open `/auth/microsoft/link` (a GET); the only outcome is
linking a Microsoft account that person picks and signs in to. An attacker's code cannot be injected
into the victim's flow: `state`, `nonce` and the PKCE verifier live in the session that started it.

**User unlink** (same-origin form; impersonation refused; D11): deletes the row and calls
`bump_session_epoch(db, user.id, expected_epoch=<session epoch>)` as `POST /logout` does, commits,
clears the session, flashes "Microsoft sign-in removed. Sign in again." (flashes render on every page
through `base.html`), and redirects to `/login`. `bump_session_epoch` is unchanged: nothing reads the
epoch back, so its `None` return and the ORM's expiry of `session_epoch` do not matter. A concurrent
bump that makes the conditional update match nothing still leaves every session ended.

**Admin removal** (`get_admin_user`; impersonation refused): in one transaction delete the row, bump
the user's epoch, add `AdminAuditEvent(action="microsoft_unlink", actor_user_id=admin.id,
payload={"user_id": str, "tenant_id": str, "sign_in_name": str | None})`, then commit once (not
`record_audit`, which commits itself). The row also appears in `/admin/simulation`'s "Recent admin
actions". Redirect to `/admin/users/{id}`, which re-routes PI targets.

**Role change to `pi`:** the link stays; sign-in step 2 refuses it; Settings hides it; the admin can
still see and remove it on the workspace PI page's admin block (§5.7).

### 5.4 Protocol

Fixed endpoints (live discovery documents checked 2026-10-13):

- authorize `https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize`
- token `https://login.microsoftonline.com/organizations/oauth2/v2.0/token`
- keys, per allowed tenant: `https://login.microsoftonline.com/{tid}/discovery/v2.0/keys`

Authorization request: `client_id`, `response_type=code`, `response_mode=query`,
`scope=openid profile`, `redirect_uri`, `state`, `nonce`, `code_challenge` (S256 of a fresh 43+
character verifier), `code_challenge_method=S256`; `prompt=select_account` for linking.

Token request: `grant_type=authorization_code`, `code`, `redirect_uri`, `code_verifier`, `client_id`
(passed explicitly; Microsoft lists it as required and authlib's `PrivateKeyJWT` does not add it), and
a certificate assertion. For each request build a new `PrivateKeyJWT(alg="PS256",
headers={"typ": "JWT", "x5t#S256": <base64url SHA-256 of the cert DER>}, claims={"jti": uuid4 hex,
"iat": now, "nbf": now, "exp": now + 300})`, so `aud` is the token endpoint actually called, and pass
it to a fresh `AsyncOAuth2Client` (registered with `register_client_auth_method`, or as the auth
instance), closed after use. This matches MSAL Python's own assertion (`audience=authority.token_endpoint`,
PS256 with `x5t#S256`, 600 s, UUID `jti`) and Microsoft's "5-10 minutes after `nbf` at most".

The token response also carries an access token for Graph's userinfo endpoint; it is ignored and never
parsed. `interaction_required` from the token endpoint is reported as "Microsoft needs you to sign in
again" with a retry link to the start route.

### 5.5 ID token validation

Implemented with joserfc in one pure function `(token, keys_for, config, nonce, now) → claims`.
Every step must pass:

1. Parse the header and claims without trusting them; header `alg` must be `RS256` (pinned in code —
   Microsoft's keys carry no `alg`); `none` and anything else are rejected.
2. `tid` is a GUID and in `MICROSOFT_ALLOWED_TENANT_IDS` — checked **before** any key fetch, so random
   tenants cause no outbound requests.
3. Signature verifies with the key whose `kid` matches, from that tenant's key set. Keys are cached per
   tenant, refreshed hourly with a 24-hour TTL (Microsoft's key-rollover guidance), and refetched on an
   unknown `kid` at most once per 5 minutes.
4. `iss == "https://login.microsoftonline.com/" + tid + "/v2.0"`, and the signing key's `issuer` equals
   `iss` (tenant key sets carry the concrete issuer; if a key carries the `{tenantid}` template it is
   substituted case-insensitively, and a key with neither fails closed).
5. `aud` is `MICROSOFT_CLIENT_ID` (Microsoft: in ID tokens "the `aud` claim is always the client ID";
   a one-element list holding it is also accepted, since OIDC allows arrays).
6. `exp`, `nbf`, `iat` hold with 300 seconds of leeway.
7. `nonce` equals the session's.
8. `oid` is present and a GUID.
9. `acct` is present and equals `0` or `"0"` (member). A guest (`1`) or a missing `acct` — the registration's
   optional claim not configured — is rejected, so a misconfigured registration fails closed (D12).

The signature check is kept although OIDC Core §3.1.3.7(6) allows relying on TLS for a token received
straight from the token endpoint; Microsoft says confidential clients "should validate ID tokens". The
cost is a dependency on the keys endpoint, cached as above.

#### 5.5.1 ORCID reauth ID token (D10)

From ORCID's live discovery document (2026-10-13): issuer `https://orcid.org`, keys
`https://orcid.org/oauth/jwks` (one RSA key, no `alg`), `id_token_signing_alg_values_supported:
["RS256"]`, `claims_supported` includes `auth_time` and `sub`. ORCID's OpenID Connect page: `sub` is
the ORCID iD; the token response still carries `orcid` and `name` beside `id_token`; `prompt=login`
"force[s] them to reauthenticate"; the `nonce` "will be returned in the id_token". Checks, all required:
header `alg == "RS256"`; signature with the matching `kid` from ORCID's key set (cached 24 hours,
refetched on an unknown `kid` at most once per 5 minutes); `iss == "https://orcid.org"`; `aud` is
`ORCID_CLIENT_ID` (or a one-element list holding it); `exp`/`iat` with 300 s leeway; `nonce` matches;
`sub == token["orcid"] ==` the signed-in user's `users.orcid`; `auth_time` present and no more than
300 seconds before now. Same joserfc helper as §5.5, separate key cache.

### 5.6 Errors and logging

Sign-in failures → `/login?error=<code>` with new codes mapped in `templates/login.html`. Link, unlink
and reauth failures → `flash(..., "error")` and `/settings` (a signed-in user sent to `/login` is
redirected to `/` and would lose the message).

| Situation | User sees | Logged |
|---|---|---|
| `error=access_denied` on the redirect | "Microsoft sign-in was cancelled or not approved." | `error` |
| any other `error` on the redirect | "Microsoft sign-in failed. If your organisation must approve Blackbird first, ask its IT team." | `error`, `error_description` |
| state missing, mismatched or expired | "Security check failed. Please try again." | — |
| token exchange or key fetch failure | "Microsoft sign-in failed. Please try again or use ORCID." | exception class, HTTP status |
| any §5.5 check fails | same generic message | which check, `tid` |
| not linked / role not eligible | specific messages (§5.3) | `tid`, user id |

When a user's organisation requires admin approval, Entra stops on its own "Need admin approval" page
(AADSTS90094) and may never redirect back; its "Back to app" button sends an error to the redirect URI
(AADSTS65004, whose `error` value Microsoft does not document). Messages key on the OAuth `error` value
only — Microsoft: "Apps that take a dependency on text or error code numbers will be broken over time."
Codes, tokens, assertions, the verifier and the key are never logged. At runtime every Microsoft failure
leaves ORCID sign-in working; a bad configuration stops the whole web app at start (S1).

### 5.7 Pages and copy

- `templates/login.html`: "Sign in with Microsoft (staff)" below the ORCID button when on; new error codes.
- `templates/settings.html`: "Microsoft sign-in" section (§5.3 visibility). Linked: sign-in name,
  linked date, last used, Unlink. Not linked: Link, or "Sign in with ORCID again to link".
- `templates/admin/user_detail.html` and the admin-only account block of
  `templates/workspace/pi_detail.html` (not while impersonating): sign-in name, tenant ID, linked, last
  used, Remove.
- Docs: `docs/operations/pis-and-access.md` (feature and runbook), a "Deploy order for `0066_…`" box in
  `docs/operations/migration-deploy-notes.md`, `.env.example`, one line in `CLAUDE.md`.

### 5.8 Dependencies

`authlib>=1.7.2` (from `>=1.3.0`) and `joserfc>=1.7.4` (now used directly). Both need Python >= 3.10;
the image is 3.11.

## 6. Threats

| Threat | Mitigation |
|---|---|
| Account takeover by email (nOAuth) | `email` never requested or used; lookup by `(tid, oid)` only. |
| Login CSRF / injected code | `state`, `nonce`, PKCE in the session that started the flow; Microsoft's one-use code. |
| IdP mix-up (RFC 9700 §4.4.2 requires a defence with two providers) | Per-provider flow keys record which issuer each request went to; the ID token's `iss` is checked (§4.4.2.1); separate redirect URIs. |
| Stolen session cookie, or someone at the user's browser, adds a permanent way in | Linking requires an ORCID re-entry of credentials (`prompt=login`, `auth_time` ≤ 5 min, same ORCID iD) within 15 minutes; removal ends sessions. |
| Token for another app or tenant | Signature, `aud`, `iss`, key issuer, tenant allowlist before any key fetch. |
| Guest accounts of an allowed tenant | `acct == 0` required (D12); a missing claim fails closed. |
| A link added without the owner noticing | Not mitigated (S3): no notification. Visible on Settings and admin pages. |
| A staff member leaves their organisation | **Not covered by Microsoft.** Disabling their Microsoft account blocks only new Microsoft sign-ins; their existing 30-day session and their ORCID login continue, and their role is not tied to Microsoft. Offboarding stays an admin action (role or access change, which bumps the epoch), as today. |
| Private key exposure | Key only in `.env`; every copy of `.env` (including the untracked `.env.bak-pre0063` and host backups) carries it; restrict permissions and rotate if a copy leaks. |
| Expired certificate | Startup warning at 30 days; renewal runbook; ORCID unaffected. |
| Flow data readable in the cookie | Only the browser holding the session can read it, and it already holds the session. |

## 7. Rollout

### 7.1 Ship dark

Commit; build `blackbird-app` and `worker`; update `scripts/migrate/preflight.py` (`DEFAULT_TARGET`,
`REVISION_ORDER`, `SUPPORTED_START_REVISIONS`, `PLANNED_OBJECTS`) and the deploy-notes box; rehearse
then apply `0066` with `./scripts/migrate/run_migration.sh` from the new image; `up -d blackbird-app
worker`. The agent does not use this; no agent rebuild or restart.

### 7.2 Turn on (runbook)

1. Choose where the registration lives (D4). Consent consequences, by the other organisation's setting:
   Microsoft-managed default (new tenants) — users can consent to `openid profile`; "verified publishers
   and apps registered in your tenant" (`microsoft-user-default-low`) — users cannot consent to an
   unverified app from another tenant; user consent disabled, or the app requires assignment — admin
   consent needed. Publisher verification needs a Partner One partner-global-account ID, a tenant
   associated with it, a DNS-verified non-`onmicrosoft.com` publisher domain, and an app registered by a
   work account. If every allowed organisation is one tenant, a single-tenant registration there is
   simpler (no cross-tenant consent) — revisit D3/D4 in that case.
2. Create the registration: "Multiple Entra ID tenants", platform Web, redirect URI
   `https://blackbird.copi.science/auth/microsoft/callback` only (development uses a separate
   registration — Microsoft: keep development redirect URIs out of production). Leave implicit grant
   off. Confirm app instance property lock is enabled (default for new apps since June 2026). Add the
   optional claim `acct` to the **ID** token (Token configuration → Add optional claim, or
   `optionalClaims.idToken` in the manifest) (D12).
3. `openssl req -x509 -newkey rsa:3072 -sha256 -days 365 -nodes -keyout key.pem -out cert.pem -subj "/CN=blackbird-microsoft-signin"`;
   upload `cert.pem` under Certificates & secrets → Certificates.
4. Put the five settings in `.env` (`base64 -w0 cert.pem`, `base64 -w0 key.pem`); `chmod 600 .env`;
   delete `key.pem` where it was generated.
5. `$DC up -d --force-recreate blackbird-app`.
6. One live link and sign-in per allowed organisation (§9), with that organisation's admin consent if
   step 1 says it is needed.
7. Calendar reminder 30 days before the certificate expires.

### 7.3 Renew, move to CA-signed, roll back

Renew or switch to CA-signed: upload the new certificate (a registration holds several), change the two
`.env` values, recreate, delete the old certificate. Roll back: clear `MICROSOFT_CLIENT_ID`, recreate;
links stay, unused.

## 8. Testing

No test talks to Microsoft or ORCID. A test RSA key signs ID tokens; tenant key sets and the token
endpoint are served by `httpx.MockTransport`. Tests switch the feature with
`asgi_app.state.microsoft_sign_in`, never through `.env`.

- **Unit** (`tests/unit/test_microsoft_identity.py`): each §5.5 rejection (`alg=none`, `HS256`,
  unknown `kid`, key-issuer mismatch, tenant not allowed — with no key fetch made, `aud` string/list
  cases, `exp`/`nbf`/`iat`, nonce, missing or invalid `oid`, `acct` = 1 and `acct` missing); success; key cache
  refresh and unknown-`kid` refetch limit; the assertion's header and claims (new `jti` per call,
  `exp - nbf == 300`, `client_id` in the body); configuration validation (each S1 failure; feature off).
- **Integration** (`tests/integration/test_microsoft_sign_in.py`, Postgres): linking — ineligible
  role, stale or Microsoft-originated sign-in, impersonation, `(tid, oid)` taken, user already linked,
  concurrent link (`IntegrityError` path), success; `/login/start?reauth=1` proceeds for a signed-in
  user, sends `scope=openid`, `prompt=login` and a `nonce`, and returns to `/settings`, while plain
  `/login/start` still redirects to `/` and still sends `/authenticate`; the reauth callback refuses a
  missing `id_token`, each §5.5.1 failure (including stale `auth_time` and a different ORCID iD) and
  leaves the session untouched when it does; sign-in — not linked,
  role `pi`, tenant removed, denied → `/access-pending`, success writes `user_id`, `epoch`,
  `signed_in_*`, `last_login_at` and creates no user or email; unlink deletes the row, ends the
  current and every other session (each refused afterwards by `get_current_user`), and lands on
  `/login` with the flash, including when a concurrent bump got there first; admin removal — audit row, all sessions end, manager 403, impersonation refused, PI target
  via the workspace page; deleting a user with the identity row not loaded removes it; Settings hides
  the section while impersonating and for PIs.
- **New migration test** `tests/integration/test_migration_0066.py`, per the repo's convention.
- **Existing tests to update:** `tests/integration/test_session_epoch.py` (four exact session-dict
  asserts gain `signed_in_*`); `tests/unit/test_admin_route_table.py` (`EXPECTED_ROUTES`, count 45 → 46);
  `tests/unit/test_migration_checks.py:281-283` and `tests/integration/test_harness_smoke.py:99`
  (`0065` → `0066`); `tests/unit/test_reachability.py` (`ROUTE_ALLOWLIST` for the callback);
  `tests/integration/test_rendered_page_gate.py` (`SKIPPED` for the three new GETs);
  `tests/unit/test_config_secret_redaction.py` (`NON_SECRET_STR_FIELDS`);
  `tests/integration/test_settings_page.py`; `tests/integration/test_login_page.py`; the ORCID callback
  tests around the shared tail (`test_auth_allowlist_gate.py`, `test_user_email.py`,
  `test_access_revocation.py`, `tests/characterization/test_auth_and_admin_routes.py`).
  `tests/integration/test_model_migration_parity.py` must still pass with no edit (it does not compare
  `ondelete`). The CSP and origin-guard pins should not change.
- **Gate:** `./scripts/ci.sh` green.

## 9. To verify at the first live sign-in

- Microsoft accepts the assertion with `aud` = the `organizations` token endpoint (MSAL does the same),
  with `PS256`, and with `client_id` in the body.
- What a user sees in each allowed organisation's consent setting, and what reaches the redirect URI.
- That `acct` arrives in the ID token once configured, and its JSON type (Microsoft documents the
  values `0`/`1` but not whether they are numbers or strings; the check accepts `0` and `"0"` only).
- Whether a guest can reach an allowed tenant through `/organizations` (D12 rejects them either way).
- ORCID honours `prompt=login` and returns `auth_time` and the `nonce` for `scope=openid`, with
  `aud` equal to our ORCID client ID (ORCID's page shows `aud` in examples but does not define it).

## 10. Sources

Microsoft Learn (fetched 2026-10-13): `id-token-claims-reference`, `claims-validation`, `access-tokens`,
`id-tokens`, `signing-key-rollover`, `v2-oauth2-auth-code-flow`, `v2-protocols-oidc`,
`certificate-credentials`, `how-to-add-credentials`, `howto-create-self-signed-certificate`,
`reply-url`, `supported-accounts-validation`, `howto-modify-supported-accounts`,
`publisher-verification-overview`, `configure-user-consent`, `manage-app-consent-policies`,
`configure-risk-based-step-up-consent`, `user-admin-consent-overview`, `application-consent-experience`,
`application-sign-in-unexpected-user-consent-error`, `reference-error-codes`,
`security-best-practices-for-app-registration`, `identity-platform-integration-checklist`,
`v2-conditional-access-dev-guide`, `external-id/redemption-experience`, `optional-claims-reference`,
Graph `application` and beta `allowedTenantsAudience`. Live: `organizations` and tenant-specific
discovery documents and key sets. Standards: RFC 9700 §2.1, §4.4; OIDC Core 1.0 §3.1.3.7;
draft-ietf-httpbis-rfc6265bis-22 §5.6.7.1. ORCID: `ORCID_AUTH_WITH_OPENID_CONNECT.md` (ORCID-Source
main), live `https://orcid.org/.well-known/openid-configuration`. Source read: authlib 1.7.2, joserfc
1.7.4, Starlette and SQLAlchemy in `.venv-test`; MSAL Python 1.39.0 wheel. The teammate guide
(`microsoft-oauth-login-setup.md`) was used for the portal steps only.
