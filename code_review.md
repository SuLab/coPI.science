# Code Review: Top 5 Priority Issues

Reviewed: 2026-04-14  
Branch: `coPI-podcast`

---

## Issue 1 — CSRF Bypass on Expired OAuth Session

**File:** `src/routers/auth.py:76-79`  
**Severity:** High (security)

### Current Code

```python
stored_state = request.session.pop("oauth_state", None)
if stored_state and state != stored_state:
    logger.warning("OAuth state mismatch")
    return RedirectResponse(url="/login?error=state_mismatch", status_code=302)
```

### Problem

The guard condition is `if stored_state and ...`, meaning it only enforces the check when `stored_state` is truthy. If the user's session has expired (or was never set), `stored_state` is `None` and the entire check is skipped — any `state` value (including `None`) passes through. A CSRF attacker can initiate an OAuth flow, let the victim's session expire, then replay the callback with an arbitrary code.

### Best Practice

Per [RFC 6749 §10.12](https://datatracker.ietf.org/doc/html/rfc6749#section-10.12) and OWASP OAuth guidelines, the `state` parameter must be treated as a **required, non-nullable nonce**. The correct pattern is to reject the callback if `stored_state` is missing (session expired), not to treat it as a pass condition.

### How to Fix

Change the condition from a two-branch `if stored_state and ...` guard to an explicit three-case rejection:

```python
stored_state = request.session.pop("oauth_state", None)

if stored_state is None:
    # Session expired before the callback arrived — cannot verify CSRF nonce
    logger.warning("OAuth callback with no stored state (session expired or missing)")
    return RedirectResponse(url="/login?error=session_expired", status_code=302)

if state != stored_state:
    logger.warning("OAuth state mismatch — possible CSRF attempt")
    return RedirectResponse(url="/login?error=state_mismatch", status_code=302)
```

Also ensure the state nonce is generated with sufficient entropy. In `src/routers/auth.py` (in the `/login` route that initiates the flow), use `secrets.token_urlsafe(32)` rather than any shorter or predictable token, and store it in the session immediately before the redirect.

---

## Issue 2 — Budget Enforcement Exits the Entire Simulation Loop

**File:** `src/agent/simulation.py:218-222`  
**Severity:** Medium (reliability / correctness)

### Current Code

```python
agent = self._select_agent()
if not agent or not self._agent_within_budget(agent):
    # All agents over budget
    logger.info("All agents over budget or no agent selected. Stopping.")
    break
```

### Problem

`_select_agent()` returns whichever agent is next in the rotation. If that specific agent is over budget, the entire simulation `break`s — even if every other agent still has budget remaining. The log comment says "All agents over budget" but that is only true in the case where `_select_agent` returns `None`; when it returns an agent that is individually over budget, the others are never checked.

### Best Practice

Budget exhaustion for a single agent should be a **skip**, not a **halt**. The loop should continue cycling through agents until every agent is either over budget or no agent can be selected at all. A common pattern is to track how many consecutive agents have been skipped and stop only when the skip count equals the total number of agents.

### How to Fix

Separate the two exit conditions and convert the over-budget case from `break` to `continue`. Count consecutive over-budget skips and only exit the loop when all agents have been skipped in a single pass:

```python
over_budget_streak = 0
total_agents = len(self._agents)

while True:
    agent = self._select_agent()
    if not agent:
        logger.info("No agent selected — simulation complete.")
        break

    if not self._agent_within_budget(agent):
        over_budget_streak += 1
        agent.state.last_selected = time.time()
        if over_budget_streak >= total_agents:
            logger.info("All agents over budget. Stopping.")
            break
        logger.debug("[%s] Over budget, skipping.", agent.agent_id)
        continue

    over_budget_streak = 0  # reset when a valid agent is found
    # ... rest of the turn logic
```

This requires that `_select_agent` rotates through agents based on `last_selected` time (which it already does), so agents that have been skipped will be picked up again on the next cycle.

---

## Issue 3 — RSS Feed Served with Missing Audio File

**File:** `src/podcast/main.py:89-103`, `src/podcast/pipeline.py`  
**Severity:** Medium (reliability)

### Current Code

```python
try:
    ok = await run_pipeline_for_agent(
        agent_id=agent_id,
        ...
    )
    if ok:
        produced.append(agent_id)
except Exception as exc:
    logger.error(
        "Pipeline failed for agent %s: %s", agent_id, exc, exc_info=True
    )
```

### Problem

`run_pipeline_for_agent` returns a boolean `ok`, but within the pipeline itself the episode DB record and RSS entry can be written before the TTS step completes. If TTS fails, the audio file does not exist, but the feed already contains an `<enclosure>` pointing to a non-existent MP3. Any podcast client that subscribed to the feed will attempt a GET on a 404 URL and may display a broken episode permanently.

### Best Practice

The pipeline should follow a **commit-last** pattern: write the episode record and RSS enclosure only after all assets are confirmed present on disk. This is the same pattern used in video/audio platforms (e.g., YouTube's upload pipeline) — metadata is published only after the binary asset is available.

### How to Fix

Inside `src/podcast/pipeline.py`, restructure the steps in this order:

1. Fetch and select the paper (read-only, safe to do first).
2. Generate the text brief (Claude Opus call).
3. Call TTS and write the audio file to disk. **Capture the returned path.**
4. Verify the audio file exists and has a non-zero size (`path.stat().st_size > 0`) before proceeding.
5. Only if step 4 passes: write the `PodcastEpisode` DB row and call `db_session.flush()`.
6. Only after the DB row is committed: build and write the RSS `<item>`.

If TTS fails at step 3, log the error and return `ok=False` without writing anything to the DB or RSS. The caller in `main.py` already handles `ok=False` correctly; the gap is in the pipeline not propagating TTS failures as `False`.

As a secondary safeguard, the RSS endpoint (`/podcast/{agent_id}/feed.xml`) should check whether `data/podcast_audio/{agent_id}/{date}.mp3` exists before including the `<enclosure>` element in its output. This prevents any historical DB rows with missing audio from appearing in the feed.

---

## Issue 4 — Non-Atomic File Writes for Profile and Podcast State

**Files:** `src/agent/agent.py:423-444`, `src/podcast/state.py:22-24`  
**Severity:** Medium (data integrity)

### Current Code

```python
# agent.py
memory_path.write_text(new_memory + "\n", encoding="utf-8")

# state.py
def _save(data: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
```

### Problem

`Path.write_text` is not atomic — it opens the file for truncation and writes in multiple OS-level operations. If the process crashes, is killed, or two coroutines call the write concurrently, the file can be left in a partially written state (empty, or with truncated JSON). For `podcast_state.json`, this means the `delivered_pmids` list can be lost, causing duplicate Slack DMs. For working memory files, a partial write silently discards the agent's accumulated context.

There is also a logical race: `_save` in `state.py` does a read-modify-write cycle (`_load()` → modify → `_save()`). Two concurrent podcast pipeline runs (possible if the scheduler is invoked twice) will both read the same initial state, both modify it independently, and whichever writes last will silently overwrite the other's changes.

### Best Practice

The standard pattern for atomic file writes on POSIX systems is **write to a temp file, then `os.rename`**. Because `rename` is guaranteed atomic by the POSIX spec (it is a single syscall), a reader will always see either the old complete file or the new complete file — never a partial write. Python's `tempfile.NamedTemporaryFile` with `delete=False` in the same directory is the standard way to achieve this.

For the read-modify-write race in `state.py`, use a `threading.Lock` (or `asyncio.Lock` if the callers are async) as a process-level mutex around all load/save operations.

### How to Fix

**Atomic write helper** (can live in `src/utils.py` or inline in each module):

```python
import os
import tempfile
from pathlib import Path

def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Write `content` to `path` atomically using a temp-file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
        os.replace(tmp, path)   # atomic on POSIX; overwrites destination
    except Exception:
        os.unlink(tmp)          # clean up temp file on any error
        raise
```

Replace all four `path.write_text(...)` calls in `agent.py` (lines 428 and 441) and `state.py` (line 24) with `atomic_write_text(path, content)`.

**Lock for state.py read-modify-write:**

```python
import threading
_STATE_LOCK = threading.Lock()

def record_delivery(agent_id: str, pmid: str) -> None:
    with _STATE_LOCK:
        data = _load()
        # ... modify ...
        _save(data)          # now uses atomic_write_text internally

def mark_run_complete() -> None:
    with _STATE_LOCK:
        data = _load()
        data["last_run_date"] = ...
        _save(data)
```

**Note:** if these functions are ever called from async context across multiple event-loop threads (e.g., concurrent `run_pipeline_for_agent` calls), a `threading.Lock` is sufficient because `asyncio.run` uses a single thread per call. If concurrency is ever introduced via `asyncio.gather`, switch to `asyncio.Lock`.

---

## Issue 5 — Per-Task Failures Silently Discarded in `asyncio.gather`

**File:** `src/agent/simulation.py:632-637`  
**Severity:** Low-Medium (observability / silent failure)

### Current Code

```python
tasks = [
    self._reply_to_thread(agent, thread)
    for thread in threads_to_reply
]
await asyncio.gather(*tasks, return_exceptions=True)
```

### Problem

`return_exceptions=True` causes `asyncio.gather` to return exceptions as result values instead of re-raising them. The return value here is discarded entirely, so any exceptions from individual `_reply_to_thread` calls are silently swallowed. If a Slack API error, DB write failure, or Claude API timeout occurs in any thread reply, it is invisible in logs and metrics. Operators have no signal that Phase 4 is partially or fully failing.

### Best Practice

When using `return_exceptions=True` the caller **must** inspect the results. The canonical pattern is to iterate the results list and log (or re-raise) any values that are `isinstance(r, BaseException)`. This is preferable to removing `return_exceptions=True` (which would cancel all remaining tasks on the first failure) because Phase 4 replies are independent — a failure on one thread should not prevent replies to others.

### How to Fix

Capture the return value of `asyncio.gather` and inspect each result:

```python
results = await asyncio.gather(*tasks, return_exceptions=True)

for thread, result in zip(threads_to_reply, results):
    if isinstance(result, BaseException):
        logger.error(
            "[%s] Phase 4: Failed to reply to thread %s: %s",
            agent.agent_id,
            thread.thread_id,
            result,
            exc_info=result,   # includes traceback in log record
        )
```

This pattern is appropriate anywhere `asyncio.gather(..., return_exceptions=True)` is used without inspecting results. There is a similar call site in `src/agent/simulation.py` for channel scanning — apply the same pattern there. Consider extracting a small helper:

```python
async def gather_logged(tasks: list, label: str) -> list:
    """gather with return_exceptions=True, logging each failure."""
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for i, r in enumerate(results):
        if isinstance(r, BaseException):
            logger.error("%s task[%d] failed: %s", label, i, r, exc_info=r)
    return results
```

---

## Summary Table

| # | File | Line(s) | Severity | Category |
|---|------|---------|----------|----------|
| 1 | `src/routers/auth.py` | 76-79 | High | Security — CSRF bypass |
| 2 | `src/agent/simulation.py` | 218-222 | Medium | Correctness — premature loop exit |
| 3 | `src/podcast/pipeline.py` + `main.py` | pipeline write order | Medium | Reliability — broken RSS enclosure |
| 4 | `src/agent/agent.py` + `src/podcast/state.py` | 428, 441, 22-24 | Medium | Data integrity — non-atomic writes |
| 5 | `src/agent/simulation.py` | 637 | Low-Medium | Observability — silent task failures |
