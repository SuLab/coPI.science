# Run 8b64a0e0 Audit Remediation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to
> implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix every code defect found by the run-8b64a0e0 audit and its five-pass RCA, then
rebuild both images, redeploy, and start a fresh 1-hour run.

**Architecture:** Six workstreams partitioned by **file ownership** so they can run in parallel
without collision. `src/agent/simulation.py` is 6,700 lines and central to two workstreams, so
those two (A and E) are **serial with each other** and owned by the lead, not by subagents.
Everything crossing a module boundary is pinned by an explicit interface block below.

**Tech Stack:** Python 3.12, SQLAlchemy 2 async, Alembic, pytest (`.venv-test` on the host),
anthropic SDK 1.0.0 in the deployed image / 0.120.2 in `.venv-test`.

**Spec:** `docs/audits/2026-08-22-run-8b64a0e0/rca-and-corrections.md` (authoritative — it
corrects `README.md`, which is superseded in part). Read both.

## Global Constraints

- **Never run pytest through the sshfs mount.** Run on the host:
  `ssh ubuntu@ec2-3-21-33-147.us-east-2.compute.amazonaws.com "cd /home/ubuntu/blackbird-copi-science && .venv-test/bin/python -m pytest ... "`
- **Never `pip install` against `.venv-test` from the sshfs client.** Host only.
- `ruff check tests/` must stay at **zero** findings. `src/` has a ratcheted ceiling of **231**
  (currently 227) — do not exceed it.
- The full gate is `./scripts/ci.sh`. It must pass before deploy.
- **Do not edit `src/` while a pytest run is in flight** — `tests/unit/test_star_topology_validation.py`
  uses `inspect.getsource()` and fails spuriously when line numbers shift mid-run.
- `prompts/roles/*` and `src/agent/thread_guidance.py` string literals are byte-pinned by
  `tests/characterization/__snapshots__/test_agent_turn_gm.ambr` and `tests/unit/test_doc_prompt_sync.py`.
  **Do not reword them in this plan's scope.** Never run `pytest --snapshot-update`.
- Alembic head is **0034**. This plan adds exactly one migration, **0035**, and it is additive.
- Two stacks share the host. Always `-f docker-compose.prod.yml`. **Never `--remove-orphans`.**
  Never touch any container whose compose project is `copi-python`.

## Cross-Workstream Interfaces (pin these exactly)

```python
# src/services/llm.py — ADDITIVE keyword, default preserves today's behaviour.
# Both generate_agent_response and generate_with_tools accept it.
on_stop_reason: Callable[[str], None] | None = None
# Invoked exactly once per call, with the FINAL API call's stop_reason
# ("end_turn" | "tool_use" | "refusal" | "max_tokens" | ...), before returning.
# Never raises into the caller: wrap the invocation in try/except.

# src/services/patents.py
@dataclass
class PriorArtResult:
    ...                      # existing fields unchanged
    total_count: int | None = None   # ODP's own `count`; None when unknown

# src/agent/specialists.py
def parse_opinion(raw: str, *, domain: str) -> SpecialistOpinion   # signature unchanged
def required_domains_for(verdict: dict, *, band: str | None = None) -> set[str]
# `band` is the COMPUTED band; when given it participates in the gate alongside
# `recommendation`. Default None keeps every existing caller working.
```

## File Ownership (do not cross these lines)

| WS | Owns | Runs |
|---|---|---|
| A | `src/agent/simulation.py` (capture gate), `alembic/versions/0035_*`, `src/models/opportunity.py`, `scripts/backfill_dropped_verdicts.py` | lead, phase 2 |
| B | `src/services/llm.py`, `src/services/email_inbound.py` | subagent, phase 1 |
| C | `src/agent/specialists.py`, `src/agent/tools.py` | subagent, phase 1 |
| D | `src/services/patents.py`, `src/services/pubmed.py` | subagent, phase 1 |
| E | `src/agent/simulation.py` (scheduler/drain), `src/agent/state.py` | lead, phase 2 (after A) |
| F | `CLAUDE.md`, `.gitignore`, `src/main.py`, `src/routers/agent_page.py`, `src/agent/agent.py`, roster/DB ops | subagent, phase 1 |

---

## Phase 1 — parallel, disjoint files

### Task B1: `llm.py` — timeout, stop_reason contract, observability, caching

**Files:** Modify `src/services/llm.py`, `src/services/email_inbound.py`.
Test: `tests/unit/test_llm_stop_reason_contract.py` (create),
`tests/unit/test_llm_nonstreaming_ceiling.py` (extend).

**Interfaces:** Produces `on_stop_reason` (see above). Consumes nothing.

