# Microsoft Sign-in for Staff Implementation Plan

> **For agentic workers:** execute with `/lathe:plan-execution` (this user's policy; it replaces
> superpowers' subagent-driven-development and executing-plans and keeps this format). Tasks 1-10
> own disjoint files and can be implemented in parallel without building or testing; Task 11
> verifies the merged result. Steps use checkbox (`- [ ]`) syntax for tracking. Under
> plan-execution the per-task "run" and "commit" steps are deferred to Task 11.

**Goal:** Admins, managers and reviewers can link a work/school Microsoft account to their existing
ORCID account and use it as a second way to sign in.

**Architecture:** A new `user_microsoft_identities` table keyed by Microsoft's `(tid, oid)`. A
generic ID-token helper (`src/services/id_tokens.py`) backs two validators: Microsoft's
(`src/services/microsoft_identity.py`) and the ORCID re-sign-in (`src/services/orcid_reauth.py`).
A new router (`src/routers/microsoft_auth.py`) runs the auth-code + PKCE flow against the
`organizations` endpoints with a certificate client assertion; both providers' callbacks end in
one shared `complete_sign_in`. The feature is switched by `app.state.microsoft_sign_in`.

**Tech Stack:** FastAPI, Starlette sessions, SQLAlchemy 2 async, Alembic, authlib 1.7.2,
joserfc 1.7.4, cryptography, httpx `MockTransport` in tests, pytest + testcontainers Postgres.

**Spec:** `docs/specs/2026-10-13-microsoft-staff-sign-in-design.md` (approved 2026-10-13). Read it
first; this plan argues from it.

## Global Constraints

- Python 3.11 compatible (image `python:3.11-slim`); ruff `line-length = 100`, rules `E F I UP B`.
- Dependencies: `authlib>=1.7.2`, `joserfc>=1.7.4`, `cryptography>=45.0.1`. No other new package.
- Roles: `MICROSOFT_SIGN_IN_ROLES = (admin, manager, reviewer)`. Never `User.is_staff` (excludes reviewers).
- Lookups by `(tenant_id, object_id)` only. Never request, store or match the Microsoft `email`
  claim; never write `users.email` or `users.name` from Microsoft.
- Endpoints: `https://login.microsoftonline.com/organizations/oauth2/v2.0/{authorize,token}`;
  keys per allowed tenant at `https://login.microsoftonline.com/{tid}/discovery/v2.0/keys`.
  `scope=openid profile`, `response_mode=query`, PKCE S256, `nonce`, `prompt=select_account` for linking.
- Client assertion: `alg=PS256`, header `x5t#S256` (base64url SHA-256 of the cert DER), claims
  `iss=sub=client_id`, `aud`=token endpoint, `jti` uuid4 hex, `iat=nbf=now`, `exp=now+300`, a
  new `PrivateKeyJWT` per request, private key passed as `client_secret`, `client_id` in the body.
- ID token: RS256 pinned; `tid` allowed before any key fetch; `iss == https://login.microsoftonline.com/{tid}/v2.0`
  and equals the key's `issuer`; `aud == client_id`; 300 s leeway; nonce; `oid` GUID; `acct` is `0` or `"0"` (not `False`).
- ORCID reauth: `scope=openid`, `prompt=login`, `nonce`; ID token from `https://orcid.org` with keys at
  `https://orcid.org/oauth/jwks`; `sub` = the signed-in user's ORCID; `auth_time` ≤ 300 s old.
- Linking needs `signed_in_via == "orcid"` and `signed_in_at` ≤ 15 minutes old, at link start AND callback.
- User unlink ends every session (current included); admin removal ends every session of the target.
- `src/` lint ratchet: no `Depends(...)` in an argument default; use module-level singletons (`_DB = Depends(get_db)`).
- Never write a string literal equal to the Microsoft callback path in `src/` outside its route
  decorator (`tests/unit/test_reachability.py` treats a src string naming a route as a caller and
  would flag its allowlist entry stale).
- Tests never call Microsoft or ORCID, and never depend on the host `.env` for the feature switch:
  set `asgi_app.state.microsoft_sign_in`.
- Run tests on the host only (the checkout is an sshfs mount): 
  `ssh -o StrictHostKeyChecking=yes ubuntu@ec2-3-21-33-147.us-east-2.compute.amazonaws.com 'cd /home/ubuntu/blackbird-copi-science && .venv-test/bin/python -m pytest <paths> -q'`.
  If ssh reports a changed host key, STOP and ask the owner; never accept it. Never `pip install`
  into `.venv-test` from the client; refresh it on the host with
  `uv pip install --python .venv-test/bin/python -e '.[dev]'`.
- Never `git checkout`/`stash`/`restore` or commit `docker-compose.prod.yml`; do not commit
  `CLAUDE.md` or `docs/audits/open-findings.md` (both carry unrelated uncommitted edits). Work on
  branch `feat/microsoft-staff-sign-in` off `blackbird`. Do not push, deploy, or start the simulation.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. A callback replayed (browser back, double click) after its flow was consumed → "Security check
   failed", never a 500. Test in Task 7 (`test_a_replayed_callback_is_refused`).
2. A token response with no `id_token` → the generic failure message, never a 500. Test in Task 7
   (`test_a_token_response_without_an_id_token_fails_cleanly`).
3. Microsoft's key endpoint down: warm cache keeps signing people in for up to 24 h; cold cache
   gives the generic failure. Tests in Task 3 (`test_a_failed_refresh_serves_cached_keys_within_the_ttl`)
   and Task 7 (`test_a_key_outage_with_a_cold_cache_fails_cleanly`).
4. A session issued before this deploy (no `signed_in_at`) on Settings → "Sign in with ORCID again
   to link", and `/auth/microsoft/link` refuses it. Test in Task 8 (`test_a_pre_deploy_session_must_sign_in_again`).
5. An upper-case tenant GUID in `MICROSOFT_ALLOWED_TENANT_IDS` still matches Microsoft's lower-case
   `tid`; a missing or over-long `preferred_username` is stored as `None` / cut to 255. Tests in
   Task 4 (`test_tenant_ids_are_normalised`, `test_the_sign_in_name_is_optional_and_bounded`).

---

### Task 1: Settings fields and dependencies

**Files:**
- Modify: `pyproject.toml` (dependencies list)
- Modify: `src/config.py` (after the `# ORCID OAuth` block, ~line 112)
- Modify: `.env.example` (after the ORCID block)
- Test: `tests/unit/test_config_secret_redaction.py` (`NON_SECRET_STR_FIELDS`, ~line 137)

**Interfaces:**
- Produces: `Settings.microsoft_client_id`, `.microsoft_redirect_uri`, `.microsoft_allowed_tenant_ids`,
  `.microsoft_certificate_b64`, `.microsoft_private_key_b64` — all `str`, default `""`.

- [ ] **Step 1: Write the failing tests** — in `tests/unit/test_config_secret_redaction.py` add to
  `NON_SECRET_STR_FIELDS` (the set literal):

```python
    # Microsoft sign-in (spec 2026-10-13 §5.2): a public client id, a redirect URL, tenant GUIDs
    # and the PUBLIC certificate. The private key (microsoft_private_key_b64) is masked by name.
    "microsoft_client_id",
    "microsoft_redirect_uri",
    "microsoft_allowed_tenant_ids",
    "microsoft_certificate_b64",
```

and append this test at the end of the file:

```python
def test_the_microsoft_private_key_is_masked():
    s = Settings(_env_file=None, microsoft_private_key_b64="sentinel-private-key")
    assert "sentinel-private-key" not in repr(s) + str(s)
    assert s.microsoft_private_key_b64 == "sentinel-private-key"
```

- [ ] **Step 2: Run to verify it fails**

Run (host): `.venv-test/bin/python -m pytest tests/unit/test_config_secret_redaction.py -q`
Expected: FAIL (`Settings` has no `microsoft_*` fields; the set comparison fails).

- [ ] **Step 3: Implement** — in `src/config.py`, directly after `orcid_redirect_uri`:

```python

    # Microsoft sign-in for staff (docs/specs/2026-10-13-microsoft-staff-sign-in-design.md
    # §5.2). Plain strings: src/services/microsoft_identity.py::load_config validates them in
    # create_app() only, so a bad value stops the web app but never the worker or the agent,
    # which load the same .env. An empty MICROSOFT_CLIENT_ID keeps the feature off.
    microsoft_client_id: str = ""
    microsoft_redirect_uri: str = ""
    # Comma-separated tenant GUIDs. A str, not a list: pydantic-settings JSON-decodes list
    # fields from the environment, and "a,b" would stop every process.
    microsoft_allowed_tenant_ids: str = ""
    # Base64 of the PEM certificate uploaded to the app registration (public).
    microsoft_certificate_b64: str = ""
    # Base64 of the PEM RSA private key. Masked in repr by its name ("key").
    microsoft_private_key_b64: str = ""
```

In `pyproject.toml` `dependencies`, change `"authlib>=1.3.0",` to `"authlib>=1.7.2",` and add after it:

```toml
    "joserfc>=1.7.4",
    "cryptography>=45.0.1",
```

In `.env.example`, after the ORCID block:

```
# Microsoft sign-in for staff (docs/specs/2026-10-13-microsoft-staff-sign-in-design.md).
# Leave MICROSOFT_CLIENT_ID empty to keep it off. Turn-on runbook: docs/operations/pis-and-access.md.
MICROSOFT_CLIENT_ID=
MICROSOFT_REDIRECT_URI=
MICROSOFT_ALLOWED_TENANT_IDS=
MICROSOFT_CERTIFICATE_B64=
MICROSOFT_PRIVATE_KEY_B64=
```

- [ ] **Step 4: Run to verify it passes** — same command. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/config.py .env.example tests/unit/test_config_secret_redaction.py
git commit -m "feat(microsoft): settings and dependency floors for staff Microsoft sign-in"
```

---

### Task 2: `user_microsoft_identities` model and migration 0066

**Files:**
- Create: `src/models/microsoft_identity.py`
- Modify: `src/models/__init__.py` (import + `__all__`)
- Create: `alembic/versions/0066_user_microsoft_identities.py`
- Modify: `scripts/migrate/preflight.py` (`DEFAULT_TARGET` ~78, `SUPPORTED_START_REVISIONS` ~119 and its comment, `PLANNED_OBJECTS` end ~558, `REVISION_ORDER` ~577)
- Modify: `docs/operations/migration-deploy-notes.md` (append a box)
- Modify: `tests/unit/test_migration_checks.py:276-283`, `tests/integration/test_harness_smoke.py:~99`
- Test: `tests/integration/test_migration_0066.py`

**Interfaces:**
- Produces: `src.models.UserMicrosoftIdentity` with columns `id, user_id, tenant_id (UUID),
  object_id (UUID), sign_in_name (str|None), linked_at, last_sign_in_at`; constraints
  `uq_user_microsoft_identities_user_id`, `uq_user_microsoft_identities_tenant_object`.

- [ ] **Step 1: Write the failing test** `tests/integration/test_migration_0066.py`:

```python
"""Migration 0066 (spec 2026-10-13 §5.1): user_microsoft_identities."""
import uuid

import pytest
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from src.models import User, UserMicrosoftIdentity
from tests import factories

pytestmark = pytest.mark.integration
TENANT = uuid.UUID("11111111-2222-3333-4444-555555555555")


async def _link(db, user, oid=None, tenant=TENANT):
    row = UserMicrosoftIdentity(user_id=user.id, tenant_id=tenant, object_id=oid or uuid.uuid4())
    db.add(row)
    await db.flush()
    return row


async def _count(db) -> int:
    return await db.scalar(select(func.count()).select_from(UserMicrosoftIdentity))


async def test_a_link_is_stamped_on_insert(db_session):
    row = await _link(db_session, await factories.make_user(db_session))
    assert row.linked_at is not None
    assert row.last_sign_in_at is None
    assert row.sign_in_name is None


async def test_one_microsoft_account_links_to_one_user(db_session):
    a, b = await factories.make_user(db_session), await factories.make_user(db_session)
    oid = uuid.uuid4()
    await _link(db_session, a, oid)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await _link(db_session, b, oid)


async def test_one_link_per_user(db_session):
    user = await factories.make_user(db_session)
    await _link(db_session, user)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await _link(db_session, user)


async def test_the_same_oid_in_another_tenant_is_another_account(db_session):
    a, b = await factories.make_user(db_session), await factories.make_user(db_session)
    oid = uuid.uuid4()
    await _link(db_session, a, oid)
    await _link(db_session, b, oid, tenant=uuid.uuid4())
    assert await _count(db_session) == 2


async def test_deleting_the_user_deletes_the_link_in_the_database(db_session):
    user = await factories.make_user(db_session)
    await _link(db_session, user)
    await db_session.execute(sa_delete(User).where(User.id == user.id))
    assert await _count(db_session) == 0


async def test_delete_user_account_removes_the_link_without_loading_it(db_session):
    from src.services.user_deletion import delete_user_account

    user = await factories.make_user(db_session)
    row = await _link(db_session, user)
    db_session.expunge(row)  # the link is never loaded on the deletion path
    await delete_user_account(db_session, user)
    assert await _count(db_session) == 0
```

- [ ] **Step 2: Run to verify it fails** — host: `.venv-test/bin/python -m pytest tests/integration/test_migration_0066.py -q`.
  Expected: FAIL (`ImportError: cannot import name 'UserMicrosoftIdentity'`).

- [ ] **Step 3: Implement the model** `src/models/microsoft_identity.py`:

```python
"""A staff account's linked Microsoft (Entra ID) sign-in (migration 0066; spec
docs/specs/2026-10-13-microsoft-staff-sign-in-design.md §5.1).

Looked up by (tenant_id, object_id) only: Microsoft's `tid` and `oid`, the immutable pair it
says to key application data on. `sign_in_name` (`preferred_username`) is display-only. No
relationship is declared on User: deleting the user removes the row through the database's
ON DELETE CASCADE, so delete_user_account never has to load it.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class UserMicrosoftIdentity(Base):
    __tablename__ = "user_microsoft_identities"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_user_microsoft_identities_user_id"),
        UniqueConstraint(
            "tenant_id", "object_id", name="uq_user_microsoft_identities_tenant_object"
        ),
    )
    #: The INSERT returns `linked_at` itself: an expired server default would lazy-load on
    #: first read, which an async session cannot do.
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    object_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    sign_in_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_sign_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

In `src/models/__init__.py` add `from src.models.microsoft_identity import UserMicrosoftIdentity`
(alphabetical among the `from src.models.…` imports) and `"UserMicrosoftIdentity",` to `__all__`
right after `"User",`.

- [ ] **Step 4: Implement the migration** `alembic/versions/0066_user_microsoft_identities.py`:

```python
"""user_microsoft_identities: a staff account's linked Microsoft sign-in

Design: docs/specs/2026-10-13-microsoft-staff-sign-in-design.md §5.1.

One row per linked account: Microsoft's immutable (tid, oid) pair, unique, and the user it signs
in to, unique (one link per user). ON DELETE CASCADE removes it with the user.

OLD CODE ON THE NEW SCHEMA is safe: nothing old reads the table. NEW CODE ON THE OLD SCHEMA is
not: the Settings page and every Microsoft route query it, so migrate before the new web image
serves. Downgrade drops the table and every link in it.

Revision ID: 0066
Revises: 0065
Create Date: 2026-10-13
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0066"
down_revision: str | None = "0065"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_microsoft_identities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sign_in_name", sa.String(255), nullable=True),
        sa.Column(
            "linked_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("last_sign_in_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", name="uq_user_microsoft_identities_user_id"),
        sa.UniqueConstraint(
            "tenant_id", "object_id", name="uq_user_microsoft_identities_tenant_object"
        ),
    )


def downgrade() -> None:
    op.drop_table("user_microsoft_identities")
```

- [ ] **Step 5: Move the preflight head** in `scripts/migrate/preflight.py`:
  - `DEFAULT_TARGET = "0066"`.
  - `SUPPORTED_START_REVISIONS`: append `"0065"` after `"0064"`; in the comment above it, change
    "and 0065's index" to "0065's index, and 0066's table".
  - `REVISION_ORDER`: append `"0066"` after `"0065"`.
  - `PLANNED_OBJECTS`: before its closing `)` add:

```python
    # 0066_user_microsoft_identities (spec 2026-10-13 §5.1).
    PlannedObject("0066", "table", "user_microsoft_identities"),
    PlannedObject(
        "0066", "constraint", "uq_user_microsoft_identities_user_id", "user_microsoft_identities"
    ),
    PlannedObject(
        "0066", "constraint", "uq_user_microsoft_identities_tenant_object",
        "user_microsoft_identities",
    ),
```

  In `tests/unit/test_migration_checks.py::test_supported_start_revisions_are_exactly_the_documented_set`
  append `"0065"` to the expected tuple and change `assert pf.DEFAULT_TARGET == "0065"` to `"0066"`.
  In `tests/integration/test_harness_smoke.py` add the comment line
  `# 0066 user_microsoft_identities (Microsoft sign-in for staff)` under the `0065` line and change
  `assert v == "0065"` to `assert v == "0066"`.

- [ ] **Step 6: Append the deploy box** to `docs/operations/migration-deploy-notes.md`:

