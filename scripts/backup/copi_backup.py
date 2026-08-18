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

import re
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime

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
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        result = Completed(argv, proc.returncode, proc.stdout, proc.stderr)
        if check and proc.returncode != 0:
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


def docker_exec(container: str, argv: list[str]) -> list[str]:
    """docker exec WITHOUT -t: a TTY would translate newlines and corrupt binary."""
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
