# Landscaping pipeline

Reference detail behind the rules in the root `CLAUDE.md`, which keeps only what
every session needs. Dated statements hold as of the date they give; re-measure a
count or a line number before relying on it.

The landscaping pipeline reads the research output of Johns Hopkins entities in Baltimore,
runs it through the S1-S9 funnel and issues the resulting opportunities to reviewers in
weekly batches on `/workspace/landscape`. It runs in its own process,
`python -m src.landscaping.worker` (`src/landscaping/worker.py`), in its own compose
service, `landscape-worker`, and keeps all of its state in the `landscape_*` tables of
migrations `0077` and `0078`. It never uses the `jobs` table, so the ordinary worker is not
involved. Design: `docs/specs/2026-10-30-blackbird-landscaping-design.md`.

Every rule of the root `CLAUDE.md` applies here: `DC="docker compose -f docker-compose.prod.yml"`,
never `--remove-orphans`, never `up -d --build`, check a container's compose project label
before touching it, and **never start a simulation run** as part of any step below.

## 1. The working-tree compose service

The service lives only in the working tree's `docker-compose.prod.yml`, which stays
uncommitted by decision (D5 of `docs/plans/2026-09-25-rca-remediation-plan.md`, spec D22),
like the `blackbird-app` rename and the agent's supervisor command. Add this block under
`services:`, next to `worker`:

```yaml
  landscape-worker:
    build: .
    profiles: [landscape]
    container_name: copi-blackbird-landscape-1
    restart: unless-stopped
    # Longer than the LLM client's 300 s read timeout, so a call in flight can finish.
    stop_grace_period: 330s
    command: ["python", "-m", "src.landscaping.worker"]
    env_file: .env
    environment:
      DATABASE_URL: postgresql+asyncpg://${POSTGRES_USER:-copi}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB:-copi}
      SECRET_KEY: ${SECRET_KEY:?Set a strong random SECRET_KEY in .env}
      ENVIRONMENT: ${ENVIRONMENT:-production}
    volumes:
      - ./prompts:/app/prompts:ro
    networks:
      - default
    depends_on:
      postgres:
        condition: service_healthy
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"
    # A starting value: set it from the pilot's measured peak (spec §12.5).
    mem_limit: 2g
```

- `profiles: [landscape]` keeps a plain `$DC up -d` from starting it; every command that
  names it passes `--profile landscape`.
- Only the `default` network: it serves no web traffic, so it never joins `copi-edge`, and
  no alias can collide with org1's services.
- `prompts/` is mounted read-only. The worker reads the rubric, the BlackbirdBot extracts,
  the landscaping prompt set and `prompts/landscaping/criteria.toml` once, at start, and
  validates them; an edit there applies at the next restart.
- The prompts' review document is `docs/specs/2026-11-20-landscaping-prompts.md`, with a Word
  export beside it: every file in `prompts/landscaping/`, the code strings, and how to change
  them. After an edit, stamp the set, run `scripts/sync_prompt_set_docs.py` and export again.
- Before touching the container, check it is this repo's:
  `docker inspect copi-blackbird-landscape-1 --format '{{index .Config.Labels "com.docker.compose.project"}}'`
  must print `copi-blackbird`.

**A refused start.** The worker refuses to start, and exits with status 1, when migration
`0077` is not applied, when another landscaping worker holds its lock, or when the rubric,
the extracts, the prompt set or the criteria file fails validation. With
`restart: unless-stopped` it then restarts in a loop: read the reason in
`docker logs --tail 50 copi-blackbird-landscape-1` ("landscaping worker not started: …").

## 2. Deploy order

1. Commit. The porcelain check of the root `CLAUDE.md` must print nothing.
2. Build only:

       $DC build blackbird-app worker
       $DC --profile landscape build landscape-worker

3. Migrate from a one-off container off the new images, rehearsing first, then applying
   (`0077` and `0078`):

       ./scripts/migrate/run_migration.sh              # rehearse (writes nothing)
       ./scripts/migrate/run_migration.sh --apply

   New code on the old schema breaks every chat request, assessment chat included, so both
   are applied before anything new serves (`docs/operations/migration-deploy-notes.md`).
4. Start the web tier on the migrated schema:

       $DC up -d blackbird-app worker

5. Start the landscaping worker:

       $DC --profile landscape up -d landscape-worker