- [ ] **B1.1 — `timeout=300.0` on the shared client.** `_client_for_key` (`llm.py:74-82`).
  **Not 120**: the run's largest legitimate reply took 119.18 s and `max_tokens=16000` at the
  observed 60.7 tok/s authorises up to 264 s. 300 cut zero legitimate calls in the measured run.
  **Trap:** passing any `timeout=` makes `Messages.create` skip
  `_calculate_nonstreaming_timeout`, which is where the SDK raises on `max_tokens > 21_333`.
  `llm.py`'s own `NONSTREAMING_MAX_TOKENS` check in `_acreate` becomes the sole guard — extend
  `test_llm_nonstreaming_ceiling.py` to assert *that* guard still raises, and add a comment at
  the ceiling constant recording that SDK enforcement is now gone.
  Test: `test_the_shared_client_carries_a_300s_timeout`, and
  `test_acreate_still_raises_above_the_nonstreaming_ceiling`.

- [ ] **B1.2 — `email_inbound.py:342`.** `messages.create` runs **synchronously inside an
  `async def`** with no timeout: one stall freezes the inbound-email poller for up to 1,801.5 s.
  Wrap in `await asyncio.to_thread(...)` and route it through the same shared client so it
  inherits the 300 s timeout.
  Test: `test_classify_reply_does_not_block_the_event_loop` — patch the client with a
  0.2 s-blocking `create`, run `classify_reply` concurrently with a 0.01 s ticker, assert the
  ticker completed while the call was in flight.

- [ ] **B1.3 — `_call_stat` records `block_types`.** Add
  `"block_types": [getattr(b, "type", None) for b in (getattr(message, "content", None) or [])]`
  to the dict `_call_stat` already builds (`llm.py:210-219`), and include the same list in
  `_log_empty_reply`. This is the one-line change that settles the unresolved `end_turn`-with-no-text
  mechanism permanently. Read defensively — this is a logging path.
  Test: `test_call_stats_records_the_reply_block_types` — a fake reply whose content is
  `[thinking, redacted_thinking]` with `stop_reason="end_turn"` yields
  `call_stats[-1]["block_types"] == ["thinking", "redacted_thinking"]` and a `""` return.

- [ ] **B1.4 — log the row before the empty-content early return.** `generate_agent_response`
  returns `""` at `llm.py:425`, *before* `_call_log_callback` at `:503`, so 26 billed refused
  consults wrote no row at all this run. Move the callback above the early return.
  **Traps:** (a) do not also let the fall-through emit a second ERROR line — fold the existing
  rich `user_tail` diagnostic into one log call; (b) the empty-content branch is the only place
  `user_tail` is captured, so keep it; (c) this adds rows that the restart path replays into the
  rate limiter's `call_times` — correct direction, but state it in the docstring.
  Test: `test_an_empty_content_reply_still_writes_a_call_log_row` — assert the callback fired
  exactly once with `response_text == ""` and `call_stats[0]["stop_reason"] == "refusal"`, and
  `test_an_empty_content_reply_logs_exactly_one_error`.

- [ ] **B1.5 — `on_stop_reason` callback.** Add the keyword to both `generate_agent_response`
  and `generate_with_tools`, invoke once with the final call's stop_reason, wrapped so it can
  never raise into the caller. Purely additive; every existing call site keeps working.
  Test: `test_on_stop_reason_reports_a_refusal_that_kept_partial_text` — a reply with partial
  text and `stop_reason="refusal"` must invoke the callback with `"refusal"` **and still return
  the partial text** (the decision belongs to the call site, not here).

- [ ] **B1.6 — parallelise `consult_specialist` tool blocks only.** `llm.py:755` is a plain
  `for` over the round's blocks; 81% of rounds carried ≥2 blocks and ran serially. Partition
  `tool_use_blocks`, `asyncio.gather` **only** the `consult_specialist` ones, keep every other
  tool serial, and reassemble `tool_results` in original block order.
  **Traps — all four are why the other tools stay serial:** `ThreadState.abstracts_other`/
  `full_text` are check-then-increment (`tools.py:278/:292`) so concurrency bypasses the
  per-thread cap; `agent.record_api_call`'s deque is mutated per call; `_record_specialist_consult`
  must precede `_post_panel_note`; and `search_prior_art` already loses 10 of 125 searches to
  self-inflicted 429s, which parallelism would multiply.
  Test: `test_consult_blocks_run_concurrently_and_others_do_not` — a fake `tool_executor`
  sleeping 1 s per block, recording `(name, enter, exit)`; assert a 3-consult round finishes
  under 1.5 s, the three intervals overlap, a mixed round's non-consult blocks do **not**
  overlap, and `tool_results[i]["tool_use_id"] == tool_use_blocks[i].id` for all i.

