# Closing the remaining gaps on `close-issues-20-27` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every remaining verified-open defect on this branch, decide the items that need a
product ruling rather than code, and hand an independent agent everything needed to close GitHub
issues #20–#27 after merge and deploy.

**Architecture:** The branch is 229 commits on top of `copi-prod` @ `18ba52c` and its gate is green
apart from work this plan schedules. Findings come from four sources that have already been
re-verified against HEAD (`verify-engine.md`, `verify-profiles-clients.md`,
`verify-web-data-docs.md`, `verify-deploy.md` in `.superpowers/sdd/2026-09-02-close-issues-20-27/`).
Work is grouped so that concurrently-running implementers never share a file — the shared checkout
swept one implementer's edit into another's commit four times on this branch. One new Alembic
revision (0029) carries both schema changes so the migration chain grows by one, not two.

**Tech Stack:** Python 3.11 (image) / 3.12 (`.venv-test`), FastAPI, SQLAlchemy 2 async + asyncpg,
Alembic (head 0028), pytest + testcontainers, ruff, mypy, Docker Compose, `slack_sdk`.

**Spec:** the eight GitHub issues, fetched live and hash-verified byte-identical to the copies the
original plan was built from:
`/tmp/claude-1000/-home-a-scripps-coPI-science/c8a1ec5c-25b0-4282-b6cd-c2567cafee26/scratchpad/liveissues/live_2{0..7}.md`
(canonical copies: `docs/plans/2026-09-02-close-issues-20-27-evidence/issues/issue_2N.md`).
Every task below cites the issue clause it serves. **An item's `Fix:` clause is the specification —
not the plan, not a ruling, and not a reviewer's preference.**

---

## Global Constraints

Copied verbatim from the project's own rules; every task's requirements implicitly include these.

- **No prompt changes (Decision D33).** No file under `prompts/` may change, and no inline
  model-facing string may change. Verified across all 229 commits: `git diff --stat 18ba52c..HEAD --
  prompts/` is empty. Any task touching a file that holds prompt text must hash every string
  constant (AST walk) before its first commit and after its last, and put the comparison in its
  report.
- **`./scripts/ci.sh` is the whole gate; there is no server-side CI (Decision D17).** Run it in the
  FOREGROUND with `MIGCHECK_PORT=55433` — 55432 is held by an unrelated `blackbird-db-copy`
  container. Current ceilings: `SRC_LINT_MAX=260` (measured 251, must not grow), `MYPY_MAX=150`
  (measured **147**, not the 145 the comment claims), `COV_MIN=60` (measured 78.84 %).
- **Never `git push`. Never open the PR.** The branch owner opens it.
- **Never read anything under `backups/`** — it holds live production credentials.
- **Never contact production Slack.** Live Slack tests run only against the `copi-test` workspace,
  and only with the four production credentials blanked in the environment first:
  `SLACK_BOT_TOKEN_CRAVATT=""`, `SLACK_BOT_TOKEN_WISEMAN=""`, `SLACK_CONFIG_TOKEN=""`,
  `SLACK_CONFIG_REFRESH_TOKEN=""` (an empty env var overrides `.env`; this was proved, not assumed).
  Use `scratchpad/run_live_slack.sh`, which refuses to start unless its preflight confirms isolation.
- **Never run `docker compose` against the production compose files** from the dev machine. A
  `docker compose … config` render is allowed.
- **Commit only via `.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> <paths…>`**
  (flock + `git commit --only -- <paths>`). A plain `git commit` in this shared checkout sweeps other
  implementers' staged files into your commit — it happened four times (29bee9c, 5040516, eda8603,
  7cc2ea3).
- **Never `git stash`, `git reset`, `git checkout`, or `git checkout -- <file>`.** Two `git stash`
  runs wiped every implementer's uncommitted work earlier on this branch.
- Commit trailers, both lines, on every commit:
  `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_015Nnc7rWXX4MbGSnGNEthzx`

## How to read a verdict in this plan

Each task is tagged with one of three dispositions, because "fix all identified issues" resolves
into three different actions:

- **FIX** — a verified-open defect with a concrete code change and no open question.
- **DECIDE** — the issue's text and the branch's behaviour genuinely conflict, or the fix trades one
  harm for another. These tasks produce a written ruling plus whatever code the ruling implies. The
  plan states options and a recommendation; it does not pretend the decision is already made.
- **STATE** — correct as built, but a reader of the PR or the issue would be misled without a
  sentence. These land in the PR body, the closing comments, or the deploy notes. `pr-body-residuals-final.md`
  already holds 60 such items; they are **not** defects to fix and must not be re-litigated.

## Premises this plan deliberately does NOT inherit

The verification pass found 21 artefact claims that are false at HEAD. The ones most likely to
mislead an executor:

- closure-20 blocker 2 (cursor advance precedes the handler) — inverted by `c4de842`.
- closure-20 blocker 5 (`/admin/agents` counts negative; template has no clamp) — `f9541ba` added the
  scope join and `templates/admin/agents.html:79` clamps; **0 of 53** active agents go negative on the
  production copy.
- closure-20 blocker 6 / closure-27 blocker 1 (the gate is red for these reasons) — both causes fixed;
  the real failure was `tests/integration/test_health_route.py::test_health_ok`, fixed in `a47b6c5`.
- closure-21 blockers 1 and 3 — fixed by `34d3c15`.
- closure-27 blockers 2 and 3 (CVE floors, pre-1.0 caps) — fixed by `f9541ba`.
- AH1 (`LOCK_SMOKE=1` exits 127) — fixed by `5090322`; the **env-inheritance** half is not (Task 14).
- AH3's harm model — much narrower than filed; `f29e295` restored the row that makes the 12-message
  close reachable.
- R18's framing — a genuinely malformed LLM response is now *safer* than pre-fix; the live shape is a
  response that **passes** validation while omitting keys.
- R20's framing — no running image is broken; it breaks at the next `docker compose build`.

---
## Traceability — every verified finding to its disposition

Built from the four verification reports (`verify-engine.md`, `verify-profiles-clients.md`,
`verify-web-data-docs.md`, `verify-deploy.md` in `.superpowers/sdd/2026-09-02-close-issues-20-27/`).
An executor cross-referencing a report by id starts here. `FIXED` rows need no work — they are
listed so nobody re-opens them, and so Task 29 can prune the PR body's residual list against them.

| id | source report | disposition |
|---|---|---|
| AH2 | engine | **Task 1** |
| R16 | engine | **Task 2** |
| R5 | engine | **Task 3** |
| AH3 | engine | **Task 4** (DECIDE) |
| R15 | engine | **Task 5** (DECIDE) |
| CL20-1 / R17 | engine | **Task 6 + Task 7** (needs 0029) |
| CL20-3 | engine | **Task 6 + Task 8** (needs 0029) |
| CL20-4 | engine | **Task 28** closing-comment carve-out (product decision, not code) |
| N1-b | web/data/docs | **Task 9** — merge blocker |
| N1-a | web/data/docs | **Task 10** |
| R8 | web/data/docs | **Task 11** (DECIDE) |
| R13 | engine | **Task 12** |
| R14 | engine | **Task 13** |
| over-impl R2 | profiles/clients | **Task 13a** |
| over-impl R1 | profiles/clients | **Task 14** |
| over-impl R18 | profiles/clients | **Task 15** |
| #22 blocker 2 | profiles/clients | **Task 16** (DECIDE) |
| #23 R3, R4 | profiles/clients | **Task 17** |
| #23 R2, R7, over-impl R3, R4 | profiles/clients | **Task 18** |
| #23 R5, R6, R8, R9, over-impl R7 | profiles/clients | **Task 19** |
| over-impl R6 | profiles/clients | **Task 19a** (DECIDE) |
| AH5 (env-inheritance half) | deploy | **Task 20** |
| over-impl R9 | deploy | **Task 21** |
| over-impl R10 | deploy | **Task 22** (measure, then DECIDE) |
| over-impl R11 | deploy | **Task 23** |
| over-impl R20 | profiles/clients | **Task 24** |
| `docs/plans` baked into the image | deploy | **Task 25** |
| #26 blocker 2 (A14) | web/data/docs | **Task 29** |
| #26 blocker 1 (`Closes #26`) | web/data/docs | **Task 28 + Task 29** |
| Gap 1 (live-Slack audit never run) | deploy | **Task 32** |
| CL21-2 residual | engine | **STATE** — pre-existing pre-commit window |
| CL21-3 | engine | **STATE** — substantive half fixed; a test name only |
| #22 item 15 | profiles/clients | **STATE** — self-heals per user; backfill offered as a follow-up |
| #22 item 35 / COR-24e | profiles/clients | **STATE** — D28, and the issue scopes the fix elsewhere |
| #23 R1 | profiles/clients | **STATE** — deliberate consequence of `5295985` |
| #23 R10 | profiles/clients | **STATE** — 15x stress, 0 failures |
| #26 R19 | web/data/docs | **STATE** — narrow it opportunistically |
| #27 I5-e, I5-f | deploy | **STATE** — D24 and a deliberate design choice |
| #27 I1-g / D17 | deploy | **STATE** — the issue's body scopes it out; its *title* does not, so the closing comment must say so |
| CL20-2 | engine | FIXED `c4de842` |
| CL20-5 | engine | FIXED `f9541ba` — 0 of 53 agents negative, measured |
| CL20-6, #23 R11, closure-25 blocker 3 | three reports | FIXED `22c3d78` + `a47b6c5` |
| CL21-1 | engine | FIXED `34d3c15` |
| #22 blocker 1 | profiles/clients | FIXED `8341b21` |
| #22 blocker 3 | profiles/clients | FIXED `42f03f4` |
| #22 item 44 | profiles/clients | FIXED `7cc2ea3` — six destructive shapes driven, all 400 |
| N2 | web/data/docs | FIXED — code in `7cc2ea3`, pinned by `a5666b4` |
| #24 blocker 1 (concurrent-insert DoD) | web/data/docs | FIXED `1230419` — two real two-connection races |
| #26 blockers 3, 4, 5 | web/data/docs | FIXED `34d3c15` |
| closure-27 blockers 1, 2, 3 | deploy | FIXED `f9541ba`, plus `a47b6c5` for the real gate failure |
| AH1 | deploy | FIXED `5090322` — verified: `LOCK_SMOKE=1` reaches and passes the smoke step |
| AH4 | deploy | FIXED `5090322` |
| over-impl R12 | web/data/docs | **NOT-A-DEFECT** — the shield persists the triple; measured both ways |
| closure-25 blockers 1-5 | web/data/docs | **NOT BLOCKING** — its own text labels them non-blockers |

