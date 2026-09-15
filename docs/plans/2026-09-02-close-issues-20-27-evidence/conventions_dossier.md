# Conventions dossier — coPI.science @ copi-prod 18ba52c

Collected read-only on 2026-09-02. Every claim cites `file:line` in `/home/a/scripps/coPI.science`.
Line numbers are from `cat -n` / `sed -n` at this commit.

---

## A. Alembic

### A.1 alembic.ini + env.py — how the URL is obtained, async vs sync

- `alembic.ini:1-5`: `script_location = alembic`, `prepend_sys_path = .`, and a hard-coded fallback
  `sqlalchemy.url = postgresql+asyncpg://copi:copi@localhost:5432/copi`.
- `alembic/env.py:20-23` overrides that URL from the environment:
  ```python
  db_url = os.environ.get("DATABASE_URL")
  if db_url:
      config.set_main_option("sqlalchemy.url", db_url)
  ```
  `DATABASE_URL` is therefore the only knob tests/CI/prod use (`tests/conftest.py:65`, `scripts/ci.sh:229-231`).
- **Async.** `alembic/env.py:8` imports `async_engine_from_config`; `run_async_migrations()`
  (`env.py:99-111`) builds an async engine with `poolclass=pool.NullPool` and runs
  `await connection.run_sync(do_run_migrations)`; `run_migrations_online()` (`env.py:114-115`)
  does `asyncio.run(run_async_migrations())`.
- `target_metadata = Base.metadata` (`env.py:29`), populated by `import src.models  # noqa: F401`
  (`env.py:14-15`).
- **One transaction for the whole chain.** `do_run_migrations` (`env.py:93-96`) calls
  `context.configure(connection=connection, target_metadata=target_metadata)` with NO
  `transaction_per_migration`; the header comment at `env.py:53-72` explains this is deliberate.
- **lock_timeout is a connect-time server setting**, not a `SET` statement:
  `LOCK_TIMEOUT_MS = os.environ.get("ALEMBIC_LOCK_TIMEOUT_MS", "10000")` (`env.py:73`), applied as
  `kwargs["connect_args"] = {"server_settings": {"lock_timeout": str(int(LOCK_TIMEOUT_MS))}}`
  (`env.py:102-107`). The comment at `env.py:76-92` records the failure mode of doing it as a SQL
  statement before `begin_transaction()` (whole chain silently rolls back, no `alembic_version`).

### A.2 Naming convention for constraints/indexes

**None.** `src/database.py:11-12` is:
```python
class Base(DeclarativeBase):
    pass
```
`grep -rn "naming_convention\|MetaData(" src alembic --include='*.py'` returns nothing (exit 1).
Constraint/index names are hand-written in each migration. Observed naming style:
- unique constraints: `uq_<table>_<cols>` — `uq_agent_messages_run_ts` (`0019:53`),
  `uq_cohort_membership_cohort_agent` (`0022:78`)
- indexes: `ix_<table-ish>_<cols>` — `ix_agent_messages_run_posted` (`0019:56`),
  `ix_pi_dm_run_agent_posted` (`0020:53`), `ix_cohort_memberships_cohort_id` (`0022:82`)
- enums: `<name>_enum` — `pi_dm_direction_enum` (`0020:39`)
- CHECK constraints: `pcm_exactly_one_of_agent_or_user` (asserted at
  `tests/integration/test_db_contract.py:281`)

### A.3 Engine construction (for reference to the app side)

`src/database.py:15-22`:
```python
def _get_engine():
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        echo=False,
        pool_size=5,
        max_overflow=10,
    )
```
Lazy singletons `get_engine()` / `get_session_factory()` (`src/database.py:29-43`), the factory
is `async_sessionmaker(get_engine(), class_=AsyncSession, expire_on_commit=False)`.

### A.4 Revision chain 0018 -> 0024 (single linear chain, no branches)

| rev | down | file | date in header |
|---|---|---|---|
| 0018 | 0017 | `0018_allowlist_email_hint.py` (:15-16) | 2026-06-29 |
| 0019 | 0018 | `0019_agent_message_content.py` (:19-20) | 2026-07-20 |
| 0020 | 0019 | `0020_pi_dm_messages.py` (:19-20) | 2026-07-20 |
| 0021 | 0020 | `0021_inbox_cursor_created_at_indexes.py` (:20-21) | 2026-07-25 |
| 0022 | 0021 | `0022_add_cohorts.py` (:25-26) | 2026-07-30 |
| 0023 | 0022 | `0023_profile_synthesis_provenance.py` (:38-39) | 2026-07-31 |
| 0024 | 0023 | `0024_add_agent_role.py` (:21-22) | 2026-08-05 |

Current head: **0024**. Revision ids are 4-digit zero-padded strings; the filename is
`NNNN_snake_description.py`. Rule from `0022:7-12`: "Revision ids are assigned at merge, never at branch."

### A.5 Head-revision pins a new migration (0025) MUST update

These hard-code "0024" and will fail or mislead if a 0025 lands without touching them:

- `tests/integration/test_harness_smoke.py:7-15`:
  ```python
  async def test_container_is_migrated(engine):
      async with engine.connect() as conn:
          v = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
          # Head-revision pin: bump it deliberately with each new migration. ...
          # 0019-0021 db-primary-conversations, 0022 cohorts,
          # 0023 researcher_profiles synthesis provenance, 0024 agents.role column
          assert v == "0024"
  ```
- `scripts/migrate/preflight.py:74` `DEFAULT_TARGET = "0024"`; `:206`
  `REVISION_ORDER = ("0018", "0019", "0020", "0021", "0022", "0023", "0024")`; `:202-204`
  `PlannedObject("0024", "column", "role", "agents"),` closes the `PLANNED_OBJECTS` tuple.
- `scripts/migrate/run_migration.sh:56` `TARGET="0024"`.
- `tests/unit/test_migration_checks.py:232` `assert pf.DEFAULT_TARGET == "0024"`; `:1129` and
  `:1170` `assert args.target == "0024"`; `:838` the drift guard loops
  `for revision in ("0019", "0020", "0021", "0022", "0023", "0024"):` and requires exactly one
  file per revision and every created index/table/column/constraint/enum in that file to be
  declared in `PLANNED_OBJECTS` (`:826-857`). Add 0025 to that tuple and to `PLANNED_OBJECTS`.
- `tests/unit/test_migration_checks.py:218-226` parametrize `revision_status(rev, "0023")` with
  "0024" expected to BLOCK; `:231` `SUPPORTED_START_REVISIONS == ("0018","0019","0020","0021","0023")`.
- `tests/unit/test_cohort_isolation.py:1296-1310` `test_head_is_the_expected_revision` only asserts
  `len(revs) >= 22` and `revs["0022"] == ["0022_add_cohorts.py"]` — a 0025 does NOT break it.
- `scripts/ci.sh:70` `MIGRATION_FLOOR="${MIGRATION_FLOOR:-0018}"` — the round trip downgrades to
  0018 and back, so a new migration's `downgrade()` IS exercised by the gate against an empty DB.

### A.6 Migration file template (most recent three, pasted in full)

**`alembic/versions/0022_add_cohorts.py`** (149 lines):
```python
"""Add cohorts, cohort_memberships and cohort_audit_events

Revision ID: 0022
Revises: 0021
Create Date: 2026-07-30 00:00:00.000000

Renumbered from 0019 at merge time. The cohort branch was cut before main's
db-primary work, so its original "0019" collided with 0019_agent_message_content:
two revisions sharing an id resolve to whichever file sorts last, which silently
skips the other while stamping the DB as fully migrated. Revision ids are assigned
at merge, never at branch. See .notes/cohort-system-v2.md §4.2 / §14 and the
alembic guard in scripts/ci.sh.

Downgrades are idempotent (if_exists) so a rollback cannot wedge on an object that
a partially-applied upgrade never created. See v2 §14.4.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A cohort is a named group of agents permitted to act on each other's
    # activity during simulation. See .notes/cohort-system-v2.md.
    op.create_table(
        "cohorts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=48), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_by",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # agent_id is the AgentRegistry slug (no FK — agent rows may not exist at
    # membership-creation time; the app validates at add time).
    op.create_table(
        "cohort_memberships",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "cohort_id",
            UUID(as_uuid=True),
            sa.ForeignKey("cohorts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_id", sa.String(length=50), nullable=False),
        sa.Column(
            "added_by",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "cohort_id", "agent_id", name="uq_cohort_membership_cohort_agent"
        ),
    )
    op.create_index(
        "ix_cohort_memberships_cohort_id", "cohort_memberships", ["cohort_id"]
    )
    op.create_index(
        "ix_cohort_memberships_agent_id", "cohort_memberships", ["agent_id"]
    )

    # Append-only audit trail. Deliberately denormalised: a cohort delete cascades
    # its memberships away and a user delete nulls the actor FK, so the trail must
    # not depend on either row surviving — hence cohort_name / actor_email columns
    # and NO FK on cohort_id. `topology` snapshots the full cohort->members map
    # plus the active gate settings at run start and on every change, so a
    # completed simulation run stays attributable to the configuration that
    # produced it (v2 §13.1).
    op.create_table(
        "cohort_audit_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("cohort_id", UUID(as_uuid=True), nullable=True),
        sa.Column("cohort_name", sa.String(length=48), nullable=False),
        sa.Column("agent_id", sa.String(length=50), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column(
            "actor_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("actor_email", sa.String(length=255), nullable=True),
        sa.Column("simulation_run_id", UUID(as_uuid=True), nullable=True),
        sa.Column("topology", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_cohort_audit_events_cohort_id", "cohort_audit_events", ["cohort_id"]
    )
    op.create_index(
        "ix_cohort_audit_events_created_at", "cohort_audit_events", ["created_at"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_cohort_audit_events_created_at",
        table_name="cohort_audit_events",
        if_exists=True,
    )
    op.drop_index(
        "ix_cohort_audit_events_cohort_id",
        table_name="cohort_audit_events",
        if_exists=True,
    )
    op.drop_table("cohort_audit_events", if_exists=True)
    op.drop_index(
        "ix_cohort_memberships_agent_id",
        table_name="cohort_memberships",
        if_exists=True,
    )
    op.drop_index(
        "ix_cohort_memberships_cohort_id",
        table_name="cohort_memberships",
        if_exists=True,
    )
    op.drop_table("cohort_memberships", if_exists=True)
    op.drop_table("cohorts", if_exists=True)
```

