"""Unit tests for OrganizationService using in-memory fakes — no database."""

from __future__ import annotations

import pytest

from app.application.services.organization_service import OrganizationService
from tests.fakes import FakeOrganizationRepository


def _make_service() -> tuple[OrganizationService, FakeOrganizationRepository]:
    organizations = FakeOrganizationRepository()
    service = OrganizationService(organization_repository=organizations)
    return service, organizations


@pytest.mark.asyncio
async def test_get_current_returns_the_organization():
    service, organizations = _make_service()
    organization = await organizations.create(name="Acme HVAC", slug="acme-hvac")

    fetched = await service.get_current(organization.id)

    assert fetched.id == organization.id
    assert fetched.name == "Acme HVAC"


@pytest.mark.asyncio
async def test_update_current_renames_without_touching_active_status():
    service, organizations = _make_service()
    organization = await organizations.create(name="Acme HVAC", slug="acme-hvac")

    updated = await service.update_current(organization.id, name="Acme Home Services")

    assert updated.name == "Acme Home Services"
    assert updated.is_active is True


@pytest.mark.asyncio
async def test_update_current_deactivates_without_touching_name():
    service, organizations = _make_service()
    organization = await organizations.create(name="Acme HVAC", slug="acme-hvac")

    updated = await service.update_current(organization.id, is_active=False)

    assert updated.is_active is False
    assert updated.name == "Acme HVAC"


@pytest.mark.asyncio
async def test_update_current_can_reactivate():
    service, organizations = _make_service()
    organization = await organizations.create(name="Acme HVAC", slug="acme-hvac")
    await service.update_current(organization.id, is_active=False)

    updated = await service.update_current(organization.id, is_active=True)

    assert updated.is_active is True
