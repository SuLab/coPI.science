"""Integration tests for the management CLI (src/cli.py) — Task 6 of the full-system plan.

Seven commands, previously zero coverage. `seed-profiles` is the documented way to add
PIs (CLAUDE.md), so it is a production path.

**Why these tests are shaped oddly.** The CLI does not take a session; every command
calls `src.cli._get_db()`, which builds its *own* engine from
`get_settings().database_url` and commits for real. Three consequences:

1. The conftest `db_session` (rolled-back transaction) is invisible to the CLI and
   useless here — the CLI reads on a different connection. These tests therefore use
   the session-scoped `engine` with a *committing* session and clean up explicitly.
2. Every test function is synchronous. `_run()` is `asyncio.run()`, which raises if a
   loop is already running, so a pytest-asyncio coroutine test could not invoke a
   command at all.
3. Inside the app container `DATABASE_URL` points at the shared dev database, which the
   plan puts off-limits. `cli_points_at_test_db` (autouse) repoints `get_settings` at
   the migrated test DB and refuses to run if that failed.

The only mocked dependency is `src.services.orcid.fetch_orcid_profile` — the CLI's sole
outbound call. No LLM is reachable from these commands: they enqueue `Job` rows and the
worker does the generating.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from typer.testing import CliRunner

from src.cli import app as cli_app
from src.models import AgentRegistry, Job, ProfileRevision, User
from tests import factories

pytestmark = pytest.mark.integration

# Rows created by this module are committed for real, so they are tagged and deleted
# around every test. ORCID is a free-form String(50); nothing validates the format.
ORCID_PREFIX = "CLI-TEST-"
AGENT_PREFIX = "clitest"


def _orcid(tag: str) -> str:
    return f"{ORCID_PREFIX}{tag}"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _in_own_loop(engine, fn):
    """Run `fn(session)` in a fresh event loop on a committing session.

    A fresh loop per call is required: the CLI's own `asyncio.run()` closes the loop it
    made, and the conftest engine uses NullPool precisely so a new loop gets a new
    asyncpg connection.
    """

    async def _inner():
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            out = await fn(session)
            await session.commit()
            return out

    return asyncio.run(_inner())


@pytest.fixture
def db(engine):
    """Call as `db(lambda s: some_coroutine(s))`; commits, returns the value."""

    def _call(fn):
        return _in_own_loop(engine, fn)

    return _call


async def _wipe(session):
    agent_ids = select(AgentRegistry.id).where(AgentRegistry.agent_id.like(f"{AGENT_PREFIX}%"))
    user_ids = select(User.id).where(User.orcid.like(f"{ORCID_PREFIX}%"))
    await session.execute(
        delete(ProfileRevision).where(ProfileRevision.agent_registry_id.in_(agent_ids))
    )
    await session.execute(delete(AgentRegistry).where(AgentRegistry.agent_id.like(f"{AGENT_PREFIX}%")))
    await session.execute(delete(Job).where(Job.user_id.in_(user_ids)))
    await session.execute(delete(User).where(User.orcid.like(f"{ORCID_PREFIX}%")))


@pytest.fixture(autouse=True)
def _clean_slate(engine):
    """Delete this module's rows before and after each test (the CLI really commits)."""
    _in_own_loop(engine, _wipe)
    yield
    _in_own_loop(engine, _wipe)


@pytest.fixture(autouse=True)
def cli_points_at_test_db(monkeypatch, pg_url):
    """Repoint `_get_db()` at the migrated test database.

    `_get_db` does `from src.config import get_settings` *inside* the function, so
    patching the module attribute is enough — the real `_get_db` still runs.
    """
    from src import config

    patched = config.get_settings().model_copy(update={"database_url": pg_url})
    monkeypatch.setattr(config, "get_settings", lambda: patched)
    monkeypatch.setenv("DATABASE_URL", pg_url)

    # Guard, not decoration: in the app container the ambient DATABASE_URL is the
    # shared dev DB. Writing there would be a plan violation, so refuse to proceed.
    assert patched.database_url == pg_url
    assert not pg_url.rstrip("/").endswith("/copi"), f"refusing to run against {pg_url}"
    return patched


