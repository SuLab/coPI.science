"""The golden suite's own recorder (tests/characterization/freeze/capture.py).

The suite is only as trustworthy as these helpers: a recorder that stored a
request by reference, or a normaliser that dropped a real field, would make a
changed prompt look frozen.
"""
from src.agent.channels import ASSESSMENTS_SUMMARY_CHANNEL
from tests.characterization.freeze.capture import (
    CallbackRecorder,
    RecordingAnthropic,
    is_consult_request,
    normalize_call_log,
    sorted_posts,
)
from tests.fakes import FakeAnthropic, FakeSlackClient


def test_with_options_returns_the_same_recording_fake():
    fake = FakeAnthropic(["hi"])
    assert fake.with_options(max_retries=0) is fake
    assert fake.options_calls == [{"max_retries": 0}]


def test_requests_are_copied_at_call_time():
    fake = RecordingAnthropic(["one", "two"])
    messages = [{"role": "user", "content": "q"}]
    fake.messages.create(model="m", max_tokens=5, messages=messages)
    messages.append({"role": "assistant", "content": "later"})
    fake.messages.create(model="m", max_tokens=5, messages=messages)
    assert fake.requests[0]["messages"] == [{"role": "user", "content": "q"}]
    assert len(fake.requests[1]["messages"]) == 2


def test_consult_requests_are_routed_apart_and_sorted():
    fake = RecordingAnthropic(["turn reply"], consult=lambda request: "consult reply")
    consult_b = [{"role": "user", "content": "## Question from the hub\n\nB?"}]
    consult_a = [{"role": "user", "content": "## Question from the hub\n\nA?"}]
    assert fake.messages.create(messages=consult_b).content[0].text == "consult reply"
    turn = fake.messages.create(messages=[{"role": "user", "content": "turn"}])
    assert turn.content[0].text == "turn reply"
    fake.messages.create(messages=consult_a)
    assert [r["messages"] for r in fake.consult_requests()] == [consult_a, consult_b]
    assert [r["messages"] for r in fake.turn_requests()] == [
        [{"role": "user", "content": "turn"}]
    ]
    assert is_consult_request({"messages": consult_a})
    assert not is_consult_request({"messages": [{"role": "user", "content": "turn"}]})


def test_normalize_call_log_drops_only_clock_readings_and_correlation_ids():
    payload = {
        "phase": "memory",
        "latency_ms": 1.0,
        "wall_ms": 2.0,
        "completed_at": object(),
        "call_id": "0123abcd",
        "call_stats": [{"seq": 1, "latency_ms": 3.0, "stop_reason": "end_turn"}],
        "response_text": "x",
    }
    assert normalize_call_log(payload) == {
        "phase": "memory",
        "call_stats": [{"seq": 1, "stop_reason": "end_turn"}],
        "response_text": "x",
    }


async def test_callback_recorder_taps_without_changing_what_the_callback_sees():
    recorder = CallbackRecorder()
    seen: list = []

    async def real(**kwargs):
        kwargs["on_stop_reason"]("end_turn")
        kwargs["on_retry"]()
        return "done"

    wrapped = recorder.wrap(real)
    out = await wrapped(
        log_meta={"phase": "memory"},
        on_stop_reason=seen.append,
        on_retry=lambda: seen.append("retry"),
    )
    assert out == "done"
    assert seen == ["end_turn", "retry"]
    assert recorder.streams == {"memory": [["on_stop_reason", "end_turn"], ["on_retry"]]}


def test_sorted_posts_drops_ts_and_orders_deterministically():
    a = FakeSlackClient(agent_id="wang")
    b = FakeSlackClient(agent_id="blackbird")
    b.post_message(ASSESSMENTS_SUMMARY_CHANNEL, "second")
    a.post_message("general", "first", thread_ts="1.0")
    assert sorted_posts({"wang": a, "blackbird": b}) == [
        {"agent": "blackbird", "channel": ASSESSMENTS_SUMMARY_CHANNEL,
         "text": "second", "thread_ts": None},
        {"agent": "wang", "channel": "general", "text": "first", "thread_ts": "1.0"},
    ]
