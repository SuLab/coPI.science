# Red-team audit — Part 22 (issue #22: profile pipeline & write integrity), MASTER_PLAN.md:7645-10531

Audited read-only against `/home/a/scripps/coPI.science` @ `copi-prod` 18ba52c on 2026-09-02.
Everything DB-free was **run** in a scratch copy of `src/` + `tests/`
(`.../scratchpad/rt22/work`) with `/home/a/scripps/coPI.science/.venv-test/bin/python`:
each task's Step-1 test against the pre-fix module, then against the module carrying that
task's exact fix, plus `ruff check` on every new file. Installed versions confirmed:
alembic 1.18.5, SQLAlchemy 2.0.51, FastAPI 0.139.2, pytest `asyncio_mode = "auto"`,
ruff `select = E,F,I,UP,B`, `ignore = ["E501"]`, `target-version = "py311"`.

---

## 1. Verdicts

| Task | Verdict | One line |
|---|---|---|
| 22.1 ORCID `_get()` | **OK** | Anchors byte-exact; ran it: 4 new tests fail pre-fix (`AttributeError`), 16 pass post-fix, ruff clean. |
| 22.2 migration 0025 | **BLOCKER** | Step-1 seed `INSERT INTO users` omits 3 NOT-NULL-no-default columns → the test can never pass; also duplicates M.1's preflight edits and DELETEs rows with no record. |
| 22.3 head pins | **BLOCKER** | Body still writes `HEAD_REVISION` into prod migration tooling; reconciliation item 1 makes this task verify-only. Grep "expect exactly" list is also wrong. |
| 22.4 in-run PMID dedup | **needs-fix (MAJOR)** | Step-1 test re-implements the loop inline — it passes on pre-fix code and can never fail; unused import fails the ruff gate. |
| 22.5 PubMed `itertext()` | **OK** | Ran it: red (`assert 'Role of ' == 'Role of TP53 in cancer'`) → green (13 passed); remaining `.text` reads justified. |
| 22.6 `_validate_profile` | **OK** | Ran it: 6 fail pre-fix → 10 pass post-fix; `_VALID_PROFILE` confirmed **125 words** and its own comment already says 100-350; no test asserts the "150-250" text. |
| 22.7 `Form(None)` | **BLOCKER** | FastAPI maps an **empty** form value to the field default, so `Form(None)` also swallows a *cleared* field → clearing a profile field through the UI silently no-ops. Plus the agent_page test double-creates a profile row. |
| 22.8 reset `synthesis_validated` | **needs-fix (BLOCKER-by-inheritance)** | Fix itself is right and additive; its agent_page test hits the same `world.pi` duplicate-profile violation. |
| 22.9 dead `pending_profile` reader | **needs-fix (MAJOR)** | Test verified red; but `specs/admin-dashboard.md:23` is left advertising the now-unreachable `pending_update`, and 2 of 3 spec anchors are off by 1-2 lines. |
| 22.10 `atomic_write.py` | **needs-fix (MAJOR)** | Module + 4 tests verified green; `mkstemp` yields **0600**, so every rewritten profile drops from 0664 → 0600 (measured) and the Deploy note's umask claim is false; the new "first private save" test is vacuous. |
| 22.11 private-seed export | **needs-fix (MAJOR)** | 3 unit tests verified green; the GM test is a placeholder that names a fixture (`export_dirs`) and a variable (`agent`) that do not exist in that file — **no GM test creates an AgentRegistry at all**, so the export path is dead there. |
| 22.12 shared `apply_synthesis` | **needs-fix (MAJOR)** | 8 unit tests verified green and the outer-gate case analysis holds; but `return 1` is wrong in a `-> bool` function, and two script snippets are indented 4 spaces short of the real code. |
| 22.13 atomic `profile_version` | **needs-fix (MAJOR)** | Statement compiles and behaves (verified); its unit test has 2 unused imports → **ruff gate red**, and it only greps the source string, so it cannot detect a wrong statement. |
| 22.14 atomic `delegate_slack_ids` | **needs-fix (MAJOR)** | All three compiled-SQL assertions verified **exactly** as written; but the `invite.py` snippet silently rewrites the existing `except` handler and deletes a comment documenting a real past bug. |

Coverage (G): all **42/42** row ids in `findings/issue_22.md` + `_redteam.md`
(`C1-a..c`, `DoD`, `V1-15a..g`, `V1-16a..g`, `V1-pm1..4`, `V1-val1..3`, `V6-22a..c`,
`V6-22-gate/flag`, `V6-23`, `V6-24a..f`, `V6-form1..4`, `V6-pend`) appear in the coverage
matrix with a task or a reasoned exclusion. No wrongly-excluded item found; the
`grantbot.py`/`foa_cache.py` write sites the plan omits are explicitly "out of scope" in
`V6-24b` itself, so that exclusion is legitimate (it is just never *stated* in the task).

---

## 2. Findings

### BLOCKER-1 (22.2, Step 1) — the migration test's seed INSERT violates three NOT NULL columns

`users.is_admin`, `users.email_notifications_enabled` and `users.onboarding_complete` were
created by `0001_initial.py:33-35` as `sa.Column(..., default=False, nullable=False)`.
`default=` is a **client-side** default; alembic emits no DDL DEFAULT. Proved by compiling
the same DDL:

```
CREATE TABLE users (... is_admin BOOLEAN NOT NULL, email_notifications_enabled BOOLEAN NOT NULL,
                    onboarding_complete BOOLEAN NOT NULL, created_at ... DEFAULT now() NOT NULL, ...)
```

`access_status` is also NOT NULL with its server default deliberately dropped
(`0010:48`) — the plan supplies that one. So the raw insert dies with
`NotNullViolationError: null value in column "is_admin"` **both before and after** the
fix: Step 2 fails for the wrong reason and Step 4 can never pass.

Corrected Step-1 seed (replaces the `INSERT INTO users` block):

```python
              await conn.execute(
                  text(
                      # is_admin / email_notifications_enabled / onboarding_complete are
                      # NOT NULL with NO server default (0001_initial used a client-side
                      # `default=`, which emits no DDL DEFAULT), so a raw INSERT must
                      # name them. access_status is NOT NULL too — 0010 drops its default.
                      "INSERT INTO users (id, name, orcid, access_status, is_admin, "
                      "email_notifications_enabled, onboarding_complete) "
                      "VALUES (:id, 'Dup User', :orcid, 'allowed', false, true, true)"
                  ),
                  {"id": user_id, "orcid": f"0000-0000-0000-{uuid.uuid4().hex[:4]}"},
              )
```

