import inspect

from src.services import assessment_chat


def test_request_is_built_from_turns_read_under_the_lock():
    src = inspect.getsource(assessment_chat.prepare_turn)
    lock_at = src.index("await _take_spend_lock(db)")
    build_at = src.index("build_request(")
    assert build_at > lock_at, "the request must be built from the turns re-read under the lock (LC-07)"
    assert src.count("build_request(") == 1


def test_clear_takes_the_lock():
    src = inspect.getsource(assessment_chat.clear_history)
    assert src.index("await _take_spend_lock(db)") < src.index("in_flight = ")
