# Per-Role Agent Customization (Hub Bot) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give agents an assignable `role` that selects per-role prompt overrides and a per-role tool allow-list, and ship a `scout_hub` role (innovation-scouting persona + a US-patent prior-art search tool) without changing how any existing `pi_lab` agent behaves.

**Architecture:** A new dependency-free `src/agent/roles.py` resolves prompt file paths (`prompts/roles/{role}/{file}` → `prompts/{file}`) and loads a tiny per-role manifest (`role.toml`). `Agent` gains a `role` field; its three duplicated system-prompt builders collapse into one composer that reads the identity block from a file (`prompts/identity.md`) instead of a hardcoded Python string. Phase-4 tool dispatch filters the tool list by the agent's role and refuses out-of-role tools in the executor. `AgentRegistry.role` (migration `0024`, default `pi_lab`) carries the assignment and is picked up live by the roster sync. A new `src/services/patents.py` wraps PatentsView (USPTO), exposed as the `search_prior_art` tool available only to `scout_hub`.

**Tech Stack:** Python 3.11 (async), SQLAlchemy 2.0 + Alembic, Pydantic Settings, `httpx`, `tomllib` (stdlib), Anthropic tool-use API, pytest + pytest-asyncio, testcontainers (Postgres) for the integration tier.

## Global Constraints

- **This plan assumes `origin/cohort-db-conversations` has already been fast-forward-merged into `blackbird`** (see `docs/blackbird-star-topology-runbook.md` Phase 1). Alembic head is `0023`; this plan adds `0024`.
- **`pi_lab` output must be byte-identical to pre-change.** The default role is the *absence* of overrides; `prompts/roles/pi_lab/` must not exist. Pinned by a regression test (Task 3).
- **Migration convention:** revision id = `max(existing) + 1`, assigned at merge time. If the branch/`main` adds a migration before this lands, renumber `0024`. Downgrades use `if_exists`. (Branch rule, `specs/cohort-system-v2.md` §4.2 / §14.4; `scripts/ci.sh` enforces the chain.)
- **Tools never raise into a turn.** Every tool path returns a `str`, matching `execute_tool`'s existing catch-all (`src/agent/tools.py:134`).
- **`DEFAULT_TOOLS` is an explicit set of the current four tools**, never "everything in `TOOL_DEFINITIONS`" — so a newly added tool is opt-in, not handed to every agent.
- **The identity render must use `str.replace`, not `str.format`** — a stray `{` in a profile/role file must not raise `KeyError` mid-turn. (This matches the existing phase-prompt builders, which already substitute via `.replace`, not `.format` — see `agent.py:632-635`.)
- **Prior-art tool output must carry the US-only caveat verbatim** (Task 7) and be asserted by a test (Task 8).
- **Secret naming:** the config field is `patentsview_api_key` — the substring `key` triggers existing `repr()` redaction (`src/config.py` `_SECRET_NAME_HINTS`). Do not rename it to something without `key`/`token`/`secret`.
- **Run tests inside the container** per `CLAUDE.md`: `docker compose exec app python -m pytest ...` (or the app image directly). Unit tiers that need no DB can run with `PYTHONPATH=/app`.

---

### Task 1: `roles.py` — prompt-path resolution

**Files:**
- Create: `src/agent/roles.py`
- Test: `tests/unit/test_roles.py`

**Interfaces:**
- Consumes: nothing (dependency-free by design — no `src.models`, no DB).
- Produces:
  - `ROLES_DIR: Path` = `Path("prompts/roles")`
  - `PROMPTS_DIR: Path` = `Path("prompts")`
  - `DEFAULT_ROLE: str` = `"pi_lab"`
  - `resolve_prompt_path(role: str, filename: str) -> Path` — returns `ROLES_DIR/role/filename` if that file exists, else `PROMPTS_DIR/filename`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_roles.py
from pathlib import Path

from src.agent import roles


def test_resolve_falls_back_to_global_when_no_role_override(tmp_path, monkeypatch):
    monkeypatch.setattr(roles, "PROMPTS_DIR", tmp_path)
    monkeypatch.setattr(roles, "ROLES_DIR", tmp_path / "roles")
    (tmp_path / "agent-system.md").write_text("GLOBAL", encoding="utf-8")

    p = roles.resolve_prompt_path("scout_hub", "agent-system.md")

    assert p == tmp_path / "agent-system.md"
    assert p.read_text(encoding="utf-8") == "GLOBAL"


def test_resolve_prefers_role_override_when_present(tmp_path, monkeypatch):
    monkeypatch.setattr(roles, "PROMPTS_DIR", tmp_path)
    monkeypatch.setattr(roles, "ROLES_DIR", tmp_path / "roles")
    (tmp_path / "agent-system.md").write_text("GLOBAL", encoding="utf-8")
    role_dir = tmp_path / "roles" / "scout_hub"
    role_dir.mkdir(parents=True)
    (role_dir / "agent-system.md").write_text("HUB", encoding="utf-8")

    p = roles.resolve_prompt_path("scout_hub", "agent-system.md")

    assert p == role_dir / "agent-system.md"
    assert p.read_text(encoding="utf-8") == "HUB"


def test_pi_lab_resolves_to_global_even_if_role_dir_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(roles, "PROMPTS_DIR", tmp_path)
    monkeypatch.setattr(roles, "ROLES_DIR", tmp_path / "roles")
    (tmp_path / "phase5-new-post.md").write_text("DEFAULT", encoding="utf-8")

    p = roles.resolve_prompt_path("pi_lab", "phase5-new-post.md")

    assert p == tmp_path / "phase5-new-post.md"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec app python -m pytest tests/unit/test_roles.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.agent.roles'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/agent/roles.py
"""Per-role agent customization: prompt-path resolution and role manifests.

Dependency-free on purpose (no src.models, no DB) so the resolution rules are
unit-testable without a database, and so src/agent/agent.py can import it
without pulling the ORM into the Agent class. See
docs/specs/2026-08-05-hub-bot-customization-design.md.
"""

from __future__ import annotations

from pathlib import Path

PROMPTS_DIR = Path("prompts")
ROLES_DIR = PROMPTS_DIR / "roles"
DEFAULT_ROLE = "pi_lab"


