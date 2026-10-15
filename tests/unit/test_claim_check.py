"""src/agent/claim_check.py and the mode setting (the 2026-10-15 publication-handling
design §11 R6.7, R6.9, R6.11)."""
import logging
import uuid

import pytest

from src.agent import claim_check
from src.agent.agent import Agent
from src.agent.claim_check import (
    FEEDBACK_TEMPLATE,
    RejectionFeedback,
    effective_mode,
    feedback_text,
    record_for_agent,
    root_context,
)
from src.agent.publication_records import LabPublicationRecord, PublicationIds
from src.config import Settings, get_settings


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    (tmp_path / "public").mkdir()
    return tmp_path


def test_mode_defaults_to_log(monkeypatch):
    monkeypatch.delenv("AUTHORSHIP_CLAIM_CHECK_MODE", raising=False)
    assert Settings(_env_file=None).authorship_claim_check_mode == "log"


def test_mode_is_case_and_space_tolerant():
    assert Settings(_env_file=None, authorship_claim_check_mode=" Enforce ").authorship_claim_check_mode == "enforce"


def test_unknown_mode_falls_back_to_log_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="src.config"):
        s = Settings(_env_file=None, authorship_claim_check_mode="shadow")
    assert s.authorship_claim_check_mode == "log"
    expected = ("AUTHORSHIP_CLAIM_CHECK_MODE must be 'log' or 'enforce', got 'shadow' — "
                "falling back to 'log'.")
    assert [r.getMessage() for r in caplog.records].count(expected) == 1


def test_hub_roles_are_always_log():
    assert effective_mode("scout_hub", "enforce") == "log"
    assert effective_mode("pi_lab", "enforce") == "enforce"


def test_start_banner_names_the_mode(monkeypatch, caplog):
    from src.agent.main import _log_start_banner

    monkeypatch.setenv("AUTHORSHIP_CLAIM_CHECK_MODE", "log")
    get_settings.cache_clear()
    try:
        with caplog.at_level(logging.INFO, logger="src.agent.main"):
            _log_start_banner([], "60m", 0, True)
    finally:
        get_settings.cache_clear()
    assert "Authorship claim check: mode log (scout_hub always log)" in caplog.text


def test_record_for_hub_is_empty(profiles):
    (profiles / "public" / "blackbird.md").write_text(
        "# Blackbird\n\nhttps://doi.org/10.1/x\n", encoding="utf-8")
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    assert record_for_agent(hub).has_records is False


def test_record_for_lab_is_its_publication_record(profiles):
    lab = Agent("wang", "WangBot", "Jane Wang", role="pi_lab")
    lab.set_publication_ids(PublicationIds(
        in_use_dois=frozenset({"10.1/x"}), in_use_pmids=frozenset(),
        not_theirs_dois=frozenset(), not_theirs_pmids=frozenset(), row_count=1,
    ))
    record = record_for_agent(lab)
    assert record == lab.publication_record()
    assert "10.1/x" in record.dois


def test_record_for_unknown_agent_is_empty():
    assert record_for_agent(None) == LabPublicationRecord()


def test_feedback_store_is_lru_capped():
    store = RejectionFeedback()
    for i in range(1001):
        store.note("wang", f"t{i}", "r")
    assert len(store) == 1000
    assert store.get("wang", "t0") is None
    assert store.get("wang", "t1000") == "r"


def test_feedback_text_is_exact():
    assert feedback_text("X") == FEEDBACK_TEMPLATE.format(reason="X") == (
        "Your previous draft was not posted: X. Cite the DOI or PubMed link from your Recent "
        "Publications section for any paper you describe as your lab's, or rephrase without "
        "claiming authorship."
    )


def test_clear_thread_removes_every_agents_entry():
    store = RejectionFeedback()
    store.note("wang", "T", "a")
    store.note("blackbird", "T", "b")
    store.note("wang", "U", "c")
    store.clear_thread("T")
    assert (store.get("wang", "T"), store.get("blackbird", "T"), store.get("wang", "U")) == (None, None, "c")


def test_root_context_rejoins_only_the_root_authors_chunks():
    following = [("wang", "two"), ("wang", "three"), ("blackbird", "reply"), ("wang", "later")]
    assert root_context("wang", "one", following) == "one\ntwo\nthree"
    assert root_context(None, "one", following) == "one"


async def test_record_check_without_a_factory_or_run_does_nothing():
    calls = []

    def factory():
        calls.append(1)
        raise AssertionError("no DB call")

    kwargs = dict(agent_id="wang", thread_key="phase5", phase="new_post", mode="log",
                  outcome="no_claim", reasons=(), excerpt="x")
    await claim_check.record_check(None, simulation_run_id=uuid.uuid4(), **kwargs)
    await claim_check.record_check(factory, simulation_run_id=None, **kwargs)
    assert calls == []
