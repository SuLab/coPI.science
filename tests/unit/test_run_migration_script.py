"""Stub-driven tests for scripts/migrate/run_migration.sh.

The script is run for real under bash, with fake `docker` and `git` binaries first on
PATH. The fake docker logs every argument vector to $STUB_LOG and answers by substring,
so each test asserts on the exact docker calls the script would make in production.
No real docker, database or network is touched.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "migrate" / "run_migration.sh"

_HEAD = "a" * 40
_FOREIGN_CONTAINER = re.compile(r"(?<![\w-])agent-run(?![\w-])")

# Dispatch order matters: the default-target read also names preflight.py, and the
# stamp read's python text is checked before the generic cases.
_FAKE_DOCKER = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB_LOG"
args="$*"
case "$args" in
  *".build_info.json"*)
    exit_code="${STUB_BUILD_INFO_EXIT:-0}"
    [ "$exit_code" = 0 ] || { echo "FileNotFoundError: /app/.build_info.json" >&2; exit "$exit_code"; }
    echo "$STUB_COMMIT ${STUB_IMAGE_DIRTY:-1}"; exit 0 ;;
  *"--print-default-target"*)
    exit_code="${STUB_DEFAULT_TARGET_EXIT:-0}"
    [ "$exit_code" = 0 ] || { echo "preflight: error: unrecognized arguments: --print-default-target" >&2; exit "$exit_code"; }
    echo "$STUB_DEFAULT_TARGET"; exit 0 ;;
  *"make_url"*)
    echo "copi"; echo "postgresql+asyncpg://copi:***@postgres:5432/copi"; exit 0 ;;
  *"alembic/versions"*)
    exit "${STUB_VERSIONS_EXIT:-0}" ;;
  *"select version_num"*)
    stamp="$(head -n 1 "$STUB_STAMPS")"; sed -i 1d "$STUB_STAMPS"; echo "$stamp"; exit 0 ;;
  *" ps --status running --services"*)
    for s in ${STUB_RUNNING:-}; do echo "$s"; done; exit 0 ;;
  *" cp "*)
    dest="${@: -1}"; head -c 4096 /dev/zero > "$dest"; exit 0 ;;
  *"pg_restore -l"*)
    echo "1; 2615 2200 SCHEMA - public copi"; exit 0 ;;
  *"preflight.py"*)
    exit "${STUB_PREFLIGHT_EXIT:-0}" ;;
  *"alembic upgrade"*)
    exit "${STUB_UPGRADE_EXIT:-0}" ;;
  *"postflight.py"*)
    exit "${STUB_POSTFLIGHT_EXIT:-0}" ;;
esac
exit 0
"""

_FAKE_GIT = r"""#!/usr/bin/env bash
case "$1" in
  rev-parse) echo "$STUB_HEAD" ;;
  status) echo " M docker-compose.prod.yml" ;;
esac
exit 0
"""


class Run:
    def __init__(self, proc: subprocess.CompletedProcess, log: Path, backup_dir: Path):
        self.rc = proc.returncode
        self.stdout = proc.stdout
        self.stderr = proc.stderr
        self.output = proc.stdout + proc.stderr
        self.calls = log.read_text().splitlines() if log.exists() else []
        self.backup_dir = backup_dir

    def calls_with(self, needle: str) -> list[str]:
        return [c for c in self.calls if needle in c]

    def one_call(self, needle: str) -> str:
        found = self.calls_with(needle)
        assert len(found) == 1, (needle, self.calls)
        return found[0]


