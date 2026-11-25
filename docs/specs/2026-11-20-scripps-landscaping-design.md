# Landscaping test run for Scripps Research (La Jolla) — design

*Branch `scripps-landscaping`, from `blackbird` at 9ab7a021, pushed to `origin`. Written
2026-11-20. Every external figure below was read from the live source that day (§12); every code
reference is to 9ab7a021. Costs are in the companion
[cost estimate](2026-11-20-scripps-landscaping-cost-estimate.md).*

## 1. Intent

The landscaping pipeline reads an institution's papers, grants, patents and datasets and turns
them into ranked funding opportunities with written assessments. Today it reads only Johns
Hopkins (Baltimore). The owner wants to know **how well it works for Scripps Research**. The
owner and one remote colleague will judge that by reading the opportunities it produces, on a
website they can both reach.

This is a test run on a throwaway branch, not a product change. Success is a complete,
reviewable Scripps landscape and a clear sense of its quality, at a bounded cost, with nothing in
production touched.

## 2. Decisions

Made by the owner:

- **Scope: Scripps Research, La Jolla only.** Work from the Jupiter, Florida campus is left out,
  as are UF Scripps, Scripps Health and its hospitals, Scripps Institution of Oceanography and
  Scripps College.
- **Calibr is excluded for now.** It is the California Institute for Biomedical Research
  (Calibr-Skaggs, OpenAlex `I4210140527`).
- **Time window: the past year** (`LANDSCAPE_WINDOW_DAYS=365`). Hopkins uses three.
- **Cost:** a per-stage estimate from the Hopkins run was wanted before the run. It is in
  [`2026-11-20-scripps-landscaping-cost-estimate.md`](2026-11-20-scripps-landscaping-cost-estimate.md),
  for both a 1-year and a 6-month window.
- **Opportunities: start with a 20% cut** (`LANDSCAPE_CUT_FRACTION=0.20`). That gives K ≈ 24 and
  about 20 opportunities for the 1-year window.
- **Where it runs:** the pipeline runs on the owner's workstation. When it has finished, the
  database is copied to a small cloud server that serves the website to both reviewers. The
  owner sets up that server manually, later (§7 is the checklist).
- **Keys:** a new Anthropic API key for this run. The NCBI, OpenAlex and USPTO keys are the ones
  production uses.
- **No known positives** are loaded for now.
- **The branch is pushed to `origin`.** Work continues in a clone on the owner's workstation.

Made by the designer (each can be overruled):

- **C1. Hard-code Scripps on the branch** rather than make the institution configurable (§5).
- **C2. The pilot fetches the whole one-year window**, not one sampled month per calendar year
  (§6.3).
- **C3. When a record's campus cannot be told, it counts.** Patents, and any paper, dataset or
  grant with no campus in its address, are kept. La Jolla is the main campus.
- **C4. A $100 daily spending cap** (`LANDSCAPE_DAILY_USD_CAP=100`); the code's default is none.
- **C5. The tech-transfer listing source stays off**, as it starts (`intake._DISABLED_AT_START`).
  Scripps has a comparable feed (§12), but Scripps Research's terms forbid crawling and
  scraping without written consent. The feed also holds only 38 items, none newer than June
  2026, and names no inventors. The owner chose on 2026-11-20 to leave it off for this test and
  not to ask the office for permission now. Turning it on later needs two code changes:
  - point `jhtv.RSS_URL` at `https://scrippsotd.technologypublisher.com/rss.aspx`;
  - give `jhtv._make_client` an explicit, non-httpx `User-Agent`.
- **C6. NCBI paced at 1 request a second** locally (`LANDSCAPE_NCBI_MAX_RPS=1`), because the key
  is shared with production.
- **C7. The remote colleague gets the `manager` role**; the owner is `admin`. Both are staff and
  see every opportunity. A `reviewer` would see only opportunities in an issued batch
  (`landscape_view.can_view`), at most 10 a batch (`review_batches.BATCH_SIZE`), and with no
  worker on the server (C8) no second batch is ever issued: about half of the ~20. A manager also
  skips onboarding (`auth.py:336-344`).
- **C8. The server runs the website only**, with no pipeline worker: the first weekly review
  batch, issued locally, travels in the database copy.
- **C9. Who decides during the run.**
  - The owner reads the 40 probe addresses (§8) before the run starts. The run waits for this.
  - The builder:
    - applies the bake-off's model picks;
    - disables a source that cannot be fetched;
    - overrides a held first epoch, so the run reaches cards.

    Each action goes in the run log with its reason. The picks may differ from production's
    current models (production set S3 to Opus 5 by hand after its RCA); the run uses the picks,
    because the pilot funnel opens only when the settings equal them (`worker.picks_applied`). A held epoch is itself a result of the
    test, so its regression report is kept and summarized for the owner. Local admin actions run
    through `scripts/scripps_admin.py`, with the owner's ORCID iD as the actor (§6.6).
- **C10. Only the landscaping pages are reworded.** The rest of the site still names Hopkins in
  places the two accounts can reach: the PI help pages, other glossary entries, and the PI steps
  of the tutorials. That text is left as it is. The reviewers are told to read only the
  landscape pages.
