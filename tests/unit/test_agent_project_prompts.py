"""Project-thread and project-reply prompts (spec §5.7, §5.8): `build_project_thread_prompt`,
`build_project_reply_post_prompt`, read from `prompts/projects/`, and `_compose_system_prompt`'s
default kept byte-identical."""

import re
import shutil

import pytest

from src.agent import project_prompts, roles
from src.agent.agent import (
    NO_PUBLICATION_RECORDS_SECTION,
    Agent,
    _default_system_prompt,
    render_lab_profile_section,
)
from src.agent.project_guidance import project_turn
from src.agent.state import ThreadState
from src.services.blackbird_rubric import render_rubric_markdown

TOKEN_RE = re.compile(r"\{[a-z_]+\}")
PERSONA = "# Gordy Lab\n\n## Research Summary\nMetabolomics of {small} cohorts.\n"
MEMORY = "Talked about lipid panels."

PROJECT = {
    "project_id": "p1",
    "title": "Lipid panel response in a mouse model",
    "summary": "Test whether the lab's lipidomics workflow reads out drug response.",
    "plan": {
        "indication": "Atherosclerosis",
        "target": "LOX-1",
        "model": "ApoE-/- mice",
        "positive_control": "to settle with the lab",
        "negative_control": "Vehicle",
        "standard_of_care": "Statins",
        "readout": "Plasma lipidomics",
        "design": "Two arms",
        "time_and_budget": "9 months, $200K",
        "lab_tools": ["LC-MS lipidomics", "ApoE-/- mice"],
    },
    "anchors": [{"section": "Model Systems", "item": "ApoE-/- mice"}],
    "sources": [
        {"key": "pmid:31500698", "kind": "pubmed", "title": "A LOX-1 paper"},
        {"key": "nct:NCT01903460", "kind": "trial", "title": "A statin trial"},
    ],
    "why_chosen": "It uses the lab's own model.",
}


def _fixed(agent: Agent) -> Agent:
    agent._public_profile = PERSONA
    agent._public_working_memory = MEMORY
    return agent


