# FINAL_A — Section A confirmation pass (coord + Part M + Part R + cross-cutting 3,4,6,7,8,9)

Scope: FINAL_CHECK.md items 1-2 over `redteam/coord_M_R.md` (6 BLOCKER / 18 MAJOR / 18 MINOR),
and items 3, 4, 6, 7, 8, 9 over the whole 23,280-line plan. Repo read-only at `copi-prod` 18ba52c.
Item 5 (six end-to-end spot-checks) is another reviewer's assignment and is not covered here.

**Verdict: NO-GO** — 2 new BLOCKERs, 6 new MAJORs. Everything from `coord_M_R.md` is applied except
two MAJORs that were applied on one side of a cross-part pair only.

---

## 1. coord_M_R.md verdict table

### BLOCKERs — 6/6 APPLIED

| id | verdict | evidence in MASTER_PLAN.md |
|---|---|---|
| B1 `migrate` missing from the logging override | APPLIED | reconciliation item 13 (L73); 27.3 Files list L20772-20775; unit test `test_every_prod_service_including_migrate_has_the_json_file_log_override` L20835-20843 (`assert prod <= override`); override diff L20908-20912; `build: context: .` L20863-20864; R.3 `docker compose $C config … grep -c awslogs # must print 0` L23065; R.6 dry proof `run --rm --no-deps -T migrate python -m alembic current` L23100; R.7 `ps -a` + `logs migrate` + `--no-deps` escape hatch L23106-23113 |
| B2 `--via-run` snapshot loss | APPLIED | M.2 Interfaces L22898-22903 names `postflight.py:557-561` (verified: `if not p.is_file(): return (title, FAIL, …)`); Step 3 adds `mkdir -p`, `SNAP_HOST_DIR`, `SNAP_IN_CONTAINER`, `compose_py` with `-v "$SNAP_HOST_DIR:/migrate-state"`; test asserts `"/migrate-state" in argv`; R.6 checks `ls -l backups/preflight_snapshot.json` after both the rehearsal and `--apply` |
| B3 R.4 must stop app/worker | APPLIED | R.4 fully rewritten L23070-23090 (`stop grantbot worker` → `stop app`, `pg_stat_activity` proof, nginx-502 note); R.6 lock remediation now names **app, worker and grantbot**; R.1 adds the `pg_total_relation_size` top-12 query. Anchors re-verified: `preflight.py:474-477` docstring, remediation list `:1522-1531` = `docker stop -t 30 agent-run` / `docker compose stop app worker grantbot` |
| B4 `confdeltype` bytes-vs-str | APPLIED | M.1 Step 1 second integration test uses `pg_get_constraintdef`; explicit "Do NOT add an FK-action check that reads `pg_constraint.confdeltype`" note; no `EXPECTED_FK_ACTIONS`/`check_fk_actions` anywhere; "The '13 checks' count in `docs/production-migration.md` stays true" |
| B5 `uq_publications_user_pmid` also in `EXPECTED_INDEXES` | APPLIED | M.1 `EXPECTED_INDEXES` = 21 entries, first is `"uq_publications_user_pmid": "USING btree (user_id, pmid)"` with the both-dicts rationale inline |
| B6 drift-guard tuple | APPLIED | M.1 Step 3: `for revision in ("0019" … "0028")` with "EXTEND it, never replace". The 0027-loop caveat is resolved the other way: 25.3 now writes all 20 `op.create_index("<name>", …)` calls **literally** (L17682-17745, with an explicit note that a loop would make the guard vacuous). Verified the two lists match exactly, 20/20 names and tables |

### MAJORs — 16 APPLIED, 2 PARTIAL

