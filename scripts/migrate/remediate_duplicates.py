"""Clear duplicate ``(simulation_run_id, message_ts)`` rows so 0019 can apply.

WHY THIS EXISTS
---------------
Migration 0019 ends with::

    op.create_unique_constraint(
        "uq_agent_messages_run_ts", "agent_messages", ["simulation_run_id", "message_ts"]
    )

On a production database at 0018 that holds two or more ``agent_messages`` rows
sharing a non-NULL ``(simulation_run_id, message_ts)``, that statement aborts::

    UniqueViolationError: could not create unique index "uq_agent_messages_run_ts"
    DETAIL:  Key (simulation_run_id, message_ts)=(…) is duplicated.

Three measured facts about that failure shape this tool (all reproduced on a
throwaway Postgres 15 at 0018, see the report accompanying this change):

1. ``alembic/env.py`` deliberately runs the whole chain in ONE transaction, so a
   failure rolls the database all the way back to 0018 — nothing is half-applied,
   but nothing is gained either.
2. The error names exactly ONE duplicate group per attempt. With 9 duplicate
   groups it took 10 ``ALTER TABLE`` attempts to walk them all.
3. That ``ALTER TABLE`` requests **AccessExclusiveLock** on ``agent_messages``
   (verified in ``pg_locks``: it blocks behind a single open ``BEGIN; SELECT``).
   A pending ACCESS EXCLUSIVE request also queues ahead of new readers, so every
   attempt is a stall on the hot table. Iterating on migration failures therefore
   costs one ACCESS EXCLUSIVE lock cycle per duplicate group. Remediating up
   front costs one.

Rows with ``message_ts IS NULL`` are NOT affected: the index Postgres builds is
NULLS DISTINCT (``pg_index.indnullsnotdistinct = false``), so any number of NULL
``message_ts`` rows coexist. Verified with 65 such rows present while the
constraint was created successfully. This tool never looks at them.

DELETE vs RENUMBER
------------------
Deleting a row destroys conversation history; the DB is the durable store from
0019 on, so a dropped message is unrecoverable. Renumbering a ``message_ts``
that is a real Slack timestamp destroys the only record of that timestamp while
the ``slack_ts`` column does not yet exist (0018), which is the same
timestamp-fabrication mistake ``scripts/backfill_slack_ts.py`` was written to
undo. So neither action is safe in general, and the tool decides per group:

* A group whose rows are **payload-identical** (equal on every column except
  ``id`` and ``created_at``) is one message logged twice. The extra rows carry
  nothing the survivor does not, so DELETING them loses no information and is
  the semantically correct repair — but it is destructive, so it happens only
  under ``--strategy keep-earliest`` / ``keep-latest``.
* A **divergent** group (the rows differ in content, sender, channel, length…)
  is never resolved by deletion, under any strategy. Each row may carry
  something unique, so the rows are preserved and the ones whose ``message_ts``
  may safely change are RENUMBERED.
* "May safely change" is decided per row by ``classify_origin`` below. A
  ``message_ts`` that is (or may be) a Slack-issued timestamp and is recorded
  nowhere else must not change.
* A group where **two or more** rows look Slack-born and the rows are not
  payload-identical cannot be repaired without guessing which row's timestamp is
  the wrong one. The tool REFUSES it and tells the operator exactly what to look
  at. It never guesses.

Renumbered ids are minted in a writer slot no live minter owns
(``REMEDIATION_WRITER_SLOT``), in the same microsecond neighbourhood as the id
they replace, and checked against every ``message_ts``/``thread_ts``/
``thread_decisions.thread_id`` already present in that run. See
``mint_replacement_ts``.

USAGE
-----
Dry run (default — writes nothing, in a READ ONLY transaction)::

    docker compose exec -e PYTHONPATH=/app app \\
        python scripts/migrate/remediate_duplicates.py

Apply::

    docker compose exec -e PYTHONPATH=/app app \\
        python scripts/migrate/remediate_duplicates.py --apply --strategy keep-earliest

``PYTHONPATH=/app`` is REQUIRED and is not decoration. ``python <path>/x.py`` puts
the *script's* directory on ``sys.path[0]``, not the repo root, so ``import src``
resolves to the copy baked into the image at build time
(``/usr/local/lib/python3.11/site-packages/src``) rather than the mounted
``/app/src``. Verified inside ``copiscience-app-1``. The header this tool prints
names the ``src/agent/ids.py`` it actually loaded, so you can see which copy you
got, and it hard-fails if that copy's writer-slot scheme is not the one it
expects.

EXIT CODES
----------
0   no duplicates, or ``--apply`` finished and none remain (re-verified in the
    same transaction as the writes).
1   duplicates remain, or would remain under the chosen strategy.
2   duplicates found in a dry run and the plan resolves all of them. A runbook
    can branch on this: 2 means "there is work to do and it is safe to do".
3   operational failure (no DSN, database unreachable, ``agent_messages``
    missing, lock timeout, id scheme unrecognised). Deliberately not 1 or 2 so a
    runbook cannot mistake a broken run for a verdict about the data.
64  usage error (``EX_USAGE``). argparse's own default is 2, which would be
    indistinguishable from "duplicates found"; it is remapped.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, NoReturn

# --------------------------------------------------------------------------- #
# The id scheme. Imported, not copied, so it cannot drift from the minters --
# and then validated, because the import may have come from a stale baked copy
# of src/ (see the PYTHONPATH note in the module docstring).
# --------------------------------------------------------------------------- #

#: Writer slot used for renumbered ids. ``src/agent/ids.py`` gives every minter a
#: residue class of the microsecond field so two processes can never mint the
#: same id; 0-3 are claimed by the engine, the web app, GrantBot and the engine's
#: module-default minter. 99 is claimed here, at the far end of the range, so a
#: remediated id cannot collide with anything a running system mints -- and so a
#: renumbered id is recognisable as remediated at a glance.
REMEDIATION_WRITER_SLOT = 99

#: What this tool assumes ``src/agent/ids.py`` uses. If the loaded module says
#: something else, the collision argument above is void and we stop.
EXPECTED_SLOT_MODULUS = 100

#: A ts-shaped id: "<seconds>.<exactly six microsecond digits>". Both Slack
#: timestamps and locally minted ids take this form. Anything else was never
#: issued by Slack.
#:
#: ``\Z``, not ``$``: ``$`` also matches immediately BEFORE a trailing newline, so
#: ``"1755000005.000001\n"`` matched and was then treated as a well-formed id --
#: which would have had the tool renumber a neighbouring row rather than the
#: malformed one, and compare a normalised id against an un-normalised column.
#: ``[0-9]`` rather than ``\d`` for the same reason: ``\d`` matches Unicode digits
#: that ``int()`` accepts but Slack never issues.
TS_SHAPE = re.compile(r"\A[0-9]{1,19}\.[0-9]{6}\Z")

#: Channel ids for channels that exist only in the DB. ``SimulationEngine``
#: writes ``f"local:{channel}"`` when there is no Slack channel to mirror into,
#: so a row with this prefix was never posted to Slack and its ``message_ts``
#: cannot be a Slack timestamp. Same predicate ``scripts/backfill_slack_ts.py``
#: uses to skip rows it must not ask Slack about.
LOCAL_CHANNEL_PREFIX = "local:"

#: Columns that do NOT count as payload when deciding whether two rows are the
#: same message logged twice. ``id`` must differ (it is the primary key) and
#: ``created_at`` is the DB's own bookkeeping clock, not the message.
NON_PAYLOAD_COLUMNS = frozenset({"id", "created_at"})

TABLE = "agent_messages"
CONSTRAINT = "uq_agent_messages_run_ts"

EXIT_CLEAN = 0
EXIT_REMAIN = 1
EXIT_FOUND_DRY_RUN = 2
EXIT_OPERATIONAL = 3
EXIT_USAGE = 64


def load_id_scheme() -> tuple[int, dict[int, str], str]:
    """Return ``(modulus, {slot: writer name}, module path)`` from src.agent.ids.

    Raises ``SchemeError`` if the module cannot be imported or if its scheme is
    not the one ``REMEDIATION_WRITER_SLOT`` was chosen against. Failing loudly
    here is the point: a silently stale ``src`` could hand back a different
    modulus, and renumbered ids would then land in a slot a live minter owns.
    """
    try:
        from src.agent import ids as ids_mod
    except ImportError as exc:  # pragma: no cover - exercised by hand, not in CI
        raise SchemeError(
            f"cannot import src.agent.ids ({exc}). Run this from the repo root, or "
            "inside the container with PYTHONPATH=/app:\n"
            "  docker compose exec -e PYTHONPATH=/app app "
            "python scripts/migrate/remediate_duplicates.py"
        ) from exc

    modulus = getattr(ids_mod, "WRITER_SLOT_MODULUS", None)
    if modulus != EXPECTED_SLOT_MODULUS:
        raise SchemeError(
            f"src.agent.ids.WRITER_SLOT_MODULUS is {modulus!r}, expected "
            f"{EXPECTED_SLOT_MODULUS}. REMEDIATION_WRITER_SLOT="
            f"{REMEDIATION_WRITER_SLOT} was chosen against the latter; with a "
            "different modulus a renumbered id could land in a live minter's "
            f"slot. Loaded from {getattr(ids_mod, '__file__', '?')}."
        )

    slots: dict[int, str] = {}
    for name in dir(ids_mod):
        if not name.startswith("WRITER_") or name == "WRITER_SLOT_MODULUS":
            continue
        value = getattr(ids_mod, name)
        if isinstance(value, int) and not isinstance(value, bool):
            slots[value] = name
    if not slots:
        raise SchemeError(
            "src.agent.ids declares no WRITER_* slots; refusing to guess which "
            "residue classes are in use."
        )
    if REMEDIATION_WRITER_SLOT in slots:
        raise SchemeError(
            f"writer slot {REMEDIATION_WRITER_SLOT} is now claimed by "
            f"{slots[REMEDIATION_WRITER_SLOT]}. Pick a free slot for "
            "REMEDIATION_WRITER_SLOT before running this."
        )
    return modulus, slots, getattr(ids_mod, "__file__", "?")


class SchemeError(RuntimeError):
    """The loaded id scheme is not the one renumbering was designed against."""


# --------------------------------------------------------------------------- #
# Pure logic. No DB, no I/O -- this is what tests/unit/test_remediate_duplicates.py
# exercises.
# --------------------------------------------------------------------------- #

# Origin verdicts, in descending order of confidence.
ORIGIN_LOCAL_CONFIRMED = "local_confirmed"
ORIGIN_SLACK_CONFIRMED = "slack_confirmed"
ORIGIN_LOCAL_PRESUMED = "local_presumed"
ORIGIN_SLACK_PRESUMED = "slack_presumed"

RENUMBER_SAFE = "safe"
RENUMBER_UNSAFE = "unsafe"

KIND_REDUNDANT = "redundant"
KIND_DIVERGENT = "divergent"

RESOLUTION_DELETE = "delete"
RESOLUTION_RENUMBER = "renumber"
RESOLUTION_NEEDS_DELETE_STRATEGY = "needs_delete_strategy"
RESOLUTION_NEEDS_HUMAN = "needs_human"

UNRESOLVED = frozenset({RESOLUTION_NEEDS_DELETE_STRATEGY, RESOLUTION_NEEDS_HUMAN})

STRATEGY_KEEP_EARLIEST = "keep-earliest"
STRATEGY_KEEP_LATEST = "keep-latest"
STRATEGY_RENUMBER = "renumber"
STRATEGIES = (STRATEGY_KEEP_EARLIEST, STRATEGY_KEEP_LATEST, STRATEGY_RENUMBER)

ACTION_KEEP = "keep"
ACTION_RENUMBER = "renumber"
ACTION_DELETE = "delete"


def parse_ts_us(ts: str | None) -> int | None:
    """Return integer microseconds-since-epoch for a ts-shaped id, else None.

    Only the exact ``<seconds>.<6 digits>`` form is accepted. ``"1755000000.2"``
    is rejected rather than guessed at: 2 microseconds and 200000 microseconds
    are both plausible readings and picking one silently would move a message by
    a fifth of a second.
    """
    if not ts or not TS_SHAPE.match(ts):
        return None
    seconds, _, micros = ts.partition(".")
    return int(seconds) * 1_000_000 + int(micros)


def format_us(us: int) -> str:
    """Format integer microseconds as a ts-shaped id.

    Byte-identical to ``src.agent.ids._fmt``; the unit tests pin that agreement
    so the two cannot drift.
    """
    return f"{us // 1_000_000}.{us % 1_000_000:06d}"


@dataclass(frozen=True)
class Origin:
    verdict: str
    evidence: str

    @property
    def is_slack(self) -> bool:
        return self.verdict in (ORIGIN_SLACK_CONFIRMED, ORIGIN_SLACK_PRESUMED)


@dataclass
class MessageRow:
    """One ``agent_messages`` row, revision-agnostic.

    ``columns`` holds every column the database actually has, so the same code
    works at 0018 (no ``content``/``slack_ts``) and at 0019 (both present).
    """

    row_id: str
    run_id: str
    message_ts: str
    created_at: datetime | None
    columns: dict[str, Any]

    # Filled in by planning.
    origin: Origin | None = None
    renumber_verdict: str = RENUMBER_UNSAFE
    renumber_reason: str = ""
    action: str = ACTION_KEEP
    new_message_ts: str | None = None

    @property
    def channel_id(self) -> str:
        return str(self.columns.get("channel_id") or "")

    @property
    def slack_ts(self) -> str | None:
        value = self.columns.get("slack_ts")
        return None if value is None else str(value)

    def payload(self) -> tuple:
        """The row's identity for "is this the same message twice?".

        Values are compared by ``repr``, not by ``==``. That is deliberately
        STRICTER than equality (it will not call ``Decimal("1")`` equal to ``1``)
        and it is total: every column type reprs, including a JSON column that
        would be unhashable as a set member. Erring strict means a row is judged
        "redundant" -- and therefore deletable -- only when it really is
        indistinguishable.
        """
        return tuple(
            (name, repr(value))
            for name, value in sorted(self.columns.items())
            if name not in NON_PAYLOAD_COLUMNS
        )

    def sort_key(self) -> tuple:
        """Deterministic ordering: DB insert clock, then primary key."""
        # created_at is NOT NULL in the schema, but a NULL would otherwise make
        # the comparison explode rather than just sort first.
        return (self.created_at is not None, self.created_at, self.row_id)


def classify_origin(row: MessageRow, *, has_slack_columns: bool, writer_slots: dict[int, str],
                    modulus: int) -> Origin:
    """Decide whether this row's ``message_ts`` is a Slack timestamp.

    Ordered strongest evidence first. The two "confirmed" verdicts are facts
    about the row; the two "presumed" ones are judgement calls, and the tool says
    which it used for every row it reports.
    """
    ts = row.message_ts

    # 1. The canonical id and the Slack ts are already recorded separately and
    #    they differ, so message_ts is a local id by construction (0019+).
    if has_slack_columns and row.slack_ts is not None and row.slack_ts != ts:
        return Origin(
            ORIGIN_LOCAL_CONFIRMED,
            f"slack_ts={row.slack_ts} differs from message_ts, so the canonical id "
            "is already decoupled from the Slack timestamp",
        )

    # 2. The message never went to Slack, so no Slack ts exists to protect.
    if row.channel_id.startswith(LOCAL_CHANNEL_PREFIX):
        return Origin(
            ORIGIN_LOCAL_CONFIRMED,
            f"channel_id {row.channel_id!r} is DB-only, so this message was never "
            "posted to Slack",
        )

    # 3. Slack never issues an id of this shape.
    us = parse_ts_us(ts)
    if us is None:
        return Origin(
            ORIGIN_LOCAL_CONFIRMED,
            f"message_ts {ts!r} is not ts-shaped (<seconds>.<6 digits>); Slack "
            "never issues this form",
        )

    # 4. Confirmed Slack ts. Renumbering message_ts is still safe here because
    #    slack_ts keeps the Slack timestamp -- see renumber_verdict().
    if has_slack_columns and row.slack_ts == ts:
        return Origin(ORIGIN_SLACK_CONFIRMED, "slack_ts == message_ts")

    # 5. Writer-slot residue. This is the ONLY signal available at 0018 for a
    #    locally minted id that also carries a real Slack channel id -- a PI
    #    message written through the web inbox, or an agent post whose Slack
    #    mirror failed (both called out in scripts/backfill_slack_ts.py). It is a
    #    judgement call: a random Slack ts lands in one of the four claimed slots
    #    about 4% of the time.
    residue = us % modulus
    if residue in writer_slots:
        return Origin(
            ORIGIN_LOCAL_PRESUMED,
            f"microsecond residue {residue:02d} is writer slot "
            f"{writer_slots[residue]} (src/agent/ids.py), so this looks locally "
            "minted despite the Slack channel id",
        )

    # 6. Nothing says local, and it is a ts-shaped id in a real Slack channel.
    return Origin(
        ORIGIN_SLACK_PRESUMED,
        f"ts-shaped id in Slack channel {row.channel_id}, microsecond residue "
        f"{residue:02d} matches no writer slot",
    )


def renumber_verdict(row: MessageRow, origin: Origin, *, has_slack_columns: bool) -> tuple[str, str]:
    """May this row's ``message_ts`` be changed without losing information?"""
    if origin.verdict == ORIGIN_LOCAL_CONFIRMED:
        return RENUMBER_SAFE, "no Slack-issued timestamp to lose"
    if origin.verdict == ORIGIN_LOCAL_PRESUMED:
        return RENUMBER_SAFE, (
            "presumed locally minted from its writer slot; if that presumption is "
            "wrong the cost is a lost Slack-mirror mapping, recoverable with "
            "scripts/backfill_slack_ts.py"
        )
    if origin.verdict == ORIGIN_SLACK_CONFIRMED:
        # slack_ts is a separate column from 0019 on, so the Slack timestamp
        # survives a change to message_ts. This is exactly what 0018 cannot do.
        return RENUMBER_SAFE, "slack_ts keeps the Slack timestamp; only the canonical id changes"
    where = "slack_ts" if has_slack_columns else "the 0018 schema has no slack_ts column, so"
    return RENUMBER_UNSAFE, (
        f"message_ts may be a Slack-issued timestamp and {where} it is the only "
        "record of it; changing it fabricates an id Slack never issued"
    )


