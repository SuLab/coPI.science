"""UI-copy accuracy check: the research-summary word-count
hint shown to PIs must not say "150-250 words" while the validator that actually
gates submission (``_MIN_SUMMARY_WORDS``/``_MAX_SUMMARY_WORDS`` in
``src/services/profile_pipeline.py``) enforces 100-350. A PI who wrote a
280-word summary trusting the on-screen hint must not have it silently pass
validation while believing they'd exceeded the stated range — or, worse,
trim a compliant summary to chase a number the code never checks.

This only touches template copy shown to humans; the LLM-facing prompt text
in profile_pipeline.py is out of scope and stays byte-identical.
"""

from pathlib import Path

from src.services.profile_pipeline import _MAX_SUMMARY_WORDS, _MIN_SUMMARY_WORDS

REPO_ROOT = Path(__file__).resolve().parents[2]

TEMPLATES = [
    REPO_ROOT / "templates" / "onboarding" / "profile_review.html",
    REPO_ROOT / "templates" / "profile" / "edit.html",
    REPO_ROOT / "templates" / "agent" / "public_profile.html",
]


def test_no_template_advertises_the_stale_150_250_word_range():
    for path in TEMPLATES:
        text = path.read_text()
        assert "150-250" not in text, f"{path} still says 150-250"
        assert "150–250" not in text, f"{path} still says 150-250 (en dash)"


def test_templates_advertise_the_enforced_word_range():
    expected = f"{_MIN_SUMMARY_WORDS}-{_MAX_SUMMARY_WORDS}"
    for path in TEMPLATES:
        text = path.read_text()
        assert expected in text, f"{path} does not mention the enforced range {expected}"
