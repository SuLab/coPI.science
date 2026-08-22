"""Pure-logic tests for scripts/migrate/preflight.py and scripts/migrate/postflight.py.

No database and no Docker: everything here exercises the decision logic — exit-code
mapping, threshold decisions, query builders, the backup verdict table, the row-count
comparison and the alembic/env.py harness analyser. The DB-facing half of both scripts is
covered against throwaway ``pf_t*`` Postgres databases (see the report for that change).

Both scripts are scripts, not an importable package (there is no ``__init__.py`` anywhere
under ``scripts/``), so they are loaded by path. Registering the module in ``sys.modules``
BEFORE ``exec_module`` is load-bearing rather than tidiness: ``@dataclass`` resolves its
annotations through ``sys.modules[cls.__module__]``, and preflight.py defines three
dataclasses.

Three tests here are regression guards for bugs that were real, were found by testing
against live fixtures, and would each have made a check fail OPEN — the worst possible
failure mode for a safety gate:

* ``test_existing_object_names_casts_relkind_to_text`` — ``pg_class.relkind`` is
  Postgres' internal ``"char"`` type and asyncpg decodes it to BYTES, so ``== "r"`` is
  always False and every table/index name collision was reported as "no collision".
* ``test_blocking_sessions_sql_uses_to_regclass`` — ``'agent_messages'::regclass`` raises
  UndefinedTable on a database that does not have the table yet, which crashed preflight
  instead of reporting.
* ``test_harness_findings_flags_sql_before_begin_transaction`` — the env.py form that made
  ``alembic upgrade`` log a full successful chain, exit 0 and commit nothing.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_MIGRATE_DIR = Path(__file__).resolve().parents[2] / "scripts" / "migrate"


def _load(module_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(module_name, _MIGRATE_DIR / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# postflight imports preflight itself, under the name "copi_migrate_preflight"; load it
# first so both this file and postflight share one instance.
pf = _load("copi_migrate_preflight", "preflight.py")
po = _load("copi_migrate_postflight", "postflight.py")


# --------------------------------------------------------------------------- #
# Exit-code contract
# --------------------------------------------------------------------------- #


def test_exit_code_constants_match_the_documented_contract():
    assert (pf.EXIT_OK, pf.EXIT_BLOCKED, pf.EXIT_WARN) == (0, 1, 2)


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([], pf.EXIT_OK),
        ([pf.PASS], pf.EXIT_OK),
        ([pf.PASS, pf.PASS], pf.EXIT_OK),
        ([pf.WARN], pf.EXIT_WARN),
        ([pf.PASS, pf.WARN], pf.EXIT_WARN),
        ([pf.BLOCK], pf.EXIT_BLOCKED),
        ([pf.PASS, pf.WARN, pf.BLOCK], pf.EXIT_BLOCKED),
        # BLOCK dominates however many WARNs precede it.
        ([pf.WARN] * 5 + [pf.BLOCK], pf.EXIT_BLOCKED),
    ],
)
def test_exit_code_for(statuses, expected):
    assert pf.exit_code_for(statuses) == expected


def test_worst_status():
    assert pf.worst_status([]) == pf.PASS
    assert pf.worst_status([pf.PASS, pf.WARN]) == pf.WARN
    assert pf.worst_status([pf.WARN, pf.BLOCK]) == pf.BLOCK


def test_preflight_report_maps_warn_to_exit_2():
    r = pf.Report("preflight")
    r.add("a", pf.PASS)
    assert r.exit_code() == 0
    r.add("b", pf.WARN)
    assert r.exit_code() == 2
    r.add("c", pf.BLOCK)
    assert r.exit_code() == 1


def test_postflight_report_maps_warn_to_exit_0():
    """postflight's contract is only 0 = verified / 1 = failed, so a WARN cannot exit 2."""
    r = pf.Report("postflight", warn_exit_code=0)
    r.add("a", pf.WARN)
    assert r.exit_code() == 0
    assert "VERIFIED WITH WARNINGS" in r.render_text()
    r.add("b", pf.BLOCK)
    assert r.exit_code() == 1
    assert "VERIFICATION FAILED" in r.render_text()


def test_report_renders_numbered_checklist_with_status_and_remediation():
    r = pf.Report("preflight")
    r.add("first thing", pf.PASS, "all good")
    r.add("second thing", pf.BLOCK, "broken", ["DROP INDEX foo;"])
    text = r.render_text()
    assert " 1. [PASS ] first thing" in text
    assert " 2. [BLOCK] second thing" in text
    assert "DROP INDEX foo;" in text
    assert "BLOCKED" in text
    assert "exit 1" in text


def test_report_to_dict_verdict_reflects_statuses_not_the_remapped_exit_code():
    r = pf.Report("postflight", warn_exit_code=0)
    r.add("a", pf.WARN)
    d = r.to_dict()
    assert d["exit_code"] == 0
    assert d["verdict"] == "warn"
    assert [c["number"] for c in d["checks"]] == [1]


async def test_add_guarded_turns_an_exception_into_a_block_rather_than_a_traceback():
    r = pf.Report("preflight")

    def explode():
        raise RuntimeError("catalog on fire")

    await r.add_guarded("some check", explode)
    assert r.statuses == [pf.BLOCK]
    assert "catalog on fire" in r.checks[0].detail
    assert r.exit_code() == 1


# --------------------------------------------------------------------------- #
# Sizing: calibration and thresholds
# --------------------------------------------------------------------------- #


def test_lock_window_constants_reproduce_the_measurements_they_were_fitted_to():
    """Measured DDL-block totals: 112 ms @10k, 747 ms @100k, 7,902 ms @1M rows.

    The model is a floor for an idle server with a warm cache, so it is allowed to be
    optimistic — but not by more than 25% at any calibration point, or the number quoted
    to the operator stops meaning anything.
    """
    for rows, measured_ms in ((10_011, 112.0), (100_011, 747.2), (1_000_011, 7901.8)):
        floor_ms, _ = pf.estimate_lock_window_ms(rows)
        assert abs(floor_ms - measured_ms) / measured_ms < 0.25, (rows, floor_ms, measured_ms)


def test_lock_window_ceiling_is_the_contention_factor_times_the_floor():
    floor_ms, ceiling_ms = pf.estimate_lock_window_ms(500_000)
    assert ceiling_ms == pytest.approx(floor_ms * pf.LOCK_WINDOW_CONTENTION_FACTOR)


def test_lock_window_is_monotone_and_defined_at_zero_and_for_nonsense_input():
    assert pf.estimate_lock_window_ms(0)[0] == pf.LOCK_WINDOW_FIXED_MS
    # A negative count can only come from a broken caller; it must not produce a
    # smaller-than-fixed (or negative) estimate that would read as "instant".
    assert pf.estimate_lock_window_ms(-5)[0] == pf.LOCK_WINDOW_FIXED_MS
    prev = -1.0
    for rows in (0, 1_000, 100_000, 1_000_000, 10_000_000):
        floor_ms = pf.estimate_lock_window_ms(rows)[0]
        assert floor_ms > prev
        prev = floor_ms


