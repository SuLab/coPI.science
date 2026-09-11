# PR draft — `audit-issues-closure` → `main`

**Title:** Close audit issues #20–#27: agent engine, worker & email, profiles, external clients, web, data layer, docs, deploy

## Summary

Resolves the verified-defect issues #20–#27 (opened by @ahueb, re-verified 2026-08-11) on top of `copi-prod` @ 18ba52c, then hardens the result with a second adversarial audit run live against the `copi-test` Slack workspace. Every fix ships with a test that fails on the pre-fix code. Migrations 0025–0030 are additive. No file under `prompts/` and no model-facing string changed.

## Issues addressed

**#20 — Agent engine: turn & thread state-machine correctness**
- E1 thread outcomes: a failed Slack post no longer creates a phantom row; `✅` finalizes only the most recent `:memo:` and both emoji spellings are treated alike on public and private paths; dead threads are evicted, tombstoned and deduplicated in `_prior_threads`.
- E2 cursors: the scan cursor is the log's own high-water mark, not wall clock; Phase‑3 activation carries the message-count offset so reply budgets survive restarts.
- E3 PI reviews/reopens: review clearing, PI context, reopen and the `@Bot` route are authorized by the agents the sender owns or delegates for (`agent_messages.sender_user_id`, migration 0030), persisted durably with a retry-and-flush path, and keyed identically by the rebuild and per-tick readers.
- E4 poller/log flush: the inbound poller uses a lookback window with identity dedup; a failed LLM-log flush re-queues its rows (bounded).
- E5 output hygiene: disallowed tags stripped, real `<@Uxxx>` mentions resolved, ungrounded authorship claims rejected.
- E6 budget/rate limits: mid-turn rate headroom is checked before Phase‑4 fan-out; a roster re-add restores pending proposals, subscribed channels and private profile from the DB.
- E7 liveness: a deterministic Slack refusal parks the thread after two strikes; parked threads are evicted after a bounded number of turns; the terminal stall reason is logged instead of spinning.

**#21 — Worker & background jobs**
- V11: the worker claims its canonical-id writer slot.
- V2: rollback before recording a failure; `failed` status with retry backoff; a reaper for orphaned `processing` jobs.
- V3: inbound-email poison pills are quarantined after bounded retries; an unresolvable instruction fails loudly instead of returning `False`.
- V4: notification rows are created only after SES accepts the send; every per-item handler rolls back on error; reply tokens rotate on resend and expire at the consumer after 14 days; the downgrade ladder is reachable again; bounces go only to known users within a budget.

**#22 — Profile pipeline & write integrity**
- V1: null-safe ORCID/PubMed parsing (including present-but-null containers); `(user_id, pmid)` unique constraint with data dedup (0025); PubMed `itertext()` so titles and abstracts are not truncated.
- V6: profile writes are atomic (`tempfile` + `os.replace`); an overwrite is gated on validation state; the private-profile seed is exported and the DB is authoritative over disk at startup and on roster re-add.
- C1: the two proven read-modify-write races use atomic SQL updates.

**#23 — External clients**
- V7: bounded `Retry-After`, a rate-limit wait budget (180 s, 8 attempts) with interruptible sleeps, Slack calls run off the event loop in a dedicated executor, nested `display_name`.
- V8: GrantBot never posts unvetted FOAs, refuses to post without its own token, FOA regexes agree in both directions, funding-detection edge cases pinned.
- V9: retry/backoff for ORCID, PubMed and grants HTTP; NCBI request pacing; tool counters charged only on success.
- V10: the invite import was already fixed by the merged PR stack; this branch adds the missing log line when a delegate invite has no bot token.

**#24 — Web request-path robustness**
- V5: concurrent first-action `IntegrityError`s (waitlist, vote, PI message, review, reopen) return 4xx instead of 500, proven by a two-connection test.
- C2: Slack provisioning I/O runs off the loop with a capped `Retry-After`; nginx gives the provisioning route its own 300 s timeout.

**#25 — Data layer**
- D1: PI users are deletable; the admin delete refuses to orphan an active or pending agent (owner and user rows locked).
- P2: `passive_deletes` on cascaded children (0026).
- D2: stale `running` simulation runs are reconciled at startup.
- P1: FK and composite indexes (0027) with model parity; `/static` and `/api/health` bypass the session middleware.
- P3: `pool_pre_ping` / `pool_recycle` / explicit `pool_timeout`.

**#26 — Documentation**
- DOC-A: posthog JS syntax error fixed; README, AGENT.md, CLAUDE.md, spec paths and the impersonation route corrected; `backfill_slack_ts.py` documented as a one-time repair.
- DOC-B: live roster sync handles rename, token rotation and re-add.
- DOC-C: numeric-suffix fallback for a third same-initial surname, shared by web and scripts.

