import uuid

import pytest

from app.core.config import get_settings
from app.domain.exceptions import InvalidTokenError
from app.infrastructure.security.jwt import create_access_token, decode_access_token


@pytest.fixture
def settings():
    return get_settings()


def test_create_and_decode_access_token_round_trip(settings):
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    token = create_access_token(
        user_id=user_id,
        organization_id=org_id,
        permissions=frozenset({"users:read"}),
        is_superuser=False,
        settings=settings,
    )

    claims = decode_access_token(token, settings=settings)

    assert claims.user_id == user_id
    assert claims.organization_id == org_id
    assert claims.permissions == frozenset({"users:read"})
    assert claims.is_superuser is False


def test_decode_access_token_rejects_garbage(settings):
    with pytest.raises(InvalidTokenError):
        decode_access_token("not-a-real-token", settings=settings)
