# Decisions log — `docs/plans/2026-09-04-close-remaining-gaps.md`

Every **DECIDE** verdict in that plan produces a written ruling **in this directory, in the
repository**. v1 of the plan wrote its six rulings into `.superpowers/`, which `.gitignore:122`
ignores (`git ls-files .superpowers` returns 0 files): `sdd-commit` — `git commit --only -- <paths>`
— cannot commit an ignored path, so none of them existed for anyone who came after the session that
made them. Reports and scratch analysis may stay in `.superpowers/`; **decisions and deliverables
may not.**

## The convention

- **One file per task: `task-<N>.md`. Never a shared file.** A single decisions file is a file that
  five parallel groups all write to, which is the exact condition that produced four
  mixed-attribution commits on this branch (`29bee9c`, `5040516`, `eda8603`, `7cc2ea3`). One
  `sdd-commit`'s `flock` cannot prevent it either: it serialises the git index, not two agents
  editing the same file.
- Wherever the plan says "the decisions file", it means **your own task's file**.
- Three headings, in this order:
  - `## Ruling` — the options considered, the option chosen, and why.
  - `## Evidence` — what you measured, with the commands, so the next reader can re-measure.
  - `## Consequence a closing comment must state` — what Task 32/33 must carry into the GitHub
    issue comment or the PR body. If the answer is "nothing", say that explicitly.
- Commit your file with the path list your task prints, and nothing wider.

## Files that are not `task-<N>.md`

- `baseline.md` — Task 1's gate baseline (figures, or what was wrong if it was red). Task 34 compares
  against it and Task 36 quotes it. Task 32 appends `## Closure dispositions` to it.

## Rulings already made by the branch owner

`task-7.md`, `task-8.md` and `task-25.md` were **escalated by the plan and ruled by the branch owner
on 2026-09-04, before their tasks started**. Their `## Ruling` sections are pre-seeded and marked
`RULED BY BRANCH OWNER 2026-09-04`. The implementer of those tasks **implements the ruling and fills
in `## Evidence`; it does not re-decide it.** If you believe a pre-seeded ruling is wrong, stop and
say so in your report — do not quietly implement something else.

## No credential values in this directory

Several of these rulings are about Slack isolation. Record **presence, absence and lengths only** —
never a token, and never a line copied out of `.env`.

---

## Closure dispositions

**Owned by Task 32** of `docs/plans/2026-09-04-close-remaining-gaps.md`. This is the **single
source** for which of #20-#27 may be closed and how. Task 33 copies the table below rather than
re-deriving it; Task 36 rewrites the PR body's `Closes` line from it. v1 of the plan had Task 28 and
Task 33 each consuming the other, so neither existed.

**Specification order.** The clauses graded below are the **Definition of done** of the *tracked*
issue copies, `docs/plans/2026-09-02-close-issues-20-27-evidence/issues/issue_2N.md` — not the plan,
and not `pr-body.md`. Cross-checked against
`.superpowers/sdd/2026-09-02-close-issues-20-27/closure-2N.md` (which graded each issue at `93a48e7`,
before this plan's tasks landed) and against the 18 `task-<N>.md` rulings in this directory.

**Grades.** `met` / `met with a stated carve-out` / `not met`. A carve-out is not a softer "met": it
means the clause is satisfied but a closing comment that says only "met" would mislead a reader.

### Gate figures — PLACEHOLDER, Task 34 supplies the final numbers

> **:construction: Task 34 must replace this block.** Every disposition below is conditional on a
> green gate at the merge commit. The figures known at the time of writing, from the implementers'
> clean-`git archive` measurements (never the working tree — a working-tree read on this branch
> produced a false 253/148 while other implementers had uncommitted edits):
>
> | figure | Task 1 baseline (`64e9981`) | provisional at `45d1198` | Task 34 final |
> |---|---|---|---|
> | alembic single head | `0028` | **`0029`** (`0029_pi_engagement_and_inbound_state`, `down_revision = "0028"`) | *(pending)* |
> | ruff `src/` | 251 / 260 | **250** / 260 | *(pending)* |
> | mypy `src/` | 147 / 150 | **138** / 150 | *(pending)* |
> | pytest | 2568 passed / 120 skipped | *(pending)* | *(pending)* |
> | branch coverage | 78.80 % / 60 | *(pending)* | *(pending)* |
> | `GATE_EXIT` | 0 | *(pending)* | *(pending)* |
>
> Do not quote a run that exited non-zero, and do not quote a working-tree ratchet measurement.
> **If Task 34's gate is not green, every `closes at merge` below becomes `do not close`** — there is
> no honest closure on a red gate, because a new failure cannot be told from a standing one.

### Summary

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

**The `Closes` line Task 36 should write:** `Closes #20, #22, #23, #24, #25`.
**Omitted, with the handoff naming where each is handled:** `#21`, `#26`, `#27`.

---

### #20 — Agent engine: turn & thread state-machine correctness

**Definition of done** (`issue_20.md:56`):

| # | clause | grade | evidence |
|---|---|---|---|
| 1 | "each PR ships a test that covers its defect line and fails against the pre-fix code" | **met** | `closure-20` measured **67 failed / 157 passed** across `tests/unit/{test_simulation_logic,test_thread_not_found,test_message_log,test_roster_sync}.py` with HEAD's tests overlaid on a `git archive 18ba52c` tree, `tests/unit/test_mentions.py` uncollectable at base, **231 passed** at HEAD. Each of this plan's #20 code tasks recorded its own red-first: `9cbfc00` (parked threads), `1c75d70` + `94792af` (E6(2), COR-11 bound), `c8263e7` (COR-10(3) — 6 failed / 1 passed pre-fix, failure text in `task-7.md`), `141023f` (COR-5), `45d1198` (COR-13), `9505554` + `b913130` (blocker 5), `f43e80e` (the third `rating != -1` reader) |
| 2 | "the offline gate now enforces `COV_MIN=60` with a `src/` lint ratchet" | **met**, conditional on Task 34 | `scripts/ci.sh` `COV_MIN=60`, `SRC_LINT_MAX=260`, `MYPY_MAX=150`; baseline green at `64e9981` (`baseline.md`). `simulation.py` branch coverage 45.54 % → 65.40 % on `tests/unit` alone |
| 3 | "this issue's tests should be written against the #30/#31/#32 stack" | **met** | base `copi-prod` @ `18ba52c` contains `f7b6f7a`, `f94d2a8`, `e292cbf` |

**Disposition: `closes at merge`.** All three of `closure-20`'s "blockers to Closes #20" that were
unimplemented `Fix:` clauses are now implemented, and both of its claim-accuracy blockers are closed:

| `closure-20` blocker | closed by | evidence a reader can check |
|---|---|---|
| 1. COR-13's "a fresh reply budget each restart" | Task 10, `45d1198` | commit subject `fix(agent): a reopened thread's reply budget survives a restart (#20 COR-13)` |
| 2. COR-10(3)'s cursor-ordering clause | Task 7, `c8263e7` (+ the `0029` column in `f0284f9`) | `agent_messages.pi_inbound_state`; seven tests in `TestPollInboundFromDbGuardsTheHandler`, 6 red pre-fix, and the one that passes pre-fix is the deliberate NULL-fallback control, paired with a red twin (`task-7.md`) |
| 3. COR-5's persistence half for `AgentRegistry.user_id IS NULL` | Tasks 8 + 9, `f0284f9` + `141023f` | `thread_decisions.pi_engaged_at` in `0029`; read gated on the agent having no linked PI (`task-9.md` Ruling 1) |
| 4. COR-5's reader consequence | **not fixed — carve-out**, see below | `task-9.md` "Reader audit" |
| 5. the `/admin/agents` review count | Tasks 16 + 29, `9505554`, `b913130`, `f43e80e` | `task-16.md`; `task-29.md` §Addendum — all three `rating != -1` readers now exclude the reopen marker |
| 6. the gate was red at `93a48e7` | `a47b6c5`, before this plan | `baseline.md` |

**Carve-outs the closing comment must state.**

1. **COR-5's named reader consequence is still true, by design** (`closure-20` blocker 4, plan
   `CL20-4`). The issue's text says the defect makes "dashboard/email keep showing 'unreviewed'".
   That symptom survives the fix: the engine's implicit engagement is recorded as
   `thread_decisions.pi_engaged_at` (and, for linked PIs, a `rating=-1` `ProposalReview`), and
   **every** reader deliberately excludes both — `src/main.py:182`, `src/routers/admin.py:864`,
   `src/routers/agent_page.py:255`, `src/services/email_notifications.py:178,854`. A PI who engaged
   without an explicit 1-4 rating still sees the proposal as awaiting review. This is Decision D6,
   and it is test-pinned; it is not a gap left by accident (`task-9.md`).
2. **COR-5's carrier is a substitution against the issue's literal wording** (`task-8.md`). The
   clause says "insert a `ProposalReview`"; the shipped carrier is
   `thread_decisions.pi_engaged_at`, because `proposal_reviews.user_id` is
   `ondelete="CASCADE"` to `users` — the issue's literal instruction would have built a
   **self-erasing** record that a PI deletion wipes. The behaviour asked for (the rebuild does not
   re-block after a restart) is delivered; the row shape is not the one the issue names.
   **The comment must name migration `0029`.**
3. **COR-10(3) is closed by re-ordering *plus* a new durable marker** (`task-7.md`), not by
   re-ordering alone: the log entry's presence *was* the dedup key and `MessageLog.append` is not
   idempotent, so the clause is unsatisfiable without a distinct key. `0029` again.
4. **Task 5's ruling — the two-strike post back-off was kept, not removed** (option (c)), and a
   parked thread is instead made invisible to `_non_funding_thread_count` and `_agent_load`, so it
   can no longer push an agent into `blocked_for_regular` for a whole run. Two residuals: a parked
   thread is **never closed** (with a silent counterpart it stays `status="active"` until the run
   ends), and `post_failure_count` is in-memory only, so a restart un-parks every parked thread.
5. **Task 16's count rescoping was an extra fix to a real production bug**, not one of #20's `Fix:`
   clauses: 12 of 53 active agents were mis-counted, `wiseman` by 89. Say so plainly.
6. **Task 16's accepted cost:** `[Reopened] …` guidance no longer renders on `/admin/discussions` at
   all (it previously rendered under a false `0/4` badge). Still durable in
   `proposal_reviews.comment`, in the private channel and on the PI's dashboard. → **F26**.
7. **Task 35's ruling on `ANTHROPIC_API_KEY`** decides whether four live-tier behaviours (full-run
   bijection, the 4000-character split, SIGTERM/restart durability, the whole PI-DM path) are
   covered only with a mocked LLM. If the key is not exported, #20's comment must say so — that
   silence is what hid COR-1b.