`publications` needs no change: only `user_id`/`title` are NOT NULL-without-default and
both are supplied; `created_at` has `DEFAULT now()`.

**What IS correct in 22.2 (verified, do not "fix"):**
- The scratch-database approach is the right answer to "the `engine` fixture is already at
  head": it never walks the shared session DB back to 0024. `pg_url` is session-scoped and
  does **not** depend on `_migrated`, so requesting it does not migrate anything
  (`tests/conftest.py:48-58`). `isolation_level="AUTOCOMMIT"` is required for
  `CREATE DATABASE` and is supplied. An async-generator fixture declared with plain
  `@pytest.fixture` works because `asyncio_mode = "auto"`.
- The dedup SQL keeps **exactly one** row per `(user_id, pmid)` (`p.id > p2.id` leaves only
  the minimum) and **cannot** touch `pmid IS NULL` rows — both because of the explicit
  `p.pmid IS NOT NULL` and because `NULL = NULL` never matches.
- `id` is `postgresql.UUID`; PG's `uuid_cmp` is a memcmp of the 16 bytes and Python's
  `UUID.__lt__` compares `self.int == int.from_bytes(bytes, "big")` — identical orderings,
  so `sorted([uuid4(), uuid4()])` really does predict which row survives.
- `down_revision = "0024"` ✓. `op.drop_constraint(..., type_="unique", if_exists=True)` is
  valid on alembic 1.18.5 (`drop_constraint(constraint_name, table_name, type_=None, *, schema=None, if_exists=None)`)
  and matches the 0022/0023/0024 convention.
- The drift guard's regex `create_unique_constraint\(\s*\n?\s*"([^"]+)"`
  (`tests/unit/test_migration_checks.py:837`) does match the plan's multi-line call.
- No existing test can violate the new constraint: the only two same-`(user_id, pmid)`
  `Publication` objects in the suite (`test_onboarding_flow.py:1062,1077`) are transient —
  never `db_session.add`ed.

### BLOCKER-2 (22.3) — the task body contradicts the binding reconciliation and poisons prod tooling

Reconciliation item 1 (MASTER_PLAN.md:56-63): `HEAD_REVISION = 0028`, M.1 is the sole
editor of `preflight.py`, `postflight.py`, `run_migration.sh:56`,
`test_harness_smoke.py:15` and the head pins in `test_migration_checks.py`, and
"**Task 22.3 is therefore verify-only (run its grep, change nothing)**". The task body
still instructs the executor to write the literal `HEAD_REVISION` into
`scripts/migrate/preflight.py:74` (`DEFAULT_TARGET`) and `run_migration.sh:56` (`TARGET`),
commit it, and accept a red tree ("Step 2: this WILL fail"). `DEFAULT_TARGET`/`TARGET` are
what the production runbook actually migrates to. Replace the whole task with:

```markdown
### Task 22.3: VERIFY-ONLY — the alembic head pins are owned by M.1

Per cross-part reconciliation item 1 the final head is `0028` and **M.1 is the only task
that edits any head pin**. This task edits nothing; it records the pre-M.1 state so M.1's
diff can be checked. Do NOT introduce a `HEAD_REVISION` placeholder anywhere.

- [ ] `grep -n '"0024"' tests/integration/test_harness_smoke.py scripts/migrate/preflight.py \
        scripts/migrate/run_migration.sh tests/unit/test_migration_checks.py`
      → expect exactly these ten hits, classified:
        test_harness_smoke.py:15             head pin                → M.1 bumps to 0028
        preflight.py:74                      DEFAULT_TARGET          → M.1 bumps to 0028
        preflight.py:203                     0024's PlannedObject    → EXTENDED (never bumped) by 22.2/M.1
        preflight.py:206                     REVISION_ORDER          → EXTENDED (never bumped) by 22.2/M.1
        run_migration.sh:56                  TARGET                  → M.1 bumps to 0028
        test_migration_checks.py:223         permanent fact ("0024 blocks against target 0023") → NEVER touch
        test_migration_checks.py:232         DEFAULT_TARGET pin      → M.1
        test_migration_checks.py:838         drift-guard revision loop → EXTENDED (never bumped) by 22.2
        test_migration_checks.py:1129,:1170  argparse default pins   → M.1
- [ ] No edits, no commit.
```

(The plan's current "expect exactly: …:15, :74, :56, :232, :1129, :1170" is measurably
wrong — the real grep also returns `preflight.py:203`, `preflight.py:206`,
`test_migration_checks.py:223` and `:838`. An executor comparing output to that list
would conclude the tree had drifted.)

Also note the deploy-blocking gap 22.3 does not mention (M.1 does cover it — verify it
survives): `SUPPORTED_START_REVISIONS = ("0018","0019","0020","0021","0023")`
(`preflight.py:89`) does **not** contain `0024`, and production is stamped 0024, so
preflight BLOCKS the real migration until M.1 adds it.

### BLOCKER-3 (22.7) — `Form(None)` cannot distinguish "empty" from "omitted"; the fix breaks clearing a field

FastAPI treats an **empty string** form value as absent and substitutes the field default
(`fastapi/dependencies/utils.py:765-780`):

```python
    if (value is None
        or (isinstance(field.field_info, params.Form) and isinstance(value, str) and value == "")
        or (field_annotation_is_sequence(...) and len(value) == 0)):
        ... return deepcopy(field.default)
```

Measured on the installed FastAPI (0.139.2) with `inst: str | None = Form(None)`:

```
POST inst=""      -> inst is None      # indistinguishable from omitted
POST (no inst)    -> inst is None
```

So after this task, `if research_summary is not None:` is False whenever the user
**cleared** the box. This is the normal UI path, not a crafted one:
`templates/profile/edit.html:227` — `hiddenInput.value = values.join(', ')` — posts `""`
for a tag field whose last pill was removed, the Research Summary is a `<textarea>` that
posts `""` when emptied, and `institution`/`department` are `<input>`s that post `""` when
cleared (today `Form("")` + `institution or None` correctly nulls them; `V6-form4`'s own
evidence line says so). The plan's claim "the fix below changes nothing on that path" is
false — it silently converts every "clear this field" into a no-op.

Corrected implementation — keep `Form("")` (so the value is still the submitted string)
and gate on **presence** in the parsed form. Verified working alongside `Form(...)` params
(Starlette caches the parsed body, so `await request.form()` in the handler is free):

