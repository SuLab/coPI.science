# Issue #29 remediation — prod rollout

Code fixes: branch `issue-29-authorship-grounding`. The RCA (issue #29
comment) shows the false claim originated 2026-06-26 (WuBot), persisted in
GoodBot's working memory, and re-emitted 2026-08-06. As of 2026-08-11 GoodBot's
prod memory STILL contains the poisoned row — deploy order matters.

On the prod host (`ssh ubuntu@copi.science`, repo `~/copi-python`):

1. Merge/pull this branch; `export COMPOSE_FILE=docker-compose.prod.yml:docker-compose.override.yml`.
2. Rebuild app+worker and the agent image (prod BAKES agent code):
   `docker compose up -d --build app worker && docker compose --profile agent build agent`
3. Sweep the poisoned memories (dry-run first, then fix):
   **Pre-flight: always run the dry-run first and read every reported line
   before adding `--fix` — the sweep prints exactly which line(s) it will
   strip from which agent, and that's the only checkpoint before it edits
   files.**
   `docker compose exec app python scripts/sweep_authorship_memories.py`
   `docker compose exec app python scripts/sweep_authorship_memories.py --fix`
   Expected: the `good` memory loses its `Co-authored "Desiderata"` row.
   The sweep now also covers the legacy flat-layout memory files
   (`profiles/memory/<agent_id>.md`), not just the partitioned
   `profiles/memory/<agent_id>/public.md` layout — both are scanned and
   reported separately if an agent has both. A single file that fails to
   read or write (permission error, bad encoding, ...) is reported to
   stdout and skipped; it does not abort the run or hide findings already
   gathered from other agents' files.
4. Restart `agent-run` per CLAUDE.md (save logs → `docker stop -t 30 agent-run` →
   `docker rm` → start). Required both for the new image AND because Agent
   caches the memory segment in-process — the sweep is invisible until restart.
5. Verify: `docker logs agent-run` shows a roster sync; then
   `grep -i "Rejected draft\|stripped ungrounded"` over the next day's logs to
   confirm the guard is live (expect zero-to-few hits in steady state).

Also on the roadmap from the RCA (separate issues):
- PI in-channel correction loop (issue #29 follow-up note).
- Backfill `publications` rows for labs with 0 records (Good lab is one) so
  fail-closed doesn't permanently mute their legitimate paper posts.