- [ ] **B1.7 — prompt caching, 5-minute TTL.** One `cache_control: {"type": "ephemeral"}`
  breakpoint at the end of the system prompt's stable prefix and one after the last
  `tool_result`. Worth ~40% of input-token spend (5.18 M of 12.76 M tokens re-sent). **This is a
  cost fix, not a perf fix** — input tokens have no measurable latency effect (R²=0.53,
  coefficient indistinguishable from zero). Use the **default 5-minute TTL**: median hub turn
  inter-arrival is 58 s, and the extra hits a 1-hour TTL buys do not pay for doubling the write
  premium. The prefix is only cacheable because `Agent._compose_system_prompt` renders
  `## Your Working Memory` **last** — 90.4% of the hub prompt is a stable prefix.
  Test: `test_the_cached_prefix_is_identical_across_differing_working_memory` — pin the ordering
  invariant, so a future reorder of `_compose_system_prompt` fails loudly instead of silently
  dropping the hit rate to zero.

- [ ] **B1.8** Run `pytest tests/unit/ -q` on the host, then commit.

### Task C1: specialists + tools — parse, floor, coverage, alarm

**Files:** Modify `src/agent/specialists.py`, `src/agent/tools.py`.
Test: `tests/unit/test_specialists.py` (extend), `tests/unit/test_specialist_floor_band.py` (create).

**Interfaces:** Consumes `on_stop_reason` from B1.5. Produces `required_domains_for(..., band=)`.

- [ ] **C1.1 — tolerant opinion parsing.** 6 of 168 consults were laundered into
  `caution`/`low`/empty, one of them inverting a `blocking`/`high` opinion that was then
  published to Slack as `⚠️ caution`. Running the repo's own `llm.py::_extract_json` over them
  recovers **3 of 3** `end_turn` cases and fails on **3 of 3** `refusal` cases — a clean split.
  **Do not import `llm.py`**: `specialists.py`'s module docstring makes it deliberately
  dependency-free on the engine. Extract the algorithm into a pure shared helper (e.g.
  `src/services/json_extract.py`) and have both call it. `parse_opinion` must still **never
  raise** — wrap, because `_extract_json` raises `ValueError`.
  Keep `_DEFAULT_SIGNAL = "caution"`: `clear` would turn an unreadable specialist into an
  approval, which is the exact failure `has_usable_content` exists to prevent.
  Test: `test_json_with_trailing_prose_keeps_its_blocking_signal` (must yield `blocking`/`high`/
  1 concern — fails today with `caution`/`low`/empty); `test_a_fenced_object_with_trailing_prose_parses`;
  `test_a_truncated_object_still_falls_back_to_caution`.

- [ ] **C1.2 — record whether the signal was read or defaulted.** Add `parse_ok: bool` to
  `SpecialistOpinion` and a nullable `parse_ok` column is **not** needed — instead persist it
  into the existing `specialist_consults.confidence`? **No.** Add nothing to the DB in this
  workstream; instead have `parse_opinion` log a WARNING naming the domain when it falls back,
  so the 6 are greppable. (A column belongs in a later migration; 0035 is owned by WS-A and must
  stay minimal.)
  Test: `test_a_defaulted_signal_logs_a_warning_naming_the_domain`.

- [ ] **C1.3 — a refusal-truncated consult must not satisfy the floor.** Using B1.5's
  `on_stop_reason`, `_execute_consult_specialist` (`tools.py`) records the consult but does
  **not** call `on_consult` when the final stop_reason was `refusal`. Three markham consults
  were credited to the panel this run while contributing zero concerns and zero questions.
  Test: `test_a_refusal_truncated_consult_is_recorded_but_not_credited` — assert a
  `specialist_consults` write happened and the domain is absent from the consulted set.

- [ ] **C1.4 — gate the floor on `band` OR `recommendation`, and stop exempting
  `route-to-incubation`.** `_specialist_floor_gap` keys on the **model-written**
  `recommendation`, so a verdict that computes to band `conditional` while saying `pass` exempts
  itself from the panel. And `route-to-incubation` — **Blackbird's own positive outcome, the
  grant it exists to award** — is exempt on the rationale that "a decline costs Blackbird
  nothing", which is a category error. Add `band` to `required_domains_for` and to
  `PANEL_REQUIRED_FOR`'s effective test.
  **Trap:** the caller currently returns `set()` *before* `required_domains_for` runs; both
  layers must change or the fix is dead code. WS-A owns the `simulation.py` caller — this task
  only changes `specialists.py`'s signature and logic, and WS-A wires it.
  Test: `test_a_conditional_band_with_a_pass_recommendation_still_owes_a_panel` (fails today),
  `test_route_to_incubation_owes_a_panel`.

- [ ] **C1.5 — `commercial` and `budget` become requirable.** No code path can require either;
  `commercial` owns `differentiation`, the highest-weighted dimension (16/100 incubation).
  Add cue-based requirement for both, matching how `chemistry`/`clinical` are triggered.
  Test: `test_commercial_is_required_when_a_differentiation_claim_is_made`.

