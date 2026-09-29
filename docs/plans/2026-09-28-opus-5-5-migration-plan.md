# Opus 5 → Opus 5.5 migration — implementation plan

> **Status 2026-09-28: STOPPED at D2 gate 1 (NO-GO).** Task 0 ran partially — 44 of ≈ 249 jobs,
> then a futility stop (not pre-registered; a disclosed deviation). `claude-opus-5-5` answered 1
> of 37 requests (31 replayed production requests + 6 rebuilt profile/review inputs); 36 were
> declined with refusal category `bio` and rescued by `claude-opus-5`, and a no-fallback control
> confirmed the decline is content-driven. Per D2, Phases 1–3 were not executed and no
> application code changed. Evidence, early-stop arithmetic and limits:
> `docs/audits/2026-09-28-opus-5-5-migration/README.md`. Re-run Task 0 if 5.5's biology
> classifier changes.

> **For agentic workers:** execute Phase 1 with `/engineering:plan-execution` (the owner's
> standing policy; this plan keeps the superpowers plan format). Every Phase 1 task is one
> disjoint-file package. **Implementers write code and tests but do not build, run tests,
> lint or commit** — that overrides every "Verify" step inside a task; every "Verify" runs
> once, in Task 8, after all packages merge. Tasks 0, 8b, 9 and 10 are **owner-gated**: 0 and
> 8b spend money, 9 deploys to the live host, 10 runs the live simulation.
> Steps use checkbox (`- [ ]`) syntax.

**Goal:** Every call the repo makes on `claude-opus-5` moves to `claude-opus-5-5` — the three
settings `llm_profile_model`, `llm_agent_model_opus`, `llm_review_model` — with no request
shape the new model rejects, with effort and `max_tokens` set from measurements, with refusal
fallbacks pinned to `claude-opus-5` at today's thinking/effort settings, and with a rollback
that is one `.env` edit plus a container recreate.

**Architecture:** One model-capability constant in `src/services/llm.py`
(`THINKING_ALWAYS_ON_MODELS`) decides, in the single choke point `_acreate`, whether a request
keeps today's shape (`thinking: disabled` by default, non-beta client — Sonnet 5, Opus 5,
anything else) or gets the new one: `thinking: adaptive` plus an explicit
`output_config.effort`, sent through `client.beta.messages.create` with
`betas=["server-side-fallback-2026-07-01"]` and an **array-form** fallback pinned to
`claude-opus-5` whose per-attempt overrides carry today's Opus 5 settings (thinking `adaptive`
when the request carries tools, else `disabled`; effort `high`, Opus 5's default). The tool loop keeps its thinking
blocks valid across the retry and forced-final calls by re-sending the same `tools` with
`tool_choice: none` instead of dropping them, and echoes content with
`model_dump(by_alias=True)`. Per-call `served_model` / fallback / refusal-category / per-attempt
usage go into the existing `llm_call_logs.call_stats` JSONB (no migration), and `_acreate`
logs one WARNING per fallback so the two routes that write no `llm_call_logs` row (profile
synthesis, review bot) are observable too. `tests/fakes.py::FakeAnthropic` learns the new
model's 400s, the SDK's beta/non-beta split, the fallback block's alias and which path each
call took.

**Tech stack:** anthropic SDK **1.8.0** in all three images (`copi-blackbird-app-1`,
`-worker-1`, `-agent-1`, Python 3.11.15) and **0.120.2** in `.venv-test` (Python 3.12.3),
measured 2026-09-28. Postgres 15, schema head `0051` (unchanged).

**Audit record.** Revision 3. It integrates three adversarial audits run 2026-09-28 (two on
revision 1 — code/tests; evidence, Phase 0 and deploy — and one on revision 2's changes).
Every finding is either applied below or recorded as rejected with its evidence ("Audit
dispositions", end of file).

---

## Evidence (measured or read 2026-09-28 unless dated)

### Code

- **E1 — what runs on Opus 5 today.** `.env` sets no `LLM_*` key, so the `src/config.py`
  defaults govern:
  | Setting (`src/config.py`) | Call site | Path | `max_tokens` | Thinking today |
  |---|---|---|---|---|
  | `llm_profile_model` (:324) | `synthesize_profile`, `src/services/llm.py:873` (worker job via `profile_pipeline.py:441,454`) | `_acreate` | 4000 (:876) | disabled (default) |
  | `llm_agent_model_opus` (:326) | thread replies, hub **and** pi_lab, `src/agent/simulation.py:2390` | `generate_with_tools` | 16000 (:2465) | adaptive on tool rounds (`llm.py:1393`); **disabled** on the final-text retry (`:1448`), the forced-final call (`:1571`) and its retry (`:1612`), none of which pass `thinking` or `tools` |
  | 〃 | pi_lab new posts, `simulation.py:3277` | `generate_agent_response` | 3300 (:3300) | disabled |
  | 〃 | specialist consults, `src/agent/tools.py:670` | `generate_agent_response` | 4000 (:708) | disabled |
  | 〃 | `scripts/panel_calibration_ladder.py:305` (`_execute_consult_specialist`) | consult path | 4000 | disabled — its docstring: "Run it on any persona, rubric or model change" |
  | `llm_review_model` (:328) | review bot, `src/services/review_bot.py:701` (worker) | `generate_agent_response` | 8000 (:705) | disabled |
  | `llm_profile_model` | `scripts/generate_sparsedata_user.py:437` | raw `client.messages.create` | 4000 | omitted → adaptive; reads `message.content[0].text` (latent AttributeError on any thinking-first reply, already true on Opus 5) |
  | `llm_review_model` | `scripts/eval_review_bot.py:187` | `llm._acreate` | `MAX_TOKENS = 8000` (:70) | disabled |
  Unaffected (Sonnet 5): memory updates (`simulation.py:8961`), `make_decision`
  (`llm.py:1226`), `email_inbound.classify_reply` (`email_inbound.py:363`). Already on 5.5:
  the assessment chat (its own streaming path). Nothing in `src/` branches on model-name
  strings.
- **E2 — the default that breaks.** `_acreate` does `kwargs.setdefault("thinking", {"type":
  "disabled"})` (`llm.py:376`).
- **E3** — no `tool_choice`, `budget_tokens` or `computer_` anywhere in `src/` or `scripts/`.
- **E4 — the tool loop** echoes content with `b.model_dump()` (`llm.py:1519`), moves one
  `cache_control` marker forward between rounds (`:1539-1543`), re-sends an identical `system`
  string and `tools` object every round, and passes the same mutable `conversation` list to
  every call.
- **E5 — pricing** is in place: `llm_pricing.py` has `claude-opus-5-5` 4 / 20 / 5 / 0.20 and
  `claude-opus-5` 5 / 25 / 6.25 / 0.50. Every Live-tab cost aggregate groups by
  `llm_call_logs.model`, the **requested** model (`simulation_stats.py:376-1127`).
- **E6** — `simulation_runs.config` and the run-start announcement carry no model id.
- **E7 — only the engine writes `llm_call_logs`.** `set_call_log_callback` is installed only by
  `SimulationEngine` (`simulation.py:910`); `review_bot.py:701` passes no `log_meta`. Profile
  synthesis and the review bot therefore leave no per-call record.
- **E8 — the test suite reads the checkout's `.env`** (`Settings(env_file=".env")`,
  `tests/conftest.py:131-141` relies on it for `BASE_URL`). The host's `.env` is also where
  Task 9's rollback writes `LLM_*` keys, so tests must not depend on model defaults via `.env`.
- **E9 — the sampled runs ran today's tools and prompts.** Run `bf6da580` (2026-09-25) was
  built from `bc68025` (hub prompts v1.8.0, rubric v3.5.0, from its run-start announcement);
  `git diff bc68025 HEAD` is empty for `src/agent/tools.py`, `specialists.py`, `agent.py`,
  `src/services/llm.py` and `prompts/roles/`. Run `75ca77f9` (2026-09-22) ran hub v1.7.0 /
  rubric v3.4.0; its `tools.py` differs from HEAD in comments only.

### SDK (0.120.2 source read; 1.8.0 introspected in the agent image)

- **E10** Non-beta `Messages.create` has `thinking`, `output_config`, `tool_choice`,
  `cache_control` and **no `fallbacks`/`betas`**; its content union has **no `fallback`
  block**. `beta.messages.create` has all of them; `fallbacks: Iterable[BetaFallbackParam] |
  Literal["default"]`. `BetaFallbackParam` = `model` (required) plus per-attempt overrides
  `max_tokens`, `thinking`, `output_config`, `speed`, "validated as if the request were made to
  `model`".
- **E11** `BetaFallbackBlock.from_` has alias `from`; `model_dump()` emits `from_`, only
  `model_dump(by_alias=True)` emits `from` (both SDKs). In 0.120.2 it is the only aliased
  field on any response model under `anthropic/types`. The fake's blocks define
  `model_dump(self)` with no parameters (`tests/fakes.py:102,123,143,154`).
- **E12** `stop_details` exists on `Message` and `BetaMessage` (`category`, `explanation`,
  `recommended_model`, `fallback_credit_token`, …). `BetaUsage.iterations` is a union of
  `message`, `fallback_message`, compaction and advisor entries, each with a required `model`
  in 0.120.2; the non-beta `Usage` has no `iterations` field (SDK models allow extras, so one
  may arrive as raw dicts). `input_transformations` exists on `BetaMessage` in 1.8.0 only.
- **E13** The non-streaming `max_tokens` guard (`not is_given(timeout) and self._client.timeout
  == DEFAULT_TIMEOUT`) is bypassed by `_client_for_key`'s own timeout on both clients, both
  SDKs; `_acreate`'s `NonStreamingMaxTokensError` stays the only enforcement.
  `MODEL_NONSTREAMING_TOKENS` has no `claude-opus-5-5` entry.

### Live API probes (our key; scripts and verbatim output in the session scratchpad)

- **E14** `thinking={"type":"disabled"}` → 400 `"thinking.type.disabled" is not supported for
  this model. Use "thinking.type.adaptive" and "output_config.effort" to control thinking
  behavior.` (req_011CfVvZpEfoUPyGNqUzzbRW; also at effort `low`,
  req_011CfVvZqWKRWdW97ebF6g6U). `{"type":"enabled","budget_tokens":1024}` → 400
  `"thinking.type.enabled" is not supported for this model. ...` (req_011CfVvZrMfbtHRTy92Nomhq).
  Omitted `thinking`, `adaptive`, and effort low/medium/high → 200.
- **E15 — thinking-block binding** (round 1 at effort `high`: thinking + text + tool_use):
  | Replay change | `prefix_mismatch_behavior` | Result |
  |---|---|---|
  | none (control) | error | 200 |
  | `tools` omitted, no binding beta | — | **200** (account not enforced by default) |
  | `tools` omitted | error | **400** `...bound to a different conversation... The tools list differs...` (req_011CfVvsDh2qmhKDPuA7ZEtb) |
  | `tools` omitted | drop_block | 200, `thinking_dropped`, `prefix_binding_mismatch` |
  | same `tools` + `tool_choice: none` | error | **200** |
  | `cache_control` removed from an earlier block | error | 200 |
  | effort changed | error | 200 |
  | `system` re-split into two blocks, same text | error | **400** `...The system prompt differs...` (req_011CfVvt3bLeWG7AY7v77FMn) |
  | replayed on `claude-opus-5` | drop_block | 200, `model_binding_mismatch` |
  Today's retry/forced-final shape therefore breaks the binding of every tool round's thinking
  block on 5.5: on our non-enforced account the docs say such blocks are "let through to the
  model" and billed (C5); on an enforced account it is a 400.
- **E16** Echoing `beta` `model_dump()` blocks (`text` with `citations: None`, `tool_use` with
  `caller`/`toolset_name: None`) → 200.
- **E17 — cache** (22,831-token prefix, nearly all `system`): a repeat, `tool_choice: none` and
  effort high→low each read all 22,831 from cache. The docs (C17) say both changes invalidate
  the **messages** level; this probe's messages part was ~15 tokens, too small to show it, so
  the docs govern: effort stays constant across a turn, and the rare `tool_choice: none` call
  accepts one messages-level miss.
- **E18** `fallbacks="default"` + `server-side-fallback-2026-07-01` on non-streaming
  `beta.messages.create` → 200 (also with the binding beta). Models API:
  `allowed_fallback_models ["claude-opus-4-8","claude-opus-5"]`, `max_tokens 128000`.
  **Array form with D4's overrides** → 200 in both variants (single-call `thinking: disabled`;
  tool-loop `adaptive` with `tools` + `tool_choice: none`), also under the `-2026-06-01` beta.
  An unsupported override key → 400 `fallbacks.0: 'temperature' is not a supported field.
  Supported: model, max_tokens, output_config, speed, thinking.` (req_011CfW1WzYHTQrw2Frq7X9aJ);
  a target outside the allowed list → 400 `'claude-sonnet-5' is not a valid fallback target for
  'claude-opus-5-5'. ...` (req_011CfW1X1LQrn8nXyYFTZb4x). On 1.8.0 the `message` entry of
  `usage.iterations` came back with `model` = `None` (0.120.2 types it as required). These
  prompts were trivial, so no fallback fired: this proves the request is **accepted**, not that
  a rescue works (Task 0 measures that). Every 5.5-served probe returned `response.model ==
  "claude-opus-5-5"` exactly.

