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

import hashlib
import os
import re
import secrets
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

DEFAULTS = {
    "BACKUP_ROOT": "/var/backups/copi",
    "RETENTION_COUNT": "5",
    "RETENTION_UNVERIFIED": "2",
    "VERIFY_IMAGE": "postgres:15",
    "VERIFY_MEM": "768m",
    "VERIFY_TIMEOUT_SEC": "1800",
    "FREE_SPACE_FACTOR": "3",
    "OFFSITE_CMD": "",
    "AWS_REGION": "us-east-2",
    "SES_SENDER_EMAIL": "",
    "MAIL_TO": "",
}


class ConfigError(Exception):
    """Raised for any malformed value in /etc/copi-backup/backup.env."""


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
        if name in seen:
            raise ConfigError(f"duplicate stack name: {name!r}")
        seen.add(name)
        stacks.append(Stack(name=name, container=container, db=db, user=user))
    if not stacks:
        raise ConfigError("no stacks configured")
    return stacks


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
        backup_root=env.get("BACKUP_ROOT", DEFAULTS["BACKUP_ROOT"]).strip(),
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
        except subprocess.TimeoutExpired:
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
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / dump_name(stack.name, stack.db, now)
    partial = final.with_suffix(final.suffix + ".partial")
    ctmp = f"/tmp/copi_backup_{os.getpid()}_{stack.name}.dump"

    try:
        with session_factory(stack) as session:
            runner.run(pg_dump_argv(stack, session.snapshot_id, ctmp), timeout=cfg.verify_timeout_sec)
            # TOC readable inside the container, on a real seekable path.
            runner.run(docker_exec(stack.container, ["pg_restore", "-l", ctmp]))
            runner.run(["docker", "cp", f"{stack.container}:{ctmp}", str(partial)])
            counts = session.counts()
            snapshot_id = session.snapshot_id
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    finally:
        runner.run(docker_exec(stack.container, ["rm", "-f", ctmp]), check=False)

    with partial.open("rb") as handle:
        os.fsync(handle.fileno())
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
            ready = runner.run(
                docker_exec(name, ["pg_isready", "-U", stack.user, "-q"]), check=False
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


def build_status(results: list[StackResult], now: datetime) -> dict:
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "last_run_utc": stamp,
        "ok": all(r.ok for r in results) and bool(results),
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


def render_failure_mail(results: list[StackResult], now: datetime) -> tuple[str, str]:
    failed = [r for r in results if not r.ok]
    names = ",".join(r.stack for r in failed) or "no-results"
    subject = f"[copi-backup] FAILED {names} {now:%Y-%m-%d}"
    lines = [f"Backup run {now:%Y-%m-%d %H:%M:%S} UTC", ""]
    if not results:
        lines += [
            "The run produced NO results at all — it aborted before any stack was "
            "processed.",
            "Check: systemctl status copi-backup.service",
            "",
        ]
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


def render_heartbeat_mail(history: list[dict], now: datetime) -> tuple[str, str]:
    subject = f"[copi-backup] weekly summary {now:%Y-%m-%d}"
    lines = [f"Weekly backup summary, {now:%Y-%m-%d} UTC", ""]
    if not history:
        lines.append("NO RUNS RECORDED IN THE LAST 7 DAYS — the timer may not be firing.")
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