- [ ] **C1.6 — `maps_to_dimension` → `maps_to_dimensions: tuple[str, ...]`,** giving
  `mechanism_validation` (weight 10) an owner (`scientific`) and `toxicity_selectivity`
  (weight 8) an owner (`chemistry`). 5 of 13 dimensions are unowned = 25% of incubation weight,
  including the two the module docstring names as the most-cited rejection reasons.
  **Trap:** `src/services/directory.py:391` builds a dict **keyed by dimension** — re-pointing
  the existing 1:1 field silently orphans `experimental_rigor`. Update that reader in the same
  change. `tests/unit/test_rubric_prompt_sync.py` explicitly permits `None`, so widening is safe.
  Test: `test_every_dimension_above_5_percent_incubation_weight_has_an_owning_specialist`.

- [ ] **C1.7 — the never-clears alarm becomes a rate test.** It is `total >= 50 and not
  counts.get("clear")` — a zero-test that one outlier in 168 silenced, and this run was the
  first ever to silence it (the single `clear` is the only one in the whole database).
  Make it fire when the `clear` **rate** is below a floor.
  Test: `test_a_single_clear_does_not_silence_the_discrimination_alarm` — 167 caution + 1 clear
  must warn.

- [ ] **C1.8** Run `pytest tests/unit/ -q`, `ruff check tests/`, commit.

### Task D1: patents + pubmed — truncation, backoff, pacing, transport

**Files:** Modify `src/services/patents.py`, `src/services/pubmed.py`.
Test: `tests/unit/test_patents.py` (extend), `tests/unit/test_pubmed_transport.py` (create).

**Interfaces:** Produces `PriorArtResult.total_count` (see above).

- [ ] **D1.1 — disclose truncation.** `_search_titles` requests `limit: 10` with
  `sort: filingDate desc` and throws away ODP's `count`, so the hub cannot tell "10 of 10" from
  "10 of 27,906". 49 of 109 broadened searches returned exactly 10. Live-verified counts:
  `resistance` 27,906, `exposure` 14,639, `screening` 14,380, `COVID` 2,070. Carry `count` into
  `PriorArtResult.total_count` and have `_scope_note` render **"showing the 10 most-recently-filed
  of N matches"** whenever `len(hits) == limit`.
  **Do not raise `limit`:** `_search_titles` already caps at `min(limit, 50)`, 27,906 is not
  pageable, and it would 5× the enrichment traffic that is already 57% of the USPTO budget.
  Keep the existing empty-result caveat — it works (0 of 55 verdicts ever claimed FTO `met`).
  Test: `test_scope_note_discloses_truncation_when_hits_hit_the_limit` (a `count=2070` / 10-hit
  response mentions both 10 and 2070); `test_a_complete_small_result_set_is_not_called_truncated`.

- [ ] **D1.2 — fix the backoff inversion.** `_salience` awards `+3` for containing a digit
  **and** `+2` for `not token.islower()`; a pure-digit token satisfies **both** (`"1".islower()`
  is `False`), scoring 5 before the length bonus and beating every 2–4-letter gene symbol
  (`GDF`=3, `LOX`=3). With `_Q_SANITISE` splitting on hyphens, the numeric suffix of every
  `SYMBOL-N` name is promoted to rank 1: `MIP-3alpha …` → `['3alpha']`, `GDF-15 …` → `['15']`,
  `GS-441524 …` → `['441524']`, `TARP gamma-8 …` → `['8']`.
  Two changes: keep `SYMBOL-N` as one token through sanitisation, and give an **all-digit** token
  no bonus.
  **Trap:** do NOT solve this by growing `_GENERIC`. `_rank_terms` is `pool = specific or tokens`,
  so a blocklist aggressive enough to empty `specific` falls back to the **unfiltered** phrase —
  re-creating the guaranteed-zero-hit bug `_tiers` exists to prevent. Also do not drop the digit
  bonus wholesale: it is what promotes `C9orf72`/`MARK2`/`HER3`, which the docstring names.
  Test: `test_a_hyphenated_symbol_survives_as_one_token` (`_tiers("LOX-1 myeloid-derived
  suppressor cells")[-1]` must not be `["1"]`); `test_an_all_digit_token_scores_below_a_gene_symbol`.

- [ ] **D1.3 — pace the ladder, cache within a run, retry 429.** All 10 429s landed on the
  **3rd POST of their own un-paced tier ladder** — the burst is ours. And 109 searches resolve
  to only 91 distinct term-sets, so 18 redundant searches (16.5%) re-paid full enrichment:
  ~24% of the USPTO budget. Add a per-run memo keyed on the normalised query, an
  `await _pace()` before every ODP request (search *and* enrichment, mirroring
  `pubmed._ncbi_get`, where pacing is the load-bearing half), and retry 429 with backoff.
  Also dedupe `_tiers` on `frozenset` rather than a list — AND is commutative, live-verified
  (`deoxyhypusine AND synthase` = `synthase AND deoxyhypusine` = 27).
  Test: `test_an_identical_query_issues_no_second_http_request`;
  `test_a_429_is_retried_before_the_search_is_abandoned`;
  `test_a_permutation_tier_is_not_issued_as_a_second_request`.