def mint_replacement_ts(original_ts: str, used: set[str], *, now_us: int,
                        modulus: int = EXPECTED_SLOT_MODULUS,
                        slot_id: int = REMEDIATION_WRITER_SLOT,
                        max_probes: int = 1000) -> str:
    """Mint a ts-shaped id that collides with nothing, as close to the original as possible.

    Two properties matter, and they pull against each other:

    * **No collision.** The id lands in writer slot ``slot_id``, a residue class
      no live minter uses, AND it is checked against ``used`` -- every
      ``message_ts``, ``thread_ts`` and ``thread_decisions.thread_id`` already
      present in that run, plus everything minted earlier in this same plan. So
      it cannot collide with existing data, with another replacement, or with an
      id a running engine goes on to mint.
    * **Ordering.** ``message_ts`` doubles as the chronological key (0018) and
      seeds ``posted_at`` (0019), so a replacement that jumps to "now" would
      teleport an old message to the end of the conversation. The candidate is
      therefore taken from the original's own microsecond slot: it lands at most
      ``modulus * probes`` microseconds AFTER the id it replaces -- one slot in
      the ordinary case, i.e. under 100µs.

    A ``message_ts`` that is not ts-shaped has no neighbourhood to stay in, so
    those fall back to ``now_us``. Raises ``RuntimeError`` rather than looping
    forever if ``max_probes`` consecutive slots are all taken.
    """
    us = parse_ts_us(original_ts)
    if us is None:
        us = now_us
    slot = us // modulus
    for _ in range(max_probes):
        candidate = format_us(slot * modulus + slot_id)
        if candidate not in used and candidate != original_ts:
            return candidate
        slot += 1
    raise RuntimeError(
        f"no free id in {max_probes} consecutive writer-{slot_id} slots after "
        f"{original_ts!r}; refusing to renumber"
    )