```markdown

> **Deploy order for `0066_user_microsoft_identities` — migrate BEFORE the new web image serves;
> no agent rebuild needed; no backfill.** `0066` creates `user_microsoft_identities`: one row per
> staff account that linked a Microsoft sign-in, unique on `(tenant_id, object_id)` and on
> `user_id`, `ON DELETE CASCADE` from `users`. Design:
> `docs/specs/2026-10-13-microsoft-staff-sign-in-design.md` §5.1.
>
> *Old code on the new schema* is safe: nothing old reads the table. *New code on the old schema*
> is not: `/settings` and every Microsoft route query it and raise `UndefinedTable`. **Agent
> image:** no rebuild needed — the engine imports `src.models` but never queries this table. The
> `CREATE TABLE` takes no lock on an existing table. **No backfill:** an empty table means nobody
> has linked yet. The feature stays off until `MICROSOFT_CLIENT_ID` is set (runbook in
> `docs/operations/pis-and-access.md`). Commit before building.
>
> Rollback: `alembic downgrade 0065` drops the table and every link; staff sign in with ORCID.
```

- [ ] **Step 7: Run to verify it passes** — host:
  `.venv-test/bin/python -m pytest tests/integration/test_migration_0066.py tests/unit/test_migration_checks.py tests/integration/test_harness_smoke.py tests/integration/test_model_migration_parity.py tests/unit/test_claude_md_references.py -q`.
  Expected: PASS (the parity test needs no edit; it does not compare `ondelete`).

- [ ] **Step 8: Commit**

```bash
git add src/models/microsoft_identity.py src/models/__init__.py alembic/versions/0066_user_microsoft_identities.py scripts/migrate/preflight.py docs/operations/migration-deploy-notes.md tests/unit/test_migration_checks.py tests/integration/test_harness_smoke.py tests/integration/test_migration_0066.py
git commit -m "feat(microsoft): user_microsoft_identities table (migration 0066)"
```

---

### Task 3: ID-token core and the shared test identity provider

**Files:**
- Create: `src/services/id_tokens.py`
- Create: `tests/oidc_fakes.py`
- Test: `tests/unit/test_id_tokens.py`

**Interfaces:**
- Produces (`src.services.id_tokens`): `ALGORITHM = "RS256"`, `LEEWAY_SECONDS = 300`,
  `KEY_REFRESH_SECONDS = 3600`, `KEY_TTL_SECONDS = 86400`, `UNKNOWN_KID_REFETCH_SECONDS = 300`,
  `HTTP_TIMEOUT_SECONDS = 10.0`, `HTTP_TRANSPORT: httpx.AsyncBaseTransport | None = None`
  (read at call time; tests replace it), `class IdTokenError(Exception)` with `.check: str`,
  `unverified_parts(token: str) -> tuple[dict, dict]`, `verify_signature(token: str, jwk: dict) -> dict`,
  `check_audience(claims: dict, client_id: str) -> None`, `check_times(claims: dict, now: int) -> None`,
  `class KeySetCache` with `async key_for(url: str, kid: str | None, *, now: float) -> dict`.
- Produces (`tests.oidc_fakes`): constants `TENANT, OTHER_TENANT, CLIENT_ID, REDIRECT_URI, OID, KID,
  MICROSOFT_HOST, ORCID_JWKS_URL, DROP`, `SIGNING_PEM, OTHER_KEY_PEM, CERT_PEM, CERT_KEY_PEM`,
  `public_jwk(kid=KID, *, key_pem=SIGNING_PEM, **extra) -> dict`,
  `sign(claims, *, kid=KID, alg="RS256", key_pem=SIGNING_PEM) -> str`,
  `forged(header: dict, claims: dict) -> str`, `make_certificate(*, key_size=2048, days=365) -> tuple[bytes, bytes]`,
  `b64(data: bytes) -> str`, `settings_for(**overrides) -> SimpleNamespace`, `make_config(**overrides)`,
  `microsoft_claims(*, nonce, now=None, tenant=TENANT, **overrides) -> dict`,
  `class FakeIdP` (`token_response`, `token_requests`, `token_form(i)`, `key_fetches`,
  `tenant_keys`, `orcid_keys`, `keys_status`), fixture `fake_idp`.

- [ ] **Step 1: Write the shared fake** `tests/oidc_fakes.py`:

```python
"""Test doubles for the OpenID Connect providers (spec 2026-10-13 §8: no test talks to Microsoft
or ORCID): a signing key, ID tokens signed with it, a self-signed client certificate, and one
httpx.MockTransport answering as Microsoft's token endpoint and per-tenant key sets and as
ORCID's key set. Import the `fake_idp` fixture into a test module to use it."""
from __future__ import annotations

import base64
import datetime
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from joserfc import jwt
from joserfc.jwk import RSAKey

TENANT = "11111111-2222-3333-4444-555555555555"
OTHER_TENANT = "99999999-8888-7777-6666-555555555555"
CLIENT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
REDIRECT_URI = "https://blackbird.example/auth/microsoft/callback"
OID = "0f0f0f0f-1111-2222-3333-444444444444"
KID = "test-key-1"
MICROSOFT_HOST = "login.microsoftonline.com"
ORCID_JWKS_URL = "https://orcid.org/oauth/jwks"
#: Pass as a claim value to `microsoft_claims` to leave that claim out.
DROP = object()


def _pem(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


SIGNING_PEM = _pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))
OTHER_KEY_PEM = _pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))


def make_certificate(*, key_size: int = 2048, days: int = 365) -> tuple[bytes, bytes]:
    """(certificate PEM, private key PEM) of a fresh self-signed certificate."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "blackbird-test")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=days))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM), _pem(key)


CERT_PEM, CERT_KEY_PEM = make_certificate()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def public_jwk(kid: str = KID, *, key_pem: bytes = SIGNING_PEM, **extra) -> dict:
    return {**RSAKey.import_key(key_pem).as_dict(private=False), "kid": kid, "use": "sig", **extra}


def sign(claims: dict, *, kid: str | None = KID, alg: str = "RS256",
         key_pem: bytes = SIGNING_PEM) -> str:
    header = {"alg": alg, "typ": "JWT"}
    if kid is not None:
        header["kid"] = kid
    return jwt.encode(header, claims, RSAKey.import_key(key_pem), algorithms=[alg])


def forged(header: dict, claims: dict) -> str:
    """A compact JWS with any header (e.g. alg=none) and a junk signature."""
    def enc(part: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(part).encode()).rstrip(b"=").decode()
    return f"{enc(header)}.{enc(claims)}.c2lnbmF0dXJl"


def microsoft_claims(*, nonce: str, now: int | None = None, tenant: str = TENANT,
                     **overrides) -> dict:
    now = int(time.time()) if now is None else now
    claims = {
        "iss": f"https://{MICROSOFT_HOST}/{tenant}/v2.0", "aud": CLIENT_ID, "tid": tenant,
        "oid": OID, "sub": "pairwise-subject", "nonce": nonce, "iat": now, "nbf": now,
        "exp": now + 3600, "acct": 0, "preferred_username": "staff@example.edu", "ver": "2.0",
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not DROP}


def settings_for(**overrides) -> SimpleNamespace:
    values = dict(
        microsoft_client_id=CLIENT_ID, microsoft_redirect_uri=REDIRECT_URI,
        microsoft_allowed_tenant_ids=TENANT, microsoft_certificate_b64=b64(CERT_PEM),
        microsoft_private_key_b64=b64(CERT_KEY_PEM), allow_http_sessions=False,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def make_config(**overrides):
    from src.services.microsoft_identity import load_config

    return load_config(settings_for(**overrides))


class FakeIdP:
    """The handler behind one MockTransport for every provider URL the code calls."""

    def __init__(self) -> None:
        self.token_response: tuple[int, dict] = (500, {"error": "server_error"})
        self.token_requests: list[httpx.Request] = []
        self.key_fetches: list[str] = []
        self.keys_status = 200
        self.tenant_keys = {
            tid: [public_jwk(issuer=f"https://{MICROSOFT_HOST}/{tid}/v2.0")]
            for tid in (TENANT, OTHER_TENANT)
        }
        self.orcid_keys = [public_jwk()]

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == MICROSOFT_HOST and url.path == "/organizations/oauth2/v2.0/token":
            self.token_requests.append(request)
            status, body = self.token_response
            return httpx.Response(status, json=body)
        if url.host == MICROSOFT_HOST and url.path.endswith("/discovery/v2.0/keys"):
            self.key_fetches.append(str(url))
            if self.keys_status != 200:
                return httpx.Response(self.keys_status)
            return httpx.Response(200, json={"keys": self.tenant_keys.get(url.path.split("/")[1], [])})
        if str(url) == ORCID_JWKS_URL:
            self.key_fetches.append(str(url))
            if self.keys_status != 200:
                return httpx.Response(self.keys_status)
            return httpx.Response(200, json={"keys": self.orcid_keys})
        return httpx.Response(404)

    def token_form(self, index: int = -1) -> dict[str, str]:
        body = self.token_requests[index].content.decode()
        return {k: v[0] for k, v in parse_qs(body).items()}


@pytest.fixture
def fake_idp(monkeypatch):
    from src.services import id_tokens, microsoft_identity, orcid_reauth

    fake = FakeIdP()
    monkeypatch.setattr(id_tokens, "HTTP_TRANSPORT", httpx.MockTransport(fake.handler))
    monkeypatch.setattr(microsoft_identity, "KEYS", id_tokens.KeySetCache())
    monkeypatch.setattr(orcid_reauth, "KEYS", id_tokens.KeySetCache())
    return fake
```

- [ ] **Step 2: Write the failing tests** `tests/unit/test_id_tokens.py`:

```python
"""src/services/id_tokens.py (spec 2026-10-13 §5.5)."""
import time

import httpx
import pytest

from src.services import id_tokens
from src.services.id_tokens import (
    IdTokenError,
    KeySetCache,
    check_audience,
    check_times,
    unverified_parts,
    verify_signature,
)
from tests.oidc_fakes import KID, OTHER_KEY_PEM, forged, public_jwk, sign

URL = "https://keys.example/jwks"


class _Keys:
    def __init__(self):
        self.keys = [public_jwk()]
        self.status = 200
        self.calls = 0

    def handler(self, request):
        self.calls += 1
        if self.status != 200:
            return httpx.Response(self.status)
        return httpx.Response(200, json={"keys": self.keys})


@pytest.fixture
def keys(monkeypatch):
    k = _Keys()
    monkeypatch.setattr(id_tokens, "HTTP_TRANSPORT", httpx.MockTransport(k.handler))
    return k


def test_unverified_parts_reads_header_and_claims():
    header, claims = unverified_parts(sign({"a": 1}))
    assert header["alg"] == "RS256" and header["kid"] == KID and claims == {"a": 1}


@pytest.mark.parametrize("alg", ["none", "HS256", "PS256", None])
def test_only_rs256_is_accepted(alg):
    with pytest.raises(IdTokenError) as e:
        unverified_parts(forged({"alg": alg}, {"a": 1}))
    assert e.value.check == "alg"


@pytest.mark.parametrize("token", ["", "a.b", "!!.!!.!!", None, 5])
def test_malformed_tokens_are_refused(token):
    with pytest.raises(IdTokenError) as e:
        unverified_parts(token)
    assert e.value.check == "malformed"


def test_verify_signature_accepts_the_right_key_and_refuses_another():
    token = sign({"a": 1})
    assert verify_signature(token, public_jwk()) == {"a": 1}
    with pytest.raises(IdTokenError) as e:
        verify_signature(token, public_jwk(key_pem=OTHER_KEY_PEM))
    assert e.value.check == "signature"


def test_check_audience():
    check_audience({"aud": "cid"}, "cid")
    check_audience({"aud": ["cid"]}, "cid")
    for claims, client_id in (({"aud": "x"}, "cid"), ({"aud": ["cid", "x"]}, "cid"),
                              ({}, "cid"), ({"aud": ""}, "")):
        with pytest.raises(IdTokenError):
            check_audience(claims, client_id)


def test_check_times_allows_the_leeway_and_no_more():
    now = 1_000_000
    check_times({"exp": now - 299, "iat": now + 299, "nbf": now + 299}, now)
    for bad in ({"exp": now - 301, "iat": now}, {"exp": now + 60, "iat": now + 301},
                {"exp": now + 60, "iat": now, "nbf": now + 301}, {"iat": now},
                {"exp": now + 60}, {"exp": True, "iat": now}, {"exp": "1", "iat": now}):
        with pytest.raises(IdTokenError):
            check_times(bad, now)


async def test_keys_are_cached_within_the_refresh_window(keys):
    cache, now = KeySetCache(), time.time()
    assert (await cache.key_for(URL, KID, now=now))["kid"] == KID
    await cache.key_for(URL, KID, now=now + id_tokens.KEY_REFRESH_SECONDS - 1)
    assert keys.calls == 1
    await cache.key_for(URL, KID, now=now + id_tokens.KEY_REFRESH_SECONDS)
    assert keys.calls == 2


async def test_an_unknown_kid_refetches_at_most_once_per_five_minutes(keys):
    cache, now = KeySetCache(), time.time()
    await cache.key_for(URL, KID, now=now)
    for t in (now + 1, now + 2):
        with pytest.raises(IdTokenError) as e:
            await cache.key_for(URL, "rotated", now=t)
        assert e.value.check == "kid"
    assert keys.calls == 2  # one forced refetch, then throttled
    keys.keys = [public_jwk("rotated")]
    with pytest.raises(IdTokenError):
        await cache.key_for(URL, "rotated", now=now + 3)
    found = await cache.key_for(URL, "rotated", now=now + 2 + id_tokens.UNKNOWN_KID_REFETCH_SECONDS)
    assert found["kid"] == "rotated"


async def test_a_failed_refresh_serves_cached_keys_within_the_ttl(keys):
    cache, now = KeySetCache(), time.time()
    await cache.key_for(URL, KID, now=now)
    keys.status = 503
    later = now + id_tokens.KEY_REFRESH_SECONDS + 10
    assert (await cache.key_for(URL, KID, now=later))["kid"] == KID
    with pytest.raises(IdTokenError) as e:
        await cache.key_for(URL, KID, now=now + id_tokens.KEY_TTL_SECONDS + 1)
    assert e.value.check == "keys"


async def test_a_cold_cache_with_the_endpoint_down_fails_closed(keys):
    keys.status = 500
    with pytest.raises(IdTokenError) as e:
        await KeySetCache().key_for(URL, KID, now=time.time())
    assert e.value.check == "keys"


async def test_a_token_without_kid_uses_a_one_key_set_only(keys):
    cache, now = KeySetCache(), time.time()
    assert (await cache.key_for(URL, None, now=now))["kid"] == KID
    keys.keys = [public_jwk("a"), public_jwk("b")]
    with pytest.raises(IdTokenError):
        await KeySetCache().key_for(URL, None, now=now)
```

- [ ] **Step 3: Run to verify it fails** — host: `.venv-test/bin/python -m pytest tests/unit/test_id_tokens.py -q`.
  Expected: FAIL (`ModuleNotFoundError: src.services.id_tokens`).

- [ ] **Step 4: Implement** `src/services/id_tokens.py`:

