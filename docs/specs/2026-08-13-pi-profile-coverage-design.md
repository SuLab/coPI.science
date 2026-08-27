# PI profile coverage: audit findings, backfill design, and prevention

**Status:** PROPOSED 2026-08-13; data repairs deployed 2026-08-13/14. **Core
pipeline tasks implemented 2026-08-24** as part of the manager Add-PI auto-flow
(`docs/plans/2026-08-24-manager-add-pi-autoflow-plan.md`): Task 1 (pubmed
itertext parse), the D4b ESearch-DOI rule (unique idlist + round-trip verify,
now inside `convert_dois_to_pmids`), `src/services/corpus.py` (S1 ORCID + S2
OpenAlex + S3 auid + S4 name+affiliation with mandatory disambiguation;
rank year-DESC; 50-cap LAST; stage failure raises for job retry), and the
pipeline integration (storage full-career capped, synthesis/export
tenure-filtered). NOT implemented: P2's coverage_suspect column and the full
Task 6 activation gate (a no-migration subset ships instead:
`src/services/agent_activation.py` refuses activating a pi_lab agent whose
profile is missing/ungrounded or whose newest job is dead, both
admin_approve_agent branches, logged override), and Task 8's historical
backfill machinery.
**Written:** 2026-08-13, against `blackbird` @ `22dd952`.
**Revised:** 2026-08-13, after an adversarial audit. The audit re-verified the headline
claims against the production database over SSH (read-only): 62 active `pi_lab` agents;
`kavran`/`pearce`/`rebecca`/`mukherjeeclavin` at 0 publications; `wu` at 3; `leung` at 53
(1998–2020); `salzberg` at 50 spanning 2009–2013; 1,835 stored titles for active PIs, 110
carrying the trailing-space truncation signature; 116 with no abstract; `grant_titles`
empty and evidence counts NULL for all 62. It also reproduced the Kavran corpus claims
live (ORCID: 0 works; OpenAlex: 33 PMID-carrying works; Hippo/TEAD titles). Corrections
from the audit are folded in below and the companion plan was revised to match.
**Scope:** the 62 `status='active'`, `role='pi_lab'` agents in the `agents` table, plus the
two code paths that create them. `BlackbirdBot` (`role='scout_hub'`) has no `user_id` and
no publications by design; it is out of scope.

**Companion document:** `docs/plans/2026-08-13-pi-profile-coverage-plan.md` — the
task-by-task implementation plan. This document is the *why* and the *what*; the plan is
the *how*. (Both documents originally lived in `data/`, which is gitignored and
bind-mounted into the agent containers; they were moved to `docs/` so they can be
committed and reviewed.)

---

## 0. Summary

Every PI profile in the system is synthesized by an LLM from a set of publication
abstracts. The quality of the bot is therefore bounded by the completeness of that set.
**37 of 62 active PIs are materially under-filled, and 4 of them have profiles synthesized
from zero publications.** Within the deliberate 50-publication cap, **911 publications are
recoverable**. A further **5 PIs are at the cap but hold the wrong 50** — an arbitrary
slice rather than the most recent.

The causes are four independent defects in two ingestion paths, not one bug. All four were
confirmed empirically against live APIs, not inferred from reading the code.

The most consequential finding is not a count. `KavranBot` is live, and its profile asserts
that Jennifer Kavran's focus is *"the BMP/TGF-β superfamily and ErbB family"*. Her actual
recent output is Hippo/TEAD condensate biology, MST-family kinase dimerization, and PLVAP
structure. The system has never read one of her papers, and the model filled the gap with
plausible, specific, wrong prose. Three other bots are in the same state.

---

## 1. How this was measured

Reproducible from a host with outbound network. All four sources are unauthenticated.

| Source | Endpoint used | What it answers | Known bias |
|---|---|---|---|
| **ORCID** | `pub.orcid.org/v3.0/{id}/works` | what the PI has self-curated | Under-reports badly; entirely PI-maintained |
| **OpenAlex** | `api.openalex.org/works?filter=author.orcid:{id}` | the real corpus, with `ids.pmid` + year per work | Only counts works where the ORCID is *linked*; undercounts PIs who never linked it (e.g. `gill`: 14 works found, true corpus far larger) |
| **PubMed** | `esearch.fcgi` with `{orcid}[auid]` | papers PubMed itself has ORCID-tagged | Floor, not ceiling — tagging is sparse and recent |
| **PubMed** | `esearch.fcgi` with `Last First[Author] AND "Johns Hopkins"[Affiliation]` | name+affiliation corpus | Good specificity at JHU; misses papers written at prior institutions |
| **PubMed** | `efetch.fcgi` per PMID | author list + per-author affiliation | Ground truth for identity checks |

Because each source is biased in a different direction, the **ceiling** for a PI is taken as
`max(openalex_pubmed_indexed, pubmed[auid], pubmed_name+aff)`. Name-only PubMed counts were
collected but **excluded** from the ceiling: they are inflated by homonyms (`Hardwick` alone
returns 1,779).

