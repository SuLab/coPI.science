# Admin user pages: read-only cohort labels — implementation plan

Status: rev 3 — EXECUTED 2026-10-08 (see §9 for post-implementation audit corrections). Base: `main` @ `c0dd9610`. Execute with `/lathe:plan-execution`:
packages P1–P3 own disjoint files and are implemented in parallel with **no build or test**
until all three are merged; then the single integrated verification in §4, then the audit in §5.

**Goal.** Show each user's cohorts on `/admin/users` (column + filter) and `/admin/users/{id}`
(agent link + cohort links + what the membership means right now). Display only.

**Evidence base.** Read-only analysis of 2026-10-08 plus an adversarial audit (`lathe:auditor`).
Prod facts were re-checked with independent `BEGIN READ ONLY` queries and `env` in the running
`app`/`worker` containers. Line numbers are at `c0dd9610`.

## 0. Facts this plan rests on

- Cohort membership is keyed by the **agent slug**, not a user: `cohort_memberships.agent_id`
  (`src/models/cohort.py:76-78`), no FK, unique `(cohort_id, agent_id)` (migration 0022).
- A user's cohorts = the cohorts of the agent they **own**: `users.id → agents.user_id`
  (unique, nullable, SET NULL) `→ agents.agent_id → cohort_memberships.agent_id`.
  `User.agent` is `uselist=False` (`src/models/user.py:61-63`). Delegation
  (`User.delegated_agents`) is **not** membership.
- Today neither `/admin/users` (`src/routers/admin.py:77-171`) nor `/admin/users/{id}`
  (`admin.py:174-210`) shows cohorts; the detail page does not show the agent at all and does
  not load `User.agent` (async SQLAlchemy will raise, not lazy-load, if a template touches it).
- Prod (2026-10-08): 146 users; 127 agents, all user-linked; 3 cohorts / 155 memberships;
  122 users would show ≥1 cohort; 19 users own no agent; an agent is in at most 3 cohorts;
  `COHORT_ISOLATION_ENABLED=true` in `app` and `worker`; `COHORT_DEFAULT_POLICY` unset → `open`;
  no cohort is named `none`; zero admin-UI membership edits ever (all audit rows actorless).
- Under isolation + policy `open`, an **uncohorted** active agent is *unrestricted*
  (`src/services/cohorts.py:155-166`); "no cohort" must not read as "isolated".
- `_cohort_gate_context(db)` (`admin.py:1470-1519`) already computes the engine's gate
  (`compute_gates`) for the active roster: `preview[agent_id]` is `None` (unrestricted) or a
  sorted list (`[]` = isolated); non-active agents are absent. Reusing it keeps the wording
  from drifting from engine behaviour.
- The users table rows are covered by an `after:absolute after:inset-0` link
  (`templates/admin/users.html:78`); any other in-row link needs `relative z-10` (as the ORCID
  link at `:89-90`). `base.html:203-212`'s row-click handler ignores clicks on nested `<a>`.
- `tests/integration/test_admin_users.py:72` hard-codes `_COLUMN_COUNT = 11`, reads Access from
  `cells[3]`, and asserts the empty-state `colspan` (`:266`).
- Responsive tier (`tests/responsive/`): `<a>` inside `p/li/td/th/dd` is exempt from the
  target-size check (`checks.py:30`); text directly in `p/td/li/label/dd` needs ≥14 px at
  320/390 (`checks.py:86,175-184`). Its seed puts the detail-page user's agent `respres` into
  a cohort (`seed.py:258-263,293`), so the new markup is exercised. The host has the
  Chromium binary but not its system libraries (`libatk-1.0.so.0`), so this tier errors there
  on `main` too; see §9 for how it was run instead.
- Stale wording: the `cohorts.json` `_comment` ("cohort_isolation_enabled is False"), the
  runtime `print` at `scripts/seed_cohorts.py:139` ("isolation stays off"), and that module's
  docstring claim at `:14-15` that enabling isolation before every agent has a cohort "would
  gate nothing" — under policy `open` a cohorted agent's gate is its co-members plus the
  uncohorted agents, so agents in different cohorts ARE gated (`cohorts.py:155-171`). The
  docstring's "default False" is correct. `specs/admin-dashboard.md:15-46` describes both pages
  and must gain the new column, filter and Account rows.

## 1. Global constraints

- **Display only.** No migration, no new POST route, no change to `cohort_memberships`,
  `compute_gates`, the engine, or any PI-facing view. Membership stays edited on `/admin/cohorts`.
- Tailwind classes are literal tokens; badge tones come only from `ui.badge` in
  `templates/_components.html:73-85`. No interpolated class fragments
  (`tests/unit/test_static_assets.py:86-97`).
- Template strings in §3 are a contract between P1 and P2 — copy them byte-for-byte
  (including the em dash `—`).