**Not blockers, recorded so nobody re-opens them:** the COR-2 restart residual (the `Fix:` line names
Phase-3 activation, which is done), the in-process-only dead-thread tombstone, E6(1)'s ≤2× retry
overshoot, COR-9b's missing-target fallback, COR-6's data-dependent cursor. Follow-ups **F1**, **F2**,
**F3**.

---

### #21 — Worker & background jobs

**Definition of done** (`issue_21.md:55`):

| # | clause | grade | evidence |
|---|---|---|---|
| 1 | "each PR ships a test that fails against the pre-fix code" | **met** | `closure-21`, measured per sub-PR against a pre-fix `src/`: V11 → 2 collection `ImportError`s; V2 → 5 failures in `tests/integration/test_worker.py` with COR-17's rollback independently mutant-killed; V3 → 16 failures in `tests/unit/test_email_inbound_hardening.py` + a wholesale error on `tests/integration/test_email_inbound_reply_paths.py`, C1 mutant-killed; V4 → 3+3+4+1 failures. This plan added `d7ce1a5` (COR-19.6), `e700dac` (COR-32), `1aeaf0a` (V4-3), `cb045b4` (V4-4), `391e545` (the offline migration path), each red-first |
| 2 | "This issue takes `worker/main.py` from 0 % coverage" | **met with a stated carve-out — the clause's premise is false** | `closure-21` measured `18ba52c` (pre-fix) at **71.83 %** branch coverage (114 stmts / 30 missed) and HEAD at **77.72 %** (162 / 33). `tests/integration/test_worker.py` landed in `d732804` (2026-07-30), an ancestor of `18ba52c`, three days after the issue's verification point; the 2026-08-11 re-verification never refreshed the sentence. `pyproject.toml`'s `[tool.coverage.run]` has `source=["src"]` and no `omit`, so **no configuration exists under which that file reported 0 %** |
| 3 | "V11 and V3 additionally add their prerequisites to `docs/inbound-email.md`'s bring-up checklist" | **met** | re-read at HEAD: `docs/inbound-email.md` step 4 carries the V11 writer-slot prerequisite (`c2228cf`) **and** a four-item V3 prerequisite paragraph — quarantine after `MAX_S3_PROCESS_ATTEMPTS`, the unknown-charset fallback in `_decode_part`, `_coerce_rating`, and the paginated S3 listing (`34d3c15`). `closure-21` graded this **NOT MET** at `93a48e7`; it is met now |

**Disposition: `close by hand with a stated carve-out`.** Off the `Closes` line. Two reasons, and
neither is a doubt about the work:

- clause 2 is **met by declaring its premise false**. A `Closes #21` that fires on merge publishes
  "DoD met" with no correction attached, and a future reader comparing the issue's "from 0 %
  coverage" against `git log` would reasonably conclude either the measurement or the claim was
  fabricated. The correction has to travel *with* the close.
- the residual orphan-channel window carries an **operator procedure** (below) that must be run once
  before `ENABLE_INBOUND_EMAIL` is turned on. An auto-close loses it.

**Carve-outs the closing comment must state.**

1. **The "from 0 % coverage" clause was never satisfiable as written.** Quote the two measurements
   (71.83 % → 77.72 %) and `d732804`. Do **not** repeat the 0 % figure anywhere.
2. **COR-19.6's residual window is knowingly left open — Task 12 Step 6 ruled option (b)**, and this
   overturned nothing less than the plan's own preferred alternative. After `391e545` (the offline
   migration commits its own rows) and `d7ce1a5` (the reopen route reads `refined_in_channel`), a
   duplicate private channel now requires `migrate_public_thread_to_private`'s own `db.commit()` to
   fail in the instant *after* Slack created the channel and accepted the handover. That failure
   leaves **no DB row at all**, so no code-side guard can see it. **Option (a) — drop the timestamp
   suffix from the channel name so Slack's `name_taken` becomes the idempotency guard — was rejected
   on two independent measurements:**
   - `_build_slug` (`src/services/private_channels.py:70-84`) is `priv-{sorted agent ids}-{origin
     channel}` and **carries no proposal identity**. The production copy already runs two legitimate
     same-slug channels — `priv-lairson-su-drug-repurposing` (`C0AUCMNCEFQ`, April) and
     `priv-lairson-su-drug-repurposing-20260616-180113` (`C0BB48ETLQL`, June) — refining two
     *different* proposals. **111** (pair, origin-channel) groups on the copy hold more than one
     proposal, covering **407 of 1017** `thread_decisions`, the largest group holding 39. Under (a)
     the June reopen would have resolved to the April channel's name and dropped one PI's guidance
     into another proposal's conversation.
   - as literally scoped, (a) does not even produce idempotency: `create_private_channel` **does not
     adopt on `name_taken`** — it retries with random entropy (`slack_client.py:946-952`) and returns
     `None` when attempts are exhausted. Its sibling `create_channel` adopts; this one deliberately
     does not. So (a) converts "mints a second channel" into "the reopen fails with no channel".
