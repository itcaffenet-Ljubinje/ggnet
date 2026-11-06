"""Images: the default OS image and game disk for new PCs

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-06 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0021'
down_revision: Union[str, Sequence[str], None] = '0020'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # A plain ADD COLUMN: batch mode would rebuild game_disks, which machines
    # and image_hardware reference (SQLite refuses with foreign keys on).
    op.add_column('game_disks', sa.Column('is_default', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('game_disks', 'is_default')     # SQLite 3.35+
