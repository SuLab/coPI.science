import re
from pathlib import Path

from src.agent.agent import Agent
from src.agent.role_capabilities import LAB_BRIEF_FILE, ROLE_CAPABILITIES

BRIEF = Path("prompts/roles/scout_hub/lab-brief.md")


def _text() -> str:
    return BRIEF.read_text(encoding="utf-8")


def _norm() -> str:
    return " ".join(_text().split()).lower()


def test_the_brief_is_short_and_raw():
    text = _text()
    assert 250 <= len(text.split()) <= 600
    assert "{" not in text and "}" not in text
    assert text.endswith("\n") and not text.endswith("\n\n")


def test_the_brief_repeats_none_of_the_p10_contradictions():
    low = _norm()
    for phrase in ("elite", "prestig", "johns hopkins", "vc firms", "≥2", "two vc",
                   "investor concern", "single-asset"):
        assert phrase not in low, phrase


def test_the_brief_states_what_the_rubric_says_a_lab_need_not_have():
    norm = _norm()
    assert "$100k–$1m" in norm
    assert "single asset" in norm and "normal shape" in norm
    assert "freedom to operate" in norm
    assert "nothing filed" in norm
    assert "where you work is not part of the screen" in norm


def test_the_brief_discloses_no_rubric_weights_or_thresholds():
    low = _norm()
    assert not re.search(r"\d\s*%|weight|threshold|\b3\.4\b|\b2\.8\b|band", low)


def test_the_brief_is_in_the_hub_prompt_set_and_never_composed():
    assert ROLE_CAPABILITIES["scout_hub"].prompt_files[-1] == LAB_BRIEF_FILE
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    first_line = _text().splitlines()[0]
    assert first_line not in hub.build_thread_reply_system_prompt()
