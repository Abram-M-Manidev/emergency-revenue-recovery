"""Settings._validate_production_safety: fails fast at construction time
rather than letting a misconfigured production deployment boot."""

import pytest

from app.core.config import Settings

_PLACEHOLDER_JWT_SECRET_KEY = "changeme-generate-a-real-64-byte-secret-for-local-dev"
_REAL_JWT_SECRET_KEY = "a-generated-production-secret-that-is-long-enough-1234567890"


def _settings(**overrides) -> Settings:
    defaults = {
        "ENVIRONMENT": "production",
        "DEBUG": False,
        "JWT_SECRET_KEY": _REAL_JWT_SECRET_KEY,
        "CORS_ORIGINS": ["https://app.example.com"],
    }
    defaults.update(overrides)
    return Settings(**defaults)


def test_a_valid_production_config_boots():
    settings = _settings()
    assert settings.ENVIRONMENT == "production"


def test_rejects_debug_mode_in_production():
    with pytest.raises(ValueError, match="DEBUG"):
        _settings(DEBUG=True)


def test_rejects_placeholder_jwt_secret_in_production():
    with pytest.raises(ValueError, match="JWT_SECRET_KEY"):
        _settings(JWT_SECRET_KEY=_PLACEHOLDER_JWT_SECRET_KEY)


def test_rejects_empty_cors_origins_in_production():
    with pytest.raises(ValueError, match="CORS_ORIGINS"):
        _settings(CORS_ORIGINS=[])


def test_rejects_wildcard_cors_origins_in_production():
    with pytest.raises(ValueError, match="CORS_ORIGINS"):
        _settings(CORS_ORIGINS=["*"])


def test_unsafe_values_are_allowed_outside_production():
    settings = _settings(
        ENVIRONMENT="development",
        DEBUG=True,
        JWT_SECRET_KEY=_PLACEHOLDER_JWT_SECRET_KEY,
        CORS_ORIGINS=["*"],
    )
    assert settings.ENVIRONMENT == "development"
