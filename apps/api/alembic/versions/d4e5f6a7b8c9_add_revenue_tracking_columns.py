"""add revenue tracking columns

Revision ID: d4e5f6a7b8c9
Revises: c9d0e1f2a3b4
Create Date: 2026-07-26 12:00:00.000000

Milestone 8 (Analytics) additive columns, bundled into one migration since
they land together as one feature (same precedent as
`a7b8c9d0e1f2_add_customer_id_to_tickets_and_appointments.py`, which
touched two tables in one revision):

- `services.default_price` — mirrors `services.default_duration_minutes`
  (`bac7205a4017_add_service_default_duration_minutes.py`): an optional
  default staff can set per service.
- `emergency_tickets.actual_value` / `appointments.actual_value` — the
  dollar value staff optionally capture when closing a ticket as RESOLVED
  or an appointment as COMPLETED. This is what lets Analytics compute a
  real "revenue recovered" number.

All three are nullable — nothing back-fills them, since there is no
historical monetary data to reconstruct.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, None] = 'c9d0e1f2a3b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('services', sa.Column('default_price', sa.Numeric(10, 2), nullable=True))
    op.add_column(
        'emergency_tickets', sa.Column('actual_value', sa.Numeric(10, 2), nullable=True)
    )
    op.add_column('appointments', sa.Column('actual_value', sa.Numeric(10, 2), nullable=True))


def downgrade() -> None:
    op.drop_column('appointments', 'actual_value')
    op.drop_column('emergency_tickets', 'actual_value')
    op.drop_column('services', 'default_price')