- [ ] **D1.4 — label evidentiary weight.** Of 624 hit rows shown to the hub, only 12% are
  "Patented Case" while **17.4% are expired provisionals that were never published and are not
  prior art at all**, and 30% are unexamined 2026 filings. Label each hit from `status`
  (granted / published-pending / unpublished-provisional-NOT-prior-art / abandoned) in the
  `<patent>` block. **Do not filter to grants** — a published pending application *is* prior art
  under §102(a)(2).
  Test: `test_an_expired_provisional_is_labelled_not_prior_art`.

- [ ] **D1.5 — NCBI transport failures are transient, and are not evidence of nonexistence.**
  `_ncbi_get` retries only on status codes, so a truncated body (`RemoteProtocolError`) is not
  retried; `fetch_pubmed_records` swallows it and `fetch_abstract` then tells the model
  **"No PubMed record found for 41130592"** — a paper that exists, and which the same run had
  successfully fetched 69 seconds earlier. Retry
  `(RemoteProtocolError, ReadTimeout, ConnectError, ReadError)` on the same backoff, and make
  `fetch_abstract` distinguish "lookup failed" from "no record found".
  **Trap:** the more important half is the second one — fixing only the retry makes the bug
  rarer and less diagnosable.
  Test: `test_a_truncated_body_is_retried_then_succeeds`;
  `test_a_persistent_transport_failure_does_not_claim_the_record_is_absent`.

- [ ] **D1.6 — `IDCONV_BASE` trailing slash.** Every one of 194 idconv calls pays a 301 and is
  re-issued: 388 requests to make 194 lookups, 32% of all NCBI traffic, and `_pace()` counts one
  so the real rate against NCBI is 2× what the pacer believes.
  Test: `test_idconv_issues_no_redirect`.

- [ ] **D1.7** Run `pytest tests/unit/ tests/contract/ -q`, commit.

### Task F1: docs, roster, dead counters

**Files:** Modify `CLAUDE.md`, `.gitignore`, `src/main.py`, `src/routers/agent_page.py`.
Test: `tests/unit/test_claude_md_disclosure_sync.py` (create).

- [ ] **F1.1 — fix `CLAUDE.md:471-473`.** It says *"the full verdict — rationale, red flags,
  gating, `raw_verdict` — never appears on anything a PI or another lab sees."* That is **false
  for the interview thread**: `thread_guidance.py:131-134` and three prompt files mandate the
  inline verdict, naming those very fields. `git show 5d67e92 -- CLAUDE.md` shows the sentence's
  subject was widened from "the `:mag:` label" to "the full verdict" when the
  `#assessments-summary` headline shipped — the D12 field list was grafted onto an unrelated
  claim. Restore the narrow claim and state the real boundary: the **sidecar** (and
  `raw_verdict`, `weighted_score`, `band`, the dimension scores) never reaches Slack; the
  verdict's five headline fields are deliberately stated inline to the PI; the protected class in
  the visible half is the PI's own **unpublished** disclosures.
  Test: `test_claude_md_does_not_claim_the_inline_verdict_fields_are_hidden` — fail if
  CLAUDE.md's disclosure sentence names any field `thread_guidance._SCOUT_HUB[CONCLUDE]`
  mandates be stated inline. This makes the `5d67e92` class of drift impossible to reintroduce.

- [ ] **F1.2 — `.gitignore`.** `logs/` is covered only for `*.json` and `*.log`, so
  `logs/opportunity_assessments_backup_1787265062.sql` (a dump of assessment bodies) and
  `logs/profiles_public_pre_sync_*.tgz` are untracked **and not ignored** — a `git add -A` would
  commit them. Ignore `logs/` wholesale.
  Test: `test_logs_directory_is_fully_ignored` — assert `git check-ignore` succeeds for a
  `logs/x.sql` path.

- [ ] **F1.3 — stop rendering a structurally-zero counter.** `src/main.py:88-96` runs a per-agent
  `COUNT(ThreadDecision WHERE outcome='proposal')` on **every authenticated page load**, and
  `agent_page.py:236-245` lists those rows as the PI dashboard's "Proposals". `proposal` is
  unreachable — only `"timeout"` and `"no_proposal"` `_close_thread` call sites exist, and the DB
  holds 0 `proposal` rows across all runs ever (this is already documented at
  `tests/integration/test_proposal_review.py:22-29`). Delete the badge query and the dashboard
  section. **Do not migrate the enum** — `ALTER TYPE ... DROP VALUE` is unsupported and nine
  reader sites would need editing in the same deploy.
  Test: `test_no_src_path_can_construct_a_proposal_outcome` — an AST/grep assertion over
  `_close_thread`'s call sites, so the dead branch cannot be quietly resurrected.

