"""``classify_reply`` must not pin the event loop for the length of its request.

Run 8b64a0e0, finding C6 (new, in neither audit document):
``src/services/email_inbound.py:342`` called ``client.messages.create``
**synchronously inside an ``async def``** and with no timeout of its own. Two
consequences, both measured on the same run that surfaced them:

- the synchronous client blocks the whole loop thread, so one classification
  froze the inbound-email poller, the Slack pollers, the DB persist flush and
  the asyncio SIGTERM handler along with it;
- the two API stalls that run produced fingerprinted at 600.09 / 600.10 s
  (``read=600``, the SDK default), and the inbound poller's own interval put the
  worst case at **1,801.5 s** of a wedged process.

Both are fixed by routing this call through ``llm._acreate`` and the shared
client: ``asyncio.to_thread`` gives the loop back, and the client carries the
300 s read timeout (``llm.CLIENT_READ_TIMEOUT_SECONDS``).
"""

import asyncio

import pytest

from src.services.email_inbound import classify_reply
from tests.fakes import FakeAnthropic, text_response

pytestmark = pytest.mark.asyncio

_VERDICT = '{"category": "review", "rating": 3, "comment": "looks good", "instruction": ""}'


async def test_classify_reply_does_not_block_the_event_loop(monkeypatch):
    """A 0.2 s-blocking ``create`` against a 0.01 s ticker.

    The ticker is the whole test: if the request runs on the loop thread it
    cannot tick at all while the call is in flight, so the count after the await
    is the difference between "off the loop" and "everything else stopped".
    """
    fake = FakeAnthropic([text_response(_VERDICT)], latency=0.2)
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    ticks = 0

    async def _ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(_ticker())
    try:
        result = await classify_reply("please make it shorter", "a proposal summary")
    finally:
        ticker.cancel()

    assert result["category"] == "review"
    assert result["rating"] == 3
    assert ticks >= 5, (
        f"the loop ticked {ticks} times during a 0.2 s request — the call is "
        "still running ON the loop thread"
    )


async def test_classify_reply_uses_the_shared_client_and_its_timeout(monkeypatch):
    """Not just off-thread — off-thread through the SAME pooled client.

    A private client here would mean its own TCP+TLS handshake per
    classification AND the SDK's 600 s default read timeout back again, which is
    the half of C6 that actually caused the 1,801.5 s worst case.
    """
    fake = FakeAnthropic([text_response(_VERDICT)])
    calls: list[str] = []

    def _client():
        calls.append("shared")
        return fake

    monkeypatch.setattr("src.services.llm.get_anthropic_client", _client)

    await classify_reply("body", "summary")

    assert calls == ["shared"], "classify_reply must go through get_anthropic_client"
    # `thinking` defaulted to disabled by `_acreate`. This is not cosmetic: on
    # Opus 5 / Sonnet 5 an OMITTED `thinking` runs ADAPTIVE thinking, which puts
    # a thinking block at content[0] — and this function used to read
    # `message.content[0].text`, so every classification would have raised
    # AttributeError into the broad handler and returned "unparseable".
    assert fake.calls[0]["thinking"] == {"type": "disabled"}


async def test_a_classification_with_no_text_block_is_unparseable_not_fatal(
    monkeypatch
):
    """The broad handler's contract is unchanged: a reply this cannot read
    degrades to ``unparseable`` rather than raising into the poller."""
    fake = FakeAnthropic([text_response("", stop_reason="refusal")])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    result = await classify_reply("body", "summary")

    assert result == {
        "category": "unparseable",
        "rating": None,
        "comment": "",
        "instruction": "",
    }
