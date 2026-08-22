# JHU instance rules: corpus and profile policies specific to this deployment

**Status:** DEPLOYED at the data level (all rules below are live in prod's data as of
2026-08-13/14); NOT yet enforced in pipeline code — see the companion plan.
**Companion plan:** `docs/plans/2026-08-13-jhu-instance-rules-plan.md` (the *how*).
**Parent spec:** `docs/specs/2026-08-13-pi-profile-coverage-design.md` — the general
coverage design. This document layers *instance policy* on top of it: what this
deployment (the JHU cohort screened by BlackbirdBot for incubation) wants profiles to
represent, as opposed to what is mechanically correct for any deployment.

**Purpose.** This instance screens PI ideas for incubation of **IP associated with
JHU**. Profiles therefore represent each PI's *JHU-tenure-era, individually-authored*
output — not their full career history, and not consortium membership. Every rule below
was validated by measurement against the live corpus before adoption; the rejected
alternatives are recorded with the numbers that killed them.

---

## R1 — Individual authorship is required; consortium papers are excluded

A paper belongs in a PI's corpus only if the PI appears as a **named individual author**
on the record. Consortium/collective attribution (N3C, GTEx, TOPMed, PGC, MACS, 4DN,
FANTOM, …) is identity-*correct* but is not individual lab output and must not consume
cap slots or ground synthesis.

- Verification matcher (proven over 2,729 pairs): surname match after folding
  (Unicode → ASCII, `ue`→`u` so `Müller`≡`Mueller`, hyphens/spaces stripped, compound
  surnames matched by containment so `Van Dang`≡`Dang` and `Marie Hardwick`≡`Hardwick`),
  plus a first-name/initials leg that treats pre-2002 initials-style `ForeName` ("J M")
  as initials. Known alias caveat: Ioannis "Yannis" Kevrekidis — alias lists beat
  loosening the matcher.
- A record with **no individual match and no CollectiveName** is *withheld and flagged*,
  never silently added or removed (this queue caught 2 true misattributions and the
  incomplete-author-list / authorless-record artifacts).
- Deployed state: 47 consortium papers removed (2026-08-13, apply2), vacated slots
  refilled additions-only with verified papers (apply3); full-corpus re-sweep confirms
  **0 consortium papers remain**.

## R2 — JHU-IP scoping: tenure-window rule, flag-not-delete

Profiles are synthesized **only from papers with `year >= tenure_start`** — the PI's
JHU employment start. The full verified corpus stays stored; the scoping is applied at
synthesis time. Reversal = regenerate without the tenure filter.

- `tenure_start` source hierarchy: **ORCID `/employments`** where a Hopkins employment
  with a start year is curated (14 PIs), else **the year of the PI's earliest
  Hopkins-affiliated paper**. Map persisted in the `app_settings` KV table, key
  **`jhu_tenure_start`** (JSON `{agent_id: year}`, 62 entries).
- Measured impact (2026-08-13): 528 of 2,728 papers (19.4%) fall outside tenure and are
  excluded from synthesis. Evidence counts describe the JHU-era grounding (e.g. `pombo`
  9, `shastri` 10, `hart` 5, `krieger` 1, `wu` 26, `agre` 35).
- **Rejected alternative — per-paper recorded affiliation.** Measured at 869 removals
  (31.9%), of which **290 are indexing artifacts**: PubMed recorded affiliations only
  for first authors before ~2014, and PIs are usually last author. Under that rule
  Peter Agre keeps 9 papers, with his JHU-era aquaporin work wrongly purged. Affiliation
  recorded on the paper is evidence of *where indexed*, not of IP assignment.
- **Known bias, accepted:** paper-derived tenure dates skew *late* for long-tenured PIs
  (their old papers lack recorded affiliations), so a few genuinely-JHU papers sit
  outside the filter. Correcting a date = edit the map, regenerate that PI.
