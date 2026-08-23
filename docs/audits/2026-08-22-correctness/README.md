# Adversarial correctness audit — whole project, 2026-08-22

Six independent read-only audits (assessment pipeline, LLM layer, specialist/evidence layer,
persistence/schema, concurrency/state, web/authorization) plus a lead strand. Every finding was
re-derived from source, from the live database, or from an executed probe — **no finding rests on
a prior audit document or on recollection**, per the instruction for this pass.

Baseline: `HEAD = f3171bb`. The running container's `src/` was verified byte-identical to the
working tree (`sha256sum` inside `blackbird-agent-run` vs host), so everything below applies to
production as deployed at 16:28Z.

Verdict labels: **CONFIRMED** = an executed query/probe/diff proves it. **LATENT** = the defect is
proven but the trigger has not occurred in production. **HYPOTHESIS** = not proven; labelled as such.

---

## 1. Regressions introduced or widened by today's deploy

These matter most: they are new, they are live, and four of them are mine.

### 1.1 CRITICAL — `/admin/assessments/<id>` now shows a green "panel verified" for 12 verdicts whose panel was never evaluated
Found independently by three of the six audits, and verified again by hand.

`src/services/assessment_detail.py:407-419` was changed today to ask
`panel_is_owed(recommendation, band)`. The **reader** moved to the band-aware predicate; the
**stored rows did not**. Every row written before 16:28Z came from a floor that keyed on
`recommendation` alone, so an exempt verdict stored `panel_incomplete=false,
missing_domains=NULL` — which on that row means *"no panel was owed"*. The new reader reads the
same NULL as *"the floor ran and found no gap"*.

```sql
SELECT count(*) FROM opportunity_assessments
WHERE panel_incomplete = false AND missing_domains IS NULL
  AND (band IN ('advance','conditional') OR recommendation <> 'pass');
-- 12
```

All 12 are the **funding-positive** verdicts — every `route-to-incubation` in the corpus, plus
the four `pass`-recommendation/`conditional`-band rows. Re-deriving each one's real gap from its
own `raw_verdict` against that run's `specialist_consults` shows at least three are not merely
unverified but **actually incomplete**: markham `['chemistry']`, janak
`['chemistry','commercial']`, green `['chemistry','clinical']`.

The worst single row is one **I** wrote: markham, run 8b64a0e0's only `route-to-incubation` and
the highest-scoring verdict in the corpus (3.04), backfilled by me at 16:35Z, now displaying a
green panel-verified box for a panel that never ran and would have failed on chemistry.

Root cause is that NULL carries two meanings and nothing distinguishes them. Minimal fix: a
`panel_checked` boolean written by `_persist_assessment`, or gate `"verified"` on evidence the
floor actually ran for that row.

### 1.2 HIGH — the `panel_is_owed` migration missed a fourth call site, and it is the restart-recovery path
`src/agent/simulation.py:3912` still reads `verdict.get("recommendation") not in
self._PANEL_REQUIRED_FOR` and returns early. `panel_is_owed`'s own docstring enumerates the three
sites that had to migrate; `_seed_consults_from_db` was not on that list and did not move.

Consequence, reproduced with a stubbed session: a `pass`-recommendation verdict scoring
`3.0/conditional` skips consult rehydration, so after a restart mid-interview
`_specialist_floor_gap` sees an empty consult set and stamps `panel_incomplete=true` with four
missing domains — **three of which are recorded as consulted in the database**. A false
accusation, on exactly the verdict class the new floor exists to protect, in exactly the scenario
`_seed_consults_from_db` exists to prevent.

### 1.3 HIGH — prompt caching silently corrupted every input-token number the system records
`usage.input_tokens` excludes cached input; the cached part arrives as
`cache_read_input_tokens` / `cache_creation_input_tokens`. Nothing in the repo reads those
(`grep -rn "cache_read\|cache_creation" src/ templates/ tests/` → no matches), so `_call_stat`
and the three `total_input_tokens +=` sites under-count by whatever the cache served.

