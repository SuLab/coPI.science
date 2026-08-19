#!/usr/bin/env python3
"""Nightly verified Postgres backups for the copi production stacks.

Design: docs/specs/2026-08-18-postgres-backup-verification-design.md

Runs on the HOST as root under systemd, not inside any container. Talks to the
production databases only through ``docker exec``. Every dump is proved restorable
before it is counted as a backup: it is restored into a throwaway, memory-capped,
network-less postgres container and its per-table row counts are compared against
the exact counts of the snapshot the dump was taken from.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# Configured only inside main() — see the comment there. Importing this module (as
# the unit tests do) must never install a stdout handler on the root logger.
logger = logging.getLogger("copi_backup")

DEFAULTS = {
    "BACKUP_ROOT": "/var/backups/copi",
    "RETENTION_COUNT": "5",
    "RETENTION_UNVERIFIED": "2",
    "VERIFY_IMAGE": "postgres:15",
    "VERIFY_MEM": "768m",
    "VERIFY_TIMEOUT_SEC": "1800",
    # Peak concurrent usage for ONE stack is roughly 2x the dump (container-side
    # temp file plus the host .partial) PLUS the restored verify volume (~2.3 GB
    # measured for copi-python), and prune runs last so a 6th copy can briefly
    # coexist with 5 retained ones. Measured need ~= 3.74 GB against a 2.16 GB
    # demand at the old factor of 3 — audit finding F3, 2026-08-18. Raised to 7.
    # NOTE: scripts/backup/backup.env.example still documents 3; it is out of
    # scope for this fix (not in the editable file list) and should be updated
    # separately.
    "FREE_SPACE_FACTOR": "7",
    "OFFSITE_CMD": "",
    "AWS_REGION": "us-east-2",
    "SES_SENDER_EMAIL": "",
    "MAIL_TO": "",
    # A legitimate bulk deletion trips this on purpose (audit C1) — it should
    # require a human to look, not silently rotate the last good copy away.
    "REGRESSION_TOLERANCE_PCT": "20",
}


class ConfigError(Exception):
    """Raised for any malformed value in /etc/copi-backup/backup.env."""


# Stack names, databases, and users: alphanumerics and hyphens only, no dots or slashes.
_SAFE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
# Container names: allow dots (Docker does), but still reject traversal vectors.
_SAFE_CONTAINER = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


@dataclass(frozen=True)
class Stack:
    name: str
    container: str
    db: str
    user: str


@dataclass(frozen=True)
class Config:
    stacks: list[Stack]
    backup_root: str
    retention_count: int
    retention_unverified: int
    verify_image: str
    verify_mem: str
    verify_timeout_sec: int
    free_space_factor: int
    offsite_cmd: str
    aws_region: str
    ses_sender_email: str
    regression_tolerance_pct: int = 20
    mail_to: list[str] = field(default_factory=list)


def parse_stacks(raw: str) -> list[Stack]:
    """Parse the STACKS block: one ``name:container:db:user`` per line."""
    stacks: list[Stack] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) != 4 or not all(p.strip() for p in parts):
            raise ConfigError(f"expected 4 colon-separated fields, got: {line!r}")
        name, container, db, user = (p.strip() for p in parts)
        if not _SAFE_NAME.match(name):
            raise ConfigError(f"unsafe stack name: {name!r}")
        if not _SAFE_CONTAINER.match(container):
            raise ConfigError(f"unsafe container: {container!r}")
        if not _SAFE_NAME.match(db):
            raise ConfigError(f"unsafe database: {db!r}")
        if not _SAFE_NAME.match(user):
            raise ConfigError(f"unsafe user: {user!r}")
        if name in seen:
            raise ConfigError(f"duplicate stack name: {name!r}")
        seen.add(name)
        stacks.append(Stack(name=name, container=container, db=db, user=user))
    if not stacks:
        raise ConfigError("no stacks configured")
    return stacks


_FORBIDDEN_ROOTS = frozenset({
    "/", "/etc", "/var", "/var/log", "/home", "/root", "/tmp", "/usr", "/sys",
    "/proc", "/boot", "/dev", "/opt", "/srv", "/lib", "/bin", "/sbin",
})


def _validate_backup_root(raw: str) -> str:
    """Validate BACKUP_ROOT: must be absolute, have depth >= 2, not in forbidden list."""
    raw = raw.strip()
    normalised = os.path.normpath(raw)
    if not os.path.isabs(normalised):
        raise ConfigError(f"BACKUP_ROOT must be absolute: {raw!r}")
    if normalised in _FORBIDDEN_ROOTS:
        raise ConfigError(f"BACKUP_ROOT in forbidden system directory: {normalised!r}")
    depth = normalised.count("/")
    if depth < 2:
        raise ConfigError(f"BACKUP_ROOT too shallow (must have >= 2 path segments): {normalised!r}")
    return normalised


def _as_int(env: dict[str, str], key: str, minimum: int) -> int:
    raw = env.get(key, DEFAULTS[key])
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc
    return max(value, minimum)


def load_config(env: dict[str, str]) -> Config:
    """Build a Config from an environment mapping, applying documented defaults."""
    return Config(
        stacks=parse_stacks(env.get("STACKS", "")),
        backup_root=_validate_backup_root(env.get("BACKUP_ROOT", DEFAULTS["BACKUP_ROOT"])),
        # Clamped to >=1: a configured 0 would let the pruner empty the directory.
        retention_count=_as_int(env, "RETENTION_COUNT", 1),
        retention_unverified=_as_int(env, "RETENTION_UNVERIFIED", 0),
        verify_image=env.get("VERIFY_IMAGE", DEFAULTS["VERIFY_IMAGE"]).strip(),
        verify_mem=env.get("VERIFY_MEM", DEFAULTS["VERIFY_MEM"]).strip(),
        verify_timeout_sec=_as_int(env, "VERIFY_TIMEOUT_SEC", 60),
        free_space_factor=_as_int(env, "FREE_SPACE_FACTOR", 1),
        offsite_cmd=env.get("OFFSITE_CMD", DEFAULTS["OFFSITE_CMD"]).strip(),
        aws_region=env.get("AWS_REGION", DEFAULTS["AWS_REGION"]).strip(),
        ses_sender_email=env.get("SES_SENDER_EMAIL", DEFAULTS["SES_SENDER_EMAIL"]).strip(),
        # Clamped to >=0: a negative tolerance would make every run "regress".
        regression_tolerance_pct=_as_int(env, "REGRESSION_TOLERANCE_PCT", 0),
        mail_to=env.get("MAIL_TO", DEFAULTS["MAIL_TO"]).split(),
    )


def read_env_file(text: str) -> dict[str, str]:
    """Parse a shell-style KEY=VALUE env file, honouring quotes and multi-line values."""
    env: dict[str, str] = {}
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    for token in lexer:
        if "=" not in token:
            raise ConfigError(f"malformed config line: {token!r}")
        key, _, value = token.partition("=")
        env[key.strip()] = value
    return env


TS_FORMAT = "%Y%m%dT%H%M%SZ"

# Anchored on both ends. The pruner only ever deletes paths this matches, which is
# what keeps the hand-made dumps in blackbird-copi-science/backups/ structurally out
# of reach (they are named copi_pre0028_20260817_203346.dump — no 'T', no 'Z').
DUMP_RE = re.compile(
    r"^(?P<stack>[A-Za-z0-9][A-Za-z0-9_-]*)"
    r"_(?P<db>[A-Za-z0-9][A-Za-z0-9_-]*)"
    r"_(?P<ts>\d{8}T\d{6}Z)"
    r"\.dump(?P<unverified>\.unverified)?$"
)


@dataclass(frozen=True)
class DumpFile:
    name: str
    stack: str
    db: str
    taken: datetime
    verified: bool


def dump_name(stack: str, db: str, when: datetime) -> str:
    """Canonical dump filename. ``when`` must be timezone-aware UTC."""
    return f"{stack}_{db}_{when.strftime(TS_FORMAT)}.dump"


def parse_dump_name(filename: str) -> DumpFile | None:
    """Parse a managed dump filename, or None if this file is not ours."""
    match = DUMP_RE.match(filename)
    if match is None:
        return None
    taken = datetime.strptime(match.group("ts"), TS_FORMAT).replace(tzinfo=UTC)
    return DumpFile(
        name=filename,
        stack=match.group("stack"),
        db=match.group("db"),
        taken=taken,
        verified=match.group("unverified") is None,
    )


def select_for_deletion(
    dumps: list[DumpFile], keep_verified: int, keep_unverified: int
) -> list[DumpFile]:
    """Count-based retention, applied per stack.

    Keeps the ``keep_verified`` newest verified dumps and the ``keep_unverified``
    newest unverified ones. Deletes nothing for a stack that has no verified dump at
    all: when verification has been failing, the unverified copies may be the only
    copies in existence, and a pruner that outlives the producer must degrade to
    "stale but present" rather than to an empty directory.
    """
    doomed: list[DumpFile] = []
    stacks = {d.stack for d in dumps}
    for stack in sorted(stacks):
        mine = [d for d in dumps if d.stack == stack]
        verified = sorted((d for d in mine if d.verified), key=lambda d: d.taken, reverse=True)
        unverified = sorted(
            (d for d in mine if not d.verified), key=lambda d: d.taken, reverse=True
        )
        if not verified:
            continue
        doomed.extend(verified[keep_verified:])
        doomed.extend(unverified[keep_unverified:])
    return doomed


# Ordinary tables only. Correct for both databases today: 30 relkind='r', zero
# partitioned tables and zero materialised views (verified 2026-08-18). If a
# partitioned table is ever added, a parent ('p') plus its leaf partitions ('r')
# would need explicit handling — see spec §4.3 and §12.8.
COUNT_TABLES_SQL = """
SELECT n.nspname || '.' || c.relname
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'r'
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
ORDER BY 1
"""


def compare_counts(
    source: dict[str, int], restored: dict[str, int]
) -> tuple[bool, list[str]]:
    """Compare snapshot counts against the restored copy. Any difference fails.

    Unlike scripts/migrate/preflight.py's compare_row_counts, there is no
    ``allow_growth`` here and no notion of expected-new tables: both sides are reads
    of the *same* snapshot, so the only correct outcome is exact equality. A surplus
    is as much a defect as a shortfall — it means the restore did not come from the
    snapshot the counts describe.
    """
    problems: list[str] = []
    for table in sorted(set(source) | set(restored)):
        want = source.get(table)
        got = restored.get(table)
        if want is None:
            problems.append(f"{table}: not in the source snapshot, {got:,} rows restored")
        elif got is None:
            problems.append(f"{table}: {want:,} rows in snapshot, MISSING from restore")
        elif want != got:
            problems.append(
                f"{table}: {want:,} rows in snapshot, {got:,} restored ({got - want:+,})"
            )
    return (not problems), problems


def detect_regression(prev: dict, cur: dict, tolerance_pct: int) -> str | None:
    """Compare this run's totals against the most recent PREVIOUS verified sidecar.

    Pure and unit-testable on its own (audit C1 layer 2): ``prev``/``cur`` are
    sidecar documents (see ``sidecar_document``). Returns a human-readable reason
    naming both figures and the percentage if total row count OR ``dump_bytes`` has
    fallen by more than ``tolerance_pct``, else None.

    A previous total of zero is treated as "nothing to compare against" rather than
    triggering a divide-by-zero: verify_dump's absolute floor already refuses to
    call a zero-row snapshot verified, so a genuine zero would never have produced
    the sidecar this function is comparing against in the first place.
    """
    prev_rows = sum((prev or {}).get("row_counts", {}).values())
    cur_rows = sum((cur or {}).get("row_counts", {}).values())
    prev_bytes = (prev or {}).get("dump_bytes", 0) or 0
    cur_bytes = (cur or {}).get("dump_bytes", 0) or 0

    reasons: list[str] = []
    if prev_rows > 0:
        drop_pct = (prev_rows - cur_rows) / prev_rows * 100
        if drop_pct > tolerance_pct:
            reasons.append(
                f"row count fell {drop_pct:.1f}% (previous {prev_rows:,}, now "
                f"{cur_rows:,}, tolerance {tolerance_pct}%)"
            )
    if prev_bytes > 0:
        drop_pct = (prev_bytes - cur_bytes) / prev_bytes * 100
        if drop_pct > tolerance_pct:
            reasons.append(
                f"dump size fell {drop_pct:.1f}% (previous {prev_bytes:,} bytes, now "
                f"{cur_bytes:,} bytes, tolerance {tolerance_pct}%)"
            )
    return "; ".join(reasons) if reasons else None


@dataclass
class Completed:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str


class Runner:
    """The single seam through which every external command is issued."""

    def run(
        self, argv: list[str], *, timeout: int | None = None, check: bool = True
    ) -> Completed:
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            result = Completed(argv, proc.returncode, proc.stdout, proc.stderr)
        except subprocess.TimeoutExpired as exc:
            # Normalise timeouts to CommandError at the seam so they are caught
            # everywhere: both dump_stack's pg_dump and verify_dump's pg_restore.
            result = Completed(
                argv,
                returncode=124,
                stdout=exc.stdout or "",
                stderr=f"timed out after {exc.timeout}s",
            )
            if check:
                raise CommandError(result) from exc
            return result
        if check and result.returncode != 0:
            raise CommandError(result)
        return result


class CommandError(Exception):
    def __init__(self, result: Completed) -> None:
        self.result = result
        super().__init__(
            f"command failed ({result.returncode}): {' '.join(result.argv)}\n"
            f"{result.stderr.strip()[:2000]}"
        )


class FakeRunner(Runner):
    """Test double. Matches a SUBSTRING of the joined argv to canned stdout.

    Substring, not argv prefix. Every in-container call is
    ``docker exec <random-container-name> ...``, so a prefix cannot distinguish
    ``pg_isready`` from ``pg_restore`` — and that indistinguishability is exactly
    what hid an infinite readiness loop during the plan audit.
    """

    def __init__(self, responses: dict[str, str] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[list[str]] = []
        self.failures: dict[str, int] = {}

    def run(
        self, argv: list[str], *, timeout: int | None = None, check: bool = True
    ) -> Completed:
        self.calls.append(list(argv))
        joined = " ".join(argv)
        for needle, rc in self.failures.items():
            if needle in joined:
                result = Completed(argv, rc, "", "fake failure")
                if check:
                    raise CommandError(result)
                return result
        for needle, out in self.responses.items():
            if needle in joined:
                return Completed(argv, 0, out, "")
        return Completed(argv, 0, "", "")


def docker_exec(container: str, argv: list[str], *, interactive: bool = False) -> list[str]:
    """docker exec WITHOUT -t: a TTY would translate newlines and corrupt binary.

    Pass ``interactive=True`` to attach stdin for long-lived processes like the
    snapshot session. Without it, Docker closes stdin immediately and the process
    exits before anything can be written to it.
    """
    if interactive:
        return ["docker", "exec", "-i", container, *argv]
    return ["docker", "exec", container, *argv]


def psql_argv(stack: Stack, sql: str) -> list[str]:
    return docker_exec(stack.container, ["psql", "-U", stack.user, "-d", stack.db, "-tAc", sql])


def pg_dump_argv(stack: Stack, snapshot_id: str, container_path: str) -> list[str]:
    """Dump to a file INSIDE the container. Never to stdout — see run_migration.sh:176."""
    return docker_exec(
        stack.container,
        [
            "nice", "-n", "10", "ionice", "-c", "3",
            "pg_dump", "-U", stack.user, "-Fc",
            f"--snapshot={snapshot_id}",
            "-f", container_path,
            stack.db,
        ],
    )


def verify_run_argv(
    cfg: Config, stack: Stack, volume: str, host_dump: str, name: str, password: str
) -> list[str]:
    return [
        "docker", "run", "-d",
        "--name", name,
        "--label", "copi.backup.ephemeral=true",
        "--network", "none",
        f"--memory={cfg.verify_mem}",
        f"--memory-swap={cfg.verify_mem}",
        "-e", f"POSTGRES_PASSWORD={password}",
        "-e", f"POSTGRES_USER={stack.user}",
        "-e", f"POSTGRES_DB={stack.db}",
        "-v", f"{volume}:/var/lib/postgresql/data",
        "-v", f"{host_dump}:/dump.bin:ro",
        cfg.verify_image,
    ]


class BackupError(Exception):
    """A failure that should mark the run failed and be reported by mail."""


def terminate_sql(sql: str) -> str:
    """Ensure a statement ends in a semicolon before it is fed to psql's stdin.

    Load-bearing, not cosmetic. psql buffers an unterminated statement and executes
    the following ``\\echo`` backslash command IMMEDIATELY, so the sentinel arrives
    before the rows do and the reader sees an empty result. Verified against the live
    server 2026-08-18: the same multi-line query returns 30 rows with a trailing
    semicolon and 0 rows without one, printing the sentinel first.

    With COUNT_TABLES_SQL unterminated, the source snapshot counts come back ``{}``
    while the restored counts are real, so every dump fails verification with one
    spurious problem per table, is renamed ``.unverified``, is never pruned, and mails
    three people every night until they stop reading the mail.
    """
    stripped = sql.strip()
    if not stripped:
        return ""
    return stripped if stripped.endswith(";") else stripped + ";"


READ_TIMEOUT_SEC = 300

SNAPSHOT_OPEN_SQL = (
    "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ; "
    "SET LOCAL idle_in_transaction_session_timeout = 0; "
    "SELECT pg_export_snapshot();"
)


def counts_sql(tables: list[str]) -> str:
    """One statement returning ``schema.table|count`` per row, exact counts."""
    if not tables:
        return ""
    parts = []
    for qualified in tables:
        schema, _, table = qualified.partition(".")
        literal = qualified.replace("'", "''")
        parts.append(
            f"SELECT '{literal}' AS t, count(*) AS n FROM \"{schema}\".\"{table}\""
        )
    return " UNION ALL ".join(parts) + " ORDER BY 1"


def parse_counts_output(stdout: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        table, sep, raw = line.partition("|")
        if not sep or not raw.strip().lstrip("-").isdigit():
            raise BackupError(f"unparseable count row: {line!r}")
        counts[table.strip()] = int(raw.strip())
    return counts


def snapshot_session_argv(stack: Stack) -> list[str]:
    """Build the argv for a long-lived snapshot session psql process.

    Returns a docker exec command with stdin attached (-i flag) so the psql
    process stays open for the duration of the dump.
    """
    return docker_exec(
        stack.container,
        ["psql", "-U", stack.user, "-d", stack.db, "-tA", "-q"],
        interactive=True,
    )


class SnapshotSession:
    """Holds one REPEATABLE READ transaction open across the pg_dump.

    Implemented as a long-lived ``docker exec -i psql`` process rather than repeated
    one-shot execs: the exported snapshot is only valid while its exporting
    transaction is open, so the connection must survive the dump.
    """

    def __init__(self, stack: Stack) -> None:
        self.stack = stack
        self.snapshot_id = ""
        self._proc: subprocess.Popen[str] | None = None

    def _terminate(self) -> None:
        """Kill and reap the psql child. Idempotent; never raises.

        Separate from __exit__ because Python does NOT call __exit__ when __enter__
        raises. Without this, a failure between Popen() and a successful snapshot
        export orphans a psql holding a REPEATABLE READ transaction with
        idle_in_transaction_session_timeout disabled — indefinitely, on production,
        blocking vacuum. Verified: `with M()` where __enter__ raises never reaches
        __exit__.
        """
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=10)   # reap, or the killed child lingers as a zombie
        except (subprocess.TimeoutExpired, OSError):
            # OSError (e.g. ChildProcessError from a watchdog/__exit__ race) must not
            # escape a function documented "never raises" — on the __enter__ path it
            # would mask the original error being propagated. ChildProcessError is a
            # subclass of OSError.
            pass

    def __enter__(self) -> SnapshotSession:
        argv = snapshot_session_argv(self.stack)
        # stderr to DEVNULL, not PIPE: nothing reads it during the dump, and a psql
        # NOTICE storm filling the 64K pipe buffer would deadlock the session that is
        # holding the snapshot open.
        self._proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            # `[-1]` on an empty list is an IndexError, not the BackupError below, so
            # the empty case is handled explicitly rather than left to the guard.
            lines = self._send(SNAPSHOT_OPEN_SQL).strip().splitlines()
            self.snapshot_id = lines[-1].strip() if lines else ""
            if not self.snapshot_id:
                raise BackupError(
                    f"{self.stack.name}: pg_export_snapshot() returned nothing"
                )
        except BaseException:
            self._terminate()
            raise
        return self

    def _send(self, sql: str) -> str:
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            raise BackupError("snapshot session is not open")
        sentinel = "__COPI_BACKUP_EOS__"
        stdout = self._proc.stdout
        self._proc.stdin.write(f"{terminate_sql(sql)}\n\\echo {sentinel}\n")
        self._proc.stdin.flush()
        # A psql that HANGS rather than dies would otherwise block this read forever,
        # holding the snapshot open until systemd's TimeoutStartSec fires an hour
        # later. The watchdog kills it; the read then hits EOF and raises below.
        watchdog = threading.Timer(READ_TIMEOUT_SEC, self._terminate)
        watchdog.start()
        try:
            lines: list[str] = []
            for line in stdout:
                if line.strip() == sentinel:
                    break
                lines.append(line)
            else:
                raise BackupError(f"{self.stack.name}: psql session closed unexpectedly")
        finally:
            watchdog.cancel()
        return "".join(lines)

    def counts(self) -> dict[str, int]:
        """Exact per-table counts, read in the SAME snapshot the dump used."""
        listing = self._send(COUNT_TABLES_SQL)
        tables = [line.strip() for line in listing.splitlines() if line.strip()]
        sql = counts_sql(tables)
        if not sql:
            return {}
        return parse_counts_output(self._send(sql))

    def __exit__(self, *exc: object) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.write("COMMIT;\n\\q\n")
                self._proc.stdin.flush()
                self._proc.stdin.close()
            self._proc.wait(timeout=30)
            self._proc = None
        except Exception:  # teardown must never mask the original error
            self._terminate()


@dataclass(frozen=True)
class DumpResult:
    path: Path
    counts: dict[str, int]
    snapshot_id: str
    size_bytes: int
    sha256: str


def _ensure_dir(path: Path) -> None:
    """Create a directory at 0700 and enforce that mode even if it already existed.

    ``mkdir(mode=...)`` is filtered through the process umask, so a default umask
    of 022 would still leave the directory at 0755 — the ``mode=`` kwarg alone is
    not load-bearing. The explicit ``chmod`` afterwards is: it also re-closes the
    directory if BACKUP_ROOT is ever removed and recreated at the default mode,
    which is exactly how F5 exposed the full production database to any local
    account despite install.sh having set 0700 once.
    """
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dump_stack(
    runner: Runner,
    cfg: Config,
    stack: Stack,
    dest_dir: Path,
    now: datetime,
    session_factory: type[SnapshotSession] = SnapshotSession,
) -> DumpResult:
    """Dump one stack against an exported snapshot; return the archive and its counts."""
    _ensure_dir(dest_dir)
    final = dest_dir / dump_name(stack.name, stack.db, now)
    partial = final.with_suffix(final.suffix + ".partial")
    ctmp = f"/tmp/copi_backup_{os.getpid()}_{stack.name}.dump"

    try:
        with session_factory(stack) as session:
            runner.run(pg_dump_argv(stack, session.snapshot_id, ctmp), timeout=cfg.verify_timeout_sec)
            # TOC readable inside the container, on a real seekable path.
            runner.run(docker_exec(stack.container, ["pg_restore", "-l", ctmp]))
            runner.run(["docker", "cp", f"{stack.container}:{ctmp}", str(partial)])
            os.chmod(partial, 0o600)
            counts = session.counts()
            snapshot_id = session.snapshot_id
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    finally:
        runner.run(docker_exec(stack.container, ["rm", "-f", ctmp]), check=False)

    with partial.open("rb") as handle:
        os.fsync(handle.fileno())
    # rename preserves the mode bits set above, so `final` is 0600 too.
    partial.replace(final)
    return DumpResult(
        path=final,
        counts=counts,
        snapshot_id=snapshot_id,
        size_bytes=final.stat().st_size,
        sha256=sha256_file(final),
    )


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    problems: list[str]
    oom: bool
    duration_sec: float


def _container_oom_killed(runner: Runner, name: str) -> bool:
    probe = runner.run(
        ["docker", "inspect", "-f", "{{.State.OOMKilled}}", name], check=False
    )
    return probe.stdout.strip().lower() == "true"


def verify_dump(
    runner: Runner,
    cfg: Config,
    stack: Stack,
    dump_path: Path,
    expected_counts: dict[str, int],
) -> VerifyResult:
    """Restore into a throwaway container and compare counts. Never raises."""
    started = time.monotonic()
    token = f"{os.getpid()}-{secrets.token_hex(4)}"
    name = f"copi-verify-{stack.name}-{token}"
    volume = f"copi-verify-{stack.name}-{token}"
    problems: list[str] = []
    oom = False

    try:
        # TOC check on the copied archive: proves the docker cp did not truncate.
        # Run in a throwaway container of the SAME image, not on the host — the
        # host has no postgres client tools installed (verified 2026-08-18), and
        # installing them would introduce a third pg_restore version alongside the
        # source server and the verify container.
        runner.run([
            "docker", "run", "--rm", "--network", "none",
            "-v", f"{dump_path}:/d.bin:ro", cfg.verify_image,
            "pg_restore", "-l", "/d.bin",
        ])
        runner.run(
            ["docker", "volume", "create", "--label", "copi.backup.ephemeral=true", volume]
        )
        runner.run(
            verify_run_argv(cfg, stack, volume, str(dump_path), name, secrets.token_urlsafe(24))
        )
        deadline = time.monotonic() + cfg.verify_timeout_sec
        while True:
            # -h 127.0.0.1 is load-bearing (audit I3): postgres:15's entrypoint runs a
            # temporary bootstrap server on the UNIX SOCKET ONLY
            # (listen_addresses='') before restarting the real one. pg_isready with
            # no host checks that socket and can report ready during that window
            # (measured 266ms against this 2s poll) — pg_restore then races a server
            # about to shut down. The bootstrap server never listens on TCP, so
            # forcing the probe onto 127.0.0.1 is a pure win: the only failure mode
            # this removes is a spurious one.
            ready = runner.run(
                docker_exec(name, ["pg_isready", "-h", "127.0.0.1", "-U", stack.user, "-q"]),
                check=False,
            )
            if ready.returncode == 0:
                break
            # Bail the moment the container is gone. Without this, a container
            # OOM-killed at startup spins here for the full VERIFY_TIMEOUT_SEC
            # (30 min), delaying the alert and holding the flock the whole time.
            # Found by running this plan's own tests during the audit: two of them
            # hung rather than failed.
            alive = runner.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", name], check=False
            )
            if alive.stdout.strip().lower() != "true":
                raise BackupError(
                    f"{stack.name}: verify container exited before becoming ready"
                )
            if time.monotonic() > deadline:
                raise BackupError(f"{stack.name}: verify container never became ready")
            time.sleep(2)

        runner.run(
            docker_exec(
                name,
                [
                    "pg_restore", "--no-owner", "--no-privileges", "--exit-on-error",
                    "-U", stack.user, "-d", stack.db, "/dump.bin",
                ],
            ),
            timeout=cfg.verify_timeout_sec,
        )
        listing = runner.run(docker_exec(name, [
            "psql", "-U", stack.user, "-d", stack.db, "-tAc", COUNT_TABLES_SQL,
        ]))
        tables = [line.strip() for line in listing.stdout.splitlines() if line.strip()]
        sql = counts_sql(tables)
        restored = (
            parse_counts_output(
                runner.run(
                    docker_exec(name, ["psql", "-U", stack.user, "-d", stack.db, "-tAc", sql])
                ).stdout
            )
            if sql
            else {}
        )
        # Defence in depth against the both-empty hazard: compare_counts({}, {})
        # is legitimately (True, []), so an upstream bug that silently produced no
        # counts on BOTH sides would report a verified backup having verified
        # nothing. That is not hypothetical — an unterminated statement fed to the
        # snapshot session did exactly this during plan development. Both production
        # databases have 30 user tables; zero means the collection step broke, not
        # that the database is empty.
        if not expected_counts:
            raise BackupError(
                f"{stack.name}: snapshot reported ZERO tables — the count step failed. "
                "Refusing to call this dump verified."
            )
        # Absolute floor, distinct from the empty-dict case above (audit C1): a
        # mass-TRUNCATEd production database still reports N tables, each with a
        # real (zero) count, so `expected_counts` is truthy and the guard above
        # never fires. Both production databases hold tens of thousands of rows;
        # a total of zero across every table means the count step or the database
        # itself is broken, not that the database is legitimately empty.
        if sum(expected_counts.values()) == 0:
            raise BackupError(
                f"{stack.name}: snapshot reported {len(expected_counts)} table(s) but "
                "ZERO total rows across all of them — the count step or the database "
                "is broken. Refusing to call this dump verified."
            )
        ok, problems = compare_counts(expected_counts, restored)
    except (CommandError, BackupError) as exc:
        oom = _container_oom_killed(runner, name)
        if oom:
            problems = [
                f"{stack.name}: verify container was OOM-killed at VERIFY_MEM="
                f"{cfg.verify_mem}. This is a harness failure, not a bad dump — "
                "raise VERIFY_MEM (spec §10 test 16) and re-verify."
            ]
        else:
            # Every BackupError raised inside this function already embeds stack.name;
            # CommandError does not. Prepend only when it is actually missing, or the
            # failure mail reads "copi-blackbird: copi-blackbird: ...".
            message = str(exc)
            if not message.startswith(f"{stack.name}:"):
                message = f"{stack.name}: {message}"
            problems = [message]
        ok = False
    finally:
        runner.run(["docker", "rm", "-f", "-v", name], check=False)
        runner.run(["docker", "volume", "rm", volume], check=False)

    return VerifyResult(
        ok=ok, problems=problems, oom=oom, duration_sec=round(time.monotonic() - started, 1)
    )


@dataclass(frozen=True)
class StackResult:
    stack: str
    dump: DumpResult | None
    verify: VerifyResult | None
    offsite_ok: bool
    error: str | None

    @property
    def ok(self) -> bool:
        """True only for a stack that produced a dump AND verified it.

        `dump is not None` is part of the invariant, not decoration: without it a
        StackResult(dump=None, verify=ok) reports "verified" with no archive on record.
        """
        return (
            self.error is None
            and self.dump is not None
            and self.verify is not None
            and self.verify.ok
        )


def sidecar_document(result: StackResult, started: datetime) -> dict:
    dump = result.dump
    verify = result.verify
    return {
        "stack": result.stack,
        "started_utc": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dump_bytes": dump.size_bytes if dump else 0,
        "dump_sha256": dump.sha256 if dump else "",
        "snapshot_id": dump.snapshot_id if dump else "",
        "row_counts": dict(dump.counts) if dump else {},
        "verified": bool(verify and verify.ok),
        "verify_duration_sec": verify.duration_sec if verify else 0.0,
        "verify_problems": list(verify.problems) if verify else [],
        "offsite": result.offsite_ok,
        "error": result.error,
    }


def build_status(
    results: list[StackResult],
    now: datetime,
    previous: dict | None = None,
    reason: str | None = None,
) -> dict:
    """Build the status.json document.

    ``last_success_utc`` is what lets a reader distinguish "ran and failed" from
    "did not run" (spec §7.2). On a failed run it is carried forward from
    ``previous`` (yesterday's status.json, if any) rather than dropped; on a
    successful run it is stamped to ``now``.
    """
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    ok = all(r.ok for r in results) and bool(results)
    last_success = stamp if ok else (previous or {}).get("last_success_utc")
    status = {
        "last_run_utc": stamp,
        "last_success_utc": last_success,
        "ok": ok,
        "stacks": {
            r.stack: {
                "verified": r.ok,
                "dump_bytes": r.dump.size_bytes if r.dump else 0,
                "problems": list(r.verify.problems) if r.verify else [],
                "error": r.error,
            }
            for r in results
        },
    }
    if reason is not None:
        status["reason"] = reason
    return status


def render_failure_mail(
    results: list[StackResult],
    now: datetime,
    offsite_failed: list[str] | None = None,
    regressed: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Render the failure mail.

    ``offsite_failed`` lists stacks whose OFFSITE_CMD hook exited non-zero. Spec §9
    requires this to be mailed even when every stack otherwise verified — it is
    NOT folded into StackResult.ok (which means "a verified backup exists" and
    must stay true), so it is threaded through as a separate argument instead.

    ``regressed`` maps stack name -> reason for stacks whose row count or dump size
    fell more than REGRESSION_TOLERANCE_PCT versus the previous verified sidecar
    (audit C1 layer 2). Kept out of StackResult.ok for the same reason as
    ``offsite_failed``: the backup genuinely verified against the snapshot it was
    taken from, so "verified" must stay true even though the run is reported FAILED.
    """
    offsite_failed = offsite_failed or []
    regressed = regressed or {}
    failed = [r for r in results if not r.ok]
    names = (
        ",".join(r.stack for r in failed)
        or ",".join(offsite_failed)
        or ",".join(regressed)
        or "no-results"
    )
    subject = f"[copi-backup] FAILED {names} {now:%Y-%m-%d}"
    lines = [f"Backup run {now:%Y-%m-%d %H:%M:%S} UTC", ""]
    if not results:
        lines += [
            "The run produced NO results at all — it aborted before any stack was "
            "processed.",
            "Check: systemctl status copi-backup.service",
            "",
        ]
    if offsite_failed:
        lines += [
            f"OFFSITE UPLOAD FAILED for: {', '.join(offsite_failed)}",
            "The local verified backup is unaffected; only the offsite copy is missing.",
            "",
        ]
    if regressed:
        lines += [
            "ROW-COUNT / SIZE REGRESSION DETECTED — dump kept (it verified correctly), "
            "but the run is marked FAILED and NOT pruned. A human must confirm this was "
            "an intentional bulk deletion:",
        ]
        lines += [f"  {stack}: {reason}" for stack, reason in regressed.items()]
        lines.append("")
    for r in results:
        lines.append(f"{r.stack}: {'OK' if r.ok else 'FAILED'}")
        if r.error:
            lines.append(f"  error: {r.error}")
        if r.verify:
            lines.extend(f"  {p}" for p in r.verify.problems)
        if r.dump:
            lines.append(f"  dump: {r.dump.path} ({r.dump.size_bytes:,} bytes)")
        lines.append("")
    return subject, "\n".join(lines)


# A normal nightly gap is 24h; 26h leaves slack for the run's own duration and
# scheduling jitter without masking real multi-day silence (audit C2). Six
# consecutive nights of total failure otherwise pass behind a green weekly mail:
# render_heartbeat_mail used to warn only when `history` was EMPTY, but six-day-old
# sidecars are non-empty and render as a clean "verified" summary.
HEARTBEAT_STALE_HOURS = 26

SIDECAR_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def render_heartbeat_mail(history: list[dict], now: datetime) -> tuple[str, str]:
    subject = f"[copi-backup] weekly summary {now:%Y-%m-%d}"
    lines = [f"Weekly backup summary, {now:%Y-%m-%d} UTC", ""]
    if not history:
        lines.append("NO RUNS RECORDED IN THE LAST 7 DAYS — the timer may not be firing.")
    else:
        newest = max(
            datetime.strptime(e["started_utc"], SIDECAR_TS_FORMAT).replace(tzinfo=UTC)
            for e in history
        )
        age_hours = (now - newest).total_seconds() / 3600
        if age_hours > HEARTBEAT_STALE_HOURS:
            lines.append(
                f"WARNING: the newest recorded run is {age_hours:.1f} hours old "
                f"(threshold {HEARTBEAT_STALE_HOURS}h) — the nightly job may be "
                "failing silently, or the timer may not be firing."
            )
            lines.append("")
    for entry in history:
        lines.append(
            f"{entry['stack']:<18} {entry['started_utc']}  "
            f"{entry['dump_bytes']:>12,} B  "
            f"{'verified' if entry['verified'] else 'UNVERIFIED'}"
        )
    return subject, "\n".join(lines)


def send_mail(cfg: Config, subject: str, body: str, client_factory=None) -> bool:
    """Send via SES v1, matching src/services/email.py.

    Swallows every ``Exception`` — including a failed lazy ``boto3`` import — so mail
    trouble can never abort a backup run. It does NOT swallow ``BaseException``
    (KeyboardInterrupt, SystemExit); those should still terminate the process.
    """
    if not cfg.mail_to or not cfg.ses_sender_email:
        return False
    try:
        if client_factory is None:
            import boto3

            def client_factory(region):
                return boto3.client("ses", region_name=region)

        client = client_factory(cfg.aws_region)
        client.send_email(
            Source=cfg.ses_sender_email,
            Destination={"ToAddresses": list(cfg.mail_to)},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
            },
        )
        return True
    except Exception:  # mail failure must not abort the run
        return False