def _hub():
    return _fixed(Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub"))


def _lab():
    return _fixed(Agent("gordy", "GordyBot", "Gordy Lab"))


def _expected_system(agent: Agent, *, trailing_newline: bool) -> str:
    """`_compose_system_prompt` as it was before ``base_prompt`` existed."""
    base = agent._load_prompt("agent-system.md", _default_system_prompt())
    if "{rubric}" in base:
        base = base.replace("{rubric}", render_rubric_markdown())
    header = f"{base}\n\n{agent._render_identity()}"
    if not agent._is_hub():
        header += "\n\n" + render_lab_profile_section(agent.public_profile)
        if agent.has_publication_records is False:
            header += "\n\n" + NO_PUBLICATION_RECORDS_SECTION
    prompt = f"{header}\n\n## Your Working Memory\n{MEMORY}"
    return f"{prompt}\n" if trailing_newline else prompt


@pytest.mark.parametrize("make", [_hub, _lab])
def test_default_system_prompt_is_unchanged(make):
    agent = make()
    assert agent.build_system_prompt() == _expected_system(agent, trailing_newline=True)
    assert agent.build_thread_reply_system_prompt() == _expected_system(
        agent, trailing_newline=False
    )
    own = agent._load_prompt("agent-system.md", _default_system_prompt())
    assert agent._compose_system_prompt(trailing_newline=True, base_prompt=own) == (
        agent.build_system_prompt()
    )


def _thread(message_count: int = 5) -> ThreadState:
    return ThreadState(
        thread_id="r1", channel="general", other_agent_id="gordy", message_count=message_count,
    )


def _history():
    return [
        {"sender": "GordyBot", "sender_agent_id": "gordy", "content": "✅ We can do it."},
        {"sender": "BlackbirdBot", "sender_agent_id": "blackbird", "content": "Which controls?"},
    ]


def _project_prompt(agent: Agent, *, other_name="GordyBot", other_lab="Gordy Lab", feedback=None):
    turn = project_turn(agent.role, 6, lab_agrees=False)
    return agent.build_project_thread_prompt(
        thread=_thread(),
        thread_history=_history(),
        other_agent_name=other_name,
        other_agent_lab=other_lab,
        project=PROJECT,
        turn=turn,
        authorship_feedback=feedback,
    ), turn


def test_hub_project_prompt_uses_the_project_system_file():
    (system, messages), turn = _project_prompt(_hub(), other_lab="Gordy Lab")
    assert "BlackbirdBot in a project thread" in system
    assert render_rubric_markdown() not in system
    assert "{rubric}" not in system
    assert render_rubric_markdown() in _hub().build_system_prompt(), "control: the default has it"
    assert not system.endswith("\n")
    assert "You are **BlackbirdBot**" in system
    content = messages[0]["content"]
    assert "Refining a project with a lab" in content
    assert turn.guidance in content and turn.instructions in content
    assert f"— {turn.label}" in content
    assert "Message 6 of at most 12" in content
    # The hub fences what the lab wrote.
    assert "<lab_message>" in content


def test_lab_project_prompt_uses_the_thread_reply_system_prompt():
    lab = _lab()
    (system, messages), _turn = _project_prompt(lab, other_name="BlackbirdBot", other_lab="Blackbird")
    assert system == lab.build_thread_reply_system_prompt()
    content = messages[0]["content"]
    assert "Refining a Blackbird project" in content
    assert "You are talking with BlackbirdBot (Blackbird)." in content


@pytest.mark.parametrize("make", [_hub, _lab])
def test_every_placeholder_is_replaced(make):
    (_system, messages), _turn = _project_prompt(make())
    assert TOKEN_RE.findall(messages[0]["content"]) == []


@pytest.mark.parametrize("make", [_hub, _lab])
def test_project_block_is_fenced_and_complete(make):
    (_system, messages), _turn = _project_prompt(make())
    content = messages[0]["content"]
    start = content.index("<project_brief>")
    block = content[start:content.index("</project_brief>", start)]
    for text in (
        "Title: Lipid panel response in a mouse model",
        "Summary: Test whether",
        "Opening plan:",
        "• Indication: Atherosclerosis",
        "• The lab's tools and models used: LC-MS lipidomics; ApoE-/- mice",
        "Builds on: Model Systems: ApoE-/- mice",
        "• PMID 31500698 — A LOX-1 paper",
        "• NCT01903460 — A statin trial",
        "Why chosen: It uses the lab's own model.",
    ):
        assert text in block
    assert block.index("Indication") < block.index("Target or mechanism") < block.index(
        "Time and budget"
    )


def test_authorship_feedback_is_appended():
    (_system, messages), _turn = _project_prompt(_lab(), feedback="NOTICE: claim blocked.")
    assert messages[0]["content"].endswith("\n\nNOTICE: claim blocked.")


def test_reply_post_prompt_fences_the_posting():
    lab = _lab()
    posting = ":test_tube: *Blackbird project for the Gordy lab*\nA plan."
    system, messages = lab.build_project_reply_post_prompt(
        posting_text=posting, title="Lipid panel response", authorship_feedback=None,
    )
    assert system == lab.build_system_prompt()
    content = messages[0]["content"]
    assert f"<project_post>\n{posting}\n</project_post>" in content
    assert '"Lipid panel response"' in content
    assert TOKEN_RE.findall(content) == []


def test_reply_post_prompt_appends_feedback():
    _system, messages = _lab().build_project_reply_post_prompt(
        posting_text="x", title="t", authorship_feedback="NOTICE",
    )
    assert messages[0]["content"].endswith("\n\nNOTICE")


@pytest.fixture
def project_tree(tmp_path, monkeypatch):
    """A copy of prompts/ with a marker appended to each project prompt; prompts/ is never
    touched."""
    shutil.copytree("prompts", tmp_path / "prompts")
    monkeypatch.setattr(roles, "PROMPTS_DIR", tmp_path / "prompts")
    monkeypatch.setattr(roles, "ROLES_DIR", tmp_path / "prompts" / "roles")
    for name in project_prompts.ENGINE_PROMPT_FILES:
        path = tmp_path / "prompts" / "projects" / name
        path.write_text(path.read_text(encoding="utf-8") + f"\nMARKER-{name}", encoding="utf-8")
    return tmp_path / "prompts"


def test_each_builder_reads_its_own_projects_file(project_tree):
    (hub_system, hub_messages), _turn = _project_prompt(_hub())
    assert "MARKER-hub-system.md" in hub_system
    assert "MARKER-hub-thread-reply.md" in hub_messages[0]["content"]
    lab = _lab()
    (lab_system, lab_messages), _turn = _project_prompt(lab)
    assert lab_system == lab.build_thread_reply_system_prompt()
    assert "MARKER-lab-thread-reply.md" in lab_messages[0]["content"]
    _system, messages = lab.build_project_reply_post_prompt(
        posting_text="x", title="t", authorship_feedback=None,
    )
    assert "MARKER-lab-posting-reply.md" in messages[0]["content"]
    # No interview prompt reads a project file.
    for agent in (_hub(), lab):
        assert "MARKER-" not in agent.build_system_prompt()
        assert "MARKER-" not in agent.build_thread_reply_system_prompt()