@pytest.fixture
def runner():
    # COLUMNS keeps rich from wrapping the list-users table mid-cell at its 80-column
    # non-tty default, which would break substring assertions for reasons unrelated to
    # the code under test.
    return CliRunner(env={"COLUMNS": "220", "TERM": "dumb", "NO_COLOR": "1"})


class _OrcidStub:
    """Recording stand-in for src.services.orcid.fetch_orcid_profile."""

    def __init__(self):
        self.calls: list[str] = []
        self.profiles: dict[str, dict] = {}
        self.fail: set[str] = set()

    def set(self, orcid: str, **fields):
        self.profiles[orcid] = {"orcid": orcid, **fields}

    async def __call__(self, orcid: str):
        self.calls.append(orcid)
        if orcid in self.fail:
            raise RuntimeError("ORCID 503")
        return self.profiles.get(orcid, {"orcid": orcid, "name": f"Stub {orcid}"})


@pytest.fixture
def orcid_stub(monkeypatch):
    stub = _OrcidStub()
    monkeypatch.setattr("src.services.orcid.fetch_orcid_profile", stub)
    return stub


def _ok(result):
    assert result.exit_code == 0, (
        f"exit={result.exit_code} exception={result.exception!r}\n{result.output}"
    )
    return result


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


async def _user_by_orcid(session, orcid):
    return (await session.execute(select(User).where(User.orcid == orcid))).scalar_one_or_none()


async def _count_users(session, orcid):
    return (
        await session.execute(select(func.count()).select_from(User).where(User.orcid == orcid))
    ).scalar_one()


async def _mine(session):
    """Every user this module created, oldest first."""
    rows = await session.execute(
        select(User).where(User.orcid.like(f"{ORCID_PREFIX}%")).order_by(User.created_at)
    )
    return list(rows.scalars())


async def _jobs_for(session, user_id):
    rows = await session.execute(
        select(Job).where(Job.user_id == user_id).order_by(Job.enqueued_at)
    )
    return list(rows.scalars())


async def _all_job_ids(session):
    return {row[0] for row in await session.execute(select(Job.id))}


async def _all_user_ids(session):
    return {row[0] for row in await session.execute(select(User.id))}


async def _jobs_by_id(session, ids):
    if not ids:
        return []
    rows = await session.execute(select(Job).where(Job.id.in_(list(ids))))
    return list(rows.scalars())


async def _revisions_for(session, agent_registry_id):
    rows = await session.execute(
        select(ProfileRevision).where(ProfileRevision.agent_registry_id == agent_registry_id)
    )
    return list(rows.scalars())


# ===========================================================================
# T6.1 — seed-profile / seed-profiles
# ===========================================================================


def test_seed_profile_creates_user_and_enqueues_job(db, runner, orcid_stub):
    """T6.1: the happy path writes the ORCID payload through to the row and the job."""
    orcid = _orcid("seed1")
    orcid_stub.set(
        orcid,
        name="Ada Lovelace",
        email="ada@example.edu",
        institution="Analytical Institute",
        department="Engines",
    )

    _ok(runner.invoke(cli_app, ["seed-profile", "--orcid", orcid]))

    user = db(lambda s: _user_by_orcid(s, orcid))
    assert user is not None, "seed-profile exited 0 but created no user"
    # Every field the command claims to copy across, so dropping one is caught.
    assert user.name == "Ada Lovelace"
    assert user.email == "ada@example.edu"
    assert user.institution == "Analytical Institute"
    assert user.department == "Engines"
    assert orcid_stub.calls == [orcid]

    jobs = db(lambda s: _jobs_for(s, user.id))
    assert len(jobs) == 1, f"expected exactly one job, got {len(jobs)}"
    assert jobs[0].type == "generate_profile"
    assert jobs[0].status == "pending"
    assert jobs[0].payload == {"user_id": str(user.id), "orcid": orcid}


