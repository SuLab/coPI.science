"""Pure-logic tests for ``env_token_for`` in scripts/backfill_agent_tokens.py.

``provision_slack_bots.py`` writes each new bot token to ``.env`` as
``SLACK_BOT_TOKEN_<AGENT_ID upper>``, a key ``Settings`` does not declare, so the
importer must read it from the environment or every newly provisioned agent is
skipped. The script is not an importable package, so it is loaded by path.
"""

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backfill_agent_tokens.py"


def _load():
    spec = importlib.util.spec_from_file_location("copi_backfill_agent_tokens", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bat = _load()


def test_a_new_agents_env_key_is_imported():
    environ = {"SLACK_BOT_TOKEN_NEWPI": "xoxb-real"}
    assert bat.env_token_for("newpi", {}, environ) == "xoxb-real"


def test_the_legacy_map_is_still_a_fallback():
    legacy = {"su": "xoxb-legacy"}
    assert bat.env_token_for("su", legacy, {}) == "xoxb-legacy"
    # The per-agent environment key wins when both are valid.
    environ = {"SLACK_BOT_TOKEN_SU": "xoxb-env"}
    assert bat.env_token_for("su", legacy, environ) == "xoxb-env"


def test_a_placeholder_is_never_imported():
    environ = {"SLACK_BOT_TOKEN_NEWPI": "xoxb-placeholder-newpi"}
    # A placeholder env key falls through to the legacy map, not into the result.
    assert bat.env_token_for("newpi", {}, environ) == ""
    assert bat.env_token_for("newpi", {"newpi": "xoxb-legacy"}, environ) == "xoxb-legacy"
    # A placeholder in the legacy map is not returned either.
    assert bat.env_token_for("su", {"su": "xoxb-placeholder"}, {}) == ""


def test_a_non_bot_per_agent_value_falls_back_to_a_valid_legacy_token():
    legacy = {"su": "xoxb-legacy"}
    for bad in ("xoxp-a-user-token", "xoxe.xoxp-config", "REPLACE_ME", "   "):
        environ = {"SLACK_BOT_TOKEN_SU": bad}
        assert bat.env_token_for("su", legacy, environ) == "xoxb-legacy"
        assert bat.env_token_for("su", {}, environ) == ""
    # A non-bot legacy value is not returned either.
    assert bat.env_token_for("su", {"su": "xoxp-user"}, {}) == ""


def test_env_values_are_stripped():
    environ = {"SLACK_BOT_TOKEN_SU": "  xoxb-env\n"}
    assert bat.env_token_for("su", {}, environ) == "xoxb-env"


def test_an_invalid_db_value_is_replaced():
    for bad in ("xoxp-a-user-token", "REPLACE_ME", "xoxb-placeholder-su"):
        assert bat.classify(bad, "xoxb-env") == "replacing_invalid"
        # Still reported as needing a token when there is nothing valid to write.
        assert bat.classify(bad, "") == "no_env"


def test_classify_fills_blank_and_keeps_valid():
    assert bat.classify(None, "xoxb-env") == "fill"
    assert bat.classify("  ", "xoxb-env") == "fill"
    assert bat.classify("xoxb-current", "xoxb-env") == "present"
    assert bat.classify(None, "") == "no_env"
