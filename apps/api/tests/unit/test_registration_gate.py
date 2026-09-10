"""`FEATURE_REGISTRATION_ENABLED` as an actual gate on self-service signup.

The flag existed in `Settings` from early on and was read by nothing, so a
deployment that set it to `false` still accepted registrations from anyone who
found the URL. On an internet-facing pilot that is the front door standing
open while the sign says closed.

Two properties are under test, and the second is the one worth having:

1. `false` refuses, `true` behaves exactly as before.
2. A refusal is refused *before* the submitted address is looked up. Ordered
   the other way, a closed deployment would answer 409 for an address that
   exists and 403 for one that does not — an account-existence oracle
   available to anyone who can reach the endpoint. Closing registration must
   not open that.

Everything else about authentication is deliberately asserted to be
unaffected: logins, refreshes and team invitations all keep working while the
front door is shut, because that is the whole point of shutting only the
front door.
"""

from __future__ import annotations

import uuid

import pytest

from app.application.services.auth_service import AuthService
from app.domain.entities.rbac import OWNER_ROLE_NAME
from app.domain.exceptions import EntityAlreadyExistsError, RegistrationDisabledError
from app.domain.repositories.refresh_token_repository import (
    RefreshTokenRecord,
    RefreshTokenRepository,
)
from tests.fakes import (
    FakeOrganizationRepository,
    FakeRoleRepository,
    FakeUserRepository,
    fake_settings,
)


