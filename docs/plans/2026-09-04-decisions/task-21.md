# Task 21 — #22 (COR-16 group): the profile word-count gate vs. the synthesis prompt's word range

## Ruling

**Chosen: (a) state it, change nothing. No code changes.** The `100-350` gate stays.

The two numbers are not two answers to one question. `150-250` is what the prompt *asks the model
for*; `100-350` is what the validator *refuses to store as validated*. A generation instruction that
sits strictly inside its acceptance window is the normal arrangement — the tolerance exists so that a
model that lands at 147 or 260 words is not thrown away for a formatting miss.

Four reasons, in order of weight:

1. **The prompt set itself documents the wider gate, deliberately.**
   `prompts/profile-synthesis-sparse.md:39-42`:
   > `4. **research_summary length.** Aim for 150-250 words. If the evidence is too` /
   > `thin to write 150 words honestly, write fewer (down to ~80) rather than` /
   > `padding with generalities. The validator allows down to 100, and a short` /
   > `honest summary is better than a long invented one.`

   That is an explicit instruction to go **below 150** for a thin-evidence researcher, naming the
   100 floor as the reason it is safe. Option (b) would reject exactly the output the prompt asks
   for, for exactly the researchers least able to produce anything else. The divergence is a
   documented design, not a drift.

2. **The `Fix:` clause does not ask for it.** The clause for the group that contains the sentence is
   *"(user_id, pmid) unique constraint + in-run dedup; `itertext()`; null/type-safe validation."*
   The word-range sentence is one of that paragraph's *findings*; the remedy the issue names for it
   is the type-safety work, which is done. This branch's diagnosed failure mode is mitigations that
   outlived the condition justifying them; inventing a behaviour change from a descriptive clause is
   that failure mode.

3. **The branch already ruled on this once, in the direction of (a), and pinned it.** `2ee48fa`
   ("align log/progress text to the enforced 100-350 range (#22 COR-16)") resolved the *reported*
   half of the disagreement — the log message and the PI-facing "unvalidated" text both said
   `150-250` while the code enforced `100-350`, i.e. the system lied to the operator and to the PI.
   Those now interpolate the constants. Option (b) would reverse that shipped fix and require
   flipping five human-facing surfaces back (`profile_pipeline.py:493` progress text,
   `profile_pipeline.py:893-894` log, and the three templates pinned by
   `tests/unit/test_profile_summary_word_range_copy.py:20-23`), plus rewriting two tests that pin
   `100`/`350` by value (`tests/unit/test_validate_profile.py:83-84`).

4. **(b) trades one harm for another, and the harm is the one Task 19 just fixed.** `validated` is no
   longer advisory. A synthesis that fails the gate takes one of two paths
   (`src/services/profile_pipeline.py:461-471`, `:483-495`):
   - the PI already has a good stored profile → the new synthesis is **discarded** and an ERROR is
     logged. Under (b) a researcher whose honest evidence supports ~120 words would have every
     future refresh discarded, permanently frozen at whatever is stored;
   - first run → stored but marked `synthesis_validated = False`, which
     `_stored_is_worth_keeping` (`:848-862`) treats as *not worth protecting*, so the next
     ungrounded run may overwrite it — and the PI is shown "did not meet the quality checks".

   The gate also has five callers, not one: `run_profile_pipeline` plus `vet_publications.py`,
   `resynth_from_current_pubs.py`, `regen_profile_from_cv.py`, `regen_profiles_from_web.py`.

**D33 is a constraint here, not the argument.** Even with prompts editable, the ruling would be the
same; D33 only removes the third option (move the prompt to `100-350`) from this PR. That option is
also unnecessary — the sparse prompt already states the true floor.

## Evidence

**Both numbers verified at HEAD (`5090322`), read from the files, not inherited.**

