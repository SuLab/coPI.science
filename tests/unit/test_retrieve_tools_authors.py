"""retrieve_abstract / retrieve_full_text must surface the author list (#29)."""

from src.agent import tools as tools_mod

FAKE = {
    "pmid": "40000001",
    "title": "Desiderata for a biomedical knowledge network",
    "abstract": "We outline desiderata.",
    "journal": "Bioinformatics Advances",
    "year": 2026,
    "authors": ["Wu C", "Liu H", "Su AI", "Wu CH"],
}


async def test_retrieve_abstract_includes_authors(monkeypatch):
    async def fake_fetch(pmid_or_doi):
        return dict(FAKE)

    monkeypatch.setattr(tools_mod, "fetch_abstract", fake_fetch)
    out = await tools_mod._execute_retrieve_abstract("10.1093/bioadv/vbag036")
    assert "Authors:" in out
    assert "Su AI" in out


async def test_retrieve_abstract_omits_line_when_no_authors(monkeypatch):
    async def fake_fetch(pmid_or_doi):
        d = dict(FAKE)
        d["authors"] = []
        return d

    monkeypatch.setattr(tools_mod, "fetch_abstract", fake_fetch)
    out = await tools_mod._execute_retrieve_abstract("10.1093/bioadv/vbag036")
    assert "Authors:" not in out


async def test_retrieve_abstract_truncates_after_20_authors(monkeypatch):
    names = [f"Author{i:02d} X" for i in range(1, 22)]  # 21 authors

    async def fake_fetch(pmid_or_doi):
        d = dict(FAKE)
        d["authors"] = names
        return d

    monkeypatch.setattr(tools_mod, "fetch_abstract", fake_fetch)
    out = await tools_mod._execute_retrieve_abstract("10.1093/bioadv/vbag036")
    for name in names[:20]:
        assert name in out
    assert names[20] not in out
    assert "+1 more" in out


async def test_retrieve_abstract_no_truncation_suffix_at_exactly_20_authors(monkeypatch):
    names = [f"Author{i:02d} X" for i in range(1, 21)]  # exactly 20 authors

    async def fake_fetch(pmid_or_doi):
        d = dict(FAKE)
        d["authors"] = names
        return d

    monkeypatch.setattr(tools_mod, "fetch_abstract", fake_fetch)
    out = await tools_mod._execute_retrieve_abstract("10.1093/bioadv/vbag036")
    for name in names:
        assert name in out
    assert "more" not in out


FAKE_FULL_TEXT = {
    "pmid": "40000002",
    "title": "Full text of a great collaboration",
    "abstract": "We describe the collaboration in detail.",
    "journal": "Cell",
    "year": 2025,
    "authors": ["Wu C", "Su AI"],
    "pmcid": "PMC1234567",
    "methods": "Cells were cultured under standard conditions.",
}


async def test_retrieve_full_text_includes_authors(monkeypatch):
    async def fake_fetch(pmid_or_doi):
        return dict(FAKE_FULL_TEXT)

    monkeypatch.setattr(tools_mod, "fetch_full_text", fake_fetch)
    out = await tools_mod._execute_retrieve_full_text("40000002")

    assert "<paper_authors>" in out
    assert "</paper_authors>" in out
    assert "Wu C, Su AI" in out

    # The Authors line sits between Title and Journal, delimited as its own
    # fenced block (SEC-14) — not just present anywhere in the output.
    title_idx = out.index("Title:")
    authors_idx = out.index("<paper_authors>")
    journal_idx = out.index("Journal:")
    assert title_idx < authors_idx < journal_idx


async def test_retrieve_full_text_omits_line_when_no_authors(monkeypatch):
    async def fake_fetch(pmid_or_doi):
        d = dict(FAKE_FULL_TEXT)
        d["authors"] = []
        return d

    monkeypatch.setattr(tools_mod, "fetch_full_text", fake_fetch)
    out = await tools_mod._execute_retrieve_full_text("40000002")
    assert "Authors:" not in out
    assert "<paper_authors>" not in out