@dataclass
class DuplicateGroup:
    run_id: str
    message_ts: str
    rows: list[MessageRow]
    #: rows elsewhere in the run whose thread_ts points at this message_ts
    thread_reply_count: int = 0
    #: channel_ids those replies live in -- used to pick which row is the real root
    thread_reply_channel_ids: set[str] = field(default_factory=set)
    #: thread_decisions rows whose thread_id points at this message_ts
    thread_decision_count: int = 0

    kind: str = KIND_DIVERGENT
    resolution: str = RESOLUTION_NEEDS_HUMAN
    reason: str = ""
    anchor_id: str | None = None

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def resolved(self) -> bool:
        return self.resolution not in UNRESOLVED

    @property
    def referenced(self) -> bool:
        return bool(self.thread_reply_count or self.thread_decision_count)


def pick_anchor(rows: list[MessageRow], *, strategy: str,
                reply_channel_ids: set[str]) -> MessageRow:
    """Choose the row that KEEPS the original ``message_ts``.

    Order of preference:

    1. The one row whose ts must not change (there is at most one, or the group
       would have been refused).
    2. A row in a channel the thread replies live in. Replies point at a ts, not
       at a row id, so whichever row keeps the ts inherits the replies; keeping
       the ts on the row that actually started the thread is what makes those
       pointers still mean something.
    3. Oldest row (``keep-latest``: newest), tie-broken on the primary key so the
       choice is reproducible across runs and machines.
    """
    unsafe = [r for r in rows if r.renumber_verdict == RENUMBER_UNSAFE]
    if unsafe:
        return min(unsafe, key=MessageRow.sort_key)
    if reply_channel_ids:
        in_reply_channel = [r for r in rows if r.channel_id in reply_channel_ids]
        if in_reply_channel:
            return min(in_reply_channel, key=MessageRow.sort_key)
    if strategy == STRATEGY_KEEP_LATEST:
        return max(rows, key=MessageRow.sort_key)
    return min(rows, key=MessageRow.sort_key)


