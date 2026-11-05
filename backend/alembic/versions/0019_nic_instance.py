"""Machines: the boot network card's device instance ID, as the agent reports it

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-26 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0019'
down_revision: Union[str, Sequence[str], None] = '0018'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('machines', schema=None) as batch_op:
        batch_op.add_column(sa.Column('nic_instance', sa.String(length=64), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('machines', schema=None) as batch_op:
        batch_op.drop_column('nic_instance')
