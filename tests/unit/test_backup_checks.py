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
import subprocess
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


def _verify_fake(counts_out="public.users|3\n", **kw):
    fake = cb.FakeRunner({
        "pg_isready": "",
        "State.Running": "true",
        "State.OOMKilled": "false",
        "-tAc": counts_out,
    })
    for k, v in kw.items():
        fake.failures[k] = v
    return fake


def test_verify_dump_tears_down_container_and_volume_on_success(tmp_path):
    dump = tmp_path / "d.dump"
    dump.write_bytes(b"x")
    fake = _verify_fake()
    cb.verify_dump(fake, CFG, STACK, dump, {"public.users": 3})
    joined = [" ".join(c) for c in fake.calls]
    assert any(j.startswith("docker rm -f -v") for j in joined), joined
    assert any(j.startswith("docker volume rm") for j in joined), joined


def test_verify_dump_tears_down_even_when_restore_fails(tmp_path):
    dump = tmp_path / "d.dump"
    dump.write_bytes(b"x")
    fake = _verify_fake()
    fake.failures["pg_restore --no-owner"] = 1
    result = cb.verify_dump(fake, CFG, STACK, dump, {"public.users": 3})
    assert result.ok is False
    joined = [" ".join(c) for c in fake.calls]
    assert any(j.startswith("docker rm -f -v") for j in joined)


def test_verify_dump_reports_count_mismatch_with_problems(tmp_path):
    dump = tmp_path / "d.dump"
    dump.write_bytes(b"x")
    fake = _verify_fake(counts_out="public.users|2\n")
    result = cb.verify_dump(fake, CFG, STACK, dump, {"public.users": 3})
    assert result.ok is False
    assert any("public.users" in p for p in result.problems)


def test_verify_dump_distinguishes_oom_from_restore_failure(tmp_path):
    # A container OOM is a harness failure, not a bad backup. Mail must say so, or
    # VERIFY_MEM tuning produces nightly false alarms that train people to ignore it.
    dump = tmp_path / "d.dump"
    dump.write_bytes(b"x")
    fake = cb.FakeRunner({"State.Running": "false", "State.OOMKilled": "true"})
    fake.failures["pg_isready"] = 13737
    result = cb.verify_dump(fake, CFG, STACK, dump, {"public.users": 3})
    assert result.ok is False
    assert result.oom is True
    assert any("OOM" in p for p in result.problems)


def test_verify_dump_refuses_to_pass_when_the_snapshot_reported_zero_tables(tmp_path):
    # compare_counts({}, {}) is legitimately (True, []), so verify_dump must refuse
    # empty source counts itself or an upstream collection bug reads as "verified".
    dump = tmp_path / "d.dump"
    dump.write_bytes(b"x")
    fake = _verify_fake(counts_out="")
    result = cb.verify_dump(fake, CFG, STACK, dump, {})
    assert result.ok is False
    assert any("ZERO tables" in p for p in result.problems)


def test_verify_dump_never_publishes_a_port(tmp_path):
    dump = tmp_path / "d.dump"
    dump.write_bytes(b"x")
    fake = _verify_fake()
    cb.verify_dump(fake, CFG, STACK, dump, {"public.users": 3})
    run_call = next(c for c in fake.calls if c[:3] == ["docker", "run", "-d"])
    assert "-p" not in run_call and "--publish" not in run_call


def test_runner_converts_a_timeout_into_commanderror(monkeypatch):
    # verify_dump promises never to raise. A bare subprocess.TimeoutExpired is not in
    # its except tuple, so it would escape, abort the nightly run and skip the other
    # stack. Normalising at the Runner seam fixes every call site at once.
    def _boom(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd=["x"], timeout=1, output="o", stderr="e")

    monkeypatch.setattr(cb.subprocess, "run", _boom)
    with pytest.raises(cb.CommandError) as caught:
        cb.Runner().run(["x"], timeout=1)
    assert caught.value.result.returncode == 124
    assert "timed out" in caught.value.result.stderr


def test_verify_dump_does_not_raise_when_the_restore_times_out(tmp_path, monkeypatch):
    monkeypatch.setattr(cb.time, "sleep", lambda _s: None)
    dump = tmp_path / "d.dump"
    dump.write_bytes(b"x")
    fake = _verify_fake()
    fake.failures["pg_restore --no-owner"] = 124
    result = cb.verify_dump(fake, CFG, STACK, dump, {"public.users": 3})
    assert result.ok is False          # returned, not raised
    assert result.problems