```
POST {"name","inst":"","tech":"a,b"} -> keys ['inst','name','tech'], "inst" in form -> True,  inst == ''
POST {"name"}                        -> keys ['name'],               "inst" in form -> False, inst == ''
```

`src/routers/profile.py::profile_save` (signature unchanged from today):

```python
    """Save profile changes."""
    # Which fields the client actually SENT. FastAPI maps an empty Form value to
    # the parameter default, so `Form(None)` cannot tell "the user cleared this
    # box" (must write "") from "the field was not in the POST at all" (must
    # leave the stored value alone). The raw form can. See issue #22 COR-22.
    form = await request.form()

    # ... (email validation block unchanged) ...

    # Update user fields
    if name:
        current_user.name = name
    if "institution" in form:
        current_user.institution = institution or None
    if "department" in form:
        current_user.department = department or None

    # ... profile lookup / create unchanged ...

    if "research_summary" in form:
        profile.research_summary = research_summary
    if "techniques" in form:
        profile.techniques = _parse_list(techniques)
    if "experimental_models" in form:
        profile.experimental_models = _parse_list(experimental_models)
    if "disease_areas" in form:
        profile.disease_areas = _parse_list(disease_areas)
    if "key_targets" in form:
        profile.key_targets = _parse_list(key_targets)
    if "keywords" in form:
        profile.keywords = _parse_list(keywords)
```

Apply the identical shape to `onboarding.py::save_profile` (six fields, no
institution/department) and `agent_page.py::save_public_profile` (six fields). All three
already take `request: Request`. Then Task 22.8's one-line
`profile.synthesis_validated = None` and 22.13's version bump sit exactly where the plan
puts them.

Add the control the naive fix would have broken (to whichever file hosts each route's
tests):

```python
async def test_profile_save_still_clears_a_field_the_user_emptied(client, db_session):
    """The other half of V6-form1..4: an EMPTY value is a deliberate clear and must
    still be written. FastAPI maps "" to the parameter default, so a value-based
    guard (`Form(None)` + `is not None`) would silently ignore it."""
    u = await factories.make_user(db_session, name="Clr", email="clr@example.org",
                                  institution="Old Institute")
    await factories.make_profile(db_session, user=u, research_summary="old", techniques=["t"])
    await db_session.flush()

    r = await client.post(
        "/profile/save", headers=_auth(u.id),
        data={"name": "Clr", "email": "clr@example.org", "institution": "",
              "research_summary": "", "techniques": ""},
    )
    assert r.status_code == 302
    assert (await _user_row(db_session, u.id))["institution"] is None
    prof = await _prof(db_session, u.id)
    assert prof["research_summary"] == ""
    assert prof["techniques"] == []
```

### BLOCKER-4 (22.7 + 22.8, and 22.10's neighbour test) — `world.pi` already has a ResearcherProfile

`tests/integration/test_agent_page.py:201-207`, the `world` fixture, ends with
`await factories.make_profile(db_session, user=pi)`. `researcher_profiles.user_id` is
`unique=True` (`src/models/profile.py:19-21`). So every plan test that does
`await factories.make_profile(db_session, user=world.pi, ...)` — 22.7's
`test_save_public_profile_partial_post_does_not_blank_omitted_fields` and 22.8's
`test_save_public_profile_resets_synthesis_validated` — raises
`IntegrityError: duplicate key value violates unique constraint` at `flush()`.

Corrected setup for both (mutate the row `world` already made):

```python
    profile = (await db_session.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == world.pi.id)
    )).scalar_one()
    profile.research_summary = "old summary"
    profile.techniques = ["old-t"]
    profile.synthesis_validated = False        # 22.8's test only
    await db_session.flush()
```

The same fixture fact invalidates 22.10's new test (see MAJOR-6).

---

### MAJOR-1 (22.4) — the Step-1 test cannot fail, and it fails the ruff gate

The test builds `pmids`/`seen_pmids` **inside the test body** and asserts on its own local
variable; it never calls anything in `profile_pipeline`. Measured: it passes on unmodified
`copi-prod` code, so it violates the plan's own global constraint ("every behaviour change
ships a test that fails on the pre-fix code"). It also carries two defects that the gate
does catch:

```
F401 [*] `src.services.profile_pipeline` imported but unused --> tests/unit/test_profile_pipeline_dedup.py:5
PytestWarning: The test <Function test_pmids_list_is_deduplicated_across_orcid_works> is marked
with '@pytest.mark.asyncio' but it is not an async function.
```
`scripts/ci.sh:243` runs `ruff check tests/unit …` expecting **zero** findings → the gate
goes red at this commit.

Corrected: extract the loop so there is something to test. In
`src/services/profile_pipeline.py` (module level, near `_validate_profile`):

```python
def _dedup_pmids(orcid_works: list[dict[str, Any]]) -> tuple[list[str], set[str]]:
    """PMIDs from an ORCID works listing, first occurrence only (issue #22 COR-16).

    ORCID lists a work once per activities-summary source, so a paper linked to two
    co-author affiliations arrives here as the same PMID twice. Returns the ordered
    list and the seen-set, so the DOI->PMID resolution loop below can keep it up to
    date instead of appending a PMID the list already has.
    """
    pmids: list[str] = []
    seen: set[str] = set()
    for w in orcid_works:
        pmid = w.get("pmid")
        if pmid and pmid not in seen:
            seen.add(pmid)
            pmids.append(pmid)
    return pmids, seen
```

and at `profile_pipeline.py:115`: `pmids, seen_pmids = _dedup_pmids(orcid_works)`.
Test (fails pre-fix with `ImportError: cannot import name '_dedup_pmids'`, ruff-clean, no
asyncio mark):

```python
"""Unit tests for the in-run publication dedup fix (issue #22 COR-16)."""

from src.services.profile_pipeline import _dedup_pmids


def test_pmids_are_deduplicated_across_orcid_works_preserving_order():
    pmids, seen = _dedup_pmids(
        [{"pmid": "111"}, {"pmid": "111"}, {"pmid": "222"}, {"pmid": None}, {}]
    )
    assert pmids == ["111", "222"]
    assert seen == {"111", "222"}
```

