# FINAL confirmation pass — SECTION B (Parts 20, 21, 22)

Scope: MASTER_PLAN.md :80-5814 (Part 20 + the 20.9b addendum), :5815-9756 (Part 21),
:9757-12993 (Part 22), plus :1-79 (constraints + reconciliation). Repo read-only @ `18ba52c`;
all runs against scratch copies with `/home/a/scripps/coPI.science/.venv-test/bin/python`.

---

## 1. Verdict table — audit findings

### plan/redteam/part_20.md (6 BLOCKER, 6 MAJOR, 7 MINOR)

| id | verdict | evidence in MASTER_PLAN.md |
|---|---|---|
| B1 (20.4 `break`→`return`) | **APPLIED** | `:785` `return` inside the memo branch, `:786-788` comment + `break` for the non-match path; prose `:790-797` rewritten |
| B2 (live Anthropic calls in 20.4/20.5/20.6/20.15) | **APPLIED** | autouse `_no_live_llm_or_disk` at `:627-639`, `:844-856`, `:987-1000`; `engine._update_agent_memory = AsyncMock()` at `:3617` |
| B3 (`_bot_uid_map` in `__init__`) | **APPLIED** | Step-3c third hunk `:4139-4156` (`getattr` + walrus + `c is not None`); `test_roster_sync.py`/`test_transport.py` added to Files `:3741-3743` and Step 5 `:4223` |
| B4 (unknown bot name → phantom agent) | **APPLIED** | `:4093-4108` resolve-then-`None`; regression test `:3837-3842` |
| B5 (20.14 impossible assertion) | **APPLIED** | `:3429-3441` asserts `thread_ts is None` + `visibility == VISIBILITY_COLLAB_PRIVATE`; Step 2 `:3448-3453` |
| B6 (20.10 reopened threads re-closed) | **APPLIED** | `_prior_thread_accounted` `:2264-2269`; split sets `:2517-2577`; offset/`pi_context` restore `:2606-2636`; idempotent mint `:2814-2838`; test asserts `offset > 0` + `pi_context` `:2036-2042` |
| M1 (ruff I001/F401 in 20.1/20.3) | APPLIED | `:125` MagicMock first; `:482-484` no `AsyncMock` |
| M2 (`rating=-1` silences 3 readers) | **PARTIAL** | text present as Steps 3c/3d/3e `:1787-1876`, but **no task owns the edits** — see NEW-2 |
| M3 (20.18 unscoped `LlmCallLog`) | APPLIED | `:4681-4695` COUNT scoped to `simulation_run_id` + separate windowed query; 4-response fake `:4539-4544` |
| M4 (20.19 no downstream re-check) | APPLIED | `:4993-5019` + third test `:5032-5073` |
| M5 (`import re`, 4 placeholder tests) | APPLIED | `:4019-4038` drops `import re`; real test bodies `:3845-3936` |
| M6 (comments deleted by BEFORE/AFTER) | APPLIED | 20.10 `:2493-2511`/`:2552-2572`; 20.15 `:3655-3657`/`:3673-3675` |
| m1, m3, m4, m5, m6, m7 | APPLIED | purge recompute `:386-396`; `.add(thread_id)` `:417`; softened cursor comment `:1199-1214`; clear-after-LLM `:5393-5399`; latch note `:5202-5208`; closed-thread test `:4570-4600`; `phase5_skip_probability` patches `:492`, `:3396` |
| m2 (anchors) | APPLIED (2 declined, correctly) | 20.2 `:263` `:134-144`, `:439` `:146`; 20.10 `:1922` `:4148-4414`; 20.17 `:4348` `:1322-1334`. **Re-verified the two declined ones myself: `_poll_inbound_from_db` BEFORE really is `:2894-2899` and `_send_dm` `:400-407` — the audit's "correction" was wrong, the fixer was right** |

### plan/redteam/part_21.md (4 BLOCKER, 7 MAJOR, 19 MINOR)