@pytest.fixture
def run_script(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", _FAKE_DOCKER), ("git", _FAKE_GIT)):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)

    def _run(*args: str, stamps: tuple[str, ...] = ("0050", "0051"), **env: str) -> Run:
        log = tmp_path / "docker.log"
        log.unlink(missing_ok=True)
        stamp_file = tmp_path / "stamps"
        stamp_file.write_text("".join(f"{s}\n" for s in stamps))
        backup_dir = tmp_path / "b"
        base = {
            k: v for k, v in os.environ.items()
            if k not in {"DATABASE_URL", "COMPOSE_FILE", "MIGRATE_SERVICE",
                         "MIGRATE_PG_SERVICE", "ALEMBIC_LOCK_TIMEOUT_MS"}
        }
        full_env = {
            **base,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "STUB_LOG": str(log),
            "STUB_STAMPS": str(stamp_file),
            "STUB_HEAD": _HEAD,
            "STUB_COMMIT": _HEAD,
            "STUB_DEFAULT_TARGET": "0051",
            "MIGRATE_BACKUP_DIR": str(backup_dir),
            **env,
        }
        proc = subprocess.run(
            ["bash", str(_SCRIPT), *args],
            env=full_env, capture_output=True, text=True, timeout=60,
        )
        return Run(proc, log, backup_dir)

    return _run


def _mount_forms(run: Run) -> tuple[str, ...]:
    return (
        f"-v {run.backup_dir}:/app/backups",
        f"-v {os.path.realpath(run.backup_dir)}:/app/backups",
    )


def _execs_web_service(call: str) -> bool:
    tokens = call.split()
    return "exec" in tokens and "blackbird-app" in tokens


def test_script_parses():
    proc = subprocess.run(["bash", "-n", str(_SCRIPT)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_rehearsal_uses_the_prod_stack_and_one_off_containers(run_script):
    run = run_script()
    assert run.rc == 0, run.output
    assert run.calls
    for call in run.calls:
        assert call.startswith("compose -f docker-compose.prod.yml "), call
        assert not _execs_web_service(call), call
    pre = run.one_call("preflight.py --target")
    assert "run --rm --no-deps" in pre
    assert any(m in pre for m in _mount_forms(run)), pre
    assert "--target 0051" in pre
    assert "--snapshot /app/backups/preflight_snapshot.json" in pre
    assert not run.calls_with("alembic upgrade")


def test_default_target_comes_from_the_image(run_script):
    run = run_script(STUB_DEFAULT_TARGET="0049")
    assert run.rc == 0, run.output
    assert "--target 0049" in run.one_call("preflight.py --target")


def test_explicit_target_still_wins(run_script):
    run = run_script("--target", "0050", STUB_DEFAULT_TARGET="0049")
    assert run.rc == 0, run.output
    assert "--target 0050" in run.one_call("preflight.py --target")
    assert not run.calls_with("--print-default-target")


def test_image_built_from_another_commit_blocks(run_script):
    run = run_script(STUB_COMMIT="b" * 40)
    assert run.rc == 1, run.output
    assert ".build_info.json" in run.stderr
    assert "docker compose -f docker-compose.prod.yml build blackbird-app worker" in run.stderr
    assert not run.calls_with("preflight.py --target")


def test_missing_build_info_blocks(run_script):
    run = run_script(STUB_BUILD_INFO_EXIT="1")
    assert run.rc == 1, run.output
    assert ".build_info.json" in run.stderr
    assert "build blackbird-app worker" in run.stderr


def test_dirty_count_mismatch_only_warns(run_script):
    run = run_script(STUB_IMAGE_DIRTY="3")
    assert run.rc == 2, run.output
    assert "WARN" in run.stdout
    assert run.calls_with("preflight.py --target")


def test_pre_flag_image_blocks_instead_of_guessing(run_script):
    # argparse's own usage error is status 2, which this script's callers read as
    # "warnings only"; the script must turn it into a BLOCK.
    run = run_script(STUB_DEFAULT_TARGET_EXIT="2")
    assert run.rc == 1, run.output
    assert "rebuild" in run.stderr
    assert not run.calls_with("preflight.py --target")


def test_target_the_image_does_not_carry_blocks(run_script):
    run = run_script(STUB_VERSIONS_EXIT="2")
    assert run.rc == 1, run.output
    assert "alembic/versions" in run.stderr


def test_unchanged_stamp_is_not_called_a_silent_rollback(run_script):
    # RCA E11: `alembic upgrade X` to a revision behind the stamp exits 0 and changes
    # nothing.
    run = run_script("--apply", stamps=("0050", "0050"))
    assert run.rc == 1, run.output
    assert "applied nothing" in run.stderr
    assert "silent rollback" not in run.output
    assert not run.calls_with("postflight.py")


@pytest.mark.parametrize("after", ["", "NONE"])
def test_missing_stamp_is_the_silent_rollback_signature(run_script, after):
    run = run_script("--apply", stamps=("0050", after))
    assert run.rc == 1, run.output
    assert "silent rollback" in run.stderr
    assert not run.calls_with("postflight.py")


def test_stamp_that_moved_elsewhere_blocks(run_script):
    run = run_script("--apply", stamps=("0048", "0050"))
    assert run.rc == 1, run.output
    assert "moved 0048 -> 0050, not 0051" in run.stderr


def test_apply_passes_the_backup_as_a_container_path(run_script):
    run = run_script("--apply")
    assert run.rc == 0, run.output
    pre = run.one_call("preflight.py --target")
    m = re.search(r"--backup-path /app/backups/(copi_pre0051_\d{8}T\d{6}\.dump)(\s|$)", pre)
    assert m, pre
    assert (run.backup_dir / m.group(1)).stat().st_size >= 1024
    assert "--backup-verified-elsewhere" not in pre


def test_apply_path_never_execs_into_the_web_service(run_script):
    run = run_script("--apply")
    assert run.rc == 0, run.output
    assert run.calls_with("alembic upgrade 0051")
    assert run.calls_with("postflight.py")
    for call in run.calls:
        assert call.startswith("compose -f docker-compose.prod.yml "), call
        assert not _execs_web_service(call), call
        tokens = call.split()
        if "exec" in tokens:
            rest = [t for t in tokens[tokens.index("exec") + 1:] if t != "-T"]
            assert rest[0] == "postgres", call
    for needle in ("alembic upgrade", "postflight.py", "select version_num"):
        for call in run.calls_with(needle):
            assert "run --rm --no-deps" in call, call
    # No live writers: growth still fails, as before.
    assert "--allow-row-growth" not in run.one_call("postflight.py")


def test_live_writers_tolerate_growth_but_not_loss(run_script):
    run = run_script("--apply", STUB_RUNNING="postgres blackbird-app")
    assert run.rc == 0, run.output
    assert "--allow-row-growth" in run.one_call("postflight.py")
    assert "WARN  row growth tolerated: live writers: blackbird-app" in run.stdout


def test_postflight_failure_prints_prod_restore_commands(run_script):
    run = run_script("--apply", STUB_POSTFLIGHT_EXIT="1")
    assert run.rc == 1, run.output
    dumps = list(run.backup_dir.glob("copi_pre0051_*.dump"))
    assert len(dumps) == 1
    assert dumps[0].name in run.stderr
    assert "docker-compose.prod.yml" in run.stderr
    assert "/admin/simulation" in run.stderr
    assert "--profile agent stop -t 420 agent" in run.stderr
    assert "--exit-on-error" in run.stderr
    assert not _FOREIGN_CONTAINER.search(run.output)
    assert "docs/production-migration.md" not in run.output
    for line in run.stderr.splitlines():
        if "docker compose" in line:
            assert "docker compose -f docker-compose.prod.yml" in line, line


def test_lock_timeout_reaches_the_container(run_script):
    run = run_script("--apply", ALEMBIC_LOCK_TIMEOUT_MS="1234")
    assert run.rc == 0, run.output
    up = run.one_call("alembic upgrade")
    # A host-side prefix would reach only the docker CLI; it must be a -e before the
    # service name.
    assert up.index("-e ALEMBIC_LOCK_TIMEOUT_MS=1234") < up.index(" blackbird-app ")
    # Scoped to the upgrade call only.
    assert len(run.calls_with("ALEMBIC_LOCK_TIMEOUT_MS")) == 1


def test_host_database_url_is_ignored(run_script):
    run = run_script(
        "--apply", DATABASE_URL="postgresql+asyncpg://copi:hunter2@elsewhere:5432/other"
    )
    assert run.rc == 0, run.output
    assert not run.calls_with("DATABASE_URL=")
    assert "hunter2" not in run.output
    assert "elsewhere" not in "\n".join(run.calls)


def test_no_dsn_reads_the_service_dsn_without_putting_it_on_the_cli(run_script):
    run = run_script("--apply")
    assert run.rc == 0, run.output
    assert run.calls_with("make_url")
    assert not run.calls_with("DATABASE_URL=")
    assert "postgresql+asyncpg://copi:***@postgres:5432/copi" in run.stdout
    assert re.search(r" pg_dump -U copi -Fc -f \S+ copi$", run.one_call("pg_dump"))


def test_explicit_database_url_reaches_the_container_before_the_service(run_script):
    dsn = "postgresql+asyncpg://copi:pw@postgres:5432/copi_x1"
    run = run_script("--database-url", dsn)
    assert run.rc == 0, run.output
    pre = run.one_call("preflight.py --target")
    assert pre.index(f"-e DATABASE_URL={dsn}") < pre.index(" blackbird-app ")


def test_run_migration_hardcodes_no_revision():
    code = [
        line for line in _SCRIPT.read_text().splitlines()
        if not line.lstrip().startswith("#")
    ]
    offenders = [line for line in code if re.search(r"\b00[0-9]{2}\b", line)]
    assert not offenders, offenders
