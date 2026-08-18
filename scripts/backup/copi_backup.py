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