- Integration tests use `pytestmark = pytest.mark.integration`. Tests that depend on the
  isolation flag/policy must pin them with `monkeypatch.setattr(get_settings(), ...)`: the host
  `.env` sets `COHORT_ISOLATION_ENABLED=true` and `get_settings()` is `lru_cache`d
  (precedent: `tests/integration/test_cohort_admin.py:167-172`).
- Gates: ruff on tests = zero findings; `src/` ruff ≤ `SRC_LINT_MAX` (260); mypy ≤ `MYPY_MAX`
  (150) — narrow `User.agent`'s `Optional` before use; coverage ≥ `COV_MIN` (60).
- This checkout **is** the production host tree (`/home/ubuntu/copi-python` via sshfs). Work on
  a branch; do not run `scripts/redeploy.sh` while the branch is checked out unless deploying it.

## 2. Preconditions (user decisions — do not proceed without them)

1. `main` is **2 commits ahead of `origin/main`** (`c0dd9610`, `cd36b694`). Either push `main`
   first, or accept that a branch cut from it carries them into the PR.
2. Create the branch: `git switch -c admin-user-cohort-labels` (untracked
   `docs/plans/2026-09-23-manuscript-handling/` stays untouched).
3. Baseline, before any edit, on the host:
   `ssh <prod-host> 'cd ~/copi-python && scripts/build-css.sh && ./scripts/ci.sh' 2>&1 | tee <scratchpad>/ci-baseline.log`
   — record every pre-existing failure so §4 can tell new from old.

## 3. Packages

| Package | Owns (exclusively) |
|---|---|
| P1 | `src/routers/admin.py`, `templates/admin/users.html`, `templates/admin/user_detail.html` |
| P2 | `tests/integration/test_admin_users.py`, `tests/integration/test_admin_user_cohorts.py` (new) |
| P3 | `cohorts.json`, `scripts/seed_cohorts.py`, `specs/admin-dashboard.md` |

### P1 — route + templates

**Interfaces produced:** `admin_users` gains query param `cohort_filter: str | None` (a cohort
UUID string, `__none__` = owns an agent with no cohort, `__no_agent__` = owns no agent; any other
value matches nothing). Template contexts gain `all_cohorts`, `cohort_filter`,
`cohort_filter_no_cohort`, `cohort_filter_no_agent` (list) and `owned_agent`, `agent_cohorts`,
`gate` (detail). Each `user_data` row gains `"cohorts": list[Cohort]`.

- [ ] **1. `src/routers/admin.py` — constants**, directly after `VALID_AGENT_STATUSES` (`:61`):

```python
# /admin/users cohort_filter values besides a cohort's UUID. Neither a UUID nor a cohort
# name (^[a-z0-9-]{1,48}$) can contain "_", so these cannot collide with a real cohort —
# a cohort may legally be named "none".
COHORT_FILTER_NO_COHORT = "__none__"
COHORT_FILTER_NO_AGENT = "__no_agent__"
```

- [ ] **2. Helper**, directly after `_template_context` (`:64-74`):

```python
async def _cohorts_by_agent(
    db: AsyncSession, agent_id: str | None = None
) -> dict[str, list[Cohort]]:
    """Map agent slug -> its cohorts, each list ordered by cohort name.

    Display-only lookup for the admin user pages. Membership is keyed by the
    AgentRegistry slug, so a user's cohorts are those of the agent they OWN
    (``User.agent``); a delegated agent's cohorts are not the delegate's.
    """
    query = (
        select(CohortMembership.agent_id, Cohort)
        .join(Cohort, CohortMembership.cohort_id == Cohort.id)
        .order_by(Cohort.name)
    )
    if agent_id is not None:
        query = query.where(CohortMembership.agent_id == agent_id)
    out: dict[str, list[Cohort]] = {}
    for aid, cohort in (await db.execute(query)).all():
        out.setdefault(aid, []).append(cohort)
    return out
```

- [ ] **3. `admin_users`** (`:79-171`):
  - Signature: add `cohort_filter: str | None = None,` after `access_filter`.
  - Docstring: append one paragraph — "The Cohorts column is display-only: it shows the
    memberships of the agent the user owns (membership is keyed by agent slug). Membership is
    edited on /admin/cohorts."
  - After the `pub_counts` dict (`:112`), add:

```python
    cohorts_by_agent = await _cohorts_by_agent(db)
    all_cohorts = (await db.execute(select(Cohort).order_by(Cohort.name))).scalars().all()
```

  - In the loop, after the `access_filter` check (`:138-139`), add:

