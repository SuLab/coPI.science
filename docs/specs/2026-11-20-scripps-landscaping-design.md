# Landscaping test run for Scripps Research (La Jolla) — design

*Branch `scripps-landscaping`, from `blackbird` at 9ab7a021, pushed to `origin`. Written
2026-11-20. Every external figure below was read from the live source that day (§12); every code
reference is to 9ab7a021. Two questions are still open (§13).*

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
- **Where it runs:** the pipeline runs on the owner's workstation. When it has finished, the
  database is copied to a small cloud server that serves the website to both reviewers. The
  owner sets up that server manually, later (§7 is the checklist).
- **Keys:** a new Anthropic API key for this run. The NCBI, OpenAlex and USPTO keys are the ones
  production uses.
- **No known positives** are loaded for now.
- **The branch is pushed to `origin`.** Work continues in a clone on the owner's workstation.

Made by the designer (each can be overruled):

- **C1. Hard-code Scripps on the branch** rather than make the institution configurable (§5).
- **C2. The pilot fetches the whole three-year window**, not one sampled month per year (§6.3).
- **C3. When a record's campus cannot be told, it counts.** Patents, and any paper, dataset or
  grant with no campus in its address, are kept. La Jolla is the main campus.
- **C4. A $100 daily spending cap** (`LANDSCAPE_DAILY_USD_CAP=100`); the code's default is none.
- **C5. The tech-transfer listing source stays off**, as it starts (`intake._DISABLED_AT_START`).
- **C6. NCBI paced at 1 request a second** locally (`LANDSCAPE_NCBI_MAX_RPS=1`), because the key
  is shared with production.
- **C7. The remote colleague gets the `reviewer` role**; the owner is `admin`.
- **C8. The server runs the website only**, with no pipeline worker: the first weekly review
  batch, issued locally, travels in the database copy.
- **C9. Who decides during the run.**
  - The owner reads the 40 probe addresses (§8) before the run starts. The run waits for this.
  - The builder:
    - applies the bake-off's model picks;
    - disables a source that cannot be fetched;
    - overrides a held first epoch, so the run reaches cards.

    Each action goes in the run log with its reason. A held epoch is itself a result of the
    test, so its regression report is kept and summarized for the owner. Local admin actions run
    under the owner's local admin account.
- **C10. Only the landscaping pages are reworded.** The rest of the site still names Hopkins in
  places a reviewer can reach: the PI help pages, other glossary entries, and reviewer tutorial
  08. That text is left as it is. The reviewers are told to read only the landscape pages.

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
- Workstation: 48 cores, 130 GB RAM, Docker 29.9 with Compose, **19 GB free on `/`**.
- Tests run from `.venv-test` on the host (`CLAUDE.md`, Testing). One full `scripts/ci.sh`, once,
  before the local build; otherwise only the affected test files.

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
random month per calendar year (`intake.pilot_slices`): at about a twenty-fourth of Hopkins's
volume (§12) it may not reach the bake-off's samples of 600 documents, 60 extractions and 60
ideas (`bakeoff.py:866`), and an empty regression kind fails and holds the first epoch
(`regression.py:96-101`). (iii) Skip the bake-off and copy production's models: the pilot funnel
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
| PubMed affiliation term | `pubmed_works.AFFILIATION_TERM` | names "Scripps Research" or "TSRI" (exact query left to the builder, §10) |
| DataCite / GEO names | `datacite.py:21-27`, `geo.py:19-27` | "The Scripps Research Institute", "Scripps Research Institute", "Scripps Research" |
| USPTO assignee | `odp_patents._NAMES` | "Scripps Research" |
| Excluded place rule | APL cities, `odp_patents.APL_CITIES`, `qualify.py:77-78` | none: make it empty |

**The address matcher.** Add `is_scripps_la_jolla_affiliation(text)` in
`src/landscaping/institutions.py`. It replaces `is_baltimore_affiliation`. A string passes when it
names Scripps Research (the phrase "scripps research", which covers "The Scripps Research
Institute", or the token `TSRI`) **and** names neither Jupiter nor Florida (`Jupiter`, `Florida`,
`FL 334…`). Bare "Scripps" never passes. Do **not** edit `src/services/jhu_rules.is_hopkins_affiliation`:
PI profiles use it too (`corpus.py:169`, `non_pubmed_works.py:198`,
`industry_sources/openalex_industry.py:92`, the tenure rules in `jhu_rules.py`).