The `existing_pubs` half of the fix (V1-16c — the one that actually turns into an
`IntegrityError` once 0025 exists) still has no test. Add this to
`tests/characterization/test_profile_pipeline_gm.py`, which is red pre-fix once 0025 is in
(pre-fix: two `db.add` → `IntegrityError`; the shared fakes return a fixed 2-record list
regardless of input, so the fetch stub must echo the requested PMIDs or nothing
duplicates):

```python
async def test_a_pmid_listed_twice_by_orcid_inserts_exactly_one_publication(
    db_session, monkeypatch
):
    """V1-16b/c: one ORCID works listing naming the same PMID twice must not
    db.add() two Publication rows (post-0025 that is an IntegrityError that
    aborts the whole pipeline run)."""
    _install_fakes(monkeypatch)

    async def dupe_works(orcid_id):
        return [{"pmid": "1001", "doi": None}, {"pmid": "1001", "doi": None}]

    async def echo_records(pmids):
        return [
            {"pmid": p, "doi": None, "title": "T", "abstract": "A", "journal": "J",
             "year": 1843, "pub_types": ["Journal Article"], "pmcid": None}
            for p in pmids
        ]

    monkeypatch.setattr(profile_pipeline, "fetch_orcid_works", dupe_works)
    monkeypatch.setattr(profile_pipeline, "fetch_pubmed_records", echo_records)

    user = await factories.make_user(db_session, name="Dupe Lovelace")
    await profile_pipeline.run_profile_pipeline(user.id, db_session)

    rows = (await db_session.execute(
        select(Publication).where(Publication.user_id == user.id, Publication.pmid == "1001")
    )).scalars().all()
    assert len(rows) == 1
```

### MAJOR-2 (22.13) — the unit test fails the ruff gate and cannot detect a wrong statement

Measured on the plan's file as written:

```
F401 [*] `uuid` imported but unused                         --> tests/unit/test_bump_profile_version_sql.py:3
F401 [*] `sqlalchemy.dialects.postgresql` imported but unused --> tests/unit/test_bump_profile_version_sql.py:5
Found 2 errors.
```

The task text says the test "compil[es] the statement (no DB needed)" and then only greps
`inspect.getsource` for the substrings `func.coalesce` and `returning` — which a docstring
would satisfy. Split out the statement (mirroring 22.14, which does this correctly) and
assert on real compiled SQL. Verified compilation on SQLAlchemy 2.0.51:

```
UPDATE researcher_profiles SET profile_version=(coalesce(researcher_profiles.profile_version, 0) + 1),
       updated_at=now() WHERE researcher_profiles.id = '<uuid>' RETURNING researcher_profiles.profile_version
```

```python
# src/services/profile_pipeline.py
def bump_profile_version_stmt(profile_id: uuid.UUID) -> Update:
    """UPDATE ... SET profile_version = COALESCE(profile_version, 0) + 1 RETURNING it."""
    return (
        update(ResearcherProfile)
        .where(ResearcherProfile.id == profile_id)
        .values(profile_version=func.coalesce(ResearcherProfile.profile_version, 0) + 1)
        .returning(ResearcherProfile.profile_version)
    )


async def bump_profile_version(db: AsyncSession, profile_id: uuid.UUID) -> int:
    """... (docstring as planned) ..."""
    return (await db.execute(bump_profile_version_stmt(profile_id))).scalar_one()
```
(`Update` comes from the same `from sqlalchemy import ...` line: `func, select, update, Update`.)

```python
"""Unit test: profile_version bump uses an atomic SQL-side increment (issue #22 C1)."""

import uuid

from sqlalchemy.dialects import postgresql

from src.services.profile_pipeline import bump_profile_version_stmt

PROFILE_ID = uuid.uuid4()


def test_bump_is_a_sql_side_coalesce_plus_one_with_returning():
    sql = str(
        bump_profile_version_stmt(PROFILE_ID).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "SET profile_version=(coalesce(researcher_profiles.profile_version, 0) + 1)" in sql
    assert f"WHERE researcher_profiles.id = '{PROFILE_ID}'" in sql
    assert sql.endswith("RETURNING researcher_profiles.profile_version")
```

**Verified safe (the drafter did not claim this, but it is the thing most likely to have
been wrong):** an ORM-enabled `update()` with an explicit `.returning()` and the default
`synchronize_session="auto"` does **not** raise. `bulk_persistence.py:1004-1016` resolves
`auto` → `evaluate` for a simple `id == :x` criteria, and
`_apply_update_set_values_to_objects` (`:1846-1857`) skips SET values it cannot evaluate in
Python and merely **expires** those attributes on the in-session object. The
`can_use_returning`/`use_supplemental_cols` conflict only exists for
`synchronize_session="fetch"`, which this WHERE never selects. `Session.execute()`
autoflushes first, so the pending field writes from 22.12 land before the UPDATE.

### MAJOR-3 (22.12) — `return 1` in a function whose contract is `bool`

`scripts/regen_profiles_from_web.py:55` is `async def regen_one(agent_id: str, context_path: Path) -> bool:`
and returns `False` on every failure path (`:59, :73, :81, :124`), with `_run` counting
failures from the falsy return. The plan's snippet for that file uses
`return 1` for the "validation gate kept the existing profile" path — `1` is truthy, so a
skipped resynthesis would be **reported as a success**. Corrected:

```python
    validated = _validate_profile(synthesized)
    applied = apply_synthesis(profile, synthesized, validated=validated)
    if not applied:
        print("  SKIPPED: validation gate kept the existing stored profile", flush=True)
        return False
```
(`scripts/regen_profile_from_cv.py::_run` really does return `int` — `return 1` is correct
*there*.)

### MAJOR-4 (22.12) — two script snippets are indented 4 spaces short of the real code

In `scripts/regen_profile_from_cv.py:103-120` and `scripts/regen_profiles_from_web.py:98-115`
the write block sits at **8 spaces** (inside `async with … as db:`); both plan snippets are
at 4. A literal paste is an `IndentationError`. `scripts/vet_publications.py` (12 spaces,
inside `try:`) and `scripts/resynth_from_current_pubs.py` (4 spaces) are correct as shown.
Same defect in 22.4's DOI-resolution snippet: the real `for w in doi_only_works:` is at 12
spaces (`if doi_only_works:` → `try:`), the snippet shows 8. Add "re-indent to match the
surrounding block" to each of these steps, or fix the snippets.

### MAJOR-5 (22.10) — `mkstemp` is 0600, so every atomic write tightens the file's mode

Measured:

```
umask-created target mode: 0o664
mkstemp mode:              0o600
after atomic replace:      0o600
```

