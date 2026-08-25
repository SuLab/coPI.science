# Manager Add-PI end-to-end onboarding — design + plan (2026-08-24)

**Status: IMPLEMENTED 2026-08-24** (same day; owner approved §8 with OpenAlex
included, the existing ~30-paper synthesis window kept, and tenure
auto-derivation with manager correction). Every task below is coded and
tested TDD-style; the audit ledger's accepted findings are all reflected.
Deviations from the letter of the plan, both safety-neutral:
(1) tenure entries live in per-user `app_settings` rows
(`jhu_tenure_start:{user_id}`, upsert via INSERT..ON CONFLICT) rather than a
locked shared map — strictly stronger than the planned row lock; persistence
happens in the caller's session, and the pipeline's paper-tier derivation is
safe against the worker's failure-commit because `resolve_corpus` raises
BEFORE derivation on any stage failure, so a derived year always reflects a
complete corpus. (2) For pre-existing PIs, S2-only (OpenAlex) candidates are
flagged for review rather than stored, same as S4-only — only ORCID-anchored
stages (S1/S3) add rows to an audited corpus. NOT deployed yet: images not
rebuilt, containers not restarted; see §6 for the deploy steps.

**Goal (owner's words):** adding a PI through the manager PIs tab must (A)
autogenerate the profile based on the 50 latest JHU-associated publications,
(B) create the bot, disabled upon creation, and (C) leave the bot easy to add
to Slack.

---

## 1. Definitions adopted

| Term | Definition | Basis |
|---|---|---|
| JHU-associated | The instance's audited tenure-window rule: publications with `year >= jhu_tenure_start`; undated papers excluded when a start is set; identity filter when no start is known. Per-paper affiliation filtering stays REJECTED (measured: 869 removals, 290 of them indexing artifacts). | `docs/specs/2026-08-13-jhu-instance-rules-design.md` §R2 |
| 50 latest | Rank year DESC, PMID DESC tiebreak, cap `[:50]` applied LAST. `EXCLUDED_TYPES` and consortium-only papers may not consume cap slots. | coverage design §4.1; jhu rules R1/R3 |
| disabled | `AgentRegistry.status='pending'`. The engine loads only `status=='active'` (`src/agent/simulation.py:7164`, `src/agent/main.py:166`), so a pending bot never polls, posts, or calls the LLM. (The owner PI does see a "requested" landing page — pending is inert, not invisible.) | verified |
| easy Slack add | The existing admin flow: `/admin/agents/{uuid}` → Provision (Manifest API → OAuth install link) → callback stores token → Approve & Activate. Provisioning stays **admin-only** (CLAUDE.md names it one of the three admin-only powers). | verified |

**Storage policy (uniform across cohorts):** the *stored* corpus is the
full-career top-50 (matching the deployed state of the existing 62 PIs — "the
full verified corpus stays stored", R2); the tenure filter is applied at
**synthesis time and at every export site**. Because ranking is year-DESC, the
tenure-filtered subset of the stored top-50 *is* the ≤50 latest JHU-associated
publications, so requirement (A) is met without forking storage semantics
between old and new PIs. This also keeps a tenure-year correction recoverable
(regenerate; no re-fetch needed).

## 2. Verified current behavior (why each piece is needed)

1. `POST /manager/pis` (`src/routers/manager.py:150-166` →
   `src/services/pi_onboarding.py:16-54`) already creates the User (role `pi`,
   name/email/institution/department from ORCID; fetch failure ⇒ no user) and
   already enqueues a `generate_profile` job. It does **not** create an
   `AgentRegistry` row, and no manager path does.
2. The job runs `run_profile_pipeline` (`src/services/profile_pipeline.py`),
   which is ORCID-only (works → PMIDs; DOI→PMID), unbounded, with **no**
   affiliation/tenure logic and **no** 50-cap; Publication rows persist
   uncapped (`:217-228`); synthesis context is year-DESC `[:30]` (`:531-533`).
   The audited JHU/coverage pipeline (corpus resolver, tenure rules, coverage
   gate, the pubmed parser fix) was never implemented — the docs themselves
   warn that any stock run "reverts that PI to full-career, ORCID-only scope."
3. Markdown export + revision are gated on an agent row existing at job time
   (`profile_export.py:24-25`; `profile_pipeline.py:470-499`), and
   README/runbook order ("seed, then create the registry row") loses that race
   every time — `scripts/backfill_agents.py` exists to repair exactly this.
   Creating the pending row atomically in the Add-PI transaction closes the
   gap for this flow.
4. The manager write surface is pinned to exactly four POST paths
   (`tests/integration/test_manager_views.py:55-74`). Adding behavior inside
   `POST /pis` and inside the existing `/pis/{user_id}/profile` form keeps
   that test green; no new manager route is added anywhere in this plan.
5. One-lab-per-user (design D7, `docs/specs/2026-08-17-user-account-types-design.md:134`)
   is structural: `AgentRegistry.user_id` is unique, and the auto-created row
   belongs to the new PI, never the manager. `POST /agent/request` early-returns
   when a row exists (`agent_page.py:427-431`), so later self-service cannot
   collide. `set_agent_mute_state` no-ops on pending (`agent_mute.py:19`), so
   the manager mute control cannot activate a pending bot. A manager-registered
   throwaway PI still cannot log in without admin access approval
   (`access_status` defaults `pending`).

## 3. Audit ledger (findings → what changed in this plan)

Accepted, high severity:

- **H1 (red-team): the worker COMMITS on failure** (`src/worker/main.py:100-111`),
  so a tenure year derived from a degraded run would persist forever and, per
  the draft's own "persisted for stability" rule, never be re-derived.
  → Tenure persistence moved to its own short transaction (`INSERT … ON
  CONFLICT`), paper-derived years persist **only** from fully successful runs,
  and every entry records provenance (`{year, source, derived_at}`).
- **H2: "Hopkins-affiliated paper" must be PI-author-only** (via the R1-style
  matcher), or a 2005 paper with a JHU co-author derives a false-early tenure.
  Employment tier: the PI's *current* (no end-date) Hopkins employment's start
  year; multiple/ended employments fall through to the paper tier.
  → Also: the employment tier is derived **at add time** (the ORCID record is
  already in hand in the route) and shown on the PI detail page for correction.
- **H3 (both audits, independently): the draft's "export is tenure-filtered
  automatically" claim was FALSE for the existing 62 PIs** — the pipeline
  exports the top-20 over ALL stored rows (`profile_pipeline.py:479-486`,
  `profile_export.py:76-82`), and `profile_edit.py:81` (the manager
  profile-edit re-export) has the same exposure. This is the exact regression
  that hit 9 recent-recruit agents on 2026-08-14.
  → `tenure_filter` is applied explicitly at **both** export call sites, and
  the test uses a full-career-store fixture (a pre-filtered fixture would be
  trivially green).
- **H4: a minimal activation gate must ship WITH auto-created bots.** Auto
  -creation breaks today's invariant that a pending row implies a completed
  profile, and the operator's real workflow is bulk (slack_install_links.md).
  A dead-job PI could be provisioned and activated with NO exported profile —
  the Kavran-class failure by routine admin action.
  → Gate in `admin_approve_agent` itself (both branches, `pi_lab`-scoped):
  refuse activation when the profile is missing, `evidence_state != grounded`,
  or the newest generate_profile job is dead — with an explicit, logged
  override checkbox. No migration needed. The pi_detail hint is keyed on job
  state so a dead job reads "generation failed", not "awaiting Slack install".

Accepted, medium/low (abridged):

- **M1/#5: tenure map keyed by `agent_id` has no key on agentless pipeline
  runs** (CLI seeding, self-signup before `/agent/request`, admin
  find-or-create). → New entries keyed by `user_id`; legacy agent_id map read
  as fallback; one-time migration script rewrites the 62 curated entries
  (verified against AgentRegistry before/after).
- **M4: re-runs for PIs with a pre-existing (audited) corpus**: additions only
  from ORCID-anchored stages (S1/S3, D4b-verified); S4-sourced candidates are
  flagged for review, not stored; never exceed 50 stored rows (skip + log at
  cap); never delete.
- **M5: corpus-stage failure ⇒ raise** (job retry ×3 → dead, visible on
  /admin/jobs and pi_detail) rather than storing a thin S1-only corpus.
- **M6: simultaneous same-surname adds** race to an IntegrityError that the
  route's `ValueError` handler misses (500). → catch, re-derive once, then
  canned error redirect.
- **L1/#L1: no ORCID validation anywhere**; the raw string reaches the ORCID
  URL path, the error-redirect query string, and (new) PubMed `[auid]` terms.
  → validate `^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$` in `find_or_create_pi_by_orcid`;
  redirect with canned error codes, not `str(exc)`.
- **#2 (fact-check): Phase 4 as drafted rendered nothing** —
  `admin_agent_detail` (`src/routers/admin.py:821-857`) loads neither the
  profile nor the tenure map. → handler change added.
- **#3: today's `convert_dois_to_pmids` already violates D4b** — its ESearch
  `{doi}[doi]` fallback takes `idlist[0]` unchecked
  (`src/services/pubmed.py:394-411`), the documented wrong-paper bug.
  → fixed: unique idlist + round-trip DOI verification.
- **#4: `Job.payload` is plain JSON with no mutation tracking** — progress
  appends after the first `db.flush()` are silently lost
  (`profile_pipeline.py:60-66`). → `update_progress` reassigns the payload
  (or `flag_modified`) so flags like `tenure_unknown` actually persist.
- **#8: deferring OpenAlex (S2) costs real coverage** — the 2026-08-13
  rehearsal shows OpenAlex was often the largest discovery source (gill:
  ORCID 0 works vs OpenAlex 33), precisely for the sparse-ORCID PIs this flow
  onboards. → recommendation flipped: **include S2**, with all non-S1
  candidates (S2/S3/S4) passing the same disambiguation + authorship gate
  (which also catches the rehearsal's one OpenAlex mislink class).
- **#9: the agent image must be rebuilt** — `src/agent/tools.py:20` imports
  from `src/services/pubmed.py`, which the parser fix touches.
- **#10: S3 retmax must be 200 explicitly** (the seeder constant it's ported
  from is 50; the design's "retmax 200" applies to S4).
- **#6/#7: corrected wording** ("pending excluded from agent-page *actions*",
  owner still sees a request page) and the roster-sync pending-exclusion test
  does NOT exist yet — it is added below, not relied on.
- **#19: the reachable identity edge is an all-digit/no-alpha name** (stem
  `""` → empty agent_id, silently), not an empty-string IndexError; the guard
  and its test target that case.

## 4. Implementation tasks

Prerequisites (pipeline correctness, independent of the feature):

- **T1** `src/services/pubmed.py:324,330` — itertext parsing for title +
  abstract (coverage plan Task 1). Regression test: title with inline
  `<i>`/`<sup>` markup survives intact. ⚠️ `src/agent/tools.py` imports this
  module → agent image rebuild required at deploy.
- **T2** `convert_dois_to_pmids` ESearch fallback (`pubmed.py:394-411`): accept
  only a unique idlist AND round-trip verify (the PMID's authoritative DOI ==
  queried DOI) — D4b. Test: multi-hit DOI ⇒ miss.
- **T3** `update_progress` (`profile_pipeline.py:60-66`): reassign
  `job.payload` (or `flag_modified`) on every call. Test: progress entry
  appended after a flush survives commit.

Corpus + JHU rules:

- **T4** `src/services/agent_identity.py`: move `derive_agent_identity` out of
  `src/routers/agent_page.py:386-407` (import back; no behavior change for
  existing callers), add the numeric third tier (`{base}2..19`), keep the web
  path's display casing (`McCarthyBot`), guard the empty-stem case (fallback
  slug from the ORCID digits, logged). The two script copies
  (`scripts/backfill_agents.py`, `scripts/generate_sparsedata_user.py`) are
  left alone but noted as divergent.
