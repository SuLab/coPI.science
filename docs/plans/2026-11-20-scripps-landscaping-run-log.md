# Scripps Research landscaping run log

Written to be publishable as it stands: no names, ORCID iDs, addresses or key values.

## 2026-11-23: pre-run checks (plan Task 7, Steps 1–3)

**Deployment.** The image is built from commit 08f90a09 (branch `scripps-landscaping`, no
uncommitted files). Its packages are anthropic 2.13.0, httpx 0.28.1 and uvicorn 0.54.0. The
worker is deployed with `LANDSCAPE_ENABLED=false` and `LANDSCAPE_DAILY_USD_CAP=100`.

**Anthropic key.**
- The first key provided was refused by the API before any billed call. It is not scoped to
  a workspace, so every request would have needed an `anthropic-workspace-id` header, which
  the app does not send. It was removed.
- The second key was installed at 15:53 PST. The app and the worker were recreated, and both
  read it.

**Batch mode: `sync`.**
- The Message Batches endpoint answered 400: "this endpoint is not available for
  HIPAA-configured organizations". `LANDSCAPE_BATCH_MODE` stays `sync`.
- The cost estimate's central figure (~$202, range $142–$263) already assumes synchronous
  prices, so the estimate is unchanged.

**Effort probe.** Sonnet 5 and Haiku 5.5 both accepted `effort=low`. Each was served by the
model requested and ended with `end_turn`. Ledger: 2 calls, $0.0006.

**USPTO key (ODP probe).**
- 1 record and no error, so the key works.
- Inventor cities are present.
- `earliestPublicationDate` and `abstractText` are absent from the record's metadata. This is
  noted only; the plan does not require them.

**Dry probe** (window 2025-11-23 to 2026-11-23, run 15:54 PST), compared with spec §12
(2026-11-20):

| Count | Measured | Spec §12 | Difference |
|---|---|---|---|
| OpenAlex works (listed = reported total) | 804 | ~808 | −0.5% |
| PubMed count | 782 | ~787 | −0.6% |
| RePORTER rows, fiscal 2026 | 226 | 226 | 0 |
| RePORTER rows, fiscal 2027 | 1 | 0 | +1, explained below |
| NSF awards | 3 | 0–3 | within range |
| Qualifying works | 431 | (new measurement) | |
| Works rejected by the campus rule | 28 | (new measurement) | |

- *Fiscal 2027 RePORTER row (decision: accepted, no change).* It is one R01 continuation,
  award notice 2026-11-20, added to RePORTER 2026-11-21. That is after the spec's
  measurement. Grants enter by fiscal year, and the window touches fiscal 2027, so the run
  takes this row with fiscal 2026's 226. The cost effect is one grant.
- No count is more than 20% off.

**Spend so far:** $0.0006 (the effort probe).

**Anthropic Console (plan Step 1).**
- The owner confirmed the key on 2026-11-23 and revoked the first key.
- The organization's limits are not shared with production (the owner, 2026-11-23).

## 2026-11-23: owner gate 1 (plan Task 7 Step 5)

The owner asked for an adversarial audit of the addresses and for gate 1 to be decided on its
findings.

**What the audit covered.**
- Every work in the window, not only the 40 samples: 804 OpenAlex works, 782 PubMed records.
- An independent re-implementation of spec §6.1, written from the spec's text. It agreed with
  the deployed rule on every work and every senior authorship.
- Every senior authorship flagged for words of the Jupiter campus, Calibr or the unrelated
  Scripps institutions, or for missing a La Jolla place word.
- PubMed and OpenAlex verdicts compared for the 580 works both hold.
- For the 53 authorships whose address alone left the campus open, the author's own
  affiliation strings since 2023 were read.

**What was correct.**
- All 28 campus-rejected works were Jupiter or Calibr.
- The rule's known blind spot did not occur. (It is a La Jolla address without "Scripps"
  beside a Jupiter Scripps address.)
- Accepted addresses with no city belonged to La Jolla authors when checked against their
  other works.

**What was wrong (decision: fix the rule, as plan Task 7 Step 5 directs).**
- *Jupiter accepted: 4 OpenAlex works and 1 PubMed record, 5 distinct works.* Each address
  named the Jupiter institute only as "UF Scripps" or "Wertheim … UF Scripps", or read
  "Scripps Research, La Jolla, FL".
- *La Jolla rejected: 2 works.* OpenAlex filed a "Scripps Research, La Jolla" address under
  another institution: once under Scripps Institution of Oceanography, once under the
  author's other institutes.

**The fix (commit 8425849e, pushed; spec §6.1 updated).**
- *Excluded-campus pattern.* It now also names "UF Scripps", "Wertheim Scripps" and the
  capital state code "FL". "Wertheim" alone is not excluded: UC San Diego's school of public
  health bears the name.
