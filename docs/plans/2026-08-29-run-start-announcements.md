# Run-Start Slack Announcements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a fresh simulation run starts (`--fresh`), post one engine-authored
marker message to a configurable list of simulation Slack channels announcing the
run boundary — start time, planned duration, git commit + branch of the running
image, hub/PI prompt-set versions, and rubric version — without the marker ever
entering the simulation's own message log.

**Architecture:** A dependency-light `run_marker` module owns the reserved
sentinel prefix, the message template (an operator-editable file under the
bind-mounted `prompts/`, with a built-in fallback), and the
`is_run_start_marker` predicate. The engine gains `_announce_run_start()`,
called from `SimulationEngine.start()` only on `--fresh`, after star-topology
validation and immediately before the main loop; it posts with the hub's client
and records what it did into `SimulationRun.config`. Both Slack-ingest paths
(the live poller and the resume reconcile) skip sentinel-prefixed messages so a
marker can never be mirrored into `agent_messages`. Git identity is captured at
image build time into `.build_info.json` (with a pure-Python `.git` parse as
fallback); prompt-set versions are new `version` keys in the two `role.toml`
manifests plus a computed content hash, mirroring the rubric's
version-plus-hash pattern.

**Tech Stack:** Python 3.11 (stdlib `tomllib`, `hashlib`, `json`), SQLAlchemy
async, slack_sdk (already wrapped by `AgentSlackClient`), pytest +
`tests/fakes.py::FakeSlackClient`, Docker (one Dockerfile serves
blackbird-app/worker/agent).

**Spec:** the "Design summary" section below (approved in-chat 2026-08-29; no
separate spec file — bounded change).

## Design summary

- **Trigger:** fresh runs only (`SimulationEngine._fresh_start`). Resumes never
  announce. Slack-off / `--mock` / no connected client → skipped with one log
  line. `--fresh --no-db` still announces (run id rendered as "unrecorded").
- **Channels:** `Settings.run_start_announce_channels`, a comma-separated
  string of channel *names*, defaulting to the 6 `SEEDED_CHANNELS` plus
  `assessments-summary`. Empty string disables the feature. Names resolve
  through the engine's `_channel_id_map` (which holds every public channel
  after `_ensure_seeded_channels`) with a `client.get_channel_id` fallback;
  unresolvable names and `local:` placeholder ids are skipped with a WARNING.
  `.env` edits apply on the next `docker compose run` (the agent container is
  created fresh per run and reads `env_file: .env`) — no rebuild needed.
- **Message:** sentinel first line `:checkered_flag: NEW EXPERIMENTAL RUN`
  (prepended by code, never part of the template, so customization cannot break
  the sentinel), then a body rendered from
  `prompts/run_start_announcement.md` via `str.format_map` (operator-editable
  through the existing `prompts/` bind mount; missing or malformed template
  falls back to a built-in default). Placeholders: `run_id`, `started_at`,
  `run_duration`, `git_commit`, `git_branch`, `git_dirty`,
  `hub_prompts_version`, `hub_prompts_hash`, `pi_prompts_version`,
  `pi_prompts_hash`, `rubric_version`, `rubric_hash`.
- **Git identity:** the announced commit is the commit the *image* was built
  from — that is the code that actually runs (`src/` is baked into the agent
  image; see CLAUDE.md "The agent image does NOT mount src/"). Captured at
  build time by `scripts/write_build_info.py` (git installed transiently in
  that Dockerfile layer) into `/app/.build_info.json` with a dirty-file count;
  at runtime `src/services/build_info.py` reads that file, falling back to a
  pure-Python parse of `.git/HEAD` + refs + `packed-refs` (the whole repo,
  `.git` included, is already `COPY . .`-ed into every image — there is no
  `.dockerignore`), and finally to "unknown". Expected steady-state dirty count
  is 1 (the permanent uncommitted `docker-compose.prod.yml` edit).
- **Prompt-set versions:** no version exists today anywhere under
  `prompts/roles/` (verified). Introduce a top-level `version = "1.0.0"` key in
  `prompts/roles/scout_hub/role.toml` and `prompts/roles/pi_lab/role.toml`
  (`load_role` provably ignores unknown keys — it reads only `label`, `tools`,
  `calls_per_load_per_window`, `post_types`), plus
  `roles.prompt_set_stamp(role)` computing a sha256[:12] content hash over the
  role's manifest and its resolved prompt files — the same
  declared-version-plus-content-hash pattern as
  `src/services/blackbird_rubric.py` (`RUBRIC_VERSION` / `RUBRIC_CONTENT_HASH`,
  `hashlib.sha256(raw_bytes).hexdigest()[:12]`). File sets: pi_lab =
  `agent-system.md, identity.md, phase4-thread-reply.md, phase5-new-post.md`
  (the four `Agent._load_prompt` filenames, all resolving to `prompts/*.md`
  for pi_lab); scout_hub = the first three only (reply-only role: `post_types
  = []` in its role.toml and the engine hard-gates Phase 5, so
  `phase5-new-post.md` is never composed for it).
- **Ingestion guards:** `_poll_slack_for_bot_messages` and
  `_rebuild_state_from_slack` skip any top-level message matching
  `is_run_start_marker`, advancing `_poll_cursors[ch_id]` (both) and recording
  the ts in `_known_slack_ts` (reconcile only). Skipping the root also
  suppresses its thread-reply fetch, so replies (human or split-continuation
  chunks) to a marker never ingest either. `_seed_slack_cursors_without_ingest`
  needs no change (it appends nothing).
- **Durable record:** `SimulationRun.config` gains a
  `run_start_announcement` key (rendered text, per-channel ts map, failed
  names, timestamp) via the whole-dict-reassignment pattern (plain `JSON`
  column, no mutation tracking). Best-effort, never raises.
- **Non-goals:** no announcement on resume; no run-end marker; no
  `agent_messages` row for the marker; no enumeration of non-simulation
  workspace channels (`all-blackbird-copi`, `funding-opportunities`,
  `new-channel`, `social` are reachable only by adding them to the setting).

## Verified facts the plan relies on (checked 2026-08-29, HEAD `eeaca33`)

| Fact | Evidence |
|---|---|
| Prompt files per role resolve via `resolve_prompt_path` | `src/agent/agent.py:282`; filenames at `:286`, `:311`, `:401`, `:490` |
| `load_role` ignores unknown role.toml keys | `src/agent/roles.py:98-120` |
| Neither prompt-set doc embeds `role.toml`; doc-sync test skips non-`.md` blocks | `grep role.toml docs/specs/2026-08-07-*.md` → none; `tests/unit/test_doc_prompt_sync.py:38-40` |
| `.git` is in every image, no git binary, no `.dockerignore` | `Dockerfile` (`COPY . .`); `docker exec copi-blackbird-app-1 ls /app/.git/HEAD` → exists, `which git` → absent |
| Engine start() sequence and insertion point | `src/agent/simulation.py:810-885` |
| Live poller / reconcile loops and their cursor bookkeeping | `src/agent/simulation.py:5765-5876`, `:7099-7232`; `_known_slack_ts` seeded at `:6479` |
| `_channel_id_map` holds every public workspace channel after setup | `:6267` (`self._channel_id_map = dict(existing)`) |
| Engine-authored post precedent (guards to copy) | `_post_assessment_summary` `:3571-3704` |
| Hub Phase-3 auto-activation excludes the hub's own posts | `:2009-2047` (`exclude_agent_id`) |
| Emoji shortcodes round-trip verbatim through `conversations.history` | live read of `#assessments-summary` — `:mag:` prefix intact (LIVE-ONLY: not re-derivable from the repo; rests on this measurement + the `:mag:` headline precedent) |
| agent service: `env_file: .env`, mounts `./prompts` | `docker-compose.prod.yml:92-117` (working tree) |
| Test DB fixtures | `tests/conftest.py` — session-scoped `engine`, `async_sessionmaker(engine, expire_on_commit=False)` pattern |
| Engine unit-test harness pattern | `tests/unit/test_assessments_summary_post.py::_engine` |
| `FakeSlackClient` lacks `get_channel_id`/`cache_channel_ids` | `tests/fakes.py:336-461` |
| Workspace is dedicated (`blackbird-copi.slack.com`), 11 live public channels | live `conversations.list` via hub token, 2026-08-29 (LIVE-ONLY: not re-derivable from the repo) |

## Global Constraints

