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

    # Slack bot tokens — one per agent
    slack_bot_token_su: str = ""
    slack_bot_token_wiseman: str = ""
    slack_bot_token_lotz: str = ""
    slack_bot_token_cravatt: str = ""
    slack_bot_token_grotjahn: str = ""
    slack_bot_token_petrascheck: str = ""
    slack_bot_token_ken: str = ""
    slack_bot_token_racki: str = ""
    slack_bot_token_saez: str = ""
    slack_bot_token_wu: str = ""
    slack_bot_token_ward: str = ""
    slack_bot_token_briney: str = ""
    slack_bot_token_forli: str = ""
    slack_bot_token_deniz: str = ""
    slack_bot_token_lairson: str = ""
    slack_bot_token_badran: str = ""
    slack_bot_token_kern: str = ""
    slack_bot_token_lasker: str = ""
    slack_bot_token_lippi: str = ""
    slack_bot_token_macrae: str = ""
    slack_bot_token_maillie: str = ""
    slack_bot_token_miller: str = ""
    slack_bot_token_mravic: str = ""
    slack_bot_token_paulson: str = ""
    slack_bot_token_pwu: str = ""
    slack_bot_token_seiple: str = ""
    slack_bot_token_williamson: str = ""
    slack_bot_token_wilson: str = ""
    slack_bot_token_grantbot: str = ""

    # Analytics
    posthog_api_key: str = ""

    # LLM models
    llm_profile_model: str = "claude-opus-4-6"
    llm_agent_model: str = "claude-sonnet-4-6"
    llm_agent_model_opus: str = "claude-opus-4-6"
    llm_agent_model_sonnet: str = "claude-sonnet-4-6"

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

    # Privacy rollout — when True (default), POST /agent/{id}/proposals/{tid}/reopen
    # migrates the thread into a new collab_private channel instead of posting
    # the PI's guidance text into the origin public thread. Can be set to False
    # to restore the legacy behavior during initial rollout or in an emergency.
    # See specs/privacy-and-channel-visibility.md and specs/pi-interaction.md
    # §"PI Reopens a Proposal".
    enable_private_refinement: bool = True

    def get_slack_tokens(self) -> dict[str, str]:
        """Return slack bot tokens keyed by agent_id."""
        return {
            "su": self.slack_bot_token_su,
            "wiseman": self.slack_bot_token_wiseman,
            "lotz": self.slack_bot_token_lotz,
            "cravatt": self.slack_bot_token_cravatt,
            "grotjahn": self.slack_bot_token_grotjahn,
            "petrascheck": self.slack_bot_token_petrascheck,
            "ken": self.slack_bot_token_ken,
            "racki": self.slack_bot_token_racki,
            "saez": self.slack_bot_token_saez,
            "wu": self.slack_bot_token_wu,
            "ward": self.slack_bot_token_ward,
            "briney": self.slack_bot_token_briney,
            "forli": self.slack_bot_token_forli,
            "deniz": self.slack_bot_token_deniz,
            "lairson": self.slack_bot_token_lairson,
            "badran": self.slack_bot_token_badran,
            "kern": self.slack_bot_token_kern,
            "lasker": self.slack_bot_token_lasker,
            "lippi": self.slack_bot_token_lippi,
            "macrae": self.slack_bot_token_macrae,
            "maillie": self.slack_bot_token_maillie,
            "miller": self.slack_bot_token_miller,
            "mravic": self.slack_bot_token_mravic,
            "paulson": self.slack_bot_token_paulson,
            "pwu": self.slack_bot_token_pwu,
            "seiple": self.slack_bot_token_seiple,
            "williamson": self.slack_bot_token_williamson,
            "wilson": self.slack_bot_token_wilson,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
