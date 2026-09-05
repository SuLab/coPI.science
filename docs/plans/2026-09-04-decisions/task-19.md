# Task 19 — #22 V6 / #27 I1: where the "absent vs empty" fallback belongs, and the honest publication-insert annotation

## Ruling

**Chosen: the fallback lives in `apply_synthesis` (`src/services/profile_pipeline.py`), not in the
scripts.** One canonical implementation, shared by every caller.

`apply_synthesis` is already the layer that owns "absent vs empty": it is the only place that both
reads the model response's keys and holds the stored `ResearcherProfile` row, and it is where the
existing default (`synthesized.get("research_summary", "")`, `_as_list(synthesized.get(...))` —
i.e. *absent means blank*) was written. It has five callers: `run_profile_pipeline` plus four
`scripts/` synthesizers (`vet_publications.py`, `resynth_from_current_pubs.py`,
`regen_profile_from_cv.py`, `regen_profiles_from_web.py`). Putting the fallback in the scripts would
be four copies of one rule, two of them in files this task does not own — and would leave the
pipeline itself, which writes far more profiles than any repair script, still blanking.

Options considered and rejected:

- **In each script, before the `apply_synthesis` call** (merge the response into the stored values
  and hand the merged dict over). Rejected: four copies, two unreachable from this task, and each
  copy would have to re-derive the column list from the ORM model. It also cannot fix the pipeline.
- **In `_validate_profile`.** Rejected, and for the reason `beae171` already recorded: that function's
  job is "should this synthesis be trusted", not "what shape are its fields", and it does not run on
  every path into `apply_synthesis`.

**The rule, precisely.** Three cases per field, not two:

| response | action |
|---|---|
| key absent | keep the stored value (an omission is not an instruction) |
| key present, right type — including `[]` / `""` | apply it (an explicit empty **is** an instruction) |
| key present, wrong type | reject it; keep the stored value; log it |

`beae171` fixed a real defect (a bare `"cancer"` for `disease_areas` stored as
`['c','a','n','c','e','r']`; a non-iterable raising `StatementError` at flush) by coercing every
non-list to `[]`. That kept the per-character corruption out of the column and put blanking in its
place. Rejecting the value without storing anything satisfies both.

**Two shapes are fixed, and the artefact's framing of the first is corrected.** A *malformed*
response is already rejected and writes nothing — that behaviour predates this task and is
untouched. What was live: (1) a response that **passes** `_validate_profile` (which checks only
`research_summary`, `techniques`, `disease_areas`) while omitting or mistyping `keywords` /
`key_targets` / `experimental_models`, writing `[]` over curated values (see `## Evidence` for the
re-measurement on the production copy); (2) the unfiled
shape, a profile with `synthesis_validated = False`, which `_stored_is_worth_keeping` deliberately
declines to protect, so an incomplete synthesis was applied to it in full and it lost everything.
The gate decides *whether* a synthesis may be applied; it never decided *what the synthesis
contains*, which is why the per-field rule has to hold on the unprotected path too.

**A response carrying none of the six fields returns `False`** (nothing applied) rather than
stamping `synthesis_validated` and `profile_generated_at` for a synthesis that never landed.

## Evidence

**mypy, Step 5.** Measured with the plan's own command, on a clean export (never the working tree,
which holds other tasks' WIP):

```bash
git archive HEAD src pyproject.toml | tar -x -C <scratch>/clean
cd <scratch>/clean && .venv-test/bin/python -m mypy src --ignore-missing-imports | grep -c ': error:'
```

| tree | findings |
|---|---|
| HEAD (`391e545`) | **147** |
| HEAD + this task's `profile_pipeline.py` | **145** |

The two that go are exactly the ones Task 27 attributes the 145→147 move to:

```
src/services/profile_pipeline.py:811: error: Incompatible return value type (got "tuple[Publication | None, bool]", expected "tuple[Publication, bool]")  [return-value]
src/services/profile_pipeline.py:823: error: Incompatible return value type (got "tuple[Publication | None, bool]", expected "tuple[Publication, bool]")  [return-value]
```