| what | file:line | text |
|---|---|---|
| gate floor | `src/services/profile_pipeline.py:864` | `_MIN_SUMMARY_WORDS = 100` |
| gate ceiling | `src/services/profile_pipeline.py:865` | `_MAX_SUMMARY_WORDS = 350` |
| gate use | `src/services/profile_pipeline.py:890-891` | `word_count = len(research_summary.split())` … `if word_count < _MIN_SUMMARY_WORDS or word_count > _MAX_SUMMARY_WORDS:` |
| retry prompt | `src/services/profile_pipeline.py:386` | `context_text + "\n\nIMPORTANT: Ensure research_summary is 150-250 words."` |
| prompt | `prompts/profile-synthesis.md:12, 24, 85` | `"150-250 word narrative…"`, `- 150-250 words (count carefully)`, `The research_summary must be 150-250 words.` |
| prompt | `prompts/profile-synthesis-sparse.md:15, 39-42` | `150-250 word narrative…`; *"Aim for 150-250 words… write fewer (down to ~80)… The validator allows down to 100"* |

**Plan correction.** The plan gives the retry prompt as `profile_pipeline.py:374`. At HEAD it is
**:386**; `:374` is the first `_validate_profile(synthesized)` call. The string is byte-identical to
the one the plan quotes — only the line number moved.

**The affected population, measured on the disposable production copy** (`copi-prodtest-db`,
`127.0.0.1:55434`, db `copi_verify`, at alembic `0028`, read-only, 2026-09-04). 141 stored profiles,
all with a non-empty `research_summary`, generated 2026-03-22 → 2026-07-29:

```bash
docker exec -e PGPASSWORD=copi copi-prodtest-db psql -U copi -d copi_verify -c "
with w as (
  select id, array_length(regexp_split_to_array(btrim(research_summary), '\s+'), 1) as n
  from researcher_profiles where coalesce(btrim(research_summary),'') <> ''
)
select case when n < 100 then '<100 (fails today)'
            when n between 100 and 149 then '100-149 (would fail under (b))'
            when n between 150 and 250 then '150-250 (passes either way)'
            when n between 251 and 350 then '251-350 (would fail under (b))'
            else '>350 (fails today)' end as bucket, count(*) from w group by 1 order by 1;"
```

| bucket | profiles |
|---|---|
| `<100` — already fails today's gate | **2** (24 and 77 words) |
| `100-149` — passes today, **would fail under (b)** | **2** (140 and 146 words) |
| `150-250` — passes either way | **137** |
| `251-350` — passes today, would fail under (b) | **0** |
| `>350` — already fails today's gate | **0** |
| **total** | **141** |

**So (b)'s affected population is 2 of 141 (1.4 %), both at the low end; the upper half of the
divergence (251-350) is empty.** Min 24, max 245, median 195 words — no stored profile has ever
exceeded 250 words, so `_MAX_SUMMARY_WORDS = 350` has never once been the binding constraint in
production, while the floor has: two profiles already sit *below* 100. That distribution is the
shape the sparse prompt predicts — the tail is thin-evidence researchers, and moving the floor to
150 walks up that tail rather than away from it. Cross-checked with Python `str.split()` semantics
(the gate's own tokenizer) over a CSV export of the same column: identical counts.

`synthesis_validated` is NULL on all 141 rows in this snapshot (the column exists — migration
0023 — but predates these writes), so the measurement is of the stored text itself, which is what
option (b) would re-judge on the next refresh.

## Consequence a closing comment must state

- **#22 (COR-16 paragraph).** The reported half of the word-range disagreement is **fixed**: the
  log line and the PI-facing "unvalidated" progress text used to say `150-250` while the code
  enforced `100-350`, and both now interpolate `_MIN_SUMMARY_WORDS`/`_MAX_SUMMARY_WORDS`
  (`2ee48fa`); the three PI-facing templates were corrected to match in `1c6ba09`.
- The remaining gap — prompt asks `150-250`, validator allows `100-350` — is **deliberate and
  stays**. The prompt is a request; the gate is a limit; `prompts/profile-synthesis-sparse.md:39-42`
  already instructs the model to write fewer than 150 words when the evidence is thin and names the
  100 floor as why that is safe. Decision D33 (no prompt changes in this PR) is not the reason —
  it only removed the option of restating the range in the prompt.
- Do **not** report this as an open defect, and do not "align" the two ranges later without the
  measurement: on the production copy **2 of 141 stored profiles (1.4 %) sit in `100-149` and would
  begin failing** if the gate were tightened to `150-250`; **0** sit in `251-350`; the longest
  summary in production is 245 words. A failing gate is not cosmetic — it either discards the
  refresh outright or stores the profile as `synthesis_validated = False`, which strips its
  overwrite protection.
- Nothing to deploy, no migration, no code change from this task.