def test_sizing_status_passes_for_a_short_window():
    rows = 100_000
    _, ceiling = pf.estimate_lock_window_ms(rows)
    status, note = pf.sizing_status(rows, ceiling)
    assert status == pf.PASS
    assert "lock window" in note


def test_sizing_status_warns_once_the_window_needs_scheduling():
    status, note = pf.sizing_status(900_000, pf.LOCK_WINDOW_WARN_MS + 1)
    assert status == pf.WARN
    assert "schedule a window" in note


def test_sizing_status_warns_that_it_is_extrapolating_beyond_the_calibrated_range():
    rows = pf.LOCK_WINDOW_CALIBRATED_MAX_ROWS + 1
    status, note = pf.sizing_status(rows, 1.0)
    assert status == pf.WARN
    assert "extrapolation" in note


def test_index_growth_fraction_matches_the_measured_growth():
    """Measured post/pre total relation size: 3608/2016, 34/19 MB, 339/188 MB -> ~+80%."""
    for pre, post in ((2016.0, 3608.0), (19.0, 34.0), (188.0, 339.0)):
        measured = (post - pre) / pre
        assert abs(pf.INDEX_GROWTH_FRACTION - measured) < 0.05, (pre, post, measured)


# --------------------------------------------------------------------------- #
# Revision classification
# --------------------------------------------------------------------------- #


def test_revision_status_blocks_when_the_database_has_never_been_stamped():
    status, reason = pf.revision_status(None, "0023")
    assert status == pf.BLOCK
    assert "never been stamped" in reason


def test_revision_status_passes_at_the_target():
    status, reason = pf.revision_status("0023", "0023")
    assert status == pf.PASS
    assert "no-op" in reason


@pytest.mark.parametrize("rev", ["0018", "0019", "0020", "0021"])
def test_revision_status_passes_at_a_supported_starting_point(rev):
    assert pf.revision_status(rev, "0023")[0] == pf.PASS


@pytest.mark.parametrize("rev", ["0001", "0017", "0022", "abcdef"])
def test_revision_status_blocks_anywhere_else(rev):
    status, reason = pf.revision_status(rev, "0023")
    assert status == pf.BLOCK
    assert rev in reason


def test_supported_start_revisions_are_exactly_the_documented_set():
    assert pf.SUPPORTED_START_REVISIONS == (
        "0018", "0019", "0020", "0021", "0023", "0024", "0025", "0026", "0027", "0028",
        "0029", "0030", "0031", "0032", "0033", "0034", "0035",
    )
    assert pf.DEFAULT_TARGET == "0036"


def test_every_post_branch_revision_is_a_supported_start():
    """Regression: DEFAULT_TARGET moved to 0028, then 0029, 0030, 0031 and 0032, each time
    without the revision immediately behind the new target joining
    SUPPORTED_START_REVISIONS -- 0026/0027 went stale for the 0028 move, and left
    unguarded the same mistake would recur for every later move too. A database
    stamped one migration behind DEFAULT_TARGET (the most likely real-world starting
    point for whatever migration is newest) was neither `current == target` nor a
    supported start, and revision_status() BLOCKED the very migration each move added.

    Rather than re-pin the specific revisions that bit us historically, derive the
    check from REVISION_ORDER/DEFAULT_TARGET: every revision this branch has actually
    produced a deployment against (i.e. at or after its own original head, 0023) must
    be a supported start and must PASS up to DEFAULT_TARGET. This way a fourth
    occurrence of the same mistake fails this test instead of slipping through.

    0022 is the one documented, deliberate exception (see
    test_0021_is_supported_because_that_is_origin_mains_own_alembic_head): no
    deployment ever reaches it, so it is intentionally absent from
    SUPPORTED_START_REVISIONS and excluded here too.
    """
    branch_floor = "0023"
    documented_exceptions = {"0022"}
    checked = []
    for rev in pf.REVISION_ORDER[:-1]:
        if rev < branch_floor or rev in documented_exceptions:
            continue
        checked.append(rev)
        assert rev in pf.SUPPORTED_START_REVISIONS, rev
        status, _reason = pf.revision_status(rev, pf.DEFAULT_TARGET)
        assert status == pf.PASS, (rev, status)
    # Sanity check that this loop actually exercised something, so a future
    # REVISION_ORDER/DEFAULT_TARGET refactor can't silently turn it into a no-op.
    assert checked


def test_0021_is_supported_because_that_is_origin_mains_own_alembic_head():
    """Regression guard: do not narrow this list back to ("0018", "0019").

    origin/main's head is 0021 (PR19 merged 0019, 0020 and 0021), so any deployment
    tracking main is stamped 0021. The first version of this allowlist blocked exactly
    that state -- preflight refused the one starting point main itself produces, which
    was found by auditing the branch for a PR into main rather than by any test.

    0022 is deliberately NOT here: no deployment reaches it (main stops at 0021, this
    branch's head is 0023) and the path has not been exercised from there. An allowlist
    for a safety gate should contain what was tested, not what seems plausible.
    """
    assert "0021" in pf.SUPPORTED_START_REVISIONS
    assert "0020" in pf.SUPPORTED_START_REVISIONS
    assert "0022" not in pf.SUPPORTED_START_REVISIONS


def test_sizing_does_not_quote_the_0019_index_build_once_0019_has_run():
    """The row-scaled lock estimate only applies while 0019 is still pending."""
    assert pf.POST_0019_STARTS == ("0020", "0021")
    for rev in pf.POST_0019_STARTS:
        assert rev in pf.SUPPORTED_START_REVISIONS


# --------------------------------------------------------------------------- #
# lock_timeout resolution
# --------------------------------------------------------------------------- #


def test_lock_timeout_prefers_the_environment_variable():
    value, source = pf.resolve_lock_timeout_ms({"ALEMBIC_LOCK_TIMEOUT_MS": "250"}, "irrelevant")
    assert value == "250"
    assert "environment" in source


def test_lock_timeout_falls_back_to_the_env_py_default():
    src = 'LOCK_TIMEOUT_MS = os.environ.get("ALEMBIC_LOCK_TIMEOUT_MS", "10000")\n'
    value, source = pf.resolve_lock_timeout_ms({}, src)
    assert value == "10000"
    assert "env.py" in source


def test_lock_timeout_reports_zero_when_env_py_sets_none():
    value, source = pf.resolve_lock_timeout_ms({}, "def do_run_migrations(c): pass\n")
    assert value == "0"
    assert "wait forever" in source


def test_lock_timeout_is_unknown_when_env_py_cannot_be_read():
    assert pf.resolve_lock_timeout_ms({}, None) == ("unknown", "could not determine")


# --------------------------------------------------------------------------- #
# The migration-harness analyser
# --------------------------------------------------------------------------- #

_BROKEN_ENV_PY = '''
LOCK_TIMEOUT_MS = os.environ.get("ALEMBIC_LOCK_TIMEOUT_MS", "10000")


def do_run_migrations(connection: Connection) -> None:
    if LOCK_TIMEOUT_MS and LOCK_TIMEOUT_MS != "0":
        connection.exec_driver_sql(f"SET lock_timeout = {int(LOCK_TIMEOUT_MS)}")
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()
'''

_FIXED_ENV_PY = '''
LOCK_TIMEOUT_MS = os.environ.get("ALEMBIC_LOCK_TIMEOUT_MS", "10000")


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()
'''


