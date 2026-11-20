# Scripps landscaping test — cost estimate

*Companion to [`2026-11-20-scripps-landscaping-design.md`](2026-11-20-scripps-landscaping-design.md).
Written 2026-11-20. Unit costs and funnel rates come from production's Johns Hopkins run, read
with read-only queries. Scripps volumes come from the live sources that day. The cut is the
owner's starting choice of 20% (`LANDSCAPE_CUT_FRACTION=0.20`).*

## 1. Summary

| | 1 year (2025-11-20 → 2026-11-20) | 6 months (2026-05-21 → 2026-11-20) |
|---|---|---|
| Eligible documents | ~557 | ~374 |
| Kept by S2 | ~164 | ~127 |
| Ideas | ~133 | ~102 |
| Ideas in the cut (K at 20%) | 24 | 18 |
| Opportunities (~0.82 × K) | ~20 | ~15 |
| **Estimated cost** | **~$199** (range $139–$258) | **~$164** (range $114–$213) |
| If the new key can use Message Batches | ~$175 | ~$146 |
| Contingency: the bake-off's second reference pass (§6) | +$3.52 | +$3.52 |

Halving the window saves only about $35:

- **Grants don't halve.** They enter by fiscal year (§4), so both windows get the same 172
  grants.
- **Some costs don't shrink at all.** The bake-off's triage round, the regression check and the
  repeatability run cost about $54 either way.
- **What does shrink:** papers, patents, and the number of ideas in the cut (K).

## 2. Method

1. **Unit costs** come from `landscape_llm_calls`, production's ledger. The models are the ones
   production runs now: S2 Haiku 5.5, S3 Opus 5, S4 Sonnet 5.5, S6–S9 Opus 5. Every Hopkins call
   was synchronous, so these are full prices.
2. **Funnel rates** come from Hopkins's active epoch: share kept by S2 for each kind of document,
   extractions per kept document, ideas per extraction, and the cut pool per idea.
3. **Scripps volumes** come from OpenAlex, NIH RePORTER and USPTO ODP for each window (§4).
4. **The cut** is K = ceil(`LANDSCAPE_CUT_FRACTION` × n) (`src/landscaping/stages/cut.py:288-290`).

Scripps volumes are multiplied through the funnel and priced at the unit costs. Stages that run on
fixed samples (the bake-off, the regression check, the repeatability run) are priced at their
sample sizes, as the code sets them.

## 3. Hopkins inputs

**Unit costs** (all calls in the ledger to 2026-11-20 03:36 UTC):

| Stage | Model | Calls | Cost | Unit cost |
|---|---|---|---|---|
| S2 screen | Haiku 5.5 | 42,065 | $7.74 | $0.000184 a call |
| S3 extract | Opus 5 | 7,668 | $317.69 | $0.0414 a call |
| S4 triage | Sonnet 5.5 | 7,164 | $609.44 | $0.0851 a call |
| S6 deep, with full-text extraction | Opus 5 | 969 + 13 over 67 analyses | $196.62 + $4.40 | $3.00 an analysis (~14.5 calls, ~7.3 of them specialist consults) |
| S7 rerank | Opus 5 | 876 | $11.14 | $0.0127 a comparison; 786 comparisons for K = 49 gives $0.204 per idea in the cut |
| S8 same-program check and names | Opus 5 | 40 + 9 | $0.50 + $0.17 | $0.0137 per idea in the cut |
| S9 final assessment | Opus 5 | 61 | $54.25 | $0.889 an assessment. 1.25 assessments per opportunity and 0.82 opportunities per idea in the cut give $0.907 per idea in the cut |

The bake-off unit costs come from the one complete bake-off run, 2026-11-16, 20:00–21:11 UTC. An
earlier attempt that day did not finish.

