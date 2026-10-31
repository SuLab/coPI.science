# Blackbird landscaping Implementation Plan

> **For agentic workers:** execute with `/lathe:plan-execution`, one phase at a time. That
> skill replaces `superpowers:subagent-driven-development` and `superpowers:executing-plans`
> in this repository.
> - Within a phase, every task's files are disjoint, and tasks rely only on the Interfaces
>   blocks of earlier tasks and earlier phases.
> - Under plan-execution's rule, implementers do not build or run tests. The test steps below
>   say what to write. The phase's verification step runs them once, after the merge.
>
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the landscaping pipeline. It continuously ingests the research outputs of
Johns Hopkins entities in Baltimore and runs the S1-S9 LLM funnel. The top 1% of
deduplicated therapeutic ideas end as BlackbirdBot-contract assessments of opportunities,
which reviewers and managers decide on in weekly batches on a new workspace page.

**Architecture:**
- **Code:** a new `src/landscaping/` package and a separate `landscape-worker` process own all
  pipeline state, in new `landscape_*` tables (migration 0077).
- **Reuse:** the existing rubric, BlackbirdBot prompts and specialist personas are read
  verbatim, never edited. Shared service modules change only additively.
- **Web:** GET-only workspace pages under `/workspace/landscape`. Writes go through
  `/reviews`, a new `/landscape` router and a new `/landscape-chat` router. Chat widens the
  shared chat tables (migration 0078), as Projects did.

**Tech Stack:** Python 3.11 (images) and 3.12 (`.venv-test`); FastAPI; SQLAlchemy async;
Alembic; Jinja2 + Tailwind; the `anthropic` SDK (0.120.2 in tests, 1.9.0 in images, both with
`messages.batches`); httpx.

**Spec:** `docs/specs/2026-10-30-blackbird-landscaping-design.md`, with its analysis
`docs/specs/2026-10-30-blackbird-landscaping-analysis.md`. Section numbers (§) below are the
spec's. "L#" are the owner decisions in its §3.1, and "D#" the calls in its §3.2.

## Global Constraints

Every task inherits these, copied from the spec.

**Untouchable files** (K1, K2):
- No edit to `prompts/roles/**`, `prompts/specialists/**`, `prompts/rubric/**` or
  `prompts/projects/**`.
- No `role.toml` bump.
- No change to `src/agent/**`. That includes `FENCE_TAGS`: untrusted text uses
  `delimit(text, tag)` with tags already registered: `publication_abstract` for summaries,
  `methods_section` for full-text sections, `patent`, `trial_record` (D44).

**Shared modules** (D45). Shared modules imported by the agent, the worker or the profile
pipeline change **additively only**: new functions, constants and settings. Existing
signatures, return shapes, defaults and behaviour stay unchanged. Covered modules:
- `src/config.py`, `src/database.py`;
- `src/services/llm.py`, `llm_pricing.py`, `advisory_locks.py`, `openalex.py`, `pubmed.py`,
  `nih_reporter.py`;
- `industry_sources/openalex_industry.py`;
- `assessment_chat.py`.

`tests/characterization/` and every existing test pass unchanged. The only edits to existing
tests are the pin moves named in a task.

**Schema** (K3, D23):
- New `landscape_*` tables only, in additive migrations `0077_landscape` and
  `0078_landscape_chat`.
- No `simulation_run_id`. No new Postgres enum type: string + CHECK.
- JSONB columns use `none_as_null=True`.
- No use of `jobs` or `job_type_enum`.
- The only existing tables touched are `app_settings` (four keys: `landscape_paused`,
  `landscape_phase`, `landscape_positive_controls`, `landscape_experts`) and the chat tables
  (0078).

**Models** (L17, D8, D42):
- Never `claude-opus-5-5` and never `claude-haiku-4-5-20251001` anywhere under landscaping.
- S6-S9 and the chat use `claude-opus-5`.
- The fallback chain is `claude-opus-5` → `claude-opus-4-8`, skipping the model that refused.

**Calls** (D32):
- `thinking={"type": "adaptive"}` on every request.
- Effort per stage: S2 `low`; S3 and S4 `medium`; S6 (its full-text re-extraction included)
  and S9 `high`; S7, the S5 tie-break and S8 `medium`. The reject audit uses the audited
  stage's builder (D66).
- Every `max_tokens` is a named constant ≤ 21,333.
- No `temperature`, `top_p` or `top_k`.
- Model JSON is parsed only with `src.services.json_extract.extract_json`.
- Stored error text goes through `src.services.job_queue.job_error_text`.
- Stage runners take each request's system prompt and model from
  `epochs.epoch_inputs(db, epoch_id)`, never from the files on disk or the current settings,
  so an epoch's judgments are never mixed (spec §7.11).

**Keys:**
- The OpenAlex key is read only through `src.services.openalex_auth`, the NCBI key only
  inside `src/services/pubmed.py`.
- NCBI is paced at ≤ `landscape_ncbi_max_rps` (default 3) through `pubmed.set_min_interval`,
  set once at worker start, so reused PubMed functions are covered too (D58).

**Prompt files:** none under `prompts/landscaping/` may contain any of the 12 `_FORBIDDEN`
phrases of `tests/unit/test_doc_prompt_sync.py:102-120`, "Baltimore" and "phase 2" among them.
The scan at `:123-130` covers every `prompts/**/*.md`.

**Code shape:**
- Every function ≤ 200 lines, complexity ≤ 20.
- No new ruff findings in `src/` (ceiling 231).
- No dead symbols; every route and template reachable.
- Coverage ≥ 60%.
- Landscaping imports only read-only names from `src/agent/` (`specialists.persona_path`,
  `specialists.parse_opinion`, `prompt_safety.delimit`, `engine.sidecar._normalize_gating`)
  and never edits it.
- Session advisory locks use `advisory_locks.SessionAdvisoryLock(database_url, key)`, the
  engine lock's helper, each on its own AUTOCOMMIT connection.

