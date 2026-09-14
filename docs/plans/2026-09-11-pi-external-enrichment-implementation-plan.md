# PI External Enrichment (NIH RePORTER grants + industry-interest score) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Attach every JHU-tenure NIH grant to each PI's profile (bolstering profile synthesis), and compute a separate, read-only *industry-interest score* per PI from tenure-scoped external evidence, without the score ever touching profile text or hub prompts.

**Architecture:** Two new worker job types run after `generate_profile`: `enrich_grants` resolves the PI's RePORTER identity by intersecting RePORTER's project→PMID links with the PI's ORCID-anchored corpus, then stores tenure-filtered projects in `pi_grants` and projects a filtered `grant_titles` list for synthesis/export. `industry_evidence` collects company co-authors, company funders, COI statements, JHU-filed patents and industry-collaborator trials into `pi_industry_evidence`, then a pure, versioned scorer writes `pi_industry_scores`. Both surface on `/manager/pis/{id}` with per-row vetoes; neither has any import path into `profile_export`, `thread_guidance`, `tools.py` or `simulation.py` (enforced by an import-probe test).

**Tech Stack:** FastAPI + SQLAlchemy async + Alembic + Postgres 15 (existing); `httpx` clients with the `_pace()` pattern from `src/services/patents.py`; `respx` for contract tests; Jinja templates under `templates/manager/`.

**Spec:** `docs/plans/2026-09-11-nih-reporter-grant-enrichment-analysis.md` and `docs/plans/2026-09-11-industry-interest-score-analysis.md` (both measured live 2026-09-11 — re-probe before trusting field names).

## Global Constraints

- Alembic head is `0046`; the new migration is `0047`, additive only, old-code-safe (nullable columns, new tables, `ALTER TYPE job_type_enum ADD VALUE IF NOT EXISTS` — cannot be downgraded, document like `0039`).
- Tenure rule: a grant/evidence row is in-tenure only if **both** the institution test and `year >= tenure_start` hold. No tenure start → grants fall back to org-only and are labelled `org_only`; the industry score is **not computed** (`reason='no_tenure_start'`).
- RePORTER org exact string: `JOHNS HOPKINS UNIVERSITY` (IPF 4134401). OpenAlex JHU family IDs: `I145311948 I2799853436 I4210150714 I2802946424 I2802697821 I4210098865 I4210129832 I4210092215 I4389425327 I4210114877`.
- Every RePORTER criteria key must be in a frozen allowlist; a single-PI query with `meta.total > 500` aborts (silent-firehose guard).
- The score never enters `ResearcherProfile`, `profiles/public/*.md`, or any prompt. `grant_titles` DOES feed synthesis, filtered to an activity-code allowlist.
- New manager POST routes must be added to `tests/integration/test_manager_views.py::test_manager_router_mutations_are_an_explicit_allowlist` in the same commit.
- Run `./scripts/ci.sh` before the final commit. Run `.venv-test/bin/python scripts/sync_prompt_set_docs.py --check` if any prompt is touched (none planned).
- Deploy: build app+worker+agent, `alembic upgrade head` from a one-off container, then start — add a `0047` box to CLAUDE.md (Task 12).

---

### Task 1: Migration 0047 + ORM models

**Files:**
- Create: `alembic/versions/0047_pi_grants_and_industry_evidence.py`
- Create: `src/models/enrichment.py`
- Modify: `src/models/__init__.py` (export new models)
- Modify: `src/models/job.py:20-23` (enum values)
- Test: `tests/unit/test_enrichment_models.py`

**Interfaces:**
- Produces: `PiGrant`, `PiIndustryEvidence`, `PiIndustryScore` ORM classes; job types `"enrich_grants"`, `"industry_evidence"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_enrichment_models.py
import uuid
import pytest
from sqlalchemy import select
from src.models import PiGrant, PiIndustryEvidence, PiIndustryScore, User

pytestmark = pytest.mark.integration


async def _user(db):
    u = User(orcid=f"0000-0001-{uuid.uuid4().hex[:4]}-{uuid.uuid4().hex[:4]}", name="T", user_role="pi")
    db.add(u); await db.flush(); return u


async def test_grant_rows_are_unique_per_user_and_core(db_session):
    u = await _user(db_session)
    db_session.add(PiGrant(user_id=u.id, core_project_num="R01AI137329", title="x",
                           org_name="JOHNS HOPKINS UNIVERSITY", first_fy=2019, last_fy=2024,
                           tenure_filter_mode="org_and_year", identity_evidence={"pmid_links": 3}))
    await db_session.flush()
    db_session.add(PiGrant(user_id=u.id, core_project_num="R01AI137329", title="dup",
                           org_name="JOHNS HOPKINS UNIVERSITY", tenure_filter_mode="org_and_year"))
    with pytest.raises(Exception):
        await db_session.flush()


async def test_score_row_allows_null_score_with_reason(db_session):
    u = await _user(db_session)
    db_session.add(PiIndustryScore(user_id=u.id, score=None, reason="no_tenure_start",
                                   scorer_version="1.0.0", components={}))
    await db_session.flush()
    row = (await db_session.execute(select(PiIndustryScore).where(PiIndustryScore.user_id == u.id))).scalar_one()
    assert row.score is None and row.reason == "no_tenure_start"


async def test_evidence_row_defaults_not_vetoed(db_session):
    u = await _user(db_session)
    e = PiIndustryEvidence(user_id=u.id, source="openalex", kind="coauthor_company",
                           external_id="W1", company_name="Paratek Pharmaceuticals",
                           company_class="pharma_biotech", year=2021, in_tenure=True, evidence={})
    db_session.add(e); await db_session.flush()
    assert e.vetoed_at is None
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv-test/bin/python -m pytest tests/unit/test_enrichment_models.py -v`
Expected: FAIL — `ImportError: cannot import name 'PiGrant'`.

- [ ] **Step 3: Write the models**

```python
# src/models/enrichment.py
"""External-enrichment tables: NIH RePORTER grants, industry evidence, industry score.

The score tables are deliberately NOT imported by profile_export, thread_guidance,
tools.py or simulation.py — tests/unit/test_enrichment_isolation.py enforces it.
"""
import uuid
from datetime import datetime

from sqlalchemy import (Boolean, DateTime, Float, ForeignKey, Integer, String, Text,
                        UniqueConstraint, func)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base

GRANT_TENURE_MODES = ("org_and_year", "org_only")
EVIDENCE_KINDS = ("coauthor_company", "company_funder", "coi_relationship", "patent_filed",
                  "patent_assigned", "trial_industry_collab", "sbir_sttr")
COMPANY_CLASSES = ("pharma_biotech", "device_dx", "cro_vendor", "other", "unknown")


class PiGrant(Base):
    __tablename__ = "pi_grants"
    __table_args__ = (UniqueConstraint("user_id", "core_project_num", name="uq_pi_grants_user_core"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="nih_reporter")
    core_project_num: Mapped[str] = mapped_column(String(40), nullable=False)
    reporter_profile_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    phr_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    terms: Mapped[str | None] = mapped_column(Text, nullable=True)
    activity_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    agency_ic: Mapped[str | None] = mapped_column(String(20), nullable=True)
    funding_mechanism: Mapped[str | None] = mapped_column(String(40), nullable=True)
    org_name: Mapped[str] = mapped_column(String(200), nullable=False)
    first_fy: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_fy: Mapped[int | None] = mapped_column(Integer, nullable=True)
    project_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    project_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    total_award_in_tenure: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_contact_pi: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_subproject: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tenure_filter_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    identity_evidence: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    vetoed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PiIndustryEvidence(Base):
    __tablename__ = "pi_industry_evidence"
    __table_args__ = (UniqueConstraint("user_id", "source", "kind", "external_id", name="uq_pi_industry_evidence_key"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False)      # openalex|pubmed|uspto|ctgov|nih_reporter
    kind: Mapped[str] = mapped_column(String(30), nullable=False)        # EVIDENCE_KINDS
    external_id: Mapped[str] = mapped_column(String(120), nullable=False)  # work id / pmid+company / app no / NCT
    company_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    company_external_id: Mapped[str | None] = mapped_column(String(120), nullable=True)  # OpenAlex I… or ROR
    company_class: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pi_role: Mapped[str | None] = mapped_column(String(30), nullable=True)  # first|last|corresponding|middle|inventor|overall_official
    in_tenure: Mapped[bool] = mapped_column(Boolean, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    vetoed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    vetoed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PiIndustryScore(Base):
    __tablename__ = "pi_industry_scores"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)          # 0..100, NULL = unscored
    raw_sum: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(40), nullable=True)      # no_tenure_start | no_evidence | ok
    components: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    field_percentile: Mapped[float | None] = mapped_column(Float, nullable=True)
    primary_field: Mapped[str | None] = mapped_column(String(120), nullable=True)
    tenure_start_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    scorer_version: Mapped[str] = mapped_column(String(20), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
```

Add to `src/models/__init__.py`: `from src.models.enrichment import PiGrant, PiIndustryEvidence, PiIndustryScore` and the three names in `__all__`.

In `src/models/job.py` change the enum to
`Enum("generate_profile", "monthly_refresh", "review_feedback_analysis", "enrich_grants", "industry_evidence", name="job_type_enum")`.

- [ ] **Step 4: Write the migration**

```python
# alembic/versions/0047_pi_grants_and_industry_evidence.py
"""pi_grants, pi_industry_evidence, pi_industry_scores + two job types

Three new tables and two ``job_type_enum`` values (``enrich_grants``,
``industry_evidence``). Purely additive: OLD CODE AGAINST THE NEW SCHEMA IS
SAFE. New code against the old schema fails only in the new worker handlers
and the manager PI page's new panels (UndefinedTable) — see the CLAUDE.md box.

Enum values cannot be dropped in Postgres; downgrade drops the tables and
leaves the values, exactly as 0039 does for ``review_feedback_analysis``.

Revision ID: 0047
Revises: 0046
Create Date: 2026-09-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0047"
down_revision: Union[str, None] = "0046"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE job_type_enum ADD VALUE IF NOT EXISTS 'enrich_grants'")
    op.execute("ALTER TYPE job_type_enum ADD VALUE IF NOT EXISTS 'industry_evidence'")

    op.create_table(
        "pi_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String(20), nullable=False, server_default="nih_reporter"),
        sa.Column("core_project_num", sa.String(40), nullable=False),
        sa.Column("reporter_profile_id", sa.Integer, nullable=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("phr_text", sa.Text, nullable=True),
        sa.Column("terms", sa.Text, nullable=True),
        sa.Column("activity_code", sa.String(10), nullable=True),
        sa.Column("agency_ic", sa.String(20), nullable=True),
        sa.Column("funding_mechanism", sa.String(40), nullable=True),
        sa.Column("org_name", sa.String(200), nullable=False),
        sa.Column("first_fy", sa.Integer, nullable=True),
        sa.Column("last_fy", sa.Integer, nullable=True),
        sa.Column("project_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("project_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total_award_in_tenure", sa.Integer, nullable=True),
        sa.Column("is_contact_pi", sa.Boolean, nullable=True),
        sa.Column("is_subproject", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("tenure_filter_mode", sa.String(20), nullable=False),
        sa.Column("identity_evidence", postgresql.JSONB, nullable=True),
        sa.Column("vetoed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "core_project_num", name="uq_pi_grants_user_core"),
    )
    op.create_index("ix_pi_grants_user_id", "pi_grants", ["user_id"])

    op.create_table(
        "pi_industry_evidence",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("external_id", sa.String(120), nullable=False),
        sa.Column("company_name", sa.String(300), nullable=True),
        sa.Column("company_external_id", sa.String(120), nullable=True),
        sa.Column("company_class", sa.String(20), nullable=False, server_default="unknown"),
        sa.Column("year", sa.Integer, nullable=True),
        sa.Column("pi_role", sa.String(30), nullable=True),
        sa.Column("in_tenure", sa.Boolean, nullable=False),
        sa.Column("evidence", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("vetoed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("vetoed_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "source", "kind", "external_id", name="uq_pi_industry_evidence_key"),
    )
    op.create_index("ix_pi_industry_evidence_user_id", "pi_industry_evidence", ["user_id"])

    op.create_table(
        "pi_industry_scores",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("score", sa.Float, nullable=True),
        sa.Column("raw_sum", sa.Float, nullable=True),
        sa.Column("reason", sa.String(40), nullable=True),
        sa.Column("components", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("field_percentile", sa.Float, nullable=True),
        sa.Column("primary_field", sa.String(120), nullable=True),
        sa.Column("tenure_start_used", sa.Integer, nullable=True),
        sa.Column("evidence_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("scorer_version", sa.String(20), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_pi_industry_scores_user_id", "pi_industry_scores", ["user_id"])


def downgrade() -> None:
    op.drop_table("pi_industry_scores")
    op.drop_table("pi_industry_evidence")
    op.drop_table("pi_grants")
    # job_type_enum keeps the two values (cannot drop); harmless at 0046.
```

- [ ] **Step 5: Run tests**

