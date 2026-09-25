# Root-cause analysis: issues surfaced by the 2026-09-24 comment cleanup

The repo-wide comment and docstring cleanup (commits `e808d4e`, `e867969`) turned up a
series of code, test, tooling and documentation defects that it was not allowed to fix,
because it changed comments only. This document analyses every one of them.

For each issue it establishes, from evidence:

- whether the defect is real;
- how it works;
- which commit introduced it and why nothing caught it;
- what it affects;
- the smallest correct fix.

No fix is implemented here, except the one regression the cleanup itself introduced (§4).

## 1. Method

- **Catalog.** Every issue workers reported during the cleanup (38 entries, `I1`–`I38`)
  was written down as an unverified claim.
- **Investigation.** Six independent Opus 5.5 investigators each owned one group of issues.
  They traced each claim through the real call path in an exact copy of the host tree
  (HEAD `398bc4d` plus the then-uncommitted cleanup) and found the introducing commits
  with `git log -S`, `git blame` and `git show`.
- **Dynamic reproduction.** Where a claim could be settled by running code, the parent ran
  it in a private clone on the host, `/home/ubuntu/cc-rca` at `398bc4d`, using the
  repository's `.venv-test`. Each mutation was reverted before the next one. The
  experiments were:
  - mutation tests that remove the behaviour a test claims to protect, each with a
    positive control;
  - an alembic run against a throwaway Postgres;
  - a render of the live hub prompt;
  - ruff counts;
  - `git check-ignore`;
  - read-only file listings inside the running production containers.

  Full logs: [`repro.log`](repro.log) and [`repro2.log`](repro2.log).
- **Adversarial audit.** Five further Opus 5.5 auditors, with fresh contexts, tried to
  refute every finding: verdict, mechanism, attribution, severity and fix. They also
  reported evidenced findings the investigators had missed. The verdicts below are the
  post-audit ones.
- **Not done.** Production was not queried. Where an answer depends on production data it
  is marked *undetermined*.

## 2. Summary

Severity is the post-audit rating. "Fixed" means fixed on `blackbird` since the issue was
reported.

