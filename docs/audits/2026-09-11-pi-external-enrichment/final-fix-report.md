# PI-external-enrichment: final review fix wave

Worktree: `/home/ubuntu/wt-pee-fix` (branch `fix/pee-final-wave`).
One commit: **0fb613c** (code + docs together), parent `5183bf5`.

## What changed, per item

### 1. `grant_titles` supplements the ORCID-fundings seed

- `src/services/grant_resolution.py`: new `merge_grant_titles(derived, existing,
  reporter_titles)` — `derived` (deduped, order-preserving) followed by each
  `existing` title not in `reporter_titles`. `reporter_titles` is "everything this
  feature is authoritative over", i.e. the title of EVERY `PiGrant` row for the
  user, vetoed rows included — so a vetoed/no-longer-derived RePORTER title is
  dropped while a non-RePORTER title (CFF, HHMI, DoD) survives.
- `src/services/grant_enrichment.py`: replaced
  `profile.grant_titles = derive_grant_titles(kept) or profile.grant_titles`
  with `merge_grant_titles(derive_grant_titles(kept), profile.grant_titles,
  {r.title for r in records} | {t for _, t in vetoed_rows})`. The vetoed-row
  query now selects `(core_project_num, title)` instead of just the core number.
- `src/routers/manager.py` (`manager_veto_grant`): the
  `if new_titles: … else: filter-out-the-vetoed-title` branch is gone. It now
  loads ALL `PiGrant` rows once, derives from the un-vetoed subset, and merges
  against `{g.title for g in rows}`. The old fallback only ran when NO eligible
  grant remained, so a veto with a surviving eligible grant assigned
  `derive_grant_titles(remaining)` outright and lost ORCID-only titles.

### 2. COI company-suffix regex word boundary

`src/services/industry_sources/pubmed_coi.py`: `(?!\w)` appended after the suffix
alternation inside the capture group. A lookahead rather than `\b` deliberately:
`\b` after `\.?` would require a word character next and would break
`"Paratek Pharmaceuticals, Inc."`.

### 3. `rescore_user` zero-raw guard

`src/services/industry_evidence.py`: `if not live:` → `if not live or raw <= 0:`.
Also `evidence_count=len(live)` in that branch (was hardcoded `0`), so a PI with
live-but-unscoreable evidence is not misreported as having none collected.

### 4. CLAUDE.md field-percentile claim

The sentence lived in the **"Deploy order for 0047" box** (the only occurrence of
`cohort_too_small` in the file — not in the "Adding New PIs" paragraph as the task
statement guessed). Replaced with: the ≥3-peer rule is a **global** cohort of other
PIs with real live evidence at the current `scorer_version`, it gates the SCORE
(`reason: cohort_too_small`, with `raw_sum`/`components` still recorded), and
`field_percentile` is **not computed in this release** (column stays NULL;
`primary_field` recorded for a future field-normalised percentile).

### 5. CLAUDE.md 0047 blast radius

