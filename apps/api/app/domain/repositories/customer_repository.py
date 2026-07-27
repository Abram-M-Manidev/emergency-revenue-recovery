from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import datetime

from app.domain.entities.customer import Customer


class CustomerRepository(ABC):
    @abstractmethod
    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        full_name: str | None,
        phone_number: str,
        email: str | None = None,
        address: str | None = None,
        notes: str | None = None,
    ) -> Customer:
        """Must raise `EntityAlreadyExistsError` (not leak a raw
        `IntegrityError`) if `(organization_id, phone_number)` already
        exists — see `SqlAlchemyAppointmentRepository.create` for the
        `begin_nested()` + catch pattern this should mirror, since two
        concurrent AI Brain turns/webhook retries can race to create the
        same customer."""
        ...

    @abstractmethod
    async def get_by_id(
        self, organization_id: uuid.UUID, customer_id: uuid.UUID
    ) -> Customer | None: ...

    @abstractmethod
    async def get_by_phone_number(
        self, organization_id: uuid.UUID, phone_number: str
    ) -> Customer | None: ...

    @abstractmethod
    async def list_for_organization(
        self,
        organization_id: uuid.UUID,
        *,
        search: str | None = None,
        limit: int,
        offset: int,
    ) -> list[Customer]:
        """`search` matches against name or phone number (case-insensitive,
        substring) when provided."""
        ...

    @abstractmethod
    async def update(
        self,
        organization_id: uuid.UUID,
        customer_id: uuid.UUID,
        *,
        full_name: str | None,
        phone_number: str,
        email: str | None,
        address: str | None,
        notes: str | None,
    ) -> Customer:
        """Must raise `EntityAlreadyExistsError` if the new `phone_number`
        collides with a different customer in the same organization.
        `organization_id` scopes the lookup itself (Milestone 9
        tenant-isolation hardening) rather than relying solely on the
        caller having already verified ownership."""
        ...

    # --- Analytics (Milestone 8) aggregate queries ---

    @abstractmethod
    async def count_new_in_range(
        self, organization_id: uuid.UUID, *, start: datetime | None, end: datetime
    ) -> int:
        """Counts customers created (by `created_at`) in the range."""
        ...

    @abstractmethod
    async def count_total(self, organization_id: uuid.UUID) -> int:
        """All-time customer count for the organization, independent of
        any date range."""
        ...
