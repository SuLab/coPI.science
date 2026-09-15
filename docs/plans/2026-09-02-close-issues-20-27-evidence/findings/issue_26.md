# Issue #26 verification — Documentation: stale runbooks, deploy prerequisite, JS syntax error, PII hygiene

Verified against `copi-prod` @ `18ba52c` (clean tree), 2026-09-02. All line numbers below are CURRENT.
Method: symbol grep, quoted code, `git log -S` for fix commits, mechanical checks run with
`.venv-test/bin/python` and `node v20.20.0` (host). No repo edits, no docker, no network.

## 1. Summary table

| id | claim (one line) | verdict | key evidence | conf |
|---|---|---|---|---|
| A1 | DOC-7: mechanism unchanged — `_restored_slack_ts` refuses inference, `_slack_parent_ts` returns None for legacy rows | STILL PRESENT (by design) | `src/agent/simulation.py:94-117` (`return row.slack_ts`), `:3463-3479` | high |
| A2 | DOC-7: `docs/production-migration.md` Step 8 orders `backfill_slack_ts.py --apply`; preflight 11 pre-splits rows; `run_migration.sh` prints reminder | CONFIRMED (as issue states) | `docs/production-migration.md:411-424`; `scripts/migrate/run_migration.sh:42,320-322` | high |
| A3 | DOC-7: CLAUDE.md and README.md have zero mention of the repair | STILL PRESENT | `grep -n slack_ts CLAUDE.md README.md` → 0 hits | high |
| A4 | DOC-7: runbook scoped to starting points 0018–0021; already-at-head workspace never enters it | STILL PRESENT | `docs/production-migration.md:7-8` | high |
| A5 | DOC-7: `specs/local-db-conversations.md:66` "Three processes mint" is wrong | STILL PRESENT | spec `:66-67`; `src/agent/ids.py:47-50` (4 slots); worker mints via `pi_inbox.py:15`/`private_channels.py:40` w/o claiming a slot (`worker/main.py` no import) | high |
| A6 | DOC-1: README says token-per-agent in env + add to `PILOT_LABS`; 0 matches in `src/` | STILL PRESENT | `README.md:97-100`; `grep -rn PILOT_LABS src/` → 0 | high |
| A7 | DOC-3: stale GitHub URL | STILL PRESENT | `AGENT.md:7` vs `git remote -v` = `SuLab/coPI.science` | high |
| A8 | DOC-3: wrong impersonation path `/api/admin/impersonate` | STILL PRESENT (line drift) | `AGENT.md:76`; real `POST /admin/impersonate` = `admin.py:1076` + `main.py:146` prefix; `templates/admin/users.html:13` | high |
| A9 | DOC-3: email listed "out of scope" though built | STILL PRESENT | `AGENT.md:19-24` ("Notifications (email)"); `src/services/email{,_inbound,_notifications}.py`; `README.md:14-15` | high |
| A10 | DOC-3: lab counts mutually inconsistent; orcids.txt has 48 | STILL PRESENT | `AGENT.md:9` "10 labs", `:17` "8 bots", `:105`,`:118` "8 pilot labs", `:121-133` 10-row table; `README.md:8` "14+"; orcids.txt = 48 unique | high |
| A11 | DOC-4: tech-stack cites `src/models/llm_call_log.py`; model lives in `agent_activity.py` | STILL PRESENT | `specs/tech-stack.md:213`; `src/models/agent_activity.py:175 class LlmCallLog`; no `llm_call_log.py` in `src/models/` | high |
| A12 | DOC-5: `base.html` `posthog.identify` object literal missing `}` → JS SyntaxError | STILL PRESENT (mechanically confirmed) | `templates/base.html:20`; jinja2 render + `node --check` → `SyntaxError: Unexpected token ')'` both branches | high |
| A13 | DOC-5: `prompts/email-reply-classify.md` dead; live classifier inlines its prompt | STILL PRESENT (line drift) | only refs `specs/email-proposal-review.md:266` + a docs plan; `email_inbound.py:449-475` inline | high |
| A14 | DOC-5: `prompts/daily_audit.md` dead; `audit_recipient_list` has zero call sites | PARTIAL — fact true, framing wrong | `config.py:453` 0 callers in `src/`; but `daily_audit.md:1-3,150-155` is a host-cron Claude prompt that reads the property by design; cron referenced `docs/specs/2026-08-18-postgres-backup-verification-design.md:456-457` | high |
| B1 | DOC-B: token-pickup for surviving agents FIXED; CLAUDE.md no-restart promise accurate | FIXED (confirmed) | `d1c440e` (2026-08-06); `simulation.py:4573-4601`; `tests/unit/test_roster_sync.py:155-190` | high |
| B2 | DOC-B: `bot_name`/`pi_name` edits not live — surviving loop diffs `role` only | STILL PRESENT | `simulation.py:4561-4567` (role only), `:4657` (names only in `to_add`); `agent.py:73-74`; no doc mentions it | high |
| C1 | DOC-C: numeric-suffix fallback missing in `derive_agent_identity`; 2nd collision → unhandled 500 | STILL PRESENT (mechanically confirmed) | `agent_page.py:393-414`; handler `:433-443` commits w/o guard; `agent_registry.py:19 unique=True`; fake-DB run returns colliding `('pwu','PWuBot')` | high |
| C2 | DOC-C: `backfill_agents.py` has numeric loop; docstring falsely claims parity with web path | STILL PRESENT | `scripts/backfill_agents.py:47-69`; `:48` names `agent_page.py` | high |
| C3 | DOC-C: bot-name half fixed (web mints `PWuBot`) | FIXED (confirmed) | `02143de` (2026-08-04); `agent_page.py:411-414`; `tests/integration/test_agent_page.py:350-355` | high |
| C4 | DOC-C: `generate_sparsedata_user.py` imports private `_extract_json` | STILL PRESENT | `scripts/generate_sparsedata_user.py:57`; no public helper exists (`llm.py:122`, dup at `simulation.py:5467`) | high |
| C5 | DOC-C: PII-CSV half fixed — output "moved" to `scripts/_sparse_run_*.csv`, `.gitignore:96` covers `scripts/_*` | FIXED, but issue's history is wrong | path was ALWAYS `scripts/_sparse_run_*` (`4a05397`, 2026-06-06, pre-baseline); the fix is the `.gitignore` rule at `:99` from `c46918a` (2026-08-03); no CSV ever committed | high |
| C6 | DOC-C: `build_cabo_sankey.py:35` bakes stale `2026-05-01` default; fix "parameterize the date" | CHANGED / mostly N/A | `DEFAULT_START = "2026-05-01"` at `:35` is only a default; `--start` argparse exists at `:145-146` since `06c7ba5` (2026-06-06, pre-baseline); `:34` comment says default preserves Cabo behaviour | high |