def test_harness_findings_flags_sql_before_begin_transaction():
    """The bug: `alembic upgrade` logs the whole chain, exits 0, and commits nothing.

    Reproduced against a throwaway database: with this form, `alembic upgrade 0018`
    reported every revision applied and left the database with ZERO tables and no
    alembic_version; with ALEMBIC_LOCK_TIMEOUT_MS=0 (which skips the statement) the same
    command left 25 tables and version 0018.
    """
    findings = pf.harness_findings(_BROKEN_ENV_PY)
    assert len(findings) == 1
    assert "connection.exec_driver_sql" in findings[0]
    assert "before context.begin_transaction()" in findings[0]


def test_harness_findings_clean_when_the_timeout_is_set_on_the_engine_instead():
    assert pf.harness_findings(_FIXED_ENV_PY) == []


@pytest.mark.parametrize("method", ["execute", "exec_driver_sql", "scalar", "scalars", "begin"])
def test_harness_findings_covers_every_autobegin_method(method):
    src = f"def do_run_migrations(connection):\n    connection.{method}('x')\n"
    assert len(pf.harness_findings(src)) == 1


def test_harness_findings_ignores_calls_on_something_other_than_the_connection():
    src = "def do_run_migrations(connection):\n    other.execute('x')\n    context.configure(1)\n"
    assert pf.harness_findings(src) == []


def test_harness_findings_ignores_other_functions():
    src = "def run_migrations_offline():\n    connection.execute('x')\n"
    assert pf.harness_findings(src) == []


def test_harness_findings_survives_a_syntax_error():
    findings = pf.harness_findings("def broken(:\n")
    assert len(findings) == 1
    assert "could not parse" in findings[0]


def test_the_real_env_py_in_this_tree_is_clean():
    """Regression guard. This file has carried the bug once; it must not again."""
    src = pf.read_env_py()
    assert src is not None, "alembic/env.py should be readable from the repo root"
    assert pf.harness_findings(src) == []


# --------------------------------------------------------------------------- #
# Blocking-session classification
# --------------------------------------------------------------------------- #


def _session(state="active", xact_age_s=0.0, pid=1):
    return {
        "pid": pid,
        "state": state,
        "usename": "copi",
        "application_name": "",
        "xact_age_s": xact_age_s,
        "query_age_s": xact_age_s,
        "query": "SELECT 1",
        "holds_agent_messages_lock": False,
    }


def test_no_sessions_passes():
    assert pf.blocking_sessions_status([])[0] == pf.PASS


@pytest.mark.parametrize("age", [0.01, 1.0, 3600.0])
def test_idle_in_transaction_blocks_at_any_age(age):
    status, detail = pf.blocking_sessions_status([_session("idle in transaction", age)])
    assert status == pf.BLOCK
    assert "queues ahead of new readers" in detail


def test_idle_in_transaction_aborted_also_blocks():
    status, _ = pf.blocking_sessions_status([_session("idle in transaction (aborted)", 2.0)])
    assert status == pf.BLOCK


def test_a_long_open_transaction_blocks():
    status, detail = pf.blocking_sessions_status(
        [_session("active", pf.DEFAULT_MAX_TOLERABLE_XACT_AGE_S + 1)]
    )
    assert status == pf.BLOCK
    assert "open longer than" in detail


def test_a_short_open_transaction_only_warns():
    """A sub-threshold query releases its lock on its own; blocking on it is crying wolf."""
    status, _ = pf.blocking_sessions_status(
        [_session("active", pf.DEFAULT_MAX_TOLERABLE_XACT_AGE_S - 1)]
    )
    assert status == pf.WARN


def test_an_active_session_with_no_transaction_only_warns():
    status, detail = pf.blocking_sessions_status([_session("active", 0.0)])
    assert status == pf.WARN
    assert "blocked (not blocking)" in detail


def test_the_threshold_is_configurable():
    session = [_session("active", 4.0)]
    assert pf.blocking_sessions_status(session, max_xact_age_s=10.0)[0] == pf.WARN
    assert pf.blocking_sessions_status(session, max_xact_age_s=1.0)[0] == pf.BLOCK


# --------------------------------------------------------------------------- #
# Backup verdict table
# --------------------------------------------------------------------------- #


def _good_backup(**over):
    facts = pf.BackupFacts(
        path="/tmp/copi.sql.gz",
        exists=True,
        size_bytes=8_000,
        age_hours=1.0,
        fmt="gzip",
        scanned=True,
        has_agent_messages_ddl=True,
        has_agent_messages_data=True,
        live_agent_messages_rows=1_000,
    )
    for k, v in over.items():
        setattr(facts, k, v)
    return facts


def test_a_recent_data_bearing_dump_passes():
    status, notes = pf.evaluate_backup(_good_backup())
    assert status == pf.PASS
    assert "data section present" in notes[0]


def test_no_backup_blocks_and_says_why_rollback_needs_one():
    status, notes = pf.evaluate_backup(pf.BackupFacts())
    assert status == pf.BLOCK
    assert "no backup found" in notes[0]
    assert any("pg_dump" in n for n in notes)


def test_a_stale_backup_blocks():
    status, notes = pf.evaluate_backup(_good_backup(age_hours=48.0), max_age_hours=24.0)
    assert status == pf.BLOCK
    assert "48.0h old" in notes[0]


def test_a_backup_exactly_at_the_age_threshold_is_accepted():
    assert pf.evaluate_backup(_good_backup(age_hours=24.0), max_age_hours=24.0)[0] == pf.PASS


def test_a_truncated_backup_blocks_on_size():
    status, notes = pf.evaluate_backup(_good_backup(size_bytes=200), min_bytes=1024)
    assert status == pf.BLOCK
    assert "under the" in notes[0]


def test_an_unreadable_backup_blocks():
    """A truncated gzip raises EOFError, not OSError; inspect_backup records it here."""
    status, notes = pf.evaluate_backup(
        _good_backup(read_error="EOFError: Compressed file ended before the end-of-stream marker")
    )
    assert status == pf.BLOCK
    assert "EOFError" in notes[0]


def test_an_unrecognisable_file_blocks():
    status, notes = pf.evaluate_backup(_good_backup(fmt="unknown"))
    assert status == pf.BLOCK
    assert "not a recognisable pg_dump output" in notes[0]


def test_a_dump_of_some_other_database_blocks():
    status, notes = pf.evaluate_backup(_good_backup(has_agent_messages_ddl=False))
    assert status == pf.BLOCK
    assert "not a dump of this database" in notes[0]


def test_a_schema_only_dump_blocks_when_the_live_table_has_rows():
    status, notes = pf.evaluate_backup(
        _good_backup(has_agent_messages_data=False, live_agent_messages_rows=42)
    )
    assert status == pf.BLOCK
    assert "--schema-only" in notes[0]


def test_a_dump_with_no_data_section_is_fine_when_the_table_is_genuinely_empty():
    """Not crying wolf: an empty agent_messages produces no COPY section, correctly."""
    status, _ = pf.evaluate_backup(
        _good_backup(has_agent_messages_data=False, live_agent_messages_rows=0)
    )
    assert status == pf.PASS