- **Never edit, stash, or check out `docker-compose.prod.yml`** (CLAUDE.md two-stack warning). This plan does not touch it.
- **Run every test/git command ON THE HOST via ssh**, never through the sshfs mount: `ssh ubuntu@ec2-3-21-33-147.us-east-2.compute.amazonaws.com "cd /home/ubuntu/blackbird-copi-science && <cmd>"` (sshfs pytest is 100-400x slower; `git status` through the mount times out). Shorthand below: `ON-HOST: <cmd>`.
- **Never run `pip install` against `.venv-test` from the sshfs client** (CLAUDE.md hazard 1). No new dependencies are added by this plan, so no install is needed.
- Do not edit any file under `prompts/roles/**/*.md` or `prompts/*.md`, and do not touch `src/agent/thread_guidance.py` — those are pinned by `.ambr` snapshots and the prompt-set docs. This plan adds only *new* files under `prompts/` and a `version` key to the two `role.toml` manifests (not embedded in any doc, not part of any composed prompt).
- Never run `pytest --snapshot-update`.
- The sentinel prefix string, once shipped, is load-bearing for ingestion filtering: changing it orphans markers already posted. It is defined in exactly one place (`run_marker.RUN_START_MARKER_PREFIX`).
- `./scripts/ci.sh` (ON-HOST) must pass before the final commit; it is the whole gate (no server-side CI).
- Do not start the simulation. Deploy/restart is the operator's decision (standing preference).

## File structure

- Create: `src/services/build_info.py` — read build/git identity (no deps beyond stdlib).
- Create: `scripts/write_build_info.py` — build-time capture (subprocess git → `.build_info.json`).
- Modify: `Dockerfile` — one RUN layer after `COPY . .` to invoke the capture script.
- Modify: `src/agent/roles.py` — `PromptSetStamp`, `ROLE_PROMPT_FILES`, `prompt_set_stamp()`.
- Modify: `prompts/roles/scout_hub/role.toml`, `prompts/roles/pi_lab/role.toml` — `version` key.
- Create: `src/agent/run_marker.py` — sentinel, template loading/rendering, predicate, channel-list parsing.
- Create: `prompts/run_start_announcement.md` — the operator-editable template.
- Modify: `src/config.py` — `run_start_announce_channels` setting.
- Modify: `src/agent/simulation.py` — ingest guards ×2, `_announce_run_start`, `_record_run_start_announcement`, one call in `start()`.
- Modify: `tests/fakes.py` — add `get_channel_id` / `cache_channel_ids` to `FakeSlackClient`.
- Modify: `.gitignore` — `.build_info.json`.
- Modify: `CLAUDE.md` — short operator section.
- Tests: `tests/unit/test_build_info.py`, `tests/unit/test_prompt_set_stamp.py`, `tests/unit/test_run_marker.py`, `tests/unit/test_run_start_announcement.py`, `tests/unit/test_run_marker_ingest_skip.py`.

---

### Task 1: Build/git identity (`build_info`)

**Files:**
- Create: `src/services/build_info.py`
- Create: `scripts/write_build_info.py`
- Modify: `.gitignore` (append `.build_info.json`)
- Test: `tests/unit/test_build_info.py`

**Interfaces:**
- Consumes: nothing (stdlib only).
- Produces: `get_build_info(root: Path | None = None) -> BuildInfo` where
  `BuildInfo` is a frozen dataclass with `commit: str | None`,
  `branch: str | None`, `dirty_files: int | None`, `source: str`
  (`"build_info_json" | "git_dir" | "unavailable"`). Task 5 calls
  `get_build_info()` with no argument.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_build_info.py
"""build_info: image-baked JSON preferred, pure-Python .git parse as fallback.

The agent image has no git binary but does carry .git (Dockerfile `COPY . .`,
no .dockerignore), so the runtime reader must never shell out to git.
"""
import json

from src.services.build_info import BuildInfo, get_build_info


def _git_dir(tmp_path, head: str, refs: dict[str, str] | None = None,
             packed: str | None = None):
    g = tmp_path / ".git"
    g.mkdir()
    (g / "HEAD").write_text(head, encoding="utf-8")
    for ref, sha in (refs or {}).items():
        p = g / ref
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(sha + "\n", encoding="utf-8")
    if packed is not None:
        (g / "packed-refs").write_text(packed, encoding="utf-8")


def test_prefers_build_info_json(tmp_path):
    (tmp_path / ".build_info.json").write_text(json.dumps(
        {"commit": "a" * 40, "branch": "blackbird", "dirty_files": 1}
    ), encoding="utf-8")
    _git_dir(tmp_path, "ref: refs/heads/other\n")  # must be ignored

    info = get_build_info(tmp_path)

    assert info == BuildInfo("a" * 40, "blackbird", 1, "build_info_json")


def test_falls_back_to_git_dir_loose_ref(tmp_path):
    _git_dir(tmp_path, "ref: refs/heads/blackbird\n",
             refs={"refs/heads/blackbird": "b" * 40})

    info = get_build_info(tmp_path)

    assert info.commit == "b" * 40
    assert info.branch == "blackbird"
    assert info.dirty_files is None  # unknowable without git
    assert info.source == "git_dir"


def test_git_dir_packed_ref(tmp_path):
    packed = (
        "# pack-refs with: peeled fully-peeled sorted\n"
        f"{'c' * 40} refs/heads/blackbird\n"
        f"{'d' * 40} refs/tags/v1\n"
        f"^{'e' * 40}\n"
    )
    _git_dir(tmp_path, "ref: refs/heads/blackbird\n", packed=packed)

    info = get_build_info(tmp_path)

    assert info.commit == "c" * 40
    assert info.branch == "blackbird"


def test_branch_names_with_slashes_survive(tmp_path):
    _git_dir(tmp_path, "ref: refs/heads/feat/x\n",
             refs={"refs/heads/feat/x": "f" * 40})

    assert get_build_info(tmp_path).branch == "feat/x"


def test_detached_head(tmp_path):
    _git_dir(tmp_path, "1" * 40 + "\n")

    info = get_build_info(tmp_path)

    assert info.commit == "1" * 40
    assert info.branch is None


def test_nothing_available(tmp_path):
    assert get_build_info(tmp_path) == BuildInfo(None, None, None, "unavailable")


def test_malformed_json_falls_through_to_git_dir(tmp_path):
    (tmp_path / ".build_info.json").write_text("{not json", encoding="utf-8")
    _git_dir(tmp_path, "ref: refs/heads/blackbird\n",
             refs={"refs/heads/blackbird": "9" * 40})

    info = get_build_info(tmp_path)

    assert info.source == "git_dir"
    assert info.commit == "9" * 40
```

- [ ] **Step 2: Run tests to verify they fail**

ON-HOST: `.venv-test/bin/python -m pytest tests/unit/test_build_info.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.services.build_info'`

- [ ] **Step 3: Write the implementation**

```python
# src/services/build_info.py
"""Which code is this process actually running?

The agent image bakes ``src/`` at build time (see CLAUDE.md: "The agent image
does NOT mount src/"), so the truthful answer to "what commit is running" is
the commit the IMAGE was built from, not whatever the host repo says at launch.
Two sources, in order:

1. ``.build_info.json`` at the repo/image root — written by
   ``scripts/write_build_info.py`` during the Docker build, the only moment a
   git binary is available (``python:3.11-slim`` ships none). Carries a
   ``dirty_files`` count, which the fallback below cannot know.
2. A pure-Python read of ``.git/HEAD`` + loose refs + ``packed-refs`` — works
   because the whole repo, ``.git`` included, is ``COPY . .``-ed into every
   image (there is deliberately no ``.dockerignore``; if one ever appears and
   excludes ``.git``, this fallback degrades to "unavailable" rather than
   breaking, which is why every read below is guarded).

Dependency-free on purpose: imported by the simulation engine at startup and
by tests that never touch a database.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: src/services/build_info.py -> parents[2] is the repo root on the host and
#: /app inside the image — the two layouts are identical by construction.
REPO_ROOT = Path(__file__).resolve().parents[2]

BUILD_INFO_FILENAME = ".build_info.json"


@dataclass(frozen=True)
class BuildInfo:
    commit: str | None
    branch: str | None
    #: Count of modified TRACKED files at image build time (``git status
    #: --porcelain --untracked-files=no | wc -l``). None when only the .git
    #: fallback was available — the dirty state is unknowable without git.
    dirty_files: int | None
    source: str  # "build_info_json" | "git_dir" | "unavailable"


def _from_json(root: Path) -> BuildInfo | None:
    path = root / BUILD_INFO_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    commit = data.get("commit")
    branch = data.get("branch")
    dirty = data.get("dirty_files")
    return BuildInfo(
        commit=str(commit) if commit else None,
        branch=str(branch) if branch else None,
        dirty_files=int(dirty) if isinstance(dirty, int) else None,
        source="build_info_json",
    )


def _resolve_ref(git_dir: Path, ref: str) -> str | None:
    loose = git_dir / ref
    try:
        return loose.read_text(encoding="utf-8").strip() or None
    except OSError:
        pass
    try:
        for line in (git_dir / "packed-refs").read_text(encoding="utf-8").splitlines():
            # "<sha> <refname>"; '#' comments and '^' peel lines are skipped.
            if line.startswith(("#", "^")) or " " not in line:
                continue
            sha, _, name = line.partition(" ")
            if name.strip() == ref:
                return sha.strip() or None
    except OSError:
        pass
    return None


def _from_git_dir(root: Path) -> BuildInfo | None:
    git_dir = root / ".git"
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if head.startswith("ref: "):
        ref = head.removeprefix("ref: ").strip()
        # removeprefix, not rsplit: branch names may contain '/', e.g. feat/x.
        branch = ref.removeprefix("refs/heads/")
        return BuildInfo(_resolve_ref(git_dir, ref), branch, None, "git_dir")
    if head:  # detached HEAD: the line IS the sha
        return BuildInfo(head, None, None, "git_dir")
    return None


def get_build_info(root: Path | None = None) -> BuildInfo:
    """Never raises: an unreadable identity degrades to 'unavailable'."""
    root = root or REPO_ROOT
    info = _from_json(root)
    if info is not None:
        return info
    info = _from_git_dir(root)
    if info is not None:
        return info
    return BuildInfo(None, None, None, "unavailable")
```

