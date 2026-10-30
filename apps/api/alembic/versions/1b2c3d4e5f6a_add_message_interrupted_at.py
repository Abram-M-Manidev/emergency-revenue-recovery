"""record turns whose reply never reached the caller, and soft-withdraw their offers

When Vapi drops a Custom-LLM stream before its final `[DONE]` frame (the
caller kept talking, so Vapi re-sends their words, grown, in a newer
request; or they hung up), ERRS still finishes the turn, because its
tickets and alerts are real. But the reply was not delivered. `[DONE]` is a
transport signal, not proof of playback. See
`VoiceService.mark_turn_interrupted` and `app/domain/turn_delivery.py`.

- `conversation_messages.interrupted_at`: set on both messages of such a
  turn. The reply is left out of the model's history. The caller message
  stays, unless the caller's next message repeats it.
- `conversation_offered_slots.withdrawn_at` / `withdrawn_message_id`:
  appointment times read out in such a turn are withdrawn rather than
  deleted. Each is kept, linked to the reply that was not delivered, and
  can never be selected or booked.
- The unique (conversation_id, slot_start_at) constraint becomes a partial
  unique index over active (non-withdrawn) offers, so a withdrawn offer stays
  on record while the same time is offered again.

Upgrade is additive for existing rows: every one stays NULL / active, which
is how they were treated before.

Downgrade restores the full unique constraint, which withdrawn rows could
violate. So it first DELETES withdrawn offers (audit history is lost) and
then drops the new columns.

Revision ID: 1b2c3d4e5f6a
Revises: 0a1b2c3d4e5f
Create Date: 2026-10-31 12:00:00.000000

"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = '1b2c3d4e5f6a'
down_revision: Union[str, None] = '0a1b2c3d4e5f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('conversation_messages',
                  sa.Column('interrupted_at', sa.DateTime(timezone=True), nullable=True))

    op.add_column('conversation_offered_slots',
                  sa.Column('withdrawn_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('conversation_offered_slots',
                  sa.Column('withdrawn_message_id', sa.UUID(), nullable=True))
    op.create_foreign_key(
        'fk_conversation_offered_slots_withdrawn_message_id',
        'conversation_offered_slots', 'conversation_messages',
        ['withdrawn_message_id'], ['id'], ondelete='SET NULL',
    )
    op.drop_constraint('uq_offered_slot_conversation_start', 'conversation_offered_slots',
                       type_='unique')
    op.create_index('uq_offered_slot_conversation_start_active', 'conversation_offered_slots',
                    ['conversation_id', 'slot_start_at'], unique=True,
                    postgresql_where=sa.text('withdrawn_at IS NULL'))


def downgrade() -> None:
    op.execute('DELETE FROM conversation_offered_slots WHERE withdrawn_at IS NOT NULL')
    op.drop_index('uq_offered_slot_conversation_start_active',
                  table_name='conversation_offered_slots',
                  postgresql_where=sa.text('withdrawn_at IS NULL'))
    op.create_unique_constraint('uq_offered_slot_conversation_start', 'conversation_offered_slots',
                                ['conversation_id', 'slot_start_at'])
    op.drop_constraint('fk_conversation_offered_slots_withdrawn_message_id',
                       'conversation_offered_slots', type_='foreignkey')
    op.drop_column('conversation_offered_slots', 'withdrawn_message_id')
    op.drop_column('conversation_offered_slots', 'withdrawn_at')

    op.drop_column('conversation_messages', 'interrupted_at')