class _InMemoryRefreshTokens(RefreshTokenRepository):
    """Local rather than in `tests/fakes.py`: authentication has only ever
    been exercised through `test_auth_flow.py` against real Postgres, so no
    shared fake exists, and one used by a single module belongs beside it."""

    def __init__(self) -> None:
        self._by_hash: dict[str, RefreshTokenRecord] = {}
        self._revoked: set[uuid.UUID] = set()

    async def create(
        self,
        *,
        token_id: uuid.UUID,
        user_id: uuid.UUID,
        token_hash: str,
        expires_at,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> uuid.UUID:
        self._by_hash[token_hash] = RefreshTokenRecord(
            id=token_id, user_id=user_id, expires_at=expires_at, is_active=True
        )
        return token_id

    async def get_active_by_hash(self, token_hash: str) -> RefreshTokenRecord | None:
        record = self._by_hash.get(token_hash)
        if record is None or record.id in self._revoked:
            return None
        return record

    async def revoke(self, token_id: uuid.UUID, *, replaced_by_id=None) -> None:
        self._revoked.add(token_id)

    async def revoke_all_for_user(self, user_id: uuid.UUID) -> None:
        for record in self._by_hash.values():
            if record.user_id == user_id:
                self._revoked.add(record.id)

_SIGNUP = {
    "organization_name": "Northside Heating & Cooling",
    "full_name": "Ada Owner",
    "email": "owner@example.com",
    "password": "super-secret-123",
}


def _service(*, registration_enabled: bool) -> AuthService:
    # The user repository is given the role repository so `create` can
    # resolve the seeded role ids into real `Role` objects — without it a
    # registered Owner comes back with no roles and the RBAC assertion below
    # would pass or fail for the wrong reason.
    roles = FakeRoleRepository()
    return AuthService(
        user_repository=FakeUserRepository(roles),
        organization_repository=FakeOrganizationRepository(),
        role_repository=roles,
        refresh_token_repository=_InMemoryRefreshTokens(),
        settings=fake_settings(FEATURE_REGISTRATION_ENABLED=registration_enabled),
    )


# --- enabled: nothing changes ------------------------------------------------


@pytest.mark.asyncio
async def test_registration_succeeds_when_enabled():
    """The default, and the behaviour every existing test depends on."""
    service = _service(registration_enabled=True)

    session = await service.register(**_SIGNUP)

    assert session.user.email == _SIGNUP["email"]
    assert session.access_token
    assert session.refresh_token


@pytest.mark.asyncio
async def test_an_enabled_registration_still_creates_the_org_and_owner_role():
    """The gate must not have disturbed tenant creation or RBAC seeding."""
    service = _service(registration_enabled=True)

    session = await service.register(**_SIGNUP)

    assert session.user.organization_id is not None
    assert OWNER_ROLE_NAME in {role.name for role in session.user.roles}


@pytest.mark.asyncio
async def test_duplicate_email_still_conflicts_when_enabled():
    """The pre-existing conflict behaviour is untouched on an open
    deployment — this is the case the ordering below must NOT break."""
    service = _service(registration_enabled=True)
    await service.register(**_SIGNUP)

    with pytest.raises(EntityAlreadyExistsError):
        await service.register(**_SIGNUP)


# --- disabled: refused -------------------------------------------------------


@pytest.mark.asyncio
async def test_registration_is_refused_when_disabled():
    service = _service(registration_enabled=False)

    with pytest.raises(RegistrationDisabledError):
        await service.register(**_SIGNUP)


@pytest.mark.asyncio
async def test_a_refused_registration_writes_nothing():
    """No half-created organization, no orphan user, nothing to clean up."""
    service = _service(registration_enabled=False)
    users = service._users  # type: ignore[attr-defined]

    with pytest.raises(RegistrationDisabledError):
        await service.register(**_SIGNUP)

    assert await users.get_by_email(_SIGNUP["email"]) is None


@pytest.mark.asyncio
async def test_the_refusal_message_names_no_account_and_no_internals():
    """The message reaches the client verbatim (see `core/errors.py`), so it
    must not echo the submitted address back or describe why the deployment
    is closed."""
    service = _service(registration_enabled=False)

    with pytest.raises(RegistrationDisabledError) as caught:
        await service.register(**_SIGNUP)

    message = caught.value.message
    assert _SIGNUP["email"] not in message
    assert _SIGNUP["organization_name"] not in message
    # It should still tell a real person what to do instead.
    assert "invite" in message.lower()


# --- the ordering property ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_closed_deployment_answers_the_same_for_known_and_unknown_emails():
    """The reason the flag is checked before the email lookup.

    If it were checked after, a closed deployment would still distinguish a
    registered address (conflict) from an unregistered one (refusal) — an
    account oracle for anyone who can reach the endpoint. Both must be
    indistinguishable."""
    open_service = _service(registration_enabled=True)
    await open_service.register(**_SIGNUP)
    # Same repositories, now with the door shut.
    closed = AuthService(
        user_repository=open_service._users,  # type: ignore[attr-defined]
        organization_repository=open_service._organizations,  # type: ignore[attr-defined]
        role_repository=open_service._roles,  # type: ignore[attr-defined]
        refresh_token_repository=open_service._refresh_tokens,  # type: ignore[attr-defined]
        settings=fake_settings(FEATURE_REGISTRATION_ENABLED=False),
    )

    with pytest.raises(RegistrationDisabledError) as known:
        await closed.register(**_SIGNUP)
    with pytest.raises(RegistrationDisabledError) as unknown:
        await closed.register(**{**_SIGNUP, "email": "nobody@example.com"})

    # Same exception type AND same message: nothing distinguishes them.
    assert type(known.value) is type(unknown.value)
    assert known.value.message == unknown.value.message


# --- only the front door is shut ---------------------------------------------


@pytest.mark.asyncio
async def test_existing_users_can_still_log_in_while_registration_is_disabled():
    """Closing signup must not lock out the people already using the
    product — otherwise it is an outage, not a policy."""
    service = _service(registration_enabled=True)
    await service.register(**_SIGNUP)

    closed = AuthService(
        user_repository=service._users,  # type: ignore[attr-defined]
        organization_repository=service._organizations,  # type: ignore[attr-defined]
        role_repository=service._roles,  # type: ignore[attr-defined]
        refresh_token_repository=service._refresh_tokens,  # type: ignore[attr-defined]
        settings=fake_settings(FEATURE_REGISTRATION_ENABLED=False),
    )

    session = await closed.login(
        email=_SIGNUP["email"], password=_SIGNUP["password"]
    )

    assert session.access_token


@pytest.mark.asyncio
async def test_refresh_still_works_while_registration_is_disabled():
    service = _service(registration_enabled=True)
    original = await service.register(**_SIGNUP)

    closed = AuthService(
        user_repository=service._users,  # type: ignore[attr-defined]
        organization_repository=service._organizations,  # type: ignore[attr-defined]
        role_repository=service._roles,  # type: ignore[attr-defined]
        refresh_token_repository=service._refresh_tokens,  # type: ignore[attr-defined]
        settings=fake_settings(FEATURE_REGISTRATION_ENABLED=False),
    )

    refreshed = await closed.refresh(raw_refresh_token=original.refresh_token)

    assert refreshed.access_token
    assert refreshed.refresh_token != original.refresh_token
