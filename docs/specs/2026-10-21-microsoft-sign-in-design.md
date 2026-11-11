# Microsoft sign-in — design

Date: 2026-10-21 · Revision 3 (after two rounds of adversarial audit) · Status: draft for owner review ·
Code baseline: `main` @ `c08e33e9`, alembic head `0030` · Source implementation: `blackbird` @
`6053d30c` (commits `5a11a106`, `3bafb292`; its spec is
`docs/specs/2026-10-13-microsoft-staff-sign-in-design.md` on that branch) · Plan:
`docs/plans/2026-10-21-microsoft-sign-in.md`

## 1. Goal

Any existing CoPI account can link one work or school Microsoft account (Microsoft Entra ID) and use
it as a second way to sign in. ORCID stays the only way to create an account and stays available to
everyone.

Success: a linked user can use either button on `/login` and lands in the same account, through the
same access gate and the same landing logic (pending invite, onboarding, saved destination,
`/profile`) as an ORCID sign-in. Unlinking, or an admin removing the link, ends every session the
account holds, on every device.

## 2. Decisions

### 2.1 Owner decisions (2026-10-21)

| # | Question | Decision |
|---|---|---|
| U1 | Who may link and use Microsoft sign-in? | Every existing account: PIs, delegates, admins. Microsoft never creates an account. The access gate (`users.access_status`) is the only eligibility rule. |
| U2 | Server-side session revocation | Port blackbird's session epoch (`users.session_epoch`). Logout, a user's unlink and an admin's removal end every session of the account. |
| U3 | Which Microsoft accounts? | Members (`acct` = 0) of the tenants listed in `MICROSOFT_ALLOWED_TENANT_IDS`, fail-closed. No personal accounts, no guests. |

**Open for the owner (security review, §6 row "Microsoft account controls a linked CoPI account"):**
U1 lets an admin link a Microsoft account, so whoever controls that Microsoft account — including
that tenant's own IT administrators — can sign in as the CoPI admin and impersonate any user. The
plan implements U1 as decided and adds a turn-on precondition (each allowed tenant enforces MFA for
this app). Excluding `is_admin` accounts from Microsoft sign-in is a one-line change if preferred.
Blackbird has the same exposure (its admins are eligible).

### 2.2 Carried over from the blackbird design (each re-checked against main in §4)

| # | Decision |
|---|---|
| B1 | The user links and unlinks from Settings; an admin can view and remove a link, never create one (blackbird D5). |
| B2 | authlib for the code exchange, joserfc for ID-token checks; not MSAL, not hand-written HTTP (D6). |
| B3 | Certificate client credential (`private_key_jwt`), not a client secret; self-signed now, CA-signed later by configuration only (D7, D8). |
| B4 | Linking needs a proven ORCID re-sign-in: `scope=openid`, `prompt=login`, a nonce, and an ORCID ID token whose `auth_time` is at most 300 s old, no more than 15 minutes before the link starts and before its callback (D10). |
| B5 | A user's own unlink ends every session, the current one included (D11). |
| B6 | Guests are refused: the registration adds the optional ID-token claim `acct`; a missing `acct` fails closed (D12). |
| B7 | A missing or invalid Microsoft configuration stops the web app at start; it never affects the worker, agent or grantbot (S1). |
| B8 | The `x5t#S256` header is computed from the certificate in `.env` (S2). |
| B9 | One linked Microsoft account per user; no notification when a link is added (S3). |
| B10 | While an admin impersonates, the Settings section is hidden and every Microsoft and reauth action refuses (S6). |
| B11 | Where the app registration lives is configuration, decided before turn-on (D4). |

### 2.3 Decisions this design makes for main (flag any you disagree with)

- **M1 Messages.** Main has no flash queue. Results are fixed codes in the redirect URL —
  `/login?error=<code>` (the existing pattern, `src/routers/auth.py:120-121`) and
  `/settings?microsoft=<code>` — mapped to fixed text in code and rendered through `base.html`'s
  existing, currently unused `flash_message`/`flash_type` block (`templates/base.html:127-137`). An
  unknown code renders nothing, so a crafted link cannot put arbitrary text on the page.
- **M2 Session reset at sign-in.** Port blackbird's `_start_fresh_session`: every sign-in (ORCID or
  Microsoft) starts a new session that keeps only a vetted `post_login_redirect` and a
  `pending_invite_token`. The sign-in response also deletes the host-only `copi-impersonate` cookie
  this app sets, so a new sign-in does not inherit an impersonation (blackbird drops it with the
  session; on main it is a separate cookie). This is a behaviour change for ORCID sign-in too.
- **M3 Invite routes honour revocation.** `GET /invite/{token}` and `POST /invite/{token}/accept`
  read `session["user_id"]` raw (`src/routers/invite.py:80-96,159-166`), so a revoked session could
  still act there. They resolve the signed-in account through the same checks as
  `get_current_user` (minus impersonation, which they never honoured) and treat a refused session as
  signed out.
- **M4 One migration.** `0031` adds `users.session_epoch` and creates `user_microsoft_identities`.
  Main's ids already diverge from blackbird's from `0025` on, so blackbird's `0057`/`0066` cannot be
  cherry-picked.
- **M5 No audit table.** Main has none for users (only `cohort_audit_events`). An admin's removal is
  a `logger.info` line, as admin user deletion is today (`src/routers/admin.py:324`).
- **M6 No ORCID-only side effects on Microsoft sign-in.** The ORCID callback's profile refresh,
  allowlist promotion, `claimed_at` and email fill-in stay ORCID-only. A linked account whose
  `access_status` is not `allowed` lands on `/access-pending` exactly as with ORCID; only an ORCID
  sign-in can promote it through the allowlist.