- **T5** `src/services/jhu_rules.py`: `HOPKINS_PATTERNS`;
  `get_tenure_start(db, user_id, agent_id=None)` reading the new
  user_id-keyed map with legacy `jhu_tenure_start` (agent_id-keyed) fallback;
  `set_tenure_start(db, user_id, year, source)` in its OWN short transaction
  via `INSERT … ON CONFLICT` (provenance: `{year, source, derived_at}`);
  `tenure_filter(pubs, start)` (identity when None; else `year >= start`,
  undated excluded); `derive_employment_start(orcid_record)` (current Hopkins
  employment start year — requires start-date parsing added to
  `src/services/orcid.py`, which today reads only org/department of current
  employments); `derive_start_from_papers(records)` (earliest paper where the
  **PI herself** matches as an individual author with a Hopkins-pattern
  affiliation). One-time migration script for the 62 legacy entries →
  user_id keys, verified against AgentRegistry.
- **T6** `src/services/corpus.py`: `resolve_corpus(orcid, name, institution,
  *, cap=50)` — S1 ORCID works (D4b-verified DOI resolution); S2 OpenAlex by
  ORCID (new thin client, no key required); S3 PubMed `{orcid}[auid]`
  (retmax 200, sort pub date); S4 PubMed name+affiliation only when
  institution is non-empty (port `_build_pubmed_query`/`_esearch_pmids` from
  `scripts/generate_sparsedata_user.py:220-261`); mandatory disambiguation
  (port `_disambiguate` `:329-415`) applied to ALL of S2/S3/S4; authorship
  classification (individual vs consortium vs no-match; consortium excluded
  from cap slots, no-match withheld + flagged, per R1/J2); `EXCLUDED_TYPES`
  skipped pre-cap; dedupe by PMID + normalized title (errata not collapsed);
  rank year DESC / PMID DESC; `[:cap]` LAST. Any stage exception ⇒ raise
  (job retry). Returns kept + flagged + per-stage counts.