| ID | Finding | Verdict | Severity | Introduced by |
|---|---|---|---|---|
| **S1** | Every production image bakes `.env` (all secrets), 583 MB of production `pg_dump`s, `.git`, `.venv-test`; container runs as root | confirmed in running containers | **high** (rated CRITICAL C2 on 2026-08-17, never fixed) | no `.dockerignore` since the Dockerfile's `COPY . .` |
| I3 | `postflight.py` fails a correct 0050→0051 upgrade and tells the operator to **restore** | confirmed (repro) | medium | `517a564` (2026-08-06) |
| M1 | `run_migration.sh` execs into the *running old* container, so it cannot work in the documented migrate-before-serve order | confirmed (audit) | medium | `28d53b7` (2026-08-04) |
| I35 | Admin cohort banner and dev compose comment tell staff to restart **`agent-run`**, the other deployment's (org1's) production container | confirmed | medium | `a5fffb4` (2026-07-30); rename `996cca7` missed it |
| I21 | `test_roles.py` pins the *opposite* of the inline-verdict contract; the live hub prompt contradicted itself | confirmed (repro) | medium | `93702e6` (2026-08-13), re-asserted by `fd12e5e` | 
| C1 | Corpus "any stage failure raises" is false for DOI resolution and EFetch; a partial NCBI outage persists a tenure start computed from an incomplete corpus | confirmed (audit) | low–medium | `3dcc00a`/`e004121` (2026-08-25) |
| I1 | `run_migration.sh` still defaults `--target 0027` (preflight: 0051) | confirmed (repro) | low | `855bec4` (2026-08-17) bumped preflight only |
| B1 | The backup check always BLOCKs in production (dump never visible in the container) | confirmed | low | `28d53b7` (2026-08-04) |
| I2/I4 | preflight's sizing check and wording still hard-wired to the 0019–0023 chain | confirmed (repro) | low | `5fa6219`, `ffef698` (2026-08-04/05) |
| I6 | Scripts print bare `docker compose exec app` (dev stack) | confirmed | low (low–medium for `run_migration.sh`) | files predate the `blackbird-app` rename; `3e78458` sweep incomplete |
| I5 | `export_agent_roster.py` usage runs org1's image on org1's network | confirmed | low | inherited from shared `main` (`a0f4b15`) |
| I8 | Assessment detail page lists other interviews' tool calls as "Unplaced turns"; heading and cap banner inflated | confirmed (partial) | low | `ff91983` (2026-08-20); `c92272d` added `thread_ts` without the reader |
| I9 | Discussions status counts wrong under any filter; a thread with 2 decisions can be listed/exported under the wrong status | confirmed (worse than reported) | low | `a6c05cc` (2026-03-27) |
| I10 | "Last-author prioritized" synthesis context never implemented; live prompt asks the model to weight last-author papers it cannot identify | confirmed | low | `5972103` (2026-03-20) |
| I11 | `repair_pi_corpus.py` keeps a random duplicate-PMID row (docstring: "later-added") | confirmed | low (latent) | `2c07d81` (2026-09-24) |
| I12 | `idea`→`idea_crosslab` alias can never let a post through; a test pins the mapping | confirmed (repro) | low | `20065e1`, `6db09f9` |
| I13 | `review_proposal`: sequential duplicate 400 vs lost race 302; neither route checks the decision is a proposal | confirmed | low | `dfdfd7a` (2026-08-21) |
| M2′ | Concurrent `reopen_proposal` → 500 | confirmed (audit) | low | no IntegrityError handling around that commit |
| I14 | Onboarding progress always blames "no individual author match" | confirmed | low | `e004121`; widened by `2c07d81` |
| I15 | 17 functions/methods with no caller in `src/`; bots still request 4 Slack scopes only dead code uses | confirmed | low | the 2026-08-13 removal cycle kept methods + tests |
| I16 | Agent-log WARNING blames migration 0035 for a NULL `raw_verdict` (column exists since 0025) | confirmed | low | `04073fa` (2026-08-22) |
| I17 | Assessment detail page says `--fresh` wipes messages (false since 2026-08-22) | confirmed | low | `ff91983`; left stale by `4c49e62` |
| I18 | "v2 §9.4" cites a nonexistent heading (means §9, requirement 4) | confirmed | informational | `8489a31` |
| I19 | Profile-revision docs list values nothing writes (`private`, `slack_dm`, `monthly_refresh`) | confirmed | low | spec copy in `8d1c73c`; writers removed 2026-08-13 |
| I20 | Reply-lane lock-contention test is vacuous | confirmed (repro) | low | `d1162d1` (2026-08-21) |
| I22 | Live PubMed-outage test T4.4 now fails outright | confirmed | low | `e004121` (2026-08-25) |
| I23 | Floor-test route-to-incubation row passes only via fail-open | confirmed (repro) | low | `ccd6f22`; premise flipped by `f94b363` |
| I24 | Two test docstrings promise checks the bodies don't make | confirmed (repro for b) | low | `5285b81`, `8489a31` |
| I25 | e2e onboarding replay asserts "Step 3 of 4" (page says 3 of 3) | confirmed (catalog had it backwards) | low | `855be6a` |
| I27 | `test_agent_page.py` fixture: one patch inert, rationale stale; the other patch still matters | partial | low | `3a23e73`, `f6c8755`; `b40d04a` |
| I28 | `test_default_status` cannot see the default | confirmed (repro) | low | `aa38b04` |
| I29 | Concurrent-review race test never checks either response | confirmed (repro) | low | `dfdfd7a`, `309fcc1` |
| C2 | No test covers booking of a consult's own truncation retry | confirmed (repro) | low | — |
| I7a | `mutate_system.sh` inert control M12b targets a docstring that no longer exists; the harness also can't run as written | confirmed (repro) | low | `543e325` (2026-08-21) |
| I30–I34, I36–I38 | documentation drift (see §6) | confirmed | low | see §6 |
| I7, I26 | "missing" pubmed test file; live Slack tests using removed attributes | **refuted** | none | — |
| I32 | "six booking sites" in CLAUDE.md | **refuted**; the cleanup's own edit was wrong and is repaired (§4) | — | — |