`_insert_publication_tolerating_conflict` now returns `tuple[Publication | None, bool]`, which is
the truth: `Session.get` and `.first()` are each `Publication | None`, and the row can be gone if the
conflicting row was deleted inside the same concurrent-writer window the function exists for. The
change exposed one caller (`run_profile_pipeline`'s insert branch), which assumed non-`None` and
would have stored `None` in `existing_pubs[pmid]`, raising `AttributeError` on the next repeat of
that PMID. It now handles the `None` — it is **not** silenced with a `type: ignore`. The two
remaining findings in this file (`:329` `[assignment]`, `:649` `[arg-type]`) are pre-existing and
untouched.

`ruff check src` on the same clean export: **251** at HEAD, **251** with this change (the two `F841`s
in this file are pre-existing). Working-tree `ruff check src` reads higher because
`src/services/http_retry.py` holds another task's uncommitted WIP.

**The shape, re-measured on the disposable production copy** (`copi-prodtest-db`, db `copi_verify`,
read-only, 2026-09-04):

```sql
select count(*) from researcher_profiles;                     -- 141
select ... from researcher_profiles
 where keywords='{}' or key_targets='{}' or experimental_models='{}';
```

**8 of 141** stored profiles hold `key_targets = '{}'` while every sibling list column is populated
(3-12 techniques, 1-18 keywords, 1-8 experimental_models) and `research_summary` is non-empty —
the shape an omitted key leaves, not the shape of a synthesis that genuinely had no targets. One of
them is at `profile_version = 20`: it survived twenty pipeline runs with a rich profile and an empty
`key_targets`. Two of the eight also hold an empty `keywords` or a NULL `experimental_models`. The DB
alone cannot prove a given `'{}'` came from an omitted key rather than an explicit `[]` — which is
exactly why the fix distinguishes the two at the point where the difference is still visible.

**Red-first (plan Steps 1-2).** `tests/unit/test_apply_synthesis.py`, run against unmodified HEAD
source: **24 failed, 15 passed**. Representative messages:

- omitted key, curated value: `AssertionError: assert [] == ['curated-a', 'curated-b']`
- `synthesis_validated=False` profile: `AssertionError: assert [] == ['ferroptosis', 'autophagy']`
- response with only misspelled keys: `assert True is False` (it was applied, and blanked six columns)
- non-string `research_summary`: `AssertionError: assert {'text': 'nested'} == 'OLD'`

After the fix: **39 passed**. The four suites the task names —
`test_apply_synthesis.py`, `test_validate_profile.py`, `test_profile_pipeline_gm.py`,
`test_private_profile_clear.py` — **80 passed, 11 snapshots passed** (no snapshot re-recorded).

**D33.** AST walk of every string constant in the three source files, before the first edit and
after the last: **14 additions; 6 removed-or-modified**, and none of them model-facing. The 6 are:
the deleted `_as_list` docstring; two docstrings extended (`apply_synthesis`,
`_insert_publication_tolerating_conflict`); two operator log lines reworded (one per script); and
the `""` default argument in `synthesized.get("research_summary", "")`, which was the defect itself.
Every prompt-bearing constant — `VET_SYSTEM_PROMPT` and `_build_vet_prompt`'s pieces in
`scripts/vet_publications.py`, `_build_synthesis_context`'s pieces, and the
`"\n\nIMPORTANT: Ensure research_summary is 150-250 words."` retry suffix — is byte-identical.

## Consequence a closing comment must state

- **#22.** The `Form("")`-shaped defect that COR-22's `Fix:` clause names ("don't overwrite fields
  absent from a POST") has an LLM-response twin, and it is now closed on the response side: an
  omitted or mistyped `keywords` / `key_targets` / `experimental_models` no longer blanks a curated
  column, on either the protected or the `synthesis_validated = False` path. The **three web save
  routes** COR-22 names (`agent_page.py`, `profile.py`, `onboarding.py`) are a different fix and are
  **not** in this task's scope — do not report COR-22 as closed on the strength of this.
- **#27 I1.** The mypy ceiling's two `[return-value]` findings are gone; `mypy src
  --ignore-missing-imports` measures **145** on a clean export. Task 27 records the provenance in
  `scripts/ci.sh` (this task must not touch that file), and the slack against `MYPY_MAX=150` is 5
  again, as `ci.sh`'s comment already claims.