### Production data (read-only)

- **E19 — 4 of 4 assessment-chat questions fell back.** Every chat question ever asked on
  `claude-opus-5-5` (2026-09-25, ~12 h window, one surface with its own system prompt, streaming)
  was declined by 5.5 and served by `claude-opus-5`: two pre-output, two mid-stream after 61 and
  130 output tokens. Categories were not stored. Motivation for Phase 0, not a measurement of
  the simulation's routes.
- **E20 — Opus 5 baseline** (all `claude-opus-5` rows, 2026-08-19 → 2026-09-25, per call from
  `call_stats`):
  | Route | calls | refusal % | p50 / p95 / max output | max thinking | `max_tokens` stops |
  |---|---|---|---|---|---|
  | consult (all 8) | 1,890 | 4.6 (technologic 9.9, chemistry 8.9, budget 5.8) | 1821 / 2921 / 4000 | 0 (off) | 6 |
  | pi_lab new_post | 195 | 2.6 | 626 / 877 / 1060 | 0 (off) | 0 |
  | pi_lab thread_reply final | 450 | 2.2 | 1522 / 2747 / 4235 | 2503 | 0 |
  | hub thread_reply final | 593 | 6.7 | 848 / 8205 / 12945 | 5451 | 0 |
  | hub tool rounds | 1,174 | 0.4 | 1142 / 3422 / 5491 | 5071 | 0 |
  | pi_lab tool rounds | 291 | 0 | 549 / 1456 / 2349 | 2204 | 0 |
  | hub forced_final | 2 | 0 | 362 / 630 / 630 | 0 | 0 |
  `llm.py:28-29`: `max_tokens=16000` at the observed 60.7 tok/s "authorises up to 264 s".
  Least-squares fit of per-call `latency_ms` on `output_tokens` over the same rows: pooled over
  calls with ≥ 2,000 output tokens, **12.36 ms/token (80.9 tok/s), intercept 9.5 s**, r² 0.49
  (n = 1,229); per route 53–78 tok/s. `latency_ms` is timed around `await _acreate(...)`
  (`llm.py:1003-1011`, `:1373-1395`), which includes the API semaphore wait (`:398`) and SDK
  retries, so production latencies over-state generation time (max 638 s on a consult). A clean
  like-for-like Opus 5 comparison therefore comes from Task 0's control arm, not from these rows.
- **E21** Latest run `bf6da580`: 5 threads, 153 consults (12–29 per persona), 27 hub and 24
  pi_lab thread_reply rows, 5 new_post rows, 8 hub rows whose reply carries `<assessment_json>`;
  run `75ca77f9`: 156 consults, 6 sidecar rows. Consult input ≈1.4–1.6k tokens/call, new_post
  ≈14k, pi_lab thread_reply ≈25k/turn, hub ≈162k/turn (cumulative).

### Documentation

- **E22** Official-docs verification is the table at the end (C1–C18). A claim marked
  PARTIAL or NOT FOUND is treated as unverified: the plan acts on it only through a Phase 0
  measurement, a probe, or a test, never by assumption.

---

## Owner decisions (recommended default first)

- **D1 — Phase 0 spend.** Approve Task 0 `--apply` with a hard ceiling of **$55** (expected
  ≈$37 incl. the Opus 5 latency control arm; ≈$48 if 5.5 declines everything and Opus 5 re-runs
  it). Task 8b (two calibration-ladder runs, ≈96 consult calls) ≈ $10 more.
- **D2 — Go / no-go.** Three gates, pre-registered here so the numbers cannot be fitted after
  the fact. Sample sizes bound what gate 1 can prove (one-sided 95 % upper bound: 0/15 ≈ 18 %,
  0/20 ≈ 14 %), so it can **stop** a bad migration but cannot certify a good one; gate 3 finishes
  the job. Small-n routes are gated on counts, not rates, so one event is not a verdict.
  1. **Phase 0 screen (before Phase 1).**
     - *Lost* (final `stop_reason == "refusal"` after the fallback): routes with n ≥ 30 —
       rate ≤ `max(2 × E20 rate, 5 %)`; routes with n < 30 — at most 1 lost call; profile
       synthesis and the review bot (n = 3, and a lost call there is a failed job) — 0.
     - *Served by 5.5* (derived from `usage.iterations`/fallback blocks, not the model string):
       ≥ 80 % per route (≥ 16 of 20; ≥ 8 of 10) and ≥ 60 % per consult persona (≥ 9 of 15).
     - *Parse success* ≥ the Opus 5 baseline on the same inputs, pairwise: consults, new_post
       and hub finals against the stored `response_text`; first requests against the Opus 5
       first call's own outcome (`call_stats[0]`: `kind`, `stop_reason`, `block_types` —
       NOT the stored `response_text`, which is the turn's final text); profile synthesis
       against `_validate_profile` over the PI's stored profile; the review bot against the
       2026-09-02 Opus 5 evaluation (`docs/audits/2026-09-02-review-pipeline/eval-results.json`).
     - Zero `reasoning_extraction` on thread_reply (R6); every ceiling passes Task 5's rule.
  2. **Pre-deploy (Task 8b).** Two calibration-ladder runs on 5.5, pooled, against the spec's
     Opus 5 pooled figures (`docs/specs/2026-08-28-specialist-verdict-vocabulary-design.md`
     §7.2 and `:176-184`): `WEAK` tier `blocking`+`gap` ≥ 85 %, pooled R ≥ 0.5625 (Opus 5's two
     runs measured 0.531 and 0.594; a single run gated at 0.594 would fail an equally good model
     about half the time).
  3. **First live run (Task 10)** stop rule, below.
  A no-go at gate 1 stops the plan before Phase 1 (nothing is implemented); the owner may
  instead choose D6's staged rollout for the routes that passed.
