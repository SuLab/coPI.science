# Review-driven assessment contract — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: use `/engineering:plan-execution` to
> implement this plan. Its no-build, no-test-until-merge instruction overrides the
> per-task test steps below where the two conflict. Steps use checkbox (`- [ ]`)
> syntax for tracking.

**Goal:** Make BlackbirdBot's assessment brief answer the four things a human
reviewer asked for in production — a readable headline, a problem-first pitch, a
named competitive landscape with development stages, and an explicit statement of
how settled the biology is — and record the sequencing rationale behind the ask.

**Architecture:** Two additive nullable JSONB columns (`0050`) carry two new sidecar
fields; the scout_hub prompt set goes 1.6.0 → 1.7.0 to produce them and to tighten
the headline and pitch contracts; `_persist_assessment` reuses the existing
`normalize_bullets` path; the detail page renders both inside the existing signals
card, which is retitled. No rubric change, no backfill, no change to
`#assessments-summary`.

**Tech Stack:** Python 3 / SQLAlchemy 2 / Alembic / FastAPI / Jinja2 / Postgres 15 /
pytest.

**Spec:** `docs/specs/2026-09-21-review-driven-assessment-contract-design.md` — read
it before starting. It carries the evidence (four findings), the eight decisions with
rationale, and six accepted risks. This plan argues from it and does not restate it.

**Revision:** adversarially audited 2026-09-21 against the repository; 8 findings,
2 of them blocking, all folded in. What the audit changed: Task 3's
`_clip_at_sentence` test gained the function-local import every other user of that
symbol has (without it: `NameError` plus an `F821` that fails `ruff`, and the step's
own "stop and report that the spec is wrong" instruction would fire on a false
premise); Task 1's migration now writes each `sa.Column(...)` on ONE line, because
the `PLANNED_OBJECTS` drift regex has no `\s*` after `sa.Column(` and a wrapped call
makes the guard pass while checking nothing; Task 2 gained two `caplog` assertions
and a warn-but-store case, without which Step 3's loop extension could be reverted
with every test still green; Task 2's expected failure mode is corrected (the row
build is swallowed by a best-effort `except`, so it is `NoResultFound`, not
`TypeError`); three more stale comments, the `preflight.py` comment block, the
template's Jinja comment and a now-vacuous assertion at
`test_assessment_detail_page.py:1938` are named; and the file-disjoint claim is
qualified — it is true, but Tasks 2 and 4 are semantically dependent on Task 1.
The audit confirmed every prompt-edit anchor exists verbatim, that Task 3's
trap-avoidance genuinely works, and that no third-party test the plan omits would
break.

## Global Constraints

- **`prompts/` is bind-mounted and read per use; `src/` is baked into the agent
  image.** Any change under `prompts/roles/**` reaches a RUNNING agent at checkout
  time, with no build and no restart. Confirm `/admin/simulation` shows no running
  engine before touching the working tree (spec §7).
- **Never `git checkout` / `git stash` / `git restore` `docker-compose.prod.yml`.** It
  carries uncommitted, load-bearing host edits (CLAUDE.md, two-stack warning).
- **Never run `pip install` against `.venv-test` from this sshfs mount.** Run pytest
  on the host: `.venv-test/bin/python -m pytest tests/ -v`.
- **`git log` over full history can exceed 120 s on this mount.** Use path-scoped,
  `-n`-limited git commands.
- Headline bound: **110 characters**, everywhere it is stated (prose and code).
- Pitch bounds: **four to six sentences**, **900 characters** total, **sentences 1–4
  end within ~550 characters**.
- New bullet fields: **2–4 bullets, ≤200 characters each** — the existing
  `_HUB_BULLETS_MIN` / `_HUB_BULLETS_MAX` / `_HUB_BULLET_CHARS` values, unchanged.
- Prompt set version after this change: **`1.7.0`**.
- Schema head after this change: **`0050`**.
- Commit trailer on every commit:
  `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`

## File map

| File | Responsibility | Task |
| --- | --- | --- |
| `alembic/versions/0050_assessment_landscape_and_evidence_maturity.py` | the two columns | 1 |
| `src/models/opportunity.py` | maps them | 1 |
| `scripts/migrate/preflight.py` | guarded production migration path | 1 |
| `tests/unit/test_migration_checks.py` | pins preflight's structures | 1 |
| `tests/integration/test_harness_smoke.py` | pins `alembic_version` | 1 |
| `src/agent/simulation.py` | writes them; headline drift alarm | 2 |
| `src/services/assessment_headline.py` | three stale comments | 2 |
| `src/services/assessment_detail.py` | one stale docstring | 2 |
| `tests/integration/test_assessment_narrative_fields.py` | persist behaviour (extend) | 2 |
| `prompts/roles/scout_hub/phase4-thread-reply.md` | the contract | 3 |
| `prompts/roles/scout_hub/role.toml` | version stamp | 3 |
| `docs/specs/2026-08-07-hub-bot-prompts.md` | generated mirror | 3 |
| `tests/unit/test_headline_contract.py` | headline contract pins | 3 |
| `tests/unit/test_rubric_prompt_sync.py` | skeleton + bounds pins | 3 |
| `tests/unit/test_pitch_contract.py` | new, pitch contract pins | 3 |
| `tests/unit/test_assessment_headline_render.py` | clip-boundary evidence | 3 |
| `templates/admin/_assessment_detail_body.html` | renders both, retitled card | 4 |
| `tests/integration/test_assessment_detail_page.py` | staff/reviewer visibility | 4 |
| `CLAUDE.md` | the `0050` deploy box | 5 |

The five tasks are **file-disjoint** and may be implemented in parallel.

> ⚠️ **File-disjoint is not the same as independently verifiable.** Tasks 2 and 4
> are SEMANTICALLY DEPENDENT on Task 1: their tests need
> `OpportunityAssessment.competitive_landscape` / `.evidence_maturity` to be mapped
> *and* the migration applied to the test container's schema. Neither can pass
> before Task 1 merges.
>
> Under `/engineering:plan-execution` this is a non-issue — **do not run tests
> inside any task**; the single verification is the post-merge `./scripts/ci.sh` at
> the integration gate. The per-task "Step 2: run it to verify it fails" and
> "Step N: run the tests" instructions below are written for a serial TDD executor
> and are **non-normative under parallel execution**: they record the expected
> failure mode and the intended post-merge outcome, not a gate to satisfy mid-task.
> `./scripts/ci.sh` runs `pytest tests/` whole under `set -euo pipefail` and returns
> one bit, so a per-task scoped green is not obtainable from it in any case.

---

### Task 1: Migration 0050, model columns, and migration bookkeeping

