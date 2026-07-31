# `tests/e2e` — browser flows (Task 12)

Covers `.notes/full-system-test-plan.md` §"Task 12". Two things live here:

- **`test_browser_flows.py`** — `FLOWS`, a machine-readable transcript of each
  flow (what to open, what to click, what must be visible), plus HTTP replays of
  every flow whose steps are ordinary form posts.
- **`seed.py` / `session.py` / `mint_cookie.py` / `auth_helper.py`** — the
  harness. `seed.py` writes fixture rows to a **live** database; the other three
  exist because ORCID login is broken (below).

Default `pytest tests/` behaviour: only the two offline well-formedness tests
run; the rest skip for want of `E2E_BASE_URL`. Nothing here touches the
production `copi` database — `seed.py` refuses any database not on
`ALLOWED_DATABASES`.

## READ THIS FIRST — `copi_slack_test` must never be dropped

Running the Slack provisioning flow rotates the Slack **app-configuration**
credential pair. Rotation is single-use: the token you type in is dead
afterwards, and the replacement `(slack_config_token, slack_config_refresh_token,
slack_config_token_exp)` triple is written into `app_settings` of whichever
database the request used. That is `copi_slack_test`. **Drop that database and
Slack app-configuration access is gone permanently**, with no way to recover it.
See `.notes/slack-integration-test-plan.md` §"Global Constraints".

## Setup

```bash
# 1. an app instance on a MIGRATED database (the live `copi` DB is at 0018 and
#    has no `agents` table, so /admin/agents cannot work against it)
docker compose run -d --name app-8002 -p 8002:8000 \
  -e DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_slack_test \
  -e BASE_URL=http://localhost:8002 -e ALLOW_HTTP_SESSIONS=true \
  app uvicorn src.main:app --host 0.0.0.0 --port 8000

# 2. a SECOND instance on the SAME database with isolation on. Cohort settings
#    are read once per process, so the banner control needs a second process.
docker compose run -d --name app-8003 -p 8003:8000 \
  -e DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_slack_test \
  -e BASE_URL=http://localhost:8003 -e ALLOW_HTTP_SESSIONS=true \
  -e COHORT_ISOLATION_ENABLED=true -e COHORT_DEFAULT_POLICY=isolated \
  app uvicorn src.main:app --host 0.0.0.0 --port 8000

# 3. seed, and note the printed user ids
docker exec -i app-8002 python -m tests.e2e.seed

# 4. run. Container-to-container hostnames, because pytest runs inside `app`.
docker compose exec -T \
  -e E2E_BASE_URL=http://app-8002:8000 \
  -e E2E_ISOLATION_BASE_URL=http://app-8003:8000 \
  -e E2E_ADMIN_USER_ID=<admin_user_id> \
  -e E2E_SIGNUP_USER_ID=<signup_user_id> \
  -e E2E_ONBOARDING_USER_ID=<onboarding_user_id> \
  app python -m pytest tests/e2e/test_browser_flows.py -q
```

## Authentication: why the cookie is forged

`session.py` forges the signed `copi-session` cookie exactly as
`tests/integration/test_cohort_admin.py::_auth` does —
`itsdangerous.TimestampSigner(settings.secret_key)` over
`base64(json({"user_id": ...}))`.

This is not a convenience. **ORCID login does not work in this deployment**:
`.env` carries `ORCID_CLIENT_ID=test-client-id`, and ORCID's authorize endpoint
answers

```
HTTP 400  {"error":"invalid_request","error_description":"Invalid parameter: client_id"}
```

so there is no consent screen for any browser to click. Worse, ORCID degrades
that 400 into its ordinary sign-in page, so the user sees a plausible ORCID
screen that can never redirect back to `/auth/callback`, and our side logs
nothing at all. `test_orcid_login_cannot_be_driven_and_says_why` xfails on this
so the finding cannot be lost. The fix is configuration (real ORCID OAuth
credentials), not code.

Two gotchas that cost red tests and are pinned in comments:

- Put the forged cookie in the **cookie jar**, never in a per-request `Cookie`
  header. The app re-issues `copi-session` on every response, and httpx then
  sends the jar cookie *plus* the explicit header; Starlette joins the two
  header values with `", "`, fails to parse, and the request arrives
  **unauthenticated**. It only bites on a redirect-follow, i.e. exactly on the
  form posts.
- Never share an `httpx.Client` across tests — it carries the previous
  identity's cookie.

## What needs a human, and why