```python
# scripts/write_build_info.py
"""Capture git identity into .build_info.json — run at IMAGE BUILD time.

Invoked by the Dockerfile in the one layer where a git binary exists (installed
and purged in the same RUN). Fails LOUDLY on any git error: a build that cannot
say what it built should not succeed silently. See src/services/build_info.py
for the runtime reader and the fallback order.
"""
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def main() -> int:
    try:
        info = {
            "commit": _git("rev-parse", "HEAD"),
            "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty_files": len(
                _git("status", "--porcelain", "--untracked-files=no").splitlines()
            ),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "generator": "scripts/write_build_info.py",
        }
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"write_build_info: git failed: {exc}", file=sys.stderr)
        return 1
    (ROOT / ".build_info.json").write_text(
        json.dumps(info, indent=2) + "\n", encoding="utf-8"
    )
    print(f"write_build_info: {info['commit'][:12]} on {info['branch']}, "
          f"{info['dirty_files']} dirty file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Append to `.gitignore` (after the `backups/` block):

```
# Baked git identity for the runtime announcement (scripts/write_build_info.py,
# written during docker build; read by src/services/build_info.py)
.build_info.json
```

- [ ] **Step 4: Run tests to verify they pass**

ON-HOST: `.venv-test/bin/python -m pytest tests/unit/test_build_info.py -v`
Expected: 7 passed

- [ ] **Step 5: Ruff and commit**

ON-HOST: `.venv-test/bin/python -m ruff check src/services/build_info.py scripts/write_build_info.py tests/unit/test_build_info.py`
Expected: no findings.

ON-HOST:
```bash
git add src/services/build_info.py scripts/write_build_info.py tests/unit/test_build_info.py .gitignore
git commit -m "feat(announce): build/git identity reader with image-baked JSON and .git fallback

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Prompt-set versions (`prompt_set_stamp`)

**Files:**
- Modify: `src/agent/roles.py` (append after `load_role`)
- Modify: `prompts/roles/scout_hub/role.toml`, `prompts/roles/pi_lab/role.toml`
- Test: `tests/unit/test_prompt_set_stamp.py`

**Interfaces:**
- Consumes: `resolve_prompt_path`, `ROLES_DIR`, module-level constants in `roles.py` (read at call time so tests can monkeypatch them, same pattern as `tests/unit/test_roles.py`).
- Produces: `prompt_set_stamp(role: str) -> PromptSetStamp` (frozen dataclass: `role: str`, `version: str`, `content_hash: str` — 12 hex chars). Task 5 calls it with `"scout_hub"` and `"pi_lab"` only; `ROLE_PROMPT_FILES: dict[str, tuple[str, ...]]` defines exactly those two keys.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_prompt_set_stamp.py
"""prompt_set_stamp: the declared version + content hash of a role's prompt set.

Mirrors the rubric's pattern (blackbird_rubric.py: [meta].version +
sha256[:12] of the bytes): the version is a human declaration, the hash is the
drift alarm that catches an edit nobody bumped the version for.
"""
import pytest

from src.agent import roles
from src.agent.roles import ROLE_PROMPT_FILES, load_role, prompt_set_stamp


def _prompt_tree(tmp_path, monkeypatch):
    """A minimal prompts/ tree covering both roles' file sets."""
    monkeypatch.setattr(roles, "PROMPTS_DIR", tmp_path)
    monkeypatch.setattr(roles, "ROLES_DIR", tmp_path / "roles")
    for name in ROLE_PROMPT_FILES["pi_lab"]:
        (tmp_path / name).write_text(f"base {name}", encoding="utf-8")
    hub = tmp_path / "roles" / "scout_hub"
    hub.mkdir(parents=True)
    (hub / "role.toml").write_text('version = "2.0.0"\nlabel = "Hub"\n', encoding="utf-8")
    for name in ROLE_PROMPT_FILES["scout_hub"]:
        (hub / name).write_text(f"hub {name}", encoding="utf-8")
    pi = tmp_path / "roles" / "pi_lab"
    pi.mkdir(parents=True)
    (pi / "role.toml").write_text('version = "1.5.0"\n', encoding="utf-8")
    return tmp_path


def test_version_comes_from_role_toml(tmp_path, monkeypatch):
    _prompt_tree(tmp_path, monkeypatch)
    assert prompt_set_stamp("scout_hub").version == "2.0.0"
    assert prompt_set_stamp("pi_lab").version == "1.5.0"


def test_missing_version_key_reads_unversioned(tmp_path, monkeypatch):
    _prompt_tree(tmp_path, monkeypatch)
    hub_manifest = tmp_path / "roles" / "scout_hub" / "role.toml"
    hub_manifest.write_text('label = "Hub"\n', encoding="utf-8")
    assert prompt_set_stamp("scout_hub").version == "unversioned"


def test_hash_is_12_hex_and_stable(tmp_path, monkeypatch):
    _prompt_tree(tmp_path, monkeypatch)
    a = prompt_set_stamp("scout_hub").content_hash
    b = prompt_set_stamp("scout_hub").content_hash
    assert a == b
    assert len(a) == 12
    int(a, 16)  # raises if not hex


def test_hash_changes_when_a_prompt_file_changes(tmp_path, monkeypatch):
    _prompt_tree(tmp_path, monkeypatch)
    before = prompt_set_stamp("scout_hub").content_hash
    hub_file = tmp_path / "roles" / "scout_hub" / "agent-system.md"
    hub_file.write_text("hub agent-system.md EDITED", encoding="utf-8")
    assert prompt_set_stamp("scout_hub").content_hash != before


def test_pi_hash_ignores_hub_overrides_and_vice_versa(tmp_path, monkeypatch):
    _prompt_tree(tmp_path, monkeypatch)
    pi_before = prompt_set_stamp("pi_lab").content_hash
    hub_file = tmp_path / "roles" / "scout_hub" / "identity.md"
    hub_file.write_text("hub identity EDITED", encoding="utf-8")
    assert prompt_set_stamp("pi_lab").content_hash == pi_before


def test_pi_hash_covers_phase5_but_hub_hash_does_not(tmp_path, monkeypatch):
    _prompt_tree(tmp_path, monkeypatch)
    hub_before = prompt_set_stamp("scout_hub").content_hash
    pi_before = prompt_set_stamp("pi_lab").content_hash
    (tmp_path / "phase5-new-post.md").write_text("EDITED", encoding="utf-8")
    assert prompt_set_stamp("scout_hub").content_hash == hub_before
    assert prompt_set_stamp("pi_lab").content_hash != pi_before


def test_unknown_role_raises_key_error(tmp_path, monkeypatch):
    _prompt_tree(tmp_path, monkeypatch)
    with pytest.raises(KeyError):
        prompt_set_stamp("grantbot")


def test_missing_file_hashes_as_missing_not_crash(tmp_path, monkeypatch):
    _prompt_tree(tmp_path, monkeypatch)
    (tmp_path / "phase5-new-post.md").unlink()
    stamp = prompt_set_stamp("pi_lab")  # must not raise
    assert len(stamp.content_hash) == 12


def test_real_role_tomls_declare_a_version_and_still_parse():
    """Against the REAL prompts/ tree: the version keys this task adds must
    exist, and load_role must keep ignoring them (it reads only label/tools/
    calls_per_load_per_window/post_types)."""
    for role in ("scout_hub", "pi_lab"):
        assert prompt_set_stamp(role).version not in ("", "unversioned")
    spec = load_role("scout_hub")
    assert spec.label == "Scout Hub"  # unchanged by the new key
