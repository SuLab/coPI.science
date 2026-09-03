"""AGENT.md factual corrections (issue #26 DOC-3): stale GitHub URL, wrong
impersonation route, and email listed as out of scope though fully built.
"""

from pathlib import Path

AGENT_MD = (Path(__file__).resolve().parents[2] / "AGENT.md").read_text()


def test_github_url_matches_the_real_remote():
    assert "andrewsu/coPI-python-opus" not in AGENT_MD
    assert "SuLab/coPI.science" in AGENT_MD


def test_impersonation_path_matches_the_real_route():
    assert "/api/admin/impersonate" not in AGENT_MD
    assert "/admin/impersonate" in AGENT_MD


def test_email_is_not_listed_as_out_of_scope():
    out_of_scope = AGENT_MD.split("## What's Out of Scope", 1)[1].split("##", 1)[0]
    assert "email" not in out_of_scope.lower()