Run: `.venv-test/bin/python -m pytest tests/unit/test_enrichment_models.py tests/unit/test_alembic_sanity.py -v` (the conftest migrates the throwaway DB with the real chain, so a broken migration fails here).
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add alembic/versions/0047_pi_grants_and_industry_evidence.py src/models/enrichment.py src/models/__init__.py src/models/job.py tests/unit/test_enrichment_models.py
git commit -m "feat(enrichment): 0047 pi_grants, pi_industry_evidence, pi_industry_scores + job types"
```

---

### Task 2: NIH RePORTER client with criteria-allowlist and firehose guards

**Files:**
- Create: `src/services/nih_reporter.py`
- Test: `tests/unit/test_nih_reporter_client.py`

**Interfaces:**
- Produces:
  - `async def search_projects(criteria: dict, include_fields: list[str], *, max_total: int = 500) -> list[dict]` — pages all results (limit 500/offset), raises `ReporterCriteriaError` on an unknown criteria key, raises `ReporterFirehoseError` if `meta.total > max_total`.
  - `async def publications_for_cores(core_project_nums: list[str]) -> dict[str, set[str]]` — `{core_project_num: {pmid,...}}` (PMIDs as strings).
  - `ALLOWED_CRITERIA = frozenset({"pi_names","pi_profile_ids","org_names","org_names_exact_match","fiscal_years","project_nums","core_project_nums","include_active_projects","appl_ids"})`
  - `PROJECT_FIELDS` — the exact `include_fields` list below.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_nih_reporter_client.py
import httpx, pytest, respx
from src.services import nih_reporter as nr

pytestmark = pytest.mark.contract
URL = "https://api.reporter.nih.gov/v2/projects/search"
PUBS = "https://api.reporter.nih.gov/v2/publications/search"


def _page(total, rows, offset=0):
    return {"meta": {"total": total, "offset": offset, "limit": 500}, "results": rows}


async def test_unknown_criteria_key_is_refused_before_any_request():
    with respx.mock() as router:
        route = router.post(URL)
        with pytest.raises(nr.ReporterCriteriaError):
            await nr.search_projects({"orcid": ["0000-0002-2214-0114"]}, nr.PROJECT_FIELDS)
        assert not route.called


async def test_firehose_total_aborts():
    with respx.mock() as router:
        router.post(URL).mock(return_value=httpx.Response(200, json=_page(2975461, [{"project_num": "x"}])))
        with pytest.raises(nr.ReporterFirehoseError):
            await nr.search_projects({"pi_names": [{"last_name": "Wu"}]}, nr.PROJECT_FIELDS)


async def test_pages_until_total_reached():
    rows1 = [{"project_num": f"a{i}"} for i in range(500)]
    rows2 = [{"project_num": "b0"}]
    with respx.mock() as router:
        router.post(URL).mock(side_effect=[
            httpx.Response(200, json=_page(501, rows1, 0)),
            httpx.Response(200, json=_page(501, rows2, 500)),
        ])
        out = await nr.search_projects({"pi_profile_ids": [9751245]}, nr.PROJECT_FIELDS, max_total=1000)
    assert len(out) == 501


async def test_publications_for_cores_groups_pmids_as_strings():
    with respx.mock() as router:
        router.post(PUBS).mock(return_value=httpx.Response(200, json={
            "meta": {"total": 2, "offset": 0, "limit": 500},
            "results": [{"coreproject": "R01AI137329", "pmid": 34187885, "applid": 1},
                        {"coreproject": "R01AI137329", "pmid": 30000000, "applid": 2}]}))
        out = await nr.publications_for_cores(["R01AI137329"])
    assert out == {"R01AI137329": {"34187885", "30000000"}}
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv-test/bin/python -m pytest tests/unit/test_nih_reporter_client.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```python
# src/services/nih_reporter.py
"""NIH RePORTER v2 client (api.reporter.nih.gov). Live-verified 2026-09-11.

Two guards exist because RePORTER SILENTLY IGNORES unknown criteria keys:
``{"criteria": {"orcid": [...]}}`` returns HTTP 200 and the whole database
(2,975,461 rows on 2026-09-11). A typo must fail loudly, not attach every NIH
grant to one PI. Rate limit measured at ~200 requests/minute per IP
(``x-rate-limit-limit: 1m``), so requests are paced 0.35 s apart.
"""
import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

BASE = "https://api.reporter.nih.gov/v2"
PAGE = 500
_PACE_INTERVAL = 0.35
_next_slot = 0.0

ALLOWED_CRITERIA = frozenset({
    "pi_names", "pi_profile_ids", "org_names", "org_names_exact_match", "fiscal_years",
    "project_nums", "core_project_nums", "include_active_projects", "appl_ids",
})
PROJECT_FIELDS = [
    "ProjectNum", "CoreProjectNum", "FiscalYear", "ProjectTitle", "PhrText", "Terms",
    "ActivityCode", "AgencyIcAdmin", "FundingMechanism", "Organization",
    "PrincipalInvestigators", "ProjectStartDate", "ProjectEndDate", "AwardAmount",
    "SubprojectId", "IsActive",
]
JHU_ORG_EXACT = "JOHNS HOPKINS UNIVERSITY"


class ReporterCriteriaError(ValueError):
    """A criteria key outside ALLOWED_CRITERIA — RePORTER would ignore it silently."""


class ReporterFirehoseError(RuntimeError):
    """meta.total exceeded the per-PI ceiling; the filter did not bite."""


async def _pace() -> None:
    global _next_slot
    loop = asyncio.get_running_loop()
    now = loop.time()
    wait = _next_slot - now
    _next_slot = max(now, _next_slot) + _PACE_INTERVAL
    if wait > 0:
        await asyncio.sleep(wait)


async def _post(client: httpx.AsyncClient, path: str, body: dict) -> dict:
    await _pace()
    for attempt in range(3):
        resp = await client.post(f"{BASE}/{path}", json=body)
        if resp.status_code == 429 and attempt < 2:
            await asyncio.sleep(2.0 * (attempt + 1))
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("unreachable")


def _check_criteria(criteria: dict) -> None:
    bad = set(criteria) - ALLOWED_CRITERIA
    if bad:
        raise ReporterCriteriaError(f"RePORTER would silently ignore criteria keys: {sorted(bad)}")


async def search_projects(criteria: dict, include_fields: list[str], *, max_total: int = 500) -> list[dict]:
    _check_criteria(criteria)
    out: list[dict] = []
    offset = 0
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            data = await _post(client, "projects/search", {
                "criteria": criteria, "include_fields": include_fields,
                "offset": offset, "limit": PAGE,
            })
            total = int(data.get("meta", {}).get("total", 0))
            if total > max_total:
                raise ReporterFirehoseError(f"RePORTER returned total={total} > {max_total} for {criteria}")
            rows = data.get("results") or []
            out.extend(rows)
            offset += PAGE
            if offset >= total or not rows:
                return out


async def publications_for_cores(core_project_nums: list[str]) -> dict[str, set[str]]:
    links: dict[str, set[str]] = {}
    if not core_project_nums:
        return links
    async with httpx.AsyncClient(timeout=30) as client:
        for i in range(0, len(core_project_nums), 50):
            chunk = core_project_nums[i:i + 50]
            offset = 0
            while True:
                data = await _post(client, "publications/search", {
                    "criteria": {"core_project_nums": chunk}, "offset": offset, "limit": PAGE,
                })
                for r in data.get("results") or []:
                    links.setdefault(r["coreproject"], set()).add(str(r["pmid"]))
                total = int(data.get("meta", {}).get("total", 0))
                offset += PAGE
                if offset >= total:
                    break
    return links
```

- [ ] **Step 4: Run tests**

Run: `.venv-test/bin/python -m pytest tests/unit/test_nih_reporter_client.py -v`
Expected: PASS (4).

- [ ] **Step 5: Commit**

```bash
git add src/services/nih_reporter.py tests/unit/test_nih_reporter_client.py
git commit -m "feat(enrichment): NIH RePORTER client with criteria allowlist and firehose guard"
```

---

### Task 3: Grant identity resolution + tenure filtering (pure functions)

**Files:**
- Create: `src/services/grant_resolution.py`
- Test: `tests/unit/test_grant_resolution.py`

**Interfaces:**
- Consumes: raw project rows shaped like RePORTER results (`core_project_num`, `fiscal_year`, `organization.org_name`, `principal_investigators[{profile_id,is_contact_pi,first_name,last_name}]`, `award_amount`, `subproject_id`, …) and `publications_for_cores` output.
- Produces:
  - `resolve_profile_ids(rows, corpus_pmids: set[str], links: dict[str,set[str]], first_name: str) -> tuple[set[int], dict]` — accepted profile_ids + evidence dict `{"pmid_linked": [...], "unique_name": [...], "rejected": [...]}`.
  - `filter_and_collapse(rows, profile_ids, tenure_start: int|None) -> tuple[list[GrantRecord], str]` — collapsed one-per-core records + mode `"org_and_year"`/`"org_only"`.
  - `derive_grant_titles(records) -> list[str]` — LLM-allowlisted activity codes, `last_fy` desc.
  - `@dataclass GrantRecord` with fields matching `PiGrant` columns (`core_project_num, reporter_profile_id, title, phr_text, terms, activity_code, agency_ic, funding_mechanism, org_name, first_fy, last_fy, project_start, project_end, total_award_in_tenure, is_contact_pi, is_subproject`).
  - `LLM_ACTIVITY_CODES = frozenset({"R01","R21","R33","R35","R37","R00","R56","DP1","DP2","U01","U19","P01","K08","K23","K99","R41","R42","R43","R44","R03","RF1","UG3","UH3"})`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_grant_resolution.py
from src.services import grant_resolution as gr

JHU = {"org_name": "JOHNS HOPKINS UNIVERSITY"}
def row(core, fy, pid, org=JHU, amount=100, first="Peng", last="Wu", sub=None, code=None):
    code = code or core[:3]
    return {"core_project_num": core, "project_num": f"5{core}-0{fy%10}", "fiscal_year": fy,
            "organization": org, "award_amount": amount, "subproject_id": sub, "activity_code": code,
            "project_title": f"T {core}", "phr_text": "p", "terms": "t", "agency_ic_admin": {"code": "AI"},
            "funding_mechanism": "Non-SBIR/STTR", "project_start_date": f"{fy}-03-01T00:00:00",
            "project_end_date": f"{fy+2}-02-28T00:00:00",
            "principal_investigators": [{"profile_id": pid, "is_contact_pi": True, "first_name": first, "last_name": last}]}


def test_name_collision_resolved_by_pmid_link():
    rows = [row("R01AA000001", 2020, 111), row("R01BB000002", 2020, 222)]
    links = {"R01AA000001": {"1", "2"}, "R01BB000002": {"9"}}
    ids, ev = gr.resolve_profile_ids(rows, corpus_pmids={"2"}, links=links, first_name="Peng")
    assert ids == {111} and ev["pmid_linked"] == [111] and 222 in ev["rejected"]


def test_unique_jhu_candidate_with_exact_first_name_accepted_without_links():
    rows = [row("R21CC000003", 2022, 333)]
    ids, ev = gr.resolve_profile_ids(rows, corpus_pmids=set(), links={}, first_name="Peng")
    assert ids == {333} and ev["unique_name"] == [333]


def test_two_candidates_no_links_accepts_nobody():
    rows = [row("R01AA000001", 2020, 111), row("R01BB000002", 2020, 222)]
    ids, _ = gr.resolve_profile_ids(rows, corpus_pmids=set(), links={}, first_name="Peng")
    assert ids == set()


def test_moved_institution_award_keeps_only_jhu_years():
    rows = [row("R01AA000001", 2016, 111, org={"org_name": "UNIVERSITY OF ELSEWHERE"}),
            row("R01AA000001", 2018, 111), row("R01AA000001", 2019, 111)]
    recs, mode = gr.filter_and_collapse(rows, {111}, tenure_start=2018)
    assert mode == "org_and_year" and len(recs) == 1
    assert (recs[0].first_fy, recs[0].last_fy, recs[0].total_award_in_tenure) == (2018, 2019, 200)


def test_pre_tenure_fiscal_years_excluded_even_at_jhu():
    rows = [row("R01AA000001", 2015, 111), row("R01AA000001", 2019, 111)]
    recs, _ = gr.filter_and_collapse(rows, {111}, tenure_start=2018)
    assert recs[0].first_fy == 2019


def test_no_tenure_is_org_only_mode():
    rows = [row("R01AA000001", 2010, 111)]
    recs, mode = gr.filter_and_collapse(rows, {111}, tenure_start=None)
    assert mode == "org_only" and len(recs) == 1


def test_supplement_rows_not_double_summed():
    a = row("R01AA000001", 2020, 111, amount=100); b = dict(a)  # same project_num → duplicate row
    recs, _ = gr.filter_and_collapse([a, b], {111}, tenure_start=None)
    assert recs[0].total_award_in_tenure == 100


def test_grant_titles_exclude_training_codes_and_sort_recent_first():
    rows = [row("T32DD000004", 2024, 111, code="T32"), row("R01AA000001", 2019, 111), row("R21CC000003", 2023, 111)]
    recs, _ = gr.filter_and_collapse(rows, {111}, tenure_start=None)
    assert gr.derive_grant_titles(recs) == ["T R21CC000003", "T R01AA000001"]
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv-test/bin/python -m pytest tests/unit/test_grant_resolution.py -v` → FAIL, module missing.

- [ ] **Step 3: Implement**