def resolve_prompt_path(role: str, filename: str) -> Path:
    """Return the role's override for ``filename`` if present, else the global file.

    ``pi_lab`` is the absence of overrides: ``prompts/roles/pi_lab/`` need never
    exist, and falling through to ``prompts/{filename}`` *is* pi_lab. That is what
    keeps existing agents byte-identical after this change lands.
    """
    override = ROLES_DIR / role / filename
    if override.is_file():
        return override
    return PROMPTS_DIR / filename
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose exec app python -m pytest tests/unit/test_roles.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/agent/roles.py tests/unit/test_roles.py
git commit -m "feat(roles): prompt-path resolution with per-role fallback"
```

---

### Task 2: `roles.py` — role manifest loading

**Files:**
- Modify: `src/agent/roles.py`
- Test: `tests/unit/test_roles.py`

**Interfaces:**
- Consumes: `TOOL_DEFINITIONS` from `src/agent/tools.py` (read only, to validate tool names). Import it lazily inside `load_role` to keep module import cheap and avoid an import cycle.
- Produces:
  - `DEFAULT_TOOLS: frozenset[str]` = `{"retrieve_profile", "retrieve_abstract", "retrieve_full_text", "retrieve_foa"}`
  - `@dataclass(frozen=True) class RoleSpec: name: str; label: str; tools: frozenset[str]`
  - `load_role(name: str) -> RoleSpec` — reads `ROLES_DIR/name/role.toml`; missing file → defaults; malformed TOML → log ERROR + defaults; tool names not in `TOOL_DEFINITIONS` → log + drop; never raises.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_roles.py
import logging

from src.agent.roles import DEFAULT_TOOLS, RoleSpec, load_role


def _write_role(tmp_path, monkeypatch, name, toml_text):
    monkeypatch.setattr(roles, "ROLES_DIR", tmp_path / "roles")
    d = tmp_path / "roles" / name
    d.mkdir(parents=True)
    (d / "role.toml").write_text(toml_text, encoding="utf-8")


def test_missing_manifest_yields_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(roles, "ROLES_DIR", tmp_path / "roles")
    spec = load_role("pi_lab")
    assert spec == RoleSpec(name="pi_lab", label="pi_lab", tools=DEFAULT_TOOLS)


def test_manifest_sets_label_and_tool_allow_list(tmp_path, monkeypatch):
    _write_role(
        tmp_path, monkeypatch, "scout_hub",
        'label = "Scout Hub"\n'
        'tools = ["retrieve_profile", "search_prior_art"]\n',
    )
    # search_prior_art must exist in TOOL_DEFINITIONS by the time this runs
    # (Task 7). Until then this asserts only the known tool survives.
    spec = load_role("scout_hub")
    assert spec.name == "scout_hub"
    assert spec.label == "Scout Hub"
    assert "retrieve_profile" in spec.tools


def test_unknown_tool_is_dropped_and_logged(tmp_path, monkeypatch, caplog):
    _write_role(
        tmp_path, monkeypatch, "weird",
        'tools = ["retrieve_profile", "does_not_exist"]\n',
    )
    with caplog.at_level(logging.WARNING):
        spec = load_role("weird")
    assert "does_not_exist" not in spec.tools
    assert "retrieve_profile" in spec.tools
    assert any("does_not_exist" in r.message for r in caplog.records)


def test_malformed_toml_falls_back_to_defaults(tmp_path, monkeypatch, caplog):
    _write_role(tmp_path, monkeypatch, "broken", "tools = [not valid toml")
    with caplog.at_level(logging.ERROR):
        spec = load_role("broken")
    assert spec.tools == DEFAULT_TOOLS
    assert spec.label == "broken"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec app python -m pytest tests/unit/test_roles.py -v`
Expected: FAIL — `ImportError: cannot import name 'DEFAULT_TOOLS'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/agent/roles.py`:

```python
import logging
import tomllib
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Explicit, NOT "every tool in TOOL_DEFINITIONS": if the default were "all tools",
# adding a new tool to that list would silently hand it to every agent. Explicit
# default keeps every new tool opt-in. See design §4.1.
DEFAULT_TOOLS: frozenset[str] = frozenset(
    {"retrieve_profile", "retrieve_abstract", "retrieve_full_text", "retrieve_foa"}
)


@dataclass(frozen=True)
class RoleSpec:
    name: str
    label: str
    tools: frozenset[str]


def _known_tool_names() -> set[str]:
    # Lazy import: avoids an import cycle (tools.py imports nothing from roles,
    # but keeping this lazy documents that roles.py must stay import-light).
    from src.agent.tools import TOOL_DEFINITIONS

    return {t["name"] for t in TOOL_DEFINITIONS}


def load_role(name: str) -> RoleSpec:
    """Load a role manifest. Never raises: a bad manifest degrades to defaults.

    - no role.toml            -> DEFAULT_TOOLS, label == name
    - malformed TOML          -> log ERROR, DEFAULT_TOOLS, label == name
    - tool not in the codebase -> log WARNING, drop it
    """
    manifest = ROLES_DIR / name / "role.toml"
    if not manifest.is_file():
        return RoleSpec(name=name, label=name, tools=DEFAULT_TOOLS)
    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        logger.error("[roles] %s: malformed role.toml (%s) — using defaults", name, exc)
        return RoleSpec(name=name, label=name, tools=DEFAULT_TOOLS)

    label = str(data.get("label", name))
    declared = data.get("tools")
    if declared is None:
        tools = DEFAULT_TOOLS
    else:
        known = _known_tool_names()
        kept = set()
        for t in declared:
            if t in known:
                kept.add(t)
            else:
                logger.warning("[roles] %s: unknown tool %r in role.toml — dropped", name, t)
        tools = frozenset(kept)
    return RoleSpec(name=name, label=label, tools=tools)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose exec app python -m pytest tests/unit/test_roles.py -v`
Expected: PASS. (`test_manifest_sets_label_and_tool_allow_list` passes because it only asserts the *known* tool survives; `search_prior_art` becomes known in Task 7.)

- [ ] **Step 5: Commit**

```bash
git add src/agent/roles.py tests/unit/test_roles.py
git commit -m "feat(roles): role.toml manifest with tool allow-list and safe fallbacks"
```

---

### Task 3: Extract identity to a file + collapse the three prompt builders

**Files:**
- Create: `prompts/identity.md`
- Modify: `src/agent/agent.py` (`Agent.__init__`; `build_system_prompt`, `build_scan_system_prompt`, `build_thread_reply_system_prompt`; add `_load_prompt`, `_compose_system_prompt`, `_render_identity`)
- Test: `tests/unit/test_agent_prompts.py`

