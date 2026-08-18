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
