# Correctness Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to
> implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.


> ## ⚠️ REVISED 2026-08-22 after an adversarial audit OF THIS PLAN.
>
> Three independent audits found this plan **not safe to execute as first written**: four
> instructions were actively harmful, one was unimplementable, eight more were insufficient, and
> fourteen factual claims were wrong or stale. The harmful ones are corrected inline below and
> marked **[REVISED]**. Read `docs/plans/2026-08-22-correctness-remediation-AUDIT.md` FIRST — it
> records what was wrong and why, including corrections this file does not repeat, the deferral
> verdicts (two were rationalisations), and nine new CONFIRMED findings in subsystems no audit had
> covered. Do not treat any un-revised task as verified; several carry line citations that drift.

**Goal:** Fix every CONFIRMED correctness defect in `docs/audits/2026-08-22-correctness/README.md`,
starting with the one that is showing a wrong answer to staff right now.

**Architecture:** Six workstreams partitioned by **file ownership** so four can run in parallel.
`src/agent/simulation.py` is needed by three of them, so those are merged into one serial,
lead-owned phase. One additive migration (**0036**) carries three DDL changes and two data
repairs. Every fix is stated as a minimal diff with the test that must fail first.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, Alembic, pytest (`.venv-test` on the
prod host), anthropic SDK 1.0.0 deployed / 0.120.2 in `.venv-test`.

**Spec:** `docs/audits/2026-08-22-correctness/README.md`. Section numbers below (§1.1 etc.) refer
to it. Read it first — it carries the evidence for every claim this plan acts on.

## Global Constraints

- **No simulation is running.** Run `6fb83501` exited 0 at 17:35:30Z, its log is saved as
  `logs/blackbird_run_1787420579.log`, and the container is removed. Do not start one.
- **Never run pytest through the sshfs mount.** Host only:
  `ssh ubuntu@ec2-3-21-33-147.us-east-2.compute.amazonaws.com "cd /home/ubuntu/blackbird-copi-science && .venv-test/bin/python -m pytest <paths> -q"`
- **Never `pip install` against `.venv-test` from the sshfs client.** Host only.
- `ruff check tests/` must stay at **zero**. `src/` has a ratcheted ceiling of **231** (currently
  ~226) — do not exceed it.
- The full gate is `./scripts/ci.sh`. It must pass before any deploy.
- **Do not edit `src/` while a pytest run is in flight** — several tests call
  `inspect.getsource(SimulationEngine)` and even the whole module, and shifting line numbers
  mid-run produces spurious failures. This cost a false 61-failure run on 2026-08-22.
- `prompts/roles/*` and `src/agent/thread_guidance.py` string literals are byte-pinned by
  `tests/characterization/__snapshots__/test_agent_turn_gm.ambr` and
  `tests/unit/test_doc_prompt_sync.py`. **This plan does not reword any of them.** Never run
  `pytest --snapshot-update`.
- Alembic head is **0035**. This plan adds exactly **0036**.
- Two stacks share the host. Always `-f docker-compose.prod.yml`. **Never `--remove-orphans`.**
  Never touch a container whose compose project is `copi-python`.
- **Deploy ordering:** 0036 is additive, but the new code maps its columns, so *new code against
  the old schema raises `UndefinedColumn`*. Build → migrate from a one-off container → start.

## Cross-Workstream Interfaces (pin these exactly)

```python
# src/models/opportunity.py — OpportunityAssessment gains two columns (migration 0036)
panel_checked: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
#   True  = the floor evaluated this verdict (gap may still be empty)
#   False = the floor determined no panel was owed
#   NULL  = written before 0036; we do not know whether any floor ran
thread_id: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)

# src/services/assessment_detail.py — _panel_state gains a FIFTH state
"gap" | "unverified" | "not_owed" | "unrecorded" | "verified"
#   "unrecorded" = owed a panel but panel_checked IS NULL. Never rendered green.

# src/services/llm.py — additive, default preserves today's behaviour
on_stop_reason: Callable[[str], None] | None = None      # already exists, unchanged
# _call_stat() gains two keys, read defensively:
#   "cache_read_input_tokens": int | None
#   "cache_creation_input_tokens": int | None

# src/agent/specialists.py — unchanged signature, new keyword
def parse_opinion(raw: str, *, domain: str) -> SpecialistOpinion
# SpecialistOpinion gains: truncated: bool = False
```

## File Ownership (do not cross these lines)

| WS | Owns | Runs |
|---|---|---|
| B | `src/services/llm.py` | subagent, phase 1 |
| C | `src/agent/specialists.py`, `src/agent/tools.py`, `src/services/patents.py`, `src/services/pubmed.py` | subagent, phase 1 |
| E | `src/main.py`, `src/dependencies.py`, `src/routers/profile.py`, `src/routers/admin.py` (cookie only) | subagent, phase 1 |
| F | `scripts/backfill_dropped_verdicts.py` | subagent, phase 1 |
| A+D | `src/agent/simulation.py`, `src/agent/main.py`, `src/agent/state.py`, `src/models/*`, `alembic/versions/0036_*`, `src/services/assessment_detail.py`, `templates/admin/_assessment_detail_body.html` | **lead**, phase 2 |

---

## Phase 1 — parallel, disjoint files

### Task B1: `llm.py` — token accounting, lost turns, off-by-one

**Files:** Modify `src/services/llm.py`.
Test: `tests/unit/test_llm_cache_accounting.py` (create), `tests/unit/test_llm_observability.py` (extend).

**Interfaces:** Produces the two new `_call_stat` keys above. Consumes nothing.