LOCK_PATH = "/run/copi-backup.lock"


def enough_free_space(free_bytes: int, last_dump_bytes: int, factor: int) -> bool:
    """True iff ``free_bytes`` covers ``factor`` times the expected backup demand.

    A ``last_dump_bytes`` of zero must never read as "no constraint": that is
    exactly the no-verified-dump-yet gap (audit F3) that let this guard pass on a
    full disk, because ``free_bytes >= factor * 0`` is trivially true for any
    ``free_bytes``, including 0. Zero demand means the caller could not measure
    what it needs (no dump on disk AND the live-DB-size fallback failed) — a guard
    that cannot measure must not pass.
    """
    if last_dump_bytes <= 0:
        return False
    return free_bytes >= factor * last_dump_bytes


def sweep(runner: Runner, cfg: Config, now: datetime) -> None:
    """Clear leftovers from a previously crashed run. Label-filtered, never a prune."""
    stale = runner.run(
        ["docker", "ps", "-aq", "--filter", "label=copi.backup.ephemeral=true"], check=False
    )
    for cid in stale.stdout.split():
        runner.run(["docker", "rm", "-f", "-v", cid], check=False)

    vols = runner.run(
        ["docker", "volume", "ls", "-q", "--filter", "label=copi.backup.ephemeral=true"],
        check=False,
    )
    for vol in vols.stdout.split():
        runner.run(["docker", "volume", "rm", vol], check=False)

    for stack in cfg.stacks:
        runner.run(
            docker_exec(stack.container, ["sh", "-c", "rm -f /tmp/copi_backup_*.dump"]),
            check=False,
        )

    root = Path(cfg.backup_root)
    cutoff = now.timestamp() - 24 * 3600
    for partial in root.glob("*/*.dump.partial"):
        if partial.stat().st_mtime < cutoff:
            partial.unlink(missing_ok=True)
    for sidecar in root.glob("*/*.dump.json"):
        if not Path(str(sidecar)[: -len(".json")]).exists():
            sidecar.unlink(missing_ok=True)