3. **The orphan-sweep procedure** (`task-12.md` §3), verbatim into the comment: run it after any
   reopen that returned a 500 or a terminal "couldn't reopen" e-mail, and **once before enabling
   inbound e-mail**. DB side `SELECT channel_id FROM agent_channels WHERE
   visibility='collab_private';` vs Slack side `conversations.list(types="private_channel")` filtered
   to `priv-*`; an orphan is a Slack channel whose id is not in the DB set; remediate by
   `conversations.archive` after confirming the same proposal has a live channel via
   `thread_decisions.refined_in_channel`. **Do not delete DB rows — there are none.**
4. **Closing the window properly needs a durable claim row committed before the Slack
   `conversations.create`, reconciled after it returns.** New design work, deliberately not
   improvised. Follow-up.
5. **CL21-3's substantive half is fixed** — `specs/local-db-conversations.md:65-73` now says six
   writer slots and names `REMEDIATION_WRITER_SLOT = 99`. Only a *test's name* still says five:
   `tests/unit/test_ids.py:101::test_five_writer_slots_are_distinct_residues`.
6. **Task 14's 409 is a user-visible response-code change** shared with #24 (see below); it is
   reached from the same review path this issue's e-mail sweeps feed.

Follow-ups: **F4**, **F5**, **F6**, **F7**.

---

### #22 — Profile pipeline & write integrity

**Definition of done** (`issue_22.md:34`):

| # | clause | grade | evidence |
|---|---|---|---|
| 1 | "each PR ships a test that fails against the pre-fix code" | **met** | `closure-22`, per sub-PR: V1 → 4 contract failures with `3fd7625~1:orcid.py`, `'Role of ' != 'Role of TP53 in cancer'` with `.text` restored, 7 failures with the `isinstance` guards mutated out, 9 with `_as_list` mutated to identity, `IntegrityError` with the dedup removed; V6 → 4 + 3 + 3 + 2 + 2 failures across the presence gates, `synthesis_validated`, `atomic_write_text`, the seed fallback and the private-save `422`; C1 → `assert 1 == 2` on the version race. This plan added `63813dc` (COR-23 seed guard), `79cee44` (curated-list blanking), `f2870cf` (item 15, docs+script), `88c922b` (the word-range ruling, no code) |
| 2 | "including a migration test for the new unique constraint against a table that already contains duplicates" | **met** | `tests/integration/test_migration_0025.py` — provisions its own scratch DB, `alembic upgrade 0024`, **INSERTs real duplicates** into `publications`, then `alembic upgrade head` with the real tooling and asserts the collapse, the untouched NULL-pmid control, the COALESCE merge and a fresh `IntegrityError` naming `uq_publications_user_pmid`. **3 passed at HEAD, 3/3 failed against `git archive 18ba52c`** with `assert 2 == 1` |

**Disposition: `closes at merge`.** Every `Fix:` bullet in all three sub-PRs is implemented, both DoD
clauses are met literally, and all three of `closure-22`'s blockers are closed:

| `closure-22` blocker | state at HEAD | evidence |
|---|---|---|
| 1. `raw_abstracts_hash` written on the discard path | **fixed**, `8341b21` | `src/services/profile_pipeline.py:519-527` — the write is guarded by `if not synthesis_discarded:`, with the reasoning in the comment at `:447-456` |
| 2. COR-16's word-range disagreement half-fixed | **ruled deliberate**, Task 21, `88c922b` | see carve-out 1 |
| 3. COR-16's `IntegrityError` Note unimplemented | **fixed**, `42f03f4` | `_insert_publication_tolerating_conflict` (`profile_pipeline.py:778`) uses `.on_conflict_do_nothing(index_elements=["user_id","pmid"])` at `:821` |

**Carve-outs the closing comment must state.**

1. **The word-range divergence stays, deliberately (Task 21, option (a), no code change).** The
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
   forward-looking. Re-measured by re-fetching **all 4,508 rows** through the fixed parser:
   **896 rows still hold text that differs from PubMed's — 209 titles and 877 abstracts, 97 % of the
   differences being a strict prefix of the correct value**, i.e. truncation. A per-user pipeline
   re-run cannot close it: **84 of the 512 rows the corruption signature flags carry neither a PMID
   nor a DOI on their owner's current ORCID record**, including every row belonging to the twelve PIs
   whose ORCID works list is empty. `scripts/repair_publication_text.py --all` (dry run by default,
   re-runnable) repairs all 896; the deploy runbook carries it as an ordered post-deploy step
   (`f2870cf`). **This is not the Task 19 defect** — Task 19 is synthesis blanking curated lists on
   *write* (8 of 141 profiles with an empty `key_targets`); this is corrupted evidence on *read*.
   Two different columns, two different fixes; do not let a comment merge them.
3. **Task 19 fixed the LLM-response twin of COR-22's `Fix:` clause, not the clause itself.** An
   omitted or mistyped `keywords` / `key_targets` / `experimental_models` no longer blanks a curated
   column, on either the protected or the `synthesis_validated = False` path. **The three web save
   routes COR-22 names (`agent_page.py`, `profile.py`, `onboarding.py`) are a different fix and were
   not in Task 19's scope — do not report COR-22 as closed on the strength of it.**
4. **Item 35 / COR-24e stands, with the #29 linkage spelled out.** The pipeline's disk export can
   still land ahead of its DB commit. The issue's `Fix:` scopes the ordering fix to "the ONE
   remaining inversion" (the private-save route, closed by 22.10) and D28 recorded this as a
   follow-up — **but `AH4` made the pipeline an unconditional writer of
   `profiles/private/{agent_id}.md` and the agents read the disk copy**, so an export that lands
   before a rolled-back commit leaves the disk authoritative and wrong.

Follow-ups: **F9**, **F10**, **F11**.

---

### #23 — External clients: Slack SDK, GrantBot, FOA regexes, HTTP robustness

**Definition of done** (`issue_23.md:56`):

| # | clause | grade | evidence |
|---|---|---|---|
| 1 | "each PR ships a test that fails against the pre-fix code" | **met** | `closure-23` re-derived every RED by swapping one file into a scratch export: V7 → 5 failed with `18ba52c:slack_client.py`; V8 → `test_foa_pattern.py` uncollectable + 9 failed in `test_funding_rules.py` + 5 in `test_grantbot_selection.py` + 2 in `test_tools_budget.py` + 2 in `test_grantbot_token_routing.py` against `dd82c91~1`; V9 → 3 failed against `ec9853a~1`, whole `test_http_retry.py` unimportable at `18ba52c`. This plan added `554b139`, `962aa6c`, `df5c45b`, each red-first |
| 2 | "The regex fixes should be table-driven over the cases above" | **met** | `tests/unit/test_foa_pattern.py:8-19` parametrises **all six rows** of the issue's own divergence table plus a lowercase twin of each and 2-/4-digit-year variants; `tests/unit/test_funding_rules.py:53-57` (apostrophes, incl. the U+2019 form the issue says "passes"), `:118-144` (COR-28b's sentence **verbatim** plus the accepted false negative), `:225-244` (`@grantbot` / `@GRANTBOT` / `@SuBOT`) |

