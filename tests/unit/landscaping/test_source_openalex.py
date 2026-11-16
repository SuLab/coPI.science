"""The OpenAlex works source and the uncapped OpenAlex paging it uses (plan Task 3.1;
spec 2026-10-30 §6.1, D39), on a fake HTTP transport."""
from datetime import date

import httpx

from src.landscaping.sources import openalex_works
from src.services import openalex


def _client_factory(handler):
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _auth(position="middle", corresponding=False, inst_ids=(), name="Avery Quillfeather",
          orcid=None, author_id="A1"):
    return {
        "author": {"id": f"https://openalex.org/{author_id}", "display_name": name,
                   "orcid": orcid},
        "raw_author_name": name,
        "author_position": position,
        "is_corresponding": corresponding,
        "institutions": [{"id": f"https://openalex.org/{i}"} for i in inst_ids],
        "raw_affiliation_strings": [],
    }


def _work(n, authorships=None, **fields):
    work = {
        "id": f"https://openalex.org/W{n}",
        "ids": {"openalex": f"https://openalex.org/W{n}"},
        "title": f"Work {n}",
        "type": "article",
        "authorships": authorships if authorships is not None else [
            _auth("first", inst_ids=["I999"]), _auth("last", inst_ids=["I145311948"]),
        ],
    }
    work.update(fields)
    return work


def _paged_handler(pages, count, seen):
    """Serve ``pages`` (lists of works) by cursor ``*``, ``c1``, ``c2`` …, each with ``count``
    as the reported total; record every request's params in ``seen``."""
    def handler(request):
        params = dict(request.url.params)
        seen.append(params)
        cursor = params["cursor"]
        index = 0 if cursor == "*" else int(cursor[1:])
        last = index == len(pages) - 1
        return httpx.Response(200, json={
            "results": pages[index],
            "meta": {"count": count, "next_cursor": None if last else f"c{index + 1}"},
        })
    return handler


async def test_iter_works_follows_cursor_to_end(monkeypatch):
    pages = [[_work(i)] for i in range(openalex._MAX_PAGES + 3)]
    seen: list[dict] = []
    budget_checks = []

    async def check():
        budget_checks.append(1)

    monkeypatch.setattr(openalex, "_make_client",
                        _client_factory(_paged_handler(pages, len(pages), seen)))
    monkeypatch.setattr(openalex.openalex_budget, "check_request_budget", check)

    got = [page async for page in openalex.iter_works("f:x", "id", per_page=1)]

    assert len(got) == len(pages) > openalex._MAX_PAGES
    assert [rows for rows, _ in got] == pages
    assert all(count == len(pages) for _, count in got)
    assert len(budget_checks) == len(pages)
    assert all(p["per-page"] == "1" and p["select"] == "id" for p in seen)


async def test_slice_counts_expected_vs_received(monkeypatch):
    pages = [[_work(1), _work(2)], [_work(3)]]
    seen: list[dict] = []
    monkeypatch.setattr(openalex, "_make_client",
                        _client_factory(_paged_handler(pages, 5, seen)))

    result = await openalex_works.fetch_slice(date(2026, 9, 1), date(2026, 9, 30))

    assert result.expected == 5
    assert result.received == 3
    assert [r.ids["openalex"] for r in result.rows] == ["W1", "W2", "W3"]
    flt = seen[0]["filter"]
    assert flt.startswith(openalex_works.LINEAGE_FILTER)
    assert "type:article|preprint|review" in flt
    assert "from_publication_date:2026-09-01" in flt
    assert "to_publication_date:2026-09-30" in flt
    assert seen[0]["select"] == openalex_works.SELECT


async def test_refetch_when_100_authorships(monkeypatch):
    listed = [_auth("middle", inst_ids=["I999"], author_id=f"A{i}") for i in range(100)]
    full = [*listed, _auth("last", inst_ids=["I145311948"], name="Rowan Thistlewood",
                           author_id="A900")]
    singles: list[str] = []

    def handler(request):
        if request.url.path.endswith("/works"):
            return httpx.Response(200, json={
                "results": [_work(7, authorships=listed)],
                "meta": {"count": 1, "next_cursor": None},
            })
        singles.append(request.url.path)
        return httpx.Response(200, json=_work(7, authorships=full))

    monkeypatch.setattr(openalex, "_make_client", _client_factory(handler))

    result = await openalex_works.fetch_slice(date(2026, 9, 1), date(2026, 9, 30))

    assert singles == ["/works/W7"]
    [doc] = result.rows
    assert doc.role_basis == "last"
    assert doc.senior_names[0]["name"] == "Rowan Thistlewood"
    assert result.received == 1


async def test_existing_list_works_unchanged(monkeypatch):
    seen: list[dict] = []

    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={
            "results": [_work(len(seen))],
            "meta": {"count": 100, "next_cursor": f"c{len(seen)}"},
        })

    monkeypatch.setattr(openalex, "_make_client", _client_factory(handler))

    works = await openalex._list_works("f:x", "id")

    assert len(seen) == openalex._MAX_PAGES
    assert len(works) == openalex._MAX_PAGES


def test_to_document_maps_a_qualifying_work():
    senior = _auth("last", inst_ids=["I145311948"], name="Rowan Thistlewood",
                   orcid="https://orcid.org/0000-0002-1825-0097", author_id="A42")
    work = _work(
        9, authorships=[_auth("first", inst_ids=["I999"]), senior],
        ids={"openalex": "https://openalex.org/W9", "doi": "https://doi.org/10.1234/abc",
             "pmid": "https://pubmed.ncbi.nlm.nih.gov/123456",
             "pmcid": "https://www.ncbi.nlm.nih.gov/pmc/articles/7654321"},
        publication_date="2026-09-15", language="en", is_retracted=True,
        abstract_inverted_index={"Kinase": [0], "inhibition": [1]},
        best_oa_location={"license": "cc-by"},
    )

    doc = openalex_works.to_document(work)

    assert doc.kind == "work"
    assert doc.ids == {"doi": "10.1234/abc", "pmid": "123456", "pmcid": "PMC7654321",
                       "openalex": "W9"}
    assert doc.role_basis == "last"
    assert doc.authorships == [senior]
    assert doc.summary == "Kinase inhibition"
    assert doc.summary_source == "openalex"
    assert doc.published == date(2026, 9, 15)
    assert doc.license == "cc-by"
    assert doc.retracted is True
    assert doc.senior_names == [{"name": "Rowan Thistlewood", "orcid": "0000-0002-1825-0097",
                                 "openalex_author_id": "A42", "nih_pi_profile_id": None}]
    assert doc.raw_qualifier["institution_ids"] == ["I145311948"]


def test_to_document_leaves_a_non_qualifying_work_unqualified():
    work = _work(10, authorships=[_auth("last", inst_ids=["I999"])], type="preprint")

    doc = openalex_works.to_document(work)

    assert doc.kind == "preprint"
    assert doc.role_basis is None
    assert doc.authorships is None
    assert doc.senior_names == []
    assert doc.summary is None and doc.summary_source is None