- **M7 Settings page for everyone.** The Microsoft section shows for any signed-in account while the
  feature is on and the session is not impersonating (neither `_is_impersonated` nor M8's rule).
- **M8 One impersonation rule for proofs.** Reauth start, the reauth callback, link start and the link
  callback all refuse when the session's own account is an admin and the request carries a
  `copi-impersonate` cookie (even one that does not resolve to a user), so the ORCID proof can never
  be earned or spent while an admin views as someone else. Unlink and admin removal refuse when
  `get_current_user` returns an impersonated user.
- **M9 Bounded remembered destination.** `is_safe_next_url` accepts at most 1024 characters, each
  printable ASCII other than `"` and `\` (0x21–0x7e; today 2000 characters of anything but CR, LF,
  NUL and `\`), so its JSON encoding in the cookie is at most 1026 bytes. A target starting with
  `//` is refused too: `///route` splits to an empty netloc, but browsers read it as the host
  `route`. `/auth/microsoft/start`
  also drops `pending_access` and `oauth_state` before writing its flow. Together they keep the
  session cookie plus the redirect to Microsoft inside nginx's 4 KB upstream-header buffer (§4). A
  `next` outside the bound is simply not remembered; `_login_location` passes the query
  percent-encoded, so ordinary pages are unaffected (a path containing a space is no longer
  remembered).

## 3. Non-goals

Personal Microsoft accounts; creating accounts from Microsoft; calling Microsoft Graph; Microsoft
single sign-out; an admin UI for the tenant list; requesting, storing or using the Microsoft `email`
claim; an audit table; changes to cross-site request protection; pre-existing issues outside
this feature (§10).

## 4. Constraints from main's code (verified 2026-10-21 at `c08e33e9`)

- ORCID is the only provider. `users.orcid` is `String(50) NOT NULL UNIQUE`
  (`src/models/user.py:23`). Roles: only `users.is_admin` (`:24`); `copi admin:grant` sets it
  (`src/cli.py:113-141`).
- Sessions: Starlette `SessionMiddleware`, cookie `copi-session`, 30 days, `same_site="lax"`,
  `https_only=not allow_http_sessions` (`src/main.py:272-280`). starlette 1.6.0 signs the base64 JSON
  with itsdangerous and does not encrypt it, has no size check, and re-sends the cookie only when the
  session was modified (`starlette/middleware/sessions.py:29,49-54,64-75`). So popping a key is not
  server-enforced single use (an older copy of the cookie still holds it), and the flow data is
  readable by whoever holds the cookie.
- No server-side revocation today: `POST /logout` clears this browser's session and deletes
  `copi-impersonate` (`src/routers/auth.py:300-313`). `get_current_user` re-checks `access_status`
  on every request and clears the session of a non-allowed user (`src/dependencies.py:82-92`).
- Sign-in sets `session["user_id"]` in one place, inside `auth_callback`
  (`src/routers/auth.py:281`), with no session reset: pre-login keys stay unless popped.
- Readers of the raw `session["user_id"]` that bypass `get_current_user` and act on the account:
  `src/routers/invite.py:80` and `:159`. The others only redirect or render (`src/routers/public.py:471`,
  `src/routers/auth.py:113,127`, `AgentBadgeMiddleware`).
- Impersonation is the `copi-impersonate` cookie (httponly, 24 h, `SameSite=lax`) set by
  `POST /admin/impersonate` (`src/routers/admin.py:1191-1247`) and honoured only for an admin
  session, and only when it parses and resolves to a user (`src/dependencies.py:94-109`,
  `src/main.py:157-166`); the impersonated `User` carries `_is_impersonated` and `_real_admin`.
- Messages: `/login?error=` codes are mapped in `templates/login.html:23-31`; `base.html` has a
  `flash_message`/`flash_type` block nothing sets. Main has no Jinja filters
  (`src/templating.py:39-42` registers only the `css_version` global) and no `data-confirm`
  handler; dates render with `strftime` and `data-utc` spans (`templates/settings.html:99`,
  `templates/base.html:155-162`), confirmations with `onsubmit="return confirm(...)"`.
- Cross-site POSTs are blocked by the `SameSite=lax` session cookie
  (`src/routers/auth.py:304-308`). nginx enforces only `frame-ancestors 'none'; base-uri 'self'; object-src 'none'`;
  `form-action 'self'` is Report-Only (`nginx/nginx.conf:139-161`, pinned by
  `tests/unit/test_nginx_config.py`). Every Microsoft round trip therefore starts from a plain link
  (GET), and the callback must be a GET (`response_mode=query`): a `form_post` callback is a
  cross-site POST, which a Lax cookie does not accompany, so the flow would be lost.
- nginx proxies the app with `proxy_buffer_size 4k` (`nginx/nginx.conf`, `location /`). nginx: the
  buffer holds "the first part of the response … This part usually contains a small response header;
  if it exceeds the buffer size, the response is considered invalid" (nginx `ngx_http_proxy_module`).
  Measured with this design's flow and today's 2000-character `next`: `Location` 454 bytes plus
  `Set-Cookie` 3549 bytes before the status line and other headers — at the limit. The session is
  `b64encode(json.dumps(...))`, so `"` costs 2 bytes and each control character 6 (`\u00XX`) (M9).
- `src.main` runs `app = create_app()` at import (`src/main.py:409`). It is imported by uvicorn
  (`docker-compose.prod.yml:62`, `Dockerfile:91`), by tests (`tests/conftest.py:137` and several test
  modules that build their own app) and by `scripts/ci.sh`'s opt-in lock smoke step. `migrate`,
  `app`, `worker`, `agent` and `grantbot` all load `.env` (`docker-compose.prod.yml:36,65,101,144,
  175`), so a check in `Settings` would stop all of them; a check in `create_app()` stops only the
  web process — and any test session whose host `.env` holds an invalid value.
- `get_settings()` is `lru_cache`d and reads `.env` from the working directory; environment
  variables override `.env`.
- Dependencies: the image installs `requirements.lock` with `--require-hashes`, then `pip install
  --no-deps .` (`Dockerfile:15,24`). The lock pins authlib 1.8.0, joserfc 1.7.5 (via authlib),
  cryptography 50.0.1 (via authlib and joserfc), httpx 0.28.1, anthropic 0.125.0, starlette 1.6.0,
  and no httpx2. `pyproject.toml` caps `anthropic<1.0.0`; anthropic ≥ 1.0.0 requires httpx2 (PyPI
  metadata of 1.17.0). `scripts/check_lockfile.py` (ci.sh step 5) requires every direct dependency to
  be pinned in the lock.
- authlib 1.8.0 builds `AsyncOAuth2Client` on `httpx2` when it is importable and on `httpx`
  otherwise, chosen at import (`authlib/integrations/httpx_client/_compat.py:9-16`); under the lock
  it is httpx, with an `AuthlibDeprecationWarning` at import (not an error: pytest has no
  `filterwarnings`). `tests/unit/test_reachability.py` imports every `try:`-guarded import in `src/`
  unless its module is in `GUARDED_IMPORT_ALLOWLIST` (empty today), so an optional `httpx2` import
  needs an entry.
- authlib 1.8.0 `PrivateKeyJWT`: signs with joserfc using the client's `client_secret` as the private
  key; defaults `exp = iat + 3600`, no `nbf`, `aud` = the URL requested; `alg` comes only from the
  constructor; a `claims` dict passed in is updated in place (and gains a `jti` only if it has none);
  only `client_assertion_type` and `client_assertion` are added to the body, not `client_id`; an
  instance is accepted directly as `token_endpoint_auth_method`; unrecognised constructor kwargs are
  silently kept as metadata. `fetch_token` raises `OAuthError` for a JSON object with `error`,
  `HTTPStatusError` for ≥ 500, `ValueError` for a non-JSON body or a malformed `expires_in`,
  `TypeError` for a non-object JSON body that it inspects (`5`, `null`, `"…"`, `["error"]`),
  `OverflowError` for `expires_in` like `1e400`; it returns a 4xx JSON object without `error`, and a
  JSON array without `"error"`, as if it were a token (probed 2026-10-21).
- joserfc 1.7.5 `jwt.decode(token, key, algorithms=[...])` verifies the signature, the allowed
  algorithm, header value types and the key's own `use`/`alg`; it does not check `exp`/`nbf`/`iat`/
  `aud`, does not require the payload to be an object, rejects unknown JWS header members, and caps the
  base64url header segment at 512 bytes and the signature at 1024 bytes.
- cryptography 50.0.1: `load_pem_private_key` raises `ValueError`, `TypeError`, or
  `cryptography.exceptions.UnsupportedAlgorithm` (not a `ValueError`) for an unknown key type; a
  certificate with an unknown key type loads, and `public_key()` then raises `UnsupportedAlgorithm`
  (both probed 2026-10-21).
- Alembic: head `0030`, pinned in `scripts/migrate/preflight.py`, `scripts/migrate/postflight.py`,
  `tests/integration/test_harness_smoke.py:18`, `tests/integration/test_migration_0030.py`,
  `tests/unit/test_migration_checks.py`, and `docs/production-migration.md:9` (test-pinned).
  `scripts/redeploy.sh` runs `alembic upgrade head` in the `migrate` service after stopping `app`,
  `worker` and `grantbot` (not `agent`), before starting the new ones.
- Account deletion is ORM `db.delete(user)` (`src/routers/profile.py:300-301`,
  `src/routers/admin.py:313-314`); a child table with no ORM relationship is removed by its
  `ON DELETE CASCADE`. `tests/integration/test_db_contract.py:369-389` pins the set of delete-orphan
  relationships, so the new table declares none.
- Gate (`./scripts/ci.sh`), baseline measured 2026-10-21 on `c08e33e9`: ruff `src/` 247 findings
  (ceiling 260; 149 are B008 `Depends(...)` defaults), test lint clean (`LINT_TARGETS` names the
  top-level test helpers file by file), mypy 139 (ceiling 150), coverage 81.48 % (floor 60), alembic
  round trip clean, lockfile consistent; pytest 3230 passed, 276 skipped, 1 failed (§10).
- Route inventories new routes must satisfy: `tests/unit/test_reachability.py` (every route linked
  from a reachable template, named by a non-docstring string in `src/`, or in `ROUTE_ALLOWLIST`;
  stale allowlist entries fail), `tests/integration/test_onboarding_flow.py:292-366` (every
  `/settings` route is in `ENDPOINTS`, count pinned at 15, and both authorization sweeps run on it).

## 5. Design

### 5.1 Data model — migration `0031`, additive

`users.session_epoch INTEGER NULL`, no default, no backfill; NULL reads as 0.

New table `user_microsoft_identities` (model `src/models/microsoft_identity.py`, no relationship on
`User`):

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `user_id` | UUID NOT NULL, FK `users.id` `ON DELETE CASCADE` (named `user_microsoft_identities_user_id_fkey`), UNIQUE `uq_user_microsoft_identities_user_id` | one link per user (B9) |
| `tenant_id` | UUID NOT NULL | `tid` |
| `object_id` | UUID NOT NULL | `oid` |
| `sign_in_name` | String(255) NULL | `preferred_username`; display only |
| `linked_at` | timestamptz NOT NULL, server default `now()` | |
| `last_sign_in_at` | timestamptz NULL | |

`UNIQUE (tenant_id, object_id)` as `uq_user_microsoft_identities_tenant_object`. Lookups use
`(tenant_id, object_id)` only. Microsoft's access-token guidance: "use the immutable claim values
`tid` and `oid` as a combined key for application data and determining whether a user should be
granted access", never `email`, `preferred_username` or `upn` (Learn: `claims-validation`); its
ID-token reference: "Your application mustn't use human-readable data to identify a user", and
`preferred_username` "can't be used to make authorization decisions" (Learn:
`id-token-claims-reference`). The mapper sets `eager_defaults=True` so the INSERT returns `linked_at`
(an async session cannot lazy-load an expired server default).

Old code on the new schema is safe (nothing old reads either object). New code on the old schema is
not (every `User` load selects `session_epoch`), which `scripts/redeploy.sh`'s migrate-first order
handles. Downgrade drops the table (every link) and the column; a later re-upgrade brings the column
back as NULL (= 0), which makes every revoked session valid again (§7.3).

### 5.2 Configuration

Five `str` settings, default `""`, in `src/config.py`:

| Setting | Masked in `repr` | Meaning |
|---|---|---|
| `MICROSOFT_CLIENT_ID` | no | Application (client) ID. Empty = feature off. |
| `MICROSOFT_REDIRECT_URI` | no | `<BASE_URL>/auth/microsoft/callback`; must equal the registered URI exactly ("Redirect URIs are case-sensitive"; Learn: `reply-url`). |
| `MICROSOFT_ALLOWED_TENANT_IDS` | no | Comma-separated tenant GUIDs. A `str`, not a list: pydantic-settings JSON-decodes list-typed environment values. |
| `MICROSOFT_CERTIFICATE_B64` | no | Base64 of the PEM certificate uploaded to the registration. |
| `MICROSOFT_PRIVATE_KEY_B64` | yes (name contains `key`) | Base64 of the unencrypted PEM RSA private key. |

`create_app()` stores `load_config(settings)` on `application.state.microsoft_sign_in`
(`MicrosoftSignInConfig`, frozen, private key excluded from `repr`; `None` = off). With
`MICROSOFT_CLIENT_ID` set it raises `MicrosoftConfigError` unless: every other setting is present;
the tenant list holds at least one canonical GUID (normalised to lower case); the redirect URI is
`https` (or `http://localhost` with `ALLOW_HTTP_SESSIONS=true`); the certificate parses and its public
key loads; the private key parses; the key is RSA of at least 2048 bits and matches the certificate.
Every cryptography error in those steps (`ValueError`, `TypeError`, `UnsupportedAlgorithm`) becomes a
`MicrosoftConfigError`. "Microsoft Entra ID currently supports only RSA", and for the certificate
Microsoft's how-to creates, "the 2048-bit size is highly recommended" while "longer values are
supported" (Learn: `howto-create-self-signed-certificate`); no minimum is stated, so 2048 is this
design's floor. A certificate that is expired or not yet valid logs an ERROR (sign-ins will fail as
`microsoft_failed`; ORCID is unaffected); one within 30 days of expiry logs a WARNING. Neither stops the
app: refusing to start on an expiring credential would turn a partial outage into a full one. The
routes are always registered; every handler 404s while the state is `None`. A change needs the `app`
container recreated (`env_file` is read at container creation).

