# Adversarial audit OF the remediation plan — 2026-08-22

Three independent read-only audits attacked `docs/plans/2026-08-22-correctness-remediation.md`
before anyone executed it: one on Phase 2 (engine/migration), one on Phase 1
(llm/specialists/web/backfill), one on completeness and the deferral judgements. Everything was
re-derived from source, the live database, the deployed container, or live probes.

**Verdict: the plan was NOT safe to execute.** Four instructions were actively harmful, one was
unimplementable, and eight more were insufficient or misaimed. Fourteen of its factual claims were
wrong or stale. The plan file has been revised for the harmful items; this document records what
was found and why, so the corrections are not mistaken for the original reasoning.

---

## 1. Harmful — would have caused damage if implemented as written

### H-1. A2.4's `thread_id`-keyed DELETE would delete the verdict it just wrote, on every supersession
The largest destructive risk found. `_capture_hub_assessment` reads `superseded` **before**
`_persist_assessment` writes the replacement, and calls `_retire_superseded_verdict` **after**
(`simulation.py:2968-3025`). Today the DELETE is keyed `slack_ts == superseded.slack_ts`, which is
the one field that differs between the two rows. Re-key it on `thread_id` — as the plan instructed
— and the replacement (same run, same `agent_id`, same `thread_id`, already committed) falls inside
the predicate. **The interview ends with zero assessments and the log reports success**
("removed 2 superseded assessment row(s)").

Corrected: have `_persist_assessment` return the new row's id and delete
`WHERE thread_id = X AND id <> new_id`, or keep `slack_ts` as the key and use `thread_id` only to
narrow. Do not reorder retire-before-persist — the existing "never leave the interview with
neither" comment is right.

### H-2. C1.3's non-ASCII tokeniser would convert noisy prior art into fake novelty
My premise was simply wrong. `Qβ malaria epitope` does **not** lose its most specific term —
`_Q_TOKEN` keeps the ASCII `Q`, producing `['Q','malaria','epitope']`, and `_salience('Q')` ranks it
FIRST, so the narrowest tier is a one-letter title search (`inventionTitle:(Q)` → live count
**1,862**). The damage today is 10 arbitrary "Q" filings offered as adjacent art, not a dropped term.

Widening the character class makes it worse, because **ODP cannot represent non-ASCII at all** —
probed live from the app container: `inventionTitle:(β)`, `(Qβ)`, `("Qβ")` all return **HTTP 404**,
and `patents.py:392-393` maps 404 → `([], 0)` = "searched, matched nothing". So every tier would
404 and the hub would be told "No US filings matched this query" — exactly what CLAUDE.md forbids
("An empty title search is never FTO").

