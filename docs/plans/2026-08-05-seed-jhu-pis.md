# Plan — seed JHU PIs from `JHU_directory1_with_ORCID_v3_BBL.xlsx`

**Status:** PLAN. Nothing executed. Written 2026-08-05.
**Source:** `data/JHU_directory1_with_ORCID_v3_BBL.xlsx` (v3, compiled 24 Jul 2026; a curated 63-row subset of a 374-row JHU directory).
**Context:** the blackbird instance is now live in **DB-only** mode (`SLACK_ENABLED=false`) with the **star topology** running (hub `blackbird`/`scout_hub` + 7 PI spokes, `COHORT_ISOLATION_ENABLED=true`, `COHORT_DEFAULT_POLICY=isolated`). Live `copi` DB is at alembic `0024`.

---

## 1. What the workbook actually contains (verified)

- **Sheet1 = the PIs (63 rows).** Columns: Name, Title, Department, **Overview** (bio), **ORCID iD**, ORCID URL, **Match confidence**, Verification evidence, Candidate (unverified, all blank), BDP flag, **Funding FY19–26**, Est. total funding, Award count, Notable (honors/patents), Row source.
- **Sheet2** = a list of 10 BSPH department names (a reference list) — **not PIs; ignore.**
- **"Method and sources"** = provenance metadata; informational.

Triage of the 63 (verified against the live DB):

| Bucket | Count | Disposition |
|---|---|---|
| Valid ORCID, confidence **High** | 59 | seed |
| Valid ORCID, confidence **Medium** | 3 | seed **and flag for human ORCID verification** |
| **Already in DB** (ORCID match: Leung, Mukherjee-Clavin, E. Pearce, Kavran, J. Wang, Rebecca) | 6 | **skip** — same people already seeded/active as spokes |
| No ORCID ("Not found": Floyd Bryant) | 1 | sparse path or skip (decision below) |
| **Net NEW to seed (valid ORCID, not in DB)** | **56** | 53 High + 3 Medium |

**`agent_id` collisions (must resolve by ORCID, not name):**
- Dedup is by **ORCID**, so the 6 same-surname pilot PIs are dropped correctly (not namesakes).
- After dedup, **exactly one** genuine collision remains: a *new* PI surnamed **Pearce** vs the existing agent `pearce` (Erika Pearce). That new agent needs a prefixed `agent_id` (e.g. first-initial → `?pearce`). No internal collisions among the 56 otherwise.

---

## 2. Tooling — the important distinction

Two seeding paths exist; they are NOT equivalent:

| Tool | Creates | Uses workbook's curated fields? | Creates `AgentRegistry`? |
|---|---|---|---|
| `src.cli seed-profiles --file <orcids.txt>` | `User` rows + `generate_profile` jobs (ORCID→PubMed→LLM, worker-run) | No (ORCID-only) | **No** |
| `scripts/generate_sparsedata_user.py --file <tsv>` | `User` + `ResearcherProfile` + **`AgentRegistry`** (status `pending`), writes `profiles/public/{id}.md`; PubMed disambiguation; handles sparse/no-ORCID; **applies the first-initial collision prefix** | Yes (name + affiliation context) | **Yes** |

`seed-profiles` skips `#` comments and dedupes existing users by ORCID (idempotent for users). But it stops at `User` — **agents that will join the star need an `AgentRegistry` row**, which only `generate_sparsedata_user.py` (or `backfill_agents.py`, or web signup) creates.

**Recommendation:** use **`generate_sparsedata_user.py`** as the primary path — it is the only single tool that produces users + profiles + **agent rows with collision handling**, and it can consume the workbook's Name/Department/Overview as disambiguation context (higher-quality profiles than ORCID-alone, and it self-audits rows below an evidence floor).

---

## 3. The plan

### Phase 0 — Extract & build the seed input (no DB writes)
1. Back up the live `copi` DB first (additive change, but keep the discipline): `pg_dump -Fc` to `/home/ubuntu/blackbird-backups/<ts>-pre-jhu-seed/`.
2. Parse Sheet1 → emit a TSV for `generate_sparsedata_user.py`: `Name<TAB>ORCID<TAB>Department|Overview[<TAB>FacultyURL]`, for the **56 new** rows (drop the 6 already-in-DB by ORCID; hold the 1 no-ORCID for a decision).
3. Emit a **sidecar audit CSV** keyed by ORCID carrying the curated columns (funding, honors, confidence, verification evidence) for review/enrichment and provenance.
4. Pre-compute `agent_id`s and resolve the one **Pearce** collision explicitly (choose the prefix), so it is deliberate, not tool-guessed.