**Google Scholar was deliberately not used.** It has no public API and its terms prohibit
scraping; the same coverage is obtainable from OpenAlex + PubMed, which are citable and
stable. **Semantic Scholar** (`api.semanticscholar.org/graph/v1`) was confirmed reachable
and is a viable cross-check, but adds nothing OpenAlex does not already provide for this
population. **Lab websites and CVs** have a narrow, real role — see §4.4 — but are not a
bulk source and must never be a substitute for indexed papers (see D5).

Audit scripts and raw results: `audit.py`, `truth.py`, `drill.py`, `resolve_test.py`,
`audit.json`, `truth.json` lived in a session-scoped scratchpad and are **not preserved**
— the §3 ledger is the durable record, and the numbers are re-derivable from the public
APIs in ~20 min. The database-side numbers were independently re-verified against
production (read-only) on 2026-08-13; see the revision note at the top.

---

## 2. The defects

### D1 — Three ORCID papers disable the PubMed search entirely
`scripts/generate_sparsedata_user.py:724`

```python
if len(orcid_pmids) >= EVIDENCE_FLOOR_PAPERS:   # == 3
    kept_pmids = orcid_pmids
    audit.n_pubmed_hits = len(orcid_pmids)
else:
    ...name+affiliation PubMed search, disambiguation, merge...
```

The name+affiliation search — the only mechanism that can find papers a PI never added to
ORCID — runs **only when ORCID yields fewer than 3 PMIDs**. A PI who curated 3 works gets 3
papers forever.

This line explains the Tier B members whose ORCID yielded 3+ PMIDs. It does **not**
explain the seven Tier B rows with ORCID = 0 (`agre`, `epearce`, `hart`, `culotta`,
`hardwick`, `mueller`, `srinivasan`): for those the name+affiliation search *did* run,
and their deficits come from the affiliation filter rejecting prior-institution papers
and from the `retmax=50` retrieval cap — the mechanisms §4.3 exists to compensate for.
Representative D1 casualties:

- `wu` — 3 on ORCID, 3 in DB, **118** PubMed-indexed papers exist.
- `casadevall` — 20 on ORCID, 20 in DB, **786** exist. He is among the most published
  microbiologists alive; his bot reasons from 2.5% of his work.
- `shastri` — 5 on ORCID, 4 in DB, **134** exist.

The gate is a floor being used as a ceiling. `EVIDENCE_FLOOR_PAPERS` legitimately answers
"is there enough evidence to persist this profile at all?" (line 750). Reusing the same
constant at line 724 to answer "should we bother searching PubMed?" conflates *sufficient*
with *complete*.

### D2 — The new-PI path has no PubMed fallback at all
`src/services/profile_pipeline.py:101-171`, reached from `src/worker/main.py:71`

D1 concerns the bulk seeder, which is a one-off script. The path that runs for **every PI
added from today onward** — self-service signup and `python -m src.cli seed-profiles` — is
`run_profile_pipeline`, and it is *purely* ORCID-driven:

```
Step 3: fetch_orcid_works(orcid)  →  pmids
Step 4: fetch_pubmed_records(pmids)
```

There is no author search, no OpenAlex, no affiliation query. When ORCID is thin the
pipeline emits a `sparse_orcid` progress note (line 157) asking the *PI* to go fix their
ORCID record, and then proceeds to synthesize a profile from whatever it has. It also has
**no 50 cap**, so it is inconsistent with the seeder's deliberate policy in the other
direction.

**This is the defect that matters most going forward.** Fixing the historical data without
fixing D2 means the next PI onboarded reproduces the problem.

### D3 — The ORCID slice is not chronological
`scripts/generate_sparsedata_user.py:702`

```python
orcid_pmids = list(dict.fromkeys(orcid_pmids))[:PUBMED_FETCH_CAP]
```

The 50-item cap is intentional and stays. But `[:50]` here slices **ORCID's group order**,
which is neither chronological nor stable. The PubMed path is correctly sorted
(`"sort": "pub date"`, line 253); the ORCID path is not sorted at all.

Consequence — five bots at the cap holding the wrong 50:

| agent | DB year range | DB papers ≥2021 | newest real work | indexed corpus |
|---|---|---|---|---|
| `salzberg` | 2009–2013 | **0** | 2026 | 350 |
| `janak` | 2007–2015 | **0** | 2026 | 162 |
| `leung` | 1998–2020 | **0** | 2026 | 111 |
| `norris` | 2007–2023 | 14 | 2026 | 167 |
| `pekosz` | 2006–2026 | 8 | 2026 | 334 |

`SalzbergBot` currently reasons as though Steven Salzberg published nothing after 2013.
`leung` additionally holds **53** rows, exceeding the cap it was supposed to respect.

### D4 — DOI→PMID resolution loses works silently
`src/services/pubmed.py:250-296`

`convert_dois_to_pmids` is two-phase: a batched NCBI ID-converter call (PMC-indexed only),
then a per-DOI ESearch fallback. Phase-2 failures are caught and logged at **`logger.debug`**
(line 294-295), which is below the configured level, so a partial resolution is
indistinguishable from a complete one.

