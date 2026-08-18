# Cohort Seeding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record the `cabo-retreat`, `schultz-reunion` and `scripps-investigators` cohorts as data, seeded from a tracked manifest by an idempotent script, and repoint `/scripps-graph`'s node selection at the new `scripps-investigators` cohort.

**Architecture:** Pure functions (`load_manifest`, `validate_manifest`, `plan_seed`) live in `src/services/cohort_seed.py` so they are testable with no database, mirroring the split `src/services/cohorts.py` already uses for the gate. `apply_plan` is the single DB-writing function. `scripts/seed_cohorts.py` is a thin argparse CLI over them. The graph fix is surgical: only node selection moves, coloring does not.

**Tech Stack:** Python 3.11, SQLAlchemy 2 async, FastAPI, Postgres 15, pytest (`asyncio_mode = "auto"`).

**Spec:** `docs/specs/2026-08-18-cohort-seeding-design.md`

## Global Constraints

- **The gate stays OFF.** Do not set `COHORT_ISOLATION_ENABLED`, do not edit `.env`, do not restart `agent-run`. `settings.cohort_isolation_enabled` remains `False` for the whole of this plan.
- **Do not change any agent's `status`.** The active roster stays at 33.
- **Manifest carries agent IDs only** — no names, ORCIDs, emails or institutions. This repo is public (`.gitignore:101-103`).
- Cohort names must match `^[a-z0-9-]{1,48}$` — the same rule `src/routers/admin.py:1389` enforces.
- Unit tests take no marker. Integration tests set `pytestmark = pytest.mark.integration`.
- Full gate is `./scripts/ci.sh` (alembic sanity, ruff on tests, pytest with a branch-coverage floor of `COV_MIN`, default 60).
- Per `CLAUDE.md`, run pytest inside the container with an explicit `TEST_DATABASE_URL` pointing at a scratch database — **never** at `copi`:
  ```bash
  docker compose exec -T -e TEST_DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_a3 \
      app python -m pytest tests/ -v
  ```

---

### Task 1: Manifest and its validator

**Files:**
- Create: `cohorts.json`
- Create: `src/services/cohort_seed.py`
- Test: `tests/unit/test_cohort_seed.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `COHORT_NAME_RE: re.Pattern`, `load_manifest(path: str | Path) -> dict[str, Any]`, `validate_manifest(manifest: dict[str, Any], known_agent_ids: set[str]) -> list[str]` (empty list means valid).

- [ ] **Step 1: Write `cohorts.json` at the repo root**

This is the complete membership of record — 34 + 77 + 37 = 148 rows over 122 distinct agents. Copy it exactly.

```json
{
  "_comment": "Cohort membership of record for the copi instance. Seeded by scripts/seed_cohorts.py. cohort_isolation_enabled is False: these memberships are RECORDED, not enforced. On the current 33-agent active roster the gate would block 0% of ordered pairs, because scripps-investigators contains every active agent. Isolation becomes meaningful (46.0% blocked) only if the 122-agent union is activated. Agent IDs only -- this repo is public; see .gitignore:101.",
  "cohorts": {
    "cabo-retreat": {
      "description": "Cabo retreat attendees, Apr 27 - May 7 2026",
      "source": "PILOT_LABS @ 0ef4741 (20 Scripps + 14 UCSF)",
      "members": [
        "badran", "briney", "capra", "craik", "echeverria", "forli", "fraser",
        "grotjahn", "kern", "lairson", "larabell", "lasker", "lippi", "maillie",
        "manglik", "millar", "miller", "minor", "mravic", "paulson", "pwu",
        "roe", "sali", "santi", "seiple", "stroud", "su", "susa", "ward",
        "wells", "williamson", "wilson", "wiseman", "zaro"
      ]
    },
    "schultz-reunion": {
      "description": "Schultz alumni pilot (Jun 1-4 2026) union reunion attendees (Jun 5-10 2026)",
      "source": "data/cohorts/newuserlist01.tsv, 02, 03, 03_retry, 04",
      "members": [
        "achatterjee", "ai", "alfonta", "bollong", "brustad", "chang",
        "chatterjee", "chen", "cherry", "chin", "ckim", "cliu", "cochran",
        "corey", "cornish", "cropp", "diercks", "ding", "dyoung", "ellman",
        "eppinger", "gan", "gildersleeve", "goto", "gray", "guo", "hogenesch",
        "hsiehwilson", "johnsson", "jwang", "koh", "lairson", "larman", "lee",
        "lemke", "liao", "lin", "liu", "luesch", "lyssiotis", "magliery",
        "mcnamara", "mehl", "mehta", "meijler", "mills", "pei", "pezacki",
        "rwang", "santoro", "scanlan", "schen", "schiller", "schultz", "shao",
        "shokat", "su", "summerer", "ting", "ulrich", "vranken", "wang",
        "watanabe", "wemmer", "winssinger", "wliu", "wurdak", "xchen", "xiao",
        "xie", "xwu", "yang", "yliu", "young", "zhang", "zhou", "zuckermann"
      ]
    },
    "scripps-investigators": {
      "description": "Scripps Research and Calibr investigators",
      "source": "users.institution ~ 'scripps|calibr', frozen 2026-08-18",
      "members": [
        "alanjary", "badran", "bollong", "briney", "chatterjee", "cravatt",
        "deniz", "diercks", "droujinine", "good", "hogenesch", "ken", "kern",
        "lairson", "lasker", "lippi", "lotz", "macrae", "maillie", "mcnamara",
        "millar", "miller", "mravic", "paulson", "petrascheck", "pwu", "racki",
        "saez", "schultz", "su", "williams", "williamson", "wilson", "wiseman",
        "wu", "yliu", "young"
      ]
    }
  }
}
```

- [ ] **Step 2: Write the failing tests**

Create `tests/unit/test_cohort_seed.py`:

```python
"""Cohort manifest loading and validation — pure, no database.

The manifest is the only thing standing between a typo and a phantom cohort
member. `src.services.cohorts.compute_gates` treats a membership row naming an
agent with no AgentRegistry row as a valid allowed sender for every one of its
cohort-mates (its docstring records this was confirmed live with 56 such rows),
so an unknown agent_id must abort the seed, not warn about it.
"""

