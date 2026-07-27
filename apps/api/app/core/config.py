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
    OPENAI_MODEL: str = "gpt-5"
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
    OPENAI_TIMEOUT_SECONDS: float = 20.0
    OPENAI_MAX_RETRIES: int = 1

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: str | list[str]) -> list[str]:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
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