def test_verify_dump_bails_fast_when_the_container_exits(tmp_path, monkeypatch):
    # Without the .State.Running check the loop spins to VERIFY_TIMEOUT_SEC (1800s)
    # holding the flock. Assert it gives up after very few attempts — asserting only
    # "it failed" would still pass with the check deleted.
    monkeypatch.setattr(cb.time, "sleep", lambda _s: None)
    dump = tmp_path / "d.dump"
    dump.write_bytes(b"x")
    fake = cb.FakeRunner({"State.Running": "false", "State.OOMKilled": "false"})
    fake.failures["pg_isready"] = 1
    result = cb.verify_dump(fake, CFG, STACK, dump, {"public.users": 3})
    attempts = sum(1 for c in fake.calls if "pg_isready" in " ".join(c))
    assert result.ok is False
    assert attempts <= 2, f"readiness loop did not bail fast: {attempts} attempts"


def test_verify_dump_error_does_not_double_the_stack_name(tmp_path):
    dump = tmp_path / "d.dump"
    dump.write_bytes(b"x")
    fake = _verify_fake(counts_out="")
    result = cb.verify_dump(fake, CFG, STACK, dump, {})
    assert result.problems
    assert not result.problems[0].startswith(f"{STACK.name}: {STACK.name}:")


def _ok_result(stack="copi-python"):
    return cb.StackResult(
        stack=stack,
        dump=cb.DumpResult(Path(f"/v/{stack}.dump"), {"public.users": 3}, "snap", 1024, "abc"),
        verify=cb.VerifyResult(True, [], False, 9.5),
        offsite_ok=True,
        error=None,
    )


def _bad_result(stack="copi-blackbird"):
    return cb.StackResult(
        stack=stack,
        dump=cb.DumpResult(Path(f"/v/{stack}.dump"), {"public.users": 3}, "snap", 1024, "abc"),
        verify=cb.VerifyResult(False, ["public.users: 3 rows in snapshot, 2 restored (-1)"],
                               False, 4.0),
        offsite_ok=True,
        error=None,
    )


def test_sidecar_document_records_verification_and_checksum():
    now = datetime(2026, 8, 18, 3, 15, tzinfo=UTC)
    doc = cb.sidecar_document(_ok_result(), started=now)
    assert doc["verified"] is True
    assert doc["dump_sha256"] == "abc"
    assert doc["snapshot_id"] == "snap"
    assert doc["offsite"] is True
    assert doc["row_counts"] == {"public.users": 3}


def test_build_status_marks_overall_failure_if_any_stack_failed():
    now = datetime(2026, 8, 18, 3, 15, tzinfo=UTC)
    status = cb.build_status([_ok_result(), _bad_result()], now)
    assert status["ok"] is False
    assert status["stacks"]["copi-python"]["verified"] is True
    assert status["stacks"]["copi-blackbird"]["verified"] is False


def test_build_status_ok_when_all_pass():
    now = datetime(2026, 8, 18, 3, 15, tzinfo=UTC)
    assert cb.build_status([_ok_result()], now)["ok"] is True


def test_build_status_is_not_ok_for_an_empty_result_list():
    # all([]) is True in Python, so without the explicit `and bool(results)` guard a run
    # that processed NOTHING would write "ok": true into the very file whose job is to
    # distinguish "healthy" from "not running". This test is that guard's only defence.
    now = datetime(2026, 8, 19, 8, 0, tzinfo=UTC)
    assert cb.build_status([], now)["ok"] is False
    assert cb.build_status([], now)["stacks"] == {}


def test_stack_result_is_not_ok_without_a_dump():
    v = cb.VerifyResult(True, [], False, 1.0)
    assert cb.StackResult("s", None, v, True, None).ok is False


def test_stack_result_is_not_ok_when_an_error_is_set():
    d = cb.DumpResult(Path("/v/x.dump"), {"public.a": 1}, "snap", 10, "abc")
    v = cb.VerifyResult(True, [], False, 1.0)
    assert cb.StackResult("s", d, v, True, "boom").ok is False