```python
        agent_cohorts = cohorts_by_agent.get(user.agent.agent_id, []) if user.agent else []
        if cohort_filter:
            if cohort_filter == COHORT_FILTER_NO_AGENT:
                if user.agent:
                    continue
            elif cohort_filter == COHORT_FILTER_NO_COHORT:
                if not user.agent or agent_cohorts:
                    continue
            elif cohort_filter not in {str(c.id) for c in agent_cohorts}:
                continue
```

  - `user_data.append({...})`: add `"cohorts": agent_cohorts,`.
  - Template context: add `cohort_filter=cohort_filter, all_cohorts=all_cohorts,
    cohort_filter_no_cohort=COHORT_FILTER_NO_COHORT, cohort_filter_no_agent=COHORT_FILTER_NO_AGENT,`.

- [ ] **4. `admin_user_detail`** (`:174-210`):
  - Query options: `.options(selectinload(User.profile), selectinload(User.jobs), selectinload(User.agent))`.
  - After the publications query, add:

```python
    # Cohort labels belong to the agent this user OWNS (membership is keyed by agent
    # slug) and move with it if it is re-linked. Read-only; membership is edited on
    # /admin/cohorts. The gate preview is the engine's own compute_gates, so the
    # "unrestricted"/"isolated" wording cannot drift from behaviour.
    owned_agent = user.agent
    agent_cohorts: list[Cohort] = []
    gate: dict[str, Any] | None = None
    if owned_agent is not None:
        agent_cohorts = (
            await _cohorts_by_agent(db, owned_agent.agent_id)
        ).get(owned_agent.agent_id, [])
        gate = await _cohort_gate_context(db)
```

  - Template context: add `owned_agent=owned_agent, agent_cohorts=agent_cohorts, gate=gate,`.

- [ ] **5. `templates/admin/users.html`**
  - Filter: after the Claimed `<div>` block (`:46-54`), add:

```html
    <div>
        <label class="text-sm font-medium text-gray-600 block mb-1">Cohort</label>
        <select onchange="applyFilter()" id="cohort-filter"
                class="min-h-11 sm:min-h-6 border border-gray-300 rounded-sm px-2 py-1 text-sm">
            <option value="">All</option>
            {% for c in all_cohorts %}
            <option value="{{ c.id }}" {% if cohort_filter == c.id|string %}selected{% endif %}>{{ c.name }}</option>
            {% endfor %}
            <option value="{{ cohort_filter_no_cohort }}" {% if cohort_filter == cohort_filter_no_cohort %}selected{% endif %}>Agent, no cohort</option>
            <option value="{{ cohort_filter_no_agent }}" {% if cohort_filter == cohort_filter_no_agent %}selected{% endif %}>No agent</option>
        </select>
    </div>
```

  - Header: after the Agent `<th>` (`:66`), add
    `<th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase hidden md:table-cell">Cohorts</th>`.
  - Body: after the Agent `<td>` (`:114-125`), before the Pubs `<td>` (`:126`), add:

```html
        <td class="px-4 py-3 hidden md:table-cell">
            {% if not item.user.agent %}
            <span class="text-sm text-gray-400">No agent</span>
            {% elif item.cohorts %}
            <div class="flex flex-wrap gap-1">
                {% for c in item.cohorts %}
                <a href="/admin/cohorts/{{ c.id }}" class="relative z-10">{{ ui.badge(c.name, 'brand' if item.user.agent.status == 'active' else 'neutral') }}</a>
                {% endfor %}
            </div>
            {% else %}
            <span class="text-sm text-gray-400">—</span>
            {% endif %}
        </td>
```

  - Empty state: `colspan="11"` → `colspan="12"` (`:150`).
  - `applyFilter()` (`:157-166`): add
    `const cohort = document.getElementById('cohort-filter').value;` and
    `if (cohort) params.set('cohort_filter', cohort);` alongside the others.

- [ ] **6. `templates/admin/user_detail.html`** — at the end of the Account `<dl>`, after the
  Joined `<dd>` (`:36`):

```html
            <dt class="text-gray-500 text-sm">Agent</dt>
            <dd class="font-medium text-sm break-all">
                {% if owned_agent %}
                <a href="/admin/agents/{{ owned_agent.id }}" class="text-green-600 hover:underline">{{ owned_agent.bot_name }}</a>
                <span class="font-normal text-gray-500">({{ owned_agent.status }})</span>
                {% else %}—{% endif %}
            </dd>
            <dt class="text-gray-500 text-sm">Cohorts</dt>
            <dd class="text-sm" id="user-cohorts">
                {% if not owned_agent %}
                <span class="text-gray-500">No agent — cohorts are assigned per agent</span>
                {% elif agent_cohorts %}
                <div class="flex flex-wrap gap-1">
                    {% for c in agent_cohorts %}
                    <a href="/admin/cohorts/{{ c.id }}">{{ ui.badge(c.name, 'brand' if owned_agent.status == 'active' else 'neutral') }}</a>
                    {% endfor %}
                </div>
                {% if not gate.isolation_enabled %}
                <div class="mt-1 text-gray-500">Recorded, not enforced: cohort isolation is off</div>
                {% elif owned_agent.status != 'active' %}
                <div class="mt-1 text-gray-500">Recorded, not in effect while the agent is {{ owned_agent.status }}</div>
                {% endif %}
                {% elif not gate.isolation_enabled %}
                <span class="text-gray-500">None (cohort isolation is off)</span>
                {% elif owned_agent.agent_id not in gate.preview %}
                <span class="text-gray-500">None</span>
                {% elif gate.preview[owned_agent.agent_id] is none %}
                <span class="text-gray-500">None — unrestricted (acts on every agent)</span>
                {% else %}
                <span class="text-gray-500">None — isolated (acts on no other agent)</span>
                {% endif %}
            </dd>
```

