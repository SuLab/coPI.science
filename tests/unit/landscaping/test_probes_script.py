"""The Phase 0 probe script (plan Task 6.6, spec 2026-10-30 §15). No network: HTTP runs over
an httpx.MockTransport and the model call is replaced."""
import contextlib
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from scripts import landscaping_probes as probes
from src.landscaping import intake, llm_client
from src.models.landscape import LandscapeReport, LandscapeSource


def _handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "api.uspto.gov" in url:
        return httpx.Response(200, json={"count": 1, "patentFileWrapperDataBag": [{
            "applicationNumberText": "1",
            "applicationMetaData": {"inventionTitle": "A fictitious device"}}]})
    if "opentargets" in url:
        return httpx.Response(200, json={"data": {"target": {"homologues": [
            {"speciesName": "Mouse", "targetGeneSymbol": "Trp53"}]}}})
    if "genenames.org" in url:
        return httpx.Response(200, text="no match")
    if "reporter.nih.gov" in url:
        return httpx.Response(200, json={"results": [{"principal_investigators": [
            {"profile_id": 1, "full_name": "Ada Fictitious"}]}]})
    if "clinicaltrials.gov" in url:
        return httpx.Response(429)
    return httpx.Response(200, json={"resultList": {}})


@pytest.fixture
def fakes(monkeypatch):
    monkeypatch.setattr(probes, "_http_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_handler)))
    monkeypatch.setattr(probes, "RATE_PACE_S", 0)
    monkeypatch.setattr(probes, "RATE_REQUESTS", 3)
    monkeypatch.setattr(probes.odp_patents, "_api_key", lambda: "k")

    async def fake_call_sync(stage, request, **_kwargs):
        assert request["thinking"] == {"type": "adaptive"}
        assert "effort" in request["output_config"]
        return SimpleNamespace(error=None, stop_reason="end_turn", served_by=request["model"])

    monkeypatch.setattr(llm_client, "call_sync", fake_call_sync)


def _factory(db_session):
    @contextlib.asynccontextmanager
    async def factory():
        yield db_session
    return factory


async def test_each_probe_stores_a_report(db_session, fakes, capsys):
    factory = _factory(db_session)
    for name in probes.PROBES:
        assert await probes.run([name], db_factory=factory) == 0
        printed = json.loads(capsys.readouterr().out)
        assert printed["probe"] == name
    rows = (await db_session.execute(
        select(LandscapeReport).where(LandscapeReport.kind == "probes"))).scalars().all()
    by_probe = {r.payload["probe"]: r.payload["finding"] for r in rows}
    assert set(by_probe) == set(probes.PROBES)
    assert by_probe["odp-fields"]["inventor_cities_present"] is False
    assert by_probe["reporter-pi"]["orcid_present"] is False
    assert by_probe["rate-limits"]["clinicaltrials"]["rate_limited"] == 3
    assert by_probe["ortholog"]["open_targets_found"] == 1
    assert all(v["accepted"] for v in by_probe["effort"].values())


async def test_unknown_probe_exits_nonzero(db_session):
    with pytest.raises(SystemExit) as exc:
        await probes.run(["no-such-probe"], db_factory=_factory(db_session))
    assert exc.value.code != 0


async def test_enable_source_with_no_rows_yet(db_session):
    factory = _factory(db_session)
    await db_session.execute(LandscapeSource.__table__.delete())
    assert await probes.run(["enable-source", "jhtv"], db_factory=factory) == 0
    rows = {r.source: r.enabled for r in
            (await db_session.execute(select(LandscapeSource))).scalars()}
    assert set(rows) == set(intake.SOURCES)
    assert rows["jhtv"] is True
    assert await probes.run(["enable-source", "nonesuch"], db_factory=factory) == 2