**Dead-code guard** (`tests/unit/test_no_dead_src_symbols.py`). A `src/` definition is live
only when live code outside `tests/` reaches it, and landscaping gets its first live roots in
Task 6.5 (the worker's `__main__`), Task 6.6 (a script) and Phase 7 (routes). Tasks never edit
that test file, so the disjoint-file rule holds. Each phase's verification step owns its
`ALLOWLIST`:
- add one entry per definition the test reports, with the reason "caller lands in Task N.M",
  naming the task that brings the caller (the file's own precedent for Phase 2 helpers);
- remove every entry the test reports stale.

After Phase 7 no landscaping entry remains.

**Compiled CSS.** Tailwind scans `src/**/*.py` as well as the templates, and `./scripts/ci.sh`
runs `scripts/build_css.sh --check` (`scripts/ci.sh:138`), so a docstring word such as "shrink"
can add a class. Implementers never run it. Each phase's verification step rebuilds
`static/css/app.css` with `scripts/build_css.sh` when the check reports drift, and commits it.

**Verification-step edits.** Besides running checks, verification steps make only these
mechanical edits: the `ALLOWLIST` above, the compiled CSS, and the prompt-set stamp (Phase 1).

**Public repository** (K6). No real researcher names, no rankings, nothing from `data/` and no
secrets in code, tests, fixtures or docs. Test fixtures use invented names.

**Help content** (`docs/operations/help-content.md`): no numbers from settings or the rubric,
no real names, no repository internals.

**Tests** run on the host: `.venv-test/bin/python -m pytest …`, and `./scripts/ci.sh` is the
gate. Over the sshfs mount, expect 100-400× slower runs.

## Review Focus

These failure modes are not exercised by any task's main-path tests. Each line names the
task that owns its test.

1. **A PubMed-first document later matched to OpenAlex** must re-key in place, recompute S1,
   and keep its S2/S3 results unless the summary changed (Task 3.5).
2. **Batch results** arriving errored, expired, canceled, truncated or refused, a chunk
   replayed after a crash, and a crash between `batches.create` and storing the id, must
   neither lose spend, double-submit, nor fire a resubmission or fallback twice (Task 2.2).
3. **A provisional idea key that later resolves** onto an existing idea, while its
   opportunity has reviews, must keep every foreign key valid and the reviews visible
   (Task 4.3).
4. **An epoch switch mid-cycle** must never show a page or cut that mixes epochs; the old cut
   stays until activation (Task 2.3, Task 7.1).
5. **Independent votes and blind batches** must not leak through counts, the chat record's
   Reviews document, the batch page, the Current and All views, probe flags or staff views,
   until the viewer submits a quick decision (Task 7.1, Task 7.4).

---
## Phase 1 — Foundations

### Task 1.1: Settings, engine role, lock keys

**Files:**
- Modify: `src/config.py`, `src/database.py`, `src/services/advisory_locks.py`
- Modify (pins): `tests/unit/test_config_secret_redaction.py` (`NON_SECRET_STR_FIELDS`),
  `tests/unit/test_make_engine.py`, `tests/unit/test_advisory_locks.py` (distinctness list)
- Test: `tests/unit/landscaping/test_settings.py`

**Interfaces:**
- **Produces** `Settings` fields (§9.1, §12.3, §13.5):

  | Field | Default |
  |---|---|
  | `landscape_enabled: bool` | `False` |
  | `landscape_screen_model: str`, `landscape_extract_model: str`, `landscape_triage_model: str` | `"claude-sonnet-5"` |
  | `landscape_deep_model`, `landscape_rerank_model`, `landscape_label_model`, `landscape_final_model` | `"claude-opus-5"` |
  | `landscape_similarity_model`, `landscape_audit_model` | `"claude-opus-5"` |
  | `landscape_fallback_models: list[str]` | `["claude-opus-5", "claude-opus-4-8"]` |
  | `llm_landscape_chat_model: str` | `"claude-opus-5"` |
  | `landscape_daily_usd_cap: float \| None` | `None` |
  | `landscape_batch_min_items: int` | `500` |
  | `landscape_batch_max_wait_minutes: int` | `60` |
  | `landscape_batch_poll_seconds: int` | `300` |
  | `landscape_sync_concurrency: int` | `4` |
  | `landscape_running_stale_minutes: int` | `30` |
  | `landscape_reject_audit_size: int` | `100` |
  | `landscape_ncbi_max_rps: float` | `3.0` |
  | `landscape_europepmc_max_rps: float` | `1.0` |
  | `landscape_bioc_max_rps: float`, `landscape_pubtator_max_rps: float` | `3.0` |
  | `landscape_openfda_daily_requests: int` | `1000` |
  | `landscape_window_days: int` | `1096` |
  | `landscape_cut_fraction: float` | `0.01` |
  | `landscape_chat_enabled: bool` | `False` |
  | `landscape_chat_daily_usd_limit: float` | `50.0`, in the finite guard tuple |

  The numeric defaults here are the builder's (§17), except the cut fraction, the window, the
  chat defaults and the pacing values the spec names (§6.1, §6.3).
- **Produces** `EngineRole` value `"landscape"` (kwargs `{"echo": False}`).
- **Produces** `LANDSCAPE_WORKER_LOCK_KEY: int = fixed_key("landscape_worker")`, which joins
  the distinctness list, and `def landscape_rerank_lock_key(epoch_id: uuid.UUID) -> int`,
  returning `fixed_key(f"landscape_rerank:{epoch_id}")`: one S7 lock per epoch (§7.8).

- [ ] **Step 1: Write the failing tests** in `test_settings.py`:
  - `test_landscape_defaults` asserts every default above; in particular `landscape_enabled is
    False`, the S6-S9 models equal `"claude-opus-5"`, `llm_landscape_chat_model ==
    "claude-opus-5"`, and `landscape_cut_fraction == 0.01`.
  - `test_no_forbidden_model_in_landscape_settings` asserts no landscape setting default equals
    `"claude-opus-5-5"` or `"claude-haiku-4-5-20251001"`.
  - `test_optional_cap_guard`: `Settings(landscape_daily_usd_cap=None)` constructs, and `nan`,
    `inf`, `0` and `-1` each raise a validation error (spec §12.3 rejects non-finite and
    non-positive values).
  - `test_chat_subcap_guard`: the default is `50.0`; `nan` raises, as the guard tuple does for
    its members (`src/config.py:712-714`); `0` logs a warning and falls back to `50.0`
    (`:715-721`).
  - `test_rerank_lock_key_per_epoch`: two epoch ids give two keys, and neither equals a key in
    the distinctness list.
- [ ] **Step 2: Implement.**
  - Fields go next to the existing `llm_*` block.
  - A new validator guards `landscape_daily_usd_cap`. **Do not** add that field to the tuple at
    `src/config.py:702-711`; add only `landscape_chat_daily_usd_limit` there.
  - Add `"landscape"` to `EngineRole` and `engine_kwargs`, and the constant and function to
    `advisory_locks.py`.
  - Update the three pin tests: add model-name fields to `NON_SECRET_STR_FIELDS`, assert the
    new role's kwargs, and add the new key to the distinctness list.
- [ ] **Step 3: Commit** `feat(landscaping): settings, engine role and lock keys`.

### Task 1.2: ORM models and migration 0077

**Files:**
- Create: `src/models/landscape.py`, `alembic/versions/0077_landscape.py`,
  `src/landscaping/schema_check.py`
- Modify: `src/models/__init__.py` (imports); `scripts/migrate/preflight.py` (`DEFAULT_TARGET`,
  `REVISION_ORDER`, `SUPPORTED_START_REVISIONS` gains `0076`, and `PLANNED_OBJECTS` gains every
  0077 table, column, foreign key, index, unique constraint and CHECK);
  `scripts/migrate/postflight.py` (`"0077"` joins `VERIFIED_REVISIONS`, with every 0077 table
  in `EXPECTED_TABLES` (`postflight.py:264`, required by `test_migration_checks.py:1338-1343`)
  and `EXPECTED_COLUMNS`, `EXPECTED_INDEXES` and `EXPECTED_CONSTRAINTS` entries for every 0077
  object, as `tests/unit/test_migration_checks.py:1312-1318` asks of a later migration);
  `docs/operations/migration-deploy-notes.md` (a "Deploy order for `0077_landscape`" box)
- Modify (pins): `tests/unit/test_migration_checks.py` (head pins),
  `tests/integration/test_harness_smoke.py:109-110`,
  `tests/integration/test_review_models.py::test_recorded_by_columns_exist_and_default_null`
  (the landscape decision, review and review-event tables carry `recorded_by_user_id` too)
- Test: `tests/integration/landscaping/test_migration_0077.py`,
  `tests/unit/landscaping/test_models.py`

**Interfaces.** Produces ORM classes, one per §11.1 table, with exactly the uniqueness rules in
§11.1:
- `LandscapeSource`, `LandscapeFetch`, `LandscapeDocument` (with a self-referencing
  `merged_into`, the tombstone of spec §6.1), `LandscapeDocumentLink`, `LandscapeText`,
  `LandscapeLab`
- `LandscapeScreen`, `LandscapeExtraction`, `LandscapeIdea`, `LandscapeIdeaLink`,
  `LandscapeExtractionLink`, `LandscapeCandidateGrouping`, `LandscapeIdeaEvidence`,
  `LandscapeIdeaAlias`, `LandscapeEvidenceCache`
- `LandscapeEpoch`, `LandscapeTriage`, `LandscapeCut`, `LandscapeDeep`, `LandscapeConsult`
- `LandscapeComparison`, `LandscapeRanking`, `LandscapeSimilarity`
- `LandscapeOpportunity`, `LandscapeOpportunityVersion`, `LandscapeAssessment`
- `LandscapeDecision`, `LandscapeReview`, `LandscapeReviewEvent`, `LandscapeReviewAssignment`
- `LandscapeReviewBatch`, `LandscapeReviewBatchItem`, `LandscapeExpertJudgment`
- `LandscapeRejectAudit`, `LandscapeRescue`, `LandscapeRegressionSet`,
  `LandscapeRegressionItem`, `LandscapeRegressionRun`
- `LandscapeBatch` (with a bigint `seq`), `LandscapeBatchItem`, `LandscapeLlmCall`,
  `LandscapeReport`

Every epoch-scoped table (spec §7.1, §11.1) carries `epoch_id` in its uniqueness key:
`LandscapeIdea` is unique on `(epoch_id, idea_key)`. Decisions, reviews and assignments are
unique on `(opportunity_id, …)`; decisions and reviews record the `landscape_assessment_id`
acted on (spec §11.1).

Every row a batch result fills (screens, extractions, triage, tournament comparisons,
opportunity versions and candidate groupings for their S8 label, assessments, reject audits)
carries its stage `status` and a nullable `batch_item_id` foreign key to
`LandscapeBatchItem`, which Task 2.2's compare-and-set matches (spec §7.1 step 5).
`LandscapeLlmCall` is unique on `(batch_id, item_seq)`; synchronous rows leave both null.

Stage rows also carry `attempts` (int, default 0), `truncated_once` (bool) and `fallback_from`
(a model name, or null), which Task 2.2's `transition` sets; `LandscapeDeep` carries `status`
too. `LandscapeBatch` carries `row_table`, the stage rows' table, `create_failures` (int,
default 0), and the statuses `submitting`, `submitted`, `ended` and `ingested`.

Other shapes:
- `LandscapeAssessment` is keyed either by `(opportunity_id, opportunity_version, epoch_id)` or,
  in a candidate epoch, by `(epoch_id, grouping_id)` (D59). The columns are nullable, a CHECK
  requires one complete key, and each key has its own unique constraint.
- `LandscapeCandidateGrouping` has `id`; `grouping_key`, the sha256 of its sorted
  (`idea_key`, idea version) pairs, unique per epoch; member `idea_key`s, label, theme, platform
  signal, edge kinds; and a `current` flag, cleared when a later S8 run replaces the grouping.
- `LandscapeBatchItem` has its own `id`, which stage rows reference as `batch_item_id`.
- `LandscapeReviewBatchItem` holds the probe flag and `position`: a blind batch's shuffled order
  (null in a normal batch, whose cards follow live card order, D30).
- `LandscapeIdea` holds the assembled evidence summary, the data flags,
  `evidence_assembled_at`, and `evidence_status`: JSONB `{source: {"status": "pending" | "done"
  | "failed", "attempts": int}}`, one entry per evidence source (spec §7.4 step 5).
- `LandscapeEpoch` carries `rejudge_started_at` and `drained_at` (spec §7.11 step 4), and
  `snapshot` (JSONB, nullable): the judging inputs themselves, stored when the epoch is created
  (Task 2.3's `build_snapshot`), which its stage runners read (spec §7.11).
- `LandscapeDeep` carries `summary` (text, nullable): S6's free-text summary, which the S7
  comparator reads when present.
  `LandscapeCandidateGrouping` and `LandscapeOpportunityVersion` carry the `cut_id` they were
  grouped from.
- `LandscapeReport.kind` is one of `bakeoff`, `bakeoff_request`, `probes`, `pilot`,
  `completeness`, `drift`, `positive_controls`, `metrics`.

`schema_check.py` produces `async def schema_ready(db) -> bool`: true when
`to_regclass('public.landscape_epochs')` is not null. The worker and the pages refuse without
it (spec §12.5).

Module constants:
- `STAGE_STATUSES = ("pending", "submitting", "submitted", "running", "done", "rescued",
  "refused")`, plus `excluded:` and `failed:` prefixes, enforced by CHECK as
  `status IN (...) OR status LIKE 'excluded:%' OR status LIKE 'failed:%'`.
- S1's result lives on the shared `LandscapeDocument` row, with its own CHECK:
  `s1_status = 'eligible' OR s1_status LIKE 'excluded:%'` (spec §7.1).
- `EPOCH_STATUSES = ("candidate", "held", "active", "retired")`, with a partial unique index
  allowing one `active`.
- `DECISIONS = ("pursue", "pass")`.
- `REVIEW_ACTIONS = ("approved", "disapproved", "cleared")`.

`LandscapeAssessment` has one column per sidecar field. Copy the column names and types of the
`opportunity_assessments` sidecar columns (`src/models/opportunity.py`) and add:
- `claim_support` (JSONB)
- `member_notes` (JSONB)
- `structured_lines` (JSONB)
- `weighted_score` (float)
- `band` (str)
- `served_by_model` (str)
- `rank` (int)
- `epoch_id`
- `opportunity_id` and `opportunity_version`, or `grouping_id` (D59)

- [ ] **Step 1: Write the failing tests.**
  - `test_migration_0077.py::test_upgrade_creates_every_landscape_table` asserts the exact
    table set.
  - `::test_unique_constraints` inserts duplicates for `(kind, canonical_external_id)`,
    `(epoch_id, idea_key)`, `(idea_id, idea_version, epoch_id)` on triage,
    `(epoch_id, doc_a, doc_b)` on extraction links, `(epoch_id, grouping_key)` on candidate
    groupings, `(opportunity_id, reviewer_id)` on decisions, and `(batch_id, item_seq)` on
    batch items and on the ledger, and expects `IntegrityError`.
  - `::test_ledger_allows_many_synchronous_rows`: two `LandscapeLlmCall` rows with null
    `batch_id` and `item_seq` both insert.
  - `::test_assessment_needs_one_complete_key`: a row with neither key, or with only an
    `opportunity_id`, fails the CHECK.
  - `::test_same_idea_key_in_two_epochs_allowed`.
  - `::test_one_active_epoch` expects a second `active` epoch row to fail.
  - `::test_round_trip` runs downgrade to 0076, asserts `schema_ready` is false, and upgrades
    to head.
  - `test_models.py::test_no_simulation_run_column` and
    `::test_jsonb_none_as_null` (mirrors `tests/unit/test_json_none_as_null.py` for the new
    models).
- [ ] **Step 2: Implement** the models and the migration: `op.create_table` per table,
  string + CHECK, indexes on every foreign key and on `(status)` columns used by the worker's
  claims. The downgrade drops the tables in reverse dependency order. Move the preflight and
  test pins, and write the deploy-order box:
  - old code on the new schema: unaffected;
  - new code on the old schema: the landscaping worker and pages refuse;
  - the agent image needs no rebuild (D45).
- [ ] **Step 3: Commit** `feat(landscaping): landscape tables (0077)`.

### Task 1.3: Pricing for new models, tiers and batch

**Files:** Modify `src/services/llm_pricing.py`, `tests/unit/test_llm_pricing.py`.

**Interfaces.** Produces:
- `PRICES` entries, written with `Decimal("…")` literals like the existing ones
  (`cost_for_tokens` multiplies `Decimal` fields):
  - `claude-sonnet-5-5`: input 2, output 10, cache write 2.50, cache read 0.10;
  - `claude-haiku-5-5`: input 0.10, output 0.50, cache write 0.125, cache read 0.01.
- `HAIKU_5_5_LONG`: input 0.50, output 2.50, cache write 0.625, cache read 0.05.
- `BATCH_MULTIPLIER = Decimal("0.5")`.
- `cost_for_request(model: str, *, input_tokens: int, output_tokens: int, cache_read: int,
  cache_creation: int, batch: bool) -> Decimal | None`. It picks the Haiku 5.5 tier from that
  request's own prompt length (input + cache read + cache creation > 100,000), then applies
  `BATCH_MULTIPLIER` when `batch`.

`cost_for_tokens` is unchanged.

- [ ] **Step 1: Write the failing tests.**
  - `test_cost_for_request_haiku_tiers`: 100,000 prompt tokens is the short tier, 100,001 the
    long tier.
  - `test_cost_for_request_batch_halves`.
  - `test_unpriced_model_returns_none`.
  - `test_cost_for_tokens_unchanged`, comparing an existing model's result before and after.
- [ ] **Step 2: Implement.** Update the module docstring line about modifiers and `AS_OF` to
  `2026-10-30`.
- [ ] **Step 3: Commit** `feat(pricing): Sonnet 5.5, Haiku 5.5 tiers and batch pricing`.

### Task 1.4: Baltimore institutions and the shared authorship-role module

**Files:**
- Create: `src/landscaping/__init__.py` (empty), `src/landscaping/institutions.py`,
  `src/services/authorship_roles.py`
- Modify: `src/services/industry_sources/openalex_industry.py` (import the lifted functions;
  delete the local `_role`, `_ROLE_RANK`, `_pi_at_jhu` bodies by delegating)
- Test: `tests/unit/landscaping/test_institutions.py`,
  `tests/unit/test_authorship_roles.py`

**Interfaces:** produces

```python
# src/services/authorship_roles.py
ROLE_RANK: dict[str, int]  # {"corresponding": 0, "last": 1, "first": 2}
def role(auth: dict) -> str  # "corresponding" | "first" | "last" | "middle"
def at_institution(auth: dict, ids: frozenset[str], matcher: Callable[[str], bool], *,
                   raw_strings: Literal["always", "only_when_no_institutions"]) -> bool
def is_individual(auth: dict) -> bool  # False for collective names (consortium, group, network, investigators, study)
# src/landscaping/institutions.py
BALTIMORE_OPENALEX_IDS: frozenset[str]  # exactly the 21 ids of spec §5.1
EXCLUDED_OPENALEX_IDS: frozenset[str]   # the §5.1 excluded entities' ids
REPORTER_ORGS: tuple[str, str]          # ("JOHNS HOPKINS UNIVERSITY", "HUGO W. MOSER RES INST KENNEDY KRIEGER")
NSF_AWARDEE: str                        # "Johns Hopkins University"
ROR_IDS: frozenset[str]                 # {"00za53h95", "037zgn354", "05q6tgt32", "05m5b8x20"}
def is_baltimore_affiliation(text: str) -> bool  # §5.1 matcher
def qualifies_openalex(work: dict) -> tuple[bool, str | None, dict | None]  # (ok, role_basis, authorship)
```

- [ ] **Step 1: Write the failing tests.**
  - `test_baltimore_ids_match_spec` holds the exact 21-id set.
  - `test_matcher`:
    - accepts "Johns Hopkins University School of Medicine", "Kennedy Krieger Institute" and
      "Jhpiego";
    - rejects "Johns Hopkins University Applied Physics Laboratory, Laurel, MD" and "Johns
      Hopkins All Children's Hospital, St. Petersburg".
  - `test_qualifies_openalex`:
    - a last author at I145311948 qualifies with `"last"`;
    - a corresponding middle author at I4210164401 qualifies with `"corresponding"`;
    - a last author at APL only does not qualify;
    - a raw-string-only authorship qualifies only when its `institutions` is empty;
    - a collective last author does not qualify.
  - `test_authorship_roles.py`: `role` and `at_institution` (with `raw_strings="always"`)
    return literal expected values, written into the test, for the authorship fixtures of
    `tests/unit/test_industry_sources_parsers.py`. Comparing with `openalex_industry`'s
    functions would be circular once they delegate.
  - The existing industry tests pass unchanged.
- [ ] **Step 2: Implement.**
- [ ] **Step 3: Commit** `feat(landscaping): Baltimore institution set; lift authorship roles`.

### Task 1.5: Criteria file, prompt-set loader and BlackbirdBot extracts

**Files:**
- Create: `prompts/landscaping/criteria.toml`, `src/landscaping/criteria.py`,
  `src/landscaping/prompt_set.py`
- Test: `tests/unit/landscaping/test_criteria.py`, `tests/unit/landscaping/test_prompt_set.py`

**Interfaces:** produces

```python
# criteria.py
@dataclass(frozen=True) class Criteria: version: str; content_hash: str; judging_hash: str; scope_text: str;
    idea_types: tuple[str, ...]; directions: tuple[str, ...]; stages: tuple[str, ...];
    evidence_grades: tuple[str, ...]  # ladder order, weakest first
    agent_kinds: tuple[str, ...]; deal_breakers: tuple[DealBreaker, ...];
    ambiguous_gene_words: frozenset[str]; reason_tags: tuple[str, ...]; reason_tags_approved: bool;
    dimension_share: float
    # judging_hash: sha256 of the parsed file without its [meta] and [reason_tags] tables, as canonical JSON (D61)
def load_criteria(path: Path = CRITERIA_PATH) -> Criteria  # raises CriteriaError
# prompt_set.py
JUDGING_FILES = ("screen.md", "extract.md", "triage.md", "deep.md", "rerank.md", "similarity.md",
                 "cluster_label.md", "final.md")  # chat.md judges nothing (D61)
@dataclass(frozen=True) class PromptSet: version: str; content_hash: str; files: dict[str, str];
    judging_hashes: dict[str, str]  # sha256 per JUDGING_FILES entry
@dataclass(frozen=True) class BlackbirdExtracts: standards: str; practice: str; contract: str;
    scout_hub_version: str; content_hash: str
def load_prompt_set(dir: Path = PROMPT_DIR) -> PromptSet  # raises PromptSetError
def chat_prompt_matches(dir: Path = PROMPT_DIR) -> bool  # chat.md against its recorded hash, for the chat (Task 7.4)
def stamp(version: str, dir: Path = PROMPT_DIR) -> None
    # writes `version` and [files] (hex sha256 of each file's bytes, every file but set.toml) into set.toml;
    # refuses when a hash changed and `version` equals the recorded one (spec §8.1). Module CLI:
    # `python -m src.landscaping.prompt_set --stamp <version>`
def load_blackbird_extracts() -> BlackbirdExtracts  # raises PromptSetError
def system_prompt(stage: str, *, ps: PromptSet, ex: BlackbirdExtracts, criteria: Criteria, rubric) -> str
```

**Details:**
- **`criteria.toml`** holds the §8.1 contents, `[triage] dimension_share = 0.8`, and
  `[reason_tags]` with `tags` and `approved = false`. The starting tag list is drafted from the
  rubric dimension titles and the deal-breaker names (L49). Task 6.3 refuses to issue review
  batches while `approved` is false (§8.1, §15). The tags are no epoch input (D61).
- **The set stamp** (spec §8.1). `set.toml` (Task 1.6) holds `version` and a `[files]` table:
  the sha256 of every other file in the directory, `criteria.toml` included. `content_hash` is
  the sha256 of the lines `"{name} {hash}"` for the sorted file names, joined by newlines.
  `load_prompt_set` refuses when a file's hash differs from its record, or a file is missing
  or unrecorded: an unrecorded edit. `stamp` writes the table.
- **Extracts.** They are taken verbatim by heading per spec §8.2:
  - `standards`: from `## Opportunity Assessment Quality Standards` to the `{rubric}` line,
    then `render_rubric_markdown()`;
  - `practice`: the two paragraphs starting "**What this looks like in practice.**" and "Or:
    an institution has chemical matter";
  - `contract`: from `### Concluding with an Opportunity Assessment: the sidecar` to the end of
    the post-skeleton rules in `phase4-thread-reply.md`.
- **`system_prompt(stage)`** fills the stage file's placeholders, then appends that stage's
  extracts (§8.2 table):
  - `{criteria_scope}`: the scope text;
  - `{criteria_vocab}`: the direction, stage, evidence-grade (ladder order) and agent-kind
    vocabularies;
  - `{deal_breakers}`: each deal-breaker with its data or LLM test;
  - `{target_rubric}`: the 11 `evidence` items, `scientific_credibility`'s then
    `translational_path`'s, read from `load_rubric()` in file order (§7.4 step 2);
  - `{data_notice}`: `llm.SYNTHESIS_DATA_NOTICE`, which names the fence tags (§9.2).

  The user message carries the fenced dossier. Each stage runner builds it; this function does
  not.

- [ ] **Step 1: Write the failing tests.** They build temporary prompt directories and need
  none of Task 1.6's files.
  - `test_headings_exist`: every heading and paragraph start the extracts need is present in
    the frozen files.
  - `test_extracts_nonempty_and_verbatim`: each extract is a substring of its file, or the
    rubric rendering.
  - `test_missing_heading_raises`: run against a temp copy with a heading removed.
  - `test_hash_without_restamp_raises`.
  - `test_judging_hashes_exclude_chat`: editing `chat.md` and re-stamping leaves
    `judging_hashes` unchanged.
  - `test_criteria_judging_hash_ignores_meta_and_reason_tags`: changing a tag, `approved`, the
    `[meta]` version or the changelog leaves `judging_hash` unchanged; changing a direction
    changes it.
  - `test_unrecorded_file_raises`: a file absent from `[files]` stops the load.
  - `test_stamp_writes_file_hashes` and `test_stamp_refuses_without_version_bump`.
  - `test_chat_prompt_matches`: false after an unrecorded `chat.md` edit.
  - `test_criteria_vocabularies`: the direction set equals
    `{"inhibit","activate","degrade","replace","edit","deliver_to","modulate","unknown"}`; the
    grade ladder order is `correlation < in_vitro < in_vivo < patient_material <
    human_genetics < clinical`; `dimension_share == 0.8`.
  - `test_reason_tags_approval_flag_parsed`: false in the shipped file.
  - `test_target_rubric_block_matches_load_rubric`: the block holds exactly the rubric's 11
    evidence items, in file order.
  - `test_criteria_has_no_forbidden_phrases`: none of the `_FORBIDDEN` phrases. The scope text
    is inlined into prompts, so it obeys the prompt-file rule.
- [ ] **Step 2: Implement.** Nothing under `prompts/roles/` is written.
- [ ] **Step 3: Commit** `feat(landscaping): criteria file, prompt-set loader, BlackbirdBot extracts`.

### Task 1.6: The landscaping prompt texts and the set stamp

**Files:**
- Create under `prompts/landscaping/`: `screen.md`, `extract.md`, `triage.md`, `deep.md`,
  `rerank.md`, `similarity.md`, `cluster_label.md`, `final.md`, `chat.md`, `set.toml`
- Test: `tests/unit/landscaping/test_prompt_texts.py`

**Interfaces.**
- **Consumes:** the output schemas of spec §7.3 (S2), §7.4 (S3, with `linked_publications`),
  §7.5 (S4), §7.7 (S6), §7.8 (S7), §7.9 (similarity: `{"same_program": bool, "reason": "<=200
  chars"}`; labels), §7.10 (S9 plus `claim_support`, `member_notes`); the placeholders and the
  stamp rule of Task 1.5.
- **Produces:** the nine prompts, and `set.toml` with `version = "0.0.0"` and an empty
  `[files]` table. The Phase 1 verification step stamps it with Task 1.5's `stamp`, because
  Task 1.5's `criteria.toml` is not merged while this task runs. Each prompt opens with the §8.2 precedence frame, verbatim, wherever the
  stage uses BlackbirdBot extracts (S4, S6, S9).

- [ ] **Step 1: Write the failing tests.**
  - `test_no_forbidden_phrases`: none of the 12 `_FORBIDDEN` phrases of
    `tests/unit/test_doc_prompt_sync.py:102-120` (case-insensitive), imported from that module.
  - `test_frame_present` in `triage.md`, `deep.md` and `final.md`.
  - `test_placeholders_known`: only `{criteria_scope}`, `{criteria_vocab}`, `{deal_breakers}`,
    `{target_rubric}` and `{data_notice}` appear.
  - `test_checklist_and_notice_where_needed`: `extract.md` and `triage.md` carry
    `{target_rubric}` and `{criteria_vocab}`; `triage.md`, `deep.md` and `final.md` carry
    `{deal_breakers}`; every judging prompt carries `{criteria_scope}` and `{data_notice}`.
  - `test_shipped_set_loads`: `load_prompt_set()` on the real directory succeeds, so the
    recorded hash matches.
- [ ] **Step 2: Write the prompts**, each instructing the model to emit only the stage's JSON
  object. Each also carries:
  - S2: recall first (`unsure` when in doubt; unmet need is not a reason to drop).
  - S3: `linked_publications` only for patents and JHTV listings, each value copied verbatim
    from the text (D67).
  - S4: blind to identities; score what the evidence shows; "unproven" is not a defect at the
    incubation grain.
  - S9: L41's headline rules come from the contract extract; `claim_support` for every
    key-point bullet.

  Then write `set.toml` with `version = "0.0.0"` and an empty `[files]` table.
- [ ] **Step 3: Commit** `feat(landscaping): stage prompts`.

### Phase 1 verification

- [ ] Run `.venv-test/bin/python -m src.landscaping.prompt_set --stamp 1.0.0`, which writes
  `set.toml`'s `[files]` hashes now that Tasks 1.5 and 1.6 are merged (`0.0.0` → `1.0.0`, so
  the bump rule holds). Expected: `set.toml` lists every file of `prompts/landscaping/` except
  itself. Commit `set.toml`.
- [ ] Run `.venv-test/bin/python -m pytest tests/unit/test_no_dead_src_symbols.py -q` and update
  its `ALLOWLIST` per the Global Constraints until it passes.
- [ ] Run `scripts/build_css.sh --check`; on drift, run `scripts/build_css.sh` and commit
  `static/css/app.css`.
- [ ] Run `.venv-test/bin/python -m pytest tests/unit/landscaping tests/unit/test_llm_pricing.py
  tests/unit/test_authorship_roles.py tests/unit/test_industry_sources_parsers.py
  tests/integration/landscaping/test_migration_0077.py -q`. Expected: all pass.
- [ ] Run `./scripts/ci.sh`. Expected: exit 0, covering alembic sanity, the round trip, ruff
  within the ceiling and coverage ≥ 60%.

## Phase 2 — LLM plumbing and epochs

### Task 2.1: Ledger, request builders, synchronous client and fallback chain

**Files:**
- Create: `src/landscaping/ledger.py`, `src/landscaping/llm_client.py`
- Test: `tests/unit/landscaping/test_llm_client.py`, `tests/integration/landscaping/test_ledger.py`

**Interfaces.**
- **Consumes:**
  - `llm.abeta_create(client, **kwargs)`, `llm.get_anthropic_client()`,
    `llm.refusal_category(message)`, `llm.NONSTREAMING_MAX_TOKENS`;
  - `llm_pricing.cost_for_request` and `llm_pricing.PRICES` (Task 1.3);
  - `Settings` (Task 1.1);
  - `LandscapeLlmCall` (Task 1.2).
- **Produces:**

```python
STAGES = ("screen", "extract", "triage", "deep", "deep_extract", "rerank", "similarity", "label", "final")
LEDGER_STAGES = STAGES + ("audit",)  # the audit is built with the audited stage's builder (D66)
@dataclass(frozen=True) class StageSpec: stage: str; model_setting: str; effort: str; max_tokens: int; retry_max_tokens: int
STAGE_SPECS: dict[str, StageSpec]
    # effort (D32, §9.2): screen low; extract and triage medium; deep, deep_extract and final high;
    # rerank, similarity and label medium. Every max_tokens and retry_max_tokens ≤ 21_333.
class UnpricedModelError(Exception)
def require_priced(model: str) -> None  # raises UnpricedModelError when model is not in PRICES (§9.4)
def build_request(stage: str, *, model: str, system: str, user: str, schema: dict | None,
                  tools: list[dict] | None = None, retry: bool = False) -> dict
    # one builder for batch, sync, fallback and bake-off (D32); retry=True uses retry_max_tokens
@dataclass class CallResult: message: Any | None; text: str; stop_reason: str | None;
    refusal_category: str | None; served_by: str; usage: dict; error: str | None
async def call_sync(stage: str, request: dict, *, db_factory, item_ref: str,
                    skip_models: frozenset[str] = frozenset(), ledger_stage: str | None = None) -> CallResult
    # the chain is request["model"] then landscape_fallback_models, de-duplicated; models in skip_models and
    # each model that refuses in this call are skipped (D42). A batch refusal passes skip_models={batch model},
    # so that model is never re-billed. One retry with retry_max_tokens on "max_tokens". require_priced
    # before each attempt. One LandscapeLlmCall row per attempt.
async def record_call(db, *, stage, model, batch_id: uuid.UUID | None, item_seq: int | None, custom_id: str | None,
                      usage, refusal_category, served_by, batch: bool, latency_ms: int | None) -> None
    # a batched row inserts with ON CONFLICT (batch_id, item_seq) DO NOTHING (spec §7.1 step 5)
async def spend_today(db) -> Decimal
async def over_daily_cap(db) -> bool   # False when landscape_daily_usd_cap is None
```

- [ ] **Step 1: Write the failing tests**, with a fake client from `tests/fakes.py` patterns.
  - `test_every_stage_constant_within_ceiling`: every `max_tokens` and `retry_max_tokens` is
    ≤ `NONSTREAMING_MAX_TOKENS`.
  - `test_build_request_shape`: `thinking == {"type": "adaptive"}`; no
    `temperature`/`top_p`/`top_k`; the schema goes in `output_config["format"]`.
  - `test_effort_per_stage`: `output_config["effort"]` equals the table above for every stage.
  - `test_refusal_chain`:
    - a Sonnet refusal tries `claude-opus-5`, then `claude-opus-4-8`;
    - an Opus 5 refusal tries only `claude-opus-4-8`;
    - after the chain, `CallResult.stop_reason == "refusal"`;
    - one ledger row per attempt.
  - `test_batch_refusal_skips_batch_model`: `skip_models={"claude-sonnet-5"}` on a Sonnet
    request starts at `claude-opus-5`; `{"claude-opus-5"}` on an S9 request tries only
    `claude-opus-4-8`.
  - `test_unpriced_model_refused`: `require_priced("claude-unknown")` raises, and `call_sync`
    makes no API call for that model.
  - `test_truncation_retry_once`.
  - `test_ledger_cost_uses_batch_flag`.
  - `test_ledger_batched_row_insert_ignore`: recording one `(batch_id, item_seq)` twice leaves
    one row.
  - `test_over_daily_cap_none_is_false`.
- [ ] **Step 2: Implement.** `call_sync` uses `abeta_create` with no `fallbacks` parameter;
  the chain is client-side (D42). Parse JSON only in callers, via `extract_json`.
- [ ] **Step 3: Commit** `feat(landscaping): ledgered LLM client with fallback chain`.

### Task 2.2: Message Batches wrapper with the intent protocol

**Files:**
- Create: `src/landscaping/batches.py`
- Test: `tests/unit/landscaping/test_batches.py`, `tests/integration/landscaping/test_batches_db.py`

**Interfaces.**
- **Consumes:** `build_request`, `record_call`, `require_priced`, `call_sync`, `CallResult`
  (Task 2.1); `LandscapeBatch`, `LandscapeBatchItem` and the stage-row columns (Task 1.2); the
  `anthropic` client's `messages.batches.create/retrieve/results/list`.
- **Produces:**

```python
OUTCOME_KINDS = ("succeeded", "refusal", "truncated", "errored", "expired", "canceled")
async def submit(db_factory, *, stage: str, row_model: type, items: list[tuple[uuid.UUID, dict]]) -> uuid.UUID
    # items = [(stage_row_id, request)]; require_priced on each model first. One transaction commits a LandscapeBatch
    # row ("submitting", bigint seq, row_table), a LandscapeBatchItem per request (item_seq, row id), and moves the
    # stage rows to "submitting" with their batch_item_id, BEFORE create. custom_id = f"b{seq}-{item_seq}"
    # (^[a-zA-Z0-9_-]{1,64}$). After create: store the API batch id and move the rows to "submitted" (D31).
    # A create that raises leaves the rows "submitting", increments the batch row's create_failures and re-arms
    # reconciliation for that row (spec §7.1 step 4). Its items become "failed:create", terminal and reported, only
    # after the third failed create AND that row's last re-armed scan found no matching batch.
@dataclass class BatchOutcome: batch_row_id: uuid.UUID; batch_item_id: uuid.UUID; item_seq: int; row_id: uuid.UUID;
    stage: str; kind: str; model: str; message: Any | None; usage: dict
async def poll(db_factory, *, chunk_size: int = 1000) -> AsyncIterator[tuple[uuid.UUID, list[BatchOutcome], bool]]
    # one ended, not-yet-ingested batch at a time, streamed from the results endpoint in chunks of at most
    # chunk_size: (batch_row_id, outcomes, is_last_chunk). A succeeded result whose stop_reason is "refusal" or
    # "max_tokens" becomes kind "refusal" or "truncated"; each item's ledger row is written (insert-ignore).
    # The caller marks the batch ingested after its last chunk (§12.2)
async def mark_ingested(db_factory, batch_row_id: uuid.UUID) -> None  # status "ingested": never read again
async def apply_result(db, row_model: type, *, row_id: uuid.UUID, batch_item_id: uuid.UUID, values: dict) -> bool
    # compare-and-set (spec §7.1 step 5, F4): UPDATE … SET values WHERE id = row_id AND status = 'submitted'
    # AND batch_item_id = batch_item_id. True when one row changed. Rows derived from the result (ideas,
    # evidence, links) are written in the same transaction, and only when it returns True (N12).
def transition(kind: str, *, attempts: int, truncated_once: bool, model: str) -> dict
    # values for a non-success outcome (spec §7.1): errored or expired → {"status": "pending", "attempts": attempts + 1}
    # the first time, then "failed:errored" or "failed:expired"; canceled → "pending"; truncated → "pending" with
    # truncated_once=True (the next request is built with retry=True), then "failed:truncated"; refusal →
    # "pending" with fallback_from=model
@dataclass(frozen=True) class FallbackAdapter: stage: str; row_model: type;
    build: Callable[[AsyncSession, Any], Awaitable[dict]];
    on_success: Callable[[AsyncSession, Any, CallResult], Awaitable[None]]
    # build(db, row) rebuilds the row's request from the database (its document or stored dossier, and its
    # epoch's inputs, Task 2.3's epoch_inputs) in a read session closed before the call.
    # each batched stage module exports FALLBACKS: tuple[FallbackAdapter, ...] (Tasks 4.4, 4.5, 4.6, 5.4, 5.5, 6.2)
async def run_fallbacks(db_factory, adapter: FallbackAdapter) -> int
    # pending rows with fallback_from set: call_sync(adapter.stage, await adapter.build(db, row),
    # skip_models={row.fallback_from});
    # success → adapter.on_success and the row's completion in one transaction; an exhausted chain → "refused".
    # Batch submission never selects a row with fallback_from set.
async def reconcile_orphans(db_factory, *, may_submit: bool) -> int
    # non-blocking (spec §7.1 step 4): its first call lists, once, every batch created since the oldest "submitting"
    # row whose id is not already stored, and that list stays fixed. While one of them
    # still runs, it returns 0 and runs again next loop; those rows and their items stay "submitting" (held). Once
    # all have ended, it matches the "b{seq}-" prefixes of their results to "submitting" rows and adopts the matches
    # (stage rows → "submitted"); a row with no match is resubmitted, but only when may_submit (never while disabled,
    # paused or over the daily cap, §9.5). Returns the number adopted.
```

- [ ] **Step 1: Write the failing tests.**
  - `test_intent_row_committed_before_create`: the fake create raises, yet the batch row and
    its stage rows exist in `submitting`.
  - `test_custom_id_matches_api_grammar`: `re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", cid)`.
  - `test_reconcile_adopts_orphan_older_than_24h`.
  - `test_reconcile_returns_while_a_listed_batch_runs`.
  - `test_reconcile_list_is_fixed_at_startup`: a batch submitted after startup never joins it.
  - `test_disabled_reconcile_never_resubmits`.
  - `test_failed_create_rearms_reconciliation`.
  - `test_repeated_create_failure_terminates`: three failed creates and an empty re-armed scan
    give `failed:create`.
  - `test_create_raised_but_batch_exists_is_adopted`: the re-armed scan finds the batch, and the
    row is adopted, not failed.
  - `test_poll_streams_in_chunks`.
  - `test_unmatched_row_resubmitted_once_and_items_held_until_then`.
  - `test_crash_between_create_and_store`: kill after create, restart; exactly one billed
    batch, no resubmission, complete ingestion (Review Focus 2).
  - `test_ingestion_compare_and_set`: replaying a chunk after a crash changes nothing, and a
    late result for an item since resubmitted under a new batch item changes nothing.
  - `test_replayed_errored_or_refused_outcome_fires_once`: no second resubmission and no
    second fallback.
  - `test_ingested_batch_not_read_again`.
  - `test_result_kinds_mapped`: `stop_reason` `"refusal"` gives `"refusal"`, and
    `"max_tokens"` gives `"truncated"`.
  - `test_transition_table`: every kind, on its first and second occurrence.
  - `test_fallback_rows_never_batched`.
  - `test_results_out_of_order_keyed_by_custom_id`.
  - `test_usage_recorded_with_batch_true`.
  - `test_submit_refuses_unpriced_model`.
- [ ] **Step 2: Implement.** `batches.py` owns the protocol and the generic transitions; each
  stage task owns its `succeeded` handling.
- [ ] **Step 3: Commit** `feat(landscaping): batches with intent rows and orphan reconciliation`.

### Task 2.3: Judgment epochs

**Files:**
- Create: `src/landscaping/epochs.py`
- Test: `tests/integration/landscaping/test_epochs.py`

**Interfaces.**
- **Consumes:**
  - `load_rubric().content_hash`;
  - `load_blackbird_extracts().content_hash`;
  - `load_prompt_set().judging_hashes` and `.version`;
  - `load_criteria().judging_hash` and `.version`;
  - `parse_criteria(text, source)`, `prompt_set_from_texts(version, files)`,
    `blackbird_rubric.loaded_rubric_bytes()` and `parse_rubric_bytes(raw, source)`;
  - stage model settings;
  - `LandscapeEpoch`.
- **Produces:**

```python
STAGE_INPUTS: dict[str, tuple[str, ...]]
    # component → the stages that read it (§7.11, §8.1, §8.2): screen.md → screen; extract.md → extract, deep;
    # triage.md → triage; rerank.md → cut, rerank; deep.md → deep; similarity.md → similarity;
    # cluster_label.md → label; final.md → final; criteria → screen, extract, triage, deep, final;
    # rubric → extract, triage, deep, final; extracts → triage, deep, final; each stage model → its stage, and the
    # rerank model → cut and rerank (it also judges the S5 tie-break)
    # personas (the composed specialist personas, panel.compose_persona over PANEL_CHEMISTRY) → deep
DOWNSTREAM: dict[str, tuple[str, ...]]  # screen → extract → triage → cut → deep → rerank, similarity, label; label → final; deep → final
def current_components() -> dict
    # the identifying components (D61): judging hashes, the criteria judging hash, the rubric and extract hashes,
    # "personas" (sha256 over the composed specialist personas in PANEL_CHEMISTRY order, read by S6),
    # and the eight stage model settings, never the fallback chain, the audit model or the chat model.
    # The set and criteria versions are recorded, not compared.
def build_snapshot() -> dict
    # the judging inputs on disk and in settings: {"prompt_set_version", "prompt_files": {each JUDGING_FILES name: text},
    # "criteria": criteria.toml text, "extracts": {standards, practice, contract, scout_hub_version, content_hash},
    # "rubric": the loaded rubric document's text, "models": {screen, extract, triage, deep, rerank, similarity,
    # label, final: model}, "personas": {each panel.PANEL_CHEMISTRY domain: panel.compose_persona(domain)}}
@dataclass(frozen=True) class EpochInputs: epoch_id: uuid.UUID; ps: PromptSet; ex: BlackbirdExtracts;
    criteria: Criteria; rubric: Rubric; models: dict[str, str]; personas: dict[str, str]
    def system(self, stage: str) -> str  # prompt_set.system_prompt(stage, ps=, ex=, criteria=, rubric=)
    def model(self, stage: str) -> str   # deep_extract takes the deep model
    def persona(self, domain: str) -> str  # the composed specialist persona S6 consults with (KeyError if unknown)
async def epoch_inputs(db, epoch_id) -> EpochInputs
    # rebuilt from the epoch's snapshot, never the files or settings (§7.11); cached per epoch in-process;
    # LookupError for an epoch with no snapshot
async def active_epoch(db) -> LandscapeEpoch | None
async def ensure_candidate(db) -> LandscapeEpoch | None
    # creates a "candidate" when the identifying components differ from the active epoch's, or when none is active;
    # an unactivated candidate with other components is retired first; the new one is built from the active epoch,
    # or, while none is active, from the retired candidate's unchanged stages (D65). Every epoch it creates, the
    # first included, stores build_snapshot().
def changed_stages(old: dict, new: dict) -> tuple[str, ...]  # STAGE_INPUTS of each changed component, plus DOWNSTREAM
async def hold(db, epoch_id, reason: str) -> None
async def override(db, epoch_id, *, actor_id, reason: str) -> None
async def activate(db, epoch_id, *, on_activate: Callable[[AsyncSession, uuid.UUID | None, uuid.UUID], Awaitable[None]]) -> None
    # one transaction: on_activate(db, old_active_id, epoch_id) (Task 5.4's activate_groupings, passed by the
    # worker), then candidate → active and old active → retired; on_activate raising rolls the whole switch back
async def copy_forward(db, *, from_epoch, to_epoch, stages: tuple[str, ...]) -> None
    # copies rows of unchanged upstream stages into the candidate (§7.11 step 2) and stamps its rejudge_started_at;
    # a copied cut's idea ids (ordered_idea_ids, and the folds' keepers, folded ids and tie class) are repointed
async def mark_drained(db, *, epoch_id) -> bool
    # acts only in phase "continuous": sets drained_at once no S2, S3, S4 or evidence-assembly work is pending for
    # any item created before rejudge_started_at. At the D69 flip the worker sets the current candidate's
    # drained_at to now, overwriting any earlier value (spec §7.11 step 4)
```

- [ ] **Step 1: Write the failing tests.**
  - `test_first_epoch_is_candidate`.
  - `test_no_change_no_candidate`.
  - `test_chat_md_and_reason_tags_create_no_candidate` (D61).
  - `test_model_change_marks_stage_and_downstream`: changing the triage model gives
    `("triage", "cut", "deep", "rerank", "similarity", "label", "final")`.
  - `test_second_change_supersedes_candidate` (D65).
  - `test_second_change_without_active_epoch_keeps_unchanged_stages` (D65).
  - `test_mark_drained_ignores_items_after_rejudge_start`.
  - `test_fallback_audit_and_chat_models_are_not_components`.
  - `test_activate_is_atomic_and_single_active`.
  - `test_activate_hook_runs_in_same_transaction`: a hook that raises leaves the candidate a
    candidate and the old epoch active.
  - `test_override_records_actor_and_reason`.
  - `test_unchanged_upstream_rows_copied_forward`.
  - `test_rerank_model_change_marks_cut`.
  - `test_copy_forward_never_writes_active_rows`: `copy_forward` and `ensure_candidate` leave
    every active-epoch row unchanged. The full re-judge test is Task 6.5's (X1).
  - `test_epoch_inputs_use_the_snapshot_not_the_files`: a judging prompt edited after the epoch
    was created leaves `epoch_inputs(...).system(stage)` on the stored text.
  - `test_copy_forward_remaps_fold_ids`.
- [ ] **Step 2: Implement.**
- [ ] **Step 3: Commit** `feat(landscaping): judgment epochs`.

### Phase 2 verification

- [ ] Run `.venv-test/bin/python -m pytest tests/unit/test_no_dead_src_symbols.py -q` and update
  its `ALLOWLIST` per the Global Constraints until it passes.
- [ ] Run `scripts/build_css.sh --check`; on drift, run `scripts/build_css.sh` and commit
  `static/css/app.css`.
- [ ] Run `.venv-test/bin/python -m pytest tests/unit/landscaping tests/integration/landscaping -q`.
  Expected: all pass.
- [ ] Run `./scripts/ci.sh`. Expected: exit 0.

## Phase 3 — Intake (S0-S1)

### Task 3.1: OpenAlex paging and the OpenAlex works source

**Files:**
- Modify (additive): `src/services/openalex.py`
- Create: `src/landscaping/sources/__init__.py`, `src/landscaping/sources/openalex_works.py`
- Test: `tests/unit/landscaping/test_source_openalex.py`

**Interfaces:** produces

```python
# sources/__init__.py: the shared record types every source and intake use
@dataclass class SliceResult: rows: list["DocumentRecord"]; expected: int; received: int
@dataclass class DocumentRecord: kind: str; ids: dict[str, str]; title: str; summary: str | None;
    published: date | None; doc_type: str; language: str | None; license: str | None;
    authorships: list[dict] | None; role_basis: str | None; raw_qualifier: dict
# openalex.py (new, additive)
async def iter_works(filter_: str, select: str, *, per_page: int = 200) -> AsyncIterator[tuple[list[dict], int]]
    # cursor paging with the key (openalex_auth), budget check (openalex_budget), retries; yields
    # (page_rows, meta_count) and never truncates
async def fetch_work(openalex_id: str, select: str) -> dict | None  # single-record lookup (free)
# sources/openalex_works.py
LINEAGE_FILTER: str  # "authorships.institutions.lineage:I145311948|I2799853436|I1298154867|I4210129832|I4210114877|I4405267562"
SELECT: str          # spec §6.1 select list
async def fetch_slice(date_from: date, date_to: date) -> SliceResult  # rows already mapped by to_document
def to_document(work: dict) -> DocumentRecord
```

- [ ] **Step 1: Write the failing tests**, on a fake HTTP transport.
  - `test_iter_works_follows_cursor_to_end`, with no 5-page cap.
  - `test_slice_counts_expected_vs_received`.
  - `test_refetch_when_100_authorships` (D39): a work listed with exactly 100 authorships is
    replaced by `fetch_work`.
  - `test_existing_list_works_unchanged`: `_list_works` still stops at `_MAX_PAGES`.
- [ ] **Step 2: Implement.** `_list_works` and `fetch_works_by_orcid` stay untouched.
- [ ] **Step 3: Commit** `feat(landscaping): OpenAlex institution source`.

### Task 3.2: PubMed additive helpers and the PubMed works source

**Files:**
- Modify (additive): `src/services/pubmed.py`
- Create: `src/landscaping/sources/pubmed_works.py`
- Test: `tests/unit/landscaping/test_source_pubmed.py`

**Interfaces:** produces

```python
# pubmed.py (new, additive)
def set_min_interval(seconds: float | None) -> None
    # a process-wide floor under _pace_interval(), which every _ncbi_get call already waits on, so reused
    # lookup_pmcids, convert_dois_to_pmids and project_search.search_pubmed are covered (D58, spec §14).
    # None, the state at import, leaves today's interval exactly as it is.
async def esearch_page(term: str, *, retstart: int, retmax: int, sort: str = "pub date") -> tuple[int, list[str]]
async def fetch_pubmed_xml(pmids: list[str]) -> str  # efetch XML, batches of EFETCH_BATCH_SIZE
def parse_pubmed_extras(xml_text: str) -> dict[str, dict]  # pmid -> {abstract, mesh, keywords, grants, publication_types, retracted, last_author, orcids}
# sources/pubmed_works.py
AFFILIATION_TERM: str  # '("Johns Hopkins"[ad] OR "Kennedy Krieger"[ad] OR "Jhpiego"[ad])'
async def fetch_month(year: int, month: int) -> SliceResult       # backfill by [dp]; refuses a slice ≥ 9,999
async def fetch_edat(day_from: date, day_to: date) -> SliceResult  # daily incremental
def to_document(record: dict, extras: dict) -> DocumentRecord     # role_basis "pubmed_last_author"
```

- [ ] **Step 1: Write the failing tests.**
  - `test_slice_over_9999_refused`.
  - `test_extras_parse_mesh_grants_retraction` on a fixture XML.
  - `test_last_valid_individual_author`: a `CollectiveName` is skipped.
  - `test_set_min_interval_floors_every_call`: with a key configured, after
    `set_min_interval(1 / 3)`, `_pace_interval()` returns ≥ 1/3, and `set_min_interval(None)`
    restores 0.11.
  - `test_default_interval_unchanged`: without the setter, `_pace_interval()` returns 0.11 with
    a key and 0.34 without.
  - `test_existing_parser_output_unchanged`.
- [ ] **Step 2: Implement.** `_parse_pubmed_xml`, `search_pmids` and the module pacer stay
  unchanged.
- [ ] **Step 3: Commit** `feat(landscaping): PubMed affiliation source`.

### Task 3.3: RePORTER and NSF sources

**Files:**
- Create: `src/landscaping/sources/reporter.py`, `src/landscaping/sources/nsf.py`
- Test: `tests/unit/landscaping/test_source_grants.py`

**Interfaces:** produces `async def fetch_fiscal_year(fy: int) -> SliceResult`. It uses
`nih_reporter.search_projects({"org_names_exact_match": list(REPORTER_ORGS), "fiscal_years":
[fy]}, PROJECT_FIELDS + ["AbstractText", "PrefTerms", "ApplId"], max_total=5000)` (RePORTER returns
`appl_id`, the §6.1 upsert key, only when asked), with `expected` from RePORTER's own `meta.total`
(one 1-row request), paging by offset at
≤ 500 rows, paced by `nih_reporter`'s own module pacer, unchanged. It also produces `async def fetch_nsf(date_from, date_to) ->
SliceResult` and `to_document` for each (`role_basis` `grant_pi`).

- [ ] **Step 1: Write the failing tests.**
  - `test_reporter_criteria_and_fields`.
  - `test_reporter_groups_by_core_project`.
  - `test_nsf_paging_rpp_25`.
- [ ] **Step 2: Implement.** Pass `max_total` as a keyword; do not change `nih_reporter.py`.
- [ ] **Step 3: Commit** `feat(landscaping): grant sources`.

### Task 3.4: Patent, JHTV and dataset sources

**Files:**
- Create: `src/landscaping/sources/odp_patents.py`, `src/landscaping/sources/jhtv.py`,
  `src/landscaping/sources/datacite.py`, `src/landscaping/sources/geo.py`
- Test: `tests/unit/landscaping/test_source_misc.py`

**Interfaces:** produces `fetch_quarter(y, q)` (ODP applicant or assignee "Johns Hopkins" /
"Kennedy Krieger", reusing `odp.ODP_PACER`, `odp_client` and `odp_headers`, with the key read as
`patents.py:468-470` does), `fetch_rss()` (JHTV), `fetch_datacite(date_from, date_to)` and
`fetch_geo(date_from, date_to)`, each returning `SliceResult`, plus `to_document`.
- Patents use `role_basis` `assignee`, JHTV `jhtv`.
- The `jhtv` source starts disabled (Task 3.5's `ensure_sources`) until a person has read its
  terms of use (spec §15).
- Datasets are always `kind="dataset"` and evidence-only (D3).
- If inventor cities are present (a Phase 0 probe), a patent whose inventors are all in
  Laurel or North Laurel is excluded at S1 (`excluded:apl`).

- [ ] **Step 1: Write the failing tests.**
  - `test_jhtv_rss_parses_caseid_guid_pubdate`.
  - `test_geo_inst_variants_query`.
  - `test_odp_query_shape`.
  - `test_apl_inventor_city_exclusion_when_present`.
  - `test_datasets_never_idea_sources`.
- [ ] **Step 2: Implement.** ODP's field names come from the Phase 0 probe. Until then the
  query copies `uspto_inventor.py:103-104` without the inventor clause, and the parser
  tolerates missing fields (logged).
- [ ] **Step 3: Commit** `feat(landscaping): patent, JHTV and dataset sources`.

### Task 3.5: Intake orchestration, document identity and S1

**Files:**
- Create: `src/landscaping/intake.py`, `src/landscaping/qualify.py`, `src/landscaping/labs.py`
- Test: `tests/integration/landscaping/test_intake.py`, `tests/unit/landscaping/test_qualify.py`

**Interfaces.**
- **Consumes:** every source's `SliceResult` and `to_document`; `institutions.qualifies_openalex`;
  `nih_reporter.publications_for_cores`; the `LandscapeSource`, `LandscapeFetch`,
  `LandscapeDocument`, `LandscapeDocumentLink` and `LandscapeLab` models.
- **Produces:**

```python
# DocumentRecord and SliceResult come from src/landscaping/sources/__init__.py (Task 3.1)
def canonical_id(ids: dict[str, str]) -> str   # precedence doi > pmid > openalex > other (D38)
async def run_slice(db_factory, source: str, slice_key: str, fetch: Callable[[], Awaitable[SliceResult]]) -> FetchOutcome
    # writes LandscapeFetch (expected/received; "incomplete" when ratio < 0.99, retried with backoff)
async def upsert_document(db, rec: DocumentRecord) -> uuid.UUID
    # matches existing by any shared id (a match on a tombstone follows merged_into); re-keys in place to the
    # highest-precedence id; recomputes S1; the active epoch's S2/S3 re-run only when the summary text changed (D38)
def s1_status(rec: DocumentRecord, *, today: date, window_days: int) -> str  # "eligible" or "excluded:<reason>"
async def schedule_due(db, *, now: datetime) -> list[tuple[str, str]]
    # (source, slice_key) due now, by app_settings.landscape_phase: pilot_slices in "pilot"; monthly slices, oldest
    # first, in "backfill", the NCBI-heavy ones only in NCBI off-peak hours (weekends, or 21:00-05:00 US Eastern, K8);
    # the D17 cadence in "continuous". A disabled source is never due.
async def ensure_sources(db) -> None
    # inserts the missing LandscapeSource rows ("jhtv" disabled, spec §15); never changes an existing row
def pilot_slices(*, today: date, seed: int) -> list[tuple[str, str]]  # one random month per window year, every enabled source (D68)
async def find_document(db, *, doi: str | None = None, pmid: str | None = None, title: str | None = None) -> uuid.UUID | None
    # DOI, else PMID, else an exact normalized title; never a tombstone (D67)
async def link_grants(db) -> int  # publications_for_cores → "grant-cites" links to stored papers, then unify_grant_pi per link
async def link_datasets(db) -> int  # a DataCite or GEO record's related DOI or PMID → a "dataset-of" link (D3)
async def ingest_positive_controls(db) -> int
    # each app_settings.landscape_positive_controls DOI or PMID, fetched singly (OpenAlex, else PubMed) and stored
    # through S1, so the pilot can trace it (D68)
def summary_of(rec: DocumentRecord, pubmed_abstract: str | None) -> str | None  # PubMed abstract first, else OpenAlex reconstructed (m-N)
async def merge_documents(db, keep_id, drop_id) -> None
    # two stored rows found to be one work (spec §6.1, F10): drop_id becomes a tombstone (merged_into = keep_id)
    # whose own S2/S3 rows stay attached to it as history; LandscapeDocumentLink rows and later epochs' idea
    # evidence point at keep_id; keep_id's S2/S3 re-run only when its summary is the one replaced.
    # A tombstone is never selected for S1-S3 or a dossier again.
# labs.py
def lab_key(rec: DocumentRecord) -> str | None  # spec §5.2 table: orcid > oa > nih > name:<surname>|<initial>
async def resolve_lab_aliases(db, keys: list[str]) -> str
    # one record, two keys for one person: all resolve to the highest-ranked; aliases recorded in LandscapeLab
async def unify_grant_pi(db, *, grant_key: str, paper_key: str, grant_name: str, paper_name: str) -> bool
    # called for a grant-paper pair that publications_for_cores links; unifies only when the names agree on
    # surname and first initial (spec §5.2)
async def unify_name_key(db, name_key: str) -> str
    # a patent inventor's or JHTV contact's name: key joins an existing lab only when exactly one Baltimore lab
    # has that name key; otherwise it stays its own lab and the collision is recorded for the metrics (Task 6.4)
```

- [ ] **Step 1: Write the failing tests.**
  - `test_pubmed_first_then_openalex_rekeys_in_place`: one document; `role_basis` changes from
    `pubmed_last_author` to `last`; S2 is kept when the summary is unchanged (Review Focus 1).
  - `test_summary_change_resets_s2_s3`.
  - `test_pubmed_abstract_preferred_over_openalex`.
  - `test_merge_two_existing_documents_tombstones_the_other`: the dropped row gets
    `merged_into`, keeps its own S2 and S3 rows, its document links point at the kept row, and
    neither the tombstone nor its rows are selected again.
  - `test_upsert_matching_a_tombstone_updates_the_kept_row`.
  - `test_lab_key_every_record_type`: OpenAlex, PubMed-only, NIH, NSF, patent and JHTV each
    yield a non-null lab key.
  - `test_one_record_two_keys_resolve_to_orcid`: an OpenAlex authorship with an ORCID maps its
    `oa:` key to the `orcid:` key.
  - `test_grant_pi_unified_through_publication_link`: agreeing names unify; a different first
    initial does not.
  - `test_name_key_unified_only_when_unique`: one matching lab unifies; two leave a separate
    `name:` lab and a recorded collision.
  - `test_incomplete_slice_flagged_and_retried`.
  - `test_s1_types_and_window`:
    - review → `excluded:type_review_evidence_only`;
    - retracted → `excluded:retracted`;
    - non-English title → `excluded:language`;
    - outside the window → `excluded:window`.
  - `test_preprint_article_link`.
  - `test_schedule_cadence`: daily PubMed, OpenAlex and JHTV; weekly OpenAlex full sweep, NSF,
    ODP, DataCite and GEO; monthly RePORTER (D17, spec §6.1).
  - `test_backfill_ncbi_slices_only_off_peak` (K8).
  - `test_pilot_slices_one_month_per_window_year` (D68).
  - `test_jhtv_starts_disabled`.
  - `test_ensure_sources_never_changes_existing_rows`: an enabled `jhtv` stays enabled.
  - `test_find_document_precedence_skips_tombstones`.
  - `test_link_grants_links_and_unifies`.
  - `test_link_datasets`.
  - `test_positive_controls_ingested_by_id`.
- [ ] **Step 2: Implement.**
- [ ] **Step 3: Commit** `feat(landscaping): intake, identity and S1`.

### Phase 3 verification

- [ ] Run `.venv-test/bin/python -m pytest tests/unit/test_no_dead_src_symbols.py -q` and update
  its `ALLOWLIST` per the Global Constraints until it passes.
- [ ] Run `scripts/build_css.sh --check`; on drift, run `scripts/build_css.sh` and commit
  `static/css/app.css`.
- [ ] Run `.venv-test/bin/python -m pytest tests/unit/landscaping tests/integration/landscaping
  tests/unit/test_openalex*.py tests/unit/test_pubmed*.py -q`. Expected: all pass, including the
  unchanged OpenAlex and PubMed tests.
- [ ] Run `./scripts/ci.sh`. Expected: exit 0.

## Phase 4 — Funnel S2-S5

### Task 4.1: Evidence clients and cache

**Files:**
- Create in `src/landscaping/evidence/`: `__init__.py`, `cache.py`, `pubtator.py`, `hgnc.py`,
  `mondo.py`, `opentargets.py`, `chembl.py`, `trials.py`, `openfda.py`, `orphadata.py`,
  `comention.py`, `uniprot.py`, `hpa.py`
- Test: `tests/unit/landscaping/test_evidence_clients.py`

**Interfaces:** produces (each paced; 429 backoff; results cached in `LandscapeEvidenceCache`
by `(source, release, query_key)`)

```python
async def cached(db, source: str, release: str, key: str, fetch: Callable[[], Awaitable[dict]]) -> dict
    # release: the source's published release where it has one (Open Targets, ChEMBL, Mondo, Orphadata, HGNC),
    # else the ISO week of the fetch. A newer release seen for a source deletes that source's older rows (§11.2)
async def pubtator_annotations(pmids: list[str]) -> dict[str, list[dict]]  # ≤ 3 req/s
async def hgnc_lookup(symbol: str) -> list[dict]                       # symbol/alias/prev search results
async def mondo_from_mesh(mesh_id: str) -> str | None; def mondo_label_match(text: str) -> str | None
def mondo_parents(mondo_id: str) -> frozenset[str]; def is_ancestor(a: str, b: str) -> bool
    # from the Mondo release files (SSSOM mappings + ontology), loaded once per release
async def ot_target(ensembl_id: str) -> dict
    # tractability, isEssential, depMapEssentiality, safetyLiabilities, geneticConstraint,
    # functionDescriptions, baselineExpression, subcellularLocations, chemicalProbes,
    # drugAndClinicalCandidates, homologues
async def ot_association(ensembl_id: str, mondo_id: str) -> dict  # overall + genetic score
async def chembl_mechanisms(target_chembl_id: str) -> list[dict]; async def chembl_molecule(name: str) -> list[dict]
async def trials_count(condition: str, intervention: str) -> dict  # by phase; reuses project_search's 1 req/s pacer
async def fda_approvals(generic_name: str) -> list[dict]  # keyless budget ≤ 1,000/day
async def orphadata_prevalence(orpha_code: str) -> dict | None
async def comention_counts(target: str, disease_mesh: str, *, window: tuple[date, date]) -> dict
async def uniprot_function(accession: str) -> dict  # protein function text (§7.7 step 4)
async def hpa_expression(ensembl_id: str) -> dict  # tissue expression (§7.7 step 4)
```

- [ ] **Step 1: Write the failing tests**, on recorded fixture JSON:
  - `test_cache_hit_skips_fetch`.
  - `test_newer_release_expires_old_rows`.
  - `test_ot_target_fields_parsed`: every field above.
  - `test_mondo_ancestor`.
  - `test_pacer_rates`: PubTator at `landscape_pubtator_max_rps`, openFDA within
    `landscape_openfda_daily_requests`.
  - `test_fda_daily_budget_stop`.
  - `test_trials_reuses_project_pacer`.
- [ ] **Step 2: Implement.** The GraphQL queries use exactly the fields verified live on
  2026-10-30. A counter logs a warning when one backfill passes 20,000 Open Targets queries,
  §6.3's trigger. Switching to release files stays the builder's decision (§17).
- [ ] **Step 3: Commit** `feat(landscaping): evidence clients`.

### Task 4.2: Normalization and the quote check

**Files:**
- Create: `src/landscaping/normalize.py`, `src/landscaping/quotes.py`
- Test: `tests/unit/landscaping/test_normalize.py`, `tests/unit/landscaping/test_quotes.py`

**Interfaces:** produces

```python
def verify_quote(quote: str, text: str) -> bool  # NFKC fold + whitespace collapse + casefold containment
def apply_quote_check(extraction: dict, text: str) -> tuple[dict, int, int]  # (cleaned, verified, dropped); unverified rubric items -> "unclear"
async def normalize_targets(targets: list[dict], *, pubtator: list[dict], criteria) -> list[dict]  # adds hgnc_id | provisional_key
async def normalize_disease(disease: dict, *, pubtator: list[dict]) -> dict  # adds mondo_id | provisional_key
async def normalize_agent(agent: dict) -> dict  # adds chembl_id when exactly one ChEMBL match
```

- [ ] **Step 1: Write the failing tests.**
  - `test_quote_normalization`: curly quotes, line breaks and case.
  - `test_unverified_rubric_item_becomes_unclear`.
  - `test_pubtator_first`.
  - `test_ambiguous_alias_left_unmatched`: an alias mapping to two genes, and a word on the
    criteria list such as "SET", both stay unmatched.
  - `test_mouse_gene_maps_to_human_ortholog_via_homologues`.
  - `test_disease_mesh_to_mondo_exact_only`.
- [ ] **Step 2: Implement.**
- [ ] **Step 3: Commit** `feat(landscaping): normalization and quote check`.

### Task 4.3: Ideas: canonical keys, merge, aliases, versions, dossiers, flags

**Files:**
- Create: `src/landscaping/ideas.py`, `src/landscaping/flags.py`
- Test: `tests/integration/landscaping/test_ideas.py`, `tests/unit/landscaping/test_flags.py`

**Interfaces:** produces

```python
def idea_key(candidate: dict, *, senior_lab: str | None) -> str  # D9 / L59: biology (targets, direction, disease); chemistry (chembl | name+lab, targets, disease)
async def merge_candidate(db, *, epoch_id, extraction_id, index, candidate, document) -> uuid.UUID  # ideas are per epoch
    # L21 unknown-direction attachment decided once (D27); links biology<->chemistry from one document
async def resolve_aliases(db, *, epoch_id) -> int  # provisional keys now normalized -> merge with FK-preserving alias rows (D36)
def version_hash(qualifying_document_ids: Iterable[uuid.UUID]) -> str
    # D37: only qualifying idea-source documents, never tombstones. Computed only when a qualifying document
    # attaches, so tombstoning never changes a stored version (spec §7.4)
def dossier_documents(idea, documents) -> list  # ≤ 20: grants, patents, JHTV first, then newest papers (D35)
def group_counts(idea, documents) -> tuple[int, int]  # (jhu_labs, outside_groups); outside filled by S6
def group_candidates(candidates: list[dict], *, senior_labs: dict[uuid.UUID, str | None]) -> dict[str, list[dict]]
    # the merge rule of merge_candidate (keys, the L21 unknown-direction rule) in memory, with no stage-row writes,
    # for the bake-off (Task 6.1); each candidate carries its document_id, and senior_labs maps document → lab key
async def evidence_payload(db, *, targets: list[dict], disease: dict | None, agent: dict | None) -> dict
    # §7.4 step 5 through the evidence cache (a shared input): the evidence summary and data_flags, without
    # writing an idea; assemble_evidence stores its result, the bake-off uses it directly
def data_flags(ot_target: dict, ot_drugs: dict, trials: dict, *, disease: str) -> dict  # target_essential, safety_liabilities, same_target_in_clinic
async def assemble_evidence(db_factory, *, epoch_id, idea_id) -> None
    # §7.4 step 5, cached per source release: Open Targets target and association, ChEMBL mechanisms, trial counts,
    # co-mention counts, Orphadata prevalence; stores the evidence summary and data_flags on the idea and stamps
    # evidence_assembled_at. Each source is tracked in evidence_status on its own; one still failing after its
    # own three attempts is "failed" and marked unavailable, and assembly has ended once every source is done or
    # failed. It never changes the idea version (D37)
async def apply_lab_unifications(db) -> int
    # D64: in every non-retired epoch, in one transaction, a chemistry idea whose key carries a lab key that is now
    # an alias merges into the idea re-keyed on the canonical lab key, through an alias row (D36); idempotent
```

- [ ] **Step 1: Write the failing tests.**
  - `test_unknown_direction_joins_single_known`.
  - `test_unknown_direction_stays_when_two_known`.
  - `test_attachment_not_revisited` (D27).
  - `test_unregistered_agent_key_includes_lab`.
  - `test_alias_merge_keeps_reviews_visible`: create an opportunity, assessment and review
    referencing the provisional idea, resolve it, and assert the review is still reachable
    (Review Focus 3).
  - `test_db_refresh_does_not_bump_version`.
  - `test_assemble_evidence_sets_flags_without_version_bump`.
  - `test_evidence_failure_ends_after_three_attempts`: two sources fail independently, each
    after its own three attempts, and both show as unavailable in the S4 dossier.
  - `test_tombstones_never_counted`: in the version hash, the group counts and the dossier.
  - `test_tombstoning_keeps_version` (spec §7.4).
  - `test_group_candidates_matches_merge_rule`.
  - `test_lab_unification_rekeys_chemistry_idea_via_alias`, in the active and a candidate
    epoch at once (D64).
  - `test_dossier_cap_order`.
  - `test_flags_from_fixture_ot_payload`.
- [ ] **Step 2: Implement.**
- [ ] **Step 3: Commit** `feat(landscaping): canonical ideas, aliases and flags`.

### Task 4.4: S2 screen and S3 extract runners

**Files:**
- Create: `src/landscaping/stages/__init__.py`, `src/landscaping/stages/screen.py`,
  `src/landscaping/stages/extract.py`, `src/landscaping/schemas.py`
- Test: `tests/integration/landscaping/test_stage_screen_extract.py`

**Interfaces.**
- **Consumes:** `build_request`, `submit`, `poll`, `call_sync`, `system_prompt`, `extract_json`,
  `apply_quote_check`, `normalize_*`, `merge_candidate`, `active_epoch` / `ensure_candidate`.
- **Produces:**

```python
SCHEMAS: dict[str, dict]  # JSON Schemas for screen, extract, triage, deep, rerank, similarity, label, final
FALLBACKS: tuple[FallbackAdapter, ...]  # in stages/screen.py and stages/extract.py, one per stage (Task 2.2)
async def submit_screen_batch(db_factory, epoch_id) -> int  # documents with S1 eligible and S2 pending
async def ingest_screen_outcomes(db_factory, outcomes: list[BatchOutcome]) -> None
    # every ingest: transition() for non-success kinds, the stage's values for "succeeded", both through
    # apply_result; derived rows only when it returns True
async def submit_extract_batch(db_factory, epoch_id) -> int  # S2 yes|unsure documents
async def ingest_extract_outcomes(db_factory, outcomes) -> None
    # quote check, normalize, merge; for a patent or JHTV listing, each linked_publications entry found verbatim in
    # its text and resolved by find_document becomes a LandscapeExtractionLink in the epoch (D67)
```

- [ ] **Step 1: Write the failing tests** with a fake batch layer:
  - `test_screen_yes_and_unsure_go_to_extract`.
  - `test_screen_input_has_no_names_or_journal`.
  - `test_refused_item_goes_down_chain_then_refused`.
  - `test_errored_resubmitted_once_then_failed`.
  - `test_extract_creates_ideas_with_epoch`.
  - `test_derived_rows_written_once_on_replay`.
  - `test_extraction_link_only_when_verbatim_and_resolved` (D67).
  - `test_registered_fence_tags_only`: every `delimit` call uses `publication_abstract`,
    `methods_section`, `patent` or `trial_record`, all present in `FENCE_TAGS`.
- [ ] **Step 2: Implement.** Every untrusted string goes through `delimit(text, tag)` with a
  registered tag (D44).
- [ ] **Step 3: Commit** `feat(landscaping): S2 screen and S3 extract`.

### Task 4.5: S4 triage and scoring

**Files:**
- Create: `src/landscaping/stages/triage.py`, `src/landscaping/scoring.py`
- Test: `tests/unit/landscaping/test_scoring.py`, `tests/integration/landscaping/test_stage_triage.py`

**Interfaces:** produces

```python
def dimension_mean(scores: dict[str, int], rubric) -> float  # Σ wᵢsᵢ/Σwᵢ over the three dims, weights from load_rubric()
def checklist_score(target_rubric: list[dict]) -> float  # 1 + 4 * present/11
def triage_score(scores, target_rubric, *, rubric, criteria) -> float  # α·mean + (1−α)·checklist, α = criteria.dimension_share (L62)
def excluded_reason(result: dict) -> str | None  # "excluded:gate" | "excluded:scope" | None; flags never exclude (L24)
def tie_key(idea_row) -> tuple  # §7.6 keys 1-8: key 4 is the JHU-only group count; stage earlier-first (L56)
def triage_dossier(idea_fields: dict, evidence: dict, documents: list[dict]) -> str
    # the blinded S4 user message (D20), pure; used by submit_triage_batch and by the bake-off
async def submit_triage_batch(db_factory, epoch_id) -> int
    # ideas whose current version lacks a triage row in the epoch and whose evidence assembly has ended (done, or
    # failed:<source>, which the dossier marks unavailable)
async def ingest_triage_outcomes(db_factory, outcomes) -> None
FALLBACKS: tuple[FallbackAdapter, ...]  # triage (Task 2.2)
```

- [ ] **Step 1: Write the failing tests.**
  - `test_triage_score_formula`: dims (5, 4, 3) with 11/11 present gives 0.8·4 + 0.2·5 = 4.2.
  - `test_gate_not_met_excludes`.
  - `test_unconfirmed_passes`.
  - `test_flags_never_exclude`.
  - `test_tie_key_stage_earlier_first`.
  - `test_blind_dossier_has_no_identities`.
  - `test_s4_input_carries_data_flags`.
  - `test_failed_evidence_does_not_block_triage`.
- [ ] **Step 2: Implement.** The weights are read from `load_rubric()`; there are no literals
  25/25/25.
- [ ] **Step 3: Commit** `feat(landscaping): S4 triage and scoring`.

### Task 4.6: Pairwise comparator and the S5 cut

**Files:**
- Create: `src/landscaping/comparator.py`, `src/landscaping/stages/cut.py`
- Test: `tests/integration/landscaping/test_cut.py`, `tests/unit/landscaping/test_comparator.py`

**Interfaces:** produces

```python
async def compare(db_factory, a, b, *, epoch_id, basis: Literal["deep"]) -> int
    # synchronous S7 insertion comparator: two S6 summaries, both orders, cached (§7.8);
    # disagreement → higher S6 provisional weighted_score, then lower id
def tournament_pairs(ids: list, *, full_up_to: int, degree: int = 10, seed: int) -> list[tuple]
    # every pair when len(ids) <= full_up_to (S5: 20; S7 rebuild: 11). Otherwise a balanced 10-regular pairing:
    # shuffle ids with random.Random(seed), then pair position i with i+1 … i+degree//2 (mod n), so every idea
    # meets exactly `degree` distinct others (spec §7.6, §7.8, F8). Each pair is asked in both orders.
async def submit_tournament(db_factory, *, epoch_id, ids, pairs, basis: Literal["triage", "deep"]) -> uuid.UUID  # one Message Batch, both orders
def win_fraction_order(results, *, tiebreak) -> list
    # win fraction = (wins + 0.5 × ties) / pairs met, where a pair whose two orders disagree is a tie;
    # equal fractions fall to `tiebreak` (S5: tie keys 6-8; S7: S6 provisional weighted_score, then id)
async def run_cut(db_factory, epoch_id) -> uuid.UUID | None
    # §7.6: pool (rescued included), order, folds (D34), K; if ranks K and K+1 tie on keys 1-5, submit the boundary
    # tournament (basis "triage", blinded S4 dossiers, full_up_to=20) and return None. The previous cut stays
    # current, and finish_cut(...) completes the snapshot when the batch ends. Then queue S6 (re-queue throttled
    # per batch cycle, D37). One cut per epoch is in flight: a cut due while a boundary batch is pending is
    # skipped, returning None. A candidate that has a complete cut taken after its drained_at takes no further cut
    # until it activates (spec §7.6), returning None.
async def finish_cut(db_factory, *, epoch_id, tournament_batch_id) -> uuid.UUID
async def ingest_tournament_outcomes(db_factory, outcomes) -> None  # comparison winners through apply_result
FALLBACKS: tuple[FallbackAdapter, ...]  # tournament pairs, stage "rerank" (Task 2.2)
async def tournament_complete(db, batch_row_id: uuid.UUID) -> bool
def cut_due(*, now: datetime, s4_pending: int, last_cut_at: datetime | None) -> bool
    # true once no S4 work of the cycle is pending, or at 06:00 UTC, whichever comes first (spec §7.1)
```

- [ ] **Step 1: Write the failing tests.**
  - `test_pool_uses_previous_version_while_new_waits`.
  - `test_deep_gate_excluded_leaves_next_pool`.
  - `test_fold_disease_ancestor`.
  - `test_fold_target_subset`.
  - `test_k_is_ceil_one_percent_of_unfolded`.
  - `test_boundary_tie_class_resolved_by_batch_without_s6`: a tie class across K includes
    ideas never through S6; the cut completes deterministically with no S6 dependency and no
    synchronous call (X2).
  - `test_previous_cut_current_until_boundary_batch_ends`.
  - `test_round_robin_up_to_20_else_balanced_10_regular`: for 20 ids every pair; for 21 and 57
    ids every idea meets exactly 10 distinct others, with no self-pair, and one seed gives one
    draw.
  - `test_win_fraction_counts_a_disagreement_as_half`.
  - `test_cut_skipped_while_boundary_batch_pending`.
  - `test_cut_due_on_drain_or_0600_utc`.
  - `test_candidate_cut_frozen_after_post_drain_cut`.
  - `test_comparator_cached_across_cuts`: a re-cut makes zero calls.
  - `test_rescued_idea_in_pool`.
  - `test_s6_requeue_throttled_per_batch_cycle`.
  - `test_no_refill_after_s6_exclusion`.
- [ ] **Step 2: Implement.** The boundary tournament goes through `submit` (Task 2.2), not
  `call_sync`, so it never waits on a live simulation run.
- [ ] **Step 3: Commit** `feat(landscaping): comparator and S5 cut`.

### Phase 4 verification

- [ ] Run `.venv-test/bin/python -m pytest tests/unit/test_no_dead_src_symbols.py -q` and update
  its `ALLOWLIST` per the Global Constraints until it passes.
- [ ] Run `scripts/build_css.sh --check`; on drift, run `scripts/build_css.sh` and commit
  `static/css/app.css`.
- [ ] Run `.venv-test/bin/python -m pytest tests/unit/landscaping tests/integration/landscaping -q`.
  Expected: all pass.
- [ ] Run `./scripts/ci.sh`. Expected: exit 0.

## Phase 5 — Deep stages S6-S9

### Task 5.1: Full-text retrieval

**Files:**
- Create: `src/landscaping/fulltext.py`
- Test: `tests/unit/landscaping/test_fulltext.py`

**Interfaces:** produces

```python
COMMERCIAL_LICENSES = frozenset({"CC0", "CC BY", "CC BY-SA", "CC BY-ND"})  # D6, normalized forms
@dataclass class FullText: text: str; sections: list[tuple[str, int, int]]; basis: Literal["full_text", "summary", "fetch_failed"];
    route: str | None; license: str | None; truncated: bool
async def fetch_full_text(doc) -> FullText
    # §6.2 order: PMC cloud metadata JSON (license_code, is_manuscript, is_retracted) → text; BioC as the alternate PMC route;
    # Europe PMC fullTextXML when its `license` is in the set; bioRxiv/medRxiv `details` license + `jatsxml`;
    # else summary. Transient errors retried 3× then basis "fetch_failed" (D41). Cap 200,000 chars at section boundaries.
def sections_from_jats(xml: str) -> list[tuple[str, str]]  # title/abstract/intro/results/discussion/methods/captions; refs dropped
```

- [ ] **Step 1: Write the failing tests.**
  - `test_cc_by_nc_is_summary_only`.
  - `test_author_manuscript_is_summary_only`.
  - `test_retracted_skipped`.
  - `test_transient_error_is_fetch_failed_not_summary`.
  - `test_references_dropped`.
  - `test_truncation_at_section_boundary`.
- [ ] **Step 2: Implement.** Use anonymous HTTPS to the `pmc-oa-opendata` bucket's object URLs.
  There is no AWS SDK dependency. Pacing: BioC at `landscape_bioc_max_rps`, Europe PMC at
  `landscape_europepmc_max_rps`.
- [ ] **Step 3: Commit** `feat(landscaping): open-access full text`.

### Task 5.2: S6 deep analysis

**Files:**
- Create: `src/landscaping/stages/deep.py`, `src/landscaping/panel.py`,
  `src/landscaping/liveness.py`
- Test: `tests/integration/landscaping/test_stage_deep.py`, `tests/unit/landscaping/test_panel.py`

**Interfaces.**
- **Consumes:**
  - `fetch_full_text` (5.1), the extract prompt and schema, `apply_quote_check` (4.2);
  - `project_search.research_tool_definitions()`, `execute_research_tool(name, input) ->
    ToolResult`;
  - `project_plan.source_key` / `resolve_source`;
  - `specialists.persona_path(domain)`, `parse_opinion(raw, domain=...)`;
  - `blackbird_rubric.render_stage_bar_markdown(domain)`, `weighted_score`;
  - `call_sync`, `system_prompt`;
  - `ot_target`, `orphadata_prevalence`, `fda_approvals`, `trials_count`, `uniprot_function`,
    `hpa_expression` (Task 4.1), for the disease profile and the target biology (§7.7 steps 3-4).
- **Produces:**

```python
PANEL_BIOLOGY = ("scientific", "chemistry", "clinical", "commercial", "legal", "talent", "budget")
PANEL_CHEMISTRY = PANEL_BIOLOGY + ("technologic",)  # D12
def compose_persona(domain: str) -> str  # persona file with "{stage_bar}" replaced; parity with tools._load_persona_text
async def run_deep(db_factory, idea_id, *, epoch_id) -> None
    # §7.7 steps 1-8: full text + re-extraction (model landscape_deep_model, stage "deep_extract"); literature loop ≤ 8 rounds;
    # disease profile with `support` on every field (estimate tag when not sourced; LLM prevalence always "estimate" unless Orphadata);
    # target biology; gates; flags review; provisional six scores; panel consults; writes LandscapeDeep + LandscapeConsult;
    # a not_met gate -> "excluded:deep_gate". Full-text evidence never bumps the idea version (D28).
    # Runs for ideas in the cut and for blind-batch probe ideas, full text included for both (L64); in a candidate
    # epoch, also for every issued probe (D62).
```

- [ ] **Step 1: Write the failing tests.**
  - `test_compose_persona_parity_with_tools`: compare with `src.agent.tools._load_persona_text`
    for every domain. Only the test imports `tools`; landscaping code imports nothing from
    `src.agent` beyond the read-only names of the Global Constraints.
  - `test_chemistry_idea_adds_technologic`.
  - `test_disease_profile_every_field_has_support`.
  - `test_prevalence_without_orphadata_is_estimate`.
  - `test_target_biology_lines_cite_uniprot_hpa_and_open_targets`.
  - `test_approved_drugs_come_from_openfda`.
  - `test_literature_loop_cites_only_retrieved`.
  - `test_full_text_evidence_does_not_bump_version`.
  - `test_deep_gate_not_met_excludes`.
  - `test_waits_while_simulation_live`: with a fake liveness check returning true, no call is
    made.
  - `test_live_means_engine_lock_held`: `simulation_live` is true whenever `engine_alive` is,
    with a stale heartbeat row too (spec §12.2).
  - `test_probe_idea_gets_full_text`: a probe idea below the cut gets `fetch_full_text` and S6.
- [ ] **Step 2: Implement.** S6 is synchronous and yields while a simulation run is live
  (D33). The liveness check is `src.services.simulation_control.engine_alive(db)`, the signal
  the simulation panel reads. It is wrapped in `src/landscaping/liveness.py` as `async def
  simulation_live(db) -> bool`, so tests can fake it. Create that file in this task.
- [ ] **Step 3: Commit** `feat(landscaping): S6 deep analysis and panel`.

### Task 5.3: S7 rankings

**Files:**
- Create: `src/landscaping/stages/rerank.py`
- Test: `tests/integration/landscaping/test_stage_rerank.py`

**Interfaces:** produces `async def update_rankings(db_factory, *, epoch_id, cut_id) -> None`
and `async def finish_rebuild(db_factory, *, epoch_id, batch_id) -> None` (spec §7.8).
1. It removes ideas that left the cut.
2. When the list is empty, or more than 25% of the cut is new since the last rebuild, it
   submits a rebuild: `submit_tournament(..., basis="deep")` over `tournament_pairs(ids,
   full_up_to=11, seed=…)`.
3. While a rebuild batch is in flight, insertions wait and no later cut submits a second
   rebuild. When it ends, `finish_rebuild` orders the list by `win_fraction_order` (ties: S6
   provisional `weighted_score`, then id), removes ideas that left meanwhile, and inserts the
   ideas that entered meanwhile one at a time.
4. Otherwise it inserts new or changed ideas **one at a time**, by binary search with
   `compare(..., basis="deep")`. It holds `landscape_rerank_lock_key(epoch_id)` through a
   `SessionAdvisoryLock` on a dedicated connection for the whole search, which spans many
   commits (X3, §7.8).

It writes `LandscapeRanking` positions. It also produces `def card_order(...)`: S7 position,
else S5 rank (D30).

- [ ] **Step 1: Write the failing tests.**
  - `test_insertion_uses_log_comparisons`: count comparator calls at K = 75 and K = 400, each
    O(log K) per inserted idea.
  - `test_rebuild_when_empty_or_over_quarter_new`.
  - `test_insertions_wait_while_rebuild_in_flight`.
  - `test_no_second_rebuild_while_in_flight`.
  - `test_rebuild_landing_removes_leavers_and_inserts_entrants`.
  - `test_insertions_serialized`: two concurrent `update_rankings` calls, on two committed
    sessions from the `engine` and `pg_url` fixtures (as `tests/integration/test_engine_lock.py`
    does), never compute positions from the same snapshot, and every adjacent pair was decided
    by an actual comparison (X3).
  - `test_unchanged_recut_no_calls`.
  - `test_removed_idea_no_calls`.
- [ ] **Step 2: Implement.**
- [ ] **Step 3: Commit** `feat(landscaping): S7 incremental rankings`.

### Task 5.4: S8 opportunities

**Files:**
- Create: `src/landscaping/stages/opportunities.py`, `src/landscaping/structured_lines.py`
- Test: `tests/integration/landscaping/test_stage_opportunities.py`,
  `tests/unit/landscaping/test_structured_lines.py`

**Interfaces:** produces

```python
def structured_line(idea) -> str  # D29: "{Direction} {SYMBOL} · {disease} · {modality or 'modality open'}" / chemistry form (§7.10)
def opportunity_edges(ideas) -> list[tuple[uuid.UUID, uuid.UUID, str]]  # L57 joining edges only
def related_edges(ideas) -> list[tuple[uuid.UUID, uuid.UUID, str]]       # gene group, disease parent, shared doc, similarity
async def judge_similarity(db_factory, a, b, *, epoch_id) -> dict  # stage "similarity"; cached per (a, va, b, vb, epoch)
async def build_opportunities(db_factory, *, epoch_id, cut_id) -> None
    # records cut_id on every version and grouping it writes
    # components over joining edges (lab_key + shared target/disease; document links); labels (stage "label",
    # batched) for size ≥ 2; singletons labelled by their structured line, with no call.
    # Active epoch: match_groups against the opportunities' current versions; a matched group becomes a new
    # LandscapeOpportunityVersion when its members or any member's version differ; an unmatched group is a new
    # opportunity; an unmatched opportunity is retired.
    # Candidate epoch: writes LandscapeCandidateGrouping rows only, clears `current` on groupings it no longer
    # produces, and touches no stable row (spec §7.9, F1).
def match_groups(groups: list[frozenset[str]], current: dict[uuid.UUID, frozenset[str]], ages: dict[uuid.UUID, datetime]) -> dict[int, uuid.UUID]
    # one-to-one, by idea_key after resolving both epochs' idea aliases (D64): pairs sharing at least half of the existing
    # opportunity's members; sorted
    # overlap ↓, opportunity age (older first), id ↑; assigned greedily, each side used at most once
async def activate_groupings(db, old_epoch_id: uuid.UUID | None, new_epoch_id: uuid.UUID) -> None
    # the epochs.activate hook (Task 2.3): inside the activation transaction, matches the candidate's groupings
    # against the active epoch's current versions, then mints versions tagged with the new epoch, creates new
    # opportunities, retires unmatched ones, and re-points the assessment of each current grouping to the version
    # minted for it (D59); assessments of groupings no longer current stay with them, as history
async def submit_label_batch(db_factory, *, epoch_id) -> int
    # stage "label", a Message Batch: opportunity versions of size ≥ 2 in the active epoch, candidate groupings in a candidate
async def ingest_label_outcomes(db_factory, outcomes) -> None
FALLBACKS: tuple[FallbackAdapter, ...]  # labels (Task 2.2)
async def make_probe(db_factory, *, epoch_id, idea_id) -> uuid.UUID
    # a single-idea probe opportunity, outside grouping and identity; the probe draw is its only caller (D62)
```

- [ ] **Step 1: Write the failing tests.**
  - `test_same_lab_shared_target_joins`.
  - `test_same_lab_unrelated_does_not_join`.
  - `test_cross_lab_same_target_is_related_only`.
  - `test_biology_chemistry_document_link_joins`.
  - `test_identity_kept_at_half_members`.
  - `test_new_opportunity_below_half`.
  - `test_similarity_cached`.
  - `test_one_to_one_matching_on_split_and_merge`.
  - `test_identity_survives_epoch_switch_by_idea_key`.
  - `test_probe_never_joins_or_matches`.
  - `test_singleton_label_is_structured_line`.
  - `test_candidate_epoch_writes_only_groupings`: no `LandscapeOpportunity` or version row
    changes while a candidate's S8 runs.
  - `test_activation_mints_versions_and_keeps_decisions`: a decision on a matched opportunity
    survives activation, shown as "changed since your decision".
  - `test_activation_repoints_only_current_groupings` (D59).
  - `test_labels_go_through_a_batch`.
  - `test_structured_lines.py::test_structured_line_biology_and_chemistry`.
- [ ] **Step 2: Implement.**
- [ ] **Step 3: Commit** `feat(landscaping): S8 opportunities`.

### Task 5.5: S9 final assessment

**Files:**
- Create: `src/landscaping/stages/final.py`
- Test: `tests/integration/landscaping/test_stage_final.py`

**Interfaces.**
- **Consumes:**
  - `verdict_fields.sidecar_column_kwargs(verdict)`;
  - `src.agent.engine.sidecar._normalize_gating`, imported, not copied (it lives under
    `src/agent/`, so importing it changes nothing there);
  - `blackbird_rubric.weighted_score`, `band`;
  - the final prompt and the contract extract;
  - `structured_line` (Task 5.4).
- **Produces:**

```python
async def submit_final_batch(db_factory, *, epoch_id) -> int
    # active epoch: opportunity versions (probes included) needing assessment, re-assessment ≤ once per batch cycle
    # (D37); candidate epoch: one per current provisional grouping, keyed (epoch_id, grouping_id) (D59), and one per
    # issued probe, keyed by the probe's own (opportunity_id, opportunity_version) with the candidate epoch (D62)
async def candidate_ready(db, *, epoch_id) -> bool
    # spec §7.11 step 4, in order: drained_at is set; the candidate's post-drain cut (taken after drained_at, frozen,
    # no boundary batch pending) exists; S6 has ended for every idea in that cut and S8 has grouped it (cut_id), its
    # label batch ended; then mark_unassessable_probes; then candidate_s9_complete
async def mark_unassessable_probes(db, *, epoch_id) -> int
    # called by candidate_ready only after the drain: an issued probe whose candidate S6 ended terminally (refused,
    # failed:* or excluded:deep_gate), or whose idea has no row in the candidate, gets an S9 row with the terminal
    # status failed:no_deep (spec §7.11 step 4, D59)
async def candidate_s9_complete(db, *, epoch_id) -> bool
    # the S9 clause of candidate_ready: S9 ended (done, failed:* or refused) for every grouping of the post-drain
    # cut and every issued probe (spec §7.11 step 4)
FALLBACKS: tuple[FallbackAdapter, ...]  # S9 (Task 2.2)
async def ingest_final_outcomes(db_factory, outcomes) -> None
    # validates (gating tri-state, clipping), retries once on invalid then "failed:invalid"; computes weighted_score/band;
    # stores claim_support, member_notes, structured_lines; carries earlier decisions forward (L60)
```

- [ ] **Step 1: Write the failing tests.**
  - `test_final_request_carries_no_rank`: no rank, position or card order appears in the S9
    request (spec §7.10, F2).
  - `test_weighted_score_from_existing_function`.
  - `test_invalid_gating_retried_then_failed`.
  - `test_claim_support_covers_every_bullet`.
  - `test_reassessment_at_most_once_per_cycle`.
  - `test_candidate_assessment_keyed_by_grouping` (D59).
  - `test_candidate_scope_includes_issued_probes` (D62).
  - `test_candidate_s9_complete_counts_terminal_failures`.
  - `test_probe_without_deep_gets_failed_no_deep` and `test_probe_idea_absent_gets_failed_no_deep`.
  - `test_absent_probe_idea_not_marked_while_s3_pending`.
  - `test_candidate_ready_requires_s6_and_s8_on_latest_cut`: a pilot cut with finished S9 does
    not count once the drain is later than it.
  - `test_candidate_activates_across_daily_cut_due`: S9 straddling a daily `cut_due` still
    completes, because the post-drain cut is frozen.
  - `test_s9_not_blocked_by_s7`: an opportunity with an unranked member still gets S9.
  - `test_decisions_carried_forward_marked_changed`: decisions are keyed on the opportunity,
    and the new assessment differs from the decided one.
- [ ] **Step 2: Implement.**
- [ ] **Step 3: Commit** `feat(landscaping): S9 opportunity assessments`.

### Phase 5 verification

- [ ] Run `.venv-test/bin/python -m pytest tests/unit/test_no_dead_src_symbols.py -q` and update
  its `ALLOWLIST` per the Global Constraints until it passes.
- [ ] Run `scripts/build_css.sh --check`; on drift, run `scripts/build_css.sh` and commit
  `static/css/app.css`.
- [ ] Run `.venv-test/bin/python -m pytest tests/unit/landscaping tests/integration/landscaping
  tests/characterization -q`. Expected: all pass, characterization unchanged.
- [ ] Run `./scripts/ci.sh`. Expected: exit 0.

## Phase 6 — Evaluation, review batches, worker

### Task 6.1: Bake-off and regression set

**Files:**
- Create: `src/landscaping/eval/__init__.py`, `src/landscaping/eval/bakeoff.py`,
  `src/landscaping/eval/regression.py`
- Test: `tests/integration/landscaping/test_bakeoff_regression.py`

**Interfaces:** produces

```python
async def run_bakeoff(db_factory, *, sample_sizes=(600, 60, 60), candidates=("claude-haiku-5-5", "claude-sonnet-5", "claude-sonnet-5-5")) -> uuid.UUID
    # stratified sample of the pilot documents (OpenAlex field, kind, year; D68); Opus 5 reference via the
    # production builders (D32); S3 candidates grouped in memory by group_candidates, their evidence from
    # evidence_payload (Task 4.3) and their S4 input from triage_dossier (Task 4.5), so no stage row of any epoch
    # is written;
    # metrics per §10.1; refusal before/after fallback; reference-refused items excluded and counted; picks per D43;
    # seeds LandscapeRegressionItem rows; stores a LandscapeReport("bakeoff")
async def freeze_regression_set(db_factory, *, reason: str) -> int
    # new LandscapeRegressionSet version; adds 20 S6/S9 opportunities from the active cut, 30 comparison pairs
    # and 30 similarity pairs, each with the active epoch's stored output, plus the positive controls, rescued
    # items and expert-judged items (§10.4)
async def refresh_after_activation(db_factory, *, epoch_id) -> int  # D65: freeze_regression_set on the new active epoch
async def run_s4_repeatability(db_factory, *, n: int = 50) -> dict
    # §10.2: n pilot ideas scored twice by the chosen model; the share with identical gates and every score within ±1
async def run_regression(db_factory, epoch_id) -> bool
    # spec §10.4 pass rule, on the set version current when the candidate was created: S2-S4 §10.1 thresholds;
    # S6 gates ≥ 90% and scores ±1 ≥ 90%; S7 (and the S5 tie-break) winner ≥ 80% of the 30 pairs; S8 similarity
    # verdict ≥ 85% of the 30 pairs, labels unscored; S9 recommendation ≥ 80%, gates ≥ 90%, scores ±1 ≥ 90%
    # (S6-S9 against the active epoch's stored outputs); expert items ≥ the active epoch's agreement.
    # With no active epoch, only the S2-S4 items are checked, against the bake-off references (D65); a candidate
    # created before any set version exists is judged on the first one, the bake-off's
```

- [ ] **Step 1: Write the failing tests.**
  - `test_cheapest_passing_model_chosen`.
  - `test_bakeoff_writes_no_stage_rows`.
  - `test_no_pass_picks_best_haiku_or_sonnet_and_flags` (D43).
  - `test_reference_refusals_excluded_from_agreement`.
  - `test_regression_fail_holds_epoch`.
  - `test_regression_pass_allows_activation`.
  - `test_final_md_only_change_runs_s9_items` (X9).
  - `test_s7_pairs_pass_at_80_percent` and `test_s8_similarity_pass_at_85_percent`.
  - `test_first_epoch_checks_s2_s4_only` (D65).
  - `test_refresh_after_activation_freezes_new_baseline` (D65).
  - `test_positive_controls_join_the_set`.
  - `test_s4_repeatability_metric`.
  - `test_set_versioned_additions_do_not_change_running_candidate`.
- [ ] **Step 2: Implement.** The bake-off writes recommended settings into its report. It does
  not change `.env`; an operator applies them (Phase 8 runbook).
- [ ] **Step 3: Commit** `feat(landscaping): bake-off and regression gate`.

### Task 6.2: Reject audit and rescue

**Files:**
- Create: `src/landscaping/eval/reject_audit.py`, `src/landscaping/rescue.py`
- Test: `tests/integration/landscaping/test_reject_audit_rescue.py`

**Interfaces:** produces
- `async def submit_reject_audit(db_factory, *, epoch_id) -> int`: one Message Batch of a
  random sample of `landscape_reject_audit_size` per stage of this cycle's S2 `no` and S4
  exclusions, built by the audited stage's builder on `landscape_audit_model`, with ledger stage
  `audit` (D66). It creates the `LandscapeRejectAudit` rows it submits.
- `async def ingest_audit_outcomes(db_factory, outcomes) -> None`: the audit decision and the
  agree flag, through `apply_result`.
- `FALLBACKS: tuple[FallbackAdapter, ...]` for the audit (Task 2.2).
- `async def rescue(db, *, item_kind, item_id, stage, expected_status, actor, reason) -> None`.
  - A compare-and-set update (`WHERE status = expected_status`) sets `rescued`.
  - S2 rescues go to S3; S4 rescues join the S5 pool; `deep_gate` rescues join S7 and S8
    while in the cut (spec §10.5).
  - It records `LandscapeRescue` and joins the regression set.
- `async def carry_rescues(db, *, from_epoch, to_epoch) -> int`: re-applies rescues when the
  same item is excluded again at the same stage, recorded as carried.

- [ ] **Step 1: Write the failing tests.**
  - `test_sample_size_respected`.
  - `test_disagreement_recorded`.
  - `test_rescue_reenters_next_stage_not_cut`.
  - `test_rescue_requires_reason`.
  - `test_rescue_added_to_regression`.
  - `test_rescue_compare_and_set_refuses_stale_status`.
  - `test_deep_gate_rescue_reaches_s7`.
  - `test_rescues_carried_into_new_epoch`.
- [ ] **Step 2: Implement.**
- [ ] **Step 3: Commit** `feat(landscaping): reject audit and rescue`.

### Task 6.3: Weekly review batches, blind draws, expert sample

**Files:**
- Create: `src/landscaping/review_batches.py`
- Test: `tests/integration/landscaping/test_review_batches.py`

**Interfaces:** produces
- `async def issue_weekly_batch(db_factory, *, today: date) -> uuid.UUID | None` (spec §13.1,
  D48):
  - up to 10 opportunities never issued before, in card order (D30), the same batch for
    everyone;
  - every fourth batch is blind: 8 of those plus the 2 probes `draw_probes` made a cycle
    earlier, in a shuffled order stored as each card's `position`. If either probe's S9 is
    not done at issue, the batch is issued as a normal one and the next batch is blind
    instead (D62);
  - fewer available means a smaller batch, possibly none;
  - it refuses while `reason_tags_approved` is false.
- `async def draw_probes(db_factory, *, for_issue_on: date) -> list[uuid.UUID]`: one batch
  cycle before a blind batch, draws 2 ideas at random from ranks K+1…5K of the active cut and
  makes each a probe (`make_probe`, Task 5.4), so its S6, full text included (L64), and S9 are
  ready by issue. Before issue, a probe is replaced by a fresh draw from the same ranks when
  its S6 is `excluded:deep_gate`, when its S6 or S9 ends `refused` or `failed:*`, when its idea
  enters the top K, or when the epoch it was drawn in is retired; the replaced probe
  opportunity is retired. An issued probe is never redrawn or retired (D62).
- `def viewer_cards(batch_items, decisions, shown: dict[uuid.UUID, uuid.UUID | None], retired: frozenset[uuid.UUID], user) -> list`,
  where `shown` maps each opportunity to the assessment the page shows (None when none is
  completed in the active epoch):
  - the viewer's carry-overs: cards in earlier batches with no quick decision from *this*
    viewer, not counted toward the 10. They are grouped by batch, newest first: a normal
    batch in live card order (D30), a blind batch in its stored `position` order. A card whose
    opportunity is retired (by S8 or at an activation) is left out; one with no completed
    assessment in the active epoch stays, marked (D62);
  - "changed since your decision" cards, never for a retired opportunity.
- `async def expert_sample(db_factory, *, quarter) -> list[uuid.UUID]`: 10 per marked expert
  (`app_settings.landscape_experts`), drawn at random from assessed and probe cards on which
  that expert has no quick decision or review (L55, D63). The worker calls it once per
  quarter.

- [ ] **Step 1: Write the failing tests.**
  - `test_ten_cards_same_batch_for_all`.
  - `test_carry_over_is_per_viewer_and_not_counted`.
  - `test_fewer_than_ten_available`.
  - `test_changed_card_shown_not_counted`.
  - `test_every_fourth_blind_with_two_probes_shuffled`.
  - `test_blind_draw_from_k_to_5k`.
  - `test_probes_drawn_one_cycle_ahead`.
  - `test_probe_redrawn_on_gate_top_k_or_retired_epoch` (D62).
  - `test_carry_over_grouped_by_batch_keeping_blind_order` (D62).
  - `test_carry_over_drops_only_retired_cards` (D62): a retired card is neither carried over
    nor shown as changed.
  - `test_issued_probe_with_failed_s9_stays` (D62).
  - `test_probe_not_ready_makes_a_normal_batch_and_the_next_blind` (D62).
  - `test_replaced_probe_retired` (D62).
  - `test_probe_redrawn_on_terminal_failure` (D62).
  - `test_expert_sample_skips_decided_or_reviewed_items` (D63).
  - `test_refuses_until_reason_tags_approved`.
  - `test_expert_sample_ten_per_expert`.
- [ ] **Step 2: Implement.** Expert marking is stored in `app_settings` under the key
  `landscape_experts`, a JSON list of user ids (spec §4 K3, §12.3).
- [ ] **Step 3: Commit** `feat(landscaping): weekly review batches and expert sample`.

### Task 6.4: Reports and metrics

**Files:**
- Create: `src/landscaping/reports.py`
- Test: `tests/integration/landscaping/test_reports.py`

**Interfaces:** produces `async def pipeline_metrics(db) -> dict` with the §10.3 keys:
- counts by stage and status, by source and by institution;
- refusal rate by stage and field;
- spend by day and stage;
- `cost_per_useful_opportunity` (useful = a pursue decision or an `approved` event, L32);
- completeness;
- quote-verification and normalization rates;
- reject-audit disagreement;
- regression runs;
- blind results, for superseded blind batches only (spec §10.6), and expert agreement, with
  items issued before the draw and manager-experts' items reported separately (D63);
- lab-name collisions (spec §5.2);
- the latest cut (N, K, tie-class size).

It also produces `async def drift_report(db) -> dict` (§5.1 drift), `async def
positive_control_trace(db) -> dict` (§10.2), and `async def pilot_report(db_factory) ->
uuid.UUID`, a `LandscapeReport("pilot")` holding (D68):
- the latest bake-off and probes reports;
- completeness per pilot slice and source;
- the S4 repeatability result, and the normalization rate with its ambiguous-name count;
- the positive-control trace: S1-S4, with S5 judged against the pilot pool, or a note that no
  control was entered;
- S6 and S9 truncation rates from the ledger;
- cost and throughput per stage, projected for the backfill.

- [ ] **Step 1: Write the failing tests.**
  - `test_cost_per_useful_opportunity`.
  - `test_counts_by_institution`.
  - `test_drift_lists_unknown_hopkins_institutions`.
  - `test_positive_control_trace_names_dropping_stage`.
  - `test_blind_metric_hidden_until_superseded`.
  - `test_pilot_report_holds_every_section`.
- [ ] **Step 2: Implement.**
- [ ] **Step 3: Commit** `feat(landscaping): pipeline metrics and reports`.

### Task 6.5: The landscaping worker process

**Files:**
- Create: `src/landscaping/worker.py`, ending in `if __name__ == "__main__": run()`. The entry
  point is `python -m src.landscaping.worker`.
- Test: `tests/integration/landscaping/test_worker.py`, `tests/unit/landscaping/test_separation.py`

**Interfaces.**
- **Consumes:** everything above.
- **Produces:** `async def main() -> None`, plus `def run() -> None` for `python -m
  src.landscaping.worker`. It handles SIGTERM/SIGINT the way `src/worker/main.py` does.
- **`INGESTERS: dict[str, Callable]`** routes each `BatchOutcome` by `stage`: `screen`,
  `extract`, `triage`, `label`, `final` and `audit` go to their stage's ingest function;
  `rerank` goes to `ingest_tournament_outcomes`, and a completed tournament then calls
  `finish_cut` (basis `triage`) or `finish_rebuild` (basis `deep`). A batch is marked ingested
  once all its outcomes are in.
- **At start:**
  - take `LANDSCAPE_WORKER_LOCK_KEY` with `SessionAdvisoryLock(settings.database_url, …)`, and
    exit if it is held;
  - exit when `schema_ready` is false (no 0077);
  - load and validate the rubric, extracts, prompt set and criteria, and exit on error;
  - call `pubmed.set_min_interval(1 / settings.landscape_ncbi_max_rps)` (D58);
  - `ensure_sources`, then `ensure_candidate` (the first start makes the first epoch a
    candidate);
  - reset stale `running` items in landscaping tables only.
- **Each loop:**
  - Always, even when disabled or paused: `reconcile_orphans(may_submit=…)`, then poll and
    ingest ended batches chunk by chunk, marking each ingested after its last chunk, and finish
    in-flight HTTP calls (§9.5).
  - Only while `landscape_enabled` is true and `landscape_paused` is false, in §12.2 order.
    While `over_daily_cap` is true, every step still runs except new LLM submissions and
    synchronous LLM calls (§9.5):
    1. due fetches (`schedule_due`), then `link_grants`, `link_datasets`,
       `apply_lab_unifications`, and `resolve_aliases` for each non-retired epoch. In phase
       `backfill`, once the backfill has drained (every enabled source's backfill slices have a
       terminal fetch, complete or incomplete after retries, and no S2-S4 work is pending for
       documents first seen in the backfill), set `landscape_phase = "continuous"` (D69);
    2. evidence assembly, then batch submissions per stage (min items, or max wait), for the
       active epoch and for at most one candidate that has passed regression or been
       overridden. The first candidate's regression waits for the bake-off, which seeds the set
       (spec §7.11 step 1);
    3. synchronous work (S6, S7 insertions, similarity, and `run_fallbacks` over every stage's
       `FALLBACKS`) under `landscape_sync_concurrency`, yielding while
       `liveness.simulation_live` is true (the engine lock is held, spec §12.2);
    4. the cut when `cut_due`, then S8 and its label batch, then the weekly batch issue, then
       reports. S5-S9 never run in phase `backfill`; they run in `pilot` (on the pilot
       documents) and in `continuous` (D69);
    5. epochs: candidate → regression → re-judge (with `carry_rescues`, and S6 and S9 on every
       issued probe, D62) → activate, passing `opportunities.activate_groupings` as the
       activation hook, then `refresh_after_activation` (D65). A candidate activates when
       `candidate_ready` is true (spec §7.11 step 4), never in phase `pilot` or `backfill`
       (D70). `mark_drained` runs each loop for a re-judging candidate; at the D69 flip the
       worker sets the current candidate's `drained_at` to now, overwriting any earlier value;
    6. the reject audit once per cycle (a Message Batch, D66);
    7. `draw_probes` one batch cycle ahead of a blind batch; S6 (full text included) and S9 on
       the probes, never S7 or S8;
    8. the quarterly expert sample;
    9. a bake-off when a `bakeoff_request` report is newer than the latest `bakeoff` report.
       Phase `pilot` (D68), in order: the pilot slices and `ingest_positive_controls`; the
       bake-off once they are in; S2-S9 on the pilot documents only once the stage model
       settings equal the bake-off's picks (the pipeline page shows the wait), with no review
       batch; then `run_s4_repeatability` and `pilot_report`.

  Phase state comes from `app_settings.landscape_phase` (`pilot` | `backfill` | `continuous`).

- [ ] **Step 1: Write the failing tests.** The lock and concurrency tests need two committed
  connections: use the `engine` and `pg_url` fixtures, as `tests/integration/test_engine_lock.py`
  does, not `db_session`.
  - `test_second_instance_exits`.
  - `test_refuses_without_0077`.
  - `test_paused_submits_nothing_but_ingests`.
  - `test_disabled_polls_only`.
  - `test_daily_cap_defers_llm_work_only`: fetches, cuts and the batch issue still run.
  - `test_restart_reconciles_then_resets_stale`.
  - `test_ncbi_interval_set_at_start`, restoring `set_min_interval(None)` in teardown.
  - `test_outcomes_routed_by_stage`.
  - `test_phase_pilot_runs_no_backfill`.
  - `test_pilot_funnel_waits_for_applied_picks`.
  - `test_no_review_batch_before_backfill_cut` (spec §15).
  - `test_backfill_complete_moves_to_continuous` (D69), including a slice left incomplete after
    its retries, and no flip while S2-S4 backfill work is pending.
  - `test_no_activation_in_pilot_or_backfill` (D70).
  - `test_candidate_activates_despite_unassessable_probe` (D59).
  - `test_first_candidate_waits_for_bakeoff`.
  - `test_first_activation_waits_for_full_window_cut`: the pilot groupings are current with
    their S9 done, including for a candidate that superseded the first one when the picks were
    applied during the pilot; after the flip, nothing activates until the post-drain cut's S6,
    S8 and S9 have ended.
  - `test_unpassed_candidate_gets_no_submissions`.
  - `test_candidate_never_changes_active`: run a real candidate re-judge while the active epoch
    serves, and assert the active epoch's cut, statuses, opportunity versions and assessments
    are byte-identical before and after (spec §7.11, X1).
  - `test_never_touches_jobs_table`: assert no SQL against `jobs` is issued.
  - `test_separation.py::test_no_per_pi_or_roster_imports`: no module under
    `src/landscaping/`, nor `src/services/landscape_*.py`, imports `src.models.publication`, `src.models.enrichment` or
    `src.models.pi_company`, or names `ResearcherProfile` or `AgentRegistry` (K12).
- [ ] **Step 2: Implement.** Every unit of work commits on its own (§12.2).
- [ ] **Step 3: Commit** `feat(landscaping): worker process`.

### Task 6.6: Phase 0 probe script

**Files:**
- Create: `scripts/landscaping_probes.py`
- Test: `tests/unit/landscaping/test_probes_script.py` (imports `from scripts import
  landscaping_probes`, as `tests/unit/test_audit_pi_corpus.py` does)

**Interfaces.** `python scripts/landscaping_probes.py <probe>`, run in a one-off container off
the new image (spec §15). Each probe prints its finding and stores it in a
`LandscapeReport("probes")` row, which the pilot report reads:
- `odp-fields`: one applicant query; the field names present, and whether inventor cities are;
- `ortholog`: Open Targets `homologues` against HGNC HCOP on a fixed list of mouse genes;
- `reporter-pi`: one RePORTER project's PI fields, and whether any carries an ORCID;
- `effort`: one request each on Sonnet 5 and Haiku 5.5 through `build_request`, with adaptive
  thinking and `output_config.effort`, reporting acceptance;
- `rate-limits`: ClinicalTrials.gov and Europe PMC at the starting pace, counting 429s.

It also has `enable-source <name>`, which runs `ensure_sources` and then sets that
`LandscapeSource` row's `enabled`, so it works before the worker's first start; it is how an
operator turns on `jhtv` after reading its terms (Task 8.1).

JHTV's terms are read by a person (Task 8.1); S6 and S9 truncation rates come from the ledger,
in the pilot report.

- [ ] **Step 1: Write the failing tests**, on fake transports: `test_each_probe_stores_a_report`,
  `test_unknown_probe_exits_nonzero`, `test_enable_source_with_no_rows_yet`.
- [ ] **Step 2: Implement.**
- [ ] **Step 3: Commit** `feat(landscaping): Phase 0 probe script`.

### Phase 6 verification

- [ ] Run `.venv-test/bin/python -m pytest tests/unit/test_no_dead_src_symbols.py -q` and update
  its `ALLOWLIST` per the Global Constraints until it passes.
- [ ] Run `scripts/build_css.sh --check`; on drift, run `scripts/build_css.sh` and commit
  `static/css/app.css`.
- [ ] Run `.venv-test/bin/python -m pytest tests/unit/landscaping tests/integration/landscaping -q`.
  Expected: all pass.
- [ ] Run `./scripts/ci.sh`. Expected: exit 0.

## Phase 7 — Web: read model, decisions, controls, chat, pages, help

Task order matters here. Every POST route and the chat router exist before Task 7.5 renders
the forms and the drawer that reach them, and only Task 7.4 edits `src/main.py`.

### Task 7.1: Read model

**Files:**
- Create: `src/services/landscape_view.py`
- Test: `tests/integration/landscaping/test_landscape_view.py`

**Interfaces.**
- **Consumes:**
  - `assessment_detail.build_dimension_rows`, `key_point_sections`, `derive_strengths_and_risks`
    (fed dicts shaped like `assessment_detail.py:2147-2170`), `rubric_revisions.resolve_revision`,
    and the gating-definition helpers;
  - the `LandscapeAssessment`, `LandscapeDecision`, `LandscapeReview`, batch and expert models;
  - `viewer_cards` (Task 6.3); `pipeline_metrics`, `drift_report`, `positive_control_trace`
    (Task 6.4).
- **Produces:** plain dicts, never ORM objects, so `src/web/presentation.py` needs no new
  schema entry:

```python
async def can_view(db, user, opportunity_id) -> bool
    # staff: any opportunity; a reviewer: one in an issued batch or in their own expert sample (D50, D63)
async def batch_view(db, user) -> dict
    # this week's batch, the viewer's carry-overs (viewer_cards, D62) and the earlier batches (§13.1). Blind-batch
    # cards hide score, band, rank, dimension and provisional scores, score_rationale and dimension_rationales
    # until the viewer decides (L53, D56). On the viewer's own unsubmitted expert-sample items, every view here and
    # below hides S9's and S6's calls (gates, scores, provisional scores, deal-breaker confirmations, panel opinions,
    # rationales, band, recommendation); on a blind-batch card both hidings apply
    # until both lift (D63). A card with no completed assessment in the active epoch says so and links the last
    # earlier one as history (spec §7.10)
async def lists_view(db, user, *, view: Literal["current", "all"], tab: Literal["unreviewed", "reviewed", "all"],
                     assignment: str) -> dict
    # staff only (L48). Tabs are per viewer: "unreviewed" = no quick decision by this viewer (D60). A viewer with
    # undecided cards in a blind batch sees none of that batch's cards here (F2)
async def detail_view(db, user, opportunity_id) -> dict | None
    # §13.2: the opportunity's current assessment IN THE ACTIVE EPOCH; member sections; history. None (→ 404)
    # unless can_view. Blind-batch cards hide what batch_view hides until the viewer decides. An expert's own
    # sample item hides S9's and S6's calls until they submit (D63). A retired
    # opportunity's page reads as history: its last assessment, marked retired (D62)
async def expert_view(db, user) -> dict  # the viewer's open expert-sample items this quarter (L55)
async def pipeline_view(db, user) -> dict
    # §13.4; probe flags and the blind metric only for superseded blind batches (F2); lists the cards with no
    # completed assessment in the active epoch (spec §7.10)
def visible_votes(user, opportunity_id, decisions, reviews, events) -> tuple[list, list, list]
    # L40/L50/L51: until `user` has a quick decision on this OPPORTUNITY, only the viewer's own items and no
    # count of anyone else's, staff included; a new assessment does not re-blind a viewer who has decided
def changed_since_decision(decision, shown_assessment_id: uuid.UUID | None) -> bool
    # compares landscape_assessment_id (D50); None (no completed assessment) differs from any decided one
def strip_staff_only(view: dict, user) -> dict  # the same staff-only verdict fields presentation.py strips for OpportunityAssessment
```

- [ ] **Step 1: Write the failing tests.**
  - `test_votes_hidden_until_own_decision`, for a reviewer, a manager and an admin (Review
    Focus 5).
  - `test_counts_hidden_too`.
  - `test_own_review_visible_before_deciding`.
  - `test_full_review_does_not_unlock` (L51).
  - `test_tabs_are_per_viewer`: another reviewer's decision moves no card between this
    viewer's tabs and changes no count (D60).
  - `test_blind_batch_hides_score_band_rank`.
  - `test_blind_hides_rationales`.
  - `test_undecided_blind_cards_left_out_of_current_and_all`.
  - `test_probe_flags_hidden_until_superseded`, on the card, detail and pipeline views.
  - `test_reviewer_cannot_get_lists_view`.
  - `test_staff_only_fields_never_reach_reviewer`: a sentinel value in each staff-only field.
  - `test_epoch_switch_shows_last_complete_cut`, with the banner text "Being re-judged under new
    criteria".
  - `test_detail_pins_active_epoch_not_newest`.
  - `test_reviewer_404_outside_issued_batches` and `test_expert_opens_own_unissued_sample_item`.
  - `test_expert_hiding_on_every_surface`: card, lists and detail, until the expert submits
    (D63).
  - `test_blind_and_expert_hiding_both_apply`.
  - `test_card_without_completed_assessment_says_so` (spec §7.10).
  - `test_blind_detail_hides_scores_until_decision` (the chat record's half is Task 7.4's).
  - `test_retired_opportunity_reads_as_history` (D62).
  - `test_pipeline_lists_cards_without_completed_assessment` (spec §7.10).
  - `test_changed_since_compares_assessment_ids`: a new version whose S9 has not run is not
    "changed" (D50).
- [ ] **Step 2: Implement.**
- [ ] **Step 3: Commit** `feat(landscaping): read model with independent votes`.

### Task 7.2: Decisions, reviews, expert judgments and their routes

**Files:**
- Create: `src/services/landscape_reviews.py`
- Modify: `src/routers/reviews.py` (new POST routes)
- Modify (pin): `tests/integration/test_reviews_router.py` (the POST allowlist gains the new
  paths)
- Test: `tests/integration/landscaping/test_landscape_reviews.py`

**Interfaces.**
- **Consumes:** `assessment_reviews._validate` and `_validate_comment`;
  `reviews._parse_dimension_scores`, `_load_assignee` and `_form_str`; `RUBRIC_VERSION`
  stamping; `can_view` (Task 7.1); `load_criteria` (Task 1.5).
- **Produces** these routes on `/reviews`, each one POST:

  | Route | Who | Impersonation |
  |---|---|---|
  | `/landscape/{opportunity_id}/decision` | review user who `can_view` | attributed via `recorded_by`, as assessments do |
  | `/landscape-decisions/{decision_id}/edit` | author only | |
  | `/landscape/{opportunity_id}/review` | review user who `can_view` | |
  | `/landscape-reviews/{review_id}/edit` | author only | |
  | `/landscape-reviews/{review_id}/delete` | admin | |
  | `/landscape/{opportunity_id}/status` | review user who `can_view` | |
  | `/landscape/{opportunity_id}/assign` | staff | refused |
  | `/landscape/{opportunity_id}/unassign` | staff | refused |
  | `/landscape/{opportunity_id}/expert-judgment` | a marked expert, for an item of their own sample | |

  - The service functions mirror `assessment_reviews.submit_feedback`, `edit_feedback`,
    `record_status_event`, `assign_reviewer` and `unassign_reviewer`, without `feedback_mode`
    or `consumed_at` (D21).
  - Each decision and review records the `landscape_assessment_id` of the assessment shown
    when it was made. Tags are validated against `criteria.reason_tags`, and reasons are at
    most 1,000 characters.
  - Dimension scores are validated with `assessment_reviews._validate(score, "log_only",
    dimension_scores)`. Landscaping has no feedback mode (D21); `"log_only"` only satisfies
    `_validate`'s membership check (`assessment_reviews.py:123-131`), and nothing stores it.
  - Every route keyed by `{opportunity_id}` answers 404 when `can_view` is false (D50).
  - The expert judgment stores gate calls, dimension scores, deal-breaker calls and a
    recommendation (L55), and refuses an item outside the expert's own sample.

- [ ] **Step 1: Write the failing tests.**
  - `test_decision_pursue_pass_only`.
  - `test_unknown_tag_rejected`.
  - `test_reason_length`.
  - `test_one_decision_per_reviewer`.
  - `test_review_dimension_scores_validated_against_live_rubric`.
  - `test_feedback_mode_never_stored`.
  - `test_never_enqueues_review_bot`: no job rows, no `prompt_change_suggestions`.
  - `test_assign_refuses_impersonation`.
  - `test_expert_judgment_only_for_own_sample_items`.
  - `test_reviewer_posts_outside_issued_batches_404`, for the decision, review and status
    routes.
  - The allowlist pin updated.
- [ ] **Step 2: Implement.**
- [ ] **Step 3: Commit** `feat(landscaping): decisions, reviews and routes`.

### Task 7.3: Pipeline controls router

**Files:**
- Create: `src/routers/landscape.py`
- Test: `tests/integration/landscaping/test_pipeline_controls.py`

**Interfaces.** POST routes on a router mounted at `/landscape` (Task 7.4 includes it in
`src/main.py`). Every route refuses impersonation; the page wraps each in a confirm (Task 7.5).

| Route | Who | Effect |
|---|---|---|
| `/landscape/pause` | staff | `app_settings.landscape_paused = true` |
| `/landscape/resume` | staff | `landscape_paused = false` |
| `/landscape/backfill/start` | admin | `landscape_phase = "backfill"`; only in phase `pilot` with a stored `pilot` report |
| `/landscape/bakeoff/run` | admin | inserts a `LandscapeReport("bakeoff_request")`, which the worker picks up |
| `/landscape/epochs/{epoch_id}/override` | admin | `epochs.override`; reason required |
| `/landscape/rescue` | staff | `rescue(...)`; reason required (L43) |
| `/landscape/positive-controls` | staff | the `landscape_positive_controls` identifiers |
| `/landscape/experts` | staff | mark or unmark (L55): `landscape_experts` |

Reason-tag approval is not a route: an operator sets `[reason_tags] approved = true` in
`criteria.toml` (L49, D61; Task 8.1). A test pins this exact POST set
(`test_landscape_router_posts_are_an_explicit_allowlist`).

- [ ] **Step 1: Write the failing tests.** They mount `landscape.router` on a FastAPI app built in
  the test with `OriginGuardMiddleware`, since Task 7.4 wires `src/main.py`; the allowlist pin
  reads `router.routes`.
  - The allowlist pin.
  - `test_backfill_start_refused_outside_pilot_or_without_report`.
  - `test_bakeoff_run_records_a_request`.
  - `test_override_requires_reason`.
  - `test_rescue_staff_only`.
  - `test_impersonation_refused_everywhere`.
  - `test_origin_guard_applies`: a POST without a matching Origin is refused.
- [ ] **Step 2: Implement.**
- [ ] **Step 3: Commit** `feat(landscaping): pipeline controls`.

### Task 7.4: Chat on landscaping assessments, and the app wiring

**Files:**
- Create: `alembic/versions/0078_landscape_chat.py`, `src/services/landscape_chat_record.py`,
  `src/routers/landscape_chat.py`
- Modify:
  - `src/models/assessment_chat.py` (column `landscape_opportunity_id`; the three CHECKs;
    `CHAT_SUBJECTS` gains `"landscape"`);
  - `src/services/assessment_chat.py` (an explicit third branch in `ChatSubject`, every
    `_subject_*` helper and `PreparedTurn`; a model chosen per subject; the landscaping
    sub-cap);
  - `templates/assessments/_chat_drawer.html` (a third kind);
  - `src/main.py`: `include_router(landscape.router, prefix="/landscape",
    tags=["landscape"])` (Task 7.3), `include_router(landscape_chat.router,
    prefix="/landscape-chat", tags=["landscape-chat"])`, and
    `application.state.landscape_chat_enabled = settings.landscape_chat_enabled` next to
    `project_chat_enabled` (`src/main.py:280-282`);
  - `scripts/migrate/preflight.py`: the pins move to `0078`; `PLANNED_OBJECTS` gains 0078's
    three columns (one per chat table), three foreign keys and index. The three redefined CHECKs stay out of
    `PLANNED_OBJECTS`, because they recreate existing constraints (`preflight.py:114` notes the
    same for `0065`), and out of `PLANNED_DROPS`, which is tables-only
    (`tests/unit/test_migration_checks.py:959-970`, F7);
  - `scripts/migrate/postflight.py`: `"0078"` joins `VERIFIED_REVISIONS`, with
    `EXPECTED_COLUMNS`, `EXPECTED_INDEXES` and `EXPECTED_CONSTRAINTS` entries for every 0078
    object (`test_migration_checks.py:1312-1352` checks the pairing). The turns check text
    changes, and the two `_subject` expectations (`postflight.py:452-453`, matched as
    substrings at `:739`) change to `'landscape'`, so 0078 is verified;
  - `tests/unit/test_migration_checks.py` (the head pins; the check-text pin at
    `:2207-2208`; the equality pins on the `_subject` expectations at `:2216-2217`, which move
    to `'landscape'`);
  - `tests/integration/test_harness_smoke.py`;
  - `docs/operations/migration-deploy-notes.md` (the 0078 box: new code on the old schema
    breaks every chat, so migrate before serving).
- Test: `tests/integration/landscaping/test_landscape_chat.py`

**Interfaces.**
- **Produces:** `build_landscape_chat_record(db, opportunity_id, user) -> ChatRecord | None`,
  built from the opportunity's current assessment in the active epoch, with documents
  Verdict, Evidence, Disease profile, Panel, Reviews and Rubric.
  - Its Reviews document goes through `visible_votes` (Task 7.1).
  - Its Verdict hides scores, band, rank and both rationale fields for an undecided
    blind-batch card. On an expert's unsubmitted sample item, the record leaves S9's and S6's
    calls out of the Verdict and Panel documents, and any S6 call out of the Disease profile
    and Evidence documents (D63).
  - It returns None (→ 404) unless `can_view`.
- **Router:** the same refusal order as `project_chat.py`: impersonation, `can_review`, both
  switches (`app.state.assessment_chat_enabled` and `app.state.landscape_chat_enabled`), 404,
  415. It answers 503 while `chat_prompt_matches()` is false (spec §8.1).
- **Model:** `llm_landscape_chat_model`.
- **Spend:** checked against the per-user caps, the shared total and
  `settings.landscape_chat_daily_usd_limit`, which Task 1.1's guard keeps finite and positive.

- [ ] **Step 1: Write the failing tests.**
  - `test_model_is_opus_5_for_landscape_subject`.
  - `test_assessment_subject_model_unchanged`.
  - `test_no_cross_subject_reads`: a landscape turn never appears in an assessment or project
    conversation, and the reverse.
  - `test_subcap_stops_landscape_but_not_assessment_chat`.
  - `test_reviews_doc_respects_independent_votes`.
  - `test_switch_off_404`.
  - `test_unrecorded_chat_md_edit_503`.
  - `test_verdict_doc_blinded_and_expert_hidden`: the Verdict document of an undecided
    blind-batch card; the Verdict, Panel, Disease profile and Evidence documents of an expert's
    unsubmitted sample item.
  - `test_migration_0078_round_trip`.
  - `test_postflight_expects_0078_objects`.
- [ ] **Step 2: Implement.**
- [ ] **Step 3: Commit** `feat(landscaping): chat on opportunity assessments (0078)`.

### Task 7.5: Workspace pages, navigation, presentation keys

**Files:**
- Create: `src/routers/workspace/landscape.py`, `templates/workspace/landscape.html`,
  `templates/workspace/landscape_detail.html`, `templates/workspace/landscape_pipeline.html`,
  `templates/landscape/_card.html`, `templates/landscape/_decision_form.html`,
  `templates/landscape/_review_form.html`, `templates/landscape/_expert_form.html`,
  `templates/landscape/_member_idea.html`
- Modify:
  - `src/routers/workspace/__init__.py` (add `landscape` to the feature tuple);
  - `src/web/navigation.py` (`NavigationItem("landscape", "Landscape", "workspace_landscape")`);
  - `src/web/presentation.py` (the new keys in `VIEW_FIELDS`, `STAFF_KEYS` and `ADMIN_KEYS`);
  - `tests/integration/test_workspace_permissions_matrix.py` (the GET inventory and the
    reviewer-200 rule gain the landscape pages);
  - `tests/integration/test_rendered_page_gate.py` (`PAGES` entries with seed data, and
    `SKIPPED["/landscape-chat/{opportunity_id}"] = "JSON for static/js/assessment_chat.js"`,
    as line 147 does for `/project-chat/{project_id}`).
- Test: `tests/integration/landscaping/test_landscape_pages.py`

**Interfaces.**
- **Consumes:** the view functions of Task 7.1, the POST routes of Tasks 7.2 and 7.3 (forms),
  the chat router of Task 7.4 (drawer), `schema_ready` (Task 1.2).
- **Routes**, all GET:
  - `workspace_landscape`: `/workspace/landscape`, query params `view`, `tab`, `assignment`;
    a marked expert also sees their open sample items (`expert_view`);
  - `workspace_landscape_pipeline`: `/workspace/landscape/pipeline`, registered **before** the
    detail route (D46);
  - `workspace_landscape_detail`: `/workspace/landscape/{opportunity_id:uuid}`; it carries
    `_expert_form.html` on an expert's own sample item.
- **Schema check** (spec §12.5): every landscape page answers 503 with a plain message while
  `schema_ready` is false, checked once per process.
- **Templates**, copied from the assessment and project templates (D26). They share only
  `assessments/_head.html`, the timeline script and the chat drawer. Every inline script
  carries the CSP nonce, and there are no inline `on…=` handlers. The pipeline page wraps each
  control form in a confirm that says what it does.

- [ ] **Step 1: Write the failing tests.**
  - `test_pipeline_route_not_shadowed_by_detail`.
  - `test_pi_gets_403`.
  - `test_reviewer_sees_batch_not_lists`.
  - `test_nav_item_for_reviewers_and_staff`.
  - `test_detail_renders_member_sections_and_badges`.
  - `test_expert_form_only_on_own_sample_item`.
  - `test_pages_503_without_0077`.
  - `test_rendered_gate_passes`: one `<main>`, one `<h1>`, heading order.
  - `test_no_post_on_workspace_router`.
- [ ] **Step 2: Implement.** The phase verification rebuilds `static/css/app.css` (Global
  Constraints).
- [ ] **Step 3: Commit** `feat(landscaping): workspace pages`.

### Task 7.6: Help content

**Files:**
- Modify: `src/web/help_pages.py` (the `HELP_PAGES` entry `landscape` with anchors `list`,
  `detail`, `reviewing`, `pipeline`, and one `TutorialStep` appended to each of the reviewer
  and manager `TUTORIALS`, with `()` for its shots until the capture commit, as
  `tests/unit/test_help_registry.py:63-69` and `tests/unit/test_help_shots.py:101-102`
  require)
- Create: `templates/help/pages/landscape.html`,
  `templates/help/tutorials/reviewer/09-landscape.html`,
  `templates/help/tutorials/manager/11-landscape.html`
- Modify: the glossary template (cut, opportunity, batch, blind batch, disease profile, role
  basis, text basis) and the troubleshooting template (the new refusals);
  `docs/operations/help-content.md` (a table row for the new templates);
  `tests/e2e/help_shots/seed.py` (seeded opportunities, a batch and a blind batch), so the
  operator's capture step has something to capture. The `Shot` entries and the `figure()`
  references come with the PNGs in the operator's capture commit (Phase 8), because
  `tests/unit/test_help_shots.py:97-100` requires every `Shot` to be referenced and `:54-59`
  every reference to have a PNG.
- Test: the existing `tests/integration/test_help_links_render.py`,
  `tests/integration/test_help_routes.py` and `tests/unit/test_help_shots.py` must pass, plus
  `tests/unit/landscaping/test_help_rules.py`

- [ ] **Step 1: Write the failing test** `test_help_rules`:
  - no digits other than 1-5 and 10,000 in the landscape help text;
  - staff sections inside `{% if permissions.staff %}`;
  - no file paths.
- [ ] **Step 2: Write the help.** Follow the content rules in
  `docs/operations/help-content.md:48-79`. Screenshots are an operator step on a workstation
  (Phase 8). Until they are captured, the pages reference no figures and `shots.py` has no
  landscape entry, so `test_help_shots` passes.
- [ ] **Step 3: Commit** `docs(help): landscaping pages`.

### Phase 7 verification

- [ ] Run `.venv-test/bin/python -m pytest tests/integration -q -k "landscape or reviews_router
  or workspace_permissions or rendered_page or help or chat or harness_smoke"` and
  `.venv-test/bin/python -m pytest tests/unit/test_migration_checks.py
  tests/unit/test_reachability.py -q`. Expected: all pass.
- [ ] Run `.venv-test/bin/python -m pytest tests/unit/test_no_dead_src_symbols.py -q` and remove
  every `ALLOWLIST` entry it reports stale. Expected: pass, with no landscaping entry left.
- [ ] Run `scripts/build_css.sh --check`; on drift, run `scripts/build_css.sh` and commit
  `static/css/app.css`.
- [ ] Run `./scripts/ci.sh`. Expected: exit 0.

## Phase 8 — Operations, docs, release

### Task 8.1: Runbook, CLAUDE.md, compose instructions

**Files:**
- Create: `docs/operations/landscaping.md`
- Modify:
  - `CLAUDE.md`, net-zero. It has 199 lines, and `tests/unit/test_claude_md_references.py:219`
    fails at 200:
    - the "Before you…" table gains `| run, deploy or change the landscaping pipeline |
      `docs/operations/landscaping.md` |`, which
      `test_claude_md_indexes_every_operations_page` requires (+1);
    - the two-line "throwaway Postgres" bullet (lines 31-32) becomes one line (−1);
    - the D5 bullet (lines 71-77) names the `landscape-worker` service and keeps its line
      count;
    - "(head is `0076` since the project chat)" (line 105) becomes "(head is `0078` since the
      landscaping chat)".
  - `docs/operations/host-and-simulation.md`: one line in its D5 box naming the service.

