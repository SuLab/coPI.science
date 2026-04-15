"""Unit tests for podcast pipeline pure-logic functions and RSS builder."""

import json
import os
import tempfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.podcast.pubmed_search import build_queries
from src.podcast.pipeline import (
    _format_candidates_for_prompt,
    _extract_section_text,
    _build_profile_text_from_db,
)
from src.podcast.rss import build_feed
from src.podcast.state import (
    get_delivered_pmids,
    record_delivery,
    get_delivered_pmids_for_user,
    record_delivery_for_user,
)


# ---------------------------------------------------------------------------
# build_queries
# ---------------------------------------------------------------------------

class TestBuildQueries:
    def test_disease_areas_produce_query(self):
        profile = {"disease_areas": ["neurodegeneration", "Alzheimer's disease"], "techniques": [], "experimental_models": [], "keywords": []}
        queries = build_queries(profile)
        assert len(queries) >= 1
        assert "neurodegeneration" in queries[0]

    def test_techniques_produce_second_query(self):
        profile = {
            "disease_areas": ["cancer"],
            "techniques": ["CRISPR", "flow cytometry"],
            "experimental_models": [],
            "keywords": [],
        }
        queries = build_queries(profile)
        assert len(queries) >= 2
        assert any("CRISPR" in q for q in queries)

    def test_keywords_produce_third_query(self):
        profile = {
            "disease_areas": ["diabetes"],
            "techniques": ["proteomics"],
            "experimental_models": [],
            "keywords": ["insulin signaling", "beta cell"],
        }
        queries = build_queries(profile)
        assert len(queries) >= 3
        assert any("insulin signaling" in q or "beta cell" in q for q in queries)

    def test_empty_profile_returns_empty(self):
        queries = build_queries({})
        assert queries == []

    def test_fallback_to_research_summary(self):
        profile = {"research_summary": "Studying ribosome biogenesis mechanisms"}
        queries = build_queries(profile)
        assert len(queries) == 1

    def test_queries_are_quoted_terms(self):
        profile = {"disease_areas": ["proteostasis"], "techniques": [], "experimental_models": [], "keywords": []}
        queries = build_queries(profile)
        assert '"proteostasis"' in queries[0]


# ---------------------------------------------------------------------------
# _format_candidates_for_prompt
# ---------------------------------------------------------------------------

class TestFormatCandidates:
    def test_numbers_candidates_from_one(self):
        records = [
            {"title": "Paper A", "abstract": "Abstract A", "journal": "Nature", "year": 2024},
            {"title": "Paper B", "abstract": "Abstract B", "journal": "Science", "year": 2024},
        ]
        text = _format_candidates_for_prompt(records)
        assert text.startswith("1.")
        assert "2." in text

    def test_includes_title_and_abstract(self):
        records = [{"title": "CRISPR therapy", "abstract": "We developed a new approach.", "journal": "Cell", "year": 2025}]
        text = _format_candidates_for_prompt(records)
        assert "CRISPR therapy" in text
        assert "We developed a new approach." in text

    def test_truncates_long_abstract(self):
        long_abstract = "x" * 1000
        records = [{"title": "T", "abstract": long_abstract, "journal": "J", "year": 2024}]
        text = _format_candidates_for_prompt(records)
        assert len(text) < 1000  # abstract truncated to 600 chars

    def test_handles_missing_fields(self):
        records = [{"title": "Minimal record"}]
        text = _format_candidates_for_prompt(records)
        assert "Minimal record" in text
        assert "No abstract" in text


# ---------------------------------------------------------------------------
# _extract_section_text
# ---------------------------------------------------------------------------

class TestExtractSectionText:
    SAMPLE_MD = """## Research Summary
We study protein folding in neurons.

## Key Methods and Technologies
- Cryo-EM
- Mass spectrometry

## Podcast Preferences
Focus on computational tools only.
"""

    def test_extracts_research_summary(self):
        text = _extract_section_text(self.SAMPLE_MD, "Research Summary")
        assert "protein folding" in text

    def test_extracts_podcast_preferences(self):
        text = _extract_section_text(self.SAMPLE_MD, "Podcast Preferences")
        assert "computational tools" in text

    def test_stops_at_next_section(self):
        text = _extract_section_text(self.SAMPLE_MD, "Research Summary")
        assert "Cryo-EM" not in text

    def test_missing_section_returns_empty(self):
        text = _extract_section_text(self.SAMPLE_MD, "Nonexistent Section")
        assert text == ""