## 3. High and medium findings

### S1 — Production images contain every secret and every historical database dump (high)

- **Mechanism.** `Dockerfile` does `COPY . .`, the build context is the repository root
  for all three services, and there is no `.dockerignore`. Listed read-only inside the
  running containers on 2026-09-25 (`copi-blackbird-app-1`, `-worker-1`, `-agent-1`,
  project `copi-blackbird`), each one contains:
  - `/app/.env` (6.3 KB, every secret);
  - `/app/backups/`: 583 MB, six full production `pg_dump`s, pre-0028 to pre-0043;
  - `/app/.git` (72 MB of full history);
  - `/app/.venv-test` (477 MB);
  - `/app/.superpowers`;
  - `/app/logs` and `/app/data`.

  That is 26,702 files against 819 tracked. There is no `USER` directive, so the
  application runs as root and the file mode on `.env` protects nothing. All 18 retained
  image tags (~1.8 GB each, `latest` plus `rollback-*`) were built the same way, so each
  one carries whatever `backups/`, `.env` and scratch files existed when it was built.
  Only the three running images were inspected.
- **Root cause.** This was reported on 2026-08-17 as **C2 — CRITICAL** in
  `docs/audits/2026-08-17-user-account-types/final-infra-audit.md:63-99`, with a one-line
  fix. It was never applied. Since then the baked dumps have grown from 208 MB (3 dumps)
  to 583 MB (6).
- **Impact.** Exposure is latent: the images appear to be local-only (no registry prefix,
  no push found). But code execution in the web-facing container, or any
  `docker save`/`push`, yields every credential and a full copy of the production
  database at once. It also drives image size and disk burn.
- **Fix.**
  1. Add a `.dockerignore` covering `.git`, `.venv-test`, `backups`, `logs`, `data`,
     `.env`, `.superpowers`, `.claude` and caches. Services already get their environment
     from compose `env_file:`, so dropping the baked `.env` is safe (per the 2026-08-17
     audit).
  2. Rebuild.
  3. Decide whether to delete the rollback tags that still carry the dumps.
  4. Consider a non-root `USER`.

  Verify with `docker run --rm --entrypoint ls <image> /app/backups /app/.env`, which
  should report "No such file".

### I3 — Post-migration check fails a correct upgrade and orders a restore (medium)

- **Mechanism.** `postflight.CHAIN_CREATED_TABLES` holds only the 0019–0023 tables.
  `run_migration.sh` always passes `--snapshot`, and `compare_row_counts` flags any table
  absent from the "before" snapshot that is not in that set.
- **Reproduced.** `compare_row_counts({'users':3}, {'users':3,'assessment_chat_turns':0,'assessment_chat_usage':0}, expected_new=CHAIN_CREATED_TABLES)`
  returns `(False, [...did not exist before the migration...])` (repro E10). The script
  then prints "Do NOT deploy application code … Restore path". This happens for every
  table-creating revision: 0025, 0027, 0030, 0039, 0042, 0047 and 0051.
- **Root cause.** `517a564` limited the set to `VERIFIED_REVISIONS` on purpose. The pinning
  test's rationale ("a real drop would be masked") is false, because `expected_new` is only
  consulted for tables missing from the "before" snapshot.
- **Aggravating change.** `d129d2e` later added 0042's enum types to `EXPECTED_ENUMS`
  without limiting them by revision. Postflight therefore also fails any target before
  0042.
