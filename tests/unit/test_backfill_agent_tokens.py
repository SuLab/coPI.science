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
    # A placeholder in the legacy map is returned as-is, and ``_valid`` rejects it.
    tok = bat.env_token_for("su", {"su": "xoxb-placeholder"}, {})
    assert not bat._valid(tok)
