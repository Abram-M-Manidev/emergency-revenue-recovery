import os

os.environ.setdefault("ENVIRONMENT", "testing")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-not-for-production-use-only-CHANGE-ME")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://errs:errs@localhost:5432/errs_test")

import pytest

from app.core.config import get_settings


@pytest.fixture(autouse=True, scope="session")
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
