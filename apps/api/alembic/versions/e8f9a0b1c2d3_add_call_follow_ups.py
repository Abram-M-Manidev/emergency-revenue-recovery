"""add call follow-ups: calls that ended without their request being recorded

- `call_follow_ups` — at most one row per conversation, created by fixed
  rules when the AI provider failed, the caller hung up before the request
  was recorded, or the call was transferred with nothing recorded. Points at
  the emergency ticket opened for it when the caller's words described a
  possible emergency. See `app/domain/follow_ups.py`.

Additive only: one new table, no existing row changed. `call_transfer_attempts.
reason` gains the value SYSTEM_UNAVAILABLE, which needs no DDL — it is a
non-native enum (VARCHAR(30)) with no CHECK constraint. Downgrade drops the
table; tickets opened through it are ordinary tickets and stay.

Revision ID: e8f9a0b1c2d3
Revises: d7e8f9a0b1c2
Create Date: 2026-10-28 12:00:00.000000

"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = 'e8f9a0b1c2d3'
down_revision: Union[str, None] = 'd7e8f9a0b1c2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'call_follow_ups',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('conversation_id', sa.UUID(), nullable=False),
        # Non-native enum: stores the member NAME (e.g. 'AI_UNAVAILABLE'),
        # like every other enum column in this schema.
        sa.Column('reason', sa.Enum(
            'AI_UNAVAILABLE', 'ENDED_BEFORE_REQUEST_RECORDED',
            'TRANSFERRED_BEFORE_REQUEST_RECORDED',
            name='call_follow_up_reason', native_enum=False, length=40), nullable=False),
        sa.Column('possible_emergency', sa.Boolean(), nullable=False),
        sa.Column('emergency_ticket_id', sa.UUID(), nullable=True),
        sa.Column('caller_number', sa.String(length=32), nullable=True),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolved_by_user_id', sa.UUID(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['emergency_ticket_id'], ['emergency_tickets.id'],
                                ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['resolved_by_user_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('conversation_id'),
    )
    op.create_index(op.f('ix_call_follow_ups_organization_id'), 'call_follow_ups',
                    ['organization_id'], unique=False)
    op.create_index('ix_call_follow_ups_organization_resolved', 'call_follow_ups',
                    ['organization_id', 'resolved_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_call_follow_ups_organization_resolved', table_name='call_follow_ups')
    op.drop_index(op.f('ix_call_follow_ups_organization_id'), table_name='call_follow_ups')
    op.drop_table('call_follow_ups')
