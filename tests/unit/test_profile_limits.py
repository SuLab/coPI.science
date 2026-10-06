from src.services.profile_limits import TAG_LIST_MAX_ITEMS, TAG_MAX_CHARS, cap_tags, clean_tag


def test_clean_tag_removes_newlines_and_leading_hashes():
    assert clean_tag("## CRISPR\nscreens") == "CRISPR screens"
    assert clean_tag("  #  ") == ""


def test_a_long_tag_is_cut_at_a_word_boundary():
    tag = ("word " * 60).strip()
    cut = clean_tag(tag)
    assert len(cut) <= TAG_MAX_CHARS and not cut.endswith(" ") and tag.startswith(cut)
    assert clean_tag("x" * 250) == "x" * TAG_MAX_CHARS


def test_cap_tags_keeps_order_drops_empties_and_caps_the_count():
    values = ["#a", "", "b\nc", 3, *[f"t{i}" for i in range(40)]]
    got = cap_tags(values)
    assert got[:2] == ["a", "b c"] and len(got) == TAG_LIST_MAX_ITEMS


def test_a_valid_word_count_cannot_hide_an_oversized_generated_summary():
    from src.services.profile_limits import SUMMARY_MAX_CHARS
    from src.services.profile_pipeline import _validate_profile

    profile = {"research_summary": "word " * 100,
               "techniques": ["a", "b", "c"], "disease_areas": ["cancer"]}
    assert _validate_profile(profile)
    profile["research_summary"] = "x" * SUMMARY_MAX_CHARS + " word" * 99
    assert len(profile["research_summary"].split()) == 100
    assert not _validate_profile(profile)