def test_seed_profile_grants_access_instead_of_leaving_the_user_pending(
    db, runner, orcid_stub
):
    """T6.1: a seeded PI is pre-vetted, so the row must land ``access_status='allowed'``.

    ``User.access_status`` defaults to ``"pending"`` in the model (migration 0010
    dropped the server default), and the command used to omit the field entirely —
    so every PI added the documented way (CLAUDE.md's `seed-profiles`) appeared in
    /admin/access-requests as a phantom request nobody had made, and had to be
    promoted afterwards by scripts/promote_active_agent_users.py.

    The `--no-pipeline` variant is asserted too: the grant lives in the row-creation
    branch, so it must not depend on the job being enqueued.
    """
    piped, quiet = _orcid("access-piped"), _orcid("access-quiet")
    orcid_stub.set(piped, name="Vetted PI")
    orcid_stub.set(quiet, name="Vetted Quiet PI")

    _ok(runner.invoke(cli_app, ["seed-profile", "--orcid", piped]))
    _ok(runner.invoke(cli_app, ["seed-profile", "--orcid", quiet, "--no-pipeline"]))

    assert db(lambda s: _user_by_orcid(s, piped)).access_status == "allowed"
    assert db(lambda s: _user_by_orcid(s, quiet)).access_status == "allowed"

    # Control for the assertions above: "pending" really is what the model would
    # have produced, so "allowed" is the command's doing and not the default.
    assert User.__table__.c.access_status.default.arg == "pending"


def test_seed_profiles_grants_access_to_every_user_in_the_file(
    db, runner, orcid_stub, tmp_path
):
    """The bulk path is the documented one, so it gets its own assertion rather
    than inheriting the single-ORCID test's coverage."""
    a, b = _orcid("access-file-a"), _orcid("access-file-b")
    orcid_stub.set(a, name="Bulk PI A")
    orcid_stub.set(b, name="Bulk PI B")
    listing = tmp_path / "orcids.txt"
    listing.write_text(f"# Cohort 4\n{a}\n{b}\n")

    _ok(runner.invoke(cli_app, ["seed-profiles", "--file", str(listing)]))

    created = db(_mine)
    assert {u.orcid for u in created} == {a, b}
    assert [u.access_status for u in created] == ["allowed", "allowed"]


def test_seed_profile_does_not_re_grant_access_to_an_existing_denied_user(
    db, runner, orcid_stub
):
    """The grant is scoped to row creation, so re-seeding must not silently
    resurrect somebody an admin deliberately denied.

    Control in the same test: a fresh ORCID in the same run does get "allowed",
    so this is not asserting that the command never grants anything.
    """
    denied = _orcid("access-denied")
    fresh = _orcid("access-fresh")
    orcid_stub.set(fresh, name="Fresh PI")

    async def _seed(session):
        await factories.make_user(
            session, orcid=denied, name="Denied PI", access_status="denied"
        )

    db(_seed)

    _ok(runner.invoke(cli_app, ["seed-profile", "--orcid", denied]))
    assert db(lambda s: _user_by_orcid(s, denied)).access_status == "denied", (
        "re-seeding must not overwrite an admin's denial"
    )

    _ok(runner.invoke(cli_app, ["seed-profile", "--orcid", fresh]))
    assert db(lambda s: _user_by_orcid(s, fresh)).access_status == "allowed"


def test_seed_profile_duplicate_orcid_does_not_create_a_second_user(db, runner, orcid_stub):
    """T6.1 control: re-seeding an ORCID is a no-op for `users`; a *new* ORCID is not."""
    dup = _orcid("dup")
    fresh = _orcid("fresh")
    orcid_stub.set(dup, name="First Seed")
    orcid_stub.set(fresh, name="Second Seed")

    _ok(runner.invoke(cli_app, ["seed-profile", "--orcid", dup]))
    _ok(runner.invoke(cli_app, ["seed-profile", "--orcid", dup]))

    assert db(lambda s: _count_users(s, dup)) == 1
    # ORCID was fetched once only — the existence check short-circuits the network call.
    assert orcid_stub.calls == [dup]

    # Positive control: the guard is not "never create anything".
    _ok(runner.invoke(cli_app, ["seed-profile", "--orcid", fresh]))
    assert db(lambda s: _count_users(s, fresh)) == 1
    assert len(db(_mine)) == 2
    assert orcid_stub.calls == [dup, fresh]

    # Characterization: the *job* is re-enqueued on every run even for an existing
    # user. That is how the command doubles as "regenerate this one PI".
    user = db(lambda s: _user_by_orcid(s, dup))
    assert len(db(lambda s: _jobs_for(s, user.id))) == 2