def prune(cfg: Config, dry_run: bool, exclude_stacks: set[str] | None = None) -> list[Path]:
    """Apply retention. Returns the paths deleted (or that would be).

    ``exclude_stacks`` skips retention entirely for named stacks (audit C1 layer 2):
    when this run detected a row-count/size regression for a stack, its older
    copies are the only remaining evidence and must survive this run's prune pass
    even though the regressed dump itself verified correctly.
    """
    exclude_stacks = exclude_stacks or set()
    deleted: list[Path] = []
    root = Path(cfg.backup_root).resolve()
    for stack in cfg.stacks:
        if stack.name in exclude_stacks:
            continue
        stack_dir = Path(cfg.backup_root) / stack.name
        if not stack_dir.is_dir():
            continue
        # Defence in depth behind parse_stacks' validation. This function is the last
        # thing that runs before unlink(), so it re-establishes the invariant itself
        # rather than trusting a caller: the directory it is about to delete inside
        # must be a direct child of BACKUP_ROOT.
        resolved = stack_dir.resolve()
        if resolved.parent != root:
            raise ConfigError(
                f"refusing to prune {resolved}: not a direct child of {root}"
            )
        found: list[DumpFile] = []
        for entry in stack_dir.iterdir():
            if entry.is_symlink() or not entry.is_file():
                continue
            parsed = parse_dump_name(entry.name)
            if parsed is not None:
                found.append(parsed)
        for doomed in select_for_deletion(
            found, cfg.retention_count, cfg.retention_unverified
        ):
            target = stack_dir / doomed.name
            sidecar = stack_dir / f"{doomed.name}.json"
            deleted.append(target)
            if not dry_run:
                target.unlink(missing_ok=True)
                sidecar.unlink(missing_ok=True)
    return deleted


