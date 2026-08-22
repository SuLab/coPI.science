# Empty-Reply Data Loss (E1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the hub silently losing a turn — and, when the empty replies repeat, a whole interview and its verdict — when the model returns a reply with no usable text, and make every such event visible in the logs and the database.

**Architecture:** Three layers, smallest first. (1) Text extraction stops taking only the *first* text block, so a multi-block reply keeps its tail — which is where the `<assessment_json>` sidecar lives. (2) `src/services/llm.py` logs an ERROR naming the terminal `stop_reason` whenever it is about to return empty text — including after a truncation retry — because it is the only layer that can see the stop reason. (3) `src/agent/simulation.py` records an `AssessmentDrop(reason="empty_reply")` at the moment the loss becomes real: when the hub **backs off** a thread after two consecutive empty replies. A single empty reply is *not* a loss — `has_pending_reply` stays True and the next Phase-4 pass retries the same ordinal — but the back-off permanently strands the interview (nothing re-attempts it; the lab is waiting on the hub), at **any** ordinal: the measured incident stranded a thread at message count 2.

**Tech Stack:** Python 3.11+, pytest, the Anthropic SDK (`anthropic` 1.0.0 in the deployed agent image, 0.120.2 in `.venv-test` — both verified 2026-08-21), SQLAlchemy 2.x, Postgres via testcontainers.

**Spec:** `docs/specs/2026-08-21-hub-prompt-v3-design.md` §8 "Window 0". The defect and its live measurement are recorded in `docs/plans/2026-08-21-open-issues-remediation-plan.md` §1.

**Revised 2026-08-21 after an adversarial audit.** Substantive changes from v1: the drop is recorded at back-off (any phase, hub-only), not on the first empty reply at CONCLUDE — v1's placement produced false and duplicate rows and excluded the only occurrence class actually measured (a count=2 EXPLORE abandonment); `generate_agent_response`'s empty-content branch keeps its diagnostics and is raised to ERROR **in place** (v1 told the implementer to replace a `logger.error` that does not exist, orphaning five locals into F841 and breaking the 231-finding src/ ceiling, currently at 228); post-retry empty replies now log too (v1 reintroduced a silent E1 path on every retry); `tests/unit/test_llm_service.py:417`'s direct `_first_text` call is updated (v1 missed it); `empty_response()` already exists in `tests/fakes.py` and is not re-added; the Task 3 test is written out in full against the real `_drive_reply` idiom from `test_hub_assessment_capture_gate.py` with run-scoped queries and cleanup (v1's `hub_engine_factory`/unscoped queries could not be built as instructed and were order-dependent); the drop banner's footer and the `AssessmentDrop` class docstring are amended because `empty_reply` is the first reason for which **nothing** reached Slack.

## Global Constraints

- Tests run **on the host**, never in the container: `.venv-test/bin/python -m pytest tests/ -v`. Remote host: `ssh ubuntu@ec2-3-21-33-147.us-east-2.compute.amazonaws.com`, repo at `/home/ubuntu/blackbird-copi-science`.
- `./scripts/ci.sh` is the entire gate. `ruff` must report **zero** findings on any test file touched, and `src/` must not exceed the ceiling `SRC_LINT_MAX=231` (measured 2026-08-21: exactly 228 — there are only 3 findings of headroom, so leave no orphaned locals behind).
- **Never** run `pytest --snapshot-update`.
- Never `git add -A`. Stage by explicit path. The user has three unrelated dirty files that must never be staged: `.gitignore`, `docker-compose.prod.yml`, `new_orcids.txt`.
- No docker state changes, no `alembic` against any live database, no pushes. This plan adds **no migration**.
- Commit messages end with:
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- `AssessmentDrop.reason` is `String(40)`. The existing vocabulary is exactly: `specialist_floor`, `unparseable_sidecar`, `missing_sidecar`, `premature_sidecar`, `duplicate_thread_verdict`. This plan adds one value: **`empty_reply`**.
- A guard must never destroy the artifact it guards. This codebase has done that twice (`1a32e43`, `56b4fdc`). Nothing in this plan refuses, discards, or rolls back a reply. The same principle applied to observability: a drop row must never be written for a loss that has not happened yet — the first empty reply is retried, and a retry that succeeds owes no row.
- Registered pytest markers are `integration`, `characterization`, `contract`, `real_llm`, `live_slack`, `live_api` — there is **no** `unit` marker; sync unit tests carry no mark, async ones use `pytest.mark.asyncio` (`asyncio_mode = "auto"` makes it redundant but it is the house style).

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `src/services/llm.py` | LLM calls, retries, per-call logging | Replace `_first_text` with `_all_text` (deleting the now-unused `text_blocks` local at `:597`); add `_log_empty_reply`; call it at every site that can return empty text, **including the post-retry extractions**; raise the empty-content branch's existing rich WARNING to ERROR in place |
| `tests/fakes.py` | Scripted fake Anthropic client | Add `multi_text_response()` only — `empty_response()` **already exists** at `tests/fakes.py:174`, do not re-add it |
| `tests/unit/test_llm_service.py` | Existing llm unit tests | `test_a_reply_with_only_thinking_yields_empty_string` (`:411-417`) calls `llm._first_text` directly — point it at `_all_text` |
| `tests/unit/test_llm_empty_reply.py` | New | Unit tests for extraction, ERROR logging, and the post-retry ERROR |
| `src/agent/simulation.py` | Engine; Phase-4 reply path | Record an `AssessmentDrop(reason="empty_reply")` **inside the back-off branch** (`empty_response_count >= 2`), hub-only, at any ordinal |
| `src/models/opportunity.py` | `AssessmentDrop` docs | Add `empty_reply` to the reason vocabulary; amend the class docstring, which currently asserts the reply "has already been posted to Slack" — false for this reason |
| `templates/admin/_assessments_body.html` | Drop banner | Add an `empty_reply` explanation branch; fix the blanket footer that claims a Slack reply was posted for every drop |
| `tests/integration/test_empty_reply_drop.py` | New | Back-off records exactly one drop (at CONCLUDE **and** at EXPLORE); a single empty reply records nothing and stays retryable; a lab back-off records nothing |

