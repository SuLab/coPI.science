"""Recorders for the prompt-freeze golden suite (spec §11).

Everything here RECORDS; nothing here decides. The suite pins what today's code
sends, so a helper that normalised away a real difference would hide exactly the
regression the suite exists to catch. The only normalisations are clock readings
(``latency_ms``, ``wall_ms``, ``completed_at`` and each ``call_stats`` entry's
``latency_ms``) and the per-call random correlation id ``call_id`` — none of them
is bot behaviour, and all of them differ on every run.
"""
from __future__ import annotations

import copy
import threading
from collections.abc import Callable
from typing import Any

from tests.fakes import FakeAnthropic, _Message, text_response

#: The first user block of every specialist consult (src/agent/tools.py
#: `_execute_consult_specialist`). Consults are gathered concurrently, so they are
#: routed and ordered by content rather than by arrival.
_CONSULT_MARKER = "## Question from the hub"
_VOLATILE_KEYS = frozenset({"latency_ms", "wall_ms", "completed_at", "call_id"})


def _first_user_text(request: dict) -> str:
    for message in request.get("messages") or []:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        for block in content or []:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "")
        return ""
    return ""


def is_consult_request(request: dict) -> bool:
    """True for a specialist-consult request, recognised by its first user block."""
    return _first_user_text(request).startswith(_CONSULT_MARKER)


class RecordingAnthropic(FakeAnthropic):
    """FakeAnthropic that deep-copies each request AT CALL TIME and routes consults.

    ``FakeAnthropic.calls`` keeps the kwargs dict itself, and
    ``generate_with_tools`` keeps appending to the ``messages`` list it passed, so
    a stored round-0 request would later show the whole conversation. This class
    snapshots each request the moment ``messages.create`` runs.

    ``script`` answers the turn's own calls in order (``FakeAnthropic``'s rules:
    str, prebuilt message, or callable). ``consult``, when given, answers every
    specialist-consult request: consults run on the API thread pool concurrently
    and must not consume the ordered script in scheduling order.
    """

    def __init__(
        self,
        script: list | None = None,
        *,
        consult: Callable[[dict], Any] | None = None,
        default_text: str = "OK",
    ) -> None:
        super().__init__(list(script or []), default_text=default_text)
        self._consult = consult
        self._lock = threading.Lock()
        self.requests: list[dict] = []

    def _next(self, kwargs: dict) -> _Message:
        snapshot = copy.deepcopy(kwargs)
        with self._lock:
            self.requests.append(snapshot)
        if self._consult is not None and is_consult_request(snapshot):
            reply = self._consult(snapshot)
            return text_response(reply) if isinstance(reply, str) else reply
        with self._lock:
            return super()._next(kwargs)

    def turn_requests(self) -> list[dict]:
        """Every non-consult request, in call order."""
        return [r for r in self.requests if not is_consult_request(r)]

    def consult_requests(self) -> list[dict]:
        """Every consult request, ordered by its question text (not by arrival)."""
        return sorted(
            (r for r in self.requests if is_consult_request(r)), key=_first_user_text
        )


def normalize_call_log(payload: dict) -> dict:
    """An ``on_llm_call`` payload with its clock readings and call id removed."""
    out = copy.deepcopy({k: v for k, v in payload.items() if k not in _VOLATILE_KEYS})
    if "call_stats" in out:
        out["call_stats"] = [
            {k: v for k, v in stat.items() if k != "latency_ms"}
            for stat in out["call_stats"] or []
        ]
    return out


class CallbackRecorder:
    """The callback sequence of every model call, one stream per ``log_meta.phase``.

    ``wrap(real)`` returns a drop-in for ``generate_with_tools`` /
    ``generate_agent_response`` that taps ``on_retry`` and ``on_stop_reason``
    before handing them on unchanged; ``on_llm_call`` is installed with
    ``src.services.llm.set_call_log_callback``. Streams keep concurrent consults
    apart (each consult has its own ``consult_<domain>`` phase).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.streams: dict[str, list] = {}

    def _add(self, stream: str, event: list) -> None:
        with self._lock:
            self.streams.setdefault(stream, []).append(event)

    def _tap(self, stream: str, name: str, callback: Callable) -> Callable:
        def tapped(*args: Any) -> Any:
            self._add(stream, [name, *args])
            return callback(*args)

        return tapped

    def wrap(self, real: Callable) -> Callable:
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            stream = str((kwargs.get("log_meta") or {}).get("phase"))
            for name in ("on_retry", "on_stop_reason"):
                callback = kwargs.get(name)
                if callback is not None:
                    kwargs[name] = self._tap(stream, name, callback)
            return await real(*args, **kwargs)

        return wrapper

    def on_llm_call(self, payload: dict) -> None:
        self._add(str(payload.get("phase")), ["on_llm_call", normalize_call_log(payload)])


def sorted_posts(clients: dict) -> list[dict]:
    """Every FakeSlackClient post, ``ts`` dropped, in a scheduling-independent order."""
    posts = [
        {"agent": agent_id, **{k: v for k, v in post.items() if k != "ts"}}
        for agent_id, client in clients.items()
        for post in client.posted
    ]
    return sorted(
        posts,
        key=lambda p: (p["agent"], p["channel"], p.get("thread_ts") or "", p["text"]),
    )