def test_seed_profile_no_pipeline_skips_the_job_but_still_creates_the_user(
    db, runner, orcid_stub
):
    """T6.1: --no-pipeline suppresses the job. Control: without it, a job appears."""
    quiet = _orcid("nopipe")
    loud = _orcid("pipe")
    orcid_stub.set(quiet, name="Quiet PI")
    orcid_stub.set(loud, name="Loud PI")

    _ok(runner.invoke(cli_app, ["seed-profile", "--orcid", quiet, "--no-pipeline"]))
    _ok(runner.invoke(cli_app, ["seed-profile", "--orcid", loud]))

    quiet_user = db(lambda s: _user_by_orcid(s, quiet))
    loud_user = db(lambda s: _user_by_orcid(s, loud))
    assert quiet_user is not None and quiet_user.name == "Quiet PI"
    assert db(lambda s: _jobs_for(s, quiet_user.id)) == []
    # Control for the absence assertion above.
    assert len(db(lambda s: _jobs_for(s, loud_user.id))) == 1


def test_seed_profile_falls_back_to_the_bare_orcid_when_the_lookup_fails(
    db, runner, orcid_stub
):
    """T6.1: an ORCID outage still yields a user row (name == the ORCID itself).

    Control in the same test: a working lookup in the same run keeps the real name, so
    "name == orcid" cannot be what the command always does.
    """
    broken = _orcid("broken")
    working = _orcid("working")
    orcid_stub.fail.add(broken)
    orcid_stub.set(working, name="Reachable PI", institution="Somewhere")

    result = _ok(runner.invoke(cli_app, ["seed-profile", "--orcid", broken]))
    assert "Failed to fetch ORCID profile" in result.output

    _ok(runner.invoke(cli_app, ["seed-profile", "--orcid", working]))

    fallback = db(lambda s: _user_by_orcid(s, broken))
    assert fallback is not None, "an ORCID outage must not lose the user entirely"
    assert fallback.name == broken
    assert fallback.institution is None
    assert len(db(lambda s: _jobs_for(s, fallback.id))) == 1

    good = db(lambda s: _user_by_orcid(s, working))
    assert good.name == "Reachable PI" and good.institution == "Somewhere"


def test_seed_profiles_reads_the_file_and_ignores_comments_and_blanks(
    db, runner, orcid_stub, tmp_path
):
    """T6.1: the documented bulk path (CLAUDE.md puts `# comment` lines in orcids.txt).

    Control for "comments create nothing": the two real lines in the same file do.
    """
    a, b = _orcid("file-a"), _orcid("file-b")
    orcid_stub.set(a, name="File PI A")
    orcid_stub.set(b, name="File PI B")
    listing = tmp_path / "orcids.txt"
    listing.write_text(f"# Cohort 3\n\n   \n{a}\n  {b}  \n")

    _ok(runner.invoke(cli_app, ["seed-profiles", "--file", str(listing)]))

    created = {u.orcid: u for u in db(_mine)}
    assert set(created) == {a, b}, f"unexpected user set {sorted(created)}"
    assert created[a].name == "File PI A" and created[b].name == "File PI B"
    # Whitespace is stripped before the lookup, not passed through as part of the id.
    assert orcid_stub.calls == [a, b]
    for user in created.values():
        assert len(db(lambda s, uid=user.id: _jobs_for(s, uid))) == 1


def test_seed_profiles_missing_file_exits_nonzero_and_writes_nothing(
    db, runner, orcid_stub, tmp_path
):
    """T6.1: a bad --file is a loud failure. Control: a good --file in the same test."""
    missing = tmp_path / "not-here.txt"
    result = runner.invoke(cli_app, ["seed-profiles", "--file", str(missing)])
    assert result.exit_code == 1, f"missing file must exit nonzero, got {result.exit_code}"
    assert "File not found" in result.output
    assert db(_mine) == []
    assert orcid_stub.calls == []

    good = tmp_path / "good.txt"
    real = _orcid("file-ok")
    orcid_stub.set(real, name="Present PI")
    good.write_text(real + "\n")
    _ok(runner.invoke(cli_app, ["seed-profiles", "--file", str(good)]))
    assert [u.orcid for u in db(_mine)] == [real]