**Definition-of-done clauses already met** (evidence in the closure audits; do not re-do): #21's
`docs/inbound-email.md` prerequisites (`34d3c15`) and `worker/main.py` coverage (71.83 % to 77.72 %,
and the "from 0 %" premise was already false at the base commit); #22's migration test against a
table that already contains duplicates (`test_migration_0025.py`, 3/3 red against base); #23's
table-driven regex cases (all six issue rows plus lowercase twins); #24's concurrent-insert test
(`1230419`); #25's `test_db_contract` PI-deletion test and the gate's `alembic upgrade head` round
trip; #26's DOC-5 rendering assertion (renders through the app's own `Jinja2Templates` and runs
`node --check`). **The only unmet DoD clause is #26's second — Task 28 Step 4.**

## File Structure

Grouped so that no two tasks that may run concurrently touch the same file. The right-hand column
is the group letter; tasks within a group are sequential, groups are parallel.

| File | Responsibility for this plan | Group |
|---|---|---|
| `src/agent/simulation.py` | engine: participation lock, activation cursor, log re-queue, post back-off, inbound ordering, durable budget | **A** |
| `src/agent/state.py` | `ThreadState`'s durable-budget field | **A** |
| `alembic/versions/0029_*.py` | one revision: durable reply budget + nullable `proposal_reviews.user_id` | **A** |
| `scripts/migrate/preflight.py`, `postflight.py` | 0029's `PlannedObject` / expectation entries | **A** |
| `src/services/email_inbound.py`, `email_notifications.py` | terminal handler, `"expired"` no-send paths | **B** |
| `src/services/profile_pipeline.py` | seed-resurrection guard's key | **C** |
| `scripts/vet_publications.py`, `scripts/resynth_from_current_pubs.py` | repair scripts' list fallback | **C** |
| `src/services/pubmed.py`, `src/agent/funding_rules.py`, `src/agent/foa_pattern.py` | NCBI pacing/semaphore, apostrophe class, FOA case | **D** |
| `scripts/ci.sh`, `tests/unit/test_ci_gate.py`, `tests/unit/test_dependencies_lock.py` | nested-gate env scrubbing, mypy ceiling shape | **E** |
| `nginx/nginx.conf`, `docker-compose.prod.yml`, `.dockerignore`, `pyproject.toml` | zone arithmetic vs `mem_limit`, image bloat, `plotly` | **E** |
| `.superpowers/sdd/…/IMPLEMENTER_PREAMBLE.md`, `REVIEWER_CONSTRAINTS.md` | the process fix for over-implementation | **F** |
| `docs/plans/2026-09-04-issue-closure-handoff.md` (new) | post-merge closure instructions for an independent agent | **F** |
| `docs/plans/2026-09-02-close-issues-20-27-pr-body.md` | corrected `Closes` line and residual list | **F** |
| `.superpowers/sdd/…/deploy-notes-accumulator.md` | deploy note 21, currently missing | **F** |

---

# Group A — engine (`#20`)

Sequential. All tasks touch `src/agent/simulation.py`; do not run them in parallel with each other.

### Task 1: Restore open thread participation for a multi-tag human root (FIX)

