"""remove_unused_smartriskmanagerjobanalysis_table

Revision ID: 4b4da307fef4
Revises: 61f48ea983b3
Create Date: 2025-10-24 22:17:46.427264

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4b4da307fef4'
down_revision: Union[str, Sequence[str], None] = '61f48ea983b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema - Remove unused SmartRiskManagerJobAnalysis table."""
    # Drop the unused junction table
    # All trading actions are now stored in SmartRiskManagerJob.graph_state['actions_log']
    op.drop_table('smartriskmanagerjobanalysis')


def downgrade() -> None:
    """Downgrade schema - Recreate SmartRiskManagerJobAnalysis table."""
    # Recreate the table in case we need to rollback
    op.create_table(
        'smartriskmanagerjobanalysis',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('smart_risk_job_id', sa.Integer(), nullable=False),
        sa.Column('market_analysis_id', sa.Integer(), nullable=False),
        sa.Column('consulted_at', sa.DateTime(), nullable=False),
        sa.Column('outputs_accessed', sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(['market_analysis_id'], ['marketanalysis.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['smart_risk_job_id'], ['smartriskmanagerjob.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_smartriskmanagerjobanalysis_market_analysis_id'), 'smartriskmanagerjobanalysis', ['market_analysis_id'], unique=False)
    op.create_index(op.f('ix_smartriskmanagerjobanalysis_smart_risk_job_id'), 'smartriskmanagerjobanalysis', ['smart_risk_job_id'], unique=False)