**`alembic/versions/0023_profile_synthesis_provenance.py`** (62 lines):
```python
"""Add synthesis-provenance columns to researcher_profiles

Revision ID: 0023
Revises: 0022
Create Date: 2026-07-31 00:00:00.000000

Two defects in src/services/profile_pipeline.py were invisible because the
pipeline wrote down nothing about *how* a profile was produced:

  1. Step 8 computed the validation result and step 9 stored on `if synthesized:`
     alone, so a profile that failed _validate_profile twice was persisted as if
     it had passed. `synthesis_validated` is the record of that decision.
  2. With PubMed unreachable, ORCID works never reach the synthesis prompt
     (_build_synthesis_context is fed only pubs_for_synthesis, which is derived
     solely from PubMed records), so the model invents a plausible profile from
     ~150 characters of name/department context and zero Publication rows are
     written. `evidence_pmid_count` / `evidence_pub_count` make that case
     self-identifying and separate it from a genuinely publication-less
     researcher (see ResearcherProfile.evidence_state).

All three are nullable and are deliberately NOT backfilled. NULL means "unknown
— this row predates the columns". Backfilling evidence_pub_count from
count(publications) would look like a free win and would be a lie: stored
publications accumulate across runs and include records with no abstract and
non-research article types, none of which reached any prompt. Inventing
provenance is exactly the failure these columns exist to expose.

Downgrades are idempotent (if_exists) so a rollback cannot wedge on a column a
partially-applied upgrade never created (see scripts/ci.sh and the 0022 note).
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "researcher_profiles",
        sa.Column("synthesis_validated", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "researcher_profiles",
        sa.Column("evidence_pmid_count", sa.Integer(), nullable=True),
    )
    op.add_column(
        "researcher_profiles",
        sa.Column("evidence_pub_count", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("researcher_profiles", "evidence_pub_count", if_exists=True)
    op.drop_column("researcher_profiles", "evidence_pmid_count", if_exists=True)
    op.drop_column("researcher_profiles", "synthesis_validated", if_exists=True)
```

**`alembic/versions/0024_add_agent_role.py`** (35 lines):
```python
"""Add role column to agents (per-role agent customization)

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-05 00:00:00.000000

`role` selects per-role prompt overrides (prompts/roles/{role}/) and a per-role
tool allow-list. Default 'pi_lab' == the pre-existing all-agents-identical
behaviour, so this column is a no-op until an agent is explicitly reassigned.
See docs/specs/2026-08-05-hub-bot-customization-design.md.

Downgrade is idempotent (if_exists) per the branch convention (0022/0023).
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("role", sa.String(length=20), nullable=False, server_default="pi_lab"),
    )


def downgrade() -> None:
    op.drop_column("agents", "role", if_exists=True)
```

Template summary: module docstring = one-line title, blank, `Revision ID:` / `Revises:` /
`Create Date: YYYY-MM-DD 00:00:00.000000`, blank, then a long **why** paragraph (0021-0024 all do
this; the docstring is where the rationale lives, not inline). Imports: `from typing import
Sequence, Union`, `import sqlalchemy as sa`, `from alembic import op` (postgres types via
`from sqlalchemy.dialects.postgresql import UUID` or `from sqlalchemy.dialects import postgresql`).
Typed module attrs `revision: str`, `down_revision: Union[str, None]`, `branch_labels`, `depends_on`.
`def upgrade() -> None:` / `def downgrade() -> None:`. Since 0022, every `op.drop_*` in
`downgrade()` carries `if_exists=True` — `tests/unit/test_cohort_isolation.py:1287-1294` pins this
for 0022 (`downgrade.count("if_exists=True") == len(drops)`); 0023 and 0024 follow by convention.
Note `scripts/ci.sh:62-69` records that 0019/0020/0021 *lack* the guards.

### A.7 Data migrations — prior art for `op.execute` / row fixes

Only three migrations touch rows; none since 0012.

- **`alembic/versions/0010_access_gate_and_waitlist.py:42-50`** — add a NOT NULL column with a
  server default, backfill existing rows, then drop the server default:
  ```python
  def upgrade() -> None:
      # 1. Add access_status to users, default 'pending', backfill existing rows to 'allowed'
      op.add_column(
          "users",
          sa.Column("access_status", sa.String(20), nullable=False, server_default="pending"),
      )
      op.execute("UPDATE users SET access_status = 'allowed'")
      # Drop the server default so new inserts rely on the model default
      op.alter_column("users", "access_status", server_default=None)
  ```
- **`0010:89-103`** — seed rows with `sa.table(...)` + `op.bulk_insert(...)` from a module-level
  `PILOT_ORCIDS` list.
- **`alembic/versions/0012_grantbot_posted_foas.py:37-53`** — parametrised INSERT through the bind:
  ```python
  if numbers:
      bind = op.get_bind()
      bind.execute(
          sa.text(
              "INSERT INTO grantbot_posted_foas (foa_number) "
              "VALUES (:n) ON CONFLICT (foa_number) DO NOTHING"
          ),
          [{"n": n} for n in numbers if n],
      )
  ```
- All other `op.execute` uses are `DROP TYPE IF EXISTS <enum>` in downgrades (`0001:269-274`,
  `0003:66,103`); 0020 uses `sa.Enum(name=...).drop(op.get_bind(), checkfirst=True)` (`0020:66`).
- Recent precedent for a **one-shot data repair done OUTSIDE alembic**: commit `18ba52c` body says
  the grantbot NULL-agent_id backlog "is repaired by a one-shot UPDATE at rollout (documented in
  the start() comment)" — see `src/agent/simulation.py:570-575`.

### A.8 Is `downgrade()` real in 0019-0024?

Yes, all six are real inverses (none is `pass`):
- 0019 (`:73-85`): drops 3 indexes, the unique constraint, re-NOT-NULLs `agent_id`, drops 7 columns. No `if_exists`.
- 0020 (`:62-66`): drops 2 indexes, table, and the enum type with `checkfirst=True`. No `if_exists`.
- 0021 (`:37-39`): drops 2 indexes. No `if_exists`.
- 0022 (`:126-149`): drops 4 indexes + 3 tables, every call `if_exists=True`.
- 0023 (`:59-62`): drops 3 columns, `if_exists=True`.
- 0024 (`:34-35`): drops 1 column, `if_exists=True`.

---

## B. Tests

### B.1 `tests/conftest.py` — fixtures (351 lines)