- **T7** pipeline integration (`profile_pipeline.py`): corpus replaces the
  ORCID-only steps 3-4. Storage: new PI (no rows) ⇒ persist the top-50; PI
  with pre-existing rows ⇒ additions-only from S1/S3, ≤50, S4 flagged-not
  -stored, diff logged. Tenure: read map (user_id → legacy); if absent,
  employment tier was already persisted at add time (T8) — else derive the
  paper tier in-pipeline, persisting only on a fully successful run.
  `tenure_filter` applied to (a) the synthesis selection (before the existing
  `[:30]` abstract-bearing window) and (b) the export publications — at BOTH
  export sites (`profile_pipeline.py:480-486` and
  `src/services/profile_edit.py:81`).

Manager flow + bot:

- **T8** `src/services/pi_onboarding.py` + `src/routers/manager.py`: ORCID
  regex validation before fetch; canned error codes in the redirect (no
  `str(exc)` interpolation); `create_pending_agent_for(db, user)` (idempotent;
  uses T4) called by `manager_create_pi` in the same transaction as User +
  Job (single commit ⇒ the worker always finds the agent row ⇒ export +
  revision un-gate); `IntegrityError` → one re-derive, then canned error;
  add-time employment-tenure derivation from the already-fetched ORCID record,
  persisted via T5 (`source="orcid_employment"` — safe: no NCBI dependency).
