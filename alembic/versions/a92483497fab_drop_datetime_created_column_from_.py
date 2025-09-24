"""drop datetime_created column from AnalysisOutput

Revision ID: a92483497fab
Revises: 90c713a1c75b
Create Date: 2025-09-24 15:13:40.872988

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a92483497fab'
down_revision: Union[str, Sequence[str], None] = '2b4cf753ba81'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Drop the old datetime_created column using batch mode for SQLite
    with op.batch_alter_table('analysisoutput', schema=None) as batch_op:
        batch_op.drop_column('datetime_created')


def downgrade() -> None:
    """Downgrade schema."""
    # Add back the datetime_created column and copy data from created_at
    with op.batch_alter_table('analysisoutput', schema=None) as batch_op:
        batch_op.add_column(sa.Column('datetime_created', sa.DateTime(), nullable=False))
    
    # Copy data from created_at to datetime_created
    op.execute('UPDATE analysisoutput SET datetime_created = created_at')