**Interfaces:**
- Consumes: `resolve_prompt_path`, `DEFAULT_ROLE` from `src/agent/roles.py` (Task 1).
- Produces:
  - `Agent.__init__(self, agent_id, bot_name, pi_name, role: str = DEFAULT_ROLE)` — new trailing keyword param, defaulted so every existing call site is unaffected.
  - `Agent.role: str` attribute.
  - `Agent._load_prompt(self, filename: str, default: str) -> str` — `_load_file(resolve_prompt_path(self.role, filename), default)`.
  - Public builder signatures unchanged: `build_system_prompt(visibility, channel_id)`, `build_scan_system_prompt()`, `build_thread_reply_system_prompt(visibility, channel_id)`.

**Context for the implementer:** the identity block is currently this exact string, duplicated at `src/agent/agent.py:203`, `:229`, `:263`:

```
## Your Identity
You are **{bot_name}**, the AI agent representing the {pi_name} lab at Scripps Research.
Your agent ID is "{agent_id}". When communicating, represent your lab professionally.
```

`prompts/identity.md` must hold exactly that text with `{bot_name}`/`{pi_name}`/`{agent_id}` placeholders, so `pi_lab` output is byte-identical.

- [ ] **Step 1: Write the failing test (byte-identical regression guard)**

```python
# tests/unit/test_agent_prompts.py
from src.agent.agent import Agent
from src.agent.roles import DEFAULT_ROLE


def _agent():
    return Agent(agent_id="su", bot_name="SuBot", pi_name="Andrew Su")


def test_default_role_is_pi_lab():
    assert _agent().role == DEFAULT_ROLE


def test_identity_block_is_present_and_substituted():
    prompt = _agent().build_scan_system_prompt()
    assert "You are **SuBot**" in prompt
    assert 'the Andrew Su lab at Scripps Research' in prompt
    assert 'agent ID is "su"' in prompt


def test_curly_brace_in_profile_does_not_crash(tmp_path, monkeypatch):
    # A profile containing a bare "{" must not raise (str.replace, not str.format).
    a = _agent()
    monkeypatch.setattr(type(a), "public_profile", property(lambda self: "budget is {tight}"))
    prompt = a.build_scan_system_prompt()  # must not raise
    assert "budget is {tight}" in prompt


def test_scan_prompt_omits_memory_and_lab_directory():
    a = _agent()
    a._lab_directory = "### Other Lab\n- paper"
    scan = a.build_scan_system_prompt()
    assert "Other Lab" not in scan  # scan prompt excludes the directory
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec app python -m pytest tests/unit/test_agent_prompts.py -v`
Expected: FAIL — `test_default_role_is_pi_lab` errors (`Agent.__init__` has no `role`), curly-brace test may error on current `.format`-free code only after refactor.

- [ ] **Step 3: Write the implementation**

In `Agent.__init__`, add the parameter and attribute:

```python
    def __init__(self, agent_id: str, bot_name: str, pi_name: str,
                 role: str = DEFAULT_ROLE):
        self.agent_id = agent_id
        self.bot_name = bot_name
        self.pi_name = pi_name
        self.role = role
        # ... existing attribute initialisers unchanged ...
```

Add the import at the top of `agent.py`:

```python
from src.agent.roles import DEFAULT_ROLE, resolve_prompt_path
```

Add helper methods on `Agent`:

```python
    def _load_prompt(self, filename: str, default: str) -> str:
        """Load a prompt file honouring this agent's role override."""
        return self._load_file(resolve_prompt_path(self.role, filename), default)

    def _render_identity(self) -> str:
        template = self._load_prompt("identity.md", _DEFAULT_IDENTITY)
        # str.replace, NOT str.format: profiles/role files may contain bare braces.
        return (
            template.replace("{bot_name}", self.bot_name)
            .replace("{pi_name}", self.pi_name)
            .replace("{agent_id}", self.agent_id)
        )

    def _compose_system_prompt(
        self,
        *,
        include_memory: bool,
        include_lab_directory: bool,
        visibility: str = VISIBILITY_PUBLIC,
        channel_id: str | None = None,
    ) -> str:
        base_prompt = self._load_prompt("agent-system.md", _default_system_prompt())
        identity = self._render_identity()
        lab_directory_section = ""
        if include_lab_directory and self._lab_directory:
            lab_directory_section = (
                "\n## Other Labs' Recent Publications\n"
                "Use these to reference other labs' work in conversations. "
                "Include links when citing.\n"
                f"{self._lab_directory}\n"
            )
        memory_section = ""
        if include_memory:
            memory_section = (
                "\n## Your Working Memory\n"
                f"{self._compose_working_memory(visibility, channel_id)}"
            )
        private_rules = PRIVATE_CHANNEL_RULES if visibility == VISIBILITY_COLLAB_PRIVATE else ""
        return f"""{base_prompt}

{identity}

## Your Lab Profile (Public)
{self.public_profile}

## Your Private Instructions
{self.private_profile}{memory_section}{lab_directory_section}{private_rules}"""
```

Add a module-level `_DEFAULT_IDENTITY` constant (the fallback when `identity.md` is missing) matching the extracted text:

```python
_DEFAULT_IDENTITY = """## Your Identity
You are **{bot_name}**, the AI agent representing the {pi_name} lab at Scripps Research.
Your agent ID is "{agent_id}". When communicating, represent your lab professionally."""
```

Rewrite the three public builders as thin wrappers:

```python
    def build_system_prompt(self, visibility=VISIBILITY_PUBLIC, channel_id=None) -> str:
        return self._compose_system_prompt(
            include_memory=True, include_lab_directory=True,
            visibility=visibility, channel_id=channel_id,
        )

    def build_scan_system_prompt(self) -> str:
        return self._compose_system_prompt(
            include_memory=False, include_lab_directory=False,
        )

    def build_thread_reply_system_prompt(self, visibility=VISIBILITY_PUBLIC, channel_id=None) -> str:
        return self._compose_system_prompt(
            include_memory=True, include_lab_directory=False,
            visibility=visibility, channel_id=channel_id,
        )
```

Create `prompts/identity.md`:

```markdown
## Your Identity
You are **{bot_name}**, the AI agent representing the {pi_name} lab at Scripps Research.
Your agent ID is "{agent_id}". When communicating, represent your lab professionally.
```