def test_a_custom_format_archive_warns_because_pg_restore_is_not_installed():
    status, notes = pf.evaluate_backup(_good_backup(fmt="custom"))
    assert status == pf.WARN
    assert "pg_restore" in notes[0]


def test_the_override_warns_loudly_and_never_passes():
    status, notes = pf.evaluate_backup(pf.BackupFacts(override_reason="EBS snapshot 20:00Z"))
    assert status == pf.WARN
    assert "EBS snapshot 20:00Z" in notes[0]
    assert "destroys agent_messages.content" in notes[0]


def test_backup_thresholds_are_the_documented_defaults():
    assert pf.DEFAULT_BACKUP_MAX_AGE_HOURS == 24.0
    assert pf.DEFAULT_BACKUP_MIN_BYTES == 1024


# --------------------------------------------------------------------------- #
# Dump scanning and discovery (filesystem only, no database)
# --------------------------------------------------------------------------- #

_PLAIN_DUMP_WITH_DATA = """\
--
-- PostgreSQL database dump
--
CREATE TABLE public.agent_messages (id uuid NOT NULL);
COPY public.agent_messages (id, content) FROM stdin;
1\thello
\\.
"""

_PLAIN_DUMP_SCHEMA_ONLY = """\
--
-- PostgreSQL database dump
--
CREATE TABLE public.agent_messages (id uuid NOT NULL);
CREATE INDEX ix_agent_messages_run_posted ON public.agent_messages USING btree (id);
"""


def test_scan_dump_text_finds_a_copy_data_section():
    assert pf.scan_dump_text(_PLAIN_DUMP_WITH_DATA) == (True, True)


def test_scan_dump_text_distinguishes_a_schema_only_dump():
    assert pf.scan_dump_text(_PLAIN_DUMP_SCHEMA_ONLY) == (True, False)


def test_scan_dump_text_finds_an_insert_style_data_section():
    body = "CREATE TABLE public.agent_messages (id uuid);\nINSERT INTO public.agent_messages VALUES (1);\n"
    assert pf.scan_dump_text(body) == (True, True)


def test_scan_dump_text_reports_nothing_for_an_unrelated_dump():
    assert pf.scan_dump_text("CREATE TABLE public.users (id uuid);\n") == (False, False)


def test_inspect_backup_records_a_truncated_gzip_instead_of_raising(tmp_path):
    """This crashed the whole script before the fix; a bad backup must be a finding."""
    import gzip

    good = tmp_path / "d.sql.gz"
    good.write_bytes(gzip.compress(_PLAIN_DUMP_WITH_DATA.encode()))
    truncated = tmp_path / "t.sql.gz"
    truncated.write_bytes(good.read_bytes()[:20])

    facts = pf.inspect_backup(truncated, live_rows=5, override=None)
    assert facts.exists
    assert facts.fmt == "gzip"
    assert facts.read_error is not None
    assert pf.evaluate_backup(facts)[0] == pf.BLOCK


def test_inspect_backup_reads_a_real_gzipped_plain_dump(tmp_path):
    import gzip

    p = tmp_path / "d.sql.gz"
    p.write_bytes(gzip.compress(_PLAIN_DUMP_WITH_DATA.encode()))
    facts = pf.inspect_backup(p, live_rows=5, override=None)
    assert (facts.fmt, facts.has_agent_messages_ddl, facts.has_agent_messages_data) == (
        "gzip",
        True,
        True,
    )
    # min_bytes=1: this hand-written dump gzips to a couple of hundred bytes, well under
    # the production floor, and the floor is not what is under test here.
    assert pf.evaluate_backup(facts, min_bytes=1)[0] == pf.PASS


def test_inspect_backup_sniffs_the_custom_format_magic(tmp_path):
    p = tmp_path / "d.dump"
    p.write_bytes(b"PGDMP" + b"\x00" * 4096)
    assert pf.inspect_backup(p, live_rows=5, override=None).fmt == "custom"


def test_inspect_backup_marks_a_non_dump_as_unknown(tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("just some notes\n" * 100)
    assert pf.inspect_backup(p, live_rows=5, override=None).fmt == "unknown"


def test_inspect_backup_on_a_missing_path_reports_not_exists(tmp_path):
    facts = pf.inspect_backup(tmp_path / "nope.sql.gz", live_rows=5, override=None)
    assert not facts.exists


def test_find_backup_takes_the_newest_file_in_a_directory(tmp_path):
    import os
    import time

    old = tmp_path / "old.sql.gz"
    new = tmp_path / "new.sql.gz"
    old.write_bytes(b"x" * 10)
    new.write_bytes(b"y" * 10)
    now = time.time()
    os.utime(old, (now - 7200, now - 7200))
    os.utime(new, (now - 60, now - 60))
    assert pf.find_backup(str(tmp_path)) == new


def test_find_backup_ignores_files_that_are_not_dumps(tmp_path):
    (tmp_path / "readme.md").write_text("hi")
    assert pf.find_backup(str(tmp_path)) is None


def test_find_backup_accepts_a_file_path_directly(tmp_path):
    p = tmp_path / "explicit.sql"
    p.write_text("x")
    assert pf.find_backup(str(p)) == p


def test_find_backup_returns_none_for_a_path_that_does_not_exist(tmp_path):
    assert pf.find_backup(str(tmp_path / "missing.sql.gz")) is None


# --------------------------------------------------------------------------- #
# Legacy-row inventory
# --------------------------------------------------------------------------- #


def test_legacy_inventory_passes_when_nothing_will_be_empty():
    assert pf.legacy_inventory_status(0, 0)[0] == pf.PASS


def test_legacy_inventory_names_slack_recoverable_rows():
    status, note = pf.legacy_inventory_status(12, 0)
    assert status == pf.WARN
    assert "12 Slack-recoverable" in note
    assert "UNRECOVERABLE" not in note


def test_legacy_inventory_shouts_about_permanently_unrecoverable_rows():
    status, note = pf.legacy_inventory_status(0, 7)
    assert status == pf.WARN
    assert "7 PERMANENTLY UNRECOVERABLE" in note


def test_legacy_inventory_reports_both_buckets_separately():
    _, note = pf.legacy_inventory_status(3, 4)
    assert "3 Slack-recoverable" in note
    assert "4 PERMANENTLY UNRECOVERABLE" in note


# --------------------------------------------------------------------------- #
# Row-count comparison (the preflight -> postflight handoff)
# --------------------------------------------------------------------------- #


def test_identical_counts_compare_clean():
    counts = {"users": 3, "agent_messages": 18}
    ok, problems = pf.compare_row_counts(counts, dict(counts))
    assert ok and problems == []


def test_row_loss_always_fails():
    ok, problems = pf.compare_row_counts({"agent_messages": 18}, {"agent_messages": 17})
    assert not ok
    assert "1 rows LOST" in problems[0]


def test_row_loss_fails_even_with_allow_row_growth():
    ok, _ = pf.compare_row_counts(
        {"agent_messages": 18}, {"agent_messages": 17}, allow_growth=True
    )
    assert not ok


def test_row_growth_fails_by_default_because_the_migration_inserts_nothing():
    ok, problems = pf.compare_row_counts({"agent_messages": 18}, {"agent_messages": 19})
    assert not ok
    assert "a writer was live" in problems[0]


def test_row_growth_can_be_allowed_explicitly():
    ok, problems = pf.compare_row_counts(
        {"agent_messages": 18}, {"agent_messages": 19}, allow_growth=True
    )
    assert ok and problems == []


def test_a_table_that_disappeared_fails():
    ok, problems = pf.compare_row_counts({"users": 3}, {})
    assert not ok
    assert "now MISSING" in problems[0]


def test_tables_the_chain_creates_are_not_flagged_as_unexpected():
    """0020 creates pi_dm_messages and 0022 creates three cohort tables, so they are
    absent from the preflight snapshot by construction. Flagging them would be a false
    failure on every single successful migration."""
    ok, problems = pf.compare_row_counts(
        {"users": 3},
        {"users": 3, "pi_dm_messages": 0, "cohorts": 0},
        expected_new=po.CHAIN_CREATED_TABLES,
    )
    assert ok and problems == []


def test_a_table_the_chain_does_not_create_is_still_flagged():
    ok, problems = pf.compare_row_counts(
        {"users": 3}, {"users": 3, "mystery": 9}, expected_new=po.CHAIN_CREATED_TABLES
    )
    assert not ok
    assert "did not exist before" in problems[0]


def test_chain_created_tables_is_derived_from_planned_objects_not_relisted():
    assert po.CHAIN_CREATED_TABLES == frozenset(
        o.name for o in pf.PLANNED_OBJECTS
        if o.kind == "table" and o.revision in po.VERIFIED_REVISIONS
    )
    assert po.CHAIN_CREATED_TABLES == {
        "pi_dm_messages",
        "cohorts",
        "cohort_memberships",
        "cohort_audit_events",
    }
    # 0025 also creates a table (opportunity_assessments), but postflight has not been
    # extended to verify it yet — it must stay out of CHAIN_CREATED_TABLES until it does,
    # or a real drop of that table would be masked as "expected to be absent".
    assert "opportunity_assessments" not in po.CHAIN_CREATED_TABLES


# --------------------------------------------------------------------------- #
# URL handling
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw",
    [
        "postgresql://copi:copi@postgres:5432/copi",
        "postgres://copi:copi@postgres:5432/copi",
        "postgresql+psycopg2://copi:copi@postgres:5432/copi",
        "postgresql+psycopg://copi:copi@postgres:5432/copi",
        "postgresql+asyncpg://copi:copi@postgres:5432/copi",
    ],
)
def test_normalize_async_url_forces_the_asyncpg_driver(raw):
    out = pf.normalize_async_url(raw)
    assert out == "postgresql+asyncpg://copi:copi@postgres:5432/copi"


