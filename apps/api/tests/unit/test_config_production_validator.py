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


def test_rejects_the_logging_notification_provider_in_production():
    """The logging provider reports DELIVERED while notifying nobody, which in
    production would licence the assistant to tell an emergency caller a
    dispatcher had been alerted when the only thing that happened was a log
    line — exactly the falsehood the notification port exists to remove."""
    with pytest.raises(ValueError, match="NOTIFICATION_PROVIDER"):
        _settings(NOTIFICATION_PROVIDER="logging")


def test_allows_a_real_notification_provider_in_production():
    settings = _settings(NOTIFICATION_PROVIDER="webhook")
    assert settings.NOTIFICATION_PROVIDER == "webhook"


def test_allows_running_production_without_notifications_configured():
    """A pilot may legitimately start with no alerting, reading its dispatch
    queue by hand. That is safe precisely because 'none' reports
    NOT_CONFIGURED, so callers are told the alert could not be confirmed
    rather than being told a human is on the way."""
    settings = _settings(NOTIFICATION_PROVIDER="none")
    assert settings.NOTIFICATION_PROVIDER == "none"


def test_the_logging_provider_is_still_allowed_in_development():
    settings = _settings(ENVIRONMENT="development", NOTIFICATION_PROVIDER="logging")
    assert settings.NOTIFICATION_PROVIDER == "logging"
