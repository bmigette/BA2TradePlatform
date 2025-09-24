"""Add risk_level, time_horizon, and market_analysis_id to ExpertRecommendation (SQLite compatible)

Revision ID: 2b4cf753ba81
Revises: 
Create Date: 2025-09-24 12:34:39.644647

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2b4cf753ba81'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Check if the columns already exist (from the failed migration)
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [col['name'] for col in inspector.get_columns('expertrecommendation')]
    
    # Add columns if they don't exist
    if 'market_analysis_id' not in columns:
        op.add_column('expertrecommendation', sa.Column('market_analysis_id', sa.Integer(), nullable=True))
    
    if 'risk_level' not in columns:
        op.add_column('expertrecommendation', sa.Column('risk_level', sa.Enum('LOW', 'MEDIUM', 'HIGH', name='risklevel'), nullable=False))
    
    if 'time_horizon' not in columns:
        op.add_column('expertrecommendation', sa.Column('time_horizon', sa.Enum('SHORT_TERM', 'MEDIUM_TERM', 'LONG_TERM', name='timehorizon'), nullable=False))
    
    # Add foreign key constraint if it doesn't exist
    try:
        op.create_foreign_key('fk_expertrecommendation_market_analysis', 'expertrecommendation', 'marketanalysis', ['market_analysis_id'], ['id'], ondelete='CASCADE')
    except Exception:
        # Foreign key might already exist or fail in SQLite, ignore
        pass
    
    # For SQLite, we can't easily change the recommended_action column type from VARCHAR to ENUM
    # Since SQLite doesn't enforce ENUMs strictly anyway, we'll leave it as VARCHAR
    # The application code will handle the enum conversion


def downgrade() -> None:
    """Downgrade schema."""
    # Remove foreign key constraint (if it exists)
    try:
        op.drop_constraint('fk_expertrecommendation_market_analysis', 'expertrecommendation', type_='foreignkey')
    except Exception:
        pass
    
    # Remove added columns
    try:
        op.drop_column('expertrecommendation', 'time_horizon')
    except Exception:
        pass
    
    try:
        op.drop_column('expertrecommendation', 'risk_level')
    except Exception:
        pass
    
    try:
        op.drop_column('expertrecommendation', 'market_analysis_id')
    except Exception:
        pass