**#27 — Deploy, CI & coverage gate**
- I1: mypy ratchet added beside the ruff ratchet; both ratchets have behavioural tests; CI stays local by decision (`scripts/ci.sh` via the pre-push hook).
- I2: `/api/health` probes the DB with a bounded deadline; a `migrate` one-shot gates app/worker/grantbot; `scripts/redeploy.sh` runs stop → migrate → verify → start → nginx reload.
- I3: `.dockerignore` excludes `backups/`, `.env*`, `profiles/`, tests and `.git`; the image runs as UID 10001.
- I4: `requirements.lock` with a freshness check and an opt-in install smoke test.
- I5: nginx reconciled with the app (CSP enforcing subset plus Report-Only with a sanitised `/api/csp-report`), per-service resource limits, json-file log rotation.

## Second audit (2026-09-08 → 09-10)

A live adversarial audit of the branch on `copi-test` found further defects, all fixed with red-first tests and recorded in `docs/plans/2026-09-08-audit-fixes.md`: DB-path PI authorization by thread membership (#20 COR-5); PI messages and DMs written while `agent-run` was down losing every side effect (`pi_inbound_state`, `pi_dm_messages.handled_at`, 0030); a standing instruction whose disk write failed clobbering the DB; new posts defaulting to `#general` when the model omitted the channel; a thread decision dropped after a blocking Slack retry sleep; graceful shutdown that could hang on a Slack retry or lose the final flush (single sticky shutdown event, interruptible sleeps, signal defaults restored only after the run status is committed); admin delete orphaning a live agent; health-probe retries exceeding the deadline; `COPI_PROFILES_DIR` with a live-tier preflight check.

## Verification

- `./scripts/ci.sh` at 72caa26: alembic head 0030, round trip clean, ruff 248/260, mypy 139/150, 3111 passed / 120 skipped, 80.84 % branch coverage.
- Live tier against `copi-test` (T0BMVSBMEC8) at 72caa26: 53/53 non-LLM, 8/8 real-LLM (`docs/plans/2026-09-04-decisions/task-35.md`).

## Closure

`Closes #20, #22, #23, #24, #25`. #21, #26 and #27 are closed by hand after merge with the stated carve-outs in `docs/plans/2026-09-04-decisions/README.md` (#21: the worker coverage clause's premise was already false; #26: needs deploy verification; #27: its definition of done is the gate itself). Residual risks and follow-ups are listed in `docs/plans/2026-09-02-close-issues-20-27-pr-body.md`.

## Migration and deploy

Schema goes 0024 → 0030 (0025 dedups `publications` and adds a unique index; 0026 cascades; 0027 adds 20 indexes; 0028–0030 add nullable columns). The chain runs as one transaction and takes the web app down for its window, so use the gated path, not a bare `up -d --build`.

**Runbook:** `docs/plans/2026-09-02-close-issues-20-27.md` Part R (steps R.0–R.11, ordered for the prod host), with the command reference in `docs/production-migration.md` §10.2. In outline:

1. R.1–R.3: record current state, take and verify a backup, `git pull` and build the new images without recreating anything.
2. R.4: stop every writer in order: `docker stop -t 30 agent-run`, then `docker compose $C stop grantbot worker`, then `stop app`. nginx returns 502 for the window; expected.
3. R.5: `sudo chown -R 10001:10001 profiles data` (never `prompts/`).
4. R.6: with `DATABASE_URL` exported as the in-network DSN, rehearse then apply:
   ```bash
   ./scripts/migrate/run_migration.sh --via-run --backup-verified-elsewhere "<backup ref>"          # rehearsal, writes nothing
   ./scripts/migrate/run_migration.sh --via-run --apply --backup-verified-elsewhere "<backup ref>"  # expect alembic_version = 0030, postflight 0 FAIL
   ```
   Exit 1 is a blocked preflight check: fix and re-run. `LockNotAvailableError` means a writer is still connected.
5. R.6b: `scripts/backfill_slack_ts.py --apply` once, if the workspace has never had it.
6. R.7–R.8: `./scripts/redeploy.sh $C` recreates app, worker and grantbot on the new image and reloads nginx.
7. R.9: `docker compose $C --profile agent build agent`, then start `agent-run` last.
8. R.10 is the rollback (restore the pre-deploy dump; the chain is one transaction, nothing is half-applied).

GrantBot needs its own Slack bot token before the first run. Do not squash-merge: the commit sequence is the review trail.

Closes #20, #22, #23, #24, #25

🤖 Generated with [Claude Code](https://claude.com/claude-code)