- **T9** templates: `templates/manager/pi_detail.html` — pending-agent state
  keyed on the latest job ("profile generating…" / "generation failed — an
  admin can retry via impersonation or CLI" / "awaiting Slack install — ask an
  admin"), with the `/admin/agents/{uuid}` deep link rendered only when
  `current_user.is_admin` (the context already swaps the real admin back in
  under impersonation); the existing Edit Profile form gains a "JHU tenure
  start" field (writes via T5, `source="manual"`) so a manager can correct a
  derived year — no new route, allowlist untouched.
- **T10** activation gate: `admin_approve_agent` (`src/routers/admin.py:860-899`),
  BOTH branches, `pi_lab`-scoped — refuse flipping to `active` when the linked
  user's profile is missing, `evidence_state != "grounded"`, or the newest
  `generate_profile` job is dead, unless an explicit "activate anyway"
  checkbox is posted (logged with actor). `admin_agent_detail`
  (`admin.py:821-857`) loads the profile, tenure entry (+provenance), and
  latest job into the template context; `templates/admin/agent_detail.html`
  shows them beside Approve & Activate.

Docs:

- **T11** amend `docs/specs/2026-08-21-manager-pi-controls-design.md` (dated
  note: `POST /manager/pis` now also creates a pending agent; D1 scope note;
  user-account-types D7 unaffected), mark the implemented slice in the two
  2026-08-13 docs, and update CLAUDE.md's "Adding New PIs" section.

## 5. Tests (each named failure mode has a test designed to catch it)

- H1: attempt-1 fails after a paper-derived tenure write ⇒ map has NO entry
  after the failure commit; attempt-2 success ⇒ one entry, correct year,
  provenance recorded. Stage-failure runs never persist paper-derived years.
- H2: a fixture paper whose only Hopkins-affiliated author is a co-author must
  NOT set tenure; multiple-employment fixtures.
- H3: full-career-store fixture (pre-tenure rows present) ⇒ exported top-20
  excludes them, at both export sites; plus a characterization snapshot of the
  exported markdown.
- H4: activation of a profile-less / dead-job / ungrounded pi_lab agent is
  refused on BOTH branches; override works and is logged; scout_hub exempt.
- M1/M3: pipeline run with no agent row (CLI/self-signup path) uses the
  user_id key, no `None`/`"None"` map keys; legacy 62-entry map read
  correctly; re-added user (new user_id) derives fresh.
- M6/L1: concurrent same-surname add ⇒ friendly error, not 500; ORCID format
  rejection; canned error codes render.
- Corpus: stage merge, disambiguation gate on S2/S3/S4, consortium exclusion,
  EXCLUDED_TYPES cannot take slots, dedupe, rank + cap-last, D4b (unique +
  round-trip), stage exception ⇒ raise.
- Roster/inertness: pending agent excluded by `_sync_roster_from_db` and
  startup roster (new test — no existing coverage, contrary to the draft);
  unmute on a pending row is refused (pin `agent_mute.py:19`).
- Manager flow: POST /pis creates User + Job + pending agent atomically;
  duplicate ORCID creates nothing; allowlist test untouched and green;
  pipeline for a manager-created PI writes `profiles/public/{agent_id}.md` +
  revision.
- T1/T2/T3 regression tests as listed.

Run everything on the host (`.venv-test`), never through the sshfs mount
(CLAUDE.md: 100-400× slowdown; pip install from a mount corrupts the venv).

## 6. Deploy

- No DB migration. Tenure map lives in `app_settings` (KV exists).
- `$DC build blackbird-app worker` AND `$DC --profile agent build agent`
  (T1 touches `pubmed.py`, imported by the agent's literature tools). Restart
  web + worker; the agent-run restart is an owner decision (flag it — a run
  may be live; follow CLAUDE.md's stop/log/build/migrate-check/start sequence).
- Two-stack rules apply throughout (`-f docker-compose.prod.yml`, never touch
  org1, never `--remove-orphans`).
- Post-deploy verification: add one test ORCID via the manager tab; watch
  `/admin/jobs`; confirm ≤50 Publication rows, tenure entry (user_id-keyed,
  with provenance), pending agent in `/admin/agents`, export file present,
  Provision → OAuth link works, activation gate blocks a dead-job agent.
- Existing 62 PIs: untouched at deploy (no auto-regeneration). Any FUTURE
  retry/refresh now runs the corpus pipeline with explicit tenure filtering at
  synthesis + export — closing the documented "stock run reverts to
  full-career scope" hole, including the export half that re-opened before.

## 7. Out of scope (explicit)

Historical backfill / Tier-D re-slice machinery; `coverage_suspect` column +
hard corpus-coverage gate (the T10 gate covers profile-existence/groundedness/
dead-job; the corpus-thinness verdict remains future work — flagged counts are
logged and visible in job progress); a manager "regenerate profile" button
(would widen the POST allowlist); the dead-job `'failed'`-status UX defect
(pre-existing; noted); unifying the two script-side identity copies;
monthly_refresh scheduling.

## 8. Decisions needing owner sign-off

1. **JHU-associated = tenure-window rule** (recommended; the audited instance
   policy) — not per-paper affiliation matching.
2. **Include OpenAlex (S2)** in the corpus (recommended after audit: often the
   largest discovery source for sparse-ORCID PIs; all its candidates pass the
   disambiguation + authorship gate). Alternative: defer to a follow-up and
   accept thinner corpora for ORCID-sparse PIs.
3. **Synthesis window**: profile is grounded in the ≤50-corpus but the prompt
   context keeps the existing "up to 30 abstract-bearing papers" window —
   exactly how the audited 2026-08-13 regeneration was produced. Alternative:
   widen to 50 (bigger prompts, unvalidated).
4. **Tenure derivation**: employment year at add time (manager-correctable via
   the Edit Profile form); paper-derived year only from fully successful runs,
   with provenance; unknown ⇒ full career + loud flag. Alternative: require
   the manager to type a year (pre-filled) before generation runs.
5. **Bot creation in the manager route only** (recommended) — not the admin
   find-or-create path, not the CLI seeder. Alternative: every creation path.
6. **Provisioning stays admin-only** (recommended; documented rule). The
   manager page tells managers to ask an admin; admins get the deep link.
7. **Activation gate ships with this change** (strongly recommended — see H4).
   Alternative: warning-only panel (rejected by the audit as bypassable).
8. **Storage policy**: store full-career top-50, filter at synthesis + export
   (recommended; cohort-consistent and correction-recoverable). Alternative:
   store pre-filtered (creates two cohorts whose rows mean different things).
