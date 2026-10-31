# JHU drug-target ingest (Blackbird landscaping): analysis of the existing interview and assessment system

**Status:** pre-spec analysis, 2026-10-30. The owner answered §7's blocking questions the same
day. The design that implements those answers is
`docs/specs/2026-10-30-blackbird-landscaping-design.md`: its §3.1 records the answers and its
§3.2 the calls made on the rest. This document is kept as the evidence base the design cites
("analysis B2" means finding B2 here).
**Evidence rule:** every claim about this repository cites the file it was read from, at
commit `0c035b43`. Every external fact was checked live on 2026-10-30 and cites its source. A
claim marked *inferred* was reasoned, not read.

## 1. The request, as stages

| Stage | The request | Label used below |
|---|---|---|
| S0 | ingest all JHU-associated data, publications, grant documents, patents | Ingest |
| S1 | filter on type of research and on date (past 3 years) | Filter |
| S2 | an LLM reads title + abstract and keeps good drug-design / drug-target research with unmet need | Screen |
| S3 | an LLM reads the full text: target, disease, possible modality (may wait), existing evidence; may need a literature review | Extract |
| S4 | classify the extractions on the existing assessment rubric and BlackbirdBot prompt set | Triage |
| S5 | rank; the top 1% continue | Cut |
| S6 | gate, score, and analyse the disease profile | Deep analysis |
| S7 | rerank | Rerank |
| S8 | cluster by similarity, paper, genes, or lab | Cluster |
| S9 | final assessment, possibly an interview | Final |

Scope given: the past 3 years; all drug-discovery and disease-mechanism research; the goal is
druggable targets; reuse the existing system as far as possible; a parallel prototype.

## 2. What exists today

### 2.1 Interviews

- **Who starts one.** Only a lab. A `pi_lab` agent posts a top-level pitch (at most one a day,
  `lab_daily_post_cap=1`, `src/config.py:469`), and BlackbirdBot (`scout_hub`, agent id
  `blackbird`) auto-activates on it (`src/agent/engine/post_lane.py:315-376`). The hub is
  reply-only: `posts_new_threads=False` (`src/agent/role_capabilities.py:72`), and its role
  manifest pins `post_types` to an empty list (`prompts/roles/scout_hub/role.toml:24`).
- **What a lab is.** A persona of one rostered PI, built from that PI's tenure-era,
  individually authored papers (rules R1/R2, `docs/specs/2026-08-13-jhu-instance-rules-design.md:28-58`;
  `src/services/jhu_rules.py`). `pi_lab` requires a linked user (`role_capabilities.py:61`).
- **Shape.** The thread closes at 12 messages (`config.py:452`): EXPLORE 1-4, DECIDE 5-11,
  CONCLUDE 12 (`src/agent/thread_guidance.py:207-219`). Every turn, lab or hub, is Claude
  Opus 5 (`llm_agent_model_opus`, `config.py:338`) with adaptive thinking and max_tokens 16000
  (`src/agent/engine/reply_lane.py:897-1002`).
- **The hub's evidence tools:** `retrieve_profile`, `retrieve_abstract`, `retrieve_full_text`
  (at most 2 per thread, `config.py:492`), `search_prior_art`, `consult_specialist`
  (`prompts/roles/scout_hub/role.toml:13`). `retrieve_full_text` returns the abstract and the
  **methods section only**: no results, figures or discussion (`src/services/pubmed.py:1372-1412`).
- **How the verdict is captured.** An `<assessment_json>` sidecar in the hub's reply
  (`src/agent/engine/sidecar.py:76-163`). The CONCLUDE guidance offers two outcomes: a verdict
  with sidecar, or ⏸️ with no sidecar, and says "Option 2 is perfectly acceptable — most
  interviews should end there" (`thread_guidance.py:185-196`).
- **What is stored.** `opportunity_assessments` is unique on `(simulation_run_id, thread_id)`.
  Its run foreign key is NOT NULL and cascades; `channel_name` is NOT NULL; `subject_agent_id`
  is a roster id (`src/models/opportunity.py:36-71`). Alongside: `specialist_consults`,
  `hub_declines`, `assessment_drops`.
- **What is published.** One headline per verdict in `#assessments-summary`. Finalize
  announces owed headlines, capped at 25 (`src/agent/engine/constants.py:181`;
  `headlines.py:352-399`).
- **Freeze.** "The interview prompt sets are frozen (owner, 2026-10-26)"
  (`src/agent/CLAUDE.md:41`); scout_hub is at 1.12.0 (`role.toml:8`).

### 2.2 The assessment contract

- **Rubric** `prompts/rubric/blackbird-rubric.toml` v3.5.0, loaded once at import:
  - 6 dimensions, weights 25/25/25/15/5/5: differentiation & unmet need, scientific
    credibility, translational path, fundable experiment, venture potential, team.
  - Scale 1-5. Bands: advance ≥ 3.4, conditional ≥ 2.8, else `pass` (shown as "decline").
  - 3 tri-state gates: `life_sciences_domain`, `credible_science`, `translational_potential`.
  - At most 3 disqualifier-grade red flags.
  - The 11-item Target Rubric as `evidence` lists on two dimensions.
- **Score.** `weighted_score` is computed in code, never taken from the model. A missing
  dimension counts as **0**, below the scale floor (`src/services/blackbird_rubric.py:503-567`).
  Gating, red flags and confidence do not enter the computation
  (`src/agent/engine/panel.py:519-539`).
