"""base.html emits posthog.init exactly when POSTHOG_API_KEY is set."""

import pytest

from src.config import get_settings

pytestmark = pytest.mark.integration


async def test_posthog_snippet_renders_only_with_a_key(client, monkeypatch):
    real = get_settings()
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: real.model_copy(update={"posthog_api_key": "phc_test_key"}),
    )
    page = await client.get("/login")
    assert page.status_code == 200
    assert "posthog.init('phc_test_key'" in page.text

    # Control: no key, no snippet — so the positive leg is not satisfied by a
    # template that always renders it.
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: real.model_copy(update={"posthog_api_key": ""}),
    )
    page = await client.get("/login")
    assert page.status_code == 200
    assert "posthog.init(" not in page.text
