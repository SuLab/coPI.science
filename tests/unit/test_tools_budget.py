"""execute_tool must only charge the per-thread abstract/full-text budget on a successful fetch
(issue #23 COR-30)."""

from src.agent import tools as tools_mod
from src.agent.state import ThreadState


async def test_a_failed_abstract_fetch_does_not_spend_the_budget(monkeypatch):
    async def fake_fetch(pmid_or_doi):
        return {"error": "No abstract found for '99999999'."}

    monkeypatch.setattr(tools_mod, "fetch_abstract", fake_fetch)
    thread = ThreadState(thread_id="1", channel="general", other_agent_id="wu")
    out = await tools_mod.execute_tool(
        "retrieve_abstract", {"pmid_or_doi": "99999999"}, "su", thread,
    )
    assert out == "No abstract found for '99999999'."
    assert thread.abstracts_other == 0, (
        f"a failed fetch spent the thread's abstract budget: {thread.abstracts_other}"
    )


async def test_a_successful_abstract_fetch_spends_the_budget(monkeypatch):
    async def fake_fetch(pmid_or_doi):
        return {
            "pmid": "40000001", "title": "T", "abstract": "A",
            "journal": "J", "year": 2026, "authors": [],
        }

    monkeypatch.setattr(tools_mod, "fetch_abstract", fake_fetch)
    thread = ThreadState(thread_id="1", channel="general", other_agent_id="wu")
    out = await tools_mod.execute_tool(
        "retrieve_abstract", {"pmid_or_doi": "40000001"}, "su", thread,
    )
    assert thread.abstracts_other == 1
    assert "T" in out  # delimited as <paper_title>T</paper_title> — SEC-14


async def test_a_failed_full_text_fetch_does_not_spend_the_budget(monkeypatch):
    async def fake_fetch(pmid_or_doi):
        return {"error": "No full text found."}

    monkeypatch.setattr(tools_mod, "fetch_full_text", fake_fetch)
    thread = ThreadState(thread_id="1", channel="general", other_agent_id="wu")
    out = await tools_mod.execute_tool(
        "retrieve_full_text", {"pmid_or_doi": "99999999"}, "su", thread,
    )
    assert out == "No full text found."
    assert thread.full_text == 0


# ---- the SHIPPED call site, not the orphaned helper (#23 R8) ----
#
# The COR-30 refactor above moved execute_tool off `_execute_retrieve_abstract` /
# `_execute_retrieve_full_text` and onto the pure `_format_*_result` renderers, which
# left those two helpers with no caller anywhere in src/ — their only callers are in
# tests/unit/test_retrieve_tools_authors.py. So every assertion about what a tool answer
# CONTAINS (the #29 author list, the #29 audit-O-I3 DOI, the SEC-14 fences) was pinned on
# a path production never executes: `_format_abstract_result` could be gutted and that
# file would stay green. These two run the same assertions through execute_tool.

_ABSTRACT_AUTHORS = [f"Author{i:02d} X" for i in range(1, 22)]  # 21 — one past the cut


async def test_execute_tool_abstract_answer_carries_the_authors_and_doi(monkeypatch):
    async def fake_fetch(pmid_or_doi):
        return {
            "pmid": "40000001", "title": "T", "abstract": "A", "journal": "J",
            "year": 2026, "authors": list(_ABSTRACT_AUTHORS),
            "doi": "10.1093/bioadv/vbag036",
        }

    monkeypatch.setattr(tools_mod, "fetch_abstract", fake_fetch)
    thread = ThreadState(thread_id="1", channel="general", other_agent_id="wu")
    out = await tools_mod.execute_tool(
        "retrieve_abstract", {"pmid_or_doi": "40000001"}, "su", thread,
    )

    # Authors, fenced as untrusted external text, truncated at 20 with a count.
    assert "<paper_authors>" in out and "</paper_authors>" in out
    for name in _ABSTRACT_AUTHORS[:20]:
        assert name in out
    assert _ABSTRACT_AUTHORS[20] not in out
    assert "+1 more" in out

    # DOI, fenced, in the identifier block — without it the #29 first-person emit gate
    # is unsatisfiable, because the model has nowhere to get a citable DOI.
    assert "<paper_doi>" in out and "10.1093/bioadv/vbag036" in out
    assert out.index("PMID:") < out.index("<paper_doi>") < out.index("Abstract:")

    assert thread.abstracts_other == 1


async def test_execute_tool_full_text_answer_carries_the_authors_doi_and_methods(monkeypatch):
    async def fake_fetch(pmid_or_doi):
        return {
            "pmid": "40000002", "title": "T", "abstract": "A", "journal": "Cell",
            "year": 2025, "authors": ["Wu C", "Su AI"], "pmcid": "PMC1234567",
            "doi": "10.1016/j.cell.2025.01.001",
            "methods": "Cells were cultured under standard conditions.",
        }

    monkeypatch.setattr(tools_mod, "fetch_full_text", fake_fetch)
    thread = ThreadState(thread_id="1", channel="general", other_agent_id="wu")
    out = await tools_mod.execute_tool(
        "retrieve_full_text", {"pmid_or_doi": "40000002"}, "su", thread,
    )

    assert "<paper_authors>" in out and "Wu C, Su AI" in out
    assert out.index("Title:") < out.index("<paper_authors>") < out.index("Journal:")
    assert "<paper_doi>" in out and "10.1016/j.cell.2025.01.001" in out
    assert "PMCID: PMC1234567" in out
    assert "<paper_methods>" in out and "standard conditions" in out

    assert thread.full_text == 1