| fixture | scope | what it provides | lines |
|---|---|---|---|
| `_pg_container` | session | `None` if `TEST_DATABASE_URL` is set, else a `PostgresContainer("postgres:15", dbname="copi_test")` published on `127.0.0.1` only (`pg.ports["5432"] = ("127.0.0.1", None)`) | 28-45 |
| `pg_url` | session | asyncpg DSN — `TEST_DATABASE_URL` verbatim, else built from the container | 48-58 |
| `_migrated` | session | runs `<venv>/alembic upgrade head` as a subprocess with `DATABASE_URL=pg_url`, asserts rc==0; returns the URL. "NOT create_all — that omits migration-only indexes/constraints" (`:5-6`) | 61-74 |
| `engine` | session | `create_async_engine(_migrated, future=True, poolclass=NullPool)`; NullPool because pytest-asyncio uses a per-test loop | 77-85 |
| `db_session` | function (async) | `engine.connect()` + outer `conn.begin()`; `AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")` so route `commit()`s become savepoint releases; outer txn rolled back in `finally` | 88-103 |
| `client` | function (async) | `create_app()`; `app.dependency_overrides[get_db]` yields `db_session`; ALSO `monkeypatch.setattr("src.main.get_session_factory", lambda: badge_factory)` because `AgentBadgeMiddleware` bypasses `get_db`; `httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")` | 106-132 |
| `_text` | function | returns `sqlalchemy.text` | 135-137 |
| `pytest_collection_modifyitems` | hook | skips `live_slack` items unless `SLACK_TEST_WORKSPACE`, `SLACK_TEST_PI_USER_ID`, `SLACK_TEST_BOT_TOKEN_SU` are all set; skips `live_api` unless `LIVE_API_TESTS` | 144-166 |
| `slack_bot_tokens`, `slack_pi_user_id`, `slack_bot_tokens_all`, `slack_clients`, `slack_client_su`, `slack_list_all_channels`, `slack_probe_channel`, `api_budget` | session/function | live-tier only (real `AgentSlackClient` against a real workspace; `t-probe-*` channels archived on teardown; NCBI/ORCID rate budget) | 169-351 |

**No settings-override fixture exists.** Tests that need settings monkeypatch `get_settings` or
module attributes directly (e.g. `tests/unit/test_roster_sync.py:23-25`,
`tests/unit/test_slack_web.py:112` `monkeypatch.setattr(slack_web, "_BACKOFF_BASE", 0)`).
**No fake-Slack fixture exists in conftest** — tests import from `tests/fakes.py` directly.
There are no per-directory `conftest.py` files (`ls tests/*/conftest.py` -> no matches).

Exact fixture code for `db_session` and `client` (`tests/conftest.py:88-132`):
```python
@pytest_asyncio.fixture
async def db_session(engine):
    """Function-scoped session whose writes (and route code's commits) roll back after the test."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        session = AsyncSession(
            bind=conn,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",  # session.commit() -> savepoint release
        )
        try:
            yield session
        finally:
            await session.close()
            if trans.is_active:
                await trans.rollback()


@pytest_asyncio.fixture
async def client(db_session, engine, monkeypatch):
    from src.database import get_db
    from src.main import create_app

    badge_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("src.main.get_session_factory", lambda: badge_factory)

    app = create_app()

    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    app.dependency_overrides.clear()
```

### B.2 `tests/fakes.py` (256 lines) — API surface

- `FakeAnthropic(responses=None, *, default_text="OK")` (`:87-113`): `.messages.create(**kw)`
  records to `.calls` and pops scripted responses (str | `_Message` | callable(kwargs)). Install
  with `monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)` (`:8`).
  Helpers `text_response()`, `tool_use_response()`, `empty_response()` (`:57-75`).
- `FakeSlackClient(agent_id="agent1", bot_token="xoxb-fake")` (`:116-186`) — implements the
  Transport protocol the engine uses: `connect()->True`, `is_connected`, `bot_user_id` (`U_<id>`),
  `post_message(channel, text, thread_ts=None)->{"ts","channel"}` (records to `.posted`, applies
  `markdown_to_mrkdwn`), `send_dm(user_id, text)`, `poll_channel_messages(...)->[]`,
  `get_thread_replies(...)->[]`, `create_channel(name)->{"id": f"C_{name}", ...}`,
  `create_private_channel(name)->{"id": f"G_{name}", ...}`, `invite_to_channel(...)->True`
  (records `.invites`), `list_channels(include_private=False)->{}`, `_resolve_channel_id(name)`.
  Deterministic ts counter from `1_700_000_000` (`:130,143-145`).