- **D3 — Effort.** `llm_opus_effort = "low"` for every single-call route (they ran thinking
  **off** on Opus 5) and `llm_opus_tool_effort = "medium"` for the tool loop (5.5's default;
  Opus 5's was `high`, C2). Phase 0 arm B measures consults at `medium` on disjoint rows. A
  tuning decision; the owner's.
- **D4 — Fallbacks.** Array form, pinned: `fallbacks=[{"model": "claude-opus-5", "thinking":
  {"type": "adaptive"} if the request carries tools else {"type": "disabled"},
  "output_config": {"effort": "high"}}]`. A rescued call then runs on Opus 5 with today's
  thinking and effort (explicit `high` equals omitting it, C17), on the Opus 5 rate-limit
  pool, and cannot land on `claude-opus-4-8` (listed first in E18). It is **not** byte-for-byte
  today's call: it inherits any `max_tokens` Task 5 raised; inside a tool loop the earlier
  rounds' 5.5 thinking blocks are dropped for the Opus 5 attempt without an error (C9), so the
  rescuer lacks that reasoning; Opus 5's cache starts cold; and the tool loop's
  retry/forced-final calls now carry `tools` (+ `tool_choice: none`), so their rescue keeps
  thinking ON — Opus 5 with tools and thinking off can write a tool call as text
  (`llm.py:1383-1387`). Kill switch `LLM_REFUSAL_FALLBACKS=false`.
  Alternative: `"default"` (routes by category; target not ours to pick). Without any
  fallback, every 5.5 decline becomes a refusal the engine treats as a truncated turn (E20:
  147 of those on Opus 5 alone).
- **D5 — Cost accounting.** Record-only: `call_stats` gains `served_model`, `fallbacks`,
  `refusal_category` and per-attempt `iterations`. The Live tab keeps pricing a row's returned
  `usage` at the requested model's rates, which for an Opus 5-served call under-states input and
  output by 20 % and cache reads by 60 % ($0.20 vs $0.50), and misses the declined 5.5 attempt,
  which is billed when it produced output (C8; whether a pre-output decline is billed is
  unresolved, see the side finding). Repricing from `iterations` touches every aggregate in
  `simulation_stats.py:376-1127` and is a separate change; the data it needs starts being kept
  now.
- **D6 — Staged rollout (optional).** The three settings are independent `.env` keys; the code
  can ship with, e.g., `LLM_AGENT_MODEL_OPUS=claude-opus-5` pinned and the simulation moved
  later. Default: all three together.

---

## Global constraints

- No migration (head stays `0051`). No prompt, persona, rubric or `role.toml` edit: no
  prompt-set bump, no `scripts/sync_prompt_set_docs.py`, and
  `tests/characterization/__snapshots__/` must not change (never `--snapshot-update`).
- No dependency or pin change.
- **Rollback guarantee.** For every model not in `THINKING_ALWAYS_ON_MODELS`, the request is
  identical to today's — same kwargs, same non-beta client — **except two model-independent
  changes**: the `max_tokens` literals Task 5's rule may raise (consult, new_post, review bot,
  thread_reply, and profile synthesis at `llm.py:876`), and `scripts/generate_sparsedata_user.py`
  sending `thinking: disabled` for a non-member instead of omitting it (Task 6). Task 5's rule
  keeps every raised value, and its doubled truncation retry, inside the 300 s read timeout on
  Opus 5; Task 9 lists the changed literals.
- Tests never depend on `.env` model values (E8): settings tests use `Settings(_env_file=None)`;
  behaviour tests pass `model=` explicitly and override settings with
  `monkeypatch.setattr("src.services.llm.get_settings", lambda: base.model_copy(update={...}))`.
- Every `max_tokens` stays an integer literal at its call site (the AST scan in
  `tests/unit/test_llm_nonstreaming_ceiling.py`) and ≤ 21,333.
- ruff: zero findings in `tests/**`; no new findings in `src/`. Rules `E, F, I, UP, B`
  (py311); the traps here are B905 (`zip(strict=)`), B023 (bind loop variables into lambdas:
  `lambda fake=fake: fake`), E731 (no assigned lambdas), B017 (no `pytest.raises(Exception)`).
- Tests run on the host only (`.venv-test/bin/python -m pytest …` over `ssh -4`), never
  through the sshfs mount; `./scripts/ci.sh` is the gate.
- Commits only when the owner asks, staging exact paths; never touch
  `docker-compose.prod.yml`'s uncommitted edit.

## File map

| File | Task | Change |
|---|---|---|
| `scripts/dev/opus55_replay.py` (new) | 0 | measurement replay, read-only |
| `tests/unit/test_opus55_replay.py` (new) | 0 | pure helpers |
| `docs/audits/2026-09-28-opus-5-5-migration/README.md` (new) | 0, 8b | numbers and verdicts |
| `src/config.py` | 1 | three defaults; two effort settings + validator; fallback switch; comment block |
| `tests/unit/test_review_bot_inputs.py` | 1 | `:29` default |
| `tests/unit/test_llm_model_settings.py` (new) | 1 | defaults, Literal pin, validators, rollback value |
| `src/services/llm.py` | 2 | constants, `thinking_defaults`, `_acreate`, tool loop, `_call_stat`, fallback WARNING, profile `max_tokens`, comments |
| `src/services/email_inbound.py` | 2 | comment at `:316` only |
| `src/services/simulation_stats.py` | 2 | `llm.py:N` citations at `:187`, `:191` (comments only) |
| `tests/fakes.py` | 3 | 5.5 400s, beta path, `call_paths`, fallback/sticky/refusal helpers, `by_alias` on every block |
| `tests/unit/test_fakes.py` | 3 | the fake's new behaviour |
| `tests/characterization/test_profile_pipeline_gm.py` | 3 | `_BoomClient` gains `.beta` |
| `tests/unit/test_simulation_stats.py` | 3 | `llm.py:114` citation at `:836` (comment only) |
| `tests/unit/test_llm_opus55.py` (new) | 4 | every new `llm.py` behaviour; non-member identity |
| `src/agent/tools.py`, `src/agent/simulation.py`, `src/services/review_bot.py` | 5 | `max_tokens` literals + comments that become false |
| `tests/unit/test_review_bot.py`, `tests/unit/test_llm_nonstreaming_ceiling.py` | 5 | pinned values; a 5.5 variant of the ceiling regression |
| `scripts/generate_sparsedata_user.py`, `scripts/eval_review_bot.py` | 6 | thinking defaults + `_all_text`; `MAX_TOKENS` mirror |
| `CLAUDE.md` | 7 | review model, SDK section, deploy box, shifted citations |

---

## Phase 0 — measure before changing anything (owner-gated: D1)

### Task 0: `scripts/dev/opus55_replay.py`

**Why:** E19 says 5.5 may decline this corpus; E20 has no thinking data for four of five
routes and no baseline at all for profile synthesis and the review bot. D2 gate 1, Task 5's
ceilings and D3 all come from this task.

- [x] **Step 1 — shape probe.** Done 2026-09-28 (E18): both D4 variants → 200. Re-run it
  as the script's first call (cents) so the report carries its own evidence; if it ever
  400s, D4 falls back to `"default"` and the rest of Task 0 uses that.
- [x] **Step 2 — read-only access.** `scripts/eval_review_bot.py::_readonly_engine` (READ
  ONLY at the wire level). No `set_call_log_callback`, no DB writes, no job enqueue, no Slack.
