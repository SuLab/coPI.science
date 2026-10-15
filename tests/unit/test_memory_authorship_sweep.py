"""The working-memory authorship sweep (the 2026-10-15 publication-handling design §11
R6.13, main SPEC GATE-13): the rules on main's memory lines, then the engine's single write
point in log and enforce mode."""
import logging
import types
import uuid

import pytest

from src.agent import claim_check
from src.agent.authorship_rules import lab_self_names, strip_ungrounded_authorship_lines
from src.agent.mentions import RosterEntry
from src.agent.publication_records import LabPublicationRecord, PublicationIds
from src.config import get_settings
from tests.characterization.freeze.fixtures import (
    apply_frozen_settings,
    build_engine,
    install_profiles,
)

B = "10.1093/bioinformatics/btad570"
R = (
    RosterEntry("good", "GoodBot", "Benjamin Good"),
    RosterEntry("wu", "WuBot", "Chunlei Wu"),
    RosterEntry("su", "SuBot", "Andrew Su"),
    RosterEntry("chin", "ChinBot", "Jason Chin"),
    RosterEntry("blackbird", "BlackbirdBot", None),
)
GOOD = LabPublicationRecord()
WU = LabPublicationRecord(dois=frozenset({B}), has_records=True)
GOOD_NAMES = lab_self_names("good", "GoodBot", "Benjamin Good")
WU_NAMES = lab_self_names("wu", "WuBot", "Chunlei Wu")
CLAIM_LINE = "Our 2023 Nature paper on KGs is relevant."
MEMORY = f"## Ideas pitched\n{CLAIM_LINE}\n1. Resume outreach.\n"


def _strip(line, own=GOOD, names=GOOD_NAMES, speaker="good"):
    return strip_ungrounded_authorship_lines(line, own, names, speaker_id=speaker, roster=R)


# --- rules (main SPEC §11 memory lines, and two of main's guard tests) ----------------------

@pytest.mark.parametrize("line", [
    "Co-authors on Desiderata with Wu Lab.",
    "Joint paper with Wu Lab on KG desiderata.",
    "Shared authorship with @WuBot on Desiderata.",
    "Our 2023 paper on KGs is relevant to Wu.",
    "Co-first authors with Su Lab on the benchmark.",
    "Good lab co-authored the Desiderata paper.",
    "Good group co-authored the Desiderata paper.",
    "Wu Lab (@WuBot) co-authored the Desiderata paper with our lab.",
    "Good Lab co-authored the Desiderata paper.",
])
def test_memory_lines_stripped(line):
    assert _strip(line)[1] == [line]


def test_memory_line_kept():
    line = "Wu Lab and Su Lab are co-authors on Desiderata."
    assert _strip(line) == (line, [])


def test_memory_grounded_line_kept():
    line = f"Co-authored https://doi.org/{B} with the Su lab."
    assert _strip(line, own=WU, names=WU_NAMES, speaker="wu") == (line, [])


def test_lines_without_authorship_are_untouched():
    assert _strip("1. Resume outreach.") == ("1. Resume outreach.", [])


# --- engine ------------------------------------------------------------------------------

