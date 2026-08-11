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
