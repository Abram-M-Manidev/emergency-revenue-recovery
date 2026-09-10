"""add per-tenant voice assistant kill switch

An operator needs to be able to stop one business's phone assistant without
stopping everyone's. Until now the only lever was `AI_TOOLS_ENABLED`, a
process-wide setting that would have disabled tools for every tenant in the
deployment, and `organizations.is_active`, which disables the whole account
including the dashboard the operator would use to investigate.

`voice_assistant_enabled` is neither: it stops inbound calls being answered
for one organization while that organization keeps working its dispatch
queue, its appointments, and its customer records.

Additive and reversible: one boolean column, `server_default true` so every
existing organization keeps the behaviour it has today. Switching the
assistant off is always an explicit act.

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-09-10 11:15:00.000000

"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f3a4b5c6d7e8'
down_revision: Union[str, None] = 'e2f3a4b5c6d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'organizations',
        sa.Column(
            'voice_assistant_enabled',
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    op.drop_column('organizations', 'voice_assistant_enabled')