---

### Task 1: Keep every text block, not just the first

**Files:**
- Modify: `src/services/llm.py:209-227` (replace `_first_text`), its call sites at `:258`, `:383`, `:738`, the inline extractions at `:617` (plus the now-unused `text_blocks` at `:597`), `:411-412`, `:653-655`, `:771-773`
- Modify: `tests/fakes.py` (add `multi_text_response`)
- Modify: `tests/unit/test_llm_service.py:411-417` (the direct `_first_text` call)
- Test: `tests/unit/test_llm_empty_reply.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `_all_text(message) -> str` in `src/services/llm.py`, replacing `_first_text`. Joins every `text` block with `"\n"`; returns `""` when there is no text block. `tests/fakes.multi_text_response(*texts, stop_reason="end_turn") -> _Message`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_llm_empty_reply.py`. No `pytestmark` — there is no registered `unit` marker and these tests are sync. Import only what this step uses (ruff F401 is a zero-tolerance finding on test files):

```python
"""E1: a reply with no usable text must not vanish silently.

The engine's Phase-4 path treats an empty string as "skip this turn", so any
path that can quietly produce "" is a turn — and after two in a row, an
interview — lost with no trace. See docs/specs/2026-08-21-hub-prompt-v3-design.md
§8 Window 0.
"""
from src.services import llm
from tests.fakes import multi_text_response


def test_all_text_joins_every_text_block():
    # A concluding hub reply emits the <assessment_json> sidecar LAST. Taking
    # only block 0 dropped it while leaving the visible half intact, which is
    # exactly how a verdict goes missing while Slack looks normal.
    message = multi_text_response("<slack_message>verdict</slack_message>",
                                  "<assessment_json>{}</assessment_json>")
    assert llm._all_text(message) == (
        "<slack_message>verdict</slack_message>\n"
        "<assessment_json>{}</assessment_json>"
    )


def test_all_text_returns_empty_string_when_there_is_no_text_block():
    message = multi_text_response()
    assert llm._all_text(message) == ""
```

- [ ] **Step 2: Add the fake builder**

In `tests/fakes.py`, beside `text_response`. Do **not** add an `empty_response` builder — it already exists at `tests/fakes.py:174`; `multi_text_response()` with zero args produces the same shape and that overlap is fine.

```python
def multi_text_response(
    *texts: str, stop_reason: str = "end_turn", usage: "_Usage | None" = None
) -> "_Message":
    """A reply carrying N text blocks (N may be 0).

    Several text blocks is what a thinking-enabled turn can interleave; zero
    (same shape as ``empty_response``) models a refusal or a thinking-only
    turn, which is the shape that used to produce a silent empty reply.
    """
    msg = _Message(
        content=[_TextBlock(text=t) for t in texts], stop_reason=stop_reason
    )
    if usage is not None:
        msg.usage = usage
    return msg
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `ssh ubuntu@ec2-3-21-33-147.us-east-2.compute.amazonaws.com 'cd /home/ubuntu/blackbird-copi-science && .venv-test/bin/python -m pytest tests/unit/test_llm_empty_reply.py -v'`
Expected: FAIL — `AttributeError: module 'src.services.llm' has no attribute '_all_text'`

- [ ] **Step 4: Implement `_all_text` and update every extraction**

Replace `_first_text` in `src/services/llm.py` with:

```python
def _all_text(message: Any) -> str:
    """Every ``text`` block's text, joined with a newline — not just the first.

    Supersedes ``_first_text``, which returned block 0 only. That was safe while
    a reply carried at most one text block, but a thinking-enabled turn can
    interleave several, and the hub's concluding reply emits its
    ``<assessment_json>`` sidecar LAST — so returning the first block dropped the
    verdict while leaving the visible half of the reply intact.

    Returns "" when there is no text block at all (a refusal, or a
    thinking-only reply). Callers treat "" as "no answer".
    """
    parts = [
        block.text
        for block in message.content
        if getattr(block, "type", None) == "text"
    ]
    return "\n".join(parts)
