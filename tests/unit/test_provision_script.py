import ast
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


def test_token_format_is_dependency_free():
    tree = ast.parse((REPO / "src/services/token_format.py").read_text())
    imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert all(not (getattr(n, "module", "") or "").startswith("src") for n in imports)


def test_slack_tokens_reexports_the_same_function():
    from src.services import slack_tokens, token_format

    assert slack_tokens.is_valid_token is token_format.is_valid_token


def test_tokenized_uses_is_valid_token():
    import scripts.provision_slack_bots as p

    env = {"SLACK_BOT_TOKEN_A": "xoxb-real", "SLACK_BOT_TOKEN_B": "xoxp-user-token",
           "SLACK_BOT_TOKEN_C": "xoxb-placeholder", "SLACK_BOT_TOKEN_D": "  "}
    assert p.tokenized_agents(env) == {"a"}


def test_create_mode_skips_agents_already_in_state(tmp_path, monkeypatch):
    import scripts.provision_slack_bots as p

    state = tmp_path / ".provision_state.json"
    state.write_text(json.dumps([{"agent_id": "a", "bot_name": "ABot", "pi_name": "A", "client_id": "c",
                                  "client_secret": "s", "app_id": "X", "oauth_url": "u"}]))
    monkeypatch.setattr(p, "STATE_FILE", state)
    missing = [{"id": "a", "name": "ABot", "pi": "A"}, {"id": "b", "name": "BBot", "pi": "B"}]
    to_create, already = p.split_against_state(missing, p.load_existing_state())
    assert [lab["id"] for lab in to_create] == ["b"] and [a["agent_id"] for a in already] == ["a"]


def test_the_script_never_reads_the_refresh_token():
    text = (REPO / "scripts/provision_slack_bots.py").read_text()
    assert "SLACK_CONFIG_REFRESH_TOKEN" not in text.split('"""', 2)[2]  # outside the module docstring
    assert "rotate_config_token" not in text