```

- [ ] **Step 2: Run tests to verify they fail**

ON-HOST: `.venv-test/bin/python -m pytest tests/unit/test_prompt_set_stamp.py -v`
Expected: FAIL — `ImportError: cannot import name 'ROLE_PROMPT_FILES'`

- [ ] **Step 3: Write the implementation**

Append to `src/agent/roles.py`:

```python
import hashlib  # add to the module's import block at the top


#: The prompt files a role actually composes, per Agent._load_prompt's call
#: sites (src/agent/agent.py:286/:311/:401/:490). scout_hub deliberately
#: omits phase5-new-post.md: the hub is reply-only (post_types = [] in its
#: role.toml, and the engine hard-gates Phase 5 for it), so that file is never
#: composed for the hub and a pi-side edit to it must not move the hub's hash.
ROLE_PROMPT_FILES: dict[str, tuple[str, ...]] = {
    "pi_lab": (
        "agent-system.md", "identity.md",
        "phase4-thread-reply.md", "phase5-new-post.md",
    ),
    "scout_hub": (
        "agent-system.md", "identity.md", "phase4-thread-reply.md",
    ),
}


@dataclass(frozen=True)
class PromptSetStamp:
    """Declared version + computed content hash of one role's prompt set.

    Same pattern as the rubric (src/services/blackbird_rubric.py): the version
    is what a human declared in role.toml, the sha256[:12] hash is what the
    files actually contain — a hash change without a version bump means an
    edit nobody recorded.
    """

    role: str
    version: str
    content_hash: str


def prompt_set_stamp(role: str) -> PromptSetStamp:
    """Stamp for a role in ROLE_PROMPT_FILES. Raises KeyError for any other
    role — the two announced roles are a closed set, and a silent default
    would stamp a role with files it does not use.

    The hash covers the role.toml manifest (when present) plus each resolved
    prompt file, keyed by FILENAME (not path) so the value is stable across
    checkouts. A missing file hashes as the literal b"<missing>" rather than
    raising: the announcement must never take down a run start.
    """
    filenames = ROLE_PROMPT_FILES[role]
    h = hashlib.sha256()
    manifest = ROLES_DIR / role / "role.toml"
    parts: list[tuple[str, Path]] = []
    if manifest.is_file():
        parts.append(("role.toml", manifest))
    parts += [(name, resolve_prompt_path(role, name)) for name in filenames]
    for name, path in parts:
        h.update(name.encode("utf-8"))
        h.update(b"\0")
        try:
            h.update(path.read_bytes())
        except OSError:
            h.update(b"<missing>")
        h.update(b"\0")

    version = "unversioned"
    if manifest.is_file():
        try:
            declared = tomllib.loads(manifest.read_text(encoding="utf-8")).get("version")
            if declared:
                version = str(declared)
        except (tomllib.TOMLDecodeError, OSError):
            pass  # load_role already logs malformed manifests
    return PromptSetStamp(role=role, version=version, content_hash=h.hexdigest()[:12])
```

Note: `ROLES_DIR` and `resolve_prompt_path` must be read from the module at
call time exactly as written above (they already are — plain module globals),
so `monkeypatch.setattr(roles, "ROLES_DIR", ...)` works.

Add to the TOP of `prompts/roles/scout_hub/role.toml`:

```toml
# Version of the scout_hub PROMPT SET (this manifest + agent-system.md,
# identity.md, phase4-thread-reply.md in this directory). Bump on ANY edit to
# those files; the run-start announcement stamps every run with this version
# plus a content hash (src/agent/roles.py::prompt_set_stamp), and a hash
# change without a version bump is an unrecorded edit. Read by
# prompt_set_stamp only — load_role ignores unknown keys.
version = "1.0.0"
```

Add to the TOP of `prompts/roles/pi_lab/role.toml`:

```toml
# Version of the pi_lab PROMPT SET (this manifest + the four BASE prompt files
# prompts/{agent-system,identity,phase4-thread-reply,phase5-new-post}.md —
# pi_lab is the absence of overrides, see src/agent/roles.py). Bump on ANY
# edit to those files; see the matching note in scout_hub/role.toml.
version = "1.0.0"
```

- [ ] **Step 4: Run new tests plus the guards on neighboring behavior**

ON-HOST: `.venv-test/bin/python -m pytest tests/unit/test_prompt_set_stamp.py tests/unit/test_roles.py tests/unit/test_doc_prompt_sync.py tests/unit/test_role_menus.py tests/unit/test_agent_prompts.py -v`
Expected: all pass — the doc-sync test skips `.toml` blocks and neither doc
embeds role.toml, so the version keys trip nothing.

- [ ] **Step 5: Ruff and commit**

ON-HOST: `.venv-test/bin/python -m ruff check src/agent/roles.py tests/unit/test_prompt_set_stamp.py`

ON-HOST:
```bash
git add src/agent/roles.py prompts/roles/scout_hub/role.toml prompts/roles/pi_lab/role.toml tests/unit/test_prompt_set_stamp.py
git commit -m "feat(announce): versioned prompt-set stamps (role.toml version + content hash)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: The marker module (`run_marker`) + template + setting

**Files:**
- Create: `src/agent/run_marker.py`
- Create: `prompts/run_start_announcement.md`
- Modify: `src/config.py` (one field, next to `max_thread_messages` around line 330)
- Modify: `tests/unit/test_config_secret_redaction.py` (classify the new str field)
- Test: `tests/unit/test_run_marker.py`

**Interfaces:**
- Consumes: `src.agent.roles` (module attribute `PROMPTS_DIR`, read at call time).
- Produces (Task 4 and 5 use these exact names):
  - `RUN_START_MARKER_PREFIX: str`
  - `is_run_start_marker(text: str | None) -> bool`
  - `render_run_start_announcement(values: dict[str, str]) -> str` — always
    returns text whose first line is the sentinel; never raises.
  - `ANNOUNCEMENT_VALUE_KEYS: tuple[str, ...]` — the 12 placeholder names.
  - `parse_announce_channels(raw: str) -> list[str]`
  - `Settings.run_start_announce_channels: str` (config.py).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_run_marker.py
"""run_marker: the sentinel, the template, and the channel-list parser.

The sentinel prefix is load-bearing: both Slack-ingest paths drop matching
messages (see test_run_marker_ingest_skip.py), so the prefix must be
engine-prepended (customization can't remove it) and stable under the
markdown->mrkdwn conversion every outbound post goes through.
"""
import pytest

from src.agent import roles
from src.agent.channels import ASSESSMENTS_SUMMARY_CHANNEL, SEEDED_CHANNELS
from src.agent.run_marker import (
    ANNOUNCEMENT_VALUE_KEYS,
    RUN_START_MARKER_PREFIX,
    is_run_start_marker,
    parse_announce_channels,
    render_run_start_announcement,
)
from src.agent.slack_client import markdown_to_mrkdwn
from src.config import Settings


VALUES = {k: f"<{k}>" for k in ANNOUNCEMENT_VALUE_KEYS}


@pytest.fixture
def no_template(tmp_path, monkeypatch):
    """Point the template at an empty dir so the built-in default renders."""
    monkeypatch.setattr(roles, "PROMPTS_DIR", tmp_path)
    return tmp_path


def test_rendered_text_starts_with_the_sentinel(no_template):
    text = render_run_start_announcement(VALUES)
    assert text.startswith(RUN_START_MARKER_PREFIX)
    assert is_run_start_marker(text)


def test_default_template_carries_every_value(no_template):
    text = render_run_start_announcement(VALUES)
    for key in ANNOUNCEMENT_VALUE_KEYS:
        assert f"<{key}>" in text, f"default template must render {{{key}}}"


def test_template_file_overrides_the_default(no_template):
    (no_template / "run_start_announcement.md").write_text(
        "custom body: {run_id}", encoding="utf-8"
    )
    text = render_run_start_announcement(VALUES)
    assert "custom body: <run_id>" in text
    assert text.startswith(RUN_START_MARKER_PREFIX)  # prefix is not the template's job


def test_template_cannot_remove_the_sentinel(no_template):
    (no_template / "run_start_announcement.md").write_text(
        "no placeholders at all", encoding="utf-8"
    )
    assert is_run_start_marker(render_run_start_announcement(VALUES))


def test_bad_placeholder_falls_back_to_default(no_template, caplog):
    (no_template / "run_start_announcement.md").write_text(
        "broken {no_such_placeholder}", encoding="utf-8"
    )
    text = render_run_start_announcement(VALUES)
    assert "broken" not in text
    assert "<run_id>" in text  # the default rendered instead
    assert any("run_start_announcement" in r.getMessage() for r in caplog.records)


def test_sentinel_survives_mrkdwn_conversion(no_template):
    assert is_run_start_marker(markdown_to_mrkdwn(render_run_start_announcement(VALUES)))


