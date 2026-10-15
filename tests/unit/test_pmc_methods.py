"""R2.12 (main PM-8, PM-9): PMC methods locator and serialiser."""
import pytest

from src.services.pmc_methods import extract_methods_section as extract

_ABSTRACT_SHAPE = (  # PMC2939063: a structured abstract's Methods must not win
    "<pmc-articleset><article><front><article-meta><abstract><sec>"
    "<title>Methodology/Principal Findings</title><p>Abstract methods text.</p></sec>"
    "</abstract></article-meta></front><body><sec><title>Introduction</title><p>Intro.</p></sec>"
    "<sec><title>Methods</title><sec><title>Chemistry: General methods</title>"
    "<p>All reaction solvents were of reagent grade.</p></sec></sec></body></article></pmc-articleset>"
)
#: test_prompt_freeze_gm.py:903-907's _PMC_XML, copied verbatim (that module is frozen).
_GOLDEN_PMC_XML = (
    "<pmc-articleset><article><body><sec><title>Methods</title>"
    "<p>Organoids were derived from resected tissue.</p><p>Base editing used ABE8e.</p>"
    "</sec></body></article></pmc-articleset>"
)


def _sec(title: str, body: str = "<p>x</p>") -> str:
    return f"<article><body><sec><title>{title}</title>{body}</sec></body></article>"


def test_abstract_sections_are_never_selected():
    assert extract(_ABSTRACT_SHAPE) == (
        "Methods\nChemistry: General methods\nAll reaction solvents were of reagent grade.")


def test_a_results_subsection_does_not_win():  # PMC4262987 shape
    xml = ('<article><body><sec><title>Results</title><sec><title>A new method for imaging</title>'
           '<p>r</p></sec></sec><sec sec-type="materials|methods"><title>Materials and methods</title>'
           '<sec><title>Software and configurations</title><p>Mugsy [<xref ref-type="bibr" rid="B36">'
           '36</xref>] v1.23.</p></sec></sec></body></article>')
    assert extract(xml) == "Materials and methods\nSoftware and configurations\nMugsy [36] v1.23."


def test_inline_markup_is_joined_without_spaces():
    xml = ("<article><body><sec><title>Methods</title><sec><title><italic>Amycolatopsis</italic>"
           " genomes</title><p>H<sub>2</sub>O<sub>2</sub> in <italic>E. coli</italic>’s cytosol"
           "</p></sec></sec></body></article>")
    assert extract(xml) == "Methods\nAmycolatopsis genomes\nH2O2 in E. coli’s cytosol"


def test_markup_wrapped_title_is_matched():
    assert extract(_sec("<bold>Materials and Methods</bold>")) == "Materials and Methods\nx"


@pytest.mark.parametrize("title, shown", [
    ("2 METHODS", "2 METHODS"), ("4 . Material and methods", "4 . Material and methods"),
    ("STAR★Methods", "STAR★Methods"), ("Materials &amp; Methods", "Materials & Methods"),
    ("IV. Methods", "IV. Methods"), ("Online Methods", "Online Methods"),
    ("Experimental Section", "Experimental Section"),
])
def test_numbered_and_decorated_titles(title, shown):
    assert extract(_sec(title)) == f"{shown}\nx"


def test_no_methods_section_returns_none():
    xml = ("<article><body><sec><title>ALGORITHMS</title><p>a</p><sec><title>Comparison with "
           "existing methods</title><p>c</p></sec></sec><sec><title>RESULTS</title><p>r</p></sec>"
           "</body></article>")
    assert extract(xml) is None


def test_back_matter_methods_used_only_when_body_has_none():
    back = "<back><sec><title>Methods</title><p>m</p></sec></back>"
    assert extract(f"<article><body><sec><title>Results</title><p>r</p></sec></body>{back}</article>") == "Methods\nm"
    assert extract(f"<article><body><sec><title>Methods</title><p>b</p></sec></body>{back}</article>") == "Methods\nb"


def test_title_only_methods_section_is_skipped():
    xml = ("<article><body><sec><title>Methods</title></sec><sec><title>Experimental procedures"
           "</title><p>e</p></sec></body></article>")
    assert extract(xml) == "Experimental procedures\ne"


def test_tables_figures_and_references_are_dropped():
    xml = ("<article><body><sec><title>Methods</title><p>Cells got H<sub>2</sub>O<sub>2</sub> "
           "(10 µM).</p><table-wrap><caption><p>Table 1</p></caption><table><tr><td>a</td>"
           "<td>b</td></tr></table></table-wrap><p>After table.</p></sec></body><back><ref-list>"
           "<ref>R1</ref></ref-list></back></article>")
    assert extract(xml) == "Methods\nCells got H2O2 (10 µM).\nAfter table."


def test_namespaced_jats_is_handled():
    namespaced = _ABSTRACT_SHAPE.replace("<article>", '<article xmlns="http://jats.nlm.nih.gov">')
    assert extract(namespaced) == extract(_ABSTRACT_SHAPE)
    # Moved from test_pubmed_parser_fidelity.py (D46's pin is superseded).
    assert extract(_sec("2 METHODS", "<p>We did it.</p>")) == "2 METHODS\nWe did it."
    assert extract('<article xmlns="http://jats.nlm.nih.gov"><body><sec><title>Methods</title>'
                   "<p>x</p></sec></body></article>") == "Methods\nx"


def test_metadata_only_record_returns_none():
    assert extract("<pmc-articleset><article><front><article-meta><title-group><article-title>T"
                   "</article-title></title-group></article-meta></front></article></pmc-articleset>") is None


def test_malformed_xml_returns_none():
    assert extract("<article><body>") is None
    assert extract("<html/>") is None


def test_the_golden_pmc_xml():
    assert extract(_GOLDEN_PMC_XML) == (
        "Methods\nOrganoids were derived from resected tissue.\nBase editing used ABE8e.")