# ===========================================================================
# T6.2 — admin:grant / admin:revoke
# ===========================================================================


def test_admin_grant_and_revoke_flip_is_admin_and_are_idempotent(db, runner):
    """T6.2: both directions, each run twice, plus a bystander that must not move."""
    target_orcid = _orcid("admin-target")
    bystander_orcid = _orcid("admin-bystander")

    async def _seed(session):
        await factories.make_user(session, orcid=target_orcid, name="Target PI", is_admin=False)
        await factories.make_user(
            session, orcid=bystander_orcid, name="Bystander PI", is_admin=False
        )

    db(_seed)

    def _is_admin(orcid):
        return db(lambda s: _user_by_orcid(s, orcid)).is_admin

    result = _ok(runner.invoke(cli_app, ["admin:grant", "--orcid", target_orcid]))
    assert "Granted admin to Target PI" in result.output
    assert _is_admin(target_orcid) is True
    # Idempotent: a second grant leaves it granted (and does not error).
    _ok(runner.invoke(cli_app, ["admin:grant", "--orcid", target_orcid]))
    assert _is_admin(target_orcid) is True
    # Scoped: the update is keyed on the ORCID, not applied to the table.
    assert _is_admin(bystander_orcid) is False

    _ok(runner.invoke(cli_app, ["admin:revoke", "--orcid", target_orcid]))
    assert _is_admin(target_orcid) is False
    _ok(runner.invoke(cli_app, ["admin:revoke", "--orcid", target_orcid]))
    assert _is_admin(target_orcid) is False

    # Control for revoke's scope: grant the bystander, revoke the target, and check
    # only the target moved.
    _ok(runner.invoke(cli_app, ["admin:grant", "--orcid", bystander_orcid]))
    _ok(runner.invoke(cli_app, ["admin:revoke", "--orcid", target_orcid]))
    assert _is_admin(bystander_orcid) is True
    assert _is_admin(target_orcid) is False


def test_admin_grant_on_unknown_orcid_changes_nothing_and_says_so(db, runner):
    """T6.2 control: an unknown ORCID must not silently create or promote anybody.

    (The plan says "email"; the command's flag is --orcid, which is the lookup key.)
    """
    real_orcid = _orcid("admin-real")
    ghost = _orcid("admin-ghost")

    async def _seed(session):
        await factories.make_user(session, orcid=real_orcid, name="Real PI", is_admin=False)

    db(_seed)
    before = len(db(_mine))

    result = runner.invoke(cli_app, ["admin:grant", "--orcid", ghost])
    assert f"User with ORCID {ghost} not found" in result.output
    assert db(lambda s: _user_by_orcid(s, ghost)) is None, "grant must not create users"
    assert len(db(_mine)) == before
    assert db(lambda s: _user_by_orcid(s, real_orcid)).is_admin is False

    revoke = runner.invoke(cli_app, ["admin:revoke", "--orcid", ghost])
    assert f"User with ORCID {ghost} not found" in revoke.output
    assert db(lambda s: _user_by_orcid(s, ghost)) is None

    # Positive control: the same invocation against a real ORCID does work, so the
    # "nothing happened" assertions above are not vacuous.
    _ok(runner.invoke(cli_app, ["admin:grant", "--orcid", real_orcid]))
    assert db(lambda s: _user_by_orcid(s, real_orcid)).is_admin is True


def test_admin_grant_on_unknown_orcid_should_exit_nonzero(runner):
    result = runner.invoke(cli_app, ["admin:grant", "--orcid", _orcid("admin-nobody")])
    assert result.exit_code != 0


# ===========================================================================
# T6.3 — list-users
# ===========================================================================


