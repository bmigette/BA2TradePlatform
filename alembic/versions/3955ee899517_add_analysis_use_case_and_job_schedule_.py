"""Add analysis use case and job schedule improvements

Revision ID: 3955ee899517
Revises: 86ba370fd69d
Create Date: 2025-09-24 22:08:42.614796

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision: str = '3955ee899517'
down_revision: Union[str, Sequence[str], None] = '86ba370fd69d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Create JobSchedule table
    op.create_table('jobschedule',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('expert_instance_id', sa.Integer(), nullable=False),
        sa.Column('symbol', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('subtype', sa.Enum('ENTER_MARKET', 'OPEN_POSITIONS', name='analysisusecase'), nullable=False),
        sa.Column('is_manual', sa.Boolean(), nullable=False),
        sa.Column('scheduled_time', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('task_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('status', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.ForeignKeyConstraint(['expert_instance_id'], ['expertinstance.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    
    # Add columns to existing tables (SQLite compatible)
    with op.batch_alter_table('marketanalysis') as batch_op:
        batch_op.add_column(sa.Column('subtype', sa.Enum('ENTER_MARKET', 'OPEN_POSITIONS', name='analysisusecase'), nullable=True))
    
    with op.batch_alter_table('ruleset') as batch_op:
        batch_op.add_column(sa.Column('type', sa.Enum('TRADING_RECOMMENDATION_RULE', name='experteventruletype'), nullable=True))
        batch_op.add_column(sa.Column('subtype', sa.Enum('ENTER_MARKET', 'OPEN_POSITIONS', name='analysisusecase'), nullable=True))
    
    # Data migration: Set default values for new columns
    connection = op.get_bind()
    
    # Update MarketAnalysis records with default subtype
    connection.execute(sa.text("UPDATE marketanalysis SET subtype = 'ENTER_MARKET' WHERE subtype IS NULL"))
    
    # Update Ruleset records with default type and subtype
    connection.execute(sa.text("UPDATE ruleset SET type = 'TRADING_RECOMMENDATION_RULE' WHERE type IS NULL"))
    connection.execute(sa.text("UPDATE ruleset SET subtype = 'ENTER_MARKET' WHERE subtype IS NULL"))
    
    # Now make MarketAnalysis.subtype NOT NULL after setting defaults
    with op.batch_alter_table('marketanalysis') as batch_op:
        batch_op.alter_column('subtype', nullable=False)
    
    # Make Ruleset.type NOT NULL after setting defaults
    with op.batch_alter_table('ruleset') as batch_op:
        batch_op.alter_column('type', nullable=False)


def downgrade() -> None:
    """Downgrade schema."""
    # Drop added columns
    with op.batch_alter_table('ruleset') as batch_op:
        batch_op.drop_column('subtype')
        batch_op.drop_column('type')
    
    with op.batch_alter_table('marketanalysis') as batch_op:
        batch_op.drop_column('subtype')
    
    # Drop JobSchedule table
    op.drop_table('jobschedule')
