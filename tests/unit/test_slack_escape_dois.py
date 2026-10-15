"""R2.16 (I6, C9): the Slack escape's DOI parts are C2's find_dois spans, and what it
percent-encodes reads back as the same DOI."""
import ast
from pathlib import Path

import pytest

from src.agent.dois import extract_dois
from src.agent.slack_escape import escape_for_slack

ROOT = Path(__file__).resolve().parents[2]
DOIS = [
    "10.1016/S0092-8674(02)00722-5", "10.1002/1097-0134(20010201)42:2<279::AID-PROT150>3.0.CO;2-U",
    "10.1002/1521-3773(20020802)41:15<2840::AID-ANIE2840>3.0.CO;2-#",
    "10.1007/978-1-4939-7514-3_14", "10.1023/A:1023485632520", "10.1042/0264-6021:3380753",
]


@pytest.mark.parametrize("template", ["see https://doi.org/{}.", "<https://doi.org/{}|paper>",
                                      "(https://doi.org/{})"])
@pytest.mark.parametrize("doi", DOIS)
def test_escaped_doi_links_read_back_as_the_same_doi(doi, template):
    text = template.format(doi)
    once = escape_for_slack(text)
    assert extract_dois(once) == {doi.lower()}
    assert escape_for_slack(once) == once


def test_a_doi_query_is_outside_the_encoded_part():
    assert escape_for_slack("see https://doi.org/10.1002/x<1>?utm=1 now") == (
        "see https://doi.org/10.1002/x%3C1%3E?utm=1 now")


def test_a_scheme_less_doi_url_is_entity_escaped():
    assert escape_for_slack("doi.org/10.1002/x<1>") == "doi.org/10.1002/x&lt;1&gt;"


def test_an_entity_in_a_doi_url_is_percent_encoded():
    assert escape_for_slack("https://doi.org/10.1002/x&lt;1&gt; ok") == (
        "https://doi.org/10.1002/x%3C1%3E ok")


def test_slack_escape_has_no_local_doi_scanner():
    tree = ast.parse((ROOT / "src/agent/slack_escape.py").read_text(encoding="utf-8"))
    imports = {(node.module, alias.name) for node in ast.walk(tree)
               if isinstance(node, ast.ImportFrom) for alias in node.names}
    assert ("src.agent.dois", "find_dois") in imports
    constants = [node.value for node in ast.walk(tree)
                 if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    assert not [c for c in constants if "10\\." in c]