**Disposition: `closes at merge`.** `closure-23` recorded **no blockers**. The residuals it listed
have since been closed or ruled:

| `closure-23` residual | state at HEAD |
|---|---|
| R2 keyed NCBI paced at 8.33 req/s against a 10 req/s ceiling | **fixed** — Task 23 (`962aa6c`) originally paced at 9.52 req/s (0.105 s); `b982d53` (#23 COR-29, audit D5) retightened it to **8.93 req/s** (0.112 s, worst-case burst 9), because 0.105 s sat at exactly the 10 req/s ceiling (worst-case burst 10) with zero jitter margin — see `src/services/pubmed.py`'s `_NCBI_PACING_SECONDS` comment. The semaphore slot is released across backoff instead of held for the whole retry loop |
| R3 apostrophe class misses U+2018 / U+00B4 / U+FF07 | **fixed** — `554b139` |
| R4 `extract_foa_number` returns the matched case verbatim | **fixed** — `554b139` canonicalises |
| R5 the V10b log line has no assertion | **fixed** — Task 24 (`df5c45b`) asserts the whole message in `tests/unit/test_delegates.py`. **But R5's premise was wrong**: see carve-out 1 |
| R6 no behavioural retry test for `orcid.py` / `grants.py` | **fixed** — `df5c45b` |
| R7 `_NCBI_SEMAPHORES` loop-bound | **fixed** — Task 23 keys the semaphore per running loop via a `WeakKeyDictionary`; the `HAZARD` comment and the test-only per-test rebinding are gone |
| R8 `_execute_retrieve_abstract` / `_execute_retrieve_full_text` have no `src/` callers | **still dead code** — carve-out 2 |
| R9 `_bot_uid_map` docstring falsely says grantbot falls back to SuBot's token | **fixed** — verified at `src/agent/simulation.py:4864` ("The collision was real until `dd82c91`…"), landed in `141023f`, **not** in Task 10's commit as `task-24.md` expected. R9 is closed |
| R10 two wall-clock pacing tests are timing-based | **STATE** — 15× stress at HEAD, 0 failures; deterministic clock injection is a larger refactor than the risk warrants |
| R11 the badge-middleware health test failed at `93a48e7` | **fixed** — `a47b6c5` |

**Carve-outs the closing comment must state.**

1. **R5 was a grep artifact, not a coverage hole — say why it looked unmet.** The audit grepped for
   the full log line `"skipping delegate Slack-ID sync"`, while `d0d9b3d`'s own test
   (`tests/integration/test_agent_page.py::test_accepting_an_invitation_with_no_bot_token_skips_the_sync_and_logs`)
   asserts a **lowercased substring** of it. Reporting R5 as "a sub-PR shipped without a test" would
   be wrong in the issue comment (`task-24.md` §1).
2. **`_execute_retrieve_abstract` / `_execute_retrieve_full_text` are still dead code in `src/`.**
   Task 24 pinned the *shipped* path (`execute_tool`) rather than deleting them, because deleting
   them would also delete the subject of `tests/unit/test_retrieve_tools_authors.py`, a file Task 24
   did not own. Either delete both helpers and re-point that file at `execute_tool`, or keep them and
   accept two renderings of the same answer. **Open a follow-up.**
3. **The plan's justification for `closure-23` R7 was wrong in one factual detail, and the comment
   must not repeat it.** `src/agent/grantbot.py` does **not** import `src/agent/tools.py`, so the
   grantbot scheduler — the one long-running process calling `asyncio.run()` repeatedly — never
   touched the NCBI semaphores. Peak concurrent keyed NCBI calls in a real grantbot day: **0**. The
   hazard was structurally present but never reachable; it is now removed outright.
4. **The retry budget bounds a call, not the pipeline.** `convert_dois_to_pmids` still issues one
   NCBI call per unresolved DOI sequentially, so a sustained outage costs `180 s × DOI count` inside
   one profile job (down from `420 s × DOI count`). `worker/main.py`'s
   `JOB_STALE_PROCESSING_THRESHOLD_SECONDS = 3600` stopgap still stands; its "≈243 s per `_ncbi_get`"
   comment is now an over-estimate (120.5 s) — safe direction, left alone as it is another task's
   file. A loop-level budget is a follow-up.
5. **The deliberate trade:** under a sustained NCBI brownout a profile job now gives up on an
   individual lookup sooner, so a profile synthesized during an outage may carry fewer publications.
   Already possible after 4 attempts; likelier now, in exchange for a job that terminates.
6. **COR-28b's ack detector (D12).** The word-count threshold closes the filed false positive by
   introducing an unfiled false negative — a short but substantive reply reads as an ack. **10 words**
   (`_ACK_SUBSTANTIVE_WORD_COUNT`, `src/agent/funding_rules.py`) was a deliberate cut. State the trade
   with the measured number; a length-independent content test is larger than the clause asks.
7. **Task 22's widening beyond U+2019** — the issue names only that code point; the shipped class
   covers the remaining apostrophe forms. Declared, not silent.
8. **D10 is a rollout note, not a defect:** prod needs a dedicated GrantBot Slack token or GrantBot
   posts nothing — that is exactly what COR-26c's `Fix:` asked for. An `xoxb-`-shaped but dead token
   passes `is_valid_token`, so the preflight should assert `auth.test`, not just the prefix.

Follow-up: **F12**.

---

### #24 — Web request-path robustness

**Definition of done** (`issue_24.md:29`):

| # | clause | grade | evidence |
|---|---|---|---|
| 1 | "each PR ships a test that fails against the pre-fix code" | **met** | `closure-24` reconstructed the pre-fix tree: base `public.py` → 1 failure, base `agent_page.py` → 5, base `admin_provisioning.py` → 5, base `slack_provisioning.py` / no `retry_after.py` → 2 collection errors |
| 2 | "for V5, a concurrent-insert test" | **met** | `tests/integration/test_concurrent_inserts.py` (`1230419`) — **two independent connections**, a genuinely concurrent first review and a concurrent waitlist signup, in the in-repo pattern of `tests/integration/test_profile_version_race.py`. `closure-24` graded this **NOT MET** at `93a48e7`, when every V5 test was a fake `AsyncSession` raising a hand-constructed `IntegrityError`; that is no longer the state |
| 3 | "for C2, an assertion that the handler does not block the loop" | **met with a stated carve-out** | four thread-identity assertions prove the real `httpx.post` executes off the loop thread (`tests/unit/test_slack_provisioning.py:43-63,144-201`), mutation-confirmed: a wrapper that keeps its name but drops `asyncio.to_thread` fails them. **The letter-gap is the word *handler*:** the assertions sit one layer down, at the service functions the two `async def` routes await. That boundary is pinned by monkeypatch trip-wires on `ap.create_app_async` / `exchange_code_async` / `lookup_team_id_async` plus an `inspect.getsource` assertion, and both handlers are a bare `await <service>(…)` (`admin.py:994`, `:1027`). There is **no** route-level or loop-responsiveness test anywhere in `tests/` |

**Disposition: `closes at merge`.** `closure-24`'s single blocker (clause 2) is closed by `1230419`,
and its three non-blocking findings are all resolved:

| `closure-24` finding | state at HEAD |
|---|---|
| N1 — `reopen_proposal`'s recovery discarded the `AgentChannel` / `PrivateChannelMember` / handover rows, leaving a real Slack channel no bot ever polls | **fixed** — Task 11 (`391e545`) makes `migrate_public_thread_to_private` commit its own rows and keep the PI's guidance; Task 12 (`d7ce1a5`) makes the retry read `refined_in_channel` |
| N2 — `review_proposal`'s recovery used `.scalar_one()` → 500 on a non-uniqueness `IntegrityError` | **fixed** — code `7cc2ea3`, pinned `a5666b4`, and Task 14 (`0d1ebb2`) replaced the recovery arm's 302 with a 409 |
| N3 — no static guard against a blocking `httpx` call on an async route | **→ F7** |

**Carve-outs the closing comment must state.**

1. **V5's recovery arm in `review_proposal` now ends in a 409, not a 302**, when the `IntegrityError`
   was **not** the review-uniqueness conflict. This is a **user-visible response-code change** on
   `POST /agent/{agent_id}/proposals/{id}/review`, and `a5666b4`'s test was **amended rather than
   deleted** to record why (`task-14.md`). Option (b) — keep the 302, just stop retiring the
   notification — was rejected because the reminder is at best the next scheduled digest and is
   suppressed entirely for `email_notification_frequency='off'` or a paused mailbox, leaving the PI
   shown a success page for a write that did not happen.
2. **The matching `EmailNotification` is deliberately left `sent`**, so the reminder loop keeps
   chasing a proposal whose review was not persisted. `record_engagement` still runs and commits: the
   PI *did* act, and dropping it would count our own write failure against them and eventually
   downgrade their e-mail frequency.
3. **There is no matching change in `reopen_proposal`'s sibling arm.** Its recovery is real (it
   re-creates the PI's inbox guidance row on the Slack-off path, `d9f0797`), so its 302 is truthful.
   Task 14 deliberately did not touch it.
4. **Clause 3's letter-gap** (above) — state where the non-blocking assertions actually sit.
5. **Task 13 removed dead machinery rather than fixing it:** the `refined_channel_id` re-bind
   capture, its assignment in the `except IntegrityError` arm, and the "recovered by re-binding" log
   clause are gone; the `ThreadDecision` re-select stays with `scalar_one_or_none()` and a new
   purpose. No behaviour a PI or operator can observe changes beyond two log lines that now say
   something true.

Follow-ups: **F5**, **F6**, **F7**, **F16**.

---

### #25 — Data layer

**Definition of done** (`issue_25.md:36`):

| # | clause | grade | evidence |
|---|---|---|---|
| 1 | "D1 ships a test that deletes a PI who is a private-channel member (currently pinned as a violation by `test_db_contract.py:268-281`)" | **met** | `tests/integration/test_db_contract.py::test_dat1_deleting_pi_member_user_cascades_pcm_row` — the **same DAT-1 slot**, inverted in place, so the "pinned as a violation" assertion is gone rather than duplicated. Measured RED against the pre-fix DB state (FK back to `SET NULL`) with the exact `CheckViolationError` |
| 2 | "P1 ships an `alembic upgrade head` check in CI" | **met** | `scripts/ci.sh` runs `upgrade head` → `downgrade $MIGRATION_FLOOR` (default `0018`) → `upgrade head` against a throwaway `postgres:15` it provisions itself, on by default; plus the single-head / duplicate-revision-id check. `closure-25` re-ran the round trip by hand, clean |

**Disposition: `closes at merge`.** `closure-25` recorded **no blockers**; both DoD clauses are met
literally, and the two gaps its notes raised are closed by Task 25 (`cd0ed78`).

**Carve-outs the closing comment must state.**

1. **Self-service account deletion is now REFUSED while the user owns an active agent** — a
   behaviour change to a user-facing route, not only a data-integrity fix. Name the page the user
   sees and the remedy (deactivate or transfer the agent first). **The guard blocks 56 of 144 users
   and leaves self-service deletion intact for the other 88.** "Owns an active agent" means an
   `agents` row whose `user_id` is the caller and whose `status` is in `{'active','pending'}`;
   `inactive` deliberately does **not** block (otherwise the refusal page's own remedy would be
   unreachable), `suspended` does not block, and a delegation never blocks.
   `pending` blocks because `admin_update_agent` promotes a pending row straight to `active` without
   re-reading `user_id`, and 2 of the 3 pending rows on the production copy already carry a
   `slack_bot_token`.
2. **D1 removed an accidental guardrail, and the cascade is real.** Re-measured on the disposable
   copy, each `DELETE` rolled back: deleting `Andrew I. Su` (owner of the **active** `su` agent)
   removes **153 publications and 63 proposal_reviews** via `ondelete="CASCADE"` and leaves
   `agents.agent_id='su', status=active, user_id IS NULL` via `SET NULL`. Those FK rules are
   pre-existing; what the branch changed is that `0026` removed the `CheckViolation` that had made
   the delete impossible. That is **what #25 D1 asked for**, so D1 is closed — but the closing
   comment must record the orphaned-active-agent and cascaded-review-history consequence, and that
   neither delete route renders a confirmation banner (#25 M6).
3. **P2's `passive_deletes=True` is now pinned by a test**, since the backlog-wide DoD clause was
   previously unmet for P2 — `closure-25` note 2 measured that *nothing* in `tests/` would notice the
   flag being removed. Three tests in `test_db_contract.py` section 7 now cover it: a mapper walk
   over all 11 `delete-orphan` relationships with the exact name set as an anti-vacuity control; a
   schema check that each of the 11 child FKs is `confdeltype='c'` (the other half of P2's `Fix:`
   clause); and a behavioural test that a user delete emits **no SQL against `publications`** yet the
   rows are gone. Red proved against `git show 32c4ca3~1:src/models/user.py` overlaid on a clean
   export.
4. **P3's `pool_pre_ping` on the app engine is untouched**, and must stay that way on the *health
   probe* engine — see #27's carve-out 3.

Follow-up: **F16**.

---

### #26 — Documentation

**Definition of done** (`issue_26.md:31`):

| # | clause | grade | evidence |
|---|---|---|---|
| 1 | "DOC-5's template fix ships with a rendering assertion" | **met** | `tests/unit/test_base_html_posthog.py` does not grep the source: it builds the app's own `Jinja2Templates`, **renders** `base.html` with a fake `posthog_api_key` and `current_user`, regex-extracts the emitted `<script>`, asserts balanced `{}`/`()`, runs `node --check` on the emitted JS, and in a second test executes it under `node` with a stub `posthog` and compares the captured `name`/`email` byte-for-byte. Re-confirmed RED against `git show 18ba52c:templates/base.html`: `posthog.identify('…', {name: '…', email: '…');` → `SyntaxError: Unexpected token ')'`. `node v20.20.0` present, so the strongest assertions are live, not skipped |
| 2 | "DOC-7 is verified by following the runbook end to end on a workspace with legacy rows" | **NOT MET — and structurally impossible before merge** | see below |

**Disposition: `closes after deploy verification`. #26 must NOT be on the `Closes` line.**

Clause 2 is operational, not test-shaped. It cannot be satisfied pre-merge and it cannot be
satisfied against the local copy of production:

- `scripts/backfill_slack_ts.py` **has no offline mode**. It aborts with exit 1 if `get_any_bot_token`
  returns `None` (`:97-107`) and issues one live `conversations.replies` / `conversations.history`
  call **per candidate row** in *both* report and `--apply` mode (`:114-121`).
- The disposable copy's tokens were rewritten to `xoxb-neutralized-local-copy`, which
  `is_valid_token` accepts **on prefix** — so running it there would fire real HTTPS requests at
  slack.com (barred), all failing auth, yielding `confirmed=0 db_origin=0 unverified=N`, exit 2, and
  `--apply` writing **zero** rows. That exercises the candidate query and the exit-code contract and
  demonstrates none of the repair.
- Even a different real workspace would not do: the verification *is* "does Slack recognise **these**
  timestamps".
- **The script has no `--help`** — its CLI is literally `"--apply" in sys.argv`
  (`scripts/backfill_slack_ts.py:157`), so `--help` is **not** a safe no-op probe; it goes straight to
  a DB connect. **Do not claim the script was tested locally.**

**Row counts, both stated because they disagree and the disagreement is unexplained:** **23** rows
with `slack_ts IS NULL` on the `copi` copy at the 2026-09-04T08:00:03Z snapshot (all 23 are real
candidates for the script's `SELECT`); `audit-phase8-migration.md` reports **28** on `copi_replay`
(24 candidates). **Re-count on the live host before acting and treat the live count as
authoritative.**

**The verification procedure Task 33 must write out** (`closure-26`, plan Task 33 Step 4): on the
prod host, count the NULL rows; run the dry run; read its `confirmed / db_origin / unverified`
triple; run `--apply`; confirm `count(*) where slack_ts is null` dropped by exactly `confirmed`;
confirm a reply into one of the repaired threads actually lands on Slack; attach the whole transcript
to #26's closing comment. Nothing short of that is "end to end on a workspace with legacy rows". **If
it fails, do not close** — comment with the failure and open a follow-up naming the clause.

**Carve-outs the closing comment must state.**

1. **The `test_prompt_hygiene.py` assertions — measured, and more precisely than the plan states.**
   The file has three assertions across two tests. Evaluated against
   `git show <rev>:specs/email-proposal-review.md`:

   | revision | `assert "…| LLM prompt for classifying" not in spec` | `assert "email_inbound.py" in spec` | `assert not re.search(r"email_inbound\.py:\d+(-\d+)?", spec)` |
   |---|---|---|---|
   | `18ba52c` (copi-prod tip) | **red** | **green** | **green** |
   | `e038528~1` (parent of test 1's fix) | red | red | green |
   | `c09f29f~1` (parent of test 2's fix) | green | green | **red** |
   | `HEAD` | green | green | green |

   So **exactly two of the three assertions are already green at `18ba52c`** — the plan's and
   `closure-26`'s finding is correct as an *assertion*-level statement, and
   `test_email_proposal_review_spec_has_no_stale_line_range_citation` is green **as a whole test** at
   `18ba52c`, i.e. it cannot fail against `copi-prod`'s code. The honest refinement a closing comment
   must carry: each test **is** red against the parent of the commit whose change it pins, because
   the stale line range was introduced *on this branch* by `e038528` and removed by `c09f29f`. They
   are legitimate guards; they are not regression proofs against pre-branch code. **Nothing in this
   plan changed them** (`git log 64e9981..HEAD -- tests/unit/test_prompt_hygiene.py` → 0 commits).
   → **F14**, and #26's comment must disclose it.
2. **`closure-26` blockers 3, 4 and 5 are fixed** by `34d3c15` — the `AGENT.md` lab counts (residual
   → **F8**), the missing `-e PYTHONPATH=/app` in the CLAUDE.md/README DOC-7 paragraphs (verified at
   `CLAUDE.md:139` and `README.md:97`), and A9's "daily" digest imprecision.
3. **Blocker 2 — A14 has no work at HEAD** and must be added to the PR body's "What NOT" list for
   #26 (Task 36 Step 2): `audit_recipient_list` still has zero `src/` call sites. Note the mitigating
   fact: the issue's "dead" framing is itself wrong — `prompts/daily_audit.md` is a host-cron Claude
   prompt whose consumer is out-of-band by design.
4. **DOC-B's fix is forward-only.** **2,211** `agent_messages` rows on real production carry
   `agent_id IS NULL` with a raw Slack uid as `sender_name` and stay invisible to every gated agent.
   No backfill ships; #26's text does not ask for one. → **F13**.
5. **The web-vs-script bot-name divergence persists** and `agents.bot_name` has no unique constraint.
   → **F15**.
6. **#26 R19** — a doc test asserts on the content of the 1.2 MB implementation plan. Brittle,
   accepted; narrow it if you are already in that file.

---

### #27 — Deploy, CI & coverage gate

**Definition of done** (`issue_27.md:25`, the clause that governs **every** issue in this backlog):

| # | clause | grade | evidence |
|---|---|---|---|
| 1 | "each PR ships a test that covers its defect line and fails against the pre-fix code" | **met with a stated carve-out** | `closure-27` reconstructed the pre-fix tree for `.dockerignore`, `Dockerfile`, `pyproject.toml`, all three compose files, `nginx/nginx.conf`, `scripts/ci.sh` and `src/main.py`: I1 → 5 failed and pre-fix `ci.sh` contains 0 occurrences of "mypy"; I2 → 4 + 2 failed; I3 → 1 + 5 + 2; I4 → 8; I5 → 12 + 4. Aggregate **37 failed / 7 passed** plus 5 plus 4; **56 passed** at HEAD. **Carve-out:** "covers its defect line" is satisfied literally only for I2, whose tests drive the real route through ASGI. I1/I3/I4/I5's defect lines live in shell and YAML/conf, so most of their tests are text/structure assertions — the correct analogue, and I1 and I4 also carry real subprocess tests |
| — | "#20, #21 and #23 touch the largest low-coverage files and therefore carry most of the ratchet" | *descriptive, not a deliverable* | borne out: `simulation.py` 45.54 % → 65.40 %, `src/worker` 71.83 % → 77.72 % |

**Disposition: `close by hand with a stated carve-out`.** Off the `Closes` line — not because the
work is in doubt, but because **#27's own definition of done is the gate**, and two of the figures a
truthful closing comment must quote are produced *after* every implementation task:

- **Task 34** owns the final gate figures. Until they exist, any statement about #27 is a
  prediction.
- **Task 35** owns the live Slack tier's first run, its skip accounting, the `ANTHROPIC_API_KEY`
  spend ruling, the `live_api` ruling and the isolation audit's verdict — and, per `task-2.md`, its
  transcript is the **first evidence that the preflight's check 3 passes** rather than merely
  refuses.

All three of `closure-27`'s blockers are closed: the stale `requirements.lock` (`f9541ba` +
`a47b6c5`; the baseline gate at `64e9981` reports the lockfile consistent), the CVE floors
(`jinja2>=3.1.6`, `python-multipart>=0.0.31`, `authlib>=1.7.1`, with the OSV provenance in
`pyproject.toml:14-16`) and the pre-1.0 caps (`uvicorn[standard]`, `httpx`, `asyncpg`, `typer`,
`python-multipart`, `anthropic` all `<1.0.0`).

**Carve-outs the closing comment must state.**

1. **D17 / I1-g — there is still no server-side CI, and no `.github/` workflow shipped.** The
   issue's **body** scopes this out conditionally and the condition was met; its **title** ("no
   GitHub Actions") does not. Say so explicitly, or the close reads as an overclaim.
2. **The mypy ceiling's number, provenance and slack (Task 27).** `MYPY_MAX` has never moved off
   **150**. The measured count fell **147 → 145 → 140 → 138**, each a real fix, none silenced with a
   `type: ignore`. The provenance block in `scripts/ci.sh` records **138 measured at `45d1198` with
   mypy 2.3.1**, on a clean `git archive` export — `b215244` re-recorded it after `bcef8be`'s 145-at-
   `9cbfc00` went stale, which is exactly the auditability defect the block exists to prevent. Slack
   is 12 (8.7 %), looser than `SRC_LINT_MAX`'s 3.6 %, and the comment says why: tightening a ratchet
   inside the change that closes 36 tasks would put an unrelated red in front of a reviewer; **145
   (slack 7) is recorded as the right post-merge value.** Carry the correction that v1's "the ceiling
   drifts with PyPI" diagnosis was **wrong** — every move it has ever made was caused by this
   repository's own code, mypy is version-capped at `>=2.3,<2.4`, and **no dependency pinning was
   needed and none was added**.
3. **The health probe: `pool_pre_ping=True` was measured and REJECTED, overturning the plan's own
   Step 3.** Four variants were built and measured in one `docker pause` window against the
   disposable copy, each as its own uvicorn from its own `git archive` export:

   | | reaped pooled conn | probe vs frozen Postgres | first probe after the outage |
   |---|---|---|---|
   | A HEAD (`pool_pre_ping=False`) | **503** ← the defect | 503 in 5.004 s | 503, then 200 |
   | B `pool_pre_ping=True` (the plan's Step 3) | 200 | **no response at all** (>15 s, measured to 25.0 s) | 200 |
   | C B + the whole `async with` bounded | 200 | 503 in 5.004 s | 503 (pool timeout), then 200 |
   | **D shipped** — retry once on `DBAPIError.connection_invalidated` | 200 | 503 in 5.005 s | **200** |

   B fails because SQLAlchemy runs the pre-ping inside `engine.connect()`, *outside* the probe's
   `asyncio.wait_for`, and asyncpg's `command_timeout` cancel handshake opens a **second**
   connection to the same frozen server and blocks there — **which is phase-8 audit C2 verbatim**,
   the Critical this same task had to assert closed. **Anyone re-adding `pool_pre_ping` to the probe
   engine must first re-run the pause experiment.** Cost was never the reason: pre-ping measured
   ~1.3 ms, comfortably inside `HEALTH_PROBE_TIMEOUT_SECONDS = 5.0`.
4. **phase-8 C2 is CLOSED by measurement, not by inference** — previously the closure was only
   *inferred*. Against a paused Postgres every probe answered: 503 in 5.004 s first, 3.00 s
   (`pool_timeout`) while the first probe's connection is still held, 0.004 s once the pooled socket
   is known dead. C2's accumulation claim no longer applies to the request pool at all — the probe
   has its own engine capped at one connection. **Residual, stated rather than hidden:** during an
   outage that one connection is held by an orphaned greenlet, so probes 2..n 503 on `pool_timeout`
   instead of on the database — correct verdict, different reason.
5. **The live Slack tier is now runnable only through `scripts/run_live_slack.sh`**, which refuses to
   start unless `scripts/live_slack_preflight.py` proves, in one process, that the four production
   credentials are **present and empty** (empty overrides `.env`; absent falls through to it —
   measured), that **no** usable production bot token resolves through `Settings` (**grantbot
   included**: `Settings.get_slack_tokens()` is a hand-written dict that omits it — 125
   `slack_bot_token_*` fields, 124 mapping entries — so `production_token_map()` widens it, and
   narrowing that back re-opens a hole straight into the production workspace), that every fixture
   token resolves via Slack's own `auth.test` to team **`T0BMVSBMEC8`**, and that the tier's own
   environment is complete so its 61 tests cannot silently skip. 13 of 13 isolation mutants killed.
   **Limit:** the accept path has never been exercised against a real workspace — **Task 35's
   transcript is the first evidence that check 3 passes** rather than merely refuses.
6. **Task 30c's ruling on the prompts mount.** `./prompts:/app/prompts` was removed from `worker`,
   reverting `3354904`'s widening of a mount #27 I5 had itself called a defect, along with the test
   that pinned it. **The same mount remains on `app`, `agent` and `grantbot`, and is still a
   defect** — left deliberately, because removing it changes deployed behaviour and belongs with the
   runbook. State the tension with **D33**: D33 forbids any change under `prompts/`, while the mount
   exists precisely so an operator can change `prompts/` with no rebuild, no review and no gate.
   Under D33 the mount has no remaining benefit and one cost — an un-gated write path into
   model-facing text on three production services, invisible to `scripts/ci.sh`. Removing the last
   three is a follow-up.
7. **The container memory figures, and the one post-deploy check that remains.** `agent`'s **768m**
   is no longer unmeasured: peak **226 MiB** over a full 53-agent `--reset-cursors` sweep and
   **185 MiB** steady over 318 turns, against a clone of the production database with Slack off and
   the LLM faked — 3.4× headroom, flat after the second sweep. Kept at 768m; **not** uncapped,
   because uncapping hands the choice of OOM victim to the kernel on a 3.7 GB host whose
   largest-RSS candidate is the uncapped `postgres`. The one term the offline measurement cannot
   cover is `_rebuild_state_from_slack`. **On the first live `agent-run` after this deploy, take one
   `docker stats --no-stream` during a turn and compare against 226 MiB; above ~500 MiB the cap must
   be revisited.** nginx's **128m** is unchanged but now *correct* rather than coincidentally
   survivable: `nginx.conf` declared **90 MiB** of shared zones into a 128 MiB cgroup; the zones are
   sized to their populations and total **37 MiB**, and a test derives the sum from the file. The
   seven-service sum is unchanged at 2432 MiB.
8. **The gate now announces the tests it did not run** (`af25113`) — 103 gated tests, so a silent
   skip is no longer possible.
9. **The process fix this branch exists for:** v1 of the plan wrote six rulings into
   `.superpowers/`, which `.gitignore:122` ignores, so `sdd-commit` (`git commit --only`) could not
   commit them and **no post-merge reader could ever see them**. All rulings for this plan are
   tracked under `docs/plans/2026-09-04-decisions/`.
10. **STATE, unchanged:** I5-e (`postgres` stays uncapped, D24); verify-deploy 3c (the `ci.sh`
    text-grep maintenance tax → **F21**); D1's cascade cost (`user: "0:0"`, `--via-run`);
    **phase-8 I4 — an evidence caveat on every "measured on the production copy" number in this
    plan**, because those measurements ran against a copy whose `llm_call_logs` was empty (1,509 MB
    of a 1,548 MB database).
11. **RC-5 and RC-6, closed in the 2026-09-08 audit fix wave.** I5's CSP was inert
    (Report-Only with no `report-uri`/`report-to`) and its own routine-deploy path
    (I2) could serve old code against a migrated schema on a running stack. Both are
    now closed, each with its own review-caught correction:
    - **RC-5.** An enforcing `Content-Security-Policy` header (`frame-ancestors 'none';
      base-uri 'self'; object-src 'none'`) ships on all three vhosts; the existing
      Report-Only header keeps its policy and gains `report-uri /api/csp-report`
      (`POST /api/csp-report` in `src/main.py`, public, 8 KB cap, 415 on an
      unsupported content-type, logged fields truncated to 200 chars with `\n`/`\r`
      stripped before logging). `form-action` was dropped from the ENFORCING header
      after review: Chromium/Firefox apply it to the redirect a form POST's response
      returns, and the admin Provision button POSTs then 302s to Slack's OAuth
      consent screen (`templates/admin/agent_detail.html:107-129` →
      `src/routers/admin.py:998-1023` → `src/services/admin_provisioning.py:216`) --
      an enforced `form-action 'self'` would have broken provisioning outright.
      `form-action` remains in the Report-Only policy.
    - **RC-6.** `scripts/redeploy.sh` builds `migrate`+`app`+`worker`, stops
      `app`/`worker` (`-t 30`), runs `migrate` and reads its exit code via `docker
      wait <container id>` (NOT `docker compose wait migrate`, which was tried first
      and, per review, races an already-exited one-shot: measured at >=0.3s to fail
      with "no containers for project" instead of reporting the exit code), starts
      the new `app`/`worker` only once migrate is verified to have exited 0, polls
      the new `app` container's Docker health status (bounded, 120s) before
      declaring success, then reloads nginx. Current scope is `app`/`worker`;
      `grantbot` still relies on `depends_on` ordering, called out in
      `docs/production-migration.md` §10.1 as the same insufficient guarantee on a
      running stack.

Follow-ups: **F17**, **F18**, **F19**, **F20**, **F21**.

---

### Rulings that overturned the plan's own recommendation

Recorded together because a reader who only sees the outcome will assume the plan was followed.

| ruling | the plan recommended | what shipped, and why |
|---|---|---|
| **Task 12 Step 6 — CL21-2** | option (a): drop the per-call timestamp suffix so Slack's `name_taken` becomes the idempotency guard | **option (b)** — leave the window, state it, ship an orphan-sweep procedure. `_build_slug` carries **no proposal identity**; the production copy already runs two legitimate same-slug channels for two different proposals, and 111 (pair, origin-channel) groups covering 407 of 1017 `thread_decisions` could collide. And `create_private_channel` **does not adopt on `name_taken`** — it retries with entropy and returns `None`, so (a) would convert "mints a second channel" into "the reopen fails" |
| **Task 29 Step 3 — the health probe** | `pool_pre_ping=True` | **rejected on measurement** — it **re-opens phase-8 C2**, the Critical the same task had to assert closed: the probe never answered against a paused Postgres (>15 s, 25.0 s). Shipped instead: retry once on `DBAPIError.connection_invalidated`, engine kwargs unchanged |
| **Task 5 — COR-1b's parked threads** | close the thread via `_close_thread(agent, thread, "timeout")` (option (b)) | **option (c)** — keep parking, exclude parked threads from both load accountants. `outcome` is a PostgreSQL enum (`Enum("proposal","no_proposal","timeout", name="thread_outcome_enum")`) with no truthful value for an undeliverable thread, and a close would DM the PI the literal sentence *"Thread with {other} in #{channel} timed out (reached message limit without a conclusion)"* about a thread that may hold two messages — plus write that false conclusion into **both** agents' prompt-fed working memory, which is how issue #29's six-week memory-poisoning chain started |
| Task 27 — the mypy ceiling | pin the dev extras against PyPI drift | **declined, deliberately** — the bisect shows the ceiling has only ever moved on this repository's own code; mypy is already capped at `>=2.3,<2.4`. Recorded as a considered no, not an oversight |
| Task 19 — where the curated-list fallback lives | (unspecified between the scripts and the pipeline) | **`apply_synthesis`**, one canonical implementation shared by all five callers, rather than four copies in `scripts/` (two of them in files Task 19 did not own) that would still leave the pipeline blanking |
| Task 24 — R5 | fix "a sub-PR that shipped without a test" | **the premise was wrong** — `d0d9b3d` shipped a behavioural test in the same commit; the audit's grep was for the full log line while the test asserts a lowercased substring |

### Follow-up issues to open

**From the plan's `## Named follow-up issues` (F1-F21)** — copy that table verbatim into the handoff;
Task 33 Step 8 turns each into a `gh issue create` line. Every one of them is a *disposition*, not a
silent drop.

**Found during execution, beyond the plan's list**
(`followups_found_during_execution.md`):

| id | title | source |
|---|---|---|
| **F22** | `SimulationEngine.start()` re-fetches the FOA cache serially on a cold start — 251 rows, ~3.5 s of `http_retry` backoff each with no network (~15 min of startup sleeping before turn 1), 251 sequential `api.grants.gov` fetches with it. Cold-cache cost only (`data/` is bind-mounted in prod), but serial and unbounded in count | Task 30's measurement harness |
| **F25** | `agent_page.py:255` still files a reopen sentinel under the dashboard's "Reviewed Proposals" heading. The *rendering* is fixed (Task 16 added a "Reopened with guidance" branch), so no false score is shown, but the categorisation is wrong | Task 16, accepted cost |
| **F26** | `[Reopened] …` guidance no longer renders on `/admin/discussions` at all — accepted cost of Task 16's predicate change (a misleading `0/4` badge is worse than an absent line) | `task-16.md` |
| *(also open a follow-up)* | `_execute_retrieve_abstract` / `_execute_retrieve_full_text` are dead code in `src/` whose only callers are `tests/unit/test_retrieve_tools_authors.py` — either delete both and re-point that file at `execute_tool`, or keep them and accept two renderings of the same answer | `task-24.md` §2 |
| *(also open a follow-up)* | A durable claim row committed **before** `conversations.create` and reconciled after it, to close CL21-2's residual window properly | `task-12.md` §4 |
| *(also open a follow-up)* | Remove `./prompts:/app/prompts` from the remaining three services (`app`, `agent`, `grantbot`) | `task-30.md` |
| *(also open a follow-up)* | A loop-level NCBI budget for `convert_dois_to_pmids`, which is still one sequential call per unresolved DOI | `task-23.md` §3 |
| *(also open a follow-up)* | A truthful terminal state for an undeliverable thread — needs a new `thread_outcome_enum` value and a migration; not part of #20 | `task-5.md` |

**Routed to Task 33, NOT follow-ups** — these are deploy-surface defects that Task 33 lands as part
of the closure/deploy handoff. If Task 33 does not land them, they become follow-ups **and** they
block the deploy:

| id | defect, verified at HEAD | consequence |
|---|---|---|
| **F23** | `scripts/migrate/run_migration.sh:73` is still `TARGET="0028"` | a bare `--apply` **under-migrates** and silently skips `0029`, which carries `thread_decisions.pi_engaged_at` (#20 COR-5) and `agent_messages.pi_inbound_state` (#20 COR-10(3)). Also needs `tests/unit/test_run_migration_sh.py:73,195` and `docs/production-migration.md` |
| **F24** | `scripts/migrate/preflight.py:91` — `SUPPORTED_START_REVISIONS = ("0018","0019","0020","0021","0023","0024")` | a start from `0028` is neither in the list nor equal to the target, so the preflight **refuses**. Harmless for this deploy (prod is at `0024`) but wrong for any re-run after `0028` lands |

### One note for whoever maintains this directory

`baseline.md`'s trailing stub still says *"`## Closure dispositions` — appended by Task 32"*, and
this README's `## Files that are not task-<N>.md` section says the same. Task 32's own Step 4 says to
write the section **here**, which is what happened. `baseline.md`'s stub is therefore stale and
should be replaced with a pointer to this section; it is not Task 32's file to edit.
