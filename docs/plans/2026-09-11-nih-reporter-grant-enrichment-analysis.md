# NIH RePORTER grant enrichment for PI profiles — analysis + adversarial review

Implemented by: docs/plans/2026-09-11-pi-external-enrichment-implementation-plan.md

Date: 2026-09-11. Everything in the "measured" column was probed live against
`api.reporter.nih.gov/v2` on this date; nothing is from memory.

## 1. Where we are today

- `ResearcherProfile.grant_titles` is filled **only** from ORCID `/fundings`
  (`src/services/orcid.py:97`, `src/services/profile_pipeline.py:110`). It is
  a bare list of titles, is **not tenure-filtered** (only `corpus_records`
  pass through `tenure_filter`, `profile_pipeline.py:311`), and is fed to the
  synthesis prompt (`_build_synthesis_context`, `:616`).
- Coverage measured in prod: **17 of 78 profiles** have any grant title.
  Control case: Gyanu Lamichhane has **0** ORCID fundings but **14 NIH core
  projects / 26 project-years** in RePORTER, all at JOHNS HOPKINS UNIVERSITY.
- Publications carry no stored affiliation (the matched author's
  `pi_affiliations` exist only in memory during corpus resolution).

## 2. What RePORTER actually offers (measured)

| Capability | Result |
|---|---|
| `POST /v2/projects/search` with `criteria.pi_names[{last_name,first_name}]` | Works; returns one row **per fiscal year per project** (26 rows → 14 `core_project_num`). |
| `criteria.pi_profile_ids` | Works; the PI `profile_id` (e.g. 9751245) is the **stable person key**. |
| `criteria.org_names` / `org_names_exact_match` | Works. Every Hopkins award in FY2005/2012/2018/2025 is one string, `JOHNS HOPKINS UNIVERSITY`, IPF `4134401` (500-row samples, 1,700–1,850 awards/year). APL and SOM are not separate orgs. |
| Fields available | `project_num`, `core_project_num`, `fiscal_year`, `project_title`, `abstract_text` (1.8 kB), `phr_text` (public health relevance), `terms`/`pref_terms`, `spending_categories_desc`, `project_start_date`/`project_end_date`, `budget_start`/`budget_end`, `award_amount`, `direct/indirect_cost_amt`, `activity_code`, `agency_ic_admin`, `funding_mechanism`, `principal_investigators[{profile_id,is_contact_pi}]`, `program_officers`, `opportunity_number`, `is_active`, `subproject_id`. |
| **No ORCID anywhere.** | `criteria.orcid` is not a field. |
| ⚠️ **Unknown criteria keys are silently ignored** | `{"criteria":{"orcid":[...]}}` and `{"bogus_field":[...]}` both return HTTP 200 with `total: 2,975,461` — the whole database. A typo turns a filter into a firehose. |
| `POST /v2/publications/search` | `core_project_nums` → PMIDs works (Lamichhane's 10 older cores → 64 links, 49 distinct PMIDs). `pmids` → core project works (`34187885 → R01AI137329`). New awards have no links yet. `appl_ids` returned 0. |
| Rate limit | `x-rate-limit-limit: 1m`, `remaining: 193` → ~200 requests/min per IP. No key required. |
| Retrieval limit | `limit` ≤ 500 per page, `offset` paging. |

## 3. Adversarial analysis — how this goes wrong, and the control for each

### A. Identity (wrong person's grants land on a profile)
1. **Name collision.** `pi_names {last:"Wu", first:"Peng"}` returns two distinct people (profile_ids 8650265 ×46 rows, 9661503 ×3). Common East-Asian and Spanish surnames are worse. *Control:* never accept a name match alone. Require **org = JHU** AND at least one of: (a) a RePORTER **publication link** whose PMID is in the PI's ORCID-anchored corpus (`publications.pmid`); (b) a project `abstract_text`/`terms` similarity to the PI's own abstracts above a threshold. Then pin the resulting `profile_id` on the user (new `app_settings` key or column) and query by `pi_profile_ids` thereafter.
2. **Two RePORTER profile_ids for one person** (name changes, hyphenation, middle initial). Measured: Lamichhane has one; assume not. *Control:* run the name search with `any_name` and collect every `profile_id` that passes the PMID-link test; store the set, not one id.
3. **Nickname / initial mismatch** (`first_name` exact vs "Bob"). *Control:* search `last_name` + `org_names` only, then disambiguate as in A1.
4. **Co-PI vs contact PI.** MPI awards list several PIs; the PI may be a non-contact MPI. *Control:* accept any entry in `principal_investigators` with the pinned `profile_id`; record `is_contact_pi` so synthesis can weight it.
5. **Silent-firehose failure mode.** A misspelled criteria key returns everything and a naive loop would attach thousands of grants. *Control:* validate criteria keys against a frozen allowlist in code; abort if `meta.total > 500` for a single PI; unit-test the guard.

### B. Tenure (non-JHU grants leak in)
6. **Pre-JHU grants** at a prior institution. *Control:* filter on `organization.org_name == 'JOHNS HOPKINS UNIVERSITY'` (exact, measured stable) **and** `fiscal_year >= tenure_start`. Both, not either.
7. **Grant that moved with the PI** (change of institution mid-award). The early fiscal-year rows carry the old org and are excluded by the org filter; the later rows are correctly included. Dedupe to `core_project_num` **after** filtering, keeping first/last in-tenure fiscal year.
8. **No tenure entry.** `tenure_filter` is an identity when start is `None` (rule J1). For grants, mirror that: with no tenure start, fall back to org filter only and stamp the profile `grant_tenure_filter='org_only'` so a reviewer sees the weaker guarantee. Do **not** infer tenure from RePORTER itself (it would be circular).
9. **Hopkins-affiliated but non-JHU orgs** (Kennedy Krieger, Lieber Institute, Howard County General, JHU APL is JHU). Measured: APL rolls into IPF 4134401. *Control:* exact-match string; `is_hopkins_affiliation` is deliberately **not** used here because its substring match would admit "Johns Hopkins Health System" style orgs if they appear.
10. **Subprojects** of P30/P50/U54 centres (`subproject_id` non-null) list the sub-project PI. These are legitimate JHU funding; keep, but label as subproject.

### C. Content contamination
11. **Abstracts are grant prose, not results**; feeding them to synthesis risks the profile claiming planned work as done. *Control:* store `project_title`, `phr_text`, `terms`, dates, amount, `activity_code`; pass to synthesis under an explicit "funded aims (proposed, not results)" heading, and keep `research_summary` grounded in publications per the existing `synthesis_validated` check.
12. **Training / infrastructure awards** (T32, S10, K12 institutional, "Clinical Research Professor" already visible in Pienta's ORCID list). *Control:* keep for the record; exclude `activity_code` in {T32,T35,S10,G20,C06,UC7,…} from the synthesis context. Use a code allowlist, not a blocklist, for anything that reaches the LLM (R01,R21,R33,R35,R37,DP1,DP2,U01,U19,P01,K08,K23,K99,R00,R41–R44 incl. SBIR/STTR as co-PI).
13. **Stale `is_active`**: RePORTER flags are fiscal-year scoped; compute "current" from `project_end_date >= today`.
14. **Amount misuse**: `award_amount` is per fiscal year; summing rows double-counts supplements (`project_num` suffixes `S1`). Sum only rows with distinct `project_num`, and report per-core totals separately from lifetime totals.

### D. Operational
15. **Rate limit** ~200/min: 73 PIs × (1 search + 1 publications call + paging) fits in one minute; add the same `_pace()` discipline `patents.py` uses.
16. **Schema drift / silent field renames**: RePORTER has renamed fields before. *Control:* pin `include_fields` and assert each expected key exists on the first result; fail the stage loudly like `CorpusStageError`.
17. **Re-runs must be idempotent**: store rows keyed on (`user_id`, `core_project_num`), upsert, never append to `grant_titles`.
18. **Privacy**: all of this is public federal data; nothing here is PI-confidential. Amounts are fine to store; consider not rendering them on PI-facing pages.

## 4. Recommended design

New table `pi_grants` (one row per `core_project_num` per user):
`user_id, source='nih_reporter', core_project_num, reporter_profile_id, title,
phr_text, terms, activity_code, agency_ic, org_name, first_fy, last_fy,
project_start, project_end, total_award_in_tenure, is_contact_pi, is_subproject,
identity_evidence (jsonb: pmid links matched, org match, tenure filter mode),
fetched_at`. Keep `grant_titles` as a derived, tenure-filtered projection for
backward compatibility of the export.

Pipeline step (new "step 2b" after ORCID fundings, before synthesis):
1. Resolve `reporter_profile_ids` for the user (cached in `app_settings`
   `reporter_profile_ids:{user_id}`; refresh if empty).
   - `projects/search {pi_names:[{last_name}], org_names_exact_match:["JOHNS HOPKINS UNIVERSITY"]}`, `include_fields` minimal, page all.
   - Group by `profile_id`; for each, `publications/search {core_project_nums}` and intersect PMIDs with the user's `publications`. Accept a `profile_id` with ≥1 shared PMID, or (fallback) first-name exact + only one candidate at JHU.
   - Zero candidates → record `no_reporter_match`, stage succeeds (grants are optional; corpus is not).
2. `projects/search {pi_profile_ids, org_names_exact_match, fiscal_years: [tenure_start..now]}`; page; dedupe to core; upsert `pi_grants`.
3. Derive `grant_titles` = in-tenure, LLM-allowlisted activity codes, sorted by `last_fy` desc.
4. Emit a `job_progress` line with counts (candidates, accepted ids, cores, excluded pre-tenure, excluded non-JHU).

Manager UI: show grants under the PI page with the evidence badge
(`pmid-linked` / `unique-name` / `org-only`) and a "not this person" toggle
that blacklists a `profile_id` for that user (persisted, respected on re-run).

Tests: fixtures for (a) name collision with two profile_ids, only one PMID-linked;
(b) moved-institution award; (c) unknown-criteria guard; (d) no-tenure fallback;
(e) supplements not double-summed.

## 5. Out of scope / not available
- NSF (`api.nsf.gov/services/v1/awards.json` works, name-keyed, same identity problem, no org exact-match guarantee) — phase 2.
- Federal RePORTER (multi-agency) is decommissioned.
- SBIR.gov API returns 403 unauthenticated — needs a key if wanted.
- Europe PMC GRIST returned 0 for an NIH grant id; it is Europe-funder centric. Skip.
