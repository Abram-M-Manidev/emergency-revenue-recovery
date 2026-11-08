from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import ColumnElement, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.repositories.contact_correction_repository import (
    ContactCorrectionRepository,
    ContactCorrectionRequest,
)
from app.infrastructure.database.models.contact_correction import (
    ContactCorrectionRequestModel,
)


def _to_entity(model: ContactCorrectionRequestModel) -> ContactCorrectionRequest:
    return ContactCorrectionRequest(
        id=model.id,
        organization_id=model.organization_id,
        conversation_id=model.conversation_id,
        field=model.field,
        proposed_value=model.proposed_value,
        proposed_turn_index=model.proposed_turn_index,
        confirmed_at=model.confirmed_at,
        superseded_at=model.superseded_at,
        created_at=model.created_at,
    )


def _pending(
    organization_id: uuid.UUID, conversation_id: uuid.UUID, field: str
) -> tuple[ColumnElement[bool], ...]:
    return (
        ContactCorrectionRequestModel.organization_id == organization_id,
        ContactCorrectionRequestModel.conversation_id == conversation_id,
        ContactCorrectionRequestModel.field == field,
        ContactCorrectionRequestModel.confirmed_at.is_(None),
        ContactCorrectionRequestModel.superseded_at.is_(None),
    )


class SqlAlchemyContactCorrectionRepository(ContactCorrectionRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def propose(
        self,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        *,
        field: str,
        proposed_value: str,
        turn_index: int,
    ) -> ContactCorrectionRequest:
        await self._session.execute(
            update(ContactCorrectionRequestModel)
            .where(*_pending(organization_id, conversation_id, field))
            .values(superseded_at=datetime.now(timezone.utc))
        )
        model = ContactCorrectionRequestModel(
            organization_id=organization_id,
            conversation_id=conversation_id,
            field=field,
            proposed_value=proposed_value,
            proposed_turn_index=turn_index,
        )
        self._session.add(model)
        await self._session.flush()
        await self._session.refresh(model)
        return _to_entity(model)

    async def get_pending(
        self, organization_id: uuid.UUID, conversation_id: uuid.UUID, *, field: str
    ) -> ContactCorrectionRequest | None:
        result = await self._session.execute(
            select(ContactCorrectionRequestModel).where(
                *_pending(organization_id, conversation_id, field)
            )
        )
        model = result.scalar_one_or_none()
        return _to_entity(model) if model is not None else None

    async def mark_confirmed(
        self, organization_id: uuid.UUID, request_id: uuid.UUID, *, at: datetime
    ) -> None:
        await self._session.execute(
            update(ContactCorrectionRequestModel)
            .where(
                ContactCorrectionRequestModel.id == request_id,
                ContactCorrectionRequestModel.organization_id == organization_id,
            )
            .values(confirmed_at=at)
        )