def test_list_users_renders_real_rows_including_the_null_institution_case(db, runner):
    """T6.3: smoke test over real data shapes, with per-row flags checked, not just
    "the command ran". Two users differing in every rendered flag pin the columns."""
    admin_orcid = _orcid("list-admin")
    plain_orcid = _orcid("list-plain")

    async def _seed(session):
        await factories.make_user(
            session,
            orcid=admin_orcid,
            name="Listed Admin",
            institution="Scripps Research",
            is_admin=True,
            onboarding_complete=True,
        )
        # institution=None exercises the "—" fallback branch.
        await factories.make_user(
            session,
            orcid=plain_orcid,
            name="Listed Plain",
            institution=None,
            is_admin=False,
            onboarding_complete=False,
        )

    db(_seed)

    result = _ok(runner.invoke(cli_app, ["list-users"]))
    out = result.output
    assert "Users" in out
    assert "Listed Admin" in out and "Listed Plain" in out
    assert admin_orcid in out and plain_orcid in out

    def _row(orcid):
        matches = [ln for ln in out.splitlines() if orcid in ln]
        assert len(matches) == 1, f"expected one rendered row for {orcid}, got {matches}"
        return matches[0]

    admin_row = _row(admin_orcid)
    plain_row = _row(plain_orcid)
    assert "Scripps Research" in admin_row
    assert "—" in plain_row, "a null institution should render the em-dash placeholder"
    # Admin + Onboarded are the last two cells. The admin row is Yes/Yes and the plain
    # row No/No, so a hard-coded flag column cannot satisfy both.
    assert (admin_row.count("Yes"), admin_row.count("No")) == (2, 0), admin_row
    assert (plain_row.count("Yes"), plain_row.count("No")) == (0, 2), plain_row


# ===========================================================================
# T6.4 — regenerate-profiles
# ===========================================================================


def test_regenerate_profiles_enqueues_exactly_one_job_per_eligible_user(db, runner):
    """T6.4 (the enqueue half): one new job per user, correct payload, none doubled."""
    orcids = [_orcid(f"regen-{i}") for i in range(3)]

    async def _seed(session):
        for i, orcid in enumerate(orcids):
            await factories.make_user(session, orcid=orcid, name=f"Regen PI {i}")

    db(_seed)

    before_jobs = db(_all_job_ids)
    all_users = db(_all_user_ids)

    result = _ok(runner.invoke(cli_app, ["regenerate-profiles"]))
    assert f"Enqueued {len(all_users)} profile regeneration jobs." in result.output

    after_jobs = db(_all_job_ids)
    new_ids = after_jobs - before_jobs
    new_jobs = db(lambda s: _jobs_by_id(s, new_ids))

    # Exactly one job per user in the table — catches both "skipped someone" and
    # "enqueued twice", which a bare count of 3 would not.
    assert {j.user_id for j in new_jobs} == all_users
    assert len(new_jobs) == len(all_users)

    by_user = {j.user_id: j for j in new_jobs}
    for user in db(_mine):
        job = by_user[user.id]
        assert job.type == "generate_profile"
        assert job.status == "pending"
        assert job.payload == {"user_id": str(user.id), "orcid": user.orcid}


def test_regenerate_profiles_ineligible_set_is_empty_because_orcid_is_not_null(db, runner):
    """T6.4 (the skip half), honestly.

    The command filters `User.orcid.isnot(None)`, but `users.orcid` is NOT NULL in both
    the model and migration 0001/0010 — so the ineligible class cannot be populated and
    the filter is unreachable. Pin that fact instead of faking a row the schema forbids;
    if someone makes the column nullable, this test goes red and real skip coverage
    becomes writable.
    """
    assert User.__table__.c.orcid.nullable is False

    async def _nullability(session, column):
        return (
            await session.execute(
                text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'users' AND column_name = :col"
                ),
                {"col": column},
            )
        ).scalar_one()

    assert db(lambda s: _nullability(s, "orcid")) == "NO"
    # Control: the same query reports YES for a genuinely nullable column, so "NO" is
    # a real answer and not an artefact of the query.
    assert db(lambda s: _nullability(s, "institution")) == "YES"

    # And the eligible half still fires, so the command is not simply doing nothing.
    orcid = _orcid("regen-eligible")

    async def _seed(session):
        await factories.make_user(session, orcid=orcid, name="Eligible PI")

    db(_seed)
    before = db(_all_job_ids)
    _ok(runner.invoke(cli_app, ["regenerate-profiles"]))
    user = db(lambda s: _user_by_orcid(s, orcid))
    new_ids = db(_all_job_ids) - before
    new_jobs = db(lambda s: _jobs_by_id(s, new_ids))
    assert [j.user_id for j in new_jobs].count(user.id) == 1