- *Authorships under other institutions.* An authorship listing other institutions but not
  Scripps Research now counts when an address passes the matcher.
- *Checks.* The landscaping suites passed (868), and the full CI gate passed (11,278 passed,
  91 skipped).

**Redeploy and re-run.**
- The image was rebuilt from 8425849e. The app and the worker were recreated; Postgres was
  untouched.
- The previous image is kept for rollback.
- *Dry probe again (window 2025-11-24 to 2026-11-24):* OpenAlex 804, qualifying 430,
  campus-rejected 31, PubMed 782, RePORTER 226 (fiscal 2026) + 1 (fiscal 2027), NSF 3.
- *The audit's dump again:* exactly the 7 predicted verdicts changed and nothing else.
  OpenAlex 431 → 430 qualifying; PubMed 336 → 334.

**Residual.**
- One meeting abstract is probably Jupiter work, but its senior address reads only "Scripps
  Research". No address rule can place it.
- A La Jolla address merged into one string with a Florida institution is still excluded.
  This is the spec's accepted cost; the audit found no instance.

**Gate 1 approved on 2026-11-23**, on the audit's findings, as the owner directed.

## 2026-11-23: the pilot starts (plan Task 8)

- **Step 1, 17:41 PST:** `LANDSCAPE_ENABLED=true`. Only the worker was recreated. Intake
  started at once: OpenAlex, RePORTER, NSF, DataCite and USPTO slices are complete or
  running. PubMed and GEO wait for their 21:00–05:00 US Eastern window.
- USPTO records often lack `abstractText` (15, 7, 9, 12 and 13 per quarter). This is noted,
  not acted on: the probe found the same.
- **Step 2, done by 18:36 PST.** Every enabled source's latest slices are `complete` or
  `incomplete`:
  - OpenAlex, PubMed, GEO and DataCite: 13 slices each, all complete.
  - USPTO: 5 complete. RePORTER: 2 complete.
  - NSF: 10 complete and 3 incomplete.
  - Eligible documents (E so far): 658, made of 316 works, 126 preprints, 171 grants and 45
    patents.
- **NSF finding (decision: fix in code, no source disabled).**
  - The 3 incomplete NSF months each report 1 award and return none. Those 3 awards are all
    the NSF awards in the window.
  - Cause: NSF's API counts `offset` from 0. The source asked for `offset=1`, which skips the
    first award of every request.
  - Measured 2026-11-23: for a one-award month, `offset=1` returns nothing. For another
    awardee's month, `offset=0` returns 13 of 13 and `offset=1` returns 12.
  - The fix starts at offset 0. It will be deployed with the next image. The 3 awards enter
    when the NSF months are fetched again on that image.
  - The same code runs in production's (Hopkins) landscaping, where each NSF month loses its
    first award. That is reported to the owner, not changed here.
- **Step 3, done 20:13 PST (04:13 UTC, 2026-11-24).** The bake-off report was stored with no
  error and no worker restart: 3,373 calls, $45.25. Reference Opus 5; candidates Haiku 5.5,
  Sonnet 5 and Sonnet 5.5; 658 pilot documents. Each candidate below shows its result and cost.
  - *Screen (600 documents), pick Sonnet 5.5, passed.* It was the only pass: recall 0.976,
    precision 0.909, no refusals, $0.99.
    - Haiku 5.5 failed on refusals: 2.5% against a 2% bar. Recall 0.976, precision 0.858,
      $0.11.
    - Sonnet 5 failed on recall (0.926) and refusals (11%).
  - *Extract (60 documents), pick Sonnet 5, shortfall: no candidate passed.* The bar was
    0.771 agreement with the reference. Sonnet 5 scored 0.766 ($0.87), Sonnet 5.5 0.663 and
    Haiku 5.5 0.594.
  - *Triage (57 ideas), pick Sonnet 5.5, shortfall: no candidate passed the gate check.* Gate
    agreement: Sonnet 5.5 0.860 ($0.77), Sonnet 5 0.632, Haiku 5.5 0.386. All three passed
    on scores.
  - *Production's (Hopkins) models now, read 2026-11-24 from its worker's settings:* screen
    Haiku 5.5, extract Opus 5, triage Sonnet 5.5. The picks differ on screen and extract.
- **Step 4, checkpoint C11 (06:10 PST, 2026-11-24).**
  - E = 658 eligible documents.
  - B = $45.25 (all of it bake-off calls; outside the bake-off, the probes spent $0.0006).
  - P = 45.25 + 31.45 + 118.30 × 658 ÷ 557 = **$216.45**. That is inside $151.73–$252.88, so
    there is no gate 2.
  - P is priced on production's models (cost estimate, method 1).