- `RecordingSlackClient(responses=None, errors=None)` (`:189-222`) — stands in for the
  `slack_sdk.WebClient` INSIDE `AgentSlackClient`; `__getattr__` records `(method_name, kwargs)`
  into `.calls`; `errors[method]` is a list of exceptions popped one per call ("fail, then
  succeed"); `.calls_to(method)`.
- `_SlackResponse(data)` (`:225-240`) with `.data/.headers/.status_code`, `get/__getitem__/__contains__`.
- `slack_error(code, *, retry_after=None)` (`:243-256`) — builds a `SlackApiError` whose
  `response.headers["Retry-After"]` and `response.get("error")` are set the way
  `_call_with_retry` reads them.

### B.3 Directory layout and what distinguishes the tiers

```
tests/conftest.py  tests/factories.py  tests/fakes.py
tests/unit/            57 files, pytestmark in 3   (no DB, no Docker; SimulationEngine(agents, slack_clients={}))
tests/integration/     33 files, pytestmark in 32  (pytest.mark.integration; real Postgres via db_session/client/engine)
tests/characterization/ 4 files, pytestmark in 4   (pytest.mark.characterization; syrupy snapshots + client)
tests/contract/         3 files, pytestmark in 3   (pytest.mark.contract; respx-mocked external HTTP)
tests/e2e/              1 test file + helpers        (httpx replays of browser flows; no marker)
tests/live_api/         4 files                      (pytest.mark.live_api; skipped unless LIVE_API_TESTS=1)
```
Markers are declared in `pyproject.toml:73-80`:
```toml
markers = [
    "integration: needs a real Postgres (testcontainers) + Docker",
    "characterization: golden-master snapshot test",
    "contract: respx-mocked external HTTP",
    "real_llm: spends real Anthropic tokens; skipped unless ANTHROPIC_API_KEY is set",
    "live_slack: hits a real Slack workspace; needs SLACK_TEST_WORKSPACE=1 plus bot tokens in the environment",
    "live_api: calls a real third-party API (ORCID/NCBI/grants.gov); needs LIVE_API_TESTS=1",
]
```
Marker placement is a module-level `pytestmark = pytest.mark.integration` line (e.g.
`tests/integration/test_agent_page.py:49`, `tests/integration/test_health_route.py:3`); live tiers
stack: `pytestmark = [pytest.mark.integration, pytest.mark.live_slack]`. Distinguisher in practice:
unit tests never take `db_session`/`client`/`engine`; integration tests do. Markers are
informational except `live_slack`/`live_api`, which are skip-gated by `conftest.py:148-166`.
Docstring at `tests/conftest.py:3-7`: "Integration/characterization/contract tests require a real
Postgres (the bugs worth pinning only reproduce on PG, not SQLite)."

### B.4 Creating rows: `tests/factories.py` (async builders, not factory_boy)

Docstring `tests/factories.py:3-8`: plain async helpers; each fills NOT NULL columns, uses a
process-wide `itertools.count` for unique columns, applies `**overrides` last, `session.add` +
`await session.flush()`, returns the instance. Available: `make_user`, `make_profile`,
`make_agent`, `make_simulation_run`, `make_agent_channel`, `make_agent_message`,
`make_thread_decision`, `make_private_channel_member`, `make_llm_call_log`; plus the
`SES_PASS_HEADER` constant (`:30-32`).

**User** (`tests/factories.py:35-50`):
```python
async def make_user(session, **overrides) -> User:
    n = next(_counter)
    data = dict(
        name=f"Researcher {n}",
        orcid=f"0000-0000-0000-{n:04d}",
        email=f"user{n}@example.edu",
        institution="Test University",
        is_admin=False,
        onboarding_complete=True,
        access_status="allowed",
    )
    data.update(overrides)
    obj = User(**data)
    session.add(obj)
    await session.flush()
    return obj
```
**AgentRegistry** (`:72-86`): defaults `agent_id=f"agent{n}"`, `bot_name=f"Agent{n}Bot"`,
`pi_name=f"Researcher {n}"`, `status="active"`, optional `user=` sets `user_id`.
**ThreadDecision** (`:137-154`): defaults `thread_id=f"{n}.000100"`, `channel=f"channel-{n}"`,
`agent_a="agent1"`, `agent_b="agent2"`, `outcome="proposal"`; creates a `SimulationRun` if none given.

Concise in-test usage, User+Agent (`tests/integration/test_agent_page.py:192-197`):
```python
async def _agent_for(db, *, name, email, agent_id, bot_name, status="active"):
    user = await factories.make_user(db, name=name, email=email)
    agent = await factories.make_agent(
        db, user=user, agent_id=agent_id, bot_name=bot_name, pi_name=name, status=status
    )
    return user, agent
```
ThreadDecision (`tests/characterization/test_public_routes.py:118-125`):
```python
async def test_proposal_vote_happy_path_returns_id(client, db_session):
    d = await factories.make_thread_decision(
        db_session, outcome="proposal", origin_visibility="public"
    )
    body = {"decision_id": str(d.id), "vote": "up", "voter_token": "browser-tok-1"}
    r = await client.post("/api/proposal-vote", json=body)
    assert r.status_code == 200
    vote_id = r.json()["id"]
```
Admin user: `return await factories.make_user(db_session, is_admin=True, email="admin@example.org")`
(`tests/integration/test_admin_users.py:59`).

### B.5 How HTTP handlers are tested (httpx `AsyncClient` over ASGI + forged cookie)

There is no login fixture. Each integration module defines a local `_auth(user_id)` that forges
the starlette `SessionMiddleware` cookie with `itsdangerous.TimestampSigner`
(`tests/integration/test_agent_page.py:52-56`; identical copies at `test_admin_users.py:36-39`,
`test_cohort_admin.py:24-27`, `tests/characterization/test_auth_and_admin_routes.py:23-31`; a
shared version exists at `tests/e2e/session.py:24-36` as `forge_session_cookie`/`auth_headers`
but the integration tests do not import it):
```python
def _auth(user_id) -> dict:
    """Forge the signed session cookie SessionMiddleware would issue."""
    signer = TimestampSigner(get_settings().secret_key)
    data = base64.b64encode(json.dumps({"user_id": str(user_id)}).encode())
    return {"Cookie": f"copi-session={signer.sign(data).decode()}"}
```
Impersonation variant (`tests/integration/test_admin_users.py:42-52`) appends
`; copi-impersonate={impersonate_id}` to the Cookie header.

Logged-in POST example (`tests/integration/test_agent_page.py:228-237`):
```python
async def _invite(client, world, email):
    """Drive the real invite route; return the DelegateInvitation token."""
    r = await client.post(
        f"/agent/{OWNER_AGENT}/delegates/invite",
        data={"emails": email},
        headers=_auth(world.pi.id),
    )
    assert r.status_code == 302, r.text
    assert "delegate_error" not in r.headers["location"], r.headers["location"]
    return r
```
Another: `r = await client.post("/agent/request", headers=_auth(user.id))` (`:304`); form POST with
`data={"rating": "3", "comment": " solid "}` (`:606`); admin form POST
`client.post("/admin/impersonate", data={"orcid": f"  {orcid}  "}, headers=_auth(admin.id))`
(`test_admin_users.py:429`). Anonymous GET: `tests/integration/test_health_route.py:6-9`
(`r = await client.get("/api/health"); assert r.json() == {"status": "ok"}`).

The auth dependency being exercised is `src/dependencies.py:36-39`
`async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User`; it
reads `request.session.get("user_id")` (`:44`) and honours `copi-impersonate` for admins.

### B.6 Constructing `SimulationEngine` in unit tests

Constructor (`src/agent/simulation.py:240-255`):
```python
def __init__(
    self,
    agents: list[Agent],
    slack_clients: dict,  # agent_id -> AgentSlackClient
    max_runtime_minutes: int = 60,
    budget_cap: int = 0,
    session_factory=None,
    simulation_run_id: uuid.UUID | None = None,
    reset_cursors: bool = False,
    slack_enabled: bool = True,
):
```
Minimal (`tests/unit/test_simulation_logic.py:165-167`):
```python
@pytest.fixture
def engine(self):
    return SimulationEngine(agents=[], slack_clients={})
```
With agents and hermetic profiles (`tests/unit/test_authorship_emit_gate.py:17-32`):
```python
@pytest.fixture
def engine(tmp_path, monkeypatch):
    # Deterministic empty profiles: no profile-parsed DOIs leak into either
    # lab's "own" set regardless of what happens to be on disk in profiles/.
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    good = Agent(agent_id="good", bot_name="GoodBot", pi_name="Benjamin Good")
    wu = Agent(agent_id="wu", bot_name="WuBot", pi_name="Chunlei Wu")
    su = Agent(agent_id="su", bot_name="SuBot", pi_name="Andrew Su")
    eng = SimulationEngine(agents=[good, wu, su], slack_clients={})
    eng._agent_publications = {
        "wu": LabPublicationRecord(dois={DESIDERATA_DOI}, has_records=True),
        "su": LabPublicationRecord(dois={DESIDERATA_DOI}, has_records=True),
    }
    return eng
```
With a fake `session_factory` for DB-reading engine methods (`tests/unit/test_roster_sync.py:70-119`):
`_FakeDB` is an async context manager whose `execute()` returns `_FakeResult(rows)` with
`.all()`/`.scalar_one_or_none()`; `_factory_for(rows)` returns `lambda: _FakeDB(rows)`;
`_make_engine` passes `session_factory=_factory_for(active_rows)` and neuters side effects with
`engine._load_pi_mappings = AsyncMock()` and `engine._build_lab_directories = lambda: None`.
Slack client class swap: `monkeypatch.setattr("src.agent.slack_client.AgentSlackClient", _FakeSlackClient)` (`:122-123`).

Integration tests that need the engine to read the rolled-back session use
`_FixtureSessionFactory(session)` — a callable whose `__aenter__` returns the fixture session and
whose `__aexit__` does NOT close it (`tests/integration/test_state_rebuild.py:56-75`,
`tests/integration/test_message_persistence.py:27-52`), passed as `session_factory=`.
Slack-off engines use `NullTransport(agent_id=...)` from `src/agent/transport.py:93,103`.

### B.7 pytest / coverage config (`pyproject.toml`)

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"           # :72  (async tests need no decorator)
markers = [...]                 # :73-80 (see B.3)

[tool.coverage.run]
branch = true                   # :83
source = ["src"]                # :84
concurrency = ["thread", "greenlet"]   # :98 — required or SQLAlchemy greenlet switches lose the tracer

[tool.coverage.report]
show_missing = false            # :101
precision = 2                   # :106
```
Comment `pyproject.toml:85-97`: without `concurrency`, coverage stops recording an async handler at
its first `await db.execute(...)`; measured admin.py 19% -> 41%. Dev deps `pyproject.toml:32-54`
include `pytest`, `pytest-asyncio`, `ruff`, `testcontainers[postgres]`, `respx`, `syrupy`,
`coverage[toml]`, `pytest-cov`, `factory-boy`, `mutmut>=2.4,<3`, `playwright`.

### B.8 Snapshot tests

Library: **syrupy** (`pyproject.toml:38`), via the `snapshot` fixture; assertions are
`assert value == snapshot` (`tests/characterization/test_agent_turn_gm.py:92,104,114,139,160,183,205,251,267`;
`tests/characterization/test_profile_pipeline_gm.py:213`). Snapshot files:
`tests/characterization/__snapshots__/test_agent_turn_gm.ambr` and `test_profile_pipeline_gm.ambr`.
Update mechanism is syrupy's `pytest --snapshot-update`, but the repo treats blind updates as
forbidden: `docs/plans/2026-08-10-org1-parity.md:16` "**Never run `pytest --snapshot-update`.**";
`docs/specs/2026-08-06-role-topology-post-type-gating-design.md:474` "does **not** license a
blanket `pytest --snapshot-update`". The GM tests also assert the crux values explicitly "so a
careless --snapshot-update cannot silently" bless a regression
(`tests/characterization/test_profile_pipeline_gm.py:332,461`). (Note: that spec cites a "CLAUDE.md
prohibition" that is not present in the current CLAUDE.md.)

### B.9 Tests that pin current behavior a fix would have to invert

1. **`tests/integration/test_db_contract.py:268-281`** — DAT-1 pins that deleting a User who is a
   PI member of a private channel RAISES (FK SET NULL drives `user_id` NULL while `agent_id` is
   already NULL, violating the CHECK):
   ```python
   async def test_dat1_deleting_pi_member_user_violates_pcm_check(db_session):
       ch = await factories.make_agent_channel(db_session, visibility="collab_private")
       u = await factories.make_user(db_session)
       await factories.make_private_channel_member(
           db_session, channel=ch, agent_id=None, user_id=u.id, role="pi"
       )
       with pytest.raises(IntegrityError) as ei:
           async with db_session.begin_nested():
               await db_session.execute(
                   text("DELETE FROM users WHERE id = :id"), {"id": u.id}
               )
       # Lock the SPECIFIC constraint that fires, so a future schema change that makes a
       # different IntegrityError fire first can't silently re-point what "DAT-1" pins.
       assert "pcm_exactly_one_of_agent_or_user" in str(ei.value)
   ```
   A migration that changes `private_channel_members.user_id` to `ondelete="CASCADE"` (or a
   route that deletes memberships first) must rewrite this test to assert the delete succeeds
   and the membership row is gone. Header comment at `:262-266`.

2. **`tests/unit/test_thread_not_found.py:134-144`** — pins that `_evict_dead_thread` also
   forgets the thread was closed:
   ```python
   def test_evicts_from_all_agents(self, engine_with_agents):
       engine, dead_ts, a, b = engine_with_agents
       engine._evict_dead_thread(dead_ts)

       for ag in (a, b):
           assert dead_ts not in ag.state.active_threads
           assert not any(p.post_id == dead_ts for p in ag.state.interesting_posts)
           assert not any(p.thread_id == dead_ts for p in ag.state.pending_proposals)

       assert f"proposal_thread:{dead_ts}" not in engine._poll_cursors
       assert dead_ts not in engine._closed_thread_ids
   ```
   Implementation it pins: `src/agent/simulation.py:1817-1818`
   `self._poll_cursors.pop(f"proposal_thread:{thread_id}", None)` /
   `self._closed_thread_ids.discard(thread_id)`. Note `_rebuild_agent_state` uses
   `_closed_thread_ids` as the "already accounted for" marker (`simulation.py:4166-4177`), so a
   fix that keeps evicted threads in `_closed_thread_ids` must flip line 144 to `in`. The fixture
   (`:108-132`) seeds `engine._poll_cursors[f"proposal_thread:{dead_ts}"] = "1.0"` and
   `engine._closed_thread_ids.add(dead_ts)`.

3. **`tests/integration/test_worker.py`** — there is no test literally about a DB *connection*
   error. The two DB-error-adjacent tests are:
   - `:968-1000` `test_an_unknown_job_type_cannot_even_be_enqueued`: raw INSERT with
     `type='bogus_type'` -> `with pytest.raises(DBAPIError) as exc:` ... `await db.rollback()`
     ... `assert "job_type_enum" in str(exc.value)`, then a CONTROL insert with a legal type
     commits. Pins that the enum is the rejecting object.
   - `:485-523` `test_process_job_swallows_the_failure_so_the_next_job_still_runs` (T5.3):
     `monkeypatch.setattr(worker_main, "run_profile_pipeline", crash_for_bad)`; `process_job`
     must return normally; `assert (await wk.job_state(j_bad)).status == "dead"`; the next job
     completes. Pins `src/worker/main.py:100-106` (`except Exception as exc: logger.error(...)`,
     job marked dead after max attempts). A fix that makes `process_job` re-raise, or that
     treats DB errors differently from pipeline errors, must adjust this.
   The module docstring `:9-14` explains it uses a committing `async_sessionmaker(engine)` (not
   `db_session`) because `claim_job`/`process_job` commit, and patches
   `src.worker.main.run_profile_pipeline` (the import-time binding), not the service module (`:16-22`).

4. Other static pins that constrain edits (not bugs, but must be kept green):
   - `tests/unit/test_slack_client_contract.py` — `self._client.` may appear exactly once in
     `src/agent/slack_client.py` (see docstring at `slack_client.py:297-302`); every Slack call
     must go through `_api` -> `_call_with_retry`.
   - `tests/unit/test_slack_boundary.py:15,44-58` — `slack_sdk` may be imported only in the two
     `ALLOWED` modules (`src/agent/slack_client.py`, `src/services/slack_web.py`).
   - `tests/unit/test_slack_web.py:183` `test_every_sync_entry_point_has_an_async_wrapper` — a
     new sync function in `slack_web.py` needs a matching `_async` wrapper.
   - `tests/unit/test_cohort_isolation.py:1312-1320` pins that `scripts/ci.sh` contains `alembic heads`.

---

## C. Reusable in-repo patterns to copy

### C.1 IntegrityError rollback + single retry, then 409 — `src/routers/agent_page.py:1000-1027`
```python
    async def _write() -> None:
        await record_pi_message(
            db,
            run_id=run_id,
            channel_name=target_channel,
            content=text,
            sender_name=f"{current_user.name} (PI)",
            thread_ts=thread_ts.strip() or None,
        )
        await db.commit()

    # M1b guard: the canonical id can collide with another process (the sim)
    # minting the same microsecond for this run, which hits the
    # uq_agent_messages_run_ts constraint and would otherwise surface as a raw
    # 500. Roll back and retry once — record_pi_message mints a fresh, monotonic
    # id, so the retry gets a new ts. See PR #19 review M1.
    try:
        await _write()
    except IntegrityError:
        await db.rollback()
        try:
            await _write()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail="Message could not be saved due to a conflict, please retry",
            )
