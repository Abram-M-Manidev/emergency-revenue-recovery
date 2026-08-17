"""P5 against a real Postgres: the association table, its constraints, and
tenant isolation enforced by the query rather than by filtering afterwards.

The unit tests prove the grounding *decisions*; these prove the storage
those decisions rest on — that a shared line really can hold two customers,
that the same pair cannot be inserted twice, and that an organization
boundary is genuinely impassable.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.infrastructure.database.models import *  # noqa: F401,F403
from app.infrastructure.database.models.organization import OrganizationModel
from app.infrastructure.database.repositories import (
    SqlAlchemyCallerIdentityRepository,
    SqlAlchemyCustomerRepository,
)
from app.infrastructure.database.session import AsyncSessionLocal, Base, engine

_CALLER = "+919999999999"


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def database_ready():
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Database not reachable, skipping integration test: {exc}")
    yield
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))
        await conn.run_sync(Base.metadata.drop_all)


async def _org() -> uuid.UUID:
    org_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            OrganizationModel(
                id=org_id, name=f"P5 Org {org_id.hex[:8]}", slug=f"p5-{org_id.hex[:8]}"
            )
        )
        await session.commit()
    return org_id


async def _customer(org_id: uuid.UUID, *, name: str, phone: str, address: str | None = None):
    async with AsyncSessionLocal() as session:
        customer = await SqlAlchemyCustomerRepository(session).create(
            organization_id=org_id, full_name=name, phone_number=phone, address=address
        )
        await session.commit()
        return customer


@pytest.mark.asyncio(loop_scope="session")
async def test_associate_then_resolve_round_trip(database_ready):
    org_id = await _org()
    customer = await _customer(org_id, name="Lucky", phone="123456789", address="16 Street")

    async with AsyncSessionLocal() as session:
        repo = SqlAlchemyCallerIdentityRepository(session)
        await repo.associate(org_id, customer_id=customer.id, caller_number=_CALLER)
        await session.commit()

    async with AsyncSessionLocal() as session:
        found = await SqlAlchemyCallerIdentityRepository(
            session
        ).find_customers_by_caller_number(org_id, _CALLER)

    assert [c.id for c in found] == [customer.id]
    assert found[0].full_name == "Lucky"


@pytest.mark.asyncio(loop_scope="session")
async def test_associate_is_idempotent_at_the_database_level(database_ready):
    """ON CONFLICT DO UPDATE, so repeated turns of one call refresh
    `last_seen_at` instead of duplicating or raising."""
    org_id = await _org()
    customer = await _customer(org_id, name="Lucky", phone="123456789")

    async with AsyncSessionLocal() as session:
        repo = SqlAlchemyCallerIdentityRepository(session)
        for _ in range(5):
            await repo.associate(org_id, customer_id=customer.id, caller_number=_CALLER)
        await session.commit()

    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            text(
                "SELECT count(*), min(first_seen_at) = max(first_seen_at) "
                "FROM customer_caller_identities WHERE organization_id = :org"
            ),
            {"org": org_id},
        )
        count, first_seen_stable = rows.one()

    assert count == 1
    assert first_seen_stable is True, "first_seen_at must not be rewritten by a repeat"


@pytest.mark.asyncio(loop_scope="session")
async def test_one_line_can_belong_to_several_customers(database_ready):
    """A household or office line. The unique constraint covers the whole
    triple precisely so this is representable."""
    org_id = await _org()
    alice = await _customer(org_id, name="Alice", phone="111")
    bob = await _customer(org_id, name="Bob", phone="222")

    async with AsyncSessionLocal() as session:
        repo = SqlAlchemyCallerIdentityRepository(session)
        for customer in (alice, bob):
            await repo.associate(org_id, customer_id=customer.id, caller_number=_CALLER)
        await session.commit()

    async with AsyncSessionLocal() as session:
        found = await SqlAlchemyCallerIdentityRepository(
            session
        ).find_customers_by_caller_number(org_id, _CALLER)

    assert {c.id for c in found} == {alice.id, bob.id}


@pytest.mark.asyncio(loop_scope="session")
async def test_one_customer_can_have_several_lines(database_ready):
    org_id = await _org()
    customer = await _customer(org_id, name="Lucky", phone="123456789")

    async with AsyncSessionLocal() as session:
        repo = SqlAlchemyCallerIdentityRepository(session)
        for number in ("+919999999999", "+919000000001"):
            await repo.associate(org_id, customer_id=customer.id, caller_number=number)
        await session.commit()

    async with AsyncSessionLocal() as session:
        repo = SqlAlchemyCallerIdentityRepository(session)
        for number in ("+919999999999", "+919000000001"):
            found = await repo.find_customers_by_caller_number(org_id, number)
            assert [c.id for c in found] == [customer.id]


@pytest.mark.asyncio(loop_scope="session")
async def test_tenant_isolation_is_enforced_in_the_query(database_ready):
    """The same caller ID in two organizations must never cross over."""
    org_a = await _org()
    org_b = await _org()
    a_customer = await _customer(org_a, name="Org A Customer", phone="111")
    b_customer = await _customer(org_b, name="Org B Customer", phone="111")

    async with AsyncSessionLocal() as session:
        repo = SqlAlchemyCallerIdentityRepository(session)
        await repo.associate(org_a, customer_id=a_customer.id, caller_number=_CALLER)
        await repo.associate(org_b, customer_id=b_customer.id, caller_number=_CALLER)
        await session.commit()

    async with AsyncSessionLocal() as session:
        repo = SqlAlchemyCallerIdentityRepository(session)
        from_a = await repo.find_customers_by_caller_number(org_a, _CALLER)
        from_b = await repo.find_customers_by_caller_number(org_b, _CALLER)

    assert [c.id for c in from_a] == [a_customer.id]
    assert [c.id for c in from_b] == [b_customer.id]


@pytest.mark.asyncio(loop_scope="session")
async def test_blank_caller_number_issues_no_query_and_writes_nothing(database_ready):
    org_id = await _org()
    customer = await _customer(org_id, name="Lucky", phone="123456789")

    async with AsyncSessionLocal() as session:
        repo = SqlAlchemyCallerIdentityRepository(session)
        for blank in ("", "   "):
            await repo.associate(org_id, customer_id=customer.id, caller_number=blank)
            assert await repo.find_customers_by_caller_number(org_id, blank) == []
        await session.commit()

    async with AsyncSessionLocal() as session:
        count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM customer_caller_identities "
                    "WHERE organization_id = :org"
                ),
                {"org": org_id},
            )
        ).scalar_one()

    assert count == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_deleting_a_customer_removes_its_associations(database_ready):
    """ON DELETE CASCADE — an association to a deleted customer would
    otherwise linger as an unresolvable row."""
    org_id = await _org()
    customer = await _customer(org_id, name="Temporary", phone="999")

    async with AsyncSessionLocal() as session:
        await SqlAlchemyCallerIdentityRepository(session).associate(
            org_id, customer_id=customer.id, caller_number=_CALLER
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("DELETE FROM customers WHERE id = :cid"), {"cid": customer.id}
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        found = await SqlAlchemyCallerIdentityRepository(
            session
        ).find_customers_by_caller_number(org_id, _CALLER)

    assert found == []


@pytest.mark.asyncio(loop_scope="session")
async def test_caller_id_does_not_match_a_customer_phone_number(database_ready):
    """The distinction P5 exists to preserve: caller ID is the line dialled
    from, `customers.phone_number` is the callback number the caller
    stated. A customer whose stated number happens to equal a caller ID is
    still not resolvable without an explicit association."""
    org_id = await _org()
    await _customer(org_id, name="Coincidence", phone=_CALLER)

    async with AsyncSessionLocal() as session:
        found = await SqlAlchemyCallerIdentityRepository(
            session
        ).find_customers_by_caller_number(org_id, _CALLER)

    assert found == []
