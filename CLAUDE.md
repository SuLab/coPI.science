# CLAUDE.md

This file keeps only what every session needs. The operator detail behind each rule is in
`docs/operations/`: read the file for an area before working in it. `src/agent/CLAUDE.md`
and `alembic/CLAUDE.md` load on their own when you read files in those directories.

> ⚠️ **`origin` (`SuLab/coPI.science`) is public — do not push `blackbird` blind.**
> `docs/audits/` and `docs/plans/2026-09-25-rca-remediation-plan.md` describe open
> weaknesses of the live host, tracked in `docs/audits/open-findings.md`. Until its
> C2 row (pre-fix images still on the host, removed by decision D1) and the SSH
> host-key row are closed, scrub those records before any push.

| Before you… | read |
|---|---|
| run or change tests, or chase a failure that smells like the Anthropic SDK | `docs/operations/testing.md` |
| run any `docker` command, deploy, or start/stop/restart the simulation | `docs/operations/host-and-simulation.md` |
| write, apply or deploy a migration | `docs/operations/migration-deploy-notes.md`, `docs/production-migration.md` |
| add, provision, activate or delete a PI; touch roles, sessions or the Origin guard | `docs/operations/pis-and-access.md` |
| touch the assessment chat | `docs/operations/assessment-chat.md` |
| touch BlackbirdBot, the rubric, prompts, verdict capture, specialists or the review bot | `docs/operations/blackbird-hub.md` |

## Testing

- `./scripts/ci.sh` is the whole gate; there is no server-side CI. It runs alembic
  sanity (one head, no duplicate revision ids), the compiled-CSS drift check
  (`scripts/build_css.sh --check`), an upgrade→downgrade→upgrade round trip
  on a throwaway Postgres, `ruff check` on `tests/` (zero findings) and on `src/` (a
  ratcheted ceiling), then the full pytest run with a branch-coverage floor. Run it
  before committing. `scripts/install-hooks.sh` installs it as a `pre-push` hook.
