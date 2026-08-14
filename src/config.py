"""Application configuration from environment variables using Pydantic Settings."""

import logging
import re
from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Stock development secret. It signs BOTH session cookies (src/main.py) and
# unsubscribe tokens (src/services/email_notifications.py), so shipping it to a
# real deployment yields forgeable admin sessions and unsubscribe links. The
# validator below refuses to start with it (or an empty value) outside dev.
INSECURE_SECRET_KEY = "insecure-dev-key-change-me"

# ENVIRONMENT values treated as non-production (the insecure default secret is
# tolerated with a warning). Anything else fails fast.
_DEV_ENVIRONMENTS = {"development", "dev", "local", "test"}

_MASK = "***REDACTED***"

# Field-name substrings that mark a setting whose ENTIRE value is a credential. Any
# such field with a non-empty value is masked in repr()/str() of the Settings object
# so an accidental log line or `repr(settings)` can't dump the ~130 secrets (SEC-19).
#
# Deliberately NOT hinted: "url"/"uri". Those fields carry a credential only inside
# their userinfo or query string, and blanking base_url or database_url wholesale
# would cost an operator the host they are actually pointed at — the first thing you
# read in a deploy postmortem. _redact_url_credentials masks them positionally
# instead. "passwd"/"credential" match no field today; they are here so a future
# `db_passwd` is covered on arrival rather than after the next audit.
_SECRET_NAME_HINTS = ("secret", "token", "key", "password", "passwd", "credential")

