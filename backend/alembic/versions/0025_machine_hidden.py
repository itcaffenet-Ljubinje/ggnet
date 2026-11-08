"""Machines: hidden (a PC kept out of the list, e.g. while it is repaired)

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-08 23:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0025'
down_revision: Union[str, Sequence[str], None] = '0024'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Plain ADD COLUMN: nothing to rebuild (see 0021).
    op.add_column('machines', sa.Column('hidden', sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('machines') as b:
        b.drop_column('hidden')