import json

import pytest

from src.services.cohort_seed import (
    COHORT_NAME_RE,
    load_manifest,
    validate_manifest,
)

REPO_MANIFEST = "cohorts.json"


def _manifest(**overrides):
    base = {
        "cohorts": {
            "alpha": {
                "description": "d",
                "source": "s",
                "members": ["su", "wiseman"],
            }
        }
    }
    base.update(overrides)
    return base


class TestLoadManifest:
    def test_loads_the_repo_manifest(self):
        m = load_manifest(REPO_MANIFEST)
        assert set(m["cohorts"]) == {
            "cabo-retreat",
            "schultz-reunion",
            "scripps-investigators",
        }

    def test_repo_manifest_has_the_expected_shape(self):
        m = load_manifest(REPO_MANIFEST)["cohorts"]
        assert len(m["cabo-retreat"]["members"]) == 34
        assert len(m["schultz-reunion"]["members"]) == 77
        assert len(m["scripps-investigators"]["members"]) == 37
        rows = sum(len(c["members"]) for c in m.values())
        distinct = {a for c in m.values() for a in c["members"]}
        assert rows == 148
        assert len(distinct) == 122

    def test_repo_manifest_carries_no_personal_data(self):
        """This repo is public (.gitignore:101). Agent IDs only."""
        raw = load_manifest(REPO_MANIFEST)
        for cohort in raw["cohorts"].values():
            for member in cohort["members"]:
                assert member.islower(), member
                assert " " not in member, member
                assert "@" not in member, member
                assert "0000-" not in member, member

    def test_bad_json_raises_value_error_naming_the_path(self, tmp_path):
        p = tmp_path / "broken.json"
        p.write_text("{not json")
        with pytest.raises(ValueError, match="broken.json"):
            load_manifest(p)


class TestValidateManifest:
    def test_valid_manifest_has_no_errors(self):
        assert validate_manifest(_manifest(), {"su", "wiseman"}) == []

    def test_repo_manifest_name_rule_matches_the_admin_form(self):
        for name in load_manifest(REPO_MANIFEST)["cohorts"]:
            assert COHORT_NAME_RE.match(name), name

    def test_unknown_agent_id_is_an_error(self):
        errors = validate_manifest(_manifest(), {"su"})
        assert len(errors) == 1
        assert "wiseman" in errors[0]
        assert "no AgentRegistry row" in errors[0]

    def test_missing_cohorts_key(self):
        assert validate_manifest({}, set()) == [
            "manifest has no non-empty 'cohorts' object"
        ]

    def test_empty_cohorts_object(self):
        assert validate_manifest({"cohorts": {}}, set()) == [
            "manifest has no non-empty 'cohorts' object"
        ]

    def test_bad_cohort_name(self):
        m = _manifest(cohorts={"Not Valid": {"description": "d", "source": "s",
                                             "members": ["su"]}})
        errors = validate_manifest(m, {"su"})
        assert any("must match" in e for e in errors)

    def test_blank_description_is_an_error(self):
        m = _manifest(cohorts={"alpha": {"description": "  ", "source": "s",
                                         "members": ["su"]}})
        errors = validate_manifest(m, {"su"})
        assert any("'description'" in e for e in errors)

    def test_blank_source_is_an_error(self):
        m = _manifest(cohorts={"alpha": {"description": "d", "source": "",
                                         "members": ["su"]}})
        errors = validate_manifest(m, {"su"})
        assert any("'source'" in e for e in errors)

    def test_empty_member_list_is_an_error(self):
        m = _manifest(cohorts={"alpha": {"description": "d", "source": "s",
                                         "members": []}})
        errors = validate_manifest(m, set())
        assert any("non-empty list" in e for e in errors)

    def test_duplicate_members_are_an_error(self):
        m = _manifest(cohorts={"alpha": {"description": "d", "source": "s",
                                         "members": ["su", "su"]}})
        errors = validate_manifest(m, {"su"})
        assert any("duplicate members: su" in e for e in errors)

    def test_all_errors_are_reported_not_just_the_first(self):
        m = _manifest(cohorts={
            "alpha": {"description": "d", "source": "s", "members": ["ghost1"]},
            "beta": {"description": "d", "source": "s", "members": ["ghost2"]},
        })
        errors = validate_manifest(m, set())
        assert len(errors) == 2
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `docker compose exec -T -e TEST_DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_a3 app python -m pytest tests/unit/test_cohort_seed.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'src.services.cohort_seed'`

- [ ] **Step 4: Write `src/services/cohort_seed.py`**

