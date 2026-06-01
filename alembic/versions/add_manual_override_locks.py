"""Add tp_manual_override and sl_manual_override flags to transaction

Revision ID: a1f7e9c4b023
Revises: cee392a8acc3
Create Date: 2026-06-01

When a user adjusts TP/SL manually, we want the value to stick — automated
sources (ruleset, smart_risk_manager, expert) must not silently re-overwrite
it on the next evaluation tick. These per-field flags express that intent.
The UI exposes a "revert" action that clears them.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1f7e9c4b023'
down_revision: Union[str, None] = 'cee392a8acc3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('transaction') as batch_op:
        batch_op.add_column(
            sa.Column('tp_manual_override', sa.Boolean(),
                       nullable=False, server_default=sa.false())
        )
        batch_op.add_column(
            sa.Column('sl_manual_override', sa.Boolean(),
                       nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table('transaction') as batch_op:
        batch_op.drop_column('sl_manual_override')
        batch_op.drop_column('tp_manual_override')
