"""Orchestrates Milestone 9's Organization settings: letting an Owner view
and rename their organization, or deactivate/reactivate it.

Deactivating an organization blocks future logins/refreshes for every one
of its users (`AuthService.login`/`.refresh` check `organization.is_active`)
but does not revoke already-issued access tokens: `get_current_user`
(app/api/deps.py) re-fetches the user fresh from the database on every
request and would immediately reject a deactivated *user*, but it never
checks the organization's own `is_active` — so a deactivated org's member
holding a still-valid access token keeps working until that token expires
(`ACCESS_TOKEN_EXPIRE_MINUTES`) and a refresh is attempted."""

from __future__ import annotations

import uuid

from app.domain.entities.organization import Organization
from app.domain.repositories.organization_repository import OrganizationRepository


class OrganizationService:
    def __init__(self, *, organization_repository: OrganizationRepository) -> None:
        self._organizations = organization_repository

    async def get_current(self, organization_id: uuid.UUID) -> Organization:
        organization = await self._organizations.get_by_id(organization_id)
        if organization is None:
            # Unreachable in practice: organization_id always comes from the
            # caller's own validated JWT, never a client-supplied value.
            raise LookupError(f"Organization '{organization_id}' was not found.")
        return organization

    async def update_current(
        self,
        organization_id: uuid.UUID,
        *,
        name: str | None = None,
        is_active: bool | None = None,
    ) -> Organization:
        return await self._organizations.update(organization_id, name=name, is_active=is_active)
