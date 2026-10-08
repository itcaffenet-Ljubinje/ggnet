"""writeback lifecycle: keep writeback, session state, boot time

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-08 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('machines', schema=None) as batch_op:
        batch_op.add_column(sa.Column('keep_writeback', sa.Boolean(), nullable=False,
                                      server_default=sa.false()))
        # Existing clones may hold writes; treat them as dirty so they are
        # discarded at the next disconnect.
        batch_op.add_column(sa.Column('writeback_dirty', sa.Boolean(), nullable=False,
                                      server_default=sa.true()))
        batch_op.add_column(sa.Column('session_active', sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column('session_changed_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('booted_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('machines', schema=None) as batch_op:
        batch_op.drop_column('booted_at')
        batch_op.drop_column('session_changed_at')
        batch_op.drop_column('session_active')
        batch_op.drop_column('writeback_dirty')
        batch_op.drop_column('keep_writeback')