```

Then update the extractions:

- `:258`, `:383` and `:738`: `_first_text(...)` → `_all_text(...)`.
- `:617`: `response_text = text_blocks[0].text if text_blocks else ""` → `response_text = _all_text(message)`, **and delete the now-unused `text_blocks = [b for b in message.content if b.type == "text"]` at `:597`** (leaving it is an F841 finding against a ceiling with 3 findings of headroom; `tool_use_blocks` at `:596` is still used — keep it).
- The three retry-site extractions all get the same keep-prior-text semantics:
  - `:411-412` (`if retry_msg.content:` + `response_text = _first_text(retry_msg)`) → the single line `response_text = _all_text(retry_msg) or response_text` (the old guard let a content-but-no-text retry clobber the truncated first text with `""`);
  - `:653-655` and `:771-773` (`retry_texts = ...` / `if retry_texts:` / `... = retry_texts[0].text`) → `response_text = _all_text(retry_msg) or response_text`.

- [ ] **Step 5: Update the existing `_first_text` unit test**

`tests/unit/test_llm_service.py:411-417`, `test_a_reply_with_only_thinking_yields_empty_string`, calls `llm._first_text(msg)` directly. Change that one call to `llm._all_text(msg)`; the test's name, docstring and assertion are still exactly right.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `ssh ... 'cd /home/ubuntu/blackbird-copi-science && .venv-test/bin/python -m pytest tests/unit/test_llm_empty_reply.py tests/unit/test_llm_service.py -v'`
Expected: PASS.

- [ ] **Step 7: Verify nothing referenced the old name**

Run: `grep -rn "_first_text" src/ tests/`
Expected: no output.

- [ ] **Step 8: Run the suites that exercise these paths, and lint**

Run: `ssh ... 'cd /home/ubuntu/blackbird-copi-science && .venv-test/bin/python -m pytest tests/unit -q && .venv-test/bin/python -m ruff check tests/unit/test_llm_empty_reply.py tests/unit/test_llm_service.py tests/fakes.py && .venv-test/bin/python -m ruff check src --output-format=concise --quiet | grep -c .'`
Expected: all pass; ruff reports zero findings on the test files; the src/ count is **228** (no new findings — the deleted `text_blocks` local is what keeps it flat).

- [ ] **Step 9: Commit**

```bash
git add src/services/llm.py tests/fakes.py tests/unit/test_llm_empty_reply.py tests/unit/test_llm_service.py
git commit -m "$(cat <<'EOF'
fix(llm): a reply's later text blocks are part of the reply

_first_text returned block 0 only. A thinking-enabled turn can interleave
several text blocks, and the hub's concluding reply emits its
<assessment_json> sidecar LAST — so the verdict was dropped while the visible
half of the reply survived. _all_text joins them all, and every retry-site
extraction now keeps the prior text when the retry returns none.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Say which stop reason produced an empty reply — on the first call AND after a retry

