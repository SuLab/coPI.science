"""fetch_orcid_works reports each work's venue (spec 2026-10-09 §5.2)."""
import httpx

from src.services import orcid


def _fake_client(payload, monkeypatch):
    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _Client())


async def test_the_journal_title_is_reported_and_a_missing_one_is_none(monkeypatch):
    _fake_client({"group": [
        {"work-summary": [{"title": {"title": {"value": "SHARP"}}, "type": "journal-article",
                           "journal-title": {"value": "IEEE RA-L"}}]},
        {"work-summary": [{"title": {"title": {"value": "Untitled venue"}},
                           "type": "conference-paper", "journal-title": None}]},
    ]}, monkeypatch)
    works = await orcid.fetch_orcid_works("0000-0002-4217-4776")
    assert [w["journal"] for w in works] == ["IEEE RA-L", None]