def test_normalize_async_url_leaves_an_unrecognised_scheme_alone():
    assert pf.normalize_async_url("sqlite+aiosqlite:///x.db") == "sqlite+aiosqlite:///x.db"


def test_redact_url_hides_the_password_but_keeps_the_rest():
    out = pf.redact_url("postgresql+asyncpg://copi:s3cret@postgres:5432/copi")
    assert out == "postgresql+asyncpg://copi:***@postgres:5432/copi"
    assert "s3cret" not in out


def test_redact_url_is_a_no_op_when_there_is_no_password():
    assert pf.redact_url("postgresql+asyncpg://postgres:5432/copi").count("***") == 0


# --------------------------------------------------------------------------- #
# Planned objects: the collision check's input
# --------------------------------------------------------------------------- #


def test_planned_objects_between_0018_and_the_target_is_everything():
    assert set(pf.planned_objects_between("0018", pf.DEFAULT_TARGET)) == set(pf.PLANNED_OBJECTS)


def test_planned_objects_between_0019_and_0023_excludes_what_0019_already_made():
    planned = pf.planned_objects_between("0019", "0023")
    names = {o.name for o in planned}
    # Already present at 0019, so their existence is correct rather than a collision.
    assert "content" not in names
    assert "uq_agent_messages_run_ts" not in names
    assert "ix_agent_messages_run_posted" not in names
    # Still to come.
    assert "pi_dm_messages" in names
    assert "cohorts" in names
    assert "ix_agent_messages_run_created" in names


def test_planned_objects_between_a_revision_and_itself_is_empty():
    assert pf.planned_objects_between("0023", "0023") == ()


def test_planned_objects_kinds_are_all_understood_by_the_collision_check():
    assert {o.kind for o in pf.PLANNED_OBJECTS} <= {
        "table",
        "column",
        "index",
        "constraint",
        "type",
    }


def test_every_planned_column_names_its_table():
    for obj in pf.PLANNED_OBJECTS:
        if obj.kind in {"column", "index", "constraint"}:
            assert obj.table, obj


def test_planned_objects_matches_what_the_migration_files_actually_create():
    """Drift guard: re-derive the object names from alembic/versions/0019..0025 and
    compare. Hardcoding the list keeps the check readable; this keeps it honest."""
    import re

    versions_dir = Path(__file__).resolve().parents[2] / "alembic" / "versions"
    patterns = {
        "index": re.compile(r'create_index\(\s*\n?\s*"([^"]+)"'),
        "table": re.compile(r'create_table\(\s*\n?\s*"([^"]+)"'),
        "column": re.compile(r'add_column\(\s*\n?\s*"[^"]+",\s*\n?\s*sa\.Column\("([^"]+)"'),
        "constraint": re.compile(r'create_unique_constraint\(\s*\n?\s*"([^"]+)"'),
    }
    for revision in ("0019", "0020", "0021", "0022", "0023", "0024", "0025"):
        matches = list(versions_dir.glob(f"{revision}_*.py"))
        assert len(matches) == 1, (revision, matches)
        source = matches[0].read_text()
        upgrade = source.split("def upgrade()", 1)[1].split("def downgrade()", 1)[0]
        declared = {
            o.name for o in pf.PLANNED_OBJECTS if o.revision == revision
        }
        for kind, pattern in patterns.items():
            found = set(pattern.findall(upgrade))
            missing = found - declared
            assert not missing, (
                f"{revision} creates {kind}(s) {sorted(missing)} that PLANNED_OBJECTS "
                "does not list — the collision check would miss them"
            )
        # Inline UniqueConstraint(...) inside create_table, plus inline sa.Enum types.
        for name in re.findall(r'sa\.UniqueConstraint\([^)]*name="([^"]+)"', upgrade):
            assert name in declared, (revision, name)
        for name in re.findall(r'name="([a-z_]+_enum)"', upgrade):
            assert name in declared, (revision, name)


# --------------------------------------------------------------------------- #
# The alembic script-directory guard (mirrors scripts/ci.sh, but relates it to the
# stamped revision). No database: check_alembic_scripts only reads files.
# --------------------------------------------------------------------------- #


def _fake_versions(tmp_path, revisions):
    """revisions: list of (revision_id, down_revision or None, filename)."""
    versions = tmp_path / "alembic" / "versions"
    versions.mkdir(parents=True)
    for rev, down, filename in revisions:
        down_literal = f'"{down}"' if down else "None"
        (versions / filename).write_text(
            f'revision: str = "{rev}"\ndown_revision: Union[str, None] = {down_literal}\n'
        )
    return tmp_path


