"""Own-paper abstract-cap exemption + reworded phase-4 injection (Task 11).

``execute_tool``'s ``retrieve_abstract`` branch enforces a per-thread cap
(``ThreadState.abstracts_other`` vs ``settings.max_abstracts_other_per_thread``)
on abstract lookups of OTHER labs' papers. An agent citing its OWN paper (a DOI
present in ``Agent.own_publication_dois``) must be exempt from BOTH the cap
check and the increment — retrieving your own paper's abstract isn't "using up"
budget meant to limit how much of someone else's work you pull in.

The exemption only recognizes DOI form: a bare PMID has nothing to match
against ``own_dois``, so it always counts against the cap even if the paper
happens to be the agent's own (documented limit, design §10).
"""

import pytest

from src.agent import tools as tools_mod
from src.agent.state import ThreadState

SCOPE_DOI = "10.1073/pnas.2509021122"
OTHER_DOI = "10.1038/s41557-023-01224-y"


@pytest.fixture(autouse=True)
def _stub_fetch_abstract(monkeypatch):
    """Avoid any real PubMed call — the cap logic runs before the fetch."""

    async def _fake(pmid_or_doi):
        return {
            "title": "Title",
            "journal": "J. Testing",
            "year": "2024",
            "pmid": "12345678",
            "abstract": "Abstract text.",
        }

    monkeypatch.setattr(tools_mod, "fetch_abstract", _fake)


async def test_own_doi_lookup_at_cap_still_succeeds_and_does_not_increment():
    """(a) An own-DOI lookup at the cap still succeeds and does not increment."""
    thread = ThreadState(
        thread_id="t1", channel="c", other_agent_id="x", abstracts_other=10
    )
    out = await tools_mod.execute_tool(
        "retrieve_abstract",
        {"pmid_or_doi": SCOPE_DOI},
        "schultz",
        thread,
        role="pi_lab",
        own_dois={SCOPE_DOI},
    )
    assert "Rate limit" not in out
    assert thread.abstracts_other == 10  # unchanged — own-paper lookups don't count


async def test_foreign_doi_lookup_increments_and_rate_limits_at_cap():
    """(b) A foreign-DOI lookup increments and rate-limits at the cap."""
    thread = ThreadState(
        thread_id="t2", channel="c", other_agent_id="x", abstracts_other=9
    )
    out = await tools_mod.execute_tool(
        "retrieve_abstract",
        {"pmid_or_doi": OTHER_DOI},
        "schultz",
        thread,
        role="pi_lab",
        own_dois={SCOPE_DOI},
    )
    assert "Rate limit" not in out
    assert thread.abstracts_other == 10  # incremented

    out2 = await tools_mod.execute_tool(
        "retrieve_abstract",
        {"pmid_or_doi": OTHER_DOI},
        "schultz",
        thread,
        role="pi_lab",
        own_dois={SCOPE_DOI},
    )
    assert "Rate limit" in out2
    assert thread.abstracts_other == 10  # not incremented past the cap


async def test_bare_pmid_lookup_counts_against_the_cap_even_with_own_dois_set():
    """(c) A bare-PMID lookup counts — documented limit, design §10.

    ``own_dois`` is a set of DOIs; a bare PMID has no DOI substring for
    ``_extract_dois`` to find, so the own-paper exemption can never match it,
    regardless of whether the paper is in fact the agent's own.
    """
    thread = ThreadState(
        thread_id="t3", channel="c", other_agent_id="x", abstracts_other=0
    )
    out = await tools_mod.execute_tool(
        "retrieve_abstract",
        {"pmid_or_doi": "12345678"},
        "schultz",
        thread,
        role="pi_lab",
        own_dois={SCOPE_DOI},
    )
    assert "Rate limit" not in out
    assert thread.abstracts_other == 1  # counted, not exempted


async def test_own_dois_none_behaves_exactly_as_before():
    """No ``own_dois`` passed (the pre-Task-11 default) — cap behaves as today."""
    thread = ThreadState(
        thread_id="t4", channel="c", other_agent_id="x", abstracts_other=10
    )
    out = await tools_mod.execute_tool(
        "retrieve_abstract",
        {"pmid_or_doi": SCOPE_DOI},
        "schultz",
        thread,
        role="pi_lab",
    )
    assert "Rate limit" in out
    assert thread.abstracts_other == 10


# --- (d) reworded phase-4 injection ------------------------------------------


@pytest.fixture
def agent_with_pub(tmp_path, monkeypatch):
    """Agent whose public profile lists one DOI (the SCOPE paper)."""
    from src.agent import agent as agent_module
    from src.agent.agent import Agent

    monkeypatch.setattr(agent_module, "PROFILES_DIR", tmp_path)
    (tmp_path / "public").mkdir()
    (tmp_path / "private").mkdir()
    (tmp_path / "public" / "schultz.md").write_text(
        f"# Schultz Lab\n\nKey paper: A chemical epigenetic tool — {SCOPE_DOI}\n"
    )
    (tmp_path / "private" / "schultz.md").write_text("No private instructions.")
    return Agent(agent_id="schultz", bot_name="SchultzBot", pi_name="Peter Schultz")


def test_own_doi_root_gets_the_reworded_speak_as_author_injection(agent_with_pub):
    """(d) The reworded phase-4 injection appears when the root cites an own
    DOI, contains "Speak as its author", and does not say "collaboration"."""
    thread = ThreadState(
        thread_id="t1", channel="chemical-biology", other_agent_id="schen", message_count=2
    )
    history = [
        {"sender": "SChenBot", "content": f"Paper — SCOPE <https://doi.org/{SCOPE_DOI}>"},
    ]
    _system, messages = agent_with_pub.build_phase4_prompt(
        thread=thread,
        thread_history=history,
        other_agent_name="SChenBot",
        other_agent_lab="Shuibing Chen",
    )
    body = messages[0]["content"]
    assert "This thread's root post cites a paper your own lab authored" in body
    assert "Speak as its author" in body
    # Scope to the injected warning paragraph — the old wording ("...toward a
    # collaboration...") must be gone, not just the exact phrase.
    warning = body.split("⚠️", 1)[1].split("\n\n", 1)[0]
    assert "collaboration" not in warning.lower()