```
avg input_tokens per API call, blackbird/thread_reply, same ~30 KB system prompt:
  8b64a0e0 (pre-caching)  344 calls  avg 24,396   min 17,699
  6fb83501 (post-caching)  63 calls  avg  3,597   min      2
rows recording FEWER input tokens than the system prompt alone can be: 109 of 141
```
One `call_stats` entry records **2** input tokens for a 30 KB prompt. `/admin/llm-calls`' token
tile now understates real input volume by ~5.9×, and any future sizing exercise done the way the
last three were — a `jsonb_array_elements(call_stats)` query — reads fiction. `tests/fakes.py`'s
`_Usage` has no cache fields, so no test can see it.

### 1.4 HIGH — `on_stop_reason` was built and then wired at only 1 of 4 call sites
`grep -rn on_stop_reason src/agent/simulation.py` → no matches. Only the specialist consult
(`src/agent/tools.py:629`) consumes it. So `thread_reply`, `new_post` and `memory` still take a
`stop_reason='refusal'` reply with partial text and post/persist it as complete — the three
damages the callback was written to prevent. 23 refusal-with-text finals exist across the two
runs; the klein truncated-memory row is one of them.

### 1.5 HIGH — the 300 s timeout made a pre-existing "lose the whole turn" path far more reachable
A raising retry or forced-final call (`llm.py:808`, `:1073`, `:1176`, `:1214`) is inside no
handler that recovers, so it discards a truncated-but-usable reply **and** never reaches
`_emit_call_log`. Executed: `rows written: 0` after 6 billed tool rounds. `APITimeoutError` is
SDK-retryable with `max_retries=2`, and `llm.py`'s own comment concedes a clamped retry needs
~351 s at the measured rate — i.e. it is *expected* to exceed 300 s. That path now costs three
billed generations, ~900 s, and then raises, losing the concluding hub reply and its sidecar.
Not yet observed in prod (0 timeout lines over 225 calls), so: mechanism CONFIRMED, trigger LATENT.

### 1.6 MEDIUM — my `last_selected` fix is incomplete: a mid-run roster add reintroduces the 10⁷ veto
`start()` anchors `last_selected` once over `self.agents`. `_sync_roster_from_db`'s add path builds
`Agent(...)` → `AgentState.last_selected = 0.0` and never anchors it. Measured on a harness:
3 new agents out of 13 took **100.0%** of 2,000 draws, weight ratio 1.79e9. With the documented
bulk case (48 bots provisioned mid-run) that is 48 consecutive monopolised turns.

### 1.7 MEDIUM — my two backfilled rows silently dropped 17 de-risking milestones
`scripts/backfill_dropped_verdicts.py:192` reads `derisking_milestones`; the sidecar contract key
is `suggested_derisking_milestones` (`phase4-thread-reply.md:214`), which the engine reads
correctly at `simulation.py:3378`. The two backfilled rows are precisely the two rows in the table
whose `derisking_milestones` is a JSON `null`, while their `raw_verdict` still holds 8 and 9
milestones respectively. Repairable from `raw_verdict`. The same script also bypasses
`_bounded_str` on four VARCHAR columns, bypasses `_normalize_gating`, sets `channel_name='unknown'`,
and keys idempotency on `(run, subject)` rather than the interview.

### 1.8 MEDIUM — supersession is the one path that deletes a verdict and keeps nothing
`_retire_superseded_verdict` DELETEs the earlier row and calls `_record_assessment_drop` **without**
`raw_verdict` — while the refusal path two hundred lines up passes it, under a comment saying a
refusal "is never a licence to destroy it". Today's gate change makes supersession the mechanism
that upholds the one-row invariant, so this is the path that will now run.

