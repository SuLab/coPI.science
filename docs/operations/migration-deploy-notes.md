# Migration deploy notes

Reference detail behind the rules in the root `CLAUDE.md`, which keeps only what
every session needs. Dated statements hold as of the date they give; re-measure a
count or a line number before relying on it.

One box per migration (and per no-migration change that still needed a deploy
order), oldest first. Each records the hazard of that deploy: what old code on the
new schema and new code on the old schema do, and whether the agent image had to
ship with it. The guarded procedure itself is `docs/production-migration.md`.

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
> warning in `docs/operations/host-and-simulation.md`. One caveat beyond the usual migrate-before-serve reasoning: an interview
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

> **Deploy order for `0036_panel_owed_thread_id_truncated_and_repairs` — migrate
> BEFORE the new code serves.** `0036` is five additive nullable columns, one
> foreign-key rule corrected, and two data repairs, so *old code against the new
> schema* is safe (nothing is backfilled, nothing is NOT NULL). The reverse
> takes the live site down in pieces. The new code **maps
> `opportunity_assessments.panel_owed` and `.thread_id`,
> `specialist_consults.truncated`, and `llm_call_logs.cache_read_input_tokens` /
> `.cache_creation_input_tokens`**, so against a pre-`0036` database:
>
> * `/admin/assessments` and `/manager/assessments` raise `UndefinedColumn` —
>   both the `select(OpportunityAssessment)` at `src/services/directory.py:464`
>   and the unvetted-panel banner COUNT, which names `panel_owed` through
>   `unvetted_panel_filter()`;
> * both assessment DETAIL pages raise — `select(OpportunityAssessment)` at
>   `src/services/assessment_detail.py:1306` and `select(SpecialistConsult)` at
>   `:1718`;
> * `/admin/activity/{run_id}/llm-calls` raises — `select(LlmCallLog)` at
>   `src/routers/admin/runs.py:105`;
> * on the engine side the LLM-log writer (`src/agent/engine/llm_log.py:116`) and the consult
>   writer (`src/agent/engine/panel.py:164`) name the new columns in their INSERTs, so every
>   `llm_call_logs` flush and every `specialist_consults` row fails — the flush
>   path will say LOST with a row count, which is the loud half; the consult
>   write is best-effort and is the silent half.
>
> Build, migrate from a one-off container, then start — same ordering as `0028`
> and `0030`:
>
>     DC="docker compose -f docker-compose.prod.yml"
>     $DC build blackbird-app worker
>     $DC run --rm blackbird-app alembic upgrade head
>     $DC run --rm blackbird-app alembic current      # must equal `alembic heads`
>     $DC up -d blackbird-app worker
>
> The agent image bakes `src/` in too and must be rebuilt separately
> (`$DC --profile agent build agent`). Production was at `0035` and this branch
> was `0036` when this was written, so this box applied to the next deploy, not to
> some hypothetical one.
>
> Four things to expect afterwards, none of them a regression:
>
> 1. **The unvetted-panel banner on `/admin/assessments` JUMPS** to include every
>    row written before `0036` — 63 of them when the migration was written. Those
>    rows have `panel_owed IS NULL`, which is deliberately never backfilled
>    ("guessing would manufacture exactly the verification this column exists to
>    stop asserting"), and NULL is one of the three states
>    `unvetted_panel_filter()` counts. Staff read that page daily; the number
>    going up on deploy day is the fix working, not a new problem.
> 2. **`private_channel_members_user_id_fkey` is dropped and recreated ON DELETE
>    CASCADE**, taking a brief ACCESS EXCLUSIVE lock on `private_channel_members`
>    and `users`. It is free today (0 production rows) and only gets dearer. It
>    fixes a real 500: under the old `SET NULL`, deleting a user who was a
>    private-channel member drove both owner columns of the membership row to
>    NULL, violated `CHECK ((agent_id IS NULL) <> (user_id IS NULL))`, and made
>    **both** `POST /profile/delete-account` and the admin delete raise.
>    `added_by_user_id` deliberately stays `SET NULL`.
> 3. **Repair order inside the migration is load-bearing** and is already coded
>    that way: repair (a) recovers 17 de-risking milestones the backfill script
>    lost to a wrong sidecar key, and repair (b) is `0031`'s JSON-`null` → SQL
>    NULL normalization applied to eleven more columns. `derisking_milestones` is
>    one of the eleven, so running (b) first makes (a) match zero rows *while
>    reporting success*. Do not reorder them, and do not run the statements by
>    hand out of order.
> 4. **The `truncated` column reads NULL as "not truncated"**, so the three
>    known-truncated consults on run 8b64a0e0 keep crediting the specialist floor
>    exactly as they do today. The alternative retroactively invalidates history
>    on no evidence.

> **Deploy order for `0037_recommended_next_experiment` — migrate BEFORE the new
> code serves.** `0037` is one additive nullable Text column
> (`opportunity_assessments.recommended_next_experiment`, sidecar item 10 of
> rubric v2.1.0 — the single experiment Blackbird should fund next), so *old
> code against the new schema* is safe. The reverse is not: the new code **maps
> the column**, so against a pre-`0037` database every
> `select(OpportunityAssessment)` — both assessment list pages, both detail
> pages — raises `UndefinedColumn`, and on the engine side `_persist_assessment`
> names it in the INSERT, so every verdict write fails too. Build, migrate from
> a one-off container, then start — same ordering as `0028`/`0030`/`0036`. NULL
> on every pre-`0037` row, deliberately never backfilled: old verdicts were
> never asked to name one, and `raw_verdict` keeps what they did emit. The same
> 2026-08-24 change set RENAMED the third gating key
> (`fto_achievable` → `translational_potential`, rubric v2.1.0): needs no DDL —
> `gating` is JSONB and old rows keep their `fto_achievable` key, unrewritten —
> but the agent image must be rebuilt or the running hub keeps emitting the old
> contract while the rubric banner claims 2.1.0.

> **Deploy order for `0038_specialist_consult_read_state_and_stamp` — migrate
> BEFORE the new code serves.** `0038` is four additive nullable columns on
> `specialist_consults` (`read_state`, `established`, `rubric_version`,
> `rubric_content_hash`), so *old code against the new schema* is safe. The
> reverse is not: the new code **maps all four**, so against a pre-`0038`
> database `select(SpecialistConsult)` at `src/services/assessment_detail.py`
> — read by both assessment detail pages, admin's and manager's — raises
> `UndefinedColumn`, and on the engine side `_record_specialist_consult`'s
> INSERT (`src/agent/engine/panel.py:164`) names all four, so every
> `specialist_consults` write fails too. (The discussions panel cards at
> `src/services/thread_panel.py` select an explicit column list that named
> none of the four when this box was written; it now names `read_state`, so that
> page raises against a pre-`0038` database too, and the migration's own
> docstring now says so too.) Build,
> migrate from a one-off container, then start — same ordering as
> `0028`/`0030`/`0036`/`0037`:
>
>     DC="docker compose -f docker-compose.prod.yml"
>     $DC build blackbird-app worker
>     $DC run --rm blackbird-app alembic upgrade head
>     $DC run --rm blackbird-app alembic current      # must equal `alembic heads`
>     $DC up -d blackbird-app worker
>
> The agent image bakes `src/` in too and must be rebuilt separately
> (`$DC --profile agent build agent`). Production was stamped `0037` when this
> was written, so this box applied to the next deploy, not to some hypothetical one.
>
> **`established` IS written, as of the 2026-08-28 persona-contract change.**
> This box previously said it was "knowingly unwritten"; that stopped being
> true when all eight personas gained an `established` key and `tools.py`
> began forwarding it on every consult. Read the three states carefully,
> because two of them are easy to conflate:
>
> * **NULL** — never asked. The row predates the contract change, or the
>   consult was never recorded. Never read it as "the specialist established
>   nothing".
> * **`[]`** — asked, and nothing came back. This is genuinely ambiguous and
>   deliberately not disambiguated: it covers both "the specialist named no
>   positives" and "the specialist ignored the key", because
>   `_str_tuple(data.get("established"))` yields the empty tuple for a missing
>   key and for an empty list alike. Do not report `[]` as a specialist finding.
> * **a non-empty list** — the only case that carries evidence.
>
> `read_state` is written on every new consult too (`read_state_for`,
> `src/agent/specialists.py`).
>
> ⚠️ **After editing anything under `prompts/` or the `_PI_LAB`/`_SCOUT_HUB`
> guidance, run `.venv-test/bin/python scripts/sync_prompt_set_docs.py`.**
> `docs/specs/2026-08-07-{pi,hub}-bot-prompts.md` embed every prompt file
> verbatim and `tests/unit/test_doc_prompt_sync.py` asserts it, so skipping the
> sync turns one prompt edit into a fistful of CI failures on the next full run
> rather than an error at the point of the edit. `--check` reports drift without
> writing.

> **Deploy order for `0040_assessment_prose_format` — migrate BEFORE the new
> code serves.** `0040` is one additive nullable String(20) column
> (`opportunity_assessments.prose_format`), so *old code against the new
> schema* is safe. The reverse is not: the new code **maps the column**, so
> against a pre-`0040` database every `select(OpportunityAssessment)` — both
> assessment list pages, both detail pages — raises `UndefinedColumn`, and on
> the engine side `_persist_assessment`'s INSERT names it, so every verdict
> write fails too. Build, migrate from a one-off container, then start — same
> ordering as `0028`/`0030`/`0036`/`0037`/`0038`:
>
>     DC="docker compose -f docker-compose.prod.yml"
>     $DC build blackbird-app worker
>     $DC run --rm blackbird-app alembic upgrade head
>     $DC run --rm blackbird-app alembic current      # must equal `alembic heads`
>     $DC up -d blackbird-app worker
>
> The agent image bakes `src/` in too and must be rebuilt separately
> (`$DC --profile agent build agent`) — this is also the change that ships the
> markdown phase4 prompt instruction the stamp gates, so a rebuild that skips
> the agent image leaves the hub writing markdown prose the running database
> stamps `NULL` (plain), the opposite of the intended pairing. NULL on every
> pre-`0040` row, deliberately never backfilled: those verdicts were never
> written under the markdown contract, so plain rendering is permanently
> correct for them, not a placeholder.

> **Deploy order for `0041_assessment_summary_posted_at` — migrate BEFORE the
> new code serves.** `0041` is one additive nullable `TIMESTAMPTZ` column
> (`opportunity_assessments.summary_posted_at`), so *old code against the new
> schema* is safe. The reverse is not, but only on the read side: the new code
> **maps the column**, so against a pre-`0041` database every
> `select(OpportunityAssessment)` — both assessment list pages, both detail
> pages — raises `UndefinedColumn`. The write side is genuinely safe:
> `_persist_assessment`'s INSERT never assigns `summary_posted_at` — that
> column is written later, by `_mark_summary_posted`, once a headline actually
> posts — and SQLAlchemy omits an unset, no-server-default nullable attribute
> from the generated INSERT's column list, so a fresh verdict write SUCCEEDS
> against a pre-`0041` schema either way. Build, migrate from a one-off
> container, then start — same ordering as
> `0028`/`0030`/`0036`/`0037`/`0038`/`0040`:
>
>     DC="docker compose -f docker-compose.prod.yml"
>     $DC build blackbird-app worker
>     $DC run --rm blackbird-app alembic upgrade head
>     $DC run --rm blackbird-app alembic current      # must equal `alembic heads`
>     $DC up -d blackbird-app worker
>
> The agent image bakes `src/` in too and must be rebuilt separately
> (`$DC --profile agent build agent`) — and here that rebuild is the whole
> point: the announce-on-close path lives in the engine, so an app-only deploy
> migrates the column and keeps losing headlines.
>
> NULL on every pre-`0041` row, deliberately never backfilled: for those rows
> the only record of whether a headline posted is the Slack channel itself, and
> a guess would be indistinguishable from a measurement. Use
> `scripts/backfill_assessment_headlines.py --stamp-only` to record one you
> have verified by eye, and the same script without `--stamp-only` to post one
> that is genuinely missing.
>
> ⚠️ **Repair the existing rows BEFORE you start any simulation run.**
> `src/agent/main.py` RESUMES the latest run by default — that is the restart
> command in `docs/operations/host-and-simulation.md` — and the new shutdown sweep
> announces every verdict of the resumed run whose `summary_posted_at` is NULL.
> NULL means "not announced", every pre-`0041` row is NULL whatever actually
> happened in Slack, so **resuming a pre-`0041` run re-announces headlines that
> are already public**, one unretractable duplicate post each. No code change
> can close this: the column is the only durable record, and before `0041`
> there was none.
>
> Concretely, for production run `61ccad6d` as measured 2026-08-29: six
> assessment rows, all `summary_posted_at IS NULL`, **five of whose headlines
> are already in `#assessments-summary`** and one (rothstein — conditional,
> 2.85, the run's highest score) genuinely missing. Resuming it as-is posts
> five duplicates and one correct headline.
>
> So, after `alembic upgrade head` and before starting ANY run, for every run
> that already has headlines in Slack: check the five against the channel by
> eye and stamp them (`--stamp-only`), post the missing one (no `--stamp-only`),
> then verify there is nothing left owed —
>
>     $DC exec -T postgres psql -U copi -d copi -t -A -c \
>       "SELECT COUNT(*) FROM opportunity_assessments
>          WHERE simulation_run_id = '<run>' AND summary_posted_at IS NULL;"
>
> — which must read `0` before that run is resumed. Run the script without
> `--apply` first: it previews every post and stamp it would make. A run with
> NO headlines in Slack needs none of this; the sweep announcing its rows is
> the fix working.

> **Deploy order for `0043_assessment_narrative_and_review_dimension_scores` —
> migrate BEFORE the new code serves.** `0043` is six additive nullable columns
> across two tables (`opportunity_assessments.headline` / `.key_points` /
> `.elevator_pitch`; `assessment_reviews.dimension_scores` / `.rubric_version` /
> `.rubric_content_hash`), so *old code against the new schema* is safe. The
> reverse fails in both directions at once. READ side: the new code **maps all
> six**, so against a pre-`0043` database every `select(OpportunityAssessment)`
> — both assessment list pages, both detail pages — and every
> `select(AssessmentReview)` — the detail pages' feedback list and
> `review_bot`'s own load — raises `UndefinedColumn`. WRITE side:
> `_persist_assessment`'s INSERT names the three narrative columns, so **every
> verdict write of a running simulation fails** — and that write is
> best-effort, so the failure is swallowed and one ERROR line lands in a log
> nobody is tailing while the Slack replies keep looking completely normal.
> That is the silent half, and it is the same shape as the 2026-08-06 near-miss
> `docs/operations/host-and-simulation.md` already records. `submit_feedback` and `edit_feedback`
> (`src/services/assessment_reviews.py:338-347`, `:405-407`) unconditionally
> assign `dimension_scores`/`rubric_version`/`rubric_content_hash` on every
> human review submission and edit too, so those ALSO fail against a
> pre-`0043` database — but LOUDLY, not silently: neither call site is wrapped
> in a best-effort try/except, so the `UndefinedColumn` propagates straight out
> of the route handler as a 500 rather than being swallowed into a log line
> nobody reads.
>
>     DC="docker compose -f docker-compose.prod.yml"
>     $DC build blackbird-app worker
>     $DC --profile agent build agent
>     $DC run --rm blackbird-app alembic upgrade head
>     $DC run --rm blackbird-app alembic current      # must equal `alembic heads`
>     $DC up -d blackbird-app worker
>     $DC up -d agent                                 # supervisor returns IDLE
>
> The agent rebuild is **not optional and not interchangeable with the prompt
> mount**. `prompts/` is bind-mounted, `src/` is baked. Prompt without image
> means the hub emits `headline`/`key_points`/`elevator_pitch` and
> `_persist_assessment` discards them — they survive only inside `raw_verdict`.
> Image without prompt means every new row writes NULL.
>
> All three narrative columns are NULL on every pre-`0043` row and are
> **deliberately never backfilled**: those verdicts were never asked for a
> headline, and a generated one would be indistinguishable from one the hub
> wrote. Every read path degrades — `headline` falls back to
> `company_or_project`, absent bullets and pitch render nothing, and the
> `#assessments-summary` headline omits the pitch segment entirely. Expect the
> new card list and the new detail brief to look, for the 12 rows then on
> record, almost exactly like the pages they replaced; the narrative half
> arrives with the first interview a rebuilt agent concludes.
>
> Production was stamped `0042` when this was written, so this box applied to the
> next deploy — as does the combined `0044`/`0045`/`0046` box immediately below
> it, which ships in the same deploy.

> **Deploy order for `0044` + `0045` + `0046` — migrate BEFORE the new code
> serves.** These three land together (2026-09-10, the seven-feature branch) and
> are all additive nullable columns, so *old code against the new schema* is
> safe in every case: nothing is backfilled, nothing is NOT NULL, and every
> read path treats NULL as the pre-migration answer. **Note the numbering: the
> plan documents assigned `0045` to F2 and `0046` to F1; execution swapped
> them.** What each one adds:
>
> * **`0044_review_recorded_by`** — `assessment_reviews.recorded_by_user_id`
>   and `assessment_review_events.recorded_by_user_id` (UUID FK `users`, ON
>   DELETE SET NULL). The second signature on a review written while an admin
>   was impersonating; NULL means the named reviewer/actor acted in person.
> * **`0045_llm_call_logs_thread_phase`** — `llm_call_logs.thread_phase`
>   (String(20): `explore`/`decide`/`conclude`) and `.message_ordinal`
>   (Integer). Stamped by the engine from the same `phase4_guidance()` call
>   that built the prompt. NULL on every pre-`0045` row and on
>   `new_post`/memory turns, deliberately never backfilled.
> * **`0046_slack_provision_initiated_by`** —
>   `slack_app_provisions.initiated_by_user_id` (UUID FK `users`, ON DELETE SET
>   NULL). Records the staff account that clicked "Install Slack bot", so
>   `complete_provisioning` can refuse to land a bot token for an install a
>   DIFFERENT account started — the Slack OAuth callback is a third-party
>   redirect and can carry no CSRF token, and its gate is now staff-wide rather
>   than admin-only. NULL (a pre-`0046` row, or a bulk
>   `scripts/make_install_links.py` row) is completable by an **admin only** —
>   allowing anyone staff would be a *widening* of the pre-`0046` admin-only
>   callback rather than a restoration of it.
>
> The reverse direction — new code against the old schema — breaks all three
> ways, and the `0046` failure is the one that will reach you first:
>
> * pre-`0044`: the new code **maps both `recorded_by_user_id` columns**, so
>   every `select(AssessmentReview)` and `select(AssessmentReviewEvent)` raises
>   `UndefinedColumn` — both assessment detail pages' feedback lists and
>   `review_bot`'s own load — and `submit_feedback` / `edit_feedback` /
>   the status write (`src/services/assessment_reviews.py`) assign the column
>   unconditionally, so every human review submission, edit and
>   approve/disapprove 500s out of the route handler. Loud, not silent.
> * pre-`0045`: the new code **maps `llm_call_logs.thread_phase` and
>   `.message_ordinal`**, so `/admin/activity/{run_id}/llm-calls`
>   (`select(LlmCallLog)`, `src/routers/admin/runs.py`) and the
>   `src/services/simulation_stats.py` aggregates raise `UndefinedColumn`, and
>   on the engine side the `_llm_log_record` INSERT names both columns — so
>   **every `llm_call_logs` flush of a running simulation fails**, which the
>   flush path reports as LOST with a row count.
> * pre-`0046`: `start_provisioning`'s INSERT names
>   `initiated_by_user_id` and `complete_provisioning`'s select reads it
>   (`src/services/admin_provisioning.py`), so **ALL Slack bot provisioning
>   breaks** — both the start of an install and the callback that lands the
>   token.
>
>     DC="docker compose -f docker-compose.prod.yml"
>     $DC build blackbird-app worker
>     $DC --profile agent build agent
>     $DC run --rm blackbird-app alembic upgrade head
>     $DC run --rm blackbird-app alembic current      # must equal `alembic heads` (0046)
>     $DC up -d blackbird-app worker
>     $DC up -d agent                                 # supervisor returns IDLE
>
> **The agent rebuild is REQUIRED, not optional**, for two independent reasons.
> First, `0045`'s producers live in the engine (`src/agent/engine/llm_log.py`, which
> stamps the reply's own row, and `src/agent/tools.py`, which stamps a
> specialist consult's) and `src/` is BAKED into the
> agent image — an app-only deploy migrates the two columns and then writes
> NULL into them forever. Second, this deploy bumps the **scout_hub prompt set
> to `1.3.0`**, whose `key_points` is a three-group object rather than a flat
> list; `prompts/` is bind-mounted and `src/` is baked, so the prompt and the
> image must ship TOGETHER. Prompt without image means the hub emits the
> three-group object and the old parser mangles or discards it; image without
> prompt means the new parser is fed the flat 1.2.x shape, which is benign —
> `normalize_key_points` still accepts a flat list and passes it through. So
> the hazardous half of this pairing is prompt-without-image, not the reverse.
> Same pairing hazard the `0043` box describes, for the same reason.
>
> Per the control-plane section of `docs/operations/host-and-simulation.md`, `$DC up -d agent` brings the supervisor back
> **IDLE**: starting a run afterwards is a separate, explicit operator action
> from `/admin/simulation`.

> **Deploy order for `0047_pi_grants_and_industry_evidence` — migrate BEFORE the
> new code serves.** `0047` adds three tables (`pi_grants`,
> `pi_industry_evidence`, `pi_industry_scores`) and two `job_type_enum` values
> (`enrich_grants`, `industry_evidence`). Additive, so *old code against the new
> schema* is safe. The reverse: `/manager/pis` and `/manager/pis/{id}` select the
> new tables (`UndefinedTable`), and the worker's two new handlers fail every
> job they're given. The engine is untouched by this change — the **agent**
> image needs no rebuild for it alone — but the **worker** image (the two new
> job handlers run there) and the **app** image (the new manager panels) both
> do.
>
>     DC="docker compose -f docker-compose.prod.yml"
>     $DC build blackbird-app worker
>     $DC run --rm blackbird-app alembic upgrade head
>     $DC run --rm blackbird-app alembic current      # must equal `alembic heads` (0047)
>     $DC up -d blackbird-app worker
>     $DC exec -T blackbird-app python scripts/enqueue_enrichment.py          # preview
>     $DC exec -T blackbird-app python scripts/enqueue_enrichment.py --apply  # backfill existing PIs
>
> Two new manager POSTs — `/manager/pis/{user_id}/grants/{grant_id}/veto` and
> `/manager/pis/{user_id}/industry/{evidence_id}/veto` — bring the explicit
> write allowlist (see Account Types in `docs/operations/pis-and-access.md`) to **eight**.
> `ResearcherProfile.grant_titles` is now RePORTER-derived and tenure-filtered
> once the `enrich_grants` job has run for a PI (the ORCID-fundings seed is
> never wiped by an empty RePORTER result — it is only supplemented); the
> industry-interest score is manager-only and has no import path into profiles
> or prompts (`tests/unit/test_enrichment_isolation.py` is the tripwire).
> RePORTER silently ignores unknown criteria keys and returns the whole
> database rather than erroring, so `src/services/nih_reporter.py` refuses any
> key outside `ALLOWED_CRITERIA` and aborts when `meta.total` exceeds a cap
> instead of paging through it blind. A PI with no recorded tenure start gets
> grants labelled `org_only` (JHU affiliation cannot be tenure-scoped) and an
> industry score of **Unscored** (`reason: no_tenure_start`); a field
> percentile additionally needs at least three other scored PIs in the same
> `primary_field` or it stays `reason: cohort_too_small`. Enum values cannot be
> dropped in Postgres; a downgrade drops the three tables and leaves the two
> `job_type_enum` values in place, exactly as `0039` does for
> `review_feedback_analysis`.

> **Deploy order for `0048_assessment_score_rationale` — migrate BEFORE the new
> code serves, and rebuild the AGENT image in the same deploy.** `0048` is one
> additive nullable Text column (`opportunity_assessments.score_rationale`,
> sidecar item 10 of scout_hub prompt set 1.4.0 — the staff-only plain-language
> account of why the dimension scores add up to the score they do), so *old
> code against the new schema* is safe. The reverse breaks both ways. READ: the
> new code **maps the column**, so every `select(OpportunityAssessment)` — both
> assessment list pages, both detail pages — raises `UndefinedColumn`. WRITE:
> `_persist_assessment` names it in the INSERT, and that write is best-effort,
> so **every verdict of a running simulation is lost** to one ERROR line in a
> log nobody is tailing while the Slack replies keep looking normal. Same shape
> as the `0043` box above.
>
>     DC="docker compose -f docker-compose.prod.yml"
>     $DC build blackbird-app worker
>     $DC --profile agent build agent
>     $DC run --rm blackbird-app alembic upgrade head
>     $DC run --rm blackbird-app alembic current      # must equal `alembic heads` (0048)
>     $DC up -d blackbird-app worker
>     $DC up -d agent                                 # supervisor returns IDLE
>
> **The agent rebuild is required, and the hazardous half of the pairing is
> prompt-without-image.** `prompts/` is bind-mounted and `src/` is baked. This
> deploy bumps the scout_hub prompt set to **1.4.0**, whose `key_points` is a
> FIVE-group object (`significance`, `innovation`, `clinical_actionability`,
> `key_questions`, `commercial_potential`) and which adds `score_rationale`.
> Image-without-prompt is benign: `normalize_key_points` now accepts any SUBSET
> of the five known group keys, so a 1.3.0 three-group sidecar still stores
> (2026-09-14 — it used to demand exact set equality, which meant one omitted
> group stored `key_points = NULL` and lost the whole field to `raw_verdict`).
> Prompt-without-image writes NULL into `score_rationale` forever and hands the
> five-group object to a parser that rejects it.
>
> NULL on every pre-`0048` row and deliberately never backfilled: those
> verdicts were never asked for a score rationale, and a generated one would be
> indistinguishable from one the hub wrote. Both assessment surfaces render
> nothing when it is NULL.

> **Deploy order for `0049_assessment_strengths_risks` — migrate BEFORE the
> new code serves, and rebuild the AGENT image in the same deploy.** `0049` is
> two additive nullable JSONB columns (`opportunity_assessments.strengths` /
> `.risks`, sidecar items 11/12 of scout_hub prompt set 1.5.0 — the hub's own
> two-to-four-bullet strengths and risks lists for a verdict), so *old code
> against the new schema* is safe. The reverse breaks both ways. READ: the new
> code **maps both columns**, so every `select(OpportunityAssessment)` — both
> assessment list pages, both detail pages — raises `UndefinedColumn`. WRITE:
> `_persist_assessment` names both in the INSERT, and that write is
> best-effort, so **every verdict of a running simulation is lost** to one
> ERROR line in a log nobody is tailing while the Slack replies keep looking
> normal. Same shape as the `0043`/`0048` boxes above.
>
>     DC="docker compose -f docker-compose.prod.yml"
>     $DC build blackbird-app worker
>     $DC --profile agent build agent
>     $DC run --rm blackbird-app alembic upgrade head
>     $DC run --rm blackbird-app alembic current      # must equal `alembic heads` (0049)
>     $DC up -d blackbird-app worker
>     $DC up -d agent                                 # supervisor returns IDLE
>
> **The agent rebuild is required, and the hazardous half of the pairing is
> prompt-without-image.** `prompts/` is bind-mounted and `src/` is baked. This
> deploy bumps the scout_hub prompt set to **1.5.0**, whose sidecar gains the
> `strengths`/`risks` keys. Prompt-without-image (an edited prompt served by
> an old image) writes NULL into both columns forever: the old image's parser
> does not know the two keys, so `normalize_bullets` is never called on them
> and `_persist_assessment` never assigns them — the hub's bullets survive
> only inside `raw_verdict`. Image-without-prompt (a rebuilt image serving an
> unbumped prompt) is benign: an old sidecar simply never emits `strengths` or
> `risks`, `verdict.get(...)` reads `None`, and `normalize_bullets(None)` is
> `None` — the columns store NULL exactly as they did before this migration.
>
> NULL on every pre-`0049` row and deliberately never backfilled: those
> verdicts were never asked for strengths/risks bullets, and generated ones
> would be indistinguishable from bullets the hub actually wrote. Both
> assessment detail pages render an "In the hub's words" section only when the
> column is non-NULL. Like `score_rationale`, both fields are app-only:
> `#assessments-summary` is untouched by this migration and still renders
> only its existing six fields — label, recommendation, band/score,
> permalink and the clipped elevator pitch.

> **Deploy order for `0050_assessment_landscape_and_evidence_maturity` — migrate
> BEFORE the new code serves, and rebuild the AGENT image in the same deploy.**
> `0050` is two additive nullable JSONB columns
> (`opportunity_assessments.competitive_landscape` / `.evidence_maturity`, sidecar
> items 13/14 of scout_hub prompt set 1.7.0), so *old code against the new schema*
> is safe. The reverse breaks on FOUR read surfaces, not two, plus the write path.
> READ: the new code maps both columns, so every `select(OpportunityAssessment)`
> raises `UndefinedColumn` — both assessment list pages, both detail pages,
> `src/services/review_bot.py` (on the **worker**, so every
> `review_feedback_analysis` job fails) and `src/routers/reviews.py` (so feedback
> submit/edit 500s out of the handler). WRITE: `_persist_assessment` names both in
> the INSERT, and that write is best-effort, so **every verdict of a running
> simulation is lost** to one ERROR line in a log nobody is tailing while the Slack
> replies keep looking normal. Same shape as the `0043`/`0048`/`0049` boxes.
>
> ⚠️ **Before you touch the working tree at all, confirm `/admin/simulation` shows
> no running engine.** `prompts/` is the host working tree and `Agent._load_prompt`
> → `_load_file` does a `read_text()` **per use**, so checking out 1.7.0 reaches a
> live agent immediately — before the build, before the migration. And
> `prompt_set_stamp` is read once at run start, so a run in flight would
> permanently record 1.6.0 for output produced under 1.7.0.
>
>     DC="docker compose -f docker-compose.prod.yml"
>     $DC build blackbird-app worker
>     $DC --profile agent build agent
>     $DC run --rm blackbird-app alembic upgrade head
>     $DC run --rm blackbird-app alembic current      # must equal `alembic heads` (0050)
>     $DC up -d blackbird-app worker
>     $DC up -d agent                                 # supervisor returns IDLE
>
> **The agent rebuild is required, and the hazardous half of the pairing is
> prompt-without-image.** `prompts/` is bind-mounted and `src/` is baked. This
> deploy bumps the scout_hub prompt set to **1.7.0**, which cuts the headline bound
> 140 → 110, reorders the elevator pitch to open on the problem, and adds the
> `competitive_landscape` and `evidence_maturity` keys. Prompt-without-image writes
> NULL into both columns forever — the old parser does not know the keys, so
> `normalize_bullets` is never called on them and `_persist_assessment` never
> assigns them; the hub's bullets survive only inside `raw_verdict`. It also leaves
> `_HEADLINE_SOFT_LIMIT` at 140 while the prose says 110, so the drift alarm goes
> quiet for exactly the headlines the change exists to catch.
> Image-without-prompt is benign: an old sidecar emits neither key,
> `verdict.get(...)` reads `None`, and `normalize_bullets(None)` is `None`.
>
> NULL on every pre-`0050` row and deliberately never backfilled: those verdicts
> were never asked for either field, and generated ones would be indistinguishable
> from bullets the hub actually wrote. Both are app-only —
> `#assessments-summary` is untouched and still renders only its existing six
> fields. The detail page's signals card is retitled **"Evidence summary"** (nav
> entry "Evidence") because it now holds five sections, not two.
>
> Design and evidence:
> `docs/specs/2026-09-21-review-driven-assessment-contract-design.md`.

> **The 2026-09-21 assessment-queue change ships NO migration, and both
> images still have to be rebuilt.** Schema head stays at `0049`; there is no
> migrate-before-serve ordering to observe. What it changes:
>
> * **scout_hub prompt set 1.5.0 → 1.6.0.** Sidecar item 6 now makes two
>   things mandatory in a `headline` — the specific disease/condition/patient
>   population, and what the intervention physically is *and does* — worded
>   for any modality, because most of the corpus is diagnostics rather than
>   drugs. No new sidecar key, so the `<assessment_json>` contract and
>   `_persist_assessment` are untouched, and the 10 existing headlines are
>   deliberately not regenerated. `prompts/` is bind-mounted, so this half
>   reaches the hub without an image build.
> * **The approve/disapprove/clear buttons are gone** from the queue card and
>   the detail page's Human review section. `POST
>   /reviews/assessments/{id}/status`, `set_review_status` and
>   `assessment_review_events` all survive, deliberately caller-less, behind a
>   `ROUTE_ALLOWLIST` entry in `tests/unit/test_reachability.py` — a new
>   category on that list, since every other entry names a real external
>   caller. The read-only Status line, Status history and the card's
>   Approved/Disapproved chip still render whatever history the database
>   holds, so a restored backup shows a chip no UI can now change.
> * **The queue has reviewed/unreviewed sub-tabs**, `?review=`, defaulting to
>   **unreviewed**. "Reviewed" means at least one `assessment_reviews` row —
>   written feedback, NOT an assignment and NOT a status event, which is
>   narrower than the card's own "Reviewed by" column
>   (`review_columns_for` unions status-event actors). The filter narrows
>   `total_count`, the five recommendation cards and `dimension_stats`; it
>   deliberately does not narrow the dropped-verdict or unvetted-panel
>   warnings, `lab_options`, or the run menu's per-run counts.
> * **The card's weighted score and band label moved into a collapsed "Why
>   this score" disclosure**, which now renders unconditionally so a pre-`0048`
>   row still carries its score. This is an accepted accessibility regression:
>   `band-label` exists because band-as-colour-alone was invisible to a
>   colour-blind reader, and it is now one click down. Only the recommendation
>   chip stays on the card face, so a recommendation/band disagreement is no
>   longer visible at a glance.
> * **URLs in assessment prose render as a blue underlined "cited paper"
>   link** (`src/services/prose_citations.py`, registered as the Jinja globals
>   `md_citations`/`plain_citations` on BOTH routers — each owns its own
>   `Jinja2Templates`, so one registration would 500 the other surface). The
>   anchor carries `href`, `title`, `class` and — on the plain path only —
>   `rel="noreferrer"`. It carries no `target`: DOMPurify 3.1.6's default
>   `ALLOWED_ATTR` has no `target`, so one would be stripped on the markdown
>   path and survive on the plain one, and the two renderings would disagree
>   visibly. `rel` is the opposite case: DOMPurify keeps it and it has no
>   visible behaviour, so it is emitted where it can be. Markdown has no
>   syntax for `rel`, so a `prose_format='markdown'` row's links carry none
>   and fall back to the browser's referrer policy. `#assessments-summary` is
>   untouched — `render_assessment_headline` still clips the RAW pitch, and
>   moving that would move the 600-character sentence boundary.
>
>     DC="docker compose -f docker-compose.prod.yml"
>     $DC build blackbird-app worker
>     $DC --profile agent build agent
>     $DC up -d blackbird-app worker
>     $DC up -d agent                                 # supervisor returns IDLE
>
> **The agent rebuild in that list changed the agent image's contents but
> changed no agent behaviour — and the distinction is the whole point.** The
> `src/` edits are six files: `src/routers/admin/` (then a single admin.py), `src/routers/manager.py`, `src/routers/reviews.py` and
> `src/services/{directory,assessment_detail,prose_citations}.py`. Five of
> those the engine never imports. The sixth does matter:
> **`src/services/assessment_detail.py` IS on the engine's import graph** —
> `src/agent/engine/verdicts.py` pulls `KEY_POINT_ACCEPTED_KEYS`, `normalize_bullets` and
> `normalize_key_points` from it, which is the sidecar parser. What this
> change added to that module is one new function, `has_review_filter()`,
> called only from `src/services/directory.py`; nothing the engine reaches
> was touched. So the rebuild was REQUIRED for image/tree parity and was
> behaviourally inert — not, as an earlier draft of this box said, a rebuild
> the agent did not need.
>
> Do not generalise this into "the agent never needs rebuilding". The rule is
> the one the "agent image does NOT mount `src/`" box states: **`src/` is
> baked, `prompts/` is mounted.** Any change under `src/agent/` — or under
> anything it imports, `assessment_detail.py` included — still requires
> `$DC --profile agent build agent` before the new code runs.
>
> The converse, which these docs had never said outright: an edit under
> (Superseded by Phase 2: the engine now snapshots `prompts/` once per start, see
> `src/agent/prompt_snapshot.py`; the text below records the earlier behaviour.)
> **`prompts/roles/**`** reached a RUNNING agent with no build and no
> restart, version bump included. `Agent._load_prompt` → `_load_file` does a
> `read_text()` **per use**, and `prompt_set_stamp` re-reads `role.toml` the
> same way. **`prompts/rubric/blackbird-rubric.toml` is the exception** and
> still needs a process restart — `src/services/blackbird_rubric.py` parses
> it ONCE at import (`_RUBRIC = parse_rubric(RUBRIC_PATH)` at module level),
> which is what the "Editing the rubric takes effect on restart, not on
> rebuild" box in `docs/operations/blackbird-hub.md` already says. Both statements are true; they are about
> different subtrees of `prompts/`.
>
> No sidecar key changed, so image-without-prompt and prompt-without-image are
> both benign for this change.

> **Deploy order for `0051_assessment_chat` — migrate BEFORE the new code serves.**
> `0051` adds two tables (`assessment_chat_turns`, `assessment_chat_usage`) and nothing
> else, so *old code against the new schema* is safe. New code against the old schema
> fails only the three `/assessment-chat/*` routes (`UndefinedTable`) — nothing else
> maps these tables — but the drawer button still renders, so migrate first anyway.
>
>     DC="docker compose -f docker-compose.prod.yml"
>     for s in blackbird-app worker agent; do         # rollback point: the build
>       docker image tag copi-blackbird-$s:latest copi-blackbird-$s:rollback-pre-0051
>     done                                            # overwrites :latest
>     $DC build blackbird-app worker
>     $DC --profile agent build agent                 # image/tree parity only
>     $DC run --rm blackbird-app alembic upgrade head
>     $DC run --rm blackbird-app alembic current      # must equal `alembic heads` (0051)
>     $DC up -d blackbird-app worker
>     $DC up -d agent                                 # ONLY when /admin/simulation shows no live run
>
> The engine and the worker import the two new model classes and the eight new
> settings and use none of them, so those two rebuilds change no behaviour; they keep
> image and tree in step. If a run is live, skip `up -d agent` until it ends.
> `prompts/assessment-chat.md` arrives with the working tree (bind-mounted into
> `blackbird-app`); the defaults need no `.env` change. Rollback: set
> `ASSESSMENT_CHAT_ENABLED=false` and recreate `blackbird-app`, or redeploy the previous
> image (the `rollback-pre-0051` tags from the first step); the two tables are harmless
> to old code. `alembic downgrade 0050` drops both tables AND every chat in them.

> **The 2026-09-25 reviewer change ships NO migration — rubric 3.5.0 and
> scout_hub 1.8.0 together, and all three images rebuild.** Design:
> `docs/specs/2026-09-24-reviewer-rubric-and-key-points-design.md`.
>
> * **Rubric 3.4.0 → 3.5.0.** Weights 25/25/25/15/5/5 (science block 50), the
>   fundable-experiment anchor tightened to a $100K–$300K / 6–12-month
>   decisive result, `[stage_bar.budget]` re-derived. Thresholds unchanged.
>   Stored scores/bands are never rescored; the outgoing 3.4.0 entry is in
>   `prompts/rubric/revisions.toml`. The web tier and the agent SUPERVISOR
>   parse the document once at import — the supervisor at container boot — so
>   both must restart.
> * **scout_hub 1.7.1 → 1.8.0.** `key_points` is six groups (Indication /
>   Audience, Lab Background, Proposal, Clinical Actionability, Key
>   Questions/Experiment, Commercial Opportunity) — the 1.8.0 contract,
>   superseded by 1.9.0's six one-bullet groups (see the `0052` box). Rows stored under the old
>   five (or three) keep their own labels; writes accept old and new keys.
>   **The hazardous half is prompt-without-image:** an old image rejects the
>   six new keys and stores `key_points` NULL. `prompts/` is live from the
>   moment the tree lands, so no run may start between landing and `up -d
>   agent`.
> * **Two behaviour fixes ride along.** An owed `#assessments-summary`
>   headline (`_announce_owed_headline`) now prints the row's STORED band and
>   score instead of recomputing them from the live weights, and
>   the dropped-verdicts backfill script (retired 2026-09-29) refused a run whose
>   rubric stamp weighed the dimensions differently from the live document.
>
>     DC="docker compose -f docker-compose.prod.yml"
>     for s in blackbird-app worker agent; do
>       docker image tag copi-blackbird-$s:latest copi-blackbird-$s:rollback-pre-3.5.0
>     done
>     $DC build blackbird-app worker
>     $DC --profile agent build agent
>     $DC up -d blackbird-app worker
>     $DC up -d agent        # ONLY when /admin/simulation shows no live run
>
> Start the next run FRESH: a resume only warns about the rubric change and
> would mix 3.4.0- and 3.5.0-scored verdicts in one run.
>
> Rollback: the `rollback-pre-3.5.0` images plus a revert commit — and that
> revert must APPEND a 3.5.0 entry to `prompts/rubric/revisions.toml`
> (sha256[:12] of the 3.5.0 file), or every row and review stamped 3.5.0
> renders "matches no entry". Old images render only `clinical_actionability`
> and `key_questions` of a six-group row; the other four groups stay in the
> column, hidden until the new code is back.

> **Deploy order for `0052_assessment_dimension_rationales` — migrate BEFORE the
> new code serves, and rebuild the AGENT image in the same deploy.** `0052` is
> one additive nullable JSONB column (`opportunity_assessments.dimension_rationales`,
> the companion to sidecar item 2's `scores` under scout_hub prompt set 1.9.0: one
> short reason per rubric dimension, keyed like `scores`), so *old code against
> the new schema* is safe. The reverse breaks both ways. READ: the new code maps
> the column, so every `select(OpportunityAssessment)` raises `UndefinedColumn` —
> both assessment list pages, both detail pages, `src/services/review_bot.py` (on
> the **worker**, so every `review_feedback_analysis` job fails),
> `src/routers/reviews.py` (feedback submit/edit 500s) and
> `src/services/directory.py`. WRITE: `_persist_assessment` names it in the
> INSERT, and that write is best-effort, so **every verdict of a running
> simulation is lost** to one ERROR line in a log nobody is tailing while the
> Slack replies keep looking normal. Same shape as the `0048`/`0049`/`0050` boxes.
> Design: `docs/specs/2026-09-28-assessment-chat-entry-and-key-points-design.md`.
>
> ⚠️ **Commit before building.** The builder stage's `git clean -ffdx` drops an
> UNTRACKED `alembic/versions/0052_assessment_dimension_rationales.py` from the
> image while the tracked `scripts/migrate/preflight.py` change that targets
> `0052` survives — the image then targets a revision it does not contain, and
> the deploy fails mid-way, after the dump. `run_migration.sh` compares only the
> image's commit to host HEAD and merely WARNs on a dirty count, so it does not
> catch this. `git status --porcelain --untracked-files=all -- src templates
> static prompts alembic scripts pyproject.toml alembic.ini` must print nothing
> before the first `$DC build`. And confirm `/admin/simulation` shows no live
> engine before the tree lands at all: `prompts/` is the host working tree and is
> read per use.
>
>     DC="docker compose -f docker-compose.prod.yml"
>     for s in blackbird-app worker agent; do
>       docker image tag copi-blackbird-$s:latest copi-blackbird-$s:rollback-pre-0052
>     done
>     $DC build blackbird-app worker
>     $DC --profile agent build agent
>     ./scripts/migrate/run_migration.sh              # rehearse (writes nothing)
>     ./scripts/migrate/run_migration.sh --apply      # dump → preflight → apply → postflight
>     $DC run --rm blackbird-app alembic current      # must equal `alembic heads` (0052)
>     $DC up -d blackbird-app worker
>     $DC up -d agent                                 # ONLY when /admin/simulation shows no live run
>
> **The agent rebuild is required, and the hazardous half of the pairing is
> prompt-without-image.** `prompts/` is bind-mounted and `src/` is baked. This
> deploy bumps the scout_hub prompt set to **1.9.0**, whose `key_points` is six
> groups of **ONE bullet each** — Indication / Audience, Lab Background, Proposal,
> Clinical Actionability, Path to Clinic / Commercialization, Commercial
> Opportunity — under a plain-language rule binding every bullet (expand every
> abbreviation, gloss every gene or pathway symbol, one main clause, at most 300
> characters). A 1.9.0 sidecar's `path_to_clinic` is an unknown key to an old
> image's `KEY_POINT_ACCEPTED_KEYS`, so `normalize_key_points` returns `None` and
> **`key_points` stores NULL for every verdict**, surviving only in
> `raw_verdict`; the old image also never assigns `dimension_rationales`, so that
> window writes NULL there too. Image-without-prompt is benign: an old sidecar
> emits neither new key, and `KEY_POINT_ACCEPTED_KEYS` is still the union of the
> current, retired and legacy keys, so a 1.8.0 sidecar still stores. **No run may
> start between landing the tree and `up -d agent`.** The simulation is not
> started as part of the deploy; start the next run FRESH — a resume would mix
> 1.8.0 and 1.9.0 key-point shapes and pre/post-`0052` rows in one run.
>
> **`key_questions` leaves the write contract but not the page.** The deciding
> experiment now lives only in `recommended_next_experiment` ("The ask"). The 1.9.0
> write path still accepts `key_questions` (with a milder "retired" WARNING), and
> the read path still renders it for every row that carries it:
> `RETIRED_KEY_POINT_GROUPS` in `src/services/assessment_detail.py` keeps a 1.8.0
> row's group under its 1.8.0 label, "Key Questions/Experiment", in its 1.8.0
> fifth slot (between Clinical Actionability and Path to Clinic), while a legacy
> 1.3.0–1.7.1 row keeps its own legacy labels and order. Stored rows are never
> rewritten.
>
> `dimension_rationales` is NULL on every pre-`0052` row and **deliberately never
> backfilled**: those verdicts were never asked for per-dimension reasons, and a
> generated one would be indistinguishable from one the hub wrote. A NULL column
> renders exactly as before. The rubric stays **3.5.0** — no `[meta].version`
> bump, so no `prompts/rubric/revisions.toml` entry and no rubric restart concern.
>
> Four page changes ride in the same deploy, none with a server change:
>
> * the detail page's nav "Ask about this assessment" button is gone, replaced by
>   a floating chat bubble bottom-right (the one `data-chat-open` on the page);
> * each list card gains a **chat** link to `…/assessments/{id}#chat`, and the
>   drawer's own script opens itself on that fragment — no new route;
> * the Evidence summary gains a neutral **Mid-scale** section, so a dimension
>   scored between the strength and risk thresholds finally appears somewhere;
> * each dimension's stored reason renders beside its score, to the **reviewer
>   tier as well as staff** (design D5, operator decision 2026-09-28), and the
>   assessment chat record quotes it on both tiers. The prompt tells the model
>   plainly that reviewers read this field; nothing mechanically stops an
>   unpublished disclosure landing there — the same residual the published
>   elevator pitch has carried since 2026-09-09.
>
> Rollback: redeploy the `rollback-pre-0052` images and revert the commit. The
> column is harmless to old code; `alembic downgrade 0051` drops it and every
> rationale in it.

> **Deploy order for `0053_remediation_phase0a_columns` — migrate BEFORE the new
> code serves, with the WORKER IDLE, and rebuild the AGENT image in the same
> deploy.** `0053` adds five nullable columns and backfills none of them:
> `jobs.not_before` (retry backoff), `users.contact_email_unverified` (the
> pending-access page's address, never copied to `users.email`),
> `opportunity_assessments.summary_claimed_at` (headline claims) and
> `simulation_runs.finalized_at` / `held_at` (end-reason classes). *Old code
> on the new schema* is safe: nothing old reads them. *New code on the old schema*
> is not: every `select` of `Job`, `User`, `OpportunityAssessment` or
> `SimulationRun` raises `UndefinedColumn` — every page, the worker's claim loop,
> and the engine's best-effort writes, which swallow it into ERROR lines while
> Slack keeps looking normal. Design: the 2026-09-29 audit-remediation spec (kept uncommitted by decision), §5 and §10.1.
>
> **The worker must be idle** (no `processing` row) when `--apply` runs: the old
> worker holds its transaction across a whole pipeline run, and `ALTER TABLE jobs`
> would wait on it past the chain's 10 s `lock_timeout` and roll the whole chain
> back. Check with
> `$DC exec -T postgres psql -U copi -d copi -c "select count(*) from jobs where status='processing'"`
> (must print 0), or stop the worker for the migration (`$DC stop worker`; the
> `up -d` below brings it back).
>
>     DC="docker compose -f docker-compose.prod.yml"
>     for s in blackbird-app worker agent; do
>       docker image tag copi-blackbird-$s:latest copi-blackbird-$s:rollback-pre-0053
>     done
>     $DC build blackbird-app worker
>     $DC --profile agent build agent
>     ./scripts/migrate/run_migration.sh              # rehearse (writes nothing)
>     ./scripts/migrate/run_migration.sh --apply      # dump → preflight → apply → postflight
>     $DC run --rm blackbird-app alembic current      # must equal `alembic heads` (0053)
>     $DC up -d blackbird-app worker
>     $DC up -d agent                                 # ONLY when /admin/simulation shows no live run
>
> No prompt or rubric file changes in this deploy, so no fresh-run requirement.
> Rollback: redeploy the `rollback-pre-0053` images; the columns are harmless to
> old code, and `alembic downgrade 0052` drops them (and every claim, hold and
> finalize stamp in them).

> **Deploy order for `0054_verdict_revision_columns` — migrate BEFORE the new
> code serves, with NO live run, and only as step 2 of the Phase 2 sequence.**
> `0054` adds four nullable columns (`opportunity_assessments.verdict_revision`,
> `.verdict_write_id`, `.verdict_ordinal`, `assessment_chat_turns.verdict_revision`).
> *Old code against the new schema* is safe: nothing old names them. *New code
> against the old schema* raises `UndefinedColumn` on every assessment read and
> every chat read, and the engine's verdict write is best-effort, so verdicts
> would be lost to one ERROR line. NULL is never backfilled; readers coalesce it
> to 1. Apply with `./scripts/migrate/run_migration.sh --target 0054` (the
> image's default target is `0055`), then run
> `scripts/migrate/merge_duplicate_assessments.py` (dry run, then `--apply`),
> then apply `0055`. The agent image must be rebuilt in the same deploy. No
> live run from before this step until the new agent is up (spec §12).

> **Deploy order for `0055_assessment_run_thread_unique` — step 4 of the Phase 2
> sequence, after `0054` and the merge script, with NO live run.** `0055` adds
> `uq_opportunity_assessments_run_thread UNIQUE (simulation_run_id, thread_id)`.
> NULL threads never conflict. The upgrade refuses while the heartbeat is fresh
> with state `running`/`stopping`/`starting` (a backstop only: confirm with
> `/admin/simulation` AND `docker ps` that no engine is up) and refuses, listing
> them, while any duplicate `(run, thread)` group remains — run
> `scripts/migrate/merge_duplicate_assessments.py` first. *Old code against the
> new schema is NOT safe*: the old engine's supersede inserts a second row for
> the thread before it deletes the first, which this constraint rejects, so the
> old agent must not run once `0055` is applied. The new agent's
> `ON CONFLICT (simulation_run_id, thread_id)` requires the constraint, so the
> new agent must not run before it; it refuses to start without it. Downgrade
> drops the constraint only.

> **Phase 2 sequence (`0054` → merge → `0055`).** No live run from before step 1 until step
> 6 has finished: an old engine against the new constraint, or a new web app against an
> engine with no lock, is unsafe. Before starting, confirm the production `.env` does not
> set `max_thread_messages` to anything but 12 and that
> `SELECT DISTINCT role FROM agents` returns only roles in `ROLE_CAPABILITIES`
> (the table is `agents`; `scripts/migrate/preflight.py` checks it, and BLOCKs on a role
> whose `role.toml` fails strict validation too).
>
> 1. `pg_dump` of `copi` (`run_migration.sh` only dumps at `--apply`), and confirm no live
>    run on `/admin/simulation` AND with `docker ps`.
> 2. `./scripts/migrate/run_migration.sh --target 0054`: rehearse, then `--apply`.
> 3. `scripts/migrate/merge_duplicate_assessments.py --database-url ...` (dry run by
>    default), then again with `--apply`, which holds the engine lock for the whole run
>    and is refused (exit 75) while anything else holds it.
> 4. `./scripts/migrate/run_migration.sh`: rehearse, then `--apply`, for `0055`. Its
>    live-run precheck is a backstop; the confirmation in step 1 is the control.
> 5. `$DC up -d blackbird-app worker`.
> 6. `$DC up -d agent`.

> **Deploy order for `0056_phase3_constraints_and_indexes`.** Step 0: `pg_dump` `copi` explicitly (§12; `run_migration.sh` dumps only at `--apply`, after the remediations). Run `scripts/migrate/remediate_0056.py --jobs --provisions --publications --emails` (dry run) from a one-off container off the NEW image; apply `--jobs --provisions` if they list rows (`--jobs` leaves a `processing` row alone unless `--worker-stopped` is passed, because the pre-Phase-3 worker still running never takes the worker lock: stop the worker first, then add `--worker-stopped`); the publications dedupe runs only with the owner's go-ahead (it changes the affected PIs' `## Recent Publications` at their next export); email case duplicates are resolved by hand. Then idle the worker (no `processing` job, or `$DC stop worker`) and confirm no live run on `/admin/simulation` — the migration builds `ix_agent_messages_agent_phase` inside the one transaction and preflight sizes it. Rehearse `./scripts/migrate/run_migration.sh`, then `--apply`. **Old code on the new schema:** safe; a racing second enqueue of a per-user job now raises instead of duplicating. **New code on the old schema:** unsafe — `Job.priority` is mapped, every `select(Job)` raises. **Agent image:** rebuild (the engine writes `rubric_documents` at start and imports the Phase 3 registries). **Downgrade** drops everything `0056` added (`rubric_documents` and its rows, the six indexes and constraints, `jobs.priority`).

> **Deploy order for `0057_email_verification_and_session_epoch` — migrate BEFORE the
> new code serves, with the WORKER IDLE; then web and worker; the AGENT image last and
> only with no live run. Everyone is signed out once.** `0057` adds
> `users.email_verified_at` (timestamptz, NULL) and `users.session_epoch` (integer, NULL)
> and, by owner decision D8 — an explicit override of "NULL is not backfilled" — stamps
> `email_verified_at = now()` on every user that has an `email` when it is applied.
> *Old code on the new schema* is safe: nothing old reads either column; an address
> changed through the old web image after `--apply` keeps its stamp, so bring the new web
> image up straight after. *New code on the old schema* is not: `User` maps both
> columns, so every `select(User)` raises `UndefinedColumn` — every signed-in page, the
> worker and the engine. Design: `docs/specs/2026-10-01-web-ui-remediation-design.md`
> §6.5-§6.7.
>
> **The worker must be idle** (no `processing` row) when `--apply` runs: `ALTER TABLE
> users` queues behind any open transaction that has read `users`, the worker holds its
> transaction across a whole pipeline run, and the chain's 10 s `lock_timeout` then rolls
> the chain back. Check with
> `$DC exec -T postgres psql -U copi -d copi -c "select count(*) from jobs where status='processing'"`
> (must print 0), or `$DC stop worker` for the migration.
>
> **Everyone is signed out once.** The new web image names the session cookie
> `__Host-copi-session` (production runs `ALLOW_HTTP_SESSIONS=false`) and keeps
> impersonation inside the signed session, so the old `copi-session` and
> `copi-impersonate` cookies are ignored from `up -d blackbird-app` on and expire on their
> own. **Agent image:** rebuild in the same deploy — the engine's Slack poller now
> mirrors only messages from known agent identities (A-02b, `src/agent/engine/slack_io.py`).
>
>     DC="docker compose -f docker-compose.prod.yml"
>     for s in blackbird-app worker agent; do
>       docker image tag copi-blackbird-$s:latest copi-blackbird-$s:rollback-pre-webui-1
>     done
>     $DC build blackbird-app worker
>     $DC --profile agent build agent
>     ./scripts/migrate/run_migration.sh              # rehearse (writes nothing)
>     ./scripts/migrate/run_migration.sh --apply      # dump → preflight → apply → postflight
>     $DC run --rm blackbird-app alembic current      # must equal `alembic heads` (0057)
>     $DC up -d blackbird-app worker
>     $DC up -d agent                                 # ONLY when /admin/simulation shows no live run
>
> No prompt or rubric file changes in this deploy, so no fresh-run requirement.
> Rollback: redeploy the `rollback-pre-webui-1` images; the columns are harmless to old
> code, and rolling the web image back signs everyone out again (the cookie name
> reverts). `alembic downgrade 0056` drops both columns and every verification stamp; a
> later re-upgrade re-stamps only the addresses present then.

> **Deploy order for `0058_hub_1_10_gate_reasons_and_pi_companies` — migrate BEFORE the
> new code serves, with the WORKER IDLE and no live run; then web and worker in one
> `up -d`; the AGENT image last, only with no live run; the next run starts FRESH
> (scout_hub 1.10.0).** `0058` adds `opportunity_assessments.gating_rationales` (JSONB,
> NULL: the hub's one-sentence reason per gate, never backfilled), the `pi_companies`
> table (the staff Companies list per PI, `src/models/pi_company.py`), and the
> `company_discovery` value of `job_type_enum`, and rebuilds
> `uq_jobs_one_active_per_user_type` under the same name with `company_discovery` in its
> predicate (preflight lists that index under `PLANNED_RECREATES`, so its existence is not
> a collision, and sizes `jobs` for it). Design: the 2026-10-02 hub 1.10.0 spec, §5.1 and
> §7.
>
> *Old code on the new schema* is safe until the first `company_discovery` job row
> exists: nothing old reads the column or the table, but the old `Job` model cannot load
> the new enum value, so an old worker's claim, or an old jobs or PI page, raises
> `LookupError` on such a row. Only new code writes one (the Find companies button, a PI's
> first profile generation, `scripts/enqueue_company_discovery.py`), so bring
> `blackbird-app` and `worker` up together and run the enqueue script only after both are
> new. *New code on the old schema* is not safe: `OpportunityAssessment` maps
> `gating_rationales`, so every `select(OpportunityAssessment)` raises `UndefinedColumn`
> (both assessment lists and detail pages, the review bot on the worker), and the
> engine's best-effort verdict write loses every verdict to one ERROR line;
> `/manager/pis/{id}` and every profile export (which now rewrites the companies file,
> `src/services/pi_companies.py`) select `pi_companies` (`UndefinedTable`). **Agent
> image:** rebuild in the same deploy; it bakes the model, the engine's
> `gating_rationales` write and `retrieve_profile`'s hub-only companies appendix.
>
> **The worker must be idle** (no `processing` row) and `/admin/simulation` must show no
> live run when `--apply` runs: the index rebuild takes ACCESS EXCLUSIVE on `jobs` and the
> new column takes it on `opportunity_assessments`, and the chain's 10 s `lock_timeout`
> rolls everything back behind a worker transaction. Check with
> `$DC exec -T postgres psql -U copi -d copi -c "select count(*) from jobs where status='processing'"`
> (must print 0), or `$DC stop worker` for the migration. Commit before building.
>
>     DC="docker compose -f docker-compose.prod.yml"
>     for s in blackbird-app worker agent; do
>       docker image tag copi-blackbird-$s:latest copi-blackbird-$s:rollback-pre-0058
>     done
>     $DC build blackbird-app worker
>     $DC --profile agent build agent
>     ./scripts/migrate/run_migration.sh              # rehearse (writes nothing)
>     ./scripts/migrate/run_migration.sh --apply      # dump → preflight → apply → postflight
>     $DC run --rm blackbird-app alembic current      # must equal `alembic heads` (0058)
>     $DC up -d blackbird-app worker
>     $DC up -d agent                                 # ONLY when /admin/simulation shows no live run
>
> Then: set `SEC_USER_AGENT` in `.env` (the contact address is the owner's to choose) and
> recreate the services that read it (`$DC up -d --force-recreate worker`, and
> `blackbird-app` if it reads the setting); run `scripts/enqueue_company_discovery.py` as a
> dry run, then with `--apply`; managers work the Suggested lists on `/manager/pis/{id}`.
> The prompt-set bump to scout_hub 1.10.0 means the next run starts FRESH from
> `/admin/simulation`, best after the Suggested lists are reviewed. Nothing here starts a
> run.
>
> Rollback: redeploy the `rollback-pre-0058` images only after
> `DELETE FROM jobs WHERE type::text = 'company_discovery'` (the old code cannot load those
> rows); the column and the table are harmless to it. `alembic downgrade 0057` drops the
> column, `pi_companies` and every company row, and restores 0056's predicate; the enum
> value stays (Postgres has no DROP VALUE), as `0039`'s and `0047`'s do.

> **Deploy order for `0059_assessment_chat_suggestions` — migrate BEFORE the new web app
> and worker serve, with the WORKER IDLE; then web and worker in one `up -d`; no agent
> rebuild.** `0059` adds `assessment_chat_suggestions` (the chat drawer's generated
> opening questions per assessment, tier and verdict revision; CASCADE with the
> assessment), `assessment_chat_opens` (one content-free row per drawer opening; both
> foreign keys SET NULL) and `assessment_chat_usage.question_origin` (varchar, NULL before
> `0059`, never backfilled). Design: `docs/operations/assessment-chat.md`, "Opening
> questions".
>
> *Old code on the new schema* is safe: nothing old reads either table or the column.
> *New code on the old schema* is not: `AssessmentChatUsage` maps `question_origin`, so
> every chat question and every ledger read raises `UndefinedColumn`, and both assessment
> detail pages select `assessment_chat_suggestions` (`UndefinedTable`, a 500 on every
> detail page). **Agent image:** no rebuild — the engine reads none of the three, and
> nothing under `src/agent/` changed.
>
> **The worker must be idle** (no `processing` row) when `--apply` runs: the new foreign
> keys lock `users` and `opportunity_assessments` briefly, the worker holds its
> transaction across a whole pipeline run, and the chain's 10 s `lock_timeout` then rolls
> the chain back. Check with
> `$DC exec -T postgres psql -U copi -d copi -c "select count(*) from jobs where status='processing'"`
> (must print 0), or `$DC stop worker` for the migration. Commit before building.
>
>     DC="docker compose -f docker-compose.prod.yml"
>     for s in blackbird-app worker; do
>       docker image tag copi-blackbird-$s:latest copi-blackbird-$s:rollback-pre-0059
>     done
>     $DC build blackbird-app worker
>     ./scripts/migrate/run_migration.sh              # rehearse (writes nothing)
>     ./scripts/migrate/run_migration.sh --apply      # dump → preflight → apply → postflight
>     $DC run --rm blackbird-app alembic current      # must equal `alembic heads` (0059)
>     $DC up -d blackbird-app worker
>
> **The new worker starts spending on its own:** while idle it generates a set for every
> assessment, newest first, about $0.50 each (`claude-opus-5`), within
> `ASSESSMENT_CHAT_SUGGESTIONS_DAILY_USD_LIMIT` (default $30 a rolling day). To hold it,
> set `ASSESSMENT_CHAT_SUGGESTIONS_ENABLED=false` in `.env` before the worker comes up.
> Nothing here touches the prompt set or the rubric, so no fresh-run requirement, and
> nothing starts a run.
>
> Rollback: redeploy the `rollback-pre-0059` images; the tables and the column are
> harmless to them. `alembic downgrade 0058` drops both tables, every row in them and the
> column.

> **Deploy order for `0060_grant_identity_orcid_fundings_job_reruns` — migrate BEFORE the
> new code serves, with the WORKER STOPPED; then web and worker; then the agent (no live
> run); then the grants repair.** `0060` adds `pi_grant_identity` (one row per PI: RePORTER
> identity status, accepted and pinned profile ids, candidates, the staff `none_confirmed`
> flag, `evaluated_at`, `orcid_fetched_at`), `pi_orcid_fundings` (one row per ORCID
> funding group, unique `(user_id, group_key)`), `pi_grants.vetoed_by_user_id`,
> `jobs.rerun_requested_at` / `jobs.rerun_not_before` and `users.name_sanitized_at` (when an
> ORCID- or OAuth-sourced name was cut to the allowed characters; the manager PI page
> flags it). Design:
> `docs/specs/2026-10-05-pi-profile-remediation-design.md` §6.1, §4.2.
>
> *Old code on the new schema* is safe: it reads none of it. *New code on the old schema*
> is not: `Job` maps the rerun columns (the worker's claim and every jobs page raise
> `UndefinedColumn`), `User` maps `name_sanitized_at` (every page), and every persona export
> selects the two new tables. **Agent image:**
> rebuild — the engine imports `src.models`.
>
> **Stop the worker** for `--apply` (`$DC stop worker`, after no row is `processing`): the
> chain's 10 s `lock_timeout` otherwise collides with the worker's job-long transaction
> and its idle-loop sweeps. Commit before building.
>
>     DC="docker compose -f docker-compose.prod.yml"
>     for s in blackbird-app worker agent; do
>       docker image tag copi-blackbird-$s:latest copi-blackbird-$s:rollback-pre-0060
>     done
>     $DC build blackbird-app worker
>     $DC --profile agent build agent
>     $DC stop worker
>     ./scripts/migrate/run_migration.sh              # rehearse (writes nothing)
>     ./scripts/migrate/run_migration.sh --apply      # dump → preflight → apply → postflight
>     $DC run --rm blackbird-app alembic current      # must equal `alembic heads` (0060)
>     $DC up -d blackbird-app worker
>     $DC up -d agent                                 # ONLY when /admin/simulation shows no live run
>
> Then run the grants repair at once (`scripts/grants_remediation.py`: `--dry-run`, then
> `--apply`, then `--verify`): until it runs, an export renders no RePORTER grants (no
> identity rows yet) and no ORCID items (no fundings yet). The repair sets the app setting
> `persona_sweep_enabled` last. Nothing here starts a run.
>
> Rollback: redeploy the `rollback-pre-0060` images; the schema is harmless to them, but
> old code re-exports from `grant_titles` and brings the misattribution back, so this is a
> last resort. `alembic downgrade 0059`, if wanted, runs from a one-off container off the
> NEW image before the images are restored, and drops both tables and the four columns.
