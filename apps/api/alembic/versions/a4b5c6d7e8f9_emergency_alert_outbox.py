"""turn emergency notification deliveries into a transactional outbox

Emergency alerts used to be sent from inside the live voice turn, before the
request's transaction committed. If anything later in the turn failed — or
the commit itself did — the ticket rolled back while the dispatcher had
already been paged about it.

`emergency_notification_deliveries` already held one row per ticket with its
status and attempt count. It now also records WHEN the row is next due to be
sent. The row is written in the same transaction as the ticket; the send
happens only after commit, from a worker that locks due rows with
`FOR UPDATE SKIP LOCKED`.

Existing rows are deliberately NOT made due. Any row still `pending` from
before this migration belongs to an old ticket whose in-request send was
interrupted; paging a dispatcher about it now, possibly days later, would be
worse than leaving it for an operator to see in the dispatch queue.

Additive and reversible: one nullable column and one partial index.

Revision ID: a4b5c6d7e8f9
Revises: f3a4b5c6d7e8
Create Date: 2026-09-25 10:00:00.000000

"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a4b5c6d7e8f9'
down_revision: Union[str, None] = 'f3a4b5c6d7e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'emergency_notification_deliveries',
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
    )
    # Only rows that still have work to do carry a due time, so the worker's
    # scan touches a handful of rows however long the delivery history grows.
    op.create_index(
        'ix_emergency_notification_deliveries_due',
        'emergency_notification_deliveries',
        ['next_attempt_at'],
        postgresql_where=sa.text('next_attempt_at IS NOT NULL'),
    )


def downgrade() -> None:
    op.drop_index(
        'ix_emergency_notification_deliveries_due',
        table_name='emergency_notification_deliveries',
    )
    op.drop_column('emergency_notification_deliveries', 'next_attempt_at')