**The OpenAlex rule is the one real logic change.** OpenAlex tags Jupiter work with the same id,
`I123431417`. For example, a 2025 authorship lists `I123431417` on "The Scripps Research
Institute, Jupiter, United States". Today `authorship_roles.at_institution` accepts any authorship
listing a matching id and reads raw strings only when no institution is listed (`raw_strings=
"only_when_no_institutions"`). For Scripps, `qualifies_openalex` must use OpenAlex's per-address
`affiliations` list, which pairs each raw string with its institution ids:

- An authorship counts when **at least one** of its `affiliations` entries lists `I123431417` and
  that entry's raw string names neither Jupiter nor Florida. An author with both campuses counts.
- An authorship listing `I123431417` with no usable `affiliations` detail counts (C3).
- An authorship listing no institution at all counts when a raw string passes the matcher, as
  today.
- The role rule is unchanged: corresponding or last author, an individual
  (`institutions._QUALIFYING_ROLES`, `authorship_roles.is_individual`).

Make the Jupiter test a small function used by the OpenAlex rule, rather than changing
`at_institution`'s contract, which other callers share. Confirm that the OpenAlex fetch returns
`affiliations` within `authorships` and that the stored `raw_qualifier`
(`openalex_works.py:111-123`) keeps enough to re-check the rule.

**Other sources.**

- *PubMed:* the last author's affiliations go through the new matcher (`pubmed_works.py:109`).
- *DataCite and GEO:* use the matcher where an address is present; with none, keep (C3).
- *RePORTER and NSF:* the names above. NIH lists no Scripps grants in Florida for fiscal 2023–2026,
  so there is nothing campus-related to filter.
- *Patents:* all Scripps Research assignments count (C3).

**Drift report.** Point the drift listing at Scripps (`reports.py:79-80`, `_hopkins_like`): names
matching "scripps research" or `TSRI`, lineage root `I123431417`. Set
`EXCLUDED_OPENALEX_IDS` to the known non-La Jolla or unrelated institutions so they are not
re-proposed: `I4387156289` (UF Scripps, Jupiter), `I150209017` (Oceanography), `I1311914864`
(Scripps Health) and its children, `I4210109568`, `I16970960` and `I4210089022`. All are listed
in §12.

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
4. The first candidate epoch then activates once its post-drain cut has S6, S8 and S9 done
   (`final.candidate_ready`). Cards appear, and the weekly batch is issued.

The pilot is anchored to the first source row's date (`intake._pilot_anchor`), so backfill may
add the slice of a month that began since. That is expected.

### 6.4 Tests and checks

- Update the tests that pin Hopkins values: `tests/unit/landscaping/test_institutions.py`,
  `test_source_openalex.py`, `test_qualify.py`, `test_source_misc.py`, `test_source_pubmed.py` and
  `test_scoring.py`. Also update any test pinning the changed wording, or the pilot slice list.
- New matcher cases:
  - **pass:** La Jolla; "TSRI"; an author with both campuses;
  - **fail:** Jupiter; Florida; "Scripps Health"; "Scripps Clinic"; "Scripps Institution of
    Oceanography"; "Scripps College"; "Wertheim UF Scripps Institute"; bare "Scripps".
- New OpenAlex rule cases built from the authorship shape in §12.
- A grep of `src/landscaping`, `src/services/landscape*`, `templates/landscape`,
  `templates/workspace/landscape.html` and the help pages finds no "Hopkins", "JHU" or
  "Baltimore" in any user- or model-visible string.
- `ruff`, the landscaping and doc-sync test files, then **one** full `scripts/ci.sh` on the host
  before the local build. The full run also shows that `alembic upgrade head` works on an empty
  database.

### 6.5 The local stack

Add `docker-compose.scripps.yml` to the branch. Do not copy production's working-tree compose
file: it pins production's container names and joins the shared edge network.

- Compose project `copi-scripps`.
- Services: `postgres` (15, own named volume), `app` (uvicorn, published on `127.0.0.1` only) and
  `landscape-worker` (`python -m src.landscaping.worker`, `stop_grace_period` 330 s, as in
  production). No `worker`, `agent`, nginx or certbot: landscaping never reads the jobs table
  (`worker.py` docstring), and the opportunity chat streams from the app process
  (`routers/landscape_chat.py:134-166`).