- **Step 5, waiting on the owner.** The owner asked on 2026-11-24 whether the bake-off had been
  skipped in favour of production's models.
  - The spec, the plan and the session record show no such decision. The spec lists skipping
    as an alternative it did not choose (§5, option iii), and C9 says the builder applies the
    bake-off's picks.
  - The bake-off has already run. It also seeded the regression set that the first epoch's
    check needs.
  - The worker opens the pilot funnel only when its settings equal the report's picks
    (`worker.picks_applied`). Running production's models therefore needs that gate bypassed.
  - **Owner's decision (2026-11-24):** screen on Sonnet 5.5 (the bake-off's pick, the only
    screen candidate that passed), extract on Opus 5 and triage on Sonnet 5.5 (production's
    models; no candidate passed either stage). Against production's models, only screen
    differs (Haiku 5.5 there). The cost is about $1 more over the run, and P stays about $216.
  - The owner also said not to re-run any part of the pipeline that has already finished.
    Checked before applying:
    - No LLM call since the bake-off's last at 04:13 UTC.
    - No regression run and no opportunity existed.
    - Intake slices that have ended are never due again (`intake._settled`).
    - The bake-off runs again only on a `bakeoff_request` report, and there is none.
- **Step 5, done 06:26 PST (14:26 UTC).**
  - *The decision is recorded as a second `bakeoff` report.* It is a copy of the bake-off's
    payload with the owner's `recommended_settings`, the bake-off's own picks under
    `bakeoff_picks`, and the decision and reason under `owner_override`. The bake-off's own
    report row is unchanged. The worker's funnel check (`worker.picks_applied`) reads the
    latest bake-off report, so it now passes.
  - *Settings.* The three settings were set in `.env.scripps` (mode 600 kept).
  - *Worker.* Only the worker was recreated, on the a0674a0f image, which carries the NSF
    offset fix; the app and Postgres were untouched. It reads the three models.
  - *Epochs.* As expected, the first candidate was retired and a new candidate was made for
    the new settings. Its regression check started at once (screen 559 items, $1.85, then
    extract).
  - *Checks.* No error and no restart; no other project's events.
  - *Rollback:* delete the override report, set the three settings back to `claude-opus-5`,
    then recreate the worker.
- **NSF months left as they are.** The 3 NSF months that ended `incomplete` (2026-04, 2026-07
  and 2026-08; 1 award each) are not fetched again: an ended slice is never due, and the
  owner asked that nothing finished be re-run. Fetching them again would add 3 awards and
  needs the owner's go-ahead.

## 2026-11-24: the open view deployed (owner's request of 2026-11-23)

- Commit a0674a0f (pushed; full CI 11,315 passed, 91 skipped) adds `LANDSCAPE_OPEN_VIEW`.
  - Sign-in is off: ORCID and Microsoft return 404, and `/` and `/login` lead to the
    Landscape list.
  - A visitor with no session may GET the Landscape list and an opportunity's page,
    read-only. They see no form that writes and no navigation beyond Landscape.
  - Every other page, and every POST, goes to `/login`, so nothing can be written.
- *Deploy, 06:20 PST.* The image was rebuilt from a0674a0f and loaded on the internal host with identical
  layers. The previous image is kept for rollback. The clone moved to a0674a0f with the
  prompts and compose file unchanged. `LANDSCAPE_OPEN_VIEW=true` was added to `.env.scripps`
  (mode 600). Only the app was recreated.
- *Checked from outside, anonymously:*
  - The landing page answers 200 with the read-only label and no POST form.
  - `/` and `/login` redirect to it.
  - The ORCID and Microsoft sign-in routes answer 404.
  - The PIs, pipeline, settings and admin pages, and a same-origin POST, redirect to `/login`.
  - The app logged its start-up warning, and there were no other project's events.
- *Rollback:* tag the kept image as `copi-scripps:local`, set `LANDSCAPE_OPEN_VIEW=false`,
  recreate the app.
- *Consequence for Task 9:* nobody can sign in, so the weekly review batches will not be
  reviewed on this host. A held epoch is overridden from the command line, which needs an
  admin account (`scripps_admin.py grant`).
- **Step 6, the candidate's regression: held, 07:12 PST (15:12 UTC).** The run took 46
  minutes and cost $11.10; Postgres records it as `failed`. Each stage is compared with the
  Opus 5 reference.
  - *Screen, Sonnet 5.5, 559 items: passed.* Recall 0.976, precision 0.902, no refusals.
  - *Extract, Opus 5, 60 items: passed.* Agreement 0.879 against a bar of 0.780; quotes
    verified 0.999.
  - *Triage, Sonnet 5.5, 57 ideas: failed.*
    - Gate agreement was 0.860 against a 0.90 bar, the same figure as in the bake-off.
    - Scores within one point: 1.00 (passes).
    - No candidate in the bake-off reached the gate bar either. Production runs triage on
      the same model.
  - The epoch is `held` with "regression failed on set version 1: triage". Nothing beyond
    S1 runs until it is overridden (spec C9: the builder overrides a held first epoch so
    the run reaches cards).
  - *Waiting on the owner:* the admin account's ORCID iD, for `scripps_admin.py grant`
    then `override-epoch`.
  - Worker log: no error. There were 14 ChEMBL 500 answers for free-text agent names, each
    left unmatched.
  - Spend on 2026-11-24 (UTC) so far: $56.34.
