"""A stale .env naming retired settings still loads."""

from src.config import Settings

RETIRED = {
    "NOTIFICATION_CHECK_INTERVAL": "300",
    "LLM_AGENT_MODEL_SONNET": "claude-sonnet-5",
    "ENABLE_INBOUND_EMAIL": "true",
    "INBOUND_POLL_INTERVAL": "60",
    "SES_INBOUND_S3_BUCKET": "copi-inbound-email",
    "SES_INBOUND_S3_PREFIX": "inbound/",
    "SES_REPLY_DOMAIN": "reply.copi.science",
}


def test_a_stale_env_with_retired_keys_still_loads(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("".join(f"{k}={v}\n" for k, v in RETIRED.items()))
    for k, v in RETIRED.items():
        monkeypatch.setenv(k, v)
    s = Settings(_env_file=str(env))
    for key in RETIRED:
        assert not hasattr(s, key.lower()), key