```
(`from sqlalchemy.exc import IntegrityError` at `agent_page.py:14`.)

### C.2 IntegrityError as lost-race -> rollback, fetch, update — `src/routers/public.py:1083-1102`
```python
    try:
        await db.commit()
    except IntegrityError:
        # Lost a race on the unique (decision, token) constraint — fetch & update.
        await db.rollback()
        vote_obj = (
            await db.execute(
                select(ProposalVote).where(
                    ProposalVote.thread_decision_id == payload.decision_id,
                    ProposalVote.voter_token == token,
                )
            )
        ).scalar_one()
        vote_obj.vote = payload.vote
        if details:
            vote_obj.details = details
        await db.commit()

    await db.refresh(vote_obj)
    return {"id": str(vote_obj.id)}
```
(Endpoint `submit_proposal_vote` starts at `public.py:1017`; import at `public.py:20`.)

### C.3 `asyncio.to_thread` wrappers — `src/services/slack_web.py:267-300`
```python
async def list_channel_ids_async(
    token: str,
    *,
    include_private: bool = True,
    exclude_archived: bool = False,
) -> dict[str, str]:
    """``list_channel_ids`` off the event loop."""
    return await asyncio.to_thread(
        list_channel_ids, token,
        include_private=include_private, exclude_archived=exclude_archived,
    )


async def lookup_user_by_email_async(token: str, email: str) -> str | None:
    """``lookup_user_by_email`` off the event loop."""
    return await asyncio.to_thread(lookup_user_by_email, token, email)


async def post_message_async(
    token: str, channel: str, text: str, *, thread_ts: str | None = None
) -> list[dict[str, Any]]:
    """``post_message`` off the event loop."""
    return await asyncio.to_thread(
        post_message, token, channel, text, thread_ts=thread_ts)
```
Rule from the module docstring `slack_web.py:15-19`: async callers MUST use the `_async` wrappers
because the sync core `time.sleep`s between retries. `__all__` at `:61-73` lists both forms.

### C.4 Bounded retry with Retry-After parse + cap — `src/services/slack_web.py:41-59, 86-122`
```python
_MAX_ATTEMPTS = 4
_BACKOFF_BASE = 0.5
_MAX_RETRY_AFTER = 30.0
_TERMINAL = frozenset({
    "invalid_auth", "account_inactive", "token_revoked", "no_permission",
    "user_not_found", "users_not_found", "channel_not_found", "not_in_channel",
})

def _call(client: WebClient, method: str, **kwargs: Any) -> Any:
    last: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            return getattr(client, method)(**kwargs)
        except SlackApiError as exc:
            code = _error_code(exc)
            if code in _TERMINAL:
                raise
            last = exc
            if attempt == _MAX_ATTEMPTS - 1:
                break
            delay = _BACKOFF_BASE * (2 ** attempt)
            if code == "ratelimited":
                retry_after = (getattr(exc.response, "headers", {}) or {}).get("Retry-After")
                if retry_after is not None:
                    try:
                        asked = float(retry_after)
                    except (TypeError, ValueError):
                        asked = delay
                    if asked > _MAX_RETRY_AFTER:
                        logger.warning(
                            "[slack_web] %s asked for Retry-After=%.0fs; capping at %.0fs",
                            method, asked, _MAX_RETRY_AFTER,
                        )
                    delay = min(asked, _MAX_RETRY_AFTER)
            logger.warning("[slack_web] %s failed (%s); retrying in %.1fs", method, code, delay)
            if delay > 0:
                time.sleep(delay)
    assert last is not None
    raise last
```
Unit-test pattern for it (`tests/unit/test_slack_web.py:191-208`): `monkeypatch.setattr(slack_web.time, "sleep", lambda d: slept.append(d))`,
`err.response.headers = {"Retry-After": "600"}`, `monkeypatch.setattr(slack_web, "_client", lambda _t: client)`,
`assert slept == [slack_web._MAX_RETRY_AFTER]`.

### C.5 `_call_with_retry` — `src/agent/slack_client.py:310-342` (constants `:119` `MAX_RETRIES = 3`)
```python
    def _call_with_retry(self, method, **kwargs) -> Any:
        last_exc: SlackApiError | None = None
        for attempt in range(MAX_RETRIES):
            try:
                return method(**kwargs)
            except SlackApiError as exc:
                if exc.response.get("error") == "ratelimited":
                    last_exc = exc
                    retry_after = int(exc.response.headers.get("Retry-After", 5))
                    logger.warning(
                        "[%s] Rate limited, retrying in %ds (attempt %d/%d)",
                        self.agent_id, retry_after, attempt + 1, MAX_RETRIES,
                    )
                    time.sleep(retry_after)
                else:
                    raise
        raise SlackApiError(
            "Rate limit retries exhausted",
            response=last_exc.response if last_exc else None,
        )