```python
"""Signature, audience and time checks for OpenID Connect ID tokens, and a cache of the signing
keys they are checked against (spec docs/specs/2026-10-13-microsoft-staff-sign-in-design.md
§5.5, §5.5.1).

Shared by Microsoft sign-in (src/services/microsoft_identity.py) and the ORCID re-sign-in
(src/services/orcid_reauth.py); each checks its own issuer, nonce and extra claims. RS256 is
pinned here, never read from the token: neither provider's keys carry an `alg`.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from dataclasses import dataclass

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import RSAKey

logger = logging.getLogger(__name__)

ALGORITHM = "RS256"
LEEWAY_SECONDS = 300
#: Microsoft's key-rollover guidance: refresh hourly, keep keys for at most 24 hours.
KEY_REFRESH_SECONDS = 3600
KEY_TTL_SECONDS = 24 * 3600
UNKNOWN_KID_REFETCH_SECONDS = 300
HTTP_TIMEOUT_SECONDS = 10.0

#: Tests replace this with an httpx.MockTransport; None uses the network. Read at call time.
HTTP_TRANSPORT: httpx.AsyncBaseTransport | None = None


class IdTokenError(Exception):
    """A token or key check failed. `check` names it; the message is for logs, never users."""

    def __init__(self, check: str, detail: str = "") -> None:
        super().__init__(f"{check}: {detail}" if detail else check)
        self.check = check


def _segment(part: str) -> dict:
    try:
        value = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise IdTokenError("malformed", type(exc).__name__) from exc
    if not isinstance(value, dict):
        raise IdTokenError("malformed", "segment is not a JSON object")
    return value


def unverified_parts(token: object) -> tuple[dict, dict]:
    """(header, claims), checking nothing but the shape and `alg`. Only for choosing keys."""
    parts = token.split(".") if isinstance(token, str) else []
    if len(parts) != 3:
        raise IdTokenError("malformed", "not a compact JWS")
    header, claims = _segment(parts[0]), _segment(parts[1])
    if header.get("alg") != ALGORITHM:
        raise IdTokenError("alg", repr(header.get("alg"))[:40])
    return header, claims


def verify_signature(token: str, jwk: dict) -> dict:
    """The token's claims after an RS256 signature check against `jwk`."""
    try:
        return dict(jwt.decode(token, RSAKey.import_key(jwk), algorithms=[ALGORITHM]).claims)
    except (JoseError, ValueError, TypeError) as exc:
        raise IdTokenError("signature", type(exc).__name__) from exc


def check_audience(claims: dict, client_id: str) -> None:
    """`aud` is our client id (or a one-element list holding it). Fails closed on an empty id."""
    aud = claims.get("aud")
    if client_id and (aud == client_id or aud == [client_id]):
        return
    raise IdTokenError("aud", repr(aud)[:80])


def _int_claim(claims: dict, name: str, *, required: bool) -> int | None:
    value = claims.get(name)
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise IdTokenError(name, f"not an integer: {value!r}"[:80])
    return value


def check_times(claims: dict, now: int) -> None:
    """`exp` and `iat` required, `nbf` optional, each with LEEWAY_SECONDS of clock skew."""
    exp = _int_claim(claims, "exp", required=True)
    iat = _int_claim(claims, "iat", required=True)
    nbf = _int_claim(claims, "nbf", required=False)
    if exp < now - LEEWAY_SECONDS:
        raise IdTokenError("exp", "expired")
    if iat > now + LEEWAY_SECONDS:
        raise IdTokenError("iat", "issued in the future")
    if nbf is not None and nbf > now + LEEWAY_SECONDS:
        raise IdTokenError("nbf", "not yet valid")


async def _fetch_keys(url: str) -> dict[str, dict]:
    try:
        async with httpx.AsyncClient(transport=HTTP_TRANSPORT, timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(url)
            response.raise_for_status()
            body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise IdTokenError("keys", f"{type(exc).__name__} fetching {url}") from exc
    keys = body.get("keys") if isinstance(body, dict) else None
    if not isinstance(keys, list):
        raise IdTokenError("keys", f"no key list at {url}")
    return {
        k["kid"]: k for k in keys
        if isinstance(k, dict) and isinstance(k.get("kid"), str) and k.get("kty") == "RSA"
    }


@dataclass
class _Entry:
    keys: dict[str, dict]
    fetched_at: float
    last_forced: float


class KeySetCache:
    """Signing keys per key-set URL: refreshed every KEY_REFRESH_SECONDS, served from cache for
    up to KEY_TTL_SECONDS when a refresh fails, refetched on an unknown `kid` at most once per
    UNKNOWN_KID_REFETCH_SECONDS. In-process and best-effort: concurrent misses may both fetch."""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    async def key_for(self, url: str, kid: str | None, *, now: float) -> dict:
        entry = self._entries.get(url)
        if entry is None or now - entry.fetched_at >= KEY_REFRESH_SECONDS:
            entry = await self._refresh(url, now, entry, forced=False)
        jwk = self._pick(entry, kid)
        if (jwk is None and entry.fetched_at < now
                and now - entry.last_forced >= UNKNOWN_KID_REFETCH_SECONDS):
            entry = await self._refresh(url, now, entry, forced=True)
            jwk = self._pick(entry, kid)
        if jwk is None:
            raise IdTokenError("kid", f"no key {kid!r} at {url}")
        return jwk

    @staticmethod
    def _pick(entry: _Entry, kid: str | None) -> dict | None:
        if isinstance(kid, str) and kid:
            return entry.keys.get(kid)
        # A token without `kid` is accepted only against a one-key set (ORCID publishes one).
        return next(iter(entry.keys.values())) if len(entry.keys) == 1 else None

    async def _refresh(self, url: str, now: float, previous: _Entry | None, *, forced: bool) -> _Entry:
        try:
            keys = await _fetch_keys(url)
        except IdTokenError:
            if previous is not None and now - previous.fetched_at < KEY_TTL_SECONDS:
                logger.warning("Signing keys at %s could not be refreshed; using the cached set", url)
                if forced:
                    previous.last_forced = now
                return previous
            raise
        last_forced = now if forced else (previous.last_forced if previous else float("-inf"))
        entry = _Entry(keys=keys, fetched_at=now, last_forced=last_forced)
        self._entries[url] = entry
        return entry
```

- [ ] **Step 5: Run to verify it passes** — same command. Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/services/id_tokens.py tests/oidc_fakes.py tests/unit/test_id_tokens.py
git commit -m "feat(microsoft): ID-token signature, audience, time checks and key cache"
```

---

### Task 4: Microsoft identity service

**Files:**
- Create: `src/services/microsoft_identity.py`
- Test: `tests/unit/test_microsoft_identity.py`

**Interfaces:**
- Consumes: Task 1 settings field names; Task 2 `UserMicrosoftIdentity`; Task 3 `id_tokens`.
- Produces (`src.services.microsoft_identity`): `AUTHORIZE_URL`, `TOKEN_URL`, `KEYS_URL_TEMPLATE`,
  `ISSUER_TEMPLATE`, `SCOPE`, `ASSERTION_LIFETIME_SECONDS = 300`,
  `MICROSOFT_SIGN_IN_ROLES`, `MICROSOFT_ERROR_MESSAGES: dict[str, str]`, `KEYS: KeySetCache`,
  `class MicrosoftConfigError(ValueError)`, `class MicrosoftSignInError(Exception)`,
  `class InteractionRequired(MicrosoftSignInError)`,
  `@dataclass(frozen=True) MicrosoftSignInConfig(client_id: str, redirect_uri: str,
  allowed_tenant_ids: frozenset[str], private_key_pem: bytes, x5t_s256: str, certificate_not_after: datetime)`,
  `@dataclass(frozen=True) MicrosoftIdentity(tenant_id: uuid.UUID, object_id: uuid.UUID, sign_in_name: str | None)`,
  `@dataclass(frozen=True) RemovedLink(tenant_id: str, sign_in_name: str | None)`,
  `load_config(settings) -> MicrosoftSignInConfig | None`, `may_use_microsoft_sign_in(user) -> bool`,
  `authorization_url(config, *, state, nonce, code_verifier, select_account: bool) -> str`,
  `async exchange_code(config, *, code, code_verifier) -> dict`,
  `async validate_id_token(config, id_token, *, nonce: str, now: int) -> MicrosoftIdentity`,
  `async find_linked_user(db, identity) -> User | None`, `async record_sign_in(db, user_id) -> None`,
  `async link_account(db, user_id, identity) -> str | None` (a `MICROSOFT_ERROR_MESSAGES` key on refusal),
  `async get_link(db, user_id) -> UserMicrosoftIdentity | None`, `async link_view(db, user_id) -> dict | None`
  (keys `sign_in_name, tenant_id (str), linked_at, last_sign_in_at`), `async unlink(db, user_id) -> RemovedLink | None`.
  None of the DB helpers commits.

- [ ] **Step 1: Write the failing tests** `tests/unit/test_microsoft_identity.py`:

```python
"""src/services/microsoft_identity.py (spec 2026-10-13 §5.2, §5.4, §5.5)."""
import base64
import hashlib
import json
import logging
import time
import uuid
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from joserfc import jwt
from joserfc.jwk import RSAKey

from src.services import microsoft_identity as mi
from src.services.id_tokens import IdTokenError
from tests.oidc_fakes import (
    CERT_KEY_PEM,
    CERT_PEM,
    CLIENT_ID,
    DROP,
    OID,
    OTHER_TENANT,
    REDIRECT_URI,
    TENANT,
    b64,
    fake_idp,  # noqa: F401  (fixture)
    forged,
    make_certificate,
    make_config,
    microsoft_claims,
    public_jwk,
    settings_for,
    sign,
)

NOW = int(time.time())


# --- configuration ---------------------------------------------------------------

def test_an_empty_client_id_means_off():
    assert mi.load_config(settings_for(microsoft_client_id="")) is None
    assert mi.load_config(settings_for(microsoft_client_id="  ")) is None


@pytest.mark.parametrize("field", [
    "microsoft_redirect_uri", "microsoft_allowed_tenant_ids",
    "microsoft_certificate_b64", "microsoft_private_key_b64",
])
def test_a_partial_configuration_refuses_to_start(field):
    with pytest.raises(mi.MicrosoftConfigError, match=field.upper()):
        mi.load_config(settings_for(**{field: ""}))


@pytest.mark.parametrize("tenants", ["not-a-guid", f"{TENANT},nope", " , "])
def test_bad_tenant_lists_refuse_to_start(tenants):
    with pytest.raises(mi.MicrosoftConfigError):
        mi.load_config(settings_for(microsoft_allowed_tenant_ids=tenants))


def test_tenant_ids_are_normalised():
    config = mi.load_config(settings_for(microsoft_allowed_tenant_ids=f" {TENANT.upper()} , {OTHER_TENANT}"))
    assert config.allowed_tenant_ids == frozenset({TENANT, OTHER_TENANT})


def test_the_redirect_uri_must_be_https_except_local_development():
    with pytest.raises(mi.MicrosoftConfigError):
        mi.load_config(settings_for(microsoft_redirect_uri="http://blackbird.example/cb"))
    with pytest.raises(mi.MicrosoftConfigError):
        mi.load_config(settings_for(microsoft_redirect_uri="http://localhost:8000/cb"))
    assert mi.load_config(settings_for(microsoft_redirect_uri="http://localhost:8000/cb",
                                       allow_http_sessions=True)) is not None


def test_the_key_must_match_the_certificate_and_be_rsa_2048_or_more():
    _, other_key = make_certificate()
    with pytest.raises(mi.MicrosoftConfigError, match="does not match"):
        mi.load_config(settings_for(microsoft_private_key_b64=b64(other_key)))
    small_cert, small_key = make_certificate(key_size=1024)
    with pytest.raises(mi.MicrosoftConfigError, match="2048"):
        mi.load_config(settings_for(microsoft_certificate_b64=b64(small_cert),
                                    microsoft_private_key_b64=b64(small_key)))
    with pytest.raises(mi.MicrosoftConfigError):
        mi.load_config(settings_for(microsoft_certificate_b64="not base64!"))


def test_the_thumbprint_is_the_base64url_sha256_of_the_certificate_der():
    der = x509.load_pem_x509_certificate(CERT_PEM).public_bytes(serialization.Encoding.DER)
    expected = base64.urlsafe_b64encode(hashlib.sha256(der).digest()).rstrip(b"=").decode()
    assert make_config().x5t_s256 == expected


def test_a_certificate_near_expiry_warns(caplog):
    cert, key = make_certificate(days=10)
    with caplog.at_level(logging.WARNING, logger=mi.__name__):
        mi.load_config(settings_for(microsoft_certificate_b64=b64(cert), microsoft_private_key_b64=b64(key)))
    assert "expires" in caplog.text


def test_only_admins_managers_and_reviewers_may_use_it():
    from types import SimpleNamespace
    assert [r for r in ("pi", "manager", "admin", "reviewer")
            if mi.may_use_microsoft_sign_in(SimpleNamespace(user_role=r))] == ["manager", "admin", "reviewer"]


# --- the authorization request ---------------------------------------------------

def test_the_authorization_url():
    url = mi.authorization_url(make_config(), state="st", nonce="nn", code_verifier="v" * 64,
                               select_account=False)
    parts = urlsplit(url)
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == mi.AUTHORIZE_URL
    expected_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(("v" * 64).encode()).digest()).rstrip(b"=").decode()
    assert q == {
        "client_id": CLIENT_ID, "response_type": "code", "redirect_uri": REDIRECT_URI,
        "response_mode": "query", "scope": "openid profile", "state": "st", "nonce": "nn",
        "code_challenge": expected_challenge, "code_challenge_method": "S256",
    }
    linked = mi.authorization_url(make_config(), state="st", nonce="nn", code_verifier="v" * 64,
                                  select_account=True)
    assert parse_qs(urlsplit(linked).query)["prompt"] == ["select_account"]


# --- the code exchange -----------------------------------------------------------

async def test_the_code_exchange_sends_a_certificate_assertion(fake_idp):
    fake_idp.token_response = (200, {"token_type": "Bearer", "access_token": "opaque", "id_token": "x"})
    config = make_config()
    tokens = await mi.exchange_code(config, code="the-code", code_verifier="v" * 64)
    await mi.exchange_code(config, code="the-code", code_verifier="v" * 64)
    assert tokens["id_token"] == "x"
    form = fake_idp.token_form(0)
    assert form["grant_type"] == "authorization_code" and form["code"] == "the-code"
    assert form["code_verifier"] == "v" * 64 and form["client_id"] == CLIENT_ID
    assert form["redirect_uri"] == REDIRECT_URI
    assert form["client_assertion_type"] == "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
    assert "client_secret" not in form
    assertion = form["client_assertion"]
    header = json.loads(base64.urlsafe_b64decode(assertion.split(".")[0] + "=="))
    assert header == {"alg": "PS256", "typ": "JWT", "x5t#S256": config.x5t_s256}
    claims = jwt.decode(assertion, RSAKey.import_key(CERT_KEY_PEM), algorithms=["PS256"]).claims
    assert claims["iss"] == claims["sub"] == CLIENT_ID
    assert claims["aud"] == mi.TOKEN_URL
    assert claims["exp"] - claims["nbf"] == 300 and claims["iat"] == claims["nbf"]
    second = fake_idp.token_form(1)["client_assertion"]
    second_jti = jwt.decode(second, RSAKey.import_key(CERT_KEY_PEM), algorithms=["PS256"]).claims["jti"]
    assert second_jti != claims["jti"]


async def test_interaction_required_is_its_own_error(fake_idp):
    fake_idp.token_response = (400, {"error": "interaction_required", "error_description": "AADSTS50076"})
    with pytest.raises(mi.InteractionRequired):
        await mi.exchange_code(make_config(), code="c", code_verifier="v" * 64)


@pytest.mark.parametrize("response", [(400, {"error": "invalid_grant"}), (503, {}), (200, ["x"])])
async def test_other_token_failures_are_sign_in_errors(fake_idp, response):
    fake_idp.token_response = response
    with pytest.raises(mi.MicrosoftSignInError):
        await mi.exchange_code(make_config(), code="c", code_verifier="v" * 64)


# --- the ID token ----------------------------------------------------------------

async def test_a_valid_token(fake_idp):
    identity = await mi.validate_id_token(
        make_config(), sign(microsoft_claims(nonce="nn", now=NOW)), nonce="nn", now=NOW)
    assert identity == mi.MicrosoftIdentity(
        tenant_id=uuid.UUID(TENANT), object_id=uuid.UUID(OID), sign_in_name="staff@example.edu")
    assert fake_idp.key_fetches == [mi.KEYS_URL_TEMPLATE.format(tid=TENANT)]


@pytest.mark.parametrize("overrides, check", [
    ({"tid": "not-a-guid"}, "tid"),
    ({"tid": OTHER_TENANT, "iss": f"https://login.microsoftonline.com/{OTHER_TENANT}/v2.0"}, "tenant"),
    ({"iss": "https://login.microsoftonline.com/common/v2.0"}, "iss"),
    ({"iss": f"https://sts.windows.net/{TENANT}/"}, "iss"),
    ({"aud": "someone-else"}, "aud"),
    ({"exp": NOW - 301}, "exp"),
    ({"nbf": NOW + 301}, "nbf"),
    ({"nonce": "other"}, "nonce"),
    ({"nonce": DROP}, "nonce"),
    ({"oid": DROP}, "oid"),
    ({"oid": "nope"}, "oid"),
    ({"acct": 1}, "acct"),
    ({"acct": "1"}, "acct"),
    ({"acct": DROP}, "acct"),
    ({"acct": False}, "acct"),
])
async def test_each_failed_check_refuses(fake_idp, overrides, check):
    token = sign(microsoft_claims(nonce="nn", now=NOW, **overrides))
    with pytest.raises(IdTokenError) as e:
        await mi.validate_id_token(make_config(), token, nonce="nn", now=NOW)
    assert e.value.check == check


async def test_a_tenant_off_the_list_causes_no_key_fetch(fake_idp):
    token = sign(microsoft_claims(nonce="nn", now=NOW, tenant=OTHER_TENANT))
    with pytest.raises(IdTokenError):
        await mi.validate_id_token(make_config(), token, nonce="nn", now=NOW)
    assert fake_idp.key_fetches == []


async def test_alg_none_and_a_wrong_key_are_refused(fake_idp):
    claims = microsoft_claims(nonce="nn", now=NOW)
    with pytest.raises(IdTokenError) as e:
        await mi.validate_id_token(make_config(), forged({"alg": "none"}, claims), nonce="nn", now=NOW)
    assert e.value.check == "alg"
    fake_idp.tenant_keys[TENANT] = [public_jwk(issuer=f"https://login.microsoftonline.com/{TENANT}/v2.0",
                                               key_pem=make_certificate()[1])]
    with pytest.raises(IdTokenError) as e:
        await mi.validate_id_token(make_config(), sign(claims), nonce="nn", now=NOW)
    assert e.value.check == "signature"


@pytest.mark.parametrize("key_issuer, ok", [
    (f"https://login.microsoftonline.com/{TENANT}/v2.0", True),
    ("https://login.microsoftonline.com/{tenantid}/v2.0", True),
    ("https://login.microsoftonline.com/{TenantId}/v2.0", True),
    (f"https://login.microsoftonline.com/{OTHER_TENANT}/v2.0", False),
    (None, False),
])
async def test_the_signing_key_must_belong_to_the_issuer(fake_idp, key_issuer, ok):
    extra = {} if key_issuer is None else {"issuer": key_issuer}
    fake_idp.tenant_keys[TENANT] = [public_jwk(**extra)]
    token = sign(microsoft_claims(nonce="nn", now=NOW))
    if ok:
        await mi.validate_id_token(make_config(), token, nonce="nn", now=NOW)
    else:
        with pytest.raises(IdTokenError) as e:
            await mi.validate_id_token(make_config(), token, nonce="nn", now=NOW)
        assert e.value.check == "key_issuer"