**Verify byte-identity manually before committing.** The old builders assembled sections in a fixed order; `_compose_system_prompt` must reproduce it. Confirm the whitespace between sections matches the originals (`src/agent/agent.py:200-276` pre-change) — especially the blank line before `## Your Working Memory` and before the lab-directory block.

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose exec app python -m pytest tests/unit/test_agent_prompts.py tests/unit/test_roles.py -v`
Expected: PASS

Then run the existing golden-master snapshot to catch any prompt drift:

Run: `docker compose exec app python -m pytest tests/characterization -v`
Expected: PASS. **If a snapshot fails**, diff it — a change to `pi_lab` prompt bytes is a bug in this task, not a snapshot to bless. Only re-baseline if the diff is purely the mechanical section-order reproduction and you have confirmed it against the pre-change strings.

- [ ] **Step 5: Commit**

```bash
git add src/agent/agent.py prompts/identity.md tests/unit/test_agent_prompts.py
git commit -m "refactor(agent): role-aware prompt loading; collapse 3 builders into 1; extract identity to file"
```

---

### Task 4: Migration `0024` — `AgentRegistry.role`

**Files:**
- Create: `alembic/versions/0024_add_agent_role.py`
- Modify: `src/models/agent_registry.py` (add `role` mapped column)
- Test: `tests/unit/test_migration_checks.py` (add a round-trip assertion) OR rely on the branch's existing alembic round-trip gate in `scripts/ci.sh`.

**Interfaces:**
- Produces: `AgentRegistry.role: Mapped[str]` (default `"pi_lab"`); DB column `agents.role VARCHAR(20) NOT NULL DEFAULT 'pi_lab'`.

- [ ] **Step 1: Write the migration**

```python
# alembic/versions/0024_add_agent_role.py
"""Add role column to agents (per-role agent customization)

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-05 00:00:00.000000

`role` selects per-role prompt overrides (prompts/roles/{role}/) and a per-role
tool allow-list. Default 'pi_lab' == the pre-existing all-agents-identical
behaviour, so this column is a no-op until an agent is explicitly reassigned.
See docs/specs/2026-08-05-hub-bot-customization-design.md.

Downgrade is idempotent (if_exists) per the branch convention (0022/0023).
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("role", sa.String(length=20), nullable=False, server_default="pi_lab"),
    )


def downgrade() -> None:
    op.drop_column("agents", "role", if_exists=True)
```

- [ ] **Step 2: Add the model column**

In `src/models/agent_registry.py`, after the `status` column:

```python
    role: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="pi_lab", default="pi_lab"
    )  # selects per-role prompts + tool allow-list; 'pi_lab' == legacy behaviour
```

- [ ] **Step 3: Run the alembic round-trip**

Run: `docker compose exec app alembic upgrade head && docker compose exec app alembic downgrade -1 && docker compose exec app alembic upgrade head`
Expected: clean upgrade → downgrade → upgrade with no error; head reports `0024`.

Verify the column and default:

Run: `docker compose exec postgres psql -U copi -d copi -c "\d agents" | grep role`
Expected: `role | character varying(20) | not null | 'pi_lab'::character varying`

- [ ] **Step 4: Run the model/CI gate**

Run: `docker compose exec app python -m pytest tests/unit/test_migration_checks.py -v`
Expected: PASS (or the branch's `scripts/ci.sh` alembic-chain check if that is where the guard lives).

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/0024_add_agent_role.py src/models/agent_registry.py tests/unit/test_migration_checks.py
git commit -m "feat(db): add agents.role column (migration 0024)"
```

---

### Task 5: Thread `role` through the roster reads

**Files:**
- Modify: `src/agent/main.py` (roster `select`, `Agent(...)` construction)
- Modify: `src/agent/simulation.py` (`_sync_roster_from_db`: `select`, `Agent(...)` construction, and a new role-diff pass over surviving agents)
- Test: `tests/integration/test_state_rebuild.py` or `tests/integration/test_full_run_live.py` (role-diff live-flip test)

**Interfaces:**
- Consumes: `AgentRegistry.role` (Task 4); `Agent(role=...)` (Task 3).
- Produces: a live agent's `.role` equals its DB `agents.role`, updated within one roster-sync tick after a DB change.

**Context:** Both roster reads currently select `agent_id, bot_name, pi_name, slack_bot_token` and build `Agent(agent_id=..., bot_name=..., pi_name=...)`. `_sync_roster_from_db` computes `to_add`/`to_remove` and **returns early when both are empty** — so a role change on a surviving agent is otherwise invisible.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_role_live_flip.py  (or fold into test_full_run_live.py)
import pytest

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_role_change_is_picked_up_without_restart(engine_with_one_agent):
    """A DB role change on a running agent updates Agent.role on the next sync."""
    engine, session_factory, agent_id = engine_with_one_agent
    assert engine.agents[agent_id].role == "pi_lab"

    from sqlalchemy import update
    from src.models import AgentRegistry
    async with session_factory() as db:
        await db.execute(
            update(AgentRegistry).where(AgentRegistry.agent_id == agent_id).values(role="scout_hub")
        )
        await db.commit()

    engine._last_roster_poll = 0.0  # force the throttle open
    await engine._sync_roster_from_db()

    assert engine.agents[agent_id].role == "scout_hub"
```

(If no `engine_with_one_agent` fixture exists, build a minimal one in the test module that seeds a single active `AgentRegistry` row against the testcontainer Postgres and constructs `SimulationEngine` with that `session_factory`, mirroring `tests/integration/test_full_run_live.py` setup.)

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec app python -m pytest tests/integration/test_role_live_flip.py -v`
Expected: FAIL — role stays `"pi_lab"` (early return skips surviving agents), or `AttributeError` on `.role` if Task 3 not present.

- [ ] **Step 3: Implement**

In `src/agent/main.py`, add `_AR.role` to the select tuple and `role=r.role` to the `Agent(...)` list comprehension:

```python
            _stmt = _select(
                _AR.agent_id, _AR.bot_name, _AR.pi_name, _AR.slack_bot_token, _AR.role
            )
            ...
    agents = [
        Agent(agent_id=r.agent_id, bot_name=r.bot_name, pi_name=r.pi_name, role=r.role)
        for r in _rows
    ]
```

In `src/agent/simulation.py` `_sync_roster_from_db`, add `AgentRegistry.role` to the `sa_select`, pass `role=r.role` when constructing the added `Agent`, and add a role-diff pass that runs **before** the `if not to_remove and not to_add: return` early exit is allowed to skip it:

```python
            desired = {r.agent_id: r for r in rows}

            # Role-diff for surviving agents (agents present in both current and
            # desired). Must run even when to_add/to_remove are empty, or a role
            # reassignment on a running agent is invisible until the next add/remove.
            role_changed = False
            for aid, agent in self.agents.items():
                r = desired.get(aid)
                if r is not None and getattr(r, "role", "pi_lab") != agent.role:
                    logger.info("[roster] %s role %s -> %s", aid, agent.role, r.role)
                    agent.role = r.role
                    role_changed = True

            current = set(self.agents)
            to_remove = current - set(desired)
            to_add = set(desired) - current
            if not to_remove and not to_add:
                if role_changed:
                    # Persona/tooling changed but membership did not — refresh the
                    # derived structures a role can influence, then stop.
                    self._build_lab_directories()
                return
```

And in the add branch: `agent = Agent(agent_id=aid, bot_name=r.bot_name, pi_name=r.pi_name, role=r.role)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose exec app python -m pytest tests/integration/test_role_live_flip.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agent/main.py src/agent/simulation.py tests/integration/test_role_live_flip.py
git commit -m "feat(roster): thread role through roster reads; pick up role changes live"
```

---

### Task 6: Per-role tool gating in Phase 4

**Files:**
- Modify: `src/agent/tools.py` (`execute_tool` refuses out-of-role tools; add a `tools_for_role` helper)
- Modify: `src/agent/simulation.py` (Phase-4 call: pass the role-filtered tool list, and the role to the executor)
- Test: `tests/unit/test_tool_gating.py`

**Interfaces:**
- Consumes: `RoleSpec`/`load_role` (Task 2); `Agent.role` (Task 3).
- Produces:
  - `tools_for_role(role: str) -> list[dict]` in `src/agent/tools.py` — `TOOL_DEFINITIONS` filtered to `load_role(role).tools`.
  - `execute_tool(tool_name, tool_input, agent_id, thread_state=None, role="pi_lab")` — new trailing `role` param; returns a refusal string if `tool_name` not in `load_role(role).tools`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_tool_gating.py
import pytest

from src.agent.tools import execute_tool, tools_for_role


def test_pi_lab_tool_list_excludes_hub_only_tools():
    names = {t["name"] for t in tools_for_role("pi_lab")}
    assert "retrieve_profile" in names
    assert "search_prior_art" not in names  # true before Task 7; still true after


@pytest.mark.asyncio
async def test_executor_refuses_a_tool_not_in_the_role():
    # retrieve_foa is a pi_lab tool; ask a hypothetical role that lacks it.
    # Use a role dir that does not exist -> DEFAULT_TOOLS (has retrieve_foa),
    # so instead assert refusal via a role we can pin: monkeypatch load_role.
    from src.agent import tools as tools_mod
    from src.agent.roles import RoleSpec

    orig = tools_mod.load_role
    tools_mod.load_role = lambda name: RoleSpec(name=name, label=name, tools=frozenset({"retrieve_profile"}))
    try:
        out = await execute_tool("retrieve_foa", {"foa_number": "PA-24-1"}, "su", None, role="locked")
    finally:
        tools_mod.load_role = orig
    assert "not available" in out.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec app python -m pytest tests/unit/test_tool_gating.py -v`
Expected: FAIL — `ImportError: cannot import name 'tools_for_role'`

- [ ] **Step 3: Implement**

In `src/agent/tools.py`, add the import and helper, and the guard at the top of `execute_tool`:

```python
from src.agent.roles import load_role


def tools_for_role(role: str) -> list[dict[str, Any]]:
    allowed = load_role(role).tools
    return [t for t in TOOL_DEFINITIONS if t["name"] in allowed]


async def execute_tool(
    tool_name: str,
    tool_input: dict[str, Any],
    agent_id: str,
    thread_state: Any | None = None,
    role: str = "pi_lab",
) -> str:
    if tool_name not in load_role(role).tools:
        logger.warning("[tools] %s: role %r may not call %s", agent_id, role, tool_name)
        return f"Tool '{tool_name}' is not available to this agent."
    try:
        if tool_name == "retrieve_profile":
            ...  # unchanged body
```

In `src/agent/simulation.py` Phase 4 (around `simulation.py:851`), pass the role to both the executor and the tool list:

```python
        async def tool_executor(tool_name: str, tool_input: dict) -> str:
            return await execute_tool(
                tool_name, tool_input, agent.agent_id, thread, role=agent.role
            )

        ...
            response_text = await generate_with_tools(
                system_prompt=system_prompt,
                messages=messages,
                tools=tools_for_role(agent.role),   # was: TOOL_DEFINITIONS
                tool_executor=tool_executor,
                ...
```

Update the import at the top of `simulation.py`:

```python
from src.agent.tools import TOOL_DEFINITIONS, execute_tool, tools_for_role
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose exec app python -m pytest tests/unit/test_tool_gating.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agent/tools.py src/agent/simulation.py tests/unit/test_tool_gating.py
git commit -m "feat(tools): per-role tool allow-list, enforced in Phase 4 and the executor"
```

---

### Task 7: PatentsView prior-art service

**Files:**
- Create: `src/services/patents.py`
- Modify: `src/config.py` (add `patentsview_api_key: str = ""`)
- Test: `tests/unit/test_patents.py`

**Interfaces:**
- Produces: `async def search_prior_art(query: str, limit: int = 10) -> list[dict[str, Any]]` returning dicts with keys `patent_id`, `title`, `date`, `abstract`, `assignees`.
- Consumes: `settings.patentsview_api_key`.

**API facts (verified 2026-08-05):** endpoint `https://search.patentsview.org/api/v1/patent/`, GET or POST, key header `X-Api-Key`, 45 req/min (429 + `Retry-After` on overage). PatentsView query body is `{"q": {...}, "f": [...], "o": {"size": N}}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_patents.py
import pytest
import respx
import httpx

from src.services import patents


@pytest.mark.asyncio
@respx.mock
async def test_search_returns_normalised_hits(monkeypatch):
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    respx.get(patents.SEARCH_URL).mock(return_value=httpx.Response(
        200, json={"patents": [
            {"patent_id": "123", "patent_title": "Widget", "patent_date": "2020-01-01",
             "patent_abstract": "An abstract", "assignees": [{"assignee_organization": "Acme"}]},
        ]},
    ))
    hits = await patents.search_prior_art("widget")
    assert hits[0]["patent_id"] == "123"
    assert hits[0]["title"] == "Widget"
    assert hits[0]["assignees"] == ["Acme"]


@pytest.mark.asyncio
async def test_missing_key_returns_empty_and_does_not_call(monkeypatch):
    monkeypatch.setattr(patents, "_api_key", lambda: "")
    hits = await patents.search_prior_art("widget")
    assert hits == []


@pytest.mark.asyncio
@respx.mock
async def test_http_error_returns_empty(monkeypatch):
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    respx.get(patents.SEARCH_URL).mock(return_value=httpx.Response(500))
    assert await patents.search_prior_art("widget") == []
```

(If `respx` is not already a dev dependency, add it to `pyproject.toml`'s test extras in this task's commit; it is the httpx-native mock the repo's live/unit split favours.)

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec app python -m pytest tests/unit/test_patents.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.services.patents'`

- [ ] **Step 3: Implement**

```python
# src/services/patents.py
"""PatentsView (USPTO) prior-art search. US filings only — see the caveat in
src/agent/tools.py where results are surfaced. Mirrors src/services/grants.py.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from src.config import get_settings

