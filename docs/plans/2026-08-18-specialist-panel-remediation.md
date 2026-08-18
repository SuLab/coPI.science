# Specialist Panel Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the specialist floor destroying verdicts, make its triggers match what they claim to match, and instrument the panel so its failures are visible without an audit.

**Architecture:** Six independently shippable phases against `src/agent/specialists.py`, `src/agent/simulation.py`, `src/agent/tools.py` and read-only admin surfaces. One additive migration. **No control-flow change in `_reply_to_thread`** and **no change to any bot prompt** — the pre-post gate that would have needed both is deferred by D6.

**Tech Stack:** Python 3.11+, SQLAlchemy 2.0 async, Alembic, FastAPI, Jinja2, pytest, ruff.

**Spec:** `docs/specs/2026-08-18-specialist-panel-remediation-design.md`

## Global Constraints

- **D6 — the prompt freeze. These files must be byte-identical before and after this work:**
  - `prompts/agent-system.md`, `prompts/phase4-thread-reply.md`, `prompts/phase5-new-post.md`, `prompts/identity.md`
  - `prompts/roles/scout_hub/agent-system.md`, `prompts/roles/scout_hub/identity.md`, `prompts/roles/scout_hub/phase4-thread-reply.md`
  - Both roles' string literals in `src/agent/thread_guidance.py`
  - The `consult_specialist` entry in `TOOL_DEFINITIONS` (`src/agent/tools.py:111-160`) — hub-facing text
  - **New hub-facing prompt text is also forbidden**, not only edits.
  - **Exempt:** `prompts/specialists/*.md` (Task 1 only, and only on proof).
- **Never run `pytest --snapshot-update`.** `tests/characterization/__snapshots__/test_agent_turn_gm.ambr` pins the `pi_lab` guidance strings; a mismatch there means a real regression.
- **Do not change `RUBRIC_WEIGHTS` or `_BAND_THRESHOLDS`** (`src/services/blackbird_rubric.py`). The 18 existing `weighted_score` values must stay comparable.
- **Run tests on the host, never in the container:** `.venv-test/bin/python -m pytest tests/ -v`. The image has no `[dev]` extra.
- **The full gate is `./scripts/ci.sh`.** It must pass before any commit that ends a task.
- **Task ordering is load-bearing: Task 6 must not land before Tasks 3–4.** Task 6 tightens the floor; Task 3 is what stops a tightened floor destroying verdicts.
- **`docker compose` must always be `-f docker-compose.prod.yml`, and never `--remove-orphans`.** A second unrelated deployment shares this host.

---

### Task 1: Phase 0 — diagnose whether `clear` is reachable (F1)

The panel returned `caution`/`blocking` on 142/142 production consults, with zero parse failures. Before rewriting eight personas on that observation, find out whether `clear` is reachable at all.

**Files:**
- Create: `scripts/diagnose_specialist_calibration.py`
- Modify (only if the diagnosis proves miscalibration): `prompts/specialists/*.md`
- Modify: `docs/specs/2026-08-18-specialist-panel-remediation-design.md` (append the finding to §5)

**Interfaces:**
- Consumes: `src.agent.specialists.parse_opinion`, `src.services.llm.generate_agent_response`
- Produces: a written finding in §5. No importable API — this is a diagnostic, not library code.

- [ ] **Step 1: Write the diagnostic script**

```python
"""Is `clear` reachable for the specialist panel, or does it only ever caution?

Production run 1787010946 returned caution/blocking on 142/142 consults with
ZERO parse failures — so this is genuine model output, not a parsing artifact.
If `clear` had a 10% base rate, P(0 in 142) is about 3e-7.

Two readings fit that evidence and they call for opposite responses:
  (a) the eight personas are miscalibrated and cannot say `clear`; fix them.
  (b) the 18 assessed ideas really were all weak; change nothing.

This script separates them by asking the SAME personas about ideas built to be
clean in the relevant domain. Throwaway: run it, record the finding, delete it
or leave it — it is not imported by anything.

Usage:  .venv-test/bin/python scripts/diagnose_specialist_calibration.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent.specialists import parse_opinion, persona_path  # noqa: E402
from src.services.llm import generate_agent_response  # noqa: E402

# Deliberately clean in the named domain. If `clear` is reachable at all, these
# are where it should appear: each one pre-empts the specific objection its
# persona is built to raise.
STRONG_CASES = [
    (
        "scientific",
        "Does the evidence support the mechanism claim?",
        "We ran vehicle and scrambled-siRNA arms in every cohort, n=24/arm "
        "powered at 80% for the 40% effect we pre-registered on OSF before "
        "unblinding. Two independent labs reproduced the rescue. The readout "
        "is decision-enabling either way: if the knockdown does not rescue, "
        "the target is wrong and we stop.",
    ),
    (
        "chemistry",
        "Is there a credible path to a development candidate?",
        "We have a 40-compound lead series off a validated crystal structure, "
        "best compound 12 nM with 400x selectivity over the two nearest family "
        "members, clean hERG at 30 uM, no structural alerts, and oral "
        "bioavailability of 55% in rat. Med-chem is tractable: three vectors "
        "on the scaffold are open.",
    ),
    (
        "commercial",
        "Is this differentiated against the current landscape?",
        "No approved agent and no competitor in registrational trials for this "
        "indication. The two prior programs (Tango, CUE-401) were discontinued "
        "for a liability our chemotype does not share. Two comparable deals "
        "closed above $200M upfront in the last 18 months.",
    ),
]


async def ask(domain: str, question: str, context: str) -> str:
    persona = persona_path(domain).read_text(encoding="utf-8")
    raw = await generate_agent_response(
        system_prompt=persona,
        messages=[{
            "role": "user",
            "content": f"## Question from the hub\n\n{question}\n\n"
                       f"## What the PI has said\n\n{context}",
        }],
        max_tokens=900,
        log_meta={"agent_id": "diagnostic", "phase": f"consult_{domain}"},
    )
    return raw


async def main() -> None:
    print("=== STRONG cases: `clear` SHOULD be reachable here ===")
    signals = []
    for domain, question, context in STRONG_CASES:
        raw = await ask(domain, question, context)
        opinion = parse_opinion(raw, domain=domain)
        signals.append(opinion.verdict_signal)
        print(f"  {domain:<12} -> {opinion.verdict_signal:<9} ({opinion.confidence})")
        for concern in opinion.concerns[:2]:
            print(f"       concern: {concern[:100]}")

    print()
    if "clear" in signals:
        print("VERDICT: `clear` IS reachable. The personas are calibrated and the")
        print("         18 production assessments really were weak ideas.")
        print("         -> Record this in the spec. Change NO persona files.")
    else:
        print("VERDICT: `clear` is NOT reachable even for deliberately clean cases.")
        print("         This is a prompt defect, not a property of the ideas.")
        print("         -> Proceed to Step 4 and fix the personas.")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run the diagnostic**

Run: `.venv-test/bin/python scripts/diagnose_specialist_calibration.py`

Expected: three lines, one per domain, each printing a signal, then a VERDICT block. This makes three real Opus calls.

- [ ] **Step 3: Record the finding in the spec**

Append to §5 of `docs/specs/2026-08-18-specialist-panel-remediation-design.md` a short subsection titled `### Diagnosis result (YYYY-MM-DD)` stating: the three signals observed, which of the two readings the evidence supports, and the decision taken. Write it as a finding with its evidence, not as a claim.

