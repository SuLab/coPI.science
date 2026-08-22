# CLAUDE.md

## Testing

Run `./scripts/ci.sh` before committing — alembic sanity (single head, no
duplicate revision ids), an upgrade→downgrade→upgrade round trip against a
throwaway Postgres it creates and destroys itself, `ruff check` on the test
suite (zero findings) plus a ratcheted ceiling on `src/`, then the full pytest
run with a branch-coverage floor. This is exactly what the `pre-push` hook
runs, and it is the whole gate: there is no server-side CI.

**The supported way to run pytest alone is on the host, not inside a
container:**

```bash
.venv-test/bin/python -m pytest tests/ -v
```

(If `.venv-test` doesn't exist yet: `uv venv .venv-test && uv pip install
--python .venv-test/bin/python -e '.[dev]'`.) The host has a Docker socket, so
with `TEST_DATABASE_URL` unset, `tests/conftest.py` spins its own ephemeral
Postgres via testcontainers and migrates it with the real alembic chain — no
container, no manual database, no env var needed. This is exactly what
`scripts/ci.sh` runs.

> ### ⚠️ The suite does not necessarily run production's Anthropic SDK.
>
> `pyproject.toml` pins only `anthropic>=0.26.0`, and the two environments have
> resolved different versions: the deployed agent image has **1.0.0**, while
> `.venv-test` has **0.120.2** (both measured 2026-08-21). So a test that passes
> here says nothing certain about SDK behaviour in the container. **Do not
> "fix" this by tightening the pin** — dependency churn on a live deployment is
> the riskier move; just know the skew is there when a failure smells like the
> client library.
>
> The one SDK behaviour this has already cost us is the non-streaming
> `max_tokens` ceiling: `BaseClient._calculate_nonstreaming_timeout` refuses any
> non-streaming request with `max_tokens > 21_333`
> (`3600 * max_tokens / 128_000 > 600s`). **Both** versions carry that guard,
> verified in each — but no test could see it, because the suite drives
> `tests/fakes.py`'s `FakeAnthropic` and never reaches the real client. That is
> why the fake now enforces the same limit itself
> (`_MAX_NONSTREAMING_MAX_TOKENS`, re-derived from the SDK's arithmetic rather
> than imported from `src`), and why `src/services/llm.py` clamps every
> truncation retry to `NONSTREAMING_MAX_TOKENS` and raises on a call site above
> it. See `tests/unit/test_llm_nonstreaming_ceiling.py`.

Running pytest **inside the container** (`docker compose exec blackbird-app
python -m pytest ...`) does not currently work: the image installs with
`pip install --no-cache-dir .` (`Dockerfile:14`), with no `[dev]` extra, so
pytest is not installed there (verified: `exec blackbird-app python -c "import
pytest"` → `ModuleNotFoundError`). Restoring that path would need the image (or
a test-targeted variant of it) to install `.[dev]` instead.

If that path is ever restored, `TEST_DATABASE_URL` becomes required again: the
web container has no Docker socket, so without it every test that needs a
database errors out (469 of them, measured 2026-08-04):

```bash
docker compose -f docker-compose.prod.yml exec -T \
  -e TEST_DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_a3 \
  blackbird-app python -m pytest tests/ -v
```

The service is **`blackbird-app`**, not `app` — see the two-stack warning under
"Running the Agent Simulation" for why the `-f docker-compose.prod.yml` is not
optional.

Whichever way you run it, a database named in `TEST_DATABASE_URL` must already
exist — the suite migrates it, it does not create it. Add a fresh scratch DB
with `docker compose -f docker-compose.prod.yml exec -T postgres createdb -U
copi copi_xN`, and give concurrent suites distinct names so they do not
migrate each other's schema mid-run. Never point `TEST_DATABASE_URL` at `copi`,
the dev database.

## Running the Agent Simulation

