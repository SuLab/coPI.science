# PI publication-corpus remediation plan (2026-09-22)

Status: **plan only — nothing applied.** Rev 3. Rev 2 folded in an
adversarial audit that found seven material defects in rev 1 (each marked
**[audit]**). Rev 3 adds Workstream B: **scoping profile generation and every
publication count to the JHU tenure window** (§3 D16–D20, §5B). Measurements
are read-only against production on 2026-09-22.

## 1. How this was measured

- `docker exec copi-blackbird-postgres-1 psql -U copi -d copi` for stored
  state (schema head `0050`).
- `docker compose -f docker-compose.prod.yml run --rm -T blackbird-app python -`
  to *replay* the deployed code against the live ORCID / OpenAlex / NCBI APIs.
- Direct `eutils.ncbi.nlm.nih.gov` and `pub.orcid.org` calls to verify
  individual records by hand.

**[audit] Two caveats on the method itself.** (a) `compose run` uses the
service *image*, which is not provably the image the running containers were
created from; `.build_info.json` inside the one-off container reports
`a031245`, but image-ID parity with the live `blackbird-app`/`worker` was not
checked. (b) That container receives the production `DATABASE_URL` and a
**read-write** `./profiles` mount, so "read-only" is a property of the scripts
that were run, not of the harness. Any future replay must avoid importing
`profile_pipeline` or `profile_export`, which write `profiles/public/*.md` —
the exact path a live agent mtime-watches (D10).

Corpus size: 73 PI accounts, 3,232 `publications` rows owned by PIs (a further
82 belong to 2 managers + 2 admins), 3,013 distinct PMIDs, every row carries a
PMID.

## 2. The headline count

20 of 73 PIs are below 50 rows; 53 sit exactly at 50.

**[audit] Do not read "below 50" as "the resolver found that many."** That
inference is false for most of this population. `resolve_corpus` (whose
`DEFAULT_CAP = 50` is applied last, `corpus.py:374`) only ever ran for the 17
PIs that have a `generate_profile` job. The other 56 were seeded by
`scripts/generate_sparsedata_user.py`, which has an entirely different
truncation regime: `PUBMED_FETCH_CAP = 50` used as the ESearch **`retmax`**
(`:252`, i.e. capped *before* disambiguation), applied again at `:702` and
`:735` on the merged PMID list, plus an ORCID-staleness gate. A sub-50 count
from that script means "ESearch returned ≤50 and disambiguation kept K", not
"K exist". Two further paths cap below 50: the merge branch's
`budget = CORPUS_CAP - len(existing_pubs)` (`profile_pipeline.py:263`), which
is **0** for every PI already at 50 — so those 53 can never grow through the
normal path — and `SEARCH_RETMAX = 200` on S3/S4. Task 2 exists precisely
because the stored count carries no provenance.

**[audit] The `evidence_pub_count` column is not "what reached the model."**
It is `len(pubs_for_synthesis)` (`profile_pipeline.py:458`), but
`_build_synthesis_context` keeps only the newest **30** (`:626-628`) and clips
each abstract to 1500 chars. Read it as a lower bound (the model's own
docstring says so). The column below is relabelled accordingly.

| PI | Pubs | Abstracts offered (`evidence_pub_count`, ≥30 means 30 reached the prompt) | Stored year range |
|---|---:|---:|---|
| Bipasha Mukherjee-Clavin | 3 | 3 | 2025–2026 |
| Utthara Nayar | 8 | 3 | 2017–2024 |
| Kimiko Krieger | 10 | 1 | 2019–2025 |
| Prakash Srinivasan | 10 | 10 | 2008–2025 |
| Richard Markham | 17 | 17 | 2015–2026 |
| **Jeffrey Rothstein** | **18** | 17 | 2019–2026 |
| Anya O'Neal | 21 | 3 | 2019–2026 |
| James Gordy | 25 | 21 | 2012–2026 |
| Thomas Hart | 27 | 5 | 1959–2025 |
| Conor McMeniman | 31 | 19 | 2006–2026 |
| Jennifer Kavran | 32 | 26 | 1998–2026 |
| Ulrich Mueller | 35 | 16 | 1993–2026 |
| Alyssa Coyne | 36 | 30 | 1963–2026 |
| Emma Camacho | 39 | 11 | 2002–2026 |
| Jotham Suez | 40 | 17 | 2012–2026 |
| Monica Mugnier | 41 | 28 | 1968–2026 |
| Anne Hamacher-Brady | 42 | 18 | 2006–2025 |
| Daeyeol Lee | 49 | 30 | 2015–2026 |
| Rachel Green | 49 | 47 → 30 | 2020–2026 |
| Scott Bailey | 49 | 33 → 30 | 1993–2026 |

## 3. Defects, each with its proof

### D1 — Rothstein's corpus was built by the pre-corpus ORCID-only pipeline

`jobs` row `22748033-6b7b-4d46-b795-b4fe5109dc55` (`generate_profile`,
2026-08-24 20:32:55 → 20:33:37, 1 attempt) logged:

```
step3       Fetching publication list from ORCID...
doi_resolve Resolving 20 DOIs to PMIDs...
step4       Fetching abstracts for 19 publications...
```