- **Step 6, overridden at 07:31 PST (15:31 UTC), with no account.** The owner said on
  2026-11-24 that only the completed run is needed and sign-in should not be. So no ORCID iD
  was asked for, and no account was made.
  - *Why no account was needed.* `scripps_admin.py override-epoch` asks for an admin's iD
    only to name the actor. The override itself is `epochs.override`. Its actor column is a
    nullable link to users, and only the pipeline page shows it. The worker goes on once
    `override_at` is set.
  - *How.* `epochs.override` was called with no actor in a one-off app container
    (`exec/x16-override.sh`).
  - *Result.* The epoch is `candidate` again, with `override_at` and the reason set and the
    actor empty. The reason names the owner's instruction, spec C9 and the triage figures.
  - *S2 started.* The worker began the re-judge (no stage copied forward, as for a first
    epoch). It sent all 658 eligible documents to S2 on Sonnet 5.5, and calls are landing.
  - *An empty cut, harmless.* The worker also took the day's S5 cut at once, with N = 0 and
    K = 0. That is the daily schedule: one cut per UTC day, due when no S4 work is pending
    or from 06:00 UTC (`cut.cut_due`), and nothing was pending yet. S8 made no groups for
    it. The pilot report waits for a cut that S8 has grouped (`worker._grouped` needs a
    grouping row), so this cut cannot trigger it. The first cut with ideas comes on the
    next UTC day, after S4.
  - No error, no restart, and no other project's events.
- **The 3 NSF months were fetched again on the owner's go-ahead (2026-11-24, 07:4x PST).**
  - *How.* `intake.run_slice` ran for month:2026-04, month:2026-07 and month:2026-08 only,
    in a one-off container of the worker's service (`exec/x17-nsf-refetch.sh`). The image
    was a0674a0f, whose NSF paging starts at offset 0.
  - *Result.* Each month is now `complete` with 1 award of 1. The earlier `incomplete` rows
    are kept as history.
  - *New documents.* 3 grants entered, all eligible: grants went from 171 to 174, and E from
    658 to 661. They have no S2 row yet. The worker counts an eligible document with no S2
    row as owed work (`epochs.funnel_work_pending`), so it will screen them in the current
    epoch.
  - Nothing else was fetched. Worker untouched (running, no restarts); no other project's
    events.
- **S2 done for the first 658 documents (by 07:55 PST, $2.16).**
  - Kept 426 (65%): 248 `yes` and 178 `unsure`, which goes on to S3. Excluded 232 (`no`).
  - By kind, yes + unsure of all: works 188/316, preprints 75/126, grants 128/171, patents
    35/45.
  - The 3 re-fetched NSF grants are still owed S2.
- **Cost finding: S2 keeps far more than the estimate assumed.**
  - The estimate used Hopkins's keep rates (18% of papers, 46% of grants, 53% of patents) and
    expected about 164 kept. Scripps keeps 426, about 2.6 times as many.
  - Carried through Hopkins's other rates, that gives about 440 extractions, about 345 ideas,
    a cut pool of about 305, and K ≈ 62 at the 20% cut, not 24.
  - S6–S9 cost about $4.13 per idea in the cut, so the run's total moves from about $216 to
    about $370 (±30% on the part not yet spent). That is past C11's upper bound of $252.88.
  - C11's formula scales by E only, so it could not see this. The checkpoint passed as
    defined.
  - The cut fraction is not an epoch component (`epochs.current_components`), so changing it
    needs only a worker recreate. It must be decided before the next UTC day's cut, which
    comes once S4 is done or from 06:00 UTC.
  - Sent to the owner for a decision.
- **Owner's decision (2026-11-24): an 8% cut.** `LANDSCAPE_CUT_FRACTION` went from 0.20 to
  0.08 at 07:58 PST (`exec/x18-cut.sh`).
  - *Before the change.* No cut with ideas existed: the only one was the empty cut of the
    day. The epoch was a candidate.
  - *How.* Only the worker was recreated. It reads 0.08, with the models unchanged; the app
    and Postgres were untouched.
  - *Why the epoch stays.* The cut fraction is not an epoch component, so the epoch is the
    same one and nothing judged is redone.
  - *Nothing lost to the restart.* The S3 batch had not yet been made when the old worker
    stopped. The new worker made it (426 extractions) with the 3 re-fetched grants' S2.
  - *Calls resumed.* The first calls landed at its first 300 s batch poll, 16:03 UTC.
  - *Projection now:* K ≈ ceil(0.08 × ~305) ≈ 25, so S6–S9 cost about $103 and the run
    about $220 in all (±30% on the part not yet spent).
  - *Rollback, before the next cut:* `LANDSCAPE_CUT_FRACTION=0.20`, then recreate the worker.
  - No error, no restart, and no other project's events.
