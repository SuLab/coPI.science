"""classify_reply must pin thinking off on its direct messages.create call.

Sonnet 5 thinks by default and max_tokens caps thinking + text together, so
without an explicit thinking={"type": "disabled"} the first content block is
a thinking block: the .text read raises and EVERY inbound reply classifies as
"unparseable". Pinned on prod as hotfix 0e2ed84; this test keeps a refactor of
classify_reply from silently dropping the pin again.
"""

import json

from src.services.email_inbound import classify_reply


class _FakeMessages:
    def __init__(self):
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)

        class _Block:
            text = json.dumps(
                {"category": "review", "rating": 3, "comment": "", "instruction": ""}
            )

        class _Msg:
            content = [_Block()]

        return _Msg()


class _FakeClient:
    def __init__(self):
        self.messages = _FakeMessages()


async def test_classify_reply_pins_thinking_disabled(monkeypatch):
    import src.services.llm as llm

    fake = _FakeClient()
    monkeypatch.setattr(llm, "get_anthropic_client", lambda: fake)

    result = await classify_reply("3 — looks great", "proposal summary")

    # The call succeeded through the fake, so the classification came through…
    assert result["category"] == "review"
    # …and the call itself must carry the pin.
    assert len(fake.messages.calls) == 1
    assert fake.messages.calls[0].get("thinking") == {"type": "disabled"}