```python
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
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `docker compose exec -T -e TEST_DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_a3 app python -m pytest tests/unit/test_cohort_seed.py -v`

Expected: PASS, 16 tests.

Note: `test_loads_the_repo_manifest` reads `cohorts.json` by relative path, so pytest must run from the repo root. The container's working directory is `/app`, which is the repo root — no change needed.

- [ ] **Step 6: Commit**

```bash
git add cohorts.json src/services/cohort_seed.py tests/unit/test_cohort_seed.py
git commit -m "feat(cohort): manifest of record for the three cohorts, plus its validator

148 membership rows over 122 agents, agent IDs only (this repo is public).
Validation collects every error rather than raising on the first, and treats an
unknown agent_id as fatal: compute_gates does not filter membership rows against
the roster, so a typo becomes a phantom allowed sender no admin screen lists."
```

---

### Task 2: The seed planner

**Files:**
- Modify: `src/services/cohort_seed.py`
- Test: `tests/unit/test_cohort_seed.py`

**Interfaces:**
- Consumes: `load_manifest`, `validate_manifest` from Task 1.
- Produces: `SeedPlan` (frozen dataclass with `cohorts_to_create: tuple[str, ...]`, `memberships_to_add: tuple[tuple[str, str], ...]`, `extra_memberships: tuple[tuple[str, str], ...]`, and property `is_noop: bool`), and `plan_seed(manifest: dict[str, Any], existing_cohorts: set[str], existing_memberships: set[tuple[str, str]]) -> SeedPlan`. Membership pairs are always `(cohort_name, agent_id)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_cohort_seed.py`:

```python
from src.services.cohort_seed import SeedPlan, plan_seed


class TestPlanSeed:
    def test_empty_database_creates_everything(self):
        plan = plan_seed(_manifest(), set(), set())
        assert plan.cohorts_to_create == ("alpha",)
        assert plan.memberships_to_add == (("alpha", "su"), ("alpha", "wiseman"))
        assert plan.extra_memberships == ()
        assert plan.is_noop is False

    def test_fully_seeded_database_is_a_noop(self):
        plan = plan_seed(
            _manifest(), {"alpha"}, {("alpha", "su"), ("alpha", "wiseman")}
        )
        assert plan.cohorts_to_create == ()
        assert plan.memberships_to_add == ()
        assert plan.is_noop is True

    def test_existing_cohort_missing_one_member(self):
        plan = plan_seed(_manifest(), {"alpha"}, {("alpha", "su")})
        assert plan.cohorts_to_create == ()
        assert plan.memberships_to_add == (("alpha", "wiseman"),)

    def test_db_only_membership_is_reported_as_extra_not_added(self):
        plan = plan_seed(
            _manifest(),
            {"alpha"},
            {("alpha", "su"), ("alpha", "wiseman"), ("alpha", "cravatt")},
        )
        assert plan.memberships_to_add == ()
        assert plan.extra_memberships == (("alpha", "cravatt"),)
        assert plan.is_noop is True  # extras alone are not work

    def test_unmanaged_cohort_is_left_entirely_alone(self):
        """A cohort the manifest does not name is none of the manifest's business."""
        plan = plan_seed(
            _manifest(),
            {"alpha", "hub-someone"},
            {("alpha", "su"), ("alpha", "wiseman"), ("hub-someone", "cravatt")},
        )
        assert plan.cohorts_to_create == ()
        assert plan.memberships_to_add == ()
        assert plan.extra_memberships == ()

    def test_results_are_sorted_and_deterministic(self):
        m = _manifest(cohorts={
            "zeta": {"description": "d", "source": "s", "members": ["wiseman", "su"]},
            "alpha": {"description": "d", "source": "s", "members": ["cravatt"]},
        })
        plan = plan_seed(m, set(), set())
        assert plan.cohorts_to_create == ("alpha", "zeta")
        assert plan.memberships_to_add == (
            ("alpha", "cravatt"), ("zeta", "su"), ("zeta", "wiseman"),
        )

    def test_plan_is_immutable(self):
        plan = plan_seed(_manifest(), set(), set())
        with pytest.raises(Exception):
            plan.cohorts_to_create = ()

    def test_repo_manifest_against_empty_db(self):
        plan = plan_seed(load_manifest(REPO_MANIFEST), set(), set())
        assert len(plan.cohorts_to_create) == 3
        assert len(plan.memberships_to_add) == 148
        assert isinstance(plan, SeedPlan)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `docker compose exec -T -e TEST_DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_a3 app python -m pytest tests/unit/test_cohort_seed.py::TestPlanSeed -v`

Expected: FAIL — `ImportError: cannot import name 'SeedPlan'`

- [ ] **Step 3: Add the planner to `src/services/cohort_seed.py`**

Add `from dataclasses import dataclass` to the imports, then append:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `docker compose exec -T -e TEST_DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_a3 app python -m pytest tests/unit/test_cohort_seed.py -v`

Expected: PASS, 24 tests.

- [ ] **Step 5: Commit**

```bash
git add src/services/cohort_seed.py tests/unit/test_cohort_seed.py
git commit -m "feat(cohort): seed planner — additive diff of manifest against DB

Extras are reported, never deleted, and only for cohorts the manifest names, so
the same script is safe on an instance carrying unrelated cohorts (blackbird has
62 hub-<pi> ones). Plans are sorted so a dry run is byte-stable."
```

---

### Task 3: Apply the plan, and the CLI

**Files:**
- Modify: `src/services/cohort_seed.py`
- Create: `scripts/seed_cohorts.py`
- Test: `tests/integration/test_cohort_seed_apply.py`

