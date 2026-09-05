# Post-merge closure handoff — issues #20-#27

**What this is.** The instructions for closing GitHub issues **#20-#27** in `SuLab/coPI.science`
after the `close-issues-20-27` branch is merged into `copi-prod` and deployed. It assumes you have
no context beyond this repository and no one to ask, so it repeats its inputs rather than
cross-referencing them.

**What this is not.** It is **not** the deploy runbook. Part R of
`docs/plans/2026-09-02-close-issues-20-27.md` (`## R.0` … `## R.12`) deploys the branch; this
document is the layer on top of it. Do not start here.

**Where the verdicts come from.** Every disposition below was graded once, by Task 32, in
`docs/plans/2026-09-04-decisions/README.md` §`Closure dispositions`, against the **Definition of
done** of the *tracked* issue copies in
`docs/plans/2026-09-02-close-issues-20-27-evidence/issues/issue_2N.md` — not against the plan and
not against the PR body. That file is the single source. **Do not re-derive the grading**; a second
derivation will differ from the first, and then nobody knows which is right. This document copies
it so you can act without reading 644 lines, but if the two ever disagree, the decisions file wins.

**The 18 per-task rulings** live in `docs/plans/2026-09-04-decisions/task-<N>.md`. Each carries a
`## Consequence a closing comment must state` heading; Step 6 below is the union of them.

---

## Step 1 — Preconditions

Closing an issue before its preconditions hold is **worse than leaving it open**. An open issue is
re-verified every audit pass; a closed one is not. Nothing in this backlog has ever been caught by
someone re-reading a closed issue, and two of this branch's worst defects (COR-1b's data loss, the
health probe's global) survived precisely because a green-looking claim stopped anyone from
looking again. If a precondition is not met, stop and follow Step 7.

- [ ] **The branch is merged.** The PR from `close-issues-20-27` is merged into `copi-prod` and you
      know the merge commit sha. Record it; every closing comment quotes it.
- [ ] **Part R has been executed** end to end on the production host, including `R.6` (migration to
      the derived head), `R.7` (service recreate), `R.7c` (the publication-text repair) and `R.9`
      (the agent restart). A closure written from a merged-but-undeployed branch describes code
      that is not running.
- [ ] **The gate was green at the merge commit.** Measured at the final branch commit:

      | figure | value |
      |---|---|
      | pytest | **2749 passed, 120 skipped** |
      | ruff `src/` | **250** / `SRC_LINT_MAX=260` |
      | mypy `src/` | **138** / `MYPY_MAX=150` |
      | branch coverage | **79.47 %** / `COV_MIN=60` |
      | alembic single head | **0029** |
      | `GATE_EXIT` | **0** |
      | wall clock | 8:46 |

      Re-run `MIGCHECK_PORT=55433 ./scripts/ci.sh` on the merge commit and compare. **If the gate is
      not green at the merge commit, every `closes at merge` row in Step 2 becomes `do not close`**
      — on a red gate a new failure cannot be told from a standing one, so no closure is honest.

- [ ] **The live Slack tier was run once against `copi-test` and its result is recorded.**

      > :construction: **PLACEHOLDER — NOT YET KNOWN.** At the time this handoff was written the
      > live tier (`scripts/run_live_slack.sh`, workspace team `T0BMVSBMEC8`) was **still running**
      > and **no result existed**. Nobody has invented one. Before you close **#27**, find the run's
      > outcome — Task 35's record, or a fresh run — and replace this block with:
      >
      > - the pass/fail/skip counts for the 61 live-tier tests,
      > - which tests skipped and why (a silent skip is the failure mode the tier exists to prevent),
      > - the `ANTHROPIC_API_KEY` ruling: was the key exported, or are the four LLM-dependent
      >   behaviours (full-run bijection, the 4000-character split, SIGTERM/restart durability, the
      >   whole PI-DM path) covered only with a mocked LLM?
      > - whether the preflight's **check 3** (every fixture token resolves via Slack's own
      >   `auth.test` to `T0BMVSBMEC8`) *accepted*. Until one transcript shows check 3 accepting, the
      >   only evidence that exists is that it refuses; the accept path has never run against a real
      >   workspace.
      >
      > **#27 cannot be closed without this.** #20's comment also depends on it (carve-out 7).
      > If the run failed, Step 7 applies. Do not write "the live tier passed" from this document.

- [ ] **You are on the right repository.** Every command below says
      `--repo SuLab/coPI.science` explicitly. There are two production instances (org1/`copi.science`
      and blackbird); the issues live in one repo.

---

## Step 2 — The per-issue closure table

Copied verbatim from `docs/plans/2026-09-04-decisions/README.md` §`Closure dispositions` → `Summary`.

| issue | DoD clauses | disposition | why |
|---|---|---|---|
| **#20** engine | 3 met | **closes at merge** | all three of `closure-20`'s open `Fix:` clauses landed (Tasks 7, 8+9, 10); its two claim-accuracy blockers landed (Tasks 16, 29) |
| **#21** worker / e-mail | 2 met, **1 met with a stated carve-out** | **close by hand with a stated carve-out** | the coverage clause was met by **declaring its premise false**, and the residual orphan-channel window carries an operator procedure that must not be lost to an auto-close |
| **#22** profiles | 2 met | **closes at merge** | both clauses met and independently re-measured; the residuals are data-repair and a ruled-deliberate divergence |
| **#23** external clients | 2 met | **closes at merge** | `closure-23` found **no** blockers; Tasks 22-24 closed the residuals it listed |
| **#24** web request path | 2 met, **1 met with a stated carve-out** | **closes at merge** | the concurrent-insert clause is now met by a real two-connection test (`1230419`); C2's clause is met one layer below the word "handler" |
| **#25** data layer | 2 met | **closes at merge** | both clauses met; the new self-service-delete refusal is a user-visible behaviour change the comment must state |
| **#26** documentation | 1 met, **1 not met** | **closes after deploy verification** | DOC-7's end-to-end clause can only be satisfied on the prod host, after merge |
| **#27** deploy / gate / image | **1 met with a stated carve-out** | **close by hand with a stated carve-out** | its own DoD *is* the gate, so Task 34's and Task 35's figures have to exist before the comment can be written; and D17 makes the title read as an overclaim |

A **carve-out is not a softer "met"**. It means the clause is satisfied but a comment that says only
"met" would mislead the next reader. Step 6 is the list of things that would mislead.

---

## Step 3 — The `Closes` line, and the three issues that must stay off it

The PR body (`docs/plans/2026-09-02-close-issues-20-27-pr-body.md`) carries exactly:

```
Closes #20, #22, #23, #24, #25
```

**#21, #26 and #27 must not be on the `Closes` line.** GitHub closes a linked issue the instant the
PR merges, with no comment attached, and each of these three needs something said *at the moment of
closing*:

- **#21** — its coverage clause was met by declaring the issue's own premise false. An auto-close
  publishes "DoD met" with no correction attached, and it drops the orphan-sweep procedure an
  operator must run once before `ENABLE_INBOUND_EMAIL` is turned on.