logger = logging.getLogger(__name__)

SEARCH_URL = "https://search.patentsview.org/api/v1/patent/"


def _api_key() -> str:
    return get_settings().patentsview_api_key


async def search_prior_art(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search US patents by text. Returns [] on any failure (never raises)."""
    key = _api_key()
    if not key:
        logger.info("[patents] no PatentsView API key configured — skipping search")
        return []
    params = {
        "q": '{"_text_any":{"patent_title":"%s"}}' % query.replace('"', ""),
        "f": '["patent_id","patent_title","patent_date","patent_abstract","assignees.assignee_organization"]',
        "o": '{"size":%d}' % limit,
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(SEARCH_URL, params=params, headers={"X-Api-Key": key})
            if resp.status_code == 429:
                logger.warning("[patents] rate limited (429)")
                return []
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.error("[patents] search failed: %s", exc)
        return []
    hits = []
    for p in data.get("patents", []) or []:
        hits.append({
            "patent_id": p.get("patent_id", ""),
            "title": p.get("patent_title", ""),
            "date": p.get("patent_date", ""),
            "abstract": p.get("patent_abstract", ""),
            "assignees": [a.get("assignee_organization", "")
                          for a in (p.get("assignees") or []) if a.get("assignee_organization")],
        })
    return hits
```

Add to `src/config.py` in the NCBI/keys area:

```python
    # PatentsView (USPTO) prior-art search — hub-only tool. Name contains "key"
    # so it is auto-redacted in repr(settings). US filings only.
    patentsview_api_key: str = ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose exec app python -m pytest tests/unit/test_patents.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/services/patents.py src/config.py tests/unit/test_patents.py pyproject.toml
git commit -m "feat(patents): PatentsView prior-art search service"
```

---

### Task 8: `search_prior_art` tool + US-only caveat

**Files:**
- Modify: `src/agent/tools.py` (add to `TOOL_DEFINITIONS`; add `_execute_search_prior_art`; add the `elif` branch)
- Test: `tests/unit/test_tool_gating.py` (extend), `tests/unit/test_patents.py` (caveat assertion)

**Interfaces:**
- Consumes: `patents.search_prior_art` (Task 7).
- Produces: tool `search_prior_art` in `TOOL_DEFINITIONS`; `execute_tool(..., tool_name="search_prior_art")` returns a formatted string beginning with the US-only caveat.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_patents.py
import pytest

from src.agent.tools import _execute_search_prior_art, TOOL_DEFINITIONS

CAVEAT_MARK = "US filings only"


def test_search_prior_art_is_a_registered_tool():
    assert any(t["name"] == "search_prior_art" for t in TOOL_DEFINITIONS)


@pytest.mark.asyncio
async def test_output_always_carries_us_only_caveat(monkeypatch):
    from src.agent import tools as tools_mod
    monkeypatch.setattr(tools_mod, "search_prior_art", lambda q, limit=10: _fake([]))
    out = await _execute_search_prior_art("crispr delivery")
    assert CAVEAT_MARK in out
    assert "no US filings matched" in out.lower()


async def _fake(v):
    return v
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec app python -m pytest tests/unit/test_patents.py -v`
Expected: FAIL — `ImportError: cannot import name '_execute_search_prior_art'`

- [ ] **Step 3: Implement**

Add to `TOOL_DEFINITIONS` in `src/agent/tools.py`:

```python
    {
        "name": "search_prior_art",
        "description": (
            "Search issued US patents (USPTO / PatentsView) for prior art related "
            "to an idea or technique. Use when assessing whether an idea is novel "
            "or patentable. US filings only — absence of a hit is NOT proof of "
            "novelty."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free-text description of the idea/technique"},
            },
            "required": ["query"],
        },
    },
```

Add the executor and dispatch branch:

```python
from src.services.patents import search_prior_art

_PATENT_CAVEAT = (
    "Source: USPTO (PatentsView), US filings only. Absence of a hit here is not "
    "evidence of novelty — EP/WO/JP filings and non-patent prior art are not searched.\n\n"
)


async def _execute_search_prior_art(query: str) -> str:
    hits = await search_prior_art(query)
    if not hits:
        return _PATENT_CAVEAT + "No US filings matched this query."
    lines = [_PATENT_CAVEAT]
    for h in hits:
        assignee = ", ".join(h["assignees"]) or "Unassigned"
        lines.append(
            delimit(
                f"US{h['patent_id']} ({h['date']}) — {h['title']} [{assignee}]\n{h['abstract']}",
                "patent",
            )
        )
    return "\n\n".join(lines)
```

In `execute_tool`, add before the `else`:

```python
        elif tool_name == "search_prior_art":
            return await _execute_search_prior_art(tool_input["query"])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose exec app python -m pytest tests/unit/test_patents.py tests/unit/test_tool_gating.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agent/tools.py tests/unit/test_patents.py
git commit -m "feat(tools): search_prior_art tool with mandatory US-only caveat"
```

---

### Task 9: `scout_hub` role content

**Files:**
- Create: `prompts/roles/scout_hub/role.toml`
- Create: `prompts/roles/scout_hub/identity.md`
- Create: `prompts/roles/scout_hub/agent-system.md`
- Create: `prompts/roles/scout_hub/phase5-new-post.md`
- Test: `tests/unit/test_roles.py` (assert scout_hub loads with the hub tool set)

**Interfaces:**
- Consumes: `load_role` (Task 2), `search_prior_art` tool (Task 8).
- Produces: a working `scout_hub` role definition on disk.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_roles.py — note this uses the REAL prompts/roles dir
from src.agent.roles import load_role as _load_role_real  # no monkeypatch


def test_scout_hub_ships_with_the_hub_tool_set():
    spec = _load_role_real("scout_hub")
    assert spec.label == "Scout Hub"
    assert "search_prior_art" in spec.tools
    assert "retrieve_foa" not in spec.tools  # GrantBot fetches FOAs, not the hub
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec app python -m pytest tests/unit/test_roles.py::test_scout_hub_ships_with_the_hub_tool_set -v`
Expected: FAIL — no `role.toml`, so `load_role` returns `DEFAULT_TOOLS` (label `scout_hub`, no `search_prior_art`).

- [ ] **Step 3: Write the content files**

`prompts/roles/scout_hub/role.toml`:

```toml
label = "Scout Hub"
tools = ["retrieve_profile", "retrieve_abstract", "retrieve_full_text", "search_prior_art"]
```

`prompts/roles/scout_hub/identity.md`:

```markdown
## Your Identity
You are **{bot_name}**, an innovation-scouting agent for the Blackbird organization.
You do NOT represent a research lab. You interview one PI at a time to surface
ideas that may be patentable, fundable, or commercializable. Your agent ID is
"{agent_id}".
```

`prompts/roles/scout_hub/agent-system.md`: (replaces the collaboration mandate — write the scouting mandate, the one-PI-at-a-time confidentiality posture, and an explicit "you never broker PI-to-PI introductions." Keep the same top-level `# ...` heading shape as `prompts/agent-system.md` so downstream section assembly is unchanged.)

`prompts/roles/scout_hub/phase5-new-post.md`: (replaces the `:memo: Summary` + ✅ collaboration handshake with an opportunity-assessment artifact: **the idea**, **novelty read** — including the US-only caveat when prior-art was checked, **funding fit**, **commercialization path**, **recommended next step**.)

**Important — the phase-5 substitution mechanism (verified against `src/agent/agent.py:632-635`):** the builder uses `str.replace("{token}", value)`, NOT `str.format`. So a literal `{` elsewhere in the file is harmless, but these four exact tokens must appear verbatim or their section renders with the raw placeholder text: `{interesting_posts}`, `{subscribed_channels}`, `{your_recent_posts}`, `{prior_conversations}`.

There is a second, sharper constraint. The `funding_only` branch (`agent.py:599-623`) strips sections with **hardcoded regexes keyed to exact headings**: `## Your subscribed channels`, `## Your recent posts`, `## Prior conversations with other labs`, and an `### Option C: Make a new top-level post` block terminated by the lookahead `### Option D:`. If the `scout_hub` override renames those headings or drops the `Option C:`/`Option D:` structure, funding-only turns silently fail to strip the right sections. **Keep those headings and the Option C/D block boundaries byte-for-byte**; change the prose inside them and the artifact instructions, not the scaffolding. Read `prompts/phase5-new-post.md` alongside `agent.py:536-635` before writing this file.

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose exec app python -m pytest tests/unit/test_roles.py -v`
Expected: PASS

Then verify the phase-5 override renders without a `KeyError`:

```python
# quick harness, run once:
docker compose exec app python -c "
from src.agent.agent import Agent
a = Agent('blackbird','BlackbirdBot','Blackbird Labs', role='scout_hub')
# call the same builder Phase 5 uses with representative kwargs; must not raise
print('phase5 ok')
"
```

- [ ] **Step 5: Commit**

```bash
git add prompts/roles/scout_hub/ tests/unit/test_roles.py
git commit -m "feat(roles): ship scout_hub role content (persona, identity, assessment artifact)"
```

---

### Task 10: Cohort-aware lab directory (closes runbook A3)

**Files:**
- Modify: `src/agent/simulation.py` (`_build_lab_directories`)
- Test: `tests/unit/test_simulation_logic.py`

**Interfaces:**
- Consumes: `Agent.allowed_sender_ids` (set by the cohort gate on the branch).
- Produces: `_build_lab_directories` restricts each agent's directory to labs in `allowed_sender_ids` when that gate is active (not None).

**Context:** `_build_lab_directories` (`src/agent/simulation.py`, ~`:2384` pre-merge / renumbered on branch) builds each agent's "Other Labs' Recent Publications" from *every* other agent. The cohort gate filters the message log but not this system-prompt section, so a hub is primed with the whole roster. Scope it to the gate.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_simulation_logic.py (add)
def test_lab_directory_respects_the_cohort_gate():
    # Build 3 agents; gate agent A to only see B (not C). A's directory must
    # exclude C's publications.
    # (Construct a minimal SimulationEngine with three Agents whose public_profile
    # contains a "## Recent Publications" block, set A.allowed_sender_ids = {"b"},
    # call engine._build_lab_directories(), assert C's pub line not in A._lab_directory
    # and B's pub line is present. Leave B.allowed_sender_ids = None (gate off) and
    # assert B still sees everyone.)
    ...
```

(Fill the body against the existing `SimulationEngine` construction pattern in `tests/unit/test_simulation_logic.py`; the assertion is: gated agent's directory ⊆ its `allowed_sender_ids`, ungated agent's directory unchanged.)

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec app python -m pytest tests/unit/test_simulation_logic.py -k lab_directory -v`
Expected: FAIL — C's publications appear in A's directory.

- [ ] **Step 3: Implement**

In `_build_lab_directories`, when assembling `sections` for an agent whose `allowed_sender_ids is not None`, skip any `other_id` not in that set:

```python
        for agent in self.agents.values():
            allowed = agent.allowed_sender_ids  # None == gate off
            sections = []
            for other_id, pubs in sorted(lab_pubs.items()):
                if other_id == agent.agent_id:
                    continue
                if allowed is not None and other_id not in allowed:
                    continue  # cohort gate: don't prime this agent with a non-mate's work
                other_agent = self.agents[other_id]
                sections.append(f"### {other_agent.pi_name} Lab")
                sections.extend(pubs)
                sections.append("")
            agent._lab_directory = "\n".join(sections) if sections else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose exec app python -m pytest tests/unit/test_simulation_logic.py -k lab_directory -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agent/simulation.py tests/unit/test_simulation_logic.py
git commit -m "fix(cohort): scope lab directory to the cohort gate (runbook A3)"
```

---

### Task 11: Admin surface — show/edit role

**Files:**
- Modify: `src/routers/admin.py` (agent-edit handler: accept `role`; cohort topology context: expose `role`)
- Modify: `templates/admin/` agent-edit template (role `<select>` or text input) and `templates/admin/cohort_topology.html` (read-only role column)
- Test: `tests/integration/test_cohort_admin.py` or `tests/integration/test_agent_page.py`

**Interfaces:**
- Consumes: `AgentRegistry.role` (Task 4).
- Produces: an admin can set an agent's role from `/admin/agents`; the topology page shows each agent's role.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_cohort_admin.py (add)
import pytest
pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_admin_can_set_agent_role(admin_client, seeded_agent):
    resp = await admin_client.post(
        f"/admin/agents/{seeded_agent.agent_id}/edit",
        data={"role": "scout_hub", **_required_edit_fields(seeded_agent)},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    # re-read from DB
    from src.models import AgentRegistry
    from sqlalchemy import select
    async with admin_client.app.state.session_factory() as db:
        row = (await db.execute(select(AgentRegistry).where(
            AgentRegistry.agent_id == seeded_agent.agent_id))).scalar_one()
    assert row.role == "scout_hub"
```

(Adapt `admin_client`, `seeded_agent`, and `_required_edit_fields` to the actual fixtures/edit-form fields in `tests/integration/test_cohort_admin.py`. Read the existing agent-edit route in `src/routers/admin.py` to learn the exact endpoint path and required form fields before writing this.)

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec app python -m pytest tests/integration/test_cohort_admin.py -k role -v`
Expected: FAIL — role not persisted (handler ignores the field).

- [ ] **Step 3: Implement**

Read the agent-edit route in `src/routers/admin.py`; add `role: str = Form("pi_lab")` to its signature and set `agent.role = role` before commit. In the agent-edit template add a control (a `<select>` listing the role directories under `prompts/roles/` plus `pi_lab`, or a plain text input if that is simpler and matches the form's style). In `admin_cohort_topology` context and `cohort_topology.html`, add a read-only role cell next to `Status`.

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose exec app python -m pytest tests/integration/test_cohort_admin.py -k role -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/routers/admin.py templates/admin/ tests/integration/test_cohort_admin.py
git commit -m "feat(admin): view and edit agent role; show role on topology page"
```

---

### Task 12: Optional live PatentsView smoke test + full-suite gate

**Files:**
- Create: `tests/live_api/test_patents_live.py`
- Test: the whole suite

**Interfaces:**
- Consumes: `patents.search_prior_art`, a real `PATENTSVIEW_API_KEY` in the environment.

- [ ] **Step 1: Write the opt-in live test**

```python
# tests/live_api/test_patents_live.py
import os
import pytest

from src.services.patents import search_prior_art

pytestmark = [
    pytest.mark.live_api,
    pytest.mark.skipif(not os.getenv("PATENTSVIEW_API_KEY"), reason="no PatentsView key"),
]


@pytest.mark.asyncio
async def test_real_search_returns_hits_for_a_common_term(monkeypatch):
    monkeypatch.setenv("PATENTSVIEW_API_KEY", os.environ["PATENTSVIEW_API_KEY"])
    from src.config import get_settings
    get_settings.cache_clear()
    hits = await search_prior_art("crispr")
    assert isinstance(hits, list)
    if hits:
        assert "patent_id" in hits[0] and "title" in hits[0]
```

Mirror the marker/skip convention in `tests/live_api/test_grants_live.py` exactly (marker name, env-guard style).

- [ ] **Step 2: Run it (guarded)**

Run: `PATENTSVIEW_API_KEY=<key> docker compose exec -e PATENTSVIEW_API_KEY app python -m pytest tests/live_api/test_patents_live.py -v`
Expected: PASS if a key is present; SKIP otherwise.

- [ ] **Step 3: Run the full unit + integration suite**

Run: `docker compose exec app python -m pytest tests/unit tests/integration tests/characterization -q`
Expected: PASS (same baseline as before this plan, plus the new tests). Investigate any characterization/golden-master failure as a `pi_lab`-drift regression (see Task 3).

- [ ] **Step 4: Run the branch's local CI gate**

Run: `docker compose exec app bash scripts/ci.sh` (or the documented invocation in that script's header)
Expected: PASS — includes the alembic round-trip that must now reach `0024`.

- [ ] **Step 5: Commit**

```bash
git add tests/live_api/test_patents_live.py
git commit -m "test(patents): opt-in live PatentsView smoke test"
```

---

## Self-Review

**Spec coverage:**
- §3 role directory + per-file fallback → Task 1.
- §4.1 `roles.py` / `RoleSpec` / `DEFAULT_TOOLS` / failure policy → Tasks 1–2.
- §4.2 `Agent.role`, `_load_prompt`, builder collapse, identity file, `.replace` → Task 3.
- §4.3 tool dispatch filter + executor refusal + Phase-4-only scope → Task 6, Task 8.
- §4.4 `AgentRegistry.role` + roster role-diff → Tasks 4–5.
- §5 `scout_hub` content → Task 9.
- §6 `src/services/patents.py`, config key, cache, caveat, failure modes → Tasks 7–8. **Note:** the design's `data/patent_cache/` disk cache is described but NOT implemented in Task 7 (the service is stateless there). It is deferred as YAGNI for a single hub bot under 45 req/min; if added later it mirrors `foa_cache.py`. Flagged so the omission is deliberate, not a gap.
- §7 migration `0024` → Task 4.
- §8 admin surface → Task 11.
- §9 testing tiers → distributed across every task + Task 12.
- §10 risks → carried into the runbook; the role-is-not-confidentiality risk is not code.
- §11 A8 (per-role prompt) → Tasks 1–3, 6, 9; A3 (lab directory) → Task 10.

**Placeholder scan:** Task 10 and Task 11 test bodies are described rather than fully coded, because both depend on fixture shapes (`SimulationEngine` construction, `admin_client`) that must be read from the existing test modules named in each task. Each names the exact file to read and the exact assertion to make. All *implementation* steps contain literal code. Task 9's two prose prompt files (`agent-system.md`, `phase5-new-post.md`) are content-authoring, not code — the constraint that matters (preserve `.format()` keys in phase5) is stated explicitly with the failure mode.

**Type consistency:** `RoleSpec(name, label, tools)` consistent across Tasks 2/6/9. `load_role` return type used identically in `tools_for_role` and `execute_tool`. `Agent(role=...)` keyword consistent across Tasks 3/5. `search_prior_art(query, limit)` signature consistent across Tasks 7/8/12. `execute_tool(..., role=...)` new param consistent across Tasks 6/8.

**Known deferrals (deliberate):** patent disk cache (see above); no per-role caps/budgets (design §10 A7, out of scope).

---

## Notes for the executor

- **Task order matters.** 1→2 (roles module), 3 (agent), 4→5 (DB + roster), 6 (gating needs 2+3), 7→8 (service then tool; Task 2's `search_prior_art` allow-list only becomes non-dropped after Task 8 registers it), 9 (content needs 8), 10 and 11 independent, 12 last.
- This plan and the design doc are **untracked** by request; the *code* commits above are real. Do not commit the two `docs/` files unless the user asks.
- If the fast-forward merge has NOT happened yet, stop — every task assumes branch code (cohort gate, `allowed_sender_ids`, alembic `0023`, `tests/{unit,integration,live_api}` layout).