async def test_a_missing_id_token_is_refused(fake_idp):
    with pytest.raises(IdTokenError) as e:
        await mi.validate_id_token(make_config(), None, nonce="nn", now=NOW)
    assert e.value.check == "missing"


@pytest.mark.parametrize("name, stored", [(DROP, None), ("", None), ("a" * 300, "a" * 255)])
async def test_the_sign_in_name_is_optional_and_bounded(fake_idp, name, stored):
    token = sign(microsoft_claims(nonce="nn", now=NOW, preferred_username=name))
    identity = await mi.validate_id_token(make_config(), token, nonce="nn", now=NOW)
    assert identity.sign_in_name == stored
```

- [ ] **Step 2: Run to verify it fails** — host: `.venv-test/bin/python -m pytest tests/unit/test_microsoft_identity.py -q`.
  Expected: FAIL (`ModuleNotFoundError: src.services.microsoft_identity`).

- [ ] **Step 3: Implement** `src/services/microsoft_identity.py`:

```python
"""Microsoft (Entra ID) sign-in for staff: configuration, the authorization request, the code
exchange with a certificate assertion, ID-token validation, and the account links (spec
docs/specs/2026-10-13-microsoft-staff-sign-in-design.md §5.1-§5.5).

Accounts are found by Microsoft's (tid, oid) pair only, never by email: Microsoft says `email`
is mutable and must not be used for authorization. Only members (`acct` 0) of the tenants in
MICROSOFT_ALLOWED_TENANT_IDS get through, and only into accounts whose role is in
MICROSOFT_SIGN_IN_ROLES. Nothing here commits.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlsplit

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client, OAuthError
from authlib.oauth2.rfc7523 import PrivateKeyJWT
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_REVIEWER,
    User,
    UserMicrosoftIdentity,
)
from src.services import id_tokens
from src.services.id_tokens import (
    IdTokenError,
    KeySetCache,
    check_audience,
    check_times,
    unverified_parts,
    verify_signature,
)

logger = logging.getLogger(__name__)

_LOGIN = "https://login.microsoftonline.com"
AUTHORIZE_URL = f"{_LOGIN}/organizations/oauth2/v2.0/authorize"
TOKEN_URL = f"{_LOGIN}/organizations/oauth2/v2.0/token"
KEYS_URL_TEMPLATE = _LOGIN + "/{tid}/discovery/v2.0/keys"
ISSUER_TEMPLATE = _LOGIN + "/{tid}/v2.0"
SCOPE = "openid profile"
ASSERTION_LIFETIME_SECONDS = 300
CERTIFICATE_WARNING_DAYS = 30
MIN_KEY_BITS = 2048

#: Who may link and use a Microsoft sign-in (owner decision D2). Not User.is_staff, which
#: leaves reviewers out.
MICROSOFT_SIGN_IN_ROLES = (USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_REVIEWER)

#: Every user-facing message of the feature, by code: the `/login?error=` codes and the flashes.
MICROSOFT_ERROR_MESSAGES = {
    "microsoft_cancelled": "Microsoft sign-in was cancelled or not approved.",
    "microsoft_error": (
        "Microsoft sign-in failed. If your organisation must approve Blackbird first, "
        "ask its IT team."
    ),
    "microsoft_failed": "Microsoft sign-in failed. Please try again or use ORCID.",
    "microsoft_interaction_required": "Microsoft needs you to sign in again. Please try again.",
    "microsoft_not_linked": (
        "No Blackbird account is linked to this Microsoft account. Sign in with ORCID, "
        "then link it in Settings."
    ),
    "microsoft_not_staff": "Microsoft sign-in is only for staff accounts. Please use ORCID.",
    "microsoft_account_taken": "That Microsoft account is already linked to a Blackbird account.",
    "microsoft_user_already_linked": "Unlink your current Microsoft account first.",
    "microsoft_link_refused": "Sign in with ORCID again, then link your Microsoft account.",
    "state_mismatch": "Security check failed. Please try again.",
}

#: Signing keys per allowed tenant (Microsoft: discover and cache keys per tenant).
KEYS = KeySetCache()


class MicrosoftConfigError(ValueError):
    """The MICROSOFT_* settings are incomplete or invalid; the web app refuses to start (S1)."""


class MicrosoftSignInError(Exception):
    """The code exchange failed. The message is for logs only."""


class InteractionRequired(MicrosoftSignInError):
    """The token endpoint answered `interaction_required`: the user must sign in again."""


@dataclass(frozen=True)
class MicrosoftSignInConfig:
    client_id: str
    redirect_uri: str
    allowed_tenant_ids: frozenset[str]
    private_key_pem: bytes
    x5t_s256: str
    certificate_not_after: datetime


@dataclass(frozen=True)
class MicrosoftIdentity:
    tenant_id: uuid.UUID
    object_id: uuid.UUID
    sign_in_name: str | None


@dataclass(frozen=True)
class RemovedLink:
    tenant_id: str
    sign_in_name: str | None


def _guid(value: object) -> uuid.UUID | None:
    """`value` as a UUID when it is a canonical (hyphenated, any case) GUID string."""
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    return parsed if str(parsed) == value.lower() else None


def _decode_b64(name: str, value: str) -> bytes:
    try:
        return base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MicrosoftConfigError(f"{name} is not base64") from exc


def load_config(settings) -> MicrosoftSignInConfig | None:
    """The validated configuration, or None when MICROSOFT_CLIENT_ID is empty (feature off).

    Raises MicrosoftConfigError on anything incomplete or invalid. Called from create_app()
    only, so it stops the web app and never the worker or the agent (spec S1).
    """
    client_id = settings.microsoft_client_id.strip()
    if not client_id:
        return None
    others = ("microsoft_redirect_uri", "microsoft_allowed_tenant_ids",
              "microsoft_certificate_b64", "microsoft_private_key_b64")
    missing = [name.upper() for name in others if not getattr(settings, name).strip()]
    if missing:
        raise MicrosoftConfigError("MICROSOFT_CLIENT_ID is set but these are empty: " + ", ".join(missing))

    tenants = set()
    for raw in settings.microsoft_allowed_tenant_ids.split(","):
        if not raw.strip():
            continue
        tenant = _guid(raw.strip())
        if tenant is None:
            raise MicrosoftConfigError(f"MICROSOFT_ALLOWED_TENANT_IDS: {raw.strip()[:40]!r} is not a GUID")
        tenants.add(str(tenant))
    if not tenants:
        raise MicrosoftConfigError("MICROSOFT_ALLOWED_TENANT_IDS lists no tenant")

    redirect_uri = settings.microsoft_redirect_uri.strip()
    parts = urlsplit(redirect_uri)
    local_http = (settings.allow_http_sessions and parts.scheme == "http"
                  and parts.hostname == "localhost")
    if not (parts.scheme == "https" or local_http) or not parts.netloc:
        raise MicrosoftConfigError(
            "MICROSOFT_REDIRECT_URI must be https (http only for localhost with ALLOW_HTTP_SESSIONS=true)"
        )

    cert_pem = _decode_b64("MICROSOFT_CERTIFICATE_B64", settings.microsoft_certificate_b64)
    key_pem = _decode_b64("MICROSOFT_PRIVATE_KEY_B64", settings.microsoft_private_key_b64)
    try:
        cert = x509.load_pem_x509_certificate(cert_pem)
    except ValueError as exc:
        raise MicrosoftConfigError("MICROSOFT_CERTIFICATE_B64 is not a PEM certificate") from exc
    try:
        key = serialization.load_pem_private_key(key_pem, password=None)
    except (ValueError, TypeError) as exc:
        raise MicrosoftConfigError(
            "MICROSOFT_PRIVATE_KEY_B64 is not an unencrypted PEM private key"
        ) from exc
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < MIN_KEY_BITS:
        raise MicrosoftConfigError(
            f"MICROSOFT_PRIVATE_KEY_B64 must be an RSA key of at least {MIN_KEY_BITS} bits "
            "(Microsoft Entra ID supports only RSA)"
        )
    cert_public = cert.public_key()
    if (not isinstance(cert_public, rsa.RSAPublicKey)
            or cert_public.public_numbers() != key.public_key().public_numbers()):
        raise MicrosoftConfigError("MICROSOFT_PRIVATE_KEY_B64 does not match the certificate")

    not_after = cert.not_valid_after_utc
    if not_after - datetime.now(UTC) < timedelta(days=CERTIFICATE_WARNING_DAYS):
        logger.warning(
            "The Microsoft sign-in certificate expires %s: upload a new one and update .env "
            "(runbook: docs/operations/pis-and-access.md)", not_after.isoformat(),
        )
    x5t = base64.urlsafe_b64encode(cert.fingerprint(hashes.SHA256())).rstrip(b"=").decode()
    return MicrosoftSignInConfig(
        client_id=client_id, redirect_uri=redirect_uri, allowed_tenant_ids=frozenset(tenants),
        private_key_pem=key_pem, x5t_s256=x5t, certificate_not_after=not_after,
    )


def may_use_microsoft_sign_in(user) -> bool:
    return getattr(user, "user_role", None) in MICROSOFT_SIGN_IN_ROLES


def authorization_url(config: MicrosoftSignInConfig, *, state: str, nonce: str,
                      code_verifier: str, select_account: bool) -> str:
    params = {
        "client_id": config.client_id, "response_type": "code",
        "redirect_uri": config.redirect_uri, "response_mode": "query", "scope": SCOPE,
        "state": state, "nonce": nonce,
        "code_challenge": create_s256_code_challenge(code_verifier),
        "code_challenge_method": "S256",
    }
    if select_account:
        # Linking must never bind whichever account the browser is already signed in to.
        params["prompt"] = "select_account"
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_code(config: MicrosoftSignInConfig, *, code: str, code_verifier: str) -> dict:
    """The token response for `code`, authenticated with a fresh certificate assertion.

    A new PrivateKeyJWT per call: authlib writes the `jti` into the instance's claims and
    would reuse it, and its defaults (1 h, no `nbf`) are not Microsoft's 5-10 minutes.
    """
    now = int(time.time())
    assertion = PrivateKeyJWT(
        alg="PS256",
        headers={"typ": "JWT", "x5t#S256": config.x5t_s256},
        claims={"jti": uuid.uuid4().hex, "iat": now, "nbf": now,
                "exp": now + ASSERTION_LIFETIME_SECONDS},
    )
    try:
        async with AsyncOAuth2Client(
            client_id=config.client_id,
            client_secret=config.private_key_pem,  # PrivateKeyJWT signs with the "secret"
            token_endpoint_auth_method=assertion,
            redirect_uri=config.redirect_uri,
            transport=id_tokens.HTTP_TRANSPORT,
            timeout=id_tokens.HTTP_TIMEOUT_SECONDS,
        ) as client:
            token = await client.fetch_token(
                TOKEN_URL, code=code, grant_type="authorization_code",
                code_verifier=code_verifier, client_id=config.client_id,
            )
    except OAuthError as exc:
        if exc.error == "interaction_required":
            raise InteractionRequired(exc.error) from exc
        raise MicrosoftSignInError(f"token endpoint error={exc.error}") from exc
    except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
        raise MicrosoftSignInError(f"token request failed: {type(exc).__name__}") from exc
    if not isinstance(token, dict):
        raise MicrosoftSignInError("token response is not an object")
    return dict(token)


async def validate_id_token(config: MicrosoftSignInConfig, id_token: object, *, nonce: str,
                            now: int) -> MicrosoftIdentity:
    """The member identity in `id_token`, or IdTokenError (spec §5.5, steps in order)."""
    if not isinstance(id_token, str) or not id_token:
        raise IdTokenError("missing", "no id_token in the token response")
    header, unverified = unverified_parts(id_token)
    raw_tid = unverified.get("tid")
    tenant = _guid(raw_tid)
    if tenant is None:
        raise IdTokenError("tid", "not a GUID")
    if str(tenant) not in config.allowed_tenant_ids:
        raise IdTokenError("tenant", f"{tenant} is not allowed")
    jwk = await KEYS.key_for(KEYS_URL_TEMPLATE.format(tid=tenant), header.get("kid"), now=now)
    claims = verify_signature(id_token, jwk)
    expected_issuer = ISSUER_TEMPLATE.format(tid=raw_tid)
    if claims.get("iss") != expected_issuer:
        raise IdTokenError("iss", repr(claims.get("iss"))[:120])
    key_issuer = jwk.get("issuer")
    if (not isinstance(key_issuer, str)
            or re.sub(r"\{tenantid\}", raw_tid, key_issuer, flags=re.IGNORECASE) != expected_issuer):
        raise IdTokenError("key_issuer", repr(key_issuer)[:120])
    check_audience(claims, config.client_id)
    check_times(claims, now)
    if not nonce or claims.get("nonce") != nonce:
        raise IdTokenError("nonce", "mismatch")
    object_id = _guid(claims.get("oid"))
    if object_id is None:
        raise IdTokenError("oid", "missing or not a GUID")
    acct = claims.get("acct")
    if isinstance(acct, bool) or acct not in (0, "0"):
        raise IdTokenError("acct", f"not a member: {acct!r}"[:60])
    name = claims.get("preferred_username")
    return MicrosoftIdentity(
        tenant_id=tenant, object_id=object_id,
        sign_in_name=name[:255] if isinstance(name, str) and name else None,
    )


async def find_linked_user(db: AsyncSession, identity: MicrosoftIdentity) -> User | None:
    return await db.scalar(
        select(User)
        .join(UserMicrosoftIdentity, UserMicrosoftIdentity.user_id == User.id)
        .where(UserMicrosoftIdentity.tenant_id == identity.tenant_id,
               UserMicrosoftIdentity.object_id == identity.object_id)
    )


async def record_sign_in(db: AsyncSession, user_id: uuid.UUID) -> None:
    await db.execute(
        update(UserMicrosoftIdentity)
        .where(UserMicrosoftIdentity.user_id == user_id)
        .values(last_sign_in_at=func.now())
        .execution_options(synchronize_session=False)
    )


async def get_link(db: AsyncSession, user_id: uuid.UUID) -> UserMicrosoftIdentity | None:
    return await db.scalar(
        select(UserMicrosoftIdentity).where(UserMicrosoftIdentity.user_id == user_id)
    )


async def link_account(db: AsyncSession, user_id: uuid.UUID,
                       identity: MicrosoftIdentity) -> str | None:
    """Insert the link. On refusal, the MICROSOFT_ERROR_MESSAGES key; a concurrent link of the
    same account or user meets the unique constraints inside a savepoint."""
    try:
        async with db.begin_nested():
            db.add(UserMicrosoftIdentity(
                user_id=user_id, tenant_id=identity.tenant_id,
                object_id=identity.object_id, sign_in_name=identity.sign_in_name,
            ))
            await db.flush()
    except IntegrityError:
        if await get_link(db, user_id) is not None:
            return "microsoft_user_already_linked"
        return "microsoft_account_taken"
    return None


async def link_view(db: AsyncSession, user_id: uuid.UUID) -> dict | None:
    """The link as plain values for templates (and the workspace projection), or None."""
    row = await get_link(db, user_id)
    if row is None:
        return None
    return {
        "sign_in_name": row.sign_in_name, "tenant_id": str(row.tenant_id),
        "linked_at": row.linked_at, "last_sign_in_at": row.last_sign_in_at,
    }


async def unlink(db: AsyncSession, user_id: uuid.UUID) -> RemovedLink | None:
    row = await get_link(db, user_id)
    if row is None:
        return None
    removed = RemovedLink(tenant_id=str(row.tenant_id), sign_in_name=row.sign_in_name)
    await db.execute(sa_delete(UserMicrosoftIdentity).where(UserMicrosoftIdentity.id == row.id))
    return removed
```

- [ ] **Step 4: Run to verify it passes** — same command. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/services/microsoft_identity.py tests/unit/test_microsoft_identity.py
git commit -m "feat(microsoft): config, certificate-assertion code exchange and ID-token validation"
```

---

### Task 5: ORCID re-sign-in ID-token validator

**Files:**
- Create: `src/services/orcid_reauth.py`
- Test: `tests/unit/test_orcid_reauth.py`

**Interfaces:**
- Consumes: Task 3 `id_tokens`.
- Produces: `ORCID_ISSUER = "https://orcid.org"`, `ORCID_KEYS_URL = "https://orcid.org/oauth/jwks"`,
  `REAUTH_MAX_AGE_SECONDS = 300`, `KEYS: KeySetCache`,
  `async validate_reauth_id_token(id_token, *, client_id: str, nonce: str | None, orcid: str, now: int) -> None`
  (raises `IdTokenError`).

- [ ] **Step 1: Write the failing tests** `tests/unit/test_orcid_reauth.py`:

```python
"""src/services/orcid_reauth.py (spec 2026-10-13 §5.5.1, D10)."""
import time

import pytest

from src.services.id_tokens import IdTokenError
from src.services.orcid_reauth import validate_reauth_id_token
from tests.oidc_fakes import DROP, fake_idp, sign  # noqa: F401  (fixture)

NOW = int(time.time())
ORCID = "0000-0002-1825-0097"


def _claims(**overrides):
    claims = {"iss": "https://orcid.org", "aud": "APP-TEST", "sub": ORCID, "nonce": "rn",
              "iat": NOW, "exp": NOW + 600, "auth_time": NOW - 5}
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not DROP}


async def _validate(token, **kw):
    args = dict(client_id="APP-TEST", nonce="rn", orcid=ORCID, now=NOW)
    args.update(kw)
    await validate_reauth_id_token(token, **args)


async def test_a_fresh_reauth_passes(fake_idp):
    await _validate(sign(_claims()))
    await _validate(sign(_claims(), kid=None))  # ORCID publishes one key


@pytest.mark.parametrize("overrides, check", [
    ({"auth_time": NOW - 301}, "auth_time"),
    ({"auth_time": DROP}, "auth_time"),
    ({"auth_time": True}, "auth_time"),
    ({"sub": "0000-0000-0000-0001"}, "sub"),
    ({"nonce": "other"}, "nonce"),
    ({"iss": "https://sandbox.orcid.org"}, "iss"),
    ({"aud": "APP-OTHER"}, "aud"),
    ({"exp": NOW - 301}, "exp"),
])
async def test_each_failed_check_refuses(fake_idp, overrides, check):
    with pytest.raises(IdTokenError) as e:
        await _validate(sign(_claims(**overrides)))
    assert e.value.check == check


async def test_a_missing_token_or_nonce_refuses(fake_idp):
    with pytest.raises(IdTokenError):
        await _validate(None)
    with pytest.raises(IdTokenError):
        await _validate(sign(_claims()), nonce=None)
```

- [ ] **Step 2: Run to verify it fails** — host: `.venv-test/bin/python -m pytest tests/unit/test_orcid_reauth.py -q`. Expected: FAIL (module missing).

- [ ] **Step 3: Implement** `src/services/orcid_reauth.py`:

```python
"""The ORCID re-sign-in that linking a Microsoft account requires (owner decision D10; spec
docs/specs/2026-10-13-microsoft-staff-sign-in-design.md §5.5.1).