```python
# src/services/grant_resolution.py
"""Turn RePORTER rows into per-PI grant records: identity, tenure, collapse.

Identity rule (adversarial analysis A1–A5): a RePORTER ``profile_id`` is the
PI iff one of its projects links (RePORTER publications endpoint) to a PMID in
the PI's own ORCID/PubMed-anchored corpus; failing that, iff it is the ONLY
Hopkins candidate with an exact first-name match. Two unlinked candidates →
nobody, never a guess.

Tenure rule (B6–B8): a fiscal-year row counts iff org == JHU exact AND
fiscal_year >= tenure_start. With no tenure start the year half is skipped and
the mode is reported as ``org_only`` so the UI can show the weaker guarantee.
"""
from dataclasses import dataclass
from datetime import datetime, timezone

from src.services.nih_reporter import JHU_ORG_EXACT

LLM_ACTIVITY_CODES = frozenset({
    "R01", "R21", "R33", "R35", "R37", "R00", "R56", "R03", "RF1", "DP1", "DP2",
    "U01", "U19", "UG3", "UH3", "P01", "K08", "K23", "K99", "R41", "R42", "R43", "R44",
})


@dataclass
class GrantRecord:
    core_project_num: str
    reporter_profile_id: int | None
    title: str
    phr_text: str | None
    terms: str | None
    activity_code: str | None
    agency_ic: str | None
    funding_mechanism: str | None
    org_name: str
    first_fy: int | None
    last_fy: int | None
    project_start: datetime | None
    project_end: datetime | None
    total_award_in_tenure: int | None
    is_contact_pi: bool | None
    is_subproject: bool


def _org(row: dict) -> str:
    return ((row.get("organization") or {}).get("org_name") or "").strip().upper()


def _pis(row: dict) -> list[dict]:
    return row.get("principal_investigators") or []


def resolve_profile_ids(rows: list[dict], corpus_pmids: set[str], links: dict[str, set[str]],
                        first_name: str) -> tuple[set[int], dict]:
    cores_by_pid: dict[int, set[str]] = {}
    first_by_pid: dict[int, set[str]] = {}
    for r in rows:
        if _org(r) != JHU_ORG_EXACT:
            continue
        for p in _pis(r):
            pid = p.get("profile_id")
            if pid is None:
                continue
            cores_by_pid.setdefault(pid, set()).add(r["core_project_num"])
            first_by_pid.setdefault(pid, set()).add((p.get("first_name") or "").strip().lower())
    pmid_linked = [pid for pid, cores in cores_by_pid.items()
                   if any(links.get(c, set()) & corpus_pmids for c in cores)]
    accepted = set(pmid_linked)
    unique_name: list[int] = []
    if not accepted and len(cores_by_pid) == 1:
        (pid, _), = cores_by_pid.items()
        if first_name.strip().lower() in first_by_pid[pid]:
            accepted.add(pid); unique_name.append(pid)
    rejected = sorted(set(cores_by_pid) - accepted)
    return accepted, {"pmid_linked": sorted(pmid_linked), "unique_name": unique_name, "rejected": rejected}


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def filter_and_collapse(rows: list[dict], profile_ids: set[int], tenure_start: int | None
                        ) -> tuple[list[GrantRecord], str]:
    mode = "org_and_year" if tenure_start is not None else "org_only"
    kept: dict[str, list[dict]] = {}
    seen_project_nums: set[str] = set()
    for r in rows:
        if _org(r) != JHU_ORG_EXACT:
            continue
        if tenure_start is not None and (r.get("fiscal_year") or 0) < tenure_start:
            continue
        if not any(p.get("profile_id") in profile_ids for p in _pis(r)):
            continue
        if r.get("project_num") in seen_project_nums:
            continue
        seen_project_nums.add(r.get("project_num"))
        kept.setdefault(r["core_project_num"], []).append(r)
    records: list[GrantRecord] = []
    for core, rs in kept.items():
        rs.sort(key=lambda x: x.get("fiscal_year") or 0)
        latest = rs[-1]
        me = next((p for p in _pis(latest) if p.get("profile_id") in profile_ids), {})
        records.append(GrantRecord(
            core_project_num=core,
            reporter_profile_id=me.get("profile_id"),
            title=latest.get("project_title") or core,
            phr_text=latest.get("phr_text"),
            terms=latest.get("terms"),
            activity_code=latest.get("activity_code"),
            agency_ic=(latest.get("agency_ic_admin") or {}).get("code"),
            funding_mechanism=latest.get("funding_mechanism"),
            org_name=JHU_ORG_EXACT,
            first_fy=rs[0].get("fiscal_year"),
            last_fy=latest.get("fiscal_year"),
            project_start=_dt(rs[0].get("project_start_date")),
            project_end=_dt(latest.get("project_end_date")),
            total_award_in_tenure=sum(int(x.get("award_amount") or 0) for x in rs),
            is_contact_pi=me.get("is_contact_pi"),
            is_subproject=any(x.get("subproject_id") for x in rs),
        ))
    return records, mode


def derive_grant_titles(records: list[GrantRecord]) -> list[str]:
    eligible = [r for r in records if (r.activity_code or "") in LLM_ACTIVITY_CODES]
    eligible.sort(key=lambda r: (r.last_fy or 0), reverse=True)
    return [r.title for r in eligible]
```

- [ ] **Step 4: Run tests** → PASS (8).

- [ ] **Step 5: Commit**

```bash
git add src/services/grant_resolution.py tests/unit/test_grant_resolution.py
git commit -m "feat(enrichment): RePORTER identity resolution, tenure filter, grant_titles projection"
```

---

### Task 4: `enrich_grants` worker job + enqueue after profile generation

**Files:**
- Create: `src/services/grant_enrichment.py`
- Modify: `src/worker/main.py:162-169` (dispatch)
- Modify: `src/services/profile_pipeline.py` (enqueue both new jobs after the final commit; find the `update_progress("step9", ...)` block at `:443` and add after the profile commit)
- Test: `tests/unit/test_grant_enrichment_job.py`

**Interfaces:**
- Consumes: Task 2 `search_projects`, `publications_for_cores`; Task 3 functions; `get_tenure_start(db, user_id, agent_id)` from `src/services/jhu_rules.py`.
- Produces: `async def execute_enrich_grants(job: Job, db: AsyncSession) -> None`; `async def enqueue_enrichment_jobs(db, user_id, orcid) -> None` (adds `enrich_grants` + `industry_evidence` `Job`s if none pending).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_grant_enrichment_job.py
import uuid, pytest
from sqlalchemy import select
from src.models import Job, PiGrant, Publication, ResearcherProfile, User
from src.services import grant_enrichment as ge
from src.services.jhu_rules import set_tenure_start

pytestmark = pytest.mark.integration
JHU = {"org_name": "JOHNS HOPKINS UNIVERSITY"}

def _row(core, fy, pid, org=JHU):
    return {"core_project_num": core, "project_num": f"5{core}-0{fy%10}", "fiscal_year": fy, "organization": org,
            "award_amount": 10, "subproject_id": None, "activity_code": core[:3], "project_title": f"T {core}",
            "phr_text": None, "terms": None, "agency_ic_admin": {"code": "AI"}, "funding_mechanism": "Non-SBIR/STTR",
            "project_start_date": None, "project_end_date": None,
            "principal_investigators": [{"profile_id": pid, "is_contact_pi": True, "first_name": "Gyanu", "last_name": "Lamichhane"}]}


async def test_job_writes_only_pmid_linked_in_tenure_grants_and_projects_titles(db_session, monkeypatch):
    u = User(orcid="0000-0002-2214-0114", name="Gyanu Lamichhane", user_role="pi"); db_session.add(u); await db_session.flush()
    db_session.add(ResearcherProfile(user_id=u.id, grant_titles=["old orcid title"]))
    db_session.add(Publication(user_id=u.id, pmid="34187885", title="p"))
    await set_tenure_start(u.id, 2018, "manual", db=db_session)
    job = Job(type="enrich_grants", user_id=u.id, payload={"user_id": str(u.id), "orcid": u.orcid}); db_session.add(job); await db_session.flush()

    calls = []
    async def fake_search(criteria, fields, max_total=500):
        calls.append(criteria)
        if "pi_names" in criteria:
            return [_row("R01AI137329", 2019, 9751245), _row("R01ZZ000009", 2019, 555)]
        return [_row("R01AI137329", 2017, 9751245), _row("R01AI137329", 2019, 9751245), _row("R21AI190702", 2026, 9751245)]
    async def fake_links(cores):
        return {"R01AI137329": {"34187885"}, "R01ZZ000009": {"1"}}
    monkeypatch.setattr(ge, "search_projects", fake_search)
    monkeypatch.setattr(ge, "publications_for_cores", fake_links)

    await ge.execute_enrich_grants(job, db_session)

    grants = (await db_session.execute(select(PiGrant).where(PiGrant.user_id == u.id))).scalars().all()
    assert {g.core_project_num for g in grants} == {"R01AI137329", "R21AI190702"}
    r01 = next(g for g in grants if g.core_project_num == "R01AI137329")
    assert r01.first_fy == 2019 and r01.tenure_filter_mode == "org_and_year" and r01.identity_evidence["pmid_linked"] == [9751245]
    prof = (await db_session.execute(select(ResearcherProfile).where(ResearcherProfile.user_id == u.id))).scalar_one()
    assert prof.grant_titles == ["T R21AI190702", "T R01AI137329"]
    assert calls[1]["org_names_exact_match"] == ["JOHNS HOPKINS UNIVERSITY"] and calls[1]["pi_profile_ids"] == [9751245]


async def test_no_candidates_completes_without_rows(db_session, monkeypatch):
    u = User(orcid="0000-0001-0000-0001", name="Nobody Here", user_role="pi"); db_session.add(u); await db_session.flush()
    job = Job(type="enrich_grants", user_id=u.id, payload={"user_id": str(u.id), "orcid": u.orcid}); db_session.add(job); await db_session.flush()
    async def none(*a, **k): return []
    monkeypatch.setattr(ge, "search_projects", none)
    await ge.execute_enrich_grants(job, db_session)
    assert (await db_session.execute(select(PiGrant).where(PiGrant.user_id == u.id))).scalars().all() == []
    assert "no_reporter_match" in (job.payload.get("progress") or [""])[-1]
```

- [ ] **Step 2: Run to verify failure** → FAIL, module missing.

- [ ] **Step 3: Implement**

```python
# src/services/grant_enrichment.py
"""Worker handler for the ``enrich_grants`` job (RePORTER → pi_grants → grant_titles)."""
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import AgentRegistry, Job, PiGrant, Publication, ResearcherProfile, User
from src.services.grant_resolution import derive_grant_titles, filter_and_collapse, resolve_profile_ids
from src.services.jhu_rules import get_tenure_start
from src.services.nih_reporter import JHU_ORG_EXACT, PROJECT_FIELDS, publications_for_cores, search_projects
from src.services.profile_pipeline import append_job_progress

logger = logging.getLogger(__name__)


def _split_name(name: str) -> tuple[str, str]:
    parts = (name or "").split()
    return (parts[0] if parts else ""), (parts[-1] if parts else "")


async def enqueue_enrichment_jobs(db: AsyncSession, user_id: uuid.UUID, orcid: str) -> None:
    for jtype in ("enrich_grants", "industry_evidence"):
        pending = await db.execute(select(Job.id).where(Job.user_id == user_id, Job.type == jtype, Job.status.in_(("pending", "processing"))))
        if pending.scalar_one_or_none() is None:
            db.add(Job(type=jtype, user_id=user_id, payload={"user_id": str(user_id), "orcid": orcid}))


async def execute_enrich_grants(job: Job, db: AsyncSession) -> None:
    user_id = uuid.UUID(job.payload["user_id"])
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
    agent = (await db.execute(select(AgentRegistry).where(AgentRegistry.user_id == user_id))).scalar_one_or_none()
    tenure_start = await get_tenure_start(db, user_id, agent_id=agent.agent_id if agent else None)
    first, last = _split_name(user.name)

    append_job_progress(job, "grants1", f"RePORTER name search for {last}")
    candidates = await search_projects(
        {"pi_names": [{"last_name": last}], "org_names_exact_match": [JHU_ORG_EXACT]}, PROJECT_FIELDS, max_total=2000)
    if not candidates:
        append_job_progress(job, "grants_done", "no_reporter_match: no JHU projects for this surname"); return

    corpus_pmids = {p for (p,) in (await db.execute(select(Publication.pmid).where(Publication.user_id == user_id, Publication.pmid.isnot(None)))).all()}
    cores = sorted({r["core_project_num"] for r in candidates})
    links = await publications_for_cores(cores)
    profile_ids, evidence = resolve_profile_ids(candidates, corpus_pmids, links, first)
    if not profile_ids:
        append_job_progress(job, "grants_done", f"no_reporter_match: {len(cores)} candidate cores, none PMID-linked; rejected={evidence['rejected']}"); return

    criteria = {"pi_profile_ids": sorted(profile_ids), "org_names_exact_match": [JHU_ORG_EXACT]}
    if tenure_start is not None:
        criteria["fiscal_years"] = list(range(tenure_start, datetime.now(timezone.utc).year + 2))
    rows = await search_projects(criteria, PROJECT_FIELDS, max_total=1500)
    records, mode = filter_and_collapse(rows, profile_ids, tenure_start)

    vetoed = {c for (c,) in (await db.execute(select(PiGrant.core_project_num).where(PiGrant.user_id == user_id, PiGrant.vetoed_at.isnot(None)))).all()}
    await db.execute(delete(PiGrant).where(PiGrant.user_id == user_id, PiGrant.vetoed_at.is_(None)))
    kept = [r for r in records if r.core_project_num not in vetoed]
    for r in kept:
        db.add(PiGrant(user_id=user_id, tenure_filter_mode=mode, identity_evidence=evidence, **r.__dict__))

    profile = (await db.execute(select(ResearcherProfile).where(ResearcherProfile.user_id == user_id))).scalar_one_or_none()
    if profile is not None:
        profile.grant_titles = derive_grant_titles(kept)
    await db.flush()
    append_job_progress(job, "grants_done",
        f"profile_ids={sorted(profile_ids)} cores={len(kept)} mode={mode} rows_seen={len(rows)} vetoed_kept={len(vetoed)}")
```

Dispatch in `src/worker/main.py` (after the `review_feedback_analysis` branch):

```python
            elif job.type == "enrich_grants":
                from src.services.grant_enrichment import execute_enrich_grants
                await execute_enrich_grants(job, db)
            elif job.type == "industry_evidence":
                from src.services.industry_evidence import execute_industry_evidence
                await execute_industry_evidence(job, db)
```

(`industry_evidence` module lands in Task 8; until then import the name lazily as shown so the worker still boots.)

In `src/services/profile_pipeline.py`, immediately after the step-9 profile commit (the `await db.commit()` that follows `update_progress("step9", ...)`), add:

```python
    from src.services.grant_enrichment import enqueue_enrichment_jobs
    await enqueue_enrichment_jobs(db, user.id, orcid_id)
    await db.commit()
    update_progress("step10", "Enqueued grant + industry enrichment jobs")