def plan_group(group: DuplicateGroup, *, strategy: str, used: set[str], now_us: int,
               has_slack_columns: bool, writer_slots: dict[int, str], modulus: int) -> None:
    """Classify ``group`` and fill in each row's action, in place.

    ``used`` is mutated: every id handed out is added, so two groups in the same
    run can never be given the same replacement.
    """
    for row in group.rows:
        row.origin = classify_origin(
            row, has_slack_columns=has_slack_columns, writer_slots=writer_slots,
            modulus=modulus,
        )
        row.renumber_verdict, row.renumber_reason = renumber_verdict(
            row, row.origin, has_slack_columns=has_slack_columns
        )
        row.action = ACTION_KEEP
        row.new_message_ts = None

    payloads = {row.payload() for row in group.rows}
    group.kind = KIND_REDUNDANT if len(payloads) == 1 else KIND_DIVERGENT
    unsafe = [r for r in group.rows if r.renumber_verdict == RENUMBER_UNSAFE]
    deletion_allowed = strategy in (STRATEGY_KEEP_EARLIEST, STRATEGY_KEEP_LATEST)

    if group.kind == KIND_REDUNDANT and deletion_allowed:
        keep = (max if strategy == STRATEGY_KEEP_LATEST else min)(
            group.rows, key=MessageRow.sort_key
        )
        group.resolution = RESOLUTION_DELETE
        group.anchor_id = keep.row_id
        group.reason = (
            f"{group.row_count} rows equal on every column except id and created_at "
            f"— one message logged {group.row_count} times. Deleting the "
            f"{group.row_count - 1} extra row(s) loses no information."
        )
        for row in group.rows:
            row.action = ACTION_KEEP if row is keep else ACTION_DELETE
        return

    if len(unsafe) <= 1:
        anchor = pick_anchor(
            group.rows, strategy=strategy, reply_channel_ids=group.thread_reply_channel_ids
        )
        group.resolution = RESOLUTION_RENUMBER
        group.anchor_id = anchor.row_id
        if group.kind == KIND_REDUNDANT:
            group.reason = (
                f"{group.row_count} byte-identical rows, all renumberable. Every row "
                "is preserved, so nothing is lost — but the same message will then "
                "appear twice in rebuilt history. --strategy keep-earliest would "
                "delete the redundant copy instead, which is almost certainly what "
                "you want."
            )
        elif unsafe:
            group.reason = (
                "rows differ, so no row may be deleted. Exactly one row's ts must not "
                f"change ({unsafe[0].row_id}); it keeps the ts and the others are renumbered."
            )
        else:
            group.reason = (
                "rows differ, so no row may be deleted. Every ts here is safe to "
                "change, so one row keeps the ts and the others are renumbered."
            )
        for row in group.rows:
            if row is anchor:
                continue
            row.action = ACTION_RENUMBER
            row.new_message_ts = mint_replacement_ts(row.message_ts, used, now_us=now_us)
            used.add(row.new_message_ts)
        return

    if group.kind == KIND_REDUNDANT:
        group.resolution = RESOLUTION_NEEDS_DELETE_STRATEGY
        group.reason = (
            f"{len(unsafe)} of {group.row_count} rows carry a ts that must not change, "
            "so renumbering cannot resolve this group. The rows ARE byte-identical, "
            "so deleting the extras loses nothing: re-run with "
            "--strategy keep-earliest."
        )
        return

    group.resolution = RESOLUTION_NEEDS_HUMAN
    group.reason = (
        f"{len(unsafe)} of {group.row_count} rows look Slack-born AND the rows differ. "
        "No id may change and no row may be dropped, so one of these rows carries a "
        "timestamp that is simply wrong and only a human can say which. "
        "REFUSING to guess."
    )