The reauth asks ORCID for `scope=openid` with `prompt=login`, which ORCID documents as forcing
an already signed-in user to reauthenticate. Its ID token proves it: signed by ORCID, for our
client, carrying our nonce, for the signed-in user's ORCID iD, with `auth_time` at most
REAUTH_MAX_AGE_SECONDS old. Ordinary ORCID sign-in (scope /authenticate) never comes here.
"""

from __future__ import annotations

from src.services.id_tokens import (
    LEEWAY_SECONDS,
    IdTokenError,
    KeySetCache,
    check_audience,
    check_times,
    unverified_parts,
    verify_signature,
)

#: From ORCID's live discovery document (https://orcid.org/.well-known/openid-configuration).
ORCID_ISSUER = "https://orcid.org"
ORCID_KEYS_URL = "https://orcid.org/oauth/jwks"
REAUTH_MAX_AGE_SECONDS = 300

KEYS = KeySetCache()


async def validate_reauth_id_token(id_token: object, *, client_id: str, nonce: str | None,
                                   orcid: str, now: int) -> None:
    """Return when the token proves `orcid` just re-entered their ORCID credentials."""
    if not isinstance(id_token, str) or not id_token:
        raise IdTokenError("missing", "no id_token in ORCID's token response")
    header, _ = unverified_parts(id_token)
    claims = verify_signature(id_token, await KEYS.key_for(ORCID_KEYS_URL, header.get("kid"), now=now))
    if claims.get("iss") != ORCID_ISSUER:
        raise IdTokenError("iss", repr(claims.get("iss"))[:80])
    check_audience(claims, client_id)
    check_times(claims, now)
    if not nonce or claims.get("nonce") != nonce:
        raise IdTokenError("nonce", "mismatch")
    if claims.get("sub") != orcid:
        raise IdTokenError("sub", "not the signed-in user's ORCID iD")
    auth_time = claims.get("auth_time")
    if (isinstance(auth_time, bool) or not isinstance(auth_time, int)
            or not now - REAUTH_MAX_AGE_SECONDS <= auth_time <= now + LEEWAY_SECONDS):
        raise IdTokenError("auth_time", f"{auth_time!r}"[:40])
```

- [ ] **Step 4: Run to verify it passes** — same command. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/services/orcid_reauth.py tests/unit/test_orcid_reauth.py
git commit -m "feat(microsoft): validate the ORCID re-sign-in ID token (prompt=login, auth_time)"
```

---

### Task 6: Shared sign-in tail, sign-in stamps, ORCID reauth, login page

**Files:**
- Create: `src/web/sign_in_session.py`
- Modify: `src/routers/auth.py` (imports; `login` ~145; `login_start` ~158-166; `_post_login_redirect` ~270; `auth_callback` ~310-400)
- Modify: `src/dependencies.py` (new `pending_access_payload`; use it at ~121-126)
- Modify: `templates/login.html`
- Modify: `tests/integration/test_session_epoch.py` (four exact-dict asserts)
- Test: `tests/integration/test_orcid_reauth_flow.py`

**Interfaces:**
- Consumes: Task 4 `MICROSOFT_ERROR_MESSAGES`; Task 5 `validate_reauth_id_token`; Task 3 `IdTokenError`.
- Produces: `src.web.sign_in_session`: `SIGNED_IN_AT_KEY = "signed_in_at"`, `SIGNED_IN_VIA_KEY = "signed_in_via"`,
  `VIA_ORCID = "orcid"`, `VIA_MICROSOFT = "microsoft"`, `LINK_REAUTH_MAX_AGE_SECONDS = 900`,
  `record_sign_in(session, via: str, now: int | None = None) -> None`,
  `fresh_orcid_sign_in(session, now: float | None = None) -> bool`.
  `src.routers.auth`: `async complete_sign_in(request, db, user, *, via: str) -> RedirectResponse`
  (commits), `ORCID_REAUTH_KEY = "orcid_reauth"`. `src.dependencies.pending_access_payload(user) -> dict`.
  `/login` template context gains `error_message: str | None` and `microsoft_enabled: bool`.

- [ ] **Step 1: Write the failing tests.** In `tests/integration/test_session_epoch.py` add below the imports:

```python
def _login_session(response) -> dict:
    """The session a successful login set, minus the sign-in stamp (spec 2026-10-13 §5.3)."""
    session = session_from_response(response)
    assert isinstance(session.pop("signed_in_at"), int)
    assert session.pop("signed_in_via") == "orcid"
    return session
```

and replace `session_from_response(r) == {` with `_login_session(r) == {` in the four tests
`test_login_clears_the_pre_login_session_and_keeps_the_vetted_next`,
`test_login_drops_an_unsafe_next`, `test_login_keeps_a_pending_invite_token`,
`test_login_stores_the_accounts_epoch`. Create `tests/integration/test_orcid_reauth_flow.py`:

```python
"""The ORCID re-sign-in before linking Microsoft (spec 2026-10-13 §5.3, §5.5.1, D10)."""
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from src.config import get_settings
from src.models import USER_ROLE_MANAGER
from src.routers import auth as auth_module
from tests import factories
from tests.oidc_fakes import fake_idp, sign  # noqa: F401  (fixture)
from tests.session_support import session_from_response, session_headers

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _orcid_client_id(monkeypatch):
    monkeypatch.setattr(get_settings(), "orcid_client_id", "APP-TEST")


def _orcid_client(orcid, id_token=None):
    class _Client:
        async def fetch_token(self, url, code, grant_type):
            token = {"orcid": orcid, "name": "Test User"}
            if id_token is not None:
                token["id_token"] = id_token
            return token

    return _Client()


def _claims(orcid, *, nonce="rn", auth_age=5, **overrides):
    now = int(time.time())
    claims = {"iss": "https://orcid.org", "aud": "APP-TEST", "sub": orcid, "nonce": nonce,
              "iat": now, "exp": now + 600, "auth_time": now - auth_age}
    claims.update(overrides)
    return claims


async def _manager(db_session):
    return await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)


async def _reauth_callback(client, monkeypatch, user, *, token_orcid=None, id_token=None,
                           started_at=None):
    monkeypatch.setattr(auth_module, "_get_oauth_client",
                        lambda: _orcid_client(token_orcid or user.orcid, id_token))

    async def _profile(orcid_id):
        return {"orcid": orcid_id, "name": "Test User"}

    monkeypatch.setattr(auth_module, "fetch_orcid_profile", _profile)
    started = int(time.time()) if started_at is None else started_at
    return await client.get("/auth/callback?code=c&state=s", headers=session_headers(
        user.id, oauth_state="s", orcid_reauth={"nonce": "rn", "started_at": started},
        post_login_redirect="/settings", signed_in_at=started - 3600, signed_in_via="orcid",
    ))


async def test_plain_login_start_still_sends_a_signed_in_user_home(client, db_session):
    user = await _manager(db_session)
    r = await client.get("/login/start", headers=session_headers(user.id))
    assert r.status_code == 302 and r.headers["location"] == "/"


async def test_reauth_start_forces_an_orcid_password_prompt(client, db_session):
    user = await _manager(db_session)
    r = await client.get("/login/start?reauth=1", headers=session_headers(user.id))
    assert r.status_code == 302
    location = urlsplit(r.headers["location"])
    q = {k: v[0] for k, v in parse_qs(location.query).items()}
    assert location.netloc == "orcid.org"
    assert q["scope"] == "openid" and q["prompt"] == "login" and q["nonce"]
    session = session_from_response(r)
    assert session["orcid_reauth"]["nonce"] == q["nonce"]
    assert session["post_login_redirect"] == "/settings"
    assert session["oauth_state"] == q["state"]


async def test_ordinary_login_start_is_unchanged(client):
    r = await client.get("/login/start")
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["scope"] == ["/authenticate"] and "prompt" not in q and "nonce" not in q


async def test_reauth_start_is_refused_while_impersonating(client, db_session):
    admin = await factories.make_user(db_session, user_role="admin")
    other = await _manager(db_session)
    r = await client.get("/login/start?reauth=1", headers=session_headers(admin.id, impersonate=other.id))
    assert r.headers["location"] == "/"


async def test_a_proven_reauth_signs_in_afresh_and_returns_to_settings(
        client, db_session, monkeypatch, fake_idp):
    user = await _manager(db_session)
    r = await _reauth_callback(client, monkeypatch, user, id_token=sign(_claims(user.orcid)))
    assert r.status_code == 302 and r.headers["location"] == "/settings"
    session = session_from_response(r)
    assert session["user_id"] == str(user.id) and session["signed_in_via"] == "orcid"
    assert int(time.time()) - session["signed_in_at"] < 60
    assert "orcid_reauth" not in session


@pytest.mark.parametrize("case", ["stale_auth_time", "other_orcid", "no_id_token", "wrong_nonce", "expired_flow"])
async def test_an_unproven_reauth_keeps_the_session_and_says_so(
        client, db_session, monkeypatch, fake_idp, case):
    user = await _manager(db_session)
    other = await factories.make_user(db_session)
    kwargs = {"id_token": sign(_claims(user.orcid))}
    if case == "stale_auth_time":
        kwargs["id_token"] = sign(_claims(user.orcid, auth_age=301))
    elif case == "other_orcid":
        kwargs = {"token_orcid": other.orcid, "id_token": sign(_claims(other.orcid))}
    elif case == "no_id_token":
        kwargs = {}
    elif case == "wrong_nonce":
        kwargs["id_token"] = sign(_claims(user.orcid, nonce="other"))
    elif case == "expired_flow":
        kwargs["started_at"] = int(time.time()) - 601
    r = await _reauth_callback(client, monkeypatch, user, **kwargs)
    assert r.status_code == 302 and r.headers["location"] == "/settings"
    session = session_from_response(r)
    assert session["user_id"] == str(user.id)
    assert session["_flashes"][0]["kind"] == "error"
    assert "post_login_redirect" not in session
```

- [ ] **Step 2: Run to verify they fail** — host:
  `.venv-test/bin/python -m pytest tests/integration/test_session_epoch.py tests/integration/test_orcid_reauth_flow.py -q`.
  Expected: FAIL (`KeyError: 'signed_in_at'`; reauth start redirects to `/`).

- [ ] **Step 3: Create** `src/web/sign_in_session.py`:

```python
"""The sign-in stamp every login writes into the session, and the freshness test that linking a
Microsoft account needs (spec docs/specs/2026-10-13-microsoft-staff-sign-in-design.md §5.3).

A session issued before this stamp existed has neither key and counts as stale.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, MutableMapping
from typing import Any

SIGNED_IN_AT_KEY = "signed_in_at"
SIGNED_IN_VIA_KEY = "signed_in_via"
VIA_ORCID = "orcid"
VIA_MICROSOFT = "microsoft"
#: How recent the ORCID sign-in must be to link a Microsoft account.
LINK_REAUTH_MAX_AGE_SECONDS = 15 * 60


def record_sign_in(session: MutableMapping[str, Any], via: str, now: int | None = None) -> None:
    session[SIGNED_IN_AT_KEY] = int(time.time()) if now is None else now
    session[SIGNED_IN_VIA_KEY] = via


def fresh_orcid_sign_in(session: Mapping[str, Any], now: float | None = None) -> bool:
    """True when this session's sign-in was through ORCID at most 15 minutes ago."""
    signed_in_at = session.get(SIGNED_IN_AT_KEY)
    if (session.get(SIGNED_IN_VIA_KEY) != VIA_ORCID or isinstance(signed_in_at, bool)
            or not isinstance(signed_in_at, int)):
        return False
    age = (time.time() if now is None else now) - signed_in_at
    return 0 <= age <= LINK_REAUTH_MAX_AGE_SECONDS
```

- [ ] **Step 4: `src/dependencies.py`** — add above `get_current_user`:

```python
def pending_access_payload(user: User) -> dict:
    """What /access-pending shows for an account that may not sign in. One shape for the login
    gate (src/routers/auth.py::complete_sign_in) and the revocation check below."""
    return {"user_id": str(user.id), "orcid": user.orcid, "email": user.email, "name": user.name}
```

and in `get_current_user` replace the inline dict with
`request.session["pending_access"] = pending_access_payload(session_user)`.

- [ ] **Step 5: `src/routers/auth.py`.** Add imports:

```python
import secrets
import time
```
```python
from src.dependencies import IMPERSONATE_KEY, pending_access_payload
from src.services.id_tokens import IdTokenError
from src.services.microsoft_identity import MICROSOFT_ERROR_MESSAGES
from src.services.orcid_reauth import validate_reauth_id_token
from src.web.flash import flash
from src.web.sign_in_session import VIA_ORCID, record_sign_in
```

Add constants after `ORCID_SCOPE`:

```python
#: The ORCID re-sign-in before linking Microsoft (spec 2026-10-13 D10): ORCID documents
#: `prompt` and `auth_time` only for the openid scope, which grants what /authenticate does.
ORCID_REAUTH_SCOPE = "openid"
#: Session key of a pending reauth: {"nonce", "started_at"}.
ORCID_REAUTH_KEY = "orcid_reauth"
REAUTH_FLOW_MAX_AGE_SECONDS = 600
_REAUTH_FAILED = "Could not confirm your ORCID sign-in. Please try again."
```

Replace the `login` view's last line with:

```python
    error = request.query_params.get("error")
    return templates.TemplateResponse(request, "login.html", {
        "error": error,
        "error_message": MICROSOFT_ERROR_MESSAGES.get(error or ""),
        "microsoft_enabled": getattr(request.app.state, "microsoft_sign_in", None) is not None,
    })
```

Replace `login_start` with:

```python
@router.get("/login/start")
async def login_start(request: Request, reauth: str | None = None):
    """Initiate the ORCID OAuth redirect.

    With ``reauth=1`` a signed-in user signs in to ORCID again before linking a Microsoft
    account: ``prompt=login`` forces the password prompt and the callback checks the ID token's
    ``auth_time`` (spec 2026-10-13 D10), then returns to /settings. Refused while impersonating.
    """
    signed_in = bool(request.session.get("user_id"))
    if signed_in and (reauth != "1" or request.session.get(IMPERSONATE_KEY)):
        return RedirectResponse(url="/", status_code=302)
    client = _get_oauth_client()
    if signed_in:
        nonce = secrets.token_urlsafe(32)
        authorization_url, state = client.create_authorization_url(
            ORCID_AUTH_URL, scope=ORCID_REAUTH_SCOPE, prompt="login", nonce=nonce
        )
        request.session[ORCID_REAUTH_KEY] = {"nonce": nonce, "started_at": int(time.time())}
        request.session[POST_LOGIN_KEY] = "/settings"
    else:
        authorization_url, state = client.create_authorization_url(ORCID_AUTH_URL)
    request.session["oauth_state"] = state
    return RedirectResponse(url=authorization_url, status_code=302)
```

In `_post_login_redirect`, change the signature to `def _post_login_redirect(request: Request, user: User, *, via: str) -> RedirectResponse:`
and after `request.session[SESSION_EPOCH_KEY] = current_epoch(user)` add
`record_sign_in(request.session, via)`.

Add after `_post_login_redirect`:

```python
async def complete_sign_in(request: Request, db: AsyncSession, user: User, *,
                           via: str) -> RedirectResponse:
    """The end of every sign-in, ORCID or Microsoft (spec 2026-10-13 §5.3): stamp, commit, start
    a fresh session (A-11), then the access gate and the landing page. One function, so the gate
    and the session rules cannot drift between providers."""
    if user.access_status == "allowed":
        user.last_login_at = datetime.now(timezone.utc)
    await db.commit()
    _start_fresh_session(request)
    if user.access_status != "allowed":
        request.session["pending_access"] = pending_access_payload(user)
        return RedirectResponse(url="/access-pending", status_code=302)
    return _post_login_redirect(request, user, via=via)