- **Thresholds.** 3.4 / 2.8 are "PROVISIONAL BY CONSTRUCTION": back-tested on 51 interview
  verdicts with 4 positives; "the re-check itself is still owed" (`blackbird-rubric.toml:130-140`).
- **Panel.** 8 specialist personas. A consult is one Opus 5 call (max_tokens 4000) returning
  `blocking | gap | adequate` (`src/agent/specialists.py:47`; `src/agent/tools.py:767-866`).
  The floor always requires `scientific` and `talent` (`specialists.py:877`), plus
  cue-triggered domains. A gap is recorded on the row, not enforced.
- **Ordering.** List views only (`src/services/directory.py:128-174`). There is no
  cross-assessment ranking, dedup, similarity or clustering anywhere in `src/`.

### 2.3 Projects, the closest existing idea generator

- **Lab-first.** A project researches "new uses for one lab's existing work" from that lab's
  public persona (`prompts/projects/research.md:3-6`; `src/services/project_research.py:38-39`).
- **Research.** Five domain sessions on Opus 5 (`config.py:441`), each with up to 8 tool
  rounds, over four tools: PubMed search, ClinicalTrials.gov v2, USPTO ODP title search,
  abstract fetch (`src/services/project_search.py:84-207`). There is no web search, no full
  text and no embeddings.
- **No scores.** "Never give a score, rank, number or band" (`prompts/projects/choose.md:8`).
- **No cap.** "There is no dollar ceiling (removed by the owner, 2026-10-27)"
  (`project_research.py:27`).
- **Run-bound.** Rows require a `simulation_run_id` (`src/models/hub_project.py:108-122`).

### 2.4 Ingest, worker and LLM infrastructure

- **OpenAlex** (`src/services/openalex.py`):
  - Person-only filters (`author.orcid:`, :117).
  - `_MAX_PAGES = 5`, so at most 1,000 works, truncated silently (:35, :130-155).
  - Fields lack topics, authorships, full dates and OA locations (:60-62).
  - A key exists in production (`OPENALEX_API_KEY` is set).
- **PubMed** (`src/services/pubmed.py`):
  - `search_pmids` reads one page (:808-830).
  - The parser drops MeSH, keywords and grant lists (:692-805).
- **Full text.** The PMC methods section only (`pmc_methods.py`). There is no license or
  open-access check, and every error becomes `None` (`pubmed.py:1243-1262`).
- **NIH RePORTER** (`src/services/nih_reporter.py`):
  - Can query by organisation and fiscal year.
  - Raises above 500 rows by default (:72-84).
  - Does not request `AbstractText` (:36-41).
- **USPTO ODP** (`src/services/odp.py`, `industry_sources/uspto_inventor.py`):
  - An inventor + applicant query (:103-104), at most 10 pages (:46).
  - A key exists in production (`USPTO_API_KEY`).
- **Worker** (`src/worker/main.py`):
  - One process, one job at a time (:567-571).
  - One transaction per handler: a late failure rolls back everything (:448-485).
  - Dedup only for per-user job types (`src/services/job_queue.py:83-84`).
- **LLM** (`src/services/llm.py`):
  - At most 8 concurrent calls (:256); no Message Batches API anywhere.
  - `llm_call_logs` requires a `simulation_run_id` (`src/models/agent_activity.py:207-211`),
    so worker calls cannot log there.
  - Each feature keeps its own cost ledger; the pattern to copy is
    `src/services/company_discovery_budget.py`.
  - `llm_pricing.py` has no price for `claude-sonnet-5-5` or `claude-haiku-5-5`, and an
    unpriced model returns `None` (`llm_pricing.py:28-39, 43-54`).
  - Production enables server-side refusal fallback (`ENGINE_REFUSAL_FALLBACK_ENABLED=true`).
- **Data model.** `publications`, `pi_grants` and `pi_industry_evidence` are keyed by
  `user_id` (`src/models/publication.py:92-147`; `src/models/enrichment.py:42-96`). There is no
  standalone works, grants or patents table.
- **JHU markers:**
  - `is_hopkins_affiliation` (`jhu_rules.py:51-65`).
  - 10 OpenAlex institution ids (`src/services/industry_sources/__init__.py:18-21`).
  - RePORTER `JOHNS HOPKINS UNIVERSITY` (`nih_reporter.py:42`).
  - No ROR ids.

### 2.5 Roster

- This is the JHU instance: "This instance screens PI ideas for incubation of IP associated
  with JHU" (`docs/specs/2026-08-13-jhu-instance-rules-design.md:20-21`).
- Roles are `pi_lab` and `scout_hub` only.
- `data/agent_roster.json` (exported 2026-08-11) lists 64 agents: 56 active (one is
  BlackbirdBot), 7 pending, 1 inactive. The live `AgentRegistry` count was not queried.

## 3. Adversarial findings

### A. An interview cannot take a paper-derived idea as its input

- **A1. No entry point.** Only a lab can open an interview, and the hub cannot post
  (§2.1). Feeding S9 into interviews needs a new start path in the engine and new prompt
  text, against a prompt freeze. Both are owner decisions.
