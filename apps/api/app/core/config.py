"""Application configuration.

Settings are loaded from environment variables (and a local .env file in
development) via pydantic-settings. A single `get_settings()` accessor is
cached so the environment is only parsed once per process, and can be
overridden in tests via `get_settings.cache_clear()`.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Environment = Literal["development", "testing", "production"]

# OpenAI's reasoning-effort knob. Typed here (rather than a bare `str`) so a
# misspelled value fails at startup instead of as a 400 on the first live
# call. Note the installed SDK (1.59.6) predates `"minimal"` and still types
# its own parameter as Literal["low", "medium", "high"] — see
# `openai_provider.py` for why the value is sent via `extra_body`.
ReasoningEffort = Literal["minimal", "low", "medium", "high"]

# Which outbound notification adapter backs emergency alerting.
# "none" is the default and reports NOT_CONFIGURED, so a deployment that
# has not set alerting up tells callers the truth instead of claiming an
# alert. "logging" reports success while telling nobody and is therefore
# refused in production (see `_validate_production_safety`).
NotificationProvider = Literal["none", "logging", "webhook"]

# The literal placeholder shipped in `.env.example` — if this is still the
# configured value in production, the secret was never actually generated.
_PLACEHOLDER_JWT_SECRET_KEY = "changeme-generate-a-real-64-byte-secret-for-local-dev"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application ---
    APP_NAME: str = "Emergency Revenue Recovery System"
    ENVIRONMENT: Environment = "development"
    DEBUG: bool = False
    API_V1_PREFIX: str = "/api/v1"

    # --- Security / JWT ---
    JWT_SECRET_KEY: str = Field(min_length=32)
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # --- Database ---
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://errs:errs@localhost:5432/errs"
    )
    DATABASE_POOL_SIZE: int = 10
    DATABASE_MAX_OVERFLOW: int = 20
    DATABASE_ECHO: bool = False

    # --- CORS ---
    # NoDecode: pydantic-settings would otherwise try to JSON-parse the raw
    # env value before our validator runs, and fail on a plain comma-separated string.
    CORS_ORIGINS: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )

    # --- Logging ---
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = True

    # --- Third-party providers ---
    OPENAI_API_KEY: str | None = None
    # Two model profiles, selected per conversation channel (see
    # `app/domain/ai/provider.py`'s AIModelProfile). QUALITY backs the
    # text/simulation dashboard, where a few seconds of latency is
    # irrelevant and classification depth is worth paying for. REALTIME
    # backs live Vapi phone calls, where every second is dead air the
    # caller hears. Benchmarked 2026-08-12 on one representative emergency
    # prompt: gpt-5 default effort 19.8-23.3s, gpt-5 low 8.6s,
    # gpt-4.1-mini 3.3s — all four reached the same
    # emergency/create_emergency_ticket outcome.
    OPENAI_MODEL: str = "gpt-5"
    OPENAI_REASONING_EFFORT: ReasoningEffort | None = "low"
    OPENAI_REALTIME_MODEL: str = "gpt-4.1-mini"
    # gpt-4.1-mini is not a reasoning model; sending the parameter to one
    # that doesn't support it is a 400, so this stays unset by default.
    OPENAI_REALTIME_REASONING_EFFORT: ReasoningEffort | None = None
    # VAPI_API_KEY/TWILIO_*: unused by application code as of Milestone 4.
    # Provisioning (creating the Vapi assistant, importing the Twilio
    # number) is an ops-side step done outside this app — these remain
    # placeholders for whoever does that manually. VAPI_SERVER_SECRET is
    # the one Vapi-related setting the backend actually reads: it verifies
    # inbound webhook requests really came from our Vapi account (see
    # `app/api/deps.py`'s `verify_vapi_secret`).
    VAPI_API_KEY: str | None = None
    VAPI_SERVER_SECRET: str | None = None
    TWILIO_ACCOUNT_SID: str | None = None
    TWILIO_AUTH_TOKEN: str | None = None
    TWILIO_PHONE_NUMBER: str | None = None

    # --- Emergency notification ---
    # Emergency callers are told a dispatcher has been alerted. Until this
    # existed nothing outbound was ever sent, so that sentence was false on
    # every call that produced one. The provider decides whether the backend
    # can honestly report an alert; the assistant is never allowed to decide
    # it for itself (see `voice_tool_executor._create_service_request`).
    NOTIFICATION_PROVIDER: NotificationProvider = "none"
    # The whole send, per attempt. This runs inside a live voice turn, where
    # every second is silence the caller hears — so it is deliberately far
    # below the AI timeouts above. Exceeding it is a FAILED delivery the
    # assistant reports truthfully, never an exception.
    NOTIFICATION_TIMEOUT_SECONDS: float = 5.0
    # Total attempts across the whole call, not per turn: the retry only
    # fires when an earlier attempt actually FAILED, so two covers a
    # transient blip without turning one emergency into a page storm.
    NOTIFICATION_MAX_ATTEMPTS: int = 2

    # --- AI Brain ---
    # Counts customer+assistant message pairs; a cheap guardrail against
    # runaway LLM cost on a single conversation until real rate limiting
    # (Production Polish milestone) exists.
    AI_MAX_CONVERSATION_TURNS: int = 20

    # --- AI Brain business tools ---
    # The tool loop lets the AI Brain create a service request, check real
    # availability, and book a slot *during* a turn, instead of only
    # emitting a `recommended_action` the backend reacts to afterwards.
    # Kept behind a flag so the pre-tool behaviour is one setting away if a
    # live call ever regresses.
    AI_TOOLS_ENABLED: bool = True
    # How many rounds may *execute tools*. The loop runs one more round than
    # this, with tools withheld, in which the model must produce the sentence
    # the caller hears — so N here means N tool rounds and at most N+1 model
    # calls per turn. The bound is a `for` over a fixed range and the loop
    # ends in a raise, so there is no path that iterates freely.
    #
    # Five, not four. The real call of 2026-08-23 used every round of a
    # budget of four: the model booked before checking (refused,
    # SLOT_NOT_OFFERED), re-created the service request, checked
    # availability, then booked successfully — three tool rounds, leaving
    # exactly one to answer in. One more recovery step and the turn would
    # have exhausted the budget and told the caller it was having trouble
    # connecting, *after* the appointment had been successfully booked. The
    # fifth round is the margin that failure showed we were missing.
    AI_MAX_TOOL_ROUNDS: int = 5
    # A tool that hangs is silence the caller hears. Every tool here is a
    # handful of indexed queries, so this is a generous ceiling, not a
    # budget.
    AI_TOOL_TIMEOUT_SECONDS: float = 10.0

    # --- Scheduling / availability ---
    # Used only when the matched Service has no `default_duration_minutes`.
    SCHEDULING_DEFAULT_DURATION_MINUTES: int = 60
    # Offered start times land on this grid, so a caller hears "10:00 or
    # 10:30", never "10:07".
    SCHEDULING_SLOT_GRANULARITY_MINUTES: int = 30
    # A caller must not be able to book a technician for a few minutes from
    # now — dispatch needs notice.
    SCHEDULING_MIN_LEAD_MINUTES: int = 120
    SCHEDULING_DEFAULT_SEARCH_DAYS: int = 7
    SCHEDULING_MAX_SEARCH_DAYS: int = 14
    # Deliberately small: this is read aloud on a phone call, where more
    # than about three options stops being a choice and starts being a list
    # the caller cannot hold in their head.
    SCHEDULING_MAX_SLOTS: int = 3
    # How many appointments may overlap one slot when the organization has
    # no technician profiles at all. Once a roster exists, the count of
    # on-call technicians is used instead and this is ignored — see
    # `DatabaseAvailabilityProvider._capacity_for`.
    SCHEDULING_DEFAULT_CAPACITY: int = 1

    # --- Feature flags ---
    FEATURE_REGISTRATION_ENABLED: bool = True
    FEATURE_MULTI_TENANT_SIGNUP: bool = False

    # --- Rate limiting ---
    # In-memory, per-process (see app/infrastructure/security/rate_limiter.py).
    # Under the production compose's multi-worker uvicorn, each worker
    # enforces its own counter, so the real ceiling is roughly
    # (limit x worker count) — an accepted trade-off, not a bug.
    RATE_LIMIT_DEFAULT_PER_MINUTE: int = 100
    RATE_LIMIT_AUTH_PER_MINUTE: int = 10

    # --- Request limits ---
    MAX_REQUEST_BODY_BYTES: int = 1_048_576  # 1 MiB

    # --- AI Brain provider reliability ---
    # Client-level timeout/retry passed straight to the OpenAI SDK so a
    # hung call can never block a live emergency-call request indefinitely.
    #
    # 45s (was 20s): gpt-5 at default reasoning effort measured 19.8s and
    # 23.3s on two runs of the same prompt, straddling the old 20s ceiling
    # — which is why the smoke test failed intermittently while the
    # integration itself was correct. 45s leaves real headroom above the
    # slowest observed QUALITY-profile call.
    OPENAI_TIMEOUT_SECONDS: float = 45.0
    OPENAI_MAX_RETRIES: int = 1
    # The REALTIME profile deliberately does NOT inherit the above. On a
    # live call the caller hears the entire timeout as silence, so waiting
    # 45s is strictly worse than failing fast into the speakable fallback
    # in `vapi_webhooks.py`; and a retry doubles that silence while
    # double-billing for a turn the caller has likely already abandoned.
    # 15s is ~4.5x the benchmarked 3.3s gpt-4.1-mini latency.
    OPENAI_REALTIME_TIMEOUT_SECONDS: float = 15.0
    OPENAI_REALTIME_MAX_RETRIES: int = 0

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: str | list[str]) -> list[str]:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator(
        "OPENAI_REASONING_EFFORT", "OPENAI_REALTIME_REASONING_EFFORT", mode="before"
    )
    @classmethod
    def _empty_reasoning_effort_is_none(cls, value: str | None) -> str | None:
        """`FOO=` in a .env file yields an empty string, not an absence.
        Without this, the documented way to say "this profile has no
        reasoning effort" would fail Literal validation and refuse to boot
        the app."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _validate_production_safety(self) -> Settings:
        """Fails fast at startup rather than booting a production deployment
        with an unsafe configuration (debug mode on, a never-rotated
        placeholder secret, or CORS open to every origin)."""
        if self.ENVIRONMENT != "production":
            return self

        if self.DEBUG:
            raise ValueError("DEBUG must be false when ENVIRONMENT=production.")
        if self.JWT_SECRET_KEY == _PLACEHOLDER_JWT_SECRET_KEY:
            raise ValueError(
                "JWT_SECRET_KEY is still the .env.example placeholder value; "
                "generate a real secret before running in production."
            )
        if not self.CORS_ORIGINS or "*" in self.CORS_ORIGINS:
            raise ValueError(
                "CORS_ORIGINS must be an explicit, non-wildcard list of origins "
                "when ENVIRONMENT=production."
            )
        if self.NOTIFICATION_PROVIDER == "logging":
            # The logging provider reports DELIVERED while notifying nobody.
            # In development that is a convenience; in production it would
            # licence the assistant to tell an emergency caller a dispatcher
            # had been alerted when the only thing that happened was a log
            # line — exactly the falsehood this provider abstraction exists
            # to remove. Refuse to boot rather than ship it.
            raise ValueError(
                "NOTIFICATION_PROVIDER='logging' notifies nobody and must not be "
                "used when ENVIRONMENT=production. Use 'webhook', or 'none' to "
                "run without emergency alerting (callers will be told it could "
                "not be confirmed)."
            )
        return self

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def is_testing(self) -> bool:
        return self.ENVIRONMENT == "testing"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
