# RCA remediation plan: fix every root cause from the 2026-09-24 audit

- **Source of truth:** [`docs/audits/2026-09-24-comment-cleanup-rca/README.md`](../audits/2026-09-24-comment-cleanup-rca/README.md)
  ("the RCA"). Finding IDs (S1, I1…I38, M1, M2, M2′, M4, M5, B1, C1, C2, I7a) are the RCA's.
- **Base:** `blackbird` @ `bc68025`.
  - The RCA and the package designs read the tree at `91f34f1`.
  - The other session's commits `91f34f1..bc68025` (3750911, 792b62d, bc68025: rubric
    3.5.0, scout_hub 1.8.0) then changed 36 files (`git diff --stat 91f34f1 bc68025`). Among
    the files this plan references, they include `src/agent/simulation.py`,
    `src/routers/admin.py`, `src/services/assessment_detail.py`,
    `templates/admin/_assessment_detail_body.html`,
    `tests/integration/test_assessment_detail_page.py`, `CLAUDE.md` (+45 at `:1666`) and
    `prompts/roles/scout_hub/phase4-thread-reply.md`.
  - Every file:line below was re-mapped to `bc68025` through `git diff -U0 91f34f1 bc68025`,
    and no referenced line fell inside a changed hunk.
  - The anchors the guards depend on were re-checked at `bc68025`: the harness counts, the
    citation lines and the phase-4 prompt sentences.
  - If the base moves again before execution, repeat that re-mapping over
    `<this base>..<new HEAD>`, taking the file list from `git diff --stat`.
- **Execution:** `/engineering:plan-execution`. The packages below have disjoint write sets
  (at most 15 files each). Implementers do not build, test, lint or commit. One integrated
  verification pass follows (§5), then an adversarial audit (§6).
- **Design provenance:** every package was designed by an independent Opus 5.5 architect
  reading the tree at `91f34f1` (re-mapped as above), or in the main session from direct reads. The specs below
  keep their file:line evidence.

## 1. Scope

In scope:
- every confirmed or partial RCA finding, low severity included;
- the auditors' additional findings (M1, M2, M2′, M4, M5, B1, C1, C2, I7a), plus four found
  while designing:
  - ORCID step-1 failure tenure path (C1b);
  - `prompts/daily_audit.md` targets org1 (G12);
  - harness anchors that are already unrunnable, or become so under P11d (I7b: `mutate_cohorts`
    M4 and M7, `mutate_slack_mirror` S1);
  - the e2e README runs pytest inside an image without pytest (I25b);
- one guard test per cross-cutting root cause (RCA §8), so each defect class fails CI if it
  recurs. §8 cause 5 (the host-only compose rename) is the exception: D5 keeps the file
  uncommitted, so it is recorded as open by decision;
- the existing tenure rows that C1 may have thinned, re-derived automatically with the
  affected profiles refreshed (D7, P16).