- [ ] **F1.4 — log the `#assessments-summary` outcome.** All 13 headlines did post (confirmed via
  read-only Slack API) but the channel has **one member — the hub bot**, nobody has joined, and
  `_post_assessment_summary` logs nothing on success and returns **silently** on its
  `channel_id`/`is_connected` guard, so an empty audience is undetectable. Add an INFO on success
  and a WARNING on the silent skip. **This is a one-line-each change inside `simulation.py`, so
  hand it to WS-A** — record it here and do not edit that file from this workstream.

- [ ] **F1.5 — roster hygiene: deactivate `shastri`.** `agents.status='active'` for
  `ShastriBot` → `users.name='Nilabh Shastri'`, ORCID `0000-0002-8060-3025`, a scientist who died
  in January 2021 (verified against AAI In Memoriam, JHU BCMB, and an *Immunity* obituary — not
  from the bot's own text). The bot said so itself, publicly, in Slack. Set
  `status='inactive'` via SQL; the running simulation picks roster changes up live, no restart
  needed. **Flag, do not fix, the two adjacent items:** the ingest schema
  (`scripts/generate_sparsedata_user.py`) cannot represent the *"died 2021"* note that is present
  in `data/JHU_directory1_with_ORCID_v3_BBL.xlsx` row 61, and 3–4 active bots represent
  non-independent PIs whose parent labs also have bots (`camacho` in Casadevall, `gordy` in
  Markham, `tripathi`). Both need a human decision.

- [ ] **F1.6 — repair the truncated working memory.** `profiles/memory/klein/public.md` ends
  mid-directive (*"Do not re-pitch this idea unless that specific ICC/CV study has"*) — a
  refusal-truncated write that replaced a complete 1,977-char memory and is now injected into
  every klein prompt. Truncate the dangling clause to the last complete sentence. 1 of 56 files
  is affected; `wang` is ambiguous and should be left alone.

- [ ] **F1.7** Run `pytest tests/unit/ tests/integration/test_agent_page.py -q`, commit.

---

## Phase 2 — serial, lead-owned, `src/agent/simulation.py`

### Task A1: the capture gate (migration + trust the sidecar)

**Files:** Create `alembic/versions/0035_assessment_drop_raw_verdict.py`,
`scripts/backfill_dropped_verdicts.py`. Modify `src/models/opportunity.py`,
`src/agent/simulation.py`. Test: `tests/unit/test_assessment_sidecar.py` (extend).

- [ ] **A1.1 — migration 0035, additive.** `assessment_drops.raw_verdict JSONB NULL` and
  `llm_call_logs.wall_ms DOUBLE PRECISION NULL`. Both nullable, both mapped by the new code, so
  **migrate before serving** (the 0028/0030 pattern). Single head; include a working `downgrade`.

- [ ] **A1.2 — persist the sidecar on every drop.** `_record_assessment_drop` takes only
  `reason` and a human `detail` string, so a refused verdict's JSON is destroyed. This is the
  change that would have made the markham loss a footnote, and it protects against every future
  gate policy. Populate `raw_verdict`.
  Test: `test_a_refused_sidecar_is_recoverable_from_its_drop_row` — the drop row alone round-trips
  to a dict `weighted_score()` accepts, with no `llm_call_logs` dependency.

- [ ] **A1.3 — delete the `premature_sidecar` arm.** Replace the two external proxies
  (`thread_phase == CONCLUDE or closes_thread`) with "the presence of an `<assessment_json>`
  sidecar means the hub is delivering a verdict." The ordinal-12 door was offered **once in 140
  hub reply turns** and **0 of 15 sidecars** used it; every verdict arrives at ordinal 6, 8 or 10.
  `_retire_superseded_verdict` — which shipped in the **same commit** as the gate — already
  guarantees one row per interview under last-write-wins, so an early verdict is provisional, not
  destructive. A prompt-compliant model would have produced **zero** stored assessments this run.
  **MUST ship with A1.4.**
  Test: `test_a_sidecar_at_ordinal_six_is_stored_then_superseded` — drive a full sidecar at
  effective ordinal 6 with no ⏸️, then a concluding one at ordinal 10; assert exactly one
  `opportunity_assessments` row (the later), one `duplicate_thread_verdict` drop, and **zero**
  `premature_sidecar` drops. Seed the `MessageLog`: `_reply_to_thread` overwrites
  `ThreadState.message_count` from `get_thread_history`, which **excludes panel notes**.

- [ ] **A1.4 — move the summary post behind the final verdict.** `_capture_hub_assessment` posts
  the `#assessments-summary` headline **before** `_retire_superseded_verdict` runs. Relaxing A1.3
  without this makes provisional headlines public and unretractable — a direct violation of design
  D14 ("a dropped or refused sidecar never posts"). Post only when the carrying reply concludes or
  closes; otherwise defer.
  Test: `test_only_one_headline_is_posted_for_a_superseded_interview` — the ordinal-6-then-10
  scenario posts exactly one message to `ASSESSMENTS_SUMMARY_CHANNEL`, carrying the later verdict.

- [ ] **A1.5 — an absent-verdict alarm that is not ordinal-scoped.**
  `_warn_if_hub_conclude_missing_assessment` fires only at CONCLUDE, so it fired **0 times**
  against a run that lost 2 verdicts and had 7 interviews killed mid-screen. Write a drop
  (`closed_before_verdict`) whenever a thread closes with no stored verdict and no hub ⏸️-decline.
  That single row would have surfaced the whole capture cluster on the day of the run.
  Test: a PI-⏸️ close at ordinal 6 with no hub decline → exactly one such drop; a hub ⏸️ decline
  with no sidecar → **no** drop (that is the normal Outcome 2).

- [ ] **A1.6 — record who closed the thread.** `_check_thread_outcome` runs
  `_reply_closes_thread` on whoever replied, with no role gate, and 7 PI bots closed their own
  live interviews. **Do NOT ship a bare `agent.role == "scout_hub"` gate:** `thread_guidance.py:56`
  and `:75` explicitly tell PI bots to withdraw with ⏸️ and call it acceptable, so gating alone
  leaves the thread `active` with `has_pending_reply` flipping — 7 destroyed interviews become 7
  **zombie** interviews and a larger bill. Minimum honest change: keep the close, record the
  closer's role on `thread_decisions`, and let A1.5's drop row explain the blank. Whether to
  suppress the PI's close is a prompt change needing sign-off — out of scope.
  Test: `test_a_pi_initiated_close_is_distinguishable_from_a_timeout` — pins the `epearce`-vs-the-
  other-7 confusion that made the original count 8 instead of 7.

- [ ] **A1.7 — wire C1.4's band-aware floor.** `_specialist_floor_gap` returns `set()` before
  `required_domains_for` is reached, so C1.4 is dead code until this passes the computed band
  through.

- [ ] **A1.8 — F1.4's summary logging** (INFO on success, WARNING on the silent skip).

- [ ] **A1.9 — backfill markham and weeraratna.** Both sidecars parse cleanly from
  `llm_call_logs.response_text`; markham recomputes to **3.0400 → conditional /
  route-to-incubation** (the run's highest), weeraratna to 2.2200 → pass. Stamp
  `rubric_version='2.0.0'`, `rubric_content_hash='e3ef75f84c48'` **from the run**, not from
  whatever `blackbird_rubric` imports at backfill time, or cross-run comparability breaks
  silently. Set `slack_ts` so the rows join back to their messages. **Do not fire the summary
  post retroactively.** Run against prod after the deploy.

- [ ] **A1.10** `./scripts/ci.sh`, then commit.

### Task E1: scheduler and drain

**Files:** Modify `src/agent/simulation.py`, `src/agent/state.py`.

- [ ] **E1.1 — per-agent memory drain, overlapped with the reply sweep.** Task 1 of the
  2026-08-21 plan correctly removed the two `_update_agent_memory` awaits from inside
  `acquire_all`, but `_drain_memory_events` now runs on the main loop **after** the reply-lane
  gather barrier, one event at a time: 72 calls, 1,100 s, **0.0% overlap** with anything, and 26
  of 27 drain episodes were immediately followed by a `thread_reply` — so the drain *creates*
  reply-lane idle time. Replace the global list + single lock with per-agent queues and locks,
  and start the drain **before** the gather barrier so it overlaps the sweep. Coalesce multiple
  queued events for one agent into one synthesis (the hub is 36 of 72 calls, 47% of drain wall).
  **Traps:** `pop(0)` happens *before* the `await`, so a cancel loses the event — requeue in a
  `finally`; `stop()` must `await` the consumers before `set_call_log_callback(None)`; and Task
  1's own regression test must be **tightened to same-agent**, not deleted (only same-agent
  ordering needs serializing — different agents touch disjoint state).
  Test: same-agent events still serialize; different-agent events run concurrently (wall ≈ max,
  not sum); a `CancelledError` mid-drain leaves the event still queued.

- [ ] **E1.2 — `last_selected` initialisation.** `state.py:103` is `last_selected: float = 0.0`,
  so a never-selected agent's staleness weight is ~1.79e9 against ~187 for a just-run agent — a
  10⁷ ratio that turns `random.choices` into a shuffle-without-replacement over all 63 agents.
  That, not the tick rate, is why **no agent got a second turn** in 59 turns. Initialise to run
  start so staleness is a preference, not a veto.
  Test: `test_a_recently_run_agent_can_be_reselected_before_every_agent_has_run`.

- [ ] **E1.3 — `k`-concurrent post turns.** `_select_agent` draws `k=1`, and the post lane runs
  at measured concurrency **1.000** using 8.8 s of API time per tick — so covering 63 agents
  takes 63 ticks ≈ 196 min, over the 180-min budget. Return up to `min(k, len(candidates))`,
  gated by a post-lane semaphore.
  **Traps:** `_turn_eligible`'s `in_flight` check is documented as "a no-op today … load-bearing
  once loop iterations can overlap" — it becomes load-bearing at `k>1`; and
  `reply_lane_max_in_flight + k` must stay clear of the agent engine's `db_pool_size=25`.
  Test: with 10 eligible agents and `k=4`, four distinct agents run per tick, none twice, and a
  `_run_post_turn` asserting `not in_flight` on entry never fires.

- [ ] **E1.4 — meter real requests, not booked turns.** The sliding-window limiter books one
  call per turn while a hub `thread_reply` makes up to 5: 558 booked vs 812 real, a **1.46×**
  global undercount (~1.55× for the hub). "The limiter never bound" is true partly because it
  meters the wrong quantity. Book per real API call.

- [ ] **E1.5 — populate `wall_ms`.** `latency_ms` is the **last** call's latency (532/532 rows),
  so the stored 215.9 min understates the true 289.4 min by 25%. **Do not change `latency_ms`** —
  `api_call_count` and the limiter's `call_times` are rebuilt from row counts and the token
  columns' per-turn-cumulative semantics are pinned to that. Fill the new `wall_ms` column from a
  turn-level timer.
  Test: a 3-round turn with latencies 1/2/3 s yields `latency_ms == 3000` (unchanged) and
  `wall_ms >= 6000`.

- [ ] **E1.6** `./scripts/ci.sh`, commit.

---

## Phase 3 — deploy and run

- [ ] **P3.1** `./scripts/ci.sh` green on the final tree. No `src/` edits while it runs.
- [ ] **P3.2** Confirm no simulation is running:
  `docker ps --filter name=blackbird-agent-run`. (Container from run 8b64a0e0 already exited 0;
  its log is saved as `logs/blackbird_run_1787391032.log`. `docker rm` it.)
- [ ] **P3.3** Build both images **before** migrating (0035 is only in the new image):
  `$DC build blackbird-app worker` then `$DC --profile agent build agent`.
- [ ] **P3.4** Migrate from a one-off container off the new image, then verify:
  `$DC run --rm blackbird-app alembic upgrade head` → `alembic current` must equal `alembic heads` (0035).
- [ ] **P3.5** Start the web tier: `$DC up -d blackbird-app worker`. **Never `--remove-orphans`.**
- [ ] **P3.6** Backfill markham + weeraratna (A1.9).
- [ ] **P3.7** Deactivate `shastri` (F1.5).
- [ ] **P3.8** Start the fresh 1-hour run:
  `$DC --profile agent run -d --name blackbird-agent-run agent python -m src.agent.main --fresh --max-runtime 60`
- [ ] **P3.9** Verify the startup banner: roster count, `Screening rubric: version 2.0.0 (content
  hash e3ef75f84c48)`, and the **new** `--fresh: ignoring N pre-existing Slack message(s)` line
  in place of `Slack reconcile: appended …`. That line is the deploy's proof.
- [ ] **P3.10** After ~10 minutes, confirm health: no `premature_sidecar` drops, no
  `Empty/unparseable` storm, `_poll_cursors` holding (no re-ingestion), and
  `select count(*) from agent_messages where simulation_run_id = <new>` showing only in-run rows.

## Self-review

**Spec coverage:** every CONFIRMED/REVISED finding in `rca-and-corrections.md` maps to a task —
C1→A1.3, C2→A1.2/A1.9, C3→F1.1, C4→F1.5, C5→B1.4, C6→B1.1/B1.2, H1→A1.6, H2→D1.1, H3→D1.2,
H4→B1.5+C1.3, H5→C1.1, H8→(human decision, deliberately not coded), M1→F1.3, M3→E1.1, M6→B1.7,
M9/M13→D1.3, M12→D1.5, plus the four "new findings" (summary-channel audience→F1.4/A1.8, limiter
metering→E1.4, prior-art cache→D1.3, `--fresh` memory→noted below).

**Deliberately NOT coded** (human decisions, per the RCA): rubric threshold re-fit (censored
corpus + a model change between corpora + unreproducible baseline); whether the visible verdict
may paraphrase unpublished specifics; whether `--fresh` should also reset working memory;
suppressing the PI's ⏸️ close (needs a prompt change with sign-off); roster policy for deceased
and non-independent PIs.