- **#26** — DoD clause 2 is **not met** and cannot be met until the DOC-7 procedure has been run on
  the production host. See Step 4. Closing it at merge would assert a verification that has not
  happened.
- **#27** — its definition of done *is* the gate, and two of the figures a truthful comment must
  quote (Task 34's final gate, Task 35's live-tier transcript) are produced after the last
  implementation commit. Until they exist, anything said about #27 is a prediction.

If you are editing the PR body and the line differs from the one above, the decisions file is
authoritative; do not add an issue to it to save yourself a `gh issue close`.

---

## Step 4 — #26 DoD clause 2 (DOC-7): the post-deploy verification procedure

The clause: *"DOC-7 is verified by following the runbook end to end on a workspace with legacy
rows."* It is operational, not test-shaped, and it is the one clause no pre-merge work could
satisfy.

### What you must know before you type anything

- `scripts/backfill_slack_ts.py` **has no offline mode.** It aborts with exit 1 if
  `get_any_bot_token` returns `None`, and it issues one live `conversations.replies` /
  `conversations.history` call **per candidate row** in *both* report and `--apply` mode.
- The script has **no `--help`.** Its CLI is literally `"--apply" in sys.argv`
  (`scripts/backfill_slack_ts.py:157`), so `--help` is **not** a safe no-op probe — it falls through
  to a DB connect and a full run in report mode.
- **Do not claim the script was tested locally.** It has never been run against the disposable
  production copy. The copy's tokens were rewritten to `xoxb-neutralized-local-copy`, which
  `is_valid_token` accepts *on prefix*, so a local run would fire real HTTPS requests at slack.com
  (barred), fail auth on every one, print `confirmed=0 db_origin=0 unverified=N`, exit 2, and write
  **zero** rows. That exercises the candidate query and the exit-code contract and demonstrates none
  of the repair.
- A different real workspace would not do either: the verification *is* "does Slack recognise
  **these** timestamps".

### The row count, and why two numbers are printed here

