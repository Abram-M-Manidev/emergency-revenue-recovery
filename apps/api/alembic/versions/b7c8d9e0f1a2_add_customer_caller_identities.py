"""add customer_caller_identities and backfill from call history

Revision ID: b7c8d9e0f1a2
Revises: a2b3c4d5e6f7
Create Date: 2026-08-17 15:30:00.000000

P5 (Known-Caller Grounding). The first real PSTN call proved that a
telephony caller ID cannot be matched against `customers.phone_number`:
the caller ID was `+91…` while the stored number was `123456789`, the
digits the caller had dictated. Those are different facts — the line a
call arrives from versus the callback number a caller supplies — and
`customers.phone_number` is C1's deduplication key, so it cannot be
repurposed.

This adds the association explicitly rather than inferring it. Purely
additive: no existing table is altered, no column added to `customers`,
no existing row modified.

The unique constraint covers the whole triple. A two-column constraint on
`(organization_id, caller_number)` would forbid a shared household or
office line from belonging to more than one customer, silently awarding
it to whoever was recorded first — which would make the assistant greet
the wrong person. Ambiguity is stored honestly and resolved in the
application by declining to ground.

The backfill reconstructs associations from the relationship that already
exists: a voice call carries a caller number, and the ticket or
appointment born from that call carries a customer id. It is deliberately
conservative — only calls that produced one of those two artifacts can be
reconstructed, so a customer with no telephony history is left
unassociated rather than invented. Idempotent via ON CONFLICT, so a
re-run is a no-op.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

# revision identifiers, used by Alembic.
revision: str = 'b7c8d9e0f1a2'
down_revision: Union[str, None] = 'a2b3c4d5e6f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Both halves of the union are org-scoped through `voice_calls`, and the
# customer's own organization is asserted again in the join so a
# mis-scoped historical row could not create a cross-tenant association.
_BACKFILL = """
INSERT INTO customer_caller_identities
    (id, organization_id, customer_id, caller_number,
     first_seen_at, last_seen_at, created_at, updated_at)
SELECT gen_random_uuid(), src.organization_id, src.customer_id, src.caller_number,
       src.first_seen_at, src.last_seen_at, now(), now()
FROM (
    SELECT vc.organization_id,
           t.customer_id,
           vc.caller_number,
           min(vc.started_at) AS first_seen_at,
           max(vc.started_at) AS last_seen_at
    FROM voice_calls vc
    JOIN emergency_tickets t ON t.conversation_id = vc.conversation_id
    JOIN customers c ON c.id = t.customer_id
                    AND c.organization_id = vc.organization_id
    WHERE vc.caller_number IS NOT NULL
      AND btrim(vc.caller_number) <> ''
      AND t.customer_id IS NOT NULL
    GROUP BY vc.organization_id, t.customer_id, vc.caller_number

    UNION ALL

    SELECT vc.organization_id,
           a.customer_id,
           vc.caller_number,
           min(vc.started_at) AS first_seen_at,
           max(vc.started_at) AS last_seen_at
    FROM voice_calls vc
    JOIN appointments a ON a.conversation_id = vc.conversation_id
    JOIN customers c ON c.id = a.customer_id
                    AND c.organization_id = vc.organization_id
    WHERE vc.caller_number IS NOT NULL
      AND btrim(vc.caller_number) <> ''
      AND a.customer_id IS NOT NULL
    GROUP BY vc.organization_id, a.customer_id, vc.caller_number
) AS src
ON CONFLICT ON CONSTRAINT uq_caller_identity_org_number_customer DO NOTHING
"""


def upgrade() -> None:
    op.create_table(
        "customer_caller_identities",
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("customer_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("caller_number", sa.String(length=32), nullable=False),
        sa.Column(
            "first_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "organization_id",
            "caller_number",
            "customer_id",
            name="uq_caller_identity_org_number_customer",
        ),
    )
    op.create_index(
        "ix_customer_caller_identities_organization_id",
        "customer_caller_identities",
        ["organization_id"],
    )
    op.create_index(
        "ix_customer_caller_identities_customer_id",
        "customer_caller_identities",
        ["customer_id"],
    )
    op.create_index(
        "ix_caller_identities_org_number",
        "customer_caller_identities",
        ["organization_id", "caller_number"],
    )

    op.execute(_BACKFILL)


def downgrade() -> None:
    # Dropping the table removes the associations with it; nothing else was
    # modified, so this fully reverses the migration.
    op.drop_index("ix_caller_identities_org_number", table_name="customer_caller_identities")
    op.drop_index(
        "ix_customer_caller_identities_customer_id", table_name="customer_caller_identities"
    )
    op.drop_index(
        "ix_customer_caller_identities_organization_id", table_name="customer_caller_identities"
    )
    op.drop_table("customer_caller_identities")