```

Also replace the ORCID fundings result at `:110` with a **merge**: keep `grant_titles = await fetch_orcid_grants(orcid_id)` as the seed (RePORTER overwrites it when the job completes).

- [ ] **Step 4: Run tests**

Run: `.venv-test/bin/python -m pytest tests/unit/test_grant_enrichment_job.py tests/unit/test_pipeline_corpus_integration.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/services/grant_enrichment.py src/worker/main.py src/services/profile_pipeline.py tests/unit/test_grant_enrichment_job.py
git commit -m "feat(enrichment): enrich_grants worker job, enqueued after profile generation"
```

---

### Task 5: Manager UI — grants panel + veto route

**Files:**
- Modify: `src/services/directory.py:207` (`load_user_detail` adds `grants`)
- Modify: `src/routers/manager.py` (new `POST /pis/{user_id}/grants/{grant_id}/veto`)
- Modify: `templates/manager/pi_detail.html` (new panel before `<!-- Publications -->` at `:285`)
- Modify: `tests/integration/test_manager_views.py:62-69` (allowlist +1)
- Test: `tests/integration/test_manager_grants_panel.py`

**Interfaces:**
- Produces: `detail["grants"]: list[PiGrant]` (non-vetoed first, ordered `last_fy desc`); veto route sets `vetoed_at=now()` and re-projects `grant_titles` via `derive_grant_titles`.

- [ ] **Step 1: Failing tests**

```python
# tests/integration/test_manager_grants_panel.py
import pytest
from sqlalchemy import select
from src.models import PiGrant, ResearcherProfile
from tests.integration.test_manager_views import make_manager, make_pi, login_as  # reuse existing helpers

pytestmark = pytest.mark.integration


async def test_grants_panel_lists_rows_with_evidence_badge(client, db_session):
    pi = await make_pi(db_session); mgr = await make_manager(db_session)
    db_session.add(PiGrant(user_id=pi.id, core_project_num="R01AI137329", title="Beta-lactam resistance", org_name="JOHNS HOPKINS UNIVERSITY",
                           activity_code="R01", first_fy=2019, last_fy=2024, tenure_filter_mode="org_and_year", identity_evidence={"pmid_linked": [9751245]}))
    await db_session.commit(); await login_as(client, mgr)
    html = (await client.get(f"/manager/pis/{pi.id}")).text
    assert "Beta-lactam resistance" in html and "PMID-linked" in html and "R01AI137329" in html


async def test_veto_hides_grant_and_drops_it_from_grant_titles(client, db_session):
    pi = await make_pi(db_session); mgr = await make_manager(db_session)
    db_session.add(ResearcherProfile(user_id=pi.id, grant_titles=["Wrong person grant"]))
    g = PiGrant(user_id=pi.id, core_project_num="R01XX000001", title="Wrong person grant", org_name="JOHNS HOPKINS UNIVERSITY",
                activity_code="R01", tenure_filter_mode="org_only"); db_session.add(g); await db_session.commit(); await login_as(client, mgr)
    r = await client.post(f"/manager/pis/{pi.id}/grants/{g.id}/veto", data={})
    assert r.status_code == 302
    await db_session.refresh(g); assert g.vetoed_at is not None
    prof = (await db_session.execute(select(ResearcherProfile).where(ResearcherProfile.user_id == pi.id))).scalar_one()
    await db_session.refresh(prof); assert prof.grant_titles == []
```

(If `make_manager`/`make_pi`/`login_as` are not the helper names in `test_manager_views.py`, use that file's actual fixtures — read its first 50 lines — the shape is what matters.)

- [ ] **Step 2: Run → FAIL** (404 on veto route; panel text absent).

- [ ] **Step 3: Implement**

`directory.py::load_user_detail` — add after the publications query:

```python
    grants = (await db.execute(
        select(PiGrant).where(PiGrant.user_id == user_id)
        .order_by(PiGrant.vetoed_at.is_(None).desc(), PiGrant.last_fy.desc().nullslast())
    )).scalars().all()
    ...
    return {..., "grants": grants}
```

`manager.py` — pass `grants=detail["grants"]` in `manager_pi_detail`'s context and add:

```python
@router.post("/pis/{user_id}/grants/{grant_id}/veto")
async def manager_veto_grant(user_id: uuid.UUID, grant_id: uuid.UUID, request: Request,
                             db: AsyncSession = _DB, current_user: User = _STAFF):
    """Mark a RePORTER grant as 'not this PI'. Persisted; re-runs respect it."""
    grant = (await db.execute(select(PiGrant).where(PiGrant.id == grant_id, PiGrant.user_id == user_id))).scalar_one_or_none()
    if grant is None:
        raise HTTPException(status_code=404, detail="Grant not found")
    grant.vetoed_at = datetime.now(timezone.utc)
    remaining = (await db.execute(select(PiGrant).where(PiGrant.user_id == user_id, PiGrant.vetoed_at.is_(None)))).scalars().all()
    profile = (await db.execute(select(ResearcherProfile).where(ResearcherProfile.user_id == user_id))).scalar_one_or_none()
    if profile is not None:
        profile.grant_titles = derive_grant_titles([GrantRecord(**{k: getattr(g, k) for k in GrantRecord.__dataclass_fields__}) for g in remaining])
    await db.commit()
    return RedirectResponse(url=f"/manager/pis/{user_id}#grants", status_code=302)
```

(`_STAFF` = the same dependency the six existing manager POSTs use — read `manager.py` for the module-level alias name; the mute route uses it.)

Template panel (insert before `<!-- Publications -->`):

```html
    <!-- NIH grants (RePORTER) -->
    <section id="grants" class="bg-white rounded-lg shadow p-6 mb-6">
      <h2 class="font-semibold text-gray-800 mb-1">NIH grants at JHU ({{ grants | selectattr('vetoed_at', 'none') | list | length }})</h2>
      <p class="text-xs text-gray-500 mb-4">Source: NIH RePORTER. Tenure filter:
        {% if grants and grants[0].tenure_filter_mode == 'org_only' %}<span class="text-amber-700">organisation only — no JHU tenure start recorded</span>{% else %}organisation and fiscal year ≥ tenure start{% endif %}.
        Vetoing a grant removes it from the profile's grant list and keeps it out on re-runs.</p>
      {% if not grants %}<p class="text-sm text-gray-500">No RePORTER match yet (job pending, or no JHU award found).</p>{% endif %}
      <ul class="divide-y">
      {% for g in grants %}
        <li class="py-2 flex justify-between gap-4 {% if g.vetoed_at %}opacity-50{% endif %}">
          <div>
            <div class="font-medium">{{ g.title }}</div>
            <div class="text-xs text-gray-500">{{ g.core_project_num }} · {{ g.activity_code }} · FY{{ g.first_fy }}–{{ g.last_fy }}
              {% if g.is_subproject %}· subproject{% endif %}
              · {% if g.identity_evidence and g.identity_evidence.pmid_linked %}<span class="text-green-700">PMID-linked</span>{% elif g.identity_evidence and g.identity_evidence.unique_name %}<span class="text-amber-700">unique name</span>{% endif %}
            </div>
          </div>
          {% if not g.vetoed_at %}
          <form method="post" action="/manager/pis/{{ target_user.id }}/grants/{{ g.id }}/veto"><button class="text-xs text-red-700 underline">Not this PI</button></form>
          {% else %}<span class="text-xs text-gray-400">vetoed</span>{% endif %}
        </li>
      {% endfor %}
      </ul>
    </section>
```

Add `"/pis/{user_id}/grants/{grant_id}/veto"` to `allowed_post_paths` in `test_manager_views.py` and update its docstring ("seven … the grant veto joined 2026-09-11").

- [ ] **Step 4: Run** `.venv-test/bin/python -m pytest tests/integration/test_manager_grants_panel.py tests/integration/test_manager_views.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/services/directory.py src/routers/manager.py templates/manager/pi_detail.html tests/integration/test_manager_grants_panel.py tests/integration/test_manager_views.py
git commit -m "feat(enrichment): manager grants panel with per-grant 'not this PI' veto"
```

---

### Task 6: Industry evidence collectors (OpenAlex + PubMed COI), pure parsers first

**Files:**
- Create: `src/services/industry_sources/__init__.py`
- Create: `src/services/industry_sources/companies.py` (classifier)
- Create: `src/services/industry_sources/openalex_industry.py`
- Create: `src/services/industry_sources/pubmed_coi.py`
- Create: `data/industry_vendor_blocklist.yaml` (curated; starts small)
- Test: `tests/unit/test_industry_sources_parsers.py`

**Interfaces:**
- Produces:
  - `@dataclass EvidenceItem(source, kind, external_id, company_name, company_external_id, company_class, year, pi_role, in_tenure, evidence: dict)`.
  - `JHU_OPENALEX_IDS = frozenset({"I145311948","I2799853436","I4210150714","I2802946424","I2802697821","I4210098865","I4210129832","I4210092215","I4389425327","I4210114877"})`.
  - `classify_company(name: str, openalex_type: str|None) -> str` → one of `COMPANY_CLASSES`.
  - `openalex_industry.evidence_from_work(work: dict, pi_orcid: str, tenure_start: int|None) -> list[EvidenceItem]` — returns `[]` unless the PI's own authorship on this work is JHU-affiliated (OpenAlex ids OR `is_hopkins_affiliation` on raw strings) AND `publication_year >= tenure_start`; one `coauthor_company` item per distinct company institution (type `company`), one `company_funder` per funder whose institution role is a company (resolved by the caller; parser takes `company_funder_ids: set[str]`).
  - `openalex_industry.fetch_works_for_pmids(pmids: list[str]) -> list[dict]` — batches `filter=pmid:a|b|…` 50 at a time with `select=id,ids,title,publication_year,authorships,funders,primary_topic`, `mailto` from `settings.ncbi_contact_email`.
  - `pubmed_coi.evidence_from_record(rec: dict, pi_year_ok: bool) -> list[EvidenceItem]` — parses `coi_statement` for positive statements naming companies; also `affiliations` strings with a company suffix regex. Extends `src/services/pubmed.py::_parse_pubmed_xml` to also emit `coi_statement` (add `rec["coi_statement"] = art.findtext(".//CoiStatement")`).

- [ ] **Step 1: Failing tests**

```python
# tests/unit/test_industry_sources_parsers.py
from src.services.industry_sources.companies import classify_company
from src.services.industry_sources.openalex_industry import evidence_from_work
from src.services.industry_sources.pubmed_coi import evidence_from_record

PI = "https://orcid.org/0000-0002-2214-0114"
def work(year, pi_inst_ids, companies, pi_raw=None, n_authors=3, pi_pos="last"):
    auths = [{"author": {"orcid": None}, "institutions": [{"id": f"https://openalex.org/{c}", "display_name": n, "type": "company"}],
              "author_position": "middle", "is_corresponding": False} for c, n in companies]
    auths.append({"author": {"orcid": PI}, "institutions": [{"id": f"https://openalex.org/{i}", "display_name": "JHU", "type": "education"} for i in pi_inst_ids],
                  "raw_affiliation_strings": pi_raw or [], "author_position": pi_pos, "is_corresponding": pi_pos == "last"})
    auths += [{"author": {"orcid": None}, "institutions": [], "author_position": "middle"}] * (n_authors - len(auths))
    return {"id": "https://openalex.org/W1", "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/1"}, "publication_year": year, "authorships": auths, "funders": []}


def test_company_coauthor_in_tenure_with_jhu_pi_affiliation():
    items = evidence_from_work(work(2021, ["I145311948"], [("I4210091798", "Paratek Pharmaceuticals (United States)")]), PI, 2018, company_funder_ids=set())
    assert len(items) == 1 and items[0].kind == "coauthor_company" and items[0].in_tenure and items[0].pi_role == "corresponding"


def test_pre_tenure_year_yields_nothing():
    assert evidence_from_work(work(2015, ["I145311948"], [("I1", "Pfizer")]), PI, 2018, company_funder_ids=set()) == []


def test_in_window_year_but_pi_not_at_jhu_yields_nothing():
    assert evidence_from_work(work(2020, ["I999"], [("I1", "Pfizer")]), PI, 2018, company_funder_ids=set()) == []


def test_raw_affiliation_string_fallback_counts_as_jhu():
    items = evidence_from_work(work(2020, [], [("I1", "Pfizer")], pi_raw=["Dept of Medicine, Johns Hopkins University School of Medicine"]), PI, 2018, company_funder_ids=set())
    assert len(items) == 1


def test_consortium_paper_is_downweighted_in_evidence_payload():
    items = evidence_from_work(work(2020, ["I145311948"], [("I1", "Pfizer")], n_authors=60, pi_pos="middle"), PI, 2018, company_funder_ids=set())
    assert items[0].evidence["author_count"] == 60 and items[0].pi_role == "middle"


def test_vendor_classified_as_cro_vendor():
    assert classify_company("Applied BioPhysics (United States)", "company") == "cro_vendor"
    assert classify_company("Paratek Pharmaceuticals (United States)", "company") == "pharma_biotech"
    assert classify_company("Medtronic", "company") == "device_dx"
    assert classify_company("Something Ltd", "company") == "other"


def test_coi_positive_statement_extracts_company_and_relationship():
    rec = {"pmid": "38980071", "year": 2024, "coi_statement": "Daniel H. Deck and Alisa W. Serio are employees of Paratek Pharmaceuticals, Inc. All authors vouch for the integrity.",
           "affiliations": ["Paratek Pharmaceuticals Inc., King of Prussia, Pennsylvania, USA."]}
    items = evidence_from_record(rec, pi_year_ok=True)
    kinds = {i.kind for i in items}
    assert "coi_relationship" in kinds
    coi = next(i for i in items if i.kind == "coi_relationship")
    assert "Paratek" in coi.company_name and coi.evidence["relationship"] == "employee" and coi.evidence["span"].startswith("Daniel")