Today's pipeline logs `step3 Resolving publication corpus (ORCID + OpenAlex +
PubMed)...` / `step4 Corpus resolved: kept N (stages …)`. Corroborating: his
ORCID holds 20 works, **all DOI-only, zero PMIDs**; 17 of his 18 stored rows
are exactly what those DOIs resolve to; and his rows still contain a
preprint/journal duplicate pair (39345637 + 40504117) that `resolve_corpus`'s
normalized-title dedupe collapses.

Replaying `resolve_corpus` today returns:

```
stages  {s1: 20, s2: 358, s3: 29, s4: 175}
kept    50   (cap-limited)
dropped {consortium: 4, excluded_type: 18, identity: 73, duplicate_title: 5}
```

18 stored vs 50 available, and **only 8 of the 50 are already stored**. He is
an **active** agent with the thinnest corpus on the instance.

Cohort: of the 17 PIs with a job, 10 ran the corpus pipeline (2026-08-25 →
09-02) and **7 ran the legacy path** — Mukherjee-Clavin, Rothstein, Kavran,
Rebecca, Jiou Wang, Erika Pearce, Anthony Leung. The other 56 have no job row;
their provenance must be established by replay (Task 2).

### D2 — the identity gate rejects PIs from their own papers

`_author_first_name_matches` (`corpus.py:115-116`) treats any `ForeName` whose
de-punctuated length exceeds 1 as a full given name and requires
`fore_lower.startswith(expected_first.lower())`. PubMed routinely stores the
forename as spaced initials.

**(a) Initials-as-ForeName.** All **70 of 70** records `resolve_corpus` flags
for Rothstein as `no_individual_author_match` contain an author with surname
"Rothstein" and `ForeName='J D'` — each refetched and checked. They include
PMIDs 2375630 (1990), 1349424 (1992), 7611729 (1995), 8785064 (1996), 9052802
(1997), 9539131 (1998) — his glutamate-transporter and ALS landmarks.

Same mechanism elsewhere: `Kavran 'J M'`, `Scott 'A L'`, `McMeniman 'C J'`,
`Feinberg 'A P'`, `Pearce 'E L'`, `Kevrekidis 'I G'`, `Janak 'P H'`,
`Margolick 'J B'`, `Rebecca 'V W'`, `Bailey 'S T'`, and **50 of the 50
measured** rows for J. Marie Hardwick (`ForeName='J Marie'`, compounded
because her stored first name is the token `"J."`, so
`"j marie".startswith("j.")` is False).
**[audit]** Stated as "50 of 50 measured", not "all": a record with no
`ForeName` element at all skips that branch entirely and matches on
`initials[0]` (`corpus.py:120-121`).

**[audit] A wider hole in the same function.** `corpus.py:154` sets
`first_name = parts[0] if len(parts) > 1 else ""` and `:164` only calls the
forename check `if first_name`. A PI whose `users.name` is a **single token**
gets no forename discrimination at all, on every stage — pure surname match.
No current PI has a single-token name, so this is latent, not live.

**(b) No diacritic folding.** PubMed spells Ulrich Mueller **"Müller"**
(verified on PMID 27798175, affiliation Scripps Research — he moved to JHU in
2017). `corpus.py:161` compares raw lowercased surnames, so 12 of his real
papers fail.
**[audit] Rev 1 claimed `patents.py::_prepare` already holds the fix. It does
not.** `_to_ascii` (`patents.py:146-166`) is NFKD + a Greek/dash table +
combining-mark strip: `"Müller"` → `"Muller"`, **not** `"Mueller"`. Accent
stripping and the German `ü→ue` expansion are *incompatible* folds; no single
normalisation yields both. See Task 1 item 3 for the corrected spec.

**(c) No compound / punctuated surname handling.** Chi Dang is indexed
**"Van Dang, Chi V"** → 5 rejections. Anya O'Neal's 34140472 is indexed
`LastName="Neal"`, `ForeName="Anya J O'"` → rejected.

**Scale, from the completed 73-PI replay** (72 resolved, 1 hard error — D11).
The identity gate withholds **1,637 records** in total. Worst: J. Marie
Hardwick 152, Chi Dang 120, Drew Pardoll 88, Andrew Feinberg 86, Christopher
Chute 82, Alan Scott 72, Edward Pearce 69, Barbara Slusher 40, David Sullivan
25, Douglas Norris 23.

**The starkest single result: `resolve_corpus` returns ZERO publications for
J. Marie Hardwick.** 152 candidates, every one withheld, because every record
spells her `ForeName='J Marie'` against a stored first name of `"J."`. A
`generate_profile` re-run for her today would resolve an empty corpus. Her 50
stored rows survive only because the merge branch never deletes (D8) — the
one time that conservatism has paid off.

**Aggregate.** Today's `match_pi_author` over all 3,232 stored rows: **94
fail**. A corrected matcher (folding + initials-aware + compound-aware) over
the same rows: **3,180 strong, 47 initials-only, 4 no-surname-author, 1
apparent conflict** — and the conflict is a false alarm (`Kevrekidis, Yannis`
is Ioannis). So nearly all 94 are the gate being wrong, not the data.

### D3 — the mirror defect: a bare initial is accepted with no corroboration

A single-letter `ForeName` matches any first name sharing that letter, and the
affiliation check fires only when `rec_stages == {"s4"}` **exactly**
(`corpus.py:333`) — so `{"s2"}`, `{"s2","s4"}` and `{"s2","s3"}` fall straight
through to `kept.append`. 47 stored rows rest on a bare initial. Hand-verified:

- **38766182** (Mugnier) — authors Lovell, Duque, Rousseau, Bhalodia, Bermea,
  Cohen, Adamo. **No Mugnier.** Wrong row.
- **36284789** (Rothstein) — authors Cossu, Atkins, Hajdu, Puccinelli, Daniel,
  Messerer. **No Rothstein.** Wrong row, and the one stored PMID absent from
  his ORCID set — consistent with the pre-D4b DOI misresolution.
- Six pre-1990 records, near-certainly other people: Coyne 14093231 (1963,
  "COYNE A"), Mugnier 4299961 (1968, "Mugnier M"), Hart 13661353 / 5505987 /
  6431915 / 3904977 ("HART T M" / "Hart T"). I verified the *match basis*, not
  the negative — high-confidence removals pending one human glance.
- Seven Ulrich Mueller rows matched as `"Mueller, U W"` (cyanobacterial psbB,
  Ran-binding protein) or `"Mueller, U"` (fission-yeast spi1p/Dis3) — a
  different Mueller in a different field, while his real work is rejected by
  D2(b). One of the seven, 15976301 (neuregulin receptors, Science 2005), is
  plausibly genuinely his; it is a judgement call, not a mechanical one.

Coupling: `derive_start_from_papers` dates tenure from the earliest paper
carrying the PI's own Hopkins affiliation, so a mis-attributed old paper with
a Hopkins string would silently backdate a tenure window. Coyne's 2018 year is
`earliest_hopkins_paper`-derived and escaped only because 1963 records carry
no affiliation at all.

### D4 — 159 non-research rows occupy cap slots

By `pub_types` against `EXCLUDED_TYPES`: 61 comment, 48 published erratum, 28
editorial, 24 letter, 10 news, 4 biography. Today's resolver drops these
*before* the cap. Worst: Casadevall 11, Agre 9, Sullivan 6, Dang 6, Pombo 6,
Hardwick 6.
**[audit]** The rule is a set *intersection* (`corpus.py:315-316`), so a record
carrying a secondary `Comment` type alongside `Journal Article` is dropped
too. The 61 `comment` rows must be split on that before any of them is deleted
(Task 4).

### D5 — 37 duplicate-title groups

Preprint + journal versions stored twice (Rothstein 39345637/40504117; Melissa
Conrad 7 pairs; Mugnier 2, Coppens 2, Green 2, Sinnis 2, …).

### D6 — Rothstein has no tenure-start entry

The only PI of 73 without `app_settings['jhu_tenure_start:{id}']`.
`tenure_filter` returns its input unchanged when `start is None`
(`jhu_rules.py:65-67`), so his profile is full-career scope; per CLAUDE.md his
RePORTER grants render `org_only` and his industry score `Unscored`.

### D7 — thin synthesis is partly by design

Krieger (tenure 2025, curated) → **1** abstract; O'Neal (2025) → 3;
Mukherjee-Clavin (2025) → 3; Nayar (2021) → 3; Hart (2025) → 5; Camacho (2023)
→ 11. The tenure filter is working as specified. A persona built on one
abstract is still a quality risk. Owner decision (Task 7), not a code fix.

### D8 — a plain re-run cannot repair a contaminated legacy corpus

`run_profile_pipeline`'s existing-corpus branch never deletes
(`profile_pipeline.py:246-286`), stores only `new_recs` whose stages intersect
`{s1,s3}`, routes S2/S4-only finds to a review log, and budgets
`CORPUS_CAP - len(existing_pubs)`.

**[audit] Rev 1's "18 → about 33" was wrong.** Measured: of today's 50 kept
records only **8** are already stored, and 15 carry an S1/S3 anchor. `new_recs`
excludes anything already stored, so the merge path can add only the
*anchored-and-new* subset — smaller than 15, since the overlapping 8 are
themselves mostly S1-anchored. Budget (50−18 = 32) is not the binding
constraint; the anchor requirement is. Either way the conclusion holds and is
stronger than rev 1 stated: **pressing "refresh" leaves ~30 of the 42 missing
papers unstored, and removes neither 36284789 nor the duplicate.**

**[audit]** `existing_pubs` is `{p.pmid: p ... if p.pmid}`
(`profile_pipeline.py:202`), so `len()` under-counts on a NULL or duplicated
PMID and the corpus can exceed the cap. Only non-unique indexes exist on
`publications.pmid`/`user_id` (`0001_initial.py:121-122`), so §4's "no
duplicates, every row has a PMID" is **observed, not enforced** — the repair
script may not rely on it.

### D9 — the low DOI→PMID yield is not a defect (cleared)

Classified by registrant prefix rather than assumed. Nayar's 93 misses: **79**
are `10.1158/1078-0432.2247xxxx` AACR figure/supplementary-data DOIs, deposited
twice each (bare + `.v1`), plus 6 Figshare (`10.6084`), 3 bioRxiv (`10.1101`),
2 `10.1200`, 2 `10.1182`, 1 `10.1186`. Krieger's 22: 21 AACR, split between
that figure pattern and meeting abstracts (`10.1158/1538-7445.am2023-…`).
None are PubMed-indexed research articles, so a miss is correct. Lesson: **an
OpenAlex work count is inflated by figures, supplements, versions and meeting
abstracts** — 103 OpenAlex works for Nayar is not 103 papers, and must never
be used as a coverage target.

**[audit] The sub-claim that `build_pubmed_query` is fine is NOT established.**
Rev 1 compared `Rothstein Jeffrey[Author] AND "Johns Hopkins University"[Affiliation]`
(175) against `Rothstein JD[Author]` (354, **unfiltered**) — different name
form *and* different filter. The controlled query was never run, and D2(a)'s
own evidence cuts the other way: those 70 records are indexed `Rothstein J D`,
which `Rothstein Jeffrey[Author]` does not match. Task 9 is reopened as a
one-ESearch check.

### D10 — regenerating a profile reaches a running simulation

`_sync_profiles_from_disk` (`simulation.py:8429-8461`) is called
unconditionally in the main loop body (`:1052`, no cadence gate — contrast
`_poll_control_plane` at `:1049`), stats
`profiles/public/{agent_id}.md`, and calls `agent.reload_profiles()`, which
nulls the cached profile so the next prompt build re-reads disk.
`export_profile_to_markdown` writes exactly that path
(`profile_export.py:11,114`). The **worker** — which runs the job — shares the
mount read-write (`docker-compose.prod.yml:87-89`), matching the agent's
(`:115`). So a regeneration swaps a live agent's persona within one tick.

**[audit] Conversely, Task 4's database writes are invisible to a live run:**
nothing under `src/agent/` imports `Publication`. Only Task 5's *export*
reaches the engine.

**[audit] The "a run is in flight right now" reading was over-stated and is
now stale.** At 14:37 the row read `state='running'` with a 144-second-old
heartbeat — but `HEARTBEAT_STALE_SECONDS = 120`
(`simulation_control.py:26,154`), so `derive_panel_state` would have returned
`stale`, and `POST /admin/simulation/stop` refuses outright unless
`panel_state == "running"` (`admin.py:2726-2730`). Run `75ca77f9` has since
ended (14:08:53 → 15:01:55, 304 API calls) and the supervisor now reports
`idle` with a 4-second heartbeat. Both facts matter for Task 0: the window is
currently open, and the Stop button is unavailable exactly when a long
`thread_reply` turn is in flight.

### D11 — one PI's profile cannot be regenerated today

Replaying Daeyeol Lee raises
`CorpusStageError: corpus stage s1_orcid_works failed: 'NoneType' object has
no attribute 'get'`. `orcid.py::fetch_orcid_works` does
`summary.get("external-ids", {}).get("external-id", [])`; `dict.get(k, {})`
returns the default only when the key is **absent**, and 15 of his 155
work-summaries carry `"external-ids": null` (put-codes 12725389, 12725390,
12725393, …). The same `.get(k, {})` idiom is used for `title`,
`title.title` and `publication-date.year.value`. The function's `try/except`
wraps only the HTTP call, so the error escapes → `CorpusStageError` →
`run_profile_pipeline` fails → the job retries 3× and goes **dead**. I scanned
all 73 PI ORCID records: he is the only one affected today, but it would fire
during Task 5.

### D12 — `_aff_match` is far too loose to serve as corroboration **[audit]**

`_distinctive_aff_tokens("Johns Hopkins University")` yields
`['johns', 'hopkins']` (`university` is a stopword, `corpus.py:63-80`), and
`_aff_match` is a plain substring OR (`:92`). Verified: it returns True for
`"Robert Wood Johnson Medical School, New Brunswick NJ"`, for
`"Hopkins Marine Station, Stanford University"`, and for `"Johnson & Johnson"`.
`jhu_rules.is_hopkins_affiliation` (`jhu_rules.py:41-55`) does this correctly
with full phrases and word-bounded acronyms; `corpus.py` does not use it. This
blocks Task 1 item 5, which leans on `_aff_match` as its containment half.

### D13 — book/chapter PMIDs vanish silently **[audit]**

`_parse_pubmed_xml` iterates `root.findall(".//PubmedArticle")`
(`pubmed.py:300`) and never matches `PubmedBookArticle`, so a book-record PMID
produces no record at all rather than a classified one. Any
"resolved N, stored M, difference X" arithmetic silently absorbs these.
(The related worry that `.//Author` scoops reference-list or investigator
authors is **refuted**: references carry `Citation` + `ArticleIdList` with no
`Author`, and investigators are `<Investigator>`.)