### 5.3 Session

Keys after this change:

| Key | Written by | Meaning |
|---|---|---|
| `user_id` | `_post_login_redirect` | signed-in account |
| `epoch` | `_post_login_redirect` | `users.session_epoch` at sign-in (NULL → 0) |
| `signed_in_at`, `signed_in_via` | `_post_login_redirect` | Unix seconds; `"orcid"` or `"microsoft"` |
| `orcid_reauth_at` | ORCID callback after a proven reauth only | Unix seconds |
| `oauth_state` | `/login/start` | ORCID state (unchanged) |
| `orcid_reauth` | `/login/start?reauth=1` | `{nonce, started_at}` |
| `microsoft_flow` | `/auth/microsoft/start` and `/link` | `{state, nonce, code_verifier, purpose, user_id, started_at}` |
| `post_login_redirect`, `pending_invite_token`, `pending_access` | unchanged | |

**Epoch.** `get_current_user` refuses a session whose `epoch` (absent = 0; a non-int or bool =
invalid) differs from `users.session_epoch` (NULL = 0): it clears the session and redirects to
`_login_location(request)`. The check runs after the access check and before impersonation, because
the epoch belongs to the account holding the session. `bump_session_epoch(db, user_id, *,
expected_epoch=None)` increments with `coalesce(session_epoch, 0) + 1`; with `expected_epoch` it
only bumps while the stored value still equals it, so replaying an already-revoked cookie cannot end
the account's newer sessions. Bumped by `POST /logout` (with the session's epoch), and
unconditionally by the user's unlink and the admin's removal: those already passed the epoch check,
and a session issued in between (a sign-in through the link being removed, after a logout elsewhere
moved the epoch) must end too. Sessions issued before the
deploy carry no `epoch` and the column is NULL, so nobody is signed out by the deploy.

