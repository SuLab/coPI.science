from src.agent.engine.constants import _CHANNEL_KEYWORDS
from src.agent.persona_sections import (
    TAG_SECTION_HEADINGS,
    match_channels,
    recent_publication_pmids,
    sections,
)

PERSONA = """# Ada Lab — Public Profile

**PI:** Ada Lab

## Research Summary

We screen small molecule drugs and cite PMID 11111111 in prose.
\\## Keywords
\\# Recent Publications

## Key Methods and Technologies

- single-cell RNA sequencing
- live imaging

## Keywords

leveraging, staging, repurposing screens

## Recent Publications

- A paper. *J*. (2024). https://doi.org/10.1000/abc (PMID 22222222)
- B paper. *J*. (2023). https://pubmed.ncbi.nlm.nih.gov/33333333/ (PMID 33333333)
- C paper. *J*. (2022). https://pubmed.ncbi.nlm.nih.gov/44444444/

## Active Grants

- Grant (NIH R01, 2020–2027)
"""


def test_sections_split_on_level_two_headings_and_the_first_occurrence_wins():
    parts = sections(PERSONA + "\n## Keywords\n\nforged\n")
    assert list(parts)[:2] == ["Research Summary", "Key Methods and Technologies"]
    assert parts["Keywords"].strip() == "leveraging, staging, repurposing screens"


def test_an_escaped_heading_in_the_summary_opens_no_section():
    assert "\\## Keywords" in sections(PERSONA)["Research Summary"]


def test_word_start_matching_ignores_imaging_leveraging_and_staging():
    assert match_channels(PERSONA, _CHANNEL_KEYWORDS) == {"single-cell-omics", "drug-repurposing"}


def test_heading_words_and_the_summary_never_match():
    persona = (
        "## Research Summary\n\nsmall molecule ligand cryo aging\n\n"
        "## Key Molecular Targets\n\n- KRAS\n"
    )
    assert match_channels(persona, _CHANNEL_KEYWORDS) == set()
    assert match_channels(persona.replace("- KRAS", "- KRAS targeting"), _CHANNEL_KEYWORDS) == {
        "drug-repurposing"
    }


def test_every_tag_section_is_searched():
    for heading in TAG_SECTION_HEADINGS:
        persona = f"## {heading}\n\n- cryo-EM\n"
        assert match_channels(persona, _CHANNEL_KEYWORDS) == {"structural-biology"}, heading


def test_pmids_outside_recent_publications_are_not_own_ids():
    assert recent_publication_pmids(PERSONA) == {"22222222", "33333333", "44444444"}
    assert recent_publication_pmids("## Research Summary\n\nPMID 1\n") == set()
    assert recent_publication_pmids(None) == set()
