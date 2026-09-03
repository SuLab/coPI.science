# Review pipeline - production first use (operator checklist)

This is the checklist for the FIRST time a human reviewer's "Learn" feedback is
allowed to reach the real `claude-opus-5` model in production, after the
2026-09-02 hardening (`docs/audits/2026-09-02-review-pipeline/README.md`). Every
step below names the exact command or query to run; do not skip a verification
step because it "should" be true.

Preconditions (verify, do not assume):
- The branch's fixes are deployed, using CLAUDE.md's "Before restarting"
  build order: `docker compose -f docker-compose.prod.yml build blackbird-app
  worker`, then `docker compose -f docker-compose.prod.yml --profile agent
  build agent` (build only, nothing is started by either command). This
  branch adds no migration (still `0042`), so there is no `alembic upgrade
  head` step here; go straight to `docker compose -f docker-compose.prod.yml
  up -d --force-recreate blackbird-app worker` once both builds finish.
  Rebuilding the `agent` image is part of the same deploy, but restarting
  the simulation run afterward is a separate operator decision - do not
  couple the two.

  This build step is load-bearing, not routine. `blackbird-app`, `worker`
  and `agent` are three INDEPENDENT images (`docker-compose.prod.yml`: each
  has its own `build: context: .` and there is no shared `image:` tag). The
  D1/D2 dedupe fix (`enqueue_analysis_if_absent`,
  `src/services/assessment_reviews.py`) is reached from
  `submit_feedback`/`edit_feedback` in `src/routers/reviews.py` - the WEB
  TIER, not the worker. A worker-only rebuild, or a `blackbird-app`
  container recreated off its OLD image, leaves the pre-fix dedupe live on
  the one write path reviewers actually use, and every DB query later in
  this runbook can still read clean, by coincidence, on a quiet day. Verify
  the running `blackbird-app` container actually has the fix before
  proceeding:

  ```
  docker compose -f docker-compose.prod.yml exec -T blackbird-app python -c "import inspect, src.services.assessment_reviews as m; print('.in_(' not in inspect.getsource(m.enqueue_analysis_if_absent))"
  ```

  must print `True`. (The pre-fix function filtered on
  `Job.status.in_(("pending", "processing"))`; the fix narrows this to
  `Job.status == "pending"` only, so the literal substring `.in_(` is gone
  from the fixed function's source and present nowhere else in it - checked
  against both the old and new source before writing this down.)

  `docker logs copi-blackbird-worker-1 | grep -c "Requeued"` prints a number
  (0 is fine) - the boot sweep ran.
- `docker inspect copi-blackbird-worker-1 --format '{{.Config.StopTimeout}}'`
  prints **330**, not `<nil>`. This is the worker's `stop_grace_period` in the
  working-tree `docker-compose.prod.yml`:

  ```yaml
  # 330s = the 300s Anthropic read timeout + margin. Without this the Docker
  # default (10s) SIGKILLs a review-bot job mid-call on every deploy, leaving
  # a zombie 'processing' row (docs/audits/2026-09-02-review-pipeline/, D4).
  stop_grace_period: 330s
  ```

  A worker recreated without this value (an old container, or a compose file
  missing the working-tree edit) will still SIGKILL a mid-call job at the
  Docker default of 10 seconds and reproduce D4.
- `SELECT count(*) FROM jobs WHERE status='processing';` returns 0.
- `SELECT count(*) FROM prompt_change_suggestions;` returns 0. This checklist
  assumes a clean slate; if it does not return 0, some `learn` feedback has
  already reached the model outside this checklist and the "exactly one
  suggestion" checks below will not hold.
- You have read §7 of the audit README ("Real-model evaluation") in full,
  especially "Grader caveats" - in particular, that `canary_followed` /
  quote-presence numbers in `eval-results.json` are evaluation-script
  artifacts, not something `/manager/prompt-suggestions` shows you, and that
  roughly a quarter of real replies are not valid JSON and go through the
  malformed-JSON recovery path (see Step 2.4 below).

Step 1 - log_only path (no model call):
1. Open `/admin/assessments/<id>` for a TERMINAL assessment from a FINISHED run
   (not one the live simulation could still supersede).
2. Add feedback: score 3, mode "Don't learn - log only".
3. Verify: `SELECT count(*) FROM jobs WHERE type='review_feedback_analysis';`
   unchanged.

Step 2 - first learn job:
1. Same assessment: add feedback, score 2, mode "Learn", a concrete comment.
2. Watch `docker logs -f copi-blackbird-worker-1` until `Job <id> completed`
   (or `failed`; read `last_error` on `/admin/jobs` before concluding
   anything).
3. Verify exactly one suggestion:
   `SELECT id, target, transcript_available, input_truncated, length(suggestion) FROM prompt_change_suggestions;`
4. If the worker log shows a WARNING containing "recovered target" and a
   target name, the model's reply was not valid JSON and the
   `_LEADING_TARGET_RE` recovery path ran (`src/services/review_bot.py`). This
   is expected roughly one time in four (§7) and is not itself a failure - the
   suggestion row still has a valid `target` and a `suggestion` body built
   from the model's raw text. Read the row anyway before trusting it.
5. Open `/manager/prompt-suggestions/<id>`: every prompt file shows
   **current**, the suggestion quotes text that exists in the named file, the
   provenance table lists your one review.
6. Verify the review is consumed:
   `SELECT consumed_at IS NOT NULL FROM assessment_reviews WHERE feedback_mode='learn';`

Step 3 - second reviewer, same assessment:
1. A second staff account adds learn feedback.
2. Expect a second job, a second suggestion with a DIFFERENT
   `feedback_snapshot` id.

Step 4 - cost:
- Read the Anthropic console for the calls (there is no in-app telemetry - see
  CLAUDE.md's review-bot paragraph). Expect roughly 47k-65k input tokens
  (mean ~60k across the 12 evaluation calls in §7; 7 of those 12 came in
  above 60k) and under 8k output per call.

Standing checks (run whenever the pipeline is in use):
- `SELECT count(*) FROM jobs WHERE status='processing' AND started_at < now() - interval '30 minutes';` -> 0
- `SELECT count(*) FROM assessment_reviews r WHERE r.feedback_mode='learn' AND r.consumed_at IS NULL AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.type='review_feedback_analysis' AND j.status IN ('pending','processing') AND j.payload->>'assessment_id' = r.assessment_id::text);` -> 0

Do NOT restart or recreate the worker while a review job is `processing`.

## What to expect on the first real suggestion

Based on the 12-call live evaluation in §7 of the audit README:

- **Targets are plausible and well-grounded.** Every graded reply named a
  file-and-location that actually exists (a rubric dimension, a specialist
  persona, the PI-lab prompt, or a correct `out_of_scope` when the feedback
  carried no actionable defect), and quoted "current text" that matched the
  named file more often than not.
- **Roughly a quarter of replies need the malformed-JSON recovery.** 3 of 12
  live replies were not valid JSON; `_parse_model_output` now recovers the
  model's declared `target` from the leading key instead of silently defaulting
  to `out_of_scope`, and logs one WARNING naming the recovered target every
  time it does. Do not read that WARNING as an error - it is the fix working -
  but do read the resulting suggestion body before acting on it, since it is
  the model's raw text rather than a cleanly reformatted one.
- **Cost is about $0.30 per job**, at roughly 60k input tokens per call. There
  is no per-job cost stored anywhere in the database (see CLAUDE.md); the
  Anthropic console is the only record.
