"""Tests for self-authored-paper detection (GitHub issue #7).

A bot must not engage with a paper its own PI/lab (co)authored as if the work
were external. These tests cover DOI extraction, the ``cites_own_paper`` check,
and that the scan/reply prompt builders surface the warning.
"""

import pytest

from src.agent import agent as agent_module
from src.agent.agent import Agent, _extract_dois
from src.agent.state import ThreadState

# The real-world case from issue #7: schen posted the SCOPE paper, which Schultz
# co-authored, and SchultzBot replied as if it were external work.
SCOPE_DOI = "10.1073/pnas.2509021122"


@pytest.fixture
def agent_with_pub(tmp_path, monkeypatch):
    """Agent whose public profile lists one DOI (the SCOPE paper)."""
    monkeypatch.setattr(agent_module, "PROFILES_DIR", tmp_path)
    (tmp_path / "public").mkdir()
    (tmp_path / "private").mkdir()
    (tmp_path / "memory").mkdir()
    (tmp_path / "public" / "schultz.md").write_text(
        f"# Schultz Lab\n\nKey paper: A chemical epigenetic tool — {SCOPE_DOI}\n"
    )
    (tmp_path / "private" / "schultz.md").write_text("No private instructions.")
    return Agent(agent_id="schultz", bot_name="SchultzBot", pi_name="Peter Schultz")


class TestExtractDois:
    def test_plain_doi(self):
        assert _extract_dois(f"see {SCOPE_DOI}") == {SCOPE_DOI}

    def test_doi_in_slack_link(self):
        text = f"published here: <https://doi.org/{SCOPE_DOI}>"
        assert _extract_dois(text) == {SCOPE_DOI}

    def test_strips_trailing_paren_and_period(self):
        # Mirrors the messy form seen in real posts: "...3c01557)>"
        assert _extract_dois("(10.1021/acscentsci.3c01557)") == {"10.1021/acscentsci.3c01557"}
        assert _extract_dois("ends here 10.1234/abc.def.") == {"10.1234/abc.def"}

    def test_case_insensitive_and_normalized(self):
        assert _extract_dois("10.1073/PNAS.2509021122") == {SCOPE_DOI}

    def test_empty_and_none(self):
        assert _extract_dois(None) == set()
        assert _extract_dois("no doi here") == set()


class TestCitesOwnPaper:
    def test_detects_own_doi(self, agent_with_pub):
        assert agent_with_pub.own_publication_dois == {SCOPE_DOI}
        assert agent_with_pub.cites_own_paper(
            f"Paper — SCOPE method <https://doi.org/{SCOPE_DOI}>"
        )

    def test_ignores_other_doi(self, agent_with_pub):
        assert not agent_with_pub.cites_own_paper("a different paper 10.1038/s41557-023-01224-y")

    def test_no_own_dois_returns_false(self, tmp_path, monkeypatch):
        # Prose-only profile (like the real Schultz profile) yields no DOIs.
        monkeypatch.setattr(agent_module, "PROFILES_DIR", tmp_path)
        (tmp_path / "public").mkdir()
        (tmp_path / "private").mkdir()
        (tmp_path / "public" / "schultz.md").write_text("Genetic code expansion lab. No DOIs listed.")
        agent = Agent(agent_id="schultz", bot_name="SchultzBot", pi_name="Peter Schultz")
        assert agent.own_publication_dois == set()
        assert not agent.cites_own_paper(f"cites {SCOPE_DOI}")


class TestScanPromptFlag:
    def test_self_authored_post_is_flagged(self, agent_with_pub):
        posts = [
            {
                "post_id": "1",
                "channel": "chemical-biology",
                "sender": "SChenBot",
                "content_snippet": f"Paper — SCOPE method <https://doi.org/{SCOPE_DOI}>",
            },
            {
                "post_id": "2",
                "channel": "chemical-biology",
                "sender": "SomeBot",
                "content_snippet": "unrelated paper 10.9999/other.123",
            },
        ]
        _system, messages = agent_with_pub.build_phase2_scan_prompt(posts)
        body = messages[0]["content"]
        # Scope to the rendered posts region — the prompt template itself also
        # mentions "SELF-AUTHORED" in its rule text.
        posts_region = body.split("## Posts to review", 1)[1].split("## Selection Criteria", 1)[0]
        # Exactly the self-authored post is flagged; the unrelated one is not.
        assert posts_region.count("⚠️ SELF-AUTHORED") == 1
        assert SCOPE_DOI in posts_region
        post2 = posts_region.split("**Post ID: 2**", 1)[1]
        assert "SELF-AUTHORED" not in post2


class TestReplyPromptCaution:
    def test_own_paper_thread_warns(self, agent_with_pub):
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
        assert "cites a paper your own lab authored" in body
        assert "Speak as its author" in body

    def test_external_paper_thread_no_warning(self, agent_with_pub):
        thread = ThreadState(
            thread_id="t2", channel="chemical-biology", other_agent_id="some", message_count=2
        )
        history = [{"sender": "SomeBot", "content": "Paper — 10.9999/other.123"}]
        _system, messages = agent_with_pub.build_phase4_prompt(
            thread=thread,
            thread_history=history,
            other_agent_name="SomeBot",
            other_agent_lab="Some Lab",
        )
        assert "cites a paper your own lab authored" not in messages[0]["content"]
