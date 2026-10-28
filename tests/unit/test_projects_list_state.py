"""The Projects list's URL state: only valid, non-default values survive, in a fixed order,
and the query string is urlencoded."""

import uuid

from src.services.projects_view import project_list_query, project_list_state


def _state(**overrides):
    values = dict(run_id=None, lab=None, outcome=None, review=None, sort=None, mine=None)
    values.update(overrides)
    return project_list_state(**values)


def test_state_drops_junk_and_defaults():
    assert project_list_state(
        run_id="x", lab="", outcome="bogus", review="unreviewed", sort="finished", mine="yes"
    ) == {}


def test_state_keeps_valid_values_in_order():
    run_id = str(uuid.uuid4())
    state = project_list_state(
        run_id=run_id, lab="lab1", outcome="ready", review="all", sort="lab", mine="1",
        assignment="mine",
    )
    assert list(state.items()) == [
        ("run_id", run_id),
        ("lab", "lab1"),
        ("outcome", "ready"),
        ("review", "all"),
        ("sort", "lab"),
        ("mine", "1"),
        ("assignment", "mine"),
    ]


def test_query_is_urlencoded():
    assert project_list_query({"lab": "a&b\r\nX: y"}) == "?lab=a%26b%0D%0AX%3A+y"
    assert project_list_query({}) == ""


def test_long_lab_dropped():
    assert _state(lab="x" * 51) == {}
    assert _state(lab="x" * 50) == {"lab": "x" * 50}


def test_lab_with_control_character_dropped():
    assert _state(lab="a\r\nb") == {}
    assert _state(lab="a\x7fb") == {}


def test_unknown_assignment_dropped():
    assert _state(assignment="all") == {}
    assert _state(assignment="everyone") == {}
