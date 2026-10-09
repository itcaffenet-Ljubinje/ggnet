"""agent inventory: IP, MAC, link speed, hardware

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-09 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0008'
down_revision: Union[str, Sequence[str], None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('machines', schema=None) as batch_op:
        batch_op.add_column(sa.Column('reported_ip', sa.String(length=45), nullable=True))
        batch_op.add_column(sa.Column('reported_mac', sa.String(length=17), nullable=True))
        batch_op.add_column(sa.Column('link_speed_mbps', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('hardware', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('machines', schema=None) as batch_op:
        batch_op.drop_column('hardware')
        batch_op.drop_column('link_speed_mbps')
        batch_op.drop_column('reported_mac')
        batch_op.drop_column('reported_ip')