# ===========================================================================
# T6.5 — backfill-profile-revisions
# ===========================================================================


def _write_profiles(tmp_path, files: dict[str, str]):
    """files maps 'public/alpha.md' -> content, under tmp_path/profiles/."""
    for rel, content in files.items():
        path = tmp_path / "profiles" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


@pytest.fixture
def backfill_fixture(db, monkeypatch, tmp_path):
    """Registered agent `alpha` with three profile files, plus two files that must be
    skipped: one for an unregistered agent and one that is empty.

    The command resolves `profiles/<type>` relative to the process CWD, so the test
    chdirs into a temp tree; otherwise it would read (and backfill from) /app/profiles.
    """
    alpha_id = f"{AGENT_PREFIX}alpha"
    beta_id = f"{AGENT_PREFIX}beta"

    async def _seed(session):
        alpha = await factories.make_agent(session, agent_id=alpha_id, bot_name="ClitestAlphaBot")
        beta = await factories.make_agent(session, agent_id=beta_id, bot_name="ClitestBetaBot")
        return alpha.id, beta.id

    alpha_uuid, beta_uuid = db(_seed)

    _write_profiles(
        tmp_path,
        {
            f"public/{alpha_id}.md": "# Alpha public\nPeptides.\n",
            f"private/{alpha_id}.md": "# Alpha private\nUnpublished.\n",
            f"memory/{alpha_id}.md": "# Alpha memory\nNotes.\n",
            f"public/{beta_id}.md": "   \n",  # empty-after-strip: skipped
            f"public/{AGENT_PREFIX}ghost.md": "# Ghost\nNo registry row.\n",
        },
    )
    monkeypatch.chdir(tmp_path)
    return {
        "alpha_id": alpha_id,
        "beta_id": beta_id,
        "alpha_uuid": alpha_uuid,
        "beta_uuid": beta_uuid,
        "tmp_path": tmp_path,
    }


def test_backfill_creates_one_revision_per_profile_file_and_skips_the_rest(
    db, runner, backfill_fixture
):
    """T6.5 (first run): three revisions for the registered agent with non-empty files.

    Both absence assertions have their control in this same run — the ghost file and
    the empty file produce nothing while alpha's three files produce three rows.
    """
    fx = backfill_fixture
    result = _ok(runner.invoke(cli_app, ["backfill-profile-revisions"]))

    assert "Created 3 profile revisions." in result.output
    assert f"no agent '{AGENT_PREFIX}ghost'" in result.output

    revisions = db(lambda s: _revisions_for(s, fx["alpha_uuid"]))
    assert len(revisions) == 3
    by_type = {r.profile_type: r for r in revisions}
    assert set(by_type) == {"public", "private", "memory"}
    for profile_type, revision in by_type.items():
        expected = (fx["tmp_path"] / "profiles" / profile_type / f"{fx['alpha_id']}.md").read_text()
        assert revision.content == expected
        assert revision.mechanism == "pipeline"
        assert revision.change_summary == "Initial backfill from existing file"
        assert revision.changed_by_user_id is None

    # Whitespace-only file: registered agent, still no revision (control = the 3 above).
    assert db(lambda s: _revisions_for(s, fx["beta_uuid"])) == []


def test_backfill_run_twice_does_not_duplicate_any_revision(db, runner, backfill_fixture):
    """Regression for the T6.5 bug, with the evidence in one place.

    `create_revision` (src/services/profile_versioning.py) used to append
    unconditionally and the command never checked for an existing row, so a second
    backfill wrote a second byte-identical revision for every file — the duplicates
    being identical is what made them useless as history. The second run must now
    report creating nothing and leave the row count alone.
    """
    fx = backfill_fixture

    first = _ok(runner.invoke(cli_app, ["backfill-profile-revisions"]))
    assert "Created 3 profile revisions." in first.output
    # Control: the first run really did create rows, so "unchanged" would mean something.
    assert len(db(lambda s: _revisions_for(s, fx["alpha_uuid"]))) == 3

    second = _ok(runner.invoke(cli_app, ["backfill-profile-revisions"]))
    assert "Created 0 profile revisions." in second.output
    assert f"Unchanged public profile for {fx['alpha_id']}" in second.output
    revisions = db(lambda s: _revisions_for(s, fx["alpha_uuid"]))
    assert len(revisions) == 3, "a re-run must not duplicate anything"
    # One revision per (type, content) pair — no identical siblings.
    assert len({(r.profile_type, r.content) for r in revisions}) == 3


