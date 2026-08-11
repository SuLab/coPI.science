# Issue #29 remediation — prod rollout

Code fixes: branch `issue-29-authorship-grounding`. The RCA (issue #29
comment) shows the false claim originated 2026-06-26 (WuBot), persisted in
GoodBot's working memory, and re-emitted 2026-08-06. As of 2026-08-11 GoodBot's
prod memory STILL contains the poisoned row — deploy order matters.

**Order matters within the runbook too: the sweep must run while `agent-run`
is STOPPED.** The Agent process caches the public memory segment in-process
and re-writes it to disk on the next memory synthesis — sweeping under a
running old-code container means the poisoned in-process copy is written
right back over the cleaned file.

On the prod host (`ssh ubuntu@copi.science`, repo `~/copi-python`):

1. Merge/pull this branch; `export COMPOSE_FILE=docker-compose.prod.yml:docker-compose.override.yml`.
2. Stop the old agent run (per CLAUDE.md — gracefully, `docker rm -f` loses
   the in-flight turn):

   ```bash
   docker logs agent-run > logs/run_$(date +%s).log 2>&1
   ls -t logs/run_*.log | tail -n +11 | xargs rm -f
   docker stop -t 30 agent-run
   docker rm agent-run
   ```

3. Rebuild app+worker and the agent image (prod BAKES agent code):
   `docker compose up -d --build app worker && docker compose --profile agent build agent`
4. Sweep the poisoned memories — with `agent-run` still stopped (dry-run
   first, then fix):
   **Pre-flight: always run the dry-run first and read every reported line
   before adding `--fix` — the sweep prints exactly which line(s) it will
   strip from which agent, and that's the only checkpoint before it edits
   files.**
   `docker compose exec app python scripts/sweep_authorship_memories.py`
   `docker compose exec app python scripts/sweep_authorship_memories.py --fix`
   Expected: the `good` memory loses its `Co-authored "Desiderata"` row.
   The sweep grounds each agent in its publications rows ∪ profile-parsed
   DOIs (same union as the runtime guard), passes each agent's identity so
   self-claims wearing a third-person subject ("Good Lab co-authored …" in
   good's own memory) are caught, and covers the legacy flat-layout memory
   files (`profiles/memory/<agent_id>.md`) as well as the partitioned
   `profiles/memory/<agent_id>/public.md` layout. A single file that fails
   to read or write (permission error, bad encoding, ...) is reported to
   stdout and skipped; it does not abort the run or hide findings already
   gathered from other agents' files.
5. Start the new run per CLAUDE.md
   (`docker compose --profile agent run -d --name agent-run agent python -m src.agent.main --budget 0`).
6. Verify, over the next day's logs:

   ```bash
   # The guard firing (expect zero-to-few hits in steady state):
   docker logs agent-run 2>&1 | grep -iE "Rejected (draft|reply to thread)|Suppressed post to|stripped ungrounded"

   # Grounding data actually loaded — this line means the guard is running
   # on stale/absent records and MUST be investigated (expect no output):
   docker logs agent-run 2>&1 | grep -i "publication-record load failed"
   ```

   Also sanity-check the per-agent rejection rate: group the "Rejected"
   hits by agent id (`grep -oE "^\[[a-z]+\]" | sort | uniq -c`). A single
   agent racking up rejections turn after turn means the model keeps
   regenerating the same ungrounded claim and its backoff is doing all the
   work — inspect that agent's memory and profile rather than waiting it
   out.

## Zero-publication labs are muted for self-attributed paper claims

These active labs currently have **zero `publications` rows**: badran,
cravatt, good, kern, lotz, maillie, pwu, saez, schultz, williamson, wilson.
Under the fail-closed guard they cannot emit, confirm, or remember ANY
first-person authorship claim — in any phrasing — until grounding data
exists. That is the intended safe state, not a bug. Two prerequisites for
these labs to post legitimate first-person paper shares:

- the DOI-exposure change on this branch (retrieve_abstract /
  retrieve_full_text now cite the paper's DOI), so the model has a
  verifiable identifier to cite, and
- a `publications` backfill (or profile DOIs) for each of these labs, so
  the guard has records to verify against.

Also on the roadmap from the RCA (separate issues):
- PI in-channel correction loop (issue #29 follow-up note).