- **A2. No counterparty for most ideas.** A lab agent exists only for a rostered PI (about 55
  active labs). Most JHU papers come from labs with no agent (*inferred* from §4.1 volumes).
  Even for a rostered lab, R1/R2 drop consortium and pre-tenure papers from the persona, so the
  agent may not "know" the paper the idea came from.
- **A3. The counterparty is simulated.** The lab agent is Opus 5 speaking as the PI. In an
  interview about a specific paper, anything it adds beyond the paper is not evidence, yet the
  verdict cannot tell the two apart.
- **A4. The designed majority outcome has no score.** ⏸️ closes carry no sidecar, so they
  carry no scores and no band (§2.1). An interview cannot be the ranking instrument.
- **A5. Cost.** One interview is up to 6 hub turns (each up to 8 Opus 5 calls), 6 lab turns
  and 2-8 consults. That is tens of Opus calls per idea, viable only for a final handful.
- **A6. The hub never reads results.** Its full-text tool returns methods only (§2.1). Today's
  credibility judgments rest on abstracts, methods and the lab persona's claims.

### B. The rubric's numbers are not a ranking instrument as they stand

- **B1. Thresholds don't transfer.** They are provisional, fitted on 51 interview verdicts, and
  the owed re-check was never done. In 3.5.0 a weight change alone moved 4 of 27 bands. A
  paper-only evidence base has a different distribution.
- **B2. Ties.** Six integer scores times weights in multiples of 5 give a score on a 0.05 grid:
  only 81 possible values from 1.00 to 5.00. Thousands of ideas will tie at any top-1% line.
- **B3. Missing dimensions zero out.** A paper cannot honestly support `team_executability`
  or `fundable_experiment`, and rarely `venture_potential`. Scored as missing they count 0, so
  the maximum possible score drops to 3.75 and "advance" needs ≥ 4.53 on the other three.
  Otherwise the model invents them. A paper-only scorer needs explicit "not assessable"
  handling: a new function, leaving `weighted_score` alone.
- **B4. Gates are not enforced.** `credible_science: not_met` can still band "advance"; only
  the model's recommendation stops it. A triage filter must enforce gates in code.
- **B5. The commercial half is the model's own knowledge.** The rubric tells the hub to fill
  the commercial dimensions "through your own diligence and the specialist panel". No tool
  supplies epidemiology, standard of care, approved drugs or competitor programs today.
- **B6. Panel calibration is unmeasured at this use.** The code records past signal flatness
  (`caution` absorbed 85.7% of consults; one run's panel never cleared 142 of 142,
  `specialists.py:49-53`, `panel.py:77-80`). It is now monitored, not corrected. Using the
  panel as a gate over hundreds of ideas needs a fresh measurement
  (`scripts/panel_calibration_ladder.py` exists for this).
- **B7. Unknown repeatability.** I found no measurement of how stable an LLM's 1-5 scores are
  on re-runs. Ranking thousands of ideas on single absolute scores assumes a stability nobody
  has measured.

### C. Feeding the same tables is hazardous

- **C1. Rows and headlines.** Writing pipeline rows into `opportunity_assessments` needs a
  simulation run, a channel and a thread, and a roster `subject_agent_id`. Any such row
  written under a run is headlined at Finalize: only 25 post and the rest are logged LOST, so
  pipeline rows would crowd out real interviews (`headlines.py:352-399`).
- **C2. Downstream readers assume an interview.** The detail page, assessment chat, review
  bot and trajectories all assume a Slack transcript keyed by run, channel and timestamp, and
  a roster subject (`src/services/interview_transcript.py:29-84`;
  `src/services/assessment_detail.py:1633-1648`).
- **C3. Run-bound tables.** `llm_call_logs` and `hub_projects` cascade from
  `simulation_runs`, and nobody may delete a run (CLAUDE.md). A standing pipeline needs its
  own run and ledger tables.
- **C4. Rubric versions.** The rubric is an import-time global, stamped per row by version and
  content hash. The pipeline must stamp the same way and decide what happens to stored scores
  when the rubric version bumps.
- **C5. The frozen prompts.** "Triage on the BlackbirdBot prompt set" can reuse the rubric
  rendering (`render_rubric_markdown`) and the sidecar contract without editing scout_hub
  files. New pipeline prompts belong in their own set, as `prompts/projects/` is.

### D. Infrastructure at institution scale

