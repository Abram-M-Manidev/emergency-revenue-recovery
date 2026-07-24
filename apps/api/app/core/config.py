"""Application configuration.

Settings are loaded from environment variables (and a local .env file in
development) via pydantic-settings. A single `get_settings()` accessor is
cached so the environment is only parsed once per process, and can be
overridden in tests via `get_settings.cache_clear()`.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Environment = Literal["development", "testing", "production"]


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

    # --- Third-party providers (placeholders wired in later milestones) ---
    OPENAI_API_KEY: str | None = None
    VAPI_API_KEY: str | None = None
    TWILIO_ACCOUNT_SID: str | None = None
    TWILIO_AUTH_TOKEN: str | None = None
    TWILIO_PHONE_NUMBER: str | None = None

    # --- Feature flags ---
    FEATURE_REGISTRATION_ENABLED: bool = True
    FEATURE_MULTI_TENANT_SIGNUP: bool = False

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: str | list[str]) -> list[str]:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def is_testing(self) -> bool:
        return self.ENVIRONMENT == "testing"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
