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

    # --- AI Brain ---
    # Counts customer+assistant message pairs; a cheap guardrail against
    # runaway LLM cost on a single conversation until real rate limiting
    # (Production Polish milestone) exists.
    AI_MAX_CONVERSATION_TURNS: int = 20

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