Added a ⚠️ paragraph: `run_profile_pipeline` calls `enqueue_enrichment_jobs`
inside its single transaction (`src/services/profile_pipeline.py:548-549`, before
the worker's commit), so against a pre-`0047` `job_type_enum` the `jobs` INSERT
raises `InvalidTextRepresentation` and the WHOLE `generate_profile` commit rolls
back — corpus, synthesised profile and revision lost for every PI whose profile job
runs in the gap, retrying into the same failure until dead. Notes that the
prescribed order prevents it and that this is what `up -d --build` does instead.

### 6. OpenAlex N+1, pacing, retry

`src/services/industry_sources/openalex_industry.py`:
- module-level `_pace()` (interval `_PACE_INTERVAL = 0.2`, same shape as
  `src/services/nih_reporter.py`) and `_get(client, url, params)` — three attempts,
  `_BACKOFF_BASE * (attempt + 1)` sleep on 429 or 5xx, `raise_for_status` only on
  the final attempt. Used by all three OpenAlex calls (`/works`, `/funders`,
  `/institutions`).
- `company_funder_ids` no longer issues one `/institutions` GET per funder: it
  builds `funders_by_inst` across the funders page(s), then issues batched
  `ids.openalex:a|b|…` institution calls of ≤50 ids and maps company hits back to
  their funder ids.

### 7. Grant veto: idempotency + impersonation WARNING

`src/routers/manager.py`: returns the redirect unchanged when
`grant.vetoed_at is not None` (no re-derivation, no persona re-export); when
`getattr(current_user, "_is_impersonated", False)` logs one WARNING naming the
grant id, the effective user and `request.session.get("user_id")` — mirroring
`manager_veto_industry_evidence`.

## Tests added/changed

- `tests/unit/test_grant_resolution.py` (+4): the spec's case (a)
  `["New R01 title", "CFF award"]`, a drop-what-we-own case, dedupe/order, `None`
  existing.
- `tests/unit/test_grant_enrichment_job.py` (+1): spec case (b) — seed
  `["CFF award"]` + one eligible R01 → `["T R01QQ000001", "CFF award"]`.
  **Expectation changed** in
  `test_job_writes_only_pmid_linked_in_tenure_grants_and_projects_titles`:
  `["T R21AI190702", "T R01AI137329"]` →
  `["T R21AI190702", "T R01AI137329", "old orcid title"]`. That is the new rule
  working: the seeded `"old orcid title"` is not the title of any `PiGrant` row, so
  it is no longer replaced by a non-empty derivation. No other expectation moved
  (`test_ineligible_activity_code_does_not_wipe_orcid_seed` already passes: derived
  is empty and `"orcid title"` is not a reporter title).
- `tests/unit/test_industry_sources_parsers.py` (+2): parametrised regression over
  the five false-positive sentences, framed as positive COI statements, asserting
  neither the exact false string nor any `Co`/`Corp`/`SA`-terminated name comes
  back; plus `"…Paratek Pharmaceuticals, Inc. All authors vouch…"` still yields
  `Paratek Pharmaceuticals, Inc`/`Paratek Pharmaceuticals`.
- `tests/unit/test_industry_evidence_job.py` (+1): one un-vetoed `cro_vendor`
  co-author with 3 scoreable peers seeded → `score is None`,
  `reason == "no_evidence"`, `raw_sum == 0.0` (previously percentile 25.0/"ok").
- `tests/integration/test_manager_grants_panel.py` (+3): spec case (c)
  `["Good grant", "ORCID-only title"]`; replayed POST leaves `vetoed_at`
  byte-identical; impersonated POST logs a WARNING containing the admin id and the
  grant id.
- `tests/contract/test_openalex_industry_contract.py` (new, `pytest.mark.contract`,
  respx): two funders → 3 institution ids → exactly ONE `/institutions` request
  carrying all three, `hits == {"F1","F2"}`; a `/works` 429-then-200 returns the
  result with `call_count == 2`. An autouse fixture zeroes `_PACE_INTERVAL` and
  `_BACKOFF_BASE` so the retry test does not sleep.

## Verification (run on the host, from the worktree)

```
$ cd /home/ubuntu/wt-pee-fix && .../.venv-test/bin/python -m pytest \
    tests/unit/test_grant_resolution.py tests/unit/test_grant_enrichment_job.py \
    tests/unit/test_industry_sources_parsers.py tests/unit/test_industry_evidence_job.py \
    tests/unit/test_industry_score.py tests/integration/test_manager_grants_panel.py \
    tests/integration/test_manager_industry_panel.py tests/integration/test_manager_views.py \
    tests/unit/test_claude_md_disclosure_sync.py tests/unit/test_doc_prompt_sync.py \
    tests/contract/test_openalex_industry_contract.py -q
136 passed, 2 skipped, 1 warning in 24.97s
```

`tests/unit/test_enrichment_isolation.py` (the prompt/profile-import tripwire) was
also run and passes.

```
$ .../.venv-test/bin/ruff check <13 changed files>
All checks passed!

$ .../.venv-test/bin/ruff check src/ --statistics | tail -1
Found 214 errors.        # ceiling SRC_LINT_MAX=231, unchanged by this wave
```

## Residual risk / notes

- `_pace()` keeps a module-global `_next_slot` keyed off `loop.time()`, exactly as
  `nih_reporter._pace` does; both are monotonic-clock based, so a new event loop
  per test does not produce a spurious long sleep (observed: contract suite runs in
  <1 s).
- Retry covers 429 and any 5xx. A 4xx other than 429 still raises immediately —
  deliberate, it is a bad query, not congestion.
- `merge_grant_titles` matches on exact title strings, which is what both call
  sites have; a RePORTER title that changes wording between runs would linger as a
  non-reporter title until the next veto/derivation names the new string. Not new
  behaviour, but now the only way a stale title can persist.
- `scripts/ci.sh` (full suite, migrations round trip, coverage floor) was NOT run —
  only the named files plus the isolation tripwire.

## Follow-up: residual MEDIUM from the re-review of 0fb613c

`src/services/grant_enrichment.py` built `reporter_titles` from this run's
`records` plus the VETOED rows' titles — and the non-vetoed `PiGrant` rows are
already deleted by that point, so a grant that stops resolving (re-resolution, a
tightened tenure start) left no trace: `merge_grant_titles` then read its stale
title in `profile.grant_titles` as a non-RePORTER seed entry and kept it forever.

Fix (commit **1000f89**): capture `pre_titles = {t for (t,) in … select(PiGrant.title)
.where(PiGrant.user_id == user_id)}` BEFORE the delete, and union it into
`reporter_titles` (`{r.title for r in records} | pre_titles`). `pre_titles` spans
vetoed and non-vetoed rows alike, so it subsumes the old vetoed-title union — the
vetoed query reverted to selecting `core_project_num` only, as it was before this
wave.

Test added — `tests/unit/test_grant_enrichment_job.py::
test_a_grant_that_drops_out_of_a_later_run_loses_its_title`: profile seeded
`["T R01OLD", "CFF award"]` with a non-vetoed `PiGrant(core_project_num="R01OLD",
title="T R01OLD")`, RePORTER returns only `R01NEW` → `["T R01NEW", "CFF award"]`
(fails before the fix with a trailing `"T R01OLD"`).

```
$ cd /home/ubuntu/wt-pee-fix && .../.venv-test/bin/python -m pytest \
    tests/unit/test_grant_enrichment_job.py tests/integration/test_manager_grants_panel.py -q
16 passed, 1 warning in 15.22s

$ .../.venv-test/bin/ruff check src/services/grant_enrichment.py tests/unit/test_grant_enrichment_job.py
All checks passed!
```