- **C11. A checkpoint after the bake-off.** The projection is redone from the real document count
  and the bake-off's real spend. The run continues on its own when the new projection is within
  25% of §6.7's figure for the chosen cut; otherwise it waits for the owner. For the 1-year
  window the projection is P = B + $31.45 + $118.30 × E ÷ 557, where B is the bake-off's actual
  spend and E the actual eligible-document count (cost estimate §5: the regression check, the
  repeatability run and the reject audit are fixed; S2–S9 scale with E). The run goes on alone
  when $151.73 ≤ P ≤ $252.88, 25% either side of the $202.30 estimate. A low projection also
  waits, since it can mean lost documents.

## 3. Goals, non-goals, anti-goals

**Goals**

1. The branch qualifies documents for Scripps Research La Jolla, and only those.
2. Nothing the model reads, and nothing on the landscaping pages or their help pages, says Johns
   Hopkins, JHU or Baltimore (C10).
3. A full run on the workstation: pilot, model bake-off, every stage S2–S9, an active epoch,
   opportunity cards and the first weekly review batch.
4. A database dump and a server checklist the owner can follow alone.

**Non-goals**

- Merging into `blackbird`.
- A configurable or multi-institution pipeline.
- The rest of the app for Scripps: PI profiles, BlackbirdBot, interviews, projects, simulation.
- Jupiter, UF Scripps, the tech-transfer feed, non-US sources.
- Re-tuning prompts, criteria, the rubric or thresholds for Scripps. The point is to see how
  the Hopkins-tuned pipeline does as it stands.

**Anti-goals** — the run fails, however well the pages look, if:

- anything in production changes: its database, containers, host, checkout or `.env`;
- the test exhausts production's shared NCBI or OpenAlex quota;
- bare "Scripps" pulls in Scripps Health, Scripps Clinic, Oceanography, Scripps College or UF
  Scripps;
- spend runs uncapped.

## 4. Constraints

- `origin` (`SuLab/coPI.science`) is **public**, and this branch is pushed there. Nothing secret or
  personal goes into it: no key values, no `.env.scripps`, no database dumps, and no logs that
  hold people's names. Check each diff before pushing.
- The production checkout `~/blackbird-copi-science` stays on `blackbird`, and its uncommitted
  `docker-compose.prod.yml` is never committed, stashed or restored.
- The image build copies the build context and runs `git clean -ffdx` (`Dockerfile:12-21`), so
  only committed files reach the image: build from a clean clone of the branch.
- The production host (2 vCPU, 3 GB RAM, about 1 GB free) and the other site that shares it are
  out of bounds.
- Workstation: 48 cores, 130 GB RAM, Docker 29.9 with Compose, **21 GB free on `/`** (2026-11-20).
- All work happens in the workstation clone (owner, 2026-11-20). Tests run from a `.venv-test`
  made in that clone with Python 3.12 and the host test venv's package versions as constraints,
  so the suite resolves what the host's does. One full `scripts/ci.sh`, once, before the local
  build; otherwise only the affected test files.

## 5. Approaches considered

**Hosting.** (1) *Chosen by the owner:* run locally, then copy the database to a small cloud
server that serves the site. (2) Keep everything on the workstation behind a tunnel: no copy, but
the workstation must stay on and be exposed. (3) A second stack on the production host: ruled
out on memory and on the shared host.

**Code.** (a) *Chosen (C1):* replace the Hopkins constants and matchers on the branch. Smallest
change; the branch is throwaway. (b) A configurable "institution profile" read by every source
module. Right if a third institution follows or this merges back; more code and tests than a test
run justifies. (c) Several institutions in one database: unsupported. There is no institution
column, one worker lock (`advisory_locks.LANDSCAPE_WORKER_LOCK_KEY`), and the epochs, cuts and
review batches are global.

**Pilot.** (i) *Chosen (C2):* the pilot fetches the whole window. (ii) The stock pilot, one
random month per calendar year (`intake.pilot_slices`), would take two months of a year that
holds only about 560 eligible documents (§6.7). That is under 100 documents, short of the
bake-off's samples of 600 documents, 60 extractions and 60 ideas (`bakeoff.py:866`). An empty
regression kind fails and holds the first epoch (`regression.py:96-101`). The whole window
roughly fills the samples; with fewer documents, the sampler takes them all
(`bakeoff.stratified_sample`). (iii) Skip the bake-off and copy production's models: the pilot funnel
waits for a bake-off report's picks (`worker.picks_applied`), and the first epoch's regression
waits for the set the bake-off seeds (`worker._begin_rejudge`), so skipping means bypassing two
gates.

## 6. Design

### 6.1 What counts as Scripps Research La Jolla

