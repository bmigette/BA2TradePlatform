"""add subtype to expertrecommendation

Records which analysis use-case (ENTER_MARKET / OPEN_POSITIONS) produced each ExpertRecommendation,
so the live OPEN_POSITIONS manage-pass can select the right rec directly instead of joining through
MarketAnalysis.subtype. Nullable + additive: legacy rows read as NULL and the live selection keeps
its all-rec fallback for them. Stored by enum NAME to match MarketAnalysis.subtype.

Revision ID: 028a1c7f0e9d
Revises: b7c3d9f5a1e8, d5e1b9a3c842
Create Date: 2026-07-07 00:00:00.000000

Also MERGES the two pre-existing branch heads (b7c3d9f5a1e8 = tradingorder.data;
d5e1b9a3c842 = expertrecommendation.target_price) which touch different tables and had
never been merged, so `alembic upgrade head` now resolves to a single head again.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '028a1c7f0e9d'
down_revision = ('b7c3d9f5a1e8', 'd5e1b9a3c842')
branch_labels = None
depends_on = None


def upgrade():
    # Nullable enum column stored by NAME (ENTER_MARKET / OPEN_POSITIONS), matching how
    # marketanalysis.subtype is persisted. On SQLite this is a TEXT column with a CHECK; the enum
    # name is reused so no duplicate type is created on Postgres.
    op.add_column(
        'expertrecommendation',
        sa.Column('subtype',
                  sa.Enum('ENTER_MARKET', 'OPEN_POSITIONS', name='analysisusecase'),
                  nullable=True),
    )


def downgrade():
    op.drop_column('expertrecommendation', 'subtype')