def needs_human_advice(group: DuplicateGroup) -> list[str]:
    """Exactly what an operator should look at for a refused group."""
    lines = [
        "  What to check, in order:",
        f"    1. Ask Slack which of these rows is real. For each row's channel_id, "
        f"call conversations.history / conversations.replies with "
        f"latest=oldest={group.message_ts} inclusive=true (scripts/backfill_slack_ts.py "
        "does exactly this lookup, and a thread reply MUST be looked up with "
        "conversations.replies — history does not return replies).",
        "    2. At most one row can be the message Slack actually holds at that ts. "
        "Any row Slack does not confirm has a fabricated or mis-copied ts.",
        "    3. Compare the two bodies. If one row is empty or truncated, it is the "
        "mis-logged one.",
    ]
    if group.referenced:
        lines.append(
            f"    4. {group.thread_reply_count} reply row(s) and "
            f"{group.thread_decision_count} thread_decisions row(s) point at this ts. "
            "Whichever row keeps it inherits them, so decide which row is the real "
            "thread root before you touch anything."
        )
    lines.append(
        "    Then fix that ONE row by hand (correct its message_ts, or delete it if "
        "it is a mis-log) and re-run this tool."
    )
    return lines


# --------------------------------------------------------------------------- #
# Database layer.
# --------------------------------------------------------------------------- #

DUP_KEYS_SQL = f"""
    SELECT simulation_run_id, message_ts, count(*) AS n
    FROM {TABLE}
    WHERE message_ts IS NOT NULL
    GROUP BY simulation_run_id, message_ts
    HAVING count(*) > 1
    ORDER BY simulation_run_id, message_ts
"""

DUP_ROWS_SQL_TEMPLATE = """
    SELECT {cols}
    FROM {table} m
    WHERE m.message_ts IS NOT NULL
      AND EXISTS (
          SELECT 1 FROM {table} d
          WHERE d.simulation_run_id = m.simulation_run_id
            AND d.message_ts = m.message_ts
            AND d.id <> m.id
      )
    ORDER BY m.simulation_run_id, m.message_ts, m.created_at, m.id
"""


def normalise_dsn(dsn: str) -> str:
    """Force an async driver onto the DSN; this tool only speaks asyncpg."""
    if dsn.startswith("postgresql+"):
        return dsn
    if dsn.startswith("postgresql://"):
        return "postgresql+asyncpg://" + dsn[len("postgresql://"):]
    if dsn.startswith("postgres://"):
        return "postgresql+asyncpg://" + dsn[len("postgres://"):]
    return dsn


def redact_dsn(dsn: str) -> str:
    """Mask only the password, keeping host/database legible (src/config.py does the same)."""
    return re.sub(r"(://[^:/@]+:)[^@]*(@)", r"\1***\2", dsn)


async def fetch_schema(conn) -> dict[str, Any]:
    from sqlalchemy import text

    cols = {
        r[0]
        for r in (
            await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = :t"
                ),
                {"t": TABLE},
            )
        ).all()
    }
    if not cols:
        raise OperationalFailure(
            f"table {TABLE!r} does not exist in the current schema — is this the "
            "right database?"
        )
    revision = None
    has_version_table = (
        await conn.execute(text("SELECT to_regclass('alembic_version')"))
    ).scalar()
    if has_version_table:
        revision = (
            await conn.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
        ).scalar()
    constraint_present = bool(
        (
            await conn.execute(
                text("SELECT 1 FROM pg_constraint WHERE conname = :c"), {"c": CONSTRAINT}
            )
        ).scalar()
    )
    thread_decisions = bool(
        (await conn.execute(text("SELECT to_regclass('thread_decisions')"))).scalar()
    )
    total, null_ts = (
        await conn.execute(
            text(f"SELECT count(*), count(*) - count(message_ts) FROM {TABLE}")
        )
    ).one()
    return {
        "columns": sorted(cols),
        "alembic_revision": revision,
        "has_content": "content" in cols,
        "has_slack_ts": "slack_ts" in cols,
        "constraint_present": constraint_present,
        "has_thread_decisions": thread_decisions,
        "total_rows": total,
        "null_message_ts_rows": null_ts,
    }


class OperationalFailure(RuntimeError):
    """Something about the environment is wrong; not a verdict about the data."""