- [x] **Step 3 — sample** (deterministic, stratified; record each row's run and that run's
  prompt-set/rubric stamps from `simulation_runs.config.run_start_announcement`):
  - consults, arm A: 15 per persona (120), from `bf6da580` first then `75ca77f9`, spread over
    distinct `thread_ts` and `message_ordinal`, kept in production order per persona;
  - consults, arm B (effort `medium`): 5 per persona (40) from rows NOT in arm A (so sticky
    routing from arm A cannot touch them, C16), run after arm A;
  - `new_post` (pi_lab): 10 (all 5 of `bf6da580` + 5 of `75ca77f9`);
  - `thread_reply` first request, pi_lab 20 and hub (`agent_id = 'blackbird'`) 20, stratified
    over `thread_phase`: `messages_json` up to the first `assistant` message (for these rows it
    is the single user message, `src/agent/agent.py:461`), the stored `system_prompt` (stored
    raw, before `_cacheable_system`), `tools = tools_for_role(role)` (E9: unchanged since
    `bc68025`);
  - hub `thread_reply` final: 10 rows whose stored reply contains `<assessment_json>`, from
    turns NOT used for the first-request sample; full stored conversation with `tools`, and
    `thinking.block_binding.prefix_mismatch_behavior = "drop_block"` (beta
    `thinking-binding-controls-2026-08-01`) since the stored thinking blocks are Opus 5's;
  - forced-final shape: 3 further sidecar rows (from turns used nowhere else, so sticky
    routing from another replay cannot reach them, C16) in the new shape (same `tools` +
    `tool_choice: none`); record how often the text comes back empty;
  - **Opus 5 latency control arm:** 10 consults and 10 hub first requests from the arm-A rows,
    re-sent on `claude-opus-5` in TODAY's shape (thinking disabled / adaptive as in E1, no
    effort, non-beta client), interleaved with the 5.5 calls — the like-for-like `a_o5`/`b_o5`
    Task 5 needs (E20's production latencies include queueing);
  - profile synthesis: 3 PIs; context from `profile_pipeline._build_synthesis_context` with
    all four inputs spelled out — the ORCID fields the pipeline uses (institution, department,
    lab website) from the stored user/profile rows, `grant_titles` as stored now (note: now
    RePORTER-supplemented, so larger than at the original synthesis), publications selected as
    the pipeline does (tenure filter, abstract filter, year rank, 50 cap), and
    `Publication.methods_text`; record the context length; user message exactly as
    `llm.synthesize_profile` builds it;
  - review bot: cases `baseline_scientific_gap`, `band_recommendation_mismatch`,
    `rubric_calibration` from `scripts/review_bot_eval_cases.json` via `eval_review_bot._build`
    (dry-run first: `_build` uses `scalar_one()`, so a missing assessment aborts the case).
- [x] **Step 4 — request shape = the post-migration shape**, built in the script (Task 0 does
  not wait on Task 2): `client.beta.messages.create(model="claude-opus-5-5",
  system=llm._cacheable_system(system_prompt), thinking={"type": "adaptive"},
  output_config={"effort": E}, betas=["server-side-fallback-2026-07-01"], fallbacks=<D4 form
  from Step 1>, max_tokens=M, …)`, `E = "low"` for single-call routes and `"medium"` for
  thread_reply, `M = min(2 × current ceiling, 21333)` so truncation does not censor the size
  distribution — except thread_reply, which keeps 16000 (doubling it would push a full-length
  reply past the 300 s read timeout, E20). Client:
  `llm._client_for_key(key).with_options(max_retries=0)` — the SDK's 2 default retries would
  re-bill timeouts invisibly.
- [x] **Step 5 — record per call, metrics only (no prompt or response text):** row id / case
  name, route, arm, run, `served_model = response.model` (and assert it equals
  `"claude-opus-5-5"` on every call with no fallback block and no `fallback_message` — E18
  observed this; served-by is still DERIVED from `iterations`/fallback blocks), each `fallback`
  block `{from, to, category}`, a **sticky** flag (`iterations` has a `fallback_message` and no
  `message` for 5.5, no fallback block, C16), `stop_reason`, `stop_details.category`,
  `recommended_model`, presence of `fallback_credit_token`, `output_tokens`, `thinking_tokens`,
  text tokens, top-level `usage` AND each `iterations` entry, latency s, cost per iteration via
  `llm_pricing.cost_for_tokens`, `input_transformations`, and `parse_ok`: consult →
  `specialists.has_usable_content` and `parse_opinion`; profile → `llm._extract_json` +
  `profile_pipeline._validate_profile`; review → `review_bot._parse_model_output` target valid;
  hub final → `simulation._extract_assessment_json` succeeds; first requests → a `tool_use`
  block, or text holding a complete `<slack_message>…</slack_message>`; plus a separate count of
  first requests ending `end_turn` with text but neither (R8). **Baselines, free** (D2 gate 1
  defines them): the same parsers over the sampled rows' stored Opus 5 `response_text` for
  consults, new_post and hub finals; the Opus 5 first call's `call_stats[0]` (`kind`,
  `stop_reason`, `block_types`) for first requests; `_validate_profile` over the 3 PIs' stored
  profiles; the graded 2026-09-02 Opus 5 review-bot evaluation for the review cases.
- [x] **Step 6 — fallback echo check.** Build one assessment's staff chat record with
  `assessment_chat_record.load_chat_record` (read-only) — the content that fell back 4 of 4 in
  E19 (there: streaming, citation documents, `"default"`; here: non-streaming, one dummy tool,
  the Step 1 form, so it may behave differently) — and ask "summarize this assessment". If
  content holds a `fallback` block, send a follow-up with the content echoed via
  `model_dump(by_alias=True)`, a synthetic `tool_result` if a `tool_use` came back, and
  `prefix_mismatch_behavior: "error"`; record 200/400. If nothing falls back, the echo rule
  stays unverified live and D2 says so.