- Its throwaway Postgres is named `copi-ci-migcheck-<pid>` on a free port, so two runs no longer
  collide on it (a leaked container from an interrupted run is removed by name at that run's exit).
- Run pytest on the host, not in a container (the images install without the `[dev]`
  extra, so pytest is absent there): `.venv-test/bin/python -m pytest tests/ -v`. With
  `TEST_DATABASE_URL` unset, `tests/conftest.py` starts its own Postgres through
  testcontainers. Create the venv ON THE HOST if missing:
  `uv venv .venv-test && uv pip install --python .venv-test/bin/python -e '.[dev]'`.
- A set `TEST_DATABASE_URL` is migrated with the real alembic chain. Never point it at
  `copi`, the production database; use a scratch database, a distinct one per
  concurrent suite.
- The checkout may be an sshfs mount of the host. Never `pip install` into `.venv-test`
  from the client: it rewrites the console-script shebangs to a path the host lacks,
  and DB-backed tests then fail with a bare `FileNotFoundError`. Pytest over sshfs can
  be 100-400x slower; check the mount before suspecting a hang.
- The suite and the images resolve different `anthropic` versions (`pyproject.toml`
  pins only `>=0.26.0`; measured 2026-09-29: 1.9.0 in all three images, 0.120.2 in
  `.venv-test`). Do not tighten the pin. `acreate`'s
  `NonStreamingMaxTokensError` in `src/services/llm.py` is the only enforcement of the
  non-streaming `max_tokens` ceiling (`NONSTREAMING_MAX_TOKENS`): the SDK skips its own
  check because the client sets a custom timeout. Do not remove it.
- Never run `pytest --snapshot-update` to clear a mismatch in
  `tests/characterization/__snapshots__/test_agent_turn_gm.ambr`: it pins the `pi_lab`
  guidance strings, and a change there takes an operator-directed, audited regeneration.
- After editing anything under `prompts/` or the `_PI_LAB`/`_SCOUT_HUB` guidance, run
  `.venv-test/bin/python scripts/sync_prompt_set_docs.py` (`--check` reports drift).
  A prompt-set edit also bumps `version` in `prompts/roles/*/role.toml`.

## The host runs TWO stacks

- A second, unrelated deployment (org1: `/home/ubuntu/copi-python`, compose project
  `copi-python`, serving `copi.science`) shares the host. Its simulation container is
  the UNPREFIXED `agent-run`: `docker stop agent-run` stops org1's production. Before
  touching any container, check
  `docker inspect <name> --format '{{index .Config.Labels "com.docker.compose.project"}}'`
  (`copi-blackbird` is this repo).
- Always pass `-f docker-compose.prod.yml` (`DC="docker compose -f docker-compose.prod.yml"`).
  Bare `docker compose` is the dev stack, whose web service is `app`. Never pass
  `--remove-orphans`: it has killed org1's nginx and certbot.
- The web service is `blackbird-app` only in the working tree's UNCOMMITTED
  `docker-compose.prod.yml`; the committed file still says `app`. That edit also pins
  the `blackbird-app` and `agent` container names, joins `copi-edge`, swaps `awslogs`
  for `json-file`, gives the worker its `prompts/` mount and runs the agent service as
  `src.agent.supervisor`. It stays uncommitted by decision
  (D5 of `docs/plans/2026-09-25-rca-remediation-plan.md`): never `git checkout`,
  `git stash` or `git restore` it, and don't commit it unless the owner reverses D5.
- Never accept a changed SSH host key for the host (open-findings row
  `2026-09-25/R-ssh-host-key`).

## Deploying

- Nothing migrates the database for you, and new code on the old schema breaks the
  site. Build only (`$DC build blackbird-app worker`, then `$DC --profile agent build agent`),
  then migrate from a one-off container off the NEW image
  (`./scripts/migrate/run_migration.sh`; rehearse without `--apply` first), then
  `$DC up -d blackbird-app worker`, and last `$DC up -d agent`, ONLY when
  `/admin/simulation` shows no live run: recreating the supervisor ends a live run.
  Never `up -d --build`, and never `exec` alembic into the old container, which cannot
  see a new revision.
- Commit before building. The builder stage's `git clean -ffdx` drops untracked files
  from the image, so this must print nothing first:
  `git status --porcelain --untracked-files=all -- src templates static prompts alembic scripts pyproject.toml alembic.ini`.
- The agent image bakes `src/` in; only `profiles/`, `prompts/` and `data/` are mounted.
  Any change under `src/agent/`, or to anything it imports, needs the agent image
  rebuilt, and the startup banner confirms which code is running.
- The engine loads the prompt set, the specialist personas, each `role.toml` and the
  rubric ONCE per start (`src/agent/prompt_snapshot.py`). An edit under `prompts/` does
  not reach a running agent: `/admin/simulation` shows "Prompt set changed on disk —
  restart to apply", and the edit applies at the next start (a restart needs no live
  run, but start FRESH after a version bump). The web app still reads prompts per
  request.
- `.env` changes need a container recreate, not a restart, of every service that reads
  it: `$DC up -d --force-recreate blackbird-app worker`, and `agent` only with no live run.
- `0056` (head is `0057` since 2026-10-02): run `scripts/migrate/remediate_0056.py --jobs --provisions --publications --emails`
  as a dry run first, and apply the publications dedupe only with the owner's go-ahead
  (the "Deploy order for `0056`" box in `docs/operations/migration-deploy-notes.md`).
- A migration that alters `jobs` (`0053`) needs the worker idle (no `processing` row) or
  stopped: the worker holds its transaction across a whole pipeline run and trips the
  chain's 10 s `lock_timeout`.
- After any code change that affects the running agent process, tell the user so they
  can decide whether to rebuild and restart.

## The simulation

- Never start a run as part of a deploy. Runs start only from `/admin/simulation`. The
  `agent` service runs `src.agent.supervisor`, which marks any pending start stale on
  boot and comes back idle. A one-off `$DC --profile agent run --name blackbird-agent-run …`
  is the emergency CLI path only.
- Stop gracefully: the admin page's Stop, or `docker stop -t 420 copi-blackbird-agent-1`
  (an emergency CLI run lives in `blackbird-agent-run` instead). Never `docker rm -f`,
  which skips the shutdown flush. Save logs after the stop. Stop and SIGTERM announce
  every owed headline, open interviews included (capped at 25); "Stop — hold open
  interviews" announces only ended interviews and marks the run held. A stall, an escaped
  exception or a failed start also holds. A natural end (max runtime, a drained
  `--max-proposals`) announces everything and FINALIZES the run, which can never be
  resumed. Detail: `docs/operations/host-and-simulation.md`.
