"""Backup and restore jobs (Images -> Backup / Restore, History)

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-06 18:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0023'
down_revision: Union[str, Sequence[str], None] = '0022'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('backup_jobs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('image_id', sa.Integer(), nullable=True),
    sa.Column('image_name', sa.String(length=64), nullable=False),
    sa.Column('location', sa.String(length=128), nullable=False),
    sa.Column('location_label', sa.String(length=255), nullable=False),
    sa.Column('folder', sa.String(length=512), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('done_bytes', sa.BigInteger(), nullable=False),
    sa.Column('total_bytes', sa.BigInteger(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['image_id'], ['images.id'], name=op.f('fk_backup_jobs_image_id_images'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_backup_jobs'))
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('backup_jobs')