- Deployed state: 34 profiles regenerated tenure-scoped (apply6, 2026-08-14 00:11 UTC);
  the other 28 PIs' corpora are entirely within tenure.
- **Scope covers the export list, not just synthesis.** Agent prompts embed the
  exported markdown verbatim, including the "Recent Publications" top-20 (shared
  cross-lab by the simulation). The first deployment missed this; 9 recent-recruit
  agents carried pre-tenure papers in their prompts until re-exported with
  tenure-filtered lists (2026-08-14, +9 revisions). Verified: no agent prompt now
  contains a pre-tenure paper.

## R3 — Editorials and errata cannot occupy cap slots

`EXCLUDED_TYPES` records (editorials, comments, letters, errata, …) were already barred
from synthesis; in this instance they are also **skipped during corpus selection**, so
they cannot displace research papers from the 50-cap. Motivating case: `dang` (an AACR
editor-in-chief) whose most-recent-50 was padded with verified-but-non-research
editorials. Publication types come from ESummary `pubtype`, whose vocabulary was
verified identical to efetch `PublicationType`.

## R4 — Name-only search stays banned, with a distinctive-surname exception (proposed)

The general design prohibits S4 without an affiliation ("Hardwick" alone → 1,779 hits).
This instance adds two findings:

- **Missing `users.institution` silently disables S4.** Two PIs (`mukherjeeclavin`,
  `pearce`) have NULL institution; for Mukherjee-Clavin this cost two-thirds of her
  corpus (3 stored vs ~9 verifiable, all carrying JHU Neurology affiliations). Fix the
  data (both are JHU) and make the coverage gate flag NULL-affiliation PIs.
- **Distinctive-surname exception (not yet applied):** when a surname's total PubMed
  hit count is small (< ~100), a name-only search is effectively homonym-free and may
  feed candidates — every hit still passes R1 verification individually. Candidates:
  `mukherjeeclavin` (3 vs ~9), `srinivasan` (10 vs ceiling ~30+, NIAID era), `markham`
  (17 vs ~34). Requires sign-off before running (additions change profiles).

## R5 — Inherited from the parent spec §7 (data-level deployed, code pending)

Unique-hit DOI resolution (D4b; multi-hit ESearch = miss — this bug planted the same
wrong paper on **six** PIs), quarantine of attribution-failing additions, kept-row
re-fetch. Recorded here only for completeness; they are general rules, not
JHU-specific policy.

---

## Deployed-state ledger (end of 2026-08-13 operation)

| Item | Value |
|---|---|
| Publications (all users) | 2,759 — individually verified, 0 consortium, 0 known-wrong |
| Active PIs at 50-cap | 45 of 62 |
| Profiles | 62/62 validated, synthesized from JHU-era corpora, evidence counts populated |
| Tenure map | `app_settings.jhu_tenure_start`, 62 entries |
| Exports | 62 `profiles/public/*.md` regenerated + 125 `profile_revisions` entries |
| Attribution review queue | empty |
| Rollback chain | `prod_pre_sync_1630` → `…step2_1655` → `…step3_1739` → `…step4/5` → `…step6` (+ full 69 MB dump), all table-scoped pg_dumps |

Every prod change shipped through a rehearsed, single-transaction apply gated on
fingerprint equality with the audited local state (`blackbird-db-copy` container:
`copi` = current, `copi_orig` = pristine); the fingerprint chain across all six applies
is unbroken.

## Open items

1. Code enforcement of R1–R4 (companion plan) — until then, any stock pipeline run
   (new signup, onboarding retry, unfiltered `regenerate-profiles`) reverts that PI to
   full-career, ORCID-only scope.
2. Distinctive-surname refill for `mukherjeeclavin`/`srinivasan`/`markham` (R4, gated
   on sign-off).
3. Tenure-date review pass for paper-derived dates (R2 bias).
4. `krieger` grounds on a single JHU-era paper — honest but thin; revisit as her JHU
   output accrues.
