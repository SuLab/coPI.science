"""Operational corpus paths report incomplete coverage when free credits are unavailable."""
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts import audit_pi_corpus as audit
from scripts import rederive_tenure_starts as tenure
from scripts import repair_pi_corpus as repair
from src.services import openalex_budget as ob


@pytest.fixture
def harness(monkeypatch):
    user = SimpleNamespace(id=uuid.uuid4(), orcid="0000-0002-1825-0097",
                           name="Rachel Green", institution="Johns Hopkins University")
    records = []
    db = SimpleNamespace(commit=AsyncMock(), execute=AsyncMock(
        return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [user]))))

    async def stored(*args):
        return records

    async def refetched(*args):
        return {}

    async def profile(*args):
        return {"employments": []}

    @asynccontextmanager
    async def factory():
        yield db

    monkeypatch.setattr(audit, "load_stored_publications", stored)
    monkeypatch.setattr(repair, "load_stored_publications", stored)
    monkeypatch.setattr(audit, "refetch_pmids", refetched)
    monkeypatch.setattr(tenure, "fetch_orcid_profile", profile)
    monkeypatch.setattr(audit, "get_session_factory", lambda: factory)
    return SimpleNamespace(user=user, db=db, records=records)


async def _invoke(kind, harness):
    if kind == "audit":
        return (await audit._classify_pi(harness.db, harness.user))["resolve_error"]
    if kind == "repair":
        plan = await repair.build_plan_for_pi(
            harness.db, harness.user, None, only="additions")
        assert not plan.additions
        return plan.additions_error
    candidate = tenure.Candidate(key="test", raw_value="2001", user_id=harness.user.id,
                                 name=harness.user.name, orcid=harness.user.orcid,
                                 institution=harness.user.institution, stored_year=2001)
    result = await tenure.rederive(candidate)
    assert not result.changed
    return result.skip_reason


@pytest.mark.parametrize("kind", ["audit", "repair", "tenure"])
@pytest.mark.parametrize("meter", [None, ob.Meter(1000, 4, 600)])
async def test_inline_scripts_do_not_resolve_without_confirmed_free_credits(
    monkeypatch, harness, kind, meter,
):
    async def read_meter():
        return meter

    async def forbidden(*args, **kwargs):
        raise AssertionError("A refused admission must not resolve the corpus")

    monkeypatch.setattr(ob, "read_meter", read_meter)
    for module in (audit, repair, tenure):
        monkeypatch.setattr(module, "resolve_corpus", forbidden)
    assert "OpenAlex free budget" in await _invoke(kind, harness)


@pytest.mark.parametrize("kind", ["audit", "repair", "tenure"])
async def test_inline_script_scopes_cover_exhaustion_after_admission(monkeypatch, harness, kind):
    reads = 0

    async def read_meter():
        nonlocal reads
        reads += 1
        return ob.Meter(1000, 5 if reads == 1 else 0, 600)

    async def resolve(*args, **kwargs):
        await ob.check_request_budget()
        raise AssertionError("The exhausted request must not run")

    monkeypatch.setattr(ob, "read_meter", read_meter)
    for module in (audit, repair, tenure):
        monkeypatch.setattr(module, "resolve_corpus", resolve)
    assert "OpenAlex free budget" in await _invoke(kind, harness)
    assert reads == 2
    await ob.check_request_budget()
    assert reads == 2  # No task-local scope leaks into later interactive work.


async def test_read_only_audit_exit_distinguishes_failed_resolve_and_explicit_skip(
    monkeypatch, harness,
):
    async def unreadable():
        return None

    monkeypatch.setattr(ob, "read_meter", unreadable)
    assert await audit._run([], skip_resolve=False) == 1
    assert await audit._run([], skip_resolve=True) == 0


async def test_audit_uses_the_canonical_bare_initial_matcher(monkeypatch, harness):
    harness.records.append(SimpleNamespace(pmid="1", title="A paper"))

    async def refetched(*args):
        return {"1": {"pmid": "1", "title": "A paper", "pub_types": ["Journal Article"],
                      "authors": [{"last": "Green", "fore": "R", "initials": "R"}]}}

    monkeypatch.setattr(audit, "refetch_pmids", refetched)
    report = await audit._classify_pi(harness.db, harness.user, skip_resolve=True)
    assert report["classification"]["bare_initial_only"] == 1
    assert report["classification"]["strong"] == 0


async def test_inline_admission_can_use_the_last_five_free_credits(monkeypatch):
    async def meter():
        return ob.Meter(1000, 5, 600)

    monkeypatch.setattr(ob, "read_meter", meter)
    await ob.check_inline_corpus_budget()
