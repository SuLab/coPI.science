"""Unit tests for _validate_profile null/type safety."""

from src.services.profile_pipeline import _validate_profile

_GOOD_SUMMARY = " ".join(["word"] * 200)  # 200 words: inside 100-350


def test_valid_profile_passes():
    assert _validate_profile(
        {
            "research_summary": _GOOD_SUMMARY,
            "techniques": ["a", "b", "c"],
            "disease_areas": ["x"],
        }
    ) is True


def test_none_profile_fails():
    assert _validate_profile(None) is False


def test_empty_profile_fails():
    assert _validate_profile({}) is False


def test_non_dict_list_profile_does_not_raise():
    # A fenced JSON array from extract_json must not reach `.get()` here and
    # crash with AttributeError one frame before apply_synthesis's own guard
    # (vet_publications.py and resynth_from_current_pubs.py call
    # _validate_profile directly).
    assert _validate_profile([1, 2, 3]) is False


def test_non_dict_string_profile_does_not_raise():
    assert _validate_profile("x") is False


def test_null_research_summary_does_not_raise():
    assert _validate_profile({"research_summary": None}) is False


def test_non_string_research_summary_does_not_raise():
    assert _validate_profile({"research_summary": 123}) is False


def test_string_techniques_does_not_raise_and_fails_validation():
    # A `techniques` value that is a string (not a list) used to score
    # `len("PCR") == 3` and PASS, then get assigned straight into an
    # ARRAY(String) column at :409.
    assert _validate_profile(
        {"research_summary": _GOOD_SUMMARY, "techniques": "PCR", "disease_areas": ["x"]}
    ) is False


def test_null_techniques_does_not_raise():
    assert _validate_profile(
        {"research_summary": _GOOD_SUMMARY, "techniques": None, "disease_areas": ["x"]}
    ) is False


def test_non_list_disease_areas_does_not_raise():
    assert _validate_profile(
        {"research_summary": _GOOD_SUMMARY, "techniques": ["a", "b", "c"], "disease_areas": "cancer"}
    ) is False


def test_word_gate_matches_the_log_and_progress_text_100_350():
    # 50 and 400 words must both fail (outside 100-350); the pre-fix gate
    # numbers agreed with this, but the progress text / log message said
    # "150-250" — this pins that gate, log and progress text agree on 100-350.
    # (The retry PROMPT at :324 is deliberately left saying 150-250: no prompt edits.)
    too_short = " ".join(["word"] * 50)
    too_long = " ".join(["word"] * 400)
    base = {"techniques": ["a", "b", "c"], "disease_areas": ["x"]}
    assert _validate_profile({**base, "research_summary": too_short}) is False
    assert _validate_profile({**base, "research_summary": too_long}) is False
    assert _validate_profile({**base, "research_summary": _GOOD_SUMMARY}) is True


def test_word_gate_constants_match_the_documented_default():
    from src.services import profile_pipeline

    assert profile_pipeline._MIN_SUMMARY_WORDS == 100
    assert profile_pipeline._MAX_SUMMARY_WORDS == 350