def _last_dump_bytes(cfg: Config) -> int:
    sizes = [p.stat().st_size for p in Path(cfg.backup_root).glob("*/*.dump") if p.is_file()]
    return max(sizes) if sizes else 0


def _live_db_bytes(runner: Runner, cfg: Config) -> int:
    """Sum of live database sizes across every configured stack.

    Used only as a fallback by the free-space guard when no verified dump exists
    yet (audit F3): on a brand-new host ``_last_dump_bytes`` is legitimately 0, and
    the guard must size itself against the real database rather than pass
    unconditionally. If this also fails to produce a number, the caller must
    treat it as a hard preflight failure — see ``enough_free_space``.
    """
    total = 0
    for stack in cfg.stacks:
        sql = f"SELECT pg_database_size('{stack.db}')"
        result = runner.run(psql_argv(stack, sql))
        raw = result.stdout.strip()
        if not raw.isdigit():
            raise BackupError(f"{stack.name}: could not read live database size: {raw!r}")
        total += int(raw)
    return total


def _run_ok(
    status: dict, offsite_failed: list[str], regressed: dict[str, str] | None = None
) -> bool:
    """The run's overall verdict: verified backups AND no attempted-and-failed offsite
    AND no undetected row-count/size regression.

    Kept separate from StackResult.ok (which means "a verified backup exists" and
    must stay true even when its offsite copy failed to upload, or when a dump that
    verified correctly nonetheless regressed against its predecessor) so cmd_run has
    one place that decides the exit code and whether to mail — see audit F7 (offsite)
    and audit C1 (regression).
    """
    return bool(status["ok"]) and not offsite_failed and not (regressed or {})


