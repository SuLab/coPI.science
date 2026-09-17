# Closing the remaining gaps on `close-issues-20-27` — Implementation Plan (v2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every remaining verified-open defect that stands between this branch and closing
GitHub issues #20–#27; rule on the items that need a product decision; and hand an independent agent
everything needed to close the issues after merge and deploy.

**Architecture:** The branch is **231 commits** on top of `copi-prod` @ `18ba52c`. Work is grouped so
that concurrently-running implementers never share a file — the shared checkout swept one
implementer's edit into another's commit four times on this branch. Group ordering is a DAG, stated
explicitly in `## Execution order`, not implied by task numbers.

**Tech Stack:** Python 3.11 (image) / 3.12 (`.venv-test`), FastAPI, SQLAlchemy 2 async + asyncpg,
Alembic (head 0028), pytest + testcontainers, ruff, mypy, Docker Compose, `slack_sdk`.

**Spec:** the eight GitHub issues. **Tracked canonical copies — use these, not a scratch path:**
`docs/plans/2026-09-02-close-issues-20-27-evidence/issues/issue_2{0..7}.md`.
Every task below cites the issue clause it serves. **An item's `Fix:` clause is the specification —
not the plan, not a ruling, and not a reviewer's preference.** Where a task deliberately goes beyond
its `Fix:` clause it says so in a **Beyond the clause:** line; a task without that line must not
exceed its clause.

**Plan location note:** the writing-plans skill's default is `docs/superpowers/plans/`, but that path
is gitignored here (`.gitignore:87`), so this plan lives in `docs/plans/` alongside
`2026-09-02-close-issues-20-27.md`, which is the tracked convention.

---

## What changed from v1, and why

v1 of this plan (commit `1a9176b`) was rejected by three independent adversarial reviews:
`plan-review-correctness.md` (EXECUTABLE WITH CHANGES), `plan-review-scope.md` (EXECUTABLE WITH
CHANGES) and `plan-review-completeness.md` (**MATERIALLY INCOMPLETE**), all in
`.superpowers/sdd/2026-09-02-close-issues-20-27/`. The substantive defects they found, all
re-verified against HEAD before this rewrite:

1. **v1's recommended fix for #20 COR-8 was wrong.** Returning `None` from
   `get_thread_allowed_agents` for a multi-tag root opens the thread to the whole roster:
   `src/agent/message_log.py:412-413` returns from the tag branch *before* the two-party fallback,
   and `:428`'s comment states `None` means "Thread still open — anyone can join". See Task 3.
2. **v1's Task 1 Step 5 would have widened a prompt-injection.** Withdrawn — see
   `## Named follow-up issues`, item F1.
3. **v1 ordered the two private-channel fixes backwards.** The `refined_in_channel` guard before the
   offline commit yields *zero* channels on retry. Task 11 now precedes Task 12.
4. **v1's migration was half unnecessary.** `reply_budget_consumed` is derivable; only the
   `proposal_reviews` change needs schema — and the carrier itself is contested. See Tasks 7–9.
5. **v1 asserted a green gate it never had.** `ci_baseline.log` — the run whose figures v1 quoted as
   "2567 passed / 120 skipped … 8:16" — ends `1 failed` and `GATE_EXIT=1`, at 15:36, one minute
   before `a47b6c5` fixed that failure. **No full gate has been run at or after `a47b6c5`.** The last
   green run is `ci_final3.log` (11:39, `2534 passed`, `CI passed`), which predates five commits.
   Task 1 now proves it, first, and no other task may start until it does.
6. **v1 told an executor to run a real `agent-run` turn with no isolation**, in a checkout whose
   `.env` holds live production Slack tokens. Rewritten with a hard preflight — Task 30.
7. **v1 committed six tasks' output to `.superpowers/`, which is gitignored** (`.gitignore:122`;
   `git ls-files .superpowers` = 0). All rulings now go to a tracked file — Task 2.
8. **v1's spec, DoD source and the only live-Slack isolation harness lived in one session's `/tmp`.**
   Task 2 lands the harness in `scripts/`; the spec pointer above is the tracked copy.
9. **v1 diagnosed the mypy ceiling as PyPI drift.** It is not: the 145→147 move is two
   `tuple[Publication | None, bool]` errors this branch introduced. Task 27.
10. **v1's claim "the only unmet DoD clause is #26's second" was false** — see `## Definition of done`.
11. **v1 dropped 50 findings** from `audit-phase8-functional.md`, `audit-phase8-migration.md` and the
    *Harmless / maintenance tax* section of `audit-over-implementation.md`, which it cited zero times.
    Every one is now dispositioned in `## Traceability` or `## Named follow-up issues`.
12. **v1 mis-cited 14 facts.** Corrected inline, each with what was measured.

## Global Constraints

Copied verbatim from the project's own rules; every task's requirements implicitly include these.

- **No prompt changes (Decision D33).** No file under `prompts/` may change, and no inline
  model-facing string may change. Verified across all **231** commits: `git diff --stat
  18ba52c..HEAD -- prompts/` is empty. Any task touching a file that holds prompt text must hash
  every string constant (AST walk) before its first commit and after its last, and put the
  comparison in its report.
- **`./scripts/ci.sh` is the whole gate; there is no server-side CI (Decision D17).** Run it in the
  FOREGROUND with `MIGCHECK_PORT=55433`. Port ownership, so nobody guesses: **55432** is held by an
  unrelated `blackbird-db-copy` container; **55433** is the gate's own throwaway migration-check
  Postgres; **55434** is `copi-prodtest-db`, the disposable production copy. Ceilings:
  `SRC_LINT_MAX=260` (measured **251**), `MYPY_MAX=150` (measured **147**), `COV_MIN=60` (measured
  **78.84 %**). **The mypy slack is 3, not the 5 the `ci.sh` comment claims** — Task 27 fixes the
  cause and restores it to 5. Until then, ~20 code-touching tasks share 3 findings of headroom.
- **`TEST_DATABASE_URL` is required for any bare `pytest` run of a DB-backed test.** Every test
  command in this plan is written to run on the host from the repo root with `.venv-test`, which
  spins throwaway Postgres via testcontainers and needs no `TEST_DATABASE_URL`. If you instead run
  inside a container, read CLAUDE.md's §Testing first and never point it at `copi`.
- **Never `git push`. Never open the PR.** The branch owner opens it.
- **Never read anything under `backups/`** — it holds live production credentials.
- **Never contact production Slack, and never run the simulation against production.** Live Slack
  work runs only against the `copi-test` workspace (team `T0BMVSBMEC8`) through
  `scripts/run_live_slack.sh` (landed by Task 2), which refuses to start unless its preflight proves
  `SLACK_BOT_TOKEN_CRAVATT`, `SLACK_BOT_TOKEN_WISEMAN`, `SLACK_CONFIG_TOKEN` and
  `SLACK_CONFIG_REFRESH_TOKEN` are blanked in the environment (an empty env var overrides `.env` —
  proved empirically, not assumed) and that every fixture token resolves, via Slack's own
  `auth.test`, to `T0BMVSBMEC8`.
- **Never run `docker compose` against the production compose files** from the dev machine. A
  `docker compose … config` render is allowed.
- **Commit only via `.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> <paths…>`**
  (`flock -w 180` + `git commit --quiet --only -F <msg> -- <paths>`). A plain `git commit` in this
  shared checkout sweeps other implementers' staged files into your commit — it happened four times
  (`29bee9c`, `5040516`, `eda8603`, `7cc2ea3`). **Every commit step below prints its own path list;
  use exactly those paths.**
- **`.superpowers/` is gitignored.** Anything that must survive the branch goes in `docs/`. Reports
  and scratch analysis may stay in `.superpowers/`; **decisions and deliverables may not.**
- **Never `git stash`, `git reset`, `git checkout`, or `git checkout -- <file>`.** Two `git stash`
  runs wiped every implementer's uncommitted work earlier on this branch.
- Commit trailers, both lines, on every commit:
  `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_015Nnc7rWXX4MbGSnGNEthzx`

## How to read a verdict in this plan

- **FIX** — a verified-open defect with a concrete code change and no open question.
- **DECIDE** — the issue's text and the branch's behaviour genuinely conflict, or the fix trades one
  harm for another. Produces a written ruling in `docs/plans/2026-09-04-decisions/task-<N>.md`
  (one file per task — see Task 2) plus whatever code the ruling implies. **The executor may rule** when the plan's
  recommendation is uncontested; where this plan records a *contested* recommendation it says
  **ESCALATE** and the branch owner rules. Two of v1's six recommendations were wrong, so this
  distinction is not a formality.
- **STATE** — correct as built, but a reader of the PR or the issue would be misled without a
  sentence. These land in the PR body, the closing comments, or the deploy notes.

## Execution order (a DAG, not the task numbering)

```
Task 1  (prove the gate)            ← nothing else may start until this passes
Task 2  (tracked decisions + harness)
   ├── Group A  engine (#20)        Tasks 3-10      ── runs ALONE; no group may run beside it
   ├── Group B  web + email (#21,#24) Tasks 11-17
   ├── Group C  profiles (#22)      Tasks 18-21
   ├── Group D  clients (#23)       Tasks 22-24
   └── Group E  deploy/gate (#27)   Tasks 25-31
Task 32 (per-issue closure decision)   ← needs A-E complete
Task 33 (closure handoff document)     ← needs Task 32
Task 34 (final gate, green)            ← needs all code complete
Task 35 (live Slack tier)              ← needs Task 34
Task 36 (PR body)                      ← LAST: consumes 32's dispositions and 34/35's figures
```

