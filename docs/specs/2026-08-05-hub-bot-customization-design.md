# Design — generalizable per-role agent customization (hub bot)

**Status:** DESIGN, not implemented. Untracked by request.
**Date:** 2026-08-05
**Target branch:** `blackbird` after fast-forwarding `origin/cohort-db-conversations`
(see `docs/blackbird-star-topology-runbook.md`). This design assumes that merge has
landed — it builds on `0023` and on the cohort gate.
**Companion:** `docs/blackbird-star-topology-runbook.md` (the star topology). That
runbook's Phase 7 items A8 (per-agent system prompt) and A3 (cohort-aware lab
directory) are the problems this design closes.

---

## 1. Problem

The blackbird deployment needs a central `blackbird` hub bot with its own persona,
its own conversational output, and a capability no PI bot has (prior-art search). The
codebase has no per-agent behaviour: `prompts/agent-system.md`, the four phase
templates, the tool list, and the identity block are identical for every agent. The
identity block is worse than global — it is **hardcoded in Python and duplicated
verbatim across three methods** (`src/agent/agent.py` `build_system_prompt`,
`build_scan_system_prompt`, `build_thread_reply_system_prompt`) plus a fourth copy in
`_default_system_prompt()`. So even a perfect `prompts/agent-system.<hub>.md` would
still tell the hub it is "the {pi_name} lab at Scripps Research".

The requirement is **generalizable**: not a blackbird special case, but a role
mechanism that a differently-mandated hub — for another org, with another mission —
can reuse by assigning a role, with no new code.

## 2. Requirements (settled during brainstorming)

- **Depth:** persona + identity, *and* the hub's own output artifacts (phase prompts),
  *and* per-role capabilities (tools). NOT per-role caps/budgets (see §9, A7 stays open).
- **Keying:** a **role/archetype** assignable to agents, not per-`agent_id` files.
  Adding a second hub is a field change, not a new directory of near-duplicate files.
- **Capabilities:** a tool **allow-list** over existing tools, plus **one new
  hub-only tool now**: prior-art / patent search.
- **Patent source:** **PatentsView / USPTO**. Free API key, 45 req/min, US government
  data with no commercial-use restriction. (EPO OPS was rejected: its free tier is
  non-commercial/evaluation only, and blackbird's purpose is explicitly commercial IP
  identification, so EPO would require a paid plan.) US-only coverage is accepted and
  must be surfaced in the tool's output — see §6.

## 3. Approach — role directory with per-file fallback (chosen)

```
prompts/
  agent-system.md            ← unchanged; the pi_lab default
  identity.md                ← NEW: extracted verbatim from the Python identity block
  phase2-scan-filter.md      ← unchanged
  phase2-prune.md            ← unchanged
  phase4-thread-reply.md     ← unchanged
  phase5-new-post.md         ← unchanged
  roles/
    scout_hub/
      role.toml              ← label + tool allow-list
      identity.md            ← overrides prompts/identity.md
      agent-system.md        ← overrides the persona/mandate
      phase5-new-post.md     ← overrides the collaboration artifact
```

**Lookup:** `resolve_prompt_path(role, filename)` returns
`prompts/roles/{role}/{filename}` if it exists, else `prompts/{filename}`. A role
overrides only the files it names; everything else is inherited. `scout_hub` inherits
`phase2-*` and `phase4-thread-reply` unchanged — only persona, identity and the
Phase-5 artifact differ.

**The default role `pi_lab` is the absence of overrides.** `prompts/roles/pi_lab/`
never needs to exist; falling through to `prompts/*.md` *is* `pi_lab`. This is what
makes "existing PI bots are untouched" a structural fact rather than something to
verify by reading.

Rejected alternatives:
- **One TOML manifest per role** (prompt bodies inline): new config format; 40–80-line
  phase prompts inside TOML are unpleasant; loses `diff role/phase5 vs default`.
- **Role as a Python class:** every role change becomes a deploy; loses the
  bind-mounted-prompt hot-reload the repo deliberately relies on; moves prompt content
  into code.

Approach A is the only option that preserves both properties this codebase already
relies on: **prompts are hot-editable data** (`./prompts:/app/prompts` is bind-mounted
on the app and agent services; `_load_file` re-reads every build with no caching), and
**roster changes take effect without a restart** (the ~30s `_sync_roster_from_db`
tick). It is also the smallest diff — the per-file fallback is one helper, and the
three duplicated prompt builders collapse into it rather than sprouting a fourth copy.

## 4. Components

### 4.1 `src/agent/roles.py` (new, no DB imports — stays unit-testable)