**Files:**
- Create: `alembic/versions/0050_assessment_landscape_and_evidence_maturity.py`
- Modify: `src/models/opportunity.py:103-104` (add two columns after `risks`)
- Modify: `scripts/migrate/preflight.py:74`, `:135-139`, `:424-426`, `:428-432`
- Test: `tests/unit/test_migration_checks.py:231-236`
- Test: `tests/integration/test_harness_smoke.py:38-65`

**Interfaces:**
- Consumes: nothing.
- Produces: `OpportunityAssessment.competitive_landscape: Mapped[list | None]` and
  `OpportunityAssessment.evidence_maturity: Mapped[list | None]`, both
  `JSONB(none_as_null=True)`, nullable. Tasks 2 and 4 rely on exactly these names.

- [ ] **Step 1: Update the two pinning tests first (they are the failing test)**

In `tests/unit/test_migration_checks.py`, change the `DEFAULT_TARGET` assertion and
extend the tuple:

```python
def test_supported_start_revisions_are_exactly_the_documented_set():
    assert pf.SUPPORTED_START_REVISIONS == (
        "0018", "0019", "0020", "0021", "0023", "0024", "0025", "0026", "0027", "0028",
        "0029", "0030", "0031", "0032", "0033", "0034", "0035", "0036", "0037", "0038",
        "0039", "0040", "0041", "0042", "0043", "0044", "0045", "0046", "0047", "0048",
        "0049",
    )
    assert pf.DEFAULT_TARGET == "0050"
```

In `tests/integration/test_harness_smoke.py`, append to the comment block and change
the assertion:

```python
        # 0050 opportunity_assessments.competitive_landscape/.evidence_maturity
        #      (the hub's named competitor set with development stages, and its
        #      per-axis statement of what is settled; sidecar items 13/14,
        #      app-only, never published to #assessments-summary)
        assert v == "0050"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_migration_checks.py -v`
Expected: FAIL — `assert '0049' == '0050'`.

(`test_harness_smoke.py` needs a database and is exercised by `./scripts/ci.sh` in
step 7; it will fail there until the migration exists.)

- [ ] **Step 3: Write the migration**

Create `alembic/versions/0050_assessment_landscape_and_evidence_maturity.py`:

```python
"""opportunity_assessments.competitive_landscape / .evidence_maturity — sidecar items 13/14.

Revision ID: 0050
Revises: 0049
Create Date: 2026-09-21

Two additive nullable JSONB columns (scout_hub prompt set 1.7.0):

* ``competitive_landscape`` — the competing and adjacent programs, each with its
  development stage, or a plain statement that a search found none.
* ``evidence_maturity`` — one bullet per axis the verdict rests on (the biology,
  and the enabling chemistry/assay/platform), each naming what IS settled and what
  is NOT.

Both are app-only, the same design as ``score_rationale`` (0048) and
``strengths``/``risks`` (0049): neither is smuggled into ``elevator_pitch`` or the
``#assessments-summary`` headline, which stays exactly the six fields it already
renders.

NULL for every row written before this revision, deliberately never backfilled:
those verdicts were never asked for either field, and a generated one would be
indistinguishable from one the hub wrote. A malformed value also stores NULL —
``normalize_bullets`` in ``src/services/assessment_detail.py`` is the gate, and
``raw_verdict`` keeps whatever the hub emitted either way. Every read path renders
nothing when a column is NULL.

Deploy order: additive and nullable, so OLD code against the NEW schema is safe.
The reverse breaks in both directions. READ — the new code maps both columns, so
every ``select(OpportunityAssessment)`` raises ``UndefinedColumn`` on FOUR
surfaces: both assessment list pages, both detail pages,
``src/services/review_bot.py`` (on the worker, so every review_feedback_analysis
job fails) and ``src/routers/reviews.py`` (so feedback submit/edit 500s). WRITE —
``_persist_assessment`` names both in the INSERT, and that write is best-effort,
so every verdict of a running simulation is lost to one ERROR line in a log nobody
is tailing while the Slack replies keep looking completely normal. Build, migrate
from a one-off container, then start — the same ordering as
0037/0040/0041/0043/0048/0049 (see CLAUDE.md).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0050"
down_revision: Union[str, None] = "0049"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "opportunity_assessments",
        sa.Column("competitive_landscape", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "opportunity_assessments",
        sa.Column("evidence_maturity", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("opportunity_assessments", "evidence_maturity")
    op.drop_column("opportunity_assessments", "competitive_landscape")
```