def _callback_failure(request: Request, code: str, *, reauth: bool) -> RedirectResponse:
    """Where a failed ORCID callback lands. A reauth belongs to a signed-in user, whom /login
    would bounce to / and drop the message, so it gets a flash on /settings instead."""
    if reauth:
        request.session.pop(POST_LOGIN_KEY, None)
        flash(request, _REAUTH_FAILED, "error")
        return RedirectResponse(url="/settings", status_code=302)
    return RedirectResponse(url=f"/login?error={code}", status_code=302)


async def _orcid_reauth_refusal(request: Request, db: AsyncSession, token: dict, orcid_id: str,
                                reauth: dict) -> RedirectResponse | None:
    """None when this callback proves the signed-in user just re-entered their ORCID credentials
    (D10); otherwise the refusal, with the session left as it was."""
    now = int(time.time())
    user_id = _session_user_id(request)
    user = await db.get(User, user_id) if user_id is not None else None
    started = reauth.get("started_at")
    if (user is None or user.orcid != orcid_id or request.session.get(IMPERSONATE_KEY)
            or isinstance(started, bool) or not isinstance(started, int)
            or now - started > REAUTH_FLOW_MAX_AGE_SECONDS):
        logger.warning("ORCID reauth refused: not the signed-in account, impersonating, or expired")
        return _callback_failure(request, "reauth", reauth=True)
    try:
        await validate_reauth_id_token(
            token.get("id_token"), client_id=get_settings().orcid_client_id,
            nonce=reauth.get("nonce"), orcid=orcid_id, now=now,
        )
    except IdTokenError as exc:
        logger.warning("ORCID reauth refused: %s", exc)
        return _callback_failure(request, "reauth", reauth=True)
    return None
```

In `auth_callback`: as its first statement add
`reauth = request.session.pop(ORCID_REAUTH_KEY, None)` and `is_reauth = isinstance(reauth, dict)`;
replace each of the five `return RedirectResponse(url="/login?error=<code>", status_code=302)`
with `return _callback_failure(request, "<code>", reauth=is_reauth)` (codes `oauth_error`,
`no_code`, `state_mismatch`, `token_error`, `no_orcid`); after the `if not orcid_id:` block add:

```python
    if is_reauth:
        refusal = await _orcid_reauth_refusal(request, db, token, orcid_id, reauth)
        if refusal is not None:
            return refusal
```

and replace everything from `if user.access_status == "allowed":` (the `last_login_at` stamp)
to the end of the function with `return await complete_sign_in(request, db, user, via=VIA_ORCID)`.
Leave the existing `client.fetch_token(ORCID_TOKEN_URL, code=code, grant_type="authorization_code")`
call exactly as it is (the ORCID test fakes pin that signature).

- [ ] **Step 6: `templates/login.html`.** In the error block, before `{% else %}Sign in failed…`, add
  `{% elif error_message %}{{ error_message }}`. After the ORCID `<a href="/login/start" …>…</a>` add:

```html
            {% if microsoft_enabled %}
            <a href="/auth/microsoft/start"
               class="flex items-center justify-center w-full mt-3 bg-white border border-gray-300 hover:bg-gray-50 text-gray-800 font-medium py-3 px-6 rounded-lg transition-colors duration-200">
                Sign in with Microsoft (staff)
            </a>
            {% endif %}
```

- [ ] **Step 7: Run to verify they pass** — host:
  `.venv-test/bin/python -m pytest tests/integration/test_session_epoch.py tests/integration/test_orcid_reauth_flow.py tests/integration/test_auth_allowlist_gate.py tests/integration/test_user_email.py tests/integration/test_access_revocation.py tests/characterization/test_auth_and_admin_routes.py tests/integration/test_double_submits.py tests/integration/test_orcid_id_names.py tests/unit/test_login_redirect.py -q`.
  Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/web/sign_in_session.py src/routers/auth.py src/dependencies.py templates/login.html tests/integration/test_session_epoch.py tests/integration/test_orcid_reauth_flow.py
git commit -m "feat(microsoft): shared sign-in tail, sign-in stamp and the ORCID prompt=login reauth"
```

---

### Task 7: Microsoft router and app wiring

**Files:**
- Create: `src/routers/microsoft_auth.py`
- Modify: `src/main.py` (router import tuple; `create_app` state + `include_router`)
- Modify: `tests/conftest.py` (`asgi_app`, ~line 196)
- Modify: `tests/unit/test_reachability.py` (`ROUTE_ALLOWLIST`), `tests/integration/test_rendered_page_gate.py` (`SKIPPED`)
- Test: `tests/integration/test_microsoft_sign_in.py`

**Interfaces:**
- Consumes: Task 4 service; Task 6 `complete_sign_in`, `sign_in_session`; Task 2 model.
- Produces: routes `GET /auth/microsoft/start`, `GET /auth/microsoft/link`, `GET /auth/microsoft/callback`;
  `FLOW_KEY = "microsoft_flow"`, `FLOW_MAX_AGE_SECONDS = 600`; `app.state.microsoft_sign_in`.

- [ ] **Step 1: Write the failing tests** `tests/integration/test_microsoft_sign_in.py`:

```python
"""Microsoft sign-in and linking flows (spec 2026-10-13 §5.3-§5.6)."""
import time
import uuid
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import func, select

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_PI, User, UserMicrosoftIdentity
from tests import factories
from tests.oidc_fakes import (
    OID,
    OTHER_TENANT,
    TENANT,
    fake_idp,  # noqa: F401  (fixture)
    make_config,
    microsoft_claims,
    sign,
)
from tests.session_support import raw_session_headers, session_from_response, session_headers

pytestmark = pytest.mark.integration


@pytest.fixture
def on(asgi_app):
    asgi_app.state.microsoft_sign_in = make_config()


def _flow(purpose="signin", *, user_id=None, started_at=None):
    return {"state": "st", "nonce": "nn", "code_verifier": "v" * 64, "purpose": purpose,
            "user_id": user_id, "started_at": int(time.time()) if started_at is None else started_at}


def _token(fake_idp, **claims):
    fake_idp.token_response = (200, {"token_type": "Bearer", "access_token": "opaque",
                                     "id_token": sign(microsoft_claims(nonce="nn", **claims))})


async def _staff(db_session, role=USER_ROLE_MANAGER, **kw):
    return await factories.make_user(db_session, user_role=role, **kw)


async def _linked(db_session, user, oid=OID):
    db_session.add(UserMicrosoftIdentity(user_id=user.id, tenant_id=uuid.UUID(TENANT),
                                         object_id=uuid.UUID(oid)))
    await db_session.flush()


async def _sign_in_callback(client, query="code=c&state=st", flow=None):
    session = {} if flow is False else {"microsoft_flow": flow or _flow()}
    return await client.get(f"/auth/microsoft/callback?{query}", headers=raw_session_headers(session))


def _fresh(user, **extra):
    return session_headers(user.id, signed_in_at=int(time.time()) - 10, signed_in_via="orcid", **extra)


# --- feature switch --------------------------------------------------------------

async def test_every_microsoft_route_is_404_while_off(client, db_session):
    user = await _staff(db_session)
    for path in ("/auth/microsoft/start", "/auth/microsoft/callback?code=c&state=s"):
        assert (await client.get(path)).status_code == 404
    assert (await client.get("/auth/microsoft/link", headers=_fresh(user))).status_code == 404


async def test_the_login_page_shows_microsoft_only_when_on(client, asgi_app):
    assert "Sign in with Microsoft" not in (await client.get("/login")).text
    asgi_app.state.microsoft_sign_in = make_config()
    assert 'href="/auth/microsoft/start"' in (await client.get("/login")).text


async def test_the_login_page_shows_microsoft_messages(client):
    r = await client.get("/login?error=microsoft_not_linked")
    assert "No Blackbird account is linked to this Microsoft account" in r.text


# --- sign-in ---------------------------------------------------------------------

async def test_start_stores_a_flow_and_redirects_to_microsoft(client, on):
    r = await client.get("/auth/microsoft/start")
    location = urlsplit(r.headers["location"])
    q = {k: v[0] for k, v in parse_qs(location.query).items()}
    flow = session_from_response(r)["microsoft_flow"]
    assert location.netloc == "login.microsoftonline.com" and location.path == "/organizations/oauth2/v2.0/authorize"
    assert q["state"] == flow["state"] and q["nonce"] == flow["nonce"] and "prompt" not in q
    assert flow["purpose"] == "signin" and len(flow["code_verifier"]) >= 43


async def test_start_sends_a_signed_in_user_home(client, db_session, on):
    user = await _staff(db_session)
    r = await client.get("/auth/microsoft/start", headers=session_headers(user.id))
    assert r.headers["location"] == "/"


async def test_a_linked_manager_signs_in(client, db_session, on, fake_idp):
    user = await _staff(db_session)
    await _linked(db_session, user)
    users_before = await db_session.scalar(select(func.count()).select_from(User))
    _token(fake_idp)
    r = await _sign_in_callback(client)
    assert r.status_code == 302 and r.headers["location"] == "/workspace/pis"
    session = session_from_response(r)
    assert session["user_id"] == str(user.id) and session["signed_in_via"] == "microsoft"
    assert "microsoft_flow" not in session
    row = await db_session.scalar(select(UserMicrosoftIdentity).where(UserMicrosoftIdentity.user_id == user.id))
    await db_session.refresh(row)
    assert row.last_sign_in_at is not None
    assert await db_session.scalar(select(func.count()).select_from(User)) == users_before


@pytest.mark.parametrize("setup, code", [
    ("unlinked", "microsoft_not_linked"),
    ("pi", "microsoft_not_staff"),
    ("other_tenant", "microsoft_failed"),
    ("guest", "microsoft_failed"),
])
async def test_refused_sign_ins(client, db_session, on, fake_idp, setup, code):
    user = await _staff(db_session, role=USER_ROLE_PI if setup == "pi" else USER_ROLE_MANAGER)
    if setup != "unlinked":
        await _linked(db_session, user)
    if setup == "other_tenant":
        _token(fake_idp, tenant=OTHER_TENANT)
    elif setup == "guest":
        _token(fake_idp, acct=1)
    else:
        _token(fake_idp)
    r = await _sign_in_callback(client)
    assert r.headers["location"] == f"/login?error={code}"
    assert "user_id" not in (session_from_response(r) or {})


async def test_a_denied_account_goes_to_access_pending(client, db_session, on, fake_idp):
    user = await _staff(db_session, access_status="denied")
    await _linked(db_session, user)
    _token(fake_idp)
    r = await _sign_in_callback(client)
    assert r.headers["location"] == "/access-pending"
    assert set(session_from_response(r)) == {"pending_access"}


@pytest.mark.parametrize("query, flow, code", [
    ("code=c&state=wrong", None, "state_mismatch"),
    ("state=st", None, "state_mismatch"),
    ("code=c&state=st", "expired", "state_mismatch"),
    ("error=access_denied&state=st", None, "microsoft_cancelled"),
    ("error=server_error&state=st", None, "microsoft_error"),
])
async def test_callback_errors(client, on, query, flow, code):
    r = await _sign_in_callback(
        client, query, _flow(started_at=int(time.time()) - 601) if flow == "expired" else None)
    assert r.headers["location"] == f"/login?error={code}"


async def test_a_replayed_callback_is_refused(client, on):
    r = await _sign_in_callback(client, flow=False)
    assert r.headers["location"] == "/login?error=state_mismatch"


async def test_a_token_response_without_an_id_token_fails_cleanly(client, on, fake_idp):
    fake_idp.token_response = (200, {"token_type": "Bearer", "access_token": "opaque"})
    r = await _sign_in_callback(client)
    assert r.headers["location"] == "/login?error=microsoft_failed"


async def test_a_key_outage_with_a_cold_cache_fails_cleanly(client, on, fake_idp):
    _token(fake_idp)
    fake_idp.keys_status = 503
    r = await _sign_in_callback(client)
    assert r.headers["location"] == "/login?error=microsoft_failed"


async def test_interaction_required_asks_to_sign_in_again(client, on, fake_idp):
    fake_idp.token_response = (400, {"error": "interaction_required"})
    r = await _sign_in_callback(client)
    assert r.headers["location"] == "/login?error=microsoft_interaction_required"


# --- linking ---------------------------------------------------------------------

async def test_link_needs_a_fresh_orcid_sign_in(client, db_session, on):
    user = await _staff(db_session)
    for headers in (session_headers(user.id),
                    session_headers(user.id, signed_in_at=int(time.time()) - 901, signed_in_via="orcid"),
                    session_headers(user.id, signed_in_at=int(time.time()), signed_in_via="microsoft")):
        r = await client.get("/auth/microsoft/link", headers=headers)
        assert r.headers["location"] == "/settings"
        assert session_from_response(r)["_flashes"][0]["kind"] == "error"


async def test_link_start_asks_microsoft_for_the_account_picker(client, db_session, on):
    user = await _staff(db_session)
    r = await client.get("/auth/microsoft/link", headers=_fresh(user))
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["prompt"] == ["select_account"]
    flow = session_from_response(r)["microsoft_flow"]
    assert flow["purpose"] == "link" and flow["user_id"] == str(user.id)


async def test_link_is_refused_for_pis_and_while_impersonating(client, db_session, on):
    pi = await _staff(db_session, role=USER_ROLE_PI)
    assert (await client.get("/auth/microsoft/link", headers=_fresh(pi))).status_code == 403
    admin = await _staff(db_session, role=USER_ROLE_ADMIN)
    manager = await _staff(db_session)
    r = await client.get("/auth/microsoft/link", headers=_fresh(admin, impersonate=manager.id))
    assert r.status_code == 403


async def _link_callback(client, user, **session):
    flow = _flow("link", user_id=str(user.id))
    return await client.get("/auth/microsoft/callback?code=c&state=st",
                            headers=_fresh(user, microsoft_flow=flow, **session))


async def test_the_link_callback_links_the_account(client, db_session, on, fake_idp):
    user = await _staff(db_session)
    _token(fake_idp)
    r = await _link_callback(client, user)
    assert r.headers["location"] == "/settings"
    assert session_from_response(r)["_flashes"][0]["kind"] == "success"
    row = await db_session.scalar(select(UserMicrosoftIdentity).where(UserMicrosoftIdentity.user_id == user.id))
    assert (str(row.tenant_id), str(row.object_id), row.sign_in_name) == (TENANT, OID, "staff@example.edu")


@pytest.mark.parametrize("taken_by", ["someone_else", "self"])
async def test_the_link_callback_refuses_a_second_link(client, db_session, on, fake_idp, taken_by):
    user = await _staff(db_session)
    if taken_by == "someone_else":
        await _linked(db_session, await _staff(db_session))
        expected = "already linked to a Blackbird account"
    else:
        await _linked(db_session, user, oid=str(uuid.uuid4()))
        expected = "Unlink your current Microsoft account first"
    _token(fake_idp)
    r = await _link_callback(client, user)
    assert r.headers["location"] == "/settings"
    assert expected in session_from_response(r)["_flashes"][0]["text"]


async def test_the_link_callback_must_be_the_session_that_started_it(client, db_session, on, fake_idp):
    user, other = await _staff(db_session), await _staff(db_session)
    _token(fake_idp)
    flow = _flow("link", user_id=str(other.id))
    r = await client.get("/auth/microsoft/callback?code=c&state=st",
                         headers=_fresh(user, microsoft_flow=flow))
    assert r.headers["location"] == "/settings"
    assert await db_session.scalar(select(func.count()).select_from(UserMicrosoftIdentity)) == 0
```

- [ ] **Step 2: Run to verify they fail** — host: `.venv-test/bin/python -m pytest tests/integration/test_microsoft_sign_in.py -q`.
  Expected: FAIL (404 for every Microsoft route; `app.state` has no `microsoft_sign_in`).

- [ ] **Step 3: Implement** `src/routers/microsoft_auth.py`:

