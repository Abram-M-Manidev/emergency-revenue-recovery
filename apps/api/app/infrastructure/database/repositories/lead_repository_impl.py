from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.repositories.lead_repository import Lead, LeadRepository
from app.infrastructure.database.models.lead import MarketingLeadModel


def _to_entity(model: MarketingLeadModel) -> Lead:
    return Lead(
        id=model.id,
        name=model.name,
        business_name=model.business_name,
        email=model.email,
        business_type=model.business_type,
        consent_version=model.consent_version,
        consented_at=model.consented_at,
        request_count=model.request_count,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class SqlAlchemyLeadRepository(LeadRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        name: str,
        business_name: str,
        email: str,
        business_type: str,
        consent_version: str,
        consented_at: datetime,
    ) -> Lead:
        # One statement, so two simultaneous submissions of the same address
        # cannot race into a unique-constraint failure.
        statement = (
            insert(MarketingLeadModel)
            .values(
                name=name,
                business_name=business_name,
                email=email,
                business_type=business_type,
                consent_version=consent_version,
                consented_at=consented_at,
                request_count=1,
            )
            .on_conflict_do_update(
                index_elements=[MarketingLeadModel.email],
                set_={
                    "name": name,
                    "business_name": business_name,
                    "business_type": business_type,
                    "consent_version": consent_version,
                    "consented_at": consented_at,
                    "request_count": MarketingLeadModel.request_count + 1,
                },
            )
            .returning(MarketingLeadModel)
        )
        result = await self._session.execute(statement)
        model = result.scalar_one()
        await self._session.flush()
        return _to_entity(model)

    async def list_all(self, *, limit: int) -> list[Lead]:
        result = await self._session.execute(
            select(MarketingLeadModel).order_by(MarketingLeadModel.created_at.desc()).limit(limit)
        )
        return [_to_entity(model) for model in result.scalars().all()]