- **Check at 08:34 PST (16:34 UTC).**
  - *S2:* the 3 re-fetched grants are screened. Totals: 427 kept, 234 excluded.
  - *S3:* 191 done, 226 submitted, 3 pending; 6 refused and sent to the fallback model
    (Opus 4.8). One PubTator 400 on a single PMID was logged as a warning.
  - *Ideas:* 269 so far, from 191 extracted documents. That is 1.41 per document, against
    Hopkins's 0.78 per extraction.
  - *Spend:* $72.13 today (UTC), which is also the total.
- **Projection again (preliminary: 45% of extractions done).**
  - At 1.41 ideas a document: about 590 ideas, a cut pool of about 520, and K ≈ 42 at 8%.
  - S6–S9 would then cost about $173, and the run about $300 in all.
  - 5% would give K ≈ 26 and about $235.
  - The idea count may fall as later extractions merge into existing ideas.
  - The first real cut comes once S4 is done or at 06:00 UTC on 2026-11-25 (22:00 PST),
    whichever is first. The fraction must be set before then.
  - Sent to the owner.
- **Owner's second decision (2026-11-24): a 5% cut.** `LANDSCAPE_CUT_FRACTION` went from
  0.08 to 0.05 at 08:37 PST (`exec/x19-cut5.sh`).
  - *Before the change.* There was still no cut with ideas, and the epoch was a candidate.
  - *How.* Only the worker was recreated. It reads 0.05, with the models unchanged; the app
    and Postgres were untouched.
  - *What the restart did to the S3 batch.* The worker logged that the in-flight local S3
    batch's requests were lost to the restart and its rows released, as designed. Those
    rows went back to `pending` and were batched again.
  - *Nothing paid twice.* Every extraction item with two ledger rows since 15:30 UTC (9 of
    them) is a refusal by Opus 5 answered by the fallback model, Opus 4.8. None is a
    repeat caused by the restart.
  - *Progress at 08:39 PST:*
    - S3: 257 done, 157 submitted, 4 pending, 9 refused (each answered by the fallback).
    - Ideas: 328, which is 1.28 per extracted document.
    - S4: 326 submitted.
    - Spend: $72.99 today (UTC).
  - *Projection:* about 540 ideas, a pool of about 480, and K ≈ 24 at 5%, the plan's size.
    S6–S9 would cost about $100, and the run about $235 in all.
  - No error and no other project's events.
- **Owner's decision (2026-11-24): the daily cap goes from $100 to $300.** Changed at
  09:21 PST (`exec/x20-cap.sh`).
  - *Why.* The cap was about to stop the run at $88.51 for the day. A higher cap does not
    change the total; it only lets the work finish in fewer days.
  - *How.* Only the worker was recreated. It reads cap 300, cut 0.05 and the same models;
    the app and Postgres were untouched.
  - *Calls resumed* at the first batch poll. No error and no other project's events.
  - *Progress at 09:26 PST:*
    - S3: finished, 406 done and 21 refused.
    - Ideas: 502.
    - S4: 324 done, 2 excluded at the gate, 180 submitted.
    - Spend: $88.87 today (UTC).
  - *Projection:* a pool of about 445, K ≈ 23 at 5%, and the run about $230 in all.
  - *Timing.* Today's (UTC) cut has been taken already (the empty one). The first cut with
    ideas is therefore due at 00:00 UTC on 2026-11-25 (16:00 PST), once S4 is done. S6–S9
    and the pilot report follow within the new cap.
  - *The 21 refused extractions are final.* Each carries refusal category `bio`, with
    `fallback_from` = Opus 5. The fallback chain (Opus 4.8) declined too, so those 21
    documents (5% of the 427 kept) yield no ideas. Production handles refusals the same
    way; nothing was changed.
- **S2–S4 finished (by 09:34 PST, 2026-11-24).**
  - *S4:* 503 done and 3 excluded at the gate. Triage cost far less than the estimate: 180
    calls for $4.17 since 09:21, about $0.023 a call against Hopkins's $0.085.
  - *Evidence:* assembled for all 502 ideas. 3 ideas have the clinical-trials lookup
    `failed` after 3 tries; the other five lookups are done for them.
  - *Spend:* $92.67, which is today's (UTC) and the total.