6. Never `--remove-orphans` and never `up -d --build`. The agent is not recreated: the
   shared modules changed only additively, so the running agent needs no rebuild, and its
   next ordinary rebuild, recreated only when `/workspace/simulation` shows no live run,
   picks the additions up.

## 3. Phase 0, step by step

Phase 0 ends in a stored pilot report and an admin's go (spec §15). Each step needs the
owner's go-ahead.

1. **The worker starts disabled.** With `LANDSCAPE_ENABLED` unset (`landscape_enabled`
   defaults to false) it validates its inputs, creates the source rows and the first
   candidate epoch, then only polls: it submits nothing and fetches nothing (spec §9.5).
2. **Run the probes** in one-off containers off the new image, so the production keys are
   used without being read on a workstation. Each prints its finding as JSON and stores it
   for the pilot report; a failed probe exits 0, because the failure is itself a finding:

       for probe in odp-fields ortholog reporter-pi effort rate-limits; do
         $DC --profile landscape run --rm landscape-worker \
           python scripts/landscaping_probes.py "$probe"
       done

   The script is `scripts/landscaping_probes.py`. S6 and S9 truncation rates come from the
   ledger once the pilot has run.
3. **JHTV's terms of use.** A person reads them. Only if they allow automated reading does
   an operator enable the source:

       $DC --profile landscape run --rm landscape-worker \
         python scripts/landscaping_probes.py enable-source jhtv

4. **Positive controls.** Staff enter the identifiers (DOIs or PMIDs) of the papers behind
   Blackbird's past positive decisions on the pipeline page, `/workspace/landscape/pipeline`
   (spec §10.2). They are stored in the database, never in git. The pilot report says so
   when the list is empty.
