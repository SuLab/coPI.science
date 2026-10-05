"""D11/item 6: ``fetch_orcid_works`` must survive ORCID's ``null`` fields.

ORCID emits ``"external-ids": null`` (not merely an absent key) for some
work-summaries — measured 15 of 155 for Daeyeol Lee, ORCID
0000-0003-3474-019X. ``dict.get(k, {})`` only supplies its default when the
key is ABSENT, so the old ``summary.get("external-ids", {}).get(...)`` chain
raised ``AttributeError`` on a present-but-null key, which escaped the
function's HTTP-only try/except, surfaced as ``CorpusStageError``, and killed
the whole ``generate_profile`` job after 3 retries. The same idiom applied to
``title``, ``title.title`` and ``publication-date.year.value``.

This fixture reproduces that exact shape (a work-summary with every one of
those keys present and null) rather than merely absent, since an absent key
was never the defect.
"""

import httpx

from src.services import orcid


async def test_a_null_external_ids_work_summary_does_not_raise(monkeypatch):
    payload = {
        "group": [
            {
                "work-summary": [
                    {
                        "title": {"title": {"value": "A paper with no IDs on file"}},
                        "publication-date": {"year": {"value": "2019"}},
                        "external-ids": None,
                        "type": "journal-article",
                    }
                ]
            }
        ]
    }

    class _Resp:
        status_code = 200  # read by http_pacing.with_retries

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

    works = await orcid.fetch_orcid_works("0000-0003-3474-019X")

    assert len(works) == 1
    work = works[0]
    assert work["title"] == "A paper with no IDs on file"
    assert work["year"] == 2019
    assert work["pmid"] is None
    assert work["doi"] is None


async def test_a_null_title_and_publication_date_do_not_raise_either(monkeypatch):
    # Same defect class, the other two chains named in D11: "title" and
    # "publication-date" can each themselves be null, not just their nested
    # "title"/"year" keys.
    payload = {
        "group": [
            {
                "work-summary": [
                    {
                        "title": None,
                        "publication-date": None,
                        "external-ids": {
                            "external-id": [
                                {
                                    "external-id-type": "doi",
                                    "external-id-value": "10.1/xyz",
                                }
                            ]
                        },
                        "type": "journal-article",
                    }
                ]
            }
        ]
    }

    class _Resp:
        status_code = 200  # read by http_pacing.with_retries

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

    works = await orcid.fetch_orcid_works("0000-0003-3474-019X")

    assert len(works) == 1
    work = works[0]
    assert work["title"] == ""
    assert work["year"] is None
    assert work["doi"] == "10.1/xyz"


async def test_fetch_orcid_profile_survives_present_but_null_parts(monkeypatch):
    """The same null idiom in the RECORD: a single-name researcher has
    ``"family-name": null``, which made Add-PI answer "Could not fetch" for a
    record that fetched fine (2026-10-05 Add-PI audit, N2)."""
    record = {
        "person": {
            "name": {"given-names": {"value": "Madonna"}, "family-name": None},
            "emails": None,
            "researcher-urls": {"researcher-url": [{"url": None}]},
        },
        "activities-summary": {
            "employments": {
                "affiliation-group": [
                    {"summaries": [{"employment-summary": {
                        "organization": None, "start-date": None,
                        "end-date": None, "display-index": None,
                    }}]},
                    {"summaries": None},
                ]
            }
        },
    }

    async def _record(_orcid_id):
        return record

    monkeypatch.setattr(orcid, "fetch_orcid_record", _record)
    profile = await orcid.fetch_orcid_profile("0000-0002-1825-0097")
    assert profile["name"] == "Madonna"
    assert profile["institution"] is None
    assert profile["employments"] == [
        {"organization": None, "start_year": None, "current": True}
    ]


async def test_fetch_orcid_profile_survives_a_null_person(monkeypatch):
    async def _record(_orcid_id):
        return {"person": None, "activities-summary": None}

    monkeypatch.setattr(orcid, "fetch_orcid_record", _record)
    profile = await orcid.fetch_orcid_profile("0000-0002-1825-0097")
    assert profile["name"] == "0000-0002-1825-0097"
    assert profile["employments"] == []
