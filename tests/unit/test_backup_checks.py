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