| flow | automatable? | why |
|---|---|---|
| admin: create cohort + edit topology | yes | ordinary form posts |
| agent self-service signup | yes | ordinary form post |
| public graph | yes | unauthenticated GET |
| onboarding | partly — see below | |
| **Slack provisioning** | **no** | needs a Slack-authenticated browser |
| **ORCID login** | **no** | no valid client_id; no consent screen exists |

### Slack provisioning

Slack's OAuth consent ("Allow") screen requires a browser with a live Slack
session, and Slack offers no headless install grant. The Playwright/MCP browser
has no Slack session, so this one step is irreducibly human. Everything either
side of it is real code under test: the `Provision` POST calls
`apps.manifest.create` for real, and Slack's redirect lands on the app's own
`/admin/agents/slack/callback`, which runs `complete_provisioning`.

Procedure:

1. Arm the app process with the config **refresh** token (env var only — never a
   file, never a command-line argument):
   re-create `app-8002` with `-e SLACK_CONFIG_REFRESH_TOKEN=...`.
2. Serve the admin cookie to the human's browser:

   ```bash
   docker exec -i app-8002 python -m tests.e2e.mint_cookie <admin-user-uuid>
   E2E_SESSION_COOKIE='<that value>' \
   E2E_TARGET_URL='http://localhost:8002/admin/agents/<probe-agent-row-id>' \
   E2E_HELPER_PORT=8099 python3 tests/e2e/auth_helper.py
   ```

   Cookies ignore port, so a cookie set by `localhost:8099` with no `Domain`
   attribute is sent to `localhost:8002` too. Caveat for the human: it is
   host-scoped to `localhost`, so it replaces their session on every localhost
   port.
3. Human opens `http://localhost:8099/`, clicks **Provision**, **verifies the
   workspace name is the test workspace**, clicks **Allow**, and lands on
   `/admin/agents/<id>?slack_ok=1`.
4. Verify from **Slack's** side, not from our column: `auth.test` on the
   resulting `xoxb-` token must return `ok: true` with the test workspace's
   `team_id` and a `bot_id`. A set `slack_bot_token` column proves only that we
   wrote a column.

The agent's `bot_name` **must end in `ProbeBot`** —
`scripts/slack_test_teardown.py` deletes apps by that suffix and refuses to
touch anything else, so a differently-named bot becomes permanent workspace
litter.

> **Record the `app_id` yourself.** `exchange_code` returns only the `xoxb-`
> string and drops the `app_id` from the `oauth.v2.access` response, and
> `complete_provisioning` then deletes the `SlackAppProvision` row that held it.
> Nothing in the data model keeps it. Since config tokens have no list-apps API,
> an app provisioned through the admin UI cannot afterwards be found for deletion
> or audit; `slack_test_teardown.py` enumerates from `PROBE_APPS_JSON` for
> exactly that reason. Recover it with one read-only call —
> `bots.info?bot=<bot_id from auth.test>` returns `app_id` — and write it down.
>
> Provisioned by this task on 2026-07-31: `T12ProbeBot`, `app_id=A0BM5AY6HEW`,
> `bot_user=U0BMZLWQARE`, `bot_id=B0BM78V63UH`, workspace `copi-test`
> (`T0BMVSBMEC8`).

`status='pending'` → `'active'` (the **Approve & Activate** button) is a
*separate* step and is **not** covered here: provisioning writes the token and
leaves the status alone. Covering it needs one more POST to
`/admin/agents/{id}/approve` plus an assertion that a running `agent-run` picks
the agent up on its next `_sync_roster_from_db` (~30s) — which needs an engine
process, so it belongs with Task 13, not here.

### Onboarding

The onboarding *routes* are fully drivable with a session: start → review →
private profile → complete, with `users.onboarding_complete` flipping only on
the final POST.

It stops on its own at **Step 3 of 4, "Building Your Profile"**. `/onboarding`
auto-enqueues a `generate_profile` job and shows that spinner while
`job_status` is `none`/`pending`/`processing`. Advancing needs the worker to run
`run_profile_pipeline`, which fetches the user's ORCID record — so without
usable ORCID credentials it can never complete, and the page spins forever. The
flow therefore substitutes the `ResearcherProfile` row the pipeline would have
written and continues from there; the pipeline itself is Task 4's subject.

## Artefacts

Screenshots and accessibility snapshots from the driven runs land in
`.playwright-mcp/` (gitignored), prefixed `t12-`.
