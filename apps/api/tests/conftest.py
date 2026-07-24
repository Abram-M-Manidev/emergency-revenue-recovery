"""Test environment setup.

`ENVIRONMENT` and `DATABASE_URL` are forced (not just defaulted) rather than
using `os.environ.setdefault`, because `docker compose exec api pytest` runs
inside the same container as the dev server, which already has both set (to
`development` and the dev database) via docker-compose.yml's `environment:`
block — `setdefault` would silently no-op and the integration tests'
`Base.metadata.drop_all()` would wipe the live dev database instead of an
isolated test one. Forcing a `_test`-suffixed database name here, regardless
of whatever DATABASE_URL the container was started with, is what keeps
`docker compose exec api pytest` safe to run against the dev stack.
"""

import os
import re

import pytest

os.environ["ENVIRONMENT"] = "testing"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-not-for-production-use-only-CHANGE-ME")

_db_url = os.environ.get("DATABASE_URL", "postgresql+asyncpg://errs:errs@localhost:5432/errs")
if not re.search(r"_test$", _db_url.split("?")[0]):
    _db_url = re.sub(r"(/[^/?]+)(\?.*)?$", lambda m: f"{m.group(1)}_test{m.group(2) or ''}", _db_url)
os.environ["DATABASE_URL"] = _db_url

from app.core.config import get_settings  # noqa: E402


@pytest.fixture(autouse=True, scope="session")
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
