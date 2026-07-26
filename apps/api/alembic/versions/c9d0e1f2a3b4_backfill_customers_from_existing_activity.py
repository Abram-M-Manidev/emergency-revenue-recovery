"""backfill customers from existing activity

Revision ID: c9d0e1f2a3b4
Revises: b3c4d5e6f7a8
Create Date: 2026-07-26 09:15:00.000000

Before Milestone 7, `emergency_tickets` and `appointments` each carried
their own denormalized `customer_phone`/`customer_name`/`customer_address`
snapshot with no shared `Customer` record. This data migration
reconstructs one `Customer` per distinct `(organization_id,
customer_phone)` pair found across both tables (skipping rows with a null
phone — nothing to dedupe on), seeding `full_name`/`address` from
whichever matching row is oldest by `created_at`, then links every
matching ticket/appointment row's new `customer_id` column to it.

This is a one-time, best-effort reconstruction, not a perfect one: if the
name/address disagree across a caller's historical rows, the earliest one
wins arbitrarily. Idempotent (safe to re-run): both the customer lookup
and the `customer_id` backfill only touch rows that don't already have a
match.

Downgrade intentionally only *unlinks* (`customer_id = NULL`) rather than
deleting the reconstructed `customers` rows — by the time anyone runs this
downgrade in a real environment, ordinary usage may have added real
customer data (staff edits, new synced customers) on top of what this
migration created, and indiscriminately deleting could destroy that. The
`customers` table itself is fully removed if `add_customers_table` (this
migration's ancestor) is also downgraded.
"""
from __future__ import annotations

import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c9d0e1f2a3b4'
down_revision: Union[str, None] = 'b3c4d5e6f7a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    tickets_t = sa.table(
        "emergency_tickets",
        sa.column("organization_id", sa.UUID()),
        sa.column("customer_phone", sa.String()),
        sa.column("customer_name", sa.String()),
        sa.column("customer_address", sa.String()),
        sa.column("customer_id", sa.UUID()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    appointments_t = sa.table(
        "appointments",
        sa.column("organization_id", sa.UUID()),
        sa.column("customer_phone", sa.String()),
        sa.column("customer_name", sa.String()),
        sa.column("customer_address", sa.String()),
        sa.column("customer_id", sa.UUID()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    customers_t = sa.table(
        "customers",
        sa.column("id", sa.UUID()),
        sa.column("organization_id", sa.UUID()),
        sa.column("full_name", sa.String()),
        sa.column("phone_number", sa.String()),
        sa.column("address", sa.String()),
    )

    rows = list(
        conn.execute(
            sa.select(
                tickets_t.c.organization_id,
                tickets_t.c.customer_phone,
                tickets_t.c.customer_name,
                tickets_t.c.customer_address,
                tickets_t.c.created_at,
            ).where(tickets_t.c.customer_phone.isnot(None))
        ).fetchall()
    )
    rows.extend(
        conn.execute(
            sa.select(
                appointments_t.c.organization_id,
                appointments_t.c.customer_phone,
                appointments_t.c.customer_name,
                appointments_t.c.customer_address,
                appointments_t.c.created_at,
            ).where(appointments_t.c.customer_phone.isnot(None))
        ).fetchall()
    )

    # Reduce to the earliest-seen (name, address) per (org, phone) pair.
    earliest: dict[tuple, tuple] = {}
    for organization_id, phone, name, address, created_at in rows:
        key = (organization_id, phone)
        if key not in earliest or created_at < earliest[key][2]:
            earliest[key] = (name, address, created_at)

    customer_ids: dict[tuple, uuid.UUID] = {}
    for (organization_id, phone), (name, address, _created_at) in earliest.items():
        existing_id = conn.execute(
            sa.select(customers_t.c.id).where(
                customers_t.c.organization_id == organization_id,
                customers_t.c.phone_number == phone,
            )
        ).scalar_one_or_none()
        if existing_id is None:
            existing_id = uuid.uuid4()
            conn.execute(
                customers_t.insert().values(
                    id=existing_id,
                    organization_id=organization_id,
                    full_name=name,
                    phone_number=phone,
                    address=address,
                )
            )
        customer_ids[(organization_id, phone)] = existing_id

    for (organization_id, phone), customer_id in customer_ids.items():
        conn.execute(
            tickets_t.update()
            .where(
                tickets_t.c.organization_id == organization_id,
                tickets_t.c.customer_phone == phone,
                tickets_t.c.customer_id.is_(None),
            )
            .values(customer_id=customer_id)
        )
        conn.execute(
            appointments_t.update()
            .where(
                appointments_t.c.organization_id == organization_id,
                appointments_t.c.customer_phone == phone,
                appointments_t.c.customer_id.is_(None),
            )
            .values(customer_id=customer_id)
        )


def downgrade() -> None:
    conn = op.get_bind()

    tickets_t = sa.table("emergency_tickets", sa.column("customer_id", sa.UUID()))
    appointments_t = sa.table("appointments", sa.column("customer_id", sa.UUID()))

    conn.execute(tickets_t.update().values(customer_id=None))
    conn.execute(appointments_t.update().values(customer_id=None))