**Group A runs alone.** Groups B, C, D and E are mutually parallel and each is internally
sequential. Group A owns `src/agent/simulation.py` exclusively; **no other task in this plan touches
it** (v1's Task 19 did, which is why that docstring fix now lives in Task 24 with a different file).
`scripts/ci.sh` and `tests/unit/test_ci_gate.py` are owned exclusively by **Group E**.

---

## File Structure

No file appears in two groups. Group A's exclusive ownership of `simulation.py` and Group E's of
`ci.sh`/`test_ci_gate.py` are the two rules v1 broke.

| File | Responsibility for this plan | Group |
|---|---|---|
| `docs/plans/2026-09-04-decisions/task-<N>.md` (new, one per task) | that task's ruling, evidence and closing-comment consequence; tracked. **One file per task, never a shared one** | each task |
| `scripts/run_live_slack.sh`, `scripts/live_slack_preflight.py` (new) | tracked live-tier isolation harness | Task 2 |
| `src/agent/simulation.py`, `src/agent/message_log.py`, `src/agent/state.py` | engine: participation, strikes, activation cursor, log re-queue, inbound ordering, reply budget, implicit review | **A** |
| `alembic/versions/0029_*.py`, `src/models/agent_registry.py`, `src/models/agent_activity.py` | one revision, if Task 8 rules for schema | **A** |
| `scripts/migrate/preflight.py`, `scripts/migrate/postflight.py` | 0029's `DEFAULT_TARGET`, `REVISION_ORDER`, `PLANNED_OBJECTS`, expectations | **A** |
| `src/routers/agent_page.py`, `src/services/private_channels.py`, `src/services/pi_inbox.py` | reopen idempotence, offline commit, lost-race disclosure, dead re-bind machinery | **B** |
| `src/services/email_inbound.py`, `src/services/email_notifications.py` | terminal handler, `"expired"` no-send paths, paused-mail allowlist | **B** |
| `src/routers/admin.py`, `templates/agent/dashboard.html`, `templates/admin/discussions.html` | the review-count predicate and the `Rating: 0/4` rendering | **B** |
| `src/services/profile_pipeline.py`, `scripts/vet_publications.py`, `scripts/resynth_from_current_pubs.py` | seed guard, synthesis fallback, publication-text repair | **C** |
| `src/services/pubmed.py`, `src/services/http_retry.py`, `src/agent/funding_rules.py`, `src/agent/foa_pattern.py`, `src/agent/tools.py`, `src/routers/invite.py` | NCBI pacing/deadline/per-loop map, apostrophe class, FOA case, missing client tests | **D** |
| `scripts/ci.sh`, `tests/unit/test_ci_gate.py`, `tests/unit/test_dependencies_lock.py` | nested-gate env scrubbing, mypy ceiling, live-tier notice | **E** |
| `nginx/nginx.conf`, `docker-compose.prod.yml`, `.dockerignore`, `src/main.py`, `scripts/build_cabo_sankey.py` | zone arithmetic, image bloat, health-probe pre-ping, plotly | **E** |
| `docs/plans/2026-09-04-issue-closure-handoff.md` (new) | post-merge closure instructions | Task 33 |
| `docs/plans/2026-09-02-close-issues-20-27-pr-body.md` | corrected `Closes` line and residual list | Task 36 |
| `docs/plans/2026-09-02-close-issues-20-27.md`, `tests/unit/test_runbook_docs.py` | Part R deploy runbook: per-user pipeline re-run, 0029 lock row — **edited only by Task 33**, which applies what Tasks 8 and 20 measured | Task 33 |

---

## Traceability — every verified finding to its disposition

Sources, all in `.superpowers/sdd/2026-09-02-close-issues-20-27/`: the four `verify-*.md`
re-verification passes, the eight `closure-2N.md` audits, `audit-over-implementation.md`,
`audit-phase8-functional.md`, `audit-phase8-migration.md`, and the three plan reviews. `FIXED` rows
need no work; they are listed so nobody re-opens them and so Task 36 can prune the PR body against
them. **`→ F<n>` means a named follow-up issue, listed in `## Named follow-up issues`** — a
disposition, not a silent drop.

### #20 — engine

| id | source | disposition |
|---|---|---|
| AH2 / COR-8 (participation lock) | verify-engine §1 | **Task 3** |
| COR-8 DB-inbound single-tag route | plan-review-correctness | **→ F1** (pre-existing; fixing it would widen a prompt injection) |
| Phase-5 strike key shared across agents (`simulation.py:376`) | audit-over-impl (harmless) | **Task 4** — prerequisite for Task 5 |
| AH3 / COR-1b two-strike back-off | verify-engine §2 | **Task 5** (DECIDE) |
| R16 (first activation loses backlog) | verify-engine | **Task 6** |
| R5 (unbounded LLM-log re-queue) | verify-engine | **Task 6** |
| R15 / COR-10(3) (inbound append order) | verify-engine | **Task 7** (DECIDE, **ESCALATE**) |
| CL20-3 / COR-5 (implicit review not persisted) | verify-engine | **Task 8** (DECIDE, **ESCALATE**) + **Task 9** |
| Candidate A pt 2 (`proposal_reviews` is the wrong carrier; `ondelete=CASCADE`) | audit-over-impl | **Task 8** — the argument Task 8 must answer |
| CL20-1 / R17 (reply budget resets on rebuild) | verify-engine | **Task 10** |
| CL20-4 (COR-5's reader consequence) | verify-engine | **STATE** → Task 33 carve-out |
| CL20-5 residual (233 `rating=0` markers counted; `Rating: 0/4` rendered 130+×) | phase8 breakage 1+2, verify-deploy finding 4 | **Task 16** |
| E7c (rebuild re-seeds `pi_context`) | audit-over-impl (harmless) | **→ F2** |
| Candidate B (`_dead_thread_ids` tombstone drops PI rows silently) | audit-over-impl | **→ F3** |
| CL20-2 | verify-engine | FIXED `c4de842` |
| CL20-5 (negative counts / clamp) | verify-engine | FIXED `f9541ba` — 0 of 53 negative, re-measured; 22 with the pre-fix query |
| CL20-6 | three reports | FIXED `22c3d78` + `a47b6c5` |

### #21 / #24 — web and e-mail

| id | source | disposition |
|---|---|---|
| N1-b (web reopen mints a 2nd private channel) | verify-web-data-docs Q1 | **Task 12** — merge blocker |
| N1-a (`_migrate_offline` never commits) | verify-web-data-docs | **Task 11** — **must precede Task 12** |
| #24 V5 (iii) (offline path discards the PI's inbox guidance row) | audit-over-impl (harmless) | **Task 11** |
| #24 V5 (i) (`scalar_one()` → 500 in the sibling handler) | audit-over-impl (harmless) | **Task 13** |
| verify-deploy finding 1a (dead re-bind machinery) | verify-deploy | **Task 13** |
| R8 / V5 (lost review race shows a success page) | verify-web-data-docs Q4 | **Task 14** (DECIDE) |
| R13 / COR-32 (terminal handler eats transient failures) | verify-engine | **Task 15** |
| R14 / V4-3 (`"expired"` on three no-send paths) | verify-engine | **Task 17** |
| over-impl R2 / V4-4 (paused mail bypasses the allowlist) | verify-profiles-clients | **Task 17** |
| CL21-2 (pre-commit orphan-channel window) | verify-engine | **Task 12 Step 6** (DECIDE) — v1's STATE **rejected** |
| CL21-3 | verify-engine | **STATE** — substantive half fixed; only `tests/unit/test_ids.py:101`'s name says "five" |
| M2 (`Form(...)` empty-string trap, systemic; `reopen.guidance`'s 400 unreachable) | phase8 functional | **Task 13** for the reopen site; **→ F4** for the other eight |
| D6 upsert unserialized (two reviewers both see success) | audit-over-impl | **→ F5** |
| #24 C2 `_MAX_MANIFEST_RETRY_AFTER = 30.0` | audit-over-impl (harmless) | **→ F6** |
| N3 (no static guard against a blocking `httpx` call on an async route) | verify-web-data-docs | **→ F7** |
| N2 | verify-web-data-docs | FIXED — code `7cc2ea3`, pinned `a5666b4` |
| CL21-1, #26 blockers 3/4/5 | verify-engine, web | FIXED `34d3c15` (#26 blocker 3 residual → **→ F8**) |
| #24 blocker 1 (concurrent-insert DoD) | verify-web-data-docs | FIXED `1230419` |

### #22 — profiles

| id | source | disposition |
|---|---|---|
| over-impl R1 (seed guard keyed on `onboarding_complete`) | verify-profiles-clients | **Task 18** |
| over-impl R18 (validating-but-incomplete synthesis blanks curated lists) | verify-profiles-clients | **Task 19** |
| #22 item 15 + phase8 breakage 3 (truncated titles / 299 empty abstracts) | phase8 functional | **Task 20** — v1's STATE **rejected** |
| #22 blocker 2 (word-count gate vs prompt) | verify-profiles-clients | **Task 21** (DECIDE) |
| #22 item 35 / COR-24e (disk export ahead of DB commit) | verify-profiles-clients | **STATE**, restated with the #29 linkage — Task 33 carve-out |
| phase8 C1 (`content: str = Form(...)` broke the clear path) | phase8 functional | FIXED `7cc2ea3` (`agent_page.py:1432` is `Form("")`) |
| #22 blockers 1, 3; item 44 | verify-profiles-clients | FIXED `8341b21`, `42f03f4`, `7cc2ea3` |
| migration 0025 COALESCE treats `''` as present | audit-over-impl (harmless) | **→ F9** |
| M3 (`derive_agent_identity`: `iii`, non-ASCII `agent_id` → Slack slug + file path) | phase8 functional | **→ F10** |
| M4 (blank private save records a 3-byte revision) | phase8 functional | **→ F11** |

### #23 — external clients

| id | source | disposition |
|---|---|---|
| R3, R4 / COR-28a, COR-27b (apostrophe class, FOA case) | verify-profiles-clients | **Task 22** |
| R2, R7, over-impl R3, R4 (retry budget, NCBI pacing, per-loop semaphore) | verify-profiles-clients | **Task 23** |
| R5, R6, R8, R9, over-impl R7 (missing tests, stale docstring) | verify-profiles-clients | **Task 24** |
| over-impl R6 / COR-28b (ack word-count threshold) | verify-profiles-clients | **STATE** with the measured cut (D12) — Task 33 carve-out |
| COR-27 sharper half (case-insensitive extract, case-sensitive compare) | audit-over-impl (harmless) | **Task 22 Step 4** |
| COR-30 (charging only on success unbounds the per-thread NCBI budget) | audit-over-impl (harmless) | **→ F12** |
| #23 R1, R10 | verify-profiles-clients | **STATE** — bounded by the 15-min tick; 15× stress, 0 failures |

### #25 / #26 — data contract and docs

| id | source | disposition |
|---|---|---|
| phase8 I3 (self-service PI delete orphans an active agent; `0026` removed the guardrail) | phase8 functional | **Task 25** (DECIDE, **ESCALATE**) — v1's "NOT BLOCKING" label **rejected** |
| #25 note 2 / P2 (no regression test for `passive_deletes`) | verify-web-data-docs | **Task 25 Step 5** |
| #26 blocker 1 (`Closes #26`) | verify-web-data-docs | **Task 33 + Task 36** |
| #26 blocker 2 (A14 omitted from the PR body) | verify-web-data-docs | **Task 36** |
| #26 DOC-B (2,211 rows with `agent_id IS NULL`, invisible to gated agents) | closure-26, phase8 breakage 4 | **→ F13** |
| #26 vacuous `test_prompt_hygiene` asserts (green at `18ba52c`) | closure-26 | **→ F14** — and named in `## Definition of done` |
| #26 bot-name divergence / no unique constraint on `agents.bot_name` | closure-26 | **→ F15** |
| #26 R19 (doc test asserts on the 1.2 MB plan) | verify-web-data-docs | **STATE** — but see Task 31's landmine warning |
| closure-25 "blockers" 1-5 | closure-25 | **note 1 → Task 25**; note 2 → Task 25 Step 5; 3-5 STATE. v1's blanket dismissal **rejected** |
| #25 D1.4 (`IntegrityError → 409 "please retry"`) | audit-over-impl (harmless) | **→ F16** |

### #27 — deploy, gate and image

| id | source | disposition |
|---|---|---|
| AH5 (nested gate inherits the operator's environment) | verify-deploy | **Task 26** |
| over-impl R11 + verify-deploy (mypy ceiling; real cause is our own annotation bug) | verify-deploy | **Task 27** |
| mypy behavioural test asymmetry (`test_ci_gate.py:90` asserts a string `ci.sh` never echoes) | verify-deploy | **Task 27 Step 5** |
| Gap 1 (live-Slack tier never run by the gate; 61 + **42** `live_api` silently skipped) | verify-deploy | **Task 28** |
| verify-deploy `2a-new` (`pool_pre_ping=False` → 503 on a healthy DB) | verify-deploy | **Task 29** |
| verify-deploy finding 2b (`src/main.py`'s two stale NullPool claims) | verify-deploy | **Task 29** |
| over-impl R9 (nginx zones 90 MiB in a 128 m limit) | verify-deploy | **Task 30** |
| over-impl R10 (agent `mem_limit` unmeasured) | verify-deploy | **Task 30** (measure, then DECIDE) |
| over-impl R20 (plotly breaks the documented command at the next build) | verify-profiles-clients | **Task 31** |
| `docs/plans` baked into the image (2.6 MB / 49 files) | verify-deploy | **Task 31** |
| verify-deploy finding 3b (`--hash=` lines never inspected; extras invisible) | verify-deploy | **→ F17** |
| verify-deploy finding 3a (a *removed* dependency is undetectable) | verify-deploy | **→ F18** |
| verify-deploy finding 3c (`ci.sh` text-grep maintenance tax) | verify-deploy | **STATE** |
| #27 I5-e (`postgres` uncapped, D24) | verify-deploy | **STATE** |
| #27 I5-f (`./prompts` mount widened to a 4th service and test-locked) | audit-over-impl | **Task 30 Step 6** (DECIDE) — v1's "by design" **rejected**: it conflicts with D33 |
| #27 I1-g / D17 (no server-side CI) | verify-deploy | **STATE** — the issue's body scopes it out, its *title* does not; Task 33 must say so |
| #27 D1 cascade cost (`user: "0:0"`, `--via-run`) | audit-over-impl (harmless) | **STATE** |
| phase8 C2 (`/api/health` never returns against a paused Postgres) | phase8 functional | **Task 29 Step 6** — closure is currently *inferred*, never asserted |
| phase8 migration C1, I2, I3, I4, M5-M11 (postflight blind spots) | phase8 migration | **→ F19** — and Task 8 Step 9 may not use "postflight exits 0" as its sole gate |
| phase8 migration N1-N9 (overstated claims, incl. N4's lock-window) | phase8 migration | **Task 8 Step 8** must not inherit the 0.3 s figure |
| phase8 I1, I2 (admin `/link` and `/approve` 500 on the likely input) | phase8 functional | **→ F20** |
| phase8 I4 (the phase-8 measurements ran against an empty `llm_call_logs`) | phase8 functional | **STATE** — an evidence caveat on every "measured on the copy" number here |
| AH1, AH4 | verify-deploy | FIXED `5090322` |
| closure-27 blockers 1, 2, 3 | verify-deploy | FIXED `f9541ba` + `a47b6c5` |
| over-impl R12 (`asyncio.shield` persists the token triple) | web/data/docs | **NOT-A-DEFECT** — measured both ways |

---

## Definition of done — the honest position

v1 claimed "the only unmet DoD clause is #26's second". That was false. #27 states the governing
clause **for every issue in this backlog**: "each PR ships a test that covers its defect line and
fails against the pre-fix code." Measured against that:

- **#26 clause 2** (DOC-7 verified by following the runbook on a workspace with legacy rows) — unmet,
  and structurally impossible pre-merge. Task 33 Step 4 carries the post-deploy procedure.
- **#23's clause is unmet today.** Task 24 exists precisely because three sub-PRs shipped without
  the test: R5 (`grep -rn "skipping delegate Slack-ID sync" tests/` = **0**, re-measured), R6, R8.
  Task 24 closes it.
- **#25 P2 has no regression test at all** — nothing in `tests/` notices `passive_deletes=True`
  being removed. Task 25 Step 5 closes it.
- **#26 has two `test_prompt_hygiene.py` assertions that are already green at `18ba52c`** — tests
  that by construction cannot fail against pre-fix code, i.e. the literal negation of the clause.
  **→ F14**; Task 33's #26 comment must disclose it.
- **The #27 mypy step's own pin is red only via `TimeoutExpired`, not an assertion**
  (`test_ci_gate.py:90` asserts `"-m pytest" not in proc.stdout`, a string `ci.sh` never echoes).
  Task 27 Step 5 closes it.
- **#21's clause is met by declaring its premise false** ("takes `worker/main.py` from 0 % coverage" —
  it was 71.83 % at the base commit; it is now 77.72 %). Honest, but it means the clause was never
  satisfiable as written, so Task 33 must say so in #21's closing comment rather than report it met.
- **Met, verified, do not re-do:** #21's `docs/inbound-email.md` prerequisites (`34d3c15`); #22's
  migration test against a table that already contains duplicates (`test_migration_0025.py`, 3/3 red
  against base); #23's "table-driven over the cases above" (the issue names only U+2019 — see
  Task 22's **Beyond the clause** line); #24's concurrent-insert test (`1230419`) and its C2
  non-blocking assertion (`tests/unit/test_admin_provisioning.py:117`); #25's `test_db_contract`
  PI-deletion test and the gate's `alembic upgrade head` round trip; #26's DOC-5 rendering assertion.

**Every task in this plan that changes behaviour ships a test that is red before the change.** Two
tasks are exceptions and say so: Task 30's memory measurement (a resource value, pinned by
`tests/unit/test_deploy_compose.py`) and the documentation-only tasks.

---

# Task 1: Prove the gate is green at HEAD (FIX — blocks everything)

**Why this is first.** v1 assumed a green gate. The last completed gate run on this branch
(`ci_baseline.log`, 15:36) ended `1 failed` / `GATE_EXIT=1`; `a47b6c5` (15:37) fixed that failure and
**no full gate has been run since**. Nobody may start a task against an unknown baseline.

- [ ] **Step 1: Confirm the tree is clean.** `git status --short` — nothing but untracked files you
  intend to leave.
- [ ] **Step 2: Run the whole gate in the foreground.**
  `MIGCHECK_PORT=55433 ./scripts/ci.sh 2>&1 | tee /tmp/ci_head.log; echo "GATE_EXIT=$?"`
  Expected: `CI passed.` and `GATE_EXIT=0`. Expected figures, from the last two runs: single head
  `0028`, round trip clean, ruff tests clean, ruff `src` **251**/260, lockfile consistent, mypy
  **147**/150, **2568 passed / 120 skipped** (2567 + the `test_health_ok` that `a47b6c5` fixed),
  ~78.8 % branch coverage, ~8 min.
- [ ] **Step 3: If it is red, fix it before anything else** and record what was wrong in
  `docs/plans/2026-09-04-decisions/baseline.md` (created by Task 2 — do Task 2 first in that case). A red baseline invalidates every "watch it fail" step in this plan, because you cannot tell
  your new failure from the standing one.
- [ ] **Step 4: Record the figures** in `docs/plans/2026-09-04-decisions/baseline.md`. Task 34 compares
  against them and Task 36 quotes them.

# Task 2: Land the tracked decisions file and the live-tier isolation harness (FIX — blocks everything)

**Why.** `.gitignore:122` ignores `.superpowers/`, and `git ls-files .superpowers` returns 0 files.
v1 wrote six DECIDE rulings and its whole process fix into that tree, where `sdd-commit`
(`git commit --only -- <paths>`) cannot commit them and a post-merge agent could never read them.
Separately, the only harness standing between the live Slack tier and **production** Slack lived in
one session's `/tmp`.

**Files:**
- Create: `docs/plans/2026-09-04-decisions/README.md`, `docs/plans/2026-09-04-decisions/baseline.md`
- Create: `scripts/run_live_slack.sh`, `scripts/live_slack_preflight.py`
- Test: `tests/unit/test_live_slack_preflight.py` (new)

- [ ] **Step 1: Create the decisions directory, one file per task.** A single shared decisions file
  would be a file five parallel groups all write to — the exact condition that produced four
  mixed-attribution commits on this branch, and one `sdd-commit`'s flock cannot prevent (it serialises
  the index, not two agents editing one file). So: create
  `docs/plans/2026-09-04-decisions/README.md` stating the convention, and every task that records
  anything writes **only** `docs/plans/2026-09-04-decisions/task-<N>.md`, with these headings:
  `## Ruling` (options considered, option chosen, why), `## Evidence` (what you measured, with
  commands), `## Consequence a closing comment must state`. Task 1 additionally writes
  `docs/plans/2026-09-04-decisions/baseline.md`; Task 32 aggregates every file into
  `## Closure dispositions` there. Wherever this plan says "the decisions file", it means **your
  task's own file**.
- [ ] **Step 2: Write the preflight as a script that exits non-zero unless isolation is proven.**
  It must assert, in this order, and print each result: (1) `SLACK_BOT_TOKEN_CRAVATT`,
  `SLACK_BOT_TOKEN_WISEMAN`, `SLACK_CONFIG_TOKEN`, `SLACK_CONFIG_REFRESH_TOKEN` are each **present
  in the environment and empty** — presence matters, because an absent var falls through to `.env`
  (pydantic-settings precedence; an empty env var overrides `.env`, which was proved empirically);
  (2) `get_slack_tokens()` yields zero usable tokens and `env_token()` returns `None`;
  (3) every `SLACK_TEST_BOT_TOKEN_{SU,CRAVATT,WISEMAN}` present resolves via Slack's own `auth.test`
  to team **`T0BMVSBMEC8`** (`copi-test`) — and refuse if any resolves elsewhere;
  (4) `SLACK_TEST_WORKSPACE`, `SLACK_TEST_PI_USER_ID`, `SLACK_TEST_BOT_TOKEN_SU` are set, since
  `tests/conftest.py:158-171` skips the tier without them and a silent skip is what let the COR-1b
  regression through.
- [ ] **Step 3: Write the runner** `scripts/run_live_slack.sh`: run the preflight, abort on non-zero,
  then `pytest tests/ -m live_slack -p no:cacheprovider` with the tier's env exported. Never source
  `.env`.
- [ ] **Step 4: Write the failing test first** for the preflight's refusal paths — a non-blanked
  production token, and a fixture token whose `auth.test` returns a foreign team id (stub the Slack
  call; do not hit Slack in a unit test). Run it, watch it fail, implement, watch it pass.
- [ ] **Step 5: Confirm the preflight refuses** on a deliberately-bad environment before you trust it
  on a good one. Record the transcript of the refusal in the decisions file.
- [ ] **Step 6: Commit.**
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  docs/plans/2026-09-04-decisions/ scripts/run_live_slack.sh \
  scripts/live_slack_preflight.py tests/unit/test_live_slack_preflight.py
```
  Subject: `feat(test): tracked live-Slack isolation harness and a tracked decisions log (#27)`

---

# Group A — engine (`#20`). Runs alone. Tasks 3-10 are strictly sequential.

### Task 3: A multi-tag human root keeps every tagged bot, and no one else (FIX)

**Issue clause (#20 COR-8, verbatim `Fix:`):** "translate `<@Uxxx>` to the agent id before matching".
It asks for **tag routing**. The branch also wired the translation into the thread-participation
*lock* and narrowed it to the first tagged id.

**Verified evidence.** `src/agent/message_log.py:411-413`:
```python
tagged_id = self._extract_tagged_agent(root.content)
if tagged_id and tagged_id != poster_id:
    return {poster_id, tagged_id} if poster_id else {tagged_id}
```
For a human root tagging two bots, `18ba52c` returned `None` and HEAD returns `{'su'}`. The live harm
is `simulation.py:2695-2701`, the Phase-5 post-LLM check, which lacks the `len(allowed) >= 2` guard
the pre-filter at `:2399-2400` has: the second bot the PI tagged burns one Phase-5 LLM call per turn
producing nothing, and the PI's second tag is silently ignored.

**Do NOT implement v1's recommendation.** v1 said "return `None` (open) when the root translates to
more than one agent id". That is wrong: the tag branch `return`s **before** the two-party fallback,
and `:428`'s own comment says `None` means "Thread still open — anyone can join". Returning `None`
would open a tagged thread to the entire roster permanently — a regression against HEAD *and*
`18ba52c` for a bot root tagging two bots, and a violation of `specs/agent-system.md:278-287` ("No
third agent may join"). v1's fallback (add the `len >= 2` guard at the consumer) is also wrong: three
consumers lack it (`simulation.py:1281`, `:1474`, `:2696`), and at `:2696` adding it would *let*
third parties post into a single-tag reserved thread.

**The fix:** return the poster plus **every** tagged non-service agent id, so the reserved-participant
property is preserved and widened to exactly the set the PI addressed.

**Files:**
- Modify: `src/agent/message_log.py` (`get_thread_allowed_agents`, `:411-413`)
- Test: `tests/unit/test_message_log.py`

**Interfaces:** consumes `extract_bot_mentions(text, uid_map)` (`src/agent/mentions.py`, existing)
and the log's bot-uid/bot-name maps. Produces no new symbols; `get_thread_allowed_agents` keeps its
`set[str] | None` signature.

- [ ] **Step 1: Read the function and both maps first.** `get_thread_allowed_agents` is at
  `src/agent/message_log.py:388`. Note that `_extract_tagged_agent` resolves a single tag and that
  the uid map is populated by a setter — **v1's test snippet failed because it set only the uid map
  and never the bot-name map, so the function returned `None` and the test passed vacuously.** Find
  the real setters (`grep -n 'def set_.*map' src/agent/message_log.py`) and use both.
- [ ] **Step 2: Write three failing tests**, not one:

```python
# tests/unit/test_message_log.py
class TestMultiTagRootParticipation:
    """#20 COR-8 asked for tag ROUTING. Locking participation to the *first*
    translated uid regressed the case the issue exists to fix, and opening the
    thread instead would break specs/agent-system.md:278-287 ("No third agent
    may join"). The correct set is the poster plus every tagged agent."""

    def test_a_human_root_tagging_two_bots_allows_exactly_those_two(self, log_with_two_bots):
        log = log_with_two_bots            # uid map AND name map both set
        log.append(_human_entry(thread_ts="100.1",
                                content="Hey <@U111> and <@U222>, compare notes"))
        assert log.get_thread_allowed_agents("100.1") == {"su", "wiseman"}

    def test_a_third_agent_is_still_excluded(self, log_with_two_bots):
        log = log_with_two_bots
        log.append(_human_entry(thread_ts="100.2", content="<@U111> <@U222> go"))
        allowed = log.get_thread_allowed_agents("100.2")
        assert allowed is not None, "a tagged thread must stay reserved, not open to the roster"
        assert "cravatt" not in allowed

    def test_a_single_tag_root_is_unchanged(self, log_with_two_bots):
        log = log_with_two_bots
        log.append(_human_entry(thread_ts="100.3", content="<@U111> thoughts?"))
        assert log.get_thread_allowed_agents("100.3") == {"su"}
```
  The second test is the one that fails if someone re-implements v1's option (a). Write the
  `log_with_two_bots` fixture explicitly; do not rely on an existing one.
- [ ] **Step 3: Run them and watch tests 1 and 2 behave as described.**
  `.venv-test/bin/python -m pytest tests/unit/test_message_log.py -k MultiTag -q -p no:cacheprovider`
  Expected: test 1 FAILS (`{'su'} != {'su', 'wiseman'}`), test 3 PASSES, test 2 PASSES at HEAD
  (it is the regression guard, and it must keep passing after your change).
- [ ] **Step 4: Implement.** Replace the single-tag branch with `extract_bot_mentions` over the root,
  translate every mention, drop service ids and the poster's own id where the existing code does, and
  return `{poster} | tagged` (or `tagged` when there is no poster). If `extract_bot_mentions` finds
  nothing, fall through to the two-party rule exactly as today.
- [ ] **Step 5: Re-check the three consumers.** `simulation.py:1281`, `:1474`, `:2696` — with the
  source fixed, confirm by reading each that none of them now needs the `len >= 2` guard, and say so
  in your report. Do **not** add the guard at `:2696`: with a correct `allowed` set it would admit
  third parties to a single-tag thread.
- [ ] **Step 6: Run the engine unit set.**
  `.venv-test/bin/python -m pytest tests/unit/test_message_log.py tests/unit/test_simulation_logic.py tests/unit/test_mentions.py tests/unit/test_roster_sync.py tests/unit/test_thread_not_found.py -q -p no:cacheprovider`
- [ ] **Step 7: Confirm no prompt string moved.** AST-walk every string constant in the touched files
  before and after; diff. Only log/comment text may differ.
- [ ] **Step 8: Commit.**
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  src/agent/message_log.py tests/unit/test_message_log.py
```
  Subject: `fix(agent): a multi-tag root reserves the thread for every bot the PI tagged (#20 COR-8)`

---

### Task 4: Post-failure strikes are per-agent, not global (FIX — prerequisite for Task 5)

**Why this comes before Task 5.** Task 5 rewrites the consumer of this counter. Strikes are keyed on
`target_post_id` alone (`src/agent/simulation.py:376`), so they are **shared across agents and one
agent's success clears another's**. Under any Task 5 option that acts on the second strike, agent A's
failures would act on agent B's thread. Fix the key first.

**Files:** Modify `src/agent/simulation.py` (the strike map at `:376` and every read/write of it);
Test `tests/unit/test_simulation_logic.py`

- [ ] **Step 1: Enumerate every read and write of the strike map** (`grep -n` the attribute name) and
  list them in your report, so the reviewer can confirm you re-keyed all of them.
- [ ] **Step 2: Write the failing test** — agent A fails twice on post P; agent B's strike count for
  P is still 0, and B's successful post does not reset A's.
- [ ] **Step 3: Run it and watch it fail.**
- [ ] **Step 4: Re-key on `(agent_id, target_post_id)`.**
- [ ] **Step 5: Run the engine unit set** (the command in Task 3 Step 6).
- [ ] **Step 6: Commit.**
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  src/agent/simulation.py tests/unit/test_simulation_logic.py
```
  Subject: `fix(agent): post-failure strikes are keyed per agent, not shared across the roster (#20 COR-1b)`

---

### Task 5: The two-strike post back-off wedges a thread slot (DECIDE, then FIX)

**Issue clause (#20 COR-1b, verbatim `Fix:`):** "signal the Slack failure distinctly from the mock
path; purge the log on evict; move the private outcome check under the `posted` guard." A two-strike
back-off is **not** in that clause — the branch invented it (`7647438`).

**Verified evidence (`verify-engine.md` §2).** The mechanism is live; the harm is narrower than filed.
`f29e295` made the counterpart's refused row trip `has_new`, so archived-channel and single-agent
`invalid_auth` threads now self-close at 12 messages. The wedge survives **only** where the
counterpart stops posting (off-roster, at cap, or silent): the thread sits `status="active"` forever,
`_non_funding_thread_count` (`:2335-2340`) counts it, three such threads trip `blocked_for_regular`
(`:2346`; `active_thread_threshold = 3`, `src/config.py:331`) and the agent goes funding-only for the
run, while `_agent_load` (`:515-518`) inflates its rate allowance. And the back-off does not deliver
its own benefit: `:1396` is `if has_new or thread.has_pending_reply`, so `has_new` alone re-enqueues
and `post_failure_count` resets only on a successful post (`:1662`).

**The decision. v1 recommended (b); (b) is now rejected — prefer (c).**

- (a) **Remove the back-off.** Returns to the issue's literal ask; restores the per-turn LLM burn that
  `7647438` was written to stop.
- (b) **Close the thread** via `_close_thread(..., "timeout")`. **Rejected on evidence:** `status` is
  `"closed"`, never `"timeout"`, and `outcome` is a **PostgreSQL enum**, so a truthful label needs a
  migration this plan does not otherwise carry. Worse, a close writes an outcome into a PI DM, into
  both agents' prompt-fed working memory, and into `/admin/discussions` — i.e. it tells the PI and the
  models that a discussion concluded when it did not.
- (c) **Keep parking, and exclude parked threads** from `_non_funding_thread_count` and `_agent_load`.
  Two call sites to keep in sync and the thread still never resolves — but nothing false is written
  anywhere, the `blocked_for_regular` cascade cannot happen, and the LLM burn still stops.
  **Recommended.**

- [ ] **Step 1: Record the ruling** in your decision file under `## Ruling`, naming the option and why, before touching code. If you choose (b) anyway, you must
  also carry the enum migration and say what the PI DM will claim.
- [ ] **Step 2: Write the failing test** — with a silent counterpart, two refused posts leave the
  thread parked *and* invisible to both accounting functions:

```python
# tests/unit/test_simulation_logic.py
async def test_a_parked_thread_does_not_consume_a_regular_slot():
    """#20 AH3: parking (has_pending_reply=False) with a silent counterpart leaves
    status='active' forever; three of them trip blocked_for_regular
    (config.active_thread_threshold=3) and the agent goes funding-only for the run."""
    engine = _engine_with_failing_client("is_archived")
    for i in range(3):
        _active_thread(engine, thread_ts=f"10{i}.1", other_agent_id="nobody")
    for _ in range(2):
        await engine._run_turn("su")

    assert engine._non_funding_thread_count("su") == 0, "parked threads still hold slots"
    assert not engine.agents["su"].state.blocked_for_regular
```
  Read `_engine_with_failing_client` and `_active_thread` in the file before using them — if they do
  not exist under those names, write them and say so; **v1's snippet named five symbols that do not
  exist**, which is how a plan's test ends up unrunnable.
- [ ] **Step 3: Run it and watch it fail.**
- [ ] **Step 4: Implement the chosen option.**
- [ ] **Step 5: Fix the now-false log line.** `simulation.py:1646-1648` still says "not counted,
  nothing persisted"; since `f29e295` the row **is** persisted. Log text, so D33 permits it.
- [ ] **Step 6: Run the engine unit set + `tests/integration/test_state_rebuild.py`.**
- [ ] **Step 7: Commit.**
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  src/agent/simulation.py tests/unit/test_simulation_logic.py \
  docs/plans/2026-09-04-decisions/task-5.md
```
  Subject: `fix(agent): a parked thread stops consuming a regular discussion slot (#20 COR-1b)`

---

### Task 6: Two engine bounds — the activation backlog and the LLM-log re-queue (FIX, two commits)

Both are small, both are in `simulation.py`, and each ships its own red-first test and its own commit.

**6a — a first-time activated agent keeps its backlog (#20 E6(2)).** The issue asks for a per-agent
rebuild so a roster addition restores state; it does not ask for the new agent's cursor to skip
everything already in the channel. `_rebuild_one_agent_state` fast-forwards the activation cursor at
`simulation.py:5342` for every id in `to_add`, so a **first-time** activation suppresses the backlog
`18ba52c` gave it (14 days, per the lookback).

- [ ] **Step 1: Write the failing test** in `tests/integration/test_state_rebuild.py`: seed a channel
  with messages older than the agent's activation, activate the agent for the first time (no prior
  `SimulationRun` state for it), assert the backlog is offered to its first turn.
- [ ] **Step 2: Run it and watch it fail.** Then **check it fails for the right reason** — there is a
  bare `except` at `simulation.py:5348` that can turn a broken test into a passing one; assert on the
  cursor value, not on the absence of an exception. Note that `last_seen_cursor` is persisted nowhere,
  so "prior state exists" must be derived from something that is.
- [ ] **Step 3: Distinguish first activation from re-activation.** A re-added agent legitimately
  resumes from its own high-water mark; a first-time activation has none. Key the fast-forward on
  whether prior state exists for that agent, not on membership of `to_add`.
- [ ] **Step 4: Run the test and the engine unit set. Commit.**
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  src/agent/simulation.py tests/integration/test_state_rebuild.py
```
  Subject: `fix(agent): a first-time activation keeps its channel backlog (#20 E6(2))`

**6b — bound the LLM-log re-queue (#20 COR-11, verbatim `Fix:` "mirror PR #19's H1 re-queue").**
`simulation.py:5421` does `self._llm_log_buffer[0:0] = batch` with no ceiling, holding full prompts in
memory against the agent container's `mem_limit: 768m`. `18ba52c` dropped the batch at ≤10 rows, so
the branch traded a bounded loss for unbounded retention.

- [ ] **Step 5: Write the failing test** — after N failed flushes the buffer stops growing and the
  oldest rows are dropped with exactly one WARNING naming the dropped count.
- [ ] **Step 6: Run it and watch it fail.**
- [ ] **Step 7: Add a ceiling** (`LLM_LOG_REQUEUE_MAX_ROWS`, a constant next to the buffer),
  drop-oldest on overflow, one log line per drop.
- [ ] **Step 8: Run the engine unit set. Commit.**
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  src/agent/simulation.py tests/unit/test_simulation_logic.py
```
  Subject: `fix(agent): cap the LLM-log re-queue so a failing flush cannot grow without bound (#20 COR-11)`

---

### Task 7: The inbound log append moved behind the handler — losing PI text (DECIDE, **ESCALATE**)

**Issue clause (#20 COR-10(3), verbatim `Fix:`):** "wrap both like the sibling pollers; apply side
effects before (or transactionally with) the cursor advance."

**Verified evidence.** `c4de842` (written in this session) moved the **log append** behind the handler
as well as the cursor advance. The append is what records the PI's text durably, so a handler failure
now leaves the row un-appended and un-advanced: it is re-scanned only while it stays inside
`PI_INBOX_LOOKBACK_S = 300.0`, and after that window the PI's message is **lost outright**.
`verify-engine.md` rates this **worse than D25**, which lost only the triggers while keeping the row.

**Why this escalates.** The issue's own dedup mechanism is the log entry's presence, so "append before
the handler" re-creates exactly the defect COR-10(3) filed. You cannot have both with one field, and
the review found a second obstacle v1 missed: **`MessageLog.append` is not idempotent**, so appending
first means the existing log-presence check skips the retry entirely — option (a) needs a *distinct*
handled-marker, not just a re-ordering. That makes (a) a schema change, which is a branch-owner call.

- (a) **Append before the handler; advance the cursor after it; add a distinct durable handled-marker**
  so dedup no longer keys on log presence. Fully satisfies both halves of the clause. Costs a durable
  field — fold it into Task 8's 0029 if that revision happens at all, otherwise it is its own revision.
- (b) **Append before the handler and accept the original COR-10(3) loss of triggers** (revert to D25).
  No schema change. Leaves the issue's item open, so #20 must then carry a stated carve-out.
- (c) Keep HEAD's order. **Rejected:** it silently loses PI text after 300 s, worse than either the
  pre-branch behaviour or the issue's complaint.

**Recommendation: (b) if the branch owner wants no further schema change on this branch; (a) if a
0029 is happening anyway for Task 8.** The two decisions are coupled — take them together.

- [ ] **Step 1: Write the escalation** into `docs/plans/2026-09-04-decisions/task-7.md`, flagged
  `ESCALATED`, stating the coupling with Task 8 and that (a) supersedes
  D25 and this session's `c4de842` ordering. **Do not implement until it is answered.**
- [ ] **Step 2: Write the failing test** for whichever option is chosen — a handler that raises once
  then succeeds must leave the PI's row present in the log **immediately**, be retried, and be
  processed **exactly once** overall; and a handler that raises for longer than `PI_INBOX_LOOKBACK_S`
  must still leave the row in the log.
- [ ] **Step 3: Run it and watch it fail.**
- [ ] **Step 4: Implement.** If (a), land the marker in Task 8's revision **before** this step.
- [ ] **Step 5: Run the engine unit set + the PI-inbox tests.**
- [ ] **Step 6: Commit.**
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  src/agent/simulation.py tests/unit/test_simulation_logic.py \
  docs/plans/2026-09-04-decisions/task-7.md
```
  Subject: `fix(agent): record the PI's inbound text before the handler runs (#20 COR-10(3))`

---

### Task 8: Decide COR-5's carrier, then land at most one migration (DECIDE, **ESCALATE**)

**Issue clause (#20 COR-5, verbatim `Fix:`):** "require the sender be the owning PI; insert a
`ProposalReview`." The first half shipped. The second half never happens when
`AgentRegistry.user_id IS NULL`: `simulation.py:3453-3469` logs a warning and returns before the
insert, and `proposal_reviews.user_id` is `NOT NULL` (verified in the DB **and** at
`src/models/agent_registry.py:81-85`).

**Why this escalates — v1 chose a carrier the audit had already rejected.** v1 planned to make
`proposal_reviews.user_id` nullable. `audit-over-implementation.md`'s *Candidate A point 2* argues
`proposal_reviews` is **the wrong carrier**: `user_id` is `ForeignKey("users.id",
ondelete="CASCADE")` (confirmed at `src/models/agent_registry.py:82-84`), so deleting a PI destroys
the engine's block-clearing markers for every proposal that PI engaged with, and those proposals
re-block after the next restart. Its proposed minimal implementation is a nullable `pi_engaged_at` on
**`thread_decisions`** — no CASCADE exposure, and one column on a table this branch already migrates.
v1 adopted the rejected carrier without engaging the argument.

**Also correct these v1 errors before writing anything:** `src/models/thread_decision.py` **does not
exist** — `ThreadDecision` is `src/models/agent_activity.py:213` and `ProposalReview` is
`src/models/agent_registry.py:70`. v1 swapped them and invented a filename.

- (a) **`thread_decisions.pi_engaged_at` (nullable timestamptz).** The audit's own recommendation; no
  CASCADE loss; the engine's block-clearing check reads it instead of a review row. **Recommended.**
- (b) **Make `proposal_reviews.user_id` nullable.** Matches the issue's literal words ("insert a
  `ProposalReview`") and keeps one mechanism for both engine and web reviews — but inherits the
  CASCADE loss the audit filed, and it breaks `tests/integration/test_proposal_review.py:621-666`,
  which manufactures its `IntegrityError` from that very NOT NULL constraint.
- (c) **Do neither; carve COR-5's second half out of `Closes #20`.** Defensible only if the branch
  owner wants zero further schema change: population today is **0 of 53** active agents with
  `AgentRegistry.user_id IS NULL` (re-measured on the copy).

**Do not lean on the "0 of 53" reassurance.** `audit-phase8-functional.md` **I3** shows one
self-service button press makes it non-zero (`POST /profile/delete-account`,
`src/routers/profile.py:259`, leaves `status=active, user_id IS NULL`), and `0026` **on this branch**
removed the `CheckViolation` that had been an accidental guardrail. See Task 25.

- [ ] **Step 1: Write the escalation** in the decisions file, presenting (a)/(b)/(c), the CASCADE
  argument, the Task 7 coupling, and the fact that Task 9 and Task 10 both depend on the answer.
- [ ] **Step 2: If the ruling needs no schema (c), skip to Task 10.** Otherwise continue.
- [ ] **Step 3: Read the two revisions you are copying the shape from.**
  `alembic/versions/0026_pcm_user_cascade.py` (resolves its target from `pg_constraint` rather than
  hard-coding a name) and `0028_thread_reopen_state.py` (a nullable `ADD COLUMN` on
  `thread_decisions`). Match their `revision`/`down_revision` idiom, `if_exists=`-guarded downgrades,
  and docstring style.
- [ ] **Step 4: Write the failing migration test** `tests/integration/test_migration_0029.py`: against
  a scratch database stamped at 0028 (create it explicitly — `createdb -U copi copi_m29` against the
  gate's own Postgres, never `copi`), assert the pre-state, `alembic upgrade head`, assert the
  post-state, then `downgrade 0028` and assert the pre-state is restored exactly.
- [ ] **Step 5: Run it and watch it fail** (`0029` does not exist).
- [ ] **Step 6: Write 0029** carrying only what the ruling requires, plus Task 7's handled-marker if
  Task 7 chose (a). Nullable, no server default, no backfill — absent means "unknown", which the
  readers must treat as today's behaviour.
- [ ] **Step 7: Update the model**, or `postflight`'s ORM-drift check fails the chain. The file is
  `src/models/agent_activity.py` for `ThreadDecision`, `src/models/agent_registry.py` for
  `ProposalReview`.
- [ ] **Step 8: Teach the tooling — six edits, not two.** v1 named `PLANNED_OBJECTS` and postflight
  only. Also required: `scripts/migrate/preflight.py:74` `DEFAULT_TARGET = "0028"` → `"0029"`;
  `preflight.py:241-242` `REVISION_ORDER` must gain `"0029"` (until it does, `check_name_collisions`
  reports green while checking nothing, and preflight **BLOCKs** a 0028→0029 upgrade); and
  `tests/unit/test_migration_checks.py` hard-asserts `"0028"` at `:232`, `:907` and `:1022-1037`.
  `postflight.DEFAULT_TARGET` is derived (`postflight.py:77`) and needs no separate edit.
- [ ] **Step 9: Run the migration suite.**
  `.venv-test/bin/python -m pytest tests/unit/test_migration_checks.py tests/integration/test_migration_0029.py tests/integration/test_migration_tooling_chain.py -q -p no:cacheprovider`
  then `.venv-test/bin/python -m alembic heads` → exactly one head, `0029`.
- [ ] **Step 10: Re-run the chain against the production copy — and do not inherit its published
  claims.** The copy is `copi-prodtest-db` on `127.0.0.1:55434` (user/pw `copi`/`copi`), described in
  `phase8-prodcopy-test-record.md`. **v1 told you to restore `pristine_0024.dump`; no such path exists
  in the repo or in that record** — the record names a dump on the production host, and `backups/` is
  forbidden. So: create a fresh database on 55434, `alembic upgrade` it from the existing
  `copi_verify` schema state, and say in your report exactly what you started from. Run
  `preflight.py --snapshot … --backup-verified-elsewhere …`, `alembic upgrade head`, then
  `postflight.py --snapshot …`; record wall time and exit codes.
  **`postflight` exiting 0 is not sufficient evidence** — `audit-phase8-migration.md` C1 records that
  it cannot detect a concurrent deleter inside a duplicate group, and M9 that with no `--snapshot` it
  WARNs and exits 0. Always pass `--snapshot`, and state in your report what postflight did *not*
  check.
- [ ] **Step 11: Record 0029's lock row for the operator table — in your own decision file, not the
  runbook.** `docs/plans/2026-09-02-close-issues-20-27.md` is edited only by Task 33 (it is otherwise a
  file two parallel groups would share). Write the row you measured into
  `docs/plans/2026-09-04-decisions/task-8.md` and note that **`audit-phase8-migration.md` N4 records
  the published "worst-case lock window 0.3 s" as not a measurement of this chain** — so state what
  yours is a measurement of. Task 33 Step 11 applies it to R.6.
- [ ] **Step 12: Commit.**
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  alembic/versions/0029_*.py src/models/agent_activity.py src/models/agent_registry.py \
  scripts/migrate/preflight.py scripts/migrate/postflight.py \
  tests/unit/test_migration_checks.py tests/integration/test_migration_0029.py \
  docs/plans/2026-09-04-decisions/task-8.md
```
  Subject: `feat(migration): 0029 — <the carrier the ruling chose> (#20 COR-5)`

---

### Task 9: Persist the implicit review for an agent with no linked user (FIX)

**Depends on:** Task 8's ruling (and its revision, if any).

- [ ] **Step 1: Write the failing test** — an agent whose `AgentRegistry.user_id` is `None` gets its
  engagement recorded through the ruling's carrier, and the rebuild does **not** re-block the proposal
  afterwards. That second assertion is the half COR-5 actually names ("`_rebuild_agent_state`
  re-blocks on restart"), so a test that only checks the write is not enough.
- [ ] **Step 2: Run it and watch it fail.**
- [ ] **Step 3: Write the record instead of returning** at `simulation.py:3453-3469`. Keep the
  warning, downgraded to INFO, naming the agent.
- [ ] **Step 4: Check every reader.** `grep -rn 'ProposalReview' src/ | grep -v test` and confirm no
  reader assumes `user_id` is non-NULL — the `-1` marker's reader filters are the ones to check. If
  the ruling was (b), `tests/integration/test_proposal_review.py:621-666` manufactures an
  `IntegrityError` from the NOT NULL you removed and **will** need rewriting; say so.
- [ ] **Step 5: Run** `.venv-test/bin/python -m pytest tests/unit/test_simulation_logic.py tests/integration/test_admin_agents_counts.py tests/integration/test_proposal_review.py -q -p no:cacheprovider`
- [ ] **Step 6: Commit.**
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  src/agent/simulation.py tests/unit/test_simulation_logic.py
```
  Subject: `fix(agent): the implicit review persists for an agent with no linked user (#20 COR-5)`

---

### Task 10: A reopened thread's reply budget survives a restart — computed, not stored (FIX)

**Issue clause (#20 COR-13, verbatim `Fix:`):** "add `thread_decision_id` to `ProposalRef`; unify the
review key; persist reopen/dedup state." The first two shipped; `ac218fb` seeded the reopen dedup set
so the **per-tick** regrant stopped. The **rebuild** path did not change.

**Verified evidence.** `simulation.py:4985-4988` and `:5322-5327` both recompute `offset = msg_count`
on every restart, so the issue's own symptom — "plus a fresh reply budget each time" — survives a
restart even though `reopened_at` is durable.

**No migration.** v1 planned a `thread_decisions.reply_budget_consumed` column. It is unnecessary:
`reopened_at` is already durable, and the consumed count is derivable from the history the rebuild
loop already loads —
`offset = sum(1 for h in history if h.posted_at < reopened_at.timestamp())`. That removes a schema
change and a hot-path write. Confirm the exact attribute names by reading the loop before you write it.

**Files:** Modify `src/agent/simulation.py` (both loops); Test `tests/integration/test_state_rebuild.py`

- [ ] **Step 1: Write the failing test** — a reopened thread with N messages posted before
  `reopened_at` offers `max_thread_messages - N` replies after a rebuild, and the number does **not**
  reset across two consecutive rebuilds.
- [ ] **Step 2: Run it and watch it fail.**
- [ ] **Step 3: Compute the offset from `reopened_at`** in both loops (`:4985-4988`, `:5322-5327`).
  A thread with no `reopened_at` keeps exactly today's behaviour.
- [ ] **Step 4: Run** `tests/integration/test_state_rebuild.py` and the engine unit set.
- [ ] **Step 5: Commit.**
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  src/agent/simulation.py tests/integration/test_state_rebuild.py
```
  Subject: `fix(agent): a reopened thread's reply budget survives a restart (#20 COR-13)`

---

# Group B — web reopen, e-mail and the admin roll-up (`#21`, `#24`, `#20` blocker 5). Sequential.

### Task 11: The Slack-off migration path never commits (FIX — **must precede Task 12**)

**Why this is first.** v1 ordered the guard (Task 12) before this commit. The review measured the
consequence and it is a regression: `agent_page.py:959-993`'s `except IntegrityError` arm re-binds
`refined_in_channel` in a *fresh* transaction precisely because `rollback()` undid the flush-only
write. With Task 12's guard in place but this commit missing, a retry sees a truthy
`refined_in_channel` pointing at rows that no longer exist, skips the migration, and yields **zero**
channels — where HEAD at least mints a working one. Land durability first, then idempotence.

**Verified evidence.** `_migrate_offline` (`src/services/private_channels.py:324-404`) only flushes.
Measured by wrapping it with a commit and watching `test_reopen_write_race_…`'s
`channel_count_before_retry == 0` assertion fail — i.e. the commit is what makes the assertion
meaningful.

**Also in this task (#24 V5 iii).** In the Slack-off legacy path the `rollback()` discards the PI's
inbox guidance row (`src/services/pi_inbox.py:163,182`), which the `except` arm never re-creates. The
PI's guidance is lost silently. Fix it in the same commit — it is the same durability boundary.

**Files:** Modify `src/services/private_channels.py`, `src/services/pi_inbox.py`;
Test `tests/unit/test_private_channel_migration.py`

- [ ] **Step 1: Write two failing tests** — (i) after `_migrate_offline`, a rollback on the caller's
  session leaves the `AgentChannel` row present; (ii) after the offline path's `IntegrityError`
  recovery, the PI's inbox guidance row still exists.
- [ ] **Step 2: Run them and watch both fail.**
- [ ] **Step 3: Add `await db.commit()`** at the end of `_migrate_offline`, with the same comment
  shape as the online path's commit (which explains *why* the durability boundary is there), and
  persist the guidance row on the same side of that boundary.
- [ ] **Step 4: Run** `.venv-test/bin/python -m pytest tests/unit/test_private_channel_migration.py tests/integration/test_proposal_review.py -q -p no:cacheprovider`
- [ ] **Step 5: Commit.**
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  src/services/private_channels.py src/services/pi_inbox.py \
  tests/unit/test_private_channel_migration.py
```
  Subject: `fix(private-channels): the Slack-off migration commits its own rows and keeps the PI's guidance (#24 N1)`

---

### Task 12: The web reopen route mints a second private Slack channel (FIX — merge blocker)

**Severity: the most serious open item in this plan, and a regression introduced by `34d3c15`
(written in this session).**

**Verified evidence (`verify-web-data-docs.md` Q1, driven on a real Postgres).**
`src/routers/agent_page.py:772` gates the migration only on `enable_private_refinement and
origin_visibility == "public"`; **no line in the file reads `td.refined_in_channel` as a guard**
(re-confirmed by grep). A retry after the review row is lost returns **302** and produces **2
`AgentChannel` rows and 2 Slack channel-create calls**, with `refined_in_channel` repointed to the
new channel and the first — which already has the handover posted in it — orphaned.
`src/agent/slack_client.py:933-936`'s per-call timestamp suffix means Slack does not refuse the second
create. `d1146a4` added exactly this guard to the **e-mail** twin (`email_inbound.py:867-895`) and
stopped there.

**Files:** Modify `src/routers/agent_page.py`; Test `tests/integration/test_proposal_review.py`

- [ ] **Step 1: Write the failing test** — mirror the e-mail twin's pin
  (`tests/integration/test_email_inbound_reply_paths.py`'s retry test) on the web route: drive
  `reopen_proposal`, lose the review row, drive it again, assert **exactly one** `AgentChannel` row
  and **one** Slack channel-create call. **Use the offline fixture as well as the online one** — v1's
  test used only the online fixture, which is why it would have passed without Task 11.
- [ ] **Step 2: Run it and watch it fail** — expect 2 rows / 2 creates.
- [ ] **Step 3: Add the guard**, matching the e-mail path's shape so the two read the same. Note the
  `else` — v1's snippet omitted it and would have raised `UnboundLocalError` on every first reopen:

```python
# src/routers/agent_page.py, before the migration call
if td.refined_in_channel:
    # Already migrated on an earlier attempt (#21 COR-19.6 / #24 N1-b). The migration
    # commits its own AgentChannel/member/handover rows as soon as its Slack side
    # effects are irreversible (private_channels.py), so this is the durable record
    # that it happened. Re-running it asks Slack for a second channel -- the name
    # carries a per-call timestamp suffix, so Slack does not refuse it -- and orphans
    # the first, which already has the handover in it.
    logger.info(
        "Proposal %s was already migrated to %s -- not migrating again",
        td.thread_id, td.refined_in_channel,
    )
    already_migrated = True
else:
    already_migrated = False
```
  then branch the migration call on `already_migrated`, falling through so the route still records the
  review row and retires the notification (again, matching the e-mail path).
- [ ] **Step 4: Correct the two stale comments** that describe the pre-`34d3c15` behaviour:
  `src/routers/agent_page.py:885-891` and `tests/integration/test_proposal_review.py:1327-1350`.
- [ ] **Step 5: Run** `.venv-test/bin/python -m pytest tests/integration/test_proposal_review.py tests/integration/test_agent_page.py tests/unit/test_concurrent_write_guards.py -q -p no:cacheprovider`
- [ ] **Step 6: DECIDE the residual window (#21 CL21-2)** and record the ruling in the decisions file.
  After Tasks 11 and 12 a second channel still requires the migration's own commit to fail *after*
  `create_private_channel` succeeded. v1 called this correct-as-built; that is **rejected** —
  closure-21 filed it as a blocker ("a live hazard once inbound e-mail is on"), the user-visible
  consequence is a PI's refinement conversation split across two Slack channels with both bots in a
  dead one, and v1's justification cited deploy note 21 as an existing control while Task 35 exists
  because that note is *missing* from the accumulator. Options:
  - (a) **Make the channel name deterministic** (drop the per-call timestamp suffix at
    `src/agent/slack_client.py:933-936`) so Slack's own `name_taken` becomes the idempotency guard on
    retry. Materially cheaper than the "adopt an existing channel by name" design work v1 rejected.
    **Recommended — evaluate this first.** Check what else depends on the suffix before you commit to it.
  - (b) Leave the window and state it in #21's closing comment, with the orphan-sweep procedure.
- [ ] **Step 7: Commit.**
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  src/routers/agent_page.py tests/integration/test_proposal_review.py \
  docs/plans/2026-09-04-decisions/task-12.md
```
  Subject: `fix(web): the reopen route reads refined_in_channel, so a retry cannot mint a second private channel (#24 N1, #21 COR-19.6)`

---

### Task 13: Three residues in the reopen/review handlers (FIX)

All three are in the code Task 12 just edited, and all three were filed and then not carried into v1.

1. **Dead re-bind machinery (verify-deploy finding 1a).** `agent_page.py:891` captures a channel id,
   `:956-963` re-binds it, and `:999-1000` logs "recovered by re-binding" — machinery the now-false
   comment justified. With Task 12's guard in place, establish by test whether the re-bind is still
   reachable. **Correcting the comment while leaving dead code makes the code unexplained rather than
   wrongly explained** — remove it or prove it live, and say which in your report.
2. **`scalar_one()` → 500 (#24 V5 i).** `agent_page.py:960-962` uses `scalar_one()`, which raises
   `NoResultFound` → 500 on exactly the case `review_proposal`'s own comment (`:626-632`) says to use
   `scalar_one_or_none()` for. This is the identical defect the traceability table records as FIXED
   (N2) in the sibling handler, surviving here.
3. **`reopen.guidance`'s coded 400 is unreachable (phase8 M2).** `agent_page.py:686` returns a coded
   400 for empty guidance, but the field is a required `str = Form(...)`, so an empty box 422s before
   the handler runs. Fix this one site with `Form("")`, matching `:1432` which `7cc2ea3` already did.
   The other eight sites are **→ F4**; do not widen this task to them.

**Files:** Modify `src/routers/agent_page.py`; Test `tests/integration/test_proposal_review.py`,
`tests/integration/test_agent_page.py`

- [ ] **Step 1: Write three failing tests**, one per residue: a lost-race reopen with no winning row
  returns a coded response rather than a 500; an empty guidance box returns the coded 400, not a 422;
  and (for residue 1) a test that either exercises the re-bind or proves it unreachable.
- [ ] **Step 2: Run them and watch them fail.**
- [ ] **Step 3: Fix all three.**
- [ ] **Step 4: Run** the two integration files plus `tests/unit/test_concurrent_write_guards.py`.
- [ ] **Step 5: Commit.**
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  src/routers/agent_page.py tests/integration/test_proposal_review.py \
  tests/integration/test_agent_page.py
```
  Subject: `fix(web): the reopen handler stops 500ing on a lost race and its empty-guidance 400 is reachable (#24 V5)`

---

### Task 14: A lost review race silently discards the PI's rating (DECIDE, then FIX)

**Verified evidence (`verify-web-data-docs.md` Q4).** `src/routers/agent_page.py:655`: the
`winner is None` arm logs at ERROR, commits `record_engagement` + `mark_notification_responded`, and
returns a `302` to the dashboard **byte-identical to the success path**. The PI sees the normal
post-review redirect and their rating and comment are gone. The proposal does re-appear on the
dashboard, so a diligent PI could notice — **but `mark_notification_responded` has already flipped the
outstanding `EmailNotification` to `responded`, so no reminder chases it.**

**Why it is a decision.** `a5666b4`'s test
`test_review_proposal_recovery_with_no_winning_row_returns_a_clean_response` **pins the 302**, so
closing this means rewriting a test written a few commits ago.

- (a) **Tell the PI.** Redirect with an error indicator the dashboard renders, and do **not** retire
  the notification when nothing was persisted. **Recommended** — the only option where the PI learns
  their input was lost.
- (b) Keep the 302 and stop retiring the notification, so the reminder e-mail chases it.
- (c) Leave as built and document it. Defensible only if (a) is judged out of scope for #24, whose
  `Fix:` is about not 500ing.

- [ ] **Step 1: Record the ruling** in the decisions file.
- [ ] **Step 2: Extend `a5666b4`'s test rather than replacing it.** It pins a real behaviour (no 500,
  no exception escape) that must survive; add the new assertions and state in its docstring why the
  redirect target changed.
- [ ] **Step 3: Run it and watch the new assertions fail.**
- [ ] **Step 4: Implement.** If (a): **name the mechanism before you code it** — read the route's other
  error paths, pick the one that already exists, and write down which you copied. The review found the
  route's error paths are *not* uniform, so "the way the other paths do it" is not a specification.
  Move `mark_notification_responded` so it only runs when a row was persisted.
- [ ] **Step 5: Run** `tests/integration/test_proposal_review.py tests/unit/test_concurrent_write_guards.py`
- [ ] **Step 6: Commit.**
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  src/routers/agent_page.py tests/integration/test_proposal_review.py \
  docs/plans/2026-09-04-decisions/task-14.md
```
  Subject: `fix(web): a lost review race tells the PI instead of showing a success page (#24 V5)`

---

### Task 15: The e-mail terminal handler consumes transient failures (FIX)

**Issue clause (#21 COR-32).** The `Fix:` asks that a failed instruction post be retried rather than
silently consumed. Ruling 21.9(a) made failures *inside* the private-channel migration terminal —
deliberately — but the handler now also catches four **pre-mutation** failures, where nothing has
happened on Slack yet and a retry is exactly right.

**Verified evidence.** `src/services/email_inbound.py:908-968`: four raise sites before any Slack
mutation are consumed by the terminal path, so a transient DNS/throttle/token blip discards a
legitimate PI instruction.

**Files:** Modify `src/services/email_inbound.py`, and `src/services/private_channels.py` if the
boundary moves; Test `tests/integration/test_email_inbound_reply_paths.py`,
`tests/unit/test_email_inbound_hardening.py`

- [ ] **Step 1: Enumerate the four pre-mutation raise sites** by line in your report, so the reviewer
  can check you moved exactly those.
- [ ] **Step 2: Write the failing tests** — each pre-mutation failure is **retried** (the S3 object is
  kept, no PI "will not be retried" e-mail); a failure *inside* the migration is still terminal (the
  existing pin must stay green).
- [ ] **Step 3: Run them and watch them fail.**
- [ ] **Step 4: Split the handler** so the terminal path covers only post-mutation failures.
- [ ] **Step 5: Run the e-mail suites.**
- [ ] **Step 6: Commit.**
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  src/services/email_inbound.py src/services/private_channels.py \
  tests/integration/test_email_inbound_reply_paths.py tests/unit/test_email_inbound_hardening.py
```
  Subject: `fix(inbound): a pre-Slack transient failure is retried, not consumed as terminal (#21 COR-32)`

---

### Task 16: The review count and the `Rating: 0/4` a PI actually sees (FIX)

**Why this is here and not marked FIXED.** v1's traceability recorded closure-20 blocker 5 as FIXED by
`f9541ba`. `audit-phase8-functional.md` breakage 1 named **two** causes and `f9541ba` fixed one (the
scope join). The other survives, verified at HEAD: `src/routers/admin.py:852` (and `:656`) filter
`ProposalReview.rating != -1`, while phase8's prescribed predicate is `rating NOT IN (-1, 0)`. The
**233 `rating=0` reopen markers** are therefore counted as reviews — wiseman alone 89. So the count is
no longer *negative* but is still *wrong*, and verify-deploy finding 4 puts it plainly: "the clamp
claim is FALSE; **the scope claim stands**" — the scope claim being blocker 5's actual ask.

**And it reaches real users.** `templates/agent/dashboard.html:255` renders `Rating: {{ rev.rating }}/4`
and `templates/admin/discussions.html:162` renders `{{ rev.rating }}/4`, for anything past the `!= -1`
filter — and reopen rows are `rating=0`. Both verified at HEAD. Wiseman's own dashboard shows **89 ×
"Rating: 0/4"**: a PI's page tells them they scored 89 proposals zero out of four.

**Files:** Modify `src/routers/admin.py`, `templates/agent/dashboard.html`,
`templates/admin/discussions.html`; Test `tests/integration/test_admin_agents_counts.py`

- [ ] **Step 1: Re-measure on the production copy first** and record the numbers: count of
  `proposal_reviews` rows by `rating`, and the per-agent review count under `!= -1` versus
  `NOT IN (-1, 0)`. v1 quoted a 233 it did not measure; you must.
- [ ] **Step 2: Write the failing tests** — a seeded reopen marker (`rating=0`) is **not** counted as a
  review, and is **not** rendered as a score in either template.
- [ ] **Step 3: Run them and watch them fail.**
- [ ] **Step 4: Change the predicate** to `NOT IN (-1, 0)` at both `admin.py` sites, and make the
  templates render a reopen marker as what it is rather than as `0/4`. Read how the `-1` marker is
  already special-cased and follow that pattern; do not invent a second convention.
- [ ] **Step 5: Run** `.venv-test/bin/python -m pytest tests/integration/test_admin_agents_counts.py tests/integration/test_agent_page.py -q -p no:cacheprovider`
- [ ] **Step 6: Commit.**
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  src/routers/admin.py templates/agent/dashboard.html templates/admin/discussions.html \
  tests/integration/test_admin_agents_counts.py docs/plans/2026-09-04-decisions/task-16.md
```
  Subject: `fix(web): a reopen marker is not a review and is not rendered as 0/4 (#20 blocker 5)`

---

### Task 17: Two e-mail notification defects (FIX, two commits)

**17a — `"expired"` is committed on three no-send paths (#21 V4-3).** The `Fix:` is about expiring an
unanswered notification and letting the ladder advance — not about marking one expired when no
replacement was sent. `src/services/email_notifications.py:289` sets the status on three paths where
no re-send follows, and the PI's reply is then dropped at `email_inbound.py:317-319` because the token
no longer resolves.

- [ ] **Step 1: Write the failing test** — a sweep that expires a notification but sends nothing leaves
  the reply token answerable, or does not mark it expired.
- [ ] **Step 2: Run it and watch it fail.**
- [ ] **Step 3: Only write `"expired"` once a replacement has been accepted by SES**, mirroring the
  ordering 21.11 already established for row creation.
- [ ] **Step 4: Run the sweep suites. Commit.**
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  src/services/email_notifications.py tests/unit/test_email_notification_sweeps.py \
  tests/integration/test_email_notification_sweeps_expiry.py
```
  Subject: `fix(email): only mark a notification expired when a replacement was actually sent (#21 V4-3)`

**17b — the paused-notification e-mail bypasses the recipient allowlist (#21 V4-4).** The `Fix:` asks
for transactional safety in the sweeps; it does **not** ask for an auto-downgrade/auto-pause ladder —
the branch armed one — and the paused notification it sends skips the guard every other send uses.
Verified at HEAD: `is_allowed_recipient` gates the sends at `email_notifications.py:309`, `:363` and
`:716`; `_send_paused_email` (`:606`) reaches `boto3` at `:641-643` with no gate. Latent only while
the allowlist is on.

- [ ] **Step 5: Write the failing test** — a paused-notification send to a recipient outside the
  allowlist is suppressed and the suppression logged, exactly as the other paths behave.
- [ ] **Step 6: Run it and watch it fail.**
- [ ] **Step 7: Route the send through the same helper** the other paths use rather than raw `boto3`.
  Read one existing send site first and match it; do not invent a second gate.
- [ ] **Step 8: Audit for siblings.** `grep -n 'boto3' src/services/email_notifications.py` — there is
  a third raw site at `:510-512`; check it and report what you found either way.
- [ ] **Step 9: Run the sweep suites. Commit.**
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  src/services/email_notifications.py tests/unit/test_email_notification_sweeps.py
```
  Subject: `fix(email): the paused-notification send honours the recipient allowlist (#21 V4-4)`

---

# Group C — profile pipeline (`#22`). Parallel with B, D, E. Sequential within.

### Task 18: The seed-resurrection guard is keyed on the wrong flag (FIX)

**Verified evidence (`verify-profiles-clients.md`, over-impl R1).** The guard is keyed on
`user.onboarding_complete`, and **`5090322` does not close it** — its Step 9b sets the very flag the
new export condition tests. On the production copy **36 of 53** active agents have
`onboarding_complete = false` (re-measured; also 113 of 144 users, so the active-agent framing is the
right one), so this is the common case. One clear route never sets the flag, so a PI who cleared their
instructions through it gets a model-authored seed regenerated.

**The route is `/agent/{id}/profile/save`** (`audit-over-implementation.md:203`) — v1 told the
executor to "identify" it without naming it. Confirm it by reading the route, then say so in your report.

**Files:** Modify `src/services/profile_pipeline.py`; Test `tests/integration/test_private_profile_clear.py`,
`tests/characterization/test_profile_pipeline_gm.py`

- [ ] **Step 1: Write the failing test** — a PI who cleared via `/agent/{id}/profile/save` does not get
  a seed on the next pipeline run.
- [ ] **Step 2: Run it and watch it fail.**
- [ ] **Step 3: Key the guard on the cleared state itself** rather than the onboarding proxy — the DB
  already records "both private columns NULL", which is the fact the guard cares about. Keep the
  admin-seeded case (never onboarded, no private content, wants a seed) working.
- [ ] **Step 4: No prompt strings may move** — this file holds the synthesis prompts. AST-hash before
  and after. If a GM snapshot changes, **stop and explain in the report** rather than re-recording it.
- [ ] **Step 5: Run** `.venv-test/bin/python -m pytest tests/unit/test_apply_synthesis.py tests/unit/test_validate_profile.py tests/characterization/test_profile_pipeline_gm.py tests/integration/test_private_profile_clear.py tests/integration/test_onboarding_flow.py -q -p no:cacheprovider`
- [ ] **Step 6: Commit.**
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  src/services/profile_pipeline.py tests/integration/test_private_profile_clear.py \
  tests/characterization/test_profile_pipeline_gm.py
```
  Subject: `fix(profile): the seed guard keys on the cleared state, not the onboarding flag (#22 COR-23)`

---

### Task 19: An operator repair script can silently blank curated lists (FIX)

**Verified evidence, with the artefact's framing corrected.** A genuinely *malformed* LLM response is
now rejected and writes nothing — safer than pre-fix. The live shape is a response that **passes**
validation while omitting or mistyping `keywords` / `key_targets` / `experimental_models`: measured
`keywords=[] key_targets=[] experimental_models=[]` written over curated values. Second, unfiled
shape: a profile with `synthesis_validated = False` is unprotected and loses everything.

**Files:** Modify `scripts/vet_publications.py`, `scripts/resynth_from_current_pubs.py`, and
`src/services/profile_pipeline.py`'s `apply_synthesis` **if** the fallback belongs there — decide by
reading which layer already owns "absent vs empty" and say which you chose and why; Test
`tests/unit/test_apply_synthesis.py`

- [ ] **Step 1: Write the failing tests** — a response that validates but omits `keywords` leaves the
  stored `keywords` unchanged; a response that omits everything changes nothing; a
  `synthesis_validated = False` profile is not blanked.
- [ ] **Step 2: Run them and watch them fail.**
- [ ] **Step 3: Restore "keep the stored value" for an absent key**, distinguishing *absent* from
  *explicitly empty*. Do not reintroduce the per-character corruption `beae171` fixed — an explicit
  non-list value must still be rejected.
- [ ] **Step 4: Run the profile suites** and `python -c "import ast,sys; [ast.parse(open(p).read()) for p in sys.argv[1:]]" scripts/vet_publications.py scripts/resynth_from_current_pubs.py`
- [ ] **Step 5: Fix the annotation that costs the branch 2 mypy findings.** This file is Group C's, and
  `_insert_publication_tolerating_conflict` annotates `tuple[Publication, bool]` while its loser branch
  can return `None` — the two `[return-value]` errors that moved the ceiling from 145 to 147 (see
  Task 27, which must not touch this file). Widen the annotation to `tuple[Publication | None, bool]`
  and fix any caller the change exposes. Re-measure: `git archive HEAD src pyproject.toml` into the
  scratchpad, `mypy src --ignore-missing-imports`, `grep -c ': error:'` — expect **145**. Record the
  before/after in your report and in your decision file under `## Evidence`.
- [ ] **Step 6: Commit.**
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  scripts/vet_publications.py scripts/resynth_from_current_pubs.py \
  src/services/profile_pipeline.py tests/unit/test_apply_synthesis.py \
  docs/plans/2026-09-04-decisions/task-19.md
```
  Subject: `fix(scripts): a validating-but-incomplete synthesis no longer blanks curated list columns, and the publication-insert annotation is honest (#22 V6, #27 I1)`

---

### Task 20: The truncated publication text the agents are still reading (FIX — v1's STATE rejected)

**Why this is a FIX and not a STATE bullet.** v1 wrote: "*they self-heal on the next pipeline pass per
user. A backfill script would heal them sooner but touches every row of a 4,731-row table for a
cosmetic gain.*" Both facts are wrong and the cost is overstated ~10×:

- **Not cosmetic.** `audit-phase8-functional.md` breakage 3, measured on the full copy: **12 titles
  under 2 characters** (`"T"`, `"H"`, `"NAD"`, `"tRNA"`, `"From "`), 9 empty/whitespace, 111 matching a
  truncation signature, **299 publications with an empty abstract** and 127 more under 80 characters.
  The auditor's own words: "**these rows are the evidence base for profile synthesis and for the
  agents' prompts**." A one-character title and an empty abstract fed to synthesis is garbage-in on
  the exact pathway #29 — a memory-poisoning incident — is about.
- **The "self-heal" has no trigger.** `35cc010` refreshes on the *update branch*, which runs when the
  pipeline re-runs for that user, and phase8 records "no migration and no script does it. **The deploy
  runbook does not list a per-user re-run.**"
- **The cost is ~132 titles and ~426 abstracts**, not every row — and the table is **4,508** rows, not
  4,731 (4,731 is the pre-0025 count; `phase8-prodcopy-test-record.md` records "4,731 before, 4,508
  after — 223 rows deleted").

**Scope discipline.** #22's `Fix:` for the `itertext()` item is the parser fix, which shipped. This
task therefore does the **minimum that removes the garbage-in**: it adds the operator procedure, and a
targeted repair only if Step 2's re-measurement still shows the rows.

**Files:** Modify `docs/plans/2026-09-02-close-issues-20-27.md` (Part R — the per-user re-run step);
optionally Create `scripts/repair_publication_text.py`; Test `tests/unit/test_runbook_docs.py`

- [ ] **Step 1: Re-measure on the production copy** and record in your decision file: counts of titles
  under 2 chars, empty/whitespace titles, truncation-signature titles, empty abstracts, abstracts
  under 80 chars. Do not inherit phase8's numbers.
- [ ] **Step 2: Write the exact post-deploy procedure** into `docs/plans/2026-09-04-decisions/task-20.md`:
  the ordered per-user pipeline re-run command, the selection query verbatim, and the criterion for
  which users need it. **Do not edit `docs/plans/2026-09-02-close-issues-20-27.md`** — Part R and
  `tests/unit/test_runbook_docs.py` are edited only by Task 33, because two parallel groups would
  otherwise share them. Task 33 Step 11 applies your procedure and pins it with a doc test.
- [ ] **Step 3: Decide** whether a targeted repair script is warranted for the rows the re-run will not
  reach, and either write it (with `--dry-run` default) or record why the re-run suffices.
- [ ] **Step 4: Commit.**
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  docs/plans/2026-09-04-decisions/task-20.md scripts/repair_publication_text.py
```
  (drop the script path if Step 3 ruled against writing it)
  Subject: `docs(profile): the measured truncated-publication inventory and the per-user re-run that heals it (#22 item 15)`

---

### Task 21: The word-count gate disagrees with the retry prompt (DECIDE)

**Verified evidence.** The gate is `100-350`; the retry prompt at `src/services/profile_pipeline.py:374`
and `prompts/profile-synthesis*.md` say `150-250`. The issue **describes** the disagreement but its
`Fix:` clause for that group does not ask for it to be resolved, and **D33 forbids editing the prompt**.

- (a) **State it, change nothing.** The gate is deliberately wider than the prompt's guidance; the
  prompt is a request, the gate is a limit; D33 barred aligning the prompt in this PR.
  **Recommended** — the `Fix:` does not ask for it, and tightening to `150-250` would start rejecting
  profiles that pass today.
- (b) Tighten the gate to `150-250`. Behaviour change, not requested; needs a measurement of how many
  stored profiles would now fail.

- [ ] **Step 1: Record the ruling** in the decisions file.
- [ ] **Step 2: If (a)** — no code. Add the sentence to the material Task 33 and Task 36 consume (the
  decisions file is the single source; do **not** edit `pr-body-residuals-final.md`, which is
  untracked). **If (b)** — measure the affected population on the production copy first, then write the
  failing test and change the constant.
- [ ] **Step 3: Commit.**
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  docs/plans/2026-09-04-decisions/task-21.md
```
  Subject: `docs: rule on the profile word-range gate/prompt divergence (#22 COR-16)`

---

# Group D — external clients (`#23`). Parallel with B, C, E. Sequential within.

### Task 22: Widen the apostrophe class, canonicalise the FOA number, and fix the case-sensitive compare (FIX)

**Issue clause (#23 COR-28a + COR-27b).** COR-28a's `Fix:` is to stop the ASCII-only apostrophe class
from mis-classifying; the branch added U+2019 only. COR-27b's `Fix:` made the pattern
case-insensitive, which means `extract_foa_number` can now return a number whose case differs from
the canonical form — and that string becomes a cache **file name**.

**Beyond the clause:** #23's DoD requires the regex tests be "table-driven over the cases above", and
**the issue names only U+2019**. Widening to U+2018/U+00B4/U+FF07 is therefore beyond the clause. It is
included because the class is a classifier over PI-authored text where those code points occur
naturally, and because a partial class is the same defect the item filed. **Say so in the commit body**
and in your report; do not let it arrive silently.

**Verified evidence:** the class is `['’ʼ]` and still misses **U+2018, U+00B4, U+FF07**;
`foa_pattern.py:35` returns `m.group(1)` verbatim. Third, unfiled but adjacent:
`funding_rules.summarize_funding_thread` (`funding_rules.py:202-230`) extracts case-insensitively then
compares case-**sensitively**, so a spin-off post whose casing differs from its root is silently missed
and an agent can duplicate it.

**Files:** Modify `src/agent/funding_rules.py`, `src/agent/foa_pattern.py`; Test
`tests/unit/test_funding_rules.py`, `tests/unit/test_foa_pattern.py`

- [ ] **Step 1: Write the failing tests, table-driven** — one row per missing code point, one asserting
  `extract_foa_number` canonicalises its result, and one for the case-mismatched spin-off post.
- [ ] **Step 2: Run them and watch them fail.**
- [ ] **Step 3: Widen the class; canonicalise the return; make the comparison case-insensitive.**
- [ ] **Step 4: Check the cache-key consequence.** Every caller that uses the result as a path or cache
  key (`foa_cache.py`) — confirm canonicalising does not orphan existing entries; if it would, say so
  and handle it.
- [ ] **Step 5: Run** `.venv-test/bin/python -m pytest tests/unit/test_funding_rules.py tests/unit/test_foa_pattern.py tests/unit/test_grantbot_selection.py -q -p no:cacheprovider`
- [ ] **Step 6: Commit.**
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  src/agent/funding_rules.py src/agent/foa_pattern.py \
  tests/unit/test_funding_rules.py tests/unit/test_foa_pattern.py
```
  Subject: `fix(funding): the apostrophe class covers the remaining code points, FOA numbers canonicalise, and the spin-off compare is case-insensitive (#23 COR-28a, COR-27b, COR-27)`

---

### Task 23: Bound the retry budget, pace NCBI at its ceiling, and make the semaphore per-loop (FIX)

**Issue clause (#23 COR-29a/c).** The `Fix:` asks for retry/backoff and key-sized concurrency. It does
not ask for an unbounded per-item retry budget, nor for throttling below NCBI's own ceiling.

**Verified evidence:**
- **over-impl R3** — a blanket 4× retry with **no total deadline**, applied inside a per-item loop, so
  a long publication list multiplies the worst case without limit.
- **over-impl R4 / closure-23 R2** — the resized semaphore paces the keyed path to **8.33 req/s against
  a 10 req/s policy ceiling** (`_NCBI_PACING_SECONDS = {True: 0.12, …}`), and holds a slot across all
  four retries, so effective concurrency is lower again.
- **closure-23 R7** — `_NCBI_SEMAPHORES` is loop-bound and the `HAZARD` comment at
  `src/services/pubmed.py:88-92` is unresolved in `src`. **This is a production hazard, not a test-only
  one**, which v1 understated: `src/agent/grantbot.py:728` calls `asyncio.run(run_grantbot(...))`
  **inside `while True:`** — that is the long-running `scheduler` service, so one process builds a
  fresh event loop per calendar day, and `src/agent/tools.py:9` puts pubmed on that process's path.
  Contention additionally requires ≥ 8 concurrent keyed calls (the semaphore's size), which was **not**
  measured — measure it in Step 4 and record the answer either way.

**Files:** Modify `src/services/pubmed.py`, `src/services/http_retry.py`; Test
`tests/contract/test_pubmed_contract.py`, `tests/unit/test_http_retry.py`

- [ ] **Step 1: Write three failing tests** — (i) a per-call total deadline caps wall time regardless of
  attempt count; (ii) the keyed path's measured peak reaches the policy ceiling rather than sitting
  below it; (iii) the semaphore is not shared across event loops (drive two `asyncio.run()` calls, the
  shape `e6a03f7` used for the pacing cursor).
- [ ] **Step 2: Run them and watch them fail.**
- [ ] **Step 3: Add a total deadline** to `get_with_retry`/`post_with_retry`, release the semaphore
  between attempts (or acquire per attempt), and make the semaphore map per-loop.
- [ ] **Step 4: Re-measure the rate** the way `verify-profiles-clients.md` did, plus the peak concurrent
  keyed-call count a real grantbot day reaches, and record before/after in your report. The plan's claim
  is a measured bound, not an intention.
- [ ] **Step 5: Run** `.venv-test/bin/python -m pytest tests/contract/test_pubmed_contract.py tests/contract/test_orcid_contract.py tests/unit/test_http_retry.py tests/unit/test_retry_after.py -q -p no:cacheprovider`
- [ ] **Step 6: Commit.**
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  src/services/pubmed.py src/services/http_retry.py \
  tests/contract/test_pubmed_contract.py tests/unit/test_http_retry.py \
  docs/plans/2026-09-04-decisions/task-23.md
```
  Subject: `fix(http): bound the retry budget with a deadline, pace NCBI at its ceiling, and key the semaphore per loop (#23 COR-29)`

---

### Task 24: Close the remaining #23 test-and-comment gaps (FIX)

This closes #23's DoD clause ("each PR ships a test that fails against the pre-fix code"), which is
**unmet today** — see `## Definition of done`. One commit.

**Verified evidence, with v1's two mis-citations corrected:**
- **closure-23 R5** — the V10b log line has no assertion. Re-measured: `grep -rn "skipping delegate
  Slack-ID sync" tests/` = **0**. The line lives at **`src/routers/invite.py:246`**; the existing test
  files are `tests/unit/test_delegate_slack_ids_sql.py` and `tests/unit/test_delegates.py`. **v1 named
  `tests/unit/test_delegate_slack_ids.py`, which does not exist** — put the assertion in
  `test_delegates.py` or create a clearly-named new file, and say which.
- **R6** — no behavioural retry test for `orcid.py` or `grants.py`; only the wiring is pinned.
  `tests/contract/test_orcid_contract.py` and `tests/contract/test_grants_contract.py` both **already
  exist**; extend them (v1 said "create if absent").
- **R8** — `_execute_retrieve_abstract` / `_execute_retrieve_full_text` (`src/agent/tools.py:215`,
  `:251`) have no call-site test.
- **R9 / over-impl R7** — the `_bot_uid_map` docstring still asserts "grantbot falls back to SuBot's
  token", which `f1c28d1` made false. **The docstring is at `simulation.py:4678`** (`_bot_uid_map` is
  defined at `:4673`); v1 cited `:4643`, which is unrelated log text.

**Group-ownership note.** Group A owns `src/agent/simulation.py`. **This task must not edit it.**
Record the docstring correction in the decisions file as a one-line item for whoever holds Group A
(fold it into Task 10's commit), and confirm in your report that you did not touch the file.

**Files:** Modify/Test `tests/unit/test_delegates.py`, `tests/contract/test_orcid_contract.py`,
`tests/contract/test_grants_contract.py`, `tests/unit/test_tools_budget.py`

- [ ] **Step 1: Write the four missing assertions/tests**, each proven red against the pre-fix code —
  `git show <commit>~1:<path>` into the scratchpad and run against that copy. **Never mutate `src/`.**
- [ ] **Step 2: Run them; confirm each passes at HEAD and fails against its pre-fix export.**
- [ ] **Step 3: Run the #23 suites.**
- [ ] **Step 4: Commit.**
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  tests/unit/test_delegates.py tests/contract/test_orcid_contract.py \
  tests/contract/test_grants_contract.py tests/unit/test_tools_budget.py \
  docs/plans/2026-09-04-decisions/task-24.md
```
  Subject: `test(clients): pin the delegate-sync log, the ORCID/grants retry behaviour and the tool budget call sites (#23)`

---

# Group E — deploy, gate, image and the data-contract guardrail (`#27`, `#25`). Parallel with B, C, D.

### Task 25: A PI can delete their own account and orphan a live agent (DECIDE, **ESCALATE**)

**Why this is a task and not a group label.** v1 disposed of `closure-25`'s five items with one row:
"NOT BLOCKING — its own text labels them non-blockers". Three things are wrong with that. (a)
closure-25's **Blockers** section says "**None**"; the five are "Notes the PR body should absorb before
merge", so the row argues against a strawman. (b) `verify-web-data-docs.md` rates **note 1 OPEN
(documentation)** and **note 2 OPEN**. (c) note 1 is the guardrail whose removal
`audit-phase8-functional.md` **I3** measures.

**Verified evidence.** `POST /profile/delete-account` exists and is self-service
(`src/routers/profile.py:259`, with a confirm page at `:246`). phase8 I3 drove it: **302**, leaving
`status=active, user_id IS NULL` — an orphaned live agent. On the production copy, deleting Andrew I.
Su cascades **63 proposal reviews and 153 publications**. `ProposalReview.user_id` is
`ForeignKey("users.id", ondelete="CASCADE")` (confirmed at `src/models/agent_registry.py:82-84`), which
is the cascade path. And `0026` **on this branch** removed the `CheckViolation` that had been an
accidental guardrail. This is the most destructive newly-reachable path the branch creates, and it is
also the counter-evidence to Task 8's reassuring "0 of 53" measurement.

**The decision (branch owner's).**
- (a) **Refuse the delete while the user owns an active agent**, with an explanatory page telling them
  to deactivate or transfer the agent first. Smallest change that removes the destructive path.
  **Recommended.**
- (b) **Deactivate the owned agents in the same transaction** as the delete. Preserves self-service but
  silently takes agents off Slack.
- (c) **State it and ship it**, and open a follow-up. Only defensible if the branch owner judges the
  path acceptable; the closing comment for #25 must then say a PI can orphan an active agent.

- [ ] **Step 1: Re-measure the cascade** on the production copy for at least two users (one with an
  active agent, one without) and record the row counts in the decisions file. Do not inherit phase8's
  numbers.
- [ ] **Step 2: Write the escalation** in the decisions file with the measurement and the three options.
  **Do not implement until it is answered.**
- [ ] **Step 3: Write the failing test** for the chosen option.
- [ ] **Step 4: Run it and watch it fail. Implement. Watch it pass.**
- [ ] **Step 5: Close #25 P2's DoD gap in the same commit.** `passive_deletes=True` was removed and
  **nothing in `tests/` would notice it being removed again** (verify-web-data-docs #25 note 2, OPEN).
  Add that regression test — it is required by #27's blanket DoD clause.
- [ ] **Step 6: Run** `.venv-test/bin/python -m pytest tests/integration/test_db_contract.py tests/integration/test_profile_routes.py -q -p no:cacheprovider` (adjust to the real file names; read the directory first).
- [ ] **Step 7: Commit.**
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  src/routers/profile.py tests/integration/test_db_contract.py \
  docs/plans/2026-09-04-decisions/task-25.md
```
  Subject: `fix(profile): account deletion cannot orphan an active agent, and passive_deletes is pinned (#25 D1, P2)`

---

### Task 26: The nested gate runs inherit the operator's environment (FIX)

**Verified evidence (AH5, half-fixed).** `5090322` fixed the `exit 127` symptom. The **env inheritance**
that made it reachable is not fixed: nested `./scripts/ci.sh` invocations inherit `LOCK_SMOKE`, and the
consequence is now **a 180-second-timeout hang on a real hash-pinned pip install** rather than a fast
failure. The four call sites are `tests/unit/test_ci_gate.py:81`, `:99` and
`tests/unit/test_dependencies_lock.py:189`, `:223` — three build the child env as
`{**os.environ, …}` and `:223` scrubs `LOCK_SMOKE` alone, which is how the bug hid. **Reproduced at 2
of the 3 unscrubbed sites**, not all three; confirm which in your report.

**Files:** Modify `tests/unit/test_ci_gate.py`, `tests/unit/test_dependencies_lock.py`

- [ ] **Step 1: Write the failing test** — with `LOCK_SMOKE=1` exported in the parent environment, each
  nested-gate test still completes in its normal time. **Assert on the child's stdout** (the absence of
  the smoke step's own output), not on wall time alone.
- [ ] **Step 2: Run it with `LOCK_SMOKE=1` exported and watch it hang or fail.**
- [ ] **Step 3: Scrub the knobs in one shared helper.** Build the child env from an explicit
  **allow-list** rather than `{**os.environ, …}`, and use that helper at all four sites.
- [ ] **Step 4: Run both files with and without `LOCK_SMOKE=1` exported.**
- [ ] **Step 5: Commit.**
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  tests/unit/test_ci_gate.py tests/unit/test_dependencies_lock.py
```
  Subject: `test(ci): the nested gate runs build their environment from an allow-list (#27 I1)`

---

### Task 27: The mypy ceiling records the wrong number for the wrong reason (FIX)

**v1's diagnosis was wrong and is corrected here.** v1 said the ceiling "drifts with PyPI". It does
not. Bisected on clean `git archive` exports with one venv and the exact command `ci.sh` runs:
`f9541ba` → **145**, `f29e295` → **147**, HEAD → **147**, and the two new findings are:

```
src/services/profile_pipeline.py: error: Incompatible return value type
  (got "tuple[Publication | None, bool]", expected "tuple[Publication, bool]")  [return-value]   x2
```

That is exactly the annotation defect `audit-over-implementation.md` already filed
(`_insert_publication_tolerating_conflict` "annotates `tuple[Publication, bool]` while the loser branch
can return `None`"). So the ceiling moved because **this branch added two findings from a known bug** —
and **the slack is 3, not the 5 the `ci.sh` comment claims**. The `ci.sh` comment is separately
inconsistent: it says "measured … (commit `2170efb`) … 145 findings", and `2170efb` re-measures at
**147** today.

**The annotation fix itself is Group C's** (`src/services/profile_pipeline.py` — Task 19 Step 5). This
task must not touch that file. Run after Group C's Task 19 if you want the restored slack reflected;
otherwise record the number you measure and let Task 34 record the final one.

**Files:** Modify `scripts/ci.sh` (the `MYPY_MAX` comment and value), `tests/unit/test_ci_gate.py`

- [ ] **Step 1: Re-measure on a clean export** — `git archive HEAD src pyproject.toml` into the
  scratchpad, run `mypy src --ignore-missing-imports` with `.venv-test`, `grep -c ': error:'`.
- [ ] **Step 2: Correct the comment** to the measured number, its provenance (which commit, which
  mypy version), and the slack stated explicitly (measured N, ceiling N+k, k justified).
- [ ] **Step 3: Decide whether the dev extras need pinning** so `.venv-test` cannot drift from the lock,
  and either pin them or record why not — but do **not** present that as the fix for the 145→147 move.
- [ ] **Step 4: Close the mypy step's own DoD gap.** `tests/unit/test_ci_gate.py:90` asserts
  `"-m pytest" not in proc.stdout`, a string `ci.sh` never echoes, so the mutant is red only via a
  180 s `TimeoutExpired`. Replace it with an assertion on something `ci.sh` actually prints.
- [ ] **Step 5: Run** `.venv-test/bin/python -m pytest tests/unit/test_ci_gate.py tests/unit/test_dependencies_lock.py -q -p no:cacheprovider`
- [ ] **Step 6: Commit.**
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  scripts/ci.sh tests/unit/test_ci_gate.py
```
  Subject: `fix(ci): the mypy ceiling records the measured count, its provenance and its real slack (#27 I1)`

---

### Task 28: The gate must say which tests it did not run (FIX)

**Why.** `scripts/ci.sh` says nothing about the gated tiers, and the silence is the mechanism by which
the COR-1b data-loss regression reached HEAD: the test that caught it is in the live tier and passes at
`18ba52c`.

**v1 got the mechanism and the scope wrong; both are corrected.** The tier is **skipped, not
deselected** — `ci.sh:503` runs `pytest tests/` with **no `-m` expression**, and
`tests/conftest.py:158-171` adds a `skip` marker whose docstring says the skip is deliberate. So the
61 are already inside the gate's reported "120 skipped". And there is a **second** silent tier the plan
must not ignore: **42 `live_api` tests** (`tests/live_api/`), gated on `LIVE_API_TESTS` by the same
conftest hook. Measured with `pytest tests/ -m <marker> --collect-only -q`, which needs no
credentials: **61 `live_slack` + 42 `live_api` = 103**.

**Files:** Modify `scripts/ci.sh`; Test `tests/unit/test_ci_gate.py`

- [ ] **Step 1: Write the failing test** — a default gate run prints one line per gated tier naming the
  count that was not run and how to run it, and the counts are not hard-coded in `ci.sh`.
- [ ] **Step 2: Run it and watch it fail.**
- [ ] **Step 3: Print the notice** after the pytest step, counting each tier with
  `pytest tests/ -m <marker> --collect-only -q` (cheap, no credentials, no run) — **not** by parsing
  skip reasons and **not** by re-running the suite. Name `scripts/run_live_slack.sh` (the tracked path
  Task 2 created; v1 named a `/tmp` path) for the Slack tier and `LIVE_API_TESTS=1` for the API tier.
  Do **not** run either tier in the gate.
- [ ] **Step 4: Run** `tests/unit/test_ci_gate.py` and one full `./scripts/ci.sh` to see the notice in
  place.
- [ ] **Step 5: Commit.**
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  scripts/ci.sh tests/unit/test_ci_gate.py
```
  Subject: `fix(ci): the gate reports the 103 gated tests it did not run (#27 I1)`

---

### Task 29: The health probe returns 503 on a healthy database (FIX)

**Verified evidence (verify-deploy `2a-new`, OPEN — introduced by `5090322`, i.e. by this session).**
`src/main.py:72` sets `pool_pre_ping=False` on what `5090322` made a *pooled* engine (`pool_size=1,
max_overflow=0`). Measured against `copi_verify`: probe 1 (fresh) 200 → probe 2 (pooled) 200 →
`pg_terminate_backend` server-side → **probe 3 (stale pooled conn) 503** with
`asyncpg.exceptions.ConnectionDoesNotExistError` → probe 4 200. So a Postgres restart or an
idle-timeout reap emits exactly one spurious unhealthy probe. Severity low — the healthcheck is
`retries: 3` at `interval: 30s`, so one 503 cannot flip the container — but **`nginx` gates on
`app: service_healthy`**, so it is not cost-free.

**Also in this task (verify-deploy finding 2b): two stale claims `5090322` left behind.** Verified at
HEAD: `get_health_engine`'s docstring says "Lazily-built, **NullPool** engine used only by
/api/health", and the block above still says "…and **NullPool** so a hung probe cannot consume a
connection", two lines above an in-body comment that says "A tiny dedicated pool, **not NullPool**".

**Files:** Modify `src/main.py`; Test `tests/integration/test_health_route.py`

- [ ] **Step 1: Write the failing test** — a probe whose pooled connection has been terminated
  server-side still returns 200. Drive it the way verify-deploy did (`pg_terminate_backend` against the
  test database), not with a mock.
- [ ] **Step 2: Run it and watch it 503.**
- [ ] **Step 3: Set `pool_pre_ping=True`.** One extra sub-millisecond round trip per probe (measured
  probe cost 0.05 s fresh / 0.00 s pooled), comfortably inside `HEALTH_PROBE_TIMEOUT_SECONDS = 5.0`.
- [ ] **Step 4: Correct both stale NullPool claims.** Comment text only; D33 permits it.
- [ ] **Step 5: Run** `.venv-test/bin/python -m pytest tests/integration/test_health_route.py tests/unit/test_health_probe.py -q -p no:cacheprovider` (read the directory for the real second file name).
- [ ] **Step 6: Assert whether phase8 C2 is closed, in writing.** `audit-phase8-functional.md` **C2**
  is a Critical: `/api/health` **never returned** against a `docker pause`d Postgres — three requests
  each hung 40 s, and "every probe leaves a hung request holding a pool connection for the duration of
  the outage". `5090322`'s `command_timeout=4.0` plausibly closes it, but **no report has ever asserted
  that it does**, and an inferred closure of a Critical is an open Critical. Re-run C2's experiment
  (`docker pause` the *disposable* copy on 55434 — never production), and record in your decision file
  either "C2 closed, measured" with the numbers, or the residual.
- [ ] **Step 7: Commit.**
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  src/main.py tests/integration/test_health_route.py \
  docs/plans/2026-09-04-decisions/task-29.md
```
  Subject: `fix(health): pre-ping the probe's pooled connection so a reaped socket is not reported as an unhealthy database (#27 I2)`

---

### Task 30: Container memory — nginx's arithmetic, and the agent's unmeasured cap (FIX + DECIDE)

**30a — nginx's limit is below its own zone arithmetic (over-impl R9).** `nginx/nginx.conf:39-46`
declares 8 × `10m` zones and `ssl_session_cache shared:SSL:10m` under one name in all three vhosts =
**90 MiB declared**, into `mem_limit: 128m`. Measured **36.48 MiB idle** with 48 workers and
`shmem 640 KiB`, so filled zones land near **126 MiB before TLS** — inside the limit only while the
zones are empty.

**Two v1 errors corrected.** (i) The stale "40 MiB" justification is in
**`docker-compose.prod.yml:190-191`**, not in `nginx.conf`, and `nginx.conf:35` carries its *own* wrong
figure — "8x10m = 80m", which omits the SSL zone. Fix **both**. (ii) **`worker_processes` does not
exist anywhere in this repo** (`grep -rn worker_processes nginx/ docker-compose*.yml` → nothing); the
mounted file is a `conf.d` snippet and `worker_processes` is a main-context directive that cannot be
set from one. The 48-worker measurement comes from the image's stock config. **So "pin
`worker_processes`" is not an available remedy** — v1 offered it.

- [ ] **Step 1: Write the failing test** — a static assertion that the sum of declared zone sizes plus
  a stated per-worker allowance fits inside the configured `mem_limit`, **computed from the two files**
  rather than hard-coded. `tests/unit/test_nginx_config.py:197-203` already regex-parses
  `limit_req_zone`/`limit_conn_zone`, so follow that idiom.
- [ ] **Step 2: Run it and watch it fail** at 90 MiB of zones in a 128 m limit.
- [ ] **Step 3: Choose and apply one** — raise `mem_limit` to cover zones + workers + TLS with headroom,
  **or** shrink the zones to what the traffic needs (the rate-limit zones are per-vhost since
  `1a430c0`, so several have small populations). State the arithmetic in the comment, replacing **both**
  stale figures.
- [ ] **Step 4: `tests/unit/test_deploy_compose.py` hardcodes `EXPECTED_MEM`/`EXPECTED_CPUS` for all
  seven services plus a ≤3072m sum.** Update it, and check the sum still holds after 30b.
- [ ] **Step 5: Validate** with `nginx -t` on the rendered output the way the existing tests do, and
  re-render the merged compose (`docker compose … config`) to confirm the limit.

**30b — the agent's cap is unmeasured (over-impl R10, DECIDE).** `agent: mem_limit 768m` is an
unmeasured cap on the one process whose `SIGKILL` loses data — the DB, not Slack, is the durable store,
and an OOM kill skips the shutdown flush, which `stop_grace_period` cannot mitigate. Roster: **53
active**.

**v1's Step 1 here was unsafe and is replaced.** It ordered "a real `agent-run` turn" with no compose
file, no `SLACK_ENABLED=false`, no credential blanking and no preflight, in a checkout whose `.env`
holds live production Slack tokens (`SLACK_BOT_TOKEN_CRAVATT`, `SLACK_BOT_TOKEN_WISEMAN`,
`SLACK_CONFIG_TOKEN`, `SLACK_CONFIG_REFRESH_TOKEN`) that `Settings.get_slack_tokens()`
(`src/config.py:494`) reads as a live fallback — and CLAUDE.md's only documented way to start
`agent-run` uses the **production** compose files, which the constraints forbid. As written it could
post to production Slack as 53 bots.

- [ ] **Step 6: Measure with the isolation contract, or do not measure live at all.** Preferred: drive
  the simulation's turn loop from a synthetic harness against `copi-prodtest-db` (55434) with
  `SLACK_ENABLED=false`, sampling RSS from inside the process, and record peak. If you run a real
  container instead, **all** of these hold: run `scripts/live_slack_preflight.py` first and abort on
  non-zero; export the four production credentials as empty; `SLACK_ENABLED=false`; point
  `DATABASE_URL` at 55434 and assert it is not `copi`; name the **dev** compose file explicitly; set
  `--budget 1 --max-runtime 5`; and record the token cost (the run needs `ANTHROPIC_API_KEY`, which
  `.env` has — 108 characters — so this is a spend decision, not an availability one).
- [ ] **Step 7: Decide** from the measurement: keep 768 m, raise it, or remove the cap for `agent` the
  way D24 removed it for `postgres`. Record the number and the ruling in your decision file and in deploy note 18 (which already says "measure `agent-run` with `docker stats --no-stream` during a turn
  before tightening" — this is that measurement).
- [ ] **Step 8: If the cap changes**, update `docker-compose.prod.yml`, `tests/unit/test_deploy_compose.py`
  and the deploy-note per-service sum together.

**30c — the fourth `./prompts` mount (DECIDE, #27 I5-f).** v1 recorded this as "kept by design". That is
**rejected**: `audit-over-implementation.md` records that **I5 named that mount as a *defect***
("silently shadowing the image's prompts") on three services, `3354904` added a fourth (`worker`), and
`test_deploy_compose.py::test_worker_mounts_prompts_like_app_and_agent_do` now **pins** the widening. It
also conflicts with this plan's own top constraint: **D33 forbids any change under `prompts/`**, while
the mount exists precisely so an operator can change `prompts/` with no rebuild, no review and no gate.

- [ ] **Step 9: Rule and record** — either drop the fourth mount (and its pin), or state the D33 tension
  explicitly in the decisions file and in #27's closing comment. Both rules cannot govern silently.
- [ ] **Step 10: Commit** (one commit for 30a, one for 30b/30c).
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  nginx/nginx.conf docker-compose.prod.yml tests/unit/test_nginx_config.py \
  tests/unit/test_deploy_compose.py docs/plans/2026-09-04-decisions/task-30.md
```
  Subjects: `fix(deploy): size nginx's memory limit against its actual zone arithmetic (#27 I5)` and
  `fix(deploy): set the agent memory limit from a measured turn, and rule on the prompts mount (#27 I5, D19)`

---

### Task 31: The image and the operator script (FIX, two commits)

**31a — `plotly`'s move breaks the documented operator command (over-impl R20).** `python3
scripts/build_cabo_sankey.py --help` → `ModuleNotFoundError: No module named 'plotly'`. **No running
image is broken** — the dev image (2026-07-30) and prod's (2026-08-20) both still ship plotly 6.9.0, and
`.venv-test` masks it locally. It breaks at the **next `docker compose build`**, i.e. this branch's
deploy. `pyproject.toml:51` has plotly in the `scripts` extra only, and it is absent from
`requirements.lock`.

- [ ] **Step 1: Write the failing test** — the documented invocation either runs in the built image or
  fails with a message naming the extra (`pip install '.[scripts]'`), not a bare `ModuleNotFoundError`.
  **Read `tests/unit/test_cabo_sankey_comment.py` before you touch the script's header**: it pins
  `DEFAULT_START` *and the adjacency of the preceding comment line*, so editing the header raises
  `StopIteration` rather than failing readably. Fix that brittleness while you are there.
- [ ] **Step 2: Run it and watch it fail.**
- [ ] **Step 3: Pick one** — import `plotly` lazily with an actionable error, **or** document the extra
  in the script's header and in the runbook line that tells the operator to `docker cp` it in. D22 /
  #27 I4 deliberately moved plotly out of the runtime image, so do **not** put it back. While in the
  header, also correct the stale "scripts/ isn't mounted — `docker cp` it in first" sentence at
  `scripts/build_cabo_sankey.py:8`, which contradicts the DOC-7 paragraph (closure-26 residual).
- [ ] **Step 4: Run the script's own test and `python -c "import ast; ast.parse(open('scripts/build_cabo_sankey.py').read())"`. Commit.**
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  scripts/build_cabo_sankey.py tests/unit/test_cabo_sankey_comment.py
```
  Subject: `fix(scripts): build_cabo_sankey names the extra it needs instead of ModuleNotFoundError (#27 I4)`

**31b — keep `docs/plans` out of the image.** Measured in a real build: `/app` is **8.1 MB**, of which
`docs/plans` is **2.6 MB** (2,644,187 bytes) across **49 files** — including the 1.23 MB implementation
plan and the 44-file evidence subtree — plus `docs/superpowers` at 408 KiB: together ~38 % of `/app`.
`.notes/`, `backups`, `tests`, `mutants`, `.superpowers`, `.git` and `uv.lock` are correctly absent.
**Nothing sensitive is present** (zero hits for `xoxb-`, `AKIA`, private keys, `.env*`, dumps) — this is
bloat, not a leak, and #27 I3's `Fix:` covers both.

- [ ] **Step 5: Add the entries to `tests/unit/test_dockerignore.py`'s `MUST_EXCLUDE`** for a
  representative path under `docs/plans/` and one under `docs/superpowers/`.
- [ ] **Step 6: Run it and watch it fail.**
- [ ] **Step 7: Exclude them in `.dockerignore`.** Check first whether anything in the image reads
  `docs/` at runtime (`grep -rn '"docs/' src/ scripts/`) — `docs/specs` is already excluded, which is
  precedent, but verify rather than assume.
- [ ] **Step 8: Rebuild once, re-measure `/app`, then `docker rmi` the tag.** Record before/after. Commit.**
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  .dockerignore tests/unit/test_dockerignore.py
```
  Subject: `fix(deploy): keep docs/plans and docs/superpowers out of the image (#27 I3)`

---

# Closing sequence — Tasks 32-36. Strictly sequential, strictly last.

### Task 32: Produce the per-issue closure decision (FIX)

- [ ] **Step 1: For each of #20-#27, walk the issue's sub-PR list and Definition of done** and mark
  every clause `met` / `met with a stated carve-out` / `not met`. Read the DoDs from the **tracked**
  copies, `docs/plans/2026-09-02-close-issues-20-27-evidence/issues/issue_2N.md` (v1 pointed at a
  scratch file). Cross-check against `closure-2N.md`.
- [ ] **Step 2: Start from this plan's `## Definition of done` section**, which already lists five
  clauses v1 wrongly reported met. Do not re-derive it; extend it with what the tasks changed.
- [ ] **Step 3: State the disposition for each issue** — `closes at merge` / `closes after deploy
  verification` / `close by hand with a stated carve-out` / `do not close` — with the evidence a reader
  can check.
- [ ] **Step 4: Write it into `docs/plans/2026-09-04-decisions/README.md`** under a new
  `## Closure dispositions` heading, aggregating every `task-<N>.md` ruling.
  Task 33 consumes it; Task 36 quotes it. **This is the single source — v1 had Task 28 and Task 33
  each consuming the other.**
- [ ] **Step 5: Commit.**
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  docs/plans/2026-09-04-decisions/
```
  Subject: `docs: per-issue closure dispositions for #20-#27`

### Task 33: Write the post-merge closure handoff (FIX — the missing deliverable)

**Why.** No document tells an independent agent how to close #20-#27 after merge and deploy;
`docs/plans/*handoff*` and `*closure*` match nothing. Part R of
`docs/plans/2026-09-02-close-issues-20-27.md` is the *deploy* runbook; the closure layer does not exist.

**Files:** Create `docs/plans/2026-09-04-issue-closure-handoff.md`; Test `tests/unit/test_runbook_docs.py`

- [ ] **Step 1: Preconditions.** The branch is merged to `copi-prod`; Part R has been executed; the gate
  was green at the merge commit; the live Slack tier was run once against `copi-test` and its result
  recorded. State that closing an issue before its preconditions hold is worse than leaving it open,
  because a closed issue stops being re-verified.
- [ ] **Step 2: Copy the per-issue closure table** from the decisions file (Task 32 Step 4). Do not
  re-derive it.
- [ ] **Step 3: The `Closes` line to use, and the ones to omit.** As of this plan **#26 must not be in
  it**: its DoD clause 2 ("DOC-7 is verified by following the runbook end to end on a workspace with
  legacy rows") can only be satisfied on the prod host after merge.
- [ ] **Step 4: The DOC-7 verification procedure**, concretely: on the prod host, count `agent_messages`
  rows with `slack_ts IS NULL` (**23** at the 2026-09-04T08:00:03Z snapshot — re-count before you act;
  note that `audit-phase8-migration.md` reports **28** on the copy, and the disagreement is unexplained,
  so treat the live count as authoritative), run
  `docker compose exec -e PYTHONPATH=/app app python scripts/backfill_slack_ts.py` (dry run) then
  `--apply`, capture the transcript, re-count, and attach the transcript to #26's closing comment. The
  script has **no `--help`** (its CLI is `"--apply" in sys.argv`), so `--help` is not a safe no-op probe.
  **Do not claim the script was tested locally** — it makes outbound Slack calls, so it has never been
  run against the copy.
- [ ] **Step 5: The exact closing commands**, e.g.
  `gh issue close 23 --repo SuLab/coPI.science --comment "$(cat comment-23.md)"`, with a template
  comment per issue: what shipped, what was deliberately not done (quoting the issue's own scope
  language), the residuals a future reader must know, and the verification evidence.
- [ ] **Step 6: The carve-outs each closing comment must state.** At minimum:
  - **#20** — COR-5's reader consequence (the dashboard/e-mail still show "unreviewed" for an implicit
    marker); Task 5's and Task 7's rulings; whether Task 8 chose (c).
  - **#21** — the remaining COR-19.6 window and Task 12 Step 6's ruling; **and that #21's "from 0 %
    coverage" DoD clause was met by declaring its premise false** (`worker/main.py` was 71.83 % at the
    base commit), which a reader of a "clause met" line would otherwise misread.
  - **#22** — the word-range divergence (Task 21); item 35 / COR-24e's disk-export-ahead-of-commit
    ordering, **with the #29 linkage spelled out**: `AH4` made the pipeline an unconditional writer of
    `profiles/private/{agent_id}.md` and the agents read the disk copy, so an export that lands before a
    rolled-back commit leaves the disk authoritative and wrong.
  - **#23** — the ack detector's word-count trade (D12, 12 words) and Task 22's declared widening
    beyond U+2019.
  - **#25** — Task 25's ruling on self-service deletion.
  - **#26** — the two `test_prompt_hygiene.py` assertions that are already green at `18ba52c`, i.e.
    tests that cannot fail against pre-fix code (**→ F14**).
  - **#27** — D17's "no server-side CI", which the issue's body scopes out but whose **title** does not;
    and Task 30c's ruling on the prompts mount.
- [ ] **Step 7: What to do when verification fails.** Do not close; comment with the failure, and open a
  follow-up issue naming the specific clause that could not be verified.
- [ ] **Step 8: The follow-up issues to open** — copy `## Named follow-up issues` from this plan
  verbatim, one `gh issue create` line each, so the residuals become tracked work rather than prose
  nobody re-reads.
- [ ] **Step 9: Add a doc test** pinning the file's existence, the per-issue table and the `Closes`
  guidance, mirroring `tests/unit/test_runbook_docs.py`. **Write it to fail readably** — see Task 31
  Step 1 on the existing landmines. Also check `test_prompt_hygiene.py`'s ban on the repo's own
  `file.py:NN` citation convention before you write a document full of them; if it trips, narrow that
  assertion in the same commit and say so.
- [ ] **Step 10: Commit the handoff.**
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  docs/plans/2026-09-04-issue-closure-handoff.md tests/unit/test_runbook_docs.py
```
  Subject: `docs: post-merge issue-closure handoff for #20-#27`
- [ ] **Step 11: Apply the two runbook additions this plan deferred to you.** Part R
  (`docs/plans/2026-09-02-close-issues-20-27.md`) is edited only here, so that no two parallel groups
  share it. Fold in (i) Task 20's per-user pipeline re-run procedure from
  `docs/plans/2026-09-04-decisions/task-20.md`, as an ordered post-deploy step with its selection query
  verbatim, and (ii) Task 8's measured 0029 row into R.6's lock table — correcting that table's header,
  since `audit-phase8-migration.md` N4 records its published "worst-case 0.3 s" as not a measurement of
  this chain. Pin both with doc tests in `tests/unit/test_runbook_docs.py`, written to fail readably.
- [ ] **Step 12: Commit.**
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  docs/plans/2026-09-02-close-issues-20-27.md tests/unit/test_runbook_docs.py
```
  Subject: `docs(runbook): Part R carries the per-user pipeline re-run and 0029's measured lock row`

### Task 34: The gate must be green (FIX)

- [ ] **Step 1: Confirm the tree is clean.** `git status --short`.
- [ ] **Step 2: Run the whole gate in the foreground.**
  `MIGCHECK_PORT=55433 ./scripts/ci.sh 2>&1 | tee /tmp/ci_final.log; echo "GATE_EXIT=$?"`
  Compare against Task 1's recorded baseline. Single head must be `0029` if Task 8 landed a revision,
  else `0028`.
- [ ] **Step 3: Re-measure both ratchets on a clean export**, not the working tree —
  `git archive HEAD src pyproject.toml` into the scratchpad, then ruff and mypy. A working-tree
  measurement already produced one false 253 reading on this branch, from a stray file copied to the
  wrong path.
- [ ] **Step 4: If anything is red, fix it and re-run the whole gate.** Do not report a partial run, and
  do not quote a run that ended non-zero — that is exactly the error v1 made.
- [ ] **Step 5: Record the final figures** in `docs/plans/2026-09-04-decisions/baseline.md` as `final`.

### Task 35: The live Slack tier must be run, and its skips accounted for (DECIDE, then verify)

**Verified evidence.** The tier is **61** tests. The last run was **52 passed / 1 failed / 8 skipped**;
the failure was #20 COR-1b, since fixed by `f29e295`. **All 8 skips have one cause** — module-level
`pytestmark` `skipif`s on `ANTHROPIC_API_KEY` (`test_full_run_live.py:86-94` = 4 tests,
`test_slack_pi_live.py:26-32` = 4 tests). **The key is not missing:** `.env` holds a 108-character
`ANTHROPIC_API_KEY`; nothing exports it into the pytest process. So this is a **spend** decision, not
an availability one — and while those 8 skip, full-run bijection, the 4000-character split,
SIGTERM/restart durability and the whole PI-DM path are covered only with a mocked LLM.

- [ ] **Step 1: Decide whether to export `ANTHROPIC_API_KEY` for one run** and record the ruling in
  your decision file. If
  yes, run it and record the result. If no, record the decision and state in #20's and #21's closing
  comments that those four behaviours are mocked-LLM-only. Do not leave it implicit — that silence is
  what hid COR-1b.
- [ ] **Step 2: Run the tier** with `scripts/run_live_slack.sh` (Task 2), which refuses to start unless
  its preflight proves the four production credentials are blanked and every fixture token resolves,
  via Slack's own `auth.test`, to team `T0BMVSBMEC8` (`copi-test`).
- [ ] **Step 3: Expect 61 minus your skip count to pass.** Any failure is either a real defect or a
  workspace-state problem — diagnose it, do not retry blindly.
- [ ] **Step 4: Run the isolation audit that was never run.** `audit-live-slack-brief.md` exists and
  `audit-live-slack.md` does not. Dispatch it, and require it to verify isolation **a priori** (what
  credentials could the process reach) — checking the production workspace for absence of activity
  would itself violate the constraint.
- [ ] **Step 5: Consider the `live_api` tier too.** 42 tests, gated on `LIVE_API_TESTS`. Decide whether
  to run them (they hit real third-party APIs, not Slack) and record the decision either way — Task 28
  makes the gate announce them, so leaving them permanently unrun should be a choice, not an oversight.
- [ ] **Step 6: Tidy the workspace.** Each fixture creates a `t-`-prefixed channel and archives it;
  `scripts/slack_test_teardown.py` exists for leftovers. Report the count of unarchived `t-` channels.
- [ ] **Step 7: Record and commit the result** — pass/fail counts, the skip count and its cause, the
  `ANTHROPIC_API_KEY` and `live_api` rulings, and the isolation audit's verdict. Task 36 Step 4 quotes
  these figures, so they must be in the repo, not only in a transcript.
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  docs/plans/2026-09-04-decisions/task-35.md
```
  Subject: `docs: the live Slack tier's result, its skips and the isolation audit's verdict (#27)`

### Task 36: Correct the PR body (FIX — LAST)

**Verified evidence — four false claims in a tracked file.**
`docs/plans/2026-09-02-close-issues-20-27-pr-body.md`: `:179` still says
`Closes #20, #21, #22, #23, #24, #25, #26, #27` **including #26**, which its own closure audit rules NOT
CLOSABLE before merge; `:28` omits **A14** from #26's "What NOT" list (verified: the list is
`A1, A2, A5, C3, C5`), implying A14 was done when it has zero `src/` callers; `:26` misstates **D29**
and `:75` is false; `:90` carries a stale residual line.

**Files:** Modify `docs/plans/2026-09-02-close-issues-20-27-pr-body.md`

- [ ] **Step 1: Rewrite the `Closes` line** to exactly the issues Task 32 marks `closes at merge`, and
  add one sentence naming each omitted issue and where its closure is handled (the handoff doc).
- [ ] **Step 2: Fix `:26`, `:75`, `:90` and add A14 to `:28`.** Each correction cites the verification
  report that found it.
- [ ] **Step 3: Refresh the residual list** against everything this plan landed, so a fixed item is not
  still listed as a live risk — the previous pass had to drop 3 and narrow 10 for exactly this reason.
  `pr-body-residuals-final.md` (60 items) is the input, but it is **untracked**: copy forward what the
  PR body needs rather than citing it.
- [ ] **Step 4: Update the Verification section** to Task 34's and Task 35's figures. **Quote only a run
  that exited 0.**
- [ ] **Step 5: Commit.**
```bash
.superpowers/sdd/2026-09-02-close-issues-20-27/sdd-commit <msgfile> \
  docs/plans/2026-09-02-close-issues-20-27-pr-body.md
```
  Subject: `docs(pr): correct the Closes line, A14 and three stale claims in the PR body`

---

## STATE — verified-open items this plan deliberately does not fix

Each was checked against HEAD and is left as built, for the stated reason. Every one must appear in the
PR body (Task 36) and, where a closing comment would otherwise mislead, in that comment (Task 33).
**Four of v1's nine STATE bullets were rejected on review and are now tasks** (#22 item 15 → Task 20;
CL21-2 → Task 12 Step 6; #27 I5-f → Task 30c; closure-25 note 1 → Task 25). What remains:

- **#21 CL21-3.** The substantive half is fixed — `specs/local-db-conversations.md:65-73` says six writer
  slots and names `REMEDIATION_WRITER_SLOT = 99`, with an equivalent runtime guard at
  `scripts/migrate/remediate_duplicates.py:212-216`. Only a test's **name** still says "five":
  `tests/unit/test_ids.py:101::test_five_writer_slots_are_distinct_residues`. Rename it if you are
  already in that file; not worth a commit of its own.
- **#22 item 35 / COR-24e.** The pipeline's disk export can still land ahead of its DB commit. The
  issue's `Fix:` scopes the ordering fix to "the ONE remaining inversion" (the private-save route,
  closed by 22.10) and D28 recorded this as a follow-up — but Task 33 Step 6 must state the #29 linkage,
  because `AH4` made the pipeline an unconditional disk writer and the agents read the disk copy.
- **#23 R1.** GrantBot's transport-failure re-fire is bounded by the scheduler's 15-minute tick and is
  the deliberate consequence of `5295985`; the alternative is the silent whole-day loss the issue asked
  to remove.
- **#23 R10.** The two wall-clock pacing tests are timing-based. Stressed 15× at HEAD with 0 failures;
  deterministic clock injection is a larger refactor than the risk warrants.
- **#23 over-impl R6 / COR-28b.** The ack detector's word-count threshold closes the filed false
  positive by introducing an unfiled false negative (a short but substantive reply reads as an ack).
  D12 recorded 12 words as a deliberate choice. State the trade with the measured cut; replacing it with
  a length-independent content test is larger than the clause asks.
- **#26 R19.** A doc test asserts on the content of the 1.2 MB implementation plan, which is brittle.
  Narrow it if you are in that file — but note Tasks 20, 31 and 33 all walk into this family of tests
  and each is told to fix the brittleness it meets.
- **#27 I5-e.** `postgres` stays uncapped (D24, rationale in-file).
- **#27 I1-g / D17.** No server-side CI. The issue's **body** scopes it out; its **title** does not, so
  Task 33 must say so in #27's closing comment.
- **#27 verify-deploy finding 3c.** The `ci.sh` text-grep maintenance tax: `test_dependencies_lock.py`
  greps `ci.sh`'s text and `test_ci_gate.py` adds six more static greps on the same file. Tasks 26-28
  add more of the same. Accepted for now; **→ F21** proposes the replacement.
- **#27 phase8 I4 — an evidence caveat, not a defect.** The phase-8 functional measurements ran against
  a copy whose `llm_call_logs` was empty (1,509 MB of a 1,548 MB database), and one dashboard measured
  479,869 bytes against a real 2,459,998. **Every "measured on the production copy" number in this plan
  inherits that caveat**, which is why several tasks tell you to re-measure rather than inherit.

---

## Named follow-up issues

Every finding this plan does not act on, as a GitHub issue to open (Task 33 Step 8 carries the
`gh issue create` lines). Naming them is the disposition; none is a silent drop.

| id | title | source |
|---|---|---|
| **F1** | The DB-inbound tag route reaches only the first tagged bot, and has no sender-ownership check | plan-review-correctness; **do not "fix" by copying the Slack path** — that path routes only bots the sender owns (`simulation.py:3146`, `:3211`), and copying it to a path with no ownership check would widen a forged "PI said…" injection to every tagged bot |
| **F2** | The rebuild re-seeds `pi_context` for reopened threads, costing one stale re-injection per restart | audit-over-impl #20 E7c |
| **F3** | `_dead_thread_ids` is an in-process tombstone that drops PI rows with no log line | audit-over-impl Candidate B; a bare `continue` at `simulation.py:3263-3275` where D25 chose `logger.error` for a lost PI trigger |
| **F4** | The `Form(...)`-empty-string trap is systemic: nine required `str` fields 422 on a legitimately-empty submission | phase8 M2 (two sites remain at `agent_page.py:1238`, `:1335`) |
| **F5** | The D6 review upsert is unserialized: two simultaneous first-time reviews both report success | audit-over-impl Candidate A pt 3 |
| **F6** | `_MAX_MANIFEST_RETRY_AFTER = 30.0` caps a `Retry-After` the baseline honoured in full (measured up to 4500 s) | audit-over-impl #24 C2 |
| **F7** | No static guard against a future blocking `httpx` call on an async route | verify-web-data-docs N3 |
| **F8** | #26 blocker 3 (A10)'s residual, recorded as "FIXED with a residual" and never itemised | verify-web-data-docs |
| **F9** | Migration 0025's COALESCE merge treats `''` as present, so an empty keeper deletes the doomed row's real value | audit-over-impl; fix is `NULLIF(col,'')` |
| **F10** | `derive_agent_identity` yields `iii`/`IIIBot` for a generational suffix and a non-ASCII `agent_id` that becomes a Slack slug and a file path | phase8 M3 |
| **F11** | A blank private-profile save records a 3-byte whitespace `profile_revisions` row | phase8 M4 |
| **F12** | Charging the NCBI budget only on success means a throttled NCBI is free *and* retried, so the per-thread bound disappears when NCBI is failing | audit-over-impl #23 COR-30 |
| **F13** | 2,211 `agent_messages` rows carry `agent_id IS NULL` + a raw Slack uid as `sender_name` and are invisible to every gated agent; no backfill ships | closure-26 DOC-B, phase8 breakage 4 |
| **F14** | Two `test_prompt_hygiene.py` assertions are already green at `18ba52c` — tests that cannot fail against pre-fix code | closure-26; **a DoD violation, so #26's closing comment must disclose it** |
| **F15** | Web-vs-script bot-name divergence persists and `agents.bot_name` has no unique constraint | closure-26 |
| **F16** | `IntegrityError → 409 "please retry."` on both user-delete routes is misleading for a genuine residual constraint | audit-over-impl #25 D1.4 |
| **F17** | `check_lockfile` never inspects `--hash=sha256:` continuation lines (~40 transitive pins unexamined) and strips the bracket group, so `uvicorn[standard]` == `uvicorn` and an added extra is invisible | verify-deploy 3b; supply-chain |
| **F18** | `check_lockfile.check()` iterates only `direct_requirements(pyproject)`, so **removing** a dependency is never detected | verify-deploy 3a |
| **F19** | Migration postflight blind spots: cannot detect a concurrent deleter inside a duplicate group (C1); `expected_deletions` recorded even when 0025 is not pending (I2); the snapshot is not bound to the database/target it came from (I3); `--allow-row-growth` voids the exact-match guarantee its help text promises (I4); no-`--snapshot` WARNs and exits 0 (M9) | phase8 migration |
| **F20** | `POST /admin/agents/{id}/link` 500s on 127 of 144 dropdown users (`agents_user_id_key` UNIQUE, no guard), its unlink branch is unreachable (`value=""` 422s), and `/approve` 500s on a duplicate `agent_slug` | phase8 I1, I2 |
| **F21** | Replace the `ci.sh` text-grep test family with a mechanism that survives editing the script | verify-deploy 3c |

## Deliberately NOT in this plan

- **The 60 residuals in `pr-body-residuals-final.md`.** They are `STATE`, not `FIX`. Task 36 refreshes
  them; Task 33 carries the ones a closing comment needs.
- **#26's DoD clause 2.** Structurally impossible pre-merge (Task 33 Step 4 handles it post-deploy).
- **Rewriting the four mixed-attribution commits.** Content is correct in all four; only the subjects
  are narrower than their file lists. A history rewrite of 231 commits to fix commit messages risks
  more than it repairs, and it is the branch owner's call.
- **`R12`.** Verified **NOT-A-DEFECT**: measured both ways, `asyncio.shield` does persist the token
  triple under cancellation, and removing it loses it.
- **Adding server-side CI.** Decision D17, and #27's body scopes it out explicitly.
- **Any change under `prompts/` or to any inline model-facing string.** D33.