```
Note the asymmetry with C.4: this one has **no cap** on `Retry-After`, no exponential backoff,
and `int()` (not `float()`) with no `ValueError` guard. Docstring `:317-322` explains `last_exc`.
Chokepoint `_api(self, method: str, **kwargs)` at `:294-308` raises `SlackNotConnected` when
`self._client is None`. Contract test for Retry-After: `tests/unit/test_slack_client_contract.py:172-182`
(`monkeypatch.setattr(time, "sleep", ...)`, `slack_error("ratelimited", retry_after=17)`, `assert slept == [17]`).

### C.6 `_paginate` — `src/agent/slack_client.py:344-398` (constants `:127` `SLACK_PAGE_LIMIT = 200`, `:133` `MAX_PAGES = 200`)
```python
    def _paginate(
        self,
        method: str,
        key: str,
        *,
        limit: int = SLACK_PAGE_LIMIT,
        **kwargs,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        seen_cursors: set[str] = set()
        cursor = ""
        for page in range(MAX_PAGES):
            call = dict(kwargs)
            call["limit"] = limit
            if cursor:
                call["cursor"] = cursor
            try:
                result = self._api(method, **call)
            except SlackApiError as exc:
                if page == 0:
                    raise
                raise SlackListingIncomplete(
                    method, items,
                    f"page {page + 1} failed: {exc.response.get('error') if exc.response else exc}",
                ) from exc
            items.extend(result.get(key) or [])
            cursor = ((result.get("response_metadata") or {}).get("next_cursor") or "").strip()
            if not cursor:
                return items
            if cursor in seen_cursors:
                raise SlackListingIncomplete(
                    method, items, f"Slack repeated cursor {cursor!r} at page {page + 1}",
                )
            seen_cursors.add(cursor)
        raise SlackListingIncomplete(
            method, items, f"still paginating after {MAX_PAGES} pages",
        )
```
`SlackListingIncomplete(method, partial, reason)` is defined at `slack_client.py:71-84`.

### C.7 Per-item `except Exception` poller pattern — `src/agent/simulation.py`

Slack-side DM poller `_poll_pi_dms` (`:3005-3047`) — early-return guard, per-agent `continue`
when disconnected, per-message try/except around the DB write, cursor advance outside the try:
```python
        for pi_slack_id, agent_ids in self._pi_slack_id_to_agent_ids.items():
            for agent_id in agent_ids:
                client = self.slack_clients.get(agent_id)
                if not client or not client.is_connected:
                    continue

                oldest = self._dm_poll_cursors.get(agent_id, default_cursor)
                messages = client.poll_dm_messages(pi_slack_id, oldest=oldest)

                for msg in messages:
                    ts = msg.get("ts", "")
                    text = msg.get("text", "").strip()
                    if not text:
                        continue
                    logger.info("[%s] PI DM from %s: %s", agent_id, pi_slack_id, text[:80])
                    try:
                        async with self.session_factory() as db:
                            await record_pi_dm(
                                db, run_id=self.simulation_run_id, agent_id=agent_id,
                                pi_user_id=pi_slack_id, direction="inbound", content=text,
                                sender_name="PI", slack_ts=ts or None,
                            )
                            await db.commit()
                    except Exception as exc:
                        logger.error("[%s] Failed to record PI DM: %s", agent_id, exc)
                    if ts > oldest:
                        self._dm_poll_cursors[agent_id] = ts
```
DB-side sibling `_poll_pi_dms_from_db` (`:3086-3140`) — one try/except around the SELECT that
`return`s on failure (`:3100-3117`), then per-row try/except around the handler (`:3128-3132`):
```python
        for r in rows:
            if r.created_at and r.created_at > self._pi_dm_cursor:
                self._pi_dm_cursor = r.created_at
            if r.ts and r.ts in self._pi_dm_seen:
                continue  # already processed (lookback re-scan)
            if r.agent_id not in self.agents:
                continue
            if r.ts:
                self._pi_dm_seen[r.ts] = r.created_at or EPOCH_UTC
            try:
                await self._pi_handler.handle_dm(r.agent_id, r.pi_user_id, r.content)
                self.agents[r.agent_id].state.has_pi_directive = True
            except Exception as exc:
                logger.error("[%s] Failed to handle PI DM (DB): %s", r.agent_id, exc)
```
The other siblings: `_poll_slack_for_pi_messages` (`:2685`), `_poll_inbound_from_db` (`:2842`),
`_poll_proposal_threads_for_pi` (`:3142`); `_seed_pi_dm_cursor` (`:3049-3084`) wraps its whole
DB read in `try/except Exception as exc: logger.warning("PI DM cursor seed failed: %s", exc)`.
Log style: `logger.error("[%s] ...: %s", agent_id, exc)` with %-formatting, never f-strings.

### C.8 `_flush_persisted` re-queue-on-failure — `src/agent/simulation.py:3841-3847, 3888-3952`
```python
        entries = self._pending_persist
        self._pending_persist = []
        ...
        try:
            async with self.session_factory() as db:
                for start in range(0, len(rows), chunk_size):
                    stmt = pg_insert(AgentMessage.__table__).values(rows[start:start + chunk_size])
                    stmt = stmt.on_conflict_do_update(
                        constraint="uq_agent_messages_run_ts",
                        set_={...},
                        where=or_(
                            AgentMessage.__table__.c.is_bot.is_(True),
                            stmt.excluded.is_bot.is_(False),
                        ),
                    )
                    await db.execute(stmt)
                ...
                await db.commit()
        except Exception as exc:
            # Re-queue the failed batch instead of dropping it. The DB is now the
            # source of truth for conversations, so a silently-dropped flush is
            # unrecoverable — a restart rebuilds from the DB and these messages
            # would be gone for good. New entries may have been enqueued while we
            # were awaiting the (failed) commit; put the failed batch back in
            # front to preserve chronological order for the next flush attempt.
            self._pending_persist[0:0] = entries
            logger.warning(
                "Failed to flush %d messages, re-queued for retry: %s",
                len(rows), exc,
            )
```
Also note `:3883-3887` chunking to stay under `_PG_MAX_BIND_PARAMS` "because the except below
re-queues the whole batch on failure" (poison-pill avoidance).

### C.9 `set_default_writer_id` — `src/agent/ids.py:114-127`; callers
```python
def set_default_writer_id(writer_id: int) -> None:
    global _default
    old = _default
    new = TsMinter(writer_id)
    with old._lock:
        new._last_slot = old._last_slot
    _default = new
```
Writer slots `ids.py:47-50`: `WRITER_ENGINE = 0`, `WRITER_WEB = 1`, `WRITER_GRANTBOT = 2`,
`WRITER_ENGINE_AUX = 3`; `WRITER_SLOT_MODULUS = 100` (`:41`). Called once per process at entry:
- `src/main.py:110-113` inside `create_app()`:
  ```python
  # Claim the web process's canonical-id writer slot, so PI messages and DMs
  # written here can never collide with ids minted by the engine or GrantBot
  # processes (R1). See src/agent/ids.py.
  set_default_writer_id(WRITER_WEB)
  ```
- `src/agent/main.py:51-54` inside the typer `main()` before `asyncio.run(...)`:
  ```python
  # Claim this process's canonical-id writer slot before anything mints. The
  # engine's own minter owns WRITER_ENGINE; the module default is used here
  # only for PI DM rows, so it takes the aux slot (R1).
  set_default_writer_id(WRITER_ENGINE_AUX)
  ```

### C.10 `_rebuild_agent_state` — signature and startup call
Signature `src/agent/simulation.py:4148-4154`:
```python
    async def _rebuild_agent_state(self) -> None:
        """Reconstruct per-agent state from the message log + DB.

        Runs after both the DB rebuild and the optional Slack reconcile, so it
        behaves identically with Slack on or off. Reads only self.message_log,
        thread_decisions, proposal_reviews and llm_call_logs — no Slack calls.
        """
```
Startup order in `start()` (`simulation.py:556-579`): `_ensure_seeded_channels()` ->
`await _persist_seeded_channels()` -> `await _sync_private_channels_from_db()` ->
`await _load_pi_mappings()` -> `message_log.set_persist_callback(self._enqueue_persist)` ->
`await _rebuild_state_from_db()` -> `await _resolve_service_bot_uids()` ->
`await _rebuild_state_from_slack()` -> **`await self._rebuild_agent_state()` (`:578`)** ->
`await _seed_pi_dm_cursor()` -> `_rewind_cursors_for_private_channels()` -> ... ->
`await _recompute_allowed_sender_ids()` (`:594`) -> `refresh_lab_directories()` (`:597`).
Only production call site is `:578`; tests call it directly after `_rebuild_state_from_db()`
(`tests/integration/test_state_rebuild.py:132,152,...`; idempotency pinned by calling it twice `:196-201`).
Inside, the DB read is guarded `if self.session_factory: try: ... async with self.session_factory() as db:` (`:4158-4161`).

### C.11 `Agent` construction in `_sync_roster_from_db` to_add branch — `src/agent/simulation.py:4638-4662`
```python
            for aid in to_add:
                r = desired[aid]
                if self.slack_enabled:
                    token = r.slack_bot_token if is_valid_token(r.slack_bot_token) else env_token(aid)
                    if not is_valid_token(token):
                        logger.info(
                            "[roster] Agent %s is active but has no usable token yet — "
                            "skipping (will retry next sync once a token is set)", aid,
                        )
                        continue
                    client = AgentSlackClient(agent_id=aid, bot_token=token)
                    if not client.connect():
                        logger.warning("[roster] Slack connect failed for new agent %s — skipping", aid)
                        continue
                else:
                    # Slack off: admit the agent with a no-op transport (never
                    # gate on a token/connection that doesn't apply in DB-only mode).
                    from src.agent.transport import NullTransport
                    client = NullTransport(agent_id=aid)
                agent = Agent(agent_id=aid, bot_name=r.bot_name, pi_name=r.pi_name, role=r.role)
                # In-place inserts (PIHandler shares these dicts by reference).
                self.agents[aid] = agent
                self.slack_clients[aid] = client
                self._bot_name_to_id[agent.bot_name.lower()] = aid
                logger.info("[roster] Added newly-active agent %s to live roster", aid)
```
The roster SELECT (`:4538-4547`) reads `AgentRegistry.agent_id, bot_name, pi_name,
slack_bot_token, role` where `status == "active"`. Whole method is wrapped in
`try: ... except Exception as exc: logger.warning("[roster] roster sync failed: %s", exc)`
(`:4531, 4675-4677`) and throttled by `ROSTER_POLL_INTERVAL` (`:4526-4529`). Inner isolated
try/except for `_load_publication_records` at `:4557-4563`. After membership changes:
`self.message_log.set_bot_name_map(self._bot_name_to_id)`, `self._pi_slack_id_to_agent_ids.clear()`,
`await self._load_pi_mappings()`, `await self._recompute_allowed_sender_ids()` (`:4665-4674`).

### C.12 `get_db` — `src/database.py:46-58`
```python
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency for database sessions."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
```
(Commits on successful handler exit; engine kwargs in A.3.)

### C.13 `AgentBadgeMiddleware.dispatch` head — `src/main.py:25-43, 96-104`
```python
class AgentBadgeMiddleware(BaseHTTPMiddleware):
    """Inject unreviewed proposal count into request.state for nav badge."""

    async def dispatch(self, request: Request, call_next):
        request.state.posthog_api_key = get_settings().posthog_api_key
        request.state.agent_badge_count = 0
        user_id_str = request.session.get("user_id") if "session" in request.scope else None
        if user_id_str:
            try:
                from src.models import (
                    AgentDelegate,
                    AgentRegistry,
                    ProposalReview,
                    ThreadDecision,
                    User,
                )
                session_factory = get_session_factory()
                async with session_factory() as db:
                    uid = uuid.UUID(user_id_str)
                    ...
            except Exception as exc:
                # Deliberately swallowed: this middleware only computes a nav
                # badge count, and no page should 500 because a count failed.
                # But it is LOGGED — ...
                logger.warning("Badge-count middleware failed, continuing: %s", exc)
        return await call_next(request)
```
It uses `get_session_factory()` directly (not `get_db`), which is why `tests/conftest.py:120-121`
monkeypatches `src.main.get_session_factory`. Registered first at `src/main.py:122` so it runs
inside `SessionMiddleware` (`:125-132`, cookie `copi-session`, 30-day max_age).

### C.14 `/api/health` — `src/main.py:150-153`
```python
    @application.get("/api/health")
    async def health():
        """Health check endpoint."""
        return {"status": "ok"}
```
Defined inline inside `create_app()`, not in a router. Test: `tests/integration/test_health_route.py:6-9`.

### C.15 Defensive strip model — `strip_ungrounded_authorship_lines` usage, `src/agent/simulation.py:5340-5360`
```python
            # Authorship hygiene (issue #29): a false authorship note written
            # here is re-injected into every future prompt. Strip lines the
            # publication records can't back before persisting.
            own_db = self._agent_publications.get(agent.agent_id)
            profile_dois = agent.own_publication_dois
            own_record = LabPublicationRecord(
                dois=(own_db.dois if own_db else set()) | profile_dois,
                has_records=bool(own_db) or bool(profile_dois),
            )
            response, stripped_lines = strip_ungrounded_authorship_lines(
                response,
                own_record,
                self_names=lab_self_names(
                    agent.agent_id, agent.bot_name, agent.pi_name
                ),
            )
            for line in stripped_lines:
                logger.warning(
                    "[%s] Memory update: stripped ungrounded authorship line: %s",
                    agent.agent_id, line[:160],
                )
```
Shape to copy: pure function returns `(cleaned_text, removed_items)`; caller logs each removed
item at WARNING with a truncated preview, then proceeds with the cleaned text
(`agent.update_working_memory_file(response, ...)` at `:5362`). Defined in
`src/agent/authorship_rules.py:355`; imported at `simulation.py:18`.

---

## D. Lint / format / running tests

### D.1 ruff config — `pyproject.toml:63-69`
```toml
[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]
ignore = ["E501"]
```
No formatter (`ruff format`/black) is configured or run by the gate.

### D.2 The gate — `scripts/ci.sh` (301 lines), run by `.git/hooks/pre-push` (`exec .../scripts/ci.sh`)

Steps in order (`ci.sh:7-20`): (1) alembic single head + no duplicate ids, offline (`:103-127`);
(2) alembic round trip `upgrade head -> downgrade $MIGRATION_FLOOR -> upgrade head` against a
throwaway postgres:15 on `127.0.0.1:55432` (`:129-240`, DSN
`postgresql+asyncpg://copi:copi@127.0.0.1:${MIGCHECK_PORT}/copi_migcheck` `:199`, floor `0018` `:70`;
skip with `CI_MIGRATION_DB=none`); (3) ruff on tests — **zero findings**; (4) ruff on src — ceiling;
(5) full pytest with coverage floor; then reclaim leaked testcontainers volumes (`:297-298`).