> ⚠️ **Each `sa.Column(...)` must stay on ONE line**, exactly as
> `alembic/versions/0049_assessment_strengths_risks.py:49` and `:53` write it. The
> `PLANNED_OBJECTS` drift guard matches
> `add_column\(\s*\n?\s*"[^"]+",\s*\n?\s*sa\.Column\("([^"]+)"`
> (`tests/unit/test_migration_checks.py:893`) — there is **no** `\s*` after
> `sa\.Column\(`. Wrapping the arguments onto their own lines makes the guard find
> zero columns for `0050`, and since it only asserts `found - declared` is empty,
> it would report success while checking nothing. That is the exact failure its own
> docstring records at `:875-879` ("four revisions' worth of new columns … had no
> collision entry, and nothing failed"). Both lines measure 99 and 95 characters
> against `line-length = 100` (`pyproject.toml:64`), so they fit.

- [ ] **Step 4: Map the columns**

In `src/models/opportunity.py`, immediately after the `risks` column at line 104:

```python
    #: Sidecar items 13/14 (scout_hub >= 1.7.0). `competitive_landscape` names the
    #: competing and adjacent programs WITH their development stages, or says a
    #: search found none; `evidence_maturity` states, per axis the verdict rests
    #: on, what is settled and what is not. Both exist because the panel already
    #: produced this material and the brief lost it to compression — see
    #: docs/specs/2026-09-21-review-driven-assessment-contract-design.md §1.1 F3.
    #: NULL on every pre-0050 row, deliberately never backfilled.
    competitive_landscape: Mapped[list | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    evidence_maturity: Mapped[list | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
```

- [ ] **Step 5: Update `preflight.py` — four sites**

Line 74:

```python
DEFAULT_TARGET = "0050"
```

The comment block at `:91-134` is the prose the pinning test calls "the documented
set", and it currently ends "…and 0048 joins now as DEFAULT_TARGET moves to 0049."
Adding `"0049"` to the tuple makes that sentence false, and no test reads the
comment. Append, in the sentence form already there:

```
#: 0049 joins now as DEFAULT_TARGET moves to 0050.
```

`SUPPORTED_START_REVISIONS` (line 135) — append `"0049"`:

```python
SUPPORTED_START_REVISIONS = (
    "0018", "0019", "0020", "0021", "0023", "0024", "0025", "0026", "0027", "0028", "0029",
    "0030", "0031", "0032", "0033", "0034", "0035", "0036", "0037", "0038", "0039", "0040",
    "0041", "0042", "0043", "0044", "0045", "0046", "0047", "0048", "0049",
)
```

`PLANNED_OBJECTS` — after the two `0049` entries, before the closing `)`:

```python
    # 0050_assessment_landscape_and_evidence_maturity
    PlannedObject("0050", "column", "competitive_landscape", "opportunity_assessments"),
    PlannedObject("0050", "column", "evidence_maturity", "opportunity_assessments"),
```

`REVISION_ORDER` — append `"0050"`:

```python
REVISION_ORDER = (
    "0018", "0019", "0020", "0021", "0022", "0023", "0024", "0025", "0026", "0027", "0028",
    "0029", "0030", "0031", "0032", "0033", "0034", "0035", "0036", "0037", "0038", "0039",
    "0040", "0041", "0042", "0043", "0044", "0045", "0046", "0047", "0048", "0049", "0050",
)
```

- [ ] **Step 6: Run the unit tests**

Run: `.venv-test/bin/python -m pytest tests/unit/test_migration_checks.py -v`
Expected: PASS, including `test_every_post_branch_revision_is_a_supported_start`,
`REVISION_ORDER[-1] == DEFAULT_TARGET`, and the `PLANNED_OBJECTS` re-derivation that
reads the migration files.

- [ ] **Step 7: Run the alembic round trip and the smoke test**

Run: `./scripts/ci.sh`
Expected: single head, no duplicate revision ids, upgrade→downgrade→upgrade clean,
`test_harness_smoke.py` passing at `0050`. Other tasks' tests may still fail; only
these must pass for this task.

- [ ] **Step 8: Commit**

```bash
git add alembic/versions/0050_assessment_landscape_and_evidence_maturity.py \
        src/models/opportunity.py scripts/migrate/preflight.py \
        tests/unit/test_migration_checks.py tests/integration/test_harness_smoke.py
git commit -m "feat(assessments): competitive_landscape and evidence_maturity columns (0050)"
```

---

### Task 2: Engine — persist both fields, and retune the headline drift alarm

**Files:**
- Modify: `src/agent/simulation.py:4537-4544` (constant use), `:4620` (soft-bound
  loop), `:4691-4692` (kwargs), `:9251` (`_HEADLINE_SOFT_LIMIT`), `:9258-9262`
  (`_PITCH_SOFT_LIMIT` comment)
- Modify: `src/services/assessment_headline.py:66-71`, `:105`, `:154-156` (comments only)
- Modify: `src/services/assessment_detail.py:156` (docstring only — `normalize_bullets`
  says "The hub's own ``strengths`` / ``risks`` sidecar lists" and now gates four
  fields; extend the sentence to name `competitive_landscape` and `evidence_maturity`.
  Do not change its behaviour.)
- Test: `tests/integration/test_assessment_narrative_fields.py` (extend — this is
  where the `0049` persist tests live; do NOT create a new file)

**Interfaces:**
- Consumes: `OpportunityAssessment.competitive_landscape` / `.evidence_maturity`
  from Task 1; `normalize_bullets` from `src/services/assessment_detail.py:155`
  (already imported at `simulation.py:68`).
- Produces: both columns populated from the sidecar keys of the same names.

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_assessment_narrative_fields.py`, following
`test_persist_assessment_stores_strengths_and_risks` at `:376` exactly — the module
uses a module-level `engine` fixture, builds a `SimulationEngine` stub by hand, calls
the unbound `SimulationEngine._persist_assessment(stub, agent_id, channel, verdict)`,
and cleans up with `_delete_run`. There is no `db_session` and no `pytest.mark.asyncio`
decorator in this module; match that.

```python
async def test_persist_assessment_stores_landscape_and_evidence_maturity(engine):
    """Sidecar items 13/14 (0050): valid bullet lists reach their columns."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    try:
        stub = SimulationEngine(
            agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
        )
        await SimulationEngine._persist_assessment(stub, "blackbird", "general", {
            "company_or_project": "Short label",
            "competitive_landscape": ["Program A is Phase I.", "Program B lapsed."],
            "evidence_maturity": ["Biology: settled.", "Chemistry: unverified."],
            "recommendation": "conditional",
            "scores": {},
        })

        async with factory() as db:
            row = (await db.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalars().one()
        assert row.competitive_landscape == ["Program A is Phase I.", "Program B lapsed."]
        assert row.evidence_maturity == ["Biology: settled.", "Chemistry: unverified."]
    finally:
        await _delete_run(factory, run_id)


async def test_wrong_typed_landscape_degrades_to_null_and_keeps_raw_verdict(
    engine, caplog
):
    """A20: a malformed narrative field costs the field, never the verdict.

    The caplog assertions are what pin the SOFT-BOUND LOOP (step 3). Without
    them the loop could be reverted to ("strengths", "risks") and every other
    test here would still pass, because the column values come from step 4's
    `normalize_bullets` kwargs, not from the loop.
    """
    import logging

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    try:
        stub = SimulationEngine(
            agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
        )
        with caplog.at_level(logging.WARNING):
            await SimulationEngine._persist_assessment(stub, "blackbird", "general", {
                "company_or_project": "Short label",
                "competitive_landscape": "not a list",
                "evidence_maturity": ["Biology: settled.", ""],
                "recommendation": "conditional",
                "scores": {},
            })

        async with factory() as db:
            row = (await db.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalars().one()
        assert row.competitive_landscape is None
        assert row.evidence_maturity is None
        assert row.raw_verdict["competitive_landscape"] == "not a list"
        warnings = "\n".join(
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        )
        assert "competitive_landscape was DROPPED" in warnings
        assert "evidence_maturity was DROPPED" in warnings
    finally:
        await _delete_run(factory, run_id)


async def test_a_five_bullet_evidence_maturity_still_stores_and_warns(engine, caplog):
    """The out-of-range bullet count is a WARNING, never a drop (spec §8), and
    this is the second test pinning the soft-bound loop's extension."""
    import logging

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    try:
        stub = SimulationEngine(
            agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
        )
        five = ["one", "two", "three", "four", "five"]
        with caplog.at_level(logging.WARNING):
            await SimulationEngine._persist_assessment(stub, "blackbird", "general", {
                "company_or_project": "Short label",
                "evidence_maturity": five,
                "recommendation": "conditional",
                "scores": {},
            })

        async with factory() as db:
            row = (await db.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalars().one()
        assert row.evidence_maturity == five
        warnings = "\n".join(
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        )
        assert "evidence_maturity carries 5 bullets" in warnings
    finally:
        await _delete_run(factory, run_id)


async def test_a_120_character_headline_warns_against_the_110_bound(engine, caplog):
    """The prose bound (scout_hub 1.7.0) and `_HEADLINE_SOFT_LIMIT` must not part
    company: at 140 the alarm was silent for exactly the headlines the 110 bound
    exists to catch."""
    import logging

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    try:
        stub = SimulationEngine(
            agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
        )
        with caplog.at_level(logging.WARNING):
            await SimulationEngine._persist_assessment(stub, "blackbird", "general", {
                "company_or_project": "Short label",
                "headline": "A" * 120,
                "recommendation": "conditional",
                "scores": {},
            })
        warnings = "\n".join(
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        )
        assert "contract asks for <=110" in warnings
    finally:
        await _delete_run(factory, run_id)
```

- [ ] **Step 2: Run them to verify they fail**

Run:
```
.venv-test/bin/python -m pytest tests/integration/test_assessment_narrative_fields.py \
  -k "landscape or evidence_maturity or 120_character" -v
```
Expected: FAIL — `sqlalchemy.exc.NoResultFound` from `.scalars().one()`, preceded by
a `Failed to persist assessment on first attempt` ERROR line, and no `<=110` warning.
**Not** a visible `TypeError`: `_persist_assessment` builds the row inside a
best-effort `except Exception` (`src/agent/simulation.py:4768-4796`, commented
"never lose a posted assessment"), which swallows the bad-keyword error, queues the
kwargs and returns. The row simply never lands.

- [ ] **Step 3: Extend the soft-bound loop**

At `src/agent/simulation.py:4620`, change the tuple and the comment above it:

```python
        # Sidecar items 11/12 (0049) and 13/14 (0050): strengths, risks,
        # competitive_landscape, evidence_maturity. Same soft-bound policy as
        # key_points above — a shape violation is warned about, never a drop by
        # itself. `normalize_bullets` is the only thing that drops the whole
        # field, and only for a genuine type violation (A20).
        for _field_name in (
            "strengths", "risks", "competitive_landscape", "evidence_maturity",
        ):
```

The loop body needs no change: it already interpolates `_field_name` into every
warning.

- [ ] **Step 4: Add the two kwargs**

At `src/agent/simulation.py:4692`, immediately after `risks=...`:

```python
            # Sidecar items 13/14 (0050): the competitor set with stages, and the
            # per-axis statement of what is settled. Degrade to None on a wrong
            # type like their narrative siblings above; raw_verdict keeps the
            # original either way.
            competitive_landscape=normalize_bullets(
                verdict.get("competitive_landscape")
            ),
            evidence_maturity=normalize_bullets(verdict.get("evidence_maturity")),
```

- [ ] **Step 5: Retune the headline drift alarm**

At `src/agent/simulation.py:9251`:

```python
_HEADLINE_SOFT_LIMIT = 110
```

This is not cosmetic. It mirrors the prose bound, and the pairing is **documented**
at `tests/unit/test_rubric_prompt_sync.py:313-316` — which is a `def` plus a
docstring; nothing in the repo asserts the constant's value. The only thing that
will pin it is Task 2 Step 1's new `test_a_120_character_headline_warns_against_the_110_bound`,
which is why that test is not optional. Left at 140, a 126-character headline — the
class this whole change exists to eliminate — would store with no warning at all.
`_PROJECT_SOFT_LIMIT` (70) and `_PITCH_SOFT_LIMIT` (900) keep their numbers.

- [ ] **Step 6: Correct the two stale comments**

`src/agent/simulation.py:9258-9262` — `_PITCH_SOFT_LIMIT` currently reads "Generous
enough for the 3-4 sentences the contract asks for". Replace that clause with:

```python
#: The pitch's own soft bound. Deliberately NOT re-derived when the contract went
#: to four-to-six sentences (scout_hub 1.7.0) — at six sentences 900 is a
#: tightening, not a generous fit, and that is the intent.
```

`src/services/assessment_headline.py:66-71` — `PITCH_DISPLAY_CHARS` currently reads
"Generous enough for the 3-5 sentences the contract asks for". Replace that clause
with:

```python
# contract asks for four to six sentences (scout_hub 1.7.0) and requires sentences
# 1-4 to END within ~550 characters, so the citation sentence completes inside this
# window; see _clip_at_sentence below, which publishes only COMPLETE sentences.
```

**Two more sites say the citation is in sentence TWO, and the reorder makes that
false.** Both are the stated *justification* for live behaviour — the unconditional
`" …"` marker — so leaving them stale misrepresents why the code does what it does:

- `src/services/assessment_headline.py:105` — "the pitch contract requires a
  citation in sentence two, so that boundary can be an abbreviation".
- `src/services/assessment_headline.py:154-156` — the same claim, restated at the
  code.

In both, replace "a citation in sentence two" with "a citation sentence (sentence
four under scout_hub >= 1.7.0)". The reasoning is unchanged and still correct: the
chosen boundary can still be an abbreviation, so the marker stays unconditional.

- [ ] **Step 7: Run the tests**

Run: `.venv-test/bin/python -m pytest tests/integration/test_assessment_narrative_fields.py -v`
Expected: all PASS, new and pre-existing. In particular
`test_an_overlong_headline_or_project_label_warns_but_still_stores` (`:220`) uses a
300-character headline, which exceeds both the old and the new bound, so step 5 does
not change its outcome. If it fails, step 5 hit the wrong constant.

- [ ] **Step 8: Commit**

```bash
git add src/agent/simulation.py src/services/assessment_headline.py \
        src/services/assessment_detail.py \
        tests/integration/test_assessment_narrative_fields.py
git commit -m "feat(assessments): persist competitive_landscape and evidence_maturity; headline alarm to 110"
```

---

### Task 3: Prompt contract — scout_hub 1.6.0 → 1.7.0

**Files:**
- Modify: `prompts/roles/scout_hub/phase4-thread-reply.md` (items 5, 6, 7, 8; new
  items 13, 14; the bare-`~` list; the `<assessment_json>` skeleton)
- Modify: `prompts/roles/scout_hub/role.toml` (version)
- Modify: `docs/specs/2026-08-07-hub-bot-prompts.md` (generated — do not hand-edit)
- Test: `tests/unit/test_headline_contract.py`, `tests/unit/test_rubric_prompt_sync.py`,
  `tests/unit/test_pitch_contract.py` (create),
  `tests/unit/test_assessment_headline_render.py` (new test, plus the docstring at
  `:261`, which also claims the citation is in sentence two)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: sidecar keys `competitive_landscape` and `evidence_maturity`, both
  arrays of strings — the exact names Task 2's `verdict.get(...)` calls read.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_headline_contract.py`, replace `test_the_headline_cap_is_unchanged`
and add two:

```python
def test_the_headline_cap_is_110():
    assert "at most 110 characters" in _item_six()


def test_the_headline_item_bans_chained_relative_clauses():
    item = _item_six_flat()
    assert "one embedded relative clause" in item


def test_the_rejected_headline_is_no_longer_an_example():
    """The 1.6.0 Write: example a human reviewer rejected (126 chars, two
    chained relative clauses). Item 6 must not hold it up as a model, and must
    not quote it in the note explaining why it was replaced — a verbatim quote
    would put the rejected string back inside _item_six()."""
    assert "builds up to toxic levels in children" not in _item_six()
```

Change the version pin:

```python
def test_the_prompt_set_version_was_bumped():
    toml = (PROMPT.parent / "role.toml").read_text()
    assert 'version = "1.7.0"' in toml
```

In `tests/unit/test_rubric_prompt_sync.py`, update the whole-text bound assertion and
its docstring:

```python
def test_phase4_bounds_the_headline_and_the_project_label():
    """The two length bounds are stated as numbers in the prompt and mirrored
    by ``_HEADLINE_SOFT_LIMIT`` / ``_PROJECT_SOFT_LIMIT`` on the write path, so
    the prose and the drift alarms cannot part company. The headline bound went
    140 -> 110 with scout_hub 1.7.0; `_HEADLINE_SOFT_LIMIT` moved with it."""
    body = _norm(_phase4_text())
    assert "at most 110 characters" in body, (
        "phase4-thread-reply.md no longer bounds the headline at 110 characters"
    )
    assert "at most 70 characters" in body, (
        "phase4-thread-reply.md no longer bounds company_or_project at 70 characters"
    )
```

and extend the skeleton key loop:

```python
    for key in (
        "headline", "key_points", "elevator_pitch", "score_rationale",
        "strengths", "risks", "competitive_landscape", "evidence_maturity",
    ):
        assert key in skeleton, f"phase4-thread-reply.md dropped {key!r}"
```

Create `tests/unit/test_pitch_contract.py`:

```python
"""The elevator pitch's ordering contract (spec 2026-09-21 §3.2).

A prompt-TEXT assertion, deliberately: the suite drives tests/fakes.py's
FakeAnthropic and never reaches a real model, so nothing here can observe what
the hub writes. What it can do is stop the requirement being deleted or
softened without a decision.
"""

from __future__ import annotations

import pathlib
import re

PROMPT = pathlib.Path("prompts/roles/scout_hub/phase4-thread-reply.md")


def _item_eight() -> str:
    text = PROMPT.read_text()
    start = text.index("8. **Elevator pitch.**")
    return text[start:text.index("9. **Project label.**", start)]


def _flat() -> str:
    return re.sub(r"\s+", " ", _item_eight()).lower()


def test_the_pitch_opens_on_the_problem_not_the_asset():
    item = _flat()
    assert "the problem" in item
    assert "not with the asset" in item


def test_the_pitch_closes_on_what_a_read_out_would_enable():
    assert "would enable" in _flat()


def test_the_pitch_sentence_count_is_four_to_six():
    assert "four to six sentences" in _flat()


def test_the_citation_budget_bounds_where_sentence_four_ENDS():
    """_clip_at_sentence publishes only COMPLETE sentences, so a budget on
    where sentence 4 begins does not keep the citation in the public excerpt."""
    item = _flat()
    assert "end within approximately 550 characters" in item
```

In `tests/unit/test_assessment_headline_render.py`, add the clip-boundary evidence
test:

```python
def test_a_late_citation_sentence_is_dropped_whole_not_clipped():
    """Evidence for the 550-character budget: _clip_at_sentence cuts at the last
    terminator INSIDE value[:600], so a citation sentence that starts before 600
    but ends after it is removed entirely, not truncated."""
    from src.services.assessment_headline import _clip_at_sentence

    s1_3 = ("A. " * 4) + "B" * 388 + ". "   # ends at ~400
    s4 = "The work builds on " + "c" * 190 + "."   # 210 chars, ends past 600
    pitch = s1_3 + s4 + " Tail sentence."
    out = _clip_at_sentence(pitch, 600)
    assert "The work builds on" not in out, (
        "a citation sentence ending past 600 must be dropped whole — this is why "
        "the contract bounds where sentence 4 ENDS, not where it begins"
    )
```

> ⚠️ **The function-local import is required, not stylistic.**
> `tests/unit/test_assessment_headline_render.py:25` imports only
> `render_assessment_headline` at module scope; all nine existing users of
> `_clip_at_sentence` import it inside the test body (`:243`, `:252`, `:265`,
> `:286`, `:301`, `:319`, `:329`, `:344`). Omitting it is a `NameError` at runtime
> **and** an `F821` under `ruff check tests/`, which `scripts/ci.sh` requires at
> zero findings.

- [ ] **Step 2: Run them to verify they fail**

Run:
```
.venv-test/bin/python -m pytest tests/unit/test_headline_contract.py \
  tests/unit/test_rubric_prompt_sync.py tests/unit/test_pitch_contract.py \
  tests/unit/test_assessment_headline_render.py -v
```
Expected: FAIL on the 110 bound, the relative-clause rule, the version pin, the
skeleton keys, and every `test_pitch_contract` case. The `_clip_at_sentence` test
should **PASS immediately** — it documents existing behaviour, and if it fails the
spec's §3.2 reasoning is wrong and you must stop and report that.

- [ ] **Step 3: Edit item 6 (headline)**

Change the opening sentence's `at most 140 characters` to `at most 110 characters`,
and element 3's `rarely fit 140 characters` to `rarely fit 110 characters`.

In the paragraph beginning `No colon-stacked noun phrases.`, insert the new rule
after the slash ban:

```
   No colon-stacked noun phrases. No slash-separated alternatives. **At most one
   embedded relative clause** — a headline that chains "that … that …" makes the
   reader hold two unresolved clauses at once, and is the commonest way a headline
   that satisfies every rule above is still hard to read. No parenthetical lab or
   institution suffix — the page already shows the lab separately. At most one
   abbreviation, spelled out on first use; a chain of gene symbols is not a
   headline. This is NOT the project label; `company_or_project` already carries
   that, and both are stored.
```

Replace the Canavan positive example. Delete these two lines:

```
       Write: "An oral drug that blocks the enzyme making a brain metabolite
       that builds up to toxic levels in children with Canavan disease."
```

and put in their place:

```
       Write: "Enzyme-blocking drug for prevention of toxic metabolite build
       up in brains of children with Canavan disease"
```

Extend the closing note. Replace:

```
   Both `Not:` examples are real headlines this prompt produced: each names a
   method and a disease somewhere inside a noun stack, and neither says in
   plain words what the thing does or who it is for.
```

with:

```
   Both `Not:` examples are real headlines this prompt produced: each names a
   method and a disease somewhere inside a noun stack, and neither says in
   plain words what the thing does or who it is for. The second positive
   example above is a human reviewer's rewrite of a headline this prompt
   produced under 1.6.0 — the one this item used to hold up as a model. It
   satisfied every rule above and was still reported as hard to read: two
   chained relative clauses at 126 characters.
```

> **Do not write the literal string `Write:` in that note, and do not quote the
> replaced headline verbatim.** `_item_six()` slices from `6. **Headline.**` to
> `7. **Key points.**`, `test_the_headline_item_carries_two_positive_examples`
> counts `Write:` occurrences in that slice and requires exactly 2, and
> `test_the_rejected_headline_is_no_longer_an_example` asserts the replaced
> sentence is absent. The wording above satisfies both; a paraphrase that
> reintroduces either string does not.

- [ ] **Step 4: Edit item 8 (elevator pitch)**

Replace the sentence-plan prose. Delete from `Sentence one names what the thing is`
through `survives in the app-only tail.` and put in its place:

```
   Write it in this order, which is how a Blackbird reviewer reads it:

   1. **The problem** — the disease, the patient population and its size, and
      what those patients get today. Open here, not with the asset: a reader
      who meets the asset name first has no context to put it in.
   2. **The solution** — what the thing is and what it does.
   3. **How it differs**, and whether it actually solves the problem sentence
      one named. If the mechanism addresses the stated liability, say so; if
      it does not, say that instead.
   4. **Where the work comes from** — the published paper, preprint or dataset
      the idea builds on, cited the way the lab's own public profile cites it
      (DOI or PubMed link), or "unpublished" plainly when there is none.
   5. **What exists today and what the money would buy.**
   6. **What a clean read-out would enable** — the sentence that says why the
      answer matters. This closes the pitch.

   Sentences 1-4 together must **end within approximately 550 characters**, so
   the citation sentence completes inside the first 600 characters that are
   posted publicly — the public excerpt is cut at the last sentence boundary
   inside that window, so a sentence that starts before it and ends after it is
   dropped whole rather than clipped. If something has to go, cut from 5, which
   survives in the app-only tail. At four sentences, elements 4 and 6 are the
   two that must survive: merge 1 with 3 and 2 with 5 before dropping either.
```

Change `Three to four sentences of plain language` to `Four to six sentences of
plain language` in the item's opening line.

- [ ] **Step 5: Edit item 7 (key points)**

In the `commercial_potential` bullet, append one clause:

```
   - `commercial_potential` — path to a product, IP, market or partner. **Not**
     the competitive landscape, which item 13 owns.
```

- [ ] **Step 6: Edit item 5 (recommended next experiment)**

Append to the end of item 5's prose, immediately before `6. **Headline.**`:

```
   Where the work has more than one package, name which package **gates** the
   others and say in one line why that order rather than the reverse — a
   reviewer who would sequence it differently must be able to see what you
   traded off.
```

- [ ] **Step 7: Add items 13 and 14**

Immediately after item 12 (`**Risks.**`) and before the `**Never write a bare `~``
paragraph:

```
13. **Competitive landscape.** Two to four bullets, each at most 200
    characters. Name the competing and adjacent programs and, for each, **its
    development stage** — clinical, filing, preclinical, abandoned — or state
    plainly that a search found none. Together the bullets must answer whether
    the field is crowded, how this compares with the clinical-stage programs
    in it when THEY were at this stage, and what expansion indications exist.
    This is your own diligence and the commercial and clinical panels' — never
    sourced from the lab agent. Record them in `competitive_landscape` as an
    array of strings. **Staff-only: like the score rationale, this field is
    never posted to Slack**, so it may cite what your diligence found — but it
    is still bound by the confidentiality rule above. Never state a number for
    the weighted score or the band.
14. **Evidence maturity.** Two to four bullets, each at most 200 characters,
    one per axis the verdict rests on — the biology, and the enabling
    chemistry, assay or platform. Each names what IS settled and what is NOT,
    in those terms. State the biology axis even when the chemistry is the
    obvious risk: a reader must never have to infer how well understood the
    biology is from the absence of a complaint about it. Record them in
    `evidence_maturity` as an array of strings. **Staff-only**, same rule as
    item 13.
```

- [ ] **Step 8: Extend the bare-`~` list and the skeleton**

In the `**Never write a bare `~` in any sidecar field**` paragraph, extend the field
list to read:

```
`headline`, `key_points`, `elevator_pitch`, `score_rationale`, `strengths`,
`risks`, `competitive_landscape`, `evidence_maturity`, `rationale` or
`recommended_next_experiment`
```

In the `<assessment_json>` skeleton, immediately after `"risks": [],`:

```json
  "competitive_landscape": [],
  "evidence_maturity": [],
```

- [ ] **Step 9: Bump the prompt-set version**

`prompts/roles/scout_hub/role.toml`:

```toml
version = "1.7.0"
```

- [ ] **Step 10: Regenerate the synced doc**

Run: `.venv-test/bin/python scripts/sync_prompt_set_docs.py`
Then: `.venv-test/bin/python scripts/sync_prompt_set_docs.py --check`
Expected: the second run reports no drift. Do not hand-edit
`docs/specs/2026-08-07-hub-bot-prompts.md`.

- [ ] **Step 11: Run the tests**

Run:
```
.venv-test/bin/python -m pytest tests/unit/test_headline_contract.py \
  tests/unit/test_rubric_prompt_sync.py tests/unit/test_pitch_contract.py \
  tests/unit/test_assessment_headline_render.py tests/unit/test_doc_prompt_sync.py \
  tests/unit/test_roles.py -v
```
Expected: all PASS. `test_roles.py`'s visible-body test slices before
`**Emit the sidecar as bare JSON`, so items 5–14 fall outside it and it is
unaffected; if it fails, an edit landed in the wrong place.

- [ ] **Step 12: Commit**

```bash
git add prompts/roles/scout_hub/phase4-thread-reply.md \
        prompts/roles/scout_hub/role.toml \
        docs/specs/2026-08-07-hub-bot-prompts.md \
        tests/unit/test_headline_contract.py tests/unit/test_rubric_prompt_sync.py \
        tests/unit/test_pitch_contract.py tests/unit/test_assessment_headline_render.py
git commit -m "feat(prompts): scout_hub 1.7.0 — 110-char headline, problem-first pitch, landscape and evidence maturity"
```

---

### Task 4: Read path — render both fields in a retitled signals card

**Files:**
- Modify: `templates/admin/_assessment_detail_body.html:55` (nav), `:216-217`
  (context sets), `:271` (`<h2>`), after `:342` (new sections), `:360-375`
  (provenance)
- Test: `tests/integration/test_assessment_detail_page.py`

**Interfaces:**
- Consumes: `a.competitive_landscape`, `a.evidence_maturity` (Task 1), and the
  existing `viewer_is_staff` context key (`src/services/assessment_detail.py:1321`,
  supplied by `src/routers/admin.py:909` and `src/routers/manager.py:673`).
- Produces: nothing other tasks consume.

No service-layer change is needed. `build_assessment_detail` returns the ORM instance
and both routers use whole-entity `select(OpportunityAssessment)`; there is no
`load_only` anywhere in `src/`, so a newly mapped column reaches the template for
free.

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_assessment_detail_page.py`, mirroring
`test_a_reviewer_never_sees_the_hubs_own_bullets` at `:2274` exactly — that module
uses the `client`, `db_session`, `admin` and `manager` fixtures, a module-level
`_seed(db_session)` helper returning `(_, assessment)`, `_main(...)` to strip chrome,
`_signals_card(...)` to isolate the card, and `auth_headers(user.id)`.

Add two module-level constants beside the existing `HUB_STRENGTH_ONE` /
`HUB_RISK_ONE`:

```python
HUB_LANDSCAPE_ONE = "Myrtelle rAAV-Olig001-ASPA is clinical-stage AAV for Canavan."
HUB_MATURITY_ONE = "Biology: the genetic lesion is unambiguous; therapy is not shown."
```

```python
async def test_staff_see_the_landscape_and_maturity_sections(client, db_session, admin):
    """Sidecar items 13/14 (0050) render as their own sections in the card."""
    _, assessment = await _seed(db_session)
    assessment.competitive_landscape = [HUB_LANDSCAPE_ONE]
    assessment.evidence_maturity = [HUB_MATURITY_ONE]
    await db_session.flush()
    card = _signals_card(_main((await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text))
    assert "Competitive landscape" in card
    assert "Evidence maturity" in card
    assert HUB_LANDSCAPE_ONE in card
    assert HUB_MATURITY_ONE in card


async def test_a_reviewer_never_sees_the_landscape_or_maturity(client, db_session):
    """Same staff-only promise the prompt makes for strengths/risks: a reviewer
    reaches the manager route but is not staff, so both fields are withheld
    while the derived half of the card still renders."""
    from src.models.user import USER_ROLE_REVIEWER

    _, assessment = await _seed(db_session)
    assessment.competitive_landscape = [HUB_LANDSCAPE_ONE]
    assessment.evidence_maturity = [HUB_MATURITY_ONE]
    await db_session.flush()
    reviewer = await factories.make_user(
        db_session, user_role=USER_ROLE_REVIEWER, email="landscape-reviewer@example.org"
    )
    body = _main((await client.get(
        f"/manager/assessments/{assessment.id}", headers=auth_headers(reviewer.id)
    )).text)
    assert 'id="signals"' in body
    assert HUB_LANDSCAPE_ONE not in body
    assert HUB_MATURITY_ONE not in body


async def test_the_signals_card_is_titled_evidence_summary(client, db_session, admin):
    """The card now holds five sections, and the jump nav is the page's only
    navigation — a title naming two of five makes the rest unreachable."""
    _, assessment = await _seed(db_session)
    body = _main((await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text)
    assert "Evidence summary" in body
    assert "Strengths and risks" not in body
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/integration/test_assessment_detail_page.py -k "landscape or maturity or evidence_summary" -v`
Expected: FAIL — the strings are absent and `Strengths and risks` is still present.

- [ ] **Step 3: Add the two context sets**

After line 217, matching the guard shape of the two above it:

```jinja
{# Sidecar items 13/14 (0050). Staff-only on the page for the same reason as the
   0049 bullets above: the prompt promises the model these are never seen outside
   staff, and a reviewer account reaches the manager route. A column is raw JSONB,
   so anything that is not a real list renders as nothing rather than as one
   bullet per character or per key. #}
{% set hub_landscape = a.competitive_landscape if (viewer_is_staff and a.competitive_landscape is iterable and a.competitive_landscape is not string and a.competitive_landscape is not mapping) else [] %}
{% set hub_maturity = a.evidence_maturity if (viewer_is_staff and a.evidence_maturity is iterable and a.evidence_maturity is not string and a.evidence_maturity is not mapping) else [] %}
```

- [ ] **Step 4: Retitle the card and its nav entry**

Line 55:

```jinja
    <a class="text-indigo-700 underline-offset-2 hover:underline" href="#signals">Evidence</a>
```

Line 271:

```jinja
    <h2 class="text-base font-semibold text-gray-900 mb-1">Evidence summary</h2>
```

The card now holds five sections — Strengths, Risks, Not established, Competitive
landscape, Evidence maturity — and the page's only navigation is this nav, so a
title naming two of five would make the new sections unreachable.

Two more stale artifacts go with the retitle:

- `templates/admin/_assessment_detail_body.html:193` — the section's own Jinja
  comment is still headed "Strengths and risks". It is inside `{# … #}` so it never
  renders and no test sees it, but leaving it makes the file's own map wrong.
- `tests/integration/test_assessment_detail_page.py:1938` — an existing
  `assert "Strengths and risks" not in inside` becomes **vacuously true** once the
  string exists nowhere in the template. Retarget it to
  `assert "Evidence summary" not in inside` so it keeps testing what it was written
  to test.

- [ ] **Step 5: Add the two sections**

After the risks `</section>` (line 342) and before the `assessment-signals-unestablished`
section, so the two new blocks sit between Risks and Not established:

```jinja
        {% if hub_landscape %}
        <section class="assessment-signals-landscape rounded-lg border border-blue-200 bg-blue-50 px-4 py-3">
            <div class="text-sm font-semibold text-blue-900">Competitive landscape</div>
            <ul class="signal-hub-words mt-1 text-base leading-relaxed text-gray-700 list-disc list-outside pl-5">
                {% for bullet in hub_landscape %}<li>{{ plain_citations(bullet) }}</li>{% endfor %}
            </ul>
        </section>
        {% endif %}
        {% if hub_maturity %}
        <section class="assessment-signals-maturity rounded-lg border border-amber-200 bg-amber-50 px-4 py-3">
            <div class="text-sm font-semibold text-amber-900">Evidence maturity</div>
            <ul class="signal-hub-words mt-1 text-base leading-relaxed text-gray-700 list-disc list-outside pl-5">
                {% for bullet in hub_maturity %}<li>{{ plain_citations(bullet) }}</li>{% endfor %}
            </ul>
        </section>
        {% endif %}
```

`plain_citations(...)` is required, not optional: lines `:298` and `:323` use it for
the `0049` bullets, and a bare `{{ bullet }}` would render a URL as plain text four
lines below one rendered as a blue "cited paper" link.

- [ ] **Step 6: Extend the provenance sentence**

In the paragraph at `:367`, change the staff-path clause to:

```jinja
        {% if hub_strengths or hub_risks or hub_landscape or hub_maturity %}
        "In the hub's words", the competitive landscape and the evidence maturity are BlackbirdBot's own text from the verdict sidecar; derived rows quote the specialists' stored text.
        {% else %}
```

- [ ] **Step 7: Run the tests**

Run: `.venv-test/bin/python -m pytest tests/integration/test_assessment_detail_page.py -v`
Expected: all PASS, including the pre-existing `'id="signals"' in body` assertion,
which is unaffected by the retitle.

- [ ] **Step 8: Commit**

```bash
git add templates/admin/_assessment_detail_body.html \
        tests/integration/test_assessment_detail_page.py
git commit -m "feat(assessments): render landscape and evidence maturity; retitle the card Evidence summary"
```

---

### Task 5: CLAUDE.md deploy box for 0050

**Files:**
- Modify: `CLAUDE.md` (insert after the `0049` box, before the 2026-09-21 no-migration box)

**Interfaces:** none.

- [ ] **Step 1: Write the box**

Insert immediately after the `0049_assessment_strengths_risks` box:

```markdown
> **Deploy order for `0050_assessment_landscape_and_evidence_maturity` — migrate
> BEFORE the new code serves, and rebuild the AGENT image in the same deploy.**
> `0050` is two additive nullable JSONB columns
> (`opportunity_assessments.competitive_landscape` / `.evidence_maturity`, sidecar
> items 13/14 of scout_hub prompt set 1.7.0), so *old code against the new schema*
> is safe. The reverse breaks on FOUR read surfaces, not two, plus the write path.
> READ: the new code maps both columns, so every `select(OpportunityAssessment)`
> raises `UndefinedColumn` — both assessment list pages, both detail pages,
> `src/services/review_bot.py` (on the **worker**, so every
> `review_feedback_analysis` job fails) and `src/routers/reviews.py` (so feedback
> submit/edit 500s out of the handler). WRITE: `_persist_assessment` names both in
> the INSERT, and that write is best-effort, so **every verdict of a running
> simulation is lost** to one ERROR line in a log nobody is tailing while the Slack
> replies keep looking normal. Same shape as the `0043`/`0048`/`0049` boxes.
>
> ⚠️ **Before you touch the working tree at all, confirm `/admin/simulation` shows
> no running engine.** `prompts/` is the host working tree and `Agent._load_prompt`
> → `_load_file` does a `read_text()` **per use**, so checking out 1.7.0 reaches a
> live agent immediately — before the build, before the migration. And
> `prompt_set_stamp` is read once at run start, so a run in flight would
> permanently record 1.6.0 for output produced under 1.7.0.
>
>     DC="docker compose -f docker-compose.prod.yml"
>     $DC build blackbird-app worker
>     $DC --profile agent build agent
>     $DC run --rm blackbird-app alembic upgrade head
>     $DC run --rm blackbird-app alembic current      # must equal `alembic heads` (0050)
>     $DC up -d blackbird-app worker
>     $DC up -d agent                                 # supervisor returns IDLE
>
> **The agent rebuild is required, and the hazardous half of the pairing is
> prompt-without-image.** `prompts/` is bind-mounted and `src/` is baked. This
> deploy bumps the scout_hub prompt set to **1.7.0**, which cuts the headline bound
> 140 → 110, reorders the elevator pitch to open on the problem, and adds the
> `competitive_landscape` and `evidence_maturity` keys. Prompt-without-image writes
> NULL into both columns forever — the old parser does not know the keys, so
> `normalize_bullets` is never called on them and `_persist_assessment` never
> assigns them; the hub's bullets survive only inside `raw_verdict`. It also leaves
> `_HEADLINE_SOFT_LIMIT` at 140 while the prose says 110, so the drift alarm goes
> quiet for exactly the headlines the change exists to catch.
> Image-without-prompt is benign: an old sidecar emits neither key,
> `verdict.get(...)` reads `None`, and `normalize_bullets(None)` is `None`.
>
> NULL on every pre-`0050` row and deliberately never backfilled: those verdicts
> were never asked for either field, and generated ones would be indistinguishable
> from bullets the hub actually wrote. Both are app-only —
> `#assessments-summary` is untouched and still renders only its existing six
> fields. The detail page's signals card is retitled **"Evidence summary"** (nav
> entry "Evidence") because it now holds five sections, not two.
>
> Design and evidence:
> `docs/specs/2026-09-21-review-driven-assessment-contract-design.md`.
```

- [ ] **Step 2: Verify the CLAUDE.md disclosure sync test still passes**

Run: `.venv-test/bin/python -m pytest tests/unit/test_claude_md_disclosure_sync.py -v`
Expected: PASS. That test guards the `#assessments-summary` field-list claim; this box
does not change it, but the box asserts the channel is untouched and the test is what
makes that assertion checkable.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(deploy): deploy box for 0050 and scout_hub 1.7.0"
```

---

## Final integration gate

- [ ] Run the whole suite: `./scripts/ci.sh`

Expected: alembic single head at `0050`, upgrade→downgrade→upgrade clean, `ruff check`
with zero findings on `tests/` and under the ratcheted ceiling on `src/`, and the full
pytest run above the branch-coverage floor.

- [ ] Confirm no prompt/code drift: `.venv-test/bin/python scripts/sync_prompt_set_docs.py --check`

- [ ] Confirm `docker-compose.prod.yml` is still modified-but-uncommitted:
`git status --porcelain` must still show ` M docker-compose.prod.yml`.

**Do not deploy and do not start a simulation run as part of executing this plan.**
Deployment is the operator's explicit action, following the Task 5 box.

## Out of scope (spec §9)

No rubric edit, no backfill of the 22 existing rows, no change to
`#assessments-summary`, no change to `_latest_consult_per_domain` or the collapsed
interview timeline, no change to the queue card, and the two production reviews stay
`consumed_at IS NULL` — the review bot is not run. `_assessment_fields`
(`src/services/review_bot.py:303-335`) is knowingly left blind to both new fields
(accepted risk 5); do not "fix" it here.
