"""record service-address corrections that need the caller's confirmation

`contact_correction_requests`: one row per proposed change to a contact
field on a request that already exists. A proposal is recorded on the turn
the assistant reads the new value back, and may only be committed on a
later turn whose read-back reply reached the caller. See
`app/domain/repositories/contact_correction_repository.py`.

Additive: a new table only. Downgrade drops it, and with it the audit trail
of proposed and confirmed corrections.

Revision ID: 2c3d4e5f6a7b
Revises: 1b2c3d4e5f6a
Create Date: 2026-11-08 12:00:00.000000

"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = '2c3d4e5f6a7b'
down_revision: Union[str, None] = '1b2c3d4e5f6a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "contact_correction_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("field", sa.String(length=32), nullable=False),
        sa.Column("proposed_value", sa.String(length=500), nullable=False),
        sa.Column("proposed_turn_index", sa.Integer(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_contact_correction_requests_organization_id"),
        "contact_correction_requests",
        ["organization_id"],
    )
    op.create_index(
        op.f("ix_contact_correction_requests_conversation_id"),
        "contact_correction_requests",
        ["conversation_id"],
    )
    op.create_index(
        "uq_contact_correction_one_pending",
        "contact_correction_requests",
        ["conversation_id", "field"],
        unique=True,
        postgresql_where=sa.text("confirmed_at IS NULL AND superseded_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_contact_correction_one_pending", table_name="contact_correction_requests")
    op.drop_index(
        op.f("ix_contact_correction_requests_conversation_id"),
        table_name="contact_correction_requests",
    )
    op.drop_index(
        op.f("ix_contact_correction_requests_organization_id"),
        table_name="contact_correction_requests",
    )
    op.drop_table("contact_correction_requests")