def _write_status(
    cfg: Config, results: list[StackResult], now: datetime, reason: str | None = None
) -> dict:
    """Build and persist status.json, carrying last_success_utc forward on failure.

    Reads whatever status.json is already on disk (if any and if parseable) purely
    to seed ``previous`` for build_status — a corrupt or missing prior file just
    means last_success_utc comes back None, which is itself an honest answer.
    """
    status_path = Path(cfg.backup_root, "status.json")
    previous: dict | None = None
    if status_path.exists():
        try:
            previous = json.loads(status_path.read_text())
        except (OSError, json.JSONDecodeError):
            previous = None
    status = build_status(results, now, previous=previous, reason=reason)
    status_path.write_text(json.dumps(status, indent=2))
    return status


def _mail_failure(cfg: Config, subject: str, body: str) -> None:
    """Send a failure mail and log loudly if SES did not actually accept it.

    A failed run with an undeliverable mail must still leave a trace — see audit
    F1. The full body (which embeds every problem collected so far) is logged at
    ERROR unconditionally, not only when the send itself fails, so the journal
    always has the complete picture even if mail silently succeeds but nobody
    reads their inbox that day.
    """
    logger.error("%s", body)
    if not send_mail(cfg, subject, body):
        logger.error("failure mail was NOT accepted by SES: %s", subject)