### D14 — NCBI pacing is per-process, not per-host **[audit]**

`_next_slot` is a module global and `_pace` spaces starts *within one process*
(`pubmed.py:94,101-115`). A replay container, the worker running
`generate_profile`, and a live agent's `fetch_abstract` are three processes
with three independent 3 req/s pacers against one NCBI IP — and this host also
carries org1's production stack, so a block is not contained to this
deployment. `ncbi_api_key` defaults to `""` (`config.py:117`), i.e. the
keyless 0.34 s interval. Setting it is a **precondition**, not an optimisation.

### D15 — regeneration downgrades `grant_titles` **[audit]**

`profile_pipeline.py:444` is
`profile.grant_titles = grant_titles or profile.grant_titles`, where
`grant_titles` is the **ORCID fundings** list (`:110`). `execute_enrich_grants`
writes the RePORTER-derived list to the same column
(`grant_enrichment.py:123`), *replacing* rather than supplementing it —
contrary to what CLAUDE.md says. So a Task 5 regeneration overwrites RePORTER
titles with ORCID fundings and exports the downgraded list to
`profiles/public/{agent_id}.md` (`profile_export.py:107-112`) **before** the
follow-on `enrich_grants` job runs (`profile_pipeline.py:548-550`). If that job
then hits `no_reporter_match`, the downgrade is permanent. Vetoes survive.

