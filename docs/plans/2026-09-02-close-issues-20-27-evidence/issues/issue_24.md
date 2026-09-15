# #24 Web request-path robustness: concurrent-insert 500s and a 5-minute event-loop freeze (2 PRs)
state=OPEN created=2026-07-30T15:53:07Z updated=2026-08-11T23:54:38Z labels=['area:web', 'verified-2026-07-30']

## BODY

Two verified defects where a single user action can 500 or stall the whole site. Both live in the `app` container, which runs a **single uvicorn worker** (`Dockerfile:24`, `docker-compose.prod.yml:29`, no `--workers`) — so a blocked event loop is a full outage, not a slow request.

Originally verified at `origin/main` @ `b7edcbc` (2026-07-30). **Re-verified 2026-08-11 against the open PR-stack tip (`issue-29-authorship-grounding` @ `b1d54da` = main + #30/#31/#32); neither endpoint was touched by the stack.**

## Priority (triage 2026-08-11)

**C2 is Tier 1** — a rate-limited Slack manifest create freezes every request for up to ~5 minutes, and nginx's 120 s `proxy_read_timeout` (issue #27 I5) converts it into a 504 for the admin doing the provisioning. **V5 is Tier 3** (small, correct pattern already exists in-repo twice).

**Suggested order: C2 → V5.**

### PR V5 — Concurrent first-action IntegrityError handling *(small)*
`waitlist_submit` (`public.py:485-503`, unique `email`) and `review_proposal` (`agent_page.py:487-513`, unique `uq_proposal_reviews_decision_agent`) do SELECT-then-INSERT with no `IntegrityError` catch → two concurrent first-time actions race and one 500s. The per-IP waitlist limiter (10/3600 s) does not stop a double-click, and `review_proposal`'s guard is a SELECT + `HTTPException(400)` followed by an unguarded `db.add`/`commit`.

The codebase already has the correct pattern twice: the vote endpoint (`public.py:1038-1057`) and the PI web-message writer (`agent_page.py:1013-1027`, rollback + one retry then 409). *Fix:* mirror it in both handlers.

### PR C2 — Move blocking provisioning I/O off the web loop *(medium)*
`admin_provision_slack` / `admin_provision_slack_callback` (`admin.py:945-963`, `:972-996`) are `async def` and await `start_provisioning`/`complete_provisioning` (`admin_provisioning.py:124-186`), which call **synchronous** `httpx.post` plus `time.sleep(wait)` with up to 5 retries and a 60 s default wait (`slack_provisioning.py:124-152`; `lookup_team_id` `:44-54` and `exchange_code` `:154-172` are also sync on async paths). A rate-limited Slack manifest create therefore freezes **every** web request for up to ~5 minutes on the single worker, and the `get_db` session (and its pooled connection) is held open across the whole blocking call.

The fix has in-repo precedent now: `asyncio.to_thread` wrappers exist at `src/services/slack_web.py:274-299` (plus `grantbot.py:624`, `agent_page.py:319`) — the pattern was simply never applied to the provisioning path.

*Fix:* offload the blocking calls to `asyncio.to_thread`; commit/close the session before external I/O.
- **Note:** `nginx.conf`'s `proxy_read_timeout 120s` converts this freeze into a 504 for the admin doing the provisioning — see PR I5 in issue #27. Fixing either alone leaves the other symptom.

**Definition of done:** each PR ships a test that fails against the pre-fix code — for V5, a concurrent-insert test; for C2, an assertion that the handler does not block the loop.


