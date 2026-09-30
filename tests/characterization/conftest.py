import pytest


@pytest.fixture(autouse=True)
def _build_through_prompt_snapshot(request):
    """§11: from Phase 2 the golden suite builds agents through PromptSnapshot.
    Only the frozen request suite is affected; test_agent_turn_gm is not."""
    if request.node.fspath.basename != "test_prompt_freeze_gm.py":
        yield
        return
    from src.agent import prompt_snapshot as ps

    ps.install(ps.PromptSnapshot.load())
    try:
        yield
    finally:
        ps.install(None)
