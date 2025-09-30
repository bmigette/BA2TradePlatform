"""add_account_id_to_tradingorder

Revision ID: 3271f7f4e2f2
Revises: 0ed865bdeda6
Create Date: 2025-09-30 11:46:35.946255

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3271f7f4e2f2'
down_revision: Union[str, Sequence[str], None] = '0ed865bdeda6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


"""add_account_id_to_tradingorder

Revision ID: 3271f7f4e2f2
Revises: 0ed865bdeda6
Create Date: 2025-09-30 11:46:35.946255

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3271f7f4e2f2'
down_revision: Union[str, Sequence[str], None] = '0ed865bdeda6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


"""add_account_id_to_tradingorder

Revision ID: 3271f7f4e2f2
Revises: 0ed865bdeda6
Create Date: 2025-09-30 11:46:35.946255

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3271f7f4e2f2'
down_revision: Union[str, Sequence[str], None] = '0ed865bdeda6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


"""add_account_id_to_tradingorder

Revision ID: 3271f7f4e2f2
Revises: 0ed865bdeda6
Create Date: 2025-09-30 11:46:35.946255

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3271f7f4e2f2'
down_revision: Union[str, Sequence[str], None] = '0ed865bdeda6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


"""add_account_id_to_tradingorder

Revision ID: 3271f7f4e2f2
Revises: 0ed865bdeda6
Create Date: 2025-09-30 11:46:35.946255

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3271f7f4e2f2'
down_revision: Union[str, Sequence[str], None] = '0ed865bdeda6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Populate existing orders with the first account (assuming there's at least one account)
    # Get the first account ID
    connection = op.get_bind()
    result = connection.execute(sa.text("SELECT id FROM accountdefinition LIMIT 1"))
    first_account_id = result.fetchone()
    
    if first_account_id:
        # Update all existing orders to use this account
        op.execute(sa.text(f"UPDATE tradingorder SET account_id = {first_account_id[0]} WHERE account_id IS NULL"))
    
    # Make the column NOT NULL (if it's still nullable)
    # Note: SQLite doesn't support adding foreign key constraints via ALTER TABLE,
    # so the foreign key will be enforced by SQLAlchemy at the application level


def downgrade() -> None:
    """Downgrade schema."""
    # For downgrade, we just set account_id back to NULL since we can't drop the column
    # in a production environment without data loss
    op.execute(sa.text("UPDATE tradingorder SET account_id = NULL"))