Variables (`ci.sh:38-88`):
```bash
VENV_PY="${VENV_PY:-$REPO_ROOT/.venv-test/bin/python}"
COV_MIN="${COV_MIN:-60}"
SRC_LINT_MAX="${SRC_LINT_MAX:-260}"
MIGCHECK_PORT="${MIGCHECK_PORT:-55432}"
MIGRATION_FLOOR="${MIGRATION_FLOOR:-0018}"
LINT_TARGETS=(
  tests/conftest.py tests/factories.py tests/fakes.py
  tests/unit tests/integration tests/characterization tests/contract
  tests/e2e
  scripts/migrate
  scripts/backup
)
```
Test-suite lint, must be clean (`ci.sh:242-243`):
```bash
echo "==> ruff (test-suite lint)"
"$VENV_PY" -m ruff check "${LINT_TARGETS[@]}"
```
The src ratchet, verbatim (`ci.sh:245-290`):
```bash
echo "==> ruff (src/ ratchet, ceiling ${SRC_LINT_MAX})"
set +e
src_lint_out="$("$VENV_PY" -m ruff check src --output-format=concise --quiet 2>&1)"
src_lint_rc=$?
set -e
if [ "$src_lint_rc" -gt 1 ]; then
  echo "ERROR: ruff failed to run over src/ (exit ${src_lint_rc}):" >&2
  printf '%s\n' "$src_lint_out" >&2
  exit 1
fi
if printf '%s' "$src_lint_out" | grep -q 'E902'; then
  echo "ERROR: ruff could not read part of src/ (E902), so the finding count is not a" >&2
  ...
  exit 1
fi
src_findings="$(printf '%s' "$src_lint_out" | grep -c . || true)"
if [ "$src_findings" -gt "$SRC_LINT_MAX" ]; then
  echo "ERROR: ruff findings in src/ rose to ${src_findings}; the ceiling is ${SRC_LINT_MAX}." >&2
  echo "Fix what you added. Do not raise SRC_LINT_MAX in scripts/ci.sh to make this pass." >&2
  printf '%s\n' "$src_lint_out" >&2
  exit 1
fi
echo "    ${src_findings} findings (ceiling ${SRC_LINT_MAX})"
```
Latest measured: 254 findings vs ceiling 260 (commit `18ba52c` body). New src code should add
zero findings; net-negative is welcome ("LOWER THIS AS DEBT IS PAID; NEVER RAISE IT" `ci.sh:52`).

Pytest step (`ci.sh:292-295`):
```bash
echo "==> pytest (full suite + branch coverage, fail-under=${COV_MIN}%)"
"$VENV_PY" -m pytest tests/ \
  --cov=src --cov-report=term-missing \
  --cov-fail-under="${COV_MIN}"
```
Coverage floor **60%** (`ci.sh:46`); latest measured 69.2% (commit `18ba52c` body). Gate
prerequisites: `.venv-test/bin/python` must exist (`ci.sh:90-95`, create with
`uv venv .venv-test && uv pip install --python .venv-test/bin/python -e '.[dev]'`) and Docker must
be reachable (`ci.sh:97-101`). `.venv-test/bin/python -> /usr/bin/python3` (Python 3.12.3 on this host).