**Shared sign-in tail.** `complete_sign_in(request, db, user, *, via) -> RedirectResponse` in
`src/routers/auth.py` ends both callbacks: set `last_login_at` if allowed; commit;
`_start_fresh_session` (keep a vetted `post_login_redirect` and a `pending_invite_token`, drop the
rest); if not allowed, write `pending_access` (`{user_id, orcid, email, name}`, the shape
`/access-pending` reads) and redirect to `/access-pending`; otherwise `_post_login_redirect(request,
user, via=via)`, which writes `user_id`, `epoch`, `signed_in_at`, `signed_in_via`, pops
`pending_access`, and returns today's landing (`/invite/{token}`, `/onboarding`, the saved
destination, else `/profile`). Every response it returns deletes `copi-impersonate` (M2). The
`epoch` written is the one read when the user was looked up, before the commit, so a revocation
that commits during the sign-in leaves the new session stale; only a user created in this request
(no epoch loaded, nothing can have bumped it) has it read after the commit.

### 5.4 Routes and flows

| Route | Purpose | Router |
|---|---|---|
| `GET /auth/microsoft/start` | begin sign-in (button on `/login`) | `src/routers/microsoft_auth.py` |
| `GET /auth/microsoft/link` | begin linking (link on `/settings`) | same |
| `GET /auth/microsoft/callback` | the single redirect URI for both | same |
| `GET /login/start?reauth=1` | ORCID re-sign-in before linking | `src/routers/auth.py` (existing route) |
| `POST /settings/microsoft/unlink` | user removes their link | `src/routers/settings.py` |
| `POST /admin/users/{user_id}/microsoft/unlink` | admin removes a user's link | `src/routers/admin.py` |

**Sign-in.** `/auth/microsoft/start` sends a session holding `user_id` to `/`; otherwise it drops
`pending_access` and `oauth_state` (M9), stores `microsoft_flow` (purpose `signin`) and redirects to
Microsoft (§5.5). The callback pops the flow and
refuses a missing flow, or a `state` that is missing or differs from the flow's (`state_mismatch`) —
before it looks at `error`, so an `error` is only acted on (and logged) for the flow that asked.
Then: `error` → `microsoft_cancelled` for `access_denied`, else `microsoft_error`; a missing `code`
or a flow older than 600 s → `state_mismatch`; exchange the code and validate the ID token (§5.5,
§5.6); find the link by `(tid, oid)` (none → `/login?error=microsoft_not_linked`); stamp
`last_sign_in_at` only for an allowed account — an UPDATE that matches no row means the link was
removed after the lookup, and the sign-in is refused as `microsoft_not_linked`; and call
`complete_sign_in(..., via="microsoft")`. It
never creates a user and never writes `users.email` or `users.name`.

**Re-sign-in with ORCID (B4).** `/login/start?reauth=1` resolves the session through
`get_current_user` (a stale or revoked session is bounced as on any page). If the session account is
impersonating (M8) it redirects to `/settings?microsoft=impersonating`. Otherwise it asks ORCID for
`scope=openid`, `prompt=login` and a fresh `nonce`, stores `orcid_reauth = {nonce, started_at}` and
`post_login_redirect = "/settings"`. Plain `/login/start` is unchanged for a signed-out visitor
(`scope=/authenticate`), still sends a signed-in one to `/`, and drops any `orcid_reauth`. The ORCID
callback pops `orcid_reauth`; when present, after the existing state check and token exchange it
requires: the session's account exists, is not impersonating (M8), has `users.orcid` equal to the
token's `orcid`, the flow is at most 600 s old, and the token response's `id_token` passes §5.7. Any
failure leaves the session as it was and redirects to `/settings?microsoft=reauth_failed`. On success
the callback continues as a normal ORCID sign-in and, after `complete_sign_in` started the fresh
session, writes `orcid_reauth_at`. The ORCID `error` query value is logged with `%r`, cut to 80
characters (today `%s`, `src/routers/auth.py:145`). The ORCID callback keeps today's order (it acts on
`error` before comparing `state`, which `test_auth_callback_error_param_redirects` pins), so a
cross-site `/auth/callback?error=x` ends a reauth in progress with `reauth_failed`; a nuisance only,
accepted.

**Linking.** `/auth/microsoft/link` uses `get_current_user`; refuses impersonation (M8, 403); and
unless `fresh_orcid_sign_in(session)` (`signed_in_via == "orcid"` and `orcid_reauth_at` an int, not a
bool, 0–900 s old) redirects to `/settings?microsoft=link_refused`. Otherwise it stores
`microsoft_flow` (purpose `link`, `user_id`) and redirects to Microsoft with `prompt=select_account`.
The callback, after §5.5–§5.6, re-resolves `get_current_user`, refuses impersonation (M8,
`/settings?microsoft=impersonating`), requires the account to equal the flow's `user_id` and the
session still fresh (else `link_refused`), then inserts the row inside
`begin_nested()`. An `IntegrityError` maps to `user_already_linked` when the user already has a row,
`account_taken` when the `(tid, oid)` pair is linked to someone, and `link_refused` otherwise (the
user was deleted meanwhile). Success → `/settings?microsoft=linked`. Every refusal is logged with
the user id and `tid`.

A cross-site page can make a signed-in user open `/auth/microsoft/link` or `/login/start?reauth=1`
(GETs). The only outcome is an ORCID password prompt, or linking the Microsoft account that person
picks and signs in to. An attacker's code cannot enter the victim's flow: `state`, `nonce` and the
PKCE verifier live in the session that started it.

**User unlink** (same-origin POST form with a static `confirm()`; impersonation refused with 403;
feature off → 404): delete the row; if there was one, `bump_session_epoch(db, user.id)`
(unconditionally, §5.3), commit, log it, clear the session and redirect to
`/login?error=microsoft_unlinked`; if there was none, redirect to `/settings?microsoft=nothing_linked`.

**Admin removal** (`get_admin_user`; impersonation refused; feature off → 404; unknown user → 404):
in one transaction delete the row and bump the user's epoch unconditionally, commit once, log
`Admin <id> removed the Microsoft sign-in of user <id> (tid=<tid>)`, redirect to
`/admin/users/{user_id}`. Nothing happens and nothing is logged when there is no row. An admin
removing their own link signs themselves out everywhere. A link callback already past its
`get_current_user` check when the removal commits can still insert a new link (no row lock); that
needs the account's ORCID password within the last 15 minutes, so it grants nothing beyond it.
A Microsoft sign-in that overlaps a removal cannot outlive it: its last-use stamp finds no row, or
its session carries the pre-removal epoch (§5.3).

