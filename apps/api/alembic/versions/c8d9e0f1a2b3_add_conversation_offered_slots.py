"""add conversation_offered_slots

Records which appointment slots were actually read out to a caller, so
`book_appointment` can refuse a time that was never offered.

Additive only: one new table, no change to any existing table, no data
migration. Dropping it restores the previous behaviour exactly.

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
Create Date: 2026-08-23 03:10:00.000000

"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c8d9e0f1a2b3'
down_revision: Union[str, None] = 'b7c8d9e0f1a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'conversation_offered_slots',
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('conversation_id', sa.UUID(), nullable=False),
        sa.Column('slot_start_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('duration_minutes', sa.Integer(), nullable=False),
        sa.Column(
            'offered_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        # Named explicitly because the repository's ON CONFLICT clause
        # targets it by name.
        sa.UniqueConstraint(
            'conversation_id', 'slot_start_at', name='uq_offered_slot_conversation_start'
        ),
    )
    op.create_index(
        op.f('ix_conversation_offered_slots_conversation_id'),
        'conversation_offered_slots',
        ['conversation_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_conversation_offered_slots_organization_id'),
        'conversation_offered_slots',
        ['organization_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f('ix_conversation_offered_slots_organization_id'),
        table_name='conversation_offered_slots',
    )
    op.drop_index(
        op.f('ix_conversation_offered_slots_conversation_id'),
        table_name='conversation_offered_slots',
    )
    op.drop_table('conversation_offered_slots')
