"""FakeAnthropic / FakeSlackClient behave like the real seams they replace."""

from tests.fakes import (
    FakeAnthropic,
    FakeSlackClient,
    _OutputTokensDetails,
    _Usage,
    text_response,
    tool_use_response,
)


def test_fake_anthropic_scripted_text_in_order():
    fake = FakeAnthropic(["first", "second"])
    m1 = fake.messages.create(model="m", max_tokens=10, system="s", messages=[])
    m2 = fake.messages.create(model="m", max_tokens=10, system="s", messages=[])
    assert m1.content[0].text == "first"
    assert m1.content[0].type == "text"
    assert m2.content[0].text == "second"


def test_fake_anthropic_falls_back_to_default():
    fake = FakeAnthropic(default_text="fallback")
    m = fake.messages.create(model="m", max_tokens=10, system="s", messages=[])
    assert m.content[0].text == "fallback"
    assert m.usage.input_tokens >= 0
    assert m.stop_reason == "end_turn"


def test_fake_anthropic_records_calls():
    fake = FakeAnthropic()
    fake.messages.create(model="claude-x", max_tokens=99, system="sys", messages=[{"role": "user", "content": "hi"}])
    assert fake.calls[0]["model"] == "claude-x"
    assert fake.calls[0]["max_tokens"] == 99


def test_fake_anthropic_callable_response_sees_kwargs():
    fake = FakeAnthropic([lambda kw: text_response(kw["system"])])
    m = fake.messages.create(model="m", max_tokens=10, system="echo-me", messages=[])
    assert m.content[0].text == "echo-me"


def test_tool_use_response_block_shape():
    m = tool_use_response("search", {"q": "x"}, block_id="toolu_9")
    block = m.content[0]
    assert block.type == "tool_use"
    assert block.name == "search"
    assert block.input == {"q": "x"}
    assert block.model_dump() == {"type": "tool_use", "id": "toolu_9", "name": "search", "input": {"q": "x"}}
    assert m.stop_reason == "tool_use"


def test_usage_reports_no_thinking_split_unless_a_test_asks_for_one():
    """Mirrors the SDK, where `Usage.output_tokens_details` is Optional and absent
    on a reply the API did not decompose. Defaulting it to a present object would
    make llm._thinking_tokens' two defensive getattrs untestable."""
    assert _Usage().output_tokens_details is None
    assert text_response("x").usage.output_tokens_details is None


def test_usage_can_carry_a_thinking_split_when_the_test_is_about_it():
    usage = _Usage(output_tokens=4000, output_tokens_details=_OutputTokensDetails(2400))
    assert text_response("x", usage=usage).usage.output_tokens_details.thinking_tokens == 2400


def test_a_tool_use_round_can_be_scripted_as_truncated():
    """The real API returns the tool_use blocks it managed to emit with
    stop_reason='max_tokens'; that combination had no representation here."""
    m = tool_use_response("search", {}, stop_reason="max_tokens")
    assert m.stop_reason == "max_tokens"
    assert m.content[0].type == "tool_use"


def test_fake_slack_records_posts_and_returns_ts():
    slack = FakeSlackClient(agent_id="a1")
    r1 = slack.post_message("C123", "hello")
    r2 = slack.post_message("C123", "world", thread_ts="1.0")
    assert r1["ts"] != r2["ts"]
    assert slack.posted[0]["text"] == "hello"
    assert slack.posted[1]["thread_ts"] == "1.0"
    assert slack.bot_user_id == "U_a1"


def test_fake_slack_channel_and_invite_recording():
    slack = FakeSlackClient()
    ch = slack.create_private_channel("priv-a-b")
    assert ch["is_private"] is True
    slack.invite_to_channel(ch["id"], ["U1", "U2"])
    assert slack.invites[0]["users"] == ["U1", "U2"]
