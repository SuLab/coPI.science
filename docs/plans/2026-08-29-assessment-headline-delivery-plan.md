# Assessment Headline Delivery — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every `opportunity_assessments` row whose interview has ended gets
exactly one `#assessments-summary` headline, exactly once, and the fact that it
posted survives a process restart.

**Architecture:** Announcement stops being a side effect of one particular
*reply* and becomes a property of the *interview ending*. A new durable column
`opportunity_assessments.summary_posted_at` is the at-most-once record; a
queue-and-drain path (modelled on the existing `_pending_memory_events`
idiom, so no Slack I/O happens under `_close_thread`'s locks) announces any
verdict an ended interview still owes; `stop()` sweeps whatever is left,
because a run ending means no later turn will ever come. A one-off repair
script re-posts headlines already lost.

**Tech Stack:** Python 3.12, SQLAlchemy 2 async, Alembic, FastAPI, pytest +
pytest-asyncio, testcontainers Postgres, Slack Web API.

**Spec:** `docs/audits/2026-08-29-lost-assessment-headlines/README.md` — read it
first. §5 states the invariant this plan implements; §1–3 are the confirmed
evidence and the adversarial audit behind every design choice here.

## Global Constraints

- **Run the suite on the HOST, never through the sshfs mount.**
  `.venv-test/bin/python -m pytest tests/ -v`. A run through the mount is
  100–400x slower. **Never** `pip install` against `.venv-test` from a client
  mounting the repo over sshfs — it corrupts the console-script shebangs.
- **`./scripts/ci.sh` is the entire gate.** There is no server-side CI. It must
  pass before any commit is pushed: alembic single-head + no duplicate revision
  ids, an upgrade→downgrade→upgrade round trip, `ruff check` on `tests/` at zero
  findings plus a ratcheted ceiling on `src/`, then the full pytest run with a
  branch-coverage floor.
- **Alembic head is `0040`.** This plan adds exactly one revision, **`0041`**,
  with `down_revision = "0040"`. Do not create a second head.
- **Never regenerate `tests/characterization/__snapshots__/test_agent_turn_gm.ambr`.**
  No task here touches `src/agent/thread_guidance.py` or anything under
  `prompts/`, so the golden master must not move. If it does, you have changed
  something you were not asked to change — revert, do not `--snapshot-update`.
- **`_post_assessment_summary` renders five fields and no more** — PI/lab name,
  project, recommendation, band/score, permalink (design D12). Never
  interpolate `verdict` wholesale, never add a "why" line, never read
  `rationale` / `red_flags` / `gating` / `raw_verdict`. This is a content
  policy, not formatting, and it is pinned by a sentinel test.
- **Never DELETE from `simulation_runs`** and never purge
  `opportunity_assessments` — every run-produced table cascades from the run
  row, and human review rows cascade from the assessment row.
- **A headline is an unretractable public Slack post.** Every new code path
  must be at-most-once across a restart; when in doubt, do not post.
- Existing `pass` → displayed as `decline` (rubric `banding.pass_label`). Keep
  that mapping exactly as it is.

---

## File Structure

**Create**

| Path | Responsibility |
|---|---|
| `alembic/versions/0041_assessment_summary_posted_at.py` | One additive nullable `TIMESTAMPTZ` column. No backfill. |
| `src/services/assessment_headline.py` | The pure headline renderer, extracted so the engine and the repair script cannot render differently. Dependency-free apart from `blackbird_rubric`. |
| `scripts/backfill_assessment_headlines.py` | Operator-gated repair for headlines already lost. Dry-run by default. |
| `tests/unit/test_assessment_headline_render.py` | Unit tests for the pure renderer. |
| `tests/integration/test_assessment_headline_delivery.py` | The end-to-end delivery invariant, driven through the real `_reply_to_thread` / `_close_thread` / `stop()` paths. |

**Modify**

| Path | Change |
|---|---|
| `src/models/opportunity.py` | Map `summary_posted_at`. |
| `src/agent/simulation.py` | `_post_assessment_summary` returns `bool` and delegates rendering; new `_mark_summary_posted`, `_announce_owed_headline`, `_drain_pending_headlines`, `_pending_headlines`; hooks in `_close_thread`, `_drain_and_flush`, `stop()`, `_capture_hub_assessment`; durable read in `_rehydrate_assessed_threads`. |
| `tests/unit/test_assessments_summary_post.py` | Assert the new `bool` return; keep the D12 sentinel. |
| `CLAUDE.md` | Deploy-order box for `0041`; correct the "one caller" claim about `_post_assessment_summary`. |

---

### Task 1: The durable at-most-once column

**Files:**
- Create: `alembic/versions/0041_assessment_summary_posted_at.py`
- Modify: `src/models/opportunity.py:90` (insert after `prose_format`)
- Test: `tests/integration/test_assessment_headline_delivery.py`

**Interfaces:**
- Produces: `OpportunityAssessment.summary_posted_at: Mapped[datetime | None]`
  — NULL means "no headline has been posted for this row". Every later task
  reads or writes this column.

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_assessment_headline_delivery.py`:

```python
"""Every assessment whose interview has ENDED gets exactly one
#assessments-summary headline, exactly once, durably.

See docs/audits/2026-08-29-lost-assessment-headlines/README.md. Production run
61ccad6d lost the rothstein verdict's headline (conditional, 2.85 — the run's
highest score) because the interview ended by `max_thread_messages` timeout
instead of by a terminal reply, and nothing announces on that path.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.models import OpportunityAssessment, SimulationRun

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_summary_posted_at_defaults_to_null(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        run = SimulationRun()
        db.add(run)
        await db.commit()
        run_id = run.id
    try:
        async with factory() as db:
            row = OpportunityAssessment(
                simulation_run_id=run_id, agent_id="blackbird",
                channel_name="general", thread_id="t1",
            )
            db.add(row)
            await db.commit()
            row_id = row.id
        async with factory() as db:
            stored = (await db.execute(
                select(OpportunityAssessment).where(OpportunityAssessment.id == row_id)
            )).scalar_one()
            assert stored.summary_posted_at is None, (
                "a fresh verdict has not been announced"
            )
    finally:
        async with factory() as db:
            stale = (await db.execute(
                select(SimulationRun).where(SimulationRun.id == run_id)
            )).scalar_one_or_none()
            if stale is not None:
                await db.delete(stale)
                await db.commit()
```

- [ ] **Step 2: Run it and watch it fail**

Run on the host: `.venv-test/bin/python -m pytest tests/integration/test_assessment_headline_delivery.py -v`

Expected: FAIL — `AttributeError: type object 'OpportunityAssessment' has no attribute 'summary_posted_at'`.

- [ ] **Step 3: Write the migration**

Create `alembic/versions/0041_assessment_summary_posted_at.py`:

```python
"""assessment summary_posted_at — the durable record that a headline posted

Additive and nullable, so OLD CODE AGAINST THE NEW SCHEMA IS SAFE. The reverse
is not: the new code MAPS this column, so every `select(OpportunityAssessment)`
— both assessment list pages, both detail pages — raises `UndefinedColumn`
against a pre-0041 database, and `_persist_assessment`'s INSERT names it, so
every verdict write fails too. Migrate BEFORE the new code serves; see the
deploy box in CLAUDE.md.

Deliberately NOT backfilled. NULL means "no headline has been posted for this
row", and for a pre-0041 row that is unknowable from the database alone — the
only record is the Slack channel. Guessing would manufacture exactly the claim
this column exists to make truthfully. `scripts/backfill_assessment_headlines.py
--stamp-only` is the operator path for marking a row whose headline is already
in Slack.

Revision ID: 0041
Revises: 0040
Create Date: 2026-08-29
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0041"
down_revision: Union[str, None] = "0040"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column(
        "opportunity_assessments",
        sa.Column("summary_posted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("opportunity_assessments", "summary_posted_at")
```

- [ ] **Step 4: Map the column**

In `src/models/opportunity.py`, immediately after the `prose_format` line
(`:90`) and its comment block, add:

```python
    # The durable half of the at-most-once headline guarantee (2026-08-29).
    # Set when a `#assessments-summary` headline for THIS row reaches Slack.
    # NULL is "not announced" and is never backfilled: for a pre-0041 row the
    # answer lives only in the Slack channel, and a guess here would be
    # indistinguishable from a measurement. `_rehydrate_assessed_threads`
    # reads it so a restart cannot re-announce a verdict already public, and
    # `_announce_owed_headline` reads it so the close path and the shutdown
    # sweep cannot both post the same one.
    summary_posted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
```

If `DateTime` or `datetime` is not already imported in that module, add
`from datetime import datetime` and include `DateTime` in the existing
`from sqlalchemy import ...` line.

- [ ] **Step 5: Run the test and the alembic round trip**

```bash
.venv-test/bin/python -m pytest tests/integration/test_assessment_headline_delivery.py -v
.venv-test/bin/python -m alembic heads   # must print exactly one head: 0041
```

Expected: test PASSES; exactly one head.

- [ ] **Step 6: Commit**

```bash
git add alembic/versions/0041_assessment_summary_posted_at.py src/models/opportunity.py tests/integration/test_assessment_headline_delivery.py
git commit -m "feat(assessments): 0041 adds summary_posted_at, the durable headline record"
```

---

### Task 2: Extract the headline renderer

The engine and the repair script must render byte-identical headlines. Extract
the pure part so there is one implementation, and so the D12 content policy is
testable without an engine.

**Files:**
- Create: `src/services/assessment_headline.py`
- Create: `tests/unit/test_assessment_headline_render.py`
- Modify: `src/agent/simulation.py:3382-3505` (`_post_assessment_summary`)
- Modify: `tests/unit/test_assessments_summary_post.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces:
  `render_assessment_headline(*, pi_label: str, project: object, recommendation: object, scores: object, permalink: str | None) -> str`
  — the complete Slack text, `:mag:`-prefixed. Task 8's repair script calls it
  directly.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_assessment_headline_render.py`:

```python
"""The `#assessments-summary` headline renders five fields and no more (D12)."""

import pytest

from src.services.assessment_headline import render_assessment_headline


def test_a_scored_verdict_renders_all_five_fields():
    text = render_assessment_headline(
        pi_label="Jeffrey Rothstein",
        project="CHMP7 / ESCRT-III–nuclear-pore-injury axis in ALS",
        recommendation="conditional",
        scores={"a": 3, "b": 3},
        permalink="https://slack.example/p1",
    )
    assert text.startswith(":mag: Jeffrey Rothstein — ")
    assert "CHMP7" in text
    assert "*conditional*" in text
    assert "band:" in text and "score:" in text
    assert "<https://slack.example/p1|View interview>" in text


def test_pass_is_displayed_as_decline():
    text = render_assessment_headline(
        pi_label="Wang", project="X", recommendation="pass",
        scores={"a": 1}, permalink=None,
    )
    assert "*decline*" in text
    assert "*pass*" not in text


def test_no_scores_omits_band_and_score_entirely():
    """An empty scores map is 'we don't know', not a 0.00 that bands as a
    decline nobody made — the same reason `_persist_assessment` leaves those
    columns NULL."""
    text = render_assessment_headline(
        pi_label="Wang", project="X", recommendation="conditional",
        scores={}, permalink=None,
    )
    assert "band:" not in text
    assert "score:" not in text


def test_a_missing_permalink_degrades_rather_than_dropping_the_post():
    text = render_assessment_headline(
        pi_label="Wang", project="X", recommendation="conditional",
        scores={}, permalink=None,
    )
    assert "(link unavailable)" in text


@pytest.mark.parametrize("bad", [None, 42, {"nested": "object"}, ""])
def test_a_non_string_project_degrades_to_untitled(bad):
    """A model that answers `company_or_project` with an object must not get a
    Python repr posted to a public channel."""
    text = render_assessment_headline(
        pi_label="Wang", project=bad, recommendation="conditional",
        scores={}, permalink=None,
    )
    assert "(untitled)" in text
    assert "nested" not in text


def test_an_overlong_project_is_clipped_to_a_headline():
    text = render_assessment_headline(
        pi_label="Wang", project="z" * 500, recommendation="conditional",
        scores={}, permalink=None,
    )
    assert "z" * 120 in text
    assert "z" * 121 not in text
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_assessment_headline_render.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.services.assessment_headline'`.

- [ ] **Step 3: Write the module**

Create `src/services/assessment_headline.py`:

```python
"""The one renderer for a `#assessments-summary` headline.

Extracted from `SimulationEngine._post_assessment_summary` on 2026-08-29 so the
engine and `scripts/backfill_assessment_headlines.py` cannot render differently
— a repaired headline that reads unlike a live one is worse than no repair,
because a reader cannot tell which rows were repaired.

**Content policy, not formatting (design D12).** Exactly five fields are ever
rendered: PI/lab name, project, recommendation, band/score, permalink. The
verdict's `rationale`, `red_flags`, `gating` and `raw_verdict` are never read
here at all, which is what keeps this post from saying more than the manager
read-only detail view already shows staff. Widening this — interpolating a
verdict wholesale, adding a "why" line — is a policy change requiring sign-off,
not a tidy-up.
"""

from __future__ import annotations

from src.services.blackbird_rubric import band as rubric_band
from src.services.blackbird_rubric import weighted_score as rubric_weighted_score

# This post's own display bound. `company_or_project` is a `Text` column with no
# width, so an unbounded value would turn a HEADLINE into a wall of model text
# that `split_for_slack` then cuts into several messages. The full title is
# always in the row and on the detail page the permalink's reader can reach.
PROJECT_DISPLAY_CHARS = 120
# `recommendation`'s own column width, so the post and the stored row can never
# disagree about it.
RECOMMENDATION_DISPLAY_CHARS = 30


def _clip(value: object, max_len: int) -> str | None:
    """A non-empty string clipped to ``max_len``, else ``None``.

    Drops a non-string outright: a model that answers `company_or_project` with
    an object would otherwise have a Python `repr` posted to a public channel.
    """
    if not isinstance(value, str) or not value:
        return None
    return value[:max_len]


def render_assessment_headline(
    *,
    pi_label: str,
    project: object,
    recommendation: object,
    scores: object,
    permalink: str | None,
) -> str:
    """Render the complete Slack text for one headline."""
    score_map = scores if isinstance(scores, dict) else {}
    if score_map:
        score = rubric_weighted_score(score_map)
        score_part = f" (band: {rubric_band(score)}, score: {score:.1f})"
    else:
        # An empty scores map is "we don't know", and `weighted_score({})` is a
        # 0.00 that bands as a decline nobody made — the same reason
        # `_persist_assessment` leaves those columns NULL.
        score_part = ""

    project_text = _clip(project, PROJECT_DISPLAY_CHARS) or "(untitled)"
    recommendation_text = (
        _clip(recommendation, RECOMMENDATION_DISPLAY_CHARS) or "unknown"
    )
    # Display form only — the stored verdict and every downstream engine
    # predicate keep writing "pass"; this headline is the one place a human
    # reads it, so it reads as "decline" (rubric banding.pass_label).
    display = "decline" if recommendation_text == "pass" else recommendation_text

    link_part = (
        f" — <{permalink}|View interview>" if permalink else " (link unavailable)"
    )
    return f":mag: {pi_label} — {project_text} → *{display}*{score_part}{link_part}"
```

Confirm the import names against `src/services/blackbird_rubric.py` — if the
public functions are already named `rubric_weighted_score` / `rubric_band`
there, import them under those names and drop the aliases.

- [ ] **Step 4: Run the unit tests**

Run: `.venv-test/bin/python -m pytest tests/unit/test_assessment_headline_render.py -v`
Expected: PASS (6 tests, one parametrized ×4).

- [ ] **Step 5: Delegate from the engine, and return a bool**

In `src/agent/simulation.py`, add to the imports:

```python
from src.services.assessment_headline import render_assessment_headline
```

Change `_post_assessment_summary`'s signature to
`-> bool` and replace its rendering block. The method becomes:

```python
    async def _post_assessment_summary(
        self, agent: Agent, thread: ThreadState, verdict: dict, slack_ts: str | None,
    ) -> bool:
```

Update the docstring's opening line to:

```
        """Post a headline-only summary of a concluded interview to the
        assessments-summary channel (design D12/D13/D14/D16). Returns True when
        a headline actually reached Slack — the caller stamps
        `opportunity_assessments.summary_posted_at` on that answer, so a False
        here must mean nothing was posted.
```

Then: every early `return` in the method becomes `return False`; the rendering
block from `scores = verdict.get("scores")` down to the `text = (...)`
assignment is replaced by

```python
            subject_agent_id = thread.other_agent_id
            pi = self.agents.get(subject_agent_id) if subject_agent_id else None
            pi_label = pi.pi_name if pi else (subject_agent_id or "Unknown lab")

            source_channel_id = self._channel_id_map.get(thread.channel)
            permalink = None
            if source_channel_id and slack_ts:
                # ... existing inner try/except for the permalink, UNCHANGED ...
                ...

            text = render_assessment_headline(
                pi_label=pi_label,
                project=verdict.get("company_or_project"),
                recommendation=verdict.get("recommendation"),
                scores=verdict.get("scores"),
                permalink=permalink,
            )
```

and the tail becomes

```python
            await client.apost_message(ASSESSMENTS_SUMMARY_CHANNEL, text)
            logger.info(
                "[%s] Posted #assessments-summary headline for %s (%s)",
                agent.agent_id, subject_agent_id or "?",
                verdict.get("recommendation"),
            )
            return True
        except Exception:
            logger.exception(
                "[%s] Failed to post assessments-summary headline for thread %s",
                agent.agent_id, thread.thread_id,
            )
            return False
```

Keep the inner permalink `try`/`except` and the `if not channel_id or not
client or not client.is_connected:` guard exactly as they are — only their
`return` values change to `False`. Delete the now-unused local `_bounded_str`
calls from this method only; leave the module-level `_bounded_str` alone, it
has other callers.

- [ ] **Step 6: Assert the new return value**

In `tests/unit/test_assessments_summary_post.py`, change the two calls that
represent success and skip to assert the return value. Add:

```python
async def test_a_posted_headline_reports_success(monkeypatch, tmp_path):
    eng, hub, lab, hub_client = _engine(monkeypatch, tmp_path)
    thread = ThreadState(thread_id="t1", channel="general", other_agent_id="wang")
    assert await eng._post_assessment_summary(hub, thread, VERDICT, "111.000") is True


async def test_an_unconfigured_channel_reports_failure(monkeypatch, tmp_path):
    """The caller must not stamp `summary_posted_at` when nothing was posted."""
    eng, hub, lab, hub_client = _engine(monkeypatch, tmp_path)
    eng._assessments_summary_channel_id = None
    thread = ThreadState(thread_id="t1", channel="general", other_agent_id="wang")
    assert await eng._post_assessment_summary(hub, thread, VERDICT, "111.000") is False
    assert ASSESSMENTS_SUMMARY_CHANNEL not in hub_client.posted_messages
```

- [ ] **Step 7: Run the affected suites**

```bash
.venv-test/bin/python -m pytest tests/unit/test_assessment_headline_render.py tests/unit/test_assessments_summary_post.py tests/integration/test_hub_assessment_capture_gate.py -v
```

Expected: all PASS. The D12 sentinel test in
`test_assessments_summary_post.py` must still pass unmodified — if it fails,
the extraction changed the rendered text and you have introduced a content
change.

- [ ] **Step 8: Commit**

```bash
git add src/services/assessment_headline.py src/agent/simulation.py tests/unit/test_assessment_headline_render.py tests/unit/test_assessments_summary_post.py
git commit -m "refactor(assessments): extract the headline renderer; _post_assessment_summary returns bool"
```

---

### Task 3: Stamp the column when a headline posts

**Files:**
- Modify: `src/agent/simulation.py` — add `_mark_summary_posted`; call it from
  `_capture_hub_assessment` (`:3326-3331`).
- Test: `tests/integration/test_assessment_headline_delivery.py`

**Interfaces:**
- Consumes: `OpportunityAssessment.summary_posted_at` (Task 1);
  `_post_assessment_summary(...) -> bool` (Task 2).
- Produces: `async def _mark_summary_posted(self, thread_id: str | None) -> None`
  — Tasks 4, 5 and 6 all call it.

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_assessment_headline_delivery.py`:

```python
from src.agent.channels import ASSESSMENTS_SUMMARY_CHANNEL
from tests.integration.test_hub_assessment_capture_gate import (  # noqa: F401
    _assessments,
    _delete_run,
    _drive_reply,
    _reply_with_sidecar,
)

# `phase4_guidance` takes the ORDINAL (message_count + 1). Seeding N prior
# messages makes the generated reply ordinal N+1.
_CONCLUDE_COUNT = 11     # ordinal 12 — the hub's own concluding turn
_LAST_DECIDE_COUNT = 10  # ordinal 11 — the turn that lost rothstein's headline


def _wire_summary_channel(sim):
    """Without this a headline is skipped for an unrelated reason
    (`channel_id=None, transport not connected`) and a delivery test passes
    while proving nothing. Production fills this in via
    `_ensure_assessments_summary_channel`."""
    sim._assessments_summary_channel_id = "C-SUMMARY"
    sim._channel_id_map[ASSESSMENTS_SUMMARY_CHANNEL] = "C-SUMMARY"
    sim._channel_id_map["single-cell-omics"] = "C_OMICS"


def _headlines(client):
    return [p for p in client.posted if p.get("channel") == ASSESSMENTS_SUMMARY_CHANNEL]


@pytest.mark.asyncio
async def test_a_posted_headline_is_recorded_on_the_row(engine, monkeypatch):
    sim, agent, thread, client, factory, run_id = await _drive_reply(
        engine, monkeypatch, _reply_with_sidecar(), prior_messages=_CONCLUDE_COUNT,
    )
    _wire_summary_channel(sim)
    try:
        # The ordinal-12 reply announces on its own. Re-drive the stamp for the
        # row the capture path just wrote.
        rows = await _assessments(factory, run_id)
        assert len(rows) == 1
        assert len(_headlines(client)) == 1, "the CONCLUDE turn announces"
        assert rows[0].summary_posted_at is not None, (
            "a posted headline is recorded durably, so a restart cannot re-post it"
        )
    finally:
        await _delete_run(factory, run_id)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv-test/bin/python -m pytest tests/integration/test_assessment_headline_delivery.py::test_a_posted_headline_is_recorded_on_the_row -v`
Expected: FAIL on the final assert — `summary_posted_at` is `None`.

- [ ] **Step 3: Implement `_mark_summary_posted`**

Add to `src/agent/simulation.py`, directly after `_post_assessment_summary`:

```python
    async def _mark_summary_posted(self, thread_id: str | None) -> None:
        """Record durably that this interview's headline is in Slack.

        Keyed by THREAD, not by row id, for two reasons that both bite in
        production. `_persist_assessment` returns `None` for a verdict that
        only reached `_pending_assessments`, so an id is not always available;
        and `_retire_superseded_verdict` DELETES the row a headline described
        and replaces it, so an id captured at post time can name a row that no
        longer exists. The thread is the stable identity of an interview, and
        the interview is what gets announced once.

        Also patches any copy still queued in `_pending_assessments`, so a row
        that has not landed yet carries the stamp when it does — otherwise the
        flush writes NULL over a headline that is already public.

        Best-effort and never raises: the headline is already in Slack by the
        time this runs. A failure here costs at-most-once across a restart, not
        the post.
        """
        if not thread_id:
            return
        now = datetime.now(UTC)
        for queued in self._pending_assessments:
            if queued.get("thread_id") == thread_id:
                queued["summary_posted_at"] = now
        if not self.session_factory or not self.simulation_run_id:
            return
        from sqlalchemy import update as sa_update

        try:
            async with self.session_factory() as db:
                await db.execute(
                    sa_update(OpportunityAssessment)
                    .where(
                        OpportunityAssessment.simulation_run_id
                        == self.simulation_run_id,
                        OpportunityAssessment.thread_id == thread_id,
                        OpportunityAssessment.summary_posted_at.is_(None),
                    )
                    .values(summary_posted_at=now)
                )
                await db.commit()
        except Exception as exc:  # noqa: BLE001 — the headline already posted
            logger.warning(
                "Posted the #assessments-summary headline for thread %s but "
                "could not record it on the row: %s. A restart of this run may "
                "post a second headline for the same interview.",
                thread_id, exc,
            )
```

- [ ] **Step 4: Call it from the capture path**

In `_capture_hub_assessment`, replace

```python
                    if announce:
                        await self._post_assessment_summary(
                            agent, thread, verdict, slack_ts,
                        )
```

with

```python
                    if announce:
                        if await self._post_assessment_summary(
                            agent, thread, verdict, slack_ts,
                        ):
                            await self._mark_summary_posted(thread.thread_id)
                        else:
                            # Nothing reached Slack. Leave `summary_posted_at`
                            # NULL so the close path, the shutdown sweep and the
                            # repair script can all still find this verdict —
                            # `_HeldVerdict.announced` alone would hide it from
                            # every one of them.
                            self._assessed_threads[thread.thread_id] = (
                                self._assessed_threads[thread.thread_id]
                                ._replace(announced=False)
                            )
                            logger.warning(
                                "[%s] The #assessments-summary headline for %s "
                                "did not post; the verdict is stored and will be "
                                "retried when the interview ends",
                                agent.agent_id, thread.other_agent_id or "?",
                            )
```

- [ ] **Step 5: Run the tests**

```bash
.venv-test/bin/python -m pytest tests/integration/test_assessment_headline_delivery.py tests/integration/test_hub_assessment_capture_gate.py tests/unit/test_assessments_summary_post.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/agent/simulation.py tests/integration/test_assessment_headline_delivery.py
git commit -m "feat(assessments): record summary_posted_at when a headline reaches Slack"
```

---

### Task 4: Announce when the interview ENDS

The core fix. `_close_thread` queues; the drain posts, outside the locks.

**Files:**
- Modify: `src/agent/simulation.py` — `__init__` (add `_pending_headlines`),
  `_close_thread` (`:2447`, inside the existing lock block, after the memory
  events are queued), `_drain_and_flush` (`:1116-1125`), plus two new methods.
- Test: `tests/integration/test_assessment_headline_delivery.py`

**Interfaces:**
- Consumes: `_mark_summary_posted` (Task 3), `_post_assessment_summary -> bool`
  (Task 2).
- Produces:
  - `self._pending_headlines: list[str]` — thread ids owing a headline.
  - `async def _announce_owed_headline(self, thread_id: str, *, trigger: str) -> bool`
  - `async def _drain_pending_headlines(self, *, limit: int | None = None) -> None`
  — Task 5 calls both.

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_assessment_headline_delivery.py`:

```python
from src.agent.message_log import LogEntry


@pytest.mark.asyncio
async def test_a_timed_out_interview_still_announces_its_verdict(engine, monkeypatch):
    """Production run 61ccad6d, rothstein: the hub concluded at ordinal 11
    (DECIDE, because a 4181-char PI reply had been split into two Slack
    messages), the thread hit `max_thread_messages` one second later, and the
    headline was lost forever."""
    sim, agent, thread, client, factory, run_id = await _drive_reply(
        engine, monkeypatch, _reply_with_sidecar(), prior_messages=_LAST_DECIDE_COUNT,
    )
    _wire_summary_channel(sim)
    try:
        assert _headlines(client) == [], "an ordinal-11 verdict is provisional"

        # The PI takes ordinal 12, the single CONCLUDE slot.
        sim.message_log.append(LogEntry(
            ts="t1.pi12", channel="single-cell-omics",
            sender_agent_id="gordy", sender_name="GordyBot",
            content="the PI's ordinal-12 reply", thread_ts="t1",
            posted_at=99.0, slack_ts="t1.pi12", slack_channel_id="C_OMICS",
        ))

        # The hub's next turn finds the thread full and closes it as `timeout`
        # without generating any reply at all.
        thread.has_pending_reply = True
        agent.state.active_threads["t1"] = thread
        await sim._reply_to_thread(agent, thread)
        assert thread.status == "closed"

        await sim._drain_pending_headlines()

        assert len(_headlines(client)) == 1, (
            "an interview that ENDS announces the verdict it holds"
        )
        rows = await _assessments(factory, run_id)
        assert len(rows) == 1
        assert rows[0].summary_posted_at is not None
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_declined_interview_announces_exactly_once(engine, monkeypatch):
    """The ⏸️ path already announces inside `_capture_hub_assessment`, before
    `_check_thread_outcome` closes the thread. The close hook must not add a
    second headline."""
    sim, agent, thread, client, factory, run_id = await _drive_reply(
        engine, monkeypatch,
        _reply_with_sidecar(closing=True), prior_messages=_LAST_DECIDE_COUNT,
    )
    _wire_summary_channel(sim)
    try:
        assert thread.status == "closed"
        await sim._drain_pending_headlines()
        assert len(_headlines(client)) == 1, "exactly one, never two"
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_draining_twice_does_not_post_twice(engine, monkeypatch):
    sim, agent, thread, client, factory, run_id = await _drive_reply(
        engine, monkeypatch, _reply_with_sidecar(), prior_messages=_LAST_DECIDE_COUNT,
    )
    _wire_summary_channel(sim)
    try:
        sim.message_log.append(LogEntry(
            ts="t1.pi12", channel="single-cell-omics",
            sender_agent_id="gordy", sender_name="GordyBot",
            content="the PI's ordinal-12 reply", thread_ts="t1",
            posted_at=99.0, slack_ts="t1.pi12", slack_channel_id="C_OMICS",
        ))
        thread.has_pending_reply = True
        agent.state.active_threads["t1"] = thread
        await sim._reply_to_thread(agent, thread)

        await sim._drain_pending_headlines()
        sim._pending_headlines.append("t1")   # simulate a re-queue
        await sim._drain_pending_headlines()

        assert len(_headlines(client)) == 1, (
            "`summary_posted_at` is the at-most-once guard, not the queue"
        )
    finally:
        await _delete_run(factory, run_id)
```

- [ ] **Step 2: Run them and watch them fail**

Run: `.venv-test/bin/python -m pytest tests/integration/test_assessment_headline_delivery.py -v`
Expected: the three new tests FAIL — `AttributeError: 'SimulationEngine' object has no attribute '_drain_pending_headlines'`.

- [ ] **Step 3: Add the queue**

In `SimulationEngine.__init__`, next to `self._pending_memory_events`, add:

```python
        # Interviews that ENDED holding a verdict nobody announced. Queued
        # rather than posted at the close, because `_close_thread` runs holding
        # the thread lock, both agent locks and a reply-lane semaphore slot, and
        # a headline is two Slack round-trips — the same reason the memory
        # events beside this are queued rather than synthesised there (audit
        # finding 1). Drained by `_drain_and_flush` and by `stop()`.
        self._pending_headlines: list[str] = []
```

Add the module-level bound next to `MEMORY_EVENTS_MAX_AT_SHUTDOWN` (`:289`):

```python
# Headlines to post during `stop()`. Each is up to two Slack round-trips and the
# container's stop grace period is finite, so the sweep is bounded like the
# memory drain beside it. Higher than that bound because a headline is cheap
# next to an LLM call, and because a whole run's worth of un-announced verdicts
# arriving at once is exactly the case this exists for.
HEADLINES_MAX_AT_SHUTDOWN = 25
```

- [ ] **Step 4: Queue from `_close_thread`**

Inside `_close_thread`'s `async with self._agent_locks.acquire_all(...)` block,
immediately after the `_pending_memory_events` appends at the end of the
method, add:

```python
            # An interview is over. If it still holds a verdict nobody
            # announced, that verdict has no later turn coming and this is the
            # last moment anything knows the interview ended — before 2026-08-29
            # nothing looked, and production lost two headlines (slusher,
            # rothstein) exactly here. QUEUE only: see `_pending_headlines`.
            held = self._assessed_threads.get(thread.thread_id)
            if (
                held is not None
                and not held.announced
                and thread.thread_id not in self._pending_headlines
            ):
                self._pending_headlines.append(thread.thread_id)
```

- [ ] **Step 5: Implement the announce and the drain**

Add both to `src/agent/simulation.py`, after `_mark_summary_posted`:

```python
    async def _announce_owed_headline(self, thread_id: str, *, trigger: str) -> bool:
        """Post the `#assessments-summary` headline an ENDED interview still owes.

        The completeness half of the invariant in
        docs/audits/2026-08-29-lost-assessment-headlines/README.md §5:
        announcement used to be a side effect of one particular REPLY (terminal
        = ⏸️, or the CONCLUDE ordinal), and an interview that ended any other
        way — the `max_thread_messages` timeout, abandonment, the run's own
        shutdown — dropped its verdict silently.

        Everything is resolved from the STORED ROW, never from live state, and
        both halves of that matter:

        * the agent that closed the thread is often the PI, not the hub
          (production run 61ccad6d logged the close under `[rothstein]`), so
          `row.agent_id` is the only correct source for whose client posts;
        * a closed thread has already been popped from every agent's
          `active_threads`, and after a restart there is no `ThreadState` at
          all — but `channel_name`, `subject_agent_id` and `slack_ts` are all
          columns, so a faithful one can be rebuilt.

        `summary_posted_at IS NULL` in the predicate is the at-most-once guard,
        and it is deliberately in the SQL rather than in Python: the close path
        and the shutdown sweep can both name the same thread, and a headline is
        a public post that cannot be retracted.

        Returns True when a headline actually reached Slack. Never raises.
        """
        held = self._assessed_threads.get(thread_id)
        if held is not None and held.announced:
            return False
        if not self.session_factory or not self.simulation_run_id:
            return False
        from sqlalchemy import select as sa_select

        try:
            async with self.session_factory() as db:
                row = (await db.execute(
                    sa_select(OpportunityAssessment)
                    .where(
                        OpportunityAssessment.simulation_run_id
                        == self.simulation_run_id,
                        OpportunityAssessment.thread_id == thread_id,
                        OpportunityAssessment.summary_posted_at.is_(None),
                    )
                    .order_by(OpportunityAssessment.created_at.desc())
                    .limit(1)
                )).scalars().first()
        except Exception as exc:  # noqa: BLE001 — never cost the caller
            logger.warning(
                "Could not read the verdict owed a headline for thread %s: %s",
                thread_id, exc,
            )
            return False
        if row is None:
            return False

        agent = self.agents.get(row.agent_id)
        if agent is None:
            logger.warning(
                "Interview %s ended owing a #assessments-summary headline, but "
                "its author %r is not on this roster — re-post with "
                "scripts/backfill_assessment_headlines.py --run %s --apply",
                thread_id, row.agent_id, self.simulation_run_id,
            )
            return False

        thread = ThreadState(
            thread_id=thread_id,
            channel=row.channel_name,
            other_agent_id=row.subject_agent_id or "",
        )
        verdict = {
            "company_or_project": row.company_or_project,
            "recommendation": row.recommendation,
            "scores": row.scores or {},
        }
        posted = await self._post_assessment_summary(
            agent, thread, verdict, row.slack_ts,
        )
        if not posted:
            return False

        await self._mark_summary_posted(thread_id)
        if held is not None:
            self._assessed_threads[thread_id] = held._replace(announced=True)
        # WARNING, not INFO. Every rescue means the hub was locked out of its
        # own CONCLUDE turn — the message-count parity break of the RCA's §2.2,
        # which this path makes non-destructive but does not fix. A run with
        # several of these is the signal to go fix that.
        logger.warning(
            "[%s] RESCUED the #assessments-summary headline for %s (thread %s, "
            "trigger=%s): the interview ended without a concluding reply, so "
            "the verdict was announced on the way out rather than by its own "
            "final turn. See docs/audits/2026-08-29-lost-assessment-headlines/.",
            row.agent_id, row.subject_agent_id or "?", thread_id, trigger,
        )
        return True

    async def _drain_pending_headlines(
        self, *, limit: int | None = None, trigger: str = "thread-close",
    ) -> None:
        """Post the headlines `_close_thread` and `stop()` queued.

        Pops BEFORE posting, so a thread that fails cannot spin the queue — the
        durable retry is `scripts/backfill_assessment_headlines.py`, not this
        loop, and `summary_posted_at` makes a re-queue harmless anyway. Never
        raises: this runs from the main loop's `finally` and from `stop()`.
        """
        drained = 0
        while self._pending_headlines and (limit is None or drained < limit):
            thread_id = self._pending_headlines.pop(0)
            drained += 1
            try:
                await self._announce_owed_headline(thread_id, trigger=trigger)
            except Exception:
                logger.exception(
                    "Failed to announce the owed headline for thread %s", thread_id,
                )
```

- [ ] **Step 6: Drain from the main loop**

At the end of `_drain_and_flush`, after the three flushes, add:

```python
        # AFTER the flushes, never before: a verdict that failed its first write
        # is on `_pending_assessments`, and `_announce_owed_headline` reads the
        # row back from the database. Draining first would find nothing and
        # silently skip the interview this whole path exists for.
        if self._pending_headlines:
            await self._drain_pending_headlines()
```

Note the deliberate difference from the memory drain two lines above: this one
is **not** gated on `self._running`. A Slack post is not an LLM call, the
queue is short, and the tick where `_running` has just been cleared is exactly
when a closing interview most needs it.

- [ ] **Step 7: Run the tests**

```bash
.venv-test/bin/python -m pytest tests/integration/test_assessment_headline_delivery.py tests/integration/test_hub_assessment_capture_gate.py -v
```

Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add src/agent/simulation.py tests/integration/test_assessment_headline_delivery.py
git commit -m "fix(assessments): announce the verdict an ended interview still owes"
```

---

### Task 5: Sweep at shutdown

A run ending means no later turn will ever come, for every interview, including
those still open.

**Files:**
- Modify: `src/agent/simulation.py` — `stop()` (`:1199`, after
  `_flush_pending_assessments(final=True)`)
- Test: `tests/integration/test_assessment_headline_delivery.py`

**Interfaces:**
- Consumes: `_drain_pending_headlines`, `_pending_headlines`,
  `HEADLINES_MAX_AT_SHUTDOWN` (Task 4).

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_assessment_headline_delivery.py`:

```python
@pytest.mark.asyncio
async def test_shutdown_announces_a_still_open_interview_s_verdict(
    engine, monkeypatch,
):
    """The run's timer is the end of the interview too. Production run
    61ccad6d's timer fired five minutes after rothstein's verdict was stored —
    by then the thread had already been closed, but an OPEN one is the same
    situation: no later turn is coming."""
    sim, agent, thread, client, factory, run_id = await _drive_reply(
        engine, monkeypatch, _reply_with_sidecar(), prior_messages=_LAST_DECIDE_COUNT,
    )
    _wire_summary_channel(sim)
    try:
        assert thread.status != "closed", "the interview is still open"
        assert _headlines(client) == []

        await sim.stop()

        assert len(_headlines(client)) == 1, (
            "a run that ends announces every verdict it is holding"
        )
        rows = await _assessments(factory, run_id)
        assert rows[0].summary_posted_at is not None
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_shutdown_does_not_re_announce_an_already_posted_headline(
    engine, monkeypatch,
):
    sim, agent, thread, client, factory, run_id = await _drive_reply(
        engine, monkeypatch, _reply_with_sidecar(), prior_messages=_CONCLUDE_COUNT,
    )
    _wire_summary_channel(sim)
    try:
        assert len(_headlines(client)) == 1
        await sim.stop()
        assert len(_headlines(client)) == 1, "exactly one, never two"
    finally:
        await _delete_run(factory, run_id)
```

- [ ] **Step 2: Run them and watch the first fail**

Run: `.venv-test/bin/python -m pytest tests/integration/test_assessment_headline_delivery.py -k shutdown -v`
Expected: `test_shutdown_announces_a_still_open_interview_s_verdict` FAILS
(`assert 0 == 1`); the re-announce test PASSES already.

- [ ] **Step 3: Implement the sweep**

In `stop()`, immediately after `await self._flush_pending_assessments(final=True)`:

```python
        # Every interview still holding an unannounced verdict is over: the run
        # is ending, so no later turn will ever conclude or supersede it. This
        # is the last chance to honour D12, and it must run AFTER the assessment
        # flush above — `_announce_owed_headline` reads the row back from the
        # database, so a verdict still sitting on `_pending_assessments` would
        # be invisible to it.
        for thread_id, held in self._assessed_threads.items():
            if not held.announced and thread_id not in self._pending_headlines:
                self._pending_headlines.append(thread_id)
        if self._pending_headlines:
            logger.info(
                "Announcing %d interview verdict(s) that ended without a "
                "concluding reply", len(self._pending_headlines),
            )
        await self._drain_pending_headlines(
            limit=HEADLINES_MAX_AT_SHUTDOWN, trigger="shutdown",
        )
        if self._pending_headlines:
            # Say LOST with the ids, the same way the buffer flushes do: the
            # assessment rows are safe, so this is recoverable, but only by
            # someone who knows it happened.
            logger.error(
                "LOST %d #assessments-summary headline(s) at shutdown (threads: "
                "%s). The assessment rows are safe — re-post with: python "
                "scripts/backfill_assessment_headlines.py --run %s --apply",
                len(self._pending_headlines),
                ", ".join(self._pending_headlines),
                self.simulation_run_id,
            )
```

- [ ] **Step 4: Run the tests**

Run: `.venv-test/bin/python -m pytest tests/integration/test_assessment_headline_delivery.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent/simulation.py tests/integration/test_assessment_headline_delivery.py
git commit -m "fix(assessments): sweep un-announced verdicts at shutdown"
```

---

### Task 6: Make the flag survive a restart and a supersession

Two at-most-once holes remain: `_rehydrate_assessed_threads` hardcodes
`announced=False`, and `_retire_superseded_verdict` deletes the row carrying
the stamp.

**Files:**
- Modify: `src/agent/simulation.py:4262` (`_rehydrate_assessed_threads`) and
  `_capture_hub_assessment`'s supersession branch (`:3335-3341`)
- Test: `tests/integration/test_assessment_headline_delivery.py`

**Interfaces:**
- Consumes: `summary_posted_at` (Task 1), `_mark_summary_posted` (Task 3).

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_assessment_headline_delivery.py`:

```python
import uuid

from src.agent.simulation import _HeldVerdict
from src.models import OpportunityAssessment


@pytest.mark.asyncio
async def test_rehydration_reads_the_durable_headline_flag(engine, monkeypatch):
    """A restart must not re-post a headline that is already public, and must
    not suppress one that never posted. Before 0041 the flag was hardcoded
    False, which got the second case right and the first case wrong."""
    from tests.integration.test_hub_assessment_capture_gate import _hub, _new_run

    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    sim, agent = _hub(factory, run_id)
    try:
        from datetime import UTC, datetime
        async with factory() as db:
            db.add(OpportunityAssessment(
                id=uuid.uuid4(), simulation_run_id=run_id, agent_id="blackbird",
                channel_name="general", thread_id="t-announced", slack_ts="1.0",
                summary_posted_at=datetime.now(UTC),
            ))
            db.add(OpportunityAssessment(
                id=uuid.uuid4(), simulation_run_id=run_id, agent_id="blackbird",
                channel_name="general", thread_id="t-owed", slack_ts="2.0",
            ))
            await db.commit()

        await sim._rehydrate_assessed_threads()

        assert sim._assessed_threads["t-announced"].announced is True
        assert sim._assessed_threads["t-owed"].announced is False
    finally:
        from tests.integration.test_hub_assessment_capture_gate import _delete_run
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_superseded_verdict_carries_its_headline_stamp_forward(
    engine, monkeypatch,
):
    """`announced` carries forward in memory so the channel keeps its first
    word. The COLUMN has to agree, or a restart re-announces the replacement."""
    sim, agent, thread, client, factory, run_id = await _drive_reply(
        engine, monkeypatch, _reply_with_sidecar(), prior_messages=_CONCLUDE_COUNT,
    )
    _wire_summary_channel(sim)
    try:
        assert len(_headlines(client)) == 1
        rows = await _assessments(factory, run_id)
        assert rows[0].summary_posted_at is not None

        # A later CONCLUDE turn supersedes it: the first row is deleted and a
        # new one takes its place on the same thread.
        sim.message_log.append(LogEntry(
            ts="t1.pi", channel="single-cell-omics",
            sender_agent_id="gordy", sender_name="GordyBot",
            content="a further PI reply", thread_ts="t1",
            posted_at=99.0, slack_ts="t1.pi", slack_channel_id="C_OMICS",
        ))
        thread.has_pending_reply = True
        agent.state.active_threads["t1"] = thread
        await sim._reply_to_thread(agent, thread)

        rows = await _assessments(factory, run_id)
        assert len(rows) == 1, "one interview, one row"
        assert rows[0].summary_posted_at is not None, (
            "the replacement inherits the stamp — the headline is already public"
        )
        assert len(_headlines(client)) == 1, "and no second headline is posted"
    finally:
        await _delete_run(factory, run_id)
```

- [ ] **Step 2: Run them and watch them fail**

Run: `.venv-test/bin/python -m pytest tests/integration/test_assessment_headline_delivery.py -k "rehydration or superseded" -v`
Expected: both FAIL.

- [ ] **Step 3: Read the flag on rehydration**

In `_rehydrate_assessed_threads`, add `summary_posted_at` to the SELECT:

```python
                    sa_select(
                        OpportunityAssessment.thread_id,
                        OpportunityAssessment.slack_ts,
                        OpportunityAssessment.summary_posted_at,
                    )
```

and change the loop:

```python
        for thread_id, slack_ts, summary_posted_at in rows:
            self._assessed_threads[thread_id] = _HeldVerdict(
                ordinal=0,
                final=thread_id in self._closed_thread_ids,
                slack_ts=slack_ts,
                announced=summary_posted_at is not None,
            )
```

Replace the `announced=False` bullet in the docstring with:

```
        * ``announced`` is READ, not defaulted, from
          ``summary_posted_at`` (migration 0041). It used to be hardcoded
          ``False`` with the reasoning that ``True`` "would suppress the
          headline for a verdict stored provisionally before the restart — a
          silent D12 breach". That was the right call against a schema with no
          answer in it, but it traded one breach for another: a verdict whose
          headline was ALREADY public got a second one, and a headline cannot
          be retracted. The column answers the question directly, so neither
          trade is necessary. A pre-0041 row reads NULL and therefore False,
          which is exactly the old behaviour.
```

- [ ] **Step 4: Carry the stamp across a supersession**

In `_capture_hub_assessment`, extend the retirement branch:

```python
                    if superseded is not None:
                        await self._retire_superseded_verdict(
                            agent.agent_id, thread, superseded,
                            replacement_ordinal=thread.message_count + 1,
                            replacement_id=replacement_id,
                        )
                        # `announced` carries forward in memory (above) so the
                        # channel keeps its first word; the COLUMN has to agree
                        # or the next restart reads NULL off the replacement and
                        # posts a second headline for the same interview.
                        # `_mark_summary_posted` keys on the thread, so it lands
                        # on whichever row now holds it.
                        if already_announced:
                            await self._mark_summary_posted(thread.thread_id)
```

- [ ] **Step 5: Run the tests**

Run: `.venv-test/bin/python -m pytest tests/integration/test_assessment_headline_delivery.py tests/integration/test_hub_assessment_capture_gate.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/agent/simulation.py tests/integration/test_assessment_headline_delivery.py
git commit -m "fix(assessments): headline flag survives restart and supersession"
```

---

### Task 7: The repair script for headlines already lost

**Files:**
- Create: `scripts/backfill_assessment_headlines.py`
- Test: `tests/integration/test_assessment_headline_delivery.py` (the pure
  selection logic; the Slack post itself is exercised by hand under
  `--dry-run`)

**Interfaces:**
- Consumes: `render_assessment_headline` (Task 2), `summary_posted_at` (Task 1).
- Produces: `select_rows_needing_headline(rows, *, live_rubric_hash, allow_rubric_drift) -> tuple[list, list[tuple[row, str]]]`
  — returns `(to_post, skipped_with_reason)`; unit-tested directly so the
  script's judgement is testable without Slack.

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_assessment_headline_delivery.py`:

```python
def test_the_repair_script_skips_rows_that_do_not_need_a_headline():
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from scripts.backfill_assessment_headlines import select_rows_needing_headline

    owed = SimpleNamespace(
        id="a", summary_posted_at=None, rubric_content_hash="42aec0479ac6",
        thread_id="t1",
    )
    already = SimpleNamespace(
        id="b", summary_posted_at=datetime.now(UTC),
        rubric_content_hash="42aec0479ac6", thread_id="t2",
    )
    drifted = SimpleNamespace(
        id="c", summary_posted_at=None, rubric_content_hash="0000deadbeef",
        thread_id="t3",
    )

    to_post, skipped = select_rows_needing_headline(
        [owed, already, drifted],
        live_rubric_hash="42aec0479ac6", allow_rubric_drift=False,
    )
    assert [r.id for r in to_post] == ["a"]
    reasons = {r.id: why for r, why in skipped}
    assert "already" in reasons["b"]
    assert "rubric" in reasons["c"]

    to_post, _ = select_rows_needing_headline(
        [owed, already, drifted],
        live_rubric_hash="42aec0479ac6", allow_rubric_drift=True,
    )
    assert [r.id for r in to_post] == ["a", "c"]
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv-test/bin/python -m pytest tests/integration/test_assessment_headline_delivery.py -k repair_script -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.backfill_assessment_headlines'`.

- [ ] **Step 3: Write the script**

Create `scripts/backfill_assessment_headlines.py`. Model the CLI and the
`--dry-run` default on `scripts/backfill_dropped_verdicts.py`.

```python
"""Re-post `#assessments-summary` headlines that were never posted.

Repairs the loss described in
docs/audits/2026-08-29-lost-assessment-headlines/README.md: an interview that
ended by the `max_thread_messages` timeout (or by abandonment, or by the run's
own shutdown) held a verdict nobody announced, and before 2026-08-29 nothing
looked. The assessment rows are intact; only the public headline is missing.

DRY RUN BY DEFAULT. `--apply` is required to post anything, because a headline
is a public Slack message that cannot be retracted.

    # see what is owed, post nothing
    python scripts/backfill_assessment_headlines.py --run <uuid>

    # post them
    python scripts/backfill_assessment_headlines.py --run <uuid> --apply

    # a headline IS already in Slack but the row predates 0041: record that
    # fact without posting a duplicate
    python scripts/backfill_assessment_headlines.py --run <uuid> \\
        --assessment <uuid> --stamp-only --apply

Rows whose `rubric_content_hash` differs from the live document are SKIPPED by
default: the headline renders a band and score, and rendering an old verdict
against today's rubric would publish a number the stored row does not carry.
`--allow-rubric-drift` overrides that deliberately.
"""
```

The module must expose the pure selector, so the judgement is testable without
a database or Slack:

```python
def select_rows_needing_headline(
    rows, *, live_rubric_hash: str, allow_rubric_drift: bool,
):
    """Split ``rows`` into (to_post, [(row, why_skipped), ...]).

    Pure and dependency-free on purpose — this is the whole judgement the
    script makes, and it must be testable without a Slack token.
    """
    to_post = []
    skipped = []
    for row in rows:
        if row.summary_posted_at is not None:
            skipped.append((row, "already announced (summary_posted_at is set)"))
            continue
        stamped = getattr(row, "rubric_content_hash", None)
        if (
            not allow_rubric_drift
            and stamped
            and live_rubric_hash
            and stamped != live_rubric_hash
        ):
            skipped.append((
                row,
                f"rubric drift: row stamped {stamped}, live document is "
                f"{live_rubric_hash} — pass --allow-rubric-drift to post anyway",
            ))
            continue
        to_post.append(row)
    return to_post, skipped
```

The rest of the script:

1. `argparse`: `--run` (required), `--assessment` (repeatable, optional
   filter), `--apply` (default off — dry run), `--stamp-only`,
   `--allow-rubric-drift`.
2. Load rows: `select(OpportunityAssessment).where(simulation_run_id == run)`,
   optionally filtered to `--assessment` ids, ordered by `created_at`.
3. `live_rubric_hash` from the same source
   `src/services/blackbird_rubric.py` exposes to the startup banner (the first
   12 hex characters of the document's sha256 — read the module and use its
   existing accessor rather than re-hashing the file).
4. For each row in `to_post`, resolve `pi_label` from
   `AgentRegistry.pi_name` for `row.subject_agent_id`, falling back to
   `row.subject_agent_id or "Unknown lab"`; resolve the posting token from
   `AgentRegistry.slack_bot_token` for `row.agent_id`.
5. Render with `render_assessment_headline(...)`. Resolve a permalink from
   `row.slack_ts` when the source channel id is resolvable, else pass `None`
   (the renderer degrades to `(link unavailable)`).
6. Print every row's rendered text and the skip reasons. **Under `--dry-run`,
   stop here.**
7. Under `--apply`: `--stamp-only` writes `summary_posted_at` and posts
   nothing; otherwise post, and write `summary_posted_at` **only for rows that
   actually posted**.
8. Exit 0 on success, 1 if any intended post failed. Print a final tally:
   `posted / stamped / skipped / failed`.

- [ ] **Step 4: Run the test**

Run: `.venv-test/bin/python -m pytest tests/integration/test_assessment_headline_delivery.py -k repair_script -v`
Expected: PASS.

- [ ] **Step 5: Lint**

Run: `.venv-test/bin/python -m ruff check scripts/backfill_assessment_headlines.py tests/`
Expected: zero findings in `tests/`.

- [ ] **Step 6: Commit**

```bash
git add scripts/backfill_assessment_headlines.py tests/integration/test_assessment_headline_delivery.py
git commit -m "feat(assessments): repair script for headlines never posted"
```

---

### Task 8: Documentation and the full gate

**Files:**
- Modify: `CLAUDE.md`
- Test: `./scripts/ci.sh` (the whole gate)

- [ ] **Step 1: Add the deploy-order box**

In `CLAUDE.md`, after the `0040_assessment_prose_format` box, add:

```markdown
> **Deploy order for `0041_assessment_summary_posted_at` — migrate BEFORE the
> new code serves.** `0041` is one additive nullable `TIMESTAMPTZ` column
> (`opportunity_assessments.summary_posted_at`), so *old code against the new
> schema* is safe. The reverse is not: the new code **maps the column**, so
> against a pre-`0041` database every `select(OpportunityAssessment)` — both
> assessment list pages, both detail pages — raises `UndefinedColumn`, and on
> the engine side `_persist_assessment`'s INSERT names it, so every verdict
> write fails too. Build, migrate from a one-off container, then start — same
> ordering as `0028`/`0030`/`0036`/`0037`/`0038`/`0040`:
>
>     DC="docker compose -f docker-compose.prod.yml"
>     $DC build blackbird-app worker
>     $DC run --rm blackbird-app alembic upgrade head
>     $DC run --rm blackbird-app alembic current      # must equal `alembic heads`
>     $DC up -d blackbird-app worker
>
> The agent image bakes `src/` in too and must be rebuilt separately
> (`$DC --profile agent build agent`) — and here that rebuild is the whole
> point: the announce-on-close path lives in the engine, so an app-only deploy
> migrates the column and keeps losing headlines.
>
> NULL on every pre-`0041` row, deliberately never backfilled: for those rows
> the only record of whether a headline posted is the Slack channel itself, and
> a guess would be indistinguishable from a measurement. Use
> `scripts/backfill_assessment_headlines.py --stamp-only` to record one you
> have verified by eye, and the same script without `--stamp-only` to post one
> that is genuinely missing.
```

- [ ] **Step 2: Correct the stale claim about the single caller**

In `CLAUDE.md`'s BlackbirdBot section, the sentence describing the
`#assessments-summary` headline says the post "fires synchronously right after
`_persist_assessment` returns HELD inside `_capture_hub_assessment`". Append:

```markdown
> As of 2026-08-29 that is no longer the ONLY path. A verdict whose interview
> ends without a terminal reply — the `max_thread_messages` timeout, an
> abandoned thread, or the run's own shutdown — is announced by
> `_announce_owed_headline`, queued by `_close_thread` and drained by
> `_drain_and_flush` / `stop()`. Announcement is now a property of the
> INTERVIEW ENDING, not of one particular reply, and `at-most-once` is enforced
> by the `opportunity_assessments.summary_posted_at` column rather than by
> in-memory state alone. Each rescue logs one **WARNING** naming the trigger —
> a run with several means the hub is being locked out of its own CONCLUDE
> ordinal (RCA §2.2), which this path makes non-destructive but does not fix.
> See `docs/audits/2026-08-29-lost-assessment-headlines/README.md`.
```

- [ ] **Step 3: Run the whole gate**

```bash
./scripts/ci.sh
```

Expected: PASS. Specifically confirm:
- exactly one alembic head (`0041`), no duplicate revision ids;
- the upgrade→downgrade→upgrade round trip succeeds (this exercises
  `0041.downgrade()`);
- `ruff check tests/` reports zero findings;
- the full pytest run is green and coverage is at or above the floor;
- `tests/characterization/__snapshots__/test_agent_turn_gm.ambr` is
  **unchanged** — `git diff --stat` must not list it.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(assessments): 0041 deploy box and the announce-on-close contract"
```

---

### Task 9: Deploy, then repair the two known losses

Not a code task. Do not start it until Task 8's gate is green.

- [ ] **Step 1: Deploy in the migrate-before-serve order**

```bash
DC="docker compose -f docker-compose.prod.yml"
$DC build blackbird-app worker
$DC --profile agent build agent
$DC run --rm blackbird-app alembic upgrade head
$DC run --rm blackbird-app alembic current      # must equal `alembic heads` (0041)
$DC up -d blackbird-app worker
```

**Do not start a simulation run.** The operator starts runs, never the deploy.

- [ ] **Step 2: Dry-run the repair for the last run**

```bash
$DC run --rm blackbird-app python scripts/backfill_assessment_headlines.py \
  --run 61ccad6d-eb1e-4023-81ba-adcea726a196
```

Expected output: five rows skipped as `already announced`… **except** they will
NOT be, because those five rows predate `0041` and their
`summary_posted_at` is NULL. The dry run will therefore propose **six** posts.
That is the expected, correct behaviour of an un-backfilled column — read the
proposed text, confirm against the Slack channel, and then:

- [ ] **Step 3: Stamp the five that are already in Slack**

```bash
$DC run --rm blackbird-app python scripts/backfill_assessment_headlines.py \
  --run 61ccad6d-eb1e-4023-81ba-adcea726a196 --stamp-only --apply \
  --assessment c3cf7ca1-d6ab-4bed-8137-b4b5cb83ba93 \
  --assessment 33d6c611-968f-4dc8-9a3e-c2fe10dd8632 \
  --assessment e2972290-6f29-418d-ab40-2028e44a5510 \
  --assessment cf96b4c7-eb60-4b16-9877-d3b1f6855803 \
  --assessment 6dd1cdd6-5c3d-4fa8-bf6d-d0bbba245c91
```

- [ ] **Step 4: Post the one that is genuinely missing**

Re-run the dry run first; it must now propose exactly one row
(`37406954-accb-4eb3-a202-a192b4a34052`, rothstein, `conditional`, 2.85). Then:

```bash
$DC run --rm blackbird-app python scripts/backfill_assessment_headlines.py \
  --run 61ccad6d-eb1e-4023-81ba-adcea726a196 --apply
```

- [ ] **Step 5: Verify**

Read `#assessments-summary` and confirm six headlines for the run, then:

```sql
SELECT COUNT(*) FILTER (WHERE summary_posted_at IS NULL) AS owed, COUNT(*) AS total
FROM opportunity_assessments
WHERE simulation_run_id = '61ccad6d-eb1e-4023-81ba-adcea726a196';
```

Expected: `owed = 0, total = 6`.

**Slusher (run `aa8359b9`, 2026-08-27, `conditional`, 2.65) is NOT
recoverable** — that row was destroyed by the 2026-08-27 rubric-v3 purge and
survives only in `backups/opportunity_assessments_pre_purge_1787862739.dump`.
Restoring it is governed by
`docs/plans/2026-08-28-run-isolation-and-assessment-archive-plan.md` Task 9 and
is an operator decision, out of scope here.

---

## Follow-on, NOT in this plan: the message-count parity break

RCA §2.2 and §6. A Slack 4000-character split writes two `agent_messages` rows
for one logical reply, permanently flipping the hub onto odd ordinals and
locking it out of the single CONCLUDE slot at ordinal 12 — after which
`max_thread_messages` closes the interview as `timeout`.

This plan makes that **non-destructive** (the verdict is still announced) but
does not fix it. The hub still loses the CONCLUDE turn whose guidance asks for
a well-formed final verdict, and interviews still end early.

It is deferred deliberately, not overlooked. `thread.message_count` feeds four
consumers at once — `phase4_guidance` (which decides prompt content), the
system-enforced close at `simulation.py:2029`, `_sidecar_refusal`'s ordinal
comparisons, and `_verdict_is_terminal` — and the `_PI_LAB` guidance strings
are pinned by a golden master that must not be regenerated without operator
sign-off. Any fix also needs a durable marker (a `agent_messages` column) to
survive the log rebuild. That is its own brainstorm, spec and plan.

**The signal to prioritise it** is now instrumented: every
`RESCUED the #assessments-summary headline` WARNING is one interview that hit
this. Count them per run.
