"""Application configuration from environment variables using Pydantic Settings."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ORCID OAuth
    orcid_client_id: str = ""
    orcid_client_secret: str = ""
    orcid_redirect_uri: str = "http://localhost:8000/auth/callback"

    # Database
    database_url: str = "postgresql+asyncpg://copi:copi@localhost:5432/copi"

    # Anthropic
    anthropic_api_key: str = ""

    # NCBI
    ncbi_api_key: str = ""

    # App
    secret_key: str = "insecure-dev-key-change-me"
    base_url: str = "http://localhost:8000"
    allow_http_sessions: bool = True

    # AWS SES
    aws_region: str = "us-east-2"
    ses_sender_email: str = "noreply@copi.science"
    ses_reply_domain: str = "reply.copi.science"
    ses_inbound_s3_bucket: str = "copi-inbound-email"
    ses_inbound_s3_prefix: str = "inbound/"

    # Email notification scheduling
    notification_check_interval: int = 300  # seconds (5 minutes)
    inbound_poll_interval: int = 60  # seconds

    # Slack tokens are loaded dynamically from the environment — see get_slack_tokens().
    # Add any number of agents to .env using the pattern:
    #   SLACK_BOT_TOKEN_<AGENT_ID>=xoxb-...
    #   SLACK_APP_TOKEN_<AGENT_ID>=xapp-...  (optional)

    # LLM models
    llm_profile_model: str = "claude-opus-4-6"
    llm_agent_model: str = "claude-sonnet-4-6"
    llm_agent_model_opus: str = "claude-opus-4-6"
    llm_agent_model_sonnet: str = "claude-sonnet-4-6"

    # Mistral AI (podcast TTS)
    mistral_api_key: str = ""
    mistral_tts_model: str = "voxtral-mini-tts-latest"
    mistral_tts_default_voice: str = ""

    # OpenAI TTS
    openai_api_key: str = ""
    openai_tts_model: str = "tts-1"
    openai_tts_default_voice: str = "alloy"

    # Podcast TTS backend: "mistral" (default), "openai", or "local" (vLLM-Omni)
    podcast_tts_backend: str = "mistral"

    # Local vLLM-Omni TTS server
    local_tts_host: str = "127.0.0.1"
    local_tts_port: int = 8010
    local_tts_model: str = "Qwen/Qwen2-Audio-7B-Instruct"
    local_tts_voice: str = "default"

    # Podcast
    podcast_base_url: str = ""  # e.g. https://copi.science — for RSS enclosure URLs
    podcast_search_window_days: int = 14
    podcast_max_candidates: int = 50
    podcast_normalize_audio: bool = False  # set true to run ffmpeg loudnorm after TTS

    # Worker
    worker_poll_interval: int = 5  # seconds

    # Simulation parameters
    active_thread_threshold: int = 3        # per-agent max active threads
    max_thread_messages: int = 12           # system-enforced thread close
    interesting_posts_cap: int = 20         # triggers prune
    turn_delay_seconds: float = 0.0         # pause between turns
    phase5_skip_probability: float = 0.0    # chance agent skips new post
    daily_post_cap: int = 5                 # max new top-level posts per agent per day
    phase5_spontaneous_interval: float = 20.0  # minutes before allowing a spontaneous Phase 5
    phase5_spontaneous_interval_max_multiplier: int = 5  # cap for skip-backoff stretch
    max_abstracts_other_per_thread: int = 10
    max_full_text_per_thread: int = 2

    def get_slack_tokens(self) -> dict[str, dict[str, str]]:
        """Return Slack tokens keyed by agent_id.

        Scans os.environ and the .env file for variables matching:
            SLACK_BOT_TOKEN_<AGENT_ID>  →  tokens[agent_id]["bot"]
            SLACK_APP_TOKEN_<AGENT_ID>  →  tokens[agent_id]["app"]

        Agent IDs are lowercased from the suffix, so SLACK_BOT_TOKEN_SU → "su".
        os.environ takes precedence over .env file values.
        """
        import os

        from dotenv import dotenv_values

        # Merge: .env file is the base, actual environment variables override.
        env: dict[str, str] = {**dotenv_values(".env"), **os.environ}  # type: ignore[arg-type]

        tokens: dict[str, dict[str, str]] = {}
        for key, val in env.items():
            if not val:
                continue
            upper = key.upper()
            if upper.startswith("SLACK_BOT_TOKEN_"):
                agent_id = key[len("SLACK_BOT_TOKEN_"):].lower()
                tokens.setdefault(agent_id, {"bot": "", "app": ""})["bot"] = val
            elif upper.startswith("SLACK_APP_TOKEN_"):
                agent_id = key[len("SLACK_APP_TOKEN_"):].lower()
                tokens.setdefault(agent_id, {"bot": "", "app": ""})["app"] = val
        return tokens


@lru_cache
def get_settings() -> Settings:
    return Settings()
