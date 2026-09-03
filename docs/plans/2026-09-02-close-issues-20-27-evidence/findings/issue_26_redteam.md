# Issue #26 — red-team pass (second reviewer)

Tree: `/home/a/scripps/coPI.science` @ `copi-prod` 18ba52c, 2026-09-02. Read-only. Own greps; own jinja2 +
`node --check` render (`scratchpad/26/rt_render.py`); own fake-DB collision harness (`scratchpad/26/rt_collide.py`);
`git show <commit>^:<path>` for pre-fix code.

## 1. Summary table

| id | first-agent verdict | red-team | reason | evidence |
|---|---|---|---|---|
| A1 | STILL PRESENT (by design) | UPHELD | `_restored_slack_ts` → `return row.slack_ts` `:117`; `_slack_parent_ts` `:3463` → `return root.slack_ts` `:3479` | `src/agent/simulation.py` |
| A2 | CONFIRMED | UPHELD | preflight-11 text `:411`, `### Step 8` `:420`, both `backfill_slack_ts.py` commands `:423-424`; runbook reminder `run_migration.sh:42,320,322` | `docs/production-migration.md` |
| A3 | STILL PRESENT | UPHELD | `grep -n -i "slack_ts\|backfill_slack" CLAUDE.md README.md` → exit 1 | — |
| A4 | STILL PRESENT | UPHELD | "Supported starting points: **0018** … **0021**. All four are tested end to end." | `docs/production-migration.md:7-8` |
| A5 | STILL PRESENT | UPHELD | spec `:66-67` "Three processes mint"; `ids.py:47-50` four slots; `ids.py:111 _default = TsMinter(WRITER_WEB)`; `worker/main.py` has 0 `src.agent.ids` imports yet reaches minting via `:165 poll_inbound_emails` → `email_inbound.py:636-638 migrate_public_thread_to_private` (`private_channels.py:40,269 mint_local_ts`) and `:671 record_pi_message` (`pi_inbox.py:15,120,151`) | grep |
| A6 | STILL PRESENT | UPHELD | `README.md:97-100` step 3/4 (`PILOT_LABS`); `grep -rn PILOT_LABS src/` → 0 | sed |
| A6-extra (first agent) | README:87-88 "mounts source code" stale; compose cmds omit prod `-f` | UPHELD | `README.md:87-88` vs `CLAUDE.md:98,108` ("prod **bakes** the source"); `README.md:63-64` bare `docker compose --profile agent run …` | sed |
| A7 | STILL PRESENT | UPHELD | `AGENT.md:7 andrewsu/coPI-python-opus`; remote `SuLab/coPI.science` | — |
| A8 | STILL PRESENT | UPHELD | `AGENT.md:76`; `admin.py:1076,1135` + `main.py:146 prefix="/admin"`; `api/admin` 0 hits in src/templates/tests | grep |
| A9 | STILL PRESENT | UPHELD | `AGENT.md:19-24` lists "Notifications (email)" out of scope | sed |
| A10 | STILL PRESENT | UPHELD | `AGENT.md:9` 10 labs, `:17` 8 bots, `:105`/`:118` 8 pilot labs; `README.md:8` 14+; `orcids.txt` 48 lines / 48 unique ORCIDs | grep |
| A11 | STILL PRESENT | UPHELD | `specs/tech-stack.md:213 llm_call_log.py`; file absent; `class LlmCallLog` at `agent_activity.py:175` | — |
| A12 | STILL PRESENT (mechanical) | **UPHELD — independently reproduced** | see §2; both branches `node --check` exit 1 `SyntaxError: Unexpected token ')'` | `rt_render.py` |
| A13 | STILL PRESENT | UPHELD | `classify_reply` `email_inbound.py:449`, inline `system_prompt` `:456`; only non-prompt ref found: `specs/email-proposal-review.md:266` | grep |
| A14 | PARTIAL (fact right, "dead" wrong) | UPHELD | `prompts/daily_audit.md:1-4` "Prompt for a scheduled Claude Code agent"; `:154,171,177` read `s.audit_recipient_list`; `config.py:154` comment names the prompt as the reader; cron referenced `docs/specs/2026-08-18-…:457,472`. Zero `src/` call sites is true (`config.py:453-455` only); "dead" is not a fair label — the consumer is out-of-band by design. Cron install itself unverifiable (no ssh) | grep |
| B1 | FIXED | **QUALIFIED** | fix real (`d1c440e` is an ancestor; adopt loop `simulation.py:4586-4610` runs before the early-return `:4614-4623`; test `test_roster_sync.py:153` deletes the client then asserts re-adoption → fails on pre-fix code which had no such loop). **But** `:4590 if r is None or aid in self.slack_clients: continue` — a *rotated* token on an already-connected agent is never adopted, and `:193 test_existing_client_is_not_rebuilt` pins that. `CLAUDE.md:115` "setting a new `slack_bot_token` … picked up live" is accurate only for tokenless→token, not for rotation | sed |
| B2 | STILL PRESENT | UPHELD (mis-cited lines) | role-only diff at `:4573-4576` (first agent: 4561-4567); names selected `:4540-4548`, consumed only in `to_add` `:4657,4661` | awk |
| C1 | STILL PRESENT (mechanical) | **UPHELD — independently reproduced** | see §2: 3rd same-initial Wu → `('pwu','PWuBot')` COLLIDES; handler `agent_page.py:433-443` `db.add`/`commit` no try/except; `agent_id unique=True` `agent_registry.py:19`. The third signup in `test_agent_page.py:347-349` is a *control* (Ada Zephyr → `zephyr`), not a triple collision | `rt_collide.py` |
| C2 | STILL PRESENT | UPHELD + sharpened | docstring `backfill_agents.py:48` claims parity with `agent_page.py`; numeric loop `:62-68`. `_bot_name_for("wu2")` → `WWuBot` **and** `_bot_name_for("wu3")` → `WWuBot` (reproduced); `bot_name` has no unique constraint (`agent_registry.py:26`) so no DB error, but `simulation.py:4661 _bot_name_to_id[bot_name.lower()]` would silently overwrite one agent's entry | run |
| C3 | FIXED | UPHELD | `02143de` ancestor; pre-fix `git show 02143de^:src/routers/agent_page.py:391 bot_name=f"{current_user.name.split()[-1]}Bot"` (→ `WuBot` for Peng Wu); `test_signup_collision_also_disambiguates_the_bot_name` asserts `"PWuBot"` → fails pre-fix | git show |
| C4 | STILL PRESENT | UPHELD | `generate_sparsedata_user.py:57 from src.services.llm import _extract_json`, used `:500` | — |
| C5 | FIXED (issue's history wrong) | UPHELD | `git log -S"_sparse_run_" -- scripts/generate_sparsedata_user.py` → only `4a05397` 2026-06-06; `git show b7edcbc:…` has `_sparse_run_` at `:29,:854`; `git show b7edcbc:.gitignore \| grep scripts/_` → none; now `.gitignore:99 scripts/_*`; `git check-ignore -v scripts/_sparse_run_x.csv` → `.gitignore:99:scripts/_*`; CSVs ever added to git: 0 | git |
| C6 | CHANGED / mostly N/A | UPHELD | `DEFAULT_START = "2026-05-01"` `:35` under "Defaults preserve the original Cabo behavior" `:34`; `--start` `:145`, `_parse_start` `:137,152`; added `06c7ba5` 2026-06-06 (pre-baseline) | git log -S |
| N-creators (coordinator ask) | — | NEW (info) | `AgentRegistry(` constructed only at `agent_page.py:435`, `scripts/backfill_agents.py:111`, `scripts/generate_sparsedata_user.py:619`; both scripts use the bare→initial→numeric loop (`backfill_agents.py:47-69`, `generate_sparsedata_user.py:166-185`); cohort_seed/cli/admin create none (`grep -rn "AgentRegistry(\|insert(AgentRegistry" src/ scripts/`) — so the web path is the only creator without the numeric fallback | grep |

## 2. Detail

### A12 — own render (`rt_render.py`, jinja2 autoescape=False, node v20.20.0)

```
[with-email] posthog.identify('1111…5555', {name: 'Andrew Su', email: 'a@b.org');   braces {=1 }=0  node --check exit=1
[no-email]   posthog.identify('1111…5555', {name: 'Andrew Su');                     braces {=1 }=0  node --check exit=1
SyntaxError: Unexpected token ')'
gating vars in block: ['request.state.posthog_api_key', 'current_user', 'current_user.email']
```
Gating: outer `{% if request.state.posthog_api_key %}` (`base.html:14`; set at `src/main.py:29` from
`Settings.posthog_api_key`, default `""` at `src/config.py:293`) → inner `{% if current_user %}` (`:19`) → the
broken `<script>` at `:20`. The `posthog.init` block (`:15-18`) is a separate `<script>` and parses fine, so the
failure is silent (no identify call), never a page error. Fires on every logged-in page of any deployment with
`POSTHOG_API_KEY` set. Whether prod sets it: not verifiable here.

### C1 — own fake-DB harness against the real `derive_agent_identity`

```
Chunlei Wu   taken=[]            -> (wu,WuBot)    queries=['wu'] ok
Peng Wu      taken=['wu']        -> (pwu,PWuBot)  queries=['wu'] ok
Pei Wu       taken=['pwu','wu']  -> (pwu,PWuBot)  queries=['wu'] COLLIDES
Ping Wu      taken=['pwu','wu']  -> (pwu,PWuBot)  queries=['wu'] COLLIDES
```
One query only (for the bare stem, `agent_page.py:407-409`); the prefixed candidate is never checked and there is no
numeric branch. Handler (`:433-443`): `agent_id, bot_name = await derive_agent_identity(...)` → `AgentRegistry(...)`
→ `db.add(agent)` → `await db.commit()`, no `try/except`; the only `IntegrityError` handlers in the file
(`:1018,1022`) are in the PI-inbox path. Unique violation → uncaught → 500. Verdict upheld.

### B1 — why QUALIFIED

`simulation.py:4586-4610` (inside `_sync_roster_from_db`, before `to_add`/early-return at `:4613-4623`):
```
4590    if r is None or aid in self.slack_clients:
4591        continue
4593-4597   token = r.slack_bot_token if is_valid_token(...) else env_token(aid); if not valid: continue
4607    "[roster] Adopted Slack client for %s (token provisioned after startup)"
```
Tokenless→token is live (the fix, and the test at `:153-178` would fail without it — it removes the client for
`late` and asserts `"late" in engine.slack_clients` after a sync). Token *rotation* for a connected agent is
skipped by `:4590`, and `:193 test_existing_client_is_not_rebuilt` asserts that skip. So the first agent's
"CLAUDE.md's no-restart promise … is now accurate as written" over-reads `CLAUDE.md:115` ("setting a new
`slack_bot_token`"): a rotated token still needs a restart, and no doc says so. FIXED stands for the issue's
claim (post-activation pickup); the doc-accuracy sub-claim is only half right.

### C2 — `_bot_name_for` sharpening

`scripts/backfill_agents.py:72-77`: for any `agent_id` ≠ bare last name it returns
`f"{agent_id[0].upper()}{last_alpha.capitalize()}Bot"`. Reproduced: `wu`→`WuBot`, `pwu`→`PWuBot`,
`wu2`→`WWuBot`, `wu3`→`WWuBot`. `bot_name` is `String(100), nullable=False` with **no** unique constraint
(`agent_registry.py:26`), so two numeric-suffix agents would both be `WWuBot` without a DB error; the engine's
`_bot_name_to_id[bot_name.lower()]` (`simulation.py:4661`) is a plain dict, so the later one overwrites the
earlier — a third divergence from the web path's naming (first agent noted the `WWuBot` shape; the duplicate
case and the missing uniqueness are new).

## 3. Mis-cites in the first agent's report

- B1 `simulation.py:4569-4601` → the adopt loop is `:4586-4610` (comment block starts `:4578`).
- B2 `simulation.py:4561-4567` → role diff is `:4573-4576`. `:4540-4546` query → `:4540-4548`. `:4657`, `:4661` exact.
- Everything else spot-checked exact: `base.html:14-22`, `main.py:29`, `config.py:293,453-455`,
  `agent_page.py:393-414,433-443,1018-1022`, `agent_registry.py:19`, `backfill_agents.py:47-69,72-77`,
  `generate_sparsedata_user.py:57,166-185,500,854`, `build_cabo_sankey.py:34-37,137,145,152`, `.gitignore:98-99`,
  `AGENT.md:7,9,17,19-24,76,105,118`, `README.md:8,87-88,97-100`, `tech-stack.md:213`, `agent_activity.py:175`,
  `local-db-conversations.md:66-67`, `ids.py:47-50,111`, `email_inbound.py:449,456,636-638,671`,
  `admin.py:1076,1135`, `main.py:146`, `production-migration.md:7-8,411,414,420-424`,
  `run_migration.sh:42,320-322`, `test_agent_page.py:333-355`, `test_roster_sync.py:153,180,193`.
- No wrong verdicts found. A13's second (docs-plan) reference was not re-found by my grep; immaterial.

## 4. Counts

Rows evaluated: 25 (22 first-agent table rows + A6-extra + C2-sharpening + creators check).
**UPHELD 24 · OVERTURNED 0 · QUALIFIED 1 (B1: fix real, but "CLAUDE.md accurate as written" over-reads — rotation
of a connected agent's token is not live) · UNVERIFIABLE 0.**
All three FIXED verdicts (B1 mechanism, C3, C5) survive: each fix commit is an ancestor of HEAD and each cited
test would fail on the pre-fix code.