### 1.9 MEDIUM — `parse_opinion` defeats the recovery branch added for it today
`specialists.py:283` calls `extract_json(_strip_fence(raw))`. `json_extract.py:75-81` carries a
branch for the shape its own comment names ("Claude sometimes drops the opening brace inside the
fence") — but that branch tests for the fence, which `_strip_fence` has already removed. Executed:
a fenced brace-less `blocking`/`high` opinion parses correctly from `raw` and **raises** after
stripping, so `parse_opinion` returns `caution`/`low`/`()` — the exact laundering today's commit
was written to end. `extract_json(raw)` strictly dominates across 13 shapes. 0 of 489 production
consults hit it so far: LATENT.

---

## 2. Pre-existing defects confirmed this pass

### 2.1 CRITICAL — `--fresh` deletes **every** run's conversation record, not its own
`src/agent/main.py:174-176`: three unfiltered `DELETE`s. Measured now:

| table | distinct runs | rows |
|---|---|---|
| `llm_call_logs` | 10 | 5,693 |
| `opportunity_assessments` | 5 | 63 |
| **`agent_messages`** | **1** | **121** |

Run 8b64a0e0 held 1,354 messages this morning; it now holds **0**. **57 of 63 assessments have a
`slack_ts` that resolves to no message.** So the assessment detail page's interview timeline is
empty for 90% of the corpus, and any post-hoc audit of a past run is impossible — including the
one I ran this morning, whose verbatim quotes are no longer reproducible from the database. The
run I started this afternoon destroyed its own evidence base.

### 2.2 HIGH — the main loop's `continue` paths skip every buffer flush and the memory drain
`simulation.py:868` and `:886` both jump past the drain and all three flushes, which have no other
call site outside `stop()`. Harness output over 5 productive ticks:
`{'flush_persisted': 0, 'flush_llm': 0, 'flush_assess': 0, 'drain': 0}` with 5 rows stranded in
each buffer. Reachable when `_select_agent()` returns None while the reply lane did work — routine
on a small roster, and the documented exit path is SIGKILL.

### 2.3 HIGH — the background `_flush_llm_logs` task is cancelled mid-commit at shutdown
`_on_llm_call` spawns it with `create_task`; `_flush_llm_logs` removes the batch from the buffer
*before* awaiting the commit; `stop()` awaits nothing. Executed: `rows the fake DB saw = 10 |
'COMMITTED' present: False | task state: cancelled`. `_on_flush_done` then calls
`task.exception()` on a cancelled task and raises `CancelledError` inside the callback, so the
operator sees a traceback that says nothing about the lost rows.

### 2.4 HIGH — one poison row loses an entire flush batch, permanently at shutdown
All three flushers add N rows, commit once, and re-queue the whole batch on failure while logging
"re-queued for retry". `stop()` makes exactly one final attempt, so that message is false and the
loss is silent. `_flush_persisted` is the primary conversation store;
`_flush_pending_assessments` is the pipeline's product. `opportunity_assessments.channel_name`
`String(100)` is fed raw while its four siblings are clipped.

### 2.5 HIGH — the one-assessment-per-interview invariant is unenforceable and unauditable
No unique constraint on `opportunity_assessments` beyond the PK, and **no `thread_id` column**, so
the invariant cannot be expressed in SQL or checked afterwards. Enforcement is entirely the
process-local `_assessed_threads`, which is **never rehydrated** (`simulation.py:370`; no
equivalent of `_seed_consults_from_db`). Today's change stores provisional verdicts, widening the
restart window from milliseconds to tens of minutes. And `duplicate_thread_verdict` count across
the entire database is **0** — the supersession path the invariant now depends on has never once
executed in production.

*Correction to my own first reading:* I initially called the five duplicate `(run, subject)` pairs
five invariant violations. Four are a PI legitimately pitching different projects in different
threads, which the design supports. Only `hart` (two rows, same channel, both
`route-to-incubation`, 31 min apart, same IscREAM asset) is suspicious — and it is now
**undeterminable**, because §2.1 deleted the messages needed to resolve it.

### 2.6 MEDIUM — no CSRF protection, and `SameSite=Lax` does not isolate the co-hosted tenants
Zero `Origin`/`Referer`/CSRF checks in `src/`. The same nginx serves `blackbird.copi.science`,
`copi.science` (the *other* tenant) and `devel.copi.science`, which are same-site for cookie
purposes — so a page on either sibling can auto-submit a top-level POST with the victim's session
cookie attached. Reachable targets include `POST /profile/delete-account` and, against a
logged-in admin, `POST /admin/users/{id}/role`. `/docs`, `/redoc` and `/openapi.json` are
unauthenticated (90 paths disclosed), which is the reconnaissance half.

### 2.7 MEDIUM — revoking access does nothing to a live session
`get_current_user` reloads the user every request but never reads `access_status`; sessions are
unkeyed signed cookies with a 30-day `max_age` and no server-side store. Probed:
`denied-user GET /profile → 200`, `pending-user GET /profile → 200`. `POST /logout` only clears
the client cookie. No live victim today (all 66 prod users are `allowed`).

### 2.8 MEDIUM — `POST /profile/save` is the one PI-write POST missing `get_pi_user`
`profile.py:99-113` uses `get_current_user` while all four siblings use `get_pi_user`. Probed: a
manager POSTs it successfully and a `ResearcherProfile` is created. Not a path to a lab bot
(`request_agent` also needs `onboarding_complete`, written only by the gated route), so a
guard-consistency defect rather than an escalation — but it also lets a manager rewrite the
`email` that binds delegate-invitation acceptance.

### 2.9 MEDIUM — non-ASCII terms are silently deleted from prior-art queries
`_Q_TOKEN` is ASCII-only, and `total_terms` counts post-tokenisation, so a dropped term is
invisible to `broadened` and to `_scope_note`. Real production queries:
`Qβ malaria epitope` → `['malaria','epitope']`, reported as a non-broadened on-point search with
the single most specific term (a bacteriophage VLP) gone. `ERα` → `ER`. A U+2011 `LOX‑1` degrades
to `['LOX','1']`, and by this module's own live measurement a bare `1` matches 34,595 titles.

### 2.10 MEDIUM — `_seed_consults_from_db` launders refusal-truncated consults into a satisfied floor
`tools.py` deliberately records a truncated consult without crediting it; `simulation.py:3893-3906`
then asserts "a row exists only for a SUCCESSFUL consult … cannot turn an unreachable specialist
into a consulted one", which is now false. Reproduced: three never-consulted domains satisfy the
floor and flip `floor_armed` False→True.

### 2.11 MEDIUM — the JSONB "absent" value is double-encoded on 9 of 11 nullable JSON columns
Only `opportunity_assessments.missing_domains` and `llm_call_logs.call_stats` carry
`JSONB(none_as_null=True)`. Everything else stores Python `None` as JSONB `null`, so SQL
`IS NULL` is false for it. `assessment_drops.raw_verdict` now holds **both** encodings of the same
logical state (15 SQL NULL, 2 JSONB null), so the obvious operator query — "which refusals kept
their verdict?" — returns 2 rows, both of which kept nothing.
**No in-app SQL reader is currently wrong**: every reader is Python-side, where both encodings
decode to `None`. So this is latent-plus-operator-facing, not a live wrong answer. It recurred
because the only tests are per-column and cannot catch a new column.

### 2.12 MEDIUM — deleting any user who is a private-channel member raises
`private_channel_members.user_id` is `ON DELETE SET NULL` under a CHECK forbidding both owner
columns being NULL. Reproduced on a throwaway DB: the cascade's own UPDATE violates the CHECK, so
`POST /profile/delete-account` and the admin delete both 500. 0 rows in prod today.

### 2.13 Others confirmed, lower severity
`--fresh` cursor seeding cannot distinguish an empty channel from a failed fetch, so one failing
channel re-imports its whole back catalogue (harness: 30 messages ingested) · the sliding-window
limiter counts turns not API calls, under-counting hub spend ~2.5× · `max_tool_rounds` buys one
extra billed round · `MessageLog` returns a self-parented entry twice, doubling an interview's
turn budget (0 such rows in prod) · a `refusal`-truncated consult still posts `⚠️ caution` into
the PI's thread · `fetch_abstract` still claims "No PubMed record found" for a completed-but-
unparseable response · the whole per-channel Slack poll body is swallowed at DEBUG while prod runs
at INFO · `copi-impersonate` is set without `Secure` because `app.state.allow_http` is never set ·
impersonation is not read-only (`delete-account` runs as the impersonated user).

---

## 3. Verified clean (negative results worth recording)

Deadlock freedom, proved by inventory plus a 2,400-op randomized stress harness: acquisition order
is semaphore → thread → agent, one sorted order per registry, no cycle expressible; no lock or
refcount leaks even on a `None` key. Slack-ts string comparison is correct for every reachable
input (first divergence is 2286-11-20). The ts minter cannot collide with or precede restored
history. `ThreadState` counters are safe because the thread lock serialises turns and the tool
gather covers only consults. Model↔prod schema drift: **none** (314 vs 316 columns diffed; the
only unmapped column is the documented `users.is_admin`). Prod schema equals what the migration
chain produces, byte-identical. Migration 0035 is a clean single head and round-trips
`head→0034→head` and `head→0018→head`. All 41 FKs have an explicit `ON DELETE`. All 7 enums match
the models. No `String(N)` column is near its cap on a real write path. `weighted_score({})` is
never persisted as a real 0.00 on any of the five paths that compute it. `_HeldVerdict`'s new
field introduced no positional hazard (both construction sites use keywords), and
`final=True ⇒ announced=True` holds. `_cacheable_system` is lossless over 10 edge cases and never
exceeds 2 of the API's 4 breakpoints; no cross-agent cache leak is possible. Exactly one
`llm_call_logs` row and exactly one `on_stop_reason` fire per turn on all 7 paths. `wall_ms` is
defined on every path that reads it (141/141 rows populated; `wall_ms < latency_ms` on 0 rows).
The SDK-guard claim in the `NONSTREAMING_MAX_TOKENS` comment is accurate on the **deployed** SDK,
verified by execution. `extract_json` never returns a *wrong* object across 31 adversarial inputs —
ambiguous cases raise. `parse_opinion` never raises across 31 texts plus 8 non-`str` types.
`panel_is_owed` is total over 276 pairs and fails closed. The prior-art cache cannot collide, never
caches an unavailable result, resists caller mutation, and is bounded. A 404 is not retried on
either external API. Systematic IDOR sweep across all 99 routes with a second PI's session: no
unintended 200. The impersonation gate tests the *session* identity, so no chaining; `is_admin`
has no writer anywhere. Assessment confidentiality holds — `raw_verdict` renders in exactly one
admin-only template, `raw_opinion` is nulled in the *service* for non-admin views, and no
PI-facing template references `weighted_score`/`band`/`raw_verdict`. Open-redirect defence rejects
all 17 payloads. No f-string SQL anywhere. `agent_messages.content` contains 0 rows matching
`assessment_json` or `weighted_score`.

---

## 4. Convergence

Findings reached independently by more than one audit, which is the strongest signal here:
§1.1 by three (assessment, panel, schema) plus my own re-derivation · §1.2 by two · §1.7 by two ·
§1.8 by two · §2.1 by three (lead, assessment, concurrency) · §2.4 by three · §2.5 by three ·
§2.11 by three.

## 5. Nothing was changed

This pass was read-only by construction: no file edited, no database write, no container touched.
The live run `6fb83501` was observed only. Fixes above are stated as minimal diffs, not applied.