---

## 3B. Tenure-scoping defects (rev 3)

Requirement: **only papers inside the JHU tenure window may reach profile
generation, the exported persona, or any publication count.** Today the rule
exists (`jhu_rules.tenure_filter`, design R2) but is applied at two of four
export sites and at zero of the count sites.

### The load-bearing property: read-side scoping is sufficient and lossless

`resolve_corpus` ranks `year DESC, pmid DESC` and applies the cap **last**
(`corpus.py:368-374`). In-tenure papers are by definition newer than
out-of-tenure ones, so they occupy the top of that ranking. Therefore:

- if a PI has ≤50 in-tenure papers in the candidate pool, **all** of them are
  in the stored top-50; the out-of-tenure rows are pure surplus;
- if a PI has >50, all 50 stored rows are already in-tenure.

**Filtering before the cap and filtering after the cap yield the identical
in-tenure set.** So the tenure requirement needs *no* re-resolve and *no*
deletion — only consistent read-side scoping. (Undated papers sort to year 0
and are cut by the cap first, which is the same direction `tenure_filter`
takes.)

Two caveats. (i) The proof covers only corpora produced by `resolve_corpus` —
the 17 job-bearing PIs. The 56 script-seeded ones came from
`generate_sparsedata_user.py`'s different regime, so their stored set may be
missing in-tenure papers that exist; that is what Task 2 measures. (ii) A
prolific PI can lose in-tenure candidates to `SEARCH_RETMAX = 200` on S3/S4
before ranking ever happens.

**Consequence for the design: do not delete out-of-tenure rows.** Full-career
storage is what lets a corrected tenure year re-widen the view instantly
without a re-fetch (`profile_pipeline.py:195-199`, JHU R2) — and D18 shows
several years are wrong. Deleting would convert a curation error into
permanent data loss.

### Scale

Of 3,232 stored PI rows: **2,660 in-tenure, 550 before tenure start, 4 undated
with a tenure set, 18 belonging to a PI with no tenure entry** (Rothstein).
So tenure-scoping the counts moves ~17% of the corpus out of view.

### D16 — SIX of the eight export sites do not tenure-filter (LIVE)

The persona file `profiles/public/{agent_id}.md` is written from eight places.
**Two filter; six do not.**

| Site | Trigger | Filtered? |
|---|---|---|
| `profile_pipeline.py:572-580` | `generate_profile` job | ✅ |
| `profile_edit.py:94-107` | `POST /manager/pis/{id}/profile` | ✅ |
| **`agent_page.py:1005-1012`** | `POST /agent/{agent_id}/public-profile/save` (PI-facing) | ❌ |
| **`onboarding.py:210-216`** | `POST /onboarding/save-profile` (PI-facing) | ❌ |
| **`manager.py:419-422`** | the post-veto re-export helper, fired by `POST /manager/pis/{id}/grants/{gid}/veto` and `…/industry/{eid}/veto` | ❌ |
| **`scripts/audit_pub_dois.py:159-165`** | DOI-repair re-export | ❌ |
| **`scripts/backfill_agents.py:120-122`** | agent backfill | ❌ |
| **`scripts/generate_sparsedata_user.py:632-634`** | the script that seeded 56 of the 73 PIs | ❌ |

Both filtered sites carry the same comment — *"an unfiltered top-20 is exactly
how pre-tenure papers reached 9 agents' prompts on 2026-08-14 (audit H3)"* — so
this is a known defect class fixed in two places and missed in six.
`export_profile_to_markdown` writes a `## Recent Publications` section of up to
20 entries (`profile_export.py:76-85`), and the engine reloads that file within
one tick (D10).

**Measured: the on-disk personas are clean today.** I parsed the
`## Recent Publications` years out of all 75 `profiles/public/*.md` and
compared each against its PI's tenure year: **0 of 72 scoped personas contain
a pre-tenure paper** (the 73rd is Rothstein, who has no year — D6). Pombo's
file lists exactly her 9 in-tenure papers, Shastri's 11, Coller's 20, Hart's
5. So this is a **latent** defect, not a live leak, and rev 3's first draft
over-claimed it.

The reason they are clean is not that the code enforces it.
`profile_revisions` shows a one-off remediation pass — mechanism `pipeline`,
change summary **"2026-08-14 JHU-IP scoping: publication list"** — that
re-exported every persona through the filtered path after the 2026-08-14
audit. None of the three `scripts/` sites mentions tenure at all
(`grep -n tenure` returns nothing in any of them). The window is held by a
past manual pass, not by an invariant.

Consequences, worst first:

1. **`manager.py:419-422` silently reverts the fix on the next click.** A
   manager vetoing a grant or an industry-evidence row re-exports the persona
   *unfiltered* as a side effect. For Pombo that would take her persona's
   publication list from 9 in-tenure entries to a top-20 containing 11
   pre-tenure ones, and the running engine picks it up within a tick (D10).
   Any repair this plan makes is undone by an unrelated staff action until
   this is fixed. This is the single highest-value line in Workstream B.