| Source | Today (Hopkins) | Scripps Research La Jolla |
|---|---|---|
| OpenAlex institution | 21 Baltimore ids, `institutions.BALTIMORE_OPENALEX_IDS` | `I123431417` only (no child institutions) |
| OpenAlex query roots | JHU lineage list, `openalex_works.py:14-19` | `authorships.institutions.lineage:I123431417` |
| ROR (DataCite) | 4 ids, `institutions.ROR_IDS` | `02dxx6824` |
| NIH RePORTER org | `institutions.REPORTER_ORGS` | `SCRIPPS RESEARCH INSTITUTE, THE` |
| NSF awardee | `institutions.NSF_AWARDEE` | `The Scripps Research Institute` |
| PubMed affiliation term | `pubmed_works.AFFILIATION_TERM` | `"Scripps Research"[ad]`: 787 records in the window (2026-11-20) |
| DataCite / GEO names | `datacite.py:21-27`, `geo.py:19-27` | "The Scripps Research Institute", "Scripps Research Institute", "Scripps Research" |
| USPTO assignee | `odp_patents._NAMES` | "Scripps Research" |
| Excluded place rule | APL cities, `odp_patents.APL_CITIES`, `qualify.py:77-78` | none: make it empty |

**The address matcher.** Add `is_scripps_la_jolla_affiliation(text)` in
`src/landscaping/institutions.py`. It replaces `is_baltimore_affiliation`. A string passes when it
names Scripps Research (the phrase "scripps research", which covers "The Scripps Research
Institute") **and** names neither Jupiter nor Florida (`Jupiter`, `Florida`, `FL 334…`, the
state code `FL` in capitals, and the UF Scripps Institute without its city: `UF Scripps`,
`Wertheim Scripps`) **nor** Calibr (`Calibr`, `California Institute for Biomedical Research`).
The state code and the UF Scripps names were added at owner gate 1 (2026-11-23): its audit of
every work in the window found Jupiter addresses written only as "The Wertheim UF Scripps
Research Institute", "UF Scripps Research Institute" or "… Scripps Research, La Jolla, FL".
"Wertheim" alone is not a Jupiter name (UC San Diego's school of public health bears it). Bare "Scripps" never passes,
and neither does `TSRI` alone: each of the 11 PubMed affiliations from 2023 to 2026 that say
TSRI without "Scripps Research" belongs to another organization (Taiwan Semiconductor Research
Institute, Taiwan Sugar Research Institute, The Saban Research Institute, Thailand Science
Research and Innovation). Do **not** edit `src/services/jhu_rules.is_hopkins_affiliation`:
PI profiles use it too (`corpus.py:169`, `non_pubmed_works.py:198`,
`industry_sources/openalex_industry.py:92`, the tenure rules in `jhu_rules.py`).

**The OpenAlex rule is the one real logic change.** OpenAlex tags Jupiter work with the same id,
`I123431417`. For example, a 2025 authorship lists `I123431417` on "The Scripps Research
Institute, Jupiter, United States". Today `authorship_roles.at_institution` accepts any authorship
listing a matching id and reads raw strings only when no institution is listed (`raw_strings=
"only_when_no_institutions"`). For Scripps, `qualifies_openalex` must use OpenAlex's per-address
`affiliations` list, which pairs each raw string with its institution ids:

- An authorship counts when **at least one** of its `affiliations` entries lists `I123431417` and
  that entry's raw string names neither Jupiter, Florida nor Calibr. An author with both campuses
  counts, and so does a Calibr author with a separate Scripps Research La Jolla address. An
  entry tagged only `I4210140527` (Calibr) never counts.
- An authorship listing `I123431417` none of whose `affiliations` entries carries
  `I123431417` counts (C3), unless its raw strings that name Scripps (all its raw strings when
  none does) are non-empty and every one names Jupiter, Florida or Calibr. So a Jupiter
  Scripps address beside another institution's address does not count (adversarial audit,
  2026-11-20). The cost: a dual-campus author whose La Jolla address lacks the word "Scripps"
  and whose only Scripps address is Jupiter does not count either; owner gate 1 looks for that
  among the dry probe's rejected addresses.
- An authorship listing other institutions but not `I123431417`, or no institution at all,
  counts when a raw string passes the matcher. Until owner gate 1 (2026-11-23) an authorship
  listing only other institutions never counted; the gate's audit found OpenAlex filing a
  "Scripps Research, La Jolla" address under Scripps Institution of Oceanography, and another
  under the author's Japanese institutes.
- The role rule is unchanged: corresponding or last author, an individual
  (`institutions._QUALIFYING_ROLES`, `authorship_roles.is_individual`).

Make the Jupiter test a small function used by the OpenAlex rule, rather than changing
`at_institution`'s contract, which other callers share. The list fetch returns `affiliations`
within `authorships` with the source's own `select` (checked live on 2026-11-20; each
`institution_ids` entry is a full `https://openalex.org/I…` URL). The stored `raw_qualifier`
(`openalex_works.py:111-123`) gains the qualifying authorship's `affiliations`, so the rule can
be re-checked from the row.

**Other sources.**

- *PubMed:* the last author's affiliations go through the new matcher (`pubmed_works.py:109`).
- *DataCite and GEO:* only the names and the ROR id change. Datasets are evidence only (D3):
  they never qualify and attach only to stored works through their DOIs and PMIDs, so they need
  no address filter.
- *RePORTER and NSF:* the names above. NIH lists no Scripps grants in Florida for fiscal 2023–2026,
  so there is nothing campus-related to filter.
- *Patents:* all Scripps Research assignments count (C3).

