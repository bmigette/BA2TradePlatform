"""make_portfolio_equity_nullable

Revision ID: 61f48ea983b3
Revises: a9bad42ec000
Create Date: 2025-10-23 11:08:37.398761

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '61f48ea983b3'
down_revision: Union[str, Sequence[str], None] = 'a9bad42ec000'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Make portfolio equity fields nullable (they are set at start/end of execution, not on creation)
    with op.batch_alter_table('smartriskmanagerjob', schema=None) as batch_op:
        batch_op.alter_column('initial_portfolio_equity',
                              existing_type=sa.Float(),
                              nullable=True)
        batch_op.alter_column('final_portfolio_equity',
                              existing_type=sa.Float(),
                              nullable=True)


def downgrade() -> None:
    """Downgrade schema."""
    # Revert to NOT NULL (this may fail if there are NULL values)
    with op.batch_alter_table('smartriskmanagerjob', schema=None) as batch_op:
        batch_op.alter_column('initial_portfolio_equity',
                              existing_type=sa.Float(),
                              nullable=False)
        batch_op.alter_column('final_portfolio_equity',
                              existing_type=sa.Float(),
                              nullable=False)