def _detect_regressions(
    cfg: Config, results: list[StackResult], now: datetime
) -> dict[str, str]:
    """Compare each successfully verified stack's totals against its most recent
    PREVIOUS sidecar (audit C1 layer 2). Evaluated BEFORE pruning.

    Only stacks that produced a verified dump this run are eligible: a stack that
    already failed dump/verify is reported through the ordinary failure path, and
    comparing a non-existent dump would be meaningless. A stack with no previous
    sidecar (first run) is skipped silently — there is nothing to regress against.
    """
    regressed: dict[str, str] = {}
    for result in results:
        if not result.ok or result.dump is None:
            continue
        stack_dir = Path(cfg.backup_root) / result.stack
        cur_sidecar = Path(f"{result.dump.path}.json")
        candidates = sorted(
            (p for p in stack_dir.glob("*.dump.json") if p != cur_sidecar),
            key=lambda p: p.name,
        )
        if not candidates:
            continue
        try:
            prev_doc = json.loads(candidates[-1].read_text())
        except (OSError, json.JSONDecodeError):
            continue
        cur_doc = sidecar_document(result, now)
        reason = detect_regression(prev_doc, cur_doc, cfg.regression_tolerance_pct)
        if reason is not None:
            logger.error("%s: regression detected: %s", result.stack, reason)
            regressed[result.stack] = reason
    return regressed


