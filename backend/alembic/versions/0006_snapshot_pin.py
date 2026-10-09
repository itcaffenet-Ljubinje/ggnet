"""snapshot pin per machine

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-09 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0006'
down_revision: Union[str, Sequence[str], None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('machines', schema=None) as batch_op:
        batch_op.add_column(sa.Column('pinned_snapshot', sa.String(length=64), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('machines', schema=None) as batch_op:
        batch_op.drop_column('pinned_snapshot')