class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _Session:
    """Answers the memory writer's AgentRegistry lookup; nothing else is asked of it."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _statement):
        return _Result(types.SimpleNamespace(id=uuid.uuid4()))

    async def commit(self):
        return None


@pytest.fixture
def world(tmp_path, monkeypatch):
    def make(mode: str, response: str, *, agent_key: str = "lab"):
        apply_frozen_settings(monkeypatch)
        monkeypatch.setenv("AUTHORSHIP_CLAIM_CHECK_MODE", mode)
        get_settings.cache_clear()
        root = install_profiles(tmp_path, monkeypatch)
        eng, agents, _clients = build_engine()
        agent = agents[agent_key]
        if agent_key == "lab":  # wang without records: a DOI-less persona and no rows
            (root / "public" / "wang.md").write_text("# Jane Wang Lab\n\nNo publications.\n",
                                                    encoding="utf-8")
            agent.reload_profiles()
            agent.set_publication_ids(PublicationIds())
        rows, revisions = [], []

        async def _collect(session_factory, **kwargs):
            rows.append(kwargs)

        async def _generate(**_kwargs):
            return response

        async def _revision(db, **kwargs):
            revisions.append(kwargs["content"])

        async def _on_roster(_agent_id):
            return True

        monkeypatch.setattr(claim_check, "record_check", _collect)
        monkeypatch.setattr("src.agent.engine.deps.generate_agent_response", _generate)
        monkeypatch.setattr("src.services.profile_versioning.create_revision", _revision)
        monkeypatch.setattr(eng.memory, "_still_on_roster", _on_roster)
        eng.ctx.session_factory = _Session
        memory_file = root / "memory" / agent.agent_id / "public.md"
        return types.SimpleNamespace(eng=eng, agent=agent, rows=rows, revisions=revisions,
                                     memory_file=memory_file)
    yield make
    get_settings.cache_clear()


async def test_a_rules_fault_keeps_memory_unswept(world, monkeypatch, caplog):
    # Audit defect 2: the sweep's rules raise; the memory text is kept as written.
    from src.agent.engine import memory as memory_mod

    def boom(*args, **kwargs):
        raise RuntimeError("rules bug")

    w = world("enforce", MEMORY)
    monkeypatch.setattr(memory_mod, "strip_ungrounded_authorship_lines", boom)
    assert await w.eng.memory._sweep_authorship_lines(w.agent, MEMORY, "public", None) == MEMORY
    assert w.rows == []
    assert "Memory authorship sweep failed; memory kept unswept" in caplog.text


async def test_log_mode_records_and_keeps_memory(world):
    w = world("log", MEMORY)
    await w.eng._update_agent_memory(w.agent, "thread closed")
    text = w.memory_file.read_text(encoding="utf-8")
    assert CLAIM_LINE in text and "1. Resume outreach." in text
    assert len(w.rows) == 1
    row = w.rows[0]
    assert (row["phase"], row["mode"], row["outcome"], row["thread_key"]) == (
        "memory", "log", "rejected", "memory:public")
    assert row["reasons"][0].code == "memory_ungrounded"


async def test_enforce_strips_file_and_revision(world):
    w = world("enforce", MEMORY)
    await w.eng._update_agent_memory(w.agent, "thread closed")
    text = w.memory_file.read_text(encoding="utf-8")
    assert CLAIM_LINE not in text and "1. Resume outreach." in text
    assert len(w.revisions) == 1
    assert CLAIM_LINE not in w.revisions[0] and "1. Resume outreach." in w.revisions[0]
    assert w.rows[0]["mode"] == "enforce"


async def test_enforce_blank_result_keeps_existing_memory(world, caplog):
    w = world("enforce", CLAIM_LINE)
    w.memory_file.parent.mkdir(parents=True, exist_ok=True)
    w.memory_file.write_text("old\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="src.agent.simulation"):
        await w.eng._update_agent_memory(w.agent, "thread closed")
    assert w.memory_file.read_text(encoding="utf-8") == "old\n"
    assert w.revisions == []
    assert ("Memory update: every line was an ungrounded authorship claim — keeping the "
            "existing working memory") in caplog.text


async def test_hub_memory_is_never_stripped(world):
    w = world("enforce", "We published 10.1/x.", agent_key="hub")
    await w.eng._update_agent_memory(w.agent, "thread closed")
    assert "We published 10.1/x." in w.memory_file.read_text(encoding="utf-8")
    assert w.rows[0]["mode"] == "log"


async def test_no_authorship_lines_no_row(world):
    w = world("log", "1. Resume outreach.")
    await w.eng._update_agent_memory(w.agent, "thread closed")
    assert w.rows == []