- **D1. Silent truncation.** Every client truncates: OpenAlex at 1,000, esearch at one page,
  RePORTER at 500, ODP at 10 pages. A JHU-wide pull is 45-98k records (§4.1). NCBI also
  caps what can be fetched:
  - PubMed ESearch serves only the first 9,999 records of any query (live: "'retstart'
    cannot be larger than 9998").
  - PMC ESearch returns at most 9,999 records, like PubMed (announced 2026-01-06, in effect
    since early February 2026).
  
  So a pull of 45,041 records must be sliced by date.
- **D2. One worker, one job.** A long ingest job blocks every PI job, interactive ones
  included. A deferral on page N rolls back pages 1..N-1. Work must be split into small,
  checkpointed jobs, or run on a separate worker.
- **D3. No batch path.** No Batches API, 8 concurrent calls, no batch prices.
- **D4. No global spend ceiling.** Profile synthesis and project research are uncapped. A
  pipeline calling `acreate` directly has neither a cap nor a log.
- **D5. Full text is methods-only.** Every error becomes `None`, so a coverage gap looks like
  "no methods".
- **D6. Contamination risk.** Institution rows stored in the per-PI tables would leak into
  personas, tenure counts and industry scores.
- **D7. Nothing to cluster with.** No embeddings, similarity or clustering code, and no
  embeddings provider key.
- **D8. Prompt injection.** Full texts are untrusted input. Reuse the `delimit`/`_escape`
  pattern (`src/services/company_sources/coi_llm.py:154-198`).

### E. Model safety classifiers

- **E1. In this repo, current-generation models declined this exact content.** Opus 5.5
  declined 36 of 37 replayed production requests with category `bio` and was rolled back.
  The consult, pitch, hub and profile routes were all hit
  (`docs/audits/2026-09-28-opus-5-5-migration/README.md:27-50`).
- **E2. Current docs** (checked 2026-10-30):
  - Classifiers run on Fable 5/5.1, Opus 5/5.5, Sonnet 5.5 and Haiku 5.5.
  - A `bio` refusal before any output **is billed**.
  - Server-side fallback is not available in the Batches API.
  - Haiku 5.5 has no server-side fallback and no fallback credit.
  - Sonnet 5.5's default fallback does not retry `bio`.
  - Source: [refusals and fallback](https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback).
- **E3. Life Sciences Verification Program.** It "relaxes the real-time classifiers that
  normally interrupt biology work" for verified organisations, naming target discovery
  ([support article 16975617](https://support.claude.com/en/articles/16975617)).
  - Covers Opus 5.5, Opus 5 and Sonnet 5 on the API.
  - Sonnet 5.5 and Haiku 5.5 only through a High-risk Use add-on.
  - 30-day retention, no zero data retention; decisions take about 3 business days.
- **Consequence.** The cheapest screening models are the ones most likely to decline, and
  their refusals cost money. Model choice and the cost model cannot be fixed until refusal
  rates are measured on a JHU sample.

### F. Hazards in the requested pipeline itself

- **F1. Dedup comes too late.** Clustering after the top-1% cut lets one lab's paper series
  on one target fill several slots and splits its evidence across copies. Ideas need merging
  on a normalized target × disease before ranking. S8 then becomes the portfolio view.
- **F2. "Top 1%" has no base.** 1% of works in the window is about 778; of PubMed records
  with an abstract, about 399; of screen positives or extracted ideas, unknown (tens). S6
  cost and reviewer load differ by more than 10× across these.
- **F3. Unmet need is rarely in an abstract.** Screening on it at S2 cuts recall. It is
  measurable at S6 from burden, standard of care and approved drugs.
- **F4. Druggability depends on modality.** Blackbird's own precedents include antisense
  programs (its private criteria document). A small-molecule-only tractability test would have
  rejected them.
- **F5. Gene clustering needs normalization.** Gene and disease names from an LLM must map
  to stable ids before gene clustering or dedup can work. No such code exists.
- **F6. Recall cannot be measured without labels.** The only known positives are the
  worked deals in Blackbird's private criteria document (not in the repository). One is a
  diagnostic, and their source papers may predate the window.
- **F7. Abstracts are missing for many works.** 23% of JHU window articles have no abstract in
  OpenAlex (11,314 of 49,106); PubMed has abstracts for 39,896 of 45,041.
- **F8. Full text is legally reachable for about half.**
  - PMC holds 28,928 JHU-affiliated records in the window.
  - 21,207 carry the open-access filter and 5,685 are author manuscripts.
  - Only these are text-minable by the permitted routes.
  - The rest is abstract-only unless publisher TDM licenses are used, and those licenses do
    not fit this use:
    - Elsevier, Springer Nature and Wiley grant subscription text mining for non-commercial
      research only.
    - Elsevier and Wiley reserve AI uses.
    - Springer Nature requires storage "without access for third parties".
- **F9. Grant documents are abstracts.** Full applications are public only through FOIA,
  redacted.
- **F10. Patents are blocked for now.** The PatentsView API has been paused since 2026-03-20
  and ODP needs a key. JHU patent counts are unverified. JHTV's public RSS lists 440
  technologies since 2023-10-30, already-disclosed inventions, with terms of use unverified.
- **F11. Datasets are poorly attributed.** DataCite datasets with the JHU ROR number 1,201,
  while name matching finds 5,186. The JH Research Data Repository API blocks scripts. Yield
  of target ideas is likely low (*inferred*).
- **F12. Prestige bias.** The rubric says prestige "is not the test", but nothing hides
  authors or journals from a scorer.
- **F13. Hallucinated targets and evidence.** Without verbatim quotes and location checks,
  extracted evidence cannot be audited.
- **F14. Evidence sources carry different licenses (§4.3).**
  - Non-commercial only: DrugBank, IHME GBD, and some DGIdb sources (OncoKB, COSMIC).
  - Share-alike: ChEMBL.
  - Unrestricted: Open Targets (CC0), HGNC, and Orphadata (CC BY).
  
  The answer to Q38 decides which may be used.
- **F15. Pharos/TCRD looks unmaintained.** Its last changelog entry is 2024-02-15 and its
  program's funding has ended.
  Target development level (Tclin/Tchem/Tbio/Tdark) would be stale. Open Targets'
  tractability by modality is current (release 26.09).

### G. Rights and governance

- **G1. PMC text-mining terms.** Only the PMC Cloud Service, OAI-PMH, E-utilities and the
  BioC API may be used for automated retrieval. License groups split commercial use (CC0, BY,
  BY-SA, BY-ND) from non-commercial (BY-NC*) ([PMC text mining](https://pmc.ncbi.nlm.nih.gov/tools/textmining/)).
  Whether Blackbird's use counts as commercial is a legal question: BBL is a nonprofit and
  BBV is a VC vehicle. PMC also contradicts itself on author manuscripts:
  - its cloud FAQ says they are "available for commercial reuse regardless of license";
  - its copyright page limits them to uses "consistent with the principles of Fair Use".
  
  No PMC page mentions LLM use.
- **G2. Retired PMC routes.** The PMC OA Web Service is gone (404) and the legacy FTP
  packages were removed in August 2026. What remains:
  - BioC (3 requests/s);
  - E-utilities;
  - OAI-PMH;
  - the `pmc-oa-opendata` S3 bucket, with one object per article version and its license code
    in the metadata.
- **G3. Third-party processing.** Sending licensed full text to the Anthropic API is a
  disclosure the license may not allow (F8). Open-access text under a commercial-use license
  is not affected.
- **G4. Consent.** Today every assessed PI is an onboarded user. The pipeline scores every
  JHU researcher's work without their knowledge, and JHTV owns licensing of JHU IP.
- **G5. The public repository.** `origin` is public. Rankings of named researchers must never
  be committed, and this document holds none.

## 4. Verified external facts (2026-10-30)

### 4.1 Volumes

The window is 2023-10-30 to 2026-10-30.

| Source | Count | Query |
|---|---|---|
| OpenAlex works, JHU + JH Medicine lineage | 77,776 | `authorships.institutions.lineage:I145311948\|I2799853436`, window |
| …articles / with abstract / with abstract and OA | 49,106 / 37,792 / 31,339 | `type:article`, `has_abstract:true`, OA status |
| …preprints | 6,792 | `type:preprint` |
| PubMed `"Johns Hopkins"[ad]` | 45,041 | `("2023/10/30"[dp] : "2026/10/30"[dp])` |
| …with abstract / with PMC full text | 39,896 / 27,934 | `hasabstract`; `"pubmed pmc"[sb]` |
| PMC affiliation search / open access / author manuscript | 28,928 / 21,207 / 5,685 | `"Johns Hopkins"[affl]` |
| Europe PMC / with full text | 41,552 / 25,461 | `AFF:"Johns Hopkins"`; `HAS_FT:Y` |
| NIH RePORTER records, `JOHNS HOPKINS UNIVERSITY` | FY24 1,827 · FY25 1,702 · FY26 1,622 | FY26: 1,173 distinct core projects |
| NSF new awards | 206 | `awardeeName`, initial award date |
| JHTV technologies (RSS) | 440 | `jhu.technologypublisher.com/rss.aspx` |
| GEO series | 427 | 12 institute-name variants |
| Patents | not verified | ODP needs a key; PatentsView paused |

**Institutions.**
- Johns Hopkins Medicine (I2799853436) is a separate top-level OpenAlex institution from
  JHU (I145311948).
- Kennedy Krieger, SAIS, Jhpiego and the Center for Health Security are separate again.
- APL and Bloomberg School papers roll up to JHU.

**OpenAlex topics are a poor drug filter.** Subfield "Drug Discovery" tags 1 article in the
window.

### 4.2 Access, 2026-10-30

- **OpenAlex.**
  - Keyless: one shared $0.10 per day per IP; a list page costs 1 credit (measured).
  - Cursor paging needs a key; a free key gets $1 per day.
  - `per-page=200` is honoured (measured).
  - Snapshot: free on S3, quarterly.
  - Content (PDF/TEI): $10 per 1,000, key required.
- **NCBI E-utilities.** 3 requests/s without a key, 10 with one (production has one).
- **NIH RePORTER.** At most 1 request/s and 500 rows per call; `abstract_text` is present on
  1,620 of 1,622 FY26 records.
- **Europe PMC.** No key. `fullTextXML` covers the open-access subset.
- **Unpaywall.** v2 only. Requires an email; the limit is 100,000 calls a day; it runs on the
  OpenAlex database; `/search` was retired on 2026-09-18.
- **bioRxiv/medRxiv API.** Cannot filter by affiliation. Use OpenAlex instead.

### 4.3 Evidence, normalization and similarity sources

Each was called live unless marked.

- **Full text.**
  - PMC cloud bucket `pmc-oa-opendata` (no auth; per-article JSON gives `license_code`,
    `is_manuscript`, `is_retracted`, XML/text/PDF URLs).
  - BioC API (open access plus author manuscripts; passages tagged by section; 3 req/s).
  - Europe PMC `fullTextXML` (no auth; rate limit unverified).
  - Unpaywall and OpenAlex content downloads ($10 per 1,000) locate other open-access copies.
- **Mentions and relations.** PubTator3 annotates genes, diseases, chemicals, variants and
  species by PMID or PMCID, and returns typed relations (treat, cause, inhibit, …).
  - No auth; 3 req/s.
  - Gene ids are species-specific: autocomplete "KRAS" → mouse 24525, while human KRAS is
    NCBI Gene 3845. Normalization must check species.
- **Identifiers.**
  - HGNC REST: symbol → HGNC, Entrez, Ensembl, UniProt, OMIM; no use restrictions.
  - Mondo: CC BY 4.0, with mappings to MeSH, EFO, DOID, OMIM, Orphanet and ICD.
  - Open Targets now uses Mondo ids.
- **Target evidence and tractability: Open Targets Platform** (GraphQL, release 26.09,
  quarterly, CC0 with source terms varying).
  - Target–disease association scores by data type.
  - Tractability by modality: small molecule, antibody, PROTAC, other.
  - Safety events.
  - `drugAndClinicalCandidates`, which replaced `knownDrugs` in 26.09.
  - No rate limit is stated; for bulk use it points to downloads or BigQuery.
- **Chemistry and drugs.**
  - ChEMBL 37: mechanisms, max phase, indications; CC BY-SA 3.0.
  - openFDA Drugs@FDA: CC0; 1,000 calls a day keyless, 120,000 with a key.
  - DrugCentral: CC BY-SA 4.0.
  - DrugBank: CC BY-NC; academic downloads paused.
  - DGIdb: licenses mixed by source.
  - Pharos/TCRD: frozen since 2024 (F15).
- **Proteins.** UniProt 2026_03 (CC BY 4.0); Human Protein Atlas 25.1 (CC BY 4.0, "commercial
  purposes" encouraged).
- **Trials.** ClinicalTrials.gov API v2.0.5. Free with attribution; the classic API is
  retired; rate limit unverified.
- **Disease burden and prevalence.**
  - IHME GBD 2023: account, non-commercial license, commercial use by contract, no public REST
    API.
  - Orphadata API: CC BY 4.0, prevalence classes.
  - CDC WONDER: national data only, one query per 2 minutes.
  - OMIM: key, renewed yearly; commercial use licensed through JHU.
  - NORD: no API; scraping prohibited.
- **Literature and citations.**
  - PubMed (≤ 9,999 records per query).
  - Europe PMC citations and references.
  - OpenAlex `cites:` and `referenced_works` (CC0).
  - Semantic Scholar Graph API: 1 req/s with a key, which production lacks; body-text snippets
    and citation contexts.
- **Similarity.**
  - "Anthropic does not offer its own embedding model"; its docs point to Voyage AI
    ([embeddings](https://platform.claude.com/docs/en/build-with-claude/embeddings)).
  - OpenAlex `search.semantic` (GTE-Large; 50 results; 1 req/s) finds neighbours but does not
    hand out vectors (unverified).
  - Semantic Scholar SPECTER2 vectors need a key; dataset license unverified.
  - Self-hostable: `allenai/specter2_base` and `voyageai/voyage-4-nano` (both Apache-2.0).
  - Hosted embedding APIs (Voyage, OpenAI, Google, Cohere, Mistral, Bedrock) offer no
    biomedical-specific model.

### 4.4 LLM prices

USD per million tokens, from [pricing](https://platform.claude.com/docs/en/about-claude/pricing).

| Model | Standard in / out | Batch in / out | Bio classifier |
|---|---|---|---|
| Haiku 5.5 (prompt ≤ 100K) | 0.10 / 0.50 | 0.05 / 0.25 | yes; no server fallback |
| Haiku 4.5 | 1 / 5 | 0.50 / 2.50 | not listed among classifier models |
| Sonnet 5.5 | 2 / 10 | 1 / 5 | yes; fallback skips `bio` |
| Sonnet 5 | 2 / 10 | 1 / 5 | not listed among classifier models |
| Opus 5 (production today) | 5 / 25 | 2.50 / 12.50 | listed |
| Opus 5.5 | 4 / 20 | 2 / 10 | yes; 36/37 declined here |

Batch API limits: up to 100,000 requests or 256 MB per batch, usually under 1 hour and at
most 24 hours; refused items must be resubmitted by hand. Web search costs $10 per 1,000
searches; web fetch costs tokens only.

## 5. Reuse map

| Stage | Reuse as is | Reuse with change | New |
|---|---|---|---|
| S0 | `openalex.reconstruct_abstract`; `pubmed.lookup_pmcids`; `identifiers.normalize_*`; `http_pacing`; `nih_reporter.publications_for_cores` | `openalex._list_works` (public, uncapped, streamed, wider `select`); `pubmed.search_pmids` (paging) and parser (MeSH, grants); `nih_reporter.search_projects` (+`AbstractText`, sliced); ODP applicant-only query; `jhu_rules` markers (+ lineage ids, ROR) | standalone document tables; a BioC / Europe PMC full-text client; a JHTV RSS reader |
| S2-S4 | `llm.abeta_create` + `refusal_category`; the structured-output pattern in `coi_llm`; `delimit`/`_escape` | `llm_pricing` (+Sonnet 5.5, Haiku 5.5, batch modifier); the `company_discovery_budget` reserve/settle ledger, generalized | Batches client; pipeline ledger; extraction schema; idea canonicalizer |
| S3 evidence | `project_search` tools (PubMed, CT.gov, ODP, abstract); `project_plan.source_key`/`resolve_source` grounding | | clients for PubTator3, HGNC, Mondo mappings, Open Targets, ChEMBL, openFDA, Orphadata (§4.3) |
| S4-S6 | `blackbird_rubric.load_rubric`, `render_rubric_markdown`, `render_stage_bar_markdown`, `band`; sidecar normalizers; `specialists.parse_opinion`, `required_domains_for` | `tools._call_specialist` (private, so wrap it); a renormalizing scorer beside `weighted_score` | triage prompt set (not scout_hub); disease-profile step |
| S7-S8 | — | — | pairwise rerank; clustering |
| S9 | `render_assessment_headline` (only if Slack is wanted) | `Agent.build_system_prompt` for a dossier-only verdict (*inferred* feasible) | interview start path (only if Q31 says so) |

## 6. Cost envelope (illustrative only)

The token sizes below are assumptions to be replaced by a measured pilot; the prices are
§4.4's. Thinking tokens, refusal re-runs and agentic literature search are excluded, so
these are floors.

| Stage | Assumed volume | Assumed tokens per item (in / out) | Haiku 5.5 batch | Sonnet 5.5 batch | Opus 5 batch |
|---|---|---|---|---|---|
| S2 screen | 45,000 abstracts | 1,000 / 200 | $4.50 | $90 | $225 |
| S3 extract | 5,000 papers | 15,000 / 3,000 | $7.50 | $150 | $375 |
| S4 triage | 7,500 ideas | 8,000 / 2,000 | — | $135 | $338 |
| S6 deep | 75-400 ideas | about 8 consults plus 1 profile call, Opus 5 standard | | | $54-$288 |
| S7 rerank | 75-400 | about K·log₂K pairwise calls, 6,000 / 600 | | | $11-$78 |

The order of magnitude for a 3-year backfill is hundreds to low thousands of dollars,
decided mostly by the S3/S4 model, the S5 base, and the refusal rate.

## 7. Open questions

Each question says what it changes. **Blocking** questions change the architecture. The
others carry a proposed default that I will use unless you change it. A default is a
proposal, not a decision.

### Purpose and output

1. **Blocking.** Who acts on the output, and what do they do next? For example: staff read a
   shortlist, then call the PI or JHTV. What result would make v1 a success? This sets the
   final list size and the recall/precision trade-off.
2. What is the deliverable: a staff-only web page of ranked ideas with dossiers, a report
   export, Slack posts, or several? *Default: a staff-only workspace page plus a CSV/Markdown
   export; no Slack.*
3. **Blocking.** A one-time 3-year backfill, or continuous intake after it (weekly or monthly)?

### Scope

4. **Blocking.** Which institutions count as "JHU-associated"? JHU and Johns Hopkins Medicine
   are separate in OpenAlex. Kennedy Krieger, APL, All Children's (Florida), SAIS, Jhpiego and
   the Center for Health Security sit at different places. *Default: the JHU + JH Medicine
   lineages, without APL, SAIS, Press or Jhpiego.*
5. **Blocking.** What role must the JHU author play: any co-author, or the senior or
   corresponding author whose lab "owns" the idea? *Default: the last or corresponding author
   is JHU-affiliated; other papers are kept as "JHU co-authored" at lower priority.*
6. How is the date window applied: rolling 3 years by publication date? Grants active in
   FY2024-26? Patents published in the window? *Default: all three as written.*
7. **Blocking.** Which source types are in v1: articles, reviews, preprints, NIH or NSF grant
   abstracts, patents, JHTV technologies, datasets? *Default: articles, preprints, NIH grant
   abstracts and JHTV technologies in v1; patents once an ODP pull is proven; datasets and
   reviews out (reviews used only as evidence).*
8. What does "data" mean in "data/publication/grant document/patent": deposited datasets
   (GEO, DataCite, the JH repository), or something else?
9. **Blocking.** Modality and product scope: therapeutics of any modality only, or also
   diagnostics, platforms or tools, devices, vaccines, and repurposing? The rubric's domain
   gate admits diagnostics and platforms, and one of Blackbird's worked deals is a diagnostic.
   *Default: therapeutics of any modality (small molecule, biologic, ASO/siRNA, gene or cell
   therapy, degrader) plus repurposing; others recorded as out-of-scope, not deleted.*
10. Are any disease areas excluded? Infectious disease and biodefense carry the highest
    refusal risk. *Default: none excluded; refusal rates reported per area.*
11. Does "disease mechanism" research qualify when it names no specific druggable molecule?
    *Default: it qualifies only if a specific gene, protein or RNA can be named as the
    intervention point.*

### Funnel shape

12. **Blocking.** "Top 1%" of what (all works, screened abstracts, screen positives, or
    de-duplicated ideas), and a percent or a fixed number? *Default: a configurable K applied
    to de-duplicated ideas.*
13. **Blocking.** Merge duplicate ideas (normalized target × disease) before ranking, and
    keep S8 for the portfolio view? *Default: yes (F1).*
14. What is clustering for: dedup, spotting a lab's platform, or presentation? Which axis
    matters most? Should a cluster become the unit of the final assessment (a "program")?
15. Should staff review at checkpoints before spending continues: after the classifier pilot,
    after the cut, and before the final stage? *Default: all three.*

### Screen and extraction

16. **Blocking.** Can Blackbird staff label 200-300 abstracts (keep or drop, with a reason)
    and name the papers behind past positives? Without labels, S2 recall is unknowable.
17. What operating point should the S2 screen use? *Default: recall-first, at least 0.9 on
    the labeled set.*
18. **Blocking.** Full text from open access and author manuscripts only, or also licensed
    content? Publisher text-mining licenses are non-commercial and reserve AI use (F8), so the
    licensed route would need new commercial agreements. *Default: open access plus author
    manuscripts; abstract-only extraction flagged lower confidence.*
19. Are these the right extraction fields?
    - target(s), normalized
    - disease, normalized
    - modality (optional)
    - mechanism hypothesis
    - the 11 Target Rubric evidence items, each with a verbatim quote and its location
    - model systems
    - novelty claims and stated limitations
    - senior lab
    - linked grants and patents
    
    What is missing? Patient population? Biomarker?
20. How deep should the literature review go? *Default: fixed database checks (§4.3) for
    every idea, and an agentic search like projects' only for the top K.*

### Triage and ranking

21. **Blocking.** "bblbot prompt set": the string occurs nowhere in the repository. Does it
    mean BlackbirdBot's interview prompts (`prompts/roles/scout_hub/`, frozen) or the projects
    prompts (`prompts/projects/`)?
22. **Blocking.** Three of the six dimensions cannot be judged from a paper (team, fundable
    experiment, mostly venture). What should triage use?
    - (a) gates plus the three science/need dimensions plus the Target Rubric checklist,
      renormalized;
    - (b) all six with "not assessable" handling; or
    - (c) weights of your own.
    
    *Default: (a), reading definitions from the rubric file, with prompts in a new prompt set;
    scout_hub untouched.*
23. Should a `not_met` gate remove an idea in code? *Default: yes (B4).*
24. May S7 use pairwise comparisons instead of absolute scores to break the 0.05-grid ties
    (B2)? *Default: yes.*
25. Should the S4 scorer be blind to authors, journal and institution (F12)? *Default: blind
    at S4; identities revealed at S6.*

### Deep analysis

26. What belongs in a "disease profile"? Candidates: prevalence and burden, standard of care,
    approved drugs, the trial pipeline, competitor targets, patient segments, market size.
    GBD bars commercial use without a contract (§4.3). *Default: Orphadata prevalence, Open
    Targets drugs and candidates, ClinicalTrials.gov pipeline, openFDA approvals, and
    LLM-summarized standard of care with citations; no GBD unless licensed.*
27. Which specialists run at S6? Talent and budget have nothing to judge without the PI.
    *Default: scientific, chemistry, clinical, commercial, legal.*
28. Should S6 look for JHU patents or JHTV listings on the same target (an asset already
    disclosed)? *Default: yes, once patents are in scope.*

### Final stage and integration

29. **Blocking.** Which parts of "the same interview/assessment system" should these ideas
    reach?
    - (a) the Assessments page;
    - (b) human review and assessment chat;
    - (c) Slack headlines;
    - (d) interviews with lab agents.
30. **Blocking.** Interviews: none in v1, only for ideas from rostered labs, or onboarding new
    PIs (the Add-PI flow) for top ideas from other labs? Any interview needs a new start path,
    new prompt text against the freeze (A1), and acceptance that the "PI" is simulated (A3).
31. **Blocking.** Separate pipeline tables with a "promote to assessment" action, or pipeline
    rows in `opportunity_assessments` with a provenance column (C1, C2)? *Default: separate
    tables; promotion later.*
32. Should a pipeline run be its own run, separate from simulation runs, and re-runnable on a
    new rubric version? *Default: yes.*

### Models, cost and operations

33. **Blocking.** What is the budget: total for the backfill, and per month if intake is
    continuous?
34. **Blocking.** Model policy, given E1-E3:
    - apply to the Life Sciences Verification Program now;
    - stay on Opus 5 with fallback for everything; or
    - pilot cheaper models first and measure refusals.
    
    *Default: pilot (300 abstracts and 50 full texts per candidate model, measuring refusal
    rate and agreement), and apply to the program in parallel if you approve.*
35. Is the Batches API acceptable: results within 24 hours, no automatic fallback in a batch?
    *Default: yes for S2-S4.*
36. **Blocking.** Where does it run? The existing worker runs one job at a time (D2). Options:
    a separate ingest worker container on the production host (18 GB free, 73% used); a
    separate database; or another machine. *Default: a separate worker container on the same
    host and database, with additive migrations, and no full-text bodies stored after
    extraction.*
37. How is similarity measured? Anthropic offers no embedding model (§4.3). Options:
    - a hosted embeddings API: a new vendor, key and data flow;
    - a self-hosted Apache-2.0 model such as SPECTER2 on the host CPU;
    - clustering on normalized gene, disease and lab ids, plus LLM judgments, with no
      embeddings.
    
    *Default: normalized ids plus LLM judgments in v1; self-hosted SPECTER2 if that proves too
    coarse.*

### Rights and relations

38. **Blocking.** Who decides whether Blackbird's use is "commercial" for CC BY-NC content, for
    author manuscripts (PMC contradicts itself, G1), and for non-commercial evidence sources
    (F14)? *Default until decided: only CC0, CC BY, BY-SA and BY-ND full text (PMC's "cc by
    license" filter alone finds 12,348 JHU records in the window); only unrestricted evidence
    sources.*
39. **Blocking.** Who may see rankings of JHU researchers who have not opted in? Should JHTV
    know about, or take part in, this pipeline? *Default: admins only; nothing leaves the app.*
40. Does anything in v1 contact a PI or JHTV? *Default: no.*

### Evaluation

41. Which known ideas must the pipeline find (positive controls)? Will you accept "at least N
    of them in the top K" as the acceptance test?
42. Who reviews the S9 output to measure precision, and how much time can they give?