**Issue clause (#20 COR-8, verbatim `Fix:`):** "translate `<@Uxxx>` to the agent id before matching".
It asks for **tag routing**. The branch also wired the translation into the thread-participation
*lock*, which is a different thing, and narrowed it.

**Verified evidence (`verify-engine.md` §1, probe).** For a human root message tagging two bots:
`18ba52c` → `get_thread_allowed_agents` returns `None` (open); HEAD → `{'su'}`. The lock is
permanent — the tag branch never falls through to the two-party rule. Two of the three harm paths
the audit claimed do **not** hold; the live one is `simulation.py:2695-2701`, the Phase-5 post-LLM
check, which lacks the `len(allowed) >= 2` guard that the pre-filter at `:2399-2400` has. Result:
the second bot the PI tagged burns one Phase-5 LLM call per turn producing nothing, and the PI's
second tag is silently ignored. Separately, `simulation.py:3377` (the **DB inbound** tag route)
still uses `_extract_tagged_agent`, so on that path only the first tagged bot is routed at all,
while the Slack path (`:3205-3212`) correctly routes every tagged bot via `extract_bot_mentions`.

**Files:**
- Modify: `src/agent/simulation.py` — the Phase-5 post-LLM check at `:2695-2701`; the DB inbound
  route at `:3377`
- Test: `tests/unit/test_simulation_logic.py`, `tests/unit/test_message_log.py`

**Interfaces:**
- Consumes: `get_thread_allowed_agents(...)` (existing), `extract_bot_mentions(text, uid_map)`
  (existing, `src/agent/mentions.py`), `self._bot_uid_map()` (existing)
- Produces: no new symbols

- [ ] **Step 1: Write the failing test — a two-bot human root leaves participation open**

```python
# tests/unit/test_simulation_logic.py
class TestAMultiTagHumanRootDoesNotLockParticipation:
    """#20 COR-8 asked for tag ROUTING. Locking participation to the first
    translated uid is a different change, and it regressed the case the issue
    exists to fix: at 18ba52c a human root tagging two bots left the thread open
    (get_thread_allowed_agents -> None); the branch narrowed it to {'su'}, so the
    second bot the PI tagged never activates and burns a Phase-5 LLM call per turn
    producing nothing."""

    def test_a_root_tagging_two_bots_is_not_locked_to_the_first(self):
        from src.agent.message_log import MessageLog

        log = MessageLog()
        log.set_bot_uid_map({"U111": "su", "U222": "wiseman"})
        log.append(_entry(thread_ts="100.1", sender_agent_id=None, is_bot=False,
                          content="Hey <@U111> and <@U222>, please compare notes"))

        allowed = log.get_thread_allowed_agents("100.1")

        assert allowed is None or {"su", "wiseman"} <= allowed, (
            f"a root tagging two bots must not restrict participation to one; got {allowed!r}"
        )
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_simulation_logic.py -k multi_tag -q -p no:cacheprovider`
Expected: FAIL, `got {'su'}`.

- [ ] **Step 3: Decide where the guard belongs, and write it down before coding**

Read `get_thread_allowed_agents` in `src/agent/message_log.py` and both call sites
(`simulation.py:2399-2400` pre-filter, `:2695-2701` post-LLM). Two candidate fixes:
  (a) at the source — when the root translates to **more than one** agent id, return `None` (open)
      rather than the first, so every consumer sees the same thing; or
  (b) at the consumer — add the pre-filter's `len(allowed) >= 2` guard to `:2695-2701`.
Prefer **(a)**: (b) leaves the same trap for the next consumer, and the audit's own root-cause note
is that the mitigation outlived its condition. Implement (a); if reading the code shows (a) breaks a
legitimate single-tag lock, implement (b) as well and say why in the report.

- [ ] **Step 4: Implement the guard**

- [ ] **Step 5: Route every tagged bot on the DB inbound path**

Change `simulation.py:3377`'s `_extract_tagged_agent` to the same
`extract_bot_mentions(content, self._bot_uid_map())` + per-agent `handle_channel_tag` loop the Slack
path uses at `:3205-3212`. Add a test that a DB inbound row tagging two bots routes to both.

- [ ] **Step 6: Run the engine unit set**

Run: `.venv-test/bin/python -m pytest tests/unit/test_simulation_logic.py tests/unit/test_message_log.py tests/unit/test_mentions.py tests/unit/test_roster_sync.py tests/unit/test_thread_not_found.py -q -p no:cacheprovider`
Expected: all pass, including the two new tests.

- [ ] **Step 7: Confirm no prompt string moved**

Dump every string constant in `src/agent/simulation.py` via an AST walk to a scratch file before and
after; diff them. Only log/comment text may differ.

- [ ] **Step 8: Commit**

```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  src/agent/simulation.py tests/unit/test_simulation_logic.py tests/unit/test_message_log.py
```
Message subject: `fix(agent): a multi-tag human root leaves the thread open, and the DB inbound path routes every tagged bot (#20 COR-8)`

---

### Task 2: A first-time activated agent keeps its backlog (FIX)

**Issue clause (#20 E6(2)).** The issue asks for a per-agent rebuild so a roster addition restores
state. It does not ask for the new agent's cursor to skip everything already in the channel.

**Verified evidence.** `_rebuild_one_agent_state` fast-forwards the activation cursor at
`simulation.py:5342`, and it is called for every id in `to_add`, so a **first-time** activation
suppresses the backlog that `18ba52c` gave it (14 days, per the lookback).

**Files:**
- Modify: `src/agent/simulation.py:5342` (and the `to_add` call site)
- Test: `tests/integration/test_state_rebuild.py`

- [ ] **Step 1: Write the failing test**

Seed a channel with messages older than the agent's activation, then activate the agent for the
first time (no prior `SimulationRun` state for it) and assert its scan cursor still sees the
backlog — i.e. the messages are offered to its first turn rather than skipped.

- [ ] **Step 2: Run it and watch it fail**

- [ ] **Step 3: Distinguish first activation from re-activation**

A re-added agent legitimately resumes from its own high-water mark; a first-time activation has no
mark to resume from. Key the fast-forward on whether prior state exists for that agent, not on
membership of `to_add`.

- [ ] **Step 4: Run the test and the engine unit set**

- [ ] **Step 5: Commit**

Subject: `fix(agent): a first-time activation keeps its channel backlog instead of fast-forwarding past it (#20 E6(2))`

---

### Task 3: Bound the LLM-log re-queue (FIX)

**Issue clause (#20 COR-11, verbatim `Fix:`):** "mirror PR #19's H1 re-queue." A re-queue, not an
unbounded one.

**Verified evidence.** `simulation.py:5421` does `self._llm_log_buffer[0:0] = batch` with no ceiling,
holding full prompts in memory against the agent container's `mem_limit: 768m`. `18ba52c` dropped
the batch at ≤10 rows, so the branch traded a bounded loss for an unbounded retention.

**Files:**
- Modify: `src/agent/simulation.py:5421` and the buffer's flush path
- Test: `tests/unit/test_simulation_logic.py`

- [ ] **Step 1: Write the failing test** — after N failed flushes the buffer stops growing and the
  oldest rows are dropped with one WARNING naming the count.
- [ ] **Step 2: Run it and watch it fail.**
- [ ] **Step 3: Add a ceiling** (a constant next to the buffer, e.g. `LLM_LOG_REQUEUE_MAX_ROWS`),
  drop-oldest on overflow, and log once per drop with the number dropped.
- [ ] **Step 4: Run the engine unit set.**
- [ ] **Step 5: Commit.** Subject: `fix(agent): cap the LLM-log re-queue so a failing flush cannot grow without bound (#20 COR-11)`

---

### Task 4: The two-strike post back-off wedges a thread slot (DECIDE, then FIX)

**Issue clause (#20 COR-1b, verbatim `Fix:`):** "signal the Slack failure distinctly from the mock
path; purge the log on evict; move the private outcome check under the `posted` guard." A
two-strike back-off is **not** in that clause — it was invented by the branch (`7647438`).

**Verified evidence (`verify-engine.md` §2).** The mechanism is live but the harm is narrower than
the audit filed. `f29e295` made the counterpart's refused row trip `has_new`, so archived-channel
and single-agent `invalid_auth` threads now self-close at 12 messages. The wedge survives **only**
where the counterpart stops posting (off-roster, at cap, or simply silent): then the thread sits
`status="active"` forever, `_non_funding_thread_count` (`:2335-2340`) counts it, three such threads
trip `blocked_for_regular` (`:2345`, `active_thread_threshold = 3`, `src/config.py:331`) and the
agent goes funding-only for the run, while `_agent_load` (`:515-518`) inflates its rate allowance.

**And the back-off does not deliver its own stated benefit:** `:1396` is `if has_new or
thread.has_pending_reply`, so `has_new` alone re-enqueues and `post_failure_count` resets only on a
successful post (`:1662`). Whenever the counterpart is alive the per-turn LLM burn continues
unabated. The back-off bites *only* in the case where it wedges the slot.

**The decision.** Three options; the plan recommends (b).

- (a) **Remove the back-off.** Returns to the issue's literal ask. Restores the per-turn LLM burn on
  a permanently-refusing post, which is what `7647438` was written to stop.
- (b) **Close the thread instead of parking it.** On the second consecutive refusal call
  `_close_thread(..., "timeout")` rather than clearing `has_pending_reply`. The slot is released, the
  downstream `blocked_for_regular` cascade cannot happen, the LLM burn still stops, and it needs no
  new state. **Recommended** — it is also the smaller change.
- (c) **Keep parking but exclude parked threads** from `_non_funding_thread_count` and `_agent_load`.
  Two more call sites to keep in sync, and the thread still never resolves.

- [ ] **Step 1: Record the ruling** in `.superpowers/sdd/2026-09-02-close-issues-20-27/progress.md`
  as a `Ruling:` line naming the option chosen and why, before touching code.

- [ ] **Step 2: Write the failing test**

```python
# tests/unit/test_simulation_logic.py
async def test_two_refused_posts_close_the_thread_instead_of_parking_it():
    """#20 AH3: parking the thread (has_pending_reply=False) with a silent counterpart
    leaves it status='active' forever; three of them trip blocked_for_regular
    (config.active_thread_threshold=3) and the agent goes funding-only for the run."""
    engine = _engine_with_failing_client("is_archived")
    thread = _active_thread(engine, other_agent_id="nobody")   # counterpart off-roster

    await engine._run_turn("su")
    await engine._run_turn("su")

    assert thread.status == "timeout", f"thread left {thread.status!r} — slot still held"
    assert thread.thread_ts not in engine.active_threads
```

- [ ] **Step 3: Run it and watch it fail.**
- [ ] **Step 4: Implement the chosen option.**
- [ ] **Step 5: Fix the now-false log line.** `simulation.py:1646-1648` still says "not counted,
  nothing persisted"; since `f29e295` the row **is** persisted. Correct the string (it is a log
  message, not a prompt, so D33 permits it).
- [ ] **Step 6: Run the engine unit set + `tests/integration/test_state_rebuild.py`.**
- [ ] **Step 7: Commit.** Subject: `fix(agent): a twice-refused post closes its thread instead of holding the slot for the run (#20 COR-1b)`

---

### Task 5: The inbound log append moved behind the handler — losing PI text (DECIDE, then FIX)

**Issue clause (#20 COR-10(3), verbatim `Fix:`):** "wrap both like the sibling pollers; apply side
effects before (or transactionally with) the cursor advance."

**Verified evidence.** `c4de842` (written by this session) moved the **log append** behind the
handler as well as the cursor advance. The append is what records the PI's text durably. So a
handler failure now leaves the row un-appended and un-advanced: it is re-scanned only while it stays
inside `PI_INBOX_LOOKBACK_S = 300`, and after that window the PI's message is **lost outright**.
`verify-engine.md` rates this **worse than D25**, which lost only the triggers while keeping the row.

**Why it is a decision, not a bug fix.** The issue's own dedup mechanism is the log entry's presence,
so "append before the handler" (safe for the text) re-creates exactly the defect COR-10(3) filed:
the lookback re-scan dedups on the appended row and the side effect is never retried. You cannot
have both with one field. Options:

- (a) **Append before the handler; advance the cursor after it; add a distinct handled-marker** so
  dedup no longer keys on log presence. Fully satisfies both halves of the clause. Costs a new
  durable field — fold it into the **0029** migration of Task 6 rather than adding a second revision.
  **Recommended.**
- (b) Append before the handler and accept the original COR-10(3) loss of triggers (revert to D25).
  Cheapest; leaves the issue's item open and must then be carved out of `Closes #20`.
- (c) Keep HEAD's order and shorten nothing. Rejected: it silently loses PI text after 300 s, which
  is worse than either the pre-branch behaviour or the issue's complaint.

- [ ] **Step 1: Record the ruling** in `progress.md`, naming the option and stating explicitly that
  it supersedes Decision D25 (and, if (a), that it supersedes this session's `c4de842` ordering).
- [ ] **Step 2: Write the failing test** — a handler that raises once then succeeds must leave the
  PI's row present in the log **immediately**, be retried, and be processed exactly once overall;
  a handler that raises for longer than `PI_INBOX_LOOKBACK_S` must still leave the row in the log.
- [ ] **Step 3: Run it and watch it fail.**
- [ ] **Step 4: Implement the chosen option** (if (a), after Task 6 has landed 0029).
- [ ] **Step 5: Run the engine unit set + the PI-inbox tests.**
- [ ] **Step 6: Commit.** Subject: `fix(agent): record the PI's inbound text before the handler runs, and dedup on a durable marker (#20 COR-10(3))`

---

### Task 6: One migration (0029) for the two schema-dependent items (FIX)

**Why one revision.** Two verified-open items need durable state, and Task 5 option (a) may need a
third. Three revisions would mean three preflight/postflight expectation updates and three entries in
the operator's lock table. Land one.

**What 0029 carries:**
1. `proposal_reviews.user_id` becomes **nullable** — required by Task 8. Verified on the production
   copy: the column is `NOT NULL` today, and `simulation.py:3453-3468` returns before the insert when
   `AgentRegistry.user_id IS NULL`, so #20 COR-5's "insert a `ProposalReview`" half never happens for
   those agents.
2. `thread_decisions.reply_budget_consumed` (nullable integer) — required by Task 7. Verified: both
   rebuild loops (`simulation.py:4985-4988`, `:5321-5327`) recompute `offset = msg_count` per restart,
   so a reopened thread gets a fresh 12-message budget every restart.
3. If Task 5 chose option (a): the inbound handled-marker.

**Files:**
- Create: `alembic/versions/0029_<slug>.py`
- Modify: `src/models/agent_activity.py` (nullability), `src/models/thread_decision.py` (new column),
  `scripts/migrate/preflight.py` (`PLANNED_OBJECTS`), `scripts/migrate/postflight.py`
  (`EXPECTED_INDEXES` / column expectations)
- Test: `tests/unit/test_migration_checks.py`, `tests/integration/test_migration_tooling_chain.py`,
  `tests/integration/test_migration_0029.py` (new)

**Interfaces:**
- Produces: `ThreadDecision.reply_budget_consumed: Mapped[int | None]` (nullable, no server default —
  NULL means "unknown / nothing consumed yet", which is how pre-0029 rows keep today's behaviour);
  `ProposalReview.user_id: Mapped[uuid.UUID | None]`

- [ ] **Step 1: Read the two existing revisions you are copying the shape from.**
  `alembic/versions/0026_pcm_user_cascade.py` (a nullability/constraint change that resolves its
  target from `pg_constraint` rather than hard-coding a name) and `0028_thread_reopen_state.py` (a
  nullable `ADD COLUMN` on this same table). Match their `revision`/`down_revision` idiom, their
  `if_exists=`-guarded downgrades, and their docstring style.
- [ ] **Step 2: Write the failing migration test** in `tests/integration/test_migration_0029.py`:
  against a scratch database stamped at 0028, assert the pre-state (`user_id` NOT NULL, no budget
  column), run `alembic upgrade head`, assert the post-state, then `downgrade 0028` and assert the
  pre-state is restored exactly.
- [ ] **Step 3: Run it and watch it fail** (`0029` does not exist).
- [ ] **Step 4: Write 0029.** Nullable `ALTER COLUMN … DROP NOT NULL` for `proposal_reviews.user_id`;
  nullable `ADD COLUMN` for the budget field (no server default, no backfill — absent means
  "unknown", which Task 7 must treat as "no budget consumed yet").
- [ ] **Step 5: Update the models** to match, or `postflight`'s ORM-drift check will fail the chain.
- [ ] **Step 6: Teach the tooling.** Add 0029's objects to `preflight.PLANNED_OBJECTS` and to
  `postflight`'s column expectations. The offline drift guard in
  `tests/unit/test_migration_checks.py` is one-directional (it checks migration → PLANNED_OBJECTS),
  so also confirm `tests/integration/test_migration_tooling_chain.py::test_postflight_expected_indexes_exist_at_head`
  still passes — that is the tier that catches the other direction.
- [ ] **Step 7: Run the migration suite.**
  `.venv-test/bin/python -m pytest tests/unit/test_migration_checks.py tests/integration/test_migration_0029.py tests/integration/test_migration_tooling_chain.py -q -p no:cacheprovider`
  and `.venv-test/bin/python -m alembic heads` → exactly one head, `0029`.
- [ ] **Step 8: Re-run the chain against the production copy.** The disposable copy is described in
  `.superpowers/sdd/2026-09-02-close-issues-20-27/phase8-prodcopy-test-record.md`: container
  `copi-prodtest-db` on `127.0.0.1:55434`. Restore `pristine_0024.dump` into a fresh database, run
  `preflight.py --snapshot … --backup-verified-elsewhere …`, `alembic upgrade head`, then
  `postflight.py --snapshot …`, and record the wall time and the exit codes. The chain must still be
  one transaction and postflight must exit 0.
- [ ] **Step 9: Update the operator-facing lock table.** `docs/plans/2026-09-02-close-issues-20-27.md`
  R.6 carries a measured per-revision lock table for 0025–0028; add 0029's row with what you measured
  in Step 8.
- [ ] **Step 10: Commit.** Subject: `feat(migration): 0029 — nullable proposal_reviews.user_id and a durable reply-budget field (#20 COR-5, COR-13)`

---

### Task 7: Consume the durable reply budget in both rebuild loops (FIX)

**Depends on:** Task 6 (uses `ThreadDecision.reply_budget_consumed`).

**Issue clause (#20 COR-13, verbatim `Fix:`):** "add `thread_decision_id` to `ProposalRef`; unify the
review key; persist reopen/dedup state." The first two shipped; `ac218fb` seeded the reopen dedup
set so the **per-tick** regrant stopped. The **rebuild** path did not change.

**Verified evidence.** `simulation.py:4985-4988` and `:5321-5327` both recompute
`offset = msg_count` on every restart, so the issue's own symptom — "plus a fresh reply budget each
time" — survives a restart even though `reopened_at` is now durable.

**Files:** Modify `src/agent/simulation.py` (both loops); Test `tests/integration/test_state_rebuild.py`

- [ ] **Step 1: Write the failing test** — a reopened thread whose durable budget field records N
  consumed messages must, after a rebuild, offer `max_thread_messages - N` replies, not a full
  budget; assert across two consecutive rebuilds that the number does not reset.
- [ ] **Step 2: Run it and watch it fail.**
- [ ] **Step 3: Persist the consumed count** into `reply_budget_consumed` where the reply budget is
  spent, and read it in both rebuild loops (`simulation.py:4985-4988`, `:5321-5327`). Treat NULL as
  "nothing consumed yet" so pre-0029 rows behave exactly as they do today.
- [ ] **Step 4: Run `tests/integration/test_state_rebuild.py` and the engine unit set.**
- [ ] **Step 5: Commit.** Subject: `fix(agent): a reopened thread's reply budget survives a restart (#20 COR-13)`

---

### Task 8: Persist the implicit review for an agent with no linked user (FIX)

**Depends on:** Task 6 (needs `proposal_reviews.user_id` nullable).

**Issue clause (#20 COR-5, verbatim `Fix:`):** "require the sender be the owning PI; insert a
`ProposalReview`." The first half shipped. The second half does not happen at all when
`AgentRegistry.user_id IS NULL`.

**Verified evidence.** `simulation.py:3453-3468` logs a warning and returns before the insert;
`proposal_reviews.user_id` is `NOT NULL` on the production copy, so the insert could not have
succeeded. Population today: **0 of 53** active agents — so this is correctness, not an outage, and
it is why the fix is scheduled behind a migration rather than rushed.

**Files:** Modify `src/agent/simulation.py:3453-3468`; Test `tests/unit/test_simulation_logic.py`

- [ ] **Step 1: Write the failing test** — an agent whose `AgentRegistry.user_id` is `None` still
  gets a `ProposalReview` row written, with `user_id IS NULL` and `submitted_via='engine'`, and the
  rebuild does **not** re-block it afterwards (that second assertion is the half of COR-5 the issue
  actually names: "`_rebuild_agent_state` re-blocks on restart").
- [ ] **Step 2: Run it and watch it fail.**
- [ ] **Step 3: Write the row instead of returning.** Keep the warning, downgraded to INFO, naming
  the agent — an operator still wants to know the agent has no linked user.
- [ ] **Step 4: Check every reader.** `grep -rn 'ProposalReview' src/ | grep -v test` and confirm no
  reader assumes `user_id` is non-NULL (the `-1` marker's reader filters are the ones to check).
- [ ] **Step 5: Run the engine unit set + `tests/integration/test_admin_agents_counts.py`.**
- [ ] **Step 6: Commit.** Subject: `fix(agent): the implicit review persists for an agent with no linked user (#20 COR-5)`

---

# Group B — web reopen + e-mail (`#21`, `#24`)

Sequential within the group. Touches `src/routers/agent_page.py`, `src/services/private_channels.py`,
`src/services/email_inbound.py`, `src/services/email_notifications.py`. **Do not run concurrently
with Group A** — Task 4/5 and Task 9 both reason about the same reopen/inbound flow, and a shared
file swept one implementer's work into another's commit four times on this branch.

### Task 9: The web reopen route mints a second private Slack channel (FIX — merge blocker)

**Severity: this is the most serious open item in the plan, and it is a regression introduced by
`34d3c15` (written in this session).**

**Verified evidence (`verify-web-data-docs.md` Q1, driven on a real Postgres, not read).**
`src/routers/agent_page.py:772` gates the migration only on
`enable_private_refinement and origin_visibility == "public"`. **No line in the file reads
`td.refined_in_channel` as a guard.** A retry after the review row is lost returns **302** and
produces **2 `AgentChannel` rows and 2 Slack channel-create calls**, with `refined_in_channel`
repointed to the new channel and the first one — which already has the handover posted in it —
orphaned. `slack_client.py:933-936`'s per-call timestamp suffix means Slack does not refuse the
second create. `d1146a4` added exactly this guard to the **e-mail** twin
(`email_inbound.py:867-895`) and stopped there.

**Files:**
- Modify: `src/routers/agent_page.py` (the `reopen_proposal` migration gate, ~`:772`)
- Test: `tests/integration/test_proposal_review.py`

**Interfaces:** consumes `ThreadDecision.refined_in_channel` (existing, durable since `34d3c15`
commits it inside the migration).

- [ ] **Step 1: Write the failing test** — mirror the e-mail twin's pin
  (`tests/integration/test_email_inbound_reply_paths.py`'s retry test) on the web route: drive
  `reopen_proposal`, lose the review row, drive it again, and assert **exactly one** `AgentChannel`
  row and **one** Slack channel-create call.
- [ ] **Step 2: Run it and watch it fail** — expect 2 rows / 2 creates.
- [ ] **Step 3: Add the guard**, matching the e-mail path's shape so the two read the same:

```python
# src/routers/agent_page.py, before the migration call
if td.refined_in_channel:
    # Already migrated on an earlier attempt (#21 COR-19.6 / #24 N1-b). The migration
    # commits its own AgentChannel/member/handover rows as soon as its Slack side
    # effects are irreversible (private_channels.py), so this is the durable record
    # that it happened. Re-running it asks Slack for a second channel — the name
    # carries a per-call timestamp suffix, so Slack does not refuse it — and orphans
    # the first, which already has the handover in it.
    logger.info(
        "Proposal %s was already migrated to %s — not migrating again",
        td.thread_id, td.refined_in_channel,
    )
    already_migrated = True
```
  then branch the migration call on `already_migrated`, falling through so the route still records
  the review row and retires the notification (again, matching the e-mail path).
- [ ] **Step 4: Correct the two stale comments** the verifier found while reading this code:
  `src/routers/agent_page.py:885-891` and `tests/integration/test_proposal_review.py:1327-1350` both
  describe the pre-`34d3c15` behaviour.
- [ ] **Step 5: Run** `.venv-test/bin/python -m pytest tests/integration/test_proposal_review.py tests/integration/test_agent_page.py tests/unit/test_concurrent_write_guards.py -q -p no:cacheprovider`
- [ ] **Step 6: Commit.** Subject: `fix(web): the reopen route reads refined_in_channel, so a retry cannot mint a second private channel (#24 N1, #21 COR-19.6)`

---

### Task 10: The Slack-off migration path never commits (FIX)

**Verified evidence.** `_migrate_offline` (`src/services/private_channels.py:324-404`) only flushes.
The `except IntegrityError` arm then re-binds `refined_in_channel` to a `local:` id whose rows are
gone. Measured by wrapping `_migrate_offline` with a commit and watching
`test_reopen_write_race_…`'s `channel_count_before_retry == 0` assertion fail — i.e. the commit is
what makes the assertion meaningful.

**Files:** Modify `src/services/private_channels.py` (`_migrate_offline`); Test `tests/unit/test_private_channel_migration.py`

- [ ] **Step 1: Write the failing test** — after `_migrate_offline`, a rollback on the caller's
  session must leave the `AgentChannel` row present.
- [ ] **Step 2: Run it and watch it fail.**
- [ ] **Step 3: Add `await db.commit()`** at the end of `_migrate_offline`, with the same comment
  shape as the online path's commit (which explains *why* the durability boundary is there).
- [ ] **Step 4: Run** `tests/unit/test_private_channel_migration.py tests/integration/test_proposal_review.py`
- [ ] **Step 5: Commit.** Subject: `fix(private-channels): the Slack-off migration commits its own rows too (#24 N1)`

---

### Task 11: A lost review race silently discards the PI's rating (DECIDE, then FIX)

**Verified evidence (`verify-web-data-docs.md` Q4).** `src/routers/agent_page.py:653-669`: the
`winner is None` arm logs at ERROR, commits `record_engagement` + `mark_notification_responded`, and
returns a `302` to the dashboard **byte-identical to the success path**. The PI sees the normal
post-review redirect — no error page, no flash, no query parameter — and their rating and comment
are gone. The proposal does re-appear on the dashboard (the badge counts `rating != -1`), so a
diligent PI could notice, **but `mark_notification_responded` has already flipped the outstanding
`EmailNotification` to `responded`, so no reminder chases it.**

**Why it is a decision.** `a5666b4`'s test
`test_review_proposal_recovery_with_no_winning_row_returns_a_clean_response` **pins the 302**, so
closing this means rewriting a test written three commits ago. Options:

- (a) **Tell the PI.** Redirect with an error indicator the dashboard renders ("we couldn't save your
  review — please try again"), and do **not** retire the notification when nothing was persisted.
  **Recommended** — it is the only option where the PI learns their input was lost.
- (b) Keep the 302 and stop retiring the notification, so the reminder e-mail chases it. Cheaper; the
  PI still sees a success page for a failed action.
- (c) Leave as built and document it in the closing comment. Only defensible if (a) is judged out of
  scope for #24, whose `Fix:` is about not 500ing.

- [ ] **Step 1: Record the ruling** in `progress.md`.
- [ ] **Step 2: Rewrite `a5666b4`'s test** to the chosen behaviour, and state in its docstring that
  it previously pinned the 302 and why that changed.
- [ ] **Step 3: Run it and watch it fail.**
- [ ] **Step 4: Implement.** If (a): thread the indicator through the redirect the way the route's
  other error paths do (read them first — do not invent a new mechanism), and move
  `mark_notification_responded` so it only runs when a row was persisted.
- [ ] **Step 5: Run** `tests/integration/test_proposal_review.py tests/unit/test_concurrent_write_guards.py`
- [ ] **Step 6: Commit.** Subject: `fix(web): a lost review race tells the PI instead of showing a success page (#24 V5)`

---

### Task 12: The e-mail terminal handler consumes transient failures (FIX)

**Issue clause (#21 COR-32).** The `Fix:` asks that a failed instruction post be retried rather than
silently consumed. Ruling 21.9(a) made failures *inside* the private-channel migration terminal —
deliberately — but the handler now also catches four **pre-mutation** failures, where nothing has
happened on Slack yet and a retry is exactly right.

**Verified evidence.** `src/services/email_inbound.py:908-968` (the span moved from the artifact's
`:924-966`; content confirmed): four raise sites before any Slack mutation are consumed by the
terminal path, so a transient DNS/throttle/token blip discards a legitimate PI instruction.

**Files:** Modify `src/services/email_inbound.py`; Test `tests/integration/test_email_inbound_reply_paths.py`, `tests/unit/test_email_inbound_hardening.py`

- [ ] **Step 1: Enumerate the four pre-mutation raise sites** in the report and list them in your own
  report, so the reviewer can check you moved exactly those.
- [ ] **Step 2: Write the failing tests** — each pre-mutation failure is **retried** (the S3 object is
  kept, no PI "will not be retried" e-mail); a failure *inside* the migration is still terminal
  (the existing pin must stay green).
- [ ] **Step 3: Run them and watch them fail.**
- [ ] **Step 4: Split the handler** so the terminal path covers only post-mutation failures.
- [ ] **Step 5: Run the e-mail suites.**
- [ ] **Step 6: Commit.** Subject: `fix(inbound): a pre-Slack transient failure is retried, not consumed as terminal (#21 COR-32)`

---

### Task 13: `"expired"` is committed on three no-send paths (FIX)

**Issue clause (#21 V4-3).** The `Fix:` is about expiring an unanswered notification and letting the
ladder advance — not about marking a notification expired when no replacement was sent.

**Verified evidence.** `src/services/email_notifications.py:289` sets the status on three paths
where no re-send follows, and the PI's reply is then dropped at `email_inbound.py:317-319` because
the token no longer resolves.

**Files:** Modify `src/services/email_notifications.py`; Test `tests/unit/test_email_notification_sweeps.py`, `tests/integration/test_email_notification_sweeps_expiry.py`

- [ ] **Step 1: Write the failing test** — a sweep that expires a notification but sends nothing must
  leave the reply token answerable (the PI's reply still resolves), or must not mark it expired.
- [ ] **Step 2: Run it and watch it fail.**
- [ ] **Step 3: Only write `"expired"` once a replacement has been accepted by SES**, mirroring the
  ordering 21.11 already established for row creation.
- [ ] **Step 4: Run the sweep suites.**
- [ ] **Step 5: Commit.** Subject: `fix(email): only mark a notification expired when a replacement was actually sent (#21 V4-3)`

---

### Task 13a: The paused-notification e-mail bypasses the recipient allowlist (FIX)

Self-review found this verified-OPEN item had no task. Added rather than dropped.

**Issue clause (#21 V4-4).** The `Fix:` asks for transactional safety in the notification sweeps. It
does **not** ask for an auto-downgrade / auto-pause ladder — the branch armed one — and the paused
notification it sends does not go through the guard every other send uses.

**Verified evidence (`verify-profiles-clients.md`, over-impl R2).** `_send_paused_email` calls raw
`boto3` with **no `is_allowed_recipient` gate**. Latent only while the allowlist is on; the moment it
is off, or a recipient is outside it, this path mails someone every other path would refuse.

**Files:** Modify `src/services/email_notifications.py`; Test `tests/unit/test_email_notification_sweeps.py`

- [ ] **Step 1: Write the failing test** — a paused-notification send to a recipient outside the
  allowlist is suppressed, and the suppression is logged, exactly as the other send paths behave.
- [ ] **Step 2: Run it and watch it fail.**
- [ ] **Step 3: Route the send through the same helper** the other paths use rather than raw `boto3`.
  Read one existing send site first and match it; do not invent a second gate.
- [ ] **Step 4: Audit for siblings** — `grep -rn 'boto3' src/services/email*.py` and confirm no other
  send bypasses the gate. Report what you found either way.
- [ ] **Step 5: Run the sweep suites.**
- [ ] **Step 6: Commit.** Subject: `fix(email): the paused-notification send honours the recipient allowlist (#21 V4-4)`

---

# Group C — profile pipeline (`#22`)

Parallel with Groups B, D, E. Touches `src/services/profile_pipeline.py` and two `scripts/`.

### Task 14: The seed-resurrection guard is keyed on the wrong flag (FIX)

**Verified evidence (`verify-profiles-clients.md`, over-impl R1).** The guard is still keyed on
`user.onboarding_complete`, and **`5090322` does not close it** — Step 9b sets the very flag the new
export condition tests. On the production copy **36 of 53** active agents have
`onboarding_complete = false`, so this is the common case, not an edge one. One clear route never
sets the flag, so a PI who cleared their instructions through it gets a model-authored seed
regenerated.

**Files:** Modify `src/services/profile_pipeline.py`; Test `tests/integration/test_private_profile_clear.py`, `tests/characterization/test_profile_pipeline_gm.py`

- [ ] **Step 1: Identify the clear route that never sets the flag** and name it in your report.
- [ ] **Step 2: Write the failing test** — a PI who cleared via that route does not get a seed on the
  next pipeline run.
- [ ] **Step 3: Run it and watch it fail.**
- [ ] **Step 4: Key the guard on the cleared state itself** rather than the onboarding proxy — the
  DB already records "both private columns NULL", which is the fact the guard cares about. Make sure
  the admin-seeded case (never onboarded, no private content, wants a seed) still gets one.
- [ ] **Step 5: No prompt strings may move** — hash before/after (this file holds the synthesis
  prompts). If a GM snapshot changes, stop and explain in the report rather than re-recording it.
- [ ] **Step 6: Run** `tests/unit/test_apply_synthesis.py tests/unit/test_validate_profile.py tests/characterization/test_profile_pipeline_gm.py tests/integration/test_private_profile_clear.py tests/integration/test_onboarding_flow.py`
- [ ] **Step 7: Commit.** Subject: `fix(profile): the seed guard keys on the cleared state, not the onboarding flag (#22 COR-23)`

---

### Task 15: An operator repair script can silently blank curated lists (FIX)

**Verified evidence, with the artifact's framing corrected.** A genuinely *malformed* LLM response is
now **rejected and writes nothing** — safer than pre-fix. The live shape is a response that
**passes** validation while omitting or mistyping `keywords` / `key_targets` /
`experimental_models`: measured `keywords=[] key_targets=[] experimental_models=[]` written over
curated values. Second, unfiled shape: a profile with `synthesis_validated = False` is unprotected
and loses everything.

**Files:** Modify `scripts/vet_publications.py`, `scripts/resynth_from_current_pubs.py` (and
`src/services/profile_pipeline.py`'s `apply_synthesis` if the fallback belongs there); Test
`tests/unit/test_apply_synthesis.py`

- [ ] **Step 1: Write the failing test** — a response that validates but omits `keywords` leaves the
  stored `keywords` unchanged; a response that omits everything changes nothing; a
  `synthesis_validated = False` profile is not blanked.
- [ ] **Step 2: Run it and watch it fail.**
- [ ] **Step 3: Restore "keep the stored value" for an absent key**, distinguishing *absent* from
  *explicitly empty*. Do not reintroduce the per-character corruption `beae171` fixed — an explicit
  non-list value must still be rejected.
- [ ] **Step 4: Run the profile suites + `python -c "import ast; ast.parse(open(p).read())"` for both scripts.**
- [ ] **Step 5: Commit.** Subject: `fix(scripts): a validating-but-incomplete synthesis no longer blanks curated list columns (#22 V6)`

---

### Task 16: The word-count gate disagrees with the retry prompt (DECIDE)

**Verified evidence.** The gate is `100-350`; the retry prompt at
`src/services/profile_pipeline.py:374` and `prompts/profile-synthesis*.md` say `150-250`. The issue
**describes** the disagreement but its `Fix:` clause for that group does not ask for it to be
resolved, and **D33 forbids editing the prompt**. So the only code-side option changes behaviour.

- (a) **State it, change nothing.** Add one line to #22's "What NOT" in the PR body and to the
  closing comment: the gate is deliberately wider than the prompt's guidance; the prompt is a
  request, the gate is a limit; D33 barred aligning the prompt in this PR. **Recommended** — the
  issue's `Fix:` does not ask for it, and tightening the gate to `150-250` would start rejecting
  profiles that pass today.
- (b) Tighten the gate to `150-250`. Behaviour change, not requested, needs its own justification and
  a measurement of how many stored profiles would now fail.

- [ ] **Step 1: Record the ruling** in `progress.md`.
- [ ] **Step 2: If (a)** — add the sentence to the PR body's #22 "What NOT" list and to
  `pr-body-residuals-final.md`. No code. **If (b)** — write the failing test, measure the affected
  population on the production copy first, then change the constant.
- [ ] **Step 3: Commit.** Subject: `docs: state the profile word-range gate/prompt divergence as deliberate (#22 COR-16)`

---

# Group D — external clients (`#23`)

Parallel with B, C, E. Touches `src/services/pubmed.py`, `src/services/http_retry.py`,
`src/agent/funding_rules.py`, `src/agent/foa_pattern.py`, `src/agent/tools.py`, `src/agent/grantbot.py`.

### Task 17: Widen the apostrophe class and normalise the FOA number's case (FIX)

**Issue clause (#23 COR-28a + COR-27b).** COR-28a's `Fix:` is to stop the ASCII-only apostrophe class
from mis-classifying; the branch added U+2019 only. COR-27b's `Fix:` made the pattern
case-insensitive, which means `extract_foa_number` can now return a number whose case differs from
the canonical form — and that string becomes a cache **file name**.

**Verified evidence:** the apostrophe class still misses **U+2018, U+00B4, U+FF07**;
`foa_pattern.py:35` returns `m.group(1)` verbatim.

**Files:** Modify `src/agent/funding_rules.py`, `src/agent/foa_pattern.py`; Test `tests/unit/test_funding_rules.py`, `tests/unit/test_foa_pattern.py`

- [ ] **Step 1: Write the failing tests, table-driven** — the issue's DoD requires the regex tests be
  "table-driven over the cases above", so add each missing code point as a row, and a row asserting
  `extract_foa_number` upper-cases (or otherwise canonicalises) its result.
- [ ] **Step 2: Run them and watch them fail.**
- [ ] **Step 3: Widen the class; canonicalise the return.** Check every caller that uses the result
  as a path or a cache key (`foa_cache.py`) and confirm canonicalising does not orphan existing
  cache entries — if it would, say so in the report and handle it.
- [ ] **Step 4: Run** `tests/unit/test_funding_rules.py tests/unit/test_foa_pattern.py tests/unit/test_grantbot_selection.py`
- [ ] **Step 5: Commit.** Subject: `fix(funding): the apostrophe class covers the remaining code points, and FOA numbers canonicalise (#23 COR-28a, COR-27b)`

---

### Task 18: Bound the retry budget and stop over-throttling NCBI (FIX)

**Issue clause (#23 COR-29a/c).** The `Fix:` asks for retry/backoff and key-sized concurrency. It does
not ask for an unbounded per-item retry budget, nor for throttling below NCBI's own ceiling.

**Verified evidence:**
- **over-impl R3** — a blanket 4× retry with **no total deadline**, applied inside a per-item loop, so
  a long publication list multiplies the worst case without limit.
- **over-impl R4 / closure-23 R2** — the resized semaphore paces the keyed path to **8.33 req/s against
  a 10 req/s policy ceiling**, and holds a slot across all four retries, so the effective concurrency
  is lower again.
- **closure-23 R7** — `_NCBI_SEMAPHORES` is still loop-bound; the `HAZARD` comment at
  `src/services/pubmed.py:87-92` is unresolved in `src` (the test fixture resets them per test).

**Files:** Modify `src/services/pubmed.py`, `src/services/http_retry.py`; Test `tests/contract/test_pubmed_contract.py`, `tests/unit/test_http_retry.py`

- [ ] **Step 1: Write the failing tests** — (i) a per-call total deadline caps wall time regardless of
  attempt count; (ii) the keyed path's measured peak reaches the policy ceiling rather than sitting
  below it; (iii) the semaphore is not shared across event loops (drive two `asyncio.run()` calls, the
  same shape `e6a03f7` used for the pacing cursor).
- [ ] **Step 2: Run them and watch them fail.**
- [ ] **Step 3: Add a total deadline** to `get_with_retry`/`post_with_retry`, release the semaphore
  between attempts (or acquire per attempt), and make the semaphore map per-loop.
- [ ] **Step 4: Re-measure the rate** the way `verify-profiles-clients.md` did and record before/after
  numbers in your report — the plan's claim is a measured bound, not an intention.
- [ ] **Step 5: Run** `tests/contract/test_pubmed_contract.py tests/contract/test_orcid_contract.py tests/unit/test_http_retry.py tests/unit/test_retry_after.py`
- [ ] **Step 6: Commit.** Subject: `fix(http): bound the retry budget with a deadline and pace NCBI at its ceiling, not below it (#23 COR-29)`

---

### Task 19: Close the remaining #23 test-and-comment gaps (FIX)

Small, mechanical, and all in the issue's own DoD ("each PR ships a test that fails against the
pre-fix code"). One commit.

**Verified evidence:** **closure-23 R5** — the V10b log line has no assertion (`grep "skipping
delegate Slack-ID sync" tests/` returns 0). **R6** — no behavioural retry test for `orcid.py` or
`grants.py`; only the wiring is pinned. **R8** — `_execute_retrieve_abstract` /
`_execute_retrieve_full_text` (`src/agent/tools.py:215`, `:251`) have no call-site test. **R9** —
`src/agent/simulation.py:4643`'s `_bot_uid_map` docstring still asserts "grantbot falls back to
SuBot's token", which `f1c28d1` made false. **over-impl R7** — COR-26c removed the SuBot fallback
without re-attributing; confirm the docstring fix is the whole remedy or say what else is needed.

**Files:** Test `tests/unit/test_delegate_slack_ids.py`, `tests/contract/test_orcid_contract.py`,
`tests/contract/test_grants_contract.py` (create if absent), `tests/unit/test_tools_budget.py`;
Modify `src/agent/simulation.py` (docstring only — **not** `_bot_uid_map`'s behaviour)

- [ ] **Step 1: Write the four missing assertions/tests**, each red before the corresponding fix
  commit it protects (use `git show <commit>~1:<path>` into scratch to prove red; never mutate `src/`).
- [ ] **Step 2: Run them and watch the new ones pass and the mutation checks fail.**
- [ ] **Step 3: Correct the `_bot_uid_map` docstring.** Log/comment text only — D33 permits it.
- [ ] **Step 4: Run the #23 suites.**
- [ ] **Step 5: Commit.** Subject: `test(clients): pin the delegate-sync log, the ORCID/grants retry behaviour and the tool budget call sites (#23)`

---

### Task 19a: The ack detector's word-count threshold trades one error for another (DECIDE)

Self-review found this verified-OPEN item had no task.

**Issue clause (#23 COR-28b).** The `Fix:` is that "a long, substantive reply is never misclassified
as acknowledgment-only". The branch implemented a **word-count threshold**, which closes the filed
false positive by introducing an unfiled false negative: a short but substantive reply is now
classified as an ack. `verify-profiles-clients.md` confirms the trade and notes the threshold does
not close the issue's clause in general — only for replies above the cut.

- (a) **Keep the threshold, state the trade** in #23's closing comment and in the residual list, with
  the measured cut (12 words, Decision D12). Cheapest, and the issue's own filed harm is gone.
  **Recommended** unless the reviewer judges the false negative worse than the false positive.
- (b) Replace the threshold with a content test that does not rely on length. Larger change, needs its
  own table-driven cases, and D12 already recorded 12 as a deliberate choice.

- [ ] **Step 1: Record the ruling** in `progress.md`, citing D12.
- [ ] **Step 2: If (a)** — add the sentence to the residual list and the closing-comment template in
  Task 28. **If (b)** — write the table-driven failing tests first, over both the filed false positive
  and at least three short-but-substantive replies.
- [ ] **Step 3: Commit.** Subject matching the option chosen.

---

# Group E — deploy, gate and image (`#27`)

Parallel with B, C, D. Touches `scripts/ci.sh`, its two test files, `nginx/nginx.conf`,
`docker-compose.prod.yml`, `.dockerignore`, `pyproject.toml`.

### Task 20: The nested gate runs inherit the operator's environment (FIX)

**Verified evidence (AH5, half-fixed).** `5090322` fixed the `exit 127` symptom. The **env
inheritance** that made it reachable is not fixed: three of the four nested `./scripts/ci.sh`
invocations still inherit `LOCK_SMOKE`, and the consequence is now **a 180-second-timeout hang on a
real hash-pinned pip install** rather than a fast failure. The four call sites are
`tests/unit/test_ci_gate.py:81`, `:99` and `tests/unit/test_dependencies_lock.py:189`, `:223`.

**Files:** Modify `tests/unit/test_ci_gate.py`, `tests/unit/test_dependencies_lock.py`; Test: the same files

- [ ] **Step 1: Write the failing test** — with `LOCK_SMOKE=1` exported in the parent environment,
  each nested-gate test still completes in its normal time (i.e. the child did not run the smoke
  step). Assert on the child's stdout, not on wall time alone.
- [ ] **Step 2: Run it with `LOCK_SMOKE=1` set and watch it hang/fail.**
- [ ] **Step 3: Scrub the knobs in one shared helper.** Build the child env from an explicit
  allow-list rather than `{**os.environ, …}`, and use that helper at all four call sites — one of the
  four already scrubs `LOCK_SMOKE` alone, which is how the bug hid.
- [ ] **Step 4: Run both files with and without `LOCK_SMOKE=1` exported.**
- [ ] **Step 5: Commit.** Subject: `test(ci): the nested gate runs build their environment from an allow-list (#27 I1)`

---

### Task 21: nginx's memory limit is below its own zone arithmetic (FIX)

**Verified evidence (R9, measured).** `nginx/nginx.conf` declares 8 × 10 m zones plus one shared
`SSL:10m` = **90 MiB**, into a `mem_limit: 128m` whose in-file justification still says 40 MiB.
Measured **36.48 MiB idle** with 48 workers (`worker_processes auto`; `cpus: 1.0` does not reduce the
count) and `shmem 640 KiB`. Filled zones therefore land at roughly **126 MiB before TLS** — inside
the limit only while the zones are empty.

**Files:** Modify `docker-compose.prod.yml` (nginx `mem_limit`) and/or `nginx/nginx.conf` (zone sizes,
`worker_processes`); Test `tests/unit/test_nginx_config.py`, `tests/unit/test_deploy_compose.py`

- [ ] **Step 1: Write the failing test** — a static assertion that the sum of declared zone sizes
  plus a stated per-worker allowance fits inside the configured `mem_limit`, computed from the two
  files rather than hard-coded. This is the test that would have caught the original mis-sizing.
- [ ] **Step 2: Run it and watch it fail** at 90 MiB of zones in a 128 m limit.
- [ ] **Step 3: Choose and apply one** — raise `mem_limit` to cover zones + workers + TLS with
  headroom, **or** shrink the zones to what the traffic needs (the rate-limit zones are per-vhost
  since `1a430c0`, so several are small-population), **or** pin `worker_processes` to a number that
  matches `cpus: 1.0`. State the arithmetic you chose in the file's comment, replacing the stale
  40 MiB claim.
- [ ] **Step 4: Validate the config** the way the existing tests do (`nginx -t` on the rendered
  output) and re-render the merged compose to confirm the limit.
- [ ] **Step 5: Commit.** Subject: `fix(deploy): size nginx's memory limit against its actual zone arithmetic (#27 I5)`

---

### Task 22: Measure the agent container's memory before capping it (DECIDE with a measurement)

**Verified evidence (R10).** `agent: mem_limit 768m` is an unmeasured cap on the one process whose
`SIGKILL` loses data — the DB, not Slack, is the durable store, and an OOM kill skips the shutdown
flush. `stop_grace_period` cannot mitigate an OOM kill. The roster is **53 active** agents.

- [ ] **Step 1: Measure.** Run a real `agent-run` turn against the local production copy with
  `docker stats --no-stream` sampled through the turn, and record peak RSS. Deploy note 18 already
  says "measure `agent-run` with `docker stats --no-stream` during a turn before tightening" — do
  that now rather than at the deploy window.
- [ ] **Step 2: Decide** from the measurement: keep 768 m, raise it, or remove the cap for `agent`
  the way D24 removed it for `postgres`. Record the number and the ruling in `progress.md` and in
  deploy note 18.
- [ ] **Step 3: If the cap changes**, update `docker-compose.prod.yml` and the deploy-note arithmetic
  (the notes carry a per-service sum; keep it consistent).
- [ ] **Step 4: Commit.** Subject: `fix(deploy): set the agent memory limit from a measured turn, not an estimate (#27 I5, D19)`

---

### Task 23: The mypy ceiling drifts with PyPI, and its comment is stale (FIX)

**Verified evidence (R11).** `MYPY_MAX` is the same shape as the I4-e defect already fixed: a ceiling
that moves when an unpinned tool changes. `f9541ba` capped mypy to one minor version, which bounds
the drift, but the recorded number is wrong — mypy re-measures at **147**, while the comment says
145 — and `.venv-test` is still resolved from `>=` floors, so a fresh venv can differ from the
committed lock.

**Files:** Modify `scripts/ci.sh` (the `MYPY_MAX` comment and value), `pyproject.toml` if the dev
extra needs the same treatment as mypy; Test `tests/unit/test_ci_gate.py`

- [ ] **Step 1: Re-measure on a clean export** — `git archive HEAD src pyproject.toml` into scratch,
  run the exact command `ci.sh` runs, record the number.
- [ ] **Step 2: Correct the comment to the measured number** and state the slack explicitly
  (measured N, ceiling N+k, k documented).
- [ ] **Step 3: Decide whether the dev extras need pinning** so `.venv-test` cannot drift from the
  lock, and either pin them or record why not.
- [ ] **Step 4: Run** `tests/unit/test_ci_gate.py tests/unit/test_dependencies_lock.py`
- [ ] **Step 5: Commit.** Subject: `fix(ci): the mypy ceiling records the measured count and its slack (#27 I1)`

---

### Task 24: `plotly`'s move breaks the documented operator command (FIX)

**Verified evidence (R20), with the artifact's framing corrected.** `python3
scripts/build_cabo_sankey.py --help` → `ModuleNotFoundError: No module named 'plotly'`. **No running
image is broken** — the dev image (2026-07-30) and prod's (2026-08-20) both still ship plotly 6.9.0.
It breaks at the **next `docker compose build`**, i.e. this branch's deploy, and is masked locally by
a stale plotly in `.venv-test`.

**Files:** Modify `scripts/build_cabo_sankey.py` (its header comment and/or an import guard) and/or
`docs/`; Test `tests/unit/test_cabo_sankey_comment.py`

- [ ] **Step 1: Write the failing test** — the script's documented invocation is either runnable in
  the built image, or the script fails with a message that names the extra to install
  (`pip install '.[scripts]'`), not a bare `ModuleNotFoundError`.
- [ ] **Step 2: Run it and watch it fail.**
- [ ] **Step 3: Pick one** — import `plotly` lazily with an actionable error, **or** document the
  extra in the script's header and in the runbook line that tells the operator to `docker cp` it in.
  D22/#27 I4 deliberately moved `plotly` out of the runtime image, so do **not** put it back.
- [ ] **Step 4: Run the script's own test and `python -c "import ast; ast.parse(...)"`.**
- [ ] **Step 5: Commit.** Subject: `fix(scripts): build_cabo_sankey names the extra it needs instead of ModuleNotFoundError (#27 I4)`

---

### Task 25: Keep `docs/plans` out of the image (FIX)

**Verified evidence (measured in a real build, then `docker rmi`'d).** `/app` is **8.1 MB**, of which
`docs/plans` is **2.6 MB across 48 files** — including the 1.23 MB implementation plan and the
evidence/red-team tree — plus `docs/superpowers` at 408 KB: together **38 % of `/app`**. `.notes/` is
correctly absent, as are `backups`, `tests`, `mutants`, `.superpowers`, `.git` and `uv.lock`.
**Nothing sensitive is present** (zero hits for `xoxb-`, `AKIA`, private keys, `.env*`, dumps) — this
is bloat, not a leak, and #27 I3's `Fix:` is explicitly about both.

**Files:** Modify `.dockerignore`; Test `tests/unit/test_dockerignore.py`

- [ ] **Step 1: Add the entry to `test_dockerignore.py`'s MUST_EXCLUDE** for a representative path
  under `docs/plans/` and one under `docs/superpowers/`.
- [ ] **Step 2: Run it and watch it fail.**
- [ ] **Step 3: Exclude them in `.dockerignore`.** Check first whether anything in the image reads
  `docs/` at runtime (`grep -rn '"docs/' src/ scripts/`) — `docs/specs` is already excluded, which is
  precedent, but verify rather than assume.
- [ ] **Step 4: Rebuild once and re-measure `/app`**, then `docker rmi` the tag. Record before/after.
- [ ] **Step 5: Commit.** Subject: `fix(deploy): keep docs/plans and docs/superpowers out of the image (#27 I3)`

---

# Group F — process, documentation and the closure handoff

Parallel with B–E, except Task 29 which must run last in this group (it consumes every other task's
outcome). Touches only `.superpowers/sdd/…` artefacts and `docs/`.

### Task 26: Make over-implementation catchable (FIX — process)

**Why.** Five actively-harmful defects on this branch were **mitigations that outlived the condition
justifying them**, and the audit's own root-cause note is that none of the 55 new test files catches
one. The mechanism: per-task briefs were written from the plan, not from the issue's `Fix:` clause,
and no reviewer was ever asked "is this *more* than was asked?". For #20 COR-1b the excess was even
pinned by a test the branch wrote, which locked the wrong behaviour in and made the regression look
deliberate.

**Files:** Modify `.superpowers/sdd/2026-09-02-close-issues-20-27/IMPLEMENTER_PREAMBLE.md`,
`REVIEWER_CONSTRAINTS.md`

- [ ] **Step 1: Add a required brief section to the preamble** — every brief must quote the issue's
  `Fix:` clause **verbatim** for the item it implements, and the implementer must state in its report
  which parts of its change are *not* in that clause and why they are nonetheless necessary.
- [ ] **Step 2: Add a required review question to the reviewer constraints** — "Does this change do
  more than the quoted `Fix:` clause asks? For each addition, is it necessary, and does it introduce
  a behaviour the issue did not request?" A reviewer must answer it explicitly, not implicitly.
- [ ] **Step 3: Add the concrete precedents** so the question is not abstract: COR-1b (stopped
  persisting → data loss), I4-e (freshness → fresh-resolve equality → gate red on PyPI's schedule),
  COR-8 (tag routing → participation lock), COR-23 (generation-time export → unconditional writer),
  and the health probe (a DB check → a new app-level global that broke two test files).
- [ ] **Step 4: Add the file-ownership rule.** Never assign two concurrent implementers the same
  file: it caused four mixed-attribution commits (29bee9c, 5040516, eda8603, 7cc2ea3), and the fourth
  happened *after* `sdd-commit` was mandatory, because the helper serialises the index but cannot
  serialise two agents editing one file.
- [ ] **Step 5: Commit.** Subject: `docs(process): briefs quote the issue's Fix clause, and reviewers must ask whether the change exceeds it`

---

### Task 27: The gate must say that 61 live tests were not run (FIX)

**Why.** `scripts/ci.sh` says **nothing** about the live Slack tier. 61 `live_slack` tests
deselect silently whenever the credentials are not exported — which is every gate run on this branch
— and that silence is the mechanism by which the COR-1b data-loss regression reached HEAD: the test
that caught it is in that tier and passes at `18ba52c`.

**Files:** Modify `scripts/ci.sh`; Test `tests/unit/test_ci_gate.py`

- [ ] **Step 1: Write the failing test** — a default gate run prints a line naming the number of
  live-Slack tests that were not run and how to run them.
- [ ] **Step 2: Run it and watch it fail.**
- [ ] **Step 3: Print the notice** after the pytest step: count the deselected `live_slack` tests and
  say `scratchpad/run_live_slack.sh` runs them against `copi-test`. Do **not** try to run them in the
  gate — the credentials are deliberately not in the gate's environment, and the tier writes to a
  real workspace.
- [ ] **Step 4: Run** `tests/unit/test_ci_gate.py`
- [ ] **Step 5: Commit.** Subject: `fix(ci): the gate reports how many live Slack tests it did not run (#27 I1)`

---

### Task 28: Write the post-merge closure handoff (FIX — the missing deliverable)

**Why.** There is no document telling an independent agent how to close #20–#27 after merge and
deploy. `docs/plans/*handoff*` and `*closure*` return nothing. Part R of
`docs/plans/2026-09-02-close-issues-20-27.md` is the *deploy* runbook; the closure layer on top of it
does not exist.

**Files:** Create `docs/plans/2026-09-04-issue-closure-handoff.md`; Test `tests/unit/test_runbook_docs.py`

**It must contain, in this order:**

- [ ] **Step 1: Preconditions.** The branch is merged to `copi-prod`; Part R has been executed; the
  gate was green at the merge commit; the live Slack tier was run once against `copi-test` and its
  result recorded. State that closing an issue before its preconditions hold is worse than leaving it
  open, because a closed issue stops being re-verified.
- [ ] **Step 2: The per-issue closure table** — for each of #20–#27: `closes at merge` /
  `closes after deploy verification` / `close by hand with a stated carve-out` / `do not close`, each
  with the specific reason and the evidence to attach. Take the dispositions from this plan's
  Task 33 output; do not re-derive them.
- [ ] **Step 3: The `Closes` line to use in the PR**, and the ones to omit. As of this plan **#26
  must not be in it**: its DoD clause 2 ("DOC-7 is verified by following the runbook end to end on a
  workspace with legacy rows") can only be satisfied on the prod host after merge — the repair script
  has no offline mode, needs a live Slack token per row, and against the neutralised local copy
  reports 23 unverified and writes nothing.
- [ ] **Step 4: The DOC-7 verification procedure**, concretely: on the prod host, count
  `agent_messages` rows with `slack_ts IS NULL` (23 at the 2026-09-04 08:00 UTC snapshot), run
  `docker compose exec -e PYTHONPATH=/app app python scripts/backfill_slack_ts.py` (dry run) then
  `--apply`, capture the transcript, re-count, and attach the transcript to #26's closing comment.
  Note that the script has **no `--help`** (its CLI is `"--apply" in sys.argv`), so `--help` is not a
  safe no-op probe.
- [ ] **Step 5: The exact closing commands**, e.g.
  `gh issue close 23 --repo SuLab/coPI.science --comment "$(cat comment-23.md)"`, with a template
  comment per issue: what shipped, what was deliberately not done (quoting the issue's own scope
  language), the residuals a future reader must know, and the verification evidence.
- [ ] **Step 6: The carve-outs each closing comment must state.** At minimum: #20's COR-5 reader
  consequence (the dashboard/e-mail still show "unreviewed" for an implicit marker — one of COR-5's
  three filed harms, preserved by design and by test) and whatever Task 5's and Task 11's rulings
  decided; #22's word-range divergence (Task 16); #21's remaining COR-19.6 window; #27's D17
  "no GitHub Actions" decision, which the issue's own body scopes out but whose **title** does not.
- [ ] **Step 7: What to do when verification fails.** Do not close; comment with the failure, and
  open a follow-up issue that names the specific clause that could not be verified.
- [ ] **Step 8: The follow-up issues to open**, one line each, from
  `pr-body-residuals-final.md`'s "Known gaps" section plus this plan's deferred items — so the
  residuals leave the PR body and become tracked work rather than prose nobody re-reads.
- [ ] **Step 9: Add a doc test** pinning the file's existence and the presence of the per-issue table
  and the `Closes` guidance, mirroring how `test_runbook_docs.py` already pins Part R's claims.
- [ ] **Step 10: Commit.** Subject: `docs: post-merge issue-closure handoff for #20-#27`

---

### Task 29: Correct the PR body (FIX — must run last in this group)

**Verified evidence — four false claims in a tracked file.** `docs/plans/2026-09-02-close-issues-20-27-pr-body.md`:
`:179` still says `Closes #20, #21, #22, #23, #24, #25, #26, #27` **including #26**, which its own
closure audit rules NOT CLOSABLE before merge; `:28` omits **A14** from #26's "What NOT" list,
implying it was done when it has zero `src/` callers; `:26` misstates **D29** and `:75` is false; `:90`
carries a stale residual line.

**Files:** Modify `docs/plans/2026-09-02-close-issues-20-27-pr-body.md`, `pr-body-residuals-final.md`

- [ ] **Step 1: Rewrite the `Closes` line** to exactly the issues Task 33 marks `closes at merge`,
  and add one sentence naming each omitted issue and where its closure is handled (the handoff doc).
- [ ] **Step 2: Fix `:26`, `:75`, `:90` and add A14 to `:28`.** Each correction cites the verification
  report that found it.
- [ ] **Step 3: Refresh the residual list** from `pr-body-residuals-final.md` after every other task
  in this plan has landed, so a fixed item is not still listed as a live risk — the previous pass had
  to drop 3 and narrow 10 for exactly this reason.
- [ ] **Step 4: Re-read the Verification section** and update the gate figures to the final run's.
- [ ] **Step 5: Commit.** Subject: `docs(pr): correct the Closes line, A14 and three stale claims in the PR body`

---

### Task 30: Deploy note 21 is missing from the accumulator (FIX)

**Verified evidence.** The note exists in `docs/plans/2026-09-02-close-issues-20-27.md` R.12 but
`deploy-notes-accumulator.md` still stops at note 20, so the two records disagree.

- [ ] **Step 1: Append note 21** to `.superpowers/sdd/2026-09-02-close-issues-20-27/deploy-notes-accumulator.md`,
  verbatim from R.12, and re-read both files to confirm the numbering and content now match.
- [ ] **Step 2: Add any note this plan generates** (Task 22's measured agent memory, Task 6's 0029 row
  in the lock table, Task 21's nginx arithmetic).
- [ ] **Step 3: Commit.** Subject: `docs: sync the deploy-notes accumulator with Part R.12`

---

---

## STATE — verified-open items this plan deliberately does not fix

Each was checked against HEAD and is left as built, for the stated reason. Every one must appear in
the PR body (Task 29) and, where a closing comment would otherwise mislead, in that comment (Task 28).

- **#21 CL21-2 residual.** After Task 9 and Task 10, a second private channel still requires the
  **migration's own commit** to fail after `create_private_channel` has already succeeded — a
  pre-commit window that `verify-web-data-docs.md` confirms is **pre-existing, not introduced**.
  Closing it properly means adopting an existing Slack channel by name, which is new design work on a
  Slack-facing path. Deploy note 21 already tells the operator how to find and archive orphans.
- **#21 CL21-3.** The substantive half is fixed — `specs/local-db-conversations.md:65-73` says six
  writer slots and names `REMEDIATION_WRITER_SLOT = 99`, and an equivalent runtime guard exists at
  `scripts/migrate/remediate_duplicates.py:212-216`. Only a **test's name** still says "five". Rename
  it opportunistically if you are already in that file; it is not worth a commit of its own.
- **#22 item 15.** Publication titles and abstracts stored before the `itertext()` fix are still
  truncated; `35cc010` made the update branch refresh them, so they self-heal on the next pipeline
  pass per user. A backfill script would heal them sooner but touches every row of a 4,731-row table
  for a cosmetic gain. State it; offer the backfill as a follow-up issue.
- **#22 item 35 / COR-24e.** The pipeline's disk export can still land ahead of its DB commit. The
  issue's own `Fix:` scopes the ordering fix to "the ONE remaining inversion" (the private-save route,
  closed by 22.10), and D28 already recorded this as a follow-up.
- **#23 R1.** GrantBot's transport-failure re-fire is bounded (the scheduler's 15-minute tick) and is
  the deliberate consequence of `5295985` — the alternative is the silent whole-day loss the issue
  asked to remove.
- **#23 R10.** The two wall-clock pacing tests are timing-based. Stressed 15× at HEAD with 0 failures;
  a deterministic clock injection is a larger refactor than the risk warrants.
- **#26 R19.** A doc test asserts on the content of the 1.2 MB implementation plan, which is brittle.
  Narrow it to the specific claim it needs if you are in that file for another reason.
- **#27 I5-e / I5-f.** `postgres` stays uncapped (D24, rationale in-file) and `./prompts` still
  shadows the baked copy on four services (kept by design so an operator can hot-fix a prompt without
  a rebuild).

---

# Group G — final verification

Strictly sequential, strictly last. Nothing in this group may run while any other group has
uncommitted work.

### Task 31: The gate must be green

- [ ] **Step 1: Confirm the tree is clean** — `git status --short` shows nothing but untracked files
  you intend to leave.
- [ ] **Step 2: Run the whole gate in the foreground.**
  `MIGCHECK_PORT=55433 ./scripts/ci.sh 2>&1 | tee <scratch>/ci_final.log`
  Baseline to beat, measured at `5090322`: single head (now 0029), round trip clean, ruff tests clean,
  ruff `src` 251/260, lockfile consistent, mypy 147/150, **2567 passed / 120 skipped**, 78.84 % branch
  coverage, 8:16.
- [ ] **Step 3: Re-measure both ratchets on a clean export**, not the working tree —
  `git archive HEAD src pyproject.toml` into scratch, then ruff and mypy. A working-tree measurement
  already produced one false 253 reading on this branch, from a stray file copied to the wrong path.
- [ ] **Step 4: If anything is red, fix it and re-run the whole gate.** Do not report a partial run.

### Task 32: The live Slack tier must be run, and its skips accounted for (DECIDE, then verify)

**Verified evidence.** The tier is 61 tests. The last run was **52 passed / 1 failed / 8 skipped**;
the failure was #20 COR-1b, since fixed by `f29e295`. **All 8 skips have one cause** — a module-level
`skipif` on `ANTHROPIC_API_KEY` (`test_full_run_live.py:86-94`, `test_slack_pi_live.py:26-32`). That
is a **missing LLM key, not a credential gap and not a code gap** — but it means full-run bijection,
the 4000-character split, SIGTERM/restart durability and the whole PI-DM path remain covered only
with a mocked LLM.

- [ ] **Step 1: Decide whether to supply `ANTHROPIC_API_KEY` for one run.** It costs real tokens and
  exercises the real model against `copi-test`. If yes, run it and record the result. If no, record
  the decision and state in #20's and #21's closing comments that those four behaviours are
  mocked-LLM-only. Do not leave it implicit — that is exactly the silence that hid COR-1b.
- [ ] **Step 2: Run the tier** with `scratchpad/run_live_slack.sh`, which refuses to start unless its
  preflight proves the four production credentials are blanked and every fixture token resolves, via
  Slack's own answer, to team `T0BMVSBMEC8` (`copi-test`).
- [ ] **Step 3: Expect 61 minus your skip count to pass.** Any failure is either a real defect or a
  workspace-state problem — diagnose it, do not retry blindly.
- [ ] **Step 4: Run the isolation audit that was never run.** `audit-live-slack-brief.md` exists and
  `audit-live-slack.md` does not. Dispatch it, and require it to verify isolation **a-priori** (what
  credentials could the process reach) — checking the production workspace for absence of activity
  would itself violate the constraint.
- [ ] **Step 5: Tidy the workspace.** Each fixture creates a `t-`-prefixed channel and archives it;
  `scripts/slack_test_teardown.py` exists for leftovers. Report the count of unarchived `t-` channels.

### Task 33: Produce the per-issue closure decision

- [ ] **Step 1: For each of #20–#27, walk the issue's own sub-PR list and Definition of done** and
  mark every clause `met` / `met with a stated carve-out` / `not met`. The DoDs are quoted verbatim in
  `<scratch>/dod_verbatim.txt`; the per-issue closure audits are `closure-2N.md`.
- [ ] **Step 2: Write the table** into the handoff document (Task 28, Step 2).
- [ ] **Step 3: State the disposition for each issue** and the evidence a reader can check.
- [ ] **Step 4: Hand the branch owner a one-page summary**: what is closable at merge, what needs the
  deploy, what is carved out, and the follow-up issues to open.

---

## Deliberately NOT in this plan

- **The 60 residuals in `pr-body-residuals-final.md`.** They are `STATE`, not `FIX`. Re-litigating
  them would triple the plan and change no behaviour. Task 29 refreshes them; Task 28 carries the
  ones a closing comment needs.
- **#26's DoD clause 2.** Structurally impossible pre-merge (Task 28 Step 4 handles it post-deploy).
- **Rewriting the four mixed-attribution commits.** Content is correct in all four; only the subjects
  are narrower than their file lists. History rewrite is the branch owner's call, and rebasing 229
  commits to fix commit messages risks more than it repairs.
- **`R12`.** Verified **NOT-A-DEFECT**: measured both ways, `asyncio.shield` does persist the token
  triple under cancellation, and removing it loses it.
- **`closure-25`'s five "blockers".** Its own text labels them non-blockers; #25 is closable as built.
- **Adding server-side CI.** Decision D17, and #27's body scopes it out explicitly.