2. A PI saving their own profile, or finishing onboarding, does the same to
   their own agent.
3. Re-running `backfill_agents.py` or `audit_pub_dois.py --apply`, both of
   which exist to be re-run, does it in bulk.

### D17 — every publication count and list on the UI is full-career

| Surface | Code | Scoped? |
|---|---|---|
| `/manager/pis`, `/admin/users` count column | `directory.py:170-175` (`func.count`) | ❌ |
| `/manager/pis/{id}` list + `Publications (N)` | `directory.py:246-251`, `manager/pi_detail.html:337` | ❌ |
| `/admin/users/{id}` list + `Publications (N)` | `directory.py:246-251`, `admin/user_detail.html:125` | ❌ |
| PI's own `/profile` | `profile.py:65-70`, `profile/view.html:147-175` | ❌ |
| `evidence_pmid_count` / `evidence_pub_count` | `profile_pipeline.py:456-458` | ✅ (post-filter) |
| Grants panel, industry score | `grant_enrichment.py:54`, `industry_evidence.py:65,130,155` | ✅ |

The synthesis input itself is already correct (`profile_pipeline.py:311`,
`in_tenure = tenure_filter(corpus_records, tenure_start)`), so "profile
generation" is scoped **except** through the two D16 export sites.

### D18 — tenure years become load-bearing, and several are questionable

Scoping counts to the tenure year turns a curation error into visible data
loss. Current in-tenure / total, worst first:

| PI | Tenure | In-tenure / total | Source |
|---|---:|---|---|
| Ana Pombo | 2024 | 9 / 50 | curated |
| Nilabh Shastri | 2018 | 11 / 50 | curated |
| Jeff Coller | 2020 | 20 / 50 | curated |
| Mikala Egeblad | 2023 | 22 / 50 | curated |
| Jane Carlton | 2023 | 23 / 50 | curated |
| Nicole Baumgarth | 2022 | 23 / 50 | curated |
| Emma Camacho | 2023 | 12 / 39 | curated |
| Melissa Conrad | 2025 | 26 / 50 | curated |
| Carl Wu | 2017 | 27 / 50 | curated |
| Thomas Hart | 2025 | 5 / 27 | curated |
| Anya O'Neal | 2025 | 3 / 21 | curated |
| Kimiko Krieger | 2025 | 1 / 10 | curated |

Some are certainly right (Coller moved to JHU in 2020; Carl Wu ~2017). Others
need a human check before their count collapses in public — and note that
Coyne's 2018 is `earliest_hopkins_paper`-derived, i.e. derived from a corpus
that D3 shows contains mis-attributions, so the derivation source is itself
suspect for anyone scored that way (Coyne, Slusher, Yarchoan, Pardoll,
Vogelstein, Semenza).

### D19 — undated papers disappear under scoping

`tenure_filter` excludes `year IS NULL` when a start is set
(`jhu_rules.py:65-70`) — "an unknown year cannot prove the paper is
in-tenure". That is the right default, but it silently drops 4 rows (Gill 2,
Kavran 1, Perrin 1). Under counts-scoping they vanish with no explanation.

### D20 — a PI with no tenure entry is scoped to *everything*

`tenure_filter` is the identity when `start is None`. So under a literal "only
tenure-period papers" rule, Rothstein's 18 rows are currently all counted with
no window at all. **DECIDED by the owner, 2026-09-22: show the full career,
badged as unscoped.** So `tenure_start is None` is a pass-through everywhere,
and every count/list surface must render an explicit "full career — no JHU
tenure start recorded" badge instead of an unqualified number. D6 (set
Rothstein's year) removes the only live instance, but the badge stays as the
defined behaviour for every PI added later.

## 4. What is NOT wrong (checked, so nobody re-chases it)

- No duplicate `publications` rows by PMID; every row has a PMID; 12 lack a
  DOI, 4 lack a year, 183 lack an abstract. **Observed, not enforced** (D8).
- PMIDs shared across up to 4 PIs are genuine JHU co-authorship.
- Every PI has a `researcher_profiles` row with `synthesis_validated = true`.
- `Kevrekidis, Yannis` is Ioannis Kevrekidis; not a mis-attribution.
- `.//Author` does not pull reference-list or investigator authors (D13).

## 5. Plan

Two workstreams. **A (Tasks 0–9)** fixes attribution: which papers are a PI's.
**B (Tasks 10–15)** enforces the tenure window on profile generation and on
every count. They interleave at three points, and the order is not negotiable:

1. **B10 (the export leak) ships first and alone** — it is a 4-line fix to a
   live persona-contamination bug and depends on nothing else.
2. **A1–A4 (matcher + corpus repair) before B12 (tenure verification)** —
   verifying a tenure year against a corpus that contains other people's
   papers (D3) verifies the wrong thing; `earliest_hopkins_paper`-derived
   years are literally computed from it.
3. **B12 before B13 (scoping the counts)** — scoping to an unverified year
   collapses ten PIs' public counts on curation errors (D18).

**Do not regenerate any profile before the matcher is fixed**, or the repair
bakes today's rejections into new personas.

### Workstream A — attribution

### Task 0 — freeze, precondition, snapshot (blocking)

1. **Set `ncbi_api_key` in `.env`** and recreate the worker
   (`$DC up -d --force-recreate worker`) before any replay or regeneration
   (D14). `.env` changes need a *recreate*, not a restart.
2. Confirm with the owner whether the simulation must be down for Tasks 4–5.
   Run `75ca77f9` ended 15:01:55 and the supervisor is `idle`, so the window is
   open now. If a run is live: prefer `/admin/simulation` → Stop, **but**
   `POST /admin/simulation/stop` refuses when the heartbeat is >120 s old
   (`admin.py:2726-2730`), which is routine during a multi-minute
   `thread_reply`. **Fallback: `docker stop -t 420 copi-blackbird-agent-1`** —
   correct per CLAUDE.md, loses nothing (`stop_grace_period: 420s`), and
   `restart: unless-stopped` does not resurrect a container stopped that way.
3. `pg_dump -Fc` to `~/backups-blackbird/`; record the digest here.
4. Snapshot the worklists to `docs/audits/2026-09-22-pi-corpus/` as CSV:
   `no_match_94.csv`, `initials_only_47.csv`, `excluded_types_159.csv`,
   `duplicate_titles_37.csv`.

### Task 1 — fix the author matcher

Each item needs a test built from a real production record. **Run
`.venv-test/bin/python -m pytest tests/unit/test_corpus.py -v` after every
item** — items 2 and 5 are adjacent to pinned behaviour.