def test_predicate_rejects_ordinary_messages():
    assert not is_run_start_marker("a normal post about :checkered_flag: racing")
    assert not is_run_start_marker("")
    assert not is_run_start_marker(None)


def test_parse_announce_channels():
    assert parse_announce_channels(" general , social ,,") == ["general", "social"]
    assert parse_announce_channels("") == []


def test_setting_default_is_the_simulation_channels():
    """Drift alarm: the literal default in config.py must equal the seeded
    channels plus assessments-summary (config.py cannot import channels.py)."""
    default = Settings.model_fields["run_start_announce_channels"].default
    assert parse_announce_channels(default) == SEEDED_CHANNELS + [ASSESSMENTS_SUMMARY_CHANNEL]


def test_shipped_template_file_renders_cleanly():
    """Against the REAL prompts/ tree: the shipped template must format with
    exactly the documented keys (an operator edit that breaks it degrades to
    the default at runtime, but we ship it working)."""
    text = render_run_start_announcement(VALUES)
    assert is_run_start_marker(text)
    assert "<run_id>" in text
```

- [ ] **Step 2: Run tests to verify they fail**

ON-HOST: `.venv-test/bin/python -m pytest tests/unit/test_run_marker.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.agent.run_marker'`

- [ ] **Step 3: Write the implementation**

```python
# src/agent/run_marker.py
"""Run-start announcement: sentinel, template, rendering, channel list.

The engine posts one marker per configured channel when a --fresh run starts
(SimulationEngine._announce_run_start). Everything here is deliberately
dependency-light (no models, no DB) so the ingest paths can import the
predicate and unit tests need nothing but a tmp dir.

THE PREFIX IS LOAD-BEARING. Both Slack-ingest paths
(_poll_slack_for_bot_messages and _rebuild_state_from_slack in
src/agent/simulation.py) drop any message matching is_run_start_marker so the
engine's own markers are never mirrored into agent_messages — without that, the
first restart of a fresh run would re-ingest the marker as a bot post
(_known_slack_ts is seeded from stored rows only). Changing the prefix orphans
every marker already posted: they would start being ingested on the next
resume. Do not change it casually; if it must change, keep the old prefix
recognized alongside the new one.

The prefix is PREPENDED BY CODE, never part of the template, so operator
customization of prompts/run_start_announcement.md cannot break the sentinel.
"""

from __future__ import annotations

import logging

from src.agent import roles

logger = logging.getLogger(__name__)

RUN_START_MARKER_PREFIX = ":checkered_flag: NEW EXPERIMENTAL RUN"

TEMPLATE_FILENAME = "run_start_announcement.md"

#: Every placeholder the template may use. The engine supplies exactly these.
ANNOUNCEMENT_VALUE_KEYS: tuple[str, ...] = (
    "run_id", "started_at", "run_duration",
    "git_commit", "git_branch", "git_dirty",
    "hub_prompts_version", "hub_prompts_hash",
    "pi_prompts_version", "pi_prompts_hash",
    "rubric_version", "rubric_hash",
)

#: Fallback body when the template file is missing or malformed. Kept in sync
#: with the SHIPPED prompts/run_start_announcement.md by hand — they may drift
#: once an operator customizes the file, which is the point of the file.
DEFAULT_TEMPLATE = """\
*A new simulation run is starting.*
- Run: {run_id}
- Started: {started_at}
- Planned duration: {run_duration}
- Code: commit {git_commit} on branch {git_branch} ({git_dirty})
- Hub prompts: v{hub_prompts_version} (hash {hub_prompts_hash})
- PI prompts: v{pi_prompts_version} (hash {pi_prompts_hash})
- Rubric: v{rubric_version} (hash {rubric_hash})
Messages above this line belong to earlier runs."""


def is_run_start_marker(text: str | None) -> bool:
    """True for engine-authored run-start markers (and nothing else the
    system writes: the prefix is reserved — no prompt offers it, and the
    realistic misfire is a foreign bot opening a message with the exact
    prefix, whose only cost is that one message not being mirrored)."""
    return bool(text) and text.lstrip().startswith(RUN_START_MARKER_PREFIX)


def _template_body() -> str:
    """The operator's template if present and readable, else the default.

    Read at call time (not import) from roles.PROMPTS_DIR so the bind-mounted
    prompts/ directory is honoured and tests can monkeypatch the dir.
    """
    path = roles.PROMPTS_DIR / TEMPLATE_FILENAME
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return DEFAULT_TEMPLATE


def render_run_start_announcement(values: dict[str, str]) -> str:
    """Render the announcement. Never raises; always sentinel-prefixed.

    A malformed operator template (unknown placeholder, stray braces) degrades
    to the built-in default with one WARNING — a broken customization must not
    cost the run its announcement.
    """
    body = _template_body()
    try:
        rendered = body.format_map(values)
    except (KeyError, IndexError, ValueError) as exc:
        logger.warning(
            "prompts/%s failed to render (%s: %s) — using the built-in "
            "default announcement body",
            TEMPLATE_FILENAME, type(exc).__name__, exc,
        )
        rendered = DEFAULT_TEMPLATE.format_map(values)
    return f"{RUN_START_MARKER_PREFIX}\n{rendered}"


def parse_announce_channels(raw: str) -> list[str]:
    """Comma-separated channel names -> list. Empty string -> [] (feature off)."""
    return [c.strip() for c in raw.split(",") if c.strip()]
```

Create `prompts/run_start_announcement.md` (byte-identical to
`DEFAULT_TEMPLATE`, plus this header comment is NOT included — the file is the
template body only):

```markdown
*A new simulation run is starting.*
- Run: {run_id}
- Started: {started_at}
- Planned duration: {run_duration}
- Code: commit {git_commit} on branch {git_branch} ({git_dirty})
- Hub prompts: v{hub_prompts_version} (hash {hub_prompts_hash})
- PI prompts: v{pi_prompts_version} (hash {pi_prompts_hash})
- Rubric: v{rubric_version} (hash {rubric_hash})
Messages above this line belong to earlier runs.
```

Add to `src/config.py`, in the agent-simulation settings block (near
`max_thread_messages`, currently line ~330):

```python
    # Channels (by NAME, comma-separated) that receive the engine-authored
    # run-start announcement when a --fresh run begins. Default: the six
    # seeded channels plus assessments-summary — the simulation's own
    # channels, deliberately not the whole workspace
    # (tests/unit/test_run_marker.py pins the equivalence to SEEDED_CHANNELS,
    # which config.py must not import). Empty string disables the
    # announcement. The message body is prompts/run_start_announcement.md;
    # the :checkered_flag: sentinel line is prepended by code and both
    # Slack-ingest paths drop messages carrying it. See
    # src/agent/run_marker.py.
    run_start_announce_channels: str = (
        "general,drug-repurposing,structural-biology,aging-and-longevity,"
        "single-cell-omics,chemical-biology,assessments-summary"
    )
```

Every plain-`str` Settings field must be classified by the secret-redaction
sweep (`tests/unit/test_config_secret_redaction.py` — its docstring says
adding a clear-rendering field "forces an edit here"; without this,
`test_every_string_field_is_classified_secret_or_not` and
`test_a_credential_in_a_url_path_is_a_known_gap` fail at the Task 6 full
gate). Add to `NON_SECRET_STR_FIELDS` (around line 139-157), keeping the
set's existing formatting:

```python
    # Channel names for the run-start announcement — public channel names,
    # not credentials.
    "run_start_announce_channels",
```

- [ ] **Step 4: Run tests to verify they pass**

ON-HOST: `.venv-test/bin/python -m pytest tests/unit/test_run_marker.py tests/unit/test_config_secret_redaction.py -v`
Expected: the 10 new tests pass, and the redaction sweep stays green (the
new field is classified non-secret).

- [ ] **Step 5: Ruff and commit**

ON-HOST: `.venv-test/bin/python -m ruff check src/agent/run_marker.py src/config.py tests/unit/test_run_marker.py`

