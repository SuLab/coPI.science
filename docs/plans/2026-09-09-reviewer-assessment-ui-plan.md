# Reviewer-Facing Assessment UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make BlackbirdBot's screening verdicts reviewable by a human — a
story headline, bullet summary and elevator pitch on every new assessment, a
card-based reading layout on both assessment surfaces, per-rubric-dimension
human scoring, and a visible explanation of why review controls disappear
under impersonation.

**Architecture:** Six additive nullable columns across two tables (migration
`0043`); three new keys in the scouting hub's `<assessment_json>` sidecar
contract, written by the existing `_persist_assessment` path; a rubric-driven
scoring form rendered from `blackbird_rubric.load_rubric()` so it can never
drift from the document; and a rewrite of the shared assessment-list body from
a table to a card list. No permission predicate changes.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy 2.x async, Alembic,
Jinja2 + Tailwind (CDN classes), Postgres 15, pytest + pytest-asyncio +
testcontainers.

**Spec:** `docs/plans/2026-09-09-reviewer-assessment-ui-design.md` — read it
first. Decision IDs `N1`–`N12` and adversarial findings `A1`–`A20` referenced
below are defined there.

## Global Constraints

Copied verbatim from the spec and `CLAUDE.md`. Every task's requirements
implicitly include this section.

- **Run pytest on the HOST, never in a container:**
  `.venv-test/bin/python -m pytest tests/ -v`. With `TEST_DATABASE_URL` unset,
  `tests/conftest.py` spins its own ephemeral Postgres via testcontainers.
- **Never run `pip install` / `uv pip install` against `.venv-test` from an
  sshfs-mounted client.** It rewrites console-script shebangs to a path that
  does not exist on the host and every DB-backed test then fails with a bare
  `FileNotFoundError`.
- **The full gate is `./scripts/ci.sh`.** There is no server-side CI. Run it
  before the final push, not after every task (it creates and destroys a
  throwaway Postgres).
- **Never `git checkout`, `git stash` or `git restore` `docker-compose.prod.yml`.**
  Its uncommitted working-tree edits are what keep this stack from colliding
  with the unrelated `copi-python` deployment on the same host. It will show as
  `M` in `git status` for the whole of this work; leave it alone and never
  `git add -A` from the repo root.
- **Never run `pytest --snapshot-update`.** `tests/characterization/__snapshots__/test_agent_turn_gm.ambr`
  pins `src/agent/thread_guidance.py`; this plan does not touch that file.
- **Every new JSONB column must be `JSONB(none_as_null=True)`.** Without it
  Python `None` persists as the JSONB scalar `null`, a second encoding of
  "absent" that `WHERE col IS NULL` does not match. That bug has shipped twice
  on this schema already. `tests/unit/test_json_none_as_null.py` walks
  `Base.metadata` and is the standing alarm.
- **Migration head is `0042`.** The new revision is `0043`, `down_revision = "0042"`.
- **The rubric document is NOT edited.** `prompts/rubric/blackbird-rubric.toml`
  `[meta].version` stays `3.4.0`. No weight, threshold, dimension, gating key
  or band semantic moves.
- **`prompts/roles/scout_hub/role.toml` `version` goes `1.0.0` → `1.1.0`** in
  the task that edits any file in that directory (Task 7), and
  `scripts/sync_prompt_set_docs.py` runs in that same commit.
- **Do not add `ruff` findings to `src/`.** `scripts/ci.sh` lints `src/`
  against a ceiling and `tests/` against zero.

---

## File Structure

| File | Task | Responsibility |
|---|---|---|
| `templates/admin/_assessment_detail_body.html` | 1, 5, 10 | Shared detail body: impersonation notice, dimension-scoring form, brief-first layout |
| `tests/integration/test_assessment_review_ui.py` | 1, 5 | Rendered HTML of the Human-review card |
| `tests/integration/test_reviews_router.py` | 1, 4 | `/reviews` POST gates and form parsing |
| `alembic/versions/0043_assessment_narrative_and_review_dimension_scores.py` | 2 | The six additive nullable columns |
| `src/models/opportunity.py` | 2 | `headline`, `key_points`, `elevator_pitch` mappings |
| `src/models/review.py` | 2 | `dimension_scores`, `rubric_version`, `rubric_content_hash` mappings |
| `tests/integration/test_assessment_narrative_fields.py` | 2, 8 | Narrative columns: round-trip, then engine write path |
| `src/services/assessment_reviews.py` | 3 | Dimension-score validation, normalization, rubric stamping |
| `tests/integration/test_review_dimension_scores.py` | 3, 4, 6 | The dimension-score feature end to end |
| `src/routers/reviews.py` | 4 | `dim_<key>` form parsing |
| `src/services/assessment_detail.py` | 5 | `review_rubric` context key + stored-score title resolution |
| `src/services/review_bot.py` | 6 | Snapshot + content-conditional `consumed_at` (A1) |
| `tests/unit/test_review_bot.py` | 6 | Snapshot shape |
| `prompts/roles/scout_hub/phase4-thread-reply.md` | 7 | Sidecar contract: three new fields + prose rules |
| `prompts/roles/scout_hub/role.toml` | 7 | Prompt-set version bump |
| `docs/specs/2026-08-07-hub-bot-prompts.md` | 7 | Regenerated by `scripts/sync_prompt_set_docs.py` |
| `src/agent/simulation.py` | 8 | `_persist_assessment` writes the three narrative columns |
| `templates/admin/_assessments_body.html` | 9 | Shared list body: table → card list |
| `tests/integration/test_opportunity_assessment_persistence.py` | 9 | Scan-only pin, narrowed (A11) |
| `src/services/assessment_headline.py` | 11 | Sixth Slack field |
| `CLAUDE.md` | 11, 12 | D12 field list; `0043` deploy box |
| `tests/unit/test_claude_md_disclosure_sync.py` | 11 | Disclosure drift alarm |

**Import boundary, verified 2026-09-09 — do not break it.** Task 3 adds
`from src.services.blackbird_rubric import ...` to
`src/services/assessment_reviews.py`. That is safe *because* the worker never
imports that module: `assessment_reviews` is imported only by
`src/routers/reviews.py` and `src/services/directory.py`, both web tier
(`grep -rn 'assessment_reviews' src/`). `src/services/review_bot.py` — the
worker's job handler — must stay free of `blackbird_rubric`,
`rubric_revisions` and `assessment_detail`; Task 6 adds no imports at all.

---

### Task 1: Impersonation notice on the Human-review card

Implements spec §1. **No permission code changes.** Most of §1.2's tests
already exist and must not be duplicated:
`test_reviewer_can_submit_feedback_and_learn_enqueues_one_deduped_job`,
`test_reviewer_cannot_assign_but_manager_can` and
`test_reviewer_can_approve_and_history_appends` (all in
`tests/integration/test_reviews_router.py`). The gap is a manager submitting
feedback, and the notice itself.

**Files:**
- Modify: `templates/admin/_assessment_detail_body.html` (Human-review card, after the intro `<p>` that ends `...flag anything the assessment got wrong.`)
- Test: `tests/integration/test_assessment_review_ui.py:267` (`test_impersonating_admin_sees_read_only_card`)
- Test: `tests/integration/test_reviews_router.py` (append)

**Interfaces:**
- Consumes: `impersonation_banner` and `can_write` — already in scope in that template (`can_write` is set at line 436 as `not impersonation_banner`).
- Produces: CSS hook `review-impersonation-notice`, asserted by later tasks' UI tests.

- [ ] **Step 1: Write the failing tests**

Extend the existing test in `tests/integration/test_assessment_review_ui.py`
(replace the whole function):

```python
async def test_impersonating_admin_sees_read_only_card(client, db_session, admin, manager):
    """The write half stays suppressed under impersonation (F6) — but it now
    SAYS so. The silent blank was read as "managers and reviewers have no
    permission to review at all", which is what prompted the 2026-09-09 change
    (design doc §0.1); every /manager/assessments/{id} view in the ten days
    before that report was made while impersonating."""
    assessment = await _seed_assessment(db_session)
    headers = auth_headers(admin.id)
    headers["Cookie"] += f"; copi-impersonate={manager.id}"

    resp = await client.get(f"/manager/assessments/{assessment.id}", headers=headers)
    assert resp.status_code == 200
    html = resp.text
    assert "Human review" in html
    assert 'action="/reviews/assessments/' not in html
    assert 'action="/reviews/feedback/' not in html
    # The notice is the point: an absence must explain itself.
    assert "review-impersonation-notice" in html
    assert "stop impersonating to review as yourself" in html


async def test_a_real_manager_sees_no_impersonation_notice(client, db_session, manager):
    """Control. The notice must appear ONLY under impersonation — a manager
    signed in as themselves has the full write half and needs no explanation."""
    assessment = await _seed_assessment(db_session)
    resp = await client.get(
        f"/manager/assessments/{assessment.id}", headers=auth_headers(manager.id)
    )
    assert resp.status_code == 200
    assert "review-impersonation-notice" not in resp.text
    assert 'action="/reviews/assessments/' in resp.text
```

Append to `tests/integration/test_reviews_router.py`:

```python
async def test_a_manager_can_submit_feedback(client, db_session):
    """The manager half of the review gate. The reviewer half is covered by
    test_reviewer_can_submit_feedback_and_learn_enqueues_one_deduped_job above;
    nothing covered a manager, which is the role the 2026-09-09 "cannot add
    reviews" report was actually about."""
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    assessment = await _seed_assessment(db_session)

    resp = await client.post(
        f"/reviews/assessments/{assessment.id}/feedback",
        data={"score": "3", "comment": "manager review", "feedback_mode": "log_only"},
        headers=auth_headers(manager.id),
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.text

    row = (
        await db_session.execute(
            select(AssessmentReview).where(
                AssessmentReview.assessment_id == assessment.id
            )
        )
    ).scalar_one()
    assert row.reviewer_user_id == manager.id
    assert row.score == 3
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv-test/bin/python -m pytest \
  tests/integration/test_assessment_review_ui.py::test_impersonating_admin_sees_read_only_card \
  tests/integration/test_assessment_review_ui.py::test_a_real_manager_sees_no_impersonation_notice \
  tests/integration/test_reviews_router.py::test_a_manager_can_submit_feedback -v
```

Expected: the two impersonation tests FAIL on the missing
`review-impersonation-notice` string. `test_a_manager_can_submit_feedback`
is expected to **PASS already** — it documents behaviour that exists. If it
fails, stop: that is a real permissions defect and the spec's §0.1 finding is
wrong.

- [ ] **Step 3: Add the notice to the template**

In `templates/admin/_assessment_detail_body.html`, immediately after the
intro paragraph that ends `...flag anything the assessment got wrong.</p>`
and before the `{# --- Status:` comment block:

```jinja
    {# N1. `can_write` suppresses the entire write half under impersonation
       (F6: a live-looking control that 403s on submit is worse than none —
       every /reviews handler is behind `_refuse_impersonation`). That is
       correct and stays. What was wrong was doing it SILENTLY: the blank card
       reads as "this role cannot review", and that is exactly how it was read
       (design doc §0.1). Say what happened and how to undo it. #}
    {% if not can_write %}
    <p class="review-impersonation-notice mb-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
        Review actions are hidden while you are impersonating another account.
        A review is recorded against the person who wrote it, so stop
        impersonating to review as yourself.
    </p>
    {% endif %}
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv-test/bin/python -m pytest tests/integration/test_assessment_review_ui.py tests/integration/test_reviews_router.py -v
```

Expected: all PASS, including the pre-existing tests in both files.

- [ ] **Step 5: Commit**

```bash
git add templates/admin/_assessment_detail_body.html \
        tests/integration/test_assessment_review_ui.py \
        tests/integration/test_reviews_router.py
git commit -m "fix(reviews): explain why review controls vanish under impersonation

The write half of the Human-review card is suppressed by \`can_write\` because
every /reviews handler refuses an impersonated session. It did so silently,
and the blank card was read as managers and reviewers having no permission to
review at all. The gates themselves were already correct.

Adds the notice and a control test, plus the missing manager-can-submit gate
test (the reviewer equivalent already existed).

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Migration 0043 and the six column mappings

Implements spec §2.1, §3.1 and §7. Schema only — nothing reads or writes the
columns yet, so this task is independently deployable and independently
revertable.

**Files:**
- Create: `alembic/versions/0043_assessment_narrative_and_review_dimension_scores.py`
- Modify: `src/models/opportunity.py` (after `company_or_project`, line 57)
- Modify: `src/models/review.py` (after `score`, line 84)
- Create: `tests/integration/test_assessment_narrative_fields.py`
- Test: `tests/integration/test_review_models.py` (append)

**Interfaces:**
- Produces: `OpportunityAssessment.headline: str | None`,
  `.key_points: list | None`, `.elevator_pitch: str | None`;
  `AssessmentReview.dimension_scores: dict | None`,
  `.rubric_version: str | None`, `.rubric_content_hash: str | None`.
  Tasks 3, 5, 6, 8, 9, 10 and 11 all rely on these exact names.

- [ ] **Step 1: Write the failing tests**

Create `tests/integration/test_assessment_narrative_fields.py`:

```python
"""The three reviewer-facing narrative fields on an assessment (migration 0043).

Task 2 covers the columns themselves; Task 8 extends this file with the engine
write path that fills them from the sidecar.
"""

import pytest
from sqlalchemy import select

from src.models import OpportunityAssessment, SimulationRun

pytestmark = pytest.mark.integration


async def _seed_run(db):
    run = SimulationRun()
    db.add(run)
    await db.flush()
    return run


async def test_narrative_fields_round_trip(db_session):
    run = await _seed_run(db_session)
    row = OpportunityAssessment(
        simulation_run_id=run.id,
        agent_id="blackbird",
        channel_name="general",
        company_or_project="Short label",
        headline="A blood test that says who responds to immunotherapy in liver cancer.",
        key_points=["The classifier does not exist yet", "Circadian confound unmeasured"],
        elevator_pitch="Hopkins has 39-plex cytokine data on 124 patients.",
    )
    db_session.add(row)
    await db_session.flush()
    db_session.expunge(row)

    stored = (
        await db_session.execute(
            select(OpportunityAssessment).where(OpportunityAssessment.id == row.id)
        )
    ).scalar_one()
    assert stored.headline.startswith("A blood test")
    assert stored.key_points == [
        "The classifier does not exist yet",
        "Circadian confound unmeasured",
    ]
    assert stored.elevator_pitch.startswith("Hopkins has")


async def test_narrative_fields_default_to_sql_null_not_json_null(db_session):
    """`key_points` is JSONB, and `none_as_null=True` is what keeps Python None
    a real SQL NULL. Without it, `WHERE key_points IS NULL` misses the row —
    the exact defect 0031 and 0036 each had to repair once (A14)."""
    run = await _seed_run(db_session)
    row = OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", channel_name="general",
        key_points=None,
    )
    db_session.add(row)
    await db_session.flush()

    found = (
        await db_session.execute(
            select(OpportunityAssessment.id).where(
                OpportunityAssessment.id == row.id,
                OpportunityAssessment.key_points.is_(None),
            )
        )
    ).scalar_one_or_none()
    assert found == row.id
```

Append to `tests/integration/test_review_models.py`:

```python
async def test_review_dimension_scores_and_rubric_stamp_round_trip(db_session):
    """A per-dimension human score is uninterpretable without knowing which
    dimensions existed when it was given, so the review carries its own rubric
    stamp — the same pair specialist_consults got in 0038, for the same reason
    (design §2.1)."""
    from sqlalchemy import select

    from src.models import AssessmentReview, OpportunityAssessment, SimulationRun

    run = SimulationRun()
    db_session.add(run)
    await db_session.flush()
    assessment = OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", channel_name="general"
    )
    db_session.add(assessment)
    await db_session.flush()

    review = AssessmentReview(
        assessment_id=assessment.id,
        reviewer_name="Someone",
        score=4,
        comment="",
        feedback_mode="log_only",
        dimension_scores={"scientific_credibility": 4, "team_executability": 2},
        rubric_version="3.4.0",
        rubric_content_hash="abc123def456",
    )
    db_session.add(review)
    await db_session.flush()
    db_session.expunge(review)

    stored = (
        await db_session.execute(
            select(AssessmentReview).where(AssessmentReview.id == review.id)
        )
    ).scalar_one()
    assert stored.dimension_scores == {
        "scientific_credibility": 4,
        "team_executability": 2,
    }
    assert stored.rubric_version == "3.4.0"
    assert stored.rubric_content_hash == "abc123def456"