### D.3 Running a single test file

Host (what the gate uses):
```bash
.venv-test/bin/python -m pytest tests/unit/test_roster_sync.py -q
.venv-test/bin/python -m pytest tests/integration/test_agent_page.py -q   # needs Docker for testcontainers
```
Host, pointing integration tests at an existing Postgres instead of testcontainers:
`TEST_DATABASE_URL=postgresql+asyncpg://... .venv-test/bin/python -m pytest tests/integration/... -q`
(`tests/conftest.py:30-35, 51-53`).

In-container (CLAUDE.md "Testing"; `TEST_DATABASE_URL` is REQUIRED because the app container has no
Docker socket; the named DB must pre-exist; never use `copi`):
```bash
docker compose exec -T -e TEST_DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_a3 \
  app python -m pytest tests/unit/test_roster_sync.py -v
docker compose exec -T postgres createdb -U copi copi_xN   # fresh scratch DB
```
Same form is used throughout `.notes/cohort-thorough-test-plan.md` (`:163,386,905`).
Lint a single file the same way the gate does: `.venv-test/bin/python -m ruff check tests/unit/test_x.py`.

### D.4 Rule: tests/ must be ruff-clean
`scripts/ci.sh:14` "ruff lint of the test suite. New test code is kept spotless — zero findings."
Enforced by `ci.sh:242-243` (plain `ruff check` on `LINT_TARGETS`, exit 1 on any finding). The same
zero-finding bar applies to `scripts/migrate` and `scripts/backup` (`ci.sh:80-87`).

---

## E. Commit / PR conventions

### E.1 Last 40 subjects (`git log --format='%h %s' -40`)
```
18ba52c feat(cohort): grantbot service-bot membership + attribution; admin access visibility
c7c3427 fix(backup): install the OnFailure target unit
166242f Fix two fail-green critical findings in the verified-backup nightly (C1, C2)
6fe3c8e fix(cohort): add the four empty-institution Scripps PIs to scripps-investigators
2c0c021 fix(cohort): post-audit fix wave — robustness, drift reporting, tests, docs
ccf912c fix(graph): select /scripps-graph nodes from the cohort, not _SCRIPPS
7591a2a feat(cohort): idempotent seeding with a real audit trail
a6bc6e5 feat(cohort): seed planner — additive diff of manifest against DB
3c84371 feat(cohort): manifest of record for the three cohorts, plus its validator
9143e32 docs(cohort): implementation plan for seeding the three cohorts
965c066 docs(backup): stop overstating what the TOC precheck can see
cc7d7e6 Merge: verified nightly Postgres backups for both production stacks
9238f73 fix(backup): raise FREE_SPACE_FACTOR to 7 in the template too
37605e0 Fix seven Important findings from the final backup-system audit
75c639f test(backup): harness green — 12/12, with the TOC boundary measured
914a0e8 fix(backup): harness must never mutate a real backup
19a366e test(backup): failure-injection harness for the verified-backup system
7179744 fix(backup): validate BACKUP_ROOT path to prevent system directory escape
6e95183 feat(backup): systemd units, config template and installer
1a917e5 fix(backup): add name validation, containment check, explicit flag rejection
c14f08f test: bind the throwaway Postgres to loopback only
97395b5 ci: reclaim testcontainers' leaked anonymous volumes
01b9506 feat(backup): preflight guards, label-filtered sweep, retention and CLI
9d9ed55 fix(backup): add comprehensive guards against untested mutations
c72a8ea feat(backup): sidecars, status document, and SES reporting
3afbfa9 fix(backup): timeout handling, stack name deduplication, fast-fail guard
b2e5797 feat(backup): isolated verify-restore with OOM discrimination
87cce8a feat(backup): fix Task 6 tests — verify partial cleanup and session failures
96890b3 feat(backup): dump orchestration with container temp and atomic rename
728716b fix(backup): orphaned processes, zombies, and hung psql
33f5ad2 fix(backup): snapshot session requires docker exec -i for stdin attachment
e62f972 feat(backup): snapshot-consistent session and exact count capture
fd7bd92 feat(backup): command builders and injectable runner seam
e498841 docs(backup): fold host findings into the design
a01042f feat(backup): exact row-count parity comparison
2842829 feat(backup): dump naming and count-based retention selection
26876c4 feat(backup): config parsing scaffold and lint gate for backup tooling
30e6f61 docs(backup): verified Postgres backup design
5e79574 docs(cohort): design for seeding cabo/schultz/scripps cohorts
364bee3 fix(inbound): fail closed on null-email users; make the help email reply-able
```
Style: conventional-commit prefix `type(scope): lowercase imperative summary`, types seen
`feat`, `fix`, `docs`, `test`, `ci`, `ops` (`a83060e ops(agent): ...`), scopes = subsystem
(`cohort`, `backup`, `agent`, `inbound`, `email`, `graph`, `tools`, `prompts`, `sweep`, `runbook`).
Em-dashes and semicolons in subjects are common; no trailing period. A minority use a bare
capitalized imperative ("Fix two fail-green critical findings..."). Merge commits are
`Merge pull request #N from SuLab/<branch>` or a hand-written `Merge: <summary>`.

### E.2 Issue references
Yes, when an issue exists: subject suffix `(#29)` or `(#29 audit I5)` (e.g. `3bcd8d9 feat(agent):
strip ungrounded authorship lines from memory syntheses (#29)`, `12f7b46 fix(agent): identity-aware
memory strip — no laundering through third-person subjects (#29 audit I5)`), and PR bodies open
with `Closes #29`. Recent cohort/backup work has no issue numbers (bodies cite audit finding ids
like "C1, C2", "I3" instead).

### E.3 Commit bodies
Long, explanatory prose in numbered/paragraph form; state root cause, what changed, measured
figures, and the gate result. Example tail from `18ba52c`:
```
Tests: +44 (service-bot attribution incl. a start()-ordering pin, service-tag
thread rules, admin users page/badges/filters/gate, service-id cohort add,
seeding access status, impersonate-create). Full gate green: 2030 passed,
120 skipped, coverage 69.2% (floor 60), ruff src 254 (ceiling 260).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
```
Every recent commit ends with a `Co-Authored-By: Claude ... <noreply@anthropic.com>` trailer.
No commit template (`git config commit.template` unset; no `.gitmessage`/`CONTRIBUTING.md`).

### E.4 PR bodies (`gh pr view 32 --json body`, `gh pr view 36 --json body`; both base = `copi-prod`)
- **PR 32** (`SuLab/issue-29-authorship-grounding` -> `copi-prod`): opens `Closes #29 (targets
  \`copi-prod\`, stacked on the \`email-fix\` line).` then a one-paragraph incident summary;
  sections `## What this does (layered, deterministic-first)` (bulleted, bold lead-ins, file
  paths in backticks), `## Verification` ("Full `./scripts/ci.sh` green: **1775 passed / 120
  skipped, 67.40% branch coverage, ruff 256/260**" + how it was audited), `## Rollout — read
  \`docs/issue-29-remediation.md\` before deploying` (ordered ops steps), `## Known
  conservative-direction follow-ups (non-blocking...)`.
- **PR 36** (`SuLab/deploy-followups` -> `copi-prod`): opens with stacking note ("Stacked on #31
  (`email-fix`): **merge #31 (and #32) first**"); sections `## What this adds` (each bullet
  starts with the commit prefix in code, e.g. "**`fix(email)`: pin ...**"), `## What this does
  NOT do` (explicitly "No migrations (alembic head stays `0024`)."), `## Testing` ("written
  test-first ... Full `./scripts/ci.sh` gate green on this branch: **1681 passed / 120 skipped,
  20 snapshots**, migration round trip clean"), and ends with the footer
  `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
Pattern to mirror: state target branch and stacking; What/What-not; Verification with exact
ci.sh numbers (passed/skipped/coverage/ruff count); Rollout steps if ops-affecting; footer.

---

## F. File sizes (`wc -l`)

| file | lines |
|---|---|
| `src/agent/simulation.py` | 5498 |
| `src/agent/slack_client.py` | 1098 |
| `src/agent/grantbot.py` | 790 |
| `src/services/email_inbound.py` | 855 |
| `src/services/email_notifications.py` | 1075 |
| `src/services/profile_pipeline.py` | 579 |
| `src/routers/agent_page.py` | 1623 |
| `src/routers/admin.py` | 1930 |
| `src/routers/public.py` | 1132 |
| `src/worker/main.py` | 185 |
| `src/main.py` | 158 |
| `src/database.py` | 58 |
| total | 14981 |

Supporting sizes: `tests/conftest.py` 351, `tests/fakes.py` 256, `tests/factories.py` 190,
`scripts/ci.sh` 301, `alembic/env.py` ~118.