5. **Enable the worker.** Set `LANDSCAPE_ENABLED=true` in `.env`, then recreate it:

       $DC --profile landscape up -d --force-recreate landscape-worker

   The pilot intake starts: every month of the window from every enabled source (the
   Scripps branch's whole-window pilot, `docs/specs/2026-11-20-scripps-landscaping-design.md`
   §6.3; PubMed and GEO in NCBI's off-peak hours), plus every positive-control document (D68).
6. **The bake-off** runs once the pilot documents are in, on samples of them, and stores a
   `bakeoff` report with its model picks. It seeds the regression set. When no Haiku or
   Sonnet candidate passes (D43), the first epoch's S2-S4 regression check fails the same
   way and holds the epoch: an admin overrides it, with a reason, on the pipeline page
   (spec §10.4).
7. **Apply the picks.** The bake-off report's `recommended_settings` maps each
   `LANDSCAPE_<STAGE>_MODEL` variable to the model it picked. Set each in `.env`, then
   recreate every service that reads it:

       $DC up -d --force-recreate blackbird-app worker
       $DC --profile landscape up -d --force-recreate landscape-worker

   The changed models make a new candidate epoch at the worker's start, which supersedes
   the first before any S2 work (D65). The pilot run (S2-S9 on the pilot documents) starts
   only once every setting equals its pick, and ends in the pilot report on the pipeline
   page. Nothing is issued to reviewers, and no epoch activates in the pilot (D70).
8. **Approve the reason tags** (L49). Once the owner approves the drafted list, an
   operator, in `prompts/landscaping/criteria.toml`:
   - sets `approved = true` under `[reason_tags]`;
   - bumps `[meta].version` and adds a changelog line;
   - records a new prompt-set version with
     `.venv-test/bin/python -m src.landscaping.prompt_set --stamp <new set version>`;
   - commits, and restarts the worker:

         $DC --profile landscape restart landscape-worker

   `[reason_tags]` is not a judging input, so this creates no epoch (D61). Until it is
   approved, the worker never issues a weekly batch.
9. **Start the backfill.** An admin presses **Start backfill** on the pipeline page. It is
   accepted only in phase `pilot` with a stored pilot report. The worker moves itself to
   `continuous` once the backfill has drained (D69); the first ready cut then activates
   the first epoch, and the first weekly batch is issued.

## 4. Operations

**Pause and resume.** Staff press **Pause** or **Resume** on the pipeline page; they set
`app_settings.landscape_paused`. The worker reads it each loop, as paused when its value,
lower-cased, is one of `true`, `1`, `yes` or `on`. While paused, and while disabled, it
starts no new work (no intake, no submissions, no synchronous calls) but keeps polling and
ingesting the batches it already submitted. `app_settings.landscape_phase` holds the phase
as plain text: `pilot`, `backfill` or `continuous`, and unset means `pilot`.

**A preview during the backfill** (D77). The cut, S6-S9 and the opportunities otherwise wait
for the backfill to drain. To see opportunities earlier, set the preview flag:

```sql
INSERT INTO app_settings (key, value) VALUES ('landscape_preview_cut', 'requested')
ON CONFLICT (key) DO UPDATE SET value = 'requested', updated_at = now();
```

The next loop takes one cut of the pool as triaged so far (the top 1%) and sets the value
to `taken`. S6, S7, S8, the labels and S9 then run on that cut, under the daily cap like any
other work, and their spend counts toward it (about $3 an idea for S6 and $0.60 an
opportunity for S9). No second cut is taken before the drain; set `requested` again for a
fresh one. Delete the row to stop the preview's work. The results are the candidate's
provisional groupings and assessments: the Landscape page does not show them, and they never
activate. The first cut after the drain replaces them, and it reuses S6 results and labels
where the ideas and groupings are unchanged.

**What S4 and S6 are sent** (D78, D79). The S4 dossier shows each Open Targets target cut
down: depMap per tissue (cell-line count, lowest, median and highest gene effect), the 10
furthest-advanced drug rows with the row count, the 10 highest-expression tissues with the row
count, and no homologues; the data flags name the depMap summary instead of repeating it. The
stored evidence and flags are unchanged, and so is what S6, S9 and the detail page show. S6
caches its analysis rounds and sends its consults one shared, cached record, with each persona
as a mid-conversation system message (supported on the S6 models, Opus 5 and its Opus 4.8
fallback; not on Sonnet 5, so do not move S6 there without changing this).

**A result that cannot be ingested** (D71). When a batch result's ingestion raises, the
worker ingests the rest of its chunk one result at a time and retries the failing one at
each poll. If other results of the batch ingest, then at its third failure, or after two
hours when the error looks like an outage, it records the result as `errored`: the row is
resubmitted once, then ends `failed:errored`. The log says `could not be ingested after N
tries: recorded as errored`, with the item and row; fix the cause before the resubmission
comes back. When no result of a batch ingests (an outage, or a fault in the ingester),
nothing is given up and the batch waits. A restart forgets the counts. A ChEMBL drug-name
lookup is the exception (D81): retried once, a 429, 5xx or network failure leaves the agent
unmatched instead of failing the result (log: `ChEMBL lookup of agent … failed; left
unmatched`), and the alias step looks it up again on a later day.

**When the backfill drains** (D69, D83). The worker moves itself to `continuous` once every
enabled source's backfill slices have ended and the candidate owes no S2-S4 work. A
`pending` S2 or S3 row that no stage would send is not owed: intake returns both rows to
`pending` when a document's summary changes, and the new record can then exclude the
document at S1 (PubMed's publication types often mark a review, comment or letter), S2 can
screen it out, or a merge can retire it. Such a row stays as it is and is sent if the
document qualifies again. If the phase stays `backfill` with no model calls and nothing in
flight, check what `epochs.funnel_work_pending` still counts.

**The alias re-check** (D82). Once a UTC day, every provisional idea is normalized again
(HGNC, ChEMBL, Open Targets) in case a facet now resolves. The lookups run in a background
task beside the loop, one idea at a time, so the first loops after 00:00 UTC make their model
calls as usual; with about 2,500 provisional ideas the check takes about an hour. The loop
stores each answer as it comes, so a restart is safe: what is stored stays, and the rest is
asked again. An idea whose lookups fail is logged (`re-checking idea … failed (…); it stays
due`) and tried again by the next check.

**Epoch overrides.** A candidate that fails its regression check is held. An admin can let
it go ahead from the pipeline page, with a reason; the override is recorded with the admin
and the reason. An active or retired epoch cannot be overridden.

**The S3 check** (D74). S3 passes the bake-off and the regression gate when its agreement with
the Opus 5 references, by the targets and diseases named, is at least 90% of Opus 5's own
agreement with them when asked again. So every bake-off and every regression run that touches
S3 asks Opus 5 the S3 items a second time: about 60 calls, about $4 at the synchronous rate.
The stage summary shows `reference_agreement`, `agreement_bar` and target and disease recall
and precision. Measured 2026-11-17 with the D73 fields, Sonnet 5.5 reached about 0.80-0.84 of
Opus 5's agreement with itself, so an S3 change on Sonnet 5.5 is held until an admin
overrides it or the S3 model changes.

**Rescue.** Staff can rescue an item a stage excluded (screen, triage, or the deep gate),
from the pipeline page, with a reason. It re-enters at the next stage and competes
normally. A rescue is refused when the item's status changed since the page loaded; reload
and check it. Rescues carry forward into later epochs and join the regression set.

**Reading the metrics.** The pipeline page shows, for the active epoch: counts per stage,
status, source and institution; refusal rates; spend per day and stage, and the cost per
useful opportunity; per-source completeness and incomplete slices; quote-verification and
normalization rates; reject-audit disagreements; regression runs per epoch; blind-batch
results (only for blind batches the next batch has superseded); expert agreement; lab-name
collisions; and the latest cut. The bake-off, pilot, probe, positive-control and drift
reports are stored on the same page. The ledger, `landscape_llm_calls`, is the only cost
record for landscaping: `llm_call_logs` does not see it.

**The Landscape pages** (spec D84-D94). The pipeline page is staff only. It prints the latest
cut as a sentence ("Latest cut: N ideas ranked, K kept, M opportunities", with its time). An
epoch an admin let through after a hold reads "Held, then overridden", with the hold and
override reasons folded under "Reasons". "Completeness (last slice)" is the completeness of
each source's most recent intake slice, not of the whole source; a slice that received more
than it expected prints as complete, and intake itself counts as before. Each reject-audit row
carries its item's title. On an opportunity's page, each History row opens that earlier
assessment read-only (`/workspace/landscape/<id>?assessment=<assessment id>`): its own
sections only, with no forms, no chat, no deal-breakers and no member ideas. Only an id the
History list shows is accepted; any other is a 404. The Verdict of the current assessment
says, in one line with no name or reason, when the active epoch went into use by an override.

**Synchronous work and the simulation.** The worker makes its synchronous calls (S6, S7,
similarity judgments, fallback chains) only while no simulation run is live, so a live run
delays them. Do not stop or start a run for it.

**Sync batch mode.** This organization's Anthropic API key is HIPAA-configured, and the
Message Batches endpoint refuses it ("not available for HIPAA-configured organizations",
2026-11-16). With `LANDSCAPE_BATCH_MODE=sync` in `.env`, each batch item is sent as its own
Messages call. The worker's poll runs them in chunks of 50, `LANDSCAPE_SYNC_CONCURRENCY` at a
time (default 4) per stage: each stage's batches run in a lane of their own, beside the other
stages' (D75), so up to four times that many calls can be in flight at once. A poll starts no
new call after 15 minutes and returns, so the loop's submissions run, and the next poll
resumes where it stopped. Before each chunk it checks again that it is enabled, unpaused, under the cap
and that no simulation run is live, so a local batch waits while a run is live. A shutdown
starts no new call. A rate-limit, overload or connection error leaves the rest for the next
poll (`LANDSCAPE_BATCH_POLL_SECONDS`, default 300). Each call is ledgered as soon as it
answers, at the full synchronous rate, about twice the batch rate. The bake-off report's cost
figures are at the batch rate. Everything else (stage rows, ingestion, refusal fallbacks)
works as in batch mode. An item whose row has left it since submission (a re-screened
document's S3 row, reset and batched again) is not sent: it is answered `canceled`, with no
ledger row (D78). A restart in the middle of a local batch sends its unanswered items
back to `pending`, where their stage batches them again. Answers paid for but not yet
ingested are lost then, at most one chunk per stage. Switch modes only while no API batch is in flight.
Fetches run beside the poll, one background task per source, so a fetch never pauses the
calls; evidence assembly and S3 normalization take four items at a time, each outside
service at its own pace (D76). A shutdown lets a running fetch finish its slice.
`src/landscaping/batches.py` documents the protocol.

**The kill switch.** Set `LANDSCAPE_ENABLED=false` in `.env`, then recreate:

    $DC --profile landscape up -d --force-recreate landscape-worker

The worker finishes the HTTP calls in flight, submits nothing new and only polls the
batches already submitted. To stop it altogether, stop it gracefully (it finishes its
current unit of work within the grace period); never `docker rm -f`:

    $DC --profile landscape stop landscape-worker
