from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import datetime

from app.domain.entities.emergency_ticket import EmergencyTicket, TicketStatus


class EmergencyTicketRepository(ABC):
    @abstractmethod
    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        matched_service_id: uuid.UUID | None,
        customer_name: str | None,
        customer_phone: str | None,
        customer_address: str | None,
        summary: str,
    ) -> EmergencyTicket:
        """Idempotent by `conversation_id`: implementations must catch a
        unique-constraint conflict on that column and return the
        already-existing ticket instead of raising — a concurrent retry of
        the same AI Brain turn must never fail or duplicate a ticket."""
        ...

    @abstractmethod
    async def get_by_id(
        self, organization_id: uuid.UUID, ticket_id: uuid.UUID
    ) -> EmergencyTicket | None: ...

    @abstractmethod
    async def get_by_conversation_id(
        self, conversation_id: uuid.UUID
    ) -> EmergencyTicket | None: ...

    @abstractmethod
    async def list_for_organization(
        self,
        organization_id: uuid.UUID,
        *,
        status: TicketStatus | None = None,
        limit: int,
        offset: int,
    ) -> list[EmergencyTicket]: ...

    @abstractmethod
    async def assign(
        self,
        ticket_id: uuid.UUID,
        *,
        technician_user_id: uuid.UUID,
        assigned_at: datetime,
    ) -> EmergencyTicket: ...

    @abstractmethod
    async def update_status(
        self,
        ticket_id: uuid.UUID,
        *,
        status: TicketStatus,
        closed_at: datetime | None = None,
    ) -> EmergencyTicket: ...

    @abstractmethod
    async def set_customer(
        self, ticket_id: uuid.UUID, *, customer_id: uuid.UUID
    ) -> EmergencyTicket:
        """Links a ticket to a `Customer` (Milestone 7) after the fact —
        called by `CustomerService.sync_customer_from_outcome`, never at
        ticket-creation time."""
        ...

    @abstractmethod
    async def list_by_customer_id(
        self, organization_id: uuid.UUID, customer_id: uuid.UUID
    ) -> list[EmergencyTicket]: ...
