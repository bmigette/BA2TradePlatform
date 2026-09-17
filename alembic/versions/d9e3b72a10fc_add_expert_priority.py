"""Add expert scheduling priority (higher first, existing experts default to 1)."""
from alembic import op
import sqlalchemy as sa

revision = "d9e3b72a10fc"
down_revision = "c8f2a41d67be"
branch_labels = None
depends_on = None


def upgrade():
    if "priority" not in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("expertinstance")}:
        op.add_column("expertinstance", sa.Column("priority", sa.Integer(), nullable=False, server_default="1"))


def downgrade():
    op.drop_column("expertinstance", "priority")
