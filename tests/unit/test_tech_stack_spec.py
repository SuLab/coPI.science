"""specs/tech-stack.md cites src/models/llm_call_log.py, which does not exist;
LlmCallLog actually lives in src/models/agent_activity.py (issue #26 DOC-4).
"""

from pathlib import Path

SPEC = (Path(__file__).resolve().parents[2] / "specs" / "tech-stack.md").read_text()


def test_no_llm_call_log_file_path():
    assert "llm_call_log.py" not in SPEC


def test_agent_activity_comment_lists_llmcalllog():
    line = next(
        line for line in SPEC.splitlines() if "agent_activity.py" in line
    )
    assert "LlmCallLog" in line