LINEAR_TREE = [
    ("0018", "0017", "0018_a.py"),
    ("0019", "0018", "0019_b.py"),
    ("0020", "0019", "0020_c.py"),
    ("0017", None, "0017_root.py"),
]


async def test_alembic_scripts_passes_on_a_single_head_matching_the_target(monkeypatch, tmp_path):
    monkeypatch.setattr(pf, "REPO_ROOT", _fake_versions(tmp_path, LINEAR_TREE))
    title, status, detail, _rem, data = await pf.check_alembic_scripts("0018", "0020")
    assert status == pf.PASS
    assert data["heads"] == ["0020"]
    assert "4 migration files" in detail


async def test_alembic_scripts_blocks_on_duplicate_revision_ids(monkeypatch, tmp_path):
    """The historical case: 0019_agent_message_content.py and 0019_add_cohorts.py both
    declared revision = "0019". A targeted upgrade applies whichever sorts last and
    stamps the database as fully migrated."""
    tree = [*LINEAR_TREE, ("0019", "0018", "0019_add_cohorts.py")]
    monkeypatch.setattr(pf, "REPO_ROOT", _fake_versions(tmp_path, tree))
    _title, status, detail, rem, data = await pf.check_alembic_scripts("0018", "0020")
    assert status == pf.BLOCK
    assert "DUPLICATE revision ids" in detail
    assert "0019" in data["duplicates"]
    assert sorted(data["duplicates"]["0019"]) == ["0019_add_cohorts.py", "0019_b.py"]
    assert any("uniq -d" in r for r in rem)


async def test_alembic_scripts_blocks_on_two_heads(monkeypatch, tmp_path):
    tree = [*LINEAR_TREE, ("0021", "0019", "0021_branch.py")]
    monkeypatch.setattr(pf, "REPO_ROOT", _fake_versions(tmp_path, tree))
    _title, status, detail, _rem, data = await pf.check_alembic_scripts("0018", "0020")
    assert status == pf.BLOCK
    assert "Expected exactly one head" in detail
    assert sorted(data["heads"]) == ["0020", "0021"]


async def test_alembic_scripts_warns_when_the_head_is_not_the_requested_target(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(pf, "REPO_ROOT", _fake_versions(tmp_path, LINEAR_TREE))
    _title, status, detail, rem, _data = await pf.check_alembic_scripts("0018", "0019")
    assert status == pf.WARN
    assert "not the requested target" in detail
    assert any("--target 0020" in r for r in rem)


async def test_alembic_scripts_blocks_when_the_stamp_exists_in_no_migration_file(
    monkeypatch, tmp_path
):
    """A database migrated by a different branch: nothing here can be trusted about it."""
    monkeypatch.setattr(pf, "REPO_ROOT", _fake_versions(tmp_path, LINEAR_TREE))
    _title, status, detail, _rem, _data = await pf.check_alembic_scripts("0099", "0020")
    assert status == pf.BLOCK
    assert "which no migration file defines" in detail


async def test_alembic_scripts_tolerates_an_unstamped_database(monkeypatch, tmp_path):
    monkeypatch.setattr(pf, "REPO_ROOT", _fake_versions(tmp_path, LINEAR_TREE))
    _title, status, _detail, _rem, _data = await pf.check_alembic_scripts(None, "0020")
    assert status == pf.PASS


async def test_alembic_scripts_agrees_with_the_real_tree():
    """The live repo must have exactly one head and no duplicate ids — the same property
    scripts/ci.sh gates on, asserted here against the same files."""
    _title, status, _detail, _rem, data = await pf.check_alembic_scripts(None, pf.DEFAULT_TARGET)
    assert status == pf.PASS
    assert data["heads"] == [pf.DEFAULT_TARGET]


def test_revision_order_covers_the_supported_range():
    assert pf.REVISION_ORDER[0] == "0018"
    assert pf.REVISION_ORDER[-1] == pf.DEFAULT_TARGET
    assert list(pf.REVISION_ORDER) == sorted(pf.REVISION_ORDER)


# --------------------------------------------------------------------------- #
# Query builders
# --------------------------------------------------------------------------- #


def test_duplicate_groups_sql_finds_every_group_in_one_pass():
    sql = " ".join(pf.DUPLICATE_GROUPS_SQL.split())
    # All groups, not the first one: GROUP BY + HAVING, with no LIMIT anywhere.
    assert "GROUP BY simulation_run_id, message_ts" in sql
    assert "HAVING count(*) > 1" in sql
    assert "LIMIT" not in sql.upper()
    # The row ids, so the operator can act without a second query.
    assert "array_agg(id::text ORDER BY created_at, id)" in sql
    # NULL message_ts is exempt from a Postgres UNIQUE constraint, so including those
    # rows would report duplicates that cannot fail the migration.
    assert "WHERE message_ts IS NOT NULL" in sql


def test_the_remediation_sql_partitions_on_the_constraint_columns():
    for sql in (pf.DEDUPE_DELETE_SQL, pf.DEDUPE_NULL_SQL):
        assert "PARTITION BY simulation_run_id, message_ts" in sql
        assert "ORDER BY created_at, id" in sql
        assert "rn > 1" in sql
        # Wrapped in a transaction so a partial remediation cannot be left behind.
        assert sql.strip().startswith("--")
        assert "BEGIN;" in sql and "COMMIT;" in sql


def test_the_two_remediations_differ_in_exactly_the_destructive_step():
    assert "DELETE FROM agent_messages" in pf.DEDUPE_DELETE_SQL
    assert "DELETE FROM agent_messages" not in pf.DEDUPE_NULL_SQL
    assert "SET message_ts = NULL" in pf.DEDUPE_NULL_SQL


def test_blocking_sessions_sql_uses_to_regclass():
    """Regression guard. `'agent_messages'::regclass` raises UndefinedTable on a database
    that does not have the table yet, which crashed preflight instead of reporting."""
    sql = pf.BLOCKING_SESSIONS_SQL
    assert "to_regclass('public.agent_messages')" in sql
    # Comment lines mention the broken form on purpose; strip them before asserting.
    executable = "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("--")
    )
    assert "::regclass" not in executable


def test_blocking_sessions_sql_excludes_our_own_sessions():
    sql = pf.BLOCKING_SESSIONS_SQL
    assert "a.pid <> pg_backend_pid()" in sql
    assert ":app_name" in sql
    assert pf.APPLICATION_NAME


def test_existing_object_names_casts_relkind_to_text():
    """Regression guard for a check that failed OPEN.

    pg_class.relkind is Postgres' internal "char" type and asyncpg decodes it to BYTES,
    so `row["k"] == "r"` was always False and the table/index sets were always empty —
    every collision was reported as "none exists yet". Verified against a fixture with a
    pre-existing ix_agent_messages_run_posted, which really does abort migration 0019.
    """
    import inspect

    source = inspect.getsource(pf.existing_object_names)
    assert "c.relkind::text" in source
    assert "c.relkind AS k" not in source


def test_snapshot_row_counts_uses_exact_counts_not_reltuples():
    import inspect

    source = inspect.getsource(pf.snapshot_row_counts)
    assert "count(*)" in source
    assert "reltuples" not in source.split('"""')[2]


