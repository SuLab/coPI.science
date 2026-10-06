"""Uncapped corpus chunks and retries stop at the actual free daily limit."""
from types import SimpleNamespace

import httpx
import pytest

from src.services import http_pacing
from src.services import openalex_budget as ob
from src.services.industry_sources import openalex_industry as oi
from src.services.industry_sources.registry import OpenAlexSource, SourceContext
from src.services.job_queue import JobDeferred


def _transport(monkeypatch, *, remaining, handler):
    meter = {'remaining': remaining, 'requests': []}
    async def read_meter():
        return ob.Meter(1000, meter['remaining'], 600)
    def respond(request):
        meter['requests'].append(request.url.path)
        meter['remaining'] -= 1
        return handler(request)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(ob, 'read_meter', read_meter)
    monkeypatch.setattr(oi.httpx, 'AsyncClient', lambda **kw: real_client(
        transport=httpx.MockTransport(respond), **kw))
    monkeypatch.setattr(oi, '_PACER', http_pacing.Pacer(0))
    return meter


async def test_a_large_corpus_defers_before_the_ninth_chunk_exceeds_the_free_limit(monkeypatch):
    meter = _transport(monkeypatch, remaining=8, handler=lambda r: httpx.Response(
        200, json={'results': [], 'meta': {'count': 0}}))
    ctx = SourceContext(user=SimpleNamespace(orcid='0000-0002-1825-0097'), tenure_start=2000,
                        keywords=set(), conditions=set(), pmids=[str(i) for i in range(1000)],
                        year_by_pmid={})
    with ob.bulk_requests(), pytest.raises(JobDeferred):
        await OpenAlexSource().fetch(ctx)
    assert meter['requests'] == ['/works'] * 8
    assert meter['remaining'] == 0


async def test_dynamic_funder_followons_obey_the_same_free_limit(monkeypatch):
    def respond(request):
        if request.url.path == '/works':
            return httpx.Response(200, json={'results': [{'funders': [{'id': 'F1'}]}]})
        assert request.url.path == '/funders'
        return httpx.Response(200, json={'results': [{'id': 'F1', 'roles': [
            {'role': 'institution', 'id': 'I1'}]}]})
    meter = _transport(monkeypatch, remaining=2, handler=respond)
    ctx = SourceContext(user=SimpleNamespace(orcid='0000-0002-1825-0097'), tenure_start=2000,
                        keywords=set(), conditions=set(), pmids=['1'], year_by_pmid={})
    with ob.bulk_requests(), pytest.raises(JobDeferred):
        await OpenAlexSource().fetch(ctx)
    assert meter['requests'] == ['/works', '/funders']
    assert meter['remaining'] == 0


async def test_retry_attempts_are_checked_before_another_charged_request(monkeypatch):
    meter = _transport(monkeypatch, remaining=1, handler=lambda r: httpx.Response(503))
    async def no_sleep(_seconds):
        pass
    monkeypatch.setattr(http_pacing, '_sleep', no_sleep)
    with ob.bulk_requests(), pytest.raises(JobDeferred):
        await oi.fetch_works_for_pmids(['1'])
    assert meter['requests'] == ['/works'] and meter['remaining'] == 0


async def test_interactive_requests_do_not_enter_the_bulk_gate_and_scope_restores(monkeypatch):
    async def never():
        raise AssertionError('interactive request must not read the bulk meter')
    monkeypatch.setattr(ob, 'read_meter', never)
    await ob.check_request_budget()
    with pytest.raises(RuntimeError), ob.bulk_requests():
        raise RuntimeError('cancelled job')
    await ob.check_request_budget()


async def test_unreadable_request_meter_retains_the_admitted_fixed_rate_fallback(monkeypatch):
    meter = _transport(monkeypatch, remaining=1, handler=lambda r: httpx.Response(
        200, json={'results': []}))

    async def unreadable():
        return None

    monkeypatch.setattr(ob, 'read_meter', unreadable)
    with ob.bulk_requests():
        result = await oi.fetch_works_for_pmids(['1'])
    assert result.items == [] and not result.truncated
    assert meter['requests'] == ['/works']