- `prompts/` mounted read-only into both, as production does.
- `env_file: .env.scripps`, which is untracked; add it to `.gitignore` on the branch.

`.env.scripps`:

| Setting | Value |
|---|---|
| `DATABASE_URL` | the compose-internal Postgres |
| `SECRET_KEY` | a new random value |
| `ENVIRONMENT` | `production` (`SECRET_KEY` is then validated, `config.py:640-663`) |
| `BASE_URL` | `http://localhost:<port>` (the Origin guard needs it exact) |
| `ALLOW_HTTP_SESSIONS` | `true` (local HTTP only) |
| `ANTHROPIC_API_KEY` | the new key |
| `NCBI_API_KEY`, `OPENALEX_API_KEY`, `USPTO_API_KEY` | production's |
| `LANDSCAPE_ENABLED` | `true` |
| `LANDSCAPE_DAILY_USD_CAP` | `100` (C4) |
| `LANDSCAPE_NCBI_MAX_RPS` | `1` (C6) |
| `LANDSCAPE_BATCH_MODE` | `api` if the new key's account accepts Message Batches, else `sync` (§10) |
| `LANDSCAPE_WINDOW_DAYS` | unset (default 1096, three years, as Hopkins) |
| stage model settings | defaults until the bake-off; then its picks |
| `LANDSCAPE_CHAT_ENABLED` | `false` locally |
| ORCID settings | only if local login is used (§6.6) |

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
4. When the bake-off report is stored, copy its `recommended_settings` into `.env.scripps` and
   recreate the worker. Until then the pilot funnel waits (`worker.picks_applied`).
5. The first candidate is checked against the bake-off's S2–S4 regression set. If it fails and is
   held, an admin override lets it proceed (`routers/landscape.py:156`).
6. When the pilot report is stored, start the backfill (`routers/landscape.py:108`). The worker
   moves to `continuous`, activates the epoch, and issues the first weekly batch.
7. Stop the worker (`docker stop -t 330`), then `pg_dump -Fc`. That file is the handover.

Steps 4–6 need an admin. The website login is ORCID only; there is no password or dev login.
Either the owner registers a localhost redirect URI on an ORCID client and logs in locally, then
`python -m src.cli admin:grant --orcid …` and sets the account allowed (`cli.py:140-172`,
`dependencies.py:132-138`). Or the builder adds a small script that runs the same functions the
two routes call, with the same checks. Not raw SQL. The choice is the builder's (§10).

Progress shows in the worker log and the `landscape_reports` and `landscape_fetches` tables. The
pipeline page needs a login.

### 6.7 Cost and time

The figures are from production's Hopkins ledger, read-only. About 78,000 documents. $1,321 so
far. The pilot covered 15,600 documents and took about 22 hours from the first fetch to the pilot
report, costing $211 with the bake-off included. Scripps has about a twenty-fourth of Hopkins's
OpenAlex output, so the whole Scripps window should come to a few thousand documents, fewer than
the Hopkins pilot. Two costs do not shrink with size: the bake-off's fixed samples and S6–S9 on
the cut. `sync` mode costs twice what `api` mode does (`llm_pricing.BATCH_MULTIPLIER`). No firmer estimate is possible before the bake-off report.

The daily cap is soft. It stops new submissions and new synchronous calls only
(`worker._maybe_submit`, `_sync_step`). A bake-off that has started runs to its end
(`worker._evaluation_step`, `bakeoff.run_bakeoff`), and work already submitted still lands. A day
can therefore end somewhat over $100.

## 7. The server (owner, manual)

1. Use a small Linux server with Docker and Postgres 15. The landscape data is small: Hopkins's
   is 1.1 GB, and Scripps should be a fraction of that.
2. Get the same commit to the server, by cloning it from `origin` or by moving the built image
   with `docker save` and `docker load`. Build the same image from that commit.
3. Run `pg_restore` on the dump.
4. Run **the app only** (C8). With no landscape worker, the database stays as delivered, with
   one weekly batch. All cards are still browsable.
5. `.env`:
   - `BASE_URL=https://<host>`, exact, or every form fails with "Cross-site request refused.";
   - a new `SECRET_KEY`;
   - `ORCID_CLIENT_ID`, `ORCID_CLIENT_SECRET` and `ORCID_REDIRECT_URI=https://<host>/auth/callback`,
     registered on that ORCID client;
   - `ANTHROPIC_API_KEY` (the new key) and `LANDSCAPE_CHAT_ENABLED=true` if the reviewers should
     have the opportunity chat (its own cap: `LANDSCAPE_CHAT_DAILY_USD_LIMIT`, default $50).
