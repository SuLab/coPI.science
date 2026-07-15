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

    # Slack app-configuration token (xoxe-...) used to create bot apps via the
    # Manifest API during provisioning. These seed the first rotation; the
    # rotated pair is persisted in the AppSetting KV table (Slack rotates the
    # config token on every use). See src/services/slack_provisioning.py.
    slack_config_token: str = ""
    slack_config_refresh_token: str = ""

    # AWS SES
    aws_region: str = "us-east-2"
    ses_sender_email: str = "noreply@copi.science"
    ses_reply_domain: str = "reply.copi.science"
    ses_inbound_s3_bucket: str = "copi-inbound-email"
    ses_inbound_s3_prefix: str = "inbound/"
    # Comma-separated recipient allowlist. Empty = send to everyone.
    outbound_email_allowlist: str = ""
    # When False, the worker skips inbound S3 polling (avoids log spam if S3 isn't set up).
    enable_inbound_email: bool = False

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
    slack_bot_token_millar: str = ""
    # UCSF (Cabo retreat)
    slack_bot_token_sali: str = ""
    slack_bot_token_larabell: str = ""
    slack_bot_token_zaro: str = ""
    slack_bot_token_roe: str = ""
    slack_bot_token_santi: str = ""
    slack_bot_token_wells: str = ""
    slack_bot_token_echeverria: str = ""
    slack_bot_token_fraser: str = ""
    slack_bot_token_craik: str = ""
    slack_bot_token_stroud: str = ""
    slack_bot_token_minor: str = ""
    slack_bot_token_manglik: str = ""
    slack_bot_token_susa: str = ""
    slack_bot_token_capra: str = ""
    # Additional PIs (post-Cabo)
    slack_bot_token_kim: str = ""
    slack_bot_token_azumaya: str = ""
    slack_bot_token_nomura: str = ""
    slack_bot_token_yeager: str = ""
    slack_bot_token_moore: str = ""
    slack_bot_token_young: str = ""
    # Onboarding batch 2026-06 (newuserlist01/02)
    slack_bot_token_achatterjee: str = ""
    slack_bot_token_bollong: str = ""
    slack_bot_token_chatterjee: str = ""
    slack_bot_token_chen: str = ""
    slack_bot_token_chin: str = ""
    slack_bot_token_ckim: str = ""
    slack_bot_token_cliu: str = ""
    slack_bot_token_cochran: str = ""
    slack_bot_token_corey: str = ""
    slack_bot_token_cornish: str = ""
    slack_bot_token_diercks: str = ""
    slack_bot_token_ding: str = ""
    slack_bot_token_ellman: str = ""
    slack_bot_token_good: str = ""
    slack_bot_token_gray: str = ""
    slack_bot_token_hsiehwilson: str = ""
    slack_bot_token_johnsson: str = ""
    slack_bot_token_lemke: str = ""
    slack_bot_token_liu: str = ""
    slack_bot_token_lyssiotis: str = ""
    slack_bot_token_mehta: str = ""
    slack_bot_token_pei: str = ""
    slack_bot_token_pezacki: str = ""
    slack_bot_token_schen: str = ""
    slack_bot_token_schultz: str = ""
    slack_bot_token_shao: str = ""
    slack_bot_token_shokat: str = ""
    slack_bot_token_ting: str = ""
    slack_bot_token_wang: str = ""
    slack_bot_token_williams: str = ""
    slack_bot_token_winssinger: str = ""
    slack_bot_token_wliu: str = ""
    slack_bot_token_xiao: str = ""
    slack_bot_token_yang: str = ""
    # Onboarding batch 3 — Schultz reunion attendees (2026-06-06)
    slack_bot_token_mcnamara: str = ""
    slack_bot_token_watanabe: str = ""
    slack_bot_token_summerer: str = ""
    slack_bot_token_vranken: str = ""
    slack_bot_token_wemmer: str = ""
    slack_bot_token_zhou: str = ""
    slack_bot_token_dyoung: str = ""
    slack_bot_token_brustad: str = ""
    slack_bot_token_gan: str = ""
    slack_bot_token_larman: str = ""
    slack_bot_token_wurdak: str = ""
    slack_bot_token_ulrich: str = ""
    slack_bot_token_luesch: str = ""
    slack_bot_token_ai: str = ""
    slack_bot_token_gildersleeve: str = ""
    slack_bot_token_mills: str = ""
    slack_bot_token_xie: str = ""
    slack_bot_token_guo: str = ""
    slack_bot_token_liao: str = ""
    slack_bot_token_jwang: str = ""
    slack_bot_token_hogenesch: str = ""
    slack_bot_token_lee: str = ""
    slack_bot_token_alfonta: str = ""
    slack_bot_token_meijler: str = ""
    slack_bot_token_koh: str = ""
    slack_bot_token_goto: str = ""
    slack_bot_token_lin: str = ""
    slack_bot_token_zuckermann: str = ""
    slack_bot_token_rwang: str = ""
    slack_bot_token_mehl: str = ""
    slack_bot_token_cherry: str = ""
    slack_bot_token_schiller: str = ""
    slack_bot_token_santoro: str = ""
    slack_bot_token_cropp: str = ""
    slack_bot_token_scanlan: str = ""
    slack_bot_token_xchen: str = ""
    slack_bot_token_xwu: str = ""
    slack_bot_token_zhang: str = ""
    slack_bot_token_chang: str = ""
    slack_bot_token_yliu: str = ""
    slack_bot_token_magliery: str = ""
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
    unreviewed_proposal_block_count: int = 2  # block Phase 5 new posts at N+ unreviewed non-funding proposals
    max_thread_messages: int = 12           # system-enforced thread close
    interesting_posts_cap: int = 20         # triggers prune
    turn_delay_seconds: float = 0.0         # pause between turns
    phase5_skip_probability: float = 0.0    # chance agent skips new post
    daily_post_cap: int = 5                 # max new top-level posts per agent per day
    phase5_spontaneous_interval: float = 20.0  # minutes before allowing a spontaneous Phase 5
    phase5_spontaneous_interval_max_multiplier: int = 5  # cap for skip-backoff stretch
    max_abstracts_other_per_thread: int = 10
    max_full_text_per_thread: int = 2

    # Cohort isolation — when True, an agent only acts on posts/threads/tags from
    # agents that share at least one cohort with it (uncohorted agents are
    # isolated). When False (default), the roster is all-vs-all as before.
    # See specs/cohort-system.md.
    cohort_isolation_enabled: bool = False
    # Reactive-priority scheduler: after this many consecutive turns given to
    # agents that owe a thread reply, force a normal (proactive) selection so
    # new-conversation formation isn't starved. See _select_agent.
    max_consecutive_reactive_turns: int = 8

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
            "millar": self.slack_bot_token_millar,
            "sali": self.slack_bot_token_sali,
            "larabell": self.slack_bot_token_larabell,
            "zaro": self.slack_bot_token_zaro,
            "roe": self.slack_bot_token_roe,
            "santi": self.slack_bot_token_santi,
            "wells": self.slack_bot_token_wells,
            "echeverria": self.slack_bot_token_echeverria,
            "fraser": self.slack_bot_token_fraser,
            "craik": self.slack_bot_token_craik,
            "stroud": self.slack_bot_token_stroud,
            "minor": self.slack_bot_token_minor,
            "manglik": self.slack_bot_token_manglik,
            "susa": self.slack_bot_token_susa,
            "capra": self.slack_bot_token_capra,
            "kim": self.slack_bot_token_kim,
            "azumaya": self.slack_bot_token_azumaya,
            "nomura": self.slack_bot_token_nomura,
            "yeager": self.slack_bot_token_yeager,
            "moore": self.slack_bot_token_moore,
            "young": self.slack_bot_token_young,
            # Onboarding batch 2026-06 (newuserlist01/02)
            "achatterjee": self.slack_bot_token_achatterjee,
            "bollong": self.slack_bot_token_bollong,
            "chatterjee": self.slack_bot_token_chatterjee,
            "chen": self.slack_bot_token_chen,
            "chin": self.slack_bot_token_chin,
            "ckim": self.slack_bot_token_ckim,
            "cliu": self.slack_bot_token_cliu,
            "cochran": self.slack_bot_token_cochran,
            "corey": self.slack_bot_token_corey,
            "cornish": self.slack_bot_token_cornish,
            "diercks": self.slack_bot_token_diercks,
            "ding": self.slack_bot_token_ding,
            "ellman": self.slack_bot_token_ellman,
            "good": self.slack_bot_token_good,
            "gray": self.slack_bot_token_gray,
            "hsiehwilson": self.slack_bot_token_hsiehwilson,
            "johnsson": self.slack_bot_token_johnsson,
            "lemke": self.slack_bot_token_lemke,
            "liu": self.slack_bot_token_liu,
            "lyssiotis": self.slack_bot_token_lyssiotis,
            "mehta": self.slack_bot_token_mehta,
            "pei": self.slack_bot_token_pei,
            "pezacki": self.slack_bot_token_pezacki,
            "schen": self.slack_bot_token_schen,
            "schultz": self.slack_bot_token_schultz,
            "shao": self.slack_bot_token_shao,
            "shokat": self.slack_bot_token_shokat,
            "ting": self.slack_bot_token_ting,
            "wang": self.slack_bot_token_wang,
            "williams": self.slack_bot_token_williams,
            "winssinger": self.slack_bot_token_winssinger,
            "wliu": self.slack_bot_token_wliu,
            "xiao": self.slack_bot_token_xiao,
            "yang": self.slack_bot_token_yang,
            # Onboarding batch 3 — Schultz reunion attendees (2026-06-06)
            "mcnamara": self.slack_bot_token_mcnamara,
            "watanabe": self.slack_bot_token_watanabe,
            "summerer": self.slack_bot_token_summerer,
            "vranken": self.slack_bot_token_vranken,
            "wemmer": self.slack_bot_token_wemmer,
            "zhou": self.slack_bot_token_zhou,
            "dyoung": self.slack_bot_token_dyoung,
            "brustad": self.slack_bot_token_brustad,
            "gan": self.slack_bot_token_gan,
            "larman": self.slack_bot_token_larman,
            "wurdak": self.slack_bot_token_wurdak,
            "ulrich": self.slack_bot_token_ulrich,
            "luesch": self.slack_bot_token_luesch,
            "ai": self.slack_bot_token_ai,
            "gildersleeve": self.slack_bot_token_gildersleeve,
            "mills": self.slack_bot_token_mills,
            "xie": self.slack_bot_token_xie,
            "guo": self.slack_bot_token_guo,
            "liao": self.slack_bot_token_liao,
            "jwang": self.slack_bot_token_jwang,
            "hogenesch": self.slack_bot_token_hogenesch,
            "lee": self.slack_bot_token_lee,
            "alfonta": self.slack_bot_token_alfonta,
            "meijler": self.slack_bot_token_meijler,
            "koh": self.slack_bot_token_koh,
            "goto": self.slack_bot_token_goto,
            "lin": self.slack_bot_token_lin,
            "zuckermann": self.slack_bot_token_zuckermann,
            "rwang": self.slack_bot_token_rwang,
            "mehl": self.slack_bot_token_mehl,
            "cherry": self.slack_bot_token_cherry,
            "schiller": self.slack_bot_token_schiller,
            "santoro": self.slack_bot_token_santoro,
            "cropp": self.slack_bot_token_cropp,
            "scanlan": self.slack_bot_token_scanlan,
            "xchen": self.slack_bot_token_xchen,
            "xwu": self.slack_bot_token_xwu,
            "zhang": self.slack_bot_token_zhang,
            "chang": self.slack_bot_token_chang,
            "yliu": self.slack_bot_token_yliu,
            "magliery": self.slack_bot_token_magliery,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