- [x] **Step 7 — rate limits and guards.** One `with_raw_response` call each on
  `claude-opus-5-5` and `claude-opus-5`; record every `anthropic-ratelimit-*` header. Guards:
  dry run by default (sample counts only, nothing leaves the host); `--count-tokens` sends
  prompts to `messages.count_tokens` for an input estimate (generates nothing); `--apply`
  spends. `--max-calls` derived from the sample plan (≈255 incl. the control arm and Steps 1,
  6, 7). Routes run
  round-robin so an abort truncates every route evenly. Running cost aborts at $55, and any
  exception after a request was sent (timeout, connection error) is charged its worst case
  (input + `M` output at both models' rates) and recorded as a data point, not dropped.
- [x] **Step 8 — tests** (`tests/unit/test_opus55_replay.py`): aggregation (served-by share,
  sticky share, per-persona rates, p50/p95/max, the latency fit and Task 5's rule) on
  hand-built records; the first-request slicer on a stored-shape conversation; no client call
  without `--apply` or `--count-tokens`.
- [x] **Step 9 — run** (owner go-ahead) in a one-off app container off the deployed image
  (SDK 1.8.0) with the tree's code mounted, per `scripts/eval_review_bot.py:15-17`:
  `docker compose -f docker-compose.prod.yml run --rm -T --no-deps -e PYTHONPATH=/app -v "$PWD/src:/app/src:ro" -v "$PWD/scripts:/app/scripts:ro" -v "$PWD/docs:/app/docs" blackbird-app python scripts/dev/opus55_replay.py [--count-tokens | --apply]`.
  Report: `docs/audits/2026-09-28-opus-5-5-migration/replay-<UTC>.json`.
- [x] **Step 10 — README**: the E20-style table for 5.5, served-by/sticky/lost per route and
  persona with 95 % intervals and raw counts, categories, the pairwise parse table, the
  latency fits for both models, echo result, rate-limit headers, Task 5's numbers, and the D2
  gate-1 verdict. If any array-form rescue errored or was lost, re-run exactly those rows with
  `fallbacks: "default"` before judging gate 1, and report both. Stop for the owner.

Cost (input at $4, or $5 on a cache write; output $20; fallback re-runs and the control arm at
Opus 5's $5 / $6.25 / $25): consults 160 × ≈$0.08 = $12.8; new_post 10 × $0.09 = $0.9; first
requests 20 × $0.14 + 20 × $0.30 = $8.8; hub finals 13 × $0.56 = $7.3; profile 3 × $0.2;
review 3 × $0.35; Opus 5 control arm 10 × $0.10 + 10 × $0.30 = $4.0; Steps 1/6/7 ≈ $0.5 →
**≈ $37**; all-fallback worst case ≈ $48; cap $55.

**Task 5 ceiling rule** (applied per route — consult, new_post, review bot, profile synthesis,
thread_reply — to Phase 0 output):
- **Latency model, per model m ∈ {5.5, Opus 5 control}:** `L_m(route, C) = a_m(route) + b_m · C`.
  `b_m` is fitted POOLED across routes on calls with ≥ 2,000 output tokens (a per-route set can
  be empty: new_post's Opus 5 max was 1,060); `a_m(route)` is the route's **maximum** residual
  `latency − b_m · output_tokens` (a worst case, not a median; a route with no calls takes the
  pooled maximum residual). Decline time `D(route)` = the maximum, over the route's
  fallback-served calls, of `latency − L_o5(route, output_tokens)`; with no fallback on the
  route, the route's maximum 5.5-served latency.
- `new = max(current, roundup_500(1.5 × max observed output_tokens))`, subject to:
  (i) `new ≤ 21,333`;
  (ii) `L_55(new) ≤ 270 s`, **or** `new == current` and `L_55(current) ≤ L_o5(current)` (no
  regression against Opus 5 measured the same way);
  (iii) rollback safety, when `new > current`: `L_o5(new) ≤ 300 s`;
  (iv) the truncation retry, when `new > current`: `L_55(r) ≤ 300 s` and `L_o5(r) ≤ 300 s`
  for `r = min(2 · new, 21,333)` (`_retry_budget`, `llm.py:492-524`) — thread_reply's clamped
  21,333 retry is already an accepted, WARNING-logged risk (`llm.py:30-34`);
  (v) a rescued call: `D(route) + L_o5(new) ≤ 300 s`.
  A failure of (i)–(iv) is a no-go for that route under D2; a failure of (v) is reported as an
  owner-accepted risk (a rare decline on a near-maximal reply times out), not a no-go.
- **thread_reply overrides the formula: it stays 16,000** unless 5.5's max observed hub output
  reaches 13,000 (81 %), then the owner decides (streaming is the real fix and is out of scope).
  With ≈ 13 hub finals that trigger has little power (Opus 5's p95 was 8,205); Task 10's
  `max_tokens`-stop rule is the real guard.
- Task 0 Step 8 tests the rule on hand-built records: 5.5 with Opus 5's `a`/`b` passes
  thread_reply at 16,000; 5.5 20 % slower fails it; a route with an empty ≥ 2k set; a route
  with zero fallbacks; a raised review ceiling whose doubled retry projects past 300 s on the
  hand-built fit fails (iv) (at 60.7 tok/s that is any ceiling ≥ 9,000; at E20's pooled 80.9
  tok/s, ≥ ≈11,700 with a 10 s intercept).
- Do not relax the rule to fit the data.

---

## Phase 1 — code (parallel packages; start only after the D2 gate-1 go)

### Task 1: settings — `src/config.py`

- [ ] Defaults: `llm_profile_model`, `llm_agent_model_opus`, `llm_review_model` →
  `"claude-opus-5-5"`. Leave `llm_agent_model`, `llm_agent_model_sonnet`,
  `llm_assessment_chat_model`.
- [ ] Add beside them:
  ```python
  # Thinking effort for requests on llm.THINKING_ALWAYS_ON_MODELS (claude-opus-5-5), which
  # cannot run thinking off; ignored for every other model. Literals, not str, so a typo is
  # caught by _known_llm_effort rather than 400-ing every request.
  llm_opus_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"        # single calls
  llm_opus_tool_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"  # tool loop
  # Server-side refusal fallback on those same requests (llm._acreate). Kill switch.
  llm_refusal_fallbacks: bool = True
  ```
  and `@field_validator("llm_opus_effort", "llm_opus_tool_effort", mode="before")`
  `_known_llm_effort(cls, value, info)`: a value in `ASSESSMENT_CHAT_EFFORTS` passes; anything
  else logs one WARNING and returns `cls.model_fields[info.field_name].default` (precedent:
  `type(self).model_fields[name].default`, `src/config.py:540`).
- [ ] Rewrite the "LLM models" comment block (`:307-323`): 5.5 cannot run thinking off (E14);
  effort is the control; `llm.THINKING_ALWAYS_ON_MODELS` decides the request shape; the three
  settings are the rollback lever (`.env` + recreate, Task 9).
- [ ] `tests/unit/test_review_bot_inputs.py:29` → `"claude-opus-5-5"`.
- [ ] New `tests/unit/test_llm_model_settings.py` (all on `Settings(_env_file=None)`): the three
  defaults; both effort defaults; `get_args(annotation) == ASSESSMENT_CHAT_EFFORTS` for both new
  fields (as `test_assessment_chat_settings.py:16-18` does); an unknown effort falls back to
  that field's own default with one WARNING (`caplog`); `llm_agent_model_opus="claude-opus-5"`
  accepted; `llm_refusal_fallbacks` parses `"false"`.
- [ ] `tests/unit/test_config_secret_redaction.py` needs no edit: its sweep keeps only
  `annotation is str` fields (confirm at implementation).

### Task 2: the request shape — `src/services/llm.py` (+ comment-only edits in `email_inbound.py:316`, `simulation_stats.py:187,191`)

Interfaces other tasks rely on: `THINKING_ALWAYS_ON_MODELS: frozenset[str]`,
`SERVER_SIDE_FALLBACK_BETA: str`, `REFUSAL_FALLBACK_MODEL: str`,
`thinking_defaults(model: str | None, *, effort: str) -> dict[str, Any]`.

- [ ] **Constants** after `NONSTREAMING_MAX_TOKENS`, each commented with its evidence:
  ```python
  # Models that REJECT thinking off — `{"type": "disabled"}` and `{"type": "enabled", ...}` are a
  # 400 at every effort level (probed on our key 2026-09-28; E14 of
  # docs/plans/2026-09-28-opus-5-5-migration-plan.md). Membership moves a request onto the
  # adaptive + effort + fallback shape; every other model keeps today's request exactly. Add a
  # model only on the same evidence.
  THINKING_ALWAYS_ON_MODELS: frozenset[str] = frozenset({"claude-opus-5-5"})
  # The beta that carries `fallbacks` (it accepts both the list and the "default" form).
  SERVER_SIDE_FALLBACK_BETA = "server-side-fallback-2026-07-01"
  # Where a declined request is re-run. One of the model's `allowed_fallback_models`
  # (Models API, 2026-09-28: claude-opus-4-8, claude-opus-5); Opus 5 because the override
  # below then reproduces the request this repo sent before the migration.
  REFUSAL_FALLBACK_MODEL = "claude-opus-5"
  ```
- [ ] **`thinking_defaults`** (pure): member → `{"thinking": {"type": "adaptive"},
  "output_config": {"effort": effort}}`; else `{"thinking": {"type": "disabled"}}`.
- [ ] **`_acreate`** — in this order, without mutating any caller-owned dict:
  1. `settings = get_settings()`; `model = kwargs.get("model")`;
     `member = model in THINKING_ALWAYS_ON_MODELS`.
  2. Non-member: `kwargs.setdefault("thinking", {"type": "disabled"})` (today's line, so the
     non-member request is unchanged). Member: `kwargs.setdefault("thinking", {"type":
     "adaptive"})` and `kwargs["output_config"] = {"effort": settings.llm_opus_effort,
     **kwargs.get("output_config", {})}` (a caller's explicit effort wins; read
     `llm_opus_effort` only here).
  3. System caching and the `NONSTREAMING_MAX_TOKENS` check exactly as today — BEFORE touching
     `client.beta`, so an oversized request still raises `NonStreamingMaxTokensError` (the
     no-row rule) on any client.
  4. `create = client.messages.create`; if `member and settings.llm_refusal_fallbacks`:
     `kwargs["betas"] = [*kwargs.get("betas", ()), SERVER_SIDE_FALLBACK_BETA]`,
     `kwargs["fallbacks"] = [{"model": REFUSAL_FALLBACK_MODEL, "thinking": {"type":
     "adaptive"} if kwargs.get("tools") else {"type": "disabled"}, "output_config":
     {"effort": "high"}}]` (D4; `"default"` if Task 0 Step 1 rejected the array form),
     `create = client.beta.messages.create`.
  5. Run `create` through the existing executor/semaphore.
  6. After the response: if `member` and (a `fallback` block, or a `fallback_message`
     iteration — the sticky case, C16), log ONE WARNING naming the requested and served model
     and each fallback category — this is the only trace for the two routes without
     `llm_call_logs` rows (E7). Detection is structural, not a model-string comparison (an id
     can come back as an alias); `response.model` is only reported. Never raise from this
     block.
  Rewrite the docstring's thinking paragraph (why the default depends on the model; why a
  member gets effort explicitly — 5.5's own default is `medium`, C2; why fallbacks force the
  beta client, E10; why the rescue mirrors today's request).
- [ ] **`generate_with_tools`** — once, after `model` resolves:
  ```python
  always_on = model in THINKING_ALWAYS_ON_MODELS
  loop_effort = {"output_config": {"effort": settings.llm_opus_tool_effort}} if always_on else {}
  # The final-text retry, the forced-final call and its retry re-send the SAME tools with
  # tool_choice "none": dropping `tools` changes the prefix every tool round's thinking block is
  # bound to (E15). Effort stays the rounds' so the messages cache survives (C17).
  no_tools = ({"tools": tools, "tool_choice": {"type": "none"}}
              if always_on and tools else {}) | loop_effort
  ```
  (`| loop_effort` outside the conditional, so a member's no-tools calls keep the rounds' effort
  even with `tools=[]`.)
  Round call (`:1374`): add `**loop_effort`. The no-tools calls (`:1448`, `:1571`, `:1612`):
  add `**no_tools`. Non-members get today's kwargs exactly. Update every comment this makes
  false: `:1381-1393` ("The truncation retry below deliberately does NOT set this"), `:739`
  (`forced_final` = "the no-tools call"), `:1553` ("force a final call without tools"),
  `_retry_budget`'s docstring and WARNING text ("the room freed by dropping tools and thinking"
  — make it conditional on membership), `_thinking_tokens` ("the one call site running adaptive
  thinking").
- [ ] **Echo** (`:1519`): `[b.model_dump(by_alias=True) for b in message.content]`. The docs
  say to send content back "as you received it" and keep a `fallback` block "exactly where it
  appeared" (C15); on a non-streaming request the declined partial output is omitted and the
  fallback block comes first (C18), so nothing needs filtering. `by_alias` changes only the
  fallback block's `from_` → `from` (E11) and drops no field, so every other echoed dict is
  unchanged (C5: a serializer that drops empty fields edits the prefix). Confirm the aliases on
  1.8.0 in Task 9's smoke.
- [ ] **`_all_text`**: unchanged; one docstring sentence citing C18.
- [ ] **`_call_stat`** additions, all via `getattr`, tolerant of dict entries and of a `None`
  iteration `model` (E12, E18):
  `served_model`, `fallbacks` (`[{"from", "to", "category"}]` or `None`), `refusal_category`
  (`stop_details.category` when `stop_reason == "refusal"`), `recommended_model`, `iterations`
  (`[{"type", "model", "input_tokens", "output_tokens", "cache_read_input_tokens",
  "cache_creation_input_tokens"}]` or `None`). Document that `llm_call_logs.model` stays the
  requested model and the Live tab prices by it (D5); that a sticky-routed call has no fallback
  block and is identified by `served_model`/`iterations` (C16); that the non-beta
  (fallbacks-off) path has no `iterations`.
- [ ] `synthesize_profile` `max_tokens` (`:876`): Task 0's profile value.
- [ ] Module comment on `NONSTREAMING_MAX_TOKENS`: add "1.8.0 re-verified 2026-09-28" (E13).
- [ ] Comment-only: `email_inbound.py:316` ("`_acreate` defaults thinking off" — now only for
  non-member models); `simulation_stats.py:187` (`llm.py:783-786`) and `:191` (`llm.py:114`) →
  the lines they now point at.

### Task 3: the fake — `tests/fakes.py`, `tests/unit/test_fakes.py`, two comment/stub fixes

Everything the fake refuses is re-derived from the probes and docs, not imported from `src`
(the `_MAX_NONSTREAMING_MAX_TOKENS` precedent), so a wrong constant in `src` fails a test
instead of both being wrong together.

- [ ] **Every existing block's `model_dump` accepts the SDK's keyword** —
  `def model_dump(self, *, by_alias: bool = False, **_: Any) -> dict` on `_TextBlock`,
  `_ThinkingBlock`, `_RedactedThinkingBlock`, `_ToolUseBlock`, output unchanged. Without this,
  Task 2's echo raises `TypeError` in every multi-round tool-loop test.
- [ ] `_THINKING_ALWAYS_ON_MODELS = frozenset({"claude-opus-5-5"})` with E14's request ids.
- [ ] `api_status_error(cls, status, message=...)`: optional message, default unchanged.
- [ ] One validation function, run by both create paths BEFORE recording (the ceiling's rule):
  member + `thinking.type in {"disabled", "enabled"}` → `BadRequestError` with E14's text;
  member + `tool_choice.type in {"any", "tool"}` → `BadRequestError` `tool_choice: type "tool"
  and "any" are not supported for this model.` (C4).
- [ ] `_Messages.create`: `TypeError` on a `fallbacks` or `betas` kwarg (E10).
- [ ] `FakeAnthropic.beta.messages.create`: same validation; `fallbacks == "default"` needs
  `server-side-fallback-2026-07-01` in `betas`, a list needs `-07-01` or `-06-01`, else
  `BadRequestError` (C8); a list entry with keys outside `model`, `max_tokens`, `thinking`,
  `output_config`, `speed` → `BadRequestError` with E18's text; a list entry whose `model` is
  not in the fake's own `{"claude-opus-4-8", "claude-opus-5"}` for a `claude-opus-5-5` request →
  `BadRequestError` with E18's text; member-model validation also applied to each entry's
  overrides "as if the request were made to `model`".
- [ ] `FakeAnthropic.call_paths: list[str]`, appended `"messages"` / `"beta"` in step with
  `calls`, so tests can prove which client ran without changing the kwargs dicts.
- [ ] `_Message` gains `model: str | None = None`, `stop_details: Any = None`; `_next` sets
  `model` to the request's model when unset.
- [ ] `_FallbackBlock(from_model, to_model, category)`: `type="fallback"`, `from_`, `to`,
  `trigger` (`SimpleNamespace`s), `model_dump(*, by_alias=False, **_)` returning **plain nested
  dicts** (so echoed conversations stay JSON-serialisable for integration tests that store
  them) and emitting `from_` unless `by_alias=True` (E11).
- [ ] `_Usage` gains an explicit `iterations: list | None = None` field (entries as
  `SimpleNamespace(type=, model=, input_tokens=, output_tokens=, cache_read_input_tokens=,
  cache_creation_input_tokens=)`); the beta `create` honours `latency` exactly as the non-beta
  one does (`time.sleep`, so off-loop tests keep their meaning).
- [ ] Helpers: `fallback_response(text, *, from_model="claude-opus-5-5",
  to_model="claude-opus-5", category="bio", tool_use=None)` → content `[fallback, text(+
  tool_use)]`, `model=to_model`, `usage.iterations=[message(from), fallback_message(to)]`;
  `sticky_response(text, *, to_model="claude-opus-5")` → text only, `iterations=
  [fallback_message(to)]`; `refusal_response(category)` → no content, `stop_reason="refusal"`,
  `stop_details=SimpleNamespace(type="refusal", category=category, explanation=None,
  recommended_model=None)`.
- [ ] `tests/unit/test_fakes.py`: each refusal fires and the call is NOT recorded; a non-member
  with `thinking: disabled` is accepted; `call_paths`; `_FallbackBlock.model_dump()` vs
  `by_alias=True`; every old block's `model_dump(by_alias=True)` equals its `model_dump()`.
- [ ] `tests/characterization/test_profile_pipeline_gm.py:245-253`: `_BoomClient` gains
  `self.beta = SimpleNamespace(messages=_BoomMessages())`, or the test would pass on an
  AttributeError instead of "synthesis boom". Its snapshot must not change.
- [ ] `tests/unit/test_simulation_stats.py:836` citation `llm.py:114` → its new line.
- [ ] Module docstring: list what the fake now mirrors.

### Task 4: tests for Task 2 — `tests/unit/test_llm_opus55.py` (new)

Pass `model=` explicitly; override settings per E8's rule; snapshot per-call `messages` with
`copy.deepcopy` inside a callable response where a test compares them (the loop passes one
mutable list to every call and edits earlier dicts).

- [ ] **Non-member identity** (`claude-opus-5`, `claude-sonnet-5`): kwargs of
  `generate_agent_response`, `synthesize_profile` and every `generate_with_tools` call kind
  (round, final retry, forced-final, forced-final retry — `max_tool_rounds=1` as setup) equal
  today's: `thinking` disabled (rounds: adaptive), no `output_config`, no `betas`/`fallbacks`,
  no `tools`/`tool_choice` on the no-tools calls; `call_paths` all `"messages"`.
- [ ] **Member**: single call → adaptive, `output_config.effort == llm_opus_effort`,
  `call_paths == ["beta"]`, `betas == [SERVER_SIDE_FALLBACK_BETA]`, `fallbacks ==
  [{"model": "claude-opus-5", "thinking": {"type": "disabled"}, "output_config": {"effort":
  "high"}}]`; with `llm_refusal_fallbacks=False` → `"messages"`, no `fallbacks`, still
  adaptive + effort; an explicit caller `output_config={"effort": "max"}` wins; an explicit
  `thinking={"type": "disabled"}` surfaces the fake's `BadRequestError` (no silent override).
- [ ] **Tool loop, member**: rounds carry `llm_opus_tool_effort`; the truncated-final retry,
  the forced-final and its truncated retry send the SAME `tools` object, `tool_choice ==
  {"type": "none"}` and the rounds' effort; every one of these calls' fallback entry has
  `thinking == {"type": "adaptive"}` (they carry tools, D4); with `tools=[]`, no `tool_choice`
  is sent and the fallback entry's thinking is `disabled`.
- [ ] **Echo**: a round returning `fallback_response(..., tool_use=...)` is echoed with the
  fallback block first, keyed `from`, every other dict unchanged.
- [ ] **`call_stats`** for a plain reply, `fallback_response`, `sticky_response`,
  `refusal_response`; `None`s on a stub lacking the attributes; dict-shaped `iterations`.
- [ ] **Fallback WARNING**: exactly one on `fallback_response` and on `sticky_response`, none on
  a plain member reply or any non-member reply (`caplog`).
- [ ] **Ceiling on the beta path**: an oversized member request raises
  `NonStreamingMaxTokensError` before any `call_paths` entry.
- [ ] Keep `test_llm_service.py`'s thinking tests as they are (default Sonnet model): they now
  also guard the non-member path.

### Task 5: ceilings and stale comments — `src/agent/tools.py`, `src/agent/simulation.py`, `src/services/review_bot.py`, two tests

- [ ] Apply Task 0's rule to consult (`tools.py:708`), new_post (`simulation.py:3300`),
  thread_reply (`simulation.py:2465`, stays 16000 unless the owner decides) and the review bot
  (`review_bot.py:705`); profile synthesis (`llm.py:876`) takes the rule's value in Task 2's
  file. Each stays a literal; each sizing comment gains one dated paragraph with the Phase 0
  numbers; history stays.
- [ ] Comments this change makes false: `simulation.py:2409-2410` and `:2428-2430` ("the one
  call site running ADAPTIVE thinking"), `:2447-2450` ("the retry path passes no `tools`"),
  `:3297-3299` ("Thinking is disabled on this path"); `tools.py:775` if 4000 changes.
- [ ] `tests/unit/test_review_bot.py:168` and `tests/unit/test_llm_nonstreaming_ceiling.py:43`
  / `:141` follow any change; add a `claude-opus-5-5` variant of the ceiling regression
  (`:224-290` run on the default Sonnet model, no longer what production runs).
- [ ] If 16000 ever changes: `llm.py:27-34` and `:59-62` derivations and CLAUDE.md's 420 s
  grace text (`:300-303`) must follow — out of scope unless the owner raises it.

### Task 6: scripts — `scripts/generate_sparsedata_user.py`, `scripts/eval_review_bot.py`

- [ ] `generate_sparsedata_user.py`: widen the import at `:57` (`from src.services.llm import
  _all_text, _extract_json, get_anthropic_client, thinking_defaults`); at `:437-442` pass
  `max_tokens=<Task 0 profile value>` and `**thinking_defaults(m, effort=
  settings.llm_opus_effort)`; `response_text = _all_text(message)`. Comment: dev script,
  non-beta client, no fallbacks; for a non-member model this also changes its thinking from
  adaptive (omitted) to disabled, matching `synthesize_profile`.
- [ ] `eval_review_bot.py:70` `MAX_TOKENS` = the review bot's Task 5 value.

### Task 7: docs — `CLAUDE.md`

- [ ] BlackbirdBot section: `settings.llm_review_model` (`claude-opus-5`) → `claude-opus-5-5`;
  keep the historical "3 of 12 live `claude-opus-5` replies".
- [ ] Re-derive the `src/services/llm.py:N` citations Task 2 shifts — everything after its
  insertion point (line 78): `:145` (CLAUDE.md ~76), `:216` and `:382` (~78), `:1360` (~291).
  `:41` does not move. `tests/unit/test_claude_md_references.py` checks range only, so check
  content by eye.
- [ ] SDK section: one paragraph — the fake now also enforces 5.5's thinking-off and forced
  `tool_choice` 400s, the non-beta signature, and the fallback block's alias, re-derived from
  probes, for the same reason it enforces the ceiling.
- [ ] A deploy box after the 2026-09-25 reviewer box — NOT titled "Deploy order for
  `00NN_…`" (no migration): what moved; all three images rebuild; `up -d agent` only with no
  live run; next run FRESH; the changed `max_tokens` literals; the rollback (Task 9) and its
  order during a live run; `llm_call_logs.model` is the requested model (`call_stats[].
  served_model` is the served one); a fallback WARNING in the worker log is the only trace for
  profile synthesis and the review bot; `PromptChangeSuggestion.model` records the requested
  model (`review_bot.py:735`), so a rescued suggestion still reads `claude-opus-5-5`.

---

## Phase 2 — verification

### Task 8: integrated checks (after all Phase 1 packages merge)

- [ ] Narrow first, on the host: `.venv-test/bin/python -m pytest tests/unit/test_llm_*.py
  tests/unit/test_fakes.py tests/unit/test_opus55_replay.py tests/unit/test_review_bot.py
  tests/unit/test_review_bot_inputs.py tests/unit/test_simulation_stats.py
  tests/unit/test_specialist_no_anchoring.py tests/unit/test_consult_accounting.py
  tests/unit/test_calibration_ladder_fixtures.py tests/unit/test_claude_md_references.py
  tests/unit/test_email_inbound_event_loop.py tests/characterization -q`.
- [ ] Then `./scripts/ci.sh`. Record the exact command and result; triage any failure first.
- [ ] `git diff --stat tests/characterization/__snapshots__/` is empty.
- [ ] Adversarial audit of the merged diff against this plan (`engineering:plan-auditor`, and
  `engineering:semantic-reviewer` on `src/services/llm.py`): every E/D/C item implemented,
  measured, or explicitly deferred; the non-member identity holds for every call kind.

### Task 8b: calibration ladder on 5.5 (owner-gated, ≈$10; D2 gate 2)

- [ ] `.venv-test/bin/python scripts/panel_calibration_ladder.py --dry-run` to list the grid.
- [ ] Run it **twice** per its docstring in a one-off agent container with the merged tree's
  `src/` mounted read-only (the image still has the old code) and the default
  `llm_agent_model_opus` now `claude-opus-5-5`.
- [ ] Gate on the pooled result (D2 gate 2: `WEAK` `blocking`+`gap` ≥ 85 %, pooled R ≥ 0.5625);
  record both runs in the Phase 0 README. A miss is a no-go for `llm_agent_model_opus` (D6 still
  allows the other two).

---

## Phase 3 — deploy (owner-gated)

### Task 9: deploy and rollback

`DC="docker compose -f docker-compose.prod.yml"` throughout; never `--remove-orphans`; never a
bare `docker compose`.

- [ ] Preconditions: D2 gates 1 and 2 = go; `/admin/simulation` shows no live run; the commit is
  made (owner asks); `git status --porcelain --untracked-files=all -- src templates static
  prompts alembic scripts pyproject.toml alembic.ini` prints nothing (CLAUDE.md: an untracked
  file is silently left out of the image).
- [ ] Rollback point: `for s in blackbird-app worker agent; do docker image tag copi-blackbird-$s:latest copi-blackbird-$s:rollback-pre-opus55; done`.
- [ ] `$DC build blackbird-app worker`; `$DC --profile agent build agent`. No alembic step;
  confirm `alembic heads` is still `0051`.
- [ ] `$DC up -d blackbird-app worker`; `$DC up -d agent` only with no live run (supervisor
  returns IDLE).
- [ ] **Smoke** (cents), each a `docker exec` of `python -c` in the named container:
  `copi-blackbird-worker-1` — `llm.generate_agent_response(..., model=settings.llm_review_model,
  max_tokens=512)` and one `synthesize_profile`-shaped `_acreate` on a trivial prompt;
  `copi-blackbird-agent-1` — the same with `llm_agent_model_opus`, plus one
  `generate_with_tools` with a no-op tool, `max_tool_rounds=0`, a prompt that REQUIRES the tool
  call, and a `set_call_log_callback` capture with `log_meta` set: assert the recorded call
  kinds are `["round", "forced_final"]` (otherwise the forced-final shape was never exercised on
  SDK 1.8.0) and that each returns non-empty text with no 400. Also print `{n: f.alias for n, f in BetaFallbackBlock.model_fields.items()}`
  and the same for the four other block types on 1.8.0 (E11 on the shipped SDK).
- [ ] **Rollback without an image change** (keeps Task 5's literals, which Task 5's rule keeps
  Opus 5-safe): 1. stop any live run from `/admin/simulation` and wait for "Simulation
  stopping..." (the supervisor runs the engine in-process and `get_settings` is cached); 2. set
  `LLM_PROFILE_MODEL=claude-opus-5`, `LLM_AGENT_MODEL_OPUS=claude-opus-5`,
  `LLM_REVIEW_MODEL=claude-opus-5` in `.env`; 3. `$DC up -d --force-recreate blackbird-app
  worker` and `$DC up -d --force-recreate agent`. Fallbacks only: `LLM_REFUSAL_FALLBACKS=false`
  and the same order. Full rollback (code and literals): the `rollback-pre-opus55` tags. Note:
  these `.env` keys also change the host test suite's defaults (E8), which Task 1/4's tests are
  written to ignore.

### Task 10: acceptance on the first fresh run (owner starts it from `/admin/simulation`)

- [ ] During this run, stop only from `/admin/simulation`, not `docker stop`: a rescued call is
  two generations in one request (R3).
- [ ] **Pre-registered stop rule** — after ≥ 50 consults and ≥ 20 hub thread_reply turns, stop
  the run and roll back by `.env` if any holds (from `call_stats`; served-by derived from
  `iterations`/fallback blocks): served-by-5.5 < 80 % overall or < 60 % for any persona; lost
  (final refusal) rate above `max(2 × the route's E20 rate, 5 %)` on a route with ≥ 30 calls,
  or ≥ 3 lost calls on a smaller one; any 400 in the agent log (`grep -E "invalid_request_error|thinking.type|bound to a different
  conversation"`); `max_tokens` stops above `max(3 × the route's E20 rate, 2 %)`;
  `reasoning_extraction` on any thread_reply.
- [ ] Profile jobs: pass only if the job's progress has no `synthesis_failed` /
  `validation_rejected` / `unvalidated` entry (`profile_pipeline.py:444,535,572`),
  `profile_generated_at` advanced (`:553`) and `synthesis_validated` is true (`:546`) — the
  pipeline swallows a synthesis exception into an empty profile (`:440-444`), so "job
  completed" proves nothing. In the worker log, grep `Failed to synthesize profile`
  (`llm.py:891`, every failed call, first try or retry), `LLM synthesis failed` and `Retry
  synthesis failed` (`profile_pipeline.py:443`, `:460`), and the fallback WARNING.
- [ ] Review bot, if used: a suggestion row appears; the fallback WARNING count in the worker
  log.

---

## Residual risks (not closed by this plan)

- **R1 — 5.5 may decline much of this corpus (E19).** Phase 0 screens it, Task 10 stops it.
  Sticky routing (C16: org-scoped, ~1 h, content hash of the conversation prefix, best-effort)
  sends later requests of the same conversation straight to Opus 5 with no fallback block;
  `served_model`/`iterations`/the WARNING make it visible, nothing prevents it.
- **R2 — cost panel accuracy (D5).**
- **R3 — latency.** Thinking is now on for consults (8 concurrent per panel) and a rescued call
  is 5.5's decline plus a full Opus 5 generation in one HTTP request (C18). Task 5's rule bounds
  single calls against the 300 s read timeout; a loaded panel and the 420 s stop grace are only
  observed in Task 10.
- **R4 — rate limits.** Separate pools (C12); Phase 0 records our headers but the throttle
  settings (`llm_calls_per_load_per_window`, `hub_llm_calls_per_window`) are not re-tuned here.
  An exhausted fallback pool returns a refusal carrying `recommended_model`, not a 429 (SDK
  `BetaRefusalStopDetails`), which the engine treats as a truncated turn; `call_stats` records
  `recommended_model`.
- **R5 — enforcement can change.** Our account is not prefix-enforced today (E15); the loop is
  made append-only anyway, but a future mid-turn edit of `system` or `tools` would 400 if
  enforcement were turned on.
- **R6 — prompts written for thinking-off Opus 5** ("You may think/reason freely outside the
  block", `prompts/roles/scout_hub/phase4-thread-reply.md:121`,
  `prompts/phase4-thread-reply.md:87`) are not re-evaluated. Prompts that ask for written-out
  reasoning can be declined as `reasoning_extraction`, never retried by a fallback (C7). Any such
  decline on thread_reply is a no-go (D2) until a prompt change lands through the role.toml bump
  / doc-sync / reviewed-snapshot path.
- **R7 — test SDK skew.** CI runs 0.120.2; 1.8.0 is exercised only by Task 0, Task 8b and Task 9.
- **R8 — early text-only end of a tool loop.** The docs warn 5.5 can end a turn with
  progress-update text mid-task; `generate_with_tools` returns any text-only reply as final.
  Phase 0 counts it; no code change here.
- **R9 — the truncation retry loses its advantage on 5.5.** Today's retry buys "strictly more
  text" because it runs thinking-disabled (`llm.py:505-511`; `simulation.py:2440-2442`: every
  2x retry at 8000, thinking disabled, succeeded). On 5.5 thinking cannot be turned off, so a
  retry at the rounds' effort may think as long again and truncate again. Kept at the rounds'
  effort deliberately (changing it invalidates the messages cache, C17); Task 10's
  `max_tokens`-stop rule watches it.
- **R10 — Phase 0 confounds.** Sticky routing is keyed on the conversation prefix (C16), which
  Task 0 keeps disjoint across arms, but how finely it keys is undocumented; sticky rows are
  reported as their own class rather than assumed away. The profile-synthesis context is an
  approximation of the live pipeline's.

## Side finding outside this plan's scope

The assessment chat stores a pre-output 5.5 decline with `"billed": false`
(`src/services/assessment_chat_stream.py:490-518`, from its spec's 2026-09-24 quote "reported
but not billed", `docs/specs/2026-09-24-assessment-chat-design.md:145,663`); 2 of E19's 4 rows
are recorded that way. The 2026-09-28 docs pass reported the refusals page as billing a
pre-output refusal for `bio`, `frontier_llm` and `reasoning_extraction`, but gave no verbatim
quote (C8). Unresolved: if true and those declines were `bio`, the chat's dollar ceilings
under-count them. The ledger keeps the declined attempt's input-token counts, so later
repricing is possible. Reported, not fixed.

---

## Docs verification (C1–C18)

Official docs fetched 2026-09-28 (`D` = `https://platform.claude.com/docs/en`). The
prompt-caching page and SDK source came through a summarising extractor and may be lightly
reformatted.

| # | Claim | Verdict | Deciding text | Source |
|---|---|---|---|---|
| C1 | thinking off / budget → 400; omitted = adaptive | VERIFIED (and E14) | "Requests that set `thinking: {"type": "disabled"}` return a 400 error at every effort level." | D/models/opus-5-5/whats-new-opus-5-5 |
| C2 | default effort `medium`; five levels; top-level effort needs no beta | VERIFIED | "All five levels … are supported, and the default is `medium`." "(Claude Opus 5 and earlier Opus models default to `high`…)" | D/models/opus-5-5/migration-guide, D/build-with-claude/effort |
| C3 | thinking counts toward `max_tokens` | VERIFIED | "`max_tokens` remains a hard limit on total output, thinking plus response text" | D/models/opus-5-5/migration-guide |
| C4 | forced `tool_choice` 400; `auto`/`none` supported | VERIFIED | "`tool_choice: type "tool" and "any" are not supported for this model.`" … "`{"type": "none"}` are supported" | D/models/opus-5-5/whats-new-opus-5-5 |
| C5 | binding prefix; enforcement by account age | VERIFIED (and E15) | "The checked prefix has three parts: The top-level `system` prompt; The set of `tools`; Every `message` before the block." "…aren't part of the prefix check, and neither are `cache_control` markers." "Accounts created on or after August 31, 2026, 00:00 UTC: the API checks Claude Fable 5.1 and Claude Opus 5.5 requests and applies `"error"` unless you set `"drop_block"`." Older accounts: "the API still runs the check but lets failing blocks through to the model." "A serializer that drops unknown block types, drops empty fields, or reorders blocks edits the prefix." | D/build-with-claude/preserved-thinking |
| C6 | `tool_use`/`tool_result` in history without `tools` | NOT FOUND; E15 and E20 (forced_final rows) show it accepted today on both models | — | — |
| C7 | refusal shape; categories; `reasoning_extraction` never retried | VERIFIED | "server-side fallback doesn't retry requests declined with `"reasoning_extraction"`" "The `stop_details` object is always present on a refusal." | D/build-with-claude/refusals-and-fallback |
| C8 | pairing, targets, billing, non-streaming | PARTIAL: pairing VERIFIED; targets only via Models API (E18); billing of a pre-output decline reported without a verbatim quote (unresolved) | "Under any other `server-side-fallback-*` value, the `fallbacks` parameter is rejected with a 400 error." "A mid-stream refusal bills the input tokens and the output already streamed at normal rates." | D/build-with-claude/refusals-and-fallback |
| C9 | 5.5 reads Opus 5 blocks; Opus 5 does not read 5.5's | VERIFIED (and E15) | "Claude Opus 5.5 reads thinking blocks from Claude Opus 5 …" | D/models/opus-5-5/migration-guide |
| C10 | between-tool text → empty `thinking` blocks by default | VERIFIED | "…progress-update `thinking` blocks … At the default `thinking.display` of `"omitted"`, their `thinking` field is empty." | D/models/opus-5-5/migration-guide |
| C11 | prices | VERIFIED (matches `llm_pricing.py`) | pricing table | D/about-claude/pricing |
| C12 | rate limits | VERIFIED: separate pools | "Claude Opus 5.5 and Claude Opus 5 each have a separate rate limit" | D/api/rate-limits |
| C13 | non-streaming limit | PARTIAL: SDK-side only | "This is a client-side validation, not an API restriction." | D/api/errors |
| C14 | other breaking changes | VERIFIED: only four; computer use n/a (E3) | "Four breaking changes affect code already running on Claude Opus 5…" | D/models/opus-5-5/whats-new-opus-5-5 |
| C15 | echo after a fallback | VERIFIED live 2026-09-28 (Task 0 Step 6: echoed fallback block accepted under `prefix_mismatch_behavior: "error"`) | "send the assistant content back as you received it" / `fallback`: "Keep it exactly where it appeared." | D/build-with-claude/refusals-and-fallback |
| C16 | sticky routing | VERIFIED | "retained for approximately 1 hour and is scoped to your organization. It is stored as a content hash of the conversation prefix plus the model that served it." A sticky turn "carries no `fallback` content block". | D/build-with-claude/refusals-and-fallback |
| C17 | cache invalidation | VERIFIED | "Changes to `tool_choice` parameter only affect message blocks." "Changing the `output_config.effort` value always invalidates message blocks… Setting effort explicitly to the model's default is equivalent to omitting it and does not invalidate." | D/build-with-claude/prompt-caching |
| C18 | non-streaming mid-output rescue | VERIFIED | "the response omits the declined model's partial output, and the fallback model answers from scratch." | D/build-with-claude/refusals-and-fallback |

---

## Audit dispositions (2026-09-28)

Three adversarial audits: two on revision 1 (code/tests; evidence, Phase 0 and deploy), one on
revision 2's changes and internal consistency.

Revision 3 applies every revision-2 finding: the blocker (Task 5's "no regression" clause
compared against `current/60.7`, which is always below 270 s, so it never applied — now both
models' latency is fitted the same way, from Task 0's new Opus 5 control arm); the undefined
latency statistics (pooled slope, per-route maximum residual, a defined decline time); the
truncation retry brought under the rule (clause iv); gate 2 made a two-run pooled test against
Opus 5's pooled R 0.5625; count-based gates for small-n routes and defined baselines for profile
synthesis, the review bot and first requests; the missing pi_lab tool-round row in E20;
structural served-by detection; the smoke that could skip the forced-final call; the rollback
list; the `tools=[]` effort edge; the fake's JSON-safe fallback dump, `iterations` and
`latency`; R9, R10; and the softened "rescue = today's call" wording.

Revision 2 applied every revision-1 finding except the three below, including the blocker
(fake `model_dump` signature → Task 3).

Rejected, with evidence:
- "The sampled runs predate scout_hub 1.8.0 / rubric 3.5.0." Run `bf6da580`'s run-start
  announcement records hub prompts v1.8.0 and rubric v3.5.0 at commit `bc68025` (E9). The older
  run `75ca77f9` does predate it; Task 0 records each row's stamps.
- "Pick one: keep the code on no-go vs Phase 1 only after go." Resolved as: no-go at gate 1
  stops before Phase 1; D6 remains the owner's alternative (D2).
- "Cap thread_reply at ~18,000." Replaced by: thread_reply stays 16,000 unless Phase 0 shows
  5.5 hub outputs ≥ 13,000, and Task 5's clause (iii) bounds every raised literal for Opus 5.
