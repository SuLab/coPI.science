# Microsoft Sign-in Implementation Plan

> **For agentic workers:** execute with `/lathe:plan-execution` (this user's policy; it replaces
> superpowers' subagent-driven-development and executing-plans and keeps this format). Tasks 1–9
> own disjoint files and can be implemented in parallel without building or testing; Task 10
> verifies the merged result. Under plan-execution the per-task "run" and "commit" steps are
> deferred to Task 10. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Any existing CoPI account can link one work/school Microsoft account and use it as a
second way to sign in; unlinking, an admin's removal and logout end every session of the account.

**Architecture:** Migration `0031` adds `users.session_epoch` and `user_microsoft_identities`
(keyed by Microsoft's `(tid, oid)`). `get_current_user` gains blackbird's session-epoch check, and
every sign-in starts a fresh session through one shared `complete_sign_in`. A generic ID-token
module backs two validators (Microsoft's and the ORCID re-sign-in's). A new router runs auth code +
PKCE against the `organizations` endpoints with a certificate client assertion. The feature is
switched by `app.state.microsoft_sign_in`. Most new code is a port of `blackbird` @ `6053d30c`.

**Tech Stack:** FastAPI 0.141.1, Starlette 1.6.0 sessions, SQLAlchemy 2 async, Alembic 1.19.1,
authlib 1.8.0, joserfc 1.7.5, cryptography 50.0.1, httpx 0.28.1 `MockTransport` in tests, pytest +
testcontainers Postgres.

**Spec:** `docs/specs/2026-10-21-microsoft-sign-in-design.md` (revision 3). Read it first; this plan
argues from it. Section numbers below (§) are the spec's.

## Global Constraints

- Branch `microsoft-sign-in` off `main` @ `c08e33e9`, worktree
  `/home/a/scripps/coPI.science-worktrees/microsoft-sign-in`. Do not push, open a PR, deploy, or
  touch the `/home/a/scripps/coPI.science` checkout (branch `blackbird`, with uncommitted files that
  must never be checked out, stashed or restored).
- "Port `<path>`" means: start from `git -C /home/a/scripps/coPI.science show 6053d30c:<path>`, apply
  the listed deltas, and change nothing else. In every ported file: user-facing "Blackbird" →
  "CoPI"; references to the blackbird spec → `docs/specs/2026-10-21-microsoft-sign-in-design.md`
  with this spec's section numbers; `docs/operations/pis-and-access.md` →
  `specs/auth-and-user-management.md`; `src.web.sign_in_session` → `src.sign_in_session`; no
  `user_role`, `USER_ROLE_*`, `MICROSOFT_SIGN_IN_ROLES` or `may_use_microsoft_sign_in` (U1); no
  `flash` (M1); no `IMPERSONATE_KEY` (main's impersonation is the `copi-impersonate` cookie, M8); no
  `| ts(...)` filter and no `data-confirm` (main has neither: dates are `strftime` inside a
  `data-utc` span, confirmations are `onsubmit="return confirm('<static text>')"` with no user data
  in the script). Ported comments must be true on main: drop mentions of `OriginGuardMiddleware`,
  `tests/integration/test_access_revocation.py`, "spec 2026-10-01 §6.7 / A-11" (say what the code
  does instead), and "anthropic pulls it in" / "httpx2 under authlib 1.8" (say: authlib 1.8 uses
  httpx2 when it is installed; under `requirements.lock` it is not).
- Python 3.11; ruff `line-length = 100`, rules `E F I UP B`. No new `B008`: a route parameter takes
  a module-level singleton (`_DB = Depends(get_db)`, `_USER = Depends(get_current_user)`,
  `_ADMIN = Depends(get_admin_user)`), never `Depends(...)` in its default. Tests and the new test
  helpers: zero ruff findings.
- Dependencies: `authlib>=1.8.0,<2.0.0`, `joserfc>=1.7.5`, `cryptography>=45.0.1`. No other new
  package. `requirements.lock` changes in `# via` comments only.
- Lookups by `(tenant_id, object_id)` only. Never request, store or match the Microsoft `email`
  claim; never write `users.email` or `users.name` from Microsoft.
- Endpoints: `https://login.microsoftonline.com/organizations/oauth2/v2.0/{authorize,token}`; keys
  per allowed tenant at `https://login.microsoftonline.com/{tid}/discovery/v2.0/keys`.
  `scope=openid profile`, `response_mode=query`, PKCE S256, `nonce`, `prompt=select_account` for
  linking only.
- Client assertion: `alg=PS256`, header `{"typ": "JWT", "x5t#S256": <base64url SHA-256 of the cert
  DER, unpadded>}`, claims `iss=sub=client_id` (authlib's defaults), `aud` = the token URL called,
  `jti` = `uuid4().hex`, `iat=nbf=now`, `exp=now+300`; a new `PrivateKeyJWT` and a new `claims` dict
  per request; private key PEM passed as `client_secret`; `client_id` passed to `fetch_token`.
- Microsoft ID token (§5.6): RS256 pinned; `tid` in the allowlist before any key fetch;
  `iss == https://login.microsoftonline.com/{tid}/v2.0` and equal to the key's `issuer`;
  `aud == client_id` or `[client_id]`; 300 s leeway; nonce; `oid` canonical GUID; `acct` in
  `(0, "0")` and not a bool.
- ORCID reauth (§5.7): `scope=openid`, `prompt=login`, `nonce`; issuer `https://orcid.org`; keys
  `https://orcid.org/oauth/jwks`; `sub` = the session account's ORCID iD; `auth_time` within 300 s.
- Linking needs `signed_in_via == "orcid"` and an int `orcid_reauth_at` 0–900 s old, at link start
  AND in the callback.
- Impersonation (M8): reauth start, reauth callback, link start and link callback refuse when
  `impersonating(request, session_account)` (the session's own account is an admin and the request
  carries a `copi-impersonate` cookie). Unlink and admin removal use `refuse_impersonation`
  (`_is_impersonated`).
- Session keys and their writers are §5.3's table; message codes, kinds and texts are §5.8's table,
  verbatim.
- Never write a non-docstring string literal containing `/auth/microsoft/callback` in `src/`
  outside its route decorator: `tests/unit/test_reachability.py` counts such a string as a caller and
  would report the `ROUTE_ALLOWLIST` entry stale.
- Tests never call Microsoft or ORCID and never rely on `.env` for the feature: set
  `asgi_app.state.microsoft_sign_in`.
- Test interpreter: the worktree's own `.venv-test` (gitignored). It must live there:
  `tests/unit/test_ci_gate.py` and `tests/unit/test_dependencies_lock.py` run nested `ci.sh`
  invocations that ignore `VENV_PY` and look for `<repo>/.venv-test`. Run pytest as
  `.venv-test/bin/python -m pytest <paths> -q` from the worktree. Never `pip install` into it outside
  Task 0's commands.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. A brand-new user's first ORCID sign-in through `complete_sign_in` (create, flush, commit, then
   read `session_epoch` for the session) must land in onboarding with `epoch == 0`, not raise. The
   suite has no test that drives a successful `/auth/callback` today. Test in Task 5
   (`test_a_first_orcid_sign_in_creates_the_user_and_starts_at_epoch_zero`).
2. A session issued before this deploy (only `user_id`) keeps working on every page, but Settings
   offers "Sign in with ORCID again to link" and `/auth/microsoft/link` refuses it. Tests in Task 3
   (`test_a_session_with_no_epoch_key_counts_as_epoch_zero`) and Task 7
   (`test_a_pre_deploy_session_must_sign_in_again`).
3. A replayed callback, a token response with no `id_token`, a 4xx JSON body with no `error`, a
   non-object JSON body and an `expires_in` of `1e400` all end on the generic message, never a 500.
   Tests in Task 6 (`test_a_replayed_callback_is_refused`,
   `test_a_token_response_without_an_id_token_fails_cleanly`) and Task 4
   (`test_a_4xx_json_without_error_is_not_a_token`, `test_malformed_token_bodies_are_sign_in_errors`).
4. A revoked session (stale epoch) cannot accept a delegate invite. Test in Task 3
   (`test_a_revoked_session_cannot_accept_an_invite`).
5. The redirect to Microsoft fits nginx's 4 KB upstream-header buffer in the worst case: a signed-out
   session holding the longest `post_login_redirect` the guard accepts (1024 characters), a
   `pending_invite_token`, a leftover `pending_access` with a 255-character non-ASCII name and email,
   and an `oauth_state`, then `/auth/microsoft/start`; and a fresh linking session, then
   `/auth/microsoft/link`. The status line plus every response header stays under 3584 bytes. Test in
   Task 6 (`test_the_redirect_to_microsoft_fits_nginx_header_buffer`), with the guard's bound and
   character set pinned in Task 5 (control characters and `"` would otherwise grow 6× and 2× in the
   cookie's JSON).

---

### Task 0: Workspace and baseline (no file changes)

- [ ] **Step 1: Confirm the branch** — `git -C /home/a/scripps/coPI.science-worktrees/microsoft-sign-in
  log --oneline -1` shows `c08e33e9` (or this plan's commit on top).
- [ ] **Step 2: Test interpreter from the lock** (skip if `.venv-test/bin/python` exists), from the
  worktree:
  ```bash
  uv venv --python 3.11 .venv-test && uv pip install --python .venv-test/bin/python -r requirements.lock \
    && uv pip install --python .venv-test/bin/python -e '.[dev]'
  .venv-test/bin/python -c 'import authlib, joserfc, cryptography; print(authlib.__version__, joserfc.__version__, cryptography.__version__)'
  ```
  Expected: `1.8.0 1.7.5 50.0.1`.
- [ ] **Step 3: Baseline gate** — `scripts/build-css.sh && ./scripts/ci.sh`. Expected: the figures
  in "Baseline" at the end of this plan.

---

### Task 1: Schema — migration `0031`, models, migration tooling

**Files:**
- Create: `alembic/versions/0031_session_epoch_and_microsoft_identities.py`
- Create: `src/models/microsoft_identity.py`
- Modify: `src/models/user.py` (add the column after `last_login_at`)
- Modify: `src/models/__init__.py` (import and `__all__`)
- Modify: `scripts/migrate/preflight.py`, `scripts/migrate/postflight.py`
- Modify: `tests/unit/test_migration_checks.py`, `tests/integration/test_harness_smoke.py:18`,
  `tests/integration/test_migration_0030.py`, `tests/integration/test_migration_0029.py` (comment at
  :247-256 only)
- Create: `tests/integration/test_migration_0031.py`
- Modify: `docs/production-migration.md` (line 9 — test-pinned — and the `0030` head mentions at
  lines 681, 684 and 699)

**Interfaces:**
- Produces: `User.session_epoch: Mapped[int | None]` (`Integer`, nullable, no default);
  `src.models.UserMicrosoftIdentity` with columns `id, user_id, tenant_id, object_id, sign_in_name,
  linked_at, last_sign_in_at` exactly as §5.1, `__mapper_args__ = {"eager_defaults": True}`, and
  no relationship on `User`.

- [ ] **Step 1: Write the failing migration test** `tests/integration/test_migration_0031.py`,
  following `tests/integration/test_migration_0030.py`'s shape (`_run_alembic`, `scratch_db`):
  - `test_0031_upgrade_adds_the_column_and_the_table`: seed a user at `0030`; upgrade to `"0031"`;
    assert `users.session_epoch` is `integer`, nullable, no default, NULL on the seeded row; the
    table's columns/types/nullability as §5.1; `linked_at` default `now()`; constraints
    `uq_user_microsoft_identities_user_id` = `UNIQUE (user_id)`,
    `uq_user_microsoft_identities_tenant_object` = `UNIQUE (tenant_id, object_id)`,
    `user_microsoft_identities_user_id_fkey` with `confdeltype = 'c'`.
  - `test_0031_deleting_a_user_removes_their_link`: insert a link row, `DELETE FROM users`, row gone.
  - `test_0031_downgrade_restores_0030`: downgrade to `"0030"`; the column and table are gone; the
    seeded user survives.
  - `test_0031_downgrade_is_idempotent`: drop both objects by hand, then downgrade succeeds.
  - `test_0031_is_the_only_head`: `ScriptDirectory.get_heads() == ["0031"]`.
- [ ] **Step 2: Run it** — `.venv-test/bin/python -m pytest tests/integration/test_migration_0031.py -q`;
  Expected: FAIL (no revision `0031`).
- [ ] **Step 3: Write the migration.** `revision = "0031"`, `down_revision = "0030"`. Upgrade:
  `op.add_column("users", sa.Column("session_epoch", sa.Integer(), nullable=True))`, then
  `op.create_table("user_microsoft_identities", ...)` with the FK named
  `user_microsoft_identities_user_id_fkey` (`sa.ForeignKeyConstraint(..., ondelete="CASCADE",
  name=...)`) and both named `sa.UniqueConstraint`s. Downgrade:
  `op.drop_table("user_microsoft_identities", if_exists=True)` then
  `op.drop_column("users", "session_epoch", if_exists=True)` (idempotent, the 0022+ convention).
  Docstring: what each object is for; "old code on the new schema is safe; new code on the old
  schema is not (every User load selects session_epoch), so migrate before the new web image serves
  — scripts/redeploy.sh does"; downgrade deletes every link, and a re-upgrade brings
  `session_epoch` back as NULL, which makes every revoked session valid again (spec §7.3).
- [ ] **Step 4: Model and column.** Create `src/models/microsoft_identity.py` by porting
  `src/models/microsoft_identity.py` (deltas: the FK gets `name="user_microsoft_identities_user_id_fkey"`
  to match the migration; docstring migration number `0031`). Add `session_epoch` to `User` with a
  one-line comment: "Bumped to end every session of the account (src/services/session_epoch.py);
  NULL reads as 0." Export the model from `src/models/__init__.py`.
- [ ] **Step 5: Register `0031` with the migration tooling.**
  - `preflight.py`: `DEFAULT_TARGET = "0031"`; append `"0031"` to `REVISION_ORDER`;
    `PLANNED_OBJECTS` += `("0031","column","session_epoch","users")`,
    `("0031","table","user_microsoft_identities")`, and the three constraints with table
    `user_microsoft_identities`; `REVISION_COST_NOTES["0031"] = "0031 one nullable ADD COLUMN on
    users (ACCESS EXCLUSIVE on users, catalogue-only, no rewrite) plus one new empty table whose
    foreign key takes SHARE ROW EXCLUSIVE on users"`.
  - `postflight.py`: `EXPECTED_COLUMNS` += `("users","session_epoch","integer",True,"")` and the
    seven table columns (`linked_at` default `"now()"`, the others per the 0020 precedent);
    `EXPECTED_TABLES` += `"user_microsoft_identities"`; `EXPECTED_CONSTRAINTS` += the three
    constraints with their exact `pg_get_constraintdef` text
    (`FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE` for the FK); and, per the
    comment at `postflight.py:178-180`, `EXPECTED_INDEXES` += the two unique indexes
    (`"uq_user_microsoft_identities_user_id": "USING btree (user_id)"`,
    `"uq_user_microsoft_identities_tenant_object": "USING btree (tenant_id, object_id)"`).
  - `tests/unit/test_migration_checks.py`: add `"0031"` to the revision tuple (≈:1005-1008); add
    `"user_microsoft_identities"` to the `CHAIN_CREATED_TABLES` literal (≈:909-914); add
    `assert "session_epoch" in names` and `assert "user_microsoft_identities" in names  # 0031`
    beside the 0030 asserts (≈:1152-1153).
  - `tests/integration/test_harness_smoke.py:18`: `"0031"`.
  - `tests/integration/test_migration_0030.py`: pin its upgrade to `"0030"` instead of `"head"` and
    replace `test_0030_is_the_only_head` with the comment `test_migration_0029.py:247-256` used for
    the same move. In `test_migration_0029.py:247-256`, point the comment at
    `test_migration_0031.py::test_0031_is_the_only_head`.
  - `docs/production-migration.md`: line 9 → `**0031** at the time of writing`; lines 681, 684,
    699 → `0031`.
- [ ] **Step 6: Run** — `.venv-test/bin/python -m pytest tests/integration/test_migration_0031.py
  tests/integration/test_migration_0030.py tests/integration/test_harness_smoke.py
  tests/unit/test_migration_checks.py tests/integration/test_migration_tooling_chain.py
  tests/unit/test_runbook_docs.py tests/integration/test_db_contract.py -q`; Expected: PASS.
- [ ] **Step 7: Commit** — `feat(auth): migration 0031 — users.session_epoch and user_microsoft_identities`.

---

### Task 2: Test harness — `asgi_app`, session helpers, lint targets

**Files:**
- Modify: `tests/conftest.py`
- Create: `tests/session_support.py`
- Modify: `scripts/ci.sh` (`LINT_TARGETS` only)

**Interfaces:**
- Produces fixture `asgi_app` (the `FastAPI` instance `client` serves), fixture `client` (unchanged
  behaviour, now built on `asgi_app`), and the `fake_idp` fixture registered for every test (import
  from `tests.oidc_fakes`, Task 4).
- Produces `tests/session_support.py`: `SESSION_COOKIE = "copi-session"`,
  `sign_session(payload: dict) -> str`, `raw_session_headers(payload: dict) -> dict[str, str]`,
  `session_headers(user_id, *, impersonate=None, **extra) -> dict[str, str]` (adds
  `; copi-impersonate=<id>` to the `Cookie` header when `impersonate` is given),
  `session_from_response(response) -> dict | None` (`None` = no session cookie set; `{}` = cleared,
  Starlette writes the literal `null`).

- [ ] **Step 1: Port `tests/session_support.py`** with deltas: cookie name is the constant
  `copi-session` (main has no `session_cookie_name`); impersonation is the `copi-impersonate`
  cookie, not session keys; drop the `IMPERSONATE_*` imports.
- [ ] **Step 2: `tests/conftest.py`.**
  - Directly after the module's import block (conftest has no module-level `src` import and loads
    before any test module, so this runs before `src.main` is first imported; placing it above the
    imports would trip E402): `os.environ["MICROSOFT_CLIENT_ID"] = ""` with a comment (environment
    variables override `.env`; a host `.env` with Microsoft settings must neither turn the feature on
    in tests nor, if invalid, stop collection when a module imports `src.main`).
  - Split `client`: a new function-scoped `asgi_app` fixture holds the two monkeypatches,
    `create_app()` and the `get_db` override, yields the app and clears the overrides; `client`
    depends on `asgi_app` and keeps only the `httpx.AsyncClient` part.
  - Register `fake_idp` with `from tests.oidc_fakes import fake_idp  # noqa: F401`.
- [ ] **Step 3: `scripts/ci.sh`** — add `tests/oidc_fakes.py tests/session_support.py` to the first
  line of `LINT_TARGETS` (`:136-137`), so the test-lint step covers them.
- [ ] **Step 4: Run** (after Task 4 exists) — `.venv-test/bin/python -m pytest
  tests/integration/test_harness_smoke.py tests/integration/test_admin_users.py
  tests/characterization/test_auth_and_admin_routes.py tests/unit/test_ci_gate.py -q`. Expected:
  PASS with unchanged counts (apart from Baseline's pre-existing failure).
- [ ] **Step 5: Commit** — `test: asgi_app fixture, shared session helpers, feature off by default`.

---

### Task 3: Session epoch, revocation, impersonation helpers, invite routes

**Files:**
- Create: `src/services/session_epoch.py`
- Modify: `src/dependencies.py`
- Modify: `src/routers/invite.py`
- Create: `tests/integration/test_session_epoch.py`
- Create: `tests/unit/test_session_epoch_helpers.py`

**Interfaces:**
- Consumes: `User.session_epoch` (Task 1); `tests.session_support`, `asgi_app` (Task 2).
- Produces in `src/services/session_epoch.py`: `SESSION_EPOCH_KEY = "epoch"`,
  `current_epoch(user: User) -> int`, `epoch_in_session(session: Mapping[str, Any]) -> int | None`,
  `async bump_session_epoch(db: AsyncSession, user_id: uuid.UUID, *, expected_epoch: int | None =
  None) -> None` (does not commit).
- Produces in `src/dependencies.py`: `IMPERSONATE_COOKIE = "copi-impersonate"`;
  `async session_account(request: Request, db: AsyncSession) -> User` (today's checks — id parse,
  account exists, access allowed — plus the epoch check; raises the same 302 `HTTPException`s;
  never applies impersonation); `get_current_user` = `session_account` + today's impersonation
  block; `refuse_impersonation(current_user: User, detail: str) -> None` (403 when
  `_is_impersonated`); `account_of(current_user: User) -> User` (`_real_admin` when impersonated,
  else the user); `impersonating(request: Request, account: User) -> bool` (`account.is_admin` and
  `request.cookies.get(IMPERSONATE_COOKIE)` is non-empty — M8).

- [ ] **Step 1: Unit tests** `tests/unit/test_session_epoch_helpers.py`: `current_epoch` reads
  NULL as 0; `epoch_in_session` returns 0 for a missing key, the int for an int, `None` for
  `"0"`, `True`, `None`, `0.0`; `impersonating` is true only for an admin with a non-empty cookie
  (including one that names no user); `account_of` returns `_real_admin` for an impersonated user.
- [ ] **Step 2: Integration tests** `tests/integration/test_session_epoch.py` (port the "epoch check"
  section of blackbird's `tests/integration/test_session_epoch.py`; drop its
  `tests.integration.test_auth_allowlist_gate._fake_oauth` import, which does not exist on main;
  users from `factories.make_user`):
  - `test_a_session_from_an_older_epoch_is_refused_and_cleared` (stale → 302 to
    `/login?next=%2Fprofile`, session `{}`; current → 200).
  - `test_a_session_with_no_epoch_key_counts_as_epoch_zero`.
  - `test_a_malformed_epoch_is_refused` (parametrized `"0"`, `True`, `None`, `0.0`).
  - `test_the_epoch_is_checked_on_the_session_account_not_the_impersonated_one`: an admin at
    epoch 1 with a stale session and a `copi-impersonate` cookie is refused.
  - `test_a_denied_account_is_still_bounced_before_the_epoch_check` (today's behaviour holds:
    session cleared, `/login`).
  - `test_bump_with_expected_epoch_only_moves_a_matching_epoch` and
    `test_bump_without_expected_epoch_always_moves` (call the service on `db_session`).
  - `test_a_revoked_session_cannot_accept_an_invite`: a pending `DelegateInvitation` for the user's
    email; `POST /invite/{token}/accept` with an epoch-0 session for a user at epoch 1 → no
    `AgentDelegate` row, redirect to `/invite/{token}`; the same with a current session → accepted.
  - `test_a_revoked_session_viewing_an_invite_is_sent_to_login_with_the_token_kept`: `GET
    /invite/{token}` with a stale session → 302 `/login`, and the response's session holds
    `pending_invite_token`.
- [ ] **Step 3: Run them** — Expected: FAIL (no module / no check).
- [ ] **Step 4: Implement.** Port `src/services/session_epoch.py` (delta: the module docstring names
  this repo's bumpers — logout, the user's unlink, the admin's removal). In `src/dependencies.py`,
  factor `session_account` out of `get_current_user` and add, after the access check and before
  impersonation: `if epoch_in_session(request.session) != current_epoch(session_user):
  request.session.clear(); raise HTTPException(302, headers={"Location": _login_location(request)})`,
  with a comment that the epoch belongs to the account holding the session. Add
  `refuse_impersonation` (ported), `account_of` and `impersonating`. In `src/routers/invite.py`,
  replace both raw `select(User).where(User.id == user_id_str)` lookups with a local
  `_signed_in_account(request, db) -> User | None` that returns `None` for no `user_id` and for any
  `HTTPException` from `session_account` (which has already cleared the session); keep the existing
  branches for `None` (GET stores `pending_invite_token` then redirects to `/login`; POST redirects
  to `/invite/{token}`). Invites keep acting on the real account, never an impersonated one
  (unchanged).
- [ ] **Step 5: Run** the two new files plus `tests/integration/test_invite_redirect.py
  tests/integration/test_admin_users.py tests/integration/test_onboarding_flow.py` — Expected: PASS.
- [ ] **Step 6: Commit** — `feat(auth): server-side session revocation by epoch; invites honour it`.

---

### Task 4: ID tokens, Microsoft identity service, ORCID reauth validator, settings, dependencies

**Files:**
- Create: `src/services/id_tokens.py`, `src/services/microsoft_identity.py`,
  `src/services/orcid_reauth.py`
- Modify: `src/config.py` (five settings after the ORCID block)
- Modify: `pyproject.toml`, `requirements.lock`, `.env.example`
- Modify: `tests/unit/test_config_secret_redaction.py`
- Create: `tests/oidc_fakes.py`, `tests/unit/test_id_tokens.py`,
  `tests/unit/test_microsoft_identity.py`, `tests/unit/test_orcid_reauth.py`

**Interfaces:**
- Consumes: `UserMicrosoftIdentity`, `User` (Task 1).
- Produces in `src/services/id_tokens.py` (port, unchanged): `IdTokenError(check, detail)` with
  `.check` and `.tid`, `unverified_parts`, `verify_signature`, `check_audience`, `check_times`,
  `KeySetCache.key_for(url, kid, *, now)`, `HTTP_TRANSPORT`, `HTTP_TIMEOUT_SECONDS = 10.0`,
  `LEEWAY_SECONDS = 300`.
- Produces in `src/services/microsoft_identity.py`: `AUTHORIZE_URL`, `TOKEN_URL`,
  `KEYS_URL_TEMPLATE`, `ISSUER_TEMPLATE`, `SCOPE`, `TOKEN_HTTP`, `TOKEN_TRANSPORT`, `KEYS`;
  `MICROSOFT_MESSAGES: dict[str, tuple[str, str]]` (§5.8: code → (text, kind)); `message_for(code:
  str | None) -> tuple[str, str] | None`; `MicrosoftConfigError`, `MicrosoftSignInError`,
  `InteractionRequired`; `MicrosoftSignInConfig`, `MicrosoftIdentity`, `RemovedLink`;
  `load_config(settings) -> MicrosoftSignInConfig | None`; `authorization_url(config, *, state,
  nonce, code_verifier, select_account) -> str`; `async exchange_code(config, *, code,
  code_verifier) -> dict`; `async validate_id_token(config, id_token, *, nonce, now) ->
  MicrosoftIdentity`; `async find_linked_user(db, identity) -> User | None`; `async
  record_sign_in(db, user_id) -> None` (stamps `last_sign_in_at`); `async get_link(db, user_id)`;
  `async link_account(db, user_id, identity) -> str | None` (returns `"user_already_linked"` or
  `"account_taken"`); `async link_view(db, user_id) -> dict | None`; `async unlink(db, user_id) ->
  RemovedLink | None`.
- Produces in `src/services/orcid_reauth.py` (port, unchanged): `ORCID_ISSUER`, `ORCID_KEYS_URL`,
  `REAUTH_MAX_AGE_SECONDS = 300`, `KEYS`, `async validate_reauth_id_token(id_token, *, client_id,
  nonce, orcid, now) -> None`.
- Produces in `tests/oidc_fakes.py`: as blackbird's (`TENANT`, `OTHER_TENANT`, `CLIENT_ID`, `OID`,
  `KID`, `DROP`, `SIGNING_PEM`, `CERT_PEM`, `CERT_KEY_PEM`, `make_certificate`, `b64`, `public_jwk`,
  `sign`, `forged`, `microsoft_claims`, `settings_for`, `make_config`, `FakeIdP`, `fake_idp`), with
  `REDIRECT_URI = "https://copi.example/auth/microsoft/callback"`.

- [ ] **Step 1: Port the tests.** `tests/unit/test_id_tokens.py` and `tests/unit/test_orcid_reauth.py`
  unchanged. `tests/unit/test_microsoft_identity.py` with deltas:
  - delete `test_only_admins_managers_and_reviewers_may_use_it`;
  - add `test_an_unsupported_private_key_type_is_a_config_error` and
    `test_a_certificate_with_an_unsupported_key_type_is_a_config_error` (build each by replacing the
    DER `rsaEncryption` OID TLV `06092a864886f70d010101` with `06092a864886f70d010163` — both raise
    `UnsupportedAlgorithm` in cryptography 50.0.1 — and expect `MicrosoftConfigError`);
  - add `test_an_expired_certificate_logs_an_error` (extend `tests/oidc_fakes.make_certificate`
    with `start_days_ago: int = 1`; a certificate with `start_days_ago=2, days=-1`, i.e.
    `not_valid_after` yesterday → an ERROR record, config still returned);
  - add `test_a_4xx_json_without_error_is_not_a_token` (token endpoint answers `400 {"foo": "bar"}`;
    `exchange_code` returns it, and `validate_id_token(config, tokens.get("id_token"), ...)` raises
    `IdTokenError` with `check == "missing"`);
  - add `test_malformed_token_bodies_are_sign_in_errors` (parametrized `200 5`, `200 null`,
    `400 ["error"]`, `200 [1, 2]`, `200 {"access_token":"a","expires_in":1e400}` → each raises
    `MicrosoftSignInError`);
  - add `test_every_message_code_has_text_and_a_known_kind` (every value is `(non-empty str, kind in
    {"error","success","info"})` and the key set equals §5.8's codes).
  `tests/unit/test_config_secret_redaction.py`: add `microsoft_client_id`, `microsoft_redirect_uri`,
  `microsoft_allowed_tenant_ids`, `microsoft_certificate_b64` to `NON_SECRET_STR_FIELDS`, and a test
  that a non-empty `microsoft_private_key_b64` is masked in `repr(Settings(...))`.
- [ ] **Step 2: Run** — Expected: FAIL (modules missing).
- [ ] **Step 3: Implement.** Port `src/services/id_tokens.py` and `src/services/orcid_reauth.py`
  as-is (spec references updated). Port `src/services/microsoft_identity.py` with deltas:
  - remove `USER_ROLE_*` imports, `MICROSOFT_SIGN_IN_ROLES`, `may_use_microsoft_sign_in`;
  - replace `MICROSOFT_ERROR_MESSAGES` with `MICROSOFT_MESSAGES` (§5.8, text verbatim) and add
    `message_for`;
  - `link_account` returns `"user_already_linked"` / `"account_taken"`;
  - `load_config`: catch `cryptography.exceptions.UnsupportedAlgorithm` (besides `ValueError`,
    `TypeError`) around `load_pem_private_key`, and wrap `cert.public_key()` the same way; log at
    ERROR when `now > not_valid_after_utc` or `now < not_valid_before_utc` (keep the 30-day WARNING);
    the warning's runbook pointer is `specs/auth-and-user-management.md`;
  - `exchange_code`: add `OverflowError` to the caught exceptions;
  - module docstring: "every account" instead of the role sentence; keep the httpx/httpx2
    `TOKEN_HTTP` shim and its comment (true under authlib 1.8.0, `_compat.py:9-16`).
  `src/config.py`: the five fields and comment from blackbird's `src/config.py` diff (spec path
  updated). `.env.example`: the five empty keys with a two-line comment pointing at
  `specs/auth-and-user-management.md` for the runbook.
  `pyproject.toml`: `"authlib>=1.8.0,<2.0.0"`, add `"joserfc>=1.7.5"` and `"cryptography>=45.0.1"`
  next to it with a one-line comment ("imported directly by src/services/{id_tokens,
  microsoft_identity}.py; already in the lock via authlib"). Then regenerate the lock with the
  command `scripts/ci.sh` prints (`uv run --isolated --no-project --python 3.11 --with pip-tools --
  python -m piptools compile --generate-hashes --no-header -o requirements.lock pyproject.toml`).
- [ ] **Step 4: Check the lock diff** — `git diff -U0 requirements.lock | grep '^[-+][a-z]'`.
  Expected: no output (no version or hash line changed; only `# via` comments). If any pin moved,
  `git checkout requirements.lock` and edit only the two `# via` blocks by hand.
- [ ] **Step 5: Run** — `.venv-test/bin/python -m pytest tests/unit/test_id_tokens.py
  tests/unit/test_microsoft_identity.py tests/unit/test_orcid_reauth.py
  tests/unit/test_config_secret_redaction.py tests/unit/test_dependencies_lock.py -q` and
  `.venv-test/bin/python scripts/check_lockfile.py`; Expected: PASS.
- [ ] **Step 6: Commit** — `feat(auth): Microsoft and ORCID ID-token validation, Microsoft configuration`.

---

### Task 5: Shared sign-in tail, fresh session, logout, ORCID reauth, login page, `next` bound

**Files:**
- Modify: `src/routers/auth.py`
- Create: `src/sign_in_session.py`
- Modify: `templates/login.html`
- Modify: `tests/unit/test_open_redirect.py`, `tests/unit/test_login_redirect.py`
- Create: `tests/integration/test_orcid_sign_in.py`, `tests/integration/test_orcid_reauth_flow.py`,
  `tests/unit/test_sign_in_session.py`

**Interfaces:**
- Consumes: `session_epoch` helpers, `session_account`, `get_current_user`, `account_of`,
  `impersonating`, `IMPERSONATE_COOKIE` (Task 3); `validate_reauth_id_token`, `IdTokenError`,
  `message_for`, `make_config` (Task 4); `asgi_app`, `tests.session_support` (Task 2).
- Produces in `src/sign_in_session.py` (port of `src/web/sign_in_session.py`, unchanged):
  `SIGNED_IN_AT_KEY`, `SIGNED_IN_VIA_KEY`, `VIA_ORCID`, `VIA_MICROSOFT`, `ORCID_REAUTH_AT_KEY`,
  `LINK_REAUTH_MAX_AGE_SECONDS = 900`, `record_sign_in(session, via, now=None)`,
  `record_orcid_reauth(session, now=None)`, `fresh_orcid_sign_in(session, now=None) -> bool`.
- Produces in `src/routers/auth.py`: `async complete_sign_in(request: Request, db: AsyncSession,
  user: User, *, via: str) -> RedirectResponse`; `ORCID_REAUTH_KEY = "orcid_reauth"`;
  `MAX_NEXT_URL_CHARS = 1024`.

- [ ] **Step 1: Unit tests.** `tests/unit/test_sign_in_session.py` (port the freshness cases: via
  must be `"orcid"`; `orcid_reauth_at` int, not bool; 0–900 s inclusive; future value refused).
  `tests/unit/test_open_redirect.py`: keep `"x" * 2001`; add `"/" + "x" * 1024` (1025 chars),
  `"/profile?q=é"`, `'/profile?q="'`, `"/profile?q=\x01"`, `"/profile?q=\x7f"` and
  `"/profile?q=a b"` to the rejected cases. `tests/unit/test_login_redirect.py`: on a real page,
  `"/profile?q=" + "x" * 1013` (1024 characters) is accepted and `"/profile?q=" + "x" * 1014` is
  rejected.
- [ ] **Step 2: Integration tests** `tests/integration/test_orcid_sign_in.py`. A local fake replaces
  `auth_module._get_oauth_client` with an object whose `async fetch_token(url, code, grant_type)`
  returns `{"orcid": <id>, "name": "Test User"}`, and `auth_module.fetch_orcid_profile` with an
  async stub; requests carry `raw_session_headers({"oauth_state": "s", ...})` and hit
  `/auth/callback?code=c&state=s`:
  - `test_a_first_orcid_sign_in_creates_the_user_and_starts_at_epoch_zero` (allowlisted ORCID → new
    user, 302 `/onboarding`, session `{"user_id", "epoch": 0, "signed_in_at", "signed_in_via":
    "orcid"}`).
  - `test_sign_in_clears_the_pre_login_session_and_keeps_the_vetted_next` (planted keys dropped;
    `post_login_redirect` `/profile/edit` kept and used).
  - `test_sign_in_drops_an_unsafe_next`, `test_sign_in_keeps_a_pending_invite_token`.
  - `test_sign_in_stores_the_accounts_epoch` (`session_epoch=4` → `epoch: 4`).
  - `test_sign_in_deletes_the_impersonation_cookie` (response sets `copi-impersonate` expired).
  - `test_a_refused_sign_in_carries_only_pending_access` (`access_status="denied"` →
    `/access-pending`, session keys `{"pending_access"}`, `pending_access["orcid"]` set).
  - `test_the_orcid_error_is_logged_with_repr_and_a_cap` (`/auth/callback?error=a%0Ab` + 200 `x` →
    the log record contains `'a\\nb` and is at most 80 characters of the value).
  - `test_logout_ends_every_session` (logout with the current epoch bumps; another session for the
    same user is then refused on `/profile`); `test_logout_with_a_stale_session_bumps_nothing`;
    `test_logout_while_signed_out_still_redirects_to_login`.
  - Login page: `test_the_login_page_shows_microsoft_only_when_on` (`asgi_app.state.microsoft_sign_in
    = make_config()` → `href="/auth/microsoft/start"` present; absent when `None`),
    `test_the_login_page_shows_microsoft_messages` (`/login?error=microsoft_not_linked` shows its
    text; `/login?error=microsoft_unlinked` shows its text outside the red box),
    `test_an_unknown_error_code_shows_the_generic_text` (`/login?error=<script>` shows "Sign in
    failed. Please try again." and not the code).
- [ ] **Step 3: Integration tests** `tests/integration/test_orcid_reauth_flow.py`: port blackbird's
  file with deltas — users from `factories.make_user` (any account), impersonation via
  `session_headers(admin.id, impersonate=user.id)`, failures assert a redirect to
  `/settings?microsoft=reauth_failed` (not a flash), an impersonated reauth start asserts
  `/settings?microsoft=impersonating`, and add `test_reauth_with_a_dangling_impersonation_cookie_is_refused`
  (admin, cookie naming no user → start redirects to `/settings?microsoft=impersonating`). Keep every
  case: plain `/login/start` still sends a signed-in user to `/` and still sends
  `scope=/authenticate`; reauth start sends `scope=openid`, `prompt=login`, a nonce, stores
  `orcid_reauth` and `post_login_redirect=/settings`; the callback refuses a missing `id_token`, each
  §5.7 failure, a stale `auth_time`, another ORCID iD, an expired flow, and leaves the session
  untouched; success writes `orcid_reauth_at` into the fresh session and lands on `/settings`. Add
  `test_the_reauth_callback_refuses_while_impersonating` (admin session holding `orcid_reauth`, a
  `copi-impersonate` cookie, a valid ID token → `/settings?microsoft=reauth_failed`, no
  `orcid_reauth_at`).
- [ ] **Step 4: Run** — Expected: FAIL.
- [ ] **Step 5: Implement `src/routers/auth.py`.** From blackbird's `auth.py` diff and its
  `_start_fresh_session`, `_session_user_id` and `logout`:
  - `is_safe_next_url`: reject unless `len(target) <= MAX_NEXT_URL_CHARS` (1024; today 2000) and
    every character is in `"!" … "~"` (0x21–0x7e) other than `"` (backslash is already refused),
    with a comment naming nginx's 4 KB upstream-header buffer and the JSON growth of `"` and control
    characters (spec M9). The existing CR/LF/NUL check becomes redundant; keep it.
  - `_start_fresh_session(request)` (port as-is).
  - `_post_login_redirect(request, user, *, via) -> RedirectResponse`: writes `user_id`, `epoch`
    (`current_epoch(user)`), `record_sign_in(request.session, via)`, pops `pending_access`, then
    today's landing logic moved verbatim from `auth_callback` (`:284-297`).
  - `complete_sign_in(...)`: as blackbird's, with `pending_access` built inline in today's shape
    (`user_id, orcid, email, name`), and `response.delete_cookie(IMPERSONATE_COOKIE)` on whichever
    response it returns.
  - `auth_callback`: pop `orcid_reauth` first; log `error` as `logger.warning("ORCID OAuth error:
    %r", error[:80])`; failures of a reauth go to `/settings?microsoft=reauth_failed` (pop
    `post_login_redirect`), others to today's `/login?error=...` URLs; after the `orcid` check, a
    reauth runs `_orcid_reauth_refusal` (port; deltas: the account comes from `_session_user_id` +
    `db.get(User, ...)`; impersonation is `impersonating(request, user)`); keep today's profile
    fetch, allowlist, find-or-create block (`:184-262`) unchanged; end with `response = await
    complete_sign_in(request, db, user, via=VIA_ORCID)` then, for a reauth with `user_id` in the
    fresh session, `record_orcid_reauth(request.session)`.
  - `login_start(request, reauth: str | None = None, db: AsyncSession = _DB)`: signed-out → today's
    behaviour plus `request.session.pop(ORCID_REAUTH_KEY, None)`; signed-in and `reauth != "1"` →
    `/`; signed-in and `reauth == "1"` → `current = await get_current_user(request, db)` (lets its
    302 propagate); `impersonating(request, account_of(current))` →
    `/settings?microsoft=impersonating`; else the openid/login/nonce authorization URL and the two
    session keys.
  - `logout(request, db: AsyncSession = _DB)`: blackbird's epoch bump, then today's
    `session.clear()` and `delete_cookie("copi-impersonate")`.
  - `login`: pass `error_message` (text of `message_for(error)` when its kind is `error`), `notice`
    (text when its kind is `info` or `success`) and `microsoft_enabled`.
  Module-level `_DB = Depends(get_db)`; no new B008.
- [ ] **Step 6: `templates/login.html`.** Render the red box only when `error` and not `notice`; in
  it add `{% elif error_message %}{{ error_message }}` before the final `else`; when `notice`, a blue
  info box with it; after the ORCID button, when `microsoft_enabled`, an `<a
  href="/auth/microsoft/start">` styled as a secondary full-width button, text "Sign in with
  Microsoft". Literal Tailwind tokens only (`CLAUDE.md` "Stylesheet build").
- [ ] **Step 7: Run** the new files plus `tests/characterization/test_auth_and_admin_routes.py
  tests/unit/test_login_redirect.py tests/unit/test_open_redirect.py
  tests/integration/test_invite_redirect.py` — Expected: PASS.
- [ ] **Step 8: Commit** — `feat(auth): one sign-in tail with a fresh session and epoch; logout ends every session; ORCID re-sign-in`.

---

### Task 6: Microsoft router and app wiring

**Files:**
- Create: `src/routers/microsoft_auth.py`
- Modify: `src/main.py`
- Modify: `tests/unit/test_reachability.py`
- Create: `tests/integration/test_microsoft_sign_in.py`

**Interfaces:**
- Consumes: Task 2 (`asgi_app`, `tests.session_support`), Task 3 (`get_current_user`,
  `account_of`, `impersonating`, `refuse_impersonation`), Task 4 (service, `tests.oidc_fakes`),
  Task 5 (`complete_sign_in`, `src.sign_in_session`).
- Produces: routes `GET /auth/microsoft/start`, `GET /auth/microsoft/link`,
  `GET /auth/microsoft/callback`; `FLOW_KEY = "microsoft_flow"`, `FLOW_MAX_AGE_SECONDS = 600`;
  `app.state.microsoft_sign_in` set in `create_app()`.

- [ ] **Step 1: Tests** — port blackbird's `tests/integration/test_microsoft_sign_in.py` with
  deltas: users from `factories.make_user` (no roles); delete the role cases (`microsoft_not_staff`;
  `test_link_is_refused_for_pis_and_while_impersonating` becomes
  `test_link_is_refused_while_impersonating` → 403, plus a dangling-cookie case); a signed-in
  sign-in lands on `/profile` (main's landing), not `/workspace/pis`; link outcomes assert
  `/settings?microsoft=<code>` (`linked`, `account_taken`, `user_already_linked`, `link_refused`,
  `microsoft_failed`, ...) instead of flashes; impersonation via the cookie. Keep: feature-off 404s,
  start stores a flow, start sends a signed-in user home, a linked user signs in (session keys,
  `last_sign_in_at`, no new user, email unchanged), not linked, denied → `/access-pending`, callback
  errors, replay, no `id_token`, cold-cache key outage, `interaction_required`, link freshness at
  start and callback, account picker on link, link success, second link refused, the callback must be
  the session that started it. Add:
  - `test_an_error_with_a_foreign_state_is_a_state_mismatch_and_not_logged` (flow state `st`,
    `?error=access_denied&state=other` → `state_mismatch`, no log record carrying the error);
  - `test_the_sign_in_response_deletes_the_impersonation_cookie`;
  - `test_the_redirect_to_microsoft_fits_nginx_header_buffer` (Review Focus 5: both cases; size =
    len of the status line plus `sum(len(k) + len(v) + 4 for k, v in r.headers.multi_items())`;
    assert `< 3584`);
  - `test_start_drops_a_leftover_pending_access_and_oauth_state`;
  - `test_the_link_callback_refuses_while_impersonating` (admin session, fresh, with a
    `copi-impersonate` cookie naming no user → `/settings?microsoft=impersonating`, no row).
- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement.** Port `src/routers/microsoft_auth.py` with deltas:
  - `_fail(request, purpose, code)`: `purpose == "link"` or a `user_id` in the session →
    `/settings?microsoft=<code>`; else `/login?error=<code>`. Codes per §5.8; success →
    `/settings?microsoft=linked`.
  - `microsoft_callback`: after popping the flow, compare `state` with the flow's first (missing
    flow, missing or different state → `state_mismatch`); only then handle `error` (logged with `%r`,
    `error[:80]` and `error_description[:300]`), then `code` and the 600 s age.
  - `microsoft_link_start`: 403 when `impersonating(request, account_of(current_user))` (M8); no
    role check; not fresh → `/settings?microsoft=link_refused`.
  - `_finish_sign_in`: no role check.
  - `microsoft_start`: pop `pending_access` and `oauth_state` before writing the flow (spec M9).
  - `_finish_link`: `current_user = await get_current_user(request, db)`; when
    `impersonating(request, account_of(current_user))` or `current_user._is_impersonated` →
    `/settings?microsoft=impersonating`; when `str(current_user.id) != flow["user_id"]` or not fresh
    → `/settings?microsoft=link_refused`.
  - No `IMPERSONATE_KEY`, no `flash`.
  `src/main.py`: `application.state.microsoft_sign_in = load_microsoft_config(settings)` after the
  session middleware, with blackbird's comment (S1 → B7), and
  `application.include_router(microsoft_auth.router, tags=["auth"])` after `auth.router`.
  `tests/unit/test_reachability.py`:
  - `ROUTE_ALLOWLIST[("GET", "/auth/microsoft/callback")] = "Microsoft Entra redirect_uri — the
    caller is login.microsoftonline.com. The value registered is settings.microsoft_redirect_uri
    (src/config.py), not a link in this app."`
  - `GUARDED_IMPORT_ALLOWLIST["httpx2"] = "Optional: authlib 1.8 builds its OAuth client on httpx2
    when installed (src/services/microsoft_identity.py TOKEN_HTTP); absent under requirements.lock,
    which pins anthropic<1.0 on httpx."`, and update the "Empty today" comment above it.
- [ ] **Step 4: Run** the new file and `tests/unit/test_reachability.py` — Expected: PASS once
  Tasks 5 and 7 add the `/auth/microsoft/start` and `/auth/microsoft/link` links.
- [ ] **Step 5: Commit** — `feat(auth): Microsoft sign-in and linking routes`.

---

### Task 7: Settings section, user unlink, authorization sweeps

**Files:**
- Modify: `src/routers/settings.py`, `templates/settings.html`
- Modify: `tests/integration/test_onboarding_flow.py`
- Create: `tests/integration/test_microsoft_settings.py`

**Interfaces:**
- Consumes: Task 2 (`asgi_app`, `tests.session_support`), Task 3 (`refuse_impersonation`,
  `bump_session_epoch`, `epoch_in_session`), Task 4 (`link_view`, `unlink`, `message_for`,
  `make_config`), Task 5 (`fresh_orcid_sign_in`).
- Produces: `POST /settings/microsoft/unlink`; template variable `microsoft` (`None` or
  `{"link": dict | None, "can_link": bool}`); `flash_message`/`flash_type` from `?microsoft=`.

- [ ] **Step 1: Tests** `tests/integration/test_microsoft_settings.py` — port blackbird's file with
  deltas: no roles (`test_the_section_is_hidden_when_off_and_while_impersonating`, and
  `test_the_section_shows_for_a_pi_and_a_delegate`); outcomes are redirects with codes
  (`/login?error=microsoft_unlinked`, `/settings?microsoft=nothing_linked`); impersonation by
  cookie. Keep: fresh session sees the link; ordinary ORCID sign-in and a pre-deploy session must sign
  in again; a linked account shows its name, its linked date and the unlink form (whose
  `onsubmit` holds no user data); unlink removes the link and ends every session (the current one and
  another, each refused afterwards); unlink at any epoch; a concurrent bump; unlink signed out changes
  nothing; nothing linked; 404 when off, 403 while impersonating. Add
  `test_a_known_message_code_renders_and_an_unknown_one_does_not` (`?microsoft=linked` shows its
  text; `?microsoft=<b>x</b>` shows nothing of it).
- [ ] **Step 2: Sweep update** in `tests/integration/test_onboarding_flow.py`:
  - `Ep` gains `microsoft_link: bool = False` and `impersonation: str = "honoured"` (`"refused"`).
  - `ENDPOINTS` += `Ep("POST", "/settings/microsoft/unlink", lambda u: {}, microsoft_link=True,
    impersonation="refused")`; the settings comment says `(5)`; `assert len(ENDPOINTS) == 16`;
    update the counts in the module docstring (`:3-5`, "Fifteen" → "Sixteen", settings `(4)` →
    `(5)`), the inventory docstring (`:338`, "a 16th" → "a 17th") and `_snapshot`'s docstring (13 →
    14).
  - `_snapshot` adds `users.session_epoch` and the user's link as `(tenant_id, object_id,
    sign_in_name)` or `None` (column selects, like its neighbours).
  - Both sweeps take `asgi_app` and set `asgi_app.state.microsoft_sign_in = make_config()`. In the
    logged-out sweep, when `ep.microsoft_link`, give the actor a link before the snapshots. In the
    cross-user sweep, always give the victim a link with `sign_in_name="victim-ms@victim.example"`
    and add that string to the leak list (`:1939-1940`); when `ep.microsoft_link`, also give the
    attacker a link with a different `object_id`.
  - Cross-user sweep, control half: when `ep.impersonation == "refused"`, assert
    `r2.status_code == 403` and `control_after == control_before` (the 403 is the proof the cookie
    was honoured; an admin acting on themselves would have been redirected).
- [ ] **Step 3: Run** — Expected: FAIL.
- [ ] **Step 4: Implement.** `src/routers/settings.py`: `_microsoft_section(request, db, user)`
  (port; deltas: no role check; hidden also when `impersonating(request, account_of(user))`, M7/M8)
  passed as `microsoft=`; `flash_message`/`flash_type` from
  `message_for(request.query_params.get("microsoft"))`; `microsoft_unlink` (port; deltas: nothing
  linked → `/settings?microsoft=nothing_linked`; success → clear the session and redirect to
  `/login?error=microsoft_unlinked`). Module-level `_DB`/`_USER` singletons for the new route.
  `templates/settings.html`: blackbird's section, placed after the Account section, wording "Use your
  organisation's Microsoft account as a second way to sign in to CoPI. ORCID keeps working."; dates
  as `<span data-utc="{{ d.isoformat() }}" data-utc-fmt="short">{{ d.strftime('%b %d, %Y') }}</span>`;
  the unlink form `onsubmit="return confirm('Remove Microsoft sign-in? You will be signed out
  everywhere.')"`; literal Tailwind tokens.
- [ ] **Step 5: Run** the new file and `tests/integration/test_onboarding_flow.py` — Expected: PASS.
- [ ] **Step 6: Commit** — `feat(settings): link and unlink Microsoft sign-in`.

---

### Task 8: Admin view and removal

**Files:**
- Modify: `src/routers/admin.py`, `templates/admin/user_detail.html`
- Create: `tests/integration/test_microsoft_admin.py`

**Interfaces:**
- Consumes: Task 2 (`asgi_app`, `tests.session_support`), Task 3 (`refuse_impersonation`,
  `bump_session_epoch`), Task 4 (`link_view`, `unlink`, `make_config`).
- Produces: `POST /admin/users/{user_id}/microsoft/unlink`; template variable `microsoft_link`.

- [ ] **Step 1: Tests** — port blackbird's `tests/integration/test_microsoft_admin.py` with deltas:
  admins are `is_admin=True`; the non-admin refusal is a plain user (403); no audit row — assert the
  log line with `caplog` and that nothing is logged when no link exists; drop the workspace-PI test.
  Keep: removal deletes the row and ends every session of the target; impersonation refused (403);
  feature off → 404; unknown user → 404; the admin page shows the link (name, tenant, dates) only when
  on. Add `test_an_admin_removing_their_own_link_is_signed_out_everywhere`.
- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement.** In `admin_user_detail`, pass `microsoft_link=await link_view(db,
  user_id)` when `request.app.state.microsoft_sign_in` is set, else `None`. Add the POST route (port
  of blackbird's `admin_remove_microsoft_link`; deltas: no `AdminAuditEvent` — log `"Admin %s removed
  the Microsoft sign-in of user %s (tid=%s)"`; parameters use `_DB`/`_ADMIN` module singletons).
  `templates/admin/user_detail.html`: an inline block with `id="microsoft-sign-in"` after the
  Account `<dl>` and before Research Profile, from blackbird's `templates/admin/_microsoft_link.html`
  as a `<dl>` (no `<table>`: `tests/unit/test_no_raw_tables.py`), dates with `strftime` as the page
  already does (`:34-36`), and the form `onsubmit="return confirm('Remove this Microsoft sign-in?
  The user will be signed out everywhere.')"` (no user name in the script).
- [ ] **Step 4: Run** the new file and `tests/integration/test_admin_users.py
  tests/integration/test_admin_user_cohorts.py` — Expected: PASS.
- [ ] **Step 5: Commit** — `feat(admin): view and remove a user's Microsoft sign-in`.

---

### Task 9: Documentation and key hygiene

**Files:**
- Modify: `specs/auth-and-user-management.md`, `specs/admin-dashboard.md`, `specs/data-model.md`,
  `specs/tech-stack.md`, `AGENT.md`, `README.md`, `CLAUDE.md`
- Modify: `.gitignore`, `.dockerignore`, `tests/unit/test_dockerignore.py`

- [ ] **Step 1: Write.**
  - `specs/auth-and-user-management.md`: replace "CoPI uses ORCID OAuth exclusively… ORCID cannot be
    unlinked" (≈:41) with ORCID as the only way to create an account plus Microsoft as a linked
    second login; a "Sessions" paragraph (epoch; logout, unlink and admin removal end every session;
    fresh session at sign-in; `next` bounded to 1024 printable ASCII characters other than `"`); a "Microsoft sign-in"
    section: flows (§5.4), settings (§5.2), the open admin-exposure decision (§2.1), and the runbook
    (§7.2–§7.3) verbatim.
  - `specs/admin-dashboard.md`: the user-detail Microsoft block and the new route in the route table
    (≈:210-232).
  - `specs/data-model.md`: `users.session_epoch` under `### User`; a `### UserMicrosoftIdentity`
    section; account deletion removes it by cascade (≈:359-363).
  - `specs/tech-stack.md:24`: "Auth: ORCID OAuth 2.0 and Microsoft Entra ID (OIDC) via Authlib and
    joserfc".
  - `AGENT.md`: in the session decision (`:98-101`), replace "Session data is minimal (user_id
    only)" (`:100`) with what the session holds now (user id, `epoch` checked against
    `users.session_epoch`, sign-in stamps) and add a dated follow-up line for the epoch; `:38` and
    `:51` mention Microsoft sign-in. Keep `POST /admin/impersonate` (pinned by
    `tests/unit/test_agent_md_facts.py`).
  - `README.md:130`: add `microsoft_auth` to the router list.
  - `CLAUDE.md`: a short "Sign-in" section: Microsoft sign-in is a linked second login keyed by
    `(tid, oid)`, never email; off until `MICROSOFT_CLIENT_ID` is set; `.env` changes need `app`
    recreated; rolling back the code or turning the feature off does not end sessions unless
    `SECRET_KEY` is rotated; runbook in `specs/auth-and-user-management.md`.
  - `.gitignore`: `*.pem` and `*.key`; `.dockerignore`: `**/*.pem` and `**/*.key`;
    `tests/unit/test_dockerignore.py`: add `"key.pem"` and `"certs/signin.key"` to `MUST_EXCLUDE`.
- [ ] **Step 2: Check** — `rg -n "exclusively|cannot be unlinked|user_id only" specs/ AGENT.md
  README.md` finds no stale claim; `.venv-test/bin/python -m pytest tests/unit/test_agent_md_facts.py
  tests/unit/test_dockerignore.py -q` passes.
- [ ] **Step 3: Commit** — `docs(auth): Microsoft sign-in, session revocation, runbook; ignore key files`.

---

### Task 10: Integrated verification

- [ ] **Step 1: Stylesheet** — `scripts/build-css.sh` (templates gained classes).
- [ ] **Step 2: Gate** — `./scripts/ci.sh`. Expected: exit status as Baseline (the one pre-existing
  failure only); `src/` ruff ≤ 247, mypy ≤ 139, coverage not below 81.48 %; alembic round trip
  clean; lockfile consistent.
- [ ] **Step 3: Static checks of the constraints** —
  `rg -n "user_role|USER_ROLE|MICROSOFT_SIGN_IN_ROLES|IMPERSONATE_KEY|src\.web|flash\(|\| ts\(|data-confirm" src templates tests`
  finds nothing new; `rg -n '"/auth/microsoft/callback"' src` finds only the route decorator;
  `rg -n -i "email" src/routers/microsoft_auth.py src/services/microsoft_identity.py` finds only
  comments that say email is never used.
- [ ] **Step 4: Manual smoke without Microsoft** — run the app locally with
  `ALLOW_HTTP_SESSIONS=true` and no `MICROSOFT_*`: `/login` shows no Microsoft button,
  `/auth/microsoft/start` is 404, logout works; with a test config on `app.state` (or a dev
  registration), `/settings` renders the section for a linked and an unlinked user.
- [ ] **Step 5: Adversarial audit** — `lathe:plan-auditor` with this plan and the full diff,
  `lathe:semantic-reviewer` and `lathe:security-reviewer` with the diff (auth is a trust boundary);
  fix every confirmed finding and rerun Step 2.
- [ ] **Step 6: Report** — what changed, the gate result with its counts, the §9 live checks still
  open, the open owner decision (§2.1), and that `agent` should be rebuilt at its next planned
  restart (it bakes `src/models/user.py`).

---

## Baseline

Measured by the planning session on 2026-10-21 against `c08e33e9`, with Task 0's `.venv-test`
(authlib 1.8.0, joserfc 1.7.5, cryptography 50.0.1, starlette 1.6.0) and the stylesheet built:

- alembic: single head `0030`; round trip (upgrade head → downgrade 0018 → upgrade head) clean.
- ruff: tests clean; `src/` 247 findings (ceiling 260).
- lockfile: 16 direct dependencies, every pin satisfies its specifier.
- mypy: 139 findings (ceiling 150).
- pytest: 3230 passed, 276 skipped, 1 failed; coverage 81.48 % (floor 60). The failure is
  pre-existing and host-specific: `tests/unit/test_ci_gate.py::test_the_printed_gated_tier_counts_are_the_real_ones`
  — the nested gate's volume-reclaim step reports `comm: file 1 is not in sorted order` (spec §10).
- Without an in-tree `.venv-test`, five more gate self-tests fail (they ignore `VENV_PY`); without
  `scripts/build-css.sh`, 156 `tests/responsive` tests error.

## Changes made during execution (post-implementation audit)

The integrated implementation was audited by plan, semantic and security review. These changes
supersede the task text above where they differ; the spec (revision 3) carries the same rules.

- `complete_sign_in` writes the epoch read when the user was looked up, and re-reads it after the
  commit only for a user created in this request, so a revocation that commits during a sign-in
  leaves the new session stale (Task 5).
- `record_sign_in(db, user_id, identity)` stamps only the exact `(tid, oid)` link and returns whether
  it still existed; the Microsoft sign-in is refused as `microsoft_not_linked` when it did not
  (Tasks 4, 6).
- The user's own unlink bumps the epoch unconditionally, like the admin's removal, so a session
  issued between the unlink's epoch check and its commit cannot survive it (Task 7).
- `link_account` returns `link_refused` when the insert fails for a reason other than the two
  unique constraints (the user was deleted meanwhile) (Task 4).
- `is_safe_next_url` also refuses targets starting with `//` (Task 5).
- The user's unlink and every link-callback refusal are logged with the user id and `tid` (Tasks 6, 7).