ON-HOST:
```bash
git add src/agent/run_marker.py prompts/run_start_announcement.md src/config.py tests/unit/test_run_marker.py tests/unit/test_config_secret_redaction.py
git commit -m "feat(announce): run-start marker module, editable template, channel setting

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Ingestion guards (poller + reconcile)

**Files:**
- Modify: `src/agent/simulation.py` — two loops (`_poll_slack_for_bot_messages` ~line 5822; `_rebuild_state_from_slack` ~line 7144) + one import
- Test: `tests/unit/test_run_marker_ingest_skip.py`

**Interfaces:**
- Consumes: `is_run_start_marker` from Task 3.
- Produces: nothing new — the invariant "a sentinel-prefixed Slack message never enters the MessageLog" that Task 5's announcement relies on.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_run_marker_ingest_skip.py
"""Neither Slack-ingest path may mirror a run-start marker into the log.

The marker has no agent_messages row by design, so without these skips the
resume reconcile re-ingests it (it is absent from _known_slack_ts, seeded from
stored rows only — simulation.py:6479) and the live poller fetches it on the
first tick of the fresh run that posted it (it posts AFTER the cursor seed).
Cursor bookkeeping is part of the contract: a skipped marker that is a
channel's newest message must still advance the cursor, or every later tick
re-fetches it forever.
"""
import pytest

from src.agent.agent import Agent
from src.agent.run_marker import RUN_START_MARKER_PREFIX
from src.agent.simulation import SimulationEngine
from tests.fakes import FakeSlackClient

pytestmark = pytest.mark.asyncio


MARKER_TS = "1700000600.000000"
NORMAL_TS = "1700000500.000000"


def _engine(monkeypatch, tmp_path):
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    client = FakeSlackClient(agent_id="blackbird")
    eng = SimulationEngine(agents=[hub], slack_clients={"blackbird": client})
    eng._channel_id_map["general"] = "C-GENERAL"
    client.channel_history["C-GENERAL"] = [
        {"ts": NORMAL_TS, "text": "an ordinary bot post",
         "bot_id": "B1", "user": "U1", "username": "OtherBot"},
        {"ts": MARKER_TS,
         "text": f"{RUN_START_MARKER_PREFIX}\nRun: x", "bot_id": "B1",
         "user": "U_blackbird", "username": "BlackbirdBot"},
    ]
    return eng, client


async def test_reconcile_skips_the_marker_and_advances_cursor(monkeypatch, tmp_path):
    eng, client = _engine(monkeypatch, tmp_path)

    await eng._rebuild_state_from_slack()

    assert eng.message_log.get_entry(NORMAL_TS) is not None
    assert eng.message_log.get_entry(MARKER_TS) is None
    assert eng._poll_cursors["C-GENERAL"] == MARKER_TS
    assert MARKER_TS in eng._known_slack_ts


async def test_live_poller_skips_the_marker_and_advances_cursor(monkeypatch, tmp_path):
    eng, client = _engine(monkeypatch, tmp_path)
    eng._poll_cursors["C-GENERAL"] = NORMAL_TS  # marker is the only new message
    eng._last_channel_poll = 0.0

    await eng._poll_slack_for_bot_messages()

    assert eng.message_log.get_entry(MARKER_TS) is None
    assert eng._poll_cursors["C-GENERAL"] == MARKER_TS


async def test_reconcile_never_fetches_replies_under_a_marker(monkeypatch, tmp_path):
    eng, client = _engine(monkeypatch, tmp_path)
    client.channel_history["C-GENERAL"][1]["reply_count"] = 2
    calls = []

    async def _no_replies(*a, **k):
        calls.append(a)
        return []

    monkeypatch.setattr(client, "aget_all_thread_replies", _no_replies)

    await eng._rebuild_state_from_slack()

    assert calls == []  # the skipped root's reply fetch never fired


async def test_ordinary_bot_posts_still_ingest(monkeypatch, tmp_path):
    """Guard against an over-eager predicate: the skip must not eat normal
    traffic (the poller path)."""
    eng, client = _engine(monkeypatch, tmp_path)
    eng._last_channel_poll = 0.0

    await eng._poll_slack_for_bot_messages()

    assert eng.message_log.get_entry(NORMAL_TS) is not None
```

- [ ] **Step 2: Run tests to verify they fail**

ON-HOST: `.venv-test/bin/python -m pytest tests/unit/test_run_marker_ingest_skip.py -v`
Expected: the two skip tests and the reply test FAIL (marker gets ingested); the ordinary-post test passes.

- [ ] **Step 3: Implement the two guards**

Add to `src/agent/simulation.py`'s import block:

```python
from src.agent.run_marker import is_run_start_marker
```

In `_poll_slack_for_bot_messages`, immediately after `ts = msg.get("ts", "")`
(currently line 5823, before the `user_id`/`is_bot` lines so the skip also
saves the `ais_bot_user` API call):

```python
                    # Engine-authored run-start markers are operational
                    # signage, not conversation: never mirror one into the
                    # log, but advance the cursor past it or this tick's
                    # newest message gets re-fetched forever. See
                    # src/agent/run_marker.py (prefix contract).
                    if is_run_start_marker(msg.get("text")):
                        if ts:
                            self._poll_cursors[ch_id] = ts
                        continue
```

In `_rebuild_state_from_slack`, immediately after `ts = msg.get("ts", "")`
(currently line 7145, before the `_known_slack_ts` dedup check):

```python
                # Run-start markers are never ingested (see the matching skip
                # in _poll_slack_for_bot_messages). Recording the ts in
                # _known_slack_ts and advancing the cursor mirrors what the
                # loop does for a dedup hit; skipping the root also skips its
                # reply_count branch, so replies under a marker (human
                # comments, split-continuation chunks) never ingest either.
                if is_run_start_marker(msg.get("text")):
                    if ts:
                        self._known_slack_ts.add(ts)
                        self._poll_cursors[ch_id] = ts
                    continue
```

- [ ] **Step 4: Run the new tests plus the neighboring ingest tests**

ON-HOST: `.venv-test/bin/python -m pytest tests/unit/test_run_marker_ingest_skip.py tests/unit/test_fresh_start_slack_restore.py tests/unit/test_fresh_start_cursor_seed.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

ON-HOST:
```bash
git add src/agent/simulation.py tests/unit/test_run_marker_ingest_skip.py
git commit -m "feat(announce): both Slack-ingest paths drop run-start markers with cursor bookkeeping

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: `_announce_run_start` + the `start()` call site

**Files:**
- Modify: `src/agent/simulation.py` — two new methods (place them after `_mark_summary_posted`, before `_announce_owed_headline`) + one gated call in `start()` + imports
- Modify: `tests/fakes.py` — add `get_channel_id` / `cache_channel_ids` to `FakeSlackClient`
- Test: `tests/unit/test_run_start_announcement.py`

**Interfaces:**
- Consumes: `render_run_start_announcement`, `parse_announce_channels`, `ANNOUNCEMENT_VALUE_KEYS` semantics (Task 3); `get_build_info` (Task 1); `prompt_set_stamp` (Task 2); `RUBRIC_VERSION` / `RUBRIC_CONTENT_HASH` (already imported at simulation.py:61); engine attrs `self._fresh_start`, `self.max_runtime_minutes`, `self._start_time`, `self.simulation_run_id`, `self.session_factory`, `self._channel_id_map`, `self.slack_clients`, `self.agents`, `self._next_poll_client()`.
- Produces: `async _announce_run_start(self) -> None`; `async _record_run_start_announcement(self, text: str, posted: dict[str, str], failed: list[str]) -> None`.

- [ ] **Step 1: Extend FakeSlackClient (no test yet — protocol conformance)**

Add to `tests/fakes.py::FakeSlackClient`, next to `list_channels`:

