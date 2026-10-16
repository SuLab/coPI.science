"""OPENALEX_API_KEY: sent as a Bearer header on every OpenAlex client, never in a URL."""
import re
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src.services import openalex, openalex_auth, openalex_budget
from src.services.industry_sources import openalex_industry

ROOT = Path(__file__).resolve().parents[2]
KEY = "OAKEY0123456789"


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setattr(openalex_auth, "get_settings",
                        lambda: SimpleNamespace(openalex_api_key=f" {KEY} "))


def test_no_key_sends_no_header(monkeypatch):
    monkeypatch.setattr(openalex_auth, "get_settings",
                        lambda: SimpleNamespace(openalex_api_key=""))
    assert openalex_auth.openalex_headers() == {}


def test_a_key_is_a_bearer_header(keyed):
    assert openalex_auth.openalex_headers() == {"Authorization": f"Bearer {KEY}"}


def test_only_openalex_auth_reads_the_key():
    """No other module reads the key, so none can put it in a URL, a log or a row."""
    hits = sorted(
        p.relative_to(ROOT).as_posix()
        for base in ("src", "scripts")
        for p in (ROOT / base).rglob("*.py")
        if re.search(r"openalex_api_key", p.read_text(encoding="utf-8"))
    )
    assert hits == ["src/config.py", "src/services/openalex_auth.py"]


def _recording_client(monkeypatch, handler):
    """Every httpx.AsyncClient built in this test answers from ``handler`` but keeps the
    headers its call site gave it."""
    real = httpx.AsyncClient

    class Recorded(real):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", Recorded)


async def test_every_page_of_a_list_carries_the_key_in_a_header_only(keyed, monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        cursor = request.url.params.get("cursor")
        meta = {"next_cursor": "c2" if cursor == "*" else None}
        return httpx.Response(200, json={"results": [{"id": cursor}], "meta": meta})

    _recording_client(monkeypatch, handler)

    async def no_budget_check():
        return None

    monkeypatch.setattr(openalex.openalex_budget, "check_request_budget", no_budget_check)
    works = await openalex._list_works("author.orcid:x", "ids")
    assert [w["id"] for w in works] == ["*", "c2"]
    assert [r.headers.get("authorization") for r in seen] == [f"Bearer {KEY}"] * 2
    assert all(KEY not in str(r.url) and "api_key" not in str(r.url) for r in seen)


async def test_the_meter_probe_and_industry_clients_carry_the_key(keyed, monkeypatch):
    assert openalex_budget._make_client().headers.get("authorization") == f"Bearer {KEY}"
    assert openalex._make_client().headers.get("authorization") == f"Bearer {KEY}"
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"results": [], "meta": {}})

    _recording_client(monkeypatch, handler)
    source = (ROOT / "src/services/industry_sources/openalex_industry.py").read_text()
    # Every client the industry source builds is given the key header.
    assert source.count("httpx.AsyncClient(") == source.count("headers=openalex_headers()") == 3
    async with httpx.AsyncClient(headers=openalex_auth.openalex_headers()) as client:
        await client.get(f"{openalex_industry.OA}/works")
    assert seen[0].headers.get("authorization") == f"Bearer {KEY}"