1. **Spaced-initials forename → compare on the first initial.** When every
   token of `ForeName` is a single letter (`"J D"`, `"A L"`, `"J M"`), match
   on the initial rather than `startswith`. Fixture: PMID 8785064 vs
   "Jeffrey Rothstein".
2. **Initial-plus-name forename.** **[audit] Rev 1's wording would have broken
   CI and re-opened the defect this module exists to stop.**
   `tests/unit/test_corpus.py:62-69` pins `Green, "R Lara"` ≠ "Rachel Green";
   a blanket "compare on the first initial" rule matches it. Correct spec:
   apply this path **only when the expected first name is itself an initial**
   (`"J."` in "J. Marie Hardwick"), and additionally require the forename's
   non-initial token to equal the expected middle name. Fixture: PMID 40874741
   vs "J. Marie Hardwick"; negative fixture: the pinned `R Lara` case.
3. **Surname folding.** **[audit] Rev 1's "reuse `patents.py::_prepare`" was
   wrong** — `_to_ascii` gives `"Müller"` → `"Muller"`, not `"Mueller"`.
   Correct spec: compare a *set* of variants on each side — raw,
   NFKD-accent-stripped (reusing `_to_ascii` for that half only), and the
   German expansion (ü→ue, ö→oe, ä→ae, ß→ss). State explicitly whether
   expected `"Mueller"` also generates `"Muller"`; it should **not**, or
   Hermann Joseph *Muller* becomes a match. Fixture: PMID 27798175 vs
   "Ulrich Mueller".
4. **Compound / punctuation-mangled surnames.** Last-token equality covers
   `"Van Dang"` → Dang (fixture PMID 33482124). **[audit] It does not cover
   O'Neal**: last-token of `"Neal"` is `"neal"`, and apostrophe-stripping
   `"o'neal"` gives `"oneal"` — neither matches. 34140472 needs a *separate*
   operation: detect a `ForeName` ending in a name particle (`"Anya J O'"`),
   splice it onto `LastName`, then re-derive the first-name check from the
   remainder (`"Anya J"`). That has its own over-match surface and needs its
   own negative test. Treat it as a distinct item, not a footnote to
   last-token equality.