**23** `agent_messages` rows have `slack_ts IS NULL` on the local production copy at the
2026-09-04T08:00:03Z snapshot (all 23 are real candidates for the script's `SELECT`).
`audit-phase8-migration.md` reports **28** on `copi_replay` (24 candidates). The disagreement is
**unexplained**. **Re-count on the live host and treat the live count as authoritative** — neither
number above is a prediction of what you will find.

### The procedure

Run this **after** `R.7` has recreated `app` on the new image (the script needs a running `app`
container; `--via-run` cannot host it). Part R's `## R.6b` carries the same commands as a deploy
step; this adds the verification and the evidence capture that clause 2 asks for.

```bash
cd /home/ubuntu/copi-python && . /tmp/deploy.env && [ -n "$C" ] || { echo "deploy.env missing"; exit 1; }

# 1. Count BEFORE. Record the number; the whole verification is a before/after.
docker compose $C exec -T postgres psql -U copi -d copi -c \
  "select count(*) from agent_messages where slack_ts is null"

# 2. Dry run. Capture the whole transcript -- it is the evidence for the closing comment.
docker compose $C exec -T -e PYTHONPATH=/app app \
  python scripts/backfill_slack_ts.py 2>&1 | tee /tmp/doc7-dryrun.log

# 3. Read the summary line: "confirmed=<a>  db_origin=<b>  unverified=<c>".
#    unverified > 0 means Slack could not be ASKED (token scope, bot not in channel, rate limit).
#    It is not a verdict. Fix access and re-run the dry run before applying, or those rows stay NULL.

# 4. Apply. Safe to re-run; read-only against Slack otherwise.
docker compose $C exec -T -e PYTHONPATH=/app app \
  python scripts/backfill_slack_ts.py --apply 2>&1 | tee /tmp/doc7-apply.log

# 5. Count AFTER. It must have dropped by EXACTLY <a> (the confirmed count from step 3).
docker compose $C exec -T postgres psql -U copi -d copi -c \
  "select count(*) from agent_messages where slack_ts is null"
```

6. **Prove the repair did what it is for.** Pick one thread whose root row was repaired and confirm
   that a reply into it actually lands on Slack. That is the behaviour the NULL `slack_ts` was
   suppressing (`_slack_parent_ts` in `src/agent/simulation.py` returns `None` for a legacy root, and
   its callers skip the mirror rather than invent a timestamp Slack never issued). A count that
   dropped is not yet a demonstration that the runbook works end to end.

7. **Attach both transcripts** (`/tmp/doc7-dryrun.log`, `/tmp/doc7-apply.log`), the before and after
   counts, and the reply evidence to #26's closing comment. Nothing short of that is "end to end on
   a workspace with legacy rows".

**Exit codes:** `0` clean, `1` no usable bot token (nothing was attempted), `2` at least one
unverified row. A `2` is not a failure of the repair, but it means the verification is **partial** —
say so in the comment, or run it again once access is fixed.

**If any of steps 1-6 does not behave as described, do not close #26.** Step 7.

---

## Step 5 — The exact closing commands

One comment per issue, written to a file first so it survives a shell mishap and can be reviewed
before it is published.

```bash
REPO=SuLab/coPI.science
MERGE_SHA="<the merge commit sha from Step 1>"   # substitute before running anything

# Issues that GitHub closed automatically at merge (#20, #22, #23, #24, #25):
# they are already closed -- you are adding the carve-out comment, not closing them.
gh issue comment 20 --repo "$REPO" --body-file comment-20.md
gh issue comment 22 --repo "$REPO" --body-file comment-22.md
gh issue comment 23 --repo "$REPO" --body-file comment-23.md
gh issue comment 24 --repo "$REPO" --body-file comment-24.md
gh issue comment 25 --repo "$REPO" --body-file comment-25.md

# Hand-closed, comment and close in one call:
gh issue close 21 --repo "$REPO" --comment "$(cat comment-21.md)"
gh issue close 27 --repo "$REPO" --comment "$(cat comment-27.md)"

# #26 ONLY after Step 4 has been run and its transcripts are in comment-26.md:
gh issue close 26 --repo "$REPO" --comment "$(cat comment-26.md)"
```

Verify each one landed, rather than assuming:

```bash
for n in 20 21 22 23 24 25 26 27; do
  gh issue view "$n" --repo "$REPO" --json number,state,closedAt \
    --jq '"#\(.number) \(.state) \(.closedAt // "-")"'
done
```

If any of #20, #22, #23, #24, #25 still reports `OPEN`, the merge did not carry the `Closes` line
(a squash-merge rewrites the PR body's linkage in some configurations). Close it by hand with the
same comment — `gh issue close <n> --repo "$REPO" --comment "$(cat comment-<n>.md)"` — and do not
add it to a `Closes` line retroactively.

### The template every comment follows

Save as `comment-2N.md`. Four sections, in this order, because a reader who stops after the first
must still have been told the truth:

~~~markdown
**Closed by** `<MERGE_SHA>` (PR from `close-issues-20-27` → `copi-prod`), deployed <date> per
Part R of `docs/plans/2026-09-02-close-issues-20-27.md`.

## What shipped
<one line per `Fix:` clause of this issue, each naming the commit that implements it. Copy the
evidence column from `docs/plans/2026-09-04-decisions/README.md` §`#2N`; do not re-derive it.>

## Definition of done
<the issue's own DoD clauses, each graded `met` / `met with a stated carve-out` / `not met`, with
the measurement. Quote the issue's wording, not a paraphrase of it.>

## What was deliberately NOT done
<quote the issue's own scope language where it excludes something, then the rulings that chose not
to do something the issue's text could be read to ask for. Step 6 of the closure handoff is the
list for this issue.>

## Residuals a future reader must know
<the carve-outs from Step 6 that are still-true facts about production, each with the follow-up
issue number it is tracked as, if any.>

## Verification
<the gate figures at the merge commit; for #26 the DOC-7 transcripts; for #27 the live Slack tier
transcript. Rulings: `docs/plans/2026-09-04-decisions/`.>
~~~

### Worked example — `comment-23.md`

~~~markdown
**Closed by** `<MERGE_SHA>`.

## What shipped
- COR-28a — the apostrophe class now covers U+2018 / U+00B4 / U+FF07 as well as U+2019 (`554b139`).
- COR-27b — `extract_foa_number` canonicalises the FOA number instead of returning the matched case
  verbatim (`554b139`).
- COR-27 — the spin-off compare is case-insensitive (`554b139`).
- COR-29 — the HTTP retry budget is bounded by a deadline, the keyed NCBI path is paced at its
  policy ceiling, and `_NCBI_SEMAPHORES` is keyed per running loop (`962aa6c`).
- The three missing tests: the delegate-sync log line, ORCID/grants retry behaviour, and the tool
  budget call sites (`df5c45b`).

## Definition of done
1. "each PR ships a test that fails against the pre-fix code" — **met**. Re-derived per sub-PR by
   swapping one file into a scratch export: V7 → 5 failed with `18ba52c:slack_client.py`; V8 →
   `test_foa_pattern.py` uncollectable + 9 + 5 + 2 + 2 failures against `dd82c91~1`; V9 → 3 failed
   against `ec9853a~1`, `test_http_retry.py` unimportable at `18ba52c`.
2. "The regex fixes should be table-driven over the cases above" — **met**.
   `tests/unit/test_foa_pattern.py:8-19` parametrises all six rows of this issue's own divergence
   table, plus a lowercase twin of each and 2-/4-digit-year variants.

## What was deliberately NOT done
- R8: `_execute_retrieve_abstract` / `_execute_retrieve_full_text` are still dead code in `src/`.
  The shipped path (`execute_tool`) was pinned instead of deleting them, because deleting them would
  also delete the subject of `tests/unit/test_retrieve_tools_authors.py`. Tracked as a follow-up.
- A loop-level NCBI budget for `convert_dois_to_pmids`. The retry budget bounds a *call*, not the
  pipeline. Tracked as a follow-up.

## Residuals a future reader must know
<Step 6's #23 block, verbatim.>

## Verification
Gate at the merge commit: 2749 passed, 120 skipped, ruff 250/260, mypy 138/150, branch coverage
79.47 %, alembic head 0029, `GATE_EXIT=0`.
~~~

---

## Step 6 — The carve-outs each closing comment must state

Paste the relevant block into the comment's *What was deliberately NOT done* / *Residuals* sections.
These are not optional colour: each one is a fact about production that a reader of "closed, DoD
met" would get wrong.

### Three rulings that overturned the plan's own recommendation

A reader who sees only the outcome will assume the plan was followed. It was not, three times, and
each reversal was decided on a measurement:

1. **Task 12 Step 6 / CL21-2 (→ #21).** The plan recommended option (a): drop the per-call timestamp
   suffix from the private-channel name so Slack's `name_taken` becomes the idempotency guard.
   **Option (b) shipped instead** — leave the window, state it, ship an orphan-sweep procedure.
   Two independent reasons: `_build_slug` (`src/services/private_channels.py:90-106`) is
   `priv-{sorted agent ids}-{origin channel}` and carries no proposal identity — the production copy
   already runs two legitimate same-slug channels for two *different* proposals
   (`priv-lairson-su-drug-repurposing` `C0AUCMNCEFQ`, April, and
   `priv-lairson-su-drug-repurposing-20260616-180113` `C0BB48ETLQL`, June), and **111** (pair,
   origin-channel) groups cover **407 of 1017** `thread_decisions`, the largest holding 39; under (a)
   the June reopen would have dropped one PI's guidance into another proposal's conversation. And
   (a) does not even produce idempotency: `create_private_channel` **does not adopt on `name_taken`**
   — it retries with random entropy (`src/agent/slack_client.py:946-952`) and returns `None` when
   attempts are exhausted, so (a) converts "mints a second channel" into "the reopen fails with no
   channel". (`create_channel`, its sibling, *does* adopt; this one deliberately does not.)
2. **Task 29 Step 3 / the health probe (→ #27, #25).** The plan recommended `pool_pre_ping=True`.
   It was **measured and rejected**: it re-opens phase-8 C2, the audit Critical that the same task
   had to assert closed. SQLAlchemy runs the pre-ping inside `engine.connect()`, *outside* the probe's
   `asyncio.wait_for`, and asyncpg's `command_timeout` cancel handshake opens a **second** connection
   to the same frozen server and blocks there — against a paused Postgres the probe **never answered
   at all** (>15 s, measured to 25.0 s). What shipped is a single retry on
   `DBAPIError.connection_invalidated`, engine kwargs unchanged. Cost was never the reason: pre-ping
   measured ~1.3 ms against `HEALTH_PROBE_TIMEOUT_SECONDS = 5.0`. **Anyone re-adding `pool_pre_ping`
   to the probe engine must first re-run the `docker pause` experiment.**
3. **Task 5 / COR-1b's parked threads (→ #20).** The plan recommended closing the thread via
   `_close_thread(agent, thread, "timeout")` (option (b)). **Option (c) shipped** — keep parking,
   exclude parked threads from both load accountants. `outcome` is a PostgreSQL enum
   (`Enum("proposal","no_proposal","timeout", name="thread_outcome_enum")`) with **no truthful value
   for an undeliverable thread**, and a close would DM the PI the literal sentence *"Thread with
   {other} in #{channel} timed out (reached message limit without a conclusion)"* about a thread that
   may hold two messages — and write that false conclusion into **both** agents' prompt-fed working
   memory, which is how issue #29's six-week memory-poisoning chain started.

Two smaller reversals, recorded so they are not read as oversights: **Task 27** declined to pin the
dev extras against PyPI drift (the ceiling has only ever moved on this repository's own code; mypy
is already capped `>=2.3,<2.4`), and **Task 24** found R5's premise was simply wrong (`d0d9b3d`
shipped a behavioural test in the same commit; the audit grepped for the full log line while the
test asserts a lowercased substring).

### #20 — agent engine

1. **COR-5's named reader consequence is still true, by design.** The issue's text says the defect
   makes "dashboard/email keep showing 'unreviewed'". **That symptom survives the fix.** The engine's
   implicit engagement is recorded as `thread_decisions.pi_engaged_at` (and, for linked PIs, a
   `rating=-1` `ProposalReview`), and **every** reader deliberately excludes both —
   `src/main.py:182`, `src/routers/admin.py:864`, `src/routers/agent_page.py:255`,
   `src/services/email_notifications.py:184,896`. A PI who engaged without an explicit 1-4 rating
   still sees the proposal as awaiting review. This is Decision D6 and it is test-pinned; it is not a
   gap left by accident.
2. **COR-5's carrier is a substitution against the issue's literal wording.** The clause says
   "insert a `ProposalReview`"; the shipped carrier is `thread_decisions.pi_engaged_at`, because
   `proposal_reviews.user_id` is `ondelete="CASCADE"` to `users` — the issue's literal instruction
   would have built a **self-erasing** record that a PI deletion wipes. The behaviour asked for (the
   rebuild does not re-block after a restart) is delivered; the row shape is not the one the issue
   names. **Name migration `0029`.**
3. **COR-10(3) is closed by re-ordering *plus* a new durable marker**, not by re-ordering alone: the
   log entry's presence *was* the dedup key and `MessageLog.append` is not idempotent, so the clause
   is unsatisfiable without a distinct key (`agent_messages.pi_inbound_state`, also in `0029`).
4. **Task 5's ruling** — see the overturned-rulings block above. Two residuals: a parked thread is
   **never closed** (with a silent counterpart it stays `status="active"` until the run ends), and
   `post_failure_count` is in-memory only, so a restart un-parks every parked thread. The exclusion
   is a within-run correction, the same scope as the back-off it corrects.
5. **Task 16's count rescoping was an extra fix to a real production bug**, not one of #20's `Fix:`
   clauses: **12 of 53 active agents were mis-counted**, `wiseman` by 89. Say so plainly.
6. **Task 16's accepted cost:** `[Reopened] …` guidance no longer renders on `/admin/discussions` at
   all (it previously rendered under a false `0/4` badge). It is still durable in
   `proposal_reviews.comment`, in the private channel and on the PI's dashboard. → **F26**.
7. **The live-tier ruling on `ANTHROPIC_API_KEY`** decides whether four behaviours (full-run
   bijection, the 4000-character split, SIGTERM/restart durability, the whole PI-DM path) are covered
   only with a mocked LLM. If the key was not exported, **#20's comment must say so** — that silence
   is what hid COR-1b. See the Step 1 placeholder.

Not blockers, recorded so nobody re-opens them: the COR-2 restart residual, the in-process-only
dead-thread tombstone, E6(1)'s ≤2× retry overshoot, COR-9b's missing-target fallback, COR-6's
data-dependent cursor. Follow-ups **F1**, **F2**, **F3**.

### #21 — worker and e-mail

1. **The "from 0 % coverage" clause was never satisfiable as written, and was met by declaring its
   premise false.** `worker/main.py` measured **71.83 %** branch coverage at the pre-fix base
   `18ba52c` (114 stmts / 30 missed) and **77.72 %** at HEAD (162 / 33). `tests/integration/
   test_worker.py` landed in `d732804` (2026-07-30), an ancestor of `18ba52c`, three days after the
   issue's verification point, and the 2026-08-11 re-verification never refreshed the sentence.
   `pyproject.toml`'s `[tool.coverage.run]` has `source=["src"]` and no `omit`, so **no configuration
   exists under which that file reported 0 %**. Quote both measurements and `d732804`; **do not
   repeat the 0 % figure anywhere**, and do not report the clause as plainly "met" — a reader
   comparing "from 0 % coverage" against `git log` would otherwise reasonably conclude that either
   the measurement or the claim was fabricated.
2. **COR-19.6's residual window is knowingly left open** — Task 12 Step 6 ruled option (b); see the
   overturned-rulings block. After `391e545` (the offline migration commits its own rows) and
   `d7ce1a5` (the reopen route reads `refined_in_channel`), a duplicate private channel now requires
   `migrate_public_thread_to_private`'s own `db.commit()` to fail in the instant *after* Slack created
   the channel and accepted the handover. **That failure leaves no DB row at all, so no code-side
   guard can see it.** Both the web reopen route and the e-mail reopen path can reach it; the e-mail
   one only once `ENABLE_INBOUND_EMAIL` is on.
3. **The orphan-sweep procedure**, verbatim into the comment. Run it after any reopen that returned a
   500 or a terminal "couldn't reopen" e-mail, **and once before enabling inbound e-mail**:
   - DB side, the channels that are supposed to exist:
     `SELECT channel_id FROM agent_channels WHERE visibility='collab_private';`
   - Slack side, per bot token: `conversations.list(types="private_channel")`, keeping names matching
     `priv-*`.
   - An orphan is a Slack `priv-*` channel whose id is **not** in the DB set. It will have the
     handover posted and both bots as members, and no bot will ever read it again.
   - Remediate by `conversations.archive` after confirming the same proposal has a live channel —
     compare `thread_decisions.refined_in_channel` for the pair. **Do not delete DB rows; there are
     none.**
4. **Closing the window properly needs a durable claim row committed before the Slack
   `conversations.create`, reconciled after it returns.** New design work, deliberately not
   improvised. Follow-up.
5. **CL21-3's substantive half is fixed** — `specs/local-db-conversations.md:65-73` now says six
   writer slots and names `REMEDIATION_WRITER_SLOT = 99`. Only a *test's name* still says five:
   `tests/unit/test_ids.py:101::test_five_writer_slots_are_distinct_residues`.
6. **Task 14's 409 is a user-visible response-code change** shared with #24 (below); it is reached
   from the same review path this issue's e-mail sweeps feed.

Follow-ups: **F4**, **F5**, **F6**, **F7**.

### #22 — profile pipeline

1. **The word-range divergence stays, deliberately** (Task 21, option (a), no code change). The
   *reported* half is fixed: the log line and the PI-facing "unvalidated" text used to say `150-250`
   while the code enforced `100-350`, and both now interpolate `_MIN_SUMMARY_WORDS` /
   `_MAX_SUMMARY_WORDS` (`2ee48fa`, templates `1c6ba09`). The remaining gap — the **prompt** asks
   `150-250`, the **validator** allows `100-350` — is a request inside a limit, and
   `prompts/profile-synthesis-sparse.md:39-42` already instructs the model to write fewer than 150
   words on thin evidence and names the 100 floor as why that is safe. **Do not "align" the two
   ranges later without the measurement:** on the production copy **2 of 141 stored profiles (1.4 %)
   sit in 100-149 and would begin failing**; **0** sit in 251-350; the longest production summary is
   245 words. A failing gate discards the refresh or stores `synthesis_validated = False`, which
   strips its overwrite protection.
2. **Item 15 / COR-16: the corrupted publication text is repaired *after* deploy, not by merging.**
   The `itertext()` parser fix (`2c1d504`) and the existing-row refresh (`35cc010`) are
   forward-looking. Re-measured by re-fetching **all 4,508 rows** through the fixed parser: **896 rows
   still hold text that differs from PubMed's — 209 titles and 877 abstracts, 97 % of the differences
   being a strict prefix of the correct value**, i.e. truncation. A per-user pipeline re-run cannot
   close it: **84 of the 512 rows the corruption signature flags carry neither a PMID nor a DOI on
   their owner's current ORCID record**, including every row belonging to the twelve PIs whose ORCID
   works list is empty. `scripts/repair_publication_text.py --all` (dry run by default, re-runnable)
   repairs all 896; Part R `## R.7c` carries it as an ordered post-deploy step. **State whether R.7c
   was actually run, and its before/after counts.**
   **This is not the Task 19 defect** — Task 19 is synthesis blanking curated lists on *write* (8 of
   141 profiles with an empty `key_targets`); this is corrupted evidence on *read*. Two different
   columns, two different fixes; do not let a comment merge them.
3. **Task 19 fixed the LLM-response twin of COR-22's `Fix:` clause, not the clause itself.** An
   omitted or mistyped `keywords` / `key_targets` / `experimental_models` no longer blanks a curated
   column, on either the protected or the `synthesis_validated = False` path. **The three web save
   routes COR-22 names (`agent_page.py`, `profile.py`, `onboarding.py`) are a different fix and were
   not in Task 19's scope — do not report COR-22 as closed on the strength of it.**
4. **Item 35 / COR-24e stands, with the #29 linkage spelled out.** The pipeline's disk export can
   still land ahead of its DB commit. The issue's `Fix:` scopes the ordering fix to "the ONE remaining
   inversion" (the private-save route, closed by 22.10) and D28 recorded this as a follow-up — **but
   `AH4` made the pipeline an unconditional writer of `profiles/private/{agent_id}.md` and the agents
   read the disk copy**, so an export that lands before a rolled-back commit leaves the disk
   authoritative and wrong. That is the same class of failure as issue #29.

Follow-ups: **F9**, **F10**, **F11**.

### #23 — external clients

1. **R5 was a grep artifact, not a coverage hole — say why it looked unmet.** The audit grepped for
   the full log line `"skipping delegate Slack-ID sync"`, while `d0d9b3d`'s own test
   (`tests/integration/test_agent_page.py::test_accepting_an_invitation_with_no_bot_token_skips_the_sync_and_logs`)
   asserts a **lowercased substring** of it. Reporting R5 as "a sub-PR shipped without a test" would
   be wrong.
2. **`_execute_retrieve_abstract` / `_execute_retrieve_full_text` are still dead code in `src/`.**
   Task 24 pinned the shipped path (`execute_tool`) rather than deleting them, because deleting them
   would also delete the subject of `tests/unit/test_retrieve_tools_authors.py`, a file Task 24 did
   not own. Follow-up.
3. **The plan's justification for R7 was wrong in one factual detail, and the comment must not repeat
   it.** `src/agent/grantbot.py` does **not** import `src/agent/tools.py`, so the grantbot scheduler —
   the one long-running process calling `asyncio.run()` repeatedly — never touched the NCBI
   semaphores. Peak concurrent keyed NCBI calls in a real grantbot day: **0**. The hazard was
   structurally present but never reachable; it is now removed outright.
4. **The retry budget bounds a call, not the pipeline.** `convert_dois_to_pmids` still issues one NCBI
   call per unresolved DOI sequentially, so a sustained outage costs `180 s × DOI count` inside one
   profile job (down from `420 s × DOI count`). `worker/main.py`'s
   `JOB_STALE_PROCESSING_THRESHOLD_SECONDS = 3600` stopgap still stands; its "≈243 s per `_ncbi_get`"
   comment is now an over-estimate (120.5 s) — safe direction, left alone as it is another task's
   file. A loop-level budget is a follow-up.
5. **The deliberate trade:** under a sustained NCBI brownout a profile job now gives up on an
   individual lookup sooner, so a profile synthesized during an outage may carry fewer publications.
   Already possible after 4 attempts; likelier now, in exchange for a job that terminates.
6. **COR-28b's ack detector (D12).** The word-count threshold closes the filed false positive by
   introducing an **unfiled false negative** — a short but substantive reply reads as an ack.
   **12 words** was a deliberate cut. State the trade with the measured number; a length-independent
   content test is larger than the clause asks.
7. **Task 22's widening beyond U+2019.** The issue names only that code point; the shipped class
   covers the remaining apostrophe forms (U+2018, U+00B4, U+FF07). Declared, not silent.
8. **D10 is a rollout note, not a defect:** prod needs a dedicated GrantBot Slack token or GrantBot
   posts nothing — exactly what COR-26c's `Fix:` asked for. An `xoxb-`-shaped but dead token passes
   `is_valid_token`, so the preflight should assert `auth.test`, not just the prefix.

Also state the paced figure: the keyed NCBI path now runs at **9.52 req/s** against a 10 req/s policy
ceiling (was 8.33), and the semaphore slot is released across backoff instead of held for the whole
retry loop. Follow-up: **F12**.

### #24 — web request path

1. **V5's recovery arm in `review_proposal` now ends in a 409, not a 302**, when the `IntegrityError`
   was **not** the review-uniqueness conflict. This is a **user-visible response-code change** on
   `POST /agent/{agent_id}/proposals/{id}/review`, and `a5666b4`'s test was **amended rather than
   deleted** to record why. Option (b) — keep the 302, just stop retiring the notification — was
   rejected because the reminder is at best the next scheduled digest and is suppressed entirely for
   `email_notification_frequency='off'` or a paused mailbox, leaving the PI shown a success page for
   a write that did not happen.
2. **The matching `EmailNotification` is deliberately left `sent`**, so the reminder loop keeps
   chasing a proposal whose review was not persisted. `record_engagement` still runs and commits: the
   PI *did* act, and dropping it would count our own write failure against them and eventually
   downgrade their e-mail frequency.
3. **There is no matching change in `reopen_proposal`'s sibling arm.** Its recovery is real (it
   re-creates the PI's inbox guidance row on the Slack-off path, `d9f0797`), so its 302 is truthful.
   Task 14 deliberately did not touch it.
4. **Clause 3's letter-gap is the word "handler".** Four thread-identity assertions prove the real
   `httpx.post` executes off the loop thread (`tests/unit/test_slack_provisioning.py:43-63,144-201`),
   mutation-confirmed — but they sit one layer down, at the service functions the two `async def`
   routes await. That boundary is pinned by monkeypatch trip-wires on `ap.create_app_async` /
   `exchange_code_async` / `lookup_team_id_async` plus an `inspect.getsource` assertion, and both
   handlers are a bare `await <service>(…)` (`admin_provision_slack`, `src/routers/admin.py:999`; `admin_provision_slack_callback`, `:1026`). **There is no
   route-level or loop-responsiveness test anywhere in `tests/`.**
5. **Task 13 removed dead machinery rather than fixing it:** the `refined_channel_id` re-bind capture,
   its assignment in the `except IntegrityError` arm, and the "recovered by re-binding" log clause are
   gone; the `ThreadDecision` re-select stays with `scalar_one_or_none()` and a new purpose. No
   behaviour a PI or operator can observe changes beyond two log lines that now say something true.

Follow-ups: **F5**, **F6**, **F7**, **F16**.

### #25 — data layer

1. **Self-service account deletion is now REFUSED while the user owns an active agent** — a behaviour
   change to a user-facing route, not only a data-integrity fix. Name the page the user sees and the
   remedy (deactivate or transfer the agent first). **The guard blocks 56 of 144 users and leaves
   self-service deletion intact for the other 88.** "Owns an active agent" means an `agents` row whose
   `user_id` is the caller and whose `status` is in `{'active','pending'}`; `inactive` deliberately
   does **not** block (otherwise the refusal page's own remedy would be unreachable), `suspended` does
   not block, and a delegation never blocks. `pending` blocks because `admin_update_agent` promotes a
   pending row straight to `active` without re-reading `user_id`, and 2 of the 3 pending rows on the
   production copy already carry a `slack_bot_token`.
2. **D1 removed an accidental guardrail, and the cascade is real.** Re-measured on the disposable
   copy, each `DELETE` rolled back: deleting `Andrew I. Su` (owner of the **active** `su` agent)
   removes **153 publications and 63 proposal_reviews** via `ondelete="CASCADE"` and leaves
   `agents.agent_id='su', status=active, user_id IS NULL` via `SET NULL`. Those FK rules are
   pre-existing; what the branch changed is that `0026` removed the `CheckViolation` that had made the
   delete impossible. That is **what #25 D1 asked for**, so D1 is closed — but record the
   orphaned-active-agent and cascaded-review-history consequence, and that **neither delete route
   renders a confirmation banner** (#25 M6).
3. **P2's `passive_deletes=True` is now pinned by a test**, since the backlog-wide DoD clause was
   previously unmet for P2 — nothing in `tests/` would have noticed the flag being removed. Three
   tests in `test_db_contract.py` section 7 cover it: a mapper walk over all 11 `delete-orphan`
   relationships with the exact name set as an anti-vacuity control; a schema check that each of the
   11 child FKs is `confdeltype='c'`; and a behavioural test that a user delete emits **no SQL against
   `publications`** yet the rows are gone.
4. **P3's `pool_pre_ping` on the app engine is untouched**, and must stay off on the *health probe*
   engine — see the overturned-rulings block, ruling 2.

Follow-up: **F16**.

### #26 — documentation

1. **The `test_prompt_hygiene.py` assertions — F14, and state it precisely.** The file has three
   assertions across two tests, evaluated against `git show <rev>:specs/email-proposal-review.md`:

   | revision | `"…\| LLM prompt for classifying" not in spec` | `"email_inbound.py" in spec` | `not re.search(r"email_inbound\.py:\d+(-\d+)?", spec)` |
   |---|---|---|---|
   | `18ba52c` (copi-prod tip) | **red** | **green** | **green** |
   | `e038528~1` (parent of test 1's fix) | red | red | green |
   | `c09f29f~1` (parent of test 2's fix) | green | green | **red** |
   | `HEAD` | green | green | green |

   So **exactly two of the three assertions are already green at `18ba52c`**, and
   `test_email_proposal_review_spec_has_no_stale_line_range_citation` is green **as a whole test**
   there, i.e. it cannot fail against `copi-prod`'s code — the literal negation of the backlog-wide
   DoD clause. The honest refinement: **each test *is* red against the parent of the commit whose
   change it pins**, because the stale line range was introduced *on this branch* by `e038528` and
   removed by `c09f29f`. They are legitimate guards; they are not regression proofs against
   pre-branch code. **Nothing on this branch changed that file**
   (`git log 64e9981..HEAD -- tests/unit/test_prompt_hygiene.py` → 0 commits). → **F14**.
2. **`closure-26` blockers 3, 4 and 5 are fixed** by `34d3c15` — the `AGENT.md` lab counts (residual
   → **F8**), the missing `-e PYTHONPATH=/app` in the CLAUDE.md/README DOC-7 paragraphs (`CLAUDE.md:139`,
   `README.md:97`), and A9's "daily" digest imprecision.
3. **Blocker 2 — A14 has no work at HEAD.** `audit_recipient_list` still has zero `src/` call sites.
   Mitigating fact: the issue's "dead" framing is itself wrong — `prompts/daily_audit.md` is a
   host-cron Claude prompt whose consumer is out-of-band by design.
4. **DOC-B's fix is forward-only.** **2,211** `agent_messages` rows on real production carry
   `agent_id IS NULL` with a raw Slack uid as `sender_name` and stay invisible to every gated agent.
   No backfill ships; #26's text does not ask for one. → **F13**.
5. **The web-vs-script bot-name divergence persists** and `agents.bot_name` has no unique constraint.
   → **F15**.
6. **#26 R19** — a doc test asserts on the content of the 1.2 MB implementation plan. Brittle,
   accepted; narrow it if you are already in that file.
7. **The DOC-7 evidence from Step 4** — both transcripts, the before/after counts, the reply that
   landed on Slack. Without it clause 2 is still not met and the issue stays open.

### #27 — deploy, gate and image

1. **D17 / I1-g — there is still no server-side CI, and no `.github/` workflow shipped.** The issue's
   **body** scopes this out conditionally and the condition was met; its **title** ("no GitHub
   Actions") does not. Say so explicitly, or the close reads as an overclaim.
2. **The mypy ceiling's number, provenance and slack.** `MYPY_MAX` has never moved off **150**. The
   measured count fell **147 → 145 → 140 → 138**, each a real fix, none silenced with a
   `type: ignore`. The provenance block in `scripts/ci.sh` records **138 measured at `45d1198` with
   mypy 2.3.1** on a clean `git archive` export. Slack is 12 (8.7 %), looser than `SRC_LINT_MAX`'s
   3.6 %, and the comment must say why: tightening a ratchet inside the change that closes 36 tasks
   would put an unrelated red in front of a reviewer; **145 (slack 7) is recorded as the right
   post-merge value.** Carry the correction that "the ceiling drifts with PyPI" was **wrong** — every
   move it has ever made was caused by this repository's own code, mypy is version-capped at
   `>=2.3,<2.4`, and **no dependency pinning was needed and none was added**.
3. **The health probe** — see the overturned-rulings block, ruling 2.
4. **phase-8 C2 is CLOSED by measurement, not by inference.** Against a paused Postgres every probe
   answered: 503 in 5.004 s first, 3.00 s (`pool_timeout`) while the first probe's connection is still
   held, 0.004 s once the pooled socket is known dead. C2's accumulation claim no longer applies to
   the request pool at all — the probe has its own engine capped at one connection. **Residual, stated
   rather than hidden:** during an outage that one connection is held by an orphaned greenlet, so
   probes 2..n 503 on `pool_timeout` instead of on the database — correct verdict, different reason.
5. **The live Slack tier is now runnable only through `scripts/run_live_slack.sh`**, which refuses to
   start unless `scripts/live_slack_preflight.py` proves, in one process, that the four production
   credentials are **present and empty** (empty overrides `.env`; absent falls through to it —
   measured), that **no** usable production bot token resolves through `Settings` (**grantbot
   included**: `Settings.get_slack_tokens()` is a hand-written dict that omits it — 125
   `slack_bot_token_*` fields, 124 mapping entries — so `production_token_map()` widens it, and
   narrowing that back re-opens a hole straight into the production workspace), that every fixture
   token resolves via Slack's own `auth.test` to team **`T0BMVSBMEC8`**, and that the tier's own
   environment is complete so its 61 tests cannot silently skip. 13 of 13 isolation mutants killed.
   **Limit: the accept path has never been exercised against a real workspace** — the first live
   transcript is the first evidence that check 3 passes rather than merely refuses. See Step 1.
6. **Task 30c's ruling on the prompts mount.** `./prompts:/app/prompts` was removed from `worker`,
   reverting `3354904`'s widening of a mount #27 I5 had itself called a defect, along with the test
   that pinned it. **The same mount remains on `app`, `agent` and `grantbot`, and is still a defect** —
   left deliberately, because removing it changes deployed behaviour and belongs with the runbook.
   State the tension with **D33**: D33 forbids any change under `prompts/`, while the mount exists
   precisely so an operator can change `prompts/` with no rebuild, no review and no gate. Under D33
   the mount has no remaining benefit and one cost — an un-gated write path into model-facing text on
   three production services, invisible to `scripts/ci.sh`. Removing the last three is a follow-up.
7. **The container memory figures, and the one post-deploy check that remains.** `agent`'s **768m** is
   no longer unmeasured: peak **226 MiB** over a full 53-agent `--reset-cursors` sweep and **185 MiB**
   steady over 318 turns, against a clone of the production database with Slack off and the LLM faked
   — 3.4× headroom, flat after the second sweep. Kept at 768m; **not** uncapped, because uncapping
   hands the choice of OOM victim to the kernel on a 3.7 GB host whose largest-RSS candidate is the
   uncapped `postgres`. The one term the offline measurement cannot cover is
   `_rebuild_state_from_slack`. **On the first live `agent-run` after this deploy, take one
   `docker stats --no-stream` during a turn and compare against 226 MiB; above ~500 MiB the cap must
   be revisited.** Put the observed figure in the comment. nginx's **128m** is unchanged but now
   *correct* rather than coincidentally survivable: `nginx.conf` declared **90 MiB** of shared zones
   into a 128 MiB cgroup; the zones are sized to their populations and total **37 MiB**, and a test
   derives the sum from the file. The seven-service sum is unchanged at 2432 MiB.
8. **The gate now announces the tests it did not run** (`af25113`) — 103 gated tests, so a silent skip
   is no longer possible.
9. **The process fix this branch exists for:** v1 of the plan wrote six rulings into `.superpowers/`,
   which `.gitignore:122` ignores, so `sdd-commit` (`git commit --only`) could not commit them and
   **no post-merge reader could ever see them**. All rulings for this plan are tracked under
   `docs/plans/2026-09-04-decisions/`.
10. **STATE, unchanged:** I5-e (`postgres` stays uncapped, D24); verify-deploy 3c (the `ci.sh`
    text-grep maintenance tax → **F21**); D1's cascade cost (`user: "0:0"`, `--via-run`); and
    **phase-8 I4 — an evidence caveat on every "measured on the production copy" number in this
    backlog**, because those measurements ran against a copy whose `llm_call_logs` was empty
    (1,509 MB of a 1,548 MB database), and one dashboard measured 479,869 bytes against a real
    2,459,998.

Follow-ups: **F17**, **F18**, **F19**, **F20**, **F21**.

---

## Step 7 — What to do when a verification step fails

**Do not close the issue.** A closed issue stops being re-verified, and a closure written over a
failed verification is indistinguishable from a closure written over a passed one.

1. Comment on the issue with the **failure itself** — the command, its output, the exit code. Not a
   summary of it.
2. Name the **specific DoD clause** that could not be verified, quoting the issue's own wording.
3. Open a follow-up issue for the failure, and link it from the comment:

   ```bash
   gh issue create --repo SuLab/coPI.science \
     --title "#2N DoD clause <k> could not be verified after the close-issues-20-27 deploy" \
     --body "Clause: <verbatim clause>. Attempted: <command>. Result: <output>. Blocks closing #2N."
   ```
4. Leave the issue **open** and say in the comment what would have to be true for it to close.
5. If the failure is in the **gate** rather than in an issue-specific verification, stop closing
   anything: every `closes at merge` disposition in Step 2 is conditional on a green gate at the
   merge commit.

---

## Step 8 — The follow-up issues to open

Every residual this backlog does not act on, as a tracked issue. **Naming them is the disposition;
none is a silent drop.** Open them before or immediately after the closures, so the closing comments
can link them.

**F23 and F24 are FIXED and must not be opened.** They were routed here as deploy blockers and
`71082fb` closed both: `run_migration.sh` now derives its target from `alembic/versions/` instead of
the stale `TARGET="0028"` literal, and `preflight.SUPPORTED_START_REVISIONS` is derived from
`REVISION_ORDER` instead of the hand-maintained `("0018".."0024")` tuple.

`F27`-`F31` are ids assigned by this handoff to the five residuals the decisions file named without
numbering.

```bash
R=SuLab/coPI.science

gh issue create --repo $R --title "The DB-inbound tag route reaches only the first tagged bot, and has no sender-ownership check" \
  --body "plan-review-correctness (F1). Do NOT 'fix' by copying the Slack path: that path routes only bots the sender owns (src/agent/simulation.py:3146, :3211), and copying it to a path with no ownership check would widen a forged 'PI said…' injection to every tagged bot."

gh issue create --repo $R --title "The rebuild re-seeds pi_context for reopened threads, costing one stale re-injection per restart" \
  --body "audit-over-impl #20 E7c (F2)."

gh issue create --repo $R --title "_dead_thread_ids is an in-process tombstone that drops PI rows with no log line" \
  --body "audit-over-impl Candidate B (F3): a bare 'continue' at src/agent/simulation.py:3263-3275, where D25 chose logger.error for a lost PI trigger."

gh issue create --repo $R --title "The Form(...)-empty-string trap is systemic: nine required str fields 422 on a legitimately-empty submission" \
  --body "phase8 M2 (F4). Two sites remain at src/routers/agent_page.py:1238 and :1335."

gh issue create --repo $R --title "The D6 review upsert is unserialized: two simultaneous first-time reviews both report success" \
  --body "audit-over-impl Candidate A pt 3 (F5)."

gh issue create --repo $R --title "_MAX_MANIFEST_RETRY_AFTER = 30.0 caps a Retry-After the baseline honoured in full (measured up to 4500 s)" \
  --body "audit-over-impl #24 C2 (F6)."

gh issue create --repo $R --title "No static guard against a future blocking httpx call on an async route" \
  --body "verify-web-data-docs N3 (F7). #24's clause 3 is met one layer below the route; nothing pins the route itself."

gh issue create --repo $R --title "#26 blocker 3 (A10)'s residual, recorded as 'FIXED with a residual' and never itemised" \
  --body "verify-web-data-docs (F8). The AGENT.md lab counts were fixed by 34d3c15; the residual was never written down."

gh issue create --repo $R --title "Migration 0025's COALESCE merge treats '' as present, so an empty keeper deletes the doomed row's real value" \
  --body "audit-over-impl (F9). Fix is NULLIF(col,'')."

gh issue create --repo $R --title "derive_agent_identity yields iii/IIIBot for a generational suffix and a non-ASCII agent_id that becomes a Slack slug and a file path" \
  --body "phase8 M3 (F10)."

gh issue create --repo $R --title "A blank private-profile save records a 3-byte whitespace profile_revisions row" \
  --body "phase8 M4 (F11)."

gh issue create --repo $R --title "Charging the NCBI budget only on success means a throttled NCBI is free and retried, so the per-thread bound disappears when NCBI is failing" \
  --body "audit-over-impl #23 COR-30 (F12)."

gh issue create --repo $R --title "2,211 agent_messages rows carry agent_id IS NULL + a raw Slack uid as sender_name and are invisible to every gated agent" \
  --body "closure-26 DOC-B, phase8 breakage 4 (F13). DOC-B's fix is forward-only; no backfill ships and #26's text does not ask for one."

gh issue create --repo $R --title "Two test_prompt_hygiene.py assertions are already green at 18ba52c — tests that cannot fail against pre-fix code" \
  --body "closure-26 (F14). A DoD violation, disclosed in #26's closing comment. Each test IS red against the parent of the commit it pins, because the stale line range was introduced on this branch by e038528 and removed by c09f29f; they are legitimate guards, not regression proofs against pre-branch code."

gh issue create --repo $R --title "Web-vs-script bot-name divergence persists and agents.bot_name has no unique constraint" \
  --body "closure-26 (F15)."

gh issue create --repo $R --title "IntegrityError -> 409 'please retry.' on both user-delete routes is misleading for a genuine residual constraint" \
  --body "audit-over-impl #25 D1.4 (F16)."

gh issue create --repo $R --title "check_lockfile never inspects --hash=sha256: continuation lines and strips the bracket group, so uvicorn[standard] == uvicorn" \
  --body "verify-deploy 3b (F17), supply-chain: ~40 transitive pins unexamined and an added extra is invisible."

gh issue create --repo $R --title "check_lockfile.check() iterates only direct_requirements(pyproject), so REMOVING a dependency is never detected" \
  --body "verify-deploy 3a (F18)."

gh issue create --repo $R --title "Migration postflight blind spots (C1, I2, I3, I4, M9)" \
  --body "phase8 migration (F19): cannot detect a concurrent deleter inside a duplicate group (C1); expected_deletions recorded even when 0025 is not pending (I2); the snapshot is not bound to the database/target it came from (I3); --allow-row-growth voids the exact-match guarantee its help text promises (I4); no --snapshot WARNs and exits 0 (M9)."

gh issue create --repo $R --title "POST /admin/agents/{id}/link 500s on 127 of 144 dropdown users, its unlink branch is unreachable, and /approve 500s on a duplicate agent_slug" \
  --body "phase8 I1, I2 (F20). agents_user_id_key is UNIQUE with no guard; the unlink branch's value=\"\" 422s before it is reached."

gh issue create --repo $R --title "Replace the ci.sh text-grep test family with a mechanism that survives editing the script" \
  --body "verify-deploy 3c (F21). test_dependencies_lock.py greps ci.sh's text and test_ci_gate.py adds six more static greps on the same file; Tasks 26-28 added more of the same."

gh issue create --repo $R --title "SimulationEngine.start() re-fetches the FOA cache serially on a cold start" \
  --body "F22, found by Task 30's measurement harness. simulation.py:730 calls _backfill_foa_cache() (:4452) over every grantbot_posted_foas.foa_number (251 rows on the production copy). foa_cache.py:90 skips entries already on disk and data/ is bind-mounted in prod, so this is a cold-cache cost only — but it is serial and unbounded in count: with no network each item burns http_retry's full backoff (~3.5 s measured), i.e. ~15 min of startup sleeping before turn 1; with network it is 251 sequential api.grants.gov fetches. Bound it, parallelise it under the existing semaphore, or move it off the startup path."

gh issue create --repo $R --title "agent_page.py:255 files a reopen sentinel under the dashboard's 'Reviewed Proposals' heading" \
  --body "F25, Task 16 accepted cost. The rendering is fixed (Task 16 added a 'Reopened with guidance' branch) so no false score is shown, but the categorisation is wrong."

gh issue create --repo $R --title "[Reopened] guidance no longer renders on /admin/discussions at all" \
  --body "F26, accepted cost of Task 16's predicate change: a misleading 0/4 badge is worse than an absent line. The text is still durable in proposal_reviews.comment, in the private channel and on the PI's dashboard."

gh issue create --repo $R --title "_execute_retrieve_abstract / _execute_retrieve_full_text are dead code in src/" \
  --body "F27, task-24.md §2. Their only callers are tests/unit/test_retrieve_tools_authors.py. Either delete both and re-point that file at execute_tool, or keep them and accept two renderings of the same answer."

gh issue create --repo $R --title "A durable claim row committed before conversations.create, reconciled after it returns" \
  --body "F28, task-12.md §4. Closes CL21-2's residual window properly: today a commit failure in the instant after Slack created the channel leaves no DB row at all, so no code-side guard can see it. New design work, deliberately not improvised on this branch."

gh issue create --repo $R --title "Remove ./prompts:/app/prompts from the remaining three services (app, agent, grantbot)" \
  --body "F29, task-30.md. The mount was removed from worker only. Under D33 (no prompt changes) it has no remaining benefit and one cost: an un-gated write path into model-facing text on three production services, invisible to scripts/ci.sh. Removing it changes deployed behaviour, so it belongs with a runbook step."

gh issue create --repo $R --title "A loop-level NCBI budget for convert_dois_to_pmids" \
  --body "F30, task-23.md §3. The retry budget bounds a call, not the pipeline: convert_dois_to_pmids still issues one sequential NCBI call per unresolved DOI, so a sustained outage costs 180 s × DOI count inside one profile job."

gh issue create --repo $R --title "A truthful terminal state for an undeliverable thread" \
  --body "F31, task-5.md. Needs a new thread_outcome_enum value and a migration; not part of #20. Today a parked thread is never closed, because the only available outcome ('timeout') would DM the PI a false conclusion about a thread that may hold two messages, and write it into both agents' working memory."
```

---

## Appendix — where each input lives

| what | where |
|---|---|
| the per-issue grading and dispositions (**the single source**) | `docs/plans/2026-09-04-decisions/README.md` §`Closure dispositions` |
| the 18 per-task rulings, each with a `## Consequence a closing comment must state` | `docs/plans/2026-09-04-decisions/task-<N>.md` |
| the gate baseline and final figures | `docs/plans/2026-09-04-decisions/baseline.md` |
| the deploy runbook | `docs/plans/2026-09-02-close-issues-20-27.md` `## R.0`-`## R.12` |
| the tracked issue copies whose DoD clauses were graded | `docs/plans/2026-09-02-close-issues-20-27-evidence/issues/issue_2N.md` |
| the PR body and its `Closes` line | `docs/plans/2026-09-02-close-issues-20-27-pr-body.md` |
| the plan these steps come from | `docs/plans/2026-09-04-close-remaining-gaps.md` Task 33 |
| this document's pins | `tests/unit/test_runbook_docs.py` |
