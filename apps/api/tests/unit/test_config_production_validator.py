"""Settings._validate_production_safety: fails fast at construction time
rather than letting a misconfigured production deployment boot."""

import pytest

from app.core.config import Settings

_PLACEHOLDER_JWT_SECRET_KEY = "changeme-generate-a-real-64-byte-secret-for-local-dev"
_REAL_JWT_SECRET_KEY = "a-generated-production-secret-that-is-long-enough-1234567890"


def _settings(**overrides) -> Settings:
    """A *valid* production configuration, with one thing overridden per test.

    Every field the production validator requires is listed explicitly, even
    where the ambient environment happens to supply it. That is not
    redundancy: `docker compose exec api pytest` runs in a container whose
    environment carries a real `VAPI_SERVER_SECRET` and `OPENAI_API_KEY` from
    `apps/api/.env`, while GitHub Actions sets neither — so a helper that
    relied on the environment would pass locally and fail in CI, and the
    positive assertions below would be proving nothing either way.
    """
    defaults = {
        "ENVIRONMENT": "production",
        "DEBUG": False,
        "JWT_SECRET_KEY": _REAL_JWT_SECRET_KEY,
        "CORS_ORIGINS": ["https://app.example.com"],
        "VAPI_SERVER_SECRET": "a-real-webhook-shared-secret",
        "OPENAI_API_KEY": "sk-not-a-real-key-for-tests-only",
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


# --- Settings that fail closed at runtime, and so must fail loudly at boot ---
#
# Both of the next two are correct-but-silent failures: the deployment looks
# healthy (`/health/ready` passes, the dashboard works) while every real
# phone call is dropped. Refusing to boot moves the discovery from a
# customer's emergency to the deploy.


def test_rejects_production_without_a_vapi_webhook_secret():
    """`is_valid_vapi_secret` fails closed on an unset secret, so the phone
    line would accept nothing and report nothing."""
    with pytest.raises(ValueError, match="VAPI_SERVER_SECRET"):
        _settings(VAPI_SERVER_SECRET=None)


def test_rejects_production_with_a_blank_vapi_webhook_secret():
    with pytest.raises(ValueError, match="VAPI_SERVER_SECRET"):
        _settings(VAPI_SERVER_SECRET="   ")


def test_rejects_production_without_an_openai_key():
    """Every turn would raise `AIProviderUnavailableError` and the caller
    would hear the fallback sentence on every call."""
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        _settings(OPENAI_API_KEY=None)


def test_rejects_cleartext_cors_origins_in_production():
    """The dashboard holds every tenant's customer records."""
    with pytest.raises(ValueError, match="CORS_ORIGINS"):
        _settings(CORS_ORIGINS=["http://app.example.com"])


def test_allows_a_localhost_origin_in_production():
    """An operator port-forwarding to debug a live host is legitimate, and
    localhost is not a cleartext network hop."""
    settings = _settings(
        CORS_ORIGINS=["https://app.example.com", "http://localhost:3000"]
    )
    assert "http://localhost:3000" in settings.CORS_ORIGINS


def test_a_fully_configured_production_deployment_boots():
    settings = _settings(NOTIFICATION_PROVIDER="webhook")
    assert settings.is_production