- **Fix.**
  - Derive the expected new tables (and expected-dropped ones, for 0026's drop) from
    `planned_objects_between(snapshot.current_revision, target)`, and require the new
    tables to have zero rows.
  - Apply the same revision-scoping to the enum check.
  - Correct the test's rationale.

### M1 — The guarded migration script cannot run in the documented deploy order (medium)

- **Mechanism.** Every step runs `docker compose exec` into the *running* service. `src/`
  and `alembic/` are baked into images, and prod mounts only `profiles/` and `prompts/`.
  Under CLAUDE.md's migrate-before-serve order, the running container is the *old* image:
  - its preflight rejects the live stamp (check 1);
  - its alembic tree lacks the new revision.

  Step 1's "running current code" check passes vacuously, because every image has
  `/app/src/__init__.py`.
- **Root cause.** `28d53b7` designed the script around the dev stack's `.:/app` bind mount.
- **Fix.** Run the steps with
  `docker compose -f docker-compose.prod.yml run --rm --no-deps -v "$PWD/backups:/app/backups" blackbird-app …`
  off the freshly built image. The backup mount also fixes B1. Make Step 1 compare the
  image's `.build_info.json` commit with the host HEAD.

  Design I1, B1, I3 and M1 together: changing only the default target leaves the guarded
  path unusable.

### I35 — Instructions to restart the other deployment's production container (medium)

- **Mechanism.** `templates/admin/_cohort_gate_banner.html` tells staff to restart
  **`agent-run`**:
  - line 58 renders unconditionally;
  - line 42 renders when isolation is off.

  The banner appears on the admin-only cohorts, cohort-detail and cohort-topology pages.
  `docker-compose.yml:49-53` (dev stack) says "a bare `docker stop agent-run`" gets its
  grace period. On this host `agent-run` is org1's production simulation (CLAUDE.md
  two-stack box).