async def load_groups(conn, columns: list[str], has_thread_decisions: bool) -> list[DuplicateGroup]:
    from sqlalchemy import text

    quoted = ", ".join(f'm."{c}"' for c in columns)
    sql = DUP_ROWS_SQL_TEMPLATE.format(cols=quoted, table=TABLE)
    result = await conn.execute(text(sql))
    keys = list(result.keys())
    by_key: dict[tuple[str, str], DuplicateGroup] = {}
    for record in result.all():
        values = dict(zip(keys, record, strict=True))
        run_id = str(values["simulation_run_id"])
        ts = str(values["message_ts"])
        row = MessageRow(
            row_id=str(values["id"]),
            run_id=run_id,
            message_ts=ts,
            created_at=values.get("created_at"),
            columns=values,
        )
        by_key.setdefault((run_id, ts), DuplicateGroup(run_id, ts, [])).rows.append(row)

    if not by_key:
        return []

    run_ids = sorted({run for run, _ in by_key})

    # Rows elsewhere in these runs that point at a duplicated ts as their thread
    # root. Runs are matched as text, not as a uuid[] bind: asyncpg has to infer
    # the array's element type, and handing it Python uuid objects for an
    # untyped placeholder is one more thing to get wrong for no benefit.
    replies = (
        await conn.execute(
            text(
                f"SELECT simulation_run_id, thread_ts, count(*) AS n, "
                f"       array_agg(DISTINCT channel_id) AS channels "
                f"FROM {TABLE} "
                f"WHERE thread_ts IS NOT NULL AND simulation_run_id::text = ANY(:runs) "
                f"GROUP BY simulation_run_id, thread_ts"
            ),
            {"runs": run_ids},
        )
    ).all()
    for run_id, thread_ts, count, channels in replies:
        group = by_key.get((str(run_id), str(thread_ts)))
        if group is not None:
            group.thread_reply_count = count
            group.thread_reply_channel_ids = {str(c) for c in (channels or [])}

    if has_thread_decisions:
        decisions = (
            await conn.execute(
                text(
                    "SELECT simulation_run_id, thread_id, count(*) FROM thread_decisions "
                    "WHERE simulation_run_id::text = ANY(:runs) "
                    "GROUP BY simulation_run_id, thread_id"
                ),
                {"runs": run_ids},
            )
        ).all()
        for run_id, thread_id, count in decisions:
            group = by_key.get((str(run_id), str(thread_id)))
            if group is not None:
                group.thread_decision_count = count

    return [by_key[k] for k in sorted(by_key)]


async def load_used_ids(conn, run_ids: list[str], has_thread_decisions: bool) -> dict[str, set[str]]:
    """Every id already spoken for in each affected run.

    ``message_ts`` is what the unique constraint covers, but ``thread_ts`` and
    ``thread_decisions.thread_id`` are soft references BY VALUE: handing a
    renumbered row an id that some reply already names as its thread root would
    silently re-parent that reply. So all three are treated as taken.
    """
    from sqlalchemy import text

    used: dict[str, set[str]] = defaultdict(set)
    rows = (
        await conn.execute(
            text(
                f"SELECT simulation_run_id, message_ts FROM {TABLE} "
                f"WHERE message_ts IS NOT NULL AND simulation_run_id::text = ANY(:runs) "
                f"UNION "
                f"SELECT simulation_run_id, thread_ts FROM {TABLE} "
                f"WHERE thread_ts IS NOT NULL AND simulation_run_id::text = ANY(:runs)"
            ),
            {"runs": run_ids},
        )
    ).all()
    for run_id, value in rows:
        used[str(run_id)].add(str(value))
    if has_thread_decisions:
        rows = (
            await conn.execute(
                text(
                    "SELECT simulation_run_id, thread_id FROM thread_decisions "
                    "WHERE simulation_run_id::text = ANY(:runs)"
                ),
                {"runs": run_ids},
            )
        ).all()
        for run_id, value in rows:
            used[str(run_id)].add(str(value))
    return used


# --------------------------------------------------------------------------- #
# Reporting.
# --------------------------------------------------------------------------- #

def _fmt_value(value: Any) -> str:
    if isinstance(value, str) and len(value) > 80:
        return f"{value[:77]!r}… ({len(value)} chars)"
    return repr(value)


def print_group(group: DuplicateGroup, index: int, total: int, out) -> None:
    print(
        f"\n[{index}/{total}] run {group.run_id}  message_ts {group.message_ts!r}  "
        f"{group.row_count} rows  {group.kind.upper()}",
        file=out,
    )
    print(
        f"        inbound references: {group.thread_reply_count} thread repl(y/ies), "
        f"{group.thread_decision_count} thread_decisions row(s)",
        file=out,
    )
    for row in group.rows:
        marker = {ACTION_KEEP: "KEEP    ", ACTION_RENUMBER: "RENUMBER", ACTION_DELETE: "DELETE  "}[
            row.action
        ]
        print(f"    {marker} id={row.row_id}", file=out)
        origin = row.origin or Origin("unclassified", "not classified")
        print(f"             origin   : {origin.verdict} — {origin.evidence}", file=out)
        print(
            f"             renumber : {row.renumber_verdict} — {row.renumber_reason}",
            file=out,
        )
        if row.new_message_ts:
            print(
                f"             new ts   : {row.message_ts} -> {row.new_message_ts}",
                file=out,
            )
        detail = "  ".join(
            f"{name}={_fmt_value(value)}"
            for name, value in sorted(row.columns.items())
            if name not in ("id", "simulation_run_id", "message_ts")
        )
        print(f"             columns  : {detail}", file=out)
    print(f"    -> {group.resolution.upper()}: {group.reason}", file=out)
    if group.resolution == RESOLUTION_NEEDS_HUMAN:
        for line in needs_human_advice(group):
            print(line, file=out)


def print_header(schema: dict[str, Any], dsn: str, strategy: str, apply: bool,
                 scheme_path: str, writer_slots: dict[int, str], out) -> None:
    print("=" * 78, file=out)
    print(f"remediate_duplicates — clear duplicate (simulation_run_id, message_ts) for {CONSTRAINT}",
          file=out)
    print("=" * 78, file=out)
    print(f"database         : {redact_dsn(dsn)}", file=out)
    print(f"alembic revision : {schema['alembic_revision']}", file=out)
    print(
        f"schema           : content={'yes' if schema['has_content'] else 'no'}  "
        f"slack_ts={'yes' if schema['has_slack_ts'] else 'no'}  "
        f"{CONSTRAINT}={'PRESENT' if schema['constraint_present'] else 'absent'}",
        file=out,
    )
    slots = ", ".join(f"{k}={v}" for k, v in sorted(writer_slots.items()))
    print(f"id scheme        : {scheme_path}", file=out)
    print(
        f"                   modulus={EXPECTED_SLOT_MODULUS} live slots [{slots}]  "
        f"remediation slot={REMEDIATION_WRITER_SLOT}",
        file=out,
    )
    print(
        f"mode             : {'APPLY (writes)' if apply else 'DRY RUN (READ ONLY transaction)'}",
        file=out,
    )
    print(f"strategy         : {strategy}", file=out)
    print(
        f"{TABLE}   : {schema['total_rows']} rows, of which "
        f"{schema['null_message_ts_rows']} have message_ts IS NULL",
        file=out,
    )
    print(
        "                   (NULL message_ts rows are exempt: the unique index is "
        "NULLS DISTINCT,\n                   so any number of them coexist. This tool "
        "never reads or writes them.)",
        file=out,
    )


