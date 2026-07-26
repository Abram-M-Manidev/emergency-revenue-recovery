"""add customer_id to tickets and appointments

Revision ID: a7b8c9d0e1f2
Revises: f1a2b3c4d5e6
Create Date: 2026-07-26 09:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7b8c9d0e1f2'
down_revision: Union[str, None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('emergency_tickets', sa.Column('customer_id', sa.UUID(), nullable=True))
    op.create_foreign_key(
        'fk_emergency_tickets_customer_id_customers',
        'emergency_tickets', 'customers', ['customer_id'], ['id'], ondelete='SET NULL',
    )
    op.add_column('appointments', sa.Column('customer_id', sa.UUID(), nullable=True))
    op.create_foreign_key(
        'fk_appointments_customer_id_customers',
        'appointments', 'customers', ['customer_id'], ['id'], ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint(
        'fk_appointments_customer_id_customers', 'appointments', type_='foreignkey'
    )
    op.drop_column('appointments', 'customer_id')
    op.drop_constraint(
        'fk_emergency_tickets_customer_id_customers', 'emergency_tickets', type_='foreignkey'
    )
    op.drop_column('emergency_tickets', 'customer_id')