6. HTTPS through any reverse proxy. Postgres is never published.
7. Accounts: each person logs in once by ORCID and lands as `pending`. Then:
   - the owner: `admin:grant` and set allowed;
   - the colleague: `role:set … reviewer` and set allowed (C7).

   Alternatively, add both ORCID iDs to the access allowlist before their first login
   (`auth.py:213-225,293-294`). An admin who has not finished onboarding is sent to
   `/onboarding` first (`auth.py:336-337`). No job worker runs here or locally, so that page
   would wait forever. The builder supplies a small script, run locally before the dump and again
   on the server for the colleague, that marks onboarding done.

## 8. Acceptance

- Matcher and OpenAlex-rule tests pass, and the wording grep is clean.
- `ruff`, the touched test files, the doc-sync check and one full `ci.sh` pass. Any failure is
  either fixed or shown to exist on `blackbird` too.
- Before the run, a dry probe of the window's counts per source, each against its live API, is
  recorded in the branch's notes:
  - OpenAlex lineage total and the count left after the Jupiter rule;
  - PubMed;
  - RePORTER;
  - NSF.

  It includes 20 rejected and 20 accepted OpenAlex addresses, read by a person.
- Local run: every enabled source fetched, then bake-off, pilot report, backfill drained, epoch
  active or overridden with a recorded reason, cards shown, and one weekly batch issued.
- No day over the cap. Production's containers, database, checkout and `.env` unchanged.
- The dump file and §7 handed over.

## 9. Risks

| Risk | Mitigation |
|---|---|
| The Jupiter rule leaks Jupiter work, or drops La Jolla work | The dry probe's 40 addresses, read before the run |
| PubMed's query catches other Scripps organizations | The last-author matcher filters every record; probe counts are compared with OpenAlex |
| A source stalls the pilot | Keys checked first (§6.5); a dead source is disabled in `landscape_sources`, with the reason noted |
| Too few documents for the bake-off or the regression set | The whole-window pilot (C2); otherwise the override in §6.6 step 5 |
| The shared NCBI or OpenAlex quota | 1 NCBI request a second, off-peak; Scripps's OpenAlex volume is a few dozen pages |
| The workstation fills up (19 GB free) | Images about 0.6 GB; prune build cache before building |
| Something private reaches the public branch | `.env.scripps`, dumps and run logs stay untracked (`.gitignore`); each diff is checked before it is pushed |

## 10. Left to the builder

- The exact PubMed query, checked against live counts.
- The exact Jupiter and Florida patterns, kept narrow so "Florida" in a disease or place name in
  a title is never read. The matcher reads addresses only.
- Whether the new key's account accepts Message Batches: one tiny probe batch decides `api` or
  `sync`.
- Local admin actions: ORCID localhost login, or a script reusing the route functions (§6.6).
- Compose details: port, volume names, memory limits.
- Where to record the dry probe and the run log on the branch.

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
- **Tech-transfer feed:** not checkable. technologypublisher.com answers 403 to automated
  requests, Johns Hopkins's feed included.

## 13. Open questions (owner)

1. **Calibr.** Does Scripps Research La Jolla include the California Institute for Biomedical
   Research (Calibr-Skaggs)? Here is what the sources say, as of 2026-11-20:
   - OpenAlex lists it as its own institution, `I4210140527` (ROR `04r28v857`, La Jolla), outside
     the `I123431417` lineage.
   - It has 91 works since 2023. 67 of them also carry `I123431417`; the other 24 would not be
     fetched without its id.
   - NIH lists no grants under its name for fiscal 2024–2026, and ROR records no link to Scripps
     Research.

   If it is included, `I4210140527` joins the OpenAlex rule, the lineage query and the drift
   report, and "Calibr" joins the address matcher.
2. **Total budget.** The daily cap is soft (§6.7), and most of the spending falls before the pilot
   report. Proposed: the run pauses after the bake-off report, which gives the actual cost per
   document. A projected total goes to the owner, and the run continues only on the owner's OK.
   The alternative is a fixed ceiling at which the run stops.