**Files:**
- Modify: `src/services/llm.py` (add `_log_empty_reply`; call it at the five sites below; raise the empty-content branch's WARNING to ERROR in place)
- Test: `tests/unit/test_llm_empty_reply.py`

**Interfaces:**
- Consumes: `_all_text` from Task 1.
- Produces: `_log_empty_reply(message, *, model, log_meta, where) -> None` in `src/services/llm.py`. Logs at ERROR naming `stop_reason`, `model`, `agent_id`, `phase` and `where`. Never raises. Called only when the extracted text is empty. `where` values: `single_call`, `single_call_retry`, `final`, `final_retry`, `forced_final`, `forced_final_retry`.

**Why the retry sites too:** v1 gated only on the *first* call's `stop_reason != "max_tokens"`. But the first call at the thread_reply site runs adaptive thinking, and thinking can consume the entire budget — six opus-5 rows logged `length(response_text)=0` with up to 1600 output tokens (see the sizing history at `src/agent/simulation.py:1820-1823`). If that truncated-to-zero call's retry then returns nothing (`_all_text(retry_msg) or response_text` keeps the prior `""`), the function returns empty with no ERROR anywhere — exactly the E1 silence this task exists to end, reintroduced on the retry path.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_llm_empty_reply.py`: add `import logging` and `import pytest` to the **top** import block (stdlib first, or ruff E402/I001 fail the zero-findings test lint), extend the `tests.fakes` import to `from tests.fakes import FakeAnthropic, multi_text_response`, and append:

```python
def test_empty_reply_logs_an_error_naming_the_stop_reason(caplog):
    # `refusal` is the case that motivated this: the turn is skipped, and
    # before this ERROR the only trace on the generate_with_tools path was a
    # WARNING in the engine that did not say WHY the text was empty.
    message = multi_text_response(stop_reason="refusal")
    with caplog.at_level(logging.ERROR, logger="src.services.llm"):
        llm._log_empty_reply(
            message,
            model="claude-opus-5",
            log_meta={"agent_id": "blackbird", "phase": "thread_reply"},
            where="final",
        )
    assert len(caplog.records) == 1
    text = caplog.records[0].getMessage()
    assert "refusal" in text
    assert "blackbird" in text
    assert "thread_reply" in text
    assert "final" in text


def test_empty_reply_logger_never_raises_on_a_bare_message():
    class Bare:
        content: list = []

    llm._log_empty_reply(Bare(), model="m", log_meta=None, where="final")


@pytest.mark.asyncio
async def test_an_empty_retry_after_truncation_still_logs(monkeypatch, caplog):
    # First call truncates with ZERO text (adaptive thinking ate the budget);
    # the retry returns nothing either. v1 of this fix gated only on the first
    # call's stop_reason, so this exact sequence returned "" in silence.
    fake = FakeAnthropic(responses=[
        multi_text_response(stop_reason="max_tokens"),
        multi_text_response(),
    ])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    async def _executor(name, params):
        return "unused"

    with caplog.at_level(logging.ERROR, logger="src.services.llm"):
        out = await llm.generate_with_tools(
            system_prompt="s",
            messages=[{"role": "user", "content": "u"}],
            tools=[{"name": "t", "description": "d",
                    "input_schema": {"type": "object"}}],
            tool_executor=_executor,
        )

    assert out == ""
    assert any("final_retry" in r.getMessage() for r in caplog.records)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `ssh ... '.venv-test/bin/python -m pytest tests/unit/test_llm_empty_reply.py -v'`
Expected: FAIL — `AttributeError: ... has no attribute '_log_empty_reply'` (3 failures, 2 passes from Task 1).

- [ ] **Step 3: Implement the logger**

Add to `src/services/llm.py`, next to `_all_text`:

```python
def _log_empty_reply(
    message: Any, *, model: str, log_meta: dict[str, Any] | None, where: str
) -> None:
    """Say loudly that a call is about to return no text, and why.

    Only this layer can see ``stop_reason``: callers receive a plain string, so
    an empty reply reaches the engine indistinguishable from a model that
    genuinely said nothing. A FIRST-pass ``max_tokens`` truncation is handled
    by the retry path and not routed here — but the retry's own empty outcome
    IS (the ``*_retry`` sites), as is every other terminal stop reason:
    ``refusal``, a thinking-only reply, an unrecognised future value.

    Never raises: this runs on a failure path, and a logging error must not
    replace the failure it is describing.
    """
    try:
        meta = log_meta or {}
        logger.error(
            "Empty reply from the model (stop_reason=%s model=%s agent=%s "
            "phase=%s site=%s) — the caller will treat this as no answer, so "
            "the turn is skipped and any verdict it carried is lost.",
            getattr(message, "stop_reason", "?"),
            model,
            meta.get("agent_id", "?"),
            meta.get("phase", "?"),
            where,
        )
    except Exception:  # noqa: BLE001 — never let logging mask the failure
        logger.error("Empty reply from the model; stop_reason unavailable")
```

- [ ] **Step 4: Call it at the five sites, and raise the sixth in place**

In `generate_with_tools`, immediately after the final-text extraction from Task 1 (formerly `:617`):

```python
            response_text = _all_text(message)
            if not response_text.strip() and message.stop_reason != "max_tokens":
                _log_empty_reply(
                    message, model=model, log_meta=log_meta, where="final"
                )
```

(`.strip()`, not bare truthiness: `_all_text` joins with `"\n"`, so blank text blocks yield a truthy `"\n"` that the engine's own `not response_text.strip()` check at `simulation.py:1879` still treats as empty — the log gate must agree with the engine.)

In the same branch's retry, after `response_text = _all_text(retry_msg) or response_text` (formerly `:653-655`):

```python
                if not response_text.strip():
                    _log_empty_reply(
                        retry_msg, model=model, log_meta=log_meta,
                        where="final_retry",
                    )
```

After the forced-final extraction (formerly `:738`):

```python
    if not response_text.strip() and message.stop_reason != "max_tokens":
        _log_empty_reply(
            message, model=model, log_meta=log_meta, where="forced_final"
        )
```

And after the forced-final retry's extraction (formerly `:771-773`):

```python
        if not response_text.strip():
            _log_empty_reply(
                retry_msg, model=model, log_meta=log_meta,
                where="forced_final_retry",
            )
```

In `generate_agent_response`:

- The empty-content branch at `:367-382` **already names the stop reason** (`stop=%r`) plus `sys_chars`/`user_chars`/token counts/`user_tail` — richer than `_log_empty_reply` can be, because it sees the prompt. Do **not** replace it with the helper (that orphans five locals into F841 findings and loses the diagnostics). Change the single token `logger.warning(` at `:373` to `logger.error(` and leave everything else in the branch untouched.
- After `response_text = _all_text(message)` (formerly `:383`):

```python
        if not response_text.strip() and message.stop_reason != "max_tokens":
            _log_empty_reply(
                message, model=model, log_meta=log_meta, where="single_call"
            )
```

- Inside the retry block, after `response_text = _all_text(retry_msg) or response_text` (formerly `:411-412`):

```python
            if not response_text.strip():
                _log_empty_reply(
                    retry_msg, model=model, log_meta=log_meta,
                    where="single_call_retry",
                )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `ssh ... '.venv-test/bin/python -m pytest tests/unit/test_llm_empty_reply.py -v'`
Expected: PASS (5 tests).

- [ ] **Step 6: Confirm the truncation path still owns its own case, and the ceiling held**

Run: `ssh ... '.venv-test/bin/python -m pytest tests/unit/test_llm_nonstreaming_ceiling.py tests/unit/test_llm_call_stats.py tests/unit/test_llm_service.py -q && .venv-test/bin/python -m ruff check src --output-format=concise --quiet | grep -c .'`
Expected: PASS — a first-pass `max_tokens` reply must still take the retry path and must **not** log a `where="final"`/`"forced_final"`/`"single_call"` ERROR — and the src/ count is still 228.

- [ ] **Step 7: Commit**

```bash
git add src/services/llm.py tests/unit/test_llm_empty_reply.py
git commit -m "$(cat <<'EOF'
fix(llm): an empty reply must say which stop reason produced it

Only this layer sees stop_reason; callers get a string, so a refusal or a
thinking-only reply reached the engine indistinguishable from a model that
said nothing — measured 13 times in 90 minutes on run 076e80b6. The check
runs after the truncation retries too: a call that truncated to zero text and
then retried to zero text used to return "" in silence.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Record the loss when the interview is abandoned

**Files:**
- Modify: `src/agent/simulation.py:1879-1891` (the existing empty-response branch — inside its back-off arm)
- Modify: `src/models/opportunity.py` (class docstring `:122-132`, reason list `:134-165`)
- Modify: `templates/admin/_assessments_body.html` (reason branch + footer)
- Test: `tests/integration/test_empty_reply_drop.py`

**Interfaces:**
- Consumes: nothing new from Tasks 1-2 (they changed only `llm.py` internals).
- Produces: an `AssessmentDrop` row with `reason="empty_reply"` written from the Phase-4 empty-response branch when, and only when, the **back-off** fires (`empty_response_count >= 2`) for a **scout_hub** agent — at any ordinal.

**Why back-off, why any phase, why hub-only:**
- *Back-off, not first occurrence.* On the first empty reply `has_pending_reply` stays True, so the next Phase-4 pass retries the same ordinal (nothing was posted, so `message_count` did not advance) — a retry that succeeds owes no drop row, and recording earlier would write a false row beside a real assessment, or two rows for one loss.
- *Any phase, not CONCLUDE-only.* Two consecutive empties strand the interview permanently wherever they happen — the hub never re-attempts (`has_pending_reply=False` and nothing resets it; the lab is waiting on the hub) — so the eventual verdict is lost just as surely at EXPLORE as at CONCLUDE. The measured incident (run 076e80b6) stranded a thread at message count 2.
- *Hub-only.* A lab's back-off also strands an interview, but the lab never owed the verdict, and this table records lost assessments; the existing missing-sidecar drop is gated on `agent.role == "scout_hub"` at `simulation.py:1951` for the same reason.

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_empty_reply_drop.py`. This is the `_drive_reply` idiom from `tests/integration/test_hub_assessment_capture_gate.py:160-217`, split so a test can drive the same thread twice, with **run-scoped** queries and try/finally cleanup — the `engine` fixture is session-scoped (`tests/conftest.py:71`), so unscoped `AssessmentDrop` queries are order-dependent across the suite. Heed CLAUDE.md's warning: `_reply_to_thread` overwrites `ThreadState.message_count` from `get_thread_history`, so prior messages are seeded into the real `MessageLog`.

```python
"""E1: consecutive empty replies abandon an interview; the abandonment must leave a row.

A single empty reply is NOT a loss: `has_pending_reply` stays True and the next
Phase-4 pass retries the same ordinal. The loss happens at the back-off
(`empty_response_count >= 2`), which permanently strands the thread — at ANY
ordinal, not just CONCLUDE: run 076e80b6 stranded a thread at message count 2.
Setup mirrors `_drive_reply` in test_hub_assessment_capture_gate.py.
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.agent import Agent
from src.agent.message_log import LogEntry
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState
from src.models import AssessmentDrop, SimulationRun
from tests.fakes import FakeSlackClient

pytestmark = pytest.mark.integration

_CONCLUDE_COUNT = 11  # prior count 11 -> ordinal 12, the CONCLUDE turn
_EXPLORE_COUNT = 1    # prior count 1  -> ordinal 2, the measured abandonment


async def _new_run(factory):
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        return run.id


async def _drops(factory, run_id):
    async with factory() as db:
        return (await db.execute(
            select(AssessmentDrop)
            .where(AssessmentDrop.simulation_run_id == run_id)
            .order_by(AssessmentDrop.created_at)
        )).scalars().all()


async def _delete_run(factory, run_id):
    async with factory() as cleanup:
        stale = (await cleanup.execute(
            select(SimulationRun).where(SimulationRun.id == run_id)
        )).scalar_one_or_none()
        if stale is not None:
            await cleanup.delete(stale)
            await cleanup.commit()


async def _stranded_thread(engine, monkeypatch, *, prior_messages, role="scout_hub"):
    """An engine + thread ready for `_reply_to_thread`, with the model scripted
    to return NOTHING. Returns before driving, so a test can drive the same
    thread twice — the back-off needs two consecutive empties."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    agent = Agent("blackbird", "BlackbirdBot", "Blackbird", role=role)
    sim = SimulationEngine(
        agents=[agent],
        slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird")},
        session_factory=factory, simulation_run_id=run_id,
    )
    thread = ThreadState(
        thread_id="t1", channel="single-cell-omics", other_agent_id="gordy",
        message_count=prior_messages, has_pending_reply=True,
    )
    agent.state.active_threads["t1"] = thread
    for i in range(prior_messages):
        ts = "t1" if i == 0 else f"t1.{i}"
        sim.message_log.append(LogEntry(
            ts=ts, channel="single-cell-omics",
            sender_agent_id="gordy" if i % 2 == 0 else "blackbird",
            sender_name="GordyBot" if i % 2 == 0 else "BlackbirdBot",
            content=f"prior interview message {i}",
            thread_ts=None if i == 0 else "t1",
            posted_at=float(i), slack_ts=ts, slack_channel_id="C_OMICS",
        ))
    monkeypatch.setattr(agent, "build_phase4_prompt", lambda **kw: ("sys", []))

    async def _empty_reply(**kwargs):
        return ""

    monkeypatch.setattr(
        "src.agent.simulation.generate_with_tools", _empty_reply
    )
    return sim, agent, thread, factory, run_id


async def test_two_empty_replies_at_conclude_record_one_drop(engine, monkeypatch):
    sim, agent, thread, factory, run_id = await _stranded_thread(
        engine, monkeypatch, prior_messages=_CONCLUDE_COUNT,
    )
    try:
        await sim._reply_to_thread(agent, thread)
        await sim._reply_to_thread(agent, thread)

        rows = await _drops(factory, run_id)
        assert [r.reason for r in rows] == ["empty_reply"], (
            "one abandonment, one row — not one per empty reply"
        )
        assert rows[0].subject_agent_id == "gordy"
        assert rows[0].thread_id == "t1"
        assert thread.has_pending_reply is False, "the back-off must still fire"
    finally:
        await _delete_run(factory, run_id)


async def test_a_single_empty_reply_is_retried_not_recorded(engine, monkeypatch):
    sim, agent, thread, factory, run_id = await _stranded_thread(
        engine, monkeypatch, prior_messages=_CONCLUDE_COUNT,
    )
    try:
        await sim._reply_to_thread(agent, thread)

        assert await _drops(factory, run_id) == []
        assert thread.has_pending_reply is True, (
            "one empty reply is retryable — recording it would be a false loss"
        )
    finally:
        await _delete_run(factory, run_id)


async def test_a_mid_interview_abandonment_is_also_recorded(engine, monkeypatch):
    # The measured incident class: run 076e80b6 stranded a thread at count=2.
    sim, agent, thread, factory, run_id = await _stranded_thread(
        engine, monkeypatch, prior_messages=_EXPLORE_COUNT,
    )
    try:
        await sim._reply_to_thread(agent, thread)
        await sim._reply_to_thread(agent, thread)

        rows = await _drops(factory, run_id)
        assert [r.reason for r in rows] == ["empty_reply"]
    finally:
        await _delete_run(factory, run_id)


async def test_a_lab_agent_back_off_records_no_drop(engine, monkeypatch):
    sim, agent, thread, factory, run_id = await _stranded_thread(
        engine, monkeypatch, prior_messages=_CONCLUDE_COUNT, role="pi_lab",
    )
    try:
        await sim._reply_to_thread(agent, thread)
        await sim._reply_to_thread(agent, thread)

        assert await _drops(factory, run_id) == [], (
            "labs never owe a verdict; their back-off is not an assessment loss"
        )
    finally:
        await _delete_run(factory, run_id)
```

(Two `_reply_to_thread` calls fit comfortably inside the rate limiter: `_agent_load` floors at 1 and the hub's allowance is `hub_llm_calls_per_window` — verified before this plan was revised.)

- [ ] **Step 2: Run the test to verify it fails**

Run: `ssh ... '.venv-test/bin/python -m pytest tests/integration/test_empty_reply_drop.py -v'`
Expected: the first and third tests FAIL finding 0 rows; the second and fourth already pass (they pin current behaviour so a later regression is caught).

- [ ] **Step 3: Implement the drop record**

In `src/agent/simulation.py`, **inside** the back-off arm of the empty-response branch (`if thread.empty_response_count >= 2:` at `:1885`), after `has_pending_reply = False` and the existing INFO line, add:

```python
                    # The back-off is the moment of loss, not the first empty
                    # reply: has_pending_reply stays True after one empty, so
                    # the next Phase-4 pass retries the same ordinal, and a
                    # retry that succeeds owes no drop row. Once backed off,
                    # nothing re-attempts this thread (the lab is waiting on
                    # the hub), so whatever verdict this interview would have
                    # produced — at ANY ordinal, not just CONCLUDE; run
                    # 076e80b6 stranded a thread at count=2 — will never
                    # exist. Hub-only: a lab's empty replies strand the
                    # interview too, but the lab never owed the verdict and
                    # this table records lost assessments.
                    if agent.role == "scout_hub":
                        message_ordinal = thread.message_count + 1
                        thread_phase, _, _ = phase4_guidance(
                            agent.role, message_ordinal
                        )
                        cause = (
                            "the model returned no usable text (see the "
                            "llm.py ERROR for the stop_reason)"
                            if not (raw_response or "").strip()
                            else "the reply could not be parsed into a "
                            "Slack message"
                        )
                        await self._record_assessment_drop(
                            agent.agent_id,
                            "empty_reply",
                            subject_agent_id=thread.other_agent_id,
                            thread_id=thread.thread_id,
                            detail=(
                                f"interview abandoned after "
                                f"{thread.empty_response_count} consecutive "
                                f"empty replies at ordinal {message_ordinal} "
                                f"({thread_phase}); {cause}"
                            ),
                        )
```

Two `cause` arms because the branch genuinely has two triggers: `raw_response` empty (the llm.py case — an ERROR with the stop reason exists) versus non-empty but stripped to nothing by `_extract_slack_message` (an unparseable reply — no llm.py ERROR exists, and blaming the model would be a misdiagnosis; the existing WARNING already says "Empty/unparseable" for exactly this reason).

`phase4_guidance` and `CONCLUDE` are already imported in this module at `:34` (they back `_warn_if_hub_conclude_missing_assessment`, `:2906`); `CONCLUDE` itself is not needed here — the phase goes into `detail` as observability, not into a gate. Confirm with `grep -n "phase4_guidance" src/agent/simulation.py | head` and do not add a duplicate import. The `+1` matches `_warn_if_hub_conclude_missing_assessment`'s own arithmetic (`:2901-2906`): nothing was posted, so `thread.message_count` is still the prior count set from `get_thread_history` at `:1648`.

- [ ] **Step 4: Document the new reason — and fix the class docstring it breaks**

In `src/models/opportunity.py`, in `AssessmentDrop`'s docstring reason list (`:134-165`), add:

```
      * ``empty_reply``          — the interview was ABANDONED: two consecutive
        replies produced no usable text (an llm.py empty reply — its ERROR
        names the stop reason — or a reply that could not be parsed into a
        Slack message), so the engine backed off and no later turn exists to
        produce the verdict. Unlike every other reason, NOTHING was posted to
        Slack for the failing turns. Recorded at any ordinal, hub-only. Added
        2026-08-21 after run 076e80b6 measured 13 empty replies in 90 minutes
        and stranded a thread at message count 2.
```

And amend the class docstring's opening paragraph (`:125-129`), which currently asserts "The concluding reply has already been posted to Slack, the thread closes normally" — true for every pre-existing reason, false for `empty_reply`. Replace that sentence with:

```
    For every reason except ``empty_reply``, the concluding reply has already
    been posted to Slack and the thread closes normally; for ``empty_reply``
    nothing was posted at all — the turns themselves failed. Either way the
    only trace is one WARNING line in a container log nobody is tailing — so an
    empty /admin/assessments page is indistinguishable from "no ideas screened
    yet".
```

- [ ] **Step 5: Update the drop banner — branch AND footer**

`templates/admin/_assessments_body.html` explains each reason by name (`:40-50`). Add, after the `missing_sidecar` branch:

```
                    {% elif reason == 'empty_reply' %}
                        &mdash; the model returned no usable reply twice in a row,
                        so the interview was abandoned before any verdict could be
                        written. Nothing was posted to Slack for these turns.
```

And fix the blanket footer (`:54-58`), which currently reads "The Slack reply for each of these was posted normally — only the machine-readable verdict was lost, and it cannot be recovered from a later turn." — false for `empty_reply`. Replace with:

```
                Except for <code>empty_reply</code>, the Slack reply for each of
                these was posted normally &mdash; only the machine-readable verdict
                was lost. None of them can be recovered from a later turn.
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `ssh ... '.venv-test/bin/python -m pytest tests/integration/test_empty_reply_drop.py -v && .venv-test/bin/python -m pytest tests/integration/test_opportunity_assessment_persistence.py -q'`
Expected: PASS (4 tests), and the persistence suite (which renders `/admin/assessments`, so it compiles the edited template) stays green.

- [ ] **Step 7: Run the full gate**

Run: `ssh ubuntu@ec2-3-21-33-147.us-east-2.compute.amazonaws.com 'cd /home/ubuntu/blackbird-copi-science && ./scripts/ci.sh'`
Expected: `==> CI passed.` and exit 0. Capture the exit code explicitly — do not read success from tailed output alone.

- [ ] **Step 8: Commit**

```bash
git add src/agent/simulation.py src/models/opportunity.py \
        tests/integration/test_empty_reply_drop.py \
        templates/admin/_assessments_body.html
git commit -m "$(cat <<'EOF'
fix(assessments): an abandoned interview is a lost verdict, so record it

Two consecutive empty replies back a thread off permanently — nothing
re-attempts it, at any ordinal (run 076e80b6 stranded one at count=2) — and
the loss existed only as one WARNING in a container log, which is why "zero
drops since restart" read as good news while an interview was dying. The drop
is recorded at the back-off, not the first empty reply: a single empty is
retried next pass, and a retry that succeeds owes no row. Hub-only — labs
never owe the verdict.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

## Deploy notes

No migration. `src/` changes, so **both** images must be rebuilt and the run restarted for this to take effect:

```bash
DC="docker compose -f docker-compose.prod.yml"
$DC up -d --build blackbird-app worker
$DC --profile agent build agent
# graceful stop only if a run is up: docker stop -t 420 blackbird-agent-run
```

The simulation is currently stopped by decision (verified 2026-08-21: no `blackbird-agent-run` container; the running `agent-run` is org1's — **do not touch it**), so this can land without a restart; it takes effect whenever the run is next started.

## Self-review

- **Spec coverage.** Window 0 of the spec asks for four things: branch on every terminal stop reason (Task 2 — including post-retry, where v1 was still silent), log ERROR with the reason (Task 2), record a drop row (Task 3 — at the abandonment, any phase, matching the spec's unqualified ask and the measured count=2 incident), concatenate text blocks instead of `[0]` (Task 1). All four have tasks.
- **Placeholders.** None: every code step carries real, complete code, including the integration test's full setup helper — v1's `hub_engine_factory` named a fixture that does not exist anywhere.
- **Type consistency.** `_all_text(message) -> str` is defined in Task 1 and used under that name everywhere after; `_log_empty_reply(message, *, model, log_meta, where)` is defined and called with exactly those keywords at six sites with six distinct `where` values; `"empty_reply"` is spelled identically in Task 3's code, its tests, the model docstring and the template branch.
- **Lint budget.** src/ sits at 228 against a 231 ceiling. Task 1 deletes the one local it orphans (`text_blocks`); Task 2 leaves the empty-content branch's locals in use by raising its log level in place instead of replacing the call. Steps 8 (Task 1) and 6 (Task 2) measure the count stays at 228.
- **Known risk.** Task 1 changes text extraction for *every* caller, not just the hub. Any caller that relied on getting only the first block would now receive more text. Task 1 Step 8 runs the whole unit tier for exactly that reason, and the golden-master suite is included in `ci.sh` at Task 3 Step 7. Task 3's hub-only gate is deliberate: a lab back-off strands an interview too, but recording it here would put lab agent_ids into a table whose consumers assume the scouting agent — if that loss needs visibility later, it is a different reason string and a different decision.