# A URL/DSN split into scheme, authority and everything after it.
_URL_RE = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)(?P<authority>[^/?#]*)(?P<rest>.*)",
    re.DOTALL,
)

# Query-parameter names whose value is a credential — libpq/asyncpg DSNs accept
# "?password=...". Narrower than _SECRET_NAME_HINTS on purpose: a Postgres URL also
# carries "?sslkey=/etc/ssl/client.key", a filename an operator needs to be able to
# read, so bare "key" is not enough of a signal on this side.
_URL_QUERY_SECRET_HINTS = (
    "password", "passwd", "secret", "token", "api_key", "apikey", "access_key",
    "credential",
)
_URL_QUERY_SECRET_RE = re.compile(
    r"(?P<sep>[?&])(?P<name>[^=&#\s]*(?:"
    + "|".join(_URL_QUERY_SECRET_HINTS)
    + r")[^=&#\s]*)=(?P<value>[^&#\s]+)",
    re.IGNORECASE,
)


def _redact_url_credentials(value: str) -> str:
    """Mask credentials embedded in a URL/DSN without hiding the rest of it.

    `database_url` is a credential-carrying field whose name matches none of
    `_SECRET_NAME_HINTS`, so `repr(settings)` printed the deployed DSN — password
    included — verbatim. Masking only the password component (the same choice
    SQLAlchemy makes in ``URL.render_as_string(hide_password=True)``) keeps the
    scheme/host/port/database legible, which is the diagnostic value of the field.

    Left deliberately untouched, so that the mask always means "a real credential is
    hidden here" rather than "this field might have one":

    * A URL with no userinfo (``postgresql://host/db``). There is no secret in it;
      masking would destroy the only useful diagnostic and would make the mask
      ambiguous about whether a password is configured at all.
    * A bare userinfo with no ``":"`` (``postgresql://copi@host/db``) — that is a
      username, not a credential. A URL whose userinfo *is* the credential
      (``https://<token>@host``) would live in a ``*_token`` field and be masked
      whole by name.
    * A present-but-empty password (``postgresql://copi:@host/db``), mirroring the
      empty-value rule on the name-based path.

    Non-URL strings are returned unchanged, so this is safe to run over every field.
    """
    m = _URL_RE.match(value)
    if not m:
        return value
    authority = m.group("authority")
    if "@" in authority:
        userinfo, _, host = authority.rpartition("@")
        user, sep, password = userinfo.partition(":")
        if sep and password:
            authority = f"{user}:{_MASK}@{host}"
    rest = _URL_QUERY_SECRET_RE.sub(
        lambda q: f"{q['sep']}{q['name']}={_MASK}", m.group("rest")
    )
    return f"{m['scheme']}{authority}{rest}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Deployment environment. "development" (default) tolerates the insecure
    # default SECRET_KEY; any other value (e.g. "production") fails fast.
    environment: str = "development"

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
    # Sent as `email=` on every E-utilities request. NCBI requires it (with `tool=`)
    # and throttles or blocks unidentified clients. Falls back to ses_sender_email.
    ncbi_contact_email: str = ""

    # App
    secret_key: str = INSECURE_SECRET_KEY
    base_url: str = "http://localhost:8000"
    # Secure by default: session cookies carry the Secure flag (https_only), so
    # they are never sent over plain HTTP. Local HTTP development must opt in
    # explicitly with ALLOW_HTTP_SESSIONS=true.
    allow_http_sessions: bool = False

    # Slack app-configuration token (xoxe-...) used to create bot apps via the
    # Manifest API during provisioning. These seed the first rotation; the
    # rotated pair is persisted in the AppSetting KV table (Slack rotates the
    # config token on every use). See src/services/slack_provisioning.py.
    slack_config_token: str = ""
    slack_config_refresh_token: str = ""

    # Master switch for all Slack integration. None = auto-detect (Slack is on
    # iff at least one agent has a usable bot token); set SLACK_ENABLED=false to
    # force the DB-only mode where the local database is the sole conversation
    # store and no Slack API calls are made. See specs/local-db-conversations.md.
    slack_enabled: bool | None = None

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
    # Comma-separated recipients of the daily activity audit email
    # (prompts/daily_audit.md). Read via audit_recipient_list.
    audit_recipients: str = "asu@scripps.edu,malanjary@scripps.edu,ahuebschen@scripps.edu"

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
    llm_profile_model: str = "claude-opus-5"
    # Agent-turn models, tiered by phase. llm_agent_model is the default for
    # the high-volume cheap paths (phase-2 scan/prune, memory synthesis,
    # make_decision) and stays on the Sonnet tier to cost-match the original
    # claude-sonnet-4-6 profile; the phase-4/5 call sites pass
    # llm_agent_model_opus explicitly. Both Sonnet 5 and Opus 5 think by
    # default and max_tokens caps thinking + text together, so the agent-path
    # LLM calls pin thinking={"type": "disabled"} (src/services/llm.py) to
    # keep today's token/latency envelope — the per-phase max_tokens values
    # are pinned by the characterization golden masters. Revisit (adaptive
    # thinking + larger caps + effort) when prompts unfreeze.
    #
    # llm_agent_model_sonnet is the ancillary knob (GrantBot, PI-DM classify,
    # inbound-email classify). It is NOT the same tier as llm_agent_model
    # despite the name: llm_agent_model IS the Sonnet-5 tier, and this one
    # trailed on claude-sonnet-4-6 until it was moved up alongside
    # llm_profile_model. Every call site of both — including the three that
    # call client.messages.create directly rather than going through
    # generate_agent_response — pins thinking={"type": "disabled"}, because
    # each reads message.content[0].text and a thinking block would take
    # content[0] on any 5-series model.
    llm_agent_model: str = "claude-sonnet-5"
    llm_agent_model_opus: str = "claude-opus-5"
    llm_agent_model_sonnet: str = "claude-sonnet-5"

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
    # agents that share at least one cohort with it. When False (default), the
    # roster is all-vs-all as before. Humans, PI-created private channels and
    # already-open threads always pass the gate.
    # See .notes/cohort-system-v2.md §5.
    cohort_isolation_enabled: bool = False
    # What happens to an agent that belongs to no cohort while isolation is on:
    #   "open"     — unrestricted (default). Enabling isolation is then safe even
    #                with zero cohorts defined: nothing changes until an admin
    #                actually builds a topology.
    #   "isolated" — the agent sees only humans. Cohort membership becomes
    #                mandatory to participate. Guarded by the startup preflight
    #                (_cohort_preflight): with zero cohorts defined this policy
    #                would silence the entire roster, so it is refused and
    #                isolation is forced off with an ERROR.
    # See .notes/cohort-system-v2.md §5.2 / §5.3.
    cohort_default_policy: Literal["open", "isolated"] = "open"
    # Reactive-priority scheduler: after this many consecutive turns given to
    # agents that owe a thread reply, force a normal (proactive) selection so
    # new-conversation formation isn't starved. Default matches
    # active_thread_threshold so the two levers stay in proportion — at the
    # original 8 a single live pair took 24 of 27 turns. See _select_agent and
    # .notes/cohort-system-v2.md §10.3.
    max_consecutive_reactive_turns: int = 3

    # Load-proportional rate limiter. Replaces the cumulative --budget cap as the
    # LIVE throttle: allowance = llm_calls_per_load_per_window * _agent_load(agent),
    # measured over a sliding llm_rate_window_seconds.
    #
    # A rate self-heals — a throttled agent is eligible again as the window slides
    # — where a cumulative cap benches permanently, and, because _rebuild_state
    # restores api_call_count from llm_call_logs, benches permanently ACROSS
    # RESTARTS. That is what took the blackbird hub off the air for 161 turns.
    #
    # Calibrated against run 4f1e8395: a spoke ran ~0.27 calls/10min and the hub
    # ~2.6, so 8 leaves a spoke ~30x headroom while tripping a runaway (back-to-back
    # calls) in ~25s. Lower this to tighten it.
    #
    # The hub's ceiling depends on active_thread_threshold, which _agent_load
    # clamps to. At the IN-CODE default of 3 a hub gets at most 3 * 8 = 24 calls
    # per window (and 3x the selection weight of an idle spoke). The deployed
    # blackbird .env raises active_thread_threshold to 12, which is where the
    # often-quoted 96/window and 12x weight come from — a fresh checkout gets
    # neither. Raise active_thread_threshold, not this number, to widen a hub.
    # See docs/specs/2026-08-06-hub-budget-scheduler-design.md §4.2 / §5.
    llm_rate_window_seconds: int = 600
    llm_calls_per_load_per_window: int = 8

    # Privacy rollout — when True (default), POST /agent/{id}/proposals/{tid}/reopen
    # migrates the thread into a new collab_private channel instead of posting
    # the PI's guidance text into the origin public thread. Can be set to False
    # to restore the legacy behavior during initial rollout or in an emergency.
    # See specs/privacy-and-channel-visibility.md and specs/pi-interaction.md
    # §"PI Reopens a Proposal".
    enable_private_refinement: bool = True

    def __repr_args__(self):
        """Redact credential-valued fields in repr()/str().

        Pydantic v2 routes both ``repr(settings)`` and ``str(settings)`` through
        ``__repr_args__`` (``BaseModel.__str__`` -> ``__repr_str__`` ->
        ``__repr_args__``; verified against pydantic 2.13 and asserted in
        tests/unit/test_config_secret_redaction.py), so masking here closes the
        only described leak path for SEC-19 (an accidental log/repr of the
        settings object) with no change to how any field is *read*. Fields keep
        their plain ``str`` type, avoiding a ``.get_secret_value()`` churn across
        ~130 call sites; a deliberate reader of a specific attribute still gets
        the real value.

        Two rules, because credentials arrive in two shapes:

        1. Whole-value secrets, recognised by field name (``_SECRET_NAME_HINTS``)
           — the ~130 bot tokens, API keys, the signing key.
        2. Credentials embedded in an otherwise-public URL/DSN, recognised by
           value shape (``_redact_url_credentials``) — ``database_url``, whose
           name matches no hint. Masked positionally so the host and database
           stay readable.

        Still out of scope, by design: ``model_dump()`` returns everything in the
        clear. The invariant that keeps that safe is tested in
        tests/unit/test_slack_tokens.py (nothing in src/ dumps a settings object).
        """
        for name, value in super().__repr_args__():
            if value and any(h in str(name).lower() for h in _SECRET_NAME_HINTS):
                yield name, _MASK
            elif value and isinstance(value, str):
                yield name, _redact_url_credentials(value)
            else:
                yield name, value

    @model_validator(mode="after")
    def _guard_secret_key(self) -> "Settings":
        """Fail fast when the insecure default secret would be used in prod.

        The default (or an empty) SECRET_KEY signs forgeable session cookies and
        unsubscribe tokens. Outside a development environment this raises at
        startup so a misconfigured deploy never comes up; in development it is
        tolerated with a warning so local runs stay friction-free.
        """
        if not self.secret_key or self.secret_key == INSECURE_SECRET_KEY:
            if self.environment.strip().lower() not in _DEV_ENVIRONMENTS:
                raise ValueError(
                    "SECRET_KEY is missing or set to the insecure development "
                    f"default while ENVIRONMENT={self.environment!r}. Generate a "
                    "strong random value (e.g. `python -c \"import secrets; "
                    "print(secrets.token_urlsafe(48))\"`) and set SECRET_KEY "
                    "before deploying — see .env.example."
                )
            logger.warning(
                "Using the insecure default SECRET_KEY (ENVIRONMENT=%s). "
                "Acceptable for local development only.",
                self.environment,
            )
        return self

    @property
    def audit_recipient_list(self) -> list[str]:
        """Daily-audit recipients, parsed from the comma-separated setting."""
        return [e.strip() for e in self.audit_recipients.split(",") if e.strip()]

    @model_validator(mode="after")
    def _guard_rate_limiter_settings(self) -> "Settings":
        """Clamp non-positive rate-limiter settings back to their defaults.

        Both fields are divisors of behaviour, not knobs with a meaningful zero:

        - ``llm_calls_per_load_per_window`` <= 0 makes ``len(times) < allowance``
          false for every agent forever, so nobody is ever eligible. The engine no
          longer exits on that (it backs off and retries), which means a typo'd
          ``0`` buys a silent, permanently idle run;
        - ``llm_rate_window_seconds`` <= 0 collapses the window to a point, so
          every recorded call is expired and the limiter never fires at all.

        Clamping rather than raising matches ``roles.py``'s treatment of the
        per-role override (warn, fall back) and keeps a bad value from taking the
        whole deployment down. The WARNING names the setting so the cause is
        greppable — the failure mode this guards against is otherwise invisible.
        """
        for name in ("llm_calls_per_load_per_window", "llm_rate_window_seconds"):
            value = getattr(self, name)
            if value <= 0:
                fallback = type(self).model_fields[name].default
                logger.warning(
                    "%s must be a positive int, got %r — falling back to %r. "
                    "The LLM rate limiter would otherwise never let any agent "
                    "take a turn.",
                    name.upper(), value, fallback,
                )
                setattr(self, name, fallback)
        return self

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
