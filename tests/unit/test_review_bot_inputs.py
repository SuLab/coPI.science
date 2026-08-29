"""Task 9: dependency-free transcript loader, review-bot model setting, prompt file.

These are narrow existence/shape checks, not behavioral tests of the loader logic
itself (which is unchanged — moved verbatim from `assessment_detail._load_thread_messages`
and already exercised indirectly by `tests/integration/test_assessment_detail_page.py`).
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_transcript_loader_module_is_dependency_free():
    import ast

    src = (ROOT / "src/services/interview_transcript.py").read_text()
    tree = ast.parse(src)
    mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {
        n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    for banned in ("blackbird_rubric", "rubric_revisions", "assessment_detail"):
        assert not any(banned in m for m in mods), mods


def test_review_model_setting_default():
    from src.config import Settings

    # _env_file=None: the default must not depend on the host's .env — the runbook
    # explicitly contemplates setting LLM_REVIEW_MODEL there, and get_settings()
    # reads .env. Precedent: test_config_secret_redaction.py:120.
    assert Settings(_env_file=None).llm_review_model == "claude-opus-5"


def test_review_bot_prompt_exists():
    assert (ROOT / "prompts/review-bot.md").is_file()
