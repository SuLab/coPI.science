# Industry-interest score for PI profiles — sources, adversarial analysis, plan

Implemented by: docs/plans/2026-09-11-pi-external-enrichment-implementation-plan.md

Date: 2026-09-11. Goal: a **score, not profile text** — how attractive the
PI's *JHU-tenure* work is to industry, as a leading indicator of
commercializability. Every source below was probed live on this date.

## 1. Sources that exist and what each yields (measured)

| # | Source | Signal | Measured on Gyanu Lamichhane (ORCID 0000-0002-2214-0114) | Tenure-scopable? |
|---|---|---|---|---|
| S1 | **OpenAlex works**: `filter=author.orcid:…,institutions.type:company` | Papers with a company-affiliated co-author | 15 of 144 works; `group_by=authorships.institutions.lineage` names Paratek Pharmaceuticals ×4, Collaborations Pharmaceuticals ×3, GSK, Applied BioPhysics, SRI | Yes — `publication_year`, plus the PI's own `authorships[].institutions` on each work |
| S2 | **OpenAlex `funders` / `awards`** on works, `group_by=funders.id` | Company-funded papers | Funders list is NIH/CFF/HHMI-heavy; company funders appear when acknowledged. Funder entities carry ROR; company-ness is via the ROR/institution `type` of the funder's institution role (GSK has both roles) | Yes (work year) |
| S3 | **PubMed `<CoiStatement>`** and `<Affiliation>` | Declared industry employment/consulting/equity; company co-authors | PMID 38980071: *"Daniel H. Deck and Alisa W. Serio are employees of Paratek…"*; affiliations name Paratek Inc. Present only for ~2017+ papers and only if the journal deposits it | Yes (paper year) |
| S4 | **USPTO ODP** `applications/search` by `firstInventorName` + `applicantBag.applicantNameText:"Johns Hopkins"` | Patent applications filed by JHU with PI as inventor; `assignmentBag` exposes **assignments/licences to companies** | 12 applications; applicant list shows JHU + co-applicant (Univ. of St. Thomas); assignment documents downloadable | Yes (`filingDate`); applicant=JHU is itself the tenure gate |
| S5 | **ClinicalTrials.gov v2** `AREA[LeadSponsorName]Johns Hopkins AND AREA[CollaboratorClass]INDUSTRY` | JHU-sponsored trials with industry collaborators; `OverallOfficialName` gives the PI | 419 JHU trials with industry collaborators exist; PI-level match is by name | Yes (`StartDate`) |
| S6 | **NIH RePORTER** activity codes R41–R44 (SBIR/STTR) with `funding_mechanism='SBIR/STTR'` | PI on a small-business award (usually as the academic partner) | Available via the grant plan's `pi_profile_ids`; Lamichhane: none | Yes |
| S7 | **JHU Tech Ventures** portfolio / "technologies available for licence" pages | Disclosed inventions, start-ups | Not an API; scraping ToS not verified — **do not use without permission** | n/a |
| S8 | Dimensions, Lens.org, Crunchbase, PitchBook | Company–academic links, start-up founding | Paid / keyed; not probed | n/a |
| S9 | Full-text acknowledgements (PMC OA) — "we thank X Inc. for compound", MTAs | Material collaborations without authorship | Repo already fetches PMC methods (`methods_text`, 105 of 3,314 pubs) | Yes |

Not usable: SBIR.gov API (403 without key); Europe PMC grants (Europe-centric).

## 2. Adversarial analysis