| Bake-off part | Calls | Cost | Unit cost |
|---|---|---|---|
| S2: reference (Opus 5) and three candidates (Haiku 5.5, Sonnet 5, Sonnet 5.5) on each sampled document | 4 × 600, plus 7 fallbacks | $9.13 | $0.0152 a document |
| S3: reference on each kept document | 136 | $7.97 | $0.0586 a kept document |
| S3: three candidates on the 60-document sample | 3 × 60 | $2.49 | $0.0415 a sampled document |
| S4: reference and three candidates on 60 ideas | 4 × 60 | $28.43 | $0.474 an idea |

Other fixed checks:

| Part | Calls | Cost | Unit cost |
|---|---|---|---|
| Regression check | screen 597; extract 120 (Opus 5) for 60 items; triage 118 for 59 items | $0.13 + $7.77 + $11.28 | $0.00022 a screen item; $0.0648 an extract call; $0.0956 a triage call; two calls an item for S3 and S4 |
| S4 repeatability (pilot) | 100 (50 ideas × 2, Sonnet 5.5) | $6.40 | fixed |
| Reject audit | 791 | $15.01 | $0.019 an item; at most 100 rejects a stage a day, after the pilot (`worker._audit_step`) |

**Funnel rates** (Hopkins active epoch, three-year window):

| Step | Hopkins | Rate |
|---|---|---|
| Eligible papers and preprints per OpenAlex work | 27,702 of 60,368 | 0.459 |
| Kept by S2: papers and preprints | 5,184 of 28,377 | 18.3% |
| Kept by S2: grants | 1,049 of 2,262 | 46.4% |
| Kept by S2: patents | 372 of 706 | 52.7% |
| Eligible patents per patent found | 707 of 780 | 0.906 |
| Extractions per kept document | 7,020 of 6,801 | 1.032 |
| Ideas per extraction | 5,496 of 7,020 | 0.783 |
| Triage calls per idea | 5,903 of 5,496 | 1.074 |
| Cut pool per idea | 4,851 of 5,496 | 0.883 |
| Opportunities per idea in the cut | 40 of 49 | 0.82 |

## 4. Scripps volumes

Each count was read on 2026-11-20 from the live source.

| Source | 1 year | 6 months | Query |
|---|---|---|---|
| OpenAlex works (article, preprint, review) | 808: 548 articles, 206 preprints, 54 reviews | 403: 257, 123, 23 | `authorships.institutions.lineage:I123431417`, `type:article\|preprint\|review`, by publication date |
| …of which carry a Jupiter address | 60 | 26 | `raw_affiliation_strings.search:jupiter` |
| NIH grants | 171 core projects (226 project rows), fiscal 2026 | the same 171 | RePORTER `org_names: SCRIPPS RESEARCH INSTITUTE, THE` |
| NSF grants | about 1 | about 1 | small; 3 awards since 2023 |
| USPTO publications naming "Scripps Research" as applicant or assignee | 46 | 32 | ODP applications search on `earliestPublicationDate`: the source's own field and query |

**Why grants don't shrink.** A grant counts when its project period overlaps the window, or when
its fiscal year does (`qualify._grant_in_window`). RePORTER is fetched by fiscal year
(`intake._month_slices`). Both windows fall inside fiscal 2026 (October 2025 to September 2026),
so both take all of that year's projects. Fiscal 2027 has no rows yet.

**Derived Scripps funnel:**

| | 1 year | 6 months |
|---|---|---|
| Eligible papers: (OpenAlex − Jupiter) × 0.459 | 343 | 173 |
| Eligible grants | 172 | 172 |
| Eligible patents: USPTO × 0.906 | 42 | 29 |
| **Eligible total** | **557** | **374** |
| Kept by S2 | 164 | 127 |
| Extractions | 170 | 131 |
| Ideas | 133 | 102 |
| Triage calls | 143 | 110 |
| Cut pool n | 117 | 90 |
| **K at 20%** | **24** | **18** |
| Opportunities | ~20 | ~15 |

## 5. Cost per stage

