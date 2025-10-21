"""add tradingorder data JSON column

Revision ID: b7c3d9f5a1e8
Revises: fe4892ad6036
Create Date: 2025-10-21 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'b7c3d9f5a1e8'
down_revision = 'fe4892ad6036'
branch_labels = None
depends_on = None


def upgrade():
    # Add nullable JSON column `data` to `tradingorder` table
    bind = op.get_bind()
    dialect_name = bind.dialect.name
    if dialect_name == 'sqlite':
        # SQLite: ALTER TABLE ADD COLUMN with JSON stored as TEXT
        op.add_column('tradingorder', sa.Column('data', sa.JSON(), nullable=True))
    else:
        # Other DBs (Postgres) support native JSON
        op.add_column('tradingorder', sa.Column('data', sa.JSON(), nullable=True))


def downgrade():
    op.drop_column('tradingorder', 'data')