**Drift report.** Point the drift listing at Scripps (`reports.py:79-80`, `_hopkins_like`): names
matching "scripps research", lineage root `I123431417`. Set
`EXCLUDED_OPENALEX_IDS` to the known non-La Jolla or unrelated institutions so they are not
re-proposed:
- `I4387156289` (UF Scripps, Jupiter);
- `I4210140527` (Calibr);
- `I150209017` (Oceanography);
- `I1311914864` (Scripps Health) and its children `I4210146328`, `I2802091096`, `I4210107570`,
  `I4210103888`, `I4210142769` and `I4210146979`;
- `I4210109568`, `I16970960` and `I4210089022`.

All are listed in §12.

**Probes script.** `scripts/landscaping_probes.py:140` uses `nih_reporter.JHU_ORG_EXACT`. Point it
at the Scripps RePORTER name; leave `nih_reporter.JHU_ORG_EXACT` itself alone, since PI profiles
use it.

### 6.2 Wording

Rename what the **model** reads; these are code strings, not prompt files:

- "Independent JHU groups" in `stages/triage.py:119` and `stages/deep.py:579`. The bake-off uses
  `triage_dossier` too (`bakeoff.py:777`).
- The `jhu_group_count` key in S9's dossier (`stages/final.py:243,267`).
- "Johns Hopkins labs" in the chat record (`services/landscape_chat_record.py:102,365`).

Use "Scripps Research groups" and "Scripps Research labs". The database column `jhu_group_count`,
the `jhtv` document kind and other identifiers keep their names. They are never shown as words.

Rename what **reviewers** read:

- `templates/workspace/landscape.html:38`
- `templates/landscape/_member_idea.html:37`
- `templates/help/pages/landscape.html`
- `templates/help/pages/glossary.html` (the landscaping entry)
- `templates/help/tutorials/manager/11-landscape.html`
- `templates/help/tutorials/reviewer/09-landscape.html`
- `src/web/help_pages.py:85`

The chat legends feed the prompt review doc, so run `scripts/sync_prompt_set_docs.py` after the
change. No Word re-export is needed on this branch.

The files under `prompts/landscaping/`, the rubric, the BlackbirdBot extracts and the specialist
personas name no institution: they stay unchanged, and so does `prompts/landscaping/set.toml`.

### 6.3 The pilot covers the whole window (C2)

- `intake.pilot_slices` returns every month slice of the window, the list `backfill_slices`
  builds, keeping the tech-transfer entry, which is dropped anyway while that source is off.
- `intake.schedule_due`'s pilot branch applies the same NCBI off-peak filter the backfill branch
  applies (`intake.py:908-910`, rule K8). The pilot now holds large PubMed and GEO jobs.

What follows, all already in the code:

1. The bake-off samples from every eligible document (`bakeoff._pilot_documents`).
2. Once its picks are applied, S2–S9 run on all of them, then the S4 repeatability run and the
   pilot report.
3. "Start backfill" finds every slice already fetched, because the slice keys are the same, so
   the drain is immediate and the worker moves to `continuous` (`worker._backfill_drained`).
4. The candidate epoch (the one built from the picks; applying them retires the first) then
   activates once its post-drain cut has S6, S8 and S9 done
   (`final.candidate_ready`). Cards appear, and the weekly batch is issued.

- `worker._pilot_intake_in` asks `schedule_due` without the off-peak filter. Otherwise, in
  NCBI's peak hours, PubMed and GEO slices not yet fetched would not be due, the pilot intake
  would look complete, and the bake-off would start without them. It also ignores a `running`
  fetch row older than `intake.RUNNING_STALE_AFTER`: a worker that died mid-slice leaves one
  behind, and nothing else ever clears it, so the bake-off would never start.
- The pilot report's backfill projection (`reports._cost_throughput`) multiplies by window months
  per pilot month; with the whole window in the pilot that factor is 1.

The pilot is anchored to the first source row's date (`intake._pilot_anchor`), so backfill may
add the slice of a month that began since. That is expected.

The pilot also ends the anchor's unfinished month, ODP quarter and RePORTER fiscal year, and
the backfill does not fetch those slices again. Records added to them after the pilot fetch
arrive only through the continuous cadence: OpenAlex the whole window (the weekly sweep, plus
the daily 120-day lookback), NSF, DataCite and GEO 35 days, ODP the current quarter, RePORTER
the current and previous fiscal years monthly, PubMed only the day before each daily fetch. So a
PubMed-only record entered between the pilot and the start of continuous intake is missed,
and so are the anchor quarter's later patents if continuous intake starts in a new quarter.
This test is a snapshot (the server runs the website only, C8), so that is accepted (adversarial
audit, 2026-11-20).

### 6.4 Tests and checks

- Update the tests that pin Hopkins values: `tests/unit/landscaping/test_institutions.py`,
  `test_source_openalex.py`, `test_qualify.py`, `test_source_misc.py`, `test_source_pubmed.py`,
  and in `tests/integration/landscaping/`, `test_intake.py` (the positive-control fixture) and
  `test_reports.py` (the drift test). `test_scoring.py` needs nothing: it names only the
  `jhu_group_count` key, which keeps its name. Also update the pilot slice test.