### Phase 1 — Seed (worker/LLM-driven)
5. Copy the TSV into the app container; run `generate_sparsedata_user.py --file <tsv>` (in-container; needs DB + prompts + `ANTHROPIC_API_KEY` + `NCBI_API_KEY`, all present). This creates `User` + `ResearcherProfile` + `AgentRegistry(status='pending', role='pi_lab')` and writes `profiles/public/{id}.md`.
6. Monitor: rows below the evidence floor (≥3 disambiguated papers OR a faculty page) are **audited, not persisted** — review the script's audit CSV. Expect some ORCIDs with thin PubMed coverage; the `0023` provenance columns (`evidence_pmid_count`/`evidence_pub_count`/`synthesis_validated`) flag these.
7. **Medium-confidence (3):** verify the ORCID→person match by hand before activating (the workbook's "Verification evidence" column is the starting point).

### Phase 2 — (Optional) enrich profiles with curated content
8. The standard pipeline ignores the workbook's Overview/funding/honors. Optionally append a curated block to each `profiles/public/{id}.md` from the sidecar CSV (small, bind-mounted, hot-reloaded — no restart). Improves the hub's scouting signal (funding history + honors are directly relevant to Blackbird's "external validation" and "team quality" dimensions).

### Phase 3 — Activate into the star (only if these PIs should join the running topology)
9. **Ordering is load-bearing (finding A1).** Under `policy='isolated'`, an `active` agent with **no cohort** is silenced (humans-only), not global. So for each PI to go live, in ONE atomic transaction: create its pairwise cohort `{blackbird, <agent_id>}` (+`grantbot`) **and** set `status='active'`. Never leave an agent active-but-uncohorted.
10. No Slack provisioning needed (DB-only): `_sync_roster_from_db` admits token-less agents with a `NullTransport`. The running `blackbird-agent-run` picks them up on the next ~30 s roster sync and `_recompute_allowed_sender_ids` extends the star — **no restart**.
11. Verify with the live gate probe: PI↔PI edges stay **0**; each new agent gates to `{blackbird, grantbot, self}`; `0 isolated`.

### Phase 4 — Verify & audit
12. Counts: new `User`/`ResearcherProfile`/`AgentRegistry` rows; profiles on disk; cohorts; active agents. Gate probe = 0 PI↔PI. org1 untouched (uptimes, nginx routing). Rollback = restore the pre-seed dump / deactivate the new agent rows (seeding is additive).

---

## 4. Decisions (DECIDED 2026-08-05)

1. **Scope:** seed **56** (53 High + 3 Medium). Medium rows are **flagged for manual ORCID verification** before activation.
2. **No-ORCID (Floyd Bryant):** **skipped.**
3. **Depth:** **seed + activate into the star** — create users + profiles + agents, and bring each up as a live spoke.
4. **Hub capacity:** **raise the global `active_thread_threshold`** (was 3). Proposed value **12** (hub can interview ~12 PIs concurrently; still bounded). This is `@lru_cache`d, so it requires restarting `blackbird-agent-run` to take effect. Confirm the number before execution.
5. **Cost:** 56 ORCID→profile syntheses (worker/LLM) + a 63-spoke running sim. Seed in one batch; restart the sim with a bounded per-agent budget for the first full-roster run.

## 4a. Concrete execution runbook (per the decisions above)

1. **Backup** live `copi` (`pg_dump -Fc` → `/home/ubuntu/blackbird-backups/<ts>-pre-jhu-seed/`).
2. **Build the TSV** for the 56 (drop the 6 already-in-DB by ORCID; exclude Bryant; mark the 3 Medium). Pre-resolve the one **Pearce** `agent_id` collision to a first-initial prefix. Write the curated sidecar CSV.
3. **Seed** in-container: `generate_sparsedata_user.py --file <tsv>` → `User` + `ResearcherProfile` + `AgentRegistry(status='pending', role='pi_lab')` + `profiles/public/{id}.md`. Review the audit CSV for evidence-floor rejects.
4. **Verify the 3 Medium** ORCID→person matches by hand; only keep the ones that check out.
5. **Raise the threshold**: set `ACTIVE_THREAD_THRESHOLD=12` in `.env`.
6. **Activate into the star** — for each verified new agent, in ONE atomic transaction: create cohort `hub-<agent_id>` = `{blackbird, <agent_id>, grantbot}` **and** set `status='active'`.
7. **Restart the sim** to pick up the new threshold + full roster: save logs, `docker stop -t 30 blackbird-agent-run && docker rm blackbird-agent-run`, then start `blackbird-agent-run` fresh with a bounded budget.
8. **Verify**: gate probe → 0 PI↔PI, all spokes gate to `{blackbird, grantbot, self}`, `0 isolated`; new row counts; org1 untouched.

---

## 5. Risks
- **Profile quality varies with PubMed coverage.** Some JHU BSPH faculty (policy, biostatistics, mental health) may have sparse PubMed footprints → evidence-thin profiles. The evidence floor + provenance columns surface these; don't activate a hollow profile as a live spoke.
- **Collision correctness.** Resolve `agent_id`s by ORCID identity, never by surname, or a namesake could overwrite/alias an existing agent. One known case (Pearce).
- **Medium-confidence ORCID mismatch.** A wrong ORCID→person link seeds a plausible-but-wrong profile; verify the 3 before activation.
- **org1 isolation.** All writes are to the `copi-blackbird` DB only; never touch `copi-python`.