> ### ⚠️ This host runs TWO stacks. Read this before any `docker` command.
>
> A second, unrelated CoPI deployment (**org1**, `/home/ubuntu/copi-python`,
> project `copi-python`, serving `copi.science`) shares this host. Its
> simulation container is named **`agent-run`** — the *unprefixed* name. This
> repo's is **`blackbird-agent-run`**.
>
> **`docker stop agent-run` / `docker rm agent-run` stops org1's PRODUCTION run.**
>
> Always confirm ownership before touching any container:
>
> ```bash
> docker inspect <name> --format '{{index .Config.Labels "com.docker.compose.project"}}'
> # copi-blackbird = this repo.  copi-python = org1, DO NOT TOUCH.
> ```
>
> Two more rules that follow from the shared host:
> - **Always pass `-f docker-compose.prod.yml`.** Bare `docker compose` resolves
>   to `docker-compose.yml`, a *different* (dev) stack whose web service is named
>   `app`, runs `--reload`, bind-mounts the whole repo, and binds host `:8001`.
>   The deployed stack is `docker-compose.prod.yml`, whose web service is
>   `blackbird-app`. `COMPOSE_PROJECT_NAME=copi-blackbird` in `.env` fixes the
>   project name but *not* the file.
> - **Never pass `--remove-orphans`** — it has killed org1's nginx + certbot.

The simulation runs in a one-off container named `blackbird-agent-run`:

```bash
DC="docker compose -f docker-compose.prod.yml"

# Resume an existing run:
$DC --profile agent run -d --name blackbird-agent-run agent python -m src.agent.main

# Fresh run (wipes agent_messages/channels, keeps proposals):
$DC --profile agent run -d --name blackbird-agent-run agent python -m src.agent.main --fresh

# With a time limit (minutes):
$DC --profile agent run -d --name blackbird-agent-run agent python -m src.agent.main --max-runtime 60
```

**`--budget` is deprecated.** It is a *cumulative* cap for the whole run, it is
rebuilt from `llm_call_logs` on restart, and it therefore benches an agent
permanently once crossed — a restart does not clear it. It defaults to 0 (off)
and should stay there. Pacing and runaway protection are now handled by the
sliding-window rate limiter, whose allowance scales with each agent's live
conversational load (`llm_calls_per_load_per_window`, `llm_rate_window_seconds`).
A hub bot in a star topology will hit any uniform cumulative cap long before any
spoke does. See `docs/specs/2026-08-06-hub-budget-scheduler-design.md`.

**Before restarting**, always save logs and rebuild containers:

```bash
DC="docker compose -f docker-compose.prod.yml"

# 1. Stop the old container FIRST — GRACEFULLY — then save logs. `docker rm -f`
#    sends SIGKILL, which skips the shutdown flush and permanently loses the
#    in-flight turn's messages (the DB, not Slack, is the durable store).
#
#    -t 420, NOT -t 30 (and not -t 180 or -t 300 either, as of the
#    thread_reply max_tokens raise below). Measured 2026-08-19: a single
#    `thread_reply` turn runs up to 134s (up to max_tool_rounds real API
#    calls, each consult 25-40s), and `request_stop()` is cooperative — it
#    flips a flag, and the flush in src/agent/main.py's finally-block only
#    runs once the main loop RETURNS. At -t 30 and even -t 90, SIGKILL landed
#    mid-turn and the flush never ran; 6 buffered llm_call_logs rows were
#    lost. `generate_with_tools` now polls the flag and stops opening new
#    tool rounds (`should_continue`), which bounds a stopping turn to the
#    round already underway plus one final call — but the grace period must
#    still exceed that.
#
#    180 -> 420 for the 2026-08-21 thread_reply max_tokens raise (4000 ->
#    16000, src/agent/simulation.py): a single 16000-token final call can run
#    ~4-5 minutes at Opus output rates, and it is the round `should_continue`
#    already lets finish — so it cannot be interrupted, only awaited out.
#    Total turn time is not much worse (this one call replaces what used to be
#    a call-then-retry pair), but it now lands in a single uninterruptible
#    call instead of two shorter ones, so the grace period has to cover that
#    call outright. Sized generously rather than to the minute: `docker stop`
#    returns as soon as the container actually exits, so a larger `-t` costs
#    nothing on the common path and is free insurance against the tail.
#
#    Verify it worked: exit code 0, and "Simulation stopping..." in the logs.
#    Exit 137 means SIGKILL and a lost flush, NOT necessarily an OOM.
docker stop -t 420 blackbird-agent-run
docker inspect blackbird-agent-run --format 'exit={{.State.ExitCode}}'

# 2. Save logs — AFTER the stop, so the shutdown lines are captured.
docker logs blackbird-agent-run > logs/blackbird_run_$(date +%s).log 2>&1
ls -t logs/blackbird_run_*.log | tail -n +11 | xargs -r rm -f
docker rm blackbird-agent-run

# 3. Rebuild the web tier AND the agent image (both bake src/ into the image)
$DC up -d --build blackbird-app worker
$DC --profile agent build agent

# 4. Apply migrations — NOTHING ELSE DOES. See the warning below.
$DC exec -T blackbird-app alembic upgrade head
$DC exec -T blackbird-app alembic current   # confirm it matches `alembic heads`

# 5. Start the new run
$DC --profile agent run -d --name blackbird-agent-run agent python -m src.agent.main
```