- [ ] **Step 4: ONLY IF the verdict was "NOT reachable" — fix the personas**

Skip this step entirely if `clear` appeared. If it did not, add to **each** of the eight files in `prompts/specialists/`, directly beneath the existing `- **clear** —` bullet:

```markdown
- A worked `clear`: if the PI has run the controls your domain would demand,
  at adequate power, with a result that is interpretable either way, then the
  correct answer is `clear` with your concerns list empty. Returning `caution`
  because something *could* still go wrong is not caution, it is abstention —
  and a panel that abstains eight times tells the hub nothing.
```

These are `prompts/specialists/*.md`, which D6 exempts. **Do not touch any file under `prompts/roles/` or the four top-level PI prompts.**

- [ ] **Step 5: Verify the prompt freeze held**

Run:
```bash
git status --porcelain prompts/ | grep -v '^ M prompts/specialists/' || echo "FREEZE OK: only specialist personas touched"
```
Expected: `FREEZE OK: only specialist personas touched` (or no output at all if Step 4 was skipped).

- [ ] **Step 6: Commit**

```bash
git add scripts/diagnose_specialist_calibration.py docs/specs/2026-08-18-specialist-panel-remediation-design.md prompts/specialists/
git commit -m "diag(specialists): find out whether 'clear' is reachable at all

142/142 production consults returned caution or blocking with zero parse
failures. Two readings fit that: miscalibrated personas, or 18 genuinely
weak ideas. This asks the same personas about ideas built to be clean in
their own domain, which separates the two."
```

---

### Task 2: Phase 1a — migration 0029 and the two new columns

**Files:**
- Create: `alembic/versions/0029_add_panel_incomplete.py`
- Modify: `src/models/opportunity.py:47-58` (add two mapped columns)
- Modify: `CLAUDE.md` (the deferred `is_admin` drop moves from `0029` to `0030`)
- Test: `tests/unit/test_opportunity_models.py`

**Interfaces:**
- Produces: `OpportunityAssessment.panel_incomplete: Mapped[bool]` (NOT NULL, server default `false`) and `OpportunityAssessment.missing_domains: Mapped[list | None]` (JSONB, nullable). Tasks 3 and 4 both consume these exact names.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_opportunity_models.py` (or append if it exists):

```python
"""The two columns the specialist floor writes when it declines to destroy a
verdict. See docs/specs/2026-08-18-specialist-panel-remediation-design.md §6."""

from src.models.opportunity import OpportunityAssessment


def test_assessment_has_panel_incomplete_defaulting_to_false():
    cols = OpportunityAssessment.__table__.columns
    assert "panel_incomplete" in cols
    assert cols["panel_incomplete"].nullable is False, (
        "a verdict with an unknown panel state is worse than useless for "
        "triage — the column must always answer"
    )
    assert cols["panel_incomplete"].server_default is not None, (
        "needs a server default so the migration can run BEFORE the new code "
        "serves, the way 0028 had to"
    )


def test_assessment_has_nullable_missing_domains():
    cols = OpportunityAssessment.__table__.columns
    assert "missing_domains" in cols
    assert cols["missing_domains"].nullable is True, (
        "NULL means 'no gap', which is different from an empty list meaning "
        "'a gap we could not name'"
    )
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_opportunity_models.py -v`
Expected: FAIL — `assert 'panel_incomplete' in cols`.

- [ ] **Step 3: Add the columns to the model**

In `src/models/opportunity.py`, immediately after the `raw_verdict` column:

```python
    # The specialist floor's finding, recorded rather than enforced by
    # discarding. `_specialist_floor_gap` used to REFUSE an advance/conditional
    # verdict whose panel was never convened — but it runs after the concluding
    # reply is already in Slack, so refusing meant the PI had been told and
    # Blackbird kept nothing. Both production refusals (gordy, 2026-08-17) lost
    # a real conditional verdict that way. Storing it flagged keeps the record
    # and keeps the warning; it does not mean the gap is acceptable.
    panel_incomplete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # Which domains were missing. NULL when there was no gap — distinct from
    # [] which would mean a gap whose domains we failed to name.
    missing_domains: Mapped[list | None] = mapped_column(JSONB, nullable=True)
```

Add `Boolean` and `text` to the existing SQLAlchemy import at the top of the file:

```python
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String, Text, func, text
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv-test/bin/python -m pytest tests/unit/test_opportunity_models.py -v`
Expected: 2 passed.

- [ ] **Step 5: Write the migration**

Create `alembic/versions/0029_add_panel_incomplete.py`:

```python
"""Add opportunity_assessments.panel_incomplete / .missing_domains

Revision ID: 0029
Revises: 0028
Create Date: 2026-08-18 00:00:00.000000

Additive with a server default, so old code against the new schema keeps
working. The reverse is NOT safe: the model maps both columns as of this
change, so every select(OpportunityAssessment) names them and would raise
UndefinedColumn against a pre-0029 database. Migrate BEFORE the new code
serves — the same ordering 0028 needed.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "opportunity_assessments",
        sa.Column(
            "panel_incomplete",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "opportunity_assessments",
        sa.Column("missing_domains", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("opportunity_assessments", "missing_domains")
    op.drop_column("opportunity_assessments", "panel_incomplete")
```

- [ ] **Step 6: Move the reserved 0029 note in CLAUDE.md**

In `CLAUDE.md`, under "Account Types", change:

> Dropping it is deferred to a separate later migration, `0029`, which **has not been written, let alone applied**

to:

> Dropping it is deferred to a separate later migration, `0030`, which **has not been written, let alone applied**

`0029` is now taken by this work. Nothing in the chain renumbers, because the `is_admin` drop was never written.

- [ ] **Step 7: Run the full gate — it round-trips the migration**

Run: `./scripts/ci.sh`
Expected: PASS. This is the step that proves `downgrade()` works; `ci.sh` does an upgrade→downgrade→upgrade cycle against a throwaway Postgres.

- [ ] **Step 8: Commit**

```bash
git add alembic/versions/0029_add_panel_incomplete.py src/models/opportunity.py tests/unit/test_opportunity_models.py CLAUDE.md
git commit -m "feat(assessments): record an incomplete panel instead of losing the verdict

Adds panel_incomplete and missing_domains. Additive with a server
default, so it can be applied before the new code serves — which it must
be, because the model maps both columns from this commit on."
```

---

### Task 3: Phase 1b — persist the verdict with the flag instead of discarding it (F3)

**Files:**
- Modify: `src/agent/simulation.py:2718-2740` (the gap branch in `_persist_assessment`) and the `assessment_kwargs` dict at `:2782-2801`
- Test: `tests/integration/test_opportunity_assessment_persistence.py`

**Interfaces:**
- Consumes: `OpportunityAssessment.panel_incomplete`, `.missing_domains` (Task 2)
- Produces: `_persist_assessment` no longer returns early on a gap. `_specialist_floor_gap` keeps its exact signature — `(verdict: dict, *, thread: ThreadState | None = None) -> set[str]` — and Task 6 relies on that.

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_opportunity_assessment_persistence.py`, following the
`engine` + `async_sessionmaker` pattern the existing `_persist_assessment` tests in that
file already use (see `test_persist_assessment_recomputes_the_score_it_is_handed`):

```python
@pytest.mark.asyncio
async def test_a_gapped_verdict_is_stored_and_flagged_not_discarded(engine):
    """The floor's finding must not cost the verdict.

    _persist_assessment runs AFTER the concluding reply is already in Slack, so
    refusing meant the PI had been told and Blackbird kept nothing. Both
    production refusals (gordy, 2026-08-17) lost a real conditional verdict
    exactly this way.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine
    from src.services.blackbird_rubric import RUBRIC_WEIGHTS

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    stub = SimulationEngine(
        agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
    )
    # Arm the floor: a consult for SOME OTHER PI proves this process records
    # consults, so an absent record for `gordy` means the panel was skipped
    # rather than that we restarted mid-interview.
    stub._record_consult("someone_else", "scientific")

    await SimulationEngine._persist_assessment(
        stub, "blackbird", "general",
        {
            "subject_agent_id": "gordy",
            "recommendation": "conditional",
            "rationale": "A peptide-based vaccine platform for tuberculosis.",
            "scores": {k: 3 for k in RUBRIC_WEIGHTS},
        },
        slack_ts="1.1",
        subject_agent_id_fallback="gordy",
    )

    async with factory() as db:
        rows = (await db.execute(select(OpportunityAssessment))).scalars().all()

    assert len(rows) == 1, "the verdict must be stored, not discarded"
    row = rows[0]
    assert row.panel_incomplete is True
    assert "chemistry" in row.missing_domains
    assert row.recommendation == "conditional", "the verdict itself is unchanged"
    assert row.weighted_score is not None, "scoring still runs on a flagged row"