```python
ROLES_DIR = Path("prompts/roles")
DEFAULT_ROLE = "pi_lab"
# Explicit, NOT "everything in TOOL_DEFINITIONS": if the default were "all tools",
# adding search_prior_art to the global list would silently hand it to every PI bot.
# Explicit default makes every new tool opt-in.
DEFAULT_TOOLS = frozenset({
    "retrieve_profile", "retrieve_abstract", "retrieve_full_text", "retrieve_foa",
})

@dataclass(frozen=True)
class RoleSpec:
    name: str
    label: str
    tools: frozenset[str]

def resolve_prompt_path(role: str, filename: str) -> Path: ...   # role file else global
def load_role(name: str) -> RoleSpec: ...                        # role.toml else defaults
```

`load_role` failure policy: a missing `role.toml` → defaults; a malformed one → log at
ERROR and fall back to `DEFAULT_TOOLS` (never raise into a turn); a tool name in the
allow-list that is not in `TOOL_DEFINITIONS` → log and drop. A typo degrades the hub;
it does not kill the run.

### 4.2 `src/agent/agent.py`

- `Agent.__init__` gains `role: str = DEFAULT_ROLE`.
- The seven `_load_file(PROMPTS_DIR / "x.md", …)` sites become `_load_prompt("x.md", …)`
  routed through `resolve_prompt_path(self.role, "x.md")`.
- **Collapse the three system-prompt builders into one.** They differ only in which
  optional blocks they append:

  | builder | working memory | lab directory | private rules |
  |---|---|---|---|
  | `build_system_prompt` | ✓ | ✓ | if private |
  | `build_scan_system_prompt` | — | — | — |
  | `build_thread_reply_system_prompt` | ✓ | — | if private |

  → `_compose_system_prompt(*, include_memory, include_lab_directory, visibility,
  channel_id)` plus three thin wrappers. This collapse is mandatory: it is what stops
  the role change from adding a *fourth* copy of the identity block.
- **Identity moves to `prompts/identity.md`**, extracted verbatim so `pi_lab` output is
  byte-identical to today. Substitution via explicit `.replace()` on `{bot_name}`,
  `{pi_name}`, `{agent_id}` — NOT `str.format`, because a stray brace in a profile or
  role file would raise `KeyError` mid-turn. Three tokens do not justify that exposure.

### 4.3 Tool dispatch (`src/agent/tools.py`, `src/agent/simulation.py`)

- Dispatch is one static `TOOL_DEFINITIONS` list + an if/elif in `execute_tool`, wired
  in at exactly one site (`simulation.py:851`, Phase-4 thread replies). Role gating is
  therefore cheap: filter the tool list passed to the LLM by `RoleSpec.tools`, and have
  `execute_tool` refuse a tool the agent's role does not carry (defence in depth —
  the model should never see it, but the executor is the real boundary).
- **Scope boundary:** tools run in **Phase 4 only**. The hub can check prior art while
  discussing an idea with a PI, not while composing a top-level post. For a scouting
  hub that is the right moment; this change does not extend tool use to Phase 5.

### 4.4 `AgentRegistry.role`

- New column `role String(20) NOT NULL DEFAULT 'pi_lab'`. Migration `0024` (§7).
- `_sync_roster_from_db` currently computes `to_add`/`to_remove` and **returns early
  when both are empty**, so a role change on a surviving agent would be invisible to a
  running sim. Add a **role-diff pass** over surviving agents: if a live agent's DB role
  differs from its in-memory role, update it and rebuild its derived structures. Role
  reassignment then goes live on the same ~30s tick as activation/inactivation.

## 5. `scout_hub` role content (ships with the mechanism)

- `roles/scout_hub/identity.md` — "You are **{bot_name}**, an innovation-scouting agent
  for the Blackbird organization. You do not represent a lab; you interview one PI at a
  time to surface patentable, fundable, and commercializable ideas."
- `roles/scout_hub/agent-system.md` — replaces the collaborate-between-labs mandate with
  the scouting mandate and the confidentiality posture (talks to one PI at a time, never
  brokers PI↔PI).
- `roles/scout_hub/phase5-new-post.md` — replaces the `:memo: Summary` + ✅ collaboration
  handshake with an **opportunity assessment** artifact: the idea, novelty read (with
  the §6 US-only caveat), funding fit, commercialization path, recommended next step.
- `role.toml` — `label = "Scout Hub"`,
  `tools = ["retrieve_profile", "retrieve_abstract", "retrieve_full_text", "search_prior_art"]`.
  (`retrieve_foa` omitted: the hub reasons about funding fit; GrantBot fetches FOAs.)

`phase2-scan-filter`, `phase2-prune`, `phase4-thread-reply` are inherited unchanged.