### P2 — tests

- [ ] **1. `tests/integration/test_admin_users.py`**
  - `:72-73` → `_COLUMN_COUNT = 12  # Name, Institution, ORCID, Access, Status, Agent, Cohorts,`
    / `# Pubs, Version, Claimed, Joined, Last Login`. `cells[3]` (Access) is unchanged.
  - `:264-265` comment (two physical lines) — replace
    `    # added column, "No users found" renders inside the Name column with ten`
    `    # blank cells beside it; _access_cell's per-row count never sees this row.`
    with
    `    # added column, "No users found" renders inside the Name column with blank`
    `    # cells beside it; _access_cell's per-row count never sees this row.`

- [ ] **2. Create `tests/integration/test_admin_user_cohorts.py`:**

```python
"""Cohort labels on the admin user pages (/admin/users, /admin/users/{id}).

Display only. Membership is keyed by agent slug (src/models/cohort.py), so a
user's cohorts are those of the agent they OWN through AgentRegistry.user_id —
never a delegated agent's. Real ASGI requests, real Postgres, real Jinja: the
same harness as tests/integration/test_admin_users.py.
"""

import base64
import json
import re
import uuid

import pytest
from itsdangerous import TimestampSigner

from src.config import get_settings
from src.models import AgentDelegate, Cohort, CohortMembership
from tests import factories

pytestmark = pytest.mark.integration

# Name, Institution, ORCID, Access, Status, Agent, Cohorts, Pubs, Version,
# Claimed, Joined, Last Login — keep in step with test_admin_users._COLUMN_COUNT.
_COLUMN_COUNT = 12
_COHORTS = 6


def _auth(user_id) -> dict:
    """Forge the signed session cookie SessionMiddleware would issue."""
    signer = TimestampSigner(get_settings().secret_key)
    data = base64.b64encode(json.dumps({"user_id": str(user_id)}).encode())
    return {"Cookie": f"copi-session={signer.sign(data).decode()}"}


def _rendered_names(html: str) -> list[str]:
    return re.findall(r'<div class="text-sm font-medium text-gray-900">([^<]+)</div>', html)


def _cohorts_cell(html: str, name: str) -> str:
    rows = [
        m.group(0)
        for m in re.finditer(r"<tr\b.*?</tr>", html, re.S)
        if f">{name}</div>" in m.group(0)
    ]
    assert len(rows) == 1, f"expected exactly one row for {name!r}, got {len(rows)}"
    cells = re.findall(r"<td\b.*?</td>", rows[0], re.S)
    assert len(cells) == _COLUMN_COUNT, f"expected {_COLUMN_COUNT} cells, got {len(cells)}"
    return cells[_COHORTS]


def _cohorts_dd(html: str) -> str:
    m = re.search(r'<dd[^>]*\bid="user-cohorts"[^>]*>(.*?)</dd>', html, re.S)
    assert m, "the Cohorts <dd> is missing from the user detail page"
    return m.group(1)


@pytest.fixture
async def admin(db_session):
    return await factories.make_user(
        db_session, name="Site Admin", is_admin=True, email="admin@example.org"
    )


@pytest.fixture
async def world(db_session, admin):
    """Every case the labels must tell apart, in one roster."""

    async def owner(name, slug, status="active"):
        user = await factories.make_user(db_session, name=name, email=f"{slug}@example.org")
        agent = await factories.make_agent(
            db_session, user=user, agent_id=slug, bot_name=f"{slug.title()}Bot",
            pi_name=name, status=status,
        )
        return user, agent

    two, two_agent = await owner("Two Cohorts", "twoc")
    unco, _ = await owner("Uncohorted", "unco")
    parked, _ = await owner("Parked", "parked", status="inactive")
    agentless = await factories.make_user(db_session, name="Agentless", email="na@example.org")
    delegate = await factories.make_user(
        db_session, name="Delegate Only", email="del@example.org"
    )
    db_session.add(AgentDelegate(agent_registry_id=two_agent.id, user_id=delegate.id))

    # beta inserted first, so name order differs from insertion order.
    beta = Cohort(name="beta-c", created_by=admin.id)
    alpha = Cohort(name="alpha-c", created_by=admin.id)
    db_session.add_all([beta, alpha])
    await db_session.flush()
    for cohort, slug in ((beta, "twoc"), (alpha, "twoc"), (alpha, "parked")):
        db_session.add(
            CohortMembership(cohort_id=cohort.id, agent_id=slug, added_by=admin.id)
        )
    await db_session.flush()
    return {
        "two": two, "two_agent": two_agent, "unco": unco, "parked": parked,
        "agentless": agentless, "delegate": delegate, "alpha": alpha, "beta": beta,
    }


# --- /admin/users column -----------------------------------------------------


async def test_the_column_renders_with_its_header(client, admin, world):
    r = await client.get("/admin/users", headers=_auth(admin.id))
    assert r.status_code == 200
    # Assert on the table's own <thead>: the admin sub-nav already renders
    # ">Cohorts<" on every admin page (base.html), so a page-wide check proves nothing.
    thead = re.search(r"<thead\b.*?</thead>", r.text, re.S).group(0)
    ths = re.findall(r"<th\b[^>]*>([^<]*)</th>", thead)
    assert len(ths) == _COLUMN_COUNT and ths[_COHORTS] == "Cohorts"
    _cohorts_cell(r.text, "Two Cohorts")  # asserts the 12-cell row shape


async def test_cohorts_are_listed_in_name_order_and_link_above_the_row_overlay(
    client, admin, world
):
    r = await client.get("/admin/users", headers=_auth(admin.id))
    cell = _cohorts_cell(r.text, "Two Cohorts")
    assert cell.index("alpha-c") < cell.index("beta-c")
    for c in (world["alpha"], world["beta"]):
        # relative z-10 lifts the link above the row's covering ::after link.
        assert f'<a href="/admin/cohorts/{c.id}" class="relative z-10">' in cell


async def test_active_badges_are_brand_and_parked_badges_are_muted(client, admin, world):
    r = await client.get("/admin/users", headers=_auth(admin.id))
    active = _cohorts_cell(r.text, "Two Cohorts")
    parked = _cohorts_cell(r.text, "Parked")
    assert "bg-indigo-100 text-indigo-700" in active
    assert "alpha-c" in parked
    assert "bg-gray-100 text-gray-700" in parked
    assert "bg-indigo-100" not in parked


async def test_no_agent_and_no_cohort_render_differently(client, admin, world):
    r = await client.get("/admin/users", headers=_auth(admin.id))
    assert "No agent" in _cohorts_cell(r.text, "Agentless")
    unco = _cohorts_cell(r.text, "Uncohorted")
    assert "—" in unco and "No agent" not in unco


async def test_a_delegate_does_not_inherit_the_agents_cohorts(client, admin, world):
    r = await client.get("/admin/users", headers=_auth(admin.id))
    cell = _cohorts_cell(r.text, "Delegate Only")
    assert "No agent" in cell
    assert "alpha-c" not in cell and "beta-c" not in cell


# --- /admin/users cohort_filter ----------------------------------------------


async def test_cohort_filter_narrows_to_the_cohorts_members(client, admin, world):
    unfiltered = await client.get("/admin/users", headers=_auth(admin.id))
    assert {"Two Cohorts", "Uncohorted", "Parked", "Agentless", "Delegate Only"} <= set(
        _rendered_names(unfiltered.text)
    )

    r = await client.get(
        f"/admin/users?cohort_filter={world['alpha'].id}", headers=_auth(admin.id)
    )
    assert r.status_code == 200
    assert set(_rendered_names(r.text)) == {"Two Cohorts", "Parked"}

    r = await client.get(
        f"/admin/users?cohort_filter={world['beta'].id}", headers=_auth(admin.id)
    )
    assert set(_rendered_names(r.text)) == {"Two Cohorts"}


async def test_the_sentinels_split_no_cohort_from_no_agent(client, admin, world):
    r = await client.get("/admin/users?cohort_filter=__none__", headers=_auth(admin.id))
    names = set(_rendered_names(r.text))
    assert "Uncohorted" in names
    assert not names & {"Two Cohorts", "Parked", "Agentless", "Delegate Only", "Site Admin"}

    r = await client.get("/admin/users?cohort_filter=__no_agent__", headers=_auth(admin.id))
    names = set(_rendered_names(r.text))
    assert {"Agentless", "Delegate Only", "Site Admin"} <= names
    assert not names & {"Two Cohorts", "Uncohorted", "Parked"}


async def test_a_cohort_named_none_is_filtered_by_id_not_by_name(
    client, db_session, admin, world
):
    none = Cohort(name="none", created_by=admin.id)
    db_session.add(none)
    await db_session.flush()
    db_session.add(CohortMembership(cohort_id=none.id, agent_id="unco", added_by=admin.id))
    await db_session.flush()

    r = await client.get(f"/admin/users?cohort_filter={none.id}", headers=_auth(admin.id))
    assert set(_rendered_names(r.text)) == {"Uncohorted"}
    r = await client.get("/admin/users?cohort_filter=none", headers=_auth(admin.id))
    assert _rendered_names(r.text) == []


@pytest.mark.parametrize("value", ["not-a-uuid", str(uuid.uuid4()), "alpha-c"])
async def test_an_unknown_cohort_filter_matches_nothing_rather_than_500ing(
    client, admin, world, value
):
    r = await client.get(f"/admin/users?cohort_filter={value}", headers=_auth(admin.id))
    assert r.status_code == 200
    assert _rendered_names(r.text) == []
    assert "No users found" in r.text
    assert f'colspan="{_COLUMN_COUNT}"' in r.text


async def test_cohort_filter_preselects_its_own_option(client, admin, world):
    alpha, beta = world["alpha"].id, world["beta"].id
    r = await client.get(f"/admin/users?cohort_filter={alpha}", headers=_auth(admin.id))
    assert re.search(rf'<option value="{alpha}"[^>]*\bselected\b', r.text)
    assert not re.search(rf'<option value="{beta}"[^>]*\bselected\b', r.text)

    r = await client.get("/admin/users?cohort_filter=__none__", headers=_auth(admin.id))
    assert re.search(r'<option value="__none__"[^>]*\bselected\b', r.text)


async def test_cohort_filter_composes_with_the_access_filter(client, db_session, admin, world):
    world["parked"].access_status = "pending"
    # Pending but outside alpha: only cohort_filter can drop it, so the test fails if
    # the route ignores cohort_filter (and fails on "Two Cohorts" if it ignores access).
    world["unco"].access_status = "pending"
    await db_session.flush()
    r = await client.get(
        f"/admin/users?cohort_filter={world['alpha'].id}&access_filter=pending",
        headers=_auth(admin.id),
    )
    assert set(_rendered_names(r.text)) == {"Parked"}


# --- /admin/users/{id} ---------------------------------------------------------


async def test_detail_shows_the_owned_agent_and_links_each_cohort(client, admin, world):
    r = await client.get(f"/admin/users/{world['two'].id}", headers=_auth(admin.id))
    assert r.status_code == 200
    assert f'href="/admin/agents/{world["two_agent"].id}"' in r.text
    assert "TwocBot" in r.text
    assert "(active)" in r.text
    dd = _cohorts_dd(r.text)
    assert dd.index("alpha-c") < dd.index("beta-c")
    for c in (world["alpha"], world["beta"]):
        assert f'href="/admin/cohorts/{c.id}"' in dd


@pytest.mark.parametrize("who", ["agentless", "delegate"])
async def test_detail_without_an_owned_agent_says_cohorts_are_per_agent(
    client, admin, world, who
):
    r = await client.get(f"/admin/users/{world[who].id}", headers=_auth(admin.id))
    assert r.status_code == 200
    dd = _cohorts_dd(r.text)
    assert "No agent — cohorts are assigned per agent" in dd
    assert "alpha-c" not in dd


async def test_detail_uncohorted_agent_is_unrestricted_under_the_open_policy(
    client, admin, world, monkeypatch
):
    monkeypatch.setattr(get_settings(), "cohort_isolation_enabled", True)
    monkeypatch.setattr(get_settings(), "cohort_default_policy", "open")
    r = await client.get(f"/admin/users/{world['unco'].id}", headers=_auth(admin.id))
    assert "None — unrestricted" in _cohorts_dd(r.text)


async def test_detail_uncohorted_agent_is_isolated_under_the_isolated_policy(
    client, admin, world, monkeypatch
):
    # twoc is an active cohorted agent, so the isolated-policy preflight passes.
    monkeypatch.setattr(get_settings(), "cohort_isolation_enabled", True)
    monkeypatch.setattr(get_settings(), "cohort_default_policy", "isolated")
    r = await client.get(f"/admin/users/{world['unco'].id}", headers=_auth(admin.id))
    assert "None — isolated" in _cohorts_dd(r.text)


async def test_detail_with_isolation_off_says_recorded_not_enforced(
    client, admin, world, monkeypatch
):
    monkeypatch.setattr(get_settings(), "cohort_isolation_enabled", False)
    r = await client.get(f"/admin/users/{world['two'].id}", headers=_auth(admin.id))
    assert "Recorded, not enforced: cohort isolation is off" in _cohorts_dd(r.text)
    r = await client.get(f"/admin/users/{world['unco'].id}", headers=_auth(admin.id))
    assert "None (cohort isolation is off)" in _cohorts_dd(r.text)


async def test_detail_parked_agents_cohorts_are_marked_not_in_effect(
    client, admin, world, monkeypatch
):
    monkeypatch.setattr(get_settings(), "cohort_isolation_enabled", True)
    r = await client.get(f"/admin/users/{world['parked'].id}", headers=_auth(admin.id))
    dd = _cohorts_dd(r.text)
    assert "alpha-c" in dd
    assert "bg-gray-100 text-gray-700" in dd
    assert "Recorded, not in effect while the agent is inactive" in dd


async def test_detail_non_active_uncohorted_agent_reads_plain_none(
    client, db_session, admin, world, monkeypatch
):
    # A non-active agent is absent from the gate preview: no unrestricted/isolated claim.
    monkeypatch.setattr(get_settings(), "cohort_isolation_enabled", True)
    idle = await factories.make_user(db_session, name="Idle", email="idle@example.org")
    await factories.make_agent(
        db_session, user=idle, agent_id="idle", bot_name="IdleBot",
        pi_name="Idle", status="inactive",
    )
    await db_session.flush()
    r = await client.get(f"/admin/users/{idle.id}", headers=_auth(admin.id))
    dd = _cohorts_dd(r.text)
    assert "None" in dd
    for wrong in ("unrestricted", "isolated", "isolation is off"):
        assert wrong not in dd


async def test_detail_enforced_membership_carries_no_caveat(client, admin, world, monkeypatch):
    monkeypatch.setattr(get_settings(), "cohort_isolation_enabled", True)
    r = await client.get(f"/admin/users/{world['two'].id}", headers=_auth(admin.id))
    dd = _cohorts_dd(r.text)
    assert "bg-indigo-100 text-indigo-700" in dd
    assert "Recorded" not in dd
```