def test_backfill_is_idempotent(db, runner, backfill_fixture):
    fx = backfill_fixture
    _ok(runner.invoke(cli_app, ["backfill-profile-revisions"]))
    after_first = len(db(lambda s: _revisions_for(s, fx["alpha_uuid"])))
    assert after_first == 3
    _ok(runner.invoke(cli_app, ["backfill-profile-revisions"]))
    assert len(db(lambda s: _revisions_for(s, fx["alpha_uuid"]))) == after_first


def test_a_changed_profile_body_still_creates_a_new_revision(db, runner, backfill_fixture):
    """The other half of the idempotency guard, and the reason it keys on content.

    `create_revision` skips a write only when the newest revision for that
    (agent, profile_type) is byte-identical. A guard that also swallowed real edits
    would be a worse bug than the duplication it replaced — it would silently drop
    history — so this pins the positive case: edit one file, re-run, get one more
    revision for that type and none for the two untouched ones.
    """
    fx = backfill_fixture
    _ok(runner.invoke(cli_app, ["backfill-profile-revisions"]))
    assert len(db(lambda s: _revisions_for(s, fx["alpha_uuid"]))) == 3

    edited = "# Alpha public\nPeptides, and now also proteases.\n"
    (fx["tmp_path"] / "profiles" / "public" / f"{fx['alpha_id']}.md").write_text(
        edited, encoding="utf-8"
    )

    second = _ok(runner.invoke(cli_app, ["backfill-profile-revisions"]))
    assert "Created 1 profile revisions." in second.output

    revisions = db(lambda s: _revisions_for(s, fx["alpha_uuid"]))
    assert len(revisions) == 4, "the edited file must produce a second revision"

    by_type: dict[str, list] = {}
    for revision in revisions:
        by_type.setdefault(revision.profile_type, []).append(revision)
    assert len(by_type["public"]) == 2
    # Both the old and the new body are on record — this is history, not a replace.
    assert {r.content for r in by_type["public"]} == {"# Alpha public\nPeptides.\n", edited}
    # Control: the two files nobody touched are still at one revision each.
    assert len(by_type["private"]) == 1
    assert len(by_type["memory"]) == 1


def test_backfill_with_no_profile_directories_is_a_clean_no_op(db, runner, monkeypatch, tmp_path):
    """Absence control for the fixture above: with no files on disk the command still
    succeeds and creates nothing, so 'created 3' upthread is attributable to the files.
    """
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)

    agent_id = f"{AGENT_PREFIX}lonely"

    async def _seed(session):
        agent = await factories.make_agent(session, agent_id=agent_id, bot_name="ClitestLonelyBot")
        return agent.id

    agent_uuid = db(_seed)

    result = _ok(runner.invoke(cli_app, ["backfill-profile-revisions"]))
    assert "Created 0 profile revisions." in result.output
    assert db(lambda s: _revisions_for(s, agent_uuid)) == []


# ===========================================================================
# Harness self-check
# ===========================================================================


def test_cli_writes_to_the_test_database_not_the_configured_one(db, runner, orcid_stub, pg_url):
    """The tests above would all pass against the wrong database. Prove the CLI's own
    `_get_db()` resolved to the test DSN by reading the row back through the test
    engine, and prove the ambient config really was pointing somewhere else."""
    from src import config

    assert config.get_settings().database_url == pg_url

    orcid = _orcid(f"resolve-{uuid.uuid4().hex[:6]}")
    orcid_stub.set(orcid, name="Resolution Probe")
    _ok(runner.invoke(cli_app, ["seed-profile", "--orcid", orcid, "--no-pipeline"]))

    assert db(lambda s: _user_by_orcid(s, orcid)) is not None
