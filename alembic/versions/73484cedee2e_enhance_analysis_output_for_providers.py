"""Enhance AnalysisOutput for data providers

Revision ID: 73484cedee2e
Revises: fe4892ad6036
Create Date: 2025-10-08 00:00:00.000000

This migration enhances the AnalysisOutput table to support the new data provider
architecture with better tracking, caching, and metadata capabilities.

Changes:
- Add provider_category and provider_name columns for provider identification
- Add symbol, start_date, end_date for data range tracking and caching
- Add format_type to track output format (dict/markdown)
- Add metadata JSON column for provider-specific data
- Make market_analysis_id nullable for standalone provider outputs
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite


# revision identifiers, used by Alembic.
revision: str = '73484cedee2e'
down_revision: Union[str, None] = 'fe4892ad6036'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema to support data provider architecture."""
    
    # Add new columns for provider tracking
    op.add_column('analysisoutput', 
        sa.Column('provider_category', sa.String(), nullable=True,
                  comment='Provider category (news, indicators, fundamentals, etc.)')
    )
    op.add_column('analysisoutput', 
        sa.Column('provider_name', sa.String(), nullable=True,
                  comment='Provider name (alpaca, yfinance, alphavantage, etc.)')
    )
    
    # Add columns for data identification and caching
    op.add_column('analysisoutput', 
        sa.Column('symbol', sa.String(), nullable=True,
                  comment='Stock symbol if applicable')
    )
    op.add_column('analysisoutput', 
        sa.Column('start_date', sa.DateTime(), nullable=True,
                  comment='Date range start for caching')
    )
    op.add_column('analysisoutput', 
        sa.Column('end_date', sa.DateTime(), nullable=True,
                  comment='Date range end for caching')
    )
    op.add_column('analysisoutput', 
        sa.Column('format_type', sa.String(), nullable=True,
                  comment="Format type: 'dict' or 'markdown'")
    )
    
    # Add metadata JSON column (named provider_metadata to avoid SQLAlchemy conflict)
    op.add_column('analysisoutput', 
        sa.Column('provider_metadata', sa.JSON(), nullable=True,
                  comment='Provider-specific metadata')
    )
    
    # Make market_analysis_id nullable for standalone provider outputs
    # Note: SQLite doesn't support ALTER COLUMN directly, we need to recreate the table
    with op.batch_alter_table('analysisoutput', schema=None) as batch_op:
        batch_op.alter_column('market_analysis_id',
                              existing_type=sa.INTEGER(),
                              nullable=True)


def downgrade() -> None:
    """Downgrade schema - remove provider enhancements."""
    
    # Remove new columns
    op.drop_column('analysisoutput', 'provider_metadata')
    op.drop_column('analysisoutput', 'format_type')
    op.drop_column('analysisoutput', 'end_date')
    op.drop_column('analysisoutput', 'start_date')
    op.drop_column('analysisoutput', 'symbol')
    op.drop_column('analysisoutput', 'provider_name')
    op.drop_column('analysisoutput', 'provider_category')
    
    # Restore market_analysis_id non-nullable
    with op.batch_alter_table('analysisoutput', schema=None) as batch_op:
        batch_op.alter_column('market_analysis_id',
                              existing_type=sa.INTEGER(),
                              nullable=False)