> ### ⚠️ Nothing migrates the database for you. Step 4 is not optional.
>
> The prod web command is a bare `uvicorn` (`docker-compose.prod.yml`), there is
> no `alembic upgrade` at startup, and no `create_all` anywhere in `src/main.py`
> or `src/database.py`. So a rebuild + restart runs the **new code against the
> old schema**, and the failure is silent rather than loud: writes that hit a
> missing table or column raise, get swallowed by a best-effort `except`, and
> leave one ERROR line in a log nobody is tailing.
>
> Measured 2026-08-06: production sat at `0024` while the branch head was `0025`.
> Restarting without step 4 would have made every `_persist_assessment` fail on a
> missing `opportunity_assessments` and lost **every** screening verdict, while
> Slack posts continued to look completely normal.
>
> Check before you start a run, not after:
>
> ```bash
> $DC exec -T postgres psql -U copi -d copi -t -A -c \
>   "SELECT version_num FROM alembic_version;"   # must equal `alembic heads`
> ```
>
> `scripts/migrate/run_migration.sh` is the guarded path for a populated
> database (preflight → apply → postflight), but it shells out to a bare
> `docker compose` and defaults `SVC=app`, neither of which matches this stack.
> Override both: `COMPOSE_FILE=docker-compose.prod.yml
> MIGRATE_SERVICE=blackbird-app ./scripts/migrate/run_migration.sh --apply`.

> ### ⚠️ The agent image does NOT mount `src/`. Rebuild it, or you deploy stale code.
>
> The `agent` service in `docker-compose.prod.yml` mounts only `./profiles`,
> `./prompts` and `./data`. **`src/` is baked into the image at build time.** So
> `$DC up -d --build blackbird-app worker` does *not* update the simulation — it
> rebuilds the web tier only, and `docker compose run agent` then starts the
> **previous** image. Measured 2026-08-06: a rebuild that skipped step 3's second
> line launched a run on hours-old code, silently, with no error — the startup
> banner (`Budget: N calls/agent`) was the only tell.
>
> After any `src/` change, always run `$DC --profile agent build agent` before
> starting a new run, and check the startup banner matches what you expect.