def test_coi_negative_statement_yields_nothing():
    assert evidence_from_record({"pmid": "1", "year": 2024, "coi_statement": "The authors declare no competing interests.", "affiliations": []}, pi_year_ok=True) == []
```

- [ ] **Step 2: Run → FAIL** (package missing).

- [ ] **Step 3: Implement**

```python
# src/services/industry_sources/__init__.py
from dataclasses import dataclass, field

@dataclass
class EvidenceItem:
    source: str
    kind: str
    external_id: str
    company_name: str | None
    company_external_id: str | None
    company_class: str
    year: int | None
    pi_role: str | None
    in_tenure: bool
    evidence: dict = field(default_factory=dict)

JHU_OPENALEX_IDS = frozenset({
    "I145311948", "I2799853436", "I4210150714", "I2802946424", "I2802697821",
    "I4210098865", "I4210129832", "I4210092215", "I4389425327", "I4210114877",
})
```

```python
# src/services/industry_sources/companies.py
"""Company classification: pharma/biotech vs device/dx vs CRO/vendor vs other.

Rule-based on purpose (adversarial A2): OpenAlex says only ``type: company``;
a reagent vendor and a pharma are both companies. The vendor list is curated
YAML under data/ so a manager can grow it without a code change."""
import re
from functools import lru_cache
from pathlib import Path

import yaml

_VENDOR_YAML = Path(__file__).resolve().parents[3] / "data" / "industry_vendor_blocklist.yaml"
_PHARMA = re.compile(r"\b(pharma\w*|therapeutics|biotherapeutics|biosciences|biopharma\w*|oncology|vaccines?|genomics|biologics|"
                     r"pfizer|merck|novartis|roche|genentech|astrazeneca|glaxosmithkline|gsk|sanofi|bayer|abbvie|amgen|gilead|lilly|"
                     r"bristol|takeda|regeneron|moderna|biontech|vertex|biogen|boehringer|paratek|xtalpi|collaborations pharmaceuticals)\b", re.I)
_DEVICE = re.compile(r"\b(medtronic|boston scientific|abbott|stryker|siemens healthineers|philips|ge healthcare|becton|dickinson|"
                     r"illumina|diagnostics?|medical devices?|imaging|dexcom|intuitive surgical|edwards lifesciences)\b", re.I)


@lru_cache(maxsize=1)
def _vendor_terms() -> list[str]:
    if not _VENDOR_YAML.exists():
        return []
    data = yaml.safe_load(_VENDOR_YAML.read_text()) or {}
    return [t.lower() for t in data.get("vendors", [])]


def classify_company(name: str, openalex_type: str | None) -> str:
    n = (name or "").lower()
    if any(v in n for v in _vendor_terms()):
        return "cro_vendor"
    if _DEVICE.search(n):
        return "device_dx"
    if _PHARMA.search(n):
        return "pharma_biotech"
    return "other" if openalex_type == "company" or n else "unknown"
```

```yaml
# data/industry_vendor_blocklist.yaml
# Companies that co-author as reagent/instrument/CRO vendors, not as scientific partners.
vendors:
  - applied biophysics
  - thermo fisher
  - bio-rad
  - agilent
  - promega
  - qiagen
  - sigma-aldrich
  - millipore
  - new england biolabs
  - charles river
  - labcorp
  - iqvia
  - covance
  - genscript
  - twist bioscience
  - integrated dna technologies
  - 10x genomics
  - bruker
  - waters corporation
  - zeiss
  - leica
  - nikon
  - olympus
```

```python
# src/services/industry_sources/openalex_industry.py
"""OpenAlex-derived industry evidence: company co-authors and company funders."""
import logging
import httpx

from src.config import get_settings
from src.services.industry_sources import JHU_OPENALEX_IDS, EvidenceItem
from src.services.industry_sources.companies import classify_company
from src.services.jhu_rules import is_hopkins_affiliation

logger = logging.getLogger(__name__)
OA = "https://api.openalex.org"
_SELECT = "id,ids,title,publication_year,authorships,funders,primary_topic"


def _oaid(url: str | None) -> str:
    return (url or "").rsplit("/", 1)[-1]


def _pi_authorship(work: dict, pi_orcid: str) -> dict | None:
    tail = pi_orcid.rsplit("/", 1)[-1]
    for a in work.get("authorships") or []:
        if (a.get("author") or {}).get("orcid", "") and a["author"]["orcid"].endswith(tail):
            return a
    return None


def _pi_at_jhu(auth: dict) -> bool:
    if any(_oaid(i.get("id")) in JHU_OPENALEX_IDS for i in auth.get("institutions") or []):
        return True
    return any(is_hopkins_affiliation(s) for s in auth.get("raw_affiliation_strings") or [])


def _role(auth: dict) -> str:
    if auth.get("is_corresponding"):
        return "corresponding"
    return auth.get("author_position") or "middle"


def evidence_from_work(work: dict, pi_orcid: str, tenure_start: int | None, *, company_funder_ids: set[str]) -> list[EvidenceItem]:
    year = work.get("publication_year")
    if tenure_start is None or not year or year < tenure_start:
        return []
    me = _pi_authorship(work, pi_orcid)
    if me is None or not _pi_at_jhu(me):
        return []
    wid = _oaid(work.get("id"))
    n_authors = len(work.get("authorships") or [])
    base = {"work_id": wid, "pmid": _oaid((work.get("ids") or {}).get("pmid")), "title": work.get("title"), "author_count": n_authors}
    items: list[EvidenceItem] = []
    seen: set[str] = set()
    for a in work.get("authorships") or []:
        if a is me:
            continue
        for inst in a.get("institutions") or []:
            if inst.get("type") != "company":
                continue
            cid = _oaid(inst.get("id"))
            if cid in seen:
                continue
            seen.add(cid)
            items.append(EvidenceItem("openalex", "coauthor_company", f"{wid}:{cid}", inst.get("display_name"), cid,
                                      classify_company(inst.get("display_name", ""), "company"), year, _role(me), True, dict(base)))
    for f in work.get("funders") or []:
        fid = _oaid(f.get("id"))
        if fid in company_funder_ids:
            items.append(EvidenceItem("openalex", "company_funder", f"{wid}:{fid}", f.get("display_name"), fid,
                                      classify_company(f.get("display_name", ""), "company"), year, _role(me), True, dict(base)))
    return items


async def fetch_works_for_pmids(pmids: list[str]) -> list[dict]:
    out: list[dict] = []
    contact = getattr(get_settings(), "ncbi_contact_email", None)
    async with httpx.AsyncClient(timeout=30) as client:
        for i in range(0, len(pmids), 50):
            chunk = "|".join(pmids[i:i + 50])
            params = {"filter": f"pmid:{chunk}", "select": _SELECT, "per-page": 50}
            if contact:
                params["mailto"] = contact
            resp = await client.get(f"{OA}/works", params=params)
            resp.raise_for_status()
            out.extend(resp.json().get("results") or [])
    return out


async def company_funder_ids(funder_ids: set[str]) -> set[str]:
    """Funder ids whose entity also has an ``institution`` role of type company (e.g. GSK)."""
    if not funder_ids:
        return set()
    hits: set[str] = set()
    contact = getattr(get_settings(), "ncbi_contact_email", None)
    async with httpx.AsyncClient(timeout=30) as client:
        for i in range(0, len(funder_ids), 50):
            chunk = "|".join(sorted(funder_ids)[i:i + 50])
            params = {"filter": f"ids.openalex:{chunk}", "select": "id,display_name,roles", "per-page": 50}
            if contact:
                params["mailto"] = contact
            resp = await client.get(f"{OA}/funders", params=params)
            resp.raise_for_status()
            for f in resp.json().get("results") or []:
                inst_ids = [_oaid(r.get("id")) for r in f.get("roles") or [] if r.get("role") == "institution"]
                if not inst_ids:
                    continue
                r2 = await client.get(f"{OA}/institutions", params={"filter": f"ids.openalex:{'|'.join(inst_ids)}", "select": "id,type", **({"mailto": contact} if contact else {})})
                r2.raise_for_status()
                if any(i.get("type") == "company" for i in r2.json().get("results") or []):
                    hits.add(_oaid(f.get("id")))
    return hits
```

```python
# src/services/industry_sources/pubmed_coi.py
"""Conflict-of-interest statements and company affiliations from PubMed records."""
import re

from src.services.industry_sources import EvidenceItem
from src.services.industry_sources.companies import classify_company

_NEG = re.compile(r"\b(no|none|declare[sd]? no|not have any|have no)\b.*\b(conflict|competing|interest)", re.I)
_COMPANY_SUFFIX = re.compile(r"\b([A-Z][\w&.\-' ]{1,60}?(?:,? Inc\.?|,? LLC|,? Ltd\.?|,? GmbH|,? AG|,? S\.?A\.?|,? Corp\.?|,? Co\.?|Pharmaceuticals?|Therapeutics|Biosciences|Biotech\w*|Bio\b|Diagnostics))", re.M)
_REL = [
    ("founder", re.compile(r"\b(co-?founder|founded)\b", re.I)),
    ("equity", re.compile(r"\b(equity|shareholder|stock|shares|ownership)\b", re.I)),
    ("employee", re.compile(r"\b(employee|employed by|employment)\b", re.I)),
    ("consultant", re.compile(r"\b(consult\w*|advisory board|scientific advisor|SAB|honorari\w*|speaker)\b", re.I)),
    ("inventor", re.compile(r"\b(patent|inventor|licens\w*|royalt\w*)\b", re.I)),
    ("funding", re.compile(r"\b(research (support|funding|grant)|sponsored|funded by)\b", re.I)),
]


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.;])\s+", text or "") if s.strip()]


def evidence_from_record(rec: dict, *, pi_year_ok: bool) -> list[EvidenceItem]:
    if not pi_year_ok:
        return []
    items: list[EvidenceItem] = []
    pmid = str(rec.get("pmid"))
    year = rec.get("year")
    seen: set[str] = set()
    for sent in _sentences(rec.get("coi_statement") or ""):
        if _NEG.search(sent):
            continue
        companies = [m.group(1).strip(" ,.") for m in _COMPANY_SUFFIX.finditer(sent)]
        if not companies:
            continue
        rel = next((name for name, rx in _REL if rx.search(sent)), "other")
        for c in companies:
            key = f"{pmid}:{c.lower()}"
            if key in seen:
                continue
            seen.add(key)
            items.append(EvidenceItem("pubmed", "coi_relationship", key, c, None, classify_company(c, "company"), year, None, True,
                                      {"relationship": rel, "span": sent[:400], "pmid": pmid}))
    return items
```

Extend `_parse_pubmed_xml` in `src/services/pubmed.py` to add `record["coi_statement"] = (art.findtext(".//CoiStatement") or "").strip() or None` and to keep the `affiliations` list per author (it already collects them for `match_pi_author`; verify with `grep -n affiliations src/services/pubmed.py`).

- [ ] **Step 4: Run tests** → PASS (8). Also run `tests/unit/test_corpus.py` to make sure the PubMed parser change broke nothing.

- [ ] **Step 5: Commit**

```bash
git add src/services/industry_sources data/industry_vendor_blocklist.yaml src/services/pubmed.py tests/unit/test_industry_sources_parsers.py
git commit -m "feat(enrichment): industry evidence parsers — OpenAlex company co-authors/funders, PubMed COI"
```

---

### Task 7: USPTO and ClinicalTrials.gov collectors

**Files:**
- Create: `src/services/industry_sources/uspto_inventor.py`
- Create: `src/services/industry_sources/ctgov.py`
- Test: `tests/unit/test_industry_sources_uspto_ctgov.py`

**Interfaces:**
- Produces:
  - `uspto_inventor.fetch_jhu_applications(inventor_full_name: str) -> list[dict]` — POST `https://api.uspto.gov/api/v1/patent/applications/search` with header `X-API-KEY` = `settings.uspto_api_key`, body `{"q": 'applicationMetaData.firstInventorName:"<name>" AND applicationMetaData.applicantBag.applicantNameText:"Johns Hopkins"', "pagination": {"offset":0,"limit":100}, "fields": [...]}`; returns `patentFileWrapperDataBag`.
  - `uspto_inventor.evidence_from_application(app: dict, tenure_start: int|None, pi_keywords: set[str]) -> list[EvidenceItem]` — `patent_filed` if JHU in applicantBag and filing year ≥ tenure and title shares ≥1 keyword token with `pi_keywords` (adversarial C14); additionally `patent_assigned` for each `assignmentBag` assignee or co-applicant whose name classifies as a company.
  - `ctgov.fetch_jhu_industry_trials(pi_name: str) -> list[dict]` — GET `https://clinicaltrials.gov/api/v2/studies?query.term=AREA[OverallOfficialName]"<name>" AND AREA[CollaboratorClass]INDUSTRY&fields=NCTId,BriefTitle,LeadSponsorName,LeadSponsorClass,CollaboratorName,CollaboratorClass,OverallOfficialName,StartDate,Condition&pageSize=100`.
  - `ctgov.evidence_from_study(study: dict, tenure_start, pi_conditions: set[str]) -> list[EvidenceItem]` — `trial_industry_collab` only when the lead sponsor name contains "Johns Hopkins" (or "Sidney Kimmel"), `StartDate` year ≥ tenure, and a condition overlaps `pi_conditions`; industry-led trials yield `[]`.

- [ ] **Step 1: Failing tests**

