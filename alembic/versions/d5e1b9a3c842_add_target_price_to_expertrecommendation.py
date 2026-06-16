"""add_target_price_to_expertrecommendation

Revision ID: d5e1b9a3c842
Revises: b7f2c1a4e9d3
Create Date: 2026-06-16 10:10:00.000000

Adds the nullable ``target_price`` column to ``expertrecommendation`` so experts
can persist a recommended target/take-profit price. Nullable with no backfill:
existing rows keep NULL and the bracket logic derives from
``expected_profit_percent`` when target_price is None.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd5e1b9a3c842'
down_revision: Union[str, Sequence[str], None] = 'b7f2c1a4e9d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('expertrecommendation', schema=None) as batch_op:
        batch_op.add_column(sa.Column('target_price', sa.Float(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('expertrecommendation', schema=None) as batch_op:
        batch_op.drop_column('target_price')