@pytest.mark.asyncio
async def test_a_complete_panel_stores_an_unflagged_row(engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine
    from src.services.blackbird_rubric import RUBRIC_WEIGHTS

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    stub = SimulationEngine(
        agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
    )
    for domain in ("scientific", "talent", "chemistry", "clinical", "technologic"):
        stub._record_consult("gordy", domain)

    await SimulationEngine._persist_assessment(
        stub, "blackbird", "general",
        {
            "subject_agent_id": "gordy",
            "recommendation": "conditional",
            "rationale": "A peptide-based platform for a tuberculosis indication.",
            "scores": {k: 3 for k in RUBRIC_WEIGHTS},
        },
        slack_ts="1.1",
        subject_agent_id_fallback="gordy",
    )

    async with factory() as db:
        row = (await db.execute(select(OpportunityAssessment))).scalars().one()
    assert row.panel_incomplete is False
    assert row.missing_domains is None, (
        "NULL means no gap; [] would mean a gap we could not name"
    )
```

`select`, `OpportunityAssessment` and `SimulationRun` are already imported at the top of
this file. **Note for Task 6:** these two tests call `_record_consult` with no
`thread_id` and must keep passing after Task 6 re-keys the record — they exercise the
`None`-keyed slot deliberately.

- [ ] **Step 2: Run it to make sure it fails**

Run: `.venv-test/bin/python -m pytest tests/integration/test_opportunity_assessment_persistence.py -k gapped -v`
Expected: FAIL — `assert len(rows) == 1` gets 0, because the gap branch still returns early.

- [ ] **Step 3: Replace the refusal with a flagged persist**

In `src/agent/simulation.py`, replace the whole `if gap:` block (the `logger.warning`, the `await self._record_assessment_drop(...)` and the bare `return`) with:

```python
        gap = self._specialist_floor_gap(subject_view, thread=thread)
        if gap:
            logger.warning(
                "[%s] Assessment for %s stored with an INCOMPLETE PANEL — "
                "recommendation %r required the %s specialist(s), never "
                "consulted during the interview. The verdict is flagged, not "
                "discarded: this check runs after the concluding reply is "
                "already in Slack, so refusing it left the PI told and "
                "Blackbird holding nothing.",
                agent_id, subject_view.get("subject_agent_id") or "?",
                verdict.get("recommendation"), ", ".join(sorted(gap)),
            )
```

Note what is deliberately gone: the `_record_assessment_drop(..., "specialist_floor", ...)` call and the `return`. A stored row is not a drop. The `specialist_floor` reason string stays defined in `src/models/opportunity.py` and handled in the template, because three historical rows still carry it.

- [ ] **Step 4: Carry the gap onto the row**

In the same method, add two entries to the `assessment_kwargs` dict, immediately after `raw_verdict=verdict,`:

```python
            panel_incomplete=bool(gap),
            # NULL, not [], when there is no gap — see the column comment.
            missing_domains=sorted(gap) if gap else None,
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv-test/bin/python -m pytest tests/integration/test_opportunity_assessment_persistence.py -v`
Expected: all pass, including the two new ones.

- [ ] **Step 6: Confirm no prompt drifted**

Run: `git diff --stat prompts/ src/agent/thread_guidance.py`
Expected: no output.

- [ ] **Step 7: Run the full gate**

Run: `./scripts/ci.sh`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/agent/simulation.py tests/integration/test_opportunity_assessment_persistence.py
git commit -m "fix(specialists): stop the floor destroying the verdicts it gates

The floor runs after the concluding reply is already posted, so refusing
meant the PI had been given the verdict and Blackbird kept nothing but a
drop row. Both production refusals lost a real conditional verdict that
way. Store it flagged instead; the warning is unchanged in severity."
```

---

### Task 4: Phase 1c — surface the flag on the assessments page

An incomplete-panel verdict must never read as a vetted one.

**Files:**
- Modify: `src/services/directory.py` (`list_assessments`, from line 147)
- Modify: `templates/admin/_assessments_body.html`
- Test: `tests/unit/test_directory_assessments.py`

**Interfaces:**
- Consumes: `OpportunityAssessment.panel_incomplete`, `.missing_domains` (Task 2)
- Produces: `list_assessments` returns an added key `incomplete_panel_count: int` in its result dict. Task 9 consumes it.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_directory_assessments.py`:

```python
"""An incomplete-panel verdict must be visibly distinct from a vetted one.

Storing it (Task 3) is only safe if the page says so — otherwise Task 3 turns
a loud refusal into a silent, ordinary-looking row.
"""

import pytest
from sqlalchemy import select

from src.models.opportunity import OpportunityAssessment
from src.services.directory import list_assessments


@pytest.mark.asyncio
async def test_list_assessments_counts_incomplete_panels(db_session):
    run = await factories.make_simulation_run(db_session)
    db_session.add(
        OpportunityAssessment(
            simulation_run_id=run.id, agent_id="blackbird",
            subject_agent_id="gordy", channel_name="general",
            recommendation="conditional",
            panel_incomplete=True, missing_domains=["chemistry"],
        )
    )
    db_session.add(
        OpportunityAssessment(
            simulation_run_id=run.id, agent_id="blackbird",
            subject_agent_id="wu", channel_name="general",
            recommendation="pass", panel_incomplete=False,
        )
    )
    await db_session.commit()

    view = await list_assessments(db_session, str(run.id))
    assert view["incomplete_panel_count"] == 1
```

`db_session` is the session-scoped fixture from `tests/conftest.py:82`;
`factories.make_simulation_run` is at `tests/factories.py:82`. Import it with
`from tests import factories`. There is no `seeded_run` fixture — do not invent one.

- [ ] **Step 2: Run it to make sure it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_directory_assessments.py -v`
Expected: FAIL with `KeyError: 'incomplete_panel_count'`.

- [ ] **Step 3: Add the count to the service**

In `src/services/directory.py`, inside `list_assessments`, after the existing `total_count` computation, add:

```python
    # Surfaced because Task 3 stops the floor discarding a gapped verdict.
    # Storing it is only safe if the page distinguishes it from a vetted one.
    incomplete_query = select(func.count()).select_from(OpportunityAssessment).where(
        OpportunityAssessment.panel_incomplete.is_(True)
    )
    if not show_all_runs and selected_run_id:
        incomplete_query = incomplete_query.where(
            OpportunityAssessment.simulation_run_id == selected_run_id
        )
    incomplete_panel_count = (await db.execute(incomplete_query)).scalar_one()
```

Add `incomplete_panel_count` to the returned dict. Ensure `func` is imported from `sqlalchemy` at the top of the file.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv-test/bin/python -m pytest tests/unit/test_directory_assessments.py -v`
Expected: PASS.

- [ ] **Step 5: Render the flag in the shared template**

In `templates/admin/_assessments_body.html`, immediately after the closing `{% endif %}` of the existing `drops_total` banner, add:

```html
{# Verdicts stored despite a gap in the specialist panel. Distinct from the
   dropped-verdict banner above: nothing was lost here, but nothing was fully
   vetted either, and a triage queue that renders the two identically is how
   an unvetted verdict gets acted on. #}
{% if incomplete_panel_count %}
<div class="rounded-xl border border-orange-300 bg-orange-50 p-4 mb-6">
    <div class="flex items-start gap-3">
        <span class="text-orange-600 text-xl leading-none">&#9873;</span>
        <div class="text-sm text-orange-900">
            <span class="font-semibold">
                {{ incomplete_panel_count }} verdict{{ '' if incomplete_panel_count == 1 else 's' }}
                stored with an incomplete specialist panel.
            </span>
            The required domains were never consulted during the interview, so
            the verdict is recorded but not fully vetted. Treat the score as
            provisional.
        </div>
    </div>
</div>
{% endif %}
```

And in the verdict table, inside the cell that already renders `recommendation`, append:

```html
{% if a.panel_incomplete %}
<span class="ml-1 text-xs font-medium text-orange-700"
      title="Missing: {{ (a.missing_domains or [])|join(', ') }}">&#9873; panel</span>
{% endif %}
```

This file is shared by the admin and manager pages and **must stay free of absolute admin/manager URLs** — the additions above contain none.

- [ ] **Step 6: Pass the new key from both routers**

In `src/routers/admin.py`, in `admin_assessments` (line 637 onward), add `incomplete_panel_count=view["incomplete_panel_count"],` to the template context. Do the same in the manager assessments route so the shared body renders identically for both. Find it with:

```bash
grep -rn "_assessments_body.html\|assessments=view" src/routers/
```

- [ ] **Step 7: Run the full gate**

Run: `./scripts/ci.sh`
Expected: PASS. `tests/unit/test_reachability.py` also runs here and will catch an unreferenced route or a template link mistake.

- [ ] **Step 8: Commit**

```bash
git add src/services/directory.py templates/admin/_assessments_body.html src/routers/ tests/unit/test_directory_assessments.py
git commit -m "feat(admin): mark verdicts stored with an incomplete panel

Task 3 stops the floor discarding a gapped verdict, which is only safe if
the triage queue shows the difference. An unflagged row means the panel
was asked; it does not mean the panel approved."
```

---

### Task 5: Phase 2 — word-boundary cue matching (F4)

Three substring cues fire inside ordinary English: `"aso"` in *reasons*, `"hit"` in *architecture*, `"als"` in *also/signals/animals/journals*. Measured across the 18 production verdicts, **3 of 18 had a domain required solely by one of these**. The cost is not the extra consult the code comment assumes — the floor runs after the interview is over, so it is the whole verdict.

**Files:**
- Modify: `src/agent/specialists.py:176-251`
- Test: `tests/unit/test_specialists.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `required_domains_for(verdict: object) -> frozenset[str]` — signature unchanged. Internal helper `_cue_matches(cue: str, text: str) -> bool` is new and private.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_specialists.py`:

```python
import itertools

from src.agent.specialists import required_domains_for


def _verdict(text, **over):
    v = {
        "recommendation": "advance", "subject_agent_id": "x",
        "rationale": text, "company_or_project": "", "funnel_stage": "",
        "red_flags": [], "suggested_derisking_milestones": [],
    }
    v.update(over)
    return v


def test_reasons_does_not_summon_the_chemistry_specialist():
    """'aso' (antisense oligonucleotide) must not match 'reasons'.

    Fired on 7 of 18 production verdicts. On `hart` it was the ONLY chemistry
    cue present, so it alone decided the requirement.
    """
    assert "chemistry" not in required_domains_for(
        _verdict("We passed for several reasons, none of them chemical.")
    )


def test_architecture_does_not_summon_the_chemistry_specialist():
    """'hit' (a screening hit) must not match 'architecture'. Fired on 6/18."""
    assert "chemistry" not in required_domains_for(
        _verdict("The granuloma architecture is not recapitulated in this model.")
    )


def test_also_and_signals_do_not_summon_the_clinical_specialist():
    """'als' (the disease) must not match 'also', 'signals', 'animals',
    'journals'. Fired on 9/18; on `mcmeniman` it was the only clinical cue."""
    for word in ("also", "signals", "animals", "journals"):
        assert "clinical" not in required_domains_for(
            _verdict(f"There are {word} to consider here.")
        ), f"{word!r} must not read as ALS"


def test_genuine_cues_still_match():
    """Narrowing must not become blindness."""
    assert "chemistry" in required_domains_for(_verdict("We have ASOs in hand."))
    assert "chemistry" in required_domains_for(_verdict("An aso-based approach."))
    assert "chemistry" in required_domains_for(_verdict("A known-compound series."))
    assert "chemistry" in required_domains_for(
        _verdict("Medicinal chemistry is tractable.")
    )
    assert "clinical" in required_domains_for(_verdict("An ALS indication."))
    assert "clinical" in required_domains_for(_verdict("A clinical-stage asset."))
    assert "clinical" in required_domains_for(_verdict("Patient-derived organoids."))
    assert "clinical" in required_domains_for(_verdict("Neurodegeneration broadly."))


def test_only_the_documented_domains_are_reachable():
    """Which domains the floor can EVER require, asserted rather than assumed.

    `commercial` and `budget` cannot be required by any input — proven
    exhaustively here rather than trusted. That is finding F5, and this test is
    what would have caught it. F5 and F6a are deferred by D6 (the fixes need a
    hub prompt change), so this pins the deferred state honestly instead of
    leaving five-of-eight as a fact remembered only in a design doc.
    """
    reachable: set[str] = set()
    cue_texts = [
        "small molecule compound inhibitor antibody peptide modality scaffold",
        "disease patient indication clinical therapeutic cancer tumor",
        "platform pipeline reusable multiple shots",
        "commercial competitor landscape deal comps investor budget cost timeline",
        "",
    ]
    for r in range(len(cue_texts) + 1):
        for combo in itertools.combinations(cue_texts, r):
            text = " ".join(combo)
            for fto in ("met", "not_met", "unconfirmed", None):
                for platform in (1, 3, 4, 5, None):
                    reachable |= required_domains_for(
                        _verdict(
                            text,
                            company_or_project=text,
                            red_flags=[text],
                            suggested_derisking_milestones=[text],
                            gating={"fto_achievable": fto},
                            scores={"platform": platform},
                        )
                    )

    assert reachable == {
        "scientific", "talent", "chemistry", "clinical", "technologic", "legal",
    }
    assert {"commercial", "budget"}.isdisjoint(reachable), (
        "commercial maps to `differentiation`, the heaviest dimension at 15%, "
        "and the floor still cannot demand it — F5, deferred by D6"
    )
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_specialists.py -k "reasons or architecture or also_and_signals" -v`
Expected: 3 FAIL. `test_genuine_cues_still_match` and the reachability test should already PASS — they pin behaviour that must survive.

- [ ] **Step 3: Implement bounded matching**

In `src/agent/specialists.py`, immediately above `_haystack`, add:

```python
# Cues short enough to appear inside unrelated English. These match as WHOLE
# WORDS (plus a simple plural); every other cue stays prefix-anchored so stems
# like "medicinal chem" -> "medicinal chemistry" and "neurodegener" ->
# "neurodegeneration" keep working.
#
# Measured on the 18 production verdicts of run 1787010946: "aso" matched
# "reasons" on 7, "hit" matched "architecture" on 6, and "als" matched
# "also"/"signals"/"animals"/"journals" on 9. Three verdicts had a specialist
# required by one of these ALONE.
_WORD_ONLY_CUES: frozenset[str] = frozenset({"als", "aso", "hit", "adc"})


@lru_cache(maxsize=None)
def _cue_pattern(cue: str) -> re.Pattern[str]:
    escaped = re.escape(cue)
    if cue in _WORD_ONLY_CUES:
        return re.compile(rf"(?<![a-z0-9]){escaped}(?:s|es)?(?![a-z0-9])")
    return re.compile(rf"(?<![a-z0-9]){escaped}")


def _cue_matches(cue: str, text: str) -> bool:
    """Whether ``cue`` occurs in ``text`` as a word rather than as a fragment.

    The lookbehind is what kills the false positives: "reasons" contains "aso"
    but not at a word boundary. Hyphens count as boundaries, so "aso-based" and
    "known-compound" still match.

    The old comment here claimed "a false positive costs one consult, a false
    negative costs the whole point of the floor." That cost model was inverted:
    this runs AFTER the interview is over, so a false positive cost the whole
    verdict.
    """
    return _cue_pattern(cue).search(text) is not None
```

Add `from functools import lru_cache` to the imports at the top of the file.

Then in `required_domains_for`, replace the three membership tests:

```python
    if any(_cue_matches(cue, text) for cue in _CHEMISTRY_CUES):
        required.add("chemistry")
    if any(_cue_matches(cue, text) for cue in _CLINICAL_CUES):
        required.add("clinical")
```

and

```python
    if platform_scored or any(_cue_matches(cue, text) for cue in _PLATFORM_CUES):
        required.add("technologic")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_specialists.py tests/unit/test_specialist_floor.py -v`
Expected: all pass.

- [ ] **Step 5: Verify against the real production verdicts**

This is the check that matters — the unit tests use synthetic strings, and the fix is only worth shipping if it changes the right real verdicts. Run:

```bash
docker compose -f docker-compose.prod.yml exec -T postgres psql -U copi -d copi -t -A \
  -c "SELECT json_agg(raw_verdict) FROM opportunity_assessments;" > /tmp/verdicts.json

.venv-test/bin/python - <<'PY'
import json
from src.agent.specialists import required_domains_for
for v in json.load(open("/tmp/verdicts.json")):
    print(v.get("subject_agent_id"), sorted(required_domains_for(v)))
PY
```

Expected: exactly three rows lose a domain relative to the audit baseline — `pearce` and `hart` each lose `chemistry`, `mcmeniman` loses `clinical`. Every other row is unchanged. If any other row changes, a legitimate cue was broken: stop and investigate before committing.

- [ ] **Step 6: Run the full gate**

Run: `./scripts/ci.sh`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/agent/specialists.py tests/unit/test_specialists.py
git commit -m "fix(specialists): match cues on word boundaries, not substrings

'aso' matched 'reasons', 'hit' matched 'architecture', 'als' matched
'also'/'signals'/'animals'/'journals'. Across the 18 production verdicts
three had a specialist required by one of these alone.

The comment claiming a false positive costs one consult was inverted:
this runs after the interview is over, so it cost the whole verdict.
Adds a reachability test pinning which domains the floor can require at
all, which is what would have caught F5."
```

---

### Task 6: Phase 3 — key the consult record per interview (F10)

`_specialist_consults` is keyed by PI and never cleared, so a PI's second interview inherits the first's consults. `huganir` was assessed 4 times in one run, `hart` 4, `pearce` 2.

**Do not start this task until Tasks 3 and 4 are merged.** This tightens the floor; Task 3 is what stops a tightened floor destroying verdicts.

**Files:**
- Modify: `src/agent/simulation.py:2987-3009` (`_record_consult`, `_consulted_domains`), `:3013-3095` (`_specialist_floor_gap`), `:1627` (the tool-executor closure)
- Test: `tests/unit/test_specialist_floor.py`

**Interfaces:**
- Consumes: `ThreadState.thread_id`, `ThreadState.other_agent_id`
- Produces: `_record_consult(pi_agent_id: str, domain: str, thread_id: str | None = None) -> None` and `_consulted_domains(pi_agent_id: str, thread_id: str | None = None) -> frozenset[str]`. `self._specialist_consults` becomes `dict[tuple[str, str | None], set[str]]`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_specialist_floor.py`:

```python
def test_a_second_interview_does_not_inherit_the_first_ones_consults():
    """One PI, two interviews: the second must convene its own panel.

    `huganir` was assessed 4 times in run 1787010946 and `hart` 4 times. Under
    PI-only keying every assessment after the first rode on the first
    interview's consults.
    """
    eng = _engine(_hub())
    eng._record_consult("huganir", "chemistry", thread_id="t1")

    assert eng._consulted_domains("huganir", "t1") == frozenset({"chemistry"})
    assert eng._consulted_domains("huganir", "t2") == frozenset(), (
        "a different interview with the same PI starts with no panel"
    )


def test_the_floor_reads_the_consults_of_this_interview_only():
    eng = _engine(_hub())
    thread_one = _activated_thread(eng, "t1", other_agent_id="huganir")
    for domain in ("scientific", "talent"):
        eng._record_consult("huganir", domain, thread_id="t1")
    thread_two = _activated_thread(eng, "t2", other_agent_id="huganir")
    thread_two.floor_armed = True

    verdict = {
        "subject_agent_id": "huganir",
        "recommendation": "advance",
        "rationale": "No cues here.",
    }
    assert eng._specialist_floor_gap(verdict, thread=thread_one) == set()
    assert eng._specialist_floor_gap(verdict, thread=thread_two) == {
        "scientific", "talent",
    }


def test_a_consult_without_a_thread_is_still_recorded():
    """Direct callers and pre-existing tests pass no thread; they must keep
    working, keyed under a None interview."""
    eng = _engine(_hub())
    eng._record_consult("huganir", "scientific")
    assert eng._consulted_domains("huganir") == frozenset({"scientific"})
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_specialist_floor.py -k "second_interview or this_interview_only" -v`
Expected: FAIL — `_record_consult() got an unexpected keyword argument 'thread_id'`.

- [ ] **Step 3: Re-key the record**

In `src/agent/simulation.py`, change the declaration at line 227:

```python
        # (pi_agent_id, thread_id) -> the specialist domains consulted during
        # that interview. Keyed per INTERVIEW, not per PI: a PI's second
        # interview must convene its own panel rather than inherit the first
        # one's. `huganir` was assessed 4 times in run 1787010946 and every
        # assessment after the first rode on the first interview's consults.
        # `thread_id` is None for direct callers that have no interview.
        self._specialist_consults: dict[tuple[str, str | None], set[str]] = {}
```

Replace `_record_consult` and `_consulted_domains` with:

```python
    def _record_consult(
        self, pi_agent_id: str, domain: str, thread_id: str | None = None,
    ) -> None:
        """Note a successful consult, keyed on the interview it happened in.

        Keyed on ``(pi, thread)`` rather than the PI alone. One PI's consults
        are NOT cumulative across interviews: a second interview is a second
        idea and owes its own panel. ``thread_id`` is None for direct callers
        that have no interview to name.
        """
        if not pi_agent_id:
            return
        self._specialist_consults.setdefault((pi_agent_id, thread_id), set()).add(domain)

    def _consulted_domains(
        self, pi_agent_id: str, thread_id: str | None = None,
    ) -> frozenset[str]:
        """Domains consulted about this PI in this interview; empty for an
        interview we have no record of."""
        return frozenset(self._specialist_consults.get((pi_agent_id, thread_id), ()))
```

- [ ] **Step 4: Join the floor on the interview**

In `_specialist_floor_gap`, replace the final two lines:

```python
        consulted = self._consulted_domains(
            subject, thread.thread_id if thread is not None else None
        )
        return set(required_domains_for(verdict) - consulted)
```

Then update the method's docstring: the paragraph beginning "The record is keyed on the PI (``subject_agent_id``), not on a thread." now states the opposite of the code. Replace it with a paragraph saying the record is keyed on `(subject, thread)`, that an earlier PI-only version let a PI's later interviews inherit an earlier panel, and that `thread=None` reads the `None`-keyed slot. **Leaving that docstring contradicting the code is a task failure** — this file's comments are load-bearing.

- [ ] **Step 5: Pass the thread id at the call site**

At `src/agent/simulation.py:1627`, in the `tool_executor` closure:

```python
                on_consult=lambda domain, _pi=thread.other_agent_id, _t=thread.thread_id: (
                    self._record_consult(_pi, domain, _t)
                ),
```

The default-argument binding is deliberate and must be kept: it captures the values at closure-creation time, so a later turn on another thread cannot rebind them.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_specialist_floor.py -v`
Expected: all pass. Several pre-existing tests call `_record_consult(pi, domain)` with no thread; those must still pass unchanged via the `None` default. If any fails, fix the production default rather than the test.

- [ ] **Step 7: Run the full gate**

Run: `./scripts/ci.sh`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/agent/simulation.py tests/unit/test_specialist_floor.py
git commit -m "fix(specialists): a PI's second interview must convene its own panel

The consult record was keyed on the PI and never cleared, so every repeat
interview inherited the first one's consults. huganir was assessed four
times in one run and hart four; only the first ever faced a panel.

Safe to tighten only because a gapped verdict is now stored and flagged
rather than discarded."
```

---

### Task 7: Phase 4a — a silent specialist must not satisfy the floor (F9)

`""`, `"   "`, `"null"` and `"[]"` all parse to `caution`/`low` today and fire `on_consult`. "The specialist was unreachable" must not become "the specialist approved". Prose stays a valid opinion — that was a deliberate design decision and it is not being reversed.

**Files:**
- Modify: `src/agent/specialists.py` (add `has_usable_content`)
- Modify: `src/agent/tools.py:492-496` (gate `on_consult`)
- Test: `tests/unit/test_specialists.py`, `tests/unit/test_consult_accounting.py`

**Interfaces:**
- Produces: `has_usable_content(raw: str) -> bool` in `src.agent.specialists`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_specialists.py`:

```python
from src.agent.specialists import has_usable_content


def test_an_empty_reply_is_not_an_opinion():
    """A call that returned nothing must not satisfy the floor."""
    for raw in ("", "   ", "\n\n", "null", "[]", "{}", "3", '"x"'):
        assert has_usable_content(raw) is False, f"{raw!r} carries no opinion"


def test_prose_is_still_an_opinion():
    """Deliberate: a specialist answering in sentences has still answered.
    Only a call that returned NOTHING is excluded."""
    assert has_usable_content("The controls here are inadequate.") is True
    assert has_usable_content("I cannot assess this without the assay.") is True


def test_a_partial_json_opinion_counts():
    assert has_usable_content('{"verdict_signal": "clear"}') is True
    assert has_usable_content('{"concerns": ["off-target risk"]}') is True
    assert has_usable_content('```json\n{"confidence": "high"}\n```') is True
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_specialists.py -k usable -v`
Expected: FAIL — `ImportError: cannot import name 'has_usable_content'`.

- [ ] **Step 3: Implement it**

In `src/agent/specialists.py`, directly below `parse_opinion`:

```python
_OPINION_FIELDS = ("verdict_signal", "concerns", "questions_to_ask", "confidence")


def has_usable_content(raw: str) -> bool:
    """Whether a specialist's reply carries an opinion at all.

    Narrower than "did it parse". Prose IS an opinion — a specialist answering
    in sentences has answered, and ``parse_opinion`` keeps treating it that way
    on purpose. This excludes only what the design's error table meant by a
    failed call: a reply with nothing in it.

    The distinction matters because ``on_consult`` is what satisfies the
    enforcement floor. If an empty reply counted, "the specialist was
    unreachable" would silently become "the specialist approved".
    """
    text = _strip_fence(raw or "").strip()
    if not text:
        return False
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return True  # unparseable prose is still an answer
    if not isinstance(data, dict):
        return False  # null, [], a bare number or string say nothing
    return any(field in data for field in _OPINION_FIELDS)
```

- [ ] **Step 4: Gate `on_consult` on it**

In `src/agent/tools.py`, in `_execute_consult_specialist`, replace:

```python
    opinion = parse_opinion(raw, domain=domain)
    if on_consult is not None:
        on_consult(domain)
```

with:

```python
    opinion = parse_opinion(raw, domain=domain)
    # A billed call that came back empty is not an opinion. `on_api_call`
    # already fired above (it answers "was this billed?"); `on_consult` answers
    # "does this satisfy the floor?" and the two must disagree here.
    if not has_usable_content(raw):
        logger.error(
            "[specialists] %s returned no usable content — NOT counted as "
            "consulted", domain,
        )
        return (
            f"The {domain} specialist returned an empty response. Proceed "
            "without this opinion; it will not count as consulted."
        )
    if on_consult is not None:
        on_consult(domain)
```

Add `has_usable_content` to the existing `from src.agent.specialists import (...)` block at the top of `src/agent/tools.py`.

- [ ] **Step 5: Add the accounting test**

Append to `tests/unit/test_consult_accounting.py`:

```python
@pytest.mark.asyncio
async def test_an_empty_specialist_reply_is_billed_but_not_counted(monkeypatch):
    """The two callbacks must disagree: the call happened and is billed, but
    it produced no opinion and must not satisfy the floor."""
    consulted, billed = [], []

    async def _empty(*args, **kwargs):
        return "   "

    monkeypatch.setattr("src.agent.tools.generate_agent_response", _empty)

    result = await _execute_consult_specialist(
        "chemistry", "Is the series tractable?", "The PI said little.",
        agent_id="blackbird",
        on_consult=consulted.append,
        on_api_call=lambda: billed.append(1),
    )

    assert billed, "a call that was issued is billed whatever it returned"
    assert consulted == [], "an empty reply must not satisfy the floor"
    assert "empty response" in result
```

Import `_execute_consult_specialist` from `src.agent.tools` at the top of that file if it is not already imported.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_specialists.py tests/unit/test_consult_accounting.py -v`
Expected: all pass.

- [ ] **Step 7: Run the full gate**

Run: `./scripts/ci.sh`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/agent/specialists.py src/agent/tools.py tests/unit/test_specialists.py tests/unit/test_consult_accounting.py
git commit -m "fix(specialists): an empty reply must not satisfy the floor

'', whitespace, null and [] all parsed to caution/low and counted as a
consult, so 'the specialist was unreachable' could become 'the specialist
approved'. Prose still counts, deliberately — only a reply with nothing
in it is excluded."
```

---

### Task 8: Phase 4b — pin the duplicated domain list, and trace dropped verdicts (F11, F12)

**Files:**
- Modify: `src/agent/simulation.py` (the `_record_assessment_drop` call in `_capture_hub_assessment`'s unparseable branch already passes `thread_id`; the remaining gap is any call site that does not)
- Test: `tests/unit/test_tool_gating.py`

**Interfaces:**
- Consumes: `SPECIALIST_DOMAINS` (`src.agent.specialists`), `TOOL_DEFINITIONS` (`src.agent.tools`)
- Produces: no new API.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_tool_gating.py`:

```python
def test_the_tool_description_names_every_specialist_domain():
    """Two sources of truth for the panel's domains: SPECIALIST_DOMAINS and the
    hardcoded prose in the consult_specialist tool description. Nothing pinned
    them together, so adding a ninth domain could leave the hub never told.

    The description is hub-facing text and frozen by D6 — this test pins the
    agreement that already exists, it does not license changing either side.
    """
    from src.agent.specialists import SPECIALIST_DOMAINS
    from src.agent.tools import TOOL_DEFINITIONS

    tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "consult_specialist")
    description = tool["description"]
    enum = tool["input_schema"]["properties"]["domain"]["enum"]

    assert set(enum) == set(SPECIALIST_DOMAINS)
    for domain in SPECIALIST_DOMAINS:
        assert f"'{domain}'" in description, (
            f"{domain} is dispatchable but the hub is never told it exists"
        )
```

- [ ] **Step 2: Run it**

Run: `.venv-test/bin/python -m pytest tests/unit/test_tool_gating.py -k description -v`
Expected: PASS immediately. The two are in agreement today; this test is a ratchet that keeps them so. If it FAILS, the drift already exists — report it and stop rather than editing the frozen description.

- [ ] **Step 3: Pass the thread id through on the drop path**

Audit every `_record_assessment_drop` call site:

```bash
grep -n "_record_assessment_drop" src/agent/simulation.py
```

Each call must pass `thread_id=` where a thread is in scope. `_persist_assessment` has `thread` as a parameter, so any drop recorded there passes `thread_id=thread.thread_id if thread else None`. Without it a dropped verdict cannot be traced to the interview that produced it — the three historical `specialist_floor` rows all have `NULL`.

- [ ] **Step 4: Run the full gate**

Run: `./scripts/ci.sh`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent/simulation.py tests/unit/test_tool_gating.py
git commit -m "test(specialists): pin the duplicated domain list; trace dropped verdicts

The panel's domains are declared twice — SPECIALIST_DOMAINS and the
hardcoded tool description — with nothing keeping them in step. Pins the
agreement rather than changing either side, since the description is
hub-facing and frozen. Also threads thread_id onto the drop path."
```

---

### Task 9: Phase 5 — instrumentation (F8 partial, F1)

Make the failures this audit found visible without an audit.

**Files:**
- Modify: `src/services/directory.py` (`list_assessments`)
- Modify: `templates/admin/_assessments_body.html`
- Test: `tests/unit/test_directory_assessments.py`

**Interfaces:**
- Consumes: `list_assessments`'s existing return dict, `RUBRIC_WEIGHTS`, `SPECIALIST_DOMAINS[...].maps_to_dimension`
- Produces: added keys `dimension_stats: list[dict]` and `band_counts: list[tuple[str, int]]`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_directory_assessments.py`:

```python
@pytest.mark.asyncio
async def test_dimension_stats_expose_the_constant_dimensions(db_session):
    """Four dimensions never exceeded 2 across all 18 production assessments,
    pinning 23 of 100 weight points near minimum. That is invisible on a page
    that only shows totals."""
    run = await factories.make_simulation_run(db_session)
    for score in (1, 2, 2):
        db_session.add(
            OpportunityAssessment(
                simulation_run_id=run.id, agent_id="blackbird",
                subject_agent_id="wu", channel_name="general",
                band="pass",
                scores={"ip_fto": score, "differentiation": 5},
            )
        )
    await db_session.commit()

    view = await list_assessments(db_session, str(run.id))
    stats = {d["dimension"]: d for d in view["dimension_stats"]}

    assert stats["ip_fto"]["max"] == 2
    assert stats["differentiation"]["max"] == 5
    assert stats["ip_fto"]["specialist"] == "legal", (
        "maps_to_dimension has never had a runtime read; this is it"
    )
    assert view["band_counts"] == [("pass", 3)]
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_directory_assessments.py -k dimension -v`
Expected: FAIL with `KeyError: 'dimension_stats'`.

- [ ] **Step 3: Compute the stats in the service**

In `src/services/directory.py`, inside `list_assessments`, after the rows are fetched:

```python
    # Per-dimension distribution. Four dimensions (external_signals, ip_fto,
    # exit_thesis, chemistry_dc_path) never exceeded 2 across the 18
    # assessments of run 1787010946 — 23 of 100 weight points pinned near
    # minimum, invisible on a page that shows only totals.
    #
    # `specialist` is the first runtime read maps_to_dimension has ever had:
    # it names who to ask when a dimension is scoring badly.
    specialist_for = {
        spec.maps_to_dimension: domain
        for domain, spec in SPECIALIST_DOMAINS.items()
        if spec.maps_to_dimension
    }
    dimension_stats = []
    for dimension, weight in RUBRIC_WEIGHTS.items():
        values = [
            row.scores[dimension]
            for row in assessments
            if isinstance(row.scores, dict)
            and isinstance(row.scores.get(dimension), (int, float))
            and not isinstance(row.scores.get(dimension), bool)
        ]
        dimension_stats.append({
            "dimension": dimension,
            "weight": weight,
            "specialist": specialist_for.get(dimension),
            "n": len(values),
            "mean": round(sum(values) / len(values), 2) if values else None,
            "min": min(values) if values else None,
            "max": max(values) if values else None,
        })

    band_counts = sorted(Counter(
        row.band for row in assessments if row.band
    ).items())
```

Add both to the returned dict. Add the imports this needs at the top of the file: `from collections import Counter`, `from src.agent.specialists import SPECIALIST_DOMAINS`, `from src.services.blackbird_rubric import RUBRIC_WEIGHTS`. Use whatever local name the function already binds the fetched rows to in place of `assessments`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv-test/bin/python -m pytest tests/unit/test_directory_assessments.py -v`
Expected: all pass.

- [ ] **Step 5: Render the distribution**

In `templates/admin/_assessments_body.html`, below the summary cards and above the verdict table:

```html
{# Per-dimension distribution. A dimension whose max never rises is not
   discriminating, and at 15% weight that is worth seeing: across run
   1787010946 every one of 18 verdicts banded 'pass' and four dimensions
   never exceeded 2. `specialist` names who to ask when one scores badly. #}
{% if dimension_stats %}
<details class="mb-6 rounded-xl border border-gray-200 bg-white p-4">
    <summary class="cursor-pointer text-sm font-semibold text-gray-700">
        Dimension distribution
        {% if band_counts %}
        <span class="ml-2 font-normal text-gray-400">
            bands: {% for b, n in band_counts %}{{ b }}&times;{{ n }}{{ ", " if not loop.last }}{% endfor %}
        </span>
        {% endif %}
    </summary>
    <table class="mt-3 w-full text-xs">
        <thead class="text-gray-400 text-left">
            <tr><th>Dimension</th><th>Wt</th><th>Specialist</th><th>n</th><th>Mean</th><th>Min</th><th>Max</th></tr>
        </thead>
        <tbody>
        {% for d in dimension_stats %}
            <tr class="border-t border-gray-100 {% if d.max is not none and d.max <= 2 %}bg-amber-50{% endif %}">
                <td class="py-1 font-mono">{{ d.dimension }}</td>
                <td>{{ d.weight }}</td>
                <td class="text-gray-500">{{ d.specialist or "&mdash;"|safe }}</td>
                <td>{{ d.n }}</td>
                <td>{{ d.mean if d.mean is not none else "&mdash;"|safe }}</td>
                <td>{{ d.min if d.min is not none else "&mdash;"|safe }}</td>
                <td>{{ d.max if d.max is not none else "&mdash;"|safe }}</td>
            </tr>
        {% endfor %}
        </tbody>
    </table>
</details>
{% endif %}
```

- [ ] **Step 6: Pass the new keys from both routers**

Add `dimension_stats=view["dimension_stats"],` and `band_counts=view["band_counts"],` to the template context in `src/routers/admin.py`'s `admin_assessments` and in the manager assessments route.

- [ ] **Step 7: Add the clear-rate monitor**

The signal is known in `_execute_consult_specialist` but not at `_record_consult`, so
`on_consult` grows a second parameter. **`_record_consult`'s signature does NOT change** —
Task 6 fixed it at `(pi_agent_id, domain, thread_id=None)` and Task 3's tests call it with
two arguments. A new thin method does the tallying instead.

In `src/agent/simulation.py`, beside `self._specialist_consults` in `__init__`:

```python
        # verdict_signal -> count, for the whole run. The panel returned caution
        # or blocking on 142/142 consults in run 1787010946 and never once
        # cleared anything; a signal with no variance carries no information,
        # and it took an audit to notice. Tallied so the run says so itself.
        self._consult_signal_counts: dict[str, int] = {}
```

Add this method directly below `_record_consult`:

```python
    def _note_consult(
        self, pi_agent_id: str, domain: str, signal: str, thread_id: str | None = None,
    ) -> None:
        """Record a consult AND tally its signal.

        Two concerns, deliberately kept apart: `_record_consult` answers "does
        the floor consider this domain covered", which is per-interview; the
        tally answers "is this panel discriminating at all", which is per-run.
        """
        self._record_consult(pi_agent_id, domain, thread_id)
        self._consult_signal_counts[signal] = (
            self._consult_signal_counts.get(signal, 0) + 1
        )
```

In the run-summary path (find it with `grep -n "run summary\|_log_run_summary\|Run complete" src/agent/simulation.py`):

```python
        total = sum(self._consult_signal_counts.values())
        if total >= 50 and not self._consult_signal_counts.get("clear"):
            logger.warning(
                "[specialists] %d consults this run and NOT ONE returned "
                "'clear'. A panel that never clears anything cannot "
                "discriminate — check persona calibration.",
                total,
            )
```

- [ ] **Step 8: Widen the `on_consult` callback to carry the signal**

In `src/agent/tools.py`, change the `on_consult` parameter type on BOTH `execute_tool` and
`_execute_consult_specialist` from `Callable[[str], None] | None` to
`Callable[[str, str], None] | None`, and change the call Task 7 left as `on_consult(domain)` to:

```python
    if on_consult is not None:
        on_consult(domain, opinion.verdict_signal)
```

Update `execute_tool`'s docstring line about `on_consult` to say it receives the domain
**and** the parsed verdict signal, and fires only on a fully successful consult.

Then in `src/agent/simulation.py:1627`, point the closure at the new method:

```python
                on_consult=lambda domain, signal, _pi=thread.other_agent_id, _t=thread.thread_id: (
                    self._note_consult(_pi, domain, signal, _t)
                ),
```

Finally, update the test Task 7 added — `on_consult=consulted.append` now receives two
arguments and will raise `TypeError`:

```python
    on_consult=lambda domain, signal: consulted.append(domain),
```

Do the same for any other test in `tests/unit/test_consult_accounting.py` or
`tests/unit/test_specialist_floor.py` that passes a one-argument `on_consult`. Find them with:

```bash
grep -rn "on_consult" tests/
```

- [ ] **Step 9: Run the full gate**

Run: `./scripts/ci.sh`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add src/services/directory.py templates/admin/_assessments_body.html src/routers/ src/agent/simulation.py src/agent/tools.py tests/unit/ 
git commit -m "feat(admin): surface the panel and rubric failures the audit had to dig for

Adds a per-dimension distribution (four dimensions never exceeded 2
across 18 assessments), a band histogram (one bar), and a clear-rate
monitor that warns when 50+ consults produce no 'clear' at all.

maps_to_dimension gets its first runtime read: it names which specialist
to ask when a dimension is scoring badly."
```

---

## Deployment

Migrate before the new code serves — the model maps the new columns from Task 2 on, so new code against a pre-0029 database raises `UndefinedColumn` on every `select(OpportunityAssessment)`.

```bash
DC="docker compose -f docker-compose.prod.yml"     # never bare `docker compose`
$DC build blackbird-app worker
$DC run --rm blackbird-app alembic upgrade head
$DC run --rm blackbird-app alembic current          # must equal `alembic heads`
$DC up -d blackbird-app worker
$DC --profile agent build agent                     # src/ is baked in; not optional
```

Never pass `--remove-orphans`. The simulation container is currently `Exited (137)`; restarting it is an operator decision, not part of this work.

## Verification after deploy

```bash
# The freeze held:
git diff --stat main..HEAD -- prompts/ | grep -v specialists || echo "FREEZE OK"

# The floor now flags rather than discards:
$DC exec -T postgres psql -U copi -d copi -c \
  "SELECT panel_incomplete, count(*) FROM opportunity_assessments GROUP BY 1;"

# No new specialist_floor drops should appear after this deploy:
$DC exec -T postgres psql -U copi -d copi -c \
  "SELECT reason, count(*), max(created_at) FROM assessment_drops GROUP BY 1;"
```