```python
# tests/unit/test_industry_sources_uspto_ctgov.py
from src.services.industry_sources.uspto_inventor import evidence_from_application
from src.services.industry_sources.ctgov import evidence_from_study

def app(filing="2022-12-01", applicants=("The Johns Hopkins University",), title="OXAZOLIDINONE FOR TREATMENT OF MYCOBACTERIUM TUBERCULOSIS", assignees=()):
    return {"applicationNumberText": "17123456",
            "applicationMetaData": {"filingDate": filing, "inventionTitle": title, "firstInventorName": "Gyanu Lamichhane",
                                    "applicantBag": [{"applicantNameText": a} for a in applicants]},
            "assignmentBag": [{"assigneeBag": [{"assigneeNameText": s}]} for s in assignees]}

def test_jhu_filed_in_tenure_with_keyword_overlap_is_patent_filed():
    items = evidence_from_application(app(), 2018, {"tuberculosis", "beta-lactam"})
    assert [i.kind for i in items] == ["patent_filed"] and items[0].pi_role == "inventor"

def test_no_jhu_applicant_yields_nothing():
    assert evidence_from_application(app(applicants=("University of St. Thomas",)), 2018, {"tuberculosis"}) == []

def test_pre_tenure_filing_yields_nothing():
    assert evidence_from_application(app(filing="2016-01-01"), 2018, {"tuberculosis"}) == []

def test_no_keyword_overlap_yields_nothing():
    assert evidence_from_application(app(title="WIDGET"), 2018, {"tuberculosis"}) == []

def test_company_assignee_adds_patent_assigned():
    items = evidence_from_application(app(assignees=("Paratek Pharmaceuticals, Inc.",)), 2018, {"tuberculosis"})
    assert {i.kind for i in items} == {"patent_filed", "patent_assigned"}

def study(lead="Sidney Kimmel Comprehensive Cancer Center at Johns Hopkins", lead_class="OTHER", collab=("Novartis Pharmaceuticals",), start="2020-09", cond=("Lymphoma",)):
    return {"protocolSection": {"identificationModule": {"nctId": "NCT01665768", "briefTitle": "T"},
            "statusModule": {"startDateStruct": {"date": start}},
            "sponsorCollaboratorsModule": {"leadSponsor": {"name": lead, "class": lead_class}, "collaborators": [{"name": c, "class": "INDUSTRY"} for c in collab]},
            "conditionsModule": {"conditions": list(cond)}}}

def test_jhu_led_trial_with_industry_collaborator_counts():
    items = evidence_from_study(study(), 2018, {"lymphoma"})
    assert len(items) == 1 and items[0].kind == "trial_industry_collab" and items[0].company_name == "Novartis Pharmaceuticals"

def test_industry_led_trial_yields_nothing():
    assert evidence_from_study(study(lead="Novartis Pharmaceuticals", lead_class="INDUSTRY"), 2018, {"lymphoma"}) == []

def test_condition_mismatch_yields_nothing():
    assert evidence_from_study(study(cond=("Psoriasis",)), 2018, {"lymphoma"}) == []
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**

```python
# src/services/industry_sources/uspto_inventor.py
import re
import httpx
from src.config import get_settings
from src.services.industry_sources import EvidenceItem
from src.services.industry_sources.companies import classify_company

SEARCH_URL = "https://api.uspto.gov/api/v1/patent/applications/search"
_FIELDS = ["applicationNumberText", "applicationMetaData.inventionTitle", "applicationMetaData.filingDate",
           "applicationMetaData.firstInventorName", "applicationMetaData.applicantBag", "assignmentBag"]
_TOKEN = re.compile(r"[a-z][a-z0-9\-]{3,}")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall((text or "").lower()))


def _is_jhu(name: str) -> bool:
    return "johns hopkins" in (name or "").lower()


async def fetch_jhu_applications(inventor_full_name: str) -> list[dict]:
    key = get_settings().uspto_api_key
    if not key:
        return []
    q = f'applicationMetaData.firstInventorName:"{inventor_full_name}" AND applicationMetaData.applicantBag.applicantNameText:"Johns Hopkins"'
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(SEARCH_URL, headers={"X-API-KEY": key}, json={"q": q, "pagination": {"offset": 0, "limit": 100}, "fields": _FIELDS})
        resp.raise_for_status()
        return resp.json().get("patentFileWrapperDataBag") or []


def evidence_from_application(app: dict, tenure_start: int | None, pi_keywords: set[str]) -> list[EvidenceItem]:
    meta = app.get("applicationMetaData") or {}
    applicants = [a.get("applicantNameText", "") for a in meta.get("applicantBag") or []]
    if not any(_is_jhu(a) for a in applicants):
        return []
    filing = meta.get("filingDate") or ""
    year = int(filing[:4]) if filing[:4].isdigit() else None
    if tenure_start is None or year is None or year < tenure_start:
        return []
    title = meta.get("inventionTitle") or ""
    kw = {k.lower() for k in pi_keywords}
    if not (_tokens(title) & {t for k in kw for t in _tokens(k)}):
        return []
    appno = app.get("applicationNumberText") or title[:40]
    base = {"title": title, "filing_date": filing, "applicants": applicants}
    items = [EvidenceItem("uspto", "patent_filed", appno, None, None, "unknown", year, "inventor", True, dict(base))]
    companies = [a for a in applicants if not _is_jhu(a) and classify_company(a, "company") != "other"]
    for bag in app.get("assignmentBag") or []:
        companies += [s.get("assigneeNameText", "") for s in bag.get("assigneeBag") or [] if not _is_jhu(s.get("assigneeNameText", ""))]
    for c in dict.fromkeys(c for c in companies if c):
        cls = classify_company(c, "company")
        if cls in ("pharma_biotech", "device_dx", "other"):
            items.append(EvidenceItem("uspto", "patent_assigned", f"{appno}:{c.lower()}", c, None, cls, year, "inventor", True, dict(base)))
    return items
```

```python
# src/services/industry_sources/ctgov.py
import httpx
from src.services.industry_sources import EvidenceItem
from src.services.industry_sources.companies import classify_company

BASE = "https://clinicaltrials.gov/api/v2/studies"
_FIELDS = "NCTId,BriefTitle,LeadSponsorName,LeadSponsorClass,CollaboratorName,CollaboratorClass,OverallOfficialName,StartDate,Condition"
_JHU_SPONSOR_MARKERS = ("johns hopkins", "sidney kimmel")


async def fetch_jhu_industry_trials(pi_name: str) -> list[dict]:
    term = f'AREA[OverallOfficialName]"{pi_name}" AND AREA[CollaboratorClass]INDUSTRY'
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(BASE, params={"query.term": term, "fields": _FIELDS, "pageSize": 100})
        resp.raise_for_status()
        return resp.json().get("studies") or []


def evidence_from_study(study: dict, tenure_start: int | None, pi_conditions: set[str]) -> list[EvidenceItem]:
    ps = study.get("protocolSection") or {}
    lead = (ps.get("sponsorCollaboratorsModule") or {}).get("leadSponsor") or {}
    if lead.get("class") == "INDUSTRY" or not any(m in (lead.get("name") or "").lower() for m in _JHU_SPONSOR_MARKERS):
        return []
    start = ((ps.get("statusModule") or {}).get("startDateStruct") or {}).get("date") or ""
    year = int(start[:4]) if start[:4].isdigit() else None
    if tenure_start is None or year is None or year < tenure_start:
        return []
    conds = {c.lower() for c in (ps.get("conditionsModule") or {}).get("conditions") or []}
    if not any(pc.lower() in c or c in pc.lower() for c in conds for pc in pi_conditions):
        return []
    nct = (ps.get("identificationModule") or {}).get("nctId")
    items = []
    for c in (ps.get("sponsorCollaboratorsModule") or {}).get("collaborators") or []:
        if c.get("class") != "INDUSTRY":
            continue
        items.append(EvidenceItem("ctgov", "trial_industry_collab", f"{nct}:{c['name'].lower()}", c["name"], None,
                                  classify_company(c["name"], "company"), year, "overall_official", True,
                                  {"nct_id": nct, "title": (ps.get("identificationModule") or {}).get("briefTitle"), "lead_sponsor": lead.get("name")}))
    return items
```

- [ ] **Step 4: Run → PASS (8).**

- [ ] **Step 5: Commit**

```bash
git add src/services/industry_sources/uspto_inventor.py src/services/industry_sources/ctgov.py tests/unit/test_industry_sources_uspto_ctgov.py
git commit -m "feat(enrichment): USPTO JHU-applicant patents and CT.gov industry-collaborator trials"
```

---

### Task 8: Versioned scorer (pure) + `industry_evidence` worker job

**Files:**
- Create: `src/services/industry_score.py`
- Create: `src/services/industry_evidence.py`
- Test: `tests/unit/test_industry_score.py`, `tests/unit/test_industry_evidence_job.py`

**Interfaces:**
- Produces:
  - `industry_score.SCORER_VERSION = "1.0.0"`; `WEIGHTS` dict below; `score_evidence(rows: list[PiIndustryEvidence|EvidenceItem]) -> tuple[float, dict]` (raw_sum, components); `normalise(raw: float, cohort_raws: list[float]) -> float` (percentile rank ×100, 0 if cohort empty).
  - `industry_evidence.execute_industry_evidence(job, db)`; `industry_evidence.rescore_user(db, user_id) -> PiIndustryScore` (used by the veto route in Task 9).

- [ ] **Step 1: Failing tests**

```python
# tests/unit/test_industry_score.py
from src.services.industry_sources import EvidenceItem
from src.services.industry_score import score_evidence, normalise, SCORER_VERSION

def ev(kind, company, cls="pharma_biotech", role="last", n_authors=5, rel=None, vetoed=False):
    e = EvidenceItem("x", kind, f"{kind}:{company}", company, None, cls, 2021, role, True, {"author_count": n_authors, **({"relationship": rel} if rel else {})})
    e.vetoed_at = "2026" if vetoed else None
    return e

def test_distinct_companies_count_once_per_kind():
    raw, comp = score_evidence([ev("coauthor_company", "Pfizer"), ev("coauthor_company", "Pfizer")])
    assert comp["coauthor_company"]["distinct_companies"] == 1 and raw == 4.5  # 3 × 1.5 (last author)

def test_vendor_and_vetoed_contribute_zero():
    raw, _ = score_evidence([ev("coauthor_company", "Agilent", cls="cro_vendor"), ev("coauthor_company", "Pfizer", vetoed=True)])
    assert raw == 0

def test_consortium_middle_author_is_downweighted():
    raw, _ = score_evidence([ev("coauthor_company", "Pfizer", role="middle", n_authors=80)])
    assert raw == 0.75  # 3 × 0.25

def test_caps_apply():
    raw, comp = score_evidence([ev("coauthor_company", f"C{i}", role="middle") for i in range(20)])
    assert comp["coauthor_company"]["weighted"] == 30 and raw == 30

def test_founder_equity_double():
    raw, _ = score_evidence([ev("coi_relationship", "Startup Inc", rel="founder")])
    assert raw == 10

def test_normalise_is_percentile_rank():
    assert normalise(5.0, [1.0, 2.0, 5.0, 9.0]) == 75.0 and normalise(1.0, []) == 0.0

def test_scorer_version_is_semver():
    assert SCORER_VERSION.count(".") == 2
```

```python
# tests/unit/test_industry_evidence_job.py
import pytest
from sqlalchemy import select
from src.models import Job, PiIndustryEvidence, PiIndustryScore, Publication, ResearcherProfile, User
from src.services import industry_evidence as ie
from src.services.jhu_rules import set_tenure_start

pytestmark = pytest.mark.integration

async def test_no_tenure_start_writes_unscored_row_and_collects_nothing(db_session, monkeypatch):
    u = User(orcid="0000-0001-1111-2222", name="No Tenure", user_role="pi"); db_session.add(u); await db_session.flush()
    job = Job(type="industry_evidence", user_id=u.id, payload={"user_id": str(u.id), "orcid": u.orcid}); db_session.add(job); await db_session.flush()
    called = []
    async def boom(*a, **k): called.append(1); return []
    monkeypatch.setattr(ie, "fetch_works_for_pmids", boom)
    await ie.execute_industry_evidence(job, db_session)
    s = (await db_session.execute(select(PiIndustryScore).where(PiIndustryScore.user_id == u.id))).scalar_one()
    assert s.score is None and s.reason == "no_tenure_start" and called == []

async def test_job_stores_evidence_and_score(db_session, monkeypatch):
    u = User(orcid="0000-0002-2214-0114", name="Gyanu Lamichhane", user_role="pi"); db_session.add(u); await db_session.flush()
    db_session.add(ResearcherProfile(user_id=u.id, keywords=["tuberculosis"], disease_areas=["Tuberculosis"]))
    db_session.add(Publication(user_id=u.id, pmid="38980071", title="p", year=2024)); await set_tenure_start(u.id, 2018, "manual", db=db_session)
    job = Job(type="industry_evidence", user_id=u.id, payload={"user_id": str(u.id), "orcid": u.orcid}); db_session.add(job); await db_session.flush()

    async def works(pmids):
        return [{"id": "https://openalex.org/W1", "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/38980071"}, "publication_year": 2024, "funders": [],
                 "authorships": [{"author": {"orcid": "https://orcid.org/0000-0002-2214-0114"}, "institutions": [{"id": "https://openalex.org/I145311948", "type": "education"}], "author_position": "last", "is_corresponding": True},
                                 {"author": {"orcid": None}, "institutions": [{"id": "https://openalex.org/I4210091798", "display_name": "Paratek Pharmaceuticals (United States)", "type": "company"}], "author_position": "middle"}]}]
    async def recs(pmids): return [{"pmid": "38980071", "year": 2024, "coi_statement": "A and B are employees of Paratek Pharmaceuticals, Inc.", "affiliations": []}]
    async def none(*a, **k): return []
    async def cf(ids): return set()
    monkeypatch.setattr(ie, "fetch_works_for_pmids", works); monkeypatch.setattr(ie, "fetch_pubmed_records", recs)
    monkeypatch.setattr(ie, "fetch_jhu_applications", none); monkeypatch.setattr(ie, "fetch_jhu_industry_trials", none); monkeypatch.setattr(ie, "company_funder_ids", cf)

    await ie.execute_industry_evidence(job, db_session)
    rows = (await db_session.execute(select(PiIndustryEvidence).where(PiIndustryEvidence.user_id == u.id))).scalars().all()
    assert {r.kind for r in rows} == {"coauthor_company", "coi_relationship"}
    s = (await db_session.execute(select(PiIndustryScore).where(PiIndustryScore.user_id == u.id).order_by(PiIndustryScore.computed_at.desc()))).scalars().first()
    assert s.reason == "ok" and s.raw_sum == 4.5 + 5 and s.evidence_count == 2 and s.tenure_start_used == 2018
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**