- One engine at a time: the engine holds a Postgres advisory lock on a dedicated
  connection (`src/services/advisory_locks.py`). A second engine, CLI or supervisor,
  raises `EngineAlreadyRunning` before writing anything. The panel's liveness comes from
  that lock; "Unresponsive" means the lock is held but the heartbeat is stale, and Stop
  still reaches it.
- **Finalize run** (the button on a stopped run's `/admin/activity/<id>` page) announces
  the run's owed headlines, then sets `finalized_at`; a finalized run can never be
  resumed and the Start form forces Fresh. The supervisor runs it under the engine lock
  when no engine is alive.
- A resume (the CLI default, or the Start form with its default-checked "Fresh run" box
  cleared) restores from the database only (nothing is re-read from Slack), clears a held
  run's hold, and has the hub interview lab pitches the stop left unanswered. It
  announces, at shutdown, every verdict of that run whose `summary_posted_at` is NULL, and
  a Slack headline cannot be retracted. Before resuming a run with rows written before
  migration `0041`, stamp or repair them with `scripts/backfill_assessment_headlines.py`
  (the `0041` box in `docs/operations/migration-deploy-notes.md`). The script claims every
  post, refuses to write (exit 2) while an engine holds the engine lock, holding it
  itself while it writes (`--run-crashed` is a no-op), and `--finalize`, `--list-in-doubt` and `--release-in-doubt` release
  held and in-doubt headlines. Start FRESH after a prompt-set or rubric version bump, or
  one run mixes versions.
- `--fresh` deletes nothing: the new `simulation_run_id` is the isolation, and rows
  accumulate across runs. `--budget` is deprecated; leave it at 0.
- `llm_calls_per_load_per_window` and `hub_llm_calls_per_window` count real API calls
  (not turns) since 2026-08-22 and have not been re-tuned. Do not raise them on your
  own initiative; tuning is the owner's call.
- Roster changes (activating an agent, a new `AgentRegistry.slack_bot_token`) are picked
  up live, with no restart.

## Access and accounts

- `BASE_URL` must be the public origin. `OriginGuardMiddleware` refuses every non-GET
  whose origin does not match, and a wrong or missing `BASE_URL` fails the whole site
  closed: every form 403s with `Cross-site request refused.` while pages still render.
  A scripted POST needs `-H "Origin: $BASE_URL"`.
- `users.user_role` (`pi`, `manager`, `admin`, `reviewer`) is the source of truth.
  `User.is_admin` is a read-only hybrid over it. Never widen `is_admin`: impersonation
  is gated on it.
- Exclude `manager`, never "non-PI": admins keep the PI surfaces. PI-write routes use
  `get_pi_user`; `is_staff` is admin or manager and excludes reviewers; `get_review_user`
  admits reviewers. The manager router's write routes are an explicit allowlist that
  `tests/integration/test_manager_views.py::test_manager_router_mutations_are_an_explicit_allowlist` pins.
- Delete a user only through `src/services/user_deletion.py::delete_user_account`,
  never `db.delete(user)`: a raw delete leaves the PI's agent running.
- The `AgentRegistry` table is the single source of truth for the agent roster.
- An `AgentRegistry.role` must have an entry in `src/agent/role_capabilities.py`; an
  agent with an unknown role, or a role whose `role.toml` fails strict validation, is
  skipped by the engine (it used to run as pi_lab). `scripts/migrate/preflight.py`
  BLOCKs on such a role.

## Data you must not destroy

- Never `DELETE FROM simulation_runs`: every run-produced table cascades from it.
  Deleting an `opportunity_assessments` row also deletes its human reviews, status
  events and assignments.
- A rubric regime change stamps and keeps; it never purges. On every `[meta].version`
  bump of `prompts/rubric/blackbird-rubric.toml`, append the OUTGOING document's entry to
  `prompts/rubric/revisions.toml` in the same commit.
- New migrations follow the established shape: additive, nullable, migrate before the
  new code serves. NULL means "never asked" and is not backfilled.