The Deploy note's claim — "`tempfile.mkstemp` creates the temp file with the process's
normal umask" — is false; `mkstemp` always passes `0o600`. Practical prod impact is
limited (no `USER` in the Dockerfile and `profiles/` is `root:root`, and all four
containers bind-mount the same `./profiles`), but it silently changes the permissions of
every public profile, private profile and memory file the system rewrites, and it would
break the moment any container runs non-root. Corrected implementation + test:

```python
import os
import stat
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """... (docstring as planned) ..."""
    path = Path(path)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
        # mkstemp ALWAYS creates 0600 regardless of umask, so without this the
        # replace silently tightens every file it rewrites (measured: 0664 -> 0600).
        # Keep the mode the file already had; 0644 for a new one, which is what
        # Path.write_text produced under this project's umask.
        try:
            os.chmod(tmp_name, stat.S_IMODE(os.stat(path).st_mode))
        except FileNotFoundError:
            os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
```

```python
def test_the_targets_permissions_survive_the_replace(tmp_path):
    """mkstemp creates 0600; os.replace would carry that onto the target and
    quietly tighten every profile file the system rewrites."""
    p = tmp_path / "out.md"
    p.write_text("original\n", encoding="utf-8")
    p.chmod(0o664)
    atomic_write_text(p, "new\n")
    assert stat.S_IMODE(p.stat().st_mode) == 0o664
```

Everything else about the module is verified: the four planned tests pass as written
(`4 passed`), `os.replace` is same-filesystem because the temp file is created in
`path.parent` and `profiles/` is one bind mount in every container
(`docker-compose.prod.yml:40,72,96,125`), and the `.tmp` suffix keeps a leaked temp file
out of `grantbot.py:55`'s `PROFILES_DIR.glob("*.md")` — the only glob over that tree in
`src/`. `src/services/__init__.py` is empty, so importing `src.services.atomic_write` from
`src/agent/agent.py` (a container with no DB config) pulls in nothing but `os`/`tempfile`.

### MAJOR-6 (22.10, Step 5) — the "first private profile save" test passes without the fix

The test's premise ("world's PI has no ResearcherProfile row by construction") is wrong:
`world` creates one at `test_agent_page.py:207`, and `factories.make_profile` even sets
`private_profile_md="# Private\nStuff."`. So the test exercises the pre-existing
`if profile:` path and is green on unmodified code — the V6-24d fix ships untested.
`_agent_for` (`test_agent_page.py:193-198`) creates a user + agent and **no** profile;
use it:

```python
async def test_first_private_profile_save_creates_the_missing_profile_row(
    client, db_session, world
):
    """Before this fix, `if profile:` made a first-ever private save (no
    ResearcherProfile row yet) a silent, permanent disk-only write."""
    pi, _agent = await _agent_for(
        db_session, name="No Profile", email="noprof@example.org",
        agent_id="tstnoprof", bot_name="NoProfBot",
    )
    await db_session.flush()

    r = await client.post(
        "/agent/tstnoprof/profile/save",
        headers=_auth(pi.id),
        data={"content": "first ever private instructions"},
    )
    assert r.status_code == 302
    profile = (await db_session.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == pi.id)
    )).scalar_one()
    assert profile.private_profile_md == "first ever private instructions"
```

### MAJOR-7 (22.11, Step 1/Step 5) — the GM test is a placeholder and names things that do not exist

- `export_dirs` is defined **only** in `tests/integration/test_onboarding_flow.py:85-94`.
  There is no `tests/*/conftest.py` and `tests/conftest.py` does not define it, so
  `test_first_run_exports_the_private_seed_to_disk(db_session, export_dirs, monkeypatch)`
  errors with `fixture 'export_dirs' not found`.
- The snippet references an `agent` variable and leaves the body as
  `# ... set up a user + agent …` — a placeholder, not code (finding class F).
- `grep -n "make_agent\|agent_id" tests/characterization/test_profile_pipeline_gm.py`
  returns **nothing**: no GM test creates an `AgentRegistry`, so `agent_id` is always
  `None` there and both `export_profile_to_markdown` and the new
  `export_private_profile` return immediately. Step 5's "run the GM suite in full" is
  therefore not evidence for this task at all — and that file does **not** patch
  `PROFILES_DIR`/`PRIVATE_PROFILES_DIR`, so the first GM test that does create an agent
  will write into the repo's real `profiles/` (gitignored, hence invisible). A concrete,
  self-contained replacement:

```python
async def test_first_run_exports_the_private_seed_to_disk(db_session, monkeypatch, tmp_path):
    """COR-23: an admin-seeded lab whose PI never visits /onboarding/private-profile
    must still get agent instructions on disk from the pipeline's own seed write.

    This is the only test in this file with an AgentRegistry, so it is also the only
    one that reaches the export at all — hence the explicit export-dir patching (the
    rest of the file never writes, so it never needed it).
    """
    from src.services import profile_export

    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path / "public")
    monkeypatch.setattr(profile_export, "PRIVATE_PROFILES_DIR", tmp_path / "private")
    _install_fakes(monkeypatch)

    user = await factories.make_user(db_session, name="Ada Lovelace")
    agent = await factories.make_agent(
        db_session, user=user, agent_id="gmseed", bot_name="GmSeedBot"
    )
    await db_session.flush()

    profile = await profile_pipeline.run_profile_pipeline(user.id, db_session)

    assert profile.private_profile_md is None      # nothing promoted it yet
    assert profile.private_profile_seed            # the pipeline generated one
    written = (tmp_path / "private" / f"{agent.agent_id}.md").read_text(encoding="utf-8")
    assert written.strip() == _PRIVATE_SEED.strip()
```

The three unit tests in this task are fine — verified `3 passed` with the planned
`export_private_profile` body, and red pre-fix (`path is None`).

### MAJOR-8 (22.2) — preflight/test_migration_checks edits collide with M.1

Reconciliation item 1 names M.1 as the **only** editor of `scripts/migrate/preflight.py`
and of the head pins in `tests/unit/test_migration_checks.py`; `plan/part_M.md:17,95-98,124`
shows M.1 rewriting `PLANNED_OBJECTS` with `PlannedObject("0025", "constraint", "uq_publications_user_pmid", "publications")`
already in it and `REVISION_ORDER` through `0027`. Task 22.2 adds the same PlannedObject
line and a `REVISION_ORDER` ending at `0025`. M.1 runs after 22.2 (phase 3), so at best
M.1's rewrite supersedes it and at worst the executor appends and produces a duplicate
entry. Add one clarifying line to both tasks:

> Task 22.2 owns **only** the `PLANNED_OBJECTS` append for 0025 and the `REVISION_ORDER`
> extension to `"0025"`, plus the `test_migration_checks.py:838` drift-loop extension to
> `"0025"`. `DEFAULT_TARGET`, `SUPPORTED_START_REVISIONS`, the postflight `EXPECTED_*`
> tables and every head pin remain M.1's. **M.1: 0025's PlannedObject may already be
> present — replace the tuple, do not append a second copy.**

(Both orderings are gate-safe on their own: leaving the drift loop at `0024` simply means
0025 is not drift-checked yet, and adding `"0025"` to it passes because the regex does
match the planned `create_unique_constraint` call.)

### MAJOR-9 (22.2) — a data-destroying DELETE with no record and an unrecoverable downgrade

`upgrade()` deletes rows and `downgrade()` cannot restore them (the docstring says so),
while the plan's stated goal is "zero data loss". The migration should at minimum record
what it destroyed in the operator's log:

```python
def upgrade() -> None:
    # Keep the lowest id per (user_id, pmid); pmid IS NOT NULL so distinct-NULL
    # rows (no-PMID publications) are never touched. RECORD the count: downgrade()
    # cannot put these rows back, so the migration log is the only trace.
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            """
            DELETE FROM publications p
            USING publications p2
            WHERE p.pmid IS NOT NULL
              AND p.user_id = p2.user_id
              AND p.pmid = p2.pmid
              AND p.id > p2.id
            """
        )
    )
    print(f"0025: deleted {result.rowcount} duplicate (user_id, pmid) publication rows")
    op.create_unique_constraint(
        "uq_publications_user_pmid", "publications", ["user_id", "pmid"]
    )
```
(requires `import sqlalchemy as sa`, matching 0024's header.) Extend the Deploy note: the
rehearsal (`run_migration.sh` without `--apply`) plus `preflight.py` check 11 must be used
to dump the doomed rows **before** `--apply`, and the verified pre-migration backup is the
only rollback for them.

### MAJOR-10 (22.14) — the `invite.py` snippet silently rewrites the existing `except` handler

The task says "the `except Exception as exc:` handler and its message are unchanged", but
its code block shows a *different* handler than the file has. Real code
(`src/routers/invite.py:245-251`):

```python
        except Exception as exc:
            # Best-effort by design (specs/web-delegates.md §Slack Linkage): a
            # delegate is useful without a Slack id. But LOG it — a bare `pass`
            # here hid an ImportError for an unknown length of time, and the
            # whole sync was dead code with nothing to show for it.
            logger.warning(
                "Delegate Slack-ID sync failed for agent %s: %s", agent.agent_id, exc
            )
```

A literal paste deletes a comment that documents a real past defect and changes the log
line. Corrected snippet (only the `try` body changes):

```python
    if user.email:
        try:
            from src.services.delegate_slack_ids import append_delegate_slack_id_stmt
            from src.services.slack_tokens import token_for_agent_row
            from src.services.slack_web import lookup_user_by_email_async

            bot_token = token_for_agent_row(agent)
            if bot_token:
                sid = await lookup_user_by_email_async(bot_token, user.email)
                if sid:
                    await db.execute(append_delegate_slack_id_stmt(agent.id, sid))
        except Exception as exc:
            # UNCHANGED — keep the existing comment and message verbatim.
            ...
```

The rest of 22.14 is verified correct against SQLAlchemy 2.0.51 — I re-ran the compilation
the drafter claims to have done:

```
APPEND: UPDATE agents SET delegate_slack_ids=array_append(coalesce(agents.delegate_slack_ids, ARRAY[]::VARCHAR[]), 'U123')
        WHERE agents.id = '<uuid>' AND (agents.delegate_slack_ids IS NULL OR NOT ('U123' = ANY (agents.delegate_slack_ids)))
REMOVE: UPDATE agents SET delegate_slack_ids=array_remove(agents.delegate_slack_ids, 'U123') WHERE agents.id = '<uuid>'
EXACT MATCH with the test's expected string: True
```
`sqlalchemy.Update` is importable from the top level; the `or_(is_(None), ~…any())` guard
is *required* (`'x' = ANY(NULL)` is NULL, so `NOT NULL` would drop the row); and every
read site tolerates `{}` instead of `NULL` (`simulation.py:3508` `or []`,
`agent_page.py:313` truthiness, `:347` `or []`, `:1601` truthiness) — no `is None` anywhere.
The new remove-path test's fixture use is valid: `slack` is autouse with
`.stub(method, response)`, `delegated.row` is the `AgentDelegate`, and
`get_any_bot_token` will find the token the test sets on `world.agent`.

### MAJOR-11 (22.9) — one spec that advertises the removed status is not updated

`specs/admin-dashboard.md:23` — `- Profile status: \`no_profile\` | \`generating\` | \`complete\` | \`pending_update\`` —
survives the task, so the docs still promise a status the code can no longer emit. Add to
Step 3 and to the `git add` line:

```markdown
- Profile status: `no_profile` | `generating` | `complete`
  (`pending_update` was removed: `pending_profile` has no writer — issue #22 V6-pend)
```

Also fix the replacement for `specs/auth-and-user-management.md`, which deletes a
statement that is still true (old step 6):

```markdown
5. NOT IMPLEMENTED as a review queue: `pending_profile` has no writer (issue #22 V6-pend).
   A differing candidate is written directly to the live profile fields, gated by
   `_validate_profile`.
6. If no arrays changed: stores new publications but does not bother user
7. (removed — no side-by-side review exists to accept/edit/dismiss)
8. (removed — nothing is staged, so nothing auto-dismisses)
```

---

### MINOR findings

1. **22.1** — "17 existing tests + the 4 new ones": the file has **12** tests
   (`grep -c '^async def test'`), 16 after the addition. Measured `16 passed`.
2. **22.2** — Step 1's prose says the test "stamps it at 0024"; the code (correctly)
   *upgrades* to 0024. Reword.
3. **22.2** — lock footprint: `create_unique_constraint` takes ACCESS EXCLUSIVE inside the
   single-transaction alembic env with `lock_timeout=10s` (`alembic/env.py:73`). The worker
   is the only writer of `publications` (`profile_pipeline.py:218`), and a pipeline run that
   commits a duplicate between the DELETE and the ADD CONSTRAINT aborts the whole chain. Add
   to the Deploy note: stop `worker` (not `app`) for this revision, or retry — the DELETE is
   idempotent, so a retry is always safe.
4. **22.2** — `scripts/backfill_publications.py:68-78` dedups against rows already in the DB
   but not within its own input list, so a PMID repeated in the input file will now abort the
   backfill with an `IntegrityError`. One-line fix worth mentioning:
   `wanted = list(dict.fromkeys(str(p).strip() for p in pmids if str(p).strip()))`.
5. **22.4** — `.scalars().first()` without an `ORDER BY` picks an arbitrary row; use
   `.order_by(Publication.id)` so the pre-0025 duplicate case is deterministic.
6. **22.5** — `tests/integration/test_profile_pipeline_live.py:944`'s comment ("a 150-250 word
   summary") goes stale under 22.6's text change; update it in the same commit.
7. **22.5** — pre-existing, out of scope, worth recording: `article.findall(".//AbstractText")`
   also matches `<OtherAbstract>`'s children, so a translated abstract is concatenated onto
   the English one. Not in the findings; do not fix here.
8. **22.5 / focus (g)** — the remaining `.text` reads in `_parse_pubmed_xml`
   (`Journal/Title:220`, `PubDate/Year:225`, `PublicationType:233`, `PMID:180`,
   `ArticleId:195-196`, `ELocationID:203`, and `findtext` on `LastName`/`Initials`/
   `CollectiveName:248-256`) are on elements MEDLINE does not mark up inline. Leaving them
   is defensible; state it in the task so the exclusion is visible.
9. **22.6** — the plan lists 3 pre-fix failures; the real count is **6** (`test_null_techniques`
   and `test_non_list_disease_areas` raise `TypeError`, `test_non_string_research_summary`
   raises `AttributeError`). Not wrong, just incomplete.
10. **22.7** — Step 2's command has two `-k` flags in one invocation (the second wins) and
    Step 4 says "the same three `-k partial_post` commands" for two files. Split them.
11. **22.10** — `monkeypatch.setattr(aw.os, "fdopen", …)` / `(aw.tempfile, "mkstemp", …)`
    patch the *global* `os`/`tempfile` modules for the duration of the test (they pass — I ran
    them — but any code executing in that window is affected). Also the failing path leaks the
    fd from `mkstemp`. Consider `monkeypatch.setattr(aw, "os", SimpleNamespace(...))` or accept
    it with a comment; and add `profiles/**/*.tmp` to `.gitignore` for the leaked-temp path.
12. **22.10** — no `f.flush()` + `os.fsync(fd)` before `os.replace`, so the guarantee is
    "no torn file if the *process* dies", not "…if the *machine* dies". That matches the
    finding (`V6-24a` is about truncation), but the module docstring says "a crash" — tighten
    the wording or add the fsync.
13. **22.10** — `src/agent/foa_cache.py:29` (`write_text(json.dumps(...))` — a truncated JSON
    cache is a *parse error* on the next read) and `src/agent/grantbot.py:720` are the two
    `write_text` sites not adopted. `V6-24b` scopes them out explicitly, and grantbot is
    Part 23's file, so the exclusion is right — but say so, because the task's own prose
    claims "every disk writer in `src/`".
14. **22.11** — the new unconditional `export_private_profile(...)` inside
    `run_profile_pipeline` runs before the enclosing job transaction commits
    (`worker/main.py:97`), i.e. it *widens* V6-24e, which this part deliberately leaves open.
    One sentence in the Deploy note.
15. **22.12** — the four scripts import the module-private `_validate_profile`. Either make it
    public (`validate_profile`, with `_validate_profile = validate_profile` kept for the
    existing tests) or give `apply_synthesis(..., validated: bool | None = None)` the job of
    computing it, so there is exactly one cross-module contract instead of one-and-a-half.
16. **22.12** — after dropping `profile.profile_generated_at = datetime.now(timezone.utc)`,
    `from datetime import datetime, timezone` becomes unused in all four scripts
    (`scripts/*.py` is outside `LINT_TARGETS`, so cosmetic only). Remove them.
17. **22.12** — the pipeline call discards `apply_synthesis`'s return value while still
    bumping `profile_version` and the evidence counts. The case analysis is right (in the
    `else` branch it always returns `True`), but `if apply_synthesis(...):` around those three
    lines costs nothing and removes the invariant from the reader's head.
18. **22.13** — `profile.profile_version = await bump_profile_version(...)` re-dirties the
    attribute, so the ORM writes the same integer again at flush. Harmless (the row lock
    serializes concurrent bumps and each session writes the value it obtained atomically),
    but worth a comment so nobody "optimizes" the atomic UPDATE away later.
19. **22.13** — a module-level `from src.services.profile_pipeline import bump_profile_version`
    in three routers drags `src.services.llm` (anthropic), `orcid` and `pubmed` into the web
    app's import graph. It works (verified by import), but
    `src/services/profile_versioning.py` — already these routes' revision helper — is a
    cheaper home. If the plan keeps it in `profile_pipeline`, put the import in the correct
    alphabetical position so ruff `I001` does not add a finding to the src/ ratchet
    (baseline measured: **254**, ceiling 260).
20. **22.14** — `db.execute(update(...))` autoflushes, so the invite-accept route now flushes
    its pending `AgentDelegate`/invitation writes slightly earlier. Same transaction, commit
    follows; no action, just noted.

---

## 3. Anchor mismatches

| Task | Plan says | Actual | Severity |
|---|---|---|---|
| 22.1 | `orcid.py:23-137`, `fetch_orcid_profile :23-73`, `grants :76-95`, `works :98-137`; test `_record()` `:19-53` | all exact (`_record` is `:22-57`, the "19-53" is a couple of lines off) | MINOR |
| 22.1 | "17 existing tests" | 12 | MINOR |
| 22.2 | `preflight.py :114-206`, closing `)` at `:204`, `REVISION_ORDER :206` | `PLANNED_OBJECTS` starts `:168`, `)` at `:204`, `REVISION_ORDER :206` | MINOR (the 114 is the section comment) |
| 22.2 | `publication.py :13-41`, import line `:6`, relationship at `:38` | exact | OK |
| 22.3 | grep hits "exactly `:15, :74, :56, :232, :1129, :1170`" | also `preflight.py:203`, `:206`, `test_migration_checks.py:223`, `:838` (10 hits) | MAJOR (part of BLOCKER-2) |
| 22.4 | `run_profile_pipeline :107-282`; `pmids` `:115`; DOI loop `:143-149`; insert loop `:212-229`; PMC lookup `:272-282` | `run_profile_pipeline` starts `:51` (body through ~`:500`); `pmids` `:115` ✓; DOI loop `:144-150`; insert loop `:212-229` ✓; PMC lookup `:271-282` | MINOR |
| 22.4 / 22.12 | snippet indentation | DOI loop is at 12 spaces not 8; `regen_profile_from_cv`/`regen_profiles_from_web` blocks at 8 not 4 | MAJOR-4 |
| 22.5 | `_parse_pubmed_xml :207-219` | title `:206-207`, abstract `:209-218` | MINOR |
| 22.6 | `_validate_profile :553-579`; retry prompt `:324`; progress text `:429-430` | exact (`:553-579`, `:324`, `:429-434`) | OK |
| 22.7 | `profile.py :106-168` / body `:143-166`; `onboarding.py :107-156` / `:148-153`; `agent_page.py :1229-1264` / `:1256-1262` | exact | OK |
| 22.8 | `_prof` at `:166-179` | `:166-181` | MINOR |
| 22.9 | `admin.py :118-127` | block is `:117-127` (`if not profile:` at `:118`) | MINOR |
| 22.9 | `specs/profile-ingestion.md` items f/g `:184-186` | `:186-187` | MINOR |
| 22.9 | `specs/auth-and-user-management.md` steps 5-8 `:143-146` (header says `:137-144`) | `:144-147` | MINOR |
| 22.9 | `specs/data-model.md :53-60` | exact | OK |
| 22.10 | `profile_export.py :115-123`, `:139-147`; `agent_page.py :1116-1154`; `agent.py :710-711`, `:737-738` | `:115-123` ✓, `:138-147`, `:1116-1154` ✓, `:710-711` ✓, `:737-738` ✓ | MINOR |
| 22.11 | `export_private_profile :126-147`; pipeline `:457-474`, `:457-484` | `:126-147` ✓; Step 9b `:457`, agent-id lookup `:467-474`, exports `:475-497` | MINOR |
| 22.12 | `resynth :76-82` / `:70-89`; `cv :112-118` / `:103-120`; `vet :177-183` / `:168-191`; `web :107-113` / `:98-115` | `resynth :76-84`; `cv :112-120`; `vet :176-187`; `web :107-115` | MINOR |
| 22.13 | four sites | matches `findings` C1-a exactly (`profile_pipeline.py:417`, `onboarding.py:154`, `agent_page.py:1262`, `profile.py:166`) | OK |
| 22.14 | `agent_page.py :1422-1430`, `:1600-1614`; `invite.py :241-244` / `:232-244` | `:1419-1428`, `:1600-1614` ✓, `:239-244` | MINOR |
| 22.11 | `agent.py:119-126` default "No private instructions yet." | `:123` reads `PROFILES_DIR/private/{agent_id}.md` ✓ | OK |

Every quoted **BEFORE** block I checked matched the file byte-for-byte modulo the
indentation issues above and the `invite.py` `except` block (MAJOR-10).

---

## 4. Counts

- **BLOCKER: 4** — 22.2 (seed INSERT violates NOT NULL), 22.3 (verify-only violated /
  `HEAD_REVISION` written into prod tooling), 22.7 (`Form(None)` breaks clearing a field),
  22.7+22.8 (`world.pi` already has a profile → UNIQUE violation in three new tests).
- **MAJOR: 11** — 22.4 test cannot fail + ruff F401; 22.13 test ruff F401 + source-grep-only;
  22.12 `return 1` in a `-> bool` function; 22.12/22.4 snippet indentation; 22.10 0600 mode
  regression + false deploy note; 22.10 vacuous first-private-save test; 22.11 placeholder GM
  test naming a nonexistent fixture; 22.2 preflight ownership collision with M.1; 22.2 silent
  destructive DELETE; 22.14 `invite.py` handler rewrite; 22.9 `specs/admin-dashboard.md` left
  stale.
- **MINOR: 20** (listed above) + **19 anchor drifts** (all MINOR except 22.3's grep list and
  the indentation ones, counted above).
- **Tasks: 3 OK (22.1, 22.5, 22.6) · 8 needs-fix · 3 blocker-bearing (22.2, 22.3, 22.7, with
  22.8 blocked only by its shared test setup).**
- **Coverage: 42/42 findings ids mapped; no wrongly-excluded item.**

### Verification log (all run, not inferred)

| What | Result |
|---|---|
| 22.1 tests vs pre-fix `orcid.py` | 4 failed (`'NoneType' has no attribute 'get'/'lower'`), 12 passed |
| 22.1 tests vs patched `orcid.py` | **16 passed**, `ruff check src/services/orcid.py` clean |
| 22.5 test vs pre-fix `pubmed.py` | failed: `assert 'Role of ' == 'Role of TP53 in cancer'` |
| 22.5 test vs patched `pubmed.py` | **13 passed**, ruff clean |
| 22.6 tests vs pre-fix pipeline | **6 failed**, 4 passed |
| 22.6 tests vs patched pipeline | **10 passed** |
| 22.10 module + 4 tests | **4 passed**; mode measured 0664 → 0600 |
| 22.11 3 unit tests vs patched exporter | **3 passed** |
| 22.12 8 unit tests vs `apply_synthesis` | **8 passed** |
| 22.13 test + `bump_profile_version` | 1 passed but **2 ruff F401**; statement compiled, SQL as quoted above |
| 22.14 3 unit tests + compiled SQL | **3 passed**; exact-string remove assertion **matches** |
| 22.9 test vs pre-fix `admin.py` | failed (`pending_profile` in source) ✓ red |
| 22.4 test vs pre-fix pipeline | **passed** (cannot fail) + 1 ruff F401 + PytestWarning |
| `ruff check src` baseline vs patched | 254 → 254 (no new src debt from the parts I applied) |
| FastAPI `Form(None)` empty-value semantics | empty string → **None** (`utils.py:765-780`) |
| FastAPI `await request.form()` presence check | works alongside `Form(...)` params; distinguishes "" from absent |
| `users` DDL defaults | `is_admin`/`email_notifications_enabled`/`onboarding_complete` NOT NULL, no DEFAULT |
| GM `_VALID_PROFILE` word count | **125** |