- New matcher cases:
  - **pass:** La Jolla; Scripps Research Translational Institute;
  - **fail:** Jupiter; Florida; a Jupiter ZIP code alone; "Scripps Health"; "Scripps Clinic";
    "Scripps Institution of Oceanography"; "Scripps College"; "Wertheim UF Scripps Institute";
    "Calibr-Skaggs Institute, Scripps Research"; "Taiwan Semiconductor Research Institute
    (TSRI)"; bare "Scripps".
- New OpenAlex rule cases built from the authorship shape in §12, including an authorship with
  both campuses, which counts.
- A tripwire test finds no "Hopkins", "JHU" (as a word) or "Baltimore" in `src/landscaping`
  (except the disabled `sources/jhtv.py`), `src/services/landscape_chat_record.py`,
  `src/services/landscape_view.py`, `templates/landscape/`, `templates/workspace/landscape*.html`,
  the landscape help page, manager tutorial 11, reviewer tutorial 09 and the landscaping prompt
  review document; and none in the glossary's Opportunity entry or the help registry's
  landscape summary.
- `ruff`, the landscaping and doc-sync test files, then **one** full `scripts/ci.sh` in the
  workstation clone before the local build. The full run also shows that `alembic upgrade head`
  works on an empty database.

### 6.5 The local stack

Add `docker-compose.scripps.yml` to the branch. Do not copy production's working-tree compose
file: it pins production's container names and joins the shared edge network.

- Compose project `copi-scripps`.
- Services: `postgres` (15, own named volume), `scripps-app` (uvicorn, published on
  `127.0.0.1:8090` only; not `app`, the dev stack's web service name) and `landscape-worker` (`python -m src.landscaping.worker`, `stop_grace_period` 330 s, as in
  production). No `worker`, `agent`, nginx or certbot: landscaping never reads the jobs table
  (`worker.py` docstring), and the opportunity chat streams from the app process
  (`routers/landscape_chat.py:134-166`).
- `prompts/` mounted read-only into both, as production does.
- `env_file: .env.scripps` for the app and the worker; Postgres reads only `.env.scripps-db`
  (`POSTGRES_USER=copi`, a new random `POSTGRES_PASSWORD`, `POSTGRES_DB=copi`), so no API key
  reaches its container. Both are untracked; add `.env.scripps*` to `.gitignore` on the branch.

`.env.scripps`:

| Setting | Value |
|---|---|
| `DATABASE_URL` | the compose-internal Postgres, `postgresql+asyncpg://copi:<password>@postgres:5432/copi` |
| `SECRET_KEY` | a new random value |
| `ENVIRONMENT` | `production` (`SECRET_KEY` is then validated, `config.py:640-663`); the compose file also pins it on the app and the worker |
| `BASE_URL` | `http://localhost:8090` (the Origin guard needs it exact) |
| `ALLOW_HTTP_SESSIONS` | `true` (local HTTP only) |
| `ANTHROPIC_API_KEY` | the new key |
| `NCBI_API_KEY`, `OPENALEX_API_KEY`, `USPTO_API_KEY` | production's (production sets no `NCBI_CONTACT_EMAIL`) |
| `LANDSCAPE_ENABLED` | `false` until the owner approves the dry probe (§8), then `true` |
| `LANDSCAPE_DAILY_USD_CAP` | `100` (C4) |
| `LANDSCAPE_NCBI_MAX_RPS` | `1` (C6) |
| `LANDSCAPE_BATCH_MODE` | `api` if the new key's account accepts Message Batches, else `sync` (`scripts/scripps_admin.py batch-probe`) |
| `LANDSCAPE_WINDOW_DAYS` | `365` (owner) |
| `LANDSCAPE_CUT_FRACTION` | `0.20` (owner). The default `0.01` would give K = 2 for Scripps |
| `LANDSCAPE_SCREEN_MODEL`, `LANDSCAPE_EXTRACT_MODEL`, `LANDSCAPE_TRIAGE_MODEL` | `claude-opus-5` until the picks are applied, then the picks. Opus 5 is no bake-off candidate, so the funnel cannot open before C11's check on a default that happens to equal a pick; the bake-off itself names its models in code |
| `LANDSCAPE_CHAT_ENABLED` | `false` locally |

Every source needs a working key before the run. A source whose slices keep failing never
counts as fetched (`worker._pilot_intake_in`, `intake.py:880-887`), and the pilot then waits
forever. Without a USPTO key the patent source raises (`odp_patents.py:165-167`). Without an
OpenAlex key, paging is refused (`openalex_auth.py:3`).

### 6.6 Running it

1. Build the image from a clean clone of the branch on the workstation, with everything
   committed.
2. `docker compose -p copi-scripps -f docker-compose.scripps.yml up -d postgres`, then
   `alembic upgrade head` from a one-off app container.
3. Start the app and the worker. The worker inserts the source rows and its first candidate
   epoch, then fetches the window. PubMed and GEO run off-peak only, so allow a night.
4. When the bake-off report is stored, check the pilot's eligible-document count and the
   bake-off's actual spend against §6.7 and record the new projection (C11); the owner is asked
   only when it falls outside the window. Then
   copy its `recommended_settings` into `.env.scripps` and recreate the worker. Until then the
   pilot funnel waits (`worker.picks_applied`).