**User unlink log:** `User <id> removed their Microsoft sign-in (tid=<tid>)`.

**Settings section** (M7): shown when the feature is on and the user is not impersonated. Linked:
sign-in name, linked date, last used, an Unlink form. Not linked and fresh: "Link Microsoft account"
(`/auth/microsoft/link`). Not linked and not fresh: "Sign in with ORCID again to link"
(`/login/start?reauth=1`). Dates use `strftime` inside a `data-utc` span, as the page already does;
confirmations use `onsubmit="return confirm('<static text>')"` with no user data in the script.

**Admin page:** `/admin/users/{user_id}` shows the link (sign-in name, tenant ID, linked, last used)
with a Remove form (static `confirm()`) when the feature is on.

### 5.5 Protocol

Fixed endpoints (live discovery documents fetched 2026-10-21): authorize
`https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize`, token
`https://login.microsoftonline.com/organizations/oauth2/v2.0/token`, keys per allowed tenant
`https://login.microsoftonline.com/{tid}/discovery/v2.0/keys`. The `organizations` authority admits
"only users with work or school accounts from Microsoft Entra ID" (Learn: `v2-protocols-oidc`).

Authorization request: `client_id`, `response_type=code`, `response_mode=query`, `scope=openid
profile`, `redirect_uri`, `state` and `nonce` (`secrets.token_urlsafe(32)` each), `code_challenge`
(S256 of `secrets.token_urlsafe(64)`, 86 characters, within PKCE's 43–128) and
`code_challenge_method=S256`; `prompt=select_account` for linking only. PKCE is "recommended for all
application types, both public and confidential clients" (Learn: `v2-oauth2-auth-code-flow`).

Token request: `grant_type=authorization_code`, `code`, `redirect_uri`, `code_verifier`,
`client_id` (Microsoft lists it as "required"; authlib does not add it), and a client assertion. Each
request builds a new `PrivateKeyJWT(alg="PS256", headers={"typ": "JWT", "x5t#S256": <base64url
SHA-256 of the certificate DER>}, claims={"jti": uuid4().hex, "iat": now, "nbf": now, "exp": now +
300})` — a new instance and dict per request, so no `jti`, `iat` or `exp` is reused — and a new
`AsyncOAuth2Client(client_id=..., client_secret=<private key PEM>,
token_endpoint_auth_method=<that instance>, redirect_uri=..., transport=TOKEN_TRANSPORT,
timeout=10.0)`, closed after use. `aud` is the token URL called (the `organizations` endpoint).
Microsoft's certificate page says `alg` "Should be PS256", `x5t#S256` is the "Base64url-encoded SHA-256
thumbprint of the X.509 certificate's DER encoding", lifetime "keep it short - 5-10 minutes after `nbf`
at most", and lists `aud` as `https://login.microsoftonline.com/{tenantId}/oauth2/v2.0/token`
(Learn: `certificate-credentials`); the auth-code page's own example still uses RS256 with `x5t`, so
PS256 is the recommendation, not a stated requirement. A fresh `jti` per request is this design's
choice: MSAL Python 1.39.0 reuses one assertion (600 s lifetime, no `nbf`) for up to 540 s, and no
page read says Entra requires a single-use `jti`. MSAL also sends PS256 with `x5t#S256` (when it
computes a SHA-256 thumbprint and the authority is not ADFS) and uses its authority's token endpoint
as `aud`. Whether Entra accepts the `organizations` token endpoint as `aud` is a live check (§9). The
client's `revoke_token` is never called (authlib would default it to `client_secret_basic`, putting
the key in a header).

authlib picks httpx or httpx2 at import (§4); `TOKEN_HTTP` names whichever it uses, its `HTTPError` is
caught alongside httpx's, and tests inject a `TOKEN_HTTP.MockTransport` through `TOKEN_TRANSPORT`.
Under the current lock that is httpx; the shim keeps a future resolve that brings in httpx2 from
turning a transport error into a 500. The exchange also catches `ValueError`, `TypeError`,
`AttributeError` and `OverflowError` from parsing, and refuses a non-dict response.

The token response's access token is never parsed. A token response that is not a dict, or that
carries no `id_token`, fails as `microsoft_failed`. `interaction_required` from the token endpoint
maps to `microsoft_interaction_required`.

### 5.6 Microsoft ID-token validation

One function `validate_id_token(config, id_token, *, nonce, now) -> MicrosoftIdentity`; every step
must pass, in this order:

1. Compact JWS with three segments whose header and payload are JSON objects; header `alg ==
   "RS256"` (pinned: no Microsoft key carries `alg`; `id_token_signing_alg_values_supported` is
   `["RS256"]`).
2. `tid` is a canonical GUID and in `MICROSOFT_ALLOWED_TENANT_IDS` — before any key fetch, so an
   unlisted tenant causes no outbound request.
3. The signature verifies with the key whose `kid` matches (a token without `kid` only against a
   one-key set; harmless, the signature is still checked), from that tenant's key set, via
   `joserfc.jwt.decode(token, RSAKey.import_key(jwk), algorithms=["RS256"])`. Keys are cached per
   key-set URL, refreshed hourly, served for up to 24 h when a refresh fails, refetched on an unknown
   `kid` at most once per 5 minutes, and not retried for 60 s after a failed fetch. Microsoft: "The
   time-to-live of keys in the cache should be configured to 24 hours, with refreshes happening every
   hour", unknown-`kid` refreshes "no more frequently than 5 mins" (Learn: `signing-key-rollover`);
   the same page also suggests "12 h with a jitter of plus or minus 1 h" and per-`kid` caching —
   the page is not consistent, and this per-set hourly refresh is the stricter reading.