| id | verdict | evidence / gap |
|---|---|---|
| M-1 phase 3 red for 4 commits | APPLIED | Global Constraint bullet 1 carve-out (L18); execution-order phase-3 row "one atomic push unit" naming the four tests and "do **not** `git push`" |
| M-2 reconciliation item 1 contradicts 22.2/25.3 | APPLIED | item 1 rewritten verbatim to the audit's text (L61) |
| M-3 `:223` / `:231` missing from M.1 | APPLIED | M.1 Files list `:223`, `:231-232`, `:251-256`; Step 3 shows both diffs. Verified against real code: 10 `"0024"` hits exactly as 22.3's table claims |
| **M-4 `check_sizing` / 25.3's check-9 claim** | **PARTIAL** | M.1 half applied (`POST_0019_STARTS (:93)` in Files, tuple `("0020","0021","0023","0024")`, `check_sizing` rewritten to size `publications`, `:251-256` in Files). **25.3's Deploy note was never corrected** — L18100 still reads "if any of these tables is large in prod, preflight's check 9 (lock-window estimate) will flag it before `--apply`". False: check 9 sizes `agent_messages` and (post-M.1) `publications` only, never the 13 tables 0027 indexes |
| M-5 R.7 health body / curl | APPLIED | R.7 uses `exec -T app python -c urllib…`, expects `b'{"status":"ok"}'`, "Do not expect a `db` key". Verified 27.2 returns `{"status": "ok"}` |
| M-6 two R.9 greps can never match | APPLIED | `=== Turn [0-9]+:` and `\[roster\]`; roster confirmation moved to the UI; `roster sync failed` / `publication-record load failed` counters |
| M-7 `.State.Health.Status` nil-panics | APPLIED | `{{if .State.Health}}…{{else}}no-healthcheck{{end}}` in both R.1 and R.9 |
| M-8 R.3 `--no-cache` + `migrate` | APPLIED | `build --no-cache app worker grantbot migrate` + `--no-cache agent` + `ls /app` image inspection |
| M-9 D14 numbers | APPLIED | D14 row now 900 s / 300 s / `5 s × 2^(attempts−1)` cap 300 s |
| M-10 `Closes #20` without D9's fix | APPLIED | Task 20.9b written (L5687-5811); D9 row; PR body "only if Task 20.9b shipped; otherwise `Refs #20`". (But see NEW-B2/NEW-M1/NEW-M3 — 20.9b itself does not hold up) |
| M-11 8 missing decision rows + false closure claim | APPLIED (1 sub-item stale) | D25-D33 added (9 rows); Coverage sentence enumerates D4, D21, D23, D27-D31, D25. Sub-item 9 not done — see NEW-m4 |
| **M-12 D22's consumer** | **PARTIAL** | D22's row rewritten exactly as the audit asked ("extend Task 26.11's Files list with `scripts/generate_sparsedata_user.py — _resolve_agent_id (:165-183)`"), but **26.11's Files list was not extended** (L19878-19880 lists only `backfill_agents.py`), Part 26 open decision #1 (L20267-20273) still says the file is out of scope, and Part 26's file table (L20245) still limits it to "import line + one call site only". Approving the default still changes nothing |
| M-13 five unlisted cross-part overlaps | APPLIED | reconciliation items 10, 11, 12 added verbatim |
| M-14 in-container pytest after 27.1 | APPLIED | Global Constraint bullet 2 + reconciliation item 14; M.1 Step 4 uses `-f docker-compose.yml` |
| M-15 27.13 limits never applied to postgres/certbot | APPLIED | R.7b added; D24 row; wording matches 27.13's Deploy note |
| M-16 unassigned `$ROLLBACK_SHA`/`$DEPLOY_SHA`/`$PRE_DEPLOY_DUMP` | APPLIED as written | all three `export`ed in R.1/R.2 (but see NEW-M6 — they still do not survive to R.4/R.10 for an agentic deployer) |
| M-17 M.1/M.2 ruff violations | APPLIED | no new preflight import (uses the file's existing `pf`/`po`), inserted at `:952`, `pathlib` dropped, M.2 header is 3 sorted stdlib imports + `from pathlib import Path`, no unused `r`; shim answers `NOT-app` to `ps --status running --services`; Step 2's expected failure corrected to `FileNotFoundError: …/argv.log` |
| M-18 unencoded DSN + missing `sudo` | APPLIED | R.6 `PW_ENC` via `urllib.parse.quote(..., safe="")` + `unset`; `sudo` on R.2's `ls`; R.10 uses `sudo sh -c '… < "$0"' "$PRE_DEPLOY_DUMP"` |

### MINORs — 18/18 APPLIED

1 `HEAD_REVISION`→0028 · 2 `assert '0024' == '0028'` · 3 postflight anchors `:97-131`/`:143-162`/`:164-173`/`:97-184` · 4 "13 checks" preserved · 5 M.3 title `:1` + scope `:7-8`, sentence de-garbled · 6 `ps -a` · 7 `|| true` on both `docker stop`/`docker rm` · 8 `--line-buffered` · 9 R.9 uses the `pg_get_constraintdef` query with both FKs spelled out · 10 `pg_size_pretty(pg_database_size('copi'))` · 11 R.8 "also re-runs `migrate` (idempotent) via app's depends_on" · 12 R.10 "Do **not** run any `docker compose up` between the restore and the checkout" + orphan-container note + no `--remove-orphans` · 13 reconciliation item 6 order 27.1(ph1)→26.7(ph5) · 14 spurious "27.3 depends on M.1" replaced with "must land together with its override entry" · 15 D15 "install it with the host's `~/.local/bin/uv`" · 16 `MIGCHECK_PORT=55433 git push …` as an env prefix · 17 Global Constraint + reconciliation 15 on the six-finding headroom · 18 `| tail -n 1` on M.2's Step 1 and Step 6 captures

---

## 2. Cross-cutting items

**Item 3 — cross-part consistency.** 21.13 vs 24.2: reconciliation item 2 is explicit about the
guard boundary and 21.13's authority — OK. 25.4 vs 27.2 in `src/main.py`: reconciliation item 16 +
25.4's own cross-part note (patch the **middleware's** factory, assert "no *badge* query", not "no
session") — OK, and 27.2's route returns `{"status": "ok"}` as R.7 expects. 27.3's override entry —
OK (B1). M.1's `PLANNED_OBJECTS`/`EXPECTED_*` vs 22.2/25.3/20.10 — exact name match verified
(`uq_publications_user_pmid`, the 20 `ix_*`, `reopened_at`); `EXPECTED_INDEXES` = 21, `EXPECTED_COLUMNS`
row `("thread_decisions","reopened_at","timestamp with time zone",True,"")`. `parse_retry_after`
23.1→24.3: identical signature `(value: str | None, default: float, cap: float = 30.0) -> float` at
L13030, L13194, L16282, used with `cap=retry_after_cap` at L16672 — OK. `BOT_TAG_RE` 20.16→23.10:
23.10 consumes `src.agent.mentions.BOT_TAG_RE` and rebuilds its wrapped regex from `BOT_TAG_RE.pattern`,
no redefinition — OK. `get_agent_bot_token` 23.4 vs D10/R.1: signature matches the real
`src/services/slack_tokens.py:54`, R.1 greps `SLACK_BOT_TOKEN_GRANTBOT` (names only) — OK.
Part R vs 27.2/27.3/27.13 — OK. **Three inconsistencies remain: NEW-M2, NEW-M3, NEW-M4/M-4, NEW-M5/M-12.**

**Item 4 — leftovers.** `HEAD_REVISION` survives at L61, L10363-10411, L12972 only, every one of them
prose that fixes it at `0028` and forbids introducing the placeholder (22.3 is now verify-only, "Do
NOT introduce a `HEAD_REVISION` placeholder anywhere") — OK. No `TBD`/`FIXME`/`XXX`; the two `TODO`
hits (L20135, L20146) are quoting a code comment under discussion — OK. No `similar to` / `analogous
to` placeholders. Task numbering complete and gap-free: 0.1-0.2, 20.1-20.22 + 20.9b, 21.1-21.13,
22.1-22.14, 23.1-23.15, 24.1-24.4, 25.1-25.6, 26.1-26.13, 27.1-27.14, M.1-M.3 (106 tasks). Coverage
matrices present for Parts 20-27 (L5588, 9658, 12883, 15678, 17156, 18619, 20200, 22540); **Parts M and
R have none and no matrix owns M.1/M.2/M.3 — NEW-m3.**

**Item 6 — Part R read as the deploying agent.** Structure, ordering and STOP conditions are sound and
measurable (alembic_version, `unless-stopped`, `status.json.ok`, 3× `pg_database_size` now has both
measuring commands, exit codes 0/1/2, `b'{"status":"ok"}'`, `migrate` Exited (0), grep counters). Every
variable is now assigned before use *within a single shell session* — **NEW-M6 is the residual.**
Minor: R.7's "app healthy within ~45 s" has no wait loop.

**Item 8 — no prompt changes.** Clean. Global Constraint bullet 8 (L24) is explicit; 22.6 leaves
`profile_pipeline.py:324` byte-for-byte and adds only a code comment above it (L10879-10880); 26.2 is
scoped by D33 to the spec-table row and touches no `prompts/` file (L18732-18734, L18846-18849); the one
`prompts/` path in 27.1 is `MUST_NOT_EXCLUDE`'s `"prompts/profile-synthesis.md"` — an assertion that the
image still *contains* it. No task edits the `email_inbound.py` classify prompt or GrantBot's selection
prompt.

**Item 9 — R vs the corrected 27.** 27.3 defines `migrate` with `build: context: .` (L20863-20864), so
R.3's `build --no-cache app worker grantbot migrate` resolves. R.7's `--no-deps` escape hatch is correct
(`--no-deps` does bypass `service_completed_successfully`). 27.13's recomputed sum is internally
consistent: 512+384+512+512+256+64+32 (+migrate 256) = 2528m, `names = (*EXPECTED_MEM, "migrate")`,
`assert total_mib <= 3072`; R.7b's `0 0 = postgres still unlimited` matches 27.13's Deploy note and D24.

---

## 3. NEW findings

### NEW-B1 (BLOCKER, item 7) — Task 22.14 puts SQL inside three swallowing `except Exception` guards whose handlers/successors read ORM attributes
Before 22.14 all three `try` bodies were an HTTP lookup plus pure Python list mutation, so a failure
could never poison the session. 22.14 moves an atomic `UPDATE` inside them while leaving the
`except Exception: log-and-continue` in place, so a failed statement/autoflush is swallowed and the
next attribute read or `commit()` raises `PendingRollbackError` — turning a cosmetic best-effort
failure into a 500 that loses the delegation. All three verified against the real tree:

1. **`src/routers/invite.py`** — plan L12827 `await db.execute(append_delegate_slack_id_stmt(agent.id, sid))`
   inside the try; the real handler at `invite.py:245-252` logs `agent.agent_id` (`:251`), and
   `await db.commit()` at `:254` plus `user.id, agent.agent_id` at `:257-258` follow the swallow.
2. **`src/routers/agent_page.py::remove_delegate`** — plan L12803; real code `agent_page.py:1613-1614`
   swallows, then `await db.delete(delegate)` / `await db.commit()` (`:1616-1617`) and
   `delegate.user_id, agent.agent_id, current_user.name` (`:1620-1621`).
3. **`src/routers/agent_page.py` connect-slack** — plan L12784; the enclosing handler
   (`agent_page.py:1431-1435`, not shown in the plan's snippet) reads `current_user.email`.

**Fix — capture ids into locals before the guarded statement, and move the SQL out of the swallowing try:**
```python
# 1. src/routers/invite.py — replace the `if user.email:` block and the commit below it
    agent_slug = agent.agent_id      # captured BEFORE any guarded db.execute(): a failed flush
    agent_row_id = agent.id          # expires every attribute on agent/user (the 21.2/21.13 trap)
    delegate_user_id = user.id
    sid = None
    if user.email:
        try:                         # LOOKUP only — no SQL inside the best-effort try
            from src.services.slack_tokens import token_for_agent_row
            from src.services.slack_web import lookup_user_by_email_async
            bot_token = token_for_agent_row(agent)
            if bot_token:
                sid = await lookup_user_by_email_async(bot_token, user.email)
        except Exception as exc:
            # Best-effort by design (specs/web-delegates.md §Slack Linkage): a
            # delegate is useful without a Slack id. But LOG it — a bare `pass`
            # here hid an ImportError for an unknown length of time, and the
            # whole sync was dead code with nothing to show for it.
            logger.warning("Delegate Slack-ID sync failed for agent %s: %s", agent_slug, exc)
    await db.commit()
    if sid:   # own unit of work, AFTER the delegation is durable: a failure here must neither
        try:  # roll the delegation back nor poison the session
            from src.services.delegate_slack_ids import append_delegate_slack_id_stmt
            await db.execute(append_delegate_slack_id_stmt(agent_row_id, sid))
            await db.commit()
        except Exception as exc:
            await db.rollback()
            logger.warning("Delegate Slack-ID append failed for agent %s: %s", agent_slug, exc)
    logger.info("Delegate %s accepted invitation for agent %s", delegate_user_id, agent_slug)
    return RedirectResponse(url=f"/agent/{agent_slug}/dashboard", status_code=302)

# 2. src/routers/agent_page.py::remove_delegate — replace the `if delegate:` body through logger.info
    if delegate:
        delegate_user_id = delegate.user_id      # locals captured BEFORE the guarded db.execute()
        delegate_email = delegate.user.email
        agent_slug, agent_row_id = agent.agent_id, agent.id
        actor_name = current_user.name
        sid = None
        if delegate_email and agent.delegate_slack_ids:
            try:
                from src.services.slack_tokens import get_any_bot_token
                from src.services.slack_web import lookup_user_by_email_async
                bot_token = await get_any_bot_token(db)
                if bot_token:
                    sid = await lookup_user_by_email_async(bot_token, delegate_email)
            except Exception as exc:
                logger.warning("Delegate Slack sync is best-effort; skipped: %s", exc)
        if sid:   # OUTSIDE the swallowing try — a failed UPDATE must not be hidden
            from src.services.delegate_slack_ids import remove_delegate_slack_id_stmt
            await db.execute(remove_delegate_slack_id_stmt(agent_row_id, sid))
        await db.delete(delegate)
        await db.commit()
        logger.info("Delegate %s removed from agent %s by %s",
                    delegate_user_id, agent_slug, actor_name)

# 3. src/routers/agent_page.py connect-slack — hoist the email above the try; it is read in the
#    pre-existing except arm at :1431-1435, which the plan's snippet does not show:
    user_email = current_user.email      # immediately above `error = None` (:1404)
#    then use `user_email` at all three read sites (the lookup call, the f-string at plan L12779,
#    and logger.warning(..., user_email, exc)).
```
Cleared as SAFE by the same audit (no change needed): 24.1 (`email_clean` is a plain local; the except
arm only logs and renders), 24.2 (except arm is `rollback()` + `raise HTTPException(400) from None`,
reads nothing), 25.2 (`name = user.name` is captured above the `try` in both routes), 26.10
(`agent_id`/`bot_name` are locals; `current_user.*` read in the constructor above the try), 22.13 (no
rollback path; raw `UPDATE … RETURNING`, `profile.id` read before the await), 20.9 (`decision.id`/
`user_id` read at `db.add` before `commit`; the rollback is the last statement in the session block),
and the 21.13 × 24.2 interaction (21.13 hoists `current_user_id`/`agent_registry_id` above the try and
its post-rollback service calls re-`SELECT`).

### NEW-B2 (BLOCKER, Task 20.9b) — both of 20.9b's tests use `cookies=_auth(...)`; `_auth` returns a **headers** dict
`tests/integration/test_agent_page.py:52-56`:
```python
def _auth(user_id) -> dict:
    """Forge the signed session cookie SessionMiddleware would issue."""
    ...
    return {"Cookie": f"copi-session={signer.sign(data).decode()}"}
```
Every existing `/message` test passes it as `headers=_auth(world.pi.id)` (`:908`, `:934`, `:943`, `:974`).
Passed as `cookies=`, httpx sets a cookie literally *named* `Cookie`, no session is presented, and the
route never reaches the new check — so Step 2's stated failure ("the first test gets 302 instead of
403") is wrong and Step 4 can never pass. **Fix: `headers=_auth(owner.id)` in both tests** (and drop
`follow_redirects=False`, which is not used by the neighbouring tests).

### NEW-M1 (MAJOR, Task 20.9b) — the test's helper names do not exist
* `run_id = await _seed_run(db_session)  # existing helper in this module` — **there is no `_seed_run`**
  anywhere in the file. The task's parenthetical hedge admits it might be absent but the inline comment
  asserts it exists.
* `make_user(...)` / `make_agent(...)` are unresolvable bare names: the module does `from tests import
  factories` (`:47`), so they are `factories.make_user` / `factories.make_agent`.
**Fix: use the module's own `world` fixture, which already supplies a run, an owned active agent and its
PI:**
```python
async def test_pi_cannot_reply_into_a_thread_their_agent_never_joined(client, db_session, world):
    root = await factories.make_agent_message(
        db_session, run=world.run, agent_id=OTHER_AGENT, channel_name="general",
        message_ts="1700000000.000100", thread_ts=None, phase="new_post", content="root")
    await factories.make_agent_message(
        db_session, run=world.run, agent_id=THIRD_AGENT, channel_name="general",
        message_ts="1700000000.000200", thread_ts=root.message_ts, phase="thread_reply", content="r")
    await db_session.flush()
    resp = await client.post(
        f"/agent/{OWNER_AGENT}/message",
        data={"channel_name": "general", "content": "looks good",
              "thread_ts": "1700000000.000100"},
        headers=_auth(world.pi.id))
    assert resp.status_code == 403
    assert "thread" in resp.json()["detail"].lower()
```
(and the mirror-image positive test with `agent_id=OWNER_AGENT` on the root, asserting 302).
`factories.make_agent_message(session, *, run=None, **overrides)` flushes for you; `AgentMessage` is
already imported at `:38` if a direct insert is preferred. Verified correct in 20.9b as written:
`post_agent_message`'s local `from src.services.pi_inbox import (...)` block exists at `:964-968`;
`select`, `or_`, `uuid`, `AsyncSession` and `AgentMessage` are all already imported in
`src/services/pi_inbox.py` (`:10-23`), so no import edit is needed; `run_id` (`:982`), `target_channel`
(`:990`) and `agent.agent_id` are all defined before the insertion point; `record_pi_message`'s
`thread_ts=thread_ts.strip() or None` is at `:1007` exactly as described.

### NEW-M2 (MAJOR, item 3) — Task 22.3's table calls `test_migration_checks.py:223` "NEVER touch", contradicting M.1 and reconciliation item 1
22.3's classification table: `| test_migration_checks.py:223 | permanent fact ("0024 blocks against
target 0023") | NEVER touch |`. But reconciliation item 1 lists `:223` among the pins **M.1 owns**, and
M.1 Step 3 correctly removes `"0024"` from that parametrize list. Verified why M.1 is right:
`revision_status` returns `PASS` for any `current in SUPPORTED_START_REVISIONS` *before* it considers
the target (`preflight.py:385-387`), so once M.1 adds `"0024"`, `revision_status("0024", "0023")` is
`PASS` and `test_revision_status_blocks_anywhere_else[0024]` fails.
**Fix — replace that row:**
> | `test_migration_checks.py:223` | `"0024"` in the "blocks anywhere else" parametrize list | **M.1 removes it** — once `"0024"` joins `SUPPORTED_START_REVISIONS`, `revision_status` returns `PASS` at `preflight.py:386-387` regardless of the target, so this case inverts |

### NEW-M3 (MAJOR, item 3) — Part 20's coverage matrix and open decision #3 are stale, sitting directly above Task 20.9b
Open decision #3 (L5669-5677) still reads *"No part in the shared file-ownership table currently claims
that function … The coordinator should assign this validation to a part (or add it as a small
follow-up)"*, and the coverage matrix row is `| COR-5 | STILL PRESENT | 20.9 |`. Both are now false:
reconciliation item 7 assigns `post_agent_message` to 20.9b, D9 says ship it, and 20.9b is 120 lines
further down the same part.
**Fix:** matrix row → `| COR-5 | STILL PRESENT | 20.9 (engine + poller) + 20.9b (web `post_agent_message`
`thread_ts`) |`; decision #3 → *"**RESOLVED (D9).** Task 20.9b implements this validation in
`post_agent_message` via the new `pi_inbox.pi_may_reply_in_thread`; reconciliation item 7 assigns that
function to Part 20. Without 20.9b the PR says `Refs #20`, not `Closes #20`."*

### NEW-M4 (MAJOR = MAJOR-4 residual) — 25.3's Deploy note still promises a check that does not exist
L18100. **Fix:** replace the final clause with *"preflight's check 9 sizes `agent_messages` and — only
after M.1's extension — `publications`; it does **not** size the 13 tables 0027 indexes, so do not rely
on it. Measure them with the `pg_total_relation_size` query in R.1."*

### NEW-M5 (MAJOR = MAJOR-12 residual) — D22's instruction was never carried into Part 26
Add to Task 26.11's Files list: `Modify: scripts/generate_sparsedata_user.py — _resolve_agent_id
(:165-183)` (same `candidate = f"{prefixed}{i}"` fix, same unit test); update Part 26's file table
(L20245) from "import line + one call site only"; and rewrite Part 26 open decision #1 (L20267-20273) to
say it is resolved by D22 rather than still asking the question.

### NEW-M6 (MAJOR, item 6) — Part R depends on shell state that an agentic deployer does not have
`$C`, `$COMPOSE_FILE`, `$ROLLBACK_SHA`, `$DEPLOY_SHA` (R.1) and `$PRE_DEPLOY_DUMP` (R.2) are consumed in
R.3, R.4, R.6, R.7, R.7b, R.8, R.9 and R.10. A deploying **agent** runs each block as a separate
non-interactive command, where none of them survive. Empty `$C` is the worst case: `docker compose stop
app` then reads only `docker-compose.yml`, the **dev** file — precisely the CLAUDE.md:20-27 failure that
left prod with `restart: no` after a reboot. `pg_restore … < ""` in R.10 is the second.
**Fix — add to Part R's preamble:**
> **R.1-R.11 assume ONE shell session.** If you are an agent whose shell state does not persist between
> commands, write the values to a file in R.1/R.2 and source it at the top of every later block:
> ```bash
> cat > /tmp/deploy.env <<EOF
> export COMPOSE_FILE=docker-compose.prod.yml:docker-compose.override.yml
> export C="-f docker-compose.prod.yml -f docker-compose.override.yml"
> export ROLLBACK_SHA=$(git rev-parse HEAD)
> export DEPLOY_SHA=$(git rev-parse origin/copi-prod)
> EOF
> . /tmp/deploy.env    # first line of every subsequent block; re-verify `echo "$C"` is non-empty
> ```
> and append `export PRE_DEPLOY_DUMP=…` to it in R.2. **Never run a `docker compose` command in Part R
> without confirming `$C` is non-empty** — a bare `docker compose` reads the dev file.

### NEW-m1 (MINOR, Task 20.9b) — anchor drift
`post_agent_message` runs `:949-1029` (the plan says `:949-1027`), and the `pi_may_post_to_channel`
block is at **`:991-998`**, not `:983-993`. `thread_ts: str = Form("")` at `:954` and
`pi_may_post_to_channel` at `src/services/pi_inbox.py:52-101` are both correct. Recoverable (the task
names the symbol and the Global Constraints require relocating by symbol), but it should be fixed.

### NEW-m2 (MINOR, Task 20.9b) — PI-authored messages carry `agent_id=NULL`, so a PI-only thread is refused
`record_pi_message` writes `agent_id=None` (`src/services/pi_inbox.py:120`), so `pi_may_reply_in_thread`'s
participant clause never matches the PI's own messages: a thread that only the PI has spoken in returns
403. Unreachable today — `templates/agent/conversations.html:44` is a fixed
`<input type="hidden" name="thread_ts" value="">` and no JS sets it, so the UI only ever submits an empty
`thread_ts` (which is exactly why the residual is a hand-crafted-POST hole worth closing). Add a note to
20.9b so a future UI wiring does not ship a false 403, e.g. also accept rows with
`is_bot IS FALSE AND sender_name = f"{current_user.name} (PI)"` in the same run/channel/thread.

### NEW-m3 (MINOR, item 4) — no coverage matrix owns M.1/M.2/M.3
Parts M and R end without one, and Part 27's matrix maps I2-a…I2-f to 27.2/27.3/N/A only — so M.1's
claimed closure of "#27 I2 runbook gap" is recorded nowhere. Add a three-row matrix to Part M (or three
rows to Part 27's) so the PR's closure claim is complete.

### NEW-m4 (MINOR, MAJOR-11 sub-item 9) — Part 26 open decision #4 is stale
L20288-20293 still says `docs/production-migration.md`'s "Supported starting points: 0018/0019/0020/0021"
is "left unwidened". M.1 makes 0024 a supported start and M.3 rewrites the title (`:1`) and the scope
line (`:7-8`). Rewrite it to say it is resolved by M.1/M.3, or delete it.

---

## 4. Counts

- coord_M_R.md: **BLOCKER 6/6 APPLIED**; **MAJOR 16/18 APPLIED, 2 PARTIAL** (MAJOR-4, MAJOR-12);
  **MINOR 18/18 APPLIED**. Zero MISSING.
- New: **2 BLOCKER** (NEW-B1, NEW-B2) · **6 MAJOR** (NEW-M1 … NEW-M6) · **4 MINOR** (NEW-m1 … NEW-m4).
- Cross-cutting items 4, 8, 9 pass clean. Items 3 and 6 pass except for the findings above. Item 7
  found one UNSAFE task (22.14, three sites); the other seven checked tasks are SAFE.

**GO/NO-GO: NO-GO.** Must change before hand-off: NEW-B1, NEW-B2, NEW-M1, NEW-M2, NEW-M3, NEW-M4
(= MAJOR-4 residual), NEW-M5 (= MAJOR-12 residual), NEW-M6. The four MINORs are should-fix.