async def test_review_dimension_scores_absent_is_sql_null(db_session):
    from sqlalchemy import select

    from src.models import AssessmentReview, OpportunityAssessment, SimulationRun

    run = SimulationRun()
    db_session.add(run)
    await db_session.flush()
    assessment = OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", channel_name="general"
    )
    db_session.add(assessment)
    await db_session.flush()
    review = AssessmentReview(
        assessment_id=assessment.id, reviewer_name="Someone", score=4,
        comment="", feedback_mode="log_only", dimension_scores=None,
    )
    db_session.add(review)
    await db_session.flush()

    found = (
        await db_session.execute(
            select(AssessmentReview.id).where(
                AssessmentReview.id == review.id,
                AssessmentReview.dimension_scores.is_(None),
            )
        )
    ).scalar_one_or_none()
    assert found == review.id
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv-test/bin/python -m pytest tests/integration/test_assessment_narrative_fields.py tests/integration/test_review_models.py -v
```

Expected: FAIL — `TypeError: 'headline' is an invalid keyword argument for OpportunityAssessment`.

- [ ] **Step 3: Write the migration**

Create `alembic/versions/0043_assessment_narrative_and_review_dimension_scores.py`:

```python
"""assessment narrative fields + per-dimension human review scores

Six additive nullable columns across two tables. No DDL rewrites, no FK
changes, no data repair, no backfill.

  opportunity_assessments : headline, key_points, elevator_pitch
  assessment_reviews      : dimension_scores, rubric_version, rubric_content_hash

Additive and nullable, so OLD CODE AGAINST THE NEW SCHEMA IS SAFE. The reverse
is not, in both directions at once. READ side: the new code MAPS all six, so
every `select(OpportunityAssessment)` (both list pages, both detail pages) and
every `select(AssessmentReview)` (the detail pages' feedback list, review_bot's
own load) raises `UndefinedColumn` against a pre-0043 database. WRITE side:
`_persist_assessment`'s INSERT names headline/key_points/elevator_pitch, so
EVERY verdict write of a running simulation fails — and that write is
best-effort, so the failure is swallowed and one ERROR line is logged while the
Slack replies keep looking completely normal. Migrate BEFORE the new code
serves; see the 0043 deploy box in CLAUDE.md.

Deliberately NOT backfilled. All three narrative fields are NULL on every
pre-0043 assessment row, because those verdicts were never asked for them and a
generated headline would be indistinguishable from one the hub actually wrote —
the same stamp-and-keep rule the rest of this table follows. Every read path
degrades: `headline` falls back to `company_or_project`, and absent
bullets/pitch render nothing.

Revision ID: 0043
Revises: 0042
Create Date: 2026-09-09
"""
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0043"
down_revision: Union[str, None] = "0042"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column(
        "opportunity_assessments", sa.Column("headline", sa.Text(), nullable=True)
    )
    op.add_column(
        "opportunity_assessments",
        sa.Column("key_points", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "opportunity_assessments", sa.Column("elevator_pitch", sa.Text(), nullable=True)
    )
    op.add_column(
        "assessment_reviews",
        sa.Column(
            "dimension_scores", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
    )
    op.add_column(
        "assessment_reviews", sa.Column("rubric_version", sa.String(20), nullable=True)
    )
    op.add_column(
        "assessment_reviews",
        sa.Column("rubric_content_hash", sa.String(20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("assessment_reviews", "rubric_content_hash")
    op.drop_column("assessment_reviews", "rubric_version")
    op.drop_column("assessment_reviews", "dimension_scores")
    op.drop_column("opportunity_assessments", "elevator_pitch")
    op.drop_column("opportunity_assessments", "key_points")
    op.drop_column("opportunity_assessments", "headline")
```

- [ ] **Step 4: Add the mappings**

In `src/models/opportunity.py`, immediately after the `company_or_project`
line (currently line 57):

```python
    # The three reviewer-facing narrative fields (sidecar items 11-13, 0043).
    # `company_or_project` above stays the SHORT label and keeps its current
    # meaning — it is the only project field the public #assessments-summary
    # headline renders, so widening it would change what is posted to Slack.
    #
    # NULL on every row written before 0043 and deliberately never backfilled:
    # those verdicts were not asked for a headline, and a generated one would be
    # indistinguishable from one the hub wrote. Every read path falls back to
    # `company_or_project` and renders nothing for absent bullets/pitch.
    headline: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 3-5 short strings. `none_as_null=True` for the reason given on
    # `missing_domains` below: without it Python None persists as the JSONB
    # scalar `null`, which `WHERE key_points IS NULL` does not match.
    key_points: Mapped[list | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    elevator_pitch: Mapped[str | None] = mapped_column(Text, nullable=True)
```

In `src/models/review.py`, immediately after the `score` line (currently
line 84):

```python
    #: Sparse per-rubric-dimension human scores, `{dimension_key: int}` on the
    #: live rubric's scale — only the dimensions this reviewer actually filled
    #: in. `{}` and NULL are ONE state ("scored no dimensions") and normalize to
    #: NULL at the service layer, so the column has a single encoding of
    #: absence. `none_as_null=True` is what makes that encoding a real SQL NULL.
    dimension_scores: Mapped[dict | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    #: WHICH rubric the human scored against. Not decoration: a per-dimension
    #: score is uninterpretable without knowing which dimensions existed when it
    #: was given (v3.0.0 replaced thirteen dual-scale dimensions with six
    #: single-scale ones). Reading a stored score back against TODAY's document
    #: is the same defect `OpportunityAssessment.panel_owed` exists to end — a
    #: write-time fact answered at render time. `specialist_consults` got this
    #: same pair in 0038 for this same reason. NULL on every pre-0043 row.
    rubric_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    rubric_content_hash: Mapped[str | None] = mapped_column(String(20), nullable=True)
```

`src/models/review.py` already imports `String` and `JSONB`; no import changes
are needed there. `src/models/opportunity.py` already imports `Text` and
`JSONB`; likewise.

- [ ] **Step 5: Run the tests to verify they pass**

```bash
.venv-test/bin/python -m pytest \
  tests/integration/test_assessment_narrative_fields.py \
  tests/integration/test_review_models.py \
  tests/unit/test_json_none_as_null.py -v
```

Expected: all PASS. `test_json_none_as_null` walks `Base.metadata` and would
fail if either new JSONB column omitted `none_as_null=True`.

- [ ] **Step 6: Verify the migration is a single head and round-trips**

```bash
.venv-test/bin/python -m alembic heads
```

Expected: exactly one line, ending `0043 (head)`.

- [ ] **Step 7: Commit**

```bash
git add alembic/versions/0043_assessment_narrative_and_review_dimension_scores.py \
        src/models/opportunity.py src/models/review.py \
        tests/integration/test_assessment_narrative_fields.py \
        tests/integration/test_review_models.py
git commit -m "feat(db): 0043 — assessment narrative fields + review dimension scores

Six additive nullable columns. opportunity_assessments gains headline,
key_points and elevator_pitch; assessment_reviews gains dimension_scores plus
the rubric_version/rubric_content_hash stamp that makes a per-dimension human
score interpretable across revisions.

Nothing reads or writes them yet. Migrate before the new code serves — see the
migration docstring.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Dimension-score validation, normalization and stamping

Implements spec §2.2. Service layer only — no HTTP, no template.

**Files:**
- Modify: `src/services/assessment_reviews.py` (imports; `_validate` at line 79; `submit_feedback` at 128; `edit_feedback` at 161)
- Create: `tests/integration/test_review_dimension_scores.py`

**Interfaces:**
- Consumes: `AssessmentReview.dimension_scores` / `.rubric_version` / `.rubric_content_hash` (Task 2).
- Produces:
  - `submit_feedback(db, *, assessment, reviewer, score, comment, feedback_mode, dimension_scores: dict[str, int] | None = None) -> AssessmentReview`
  - `edit_feedback(db, *, review, score, comment, feedback_mode, dimension_scores: dict[str, int] | None = None) -> AssessmentReview`
  - `_normalized_dimension_scores(dimension_scores: dict[str, int] | None) -> dict[str, int] | None`
  Task 4 calls both public functions with the new keyword.

- [ ] **Step 1: Write the failing tests**

Create `tests/integration/test_review_dimension_scores.py`:

```python
"""Per-rubric-dimension human scores on a review (design §2.2).

Optional and sparse by decision N3: a reviewer may score any subset of the six
dimensions, or none. The required overall "proposal merit" score is unchanged.
"""

import pytest
from sqlalchemy import select

from src.models import USER_ROLE_REVIEWER, AssessmentReview
from src.services.assessment_reviews import edit_feedback, submit_feedback
from src.services.blackbird_rubric import (
    RUBRIC_CONTENT_HASH,
    RUBRIC_VERSION,
    load_rubric,
)
from tests import factories
from tests.integration.test_manager_access import auth_headers
from tests.integration.test_reviews_router import _seed_assessment

pytestmark = pytest.mark.integration


def _first_two_dimension_keys() -> tuple[str, str]:
    """Read the keys from the document rather than hard-coding them: the six
    keys are a rubric-version fact, and a test that pins them here would fail
    for the wrong reason on the next consolidation."""
    dims = load_rubric().dimensions
    return dims[0].key, dims[1].key


async def test_a_sparse_dimension_set_is_stored_and_stamped(client, db_session):
    key_a, key_b = _first_two_dimension_keys()
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)

    review = await submit_feedback(
        db_session,
        assessment=assessment,
        reviewer=reviewer,
        score=4,
        comment="",
        feedback_mode="log_only",
        dimension_scores={key_a: 5, key_b: 2},
    )
    await db_session.flush()

    assert review.dimension_scores == {key_a: 5, key_b: 2}
    assert review.rubric_version == RUBRIC_VERSION
    assert review.rubric_content_hash == RUBRIC_CONTENT_HASH
    assert review.score == 4  # the overall merit score is untouched


async def test_no_dimension_scores_stores_sql_null_not_an_empty_dict(client, db_session):
    """`{}` and None are one state. Storing both would give the column two
    encodings of absence, which is the JSONB defect 0031 and 0036 each fixed."""
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)

    review = await submit_feedback(
        db_session, assessment=assessment, reviewer=reviewer, score=3,
        comment="", feedback_mode="log_only", dimension_scores={},
    )
    await db_session.flush()
    assert review.dimension_scores is None

    found = (
        await db_session.execute(
            select(AssessmentReview.id).where(
                AssessmentReview.id == review.id,
                AssessmentReview.dimension_scores.is_(None),
            )
        )
    ).scalar_one_or_none()
    assert found == review.id


async def test_an_unknown_dimension_key_is_rejected(client, db_session):
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)

    with pytest.raises(ValueError, match="unknown rubric dimension"):
        await submit_feedback(
            db_session, assessment=assessment, reviewer=reviewer, score=3,
            comment="", feedback_mode="log_only",
            dimension_scores={"funnel_stage_vibes": 4},
        )


@pytest.mark.parametrize("bad", [0, 6, -1, 99])
async def test_an_out_of_scale_dimension_score_is_rejected(client, db_session, bad):
    key_a, _ = _first_two_dimension_keys()
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)

    with pytest.raises(ValueError, match="must be between"):
        await submit_feedback(
            db_session, assessment=assessment, reviewer=reviewer, score=3,
            comment="", feedback_mode="log_only", dimension_scores={key_a: bad},
        )


async def test_a_boolean_is_not_an_integer_score(client, db_session):
    """`bool` subclasses `int`, so `True` passes a naive 1 <= v <= 5. A form
    that ever posts a checkbox into one of these fields must fail, not record
    a 1 nobody chose."""
    key_a, _ = _first_two_dimension_keys()
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)

    with pytest.raises(ValueError, match="must be an integer"):
        await submit_feedback(
            db_session, assessment=assessment, reviewer=reviewer, score=3,
            comment="", feedback_mode="log_only", dimension_scores={key_a: True},
        )


async def test_editing_replaces_the_dimension_set_and_restamps(client, db_session):
    """An edit is a re-entry of the whole set, not a merge: a dimension the
    reviewer cleared must actually clear. The stamp moves with it, because the
    scores being stored are the ones given under the CURRENT document."""
    key_a, key_b = _first_two_dimension_keys()
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)

    review = await submit_feedback(
        db_session, assessment=assessment, reviewer=reviewer, score=4,
        comment="", feedback_mode="log_only",
        dimension_scores={key_a: 5, key_b: 2},
    )
    await db_session.flush()

    await edit_feedback(
        db_session, review=review, score=4, comment="", feedback_mode="log_only",
        dimension_scores={key_b: 3},
    )
    await db_session.flush()

    assert review.dimension_scores == {key_b: 3}
    assert review.rubric_version == RUBRIC_VERSION
    assert review.edited is True
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv-test/bin/python -m pytest tests/integration/test_review_dimension_scores.py -v
```

Expected: FAIL — `submit_feedback() got an unexpected keyword argument 'dimension_scores'`.

- [ ] **Step 3: Implement the service changes**

Add to the imports at the top of `src/services/assessment_reviews.py`:

```python
from src.services.blackbird_rubric import (
    RUBRIC_CONTENT_HASH,
    RUBRIC_VERSION,
    load_rubric,
)
```

Import-boundary note for the module docstring (append a paragraph):

```
``blackbird_rubric`` is imported here for dimension-key validation. That is
safe because this module is web-tier only — it is imported by
``src/routers/reviews.py`` and ``src/services/directory.py`` and by nothing the
worker loads. ``src/services/review_bot.py``, which DOES run on the worker,
must stay free of ``blackbird_rubric``/``rubric_revisions``/``assessment_detail``;
see the probe in ``tests/unit/test_review_bot_inputs.py``.
```

Replace `_validate` (line 79) with:

```python
def _validate(
    score: int,
    feedback_mode: str,
    dimension_scores: dict[str, int] | None = None,
) -> None:
    if not (1 <= score <= 5):
        raise ValueError("score must be between 1 and 5")
    if feedback_mode not in VALID_FEEDBACK_MODES:
        raise ValueError(f"feedback_mode must be one of {VALID_FEEDBACK_MODES}")
    if not dimension_scores:
        return
    # Validated against the LIVE document, which is also what gets stamped on
    # the row — the two cannot disagree, because they are read in the same call.
    rubric = load_rubric()
    valid_keys = {d.key for d in rubric.dimensions}
    for key, value in dimension_scores.items():
        if key not in valid_keys:
            raise ValueError(f"unknown rubric dimension: {key}")
        # `bool` subclasses `int`, so `True` would sail through the range check
        # below and store as a 1 nobody chose. Reject it explicitly.
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"dimension score for {key} must be an integer")
        if not (rubric.scale_min <= value <= rubric.scale_max):
            raise ValueError(
                f"dimension score for {key} must be between "
                f"{rubric.scale_min} and {rubric.scale_max}"
            )


def _normalized_dimension_scores(
    dimension_scores: dict[str, int] | None,
) -> dict[str, int] | None:
    """`{}` and `None` are the same state — "scored no dimensions" — so they
    store as one value. Two encodings of absence on a JSONB column is the
    defect 0031 and 0036 each had to repair once already."""
    return dimension_scores or None
```

In `submit_feedback`, add the parameter and the three assignments:

```python
async def submit_feedback(
    db: AsyncSession,
    *,
    assessment: OpportunityAssessment,
    reviewer: User,
    score: int,
    comment: str,
    feedback_mode: str,
    dimension_scores: dict[str, int] | None = None,
) -> AssessmentReview:
    _validate(score, feedback_mode, dimension_scores)
    review = AssessmentReview(
        assessment_id=assessment.id,
        reviewer_user_id=reviewer.id,
        reviewer_name=reviewer.name,
        score=score,
        comment=comment[:_MAX_COMMENT_CHARS],
        feedback_mode=feedback_mode,
        dimension_scores=_normalized_dimension_scores(dimension_scores),
        # Stamped from the module-level constants, not re-read from disk: the
        # rubric cannot change under a running process, so this is the same
        # document `_validate` just checked the keys against.
        rubric_version=RUBRIC_VERSION,
        rubric_content_hash=RUBRIC_CONTENT_HASH,
    )
    db.add(review)
    await db.flush()
    if feedback_mode == "learn":
        await enqueue_analysis_if_absent(
            db, assessment_id=assessment.id, user_id=reviewer.id
        )
    return review
```

In `edit_feedback`, likewise — and note in its docstring that an edit
REPLACES the set and RE-STAMPS:

```python
async def edit_feedback(
    db: AsyncSession,
    *,
    review: AssessmentReview,
    score: int,
    comment: str,
    feedback_mode: str,
    dimension_scores: dict[str, int] | None = None,
) -> AssessmentReview:
    """Mutate an existing review in place. Caller commits.

    Author-only is the router's check, not this function's. Resets
    ``consumed_at`` to ``None`` so an edited row is picked back up by the
    next analysis job even if the original had already been consumed.

    ``dimension_scores`` REPLACES the stored set rather than merging into it —
    the form re-posts every dimension, so a field the reviewer cleared must
    actually clear. The rubric stamp is rewritten for the same reason: the
    scores now on the row are the ones given under the document live at edit
    time, which is not necessarily the one that scored the original.
    """
    _validate(score, feedback_mode, dimension_scores)
    review.score = score
    review.comment = comment[:_MAX_COMMENT_CHARS]
    review.feedback_mode = feedback_mode
    review.dimension_scores = _normalized_dimension_scores(dimension_scores)
    review.rubric_version = RUBRIC_VERSION
    review.rubric_content_hash = RUBRIC_CONTENT_HASH
    review.edited = True
    review.consumed_at = None
    if feedback_mode == "learn":
        await enqueue_analysis_if_absent(
            db, assessment_id=review.assessment_id, user_id=review.reviewer_user_id
        )
    return review
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv-test/bin/python -m pytest tests/integration/test_review_dimension_scores.py tests/integration/test_reviews_router.py -v
```

Expected: all PASS. `test_reviews_router.py` must stay green — every existing
caller omits the new keyword and gets `None`.

- [ ] **Step 5: Commit**

```bash
git add src/services/assessment_reviews.py tests/integration/test_review_dimension_scores.py
git commit -m "feat(reviews): per-rubric-dimension human scores in the service layer

submit_feedback/edit_feedback take an optional sparse {dimension_key: 1-5} map,
validated against the live rubric document and stamped with its version and
content hash. Empty normalizes to SQL NULL so the column has one encoding of
absence; bool is rejected explicitly because it subclasses int.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Parse `dim_<key>` fields in the reviews router

Implements spec §2.2's HTTP half.

**Files:**
- Modify: `src/routers/reviews.py` (`submit_review_feedback` at line 117, `edit_review_feedback` at 148)
- Test: `tests/integration/test_review_dimension_scores.py` (append)

**Interfaces:**
- Consumes: `submit_feedback(..., dimension_scores=...)` and `edit_feedback(..., dimension_scores=...)` (Task 3).
- Produces: form-field convention `dim_<dimension_key>`, consumed by the templates in Task 5.

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_review_dimension_scores.py` (`auth_headers`
is already imported in the header Task 3 wrote — do NOT add a second import at
the bottom of the file: `E402` is in this repo's ruff rule set and the test
suite is linted at **zero** findings):

```python
async def test_the_form_posts_dimension_scores_and_blanks_are_dropped(
    client, db_session
):
    """An unfilled select posts an empty string. It must be DROPPED, never
    coerced to 0 — the rubric scale starts at 1, so a stored 0 would be a
    score nobody gave (and would drag any future average)."""
    key_a, key_b = _first_two_dimension_keys()
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)

    resp = await client.post(
        f"/reviews/assessments/{assessment.id}/feedback",
        data={
            "score": "4",
            "comment": "c",
            "feedback_mode": "log_only",
            f"dim_{key_a}": "5",
            f"dim_{key_b}": "",
        },
        headers=auth_headers(reviewer.id),
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.text

    row = (
        await db_session.execute(
            select(AssessmentReview).where(
                AssessmentReview.assessment_id == assessment.id
            )
        )
    ).scalar_one()
    assert row.dimension_scores == {key_a: 5}


async def test_posting_no_dimension_fields_at_all_still_works(client, db_session):
    """Backwards compatibility with the pre-0043 form shape, and with any
    script that posts only the overall score."""
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)

    resp = await client.post(
        f"/reviews/assessments/{assessment.id}/feedback",
        data={"score": "4", "comment": "c", "feedback_mode": "log_only"},
        headers=auth_headers(reviewer.id),
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.text
    row = (
        await db_session.execute(
            select(AssessmentReview).where(
                AssessmentReview.assessment_id == assessment.id
            )
        )
    ).scalar_one()
    assert row.dimension_scores is None


async def test_an_unknown_dimension_field_is_a_400_and_writes_nothing(
    client, db_session
):
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)

    resp = await client.post(
        f"/reviews/assessments/{assessment.id}/feedback",
        data={
            "score": "4", "comment": "c", "feedback_mode": "log_only",
            "dim_not_a_real_dimension": "3",
        },
        headers=auth_headers(reviewer.id),
        follow_redirects=False,
    )
    assert resp.status_code == 400
    rows = (
        await db_session.execute(
            select(AssessmentReview).where(
                AssessmentReview.assessment_id == assessment.id
            )
        )
    ).scalars().all()
    assert rows == []


async def test_a_non_numeric_dimension_field_is_a_400(client, db_session):
    key_a, _ = _first_two_dimension_keys()
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)

    resp = await client.post(
        f"/reviews/assessments/{assessment.id}/feedback",
        data={
            "score": "4", "comment": "c", "feedback_mode": "log_only",
            f"dim_{key_a}": "excellent",
        },
        headers=auth_headers(reviewer.id),
        follow_redirects=False,
    )
    assert resp.status_code == 400
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv-test/bin/python -m pytest tests/integration/test_review_dimension_scores.py -v -k "form or posting or unknown_dimension_field or non_numeric"
```

Expected: FAIL — `dimension_scores` comes back `None` where `{key_a: 5}` is
expected, and the two malformed posts return 302 instead of 400.

- [ ] **Step 3: Implement the parser and wire both handlers**

In `src/routers/reviews.py`, add `Request` to the FastAPI import and add this
module-level constant and helper after `_parse_assignee_id`:

```python
#: Prefix for the per-rubric-dimension score fields the Human-review card
#: posts. One field per dimension, named from the rubric document's own key
#: (`dim_scientific_credibility`), so the form and the validator cannot drift.
_DIM_FIELD_PREFIX = "dim_"


def _parse_dimension_scores(form) -> dict[str, int]:
    """Pull the `dim_<key>` fields out of a posted form.

    An empty value means "not scored" and is DROPPED, never coerced to 0: the
    rubric scale starts at 1, so a stored 0 is a score nobody gave. A
    non-integer is a 400 rather than a silent skip — recording a review that
    quietly omits what the reviewer typed is worse than refusing it. Unknown
    KEYS are not checked here; `assessment_reviews._validate` owns that, against
    the live document.
    """
    scores: dict[str, int] = {}
    for field, raw in form.multi_items():
        if not field.startswith(_DIM_FIELD_PREFIX):
            continue
        value = (raw or "").strip()
        if not value:
            continue
        try:
            scores[field[len(_DIM_FIELD_PREFIX) :]] = int(value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=400, detail=f"Malformed dimension score for {field}"
            ) from exc
    return scores
```

Change `submit_review_feedback`'s signature to take `request: Request` as its
second parameter and parse the form before calling the service:

```python
@router.post("/assessments/{assessment_id}/feedback")
async def submit_review_feedback(
    assessment_id: uuid.UUID,
    request: Request,
    score: int = Form(...),
    comment: str = Form(""),
    feedback_mode: str = Form(...),
    surface: str = Form("manager"),
    db: AsyncSession = _DB,
    current_user: User = _REVIEW,
):
    _refuse_impersonation(current_user)
    assessment = await _load_assessment(db, assessment_id)
    dimension_scores = _parse_dimension_scores(await request.form())
    try:
        await submit_feedback(
            db,
            assessment=assessment,
            reviewer=current_user,
            score=score,
            comment=comment,
            feedback_mode=feedback_mode,
            dimension_scores=dimension_scores,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.commit()
    logger.info(
        "Review feedback by %s (%s) on assessment %s: score=%s mode=%s dims=%d",
        current_user.name, current_user.id, assessment_id, score, feedback_mode,
        len(dimension_scores),
    )
    return _assessments_redirect(surface, current_user, assessment_id)
```

Apply the same two changes to `edit_review_feedback` — add
`request: Request` after `feedback_id`, parse the form, and pass
`dimension_scores=dimension_scores` into `edit_feedback`. Extend its log line
the same way.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv-test/bin/python -m pytest tests/integration/test_review_dimension_scores.py tests/integration/test_reviews_router.py -v
```

Expected: all PASS, including `test_the_reviews_router_posts_are_an_explicit_allowlist`
(no route was added).

- [ ] **Step 5: Commit**

```bash
git add src/routers/reviews.py tests/integration/test_review_dimension_scores.py
git commit -m "feat(reviews): parse dim_<key> dimension-score fields on submit and edit

An unfilled select posts an empty string and is dropped, never coerced to 0 —
the rubric scale starts at 1. A non-integer or an unknown dimension key is a
400 that writes nothing, rather than a review that silently omits what the
reviewer entered.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Rubric-driven scoring form on the Human-review card

Implements spec §2.3. The form is rendered from the live rubric document, and
each stored review's scores are titled by the revision **that reviewer** scored
against.

**Files:**
- Modify: `src/services/assessment_detail.py` (imports at line 61; `_load_review_feedback` at 739; the return dict at 695)
- Modify: `templates/admin/_assessment_detail_body.html` (Human-review card)
- Test: `tests/integration/test_assessment_review_ui.py` (append)

**Interfaces:**
- Consumes: `AssessmentReview.dimension_scores` / `.rubric_version` / `.rubric_content_hash` (Task 2); form-field convention `dim_<key>` (Task 4).
- Produces: context key
  `review_rubric = {"version": str, "scale_min": int, "scale_max": int, "dimensions": [{"key": str, "title": str, "weight": int, "anchors": str, "bot_score": float | None}]}`,
  and, on each row of `review_feedback`, the attached attributes
  `dimension_rows: list[{"key": str, "title": str, "score": int}]` and
  `dimension_provenance: str`.
  Both assessment-detail routes splat `**detail`
  (`src/routers/admin.py:875`, `src/routers/manager.py:381`), so a new key here
  reaches both surfaces. **The list route does not splat — see Task 9's note (A2).**

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_assessment_review_ui.py`:

```python
async def test_the_add_form_renders_one_select_per_live_rubric_dimension(
    client, db_session, admin
):
    """Rendered from prompts/rubric/blackbird-rubric.toml, not from template
    literals: the form and the validator that stamps the score must read one
    document (design §2.3)."""
    from src.services.blackbird_rubric import load_rubric

    rubric = load_rubric()
    assessment = await _seed_assessment(db_session)

    html = (
        await client.get(
            f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
        )
    ).text

    assert "review-rubric-instructions" in html
    assert rubric.version in html
    for dim in rubric.dimensions:
        assert f'name="dim_{dim.key}"' in html, dim.key
        assert dim.title in html, dim.key


async def test_the_form_shows_the_bots_own_score_beside_each_dimension(
    client, db_session, admin
):
    """A9, accepted with the anchoring cost recorded: disagreement has to be
    visible at the point of scoring. It must read as the BOT's claim, never as
    a pre-filled default — so the human's own select stays empty."""
    from src.services.blackbird_rubric import load_rubric

    first = load_rubric().dimensions[0]
    assessment = await _seed_assessment(db_session)
    assessment.scores = {first.key: 4}
    await db_session.flush()

    html = (
        await client.get(
            f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
        )
    ).text

    assert "BlackbirdBot scored 4" in html
    # The human's select for that dimension must have nothing selected.
    assert f'name="dim_{first.key}"' in html
    assert 'value="4" selected' not in html


async def test_a_stored_review_renders_its_dimension_scores(
    client, db_session, admin, reviewer
):
    from src.services.blackbird_rubric import (
        RUBRIC_CONTENT_HASH,
        RUBRIC_VERSION,
        load_rubric,
    )

    first = load_rubric().dimensions[0]
    assessment = await _seed_assessment(db_session)
    db_session.add(
        AssessmentReview(
            assessment_id=assessment.id,
            reviewer_user_id=reviewer.id,
            reviewer_name=reviewer.name,
            score=4,
            comment="",
            feedback_mode="log_only",
            dimension_scores={first.key: 2},
            rubric_version=RUBRIC_VERSION,
            rubric_content_hash=RUBRIC_CONTENT_HASH,
            created_at=BASE_TIME,
            updated_at=BASE_TIME,
        )
    )
    await db_session.flush()

    html = (
        await client.get(
            f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
        )
    ).text
    assert "review-dimension-list" in html
    assert first.title in html


async def test_a_review_stamped_with_an_unknown_revision_shows_raw_keys(
    client, db_session, admin, reviewer
):
    """A13. Rubric v3.0.0 replaced thirteen dual-scale dimensions with six
    single-scale ones, so a key means nothing outside its own revision.
    Titling an unresolvable stamp from today's document would invent a claim;
    the keys render as stored instead, with a warning."""
    assessment = await _seed_assessment(db_session)
    db_session.add(
        AssessmentReview(
            assessment_id=assessment.id,
            reviewer_user_id=reviewer.id,
            reviewer_name=reviewer.name,
            score=3,
            comment="",
            feedback_mode="log_only",
            dimension_scores={"some_retired_dimension": 5},
            rubric_version="1.0.0-not-in-the-registry",
            rubric_content_hash="ffffffffffff",
            created_at=BASE_TIME,
            updated_at=BASE_TIME,
        )
    )
    await db_session.flush()

    html = (
        await client.get(
            f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
        )
    ).text
    assert "some_retired_dimension" in html
    assert "unrecognized rubric revision" in html


async def test_the_form_renders_for_a_reviewer_on_the_manager_surface(
    client, db_session, reviewer
):
    """The whole point of the feature: a reviewer, signed in as themselves,
    gets the scoring form on the only assessment surface they can reach."""
    from src.services.blackbird_rubric import load_rubric

    first = load_rubric().dimensions[0]
    assessment = await _seed_assessment(db_session)

    html = (
        await client.get(
            f"/manager/assessments/{assessment.id}", headers=auth_headers(reviewer.id)
        )
    ).text
    assert f'name="dim_{first.key}"' in html
    assert "review-rubric-instructions" in html
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv-test/bin/python -m pytest tests/integration/test_assessment_review_ui.py -v -k "rubric_dimension or bots_own_score or dimension_scores or unknown_revision or reviewer_on_the_manager"
```

Expected: FAIL — `review-rubric-instructions` is not in the HTML.

- [ ] **Step 3: Add the service context**

In `src/services/assessment_detail.py`, extend the rubric import (line 61) and
add the revision-provenance constant:

```python
from src.services.blackbird_rubric import BANDING, RUBRIC_VERSION, load_rubric
from src.services.rubric_revisions import PROVENANCE_UNKNOWN, resolve_revision
```

Add this helper next to the other `_load_review_*` functions:

```python
def _review_dimension_rows(review: AssessmentReview) -> tuple[list[dict], str]:
    """One reviewer's stored per-dimension scores, titled by the revision THEY
    scored against — never by today's document.

    Rubric v3.0.0 replaced thirteen dual-scale dimensions with six single-scale
    ones, so a dimension key is only meaningful against its own revision. An
    unresolvable stamp renders the keys as stored, untitled: silently remapping
    them onto today's dimensions would manufacture a claim nobody made (A13).
    Returns ``([], "")`` for a review that scored no dimensions.
    """
    scores = (
        review.dimension_scores if isinstance(review.dimension_scores, dict) else {}
    )
    if not scores:
        return [], ""
    revision, provenance = resolve_revision(
        review.rubric_version, review.rubric_content_hash
    )
    titles = (
        {d.key: d.title for d in revision.dimensions} if revision is not None else {}
    )
    rows = [
        {"key": key, "title": titles.get(key, key), "score": scores[key]}
        for key in sorted(scores)
    ]
    return rows, provenance
```

In `_load_review_feedback`, attach it before returning — the same
attach-to-the-row pattern `list_assessments` uses for `panel_state` and
`review_cols`, and for the same reason: a row-borne attribute cannot
half-arrive on one surface:

```python
    rows = list(rows)
    for row in rows:
        # Not mapped columns — ordinary instance attributes on read-only rows
        # handed straight to a template. Nothing is persisted.
        row.dimension_rows, row.dimension_provenance = _review_dimension_rows(row)
    return rows
```

In `build_assessment_detail`, after `review_capable_users` is computed, build
the form's context. `normalized_scores` is already in scope (defined just
below `resolve_revision` at line ~564):

```python
    # The scoring form's own source of truth: the LIVE document, because that
    # is what the reviewer is about to score against and what
    # `submit_feedback` will stamp on the row. Deliberately NOT the revision
    # that scored the assessment — a human reviewing a v3.2.0 verdict today is
    # giving a v3.4.0 opinion, and the stamp on their row must say so.
    # `bot_score` rides along per dimension (A9): disagreement has to be
    # visible where the human is choosing, and it is labelled as the bot's.
    live_rubric = load_rubric()
    review_rubric = {
        "version": live_rubric.version,
        "scale_min": live_rubric.scale_min,
        "scale_max": live_rubric.scale_max,
        "dimensions": [
            {
                "key": d.key,
                "title": d.title,
                "weight": d.weight,
                "anchors": d.anchors,
                "bot_score": _score_value(normalized_scores.get(d.key)),
            }
            for d in live_rubric.dimensions
        ],
    }
```

Add two keys to the returned dict, beside the other review keys:

```python
        "review_rubric": review_rubric,
        "revision_provenance_unknown": PROVENANCE_UNKNOWN,
```

- [ ] **Step 4: Add the form to the template**

In `templates/admin/_assessment_detail_body.html`, define this macro
immediately before the `<div>` that opens the Human-review card:

```jinja
{# N3/N11: one optional select per LIVE rubric dimension, rendered from
   `review_rubric` (src/services/assessment_detail.py), which reads
   prompts/rubric/blackbird-rubric.toml directly. Rendering the six from the
   document rather than from literals here is what stops this form drifting
   from the validator that checks it and the stamp that records it.

   `selected` is the reviewer's stored map on an edit form and `{}` on the add
   form. A blank select is a real answer — "I cannot judge this one" — and
   posts an empty string that src/routers/reviews.py drops rather than
   coercing to 0 (the scale starts at 1). #}
{% macro dimension_score_rows(review_rubric, selected) %}
<div class="review-dimension-scores space-y-2">
    {% for d in review_rubric.dimensions %}
    <div class="review-dimension-row review-dim-{{ d.key }} flex items-start gap-2">
        <select name="dim_{{ d.key }}" class="border rounded px-1 py-0.5 text-xs shrink-0">
            <option value="">&mdash;</option>
            {% for n in range(review_rubric.scale_min, review_rubric.scale_max + 1) %}
            <option value="{{ n }}" {% if selected.get(d.key) == n %}selected{% endif %}>{{ n }}</option>
            {% endfor %}
        </select>
        <div class="min-w-0">
            <div class="text-xs font-medium text-gray-700">
                {{ d.title }}
                <span class="font-normal text-gray-400">&middot; {{ d.weight }}% weight</span>
                {# A9: the bot's own number, muted and explicitly attributed so
                   it reads as its claim, never as a pre-filled default. The
                   human's select above stays empty either way. #}
                {% if d.bot_score is not none %}
                <span class="review-bot-score font-normal text-gray-400">&middot; BlackbirdBot scored {{ "%g"|format(d.bot_score) }}</span>
                {% else %}
                <span class="review-bot-score font-normal text-gray-400">&middot; BlackbirdBot did not score this</span>
                {% endif %}
            </div>
            <details class="mt-0.5">
                <summary class="cursor-pointer text-xs text-indigo-600">What the scale means here</summary>
                <p class="mt-1 text-xs text-gray-500">{{ d.anchors }}</p>
            </details>
        </div>
    </div>
    {% endfor %}
</div>
{% endmacro %}
```

Replace the card's existing intro `<p>` (the one beginning
`Scores here rate the proposal's own scientific and strategic merit`) with:

```jinja
    <p class="review-rubric-instructions text-xs text-gray-500 mb-3">
        Scores here rate the proposal's own scientific and strategic merit, not
        how well the bot performed. Review it against Blackbird's rubric
        (<span class="font-mono">{{ review_rubric.version }}</span>): read the
        full rationale below before scoring — the brief at the top of this page
        is BlackbirdBot's own summary of it, not an independent account. Score
        each rubric dimension from {{ review_rubric.scale_min }} (weak) to
        {{ review_rubric.scale_max }} (strongly meets Blackbird's bar), and leave
        a dimension blank if you cannot judge it. The overall proposal-merit
        score is required; the per-dimension scores are not. Use the comment to
        explain the score and to flag anything the assessment got wrong.
    </p>
```

In the **add-feedback** form, between the overall-merit select and the comment
textarea:

```jinja
            <label class="block text-xs font-medium text-gray-600 mb-1 mt-3">Rubric dimensions (optional)</label>
            {{ dimension_score_rows(review_rubric, {}) }}
```

In the **edit** form, in the same position:

```jinja
            <label class="block text-xs font-medium text-gray-600 mb-1 mt-3">Rubric dimensions (optional)</label>
            {{ dimension_score_rows(review_rubric, review.dimension_scores or {}) }}
```

In the **displayed** feedback row, immediately after the comment `<p>`:

```jinja
                {% if review.dimension_rows %}
                <ul class="review-dimension-list mt-1 text-xs text-gray-600 space-y-0.5">
                    {% for row in review.dimension_rows %}
                    <li><span class="font-semibold text-gray-700">{{ row.score }}</span> &mdash; {{ row.title }}</li>
                    {% endfor %}
                </ul>
                {% if review.dimension_provenance == revision_provenance_unknown %}
                {# A13. The stamp matches no entry in prompts/rubric/revisions.toml,
                   so the keys above are printed exactly as stored and carry no
                   titles or weights. Saying nothing would let them read as
                   today's dimensions, which is the claim this branch exists to
                   avoid making. #}
                <p class="review-dimension-unknown-revision mt-1 text-xs text-amber-700">
                    Scored against an unrecognized rubric revision
                    (<span class="font-mono">{{ review.rubric_version or 'unstamped' }}</span>) —
                    shown as stored, with no dimension titles or weights.
                </p>
                {% endif %}
                {% endif %}
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
.venv-test/bin/python -m pytest tests/integration/test_assessment_review_ui.py tests/integration/test_assessment_detail_page.py -v
```

Expected: all PASS, including the pre-existing detail-page tests.

- [ ] **Step 6: Commit**

```bash
git add src/services/assessment_detail.py templates/admin/_assessment_detail_body.html \
        tests/integration/test_assessment_review_ui.py
git commit -m "feat(reviews): rubric-dimension scoring form on the Human-review card

One optional 1-5 select per live rubric dimension, rendered from
blackbird-rubric.toml so the form, the validator and the stamp read one
document. Each row carries the dimension's anchor text and BlackbirdBot's own
score for it. Stored reviews are titled by the revision THEY were scored
against; an unresolvable stamp shows raw keys with a warning rather than
remapping onto today's dimensions.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Stop `review_bot` swallowing a dimension-score-only edit (A1)

Implements spec §2.4. **This is a correctness fix, not a nicety** — without it,
Task 3 introduces a silent data-loss path.

The hole: `edit_feedback` resets `consumed_at = None` and enqueues a fresh job,
but an already-`processing` job's content-conditional UPDATE
(`src/services/review_bot.py:511-512`) matches on `score` and `comment` only.
A reviewer who changes **only** the per-dimension scores therefore has
`consumed_at` re-stamped by the in-flight job; the newly enqueued job then finds
nothing unconsumed and completes as a no-op. No WARNING fires, because from the
UPDATE's point of view the stamp succeeded.

**Files:**
- Modify: `src/services/review_bot.py` (`feedback_snapshot` at 444; the UPDATE at 505-514)
- Test: `tests/integration/test_review_dimension_scores.py` (append)

**Interfaces:**
- Consumes: `AssessmentReview.dimension_scores` (Task 2), `edit_feedback` (Task 3).
- Produces: nothing new. **Adds no imports** — the worker's import boundary
  (see File Structure) must not move.

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_review_dimension_scores.py`:

```python
async def test_a_dimension_only_edit_survives_an_in_flight_analysis_job(
    client, db_session
):
    """A1. `edit_feedback` resets consumed_at and enqueues a new job, but the
    in-flight job's conditional UPDATE used to match on (score, comment) alone
    — so an edit that touched ONLY the dimension scores got re-stamped
    consumed, and the new job then found nothing to analyze. Silent: the stamp
    SUCCEEDED, so the job's own `stamped != len(reviews)` warning never fired.

    Simulated by snapshotting the row, editing it, then running the UPDATE the
    handler runs — the same shape as the handler, without an Opus round trip.
    """
    from sqlalchemy import update

    from src.models import AssessmentReview as AR
    from src.services.review_bot import _feedback_snapshot_entry

    key_a, key_b = _first_two_dimension_keys()
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)

    review = await submit_feedback(
        db_session, assessment=assessment, reviewer=reviewer, score=4,
        comment="unchanged", feedback_mode="learn",
        dimension_scores={key_a: 5},
    )
    await db_session.flush()

    # The job snapshots the row, then goes off to the model.
    snap = _feedback_snapshot_entry(review)

    # Meanwhile the reviewer changes ONLY the dimension scores.
    await edit_feedback(
        db_session, review=review, score=4, comment="unchanged",
        feedback_mode="learn", dimension_scores={key_a: 5, key_b: 1},
    )
    await db_session.flush()

    # The job comes back and tries to stamp what it snapshotted.
    from src.services.review_bot import consumed_at_predicates

    result = await db_session.execute(
        update(AR)
        .where(AR.id == review.id, *consumed_at_predicates(snap))
        .values(consumed_at=review.created_at)
    )
    assert result.rowcount == 0, (
        "the in-flight job re-stamped a row whose dimension scores had changed"
    )
    await db_session.refresh(review)
    assert review.consumed_at is None

    # CONTROL, so the assertion above cannot pass for the wrong reason: the
    # same predicate against an UNCHANGED snapshot must still match. Without
    # it, a `consumed_at_predicates` that matched NOTHING at all would look
    # exactly like a pass.
    fresh = _feedback_snapshot_entry(review)
    ok = await db_session.execute(
        update(AR).where(AR.id == review.id, *consumed_at_predicates(fresh))
        .values(consumed_at=review.created_at)
    )
    assert ok.rowcount == 1
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
.venv-test/bin/python -m pytest tests/integration/test_review_dimension_scores.py::test_a_dimension_only_edit_survives_an_in_flight_analysis_job -v
```

Expected: FAIL with `ImportError: cannot import name '_feedback_snapshot_entry'`.

- [ ] **Step 3: Extract the snapshot and the predicate, and add the comparison**

In `src/services/review_bot.py`, add these two module-level functions above
`execute_review_analysis`:

```python
def _feedback_snapshot_entry(review: AssessmentReview) -> dict:
    """One review row as the plain dict a suggestion records as provenance.

    Extracted from the inline comprehension so the consumed_at predicate below
    and the snapshot can never describe different fields — which is exactly how
    a dimension-score-only edit came to be silently swallowed (A1).
    """
    return {
        "id": str(review.id),
        "reviewer_name": review.reviewer_name,
        "score": review.score,
        "dimension_scores": review.dimension_scores,
        "feedback_mode": review.feedback_mode,
        "comment": review.comment,
        "created_at": review.created_at.isoformat(),
    }


def consumed_at_predicates(snap: dict) -> tuple:
    """WHERE terms that match a row ONLY if it still reads exactly as
    snapshotted.

    EVERY reviewer-editable field must appear here. `edit_feedback` resets
    `consumed_at` and enqueues a replacement job, so a field missing from this
    tuple means an in-flight job re-stamps a row that has since changed, the
    replacement job finds nothing unconsumed, and the edit is lost with no
    warning — the stamp having *succeeded* is why nothing logs (A1).

    `dimension_scores` is JSONB and nullable, so the None case must be spelled
    `.is_(None)`: SQL `col = NULL` is never true, which would make every
    unscored row look edited and leave it permanently unconsumed.
    """
    dims = snap["dimension_scores"]
    return (
        AssessmentReview.consumed_at.is_(None),
        AssessmentReview.feedback_mode == "learn",
        AssessmentReview.score == snap["score"],
        AssessmentReview.comment == snap["comment"],
        AssessmentReview.dimension_scores.is_(None)
        if dims is None
        else AssessmentReview.dimension_scores == dims,
    )
```

Replace the inline snapshot comprehension (line 444) with:

```python
    feedback_snapshot = [_feedback_snapshot_entry(review) for review in reviews]
```

Replace the UPDATE's `.where(...)` (lines 508-514) with:

```python
        result = await db.execute(
            update(AssessmentReview)
            .where(AssessmentReview.id == review.id, *consumed_at_predicates(snap))
            .values(consumed_at=now)
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv-test/bin/python -m pytest \
  tests/integration/test_review_dimension_scores.py \
  tests/integration/test_review_pipeline_races.py \
  tests/integration/test_review_job_end_to_end.py \
  tests/unit/test_review_bot.py tests/unit/test_review_bot_inputs.py \
  tests/unit/test_review_bot_edges.py -v
```

Expected: all PASS. `test_review_bot_inputs.py` includes the AST probe that
`interview_transcript.py` imports no rubric module — this task adds no imports
anywhere, so it must stay green.

- [ ] **Step 5: Commit**

```bash
git add src/services/review_bot.py tests/integration/test_review_dimension_scores.py
git commit -m "fix(review-bot): include dimension_scores in the consumed_at predicate

edit_feedback resets consumed_at and enqueues a replacement job, but the
in-flight job's conditional UPDATE matched on (score, comment) alone — so an
edit touching only the per-dimension scores was re-stamped consumed and the
replacement job found nothing to analyze. Silent, because the stamp succeeded.

Snapshot and predicate are now one pair of functions so they cannot describe
different fields again. NULL is compared with IS NULL, not =.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Sidecar contract — three narrative fields and two prose rules

Implements spec §3.2. **Prompt files only** — no Python. Nothing writes the
columns yet (that is Task 8), so this task is safe to land alone: the hub
simply emits three keys the engine currently ignores, which `raw_verdict`
preserves regardless.

**Files:**
- Modify: `prompts/roles/scout_hub/phase4-thread-reply.md`
- Modify: `prompts/roles/scout_hub/role.toml` (line 7)
- Modify (generated): `docs/specs/2026-08-07-hub-bot-prompts.md`
- Test: `tests/unit/test_rubric_prompt_sync.py` (append)

**Interfaces:**
- Produces: sidecar keys `headline` (str), `key_points` (list[str]),
  `elevator_pitch` (str). Task 8 reads exactly these three names off the
  parsed verdict dict.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_rubric_prompt_sync.py`:

```python
def test_skeleton_carries_the_three_narrative_fields():
    """The reviewer-facing narrative contract (design §3.2). These are NOT
    scored and are deliberately absent from the rubric document — the sidecar
    skeleton is their only definition, so this is where drift is caught."""
    skeleton = _skeleton()
    for key in ("headline", "key_points", "elevator_pitch"):
        assert key in skeleton, f"phase4-thread-reply.md dropped {key!r}"
    assert skeleton["key_points"] == []
    assert skeleton["headline"] == ""
    assert skeleton["elevator_pitch"] == ""


def test_phase4_binds_the_rationale_to_a_bolded_summary_sentence():
    """N6: a short run-in label AND a bolded one-sentence summary, so reading
    only the bold text gives the whole argument."""
    body = _norm(_phase4_text())
    assert "bolded one-sentence summary" in body
    assert "two to four words" in body


def test_phase4_binds_the_next_experiment_to_a_bolded_one_line_ask():
    """The detail page's "The ask" line is the FIRST line of
    recommended_next_experiment. Parsing cost and duration out of prose would
    be fragile in exactly the way that produces a confidently wrong number, so
    the prompt is asked for it instead (design §3.2)."""
    body = _norm(_phase4_text())
    assert "bolded one-line ask" in body


def test_the_scout_hub_prompt_set_version_moved_with_the_contract():
    """`prompt_set_stamp` records version + content hash in every run-start
    announcement, so a content change without a version bump is by definition
    an unrecorded edit (A17)."""
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    manifest = tomllib.loads(
        (root / "prompts/roles/scout_hub/role.toml").read_text(encoding="utf-8")
    )
    assert manifest["version"] != "1.0.0", (
        "phase4-thread-reply.md changed; bump prompts/roles/scout_hub/role.toml"
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv-test/bin/python -m pytest tests/unit/test_rubric_prompt_sync.py -v
```

Expected: FAIL — `phase4-thread-reply.md dropped 'headline'`.

- [ ] **Step 3: Add the three contract items**

In `prompts/roles/scout_hub/phase4-thread-reply.md`, after numbered item 5
(**Recommended next experiment to fund**) and before the
`**Formatting `rationale` and `recommended_next_experiment`.**` paragraph,
insert:

```markdown
6. **Headline.** One sentence, at most 200 characters, written for a Blackbird
   reviewer who has never heard of this lab. It must name the mechanism or
   tool, the target disease or patient population, and the reason this is
   fundable — the whole story in one line. This is NOT the project label;
   `company_or_project` already carries that, and both are stored.
   Record it in `headline`.
7. **Key points.** Three to five bullets, each at most 160 characters and each
   a complete claim rather than a topic — "the classifier does not exist yet;
   no AUC was ever computed", not "classifier status". Together they must let a
   reviewer who reads nothing else say what the idea is, what is actually
   established, and what the deciding risk is. Record them in `key_points` as
   an array of strings.
8. **Elevator pitch.** Three to five sentences of plain language, for a
   scientifically literate reader who is not a specialist in this field. State
   what exists today, what the money would buy, and why the answer matters.
   Minimal jargon; spell out an abbreviation the first time. Record it in
   `elevator_pitch`.
```

Replace the **Formatting** paragraph with:

```markdown
**Formatting `rationale` and `recommended_next_experiment`.** Write both fields
in simple Markdown: short paragraphs separated by a blank line, `-` bullets for
lists. No headings, no tables, no code fences. Wrap any identifier that contains
a literal asterisk in backticks — e.g. `` `HLA-A*02:01` `` — so it renders as
text instead of being read as emphasis.

Open **every** `rationale` paragraph with a run-in label of **two to four
words** in bold, then a **bolded one-sentence summary** of that paragraph:

    **Scientific panel.** **The circadian confound is the biggest threat to
    this biomarker.** Ordering (discovery-set analytics, then randomised
    interaction) is correct. At 30 events a composite AUC is estimable but not
    lockable...

Keep the label short — it names the source, the summary carries the finding. A
reader who reads only the bold text must get the whole argument.

Open `recommended_next_experiment` with a **bolded one-line ask** naming the
cost and the duration before anything else:

    **$100–175K · 4–6 months — pre-registered analytical-validity package on
    the existing 124-patient cohort.**

Everything else — scope of work, deliverable, pass threshold — follows that
line as normal paragraphs and bullets.
```

Update the `<assessment_json>` skeleton, adding the three keys immediately
after `subject_agent_id`:

```
<assessment_json>
{
  "company_or_project": "",
  "subject_agent_id": "",
  "headline": "",
  "key_points": [],
  "elevator_pitch": "",
  "gating": {
```

(the rest of the skeleton is unchanged).

- [ ] **Step 4: Bump the prompt-set version**

In `prompts/roles/scout_hub/role.toml`, line 7:

```toml
version = "1.1.0"
```

- [ ] **Step 5: Regenerate the embedded prompt docs**

```bash
.venv-test/bin/python scripts/sync_prompt_set_docs.py
.venv-test/bin/python scripts/sync_prompt_set_docs.py --check
```

Expected: the second command reports no drift.

- [ ] **Step 6: Run the tests to verify they pass**

```bash
.venv-test/bin/python -m pytest tests/unit/test_rubric_prompt_sync.py tests/unit/test_doc_prompt_sync.py -v
```

Expected: all PASS. In particular
`test_skeleton_scores_keys_are_exactly_the_rubric_dimensions` and
`test_skeleton_gating_keys_are_exactly_the_documents_gating_criteria` must
still pass — the three new keys are top-level and touch neither set. **If
either fails, a new key landed inside `scores` or `gating`; move it out.**

- [ ] **Step 7: Commit**

```bash
git add prompts/roles/scout_hub/phase4-thread-reply.md \
        prompts/roles/scout_hub/role.toml \
        docs/specs/2026-08-07-hub-bot-prompts.md \
        tests/unit/test_rubric_prompt_sync.py
git commit -m "feat(prompts): sidecar carries a headline, key points and an elevator pitch

Three new top-level sidecar keys for reviewers, plus two prose rules: every
rationale paragraph opens with a short run-in label and a bolded one-sentence
summary, and recommended_next_experiment opens with a bolded one-line ask
naming cost and duration (so the detail page never has to parse them out).

No rubric change — no weight, threshold, dimension or gating key moves.
scout_hub prompt set 1.0.0 -> 1.1.0.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Persist the three narrative fields

Implements spec §3.3. After this task the engine writes what Task 7 asks for.

**Files:**
- Modify: `src/agent/simulation.py` (`_persist_assessment`'s `assessment_kwargs`, around line 4519)
- Test: `tests/integration/test_assessment_narrative_fields.py` (append)

**Interfaces:**
- Consumes: sidecar keys `headline` / `key_points` / `elevator_pitch` (Task 7); the three columns (Task 2).
- Produces: populated columns for every verdict written after deploy.

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_assessment_narrative_fields.py`:

`_persist_assessment` is called as an UNBOUND method on a real (if agent-less)
`SimulationEngine` built over its own `async_sessionmaker`, and the row is read
back through **that same factory** — not through `db_session`, which is a
different transaction and will not see the engine's commit. This is the harness
`tests/integration/test_opportunity_assessment_persistence.py:170-192` already
uses; copy it rather than inventing a second one. Note the positional argument
order: `(self, agent_id, channel, verdict)`.

```python
async def test_persist_assessment_stores_the_three_narrative_fields(engine):
    """Sidecar items 6-8 reach their columns."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    stub = SimulationEngine(
        agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
    )
    await SimulationEngine._persist_assessment(stub, "blackbird", "general", {
        "subject_agent_id": "wang",
        "company_or_project": "Short label",
        "headline": "A blood test that says who responds to immunotherapy.",
        "key_points": ["No classifier exists yet", "Circadian confound unmeasured"],
        "elevator_pitch": "Hopkins has cytokine data on 124 patients.",
        "recommendation": "conditional",
        "scores": {},
    })

    async with factory() as db:
        row = (await db.execute(
            select(OpportunityAssessment).where(
                OpportunityAssessment.simulation_run_id == run_id
            )
        )).scalars().one()
    assert row.headline.startswith("A blood test")
    assert row.key_points == ["No classifier exists yet", "Circadian confound unmeasured"]
    assert row.elevator_pitch.startswith("Hopkins has")


async def test_a_non_list_key_points_degrades_to_null_and_keeps_raw_verdict(engine):
    """A20. A model that answers `key_points` with a string must not DataError
    the row out of existence — the row IS the archive. It degrades to NULL and
    raw_verdict keeps what was actually emitted, exactly like `red_flags` and
    `derisking_milestones` already do."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    stub = SimulationEngine(
        agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
    )
    await SimulationEngine._persist_assessment(stub, "blackbird", "general", {
        "company_or_project": "Short label",
        "key_points": "not a list at all",
        "recommendation": "pass",
        "scores": {},
    })

    async with factory() as db:
        row = (await db.execute(
            select(OpportunityAssessment).where(
                OpportunityAssessment.simulation_run_id == run_id
            )
        )).scalars().one()
    assert row.key_points is None
    assert row.raw_verdict["key_points"] == "not a list at all"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv-test/bin/python -m pytest tests/integration/test_assessment_narrative_fields.py -v
```

Expected: FAIL — `row.headline` is `None`.

- [ ] **Step 3: Write the columns**

In `src/agent/simulation.py`, inside `assessment_kwargs`, immediately after the
`company_or_project=` line:

```python
            # Sidecar items 6-8 (2026-09-09): the reviewer-facing narrative.
            # `company_or_project` above stays the short label — these three are
            # what the assessment pages lead with. Each degrades exactly like
            # its existing siblings: a wrong type becomes None and `raw_verdict`
            # keeps the original, because a malformed narrative field must never
            # cost the verdict (A20).
            headline=_str_or_none(verdict.get("headline")),
            key_points=(
                key_points if isinstance(key_points, list) else None
            ),
            elevator_pitch=_str_or_none(verdict.get("elevator_pitch")),
```

**Order matters, and the plan's presentation is not the file's order.** Add the
extraction FIRST, on the line after
`milestones = verdict.get("suggested_derisking_milestones")`
(`src/agent/simulation.py:4483`) — it must precede both Step 4's warning block
and `assessment_kwargs`:

```python
        key_points = verdict.get("key_points")
```

- [ ] **Step 4: Add the shape warnings**

Immediately before `assessment_kwargs` is built, add:

```python
        # Shape checks are WARNINGS, never drops (A4). Nothing here can verify
        # that a headline tells the whole story — only that it is the shape the
        # contract asks for. A verdict that misses the shape is still the
        # archive's copy of that verdict.
        if isinstance(verdict.get("headline"), str) and len(
            verdict["headline"]
        ) > _HEADLINE_SOFT_LIMIT:
            logger.warning(
                "[%s] Assessment headline is %d chars (contract asks for <=%d): %s",
                agent_id, len(verdict["headline"]), _HEADLINE_SOFT_LIMIT,
                verdict["headline"][:80],
            )
        if isinstance(key_points, list) and not (
            _KEY_POINTS_MIN <= len(key_points) <= _KEY_POINTS_MAX
        ):
            logger.warning(
                "[%s] Assessment carries %d key_points (contract asks for %d-%d)",
                agent_id, len(key_points), _KEY_POINTS_MIN, _KEY_POINTS_MAX,
            )
```

with these module-level constants beside the other assessment constants in
`src/agent/simulation.py`:

```python
#: Contract bounds from prompts/roles/scout_hub/phase4-thread-reply.md items
#: 6-7. SOFT: exceeding one logs a WARNING and stores the value as emitted.
#: Enforcing them by dropping would trade a long headline for a lost verdict,
#: and the row is the archive.
_HEADLINE_SOFT_LIMIT = 200
_KEY_POINTS_MIN = 3
_KEY_POINTS_MAX = 5
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
.venv-test/bin/python -m pytest \
  tests/integration/test_assessment_narrative_fields.py \
  tests/integration/test_opportunity_assessment_persistence.py \
  tests/integration/test_hub_assessment_capture_gate.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/agent/simulation.py tests/integration/test_assessment_narrative_fields.py
git commit -m "feat(sim): persist the sidecar's headline, key points and elevator pitch

Each degrades like its existing siblings — a wrong type becomes NULL and
raw_verdict keeps the original, because a malformed narrative field must never
cost the verdict. Contract bounds are WARNINGs, not drops.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: Assessments list — table becomes a card list

Implements spec §4 on **both** `/admin/assessments` and `/manager/assessments`
(decision N7), which share one body template.

> **A2 — the trap in this task.** `admin_assessments` ALLOWLISTS every context
> key it forwards (`src/routers/admin.py:814-840`, and its own comment says
> so); `manager_assessments` splats `**view`. A new top-level context key
> therefore reaches the manager page and is Jinja `Undefined` — silently falsy,
> never an error — on the admin page. **This task must add no new top-level
> context key.** Everything the card needs is already on the row objects:
> `a.headline`, `a.key_points`, `a.company_or_project`, `a.gating`,
> `a.red_flags`, `a.rubric_version`, `a.rubric_content_hash`, `a.created_at`,
> `a.panel_state`, `a.review_cols`, `a.weighted_score`, `a.band`,
> `a.recommendation`, `a.confidence`, `a.subject_agent_id`,
> `a.simulation_run_id`. The wrappers' `assessment_link(a)` and `pi_link(a)`
> macros carry the two literal URLs.

**Files:**
- Modify: `templates/admin/_assessments_body.html` (replace the `<table>` block from `{% if assessments %}` to its `{% endif %}`)
- Modify: `tests/integration/test_assessment_queue_controls.py:519` (`_row_slice` — **see the box below; this is not optional**)
- Modify: `tests/integration/test_manager_views.py:534` (the `"panel</span>"` pin — see the second box)
- Test: `tests/integration/test_opportunity_assessment_persistence.py:1460` (`test_admin_assessments_page_renders_no_inline_detail_rows`)
- Test: `tests/integration/test_assessment_queue_controls.py` (append)

> **`_row_slice` FAILS SILENTLY under a card list — fix it in this task.**
> `tests/integration/test_assessment_queue_controls.py:519` is
> `html.split(marker, 1)[1].split("</tr>", 1)[0]`. With the table gone there is
> no `</tr>`, so `str.split` returns a one-element list and `[0]` is **the
> entire rest of the page**. The four assertions that use it — the two
> parametrized runs of `test_list_pages_show_reviewer_columns`,
> `test_reviewer_role.py::test_reviewer_sees_the_review_columns_on_manager_assessments`,
> and the row-scoped badge check — would keep PASSING while checking nothing: a
> per-row assertion silently widened to the whole page, which is strictly worse
> than a red test. Verified by direct simulation, 2026-09-09.
>
> Rewrite it against the card boundary, keeping the name and signature so no
> call site changes:
>
> ```python
> #: One card's markup, from the row marker to the start of the NEXT card.
> #: Was `.split("</tr>", 1)[0]` until 2026-09-09. The card list has no `</tr>`,
> #: and `str.split` on an absent separator returns the whole remaining page —
> #: so every caller silently became a PAGE-wide assertion instead of a
> #: row-scoped one, and none of them failed.
> def _row_slice(html: str, marker: str) -> str:
>     """The rest of the card the row marker sits in."""
>     return html.split(marker, 1)[1].split('class="assessment-card ', 1)[0]
> ```
>
> and pin the boundary, so the next layout change fails loudly instead of going
> vacuous again:
>
> ```python
> def test_row_slice_stops_at_the_next_card():
>     """Guard on the guard. `_row_slice` is what makes four column assertions
>     ROW-scoped. If its boundary string stops matching the markup it does not
>     fail — it returns the whole page, and those four stop testing anything."""
>     html = (
>         '<div class="assessment-card p-5">A-MARKER Alice</div>'
>         '<div class="assessment-card p-5">B-MARKER Bob</div>'
>     )
>     sliced = _row_slice(html, "A-MARKER")
>     assert "Alice" in sliced
>     assert "Bob" not in sliced
> ```
>
> The template's card `<div>` must therefore open with the exact substring
> `class="assessment-card ` (trailing space included). It does in Step 3 — do
> not reorder those classes.

> **The gap badge's text change breaks one existing pin — update it here.**
> `tests/integration/test_manager_views.py:534` asserts `"panel</span>" in html`
> against today's `&#9873; panel</span>`. Step 3 relabels that badge
> `&#9873; panel incomplete` (the bare word "panel" was legible in the
> Recommendation column and is not on a card), so the literal
> `panel</span>` no longer appears. Verified by direct comparison, 2026-09-09.
> Change it to:
>
> ```python
>     # Relabelled 2026-09-09 with the card list: the badge read "⚑ panel" when
>     # it sat inside the Recommendation column and had that column for context.
>     # On a card it needs to say what it means. Still the per-row marker, still
>     # distinct from the banner copy asserted above.
>     assert "panel incomplete</span>" in html
> ```
>
> The three OTHER badge texts are deliberately unchanged, because four
> assertions in `test_assessment_queue_controls.py` pin them by exact string
> and by COUNT (`:340-350`, `:397-399`, `:448-455`): `panel not recorded`,
> `panel unverified`, `panel state unknown`, and the
> `"&#9873; panel" in html or "⚑ panel" in html` check — which
> `&#9873; panel incomplete` still satisfies. Each badge's `title=` text is
> also checked to contain none of those phrases, so the `== 1` counts hold.
> **Do not reword any of the three.**
>
> The red-flag cell keeps `{{ n }} flag{{ 's' }}` exactly: `"1 flag"` and
> `"2 flags"` are pinned at
> `tests/integration/test_opportunity_assessment_persistence.py:1302`, `:1505`
> and `:2370`. Only the EMPTY case changes wording ("none" → "no flags"), and
> nothing pins that.

Everything ABOVE the list is untouched: the run/lab/sort controls, the five
recommendation tiles, the dropped-verdict banner, the unvetted-panel banner and
the dimension-distribution disclosure all live before the `{% if assessments %}`
this task replaces. So does the "Displaying the top N of TOTAL" note — that one
sits in the two WRAPPERS (`templates/admin/assessments.html:76-82` and its
manager twin), not in this body. **That note is A10's entire mitigation**:
`ASSESSMENTS_LIMIT` stays 500, cards are several times taller than table rows,
and the count line is what tells a reader the page is truncated. Do not remove
it, and do not add pagination in this task.

**Interfaces:**
- Consumes: `OpportunityAssessment.headline` / `.key_points` (Task 2, populated by Task 8).
- Produces: CSS hooks `assessment-card`, `assessment-card-headline`, `assessment-card-points`.

- [ ] **Step 1: Write the failing tests**

Narrow the scan-only pin (A11). Replace the docstring and the final assertions
of `test_admin_assessments_page_renders_no_inline_detail_rows` in
`tests/integration/test_opportunity_assessment_persistence.py`:

```python
async def test_admin_assessments_page_renders_no_inline_detail_rows(
    client, db_session, admin
):
    """The expandable per-row detail (rationale + score chips + red-flag list +
    milestones, toggled by an onclick) was removed 2026-08-27, and the three
    DENSE fields it showed stay off this page.

    NARROWED 2026-09-09 (design §4.1): the page is now a card list and each card
    carries `key_points`. That is a deliberate, bounded reversal — `key_points`
    is a purpose-built 3-5 bullet summary written for triage, not the dense
    evidence this pin was written about. `rationale`, red-flag TEXT and
    `derisking_milestones` remain detail-page content, and the flag COUNT
    remains their only trace here. The blanket "assessment-detail" not-in check
    also keeps the wrapper macros' class name honest.
    """
    run = SimulationRun()
    db_session.add(run)
    await db_session.flush()
    assessment = OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="wang",
        channel_name="general", company_or_project="Rich verdict fixture",
        weighted_score=3.05, band="conditional",
        rationale="RATIONALE-ONLY-ON-THE-DETAIL-PAGE",
        red_flags=["FLAG-ONLY-ON-THE-DETAIL-PAGE"],
        derisking_milestones=["MILESTONE-ONLY-ON-THE-DETAIL-PAGE"],
        scores={"differentiation": 4},
        key_points=["KEY-POINT-BELONGS-ON-THE-TRIAGE-PAGE"],
    )
    db_session.add(assessment)
    await db_session.flush()

    resp = await client.get("/admin/assessments", headers=_auth(admin.id))
    assert resp.status_code == 200
    html = resp.text
    assert "Rich verdict fixture" in html

    # None of the removed machinery, even for a row with every detail field.
    assert "assessment-detail" not in html
    assert "Expand all" not in html and "Collapse all" not in html
    assert "Click for rationale" not in html

    # The three dense fields stay off the triage page — the flag count badge is
    # their only trace here.
    assert "RATIONALE-ONLY-ON-THE-DETAIL-PAGE" not in html
    assert "FLAG-ONLY-ON-THE-DETAIL-PAGE" not in html
    assert "MILESTONE-ONLY-ON-THE-DETAIL-PAGE" not in html
    assert "1 flag" in html

    # The summary field DOES render — that is the point of the card list.
    assert "KEY-POINT-BELONGS-ON-THE-TRIAGE-PAGE" in html
```

Append to `tests/integration/test_assessment_queue_controls.py`:

```python
async def test_the_card_leads_with_the_headline_and_keeps_the_short_label(
    client, db_session, admin
):
    run = await factories.make_simulation_run(db_session)
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="wang",
        channel_name="general",
        company_or_project="SHORT-LABEL-MARKER",
        headline="HEADLINE-MARKER: a blood test for immunotherapy response.",
    ))
    await db_session.flush()

    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text
    assert "assessment-card-headline" in html
    assert "HEADLINE-MARKER" in html
    assert "SHORT-LABEL-MARKER" in html


async def test_a_row_with_no_headline_falls_back_to_the_short_label(
    client, db_session, admin
):
    """A3. All 12 rows in production today have headline IS NULL and are never
    backfilled, so the fallback is the COMMON case, not an edge one. It must
    render exactly what today's page renders, with no empty-state artefact."""
    run = await factories.make_simulation_run(db_session)
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="wang",
        channel_name="general", company_or_project="ONLY-LABEL-MARKER",
        headline=None, key_points=None,
    ))
    await db_session.flush()

    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text
    assert "ONLY-LABEL-MARKER" in html
    # No bullet block, and no subtitle line — the fallback shows the label
    # ONCE, as the heading, exactly as the old Project cell did.
    assert "assessment-card-points" not in html
    assert "assessment-card-label" not in html


async def test_the_card_keeps_gating_panel_flags_and_rubric_on_its_face(
    client, db_session, admin
):
    """N9. Dropping the panel badge in particular would be a real loss:
    rendering a non-verified panel as unremarkable is a named failure mode."""
    run = await factories.make_simulation_run(db_session)
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="wang",
        channel_name="general", company_or_project="Face check",
        gating={"life_sciences_domain": "met", "credible_science": "unconfirmed"},
        red_flags=["one", "two"],
        rubric_version="3.4.0",
    ))
    await db_session.flush()

    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text
    assert "life sciences domain" in html
    assert "2 flags" in html
    assert "3.4.0" in html
    assert "panel not recorded" in html   # panel_owed IS NULL -> 'unrecorded'


async def test_the_manager_surface_renders_the_same_cards(client, db_session):
    from src.models import USER_ROLE_MANAGER

    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    run = await factories.make_simulation_run(db_session)
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="wang",
        channel_name="general", company_or_project="Manager card",
        headline="MANAGER-HEADLINE-MARKER",
    ))
    await db_session.flush()

    html = (await client.get(
        f"/manager/assessments?run_id={run.id}", headers=auth_headers(manager.id)
    )).text
    assert "assessment-card" in html
    assert "MANAGER-HEADLINE-MARKER" in html
```

Check the import names at the top of `test_assessment_queue_controls.py` before
adding — it already imports `factories` and `OpportunityAssessment`; confirm
whether its auth helper is named `auth_headers` or `_auth` and match it:

```bash
grep -n "^from\|^import\|auth_headers\|def _auth" tests/integration/test_assessment_queue_controls.py | head -20
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv-test/bin/python -m pytest \
  tests/integration/test_opportunity_assessment_persistence.py::test_admin_assessments_page_renders_no_inline_detail_rows \
  tests/integration/test_assessment_queue_controls.py -v -k "card or headline or face or manager_surface"
```

Expected: FAIL — `assessment-card-headline` is not in the HTML.

- [ ] **Step 3: Replace the table with cards**

In `templates/admin/_assessments_body.html`, replace everything from
`{% if assessments %}` down to (but not including) the final
`{% else %}` / no-assessments block with:

```jinja
{% if assessments %}
{# Card list, both surfaces (design N7). Replaced the eleven-column triage
   table 2026-09-09: the Project cell was a truncated wall of jargon and the
   page could not be read, only scanned.

   STILL SCAN-ONLY in the sense the 2026-08-27 pin means: `rationale`, red-flag
   TEXT and `derisking_milestones` are detail-page content and appear nowhere
   here. `key_points` is the one addition, and it is a purpose-built 3-5 bullet
   summary rather than dense evidence — see design §4.1 and the narrowed
   test_admin_assessments_page_renders_no_inline_detail_rows.

   NO NEW CONTEXT KEY may be introduced here. `admin_assessments` allowlists
   every key it forwards while `manager_assessments` splats, so a top-level key
   added to list_assessments and not to that allowlist renders as silently-falsy
   Jinja Undefined on the admin surface ONLY. Everything below is either an
   attribute of `a` or one of the two wrapper macros. #}
<div class="space-y-4">
{% for a in assessments %}
    <div class="assessment-card bg-white rounded-xl shadow-sm border border-gray-200 p-5">
        <div class="flex items-start justify-between gap-4 flex-wrap">
            <div class="min-w-0">
                {# The story headline leads. NULL on every row written before
                   0043 and never backfilled, so the fallback is the COMMON
                   case today, not an edge one — it must render exactly what
                   the old Project cell rendered. #}
                <div class="assessment-card-headline text-base font-semibold text-gray-900">
                    {{ a.headline or a.company_or_project or "—" }}
                    {% if a.confidence %}
                        {# Strip any brackets the value already carries before
                           re-wrapping: the prompt shows the label bracketed in
                           the Slack body but bare in the sidecar, and
                           "[[High]]" is nobody's label. #}
                        <span class="text-xs font-normal text-gray-400">[{{ a.confidence.strip("[]") }}]</span>
                    {% endif %}
                </div>
                {% if a.headline and a.company_or_project %}
                <div class="assessment-card-label text-xs text-gray-500 mt-0.5">{{ a.company_or_project }}</div>
                {% endif %}
                <div class="text-xs text-gray-500 mt-1">
                    Lab: <span class="font-medium text-gray-700">{{ pi_link(a) }}</span>
                    &middot; <span data-utc="{{ a.created_at.isoformat() }}" data-utc-fmt="short">{{ a.created_at.strftime("%Y-%m-%d %H:%M") }}</span>
                    &middot;
                    {% if a.rubric_version %}
                        rubric <span class="font-mono"{% if a.rubric_content_hash %} title="content hash {{ a.rubric_content_hash }}"{% endif %}>{{ a.rubric_version }}</span>
                    {% else %}
                        <span title="written before rubric stamping (migration 0030)">rubric unstamped</span>
                    {% endif %}
                    {% if show_all_runs %}
                        {% set src_run = runs_by_id.get(a.simulation_run_id) %}
                        &middot;
                        {% if src_run %}
                            run {{ src_run.started_at.strftime('%b %d %H:%M') }}{% if runs and src_run.id == runs[0].id %} (current){% endif %}
                        {% else %}
                            run unknown
                        {% endif %}
                    {% endif %}
                </div>
            </div>
            <div class="text-right shrink-0">
                {% if a.recommendation %}
                    {% set rec_chip = {
                        'advance': 'bg-green-100 text-green-700',
                        'conditional': 'bg-amber-100 text-amber-700',
                        'route-to-incubation': 'bg-teal-100 text-teal-700',
                        'pass': 'bg-gray-100 text-gray-600',
                    } %}
                    <span class="px-2 py-0.5 rounded-full text-xs font-medium whitespace-nowrap {{ rec_chip.get(a.recommendation, 'bg-gray-100 text-gray-500') }}">{{ banding.pass_label if a.recommendation == 'pass' else a.recommendation }}</span>
                {% else %}<span class="text-xs text-gray-400">no recommendation</span>{% endif %}
                <div class="mt-1">
                    <span class="text-xl font-bold
                        {% if a.band == 'advance' %}text-green-600
                        {% elif a.band == 'conditional' %}text-amber-600
                        {% else %}text-gray-400{% endif %}">
                        {% if a.weighted_score is not none %}{{ "%.2f"|format(a.weighted_score) }}{% else %}—{% endif %}
                    </span>
                    {# "pass" renders as "decline": it is the PDF's deal
                       vocabulary (pass ON the deal), and unlabelled it reads as
                       the opposite of what it means. Always visible text, never
                       colour alone. #}
                    <span class="band-label ml-1.5 text-xs font-medium uppercase tracking-wide align-middle
                        {% if a.band == 'advance' %}text-green-600
                        {% elif a.band == 'conditional' %}text-amber-600
                        {% else %}text-gray-400{% endif %}">{{ (banding.pass_label if a.band == 'pass' else a.band) or '—' }}</span>
                </div>
            </div>
        </div>

        {% if a.key_points %}
        <ul class="assessment-card-points mt-3 list-disc list-inside text-sm text-gray-700 space-y-0.5">
            {% for point in a.key_points %}<li>{{ point }}</li>{% endfor %}
        </ul>
        {% endif %}

        <div class="mt-3 pt-3 border-t border-gray-100 flex items-center gap-x-4 gap-y-1 flex-wrap text-xs">
            {% if a.gating %}
            <span class="flex items-center gap-2 flex-wrap">
                {% for key, gate_state in a.gating.items() %}
                <span class="gating-row gating-{{ gate_state if gate_state in ('met', 'not_met', 'unconfirmed') else 'unknown' }} text-gray-600">
                    {% if gate_state == 'met' %}
                        <span class="text-green-600" title="Met">&#9989;</span>
                    {% elif gate_state == 'not_met' %}
                        <span class="text-red-600" title="Not met">&#10060;</span>
                    {% elif gate_state == 'unconfirmed' %}
                        <span class="text-blue-500" title="Unconfirmed — never asked">&#10067;</span>
                    {% else %}
                        <span class="text-gray-400" title="Unrecognized gating value: {{ gate_state }}">&bull;</span>
                    {% endif %}
                    {{ key.replace("_", " ") }}
                </span>
                {% endfor %}
            </span>
            {% endif %}

            {% if a.red_flags %}
                <span class="text-amber-700 font-medium whitespace-nowrap">{{ a.red_flags | length }} flag{{ '' if a.red_flags | length == 1 else 's' }}</span>
            {% else %}<span class="text-gray-400">no flags</span>{% endif %}

            {# The panel finding, in the same five states the detail page uses.
               BEING UNBADGED IS A CLAIM, so `verified` and `not_owed` are
               enumerated and the terminal {% else %} is a badge — a sixth state
               must never fall off the end reading like a verified panel.
               Pinned by test_an_unknown_panel_state_is_never_left_unbadged. #}
            {% if a.panel_state == 'gap' %}
            <span class="font-medium text-orange-700" title="Missing: {{ (a.missing_domains or [])|join(', ') }}">&#9873; panel incomplete</span>
            {% elif a.panel_state == 'unverified' %}
            <span class="font-medium text-gray-600" title="The specialist floor could not be checked for this verdict (no consult was recorded for anyone, or it named no lab). Not a gap, and not a verification.">panel unverified</span>
            {% elif a.panel_state == 'unrecorded' %}
            <span class="font-medium text-amber-700" title="This row does not record whether a specialist panel was owed, so nothing can be said about one either way.">panel not recorded</span>
            {% elif a.panel_state in ('verified', 'not_owed') %}
            {% else %}
            <span class="font-medium text-amber-700" title="This row's panel finding is not one this page knows how to read, so nothing can be said about it either way. Not a verification.">panel state unknown</span>
            {% endif %}

            {% if a.review_cols.status == 'approved' %}
                <span class="px-2 py-0.5 rounded-full font-medium whitespace-nowrap bg-green-100 text-green-700">Approved</span>
            {% elif a.review_cols.status == 'disapproved' %}
                <span class="px-2 py-0.5 rounded-full font-medium whitespace-nowrap bg-red-100 text-red-700">Disapproved</span>
            {% elif a.review_cols.status is none %}
            {% else %}
                <span class="px-2 py-0.5 rounded-full font-medium whitespace-nowrap bg-amber-100 text-amber-800" title="Unrecognized review status: {{ a.review_cols.status }}">review status unknown</span>
            {% endif %}

            <span class="text-gray-500">Assigned: {{ a.review_cols.assigned_names | join(", ") if a.review_cols.assigned_names else "—" }}</span>
            <span class="text-gray-500">Reviewed by: {{ a.review_cols.reviewed_by_names | join(", ") if a.review_cols.reviewed_by_names else "—" }}</span>
            <span class="ml-auto">{{ assessment_link(a) }}</span>
        </div>
    </div>
{% endfor %}
</div>
{% else %}
```

Leave the existing no-assessments `{% else %}` block and its `{% endif %}`
exactly as they are.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv-test/bin/python -m pytest \
  tests/integration/test_assessment_queue_controls.py \
  tests/integration/test_opportunity_assessment_persistence.py \
  tests/integration/test_manager_views.py \
  tests/integration/test_reviewer_role.py \
  tests/unit/test_reachability.py -v
```

Expected: all PASS. Three of these are the real gate on this task:
`test_row_slice_stops_at_the_next_card` (the new guard — without it the next
four are vacuous), `test_reviewer_role.py::test_reviewer_sees_the_review_columns_on_manager_assessments`
(the Assigned/Reviewed-by content survived, ROW-scoped), and `test_reachability`
(both surfaces' routes still have a literal reference).

- [ ] **Step 5: Verify the body still contains no absolute surface URLs**

```bash
grep -nE '"/(admin|manager)/' templates/admin/_assessments_body.html
```

Expected: **no output.** Any hit means a literal URL leaked into the shared
body and one surface's route will lose its `test_reachability` credit.

- [ ] **Step 6: Commit**

```bash
git add templates/admin/_assessments_body.html \
        tests/integration/test_assessment_queue_controls.py \
        tests/integration/test_manager_views.py \
        tests/integration/test_opportunity_assessment_persistence.py
git commit -m "feat(assessments): card list replaces the eleven-column triage table

Both surfaces. The story headline leads, the short project label demotes to a
subtitle, and up to five key points sit on the card face. Gating icons, the
panel badge, the red-flag count and the rubric stamp all survive — dropping the
panel badge in particular would render a non-verified panel as unremarkable.

Adds no context key: admin allowlists what it forwards while manager splats, so
a new top-level key would be silently Undefined on admin only.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: Detail page — brief first, evidence collapsed, sticky nav

Implements spec §5 and decisions N8/N10.

**Files:**
- Modify: `templates/admin/_assessment_detail_body.html`
- Test: `tests/integration/test_assessment_detail_page.py` (append)

**Interfaces:**
- Consumes: `assessment.headline` / `.key_points` / `.elevator_pitch` (Task 2/8), `assessment.prose_format` (existing).
- Produces: CSS hooks `assessment-brief`, `assessment-brief-pitch`, `assessment-brief-points`, `assessment-jump-nav`, and anchors `#brief`, `#rationale`, `#panel`, `#scores`, `#review`, `#timeline`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_assessment_detail_page.py`:

```python
async def test_the_brief_leads_with_headline_pitch_and_points(
    client, db_session, admin
):
    run, assessment = await _seed(db_session)
    assessment.headline = "HEADLINE-MARKER: a blood test for immunotherapy response."
    assessment.elevator_pitch = "PITCH-MARKER. Hopkins has data on 124 patients."
    assessment.key_points = ["POINT-ONE-MARKER", "POINT-TWO-MARKER"]
    await db_session.flush()

    html = (await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text
    assert "assessment-brief" in html
    assert "HEADLINE-MARKER" in html
    assert "PITCH-MARKER" in html
    assert "POINT-ONE-MARKER" in html and "POINT-TWO-MARKER" in html
    # The brief precedes the rationale on the page, not just in the DOM tree.
    assert html.index("assessment-brief") < html.index("assessment-rationale")


async def test_a_pre_0043_row_renders_the_brief_without_empty_states(
    client, db_session, admin
):
    """A3: every row in production today has all three fields NULL. The page
    must degrade to the short label and show no bullet or pitch block."""
    run, assessment = await _seed(db_session)
    assessment.company_or_project = "ONLY-LABEL-MARKER"
    assessment.headline = None
    assessment.elevator_pitch = None
    assessment.key_points = None
    await db_session.flush()

    html = (await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text
    assert "ONLY-LABEL-MARKER" in html
    assert "assessment-brief-pitch" not in html
    assert "assessment-brief-points" not in html


async def test_the_panel_banner_is_never_inside_a_collapsed_details(
    client, db_session, admin
):
    """N8/design §5. The panel banner is a WARNING, not evidence. Rendering a
    non-verified panel as unremarkable is a named failure mode in this repo;
    putting it behind a disclosure is the same error in a different place."""
    import re

    run, assessment = await _seed(db_session)
    await db_session.flush()

    html = (await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text
    # Everything inside any <details>...</details> must not contain the banner.
    inside = "".join(re.findall(r"<details\b.*?</details>", html, re.DOTALL))
    assert "Specialist panel" not in inside


async def test_a_non_empty_red_flag_list_is_never_collapsed(
    client, db_session, admin
):
    """Same reasoning: a disqualifier-grade flag behind a click is a flag the
    reviewer does not see."""
    import re

    run, assessment = await _seed(db_session)
    assessment.red_flags = ["RED-FLAG-MARKER-MUST-BE-VISIBLE"]
    await db_session.flush()

    html = (await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text
    inside = "".join(re.findall(r"<details\b.*?</details>", html, re.DOTALL))
    assert "RED-FLAG-MARKER-MUST-BE-VISIBLE" in html
    assert "RED-FLAG-MARKER-MUST-BE-VISIBLE" not in inside


async def test_the_rationale_is_collapsed_and_labelled_with_its_size(
    client, db_session, admin
):
    run, assessment = await _seed(db_session)
    assessment.rationale = "**A.** one\n\n**B.** two\n\n**C.** three"
    assessment.prose_format = "markdown"
    await db_session.flush()

    html = (await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text
    assert "Full rationale (3 paragraphs)" in html


async def test_the_jump_nav_lists_every_section(client, db_session, admin):
    run, assessment = await _seed(db_session)
    await db_session.flush()

    html = (await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text
    assert "assessment-jump-nav" in html
    for anchor in ("#brief", "#rationale", "#panel", "#scores", "#review", "#timeline"):
        assert f'href="{anchor}"' in html, anchor
```

Helper names verified 2026-09-09 against the real file, not assumed: it uses
`auth_headers` (imported from `tests.integration.test_manager_access` — there is
no local `_auth` here, unlike `test_opportunity_assessment_persistence.py`), and
its seeder is `_seed(db_session)` at line 57, which returns the tuple
`(run, assessment)`. It already defines `admin` and `manager` fixtures. Do not
add a second seeder.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv-test/bin/python -m pytest tests/integration/test_assessment_detail_page.py -v -k "brief or jump_nav or collapsed or never_inside"
```

Expected: FAIL — `assessment-brief` is not in the HTML.

- [ ] **Step 3: Add the brief and the jump nav**

At the very top of `templates/admin/_assessment_detail_body.html`, after the
`{% set a = assessment %}` line and the `signal_chip` map, insert:

```jinja
{# ---------------------------------------------------------------------------
   Reviewer brief (design §5). The reviewer's job is to judge the PROPOSAL, and
   the page used to open with the machinery of the verdict. Headline, pitch and
   key points come from the sidecar (0043) and are NULL on every row written
   before it — deliberately never backfilled — so every block here is
   conditional and the headline falls back to the short label. A pre-0043 row
   must render exactly as informative as it did before, with no empty states.
   --------------------------------------------------------------------------- #}
<nav class="assessment-jump-nav sticky top-0 z-10 -mx-2 mb-4 flex flex-wrap gap-x-4 gap-y-1 bg-white/95 px-2 py-2 text-xs text-gray-500 backdrop-blur border-b border-gray-100">
    <a class="hover:text-indigo-600" href="#brief">Brief</a>
    <a class="hover:text-indigo-600" href="#rationale">Rationale</a>
    <a class="hover:text-indigo-600" href="#panel">Panel</a>
    <a class="hover:text-indigo-600" href="#scores">Scores</a>
    <a class="hover:text-indigo-600" href="#review">Review</a>
    <a class="hover:text-indigo-600" href="#timeline">Timeline</a>
</nav>

<div id="brief" class="assessment-brief bg-white rounded-xl shadow-sm border border-gray-200 p-6 mb-6">
    <h2 class="text-xl font-semibold text-gray-900">{{ a.headline or a.company_or_project or "—" }}</h2>
    {% if a.headline and a.company_or_project %}
    <div class="assessment-brief-label text-sm text-gray-500 mt-1">{{ a.company_or_project }}</div>
    {% endif %}
    {% if a.elevator_pitch %}
    <div class="mt-4">
        <div class="text-xs font-semibold text-gray-500 uppercase">In one minute</div>
        {# Same write-time `prose_format` stamp and the same safe fallback as
           Rationale below: NULL means legacy plain text, which may contain a
           literal `*` in a scientific identifier that a markdown pass would
           corrupt by reading as emphasis. #}
        {% if a.prose_format == 'markdown' %}
        <div class="assessment-brief-pitch md-content mt-1 text-sm text-gray-700" data-markdown="{{ a.elevator_pitch | e }}"></div>
        {% else %}
        <p class="assessment-brief-pitch mt-1 text-sm text-gray-700 whitespace-pre-line">{{ a.elevator_pitch }}</p>
        {% endif %}
    </div>
    {% endif %}
    {% if a.key_points %}
    <div class="mt-4">
        <div class="text-xs font-semibold text-gray-500 uppercase">Key points</div>
        <ul class="assessment-brief-points mt-1 list-disc list-inside text-sm text-gray-700 space-y-0.5">
            {% for point in a.key_points %}<li>{{ point }}</li>{% endfor %}
        </ul>
    </div>
    {% endif %}
</div>
```

- [ ] **Step 4: Move the ask up and collapse the evidence**

Move the existing **Recommended next experiment** block so it sits immediately
after the brief `<div>` above, and change its heading to
`The ask`, keeping its `{% if a.recommended_next_experiment %}` guard and both
`prose_format` branches exactly as they are.

Wrap these four existing blocks in `<details>`, adding the matching `id` to
each wrapper:

| block | wrapper |
|---|---|
| Rationale card | `<details id="rationale" class="mb-6"><summary class="cursor-pointer text-sm font-semibold text-gray-700 uppercase">Full rationale ({{ a.rationale.split('\n\n') \| length }} paragraph{{ '' if a.rationale.split('\n\n') \| length == 1 else 's' }})</summary>` |
| Gating criteria card | `<details id="gating" class="mb-6"><summary class="cursor-pointer text-sm font-semibold text-gray-700 uppercase">Gating criteria</summary>` |
| Dimension scores card | `<details id="scores" class="mb-6"><summary class="cursor-pointer text-sm font-semibold text-gray-700 uppercase">Dimension scores</summary>` |
| Interview timeline card | `<details id="timeline" class="mb-6"><summary class="cursor-pointer text-sm font-semibold text-gray-700 uppercase">Interview timeline</summary>` |

The Gating and Red flags cards currently share one
`grid grid-cols-1 lg:grid-cols-2` wrapper — **split them.** Gating goes inside
the `<details id="gating">`; the Red flags card comes OUT of the grid and stays
a plain, always-expanded card. Add `id="review"` to the Human-review card's
outer `<div>` and `id="panel"` to the panel-status banner `<div>`.

```jinja
{# N8. TWO blocks deliberately stay uncollapsed: the panel-status banner and a
   non-empty red-flag list. Both are WARNINGS, not evidence. This repo already
   treats "a non-verified panel rendered as unremarkable" as a named failure
   mode — panel_state's terminal {% else %} is the neutral box for exactly that
   reason — and a disqualifier-grade red flag behind a disclosure is the same
   error in a different place. Pinned by
   test_the_panel_banner_is_never_inside_a_collapsed_details and
   test_a_non_empty_red_flag_list_is_never_collapsed. #}
```

Put that comment above the panel banner. The Red flags card keeps its existing
`{% if a.red_flags %}` / `{% else %}` branches; only its grid parent changes.

- [ ] **Step 5: Run the tests to verify they pass**

```bash
.venv-test/bin/python -m pytest \
  tests/integration/test_assessment_detail_page.py \
  tests/integration/test_assessment_review_ui.py -v
```

Expected: all PASS, including the pre-existing
`test_admin_detail_page_renders_the_whole_verdict` and
`test_an_unknown_panel_state_never_renders_green`.

- [ ] **Step 6: Commit**

```bash
git add templates/admin/_assessment_detail_body.html tests/integration/test_assessment_detail_page.py
git commit -m "feat(assessments): reviewer brief first, dense evidence collapsed

Headline, elevator pitch, key points and the ask lead the detail page; the full
rationale, gating and dimension scores move behind disclosures, with the
rationale's summary labelled by paragraph count so its size is visible.

The panel-status banner and any non-empty red-flag list stay uncollapsed and
are pinned that way: both are warnings, and hiding a warning behind a click is
the same defect as rendering an unvetted panel green.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 11: Elevator pitch in the #assessments-summary headline

Implements spec §6 (decision N12). **This is the only unretractable part of the
change** — a Slack post cannot be recalled.

**Files:**
- Modify: `src/services/assessment_headline.py` (module docstring; `render_assessment_headline` at line 61)
- Modify: `src/agent/simulation.py:3783` (the engine call site)
- Modify: `scripts/backfill_assessment_headlines.py:279` (`_render_for`)
- Modify: `CLAUDE.md` (the D12 field list in the BlackbirdBot section)
- Test: `tests/unit/test_assessment_headline_render.py`
- Test: `tests/unit/test_claude_md_disclosure_sync.py`
- Test: `tests/unit/test_assessments_summary_post.py` (D12 sentinel — `test_the_headline_leaks_no_rationale_red_flags_gating_or_raw_verdict` at `:118`)
- Test: `tests/unit/test_backfill_assessment_headlines.py`, `tests/integration/test_assessment_headline_delivery.py` (re-run only)

**Interfaces:**
- Consumes: `OpportunityAssessment.elevator_pitch` (Task 2), the sidecar's `elevator_pitch` (Task 7).
- Produces: `render_assessment_headline(..., elevator_pitch: object = None)` — a keyword-only parameter defaulting to `None`, so every existing caller and test keeps its current output byte for byte.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_assessment_headline_render.py`:

```python
def test_the_pitch_renders_as_a_second_line():
    text = render_assessment_headline(
        pi_label="Yarchoan lab",
        project="Th17/IL-8 plasma cytokine score",
        recommendation="conditional",
        scores={},
        permalink=None,
        elevator_pitch="Hopkins has 39-plex cytokine data on 124 ICI patients.",
    )
    assert "Hopkins has 39-plex cytokine data" in text
    assert text.count("\n") == 1


def test_an_absent_pitch_is_byte_identical_to_the_old_output():
    """A6/A3. Every row in production today has elevator_pitch IS NULL, and the
    repair script shares this renderer — so the widening must be invisible for
    a row that carries no pitch, or re-running the backfill would change what
    it posts for rows it has already handled."""
    common = dict(
        pi_label="Yarchoan lab",
        project="Th17/IL-8 plasma cytokine score",
        recommendation="conditional",
        scores={},
        permalink=None,
    )
    assert render_assessment_headline(**common) == render_assessment_headline(
        **common, elevator_pitch=None
    )
    assert "\n" not in render_assessment_headline(**common)


def test_a_non_string_pitch_is_dropped_not_repr_posted():
    """`_clip` drops a non-string outright: a model that answers with an object
    must not have a Python repr posted to a workspace-visible channel."""
    text = render_assessment_headline(
        pi_label="L", project="P", recommendation="pass", scores={}, permalink=None,
        elevator_pitch={"not": "a string"},
    )
    assert "not" not in text
    assert "\n" not in text


def test_an_overlong_pitch_is_clipped():
    from src.services.assessment_headline import PITCH_DISPLAY_CHARS

    text = render_assessment_headline(
        pi_label="L", project="P", recommendation="pass", scores={}, permalink=None,
        elevator_pitch="x" * (PITCH_DISPLAY_CHARS + 500),
    )
    assert "x" * PITCH_DISPLAY_CHARS in text
    assert "x" * (PITCH_DISPLAY_CHARS + 1) not in text
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv-test/bin/python -m pytest tests/unit/test_assessment_headline_render.py -v
```

Expected: FAIL — `render_assessment_headline() got an unexpected keyword argument 'elevator_pitch'`.

- [ ] **Step 3: Add the sixth field**

In `src/services/assessment_headline.py`, add the bound next to the other two:

```python
# This post's own display bound for the pitch, the same reasoning as
# PROJECT_DISPLAY_CHARS above: `elevator_pitch` is an unbounded Text column and
# a headline is not the place for a wall of model prose. Generous enough for the
# 3-5 sentences the contract asks for; the full text is on the detail page the
# permalink's reader can reach.
PITCH_DISPLAY_CHARS = 600
```

Add the parameter and the line:

```python
def render_assessment_headline(
    *,
    pi_label: str,
    project: object,
    recommendation: object,
    scores: object,
    permalink: str | None,
    score: float | None = None,
    band: str | None = None,
    elevator_pitch: object = None,
) -> str:
```

and, immediately before the `return`:

```python
    # Sixth field (2026-09-09), a deliberate widening of design D12's five.
    # OMITTED ENTIRELY when absent, exactly as the band/score segment is for an
    # empty `scores` map — every row written before 0043 has NULL here and is
    # deliberately never backfilled, so a repaired headline for one of those
    # rows must be byte-identical to what this function produced before the
    # widening. `_clip` drops a non-string outright, so a model that answers
    # with an object cannot have a Python repr posted to a channel humans read.
    pitch_text = _clip(elevator_pitch, PITCH_DISPLAY_CHARS)
    pitch_part = f"\n{pitch_text}" if pitch_text else ""
    return (
        f":mag: {pi_label} — {project_text} → *{display}*{score_part}{link_part}"
        f"{pitch_part}"
    )
```

Update the module docstring's content-policy paragraph: it currently states
"Exactly five fields are ever rendered" — make it six and name the pitch,
recording that this widening rests on the operator's assertion that PIs cannot
join the Slack workspace (design §0.2, A6).

- [ ] **Step 4: Wire both call sites**

`src/agent/simulation.py:3783`:

```python
            text = render_assessment_headline(
                pi_label=pi_label,
                project=verdict.get("company_or_project"),
                recommendation=verdict.get("recommendation"),
                scores=verdict.get("scores"),
                permalink=permalink,
                elevator_pitch=verdict.get("elevator_pitch"),
            )
```

`scripts/backfill_assessment_headlines.py`, in `_render_for`:

```python
        score=row.weighted_score,
        band=row.band,
        elevator_pitch=row.elevator_pitch,
```

- [ ] **Step 5: Update CLAUDE.md and its drift alarm**

In `CLAUDE.md`'s BlackbirdBot section, the sentence describing the
`#assessments-summary` post currently reads:

> a headline-only line (PI/lab name, `company_or_project`, `recommendation`,
> band/score, and a permalink or `(link unavailable)`) to `#assessments-summary`
> ... deliberately with **no** rationale, red flags, gating, or `raw_verdict`
> (design D12)

Replace with:

> a headline line (PI/lab name, `company_or_project`, `recommendation`,
> band/score, a permalink or `(link unavailable)`, and — since 2026-09-09 — the
> sidecar's `elevator_pitch` on a second line) to `#assessments-summary`
> ... deliberately with **no** rationale, red flags, gating, or `raw_verdict`
> (design D12, widened once). The pitch is a SIDECAR field and may carry the
> PI's unpublished disclosures; publishing it rests on the operator's assertion
> (2026-09-09) that PIs cannot join the workspace, which no code enforces —
> `SLACK_INVITE_URL` (`src/routers/agent_page.py:37`) still renders a join link
> on every PI's own `/agent` page. The pitch segment is omitted entirely when
> `elevator_pitch` is NULL, which is every row written before migration `0043`.

Then run the drift alarm and update its field list to match:

```bash
.venv-test/bin/python -m pytest tests/unit/test_claude_md_disclosure_sync.py -v
```

Read the failure before editing the test: `test_the_inline_field_list_still_matches_the_guidance`
is a two-directional control on this module's own vocabulary, and
`test_claude_md_does_not_claim_the_inline_verdict_fields_are_hidden` is the
finding from RCA §1. **Neither may be weakened** — only the field list they
compare against moves.

- [ ] **Step 6: Run the tests to verify they pass**

```bash
.venv-test/bin/python -m pytest \
  tests/unit/test_assessment_headline_render.py \
  tests/unit/test_assessments_summary_post.py \
  tests/unit/test_backfill_assessment_headlines.py \
  tests/integration/test_assessment_headline_delivery.py \
  tests/unit/test_claude_md_disclosure_sync.py -v
```

(Those four are the complete set of headline tests, enumerated 2026-09-09 —
there is no `test_assessment_headline_backfill.py`.)

Expected: all PASS. `test_assessments_summary_post.py` is D12's sentinel test —
if it fails on content it did not expect, stop and read it: the widening is
supposed to be exactly one line, and only when a pitch exists.

- [ ] **Step 7: Commit**

```bash
git add src/services/assessment_headline.py src/agent/simulation.py \
        scripts/backfill_assessment_headlines.py CLAUDE.md \
        tests/unit/test_assessment_headline_render.py \
        tests/unit/test_claude_md_disclosure_sync.py
git commit -m "feat(assessments): carry the elevator pitch in the Slack headline

A deliberate widening of design D12 from five rendered fields to six. Omitted
entirely when the pitch is NULL, so every pre-0043 row — and any headline the
repair script re-renders for one — is byte-identical to before.

The pitch is a sidecar field and may carry unpublished disclosures; this rests
on the operator's assertion that PIs cannot join the workspace, recorded in
CLAUDE.md and in the design doc because no code enforces it.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 12: The `0043` deploy box, and the full gate

Implements spec §7.1. Documentation plus the one run that decides whether any
of this ships.

**Files:**
- Modify: `CLAUDE.md` (a new deploy box after the `0041` box)

- [ ] **Step 1: Add the deploy box**

Insert after the `0041_assessment_summary_posted_at` box in `CLAUDE.md`,
matching the house style of the `0037`/`0038`/`0040`/`0041` boxes:

```markdown
> **Deploy order for `0043_assessment_narrative_and_review_dimension_scores` —
> migrate BEFORE the new code serves.** `0043` is six additive nullable columns
> across two tables (`opportunity_assessments.headline` / `.key_points` /
> `.elevator_pitch`; `assessment_reviews.dimension_scores` / `.rubric_version` /
> `.rubric_content_hash`), so *old code against the new schema* is safe. The
> reverse fails in both directions at once. READ side: the new code **maps all
> six**, so against a pre-`0043` database every `select(OpportunityAssessment)`
> — both assessment list pages, both detail pages — and every
> `select(AssessmentReview)` — the detail pages' feedback list and
> `review_bot`'s own load — raises `UndefinedColumn`. WRITE side:
> `_persist_assessment`'s INSERT names the three narrative columns, so **every
> verdict write of a running simulation fails** — and that write is
> best-effort, so the failure is swallowed and one ERROR line lands in a log
> nobody is tailing while the Slack replies keep looking completely normal.
> That is the silent half, and it is the same shape as the 2026-08-06 near-miss
> this file already records.
>
>     DC="docker compose -f docker-compose.prod.yml"
>     $DC build blackbird-app worker
>     $DC --profile agent build agent
>     $DC run --rm blackbird-app alembic upgrade head
>     $DC run --rm blackbird-app alembic current      # must equal `alembic heads`
>     $DC up -d blackbird-app worker
>     $DC up -d agent                                 # supervisor returns IDLE
>
> The agent rebuild is **not optional and not interchangeable with the prompt
> mount**. `prompts/` is bind-mounted, `src/` is baked. Prompt without image
> means the hub emits `headline`/`key_points`/`elevator_pitch` and
> `_persist_assessment` discards them — they survive only inside `raw_verdict`.
> Image without prompt means every new row writes NULL.
>
> All three narrative columns are NULL on every pre-`0043` row and are
> **deliberately never backfilled**: those verdicts were never asked for a
> headline, and a generated one would be indistinguishable from one the hub
> wrote. Every read path degrades — `headline` falls back to
> `company_or_project`, absent bullets and pitch render nothing, and the
> `#assessments-summary` headline omits the pitch segment entirely. Expect the
> new card list and the new detail brief to look, for the 12 rows currently on
> record, almost exactly like the pages they replaced; the narrative half
> arrives with the first interview a rebuilt agent concludes.
>
> Production is stamped `0042`, so this box applies to the next deploy.
```

- [ ] **Step 2: Run the whole gate**

```bash
./scripts/ci.sh
```

Expected: alembic single-head and a clean upgrade→downgrade→upgrade round trip
against the throwaway Postgres, zero ruff findings in `tests/`, `src/` under
its ceiling, and the full pytest run above the coverage floor.

**If the round trip fails on `0043`'s `downgrade()`, do not weaken it** — six
`drop_column` calls in reverse order is the correct inverse of six
`add_column`s, so a failure means something else in the chain moved.

- [ ] **Step 3: Verify the prompt docs are still in sync**

```bash
.venv-test/bin/python scripts/sync_prompt_set_docs.py --check
```

Expected: no drift reported.

- [ ] **Step 4: Confirm the compose file was never touched**

```bash
git status --short docker-compose.prod.yml
```

Expected: exactly ` M docker-compose.prod.yml` — modified, unstaged,
uncommitted, as it was before this work began. **If it is staged or committed,
undo that**: its working-tree-only edits are what keep this stack off the
unrelated `copi-python` deployment sharing this host.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: deploy box for 0043

Six additive nullable columns; migrate before the new code serves. The write
side is the silent half — _persist_assessment names three of them, and that
write is best-effort, so a start-before-migrate loses every verdict of the run
while Slack keeps looking normal.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Post-deploy verification (not code)

Neither of these is provable by a test; both are named in the spec.

- [ ] **Provision a reviewer account and walk the path signed in as it** (A12,
  spec §1.3). Production has zero `user_role='reviewer'` accounts, so every
  claim about the reviewer path currently rests on tests alone. Use
  `/admin/users/{id}` → Account Type, or
  `docker compose -f docker-compose.prod.yml exec blackbird-app python -m src.cli role:set --orcid <id> --role reviewer`.
  **Do not verify by impersonating** — that shows no write forms, which is the
  exact confusion this whole change started from.
- [ ] **Re-confirm the N12 premise before the first post-deploy verdict
  concludes** (A6): that PIs genuinely cannot join the Slack workspace. The
  first headline carrying an elevator pitch is unretractable.

---

## Plan audit, 2026-09-09

Everything below was checked against the working tree and the running
deployment, not from memory. Eight defects were found in the first draft of
this plan and are already corrected above; they are recorded here because two
of them are the kind that would have shipped green.

### Defects found and fixed

| # | Defect | How it was caught | Severity |
|---|---|---|---|
| P1 | `_row_slice` (`test_assessment_queue_controls.py:519`) splits on `</tr>`. With no table, `str.split` returns a one-element list and the helper yields **the whole rest of the page** — four row-scoped assertions keep PASSING while asserting nothing. | Simulated the helper against card markup: the slice contained the *next* row's content. | **Silent false green** |
| P2 | `test_manager_views.py:534` pins `"panel</span>"`; the card relabels that badge `panel incomplete`, so the literal disappears. | Direct substring comparison of old vs new markup. | Red test |
| P3 | Task 8 invented a helper `build_engine_for_persist` that does not exist, and read the row back through `db_session` — a different transaction from the engine's own `session_factory`, so it would never see the commit. | `grep` for the helper; read the real harness at `test_opportunity_assessment_persistence.py:170-192`. | Would not run |
| P4 | Task 10 used `_auth(...)` and `_seed_assessment(...)`. `test_assessment_detail_page.py` has neither: it imports `auth_headers` and its seeder is `_seed()` returning `(run, assessment)`. | `grep` for both helper names in that file. | Would not run |
| P5 | Task 11 pointed at `tests/integration/test_assessment_headline_backfill.py`, which does not exist. The real files are `tests/unit/test_backfill_assessment_headlines.py` and `tests/integration/test_assessment_headline_delivery.py`. | Enumerated `tests/**/*headline*`. | Wrong command |
| P6 | Task 4 appended a module-level `import` to the bottom of a test file. `E402` is in this repo's ruff rule set (`select = ["E","F","I","UP","B"]`) and the test suite is linted at **zero** findings. | Read `[tool.ruff.lint]` in `pyproject.toml`. | CI failure |
| P7 | Six handler line numbers were wrong in **both** documents — `reviews.py` feedback/status/assign/unassign are at `:117`/`:194`/`:218`/`:239`, not `:110`/`:186`/`:196`/`:220`. | `grep -n '^@router.post\|^async def'`. | Misleading |
| P8 | A10's mitigation (the "top N of TOTAL" note) had no task. It lives in the wrappers, not the body, so it survives by accident — now stated so a later edit cannot remove it unknowingly. | Spec-coverage pass, section by section. | Coverage gap |

### Claims verified as correct

- **All 66 distinct file references** across both documents resolve to real
  files, and **no cited line number is past its file's end**. The only
  unresolved paths are the three files this plan CREATES plus
  `scripts/backfill_assessment_summaries.py`, which the spec names as an
  explicitly out-of-scope follow-up.
- **Import boundary.** `assessment_reviews` may import `blackbird_rubric`: it is
  imported only by `src/routers/reviews.py` and `src/services/directory.py`,
  both web tier. `src/worker/main.py` imports `review_bot`, `profile_pipeline`,
  `config` and `models` — never `assessment_reviews`. The AST probe in
  `tests/unit/test_review_bot_inputs.py:12` is scoped to
  `interview_transcript.py` alone, which Task 6 does not touch.
- **`FormData.multi_items()` exists** (Starlette 1.4.1) and `Request` caches the
  parsed form, so Task 4's `await request.form()` alongside FastAPI's own
  `Form(...)` parameters reads the same cached body rather than a consumed
  stream.
- **JSONB comparison compiles correctly**: `dimension_scores == {...}` renders
  `= %(param)s::JSONB` (Postgres jsonb equality, key-order independent) and
  `.is_(None)` renders `IS NULL`. Task 6's null-safe branch is therefore
  necessary and sufficient.
- **Jinja escapes work as assumed**: `{{ s.split('\n\n') | length }}` returns 3
  for a three-paragraph string, so Task 10's paragraph count is real.
- **`asyncio_mode = "auto"`**, so no `@pytest.mark.asyncio` decorator is needed
  on the new async tests.
- **No test pins `build_assessment_detail`'s context-key set**, so Task 5's two
  new keys break nothing.
- **Both assessment-DETAIL routes splat `**detail`** (`admin.py:875`,
  `manager.py:381`), so Task 5's keys reach both surfaces; only the LIST route
  allowlists, which is why Task 9 adds no key (A2).
- **The other three panel badges and the red-flag counts are pinned by exact
  string and by count** (`test_assessment_queue_controls.py:340-350`,
  `:397-399`, `:448-455`; `test_opportunity_assessment_persistence.py:1302`,
  `:1505`, `:2370`) and are preserved verbatim by the card markup.
- **Model exports** `USER_ROLE_REVIEWER`, `AssessmentReview`, `SimulationRun`,
  `OpportunityAssessment` all resolve from `src.models`.
- **`test_assessment_review_ui.py` already defines** `admin`, `manager` and
  `reviewer` fixtures, `BASE_TIME`, and imports `AssessmentReview` and
  `_seed_assessment` — Task 5's tests add no fixture.
- **Rubric scale is 1–5 and six dimensions**, read from
  `prompts/rubric/blackbird-rubric.toml` `[scale]` and the six `[[dimension]]`
  tables, so the form's `range(scale_min, scale_max + 1)` yields 1..5.

### Spec coverage

Every spec section maps to a task: §1→T1, §2.1→T2, §2.2→T3+T4, §2.3→T5,
§2.4→T6, §3.1→T2, §3.2→T7, §3.3→T8, §4→T9, §4.1→T9, §5→T10, §6→T11, §7→T2,
§7.1→T12, §8→distributed. Every adversarial finding maps to a mitigation:
A1→T6, A2→T9's box, A3→T9/T10 degradation tests, A4→T8 warnings, A5→T7 prompt
cap, A6→T11 docs + post-deploy check, A7→T12 box, A8→T5/T10 ordering, A9→T5
labelling, A10→T9 note, A11→T9 narrowed pin, A12→post-deploy check, A13→T5
provenance, A14→T2 tests, A15→accepted with no action, A16→T12, A17→T7,
A18→T7, A19→T11, A20→T8.

### Residual risks this plan does NOT eliminate

1. **Prompt compliance is unverifiable in code** (A4). Nothing can check that a
   headline tells the whole story or that a bolded sentence actually summarises
   its paragraph. The shape checks are WARNINGs and the fields degrade to
   today's rendering.
2. **The first post-deploy headline carrying a pitch is unretractable** (A6),
   and its safety rests on an operator assertion no code enforces.
3. **The narrative half of this work shows nothing until a rebuilt agent
   concludes an interview** (A3). Every one of the 12 rows on record renders
   with today's content in the new shape.
4. **`test_the_headline_leaks_no_rationale_red_flags_gating_or_raw_verdict`
   scans the WHOLE post text for the substrings `rationale`, `confidence`,
   `gating`, `milestone`.** Once the pitch is in the post, any future fixture
   whose pitch prose happens to contain one of those ordinary English words
   will fail that test for a reason that is not a leak. Not a runtime risk —
   there is no such check in production — but the next person to touch that
   fixture should know why it tripped.
5. **The suite does not run production's Anthropic SDK**, and none of this
   change's tests reach a real model. Prompt-contract changes (Task 7) are
   verified by shape, never by behaviour.