### A. The score measures the wrong thing
1. **"Industry co-author" ≠ "industry interested in *this PI's* science."** A 300-author consortium paper with a Pfizer statistician counts the same as a two-lab collaboration. *Control:* weight by PI author position (first/last/corresponding — `authorships.is_corresponding`, `author_position` already stored on `publications`) and by author count (down-weight >30 authors; drop consortium-only papers as `corpus.py` already does).
2. **Reagent vendors and CROs are companies.** Applied BioPhysics (an instrument maker) appears in S1. *Control:* classify company co-authors into {pharma/biotech, device/diagnostics, CRO/vendor, other} using OpenAlex `type=company` + a curated vendor blocklist; score only the first two, list the rest as context.
3. **Clinical-trial industry collaborators are pharma-driven, not PI-driven** (Novartis supplying drug for an investigator-initiated trial is a weaker signal than a PI's own compound going into a company trial). *Control:* separate weights for "PI is OverallOfficial & JHU is lead sponsor & industry collaborator" (moderate) vs "industry is lead sponsor and PI is site investigator" (near zero — that's service, not IP).
4. **Patents are the strongest signal only when licensed.** An unlicensed application is intent; an `assignmentBag` conveyance to a company (or a company as co-applicant) is revealed preference. *Control:* two tiers — filed vs assigned/licensed — and treat "company is co-applicant" as top tier.
5. **COI statements reveal consulting, SABs, equity, founder roles** — the most direct "industry finds this person valuable" evidence. But "The authors declare no competing interests" is the majority and says nothing. *Control:* parse only positive statements; extract (company, relationship type) with a small LLM call, dedupe company names across papers, and cap the contribution so one prolific consultant does not dominate.
6. **Field base rates differ.** Infectious-disease and oncology PIs co-author with pharma far more than a structural biologist. *Control:* report the raw components **and** a field-normalised percentile (OpenAlex `primary_topic.field` of the PI's corpus), so the manager sees both.

### B. Tenure leakage (the "JHU only" rule)
7. A paper in the tenure window co-authored with the PI's *previous* industry employer is still in-tenure by year but may reflect pre-JHU work. *Control:* the PI's **own affiliation on that work** must match a Hopkins institution (`authorships[].institutions` ∈ {I145311948 JHU, I2799853436 JH Medicine, I4210150714 JH Hospital, I2802946424 APL, I2802697821 Bayview, I4210098865 All Children's, I4210129832 Children's Center, I4210092215 Berman, I4389425327 CFAR, I4210114877 CHS} — the OpenAlex JHU family measured today) **and** `publication_year >= tenure_start`. Both. Use `raw_affiliation_strings` + `is_hopkins_affiliation` as a fallback when OpenAlex has no institution for the PI's authorship (measured: some early works have `[]`).
8. OpenAlex mis-assigns ORCIDs occasionally; a work by a namesake is possible. *Control:* intersect with the PI's stored `publications` (ORCID/PubMed-anchored corpus) — score only works already in our corpus, using OpenAlex purely as an enrichment lookup by `pmid`/`doi`.
9. Patents: filing date is tenure-scopable, but priority/provisional dates may predate JHU. *Control:* require JHU in `applicantBag` (a JHU-filed application is by construction JHU-period IP); ignore applications where JHU is absent.
10. Trials: use `StartDate >= tenure_start` and lead sponsor ∈ JHU sponsor names (measured strings include "Sidney Kimmel Comprehensive Cancer Center at Johns Hopkins" — the sponsor name set must be enumerated, not substring-matched blindly).
11. No tenure entry → **do not score**; emit `unscored: no tenure start` rather than an org-only guess. This is stricter than rule J1 on purpose: a score is a judgment, not a list.

### C. Gaming, drift and provenance
12. The PI or a manager can edit the profile — the score must be recomputed only from sources, never from `user_submitted_texts`.
13. OpenAlex `institutions.type` and company lineage change over time; store the raw evidence rows (work id, company id, role) so a re-score is reproducible and diffs are explainable.
14. Name-based sources (S4, S5) have the same collision problem as RePORTER. *Control:* for patents, require JHU applicant AND inventor full name match AND at least one CPC/keyword overlap with the PI's `key_targets`/`keywords`; for trials, require PI name AND JHU sponsor AND condition overlap with `disease_areas`. Show evidence for manual veto.
15. LLM extraction of COI relationships can hallucinate a company. *Control:* extraction returns spans; keep only companies that appear verbatim in the statement; log the call.

### D. What the score must not do
16. Must not be written into `research_summary`, `grant_titles`, or any profile text (operator instruction). Store on a separate table and expose read-only.
17. Must not reach the hub's prompt (it would bias screening); no code path from the new table into `Agent._compose_system_prompt` or `profile_export`.

## 3. Plan

### Data model
`pi_industry_evidence` — one row per (user_id, source, external_id):
`kind` ∈ {coauthor_company, company_funder, coi_relationship, patent_filed,
patent_assigned, trial_industry_collab, sbir_sttr, ack_material}, `company_name`,
`company_openalex_id`/`ror`, `company_class` ∈ {pharma_biotech, device_dx,
cro_vendor, other, unknown}, `year`, `pi_role` (first/last/corresponding/middle;
inventor; overall_official), `evidence` jsonb (ids, spans), `in_tenure` bool,
`fetched_at`.

`pi_industry_scores` — one row per user per scoring run: `score_0_100`,
`components` jsonb (per kind: count, weighted, capped), `field_percentile`,
`tenure_start_used`, `evidence_count`, `scorer_version`, `computed_at`.
Versioned exactly like the rubric (bump `scorer_version` on any weight change;
old rows keep their version).

### Collection (worker job `score_industry_interest`, per PI, idempotent)
1. Load tenure start; if None → write `pi_industry_scores` with `score=NULL,
   reason='no_tenure_start'` and stop.
2. For each stored publication with pmid/doi: OpenAlex lookup (batch `filter=pmid:…|…`, 50/req); keep works where the PI's authorship is Hopkins-affiliated and year ≥ tenure. Record company co-author institutions (type=company) and company funders.
3. PubMed efetch (batch 200) for the same PMIDs: parse `CoiStatement`, `Affiliation` strings containing Inc/Ltd/GmbH/Pharma/Therapeutics/…; LLM-extract (company, relationship) from positive COI statements only.
4. USPTO ODP: `firstInventorName` = PI (and try inventor bag if the API exposes it) AND applicant "Johns Hopkins"; fetch `assignmentBag`; classify assignee.
5. ClinicalTrials.gov: PI as OverallOfficial, JHU-family lead sponsor, `CollaboratorClass INDUSTRY`, start ≥ tenure.
6. RePORTER SBIR/STTR rows from the grant pipeline (depends on the RePORTER plan's pinned `profile_id`).
7. Company classification: OpenAlex institution entity → `type`, plus a curated YAML of vendor/CRO names; unknowns default to `other` (weight 0.25) and are surfaced for curation.

### Scoring v1 (transparent, additive, capped)
- coauthor_company (pharma/device): 3 per distinct company, ×1.5 if PI first/last/corresponding, cap 30
- company_funder: 4 per distinct company, cap 20
- coi_relationship (consulting/SAB/equity/founder): 5 per distinct company, founder/equity ×2, cap 25
- patent_filed (JHU applicant): 4 each, cap 12; patent_assigned/co-applicant company: 10 each, cap 30
- trial_industry_collab (JHU lead, PI official): 3 each, cap 9
- sbir_sttr: 6 each, cap 12
- Normalise raw sum to 0–100 against the cohort (all scored JHU PIs), and also emit `field_percentile` within `primary_topic.field`.
- Store components so the manager can see *why*.

### Surfacing
- Read-only panel on `/manager/pis/{id}` and a sortable column on `/manager/pis`: score, percentile, evidence count, "recomputed at", with an evidence drawer listing every row and a per-row "not this PI / not industry" veto that persists and excludes on re-score.
- No PI-facing surface; no prompt/export path (test: import probe like the review-bot's, asserting `profile_export` and `simulation.py` never import the new module).

### Validation before trusting the number
- Positive controls: PIs with known company ties (COI-positive papers, licensed patents) must score in the top quartile.
- Negative controls: early-career PIs and pure-methods labs should be bottom quartile; a fresh manager account (no corpus) must be `unscored`.
- Adversarial fixture set: consortium paper with a pharma statistician; vendor co-author; pre-tenure paper inside the year window with non-JHU PI affiliation; namesake patent without JHU applicant; industry-sponsored trial where PI is a site investigator — each must contribute 0 or be down-weighted as specified.
- Re-score determinism: same evidence rows → same score (unit test).

### Sequencing
1. Evidence collection + storage (no score) — 1 worker job, 1 migration, manager evidence drawer.
2. Human review of evidence quality on ~10 PIs; curate the vendor list.
3. Scoring v1 + cohort normalisation + controls.
4. Only then decide how the score is consumed (explicitly out of scope now).

## 4. Open items needing an operator decision
- Whether JHU Tech Ventures data may be used (licensing terms).
- Whether to purchase a Dimensions or Lens API key (would add company–PI grant and patent-citation links; not required for v1).
- Weights above are a proposal; they should be set after step 2's evidence review, not before.
