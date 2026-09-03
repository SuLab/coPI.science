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
