# Task 20 — the truncated publication text the agents are still reading (#22 item 15)

Plan: `docs/plans/2026-09-04-close-remaining-gaps.md` § Task 20.
Issue clause (`docs/plans/2026-09-02-close-issues-20-27-evidence/issues/issue_22.md:19-20`,
COR-16): *"`(user_id, pmid)` unique constraint + in-run dedup; `itertext()`; null/type-safe
validation."* All three shipped. This task is about the rows the parser fix does **not**
reach backwards.

## Ruling

**Both.** The operator procedure below is the deliverable; a targeted repair script
(`scripts/repair_publication_text.py`) is written as well, because the measurement shows a
population of corrupted rows the per-user pipeline re-run **structurally cannot reach**.

Options considered:

- **(a) Per-user pipeline re-run only.** `35cc010` refreshes `title`/`abstract`/`journal`/`year`
  on the pipeline's existing-row branch, so re-running the pipeline for a PI repairs their rows.
  **Rejected as sufficient on its own.** The pipeline only ever sees a PMID that is on the PI's
  *current ORCID works list*. Measured against `pub.orcid.org` for all 92 affected PIs (below):
  **84 of the 512 corrupted rows (16.4 %) carry neither a PMID nor a DOI that appears anywhere in
  their owner's ORCID record** — 67 of them belong to **twelve PIs whose ORCID works list is
  empty outright**, which is exactly why `scripts/backfill_publications.py` had to insert their
  publications by hand in the first place (that script's own docstring: *"their ORCID profiles
  list no works"*). A re-run for those PIs fetches zero works, builds zero PMIDs, and never
  enters the update branch. Their rows stay corrupt forever. A further 274 rows are reachable
  only if the pipeline's best-effort `convert_dois_to_pmids` resolves them, so the re-run's real
  reach is somewhere between 154 and 428 of 512.
- **(b) Repair script only.** Repairs all 896 genuinely-wrong rows for the price of 44 `efetch`
  calls and no LLM spend. **Rejected as sufficient on its own**, but only for one reason: it
  repairs the *evidence*, not the *synthesized prose already written from the corrupted evidence*.
  A PI whose `research_summary` was synthesized against `"Role of "` and an empty abstract keeps
  that summary until the pipeline re-runs.
- **(c) A migration.** Rejected outright, and the plan forbids it. The repair needs a live PubMed
  round trip; a migration cannot make one, and this is not a schema change.

So: **(b) then (a)** — repair the corpus first (cheap, no LLM, no profile overwrite), then re-run
the pipeline only for the PIs whose synthesized profile should be rebuilt. The procedure below is
written so the two halves can be applied independently, because the second half has a real blast
radius (101 full pipeline runs, each with its own LLM synthesis and its own `ResearcherProfile`
overwrite) and the first has none.

### Why the script's default is `--dry-run`

It writes to the four columns `profile_export.py` and the synthesis context read. The dry run
prints the exact per-row diff plan and touches nothing; `--apply` is a separate, deliberate
invocation. Same shape as `scripts/backfill_publications.py`, which operators already know.

### Distinct from Task 19

Task 19 (`79cee44`) fixed a *synthesis-blanking* defect in the same area and measured 8 of 141
profiles holding `key_targets = '{}'` with every sibling list populated. That is a defect in what
synthesis **writes**. This task is a defect in what synthesis **reads**. They share no rows, no
column and no fix; do not let a closing comment merge them.

## Evidence

### The measured inventory

Measured 2026-09-04 on the disposable production copy — `copi-prodtest-db`,
`127.0.0.1:55434`, database `copi_verify`, at alembic head `0028`, **never production** — in a
`SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY` session:

```bash
docker exec -i copi-prodtest-db psql -U copi -d copi_verify <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT
  count(*)                                                               AS total_rows,
  count(*) FILTER (WHERE char_length(title) < 2)                         AS title_raw_lt2,
  count(*) FILTER (WHERE btrim(coalesce(title,'')) = '')                 AS title_empty_ws,
  count(*) FILTER (WHERE title ~ '[[:space:]]$')                         AS title_trailing_ws,
  count(*) FILTER (WHERE title ~ '[[:space:]]$'
                      OR char_length(btrim(title)) < 2)                  AS title_corrupt_any,
  count(*) FILTER (WHERE coalesce(abstract,'') = '')                     AS abstract_empty,
  count(*) FILTER (WHERE btrim(coalesce(abstract,'')) <> ''
                     AND char_length(btrim(abstract)) < 80)              AS abstract_lt80_nonempty,
  count(*) FILTER (WHERE char_length(btrim(coalesce(abstract,''))) < 80) AS abstract_lt80_total
FROM publications;
SQL
```

| Measure | Definition used | Count | phase8's figure |
|---|---|---:|---|
| Total rows | `count(*)` | **4,508** | 4,508 ✓ (the plan's 4,731 is pre-0025) |
| Titles under 2 chars | `char_length(title) < 2` | **12** | 12 ✓ |
| Empty / whitespace titles | `btrim(coalesce(title,'')) = ''` | **9** | 9 ✓ — but note these 9 are a **subset** of the 12, not additional |
| Truncation-signature titles | `title ~ '[[:space:]]$'` | **120** | 111 ✗ — phase8's predicate is not recorded and does not reproduce |
| Corrupt titles, union | trailing whitespace **or** trimmed length < 2 | **132** | (the plan's "~132 titles") |
| Empty abstracts | `coalesce(abstract,'') = ''` | **299** | 299 ✓ (10 NULL + 289 `''`; a 300th is whitespace-only) |
| Abstracts under 80 chars, non-empty | `btrim(abstract) <> '' AND char_length(btrim(abstract)) < 80` | **126** | 127 ✗ (off by one) |
| Abstracts under 80 chars, total | `char_length(btrim(coalesce(abstract,''))) < 80` | **426** | (the plan's "~426 abstracts") |
| Rows matching either arm | the selection predicate below | **512** | not measured before |
| PIs owning at least one | `count(DISTINCT user_id)` | **93** (92 real ORCIDs + 1 synthetic `SPARSE-…` user) | not measured before |
| Rows genuinely wrong (live re-fetch, whole table) | fresh parser output differs | **896** | not measured before |

Five of the plan's inherited numbers reproduce; two do not (the truncation signature, and the
under-80 abstract count). The "12 titles under 2 characters" and "9 empty/whitespace" figures are
**overlapping**, not additive — the plan reads as if they were.

Character of the damage, so nobody re-classifies it as cosmetic. Shortest stored titles:
`''` ×9, `'T'` (pmid 28613821), `'T'` (29625991), `'H'` (38478695), `'H '` (37904965),
`'NAD'` (31754102), `'RNA '` (33801802), `'tRNA'` (28837402), `'From '` (41800026),
`'Novel '` (35494649), `'Dual Bcl-X'` (34216984), `'Synthesis of '` (37457378). Shortest
non-empty abstracts: `'Na'`, `'sp'`, `'RNA '`, `'The '`, `'NADP'`, `'A [Rh'`, `'An NAD'`,
`'A Ru(bpy)'`. Every one is the pre-`itertext()` parser stopping at the first inline markup tag.

### How much of it a pipeline re-run actually reaches

For each of the 92 affected PIs with a real ORCID, `https://pub.orcid.org/v3.0/<orcid>/works`
was fetched once (public read-only API, the same endpoint `fetch_orcid_works` calls) and every
corrupted row's PMID and DOI checked against that PI's works list — the exact test the pipeline
applies. Script: `<scratch>/task20/orcid_probe.py`.

| Reachability of the 512 corrupted rows | Rows |
|---|---:|
| PMID listed directly on ORCID → the re-run repairs it | **154** |
| Not listed by PMID, but its DOI is → repaired *if* `convert_dois_to_pmids` resolves it | **274** |
| Neither PMID nor DOI anywhere on ORCID → **the re-run never sees it** | **83** |
| Owner has no ORCID identifier at all (`SPARSE-BCF73CF6`) | **1** |

Twelve PIs return **zero** works from ORCID and account for 67 of the 83:
`0000-0003-2932-4941` (13 rows), `0000-0002-9138-8973` (12), `0000-0003-0339-0959` (11),
`0000-0001-8562-5736` (10), `0000-0002-9943-7557` (6), `0000-0002-5676-4718` (4),
`0000-0003-2612-5445` (4), `0000-0002-3354-1263` (2), `0000-0002-7031-5433` (2),
`0000-0001-6252-3390` (1), `0000-0002-7813-0302` (1), `0000-0003-1219-4757` (1).
Spot-checked directly: `curl -s https://pub.orcid.org/v3.0/<orcid>/works` returns
`"group": []` for each of the first four. Their publication rows were created 2026-05-01 and
2026-06-06 by `scripts/backfill_publications.py`, whose docstring states the reason.

### What the repair would actually change

`scripts/repair_publication_text.py`, dry run, against the copy (read-only, no `--apply`,
no commit):

```bash
DATABASE_URL="postgresql+asyncpg://copi:copi@127.0.0.1:55434/copi_verify" \
  .venv-test/bin/python scripts/repair_publication_text.py
```

```
512 row(s) matched the selection predicate; 264 already correct, 0 not found on PubMed.
Dry run: 248 row(s) would be repaired.
```

248 rows change: **233 abstracts, 166 titles, 8 journals**. The other 264 selected rows are
records PubMed genuinely publishes without an abstract (commentaries, errata, journal-club
pieces, pre-1990 papers) — they report `no-change` and will do so on every future run, which is
what makes the script re-runnable. Note 166 > 132: rows selected on the *abstract* arm turn out
to have a truncated title too, so the title signature under-counts the title damage.

### The predicate is a signature, not an oracle — the damage is 3.6× larger

Every one of the 4,508 rows was re-fetched read-only and compared field by field against the
fixed parser's output (`<scratch>/task20/full_scan2.py` + `classify.py`; the fetched records are
cached so the classification is reproducible without re-hitting NCBI):

| Full-corpus sweep of all 4,508 rows | Rows |
|---|---:|
| Rows differing from the fixed parser in any of the four columns | **896** |
| — title differs | **209** |
|   · stored value is a strict prefix of the fresh one (truncation) | 198 |
|   · stored value was empty | 9 |
|   · genuine PubMed metadata revision, not truncation | **2** |
| — abstract differs | **877** |
|   · stored value is a strict prefix of the fresh one (truncation) | 811 |
|   · stored value was empty | 34 |
|   · other drift (label re-formatting, revised text) | 32 |
| — journal differs | 8 |
| — year differs | 0 |
| Rows whose PMID PubMed no longer resolves | 0 |

**~97 % of the difference is unambiguous truncation**, and the targeted predicate reaches only
248 of the 896. It misses a title cut at markup that is neither empty nor followed by a space —
`'Inhibition of STEP'` for *Inhibition of STEP61 ameliorates deficits…*, `'Metabolic control of T'`
for *…of TH17…*, `'When Cancer Cells Are Given Lemo[NH'` — and every abstract truncated past 80
characters. That is why the script takes `--all`, and why the runbook step below uses it. The
cost of `--all` is 44 `efetch` batches instead of 6; it is not a different cost class.

## The procedure Task 33 must put in Part R

Two steps. **Step A has no blast radius and should be unconditional. Step B is the optional
half**, and must not run before Task 19's synthesis fix is deployed.

### Step A — repair the stored corpus (required, no LLM spend)

Run after `alembic upgrade head` and after the app-code deploy (the script needs the fixed
`src/services/pubmed.py` parser, which is what it re-parses through). Dry run first:

```bash
docker compose exec -T app python scripts/repair_publication_text.py --all
docker compose exec -T app python scripts/repair_publication_text.py --all --apply
```

Safe to re-run; the dry run is the default and `--apply` is the only writing mode. It re-fetches
one `efetch` slot per distinct PMID (44 batches of 100 for the whole table; 6 without `--all`)
and never blanks or shortens a value already on a row. The shipped script, run `--all` against
the copy, reproduces the independent full-corpus scan exactly:

```
4508 row(s) considered; 3612 already correct, 0 not found on PubMed.
Dry run: 896 row(s) would be repaired.
103 PI(s) own a repaired row.
```

Expect **896 rows repaired and 103 PIs listed** on a production-shaped database — 248 rows and 92
PIs without `--all`, which is why the step uses it. Two of the 103 are the synthetic
`SPARSE-…` users; they have no ORCID and Step B does not apply to them.

**Selection query, verbatim** — this is the predicate the script uses
(`SELECTION_PREDICATE_SQL` in `scripts/repair_publication_text.py`), reproduced here so an
operator can inventory before and verify after:

```sql
SELECT p.pmid, u.orcid, u.name, char_length(btrim(p.title)) AS title_len,
       char_length(btrim(coalesce(p.abstract, ''))) AS abstract_len
FROM publications p
JOIN users u ON u.id = p.user_id
WHERE p.title ~ '[[:space:]]$'
   OR char_length(btrim(p.title)) < 2
   OR char_length(btrim(coalesce(p.abstract, ''))) < 80
ORDER BY u.orcid, p.pmid;
```

Expect 512 rows before and ~264 after on a production-shaped database: the count does **not** drop
to zero, because the genuinely abstract-less records keep matching. The check that the repair
worked is that the *title* arm empties:

```sql
SELECT count(*) FROM publications
WHERE title ~ '[[:space:]]$' OR char_length(btrim(title)) < 2;   -- 132 before, expect 0 after
```

A second `--all` dry run is the stronger check: it should report 0 rows to repair.

### Step B — per-user pipeline re-run (optional; rebuilds the synthesized prose)

Only Step B rewrites a `ResearcherProfile` whose text was synthesized from the corrupted
evidence. **Criterion: a PI needs it iff Step A's run reported at least one `repair` for a row
they own.** Do not re-derive that list from the corruption predicate afterwards — the repair has
already made those rows stop matching it, and with `--all` the predicate under-selects by 3.6× in
the first place. Step A prints the list itself, as its last block:

```
N PI(s) own a repaired row. Their stored profile prose was synthesized from the old
text; re-run the pipeline for each if you want it rebuilt (`python -m src.cli
seed-profile --orcid <ORCID>`, one job each):
  0000-0001-9320-5512
  …
```

An operator who wants the list before running Step A, or wants row counts, can use the same
inventory query as Step A with a `GROUP BY`; it is a **lower bound** on the Step A list:

```sql
SELECT u.orcid, u.name, count(*) AS corrupted_rows
FROM publications p
JOIN users u ON u.id = p.user_id
WHERE (p.title ~ '[[:space:]]$'
       OR char_length(btrim(p.title)) < 2
       OR char_length(btrim(coalesce(p.abstract, ''))) < 80)
  AND u.orcid ~ '^[0-9]{4}-[0-9]{4}-[0-9]{4}-[0-9]{3}[0-9X]$'
GROUP BY u.orcid, u.name
ORDER BY corrupted_rows DESC;
```

(92 PIs on the copy. The `orcid ~` clause drops the one synthetic `SPARSE-…` user, for whom an
ORCID fetch would fail; its single row is repaired by Step A and needs nothing else.)

The per-user re-run command, one ORCID at a time:

```bash
docker compose exec -T app python -m src.cli seed-profile --orcid 0000-0001-9320-5512
```

Confirmed against the code, not guessed: `seed_profile` calls `_seed_one_orcid`
(`src/cli.py:30-80`), which looks the user up **by ORCID**, prints
`User with ORCID … already exists` instead of creating a second row, and enqueues
`Job(type="generate_profile", user_id=user.id, payload={...})`. The worker claims it
(`claim_job`) and `execute_generate_profile` (`src/worker/main.py:136-152`) calls
`run_profile_pipeline`, whose existing-row branch (`src/services/profile_pipeline.py:223-247`)
is the refresh `35cc010` added.

Operator warnings, all verified:

- **Do not use `python -m src.cli regenerate-profiles`.** It enqueues a job for *every* user with
  an ORCID — 144 on the copy, against 101 that need one (`src/cli.py:205-231`).
- The worker claims **one job at a time** and each job makes ORCID + PubMed + PMC calls and one
  or two LLM synthesis calls, so 101 jobs is an hours-long, paid queue. Enqueue in batches.
- Every run **overwrites** the PI's `ResearcherProfile` (subject to the pipeline's own
  `validated`/`lost_evidence` gate) and creates a `ProfileRevision`. Do not run Step B before
  Task 19's synthesis fix is deployed.
- The pipeline has no unchanged-input short-circuit — `raw_abstracts_hash` is written but never
  read as a skip condition — so a re-run always re-synthesizes.

## Consequence a closing comment must state

For **#22**, on COR-16 / item 15:

> The `itertext()` parser fix (`2c1d504`) and the pipeline's existing-row refresh (`35cc010`)
> are forward-looking: the rows written before them stay truncated until something re-fetches
> them. Re-measured on the production copy by re-fetching **all 4,508 rows** through the fixed
> parser: **896 rows still hold text that differs from PubMed's — 209 titles and 877 abstracts,
> and 97 % of those differences are a strict prefix of the correct value, i.e. truncation, not
> metadata drift.** Those rows are the evidence base for profile synthesis and for the agents'
> prompts. A per-user pipeline re-run does not close it on its own: it only sees PMIDs listed on
> the PI's current ORCID record, and **84 of the 512 rows the corruption signature flags carry
> neither a PMID nor a DOI that appears there** — including every row belonging to the twelve PIs
> whose ORCID works list is empty, the same PIs `scripts/backfill_publications.py` exists for.
> `scripts/repair_publication_text.py --all` (dry run by default, re-runnable) repairs all 896
> directly, and the deploy runbook now carries it as an ordered post-deploy step.

State separately that this is **not** the Task 19 defect: Task 19 is synthesis blanking curated
lists on write (8 of 141 profiles with an empty `key_targets`), this is corrupted evidence on
read. Two different columns, two different fixes.
