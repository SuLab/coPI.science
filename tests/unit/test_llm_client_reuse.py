from src.services import llm


def test_get_anthropic_client_reuses_one_instance_per_key(monkeypatch):
    llm._client_for_key.cache_clear()

    class _S:
        anthropic_api_key = "key-a"

    monkeypatch.setattr(llm, "get_settings", lambda: _S())
    c1 = llm.get_anthropic_client()
    c2 = llm.get_anthropic_client()
    assert c1 is c2, "each call built a fresh client (fresh connection pool)"

    _S.anthropic_api_key = "key-b"
    c3 = llm.get_anthropic_client()
    assert c3 is not c1, "a different API key must get its own client"