def _cmd_run_inner(cfg: Config, runner: Runner, now: datetime, skip_prune: bool) -> int:
    logger.info("run start: stacks=%s", [s.name for s in cfg.stacks])
    _ensure_dir(Path(cfg.backup_root))
    sweep(runner, cfg, now)

    free = shutil.disk_usage(cfg.backup_root).free
    demand = _last_dump_bytes(cfg)
    if demand == 0:
        # No verified dump on disk yet (first run, or verification has been
        # failing) — fall back to the live DB size rather than let a 0 demand
        # pass the guard unconditionally (audit F3).
        try:
            demand = _live_db_bytes(runner, cfg)
            logger.info("no dump on disk yet; sized the guard against the live DB: %d bytes", demand)
        except (CommandError, BackupError) as exc:
            reason = f"free-space guard could not measure demand: {exc}"
            logger.error(reason)
            _write_status(cfg, [], now, reason=reason)
            _mail_failure(cfg, f"[copi-backup] FAILED preflight {now:%Y-%m-%d}", reason)
            return 1
    if not enough_free_space(free, demand, cfg.free_space_factor):
        reason = (
            f"insufficient free space: {free:,} bytes available, "
            f"need >= {cfg.free_space_factor * demand:,} "
            f"({cfg.free_space_factor}x{demand:,})"
        )
        logger.error(reason)
        _write_status(cfg, [], now, reason=reason)
        _mail_failure(cfg, f"[copi-backup] FAILED preflight {now:%Y-%m-%d}", reason)
        return 1

    results: list[StackResult] = []
    offsite_failed: list[str] = []
    for stack in cfg.stacks:
        dest = Path(cfg.backup_root) / stack.name
        try:
            dump = dump_stack(runner, cfg, stack, dest, now)
            logger.info("%s: dump written: %s (%d bytes)", stack.name, dump.path, dump.size_bytes)
            verify = verify_dump(runner, cfg, stack, dump.path, dump.counts)
            logger.info(
                "%s: verify %s in %.1fs",
                stack.name, "OK" if verify.ok else "FAILED", verify.duration_sec,
            )
            if not verify.ok:
                logger.error("%s: verify problems: %s", stack.name, verify.problems)
            final = dump.path
            if not verify.ok:
                final = dump.path.with_name(dump.path.name + ".unverified")
                dump.path.replace(final)

            # The sidecar must exist BEFORE OFFSITE_CMD runs. Spec §6 invokes the
            # hook as `$OFFSITE_CMD <dump> <sidecar>`, and a hook handed a path that
            # does not exist yet cannot upload it. Written once with offsite=False,
            # then rewritten with the real result if the hook actually ran.
            sidecar = final.with_name(final.name + ".json")
            result = StackResult(stack.name, dump, verify, False, None)
            sidecar.write_text(json.dumps(sidecar_document(result, now), indent=2))
            os.chmod(sidecar, 0o600)

            if cfg.offsite_cmd and verify.ok:
                offsite_ok = runner.run(
                    [cfg.offsite_cmd, str(final), str(sidecar)], check=False
                ).returncode == 0
                if not offsite_ok:
                    offsite_failed.append(stack.name)
                    logger.error("%s: OFFSITE_CMD exited non-zero", stack.name)
                result = StackResult(stack.name, dump, verify, offsite_ok, None)
                sidecar.write_text(json.dumps(sidecar_document(result, now), indent=2))
                os.chmod(sidecar, 0o600)
        except Exception as exc:  # one stack must not abort the other
            logger.error("%s: run failed: %s", stack.name, exc)
            result = StackResult(stack.name, None, None, False, str(exc))
        results.append(result)

    # Evaluated BEFORE pruning (audit C1 layer 2): a legitimate bulk deletion trips
    # this on purpose — the dump is kept (it verified correctly against its own
    # snapshot) but the run is reported FAILED and this stack is excluded from the
    # prune pass below, since its older copies are the only remaining evidence.
    regressed = _detect_regressions(cfg, results, now)

    status = _write_status(cfg, results, now)
    overall_ok = _run_ok(status, offsite_failed, regressed)
    logger.info("run complete: overall=%s", "OK" if overall_ok else "FAILED")

    # Mail BEFORE prune (audit F2): an exception from prune must never suppress the
    # failure mail for an already-failing run.
    if not overall_ok:
        subject, body = render_failure_mail(
            results, now, offsite_failed=offsite_failed, regressed=regressed
        )
        _mail_failure(cfg, subject, body)

    if not skip_prune:
        try:
            deleted = prune(cfg, dry_run=False, exclude_stacks=set(regressed))
            logger.info("prune deleted %d file(s): %s", len(deleted), [str(p) for p in deleted])
        except Exception as exc:
            # Must not mask the run's verdict computed above.
            logger.error("prune failed: %s", exc)

    return 0 if overall_ok else 1