**Note:** The agent-run container loads Python modules only at startup, so **code**
changes require rebuilding the image (above) and restarting the container. **After any code change that affects the running agent process, flag this to the user so they can decide whether to restart.** (Roster changes — activating/inactivating agents or setting a new `slack_bot_token` in `AgentRegistry` — do NOT need a restart; they're picked up live by `_sync_roster_from_db`.)

**`.env` changes need a container *recreate*, not a restart.** `env_file` is
resolved when the container is created, so `docker restart` re-runs the old
environment. Step 2 + step 4 above (rm, then `run`) is what actually picks up an
edited `.env`.

## Adding New PIs

**The `AgentRegistry` table is the single source of truth for the agent roster.**
There is no longer a `PILOT_LABS` list and no per-agent `config.py` token fields to
edit. A running `agent-run` re-syncs the roster from the DB every ~30s
(`_sync_roster_from_db`), so flipping an agent to `status='active'` (with a token on
its row) makes it go live **without a restart**.

### 1. Create user records and generate profiles

Look up each PI's ORCID ID (search orcid.org or the ORCID public API). Add them to `orcids.txt` with a comment line, then seed:

```bash
docker compose -f docker-compose.prod.yml exec blackbird-app python -m src.cli seed-profiles --file new_orcids.txt
```

This creates `User` rows and enqueues profile generation jobs (processed by the worker).

### 2. Create agent registry entries

Each agent needs an `AgentRegistry` row with a unique `agent_id` (lowercase last name)
and `bot_name` (`{LastName}Bot`), created `status='pending'`. Self-service signups
(`src/routers/agent_page.py`) and the backfill scripts both create these automatically.

**Last-name collisions:** If a last name is already taken (e.g., Chunlei Wu = `wu`), prefix with the first initial (e.g., Peng Wu = `pwu` / `PWuBot`). The web UI applies this logic automatically.

### 3. Provision the Slack bot + activate (admin UI)

Go to **/admin/agents → the pending agent → Provision**. This creates the Slack app
via the Manifest API and sends you to Slack's install screen; on approval you return to
the page with the **bot token filled in and saved to `AgentRegistry.slack_bot_token`**.
Click **Approve & Activate** to set `status='active'`. The running simulation picks the
agent up on its next roster sync — no `.env` edit, no `config.py` edit, no restart.

Requires `SLACK_CONFIG_TOKEN` / `SLACK_CONFIG_REFRESH_TOKEN` in the environment (the
rotating pair is persisted in the `app_settings` KV table) and a public `base_url`.

**Bulk provisioning** (many agents at once) still uses the host script. First export the
roster from the container, then run the script on the host:

```bash
docker compose -f docker-compose.prod.yml exec blackbird-app python scripts/export_agent_roster.py   # writes data/agent_roster.json
python3 scripts/provision_slack_bots.py                               # host: creates apps, prints OAuth URLs
```

The host script writes tokens to `.env`; import them into the DB column with:

```bash
docker compose -f docker-compose.prod.yml exec blackbird-app python scripts/backfill_agent_tokens.py
```

(`.env` + `config.py get_slack_tokens()` remain a read fallback, but the DB column is
authoritative.)

## Account Types (PI / manager / admin)

**`users.user_role` is the single source of truth**, with values `pi`, `manager`,
`admin`. `User.is_admin` is no longer a mapped column — it is a read-only
`hybrid_property` over `user_role`, so it still works in both SQL
(`select(User.is_admin)`) and Python, but **cannot be assigned**. Set the role
instead. The physical `users.is_admin` column stays in the database, unmapped and
defaulted. Dropping it is deferred to a separate later migration (`0031`+ — `0030` is
now taken by `specialist_consults`), which **has not been written, let alone applied** —
see the design doc's §8.

> **Deploy order for `0028_add_user_role` — migrate BEFORE the new code serves.**
> `0028` is additive and gives `is_admin` a server default, so *old code against the
> new schema* is safe: the running container keeps reading and inserting users. The
> reverse is not. The new code **maps `users.user_role`**, so it is named in the SELECT
> list of every `select(User)`, and against a pre-`0028` database each one raises
> `UndefinedColumn` — login included, for the whole gap. `up -d --build` builds and
> starts in one step, which is exactly that broken direction, and you cannot `exec`
> alembic in the *old* container because `0028` is only in the new image. Build, then
> migrate from a one-off container off that image, then start:
>
>     DC="docker compose -f docker-compose.prod.yml"
>     $DC build blackbird-app worker
>     $DC run --rm blackbird-app alembic upgrade head
>     $DC run --rm blackbird-app alembic current      # must equal `alembic heads`
>     $DC up -d blackbird-app worker

- **PI** — the original account: own profile, own lab agent, `/profile` and `/agent`.
- **Manager** — global, read-mostly: `/manager/pis`, `/manager/assessments`,
  `/manager/discussions`, `/manager/activity`. A scoped, deliberate reversal of the
  original all-GET guarantee (design D1) adds exactly four write routes — `POST
  /manager/pis` (create a PI via ORCID), `/manager/pis/{id}/profile` (edit a PI's
  profile fields), and `/manager/pis/{id}/mute` / `/unmute` (toggle a PI's agent) —
  and nothing else; `tests/integration/test_manager_views.py`'s
  `test_manager_router_mutations_are_an_explicit_allowlist` fails loudly on a fifth.
  **Still cannot impersonate**, set roles, or provision Slack bots (all three stay
  admin-only), and there is deliberately no LLM-call drill-down and no export.
  Managers *do* see private (`collab_private`) discussion threads — a policy
  decision, recorded in the design doc.
- **Admin** — everything, including `/admin/*` and impersonation.

`is_manager` means exactly `user_role == 'manager'`. The "may see the manager views"
predicate is **`is_staff`** (admin OR manager). **Never widen `is_admin`** — impersonation
(`src/dependencies.py`, and a duplicate check in `src/main.py`) is gated on it and
returns a fully substituted user, so a manager satisfying `is_admin` would be a full
privilege escalation.

**Exclude `manager`, never "non-PI".** An admin is not a `pi` either, and admins keep
the PI surfaces (`base.html` still offers them My Profile / My Agent), so a `!= 'pi'`
guard locks admins out of their own account — `/profile` bounces an admin whose
onboarding is incomplete to `/onboarding`, and only `POST /onboarding/save-profile` can
ever clear that flag. The PI-write POSTs (`/onboarding/save-profile`,
`/onboarding/retry`, `/profile/refresh`, `/agent/request`) are gated on
**`get_pi_user`** in `src/dependencies.py`, which 403s a manager and lets an admin
through. A read-only redirect is not enough there: `save-profile` writes
`onboarding_complete` and creates the profile, which is the whole gate on
`/agent/request` — so an ungated pair is a manager with a lab bot.

Appoint from **/admin/users/{id} → Account Type**. The last admin cannot be demoted
there (that guard counts only admins with `access_status='allowed'` — a denied admin
cannot log in, so counting one would just make demotion easier). If no admin can log in at all, recover from a container shell:

    docker compose -f docker-compose.prod.yml exec blackbird-app \
      python -m src.cli role:set --orcid 0000-0000-0000-0000 --role admin

New managers are provisioned in two steps: they sign in with ORCID (landing on
`/access-pending`), an admin approves them at `/admin/access-requests`, then sets their
role. Between approval and role-setting the account behaves as a PI.

## BlackbirdBot (the scout_hub role)

BlackbirdBot screens PI ideas against `data/Blackbird_initial_priorities-criteria_v1.pdf`.
**The rubric criteria live in one document, `prompts/rubric/blackbird-rubric.toml`** —
weights, band thresholds, the 1–5 scale, gating criteria, checklist, red flags, the
heuristic. `src/services/blackbird_rubric.py` loads it once at import (fail-fast on an
invalid document) and renders it into the `{rubric}` placeholder of
`prompts/roles/scout_hub/agent-system.md` at prompt-composition time, so the prompt the
hub reads and the score the code computes cannot drift apart. The `<assessment_json>`
skeleton stays in `prompts/roles/scout_hub/phase4-thread-reply.md` (it is the
authoritative contract for the sidecar's shape);
`tests/unit/test_rubric_prompt_sync.py` is the drift alarm between the two, plus
`specialists.py`'s `maps_to_dimension`. The per-phase behaviour otherwise lives in
`prompts/roles/scout_hub/` and `src/agent/thread_guidance.py`.

**Editing the rubric takes effect on restart, not on rebuild.** `prompts/` is
bind-mounted into exactly the two services that read it, `blackbird-app` and `agent`, so
a document edit needs no image build (`worker` mounts only `./profiles` — it never
imports the rubric). But the document is read ONCE at import, so a running process keeps
the rubric it started with. Stop the run, start it again (see "Before restarting" above), and
check the startup banner: it logs `Screening rubric: version X (content hash Y)`. X must
match `[meta].version` in the file; Y is the first 12 hex characters of the file's sha256
(not the full digest). New assessments are stamped with both, so pre-/post-change rows
stay comparable.

> **Deploy order for `0030_specialist_consults_rubric_version` — migrate BEFORE the new
> code serves.** `0030` is additive (a new `specialist_consults` table, plus nullable
> `rubric_version`/`rubric_content_hash` columns on `opportunity_assessments`), so *old
> code against the new schema* is safe. The reverse is not: the new code **maps
> `opportunity_assessments.rubric_version`/`.rubric_content_hash`**, so EVERY
> `select(OpportunityAssessment)` — the assessments pages, the detail pages — and the
> discussions pages' `specialist_consults` query all raise
> `UndefinedColumn`/`UndefinedTable` against a pre-`0030` database. Build, migrate from a
> one-off container, then start — same ordering as `0028`:
>
>     DC="docker compose -f docker-compose.prod.yml"
>     $DC build blackbird-app worker
>     $DC run --rm blackbird-app alembic upgrade head
>     $DC run --rm blackbird-app alembic current      # must equal `alembic heads`
>     $DC up -d blackbird-app worker
>
> The agent image bakes `src/` in too and must be rebuilt separately
> (`$DC --profile agent build agent`) — see the "agent image does NOT mount `src/`"
> warning above. One caveat beyond the usual migrate-before-serve reasoning: an interview
> already in flight across the deploy has no `specialist_consults` rows yet (they only
> start being written once the new code is running), so a verdict that concludes without
> a fresh consult can be stamped `panel_incomplete` with a full `missing_domains` list — a
> false accusation, but only in that one-time window.

> **`0031_normalize_missing_domains_null` needs NO deploy ordering.** It is
> data-only — it rewrites `opportunity_assessments.missing_domains` from the JSONB
> scalar `null` to a real SQL NULL, and changes no DDL. The code side is
> `JSONB(none_as_null=True)` on the mapped column, which is a Python-side
> property, so old code against normalized data and new code against
> un-normalized data both read `None` exactly as before. Apply it in any order
> relative to the restart. Details:
> `docs/audits/2026-08-20-assessment-duplication/README.md`.

**One interview yields exactly one assessment, and it comes from the reply that
ENDS the interview.** `_capture_hub_assessment` stores a sidecar carried by a reply
that concludes (ordinal 12) **or closes the thread** — the ⏸️ decline, decided by
`_reply_closes_thread`, hoisted once in `_reply_to_thread` and passed down as
`closes_thread` so the capture gate and `_check_thread_outcome` cannot disagree. It
refuses one from a turn that does neither (`premature_sidecar`), and refuses a
re-capture of a turn already stored or anything following a closing verdict
(`duplicate_thread_verdict`) — recording every refusal in `assessment_drops` rather
than logging it and moving on. **Gating on the ordinal alone destroys data:** every
`pass` verdict is delivered as a ⏸️ decline, so its own reply closes the thread
before ordinal 12 and no later turn exists to record it (run 076e80b6: 4 of 5
`premature_sidecar` refusals were the thread's terminal message; all 23 `pass`
sidecars ever emitted carried ⏸️). Gating on neither wrote three rows for a single
pearce interview (ordinals 8, 10 and 12), because the `<assessment_json>` contract
sits in the STATIC body of `phase4-thread-reply.md` and is therefore in front of the
model on every phase-4 turn, not just the one asked for it. A later concluding or
closing verdict **supersedes** an earlier provisional one — last write wins, and
`_retire_superseded_verdict` removes the earlier row (leaving a
`duplicate_thread_verdict` drop as its trace) so the one-row invariant still holds.
When writing a
test that drives a concluding reply, seed the thread's history in the
`MessageLog`: `_reply_to_thread` overwrites `ThreadState.message_count` from
`get_thread_history`, so `message_count=11` over an empty log is an ordinal-1
EXPLORE turn, not the CONCLUDE turn it looks like.

As of the 2026-08-12 removal cycle (private instructions + reply-only hub), there is no
runtime "private profile" mechanism — `Agent._compose_system_prompt` injects the rendered
rubric but no `## Your Private Instructions` header, and nothing reads
`profiles/private/{agent_id}.md` per-agent. The stale hub copy has now been diffed against
the extracted rubric and archived as
`profiles/private/blackbird.archived-2026-08-20.md` (untracked, git-ignored, unread — no
longer a per-deploy chore) — the diff is recorded in
`docs/audits/2026-08-20-rubric-extraction/blackbird-private-diff.md`. Its one substantive
delta was a fourth **`baltimore_commitment`** gating criterion, deliberately absent from
the tracked rubric: the three gating keys are structural (the sidecar's JSON keys and the
`opportunity_assessments.gating` keys), and `blackbird_rubric.py`'s validator rejects a
fourth outright.

- **Interview guidance is per-role Python**, not a prompt: `src/agent/thread_guidance.py`.
  The `pi_lab` strings there are byte-identical to the pre-refactor literals and are pinned
  by `tests/characterization/__snapshots__/test_agent_turn_gm.ambr` — do not reword them,
  and never run `pytest --snapshot-update` to make a mismatch go away.
- **Inside an interview thread the hub is reply-only — it never makes a top-level post
  there.** An Opportunity Assessment is not a post type: it is an `<assessment_json>`
  sidecar carried inside the hub's CONCLUDING reply in the interview thread (bare JSON, *no*
  ``` fence). It is stripped from the Slack body before anything is posted and written to
  `opportunity_assessments`, visible at `/admin/assessments`. To the MODEL, `:mag:` names
  the sidecar and is never a post label it may write
  (`prompts/roles/scout_hub/agent-system.md`); the full verdict — rationale, red flags,
  gating, `raw_verdict` — never appears on anything a PI or another lab sees.
  As of the 2026-08-21 manager-PI-controls cycle
  (`SimulationEngine._post_assessment_summary`, `src/agent/simulation.py`), every HELD
  verdict — pass or fail alike — does additionally trigger one genuinely top-level post,
  written by the ENGINE rather than the model and prefixed with that same `:mag:`: a
  headline-only line (PI/lab name, `company_or_project`, `recommendation`, band/score, and a
  permalink or `(link unavailable)`) to `#assessments-summary`
  (`ASSESSMENTS_SUMMARY_CHANNEL`, `src/agent/channels.py`) — deliberately with **no**
  rationale, red flags, gating, or `raw_verdict` (design D12). Band/score are omitted
  entirely when the verdict carried no dimension scores, for the same reason
  `_persist_assessment` leaves those columns NULL: an empty `scores` map is "we don't know",
  and `weighted_score({})` is a 0.00 that bands as a decline nobody made. That channel is
  human-joinable/workspace-visible ("public" in the Slack sense — design D11) but is never
  added to `SEEDED_CHANNELS` or any per-agent subscription, so no PI-lab bot is ever joined
  to it or polls it — it still never reaches a PI/lab **agent**'s own view of the
  simulation, only human staff who join the channel directly. The post fires synchronously
  right after `_persist_assessment` returns HELD inside `_capture_hub_assessment`; a dropped
  or refused sidecar (an `AssessmentDrop` row, never an `opportunity_assessments` row) never
  posts (design D14), and a Slack failure in the post/permalink step is caught and logged,
  never raised into the calling turn (design D16) — see
  `docs/specs/2026-08-21-manager-pi-controls-design.md`. With `SLACK_ENABLED=false` the
  headline is skipped outright (the hub's transport is a `NullTransport`, which has no
  async post/permalink methods at all) — the assessment row is still written, so nothing is
  lost but the Slack copy.
- **`weighted_score` is computed**, never taken from the model:
  `src/services/blackbird_rubric.py`. `recommendation` (which may be
  `route-to-incubation`) comes straight from the model's verdict and the computed `band`
  comes straight from `weighted_score` — they are separate columns on
  `opportunity_assessments` and neither is derived from the other.
- **`gating` values are the tri-state strings** `"met"` / `"not_met"` / `"unconfirmed"`,
  never booleans — "the PI declined" and "we never asked" are different answers, and only
  the former can license discounting an idea.
- **`search_prior_art` is a TITLE-only search** on the USPTO Open Data Portal (PatentsView
  was decommissioned in its 2026-03-20 migration to api.uspto.gov). It backs off to the
  most specific terms when the full phrase misses — before that backoff existed, every
  production search ANDed in domain-generic words like "inhibitor" and returned zero hits,
  reported to PIs as clean novelty. An empty title search is never FTO.