Out of scope:
- the refuted claims (I7, I26, I32's original claim);
- production data changes, except P16's tenure re-derivation and profile refresh (D7);
- reinstalling existing Slack bots (D17, a tracked follow-up);
- the non-root container user (D4, a tracked follow-up);
- committing `docker-compose.prod.yml` (D5);
- secret rotation (D3: the images never left the host).

## 2. Decisions (settled with the owner, 2026-09-25)

The owner answered every decision on 2026-09-25. ★ marks an answer that departs from the
draft's recommended default; the plan below is written against these answers.

| # | Decision | Answer | Affects |
|---|---|---|---|
| D1 | Delete every pre-fix `copi-blackbird-*` image. Each one bakes `.env`, and since the dumps landed most also bake the prod dumps. On 2026-09-25 that was 18 `rollback-*` tags, the stale `copi-blackbird-app:latest` and `copi-blackbird-grantbot:latest`, and the three current `:latest` images once §7 rebuilds them, plus `rollback-pre-rca` and this project's dangling images. Irreversible, and it removes other sessions' rollback points. | **Delete all, one healthy day after deploy.** First tag the verified post-fix images `rollback-post-rca`. Delete by explicit tag or ID, never `docker image prune -a` / `system prune -a` (those hit org1). | §7 step 11 |
| D2 | Clear the build cache, which still holds the old `COPY . .` layers. | **Yes, right after D1.** Review `docker buildx du --verbose` first, then run `docker builder prune -a`. It is daemon-wide, so org1's next build is cold once; no data is lost. | §7 step 11 |
| D3 | Rotate the secrets baked into the pre-fix images. | **No rotation.** The owner confirmed that no copi-blackbird image has ever been `docker save`d, pushed, copied off the host or shared. The exposure stays latent and ends with D1/D2. | — |
| D4 | Non-root `USER` in the Dockerfile. | **Defer** to a separate change. It needs a fixed UID, a `chown` of the host `profiles/` and `data/`, and an inventory of write paths. Tracked in the findings register (P13). | — |
| D5 ★ | Commit the host-only `docker-compose.prod.yml` edits. | **Keep it uncommitted.** Consequences: there is no step-0 commit. P14 drops its committed-compose test, which would fail in any clean clone because the committed file still names `app`. CLAUDE.md keeps its "UNCOMMITTED edit" box (P13 G7 is limited to line refreshes). Images report `dirty_files = 1`. RCA §8 cause 5 stays open by decision, with a register row. | P13, P14, §7 |
| D6 | The guarded migration path (`run_migration.sh`). | **Redesign it** (P4). It becomes deploy step 4, rehearsed first without `--apply`. | P3, P4, P13 G5 |
| D7 ★ | Tenure years stored with source `earliest_hopkins_paper` that may come from a silently thinned corpus. | **Re-derive automatically, and refresh the outputs.** New package P16 adds `scripts/rederive_tenure_starts.py`, run once after deploy. It re-derives every `earliest_hopkins_paper` row against the strict corpus and overwrites it on disagreement. It never touches `manual` rows, and it skips (with no write) a PI whose ORCID or corpus fetch fails. For each changed PI it queues one `generate_profile`, whose pipeline also queues the grants and industry refresh (`profile_pipeline.py:548-549`). That is about one synthesis LLM call per changed PI, which the owner accepted. It previews by default, writes a backup before `--apply`, and aborts above `--max-changes` (default 10). | P16, §7 step 9 |
| D8 | ORCID step-1 failure persists a paper-derived tenure year (C1b). | **Use it, don't store it** (P6). | P6 |
| D9 | I10 "last-author" weighting. | **Strike the claim** (P6 prompt, P13 specs). No `role.toml` bump is needed. | P6, P13 |
| D10 | I13 duplicate proposal review. | **302 to the dashboard** for both a sequential duplicate and a lost race. | P9, P11b |
| D11 | I15 dead code. | **Delete**, with its tests, fakes and docs. | P11a–e |
| D12 | The "grandfathered thread loses reactive priority" rule. | **Retire it.** `grandfathered` becomes reporting-only, and the spec and banner say so. | P2, P11c, P11d |
| D13 ★ | Slack scope `groups:read` (and the unverified `chat.getPermalink`, `auth.revoke` and `im:write` needs). | **Check Slack's docs, then decide**, before fan-out (§4 step 0). If no method the code calls needs `groups:read`, P11b drops it and P11a removes the unused `include_private` parameter. Otherwise it is kept, with the method cited. **Outcome (docs check, 2026-09-25):** `groups:read` is needed only by `conversations.list`/`info`/`members`/`open` and `users.conversations` on private channels (https://docs.slack.dev/reference/scopes/groups.read), and no live code lists private channels. So it was dropped, with `include_private`, leaving `BOT_SCOPES` at 8. `chat.getPermalink` and `auth.revoke` need no scope. `chat.postMessage` to a user ID needs only `chat:write`. | §4 step 0, P11a, P11b |
| D14 ★ | `scripts/spike_private_channels.py`, a spike for a removed feature. It calls `slack_sdk` directly and has no importer. | **Delete it** (P5). | P5 |
| D15 | Dead-code guard scope. | **All of `src/`**, with a reason required per allowlist entry. Narrow to `src/agent` + `src/services` if there are more than ~30 genuine false positives. | P15 |
| D16 ★ | `prompts/daily_audit.md`, which hard-codes org1's `/home/ubuntu/copi-python` ×6. The host's only daily-audit cron is org1's own, which uses org1's copy. | **Delete it** (P13). | P13 |
| D17 | Reinstall the existing Slack bots so they drop the excess scopes. On 2026-09-25, 11 active and 62 inactive agents hold bot tokens. | **Later, as a tracked follow-up** with a register row. The reinstall procedure (manifest, token swap, roster sync) is designed and tried on one bot first. | P13 register |
| D18 ★ | The credentialed tiers, which `ci.sh` skips. | **No-spend tiers only.** Run T4.4 unmutated, and the live ORCID/NCBI tiers of `mutate_system.sh` without `pipeline`. Force `ANTHROPIC_API_KEY` to a dummy value so no call can spend. Skip T4.4's mutant, the live Slack files (P11c) and `mutate_slack_mirror.sh`, and report them as unverified. | §5 step 10, P12b |

## 3. Coverage matrix

Every RCA finding maps to exactly one owning package. Fixes whose text appears in several
files are split by file, as noted.

| Finding | Package(s) |
|---|---|
| S1 | P1 (+ P13 G4 docs, §7 deploy) |
| I1, B1, M1 | P3 (python half), P4 (script), P13 G5 |
| I2, I4, M5, I3 | P3 |
| I6 | P3 (preflight/postflight strings), P4 (`run_migration.sh`), P5 (other scripts), P2 (`export_agent_roster`/`provision_slack_bots`), P6 (`repair_pi_corpus.py:44`) |
| M2 | P3 (preflight), P4 (`run_migration.sh`), P2 (`provision_slack_bots`) |
| M4, I5 | P2 (+ P13 G6) |
| I35 | P2 |
| I34, I36 | P1 |
| C1, C1b (D8), I10, I11, I14 | P6 (+ P13 specs for I10) |
| C1's already-stored tenure rows (D7) | P16 |
| I8, I17 | P7 |
| I9 | P8 |
| I13, M2′, I29 | P9 |
| I18 (admin half), I33, I37, I38 | P10 |
| I15, I27, scopes | P11a, P11b, P11c, P11d, P11e |
| I12, I16, I18 (simulation half) | P11d |
| I19 | P11e (code), P13 G9 (specs) |
| I20–I25, I25b, I28, C2 | P12a |
| I7a, I7b, mutation harness | P12b |
| I30, I31, I32-clarification, M3-plan erratum, G11 profile comment, G12 | P13 (G11 lives in P6, which owns `src/models/profile.py`) |
| RCA §8 guards | P1 (the S1 class: tracked-tree-only images), P2 (§8.4 foreign names), P3/P4 (§8.2), P6 (§8.3/§8.2), P11b (scopes), P11e (§8.1 docs), P12b (§8.3 vacuity tier and harness-target guard, §8.4 citation guard), P13 (§8.4 CLAUDE.md; §8.6 findings register), P14 (§8.4 operator text), P15 (§8.1 dead code). §8.5 is open by D5, as a register row. |

## 4. Execution order

0. **Main session, before fan-out: the Slack scope check (D13).**
   - Use the `docs-check` skill (official Slack API docs) for every method the code calls.
     `METHOD_SCOPES` (P11b) lists them; add `chat.getPermalink`, `auth.revoke`, and
     `chat.postMessage` to a user ID.
   - Record the scope each one needs, and whether any needs `groups:read` for a private
     channel.
   - Write the answer into P11a/P11b before dispatch:
     - drop `groups:read` and `include_private` if nothing needs them;
     - otherwise keep `groups:read` and cite the method;
     - either way, fill in the `chat.getPermalink` / `auth.revoke` rows.
   - D5 = keep uncommitted, so there is no compose commit here.
1. **Fan out every package, P1 through P16, in parallel**, up to the concurrency limit. All
   write sets are disjoint. Interfaces between packages are stated in each package.
2. **Integrate (§5), then audit (§6), then deploy (§7).**

**Prompt hazard.** Editing `prompts/profile-synthesis.md` (P6) or `prompts/daily_audit.md`
(P13) in the LIVE host tree takes effect immediately, because both are bind-mounted and read
per use. Implementers must work in a scratch clone or worktree, never in
`/home/ubuntu/blackbird-copi-science` directly. Land into the host tree only in §7.

## 4a. Work packages

Each package lists:
- the files it owns, exclusively;
- the change;
- the tests;
- acceptance criteria;
- deploy notes.

Line numbers are at `bc68025`. Before editing, locate each target by content, since sibling
packages may shift nothing in your files, but the base can move.

### P1 — Image contents and repo hygiene (S1, I36, I34)

**Owns:**
- `.dockerignore` (new)
- `Dockerfile`
- `src/services/build_info.py`
- `tests/unit/test_build_info.py`
- `tests/unit/test_docker_build_context.py` (new)
- `.gitignore`
- `tests/unit/test_repo_hygiene.py`
- `nginx/nginx.conf`

**S1 change.**

The obvious one-line `.dockerignore` from the 2026-08-17 audit would **break every
build**. `Dockerfile:24-28` runs `scripts/write_build_info.py`, which shells out to `git`
(`:17-33`) and exits 1 on failure (`:35-37`). So:

1. **Create `.dockerignore`**, with a header comment stating the rule:
   - Exclude only untracked paths. An excluded tracked path would count as deleted in the
     builder's `git status --untracked-files=no`, which inflates `dirty_files`.
   - Keep `.git`: the builder stage needs it, and deletes it.
   - Pinned by `tests/unit/test_docker_build_context.py`.

   Contents:
   ```
   .env*
   !.env.example
   .provision_state.json
   certbot
   backups
   data
   logs
   profiles
   .venv
   .venv-test
   venv
   .superpowers
   .claude
   .notes
   .playwright-mcp
   docs/superpowers
   .mutmut-cache
   mutants
   .pytest_cache
   .ruff_cache
   htmlcov
   .coverage
   build
   dist
   *.egg-info
   **/__pycache__
   **/*.pyc
   .idea
   .vscode
   .build_info.json
   ```
   These paths must stay in the context:
   - `templates/`, `static/`, `prompts/`, `alembic/`, `alembic.ini`, `pyproject.toml`, `src/`;
   - `scripts/`, needed for `write_build_info.py` and the in-image `scripts/migrate/*` and
     `enqueue_enrichment.py`;
   - `.git`, needed by the builder stage only.

   Excluding `data/`, `logs/` and `profiles/` is safe:
   - no `src/` code reads `data/` or writes `logs/`;
   - `profiles/` is bind-mounted into all three prod services;
   - the agent mounts `./data`;
   - `Dockerfile:31` still runs `mkdir -p profiles/public profiles/private prompts logs static`.
2. **Rewrite the `Dockerfile` as two stages**, keeping the rationale comment from `:19-23`,
   reworded:
   ```
   FROM python:3.11-slim AS source
   WORKDIR /app
   RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
   COPY . .
   RUN git config --global --add safe.directory /app \
       && git clean -ffdx \
       && python scripts/write_build_info.py \
       && rm -rf .git

   FROM python:3.11-slim
   WORKDIR /app
   RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev && rm -rf /var/lib/apt/lists/*
   COPY pyproject.toml .
   COPY src/ src/
   RUN pip install --no-cache-dir .
   COPY --from=source /app/ /app/
   RUN mkdir -p profiles/public profiles/private prompts logs static
   EXPOSE 8000
   CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
   ```
   Two layers, for two reasons:
   - **`.dockerignore`** keeps secrets and dumps out of the build context. Without it they
     would still reach the daemon and persist in the source stage's `COPY . .` cache layer.
   - **`git clean -ffdx`** makes the image hold exactly the tracked tree plus generated
     files. The deny-list alone would bake the next untracked file anyone drops in the
     checkout, such as a root-level `*.dump` or a scratch token file (RCA §8.6: a known
     class left open).

     It runs before `write_build_info.py`. `dirty_files` counts tracked changes only
     (`--untracked-files=no`), so the count is unchanged.

     Measured 2026-09-25: `git status --ignored --porcelain` shows nothing untracked or
     ignored under `src templates static prompts alembic scripts`, and `static/` holds 4
     files, all tracked. So the clean removes nothing the image reads today.
3. **`src/services/build_info.py:12-16`**, docstring only:
   - the `.git` fallback now serves only host-checkout processes;
   - images never carry `.git`;
   - remove "there is deliberately no `.dockerignore`".
4. **`tests/unit/test_build_info.py:1-4`**, docstring only: the same correction.

**S1 tests.** New `tests/unit/test_docker_build_context.py`, with no Docker needed:
- `test_dockerignore_excludes_secrets_and_production_data`:
  - `.env*`, `backups`, `data`, `logs`, `profiles`, `.venv-test`, `.superpowers`, `.claude`,
    `certbot` and `.provision_state.json` are all present;
  - the only negation line in the file is exactly `!.env.example`, so no later `!` line can
    re-include something a line above excludes. That is what makes a presence check sound.
- `test_dockerignore_keeps_what_the_image_reads`: no positive pattern equals, or globs,
  `*`, `**`, `.git`, `src`, `templates`, `static`, `prompts`, `scripts`, `alembic`,
  `alembic.ini` or `pyproject.toml`.
- `test_dockerignore_excludes_only_untracked_paths`:
  - skip if there is no `.git`;
  - for each positive pattern, run `git ls-files -z -- ':(glob)<p>' ':(glob)<p>/**'`;
  - subtract the `!` patterns;
  - the result must be empty.
- `test_final_stage_never_copies_the_build_context`: parse the Dockerfile into stages.
  - The final stage has no `ADD`.
  - Every `COPY` in the final stage is either `--from=source`, or copies exactly
    `pyproject.toml` or `src/`, the pip layer. An allow-list, so that `COPY ./ /app` or
    `COPY . /app` fail as surely as `COPY . .`.
  - The `source` stage has a single `RUN` containing `git clean -ffdx`,
    `write_build_info.py` and `rm -rf .git`, in that order.

All four fail today: there is no `.dockerignore`, and `Dockerfile:17` is a single-stage
`COPY . .`.

**I36.**
- `.gitignore:44-46`: change the comment to `# Generated/scratch data (not source)` and
  delete the `static/` line.
- `tests/unit/test_repo_hygiene.py`:
  - module docstring "Two" becomes "Three invariants", with a new item 3;
  - new `test_static_sources_are_not_ignored`:
    - skip if there is no `.git`;
    - control: `_check_ignore("src/main.py") == 1`;
    - then `static/js/new_asset.js` and `static/css/new.css` must both return 1.

  The test fails today: repro R5 shows `.gitignore:46:static/`.

**I34.** Delete `nginx/nginx.conf:166-171`, the "Cache Next.js static assets" comment and
its `location /_next/static/ {…}` block. After that, `/_next/*` falls to `location /`, which
has `limit_req` and the server-level headers.

**Acceptance.**
- The new unit tests pass.
- After a rebuild (§7), each image has none of the following, and no `git` binary:
  `/app/.env`, `/app/backups`, `/app/.git`, `/app/.venv-test`, `/app/data`, `/app/certbot`,
  `/app/.superpowers`.
- `/app/.build_info.json` `commit` equals the host HEAD, and `dirty_files` equals the host
  `git status --porcelain --untracked-files=no | wc -l`.
- Every regular file under `/app` is a tracked path, `.build_info.json`, or one of pip's
  in-tree artifacts (`build/`, `*.egg-info`, `__pycache__`). This is checked in §5's image
  smoke build, before production.
- `git check-ignore static/js/x.js` exits 1.
- `grep -c _next nginx/nginx.conf` returns 0.

**Deploy.**
- Rebuild the app, worker and agent images (§7).
- The effective settings must be unchanged. Check with the settings-hash probe in §7.
- **No nginx reload, and never touch `copi-python-*`.** Production traffic goes through
  org1's nginx container (`copi-python-nginx-1`, `SECOND_INSTANCE_SETUP.md:260`), whose
  config is org1's file (CLAUDE.md, "Streaming through org1's nginx").
- This repo's `nginx/nginx.conf` is not loaded by any running container. Its upstream
  `app:8000` cannot resolve. I34 is therefore repo hygiene, so a future deploy of this file
  cannot bring the bypass back.
- Confirm with `docker ps --filter label=com.docker.compose.project=copi-blackbird --format '{{.Names}}'`,
  which lists no nginx. On 2026-09-25 it listed agent, app, worker and postgres only, and the
  host's one nginx was `copi-python-nginx-1`. If one is ever listed, stop and ask; do not reload it by hand.

### P2 — Foreign-deployment hazards and roster export (I35, I5, M4, M2 in `provision_slack_bots`)

**Owns:**
- `templates/admin/_cohort_gate_banner.html`
- `docker-compose.yml`
- `specs/cohort-system-v2.md`
- `tests/unit/test_cohort_gate_banner_copy.py` (new)
- `tests/unit/test_no_foreign_deployment_names.py` (new)
- `scripts/export_agent_roster.py`
- `scripts/provision_slack_bots.py`
- `tests/unit/test_provision_roster.py` (new)

**I35 semantics.**
- Cohort settings come from `Settings` (`config.py:418,429`).
- `.env` is resolved when the container is created.
- The supervisor restarts in the same container (`supervisor.py:164-171`), so a change needs
  a **recreate**.
- The banner reads the web tier's settings (`admin.py:1556-1557,1572-1573`).

**I35 changes.**
- `_cohort_gate_banner.html:40-43`, the isolation-OFF branch. Replace the `agent-run`
  restart sentence with:
  `turning it on means setting <code>COHORT_ISOLATION_ENABLED=true</code> in <code>.env</code> and <em>recreating</em> the <code>agent</code> container once <a href="/admin/simulation">/admin/simulation</a> shows no live run; a restart keeps the old environment.`
- `_cohort_gate_banner.html:56-58`. Replace "requires restarting `agent-run`" with the
  following. Spell every command out, since an operator pastes it:
  `means editing <code>.env</code>, then, once /admin/simulation shows no live run, <code>docker compose -f docker-compose.prod.yml --profile agent up -d --no-deps --force-recreate agent</code>, and <code>docker compose -f docker-compose.prod.yml up -d --no-deps --force-recreate blackbird-app</code> for this banner to show the new value.`
  Keep "Only the topology is live."
- `_cohort_gate_banner.html:53` (D12). "…but it loses scheduling priority once its partner
  leaves the cohort" becomes "…and it is scheduled like any other open thread (since the
  two-lane scheduler, being grandfathered only marks it in the cohort topology report)".
- `docker-compose.yml:49-53` (the dev stack):
  - the last sentence becomes "…so a bare `docker stop` of this service's container gets it too.";
  - add "(Production uses 420s; see CLAUDE.md.)".
- `specs/cohort-system-v2.md:691`: "requires restarting `agent-run`" becomes "requires
  recreating the agent container (compose resolves `.env` at container creation; see
  CLAUDE.md)".
- `specs/cohort-system-v2.md`, D12. Mark the reactive-priority rule retired wherever the spec
  states it as normative. Keep each heading and number, because P12b's citation guard
  resolves `§8`.
  - `:546`, §8 item 3: "Grandfathered threads are **excluded from the reactive-priority
    tier**" becomes a struck statement plus "**Retired (2026-09-25, D12):** the two-lane
    scheduler has no reactive tier; `grandfathered` is reported in `cohort_topology_snapshot`
    only".
  - `:641`: drop "and excluding grandfathered threads (§8)".
  - `:933`: the "does **not** win the reactive tier" bullet gets the same retirement note.
  - `:1044`: the test-table row names `test_grandfathered_thread_still_concludes`, which is
    P11d's rename, and the behaviour "still concludes".
  - `:309`, the `has_new_reply_from_other` row. Its "Must take the gate … feeds both
    `_owes_reply` (scheduler)" text becomes: "Ungated by design in the reply lane:
    `_pending_reply_pairs` passes `allowed_sender_ids=None` (`simulation.py:1672-1675`).
    `_owes_reply` was removed (D12)." Do not say the reply lane feeds a gated read.
  - `:898` ("cover the filter, the recompute, `_owes_reply`"): mark `_owes_reply` "removed
    (D12)".
  - `:528-531`, the "Confirmed defect" paragraph about the reactive tier, and `:550-552`, §8
    item 4 ("the caller passes `None` when the thread is already open and
    non-grandfathered"), get the same "Retired (D12)" note. Both become false once the rule
    is retired.
  - P11d deletes the function, and a spec that still names it repeats RCA §8 cause 1.
    Afterwards, grepping the spec for `_owes_reply` and "non-grandfathered" returns only
    lines marked retired or removed.
  - Afterwards, `grep -n "loses scheduling priority\|excluded from the reactive-priority" templates specs`
    returns only the struck, retired line.
- Leave these alone:
  - descriptive mentions: `tests/e2e/README.md:201`, `tests/characterization/test_agent_turn_gm.py:57`;
  - `docs/production-migration.md`, whose runnable flows P4 rewrites;
  - explicit warnings in `CLAUDE.md`, `README.md:66-67`, `provision_slack_bots.py:498-500`,
    `preflight.py:1772,1925` and `run_migration.sh:331`.

**I5 and M4: roster export.**
- `scripts/export_agent_roster.py:11-18` (docstring). Replace both usage forms with:
  - the host command
    `docker compose -f docker-compose.prod.yml run --rm --no-deps -T -v "$PWD/data:/app/data" blackbird-app python scripts/export_agent_roster.py`
    (the web service has no `./data` mount, so the run binds one);
  - the warning: "Never attach a one-off container to the other deployment's network, or run
    its image: on this host both belong to org1". The literal names would fail this
    package's own foreign-name guard, since `export_agent_roster.py` is not on its
    allowlist.
- `scripts/provision_slack_bots.py`:
  - The `:11-12` docstring and the `:113-116` missing-roster message give the same export
    command.
  - `load_roster(max_age_s: float | None = ROSTER_MAX_AGE_S)`, with `ROSTER_MAX_AGE_S = 3600`:
    - if the file is older than `max_age_s`, raise `RuntimeError` naming its age and the
      export command;
    - always `console.print` the roster's age.
  - New `--allow-stale-roster` flag. The call at `:274` becomes
    `load_roster(None if args.allow_stale_roster else ROSTER_MAX_AGE_S)`.
  - M2: replace the `:497-517` closing block. Today it says to stop and remove
    `blackbird-agent-run`, `up -d --build`, then start a CLI run. The new block prints:
    - "Import the tokens into AgentRegistry (the DB column is authoritative):";
    - `docker compose -f docker-compose.prod.yml run --rm --no-deps -T blackbird-app python scripts/backfill_agent_tokens.py --dry-run`,
      then the same without `--dry-run`;
    - "No restart: the running simulation's roster sync picks up the tokens within ~30s."

    Replace the `:497-503` comment with exactly:
    `# Two stacks share this host: always -f docker-compose.prod.yml; the unprefixed agent-run container is org1's, never stop or remove it.`
    That file is allowlisted as a warning in the foreign-name guard, and the line contains
    no docker command, so P14's command rules do not apply to it.
  - Interface with P5. The printed backfill command imports the tokens only because P5
    teaches `backfill_agent_tokens.py` to read `SLACK_BOT_TOKEN_<AGENT_ID>` generically.
    That is the key this script writes (`provision_slack_bots.py:197-198`). Today the
    importer reads only the hard-coded `Settings.get_slack_tokens()` map
    (`backfill_agent_tokens.py:47,58`, `config.py:599`), so a newly provisioned agent is
    never imported.
  - P11b dependency. P11b drops `groups:write` from `BOT_SCOPES`, but the live Slack tier's
    `su` and `cravatt` bots still need it to create private channels (P11c). Today the
    script can only subtract scopes (`--omit-scope`, `:256`, applied at `:439`), so without
    a way to add one the live tier could never be provisioned again.
    - Add a repeatable `--add-scope AGENT_ID:SCOPE`, parsed like `--omit-scope` (`:302-305`).
    - Extract a pure `scopes_for(agent_id, omit, add, base=BOT_SCOPES) -> list[str] | None`:
      `base` minus the omitted scopes, plus the added ones, with no duplicates, or `None`
      when neither applies (today's "use the default" signal). Call it at `:439`.
      `main()` binds an `HTTPServer` (`:399-400`), calls Slack, sleeps between agents
      (`:461-462`) and blocks on OAuth callbacks (`:478-480`), so only the pure function is
      testable.
    - In the help text at `:257-260`, replace the `wiseman:groups:write` example with
      `--add-scope su:groups:write`. Only `su` creates or invites into private channels in
      the live tier (`test_slack_cohort_live.py:246,283,306,310`,
      `test_slack_client_live.py:427,455,475`, `test_slack_lifecycle_live.py:444-452`).
    - Test, in `test_provision_roster.py`: `test_scopes_for_extends_only_the_named_agent`.
      It exercises `scopes_for` directly, with no port and no Slack.

**Tests.**
- `tests/unit/test_cohort_gate_banner_copy.py`:
  - render the partial with a bare `jinja2.Environment(loader=FileSystemLoader(ROOT/"templates"))`;
  - use `gate={"preflight_error": None, "isolation_enabled": X, "default_policy": "open", "summary": {"gated":0,"total":0,"isolated":[],"unrestricted":[]}, "bot_names": {}, "snapshot": None}`
    for X in False and True;
  - `test_banner_never_names_the_other_deployments_container`: no match for
    `(?<![\w-])agent-run(?![\w-])`;
  - `test_banner_says_recreate_not_restart`: the output contains `--force-recreate` and
    `/admin/simulation`.
- `tests/unit/test_no_foreign_deployment_names.py`:
  - scan `templates/**/*.html`, `src/**/*.py`, `specs/**/*.md`, `docker-compose.yml` and
    `scripts/**/*.{py,sh}`;
  - allowlist, as warnings, `scripts/provision_slack_bots.py`,
    `scripts/migrate/preflight.py` and `scripts/migrate/run_migration.sh`;
  - fail on any line matching `(?<![\w-])agent-run(?![\w-])|copi-python[-_]`.

  It fails today on `banner:42,58`, `specs:691`, `docker-compose.yml:52` and
  `export_agent_roster.py:13-14`.
- `tests/unit/test_provision_roster.py`:
  - load the script by path, as `test_migration_checks.py:37-43` does;
  - first confirm `dotenv` and `rich` import in `.venv-test`, and do not `importorskip`;
  - `test_stale_roster_is_refused`: an mtime 2 h old raises `RuntimeError` mentioning
    `export_agent_roster.py` and `docker-compose.prod.yml`;
  - `test_fresh_roster_loads_and_filters`;
  - `test_allow_stale_bypasses_the_age_check`;
  - `test_missing_roster_message_names_the_prod_command`.

**Acceptance.**
- No operator-facing instruction names `agent-run` or `copi-python` except as an explicit
  warning.
- The banner says to recreate after `/admin/simulation` shows idle.
- The documented export writes the host `data/agent_roster.json` with a fresh mtime.
- The provisioning script refuses a roster older than 1 h.

**Deploy.** Rebuild the web image, because templates are baked in. The scripts run on the host.

### P3 — Migration checks: preflight and postflight (Python half of I1; I2, I3, I4, M5; I6 and M2 strings)

**Owns:**
- `scripts/migrate/preflight.py`
- `scripts/migrate/postflight.py`
- `tests/unit/test_migration_checks.py`

**Interface provided to P4.** `python scripts/migrate/preflight.py --print-default-target`
prints `DEFAULT_TARGET` and exits 0 without connecting to a database.

**I1 python half.**
- `revision_status` (`:622-639`): after the `current == target` check, add:
  - `if target not in REVISION_ORDER` → BLOCK, "target X is not a revision this tool knows";
  - `if current in REVISION_ORDER and REVISION_ORDER.index(target) < REVISION_ORDER.index(current)`
    → BLOCK, "target X is BEHIND the stamp Y. `alembic upgrade X` is a silent no-op (exit 0)
    and this tool never downgrades."
- Remediation text `:1301-1302`: "one of SUPPORTED_START_REVISIONS".
- `build_parser` adds `--print-default-target`. `main` handles it before `asyncio.run`.

**I3: derive postflight's expectations.**
- In preflight:
  - factor out `_pending_revisions(current, target)`;
  - add `PLANNED_DROPS = (PlannedObject("0026","table","grantbot_posted_foas"),)`. It is the
    only upgrade-time `drop_table` in 0019–0051 (`0026:30-31`);
  - add `tables_created_between(current, target)` and `tables_dropped_between(current, target)`;
  - remove the stale note at `:208-212`, and fix the `PlannedObject` docstring;
  - `compare_row_counts(..., expected_dropped=())`:
    - a new table with rows, unless `allow_growth`, is a problem: "created by this migration
      but holds N rows; … a writer was live";
    - an expected-dropped table that is missing is not a problem;
    - an expected-dropped table that still exists is a problem;
    - update the docstring.
- In postflight:
  - delete `CHAIN_CREATED_TABLES` (`:139-145`);
  - add `_TYPE_REVISION` derived from `PLANNED_OBJECTS` types;
  - add `expected_enums_for(target)`, returning the entries at or before the target (all of
    them for an unknown target);
  - `check_enums(conn, target)` gets a new title and a new `add_guarded` name;
  - `check_row_counts(conn, snapshot_path, allow_growth, target)`:
    - it requires the snapshot's `current_revision`, and FAILs if it is absent;
    - it compares using `tables_created_between` / `tables_dropped_between`;
  - `run_postflight` passes `args.target`;
  - update the module docstring and the `--target` help text.

**I2, I4 and M5.**
- Delete `POST_0019_STARTS` (`:143-145`).
- Add `agent_messages_ddl_pending(current, target)`:
  `any(o.table == "agent_messages" for o in planned_objects_between(current or "0018", target))`.
- `check_sizing(conn, rev, target)`:
  - fast path when nothing is pending, with derived text and no "0019..0023" or "0022" wording;
  - slow-path text: "whole {rev}..{target} chain"; "estimate calibrated on the 0019+0021
    block, an upper bound when only 0021 is pending";
  - call site `:1358` passes `args.target`;
  - `estimate_lock_window_ms` docstring `:597-599`: "the whole pending chain".

**I6 and M2 strings.** `P` below means `docker compose -f docker-compose.prod.yml`.
- preflight `:7` docstring becomes
  `P run --rm --no-deps -T -v "$PWD/backups:/app/backups" blackbird-app python scripts/migrate/preflight.py --snapshot /app/backups/preflight_snapshot.json`.
- `:811,826,852,834,1964` (runtime): add `-f docker-compose.prod.yml`.
- `:2009` becomes `P exec -T blackbird-app python scripts/backfill_slack_history_to_db.py`.
- `:2066-2067`: the snapshot path becomes `/app/backups/preflight_snapshot.json`.
- `:1780-1781` and `:1930-1931`: replace with these four lines, and no `&&` chain:
  - "Stop the run from /admin/simulation (Stop drains and flushes in-process), then:"
  - `docker compose -f docker-compose.prod.yml --profile agent stop -t 420 agent`
  - `docker stop -t 420 blackbird-agent-run   # ONLY if an emergency CLI run is live`
  - `docker compose -f docker-compose.prod.yml stop blackbird-app worker`

  Replace the comments at `:1772-1779` and `:1925` with:
  `# Stop order: the supervisor first (/admin/simulation Stop drains and flushes in-process), then the agent service with the 420 s grace period CLAUDE.md sizes. An emergency CLI run's container is blackbird-agent-run, never the unprefixed agent-run, which is org1's.`
  This file is allowlisted as a warning in P2's foreign-name guard.
- postflight `:6-7` docstring becomes
  `P run --rm --no-deps -T -v "$PWD/backups:/app/backups" blackbird-app python scripts/migrate/postflight.py --snapshot /app/backups/preflight_snapshot.json`.

**Tests** (`tests/unit/test_migration_checks.py`).
- New:
  - `test_revision_status_blocks_a_target_behind_the_stamp`: 0050 → 0027 BLOCKs with "BEHIND";
  - `test_revision_status_blocks_an_unknown_target`;
  - `test_print_default_target_needs_no_database`.
- Replace the tests at `:758-767` and `:770-775` with variants based on `tables_created_between`.
- **Delete** `:778-792`: its rationale is false.
- New:
  - `test_tables_created_between_is_every_planned_table_in_the_span`: `("0050","0051")` gives
    `{"assessment_chat_turns","assessment_chat_usage"}`, and `po` has no
    `CHAIN_CREATED_TABLES` attribute;
  - `test_a_correct_0050_to_0051_upgrade_compares_clean`, using the RCA E10 inputs, gives
    `(True, [])`;
  - `test_a_new_table_with_rows_is_flagged`;
  - `test_0026_drop_is_expected_from_a_pre_0026_start`;
  - `test_planned_drops_match_the_migration_files`: an `upgrade()` body regex against
    `PLANNED_DROPS`;
  - async `test_row_count_check_passes_a_correct_0050_to_0051_upgrade`: monkeypatch
    `pf.snapshot_row_counts`;
  - `test_row_count_check_fails_without_a_snapshot_revision`;
  - `test_enum_expectations_are_scoped_to_the_target`: 0041 gives only
    `pi_dm_direction_enum`;
  - async `test_check_enums_passes_below_0042`.
- Replace `:292-296` with `test_agent_messages_ddl_pending_is_derived_from_planned_objects`:
  0018, 0019 and **0020** are True; 0021 and 0050 are False; `POST_0019_STARTS` is gone.
- New:
  - async `test_sizing_for_a_late_start_is_not_row_scaled`: 0050 → 0051 at 500,000 rows gives
    PASS, and the text has no "0019..0023";
  - async `test_sizing_from_0020_is_row_scaled`.
- Keep `:1159-1162`.

**Acceptance.**
- A correct 0050 → 0051 upgrade gives a row-count PASS.
- 0025 → 0027 passes with `grantbot_posted_foas` gone.
- A `--target 0041` postflight does not fail on the 0042 enums.
- A target behind the stamp BLOCKs at check 1.

**Deploy.** The files are baked into the images, so the change takes effect at the next build.

### P4 — Guarded migration script (I1, B1, M1; M2 in `run_migration.sh`). Under D6, redesign.

**Owns:**
- `scripts/migrate/run_migration.sh`
- `tests/unit/test_run_migration_script.py` (new)
- `docs/production-migration.md`

**Consumes:**
- P3's `--print-default-target`;
- the image's `/app/.build_info.json`, written by `scripts/write_build_info.py` and
  preserved by P1.

**Change.** Every in-image call becomes a one-off container off the **built** image. Nothing
execs into the running, old container.
1. **Variables** (replaces `:57-62`):
   - `TARGET=""`, resolved in Step 1;
   - `COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"`;
   - `SVC="${MIGRATE_SERVICE:-blackbird-app}"`;
   - `DC=(docker compose -f "$COMPOSE_FILE")`;
   - `BACKUP_MOUNT=/app/backups`;
   - `DSN=""` (delete the `DSN="${DATABASE_URL:-}"` default at `:59`). Only `--database-url`
     counts as "the operator passed a DSN"; a host `DATABASE_URL` is ignored;
   - `DSN_ENV=()`, set to `(-e "DATABASE_URL=$DSN")` only when `--database-url` was given;
   - `RUN_ENV=()`, per-call environment for `run_img`;
   - after argument parsing: `mkdir -p "$BACKUP_DIR"; BACKUP_DIR_ABS="$(cd "$BACKUP_DIR" && pwd)"`.
2. **`run_img()`**: `"${DC[@]}" run --rm --no-deps -T -v "$BACKUP_DIR_ABS:$BACKUP_MOUNT" ${DSN_ENV[@]+"${DSN_ENV[@]}"} ${RUN_ENV[@]+"${RUN_ENV[@]}"} "$SVC" "$@"`.
   - The `${a[@]+"${a[@]}"}` form keeps empty arrays safe under `set -u` on any bash.
   - `-e` options must precede the service name. An environment prefix on the host
     (`VAR=… run_img`) would reach only the docker CLI.
   - It replaces `run_py` and every `exec "$SVC"`.
   - **Never** pass `--use-aliases`: the service is on `copi-edge`, and aliases there collide
     with org1.
   - Postgres calls stay `"${DC[@]}" exec -T "$PG_SVC" …`, and copies use `"${DC[@]}" cp`.
3. **Step 1, "the image is the one you just built"** (replaces `:100-130`):
   - read `commit` and `dirty_files` from `/app/.build_info.json` with `run_img`.
     - Every in-image read in this script is captured as
       `if ! out="$(run_img … 2>&1)"; then … exit "$EX_BLOCKED"; fi`.
     - Under `set -euo pipefail` (`:50`), a bare `VAR="$(failing command)"` aborts the script
       with the command's own status and no message. A pre-P3 image's argparse error is
       status 2, which is `EX_WARN` (`:52`), so the failure would read as "warnings only".
   - compare them to the host `git rev-parse HEAD` and dirty count:
     - a missing file or a commit mismatch BLOCKs with exit 1 and prints the build command;
     - a dirty-count mismatch WARNs;
   - if there is no `--target`:
     - `TARGET="$(run_img python scripts/migrate/preflight.py --print-default-target | tr -d '\r')"`;
     - a failed read, or output not matching `^[0-9]{4}$`, BLOCKs (exit 1) with "image
       predates --print-default-target; rebuild: docker compose -f docker-compose.prod.yml build blackbird-app worker";
     - `alembic/versions/${TARGET}_*.py` must exist in the image;
   - print the banner after Step 1;
   - rewrite the header (`:3-7`): the default target is the image's
     `preflight.DEFAULT_TARGET`, which CI pins to the single alembic head.
4. **Step 2, DSN.** Never bring a password-bearing DSN into the host shell.
   - If `--database-url` was not given, parse the service's own DSN inside the container:
     `run_img python -c 'import os; from sqlalchemy.engine import make_url as m; u = m(os.environ["DATABASE_URL"]); print(u.database or ""); print(u.render_as_string(hide_password=True))'`.
   - Keep the database name (`DBNAME`) and the redacted URL, which is printed as the target.
     `DSN_ENV` stays empty.
   - An empty `DBNAME` BLOCKs with exit 64.
   - Step 3's dump uses `DBNAME` instead of the `sed` over `$DSN` (`:173`).
   - Rewrite the `:132-143` comment.
5. **Step 3, backup (B1).** Preflight gets the container path:
   `--backup-path "$BACKUP_MOUNT/$(basename "$BACKUP_FILE")"`.
6. **Step 4.**
   - `SNAP_CTR=/app/backups/preflight_snapshot.json`, used by preflight and postflight.
   - Drop `MIGRATE_SNAPSHOT`.
   - The `remediate_duplicates` hint becomes `"${DC[@]}" run --rm --no-deps -T -e PYTHONPATH=/app $SVC …`.
7. **Stamps.** A `read_stamp()` helper via `run_img`, and `PRE_STAMP` read before Step 5.
8. **Step 5.** `RUN_ENV=(-e "ALEMBIC_LOCK_TIMEOUT_MS=${ALEMBIC_LOCK_TIMEOUT_MS:-10000}")`, then
   `run_img python -m alembic upgrade "$TARGET"`, then `RUN_ENV=()`. 10000 is
   `alembic/env.py:66`'s own default.
   - `:264` becomes `P exec -T $PG_SVC psql …`.
   - `:270` becomes "Stop the writers: Stop on /admin/simulation, then
     `docker compose -f docker-compose.prod.yml --profile agent stop -t 420 agent` (and
     `docker stop -t 420 blackbird-agent-run` only if an emergency CLI run is live). Re-run."
9. **Step 6.**

   | Outcome | Result |
   |---|---|
   | `STAMP == TARGET` | PASS |
   | empty or `NONE` | "silent rollback" BLOCK |
   | `STAMP == PRE_STAMP` | "alembic exited 0 but applied nothing … database unchanged" BLOCK (**not** "silent rollback") |
   | anything else | "moved PRE → STAMP, not TARGET" BLOCK |
10. **Step 7, postflight.**
    - **Live writers.** Before postflight, run `"${DC[@]}" ps --status running --services`.
      - If `blackbird-app`, `worker` or `agent` is running, pass `--allow-row-growth` and
        print `WARN  row growth tolerated: live writers: <services>`. Migrate-before-serve
        keeps the old web tier and worker serving through the migration, so growth is
        expected (`preflight.py:895-899` reads growth as "a writer was live").
      - Loss and a missing table still FAIL.
      - Otherwise pass nothing, so growth still fails, as today.
    - **Exit codes.** Postflight exits 0 when verified or warned (`warn_exit_code=0`,
      `postflight.py:687-689`) and 1 on FAIL.
    - **On exit 1, the script prints its own restore commands.** It does not point at a doc.
      The commands name the dump it took (`$BACKUP_FILE`) and use only
      `docker compose -f docker-compose.prod.yml`, `blackbird-app`, the `/admin/simulation`
      Stop and `--profile agent stop -t 420 agent`.

      Today `:311-313` send the operator to `docs/production-migration.md` "If postflight
      fails". Its step 3 (`:542`) is `docker stop -t 30 agent-run`, which stops org1's
      production simulation on this host, followed by bare `docker compose` against the
      dev stack (`:543-551`).
11. **Closing block.**
    - `backfill_slack_ts` via `P run --rm --no-deps -T $SVC …`, with "(only needed if this
      chain ran 0019)".
    - `P up -d blackbird-app worker`.
    - `:331` becomes
      `P --profile agent up -d agent   # supervisor returns IDLE; start a run from /admin/simulation only when intended (NOT agent-run, which is org1's)`.

**`docs/production-migration.md`.**
- `:18-19`: the script now runs one-off containers off the built image. Drop "Current head
  is 0028".
- `:57-58`: "does the `docker compose run --rm` for you".
- `:572`: `MIGRATE_SERVICE` defaults to `blackbird-app`, `COMPOSE_FILE` to
  `docker-compose.prod.yml`.
- `:575`: "inside a one-off container off the built image".
- **`:528-557`, "If postflight fails", rewritten prod-correct.** Use the same commands the
  script now prints: `/admin/simulation` Stop, then
  `docker compose -f docker-compose.prod.yml --profile agent stop -t 420 agent`, then
  `docker compose -f docker-compose.prod.yml stop blackbird-app worker`. `cp`, `exec` and
  `pg_restore` go against `-f docker-compose.prod.yml` `postgres`. Keep the rename-not-drop
  and `--exit-on-error` guidance verbatim.
- **The other runnable flows**, `:248-253`, `:440-441`, `:459` and `:477`, get the same
  treatment, so the doc the script calls its companion runs as written:
  - `-f docker-compose.prod.yml`;
  - `blackbird-app`;
  - `up -d blackbird-app worker`, never `--build`: the build is the separate step;
  - the agent started from `/admin/simulation`. The `--name agent-run` line at `:477` goes.
  The STOP banner stays, reworded to "commands below are prod-correct as of 2026-09-25; the
  0023 narrative is history".
- Leave `docs/blackbird-star-topology-runbook.md` alone; it is a historical record.

**Tests.** New `tests/unit/test_run_migration_script.py`.
- Harness:
  - run `bash scripts/migrate/run_migration.sh …` via `subprocess`, with `PATH` prefixed by
    `tmp_path/bin`;
  - a fake `docker` logs `$*` to `$STUB_LOG` and dispatches on substrings:
    - `.build_info.json` returns `"$STUB_COMMIT 1"`;
    - `--print-default-target` returns `$STUB_DEFAULT_TARGET`;
    - the DSN read returns a fake DSN;
    - `ls alembic/versions` exits 0;
    - `select version_num` returns the next value from a stamp-sequence file;
    - `preflight.py`, `alembic upgrade` and `postflight.py` exit `${STUB_*_EXIT:-0}`;
  - a fake `git` returns `$STUB_HEAD`, plus one status line;
  - `MIGRATE_BACKUP_DIR=$tmp_path/b` is always set.
- Tests:
  - `test_script_parses`, via `bash -n`.
  - `test_rehearsal_uses_the_prod_stack_and_one_off_containers`:
    - every docker call is `compose -f docker-compose.prod.yml`;
    - no call has `exec` together with `blackbird-app`;
    - the preflight call has `run --rm --no-deps`, `-v <tmp>/b:/app/backups`, `--target 0051`
      and `--snapshot /app/backups/preflight_snapshot.json`.
  - `test_default_target_comes_from_the_image`: `STUB_DEFAULT_TARGET=0049` gives `--target 0049`.
  - `test_explicit_target_still_wins`.
  - `test_image_built_from_another_commit_blocks`: exit 1, and the message names
    `.build_info.json` and the build command.
  - `test_pre_flag_image_blocks_instead_of_guessing`. The stub exits **2** on
    `--print-default-target`, like real argparse. The script must exit 1, not 2, with
    "rebuild" on stderr.
  - `test_unchanged_stamp_is_not_called_a_silent_rollback`: stamps 0050 then 0050 give exit
    1, the message says "applied nothing", and does not say "silent rollback" (RCA E11).
  - `test_missing_stamp_is_the_silent_rollback_signature`.
  - `test_apply_passes_the_backup_as_a_container_path`.
  - `test_apply_path_never_execs_into_the_web_service`: in a full `--apply` run, no logged
    docker call contains both `exec` and `blackbird-app`. Only `postgres` is exec'd.
  - `test_live_writers_tolerate_growth_but_not_loss`: stub `ps --status running --services`
    to return `blackbird-app`. The postflight call carries `--allow-row-growth`, and the
    output has the WARN line.
  - `test_postflight_failure_prints_prod_restore_commands`: the postflight stub exits 1. The
    output names the dump file and `docker-compose.prod.yml`. It contains no
    `(?<![\w-])agent-run(?![\w-])` and no `docs/production-migration.md`.
  - `test_lock_timeout_reaches_the_container`: with `ALEMBIC_LOCK_TIMEOUT_MS=1234`, the
    upgrade call has `-e ALEMBIC_LOCK_TIMEOUT_MS=1234` before the service name.
  - `test_host_database_url_is_ignored`: with a host `DATABASE_URL`, no docker call carries
    `DATABASE_URL=`.
  - `test_no_dsn_reads_the_service_dsn_without_putting_it_on_the_cli`.
  - `test_run_migration_hardcodes_no_revision`: no non-comment line matches `\b00[0-9]{2}\b`.

  The "fails today" tests must be shown to fail once against the pre-change script.

**Acceptance.**
- `grep -nE 'exec[^|]*(\$SVC|blackbird-app)|TARGET="[0-9]|DATABASE_URL:-' scripts/migrate/run_migration.sh`
  returns nothing. The old grep looked for the literal `compose exec`, which the
  `"${DC[@]}" exec` form never contains.
- `grep -n 'agent-run\|compose \(exec\|stop\|cp\|up\|run\) \(-T \)\?\(app\|postgres\)' docs/production-migration.md`
  shows nothing outside the STOP banner's own warnings.
- The stub tests pass, including the apply-path ones above.
- Operator-gated in production: after
  `docker compose -f docker-compose.prod.yml build blackbird-app worker`, with the old
  container still serving, a no-flag rehearsal exits 0 or 2 and check 1 PASSes the live stamp.
- B1 is verified by `test_apply_passes_the_backup_as_a_container_path`. A rehearsal cannot
  show it: it always passes `--backup-verified-elsewhere` (`run_migration.sh:168-170`), so
  check 12 never sees a dump. The first real `--apply` confirms check 12 against a real
  dump, and it stays recorded as unverified in production until then.

**Deploy.** The script runs on the host. It needs P3's flag in the image; older images fail
closed with "rebuild".

### P5 — Operator commands in the other scripts (I6)

**Owns:**
- `scripts/audit_pub_dois.py`
- `scripts/backfill_agents.py`
- `scripts/backfill_agent_tokens.py`
- `scripts/backfill_slack_history_to_db.py`
- `scripts/backfill_slack_ts.py`
- `scripts/build_cabo_sankey.py`
- `scripts/generate_sparsedata_user.py`
- `scripts/spike_private_channels.py`
- `scripts/wipe_slack.py`
- `scripts/migrate/remediate_duplicates.py`
- `tests/unit/test_backfill_agent_tokens.py` (new)

**Change.** Exact replacements, where `P` is `docker compose -f docker-compose.prod.yml`:

| Location | New command |
|---|---|
| `audit_pub_dois.py:22,25,29,30` | `P exec -T blackbird-app python scripts/audit_pub_dois.py …` |
| `backfill_agents.py:11` | `P cp scripts/backfill_agents.py blackbird-app:/app/scripts/` |
| `backfill_agents.py:12` | `P exec -T blackbird-app python …` |
| `backfill_agent_tokens.py:14,16` | `P run --rm --no-deps -T blackbird-app python scripts/backfill_agent_tokens.py [--dry-run]` |
| `backfill_slack_history_to_db.py:19` | `P exec -T blackbird-app python …` |
| `backfill_slack_ts.py:19,20` | `P run --rm --no-deps -T blackbird-app python …` |
| `build_cabo_sankey.py:9` | `P cp scripts/build_cabo_sankey.py blackbird-app:/app/scripts/` |
| `build_cabo_sankey.py:12,15` | `P exec -T blackbird-app python …` |
| `build_cabo_sankey.py:19` | `P cp blackbird-app:/app/data/schultz_viz "$HOME/schultz_viz"`. Host `data/` is root-owned, so a copy into it fails for the operator, and `blackbird-app` does not mount `./data` (only `./profiles` and `./prompts`). |
| `generate_sparsedata_user.py:22` | `P cp … blackbird-app:/app/scripts/` |
| `generate_sparsedata_user.py:23` | `P exec -T blackbird-app python …` |
| `wipe_slack.py:17,21,25,29` | `P exec -T blackbird-app python …` |
| `migrate/remediate_duplicates.py:73,78` (doc) | `P run --rm --no-deps -T -e PYTHONPATH=/app blackbird-app \` |
| `migrate/remediate_duplicates.py:185` (runtime string) | same form as `:73,78` |

**D14: delete `scripts/spike_private_channels.py`.** It is a one-off spike for the
private-channel feature removed on 2026-08-12. It calls `slack_sdk.WebClient` directly
(`:37-40`), and nothing imports or schedules it. Git history keeps it.

Notes on the choices:
- `backfill_agent_tokens.py` uses `run`, because `.env` is read when a container is created.
- `backfill_slack_ts.py` uses `run`, because it runs in the migrate-before-serve window.

**`backfill_agent_tokens.py` reads the key the provisioner writes (M2 root cause).** This
is a code change, not just text.
- `provision_slack_bots.py:197-198` writes `SLACK_BOT_TOKEN_<AGENT_ID upper>` into `.env`.
- The importer reads only `settings.get_slack_tokens()` (`:47`, `:58`). That is a hard-coded
  map of the legacy agents (`config.py:599-`), and `Settings` ignores unknown keys. So every
  newly provisioned agent is skipped as `skipped_no_env`.
- Add a pure `env_token_for(agent_id: str, legacy: Mapping[str, str], environ: Mapping[str, str]) -> str`.
  It returns `environ.get(f"SLACK_BOT_TOKEN_{agent_id.upper()}")` when that passes `_valid`,
  and otherwise `legacy.get(agent_id, "")`.
- `main` calls it with `os.environ`. A compose `run` loads every `.env` key into the
  container's environment through `env_file:`, whether or not `Settings` declares it.
- New `tests/unit/test_backfill_agent_tokens.py`:
  - `test_a_new_agents_env_key_is_imported`: `SLACK_BOT_TOKEN_NEWPI=xoxb-real` fills `newpi`.
  - `test_the_legacy_map_is_still_a_fallback`.
  - `test_a_placeholder_is_never_imported`.
  - Load the script by path, like `test_migration_checks.py:37-43`.
- The script also needs `(os.environ, not Settings)` in its docstring.

Out of scope for this package:
- `mutate_*.sh`: P12b moves them to the host `.venv-test` and removes every `docker compose`
  call from them.
- Already correct: `render_admin_simulation.py`, `dev/assessment_chat_refusal_sweep.py`,
  `ensure_star_spokes.py`, `enqueue_enrichment.py`, `eval_review_bot.py`,
  `make_install_links.py`, `backfill_dropped_verdicts.py`, `migrate_tenure_map.py`,
  `panel_calibration_ladder.py`.

**Tests.** Covered by P14's `test_operator_commands.py`.

**Acceptance.**
```
grep -rnE 'docker compose (exec|cp|run)|exec( -T)? app\b| app:/app' scripts/ \
  | grep -v 'docker-compose.prod.yml'
```
returns nothing on the merged tree. There is no `mutate_` exclusion, so this also checks
P12b.

**Deploy.** Text only.

### P6 — Corpus pipeline (C1, C1b/D8, I14, I10/D9 option B, I11, G11)

**Owns:**
- `src/services/pubmed.py`
- `src/services/corpus.py`
- `src/services/jhu_rules.py`
- `src/services/profile_pipeline.py`
- `src/models/profile.py`
- `prompts/profile-synthesis.md`
- `scripts/repair_pi_corpus.py`
- `tests/unit/test_corpus_stage_strictness.py` (new)
- `tests/unit/test_pubmed_transport.py`
- `tests/unit/test_corpus.py`
- `tests/characterization/test_profile_pipeline_gm.py`
- `tests/unit/test_pipeline_corpus_integration.py`
- `tests/unit/test_synthesis_prompt_claims.py` (new)
- `tests/integration/test_repair_pi_corpus_load_order.py` (new)

**C1 — stage failures must raise.**

`src/services/pubmed.py`:
- `fetch_pubmed_records` (`:198-223`): add a keyword-only `strict: bool = False`.
  - In the per-batch `except` (`:221-222`), `if strict: raise`; otherwise log, as today.
  - Rewrite the docstring (`:204-209`). The swallowing default is kept for ingest callers:
    `industry_evidence.py:154`, `scripts/repair_pi_corpus.py:525`,
    `scripts/generate_sparsedata_user.py:704`.
- `convert_dois_to_pmids` (`:453-549`): add `*, strict: bool = False`.
  - In the Phase-1 `except` (`:500-501`) and the Phase-2 `except` (`:546-547`), `if strict: raise`.
  - The round-trip call at `:531` becomes `fetch_pubmed_records([pmid], strict=strict)`.
  - These are still answers, not failures: an idconv `status=="error"`,
    `len(id_list) != 1`, and a DOI mismatch.
- `fetch_abstract` is untouched.
- **Amended in the audit-fix round (§9):** strict mode re-raises only a *transient* failure
  (transport error, 429, 5xx). A permanent per-item failure (another 4xx, an unparseable
  record) is no longer a stage failure:
  - a batch falls back to one-PMID fetches, and PMIDs that still fail are dropped with a
    WARNING;
  - a DOI counts as no match, with a WARNING.

  Otherwise one persistently bad DOI would dead-letter every regeneration of that PI.
- **Amended again in the re-audit round (§9):** the split is now `_is_per_item_failure`
  (`src/services/pubmed.py`), which is narrower than "not transient". Only a 4xx other than
  429, a `PubMedParseError`, or a JSON/Unicode decode error is per-item. Any other exception
  re-raises, because it is most likely a bug of ours. Three consecutive identical per-item
  failures (`_SYSTEMIC_RUN`: the same 4xx, or the same unreadable body) re-raise as
  systemic. Every dropped PMID/DOI is appended to `CorpusResult.permanently_dropped`,
  except a DOI whose paper EFetch returned through another stage. `fetch_orcid_works`
  gained `strict`, and `resolve_corpus` uses it: an empty list and the record-state
  statuses 301/404/409/410 read as no works; anything else raises. A paper-derived
  tenure start from a corpus with drops is used for that run only and not stored.

`src/services/corpus.py`:
- `:485` becomes `convert_dois_to_pmids(list(doi_pool), strict=True)`.
- `:506` becomes `fetch_pubmed_records(list(stages), strict=True)`.

`src/services/jhu_rules.py:15-18` has a stale docstring: it says a "SHORT dedicated session".
Correct it: the pipeline writes on the job session, and a failed run's year never commits,
because `process_job` rolls back (`worker/main.py:188`).

**C1b / D8.** `profile_pipeline.py`. When the ORCID profile fetch fails (step 1, the
`except` at `:103-105`, where `orcid_profile` loses `employments`), set
`step1_failed = True`.

In the tenure block (`:160-185`), when `step1_failed` is set and no stored start exists:
- still compute `derive_start_from_papers(corpus_result.kept)` for THIS run's filtering;
- do NOT call `set_tenure_start`. A stored value would stick, because `get_tenure_start`
  prefers it on every later run, even after ORCID recovers;
- log a WARNING and emit progress "JHU tenure start {y} used for this run only (ORCID
  profile unavailable; not recorded)".

The comment at `:154-159` is stale. It says "the worker COMMITS mid-pipeline state when a
job fails", but `process_job` rolls back before its failure bookkeeping
(`worker/main.py:183-188`). Correct it, and keep the rule that a year is persisted only from
a complete corpus and a successful step 1.

**I14 — flag reasons.**
- `corpus.py:587-596`: restore the split, so the reason is
  `"bare_initial_unconfirmed" if bare_initial else "s4_affiliation_mismatch"`.
- `profile_pipeline.py:138-146`: add `_FLAG_REASON_LABELS`:

  | Reason code | Label |
  |---|---|
  | `no_individual_author_match` | "no individual author match" |
  | `bare_initial_unconfirmed` | "initial-only name match not confirmed" |
  | `s4_affiliation_mismatch` | "name+affiliation search hit with a different author affiliation" |

  Build the parenthetical from a `Counter` of the flag reasons, as "N <label>" joined by
  "; ". An unknown reason shows its raw code.

**I10, option B (D9) — strike the last-author claim.**
- `prompts/profile-synthesis.md:76-78`: replace item 3 with "**Weighting.** The context lists
  publications newest first and does not state the PI's author position; do not infer the
  PI's role on a paper from author order."
- `profile_pipeline.py:624`: the comment becomes `# Publications (most recent 30)`.
- `src/models/profile.py:62-63`: change "research-type AND carried an abstract" to
  "in-tenure AND carried an abstract".
- G11, `profile.py:65`: change "Read it as a lower bound on grounding, not as a count of what
  the model saw:" to "Read it as what was offered to the synthesis step — an upper bound on
  what the model saw, not a count of it:".
- `src/models/profile.py:122-129`, the `evidence_state` docstring. "`convert_dois_to_pmids`
  failing (it swallows its own errors) … logged by `convert_dois_to_pmids` during steps 3+4"
  stops being true once the corpus path passes `strict=True`. The corpus path now raises
  `CorpusStageError` and the job fails. The swallowing default survives only for ingest
  callers. Say so, and keep the "still understated" case, re-labelled as applying to rows
  written before the strict corpus path shipped (2026-09-25). Word it the way the same
  docstring already words its other older-rows case (`profile.py:112-114`).
- The spec edits for I10 are in P13.

**I11 — deterministic duplicate order.**
- `scripts/repair_pi_corpus.py` `load_stored_publications` (`:505-508`): order by
  `.order_by(Publication.created_at, Publication.id)`.
- Add a note to the `partition_duplicate_pmids` docstring: rows written in the same
  transaction share `created_at`, so ties fall back to id.
- I6 part: the `:44` docstring becomes
  `docker compose -f docker-compose.prod.yml run --rm --no-deps -T blackbird-app`.

**Tests.**

New `tests/unit/test_corpus_stage_strictness.py`:
- Setup:
  - copy `_no_waiting` and `_client_factory` from `test_pubmed_transport.py:36-50`;
  - fake `corpus.fetch_orcid_works`, `fetch_works_by_orcid` and `search_pmids`;
  - keep `convert_dois_to_pmids` and `fetch_pubmed_records` real, over an httpx
    `MockTransport`.
- `test_one_failed_efetch_batch_raises_corpus_stage_error`: 150 PMIDs, the first EFetch
  returns `<not-xml`; expect a raise matching `efetch`.
- `test_an_idconv_failure_raises_corpus_stage_error`: a `ConnectError`; expect a raise
  matching `doi_resolution`.
- `test_a_failed_doi_esearch_raises_corpus_stage_error`: a 503, three times.
- `test_a_failed_doi_roundtrip_efetch_raises`: a `RemoteProtocolError`, three times.
- Positive control, `test_a_doi_answered_as_absent_is_not_a_failure`.
- Guard, `test_every_corpus_stage_call_is_strict`: parse the AST of `resolve_corpus`; every
  `_stage` call wrapping `convert_dois_to_pmids` or `fetch_pubmed_records` must pass
  `strict=True`.

`tests/unit/test_pubmed_transport.py`:
- New `test_strict_batch_path_raises_on_one_bad_response`, expecting `PubMedParseError`.
- Edit the docstring at `:194-200`.

`tests/unit/test_corpus.py`:
- The `_wire` fakes at `:114,118` get `*, strict=False` and `assert strict is True`.
  Otherwise the fake raises `TypeError`, `_stage` wraps it, and the test passes for the wrong
  reason.
- Add a reason assertion, `["s4_affiliation_mismatch"]`, to
  `test_an_s4_only_candidate_needs_an_affiliation_match_too` (`:154-164`).
- New `test_a_bare_initial_unconfirmed_miss_keeps_its_reason`.
- Guard, `test_every_flag_reason_has_a_progress_label`: the reason literals in the AST must
  equal the keys of `_FLAG_REASON_LABELS`.

`tests/characterization/test_profile_pipeline_gm.py`:
- The fakes at `:111,114,300,303` get `*, strict=False` and `assert strict`.
- `pubmed_is_down` at `:634,755` gets the new signature.
- At `:644` and `:759`, capture `ei` and assert that `ei.value.__cause__` is a
  `ConnectionError`.
- Never run a blind `--snapshot-update`.

`tests/unit/test_pipeline_corpus_integration.py`:
- New `test_flag_progress_names_the_actual_reasons`.
- New, for C1b: `test_a_failed_orcid_profile_fetch_persists_no_derived_tenure_start`.

New `tests/unit/test_synthesis_prompt_claims.py`:
- `test_prompt_only_requests_author_weighting_the_context_supplies`: if `"last-author"` appears
  in `prompt.lower()`, then "[last author]" must appear in the context.
  - Lower-case the check: the pre-fix prompt says "Last-author" (`profile-synthesis.md:76`),
    which a case-sensitive check never sees.
  - Show once that the test fails against the pre-fix prompt.

New `tests/integration/test_repair_pi_corpus_load_order.py`:
- `test_the_later_added_duplicate_is_the_one_removed`: the older row has `created_at`
  2026-01-01 and id `ffff…`; the newer has 2026-02-01 and id `0000…`. The removal must be the
  newer row.

**Interface with P12b.** These `mutate_system.sh` anchors must still occur exactly once:
- `pubmed.py`:
  - `    """Make a rate-limited, identified GET request to NCBI, with retry.` (M12b's new target);
  - `    if assigned.lower() == auth.lower():` (M2);
  - `    params.setdefault("tool", _NCBI_TOOL)` (M3).
- `profile_pipeline.py`:
  - `    Validate synthesized profile fields.` (M12d);
  - `def _validate_profile(profile: dict[str, Any]) -> bool:` (M6/M6b).

If an edit must move one, say so in the report; §5 re-points it.

**Acceptance.**
- A single failed or unparseable EFetch batch, or a failed idconv, ESearch or round-trip
  inside `resolve_corpus`, raises `CorpusStageError`. The job then goes pending or dead, and
  no tenure row is committed.
- `fetch_abstract` and `industry_evidence` are unchanged.
- The progress text names the real flag reasons.
- The live prompt makes no request the context cannot satisfy.
- `grep -n "strict=True" src/services/corpus.py` returns exactly 2 hits.

**Deploy.**
- Rebuild worker and app; rebuild agent for parity.
- The prompt is bind-mounted and read on each call, so it takes effect when it lands (§4
  hazard). It needs no `role.toml` bump and no `sync_prompt_set_docs` run, because it is not
  part of a prompt set.
- Risk to watch: deterministic per-batch failures that used to thin the corpus now dead-letter
  the job after 3 attempts. Check the worker logs for "Failed to fetch PubMed batch" and
  "Failed batch DOI→PMID".

### P7 — Assessment detail page (I8, I17)

**Owns:**
- `src/services/assessment_detail.py`
- `templates/admin/_assessment_detail_body.html`
- `tests/integration/test_assessment_detail_page.py`

**I8.**
- `_load_tool_turns` (`:1634-1691`): add a `thread_id` parameter. When it is set, add
  `or_(LlmCallLog.thread_ts == thread_id, LlmCallLog.thread_ts.is_(None))`. The `IS NULL`
  branch keeps rows logged before 0042 on the time-window heuristic.
- The call site at `:1270` passes `thread_id`, taken from `:1243`.
- Update the docstring and the comment at `:1292-1302`.
- Template copy:
  - `:1318-1319` becomes "Tool activity from this thread (or, for calls logged before thread
    attribution, this thread's time window) whose reply text could not be matched…";
  - `:1329-1330` becomes "…most recent hub calls for this thread…".

**I17.** Template `:1178-1181`. After "Interview messages unavailable." (a pinned prefix),
replace the sentence with: "The thread this verdict came from is not in
<span class="font-mono">agent_messages</span> — its Slack timestamp was never recorded, its
messages were never flushed (e.g. a SIGKILL mid-turn), or it predates 2026-08-22, when a
<code>--fresh</code> run still deleted messages. Everything above is unaffected."

**Tests** (`test_assessment_detail_page.py`).
- Rewrite the header comment at `:1207-1217`.
- Keep the tests at `:523` and `:1220`, which use a NULL `thread_ts`.
- New `test_a_turn_stamped_with_another_thread_is_not_shown`: a foreign-`thread_ts` row is not
  unplaced, and `logs_scanned == 2`.
- New `test_other_threads_cannot_crowd_this_thread_out_of_the_scan_cap`: with
  `LOG_SCAN_LIMIT=2`, add two newer foreign rows; the page's own turn must still be placed.
- In the unavailable test (near `:655`), add `assert "wipes messages" not in resp.text`.

**Acceptance.**
- The "Unplaced turns (N)" count and the cap banner count only rows that belong to this thread
  or have no thread.
- No rendered text claims that `--fresh` deletes anything.

**Deploy.**
- Rebuild the app image.
- Rebuild the agent image too, for parity. `src/services/assessment_detail.py` is on the
  engine's import graph (CLAUDE.md, the 2026-09-21 box), although `_load_tool_turns` is not
  reached by the engine.

### P8 — Discussions counts (I9)

**Owns:**
- `src/services/directory.py`
- `tests/integration/test_discussions_filters.py` (new)

**Change.** In `build_discussions_view`:
- Delete the first filter pass (`:921-925`). Counts become run-wide, which matches the card
  links (`templates/admin/discussions.html:57`, and the manager template at `:56`). The second
  pass (`:992-1006`) still filters the list.
- The orphan loop (`:947-948`) becomes
  `for thread_id, td in decision_map.items(): if thread_id not in known_thread_ids:`. This
  takes each thread's LAST decision.

**Tests.** New `tests/integration/test_discussions_filters.py`, with this A/B/C/D fixture:

| Thread | Channel | Messages | Decisions |
|---|---|---|---|
| A | chan-a | root + reply | `no_proposal` @T0 ("A-FIRST"), then `timeout` @T0+60 ("A-LAST") |
| B | chan-b | root + reply | none (active) |
| C | chan-b | root, no replies | none |
| D (orphan) | chan-b | no root | `proposal` @T0, then `no_proposal` @T0+60 |

- `test_a_status_filter_never_lists_a_thread_under_its_first_decision`: with
  `status_filter="no_proposal"`, the list is only `["D"]`, and the counts are
  `{timeout:1, active:1, no_replies:1, no_proposal:1}`.
- `test_a_channel_filter_does_not_change_the_status_counts`.
- `test_a_real_orphan_takes_its_last_decision`.
- `test_export_under_a_status_filter_excludes_the_mislisted_thread`: via
  `GET /admin/discussions?…&status_filter=no_proposal&export=true`, "A-FIRST" is absent from
  the export.

**Acceptance.** The cards and the footer total (relabelled "Total threads" in the audit-fix round, §9) are identical under any filter. The list and
the export show each thread once, under its last decision.

**Deploy.** Rebuild the app image.

### P9 — Proposal routes (I13 per D10, M2′, I29)

**Owns:**
- `src/routers/agent_page.py`
- `tests/integration/test_proposal_review.py`
- `tests/integration/test_concurrent_web_writes.py`

**Change.**

`review_proposal`:
- After the membership check, add
  `if td.outcome != "proposal": raise HTTPException(404, "Proposal not found")`.
- The SELECT guard's `raise HTTPException(400, "Already reviewed")` (`:482-483`) becomes the
  same 302 dashboard redirect that the IntegrityError branch returns (D10). Rewrite the comment
  at `:513-514`.

`reopen_proposal`:
- Add the same outcome check after `:590-591`.
- M2′: wrap `record_engagement`, `mark_notification_responded` and `db.commit()`
  (`:649-654`) in `try: … except IntegrityError: await db.rollback(); return RedirectResponse(f"/agent/{agent_id}/dashboard", 302)`.
  - The helpers must be inside the `try`, because their autoflush can surface the error
    early. This mirrors `review_proposal`.
  - The rollback also discards the inbox row, so no duplicate guidance survives.

**Tests.**
- `test_proposal_review.py`:
  - `test_a_decided_review_cannot_be_re_decided` (`:553-577`) now asserts a 302 to the
    dashboard and a single review row.
  - New `test_review_of_a_non_proposal_decision_is_404` and
    `test_reopen_of_a_non_proposal_decision_is_404`: an outcome of `'timeout'` returns 404 and
    writes no row.
- `test_concurrent_web_writes.py`:
  - `test_concurrent_proposal_reviews_do_not_500` asserts
    `sorted([r1.status_code, r2.status_code]) == [302, 302]` and that both
    `headers["location"] == f"/agent/{agent_id}/dashboard"`. This catches RCA mutation R2
    (P12b's M20). "No exception", or "status < 500" on one response, would not.
  - Delete the docstring clause "this test does not assert the response" (`:72`).
  - New `test_concurrent_reopens_do_not_500`. Use the file's `_ExistenceCheckGate` pattern.
    Both calls return 302, and there is exactly one `ProposalReview` and exactly one PI inbox
    row.

- Stale text in this package's own files:
  - `test_proposal_review.py:54`: "`/review` with 400 "Already reviewed"" becomes "`/review`
    with a redirect to the dashboard, like `/reopen`";
  - the `_ExistenceCheckGate` docstring (`test_concurrent_web_writes.py:20-33`):
    - "correctly finds the row and raises HTTPException(400, "Already reviewed")" becomes
      "…finds the row and redirects (D10)";
    - its "~487-494" location for the guard becomes a content reference (the pre-insert
      SELECT in `review_proposal`), since the guard is at `:476-483` at `bc68025`;
    - keep the reason the gate exists;
  - `test_concurrent_web_writes.py:77-79` ("… 400 at its own existence check");
  - `test_proposal_review.py:560` ("a 400 from a globally broken endpoint") and `:595` ("so
    the 400 above").
  - Afterwards, `grep -n 400` in both files shows only rating-validation lines.

**Interface with P11b.** P11b owns `tests/integration/test_agent_page.py`, whose
`test_a_pi_review_is_recorded_and_cannot_be_submitted_twice` asserts `r2.status_code == 400`
(`:619`). P11b changes it to match D10 (see P11b). The other `== 400` asserts in that file
(`:378`, `:529`, `:643`) are different validations and are unaffected.

**Interface with P12b.** Two `mutate_system.sh` anchors in this file must still occur exactly
once after the change: `    if already_reviewed is not None:` (M10) and
`            "Ignoring duplicate reopen of proposal %s by %s "` (M12g). If an edit must move
either, say so in the report; §5 re-points it. P12b's M20 anchor (the lost-race redirect in
`review_proposal`) is chosen in §5 against this package's final text.

**Acceptance.**
- A sequential duplicate and a lost race behave the same.
- A non-proposal decision cannot be reviewed or reopened.
- R2 now fails the race test.

**Deploy.** Rebuild the app image.

### P10 — Admin router text and the 0038 docstring (I18 admin half, I33, I37, I38)

**Owns:**
- `src/routers/admin.py`
- `tests/integration/test_manager_slack_provisioning.py`
- `tests/integration/test_manager_views.py`
- `alembic/versions/0038_specialist_consult_read_state_and_stamp.py`

**I18.** `admin.py:1563`: "(v2 §9.4 / §13.1)" becomes "(v2 §9 req. 4 / §13.1)".

**I37.** `admin.py:163-167`: replace the counts with: "ruff's B008 flags a `Depends(...)` call
sitting in an argument default, and every such default counts against the src/ lint ratchet
(scripts/ci.sh's SRC_LINT_MAX). New handlers here take these module-level singletons instead
of adding to that debt."

**I38.** Reword to the actual policy:

| Location | New text |
|---|---|
| `test_manager_slack_provisioning.py:88-90` | "Operator decision 2026-09-11: the PI-management controls on /manager/pis (Add-PI, Edit Profile, mute, Slack provision/activate/callback) are neither hidden nor refused while an admin impersonates a manager. An admin wearing a manager reaches both provisioning POSTs, attributed to the impersonated manager; a reviewer still cannot. Still refused while impersonating: reviewer assign/unassign and prompt-suggestion generate/status (`_refuse_impersonation`, reviews.py), every assessment-chat route (`_refused`, assessment_chat.py), self-service account deletion (profile.py) and the admin user delete (admin.py). Review feedback and review-status writes are allowed, attributed to the impersonated user (CLAUDE.md, Account Types)." |
| `test_manager_slack_provisioning.py:303-305` | "Operator decision 2026-09-11: the Slack provisioning callback admits an impersonated session. …" |
| `admin.py:1198-1199` | "(operator decision 2026-09-11: the /manager/pis PI-management controls, this callback included, are not refused while impersonating; reviewer assign/unassign, prompt-suggestion generate/status, assessment chat and both account deletes still are)" |
| `test_manager_views.py:759-761` | "…the PI-management controls on /manager/pis are not hidden while impersonating … (Assign/unassign and the chat drawer on the assessment pages are still hidden under impersonation.)" |

Refusal sites confirmed at HEAD:
- `reviews.py` `_refuse_impersonation`: `:439`, `:460`, `:508`, `:551`;
- `assessment_chat.py` `_refused`: `:121`, `:146`, `:181`;
- `profile.py:218`;
- `admin.py:268`.

**I33.** In `0038…py:35-39`, replace the "NOT affected" paragraph with: "Also affected since
46d9a99 (2026-08-28): the discussions panel cards at src/services/thread_panel.py select an
explicit column list that now names ``SpecialistConsult.read_state``, so /admin/discussions
and /manager/discussions also raise against a pre-0038 database. (When this docstring was
first written the list named none of the four columns.)" Do not touch the `revision` and
`down_revision` lines.

**Acceptance.**
- `grep -rn "§9.4" src/routers` returns nothing.
- `alembic heads` returns a single head, unchanged.
- No hard-coded B008 counts remain.
- Docstring assertions are unchanged.

**Deploy.** No behavior change. Rebuild the app image for parity.

### P11 — Dead code and Slack scopes (I15 per D11–D13, I27, I12, I16, I18 in simulation.py, and the I19 code half)

Checked that no live path breaks:
- The only dynamic dispatch uses Slack SDK method-name strings (`slack_client.py:393`,
  `slack_web.py:99`).
- `templates/`, `scripts/` and the CLI do not reference any removed symbol.
- The engine's Transport use is `connect`, `is_connected`, `bot_user_id`, `ais_bot_user`,
  `apost_message`/`post_message`, `create_channel`, `join_channel`/`ajoin_channel`,
  `list_channels`, `get_channel_id`, `cache_channel_ids`, `apoll_channel_messages`,
  `aget_full_channel_history`/`get_full_channel_history` and `aget_all_thread_replies`. None
  of these is removed.
- **Keep** `AgentSlackClient.join_channel`/`ajoin_channel`, `NullTransport.join_channel` and
  the fakes' join methods. Only `slack_web.join_channel` is dead.

#### P11a — Slack client and Transport

**Owns:**
- `src/agent/transport.py`
- `src/agent/slack_client.py`
- `tests/fakes.py`
- `tests/unit/test_transport.py`
- `tests/unit/test_fakes.py`
- `tests/unit/test_slack_client_contract.py`
- `tests/unit/test_thread_not_found.py`
- `tests/unit/test_slack_private_channel_creation.py`
- `tests/unit/test_slack_off_loop.py`
- `specs/local-db-conversations.md`

**Remove, keeping each Transport, NullTransport and fake member in step with its definition.**

`create_private_channel` (`slack_client.py:1034-1080`) and `invite_to_channel` (`:1082-1116`):
- delete `_MAX_PRIVATE_CHANNEL_ATTEMPTS` (`:190-193`);
- delete `import secrets` (`:23`), which otherwise becomes an F401 over the ceiling;
- reword the module docstring at `:11`;
- delete Transport `:60-61`, NullTransport `:141-145`, and the fakes at `:463-470` plus
  `self.invites` (`:367`);
- tests:
  - `test_fakes.py:84-89`;
  - `test_transport.py:39-42` (keep the `create_channel` and `join_channel` asserts);
  - `test_slack_client_contract.py:971-978`;
  - in `test_slack_private_channel_creation.py`, delete the private-create, name-taken and
    invite tests and `_FakeSlack`; keep `test_public_create_channel_still_works` and
    `TestImports`; rewrite the docstring.

`send_dm` (`:964-970`), `poll_dm_messages` (`:972-984`) and `open_dm_channel` (`:935-962`):
- delete `self._dm_channels` (`:353`) and the section header (`:931-933`);
- `:355-364` "Guards both caches" becomes "Guards the channel cache";
- delete Transport `:57-58` and `:91`, NullTransport `:132-136` and `:175-176`, and the fake
  `send_dm` at `:403-404`;
- tests:
  - `test_transport.py:33-34` and `:51`;
  - `test_slack_off_loop.py::test_concurrent_dm_channel_opens_for_the_same_user_fetch_only_once`
    (`:131-151`), plus `_SlowListingClient.conversations_open` / `open_calls` and the
    `._dm_channels` mention at `:59`.

`get_thread_replies` (`:637-674`):
- delete Transport `:88`, NullTransport `:166-167` and the fake at `:414-415`;
- "all four inbound" becomes "all three" at `transport.py:86` and
  `test_slack_client_contract.py:714`;
- tests:
  - the `test_slack_client_contract.py:709` parametrize row;
  - `test_thread_not_found.py::TestGetThreadRepliesRaisesThreadNotFound` (`:37-49`) and
    docstring point 1, which should now name `get_all_thread_replies` (it also raises
    `ThreadNotFound`, `:731-732`);
  - `test_transport.py:48`.

`resolve_user_name` (`:740-749`):
- delete Transport `:41` and NullTransport `:122-123`;
- keep `users_info`, which `is_bot_user` uses.

`aconnect` (`:1290-1291`): delete it and the fake at `:455-456`.

Docstrings: at `transport.py:13`, drop "and private-channel flows". At
`specs/local-db-conversations.md:124-129`, list the surviving members.

**D13, conditional.** If §4 step 0 finds that nothing needs `groups:read`, remove the unused
`include_private` parameter from `Transport.list_channels` (`transport.py:70`),
`NullTransport.list_channels` (`:151`), `AgentSlackClient.list_channels`
(`slack_client.py:1139-1168`, keeping `types="public_channel"`) and the fakes. No caller
passes it: `git grep include_private -- src scripts` shows only definitions, plus
`slack_web`'s dead `list_channel_ids`, which P11b deletes.

**Interface with P12a and P12b.**
- Keep `FakeAnthropic`, `text_response` and its `stop_reason` parameter in `tests/fakes.py`;
  P12a's C2 test uses them.
- Two `mutate_slack_mirror.sh` anchors must still occur exactly once in `slack_client.py`:
  - `            self._client = None` (S3, in `connect`, `:540`);
  - `        last_exc: SlackApiError | None = None` (S4, `:409`).
  Neither is inside a deleted member.

**Guard tests.**
- `test_transport.py::test_every_transport_member_has_an_engine_caller`: parse the AST of the
  `Transport` body. Every member `n` needs an `ast.Attribute` with `attr in {n, "a"+n}`
  somewhere in `src/**/*.py`, not counting `transport.py` and `slack_client.py`. Today it
  fails on 7 members.
- `test_transport.py::test_null_transport_defines_only_protocol_members`.

#### P11b — `slack_web`, `slack_tokens`, scopes, and the I27 fixture

**Owns:**
- `src/services/slack_web.py`
- `src/services/slack_tokens.py`
- `src/services/slack_provisioning.py`
- `tests/unit/test_slack_web.py`
- `tests/unit/test_slack_tokens.py`
- `tests/unit/test_slack_provisioning.py`
- `tests/integration/test_agent_page.py`

**`slack_web`.**
- Remove `list_channel_ids`, `join_channel`, `post_message`, `list_channel_ids_async`,
  `get_user_info_async`, `join_channel_async` and `post_message_async` (`:128-180`,
  `:205-264`, `:300-310`, `:318-333`).
- Remove their `__all__` entries and the `SlackListingIncomplete` re-export (`:62-76`).
- Remove the import block at `:32-38`, which becomes unused.
- Keep `get_user_info`; `agent_page.py:1041` uses it.
- Tests (`test_slack_web.py`):
  - delete the four `test_post_message_*` and both `test_list_channel_ids_*`;
  - `test_every_sync_entry_point_has_an_async_wrapper` (`:184-189`) iterates
    `("lookup_user_by_email","revoke_token")`;
  - fix the docstring at `:4-5`.

**`slack_tokens`.**
- Remove `slack_globally_enabled` (`:68-81`) and `get_agent_bot_token` (`:54-65`).
- The `is_valid_token` docstring (`:23-31`) now names the live consumers:
  `token_for_agent_row`, `get_any_bot_token`, and the auto-detect in `main.py:226-231`.
- Tests (`test_slack_tokens.py`): remove `test_get_agent_bot_token_reads_the_db_then_env`,
  `ENABLED_CASES`, `test_slack_globally_enabled_tri_state`,
  `test_enabled_cases_cover_all_three_branches` and their imports. In the docstring at
  `:1-12`, note that `main.py`'s inline tri-state is still untested.

**Scopes (D13).**
- **D13 is settled by §4 step 0's Slack docs check, before dispatch.** The main session
  writes the outcome into this package and P11a:
  - **`groups:read` needed:** keep it, and cite the method in the `conversations.list
    (private)` row's comment;
  - **not needed:** drop it from `BOT_SCOPES` and delete that row. P11a then removes the
    now-unused `include_private` parameter.
  - Either way, the `chat.getPermalink` and `auth.revoke` rows get the scopes the docs name,
    as does posting to a user ID (`im:write`, if needed).
- `slack_provisioning.py` `BOT_SCOPES` (`:23-41`): remove `groups:write`, `im:history` and
  `im:write`. That leaves 9 entries, or 8 if the docs check drops `groups:read`. Fix the
  comments at `:22` and `:31-35`, and the `create_app` docstring at `:95-100`.
- `test_slack_provisioning.py` `METHOD_SCOPES` (`:38-55`):
  - remove the rows `conversations.create (private)`, `conversations.invite (private)`,
    `conversations.open` and `conversations.history (dm)`;
  - add rows for `chat.getPermalink` (`slack_client.py:783`) and `auth.revoke`
    (`slack_web.py:276`). Their required scopes are UNDETERMINED and must be checked against
    Slack's method docs; use `None` only if the docs confirm it;
  - justify the `conversations.history (private)` row (`groups:history`) with
    `_sync_private_channels_from_db` and the legacy `collab_private` polling
    (`simulation.py:2948`, `:3030`);
  - the existing `conversations.list (private)` → `groups:read` row (`:43`) gets a comment
    citing `list_channels(include_private=True)` as its caller, marked UNDETERMINED per D13.
    Do not add a second `groups:read` row: a key outside the method set would break this
    package's own equality guard;
  - rewrite the docstring at `:61-65`.
- Guard tests:
  - `test_method_scope_table_matches_the_methods_src_calls`: collect the first string
    argument of `self._api`/`self._paginate` in `slack_client.py` and of `_call(<x>, "…")` in
    `slack_web.py` from the AST, map `a_b` to `a.b`, and compare with the `METHOD_SCOPES`
    keys (qualifiers stripped). It fails today, because `chat.getPermalink` and
    `auth.revoke` are missing.
  - `test_manifest_requests_no_scope_nothing_needs`: `set(BOT_SCOPES) == needed`.

**I27.** In `tests/integration/test_agent_page.py`:
- delete the `slack_enabled` monkeypatch at `:146`;
- rename the fixture to `_no_env_bot_tokens`;
- rewrite its docstring: the `get_slack_tokens` stub keeps the `.env` fallback in
  `get_any_bot_token` and `env_token` from handing out a real live-tier token;
- keep the `:150` stub.

**D10 follow-through (P9 interface).**
`test_a_pi_review_is_recorded_and_cannot_be_submitted_twice` (`:604-634`) asserts
`r2.status_code == 400` at `:619`.
- It now asserts a 302 whose `location` is `/agent/{OWNER_AGENT}/dashboard`, with the single
  review row assertion kept.
- The control comment at `:622-623` becomes "…so the redirect is the already-reviewed guard
  rather than a route that broke after one write".

New test `test_env_bot_tokens_never_reach_the_delegate_lookup`:
- set `SLACK_BOT_TOKEN_SU` with `monkeypatch.setenv`, then `get_settings.cache_clear()`, and
  register `request.addfinalizer(get_settings.cache_clear)`. Otherwise the cached `Settings`
  keeps the fake token for the rest of the session: `tests/conftest.py` never clears it;
- **no** agent row in the world has a DB token, not only the owner.
  `get_any_bot_token` walks every row (`slack_tokens.py:89-98`) before the `.env` fallback;
- the owner agent has `delegate_slack_ids=["U1"]`;
- `GET` the dashboard: no `users_info` call is made, and the raw id is shown;
- use the route and world helpers from the existing dashboard tests.

Mutation check: comment out `:150`, and the new test must fail.

#### P11c — Live Slack tier

**Owns:**
- `tests/slack_live_support.py` (new)
- `tests/integration/test_slack_client_live.py`
- `tests/integration/test_slack_mirror_live.py`
- `tests/integration/test_slack_lifecycle_live.py`
- `tests/integration/test_slack_cohort_live.py`
- `tests/integration/test_slack_provision_live.py`

**`tests/slack_live_support.py`** provides two helpers:
- `create_private_channel(client, name)`, returning
  `client._api("conversations_create", name=name, is_private=True)["channel"]`;
- `invite(client, cid, uids)`, which calls `conversations_invite` once per uid.

**`test_slack_client_live.py`.**
- Delete the `resolve_user_name` tests and `test_an_unknown_user_id_does_not_raise`
  (`:47-56`), the DM section (`:380-416`), and
  `test_private_channel_creation_needs_groups_write` (`:442-464`).
- The `private_channel` fixture uses the helper; drop its `startswith` assert.
- The invite test uses `invite`.
- Fix the docstring at `:485-488`.
- At `:211-214`, assert via `get_all_thread_replies`.

**The other live files.**
- `get_thread_replies` becomes `get_all_thread_replies` at `test_slack_mirror_live.py:163,
  198, 216`, `test_slack_lifecycle_live.py:146` and `test_slack_cohort_live.py:228`.
- `test_slack_cohort_live.py:246, 283, 306, 310` use the helper.
- `test_slack_cohort_live.py:217` and `:222`: replace the `_owes_reply` asserts, keeping the
  positive control.
  - `:217` becomes
    `assert any(t.thread_id == root.message_ts for a, t in eng._pending_reply_pairs() if a is su), "precondition: the thread owes a reply in-cohort"`.
  - `:222` becomes the same assertion with the message "a grandfathered thread still owes
    its reply (D12)". The inversion is the retired rule.
  - This mirrors P11d's engine-test rewrite.
- Delete `test_slack_lifecycle_live.py::test_invite_tolerates_self_and_repeat_but_reports_a_real_failure`
  (`:434-456`).
- `test_slack_provision_live.py:57-80`: assert `set(BOT_SCOPES) <= granted`, and
  `"groups:write" in su`. The fixture needs that scope; say so in the docstring.

These files run only with `SLACK_TEST_*` credentials.

#### P11d — Engine (`simulation.py`, `message_log.py`, `state.py`, `post_types.py`)

**Owns:**
- `src/agent/simulation.py`
- `src/agent/message_log.py`
- `src/agent/state.py`
- `src/agent/post_types.py`
- `tests/unit/test_message_log.py`
- `tests/unit/test_message_log_differential.py`
- `tests/unit/test_cohort_isolation.py`
- `tests/unit/test_simulation_logic.py`
- `tests/unit/test_self_parented_entry.py`
- `tests/unit/test_post_types.py`
- `tests/unit/test_post_type_enforcement.py`
- `tests/integration/test_message_persistence.py`
- `tests/integration/test_specialist_consult_capture.py`
- `tests/integration/test_cohort_engine_live.py`
- `tests/integration/test_hub_assessment_capture_gate.py`

**`simulation.py`, by symbol.**
- (a) Comment above `REBUILD_WINDOW_S` (`:347-350`): drop the `_hydrate_thread_from_db`
  sentence.
- (b) Delete `_owes_reply` (`:1534-1571`). D12 retires the rule.
- (c) `_pending_reply_pairs` docstring: "See v2 §8."
- (d) `_rebuild_state_from_db` comment (`:7133-7135`): drop the hydrate clause.
- (e) Delete `_hydrate_thread_from_db` (`:7236-7286`).
- (f) `_recompute_allowed_sender_ids` comment (`:8730-8732`): "…or `cohort_topology_snapshot`
  keeps reporting threads the gate no longer affects."
- (g) `_apply_cohort_gate_to_state` docstring (`:8840-8843`): drop the `_owes_reply` sentence.
- (h) Log string (`:8880-8882`): "…grandfathered — partner is outside the cohort; it may still
  conclude".
- I12: drop the `resolve_post_type_name` import (`:26`); `:6325-6327` becomes
  `by_name.get(post_type)`, and the comment goes.
- I16: in the warning at `:5313-5316`, "(the column only arrived in migration 0035)" becomes
  "(every engine writer sets it, so this row was written out of band)". Keep the substrings
  "raw_verdict" and "NULL".
- I18: `:8902` "(v2 §9.4/§13)" becomes "(v2 §9 req. 4 / §13)".

**`message_log.py`.**
- Delete `get_last_bot_sender_in_channel` (`:467-488`), `self._last_bot_in_channel`
  (`:213`), and its per-append update in `_record` (`:326-329`).
- Update the docstrings at `:152`, `:200` and `:283-286`.
- Hydrate references: `:61-62` and `:291`.
- `_owes_reply` references: `:138` and `:624`.

**`state.py:32-34`:** "reported in `cohort_topology_snapshot`; it no longer affects scheduling".

**`post_types.py` (I12):** delete `LEGACY_POST_TYPE_ALIASES` and its comment (`:74-86`), and
`resolve_post_type_name` (`:89-91`).

**Tests.**
- `test_message_log.py`: delete `TestLastBotSenderInChannel` (`:249-277`) and the
  `test_last_bot_sender_*` tests (`:348-361`).
- `test_message_log_differential.py`: remove the reference method (`:72-83`) and its loop
  (`:195-197`).
- `test_cohort_isolation.py`:
  - drop `get_last_bot_sender_in_channel` from the tuple at `:706`;
  - delete the grandfathered, permitted and closed reactive-priority tests (`:902-918`,
    `:961-969`);
  - fix the docstring at `:13`.
- `test_simulation_logic.py`:
  - `:1260-1272` asserts on `engine._pending_reply_pairs()` instead: a human entry gives
    `[]`, a bot entry gives `[(agent, thread)]`;
  - delete the `_owes_reply` lines at `:1777` and `:1781`;
  - update `:1220`.
- `test_self_parented_entry.py:12`: drop the name.
- `test_post_types.py`: remove the imports (`:13`, `:19`), `test_the_retired_idea_name_still_resolves`
  (`:66-81`) and the alias loop (`:86`).
- `test_post_type_enforcement.py`: keep the rejection tests, and reword the docstrings at
  `:238` and `~:470`.
- `test_message_persistence.py`: delete `test_hydrate_thread_loads_windowed_out_thread`
  (`:345-368`) and the `resolve_user_name` stub (`:458-459`).
- `test_specialist_consult_capture.py:1102-1106`: remove the hydrate leg.
- `test_cohort_engine_live.py`:
  - delete `:489` and `:776`;
  - rewrite the `:1263-1274` precondition to use `_pending_reply_pairs()`, and rename the test
    to `test_grandfathered_thread_still_concludes`.
- `test_hub_assessment_capture_gate.py` `test_a_superseded_row_with_a_null_verdict_says_so`:
  add `assert not any("migration 0035" in m for m in messages)`. A bare `"0035"` could match a
  uuid or a timestamp.

**Interface with P12b.**
- These harness anchors must still occur exactly once:
  - `simulation.py`:
    - `        visibility = self._resolve_channel_visibility(channel)` (cohorts M6, `:6855`);
    - `            if target_id == agent.agent_id or target_id in allowed:` (M9, `:5955`);
    - `        return root.slack_ts` (mirror S2, `:6938`);
    - `                visibility=visibility,\n                slack_ts=slack_ts,` (mirror S1's
      new anchor, `:6879-6880`);
  - `message_log.py`:
    - `    if not entry.is_bot:\n        return True` (cohorts M4's new anchor, `:102-103`);
    - `    if entry.visibility == VISIBILITY_COLLAB_PRIVATE:` (M5).
- Cohorts M7 targets `_owes_reply`, which this package deletes. P12b retires M7.

**Constraint.** Several tests use `inspect.getsource` pins on `start`, `_run_main_loop`,
`_pending_reply_pairs`, `_select_agent`, `_run_post_turn` and `_phase5_new_post` (RCA brief).
None of these edits may reorder or add the pinned tokens.

**Deploy (P11a and P11d).** Both change engine code (`src/agent/`), so the agent image must be
rebuilt. The only runtime-visible changes are two log texts: the I16 warning, and (h), the
grandfather log string at `simulation.py:8880-8882`.
- I18 (`:8902`) is a docstring.
- I12 changes no accepted name. `idea` maps to `idea_crosslab`, which has no CANONICAL
  entry (`post_types.py:44-55`, `:86`), so `idea` is already rejected today.

§7 rebuilds all three images.

#### P11e — Profile versioning (I15's `get_revision_history`, the I19 code half)

**Owns:**
- `src/services/profile_versioning.py`
- `src/models/profile_revision.py`
- `tests/unit/test_profile_versioning.py`

**Change.**
- Delete `get_revision_history` (`:111-138`). It has never had a caller or a test.
- I19 docstrings and comments:
  - `profile_versioning.py:3-4, 63, 66`. `:123` lies inside the deleted function.
  - `profile_revision.py:26, 35`.

  Mark `private` and `slack_dm` as historical: pre-2026-08-13 rows may still carry them.
  Mark `monthly_refresh` as never written as a mechanism: the `monthly_refresh` job writes
  `pipeline` (`worker/main.py:138-140`). Do not touch the job enum.

**Tests.**
- Rename `test_create_revision_slack_dm` (`:53-66`) to
  `test_a_historical_private_slack_dm_row_still_loads`.
- `:72`: `profile_type` becomes `"public"`.
- Guard `test_live_writers_use_only_live_values`: every `create_revision(...)` call in the
  `src/` AST uses a literal `mechanism` in {web, agent, pipeline} and a `profile_type` in
  {public, memory}. `cli.py:324` is a loop over a literal tuple; check its elements. Also
  assert that `create_revision.__doc__` names each live value.

### P12 — Test-quality fixes and the mutation harness

P12 is split in two because together it would own 16 files.

#### P12a — Tests that pin a proxy (I20, I21, I22, I23, I24, I25 + I25b, I28, C2)

I29 is handled in P9, which owns `test_concurrent_web_writes.py`.

**Owns:**
- `tests/unit/test_reply_lane.py`
- `tests/unit/test_roles.py`
- `tests/integration/test_profile_pipeline_live.py`
- `tests/unit/test_specialist_floor.py`
- `tests/integration/test_cli.py`
- `tests/integration/test_cohort_admin.py`
- `tests/e2e/test_browser_flows.py`
- `tests/e2e/README.md`
- `tests/unit/test_delegates.py`
- `tests/unit/test_consult_accounting.py`

**I20: the reply-lane lock test never contends.** This is
`test_reply_lane.py::test_thread_lock_then_agent_lock_does_not_deadlock_against_an_agent_lock_only_caller`
(`:843-904`).

Why it cannot fail: the `_update_agent_memory` monkeypatch it relies on is never reached.
Since `d1162d1`, `_close_thread` only queues memory events onto `_pending_memory_events`
(`simulation.py:2861-2882`), and it does that without an await. The only await left inside
the agent-lock span is the `ThreadDecision` write (`simulation.py:2825-2839`), which runs
only when both `session_factory` and `simulation_run_id` are set.

Changes:
- Delete the `_update_agent_memory` monkeypatch (`:876-879`). Keep the `try_reserve` patch.
- Add a gated fake session. Import `uuid` if the file does not already.
  ```python
  entered, release, added = asyncio.Event(), asyncio.Event(), []
  class _GatedSession:
      async def __aenter__(self): return self
      async def __aexit__(self, *exc): return False
      def add(self, obj): added.append(obj)
      async def commit(self):
          entered.set()
          await release.wait()
  eng.session_factory = lambda: _GatedSession()
  eng.simulation_run_id = uuid.uuid4()
  ```
- Replace both `asyncio.sleep(0.001)` handshakes with this sequence:
  ```python
  close_task = asyncio.ensure_future(_close_under_thread_lock())
  await asyncio.wait_for(entered.wait(), 1.0)   # close now holds both agent locks
  phase5_task = asyncio.ensure_future(eng._phase5_new_post(wang))
  for _ in range(5):
      await asyncio.sleep(0)
  assert eng._agent_locks.get("wang").locked()
  assert not phase5_task.done()
  assert wang.state.last_phase5_action_time == 0.0   # blocked at acquire_all
  release.set()
  await asyncio.wait_for(asyncio.gather(close_task, phase5_task), timeout=2.0)
  assert thread.status == "closed"
  assert len(added) == 1
  assert wang.state.last_phase5_action_time > 0.0    # got the lock after close released it
  ```
  `last_phase5_action_time` defaults to 0.0 (`state.py:141`). It is stamped as the first
  statement inside the lock (`simulation.py:3204-3211`).
- In the docstring, name the real contention point (the thread-decision commit inside the
  agent-lock span) and record that `d1162d1` moved memory updates out of that span.
- If the DB write ever leaves the lock span, the `.locked()` assert fails loudly. The test
  cannot silently go vacuous again.

**I21: `test_roles.py` pins the opposite of the inline-verdict contract.** Replace
`test_visible_body_hides_the_verdict_the_sidecar_still_carries` (`:316-373`) with
`test_conclude_prompt_asks_for_the_inline_verdict_and_keeps_scores_in_the_sidecar`.

Render the live CONCLUDE prompt:
```python
from src.agent import thread_guidance
from src.agent.agent import Agent
from src.agent.state import ThreadState

a = Agent("blackbird", "BlackbirdBot", "Blackbird Labs", role="scout_hub")
t = ThreadState(thread_id="t1", channel="general", other_agent_id="wang", message_count=11)
_, m = a.build_phase4_prompt(thread=t,
                             thread_history=[{"sender": "WangBot", "content": "pitch"}],
                             other_agent_name="WangBot", other_agent_lab="Wang")
norm = " ".join(m[0]["content"].split())
```
`message_count=11` is ordinal 12, which is CONCLUDE (`agent.py:427-430`,
`thread_guidance.py:211-216`).

Assert, in order:
1. `thread_guidance.CONCLUDE in norm`. This is the `{thread_phase}` substitution, and it
   proves the right ordinal rendered.
2. Let `visible = norm[:norm.index("**Emit the sidecar as bare JSON")]`. Each of `"not met"`,
   `"red flags"`, `"advance"`, `"route-to-incubation"`, `"confidence label"` and `"decline"`
   appears in `visible.lower()`.
3. `"none of it may appear" not in norm.lower()`. This is the E7 contradiction.
4. Let `admits = [s for s in norm.split(". ") if "also appears in `<slack_message>`" in s]`.
   Then `len(admits) == 1`. `admits[0].lower()` contains gating, recommendation, red flags
   and confidence, and contains none of `weighted`, `band`, `dimension`, `score` or
   `raw_verdict`.
5. `"not the dimension scores" in norm` (`phase4-thread-reply.md:175-176`).
6. Both normalized `thread_guidance._SCOUT_HUB[thread_guidance.CONCLUDE]` strings are
   substrings of `norm`, and neither contains `weighted`, `band` or `dimension score`.
7. Keep the old sidecar-half checks against
   `norm[norm.index("**Emit the sidecar as bare JSON"):]`: `Gating criteria`, `Red flags`,
   `route-to-incubation` and `not_met`, plus `advance`/`conditional`/`pass` in `.lower()`.

The docstring states the real contract: the verdict appears inline; scores, band and
`raw_verdict` stay sidecar-only. It cites `thread_guidance._SCOUT_HUB[CONCLUDE]` and
`test_claude_md_disclosure_sync.py`, and drops the "F5 / never carry the gating statuses"
premise.

No prompt file is edited, so this needs no `role.toml` bump and no
`sync_prompt_set_docs.py` run. The old node id survives only in dated audit and spec
records, which stay as they are.

**I22: live test T4.4 now fails outright.** Replace
`test_t44_pubmed_unreachable_still_yields_a_profile_but_a_measurably_thinner_one`
(`test_profile_pipeline_live.py:857-1045`) with
`test_t44_pubmed_unreachable_fails_the_run_and_stores_nothing`. Keep the same fixtures and the
same `respx` NCBI-outage routes (`:880-894`).

The current contract (audit M5, and CLAUDE.md "A corpus-stage failure FAILS the job"):
`resolve_corpus._stage` raises `CorpusStageError` (`corpus.py:428-434`), and `search_pmids`
raises on transport failure (`pubmed.py:433-445`).

```python
with respx.mock(assert_all_called=False) as router:
    ...  # the same NCBI routes; pass_through for the rest
    with pytest.raises(CorpusStageError) as exc_info:
        await profile_pipeline.run_profile_pipeline(user.id, db_session)
    ncbi_attempts = sum(r.call_count for r in blocked)
assert ncbi_attempts > 0                     # keep the existing message
cause = exc_info.value.__cause__
assert isinstance(cause, httpx.ConnectError) and "(T4.4)" in str(cause)
assert await profile_rows(db_session, user.id) == []
assert await publications(db_session, user.id) == []
assert probe.public_calls == 0 and probe.contexts == []
```

- Add `from src.services.corpus import CorpusStageError`.
- The `__cause__` check proves the failure is our simulated outage, not a live ORCID or
  OpenAlex failure (rule L3).
- The row checks mirror GM `test_profile_pipeline_pubmed_outage_raises_instead_of_fabricating`
  (`test_profile_pipeline_gm.py:618-660`).
- Drop `require_observation("single_run", "T4.1")`, which removes the ordering dependency.
- Drop the write-only `_OBSERVED["degraded"]` (`:1037`).
- Drop the `user.name` assertion. Whether step 1's name write commits before the corpus
  stage raises is UNDETERMINED.
- Module text:
  - `:36`: T4.4 now contributes 0 LLM calls.
    - Recompute the module's per-test budget from the file's own current assertions (for
      example `single["llm_calls"] == 1` and `llm_calls_total`).
    - T4.1's private seed is already gone (`probe.private_calls == 0`), so do not carry
      the old arithmetic forward.
  - `:84`: Carberry's empty corpus is covered by GM
    `test_profile_pipeline_researcher_with_no_works_is_not_reported_as_evidence_lost`.
  - `:445`: "the one T4.4 produces" becomes "an ungrounded one".
  - `:194`: `(T4.3, T4.4)` becomes `(T4.3)`.
- Interface with P6. P6's strict flags make the pipeline stricter, not looser, and a total
  outage still raises at stage s3, so the new T4.4 holds with or without P6.

**I23: the floor-test route row passes only because the floor fails open.** In
`test_specialist_floor.py:90-93`:
- `("pass", set(), set())` becomes `("pass", {"budget"}, set())`;
- `("route-to-incubation", set(), set())` becomes
  `("route-to-incubation", {"budget"}, {"scientific", "talent"})`.

Recording `budget` arms the floor, which is the same technique the `advance` rows use.
Replace the comment at `:90-91` with: both rows are armed; `pass` is exempt
(`PANEL_EXEMPT_RECOMMENDATIONS`, `specialists.py:921`); route owes the always-required pair.

**I24a: `test_cli.py` checks its own patch.** In
`test_cli_writes_to_the_test_database_not_the_configured_one` (`:881-893`):
- Delete `:887` `assert config.get_settings().database_url == pg_url`. It reads back the
  autouse fixture's own patch (`:110-127`).
- Delete the now-unused `from src import config` (`:885`).
- Make the docstring claim only what `:893` proves: a row that the CLI's own `_get_db()`
  committed can be read through the test engine. The ambient-DSN guard is the fixture's
  `/copi` refusal (`:126`).

**I24b: the cohort view test cannot see an empty page.** In
`test_cohort_admin.py::test_pi_facing_thread_view_is_never_cohort_filtered` (`:587-608`):
- Add the `monkeypatch` fixture and
  `monkeypatch.setattr(get_settings(), "cohort_isolation_enabled", True)`, as at `:555`.
- Request `/admin/discussions?run_id={run.id}`. Without the parameter the page picks the
  newest run (`directory.py:810-823`).
- Keep the 200 check and add:
  - `assert "Showing 2 threads" in r.text`, the text from
    `templates/admin/_discussions_threads.html:282`;
  - `assert "SuBot" in r.text and "CravattBot" in r.text`. The poster column renders
    `{{ t.agent_id | capitalize }}Bot` at `:112`.
- P8's I9 change affects filtered counts only; the unfiltered count of 2 is unchanged.

**I25: the e2e onboarding replay expects "Step 3 of 4".**
- `test_browser_flows.py:510`: `"Step 3 of 4"` becomes `"Step 3 of 3"`. That matches
  `templates/onboarding/profile_review.html:10` and `FLOWS["onboarding"]["stops_at"]`
  (`:184`).
- `README.md:211` gets the same fix.
- `README.md:207-209`: "start → review → private profile → complete, with
  `users.onboarding_complete` flipping only on the final POST" becomes "`GET /onboarding`
  (spinner, then the review form) → `POST /onboarding/save-profile`, which flips
  `users.onboarding_complete`". `onboarding.py` registers only `GET ""`,
  `POST /save-profile` and `POST /retry`.

**I25b (found while planning; the same root cause as I7a's second half).**
`README.md:59-66`, setup step 4, runs `docker compose exec -T … app python -m pytest`. The
dev `app` service builds from the same `Dockerfile` (`docker-compose.yml:19`), and that
installs `.` without `[dev]` (`Dockerfile:14`), so the container has no pytest.

Rewrite step 4 to run on the host:
`E2E_BASE_URL=http://localhost:8002 E2E_ISOLATION_BASE_URL=http://localhost:8003 E2E_ADMIN_USER_ID=… E2E_SIGNUP_USER_ID=… E2E_ONBOARDING_USER_ID=… .venv-test/bin/python -m pytest tests/e2e/test_browser_flows.py -q`.
The ports are published by steps 1-2 (`-p 8002:8000`, `-p 8003:8000`). Replace the
comment "Container-to-container hostnames, because pytest runs inside `app`" with the reason
the step runs on the host.

Before rewriting, confirm that the e2e modules read only `E2E_*` variables and HTTP, and no
container-only hostname (`grep -n "environ\|getenv" tests/e2e/*.py`). If a module needs a
database URL, pass the host-published one. Steps 0-3 (alembic, `uvicorn`, `seed`) need no
dev extras and stay unchanged. Keep the `copi_slack_test` warnings verbatim.

**I28: `test_default_status` cannot see the default.** Replace the body of
`test_delegates.py::TestDelegateInvitation::test_default_status` (`:30-42`) with:
```python
col = DelegateInvitation.__table__.c.status
assert col.default is not None and col.default.arg == "pending"
```
The model sets `default="pending"` at `delegate.py:31-33`. The docstring should say "the ORM
insert default is 'pending'", and note that the migration's `server_default` is not
exercised.

**C2: nothing tests that a consult's own truncation retry is booked.** Add
`test_a_consults_own_truncation_retry_is_booked` to `test_consult_accounting.py`. It uses the
same patch point as `test_the_hub_reads_the_opinion_before_it_reads_the_label`
(`:517-521`):
```python
from tests.fakes import text_response
fake = FakeAnthropic([
    text_response('{"verdict_signal": "adequate", "concerns": [', stop_reason="max_tokens"),
    '{"verdict_signal": "adequate", "confidence": "high"}',
])
monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)
billed, consulted = [], []
await _execute_consult_specialist(
    "chemistry", "q", "c", agent_id="blackbird",
    on_consult=lambda d, s: consulted.append(d),
    on_api_call=lambda: billed.append(1),
)
assert len(fake.calls) == 2      # control: the retry really ran
assert len(billed) == 2          # the consult plus its own truncation retry
assert consulted == ["chemistry"]
```
- The booking before the call is at `tools.py:659-660`.
- `on_retry=on_api_call` is at `tools.py:727`; `llm.py:1068-1096` fires it after the
  `max_tokens` retry.
- The retry stays under the fake's 21,333 ceiling (`max_tokens=4000`, `tools.py:708`).
- Interface with P11a. P11a edits `tests/fakes.py` and must keep `FakeAnthropic`,
  `text_response` and its `stop_reason` parameter.

**Acceptance.** Each fixed test fails under its killing mutation and passes without it; see
the §5 table and P12b's vacuity tier. In addition:
- `grep -rn "Step 3 of 4" tests/ templates/` returns nothing.
- `grep -n 'get_settings().database_url == pg_url' tests/integration/test_cli.py` returns
  nothing.
- `test_reply_lane.py` no longer references `_update_agent_memory`.

**Deploy.** None. These are tests and a test README.

#### P12b — Mutation harness and the two drift guards (I7a, I7b, RCA §8 causes 3 and 4)

**Owns:**
- `scripts/mutate_system.sh`
- `scripts/mutate_cohorts.sh`
- `scripts/mutate_slack_mirror.sh`
- `tests/unit/test_mutation_harness_targets.py` (new)
- `tests/unit/test_cohort_spec_citations.py` (new)
- `specs/cohort-system.md`

**I7a: re-point M12b.** Its target string no longer exists (E12). Point it at the current first
docstring line of `_ncbi_get` (`pubmed.py:164`, one occurrence at `bc68025`):
`'pubmed_both~~src/services/pubmed.py~~    """Make a rate-limited, identified GET request to NCBI, with retry.~~    """Make a rate-limited, identified GET request to NCBI E-utilities, with retry. [INERT EDIT]~~M12b INERT docstring — MUST SURVIVE'`

**I7b (found while planning).** Two more harness anchors are already unrunnable at `bc68025`,
and one is about to be. Each script's applier requires exactly one occurrence
(`mutate_slack_mirror.sh:124-127`, `mutate_system.sh:317-318`) and reports ERROR otherwise.
- **`mutate_cohorts.sh` M4.** `    if not entry.is_bot:` occurs twice in `message_log.py` (`:102`
  and, as a substring, `:656`). The intended site is `_entry_allowed`'s human bypass at `:102`.
  Re-point the entry to `    if not entry.is_bot:\n        return True` →
  `    if entry.sender_agent_id is None:\n        return True`, which occurs once.
- **`mutate_slack_mirror.sh` S1.** `            slack_ts=slack_ts,` occurs four times in
  `simulation.py` (`:3647`, `:4748`, `:5399`, `:6880`). The intended site is the outbound
  `LogEntry` at `:6880`. Re-point it to
  `                visibility=visibility,\n                slack_ts=slack_ts,` →
  `                visibility=visibility,\n                slack_ts=None,`, which occurs once.
- **`mutate_cohorts.sh` M7.** It targets `simulation.py:1564`, which is inside `_owes_reply`
  (`:1534-1571`). P11d deletes that function (D12). Retire M7 with a comment in the style of
  the existing M8 retirement note: "M7 was pinned to `_owes_reply`'s grandfathered skip; D12
  retired the rule and the function is gone."

**I7a second half: run the harnesses on the host with `.venv-test`.** Today they
`docker compose exec -T app` (`mutate_system.sh:139,142`, `mutate_cohorts.sh:69,72`,
`mutate_slack_mirror.sh:46,74-163`). That is the dev service, and its image has no pytest.
Apply the same edits to all three scripts.

- **Interpreter.** Replace `SVC`/`DC` with the following, and delete `MUTSYS_SERVICE`,
  `MUTCOH_SERVICE` and `MUTMIRROR_SERVICE`:
  ```bash
  PY="${MUT_PYTHON:-$PWD/.venv-test/bin/python}"
  [ -x "$PY" ] || { echo "ERROR: $PY missing — create .venv-test ON THE HOST (CLAUDE.md); the images have no pytest" >&2; exit 1; }
  ```
- **Copy directory.** Use `COPY="${MUTSYS_COPY_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/mutsys.XXXXXX")}" || exit 1`,
  and the same for `mutcoh` and `mutmirror`.
  - For an override directory: refuse one that exists and is non-empty; otherwise
    `mkdir -p -m 0700` it before resolving, so a nonexistent override does not make `cd`
    fail into an empty `COPY`.
  - Then `COPY=$(cd "$COPY" && pwd -P)`, and `ROOT=$(pwd -P)`.
  - **Guard before the tar copy and again before every `rm -rf`.** Abort unless `COPY` is
    non-empty, absolute, not `/`, not `$ROOT`, and not inside `$ROOT`
    (`case "$COPY/" in "$ROOT"/*) die;; esac`). Compare physical paths with physical
    paths.
  - Why: if `mktemp` fails, `COPY` is empty, and bash's `cd ""` succeeds as a no-op. The
    `pwd -P` would then return the repo root, the mutants would land in the live tree, and
    the EXIT trap's `rm -rf -- "$COPY"` would delete it. The old literal default
    (`/tmp/mutsys`) had no such failure mode.
- **Copy.** Run
  `tar -C "$PWD" --exclude=./.git --exclude=./.venv-test --exclude=./backups --exclude=./logs --exclude=./mutants --exclude=./build --exclude=./.hypothesis --exclude=./.pytest_cache --exclude=./.ruff_cache --exclude=./.playwright-mcp --exclude=__pycache__ -cf - . | tar -C "$COPY" -xf -`.
  - `backups` is newly excluded; it holds 583 MB of dumps (RCA S1).
  - `.env` stays in, for parity with `ci.sh`, because `Settings` reads a cwd-relative `.env`
    (`config.py:104`). The copy lives in a 0700 `mktemp` directory that is removed on EXIT.
- **Provenance.** Run `(cd "$COPY" && "$PY" -c 'import src; print(src.__file__)')`; it must
  equal `"$COPY/src/__init__.py"`. The RCA's E2 and R1-R3 runs used this venv against a
  separate clone, and the clone's code executed. If an editable install ever made `import src`
  resolve to the repo, this check refuses to run.
- **Applier.** `FROM="$from" TO="$to" "$PY" - "$COPY/$file" <<'PY' … PY`, with the same body as
  today.
- **Import guard** (cohorts and slack mirror only): `(cd "$COPY" && "$PY" -c "import $mod")`.
- **Restore.** `cp -- "$file" "$COPY/$file"`, checked with `cmp -s`.
- **Cleanup.** `rm -rf -- "$COPY"`.
- **Run.** `(cd "$COPY" && env "${envargs[@]}" timeout -k 30 "${MUT_TIMEOUT:-900}" sh -c "exec \"$PY\" -m pytest $select -q -x -rf -p no:cacheprovider") >"$log" 2>&1`.
  - The repo has no pytest-timeout, and a mutant can hang a selection, so every run is
    bounded. The RCA wrapped E2 in `timeout 300` (`repro.sh:9`).
  - `exec` makes the kill reach pytest itself, not just `sh`.
  - **Exit 124 or 137 is `TIMEOUT` (ERROR, `fail=1`) for every mutant, inert or real, never
    a kill.** A kill must name a failing test. This is the harnesses' own rule:
    `mutate_system.sh:45` ("never killed"), and `mutate_slack_mirror.sh:31-33` (an
    unreachable workspace once looked like a kill).
  - No fixed test needs a timeout to be killed. I20 fails fast through its own
    `asyncio.wait_for`, and the RCA's E2 control failed in 5.37 s (`repro.log:240-241`).
  - `envargs` entries become `VAR=value`.
  - `sh -c` keeps `pubmed_both`'s `-k '…'` quoting.
  - Cohorts keeps `-m 'not real_llm'`; the slack mirror keeps `-m live_slack`.
- **Database.** `TEST_DATABASE_URL` becomes optional in all three. Under `set -u`
  (`mutate_system.sh:134`), read it as `${TEST_DATABASE_URL:-}` (`:156`), and append it to
  `envargs` (`:292`) only when it is non-empty. `conftest.py:34` treats an empty value as
  unset.
  - remove the unresolvable `postgres:5432/copi_a3` default (`mutate_system.sh:138`) and the
    `:?` requirements (`mutate_cohorts.sh:66`, `mutate_slack_mirror.sh:44`);
  - when it is unset, `tests/conftest.py:30-44` starts a testcontainers Postgres;
  - keep the `*/copi` refusal whenever it is set.
- **Tier filter (D18).** `mutate_system.sh` gains `MUT_TIERS`, a space-separated allow-list
  of tier names; the default is every tier. Each mutant whose tier is not listed reports
  `skipped (MUT_TIERS)`. That lets §5 run the live ORCID/NCBI tiers without `pipeline`, the
  one that spends Anthropic calls.
- **`mutate_slack_mirror.sh`.** Delete `ENVARGS`, since the host environment is inherited.
  Keep the `SLACK_TEST_WORKSPACE` requirement and the baseline run.
- **Headers.**
  - Replace "container's /tmp", "installed into site-packages in this image" and the
    `*_SERVICE` rows with host and venv wording, and add a `MUT_PYTHON` row.
  - Add "run on the host, not over sshfs (CLAUDE.md)".
  - Keep the dated MEASURED records, and append a re-measurement line after §5 runs them.

**Vacuity tiers (RCA §8 cause 3).** The killing mutation of every fixed test stays recorded
and re-runnable. Each real vacuity mutant gets **its own tier, selecting only its fixed
test's node**. With whole files and `-x`, whichever test fails first counts as the kill: under
M15 the older `test_route_to_incubation_owes_a_panel` already fails (`repro.log:264-295`),
so M15 would report "killed" whether or not the I23 row were fixed.

In `mutate_system.sh`, all with `TIER_CREDS[…]=""`:

| Tier | Selects exactly |
|---|---|
| `vac_i20` | `tests/unit/test_reply_lane.py::test_thread_lock_then_agent_lock_does_not_deadlock_against_an_agent_lock_only_caller` |
| `vac_i23_route` | the `route-to-incubation` parametrization of `tests/unit/test_specialist_floor.py`'s floor-arithmetic test (its exact node ID) |
| `vac_i23_pass` | the `pass` parametrization of the same test |
| `vac_c2` | `tests/unit/test_consult_accounting.py::test_a_consults_own_truncation_retry_is_booked` |
| `vac_i21` | `tests/unit/test_roles.py::test_conclude_prompt_asks_for_the_inline_verdict_and_keeps_scores_in_the_sidecar` |
| `vac_i28` | `tests/unit/test_delegates.py::TestDelegateInvitation::test_default_status` |
| `vac_i29` | `tests/integration/test_concurrent_web_writes.py::test_concurrent_proposal_reviews_do_not_500` |
| `vac_i24b` | `tests/integration/test_cohort_admin.py::test_pi_facing_thread_view_is_never_cohort_filtered` |
| `vacuity` | the union of all eight nodes; it holds only the inert control M13 |

- The harness-target guard's `INERT_COVERS` maps every `vac_*` tier to `vacuity`. Its premise
  check asserts that `vacuity`'s selection contains each `vac_*` node.
- R2 and R1 live here rather than in `agentpage` or `mutate_cohorts.sh`, whose selections
  are whole files.

Mutants, with the inert control first:

| Label | File | From → to |
|---|---|---|
| M13 INERT (`vacuity`) | `src/agent/locks.py` | `"""Per-key asyncio locks with deadlock-free multi-acquire.` → the same text plus ` [INERT EDIT]` |
| M14 I20/E2 (`vac_i20`) | `src/agent/locks.py` | `                await lock.acquire()` → `                if lock.locked(): await asyncio.Event().wait()\n                await lock.acquire()` |
| M15 I23/E3 (`vac_i23_route`) | `src/agent/specialists.py` | `PANEL_EXEMPT_RECOMMENDATIONS: frozenset[str] = frozenset({"pass"})` → `… frozenset({"pass", "route-to-incubation"})` |
| M16 I23 pass row (`vac_i23_pass`) | `src/agent/specialists.py` | the same line → `… = frozenset()` |
| M17 C2/R3 (`vac_c2`) | `src/agent/tools.py` | `            on_retry=on_api_call,` → `            on_retry=None,` |
| M18 I21/E7 (`vac_i21`) | `prompts/roles/scout_hub/phase4-thread-reply.md` | ``Only your inline verdict also appears in `<slack_message>` `` → ``None of it may appear anywhere in `<slack_message>` `` |
| M19 I28/E14 (`vac_i28`) | `src/models/delegate.py` | `String(20), nullable=False, default="pending"` → `default="accepted"` |
| M20 I29/R2 (`vac_i29`) | `src/routers/agent_page.py` | the lost-race `IntegrityError` redirect in `review_proposal` → `from fastapi.responses import Response` + `return Response(status_code=500)`, as in `repro2.sh:22-36` |

- Every anchor above except M20 occurs exactly once at `bc68025`.
- M20's anchor must be chosen against the merged `agent_page.py` (§5 step 2). P9 adds a second
  `except IntegrityError` → rollback → redirect block in `reopen_proposal`, so the RCA's
  four-line anchor will no longer be unique. Widen it with a line only `review_proposal`'s
  branch carries.
- M21 I24b/R1 (`vac_i24b`): `src/services/directory.py`
  `    root_posts = roots_result.scalars().all()` → `    root_posts = []` (one occurrence).
- **Sensitivity check (§5).** Revert the I23 route row to `("route-to-incubation", set(), set())`
  and run `vac_i23_route` under M15. It must report SURVIVED.

**Guard: `tests/unit/test_mutation_harness_targets.py`.** For each of the three scripts:
- Read the lines between `MUTANTS=(` and the closing `)`, skipping blank and `#` lines.
- `shlex.split` each entry. There must be exactly one token; posix mode turns `\"` into `"`.
- Split on `~~`. The layout is (tier, file, from, to, label) for `mutate_system` and
  (file, from, to, label) for the other two.
- Normalize with `frm.replace("\\n", "\n")`, as the appliers do.
- Assert `(ROOT / file).read_text().count(frm) == 1`.
- For `mutate_system`, also assert:
  - every tier used is a key of `TIER_SELECT` (`^\s*\[(\w+)\]=` inside `declare -A TIER_SELECT=(`);
  - every tier that has a real mutant is covered by an `INERT` mutant, either in its own
    tier or in a tier named by an explicit `INERT_COVERS` map in the test.
    - The map is `{"pubmed_doi": "pubmed_both", "pubmed_tool": "pubmed_both"}` plus every
      `vac_*` tier → `vacuity`, because M12b,
      the pubmed inert control, lives in `pubmed_both` (`mutate_system.sh:209-211`).
    - The test also asserts the premise: `TIER_SELECT[pubmed_both]`'s `-k` names both tests
      that `pubmed_doi` and `pubmed_tool` select.
- Assert that no double-quoted entry contains `$` or a backtick. shlex does not expand
  either, but bash would: a backtick in double quotes is command substitution.
  - M18's anchor contains backticks (`` `<slack_message>` ``), so P12b writes M18 in single
    quotes, as M12b already is. Any other entry with a backtick is written the same way.
- Control: at least one entry parsed per script.

At `bc68025` this fails on M12b (count 0), M4 (count 2) and S1 (count 4). The main session
emulated the parse in the same way and found exactly those three. After P12b and the §5
re-pointing it passes. It lives under `tests/unit`, so `ci.sh` runs it.

**Guard: `tests/unit/test_cohort_spec_citations.py` (RCA §8 cause 4).**

Parse `specs/cohort-system-v2.md`:
- A line starting with three backticks toggles a fence.
- Outside fences, a heading matching `^#{1,6}\s+(\d+(?:\.\d+)*)\.?\s` opens section N. Any
  other heading closes the current section. Collect each section's body lines.
- The spec has, among others, `## 8.` (`:506`), `## 9.` (`:557`, **Requirements.** at `:569`),
  `## 13.` (`:736`) and `### 13.1` (`:749`).

Scan:
- **Roots:** `src/`, `tests/`, `templates/`, `scripts/`, `alembic/`, `specs/`, and the top-level
  `docs/*.md`.
- **Excluded:** `docs/audits/`, `docs/plans/` and `docs/specs/`, which are dated records that
  quote defects verbatim (this plan and the RCA both quote "v2 §9.4"). Also this test file.
- **Suffixes:** `.py .md .html .sh .toml .txt .yml .yaml`.

Grammar:
```python
REF = r"§\d+(?:\.\d+)*(?:\s+req\.\s*\d+)?"
CITE = re.compile(r"(?:cohort-system-v2(?:\.md)?`*\s*|\bv2\s+)(" + REF + r"(?:\s*[/,]\s*" + REF + r")*)")
ONE = re.compile(r"§(\d+(?:\.\d+)*)(?:\s+req\.\s*(\d+))?")
```
- Section N must be parsed.
- For `§N req. M`, section N's body must contain `**Requirements.**` and a line matching
  `^M\.\s`.

Tests:
- `test_the_spec_parser_sees_numbered_sections`: control; `{"5.1", "6.3.1", "9", "14.6"}` is
  a subset of the parsed sections.
- `test_the_scanner_finds_the_known_citations`: anti-vacuity; more than 30 citations, and
  `src/services/cohorts.py` citing §5.3 is among them.
- `test_citation_grammar`, on synthetic strings:
  - `v2 §9 req. 4` resolves;
  - `v2 §9 req. 5` and `v2 §9.4` do not;
  - `specs/cohort-system-v2.md §4.2 / §14.4` yields two resolving references.
- `test_every_cohort_spec_citation_resolves` collects `path:line §ref` failures and asserts
  that the list is empty.

Current state:
- At `bc68025` it fails on `admin.py:1563` (`v2 §9.4 / §13.1`) and `simulation.py:8902`
  (`v2 §9.4/§13`), which is I18.
- It passes once P10 and P11d write `v2 §9 req. 4 / §13.1` and `v2 §9 req. 4 / §13`.
- P11d's new "See v2 §8." also resolves.
- Every package merges before §5, so the ordering dependency on I18 is satisfied.

**`specs/cohort-system.md:14-19`.** Replace the counted claim ("31 comments across 16 files …
all 15 distinct cited sections … resolve"). The counts drift with every edit, and nothing
checks them: P11d alone deletes citing code. Keep the `.notes/` → `specs/` reading
instruction, and replace the counts with: "Every `v2 §N` / `cohort-system-v2 §N` citation in
the tracked tree resolves to a numbered heading; `tests/unit/test_cohort_spec_citations.py`
enforces it. Cite a numbered requirement as `§N req. M`."

**Acceptance.**
- `grep -n "docker compose" scripts/mutate_*.sh` returns nothing.
- `bash -n scripts/mutate_*.sh` passes.
- Both guard tests pass on the merged tree.
- On the host (§5), `./scripts/mutate_system.sh` offline:
  - every `vac_*` tier kills its own mutant (M14-M21), and M13 survives in `vacuity`;
  - every other offline tier kills its real mutants and its inert control survives.
- `./scripts/mutate_cohorts.sh` kills M1-M6 and M9 (M7 is retired), and M0 survives.

**Deploy.** None. The M18 mutant edits a bind-mounted prompt, but only ever inside the
`mktemp` copy, never the live tree.

### P13 — Documentation (all CLAUDE.md edits; specs; plan erratum; `daily_audit`)

**Owns:**
- `CLAUDE.md`
- `specs/profile-versioning.md`
- `specs/data-model.md`
- `specs/profile-ingestion.md`
- `docs/plans/2026-09-22-pi-corpus-attribution-remediation-plan.md`
- `prompts/daily_audit.md`
- `tests/unit/test_claude_md_references.py` (new)
- `docs/audits/open-findings.md` (new)
- `tests/unit/test_open_findings_register.py` (new)

**CLAUDE.md edits.** Keep every phrase that `tests/unit/test_claude_md_disclosure_sync.py`
pins.
- **G1 (I30):** move lines `:1075-1103`, the "Four things to expect afterwards" list and its
  leading blank quote line, from the 0037 box to right after `:1056`, which ends the 0036 box.
- **G2 (I31):** `:965` "in the 2026-09-02 evaluation" becomes "in the 2026-09-03 evaluation
  (reported in the 2026-09-02 audit)".
- **G3 (I32):** `:232` "booked six sites" becomes "booked six unreserved sites (besides the
  two reserved turns)".
- **G4 (S1):**
  - `:219-221`: "`dirty state unknown` or an unknown commit … means an image built before the
    two-stage Dockerfile: images no longer carry `.git`, and the builder stage fails the build
    if `.build_info.json` cannot be written (rebuild)".
  - Add one sentence to the "agent image does NOT mount `src/`" box. It covers the filtered
    build context: no `.env`, `backups/`, `data/`, `logs/`, `profiles/` or `.venv-test`.
    Services get their env from `env_file:`. Never add a tracked path or `.git` to
    `.dockerignore`. Cite `tests/unit/test_docker_build_context.py`.
- **G5 (D6 = redesign):** replace `:375-379` with a paragraph saying `run_migration.sh`:
  - runs dump → preflight → apply → read-back → postflight;
  - uses one-off containers off the image just built;
  - refuses an image built from another commit;
  - takes its target from the image's `DEFAULT_TARGET`;
  - mounts `backups/` at `/app/backups`;
  - is used as step 4 in place of `alembic upgrade head`, rehearsed first without `--apply`.
- **G6 (M4):** `:556` becomes
  `docker compose -f docker-compose.prod.yml run --rm --no-deps -T -v "$PWD/data:/app/data" blackbird-app python scripts/export_agent_roster.py   # writes host data/agent_roster.json`.
  Add a note that `provision_slack_bots.py` refuses a roster older than 1 h unless given
  `--allow-stale-roster`.
- **G7 (D5 = keep uncommitted):** leave the "UNCOMMITTED edit" box at `:134-160` and its
  "never `git checkout`/`git stash`/`git restore`" rule as they are. Add one sentence: this
  was a deliberate decision on 2026-09-25 (D5), recorded as open in
  `docs/audits/open-findings.md`.
- **G8:** no edit. `:500`, "A corpus-stage failure FAILS the job", becomes true once P6
  lands.
- **G13 (with P10's I33).** The 0038 box's "(… the migration's own docstring, which says it
  is unaffected, predates that change.)" (`:1116-1118`) becomes "(… and the migration's own
  docstring now says so too.)". P10 rewrites that docstring, so without this edit
  CLAUDE.md turns false.
- **G15 (M2 class, with P5).** `:563` tells the operator to
  `docker compose -f docker-compose.prod.yml exec blackbird-app python scripts/backfill_agent_tokens.py`.
  `exec` runs in the long-running container, whose environment dates from its creation, so
  it would skip every newly added `SLACK_BOT_TOKEN_<ID>`. Rewrite it to
  `docker compose -f docker-compose.prod.yml run --rm --no-deps -T blackbird-app python scripts/backfill_agent_tokens.py --dry-run`,
  then the same without `--dry-run`, matching P2's printed block. Afterwards,
  `grep -n 'exec blackbird-app python scripts/backfill_agent_tokens' CLAUDE.md` returns
  nothing.
- **G14 (I6 class).** The "Running pytest inside the container" sentence's
  `docker compose exec blackbird-app python -m pytest ...` gains
  `-f docker-compose.prod.yml`, like the command block below it.

**Specs.**
- **G9 (I19):** `specs/profile-versioning.md` lines 5, 9, 19, 22, 37, 40, 49, 52, 73-76, 97
  and 100, and `specs/data-model.md` lines 213, 219 and 222: mark `private` and `slack_dm` as
  historical, and `monthly_refresh` as never a revision mechanism.
- **G9b (with P11e).** `specs/profile-versioning.md:106-117` (the helper block; `:104` closes
  the `create_revision` fence and must stay) documents `get_revision_history`,
  which P11e deletes. Replace the block with one sentence: history is read directly from
  `profile_revisions`, and there is no helper (removed 2026-09-25, D11).
- **I10-B:**
  - `specs/profile-ingestion.md`: add "(not populated; column reserved)" at `:65`; drop
    "last-author prioritized" at `:68` and `:113`; drop the `[last author / first author]`
    tag at `:114`.
  - `specs/data-model.md:75`: author_position is "never written".

**G10 (M3).** In the 2026-09-22 plan, after `:667`, add an erratum: the executed script keeps
the higher-year record and removes the other member, so the plan line had the direction
reversed. Before writing it, check the direction against `scripts/repair_pi_corpus.py` as it
stands after P6.

**G12 (D16). Delete `prompts/daily_audit.md`.**
- It hard-codes org1's `/home/ubuntu/copi-python` six times. The host's only daily-audit
  cron is org1's own (`cd /home/ubuntu/copi-python && ./scripts/run_daily_audit.sh`), which
  uses org1's copy. No blackbird job reads this file.
- `git grep daily_audit` outside `docs/` returns nothing. Mentions in dated docs stay.
- It is in no prompt set, so no `role.toml` bump is needed. Confirm that
  `test_doc_prompt_sync` does not embed it.

**Findings register (RCA §8.6: known findings not remediated).** S1 was rated CRITICAL on
2026-08-17 (C2 of `docs/audits/2026-08-17-user-account-types/final-infra-audit.md`) and was
still open five weeks later, because an audit's findings live only in its own prose.

New `docs/audits/open-findings.md` holds one table:

| id | source | severity | status | evidence | owner / decision |
|---|---|---|---|---|---|

- `status` is one of `open`, `fixed`, `refuted` or `deferred`.
- `evidence` is a commit hash or a repo `path:line` that shows the status.
- `owner / decision` names a person, a decision ID or "owner".

Seed it with two sets of rows:
1. **All 12 findings of the 2026-08-17 final infra audit.** Its `## Findings` section has the
   headings `### C1 — …` through `### L12 — …`: C1, C2, H3, H4, H5, M6, M7, M8, L9, L10, L11
   and L12.
   - C2 is this plan's S1 (P1, `fixed` once §7 verifies it).
   - M7 is the uncommitted compose file (D5).
   - Re-verify every other one against the tree and the host at execution time, and record
     its status with file:line or command evidence.
   - This plan fixes none of the others. Each one still `open` becomes a §8 follow-up.
2. **This plan's deferred and residual items:**
   - D4 (non-root `USER`);
   - D5 (RCA §8 cause 5: the prod compose file stays uncommitted);
   - D17 (73 bots with old scopes: 11 active and 62 inactive on 2026-09-25);
   - the §8 list.

The file's header states the rule: a new audit appends its findings here in the same
commit, and closing one means setting `status` and `evidence`.

New `tests/unit/test_open_findings_register.py`:
- `test_every_row_is_well_formed`: six cells; `status` is in the vocabulary; `open` and
  `deferred` rows name an owner or decision.
- `test_fixed_rows_cite_real_evidence`: a `fixed` row's evidence is either a commit that
  `git cat-file -e` finds (skip if there is no `.git`) or a `path:line` inside that file's
  length.
- `test_the_2026_08_17_infra_audit_is_fully_registered`: every `^### ([CHML]\d+) — `
  heading inside that audit's `## Findings` section has a row with id
  `2026-08-17/<ID>`. There are 12, and the test asserts that count as its control.
- Control: the table parses more than 5 rows.

**Guard.** New `tests/unit/test_claude_md_references.py`:
- `test_deploy_box_migrations_exist`: every "Deploy order for `00NN_slug`" names an existing
  file in `alembic/versions`.
- `test_referenced_repo_paths_exist`: backticked `src/`, `scripts/`, `tests/`, `templates/`,
  `prompts/`, `alembic/`, `docs/` and `specs/` paths must exist.
  - Expand `{a,b}` brace groups first, as in `docs/specs/2026-08-07-{pi,hub}-bot-prompts.md`.
  - Treat a path containing `*` as a glob that must match at least one file, as in
    `prompts/roles/*/role.toml`.
  - Strip a trailing `::node` (for example `test_x.py::test_y`), then a trailing `:N` or
    `:N-M`, before checking.
  - A path ending in `/**` passes if that directory exists. On Python 3.11, `Path.glob`
    returns only directories for a trailing `**`.
  - Controls: the guard passes on `bc68025`'s CLAUDE.md, and a planted `src/nope.py`
    fails it. Allowlist untracked or
  removed paths CLAUDE.md names on purpose, such as
  `profiles/private/blackbird.archived-2026-08-20.md` and `backups/*.dump`, with a reason for
  each.
- `test_line_references_are_in_range`: for every `path:N`, N must not exceed the file's
  length.

### P14 — Operator-text guards (RCA §8.4 and §8.5)

**Owns:**
- `tests/unit/test_operator_commands.py` (new)
- `tests/unit/test_compose_service_references.py` (new)

**`test_operator_commands.py`** scans every `scripts/**/*.{py,sh}`. It matches commands, not
prose, so a warning that says "a bare `docker compose` resolves to the dev stack" stays
legal.
- A command is `docker compose` followed, after optional `--profile X`, by a subcommand in
  `exec|run|up|build|stop|start|restart|cp|logs|ps|down|rm|pull|kill`.
  - It fails without `-f docker-compose.prod.yml` before the subcommand.
  - The one exemption is the `DC=(docker compose -f "$COMPOSE_FILE")` line of
    `run_migration.sh`.
  - The `mutate_*.sh` harnesses get no exemption: P12b removes every `docker compose` call
    from them.
- `\bexec(?: -T)?(?: -e \S+)* (?<![\w/.:-])app(?![\w/:-])` fails, and so does
  `(?<![\w-])app:/app`.
  - Not `\bapp:/app`: `\b` matches between `-` and `a`, so it would flag
    `blackbird-app:/app/…`. That covers P5's new `cp` lines and the already-correct
    `make_install_links.py:33`.
- `docker (stop|rm|start|restart|kill|logs|inspect|exec)\b[^\n]*\bblackbird-agent-run\b`, and
  `--name blackbird-agent-run`, are allowed only on lines that also contain "emergency".
  Prose that names the container is not a command.
- `&& docker compose` is forbidden.
- Positive control: `test_the_scanner_flags_a_planted_bare_command`.

**`test_compose_service_references.py`:**
- No compose-file test. Under D5 the committed `docker-compose.prod.yml` still names the web
  service `app`, so a test of the committed file fails in every clean clone, including §5's.
  A test of the working tree would pass only on the host. RCA §8 cause 5 is recorded as open
  instead (P13 register).
- `test_no_operator_text_targets_the_dev_web_service`: scan `templates/**/*.html` and
  `CLAUDE.md` for a `docker compose … (exec|run|up|build|logs|stop|restart) …` instruction
  whose service is the dev `app`, unless the line names `docker-compose.yml`. `scripts/` is
  P14's other test.
  - Match the service as a delimited token: `(?<![\w/.:-])app(?![\w/:-])`.
  - `app\b` would flag every correct line: `blackbird-app` (a word boundary sits between `-`
    and `a`), the `/app/…` container paths (P13 G6's `-v "$PWD/data:/app/data"`), and P2's
    banner command.
  - Controls, in both P14 tests:
    - positives that must be flagged: `docker compose exec app python x` and
      `docker compose exec -T app python x`;
    - negatives that must pass: `docker compose -f docker-compose.prod.yml exec -T blackbird-app python x`,
      `… run --rm -v "$PWD/data:/app/data" blackbird-app …`, `uvicorn src.main:app` and
      `blackbird-app:/app/scripts/`.

These guards pass only once P2, P3, P4, P5, P12b and P13 land. They are verified in §5.

### P15 — Dead-code guard (RCA §8.1)

**Owns:** `tests/unit/test_no_dead_src_symbols.py` (new).

**What the scanner looks at.** A pure AST scanner, with no vulture dependency.

Definitions:
- top-level functions, and methods of top-level classes, in `src/`;
- skipped: dunders, decorated definitions (other than `staticmethod`, `classmethod`,
  `property` and `cached_property`), and `Protocol` bodies.

The reference corpus:
- `src/`, `scripts/`, `alembic/`, and `\bname\b` tokens in `templates/**/*.html`;
- `tests/` does not count, and neither does `__all__`.

What counts as a reference:
- For a module function: a `Name` in its own module, `from M import f`, or `alias.f` where
  the alias is bound by an import.
- For a method: `Attribute.attr` equal to `name` or `"a"+name`, a `getattr` string, or a
  template token.

**How it decides.**
- Fixpoint: a reference only counts if it sits inside a definition that is not itself dead.
- `ALLOWLIST: dict[str, str]` maps each entry to a reason. The test fails on any unlisted
  dead symbol, and on any allowlist entry that is stale.
- Seven `tmp_path` control cases.
- Scope follows D15.

**Finalizing.** Build the allowlist during the integrated verification (§5), because it
depends on the merged tree. State the known limitation in the module docstring: dead methods
that share a name with a live attribute call are missed.

### P16 — Re-derive the tenure starts C1 may have thinned, and refresh their outputs (D7)

**Owns:**
- `scripts/rederive_tenure_starts.py` (new)
- `tests/integration/test_rederive_tenure_starts.py` (new)

**Consumes P6.** The strict corpus path: `resolve_corpus` raises `CorpusStageError` on any
stage failure. Also D8's rule. The script runs only on an image that carries P6.

**Candidates.** Every per-user row `jhu_tenure_start:{user_id}` (`jhu_rules.py:36`, JSON
`{"year", "source", "derived_at"}`) whose `source` is `earliest_hopkins_paper`.
- Rows with source `manual`, `curated-2026-08-13` or `orcid_employment`, and the legacy
  agent-id map, are never rewritten.

**Re-derivation**, the way the pipeline now derives a start when nothing is recorded
(`profile_pipeline.py:161-185` at `bc68025`):
1. Fetch the ORCID profile. If that fails, skip the PI with no write (D8).
2. `derive_employment_start(employments)` (`jhu_rules.py:70`). If it returns a year, that
   year is the new value, with source `orcid_employment`. C1b rows get their employment
   year this way.
3. Otherwise run `resolve_corpus(...)` exactly as the pipeline does, then
   `derive_start_from_papers(ranked)` (`jhu_rules.py:87`; `ranked` is the pre-cap list, as
   amended in §9), with source `earliest_hopkins_paper`. Any exception from
   `resolve_corpus` skips the PI with no write, and so does a corpus with
   `permanently_dropped` records (`incomplete_corpus`, §9 re-audit round).
4. If it derives no year, report the row and leave it. Nothing is ever deleted.

**Modes.**
- **Default: preview.** One line per candidate: user id, name, ORCID, and stored year →
  re-derived year and source, or the skip reason. No write, no job.
- **`--apply`:**
  - if the changes exceed `--max-changes` (default 10), abort before any write and print the
    table. The 2026-09-22 plan named six suspects, so far more than that means something is
    wrong;
  - refuse to start unless the backup directory is a mount point (override:
    `--allow-unmounted-backup-dir`), since a `run --rm` container without `-v` would
    delete the backup on exit;
  - before the first write, save every candidate row's key, its current value (`old`) and
    the value this run will write (`written`) to `/app/backups/tenure_starts_<UTC stamp>.json`
    (mode 0600);
  - write each change as a conditional update that matches the value read at the start, so a
    manual edit made during the run's network calls survives and is reported
    `changed_since_read`;
  - for each changed PI, add one
    `Job(type="generate_profile", user_id=uid, payload={"user_id": str(uid), "orcid": orcid})`,
    the payload shape at `admin.py:1413-1415`;
    - skip it if a `generate_profile` job is already **pending**. A `processing` job has
      already read the old year, so a new pending job is queued behind it;
    - the profile pipeline queues `enrich_grants` and `industry_evidence` itself
      (`profile_pipeline.py:548-549`), so the script does not queue them too, which would
      run them twice;
  - one commit, then print the counts.
- **`--restore <backup.json>`:** the rollback.
  - The whole file is validated first: format marker, `jhu_tenure_start:<uuid>` keys, and
    tenure-JSON values.
  - `old` is restored only where the user still exists and the row still equals `written`.
    It never inserts, so a purged key stays purged.
  - Restore does not re-queue profiles.
- **`--orcid` (repeatable):** scope the run.
- **Cost.** Live ORCID/OpenAlex/PubMed traffic per candidate, and no LLM in the script. Each
  queued profile regeneration costs about one synthesis LLM call on the worker (the live
  T4.1 run measures one), which the owner accepted.

**Tests.** `tests/integration/test_rederive_tenure_starts.py` loads the script by path and uses
the suite's `db_session`, with fakes for the ORCID fetch and `resolve_corpus`:
- `test_preview_writes_nothing_and_queues_nothing`;
- `test_a_disagreeing_paper_row_is_overwritten_and_its_profile_queued`;
- `test_orcid_employment_wins_when_it_is_available`;
- `test_manual_curated_and_employment_rows_are_never_touched`;
- `test_a_corpus_failure_skips_the_pi_with_no_write`;
- `test_an_orcid_failure_skips_the_pi_with_no_write`;
- `test_a_pending_profile_job_is_not_duplicated`;
- `test_max_changes_aborts_before_any_write`;
- `test_the_backup_is_written_before_the_first_write_and_restores_it`;
- `test_no_rederived_year_is_reported_not_deleted`.

**Acceptance.**
- The preview lists every `earliest_hopkins_paper` row.
- `--apply` changes only rows whose strict re-derivation differs, and queues one profile job
  per changed PI.
- The backup restores every prior value.

**Deploy.** Runs in the app image after §7's rebuild (§7 step 9). Its backup lands in host
`backups/`.

## 5. Integrated verification

This runs once, after all packages are merged. It runs in a scratch clone on the host; the
live tree is never used. Before starting, confirm with the other session that nothing else is
running a CI job.

**Scratch-clone setup.**
- Make a full `git clone` of `blackbird`, then merge every package into it.
- **Commit the merged tree in the clone**, as a local commit that is never pushed or landed
  from there. All three harnesses refuse to run while `git diff --quiet -- src/` is dirty
  (`mutate_system.sh:166`, `mutate_cohorts.sh:93`, `mutate_slack_mirror.sh:59`).
- `.venv-test` is untracked, so the clone has none. Point at the host's venv by absolute
  path, as the RCA's repro did (`repro.sh:6`):
  - `VENV_PY=/home/ubuntu/blackbird-copi-science/.venv-test/bin/python` for `ci.sh`
    (`ci.sh:34`);
  - `MUT_PYTHON=` the same path for the harnesses.
- Never `pip install` into that venv: it is shared with the live tree.
- Define `PY=/home/ubuntu/blackbird-copi-science/.venv-test/bin/python` once, and use `$PY`
  wherever the steps below say `.venv-test/bin/python`.
- **Re-commit in the clone** after step 2 (the P15 allowlist and any dead-code deletions),
  and after every repair in step 11, before steps 7 and 9. The harnesses refuse a dirty
  `src/`.

1. **Ownership diff.** Every changed file must belong to exactly one package, and nothing may
   change outside the plan's file list.
2. **Finalize the tree-dependent parts.**
   - **P15 allowlist.** Run the scanner. For each hit, either delete genuinely dead code (in
     scope if the RCA covers it; otherwise record it as a follow-up with a reason) or
     allowlist it with a reason. Apply D15's scope rule.
   - **Harness anchors.** Run `tests/unit/test_mutation_harness_targets.py`. Re-point every
     anchor that a package's edit moved, including any the reports flag (P6, P9, P11a,
     P11d). Choose M20's anchor against the merged `agent_page.py` so that it occurs exactly
     once.
3. **Lint.** `.venv-test/bin/python -m ruff check <ci.sh targets>` must report zero findings,
   and the src count must stay at or under the ceiling of 231.
4. **Alembic.** `.venv-test/bin/python -m alembic heads` must show a single head, still 0051.
   The 0038 edit only touches a docstring.
5. **Prompt docs.** `.venv-test/bin/python scripts/sync_prompt_set_docs.py --check` must exit 0.
   None of these edits touch `prompts/roles/**`.
6. **Full gate.** Run `./scripts/ci.sh` in the scratch clone, using a private throwaway
   Postgres via `CI_MIGRATION_DB`, as before.
7. **Image smoke build (P1), before production ever builds the new Dockerfile.**
   - Build from a full `git clone` of the merged tree, not a worktree: a worktree's `.git`
     is a file, and git fails in the source stage.
   - `docker build -t copi-blackbird-rca-verify:tmp <clone>`. This is a distinct tag, so no
     compose image, and nothing of org1's, is touched.
   - Then run:
     - `docker run --rm --network none --entrypoint sh copi-blackbird-rca-verify:tmp -c 'ls -d /app/.env /app/backups /app/.git /app/.venv-test /app/data /app/certbot /app/.superpowers 2>&1; command -v git; cat /app/.build_info.json'`.
       Every path must report "No such file", `git` must be absent, and the commit must
       equal the clone's HEAD.
     - `docker run --rm --network none -e DATABASE_URL=postgresql+asyncpg://x:x@127.0.0.1:1/x copi-blackbird-rca-verify:tmp python -c 'import src.main, src.worker.main, src.agent.supervisor'`.
       This needs no secrets and no network.
     - The tracked-tree check. Take `find /app -type f` from the image, drop `build/`,
       `*.egg-info`, `__pycache__` and `.build_info.json`, and it must be a subset of the
       clone's `git ls-files`.
   - Afterwards, `docker rmi copi-blackbird-rca-verify:tmp`, by that exact tag.
8. **Mutation re-checks.** Every RCA repro that proved a test vacuous must now FAIL against the
   fixed tests. Revert each mutation after its run.

   | Test | Mutation | Result now |
   |---|---|---|
   | reply-lane lock test (I20) | E2 (M14): contended acquires deadlock | FAIL |
   | floor test route row (I23) | E3 (M15): route re-exempted | FAIL |
   | floor test pass row (I23) | M16: `PANEL_EXEMPT_RECOMMENDATIONS = frozenset()` | FAIL |
   | `test_roles` rewrite (I21) | M18: revert `df4d975`'s sentence to "None of it may appear anywhere in `<slack_message>`" (the E7 state) | FAIL |
   | `test_roles` rewrite (I21) | delete the "Close with your verdict stated inline …" sentence from `thread_guidance.py:169-173` | FAIL |
   | delegate default test (I28) | E14 (M19): `default="accepted"` | FAIL |
   | harness-target guard (I7a/I7b) | restore M12b's old target, or M4's or S1's single-line anchor | FAIL |
   | citation guard (§8.4) | cite `v2 §9.5` or `v2 §9 req. 5` anywhere in a scanned root | FAIL |
   | cohort view test (I24b) | R1 (M21): `root_posts = []` | FAIL |
   | vacuity-tier sensitivity | revert the I23 route row to `pytest.param("route-to-incubation", set(), set(), id="route-to-incubation-armed-owes-pair")` (the id must stay, or the tier's node disappears), then apply M15 in `vac_i23_route` | SURVIVED |
   | race test (I29) | R2 (M20): lost race returns 500 | FAIL |
   | consult-retry booking test (C2) | R3 (M17): drop `on_retry=on_api_call` | FAIL |
   | postflight row counts (I3) | E10: the 0050→0051 inputs | PASS (was FAIL) |
   | `run_migration.sh` stub tests (I1) | E11 scenario | "applied nothing", not "silent rollback" |
   | corpus strictness (C1) | drop either `strict=True` | FAIL |
   | discussions filters (I9) | restore the first filter pass | FAIL |
   | `thread_ts` filter (I8) | delete the clause | FAIL |
   | Transport guard (P11a) | restore any removed Transport member | FAIL |
   | dead-code guard (P15) | restore a removed dead module function (e.g. `slack_web.post_message`) with no caller | FAIL |

9. **Harness runs, on the host.** Run `./scripts/mutate_system.sh` (offline tiers, including
   `vacuity`) and `./scripts/mutate_cohorts.sh` from the scratch clone, with `.venv-test`.
   `TEST_DATABASE_URL` stays unset, so each run starts its own testcontainers Postgres. The
   expected result is in P12b's acceptance. These runs record the killing mutation of every
   row above that has an M-label, so the table can be re-run later with one command.
10. **Credentialed tiers: no-spend only (D18).** Every command sets
    `ANTHROPIC_API_KEY=sk-ant-dummy-no-spend`. An environment variable overrides `.env` in
    `Settings`, so any accidental Anthropic call fails with 401 and spends nothing.
    - T4.4, unmutated:
      `LIVE_API_TESTS=1 ANTHROPIC_API_KEY=sk-ant-dummy-no-spend $VENV_PY -m pytest -q tests/integration/test_profile_pipeline_live.py -k t44 -m 'live_api and real_llm'`.
      The dummy satisfies the module's key gate, and T4.4 asserts no synthesis ran.
    - `LIVE_API_TESTS=1 ANTHROPIC_API_KEY=sk-ant-dummy-no-spend MUT_TIERS="orcid pubmed_tool pubmed_doi pubmed_both" ./scripts/mutate_system.sh`.
      M12b must survive, and the live real mutants must be killed.
    - **Not run, reported as unverified:** T4.4's `_stage` → `return []` mutant (it would
      spend 1-2 calls), the P11c live Slack files, `mutate_slack_mirror.sh`, and the
      `pipeline` tier.
11. **On failure.** Dispatch `engineering:test-triage` with the captured output, repair, then
    rerun the failing check and then the full gate.

## 6. Adversarial audit

This runs only once §5 is green. Every subagent runs on Opus 5.5; none on Fable.

- `engineering:plan-auditor` gets this plan and the full diff. Its job is to prove the plan is
  NOT done: every package's acceptance criteria against the repository artifacts.
- `engineering:semantic-reviewer` gets the diff.
- `engineering:security-reviewer` covers the trust-boundary changes:
  - the P1 image and build context;
  - the P11b Slack scopes;
  - the P9 proposal routes;
  - P10's impersonation wording against the real refusal sites.

Merge the three reports, fix every confirmed finding, rerun §5, and re-audit the fixed areas.

## 7. Deploy and rollback

There is no migration; the schema stays at 0051 (or at whatever head the other session's
work has reached when this deploy runs; P3/P4 derive it). Run everything from
`/home/ubuntu/blackbird-copi-science` on the host, with
`DC="docker compose -f docker-compose.prod.yml"`. Never touch a `copi-python-*` container,
never pass `--remove-orphans`, and never start a run: runs start from `/admin/simulation`
only.

1. **Coordinate.** Another session deploys from this tree (it tagged `rollback-pre-3.5.0`
   on 2026-09-25). Agree a window with it first.
2. **Rollback tags, from what is actually running**, not from `:latest`, which a build
   without an `up` may already have moved. For each of `app:blackbird-app`, `worker:worker`
   and `agent:agent` (container suffix : image repo suffix):
   ```
   docker image tag "$(docker inspect copi-blackbird-<c>-1 --format '{{.Image}}')" copi-blackbird-<r>:rollback-pre-rca
   ```
3. **Settings-hash baseline**, on the old images, from fresh one-off containers. A
   long-running container carries whatever `.env` it was created with, so it would mix
   env drift into the S1 comparison:
   ```
   H='import hashlib; from src.config import get_settings as g; print(hashlib.sha256(g().model_dump_json().encode()).hexdigest())'
   $DC run --rm --no-deps -T blackbird-app python -c "$H"
   $DC run --rm --no-deps -T worker python -c "$H"
   $DC --profile agent run --rm --no-deps -T agent python -c "$H"
   ```
   - `config.py` has no `SecretStr`, so the dump covers every value, secrets included. Only
     the hash leaves the container.
   - `run` overrides the service command, so the agent one-off imports config and exits. No
     simulation starts.
   - Before step 3, check once that compose accepts a one-off for the agent service, whose
     `container_name` is pinned:
     `$DC --profile agent run --rm --no-deps -T agent python -c 'print(1)'`.
     If it refuses, take the agent's baseline and post-check with
     `docker exec copi-blackbird-agent-1 python -c "$H"`, before and after its recreate.
     That reflects the container's creation-time env, so compare the agent only with
     itself. Do not substitute `docker run --env-file`: its quote handling is not
     established to match compose's `env_file:`.
4. **Land** the merged tree on `blackbird`: per-file 3-way, never overwriting another
   session's uncommitted work.
   - Then `git status --porcelain --untracked-files=no` must list exactly
     ` M docker-compose.prod.yml` (D5 keeps it uncommitted) and nothing else, because the
     build bakes the working tree.
   - Also, `git status --porcelain --untracked-files=all -- src templates static prompts alembic scripts pyproject.toml alembic.ini`
     must print nothing. The builder's `git clean -ffdx` silently drops untracked files,
     so an un-added template, prompt or revision would be missing from the image. If another session has other uncommitted tracked
     changes, stop and coordinate, since they would ship too. Never stash or check out
     their work.
   - `prompts/profile-synthesis.md` and `prompts/daily_audit.md` take effect on landing,
     because they are read per use.
5. **Build:** `$DC build blackbird-app worker && $DC --profile agent build agent`.
6. **Verify the built images**, for each of the three:
   - repeat §5 step 7's path, git and `.build_info.json` checks with `docker run --rm --network none --entrypoint sh <image> -c …`;
   - `commit` must equal HEAD, and `dirty_files` must be 1 (`docker-compose.prod.yml`, D5);
   - repeat step 3's hash with the new images, before anything starts. It must equal the
     baseline.
   - On a mismatch, do not `up`. Nothing has started, so there is nothing to roll back;
     find the key that moved first.
7. `$DC up -d blackbird-app worker`. `/admin/simulation` must show the new commit with
   source `build_info_json`.
8. `$DC --profile agent up -d agent`, **only** when `/admin/simulation` shows no live run.
   It returns IDLE.
9. **D7: re-derive the tenure starts (P16).**
   - Preview:
     `$DC run --rm --no-deps -T -v "$PWD/backups:/app/backups" blackbird-app python scripts/rederive_tenure_starts.py`.
   - Then the same command with `--apply`. It aborts on its own above `--max-changes`.
   - Record the preview table and the backup file name in the deploy notes.
   - The queued profile regenerations run on the worker.
   - Rollback: `… rederive_tenure_starts.py --restore /app/backups/tenure_starts_<stamp>.json`.
10. Optional, operator-gated: rehearse the redesigned guarded path (P4) without `--apply`.
    Preflight is read-only.
11. **Post-deploy decisions.**
    - **D1**, after one healthy day:
      - tag the running images `rollback-post-rca`, from their image IDs as in step 2;
      - delete every other `copi-blackbird-*` tag by explicit name, `rollback-pre-rca`
        included;
      - remove this project's dangling images by ID:
        `docker images -q -f dangling=true -f label=com.docker.compose.project=copi-blackbird`.
        Compose-built images carry that label (checked 2026-09-25);
      - remove its exited containers by ID (none on 2026-09-25), saving the logs of any
        `blackbird-agent-run` first;
      - post-check: for every remaining `copi-blackbird-*` image,
        `docker run --rm --network none --entrypoint sh <img> -c 'ls /app/.env /app/backups'`
        reports "No such file" twice.
    - **D2:** `docker buildx du --verbose`, then `docker builder prune -a`, with the owner's OK.
    - **D3:** no rotation. The owner confirmed that no image ever left the host.
    - **Tenure-scope audit.** Run `scripts/audit_tenure_scope.py` once after step 9's
      regenerations finish. It is read-only and exits non-zero on any persona that leaks
      a pre-tenure publication.

**Rollback.**
- For each service:
  `docker image tag copi-blackbird-<r>:rollback-pre-rca copi-blackbird-<r>:latest`.
- Then `$DC up -d blackbird-app worker`, and `$DC --profile agent up -d agent` only when no
  run is live.
- `git revert` the landing commit(s). The prompt edits roll back with the revert, because
  they are read per use.

## 8. Residual and follow-ups (not fixed by this plan)

Each item is also a row in `docs/audits/open-findings.md` (P13), with its owner.

- **S1's second half.** A non-root container `USER` (D4, owner).
- **Slack.**
  - Existing Slack bots keep `groups:write` / `im:*` until they are reinstalled (D17,
    owner). The reinstall procedure is UNDETERMINED.
  - The scopes for `chat.getPermalink` and `auth.revoke`, and whether posting to a user ID
    needs `im:write`, must be checked against Slack's docs (P11b rows, RCA §9).
- **Production-data questions from RCA §9.** Threads with more than one decision (I9);
  legacy `outcome='proposal'` rows (I13); historical `private` / `slack_dm` revisions
  (I19); superseded rows with a NULL `raw_verdict` (I16).
- **Measured, not fixed.**
  - Hub compliance with the fixed scout_hub prompt set (1.7.1, now 1.8.0 after the other
    session's 3.5.0 change) is unmeasured until a run.
  - org1's `agent-run` StopTimeout (I35) is org1's to set; it is recorded only.
- **I34.** This repo's nginx is not live: on 2026-09-25 no `copi-blackbird` nginx container
  exists. Re-check this if the project ever adds one.
- **I22's bisect hazard.** Commits `3dcc00a..c62af04` do not import, and history cannot be
  fixed. Anyone bisecting across them must skip them.
- **The 2026-08-17 final infra audit's other findings** (C1, H3, H4, H5, M6, M8, L9-L12).
  P13 re-verifies them into the register, and each one still open is a follow-up.
- **SSH.** The host key mismatch on the NAT64 IPv6 path (RCA §10). Verify it out of band
  and keep SSH on IPv4.

## 9. Execution record (2026-09-25)

**Implementation.** All 21 packages were implemented in parallel in a local clone of
`blackbird` @ `9e53735`. Each package was written by an Opus 5.5 `bulk-implementer`,
at most 12 at a time. There was no build and no test before integration.

**Reconciliation edits by the main session, before integration:**
- **D13:** removed `include_private` from `Transport`, `NullTransport`,
  `AgentSlackClient.list_channels` and the fake. The contract test now pins public-only
  listing.
- **`specs/agent-system.md:171`:** the spike script is marked removed.
- **`specs/cohort-system-v2.md`:**
  - a §10 "superseded" note: `e541706` replaced the two-tier scheduler, and D12 retired
    the priority rule;
  - the `:25` row;
  - the harness sentence at `:1040`, which is M7 retired.
- **`docs/production-migration.md`:** postflight check 7's new title.
- **`scripts/provision_slack_bots.py`:** the `--add-scope` help names both
  `su:groups:write` and `su:groups:read`. The live tier's private-channel listing needs
  `groups:read`, which `BOT_SCOPES` no longer requests.
- **`tests/integration/test_profile_pipeline_live.py`:** a stale T4.4 failure message.
- **`scripts/mutate_system.sh`:** the `vac_i23_*` tiers select P12a's explicit
  parametrization IDs (`[pass-armed-exempt]`, `[route-to-incubation-armed-owes-pair]`),
  quoted inside the select string.

**Deviations the implementers reported, beyond the letter of the plan.** The §6 audit
checks each of these.
- **P9:** removed `reopen_proposal`'s second existing-review check. Under a race it let a
  second PI inbox row commit without error, which the plan's rollback claim relied on not
  happening.
- **P8:** the discussions page's agent list is now built from the whole run, like the
  counts.
- **P12b:** only pytest exit 1 is scored as a kill. 124 and 137 are TIMEOUT, and any other
  code is ERROR.
- **P16:** any exception from `resolve_corpus` skips the PI with no write, not only
  `CorpusStageError`.
- **P13:** register statuses are its own judgement: C2/S1 stays open until §7, and H4 and
  M6 are fixed.

**Audit-fix round (2026-09-25, after the §6 audit).** Eight Opus reviewers found nothing
critical. They reported:
- 5 plan audits of 21 packages: 152 of 169 items verified complete. The rest were minor
  gaps, plus two major ones: the Dockerfile pip layer copied from the build context, and
  P16's write was unconditional;
- 2 semantic reviews: 15 findings, 3 of them medium;
- 1 security review: 8 findings, 2 of them medium.

After deduplication, every confirmed finding was fixed:
- **Image:** every final-stage COPY is `--from=source`.
- **Proposal routes:** only a `uq_proposal_reviews_decision_agent` violation is a lost
  race. Anything else re-raises, and a lost race logs a WARNING.
- **Discussions:** the footer is relabelled "Total threads", and "(filtered)" also covers
  the agent filter.
- **Corpus:** tenure comes from `CorpusResult.ranked`, the pre-cap list. Strict mode fails
  only on transient errors.
- **P16:**
  - writes are conditional;
  - the dedupe is pending-only;
  - the backup is 0600 on a required mount and records both old and written values;
  - restore is validated;
  - an empty name is filled from ORCID.
- **Migration:**
  - `run_migration.sh` gives restore advice only when it has a dump and the schema failed;
  - it detects writers more fully;
  - the DSN is passed by name, and a dump from a foreign host is refused;
  - the agent-start line is guarded;
  - `compare_row_counts` fails on a missing expected table;
  - postflight checks the snapshot's database identity.
- **Slack:**
  - the backfill and PI deletion use `is_valid_token`, and revoke only bot tokens;
  - su's `groups:read` is named everywhere;
  - `scopes_for` refuses a contradictory omit/add pair.
- **Mutation harness:**
  - a kill must name a FAILED test;
  - override directories are owner-checked, non-symlink and 0700;
  - INT/TERM cleanup;
  - the `copi` database refusal parses the URL.
- **Guards:**
  - the P15 scan seeds only the two framework entry points;
  - the operator-command guard catches dev-service `run`/`up` and `:-app` defaults;
  - the CLAUDE.md guard checks `./scripts/…` paths;
  - the banner test pins the OFF sentence.
- **Docs:**
  - the register is redacted, with a do-not-push note, and H4 is back to open;
  - CLAUDE.md's runbook step 4 points to the guarded path;
  - CLAUDE.md warns that untracked files are dropped from images;
  - the spec, scope and D12 wording is corrected;
  - the P16 text above is updated.

**Re-audit round (2026-09-25, after the audit-fix round).** A focused re-audit of the fixed
areas found one high regression and several smaller gaps. All were fixed:
- **Proposal routes:** the lost-race WARNING read `agent.agent_id` after the failed flush,
  which raised `PendingRollbackError` under concurrency. It now logs the path parameter.
  Any other `IntegrityError` rolls back, logs the constraint name at ERROR, and returns a
  bare 500.
- **`run_migration.sh`:**
  - a DSN whose query string names a host or port is refused;
  - a postflight that exits 1 without naming a failed check gets its own message, which
    says not to restore;
  - the row-count-only message is labelled as such.
- **Corpus strictness:** see the second P6 amendment above. `rederive_tenure_starts.py`
  also changed:
  - it skips a PI whose corpus has drops (`incomplete_corpus`);
  - `--apply` skips a change to a LATER year (`later_than_stored`) unless `--allow-later`;
  - it reports paper-sourced rows that `--restore` could not read back
    (`unparseable_key`/`unparseable_value`);
  - its image-vintage guard requires `permanently_dropped` and ORCID `strict`.
- **Mutation harness:**
  - the `copi` refusal also reads `database=`/`dbname=` query parameters;
  - the DSN and the API key reach the test run through the environment, never `env`'s
    argv;
  - HUP gets the same cleanup as INT and TERM.
- **Docs:**
  - `docs/production-migration.md` step 10 checks the agent command by parsing compose's
    JSON;
  - CLAUDE.md opens with a do-not-push warning;
  - the register gains `R-admin-token-form` and `R-enrichment-fixwave`.

**Re-audit round 3 (2026-09-25).** A semantic review of round 2 found four gaps, all fixed:
- **Medium:** strict `fetch_orcid_works` raised on ORCID's record-state answers — 301
  (deprecated), 409 (locked; deactivated measured 409 with error-code 9044) and 410. Each
  repeats on every retry, so they dead-lettered the PI. They now read as no works.
- **Medium:** the systemic rule counted only 4xx, so an outage page served with 200 to every
  request dropped the whole corpus and the job still succeeded. Three identical unreadable
  bodies now re-raise too (`_failure_signature`).
- **Low:** `--allow-later` also held back a later ORCID-employment year, which the pipeline
  prefers over any paper year. The guard now applies to paper-derived years only.
- **Low:** a DOI whose lookup failed but whose paper arrived through another stage marked
  the corpus incomplete forever. It is no longer counted.