### P3 — stale wording and the dashboard spec

- [ ] **1. `cohorts.json` `_comment`:** replace the clause
  `cohort_isolation_enabled is False: these memberships are RECORDED, not enforced.` with
  `Seeding never changes cohort_isolation_enabled (default False); whether these memberships are enforced is that setting -- /admin/cohorts shows what is in force.`
  Touch nothing else in the file (`tests/unit/test_cohort_seed.py:44-81` pins its members).
- [ ] **2. `scripts/seed_cohorts.py:139`:**
  `print("The interaction gate is unchanged (isolation stays off).")` →
  `print("The interaction gate setting is unchanged (seeding never toggles cohort_isolation_enabled).")`.
- [ ] **3. `scripts/seed_cohorts.py:14-15` docstring:** replace
  `restarted. Enabling it before every agent in the roster has a cohort assigned`
  `would gate nothing.`
  with
  `restarted. Under the default policy ("open") an agent with no cohort stays`
  `unrestricted in both directions, so enabling it gates only agents that sit in`
  `different cohorts from each other.`
  (`compute_gates`, `src/services/cohorts.py:155-171`.) Leave the rest of the docstring.
- [ ] **4. `specs/admin-dashboard.md`:** in §1 Columns, after "Agent status", add
  `- Cohorts: the owned agent's cohorts (display-only; muted when the agent is not active; "No agent" / "—")`;
  in §1 Filters add `- Cohort (a cohort, "Agent, no cohort", or "No agent")`; in §2 Account add
  `- Agent (link + status) and Cohorts, with whether the membership is in effect`.
  Do not fix the spec's older omissions (Access, Last Login) here.