# --------------------------------------------------------------------------- #
# postflight's expectations
# --------------------------------------------------------------------------- #


def test_postflight_expects_agent_id_to_have_become_nullable():
    """0019 RELAXES agent_messages.agent_id. If postflight expected NOT NULL it would
    pass on a database where 0019 never ran."""
    spec = [c for c in po.EXPECTED_COLUMNS if c[:2] == ("agent_messages", "agent_id")]
    assert len(spec) == 1
    assert spec[0][3] is True


def test_postflight_expects_the_0023_columns_to_stay_nullable_and_unbackfilled():
    for column in ("synthesis_validated", "evidence_pmid_count", "evidence_pub_count"):
        spec = [c for c in po.EXPECTED_COLUMNS if c[:2] == ("researcher_profiles", column)]
        assert len(spec) == 1, column
        assert spec[0][3] is True, column


def test_postflight_pins_the_content_columns_as_not_null_with_their_server_defaults():
    expected = {
        "content": "''::text",
        "sender_name": "''::character varying",
        "is_bot": "true",
        "posted_at": "'0'::double precision",
    }
    for column, default in expected.items():
        spec = [c for c in po.EXPECTED_COLUMNS if c[:2] == ("agent_messages", column)]
        assert len(spec) == 1, column
        assert spec[0][3] is False, column
        assert spec[0][4] == default, column


def test_postflight_keeps_the_partial_predicate_in_the_expected_index_definition():
    """Without the predicate the index is a different, much larger object."""
    assert "WHERE (slack_ts IS NOT NULL)" in po.EXPECTED_INDEXES["ix_agent_messages_run_slack_ts"]


def test_postflight_expects_an_index_for_every_index_the_chain_creates():
    # Scoped to the revisions postflight actually verifies (po.VERIFIED_REVISIONS):
    # PLANNED_OBJECTS also carries 0025's two indexes for preflight's collision check,
    # but postflight has no EXPECTED_INDEXES entries for them yet (see C9/VERIFIED_REVISIONS).
    planned = {
        o.name for o in pf.PLANNED_OBJECTS
        if o.kind in {"index", "constraint"} and o.revision in po.VERIFIED_REVISIONS
    }
    assert planned <= set(po.EXPECTED_INDEXES)


def test_postflight_expects_a_table_for_every_table_the_chain_creates():
    planned = {
        o.name for o in pf.PLANNED_OBJECTS
        if o.kind == "table" and o.revision in po.VERIFIED_REVISIONS
    }
    assert planned == set(po.EXPECTED_TABLES)


def test_postflight_expects_a_column_for_every_column_the_chain_creates():
    planned = {
        (o.table, o.name) for o in pf.PLANNED_OBJECTS
        if o.kind == "column" and o.revision in po.VERIFIED_REVISIONS
    }
    expected = {(t, c) for (t, c, _dt, _n, _d) in po.EXPECTED_COLUMNS}
    assert planned <= expected


def test_postflight_expects_the_enum_the_chain_creates():
    planned = {o.name for o in pf.PLANNED_OBJECTS if o.kind == "type"}
    assert planned == set(po.EXPECTED_ENUMS)
    assert po.EXPECTED_ENUMS["pi_dm_direction_enum"] == ("inbound", "outbound")


def test_must_be_non_null_is_derived_from_expected_columns():
    assert set(po.MUST_BE_NON_NULL) == {
        (t, c) for (t, c, _dt, nullable, _d) in po.EXPECTED_COLUMNS if not nullable
    }
    assert ("agent_messages", "content") in po.MUST_BE_NON_NULL
    assert ("agent_messages", "agent_id") not in po.MUST_BE_NON_NULL


def test_drift_classification_fails_on_what_a_dropped_object_produces():
    """Sabotage-verified: dropping a column yields add_column, dropping an index yields
    add_index, relaxing a NOT NULL yields modify_nullable."""
    for op in ("add_column", "add_index", "add_table", "modify_nullable"):
        assert op in po.DRIFT_FAIL_OPS


def test_drift_classification_ignores_only_the_pre_existing_noise():
    """25 differences are reported on a correctly migrated database; all are the DB
    having something the ORM never declared, which is harmless."""
    for op in ("remove_index", "remove_constraint", "add_table_comment"):
        assert op in po.DRIFT_IGNORED_OPS
    assert not (po.DRIFT_FAIL_OPS & po.DRIFT_IGNORED_OPS)


def test_postflight_status_aliases_are_the_same_tokens_preflight_uses():
    assert (po.PASS, po.WARN, po.FAIL) == (pf.PASS, pf.WARN, pf.BLOCK)


# --------------------------------------------------------------------------- #
# CLI surface
# --------------------------------------------------------------------------- #


def test_preflight_parser_defaults():
    args = pf.build_parser().parse_args([])
    assert args.database_url is None
    # Derived, not re-pinned: the literal tripwire lives in
    # test_supported_start_revisions_are_exactly_the_documented_set, and a second
    # copy here only ever produced a third place to forget on a target bump.
    assert args.target == pf.DEFAULT_TARGET
    assert args.json is False
    assert args.snapshot is None
    assert args.backup_path is None
    assert args.backup_max_age_hours == pf.DEFAULT_BACKUP_MAX_AGE_HOURS
    assert args.backup_min_bytes == pf.DEFAULT_BACKUP_MIN_BYTES
    assert args.backup_verified_elsewhere is None
    assert args.max_xact_age_s == pf.DEFAULT_MAX_TOLERABLE_XACT_AGE_S
    assert args.statement_timeout_ms == 60_000


def test_preflight_parser_accepts_the_documented_interface():
    args = pf.build_parser().parse_args(
        [
            "--database-url",
            "postgresql://u:p@h:5432/d",
            "--target",
            "0022",
            "--json",
            "--snapshot",
            "/tmp/s.json",
            "--backup-path",
            "/tmp/b.sql.gz",
            "--backup-max-age-hours",
            "6",
            "--max-duplicate-groups",
            "5",
        ]
    )
    assert args.database_url == "postgresql://u:p@h:5432/d"
    assert args.target == "0022"
    assert args.json is True
    assert args.snapshot == "/tmp/s.json"
    assert args.backup_path == "/tmp/b.sql.gz"
    assert args.backup_max_age_hours == 6.0
    assert args.max_duplicate_groups == 5


def test_postflight_parser_defaults_and_shape():
    args = po.build_parser().parse_args([])
    assert args.database_url is None
    assert args.target == po.DEFAULT_TARGET
    assert args.json is False
    assert args.snapshot is None
    assert args.allow_row_growth is False


def test_postflight_parser_accepts_the_documented_interface():
    args = po.build_parser().parse_args(
        ["--database-url", "postgresql://u:p@h/d", "--target", "0023",
         "--json", "--snapshot", "/tmp/s.json", "--allow-row-growth"]
    )
    assert args.snapshot == "/tmp/s.json"
    assert args.allow_row_growth is True


def test_both_scripts_share_one_preflight_module_instance():
    """postflight loads preflight by path; two live copies would give two sets of
    dataclasses and two sets of constants that could silently disagree."""
    assert po._pf is pf