```python
    def get_channel_id(self, channel_name: str) -> str | None:
        return self._existing_channels.get(channel_name)

    def cache_channel_ids(self, mapping: dict) -> None:
        self._existing_channels.update(mapping)
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/unit/test_run_start_announcement.py
"""_announce_run_start: fresh runs announce to the configured channels, once,
with the hub's voice, and nothing about the announcement can break a startup.
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.agent import Agent
from src.agent.run_marker import RUN_START_MARKER_PREFIX
from src.agent.simulation import SimulationEngine
from src.models import SimulationRun
from tests.fakes import FakeSlackClient

pytestmark = pytest.mark.asyncio


def _engine(monkeypatch, tmp_path, *, with_hub=True, channels="general,assessments-summary"):
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    # Stub the ONE setting the method reads. Never touch the real
    # (lru_cached) get_settings from a test: clearing the cache leaks a
    # test-built Settings into every later test in the session.
    from types import SimpleNamespace
    monkeypatch.setattr(
        "src.agent.simulation.get_settings",
        lambda: SimpleNamespace(run_start_announce_channels=channels),
    )

    agents, clients = [], {}
    if with_hub:
        hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
        agents.append(hub)
        clients["blackbird"] = FakeSlackClient(agent_id="blackbird")
    lab = Agent("wang", "WangBot", "Wang", role="pi_lab")
    agents.append(lab)
    clients["wang"] = FakeSlackClient(agent_id="wang")
    eng = SimulationEngine(agents=agents, slack_clients=clients, fresh_start=True)
    eng._channel_id_map.update({
        "general": "C-GENERAL", "assessments-summary": "C-SUMMARY",
    })
    return eng, clients


async def test_posts_the_marker_to_every_configured_channel(monkeypatch, tmp_path):
    eng, clients = _engine(monkeypatch, tmp_path)

    await eng._announce_run_start()

    hub = clients["blackbird"]
    for ch_id in ("C-GENERAL", "C-SUMMARY"):
        texts = hub.posted_messages.get(ch_id, [])
        assert len(texts) == 1
        assert texts[0].startswith(RUN_START_MARKER_PREFIX)
    assert not clients["wang"].posted  # the hub speaks, not a lab


async def test_falls_back_to_another_client_without_a_hub(monkeypatch, tmp_path, caplog):
    eng, clients = _engine(monkeypatch, tmp_path, with_hub=False)

    await eng._announce_run_start()

    assert len(clients["wang"].posted_messages.get("C-GENERAL", [])) == 1
    assert any("falling back" in r.getMessage().lower() for r in caplog.records)


async def test_unresolvable_and_local_channels_are_skipped(monkeypatch, tmp_path, caplog):
    eng, clients = _engine(
        monkeypatch, tmp_path, channels="general,assessments-summary,no-such-channel",
    )
    eng._channel_id_map["assessments-summary"] = "local:assessments-summary"

    await eng._announce_run_start()

    hub = clients["blackbird"]
    assert len(hub.posted_messages.get("C-GENERAL", [])) == 1
    assert "local:assessments-summary" not in hub.posted_messages
    assert not any(k.startswith("local:") for k in hub.posted_messages)


async def test_a_refused_post_is_tolerated(monkeypatch, tmp_path):
    eng, clients = _engine(monkeypatch, tmp_path)
    monkeypatch.setattr(clients["blackbird"], "post_message", lambda *a, **k: None)

    await eng._announce_run_start()  # must not raise


async def test_empty_setting_disables_the_announcement(monkeypatch, tmp_path):
    eng, clients = _engine(monkeypatch, tmp_path, channels="")

    await eng._announce_run_start()

    assert not clients["blackbird"].posted


async def test_no_connected_client_is_a_quiet_skip(monkeypatch, tmp_path):
    eng, clients = _engine(monkeypatch, tmp_path)
    for c in clients.values():
        monkeypatch.setattr(type(c), "is_connected", property(lambda self: False))

    await eng._announce_run_start()  # must not raise; nothing posted

    assert not clients["blackbird"].posted


async def test_the_run_row_records_the_announcement(monkeypatch, tmp_path, engine):
    eng, clients = _engine(monkeypatch, tmp_path)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        run = SimulationRun(status="running", config={"seed": True})
        db.add(run)
        await db.commit()
        run_id = run.id
    eng.session_factory = factory
    eng.simulation_run_id = run_id

    await eng._announce_run_start()

    try:
        async with factory() as db:
            row = (await db.execute(
                select(SimulationRun).where(SimulationRun.id == run_id)
            )).scalar_one()
        rec = row.config["run_start_announcement"]
        assert rec["posted"].keys() == {"general", "assessments-summary"}
        assert rec["failed"] == []
        assert rec["text"].startswith(RUN_START_MARKER_PREFIX)
        assert row.config["seed"] is True  # reassignment preserved existing keys
    finally:
        # The factory commits for real (this is the method's own session
        # path, not the rolled-back db_session fixture); don't leave a run
        # row in the shared session DB — main.py's resume path orders by
        # started_at DESC, so a leaked row could poison a future test.
        async with factory() as db:
            leftover = (await db.execute(
                select(SimulationRun).where(SimulationRun.id == run_id)
            )).scalar_one_or_none()
            if leftover is not None:
                await db.delete(leftover)
                await db.commit()


async def test_start_announces_only_fresh_runs_after_validation(monkeypatch, tmp_path):
    """Positional pin: announce fires between _validate_star_topology and
    _run_main_loop, and only when fresh_start=True."""
    calls: list[str] = []

    def _rec(name, result=None):
        async def _async(*a, **k):
            calls.append(name)
            return result
        def _sync(*a, **k):
            calls.append(name)
            return result
        return _async, _sync

    for fresh, expected in ((True, 1), (False, 0)):
        calls.clear()
        eng, _ = _engine(monkeypatch, tmp_path)
        eng._fresh_start = fresh
        for name in (
            "_persist_seeded_channels", "_sync_private_channels_from_db",
            "_rebuild_state_from_db", "_restore_slack_state",
            "_rebuild_agent_state", "_rehydrate_assessed_threads",
            "_recompute_allowed_sender_ids", "_record_topology_snapshot",
            "_announce_run_start", "_run_main_loop",
        ):
            monkeypatch.setattr(eng, name, _rec(name)[0])
        for name in (
            "_ensure_seeded_channels", "_ensure_assessments_summary_channel",
            "_rewind_cursors_for_private_channels", "refresh_lab_directories",
        ):
            monkeypatch.setattr(eng, name, _rec(name)[1])
        monkeypatch.setattr(eng, "_validate_star_topology", _rec("_validate", [])[1])

        try:
            await eng.start()
        finally:
            # start() installs a process-global LLM-log callback (:849,
            # src/services/llm.py) pointing at this dead test engine —
            # clear it so no later test's LLM fake calls into it.
            from src.agent.simulation import set_call_log_callback
            set_call_log_callback(None)

        assert calls.count("_announce_run_start") == expected
        if fresh:
            assert calls.index("_validate") < calls.index("_announce_run_start")
            assert calls.index("_announce_run_start") < calls.index("_run_main_loop")
```

- [ ] **Step 3: Run tests to verify they fail**

ON-HOST: `.venv-test/bin/python -m pytest tests/unit/test_run_start_announcement.py -v`
Expected: FAIL — `AttributeError: 'SimulationEngine' object has no attribute '_announce_run_start'`
(the DB-backed test spins the testcontainers Postgres; that is normal)

- [ ] **Step 4: Implement the engine methods and call site**

Add to `src/agent/simulation.py`'s import block:

Widen the Task-4 import line to
`from src.agent.run_marker import is_run_start_marker, parse_announce_channels, render_run_start_announcement`,
widen the existing line 29 to
`from src.agent.roles import load_role, prompt_set_stamp`, and add:

```python
from src.services.build_info import get_build_info
```

New methods (place after `_mark_summary_posted`, i.e. after current line 3754):