- **Root cause.** The banner text comes from `a5fffb4` (2026-07-30), written when
  `agent-run` was this repo's container. Two later sweeps renamed only CLAUDE.md:
  - `996cca7` (2026-08-05);
  - `3e78458` (2026-08-17, an explicit "stop pointing operators at the other deployment's
    agent container" sweep).

  The compose comment is inherited verbatim from org1's own compose file.
- **Fix.** Name this stack's container (`copi-blackbird-agent-1`), and say the settings
  change needs a *recreate* once `/admin/simulation` shows no live run
  (`docker compose -f docker-compose.prod.yml up -d --force-recreate agent`). Drop the
  container name from the dev compose comment. Verify:
  `grep -rn --exclude-dir=.git agent-run . | grep -v blackbird-agent-run`.

### I21 — A test pins the reverse of the verdict-disclosure contract (medium)

- **Mechanism.** `tests/unit/test_roles.py::test_visible_body_hides_the_verdict_the_sidecar_still_carries`
  says the visible reply must never carry gating, recommendation or red flags. The
  contract (thread_guidance CONCLUDE, agent-system.md, CLAUDE.md,
  `test_claude_md_disclosure_sync.py`) requires exactly that inline. The test reads only
  one byte-slice of the phase-4 template.
- **Reproduced.**
  - An inline-verdict instruction added outside the slice passes; the same sentence inside
    it fails (E5/E6).
  - Rendering the CONCLUDE prompt showed both the inline mandate and "none of it may appear
    anywhere in `<slack_message>`", a sentence that governs a list starting with "1. Gating
    criteria" (E7). It was present in every hub phase-4 turn.
- **Root cause.**
  - The contradiction was born in one commit, `93702e6` (2026-08-13, sidecar relocated
    into CONCLUDE). `23da58d` re-anchored the test mechanically.
  - `fd12e5e` (2026-08-27) edited the test and kept the false premise, five days after the
    2026-08-22 RCA documented the inline contract.
  - Nothing checked the rendered prompt.
- **Status.** The prompt half is **fixed**: `df4d975` (scout_hub 1.7.1) made the preamble
  admit the inline fields and added `test_the_sidecar_preamble_admits_the_inline_verdict`.
  The test half is **open**: `test_roles.py` still pins the old rule.
- **Fix.** Rewrite the test to render `build_phase4_prompt` at the CONCLUDE ordinal. Assert
  that the inline fields are requested, and that `weighted_score`, band and dimension
  scores are not.

### C1 — A partial NCBI failure silently thins the corpus and fixes a tenure year (low–medium)

- **Mechanism.** `corpus.py` promises that any stage failure raises `CorpusStageError`.
  Two stages cannot raise:
  - `convert_dois_to_pmids` swallows failures (`pubmed.py:500-501`, `:546-547`);
  - `fetch_pubmed_records` swallows per batch (`pubmed.py:218-222`).

  The pipeline then derives and **stores** a JHU tenure start from the incomplete corpus
  (provenance `earliest_hopkins_paper`, `profile_pipeline.py:175-180`), against its own
  "only from a COMPLETE corpus" comment. `get_tenure_start` prefers the stored value on
  every later run.
- **Root cause.** `3dcc00a`/`e004121` (2026-08-25) introduced the raising contract but
  reused the swallowing helpers.
- **Fix.** Give the corpus path raising variants (or a returned failure set), keeping the
  swallowing for `fetch_abstract`. Verify with a respx test that fails one EFetch batch or
  the ID-converter call and expects `CorpusStageError`.

## 4. Regression introduced by the cleanup, and its repair (I32)

CLAUDE.md's "`Agent.record_api_call` booked six sites" is **correct**. It counts the six
unreserved booking points at `2ef1438` and at `68e35c6`'s parent:

- the consult;
- the consult's own truncation retry;
- two turn truncation retries;
- the memory update;
- the memory retry.

A cleanup worker counted `simulation.py` references only, which leaves out the consult
retry wired in `tools.py`. It "corrected" three texts to five, seven and five, which
contradicted CLAUDE.md. The adversarial audit caught this.

The repair was applied before commit, and `e808d4e` carries the corrected text:

- `agent.py`: SIX (historical) and seven (today, with tool rounds);
- `test_api_call_accounting.py`: eight, counting both reserved turns;
- `test_rate_reservation.py`: SIX.

The same repair updated two older "six" comments that had been stale since `68e35c6` added
tool-round booking.

## 5. Low-severity findings

**Migration tooling**
- **I1.** `TARGET="0027"` (last set in `0bc93e5`). `855bec4` moved `DEFAULT_TARGET` without
  it, and no test reads the shell script. `alembic upgrade 0027` from 0050 is a silent no-op
  with exit 0 (repro E11), which Step 6 then misreports as a "silent rollback". In
  production the old image's preflight blocks first, so this fails closed.
- **B1.** The dump goes to host `backups/`, but preflight runs in `blackbird-app`, where
  `/app/backups` is only a build-time copy. So check 12 BLOCKs unless
  `--backup-verified-elsewhere` or `MIGRATE_BACKUP_DIR=profiles` is used; a root-owned
  host snapshot suggests operators worked around it.
- **I2/I4.** `POST_0019_STARTS = ("0020","0021")`. For starts 0023–0050 the sizing check
  quotes an irrelevant `agent_messages` lock window and "0019..0023 chain" text; it WARNs
  only from 410,417 rows (E9). It also wrongly includes 0020, from which 0021 still builds
  an `agent_messages` index. The pinning test pins the stale tuple.
- **I6.** Bare `docker compose exec app` appears in many script docstrings and in text
  printed at runtime. `3e78458`'s message claimed the `-f` fix, but its diff changed two
  echo lines. `run_migration.sh:113,122` print `up -d --build $SVC` (default `app`).
- **M2.** Stale `blackbird-agent-run` names in `run_migration.sh`, `preflight.py` (an `&&`
  chain means the app/worker stop never runs) and `provision_slack_bots.py`.
- **M4.** `export_agent_roster.py` run in `blackbird-app` writes `data/agent_roster.json`
  where there is no `./data` mount. The host script then silently reads a stale root-owned
  roster.
- **I5.** Usage text for org1's network and image, inherited from the shared `main`. It
  most likely fails closed (different database credentials).

**App and engine**
- **I8.** `_load_tool_turns` filters by run/phase/agent/channel/time window, not by
  `thread_ts`.
  - The panel counts are unaffected.
  - The "Unplaced turns (N)" heading and the cap banner are inflated.
  - Crowding out this interview's own turns needs ~32 other interviews inside its window.
  - The test still says the log table "cannot express" a thread.
- **I9.** Pass-1 filtering, then orphan reconstruction from the filtered list, then counting
  over the mixture. A thread whose first decision matches a status filter and whose last
  does not is listed and exported under the wrong status. Fix: delete the first filter
  pass.
- **I10.** `Publication.author_position` is never written; the live synthesis prompt asks
  for last-author weighting the context cannot support.
- **I11.** `order_by(Publication.id)` over a uuid4 key; latent (no duplicates measured).
- **I12.** Remove the alias. E8: removing it breaks only the test pinning the mapping. Do
  not repoint it to `pitch`.
- **I13 / M2′.** Unify the duplicate-review behaviour. Wrap `reopen_proposal`'s commit in
  `IntegrityError` handling. Both routes should check `td.outcome`.
- **I14.** Build the parenthetical from the actual flag reasons. `2c07d81` also merged
  `s4_affiliation_mismatch` into `bare_initial_unconfirmed`, mislabelling full-forename
  S4-only misses.
- **I15.** Dead code:
  - `_hydrate_thread_from_db`, `_owes_reply`, `get_last_bot_sender_in_channel`;
  - `create_private_channel`, `invite_to_channel`, `send_dm`, `poll_dm_messages`,
    `open_dm_channel`, `get_thread_replies`, `resolve_user_name`, `aconnect`;
  - `slack_web.list_channel_ids`, `post_message`, `join_channel` and their async
    wrappers, `get_user_info_async`;
  - `slack_globally_enabled`, `get_agent_bot_token`, `get_revision_history`.

  The last src callers were removed in `23da58d`, `e541706`, `2e4b448`, `d1ef831`,
  `855be6a` and `b40d04a`. Some of these symbols never had a caller. Bots are still
  provisioned with `groups:write`, `im:history`, `im:write` and `groups:read`, which only
  dead code, the live-Slack test tier and test scripts use. Nothing detects dead code:
  ruff has no unused-function rule, and coverage counts test-only code.
- **I16/I17.** Correct the runtime log string and the rendered page text. The adjacent
  comments were already corrected by the cleanup.
- **I19.** Mark `private` and `slack_dm` as historical values, not deleted ones.
  `monthly_refresh` is still a job type, just never a revision mechanism.

**Tests and tooling**
- **I20.** Reproduced with E2: every contended acquisition deadlocks, yet the test passes,
  and the positive control fails. Make the test contend at the only remaining await inside
  the lock span.
- **I22.** T4.4 fails when T4.1 ran first. Separately, committed history between `3dcc00a`
  and `c62af04` does not import, a bisect hazard.
- **I23.** Only the route row is vacuous (E3/E4). Arm it with a non-empty consult map.
- **I24.** The `test_cli.py:887` self-check reads back its own fixture's patch. The
  cohort-view test passes with an empty listing (R1).
- **I25.** Fix `test_browser_flows.py:510` and `tests/e2e/README.md:211` to "Step 3 of 3".
- **I27.** Drop the inert `slack_enabled` patch and re-document the `get_slack_tokens` stub.
- **I28.** Passes with `default="accepted"` (E14).
- **I29.** Passes when the lost race returns a 500 (R2).
- **C2.** Unit suite plus consult capture: 2919 passed with the consult retry unbooked (R3).
- **I7a.** M12b's target string is gone (E12), so the pubmed tier loses its inert control
  under `LIVE_API_TESTS=1`. The harness also runs `docker compose exec app` against an
  image with no pytest.

## 6. Documentation drift (low)

- **I30.** The 0036 box's "Four things to expect afterwards" sit inside the 0037 box.
  `3cdb7f5` inserted 0037 between the 0036 body and its tail (written in `86f325d`).
- **I31.** The "3 of 12" evaluation is dated 2026-09-03 (`eval-results.json`); line 964
  says 2026-09-02, the audit directory's date.
- **I33.** The 0038 migration docstring says `thread_panel.py` names none of the new
  columns. `46d9a99` added `read_state` hours after `b648be5` wrote that. CLAUDE.md is
  corrected; the migration is not.
- **I34.** `nginx.conf`'s `/_next/static/` block is Next.js boilerplate from `0d3aebc`,
  wrong from the start. The file is probably not live: its upstream `app:8000` cannot
  resolve the renamed service, and org1's nginx serves the site.
- **I36.** `.gitignore:46` ignores `static/`, so new hand-written assets are silently
  untracked (R5). It is safe to remove today, since host `static/` matches the tracked
  files.
- **I37.** The "140 existing B008s in this file" claim is wrong: `admin.py` has 66, `src`
  has 134, and headroom is 16, not ~3 (R4). It is likely a misattributed src-wide figure.
- **I38.** Four texts say "impersonation refuses nothing" or "nothing is hidden". Assign,
  unassign, suggestions generate, suggestion status, every chat route, self-delete and
  admin delete all refuse, and several controls are hidden. `4a04b61` dropped a qualifier
  that was never sufficient. This matters because it is a security-policy statement.

## 7. Refuted claims

- **I7.** `tests/live_api/test_pubmed_live.py` exists (tracked since `58f181f`).
- **I26.** `AgentState.last_seen_cursor` (since `497ec82`) and
  `SimulationEngine._owes_reply` both exist (E13). The live tier is skipped without
  `SLACK_TEST_*` credentials.
- **I32.** See §4.

## 8. Cross-cutting root causes

1. **Removals that delete call sites but keep everything else.** The 2026-08-12…15
   removal cycle deleted callers and left:
   - the methods (I15);
   - their tests, which keep coverage green;
   - their Slack scopes;
   - their comments and docs (I17, I19, I27).
2. **One value in two places with no test.** `run_migration.sh` `TARGET` vs preflight
   `DEFAULT_TARGET` (I1); `POST_0019_STARTS` and `CHAIN_CREATED_TABLES` vs the growing
   start set (I2, I3).
3. **Tests that pin a proxy instead of the behaviour.** A template slice (I21), "no
   exception raised" (I29), a fixture's own patch (I24), an argument read back (I28), a
   mapping table (I12), a fail-open path (I23), an await that moved (I20). Each passed
   through the change it was meant to catch; mutation testing exposed all of them.
4. **Prose that nothing reads.** Operator runbooks, migration docstrings, compose and nginx
   comments, admin template copy and CLAUDE.md sections outside the two sync-tested
   anchors (I30–I38, I16, I17, I35).
5. **The host-only compose rename.** `blackbird-app` exists only in the uncommitted
   working-tree compose file, so committed code and docs keep naming `app`, and nothing can
   flag them (I6, I35, M1, M4).
6. **Known findings not remediated.** S1 was rated CRITICAL on 2026-08-17. I5 was flagged
   the same day.

## 9. Residual and undetermined

- Whether production holds:
  - `thread_decisions` threads with more than one outcome (I9);
  - legacy `outcome='proposal'` rows (I13, M2′);
  - pre-2026-08-13 `private` / `slack_dm` revision rows (I19);
  - superseded rows with NULL `raw_verdict` (I16).
- Whether this repo's nginx container is live (I34), and org1's `agent-run` StopTimeout
  (I35).
- Slack scope rules: whether `groups:read` is needed, and whether posting to a user ID
  needs `im:write`. These were not verified against Slack's documentation.
- Hub compliance with the fixed prompt set 1.7.1 is unmeasured.

## 10. Security note outside the RCA scope

During this work, another session's new SSH connection to the host failed strict host-key
checking:

- that connection went over a NAT64-synthesized IPv6 address, `64:ff9b::315:2193`, which
  DNS now returns alongside the A record `3.21.33.147`;
- it was offered `SHA256:2emoXcWCTg6aWhF47kByPQO6UGMEj0YmvH+ydOByS5k`;
- `known_hosts` holds `SHA256:l6Ew6hPhuB5SMTDbvOXu8HCQhcnYSGQB/l2A6hiN+ic`.

A fresh IPv4 connection presents the known key. No session accepted the new key. Verify
the host key out of band, and keep SSH on IPv4 (`ssh -4`, or `AddressFamily inet`) until
this is understood.