- [ ] **B1.1 — record cached input tokens (§1.3).** `usage.input_tokens` EXCLUDES cached input;
  the cached part arrives as `cache_read_input_tokens` / `cache_creation_input_tokens`. Nothing
  reads them today, so 109 of 141 live rows record fewer input tokens than the system prompt alone
  can be, and one records **2** for a 30 KB prompt. Add both to `_call_stat` (defensive `getattr`,
  same style as `_thinking_tokens`), and make the three `total_input_tokens +=` sites accumulate
  **input + cache_read + cache_creation** so the column keeps meaning "billable input volume for
  this turn".
  **[REVISED]** Two errors in the original: (1) both fields are `Optional[int]` defaulting to
  `None` on the deployed SDK, so a literal `total_input_tokens += ... + cache_read + cache_creation`
  is a **`TypeError` inside a billed turn** — read them through a defensive helper and `or 0`;
  (2) summing in place is the exact thing `agent_activity.py:198-210` rules against ("Do NOT 'fix'
  this column by summing it — the numbers already in the table would then mean two different things
  depending on when they were written"), which is why 0035 added `wall_ms` as a new column.
  **Preferred: add `cache_read_input_tokens` / `cache_creation_input_tokens` COLUMNS in 0036** and
  sum in the admin tile. If you keep the in-place sum instead, update that model comment to record
  the definition change and the 228-row discontinuity, and say so in the commit.
  Also update `test_llm_call_stats.py:462`, which pins
  `input_tokens == sum(call_stats input_tokens)` and otherwise becomes vacuous. Ignore
  `usage.cache_creation` — it is a per-TTL breakdown of `cache_creation_input_tokens` and would
  double-count.
  **Trap:** do NOT change what `input_tokens` means per-call inside `call_stats` — record the three
  numbers separately there so a future reader can still see the split. Only the turn TOTAL sums them.
  Also extend `tests/fakes.py`'s `_Usage` with the two fields defaulting to `None`, so existing
  tests are unaffected — this is the reason no test could see the bug.
  Tests: `test_turn_totals_include_cached_input`, `test_call_stats_records_the_cache_split`,
  `test_a_usage_without_cache_fields_still_works`.

- [ ] **B1.2 — a raising retry must not lose the answer already in hand (§1.5).** `llm.py:808`,
  `:1073`, `:1176`, `:1214` are the retry / forced-final `_acreate` calls. An exception there
  discards a truncated-but-usable reply AND never reaches `_emit_call_log`, so the turn's billed
  calls vanish from the record — measured: `rows written: 0` after 6 billed rounds. Wrap each in
  `try/except Exception`, log at ERROR naming the phase, and fall through with the text already
  accumulated. Ensure `_emit_call_log` still runs on that path.
  **Trap:** this is now much more reachable because `CLIENT_READ_TIMEOUT_SECONDS = 300` and the SDK
  retries `APITimeoutError` twice — the module's own comment concedes a clamped 21,333-token retry
  needs ~351 s. Do NOT "fix" that by raising the timeout back to 600; that reinstates the 10.6%
  dead-air stall this replaced.
  Tests: `test_a_raising_retry_returns_the_first_pass_text`,
  `test_a_raising_retry_still_writes_its_call_log_row`.

- [ ] **B1.3 — whitespace-only retry must not clobber the first pass (§2.13).** `llm.py:826`,
  `:1097`, `:1237` are `_all_text(retry_msg) or response_text`; `"\n\n   \n"` is truthy and wins.
  Change to `_all_text(retry_msg).strip() or response_text`.
  Test: `test_a_whitespace_only_retry_keeps_the_first_pass_text`.

- [ ] **B1.4 — `_emit_call_log` must not be able to kill a turn (§2.13).** Its two siblings
  (`_notify_stop_reason:448`, `_log_empty_reply:583`) swallow everything on the stated principle
  that an observability hook must not cost the reply. `_emit_call_log` calls the callback bare and
  two of its call sites have no enclosing try. Wrap the invocation in
  `try/except Exception: logger.exception(...)`.
  Test: `test_a_raising_log_callback_does_not_cost_the_reply`.

- [ ] **B1.5 — `max_tool_rounds` off-by-one (§2.13).** `for round_num in range(max_tool_rounds + 1)`
  yields one more tool-capable call than the setting names, so the docstring, the
  `"Max tool rounds (%d) reached"` warning and CLAUDE.md's shutdown-grace arithmetic all
  under-count by one. **[REVISED — do NOT change the loop.]** Max observed rounds over all 1,121
  rows carrying `call_stats` is **4** against a budget of 6, and no caller ever passes
  `max_tool_rounds` — so `range(max_tool_rounds)` removes headroom production has never used. And
  seven tests use `max_tool_rounds=1` as SETUP to force a two-round turn
  (`test_llm_call_stats.py:265` asserts `["round","round","forced_final"]`), so the original trap
  ("its expectation is part of the bug") would have **deleted the only multi-round coverage**.
  Correct the DOCUMENTATION instead: the parameter docstring, the `"Max tool rounds (%d) reached"`
  message, `llm.py:81`, `agent_activity.py:194`, and CLAUDE.md's grace-period arithmetic. Zero
  behaviour change, zero test churn.

- [ ] **B1.6 — dedicated executor for the API calls (§1.3 note / llm-F5).** `_acreate` runs under
  `asyncio.to_thread`, whose default executor on this 2-vCPU host is `min(32, 2+4) = 6` — so the
  consult gather silently tops out at 6 (the hub can request 8) and an unrelated agent's turn queues
  behind a 300 s consult. Create a module-level `ThreadPoolExecutor` sized from the intended fan-out
  and use `loop.run_in_executor` in `_acreate`.
  **[REVISED]** "Sized from the intended fan-out" computes to ~32 concurrent 300 s API calls on a
  2-vCPU host: `reply_lane_max_in_flight=4` × 8 specialists. The accidental 6-thread executor is a
  **load-bearing throttle** — `_acreate` has no semaphore and nothing retries 429s beyond the SDK's
  two. Use a modest fixed pool (12-16) **plus an explicit `asyncio.Semaphore` in `_acreate`**, and
  state the number and its basis as this module does for every other constant. The benefit is also
  larger than stated: every Slack call goes through `to_thread` too, so 8 gathered consults can
  starve the pollers and the persist path.
  **Traps:** create it lazily (the web tier imports this module too); preserve the
  `contextvars.Context` that `asyncio.to_thread` propagates and `run_in_executor` does not; and note
  that a never-shut-down pool is joined at interpreter exit rather than by
  `loop.shutdown_default_executor()`, which changes shutdown ORDERING relative to `main.py`'s
  flush.
  Test: `test_api_calls_do_not_share_the_default_thread_pool`.

- [ ] **B1.7** Run `pytest tests/unit/test_llm_*.py -q` on the host; `ruff check tests/`; commit.

### Task C1: specialists / tools / patents / pubmed

**Files:** Modify `src/agent/specialists.py`, `src/agent/tools.py`, `src/services/patents.py`,
`src/services/pubmed.py`. Test: extend `tests/unit/test_specialists.py`,
`tests/unit/test_patents.py`, `tests/unit/test_pubmed_transport.py`,
`tests/unit/test_consult_accounting.py`.

**Interfaces:** Produces `SpecialistOpinion.truncated: bool = False`. Consumes `on_stop_reason`
(already shipped).

- [ ] **C1.1 — stop `_strip_fence` defeating the recovery branch (§1.9).**
  `specialists.py:283` calls `extract_json(_strip_fence(raw or ""))`. `json_extract.py:75-81` has a
  branch for the shape its own comment names ("Claude sometimes drops the opening brace inside the
  fence") — and it tests for the fence that `_strip_fence` just removed. So a fenced brace-less
  `blocking`/`high` opinion parses from `raw` and **raises** after stripping, landing on
  `caution`/`low`/`()`. `extract_json(raw)` strictly dominates across 13 shapes. Change to
  `extract_json(raw or "")`.
  **Trap:** keep `_strip_fence` in `has_usable_content`, where it is load-bearing — a fenced `{}`
  must still read as "says nothing".
  Tests: `test_a_fenced_braceless_blocking_opinion_is_not_laundered`,
  `test_a_fenced_empty_object_still_reads_as_no_content`.

- [ ] **C1.2 — mark a truncated consult so nothing downstream can launder it (§2.10, §2.13).**
  Add `truncated: bool = False` to `SpecialistOpinion`. `tools.py` already refuses to credit a
  `refusal`-truncated consult to the floor; carry the flag into `on_consult_record`'s fields so
  (a) the panel note is skipped or rendered `TRUNCATED — not counted` rather than the defaulted
  `⚠️ caution` that currently reaches the PI's thread, and (b) WS-A can exclude it from the
  consult-rehydration SELECT.
  **Trap:** `format_panel_note`'s three-argument signature IS the enforcement that nothing else
  leaks into a note (`test_nothing_but_the_three_publishable_fields_can_reach_a_note`). Do not
  widen it — skip the note instead, or pass an unrecognised signal, which `format_panel_note`
  already renders bare by design.
  Tests: `test_a_truncated_consult_posts_no_caution_note`,
  `test_a_truncated_consult_is_marked_on_the_record`.

- [ ] **C1.3 — non-ASCII terms must not vanish from a prior-art query (§2.9).**
  `_Q_TOKEN = re.compile(r"[A-Za-z0-9]+(?:-[0-9][A-Za-z0-9]*)*")` is ASCII-only, and
  `total_terms` counts post-tokenisation, so a dropped term is invisible to `broadened` and to
  `_scope_note`. Real production queries: `Qβ malaria epitope` → `['malaria','epitope']` reported
  as a non-broadened on-point search with the most specific term gone; `ERα` → `ER`; a U+2011
  `LOX‑1` → `['LOX','1']`, and a bare `1` matches 34,595 titles by this module's own measurement.
  **[REVISED — the original instruction was HARMFUL and its premise was wrong.]** `Qβ` does NOT
  lose its specific term: `Q` is ASCII, so tokens are `['Q','malaria','epitope']` and `_salience`
  ranks `Q` FIRST — the narrowest tier is a one-letter search (`inventionTitle:(Q)` → live count
  1,862). And **ODP cannot represent non-ASCII at all**: probed live, `(β)`, `(Qβ)` and `("Qβ")`
  all return HTTP 404, which `patents.py:392-393` maps to `([], 0)` = "matched nothing" — so
  widening the class would report **fake novelty**, the one thing CLAUDE.md forbids. NFKC also does
  not fold U+2010/2011/2013/2014 to ASCII `-`, so the measured `LOX‑1` case would stay broken.
  Correct form: **transliterate, do not widen.** Map `α→alpha, β→beta, γ→gamma …` and NFKD-strip
  combining marks BEFORE tokenising, keeping the character class ASCII so
  `patents.py:45-47`'s leading-`-` injection invariant still holds. Use an explicit
  `str.translate` table for U+2010–U+2015/U+2212/U+FF0D. Do NOT redefine `total_terms` as the
  whitespace count (`TFEB / TFE3 fusion` would falsely report `broadened`); add a separate
  `dropped_or_rewritten` field instead. Separately worth fixing: 404-as-zero is the mechanism that
  turns any unrepresentable query into a clean negative.
  Tests: `test_a_greek_letter_term_survives_tokenisation`,
  `test_a_unicode_hyphen_symbol_is_not_split`, `test_a_dropped_term_forces_broadened`.

- [ ] **C1.4 — word-form query operators (§2.13).** `_q_term` quotes only hyphenated tokens, so
  `AND`/`OR`/`NOT` survive as bare tokens, and `_salience("NOT") == 3` ranks them above prose so
  one can become a whole backoff tier (`inventionTitle:(TFE3 AND TFEB AND NOT)`). Drop
  `{and, or, not}` case-insensitively from the token stream.
  Test: `test_a_word_operator_never_becomes_a_search_term`.

- [ ] **C1.5 — a completed-but-unparseable PubMed response must not claim nonexistence (§2.13).**
  `_parse_pubmed_xml` swallows `ET.ParseError` and returns `[]`, so `_fetch_pubmed_batch` returns
  `[]` without raising and the new `_LOOKUP_FAILED` guard never sees it — the model is told
  "No PubMed record found for X" for a request that completed with malformed XML. Have the
  single-record path distinguish a parse failure from an empty result set.
  Test: `test_malformed_xml_does_not_claim_the_record_is_absent`.

- [ ] **C1.6 — `_scope_note` string joins are malformed (§2.13).** Produces
  `"…for TFEB AND melanoma.COMPLETENESS: showing…"` (missing space) and a trailing `\n\n\n\n`
  because `truncation_note` already ends in `\n\n`. Fix the concatenation.
  Test: `test_the_scope_note_reads_as_prose`.

- [ ] **C1.7 — widen `_RETRYABLE_TRANSPORT` (§2.13).** It omits `ConnectTimeout`, `WriteTimeout`,
  `PoolTimeout`, `WriteError` while the justifying comment implies the timeout family is covered.
  Test: `test_every_timeout_class_is_retried`.

- [ ] **C1.8** Run the four test files on the host; `ruff check tests/`; commit.

### Task E1: web / authorization

**Files:** Modify `src/main.py`, `src/dependencies.py`, `src/routers/profile.py`,
`src/routers/admin.py` (impersonate cookie only).
Test: `tests/integration/test_request_origin_guard.py` (create),
`tests/integration/test_access_revocation.py` (create), extend
`tests/integration/test_manager_views.py`.

- [ ] **E1.1 — an Origin/Sec-Fetch-Site guard on every state-changing request (§2.6).** There are
  zero `Origin`/`Referer`/CSRF checks in `src/`, and `SameSite=Lax` does not help: the same nginx
  serves `blackbird.copi.science`, `copi.science` (the other tenant) and `devel.copi.science`,
  which are **same-site** for cookie purposes — so a page on either sibling can auto-submit a
  top-level POST with the victim's session cookie attached. Reachable targets include
  `POST /profile/delete-account` and, against a logged-in admin, `POST /admin/users/{id}/role`.
  Add one middleware: for any method not in `{GET, HEAD, OPTIONS}`, require `Origin` (or
  `Referer`'s origin) to equal `settings.base_url`; **fail closed when absent**; return 403.
  **[REVISED — three of the original traps were wrong.]**
  (a) **You MUST exempt `POST /settings/unsubscribe/{token}`.** `email_notifications.py:437`/`:616`
  set `List-Unsubscribe-Post: List-Unsubscribe=One-Click`, and those POSTs are issued
  **server-side by Gmail/Apple/Yahoo with no Origin and no Referer** — a fail-closed exempt-nothing
  guard breaks RFC 8058 one-click unsubscribe and bulk-sender compliance. `auth.py:35-40` already
  treats that path as a non-browser exemption. Gate the exemption on the request carrying no
  session cookie so it cannot become a CSRF gadget. The original "exempt nothing by path" was wrong.
  (b) **Middleware order:** Starlette PREPENDS, so the middleware added LAST runs OUTERMOST. The
  guard reads only headers, needs no session, and should be outermost (added last) — which also
  rejects before `AgentBadgeMiddleware`'s per-agent COUNT queries run. The original trap said the
  opposite.
  (c) The ORCID callback is a GET (`auth.py:138`) — verified, unaffected. No inbound Slack POST
  route exists.
  (d) **The test blast radius is three lines, not the largest risk in this plan:**
  `tests/conftest.py:100-125` plus the two files that build their own client
  (`test_badge_impersonation_gate.py:79`, `test_manager_access.py:85`). But the header **must be
  derived from `get_settings().base_url` at runtime** — the host's `.env` sets
  `BASE_URL=https://blackbird.copi.science`, so a hardcoded `http://testserver` passes locally and
  403s everything on the host. Normalise `base_url` to scheme://host[:port] before comparing.
  Tests: `test_a_post_without_an_origin_is_refused`,
  `test_a_post_from_a_sibling_domain_is_refused`, `test_a_post_from_our_own_origin_is_allowed`,
  `test_a_get_needs_no_origin`.

- [ ] **E1.2 — revoking access must end the session (§2.7).** `get_current_user` reloads the user
  every request but never reads `access_status`; sessions are unkeyed signed cookies with a 30-day
  `max_age` and no server-side store, so `admin_deny_access` is a no-op against a live session for
  up to 30 days. Probed: `denied-user GET /profile → 200`. In `get_current_user`, if
  `session_user.access_status != "allowed"`, clear the session and redirect to `/access-pending`.
  **Trap:** `/access-pending` itself and `POST /logout` must remain reachable, or a denied user
  is redirect-looped.
  Tests: `test_a_denied_user_is_logged_out_on_the_next_request`,
  `test_a_pending_user_cannot_reach_the_profile_page`,
  `test_a_denied_user_can_still_reach_access_pending`.

- [ ] **E1.3 — `POST /profile/save` needs `get_pi_user` (§2.8).** `profile.py:99-113` uses
  `get_current_user` while all four sibling PI-write POSTs use `get_pi_user`. Probed: a manager
  POSTs it successfully and a `ResearcherProfile` is created; it also lets a manager rewrite the
  `email` that binds delegate-invitation acceptance. One-word change.
  Test: `test_a_manager_cannot_save_a_pi_profile`.

- [ ] **E1.4 — close the interactive docs (§2.6).** `create_app()` passes no `docs_url`/`redoc_url`/
  `openapi_url`, so `/docs`, `/redoc` and `/openapi.json` are unauthenticated and disclose 90
  paths plus every form field name. Pass `docs_url=None, redoc_url=None, openapi_url=None`.
  Test: `test_the_openapi_schema_is_not_public`.

- [ ] **E1.5 — the impersonate cookie is never `Secure` (§2.13).** `admin.py:1055` reads
  `request.app.state.allow_http`, which **nothing ever sets**, so the `hasattr` is always False and
  the ternary always yields `secure=False`. Use `secure=not get_settings().allow_http_sessions`,
  matching the session cookie.
  Test: `test_the_impersonate_cookie_is_secure_when_https_is_required`.

- [ ] **E1.6** Run the three suites plus `tests/characterization/test_auth_and_admin_routes.py` on
  the host; `ruff check tests/`; commit.

### Task F1: the backfill script

**Files:** Modify `scripts/backfill_dropped_verdicts.py`.
Test: `tests/unit/test_backfill_dropped_verdicts.py` (create).

- [ ] **F1.1 — read the right sidecar key (§1.7).** Line 192 reads `derisking_milestones`; the
  sidecar contract key is `suggested_derisking_milestones`
  (`prompts/roles/scout_hub/phase4-thread-reply.md:214`), which the engine reads correctly at
  `simulation.py:3378`. The two rows this script wrote are exactly the two rows in the table whose
  `derisking_milestones` is a JSON `null`, while their `raw_verdict` still holds 8 and 9 milestones.
  Test: `test_the_backfill_reads_the_contract_milestone_key`.

- [ ] **F1.2 — stop claiming a verified panel (§1.1/§F2).** The script sets neither
  `panel_incomplete` nor `missing_domains`, so they land `false`/NULL, which the three-state
  contract reads as "the floor ran and found no gap". Set `missing_domains=[]` (the documented
  UNVERIFIED state) and `panel_checked=False` once WS-A adds that column.
  **Trap:** this task must land AFTER WS-A's model change or the kwarg will not exist. Write the
  code, and if `panel_checked` is absent, set only `missing_domains=[]` and say so in the report.
  Test: `test_a_backfilled_row_does_not_claim_a_verified_panel`.

- [ ] **F1.3 — reuse the engine's guards (§F8).** The script bypasses `_bounded_str` on four
  LLM-sourced VARCHAR columns (an over-long value raises `StringDataRightTruncation` and, because
  the script commits once at the end, loses **every** row it would have written) and bypasses
  `_normalize_gating` (a legacy boolean would be stored raw, breaking the tri-state invariant).
  Import and apply both. Also key idempotency on the interview, not `(run, subject)` — a PI with
  two interviews currently has the second one's verdict silently skipped.
  Tests: `test_an_overlong_recommendation_is_clipped_not_fatal`,
  `test_gating_is_normalised_on_a_backfilled_row`.

- [ ] **F1.4 — narrow the `llm_call_logs` fallback (§M5.7).** It filters only
  `agent_id == 'blackbird'` + `LIKE '%<assessment_json>%'` and takes the last row at-or-before the
  drop. The hub interleaves many interviews, so if the turn's log row ever lands *after* the drop
  row the candidate is a **different PI's verdict**. It happened to work (log rows 249 ms and
  232 ms before their drops) but nothing enforces it. Match on the subject inside the sidecar.
  Test: `test_the_fallback_never_recovers_another_pis_verdict`.

- [ ] **F1.5** Run the new test file; `ruff check tests/`; commit.

---

## Phase 2 — serial, lead-owned: `simulation.py`, models, migration 0036

### Task A1: migration 0036 and the panel-state truth fix

**Files:** Create `alembic/versions/0036_panel_checked_thread_id_and_repairs.py`.
Modify `src/models/opportunity.py`, `src/models/agent_activity.py`,
`src/models/specialist_consult.py`, `src/services/assessment_detail.py`,
`templates/admin/_assessment_detail_body.html`.
Test: extend `tests/unit/test_panel_state.py`, `tests/integration/test_assessment_detail_page.py`,
`tests/unit/test_migration_checks.py`, `tests/integration/test_harness_smoke.py`.

- [ ] **A1.1 — migration 0036 (additive DDL + two data repairs).**
  DDL: `opportunity_assessments.panel_checked BOOLEAN NULL`;
  `opportunity_assessments.thread_id VARCHAR(50) NULL` + index;
  recreate `private_channel_members_user_id_fkey` as **ON DELETE CASCADE** (it is currently
  `SET NULL` under a CHECK forbidding both owner columns NULL, so any user delete for a member
  raises — reproduced; the table has 0 prod rows, so this is safe now and only gets harder later).
  Data: `UPDATE opportunity_assessments SET derisking_milestones =
  raw_verdict->'suggested_derisking_milestones' WHERE slack_ts IS NULL AND
  jsonb_typeof(derisking_milestones) = 'null'` (repairs §1.7's 17 lost milestones);
  and normalise JSONB `null` → SQL NULL on the columns §2.11 names, in the shape of 0031.
  **[REVISED]** Four corrections: (1) **the milestone repair MUST run BEFORE the JSONB
  normalisation** — `derisking_milestones` is one of the normalised columns, so normalising first
  makes the repair match ZERO rows while reporting success, losing all 17 milestones; (2) add
  `specialist_consults.truncated BOOLEAN` as a fourth DDL item (A2.2 is blocked without it);
  (3) change the model's `ondelete` too (`agent_activity.py:337`) or 0036 creates the drift it is
  fixing; (4) `tests/integration/test_db_contract.py:268` deliberately PINS the FK's current broken
  behaviour with a comment saying so — **invert** that assertion, do not delete it.
  Bump the head pins: `scripts/migrate/preflight.py`'s `DEFAULT_TARGET`, `REVISION_ORDER`,
  `SUPPORTED_START_REVISIONS` (add `"0035"`), plus
  `tests/unit/test_migration_checks.py` and `tests/integration/test_harness_smoke.py`.
  **Trap:** a downgrade must restore `SET NULL` on that FK or the round trip is not an inverse and
  `scripts/ci.sh`'s upgrade→downgrade→upgrade check fails.

- [ ] **A1.2 — `JSONB(none_as_null=True)` on the remaining columns (§2.11). [REVISED: it is 11 of 13, not nine — enumerate from `Base.metadata`, not from prose. The three nobody listed are `researcher_profiles.pending_profile`, `.user_submitted_texts` and `cohort_audit_events.topology`.]** Only
  `opportunity_assessments.missing_domains` and `llm_call_logs.call_stats` have it today, so
  `assessment_drops.raw_verdict` already holds BOTH encodings of "no verdict kept" (15 SQL NULL,
  2 JSONB null) and the obvious operator query returns 2 rows, both of which kept nothing. This is
  a Python-side property — **no deploy ordering**. Add a metadata-driven test so a tenth column
  cannot repeat this.
  Test: `test_every_nullable_json_column_uses_none_as_null` — walk `Base.metadata`, fail on any
  nullable JSON/JSONB column whose type lacks `none_as_null`. This is the test whose absence let
  0035 reintroduce the bug 0031 fixed.

- [ ] **A1.3 — `_panel_state` must stop claiming a verification that never happened (§1.1).**
  THE headline fix. Add `"unrecorded"` as a fifth state: returned when the verdict is owed a panel
  (`panel_is_owed`) but `panel_checked IS NULL`. Write `panel_checked` in `_persist_assessment`:
  `True` when the floor evaluated the verdict, `False` when it determined none was owed.
  Render `"unrecorded"` in the template as a **neutral/amber** box reading
  "Specialist panel: not recorded — this verdict predates panel tracking", never the green
  `{% else %}` branch.
  **[REVISED]** (1) **Make the stored column the sole authority for green** — `verified` iff
  `panel_checked is True`, `not_owed` iff `False`, `unrecorded` iff NULL — and delete
  `panel_is_owed` from the read path. Leaving `if not panel_is_owed(...)` ahead of the column test
  re-arms this very bug the next time the predicate widens, and it has widened twice this month.
  (2) Name it **`panel_owed`** (a durable fact: "was a panel owed under the rules in force at write
  time") rather than `panel_checked`, which is ambiguous with the `missing_domains=[]`
  unverifiable case. (3) Compute it ONCE in `_persist_assessment` and pass it down, or this becomes
  a FOURTH site computing `panel_is_owed`. (4) Four existing assertions **invert**
  (`test_panel_state.py:46,47,77,109`) plus `test_assessment_detail_page.py:776`, and ~45
  hand-rolled test constructions will leave the column NULL — the `_assessment()` helper needs a
  default. (5) Drop the copy "predates panel tracking": it will be false for every post-0036 row
  that lands NULL. Say "not recorded".
  (6) **This fixes ONE of THREE surfaces.** The assessments LIST page
  (`_assessments_body.html:288-297`) and the run-level banner (`directory.py:335-337`) still gate on
  `panel_incomplete` alone, on both admin AND manager templates, so the same 12 rows look
  unremarkable one click away. Add them to this task's file list.
  **Trap:** the template's final `{% else %}` is the green "no gap recorded" box, so ANY unhandled
  state renders green. Add the explicit branch, and add a test that an unknown state string does
  not render green.
  Tests: `test_an_owed_verdict_with_no_panel_record_is_not_called_verified`,
  `test_a_floor_checked_verdict_with_no_gap_is_verified`,
  `test_an_unknown_panel_state_never_renders_green`,
  and one asserting the 12 historical shapes now render `unrecorded` rather than `verified`.

### Task A2: the assessment pipeline

- [ ] **A2.1 — migrate the fourth `panel_is_owed` call site (§1.2).**
  `simulation.py:3912` still reads `verdict.get("recommendation") not in self._PANEL_REQUIRED_FOR`
  and returns early, so `_seed_consults_from_db` skips consult rehydration for exactly the verdicts
  the new floor holds to the panel. Reproduced: a `pass`/`conditional` verdict is stamped
  `panel_incomplete=true` naming four domains, three of which ARE recorded as consulted. Replace
  with `panel_is_owed(verdict.get("recommendation"), self._computed_score_and_band(verdict)[1])`.
  Then delete `_PANEL_REQUIRED_FOR` if it has no readers left, and update `panel_is_owed`'s
  docstring, which enumerates three sites and is now wrong.
  Test: `test_the_consult_seed_runs_for_a_band_owed_verdict`.

- [ ] **A2.2 — exclude truncated consults from rehydration (§2.10). [REVISED — BLOCKED until
  0036 adds `specialist_consults.truncated`.]** There is no truncation column today, and
  `tools.py:679-690` passes identical kwargs for a refused and a good consult, so the stored row is
  byte-indistinguishable and there is nothing to filter on. Add
  `specialist_consults.truncated BOOLEAN` as a **fourth DDL item in A1.1**, widen
  `_record_specialist_consult` to persist it, then filter. Note the default makes all pre-0036 rows
  read "not truncated", so the three known-truncated 8b64a0e0 consults keep crediting the floor —
  say so rather than implying a repair. Related: **C1.2 as scoped is a no-op** — the `⚠️ caution`
  note is posted by `SimulationEngine._post_panel_note`, which WS-C does not own and whose signature
  ends in `**_withheld`, so a new field is silently absorbed. `_seed_consults_from_db`'s
  docstring asserts "a row exists only for a SUCCESSFUL consult … cannot turn an unreachable
  specialist into a consulted one". C1.2 makes that false. Filter the SELECT on the truncated
  marker and correct the docstring.
  Test: `test_a_truncated_consult_does_not_satisfy_the_floor_after_a_restart`.

- [ ] **A2.3 — supersession must keep the verdict it deletes (§1.8).**
  `_retire_superseded_verdict` DELETEs the earlier row and records the drop **without**
  `raw_verdict`, while the refusal path passes it under a comment saying a refusal "is never a
  licence to destroy it". SELECT the row's `raw_verdict` in the same session before the DELETE and
  pass it through.
  Test: `test_a_superseded_verdict_is_recoverable_from_its_drop_row`.

- [ ] **A2.4 — make the one-row invariant enforceable (§2.5).** Write `thread_id` in
  `_persist_assessment` (from `thread.thread_id`), rehydrate `_assessed_threads` at startup from
  `opportunity_assessments` for the current run, and narrow
  `_retire_superseded_verdict` **without** re-keying it on `thread_id` alone.
  **[REVISED — the original instruction was HARMFUL.]** `_capture_hub_assessment` reads
  `superseded` BEFORE `_persist_assessment` writes the replacement and retires AFTER, so a DELETE
  keyed `thread_id == thread.thread_id` matches the replacement too (same run, same `agent_id`,
  same thread, already committed): **every supersession would end the interview with zero
  assessments, logging success.** `slack_ts` is load-bearing precisely because it is the one field
  that differs. Correct form: have `_persist_assessment` return the new row's id and delete
  `WHERE thread_id = X AND id <> new_id`, or keep `slack_ts` as the key and use `thread_id` only to
  narrow. Do NOT reorder retire-before-persist.
  Note also: removing the `if not superseded.slack_ts: return` bail poisons the retry-queue filter
  at `:3635-3646` (`row.get("slack_ts") == None` would match every queued assessment with a NULL
  slack_ts). And rehydration must set `ordinal=0`, `announced=False`, and derive
  `final = thread_id in self._closed_thread_ids` — a guessed `final=True` refuses the interview's
  own concluding verdict, turning a duplicate-row bug into loss of the better verdict.
  **Trap:** do NOT add a unique constraint in this migration. Two legitimate interviews with the
  same PI exist in prod, and the correct key is `(run, thread_id)` — but `thread_id` is NULL for
  all 63 historical rows, so a unique index would have to be partial (`WHERE thread_id IS NOT
  NULL`). Land the column and the rehydration first, verify on one run, then add the constraint in
  a later migration.
  Tests: `test_assessed_threads_is_rehydrated_after_a_restart`,
  `test_supersession_finds_the_row_by_thread_not_slack_ts`.

- [ ] **A2.5 — wire `on_stop_reason` at the three remaining call sites (§1.4).**
  `grep -rn on_stop_reason src/agent/simulation.py` → no matches. Only the specialist consult
  consumes it, so `thread_reply`, `new_post` and `memory` still take a `refusal`-truncated reply
  and post/persist it as complete. 23 such finals exist across two runs; the klein truncated-memory
  row on disk is one. Pass a recorder at each site and: refuse to overwrite working memory from a
  truncated reply; post a thread reply with an explicit truncation marker rather than silently;
  skip the `new_post`.
  **Trap:** do NOT discard the partial text on `thread_reply` — that converts a mid-word post into
  an EMPTY reply, which increments `empty_response_count` and on a second occurrence abandons the
  interview with an `empty_reply` drop. That is how the zavala interview died.
  Tests: `test_a_truncated_memory_reply_does_not_overwrite_working_memory`,
  `test_a_truncated_thread_reply_is_marked_not_silently_posted`.

### Task A3: the main loop, `--fresh`, and the flush paths

- [ ] **A3.1 — `--fresh` must not delete other runs' history (§2.1). CRITICAL.**
  `src/agent/main.py:174-176` issues three unfiltered `DELETE`s. Run 8b64a0e0 held 1,354 messages
  this morning and now holds 0; **57 of 63 assessments have a `slack_ts` that resolves to no
  message**, so the detail page's interview timeline is empty for 90% of the corpus and no past run
  can be audited. Delete nothing: a new `simulation_run_id` already isolates a fresh run
  everywhere the engine reads, and the reconcile that made wiping necessary is now skipped on
  `--fresh`. If a wipe is still wanted, scope it to the runs being reset — which requires moving it
  AFTER the `SimulationRun` insert.
  **[REVISED — the original trap was misaimed.]** `--fresh` never deletes `thread_decisions`, so
  the two cross-run reads originally flagged are unaffected by removing the wipe. **The read that
  actually blocks "delete nothing" is `_sync_private_channels_from_db`'s select
  (`simulation.py:2365`), which has NO `simulation_run_id` filter** — a fresh run would discover
  every previous run's private channels, join its bots to them, and (via A3.6's `"0"` cursor)
  re-ingest their whole Slack back catalogue. **Scoping that select is a hard prerequisite of this
  task.** Latent today (0 `collab_private` rows), and the fixture for
  `test_a_fresh_run_reads_none_of_a_previous_runs_state` must be a previous run's PRIVATE channel,
  which is the case that currently fails. Every `AgentMessage` read IS run-scoped (verified:
  `:4646, :5137, :5218, :5245, :5381`). `pi_dm_messages` is a dead table — nothing writes it.
  Tests: `test_fresh_does_not_delete_another_runs_messages`,
  `test_a_fresh_run_reads_none_of_a_previous_runs_state`.

- [ ] **A3.2 — the main loop's `continue` paths must not skip the flushes (§2.2).**
  `simulation.py:868` and `:886` jump past `_drain_memory_events` and all three flushes, which have
  no other call site outside `stop()`. Harness: 5 productive ticks, `{'flush_persisted': 0,
  'flush_llm': 0, 'flush_assess': 0, 'drain': 0}`, 5 rows stranded in each buffer. Hoist the drain
  and the three flushes into one helper and call it on every loop exit.
  Test: `test_every_loop_exit_flushes_its_buffers`.

- [ ] **A3.3 — await the background flush task at shutdown (§2.3).** `_on_llm_call` spawns
  `_flush_llm_logs` with `create_task`; that function removes the batch from the buffer BEFORE
  awaiting the commit; `stop()` awaits nothing, so `asyncio.run` cancels it mid-commit. Executed:
  `rows the fake DB saw = 10 | 'COMMITTED' present: False | task state: cancelled`. **[REVISED]** Track spawned tasks in a set and gather them **after
  `set_call_log_callback(None)` and BEFORE the final `_flush_llm_logs()`** — NOT at the top of
  `stop()`, whose first act is `_drain_memory_events`, which makes real LLM calls and can spawn a
  NEW flush task, reintroducing the orphan. Use `return_exceptions=True` (a cancelled task
  re-raises out of `gather` and would abort `stop()` before `_flush_pending_assessments`), and
  discard tasks from the set on completion so it does not grow for the run's life. Also guard
  `_on_flush_done` with `if task.cancelled(): return` — it currently calls `task.exception()` on a
  cancelled task and raises `CancelledError` inside the callback, so the operator sees a traceback
  that says nothing about the lost rows.
  Tests: `test_a_spawned_flush_is_awaited_before_shutdown_completes`,
  `test_a_cancelled_flush_task_does_not_raise_in_its_done_callback`.

- [ ] **A3.4 — one poison row must not lose a whole batch (§2.4).** All three flushers add N rows,
  commit once, and re-queue the batch on failure while logging "re-queued for retry" — but `stop()`
  makes exactly one final attempt, so that message is false and the loss is silent. **[REVISED]** On batch failure, fall back to per-row commits
  **only for row-specific errors (`IntegrityError`, `DataError`) and/or under a wall-clock
  deadline — never on bare `Exception`.** The commonest failure here is the pool-checkout timeout
  `_persist_assessment`'s own comment names; a per-row fallback on that error issues N sequential
  checkouts, and ~15 rows at a 30 s timeout exceeds `docker stop`'s 420 s inside `stop()`, getting
  SIGKILLed and losing the batch PLUS everything not yet flushed. Also: the failed session **cannot
  be reused** — the `except` sits outside the `async with`, so it is already closed and rolled back.
  Open a new session and use `begin_nested()` per row. Also clip `opportunity_assessments.channel_name` with `_bounded_str`, which its
  four sibling columns already get.
  Tests: `test_one_bad_row_does_not_lose_the_batch`,
  `test_a_failed_shutdown_flush_says_lost_not_requeued`.

- [ ] **A3.5 — anchor `last_selected` on every agent-creation path (§1.6).** `start()` anchors it
  once over `self.agents`; `_sync_roster_from_db`'s add path builds `Agent(...)` →
  `AgentState.last_selected = 0.0` and never anchors it. Measured: 3 new agents took **100.0%** of
  2,000 draws (weight ratio 1.79e9). Move the anchor into `Agent.__init__`/`AgentState` so no
  creation path can miss it, and delete the now-redundant loop in `start()`.
  Test: `test_a_mid_run_roster_addition_does_not_monopolise_selection`.

- [ ] **A3.6 — the `--fresh` cursor seed must distinguish empty from failed (§2.13).**
  `get_full_channel_history` swallows `SlackApiError` and returns `[]`, so the seed's own
  `try/except` never fires, `newest` stays `""`, the cursor stays `"0"`, and that channel
  re-imports its whole back catalogue on the first poll (harness: 30 messages ingested). Track
  per-channel success explicitly and, on failure, set the cursor to a wall-clock-derived ts rather
  than leaving `"0"`. Also use `_next_poll_client()` instead of `next(iter(...))` so a disconnected
  first-in-dict client cannot skip the seed entirely while the poller still works.
  Tests: `test_a_failed_history_fetch_does_not_leave_the_cursor_at_zero`,
  `test_the_seed_uses_a_connected_client`.

- [ ] **A3.7 — meter real API calls, not turns (§2.13).** `record_api_call` fires once per
  `generate_*` invocation, so tool rounds inside `generate_with_tools` are invisible: the hub's
  real spend is ~2.5× what the sliding window counts (140 rows / 344 real calls). **[REVISED]** The premise is half-true: `record_api_call` already books **six** sites
  including specialist consults (`tools.py:559`) and truncation retries (`on_retry`, three sites) —
  only the extra TOOL ROUNDS are unbooked. So `len(call_stats)` **double-books every retry** and,
  at the two reserved sites, the reservation too. Use the `kind` discriminator `call_stats` already
  carries (`round`/`final`/`forced_final`/`retry`), or add one `on_api_call` hook per `_acreate` in
  `llm.py` and delete the `on_retry`/`already_reserved` special-casing — noting that touches WS-B's
  file, an ownership collision the plan does not resolve. The rebuild must use
  `COALESCE(jsonb_array_length(call_stats), 1)`: **4,650 of 5,771 rows have `call_stats IS NULL`**
  (the column arrived in 0032), so a bare `jsonb_array_length` collapses the lifetime rebuild and
  LOOSENS the throttle. Note `SimulationRun.total_api_calls` changes units and stops being
  comparable with every historical run.
  **Trap:** `api_call_count` and `call_times` are rebuilt from `llm_call_logs` as one entry per
  ROW. If live booking becomes per-call while the rebuild stays per-row, a restart silently
  loosens the throttle. Change both together, or derive the rebuild from `call_stats` length.
  Test: `test_the_window_counts_every_api_call_in_a_multi_round_turn`.

- [ ] **A3.8 — `MessageLog` self-parented entry (§2.13).** `get_thread_history` pins
  `root = _by_ts[thread_ts]` then extends with `_by_thread[thread_ts]`, so an entry whose
  `thread_ts == ts` is returned twice and `get_thread_message_count` returns 2 for one message —
  and that count IS the interview's turn budget. `normalize_inbound_message` guards the Slack
  ingest path but `_rebuild_state_from_db` and `_hydrate_thread_from_db` copy `thread_ts` verbatim.
  **[REVISED]** Do NOT merely guard the `_by_thread` insertion: `_record` uses
  `thread_ts is None` — not that insertion — to populate the top-level indexes, so a guard-only fix
  leaves a self-parented entry in NEITHER index, invisible to `get_new_top_level_posts` and
  therefore to Phase 3. **Normalise instead:** when `entry.thread_ts == entry.ts`, treat it as a
  root (`thread_ts = None`). That fixes the count, the history, both indexes, and the persisted
  `phase`/`thread_ts` in one place. Also reject
  a falsy `ts` in `append`/`load_entry` with a warning (a `ts=""` entry currently collapses every
  ts-less message into one and is then skipped by `_flush_persisted`, so it reaches neither the log
  nor the DB). 0 rows of either shape exist in prod.
  Tests: `test_a_self_parented_entry_is_not_counted_twice`,
  `test_an_entry_with_no_ts_is_rejected_loudly`.

- [ ] **A3.9 — raise the swallowed Slack poll error (§2.13).**
  `_poll_slack_for_bot_messages`'s `except Exception: logger.debug(...)` covers the entire
  per-channel ingest, and prod runs at INFO, so a channel failing every tick is invisible. Raise
  to `warning`.

- [ ] **A3.10** `./scripts/ci.sh`. No `src/` edits while it runs. Then commit.

---

## Phase 3 — verify, then decide on deploy

- [ ] **P3.1** `./scripts/ci.sh` green on the final tree.
- [ ] **P3.2** Adversarial audit of the implementation (separate dispatch — see below).
- [ ] **P3.3** Deploy is a SEPARATE decision and is NOT part of this plan. When taken:
  build both images → `run --rm blackbird-app alembic upgrade head` → verify
  `alembic current` == `alembic heads` == 0036 → `up -d blackbird-app worker` →
  `--profile agent build agent`. Migrate before serving: the new code maps 0036's columns.

## Deliberately NOT in this plan

- **A unique constraint on `(run, thread_id)`** — `thread_id` is NULL on all 63 historical rows, so
  it must be a partial index, and it should follow one clean run's worth of evidence (A2.4).
- **Re-running the floor over the 12 historical rows** — A1.3 makes them render honestly as
  `unrecorded`, which is true. Recomputing a 2026-08-17 verdict's panel against today's rules
  would invent a result, and §2.1 has already destroyed the messages that would justify it.
- **The rubric threshold re-fit** — still a human calibration decision, unchanged by this plan.
- **Prompt changes** so the hub knows `pass`/`route-to-incubation` now owe a panel and that
  `commercial`/`budget` are requirable. Needs sign-off and a paired doc edit; without it ~21% of
  verdicts will carry a `panel_incomplete` flag for rules the model was never given. **This is the
  most important deferred item** — it is a live divergence, not a latent one.
- **`impersonation is not read-only`** and the three `500`-on-bad-input items (§2.13) — real but
  admin-only and non-corrupting.

## Self-review

**Spec coverage.** Every CONFIRMED finding maps to a task: §1.1→A1.3, §1.2→A2.1, §1.3→B1.1,
§1.4→A2.5, §1.5→B1.2, §1.6→A3.5, §1.7→F1.1+A1.1, §1.8→A2.3, §1.9→C1.1, §2.1→A3.1, §2.2→A3.2,
§2.3→A3.3, §2.4→A3.4, §2.5→A2.4, §2.6→E1.1+E1.4, §2.7→E1.2, §2.8→E1.3, §2.9→C1.3, §2.10→C1.2+A2.2,
§2.11→A1.2+A1.1, §2.12→A1.1, §2.13→B1.3/B1.4/B1.5/B1.6/C1.4/C1.5/C1.6/C1.7/E1.5/A3.6/A3.7/A3.8/A3.9.
The two HYPOTHESIS items (NUL-byte poison, `derive_agent_identity`) are covered incidentally by
A3.4's per-row fallback; neither is proven, so neither gets a dedicated task.

**Ordering.** F1.2 depends on A1.1's column and says so. A2.2 depends on C1.2's marker and says so.
E1.1 has the widest blast radius (every POST test) and is called out. A3.7 must change live booking
and the rebuild together, and says so.

**Largest risk in this plan [REVISED].** Not E1.1 — its test blast radius is three lines. The
largest DESTRUCTIVE risk is **A2.4's DELETE re-keying**, which would silently empty an interview on
every supersession. The largest risk to production is **E1.1 breaking RFC 8058 one-click
unsubscribe**. Both are corrected above.

## Added after the plan audit — must be scheduled, not dropped

These were confirmed by the audit of this plan and belong in a follow-up (they are out of the six
workstreams' file ownership, so they do not belong in this plan's tasks):

- **H1 recurred.** 2 of 15 closes in run 6fb83501 were a PI's own bot ending the hub's live
  interview with zero verdicts — one of them `weeraratna`. `closed_by_role` measures it; nothing
  fixes it. The remedy needs a prompt change with sign-off.
- **The NCBI api_key is still in cleartext logs** — 170 lines in the newest run's log; never rotated.
- **The prompt states the OPPOSITE of the new floor rule in three places**
  (`phase4-thread-reply.md:83`, the mandatory list at `:68-79`, and `:63` +
  `thread_guidance.py:153`). `test_doc_prompt_sync.py` makes this a mechanical two-file edit, not the
  snapshot minefield this plan's constraints imply. Deferring it means ~15-18% of verdicts carry a
  `panel_incomplete` flag for rules the model was never given.
- **7 of the 13 historical panel rows ARE exactly recomputable** from `raw_verdict` +
  `specialist_consults` (five have a demonstrable gap, two are demonstrably clean). The deferral in
  this plan was a rationalisation for those seven; only run `88d81cd8`'s six are unknowable.
- **Job worker:** progress entries after step 4 are silently discarded (`Job.payload` is not
  `MutableDict`); a DB error strands a job in `processing` forever with no lease and no reaper; the
  worker has no `stop_grace_period` while all 10 prod jobs ran 17-74 s; `monthly_refresh` has no
  enqueue site anywhere.
- **ORCID outage is recorded as "no publications"** (`orcid.py:102-109` swallows every transport
  failure), stamping the profile `no_evidence_available` — which tells the operator not to retry.
  CRITICAL.
- **PubMed titles/abstracts truncate at the first `<i>`/`<sup>`** (`Element.text`, not `itertext`):
  36% of abstracts and 12% of titles in a 100-PMID sample. Specified as D6 on 2026-08-13 and never
  implemented; the committed pipeline is a regression against the data currently in production.
- **A profile refresh silently replaces a good profile with a thinner one** (guard is
  `evidence_pub_count == 0` only): Davis 40→24 grounding abstracts, Chute 49→22, from one click.
- **Activating an agent via the documented UI produces a mute bot and then blocks the next restart**
  — `POST /admin/agents/{id}/approve` creates no `CohortMembership`, and under
  `COHORT_DEFAULT_POLICY=isolated` `_validate_star_topology` makes `start()` raise. CLAUDE.md's
  "Adding New PIs" never mentions cohorts.
- **`cohort_memberships` holds 62 memberships for `grantbot`, which has no `agents` row.**
- **Working memory: disk and DB have diverged** and only a `logger.warning` marks it, so
  `profile_revisions` is not a complete history of what the agents read.
- **Commit the per-strand audit reports.** Nine tasks in this plan cite evidence
  (`§F2`, `§F8`, `§M5.7`, `llm-F5`) that exists in no committed file, and the harnesses quoted are
  not re-runnable. Also: no container log covers 2026-08-22 09:30→16:35.