def group_to_json(group: DuplicateGroup) -> dict[str, Any]:
    return {
        "simulation_run_id": group.run_id,
        "message_ts": group.message_ts,
        "row_count": group.row_count,
        "kind": group.kind,
        "resolution": group.resolution,
        "reason": group.reason,
        "resolved": group.resolved,
        "anchor_id": group.anchor_id,
        "thread_reply_count": group.thread_reply_count,
        "thread_decision_count": group.thread_decision_count,
        "rows": [
            {
                "id": row.row_id,
                "origin": row.origin.verdict if row.origin else None,
                "origin_evidence": row.origin.evidence if row.origin else None,
                "renumber": row.renumber_verdict,
                "renumber_reason": row.renumber_reason,
                "action": row.action,
                "new_message_ts": row.new_message_ts,
                "columns": {k: _json_safe(v) for k, v in row.columns.items()},
            }
            for row in group.rows
        ],
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime | uuid.UUID):
        return str(value)
    return value


# --------------------------------------------------------------------------- #
# Orchestration.
# --------------------------------------------------------------------------- #

async def remediate(dsn: str, *, apply: bool, strategy: str, as_json: bool,
                    lock_timeout_ms: int) -> int:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    modulus, writer_slots, scheme_path = load_id_scheme()
    # With --json, stdout must stay parseable, so the human report goes to stderr.
    out = sys.stderr if as_json else sys.stdout
    engine = create_async_engine(dsn, pool_pre_ping=False)
    try:
        async with engine.begin() as conn:
            if apply:
                # One transaction for read, plan, write and re-verify. The lock
                # blocks writers (INSERT/UPDATE/DELETE) but not readers, so the
                # plan cannot be computed from rows another session is changing
                # underneath us, and "no duplicates remain" is true as of commit.
                await conn.execute(text(f"SET LOCAL lock_timeout = '{int(lock_timeout_ms)}ms'"))
                await conn.execute(text(f"LOCK TABLE {TABLE} IN SHARE ROW EXCLUSIVE MODE"))
            else:
                # Belt and braces: a dry run cannot write even if this code is wrong.
                await conn.execute(text("SET TRANSACTION READ ONLY"))

            schema = await fetch_schema(conn)
            print_header(schema, dsn, strategy, apply, scheme_path, writer_slots, out)
            groups = await load_groups(conn, schema["columns"], schema["has_thread_decisions"])

            if not groups:
                print(
                    f"\nNo duplicate (simulation_run_id, message_ts) groups. "
                    f"{CONSTRAINT} can be created as-is.",
                    file=out,
                )
                payload = _envelope(schema, dsn, strategy, apply, [], None, EXIT_CLEAN)
                if as_json:
                    print(json.dumps(payload, indent=2))
                return EXIT_CLEAN

            used = await load_used_ids(
                conn, sorted({g.run_id for g in groups}), schema["has_thread_decisions"]
            )
            now_us = time.time_ns() // 1000
            print(
                f"\n{len(groups)} duplicate group(s) covering "
                f"{sum(g.row_count for g in groups)} row(s):",
                file=out,
            )
            for index, group in enumerate(groups, start=1):
                plan_group(
                    group, strategy=strategy, used=used[group.run_id], now_us=now_us,
                    has_slack_columns=schema["has_slack_ts"], writer_slots=writer_slots,
                    modulus=modulus,
                )
                print_group(group, index, len(groups), out)

            unresolved = [g for g in groups if not g.resolved]
            renumbers = [(g, r) for g in groups for r in g.rows if r.action == ACTION_RENUMBER]
            deletes = [(g, r) for g in groups for r in g.rows if r.action == ACTION_DELETE]
            print_summary(groups, unresolved, renumbers, deletes, apply, strategy, out)

            remaining = None
            if apply:
                if unresolved:
                    # Refuse the whole run rather than half-fixing the table: a
                    # partial fix still fails the migration, and it would have
                    # spent writes and a lock cycle to get there.
                    print(
                        f"\nREFUSING TO WRITE: {len(unresolved)} group(s) cannot be "
                        "resolved under this strategy (listed above). Nothing was "
                        "changed. Resolve those first — the migration will fail on "
                        "them regardless of what this tool fixes elsewhere.",
                        file=out,
                    )
                    payload = _envelope(
                        schema, dsn, strategy, apply, groups, None, EXIT_REMAIN
                    )
                    if as_json:
                        print(json.dumps(payload, indent=2))
                    return EXIT_REMAIN

                # One statement per row, keyed on the primary key. Slower than a
                # set-based UPDATE and deliberately so: every write is traceable
                # to a row this tool printed, and a row that has changed under us
                # cannot be caught by a broad predicate we no longer believe.
                for _group, row in renumbers:
                    await conn.execute(
                        text(f"UPDATE {TABLE} SET message_ts = :new WHERE id = CAST(:id AS uuid)"),
                        {"new": row.new_message_ts, "id": row.row_id},
                    )
                for _group, row in deletes:
                    await conn.execute(
                        text(f"DELETE FROM {TABLE} WHERE id = CAST(:id AS uuid)"),
                        {"id": row.row_id},
                    )
                remaining = [
                    {"simulation_run_id": str(r[0]), "message_ts": r[1], "row_count": r[2]}
                    for r in (await conn.execute(text(DUP_KEYS_SQL))).all()
                ]
                print(
                    f"\nApplied: {len(renumbers)} renumbered, {len(deletes)} deleted.",
                    file=out,
                )
                if remaining:
                    print(
                        f"POST-CHECK FAILED: {len(remaining)} duplicate group(s) still "
                        f"present: {remaining}. Rolling back.",
                        file=out,
                    )
                    raise _PostCheckFailed(remaining, schema, groups)
                print(
                    "Post-check inside the same transaction: 0 duplicate groups remain. "
                    f"{CONSTRAINT} can now be created — run `alembic upgrade head`.",
                    file=out,
                )
                code = EXIT_CLEAN
            else:
                code = EXIT_REMAIN if unresolved else EXIT_FOUND_DRY_RUN

            payload = _envelope(schema, dsn, strategy, apply, groups, remaining, code)
            if as_json:
                print(json.dumps(payload, indent=2))
            return code
    except _PostCheckFailed as failure:
        payload = _envelope(
            failure.schema, dsn, strategy, apply, failure.groups, failure.remaining, EXIT_REMAIN
        )
        if as_json:
            print(json.dumps(payload, indent=2))
        return EXIT_REMAIN
    finally:
        await engine.dispose()