# ---------------------------------------------------------------------------
# RSS feed builder
# ---------------------------------------------------------------------------

def _make_episode(**kwargs):
    """Create a minimal PodcastEpisode-like object for RSS tests."""
    defaults = dict(
        episode_date=date(2026, 4, 10),
        paper_title="A Great Paper",
        paper_authors="Smith J et al.",
        paper_journal="Nature",
        paper_year=2026,
        pmid="12345678",
        paper_url=None,
        text_summary="This paper found something important.",
        audio_file_path=None,
        audio_duration_seconds=None,
        slack_delivered=True,
        selection_justification="Highly relevant to the PI's work.",
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class TestBuildFeed:
    # --- agent path ---

    def test_returns_valid_xml_root(self):
        xml = build_feed("Jane Smith", [], "https://example.com", agent_id="testagent")
        assert xml.startswith("<?xml")
        assert "<rss" in xml

    def test_includes_pi_name_in_channel(self):
        xml = build_feed("Jane Smith", [], "https://example.com", agent_id="testagent")
        assert "Jane Smith" in xml

    def test_agent_feed_url_uses_agent_id(self):
        xml = build_feed("Jane Smith", [], "https://example.com", agent_id="testagent")
        assert "/podcast/testagent/feed.xml" in xml

    def test_single_episode_appears_in_feed(self):
        ep = _make_episode()
        xml = build_feed("Jane Smith", [ep], "https://example.com", agent_id="testagent")
        assert "A Great Paper" in xml
        assert "2026-04-10" in xml

    def test_pubmed_link_used_when_no_paper_url(self):
        ep = _make_episode(pmid="99887766", paper_url=None)
        xml = build_feed("Jane Smith", [ep], "https://example.com", agent_id="testagent")
        assert "pubmed.ncbi.nlm.nih.gov/99887766" in xml

    def test_paper_url_overrides_pubmed_link(self):
        ep = _make_episode(
            pmid="biorxiv:2026.01.01.123456",
            paper_url="https://www.biorxiv.org/content/10.1101/2026.01.01.123456v1",
        )
        xml = build_feed("Jane Smith", [ep], "https://example.com", agent_id="testagent")
        assert "biorxiv.org" in xml
        assert "pubmed.ncbi.nlm.nih.gov" not in xml

    def test_audio_enclosure_when_audio_present(self, tmp_path):
        audio_file = tmp_path / "2026-04-10.mp3"
        audio_file.write_bytes(b"\x00" * 1000)
        ep = _make_episode(audio_file_path=str(audio_file), audio_duration_seconds=90)
        xml = build_feed("Jane Smith", [ep], "https://example.com", agent_id="testagent")
        assert "<enclosure" in xml
        assert 'type="audio/mpeg"' in xml
        assert "<itunes:duration>1:30</itunes:duration>" in xml

    def test_no_enclosure_when_no_audio(self):
        ep = _make_episode(audio_file_path=None)
        xml = build_feed("Jane Smith", [ep], "https://example.com", agent_id="testagent")
        assert "<enclosure" not in xml

    def test_xml_escaping_in_title(self):
        ep = _make_episode(paper_title="Proteins & <Stuff>")
        xml = build_feed("Jane Smith", [ep], "https://example.com", agent_id="testagent")
        assert "Proteins &amp; &lt;Stuff&gt;" in xml

    def test_empty_episodes_list(self):
        xml = build_feed("Jane Smith", [], "https://example.com", agent_id="testagent")
        assert "<item>" not in xml

    def test_agent_guid_format(self):
        ep = _make_episode()
        xml = build_feed("Jane Smith", [ep], "https://example.com", agent_id="testagent")
        assert "testagent-2026-04-10" in xml

    # --- user path ---

    def test_user_feed_url_uses_user_id(self):
        uid = "11111111-2222-3333-4444-555555555555"
        xml = build_feed("Alice Brown", [], "https://example.com", user_id=uid)
        assert f"/podcast/users/{uid}/feed.xml" in xml

    def test_user_feed_has_correct_pi_name(self):
        uid = "11111111-2222-3333-4444-555555555555"
        xml = build_feed("Alice Brown", [], "https://example.com", user_id=uid)
        assert "Alice Brown" in xml

    def test_user_audio_url_uses_user_path(self, tmp_path):
        uid = "11111111-2222-3333-4444-555555555555"
        audio_file = tmp_path / "2026-04-10.mp3"
        audio_file.write_bytes(b"\x00" * 500)
        ep = _make_episode(audio_file_path=str(audio_file))
        xml = build_feed("Alice Brown", [ep], "https://example.com", user_id=uid)
        assert f"/podcast/users/{uid}/audio/2026-04-10.mp3" in xml

    def test_user_guid_format(self):
        uid = "11111111-2222-3333-4444-555555555555"
        ep = _make_episode()
        xml = build_feed("Alice Brown", [ep], "https://example.com", user_id=uid)
        assert f"user-{uid}-2026-04-10" in xml


# ---------------------------------------------------------------------------
# State helpers — user path
# ---------------------------------------------------------------------------

class TestUserState:
    def test_new_user_has_empty_delivered_set(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.podcast.state.STATE_FILE", tmp_path / "state.json")
        result = get_delivered_pmids_for_user("user-uuid-abc")
        assert result == set()

    def test_record_and_retrieve_user_delivery(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.podcast.state.STATE_FILE", tmp_path / "state.json")
        record_delivery_for_user("user-uuid-abc", "12345")
        record_delivery_for_user("user-uuid-abc", "67890")
        result = get_delivered_pmids_for_user("user-uuid-abc")
        assert result == {"12345", "67890"}

    def test_user_and_agent_state_are_independent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.podcast.state.STATE_FILE", tmp_path / "state.json")
        record_delivery("myagent", "11111")
        record_delivery_for_user("user-uuid-abc", "22222")
        assert get_delivered_pmids("myagent") == {"11111"}
        assert get_delivered_pmids_for_user("user-uuid-abc") == {"22222"}
        # no cross-contamination
        assert "22222" not in get_delivered_pmids("myagent")
        assert "11111" not in get_delivered_pmids_for_user("user-uuid-abc")

    def test_duplicate_pmid_not_added_twice(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.podcast.state.STATE_FILE", tmp_path / "state.json")
        record_delivery_for_user("user-uuid-abc", "99999")
        record_delivery_for_user("user-uuid-abc", "99999")
        raw = json.loads((tmp_path / "state.json").read_text())
        assert raw["users"]["user-uuid-abc"]["delivered_pmids"].count("99999") == 1

    def test_atomic_write_leaves_valid_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.podcast.state.STATE_FILE", tmp_path / "state.json")
        record_delivery_for_user("u1", "aaa")
        content = (tmp_path / "state.json").read_text()
        parsed = json.loads(content)  # must be valid JSON
        assert "users" in parsed


# ---------------------------------------------------------------------------
# _build_profile_text_from_db
# ---------------------------------------------------------------------------

class TestBuildProfileTextFromDb:
    def _make_user(self, **kwargs):
        defaults = dict(name="Dr. Alice", institution="MIT", department="Biology")
        defaults.update(kwargs)
        return SimpleNamespace(**defaults)

    def _make_profile(self, **kwargs):
        defaults = dict(
            research_summary="We study protein aggregation.",
            disease_areas=["Alzheimer's", "Parkinson's"],
            techniques=["cryo-EM", "mass spectrometry"],
            experimental_models=["mouse", "iPSC"],
            keywords=["proteostasis", "neurodegeneration"],
        )
        defaults.update(kwargs)
        return SimpleNamespace(**defaults)

    def test_includes_user_name(self):
        text = _build_profile_text_from_db(self._make_user(), self._make_profile())
        assert "Dr. Alice" in text

    def test_includes_research_summary(self):
        text = _build_profile_text_from_db(self._make_user(), self._make_profile())
        assert "protein aggregation" in text

    def test_includes_disease_areas(self):
        text = _build_profile_text_from_db(self._make_user(), self._make_profile())
        assert "Alzheimer" in text

    def test_includes_techniques(self):
        text = _build_profile_text_from_db(self._make_user(), self._make_profile())
        assert "cryo-EM" in text

    def test_handles_none_fields_gracefully(self):
        profile = self._make_profile(disease_areas=None, techniques=None, keywords=None)
        text = _build_profile_text_from_db(self._make_user(), profile)
        assert "protein aggregation" in text  # summary still present

    def test_handles_missing_institution(self):
        user = self._make_user(institution=None, department=None)
        text = _build_profile_text_from_db(user, self._make_profile())
        assert "Dr. Alice" in text
