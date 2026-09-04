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
    # Anchored to the exact route form (issue #26 Minor 2): a bare substring
    # check is vacuous because "/api/admin/impersonate" also contains
    # "/admin/impersonate".
    assert "POST /admin/impersonate" in AGENT_MD


def test_email_is_not_listed_as_out_of_scope():
    out_of_scope = AGENT_MD.split("## What's Out of Scope", 1)[1].split("##", 1)[0]
    assert "email" not in out_of_scope.lower()


def test_daily_digest_is_not_listed_as_out_of_scope_but_is_documented_as_built():
    """issue #26 I3: the periodic status digest (check_and_send_status_overviews,
    wired daily in src/worker/main.py) was still listed under "What's Out of
    Scope" even after the adjacent built-features paragraph was added —
    contradicting itself across two sections."""
    out_of_scope = AGENT_MD.split("## What's Out of Scope", 1)[1].split("##", 1)[0]
    assert "digest" not in out_of_scope.lower()

    built = AGENT_MD.split("## What Email Actually Does (in scope, built)", 1)[1].split(
        "##", 1
    )[0]
    assert "digest" in built.lower()