def cmd_run(cfg: Config, runner: Runner, now: datetime, skip_prune: bool) -> int:
    """Entry point for `copi-backup run`.

    Wraps _cmd_run_inner so that a truly unexpected exception can never leave
    status.json and mail silent — audit F4 required a status.json write (and a
    best-effort mail) on EVERY exit path, including one nobody anticipated.
    """
    try:
        return _cmd_run_inner(cfg, runner, now, skip_prune)
    except Exception as exc:
        reason = f"unexpected exception: {exc}"
        logger.error("run aborted by an unexpected exception: %s", exc)
        try:
            _write_status(cfg, [], now, reason=reason)
        except Exception as write_exc:
            logger.error("could not write status.json after an unexpected exception: %s", write_exc)
        _mail_failure(cfg, f"[copi-backup] FAILED unexpected-exception {now:%Y-%m-%d}", reason)
        return 1


def cmd_report(cfg: Config, now: datetime) -> int:
    history: list[dict] = []
    cutoff = now.timestamp() - 7 * 86400
    for sidecar in sorted(Path(cfg.backup_root).glob("*/*.dump*.json")):
        if sidecar.stat().st_mtime < cutoff:
            continue
        try:
            history.append(json.loads(sidecar.read_text()))
        except json.JSONDecodeError:
            continue
    subject, body = render_heartbeat_mail(history, now)
    mailed = send_mail(cfg, subject, body)
    if not mailed:
        logger.error("weekly heartbeat mail was NOT accepted by SES: %s", subject)
    return 0 if mailed else 1


def _write_signal_status(cfg: Config, signum: int) -> None:
    """Persist status.json marking the run killed by a signal. Never raises.

    Split out of the handler itself so the file-IO/logging logic is unit-testable
    without actually delivering a signal (forbidden here: no real subprocess/signal
    plumbing in this suite). Every exception is swallowed after being logged: a
    handler invoked mid-syscall must not itself raise into arbitrary interrupted
    code.
    """
    try:
        name = signal.Signals(signum).name
    except ValueError:
        name = str(signum)
    try:
        logger.error("received %s — writing status.json before exit", name)
    except Exception:
        pass
    try:
        # Belt-and-braces: normally _cmd_run_inner has already called this before
        # any subprocess (and therefore before any signal-prone wait) runs, but a
        # signal landing in the narrow window before that must still succeed in
        # writing status.json rather than silently no-op on a missing directory.
        _ensure_dir(Path(cfg.backup_root))
        _write_status(cfg, [], datetime.now(UTC), reason=f"killed by {name}")
    except Exception as exc:
        try:
            logger.error("signal handler failed to write status.json: %s", exc)
        except Exception:
            pass


def _install_signal_handlers(cfg: Config) -> None:
    """Install SIGTERM/SIGINT handlers so a kill mid-run cannot leave status.json
    reporting yesterday's success (audit C2).

    ``TimeoutStartSec=3600`` sends SIGTERM to a run that overruns; the default
    disposition tears the interpreter down immediately, so cmd_run's own
    ``except Exception`` — and therefore ``_write_status`` — never runs at all, and
    status.json still says ``ok: true`` from the night before. Installed inside
    main(), not at import: importing this module (as the unit tests do) must never
    change the importing process's signal disposition.
    """

    def _handle(signum: int, _frame: object) -> None:
        _write_signal_status(cfg, signum)
        # os._exit(), not sys.exit(): this must not itself raise. sys.exit() raises
        # SystemExit into whatever arbitrary code the signal interrupted, which
        # could be caught by a surrounding `except Exception` — no, SystemExit is
        # not Exception, but it could still be caught by `except BaseException`
        # somewhere on the interrupted stack, or trigger unexpected `finally`
        # cleanup mid-teardown. os._exit() terminates unconditionally and
        # immediately: exactly and only what a kill signal's handler should do.
        os._exit(1)

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="copi-backup")
    parser.add_argument("command", choices=["run", "report", "prune"])
    parser.add_argument("--config", default="/etc/copi-backup/backup.env")
    # Two distinct flags on purpose. `prune --dry-run` is a true dry run: it lists what
    # it would delete and deletes nothing. `run` has no dry mode — it always dumps and
    # verifies — so its flag says exactly what it does. Calling that one --dry-run would
    # invite an operator to "safely" trigger a 721MB dump and two containers on a
    # production host.
    parser.add_argument("--dry-run", action="store_true", help="prune only: list, do not delete")
    parser.add_argument("--no-prune", action="store_true", help="run only: skip retention")
    args = parser.parse_args(argv)
    # argparse accepts both flags for every subcommand because they are global, so the
    # mismatched combinations are rejected explicitly. Without this, `run --dry-run`
    # parses cleanly and is silently IGNORED — the operator gets a full 721MB dump from
    # a command they believed was a no-op, which is precisely the footgun the two
    # separate flags exist to prevent.
    if args.command == "run" and args.dry_run:
        parser.error(
            "run has no dry mode: it always dumps and verifies. "
            "Use `run --no-prune` to skip retention."
        )
    if args.command != "run" and args.no_prune:
        parser.error("--no-prune applies to `run`; use `prune --dry-run` to preview.")

    # Configured here, not at module import time: systemd captures a unit's stdout
    # into the journal, so writing there is what makes a failed run visible at all
    # (audit F1) — but this must not reconfigure logging for anything that merely
    # imports this module (e.g. the unit tests, which load it via importlib).
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)

    cfg = load_config(read_env_file(Path(args.config).read_text()))
    now = datetime.now(UTC)
    _install_signal_handlers(cfg)

    with open(LOCK_PATH, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        if args.command == "run":
            return cmd_run(cfg, Runner(), now, args.no_prune)
        if args.command == "report":
            return cmd_report(cfg, now)
        for path in prune(cfg, dry_run=args.dry_run):
            sys.stdout.write(f"{path}\n")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
