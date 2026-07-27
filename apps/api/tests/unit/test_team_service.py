"""Unit tests for TeamService using in-memory fakes — no database. Mirrors
`test_dispatch_service.py`'s structure."""

from __future__ import annotations

import uuid

import pytest

from app.application.services.team_service import TeamService
from app.domain.entities.rbac import DEFAULT_ROLES, OWNER_ROLE_NAME
from app.domain.exceptions import (
    AuthorizationError,
    EntityAlreadyExistsError,
    EntityNotFoundError,
    LastOwnerError,
)
from tests.fakes import FakeRoleRepository, FakeUserRepository

_ORG_ID = uuid.uuid4()
_OTHER_ORG_ID = uuid.uuid4()


def _make_service() -> tuple[TeamService, FakeUserRepository, FakeRoleRepository]:
    roles = FakeRoleRepository()
    users = FakeUserRepository(role_repository=roles)
    service = TeamService(user_repository=users, role_repository=roles)
    return service, users, roles


async def _seed_owner(users: FakeUserRepository, roles: FakeRoleRepository, *, organization_id=_ORG_ID):
    role = await roles.get_or_create_by_name(
        organization_id, OWNER_ROLE_NAME, DEFAULT_ROLES[OWNER_ROLE_NAME]
    )
    return await users.create(
        organization_id=organization_id,
        email=f"owner-{organization_id}@example.com",
        hashed_password="x",
        full_name="Owner",
        role_ids=[role.id],
    )


@pytest.mark.asyncio
async def test_invite_member_creates_user_with_requested_role():
    service, users, roles = _make_service()

    member = await service.invite_member(
        _ORG_ID,
        full_name="Ada Admin",
        email="ada@example.com",
        temporary_password="hunter22",
        role_name="Admin",
    )

    assert member.organization_id == _ORG_ID
    assert [role.name for role in member.roles] == ["Admin"]
    stored = await users.get_by_id(member.id)
    assert stored is not None


@pytest.mark.asyncio
async def test_invite_member_duplicate_email_raises():
    service, users, roles = _make_service()
    await service.invite_member(
        _ORG_ID, full_name="Ada", email="dupe@example.com",
        temporary_password="hunter22", role_name="Member",
    )

    with pytest.raises(EntityAlreadyExistsError):
        await service.invite_member(
            _ORG_ID, full_name="Ada Two", email="dupe@example.com",
            temporary_password="hunter22", role_name="Member",
        )