4. `iss == "https://login.microsoftonline.com/" + tid + "/v2.0"`, and the signing key's `issuer`,
   after substituting a `{tenantid}` template case-insensitively, equals `iss`; a key with no
   `issuer` fails (Learn: `access-tokens`: templated-issuer keys "should only be used when the `iss`
   claim in the token matches this value after substituting the `tid` claim"). Tenant key sets carry
   the concrete issuer.
5. `aud` equals `MICROSOFT_CLIENT_ID`, or is a one-element list holding it ("the `aud` claim is
   always the client ID"; Learn: `optional-claims-reference`).
6. `exp` and `iat` are integers (not bool), `nbf` optional; each within 300 s of leeway.
7. `nonce` equals the flow's.
8. `oid` is a canonical GUID.
9. `acct` is `0` or `"0"` and not a bool. Microsoft documents 0 = member, 1 = guest but not the JSON
   type; missing fails closed (B6).

`preferred_username`, if a non-empty string, is kept (cut to 255) as `sign_in_name` for display.
Every check failure raises `IdTokenError(check, detail)`; `detail` is for logs only.

### 5.7 ORCID reauth ID-token validation (B4)

From ORCID's live discovery document (2026-10-21): issuer `https://orcid.org`, `jwks_uri`
`https://orcid.org/oauth/jwks` (one RSA key with a `kid`, no `alg`),
`id_token_signing_alg_values_supported: ["RS256"]`, `claims_supported` includes `auth_time` and
`sub`. ORCID's OpenID Connect page: `prompt=login` "If the user is already logged in, force them to
reauthenticate"; the nonce "will be returned in the id_token"; `sub` is the bare ORCID iD; its example
token response for `openid` carries `name` and `orcid` beside `id_token`. Checks, all required: header
`alg == "RS256"`; signature with the matching `kid` (a token without `kid` only against a one-key set)
from ORCID's key set (same cache rules, separate cache); `iss == "https://orcid.org"`; `aud` is
`ORCID_CLIENT_ID` (or a one-element list holding it); `exp`/`iat` with 300 s leeway; nonce matches;
`sub == token["orcid"] ==` the session account's `users.orcid`; `auth_time` an int with
`now - 300 <= auth_time <= now + 300`. What `auth_time` means at ORCID is verified live before turn-on
(§9).

### 5.8 Messages and logging

`MICROSOFT_MESSAGES: dict[str, tuple[str, str]]` maps each code to `(text, kind)`, `kind` in
`error`/`success`/`info`. `/login` passes `error_message` for a known code of kind `error` into its
existing error box (unknown codes keep the generic "Sign in failed" text, as today) and `notice` for a
known code of another kind (an info box). `/settings` passes `flash_message`/`flash_type` for a known
`?microsoft=` code and nothing otherwise.

| Code | Shown on | Kind | Text |
|---|---|---|---|
| `microsoft_cancelled` | login/settings | error | Microsoft sign-in was cancelled or not approved. |
| `microsoft_error` | login/settings | error | Microsoft sign-in failed. If your organisation must approve CoPI first, ask its IT team. |
| `microsoft_failed` | login/settings | error | Microsoft sign-in failed. Please try again or use ORCID. |
| `microsoft_interaction_required` | login/settings | error | Microsoft needs you to sign in again. Please try again. |
| `microsoft_not_linked` | login | error | No CoPI account is linked to this Microsoft account. Sign in with ORCID, then link it in Settings. |
| `state_mismatch` | login/settings | error | Security check failed. Please try again. (login text already exists) |
| `microsoft_unlinked` | login | info | Microsoft sign-in removed. Please sign in again. |
| `linked` | settings | success | Microsoft sign-in linked. You can now use it on the sign-in page. |
| `nothing_linked` | settings | info | No Microsoft account is linked. |
| `account_taken` | settings | error | That Microsoft account is already linked to a CoPI account. |
| `user_already_linked` | settings | error | Unlink your current Microsoft account first. |
| `link_refused` | settings | error | Sign in with ORCID again, then link your Microsoft account. |
| `reauth_failed` | settings | error | Could not confirm your ORCID sign-in. Please try again. |
| `impersonating` | settings | error | Microsoft sign-in changes are disabled while impersonating. |

Sign-in failures go to `/login?error=<code>`; link-flow and reauth failures to
`/settings?microsoft=<code>` (a signed-in user sent to `/login` is redirected to `/` and would lose
the message). Microsoft-side errors are keyed on the OAuth `error` value only: "Apps that take a
dependency on text or error code numbers will be broken over time" (Learn: `reference-error-codes`).
When an organisation requires admin approval, Entra reports AADSTS90094 "AdminConsentRequired" (or
AADSTS90095 with the admin-consent workflow); whether the user is ever redirected back is a live
check (§9). The app logs: the `error` value cut to 80 characters and up to 300 characters of
`error_description`, both with `%r`; the failing check name and `tid`; the exception class and HTTP
status of a token or key failure; user id and `tid` on link, unlink, removal and refusal. The app never
logs codes, tokens, assertions, the verifier or the key. uvicorn's and nginx's access logs do record
the callback query string (the authorization code), as they already do for `/auth/callback`; a
Microsoft code is useless without the PKCE verifier and the client assertion.

### 5.9 Dependencies

`pyproject.toml`: `authlib>=1.8.0,<2.0.0` (from `>=1.7.1`; 1.8.0 is what the lock installs and what
§4's facts were read from), new direct `joserfc>=1.7.5` and `cryptography>=45.0.1` (both already in
the lock; now imported directly). `requirements.lock` is regenerated with the command in
`scripts/ci.sh`; only `# via` annotations may change.

## 6. Threats

| Threat | Mitigation |
|---|---|
| Account takeover by email | `email` never requested or used; lookups by `(tid, oid)` only. |
| Login CSRF / injected code | `state`, `nonce`, PKCE verifier in the session that started the flow; Microsoft codes "Typically … expire after about 1 minute" and a redeemed code is refused (AADSTS54005). |
| IdP mix-up (two providers) | Separate flow keys and callbacks per provider; `iss` checked against the provider's issuer. |
| Stolen session cookie adds a permanent way in | Linking needs an ORCID password re-entry (`prompt=login`, `auth_time` ≤ 300 s, same ORCID iD) within 15 minutes — provided ORCID's `auth_time` means what §9 checks. |
| **Microsoft account controls a linked CoPI account** | **Partly mitigated.** Whoever controls the linked Microsoft account — by phishing, a weak password, or as an administrator of that tenant (password reset, Temporary Access Pass) — signs in as the CoPI account, an admin's included, and nothing notifies the user. The tenant allowlist limits this to organisations the operator trusts; turn-on requires each allowed tenant to enforce MFA for this app (§7.2); excluding admins is open (§2.1). |
| Revoked session still acting | Epoch check in `get_current_user`; invite routes resolve through it (M3). The remaining raw readers (§4) only redirect or render. |
| Token for another app or tenant, or forged | Signature with the tenant's keys, key issuer, `iss`, `aud`, allowlist before any key fetch, RS256 only. |
| Guest of an allowed tenant | Through `organizations` a user signs in to their home tenant ("for guest scenarios … you must provide the tenant identifier"; Learn: `v2-protocols-oidc`), so a guest arrives with their home `tid` and the allowlist refuses it; `acct == 0` is the second layer, and a missing claim fails closed. |
| A link added without the owner noticing | Not mitigated (B9): visible on Settings and to admins. |
| Someone leaves their organisation | Not covered: disabling the Microsoft account blocks only new Microsoft sign-ins; existing sessions and ORCID continue. Offboarding stays an admin action (deny access, which every request re-checks). |
| Private key exposure | The key is only in `.env`, which all five services load (as they load `SECRET_KEY`, which can already forge any session); every copy of `.env` and its backups carries it. Generate it outside the repository (§7.2); rotate if a copy leaks. |
| Expired certificate | ERROR log at start; WARNING at 30 days; ORCID unaffected. |
| Flow data readable in the cookie | Only the holder of the cookie can read it, and it already holds the session. |
| Oversized cookie breaks the redirect (nginx 502) — a crafted `/login?next=` link | M9 bounds the remembered destination's encoded size and `/auth/microsoft/start` drops `pending_access`; a test pins the worst case under 3584 bytes of headers. ORCID's `/login/start` keeps today's footprint minus the longer `next`. |
| An admin's fresh sign-in counting for someone they impersonate | M8 at all four proof points; a new sign-in drops the host-only impersonation cookie (M2). |
| Unauthenticated callers make the app call Microsoft's token endpoint | Only nginx's general per-IP rate limit applies; Microsoft could throttle the client ID. A dedicated limit for the callback is a follow-up. |

## 7. Rollout

### 7.1 Ship dark

Commit; follow `docs/production-migration.md` (rehearse `./scripts/migrate/run_migration.sh` without
`--apply`), then `./scripts/redeploy.sh` with both prod compose files, which builds and migrates
before starting the new `app`, `worker` and `grantbot`. `0031`'s `ALTER TABLE users` and the new
foreign key lock `users` briefly while the `agent` (not stopped by `redeploy.sh`) may be reading it;
on `LockNotAvailableError` follow the runbook's existing retry. The feature stays off until
`MICROSOFT_CLIENT_ID` is set. The session-epoch check and the fresh session at sign-in are live from
this deploy. The agent image bakes `src/models/user.py`; it keeps working on the additive schema, so
rebuild it at its next planned restart (no restart needed for this change).

### 7.2 Turn on (runbook)

1. Decide where the registration lives (B11) and list the tenants (U3). For each allowed tenant,
   confirm with its IT team that it enforces MFA for this app (Conditional Access), and how its consent
   policy treats `openid profile` for an app from another tenant (users may consent, or an admin must:
   AADSTS90094).
2. Create the registration: supported account types "Accounts in any organizational directory
   (multitenant)", platform Web, redirect URI `<BASE_URL>/auth/microsoft/callback` only (https;
   case-sensitive), implicit grant off. "Since June 2026, the Enable property lock setting is Enabled by
   default for new applications" (Learn: `howto-configure-app-instance-property-locks`); confirm it.
   Add the optional claim `acct` to the ID token (Token configuration → Add optional claim → ID).
3. Outside the repository: `d=$(mktemp -d) && openssl req -x509 -newkey rsa:2048 -sha256 -days 365
   -nodes -keyout "$d/key.pem" -out "$d/cert.pem" -subj "/CN=copi-microsoft-signin"`; upload
   `$d/cert.pem` under Certificates & secrets → Certificates.
4. Put the five settings in `.env` (`base64 -w0 "$d/cert.pem"`, `base64 -w0 "$d/key.pem"`);
   `chmod 600 .env`; `rm -r "$d"`.
5. Recreate the web container with both prod compose files (`docker compose -f
   docker-compose.prod.yml -f docker-compose.override.yml up -d --force-recreate app`), then reload
   nginx (`... exec -T nginx nginx -s reload`), as `scripts/redeploy.sh` does after recreating `app`.
6. Verify §9 with one link and sign-in per allowed organisation before announcing the feature.
7. Calendar reminder 30 days before the certificate expires.

### 7.3 Renew, move to CA-signed, roll back

Renew: upload the new certificate (a registration holds several), change the two `.env` values,
recreate `app`, delete the old certificate. Turn the feature off: clear `MICROSOFT_CLIENT_ID`,
recreate `app`; links stay, unused; sessions already signed in through Microsoft stay valid until
they expire — if the reason is a flaw in Microsoft sign-in, also rotate `SECRET_KEY`. Roll back the
code (or downgrade `0031`): the previous image ignores `epoch`, so every session revoked by logout,
unlink or removal in the last 30 days works again; rotate `SECRET_KEY` with it if those revocations
must hold. Rotating `SECRET_KEY` signs everyone out and invalidates outstanding unsubscribe links
(`src/config.py:13-16`).

## 8. Testing

No test talks to Microsoft or ORCID. A test RSA key signs ID tokens; the token endpoint and key sets
are served by a `MockTransport`. `tests/conftest.py` sets `MICROSOFT_CLIENT_ID=""` in the environment
before anything imports `src`, so a host `.env` with Microsoft settings neither turns the feature on
nor, if invalid, stops collection; tests switch it on with `asgi_app.state.microsoft_sign_in`.

- Unit: configuration validation (each failure, feature off, tenant normalisation,
  `UnsupportedAlgorithm` from the key and from the certificate, expired certificate), the authorization
  URL, the assertion (header, claims, new `jti` per call, `exp - nbf == 300`, `client_id` in the body),
  token-endpoint failures (4xx without `error`, non-dict bodies, `OverflowError`), every §5.6 and §5.7
  rejection, key cache behaviour, sign-in stamp and freshness helpers, session-epoch helpers,
  `is_safe_next_url`'s new bound.
- Integration (Postgres): migration `0031` round trip; epoch enforcement, logout, fresh session at
  ORCID sign-in (including a brand-new user), invite routes with a revoked session; reauth start and
  callback; Microsoft sign-in, linking, refusals, replay, key outage, the worst-case redirect header
  size; Settings section and unlink; admin view and removal; deleting a user removes the link; the
  authorization sweeps cover the new `/settings` route and the victim's sign-in name.
- Gate: `./scripts/ci.sh` green apart from §10's pre-existing failure; ruff `src/` and mypy counts not
  above the baseline (§4).

## 9. To verify live before turn-on

- Microsoft accepts the assertion: PS256, `x5t#S256`, `aud` = the `organizations` token endpoint (the
  certificate page lists the `{tenantId}` endpoint; no page says `organizations` is accepted),
  `client_id` in the body.
- Microsoft echoes the `nonce` in the code-flow ID token (documented for OpenID Connect sign-in and the
  ID token; the code-flow request table does not list `nonce`) — a missing nonce fails every sign-in
  closed.
- `acct` arrives once configured, and its JSON type.
- The ID token's JWS header holds only registered members: joserfc refuses unknown ones, which would
  fail every sign-in closed.
- What a user sees under each allowed organisation's consent setting, and whether a refusal ever comes
  back to the redirect URI.
- ORCID: with an active ORCID session in the browser, `/login/start?reauth=1` shows the password page;
  the returned `auth_time` is the moment the password was entered (not `iat`); the nonce and `aud`
  (our client ID) are present. Do not turn linking on unless all hold.

## 10. Pre-existing issues seen, out of scope

Security-relevant issues in existing code noticed during this work are tracked privately, not here.
Others:

- `/login?deleted=1` (`src/routers/profile.py:310`) is never rendered by `login.html`.
- The comment at `src/routers/auth.py:158-159` says ORCID "does not support PKCE (ORCID-Source#5977)".
  GitHub records that issue as closed on 2022-11-28 ("state_reason: completed") with the comment "the
  feedback has been captured on our community board … I hope to get to it in 2023"; ORCID's discovery
  document lists no `code_challenge_methods_supported`, and its OpenID Connect page does not mention
  PKCE.
- `tests/unit/test_ci_gate.py::test_the_printed_gated_tier_counts_are_the_real_ones` fails on the
  development host at baseline: the nested gate's volume-reclaim step reports `comm: file 1 is not in
  sorted order` (the host has many unrelated Docker volumes). Unrelated to auth.

## 11. Appendix — the blackbird implementation, and what this port changes

### 11.1 What blackbird has

Commits `5a11a106` (feature) and `3bafb292` (authlib 1.8 / httpx2 compatibility): 16 `src/` files
(+1153/−59), 20 test files (+1470/−11), migration `0066`, spec and plan. No later commit touches the
Microsoft modules; later commits change shared files (the `pending_access` payload gained `user_role`
and `access_status`, login and settings copy, presentation keys) and add unrelated settings. Scope:
admins, managers and reviewers only (`MICROSOFT_SIGN_IN_ROLES`), as a second login linked by
`(tid, oid)`.

| Piece | blackbird file | Ported as |
|---|---|---|
| Generic ID-token checks and per-URL key cache | `src/services/id_tokens.py` | same path, unchanged |
| Config, authorization URL, code exchange, ID token, link CRUD | `src/services/microsoft_identity.py` | same path; roles removed (U1), messages reworked (M1), all cryptography errors and `OverflowError` caught, expired-certificate ERROR |
| ORCID reauth ID token | `src/services/orcid_reauth.py` | same path, unchanged |
| Sign-in stamps, freshness | `src/web/sign_in_session.py` | `src/sign_in_session.py` (main has no `src/web/`) |
| Start, link, callback | `src/routers/microsoft_auth.py` | same path; query-code messages; no role check; M8; state checked before `error` |
| Shared tail, reauth, logout | `src/routers/auth.py` diff | applied to main's `auth.py`, with `_start_fresh_session` and the epoch from blackbird's session-epoch work |
| Settings section, unlink | `src/routers/settings.py` diff | added to main's settings router; `strftime`/`data-utc`, static `confirm()` |
| Admin view, removal, audit row | `src/routers/admin/users.py`, `templates/admin/_microsoft_link.html`, workspace PI page | `src/routers/admin.py` + inline block in `templates/admin/user_detail.html`; log line, no audit row (M5) |
| Table | `alembic/versions/0066_user_microsoft_identities.py` | inside `0031` with `users.session_epoch` (M4) |
| Test doubles | `tests/oidc_fakes.py` | same path |

It leans on blackbird-only infrastructure that main lacks: the session epoch (`0057`,
`src/services/session_epoch.py`, `tests/session_support.py`), the session reset at login
(`_start_fresh_session`, A-11), the flash queue (`src/web/flash.py`), `OriginGuardMiddleware`,
session-held impersonation (`IMPERSONATE_KEY`), `refuse_impersonation`, `users.user_role`,
`admin_audit_events`, `page_context`, the `ts` Jinja filter and `data-confirm` handler, the workspace
PI page, and the `__Host-copi-session` cookie. This design ports the session epoch, the session reset,
`refuse_impersonation` and the session test helpers; replaces the flash queue (M1), session-held
impersonation (M8), roles (U1) and the audit table (M5); and drops the Origin guard and the `__Host-`
cookie (§3, §6).

### 11.2 Findings about blackbird's implementation (checked 2026-10-21)

Its protocol choices hold against current sources: PS256 with `x5t#S256` and a 300 s assertion
(Microsoft's certificate page: "5-10 minutes after nbf at most"; MSAL 1.39.0 uses the same `alg`,
header and `aud` but a 600 s assertion it reuses), `client_id` in the token body (authlib does not
add it), a new `PrivateKeyJWT` and `claims` dict per request, RS256 pinned (no Microsoft or ORCID key
carries `alg`), the key-issuer check, the per-tenant key cache timings, and the httpx/httpx2 shim
(authlib 1.8.0 `_compat.py`). Defects and gaps:

1. **Low.** `load_config` catches `ValueError` and `TypeError` from `load_pem_private_key` but not
   `cryptography.exceptions.UnsupportedAlgorithm`, raised for an unknown key type (probed:
   `Unknown key type: 1.2.840.113549.1.1.99`), and `cert.public_key()` sits outside any `try` and
   raises `UnsupportedAlgorithm` for a certificate with an unknown key type (probed). The web app still
   refuses to start, but with a raw traceback instead of `MicrosoftConfigError`. Fixed in the port.
2. **Low.** An expired or not-yet-valid certificate only produces the 30-day warning; every sign-in
   then fails with `invalid_client` in the logs. The port logs an ERROR.
3. **Info.** `exchange_code` does not catch `OverflowError` (authlib raises it for `expires_in` like
   `1e400`, probed), so such a token response becomes a 500. Only Microsoft, over TLS, controls that
   body. Fixed in the port.
4. **Info.** The Microsoft callback acts on `error` before comparing `state`, so a cross-site
   `?error=` link pops a victim's in-progress flow and logs attacker text (capped, `%r`). Nuisance
   only; the port compares `state` first.
5. **Info.** The ORCID callback logs its `error` query value with `%s` (`auth.py:412`; main `:145`),
   which lets a crafted URL forge log lines. The port uses `%r` with a cap.
6. **Low, docs.** The blackbird plan's Global Constraints still say linking needs
   "`signed_in_via == "orcid"` and `signed_in_at` ≤ 15 minutes old"; the spec (D10) and the code
   require the stricter `orcid_reauth_at`.
7. **Info.** The blackbird spec's live-verification list (§9) omits two conditions that fail every
   sign-in closed if false — the nonce echo in a code-flow ID token, and only registered JWS header
   members — and checks only that ORCID returns `auth_time`, not what it means.
8. **Info.** A link callback past its checks can insert a link concurrently with an admin's removal
   (no row lock); it needs the ORCID password within 15 minutes, so it grants nothing more.
9. **Info.** `fetch_token` returns a 4xx JSON object with no `error` key as a token; blackbird then
   fails on the missing `id_token` with the generic message, so the outcome is correct. The port keeps
   that path and tests it.
10. **Info.** Blackbird's admins can link too, so the §6 row "Microsoft account controls a linked
    CoPI account" applies to blackbird as deployed.

## 12. Sources

Microsoft Learn (fetched 2026-10-21, twice, independently): `v2-protocols-oidc`,
`v2-oauth2-auth-code-flow`, `certificate-credentials`, `id-token-claims-reference`,
`optional-claims-reference`, `optional-claims`, `signing-key-rollover`, `access-tokens`,
`reference-error-codes`, `claims-validation`, `reply-url`, `howto-configure-app-instance-property-locks`,
`howto-create-self-signed-certificate`. Live: the `organizations`, `common` and a tenant-specific
discovery document and key set; ORCID's discovery document and key set. MSAL Python 1.39.0
(`msal/application.py`, `msal/oauth2cli/assertion.py`). ORCID-Source
`orcid-web/ORCID_AUTH_WITH_OPENID_CONNECT.md` and issue #5977 (GitHub API). nginx
`ngx_http_proxy_module` (`proxy_buffer_size`). Package source read and probed: authlib 1.8.0 and 1.7.2,
joserfc 1.7.5 and 1.7.4, cryptography 50.0.1, starlette 1.6.0, fastapi 0.141.1, itsdangerous 2.2.0,
anthropic 0.125.0 and 1.17.0 metadata.