- **Projection before the first real cut.**
  - About 499 ideas passed triage. After near-duplicate folding (Hopkins pool per idea:
    0.883), n ≈ 440–499, so K = ceil(0.05 × n) ≈ 22–25 at the owner's 5%.
  - The rest of the run: S6–S9 at about $4.13 per idea in the cut, about $90–103, plus S4
    repeatability (about $6) and the reject audit (about $6).
  - That puts the run at about $200–210 in all.
  - The cut comes at 00:00 UTC on 2026-11-25 (16:00 PST): one cut per UTC day, and today's
    was the empty one.
- **Check at 13:07 PST (21:07 UTC).** The SSH connection dropped after 09:34 PST and was
  reopened at 13:07. The worker ran on regardless: running, no restarts, no error.
  - It has been idle since its last triage call at 09:32 PST, as expected: S2–S4 are done,
    and the first cut with ideas is due at 00:00 UTC.
  - Spend is unchanged at $92.67, and there were no other project's events.
  - Task 9 Step 1 is ready as `exec/x21-backfill.sh`, reviewed. It runs only when a `pilot`
    report exists and the phase is still `pilot`.
- **The day's empty cut was deleted on the owner's instruction (13:11 PST, 21:11 UTC).**
  - *Why it was in the way.* S5 takes at most one cut per UTC day. The empty cut (N = 0,
    K = 0) had been taken at 07:31 PST, before any idea existed, so the first real cut would
    otherwise have waited for 00:00 UTC.
  - *Checked first* (`exec/x22-cut-now.sh`): it was the only cut, and no grouping, S6 row or
    opportunity version referred to it. No idea was marked in it, S2–S4 had nothing in
    flight, and the epoch was a candidate.
  - *Deleted* in one transaction, guarded on its id and N = K = 0.
  - **The first real cut, 13:12 PST: N = 466, K = 24** (5%), with a tie class of 2.
  - *S6 started at once* (4 running, 20 pending), and S7 reranking started beside it. One
    Europe PMC full-text lookup failed transiently (HTTP 500) and was logged as a warning.
  - No error, no restart, and no other project's events.
- **The backfill was started before the pilot report, on the owner's instruction (13:20 PST,
  21:20 UTC).** The pilot report will be generated by hand once S6–S9 are done.
  - *Why.* `begin_backfill` refuses without a pilot report, and the worker stores that report
    only after S6–S9. Activation also needs a cut taken after the drain, with one cut per UTC
    day. Waiting for the report risked missing the 00:00 UTC cut and losing a day.
  - *Checked first* (`exec/x23-backfill-now.sh`): the phase was unset (pilot), there was no
    pilot report, the epoch was a candidate not yet drained, one real cut existed, and S2–S4
    owed nothing.
  - *The write.* `landscape_phase = backfill`, the same `app_settings` upsert
    `begin_backfill` makes, without its report check. This bypasses the guard by hand; it
    is recorded here.
  - *No flip within 3 minutes*, so the script stopped and the state was examined read-only
    in a one-off container:
    - the backfill slices are all ended (72 wanted, none missing);
    - `_backfill_fetched` returns true;
    - `funnel_work_pending` returns false.
  - *Why the flip waits.* The flip runs at the start of the worker's next pass. The current
    pass is still in its synchronous step, running the 24 S6 analyses queued at the cut,
    4 at a time. The flip to continuous therefore comes once that pass ends, expected well
    before 00:00 UTC.
  - *Not stalled.* S6 is progressing: 2 done, 4 running, 46 deep calls in 10 minutes.
- **Flipped to continuous at 14:19 PST (22:19:43 UTC).** The candidate's `drained_at` was set
  in the same transaction (`_flip_to_continuous`), 3 h 40 min before the 00:00 UTC cut. That
  cut is therefore the post-drain cut activation needs.
  - The flip came when the worker's pass with the queued S6 analyses ended, as diagnosed.
  - *S6 at the flip:* 22 done and 1 running. 1 ended `failed:truncated`: the analysis hit
    max_tokens even after its retry. The worker treats that as final, and the pilot report's
    truncation rate counts it.
  - *S7 and similarity:* running (199 rerank and 14 similarity calls in 5 minutes).
  - *Intake:* no continuous fetch had started yet.
  - *Spend:* $128.62 today (UTC), which is also the total.
- **S6–S9 ended on the first cut (15:25 PST, 23:25 UTC).**
  - *S6:* 23 done, 1 `failed:truncated` (final).
  - *S7:* 224 rerank comparisons and 16 similarity calls, all done.
  - *S8:* 17 groups, labelled.
  - *S9:* all 17 assessments done. 3 of the 20 final calls hit max_tokens (16,000) and were
    retried successfully, so no assessment is truncated.
  - *Continuous intake:* the first daily and weekly fetches ran at 14:39 PST, and every slice
    is complete. They brought 4 new documents.
