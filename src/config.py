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

    # Slack tokens — one pair per agent
    slack_bot_token_su: str = ""
    slack_app_token_su: str = ""
    slack_bot_token_wiseman: str = ""
    slack_app_token_wiseman: str = ""
    slack_bot_token_lotz: str = ""
    slack_app_token_lotz: str = ""
    slack_bot_token_cravatt: str = ""
    slack_app_token_cravatt: str = ""
    slack_bot_token_grotjahn: str = ""
    slack_app_token_grotjahn: str = ""
    slack_bot_token_petrascheck: str = ""
    slack_app_token_petrascheck: str = ""
    slack_bot_token_ken: str = ""
    slack_app_token_ken: str = ""
    slack_bot_token_racki: str = ""
    slack_app_token_racki: str = ""
    slack_bot_token_saez: str = ""
    slack_app_token_saez: str = ""
    slack_bot_token_wu: str = ""
    slack_app_token_wu: str = ""
    slack_bot_token_ward: str = ""
    slack_app_token_ward: str = ""
    slack_bot_token_briney: str = ""
    slack_app_token_briney: str = ""
    slack_bot_token_forli: str = ""
    slack_app_token_forli: str = ""
    slack_bot_token_deniz: str = ""
    slack_app_token_deniz: str = ""
    slack_bot_token_grantbot: str = ""
    slack_app_token_grantbot: str = ""

    # LLM models
    llm_profile_model: str = "claude-opus-4-6"
    llm_agent_model: str = "claude-sonnet-4-6"
    llm_agent_model_opus: str = "claude-opus-4-6"
    llm_agent_model_sonnet: str = "claude-sonnet-4-6"

    # ElevenLabs (podcast TTS)
    elevenlabs_api_key: str = ""
    elevenlabs_model: str = "eleven_multilingual_v2"
    elevenlabs_default_voice_id: str = "21m00Tcm4TlvDq8ikWAM"  # Rachel — neutral, clear

    # Podcast
    podcast_base_url: str = ""  # e.g. https://copi.science — for RSS enclosure URLs
    podcast_search_window_days: int = 14
    podcast_max_candidates: int = 50

    # Worker
    worker_poll_interval: int = 5  # seconds

    # Simulation parameters
    active_thread_threshold: int = 3        # per-agent max active threads
    max_thread_messages: int = 12           # system-enforced thread close
    interesting_posts_cap: int = 20         # triggers prune
    turn_delay_seconds: float = 0.0         # pause between turns
    phase5_skip_probability: float = 0.0    # chance agent skips new post
    max_abstracts_other_per_thread: int = 10
    max_full_text_per_thread: int = 2

    def get_slack_tokens(self) -> dict[str, dict[str, str]]:
        """Return slack tokens keyed by agent_id."""
        return {
            "su": {"bot": self.slack_bot_token_su, "app": self.slack_app_token_su},
            "wiseman": {"bot": self.slack_bot_token_wiseman, "app": self.slack_app_token_wiseman},
            "lotz": {"bot": self.slack_bot_token_lotz, "app": self.slack_app_token_lotz},
            "cravatt": {
                "bot": self.slack_bot_token_cravatt,
                "app": self.slack_app_token_cravatt,
            },
            "grotjahn": {
                "bot": self.slack_bot_token_grotjahn,
                "app": self.slack_app_token_grotjahn,
            },
            "petrascheck": {
                "bot": self.slack_bot_token_petrascheck,
                "app": self.slack_app_token_petrascheck,
            },
            "ken": {"bot": self.slack_bot_token_ken, "app": self.slack_app_token_ken},
            "racki": {"bot": self.slack_bot_token_racki, "app": self.slack_app_token_racki},
            "saez": {"bot": self.slack_bot_token_saez, "app": self.slack_app_token_saez},
            "wu": {"bot": self.slack_bot_token_wu, "app": self.slack_app_token_wu},
            "ward": {"bot": self.slack_bot_token_ward, "app": self.slack_app_token_ward},
            "briney": {"bot": self.slack_bot_token_briney, "app": self.slack_app_token_briney},
            "forli": {"bot": self.slack_bot_token_forli, "app": self.slack_app_token_forli},
            "deniz": {"bot": self.slack_bot_token_deniz, "app": self.slack_app_token_deniz},
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