The prescribed mechanism does not work either: NFKC does **not** fold U+2010/U+2011/U+2013/U+2014 to
ASCII `-`, so the measured `LOX‑1` case stays broken. And widening the class risks the injection
invariant `patents.py:45-47` exists to hold (a leading `-` is ODP's NOT operator), pinned by
`test_patents.py:572`.

Corrected: **transliterate, don't widen** — map `α→alpha, β→beta, γ→gamma …` and NFKD-strip
combining marks *before* tokenising, keeping the class ASCII (`inventionTitle:(beta)` → live count
13,640; `(alpha AND estrogen)` → 112). Use an explicit `str.translate` table for the dash range.
Do **not** redefine `total_terms` as the whitespace count — `TFEB / TFE3 fusion` would then report
`broadened=True` on a search that was never broadened. Add a separate `dropped_or_rewritten` field.

### H-3. E1.1's Origin guard would break RFC 8058 one-click unsubscribe in production
`email_notifications.py:437` and `:616` set `List-Unsubscribe-Post: List-Unsubscribe=One-Click`, and
`settings.py:195` serves the matching `POST /unsubscribe/{token}`. Those POSTs are issued
**server-side by Gmail/Apple/Yahoo** with no `Origin` and no `Referer`. My "exempt nothing by path,
or the exemption becomes the hole" instruction 403s every one of them — breaking unsubscribe and
bulk-sender compliance. `auth.py:35-40` already carries that path as a non-browser exemption, so the
precedent was in the tree.

Two further errors in the same task: my middleware-ordering trap was **self-contradictory**
(Starlette prepends, so "added last" runs *outermost* — the opposite of what I wrote), and the test
`Origin` cannot be hardcoded to `http://testserver` because `.env` on the host sets
`BASE_URL=https://blackbird.copi.science`, so the suite would pass locally and 403 everything on the
host. Also: I called this the plan's largest blast radius; it is **three lines**
(`tests/conftest.py:100-125` plus two files that build their own client).

### H-4. A3.4's unconditional per-row fallback would blow the shutdown grace period
The commonest flush failure is not a poison row — it is the pool-checkout timeout
`_persist_assessment`'s own comment names. On that error a per-row fallback issues N sequential
checkouts, each waiting the pool timeout; ~15 rows at 30 s exceeds `docker stop`'s 420 s inside
`stop()` and gets SIGKILLed, losing the batch *plus* everything not yet flushed. Gate the fallback
on row-specific errors (`IntegrityError`, `DataError`) and/or a wall-clock deadline — never on bare
`Exception`. Also: the failed session **cannot** be reused (the `except` sits outside the
`async with`, so it is already closed and rolled back); use a new session with `begin_nested()` per
row.

---

## 2. Unimplementable

### U-1. A2.2 has nothing to filter on
`specialist_consults` has **no truncation column**, and `tools.py:679-690` passes the *same* kwargs
to `on_consult_record` for a refused consult as for a good one — so the stored row is
byte-indistinguishable. Migration 0036 as specified adds no such column, so "filter the SELECT on
the truncated marker" cannot be written, and C1.2's stated purpose ("so WS-A can exclude it") is
unsatisfiable across the whole plan. `specialist_consults.truncated` must be a **fourth** DDL item
in 0036, with `_record_specialist_consult` widened to persist it.

Worse, C1.2 as scoped is a **no-op**: the `⚠️ caution` note it aims to suppress is posted by
`SimulationEngine._post_panel_note`, in a file WS-C does not own, whose signature ends in
`**_withheld` — so a new `truncated=True` field is silently absorbed and the note posts anyway.

---

## 3. Wrong

### W-1. B1.5 should not change the loop
Max observed tool rounds over all 1,121 rows carrying `call_stats` is **4** against a budget of 6, and
no caller ever passes `max_tool_rounds`. So `range(max_tool_rounds)` removes headroom production has
never used and gains nothing measurable. And my trap instruction was backwards: seven tests use
`max_tool_rounds=1` as *setup* to force a two-round turn (`test_llm_call_stats.py:265` asserts
`["round","round","forced_final"]`), so "its expectation is part of the bug" would have **deleted the
only multi-round coverage**. Fix the docstring, the warning string, `llm.py:81`,
`agent_activity.py:194` and CLAUDE.md's arithmetic instead. Zero behaviour change.

### W-2. A3.7's fix double-counts
My premise was half-true. `Agent.record_api_call` already books **six** sites including specialist
consults (`tools.py:559`) and truncation retries (`on_retry`, three sites). Only the extra *tool
rounds* are unbooked, so "book `len(call_stats)` entries" double-books every retry and, at the two
reserved sites, the reservation too. Use the `kind` discriminator `call_stats` already carries. And
the rebuild must use `COALESCE(jsonb_array_length(call_stats), 1)` — **4,650 of 5,771 rows have
`call_stats IS NULL`** (the column arrived in 0032), so a bare `jsonb_array_length` collapses the
lifetime rebuild and *loosens* the throttle. Note also this fix requires editing `llm.py`, which the
plan assigns to Phase-1 WS-B while A3.7 is Phase-2 lead-owned — an unresolved ownership collision.

### W-3. B1.1 violates a standing ruling in this repo, and `+= None` raises
Both cache fields are `Optional[int]` defaulting to `None` on the deployed SDK, so the literal
instruction is a `TypeError` inside a billed turn. And `agent_activity.py:198-210` explicitly rules:
*"Do NOT 'fix' this column by summing it — the numbers already in the table would then mean two
different things depending on when they were written"* — with `wall_ms`'s comment citing that rule
as the reason 0035 added a column instead. My plan does the ruled-against thing without mentioning
it. Preferred: add `cache_read_input_tokens` / `cache_creation_input_tokens` **columns** in 0036.
Also `test_llm_call_stats.py:462` pins `input_tokens == sum(call_stats input_tokens)` and becomes
vacuous unless updated.

---

## 4. Insufficient or misaimed

- **A1.1 ordering trap (data loss).** The milestone repair and the generic JSONB normalisation are in
  one bullet, and `derisking_milestones` is one of the normalised columns. Normalise first and the
  repair matches **zero** rows while reporting success — all 17 milestones lost. The repair MUST
  precede the normalisation. Also: `tests/integration/test_db_contract.py:268` deliberately pins the
  FK's *current* broken behaviour and must be inverted, not deleted; and the model's `ondelete`
  (`agent_activity.py:337`) must change with the DDL or 0036 creates the drift it is fixing.
- **A1.2 undercounts.** It is **11 of 13** nullable JSON columns lacking `none_as_null`, not "nine
  remaining" — the audit's own figure was also wrong. The two nobody enumerated
  (`researcher_profiles.pending_profile`, `.user_submitted_texts`, plus
  `cohort_audit_events.topology`) would keep mixed encodings forever. Enumerate from
  `Base.metadata`, not from prose.
- **A1.3's branch order re-arms the bug it fixes.** Leaving `if not panel_is_owed(...): return
  "not_owed"` ahead of the `panel_checked` test means a row stamped `panel_checked=False` under
  *today's* rules falls through to `verified` the next time `panel_is_owed` widens — and it has
  widened twice this month. Make the stored column the sole authority for green and delete
  `panel_is_owed` from the read path. Name it `panel_owed` (a durable fact) rather than
  `panel_checked` (ambiguous with the unverifiable case). Also: four existing assertions **invert**
  (`test_panel_state.py:46,47,77,109`) plus `test_assessment_detail_page.py:776`; ~45 hand-rolled
  test constructions leave the column NULL; and the proposed copy "predates panel tracking" will be
  false for every post-0036 row that lands NULL.
- **A1.3 fixes one of three surfaces.** The assessments **list** page
  (`_assessments_body.html:288-297`) and the run-level banner (`directory.py:335-337`) still gate on
  `panel_incomplete` alone, so the same 12 rows look unremarkable one click away — on both admin and
  **manager** templates. This was flagged in the *earlier* audit and never fixed.
- **A3.1's named traps are misaimed.** `--fresh` never deletes `thread_decisions`, so the two
  cross-run reads I flagged are unaffected by removing the wipe. The read that actually blocks
  "delete nothing" is one I never named: `_sync_private_channels_from_db`'s select
  (`simulation.py:2365`) has **no `simulation_run_id` filter**, so a fresh run would discover every
  previous run's private channels, join its bots to them, and — via A3.6's `"0"` cursor — re-ingest
  their whole Slack back catalogue. Scoping that select is a hard prerequisite. (Latent today: 0
  `collab_private` rows.) Also `pi_dm_messages` is a dead table — nothing writes it.
- **A3.3's gather is in the wrong place.** `stop()`'s first act is `_drain_memory_events`, which
  makes real LLM calls, which can spawn a *new* flush task — so gathering "at the top" reintroduces
  the orphan. Gather after `set_call_log_callback(None)` and before the final `_flush_llm_logs()`,
  with `return_exceptions=True`.
- **A3.8's guard creates a new inconsistency.** `_record` uses `thread_ts is None` — not the
  `_by_thread` insertion — to populate the top-level indexes, so guarding only the first branch
  leaves a self-parented entry in **neither** index: no longer double-counted, but invisible to
  `get_new_top_level_posts` and therefore to Phase 3. Normalise instead: when
  `entry.thread_ts == entry.ts`, treat it as a root.
- **B1.2 is incomplete and unsafe at one site.** It misses site 1008 (the tool-round call) and the
  `_execute_tool_blocks` raise, which discard a whole multi-round turn's record — one
  function-level guard closes the class instead of three point patches. At site 1176
  "fall through with the text already accumulated" is wrong: `response_text` is not yet bound and
  `message` still points at the last tool round, so falling through double-counts its tokens and
  mislabels it `forced_final`. Combined with B1.5 it is a `NameError`.
- **B1.6's sizing advice is the hazard.** "Sized from the intended fan-out" computes to ~32
  concurrent 300 s API calls on a 2-vCPU host. The accidental 6-thread executor is a **load-bearing
  throttle** — `_acreate` has no semaphore and nothing retries 429s beyond the SDK's two. Use a
  modest fixed pool **plus an explicit semaphore**. The benefit is also larger than I stated: every
  Slack call goes through `to_thread` too, so 8 gathered consults can starve the pollers.
- **C1.5, C1.7, E1.2, E1.4, F1.3** each need a smaller correction: raise a dedicated
  `PubMedParseError` rather than change the parser's return contract; use base classes
  (`TimeoutException, NetworkError, RemoteProtocolError`) rather than 8 leaf names;
  `session.clear()` destroys the `pending_access` the `/access-pending` page renders, producing a
  dead end; closing `/docs` breaks `test_reachability.py:111-115`'s allowlist; and the backfill also
  bypasses `_str_or_none`, which is the same "one bad value loses every row" failure F1.3 exists to
  fix.

---

## 5. Factual claims that were wrong or stale

| Claim in the plan/audit | Actual |
|---|---|
| 57 of **63** assessments dangling | 57 of **64** — the snapshot missed run 6fb83501's last verdict |
| 109 of 141 rows with impossible input tokens | **184 of 228** once the run finished (80.7%) |
| 23 refusal-with-text finals "across the two runs" | **14** across those two; 24 across all ten |
| 3 new agents took 100% of 2,000 draws | Mechanism real, figure misleading — the main loop anchors on every selection, so N unanchored agents monopolise N *consecutive* turns, then normalise |
| §2.11 "9 of 11" JSONB columns | **11 of 13** |
| "Two legitimate interviews with the same PI exist in prod" | **Five** duplicate (run, subject) pairs |
| ~21% of verdicts newly owed a panel | 13/64 = 20.3% ✓ — but *flagged* will be ~15-18%; owed ≠ flagged |
| `thread_id` NULL on all 63 historical rows | Vacuous — the column does not exist yet; it will be NULL on all **64** |
| A3.2's `continue` at `:886` | `:881`. Several other line citations drift — re-locate before editing |
| "the klein truncated-memory row **on disk**" | No longer on disk; it is the newest `profile_revisions` row, and disk now holds a *different* 1,388-byte body with no revision row at all |

Two claims survived exactly: **12** rows falsely "verified", and `private_channel_members` has 0
rows with the `SET NULL`-under-CHECK defect.

---

## 6. Deferral verdicts

- **Unique constraint on `(run, thread_id)`** — deferral honest, reason wrong. The real reason is
  that a unique violation raises inside `_persist_assessment`'s try (after the row commits) and
  inside the single batch commit, converting a duplicate into a **lost** verdict until A3.4 lands.
- **Re-running the floor over the 12 historical rows — RATIONALISATION for 7 of 13.** No messages
  are needed: 7 of the 13 are exactly computable from `raw_verdict` + `specialist_consults`, and the
  probe was run. Five have a demonstrable gap (markham `['chemistry']`, janak
  `['chemistry','commercial']`, green, pearce) and **two are demonstrably clean** (huganir,
  feinberg). Only run `88d81cd8`'s six are genuinely unknowable (`specialist_consults` postdates
  them). Rendering all 13 `unrecorded` withholds a truthful green from two and downgrades a provable
  gap on the corpus's highest-scoring verdict to a shrug.
- **Rubric threshold re-fit — honest.** All four original reasons still hold and §2.1 made the
  corpus worse.
- **Prompt divergence — honest conclusion, materially understated content.** The prompt does not
  merely omit the new rule, it states the **opposite** in three places:
  `phase4-thread-reply.md:83` ("`pass` and `route-to-incubation` verdicts require no panel at all" —
  the only two recommendations production has ever emitted), the mandatory list at `:68-79` omitting
  `commercial`/`budget` (and `commercial` already appears in a real gap), and `:63` +
  `thread_guidance.py:153` threatening that such a verdict "is refused and nothing is persisted",
  which is now false. `test_doc_prompt_sync.py` makes the edit a mechanical two-file change, not the
  snapshot minefield the plan implies.
- **"Impersonation is not read-only … non-corrupting" — RATIONALISATION.** Under impersonation
  `current_user` IS the PI and `profile.py:163-180` runs `db.delete(current_user)` with nine
  cascading tables, then clears `copi-impersonate` on the way out — proof the author knew. It is
  also one of the two CSRF targets §2.6 names.
- **"Three 500-on-bad-input items" — not judgeable.** They appear nowhere in the published audit.

---

## 7. A process finding: the evidence was never committed

Nine plan tasks cite section numbers (`§F2`, `§F8`, `§M5.7`, `llm-F5`) that exist in **no committed
file**; five more attributed to "§2.13" are not among its nine items; the two "HYPOTHESIS items" the
self-review names do not appear at all. The six per-strand audit reports were consumed into a
summary and discarded, unlike the 2026-08-17 audit (13 strand files committed) and 2026-08-21
(harnesses committed). Consequence: the plan's "every CONFIRMED finding maps to a task" cannot be
checked in the direction that matters, the harness outputs it quotes are not re-runnable, and a
deferred item nobody can read is indistinguishable from a dropped one.

Compounding: **there is no saved container log for 2026-08-22 09:30→16:35.**
`logs/blackbird_run_1787391032.log` ends at 09:29:59 and the next begins at 16:35:23, so run
8b64a0e0's last seven hours were never captured before `docker rm` — the same window whose
`agent_messages` §2.1 destroyed.

---

## 8. Findings dropped between the two audit documents

| Item | Status |
|---|---|
| **H1 — PI bots killing live interviews** | Instrumented (`closed_by_role`), never fixed, and **recurred**: 2 of 15 closes in run 6fb83501 were `pi_lab` with 0 verdicts — one of them `weeraratna`, the same PI whose verdict the previous run destroyed |
| **NCBI api_key in cleartext logs** | Open, key never rotated — **170 lines** in the newest log |
| **`--fresh` does not reset working memory** | Open; 57 files under `profiles/memory/`, and A3.1 rewrites `--fresh` semantics without revisiting it |
| **Contradictory/redundant consults** | Open and worse: 130 of 332 thread×domain pairs redundant, **30 contradictory** (was 9) |
| **`docker stop -t 420` vs the tail** | Open and worse: `timeout=300` × SDK 2 retries = ~900 s uninterruptible |
| **Nested-lab double counting** (`camacho`, `gordy`, `tripathi`) | Open; `gordy` produced 2 assessments in one run. The `shastri` half *was* executed |
| **Dimensions with no owning specialist** | 5 → 3 (`external_signals`, `dev_regulatory_feasibility`, `exit_thesis`) |
| **The real confidentiality invariant** (visible reply may describe the idea "only at the level the PI has already made public") | Open, unencoded, untested |

---

## 9. New CONFIRMED findings in subsystems no audit had covered

**Job worker / profile pipeline**
- **Every job-progress entry after step 4 is silently discarded; 10 of 10 prod rows are truncated.**
  `Job.payload` is plain `JSON`, not `MutableDict.as_mutable(JSON)`, and `update_progress` only
  reassigns on its first call. The PI-facing `unvalidated`/`ungrounded` explanations — written so a
  degraded profile is distinguishable — never reach the row the onboarding page renders.
- **A DB error strands a job in `processing` forever.** The error handler commits on the session the
  pipeline poisoned, so no status is written; `claim_job` only selects `pending`; there is no lease
  and no reaper. The PI's page then self-reloads every 5 s indefinitely.
- **The worker has no `stop_grace_period`** and all 10 prod jobs ran 17-74 s against Docker's 10 s
  default — so CLAUDE.md's own `up -d --build worker` line SIGKILLs mid-job into the orphan above.
- **`monthly_refresh` has no enqueue site anywhere**, while a degraded profile's explanation tells
  the reader the next monthly refresh will fix it.

**ORCID / PubMed ingest**
- **CRITICAL: an ORCID outage is recorded as "this researcher has no publications."**
  `orcid.py:102-109` swallows every HTTP/transport failure and returns `[]`, so
  `works_lookup_failed` can only ever be set by a *parse* error. The profile is then stamped
  `no_evidence_available`, documented as "nothing was lost and regenerating will not change it" —
  telling the operator not to retry the one profile that needs it. Two green tests jointly conceal
  it, one of which monkeypatches a raise the real function never emits.
- **Titles and abstracts truncate at the first `<i>`/`<sup>`/`<b>`.** `pubmed.py:282-293` uses
  `Element.text`. Live: PMID 38511961 → title 0 chars, abstract 0 of 2,667. Sample of 100 prod
  PMIDs: **36% of abstracts, 12% of titles**. Specified as D6 in the 2026-08-13 design and never
  implemented. Prod's stored rows are intact only because they came from an out-of-band backfill —
  so the committed pipeline is a **regression against the data in production** and re-inflicts it on
  every refresh.
- **A refresh silently replaces a good profile with a thinner one.** The anti-regression guard is
  `evidence_pub_count == 0` only. Live dry run: Davis 40→24 grounding abstracts, Chute 49→22 — both
  would overwrite, from one click or one `copi regenerate-profiles`.

**Cohort gate**
- **Activating an agent through the documented UI produces a structurally mute bot, and then blocks
  the next restart.** Prod runs `COHORT_DEFAULT_POLICY=isolated`; an agent with no
  `CohortMembership` gets an empty gate, so it may act on nobody and the hub can never interview it.
  `POST /admin/agents/{id}/approve` flips `status='active'` with no membership and no warning, and
  at the next start `_validate_star_topology` makes `start()` **raise** — the simulation refuses to
  boot. CLAUDE.md's "Adding New PIs" never mentions cohorts. All 62 current agents happen to have
  memberships; the next one added will not.
- **`cohort_memberships` holds 62 memberships for `grantbot`, which has no `agents` row** — a legacy
  id that is an allowed sender for every agent in the roster.

**Working memory**
- **Disk and DB have diverged and only a warning marks it.** `_update_agent_memory` writes the file
  first and wraps the `profile_revisions` insert in `try/except → logger.warning`. For `klein` the
  newest DB revision is the *truncated* one (09:00:58Z) while the file is a *different* complete
  1,388-byte body (mtime 15:07) with **no revision row at all**. So `profile_revisions` is not a
  complete history of what the agents actually read — and the cause is unprovable because the
  container log for that window was never saved.
