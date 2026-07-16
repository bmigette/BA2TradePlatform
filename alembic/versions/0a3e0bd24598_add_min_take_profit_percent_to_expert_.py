"""add min_take_profit_percent to expert_recommendation

Revision ID: 0a3e0bd24598
Revises: 028a1c7f0e9d
Create Date: 2026-07-16 10:58:26.568474

Hand-trimmed from the autogenerate output: the raw diff also picked up unrelated
pre-existing schema drift (llmusagelog index/column changes, smartriskmanagerjob
REAL->Float, recommended_action VARCHAR->Enum) that predates this change and isn't
reviewed/intended here -- this migration touches ONLY expertrecommendation.
min_take_profit_percent, added with a server_default so it applies cleanly to a table
that already has rows (SQLite requires a DEFAULT for a NOT NULL ADD COLUMN).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0a3e0bd24598'
down_revision: Union[str, Sequence[str], None] = '028a1c7f0e9d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('expertrecommendation', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'min_take_profit_percent', sa.Float(), nullable=False, server_default='2.0'
        ))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('expertrecommendation', schema=None) as batch_op:
        batch_op.drop_column('min_take_profit_percent')
