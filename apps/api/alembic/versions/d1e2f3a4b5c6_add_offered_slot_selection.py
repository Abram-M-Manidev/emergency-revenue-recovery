"""add caller-selection state to conversation_offered_slots

Completes the appointment-consent invariant. `c8d9e0f1a2b3` recorded which
slots a caller was *offered*, which stopped the model booking a time nobody
was read. It did not stop the model booking a time the caller was read but
never *chose* — every offered slot passed that check, so an assistant that
offered three times and immediately booked one was approved.

These columns record the choice, and the conversation turn it happened in.
A caller cannot answer an offer they have not heard, so a selection counts
only from a strictly later turn than the offer — the deterministic half that
no prompt rule and no tool argument can supply.

Additive only: three nullable/defaulted columns and one partial unique index
on an existing table. No data migration, no change to any other table.
Downgrade restores the previous behaviour exactly.

Revision ID: d1e2f3a4b5c6
Revises: c8d9e0f1a2b3
Create Date: 2026-09-10 04:00:00.000000

"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd1e2f3a4b5c6'
down_revision: Union[str, None] = 'c8d9e0f1a2b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default so existing rows (dev-database offers from before this
    # migration) get a real value rather than blocking the NOT NULL. Index 0
    # is the safe reading for them: it means "offered on the first turn", so
    # a later selection against one of those rows is still legitimate, and an
    # attempt to select within turn 0 is still refused.
    op.add_column(
        'conversation_offered_slots',
        sa.Column(
            'offered_turn_index',
            sa.Integer(),
            nullable=False,
            server_default='0',
        ),
    )
    op.add_column(
        'conversation_offered_slots',
        sa.Column('selected_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'conversation_offered_slots',
        sa.Column('selected_turn_index', sa.Integer(), nullable=True),
    )
    # One live selection per conversation, enforced by the database. The
    # repository clears before it sets, but two overlapping turns on one call
    # could otherwise interleave and leave two selected rows — from which
    # "which slot did the caller choose?" has no single answer.
    op.create_index(
        'uq_offered_slot_one_active_selection',
        'conversation_offered_slots',
        ['conversation_id'],
        unique=True,
        postgresql_where=sa.text('selected_at IS NOT NULL'),
    )


def downgrade() -> None:
    op.drop_index(
        'uq_offered_slot_one_active_selection',
        table_name='conversation_offered_slots',
    )
    op.drop_column('conversation_offered_slots', 'selected_turn_index')
    op.drop_column('conversation_offered_slots', 'selected_at')
    op.drop_column('conversation_offered_slots', 'offered_turn_index')
