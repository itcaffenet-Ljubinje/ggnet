"""OS images: the network cards Add new hardware installed in them

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-27 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0020'
down_revision: Union[str, Sequence[str], None] = '0019'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'image_hardware',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('disk_id', sa.Integer(), nullable=False),
        sa.Column('nic', sa.String(length=64), nullable=False),
        sa.Column('instance', sa.String(length=64), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('snapshot', sa.String(length=64), nullable=True),
        sa.Column('added_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['disk_id'], ['game_disks.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('disk_id', 'nic', 'instance', name='uq_image_hardware_card'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('image_hardware')
