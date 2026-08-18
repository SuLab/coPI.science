"""Cohort manifest loading, validation and seed planning — pure functions.

`cohorts.json` at the repo root is the membership of record for the three
cohorts in docs/specs/2026-08-18-cohort-seeding-design.md. Parsing, validation
and the create/add diff live here rather than in `scripts/seed_cohorts.py` so
they are testable with no database and no CLI — the same split
`src/services/cohorts.py` uses for the gate itself, and for the same reason: a
documented rule and the running code must not be able to drift.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# The rule the admin create form enforces (src/routers/admin.py:1389). Kept in
# sync deliberately: a cohort name the UI would reject must not be creatable
# through the back door.
COHORT_NAME_RE = re.compile(r"^[a-z0-9-]{1,48}$")


def load_manifest(path: str | Path) -> dict[str, Any]:
    """Parse the manifest file.

    Raises ValueError naming the path on malformed JSON, because the caller is a
    CLI whose user needs to know *which* file is broken.
    """
    p = Path(path)
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"{p}: invalid JSON: {exc}") from exc


def validate_manifest(
    manifest: dict[str, Any], known_agent_ids: set[str]
) -> list[str]:
    """Return every problem with the manifest; an empty list means it is safe.

    All errors are collected rather than raising on the first, so one run tells
    the operator everything to fix.

    The unknown-agent check is the important one. `compute_gates` builds
    `members_by_cohort` from raw membership rows without filtering against the
    roster, so a membership naming a non-existent agent still lands in every
    cohort-mate's allowed-sender set and is invisible on every admin screen. A
    typo here is a phantom sender, not a no-op.
    """
    errors: list[str] = []
    cohorts = manifest.get("cohorts")
    if not isinstance(cohorts, dict) or not cohorts:
        return ["manifest has no non-empty 'cohorts' object"]

    for name, body in cohorts.items():
        if not COHORT_NAME_RE.match(name):
            errors.append(f"{name!r}: name must match {COHORT_NAME_RE.pattern}")
        if not isinstance(body, dict):
            errors.append(f"{name!r}: value must be an object")
            continue
        for field in ("description", "source"):
            value = body.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{name!r}: {field!r} must be a non-empty string")
        members = body.get("members")
        if not isinstance(members, list) or not members:
            errors.append(f"{name!r}: 'members' must be a non-empty list")
            continue
        if len(set(members)) != len(members):
            dupes = sorted({m for m in members if members.count(m) > 1})
            errors.append(f"{name!r}: duplicate members: {', '.join(dupes)}")
        unknown = sorted(set(members) - known_agent_ids)
        if unknown:
            errors.append(
                f"{name!r}: {len(unknown)} member(s) have no AgentRegistry row: "
                f"{', '.join(unknown)}"
            )
    return errors


@dataclass(frozen=True)
class SeedPlan:
    """What seeding would change. Membership pairs are (cohort_name, agent_id).

    Frozen so a caller cannot print one plan and apply another.
    """

    cohorts_to_create: tuple[str, ...]
    memberships_to_add: tuple[tuple[str, str], ...]
    extra_memberships: tuple[tuple[str, str], ...]

    @property
    def is_noop(self) -> bool:
        """True when applying without --prune would write nothing.

        Extras are deliberately excluded: they are a report, not work.
        """
        return not self.cohorts_to_create and not self.memberships_to_add


def plan_seed(
    manifest: dict[str, Any],
    existing_cohorts: set[str],
    existing_memberships: set[tuple[str, str]],
) -> SeedPlan:
    """Diff the manifest against the database. Additive by default.

    `extra_memberships` is scoped to cohorts the manifest names. The copi
    database is expected to hold only these three, but the same code must be
    safe on an instance that also runs unrelated cohorts — blackbird carries 62
    `hub-<pi>` cohorts — so a cohort the manifest does not mention is never
    reported and never touched.

    Everything is sorted so a dry-run plan is reviewable and byte-stable across
    runs.
    """
    cohorts = manifest["cohorts"]
    to_create = tuple(sorted(n for n in cohorts if n not in existing_cohorts))

    wanted = {
        (name, agent_id)
        for name, body in cohorts.items()
        for agent_id in body["members"]
    }
    to_add = tuple(sorted(wanted - existing_memberships))

    managed = set(cohorts)
    extra = tuple(sorted(
        pair for pair in existing_memberships - wanted if pair[0] in managed
    ))
    return SeedPlan(to_create, to_add, extra)


async def apply_plan(
    db: Any,
    manifest: dict[str, Any],
    plan: SeedPlan,
    *,
    actor: Any | None = None,
    prune: bool = False,
) -> None:
    """Write the plan. Flushes but does NOT commit — the caller owns the transaction.

    Every mutation is audited. blackbird's 62 cohorts were inserted by direct SQL
    and carry no `created`/`agent_added` rows, so there is no record of who added
    whom or when; that is the specific failure this function exists to avoid.

    `actor` is None for script runs. That leaves `actor_id`/`actor_email` null,
    which is honest — a cron-style seed has no human actor, and inventing one
    would attribute the change to somebody who did not make it.
    """
    from sqlalchemy import delete, select

    from src.models import Cohort, CohortMembership
    from src.models.cohort import (
        COHORT_ACTION_AGENT_ADDED,
        COHORT_ACTION_AGENT_REMOVED,
        COHORT_ACTION_CREATED,
    )
    from src.services.cohorts import record_cohort_audit_event

    ids_by_name: dict[str, Any] = {
        name: cid for cid, name in await db.execute(select(Cohort.id, Cohort.name))
    }

    for name in plan.cohorts_to_create:
        body = manifest["cohorts"][name]
        cohort = Cohort(name=name, description=body["description"])
        db.add(cohort)
        await db.flush()
        ids_by_name[name] = cohort.id
        await record_cohort_audit_event(
            db,
            action=COHORT_ACTION_CREATED,
            cohort_name=name,
            cohort_id=cohort.id,
            actor=actor,
        )

    for name, agent_id in plan.memberships_to_add:
        cohort_id = ids_by_name[name]
        db.add(CohortMembership(cohort_id=cohort_id, agent_id=agent_id))
        await record_cohort_audit_event(
            db,
            action=COHORT_ACTION_AGENT_ADDED,
            cohort_name=name,
            cohort_id=cohort_id,
            agent_id=agent_id,
            actor=actor,
        )

    if prune:
        for name, agent_id in plan.extra_memberships:
            cohort_id = ids_by_name[name]
            await db.execute(
                delete(CohortMembership).where(
                    CohortMembership.cohort_id == cohort_id,
                    CohortMembership.agent_id == agent_id,
                )
            )
            await record_cohort_audit_event(
                db,
                action=COHORT_ACTION_AGENT_REMOVED,
                cohort_name=name,
                cohort_id=cohort_id,
                agent_id=agent_id,
                actor=actor,
            )

    await db.flush()