## 4. Integrated verification (after P1–P3 merge; run on the host)

Prefix: `ssh <prod-host> 'cd ~/copi-python && …'`.

1. `scripts/build-css.sh`
2. Narrow:
   `.venv-test/bin/python -m pytest tests/integration/test_admin_user_cohorts.py tests/integration/test_admin_users.py tests/unit/test_admin_users_profile_status.py tests/unit/test_reachability.py tests/characterization/test_auth_and_admin_routes.py tests/unit/test_cohort_seed.py tests/integration/test_seed_cohorts_script.py tests/unit/test_static_assets.py -q`
3. Responsive, the touched pages at all four widths:
   `.venv-test/bin/python -m pytest tests/responsive -q -k "admin_users or admin_user_detail or covering_row"`
4. Full gate: `./scripts/ci.sh` — compare against the §2.3 baseline; only baseline failures may remain.

Done means: every §6 criterion holds, steps 2–3 pass, and step 4 shows no failure absent from
the baseline.

## 5. Audit (after §4 is green)

- `lathe:plan-auditor`: this document vs the merged diff — every checkbox, every contract string.
- `lathe:semantic-reviewer` on the diff (diff text in the prompt): wording vs `compute_gates`
  truth table; Optional/eager-loading; filter semantics for pending/inactive/agentless users.

