# #26 Documentation: stale runbooks, a missing deploy prerequisite, a JS syntax error, PII hygiene (3 PRs)
state=OPEN created=2026-07-30T15:53:10Z updated=2026-08-11T23:54:40Z labels=['area:docs', 'verified-2026-07-30']

## BODY

Three documentation PRs plus two small real bugs that surfaced with them.

Originally verified at `origin/main` @ `b7edcbc` (2026-07-30). **Re-verified 2026-08-11 against the open PR-stack tip (`issue-29-authorship-grounding` @ `b1d54da` = main + #30/#31/#32); several items are overtaken by the stack — marked below.**

## Priority (triage 2026-08-11)

Low tier as a bundle, with two exceptions worth pulling into the next web PR: **DOC-5's template syntax error** (trivial) and **DOC-C's missing numeric-suffix fallback** (a live 500 on a double last-name collision). DOC-7's remaining gap is one paragraph in CLAUDE.md/README.

### PR DOC-A — Stale-doc, runbook & JS-bug bundle *(trivial)*
- **DOC-7 — partially addressed by the stack.** The mechanism is unchanged (`_restored_slack_ts` refuses inference, `simulation.py:94-118`; `_slack_parent_ts` returns `None` for legacy rows → replies silently kept off Slack), but `docs/production-migration.md` §8 **Step 8** now makes `scripts/backfill_slack_ts.py --apply` an ordered pre-deploy step, preflight check 11 pre-splits recoverable vs unrecoverable rows, and `scripts/migrate/run_migration.sh` prints the reminder. **Remaining:** `CLAUDE.md` and `README.md` still contain zero mention of it, and the runbook scopes itself to starting points 0018–0021 — a workspace already at head that still carries pre-Stage-6 `slack_ts IS NULL` rows never enters that document. *Fix:* add the one-time repair paragraph to the CLAUDE.md/README restart runbooks. While in `specs/local-db-conversations.md`, also fix the "Three processes mint" count at `:66` — see PR V11 in issue #21 (it is now wrong three ways over).
- **DOC-1** — still present. `README.md:98-99` says to create a Slack bot token per agent in env config and add PIs to `PILOT_LABS` in `src/agent/simulation.py` (0 matches in `src/`). Both stale — the roster and tokens are DB-driven via `AgentRegistry` (contradicted by CLAUDE.md's own correct flow).
- **DOC-3** — still present, all four: stale GitHub URL (`AGENT.md:7`); wrong impersonation path (`:76` says `/api/admin/impersonate`; real: `POST /admin/impersonate`, `admin.py:1057` mounted under `/admin`); email listed "out of scope" (`:23`) though fully built; lab counts mutually inconsistent (`AGENT.md:9` "10 labs" vs `:105/:118` "8 pilot labs" vs `README.md:8` "14+"; `orcids.txt` has 48 ORCIDs).
- **DOC-4** — still present. `specs/tech-stack.md:213` cites `src/models/llm_call_log.py`; the model lives at `src/models/agent_activity.py:175`.
- **DOC-5** — still present. `templates/base.html:20`: the `posthog.identify` object literal is missing its closing `}` before `)` — a JS syntax error whenever it renders for a logged-in user on a deployment with PostHog configured (double-gated, so not universal, but real). Dead prompts both confirmed still dead: `prompts/email-reply-classify.md`'s only reference is a spec (the live classifier builds its prompt inline, `email_inbound.py:413-422`); `prompts/daily_audit.md`'s consumer `audit_recipient_list` (`config.py:439`) has zero call sites — the prod-side `9ab5555` edit changed the prompt's content, not whether anything reads it. Lab counts disagree across docs (see DOC-3).

### PR DOC-B — Roster-sync: one real gap left *(small)*
Largely overtaken by the stack. The token-pickup half is **fixed**: `_sync_roster_from_db` now runs a token-diff loop for surviving agents before the membership early-return (`simulation.py:4455-4495`), so setting a token after boot (post-activation `connect_slack`) goes live without a restart — and CLAUDE.md's no-restart promise, which is scoped to activation/token changes only, is now accurate as written. **Still true:** `bot_name`/`pi_name` edits are not live — the surviving-agent loop diffs `role` only (`:4448-4454`) and names are read only in the `to_add` branch (`:4540`), so renaming a live agent needs a restart, which no doc states. *Fix:* either pick up name edits in the sync (small) or document the restart requirement.

### PR DOC-C — Collision path + scripts hygiene *(small)*
- **Numeric-suffix fallback — still missing (live 500).** `derive_agent_identity` (`agent_page.py:396-414`) goes bare stem → first-initial prefix → stop; a second collision hits the `agent_id` unique constraint on commit (`:435-446`) → unhandled 500 on `POST /agent/request`. `scripts/backfill_agents.py:47-69` has the numeric loop, and its docstring **falsely claims parity** with the web path. *(The bot-name half is fixed in the stack: the web path now mints the initial-prefixed bot name, e.g. `PWuBot`.)*
- **`generate_sparsedata_user.py`** — still imports the private `_extract_json` (`:57`). *(The PII-CSV half is fixed in the stack: output moved to `scripts/_sparse_run_*.csv` and `.gitignore:96` covers `scripts/_*`.)*
- **`build_cabo_sankey.py:35`** — still bakes the stale `2026-05-01` default.

*Fix:* port the numeric fallback to the web path (and correct the backfill script's docstring); import a public JSON helper; parameterize the date.

**Definition of done:** DOC-5's template fix ships with a rendering assertion. DOC-7 is verified by following the runbook end to end on a workspace with legacy rows.