```python
    def _run_start_announcement_values(self) -> dict[str, str]:
        """The 12 template placeholders (run_marker.ANNOUNCEMENT_VALUE_KEYS).

        Every value is a plain pre-rendered string so an operator template
        needs no format specs. The git identity describes the IMAGE this
        process runs from (see src/services/build_info.py) — for the agent
        that is exactly the code executing, since src/ is baked at build.
        """
        started = self._start_time or datetime.now(UTC)
        build = get_build_info()
        hub_stamp = prompt_set_stamp("scout_hub")
        pi_stamp = prompt_set_stamp("pi_lab")
        if build.dirty_files is None:
            dirty = "dirty state unknown"
        elif build.dirty_files == 0:
            dirty = "clean"
        else:
            dirty = f"{build.dirty_files} uncommitted change(s) at image build"
        return {
            "run_id": str(self.simulation_run_id) if self.simulation_run_id
            else "unrecorded (--no-db)",
            "started_at": started.strftime("%Y-%m-%d %H:%M UTC"),
            "run_duration": (
                f"{self.max_runtime_minutes} minutes"
                if self.max_runtime_minutes > 0 else "indefinite (until stopped)"
            ),
            "git_commit": build.commit[:7] if build.commit else "unknown",
            "git_branch": build.branch or "unknown",
            "git_dirty": dirty,
            "hub_prompts_version": hub_stamp.version,
            "hub_prompts_hash": hub_stamp.content_hash,
            "pi_prompts_version": pi_stamp.version,
            "pi_prompts_hash": pi_stamp.content_hash,
            "rubric_version": RUBRIC_VERSION,
            "rubric_hash": RUBRIC_CONTENT_HASH,
        }

    async def _announce_run_start(self) -> None:
        """Post the run-start marker to every configured channel (fresh runs
        only — the caller gates on self._fresh_start).

        Best-effort end to end, same philosophy as _post_assessment_summary:
        nothing here may take down a run start. Refusals arrive as a falsy
        return from post_message, not as exceptions, so the return value is
        checked per channel. Posts with the hub's client (the engine's voice,
        and the identity with zero blast radius if the ingest sentinel ever
        regressed — see run_marker.py); falls back to any connected client
        with a WARNING. The markers post AFTER the fresh-start cursor seed,
        so the live poller WILL fetch them on its first tick — the sentinel
        skip (Task 4) is what drops them there and on every later resume.
        """
        try:
            names = parse_announce_channels(
                get_settings().run_start_announce_channels
            )
            if not names:
                logger.info("Run-start announcement disabled (no channels configured)")
                return

            hub = next(
                (a for a in self.agents.values() if a.role == "scout_hub"), None
            )
            client = self.slack_clients.get(hub.agent_id) if hub else None
            if not client or not client.is_connected:
                fallback = self._next_poll_client()
                if fallback is None:
                    logger.info(
                        "Run-start announcement skipped: no connected Slack "
                        "client (Slack off or all tokens dead)"
                    )
                    return
                logger.warning(
                    "Run-start announcement: hub client unavailable — "
                    "falling back to [%s]'s client (marker will carry a "
                    "lab bot's identity)", fallback.agent_id,
                )
                client = fallback

            text = render_run_start_announcement(
                self._run_start_announcement_values()
            )
            posted: dict[str, str] = {}
            failed: list[str] = []
            for name in names:
                ch_id = self._channel_id_map.get(name)
                if not ch_id:
                    ch_id = await asyncio.to_thread(client.get_channel_id, name)
                if not ch_id or ch_id.startswith("local:"):
                    logger.warning(
                        "Run-start announcement: cannot resolve #%s to a real "
                        "Slack channel (got %r) — skipping it", name, ch_id,
                    )
                    failed.append(name)
                    continue
                try:
                    result = await client.apost_message(ch_id, text)
                except Exception:  # noqa: BLE001 — per-channel isolation
                    logger.warning(
                        "Run-start announcement to #%s (%s) raised — skipping",
                        name, ch_id, exc_info=True,
                    )
                    failed.append(name)
                    continue
                ts = (result or {}).get("ts")
                if ts:
                    posted[name] = ts
                else:
                    logger.warning(
                        "Run-start announcement to #%s (%s) was refused by "
                        "Slack — nothing posted there", name, ch_id,
                    )
                    failed.append(name)
            logger.info(
                "Run-start announcement: posted to %d channel(s)%s — %s",
                len(posted),
                f", {len(failed)} failed ({', '.join(failed)})" if failed else "",
                ", ".join(posted) or "none",
            )
            await self._record_run_start_announcement(text, posted, failed)
        except Exception:
            logger.exception("Run-start announcement failed — continuing startup")

    async def _record_run_start_announcement(
        self, text: str, posted: dict[str, str], failed: list[str],
    ) -> None:
        """Durably record what was announced on the run row's config.

        Reassigns the whole dict rather than mutating: SimulationRun.config is
        a plain JSON column (src/models/agent_activity.py:66) with no mutation
        tracking, so an in-place update would silently not persist. Best-effort
        and never raises — the Slack posts already happened.
        """
        if not self.session_factory or not self.simulation_run_id:
            return
        from sqlalchemy import select as sa_select
        try:
            async with self.session_factory() as db:
                run = (await db.execute(
                    sa_select(SimulationRun).where(
                        SimulationRun.id == self.simulation_run_id
                    )
                )).scalar_one_or_none()
                if run is None:
                    return
                run.config = {
                    **(run.config or {}),
                    "run_start_announcement": {
                        "at": datetime.now(UTC).isoformat(),
                        "text": text,
                        "posted": posted,
                        "failed": failed,
                    },
                }
                await db.commit()
        except Exception as exc:  # noqa: BLE001 — record is advisory
            logger.warning(
                "Could not record the run-start announcement on run %s: %s",
                self.simulation_run_id, exc,
            )
```

`SimulationRun` is already imported in simulation.py's model import block
(`from src.models import (...)` at line 44) — verify, and add it there if not.

In `start()`, immediately after `await self._record_topology_snapshot()`
(currently line 875) and before `await self._run_main_loop()`:

```python
        # Announce the run boundary in Slack — fresh runs only (a resume is
        # not a new experiment), and only after validation so a run that
        # fails startup is never announced. Best-effort: see
        # _announce_run_start.
        if self._fresh_start:
            await self._announce_run_start()
```

- [ ] **Step 5: Run tests to verify they pass**

ON-HOST: `.venv-test/bin/python -m pytest tests/unit/test_run_start_announcement.py tests/unit/test_run_marker_ingest_skip.py tests/unit/test_assessments_summary_post.py -v`
Expected: all pass.

- [ ] **Step 6: Ruff and commit**

ON-HOST: `.venv-test/bin/python -m ruff check src/agent/simulation.py tests/unit/test_run_start_announcement.py tests/fakes.py`

ON-HOST:
```bash
git add src/agent/simulation.py tests/unit/test_run_start_announcement.py tests/fakes.py
git commit -m "feat(announce): engine posts run-start markers to configured channels on --fresh

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Dockerfile capture, docs, full gate

**Files:**
- Modify: `Dockerfile`
- Modify: `CLAUDE.md`
- Verify: full `./scripts/ci.sh`

- [ ] **Step 1: Add the build-info layer to the Dockerfile**

After the existing `COPY . .` line:

```dockerfile
# Bake the git identity of THIS build into .build_info.json — the runtime has
# no git binary, and the announced "commit/branch/dirty" must describe the
# image's code, which is what actually runs (src/ is baked, not mounted).
# git is installed and purged inside one layer; the safe.directory entry
# covers builders whose COPY'd files change apparent ownership.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && git config --global --add safe.directory /app \
    && python scripts/write_build_info.py \
    && apt-get purge -y git \
    && rm -rf /var/lib/apt/lists/*
```

- [ ] **Step 2: Verify the image builds and carries the identity**

ON-HOST:
```bash
docker compose -f docker-compose.prod.yml build blackbird-app
docker compose -f docker-compose.prod.yml run --rm --no-deps blackbird-app cat /app/.build_info.json
```
Expected: JSON with the current commit, `"branch": "blackbird"`, and a small
`dirty_files` count (1 is the steady-state: the permanent uncommitted
`docker-compose.prod.yml` edit). Do NOT `up`/restart anything — this is a
build-only verification; the running site keeps its old containers.

- [ ] **Step 3: Add the operator section to CLAUDE.md**

Insert after the `--budget` paragraph in "Running the Agent Simulation" (i.e.
before the "As of 2026-08-22 every one of those numbers…" box):

```markdown
**Fresh runs announce themselves in Slack.** On `--fresh` (and only then), the
engine posts one `:checkered_flag: NEW EXPERIMENTAL RUN` marker per channel in
`RUN_START_ANNOUNCE_CHANNELS` (`src/config.py`, default: the six seeded
channels + `assessments-summary`; empty disables) right before the main loop —
after star-topology validation, so a run that fails startup is never
announced. The body is `prompts/run_start_announcement.md` (bind-mounted:
editable without a rebuild; the sentinel first line is prepended by code and
is NOT editable), carrying start time, planned duration, the image's git
commit/branch/dirty count (`.build_info.json`, baked by the Dockerfile —
`unknown`s mean a pre-feature image), the hub/PI prompt-set versions
(`version` keys in the two `prompts/roles/*/role.toml`, which must be bumped
on any prompt-set edit) and the rubric version. Both Slack-ingest paths drop
sentinel-prefixed messages, so markers never enter `agent_messages` — do not
reuse that prefix for anything else, and do not change it (old markers would
start re-ingesting on the next resume; see `src/agent/run_marker.py`). What
was announced is recorded under `run_start_announcement` in
`simulation_runs.config`.
```

- [ ] **Step 4: Run the doc-sync check and the CLAUDE.md drift alarm**

ON-HOST:
```bash
.venv-test/bin/python scripts/sync_prompt_set_docs.py --check
.venv-test/bin/python -m pytest tests/unit/test_claude_md_disclosure_sync.py tests/unit/test_doc_prompt_sync.py -v
```
Expected: no drift (this plan edits no `.md` prompt file and no `_PI_LAB`/`_SCOUT_HUB` string), all pass.

- [ ] **Step 5: Full gate**

ON-HOST: `./scripts/ci.sh`
Expected: alembic sanity, ruff (test suite zero findings, src within ratchet), full pytest with the branch-coverage floor — all green.

- [ ] **Step 6: Commit**

ON-HOST:
```bash
git add Dockerfile CLAUDE.md docs/plans/2026-08-29-run-start-announcements.md
git commit -m "feat(announce): bake git identity at image build; document run-start announcements

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Deploy notes (operator, not part of implementation)

- **No migration.** Nothing touches the schema (`run_start_announcement` lives inside the existing `simulation_runs.config` JSON).
- **All three images must be rebuilt** (`$DC build blackbird-app worker` + `$DC --profile agent build agent`): the engine change lives in the agent image, and the `.build_info.json` layer only exists in images built after this lands. An old agent image simply never announces; a new one on a pre-feature build announces `unknown` git fields — both are safe.
- The announcement fires on the **next `--fresh` run only**. Resuming the current run (`61ccad6d`) posts nothing.
- To change recipients: edit `RUN_START_ANNOUNCE_CHANNELS` in `.env` (the agent container is created per run, so the next run picks it up — no rebuild). To change wording: edit `prompts/run_start_announcement.md` (bind-mounted — no rebuild). Both take effect at the next run start, not mid-run.
- Per the standing preference, nothing in this plan starts, stops, or restarts the simulation.