def test_sidecar_document_survives_a_result_with_no_dump_or_verify():
    now = datetime(2026, 8, 19, 8, 0, tzinfo=UTC)
    doc = cb.sidecar_document(cb.StackResult("s", None, None, False, "boom"), now)
    assert doc["verified"] is False
    assert doc["dump_bytes"] == 0
    assert doc["dump_sha256"] == ""
    assert doc["row_counts"] == {}
    assert doc["error"] == "boom"


def test_failure_mail_names_only_failing_stacks_in_subject():
    now = datetime(2026, 8, 18, 3, 15, tzinfo=UTC)
    subject, body = cb.render_failure_mail([_ok_result(), _bad_result()], now)
    assert "copi-blackbird" in subject
    assert "copi-python" not in subject
    assert "FAILED" in subject
    assert "public.users" in body


def test_failure_mail_is_one_message_for_multiple_failures():
    now = datetime(2026, 8, 18, 3, 15, tzinfo=UTC)
    subject, _ = cb.render_failure_mail([_bad_result("a"), _bad_result("b")], now)
    assert "a" in subject and "b" in subject


def test_failure_mail_with_no_results_says_the_run_produced_none():
    now = datetime(2026, 8, 19, 8, 0, tzinfo=UTC)
    subject, body = cb.render_failure_mail([], now)
    assert "no-results" in subject
    assert "NO results at all" in body
    assert "systemctl status copi-backup.service" in body


def test_send_mail_uses_ses_v1_client_and_all_recipients():
    sent = {}

    class _Client:
        def send_email(self, **kw):
            sent.update(kw)
            return {"MessageId": "1"}

    cfg = cb.load_config({
        "STACKS": "a:c:d:u",
        "SES_SENDER_EMAIL": "noreply@copi.science",
        "MAIL_TO": "x@e.com y@e.com",
    })
    assert cb.send_mail(cfg, "S", "B", client_factory=lambda region: _Client()) is True
    assert sent["Source"] == "noreply@copi.science"
    assert sent["Destination"]["ToAddresses"] == ["x@e.com", "y@e.com"]
    assert sent["Message"]["Subject"]["Data"] == "S"


def test_send_mail_returns_false_and_does_not_raise_on_ses_error():
    # SES failing must not abort the run; the exit code still reports the failure.
    class _Client:
        def send_email(self, **kw):
            raise RuntimeError("ses is down")

    cfg = cb.load_config({"STACKS": "a:c:d:u", "SES_SENDER_EMAIL": "s@e.com", "MAIL_TO": "x@e.com"})
    assert cb.send_mail(cfg, "S", "B", client_factory=lambda region: _Client()) is False


def test_enough_free_space_requires_factor_multiple():
    assert cb.enough_free_space(free_bytes=300, last_dump_bytes=100, factor=3) is True
    assert cb.enough_free_space(free_bytes=299, last_dump_bytes=100, factor=3) is False


def test_enough_free_space_handles_first_run_with_no_previous_dump():
    assert cb.enough_free_space(free_bytes=10, last_dump_bytes=0, factor=3) is True


def test_sweep_never_calls_bare_volume_prune():
    # A bare prune would destroy copi_pgdata, copi-prod_pgdata,
    # copi-python_grantbot_data and collab-platform_mongodb_data.
    #
    # The fake MUST return volume ids, or the deletion loop never runs and this test is
    # vacuous: with an empty FakeRunner, mutating `docker volume rm` to
    # `docker volume prune -f` still passed.
    fake = cb.FakeRunner({"docker volume ls": "vol-a\nvol-b\n", "docker ps -aq": "cid-1\n"})
    cb.sweep(fake, CFG, datetime(2026, 8, 18, tzinfo=UTC))
    joined = [" ".join(c) for c in fake.calls]
    assert any("docker volume rm vol-a" in j for j in joined), joined
    assert any("docker rm -f -v cid-1" in j for j in joined), joined
    for call in fake.calls:
        assert call[:3] != ["docker", "volume", "prune"], call
        if call[:3] == ["docker", "volume", "ls"]:
            assert "--filter" in call and "label=copi.backup.ephemeral=true" in " ".join(call)


def test_parse_stacks_rejects_a_traversing_stack_name():
    # "..": prune() would resolve to BACKUP_ROOT's parent and delete files it never
    # created. Demonstrated during review — 5 unrelated files destroyed.
    for bad in ("..", ".", "../evil", "foo/bar", ".hidden"):
        with pytest.raises(cb.ConfigError, match="unsafe stack name"):
            cb.parse_stacks(f"{bad}:c:copi:copi")