Measured on `lee` (Daeyeol Lee): ORCID lists 155 works, 139 with DOIs, 123 of them
journal articles. The DB holds **21**. I re-ran the pipeline's own two-phase resolver
against a 40-DOI sample of his ORCID record:

```
Phase 1 (NCBI ID converter): 36/40 resolved
Phase 2 (ESearch [doi]):      4/4  of the remainder resolved
TOTAL RESOLVABLE:            40/40 (100%)
```

The works were resolvable then and are resolvable now. They were dropped in transit.
Also affects `huganir` (53 ORCID → 42 DB) and `mcmeniman` (32 → 24).

### D5 — Profiles are persisted with zero publications
`scripts/generate_sparsedata_user.py:750`

```python
if audit.n_papers_kept < EVIDENCE_FLOOR_PAPERS and not faculty_text:
    ...reject...
```

The `or a faculty page` branch means a scraped web page alone is sufficient grounding to
persist a profile **and create the agent**. Four active bots reached production this way:

| agent | DB pubs | summary words | techniques | indexed corpus that exists |
|---|---|---|---|---|
| `kavran` | 0 | 183 | 11 | 33 |
| `pearce` (Erika Pearce) | 0 | 190 | 10 | 132 |
| `rebecca` (Vito Rebecca) | 0 | 184 | 12 | 53 |
| `mukherjeeclavin` | 0 | 184 | 6 | 5 |

These are not obviously-empty profiles. They are fluent, specific, and confidently wrong in
their specifics. Verified for `kavran`: the stored summary claims *"receptor tyrosine kinases
and pseudokinases"* and *"the BMP/TGF-β superfamily and ErbB family"*. Her actual recent
titles, from OpenAlex:

```
2026  TEAD1 condensates are transcriptionally inactive storage sites on pericentromeric heterochromatin
2026  A conserved dimerization element is required for protein kinase activation by trans-autophosphorylation
2023  Dimerization and autophosphorylation of the MST family of kinases are controlled by the same segment
2023  Structural insights into plasmalemma vesicle-associated protein (PLVAP)
```

The real centre of mass — Hippo/MST kinase regulation — is absent, and the stated one is
invented. `ResearcherProfile.evidence_state` (`src/models/profile.py:84-119`) exists
precisely to name this condition (`no_evidence_available`), but the seeder never writes the
columns it reads, so all 62 rows report `unknown`. The guard exists and is disconnected.

### D6 — Titles and abstracts truncate at the first markup element
`src/services/pubmed.py:207-218`

```python
title_el = article.find(".//ArticleTitle")
record["title"] = (title_el.text or "") if title_el is not None else ""
```

`Element.text` returns only the character data **before the first child element**. PubMed
`<ArticleTitle>` routinely contains `<i>` (species names), `<sup>`, `<b>`. Everything from
the first tag onward is discarded. **112 of 1,835 stored titles (6.1%)** are truncated:

```
mcmeniman  41465638  "Mapping the Genetic Relatedness of Outdoor-Biting "
mcmeniman  36223988  "Batch Rearing "
mcmeniman  37612143  "Quantification of "
hardwick   36603033  "Similar evolutionary trajectories in an environmental "
```

The identical bug is present for abstracts at line 214 (`abstract_el.text`), which silently
drops abstract text after any inline markup. (Separately, 116 stored publications have no
abstract at all — most of those are genuinely abstract-less records such as editorials and
errata, not truncation victims; the truncated-abstract set overlaps the truncated-title
set but has not been separately quantified.) The damage is concentrated in vector-biology
and mycology labs, where italicised binomials are near-universal — exactly the PIs whose
subject matter the truncation destroys.

### D7 — Duplicate works across preprint / journal / erratum PMIDs
Deduplication is keyed on PMID (`profile_pipeline.py:182`). A preprint, its published
version, and its erratum carry three distinct PMIDs and three near-identical titles, so all
three are stored. **37 redundant rows across 20 PIs**; worst are `huganir` (6), `klein` (4),
`pombo` (3), `dang` (3). `mueller` holds *"Preserving Derivative Information while
Transforming Neuronal Curves"* three times (PMIDs 38036915, 36994162, 37034653).

Effect is dilution: duplicates consume slots in the 50-cap and in the 30-abstract synthesis
window (`_build_synthesis_context`, `sorted_pubs[:30]`).

Remediation collapses **preprint/journal pairs** by normalised title, keeping the later
(published) version. **Errata are deliberately not collapsed**: their titles carry an
`Erratum:`/`Correction to:` prefix, and collapsing by title would keep the erratum — the
later of the pair — and discard the real article. Errata are already excluded from
synthesis by `EXCLUDED_TYPES` (`profile_pipeline.py:39`); the residual cost is that an
erratum can occupy a cap slot, which is accepted.

### D8 — Grants and evidence provenance are never populated
Across all 62 active profiles:

```
zero_grants | null_summary | null_evidence | has_user_text | total
         62 |            0 |            62 |             0 |    62
```