@pytest.mark.asyncio
async def test_invite_member_rejects_technician_role():
    service, _, _ = _make_service()

    with pytest.raises(AuthorizationError):
        await service.invite_member(
            _ORG_ID,
            full_name="Tech",
            email="tech@example.com",
            temporary_password="hunter22",
            role_name="Technician",  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_list_members_only_returns_own_organization():
    service, users, roles = _make_service()
    owner = await _seed_owner(users, roles)
    await _seed_owner(users, roles, organization_id=_OTHER_ORG_ID)

    members = await service.list_members(_ORG_ID)

    assert [m.id for m in members] == [owner.id]


@pytest.mark.asyncio
async def test_set_member_active_deactivates_a_non_owner():
    service, users, roles = _make_service()
    owner = await _seed_owner(users, roles)
    member = await service.invite_member(
        _ORG_ID, full_name="Mia Member", email="mia@example.com",
        temporary_password="hunter22", role_name="Member",
    )

    updated = await service.set_member_active(
        _ORG_ID, member.id, is_active=False, acting_user_id=owner.id
    )

    assert updated.is_active is False


@pytest.mark.asyncio
async def test_set_member_active_blocks_self_deactivation():
    service, users, roles = _make_service()
    owner = await _seed_owner(users, roles)
    second_owner = await service.invite_member(
        _ORG_ID, full_name="Second Owner", email="second@example.com",
        temporary_password="hunter22", role_name="Admin",
    )

    with pytest.raises(AuthorizationError):
        await service.set_member_active(
            _ORG_ID, owner.id, is_active=False, acting_user_id=owner.id
        )
    # Unaffected by the blocked attempt.
    assert (await users.get_by_id(owner.id)).is_active is True
    assert second_owner.is_active is True


@pytest.mark.asyncio
async def test_set_member_active_blocks_deactivating_the_last_owner():
    service, users, roles = _make_service()
    owner = await _seed_owner(users, roles)
    admin = await service.invite_member(
        _ORG_ID, full_name="Admin", email="admin@example.com",
        temporary_password="hunter22", role_name="Admin",
    )

    with pytest.raises(LastOwnerError):
        await service.set_member_active(
            _ORG_ID, owner.id, is_active=False, acting_user_id=admin.id
        )


@pytest.mark.asyncio
async def test_set_member_active_allows_deactivating_an_owner_when_another_remains():
    service, users, roles = _make_service()
    owner = await _seed_owner(users, roles)
    other_owner = await service.invite_member(
        _ORG_ID, full_name="Second Owner", email="second-owner@example.com",
        temporary_password="hunter22", role_name="Admin",
    )
    other_owner = await service.set_member_role(
        _ORG_ID, other_owner.id, role_name="Owner", acting_user_id=owner.id
    )

    updated = await service.set_member_active(
        _ORG_ID, owner.id, is_active=False, acting_user_id=other_owner.id
    )

    assert updated.is_active is False


@pytest.mark.asyncio
async def test_set_member_active_cross_org_id_is_not_found():
    service, users, roles = _make_service()
    owner = await _seed_owner(users, roles)
    other_org_owner = await _seed_owner(users, roles, organization_id=_OTHER_ORG_ID)

    with pytest.raises(EntityNotFoundError):
        await service.set_member_active(
            _ORG_ID, other_org_owner.id, is_active=False, acting_user_id=owner.id
        )


@pytest.mark.asyncio
async def test_set_member_role_promotes_admin_to_owner():
    service, users, roles = _make_service()
    owner = await _seed_owner(users, roles)
    admin = await service.invite_member(
        _ORG_ID, full_name="Admin", email="admin2@example.com",
        temporary_password="hunter22", role_name="Admin",
    )

    updated = await service.set_member_role(
        _ORG_ID, admin.id, role_name="Owner", acting_user_id=owner.id
    )

    assert [role.name for role in updated.roles] == ["Owner"]


@pytest.mark.asyncio
async def test_set_member_role_blocks_demoting_the_last_owner():
    service, users, roles = _make_service()
    owner = await _seed_owner(users, roles)
    admin = await service.invite_member(
        _ORG_ID, full_name="Admin", email="admin3@example.com",
        temporary_password="hunter22", role_name="Admin",
    )

    with pytest.raises(LastOwnerError):
        await service.set_member_role(
            _ORG_ID, owner.id, role_name="Admin", acting_user_id=admin.id
        )


@pytest.mark.asyncio
async def test_set_member_role_allows_demotion_when_another_owner_remains():
    service, users, roles = _make_service()
    owner = await _seed_owner(users, roles)
    admin = await service.invite_member(
        _ORG_ID, full_name="Admin", email="admin4@example.com",
        temporary_password="hunter22", role_name="Admin",
    )
    admin = await service.set_member_role(
        _ORG_ID, admin.id, role_name="Owner", acting_user_id=owner.id
    )

    updated = await service.set_member_role(
        _ORG_ID, owner.id, role_name="Admin", acting_user_id=admin.id
    )

    assert [role.name for role in updated.roles] == ["Admin"]


@pytest.mark.asyncio
async def test_set_member_role_rejects_technician():
    service, users, roles = _make_service()
    owner = await _seed_owner(users, roles)
    member = await service.invite_member(
        _ORG_ID, full_name="Mia Member", email="mia2@example.com",
        temporary_password="hunter22", role_name="Member",
    )

    with pytest.raises(AuthorizationError):
        await service.set_member_role(
            _ORG_ID,
            member.id,
            role_name="Technician",  # type: ignore[arg-type]
            acting_user_id=owner.id,
        )