| id | verdict | evidence |
|---|---|---|
| B1 (`timedelta` import) | **APPLIED** | Files `:6719` "imports (:11)"; import diff `:6822-6828` |
| B2 (per-item rollback = V4-1 made unconditional) | **APPLIED** | per-item **commit** in all three sweeps `:8346-8443`; 3 data-loss-shaped tests `:8168-8319` |
| B3 (expiry re-INSERT vs `uq_email_notification_user_thread_category`) | **APPLIED** | SELECT-then-upsert in both writers `:8667-8698`, `:8749-8778`; "Why the write must be an upsert" `:8508-8525` |
| B4 (21.12 DB tests under `tests/unit/`) | **APPLIED** | new `tests/integration/test_email_notification_sweeps_expiry.py` `:8882-8886`, `_eager()` + autouse `no_ses` `:8954-8975`, B3-pinning third test `:9032-9092` |
| M1 (post-send INSERT can fail) | APPLIED | same upsert as B3 |
| M2 (non-idempotent re-migrate + 3 emails) | APPLIED | `refined_in_channel` guard `:7997-8009`; `_INSTRUCTION_FAILURE_EMAILS_SENT` `:7953-7980`; 2nd test `:7879-7918` |
| M3 (leaked `simulation_runs` row) | APPLIED | `td_run_id` `:7567-7570`, delete `:7616-7618`, leak guard `:7621-7627` |
| M4 (1 of 3 sweeps tested) | APPLIED | three tests, one per sweep |
| M5 (24.2's `except` undoes the retire) | APPLIED | hoisted import `:9500-9506`; except-arm retire `:9515-9524`; `_ReviewRaceSession.execute` + `_FakeResult.scalars()` `:9534-9582` |
| M6 (reconciliation items 2 & 7) | **PARTIAL** | item 2 rewritten (`:62`); **item 7 still says ``reopen_proposal` (21.8, 21.13)`` at `:67` — 21.8 does not touch `agent_page.py` (its Files list `:7500-7502`)** → NEW-6 |
| M7 (blocking backoff sleep) | APPLIED | chunked `_shutdown`-aware sleep `:6524-6535`; coordinator note `:6540-6549` |
| minors #1-#18 | APPLIED (or verified-accurate) | #3 doc corrections `:6656-6705`; #4 five-test list `:6631-6647`; #6 residual-limitations para `:6890-6903`; #7 comment moved `:6960-6963`; #9 `:7202-7206`; #10 `:7145-7148`; #11 `_MAX_S3_LIST_PAGES = 20` `:7470`; #14 `:8905-8906`; #18 V11-h PARTIAL row `:9669` |
| minor #19 | SKIPPED with reason (acceptable — no functional difference) | changelog |
| fixer-found blockers (rollback→`MissingGreenlet`) | **APPLIED** | 21.2 `refresh` + `job_id` `:6215-6252`; 21.10 captured `user_id`/`td_id` `:8347-8356`, `:8397-8398`, `:8430-8431`; 21.13 captured ids `:9488-9498` |

### plan/redteam/part_22.md (4 BLOCKER, 11 MAJOR, 20 MINOR + 19 anchors)

| id | verdict | evidence |
|---|---|---|
| BLOCKER-1 (seed INSERT NOT NULLs) | **APPLIED** | `:10135-10142` names `is_admin`/`email_notifications_enabled`/`onboarding_complete`/`access_status` |
| BLOCKER-2 (22.3 verify-only) | **APPLIED** | whole task rewritten `:10360-10414`; 10-hit table; "Do NOT introduce a `HEAD_REVISION` placeholder" |
| BLOCKER-3 (`Form(None)` breaks clearing) | **APPLIED** | `Form("")` kept + `"<field>" in form` in all three routes `:11146-11311`; three `still_clears` controls `:11073-11130` |
| BLOCKER-4 (`world.pi` already has a profile) | **APPLIED** | mutate-not-create in 22.7 `:11047-11052`, 22.8 `:11396-11403`; 22.10 switched to `_agent_for` `:11869-11889` |
| MAJOR-1..11 | **APPLIED** | `_dedup_pmids` `:10513-10537`; `bump_profile_version_stmt` + compiled-SQL test `:12496-12558`; `return False` `:12431`; indentation notes `:10539`, `:12359`, `:12411`; `os.chmod` + 5th test `:11736-11743`, `:11688-11695`; `_agent_for` test; concrete GM test `:11981-12007`; coordinator note `:10315-10322`; `print(rowcount)` `:10274`; verbatim `except` `:12828-12839`; `specs/admin-dashboard.md` `:11560-11566` |
| MINOR-1..5, 7, 9-13, 17-19 | APPLIED | e.g. "12 existing tests … = 16" `:10030`; split `-k` commands `:11139-11145`; `profiles/**/*.tmp` `:11849-11852` |
| MINOR-6, 8, 14, 20 | **not applied** (informational; MINOR-14's substance is carried in the coverage matrix `:12924` and open decision 5b) | — |
| MINOR-15, 16 | SKIPPED with reasons (redesign / ruff-invisible) | changelog |

Counts: **14 BLOCKERs → 14 APPLIED**; **24 MAJORs → 22 APPLIED, 2 PARTIAL** (part_20 M2, part_21 M6);
**46 MINOR/anchor items → 40 applied, 4 informational-not-applied, 2 skipped-with-reason**.

---

## 2. Focus-area answers

**(a) AsyncSession rollback trap outside Part 21.** Confirmed the 21.2/21.3/21.10/21.13 fixes are in
the text (see table). Checked the other sites in my scope:
- **20.9 `_persist_implicit_proposal_review` — SAFE.** `decision.id` and `user_id` are read *before*
  `db.add`/`commit`; the `except IntegrityError:` arm only `await db.rollback()`s and falls out of the
  `async with`; the outer `except Exception` logs plain `str` locals (`agent_id`, `thread_ts`). No ORM
  attribute is read after a rollback or a failed flush.
- **22.13 — SAFE.** `profile.id` is read before `db.execute`; none of the three routes wraps the bump
  in a `try`. `profile_save` has no `except` at all (verified in `src/routers/profile.py:122-168`).
- **22.14 — SAFE in practice, one defensive nit (NEW-9).** Neither statement rolls back. `invite.py`'s
  best-effort `except` does read `agent.agent_id` after the new `db.execute` (which autoflushes) — but
  the pending `AgentDelegate`/invitation writes are already flushed by the `select(AgentRegistry)` at
  `invite.py:227`, *outside* the `try`, so there is nothing left to fail a flush inside it.
  `agent_page.py`'s connect-slack `except` reads `current_user.email`, and already had `db.commit()`
  inside the same `try` pre-fix — pre-existing, not introduced here.

**(b) 20.10 end-to-end.** Internally consistent with 20.6 (Step 3d starts *after* 20.6's
`status == "closed"` guard and keeps its `"thread_id"` key), with 20.8 (same `message_count_offset`
mechanism), and with migration 0028 (`reopened_at`, nullable, `DateTime(timezone=True)` — matches
M.1's `PlannedObject("0028","column","reopened_at","thread_decisions")` at `:22773` and the postflight
row `("thread_decisions","reopened_at","timestamp with time zone", True, "")`). Verified against the
real code that the design *works*: `_rebuild_agent_state`'s "2." loop skips on the **local**
`closed_thread_ids` (`simulation.py:4198`), so keeping a reopened thread out of that local set is
exactly what lets the loop rebuild it; `_rebuild_state_from_db` loads `agent_id IS NULL` rows as
`sender_agent_id=None` (`simulation.py:3729`), so the `pi_context` scan finds the PI row; the new
integration test's `factories.make_thread_decision(..., reopened_at=…)` passes through `**overrides`
(`tests/factories.py:137-154`). **One inconsistency with 20.18 — see NEW-3.**

**(c) 22.7's three route blocks.** Complete and consistent: all three keep every `Form("")`, all three
add exactly one `form = await request.form()`, all six fields gated by `"<field>" in form`, and
`institution`/`department`'s real (dead) `is not None` guards — confirmed present at
`src/routers/profile.py:146-149` — are replaced, not kept. All three routes really do already take
`request: Request` (verified in the tree). `_parse_list` exists in `profile.py:35` and
`agent_page.py:1162`; `onboarding.py` uses its own local `parse_list` ✓. 22.8's and 22.13's one-liners
land inside the same blocks with matching `"<field>" in form` guards.

**(d) Six spot-checks (diff applied to a scratch copy, DB-free tests run).**

| task | pre-fix | post-fix |
|---|---|---|
| 20.4 | `1 failed, 2 passed` — `assert a.state.pending_proposals == []` vs a 1-element list, exactly as Step 2 documents | `3 passed` |
| 20.17 | `2 failed, 1 passed` — phase 4 dispatched 2 replies, phase 5 awaited the LLM once | `3 passed`; neighbours `tests/unit/test_simulation_logic.py + test_cohort_isolation.py + test_hub_budget_scheduler.py` = **250 passed** with 20.4+20.17 both applied |
| 21.10 | `3 failed` (all on `events[:2] == ["commit","rollback"]`) | `3 passed`; `ruff check src/services/email_notifications.py` 11 = pristine 11 (net-zero); new test file ruff-clean |
| 21.6 | `9 failed, 15 passed` | `24 passed`; `ruff check src/services/email_inbound.py` clean |
| 22.13 | `ImportError` by construction | `1 passed`, compiled SQL matches all three assertions; `profile_pipeline.py` ruff 2 = pristine 2 |
| 22.10 | `ModuleNotFoundError` by construction | `5 passed` incl. the 0664-survives-replace test; module + test ruff-clean |

**Item 4 (numbering / matrices, my parts).** 20.1–20.22 all present + 20.9b at the end; 21.1–21.13;
22.1–22.14 — no gaps, no duplicates. Each part ends with a coverage matrix (`:5588`, `:9658`,
`:12883`). No `TBD`/`TODO`/`FIXME`/"similar to" anywhere in `:80-12993`. The five `HEAD_REVISION`
mentions (`:10363`, `:10367`, `:10373`, `:10411`, `:12972`) are all inside Task 22.3 / open decision 3
and all say it means `0028` / must not be introduced. ✔

**Item 8 (no prompt changes).** No task under my parts creates/edits/deletes a file under `prompts/`
(the only two mentions, `:829-830`, are a docstring citation). 26.2/22.6 comply *in prose* — **but
22.6's Step-3 code block edits the inline retry prompt, contradicting its own instruction two lines
above: NEW-1 (BLOCKER).** 20.21 is the one permitted behaviour change to what the model sees, exactly
as Global Constraints `:24` allows.

---

## 3. New findings

### NEW-1 — **BLOCKER** — Task 22.6 Step 3 ships a prompt change it forbids two lines earlier

`:10878-10880` says *"Leave the retry prompt at `:324` byte-for-byte unchanged (\"IMPORTANT: Ensure
research_summary is 150-250 words.\") — no prompt changes in this PR. Add a one-line code comment
above `:324`"*. The very next code block (`:10881-10888`) then shows that prompt **rewritten**:

```python
              synthesized = await synthesize_profile(
                  context_text
                  + f"\n\nIMPORTANT: Ensure research_summary is "
                  f"{_MIN_SUMMARY_WORDS}-{_MAX_SUMMARY_WORDS} words.",
                  user.name,
              )
```

With `_MIN/_MAX = 100/350` that changes what the model is told from "150-250" to "100-350" — a prompt
edit, explicitly banned by Global Constraints `:24` (which names this exact line,
`profile_pipeline.py:324`). Verified the real line is
`context_text + "\n\nIMPORTANT: Ensure research_summary is 150-250 words."`
(`src/services/profile_pipeline.py:323`). **Fix: delete the code block at `:10881-10888` entirely and
replace it with the comment-only edit the prose already specifies:**

```python
# src/services/profile_pipeline.py, immediately above the retry synthesize_profile call (:322-325)
            # Prompt text intentionally unchanged (no prompt changes in this PR); the
            # gate below is the lenient _MIN_SUMMARY_WORDS-_MAX_SUMMARY_WORDS range.
            synthesized = await synthesize_profile(
                context_text + "\n\nIMPORTANT: Ensure research_summary is 150-250 words.",
                user.name,
            )
```

### NEW-2 — **BLOCKER (unassigned work)** — Task 20.9's `rating=-1` regression has no owning task

Task 20.9 commits `_persist_implicit_proposal_review` (Step 3, in Part 20's own commit) but the three
one-line `ProposalReview.rating != -1` filters that keep it from silencing the PI (Steps 3c/3d/3e,
`:1787-1876`) are **explicitly excluded from Step 6** (`:1903-1905`) and handed to "the coordinator …
as a follow-up after the phase-5 GATE". No task id, no phase, no test owns them, and the Execution
order (`:48-57`) has no such follow-up. I confirmed the regression is real against the tree:
`_get_unreviewed_proposals_for_user` (`email_notifications.py:170-176`) drops a proposal the moment
*any* `ProposalReview` row exists, so **the review-request email is never sent**;
`AgentBadgeMiddleware` (`main.py:88-94`) stops counting it; `agent_dashboard`
(`agent_page.py:250-253`) hides the form. As assembled, Part 20 ships that regression.
**Fix (one of, must be written into the plan, not left to "the coordinator"):**
(i) add a numbered task — e.g. **Task M.4**, placed in phase 6 after the phase-5 GATE, owning all
three one-liners plus one red→green test each (`tests/unit/test_agent_badge_middleware.py`-style
patch of the middleware session factory; a `_get_unreviewed_proposals_for_user` integration test with
a `rating=-1` row; a dashboard-render assertion) and add `M.4` to the Execution order row for phase 6
and to reconciliation item 8; **or** (ii) drop the `rating=-1` row from 20.9 and record durability via
20.10's `reopened_at`-style marker; **or** (iii) state in Task 20.9 and in Open Decision 2 that the
badge, the email and the dashboard form are knowingly suppressed. Silence is the one option the plan
must not ship.

### NEW-3 — **MAJOR** — 20.18's `_rebuild_one_agent_state` reintroduces B6 on the roster-re-add path

20.10's B6 fix restores `message_count_offset`/`pi_context` for a reopened thread in
`_rebuild_agent_state`'s "2." loop (`:2606-2636`). 20.18's new `_rebuild_one_agent_state`
(`:4728-4765`) has the *same* loop but always builds `ThreadState(...)` with no offset and no
`pi_context`, and it only skips `thread_id in self._closed_thread_ids` — which by design does **not**
contain a reopened thread. So an inactive→active roster flip for an agent with a reopened proposal
thread restores it with a zero reply budget: the next Phase 4 recompute (`len(history) - 0`) trips
`max_thread_messages` and closes it as `timeout`, minting a spurious `ThreadDecision` + PI DM +
memory syntheses — the exact failure B6 exists to prevent, one path later. **Fix — in 20.18 Step 3a,
widen the existing `ThreadDecision` read and mirror 20.10's restoration:**

```python
                # Reopened-and-not-since-re-closed threads for this agent (COR-13 / B6):
                # the same restoration _rebuild_agent_state's "2." loop does, or a
                # re-added agent's reopened thread comes back with no reply budget and
                # no PI guidance and is re-closed as 'timeout' on its first Phase 4.
                reopened_rows = (await db.execute(
                    sa_select(ThreadDecision.thread_id, ThreadDecision.reopened_at).where(
                        sa_or(
                            ThreadDecision.agent_a == agent_id,
                            ThreadDecision.agent_b == agent_id,
                        ),
                    )
                )).all()
                reopened_thread_ids = {
                    r.thread_id for r in reopened_rows if r.reopened_at is not None
                }
```
and in the `active_threads` loop below:
```python
                offset = 0
                pi_context = None
                if thread_id in reopened_thread_ids:
                    offset = msg_count
                    for h in self.message_log.get_thread_history(thread_id):
                        if h.sender_agent_id is None:
                            pi_context = h.content
                agent.state.active_threads[thread_id] = ThreadState(
                    ...,
                    message_count_offset=offset,
                    pi_context=pi_context,
                )
```
(and bump Step 1's fake to 5 queued responses, adding `[]` for the new read).

### NEW-4 — **MAJOR** — Task 20.9b's tests cannot run, and Part 20's own bookkeeping denies 20.9b exists

Three concrete defects in the addendum (which no red-team report ever covered — part_20.md audited
20.1–20.22 only):
1. Both tests pass the session cookie as `cookies=_auth(owner.id)` (`:5723`, `:5740`).
   `_auth` returns **headers** — `tests/integration/test_agent_page.py:52-56` returns
   `{"Cookie": f"copi-session={...}"}`. Passing that as `cookies=` creates a cookie literally named
   `Cookie`, the request is unauthenticated, and both tests fail for the wrong reason.
   **Fix: `headers=_auth(owner.id)`** (every existing `/message` test does exactly this).
2. `run_id = await _seed_run(db_session)` (`:5705`, `:5730`): **there is no `_seed_run` in that
   module** (`grep -n "def _seed_run" tests/integration/test_agent_page.py` → nothing), and the
   hedge at `:5744` ("copy the run-seeding lines from the nearest existing `/message` test") points
   at a test that seeds nothing inline — it uses the `world` fixture, which creates the
   `SimulationRun` (`:200-225`). `post_agent_message` resolves the run via
   `get_latest_run_id(db)` (`agent_page.py:982`). **Fix: build both tests on the `world` fixture**
   (`world.run`, `world.pi`, `OWNER_AGENT`, `world.other_agent`) and drop `_seed_run`, e.g.
   `async def test_pi_cannot_reply_into_a_thread_their_agent_never_joined(client, db_session, world):`
   with the two `AgentMessage` rows written for `OTHER_AGENT`/`THIRD_AGENT` under
   `simulation_run_id=world.run.id`, and `headers=_auth(world.pi.id)`.
3. Part 20's "Files this part modifies" says **"No task in this part COMMITS a change to
   `agent_page.py`"** (`:5642-5643`) and Open Decision 3 says **"this part does not touch
   `agent_page.py` at all"** (`:5669-5677`) — both false now that 20.9b edits
   `post_agent_message` *and* `src/services/pi_inbox.py` (the latter is missing from the Files list
   entirely). Reconciliation item 7 (`:67`) is already correct (`post_agent_message` → **20.9b**).
   **Fix: add `src/routers/agent_page.py` (function `post_agent_message` only) and
   `src/services/pi_inbox.py` to Part 20's Files list, rewrite Open Decision 3 to "closed by Task
   20.9b", and add a `COR-5 web residual → 20.9b` row to Part 20's coverage matrix.**
   (Verified 20.9b's *implementation* is otherwise sound: `pi_inbox.py` already imports `or_`,
   `select` and `AgentMessage` (`:1-24`), `pi_may_post_to_channel` really is at `:52-101`, and the new
   guard's insertion point after it is right.)

### NEW-5 — **MAJOR (lint constraint)** — Task 21.12's new line adds a ruff `UP017` finding

`:9152` writes `age = datetime.now(timezone.utc) - outstanding_notification.sent_at`. Measured:
`ruff check --select E,F,I,UP,B` flags `datetime.now(timezone.utc)` as `UP017 Use datetime.UTC alias`
(1 error), so this violates Global Constraints `:17`/`:75` ("every new or edited `src/` line is
ruff-clean"; six findings of headroom, raising `SRC_LINT_MAX` not permitted). Task 21.11 already hit
this and switched to `datetime.now(UTC)` + added `UTC` to that file's import (`:8606`), and 21.11
lands first. **Fix: `age = datetime.now(UTC) - outstanding_notification.sent_at`** (no import change
needed once 21.11 has landed; if 21.12 is ever executed alone, add `UTC` to `email_notifications.py:5`).
Also confirmed the read is safe: `EmailNotification.sent_at` is `nullable=False` with
`server_default=func.now()` (`src/models/email_notification.py:46-48`), so there is no `None` case.

### NEW-6 — **MAJOR** — reconciliation item 7 still mis-assigns `reopen_proposal` to Task 21.8

`:67` reads ``` `reopen_proposal` (21.8, 21.13) ```. Task 21.8's Files list is
`src/services/email_inbound.py` + its test file only (`:7500-7502`); it never touches
`src/routers/agent_page.py`. This is the unapplied half of part_21 M6 (the Part-21 fixer correctly
noted it was out of its file scope and flagged it for whoever edits the coordinator text; nobody did).
**Fix: `` `reopen_proposal` (21.13) ``.**

### NEW-7 — MINOR — 20.17's Phase-5 BEFORE snippet matches three sites

`agent.record_api_call()` + `try:` + `response = await generate_agent_response(` occurs **3×** in
`src/agent/simulation.py` (Phase 2's two calls and Phase 5's). The task's scope note (`:4256-4260`)
correctly says Phase 2 is out of scope, but a literal search-and-replace patches the wrong site; I hit
this while spot-checking. **Fix: add "locate this inside `_phase5_new_post` (the third of three
identical `record_api_call()` + `generate_agent_response(` sites) — do not patch Phase 2's two" to
Step 3.**

### NEW-8 — MINOR — 22.6's Files list and Step 3 disagree on `_validate_profile`'s anchor

Files says `:561-579` (`:10723`), Step 3 says `:553-579` (`:10829`). The real `def _validate_profile`
is at `src/services/profile_pipeline.py:553`. **Fix: `:553-579` in both.**

### NEW-9 — MINOR — 22.14: capture the agent slug before the new `db.execute`

`invite.py`'s best-effort `except` logs `agent.agent_id` (`invite.py:250-252`). Post-22.14 the `try`
body contains a real `db.execute`, so a DB-level failure there leaves the session needing rollback and
the handler's own attribute read can raise instead of logging. No live trigger today (the pending
writes are already flushed at `invite.py:227`, outside the `try`). **Fix (defensive, 1 line): capture
`agent_slug = agent.agent_id` before `if user.email:` and log `agent_slug`.** Same shape applies to
`agent_page.py`'s delegate-remove handler if it ever grows an ORM read.

### NEW-10 — MINOR — stale sentence in 21.13's "Produces"

`:9236-9240` ends mid-parenthesis and says the cross-part risk is "not re-verified here — flag as a
cross-part risk if #24's part also edits `review_proposal`'s body", which the task's own
"Composition with Task 24.2" section (`:9262-9274`) has since resolved in full. **Fix: replace the
trailing parenthetical with "24.2 edits the same function first (reconciliation item 2); see
*Composition with Task 24.2* below."**

---

## 4. Verdict

**NO-GO** on the text as assembled — 2 blocker-class items (NEW-1, NEW-2) and 4 majors (NEW-3 … NEW-6)
must change first. Everything else in Parts 20-22 is applied, internally consistent, anchored to real
symbols, and (for the six tasks I ran end to end) genuinely red-before / green-after with net-zero
lint. None of the six items requires re-deriving any finding: each has its corrected text above, and
all six are text-local edits (one code block deleted, one task added or one decision stated, one
method widened, one test setup rewritten, one word changed twice).

Must change before hand-off:
1. **NEW-1** — delete 22.6's prompt-rewriting code block; keep the prompt byte-for-byte (BLOCKER).
2. **NEW-2** — give 20.9's Steps 3c/3d/3e an owning task + phase + tests, or drop the `rating=-1` row,
   or state the suppression explicitly (BLOCKER).
3. **NEW-3** — restore `message_count_offset`/`pi_context` for reopened threads in
   `_rebuild_one_agent_state` (MAJOR).
4. **NEW-4** — fix 20.9b's `cookies=`→`headers=`, replace `_seed_run` with the `world` fixture, and
   correct Part 20's Files list / Open Decision 3 / coverage matrix (MAJOR).
5. **NEW-5** — 21.12: `datetime.now(UTC)` (MAJOR, lint constraint).
6. **NEW-6** — reconciliation item 7: `reopen_proposal` (21.13) (MAJOR).
Recommended but not blocking: NEW-7 … NEW-10.
