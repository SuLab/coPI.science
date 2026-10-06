# PIs, accounts and access

Reference detail behind the rules in the root `CLAUDE.md`, which keeps only what
every session needs. Dated statements hold as of the date they give; re-measure a
count or a line number before relying on it.

## Adding New PIs

**The `AgentRegistry` table is the single source of truth for the agent roster.**
There is no longer a `PILOT_LABS` list and no per-agent `config.py` token fields to
edit. A running simulation engine re-syncs the roster from the DB every ~30s
(`_sync_roster_from_db`), so flipping an agent to `status='active'` (with a token on
its row) makes it go live **without a restart**.

**The one-step path (2026-08-24): the manager PIs tab.** `POST /manager/pis`
(the "Add a PI by ORCID iD" form on `/manager/pis`) now does, in ONE commit:
create the User, enqueue the `generate_profile` job, mint a **pending**
`AgentRegistry` row (inert — the roster sync loads only `status='active'`),
and record the ORCID-employment-derived JHU tenure start. The atomicity is
deliberate: the job and the agent row commit together, so the worker can never
run the pipeline before the row exists — the old seed-then-create-row order
lost the markdown export and revision every time. The profile job runs the **corpus
pipeline** (`src/services/corpus.py`: ORCID + OpenAlex + PubMed-by-ORCID +
name-affiliation search, identity-gated, consortium-excluded, year-ranked; every ORCID-anchored find (ORCID works or PubMed's ORCID field) is stored, uncapped, while finds from OpenAlex or the name+affiliation search alone wait as candidates for staff review) and the synthesis/export are tenure-filtered
(`src/services/jhu_rules.py`; per-user `app_settings` keys, with the legacy
agent_id map still read as fallback — its 62 curated entries were migrated in
2026-08). A wrong or missing tenure year is correctable on the manager
Edit Profile form ("JHU tenure start"). A corpus-stage failure FAILS the job
(retry ×3, waiting 4 then 16 minutes between attempts → dead, visible on /admin/jobs, the PI detail page and, with a Try Again button, the PI's onboarding page) instead of
storing a thin ORCID-only profile. A tenure year derived from papers after an ORCID failure or with an incomplete corpus is kept provisionally (app_settings key `jhu_tenure_provisional:{user_id}`), so later profile edits export with the same scoping as the pipeline; the next healthy run replaces or deletes it. A search that hit its page cap counts as incomplete the same way, and only ORCID-anchored papers date tenure. A tenure-year change on the manager form regenerates the profile (`generate_profile`), whose step 10 refreshes grants, industry evidence and companies. A synthesis that fails, is refused or returns something unusable fails the job (retry ×3, then dead) and leaves the stored profile alone; a PI whose name is an ORCID iD is refused at once (fix the name, then Regenerate). Every enqueue goes through `src/services/profile_jobs.py`, so a double click or Add-PI followed by an approval runs one pipeline, not two. **Activation is gated**: `admin_approve_agent`
refuses to flip a `pi_lab` agent to `active` — through the approve button OR
the status dropdown — when its profile is missing, ungrounded or has no Research Summary, or its newest
generation job is dead, unless the logged "activate anyway" override is
checked; and, override or not, when the persona file `profiles/public/<slug>.md` is missing or has no Research Summary, or the owner's role may not use the PI surfaces (`src/services/agent_activation.py`, `persona_blockers`). Every activation path applies the same gate (admin approve and status dropdown, manager Activate, manager unmute, an active agent's role change). The CLI `seed-profiles` path
below still works but creates NO agent row and derives NO tenure entry.

**Paper review, drafts and Regenerate** (manager PI page, staff only; spec 2026-10-05 §6.3).
*Candidate papers* lists finds without an ORCID anchor: Accept stores the paper (fetched
from PubMed) as `manual`, Reject keeps it out for good. *Unanchored papers* lists stored
rows the resolver no longer returns with an anchor (`provenance = 'unanchored'`): Keep marks
them `manual`, Exclude hides them from synthesis, the persona, RePORTER linking, discovery
and industry scans (the row is kept; excluded rows are listed collapsed, with a staff Restore).
A regeneration of a profile a person edited since
its last generation (`researcher_profiles.human_edited_at`) does not overwrite it: the
synthesis waits as a *Pending draft* with a field diff; Accept writes it (refused if the
profile changed since, or while a generation runs), Discard drops it. *Regenerate* queues
a generation for any profile (not while one runs, nor within an hour of the last one). When
the post-commit persona write fails (`agents.persona_export_failed_at`) or the file differs
from a fresh render, the page says "Persona file out of date" and offers Re-export. Stored
rows' title, abstract, journal, year and PMCID follow PubMed on every run, except `manual`
rows. The export links a DOI only when it matches PubMed's record for the PMID
(`publications.doi_verified`), else the PubMed page.

**The manager's whole path, 2026-10-05.** On `/manager/pis/{id}` a manager then
presses **Install Slack bot** (Slack app + OAuth, token saved on the row) and
**Activate agent**. Activation also wires the lab into its `hub-{slug}` star spoke
(`src/services/star_topology.py::ensure_lab_spoke`), in the same commit as the status
flip; every other activation path (admin approve, the admin role change, unmute)
does the same. Before this, nothing but the admin ensure-spoke buttons and
`scripts/ensure_star_spokes.py` created spokes, so under production's
`COHORT_ISOLATION_ENABLED=true` / `COHORT_DEFAULT_POLICY=isolated` a
manager-activated lab was isolated mid-run and the next run start failed
`_validate_star_topology`. With isolation on, an activation whose spoke cannot be
ensured (two active scout_hubs, a lab-to-lab membership, a concurrent write of the
same spoke) is refused; the spoke goes to the active hub, so a parked second hub
row (D14 allows one) does not count, and a roster with no non-suspended scout_hub
is logged and allowed (no run can start then anyway). A dead
generation job or an ungrounded profile shows **Retry profile generation** on the
same page (manager route above; no admin impersonation needed). An ORCID already
held by a PI account with no agent — someone who signed in before being added — is
adopted by Add-PI (agent minted, profile job enqueued if there is no profile)
instead of "already exists"; any other holder still gets "already exists".
Agent slugs and bot names are ASCII letters and digits only
(`src/services/agent_identity.py`): accents are folded (`Müller` → `muller` /
`MullerBot`), punctuation dropped (`O'Brien` → `OBrienBot`), a trailing
`Jr.`/`III`/`PhD` skipped, and a name with no usable surname (non-Latin script, or
the ORCID iD an ORCID record with a private name yields) falls back to `pi` + the
iD's last four digits. Slack restricts bot display names to ASCII, and the
`[a-z0-9_-]` slug checks in `src/agent/tools.py`, `user_deletion` and
`pi_companies` refused the accented slugs the old derivation produced.

**Editing a profile and the persona file (2026-10-06).** Every profile form posts the profile's version; a post without one over an existing profile is refused ("reload the page"), so a form opened before the first generation finished can no longer blank it. A field the form does not carry is left alone. A human edit is checked only where it changes something: a name must pass the D60 allowlist (letters of any script, spaces, `. , - ' ’`, at most 100 characters) and is copied to `agents.pi_name` (a running engine picks it up at the next roster poll; the Slack app's display name is not renamed); the summary is limited to 350 words and 20,000 characters (including a single whitespace-free token), each tag list to 30 items of at most 200 characters with no line break or leading `#` (`src/services/profile_limits.py`). Saving the same edit twice shows success. The manager's Edit Profile form appears once a profile exists. Creating, requesting, linking, renaming and activating an agent publish its persona after the commit (revision mechanism `lifecycle_export`); a file already at a new or relinked agent's slug is moved to `profiles/private/orphaned/`. `scripts/persona_file_audit.py --check` lists agents whose owner has a profile but whose file is missing or summary-less, and persona files with no agent row; `--archive-orphans --apply` and `--reexport-missing --apply` repair them. The manager PI page lists the persona's revisions (staff only) and labels a provisional tenure year.

The `generate_profile` job now enqueues two follow-on jobs of its own,
`enrich_grants` and `industry_evidence`, which run on the worker independently
of the corpus pipeline and never block or fail the profile it followed. The
first resolves the PI's NIH RePORTER identity: only candidates whose name on the
award matches the PI's are considered, and a candidate is accepted when one of its
own awards links to a PMID stored for the PI. The outcome is a status in
`pi_grant_identity` (`resolved`, `held`, `unconfirmed`, `no_match`, `firehose`, or
the staff-set `pinned` / `none_confirmed`); only `resolved` and `pinned` render
RePORTER grants. The job writes `pi_grants` and `pi_grant_identity`, refreshes the
PI's ORCID fundings into `pi_orcid_fundings`, and re-exports
`profiles/public/{agent_id}.md` after its commit when the rendered persona changed.
The persona's `## Active Grants` and `## Past Grants (since <year>)` sections are
derived at export time from those two tables (`src/services/grant_sections.py`);
`researcher_profiles.grant_titles` is retired (no readers or writers). A daily
worker sweep, gated by the app setting `persona_sweep_enabled`, re-exports every
persona whose file differs from a fresh render, so an award that has ended leaves
the Active section without an unrelated edit.
The second scores industry interest from OpenAlex, PubMed competing-interest, USPTO and
ClinicalTrials.gov evidence and writes nothing a prompt or a profile export reads
(`tests/unit/test_enrichment_isolation.py` walks the imports). A competing-interest
statement credits a company to the PI only when the run of named persons nearest before
it names the PI, or says "all/each author(s)" (`src/services/coi_attribution.py`). USPTO
and ClinicalTrials.gov are paged; each score row records what every source covered
(`ok`, `truncated` or `unavailable:<reason>`), and the manager pages show "partial" when a
source is not `ok` (its stored rows are kept, and scored only from the tenure start on).
Evidence rows are upserted, so a row keeps its id and its veto across runs. The
percentile and the reason (`no_tenure_start`, `no_evidence`, `cohort_too_small`, `ok`) are
computed when the page renders, from each PI's latest row of the current
`SCORER_VERSION`; a PI with only older rows shows "rescoring". The grants panel and the
industry-interest score are manager-only surfaces on `/manager/pis/{id}`: each RePORTER
grant, ORCID funding and piece of industry evidence is individually vetoable ("not this
PI's"), and staff can pin the PI's RePORTER profile or confirm the PI has none, via the
routes below. OpenAlex is keyless here: 1000 credits/day per IP (`x-ratelimit-limit`),
reset at 00:00 UTC; a spent budget makes OpenAlex unavailable for that run while the other
sources still refresh. Re-run every PI through `scripts/_bulk_enqueue.py`: the worker
reads OpenAlex's free meter (a singleton lookup costs nothing) before each BULK job that
spends credits and defers it to 00:00 UTC once its estimate exceeds the available free
credits. Charged requests and retries check the meter too, because the uncapped corpus
can exceed a job's admission estimate. The owner removed the shared-budget reservation:
Blackbird may use the whole free daily budget, never prepaid credits
(`src/services/openalex_budget.py`; an unreadable meter falls back to one job per fixed
slot). `--fixed-schedule` staggers the jobs instead. `scripts/enqueue_enrichment.py` backfills both jobs for PIs who
predate this feature — it previews by default, needs `--apply` to enqueue for real, and
takes `--only grants` / `--only industry` and `--orcid` to scope a run.

Inline corpus audit/repair and tenure-rederivation scripts require a readable free
meter and report deferred work as incomplete; they do not persist worker jobs for a
later slot. The final corpus verifier also fails if any live resolve fails: newly
indexed unstored papers are warnings, but unavailable coverage is not a passing check.

Company discovery (`company_discovery`) runs after every successful profile generation (a
request while one runs flags a rerun) and on Find companies. Its Claude extraction of
founder claims from the PI's own competing-interest statements is capped at
`COMPANY_DISCOVERY_DAILY_USD_LIMIT` dollars per rolling 24 h ($20 by default), counted in
`company_discovery_usage`; at the ceiling the job waits, without using up an attempt, until
the budget frees, and the Companies card says until when. `company_discovery_coi_ledger`
keeps each statement's outcome per way of naming the PI, so a statement is paid for once
and its claims come back on later runs. Delete on a confirmed company records it as
rejected, so discovery never suggests it again; adding the same name by hand later revives
it as a confirmed manual entry.

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
roster and mint a config access token from the container (the single-use refresh token
lives only in `app_settings`, so the host never sees it), then run the script on the host:

```bash
docker compose -f docker-compose.prod.yml run --rm --no-deps -T -v "$PWD/data:/app/data" blackbird-app python scripts/export_agent_roster.py   # writes host data/agent_roster.json
docker compose -f docker-compose.prod.yml run --rm --no-deps -T --user "$(id -u):$(id -g)" -v "$PWD/data:/app/data" blackbird-app python scripts/slack_config_token.py   # writes data/.slack_config_token (0600)
python3 scripts/provision_slack_bots.py                               # host: creates apps, prints OAuth URLs; removes the token file
```

The web service has no `./data` mount, so the one-off binds one. The host script
refuses a roster older than 1 h unless given `--allow-stale-roster`: re-export
rather than provision from a stale roster.

The host script writes tokens to `.env` as `SLACK_BOT_TOKEN_<AGENT_ID>`. The engine
reads that key as a fallback only for an agent id that `Settings` declares as a
`slack_bot_token_<id>` field (the legacy map in `config.py get_slack_tokens()`), and
only once the agent service is recreated (`$DC up -d --force-recreate agent`, only with
no live run: a long-running container's environment dates from its creation).
`Settings` ignores undeclared keys, so for any NEW agent the `.env` line does nothing:
paste the token into the agent's approval form on `/admin/agents/<id>`, which makes it
authoritative in `AgentRegistry.slack_bot_token`. (The one-off importer script, which
read `os.environ` directly, was retired 2026-09-29.)

(`.env` + `config.py get_slack_tokens()` remain a read fallback, but the DB column is
authoritative.)

## The Origin guard (every non-GET request, added 2026-08-22)

`OriginGuardMiddleware` (`src/main.py:103`) refuses any request whose method is
not GET/HEAD/OPTIONS unless it proves it came from our own origin. It is the
outermost middleware that can refuse a request — a forged POST is refused before
the session is even opened; only `SecurityHeadersMiddleware`, which refuses
nothing and adds the response headers, sits outside it — and that position is
pinned structurally by
`tests/integration/test_origin_guard.py::test_the_guard_is_the_outermost_refusing_middleware`.
Exactly one path is exempt: `POST /api/csp-report` (browsers send CSP reports
without a usable `Origin`); the route admits only report content types, caps the
body, and rate-limits its log lines (`src/routers/csp_report.py`).
It exists because `same_site="lax"` was the only defence and is void here: one
nginx serves `blackbird.copi.science`, `copi.science` and `devel.copi.science`,
SameSite is computed on the registrable domain, so a page on either sibling
could auto-submit `POST /profile/delete-account` (cascades nine tables) or,
against a signed-in admin, `POST /admin/users/{id}/role`.

Three operator consequences, in order of how much they will cost you:

1. ⚠️ **A wrong or missing `BASE_URL` fails the site CLOSED, site-wide.** The
   expected origin is `normalized_origin(settings.base_url)`, and when that is
   `None` the guard sets `allowed = False` unconditionally rather than comparing
   equal to everything (`src/main.py:154-157`). Every login POST, every form,
   every admin action 403s with `Cross-site request refused.` while GETs keep
   rendering normally — so the site looks up. Production is
   `BASE_URL=https://blackbird.copi.science` (`.env:31`); the *default* is
   `http://localhost:8000` (`src/config.py:140`), which is a perfectly valid
   origin and will therefore silently refuse everything in production. Ports are
   normalised (`https://host:443` == `https://host`) and a trailing slash is
   tolerated, so those are not the failure mode; a scheme/host mismatch is.
2. **`curl -X POST` against the app now needs `-H "Origin: $BASE_URL"`.** Any
   script, health check or one-off `curl` that POSTs will 403 without it.
   `Sec-Fetch-Site: same-origin` works as an alternative (it is a forbidden
   header name, so a browser will not let a page forge one). There is no
   exemption: the one-click unsubscribe route that had one was retired with PI
   notification email (R-02, 2026-09-29).
3. **`/docs`, `/redoc` and `/openapi.json` now 404**, not 401 — `create_app`
   passes `docs_url=None, redoc_url=None, openapi_url=None`, unregistering the
   routes. They were publishing the whole route inventory to anonymous callers.
   `application.openapi()` still builds the schema in-process, which is what
   `tests/unit/test_reachability.py`'s route walk needs.

Every refusal logs one WARNING naming the method, path, received origin,
`Sec-Fetch-Site` and the expected origin — grep for `Refused cross-site` first
when a form stops working after a deploy.

## Agent roles

An `AgentRegistry.role` (table `agents`) must have an entry in
`src/agent/role_capabilities.py`; an agent with an unknown role, or a role whose `role.toml`
fails strict validation, is skipped by the engine (it used to run as pi_lab). A running
agent reassigned to such a role is removed from the live roster at the next roster poll.
`scripts/migrate/preflight.py` BLOCKs on such a role (`check_agent_roles`).

## Account Types (PI / manager / admin / reviewer)

**`users.user_role` is the single source of truth**, with values `pi`, `manager`,
`admin`, `reviewer`. `User.is_admin` is no longer a mapped column — it is a read-only
`hybrid_property` over `user_role`, so it still works in both SQL
(`select(User.is_admin)`) and Python, but **cannot be assigned**. Set the role
instead. The physical `users.is_admin` column stays in the database, unmapped and
defaulted. Dropping it is deferred to a separate later migration (`0053`+ — `0031`
through `0052` are all taken now: `0038` went to
`specialist_consults`'s `read_state`/`established`/rubric stamp instead, `0039`
to the reviewer-role/review-tables migration instead, `0040` went to
`prose_format`, `0041` to `summary_posted_at`, and `0052` to
`dimension_rationales`, see the
`0028` box in `docs/operations/migration-deploy-notes.md`), which **has not been written, let alone applied** — see the design
doc's §8.

- **PI** — the original account: own profile, own lab agent, `/profile` and `/agent`.
- **Manager** — global, read-mostly: `/manager/pis`, `/manager/assessments`,
  `/manager/discussions`, `/manager/activity`. A scoped, deliberate reversal of the
  original all-GET guarantee (design D1) adds exactly nineteen write routes — `POST
  /manager/pis` (create a PI via ORCID), `/manager/pis/{id}/profile` (edit a PI's
  profile fields), `/manager/pis/{id}/profile/retry` (queue profile generation again
  after a dead job or for an ungrounded profile), `/manager/pis/{id}/mute` / `/unmute` (toggle a PI's agent),
  `/manager/pis/{id}/verify-email` (mark the PI's address verified),
  `/manager/pis/{id}/slack/provision` / `/activate` (install a pending PI's Slack
  bot and bring the agent live), `/manager/pis/{id}/grants/{grant_id}/veto` /
  `/manager/pis/{id}/industry/{evidence_id}/veto` (mark a RePORTER-derived grant
  or a piece of industry evidence as not this PI's),
  `/manager/pis/{id}/orcid-fundings/{funding_id}/veto` (mark an ORCID funding as not
  this PI's), `/manager/pis/{id}/grant-identity/pin`, `/unpin` and `/none` (staff pin
  of the PI's RePORTER profile, removing the pin, and "PI has no RePORTER profile"),
  and the five Companies routes
  (scout_hub 1.10.0) — `/manager/pis/{id}/companies` (add a company by hand),
  `/manager/pis/{id}/companies/{company_id}/delete`, `/confirm` and `/reject`
  (review a discovered or confirmed company; delete records a confirmed company as
  rejected, and a manual add of that name revives it), and `/manager/pis/{id}/companies/discover`
  (queue company discovery) — and nothing else;
  `tests/integration/test_manager_views.py`'s
  `test_manager_router_mutations_are_an_explicit_allowlist` fails loudly on a twentieth.
  **Still cannot impersonate** or set roles (both stay admin-only), and there is
  deliberately no LLM-call drill-down and no export. A manager MAY provision a Slack
  bot and activate a pending PI's agent from `/manager/pis/{id}` (F2, 2026-09-10) —
  subject to the same `activate_agent` gate an admin faces but with **no** override,
  which stays admin-only; the Slack OAuth callback keeps its baked-in
  `/admin/agents/slack/callback` path and only widened its gate to staff, refusing
  any install a different account started (`slack_app_provisions.initiated_by_user_id`,
  migration `0046`).
  Managers *do* see private (`collab_private`) discussion threads — a policy
  decision, recorded in the design doc.
- **Reviewer** — read+review only, no write outside review: read-only PI directory
  and assessments (`/manager/pis`, `/manager/pis/{id}`, `/manager/assessments`,
  `/manager/assessments/{id}`), plus leaving review feedback and
  approve/disapprove via `/reviews`, and asking the assessment chat
  (`/assessment-chat/*`, reviewer tier — see `docs/operations/assessment-chat.md`). Cannot assign reviewers, cannot see
  discussions/activity/prompt-suggestions, has no PI surface (`/profile`,
  `/agent`), and has no admin access. Provisioned the same way as manager/admin:
  the admin Account Type role-set on `/admin/users/{id}`, or the `role:set` CLI.
  `get_pi_user` denies it exactly as it denies a manager; `is_staff` (admin OR
  manager) deliberately **excludes** it, since the manager router's write
  handlers and the discussions/activity/prompt-suggestions pages must never
  admit a reviewer; the review-scoped predicate is a separate dependency,
  **`get_review_user`** (admin OR manager OR reviewer), gating the `/reviews`
  router and the manager router's reviewer-visible GETs. The assessment chat router
  applies the same predicate itself, AFTER refusing impersonation, so an impersonated
  session is told `{"error": "impersonating"}` rather than refused by role
  (`_refused`, `src/routers/assessment_chat.py`).
  **Review WRITES are allowed while impersonating** (operator decision
  2026-09-10, reversing the earlier blanket refusal): feedback submit/edit and
  the approve/disapprove status writes go through, attributed to the
  *impersonated* user, with the real admin recorded in the new
  `recorded_by_user_id` second signature (`assessment_reviews` and
  `assessment_review_events`, migration `0044`; NULL means the named user acted
  in person). The four non-review actions on that router —
  reviewer assign, unassign, prompt-suggestion generate, and prompt-suggestion
  status — still refuse an
  impersonated session outright (`refuse_impersonation`,
  `src/dependencies.py`, shared with the admin role and delete routes and
  `POST /profile/delete-account`).
- **Admin** — everything, including `/admin/*` and impersonation.

`is_manager` means exactly `user_role == 'manager'`. The "may see the manager views"
predicate is **`is_staff`** (admin OR manager). **Never widen `is_admin`** — impersonation
(`src/dependencies.py`, and a duplicate check in `src/main.py`) is gated on it and
returns a fully substituted user, so a manager satisfying `is_admin` would be a full
privilege escalation.

**One PI-surface rule.** `User.may_use_pi_surfaces` (`src/models/user.py`) is
`user_role in (pi, admin)`: the nav links in `base.html`, `get_pi_user`, the
onboarding bounce and the post-login onboarding redirect all read it. It is an
allowlist, so a role added later is excluded from the PI surfaces until listed,
and for every role in `VALID_USER_ROLES` it equals the old
`not (is_manager or is_reviewer)`. Each excluded role keeps its own landing page.

**Impersonation.** It is held in the signed session (`session["impersonate_user_id"]`,
with `impersonate_expires_at` 24 h after the start; `start_impersonation` /
`end_impersonation` in `src/dependencies.py`) and honoured only while the session holder
is an admin; the old unsigned `copi-impersonate` cookie is neither read nor written.
`refuse_impersonation`, `recorded_by` and `impersonation_note`
live in `src/dependencies.py`. Role changes (`POST /admin/users/{id}/role`) are
refused under impersonation (403), as are account deletions. Writes on the agent
page (`/agent/*`) made while impersonating are attributed to the impersonated
user, with `impersonated by admin <uuid>` in the log line; a public-profile save
records its revision with mechanism `web_impersonated` and that note as the
change summary. A role change that would take the PI surfaces away (to `manager` or `reviewer`) is refused, on `/admin/users/{id}` and by `role:set`, while the account owns an `active`, `pending` or `inactive` agent: suspend the agent first (`src/services/user_roles.py`). An agent page write also needs the PI surfaces (`get_agent_with_access(..., write=True)`).

**Exclude `manager`, never "non-PI".** An admin is not a `pi` either, and admins keep
the PI surfaces (`base.html` still offers them My Profile / My Agent), so a `!= 'pi'`
guard locks admins out of their own account — `/profile` bounces an admin whose
onboarding is incomplete to `/onboarding`, and only `POST /onboarding/save-profile` can
ever clear that flag. The **five** PI-write POSTs — `/onboarding/save-profile`
(`src/routers/onboarding.py:133`), `/onboarding/retry` (`:251`), `/profile/save`
(`src/routers/profile.py:132`), `/profile/refresh` (`:172`) and `/agent/request`
(`src/routers/agent_page.py:275`) — are gated on **`get_pi_user`** in
`src/dependencies.py:182`, which 403s a manager and lets an admin through. A
read-only redirect is not enough there: `save-profile` writes
`onboarding_complete` and creates the profile, which is the whole gate on
`/agent/request` — so an ungated pair is a manager with a lab bot.
`POST /profile/save` was the fifth and was left on `get_current_user` when the
other four were moved (fixed 2026-08-22, E1.3): it calls `apply_profile_edits`,
so a manager could create a `ResearcherProfile` on their own account and rewrite
`users.email` — the field delegate-invitation acceptance binds to. Managers keep
`POST /manager/pis/{user_id}/profile`, which calls the same service function
against a PI they name.

Appoint from **/admin/users/{id} → Account Type**. The last admin cannot be demoted
there (that guard counts only admins with `access_status='allowed'` — a denied admin
cannot log in, so counting one would just make demotion easier). The check,
`ensure_admin_remains` (`src/services/admin_invariant.py`), takes the
`ADMIN_INVARIANT_LOCK_KEY` advisory transaction lock before counting, and both doors
out of adminhood (the role route and `POST /profile/delete-account`) call it, so two
concurrent demotions cannot both pass. The lock is held to the caller's commit.
Not covered: an admin deleted by another admin (`/admin/users/{id}/delete`) or
denied access does not go through this check (open-findings rows
`2026-09-30/W-admin-cross-delete` and `2026-09-30/W-admin-access-denial`). If no admin can log in at all, recover from a container shell:

    docker compose -f docker-compose.prod.yml exec blackbird-app \
      python -m src.cli role:set --orcid 0000-0000-0000-0000 --role admin

New managers are provisioned in two steps: they sign in with ORCID (landing on
`/access-pending`), an admin approves them at `/admin/access-requests`, then sets their
role. Between approval and role-setting the account behaves as a PI.

**A pending user's typed address is unverified.** The address typed on `/access-pending`
goes to `users.contact_email_unverified`, shown as "(unverified)" on
`/admin/access-requests`, and is never copied to `users.email`. Every `users.email` write
goes through `src/services/user_email.py::assign_user_email`, which refuses an address
another account holds in any case (a login whose ORCID address is taken proceeds without
an email instead of failing), and `tests/unit/test_email_writer_tripwire.py` fails on any
other writer.

**Email verification** (migration `0057`, web UI remediation spec §6.6).
`users.email_verified_at` is set only by an admin (`POST /admin/users/{id}/verify-email`,
any user) or a manager (`POST /manager/pis/{id}/verify-email`, PIs only), both refused
under impersonation and each recorded as an `admin_audit_events` row with action
`verify_email`; there is no verification email. `assign_user_email` clears it whenever
the address changes (compared case-insensitively) or is cleared. Delegate-invitation
acceptance requires a PI-surface account (`may_use_pi_surfaces`) whose verified address
equals the invited one; otherwise the page says "An administrator must verify your email
address before you can accept this invitation." Every account that had an email when
`0057` was applied was marked verified (owner decision D8).

**Profile edit forms refuse a stale save.** The four edit forms carry the
`profile_version` they were rendered with; a save after a regeneration or another edit
writes nothing and asks the user to reload. A regeneration keeps a human edit saved while
it ran.

**Revoking access now ends the session immediately** (`src/dependencies.py:104`,
fixed 2026-08-22 as E1.2). Sessions are unkeyed signed cookies with a 30-day
`max_age` and no server-side store, so before `0057` `users.access_status` was the only
revocation signal there was — and nothing read it after login, so
`admin_deny_access` set the column and changed nothing the user could observe: a
denied user's `GET /profile` returned 200 for up to thirty more days. The check
now pops `user_id`, repopulates `pending_access` in the shape `auth.py` writes at
login, and 302s to `/access-pending`. Two consequences: `/access-pending` and
`POST /logout` must stay free of `get_current_user` or the bounce becomes a loop
with no way out; and the check runs on the **session holder**, deliberately
before the impersonation block, so **an admin can still impersonate a denied
account** — that is a support path, not an oversight, and it is commented at the
check.

**Sessions** (web UI remediation spec §6.7). The cookie is `__Host-copi-session` when
`ALLOW_HTTP_SESSIONS=false` (production) and `copi-session` otherwise
(`src/main.py::session_cookie_name`). Login clears the pre-login session, keeping only the
vetted `next` and a pending invite token, and stores `users.session_epoch` (NULL as 0) as
`session["epoch"]`; `get_current_user` refuses a session whose epoch differs, after the
access check above. `POST /logout` bumps the epoch, signing the account out on every
device, reading `session["user_id"]` directly and still with no auth dependency; access
denial and a role change (`/admin/users/{id}/role`, `admin:grant`, `admin:revoke`,
`role:set`) bump it too (`src/services/session_epoch.py`).

## Deleting a PI

**All deletion goes through `src/services/user_deletion.py::delete_user_account`**
(both `POST /profile/delete-account` and `POST /admin/users/{id}/delete`).
Never `db.delete(user)` directly — before 2026-08-25 that was the whole
process, and it left the deleted PI's agent RUNNING: `agents.user_id` is SET
NULL, the roster sync loads by status alone, and the agent reads its persona
from `profiles/public/{agent_id}.md`, not from the users table. See
`docs/audits/2026-08-25-pi-deletion/README.md`.

What the teardown does: suspends the linked agent (`status='suspended'` — the
one state a manager unmute cannot undo), purges `profile_revisions` (full
profile snapshots), the agent's `pi_dm_messages`, the
`jhu_tenure_start:{user_id}` app_settings key, the on-disk
`profiles/public/{agent_id}.md` and `profiles/memory/{agent_id}` artifacts,
and revokes the Slack bot token (post-commit, best-effort — a failed
revocation is logged loudly and leaves the token in the DB column for manual
revocation; the agent is suspended either way). Only a value that passes
`is_valid_token` (an `xoxb-` bot token) is ever sent to `auth.revoke`: anything
else in the column is reported for manual review and left alone, so a user token
pasted there by mistake is never revoked. The agent ROW is kept: it is
the record behind old messages and assessments, and its `agent_id` slug stays
reserved. Deliberately retained: `agent_messages`, `llm_call_logs`,
assessments, and everything already posted to Slack — both confirmation pages
say so. The simulation's `llm_call_logs` keep the persona text the bots were sent (D35); both deletion pages say so.

Guards: an impersonating admin cannot trigger the self-service delete (403);
the last loginable admin cannot self-delete; the admin form has a
default-checked "also remove from the access allowlist" checkbox — without it
a deleted allowlisted ORCID can sign straight back in as `allowed`. Related:
the allowlist promotes only `pending` users at login; a `denied` user stays
denied (`src/routers/auth.py`). The admin delete route refuses impersonated
sessions the same way. Known accepted residual: a deletion issued while that
user's generate_profile job is mid-run can block on the jobs-row lock until
the pipeline finishes; the delete still commits.

The roster criterion lives in `src/agent/roster_query.py` and excludes
`pi_lab` rows with `user_id IS NULL` (hub/specialists are exempt). Both the
startup load and `_sync_roster_from_db` use it; `--all-agents` bypasses it at
startup only — the ~30s live sync still applies the criterion and evicts
non-matching rows on its first tick.
One consequence to know before touching **/admin/agents → Link**: UNLINKING an
active `pi_lab` agent (submitting the link form with an empty user) now evicts
it from the running roster within ~30s — the same invariant, applied live.