class _PostCheckFailed(Exception):
    """Raised to force a rollback when duplicates survive the writes."""

    def __init__(self, remaining, schema, groups):
        super().__init__("duplicates remain after apply")
        self.remaining = remaining
        self.schema = schema
        self.groups = groups


def print_summary(groups, unresolved, renumbers, deletes, apply, strategy, out) -> None:
    counts: dict[str, int] = defaultdict(int)
    for group in groups:
        counts[group.resolution] += 1
    print("\n" + "-" * 78, file=out)
    print("SUMMARY", file=out)
    print(f"  duplicate groups           : {len(groups)}", file=out)
    print(f"  rows in those groups       : {sum(g.row_count for g in groups)}", file=out)
    for resolution in (
        RESOLUTION_DELETE, RESOLUTION_RENUMBER, RESOLUTION_NEEDS_DELETE_STRATEGY,
        RESOLUTION_NEEDS_HUMAN,
    ):
        print(f"  groups {resolution:<20}: {counts[resolution]}", file=out)
    print(f"  rows to renumber           : {len(renumbers)}", file=out)
    print(f"  rows to delete             : {len(deletes)}", file=out)
    print("-" * 78, file=out)
    if unresolved and not apply:
        needs_delete = [
            g for g in unresolved if g.resolution == RESOLUTION_NEEDS_DELETE_STRATEGY
        ]
        needs_human = [g for g in unresolved if g.resolution == RESOLUTION_NEEDS_HUMAN]
        print(
            f"\n{len(unresolved)} group(s) would REMAIN, so the migration would still "
            "fail. Nothing here is safe to automate:",
            file=out,
        )
        if needs_delete:
            print(
                f"  * {len(needs_delete)} group(s) are byte-identical copies whose ts "
                "must not change. Deleting the copies loses nothing — re-run with "
                "--strategy keep-earliest to do it.",
                file=out,
            )
        if needs_human:
            print(
                f"  * {len(needs_human)} group(s) need a human: two or more rows look "
                "Slack-born and they are not identical. See the per-group notes above.",
                file=out,
            )
    elif not apply:
        print(
            f"\nDry run. Nothing was written (the transaction was READ ONLY). Re-run "
            f"with --apply --strategy {strategy} to make these changes.",
            file=out,
        )


def _envelope(schema, dsn, strategy, apply, groups, remaining, code) -> dict[str, Any]:
    counts: dict[str, int] = defaultdict(int)
    for group in groups:
        counts[group.resolution] += 1
    return {
        "tool": "remediate_duplicates",
        "database": redact_dsn(dsn),
        "alembic_revision": schema.get("alembic_revision"),
        "schema": {
            "has_content": schema.get("has_content"),
            "has_slack_ts": schema.get("has_slack_ts"),
            "constraint_present": schema.get("constraint_present"),
            "total_rows": schema.get("total_rows"),
            "null_message_ts_rows": schema.get("null_message_ts_rows"),
        },
        "strategy": strategy,
        "apply": apply,
        "summary": {
            "duplicate_groups": len(groups),
            "rows_in_groups": sum(g.row_count for g in groups),
            "by_resolution": dict(counts),
            "rows_to_renumber": sum(
                1 for g in groups for r in g.rows if r.action == ACTION_RENUMBER
            ),
            "rows_to_delete": sum(
                1 for g in groups for r in g.rows if r.action == ACTION_DELETE
            ),
            "unresolved_groups": sum(1 for g in groups if not g.resolved),
        },
        "duplicate_groups": [group_to_json(g) for g in groups],
        "remaining_after_apply": remaining,
        "exit_code": code,
    }


class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on a usage error; 2 already means "duplicates found"."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="remediate_duplicates",
        description=(
            "Clear duplicate (simulation_run_id, message_ts) rows in agent_messages "
            "so migration 0019 can create uq_agent_messages_run_ts. Dry run unless "
            "--apply is given."
        ),
        epilog=(
            "exit codes: 0 clean / applied, 1 duplicates remain or would remain, "
            "2 duplicates found in a dry run and all resolvable, 3 operational "
            "failure, 64 usage error."
        ),
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Postgres DSN. Defaults to $DATABASE_URL.",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually write. Without this the tool only reports (READ ONLY transaction).",
    )
    parser.add_argument(
        "--strategy", choices=STRATEGIES, default=STRATEGY_RENUMBER,
        help=(
            "renumber (default, non-destructive): never delete a row; give the "
            "duplicates new locally-minted ids where that is safe. "
            "keep-earliest / keep-latest (destructive, opt-in): additionally DELETE "
            "the redundant copies of byte-identical groups, keeping the oldest / "
            "newest row. Divergent groups are still renumbered, never deleted, "
            "under every strategy."
        ),
    )
    parser.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit the machine-readable report on stdout (human report moves to stderr).",
    )
    parser.add_argument(
        "--lock-timeout-ms", type=int, default=int(os.environ.get("REMEDIATE_LOCK_TIMEOUT_MS", 10000)),
        help=(
            "How long --apply waits for the table lock before giving up (default "
            "10000, matching ALEMBIC_LOCK_TIMEOUT_MS). 0 waits forever."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dsn = args.database_url or os.environ.get("DATABASE_URL")
    if not dsn:
        print(
            "ERROR: no database URL. Pass --database-url or set DATABASE_URL.",
            file=sys.stderr,
        )
        return EXIT_OPERATIONAL
    try:
        return asyncio.run(
            remediate(
                normalise_dsn(dsn), apply=args.apply, strategy=args.strategy,
                as_json=args.as_json, lock_timeout_ms=args.lock_timeout_ms,
            )
        )
    except SchemeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_OPERATIONAL
    except OperationalFailure as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_OPERATIONAL
    except Exception as exc:  # noqa: BLE001 — any DB failure is operational, not a verdict
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_OPERATIONAL


if __name__ == "__main__":
    raise SystemExit(main())
