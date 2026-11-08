"""Images: export of a version to VHDX/VHD as a backup job

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-08 02:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0024'
down_revision: Union[str, Sequence[str], None] = '0023'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Plain ADD COLUMN: nothing to rebuild (see 0021).
    op.add_column('backup_jobs', sa.Column('snapshot', sa.String(length=64), nullable=True))
    op.add_column('backup_jobs', sa.Column('format', sa.String(length=8), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('backup_jobs', 'format')
    op.drop_column('backup_jobs', 'snapshot')