5. The candidate built from the picks is checked against the bake-off's S2–S4 regression set,
   before any S2 work. If it fails and is
   held, `scripts/scripps_admin.py override-epoch` lets it proceed, through the same
   `epochs.override` the route calls (`routers/landscape.py:156`).
6. When the pilot report is stored, `scripts/scripps_admin.py start-backfill` starts the backfill
   with the route's checks (`routers/landscape.py:108`). The worker moves to `continuous`,
   activates the epoch, and issues the first weekly batch.
7. Stop the worker (`docker stop -t 330`), then `pg_dump -Fc`. That file is the handover.

Steps 5 and 6 need an admin. The website login is ORCID only, with no password or dev login, so
they run as one-off commands in the app container instead (builder's choice): no local web login
is needed. `scripts/scripps_admin.py` also creates the reviewers' accounts (§7) and disables a
source that cannot be fetched (C9).

Progress shows in the worker log and the `landscape_reports` and `landscape_fetches` tables. The
pipeline page needs a login.

### 6.7 Cost and time

The per-stage estimate, its inputs and its formulas are in the companion
[cost estimate](2026-11-20-scripps-landscaping-cost-estimate.md). It is built from production's
Hopkins ledger and funnel, and from Scripps volumes read from the live sources on 2026-11-20.
At the 20% cut:

| Window | Eligible documents | K | Opportunities | Estimate | With batches |
|---|---|---|---|---|---|
| 1 year (the run's setting) | ~557 | 24 | ~20 | ~$202 ($142–$263) | ~$179 |
| 6 months | ~374 | 18 | ~15 | ~$167 ($117–$217) | ~$149 |

About $54 of either total is fixed: the bake-off's triage round, the regression check and the
repeatability run. Each idea in the cut adds about $4.13 for S6 through S9. Grants enter by fiscal
year, so a 6-month window keeps all 172 of them.

**Time.** Allow two to four days. At $100 a day, a $202 run spans at least two days, and PubMed
and GEO fetch only off-peak (§6.3).

The daily cap is soft. It stops new submissions and new synchronous calls only
(`worker._maybe_submit`, `_sync_step`). A bake-off that has started runs to its end
(`worker._evaluation_step`, `bakeoff.run_bakeoff`), and work already submitted still lands. A day
can therefore end somewhat over $100.

## 7. The server (owner, manual)

1. Use a small Linux server with Docker. The landscape data is small: Hopkins's is 1.1 GB, and
   Scripps should be a fraction of that.
2. Get the same commit to the server, by cloning it from `origin` or by moving the built image
   with `docker save` and `docker load`. Build the same image from that commit.
3. Write a new `.env.scripps-db` (new password) and a new `.env.scripps`. Never copy the
   workstation's `.env.scripps`: it holds production's NCBI, OpenAlex and USPTO keys, which the
   server does not need. The server's `.env.scripps`:
   - `ENVIRONMENT=production`, a new `SECRET_KEY`, and `DATABASE_URL` for the new password;
   - `BASE_URL=https://<host>`, exact, or every form fails with "Cross-site request refused.";
   - `ORCID_CLIENT_ID`, `ORCID_CLIENT_SECRET` and `ORCID_REDIRECT_URI=https://<host>/auth/callback`,
     registered on that ORCID client;
   - `ANTHROPIC_API_KEY` (the new key) and `LANDSCAPE_CHAT_ENABLED=true` only if the reviewers
     should have the opportunity chat (its own cap: `LANDSCAPE_CHAT_DAILY_USD_LIMIT`, default $50);
   - no `LANDSCAPE_ENABLED`.
4. Start Postgres alone (`up -d postgres` with the branch's compose file), restore the dump into
   the empty database with `pg_restore --no-owner`, before any `alembic` command. The dump
   carries its schema at `0078`.
5. Run **the app only** (C8): `up -d scripps-app`, never a bare `up -d`, which would also start
   the worker. With no worker, the database stays as delivered, with one weekly batch; staff see
   every card.
6. HTTPS through any reverse proxy in front of the app's port. Postgres is never published.
7. Accounts. `scripts/scripps_admin.py grant` creates each account allowed: the owner as `admin`
   with onboarding marked done, the colleague as `manager` (C7). Run locally before the dump,
   both accounts travel in it; otherwise run it on the server in a one-off app container. When
   the person first logs in by ORCID, the login finds the account by ORCID iD. Onboarding must be
   marked done for an admin, who is otherwise sent to `/onboarding` (`auth.py:336-337`), a page
   that waits for a profile job no worker runs here.
8. **Open view instead of sign-in (owner, 2026-11-23).** The su08 deployment runs with
   `LANDSCAPE_OPEN_VIEW=true` and no ORCID client. There is no sign-in of any kind: `/login`,
   and the site root, send a visitor to the Landscape list. ORCID's `/login/start` and
   `/auth/callback`, and every Microsoft sign-in route, answer 404.

   Anyone who reaches the site reads, as staff see them, these pages and no others:
   - the Landscape list, opened at `?view=current&tab=all`;
   - each opportunity's page.

   The pages are read-only (`src/web/open_view.py`):
   - no decision, review, expert or assignment form;
   - no review panel and no pipeline link;
   - the navigation holds the Landscape page alone.

   Everything else answers as it does to a visitor with no session: every other page and
   every POST. So nothing is written through the site.

   Consequences:
   - Nobody can record a review, so weekly batches go unreviewed.
   - An epoch override still runs through `scripts/scripps_admin.py`, with an account that
     `grant` created by ORCID iD.
   - The setting is off by default and must never be set in production.
9. **No final assessment (owner, 2026-11-24).** Scripps does not evaluate opportunities the way
   S9's rubric does, so the su08 deployment runs with `LANDSCAPE_FINAL_ENABLED=false`
   (`src/landscaping/s9_switch.py`). The opportunities shown are the top results of the S7
   pairwise ranking:
   - The worker makes no S9 call: no S9 batch, no S9 fallback, and no regression check of a kind
     only S9 reads.
   - A candidate activates once its post-drain cut's S6 and S8 have ended, labels included.
   - A weekly batch takes the never-issued opportunities in card order (the best member's S7
     position) without waiting for S9. No batch is blind, so no probe is drawn.
   - The pages read no S9 row. A card is its S8 label, a "Rank N" chip (the best member's S7
     position; 1 is the strongest) and each member idea's structured line, S6 summary and
     sources. The opportunity page adds the members' full S6 sections. There is no score, band,
     recommendation, pitch, key points, verdict, gating or score section, no count tiles and no
     sort but the pipeline order, and nothing says an assessment is missing.

   The run's first cut was assessed by S9 before this decision; its 17 rows stay in the
   database, unread. The setting is on by default, so production is unchanged.

## 8. Acceptance

- Matcher and OpenAlex-rule tests pass, and the wording grep is clean.
- `ruff`, the touched test files, the doc-sync check and one full `ci.sh` pass. Any failure is
  either fixed or shown to exist on `blackbird` too.
- Before the run, a dry probe of the window's counts per source, each against its live API, is
  recorded in the run log, `docs/plans/2026-11-20-scripps-landscaping-run-log.md` (counts and
  decisions only, since the branch is public):
  - OpenAlex lineage total, the works that qualify (senior author and campus rule), and the
    works the campus rule alone rejects;
  - PubMed;
  - RePORTER;
  - NSF.

  It includes 20 rejected and 20 accepted OpenAlex addresses, read by a person. Those go to
  `/home/a/scripps-run/`, outside the repository.
- Local run: every enabled source fetched, then bake-off, pilot report, backfill drained, epoch
  active or overridden with a recorded reason, cards shown, and one weekly batch issued.
- Spend per UTC day is recorded; a day over $100 is explained by work started before the cap
  was reached (the bake-off, the regression check, a synchronous chunk or a batch already
  submitted, §6.7). Production's containers, database, checkout and `.env` unchanged.
- The dump file and §7 handed over.

## 9. Risks

| Risk | Mitigation |
|---|---|
| The Jupiter rule leaks Jupiter work, or drops La Jolla work | The dry probe's 40 addresses, read before the run |
| PubMed's query catches other Scripps organizations | The last-author matcher filters every record; probe counts are compared with OpenAlex |
| A source stalls the pilot | Keys checked first (§6.5); a dead source is disabled in `landscape_sources`, with the reason noted |
| Too few documents for the bake-off or the regression set | The whole-window pilot (C2), and the sampler takes every document when there are fewer; otherwise the override in §6.6 step 5 |
| The estimate in §6.7 is off | It is checked against the bake-off's actual spend before the pilot funnel starts (C11, §6.6 step 4) |
| The shared NCBI or OpenAlex quota | 1 NCBI request a second, off-peak; Scripps's one-year OpenAlex volume is about 800 works, a handful of pages |
| The workstation fills up (21 GB free on 2026-11-20) | At least 5 GB free before building; the shared build cache is not pruned, since other projects on the workstation use it |
| Something private reaches the public branch | `.env.scripps`, dumps and run logs stay untracked (`.gitignore`); each diff is checked before it is pushed |

## 10. Left to the builder

Settled in the implementation plan, `docs/plans/2026-11-20-scripps-landscaping-plan.md`:

- The PubMed query: `"Scripps Research"[ad]` (§6.1).
- The Jupiter, Florida and Calibr patterns, read from addresses only.
- The Message Batches probe: `scripts/scripps_admin.py batch-probe`.
- Local admin actions: `scripts/scripps_admin.py` (§6.6).
- Compose details: port 8090, volume `pgdata` in project `copi-scripps`, worker memory 4 GB.
- The run log: `docs/plans/2026-11-20-scripps-landscaping-run-log.md`; the dry probe's addresses,
  the dump and the logs: `/home/a/scripps-run/`.

## 11. Production reference

These figures were read on 2026-11-20 with read-only queries:

- Landscape tables: about 1.1 GB.
- Documents by kind: work 61,179, preprint 6,741, dataset 4,983, grant 2,265, tech-transfer
  listing 2,112, patent 780.
- Top qualifying institutions: OpenAlex `I145311948` (Johns Hopkins University) and RePORTER
  "JOHNS HOPKINS UNIVERSITY".
- Spend: $1,321.25 over 66,825 calls; $211.37 before the pilot report.
- Epochs: two retired, one active since 2026-11-19 18:35 UTC.

## 12. Scripps facts (live sources, 2026-11-20)

- **OpenAlex.**
  - Scripps Research Institute is `I123431417` (ROR `02dxx6824`, San Diego). It has no child
    institutions. Works through the lineage filter: 1,052 in 2023, 877 in 2024, 995 in 2025 and
    854 in 2026. Johns Hopkins had about 22,700–24,100 a year.
  - 263 Scripps Research works since 2023 carry a Jupiter address.
  - Other institutions whose name contains "Scripps":
    - `I150209017` Scripps Institution of Oceanography (UCSD lineage `I36258959`);
    - `I1311914864` Scripps Health, and under it `I4210146328` Scripps Clinic, `I2802091096`
      Mercy, `I4210107570` Memorial, `I4210103888` Green, `I4210142769` Whittier Diabetes and
      `I4210146979` MD Anderson;
    - `I4210109568` Scripps Clinic Medical Group;
    - `I16970960` Scripps College;
    - `I4387156289` The Herbert Wertheim UF Scripps Institute (Jupiter, University of Florida
      lineage `I33213144`; 612 works since 2023);
    - `I4210089022` Scripps Korea Antibody Institute;
    - companies `I4210140114`, `I4210115855` and `I4405264316`.
  - Authorship shape: `affiliations` is a list of `{raw_affiliation_string, institution_ids}`
    alongside `institutions` and `raw_affiliation_strings`.
- **ROR `02dxx6824`:** names "Scripps Research Institute", "TSRI" and "Institut de Recherche
  Scripps". It lists UF Scripps as a related organization.
- **NIH RePORTER, fiscal 2024–2026,** for org names matching "SCRIPPS": 724 projects. Of these,
  487 are "SCRIPPS RESEARCH INSTITUTE, THE" (La Jolla, CA) in the first 500 returned, 6 Scripps
  Health, 5 Scripps Institution of Oceanography and 2 Scripps College. Florida, fiscal 2023–2026:
  none.
- **NSF,** awardees matching "Scripps" since 2023, first 25: 22 "University of California-San
  Diego Scripps Inst of Oceanography" and 3 "The Scripps Research Institute" (La Jolla).
- **Tech-transfer feed: one exists.**
  - Scripps Research's Office of Technology Development links its "available technologies" from
    `scripps.edu/science/technology-development/` to `https://scrippsotd.technologypublisher.com/`.
    It is the same Inteum platform as Johns Hopkins's.
  - The RSS 2.0 feed is `https://scrippsotd.technologypublisher.com/rss.aspx`. It holds **38
    items**, published 2026-05-21 to 2026-06-30, and nothing since.
  - Every item carries `title`, `caseId` (`BD-nnnn`), `link`, `description` (cut at about 600
    characters), `pubDate`, `guid` and `author`. The author is always the office's own address,
    so the feed gives no inventor names, and there are no `category` elements.
  - The listing, sitemap and search index name neither Jupiter nor Calibr. Scripps Florida
    discoveries are licensed through UF Innovate instead.
  - **The 403 comes from the user agent.** httpx's default agent (`python-httpx/…`) gets 403
    from this feed and from Johns Hopkins's. A generic agent gets 200. `jhtv.py` sets no agent,
    and production's `jhtv` source is disabled after 20 fetches that failed with 403.
  - **Terms.** Scripps Research's terms of service (`scripps.edu/terms-of-use`) apply to its
    website "and any information services that contain or link to these Terms of Service". They
    forbid using the Site "to spam, phish, pharm, pretext, spider, crawl, or scrape, or to train
    artificial intelligence algorithms". They allow content to be copied only for "personal,
    non-commercial home viewing use" without written consent. The Inteum host has no
    `robots.txt` and links no terms, so whether these terms cover the feed is unverified.
- **The one-year window, 2025-11-20 to 2026-11-20:**
  - **OpenAlex:** 808 Scripps Research articles, preprints and reviews; 60 of them carry a
    Jupiter address. The Hopkins lineage roots had 60,368 in their own three-year window.
  - **NIH RePORTER, fiscal 2026,** "SCRIPPS RESEARCH INSTITUTE, THE": 226 project rows, 171 core
    projects. There are no fiscal 2027 rows yet.
  - **USPTO ODP, publications in the year:** "Scripps Research" is applicant on 46 and assignee
    on 33. For "Johns Hopkins" the figures are 237 and 177.
- **Calibr (excluded, §2):** OpenAlex `I4210140527`, ROR `04r28v857`, La Jolla, outside the
  `I123431417` lineage. It has 91 works since 2023, 67 of which also carry `I123431417`. NIH
  lists no grants under its name for fiscal 2024–2026, and ROR records no link to Scripps
  Research.

## 13. Open questions

None. The owner settled the last two on 2026-11-20: the cut fraction is 0.20 (§2), and the
tech-transfer feed stays off without a permission request (C5). The feed would add at most 38
listings, about 17 kept at Hopkins's 45% S2 keep rate for listings: a few dollars of S3 and S4.