## 6. Acceptance criteria

1. `/admin/users` has a Cohorts column (md+) listing the owned agent's cohorts in name order as
   links to `/admin/cohorts/{id}`; muted for non-active agents; "No agent" vs "—" distinct;
   delegates show "No agent".
2. `cohort_filter` accepts cohort UUIDs and the two sentinels; unknown values render the empty
   state with `colspan="12"`, never a 500; the select preselects its value and composes with the
   other filters.
3. `/admin/users/{id}` shows the owned agent (link + status) and cohorts, with the §3 P1.6
   wording for isolation-off, non-active agent, unrestricted, and isolated.
4. No change to membership data, gate computation, engine, migrations, or PI-facing views.
5. `cohorts.json` `_comment` and the seed script's closing line no longer assert isolation is
   off; the seed docstring states the open-policy gate correctly; `specs/admin-dashboard.md`
   describes the new column, filter and Account rows.

## 7. Ship (each step needs the user's go-ahead)

1. Commit on `admin-user-cohort-labels`; push; open a PR (repo is public).
2. After merge, put the host tree back on `main` first — `redeploy.sh` builds from this
   checkout: `git switch main && git pull --ff-only`, and confirm `git log -1` is the merge
   commit. Then `./scripts/redeploy.sh -f docker-compose.prod.yml -f docker-compose.override.yml`.
   No migration. No `agent` rebuild/restart needed (the agent process does not serve admin
   routes). Confirm `docker inspect copi-python-app-1 -f '{{.HostConfig.RestartPolicy.Name}}'`
   = `unless-stopped`.
