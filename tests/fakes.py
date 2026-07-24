"""In-memory fakes for the two external seams the code reaches through.

FakeAnthropic mirrors exactly what src/services/llm.py consumes off
anthropic.Anthropic: ``client.messages.create(model=, max_tokens=, system=,
messages=, [tools=])`` returning an object with ``.content`` (blocks exposing
``.type`` and ``.text``; tool_use blocks add ``.id/.name/.input/.model_dump()``),
``.stop_reason``, and ``.usage.input_tokens/.output_tokens``. Install via
``monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)``.

FakeSlackClient records the AgentSlackClient calls the simulation makes and
hands back deterministic ts/channel ids, so agent-turn golden-master tests never
touch the network.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from src.agent.slack_client import markdown_to_mrkdwn


@dataclass
class _Usage:
    input_tokens: int = 10
    output_tokens: int = 20


@dataclass
class _TextBlock:
    text: str
    type: str = "text"

    def model_dump(self) -> dict:
        # Real anthropic text blocks expose model_dump(); generate_with_tools()
        # serializes every content block, so a mixed text+tool_use turn needs this.
        return {"type": "text", "text": self.text}


@dataclass
class _ToolUseBlock:
    id: str
    name: str
    input: dict
    type: str = "tool_use"

    def model_dump(self) -> dict:
        return {"type": "tool_use", "id": self.id, "name": self.name, "input": self.input}


@dataclass
class _Message:
    content: list
    stop_reason: str = "end_turn"
    usage: _Usage = field(default_factory=_Usage)


def text_response(text: str, *, stop_reason: str = "end_turn") -> _Message:
    """A plain-text assistant message (the common case)."""
    return _Message(content=[_TextBlock(text=text)], stop_reason=stop_reason)


def tool_use_response(
    tool_name: str, tool_input: dict, *, block_id: str = "toolu_1", text: str = ""
) -> _Message:
    """An assistant turn that requests a tool call (drives generate_with_tools)."""
    blocks: list[Any] = []
    if text:
        blocks.append(_TextBlock(text=text))
    blocks.append(_ToolUseBlock(id=block_id, name=tool_name, input=tool_input))
    return _Message(content=blocks, stop_reason="tool_use")


def empty_response(*, stop_reason: str = "end_turn") -> _Message:
    """An assistant message with no content blocks (Claude occasionally returns this)."""
    return _Message(content=[], stop_reason=stop_reason)


class _Messages:
    def __init__(self, parent: "FakeAnthropic") -> None:
        self._parent = parent

    def create(self, **kwargs) -> _Message:
        self._parent.calls.append(kwargs)
        return self._parent._next(kwargs)


class FakeAnthropic:
    """Scriptable stand-in for anthropic.Anthropic.

    ``responses`` is consumed in order; each item may be a str (wrapped as a
    text message), a pre-built _Message, or a callable(kwargs) -> str | _Message.
    When exhausted, returns ``default_text``. Every create() call is recorded on
    ``.calls`` for assertions.
    """

    def __init__(
        self,
        responses: list[str | _Message | Callable[[dict], Any]] | None = None,
        *,
        default_text: str = "OK",
    ) -> None:
        self._responses = list(responses or [])
        self.default_text = default_text
        self.calls: list[dict] = []
        self.messages = _Messages(self)

    def _next(self, kwargs: dict) -> _Message:
        r: Any = self._responses.pop(0) if self._responses else self.default_text
        if callable(r) and not isinstance(r, _Message):
            r = r(kwargs)
        if isinstance(r, str):
            return text_response(r)
        return r


class FakeSlackClient:
    """Records the AgentSlackClient surface the simulation uses; no network.

    Returns deterministic ts/channel ids so posted-message golden masters stay
    stable. Extend as agent-turn tests exercise more of the interface.
    """

    def __init__(self, agent_id: str = "agent1", bot_token: str = "xoxb-fake") -> None:
        self.agent_id = agent_id
        self.bot_token = bot_token
        self._bot_user_id = f"U_{agent_id}"
        self.posted: list[dict] = []
        self.created_channels: list[dict] = []
        self.invites: list[dict] = []
        self._ts = 1_700_000_000

    def connect(self) -> bool:
        return True

    @property
    def is_connected(self) -> bool:
        return True

    @property
    def bot_user_id(self) -> str | None:
        return self._bot_user_id

    def _next_ts(self) -> str:
        self._ts += 1
        return f"{self._ts}.000000"

    def post_message(self, channel: str, text: str, thread_ts: str | None = None) -> dict:
        ts = self._next_ts()
        # Mirror the real AgentSlackClient, which converts markdown -> Slack mrkdwn
        # before posting (slack_client.py), so posted[..]["text"] reflects what Slack
        # actually receives (e.g. **bold** -> *bold*), not the pre-conversion text.
        rec = {"channel": channel, "text": markdown_to_mrkdwn(text), "thread_ts": thread_ts, "ts": ts}
        self.posted.append(rec)
        return {"ts": ts, "channel": channel}

    def send_dm(self, user_id: str, text: str) -> dict:
        return self.post_message(f"D_{user_id}", text)

    def poll_channel_messages(self, channel_id: str, oldest: str = "0", limit: int = 100) -> list:
        return []

    def get_thread_replies(self, channel_id: str, thread_ts: str, oldest: str = "0") -> list:
        return []

    def create_channel(self, name: str) -> dict:
        ch = {"id": f"C_{name}", "name": name}
        self.created_channels.append(ch)
        return ch

    def create_private_channel(self, name: str) -> dict:
        ch = {"id": f"G_{name}", "name": name, "is_private": True}
        self.created_channels.append(ch)
        return ch

    def invite_to_channel(self, channel_id: str, user_ids: list[str]) -> bool:
        self.invites.append({"channel": channel_id, "users": list(user_ids)})
        return True

    def list_channels(self, include_private: bool = False) -> dict:
        return {}