```python
"""Microsoft sign-in for staff: start, link and the one shared callback (spec
docs/specs/2026-10-13-microsoft-staff-sign-in-design.md §5.3-§5.6).

Every handler 404s while app.state.microsoft_sign_in is None (feature off). The callback is a
GET (response_mode=query): a form_post callback would be refused by OriginGuardMiddleware and
arrive without the SameSite=lax session cookie. One session key, FLOW_KEY, carries a single
attempt's state, nonce, PKCE verifier and purpose; it is separate from ORCID's oauth_state, so
neither provider's callback can consume the other's flow.
"""

from __future__ import annotations

import logging
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import IMPERSONATE_KEY, get_current_user, refuse_impersonation
from src.models import User
from src.routers.auth import complete_sign_in
from src.services import microsoft_identity
from src.services.id_tokens import IdTokenError
from src.services.microsoft_identity import (
    MICROSOFT_ERROR_MESSAGES,
    InteractionRequired,
    MicrosoftIdentity,
    MicrosoftSignInConfig,
    MicrosoftSignInError,
    may_use_microsoft_sign_in,
)
from src.web.flash import flash
from src.web.sign_in_session import VIA_MICROSOFT, fresh_orcid_sign_in

logger = logging.getLogger(__name__)
router = APIRouter()

FLOW_KEY = "microsoft_flow"
FLOW_MAX_AGE_SECONDS = 600
_IMPERSONATION_DETAIL = "Microsoft sign-in changes are disabled while impersonating."


def _require_config(request: Request) -> MicrosoftSignInConfig:
    config = getattr(request.app.state, "microsoft_sign_in", None)
    if config is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return config


# Module-level dependency singletons (the src/ lint ratchet counts each Depends in a default).
_CONFIG = Depends(_require_config)
_DB = Depends(get_db)
_USER = Depends(get_current_user)


def _fail(request: Request, purpose: object, code: str) -> RedirectResponse:
    """A linking user (signed in) gets a flash on /settings; anyone else /login?error=<code>,
    because /login sends a signed-in user to / and would drop the message."""
    if purpose == "link" or request.session.get("user_id"):
        flash(request, MICROSOFT_ERROR_MESSAGES[code], "error")
        return RedirectResponse(url="/settings", status_code=302)
    return RedirectResponse(url=f"/login?error={code}", status_code=302)


def _start(request: Request, config: MicrosoftSignInConfig, *, purpose: str,
           user_id: str | None = None) -> RedirectResponse:
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)  # 86 characters, within PKCE's 43-128
    request.session[FLOW_KEY] = {
        "state": state, "nonce": nonce, "code_verifier": code_verifier, "purpose": purpose,
        "user_id": user_id, "started_at": int(time.time()),
    }
    url = microsoft_identity.authorization_url(
        config, state=state, nonce=nonce, code_verifier=code_verifier,
        select_account=purpose == "link",
    )
    return RedirectResponse(url=url, status_code=302)


@router.get("/auth/microsoft/start")
async def microsoft_start(request: Request, config: MicrosoftSignInConfig = _CONFIG):
    """Begin a Microsoft sign-in from the /login button."""
    if request.session.get("user_id"):
        return RedirectResponse(url="/", status_code=302)
    return _start(request, config, purpose="signin")


@router.get("/auth/microsoft/link")
async def microsoft_link_start(request: Request, config: MicrosoftSignInConfig = _CONFIG,
                               current_user: User = _USER):
    """Begin linking from Settings: staff only, not impersonating, ORCID sign-in ≤ 15 min old."""
    refuse_impersonation(current_user, _IMPERSONATION_DETAIL)
    if not may_use_microsoft_sign_in(current_user):
        raise HTTPException(status_code=403, detail="Microsoft sign-in is for staff accounts.")
    if not fresh_orcid_sign_in(request.session):
        flash(request, MICROSOFT_ERROR_MESSAGES["microsoft_link_refused"], "error")
        return RedirectResponse(url="/settings", status_code=302)
    return _start(request, config, purpose="link", user_id=str(current_user.id))


@router.get("/auth/microsoft/callback")
async def microsoft_callback(
    request: Request,
    config: MicrosoftSignInConfig = _CONFIG,
    db: AsyncSession = _DB,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    """Microsoft's redirect back, for a sign-in or a link (the flow says which)."""
    flow = request.session.pop(FLOW_KEY, None)
    if not isinstance(flow, dict):
        return _fail(request, None, "state_mismatch")
    purpose = flow.get("purpose")
    if error:
        logger.warning("Microsoft sign-in error=%s description=%s", error[:80],
                       request.query_params.get("error_description", "")[:300])
        return _fail(request, purpose, "microsoft_cancelled" if error == "access_denied" else "microsoft_error")
    started_at = flow.get("started_at")
    if (not code or not state or state != flow.get("state")
            or not isinstance(started_at, int) or isinstance(started_at, bool)
            or int(time.time()) - started_at > FLOW_MAX_AGE_SECONDS):
        return _fail(request, purpose, "state_mismatch")
    try:
        tokens = await microsoft_identity.exchange_code(
            config, code=code, code_verifier=str(flow.get("code_verifier", "")))
        identity = await microsoft_identity.validate_id_token(
            config, tokens.get("id_token"), nonce=str(flow.get("nonce", "")), now=int(time.time()))
    except InteractionRequired:
        return _fail(request, purpose, "microsoft_interaction_required")
    except (MicrosoftSignInError, IdTokenError) as exc:
        logger.warning("Microsoft sign-in failed: %s", exc)
        return _fail(request, purpose, "microsoft_failed")
    if purpose == "link":
        return await _finish_link(request, db, flow, identity)
    return await _finish_sign_in(request, db, identity)


async def _finish_sign_in(request: Request, db: AsyncSession,
                          identity: MicrosoftIdentity) -> RedirectResponse:
    user = await microsoft_identity.find_linked_user(db, identity)
    if user is None:
        logger.info("Microsoft sign-in refused: no linked account (tid=%s)", identity.tenant_id)
        return _fail(request, "signin", "microsoft_not_linked")
    if not may_use_microsoft_sign_in(user):
        logger.info("Microsoft sign-in refused: user %s has role %s", user.id, user.user_role)
        return _fail(request, "signin", "microsoft_not_staff")
    await microsoft_identity.record_sign_in(db, user.id)
    return await complete_sign_in(request, db, user, via=VIA_MICROSOFT)


async def _finish_link(request: Request, db: AsyncSession, flow: dict,
                       identity: MicrosoftIdentity) -> RedirectResponse:
    """Re-check everything /auth/microsoft/link checked, against the session now."""
    current_user = await get_current_user(request, db)  # 302s a stale or revoked session
    refuse_impersonation(current_user, _IMPERSONATION_DETAIL)
    if (request.session.get(IMPERSONATE_KEY) or str(current_user.id) != flow.get("user_id")
            or not may_use_microsoft_sign_in(current_user)
            or not fresh_orcid_sign_in(request.session)):
        return _fail(request, "link", "microsoft_link_refused")
    user_id = current_user.id
    refusal = await microsoft_identity.link_account(db, user_id, identity)
    if refusal is not None:
        logger.info("Microsoft link refused for user %s: %s", user_id, refusal)
        return _fail(request, "link", refusal)
    await db.commit()
    logger.info("User %s linked a Microsoft sign-in (tid=%s)", user_id, identity.tenant_id)
    flash(request, "Microsoft sign-in linked. You can now use it on the sign-in page.", "success")
    return RedirectResponse(url="/settings", status_code=302)
```

- [ ] **Step 4: Wire it in `src/main.py`.** Add `microsoft_auth,` to the `from src.routers import (...)`
  tuple (between `manager,` and `onboarding,`) and
  `from src.services.microsoft_identity import load_config as load_microsoft_config` to the imports.
  In `create_app()`, after `application.state.assessment_chat_enabled = …` add:

```python
    # Microsoft sign-in for staff (spec 2026-10-13 §5.2, S1). Validated here, in the web
    # process only: a bad MICROSOFT_* value stops the web app at start (MicrosoftConfigError)
    # but never the worker or the agent, which load the same .env. None = off.
    application.state.microsoft_sign_in = load_microsoft_config(settings)
```

  and after `application.include_router(auth.router, tags=["auth"])` add
  `application.include_router(microsoft_auth.router, tags=["auth"])`.

- [ ] **Step 5: Test plumbing.** In `tests/conftest.py::asgi_app`, right after `app = create_app()`:

```python
    # Microsoft sign-in off unless a test turns it on: create_app() reads the host's .env, and
    # no test may depend on it (spec 2026-10-13 §8).
    app.state.microsoft_sign_in = None
```

  In `tests/unit/test_reachability.py::ROUTE_ALLOWLIST` after the `("GET", "/auth/callback")` entry:

```python
    ("GET", "/auth/microsoft/callback"): (
        "Microsoft sign-in redirect_uri — the caller is login.microsoftonline.com. The value "
        "registered with Microsoft is settings.microsoft_redirect_uri (src/config.py), not a "
        "link in this app."
    ),
```

  In `tests/integration/test_rendered_page_gate.py::SKIPPED` add:

```python
    "/auth/microsoft/start": "redirects to Microsoft; 404 while the feature is off",
    "/auth/microsoft/link": "redirects to Microsoft or /settings; 404 while the feature is off",
    "/auth/microsoft/callback": "Microsoft OAuth redirect target; redirects, renders no page",
```

- [ ] **Step 6: Run to verify they pass** — host:
  `.venv-test/bin/python -m pytest tests/integration/test_microsoft_sign_in.py tests/unit/test_reachability.py tests/integration/test_rendered_page_gate.py tests/integration/test_login_page.py tests/integration/test_origin_guard.py tests/unit/test_security_headers.py -q`.
  Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/routers/microsoft_auth.py src/main.py tests/conftest.py tests/unit/test_reachability.py tests/integration/test_rendered_page_gate.py tests/integration/test_microsoft_sign_in.py
git commit -m "feat(microsoft): sign-in and link routes on the organizations endpoints"
```

---

### Task 8: Settings section and user unlink

**Files:**
- Modify: `src/routers/settings.py`
- Modify: `templates/settings.html`
- Modify: `tests/integration/test_settings_page.py` (module docstring only)
- Test: `tests/integration/test_microsoft_settings.py`

**Interfaces:**
- Consumes: Task 4 `link_view`, `unlink`, `may_use_microsoft_sign_in`; Task 6 `fresh_orcid_sign_in`;
  `src.services.session_epoch.bump_session_epoch`, `epoch_in_session`.
- Produces: `POST /settings/microsoft/unlink`; template variable `microsoft: dict | None`
  (`{"link": link_view-dict | None, "can_link": bool}`).

- [ ] **Step 1: Write the failing tests** `tests/integration/test_microsoft_settings.py`:

```python
"""The Settings section and the user's own unlink (spec 2026-10-13 §5.3, §5.7, D11)."""
import time
import uuid

import pytest
from sqlalchemy import func, select

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_PI, User, UserMicrosoftIdentity
from tests import factories
from tests.oidc_fakes import OID, TENANT, make_config
from tests.session_support import session_from_response, session_headers

pytestmark = pytest.mark.integration


@pytest.fixture
def on(asgi_app):
    asgi_app.state.microsoft_sign_in = make_config()


async def _user(db_session, role=USER_ROLE_MANAGER):
    return await factories.make_user(db_session, user_role=role)


async def _link(db_session, user):
    db_session.add(UserMicrosoftIdentity(user_id=user.id, tenant_id=uuid.UUID(TENANT),
                                         object_id=uuid.UUID(OID), sign_in_name="staff@example.edu"))
    await db_session.flush()


def _fresh(user, **extra):
    return session_headers(user.id, signed_in_at=int(time.time()) - 10, signed_in_via="orcid", **extra)


async def test_the_section_is_hidden_when_off_for_pis_and_while_impersonating(client, db_session, asgi_app):
    manager = await _user(db_session)
    assert "Microsoft sign-in" not in (await client.get("/settings", headers=_fresh(manager))).text
    asgi_app.state.microsoft_sign_in = make_config()
    pi = await _user(db_session, USER_ROLE_PI)
    assert "Microsoft sign-in" not in (await client.get("/settings", headers=_fresh(pi))).text
    admin = await _user(db_session, USER_ROLE_ADMIN)
    r = await client.get("/settings", headers=_fresh(admin, impersonate=manager.id))
    assert "Microsoft sign-in" not in r.text


async def test_a_fresh_orcid_session_sees_the_link_button(client, db_session, on):
    r = await client.get("/settings", headers=_fresh(await _user(db_session)))
    assert 'href="/auth/microsoft/link"' in r.text


async def test_a_pre_deploy_session_must_sign_in_again(client, db_session, on):
    user = await _user(db_session)
    r = await client.get("/settings", headers=session_headers(user.id))
    assert 'href="/login/start?reauth=1"' in r.text
    assert 'href="/auth/microsoft/link"' not in r.text
    r = await client.get("/auth/microsoft/link", headers=session_headers(user.id))
    assert r.headers["location"] == "/settings"


async def test_a_linked_account_shows_its_name_and_the_unlink_form(client, db_session, on):
    user = await _user(db_session)
    await _link(db_session, user)
    r = await client.get("/settings", headers=_fresh(user))
    assert "staff@example.edu" in r.text
    assert 'action="/settings/microsoft/unlink"' in r.text


async def test_unlink_removes_the_link_and_ends_every_session(client, db_session, on):
    user = await _user(db_session)
    await _link(db_session, user)
    r = await client.post("/settings/microsoft/unlink", headers=session_headers(user.id))
    assert r.status_code == 302 and r.headers["location"] == "/login"
    session = session_from_response(r)
    assert "user_id" not in session and session["_flashes"][0]["kind"] == "success"
    assert await db_session.scalar(select(func.count()).select_from(UserMicrosoftIdentity)) == 0
    refreshed = await db_session.get(User, user.id)
    await db_session.refresh(refreshed)
    assert refreshed.session_epoch == 1
    old = await client.get("/settings", headers=session_headers(user.id))  # epoch 0: revoked
    assert old.status_code == 302 and old.headers["location"].startswith("/login")


async def test_unlink_ends_the_session_at_any_epoch(client, db_session, on):
    user = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER, session_epoch=3)
    await _link(db_session, user)
    r = await client.post("/settings/microsoft/unlink", headers=session_headers(user.id, epoch=3))
    assert r.headers["location"] == "/login"
    assert "user_id" not in session_from_response(r)


async def test_unlink_with_nothing_linked_changes_nothing(client, db_session, on):
    user = await _user(db_session)
    r = await client.post("/settings/microsoft/unlink", headers=session_headers(user.id))
    assert r.headers["location"] == "/settings"
    refreshed = await db_session.get(User, user.id)
    await db_session.refresh(refreshed)
    assert refreshed.session_epoch in (None, 0)


async def test_unlink_is_404_when_off_and_403_while_impersonating(client, db_session, asgi_app):
    user = await _user(db_session)
    assert (await client.post("/settings/microsoft/unlink", headers=session_headers(user.id))).status_code == 404
    asgi_app.state.microsoft_sign_in = make_config()
    admin = await _user(db_session, USER_ROLE_ADMIN)
    r = await client.post("/settings/microsoft/unlink", headers=session_headers(admin.id, impersonate=user.id))
    assert r.status_code == 403
