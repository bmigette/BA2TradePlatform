"""rename_portfolio_value_to_equity

Revision ID: a9bad42ec000
Revises: 4bfccb7e5f31
Create Date: 2025-10-23 11:01:11.596521

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a9bad42ec000'
down_revision: Union[str, Sequence[str], None] = '4bfccb7e5f31'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Rename columns in smartriskmanagerjob table for clarity
    # "value" could be confused with profit/loss, "equity" is clearer
    with op.batch_alter_table('smartriskmanagerjob', schema=None) as batch_op:
        batch_op.alter_column('initial_portfolio_value',
                              new_column_name='initial_portfolio_equity',
                              existing_type=sa.Float(),
                              existing_nullable=True)
        batch_op.alter_column('final_portfolio_value',
                              new_column_name='final_portfolio_equity',
                              existing_type=sa.Float(),
                              existing_nullable=True)


def downgrade() -> None:
    """Downgrade schema."""
    # Revert column renames
    with op.batch_alter_table('smartriskmanagerjob', schema=None) as batch_op:
        batch_op.alter_column('initial_portfolio_equity',
                              new_column_name='initial_portfolio_value',
                              existing_type=sa.Float(),
                              existing_nullable=True)
        batch_op.alter_column('final_portfolio_equity',
                              new_column_name='final_portfolio_value',
                              existing_type=sa.Float(),
                              existing_nullable=True)
