"""Pure-logic tests for scripts/backup/copi_backup.py.

No database, no Docker, no network: everything here exercises decision logic —
config parsing, dump-name round-tripping, the retention selector, the row-count
comparison, and the status document. The Docker-facing half is covered by
scripts/backup/failure_injection.sh, which runs on the host and is not part of
scripts/ci.sh.

Like scripts/migrate, scripts/backup is a script and not an importable package
(there is no ``__init__.py`` anywhere under ``scripts/``), so the module is loaded
by path. Registering it in ``sys.modules`` BEFORE ``exec_module`` is load-bearing
rather than tidiness: ``@dataclass`` resolves its annotations through
``sys.modules[cls.__module__]``, and this module defines five dataclasses.
"""

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "scripts" / "backup" / "copi_backup.py"


def _load():
    spec = importlib.util.spec_from_file_location("copi_backup", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["copi_backup"] = module
    spec.loader.exec_module(module)
    return module


cb = _load()


def test_parse_stacks_reads_four_colon_separated_fields():
    stacks = cb.parse_stacks("copi-python:copi-python-postgres-1:copi:copi")
    assert len(stacks) == 1
    assert stacks[0].name == "copi-python"
    assert stacks[0].container == "copi-python-postgres-1"
    assert stacks[0].db == "copi"
    assert stacks[0].user == "copi"


def test_parse_stacks_handles_multiline_and_indentation():
    raw = """copi-python:copi-python-postgres-1:copi:copi
        copi-blackbird:copi-blackbird-postgres-1:copi:copi"""
    stacks = cb.parse_stacks(raw)
    assert [s.name for s in stacks] == ["copi-python", "copi-blackbird"]


def test_parse_stacks_rejects_wrong_field_count():
    with pytest.raises(cb.ConfigError, match="expected 4 colon-separated fields"):
        cb.parse_stacks("copi-python:copi-python-postgres-1:copi")


def test_parse_stacks_rejects_duplicate_stack_names():
    raw = "a:c1:db:u\na:c2:db:u"
    with pytest.raises(cb.ConfigError, match="duplicate stack name"):
        cb.parse_stacks(raw)


def test_parse_stacks_rejects_empty():
    with pytest.raises(cb.ConfigError, match="no stacks configured"):
        cb.parse_stacks("   \n  ")


def test_load_config_applies_documented_defaults():
    cfg = cb.load_config({"STACKS": "a:c:d:u"})
    assert cfg.retention_count == 5
    assert cfg.retention_unverified == 2
    assert cfg.verify_image == "postgres:15"
    assert cfg.free_space_factor == 3
    assert cfg.offsite_cmd == ""


def test_load_config_clamps_retention_count_to_at_least_one():
    # A configured 0 would let the pruner empty the directory; the floor is structural.
    cfg = cb.load_config({"STACKS": "a:c:d:u", "RETENTION_COUNT": "0"})
    assert cfg.retention_count == 1


def test_load_config_rejects_non_integer_retention():
    with pytest.raises(cb.ConfigError, match="RETENTION_COUNT"):
        cb.load_config({"STACKS": "a:c:d:u", "RETENTION_COUNT": "five"})


def test_load_config_splits_mail_recipients_on_whitespace():
    cfg = cb.load_config({"STACKS": "a:c:d:u", "MAIL_TO": "x@e.com  y@e.com\nz@e.com"})
    assert cfg.mail_to == ["x@e.com", "y@e.com", "z@e.com"]


def _dump(stack="copi-python", db="copi", day=1, verified=True):
    when = datetime(2026, 8, day, 3, 15, 0, tzinfo=UTC)
    name = cb.dump_name(stack, db, when)
    if not verified:
        name += ".unverified"
    parsed = cb.parse_dump_name(name)
    assert parsed is not None, name
    return parsed


def test_dump_name_is_utc_and_round_trips():
    when = datetime(2026, 8, 18, 3, 15, 0, tzinfo=UTC)
    name = cb.dump_name("copi-python", "copi", when)
    assert name == "copi-python_copi_20260818T031500Z.dump"
    parsed = cb.parse_dump_name(name)
    assert parsed.stack == "copi-python"
    assert parsed.db == "copi"
    assert parsed.taken == when
    assert parsed.verified is True


def test_parse_dump_name_marks_unverified_suffix():
    parsed = cb.parse_dump_name("copi-python_copi_20260818T031500Z.dump.unverified")
    assert parsed.verified is False


def test_parse_dump_name_rejects_foreign_files():
    # These are the hand-made pre-deploy dumps; the pruner must never match them.
    assert cb.parse_dump_name("copi_pre0028_20260817_203346.dump") is None
    assert cb.parse_dump_name("dump.err") is None
    assert cb.parse_dump_name("copi-python_copi_20260818T031500Z.json") is None
    assert cb.parse_dump_name("copi-python_copi_20260818T031500Z.dump.partial") is None


def test_select_for_deletion_keeps_n_most_recent_verified():
    dumps = [_dump(day=d) for d in range(1, 9)]  # 8 verified
    doomed = cb.select_for_deletion(dumps, keep_verified=5, keep_unverified=2)
    assert sorted(d.taken.day for d in doomed) == [1, 2, 3]


def test_select_for_deletion_keeps_n_most_recent_unverified():
    dumps = [_dump(day=d) for d in range(1, 6)]
    dumps += [_dump(day=d, verified=False) for d in range(10, 15)]
    doomed = cb.select_for_deletion(dumps, keep_verified=5, keep_unverified=2)
    assert all(not d.verified for d in doomed)
    assert sorted(d.taken.day for d in doomed) == [10, 11, 12]


def test_select_for_deletion_deletes_nothing_when_no_verified_dump_exists():
    # The floor: unverified copies may be the only copies in existence.
    dumps = [_dump(day=d, verified=False) for d in range(1, 6)]
    assert cb.select_for_deletion(dumps, keep_verified=5, keep_unverified=2) == []


def test_select_for_deletion_is_per_stack():
    dumps = [_dump(stack="copi-python", day=d) for d in range(1, 8)]
    dumps += [_dump(stack="copi-blackbird", day=d) for d in range(1, 3)]
    doomed = cb.select_for_deletion(dumps, keep_verified=5, keep_unverified=2)
    assert {d.stack for d in doomed} == {"copi-python"}
    assert len(doomed) == 2


def test_select_for_deletion_empty_input():
    assert cb.select_for_deletion([], keep_verified=5, keep_unverified=2) == []


def test_compare_counts_accepts_exact_parity():
    ok, problems = cb.compare_counts({"public.users": 5}, {"public.users": 5})
    assert ok is True
    assert problems == []


def test_compare_counts_flags_shortfall_with_table_and_delta():
    ok, problems = cb.compare_counts({"public.users": 100}, {"public.users": 97})
    assert ok is False
    assert len(problems) == 1
    assert "public.users" in problems[0]
    assert "-3" in problems[0]


def test_compare_counts_flags_surplus():
    ok, problems = cb.compare_counts({"public.users": 100}, {"public.users": 104})
    assert ok is False
    assert "+4" in problems[0]


def test_compare_counts_flags_table_missing_from_restore():
    ok, problems = cb.compare_counts({"public.users": 5, "public.jobs": 2}, {"public.users": 5})
    assert ok is False
    assert "MISSING" in problems[0]
    assert "public.jobs" in problems[0]


def test_compare_counts_flags_unexpected_table_in_restore():
    ok, problems = cb.compare_counts({}, {"public.ghost": 1})
    assert ok is False
    assert "not in the source snapshot" in problems[0]


def test_compare_counts_reports_every_problem_sorted():
    ok, problems = cb.compare_counts(
        {"public.b": 1, "public.a": 1}, {"public.b": 0, "public.a": 0}
    )
    assert ok is False
    assert len(problems) == 2
    assert problems[0].startswith("public.a")
    assert problems[1].startswith("public.b")


def test_compare_counts_both_empty_is_parity():
    # A database with no user tables is odd but not a backup failure.
    assert cb.compare_counts({}, {}) == (True, [])


STACK = cb.Stack(name="copi-python", container="copi-python-postgres-1", db="copi", user="copi")
CFG = cb.load_config({"STACKS": "copi-python:copi-python-postgres-1:copi:copi"})


def test_pg_dump_argv_writes_to_a_container_path_not_stdout():
    # A custom-format archive needs random access for its TOC; a pipe is not
    # seekable. scripts/migrate/run_migration.sh:176 documents the failure this
    # avoids. -f, never stdout redirection.
    argv = cb.pg_dump_argv(STACK, "00000009-00008D07-1", "/tmp/copi_backup_1.dump")
    assert "-f" in argv
    assert "/tmp/copi_backup_1.dump" in argv
    assert "-Fc" in argv
    assert "--snapshot=00000009-00008D07-1" in argv
    assert argv[0] == "docker"
    assert "-t" not in argv  # a TTY would corrupt binary output


def test_pg_dump_argv_is_niced():
    argv = cb.pg_dump_argv(STACK, "s", "/tmp/x.dump")
    joined = " ".join(argv)
    assert "nice" in joined
    assert "ionice" in joined


def test_psql_argv_uses_tuples_only_unaligned_output():
    argv = cb.psql_argv(STACK, "SELECT 1")
    assert "-tAc" in argv
    assert "SELECT 1" in argv


def test_verify_run_argv_is_isolated_and_capped():
    argv = cb.verify_run_argv(CFG, STACK, "vol-1", "/host/x.dump", "copi-verify-1", "pw")
    joined = " ".join(argv)
    assert "--network" in argv and "none" in argv
    assert f"--memory={CFG.verify_mem}" in joined or "--memory" in argv
    # swap disabled: equal to --memory, so a runaway restore fails instead of thrashing
    assert f"--memory-swap={CFG.verify_mem}" in joined or "--memory-swap" in argv
    assert "copi.backup.ephemeral=true" in joined
    assert ":ro" in joined  # dump mounted read-only
    assert "-p" not in argv  # never publish a port


def test_verify_run_argv_uses_stack_credentials_not_hardcoded():
    stack = cb.Stack(name="s", container="c", db="otherdb", user="otheruser")
    joined = " ".join(cb.verify_run_argv(CFG, stack, "v", "/h.dump", "n", "pw"))
    assert "POSTGRES_DB=otherdb" in joined
    assert "POSTGRES_USER=otheruser" in joined


def test_fake_runner_records_calls():
    fake = cb.FakeRunner({"docker version": "ok"})
    fake.run(["docker", "version"])
    assert fake.calls == [["docker", "version"]]


def test_snapshot_sql_disables_idle_timeout_before_exporting():
    # If idle_in_transaction_session_timeout were non-zero the server would kill the
    # session while pg_dump runs, invalidating the snapshot. Both servers are at 0
    # today; setting it explicitly means a future server-config change cannot
    # silently break backups.
    sql = cb.SNAPSHOT_OPEN_SQL
    assert "REPEATABLE READ" in sql
    assert "idle_in_transaction_session_timeout" in sql
    assert sql.index("idle_in_transaction_session_timeout") < sql.index("pg_export_snapshot")


def test_count_sql_for_tables_builds_one_union_per_table():
    sql = cb.counts_sql(["public.users", "public.jobs"])
    assert sql.count("UNION ALL") == 1
    assert "public.users" in sql and "public.jobs" in sql
    assert "count(*)" in sql


def test_docker_exec_omits_i_by_default_and_never_adds_t():
    argv = cb.docker_exec("c", ["pg_dump"])
    assert "-i" not in argv
    assert "-t" not in argv


def test_snapshot_session_argv_sets_dash_i():
    # Without -i, Docker does not attach stdin, psql sees EOF and exits before it can
    # be sent anything — the session dies with "closed unexpectedly". Verified live.
    argv = cb.snapshot_session_argv(STACK)
    assert "-i" in argv
    assert "-t" not in argv
    assert argv.index("-i") < argv.index(STACK.container)
    assert "psql" in argv and "-tA" in argv


def test_terminate_sql_appends_missing_semicolon():
    # psql executes a following \echo immediately when the statement is still
    # buffered, so the sentinel would arrive before the rows. Verified live: same
    # query returns 30 rows terminated, 0 rows unterminated.
    assert cb.terminate_sql("SELECT 1") == "SELECT 1;"
    assert cb.terminate_sql("  SELECT 1  \n") == "SELECT 1;"


def test_terminate_sql_leaves_an_already_terminated_statement_alone():
    assert cb.terminate_sql("SELECT 1;") == "SELECT 1;"
    assert cb.terminate_sql("\nSELECT 1;\n") == "SELECT 1;"


def test_terminate_sql_on_empty_input_is_empty():
    assert cb.terminate_sql("   \n ") == ""


def test_plan_sql_constants_survive_termination():
    # The two constants that are fed through _send are both unterminated as written.
    assert cb.terminate_sql(cb.COUNT_TABLES_SQL).endswith(";")
    assert cb.terminate_sql(cb.counts_sql(["public.a"])).endswith(";")


def test_count_sql_for_no_tables_is_a_no_op_select():
    assert cb.counts_sql([]) == ""


def test_parse_counts_output_reads_pipe_separated_pairs():
    out = "public.users|412\npublic.jobs|0\n"
    assert cb.parse_counts_output(out) == {"public.users": 412, "public.jobs": 0}


def test_parse_counts_output_ignores_blank_lines():
    assert cb.parse_counts_output("public.a|1\n\n  \n") == {"public.a": 1}


def test_parse_counts_output_rejects_malformed_rows():
    with pytest.raises(cb.BackupError, match="unparseable count row"):
        cb.parse_counts_output("public.a|not-a-number\n")


def test_snapshot_session_terminate_is_idempotent_and_safe():
    # _terminate() must be safe to call on a session that was never opened (no proc)
    # and safe to call twice (idempotent). Python does NOT call __exit__ when __enter__
    # raises, so _terminate() must not raise and must clean up even if called before
    # the session is fully initialized.
    session = cb.SnapshotSession(STACK)
    # Call on unopened session - no proc yet
    session._terminate()
    # Call again - should be no-op
    session._terminate()
    assert session._proc is None


class _FakeSession:
    def __init__(self, stack):
        self.stack = stack
        self.snapshot_id = "00000009-00008D07-1"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def counts(self):
        return {"public.users": 3}


def test_dump_stack_removes_container_temp_even_when_dump_fails(tmp_path):
    fake = cb.FakeRunner()
    fake.failures["pg_dump -U"] = 1
    with pytest.raises(cb.CommandError):
        cb.dump_stack(
            fake, CFG, STACK, tmp_path,
            datetime(2026, 8, 18, 3, 15, tzinfo=UTC),
            session_factory=_FakeSession,
        )
    joined = [" ".join(c) for c in fake.calls]
    assert any("rm -f /tmp/copi_backup_" in j for j in joined), joined


def test_dump_stack_leaves_no_partial_file_when_dump_fails(tmp_path):
    fake = cb.FakeRunner()
    fake.failures["pg_dump -U"] = 1
    with pytest.raises(cb.CommandError):
        cb.dump_stack(
            fake, CFG, STACK, tmp_path,
            datetime(2026, 8, 18, 3, 15, tzinfo=UTC),
            session_factory=_FakeSession,
        )
    assert list(tmp_path.glob("*.partial")) == []


def test_dump_stack_verifies_toc_inside_container_before_copying(tmp_path):
    fake = cb.FakeRunner()
    fake.failures["pg_restore -l"] = 1
    with pytest.raises(cb.CommandError):
        cb.dump_stack(
            fake, CFG, STACK, tmp_path,
            datetime(2026, 8, 18, 3, 15, tzinfo=UTC),
            session_factory=_FakeSession,
        )
    order = [" ".join(c) for c in fake.calls]
    toc = next(i for i, j in enumerate(order) if "pg_restore -l" in j)
    assert not any("docker cp" in j for j in order[:toc])


class _FailingSession:
    """A snapshot session that fails in __enter__, like a dead database."""

    def __init__(self, stack):
        self.stack = stack
        self.snapshot_id = ""

    def __enter__(self):
        raise cb.BackupError("snapshot export failed")

    def __exit__(self, *exc):
        return None

    def counts(self):
        return {}


class _PartialWritingRunner(cb.FakeRunner):
    """docker cp really writes a truncated file, then fails.

    Needed because a plain FakeRunner never touches the disk, so the earlier
    "leaves no partial" tests were vacuous — no .partial ever existed and removing
    the unlink() would not have failed them.
    """

    def run(self, argv, *, timeout=None, check=True):
        self.calls.append(list(argv))
        if argv[:2] == ["docker", "cp"]:
            Path(argv[-1]).write_bytes(b"TRUNCATED-ARCHIVE")
            result = cb.Completed(argv, 1, "", "cp failed midway")
            if check:
                raise cb.CommandError(result)
            return result
        return cb.Completed(argv, 0, "", "")


def test_dump_stack_unlinks_a_real_partial_when_docker_cp_fails(tmp_path):
    fake = _PartialWritingRunner()
    with pytest.raises(cb.CommandError):
        cb.dump_stack(
            fake, CFG, STACK, tmp_path,
            datetime(2026, 8, 18, 3, 15, tzinfo=UTC),
            session_factory=_FakeSession,
        )
    assert list(tmp_path.glob("*.partial")) == [], "a truncated .partial survived"
    assert list(tmp_path.glob("*.dump")) == [], "a truncated file took the final name"


def test_dump_stack_cleans_container_temp_when_the_session_itself_fails(tmp_path):
    # The brief calls out "an exception from the snapshot session" as a path the temp
    # cleanup must survive; _FakeSession never fails, so it was never exercised.
    fake = cb.FakeRunner()
    with pytest.raises(cb.BackupError):
        cb.dump_stack(
            fake, CFG, STACK, tmp_path,
            datetime(2026, 8, 18, 3, 15, tzinfo=UTC),
            session_factory=_FailingSession,
        )
    joined = [" ".join(c) for c in fake.calls]
    assert any("rm -f /tmp/copi_backup_" in j for j in joined), joined
    assert list(tmp_path.glob("*.partial")) == []


def test_sha256_file_matches_hashlib(tmp_path):
    import hashlib
    p = tmp_path / "x.bin"
    p.write_bytes(b"hello copi")
    assert cb.sha256_file(p) == hashlib.sha256(b"hello copi").hexdigest()