```

- [ ] **Step 2: Run to verify they fail** — host: `.venv-test/bin/python -m pytest tests/integration/test_microsoft_settings.py -q`.
  Expected: FAIL (no section; unlink route 404/405).

- [ ] **Step 3: Implement** — replace `src/routers/settings.py` with:

```python
"""User settings router — the account page, and the Microsoft sign-in section for staff
(spec docs/specs/2026-10-13-microsoft-staff-sign-in-design.md §5.3, §5.7).

The email-notification preferences and the unsubscribe links were retired with
PI notification email. Their tables stay.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_current_user, refuse_impersonation
from src.models import User
from src.services.microsoft_identity import link_view, may_use_microsoft_sign_in, unlink
from src.services.session_epoch import bump_session_epoch, epoch_in_session
from src.web.flash import flash
from src.web.page_context import page_context
from src.web.sign_in_session import fresh_orcid_sign_in
from src.web.templating import make_templates

router = APIRouter()
templates = make_templates()
_DB = Depends(get_db)
_USER = Depends(get_current_user)


def _template_context(request: Request, user: User, **kwargs) -> dict:
    return page_context(request, user, active_page="settings", user=user, **kwargs)


def _microsoft_on(request: Request) -> bool:
    return getattr(request.app.state, "microsoft_sign_in", None) is not None


async def _microsoft_section(request: Request, db: AsyncSession, user: User) -> dict | None:
    """What the Microsoft section shows; None hides it (feature off, role not eligible, or
    impersonating: the admin's own fresh sign-in must never count for someone else, S6)."""
    if (not _microsoft_on(request) or getattr(user, "_is_impersonated", False)
            or not may_use_microsoft_sign_in(user)):
        return None
    return {"link": await link_view(db, user.id), "can_link": fresh_orcid_sign_in(request.session)}


@router.get("", response_class=HTMLResponse)
async def settings_page(request: Request, current_user: User = _USER, db: AsyncSession = _DB):
    """User settings page: the account section, and Microsoft sign-in for staff."""
    return templates.TemplateResponse(
        request, "settings.html",
        _template_context(request, current_user,
                          microsoft=await _microsoft_section(request, db, current_user)),
    )


@router.post("/microsoft/unlink")
async def microsoft_unlink(request: Request, current_user: User = _USER, db: AsyncSession = _DB):
    """Remove the user's own Microsoft sign-in and end every session, this one included (D11)."""
    if not _microsoft_on(request):
        raise HTTPException(status_code=404, detail="Not Found")
    refuse_impersonation(current_user, "Microsoft sign-in changes are disabled while impersonating.")
    user_id = current_user.id
    if await unlink(db, user_id) is None:
        flash(request, "No Microsoft account is linked.", "info")
        return RedirectResponse(url="/settings", status_code=302)
    await bump_session_epoch(db, user_id, expected_epoch=epoch_in_session(request.session))
    await db.commit()
    request.session.clear()
    flash(request, "Microsoft sign-in removed. Please sign in again.", "success")
    return RedirectResponse(url="/login", status_code=302)
```

Replace `templates/settings.html` content block's inner `<div class="max-w-2xl mx-auto">` children
so that, after the Account section `</div>`, it has:

```html
    {% if microsoft %}
    <!-- Microsoft sign-in (staff; spec 2026-10-13 §5.7) -->
    <div class="border-t border-gray-200 mt-8 pt-6" id="microsoft-sign-in">
        <h2 class="text-lg font-semibold text-gray-900 mb-2">Microsoft sign-in</h2>
        {% if microsoft.link %}
        <p class="text-sm text-gray-700 mb-1">
            Linked to <strong>{{ microsoft.link.sign_in_name or "your Microsoft account" }}</strong>
            on {{ microsoft.link.linked_at | ts("date") }}.
        </p>
        <p class="text-sm text-gray-500 mb-3">
            Last used: {{ microsoft.link.last_sign_in_at | ts("date") if microsoft.link.last_sign_in_at else "never" }}.
        </p>
        <form method="POST" action="/settings/microsoft/unlink"
              data-confirm="Remove Microsoft sign-in? You will be signed out everywhere.">
            <button type="submit" class="text-sm text-red-600 hover:text-red-700">Unlink Microsoft account</button>
        </form>
        {% elif microsoft.can_link %}
        <p class="text-sm text-gray-600 mb-3">
            Use your organisation's Microsoft account as a second way to sign in. ORCID keeps working.
        </p>
        <a href="/auth/microsoft/link" class="text-sm text-indigo-600 hover:underline">Link Microsoft account</a>
        {% else %}
        <p class="text-sm text-gray-600 mb-3">To link a Microsoft account, first confirm it's you.</p>
        <a href="/login/start?reauth=1" class="text-sm text-indigo-600 hover:underline">Sign in with ORCID again to link</a>
        {% endif %}
    </div>
    {% endif %}
```

Change the docstring of `tests/integration/test_settings_page.py` to
`"""/settings is the account page (plus the Microsoft section for staff, tested in test_microsoft_settings.py)."""`.

- [ ] **Step 4: Run to verify they pass** — host:
  `.venv-test/bin/python -m pytest tests/integration/test_microsoft_settings.py tests/integration/test_settings_page.py -q`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/routers/settings.py templates/settings.html tests/integration/test_settings_page.py tests/integration/test_microsoft_settings.py
git commit -m "feat(microsoft): Settings section and unlink that ends every session"
```

---

### Task 9: Admin view and removal

**Files:**
- Modify: `src/routers/admin/users.py` (detail context ~95-107; new route after `admin_verify_user_email`)
- Create: `templates/admin/_microsoft_link.html`
- Modify: `templates/admin/user_detail.html` (include after the "Account type" card, ~line 100)
- Modify: `templates/workspace/pi_detail.html` (include inside `{% if permissions.admin and not identity.is_impersonating %}`, after the Account Type card)
- Modify: `src/routers/workspace/pi_directory.py` (`workspace_pi_detail` context)
- Modify: `src/web/presentation.py` (`VIEW_FIELDS`, `ADMIN_KEYS`)
- Modify: `tests/unit/test_admin_route_table.py`
- Test: `tests/integration/test_microsoft_admin.py`

**Interfaces:**
- Consumes: Task 4 `link_view`, `unlink`; `bump_session_epoch`; `AdminAuditEvent`.
- Produces: `POST /admin/users/{user_id}/microsoft/unlink`; template variable `microsoft_link: dict | None`.

- [ ] **Step 1: Write the failing tests.** In `tests/unit/test_admin_route_table.py` add
  `("POST", "/users/{user_id}/microsoft/unlink"),` after the verify-email entry and change
  `assert len(_routes()) == 45` to `46`. Create `tests/integration/test_microsoft_admin.py`:

```python
"""Admins see and remove a user's Microsoft sign-in (spec 2026-10-13 §5.3, §5.7, S5)."""
import uuid

import pytest
from sqlalchemy import func, select

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    AdminAuditEvent,
    User,
    UserMicrosoftIdentity,
)
from tests import factories
from tests.oidc_fakes import OID, TENANT, make_config
from tests.session_support import session_headers

pytestmark = pytest.mark.integration


@pytest.fixture
def on(asgi_app):
    asgi_app.state.microsoft_sign_in = make_config()


async def _linked_user(db_session, role=USER_ROLE_MANAGER):
    user = await factories.make_user(db_session, user_role=role)
    db_session.add(UserMicrosoftIdentity(user_id=user.id, tenant_id=uuid.UUID(TENANT),
                                         object_id=uuid.UUID(OID), sign_in_name="staff@example.edu"))
    await db_session.flush()
    return user


async def _admin(db_session):
    return await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)


async def test_an_admin_removes_a_link_and_ends_the_users_sessions(client, db_session, on):
    admin, user = await _admin(db_session), await _linked_user(db_session)
    r = await client.post(f"/admin/users/{user.id}/microsoft/unlink", headers=session_headers(admin.id))
    assert r.status_code == 302 and r.headers["location"] == f"/admin/users/{user.id}"
    assert await db_session.scalar(select(func.count()).select_from(UserMicrosoftIdentity)) == 0
    refreshed = await db_session.get(User, user.id)
    await db_session.refresh(refreshed)
    assert refreshed.session_epoch == 1
    event = await db_session.scalar(select(AdminAuditEvent).where(AdminAuditEvent.action == "microsoft_unlink"))
    assert event.actor_user_id == admin.id
    assert event.payload == {"user_id": str(user.id), "tenant_id": TENANT, "sign_in_name": "staff@example.edu"}


async def test_removing_a_missing_link_writes_no_audit_row(client, db_session, on):
    admin = await _admin(db_session)
    user = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    r = await client.post(f"/admin/users/{user.id}/microsoft/unlink", headers=session_headers(admin.id))
    assert r.status_code == 302
    assert await db_session.scalar(select(func.count()).select_from(AdminAuditEvent)
                                   .where(AdminAuditEvent.action == "microsoft_unlink")) == 0


async def test_managers_impersonation_and_feature_off_are_refused(client, db_session, asgi_app):
    admin, user = await _admin(db_session), await _linked_user(db_session)
    path = f"/admin/users/{user.id}/microsoft/unlink"
    assert (await client.post(path, headers=session_headers(admin.id))).status_code == 404
    asgi_app.state.microsoft_sign_in = make_config()
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    assert (await client.post(path, headers=session_headers(manager.id))).status_code == 403
    r = await client.post(path, headers=session_headers(admin.id, impersonate=manager.id))
    assert r.status_code == 403


async def test_the_admin_user_page_shows_the_link(client, db_session, on):
    admin, user = await _admin(db_session), await _linked_user(db_session)
    r = await client.get(f"/admin/users/{user.id}", headers=session_headers(admin.id))
    assert "staff@example.edu" in r.text and "Remove Microsoft sign-in" in r.text


async def test_a_demoted_pis_link_is_removable_from_the_workspace_page(client, db_session, on):
    admin, pi = await _admin(db_session), await _linked_user(db_session, USER_ROLE_PI)
    await factories.make_profile(db_session, user=pi)
    r = await client.get(f"/workspace/pis/{pi.id}", headers=session_headers(admin.id))
    assert r.status_code == 200 and "Remove Microsoft sign-in" in r.text
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    r = await client.get(f"/workspace/pis/{pi.id}", headers=session_headers(manager.id))
    assert "Remove Microsoft sign-in" not in r.text and "staff@example.edu" not in r.text
```

- [ ] **Step 2: Run to verify they fail** — host:
  `.venv-test/bin/python -m pytest tests/integration/test_microsoft_admin.py tests/unit/test_admin_route_table.py -q`. Expected: FAIL.

- [ ] **Step 3: Implement the route** in `src/routers/admin/users.py`. Imports: add `Request` is
  already imported; add `from src.models import AdminAuditEvent` (extend the existing `from src.models import …`),
  `from src.services.microsoft_identity import link_view, unlink`,
  `from src.services.session_epoch import bump_session_epoch`. In `admin_user_detail`, add to the
  `_template_context(...)` call:
  `microsoft_link=(await link_view(db, user_id)) if getattr(request.app.state, "microsoft_sign_in", None) else None,`.
  Append:

```python
@router.post("/users/{user_id}/microsoft/unlink")
async def admin_remove_microsoft_link(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _ADMIN,
):
    """Remove a user's linked Microsoft sign-in and end all their sessions (spec 2026-10-13
    §5.3). Admins only; refused under impersonation, because the audit row names the admin.
    An admin removing their own link signs themselves out too (S5)."""
    if getattr(request.app.state, "microsoft_sign_in", None) is None:
        raise HTTPException(status_code=404, detail="Not Found")
    refuse_impersonation(current_user, "Microsoft sign-in changes are disabled while impersonating.")
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    actor_id = current_user.id
    removed = await unlink(db, user_id)
    if removed is not None:
        await bump_session_epoch(db, user_id)
        db.add(AdminAuditEvent(
            action="microsoft_unlink", actor_user_id=actor_id,
            payload={"user_id": str(user_id), "tenant_id": removed.tenant_id,
                     "sign_in_name": removed.sign_in_name},
        ))
        await db.commit()
        logger.info("Admin %s removed the Microsoft sign-in of user %s", actor_id, user_id)
    return RedirectResponse(url=f"/admin/users/{user_id}", status_code=302)
```

- [ ] **Step 4: The partial** `templates/admin/_microsoft_link.html`:

```html
{# A user's linked Microsoft sign-in, for admins (spec 2026-10-13 §5.7). Expects target_user and
   microsoft_link (src/services/microsoft_identity.py::link_view, or None). #}
{% if microsoft_link %}
<div class="bg-white rounded-xl shadow-sm border border-gray-200 p-6 mb-6" id="microsoft-sign-in">
    <h2 class="font-semibold text-gray-800 mb-2">Microsoft sign-in</h2>
    <dl class="grid grid-cols-2 gap-4 text-sm mb-4">
        <div><dt class="text-gray-500">Signed in as</dt><dd class="font-medium">{{ microsoft_link.sign_in_name or '—' }}</dd></div>
        <div><dt class="text-gray-500">Tenant ID</dt><dd class="font-medium font-mono text-xs">{{ microsoft_link.tenant_id }}</dd></div>
        <div><dt class="text-gray-500">Linked</dt><dd class="font-medium">{{ microsoft_link.linked_at | ts("date") }}</dd></div>
        <div><dt class="text-gray-500">Last used</dt><dd class="font-medium">{{ microsoft_link.last_sign_in_at | ts("date") if microsoft_link.last_sign_in_at else 'never' }}</dd></div>
    </dl>
    <form method="POST" action="/admin/users/{{ target_user.id }}/microsoft/unlink"
          data-confirm="Remove {{ target_user.name }}'s Microsoft sign-in? They will be signed out everywhere.">
        <button type="submit"
                class="px-4 py-2 bg-red-600 text-white text-sm font-medium rounded-lg hover:bg-red-700">
            Remove Microsoft sign-in
        </button>
    </form>
</div>
{% endif %}
```

  In `templates/admin/user_detail.html` add `{% include "admin/_microsoft_link.html" %}` on the line
  after the Account type card's closing `</div>`. In `templates/workspace/pi_detail.html`, inside
  the `{% if permissions.admin and not identity.is_impersonating %}` section, add the same include
  after the Account Type card's closing `</div>` (before `<!-- Danger zone -->`).

- [ ] **Step 5: Workspace context and projection.** In `src/routers/workspace/pi_directory.py` add
  `from src.services.microsoft_identity import link_view` and, in `workspace_pi_detail`'s
  `workspace_context(...)` call, add
  `microsoft_link=(await link_view(db, user_id)) if current_user.is_admin and getattr(request.app.state, "microsoft_sign_in", None) else None,`.
  In `src/web/presentation.py`: in the `VIEW_FIELDS` string add the line
  `"last_sign_in_at linked_at microsoft_link sign_in_name tenant_id "` (anywhere inside the
  parenthesised string, ending with a space), and add `microsoft_link` to the `ADMIN_KEYS` string.

- [ ] **Step 6: Run to verify they pass** — host:
  `.venv-test/bin/python -m pytest tests/integration/test_microsoft_admin.py tests/unit/test_admin_route_table.py tests/unit/test_workspace_presentation.py tests/integration/test_workspace_data_contract.py tests/unit/test_reachability.py -q`. Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/routers/admin/users.py templates/admin/_microsoft_link.html templates/admin/user_detail.html templates/workspace/pi_detail.html src/routers/workspace/pi_directory.py src/web/presentation.py tests/unit/test_admin_route_table.py tests/integration/test_microsoft_admin.py
git commit -m "feat(microsoft): admins view and remove a user's Microsoft sign-in"
```

---

### Task 10: Operator documentation

**Files:**
- Modify: `docs/operations/pis-and-access.md` (new section after "Sessions")
- Modify: `CLAUDE.md` (one line; NOT committed — the file has unrelated uncommitted edits)

- [ ] **Step 1: Add the section** to `docs/operations/pis-and-access.md`, after the "**Sessions**" paragraph:

```markdown
**Microsoft sign-in for staff** (spec `docs/specs/2026-10-13-microsoft-staff-sign-in-design.md`,
migration `0066`). Admins, managers and reviewers can link a work or school Microsoft account in
Settings and then use "Sign in with Microsoft (staff)" on `/login`; ORCID keeps working and stays
the only way to create an account. Linking needs an ORCID re-sign-in (`prompt=login`, checked by
`auth_time`) under 15 minutes old. Accounts are matched by Microsoft's `(tid, oid)` only, never by
email; only members (`acct` 0) of the tenants in `MICROSOFT_ALLOWED_TENANT_IDS` get through. A
user's own Unlink signs them out everywhere; an admin removes a link from `/admin/users/{id}` (or the
workspace PI page for a `pi`), which also signs the user out and writes an `admin_audit_events` row
(`microsoft_unlink`). Disabling someone's Microsoft account does NOT end their Blackbird sessions or
their ORCID login: offboarding is still a role or access change here.

Off while `MICROSOFT_CLIENT_ID` is empty. A missing or invalid `MICROSOFT_*` value stops
`blackbird-app` at start (the worker and agent are unaffected); clear `MICROSOFT_CLIENT_ID` and
recreate to roll back. **Turning it on:**

1. Decide where the app registration lives (spec D4, §7.2). In an organisation that lets users
   consent only to verified publishers or apps registered in its own tenant, its admin must approve
   the app once.
2. Entra admin center → App registrations → New registration: "Multiple Entra ID tenants", platform
   Web, redirect URI `https://blackbird.copi.science/auth/microsoft/callback` only (development uses a
   separate registration). Leave implicit grant off; confirm app instance property lock is on.
   Token configuration → Add optional claim → ID → `acct`.
3. `openssl req -x509 -newkey rsa:3072 -sha256 -days 365 -nodes -keyout key.pem -out cert.pem -subj "/CN=blackbird-microsoft-signin"`;
   upload `cert.pem` under Certificates & secrets → Certificates.
4. In `.env`: `MICROSOFT_CLIENT_ID`, `MICROSOFT_REDIRECT_URI` (the URI above),
   `MICROSOFT_ALLOWED_TENANT_IDS` (comma-separated tenant GUIDs), `MICROSOFT_CERTIFICATE_B64`
   (`base64 -w0 cert.pem`), `MICROSOFT_PRIVATE_KEY_B64` (`base64 -w0 key.pem`); `chmod 600 .env`;
   delete `key.pem`. Every copy of `.env` (backups included) now carries the key.
5. `$DC up -d --force-recreate blackbird-app`.
6. One live link and sign-in per allowed organisation; confirm the items in spec §9.
7. Calendar a reminder 30 days before the certificate expires (the app also logs a warning at
   start from 30 days out). To renew or move to a CA-signed certificate: upload the new one (a
   registration holds several), change the two `.env` values, recreate, delete the old one.
```

- [ ] **Step 2: CLAUDE.md** — under "## Access and accounts" add (leave uncommitted):

```markdown
- Microsoft sign-in is a second login for admins, managers and reviewers, linked to an existing
  ORCID account by `(tid, oid)` — never by email. Runbook and limits: `docs/operations/pis-and-access.md`.
```

- [ ] **Step 3: Commit** (docs only; not `CLAUDE.md`)

```bash
git add docs/operations/pis-and-access.md
git commit -m "docs(microsoft): staff Microsoft sign-in operations and turn-on runbook"
```

---

### Task 11: Integrated verification

**Files:** none new.

- [ ] **Step 1: Refresh the host test venv** (host only):
  `uv pip install --python .venv-test/bin/python -e '.[dev]'`. Expected: authlib ≥ 1.7.2,
  joserfc ≥ 1.7.4, cryptography ≥ 45.0.1 resolved.
- [ ] **Step 2: Lint the change** (host): `.venv-test/bin/ruff check src/services/id_tokens.py src/services/microsoft_identity.py src/services/orcid_reauth.py src/web/sign_in_session.py src/routers/microsoft_auth.py src/routers/auth.py src/routers/settings.py src/routers/admin/users.py src/routers/workspace/pi_directory.py src/main.py src/models/microsoft_identity.py tests/`.
  Expected: no findings (the `src/` ratchet ceiling must not rise).
- [ ] **Step 3: Full gate** (host): `./scripts/ci.sh`. Expected: alembic sanity (one head `0066`),
  CSS drift check, the `0066` upgrade→downgrade→upgrade round trip, ruff, and the full pytest run
  with the coverage floor all pass. Capture the exact failing command and output for any failure
  before repairing.
- [ ] **Step 4: Commit status check** — `git status --porcelain --untracked-files=all -- src templates static prompts alembic scripts pyproject.toml alembic.ini`
  prints nothing (everything the image needs is committed). `docker-compose.prod.yml`, `CLAUDE.md`
  and `docs/audits/open-findings.md` remain modified and uncommitted.
- [ ] **Step 5: Report** what changed, the commands run and their results, and the residual items:
  spec §9 live checks, the D4 decision, and that deploying (build, migrate `0066`, `up -d
  blackbird-app worker`) is the owner's call.