5. **Require corroboration for a bare-initial match** — own-affiliation match
   OR an S1/S3 anchor — on *every* stage combination, not just
   `rec_stages == {"s4"}`.
   **[audit] Two blockers before this can ship.** (i) It cannot use
   `_aff_match` as-is (D12); fix that first, preferably by delegating to
   `jhu_rules.is_hopkins_affiliation`'s phrase/acronym approach. (ii) After
   item 1, `"J D"` *is* a bare-initial forename, so this rule re-drops exactly
   the records items 1–4 recover: pre-~1988 papers carry no affiliation at all,
   S3 (`{orcid}[auid]`) cannot exist for a pre-ORCID record, and S1 exists only
   if the PI curated it. Whether Rothstein's 1990s landmarks survive depends on
   whether they carry an S3 anchor — **measure that before implementing**, and
   if they do not, the corroboration set needs a third member (e.g. journal +
   subject-area continuity with the PI's known corpus, or explicit allow-listing
   by the repair script's human review).
6. **Null-safe ORCID parsing** (D11): every `summary.get(k, {})` chain in
   `fetch_orcid_works` becomes `(summary.get(k) or {})`. Fixture: a
   work-summary with `"external-ids": null` from `0000-0003-3474-019X`.
7. **Parse `PubmedBookArticle`** or explicitly count and report unparsed PMIDs
   (D13), so no downstream arithmetic silently absorbs them.

Verification: targeted pytest, then the full `./scripts/ci.sh`.

### Task 2 — provenance + the measurement instrument

**[audit] The before/after instrument in rev 1 was invalid.** Re-classifying
the 3,232 *stored* rows cannot detect over-matching, because false positives
appear in the **unstored candidate pool** (for Rothstein alone: 73
identity-dropped records against `stage_counts {s2: 358, s4: 175}`). The
correct instrument is a per-PI `resolve_corpus` run before and after Task 1,
diffing `dropped["identity"]` and the kept sets.

**The read-only 73-PI baseline replay is complete** (72 resolved, 1 hard
error — D11). Headline numbers, which are the sizing input for Tasks 3–4:

- **1,637** records withheld by the identity gate (D2).
- **245** papers the resolver finds that production does not store;
  **313** stored rows the resolver no longer returns.
- **19** PIs resolve below the cap today — led by **Hardwick at 0**,
  Srinivasan 4 (vs 10 stored), Mueller 18 (vs 35), Nayar 10 (vs 8).
- Rothstein: 18 stored → 50 resolved, 42 of them new.
- Several PIs resolve *fewer* than they store (Mueller 18 vs 35, Bailey 43 vs
  49, Suez 36 vs 40) — a mix of the identity gate (D2), excluded types (D4)
  and duplicate-title collapses (D5), which is why Task 3 needs per-row
  evidence rather than a set difference.

Re-run this after Task 1 with the API key set (Task 0.1) and diff against the
baseline. The delta in `dropped["identity"]` and in the kept sets is the
over-match alarm; the baseline is retained for that comparison.

### Task 3 — `scripts/repair_pi_corpus.py`, dry-run by default

**[audit] It needs an evidence step rev 1 omitted.** `publications` stores no
`pub_types` and no author list (`src/models/publication.py`), and
`resolve_corpus` only efetches PMIDs some stage proposed
(`corpus.py:293-297`) — so a *stored-but-not-resolved* row, which is exactly
the Batch A class, is never fetched and yields no evidence. Add an explicit
**refetch of all 3,232 stored PMIDs**, paced under Task 0.1.

Then, per PI, `--apply` required, `--orcid`/`--only` scoping, mirroring
`scripts/enqueue_enrichment.py`:

- **Remove**: rows failing the *fixed* strict re-check; rows whose
  `pub_types` hit `EXCLUDED_TYPES`; the later member of each duplicate-title
  group (higher year, then higher PMID — `resolve_corpus`'s own rule).
- **Add**: the resolver's kept set up to the cap, ranked year-DESC/PMID-DESC.
- **Never** auto-drop a human-verified row; print every removal with its
  evidence (author list, pub types).
- Per-PI markdown report into `docs/audits/2026-09-22-pi-corpus/`.
- **[audit] Do not model this on `scripts/generate_sparsedata_user.py`** — it
  is destructive (`delete(Publication).where(user_id == ...)` at `:553`).

Judgement calls — the 47-row initials-only list minus the provable members —
go to a `--review` report, never the automatic set.

### Task 4 — build, then apply in reviewed batches

**[audit] Rev 1 had no build step, and the obvious invocation would have run
the OLD matcher.** `src/` is baked into the images (`Dockerfile:13,17`), and
`$DC exec -T blackbird-app python scripts/…` executes the *running* container's
pre-fix `match_pi_author` — the same trap CLAUDE.md documents for alembic. So:

```
DC="docker compose -f docker-compose.prod.yml"
$DC build blackbird-app worker         # worker: it runs generate_profile (Task 5)
$DC run --rm blackbird-app python scripts/repair_pi_corpus.py            # dry-run
$DC run --rm blackbird-app python scripts/repair_pi_corpus.py --apply    # per batch
$DC up -d blackbird-app worker
```

`run --rm`, never `exec`. The **agent** image needs no rebuild for this change
(nothing under `src/agent/` imports `corpus` or `profile_pipeline`), but
rebuild it anyway for image/tree parity per CLAUDE.md, and say so in the
deploy note rather than leaving the question open. No migration is involved.

1. **Batch A** (2 rows, proven): delete 36284789 from Rothstein, 38766182 from
   Mugnier.
2. **Batch B**: 37 duplicate-title collapses, and the excluded-type removals —
   **[audit] first measure how many of the 61 `comment` rows also carry
   `Journal Article`** and route those to `--review`, since this batch is the
   largest deletion and the 62-PI audited cohort's standing rule is "never
   delete".
3. **Batch C**: the 7 legacy-ORCID-only PIs plus any PI Task 2 shows materially
   under-resolved. These need the script's add-path, not `POST /profile/refresh`
   (D8).
4. **Batch D**: the initials-only review list, one human pass.

Re-run the Task 2 delta after each batch.

### Task 5 — regenerate profiles and exports

Only after Task 4, and only on a rebuilt worker.

- **[audit] Guard `grant_titles`** (D15): either snapshot and restore the
  RePORTER-derived list around the regeneration, or enqueue `enrich_grants`
  immediately after and accept a window, or fix
  `profile_pipeline.py:444` to not overwrite a RePORTER-sourced value. Decide
  before running, not after.
- **[audit] Task 5 is not a pure re-synthesis** — `run_profile_pipeline` calls
  `resolve_corpus` and the add-path before synthesising. If Task 3 leaves a PI
  below the cap, the pipeline re-adds S1/S3-anchored records, possibly
  including one Batch D removed by human judgement. The two add-paths use
  different admission rules, so the outcome is order-dependent. Either hold the
  invariant "the script leaves every repaired PI at the cap" or add a
  no-additions mode.
- Expect `profile_version` bumps and new `profile_revisions` — that is the
  audit trail. Re-check `evidence_pub_count` afterwards.

### Task 6 — set Rothstein's tenure start — RESOLVED, no manual entry needed

Measured 2026-09-22: his ORCID holds exactly one employment —
`Johns Hopkins University`, role "Director, Brain Science Institute",
**start 1986, no end date**. So `derive_employment_start` returns **1986**
(current ✓, start_year ✓, `is_hopkins_affiliation("Johns Hopkins University")`
✓) and `run_profile_pipeline` persists it automatically with
`source="orcid_employment"` on his next `generate_profile` — the tenure block
runs before synthesis, so no separate step and no manual form entry is needed.

Two consequences: a 1986 window excludes nothing he has (his stored corpus
starts 2019, the repaired one starts 2021), so scoping his counts costs him no
papers; and it clears the only PI currently on the D20 unscoped-badge path.

### Task 7 — owner decision on thin-by-design profiles

For the six PIs whose tenure window leaves ≤5 abstracts (D7): accept, widen
the window for recently-arrived faculty, or mark the profile low-grounding in
the manager UI. No code change until decided.

### Task 8 — guardrails

- `scripts/audit_pi_corpus.py` (read-only, the §3 classification **plus** the
  Task 2 resolve delta) alongside `scripts/audit_pub_dois.py`.
- Surface corpus size, the **corrected** grounding figure
  (`min(evidence_pub_count, 30)` — D-§2) and the no-match count on
  `/manager/pis/{id}`.
- Record in CLAUDE.md: `POST /profile/refresh` **cannot** repair a
  contaminated corpus (D8); an OpenAlex work count is not a paper count (D9);
  NCBI pacing is per-process and this host is shared (D14).

### Task 9 — reopened **[audit]**

Run the one controlled ESearch rev 1 skipped:
`Rothstein JD[Author] AND "Johns Hopkins University"[Affiliation]` against
`Rothstein Jeffrey[Author] AND "Johns Hopkins University"[Affiliation]`. If the
initials form returns materially more, `build_pubmed_query` (`corpus.py:130-134`)
systematically cannot retrieve initials-indexed records, which is fatal for
sparse-ORCID PIs — the same cohort Task 1 item 5 penalises.

### Workstream B — tenure scoping

The governing decision, to be confirmed by the owner before B13:
**scope at read, never at storage.** Storage stays full-career (JHU R2), and
the tenure window becomes a filter every read path applies through one shared
helper. Rationale in §3B: read-side scoping is provably lossless for
resolver-built corpora, and it keeps a wrong tenure year (D18) recoverable by
editing one field instead of re-fetching a corpus.

#### Task 10 — close the export leak (ship first, independently)

**Do not patch six call sites.** Six is how this defect happened: the filter
is a convention applied by hand, and the hand slipped six times out of eight.
Move the filter *inside the boundary* instead:

1. Add `export_publications_for(db, user_id, agent_id) -> (list[Publication],
   tenure_start)` to `src/services/profile_export.py` (or the Task 11
   `tenure_scope` module), which loads and filters in one place.
2. Change `export_profile_to_markdown` so `publications` is **not** a
   caller-supplied list. Either it takes `db` and loads them itself, or it
   takes an opaque `TenureScopedPublications` value that only the helper can
   construct. A plain `list[Publication]` parameter is what made six
   unfiltered call sites possible.
3. Convert all eight call sites (§3B D16) to the new signature. The three
   `scripts/` ones matter as much as the routers: `generate_sparsedata_user.py`
   wrote 56 of the 73 personas on disk today.
4. `tenure_start is None` stays a pass-through (D20).

Add a test that walks every caller of `export_profile_to_markdown` in `src/`
and `scripts/` and asserts the new signature — the same shape as
`tests/unit/test_reachability.py`'s route walk and
`tests/integration/test_manager_views.py`'s allowlist. A comment is what
failed here; a tripwire is what is needed.

**`manager.py:419-422` is the urgent one** and can ship ahead of the
refactor if the refactor takes longer than a day: until it is filtered, every
grant/industry veto silently reverts a correctly-exported persona to
full-career, which would quietly undo Task 14.

Verification: targeted pytest; then `$DC build blackbird-app worker && $DC up
-d blackbird-app worker`. **The agent image needs no rebuild** (nothing under
`src/agent/` imports these), but the corrected filter only reaches an existing
persona when that persona is re-exported — so pair with Task 14.

#### Task 11 — one scoping helper, one definition

Add a single service function — e.g.
`src/services/tenure_scope.py::in_tenure_publications(db, user_id, agent_id)`
— returning `(publications, tenure_start, out_of_tenure_count)`. Every read
site in D17's table calls it. Rules, stated once:

- `year >= tenure_start` (inclusive), matching `tenure_filter`.
- `year IS NULL` is **excluded** when a start is set (D19) but **counted and
  reported** as `undated_excluded`, never silently dropped.
- `tenure_start is None` returns everything with `tenure_start=None` so the
  caller can badge it (D20).

For `directory.py:170-175` the count must stay a single grouped SQL query, not
73 per-user round-trips: `func.count(...) FILTER (WHERE p.year >= tenure)`
with the tenure years loaded from `app_settings` in one read. Note the legacy
agent-keyed fallback map (`jhu_rules.LEGACY_TENURE_KEY`, 62 entries) — the SQL
path must honour it or 62 PIs silently lose their window.

#### Task 12 — verify the tenure years (blocking for B13)

Runs **after** A1–A4, so verification happens against a cleaned corpus.

1. Re-derive each PI's year from ORCID employments
   (`derive_employment_start`) and compare against the stored value. Report
   every disagreement; do not auto-overwrite a curated entry.
2. For the six PIs whose year came from `earliest_hopkins_paper` (Coyne,
   Slusher, Yarchoan, Pardoll, Vogelstein, Semenza), re-derive from the
   *repaired* corpus — the old derivation ran over rows D3 shows are
   contaminated.
3. Human review of the twelve in D18's table, worst first. One JHU faculty
   page or ORCID employment check each.
4. Record every correction with `set_tenure_start(..., source="manual")` so a
   curated year stays distinguishable from a derived one.
5. Re-run the impact query and re-publish D18's table with the corrected
   years, so B13's blast radius is known before it lands.

#### Task 13 — scope the counts and lists

Switch the four unscoped surfaces in D17 to the Task 11 helper:

- `/manager/pis` and `/admin/users` count columns
  (`directory.py:170-175`; `manager/pis.html:137`, `admin/users.html:105`)
- `/manager/pis/{id}` and `/admin/users/{id}` lists and headers
  (`directory.py:246-251`; `manager/pi_detail.html:337`,
  `admin/user_detail.html:125`)
- the PI's own `/profile` (`profile.py:65-70`; `profile/view.html:147-175`)

Presentation rules, so the change is legible rather than alarming:

- Label the number **"Publications (JHU tenure, since YYYY)"**, not
  "Publications". A bare number that drops from 50 to 9 with no explanation
  will be read as data loss.
- Show the out-of-tenure count as a muted secondary figure
  (`+41 before tenure`), and on the two staff detail pages provide a
  **"show full career"** toggle. This is not a nicety: correcting a wrong
  tenure year requires seeing the papers the current year excludes.
- Where `tenure_start is None`, render **"Publications (full career — no JHU
  tenure start recorded)"** and link to the Edit Profile field (D20).
- Surface `undated_excluded` wherever it is non-zero (D19).

The PI's own `/profile` page is the one to think hardest about: a PI who sees
their lifetime output cut to a JHU-only slice with no label will report it as
a bug. The label is the fix.

#### Task 14 — re-export every persona under the corrected window

After A5 and B12, re-export `profiles/public/{agent_id}.md` for every PI whose
tenure year changed in B12 or whose corpus changed in A4. The on-disk files
are clean *today* (D16), so this is about not regressing them, and about
reflecting the repaired corpus — not about fixing an existing leak.

Run the D16 measurement before and after as the acceptance check: parse the
`## Recent Publications` years out of every `profiles/public/*.md` and assert
none precedes that PI's tenure year. That is exactly the check Task 15
codifies.

Subject to Task 0.2: this rewrites files a live agent mtime-watches.

#### Task 15 — make the invariant enforceable, not conventional

- A test that asserts every `export_profile_to_markdown` call site filters
  (Task 10).
- A read-only `scripts/audit_tenure_scope.py` reporting, per PI: tenure year
  and its source, in-tenure / before-tenure / undated counts, and whether the
  on-disk persona's publication list contains any out-of-tenure entry. That
  last check is the only one that would have caught D16.
- Record in CLAUDE.md: storage is full-career **by design**, the tenure window
  is a read-side filter, and every new publication surface must go through the
  Task 11 helper.

## 6. Risks

- **Deleting a human-verified row.** Dry-run + per-row evidence + pg_dump +
  judgement calls kept out of the automatic set.
- **A widened matcher over-matching.** Items 1–4 widen, item 5 narrows; both
  halves must land together, and the measurement is the Task 2 *resolve delta*,
  not the stored-row classification.
- **Item 5 over-narrowing** onto pre-1988 and sparse-ORCID records — measure
  before implementing (Task 1 item 5(ii)).
- **Running the repair with the old matcher.** Task 4's build-then-`run --rm`
  sequence is the mitigation; `exec` is the failure mode.
- **NCBI block affecting org1's production stack** (D14) — API key is a
  precondition, and concurrent replays must be serialised.
- **Persona swap mid-run** (D10) — Task 0.2, with the `docker stop -t 420`
  fallback for the stale-heartbeat case.
- **Cap interaction.** Freeing ~196 slots means capped PIs pull in new papers
  on the next resolve — expected, and why Batch B precedes Batch C.

### Workstream B risks

- **Scoping counts on a wrong tenure year** is the largest one: ten PIs lose
  more than a third of their visible corpus, and Pombo drops 50 → 9. B12 is
  blocking for B13 precisely for this, and the "show full career" toggle keeps
  the excluded rows inspectable so an error is correctable rather than
  invisible.
- **Deleting out-of-tenure rows instead of filtering** would make a curation
  error permanent. Rejected in §3B; if anyone proposes it later, the 550 rows
  are the cost and a re-fetch is the only recovery.
- **A PI reads their own scoped count as data loss.** Mitigated by the label,
  not by the number.
- **`earliest_hopkins_paper` is circular** — six PIs' years were derived from
  a corpus that D3 shows is contaminated. B12.2 re-derives after repair.
- **Missing the legacy tenure map** in the new SQL count path would silently
  unscope 62 PIs (Task 11).
- **A fifth export site** reintroducing D16. The enumeration test in Task 15
  is the only durable guard; a code comment is what failed last time.