`grant_titles` is empty for **every** PI, though `fetch_orcid_grants` exists
(`src/services/orcid.py:76`) and the seeder never calls it. `evidence_pmid_count` and
`evidence_pub_count` are NULL for **every** PI, so `evidence_state` returns `unknown`
system-wide and cannot distinguish D5's four hollow profiles from the 58 grounded ones.

### What is *not* wrong

Adversarial checks that came back clean — recorded so they are not re-litigated:

- **Name disambiguation held.** Every spot-check passed. `srinivasan` (12 papers, all
  *Plasmodium*) is correct despite a very common surname. `hart` (Lyme/*Borrelia*) correct.
  `agre` (aquaporins) correct. I suspected cross-attribution in `mueller` — three
  computational-neuroanatomy papers in a hair-cell lab — and checked the efetch author
  affiliations: all three are genuinely co-authored by the JHU Ulrich Mueller
  (`umuelle3@jhmi.edu`, Dept. of Neuroscience). **No false attributions were found.**
- **Thin ≠ broken.** `nayar` holds 6 papers because 26 of her 33 ORCID works are bioRxiv
  preprints not indexed in PubMed; 6 is correct. `krieger` (10) is likewise correct. These
  two are Tier E and need no backfill. `oneal` and `srinivasan` also have genuinely small
  indexed corpora, but with small, real gaps (5 each) — they sit in Tier B and are
  backfilled **only to their ceilings** (20 and 17). None of the four may be padded with
  weak name-matches beyond what the ORCID-anchored sources support.
- **The 50 cap is being applied** on the PubMed path, sorted by publication date, as
  intended. D3 is a defect in the *other* path only.

---

## 3. The remediation ledger

`ceiling` = best-supported estimate of the PI's PubMed-indexed corpus.
`target` = `min(50, ceiling)` — the cap is retained. `gap` = `target − DB`.

| # | agent_id | PI | DB | ORCID | OA-idx | PM[auid] | PM name+aff | ceiling | target | gap | tier |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `pearce` | Erika Pearce | 0 | 0 | 132 | 31 | – | 132 | 50 | **50** | A |
| 2 | `rebecca` | Vito Rebecca | 0 | 0 | 53 | 9 | 32 | 53 | 50 | **50** | A |
| 3 | `kavran` | Jennifer Kavran | 0 | 0 | 33 | 7 | 25 | 33 | 33 | **33** | A |
| 4 | `mukherjeeclavin` | Bipasha Mukherjee-Clavin | 0 | 0 | 5 | 2 | – | 5 | 5 | **5** | A |
| 5 | `lee` | Daeyeol Lee | 21 | 155 | 129 | 18 | 21 | 129 | 50 | **29** | C |
| 6 | `huganir` | Richard Huganir | 42 | 53 | 410 | 49 | 213 | 410 | 50 | **8** | C |
| 7 | `mcmeniman` | Conor McMeniman | 24 | 32 | 32 | 11 | 20 | 32 | 32 | **8** | C |
| 8 | `wu` | Carl Wu | 3 | 3 | 118 | 17 | 30 | 118 | 50 | **47** | B |
| 9 | `shastri` | Nilabh Shastri | 4 | 5 | 134 | 3 | 8 | 134 | 50 | **46** | B |
| 10 | `bailey` | Scott Bailey | 5 | 6 | 47 | 4 | 32 | 47 | 47 | **42** | B |
| 11 | `weeraratna` | Ashani Weeraratna | 9 | 10 | 157 | 14 | 57 | 157 | 50 | **41** | B |
| 12 | `agre` | Peter Agre | 10 | 0 | 229 | 2 | 33 | 229 | 50 | **40** | B |
| 13 | `zavala` | Fidel Zavala | 12 | 12 | 198 | 20 | 95 | 198 | 50 | **38** | B |
| 14 | `carlton` | Jane Carlton | 13 | 13 | 160 | 15 | 17 | 160 | 50 | **37** | B |
| 15 | `coppens` | Isabelle Coppens | 13 | 21 | 179 | 16 | 106 | 179 | 50 | **37** | B |
| 16 | `sinnis` | Photini Sinnis | 15 | 20 | 126 | 25 | 69 | 126 | 50 | **35** | B |
| 17 | `markham` | Richard Markham | 3 | 6 | 11 | 6 | 34 | 34 | 34 | **31** | B |
| 18 | `casadevall` | Arturo Casadevall | 20 | 20 | 786 | 286 | 516 | 786 | 50 | **30** | B |
| 19 | `wang` | Jiou Wang | 21 | 27 | 33 | 20 | 64 | 64 | 50 | **29** | B |
| 20 | `cai` | Danfeng Cai | 23 | 25 | 50 | 8 | 21 | 50 | 50 | **27** | B |
| 21 | `chute` | Christopher Chute | 23 | 26 | 403 | 42 | 116 | 403 | 50 | **27** | B |
| 22 | `thompson` | Elizabeth Thompson | 25 | 34 | 71 | 11 | 150 | 150 | 50 | **25** | B |
| 23 | `epearce` | Edward Pearce | 26 | 0 | 251 | 14 | 28 | 251 | 50 | **24** | B |
| 24 | `davis` | Kimberly Davis | 28 | 33 | 66 | 16 | 34 | 66 | 50 | **22** | B |
| 25 | `green` | Rachel Green | 28 | 31 | 193 | 32 | 139 | 193 | 50 | **22** | B |
| 26 | `hart` | Thomas Hart | 8 | 0 | 27 | 5 | 4 | 27 | 27 | **19** | B |
| 27 | `camacho` | Emma Camacho | 25 | 32 | 41 | 10 | 28 | 41 | 41 | **16** | B |
| 28 | `culotta` | Valeria Culotta | 36 | 0 | 139 | 10 | 73 | 139 | 50 | **14** | B |
| 29 | `mugnier` | Monica Mugnier | 29 | 35 | 42 | 19 | 28 | 42 | 42 | **13** | B |
| 30 | `gordy` | James Gordy | 14 | 15 | 26 | 7 | 22 | 26 | 26 | **12** | B |
| 31 | `hardwick` | J. Marie Hardwick | 38 | 0 | 163 | 13 | 142 | 163 | 50 | **12** | B |
| 32 | `mueller` | Ulrich Mueller | 10 | 0 | 21 | 16 | 11 | 21 | 21 | **11** | B |
| 33 | `kevrekidis` | Ioannis Kevrekidis | 41 | 46 | 141 | 19 | 41 | 141 | 50 | **9** | B |
| 34 | `klein` | Sabra Klein | 44 | 48 | 285 | 60 | 227 | 285 | 50 | **6** | B |
| 35 | `oneal` | Anya O'Neal | 15 | 17 | 20 | 6 | 1 | 20 | 20 | **5** | B |
| 36 | `srinivasan` | Prakash Srinivasan | 12 | 0 | 4 | 3 | 17 | 17 | 17 | **5** | B |
| 37 | `perrin` | Eliana Perrin | 46 | 2 | 37 | 3 | 67 | 67 | 50 | **4** | B |

**Tier D — at the cap, wrong 50. Re-slice, do not add.** `salzberg`, `janak`, `leung`,
`norris`, `pekosz` (see D3 table). `leung` must also drop from 53 to 50.

**Tier E — no action (20 PIs).** `coller`(49), `pombo`(49), `baumgarth`(50), `conrad`(50),
`dang`(50), `dimopoulos`(50), `egeblad`(50), `feinberg`(50), `gill`(50), `hamacherbrady`(42),
`margolick`(50), `pienta`(50), `prigge`(50), `scott`(50), `sullivan`(50), `tripathi`(50),
`tsapatsis`(50), `suez`(40), `krieger`(10), `nayar`(6).

| Tier | PIs | Publications recoverable |
|---|---|---|
| A — zero evidence | 4 | 138 |
| B — ORCID sparse, needs external backfill | 30 | 726 |
| C — ingestion loss, refetch alone suffices | 3 | 45 |
| D — wrong 50, re-slice | 5 | 0 (replacement, not addition) |
| E — adequate | 20 | 2 (deliberately not pursued) |
| **Total** | **62** | **911** (909 pursued) |

(4 + 30 + 3 + 5 + 20 = 62. Tier E's 2 recoverable publications are counted for honesty
but are left alone per §6 — the backfill pursues 909 across the 37 Tier A/B/C PIs.)

---

## 4. How to fill the profiles

### 4.1 The canonical corpus algorithm

One function, used by both ingestion paths and by the backfill. Union first, rank second,
cap last — the current code caps before it has finished gathering, which is what makes
D1 and D3 possible.

```
resolve_corpus(orcid, name, affiliations, cap=50) -> list[PMID]

  1. GATHER  (union of all available sources; never short-circuit)
       S1  ORCID works           → PMIDs directly, plus DOIs for resolution
       S2  OpenAlex by ORCID     → works carrying ids.pmid, with year
       S3  PubMed [auid] search  → f"{orcid}[auid]"
       S4  PubMed name+aff       → f"{Last} {First}[Author] AND (\"{aff}\"[Affiliation])"
                                   sorted by pub date, retmax 200
  2. RESOLVE any DOI-only works via convert_dois_to_pmids  (D4: count and LOG the misses)
  3. DISAMBIGUATE  S4 hits only  (S1–S3 are ORCID-anchored and bypass this).
       MANDATORY, in-resolver, for every caller — see §4.3. S4 is SKIPPED entirely
       when no affiliation is known: a name-only search is prohibited (§1: `Hardwick`
       alone returns 1,779).
  4. DEDUPE   by PMID, then by normalised title  (D7: collapses preprint/journal pairs;
              errata keep their prefixed titles and are not collapsed — see §2 D7)
  5. DATE     any still-undated PMIDs via one esummary batch. S3/S4 hits arrive as bare
              PMIDs with no year; ranking them at year=None silently prefers ANY dated
              old work over an undated recent one, which re-creates D3 for exactly the
              PIs whose ORCID/OpenAlex coverage is thin.
  6. RANK     by publication year DESC, then PMID DESC as a stable tiebreak
  7. CAP      [:50]                                          ← the cap is applied HERE, last
```

An empty/placeholder ORCID skips S1–S3 and resolves from S4 alone (still disambiguated,
still affiliation-gated).

Steps 1 and 7 are the whole design. The cap is a *presentation* decision applied to a fully
gathered, fully ranked corpus. It is never a *retrieval* limit, and gathering is never
skipped because one source already returned something.

`retmax` on S4 is 200, not 50: the search must over-fetch so that step 5 has a real corpus
to rank before step 6 truncates it. This is not a change to the cap policy.

### 4.2 Source precedence

Sources disagree; precedence resolves it.

1. **PMID identity** — a paper is its PMID. Everything else is a route to one.
2. **PubMed `efetch` is authoritative for content** — title, abstract, journal, year,
   author list. OpenAlex and ORCID metadata are never stored as content.
3. **ORCID is authoritative for DOI** where it disagrees with PubMed and the mismatch is
   not verifiable — the existing `reconcile_pub_doi` gate
   (`profile_pipeline.py:196-210`) already encodes this and must be preserved.
4. **OpenAlex is used for discovery and for the coverage estimate only**, never as a
   content source.

### 4.3 Disambiguation rules for S4

S1–S3 are anchored to a verified ORCID and are trusted. Only S4 (name + affiliation) can
introduce a wrong author, so only S4 is filtered — but it must be filtered in **every**
caller. The disambiguator therefore lives **inside `src/services/corpus.py`** and is
applied by `resolve_corpus` itself by default; it is not an optional hook a caller can
forget (the pipeline has never had one, and a forgotten hook there would union raw
name-search hits into every future PI's corpus — the false-attribution failure §2
verified absent). Two hard rules:

- **No S4 without an affiliation.** If `user.institution` and `user.department` are both
  empty, S4 is skipped; a bare `Last First[Author]` search is never run.
- **Do not tighten the matcher.** Port the existing `_disambiguate` logic from the seeder
  byte-compatibly, with its `INSTITUTION_STOPWORDS` handling; a hit is kept when **both**
  hold:

- an author's `LastName` matches and `ForeName`/`Initials` are consistent with the PI's
  first name (`_author_first_name_matches`), and
- that author's affiliation string shares a distinctive (stopword-stripped) token with one
  of the PI's known affiliations.

Papers written at a **prior institution** will fail the affiliation half. This is the known
and accepted cost of S4 and is why S2 (OpenAlex by ORCID, institution-agnostic) is in the
union — it recovers exactly that set. `agre` (JHU, but decades at other institutions) and
`epearce`/`pearce` (Max Planck before JHU) are the cases this matters for.

### 4.4 Tier A requires a human, not a better query

For the four zero-evidence PIs the failure was identity resolution, not availability — the
papers exist and OpenAlex finds them from the ORCID. But these four already have a
*published, live, confidently wrong* profile. The backfill must therefore:

1. Resolve the corpus and **present the top 10 titles for human confirmation** before any
   synthesis, keyed to the ORCID on the `users` row.
2. Regenerate the profile only after confirmation.
3. Until confirmed, set `status='pending'` on the agent so the bot stops posting.

Lab websites and CVs belong **here and only here** — as a human-readable tiebreak when
confirming that ORCID `0000-0001-9117-5209` is the Jennifer Kavran the roster means. They
are not a source of publications and, per D5, are not evidence for synthesis.

### 4.5 Tier D is a replacement, not an addition

For `salzberg`, `janak`, `leung`, `norris`, `pekosz`: run the same `resolve_corpus`, then
**delete the rows outside the new top-50** rather than adding to them. `leung` returns to
50 from 53. No PI ends above 50.

### 4.6 Order of operations

The title/abstract truncation (D6) must be fixed **before** any backfill runs, or 6% of the
newly fetched titles arrive damaged and the whole exercise has to be repeated.

Two ordering rules the first draft got wrong:

- **Tier A is demoted to `pending` before anything is applied anywhere.** §4.4's human
  gate is meaningless if a blanket backfill or an all-users regeneration reaches the four
  hollow profiles first. The roster sync picks up the demotion within ~30s; no restart.
- **The backfill re-fetches kept rows, not just missing ones.** A PMID already stored is
  re-fetched and its title/abstract/journal/year updated in place — that is what actually
  repairs the ~112 truncated titles and truncated abstracts sitting in rows that no diff
  would otherwise touch. Regeneration alone cannot fix them: synthesis reads the stored
  rows.

```
D6 fix → deploy → Tier A agents → 'pending'
       → resolve_corpus → backfill Tier C → Tier B → Tier D re-slice
         (each --apply also refreshes kept rows' title/abstract/journal/year)
       → regenerate profiles for Tiers B/C/D (per-agent, not all-users)
       → Tier A: human confirm → backfill → regenerate → re-activate (gated)
```

Profile regeneration re-exports `profiles/public/*.md` itself
(`profile_pipeline.py:477-484`), and the running simulation picks the files up by mtime
polling (`simulation.py:4822`) — no separate export step and no restart are needed for
profile *content*; the restart at the end is for *code*.

Profile *regeneration* is required after backfill: the stored `research_summary` was
synthesized from the old, thin abstract set and does not update itself. `src/cli.py:199`
(`regenerate-profiles`) is the existing entry point, **but it currently takes no filter —
it enqueues every user with an ORCID**. Tiered execution requires the `--agent` filter the
plan adds (Task 8); an unfiltered run would regenerate Tier A before its human gate and
churn Tier E for nothing.

---

## 5. Prevention: what changes in the codebase

The goal is that **a PI added tomorrow cannot reach `status='active'` with a hollow or
truncated profile**, without anyone remembering to check.

### P1 — One corpus resolver, two callers
New module `src/services/corpus.py` implementing §4.1. Both
`src/services/profile_pipeline.py` (step 3) and `scripts/generate_sparsedata_user.py`
(line 724) call it. The `>= EVIDENCE_FLOOR_PAPERS` short-circuit at line 724 is **deleted** —
the union in step 1 makes it meaningless. `EVIDENCE_FLOOR_PAPERS` keeps its one legitimate
job at line 750.

This closes D1, D2 and D3 together, because there is no longer a second, differently-broken
path to maintain.

### P2 — A coverage gate that fires on the discrepancy, not the absence
The system's blind spot is that "ORCID returned 3 works" and "this PI has published 3
papers" are indistinguishable today. They are distinguishable the moment an independent
estimate is on hand, and after P1 one always is.

At the end of resolution, compare retrieved against `ceiling` (§3) and **persist the
verdict**: a new nullable boolean column `researcher_profiles.coverage_suspect` (alembic
migration), written by both ingestion paths on every run. Raise the flag when
`retrieved < 0.5 × min(50, ceiling)`, with a floor: when `min(50, ceiling) < 10` the
estimate is too noisy to accuse the pipeline, and a genuinely early-career PI must not be
flagged (`COVERAGE_FLOOR = 10`).

"Loudly" means three concrete things, not a log line: the flag is stored on the profile
row, surfaced as a job-progress entry the PI-facing page renders, and **enforced at
activation by P3**. An in-memory flag that only reaches a log nobody tails would be D8
all over again. Under this rule the seeder run that produced `wu` (3 retrieved vs 50
expected) would have been flagged and blocked at activation instead of silently
succeeding.

The estimate is already free: S2 and S3 are in the union.

### P3 — Wire up `evidence_state`, then enforce it
`ResearcherProfile.evidence_state` (`src/models/profile.py:84`) already names the exact
condition D5 describes, and already documents `no_evidence_available`. It is dead code
because nothing writes its inputs on the seeder path.

- Write `evidence_pmid_count` / `evidence_pub_count` (and P2's `coverage_suspect`) from
  **both** paths.
- **Provisioning gate:** `admin_approve_agent` is the only *function* that writes
  `status='active'`, but it does so from **two branches**: the pending→active approval at
  `src/routers/admin.py:1011`, and the `agent_status` form-dropdown edit at `:1014-1015`
  (`VALID_AGENT_STATUSES` includes `"active"`, so an admin re-activating an inactive or
  suspended agent takes this branch). The gate must run **before both** — gating only the
  approval branch leaves a one-click bypass on the same page. It refuses activation when
  `evidence_state != "grounded"` or `coverage_suspect` is set. Override must be an
  explicit, logged admin action (a form control that must be added to
  `templates/admin/agent_detail.html`), not a default.
- **Scope:** the gate applies to `role='pi_lab'` agents. A `scout_hub` agent
  (BlackbirdBot) has no `user_id` and no profile *by design* and must remain approvable
  without an override.

A faculty page may still justify creating a `pending` agent. It may never justify an
`active` one.

### P4 — Extract element text, not `Element.text`
`src/services/pubmed.py:207` and `:214` — use `"".join(el.itertext())` so inline markup is
flattened rather than truncating the field. Closes D6 for all future fetches.

### P5 — Dedupe on normalised title as well as PMID
In `resolve_corpus` step 4, so preprint/journal/erratum triplets collapse before the cap is
applied. Closes D7.

### P6 — Call `fetch_orcid_grants` from the seeder path
Closes D8's grants half. The function already exists and is already called by
`profile_pipeline.py:96`; only the seeder omits it.

### Defect → prevention map

| Defect | Closed by | Fixes future PIs | Fixes existing 62 |
|---|---|---|---|
| D1 ORCID ≥3 disables PubMed | P1 | yes | via backfill |
| D2 new-PI path is ORCID-only | P1 | **yes — the critical one** | n/a |
| D3 non-chronological slice | P1 (rank before cap) | yes | via Tier D re-slice |
| D4 silent DOI→PMID loss | P1 step 2 (log + count), P2 | yes | via backfill |
| D5 zero-publication profiles | P3 | yes | via Tier A + agent → `pending` |
| D6 truncated titles/abstracts | P4 | yes | via backfill re-fetch of **all stored PMIDs** — kept rows are updated in place, not skipped (a pure add/remove diff would never touch the 112 damaged rows) |
| D7 duplicate works | P5 | yes | via backfill |
| D8 no grants / no evidence counts | P6, P3 | yes | via backfill |

---

## 6. Non-goals

- **The 50-publication cap is not under review.** It is deliberate and is retained
  everywhere. Every recommendation here is bounded by `min(50, ceiling)`; the only change is
  that the 50 become the 50 *most recent*, which is what the cap already intends.
- **No re-litigating disambiguation.** No false attributions were found (§2). Do not add
  disambiguation strictness; the measured risk is under-retrieval, and tightening the filter
  makes the actual problem worse. (This is a ban on *tightening the matcher*, not on
  *applying* it: the pipeline path has never disambiguated at all, and §4.3 makes the
  existing matcher mandatory in the shared resolver for every caller.)
- **Do not "fix" Tier E.** `nayar`'s 6 papers and `krieger`'s 10 are correct. A backfill
  that pads them with weak name-matches would convert a correct profile into a wrong one.
- **No Google Scholar.** No API, scraping prohibited, and it adds nothing over
  OpenAlex + PubMed here.
- **Lab websites and CVs are not a publication source** — that assumption is D5.

---

> **Instance policies:** deployment-specific rules layered on this design (JHU-IP
> tenure scoping, consortium exclusion, editorial slot rules, distinctive-surname
> exception) live in `docs/specs/2026-08-13-jhu-instance-rules-design.md` and its
> companion plan. This document stays deployment-agnostic.

## 7. Backfill rehearsal findings (2026-08-13, local DB copy)

The full resolver was rehearsed end-to-end against a restored copy of the prod database
(all 62 PIs, +1,599/−653 rows, ~2,850 NCBI calls) with an attribution check on **every**
added paper (author-list verification against the PI's name and affiliation). Results:
945 additions verified STRONG (name + affiliation), 587 NAME_ONLY (ORCID-anchored
prior-institution papers — expected), and after resolving every flag, **2 of 1,599
additions (0.13%) were genuinely wrong** and 44 were consortium-authorship papers.
Findings that must carry into the implementation:

- **D4b — a multi-hit ESearch DOI lookup is a miss, not a hit.** `convert_dois_to_pmids`'s
  phase-2 fallback (`{doi}[doi]`, take `idlist[0]`) tokenizes unusual DOIs: Daeyeol Lee's
  Research Square preprint DOI (`10.21203/rs.3.rs-1160167/v1`) returned **four unrelated
  PMIDs** and the first was stored — a wrong paper with a clean title. `reconcile_pub_doi`
  does not catch this (it corrects the DOI and keeps the wrong paper). Fix in P1 step 2:
  accept the ESearch result only when `idlist` has exactly one entry, and round-trip
  verify (the PMID's authoritative DOI must equal the queried DOI).
- **S2 (OpenAlex) identity is imperfect: quarantine NO_MATCH additions.** OpenAlex
  attached Rachel Green's ORCID to a psychology paper by *R. Lara Green* (PMID 39533155).
  ORCID-anchored does not mean infallible: every added paper must pass the author-list
  attribution check, and papers where the PI cannot be found among the authors are
  **flagged and withheld**, not silently stored.
- **Editorials consume cap slots.** `dang` (an AACR journals editor-in-chief) resolves to
  a most-recent-50 heavy with verified-but-non-research items (anniversary editorials,
  "Peer Review: Value Added and Civility", journal commentary). `EXCLUDED_TYPES` already
  keeps these out of *synthesis*, but they still displace research papers from the cap.
  The resolver should down-rank PubMed types in `EXCLUDED_TYPES` before capping — same
  decision point as the consortium policy below.
- **Consortium papers need a policy.** 44 additions (N3C for `chute`, GTEx for
  `feinberg`, TOPMed for `salzberg`, PGC for `huganir`, MACS for `margolick`, 4DN for
  `pombo`) list the PI only via a collective/consortium (or, in one case, PubMed's
  author list is an incomplete subset of the journal's). Identity is correct, but these
  are not individual lab output and they consume cap slots. Default: keep, but rank them
  behind individually-authored papers when trimming to the cap — decide before Task 8.
- **Surname variants must not fail verification.** Legitimate papers verify under
  `Müller`/`Mueller` (transliteration), `Van Dang`/`Dang` and `Marie Hardwick`/`Hardwick`
  (PubMed splits compound names unpredictably), and pre-2002 initials-style
  `ForeName="J M"` records. The seeder's `_author_first_name_matches` docstring promises
  initials-style fall-through that its code does not implement (`len > 1` sends "J M"
  down the full-name branch) — harmless for S4 filtering (under-retrieval only) but it
  must not be reused for *verification*, where it false-flags.
- **The guards held.** The below-cap no-removal rule protected `srinivasan` when the
  resolver under-found (3 found vs 10 stored: nothing deleted); `krieger` was untouched;
  Tier D re-slices landed exactly (`salzberg` 2009–2013 → 2022–2026). At-cap Tier E PIs
  were re-sliced toward the most-recent 50 (`coller` ±20, `dang` ±25 …) — consistent with
  the cap's stated meaning but beyond the ledger's scope; the Tier E tripwire in the
  runbook should say "large *unexplained* delta" rather than ±2, or at-cap re-slicing
  should be gated behind an explicit flag.
