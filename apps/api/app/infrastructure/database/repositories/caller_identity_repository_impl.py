from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities.customer import Customer
from app.domain.repositories.caller_identity_repository import CallerIdentityRepository
from app.infrastructure.database.models.customer import CustomerModel
from app.infrastructure.database.models.customer_caller_identity import (
    CustomerCallerIdentityModel,
)


def _to_entity(model: CustomerModel) -> Customer:
    return Customer(
        id=model.id,
        organization_id=model.organization_id,
        full_name=model.full_name,
        phone_number=model.phone_number,
        email=model.email,
        address=model.address,
        notes=model.notes,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class SqlAlchemyCallerIdentityRepository(CallerIdentityRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_customers_by_caller_number(
        self, organization_id: uuid.UUID, caller_number: str
    ) -> list[Customer]:
        if not caller_number or not caller_number.strip():
            # A missing or blank caller ID is not an error — most channels
            # simply don't have one (the text/simulation path never does).
            # Short-circuited here so no query is issued at all.
            return []

        result = await self._session.execute(
            select(CustomerModel)
            .join(
                CustomerCallerIdentityModel,
                CustomerCallerIdentityModel.customer_id == CustomerModel.id,
            )
            # `organization_id` is constrained on the association *and* on
            # the customer. Either alone would be sufficient given the
            # write path, but asserting both means a mis-scoped association
            # row could still never surface another tenant's customer.
            .where(
                CustomerCallerIdentityModel.organization_id == organization_id,
                CustomerModel.organization_id == organization_id,
                CustomerCallerIdentityModel.caller_number == caller_number.strip(),
            )
            .order_by(CustomerCallerIdentityModel.last_seen_at.desc())
        )
        return [_to_entity(model) for model in result.scalars().all()]

    async def associate(
        self, organization_id: uuid.UUID, *, customer_id: uuid.UUID, caller_number: str
    ) -> None:
        if not caller_number or not caller_number.strip():
            return

        # ON CONFLICT DO UPDATE against the natural key, so a repeat turn on
        # the same call is a `last_seen_at` refresh rather than a duplicate
        # row or an IntegrityError. Done in one statement specifically to
        # avoid a read-then-write race between two concurrent turns of the
        # same call — P1 serialises those today, but this write must not
        # depend on that to stay correct.
        statement = pg_insert(CustomerCallerIdentityModel).values(
            organization_id=organization_id,
            customer_id=customer_id,
            caller_number=caller_number.strip(),
        )
        await self._session.execute(
            statement.on_conflict_do_update(
                constraint="uq_caller_identity_org_number_customer",
                set_={"last_seen_at": func.now()},
            )
        )