def test_parse_stacks_rejects_unsafe_db_and_user():
    with pytest.raises(cb.ConfigError, match="unsafe database"):
        cb.parse_stacks("s:c:../evil:copi")
    with pytest.raises(cb.ConfigError, match="unsafe user"):
        cb.parse_stacks("s:c:copi:../evil")


def test_parse_stacks_still_accepts_the_real_production_names():
    stacks = cb.parse_stacks(
        "copi-python:copi-python-postgres-1:copi:copi\n"
        "copi-blackbird:copi-blackbird-postgres-1:copi:copi"
    )
    assert [s.name for s in stacks] == ["copi-python", "copi-blackbird"]


def test_prune_refuses_a_stack_dir_outside_backup_root(tmp_path):
    # Defence in depth: even if a bad name reached Config, prune must refuse.
    root = tmp_path / "backups"
    (root / "sub").mkdir(parents=True)
    cfg = cb.load_config({"STACKS": "sub:c:copi:copi", "BACKUP_ROOT": str(root)})
    escaped = cb.Config(
        stacks=[cb.Stack("..", "c", "copi", "copi")],
        backup_root=cfg.backup_root,
        retention_count=cfg.retention_count,
        retention_unverified=cfg.retention_unverified,
        verify_image=cfg.verify_image,
        verify_mem=cfg.verify_mem,
        verify_timeout_sec=cfg.verify_timeout_sec,
        free_space_factor=cfg.free_space_factor,
        offsite_cmd=cfg.offsite_cmd,
        aws_region=cfg.aws_region,
        ses_sender_email=cfg.ses_sender_email,
        mail_to=cfg.mail_to,
    )
    with pytest.raises(cb.ConfigError, match="not a direct child"):
        cb.prune(escaped, dry_run=False)


def test_run_rejects_dry_run_instead_of_silently_ignoring_it():
    # `run` has no dry mode. Accepting the flag and ignoring it hands the operator a
    # full 721MB dump from a command they believed was a no-op.
    with pytest.raises(SystemExit):
        cb.main(["run", "--dry-run"])
    with pytest.raises(SystemExit):
        cb.main(["prune", "--no-prune"])


def test_prune_deletes_dump_and_its_sidecar(tmp_path):
    stack_dir = tmp_path / "copi-python"
    stack_dir.mkdir()
    for day in range(1, 8):
        name = f"copi-python_copi_202608{day:02d}T031500Z.dump"
        (stack_dir / name).write_bytes(b"x")
        (stack_dir / f"{name}.json").write_text("{}")
    cfg = cb.load_config({"STACKS": "copi-python:c:copi:copi", "BACKUP_ROOT": str(tmp_path)})
    deleted = cb.prune(cfg, dry_run=False)
    assert len(deleted) == 2
    assert len(list(stack_dir.glob("*.dump"))) == 5
    assert len(list(stack_dir.glob("*.json"))) == 5


def test_prune_ignores_foreign_files(tmp_path):
    stack_dir = tmp_path / "copi-python"
    stack_dir.mkdir()
    (stack_dir / "copi_pre0028_20260817_203346.dump").write_bytes(b"x")
    (stack_dir / "dump.err").write_text("")
    for day in range(1, 8):
        (stack_dir / f"copi-python_copi_202608{day:02d}T031500Z.dump").write_bytes(b"x")
    cfg = cb.load_config({"STACKS": "copi-python:c:copi:copi", "BACKUP_ROOT": str(tmp_path)})
    cb.prune(cfg, dry_run=False)
    assert (stack_dir / "copi_pre0028_20260817_203346.dump").exists()
    assert (stack_dir / "dump.err").exists()


def test_prune_dry_run_deletes_nothing(tmp_path):
    stack_dir = tmp_path / "copi-python"
    stack_dir.mkdir()
    for day in range(1, 8):
        (stack_dir / f"copi-python_copi_202608{day:02d}T031500Z.dump").write_bytes(b"x")
    cfg = cb.load_config({"STACKS": "copi-python:c:copi:copi", "BACKUP_ROOT": str(tmp_path)})
    planned = cb.prune(cfg, dry_run=True)
    assert len(planned) == 2
    assert len(list(stack_dir.glob("*.dump"))) == 7