**Counts:** 14 still present (A1, A3–A13 incl. A14's factual half, B2, C1, C2, C4) · 3 fixed (B1, C3, C5) · 1 partial (A14) · 1 changed (C6) · 0 not reproducible · 1 confirmed-as-stated (A2).

(Counting A14 once as partial: 13 still present, 3 fixed, 1 partial, 1 changed, 1 confirmed.)

---

## 2. Per-item detail

### PR DOC-A

#### A1 — `_restored_slack_ts` / `_slack_parent_ts` mechanism (STILL PRESENT, by design)

`src/agent/simulation.py:94-117`:
```python
def _restored_slack_ts(row: AgentMessage) -> str | None:
    """Slack ts for a restored ``agent_messages`` row, or None if it has none.
    ...
    Legacy rows are repaired by ``scripts/backfill_slack_ts.py``, a one-time pass
    that asks Slack which timestamps actually exist rather than assuming. Run it
    before deploying this change on a workspace with pre-Stage-6 history.
    """
    return row.slack_ts
```
`:3463-3479` `_slack_parent_ts`: `root = self.message_log.get_entry(thread_ts); if root is None: return thread_ts; return root.slack_ts` — a legacy row with `slack_ts IS NULL` returns `None`, and callers (`:3343`) skip the mirror. Issue's `:94-118` citation is still accurate. This is the intended design, not a defect; the defect is documentation (A3/A4).

#### A2 — Runbook Step 8 / preflight 11 / run_migration.sh reminder (CONFIRMED)

`docs/production-migration.md:411-412` "Preflight check 11 splits legacy rows into Slack-recoverable and permanently unrecoverable"; `:420-424` "### Step 8 — repair the Slack mirror mapping" with the `backfill_slack_ts.py` report/`--apply` commands. `scripts/migrate/run_migration.sh:42` (comment: "It does not run scripts/backfill_slack_ts.py"), `:320-322` prints the two commands as a reminder. Issue is accurate here.

#### A3 — CLAUDE.md / README.md silence (STILL PRESENT)

`grep -n -i "backfill_slack_ts\|slack_ts" CLAUDE.md README.md` → **0 hits**. CLAUDE.md's "Before restarting" runbook (`CLAUDE.md:79-103`) and README's (`README.md:74-85`) both go save-logs → stop → rebuild → start with no one-time repair paragraph.

Partial mitigations outside the runbooks: `specs/local-db-conversations.md:57-59` and `scripts/backfill_slack_ts.py:16-17` both say "run it before deploying on a workspace with pre-Stage-6 history" — but an operator following CLAUDE.md never sees either.

#### A4 — Runbook scope 0018–0021 (STILL PRESENT)

`docs/production-migration.md:7-8`: "Supported starting points: **0018** (`main` before PR19), **0019**, **0020** and **0021**. All four are tested end to end." `grep -n -i "already" docs/production-migration.md` finds no "already at head" case. A workspace at 0024 with pre-Stage-6 NULL rows has no entry point into the document.

#### A5 — "Three processes mint" (STILL PRESENT)

`specs/local-db-conversations.md:66-67`: "Three processes mint into the same run — the engine, the web app and GrantBot — so each minter also owns a **writer slot**".

Reality:
- `src/agent/ids.py:47-50` defines **four** slots: `WRITER_ENGINE=0`, `WRITER_WEB=1`, `WRITER_GRANTBOT=2`, `WRITER_ENGINE_AUX=3`.
- `set_default_writer_id` is claimed in `src/main.py:113` (WEB), `src/agent/main.py:54` (ENGINE_AUX), `src/agent/grantbot.py:731,764` (GRANTBOT). `src/worker/main.py` has **no** `src.agent.ids` import.
- The **worker** process mints too: `email_inbound.py:636-638` → `private_channels.migrate_public_thread_to_private` (`private_channels.py:40` imports `mint_local_ts`, `:270` writes `AgentMessage`) and `email_inbound.py:671-674` → `pi_inbox.record_pi_message` (`pi_inbox.py:15,121`). It inherits the module default `TsMinter(WRITER_WEB)` (`ids.py:111`), i.e. shares residue class 1 with the web app.
So the count is wrong on at least: number of slots (4 not 3), number of minting processes (4 incl. worker), and the worker's unclaimed slot. This matches issue #21 PR V11's description (`issues/issue_21.md:17-24`), which I read for context only.

#### A6 — DOC-1 README `PILOT_LABS` (STILL PRESENT)

`README.md:97-100`:
```
3. Create a Slack bot token per agent and add to env config.
4. Add to `PILOT_LABS` in `src/agent/simulation.py` and restart the
   simulation.
```
`grep -rn PILOT_LABS src/` → 0. The only repo mentions are comments saying it no longer exists (`scripts/backfill_agents.py:157`, `scripts/generate_sparsedata_user.py:31,864`, `CLAUDE.md:121`, and cohort design docs citing `PILOT_LABS @ 0ef4741` as a historical source). Contradicts `CLAUDE.md:119-124`.

**Extra stale item the issue missed:** `README.md:87-88` "The `agent-run` container mounts source code but only loads modules at startup — code changes … require a restart" contradicts `CLAUDE.md:107-111` (prod bakes source; requires `build agent`). README's compose commands (`README.md:62-85`) also omit the prod `-f` flag set that CLAUDE.md says is mandatory.

#### A7 — AGENT.md GitHub URL (STILL PRESENT)

`AGENT.md:7`: `**GitHub:** https://github.com/andrewsu/coPI-python-opus`. `git remote -v` → `git@github-work:SuLab/coPI.science.git`. Last touch of AGENT.md is `4df7105` (pilot-lab add); never updated.

#### A8 — Impersonation path (STILL PRESENT; issue line drifted)

`AGENT.md:76`: "Admin impersonation routes placed at `/api/admin/impersonate` (POST) and `/api/admin/impersonate/stop` (POST) rather than inside the `/admin` router prefix."
Real: `src/routers/admin.py:53 router = APIRouter()`, `:1076 @router.post("/impersonate")`, `:1135 @router.post("/impersonate/stop")`; mounted `src/main.py:146 include_router(admin.router, prefix="/admin")`. Templates agree: `templates/admin/users.html:13 action="/admin/impersonate"`, `templates/base.html:33 action="/admin/impersonate/stop"`. `grep -rn "api/admin" src/ templates/ tests/` → 0. Issue cited `admin.py:1057`; now `:1076`.

#### A9 — Email "out of scope" (STILL PRESENT)

`AGENT.md:19-24`:
```
## What's Out of Scope
- Matching engine (pairwise proposal generation)
- Swipe interface
- Notifications (email)
- Daily digest
```
Built: `src/services/email.py`, `email_inbound.py`, `email_notifications.py`; `src/worker/main.py:144-154` sends proposal emails; `src/models/email_notification.py`; `README.md:14-15` advertises "email-reply intake".

#### A10 — Lab counts (STILL PRESENT)

`AGENT.md:9` "**Pilot:** 10 labs at Scripps Research"; `:17` "Slack agent system (8 bots, simulation engine)"; `:105` "one of the 8 pilot labs"; `:118` "Agent profiles (8 pilot labs …)"; `:121-133` "Pilot Lab ORCIDs" table has 10 rows. `README.md:8` "Currently piloting with 14+ labs". `orcids.txt`: 106 lines, 48 non-comment/non-blank lines, 48 unique ORCID-pattern matches. CLAUDE.md states no count. Issue's "48 ORCIDs" is correct.

#### A11 — DOC-4 `LlmCallLog` location (STILL PRESENT)

`specs/tech-stack.md:213`: `│   └── llm_call_log.py     # LlmCallLog`. `ls src/models/` has no `llm_call_log.py`; `grep -rni "class LlmCallLog" src/` → `src/models/agent_activity.py:175`. `tech-stack.md:210`'s comment for `agent_activity.py` lists only "SimulationRun, AgentMessage, AgentChannel". Issue text writes `LLMCallLog` — wrong casing (actual: `LlmCallLog`); the spec's casing is right, only the file is wrong.

#### A12 — DOC-5 `posthog.identify` syntax error (STILL PRESENT, mechanically confirmed)

`templates/base.html:14-22`:
```html
{% if request.state.posthog_api_key %}
<script> … posthog.init('{{ request.state.posthog_api_key }}', {…}) </script>
{% if current_user %}
<script>posthog.identify('{{ current_user.id }}', {name: '{{ current_user.name }}'{% if current_user.email %}, email: '{{ current_user.email }}'{% endif %});</script>
{% endif %}
{% endif %}
```
Ran (`scratchpad/26/render_identify.py`): extracted the `<script>` line, rendered with jinja2 3.1.6 (`autoescape=False`) for a fake user with and without email, `node --check` on each:
```
[with-email] posthog.identify('1111…', {name: 'Andrew Su', email: 'a@b.org');   braces open=1 close=0
             node --check exit=1  SyntaxError: Unexpected token ')'
[no-email]   posthog.identify('1111…', {name: 'Andrew Su');                     braces open=1 close=0
             node --check exit=1  SyntaxError: Unexpected token ')'
```
Gating: `request.state.posthog_api_key` is set in `src/main.py:29` from `Settings.posthog_api_key` (default `""`, `src/config.py:293`) → the block only renders when a key is configured AND `current_user` is truthy. Introduced in `fb73227` ("Add PostHog analytics with first-party proxy"); `git log -S"posthog.identify"` shows no later change. Effect when it fires: that `<script>` block fails to parse, so `identify` never runs (the earlier `posthog.init` block is a separate `<script>` and is unaffected). Not a crash, but analytics identification is silently broken on any PostHog-enabled deployment for every logged-in page.

Tests: `grep -rn posthog tests/` → only `tests/unit/test_config_secret_redaction.py:25` (a redaction fixture). `tests/unit/test_reachability.py:981` names `base.html` as a string only. No test renders `base.html` with a key + user; `tests/e2e/test_browser_flows.py` does not check console/page errors. The "rendering assertion" DoD is unmet.

#### A13 — Dead prompt `prompts/email-reply-classify.md` (STILL PRESENT)

Repo-wide refs excluding `prompts/`: `specs/email-proposal-review.md:266` (a "New Files" table) and `docs/superpowers/plans/2026-08-12-branch2-inventories/branch2-inventory-funding.md:365`. `git log -S"email-reply-classify" -- src/` → empty (never loaded by code). The live classifier `src/services/email_inbound.py:449-475 classify_reply` hardcodes `system_prompt` and an f-string `user_message` with a per-call boundary token. Issue cited `:413-422`; drifted to `:449+`.

#### A14 — `prompts/daily_audit.md` / `audit_recipient_list` (PARTIAL — fact right, "dead" framing wrong)

True: `src/config.py:453-455` defines `audit_recipient_list`; `grep -rn audit_recipient_list src/ scripts/ tests/` → only the definition and the `:154` comment. `audit_recipients` appears additionally only in `tests/unit/test_config_secret_redaction.py:142` (field list).

But the prompt is not app-loaded by design: `prompts/daily_audit.md:1-5` "Prompt for a scheduled Claude Code agent (or on-demand run) …"; `:150-155` instruct that agent to read `settings.audit_recipient_list` when composing the SES send command. `docs/specs/2026-08-18-postgres-backup-verification-design.md:456-457,472` refer to "the 07:00 `daily_audit.md` Claude cron — which already inspects this host daily". So the property has an out-of-band consumer and the prompt is live infrastructure, not dead. Issue's note that `9ab5555` changed content not readers is correct (`git log -- prompts/daily_audit.md`: `9ab5555`, `c9bb98c`, `2aef945`, `3da9821`). Cannot verify the host crontab itself (no ssh permitted).

### PR DOC-B

#### B1 — Token pickup for surviving agents (FIXED, confirmed)

`src/agent/simulation.py:4569-4601` (inside `_sync_roster_from_db`, before the membership early-return at `:4603-4614`):
```python
            if self.slack_enabled:
                for aid in self.agents:
                    r = desired.get(aid)
                    if r is None or aid in self.slack_clients:
                        continue
                    token = (r.slack_bot_token if is_valid_token(r.slack_bot_token) else env_token(aid))
                    if not is_valid_token(token):
                        continue  # still tokenless — retry on a later tick
                    client = AgentSlackClient(agent_id=aid, bot_token=token)
                    if not client.connect(): …continue
                    self.slack_clients[aid] = client
                    logger.info("[roster] Adopted Slack client for %s (token provisioned after startup)", aid)
```
Fix commit: `d1c440e` 2026-08-06 "fix(roster): adopt a Slack client when a live agent gains a token" (ancestor of HEAD). Tests: `tests/unit/test_roster_sync.py:155-190` `test_surviving_agent_that_gains_a_token_gets_a_client`, `test_surviving_agent_without_a_token_gets_no_client`, `test_existing_client_is_not_rebuilt`. `CLAUDE.md:114-116` limits the no-restart promise to "activating/inactivating agents or setting a new `slack_bot_token`" — accurate. Issue's `:4455-4495` drifted to `:4569-4601`.

#### B2 — `bot_name` / `pi_name` not live (STILL PRESENT)

Query selects names (`:4540-4546`), but the surviving-agent diff is role-only, `:4561-4567`:
```python
            for aid, agent in self.agents.items():
                r = desired.get(aid)
                if r is not None and getattr(r, "role", "pi_lab") != agent.role:
                    agent.role = r.role
                    role_changed = True
```
Names are consumed only in the `to_add` branch: `:4657 agent = Agent(agent_id=aid, bot_name=r.bot_name, pi_name=r.pi_name, role=r.role)`, `:4661 self._bot_name_to_id[agent.bot_name.lower()] = aid`. `Agent.bot_name`/`pi_name` are plain attributes (`src/agent/agent.py:73-74`) read by the lab directory (`simulation.py:3644 f"### {other_agent.pi_name} Lab"`), sender names (`:3403`), and the bot-name map. A DB rename of a live agent is therefore invisible until restart. `grep -rn -i "rename\|bot_name" CLAUDE.md README.md docs/production-migration.md` → no doc states the restart requirement. No test covers a rename (`test_roster_sync.py` has none).

### PR DOC-C

#### C1 — Numeric-suffix fallback missing (STILL PRESENT, mechanically confirmed)

`src/routers/agent_page.py:393-414`:
```python
async def derive_agent_identity(db, full_name) -> tuple[str, str]:
    last_name = full_name.split()[-1]
    stem = "".join(c for c in last_name.lower() if c.isalpha())
    display = last_name
    collision = await db.execute(select(AgentRegistry).where(AgentRegistry.agent_id == stem))
    if collision.scalar_one_or_none():
        initial = full_name[0]
        return f"{initial.lower()}{stem}", f"{initial.upper()}{display}Bot"
    return stem, f"{display}Bot"
```
Only one query (for `stem`); the prefixed candidate is never checked. Handler `:433-443` does `db.add(agent); await db.commit()` with no `try/except` (the `IntegrityError` handlers at `:1018-1022` belong to the PI-inbox write, not signup). `src/models/agent_registry.py:19 agent_id … unique=True`.

Ran (`scratchpad/26/collide.py`) against the real function with a fake `db` that treats a given set of ids as taken:
```
taken=[]              'Chunlei Wu' -> ('wu','WuBot')    queries=['wu']  ok
taken=['wu']          'Peng Wu'    -> ('pwu','PWuBot')  queries=['wu']  ok
taken=['pwu','wu']    'Pei Wu'     -> ('pwu','PWuBot')  queries=['wu']  COLLIDES
taken=['pwu','wu','wu2'] 'Ping Wu' -> ('pwu','PWuBot')  queries=['wu']  COLLIDES
source has numeric loop?: False
```
A third same-initial namesake gets a duplicate `agent_id` → unique-violation on commit → unhandled 500 on `POST /agent/request`. Tests: `tests/integration/test_agent_page.py:333-355` cover exactly two Wus (`wu` → `pwu`, `PWuBot`); no triple-collision test.

#### C2 — `backfill_agents.py` numeric loop and parity claim (STILL PRESENT)

`scripts/backfill_agents.py:47-69`: docstring `"""Same collision logic as scripts/generate_sparsedata_user.py + agent_page.py. Order: bare last name → first-initial prefix → numeric suffix."""` followed by the two checks and `for i in range(2, 20): candidate = f"{base}{i}" …`. The claim is true w.r.t. `scripts/generate_sparsedata_user.py:166-185` (identical loop) and false w.r.t. `agent_page.py` (C1). Also note `backfill_agents.py:65 _bot_name_for` builds the bot name from `agent_id[0]` for any non-bare id, so a numeric-suffixed `wu2` yields `WWuBot` — a third divergence from the web path's naming (not raised by the issue).

#### C3 — Bot-name half fixed (FIXED, confirmed)

`agent_page.py:411-413` returns `f"{initial.upper()}{display}Bot"` on collision. Fix commit `02143de` 2026-08-04 "fix: close the Slack boundary, the privacy hole, and two identity defects" (introduced `derive_agent_identity`). Test `tests/integration/test_agent_page.py:350-355` asserts `bot_name == "PWuBot"`.

#### C4 — Private `_extract_json` import (STILL PRESENT)

`scripts/generate_sparsedata_user.py:57 from src.services.llm import _extract_json, get_anthropic_client`, used at `:500`. Present since the script's first commit `4a05397`. No public alternative exists: `grep -rn "def _extract_json\|def extract_json" src/` → `src/services/llm.py:122` and an independent copy at `src/agent/simulation.py:5467`. The issue's fix ("import a public JSON helper") requires first creating one.

#### C5 — PII CSV / `.gitignore` (FIXED; issue's history wrong)

Current: `generate_sparsedata_user.py:854 audit_path = Path("scripts") / f"_sparse_run_{ts}.csv"`; `.gitignore:98-99` `# One-off scratch scripts and their inputs` / `scripts/_*` (issue said `:96`; drifted). `git ls-files | grep -i csv` → nothing tracked; `git log --all --diff-filter=A -- '*.csv'` → no CSV ever committed.
Correction to the issue: the output did **not** "move" — `git show b7edcbc:scripts/generate_sparsedata_user.py` already has `scripts/_sparse_run_{timestamp}.csv` at `:29,:854`, and `git log -S"_sparse_run_"` returns only `4a05397` (2026-06-06, pre-baseline). What the baseline lacked was the ignore rule: `git show b7edcbc:.gitignore | grep scripts/_` → none; added by `c46918a` 2026-08-03 "chore: track .dockerignore; keep local state and personal data out of git".

#### C6 — `build_cabo_sankey.py` default date (CHANGED / mostly N/A)

`scripts/build_cabo_sankey.py:34-37`:
```python
# Defaults preserve the original Cabo behavior.
DEFAULT_START = "2026-05-01"
DEFAULT_OUT = "/app/data/cabo_viz"
DEFAULT_LABEL = "40-PI Cabo run"
```
`:145-146 ap.add_argument("--start", default=DEFAULT_START, help=…)`, `:137-139 _parse_start`. Module docstring `:4-6,15-16` documents `--start` as the window selector with a Schultz example. `--start` was added in `06c7ba5` (2026-06-06), before the `b7edcbc` baseline — so "parameterize the date" was already done when the issue was first filed. What remains is that the *default* is the Cabo window, which the file explicitly labels as intentional. Low value; at most change the default or make `--start` required.

---

## 3. Counts

**13 still present** (A1 by-design, A3, A4, A5, A6, A7, A8, A9, A10, A11, A12, A13, B2, C1, C2, C4 — 16 rows if A1 and the A14 factual half are counted; excluding A1-by-design and A2-confirmed: 13 doc/code defects live) · **3 fixed** (B1, C3, C5) · **1 partial** (A14) · **1 changed** (C6) · **0 not reproducible** · **1 confirmed as the issue states** (A2).

## 4. What I could not verify and why

- Whether the production deployment actually has `POSTHOG_API_KEY` set (decides whether A12 fires in prod). `.env.example` has no posthog entry; no network/ssh allowed.
- Whether the host `daily_audit.md` cron (A14) is really installed — only documentary evidence (`docs/specs/2026-08-18-…:456-457`); crontab not accessible from here.
- Whether prod has any `slack_ts IS NULL` legacy rows today (A3/A4 impact) — requires the prod DB; memory notes say the DB is already at 0024, which is exactly the "already at head" case the runbook does not cover.
- The full-suite behaviour of `POST /agent/request` on a real triple collision (C1) — integration tests need Docker; I substituted a fake-DB run against the real `derive_agent_identity` plus the unique constraint and the absence of any `try/except` in the handler.
- Issue #21 PR V11's exact "three ways over" enumeration — I read `issues/issue_21.md` only to confirm the shape; my own count (4 slots, unclaimed worker slot, 4 minting processes) is from current source.