3. Smoke: as an admin, `/admin/users` shows a 3-cohort agent's three badges; filter by
   `scripps-investigators` returns its members; one agentless user's detail page shows
   "No agent — cohorts are assigned per agent".
4. Rollback: revert the merge commit and redeploy. No data to restore.

## 8. Out of scope (separate changes)

- **Agent slug rename orphans memberships** (pre-existing bug): `admin_approve_agent` rewrites
  `agent.agent_id` (`admin.py:975`, editable at `agent_detail.html:72`) without touching
  `cohort_memberships`; labels vanish and the old slug stays a phantom allowed sender. Fix by
  refusing a slug change while memberships exist, or moving them with audit rows.
- Assigning cohorts from the user page; labels for users without an agent.
- `applyFilter()` drops the server-supported `institution_filter`.
- `admin_cohort_add_agent` duplicate-check/insert race (unique violation → 500).
- Engine topology-snapshot signature misses membership moves (`simulation.py:7588-7617`).
- `admin_cohort_detail`'s agent→cohort map is unordered (`admin.py:1798-1804`).

## 9. Rev 3 — post-implementation audit corrections (binding over §3 where they differ)

Executed on branch `admin-user-cohort-labels`. Audits after integration: `lathe:plan-auditor`
(23/27 items verified, 4 evidence gaps), `lathe:semantic-reviewer` (6 findings),
`lathe:security-reviewer` (none; Jinja autoescape confirmed on for both templates).

Applied:
- **Non-active agent wording was false.** `compute_gates` builds cohort-mate sets from every
  membership row, so a non-running agent still sits in its active mates' allowed senders.
  "Recorded, not in effect while the agent is {status}" →
  "This agent is {status} and does not run". (A re-review rejected an added clause
  "cohort-mates still accept its posts": false when no cohort-mate is active.)
- **Preflight refusal was invisible.** When `gate.preflight_error` is set (policy `isolated`,
  no active cohorted agent) the page now says "Recorded, not enforced: isolation was forced off"
  (cohorted) / "None (isolation was forced off: details)" (uncohorted), linking `/admin/cohorts`,
  instead of "unrestricted".
- `<label for="cohort-filter">` on the new filter.
- Tests: parked-agent assertion updated (and policy pinned `open`);
  `test_detail_says_when_the_isolated_policy_was_forced_off`;
  `test_cohort_filter_composes_with_the_claimed_filter`.
- §4.3 ran off-host: local venv from `requirements.lock` + dev deps, Playwright 1.62.0 against the
  cached `chromium_headless_shell-1234`, testcontainers Postgres, on a snapshot whose 9 changed
  files (incl. `static/css/app.min.css` from the host build) were sha256-identical to the host
  working tree. Full `tests/responsive` (156) plus a scratch-only journey driving
  `applyFilter()` through the real `<select>` (keeps `access_filter`, reselects, sentinel, badge
  link click) — 157 passed.

Not applied (recorded):
- Gate wording uses the app process's settings; a long-running `agent-run` started before a
  `.env` change could differ. Same as the existing `/admin/cohorts` banner, which documents it.
- Under `python -m src.agent.main --all-agents` the engine runs every status, while the page
  (like `/admin/cohorts`) assumes the default active-only roster. Not used by the prod runbook.
- Uppercase/braced UUIDs in `cohort_filter` match nothing — URLs come from the page's own select.
- `_cohort_gate_context` also reads the latest topology snapshot, unused on this page — one
  indexed row on a rarely viewed admin page.