| Stage | Formula | 1 year | 6 months |
|---|---|---|---|
| S2 screen | eligible × 1.03 × $0.000184 | $0.11 | $0.07 |
| S3 extract | extractions × $0.0414 | $7.03 | $5.42 |
| S4 triage | triage calls × $0.0851 | $12.14 | $9.35 |
| S5 cut | no model call, unless a boundary tie needs the tournament | ~$0 | ~$0 |
| S6 deep | K × $3.00 | $72.01 | $54.01 |
| S7 rerank | K × $0.204 | $4.90 | $3.67 |
| S8 same-program check and names | K × $0.0137 | $0.33 | $0.25 |
| S9 final assessment | K × $0.907 | $21.78 | $16.33 |
| Bake-off S2 | min(600, eligible) × $0.0152 | $8.47 | $5.69 |
| Bake-off S3 | kept × $0.0586 + min(60, kept) × $0.0415 | $12.13 | $9.91 |
| Bake-off S4 | min(60, ideas) × $0.474 | $28.43 | $28.43 |
| Regression check, first epoch | screen + 2 × S3 + 2 × S4 items, as Hopkins | $19.36 | $19.32 |
| S4 repeatability | 50 ideas × 2 calls | $6.40 | $6.40 |
| Reject audit | ≤ 100 S2 rejects a day × 3 days × $0.019 | $5.69 | $4.69 |
| **Total** | | **$198.78** | **$163.55** |

S6 through S9 come to $4.125 per idea in the cut, so each idea added to the cut, or removed,
moves the total by about that much. At 20% the cut is about half the total: $99 for 1 year and
$74 for 6 months.

## 6. Caveats

- **±30% on everything.** These are the largest unknowns:
  - Scripps's share of papers with a Scripps senior author may differ from Hopkins's 0.459.
  - Ideas group differently at small scale.
  - S6 and S9 costs follow dossier size: S9 read about 111,000 input tokens an assessment at
    Hopkins.
  - OpenAlex indexes the newest months late, which affects the 6-month window most.
- **Second reference pass.** The code asks the bake-off's S3 reference a second time on its
  60-document sample (`bakeoff._extract_round`). Hopkins's ledger shows no such calls, so it is a
  contingency: 60 × $0.0586 = $3.52.
- **Tech-transfer listings are off** (design C5), so they cost nothing. Scripps's feed holds 38
  listings. If they were turned on, about 17 would be kept, at Hopkins's 44.6% rate for
  listings. That adds about $0.75 of S3 and $1.30 of S4, plus whatever reaches the cut.
- **Probes** are drawn only for a blind batch, which is every fourth batch
  (`review_batches.draw_probes`). A test that issues one or two batches draws none.
- **Batches.** If the new key's account accepts Message Batches, S3, S4, the S8 names, S9 and the
  reject audit cost half (`llm_pricing.BATCH_MULTIPLIER`). That saves about $23 for 1 year and $18
  for 6 months. The bake-off, the regression check, S6 and S7 are synchronous either way.
- **The cap paces the run.** At $100 a day (C4, soft), a $199 run spans at least two days, and
  PubMed and GEO fetch only off-peak. So allow two to four days.
- **Re-check (C11).** When the bake-off report is stored, the real eligible count and the
  bake-off's real spend replace §4's estimates. If the new total is more than 25% over the
  estimate for the chosen window, the run waits for the owner.

## 7. Re-running this estimate

The ledger figures in §3 come from read-only queries on production: `landscape_llm_calls`
grouped by `stage`, `model` and `item_ref` prefix (`bakeoff`, `regression`), plus the
active-epoch rows of `landscape_screens`, `landscape_extractions`, `landscape_ideas`,
`landscape_triage`, `landscape_deep`, `landscape_comparisons` and `landscape_assessments`. The
Scripps volumes in §4 come from the public APIs named there, plus USPTO ODP with a key.