## 6. Prior-art tool — `src/services/patents.py` (new)

Follows `src/services/grants.py` house style: module-level URL constant,
`httpx.AsyncClient`, plain dicts out.

```python
SEARCH_URL = "https://search.patentsview.org/api/v1/patent/"
async def search_prior_art(query: str, limit: int = 10) -> list[dict[str, Any]]
```

Returns `patent_id`, `title`, `date`, `abstract`, `assignees`.

- **Auth:** `X-Api-Key` header from `settings.patentsview_api_key: str = ""`. The name
  contains `key`, so it is auto-redacted in `repr(settings)` by the existing
  `_SECRET_NAME_HINTS` path — the name is chosen for that.
- **Cache:** `data/patent_cache/{sha256(query)[:16]}.json`, mirroring `foa_cache.py`.
  `./data:/app/data` is already mounted on the `agent` service (the only tool-runner).
- **Mandatory output caveat**, prefixed to every result set and asserted by a test:
  > Source: USPTO (PatentsView), **US filings only**. Absence of a hit here is not
  > evidence of novelty — EP/WO/JP filings and non-patent prior art are not searched.

  Load-bearing: a patentability-judging hub will otherwise read empty results as
  "novel" and be confidently wrong about anything filed only abroad.
- **Failure modes — all return strings, never raise** (matches `execute_tool`'s
  catch-all):

  | condition | behaviour |
  |---|---|
  | no API key | `"Prior-art search unavailable: no PatentsView API key configured."` |
  | HTTP 429 | honour `Retry-After`, one retry, then soft failure |
  | non-200 / timeout | log ERROR, return soft-failure string |
  | zero hits | explicit "no US filings matched" — distinct from an error |

  45 req/min is not a real constraint for one hub bot calling inside Phase-4 turns.

## 7. Migration `0024_add_agent_role.py`

`op.add_column("agents", sa.Column("role", sa.String(20), nullable=False,
server_default="pi_lab"))`; idempotent `if_exists` downgrade, matching the
`0022`/`0023` convention on the branch. **Renumber at merge time** if the branch or
`main` adds a migration first — this is the branch's own §4.2 rule
(`specs/cohort-system-v2.md`).

## 8. Admin surface (small, no new page)

- `AgentRegistry.role` shown and editable on the existing `/admin/agents` agent-edit
  form.
- A read-only role line on `/admin/cohorts/topology` so a hub is visibly a hub next to
  its gate preview.

## 9. Testing

Tiers match the branch's own layout.

- **unit, no DB:** `resolve_prompt_path` fallback; `load_role` missing/partial/malformed
  `role.toml`; unknown tool dropped-and-logged; **`pi_lab` yields a byte-identical
  system prompt to pre-change** (the regression guard proving PI bots are untouched);
  US-only caveat present in tool output.
- **unit:** `search_prior_art` against a recorded PatentsView fixture; the four failure
  modes; cache hit/miss.
- **integration:** role-diff in `_sync_roster_from_db` flips an agent's role live; a
  `scout_hub` agent's tool list contains `search_prior_art` and a `pi_lab` agent's does
  not.
- **live_api (opt-in):** one real PatentsView call behind the same marker
  `tests/live_api/test_grants_live.py` uses.

## 10. Risks on the record

1. **The role gate is not confidentiality.** `scout_hub` makes a bot *behave* as a hub;
   it does not enforce the star. The cohort gate + Slack-off (topology runbook) do that.
   Assigning `scout_hub` while skipping the cohort setup yields a bot that talks like a
   hub *to everyone*. The two designs are complementary and both required.
2. **Prompt content is data — unversioned, unreviewed.** Editing
   `roles/scout_hub/phase5-new-post.md` on disk changes hub behaviour live, with no code
   review and no migration. That is the intended hot-edit property, but the role's
   *behaviour* can drift from what any test pins. Keep the shipped role files in git;
   round-trip live edits back to the repo.
3. **A7 (hub capacity) is deliberately out of scope.** `active_thread_threshold = 3`
   is global, so the hub holds at most 3 concurrent PI conversations against N spokes
   (threads hard-close at `max_thread_messages = 12`). This design does not add
   per-role caps. If the hub saturates, that is a separate change; measure first
   (topology runbook Phase 7 A7).

## 11. What this closes in the topology runbook

- **A8** (per-agent system prompt) — closed by §3/§4/§5.
- **A3** (cohort-aware lab directory) — closed opportunistically: with the identity/
  persona now role-driven, scope `_build_lab_directories` to `allowed_sender_ids` when
  the gate is on, so a `scout_hub` is not primed with the whole roster. Small, include
  it here.