def test_resolve_database_url_prefers_the_cli_then_the_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://e:e@h:5432/env_db")
    assert pf.resolve_database_url("postgresql://c:c@h:5432/cli_db").endswith("/cli_db")
    assert pf.resolve_database_url(None).endswith("/env_db")
    assert pf.resolve_database_url(None).startswith("postgresql+asyncpg://")


def test_write_snapshot_warns_when_no_path_is_given():
    class _Args:
        snapshot = None
        target = "0023"

    status, detail, remediation = pf.write_snapshot(
        _Args(), pf.Report("preflight"), {"users": 1}, "0018"
    )
    assert status == pf.WARN
    assert "postflight cannot compare" in detail
    assert any("--snapshot" in r for r in remediation)


def test_write_snapshot_round_trips_through_compare_row_counts(tmp_path):
    import json

    class _Args:
        target = "0023"

        def __init__(self, path):
            self.snapshot = str(path)

    counts = {"users": 3, "agent_messages": 18}
    status, detail, _ = pf.write_snapshot(
        _Args(tmp_path / "snap.json"), pf.Report("preflight"), counts, "0018"
    )
    assert status == pf.PASS
    assert "18" in detail or "21" in detail
    payload = json.loads((tmp_path / "snap.json").read_text())
    assert payload["kind"] == "preflight-snapshot"
    assert payload["current_revision"] == "0018"
    assert payload["row_counts"] == counts
    ok, problems = pf.compare_row_counts(payload["row_counts"], counts)
    assert ok and problems == []


def test_write_snapshot_blocks_when_the_path_is_not_writable(tmp_path):
    class _Args:
        target = "0023"
        snapshot = "/proc/definitely/not/writable/snap.json"

    status, detail, _ = pf.write_snapshot(_Args(), pf.Report("preflight"), {"users": 1}, "0018")
    assert status == pf.BLOCK
    assert "could not write snapshot" in detail


# --------------------------------------------------------------------------- #
# check_ambiguous_revision: THREE files in this repo's history declared 0019
#
# Enumerated from git rather than memory — every historical blob under
# alembic/versions/ was parsed for its declared revision id:
#
#   0019_agent_message_content.py     (a7659b4, this chain)   -> agent_messages.content
#   0019_add_cohorts.py               (b00b0e6, cohort-agent-isolation) -> cohorts
#   0019_add_hidden_to_proposals.py   (4037b79, coPI-podcast) -> thread_decisions.hidden
#
# Why naming the right one matters, measured on fixtures in each state:
#   * cohort 0019  -> `alembic upgrade 0023` dies at 0022 with DuplicateTableError,
#     revision stays 0019, nothing applied. Loud and safe.
#   * podcast 0019 -> `alembic upgrade 0023` EXITS 0 AND STAMPS 0023 while
#     agent_messages.content and uq_agent_messages_run_ts do not exist. Alembic
#     reports total success on a database the app cannot run against.
# The remediations are opposites (drop the cohort tables vs. leave the two `hidden`
# columns alone), so a check that guessed would send the operator the wrong way.
# --------------------------------------------------------------------------- #


def _stub_schema(monkeypatch, *, content: bool, cohorts: bool, hidden: bool) -> None:
    async def _column_exists(_conn, table: str, column: str) -> bool:
        if (table, column) == ("agent_messages", "content"):
            return content
        if (table, column) == ("thread_decisions", "hidden"):
            return hidden
        return False

    async def _table_exists(_conn, name: str) -> bool:
        return cohorts if name == "cohorts" else False

    monkeypatch.setattr(pf, "column_exists", _column_exists)
    monkeypatch.setattr(pf, "table_exists", _table_exists)


async def test_ambiguous_revision_is_not_applicable_away_from_0019(monkeypatch):
    _stub_schema(monkeypatch, content=False, cohorts=False, hidden=False)
    _title, status, detail, _rem, _data = await pf.check_ambiguous_revision(None, "0018")
    assert status == pf.PASS
    assert "not applicable" in detail


async def test_ambiguous_revision_passes_when_the_content_columns_are_present(monkeypatch):
    # The only state that may proceed. Note it passes even alongside the other
    # signatures: content present means the right 0019 ran, whatever else is there.
    _stub_schema(monkeypatch, content=True, cohorts=True, hidden=True)
    _title, status, detail, rem, data = await pf.check_ambiguous_revision(None, "0019")
    assert status == pf.PASS
    assert "0019_agent_message_content" in detail
    assert rem == []
    assert data["agent_messages.content"] is True


async def test_ambiguous_revision_names_the_cohort_0019_and_says_to_drop_its_tables(monkeypatch):
    _stub_schema(monkeypatch, content=False, cohorts=True, hidden=False)
    _title, status, detail, rem, _data = await pf.check_ambiguous_revision(None, "0019")
    joined = "\n".join(rem)
    assert status == pf.BLOCK
    assert "0019_add_cohorts" in detail
    assert "0019_add_hidden_to_proposals" not in detail
    assert "DROP TABLE IF EXISTS cohort_memberships, cohorts CASCADE;" in joined
    # It must warn about the specific way the upgrade fails, so the operator
    # recognises the DuplicateTableError when they see it.
    assert "0022" in joined


async def test_ambiguous_revision_names_the_podcast_0019_and_leaves_its_columns_alone(monkeypatch):
    _stub_schema(monkeypatch, content=False, cohorts=False, hidden=True)
    _title, status, detail, rem, _data = await pf.check_ambiguous_revision(None, "0019")
    joined = "\n".join(rem)
    assert status == pf.BLOCK
    assert "0019_add_hidden_to_proposals" in detail
    assert "0019_add_cohorts" not in detail
    # The opposite advice from the cohort case: these columns are orphaned but
    # harmless, so the remediation must NOT tell anyone to drop the cohort tables,
    # and must prefer leaving data in place.
    assert "cohort_memberships" not in joined
    assert "left in place" in joined


async def test_ambiguous_revision_refuses_to_guess_on_an_unknown_0019(monkeypatch):
    _stub_schema(monkeypatch, content=False, cohorts=False, hidden=False)
    _title, status, detail, rem, _data = await pf.check_ambiguous_revision(None, "0019")
    joined = "\n".join(rem)
    assert status == pf.BLOCK
    assert "unrecognised 0019" in detail
    # Failing closed is not enough: it must not hand over a remediation that was
    # written for a different database's history.
    assert "inspect the schema by hand" in joined
    assert "DROP TABLE" not in joined


async def test_ambiguous_revision_distinguishes_all_three_signatures(monkeypatch):
    """One assertion that the check actually probes three things, not two.

    Guards the regression where a third 0019 existed but the check only knew about
    two, so a podcast-0019 database was correctly blocked and then handed the
    cohort remediation.
    """
    seen = []
    for cohorts, hidden in ((True, False), (False, True), (False, False)):
        _stub_schema(monkeypatch, content=False, cohorts=cohorts, hidden=hidden)
        _t, status, detail, _rem, _d = await pf.check_ambiguous_revision(None, "0019")
        assert status == pf.BLOCK
        seen.append(detail)
    assert len(set(seen)) == 3, "each of the three states must be diagnosed differently"