**Interfaces:**
- Consumes: `SeedPlan`, `plan_seed`, `load_manifest`, `validate_manifest` from Tasks 1-2; `record_cohort_audit_event` from `src.services.cohorts`; `COHORT_ACTION_CREATED`, `COHORT_ACTION_AGENT_ADDED`, `COHORT_ACTION_AGENT_REMOVED` from `src.models.cohort`.
- Produces: `async def apply_plan(db, manifest: dict[str, Any], plan: SeedPlan, *, actor=None, prune: bool = False) -> None`. Adds objects and audit rows to the session and flushes; **does not commit** — the caller owns the transaction.

- [ ] **Step 1: Write the failing tests**

Create `tests/integration/test_cohort_seed_apply.py`:

```python
"""Seeding against a real Postgres.

Idempotence is the property that matters: this script will be run more than once
against production, and the second run must be a no-op rather than a duplicate-key
crash or a silent double-insert. The audit assertions exist because blackbird's 62
cohorts were seeded by direct SQL and have no `created`/`agent_added` rows at all —
that is the failure this task is written to avoid.
"""

import pytest
from sqlalchemy import func, select

from src.models import AgentRegistry, Cohort, CohortAuditEvent, CohortMembership
from src.services.cohort_seed import apply_plan, plan_seed
from src.services.cohorts import compute_gates
from tests import factories

pytestmark = pytest.mark.integration

MANIFEST = {
    "cohorts": {
        "alpha": {"description": "A", "source": "src-a", "members": ["su", "wiseman"]},
        "beta": {"description": "B", "source": "src-b", "members": ["wiseman", "cravatt"]},
    }
}


@pytest.fixture
async def roster(db_session):
    for aid, bot in (("su", "SuBot"), ("wiseman", "WisemanBot"), ("cravatt", "CravattBot")):
        user = await factories.make_user(db_session, email=f"{aid}@example.org")
        await factories.make_agent(
            db_session, user=user, agent_id=aid, bot_name=bot,
            pi_name=f"PI {aid}", status="active",
        )
    await db_session.flush()


async def _state(db):
    cohorts = {n for (n,) in await db.execute(select(Cohort.name))}
    rows = (await db.execute(
        select(Cohort.name, CohortMembership.agent_id)
        .join(CohortMembership, CohortMembership.cohort_id == Cohort.id)
    )).all()
    return cohorts, {(n, a) for n, a in rows}


async def _seed(db, manifest=MANIFEST, prune=False):
    cohorts, memberships = await _state(db)
    plan = plan_seed(manifest, cohorts, memberships)
    await apply_plan(db, manifest, plan, prune=prune)
    return plan


class TestApplyPlan:
    async def test_creates_cohorts_and_memberships(self, db_session, roster):
        await _seed(db_session)
        cohorts, memberships = await _state(db_session)
        assert cohorts == {"alpha", "beta"}
        assert memberships == {
            ("alpha", "su"), ("alpha", "wiseman"),
            ("beta", "wiseman"), ("beta", "cravatt"),
        }

    async def test_description_is_written(self, db_session, roster):
        await _seed(db_session)
        c = (await db_session.execute(
            select(Cohort).where(Cohort.name == "alpha")
        )).scalar_one()
        assert c.description == "A"

    async def test_second_run_is_a_noop(self, db_session, roster):
        await _seed(db_session)
        before = (await db_session.execute(
            select(func.count()).select_from(CohortMembership)
        )).scalar_one()

        plan = await _seed(db_session)

        assert plan.is_noop is True
        after = (await db_session.execute(
            select(func.count()).select_from(CohortMembership)
        )).scalar_one()
        assert after == before == 4

    async def test_agent_in_two_cohorts_gets_two_rows(self, db_session, roster):
        """wiseman is in both alpha and beta — overlap is the point, not a bug."""
        await _seed(db_session)
        rows = (await db_session.execute(
            select(func.count()).select_from(CohortMembership)
            .where(CohortMembership.agent_id == "wiseman")
        )).scalar_one()
        assert rows == 2

    async def test_writes_created_and_agent_added_audit_events(self, db_session, roster):
        await _seed(db_session)
        events = (await db_session.execute(select(CohortAuditEvent))).scalars().all()
        created = [e for e in events if e.action == "created"]
        added = [e for e in events if e.action == "agent_added"]
        assert {e.cohort_name for e in created} == {"alpha", "beta"}
        assert len(added) == 4
        assert all(e.cohort_id is not None for e in created + added)
        assert {(e.cohort_name, e.agent_id) for e in added} == {
            ("alpha", "su"), ("alpha", "wiseman"),
            ("beta", "wiseman"), ("beta", "cravatt"),
        }

    async def test_noop_run_writes_no_further_audit_events(self, db_session, roster):
        await _seed(db_session)
        before = (await db_session.execute(
            select(func.count()).select_from(CohortAuditEvent)
        )).scalar_one()
        await _seed(db_session)
        after = (await db_session.execute(
            select(func.count()).select_from(CohortAuditEvent)
        )).scalar_one()
        assert after == before

    async def test_prune_deletes_extras_and_audits_the_removal(self, db_session, roster):
        await _seed(db_session)
        c = (await db_session.execute(
            select(Cohort).where(Cohort.name == "alpha")
        )).scalar_one()
        db_session.add(CohortMembership(cohort_id=c.id, agent_id="cravatt"))
        await db_session.flush()

        plan = await _seed(db_session, prune=True)

        assert plan.extra_memberships == (("alpha", "cravatt"),)
        _, memberships = await _state(db_session)
        assert ("alpha", "cravatt") not in memberships
        removed = (await db_session.execute(
            select(CohortAuditEvent).where(CohortAuditEvent.action == "agent_removed")
        )).scalars().all()
        assert [(e.cohort_name, e.agent_id) for e in removed] == [("alpha", "cravatt")]

    async def test_without_prune_extras_survive(self, db_session, roster):
        await _seed(db_session)
        c = (await db_session.execute(
            select(Cohort).where(Cohort.name == "alpha")
        )).scalar_one()
        db_session.add(CohortMembership(cohort_id=c.id, agent_id="cravatt"))
        await db_session.flush()

        await _seed(db_session, prune=False)

        _, memberships = await _state(db_session)
        assert ("alpha", "cravatt") in memberships


class TestGateStaysInert:
    async def test_seeded_topology_gates_nothing_while_isolation_is_off(
        self, db_session, roster
    ):
        """The whole plan rests on this: memberships recorded, nothing enforced."""
        await _seed(db_session)
        _, memberships = await _state(db_session)
        agent_ids = [a for (a,) in await db_session.execute(
            select(AgentRegistry.agent_id).where(AgentRegistry.status == "active")
        )]

        gates, error = compute_gates(
            membership_rows=sorted(memberships),
            agent_ids=agent_ids,
            isolation_enabled=False,
            policy="open",
            cohort_count=2,
        )

        assert error is None
        assert set(gates) == set(agent_ids)
        assert all(g is None for g in gates.values())
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `docker compose exec -T -e TEST_DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_a3 app python -m pytest tests/integration/test_cohort_seed_apply.py -v`

Expected: FAIL — `ImportError: cannot import name 'apply_plan'`

- [ ] **Step 3: Add `apply_plan` to `src/services/cohort_seed.py`**

Append (the `record_cohort_audit_event` import is deferred inside the function to keep this module importable without the models package, matching how `simulation.py` imports the cohort models):

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `docker compose exec -T -e TEST_DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_a3 app python -m pytest tests/integration/test_cohort_seed_apply.py -v`

Expected: PASS, 10 tests.

- [ ] **Step 5: Write `scripts/seed_cohorts.py`**

```python
"""Seed the cohort membership of record from cohorts.json.

Creates the cohorts named in the manifest and adds their members. Idempotent:
running it twice is a no-op. Additive by default — a membership present in the
database but absent from the manifest is reported and kept, and only deleted if
you pass --prune.

This does NOT enable the interaction gate. `cohort_isolation_enabled` is a
separate setting, default False, read by a running `agent-run` through an
lru_cached get_settings(); flipping it needs the container recreated, not just
restarted. See docs/specs/2026-08-18-cohort-seeding-design.md §1.1 for why
enabling it against the current 33-agent roster would gate nothing.

`scripts/` is baked into the image, not bind-mounted, so a freshly added script
must be copied in before it can run:

    docker cp scripts/seed_cohorts.py copi-python-app-1:/app/scripts/
    docker cp cohorts.json copi-python-app-1:/app/
    docker compose exec -T app python scripts/seed_cohorts.py --dry-run

Drop --dry-run to apply.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import get_settings
from src.models import AgentRegistry, Cohort, CohortMembership
from src.services.cohort_seed import apply_plan, load_manifest, plan_seed, validate_manifest

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("seed_cohorts")


async def _run(manifest_path: Path, dry_run: bool, prune: bool) -> int:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        manifest = load_manifest(manifest_path)
        async with factory() as db:
            known = {aid for (aid,) in await db.execute(select(AgentRegistry.agent_id))}
            errors = validate_manifest(manifest, known)
            if errors:
                logger.error("Manifest is invalid. NOTHING was written:")
                for err in errors:
                    logger.error("  %s", err)
                return 1

            existing_cohorts = {n for (n,) in await db.execute(select(Cohort.name))}
            rows = (await db.execute(
                select(Cohort.name, CohortMembership.agent_id)
                .join(CohortMembership, CohortMembership.cohort_id == Cohort.id)
            )).all()
            existing_memberships = {(n, a) for n, a in rows}

            plan = plan_seed(manifest, existing_cohorts, existing_memberships)

            print("\n=== Seed plan ===")
            print(f"cohorts to create      : {len(plan.cohorts_to_create)}")
            for name in plan.cohorts_to_create:
                size = len(manifest["cohorts"][name]["members"])
                print(f"    + {name}  ({size} members)")
            print(f"memberships to add     : {len(plan.memberships_to_add)}")
            for name, agent_id in plan.memberships_to_add:
                print(f"    + {name}/{agent_id}")
            print(f"in DB, not in manifest : {len(plan.extra_memberships)}")
            for name, agent_id in plan.extra_memberships:
                suffix = " -> WILL DELETE" if prune else " (kept; --prune to delete)"
                print(f"    ? {name}/{agent_id}{suffix}")

            if dry_run:
                print("\n[dry-run] nothing written.")
                return 0
            if plan.is_noop and not (prune and plan.extra_memberships):
                print("\nAlready seeded; nothing to do.")
                return 0

            await apply_plan(db, manifest, plan, prune=prune)
            await db.commit()
            print(
                f"\nApplied: {len(plan.cohorts_to_create)} cohort(s) created, "
                f"{len(plan.memberships_to_add)} membership(s) added"
                + (f", {len(plan.extra_memberships)} pruned." if prune else ".")
            )
            print("The interaction gate is unchanged (isolation stays off).")
            return 0
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--manifest", default="cohorts.json", help="Manifest path (default: cohorts.json)")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan; write nothing")
    parser.add_argument("--prune", action="store_true", help="Delete memberships absent from the manifest")
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(Path(args.manifest), args.dry_run, args.prune)))


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Verify the CLI parses and refuses a bad manifest**

Run:
```bash
docker cp scripts/seed_cohorts.py copi-python-app-1:/app/scripts/
docker compose -f docker-compose.prod.yml -f docker-compose.override.yml exec -T app \
    python scripts/seed_cohorts.py --help
```
Expected: usage text listing `--manifest`, `--dry-run`, `--prune`. Do **not** run it without `--dry-run` yet; that is Task 5.

- [ ] **Step 7: Commit**

```bash
git add src/services/cohort_seed.py scripts/seed_cohorts.py tests/integration/test_cohort_seed_apply.py
git commit -m "feat(cohort): idempotent seeding with a real audit trail

apply_plan flushes but never commits, so the caller owns the transaction. Every
create/add/remove writes a cohort_audit_events row — blackbird's 62 hand-seeded
cohorts have none, which is the failure being avoided. actor stays null for
script runs rather than attributing the change to a human who did not make it.

Includes a test proving the seeded topology gates nothing while isolation is off."
```

---

### Task 4: Repoint `/scripps-graph` node selection at the cohort

**Files:**
- Modify: `src/routers/public.py:94` (comment), `:628-632` (selection), `:653-657` (coloring)
- Test: `tests/integration/test_public_graph.py`

**Interfaces:**
- Consumes: the `scripps-investigators` cohort created in Task 3.
- Produces: `SCRIPPS_COHORT_NAME: str` and `async def _scripps_agent_ids(db) -> set[str] | None` in `src/routers/public.py`. Returns `None` when the cohort is absent or empty, which is the caller's signal to fall back.

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_public_graph.py`. Use the module's existing `_get` helper — it clears `_GRAPH_CACHE`, without which a test reads the previous test's payload.

```python
class TestScrippsGraphUsesTheCohort:
    """/scripps-graph selects nodes from the scripps-investigators cohort.

    The hardcoded _SCRIPPS set it used before is stale by nine Scripps/Calibr PIs
    (bollong, chatterjee, diercks, droujinine, good, hogenesch, mcnamara, yliu,
    alanjary), every one of them invisible on the page. The cohort is now the
    roster of record; _SCRIPPS survives only as a fallback and as the Cabo-window
    institution map, which must NOT move.
    """

    async def test_cohort_member_outside_the_hardcoded_set_is_selected(
        self, client, db_session
    ):
        from src.models import Cohort, CohortMembership
        from src.routers import public as public_mod

        assert "droujinine" not in public_mod._SCRIPPS  # the premise

        a, b = await _two_agents_with_a_proposal(db_session, "droujinine", "good")
        cohort = Cohort(name="scripps-investigators", description="d")
        db_session.add(cohort)
        await db_session.flush()
        for aid in (a, b):
            db_session.add(CohortMembership(cohort_id=cohort.id, agent_id=aid))
        await db_session.commit()

        payload = _payload(await _get(client, "/scripps-graph"))

        assert {n["id"] for n in payload["nodes"]} == {"droujinine", "good"}

    async def test_falls_back_to_hardcoded_set_when_cohort_absent(
        self, client, db_session
    ):
        """The route must not break before Task 5 runs in production."""
        from src.routers import public as public_mod

        assert "cravatt" in public_mod._SCRIPPS  # the premise

        await _two_agents_with_a_proposal(db_session, "cravatt", "petrascheck")
        await db_session.commit()

        response = await _get(client, "/scripps-graph")

        assert response.status_code == 200
        assert {n["id"] for n in _payload(response)["nodes"]} == {
            "cravatt", "petrascheck"
        }

    async def test_every_node_colors_as_scripps(self, client, db_session):
        """On a Scripps-only view nothing may fall through to 'Other'."""
        from src.models import Cohort, CohortMembership

        a, b = await _two_agents_with_a_proposal(db_session, "droujinine", "good")
        cohort = Cohort(name="scripps-investigators", description="d")
        db_session.add(cohort)
        await db_session.flush()
        for aid in (a, b):
            db_session.add(CohortMembership(cohort_id=cohort.id, agent_id=aid))
        await db_session.commit()

        payload = _payload(await _get(client, "/scripps-graph"))

        assert {n["institution"] for n in payload["nodes"]} == {"Scripps"}

    async def test_cabo_graph_coloring_is_untouched_by_the_cohort(
        self, client, db_session
    ):
        """_institution_for is a historical Apr-May map. Seeding must not redraw it."""
        from src.models import Cohort, CohortMembership
        from src.routers import public as public_mod

        assert public_mod._institution_for("sali") == "UCSF"
        assert public_mod._institution_for("droujinine") == "Other"

        cohort = Cohort(name="scripps-investigators", description="d")
        db_session.add(cohort)
        await db_session.flush()
        db_session.add(CohortMembership(cohort_id=cohort.id, agent_id="droujinine"))
        await db_session.commit()

        assert public_mod._institution_for("sali") == "UCSF"
        assert public_mod._institution_for("droujinine") == "Other"
```

Add this helper next to the module's other helpers (near `_payload`, around line 132). It builds the minimum a graph edge needs: two agents, a `new_post` message inside the post boundary, and a public proposal decided inside the window.

```python
async def _two_agents_with_a_proposal(db_session, agent_a: str, agent_b: str):
    """Two agents joined by one in-window public proposal. Returns their ids.

    Dates are chosen to sit inside /scripps-graph's default window, which starts
    at CABO_WINDOW_START (2026-03-01) and is unbounded above.
    """
    from datetime import datetime, timezone

    from tests import factories

    run = await factories.make_simulation_run(db_session)
    for aid in (agent_a, agent_b):
        user = await factories.make_user(db_session, email=f"{aid}@example.org")
        await factories.make_agent(
            db_session, user=user, agent_id=aid, bot_name=f"{aid.title()}Bot",
            pi_name=f"PI {aid}", status="active",
        )
    ts = "1780000000.000100"
    await factories.make_agent_message(
        db_session, run=run, agent_id=agent_a, phase="new_post", message_ts=ts,
        created_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )
    await factories.make_thread_decision(
        db_session, run=run, agent_a=agent_a, agent_b=agent_b, thread_id=ts,
        outcome="proposal", origin_visibility="public",
        decided_at=datetime(2026, 4, 2, tzinfo=timezone.utc),
    )
    await db_session.flush()
    return agent_a, agent_b
```

`tests/factories.py` already provides every factory this helper uses: `make_simulation_run` (:89), `make_agent` (:72), `make_agent_message` (:117) and `make_thread_decision` (:137). No factory changes are needed. `make_thread_decision` already defaults `outcome="proposal"`; `origin_visibility` is not in its defaults, which is why the helper passes it explicitly.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `docker compose exec -T -e TEST_DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_a3 app python -m pytest tests/integration/test_public_graph.py::TestScrippsGraphUsesTheCohort -v`

Expected: FAIL — `test_cohort_member_outside_the_hardcoded_set_is_selected` returns no nodes, because `droujinine` is filtered out by `_SCRIPPS`.

- [ ] **Step 3: Add the cohort lookup to `src/routers/public.py`**

Insert after the `_OTHER_INST` block (around line 112):

```python
# /scripps-graph selects its nodes from this cohort, not from _SCRIPPS. See
# docs/specs/2026-08-18-cohort-seeding-design.md §5 and
# docs/plans/2026-08-18-cohort-seeding.md.
SCRIPPS_COHORT_NAME = "scripps-investigators"


async def _scripps_agent_ids(db: AsyncSession) -> set[str] | None:
    """Agent IDs in the scripps-investigators cohort, or None if there are none.

    None means "cohort absent or empty" and tells the caller to fall back to
    _SCRIPPS. Returning the empty set instead would render an empty graph, which
    looks like a data problem rather than an un-seeded database.
    """
    rows = await db.execute(
        text(
            "SELECT m.agent_id FROM cohort_memberships m "
            "JOIN cohorts c ON c.id = m.cohort_id "
            "WHERE c.name = :name"
        ),
        {"name": SCRIPPS_COHORT_NAME},
    )
    return {r.agent_id for r in rows} or None
```

- [ ] **Step 4: Change node selection and coloring in `_build_graph_payload`**

Replace the `elif scripps_only:` branch (currently `src/routers/public.py:628-632`):

```python
    elif scripps_only:
        nodes_result = await db.execute(
            text("SELECT agent_id, pi_name, bot_name FROM agents ORDER BY pi_name")
        )
        selector = await _scripps_agent_ids(db)
        if selector is None:
            logger.warning(
                "[graph] cohort %r is absent or empty; falling back to the "
                "hardcoded _SCRIPPS set, which is stale — run "
                "scripts/seed_cohorts.py",
                SCRIPPS_COHORT_NAME,
            )
            selector = _SCRIPPS
        active_rows = [r for r in nodes_result.fetchall() if r.agent_id in selector]
```

Then replace the `institution_of` selection (currently `:653-657`):

```python
    if use_profile_institution:
        inst_map = _group_institutions([row.institution for row in active_rows])
        institution_of = lambda row: inst_map[row.institution]  # noqa: E731
    elif scripps_only:
        # Every node on this view is Scripps by construction. Without this branch
        # the nine cohort members absent from _SCRIPPS would colour as "Other".
        institution_of = lambda row: "Scripps"  # noqa: E731
    else:
        institution_of = lambda row: _institution_for(row.agent_id)  # noqa: E731
```

- [ ] **Step 5: Correct the `_SCRIPPS` docstring so the two jobs cannot be re-conflated**

Replace the comment above `_SCRIPPS` (currently `src/routers/public.py:91-93`):

```python
# Institution map for the **Cabo run window** (Apr 27 - May 7 2026), used by
# _institution_for to colour /cabo-graph's legacy Scripps/UCSF/Other legend. It is
# a historical snapshot and is correct for that window — do not "refresh" it, or a
# published graph silently redraws.
#
# It is NOT the current Scripps roster: as of 2026-08-18 it omits nine
# Scripps/Calibr PIs (alanjary, bollong, chatterjee, diercks, droujinine, good,
# hogenesch, mcnamara, yliu). /scripps-graph therefore selects its nodes from the
# `scripps-investigators` cohort and only falls back here when that cohort is
# missing. See docs/specs/2026-08-18-cohort-seeding-design.md §5.
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `docker compose exec -T -e TEST_DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_a3 app python -m pytest tests/integration/test_public_graph.py -v`

Expected: PASS, including the four new tests and every pre-existing one — the Cabo, pilot and group-alumni windows must be unchanged.

- [ ] **Step 7: Run the full gate**

Run: `./scripts/ci.sh`

Expected: alembic single-head OK, ruff clean, full pytest green above the `COV_MIN` floor.

Per memory, on the prod host `ci.sh` needs `.venv-test` present and any root-owned `copi.egg-info` / `.ruff_cache` removed first, and the box has 2 cores — do not build containers while pytest runs.

- [ ] **Step 8: Commit**

```bash
git add src/routers/public.py tests/integration/test_public_graph.py
git commit -m "fix(graph): select /scripps-graph nodes from the cohort, not _SCRIPPS

_SCRIPPS did two jobs and only one was broken. Node selection (public.py:632) was
stale by nine Scripps/Calibr PIs — alanjary, bollong, chatterjee, diercks,
droujinine, good, hogenesch, mcnamara, yliu — all invisible on the page. That now
reads the scripps-investigators cohort, falling back to _SCRIPPS with a warning
when the cohort is absent so the route works before seeding.

Its other use, _institution_for's Cabo-window colouring, is a correct historical
snapshot and is deliberately untouched; a test pins that seeding cannot move it."
```

---

### Task 5: Seed production

**Files:** none — this task changes data, not code.

**Interfaces:**
- Consumes: `cohorts.json` and `scripts/seed_cohorts.py` from Tasks 1-3.
- Produces: 3 cohorts and 148 membership rows in the `copi` database.

- [ ] **Step 1: Copy the script and manifest into the running app container**

`docker-compose.prod.yml` bind-mounts only `./profiles` and `./prompts`, so new files are not visible to the container until copied or rebuilt.

```bash
docker cp scripts/seed_cohorts.py copi-python-app-1:/app/scripts/
docker cp cohorts.json copi-python-app-1:/app/
```

- [ ] **Step 2: Dry run and review the plan**

```bash
export COMPOSE_FILE=docker-compose.prod.yml:docker-compose.override.yml
docker compose exec -T app python scripts/seed_cohorts.py --dry-run
```

Expected on a first run:
```
cohorts to create      : 3
    + cabo-retreat  (34 members)
    + schultz-reunion  (77 members)
    + scripps-investigators  (37 members)
memberships to add     : 148
in DB, not in manifest : 0
[dry-run] nothing written.
```

**Stop and check before continuing.** If any agent ID fails validation the script exits 1 having written nothing — fix `cohorts.json`, re-run Task 1's tests, and start this task again. If `in DB, not in manifest` is non-zero, something else created cohorts; investigate before applying, and do **not** reach for `--prune` to make the number go away.

- [ ] **Step 3: Apply**

```bash
docker compose exec -T app python scripts/seed_cohorts.py
```

Expected: `Applied: 3 cohort(s) created, 148 membership(s) added.`

- [ ] **Step 4: Verify in the database**

```bash
docker compose exec -T postgres psql -U copi -d copi -c \
  "SELECT c.name, count(m.id) AS members FROM cohorts c
   LEFT JOIN cohort_memberships m ON m.cohort_id = c.id
   GROUP BY c.name ORDER BY c.name;"
```

Expected exactly:
```
 cabo-retreat          |  34
 schultz-reunion       |  77
 scripps-investigators |  37
```

Then confirm the audit trail exists — the thing blackbird's cohorts lack:

```bash
docker compose exec -T postgres psql -U copi -d copi -c \
  "SELECT action, count(*) FROM cohort_audit_events
   WHERE action IN ('created','agent_added') GROUP BY action;"
```

Expected: `created | 3` and `agent_added | 148`.

- [ ] **Step 5: Confirm the gate is still off**

```bash
docker compose exec -T postgres psql -U copi -d copi -c \
  "SELECT topology->>'cohort_isolation_enabled' AS enabled,
          topology->>'gate_active' AS active
   FROM cohort_audit_events WHERE topology IS NOT NULL
   ORDER BY created_at DESC LIMIT 1;"
```

The newest topology snapshot predates this task (the running `agent-run` only writes one at start and on membership change through the admin UI), so this reads `false | false`. Also confirm nothing changed for the agents:

```bash
docker compose exec -T postgres psql -U copi -d copi -c \
  "SELECT status, count(*) FROM agents GROUP BY status ORDER BY 2 DESC;"
```

Expected, unchanged: `active | 33`, `inactive | 91`, `pending | 3`.

- [ ] **Step 6: Verify the two admin pages and the fixed graph render**

Visit `/admin/cohorts` (three cohorts with member counts, gate banner reporting isolation disabled), `/admin/cohorts/topology` (the 127 x 3 matrix), and `/scripps-graph` — which should now include `droujinine`, `good`, `bollong`, `diercks`, `hogenesch`, `mcnamara` and `yliu` wherever they have in-window proposals.

- [ ] **Step 7: Record the outcome**

No commit — this task wrote data, not code. Report the four verification outputs from Steps 4-5 back to the user.

---

## Rollback

Nothing here is destructive and no code path reads the cohorts while isolation is off, so rollback is a delete:

```sql
DELETE FROM cohorts WHERE name IN
  ('cabo-retreat', 'schultz-reunion', 'scripps-investigators');
```

`cohort_memberships.cohort_id` is `ON DELETE CASCADE`, so memberships go with it. `cohort_audit_events` has **no** FK on `cohort_id` by design and survives — that is the point of the trail. `/scripps-graph` reverts to its `_SCRIPPS` fallback automatically.

## Out of scope

Enabling `cohort_isolation_enabled`, restarting `agent-run`, reactivating any agent, and the data defects in spec §2.2 (`hogenesch`'s institution conflict, the two seeded attendees with no agent row, `eppinger`'s missing token, the eight cabo members with empty institutions).
