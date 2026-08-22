"""In-memory fakes for the two external seams the code reaches through.

FakeAnthropic mirrors exactly what src/services/llm.py consumes off
anthropic.Anthropic: ``client.messages.create(model=, max_tokens=, system=,
messages=, [tools=])`` returning an object with ``.content`` (blocks exposing
``.type`` and ``.text``; tool_use blocks add ``.id/.name/.input/.model_dump()``),
``.stop_reason``, ``.usage.input_tokens/.output_tokens`` and the optional
``.usage.output_tokens_details.thinking_tokens``. Install via
``monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)``.

It also mirrors the one thing the real client REFUSES: a non-streaming request
whose ``max_tokens`` implies more than 10 minutes of generation. See
``_MAX_NONSTREAMING_MAX_TOKENS``.

FakeSlackClient records the AgentSlackClient calls the simulation makes and
hands back deterministic ts/channel ids, so agent-turn golden-master tests never
touch the network.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from src.agent.slack_client import markdown_to_mrkdwn

# The SDK's own ceiling, RE-DERIVED here rather than imported from
# src.services.llm, on purpose. anthropic's
# ``BaseClient._calculate_nonstreaming_timeout`` computes
# ``expected_time = maximum_time * max_tokens / 128_000`` with
# ``maximum_time = 60 * 60`` and raises ValueError once that exceeds its
# ``default_time = 60 * 10`` — so the last accepted value is the floor of
# ``600 * 128_000 / 3600``. Writing the arithmetic out means this fake agrees
# with the SDK, not with whatever constant src happens to hold; if the two ever
# disagree, test_llm_nonstreaming_ceiling.py fails on the comparison instead of
# both being wrong together.
#
# The whole suite drives this seam, so without this check nothing in CI can
# observe the ceiling at all: the 16000-token thread_reply ceiling shipped with a
# 2x truncation retry that asks for 32000, which the real client rejects and this
# fake used to accept happily.
_MAX_NONSTREAMING_MAX_TOKENS = int(600 * 128_000 / 3600)  # 21_333


@dataclass
class _OutputTokensDetails:
    """The SDK's ``usage.output_tokens_details`` (anthropic >= 0.120).

    Only field src/services/llm.py reads. Exists as its own class rather than a
    plain int on _Usage because the real thing is a nested Optional model, and
    the production code reaches it through two defensive ``getattr``s — a flat
    attribute would let a bug in that traversal pass.
    """

    thinking_tokens: int = 0


@dataclass
class _Usage:
    input_tokens: int = 10
    output_tokens: int = 20
    # None by DEFAULT, matching both the SDK (``Optional``, absent when the API
    # did not decompose the reply) and every pre-existing user of this fake, for
    # which the field simply is not the subject. llm._thinking_tokens must
    # therefore return None here, not raise and not invent a 0 — a test that
    # cares passes _OutputTokensDetails(...) explicitly.
    output_tokens_details: "_OutputTokensDetails | None" = None


@dataclass
class _TextBlock:
    text: str
    type: str = "text"

    def model_dump(self) -> dict:
        # Real anthropic text blocks expose model_dump(); generate_with_tools()
        # serializes every content block, so a mixed text+tool_use turn needs this.
        return {"type": "text", "text": self.text}


@dataclass
class _ThinkingBlock:
    """A thinking block, which leads the content list when thinking is enabled.

    Deliberately has NO ``.text`` attribute — real thinking blocks carry
    ``.thinking`` instead. That is the whole point: code that reaches for
    ``content[0].text`` raises AttributeError against a thinking-enabled reply,
    which is exactly the trap Opus 5 / Sonnet 5 set by running adaptive thinking
    when ``thinking`` is omitted. A fake that exposed ``.text`` here would let
    that bug pass.
    """

    thinking: str = ""
    type: str = "thinking"

    def model_dump(self) -> dict:
        return {"type": "thinking", "thinking": self.thinking}


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


def text_response(
    text: str, *, stop_reason: str = "end_turn", usage: _Usage | None = None
) -> _Message:
    """A plain-text assistant message (the common case).

    ``usage`` is optional and defaults to _Message's own _Usage, so every
    pre-existing caller is unchanged; pass it when the test is about the numbers
    a call reports (per-call token accounting, the thinking/text split).
    """
    msg = _Message(content=[_TextBlock(text=text)], stop_reason=stop_reason)
    if usage is not None:
        msg.usage = usage
    return msg


def multi_text_response(
    *texts: str, stop_reason: str = "end_turn", usage: "_Usage | None" = None
) -> "_Message":
    """A reply carrying N text blocks (N may be 0).

    Several text blocks is what a thinking-enabled turn can interleave; zero
    (same shape as ``empty_response``) models a refusal or a thinking-only
    turn, which is the shape that used to produce a silent empty reply.
    """
    msg = _Message(
        content=[_TextBlock(text=t) for t in texts], stop_reason=stop_reason
    )
    if usage is not None:
        msg.usage = usage
    return msg


def thinking_then_text_response(
    text: str, *, thinking: str = "reasoning...", stop_reason: str = "end_turn"
) -> _Message:
    """What a thinking-enabled reply actually looks like: thinking block FIRST.

    This is the shape Opus 5 and Sonnet 5 return whenever ``thinking`` is
    omitted, because both run adaptive thinking by default. Use it to prove that
    text extraction filters by block type rather than indexing ``content[0]``.
    """
    return _Message(
        content=[_ThinkingBlock(thinking=thinking), _TextBlock(text=text)],
        stop_reason=stop_reason,
    )


def tool_use_response(
    tool_name: str,
    tool_input: dict,
    *,
    block_id: str = "toolu_1",
    text: str = "",
    stop_reason: str = "tool_use",
    usage: _Usage | None = None,
) -> _Message:
    """An assistant turn that requests a tool call (drives generate_with_tools).

    ``stop_reason`` is overridable because a tool-use round CAN truncate: the API
    returns the tool_use blocks it managed to emit with ``stop_reason
    ='max_tokens'``. That combination had no representation here, which is part
    of why the truncating-tool-round case went untested for so long.
    """
    blocks: list[Any] = []
    if text:
        blocks.append(_TextBlock(text=text))
    blocks.append(_ToolUseBlock(id=block_id, name=tool_name, input=tool_input))
    msg = _Message(content=blocks, stop_reason=stop_reason)
    if usage is not None:
        msg.usage = usage
    return msg


def empty_response(*, stop_reason: str = "end_turn") -> _Message:
    """An assistant message with no content blocks (Claude occasionally returns this)."""
    return _Message(content=[], stop_reason=stop_reason)


class _Messages:
    def __init__(self, parent: "FakeAnthropic") -> None:
        self._parent = parent

    def create(self, **kwargs) -> _Message:
        # BEFORE recording the call, exactly like the SDK: it validates the
        # timeout it would need and raises without sending anything, so a
        # rejected request must not show up in ``.calls`` either — that is how a
        # test proves the guard fired instead of the request going out.
        max_tokens = kwargs.get("max_tokens")
        if max_tokens is not None and max_tokens > _MAX_NONSTREAMING_MAX_TOKENS:
            raise ValueError(
                "Streaming is required for operations that may take longer than "
                "10 minutes. See "
                "https://github.com/anthropics/anthropic-sdk-python#long-requests "
                f"for more details (max_tokens={max_tokens})"
            )
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

    def __init__(self, agent_id: str = "agent1", bot_token: str = "xoxb-fake",
                 existing_channels: dict | None = None) -> None:
        self.agent_id = agent_id
        self.bot_token = bot_token
        self._bot_user_id = f"U_{agent_id}"
        self.posted: list[dict] = []
        # channel (name or id, as passed by the caller) -> list of texts
        # posted to it, in order. A second, channel-keyed view onto the same
        # calls `self.posted` records, for tests that only care "did exactly
        # one message land in #assessments-summary" without wading through
        # every post the agent made across every channel.
        self.posted_messages: dict[str, list[str]] = {}
        self.created_channels: list[dict] = []
        self.invites: list[dict] = []
        self.joined_channels: set[str] = set()
        self._existing_channels: dict = existing_channels or {}  # name -> id for list_channels
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
        self.posted_messages.setdefault(channel, []).append(rec["text"])
        return {"ts": ts, "channel": channel}

    def send_dm(self, user_id: str, text: str) -> dict:
        return self.post_message(f"D_{user_id}", text)

    def poll_channel_messages(self, channel_id: str, oldest: str = "0", limit: int = 100) -> list:
        return []

    def get_thread_replies(self, channel_id: str, thread_ts: str, oldest: str = "0") -> list:
        return []

    # Async twins — the engine now awaits these, off the loop thread, exactly
    # like the real AgentSlackClient (src/agent/slack_client.py). Kept here so
    # this fake still satisfies the surface the simulation calls.
    async def apost_message(self, *args, **kwargs) -> dict:
        return await asyncio.to_thread(self.post_message, *args, **kwargs)

    async def apoll_channel_messages(self, *args, **kwargs) -> list:
        return await asyncio.to_thread(self.poll_channel_messages, *args, **kwargs)

    def is_bot_user(self, user_id: str) -> bool:
        return False

    async def ais_bot_user(self, user_id: str) -> bool:
        return self.is_bot_user(user_id)

    def join_channel(self, channel_id: str) -> None:
        # For test tracking, extract the channel name from the ID.
        # FakeSlackClient creates IDs as "C_name" or "G_name", and real IDs pass through.
        ch_name = channel_id
        if channel_id.startswith(("C_", "G_")):
            ch_name = channel_id[2:]  # Strip the "C_" or "G_" prefix
        self.joined_channels.add(ch_name)

    async def ajoin_channel(self, channel_id: str) -> None:
        return self.join_channel(channel_id)

    async def aconnect(self) -> bool:
        return self.connect()

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
        return dict(self._existing_channels)

    def _resolve_channel_id(self, channel: str) -> str:
        """Name -> id, mirroring AgentSlackClient (ids pass through unchanged)."""
        if channel.startswith(("C", "G")):
            return channel
        return f"C_{channel}"

    def get_permalink(self, channel_id: str, message_ts: str) -> str | None:
        return f"https://fake.slack.com/archives/{channel_id}/p{message_ts.replace('.', '')}"

    async def aget_permalink(self, *args, **kwargs) -> str | None:
        return self.get_permalink(*args, **kwargs)


class RecordingSlackClient:
    """Records outbound Slack Web API calls; scripts responses and errors.

    Deliberately distinct from ``FakeSlackClient``. That one implements the
    *Transport* protocol and returns canned values, which means a mirror that never
    called Slack at all would satisfy it just as well as one that did. This class
    stands in for the ``slack_sdk.WebClient`` **inside** ``AgentSlackClient``, and
    ``.calls`` is the evidence that the call was made and with what arguments.

    ``responses`` maps a WebClient method name to the dict it should return.
    ``errors`` maps a method name to a list of exceptions, popped one per call, so a
    retry path can be scripted as "fail, then succeed".
    """

    def __init__(self, responses=None, errors=None):
        self.calls: list[tuple[str, dict]] = []
        self._responses = dict(responses or {})
        self._errors = {k: list(v) for k, v in (errors or {}).items()}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def _call(**kwargs):
            self.calls.append((name, kwargs))
            queue = self._errors.get(name)
            if queue:
                raise queue.pop(0)
            return _SlackResponse(self._responses.get(name, {"ok": True}))

        return _call

    def calls_to(self, method: str) -> list[dict]:
        return [kw for m, kw in self.calls if m == method]


class _SlackResponse:
    """The parts of slack_sdk's SlackResponse that AgentSlackClient touches."""

    def __init__(self, data: dict):
        self.data = data
        self.headers: dict[str, str] = {}
        self.status_code = 200

    def get(self, key, default=None):
        return self.data.get(key, default)

    def __getitem__(self, key):
        return self.data[key]

    def __contains__(self, key):
        return key in self.data


def slack_error(code: str, *, retry_after: int | None = None):
    """A SlackApiError shaped the way ``_call_with_retry`` inspects it.

    It reads ``exc.response.get("error")`` and ``exc.response.headers.get("Retry-After")``,
    so both have to be present on the response object or the retry branch is never
    reached and the test would pass for the wrong reason.
    """
    from slack_sdk.errors import SlackApiError

    resp = _SlackResponse({"ok": False, "error": code})
    if retry_after is not None:
        resp.headers = {"Retry-After": str(retry_after)}
    resp.status_code = 429 if code == "ratelimited" else 400
    return SlackApiError(code, resp)
