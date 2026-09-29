"""add human fallback: call transfer settings and attempts

A caller must always have a human exit. Two tables, deliberately separate
(the same split as emergency notifications):

- `organization_call_transfer_settings` — where one tenant's calls may be
  handed to a person: the office during business hours, the on-call line
  after hours, and whether emergencies go straight to a human once their
  ticket is recorded. Its own table rather than columns on
  `business_profiles`, because that profile is assembled into the LLM prompt
  on every turn and an on-call number is often a technician's personal
  mobile.
- `call_transfer_attempts` — every attempt and the state it ended in
  (requested / destination_resolved / initiated / unavailable / failed). The
  audit trail of what a caller was offered, and what lets a second request
  in the same call see that the call is already moving.

Additive only: two new tables, no change to any existing table, no data
migration. Downgrade drops them and the assistant simply has no transfer
tool behaviour to rely on (the tool reports "unavailable").

Enums are VARCHAR (`native_enum=False`), like every enum in this schema.

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-09-29 12:00:00.000000

"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = 'b5c6d7e8f9a0'
down_revision: Union[str, None] = 'a4b5c6d7e8f9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        'organization_call_transfer_settings',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('business_hours_number', sa.String(length=32), nullable=True),
        sa.Column('after_hours_number', sa.String(length=32), nullable=True),
        sa.Column('transfer_emergencies', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('is_enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        *_timestamps(),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_organization_call_transfer_settings_organization_id'),
        'organization_call_transfer_settings', ['organization_id'], unique=True,
    )

    op.create_table(
        'call_transfer_attempts',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('conversation_id', sa.UUID(), nullable=False),
        sa.Column('status', sa.Enum(
            'REQUESTED', 'DESTINATION_RESOLVED', 'INITIATED', 'UNAVAILABLE', 'FAILED',
            name='call_transfer_status', native_enum=False, length=30), nullable=False),
        sa.Column('reason', sa.Enum(
            'CALLER_REQUESTED', 'CALLER_FRUSTRATED', 'OUT_OF_SCOPE', 'EMERGENCY_POLICY',
            name='call_transfer_reason', native_enum=False, length=30), nullable=False),
        sa.Column('is_emergency', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('destination_kind', sa.Enum(
            'BUSINESS_HOURS', 'AFTER_HOURS',
            name='call_transfer_destination_kind', native_enum=False, length=20), nullable=True),
        sa.Column('destination_number', sa.String(length=32), nullable=True),
        sa.Column('error_code', sa.String(length=50), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_call_transfer_attempts_organization_id'),
        'call_transfer_attempts', ['organization_id'], unique=False,
    )
    op.create_index(
        'ix_call_transfer_attempts_conversation_status',
        'call_transfer_attempts', ['conversation_id', 'status'], unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_call_transfer_attempts_conversation_status',
                  table_name='call_transfer_attempts')
    op.drop_index(op.f('ix_call_transfer_attempts_organization_id'),
                  table_name='call_transfer_attempts')
    op.drop_table('call_transfer_attempts')
    op.drop_index(op.f('ix_organization_call_transfer_settings_organization_id'),
                  table_name='organization_call_transfer_settings')
    op.drop_table('organization_call_transfer_settings')