```python
# src/services/industry_score.py
"""Pure, versioned industry-interest scorer. Bump SCORER_VERSION on ANY weight change."""
from bisect import bisect_left

SCORER_VERSION = "1.0.0"
SCORED_CLASSES = {"pharma_biotech", "device_dx", "other"}
CLASS_FACTOR = {"pharma_biotech": 1.0, "device_dx": 1.0, "other": 0.25}
WEIGHTS = {  # kind: (per distinct company, cap)
    "coauthor_company": (3.0, 30.0),
    "company_funder": (4.0, 20.0),
    "coi_relationship": (5.0, 25.0),
    "patent_filed": (4.0, 12.0),
    "patent_assigned": (10.0, 30.0),
    "trial_industry_collab": (3.0, 9.0),
    "sbir_sttr": (6.0, 12.0),
}
_ROLE_FACTOR = {"first": 1.5, "last": 1.5, "corresponding": 1.5, "inventor": 1.0, "overall_official": 1.0}


def _item_factor(e) -> float:
    ev = e.evidence or {}
    f = _ROLE_FACTOR.get(e.pi_role or "", 1.0)
    if (e.pi_role or "middle") == "middle" and (ev.get("author_count") or 0) > 30:
        f = 0.25
    if e.kind == "coi_relationship" and ev.get("relationship") in ("founder", "equity"):
        f *= 2.0
    return f


def score_evidence(rows) -> tuple[float, dict]:
    components: dict[str, dict] = {}
    best: dict[tuple[str, str], float] = {}
    for e in rows:
        if getattr(e, "vetoed_at", None) is not None or not e.in_tenure or e.kind not in WEIGHTS:
            continue
        if e.company_class not in SCORED_CLASSES:
            continue
        key = (e.kind, (e.company_name or e.external_id).lower())
        per, _ = WEIGHTS[e.kind]
        val = per * _item_factor(e) * CLASS_FACTOR[e.company_class]
        best[key] = max(best.get(key, 0.0), val)
    for (kind, _), val in best.items():
        c = components.setdefault(kind, {"distinct_companies": 0, "uncapped": 0.0})
        c["distinct_companies"] += 1
        c["uncapped"] += val
    raw = 0.0
    for kind, c in components.items():
        c["weighted"] = min(c["uncapped"], WEIGHTS[kind][1])
        raw += c["weighted"]
    return raw, components


def normalise(raw: float, cohort_raws: list[float]) -> float:
    if not cohort_raws:
        return 0.0
    s = sorted(cohort_raws)
    return round(100.0 * bisect_left(s, raw) / len(s), 1)
```

```python
# src/services/industry_evidence.py
"""Worker handler for the ``industry_evidence`` job + rescoring helper."""
import logging
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import AgentRegistry, Job, PiIndustryEvidence, PiIndustryScore, Publication, ResearcherProfile, User
from src.services.industry_score import SCORER_VERSION, normalise, score_evidence
from src.services.industry_sources.ctgov import evidence_from_study, fetch_jhu_industry_trials
from src.services.industry_sources.openalex_industry import company_funder_ids, evidence_from_work, fetch_works_for_pmids
from src.services.industry_sources.pubmed_coi import evidence_from_record
from src.services.industry_sources.uspto_inventor import evidence_from_application, fetch_jhu_applications
from src.services.jhu_rules import get_tenure_start
from src.services.profile_pipeline import append_job_progress
from src.services.pubmed import fetch_pubmed_records

logger = logging.getLogger(__name__)


def _oaid(url):
    return (url or "").rsplit("/", 1)[-1]


async def rescore_user(db: AsyncSession, user_id: uuid.UUID, tenure_start: int | None, primary_field: str | None = None) -> PiIndustryScore:
    rows = (await db.execute(select(PiIndustryEvidence).where(PiIndustryEvidence.user_id == user_id))).scalars().all()
    raw, comps = score_evidence(rows)
    latest = (await db.execute(
        select(PiIndustryScore.user_id, PiIndustryScore.raw_sum).distinct(PiIndustryScore.user_id)
        .where(PiIndustryScore.raw_sum.isnot(None), PiIndustryScore.user_id != user_id, PiIndustryScore.scorer_version == SCORER_VERSION)
        .order_by(PiIndustryScore.user_id, PiIndustryScore.computed_at.desc()))).all()
    cohort = [r for (_, r) in latest] + [raw]
    live = [r for r in rows if r.vetoed_at is None]
    score = PiIndustryScore(user_id=user_id, score=normalise(raw, cohort), raw_sum=raw, reason="ok" if live else "no_evidence",
                            components=comps, primary_field=primary_field, tenure_start_used=tenure_start,
                            evidence_count=len(live), scorer_version=SCORER_VERSION)
    db.add(score)
    await db.flush()
    return score


async def execute_industry_evidence(job: Job, db: AsyncSession) -> None:
    user_id = uuid.UUID(job.payload["user_id"])
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
    agent = (await db.execute(select(AgentRegistry).where(AgentRegistry.user_id == user_id))).scalar_one_or_none()
    tenure_start = await get_tenure_start(db, user_id, agent_id=agent.agent_id if agent else None)
    if tenure_start is None:
        db.add(PiIndustryScore(user_id=user_id, score=None, reason="no_tenure_start", components={}, scorer_version=SCORER_VERSION))
        await db.flush()
        append_job_progress(job, "industry_done", "unscored: no JHU tenure start"); return

    profile = (await db.execute(select(ResearcherProfile).where(ResearcherProfile.user_id == user_id))).scalar_one_or_none()
    keywords = set((profile.keywords or []) + (profile.key_targets or []) + (profile.disease_areas or [])) if profile else set()
    conditions = set(profile.disease_areas or []) if profile else set()
    pubs = (await db.execute(select(Publication).where(Publication.user_id == user_id, Publication.pmid.isnot(None)))).scalars().all()
    pmids = [p.pmid for p in pubs]
    year_by_pmid = {p.pmid: p.year for p in pubs}

    items = []
    works = await fetch_works_for_pmids(pmids)
    funder_ids = {_oaid(f.get("id")) for w in works for f in w.get("funders") or []}
    cfids = await company_funder_ids(funder_ids)
    primary_field = None
    for w in works:
        primary_field = primary_field or ((w.get("primary_topic") or {}).get("field") or {}).get("display_name")
        items += evidence_from_work(w, user.orcid, tenure_start, company_funder_ids=cfids)
    append_job_progress(job, "industry1", f"openalex works={len(works)} items={len(items)}")

    for rec in await fetch_pubmed_records(pmids):
        y = rec.get("year") or year_by_pmid.get(str(rec.get("pmid")))
        items += evidence_from_record(rec, pi_year_ok=bool(y and y >= tenure_start))
    for app in await fetch_jhu_applications(user.name):
        items += evidence_from_application(app, tenure_start, keywords)
    for st in await fetch_jhu_industry_trials(user.name):
        items += evidence_from_study(st, tenure_start, conditions)

    vetoed = {(r.source, r.kind, r.external_id) for r in (await db.execute(
        select(PiIndustryEvidence).where(PiIndustryEvidence.user_id == user_id, PiIndustryEvidence.vetoed_at.isnot(None)))).scalars().all()}
    await db.execute(delete(PiIndustryEvidence).where(PiIndustryEvidence.user_id == user_id, PiIndustryEvidence.vetoed_at.is_(None)))
    seen = set()
    for it in items:
        key = (it.source, it.kind, it.external_id)
        if key in seen or key in vetoed:
            continue
        seen.add(key)
        db.add(PiIndustryEvidence(user_id=user_id, **it.__dict__))
    await db.flush()
    s = await rescore_user(db, user_id, tenure_start, primary_field)
    append_job_progress(job, "industry_done", f"evidence={s.evidence_count} raw={s.raw_sum} score={s.score} v{SCORER_VERSION}")
```

`_parse_pubmed_xml` already emits `record["pmid"]` and `record["year"]` (`src/services/pubmed.py:306`, `:360`); Task 6 added `record["coi_statement"]`. Confirm the per-author affiliation key name it emits (`grep -n affiliations src/services/pubmed.py`) before relying on `rec["affiliations"]`.

- [ ] **Step 4: Run** `.venv-test/bin/python -m pytest tests/unit/test_industry_score.py tests/unit/test_industry_evidence_job.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/services/industry_score.py src/services/industry_evidence.py tests/unit/test_industry_score.py tests/unit/test_industry_evidence_job.py
git commit -m "feat(enrichment): versioned industry-interest scorer and industry_evidence worker job"
```

---

### Task 9: Manager UI — industry score panel, evidence drawer, veto, list column

**Files:**
- Modify: `src/services/directory.py` (`load_user_detail` adds `industry_score`, `industry_evidence`; the PI list loader adds latest score per user)
- Modify: `src/routers/manager.py` (new `POST /pis/{user_id}/industry/{evidence_id}/veto`)
- Modify: `templates/manager/pi_detail.html`, `templates/manager/pis.html`
- Modify: `tests/integration/test_manager_views.py` (allowlist +1 → eight)
- Test: `tests/integration/test_manager_industry_panel.py`

**Interfaces:**
- Consumes: `rescore_user` from Task 8.
- Produces: `detail["industry_score"]: PiIndustryScore|None` (latest), `detail["industry_evidence"]: list[PiIndustryEvidence]`; list rows gain `.industry_score` (float|None) and `.industry_reason`.

- [ ] **Step 1: Failing tests**

```python
# tests/integration/test_manager_industry_panel.py
import pytest
from sqlalchemy import select
from src.models import PiIndustryEvidence, PiIndustryScore
from tests.integration.test_manager_views import make_manager, make_pi, login_as

pytestmark = pytest.mark.integration

async def test_unscored_pi_shows_reason_not_zero(client, db_session):
    pi = await make_pi(db_session); mgr = await make_manager(db_session)
    db_session.add(PiIndustryScore(user_id=pi.id, score=None, reason="no_tenure_start", components={}, scorer_version="1.0.0")); await db_session.commit()
    await login_as(client, mgr)
    html = (await client.get(f"/manager/pis/{pi.id}")).text
    assert "Unscored" in html and "no JHU tenure start" in html and ">0.0<" not in html

async def test_veto_evidence_rescored_and_hidden(client, db_session):
    pi = await make_pi(db_session); mgr = await make_manager(db_session)
    e = PiIndustryEvidence(user_id=pi.id, source="openalex", kind="coauthor_company", external_id="W1:I1", company_name="Pfizer",
                           company_class="pharma_biotech", year=2021, pi_role="last", in_tenure=True, evidence={})
    db_session.add(e); db_session.add(PiIndustryScore(user_id=pi.id, score=50.0, raw_sum=4.5, reason="ok", components={}, scorer_version="1.0.0", tenure_start_used=2018))
    await db_session.commit(); await login_as(client, mgr)
    r = await client.post(f"/manager/pis/{pi.id}/industry/{e.id}/veto", data={}); assert r.status_code == 302
    latest = (await db_session.execute(select(PiIndustryScore).where(PiIndustryScore.user_id == pi.id).order_by(PiIndustryScore.computed_at.desc()))).scalars().first()
    assert latest.reason == "no_evidence" and latest.raw_sum == 0

async def test_pi_list_has_industry_column(client, db_session):
    pi = await make_pi(db_session); mgr = await make_manager(db_session)
    db_session.add(PiIndustryScore(user_id=pi.id, score=73.2, raw_sum=20, reason="ok", components={}, scorer_version="1.0.0")); await db_session.commit()
    await login_as(client, mgr)
    html = (await client.get("/manager/pis")).text
    assert "Industry interest" in html and "73.2" in html
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**

`directory.py`: in `load_user_detail` add

```python
    industry_score = (await db.execute(select(PiIndustryScore).where(PiIndustryScore.user_id == user_id)
                                       .order_by(PiIndustryScore.computed_at.desc()).limit(1))).scalar_one_or_none()
    industry_evidence = (await db.execute(select(PiIndustryEvidence).where(PiIndustryEvidence.user_id == user_id)
                                          .order_by(PiIndustryEvidence.vetoed_at.is_(None).desc(), PiIndustryEvidence.year.desc().nullslast()))).scalars().all()