`docs/operations/landscaping.md` covers:
1. **The working-tree compose service** (not committed, the RCA plan's D5; spec D22). Its exact
   YAML block:
   - service `landscape-worker`, `build: .`, `profiles: [landscape]`;
   - `container_name: copi-blackbird-landscape-1`, `restart: unless-stopped`,
     `stop_grace_period: 330s`;
   - `command: ["python","-m","src.landscaping.worker"]`;
   - `env_file: .env`, plus the worker's `DATABASE_URL`/`SECRET_KEY`/`ENVIRONMENT` block;
   - volume `./prompts:/app/prompts:ro`, network `default` only;
   - json-file logging 10m × 5, and a memory limit.
2. **The deploy order:**
   1. commit; the porcelain check prints nothing;
   2. `$DC build blackbird-app worker` and `$DC --profile landscape build landscape-worker`;
   3. rehearse, then apply `./scripts/migrate/run_migration.sh` (0077 and 0078);
   4. `$DC up -d blackbird-app worker`;
   5. `$DC --profile landscape up -d landscape-worker`;
   6. never `--remove-orphans` or `up -d --build`; the agent is not recreated.
3. **Phase 0, step by step:**
   - the worker starts disabled (`LANDSCAPE_ENABLED` unset, so it only polls; spec §9.5);
   - the probes of `scripts/landscaping_probes.py`, run in a one-off container (Task 6.6);
   - a person reads JHTV's terms of use; only if they allow automated reading does an operator
     run `python scripts/landscaping_probes.py enable-source jhtv`;
   - staff enter the positive-control identifiers on the pipeline page (spec §10.2); the pilot
     report says so when the list is empty;
   - set `LANDSCAPE_ENABLED=true` in `.env` and recreate `landscape-worker` (`--force-recreate`):
     the pilot intake starts;
   - the pilot intake and the bake-off (D68). When no Haiku or Sonnet candidate passes (D43),
     the first epoch's S2-S4 regression fails the same way; an admin overrides it, with a
     reason, on the pipeline page (spec §10.4);
   - apply the bake-off's model picks in `.env`, then recreate (`--force-recreate`) the services
     that read it; the pilot run starts once the settings equal the picks, and ends in the
     pilot report on the pipeline page;
   - the owner approves the reason tags: an operator sets `[reason_tags] approved = true` in
     `prompts/landscaping/criteria.toml`, bumps its `[meta].version`, runs `python -m
     src.landscaping.prompt_set --stamp <new set version>`, commits, and restarts the
     landscaping worker. This creates no epoch (D61);
   - the admin presses Start backfill.
4. **Operations:** pause and resume, epoch overrides, rescue, reading the metrics, and the kill
   switch (`LANDSCAPE_ENABLED=false`, then recreate).

- [ ] **Step 1: Write the docs.** Every backticked path must exist
  (`test_claude_md_references.py`).
- [ ] **Step 2: Commit** `docs(landscaping): operations runbook`.

### Phase 8 verification and handoff

- [ ] Run `.venv-test/bin/python -m pytest tests/unit/test_claude_md_references.py
  tests/unit/test_compose_service_references.py -q`. Expected: pass, with `CLAUDE.md` at 199
  lines.
- [ ] Run `./scripts/ci.sh`. Expected: exit 0.
- [ ] `/lathe:plan-execution` runs its adversarial audit of the merged branch against this
  plan and the spec.
- **Operator steps.** These are not done by implementers, and they need the owner's go:
  - edit the working-tree compose file;
  - build and migrate per the runbook;
  - capture help screenshots on a workstation: add the landscape `Shot` entries to
    `tests/e2e/help_shots/shots.py`, run `python -m tests.e2e.help_shots.capture`, add the
    `figure()` references to the landscape help templates, and commit the PNGs, the manifest,
    `shots.py` and the templates together;
  - run Phase 0 (Task 6.6's probes, the pilot, the reason-tag approval);
  - **never start a simulation run** as part of this.
