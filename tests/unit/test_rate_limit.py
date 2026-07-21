"""Unit tests for the in-process rate limiter used on public write endpoints.

Backs SEC-7 (proposal-vote throttle) and SEC-17 (waitlist throttle): the
limiter must cut off a burst once the window fills, recover after the window
elapses, key independently per caller, and read the real client IP from the
nginx-set forwarding headers.
"""

from types import SimpleNamespace

from src.services.rate_limit import SlidingWindowRateLimiter, client_ip


def _req(headers=None, client_host="10.0.0.1"):
    return SimpleNamespace(
        headers=headers or {},
        client=SimpleNamespace(host=client_host),
    )


def test_allows_up_to_limit_then_blocks():
    rl = SlidingWindowRateLimiter(max_events=3, window_seconds=60)
    assert [rl.allow("k") for _ in range(3)] == [True, True, True]
    assert rl.allow("k") is False


def test_window_recovers_after_expiry(monkeypatch):
    import src.services.rate_limit as mod

    now = [1000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: now[0])
    rl = SlidingWindowRateLimiter(max_events=2, window_seconds=10)

    assert rl.allow("k") is True
    assert rl.allow("k") is True
    assert rl.allow("k") is False  # window full

    now[0] += 11  # slide past the window
    assert rl.allow("k") is True


def test_keys_are_independent():
    rl = SlidingWindowRateLimiter(max_events=1, window_seconds=60)
    assert rl.allow("a") is True
    assert rl.allow("b") is True  # different key, own bucket
    assert rl.allow("a") is False


def test_sweep_drops_idle_keys(monkeypatch):
    import src.services.rate_limit as mod

    now = [0.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: now[0])
    rl = SlidingWindowRateLimiter(max_events=5, window_seconds=10)

    for i in range(50):
        rl.allow(f"key-{i}")
    assert len(rl._events) == 50

    now[0] += 100  # everything expires
    rl.allow("fresh")  # triggers a sweep
    assert set(rl._events) == {"fresh"}


def test_client_ip_prefers_x_real_ip():
    req = _req(headers={"x-real-ip": "203.0.113.7", "x-forwarded-for": "1.2.3.4"})
    assert client_ip(req) == "203.0.113.7"


def test_client_ip_falls_back_to_forwarded_for():
    req = _req(headers={"x-forwarded-for": "203.0.113.9, 10.0.0.1"})
    assert client_ip(req) == "203.0.113.9"


def test_client_ip_falls_back_to_socket_peer():
    assert client_ip(_req(client_host="198.51.100.2")) == "198.51.100.2"