```

and return both. In the PI-list loader (the function `/manager/pis` uses — find it with `grep -n "def load_pi_directory\|def list_pis" src/services/directory.py`), add a lateral/`DISTINCT ON (user_id)` subquery for the latest `PiIndustryScore` and attach `industry_score`/`industry_reason` to each row.

`manager.py` route:

```python
@router.post("/pis/{user_id}/industry/{evidence_id}/veto")
async def manager_veto_industry_evidence(user_id: uuid.UUID, evidence_id: uuid.UUID, request: Request,
                                         db: AsyncSession = _DB, current_user: User = _STAFF):
    """'Not this PI / not industry' veto on one evidence row; persisted and rescored immediately."""
    row = (await db.execute(select(PiIndustryEvidence).where(PiIndustryEvidence.id == evidence_id, PiIndustryEvidence.user_id == user_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Evidence not found")
    row.vetoed_at = datetime.now(timezone.utc)
    row.vetoed_by_user_id = current_user.id
    agent = (await db.execute(select(AgentRegistry).where(AgentRegistry.user_id == user_id))).scalar_one_or_none()
    tenure = await get_tenure_start(db, user_id, agent_id=agent.agent_id if agent else None)
    await rescore_user(db, user_id, tenure)
    await db.commit()
    return RedirectResponse(url=f"/manager/pis/{user_id}#industry", status_code=302)
```

Template panel (after the grants section):

```html
    <!-- Industry interest (read-only score; never enters the profile or prompts) -->
    <section id="industry" class="bg-white rounded-lg shadow p-6 mb-6">
      <h2 class="font-semibold text-gray-800 mb-1">Industry interest</h2>
      {% if not industry_score %}<p class="text-sm text-gray-500">Not computed yet.</p>
      {% elif industry_score.score is none %}<p class="text-sm"><span class="font-medium text-amber-700">Unscored</span> —
        {% if industry_score.reason == 'no_tenure_start' %}no JHU tenure start recorded; set one above and re-run.{% else %}{{ industry_score.reason }}{% endif %}</p>
      {% else %}
      <p class="text-3xl font-semibold">{{ '%.1f' % industry_score.score }}<span class="text-sm text-gray-500 font-normal"> / 100 cohort percentile · raw {{ '%.1f' % industry_score.raw_sum }} · {{ industry_score.evidence_count }} evidence rows · scorer v{{ industry_score.scorer_version }} · tenure ≥ {{ industry_score.tenure_start_used }}</span></p>
      <dl class="grid grid-cols-2 gap-x-6 text-sm mt-3">
        {% for kind, c in industry_score.components.items() %}<dt class="text-gray-500">{{ kind.replace('_',' ') }}</dt><dd>{{ c.distinct_companies }} companies → {{ '%.1f' % c.weighted }}</dd>{% endfor %}
      </dl>
      {% endif %}
      <details class="mt-4"><summary class="text-sm text-blue-700 cursor-pointer">Evidence ({{ industry_evidence | length }})</summary>
        <ul class="divide-y mt-2">
        {% for e in industry_evidence %}
          <li class="py-2 flex justify-between gap-4 text-sm {% if e.vetoed_at %}opacity-50{% endif %}">
            <div><span class="font-medium">{{ e.company_name or '—' }}</span> <span class="text-xs text-gray-500">{{ e.kind.replace('_',' ') }} · {{ e.company_class }} · {{ e.year }} · PI role {{ e.pi_role or '—' }} · {{ e.source }}</span>
              {% if e.evidence.span %}<div class="text-xs text-gray-600 italic">“{{ e.evidence.span }}”</div>{% endif %}
              {% if e.evidence.title %}<div class="text-xs text-gray-600">{{ e.evidence.title }}</div>{% endif %}</div>
            {% if not e.vetoed_at %}<form method="post" action="/manager/pis/{{ target_user.id }}/industry/{{ e.id }}/veto"><button class="text-xs text-red-700 underline">Not this PI / not industry</button></form>{% else %}<span class="text-xs text-gray-400">vetoed</span>{% endif %}
          </li>
        {% endfor %}
        </ul></details>
    </section>
```

`pis.html`: add a column header `Industry interest` and per-row `{{ '%.1f' % row.industry_score if row.industry_score is not none else ('unscored' if row.industry_reason else '—') }}`; make it sortable if the table already supports sort params (check the existing header markup; if not, plain column).

Add `"/pis/{user_id}/industry/{evidence_id}/veto"` to the allowlist test (now eight paths; update the docstring).

- [ ] **Step 4: Run** `.venv-test/bin/python -m pytest tests/integration/test_manager_industry_panel.py tests/integration/test_manager_views.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/services/directory.py src/routers/manager.py templates/manager/pi_detail.html templates/manager/pis.html tests/integration/test_manager_industry_panel.py tests/integration/test_manager_views.py
git commit -m "feat(enrichment): industry-interest panel, evidence drawer with veto, PI-list column"
```

---

### Task 10: Isolation guard — the score never reaches profiles or prompts

**Files:**
- Test: `tests/unit/test_enrichment_isolation.py`

- [ ] **Step 1: Write the test (it should pass immediately; it is a tripwire)**

```python
# tests/unit/test_enrichment_isolation.py
"""The industry score is a manager-facing indicator ONLY. Nothing that builds
profile text, the agent's system prompt, tool results, or the engine may import
the score/evidence modules (adversarial D16/D17)."""
import ast
from pathlib import Path

FORBIDDEN = {"src.services.industry_score", "src.services.industry_evidence", "src.services.industry_sources"}
CONSUMERS = [
    "src/services/profile_export.py", "src/services/profile_pipeline.py", "src/agent/simulation.py",
    "src/agent/tools.py", "src/agent/thread_guidance.py", "src/agent/specialists.py", "src/services/review_bot.py",
]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def test_score_modules_are_not_imported_by_profile_or_engine_code():
    for rel in CONSUMERS:
        mods = _imports(Path(rel))
        hit = {m for m in mods if any(m == f or m.startswith(f + ".") for f in FORBIDDEN)}
        assert not hit, f"{rel} imports {hit}"


def test_score_columns_never_appear_in_profile_export_text():
    src = Path("src/services/profile_export.py").read_text()
    assert "industry" not in src.lower()
```

- [ ] **Step 2: Run** → PASS. Commit:

```bash
git add tests/unit/test_enrichment_isolation.py
git commit -m "test(enrichment): tripwire — industry score never imported by profile/engine code"
```

---

### Task 11: Backfill CLI for existing PIs

**Files:**
- Create: `scripts/enqueue_enrichment.py`
- Test: `tests/unit/test_enqueue_enrichment_script.py`

**Interfaces:**
- Consumes: `enqueue_enrichment_jobs(db, user_id, orcid)` (Task 4).
- Produces: `python scripts/enqueue_enrichment.py [--only grants|industry] [--orcid X] [--apply]` — previews by default; `--apply` enqueues for every `user_role='pi'` with a `ResearcherProfile`.

- [ ] **Step 1: Failing test**

```python
# tests/unit/test_enqueue_enrichment_script.py
import pytest
from sqlalchemy import select
from src.models import Job, ResearcherProfile, User
from scripts.enqueue_enrichment import enqueue_for_all

pytestmark = pytest.mark.integration

async def test_dry_run_enqueues_nothing_and_apply_enqueues_two_per_pi(db_session):
    u = User(orcid="0000-0003-0000-0003", name="P I", user_role="pi"); db_session.add(u); await db_session.flush()
    db_session.add(ResearcherProfile(user_id=u.id)); await db_session.flush()
    n = await enqueue_for_all(db_session, apply=False, only=None, orcid=None)
    assert n == 1 and (await db_session.execute(select(Job).where(Job.user_id == u.id))).scalars().all() == []
    await enqueue_for_all(db_session, apply=True, only=None, orcid=None)
    assert sorted(j.type for j in (await db_session.execute(select(Job).where(Job.user_id == u.id))).scalars().all()) == ["enrich_grants", "industry_evidence"]
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**

```python
# scripts/enqueue_enrichment.py
"""Enqueue enrich_grants / industry_evidence jobs for existing PIs. Preview by default.

  docker compose -f docker-compose.prod.yml exec -T blackbird-app python scripts/enqueue_enrichment.py --apply
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from src.database import get_session_factory  # noqa: E402
from src.models import Job, ResearcherProfile, User  # noqa: E402


async def enqueue_for_all(db, *, apply: bool, only: str | None, orcid: str | None) -> int:
    q = select(User).join(ResearcherProfile, ResearcherProfile.user_id == User.id).where(User.user_role == "pi")
    if orcid:
        q = q.where(User.orcid == orcid)
    users = (await db.execute(q)).scalars().all()
    types = {"grants": ["enrich_grants"], "industry": ["industry_evidence"], None: ["enrich_grants", "industry_evidence"]}[only]
    for u in users:
        print(f"{'ENQUEUE' if apply else 'would enqueue'} {types} for {u.name} ({u.orcid})")
        if apply:
            for t in types:
                pending = await db.execute(select(Job.id).where(Job.user_id == u.id, Job.type == t, Job.status.in_(("pending", "processing"))))
                if pending.scalar_one_or_none() is None:
                    db.add(Job(type=t, user_id=u.id, payload={"user_id": str(u.id), "orcid": u.orcid}))
    if apply:
        await db.commit()
    return len(users)


async def _main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    p.add_argument("--only", choices=["grants", "industry"])
    p.add_argument("--orcid")
    a = p.parse_args()
    async with get_session_factory()() as db:
        n = await enqueue_for_all(db, apply=a.apply, only=a.only, orcid=a.orcid)
    print(f"{n} PI(s) {'enqueued' if a.apply else 'previewed'}")


if __name__ == "__main__":
    asyncio.run(_main())
```

- [ ] **Step 4: Run → PASS.** Commit:

```bash
git add scripts/enqueue_enrichment.py tests/unit/test_enqueue_enrichment_script.py
git commit -m "feat(enrichment): backfill script to enqueue enrichment jobs for existing PIs"
```

---

### Task 12: Docs, CLAUDE.md deploy box, full CI

**Files:**
- Modify: `CLAUDE.md` (new `0047` deploy box after the `0044/0045/0046` box; one paragraph under "Adding New PIs" describing the two follow-on jobs and the two veto routes; update the manager allowlist sentence from "exactly six write routes" to "exactly eight")
- Modify: `docs/plans/2026-09-11-nih-reporter-grant-enrichment-analysis.md` and `…-industry-interest-score-analysis.md` — add a one-line "Implemented by: this plan" header.

- [ ] **Step 1: Write the CLAUDE.md box**

```markdown
> **Deploy order for `0047_pi_grants_and_industry_evidence` — migrate BEFORE the
> new code serves.** `0047` adds three tables (`pi_grants`,
> `pi_industry_evidence`, `pi_industry_scores`) and two `job_type_enum` values
> (`enrich_grants`, `industry_evidence`). Additive, so *old code against the new
> schema* is safe. The reverse: `/manager/pis` and `/manager/pis/{id}` select the
> new tables (`UndefinedTable`), and the worker's two new handlers fail every
> job. The engine is untouched — the agent image needs no rebuild for this
> change alone. Enum values cannot be dropped; downgrade leaves them.
>
>     DC="docker compose -f docker-compose.prod.yml"
>     $DC build blackbird-app worker
>     $DC run --rm blackbird-app alembic upgrade head
>     $DC run --rm blackbird-app alembic current      # must equal `alembic heads` (0047)
>     $DC up -d blackbird-app worker
>     $DC exec -T blackbird-app python scripts/enqueue_enrichment.py          # preview
>     $DC exec -T blackbird-app python scripts/enqueue_enrichment.py --apply  # backfill 73 PIs
>
> Two new manager POSTs (`/pis/{id}/grants/{gid}/veto`,
> `/pis/{id}/industry/{eid}/veto`) bring the explicit allowlist to EIGHT.
> `grant_titles` is now REPORTER-derived and tenure-filtered once the job runs
> (ORCID fundings remain the seed); the industry score is manager-only and has no
> path into profiles or prompts (`tests/unit/test_enrichment_isolation.py`).
> RePORTER silently ignores unknown criteria keys and returns the whole database —
> `src/services/nih_reporter.py` refuses any key outside `ALLOWED_CRITERIA`.
```

- [ ] **Step 2: Run the whole gate**

Run: `./scripts/ci.sh`
Expected: alembic single head `0047`, round trip OK, ruff zero findings on tests, pytest green with coverage floor met. If `ruff` flags the new modules under the ratcheted `src/` ceiling, fix the findings rather than raising the ceiling.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md docs/plans/2026-09-11-nih-reporter-grant-enrichment-analysis.md docs/plans/2026-09-11-industry-interest-score-analysis.md
git commit -m "docs(enrichment): 0047 deploy box, manager allowlist now eight, analysis docs point at the plan"
```

---

## Post-implementation validation (operator, on prod, before trusting numbers)

1. Backfill with `--only grants` first; on `/manager/pis/{id}` for Lamichhane expect ~14 cores, badge **PMID-linked**, all `JOHNS HOPKINS UNIVERSITY`; for a common-surname PI expect either PMID-linked or `no_reporter_match` — never "unique name" when two JHU candidates exist.
2. Then `--only industry`. Positive control: Lamichhane should show Paratek (coauthor + COI employee) and ≥1 JHU-applicant patent. Negative control: a new PI with no tenure start shows **Unscored**.
3. Review ~10 PIs' evidence drawers; grow `data/industry_vendor_blocklist.yaml`; only then consider weight changes (bump `SCORER_VERSION`).

## Self-review notes

- Spec coverage: RePORTER identity (A1–A5) → Tasks 2–4; tenure (B6–B10) → Task 3; content filter (C11–C14) → `LLM_ACTIVITY_CODES`, `phr_text` stored not abstracts; ops (D15–D18) → pacing, `include_fields` pinned, upsert-by-delete-and-reinsert with veto preservation. Industry A1–A6 → role/author-count factors, vendor class, trial lead-sponsor rule, patent tiers, COI positive-only, `primary_field` stored (percentile by field is recorded as a v1.1 follow-up — v1 normalises against the whole cohort). B7–B11 → `evidence_from_work` JHU-authorship gate, corpus-anchored lookup, JHU applicant rule, sponsor set, no-tenure → unscored. C12–C15 → sources only, raw evidence stored, name-collision keyword/condition overlap, regex-not-LLM extraction (LLM extraction deferred; spans kept). D16–D17 → Task 10.
- Known deferral: field-normalised percentile (spec A6) — `primary_field` is captured so it can be added without re-collection.
- Type consistency: `EvidenceItem` fields == `PiIndustryEvidence` insertable columns (`**it.__dict__` relies on this — do not add a field to one without the other). `GrantRecord` fields == `PiGrant` insertable columns for the same reason.