- **The pilot report was generated by hand (15:27 PST, 23:27 UTC), on the owner's instruction.**
  - *Script:* `exec/x24-pilot-report.sh` ran `reports.pilot_report` once, in a one-off
    container of the worker's service on the deployed commit.
  - *Checked first:* there was no pilot report, S6–S9 had nothing in flight, and S9
    assessments existed.
  - *S4 repeatability:* 46 of 50 ideas were consistent (0.92), scored by Sonnet 5.5 (the
    epoch's S4 model). The 100 calls cost $2.57, the only model calls the report made.
  - *Truncation:* S6 had 3 of 363 calls truncated (0.8%); S9 had 3 of 20 (15%, all retried).
  - *Normalization:* 664 of 1,401 names matched (47%), and 737 are ambiguous.
  - *Positive controls:* none were entered.
  - *Bake-off:* the report's bake-off section is the owner's mixed-model override report.
  - *Cost to date by stage:*
    | Stage | Cost |
    |---|---|
    | extract | $57.04 |
    | deep | $38.19 |
    | triage | $24.60 |
    | final | $16.16 |
    | screen | $13.64 |
    | rerank | $2.85 |
    | deep_extract | $1.81 |
    | audit | $1.23 |
    | similarity | $0.20 |
    | label | $0.10 |
  - No other project's events.
- **The post-drain cut, and activation (16:00 PST, 00:00 UTC 2026-11-25).**
  - *Cut:* taken at 00:00:30 UTC, N = 464, K = 24. It held the same 24 ideas as the first
    cut, so every S6 record was reused, and S8 re-pointed its 17 groups to the new cut at once.
  - *Activation:* the groups' S9 assessments were already done, so the candidate was ready.
    The epoch activated at 00:00:30 UTC: 17 opportunities were minted, and the regression
    baseline (set version 2) was frozen.
  - *Review batch:* the first weekly batch was issued at 00:01 UTC. It is a normal batch of
    10 cards, taken in card order.
  - **Task 9 Step 2 is met:** one active epoch, 17 opportunities and one review batch.
  - *Spend:* $0.24 from 00:00 UTC to the stop, for 2 extractions and 1 audit call. S9 made no
    new call, because none was owed.
- **The owner removed S9, the final assessment, from the Scripps run (16:0x PST).** Scripps
  does not evaluate opportunities the way S9's rubric does. The pages should show the top
  results of the S7 pairwise ranking instead. The owner's choices:
  - stop the worker now, until the change is deployed;
  - each card shows its rank and its members' S6 analyses and sources;
  - every opportunity of the cut is kept, in rank order.
- **The worker was stopped (16:20 PST, 00:20 UTC)** with `exec/x25-stop-worker.sh`: our
  service only, using the same `dcs stop` call the disk guard makes. The app and Postgres kept
  running, and no other project's events were seen. Nothing restarts the worker: its restart
  policy is unless-stopped, and no cron job starts it.
  - *Finding:* S9 had in fact already run on the first cut, before the owner's decision
    ($16.16, 17 assessments). I had wrongly told the owner it would run again on the
    post-drain cut. Those rows stay in the database and the pages stop reading them.
- **Task 9 Step 3: model spend by UTC day** (ledger `landscape_llm_calls`; read 00:37 UTC
  2026-11-25, with the worker stopped):

  | UTC day | Calls | Spend |
  |---|---|---|
  | 2026-11-23 | 2 | $0.00 (2 screen calls) |
  | 2026-11-24 | 6,570 | $155.80 |
  | 2026-11-25 | 3 | $0.24 |
  | **Total** | **6,575** | **$156.05** |

  - *2026-11-24 by stage:*

    | Stage | Calls | Spend |
    |---|---|---|
    | extract | 1,152 | $57.04 |
    | deep | 363 | $38.19 |
    | triage | 915 | $24.60 |
    | final | 20 | $16.16 |
    | screen | 3,761 | $13.63 |
    | rerank | 224 | $2.85 |
    | deep_extract | 4 | $1.81 |
    | audit | 110 | $1.23 |
    | similarity | 16 | $0.20 |
    | label | 5 | $0.10 |

    The bake-off, the regression check and the pilot report's repeatability run are within
    these figures.
  - The daily cap ($300) was never reached. The design's estimate for a 1-year run was
    $202.30.
- **S9 removed from the run: deployed 17:37 PST (01:37 UTC 2026-11-25).**
  - *Code:* `LANDSCAPE_FINAL_ENABLED` (`src/landscaping/s9_switch.py`), committed on
    scripps-landscaping and pushed without tags. It is on by default, so production is
    unchanged. With it off:
    - the worker makes no S9 call;
    - activation waits only for S6 and S8;
    - review batches take opportunities in rank order and are never blind;
    - the pages read no S9 row.
  - *Review and tests:* an independent review found no remaining S9 call path. Its two
    important findings were fixed before the deploy:
    - the regression baseline after a later activation would have held no S6 items;
    - the review form still referred to a bot score.
    The full CI ran once: 11,323 passed and 1 failed, a pinned link count that the cards'
    new source links raised by one. That count was raised to match, and its file was re-run
    and passed (24 tests).
  - *Deploy* (`exec/x26-s9off.sh`):
    - the previous image was kept as the rollback image;
    - the new image's layers matched the workstation's;
    - the host's clone moved to the new commit, with prompts and the compose file unchanged;
    - `LANDSCAPE_FINAL_ENABLED=false` was added to the env file;
    - the app was recreated (healthy), then the worker was recreated and so started again.
  - *Checks:*
    - The worker had run 90 s with no restart, no error and no S9 call.
    - From outside, anonymously, the list shows 17 cards in pairwise-rank order: ranks 1, 2,
      3, 5, 6, 8 and 12–22. The gaps are ideas grouped onto a better-ranked card.
    - The cards hold 23 member ideas, each with its S6 summary and sources.
    - The batch page shows the 10 issued cards, ranks 1–15.
    - No S9 section, tile or note is shown, and there is no POST form.
    - No other project's events were seen.
  - The 17 stored S9 rows are kept and no longer read.

## 2026-11-24/25: handover (repository plan Task 10; the owner chose to hand over now)

- **Step 1: stop and dump.**
  - *Stop:* the worker was stopped at 18:27 PST (02:27 UTC 2026-11-25) with
    `exec/x25-stop-worker.sh`. The site keeps running, read-only.
  - *Spend after the restart:* $0.10, for the two re-queued extractions. The final total is
    **$156.15**.
  - *Dump:* `exec/x27-handover.sh` took a custom-format dump on the workstation,
    `copi-scripps-20261124.dump`:
    - 35,017,992 bytes;
    - SHA-256 `624702cb0a75cc05cb45366dc8f8e6edd5c386d686d3c379285236b016b9a3ba`;
    - `pg_restore --list` reads it: 42 landscape tables, archived 2026-11-25 02:27:37 UTC.
  - *What it holds:* 2,349 documents, 504 ideas, 17 opportunities and 6,577 model calls.
- **Step 2: production unchanged.** Task 0 Step 5 was repeated read-only and compared with the
  snapshot of 2026-11-20:
  - the branch, head, status hash and `.env` hash are identical;
  - the same five containers are up, with only their uptimes longer.
- **Step 3: acceptance (spec §8).**
  - [x] *Matcher and OpenAlex-rule tests pass, and the wording grep is clean.* Phase A
    (pushed 08f90a09). They passed again in every later full CI: gate 1 fixes, the open view,
    and the S9 switch.
  - [x] *ruff, the touched tests, the doc-sync check and one full `ci.sh`.* They passed for
    each code change. The last, the S9 switch, had 11,323 passed and 1 pinned count raised
    with its own change, its file re-run and passing.
  - [x] *Dry probe recorded, and 20 + 20 OpenAlex addresses read by a person.* The owner's
    gate 1 was approved 2026-11-23 on an adversarial audit. The addresses are kept outside the
    repository.
  - [x] *Every enabled source fetched.* The newest slice of every enabled source is
    complete: OpenAlex, PubMed, RePORTER, NSF, ODP patents, DataCite and GEO. The
    tech-transfer feed stays off by the owner's decision (spec §13).
  - [x] *Bake-off:* stored, checkpoint C11 passed, and the owner's mixed models were applied
    through an override report.
  - [x] *Pilot report:* stored by hand at 23:27 UTC, on the owner's instruction.
  - [x] *Backfill drained:* 22:19:43 UTC. The backfill was started by hand, on the owner's
    instruction.
  - [x] *Epoch active or overridden with a recorded reason:* overridden after the held
    regression (no actor, reason recorded), then activated 00:00:30 UTC 2026-11-25.
  - [x] *Cards shown:* 17 opportunities on the list, in pairwise-rank order, with S9 removed
    by the owner's decision.
  - [x] *One weekly batch issued:* 10 cards, 00:01 UTC 2026-11-25.
  - [x] *Spend per UTC day recorded.* The day over $100 is explained: on 2026-11-24 UTC,
    $155.80 included the bake-off and the regression check. The owner raised the daily cap
    from $100 to $300 at 09:21 PST, when the cap was about to stop the run at $88.51.
  - [x] *Production's containers, database, checkout and `.env` unchanged* (Step 2). Nothing
    in this run connects to production's database.
  - [x] *Dump file made* (Step 1). It and spec §7 go to the owner with this log.
